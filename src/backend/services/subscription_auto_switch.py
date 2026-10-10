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
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from database import db
from db_models import NotificationCreate
from utils.helpers import parse_iso_timestamp, utc_now_iso

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


def _readmit_recovered(headroom, current_subscription_id: str) -> tuple:
    """The skip-list override (#2638). Returns `(readmitted, why_by_id)`.

    `db.list_viable_alternative_subscriptions` excludes every subscription with
    ANY failure event in a flat 2-hour window. That window is a proxy for "the
    provider is still refusing it", and on a two-subscription install one stale
    event is the difference between a turn that completes on the other
    subscription and a turn the user watches fail: #2320's own evidence was
    exactly this — "no viable alternative", with an alternative sitting there.

    So the exclusion is overridden PER CANDIDATE, on positive evidence only —
    `subscription_headroom_service.recovery_verdict`, which either has a fresh
    provider reading saying the token is being served or the provider's own
    reset instant for the blocked window, elapsed and predating the failure.
    Absence of evidence readmits nothing, which is what keeps #444's ping-pong
    closed: the loop there was caused by FORGETTING a failure, and nothing here
    forgets one.

    Raises rather than swallows: the caller's `except` already degrades the
    whole ranking half to the pre-#2409 load-balance pick, and that degradation
    is the correct answer to "the evidence could not be read" — a local
    try/except here would readmit nobody while reporting success, which is the
    same silent-inertness this module warns about one function down.
    """
    skipped = db.list_recently_failed_alternatives(current_subscription_id)
    if not skipped:
        return [], {}
    ids = [c.id for c in skipped]
    # Bounded at the DISPLAY freshness, the same bound the evacuation door
    # (`_assigned_subscription_is_refused`) uses — a reading as old as the
    # window it overrules cannot overrule it, in either direction. The
    # selection bound (≥ 2h) would let a pre-wall "ok" readmit for two hours.
    fresh = headroom.cached_headroom_readings(
        ids, max_age_seconds=headroom.FRESHNESS_SECONDS
    )
    aged = headroom.cached_headroom_readings(
        ids, max_age_seconds=headroom.RECOVERY_INSTANT_MAX_AGE_SECONDS
    )
    last_failed = db.last_failure_at_by_subscription(ids)
    readmitted, why = [], {}
    for c in skipped:
        verdict = headroom.recovery_verdict(
            fresh.get(c.id), aged.get(c.id), last_failed.get(c.id)
        )
        if verdict:
            readmitted.append(c)
            why[c.id] = verdict
    if readmitted:
        logger.info(
            "[#2638] readmitting %d skip-listed alternative(s) to subscription "
            "%s on provider evidence: %s",
            len(readmitted), current_subscription_id,
            ", ".join(f"'{c.name}' ({why[c.id]})" for c in readmitted),
        )
    return readmitted, why


def _blocked_window_already_reset(reading, now: Optional[datetime] = None) -> bool:
    """A fresh reading whose BLOCKED window carries a `resets_at` that has
    already elapsed describes a quota that no longer exists (#3470, the
    `_readmit_recovered` rule applied to the last-resort rung). Fail-closed:
    an unreadable instant is not evidence the window rolled over."""
    if reading is None:
        return False
    now = now or datetime.now(timezone.utc)
    for window in (getattr(reading, "five_hour", None), getattr(reading, "seven_day", None)):
        if window is None or not getattr(window, "blocked", False):
            continue
        try:
            if window.resets_at and parse_iso_timestamp(window.resets_at) <= now:
                return True
        except Exception:  # noqa: BLE001 — a provider string we cannot read proves nothing
            continue
    return False


def _last_resort_candidates(
    current_subscription_id: str, exclude_ids, min_age_seconds: int
) -> list:
    """The skip-listed alternatives a WALK may still try (#3470 AC#2).

    `list_viable_alternative_subscriptions` drops every subscription with any
    failure event in a flat 2h window. For a turn that has already failed on
    everything else, that inference is the difference between a reply and a
    "try again later" against a pool with headroom: the AC names the rule —
    *the 2h skip-list must not block a candidate that is the last untried one*.
    Excluded here are only the subscriptions this TURN already ran on
    (`exclude_ids`) and, for autonomous triggers, candidates whose last failure
    is younger than `min_age_seconds` — a schedule firing every five minutes
    must not re-probe a refused subscription on every run. A trial moves no
    assignment (the caller commits only the subscription that served), so the
    #444 thrash — assignment flips and a notification per flip — cannot recur.
    """
    skipped = [
        c for c in db.list_recently_failed_alternatives(current_subscription_id)
        if c.id not in exclude_ids
    ]
    if not skipped or min_age_seconds <= 0:
        return skipped
    last_failed = db.last_failure_at_by_subscription([c.id for c in skipped])
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=min_age_seconds)
    aged = []
    for c in skipped:
        raw = last_failed.get(c.id)
        try:
            failed_at = parse_iso_timestamp(raw) if raw else None
        except Exception:  # noqa: BLE001 — unreadable ⇒ treat as recent (fail closed)
            failed_at = datetime.now(timezone.utc)
        if failed_at is None or failed_at <= cutoff:
            aged.append(c)
    return aged


def select_best_alternative_subscription(
    current_subscription_id: str,
    *,
    exclude_ids=(),
    last_resort_min_age_seconds: Optional[int] = None,
) -> Optional[tuple]:
    """Filter (db) → rank (cached headroom) → first. Returns `(subscription,
    why)` or None (#2409). Synchronous by design — call it via
    `asyncio.to_thread`: both reads are blocking and it runs under the
    per-agent switch lock.

    `exclude_ids` (#3470): subscriptions this turn already ran on — never a
    candidate again within the turn, whatever the skip-list says.
    `last_resort_min_age_seconds` (#3470): when not None, a turn that finds no
    viable or readmitted candidate may fall back to the skip-listed ones
    (`_last_resort_candidates`), still RANKED so a fresh provider refusal keeps
    excluding a candidate — the skip-list is an inference from past events;
    live evidence is not overridden. None (the default, every pre-#3470
    caller) keeps the skip-list absolute.

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
    exclude_ids = set(exclude_ids or ())
    survivors = [
        s for s in db.list_viable_alternative_subscriptions(current_subscription_id)
        if s.id not in exclude_ids
    ]
    last_resort_ids: set = set()
    try:
        headroom = importlib.import_module("services.subscription_headroom_service")
        readmitted, readmit_why = _readmit_recovered(headroom, current_subscription_id)
        readmitted = [c for c in readmitted if c.id not in exclude_ids]
        candidates = survivors + readmitted
        if not candidates and last_resort_min_age_seconds is not None:
            last_resort = _last_resort_candidates(
                current_subscription_id, exclude_ids, last_resort_min_age_seconds
            )
            last_resort_ids = {c.id for c in last_resort}
            candidates = last_resort
            if candidates:
                logger.info(
                    "[#3470] no viable alternative to subscription %s; trying %d "
                    "skip-listed candidate(s) as a last resort (untried this turn)",
                    current_subscription_id, len(candidates),
                )
        if not candidates:
            return None
        readings = headroom.cached_headroom_readings([c.id for c in candidates])
        # A candidate readmitted because its WINDOW RESET carries a reading
        # whose `blocked` flag describes the window that just rolled over —
        # and `rank_subscriptions` drops a blocked candidate as REFUSED. Left
        # as-is the readmission would be inert in exactly the case it exists
        # for: the ranker would throw the recovered subscription straight back
        # out. The number is stale and the flag is about a quota that no longer
        # exists, so the honest tier is UNKNOWN — it still ranks, after any
        # measured candidate, in load-balance order.
        #
        # `serving_now` readmissions are untouched: their reading is fresh and
        # says the provider is serving, so the ranker keeps them on its own.
        for sid, verdict in readmit_why.items():
            if verdict == headroom.RECOVERY_WINDOW_RESET:
                readings[sid] = None
        # #3470: the same rule for a last-resort candidate whose blocked window
        # has a reset instant already in the past — the flag is about a quota
        # that no longer exists, so it ranks UNKNOWN rather than REFUSED.
        for sid in last_resort_ids:
            if _blocked_window_already_reset(readings.get(sid)):
                readings[sid] = None
        ranked = headroom.rank_subscriptions(candidates, readings)
        auto_refresh = bool(headroom.is_auto_refresh_enabled())
    except Exception as e:  # noqa: BLE001 — the ranking may fail; the switch may not
        logger.warning(
            "[#2409] headroom ranking unavailable (%s: %s) — choosing the "
            "alternative for subscription %s by load-balance order",
            type(e).__name__, e, current_subscription_id,
        )
        # Readmission is deliberately NOT available here. It exists only as an
        # override backed by fresh provider evidence, and this branch is
        # precisely the one where that evidence could not be read — so the 2h
        # skip-list stands and the pick is the pre-#2409, pre-#2638 one.
        if not survivors:
            return None
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
            current_subscription_id, len(candidates),
        )
        return None
    chosen = ranked[0]
    why = headroom.describe_reading(readings.get(chosen.id))
    why["candidates"] = len(candidates)
    why["auto_refresh_enabled"] = auto_refresh
    # #2638: name the override on the pick that used it, so an operator reading
    # the notification can tell a never-failed candidate from a readmitted one.
    why["readmitted"] = readmit_why.get(chosen.id)
    # #3470: and a last-resort pick from one the skip-list would have excluded.
    why["last_resort"] = chosen.id in last_resort_ids
    if all(readings.get(c.id) is None for c in candidates):
        if auto_refresh:
            logger.info(
                "[#2409] no fresh headroom reading for any of %d alternative(s) to "
                "subscription %s — chosen by load-balance order",
                len(candidates), current_subscription_id,
            )
        else:
            logger.warning(
                "[#2409] no headroom reading for any of %d alternative(s) to "
                "subscription %s and ambient headroom refresh is OFF, so none "
                "will ever exist — headroom ranking is inert; chosen by "
                "load-balance order",
                len(candidates), current_subscription_id,
            )
    logger.info(
        "[#2409] alternative to subscription %s: '%s' (%s; 7d %s, 5h %s) "
        "out of %d candidate(s)",
        current_subscription_id, chosen.name, why["tier"],
        _pct_or_na(why["seven_day_pct"]), _pct_or_na(why["five_hour_pct"]),
        len(candidates),
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
            old_subscription_name=old_name,
            new_subscription=alternative,
            failure_kind=failure_kind,
            event_count=consecutive_count,
            destination_headroom=destination_headroom,
            old_subscription_id=current_sub_id,
        )


def _assigned_subscription_is_refused(subscription_id: str) -> Optional[str]:
    """Is the agent's OWN subscription known to be unable to serve, right now?

    Returns the evidence name (`"provider_refusing"` / `"recent_rate_limit"`) or
    `None`. Synchronous — both reads are blocking; call it via `asyncio.to_thread`.

    The two signals answer different questions and both are needed. The cached
    provider reading is ground truth but only exists where the sampler has been;
    the 2h `rate_limit` event is the platform's own record and exists even with
    ambient refresh off. Either alone leaves a real case uncovered.

    Deliberately the DISPLAY predicate `is_subscription_rate_limited` (429 only,
    #2352) and not the kind-blind candidate-skip one: this decides whether to
    move an agent OFF its subscription pre-emptively, and an auth failure is a
    credential problem that a different subscription may share (a `.env` shadow
    is per-agent, not per-subscription). Quota exhaustion is the case a switch
    actually fixes.

    Fail-CLOSED to `None` on every error: "we could not tell" must dispatch
    normally, because the alternative is refusing to run a turn that would very
    likely have worked. The post-failure switch (#792) is still behind it.

    THREE-STATE, not an OR (#447, and `recovery_verdict`'s rule one level over).
    A fresh reading that says *serving* ends the question — it does NOT fall
    through to the 2h event predicate. Written as `fresh_refusing OR db_events`
    this is exactly the shape #447 exists to replace, and it makes the two
    directions disagree: `recovery_verdict` readmits a subscription the provider
    is demonstrably serving, while this would keep evacuating agents off it on
    every dispatch — a hot-reload and a high-priority notification per turn, and
    with two such subscriptions, a flap turn after turn. The db predicate is an
    inference from past failures; a probe is ground truth about now, so it wins
    in both directions. Absence of a fresh reading still falls through, which is
    the case the event arm was added for (ambient refresh off).
    """
    try:
        headroom = importlib.import_module("services.subscription_headroom_service")
        # Bounded at the DISPLAY freshness (`FRESHNESS_SECONDS`, 30 min), not at
        # `cached_headroom_readings`' default SELECTION bound
        # (`MAX_READING_AGE_SECONDS`, >= 2h). The default is calibrated for
        # RANKING candidates, where a stale reading is better than none; this
        # call decides whether a verdict may OVERRULE the 2h event predicate,
        # and a reading as old as the window it overrules cannot. Without the
        # bound a two-hour-old "serving" reading suppresses a five-minute-old
        # 429 and pins the agent on a subscription that is refusing it right
        # now — the #447 rule ("a probe is ground truth about NOW") applied to a
        # probe that is no longer about now.
        #
        # This is the same bound `_headroom_indicates_healthy` uses for the same
        # judgement one module over, and the same one the file already declares
        # for the mirror case: `REFUSAL_FRESHNESS_SECONDS = FRESHNESS_SECONDS`,
        # "a refusal is trusted exactly as long as the LIMIT badge trusts one".
        # It tightens the refusing arm too, which is deliberate and safe — a
        # stale refusal now falls through to the event predicate rather than
        # evacuating on its own.
        reading = headroom.cached_headroom_readings(
            [subscription_id], max_age_seconds=headroom.FRESHNESS_SECONDS
        ).get(subscription_id)
        if reading is not None:
            return "provider_refusing" if reading.refusing else None
    except Exception as e:  # noqa: BLE001 — unreadable evidence proves nothing
        logger.warning(
            "[#2638] could not read the headroom snapshot for subscription %s "
            "(%s) — dispatching without a pre-emptive switch",
            subscription_id, type(e).__name__,
        )
    try:
        if db.is_subscription_rate_limited(subscription_id):
            return "recent_rate_limit"
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "[#2638] could not read failure events for subscription %s (%s)",
            subscription_id, type(e).__name__,
        )
    return None


async def ensure_serviceable_subscription(agent_name: str) -> Optional[dict]:
    """#2638 AC#3 — switch BEFORE the first dispatch when the assigned
    subscription is already known not to serve and an alternative exists.

    SUB-003 has always been reactive: the turn is dispatched, the provider
    refuses it, and #792 re-issues once on the new subscription. That works, but
    the first message after a subscription hits its wall always burns a failed
    attempt — and on the Workspace it is a person watching their message fail.
    Everything needed to avoid it is already known at dispatch time; nothing was
    reading it.

    Returns the same dict `handle_subscription_failure` returns, or `None` for
    "changed nothing" — which covers no subscription, no evidence, auto-switch
    disabled, and no viable alternative. It NEVER raises: a pre-flight
    optimisation must not be able to fail a turn that would otherwise run, and
    the reactive path remains the backstop for everything this declines to do.

    Records NO failure event. Nothing failed — that is the whole point — and a
    synthetic event would poison the very skip-list that decides where the agent
    may move next.
    """
    try:
        if db.get_setting_value("auto_switch_subscriptions", default="true") != "true":
            return None
        sub_at_entry = db.get_agent_subscription_id(agent_name)
        if not sub_at_entry:
            return None
        evidence = await asyncio.to_thread(_assigned_subscription_is_refused, sub_at_entry)
        if not evidence:
            return None

        async with await agent_switch_lock(agent_name):
            # Same stale-check as the reactive path: another coroutine may have
            # moved the agent while we waited for the lock, and its pick was
            # made on the same evidence ours was.
            current_sub_id = db.get_agent_subscription_id(agent_name)
            if current_sub_id != sub_at_entry:
                return None
            picked = await asyncio.to_thread(
                select_best_alternative_subscription, current_sub_id
            )
            if not picked:
                # #3470 Part 2: no subscription can serve — route THIS turn to
                # the platform API key by per-spawn override instead of
                # dispatching a doomed attempt. No assignment changes; the
                # dispatcher reads `route` and attaches the credential.
                if api_key_rung(agent_name) is not None:
                    logger.info(
                        "[#3470] agent '%s' is on a subscription that cannot serve "
                        "(%s) and no alternative is available — routing the turn "
                        "to the platform API key",
                        agent_name, evidence,
                    )
                    return {"switched": False, "route": "api_key", "evidence": evidence}
                logger.info(
                    "[#2638] agent '%s' is on a subscription that cannot serve "
                    "(%s) and no alternative is available — dispatching anyway, "
                    "so the failure is the provider's answer rather than ours",
                    agent_name, evidence,
                )
                return None
            alternative, destination_headroom = picked
            current_sub = db.get_subscription(current_sub_id)
            old_name = current_sub.name if current_sub else current_sub_id
            logger.info(
                "[#2638] pre-dispatch switch for '%s': '%s' is refused (%s) -> '%s'",
                agent_name, old_name, evidence, alternative.name,
            )
            # The SAME switch the reactive path performs (AC#6): one activity,
            # one notification, one hot-reload, so the Settings usage cards and
            # the Dashboard pressure badges keep counting whichever path fired.
            result = await _perform_auto_switch(
                agent_name=agent_name,
                old_subscription_name=old_name,
                new_subscription=alternative,
                failure_kind="rate_limit",
                event_count=0,
                destination_headroom=destination_headroom,
                pre_dispatch=True,
                old_subscription_id=current_sub_id,
            )
            result["evidence"] = evidence
            return result
    except Exception as e:  # noqa: BLE001 — never fail a turn from the pre-flight
        logger.error(
            "[#2638] pre-dispatch subscription check failed for '%s': %s",
            agent_name, e,
        )
        return None


API_KEY_FALLBACK_SETTING = "subscription_api_key_fallback"


def is_api_key_fallback_enabled() -> bool:
    """Default ON (#2638 AC#4), read at call time.

    A setting rather than `.env`: the operator who needs to turn this off is the
    one who registered subscriptions precisely so that spend goes through them,
    and that decision must be reachable from Settings → Subscriptions rather
    than from a redeploy. Fail-OPEN (enabled) on a read error — the failure this
    guards is a user's turn dying with a usable key sitting in settings.
    """
    try:
        return db.get_setting_value(API_KEY_FALLBACK_SETTING, default="true") == "true"
    except Exception as e:  # noqa: BLE001
        # The setting NAME is deliberately not interpolated. It is a hard-coded
        # constant one line above, so the log gains nothing from repeating it —
        # and CodeQL's clear-text-logging rule flags any `*_KEY`-shaped name
        # reaching a log call, which would leave a permanent false positive on
        # this file for every future PR. Removing the interpolation is cheaper
        # and more honest than a dismissal a later reader has to re-litigate.
        logger.warning(
            "[#2638] could not read the API-key fallback setting (%s) — "
            "treating it as enabled",
            type(e).__name__,
        )
        return True


def earliest_known_reset(subscription_ids) -> Optional[str]:
    """The soonest provider reset instant across a set of subscriptions.

    Read from AGED snapshots on purpose — the #447/#2396 asymmetry this codebase
    already states: a utilisation *number* decays, an *instant* does not. When a
    turn cannot run at all, "try again after 19:10" is the only useful thing the
    platform can say, and the sampler already knows it.

    Returns `None` when nothing is known, and the caller must then say nothing
    rather than guess — a fabricated time is worse than an unqualified "later".
    """
    try:
        headroom = importlib.import_module("services.subscription_headroom_service")
        readings = headroom.cached_headroom_readings(
            list(subscription_ids),
            max_age_seconds=headroom.RECOVERY_INSTANT_MAX_AGE_SECONDS,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("[#2638] earliest-reset lookup failed (%s)", type(e).__name__)
        return None
    instants = []
    now = datetime.now(timezone.utc)
    for reading in readings.values():
        if reading is None:
            continue
        for window in (reading.five_hour, reading.seven_day):
            if window is None or not window.blocked or not window.resets_at:
                continue
            # #3470: never tell a person their quota "resets at" an instant that
            # has already passed — a stale snapshot keeps its instants for up
            # to 7 days. Unreadable ⇒ dropped: a time we cannot parse is not
            # one we should display either.
            try:
                if parse_iso_timestamp(window.resets_at) <= now:
                    continue
            except Exception:  # noqa: BLE001
                continue
            instants.append(window.resets_at)
    if not instants:
        return None
    # ISO-Z strings sort lexicographically, which is why every timestamp in this
    # platform is written that way (Invariant #16).
    return min(instants)


# ---------------------------------------------------------------------------
# #3470 — walk the whole pool before a turn fails; the API key is a rung
# ---------------------------------------------------------------------------
#
# SUB-003 remediated at most ONCE per turn (#792: one switch, one re-issue;
# #2638: or one API-key fallback), and the fallback CLEARED the assignment and
# RESTARTED the container — a few-hour limit on a key with no credit became a
# permanent outage, and in-flight work died with the restart.
#
# The walk below is a *credential trial*. A refused attempt asks for the next
# rung — another subscription, then the platform API key — and the dispatcher
# re-issues the SAME turn with a request-scoped `auth_override` the agent
# applies to that one spawn (`AUTH_OVERRIDE_HEADER` confirms it did). Nothing
# in the container or the DB moves until a trial SERVES: the dispatcher then
# commits that subscription once (`commit_subscription_walk` →
# `_perform_auto_switch`: assign + hot-reload + one notification). A key that
# served changes no assignment at all — the fallback is per-turn routing, and
# the agent returns to its subscription the moment it can serve, with nothing
# to restore.
#
# Attribution is exact by construction: a failed trial ran on the credential
# the walk sent, so its failure event goes to THAT subscription (or the key),
# never to whatever the assignment reads after the fact — the #799 cascade
# (turn 2 blaming turn 1's destination) cannot happen. Liveness is the walk's
# own property: every rung grows `tried_subscription_ids` or sets
# `tried_api_key`, a pick already tried is treated as exhausted, and a hard cap
# of `len(subscriptions) + 2` rungs is computed once. The remaining turn budget
# is the dispatcher's bound (#2789 — the only ceiling).
#
# An agent image that predates the override ignores the field and runs the
# attempt on its baseline; the header is then absent and the walk degrades to
# LEGACY mode — commit-and-retry per rung, as #792 did — capped at one
# restart-based rung (a restart kills the agent's concurrent work and outruns
# the #2944 claim grace) and with no key rung (the restart-based key path IS the
# stranding bug this replaces).

# Mirrors `agent_server/services/execution_env.AUTH_OVERRIDE_HEADER` by name.
AUTH_OVERRIDE_HEADER = "X-Trinity-Auth-Override"

# A refused platform key is skipped for a while, like a refused subscription
# (#2409's "recently refused by the provider" rule). Kind-AWARE: an auth /
# billing refusal ("credit balance is too low", a revoked key) holds for the
# same 2h the subscription skip-list uses; a 429 is a burst, not "no credit",
# and a fleet-wide 2h skip for it would remove the last rung from every agent
# on one spike. One platform key ⇒ one marker, not agent-keyed.
API_KEY_REFUSAL_KEY = "platform_api_key:refused"
API_KEY_REFUSAL_TTL_SECONDS = {"auth": 2 * 3600, "rate_limit": 300}

# Autonomous triggers may try a skip-listed subscription only when its last
# failure is at least this old — a schedule firing every five minutes must not
# re-probe a refused subscription on every run. Interactive triggers (a person
# is waiting) try any untried candidate.
LAST_RESORT_MIN_AGE_AUTONOMOUS_SECONDS = 1800

# Transition notifications ("routed to the platform API key" / "returned to its
# subscription") and the exhausted-pool alert are deduped through TTL markers.
# Informational only — a stale or missing marker changes a notification, never
# a credential — so they are EXEMPT from the lifecycle clear (#1560 registry).
API_KEY_ROUTE_NOTICE_KEY = "agent:api_key_route:{name}"
API_KEY_ROUTE_NOTICE_TTL_SECONDS = 24 * 3600
POOL_EXHAUSTED_NOTICE_KEY = "agent:pool_exhausted_notice:{name}"
POOL_EXHAUSTED_NOTICE_TTL_SECONDS = 3600

_WALK_MAX_RUNGS_FALLBACK = 8


def _marker_redis():
    """The breaker Redis client, or None — every marker here is fail-open."""
    try:
        from redis_breaker_util import get_breaker_redis
        return get_breaker_redis()
    except Exception:  # noqa: BLE001
        return None


def record_api_key_refusal(failure_kind: str) -> None:
    """Remember that the platform key refused a turn (#3470 Part 2 AC#2)."""
    r = _marker_redis()
    if r is None:
        return
    ttl = API_KEY_REFUSAL_TTL_SECONDS.get(failure_kind, API_KEY_REFUSAL_TTL_SECONDS["auth"])
    try:
        r.set(API_KEY_REFUSAL_KEY, f"{failure_kind}@{utc_now_iso()}", ex=ttl)
    except Exception as e:  # noqa: BLE001
        logger.warning("[#3470] could not record the API-key refusal (%s)", type(e).__name__)


def is_api_key_recently_refused() -> bool:
    """Fail-OPEN: an unreadable marker means the key is tried, and a refusal is
    then one more exhausted candidate — never a turn that dies with a usable
    key sitting in settings."""
    r = _marker_redis()
    if r is None:
        return False
    try:
        return bool(r.get(API_KEY_REFUSAL_KEY))
    except Exception:  # noqa: BLE001
        return False


def clear_api_key_refusal() -> None:
    """Best-effort: a turn the key SERVED proves the marker stale."""
    r = _marker_redis()
    if r is None:
        return
    try:
        r.delete(API_KEY_REFUSAL_KEY)
    except Exception:  # noqa: BLE001
        pass


@dataclass
class Rung:
    """One step of a walk: the credential the NEXT attempt should run on.

    `credential` is the secret itself (an OAuth token or the platform key) and
    exists only to be placed on the dispatch payload — it is never logged,
    never persisted, never surfaced on a result. `kind`:
      "subscription"      — try another subscription (trial, commit if it serves)
      "api_key"           — try the platform key (per-turn routing, no commit)
      "concurrent_switch" — a concurrent turn already moved the agent; re-run
                            on the new assignment, nothing to override
    """
    kind: str
    subscription_id: Optional[str] = None
    subscription_name: Optional[str] = None
    # repr=False: a dataclass repr of a Rung — or of the SubscriptionWalk that
    # holds one as `active` — must never print the secret, so a stray
    # `logger.debug(f"{walk}")` or an assertion message cannot leak it.
    credential: Optional[str] = field(default=None, repr=False)
    why: Optional[dict] = None

    @property
    def label(self) -> str:
        return self.subscription_name or ("platform API key" if self.kind == "api_key" else "?")

    def payload(self) -> Optional[dict]:
        """The `auth_override` body for the agent, or None (nothing to send)."""
        if self.kind == "subscription" and self.credential:
            return {"oauth_token": self.credential}
        if self.kind == "api_key" and self.credential:
            return {"api_key": self.credential}
        return None

    @property
    def override_kind(self) -> Optional[str]:
        if self.kind == "subscription":
            return "oauth_token"
        if self.kind == "api_key":
            return "api_key"
        return None


@dataclass
class SubscriptionWalk:
    """Per-turn state of a SUB-003 walk (see the section comment)."""
    agent_name: str
    interactive: bool
    assigned_subscription_id: Optional[str] = None
    assigned_subscription_name: Optional[str] = None
    tried_subscription_ids: set = field(default_factory=set)
    tried_api_key: bool = False
    attempts: list = field(default_factory=list)
    # The override the CURRENT attempt runs on (None = the container baseline).
    active: Optional[Rung] = None
    # None until the first override-bearing response says yes/no.
    override_supported: Optional[bool] = None
    legacy_restart_used: bool = False
    # None until the first refusal (`_resolve_walk_lazies`); never read before.
    max_rungs: Optional[int] = None
    # Filled by `commit_subscription_walk` / legacy applies: the switch dict a
    # caller surfaces (`TaskExecutionResult.subscription_switch`).
    committed: Optional[dict] = None
    # Set once the walk has nothing further to offer; names why.
    stop_reason: Optional[str] = None

    @property
    def started(self) -> bool:
        return bool(self.attempts) or self.active is not None or self.committed is not None

    def ran_on(self) -> tuple:
        """`(kind, subscription_id, label)` of the credential the attempt that
        just came back ran on — exact, because it is what the walk sent."""
        if self.active is not None and self.active.kind == "api_key":
            return "api_key", None, "platform API key"
        if self.active is not None and self.active.kind == "subscription":
            return "subscription", self.active.subscription_id, self.active.label
        return (
            "subscription",
            self.assigned_subscription_id,
            self.assigned_subscription_name or self.assigned_subscription_id or "assigned subscription",
        )

    def trail_text(self) -> str:
        """One sentence for the FAILED row's `error` and the operator alert."""
        if not self.attempts:
            return ""
        steps = []
        for a in self.attempts:
            kind = "rate limit" if a.get("failure") == "rate_limit" else a.get("failure") or "refused"
            steps.append(f"{a.get('on')} ({kind})")
        tail = {
            "exhausted": "every candidate refused.",
            "budget": "stopped: the turn's time budget ran out.",
            "cap": "stopped: attempt cap reached.",
            "disabled": "automatic switching is off.",
            "legacy_restart": "stopped after a container restart.",
        }.get(self.stop_reason or "", "")
        return f" Tried: {' -> '.join(steps)}; {tail}".rstrip()

    def summary(self) -> Optional[dict]:
        """What rides `TaskExecutionResult.subscription_switch` (#2638 shape,
        extended): `switched` means the ASSIGNMENT changed; `retried` means at
        least one re-issue ran; `exhausted` means every rung refused."""
        if not self.started:
            return None
        base = dict(self.committed or {})
        base.setdefault("switched", False)
        base.setdefault("new_subscription", None)
        base["attempts"] = [dict(a) for a in self.attempts]
        base["retried"] = any(a.get("next") for a in self.attempts)
        base["exhausted"] = self.stop_reason in ("exhausted", "cap", "disabled", "legacy_restart")
        base["budget_exhausted"] = self.stop_reason == "budget"
        base["tried_subscription_ids"] = sorted(self.tried_subscription_ids)
        base["routed_api_key"] = bool(self.active is not None and self.active.kind == "api_key")
        return base


def _subscription_count_cap() -> int:
    try:
        return int(len(db.list_subscriptions())) + 2
    except Exception:  # noqa: BLE001 — a cap must exist even when the read fails
        return _WALK_MAX_RUNGS_FALLBACK


def start_subscription_walk(agent_name: str, triggered_by: Optional[str]) -> SubscriptionWalk:
    """A fresh walk for one turn. Snapshots the assignment the turn starts on
    (the baseline the first attempt runs on) and whether a person is waiting.
    ONE read on the hot path — the subscription's name and the rung cap are
    resolved lazily, on the first refusal, so a turn that never fails pays
    nothing more than it did before #3470."""
    try:
        from services.pull_pilot import INTERACTIVE_TRIGGERS
        interactive = (triggered_by or "") in INTERACTIVE_TRIGGERS
    except Exception:  # noqa: BLE001
        interactive = False
    walk = SubscriptionWalk(agent_name=agent_name, interactive=interactive)
    try:
        walk.assigned_subscription_id = db.get_agent_subscription_id(agent_name)
    except Exception as e:  # noqa: BLE001
        logger.warning("[#3470] could not read the assignment for '%s': %s", agent_name, type(e).__name__)
    return walk


def _resolve_walk_lazies(walk: SubscriptionWalk) -> None:
    """The reads a walk needs only once it has something to do."""
    if walk.max_rungs is None:
        walk.max_rungs = _subscription_count_cap()
    if walk.assigned_subscription_id and not walk.assigned_subscription_name:
        try:
            sub = db.get_subscription(walk.assigned_subscription_id)
            walk.assigned_subscription_name = getattr(sub, "name", None) or walk.assigned_subscription_id
        except Exception:  # noqa: BLE001
            walk.assigned_subscription_name = walk.assigned_subscription_id


def apply_pre_dispatch_result(walk: SubscriptionWalk, pre_switch: Optional[dict]) -> None:
    """Fold `ensure_serviceable_subscription`'s answer into the walk: a switch
    moves the baseline (its origin is known-refused, so it is TRIED); a key
    route makes the first attempt run on the key by override."""
    if not pre_switch:
        return
    if pre_switch.get("switched"):
        old_id = pre_switch.get("old_subscription_id")
        if old_id:
            walk.tried_subscription_ids.add(old_id)
        walk.assigned_subscription_id = pre_switch.get("new_subscription_id") or walk.assigned_subscription_id
        walk.assigned_subscription_name = pre_switch.get("new_subscription") or walk.assigned_subscription_name
        walk.committed = dict(pre_switch)
        return
    if pre_switch.get("route") == "api_key":
        rung = api_key_rung(walk.agent_name)
        if rung is not None:
            _resolve_walk_lazies(walk)
            walk.active = rung
            walk.attempts.append({
                "attempt": 0, "on": walk.assigned_subscription_name or "assigned subscription",
                "failure": "known_refused",
                "next": "routed to the platform API key before the first attempt",
            })


def api_key_rung(agent_name: str) -> Optional[Rung]:
    """The platform-key rung, or None when it must not be offered (#3470 Part 2).

    Offered only when: the fallback setting is on; the agent is a Claude
    runtime (a Claude key means nothing to Gemini/Codex); the agent's
    `use_platform_api_key` is not explicitly False (an operator's "never bill
    the key" is a hard rule — the column defaults to True, so this only ever
    refuses a deliberate opt-out); a key is configured; and the key is not
    recently refused. Never raises.
    """
    try:
        if not is_api_key_fallback_enabled():
            return None
        from services.agent_service.helpers import is_claude_runtime
        from services.docker_service import get_agent_container

        container = get_agent_container(agent_name)
        runtime = "claude-code"
        if container is not None:
            runtime = (container.labels or {}).get("trinity.agent-runtime") or runtime
        if not is_claude_runtime(runtime):
            return None
        try:
            if db.get_use_platform_api_key(agent_name) is False:
                return None
        except Exception:  # noqa: BLE001 — unreadable preference ⇒ the default (True)
            pass
        from services.settings_service import get_anthropic_api_key
        api_key = get_anthropic_api_key()
        if not api_key:
            return None
        if is_api_key_recently_refused():
            logger.info("[#3470] platform API key recently refused — not offered to '%s'", agent_name)
            return None
        return Rung(kind="api_key", credential=api_key)
    except Exception as e:  # noqa: BLE001 — a last resort must not become the failure
        logger.error("[#3470] API-key rung check failed for '%s': %s", agent_name, e)
        return None


async def fallback_to_api_key(agent_name: str) -> Optional[dict]:
    """#2638 AC#4, reshaped by #3470 Part 2: is the platform API key a rung this
    agent may try? Returns a credential-free summary or None — and changes
    NOTHING: no assignment cleared, no flag set, no restart. The key reaches
    the agent only as a per-spawn `auth_override` on the turn that needs it.
    Never raises.
    """
    rung = api_key_rung(agent_name)
    if rung is None:
        return None
    return {"switched": False, "fallback": "api_key", "agent_name": agent_name, "route": "api_key"}


async def advance_subscription_walk(
    walk: SubscriptionWalk,
    *,
    failure_kind: str,
    error_message: str = "",
    budget_ok: bool = True,
) -> Optional[Rung]:
    """The attempt that just came back was refused (`failure_kind`). Record it
    against the credential it ran on, then return the next rung — or None when
    the walk is over (`walk.stop_reason` says why). Never raises.

    `budget_ok=False` records the refusal (#471: unconditionally — the
    skip-list depends on it) and stops without picking.
    """
    agent_name = walk.agent_name
    if walk.assigned_subscription_id is None and walk.active is None:
        # A key-only / credential-less agent: there is no pool to walk and the
        # platform key is the very baseline that just refused. The pre-#3470
        # switcher returned early here too (`if not sub_at_entry`). No attempt
        # is recorded, so the caller's wording falls through to its own.
        walk.stop_reason = "no_subscription"
        return None
    _resolve_walk_lazies(walk)
    ran_kind, ran_id, ran_label = walk.ran_on()
    attempt = {"attempt": len(walk.attempts) + 1, "on": ran_label, "failure": failure_kind}
    walk.attempts.append(attempt)

    # 1. Record the refusal where it happened.
    try:
        if ran_kind == "api_key":
            walk.tried_api_key = True
            record_api_key_refusal(failure_kind)
        elif ran_id:
            walk.tried_subscription_ids.add(ran_id)
            db.record_rate_limit_event(
                agent_name=agent_name, subscription_id=ran_id,
                error_message=error_message, failure_kind=failure_kind,
            )
    except Exception as e:  # noqa: BLE001 — recording must never fail the turn
        logger.error("[#3470] could not record the refusal for '%s': %s", agent_name, e)
    walk.active = None

    # 2. Bounds.
    if not budget_ok:
        walk.stop_reason = "budget"
        return None
    if len(walk.attempts) > (walk.max_rungs or _WALK_MAX_RUNGS_FALLBACK):
        walk.stop_reason = "cap"
        return None
    if walk.legacy_restart_used:
        walk.stop_reason = "legacy_restart"
        return None
    try:
        enabled = db.get_setting_value("auto_switch_subscriptions", default="true") == "true"
    except Exception:  # noqa: BLE001
        enabled = True
    if not enabled:
        walk.stop_reason = "disabled"
        return None

    # 3. A concurrent turn may already have moved the agent (#799, AC#7): the
    #    attempt ran on the OLD baseline, the new assignment is untried — run
    #    on it rather than switching again or racing a restart.
    if ran_kind == "subscription" and ran_id and ran_id == walk.assigned_subscription_id:
        try:
            current = db.get_agent_subscription_id(agent_name)
        except Exception:  # noqa: BLE001
            current = None
        if current and current != ran_id and current not in walk.tried_subscription_ids:
            sub = db.get_subscription(current)
            name = getattr(sub, "name", None) or current
            walk.assigned_subscription_id = current
            walk.assigned_subscription_name = name
            attempt["next"] = f"re-run on '{name}' (moved by a concurrent turn)"
            return Rung(kind="concurrent_switch", subscription_id=current, subscription_name=name)

    # 4. Another subscription, excluding everything this turn ran on. A
    #    candidate whose token cannot be read is marked tried and the pick is
    #    repeated (bounded), so a healthy third subscription is still offered.
    base = walk.assigned_subscription_id or ran_id
    if base:
        min_age = 0 if walk.interactive else LAST_RESORT_MIN_AGE_AUTONOMOUS_SECONDS
        for _ in range(3):
            try:
                picked = await asyncio.to_thread(
                    select_best_alternative_subscription, base,
                    exclude_ids=set(walk.tried_subscription_ids),
                    last_resort_min_age_seconds=min_age,
                )
            except Exception as e:  # noqa: BLE001
                logger.error("[#3470] candidate selection failed for '%s': %s", agent_name, e)
                picked = None
            if not picked:
                break
            alternative, why = picked
            if alternative.id in walk.tried_subscription_ids:
                logger.warning(
                    "[#3470] selector offered an already-tried subscription to '%s' — "
                    "treating the pool as exhausted", agent_name,
                )
                break
            token = None
            try:
                token = db.get_subscription_token(alternative.id)
            except Exception as e:  # noqa: BLE001
                logger.error("[#3470] token for %s unreadable: %s", alternative.id, type(e).__name__)
            if token:
                rung = Rung(
                    kind="subscription", subscription_id=alternative.id,
                    subscription_name=alternative.name, credential=token, why=why,
                )
                walk.active = rung
                attempt["next"] = f"try '{alternative.name}'"
                if isinstance(why, dict) and why.get("last_resort"):
                    attempt["next"] += " (skip-listed; last untried)"
                return rung
            # An undecryptable token is a candidate that cannot serve.
            walk.tried_subscription_ids.add(alternative.id)

    # 5. The platform API key — once, by override only (never in legacy mode).
    if not walk.tried_api_key and walk.override_supported is not False:
        rung = api_key_rung(agent_name)
        if rung is not None:
            walk.active = rung
            attempt["next"] = "try the platform API key"
            return rung

    walk.stop_reason = "exhausted"
    return None


def note_override_honoured(walk: SubscriptionWalk, headers) -> bool:
    """After a dispatch that carried an override: did the agent apply it?

    Reads `AUTH_OVERRIDE_HEADER`. True ⇒ the attempt ran on `walk.active`.
    False ⇒ an older image ignored the field; the attempt ran on the BASELINE
    (so `walk.active` is cleared to keep attribution exact) and the walk is
    now in legacy mode. No-op (True) when nothing was overridden.
    """
    if walk.active is None:
        return True
    try:
        sent = headers.get(AUTH_OVERRIDE_HEADER) if headers is not None else None
    except Exception:  # noqa: BLE001
        sent = None
    if sent and sent == walk.active.override_kind:
        walk.override_supported = True
        return True
    if walk.override_supported is None:
        logger.warning(
            "[#3470] agent '%s' did not apply the per-spawn credential (image predates "
            "it) — SUB-003 falls back to commit-and-retry for this turn", walk.agent_name,
        )
    walk.override_supported = False
    walk.active = None
    return False


async def apply_rung_legacy(walk: SubscriptionWalk, rung: Rung, *, failure_kind: str) -> bool:
    """Legacy mode (old agent image): make the rung the ASSIGNMENT now —
    assign + hot-reload (or restart) + notification, exactly the #792 switch —
    so the next baseline dispatch runs on it. Returns False when the rung
    cannot be applied; a restart-based apply is allowed ONCE per turn.
    """
    if rung.kind != "subscription" or not rung.subscription_id:
        return False
    try:
        async with await agent_switch_lock(walk.agent_name):
            current = db.get_agent_subscription_id(walk.agent_name)
            if current != walk.assigned_subscription_id:
                # Someone else moved the agent meanwhile — run on that instead.
                sub = db.get_subscription(current) if current else None
                walk.assigned_subscription_id = current
                walk.assigned_subscription_name = getattr(sub, "name", None) or current
                return bool(current)
            new_sub = db.get_subscription(rung.subscription_id)
            if new_sub is None:
                return False
            result = await _perform_auto_switch(
                agent_name=walk.agent_name,
                old_subscription_name=walk.assigned_subscription_name or (current or "?"),
                new_subscription=new_sub,
                failure_kind=failure_kind,
                event_count=len(walk.attempts),
                destination_headroom=rung.why,
                old_subscription_id=current,
            )
    except Exception as e:  # noqa: BLE001
        logger.error("[#3470] legacy switch failed for '%s': %s", walk.agent_name, e)
        return False
    walk.committed = result
    walk.assigned_subscription_id = rung.subscription_id
    walk.assigned_subscription_name = rung.subscription_name
    walk.active = None
    if result.get("restart_result") != "hot_reloaded":
        # A recreate killed whatever else the agent was doing and takes longer
        # than the settle + connect backoff the re-issue allows. Once per turn.
        walk.legacy_restart_used = True
    return True


def _notice_marker(key: str, *, ttl: int) -> Optional[bool]:
    """SET NX a dedupe marker. True = first in the window (notify), False =
    already notified, None = Redis unavailable (notify — fail-open)."""
    r = _marker_redis()
    if r is None:
        return None
    try:
        return bool(r.set(key, utc_now_iso(), nx=True, ex=ttl))
    except Exception:  # noqa: BLE001
        return None


def _notice_marker_present(key: str) -> bool:
    r = _marker_redis()
    if r is None:
        return False
    try:
        return bool(r.get(key))
    except Exception:  # noqa: BLE001
        return False


def _clear_notice_marker(key: str) -> None:
    r = _marker_redis()
    if r is None:
        return
    try:
        r.delete(key)
    except Exception:  # noqa: BLE001
        pass


def _notify(agent_name: str, *, title: str, message: str, metadata: dict, priority: str = "high") -> None:
    try:
        db.create_notification(
            agent_name=agent_name,
            data=NotificationCreate(
                notification_type="alert", title=title, message=message,
                priority=priority, category="subscription", metadata=metadata,
            ),
        )
    except Exception as e:  # noqa: BLE001
        logger.error("[#3470] failed to notify about '%s' for '%s': %s", title, agent_name, e)


async def commit_subscription_walk(walk: SubscriptionWalk, *, failure_kind: str) -> Optional[dict]:
    """The attempt that just came back SERVED. Make its credential durable
    where that is the right thing, and say so once.

    * On a subscription trial: commit — `_perform_auto_switch` under the #799
      lock (assign + hot-reload + ONE notification), after re-checking that the
      assignment is still the one this turn started on (a concurrent commit
      wins; this one is then a no-op with a log line).
    * On the API key: no assignment changes (per-turn routing, Part 2 AC#3);
      the "routed to the platform API key" notification fires once per 24h
      transition and the refusal marker is cleared (it served).
    * On the baseline, after earlier turns ran on the key: the "returned to its
      subscription" notification (Part 2 AC#4), and the transition marker goes.
    Never raises. Returns the switch dict a caller surfaces, or None.
    """
    agent_name = walk.agent_name
    try:
        _resolve_walk_lazies(walk)
        rung = walk.active
        if rung is not None and rung.kind == "subscription" and rung.subscription_id:
            async with await agent_switch_lock(agent_name):
                current = db.get_agent_subscription_id(agent_name)
                if current != walk.assigned_subscription_id:
                    logger.info(
                        "[#3470] '%s' served on '%s' but a concurrent turn already moved the "
                        "agent to %s — not committing", agent_name, rung.label, current,
                    )
                    return walk.committed
                new_sub = db.get_subscription(rung.subscription_id)
                if new_sub is None:
                    return walk.committed
                # The #799 lock is process-local and prod runs several workers,
                # so the read above cannot see a sibling worker's commit. The
                # assignment write is therefore a compare-and-set on the value
                # this turn started from: the first worker wins, the second
                # reads False and skips its hot-reload and notification.
                if not db.assign_subscription_to_agent(
                    agent_name, rung.subscription_id,
                    expected_subscription_id=walk.assigned_subscription_id,
                ):
                    logger.info(
                        "[#3470] '%s': a sibling worker committed another subscription "
                        "first — not committing '%s'", agent_name, rung.label,
                    )
                    return walk.committed
                result = await _perform_auto_switch(
                    agent_name=agent_name,
                    old_subscription_name=walk.assigned_subscription_name or (current or "?"),
                    new_subscription=new_sub,
                    failure_kind=failure_kind,
                    event_count=len(walk.attempts),
                    destination_headroom=rung.why,
                    old_subscription_id=current,
                )
            walk.committed = result
            walk.assigned_subscription_id = rung.subscription_id
            walk.assigned_subscription_name = rung.subscription_name
            if _notice_marker_present(API_KEY_ROUTE_NOTICE_KEY.format(name=agent_name)):
                _clear_notice_marker(API_KEY_ROUTE_NOTICE_KEY.format(name=agent_name))
                _notify(
                    agent_name,
                    title=f"Returned to subscription '{rung.label}'",
                    message=(
                        f"Agent '{agent_name}' had been running on the platform API key while "
                        f"no subscription could serve it; it is back on subscription "
                        f"'{rung.label}'."
                    ),
                    metadata={"subscription": rung.label, "transition": "api_key_to_subscription"},
                    priority="normal",
                )
            return result
        if rung is not None and rung.kind == "api_key":
            clear_api_key_refusal()
            first = _notice_marker(
                API_KEY_ROUTE_NOTICE_KEY.format(name=agent_name),
                ttl=API_KEY_ROUTE_NOTICE_TTL_SECONDS,
            )
            if first is not False:
                origin = walk.assigned_subscription_name or "its subscription"
                _notify(
                    agent_name,
                    title="Switched to the platform API key",
                    message=(
                        f"Agent '{agent_name}' could not be served by subscription "
                        f"'{origin}' or any alternative, so this turn ran on the platform "
                        "API key. Its subscription assignment is unchanged; it returns to "
                        "the subscription as soon as that can serve again."
                    ),
                    metadata={"old_subscription": origin, "fallback": "api_key",
                              "transition": "subscription_to_api_key"},
                )
            logger.warning(
                "[#3470] agent '%s' served on the platform API key (assignment kept)", agent_name,
            )
            walk.committed = {
                "switched": False, "fallback": "api_key", "agent_name": agent_name,
                "old_subscription": walk.assigned_subscription_name, "new_subscription": None,
            }
            return walk.committed
        # Served on the baseline. If earlier turns had been routed to the key,
        # the subscription can serve again — say so and drop the marker.
        marker = API_KEY_ROUTE_NOTICE_KEY.format(name=agent_name)
        if walk.assigned_subscription_id and _notice_marker_present(marker):
            _clear_notice_marker(marker)
            name = walk.assigned_subscription_name or walk.assigned_subscription_id
            _notify(
                agent_name,
                title=f"Returned to subscription '{name}'",
                message=(
                    f"Agent '{agent_name}' had been running on the platform API key while its "
                    f"subscription could not serve; subscription '{name}' is serving again."
                ),
                metadata={"subscription": name, "transition": "api_key_to_subscription"},
                priority="normal",
            )
        return walk.committed
    except Exception as e:  # noqa: BLE001 — a committed, billed success must stand
        logger.error("[#3470] commit after a served trial failed for '%s': %s", agent_name, e)
        return walk.committed


def notify_pool_exhausted(walk: SubscriptionWalk, earliest_reset: Optional[str]) -> None:
    """Every candidate refused: tell the operator WHICH were tried and why, once
    per agent per hour (a Workspace user re-sending must not page per message).
    Never raises."""
    if not walk.attempts:
        return
    first = _notice_marker(
        POOL_EXHAUSTED_NOTICE_KEY.format(name=walk.agent_name),
        ttl=POOL_EXHAUSTED_NOTICE_TTL_SECONDS,
    )
    if first is False:
        return
    when = ""
    if earliest_reset:
        try:
            when = " Earliest known reset: " + parse_iso_timestamp(earliest_reset).strftime(
                "%H:%M UTC on %-d %b"
            ) + "."
        except Exception:  # noqa: BLE001
            when = ""
    _notify(
        walk.agent_name,
        title="No subscription could serve a turn",
        message=(
            f"Agent '{walk.agent_name}' tried every credential available to it and all "
            f"refused.{walk.trail_text()}{when}"
        ),
        metadata={
            "attempts": [dict(a) for a in walk.attempts],
            "tried_subscription_ids": sorted(walk.tried_subscription_ids),
            "tried_api_key": walk.tried_api_key,
            "earliest_reset": earliest_reset,
            "stop_reason": walk.stop_reason,
        },
    )


def walk_reset_candidates(walk: SubscriptionWalk) -> list:
    """Subscription ids whose reset instants a refusal message should consider:
    everything the turn ran on plus the current assignment."""
    ids = set(walk.tried_subscription_ids)
    if walk.assigned_subscription_id:
        ids.add(walk.assigned_subscription_id)
    return sorted(ids)


# #2643: strong refs for the fire-and-forget switches spawned below. asyncio
# holds only a WEAK reference to a bare `create_task`, so an un-referenced
# switch can be collected mid-flight — the #1083 `_inflight` footgun, and here
# it would drop the remediation silently on a path nobody is watching yet.
_inflight_switch_tasks: "set[asyncio.Task]" = set()


def spawn_subscription_failure(
    agent_name: str,
    *,
    error_message: str = "",
    failure_kind: str = "rate_limit",
) -> None:
    """Fire `handle_subscription_failure` from a SYNCHRONOUS terminal writer.

    The pull sink (`pull_coordination_service.apply_task_result`, #2643) is a
    sync function called from an async router handler, exactly like the #1578
    emit and the #1804 activity close it sits beside — so it gets the same
    wrapper shape rather than a bespoke `create_task` at the call site:
    `spawn_task_terminal_event` / `spawn_close_execution_activity`.

    Fail-open in both directions, because it runs AFTER a committed, billed
    terminal and must never be able to turn one into a 500 on the result
    endpoint: a raising switch is logged and swallowed, and no running loop is
    a skip (with the coroutine closed, so it cannot warn "never awaited").

    It deliberately does NOT re-deliver the turn. Re-delivery is the lease
    reaper's decision (#1081 Phase 3) and the #1085 governor's correlated-cause
    pause still gates it; this only makes sure the agent is on a subscription
    that can serve the NEXT attempt.
    """
    # ONE coroutine, and the handler call lives inside it — building the inner
    # coroutine here and wrapping it would leave TWO objects to close on the
    # no-loop path, and closing only the inner one still warns "never awaited"
    # for the wrapper.
    coro = _guarded_switch(
        agent_name, error_message=error_message, failure_kind=failure_kind
    )
    try:
        task = asyncio.create_task(coro)
    except RuntimeError as e:
        coro.close()
        logger.debug(
            "[SUB-003] pull-path switch skipped for '%s' (no running loop): %s",
            agent_name, e,
        )
        return
    _inflight_switch_tasks.add(task)
    task.add_done_callback(_inflight_switch_tasks.discard)


async def _guarded_switch(
    agent_name: str, *, error_message: str, failure_kind: str
) -> None:
    """Run a spawned switch, swallowing anything it raises (#2643)."""
    try:
        await handle_subscription_failure(
            agent_name=agent_name,
            error_message=error_message,
            failure_kind=failure_kind,
        )
    except Exception as e:  # noqa: BLE001 — never affect the billed terminal
        logger.error(
            "[SUB-003] auto-switch failed for '%s' after a pull terminal: %s",
            agent_name, e,
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


def _failure_phrase(failure_kind: str, *, pre_dispatch: bool = False) -> str:
    """Notification + log wording per failure kind.

    #2638: a PRE-DISPATCH switch has no failure behind it — that is its whole
    point — so it must not be described as one. An operator reading "switched
    after a rate-limit error" for a turn that never ran would go looking for a
    failed execution that does not exist.
    """
    if pre_dispatch:
        return "its subscription was found unable to serve, before the turn ran"
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
    old_subscription_name: str,
    new_subscription,
    failure_kind: str,
    event_count: int,
    destination_headroom: Optional[dict] = None,
    pre_dispatch: bool = False,
    old_subscription_id: Optional[str] = None,
) -> dict:
    """
    Execute the subscription switch: DB update, container restart, log, notify.

    `destination_headroom` (#2409) is the selector's `why` — surfaced on the
    activity, the notification and the result so an operator can see how
    full the destination was when it was chosen. Optional: the fail-open
    selector and older callers pass nothing and get the pre-#2409 wording.
    """
    phrase = _failure_phrase(failure_kind, pre_dispatch=pre_dispatch)
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
            "pre_dispatch": pre_dispatch,
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
                    "pre_dispatch": pre_dispatch,
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
        # #3470: ids beside the names, so a walk can exclude the origin from
        # its candidates and attribute later failures exactly.
        "old_subscription_id": old_subscription_id,
        "new_subscription_id": new_subscription.id,
        "failure_kind": failure_kind,
        "event_count": event_count,
        "restart_result": restart_result,
        "destination_headroom": destination_headroom,
        "pre_dispatch": pre_dispatch,
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
