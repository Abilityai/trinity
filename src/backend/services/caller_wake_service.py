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

**The storm bound.** The idempotency key is ``caller_wake:{caller}:{bucket}``
over a 15-second window, NOT the callee's execution id. A cleanup sweep can
terminate hundreds of stale rows in one cycle, and keying per callee would bill
one turn for each. One turn per caller per window instead, and the frame says
plainly that other runs may have ended in the same window — so the caller reads
``list_recent_executions`` rather than being told a half-truth.

**The wake stands where the delegator stood (#4357).** A wake continues the
delegator's own work, so it carries the delegator's standing, not one more hop:
``max(callee − 1, MAX(running caller), 0)``, NULL counted as 0. The callee row
was stamped ``1 + MAX(running caller)`` at dispatch, so ``callee − 1`` is the
depth the delegator stood at when it delegated; the live MAX keeps a wake from
standing below a deep turn of the caller that is still running (a collapsed
window can read the shallower ending). It was ``callee + 1``, and the wake turn's
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
about to be dispatched — a collapsed window spends nothing. A wake past it is
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

Never raises: this runs on the path of an already-billed terminal.
"""

from __future__ import annotations

import logging
import time
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
})

# One wake per caller per window. 15s is short enough that a caller waiting on
# one delegation is woken promptly, and long enough that a sweep of hundreds of
# stale rows collapses into a handful of turns.
WAKE_BUCKET_SECONDS = 15

# #4357: the per-pair hourly wake budget (the #2973 event-budget shape).
_WAKE_BUDGET_SETTING = "caller_wake_max_per_hour"
_WAKE_WINDOW_SECONDS = 3600
_WAKE_KEY_PREFIX = "trinity:wake_fires:"

# The same cap and chokepoint the #1578 event payload uses, for the same reason.
SUMMARY_MAX = 2000
ASK_IDS_MAX = 10


def _bucket_for(now: float) -> int:
    return int(now // WAKE_BUCKET_SECONDS)


def _idempotency_key(caller: str, now: Optional[float] = None) -> str:
    return f"caller_wake:{caller}:{_bucket_for(now if now is not None else time.time())}"


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


def _complete_idempotency(decision, execution_id) -> None:
    from services import idempotency_service

    idempotency_service.complete(decision, execution_id, None)


def _fail_idempotency(decision) -> None:
    from services import idempotency_service

    idempotency_service.fail(decision)


def _agent_scope(agent_name: str) -> str:
    from services import idempotency_service

    return idempotency_service.make_agent_scope(agent_name)


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
        "Other delegated runs may have ended in the same window as this one — "
        "`list_recent_executions` is the complete list.",
    ]
    return "\n".join(lines)


# --- the hook --------------------------------------------------------------

async def maybe_wake_caller(execution_id: Optional[str]) -> Optional[str]:
    """Wake the agent that delegated ``execution_id``, at most once per window.

    Returns the wake's execution id, or None when nothing is owed a wake, the
    caller has not opted in, is not running, the window was already claimed, or
    the dispatch failed. Never raises — the terminal is already recorded.
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

    caller = getattr(row, "source_agent_name", None)
    callee = getattr(row, "agent_name", None)
    # The caller is read from the row's principal-derived column and nowhere
    # else. `derive_source_and_trigger` writes it from the authenticated agent
    # principal, so no header, body field or event payload can name a third
    # agent to bill (SELF-EXEC-001).
    if not caller or not callee or caller == callee:
        return None
    if getattr(row, "fan_out_id", None):
        return None

    triggered_by = getattr(row, "triggered_by", None)
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
    try:
        idem = _begin_idempotency(_agent_scope(caller), _idempotency_key(caller))
    except Exception:  # noqa: BLE001
        logger.exception("ent#1102: idempotency begin failed — dispatching anyway")

    if idem is not None and getattr(idem, "replay", False):
        logger.info(
            "ent#1102: %s already woken in this window — no second wake for %s",
            caller, execution_id,
        )
        return None

    capped_at = await _within_wake_budget(callee, caller)
    if capped_at is not None:
        # Released, not completed: no turn ran, so the window holds nothing to
        # replay — a later ending in it is skipped by the same budget anyway.
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
    # ent#1216: an owner's delegation wakes as the owner's turn; every other
    # wake carries nobody, as before.
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
            _complete_idempotency(idem, result.execution_id)
        except Exception:  # noqa: BLE001
            logger.exception("ent#1102: idempotency complete failed")

    await _audit(caller=caller, callee=callee, callee_execution_id=execution_id,
                 execution_id=result.execution_id, status=result.status,
                 error=result.error, policy_source=verdict.source,
                 ask_ids=ask_ids, chain_depth=chain_depth,
                 callee_chain_depth=callee_depth, standing=standing)
    return result.execution_id
