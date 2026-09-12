"""
Subscription Service (SUB-002)

Manages Claude Max/Pro subscription token assignment and auth mode detection.
Tokens are generated via `claude setup-token` (~1 year lifetime) and injected
as `CLAUDE_CODE_OAUTH_TOKEN` env var on agent containers at creation time.

No file injection is needed — the token is part of the container environment.
"""

import asyncio
import importlib
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from database import db
from db_models import AgentAuthStatus, SubscriptionCredential

logger = logging.getLogger(__name__)


def derive_auth_mode(has_subscription: bool, has_api_key: bool) -> str:
    """The ONE auth-mode enum derivation (#471) — shared by the per-agent
    `AgentAuthStatus` resolver below and the fleet subscription-pressure batch
    endpoint, so the two surfaces use one vocabulary by construction
    ("subscription" | "api_key" | "not_configured")."""
    if has_subscription:
        return "subscription"
    if has_api_key:
        return "api_key"
    return "not_configured"


async def get_agent_auth_mode(agent_name: str) -> AgentAuthStatus:
    """
    Detect the authentication mode for an agent.

    Determines auth purely from DB state:
    1. If agent has a subscription assigned → "subscription"
    2. If agent has use_platform_api_key enabled → "api_key"
    3. Otherwise → "not_configured"

    Args:
        agent_name: Name of the agent

    Returns:
        AgentAuthStatus with detected mode
    """
    # Check for subscription assignment
    subscription = db.get_agent_subscription(agent_name)
    has_subscription = subscription is not None

    # Check for platform API key setting
    has_api_key = db.get_use_platform_api_key(agent_name) or False

    # Determine auth mode (shared derivation, #471)
    auth_mode = derive_auth_mode(has_subscription, has_api_key)

    return AgentAuthStatus(
        agent_name=agent_name,
        auth_mode=auth_mode,
        subscription_name=subscription.name if subscription else None,
        subscription_id=subscription.id if subscription else None,
        has_api_key=has_api_key,
    )


def select_subscription_for_new_agent() -> Optional[SubscriptionCredential]:
    """The subscription a NEW Claude agent is auto-assigned to (#74), chosen
    the way auto-switch chooses an alternative (#2409): the db lists every
    subscription that has not failed recently (kind-blind, #2352) in
    load-balance order, the cached provider headroom ranks them furthest from
    the nearest wall first and drops any the provider is currently refusing,
    and the first candidate whose token still decrypts (#340) wins — a
    viability filter walked in RANKED order, so the common case costs one
    decrypt instead of one per subscription.

    `database` is resolved at CALL time on purpose: the agent-creation test
    harnesses stub it per test, and a module-level binding taken on first
    import would answer the previous test's stub (learnings 2026-08-12).
    Fail-open on the ranking half only — Redis down or a bad import degrades
    to load-balance order, which is exactly the pre-#2409 round-robin — and
    loudly, so an inert ranker cannot pass for a working one.
    """
    from database import db as _db

    candidates = _db.list_assignable_subscriptions()
    if not candidates:
        return None
    try:
        headroom = importlib.import_module("services.subscription_headroom_service")
        readings = headroom.cached_headroom_readings([c.id for c in candidates])
        ranked = headroom.rank_subscriptions(candidates, readings)
    except Exception as e:  # noqa: BLE001 — the ranking may fail; assignment may not
        logger.warning(
            "[#2409] headroom ranking unavailable for new-agent assignment "
            "(%s: %s) — using load-balance order", type(e).__name__, e,
        )
        ranked = list(candidates)
    for sub in ranked:
        # #340: skip invalid/legacy tokens. Order-neutral, so it runs AFTER the
        # ranking rather than decrypting every candidate up front.
        if _db.get_subscription_token(sub.id):
            return sub
    return None



# ============================================================================
# #2572 — credential-less agents adopt an available subscription
# ============================================================================
#
# The gap this closes sits BETWEEN two features that each behave as written:
# SUB-003 (``subscription_auto_switch``) only ever moves an agent that already
# HAS a subscription (its precondition 2 — "not api key"), and registering a
# subscription assigned no agent at all. So on an instance with no
# ``ANTHROPIC_API_KEY`` — the default for an unattended / marketplace install —
# every pre-existing agent sat in ``api_key`` mode with nothing behind it, and
# the one action the product tells the operator to take changed nothing.
#
# THREE triggers, and deliberately no periodic sweep (the operator's decision,
# 2026-09-12):
#   A1  ``POST /api/subscriptions``                     — routers/subscriptions.py
#   A2  the two Anthropic-key clear paths               — routers/settings.py
#   B   agent creation — ALREADY shipped by #74 (``crud._apply_subscription_env``),
#       which gates only on ``is_claude_runtime`` and so already covers a keyless
#       instance. Pinned by test, not re-implemented here.
#
# SUB-003's precondition is NOT relaxed: this is a second, event-driven trigger,
# not a change to failure-driven switching.
#
# The cardinal rule the issue makes non-negotiable — "agents with a working API
# key are unaffected, silently migrating them would move real spend onto
# someone's personal plan" — is guaranteed STRUCTURALLY by condition 1 below:
# the sweep short-circuits to "adopt nobody" whenever the platform can resolve
# an Anthropic key at all.

#: Above this many adoptions in one sweep, log a WARNING with the elapsed time.
#: Deliberately a warning and NOT a cap — a cap bounds the blast radius but
#: silently strands the remainder, and (no periodic sweep) nothing would come
#: back for them.
ADOPTION_SWEEP_WARN = 50

#: ``details.trigger`` on the audit rows — which event ran the sweep.
TRIGGER_SUBSCRIPTION_REGISTERED = "subscription_registered"
TRIGGER_INSTANCE_KEY_DELETED = "instance_key_deleted"

#: One sweep at a time per process. A second sweep SKIPS rather than queueing:
#: both callers are event hooks on an admin action, and two of them racing would
#: each pay the whole fleet scan to reach the same fixed point. Reachable in
#: practice — the Settings panel and the MCP ``register_subscription`` tool hit
#: the same endpoint.
_SWEEP_LOCK: Optional[asyncio.Lock] = None

#: Strong references to the backgrounded apply tasks. A bare ``create_task``
#: result is only WEAKLY referenced and can be garbage-collected mid-run (the
#: #526 footgun) — the repo idiom is a module-level set plus a done-callback
#: (``brain_orb_postprocess.py``, ``activity_service.py``).
_adoption_tasks: set = set()


def _sweep_lock() -> asyncio.Lock:
    """The process-wide sweep mutex, created lazily ON THE RUNNING LOOP.

    Same reason as ``subscription_auto_switch.agent_switch_lock``: an
    ``asyncio.Lock`` binds to the loop that first awaits it, so one built at
    import time and reused raises "bound to a different event loop" under
    pytest's per-test loops.
    """
    global _SWEEP_LOCK
    if _SWEEP_LOCK is None:
        _SWEEP_LOCK = asyncio.Lock()
    return _SWEEP_LOCK


def _reset_sweep_lock_for_test() -> None:
    """Test hook: drop the sweep lock so each test's event loop starts clean
    (mirrors ``subscription_auto_switch._reset_locks_for_test``)."""
    global _SWEEP_LOCK
    _SWEEP_LOCK = None


def instance_has_api_key() -> bool:
    """True when the platform itself can authenticate an ``api_key``-mode agent.

    MUST stay ``get_anthropic_api_key()`` — which resolves the encrypted setting
    row, then the legacy cleartext row, then ``os.getenv('ANTHROPIC_API_KEY')``.
    It must NEVER become ``has_secret_setting()``, which is DB-only and
    presence-only: on the key-deletion trigger that swap would adopt a whole
    fleet off a still-working ``.env``/compose key and commit exactly the sin the
    issue forbids. ``delete_anthropic_key``'s own ``fallback_configured`` field
    exists because that env fallback is common.

    SYNCHRONOUS — one uncached SQLite read (deliberately uncached, for
    ``--workers 2`` consistency). Callers on an async path go through
    ``asyncio.to_thread``.
    """
    from services.settings_service import get_anthropic_api_key

    return bool((get_anthropic_api_key() or "").strip())


def credentialless_agent_names() -> List[str]:
    """Agents whose ACTIVE auth mode resolves to no usable credential.

    Pure DB, ONE query, no Docker — and ``[]`` the moment the instance has a
    platform key, which short-circuits the whole sweep on any normal install.

    Conditions (the issue's own predicate — "no API key configured at instance
    level and none set for the agent"):
      1. the platform resolves no Anthropic key (above);
      2. ``subscription_id IS NULL`` — an agent already on a subscription is
         never moved (AC bullet 3);
      3. ``use_platform_api_key IS TRUE``. ``False`` is the operator asserting
         through ``PUT /api/agents/{name}/api-key-setting`` that this agent
         brings its OWN credential (a ``.env`` key, CRED-002) — which the
         backend structurally cannot see, so "none set for the agent" is
         unverifiable there, and adopting would make the agent-side
         ``arm_subscription_auth_guard()`` (#2114) force-unset the very key that
         was working. That is the forbidden "moved an agent with a working key",
         in its per-agent flavour. The column defaults to 1, so the reported
         scenario is fully covered.

    Runtime (Claude-only, #1187 decision 7) and ephemerality are conditions 4
    and 5; they need Docker / a second read and are applied by the sweep.

    SYNCHRONOUS — call via ``asyncio.to_thread``.
    """
    if instance_has_api_key():
        return []
    return sorted(
        name
        for name, m in db.get_agent_subscription_map().items()
        if m["subscription_id"] is None and m["use_platform_api_key"]
    )


def _partition_ephemeral(names: List[str]) -> Tuple[List[str], List[str]]:
    """Split ``names`` into (durable, ephemeral). SYNCHRONOUS — via to_thread.

    Ephemeral "ghosts" are excluded from the sweep ENTIRELY, not merely from the
    restart phase. They are volume-less by invariant (``lifecycle`` relies on
    "ghosts never recreate" as a load-bearing assumption), and while the
    image-drift recreate predicate exempts them explicitly, the AUTH predicate
    (``check_api_key_env_matches``) does not — so an adopted ghost driven through
    ``start_agent_internal`` would be recreated and its workspace destroyed
    mid-budget (trinity-enterprise#69). Adopting one buys nothing either: they
    are short-lived, and #74 already covers ghosts created after a subscription
    exists.
    """
    durable: List[str] = []
    ghosts: List[str] = []
    for name in names:
        try:
            info = db.get_agent_ephemeral_info(name)
        except Exception as e:  # noqa: BLE001 — one unreadable row must not abort the sweep
            logger.warning(
                "[#2572] could not read ephemeral info for agent '%s' (%s); "
                "treating it as ephemeral and skipping it", name, e,
            )
            ghosts.append(name)
            continue
        if info and info.get("is_ephemeral"):
            ghosts.append(name)
        else:
            durable.append(name)
    return durable, ghosts


def _decide_and_assign(
    agent_name: str, preselected: Optional[SubscriptionCredential]
) -> Tuple[str, Optional[SubscriptionCredential]]:
    """Re-check, select and persist for ONE agent. SYNCHRONOUS — via to_thread,
    and always called while holding that agent's #799 switch lock.

    The ``subscription_id IS NULL`` re-read inside the lock is what makes the
    sweep idempotent (a re-registration upsert, or two admins) and what keeps it
    from stealing an agent a concurrent SUB-003 switch just placed. Note this
    skip is a NEW rule rather than a reuse: the manual assign path reads
    ``old_sub_id`` under the lock too, but assigns unconditionally — it uses the
    value only to choose hot-reload vs recreate.

    Returns ``("already_assigned" | "no_subscription" | "assigned", sub)``.
    """
    if db.get_agent_subscription_id(agent_name) is not None:
        return "already_assigned", None
    sub = preselected or select_subscription_for_new_agent()
    if sub is None:
        return "no_subscription", None
    db.assign_subscription_to_agent(agent_name, sub.id)
    return "assigned", sub


async def adopt_for_credentialless_agents(
    *,
    actor_user: Any = None,
    actor_ip: Optional[str] = None,
    endpoint: Optional[str] = None,
    request_id: Optional[str] = None,
    trigger: str = TRIGGER_SUBSCRIPTION_REGISTERED,
) -> Dict[str, str]:
    """Assign an available subscription to every agent whose active auth mode
    resolves to no usable credential (#2572), then spawn the container apply.

    Returns ``{agent_name: subscription_name}`` for the DB phase.

    TWO PHASES, and the split is load-bearing:

    * **Phase A — decide + persist, AWAITED inside the request** (but every
      blocking call off the event loop). This is what makes
      ``GET /api/subscriptions`` correct by the time the triggering request
      returns, which it must be: the Settings panel refetches that endpoint on
      the very next line after registering. Backgrounding Phase A would race
      that refetch and show ``agent_count: 0``.
    * **Phase B — apply to running containers, in the BACKGROUND.** Registration
      (or a key deletion) must not block on N container recreates.

    Credential-less → subscription is an auth-MODE change, so the apply is
    ``_restart_agent``, never ``_hot_reload_subscription_token`` (whose docstring
    carries the invariant that all its producers are sub→sub by construction).

    Fail-open per agent — one agent's failure never aborts the sweep — and the
    sweep never fails the request that triggered it (the callers wrap it too).
    """
    lock = _sweep_lock()
    if lock.locked():
        logger.info(
            "[#2572] adoption sweep already in progress (trigger=%s); skipping "
            "this one rather than queueing it", trigger,
        )
        return {}

    async with lock:
        started = time.monotonic()

        names = await asyncio.to_thread(credentialless_agent_names)
        if not names:
            return {}

        # Condition 4, in ONE Docker round trip off the event loop. Never
        # `get_agent_runtime()` (N blocking inspects, and it defaults to
        # "claude-code" on every failure) and never a per-agent
        # `get_agent_container()` loop.
        # Function-local imports throughout: the repo idiom for this module (it
        # keeps the no-cycle property and this module's call-time stub-ability,
        # which the agent-creation harnesses depend on).
        from services.agent_service.helpers import is_claude_runtime
        from services.docker_utils import agent_container_runtime_labels_async

        skipped = {"no_container": 0, "non_claude": 0, "ephemeral": 0,
                   "already_assigned": 0, "docker_unreadable": 0}

        labels = await agent_container_runtime_labels_async()
        if labels is None:
            # Tri-state: Docker could not be ASKED. Fail CLOSED — deliberately
            # the opposite resolution to `agent_container_runtimes`' documented
            # fail-open, because that call site decides a UI affordance while
            # this one writes a persisted credential assignment: guessing wrong
            # hands every Gemini/Codex agent a subscription in one shot.
            logger.error(
                "[#2572] adoption sweep aborted (trigger=%s): Docker could not be "
                "asked for container runtimes, so %d credential-less agent(s) "
                "cannot be runtime-verified — adopted nobody",
                trigger, len(names),
            )
            # The summary row exists FOR this case: "adopted 0 of 40 because
            # Docker was unreadable" must be distinguishable in the record from
            # "nothing to do", and `agent_count` staying 0 is the operator's
            # only other signal. An ERROR log alone leaves the audit trail
            # saying nothing happened, which is the wrong answer.
            skipped["docker_unreadable"] = len(names)
            await _log_adoption_sweep(
                trigger=trigger, adopted={}, assigned_ids={},
                candidates=len(names), skipped=skipped,
                actor_user=actor_user, actor_ip=actor_ip,
                endpoint=endpoint, request_id=request_id,
            )
            return {}

        claude_names: List[str] = []
        for name in names:
            if name not in labels:
                # Docker ANSWERED and this agent has no container. Skipped, and
                # (no periodic sweep) no later trigger reaches it until it has a
                # container again — accepted: it is not running, so it is not
                # the live pain this issue reports, and the subscription
                # switcher on the agent header is a one-click recovery.
                skipped["no_container"] += 1
                continue
            raw = labels.get(name)
            if not raw or not is_claude_runtime(raw):
                # LABEL-STRICT: an absent label is not evidence of a Claude
                # runtime. `is_claude_runtime(None)` is True by design (the
                # unset default) and `trinity-system` carries no runtime label
                # at all, so trusting the default here would adopt agents on no
                # evidence.
                skipped["non_claude"] += 1
                continue
            claude_names.append(name)

        candidates, ghosts = await asyncio.to_thread(_partition_ephemeral, claude_names)
        skipped["ephemeral"] = len(ghosts)
        if not candidates:
            logger.info(
                "[#2572] adoption sweep (trigger=%s): %d credential-less agent(s), "
                "none eligible; skipped %s", trigger, len(names), skipped,
            )
            return {}

        assignable = await asyncio.to_thread(db.list_assignable_subscriptions)
        if not assignable:
            logger.warning(
                "[#2572] adoption sweep (trigger=%s): %d credential-less agent(s) "
                "but no assignable subscription — adopted nobody",
                trigger, len(candidates),
            )
            return {}

        # Single-candidate short-circuit — the dominant case (a first
        # registration). Resolve the ranker ONCE instead of once per agent. With
        # two or more, call it per agent so the `agent_count ASC` load-balance
        # tiebreak spreads adoption the way create-time assignment does.
        preselected: Optional[SubscriptionCredential] = None
        if len(assignable) == 1:
            preselected = await asyncio.to_thread(select_subscription_for_new_agent)
            if preselected is None:
                logger.warning(
                    "[#2572] adoption sweep (trigger=%s): the only assignable "
                    "subscription has no usable token — adopted nobody", trigger,
                )
                return {}

        from services.subscription_auto_switch import agent_switch_lock

        adopted: Dict[str, str] = {}
        assigned_ids: Dict[str, str] = {}
        for name in candidates:
            try:
                # #799: the SAME per-agent mutex SUB-003 and the manual assign
                # path take, so the sweep cannot interleave with either.
                async with await agent_switch_lock(name):
                    status, sub = await asyncio.to_thread(
                        _decide_and_assign, name, preselected
                    )
                    if status == "already_assigned":
                        skipped["already_assigned"] += 1
                        continue
                    if status == "no_subscription":
                        logger.warning(
                            "[#2572] adoption sweep (trigger=%s): nothing assignable "
                            "left after %d adoption(s); stopping",
                            trigger, len(adopted),
                        )
                        break
                    adopted[name] = sub.name
                    assigned_ids[name] = sub.id
                    await _log_adoption(
                        agent_name=name, sub=sub, trigger=trigger,
                        actor_user=actor_user, actor_ip=actor_ip,
                        endpoint=endpoint, request_id=request_id,
                    )
            except Exception as e:  # noqa: BLE001 — one agent must not abort the sweep
                logger.error(
                    "[#2572] adoption failed for agent '%s' (trigger=%s): %s",
                    name, trigger, e,
                )
                continue

        elapsed = time.monotonic() - started
        summary = (
            f"[#2572] adoption sweep (trigger={trigger}): adopted "
            f"{len(adopted)} of {len(names)} credential-less agent(s)"
            + (f": {', '.join(sorted(adopted))}" if adopted else "")
            + f"; skipped {skipped}"
        )
        if len(adopted) > ADOPTION_SWEEP_WARN:
            logger.warning("%s; took %.1fs", summary, elapsed)
        else:
            logger.info(summary)

        await _log_adoption_sweep(
            trigger=trigger, adopted=adopted, assigned_ids=assigned_ids,
            candidates=len(names), skipped=skipped,
            actor_user=actor_user, actor_ip=actor_ip,
            endpoint=endpoint, request_id=request_id,
        )

        # Phase B — never awaited here.
        if assigned_ids:
            _spawn_apply_adoptions(assigned_ids)

        return adopted


async def _log_adoption(
    *, agent_name: str, sub: SubscriptionCredential, trigger: str,
    actor_user: Any, actor_ip: Optional[str], endpoint: Optional[str],
    request_id: Optional[str],
) -> None:
    """One ``audit_log`` row per adopted agent (AC bullet 5).

    ``details`` carries the subscription ID and NAME only — never a token
    (Invariant #12). ``log()`` is best-effort by contract and its return value
    is deliberately not branched on.
    """
    from services.platform_audit_service import AuditEventType, platform_audit_service

    await platform_audit_service.log(
        event_type=AuditEventType.CREDENTIALS,
        event_action="subscription_auto_adopt",
        source="api",
        actor_user=actor_user,
        actor_ip=actor_ip,
        target_type="agent",
        target_id=agent_name,
        endpoint=endpoint,
        request_id=request_id,
        details={
            "subscription_id": sub.id,
            "subscription_name": sub.name,
            "trigger": trigger,
            "reason": "no_usable_credential",
            "previous_auth_mode": "api_key",
        },
    )


async def _log_adoption_sweep(
    *, trigger: str, adopted: Dict[str, str], assigned_ids: Dict[str, str],
    candidates: int, skipped: Dict[str, int], actor_user: Any,
    actor_ip: Optional[str], endpoint: Optional[str], request_id: Optional[str],
) -> None:
    """One sweep-summary row. Without it, a sweep that adopted 0 of 40 because
    Docker was unreadable is indistinguishable in the record from a sweep with
    nothing to do — and the operator's only other signal is ``agent_count``
    staying 0.
    """
    from services.platform_audit_service import AuditEventType, platform_audit_service

    subscription_ids = sorted(set(assigned_ids.values()))
    await platform_audit_service.log(
        event_type=AuditEventType.CREDENTIALS,
        event_action="subscription_auto_adopt_sweep",
        source="api",
        actor_user=actor_user,
        actor_ip=actor_ip,
        target_type="subscription",
        # One id when the whole sweep landed on one subscription (the dominant
        # case); the full set always rides in `details`.
        target_id=subscription_ids[0] if len(subscription_ids) == 1 else None,
        endpoint=endpoint,
        request_id=request_id,
        details={
            "trigger": trigger,
            "adopted": len(adopted),
            "candidates": candidates,
            "skipped": skipped,
            "agents": sorted(adopted),
            "subscription_ids": subscription_ids,
        },
    )


def _spawn_apply_adoptions(assigned: Dict[str, str]) -> None:
    """Fire Phase B off the request path, holding a STRONG reference to it."""
    task = asyncio.create_task(_apply_adoptions(assigned))
    _adoption_tasks.add(task)
    task.add_done_callback(_adoption_tasks.discard)


async def _apply_adoptions(assigned: Dict[str, str]) -> Dict[str, str]:
    """Phase B — make the running containers match the DB (#2572).

    Takes ``{agent_name: subscription_id}``, not a bare name list, because the
    assignment is RE-VERIFIED under the per-agent lock before any restart:
    Phase A has itself just created SUB-003 eligibility (its precondition 2 is
    "agent has a subscription assigned"), so between the phases a failing turn
    can legitimately auto-switch the agent onto a working credential and start a
    real turn on it. Restarting blind would kill that turn — and would falsify
    Phase A's "these agents had no credential, so nothing is lost", which is true
    at A and FALSE at B.

    Why this phase is load-bearing at all rather than mere convenience: a
    freshly-assigned agent fails ``check_api_key_env_matches``, so
    ``start_agent_internal`` re-bakes its auth block from the DB on its next
    start — but nothing on the dispatch path calls ``start_agent_internal``, so
    a RUNNING agent has no "next start", and the operator declined a periodic
    healer. Without this phase the badge would say "subscription" while the agent
    still answered "Not logged in", which is harder to diagnose than the bug.
    """
    from services.agent_service.helpers import is_system_agent_name
    from services.subscription_auto_switch import _restart_agent, agent_switch_lock

    results: Dict[str, str] = {}
    try:
        for name, subscription_id in assigned.items():
            try:
                if is_system_agent_name(name):
                    # `agent-trinity-system` is adopted in the DB (so the badge,
                    # `GET /api/subscriptions` and its next deliberate restart
                    # are all correct) but NEVER restarted from here.
                    # `_restart_agent` stops the container first, so
                    # `was_already_running` is False by the time
                    # `start_agent_internal` evaluates it and the #1816 guard —
                    # whose entire purpose is "a running trinity-system is never
                    # recreated mid-operation" — is bypassed by construction.
                    # An unattended fan-out triggered by a request on a
                    # different resource must not join the list of callers
                    # permitted to stop it first.
                    results[name] = "skipped_system_agent"
                    logger.info(
                        "[#2572] adopted '%s' in the DB but not restarting it "
                        "(#1816: the platform orchestrator is restarted only by "
                        "a deliberate operator action)", name,
                    )
                    continue

                async with await agent_switch_lock(name):
                    current = await asyncio.to_thread(db.get_agent_subscription_id, name)
                    if current != subscription_id:
                        results[name] = "skipped_reassigned"
                        logger.info(
                            "[#2572] agent '%s' moved to another subscription "
                            "between the decide and apply phases; not restarting",
                            name,
                        )
                        continue
                    result = await _restart_agent(name)

                results[name] = result
                if result not in ("success", "not_running", "no_container"):
                    logger.warning(
                        "[#2572] adopted agent '%s' was not applied to its "
                        "container: %s — it will pick the token up on its next "
                        "start", name, result,
                    )
            except Exception as e:  # noqa: BLE001 — one agent must not abort the fan-out
                results[name] = f"failed: {e}"
                logger.warning(
                    "[#2572] failed to apply the adopted subscription to agent "
                    "'%s': %s", name, e,
                )
    finally:
        # `try/finally` so a cancellation (a uvicorn reload or shutdown during a
        # long fan-out) is visible rather than silent; the per-agent lock bounds
        # the damage to one agent.
        logger.info("[#2572] adoption apply phase finished: %s", results)
    return results
