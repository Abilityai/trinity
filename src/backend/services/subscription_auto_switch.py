"""
Subscription Auto-Switch Service (SUB-003).

Automatically switches an agent to a different subscription on the first
subscription failure — either a rate-limit (429) or an auth-class error
(401/403/credit balance/expired token, etc.).

Preconditions (all must be true):
1. Setting "auto_switch_subscriptions" is enabled (default: on, opt-out)
2. Agent has a subscription assigned (not API key)
3. At least one rate-limit / auth event recorded for this (agent, subscription)
4. At least one alternative subscription is available, not rate-limited, and
   (#2409) not currently refused by the provider — the survivors are ranked
   by cached headroom, furthest from the nearest wall first

Threshold note (#441): pre-#441 we required 2+ consecutive 429s before
switching. That guaranteed at least one user-visible failure on long-running
schedules and never fired on auth-class breakage at all. The 2h skip-list on
alternative selection (`select_best_alternative_subscription` +
`has_recent_subscription_failures` — kind-BLIND, and renamed from
`is_subscription_rate_limited` by #2352 precisely so this caller keeps counting
auth failures while the display surfaces stopped calling them rate limits) is
what prevents thrashing — see
`tests/unit/test_subscription_auto_switch_pingpong.py` for the regression
tests pinning that contract.
"""

import asyncio
import importlib
import logging
from typing import Optional

from database import db
from db_models import NotificationCreate

# Re-export the shared SUB-003 auth-class classifier (#1088) so existing
# consumers (routers/chat.py, services/task_execution_service.py) and their
# test patch targets keep importing `is_auth_failure` from this module
# unchanged. The redundant alias makes this an explicit re-export (recognised
# by ruff F401 + mypy --no-implicit-reexport).
from services.failure_classifier import is_auth_failure as is_auth_failure

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-agent switch lock (#799)
# ---------------------------------------------------------------------------
#
# Concurrent subscription failures on the SAME agent (two chat requests, or a
# chat overlapping a scheduled task) both enter `handle_subscription_failure`
# and, without mutual exclusion, both pick the same alternative, both
# `assign_subscription_to_agent`, and both fire `_restart_agent` — the second
# `container_stop` racing the first `start_agent_internal` wedges the container,
# duplicates the switch notification, or trips the #421 `was_already_running`
# ambiguity. A per-agent lock serializes the read→decide→assign→restart window.
#
# Mirrors the event-loop-safe lazy pattern in `services/agent_call_limiter.py`
# (module dict + a lazily-created guard) rather than `defaultdict(asyncio.Lock)`:
# a defaultdict binds each lock to whatever event loop is current at first key
# access and persists it, which breaks across pytest's per-test loops
# ("Future attached to a different loop"). Creating the locks lazily on the
# running loop avoids that.
#
# INVARIANT: process-local. Correct only while (a) the backend runs a single
# process and (b) the scheduler delegates execution to the backend via
# `/api/internal/execute-task` rather than calling this module in its own
# process — both true today. If the backend ever runs multiple workers, escalate
# to a Redis `SETNX` lock keyed `auto_switch:{agent_name}` (TTL ≥ longest
# plausible container start, ~60s).
_AGENT_SWITCH_LOCKS: dict[str, asyncio.Lock] = {}
_AGENT_SWITCH_LOCKS_GUARD: Optional[asyncio.Lock] = None


async def agent_switch_lock(agent_name: str) -> asyncio.Lock:
    """Return the per-agent switch lock, creating it lazily on the running loop."""
    global _AGENT_SWITCH_LOCKS_GUARD
    if _AGENT_SWITCH_LOCKS_GUARD is None:
        _AGENT_SWITCH_LOCKS_GUARD = asyncio.Lock()
    lock = _AGENT_SWITCH_LOCKS.get(agent_name)
    if lock is None:
        async with _AGENT_SWITCH_LOCKS_GUARD:
            lock = _AGENT_SWITCH_LOCKS.setdefault(agent_name, asyncio.Lock())
    return lock


def _reset_locks_for_test() -> None:
    """Test hook: drop all per-agent locks + the guard so each test's event loop
    starts clean (locks are loop-bound)."""
    global _AGENT_SWITCH_LOCKS_GUARD
    _AGENT_SWITCH_LOCKS.clear()
    _AGENT_SWITCH_LOCKS_GUARD = None


# The one selection tier the headroom service cannot produce: the ranking half
# failed and the pick fell back to the db's load-balance order. Beside the
# service's `SELECTION_MEASURED` / `SELECTION_UNKNOWN` / `SELECTION_REFUSED`.
SELECTION_UNRANKED = "unranked"


def _pct_or_na(value) -> str:
    return f"{value:.0f}%" if isinstance(value, (int, float)) else "n/a"


def select_best_alternative_subscription(current_subscription_id: str) -> Optional[tuple]:
    """Filter (db) → rank (cached headroom) → first. Returns `(subscription,
    why)` or None (#2409). Synchronous by design — call it via
    `asyncio.to_thread`: both reads are blocking and it runs under the
    per-agent switch lock.

    The db layer answers "which subscriptions are usable at all" (the 2h
    failure filter, kind-blind, #444/#2352 — FIRST and unchanged, so a
    candidate that just failed is never even read). This function answers
    "which of those is best": the survivors are ranked by the provider
    snapshot the sampler already cached — ONE `MGET`, never a probe — furthest
    from the nearest wall first (`subscription_headroom_service.
    rank_subscriptions`), and a subscription the provider is currently
    refusing is dropped. Fail-open on ANY failure of the ranking half — Redis
    down, an import that resolved to the wrong thing, a bug — to the db
    layer's own load-balance order, which is exactly the pre-#2409 pick, and
    LOUDLY: a silent fallback here is the "policy flip" class from
    learnings 2026-08-12, and an inert ranker must not look like a working
    one. The lazy `importlib` resolution is deliberate too: the service tests
    stub `services` as a bare module at load, and `import_module` answers
    from `sys.modules` rather than from a package attribute a previous test
    may have left behind.

    `why` is what the switch surfaces (activity, notification, log): the
    ranker's tier and figures for the pick, how many alternatives there were,
    and whether ambient refresh is on — because on a two-subscription install
    the ranking cannot change the pick, and the explanation IS the value.
    """
    survivors = db.list_viable_alternative_subscriptions(current_subscription_id)
    if not survivors:
        return None
    try:
        headroom = importlib.import_module("services.subscription_headroom_service")
        readings = headroom.cached_headroom_readings([c.id for c in survivors])
        ranked = headroom.rank_subscriptions(survivors, readings)
        auto_refresh = bool(headroom.is_auto_refresh_enabled())
    except Exception as e:  # noqa: BLE001 — the ranking may fail; the switch may not
        logger.warning(
            "[#2409] headroom ranking unavailable (%s: %s) — choosing the "
            "alternative for subscription %s by load-balance order",
            type(e).__name__, e, current_subscription_id,
        )
        chosen = survivors[0]
        return chosen, {
            "tier": SELECTION_UNRANKED,
            "seven_day_pct": None, "five_hour_pct": None,
            "seven_day_resets_at": None, "five_hour_resets_at": None,
            "reading_age_seconds": None,
            "candidates": len(survivors), "auto_refresh_enabled": None,
        }
    if not ranked:
        logger.warning(
            "[#2409] every alternative to subscription %s (%d candidate(s)) is "
            "currently refused by the provider (probe 429, blocking window or "
            "rejected token) — not switching onto a subscription that cannot serve",
            current_subscription_id, len(survivors),
        )
        return None
    chosen = ranked[0]
    why = headroom.describe_reading(readings.get(chosen.id))
    why["candidates"] = len(survivors)
    why["auto_refresh_enabled"] = auto_refresh
    if all(readings.get(c.id) is None for c in survivors):
        if auto_refresh:
            logger.info(
                "[#2409] no fresh headroom reading for any of %d alternative(s) to "
                "subscription %s — chosen by load-balance order",
                len(survivors), current_subscription_id,
            )
        else:
            logger.warning(
                "[#2409] no headroom reading for any of %d alternative(s) to "
                "subscription %s and ambient headroom refresh is OFF, so none "
                "will ever exist — headroom ranking is inert; chosen by "
                "load-balance order",
                len(survivors), current_subscription_id,
            )
    logger.info(
        "[#2409] alternative to subscription %s: '%s' (%s; 7d %s, 5h %s) "
        "out of %d candidate(s)",
        current_subscription_id, chosen.name, why["tier"],
        _pct_or_na(why["seven_day_pct"]), _pct_or_na(why["five_hour_pct"]),
        len(survivors),
    )
    return chosen, why


async def handle_subscription_failure(
    agent_name: str,
    error_message: str = "",
    failure_kind: str = "rate_limit",
) -> Optional[dict]:
    """
    Called when a subscription-backed agent fails with either a rate-limit (429)
    or an auth-class error.

    Records the event and triggers auto-switch on the first occurrence (subject
    to the alternative being viable per the 2h skip-list).

    Args:
        agent_name: name of the agent that failed
        error_message: server-side error string for audit + notification text
        failure_kind: "rate_limit" (429) or "auth" (401/403/credit/etc.)

    Returns:
        dict with switch details if auto-switch occurred, None otherwise.
    """
    # 1. Snapshot the agent's subscription BEFORE anything else. This is the
    # subscription our failure was (approximately) about. If a concurrent failure
    # switches the agent off it while we wait for the lock, our failure is stale.
    sub_at_entry = db.get_agent_subscription_id(agent_name)
    if not sub_at_entry:
        return None

    # 2. Record the failure event UNCONDITIONALLY — before the enabled gate
    # (#471). The event stream feeds the observability surfaces (Settings
    # usage cards, Dashboard pressure badges), and gating the *recording* on
    # auto-switch left operators who disabled automatic remediation — exactly
    # the population depending on manual visibility — with a permanently-zero
    # count. Attribution to the pre-lock snapshot is deliberate and MORE
    # correct than the old under-lock re-read: the failure genuinely happened
    # on `sub_at_entry`, and a stale failure (agent already switched) used to
    # record nothing at all. Recording is a single INSERT — it does not need
    # the #799 lock, which protects the read→decide→assign window.
    consecutive_count = db.record_rate_limit_event(
        agent_name=agent_name,
        subscription_id=sub_at_entry,
        error_message=error_message,
        failure_kind=failure_kind,
    )

    # 3. Check if auto-switch is enabled (default: on, #441). Cheap, lock-free —
    # a disabled platform never contends for the per-agent lock. The event
    # above is already on record either way.
    enabled = db.get_setting_value("auto_switch_subscriptions", default="true") == "true"
    if not enabled:
        return None

    # #799: serialize the read→decide→assign→restart window per agent so two
    # concurrent failures on the same agent can't both switch + restart it.
    async with await agent_switch_lock(agent_name):
        # Re-read under the lock. If another coroutine already switched the agent
        # off `sub_at_entry`, this failure is stale — return rather than switch
        # again. This is what makes the fix correct for 3+ subscriptions: without
        # it, a loser whose failure was about sub-A would attribute it to the new
        # current sub-B and cascade A→B→C (#799 / Codex C8).
        current_sub_id = db.get_agent_subscription_id(agent_name)
        if current_sub_id != sub_at_entry:
            logger.info(
                f"[SUB-003] Agent '{agent_name}' already switched off subscription "
                f"{sub_at_entry} (now {current_sub_id}) before this {failure_kind} "
                f"failure acquired the lock — stale failure, skipping"
            )
            return None

        # 4. Find a viable alternative subscription. (The failure event was
        # already recorded at step 2, pre-gate, against `sub_at_entry` — which
        # equals `current_sub_id` on this non-stale path. Auth-class events
        # share the same table with `failure_kind` persisted since #471;
        # `has_recent_subscription_failures` treats any event in the 2h window
        # as a reason to skip the subscription as a candidate, which is the
        # behavior we want for both kinds of failure. #2352 gave that predicate
        # its own name: `is_subscription_rate_limited` now means real 429s only,
        # for the badges, and MUST NOT be substituted back in here.)
        # #2409: the filter is the db's, the ranking is ours, and both reads
        # are blocking — off the loop, since we hold the per-agent lock.
        picked = await asyncio.to_thread(
            select_best_alternative_subscription, current_sub_id
        )
        if not picked:
            logger.warning(
                f"[SUB-003] Agent '{agent_name}' hit a {failure_kind} failure on "
                f"subscription {current_sub_id} (event #{consecutive_count}) "
                f"but no viable alternative subscription is available"
            )
            return None
        alternative, destination_headroom = picked

        # Get current subscription name for logging / notification
        current_sub = db.get_subscription(current_sub_id)
        old_name = current_sub.name if current_sub else current_sub_id

        # 5. Perform the switch (still under the lock — the assign + restart must
        # not interleave with a concurrent switch for this agent).
        return await _perform_auto_switch(
            agent_name=agent_name,
            old_subscription_id=current_sub_id,
            old_subscription_name=old_name,
            new_subscription=alternative,
            failure_kind=failure_kind,
            event_count=consecutive_count,
            destination_headroom=destination_headroom,
        )


async def handle_rate_limit_error(
    agent_name: str,
    error_message: str = "",
) -> Optional[dict]:
    """Backward-compatible shim — delegates to `handle_subscription_failure`
    with `failure_kind="rate_limit"`. Existing 429 callers don't need to
    migrate atomically.
    """
    return await handle_subscription_failure(
        agent_name=agent_name,
        error_message=error_message,
        failure_kind="rate_limit",
    )


def _failure_phrase(failure_kind: str) -> str:
    """Notification + log wording per failure kind."""
    if failure_kind == "auth":
        return "an authentication failure"
    return "a rate-limit error"


def _destination_clause(why) -> str:
    """One sentence on WHY this destination (#2409) — the issue's own complaint
    was that nothing surfaced when the destination was a bad choice. Fail-soft:
    a malformed reason yields no clause, never a failed switch."""
    if not isinstance(why, dict):
        return ""
    try:
        n = why.get("candidates")
        of = f" of the {n} alternatives" if isinstance(n, int) and n > 1 else ""
        tier = why.get("tier")
        if tier == "measured":
            week, day = why.get("seven_day_pct"), why.get("five_hour_pct")
            parts = []
            if isinstance(week, (int, float)):
                parts.append(f"{week:.0f}% of its weekly limit")
            if isinstance(day, (int, float)):
                parts.append(f"{day:.0f}% of its 5-hour limit")
            used = f" ({' and '.join(parts)} used)" if parts else ""
            return f" It had the most headroom{of}{used}."
        if tier == "unknown":
            off = (
                " (ambient headroom refresh is off)"
                if why.get("auto_refresh_enabled") is False else ""
            )
            return (
                f" No fresh headroom reading was available for it{off}; "
                f"it was chosen by load-balance order."
            )
        return ""
    except Exception:  # noqa: BLE001 — wording must never break a switch
        return ""


async def _perform_auto_switch(
    agent_name: str,
    old_subscription_id: str,
    old_subscription_name: str,
    new_subscription,
    failure_kind: str,
    event_count: int,
    destination_headroom: Optional[dict] = None,
) -> dict:
    """
    Execute the subscription switch: DB update, container restart, log, notify.

    `destination_headroom` (#2409) is the selector's `why` — surfaced on the
    activity, the notification and the result so an operator can see how
    full the destination was when it was chosen. Optional: the fail-open
    selector and older callers pass nothing and get the pre-#2409 wording.
    """
    phrase = _failure_phrase(failure_kind)
    logger.info(
        f"[SUB-003] Auto-switching agent '{agent_name}' from '{old_subscription_name}' "
        f"to '{new_subscription.name}' after {phrase}"
    )

    # Switch subscription in DB
    db.assign_subscription_to_agent(agent_name, new_subscription.id)

    # NOTE: Do NOT clear rate-limit events for the old subscription here. The
    # events are the signal that the old subscription just failed —
    # `has_recent_subscription_failures()` counts them over a 2h window
    # regardless of kind (#2352), and `list_viable_alternative_subscriptions()`
    # uses that to filter candidates (#2409: filter there, rank here).
    # Clearing here causes a ping-pong between exhausted subscriptions because
    # the old sub looks viable on the next cycle (issue #444). Events age out
    # naturally via the 2h query window (enforced by iso_cutoff — see
    # utils/helpers.py, issue #476) and the 24h cleanup in
    # services/cleanup_service.py removes them from disk.

    # Rotate the subscription token on the running container via hot-reload so
    # in-flight turns survive the switch (#1089). Falls back to a full restart on
    # a 404 (old base image without the endpoint), transport failure, or when no
    # token is resolvable — identical to the previous recreate behavior.
    restart_result = await _hot_reload_subscription_token(agent_name)

    # Log activity event
    from services.activity_service import activity_service
    from models import ActivityType, ActivityState

    activity_id = await activity_service.track_activity(
        agent_name=agent_name,
        activity_type=ActivityType.SCHEDULE_END,  # System event
        triggered_by="system",
        details={
            "action": "subscription_auto_switch",
            "old_subscription": old_subscription_name,
            "new_subscription": new_subscription.name,
            "failure_kind": failure_kind,
            "event_count": event_count,
            "restart_result": restart_result,
            "destination_headroom": destination_headroom,
        },
    )
    await activity_service.complete_activity(
        activity_id=activity_id,
        status=ActivityState.COMPLETED,
        details={"message": f"Auto-switched from '{old_subscription_name}' to '{new_subscription.name}'"},
    )

    # Send notification to agent owner
    try:
        db.create_notification(
            agent_name=agent_name,
            data=NotificationCreate(
                notification_type="alert",
                title=f"Subscription auto-switched to '{new_subscription.name}'",
                message=(
                    f"Agent '{agent_name}' was automatically switched from subscription "
                    f"'{old_subscription_name}' to '{new_subscription.name}' after {phrase}."
                    + _destination_clause(destination_headroom)
                ),
                priority="high",
                category="subscription",
                metadata={
                    "old_subscription": old_subscription_name,
                    "new_subscription": new_subscription.name,
                    "failure_kind": failure_kind,
                    "event_count": event_count,
                    "destination_headroom": destination_headroom,
                },
            )
        )
    except Exception as e:
        logger.error(f"[SUB-003] Failed to send auto-switch notification for '{agent_name}': {e}")

    result = {
        "switched": True,
        "agent_name": agent_name,
        "old_subscription": old_subscription_name,
        "new_subscription": new_subscription.name,
        "failure_kind": failure_kind,
        "event_count": event_count,
        "restart_result": restart_result,
        "destination_headroom": destination_headroom,
    }

    logger.info(f"[SUB-003] Auto-switch complete: {result}")
    return result


async def _restart_agent(agent_name: str) -> str:
    """Restart an agent container to apply the new subscription token."""
    try:
        from services.docker_service import get_agent_container, get_agent_status_from_container
        from services.docker_utils import container_stop
        from services.agent_service import start_agent_internal

        container = get_agent_container(agent_name)
        if not container:
            return "no_container"

        agent_status = get_agent_status_from_container(container)
        if agent_status.status != "running":
            return "not_running"

        await container_stop(container)
        await start_agent_internal(agent_name)
        return "success"
    except Exception as e:
        logger.error(f"[SUB-003] Failed to restart agent '{agent_name}': {e}")
        return f"failed: {e}"


async def _hot_reload_subscription_token(agent_name: str) -> str:
    """Push the agent's current DB subscription token to the running container
    via ``POST /api/credentials/reload-token`` (#1089).

    The agent server mutates its own ``os.environ["CLAUDE_CODE_OAUTH_TOKEN"]``,
    so the NEXT claude subprocess uses the rotated token while in-flight turns
    keep their already-inherited old token and finish — "rotate a credential"
    is no longer the same operation as "kill every running turn".

    Falls back to the full ``_restart_agent`` recreate path (today's behavior,
    no regression) on:
      - a 404 — an old base image that predates the endpoint,
      - any transport / circuit failure (``AgentClientError`` family), or
      - no resolvable token for the agent's current subscription.
    Returns ``"no_container"`` / ``"not_running"`` when the agent is not a
    running container, mirroring ``_restart_agent``.

    Invariant every caller relies on (#2114): a call here implies the agent is
    subscription-backed AT SEND TIME — structurally enforced, not conventional:
    all three producers (auto-switch, manual sub→sub reassignment, key-rollover
    fan-out) are sub→sub by construction (auth-MODE changes recreate instead),
    and this helper re-resolves the subscription from the DB below, falling
    back to restart when no token resolves. That is what makes
    ``remove_api_key=True`` safe: a subscription-backed Claude agent never has
    a legitimate ``ANTHROPIC_API_KEY`` at spawn, and post-#1999 the `.env`
    file is a second source for it that no recreate ever cleans — a stale key
    there shadows every spawn (Claude Code prefers the key over the OAuth
    token). Non-Claude runtimes keep ``False``: a legacy subscription row on a
    Gemini/Codex agent must not strip a `.env` key its own scripts may use.
    """
    try:
        from services.docker_service import (
            get_agent_container,
            get_agent_status_from_container,
        )
        from services.agent_client import get_agent_client, AgentClientError
        from services.agent_service.helpers import is_claude_runtime

        container = get_agent_container(agent_name)
        if not container:
            return "no_container"
        if get_agent_status_from_container(container).status != "running":
            return "not_running"

        sub_id = db.get_agent_subscription_id(agent_name)
        token = db.get_subscription_token(sub_id) if sub_id else None
        if not token:
            # No token to push (e.g. assignment cleared mid-flight). Fall back to
            # the recreate path, which re-bakes Config.Env from the DB.
            return await _restart_agent(agent_name)

        # #2114: remove_api_key=True for Claude runtimes. The old False leaned on
        # "subscription agents never carry ANTHROPIC_API_KEY in env (popped at
        # create time, lifecycle.py)" — true for Config.Env, false for the .env
        # FILE post-#1999 (re-read at every spawn, survives every recreate on
        # the workspace volume). True force-unsets the key at the spawn layer
        # without touching the file. Label read is best-effort with the same
        # claude-code default as docker_service.get_agent_runtime.
        try:
            runtime = container.labels.get("trinity.agent-runtime", "claude-code") or "claude-code"
        except Exception:
            runtime = "claude-code"

        client = get_agent_client(agent_name)
        try:
            resp = await client.post(
                "/api/credentials/reload-token",
                json={"token": token, "remove_api_key": is_claude_runtime(runtime)},
                timeout=10.0,
            )
        except AgentClientError as e:
            logger.warning(
                f"[SUB-003] hot-reload transport failure for '{agent_name}': {e}; "
                f"falling back to restart"
            )
            return await _restart_agent(agent_name)

        if resp.status_code >= 400:  # 404 = old base image without the endpoint
            logger.info(
                f"[SUB-003] hot-reload returned HTTP {resp.status_code} for "
                f"'{agent_name}'; falling back to restart"
            )
            return await _restart_agent(agent_name)

        # #2114: the endpoint reports (names only) which force-unset keys the
        # agent's .env would otherwise deliver to spawns. Surface it HERE — the
        # backend log operators actually read during a subscription incident —
        # instead of only a once-per-boot line in the container log.
        # Agent-supplied data: validate shape INSIDE the try — a tampered
        # response (non-list, non-str items) must degrade to "no warning",
        # never TypeError out of the function-level except and demote an
        # already-successful hot-reload into a container restart.
        try:
            raw_shadow = (resp.json() or {}).get("env_shadow") or []
            if not isinstance(raw_shadow, list):
                raw_shadow = []
            env_shadow = [k for k in raw_shadow if isinstance(k, str)][:8]
        except Exception:
            env_shadow = []
        if env_shadow:
            logger.warning(
                f"[SUB-003] agent '{agent_name}': .env carries "
                f"{', '.join(env_shadow)} — would shadow subscription auth at "
                f"spawn; suppressed via force-unset. If that key previously "
                f"authenticated this agent, its auth source is now the "
                f"subscription (#2114)"
            )

        logger.info(f"[SUB-003] Hot-reloaded subscription token for '{agent_name}' (no recreate)")
        return "hot_reloaded"
    except Exception as e:
        logger.error(
            f"[SUB-003] hot-reload error for '{agent_name}': {e}; falling back to restart"
        )
        return await _restart_agent(agent_name)


async def reload_subscription_for_all_agents(subscription_id: str) -> dict[str, str]:
    """Hot-reload the subscription token on every running agent assigned to
    `subscription_id` (#1089 key rollover — re-registering a subscription's
    token via the `/api/subscriptions` upsert).

    Best-effort per agent, each under the #799 per-agent switch lock so a
    rollout can't interleave with a concurrent auto-switch: a failure on one
    agent is logged and does NOT abort the fan-out or block the others. Stopped
    agents are skipped by the helper (`not_running`) — they pick up the new
    token on next start (Config.Env is re-baked from the DB on recreate).
    Returns ``{agent_name: result}`` for observability.
    """
    results: dict[str, str] = {}
    for agent_name in db.get_agents_by_subscription(subscription_id):
        try:
            async with await agent_switch_lock(agent_name):
                results[agent_name] = await _hot_reload_subscription_token(agent_name)
        except Exception as e:
            logger.error(
                f"[SUB-003] key-rollover hot-reload failed for '{agent_name}': {e}"
            )
            results[agent_name] = f"failed: {e}"
    return results
