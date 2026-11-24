"""Tell the caller when a run it delegated has ended (ent#1102).

An agent that calls ``chat_with_agent`` with ``async=true`` gets a receipt, not
an answer. Before this it had no way to learn the answer had arrived, so it
polled — the retrospective behind ruling D4 measured that: $246 of polling, 13
re-dispatches, 10 operator nudges, for runs that had already finished. The fix
is to push once instead of being polled many times.

**Where this hangs, and why.** ``event_dispatch_service._terminal_side_effects``
is the one fan-out every CAS-won terminal writer already calls — 16 sites — so
one guarded sibling there covers task, chat, pull, fan-out and all three cleanup
paths without any of them changing. It is deliberately NOT nested inside
``emit_task_terminal_event``: that returns early when no event subscription
matches, which is the common case, so anything inside it would almost never run.
And it is deliberately not armed by the MCP client at receipt time — the
terminal knows the run ended; the client does not.

**What it will not do.** The wake spends a billed turn on an agent that did not
schedule it, so every one of these is a hard stand-down:

* no ``source_agent_name`` — a human's run has no delegating agent to tell;
* the caller IS the callee (a self-task), or the row is a ``fan_out`` leg (the
  #2524 join is the batch's own reaction);
* the row's own trigger is one of the four reserved platform triggers. A wake's
  end never wakes anyone, so no chain here can sustain itself; and a platform
  turn's end — an answered ask, an ended ask, an event dispatch — is not a
  delegation's end;
* ``wake_policy.resolve`` says no (per-agent tri-state → instance default), or
  could not be read at all;
* the caller already holds a matching ``agent.task.*`` subscription on the
  callee, which the emit dispatches on — exactly one turn per ending (AC 6);
* the caller's container is stopped or missing: nothing auto-starts an agent, so
  that wake would be a FAILED execution and nothing else. Audited as
  ``skipped_not_running``; the ending stays readable through
  ``get_execution_result`` when the caller next runs.

**The storm bound: one wake per caller at a time (#4733).** Every wake — a
first ending's and a parked one's — claims the caller's one hold,
``caller_wake:{caller}:running``, NOT a key per callee execution: a cleanup
sweep can terminate hundreds of stale rows in one cycle, and keying per callee
would bill one turn for each. The hold was a 15-second window key until #4733;
a wake outlived its window, and an ending in a later one started a second wake
beside it. The frame still says plainly that other runs may have ended around
this one, so the caller reads ``list_recent_executions`` rather than being told
a half-truth.

**The hold is a DB fact, never a stuck key.** The caller is held while a
``caller_wake`` row of it is queued, running or pending a retry, or while a
claim is younger than ``HOLD_GRACE_SECONDS`` — the gap between the claim and
that row's insert. A claim older than that with no live row is reclaimed by
its owner token. That is what frees the caller after a clean backend stop,
which cancels the holder and writes the wake row FAILED without a terminal
fan-out. The holder releases the claim when its dispatch returns, and the wake
row's own terminal releases it too, but only a claim taken at or before that
row's admission (``queued_at``, else ``started_at``; both re-stamp rules keep
``queued_at``) — never a later wake's.

**A collision is parked, never dropped (#4536).** An ending that ended at or
before the running wake's claim, and reached here within ``FOLD_SKEW_SECONDS``
of ending, folds into that wake — its ``list_recent_executions`` shows it. Any
other ending while the caller is held is parked in Redis
(``trinity:wake_missed:{caller}``, latest wins, audited ``parked``) and
delivered once when the wake ends: from the wake row's own terminal and from
the holder after it returns or raises an ``Exception``. A holder cancelled or
closed with its loop leaves the marker. Bound: one running wake plus one
pending per caller. Redis trouble on the park is audited ``skipped_window``.

**At capacity the wake is parked, not a failed row (#4733).** Before the wake
row exists, the caller's slots are read: full, and one of its runs is running,
means admission would refuse, so the ending is parked (``parked``,
``reason="at_capacity"``) and delivered when any run of the caller ends: every
terminal fan-out reaches this hook (a person's run and a platform turn
included), and a push ``/chat``, which writes its terminal without one, drains
it where its slot is released (``drain_after_slot_release``). A full meter with
no running run is a slot no run holds (expired, or leaked until it is
reclaimed), so that dispatches and admission decides. A pilot is never refused
for a slot (its wake queues), so it is not checked. A refusal that races the
check is the one FAILED row left; its ending is parked too, except on a pilot,
where a refusal means a full backlog.

**The wake stands where the delegator stood (#4357).** A wake continues the
delegator's own work, so it carries the delegator's standing, not one more hop:
``max(callee − 1, MAX(running caller), 0)``, NULL counted as 0. The callee row
was stamped ``1 + MAX(running caller)`` at dispatch, so ``callee − 1`` is the
depth the delegator stood at when it delegated; the live MAX keeps a wake from
standing below a deep turn of the caller that is still running (a folded
ending can be the shallower one). It was ``callee + 1``, and the wake turn's
next delegation is ``1 + MAX(running)`` over a set that includes the wake row, so
every delegate→wake round added two and an orchestrator working through wakes
was refused after about four rounds. A wake is therefore at most ``max − 1``
unless a turn of the delegator that is still running stands deeper, and it can
make one hop whenever the delegator's own turns allow one; a genuine chain —
hops from turns still running — is still counted and refused by the guard
exactly as before.

**The wake budget.** Depth therefore no longer bounds how many wakes follow one
another, so a per-pair hourly budget does: ``caller_wake_max_per_hour`` (default
120, ``0`` or less = unlimited) wakes from one callee into one delegator per
one-hour Redis window, the #2973 event-budget shape. It is spent only by a wake
about to be dispatched — an ending folded or parked spends nothing. A wake past it is
skipped, audited ``skipped_budget``, and the delegator gets one notification
per window; the ending stays readable through ``get_execution_result``. Redis
trouble fails open: the budget must not cost an agent the wake it was promised.

**Whose turn the wake is (trinity-enterprise#1216).** The wake carried nobody,
so ``turn_tier_service.turn_actor`` rated it a visitor's turn, and the ledger
rule (``dependencies.acting_turn_user_id``, ent#926) then hid every execution
row from the delegator's own key for as long as the wake ran — the wake turn
could not read the result it was woken for. The callee row records who the
delegation was for (``turn_origin_user_id`` at dispatch), so the wake carries
that person when they are owner tier on the delegator, and nobody otherwise:

* the owner's own work (the agent's owner, an admin, a provider-rated member)
  wakes as an owner turn and reads at the full view;
* a person the delegator is only shared with, a person who cannot open it at
  all (a hub's member, whose delegating turn ran ``delegated``), and a visitor
  wake as the no-person turn they always were. No column records the
  delegating turn, so its exact standing cannot be rebuilt here, and a
  non-owner's wake is left unchanged rather than re-rated on a guess.

``source_user_email`` stays off the row either way (ent#886).

**The wake stands as the turn that delegated (trinity-enterprise#1225).** The
callee row records the caller's turn that dispatched it
(``continues_execution_id``, written only while that turn ran). The wake
carries that link and nobody on the row, and ``turn_tier_service.turn_actor``
rates it exactly as the delegating turn: a shared person's delegation wakes as
that person (reads narrowed to them, no shell), a ``delegated`` turn's keeps
owner tier, a visitor's stays a visitor's. The person rule above is the
fallback for a callee row with no link (one written before the column, or by
a key that named no running turn).

Never raises: this runs on the path of an already-billed terminal.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, List, Optional, Tuple

from utils.credential_sanitizer import sanitize_text

logger = logging.getLogger(__name__)

TRIGGERED_BY_CALLER_WAKE = "caller_wake"

# A platform turn's end is not a delegation's end. ``caller_wake`` itself is
# first in this set: that is the recursion break, and it is why a wake chain
# cannot be self-sustaining no matter how the agents are wired.
RESERVED_TRIGGERS = frozenset({
    TRIGGERED_BY_CALLER_WAKE,
    "event",              # event_dispatch_service.RESERVED_EVENT_TRIGGER
    "operator_response",  # operator_resume_service.TRIGGERED_BY
    "operator_ending",    # operator_resume_service.TRIGGERED_BY_ENDING
    "project_steward",    # trinity-enterprise#1236: the platform's project steward wake
})

# #4733: one wake per caller at a time. A claim with no live wake row behind it
# holds the caller for this long — the gap between the claim and the wake row's
# insert, which is a few awaits.
HOLD_GRACE_SECONDS = 120

# An ending folds into the running wake only when it ended by that wake's claim
# AND reached the hook this soon after ending: a row written late with an
# earlier `completed_at` (a retained result, the agent's own finish instant)
# was listed as running, so it is parked instead. 15 s is the old window.
FOLD_SKEW_SECONDS = 15


# #4357: the per-pair hourly wake budget (the #2973 event-budget shape).
_WAKE_BUDGET_SETTING = "caller_wake_max_per_hour"
_WAKE_WINDOW_SECONDS = 3600
_WAKE_KEY_PREFIX = "trinity:wake_fires:"

# The same cap and chokepoint the #1578 event payload uses, for the same reason.
SUMMARY_MAX = 2000
ASK_IDS_MAX = 10


# #4536/#4733: an ending that arrives while a wake of its caller runs, or while
# the caller has no free slot, is parked here (latest wins) and delivered when
# that wake or a run of the caller ends. 24 h, the idempotency TTL, so no wake
# turn outlives its marker.
_MISSED_KEY_PREFIX = "trinity:wake_missed:"
_MISSED_TTL_SECONDS = 86400

# Strong references to follow-on wakes until they finish.
_FOLLOW_ONS: set = set()

# `_claimed_wake`'s answer when admission refused the wake for a slot and the
# ending was parked: no wake ran, and the holder must not drain it straight back.
_PARKED_AT_CAPACITY = object()


def _hold_key(caller: str) -> str:
    """#4733: the caller's one hold. Never `attach_execution` to it — that
    moves `updated_at`, which every reader here takes as the claim instant."""
    return f"caller_wake:{caller}:running"


# --- seams (each one its own function so a test can stub it) ---------------

def _read_row(execution_id: str):
    from database import db

    return db.get_execution(execution_id)


def _resolve_policy(agent_name: str):
    from services.wake_policy import resolve

    return resolve(agent_name)


def _caller_subscribes_to(caller: str, callee: str, terminal_status: Any) -> bool:
    """True when the emit will already dispatch a turn on the caller.

    Shares the read the emit does (``find_matching_event_subscriptions``), so
    the two cannot disagree about who is subscribed. Fails OPEN — if the read
    breaks we would rather risk the second turn than lose the wake, because the
    duplicate is visible and the silence is not.
    """
    from database import db
    from services.event_dispatch_service import _event_type_for_status

    try:
        event_type = _event_type_for_status(terminal_status)
        subs = db.find_matching_event_subscriptions(callee, event_type) or []
    except Exception:  # noqa: BLE001
        return False
    for sub in subs:
        target = sub.get("agent_name") if isinstance(sub, dict) else getattr(sub, "agent_name", None)
        if target == caller:
            return True
    return False


async def _container_state(agent_name: str) -> Optional[str]:
    """``running`` / ``stopped`` / ``missing`` / ``None`` when Docker was unaskable.

    Read off the event loop (#2196): the Docker SDK call blocks, and on the loop
    it would stall every request on this worker.
    """
    from services.docker_utils import agent_container_state_async

    return await agent_container_state_async(agent_name)


def _pending_ask_ids(execution_id: str) -> List[str]:
    from database import db

    try:
        return list(db.list_pending_ask_ids_for_execution(execution_id, ASK_IDS_MAX) or [])
    except Exception:  # noqa: BLE001 — an enrichment, never the reason to skip
        logger.debug("ent#1102: pending-ask read failed for %s", execution_id)
        return []


def _begin_idempotency(scope: str, key: str):
    from services import idempotency_service

    return idempotency_service.begin(scope, key)


def _fail_idempotency(decision) -> None:
    from services import idempotency_service

    idempotency_service.fail(decision)


def _agent_scope(agent_name: str) -> str:
    from services import idempotency_service

    return idempotency_service.make_agent_scope(agent_name)


def _peek_idempotency(scope: str, key: str):
    from services import idempotency_service

    return idempotency_service.peek(scope, key)


def _reclaim_idempotency(decision) -> bool:
    from services import idempotency_service

    return bool(idempotency_service.reclaim_in_flight(decision))


def _wake_row_live(caller: str) -> bool:
    """A wake row of ``caller`` is still queued, running or pending a retry."""
    from database import db
    from models import TaskExecutionStatus as S

    return db.has_execution_with_status(
        caller, (S.QUEUED, S.RUNNING, S.PENDING_RETRY), TRIGGERED_BY_CALLER_WAKE)


def _caller_running(caller: str) -> bool:
    from database import db
    from models import TaskExecutionStatus as S

    return db.has_execution_with_status(caller, (S.RUNNING,))


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _has_free_slot(caller: str) -> bool:
    """Would admission take a wake of ``caller`` now? (#4733, the capacity ruling)

    No only when the slot meter is full AND one of the caller's runs is
    running: the meter still counts a slot whose TTL ran out until admission
    cleans it, and without a running run there is also no run whose end would
    deliver a parked ending. A pilot queues its wake and is never refused for a
    slot, so it is not checked. Fails open: an unreadable meter dispatches, and
    admission decides, as before.
    """
    try:
        from services.pull_pilot import pull_owns_dispatch

        if pull_owns_dispatch(caller, TRIGGERED_BY_CALLER_WAKE):
            return True
        from database import db
        from services.capacity_manager import get_capacity_manager

        max_tasks = await asyncio.to_thread(db.get_max_parallel_tasks, caller)
        state = await get_capacity_manager().get_slot_state(caller, max_tasks)
        if int(getattr(state, "available_slots", 1)) > 0:
            return True
        return not await asyncio.to_thread(_caller_running, caller)
    except Exception as e:  # noqa: BLE001 — admission still guards the slot
        logger.debug("#4733: slot check for %s failed (%s); dispatching", caller, type(e).__name__)
        return True


def _running_depth(agent_name: str) -> int:
    from database import db

    return db.get_max_running_chain_depth(agent_name)


def _standing_depth(caller: str, callee_depth: Any) -> int:
    """The delegator's standing: ``max(callee − 1, MAX(running caller), 0)`` (#4357).

    NULL (a root callee) counts as 0. A depth that is not an integer leaves the
    live MAX as the floor — the row was read, so the caller's running turns are
    the honest measure, never 0 under them. A MAX that cannot be read leaves
    ``callee − 1`` alone: the wake is still owed, and this never raises.
    """
    try:
        from_callee = int(callee_depth or 0) - 1
    except (TypeError, ValueError):
        from_callee = 0
    try:
        running = int(_running_depth(caller) or 0)
    except Exception:  # noqa: BLE001 — never raise on the billed terminal path
        logger.debug("#4357: running depth unreadable for %s", caller)
        running = 0
    return max(from_callee, running, 0)


def _standing_person(caller: str, row) -> Optional[int]:
    """The person the wake carries (ent#1216): the callee row's, and only when
    that person is owner tier on ``caller``; None for everyone else.

    Rated with the real ``turn_actor`` over the row the wake will be — the
    delegator, that person, no source agent — so the delegation rule cannot
    lift it and the member provider counts. Anything that cannot be read
    carries nobody: the wake is still owed, and this never guesses upward.
    """
    person = getattr(row, "source_user_id", None)
    if person is None:
        return None
    try:
        from database import db
        from services import turn_tier_service

        tier = turn_tier_service.turn_actor(
            db, caller, execution={"agent_name": caller, "source_user_id": person}
        )[0]
    except Exception:  # noqa: BLE001 — never raise on the billed terminal path
        logger.debug("ent#1216: standing unreadable for %s", caller)
        return None
    return person if tier == turn_tier_service.OWNER else None


def _wake_budget_limit() -> int:
    """The configured cap; ``0`` or less is unlimited. The code default on any read error."""
    from config import OPS_SETTINGS_DEFAULTS, OPS_SETTINGS_VALIDATION
    from services.settings_service import settings_service

    default = int(OPS_SETTINGS_DEFAULTS[_WAKE_BUDGET_SETTING])
    _kind, _low, high = OPS_SETTINGS_VALIDATION[_WAKE_BUDGET_SETTING]
    try:
        value = settings_service.get_ops_setting(_WAKE_BUDGET_SETTING, int)
    except Exception as e:  # noqa: BLE001 — a bad setting must not cost the wake
        logger.warning("[#4357] %s unreadable (%s); using %d", _WAKE_BUDGET_SETTING, e, default)
        return default
    return min(high, value)


def _wake_budget_redis():
    """Separate function so tests have one obvious patch point."""
    from routers.auth import get_redis_client
    return get_redis_client()


async def _within_wake_budget(callee: str, caller: str) -> Optional[int]:
    """Spend one ``callee`` → ``caller`` wake; the cap once the hour's is spent, else None.

    INCR and the TTL go in one transaction (``EXPIRE NX`` never extends a live
    window): split, a crash between them leaves a TTL-less key that stops the
    pair's wakes for good. Fail-OPEN on Redis trouble.
    """
    limit = _wake_budget_limit()
    if limit <= 0:
        return None
    key = f"{_WAKE_KEY_PREFIX}{callee}:{caller}"
    try:
        client = _wake_budget_redis()
        if client is None:
            logger.warning("[#4357] Redis unavailable; caller-wake budget not enforced")
            return None
        pipe = client.pipeline(transaction=True)
        pipe.incr(key)
        pipe.expire(key, _WAKE_WINDOW_SECONDS, nx=True)
        count, _ = pipe.execute()
    except Exception as e:  # noqa: BLE001
        logger.warning("[#4357] caller-wake budget check failed (%s); allowing", e)
        return None

    if count <= limit:
        return None
    logger.warning(
        "[#4357] wake %s -> %s skipped: %d this hour > cap %d", callee, caller, count, limit,
    )
    # One alert per window. A flag (not `count == limit + 1`) so a cap lowered
    # mid-window below the running count still alerts once.
    try:
        first_skip = client.set(f"{key}:alerted", 1, nx=True, ex=_WAKE_WINDOW_SECONDS)
    except Exception:  # noqa: BLE001 — alerting is best-effort
        first_skip = False
    if first_skip:
        try:
            from services.monitoring_alerts import get_alert_service
            await get_alert_service().alert_caller_wake_budget_exhausted(caller, callee, limit)
        except Exception:  # noqa: BLE001 — the skip stands whether or not the alert lands
            logger.exception("[#4357] could not raise the wake-budget alert for %s -> %s",
                             callee, caller)
    return limit


def _spawn_followon(coro) -> None:
    """Run a follow-on wake as its own task, never nested inside the drain."""
    import asyncio

    task = asyncio.get_running_loop().create_task(coro)
    _FOLLOW_ONS.add(task)
    task.add_done_callback(_FOLLOW_ONS.discard)


def _as_utc(value):
    from datetime import datetime, timezone

    from utils.helpers import parse_iso_timestamp

    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        return parse_iso_timestamp(str(value))
    except Exception:  # noqa: BLE001 — unreadable counts as "not covered"
        return None


def _ended_by(row, claimed_at) -> bool:
    """This run ended at or before ``claimed_at``, so a wake claimed then shows
    it in ``list_recent_executions``. Unknown either way: not covered."""
    ended = _as_utc(getattr(row, "completed_at", None))
    claimed = _as_utc(claimed_at)
    return ended is not None and claimed is not None and ended <= claimed


def _covered(row, idem) -> bool:
    """Folds into the wake holding ``idem`` (#4536): ended by its claim, and
    reached here promptly — a late write back-dated before the claim was listed
    as running, so it is not covered (#4733)."""
    if not _ended_by(row, getattr(idem, "claimed_at", None)):
        return False
    ended = _as_utc(getattr(row, "completed_at", None))
    return (_now() - ended).total_seconds() <= FOLD_SKEW_SECONDS


# --- the caller's hold (#4733) ---------------------------------------------

def _young(hold) -> bool:
    """A claim inside the gap before its wake row exists. Unknown age: young."""
    claimed = _as_utc(getattr(hold, "claimed_at", None))
    return claimed is None or (_now() - claimed).total_seconds() < HOLD_GRACE_SECONDS


def _wake_live(caller: str) -> bool:
    """A wake row of ``caller`` is queued, running or pending a retry. An
    unreadable answer holds the caller: a parked ending waits, two wakes never run."""
    try:
        return bool(_wake_row_live(caller))
    except Exception:  # noqa: BLE001
        logger.warning("#4733: wake rows of %s unreadable; holding", caller)
        return True


def _caller_busy(caller: str) -> bool:
    """Is a wake of ``caller`` running, or about to? Reads only — never claims."""
    if _wake_live(caller):
        return True
    try:
        hold = _peek_idempotency(_agent_scope(caller), _hold_key(caller))
    except Exception:  # noqa: BLE001
        logger.warning("#4733: hold of %s unreadable; holding", caller)
        return True
    return bool(hold is not None and getattr(hold, "in_flight", False) and _young(hold))


def _stale(hold, caller: str) -> bool:
    return (getattr(hold, "replay", False) and getattr(hold, "in_flight", False)
            and not _young(hold) and not _wake_live(caller))


def _claim_hold(scope: str, key: str, caller: str):
    """Claim the caller's hold, reclaiming one whose wake is gone: older than
    the grace with no live wake row (a holder cancelled by a backend stop).

    None when a stale hold could not be reclaimed: the wake then runs without
    one, as when the claim itself fails. Answering "held" there would park the
    ending while ``_caller_busy`` answers "free" and drains it straight back —
    a loop with no end.
    """
    hold = _begin_idempotency(scope, key)
    if not _stale(hold, caller):
        return hold
    if _reclaim_idempotency(hold):
        logger.info("#4733: reclaimed a stale wake hold of %s", caller)
    hold = _begin_idempotency(scope, key)
    if not _stale(hold, caller):
        return hold
    logger.warning("#4733: a stale wake hold of %s could not be reclaimed; waking without it",
                   caller)
    return None


def _release_hold_of(wake_row) -> None:
    """A wake row's terminal frees the hold its own dispatch took, and no other.

    The hold is that wake's only when it was claimed at or before the row's
    admission (``queued_at`` survives the pull claim and the limiter park,
    which re-stamp ``started_at``) and no other wake of the caller is live. A
    later wake claimed after this row was admitted; its hold stays. Never raises.
    """
    caller = getattr(wake_row, "agent_name", None)
    try:
        hold = _peek_idempotency(_agent_scope(caller), _hold_key(caller))
        if hold is None or not getattr(hold, "in_flight", False):
            return
        admitted = (_as_utc(getattr(wake_row, "queued_at", None))
                    or _as_utc(getattr(wake_row, "started_at", None)))
        claimed = _as_utc(getattr(hold, "claimed_at", None))
        if admitted is None or claimed is None or claimed > admitted:
            return
        if _wake_live(caller):
            return
        _reclaim_idempotency(hold)
    except Exception:  # noqa: BLE001 — the grace and the live-row read still free it
        logger.exception("#4733: releasing the hold of %s failed", caller)


async def _mark_parked(caller: str, callee: str, execution_id: str, policy_source,
                       **details) -> bool:
    """Write the parked marker (latest wins) and its audit. False when Redis
    refused it — the drop is audited ``skipped_window``."""
    marker = f"{_MISSED_KEY_PREFIX}{caller}"
    try:
        _wake_budget_redis().set(marker, execution_id, ex=_MISSED_TTL_SECONDS)
    except Exception as e:  # noqa: BLE001 — fail-open, but the drop is recorded
        logger.warning("#4536: could not park %s for %s (%s); not woken", execution_id, caller, e)
        await _audit(caller=caller, callee=callee, callee_execution_id=execution_id,
                     execution_id=None, status="skipped_window", error=type(e).__name__,
                     policy_source=policy_source, **details)
        return False
    await _audit(caller=caller, callee=callee, callee_execution_id=execution_id,
                 execution_id=None, status="parked", error=None, policy_source=policy_source,
                 **details)
    return True


async def _park(caller: str, callee: str, execution_id: str, policy_source) -> None:
    """Hold an ending for the running wake to deliver when it ends (#4536)."""
    if not await _mark_parked(caller, callee, execution_id, policy_source):
        return
    # The holder may have ended and drained between our check and the SET.
    # Still held: its end drains the marker. Free: nobody will, so take it.
    if await asyncio.to_thread(_caller_busy, caller):
        return
    await _drain_missed(caller)


async def _park_at_capacity(caller: str, callee: str, execution_id: str, idem,
                            policy_source) -> None:
    """No slot for the wake: park the ending instead of a FAILED wake row (#4733).

    The hold is released first — no wake runs. A run of the caller is running
    (``_has_free_slot`` says so), and its end delivers the marker; if it ended
    between the check and the SET, the re-check takes the slot it freed."""
    if idem is not None:
        try:
            _fail_idempotency(idem)
        except Exception:  # noqa: BLE001
            logger.exception("#4733: idempotency release failed")
    if not await _mark_parked(caller, callee, execution_id, policy_source, reason="at_capacity"):
        return
    await _deliver_if_a_slot_freed(caller)


async def _deliver_if_a_slot_freed(caller: str) -> None:
    """After a capacity park: the run that held the slot may have ended between
    the check and the SET, and its own drain then found no marker. Take the
    slot it freed. Each pass needs the meter to change, so this cannot spin."""
    if await _has_free_slot(caller) and not await asyncio.to_thread(_caller_busy, caller):
        await _drain_missed(caller)


def drain_after_slot_release(agent_name: Optional[str]) -> None:
    """A slot of ``agent_name`` was released by a turn that writes its terminal
    without the fan-out — a push ``/chat`` (#4733, E1). Deliver a wake parked
    for a free slot, as any other run's end does. Fire-and-forget; never raises."""
    if not agent_name:
        return
    coro = _drain_if_idle(agent_name)
    try:
        _spawn_followon(coro)
    except Exception:  # noqa: BLE001 — no running loop: the next end delivers it
        coro.close()
        logger.debug("#4733: no loop to drain a parked wake of %s", agent_name)


async def _drain_if_idle(agent_name: Optional[str]) -> None:
    """Any run's end may free the slot a parked wake waits for (#4733, E1).

    One Redis read per terminal; the rows are read only when something is
    parked. While a wake of the agent runs, its own end delivers instead."""
    if not agent_name:
        return
    try:
        client = _wake_budget_redis()
        if client is None or not client.exists(f"{_MISSED_KEY_PREFIX}{agent_name}"):
            return
    except Exception:  # noqa: BLE001 — a hint read; the next end tries again
        return
    if await asyncio.to_thread(_caller_busy, agent_name):
        return
    await _drain_missed(agent_name)


async def _drain_missed(caller: str) -> None:
    """Deliver the ending parked for ``caller``, if any (#4536).

    Called when a wake ends — from the wake row's own terminal and from the
    holder once its dispatch returns — and when any run of the caller ends.
    The marker is taken with GETDEL, so one parked ending is delivered once;
    its follow-on claims the caller's hold like any other wake.
    """
    try:
        parked = _wake_budget_redis().getdel(f"{_MISSED_KEY_PREFIX}{caller}")
    except Exception as e:  # noqa: BLE001
        logger.warning("#4536: could not drain parked wake for %s: %s", caller, e)
        return
    if not parked:
        return
    if isinstance(parked, bytes):
        parked = parked.decode()
    _spawn_followon(maybe_wake_caller(parked, _expect_caller=caller))


async def _dispatch_wake(**kwargs):
    from services.task_execution_service import dispatch_and_await_terminal

    return await dispatch_and_await_terminal(**kwargs)


async def _audit(**details) -> None:
    """The wake's receipt: ids, enums and the deciding policy layer — never the
    callee's words. Best-effort; an audit failure must not break the terminal.

    Mirrors ``operator_resume_service._audit_ending``, including ``source
    ="system"``: no person asked for this turn, so there is no actor to name.
    """
    from services.platform_audit_service import AuditEventType, platform_audit_service

    try:
        await platform_audit_service.log(
            event_type=AuditEventType.EXECUTION,
            event_action="caller_wake_dispatch",
            source="system",
            actor_email=None,
            target_type="agent",
            target_id=details.get("caller"),
            details=details,
        )
    except Exception:  # noqa: BLE001
        logger.exception("ent#1102: wake audit write failed for %s", details.get("caller"))


# --- the frame -------------------------------------------------------------

def _summary_of(row) -> Tuple[str, Optional[int]]:
    """The callee's words, redacted and capped — and, only when the cap cut
    them, the length of the full text (ent#1216), so the frame can say so."""
    raw = getattr(row, "error", None) or getattr(row, "response", None) or ""
    if not raw:
        return "", None
    text = str(raw)
    # Redact credentials at this single chokepoint, then cap — the #1578 order:
    # sanitize a bounded slice, then truncate, so a secret cannot survive by
    # sitting past the cap.
    clean = sanitize_text(text[: SUMMARY_MAX * 2])
    cut = len(text) > SUMMARY_MAX * 2 or len(clean) > SUMMARY_MAX
    return clean[:SUMMARY_MAX], (len(text) if cut else None)


def _framed_wake(row, ask_ids: List[str]) -> str:
    callee = getattr(row, "agent_name", "?")
    execution_id = getattr(row, "id", "?")
    status = getattr(row, "status", "?")
    status = status.value if hasattr(status, "value") else str(status)
    summary, full_length = _summary_of(row)

    lines = [
        f"A run you delegated to `{callee}` has ended: `{execution_id}` → **{status}**.",
        "",
        "You are being woken because it ended — this is the push you were promised "
        "by the receipt. Do not poll for it and do not set a reminder for it.",
    ]
    if ask_ids:
        lines += [
            "",
            f"It stopped at {len(ask_ids)} unanswered ask(s): "
            + ", ".join(f"`{a}`" for a in ask_ids)
            + ". The run ended with those still pending, so its work is NOT finished — "
            "read them with `list_operator_queue` before deciding what to do.",
        ]
    if summary:
        lines += ["", "Its own words on how it ended:", "", summary]
        if full_length is not None:
            # ent#1216: a silent cut read as the run's own answer stopping there.
            lines += ["", f"[cut here: the first {len(summary):,} of {full_length:,} characters]"]
    lines += [
        "",
        f"Read the full result with `get_execution_result('{callee}', '{execution_id}')`. "
        "Other delegated runs may have ended around the same time as this one — "
        "`list_recent_executions` is the complete list.",
    ]
    return "\n".join(lines)


# --- the hook --------------------------------------------------------------

async def maybe_wake_caller(
    execution_id: Optional[str],
    *,
    _expect_caller: Optional[str] = None,
) -> Optional[str]:
    """Wake the agent that delegated ``execution_id``, one wake per caller at a time.

    Returns the wake's execution id, or None when nothing is owed a wake, the
    caller has not opted in, is not running, a wake of it is running (the
    ending then folds into it or is parked for it to deliver, #4536/#4733), it
    has no free slot (parked, #4733), or the dispatch failed. Never raises — the
    terminal is already recorded.

    ``_expect_caller`` is the drain's: a parked ending is woken only for the
    caller it was parked for.
    """
    if not execution_id:
        return None

    try:
        row = _read_row(execution_id)
    except Exception as e:  # noqa: BLE001 — fail-open on the billed terminal path
        logger.debug("ent#1102: get_execution(%s) failed: %s", execution_id, e)
        return None
    if row is None:
        return None

    triggered_by = getattr(row, "triggered_by", None)
    if triggered_by and str(triggered_by) == TRIGGERED_BY_CALLER_WAKE:
        # #4536: a wake's own end delivers what was parked while it ran. It
        # wakes nobody for itself (the recursion break below still holds).
        if getattr(row, "agent_name", None):
            await asyncio.to_thread(_release_hold_of, row)   # #4733: its own hold only
            await _drain_missed(row.agent_name)
        return None
    if _expect_caller is None:
        # #4733 (E1): any run's end may free the slot a parked wake waits for.
        await _drain_if_idle(getattr(row, "agent_name", None))

    caller = getattr(row, "source_agent_name", None)
    callee = getattr(row, "agent_name", None)
    # The caller is read from the row's principal-derived column and nowhere
    # else. `derive_source_and_trigger` writes it from the authenticated agent
    # principal, so no header, body field or event payload can name a third
    # agent to bill (SELF-EXEC-001).
    if not caller or not callee or caller == callee:
        return None
    if _expect_caller is not None and caller != _expect_caller:
        return None
    if getattr(row, "fan_out_id", None):
        return None

    if triggered_by and str(triggered_by) in RESERVED_TRIGGERS:
        return None

    verdict = _resolve_policy(caller)
    if not verdict.enabled:
        logger.debug(
            "ent#1102: %s not woken for %s — %s", caller, execution_id, verdict.reason
        )
        return None

    terminal_status = getattr(row, "status", None)
    if _caller_subscribes_to(caller, callee, terminal_status):
        # AC 6: the emit is already dispatching a turn on this caller for this
        # very terminal. One ending, one turn.
        return None

    try:
        state = await _container_state(caller)
    except Exception:  # noqa: BLE001 — "could not ask" is None, never a raise
        state = None
    if state in ("stopped", "missing"):
        await _audit(caller=caller, callee=callee, callee_execution_id=execution_id,
                     execution_id=None, status="skipped_not_running", error=None,
                     policy_source=verdict.source)
        return None

    idem = None
    scope = _agent_scope(caller)
    key = _hold_key(caller)
    try:
        idem = _claim_hold(scope, key, caller)
    except Exception:  # noqa: BLE001
        logger.exception("ent#1102: idempotency begin failed — dispatching anyway")

    if idem is not None and getattr(idem, "replay", False):
        if getattr(idem, "in_flight", False) and (
                _expect_caller is not None or not _covered(row, idem)):
            # #4536/#4733: a wake of this caller is still running and this run
            # ended after it claimed — it may never see it. Park, never drop.
            # A drained ending was owed delivery by name, so it never folds
            # into a claim that may yet not dispatch (a budget cap, an error).
            await _park(caller, callee, execution_id, verdict.source)
            return None
        logger.info(
            "ent#1102: %s is being woken — no second wake for %s", caller, execution_id,
        )
        return None
    if idem is not None and _wake_live(caller):
        # #4733: the hold is free, but a wake row of this caller still runs (a
        # holder whose wait gave up first). The row is the fact; park for it.
        _fail_idempotency(idem)
        await _park(caller, callee, execution_id, verdict.source)
        return None
    if not await _has_free_slot(caller):
        await _park_at_capacity(caller, callee, execution_id, idem, verdict.source)
        return None

    wake_id = None
    try:
        wake_id = await _claimed_wake(row, caller, callee, execution_id, verdict, idem)
    except asyncio.CancelledError:
        # #4536: a stopping loop would never run the follow-on, and GETDEL
        # would lose the marker. Leave it for the wake row's terminal.
        raise
    except Exception:
        # GeneratorExit (a coroutine closed with its loop) passes through: the
        # wake row's terminal drains, as for a cancel.
        await _drain_missed(caller)
        raise
    if wake_id is _PARKED_AT_CAPACITY:
        # #4733: a run's end delivers it, not this holder — unless that run
        # ended before the marker landed, when its drain found nothing.
        await _deliver_if_a_slot_freed(caller)
        return None
    # #4536: deliver what was parked while this claim was held (the wake
    # row's terminal drains it too).
    await _drain_missed(caller)
    return wake_id


async def _claimed_wake(row, caller, callee, execution_id, verdict, idem):
    capped_at = await _within_wake_budget(callee, caller)
    if capped_at is not None:
        # Released, not completed: no turn ran, so the hold covers nothing. An
        # ending that folded into it meanwhile is not woken (a stated limit).
        if idem is not None:
            try:
                _fail_idempotency(idem)
            except Exception:  # noqa: BLE001
                logger.exception("ent#1102: idempotency release failed")
        await _audit(caller=caller, callee=callee, callee_execution_id=execution_id,
                     execution_id=None, status="skipped_budget", error=None,
                     policy_source=verdict.source, limit=capped_at)
        return None

    ask_ids = _pending_ask_ids(execution_id)
    callee_depth = getattr(row, "chain_depth", None)
    chain_depth = _standing_depth(caller, callee_depth)
    # ent#1225: the wake continues the caller's turn that delegated, and is
    # rated as it. ent#1216 is the fallback for a callee row with no link: an
    # owner's delegation wakes as the owner's turn, every other one as nobody.
    origin = getattr(row, "continues_execution_id", None)
    if isinstance(origin, str) and origin:
        standing = "origin"
        attribution = {"continues_execution_id": origin}
    else:
        person = _standing_person(caller, row)
        standing = "owner" if person is not None else "none"
        attribution = {"source_user_id": person} if person is not None else {}

    try:
        result = await _dispatch_wake(
            agent_name=caller,
            message=_framed_wake(row, ask_ids),
            # ent#751: the skill gate reads person text only, and no person
            # wrote this — the platform did. ent#886/#715: no source_user_email.
            request_text="",
            triggered_by=TRIGGERED_BY_CALLER_WAKE,
            chain_depth=chain_depth,
            **attribution,
        )
    except Exception as exc:  # noqa: BLE001
        if idem is not None:
            try:
                _fail_idempotency(idem)
            except Exception:  # noqa: BLE001
                logger.exception("ent#1102: idempotency release failed")
        logger.exception("ent#1102: wake raised for caller=%s callee=%s", caller, execution_id)
        await _audit(caller=caller, callee=callee, callee_execution_id=execution_id,
                     execution_id=None, status="dispatch_error", error=type(exc).__name__,
                     policy_source=verdict.source, standing=standing)
        return None

    if idem is not None:
        try:
            # #4536/#4733: a finished wake covers nothing that ends after it,
            # so the caller's hold is released and the next ending wakes at
            # once. GETDEL already gives one follow-on per parked ending.
            _fail_idempotency(idem)
        except Exception:  # noqa: BLE001
            logger.exception("ent#1102: idempotency settle failed")

    await _audit(caller=caller, callee=callee, callee_execution_id=execution_id,
                 execution_id=result.execution_id, status=result.status,
                 error=result.error, policy_source=verdict.source,
                 ask_ids=ask_ids, chain_depth=chain_depth,
                 callee_chain_depth=callee_depth, standing=standing)
    if await asyncio.to_thread(_refused_for_a_slot, caller, result) and await _mark_parked(
            caller, callee, execution_id, verdict.source, reason="at_capacity"):
        return _PARKED_AT_CAPACITY      # #4733: the slot check lost a race
    return result.execution_id


def _refused_for_a_slot(caller: str, result) -> bool:
    """Admission refused this wake because every slot was taken (#4733): the
    FAILED row is the stated limit, and the ending is parked for a run's end.
    Off a pilot, CAPACITY is only ever the full slot pool; a pilot's is a full
    backlog or an unclaimed queue, which repeat, so it is not parked. Nor is
    one without a running run, whose end is what delivers it."""
    from services.execution_envelope import TaskExecutionErrorCode

    code = getattr(result, "error_code", None)
    if str(getattr(code, "value", code)) != TaskExecutionErrorCode.CAPACITY.value:
        return False
    try:
        from services.pull_pilot import pull_owns_dispatch

        return not pull_owns_dispatch(caller, TRIGGERED_BY_CALLER_WAKE) and _caller_running(caller)
    except Exception:  # noqa: BLE001 — unknown: leave the FAILED row as it was
        return False
