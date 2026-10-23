"""Pull / work-stealing coordination service (#1081 Phase 1 — **DARK**).

The service half of the two internal pull seams (``TARGET_ARCHITECTURE.md``
§"Coordination Model", ``MESSAGE_ENVELOPE_SCHEMA.md`` §3):

* :func:`claim_next_task` — atomically claim an agent's oldest queued task for a
  worker and return the §3.1 claim response (envelope frame + lease metadata).
* :func:`apply_task_result` — CAS-apply a worker's terminal (§3.3) under the
  claim-token gate, returning a typed :class:`ResultApplyOutcome` the router
  maps to §3.4 (``applied`` / ``replayed`` / ``not_found`` / ``conflict``).

**Dark.** Nothing in production dispatch calls these yet — no push path is
rewired, no ``PULL_MODE`` flag exists, no agent worker pulls. Phase 2 wires the
callers. Current behavior is unchanged.

Three-layer (Invariant #1): the router (``routers/internal.py``) does HTTP only;
this module holds the coordination policy; the atomic claim and the CAS terminal
write live in ``db/schedules.py``. This service reuses that machinery
(``claim_next_queued`` / ``update_execution_status``) rather than forking a
parallel claim path — the CAS in ``update_execution_status`` is the single dedup
authority (aligned with ``task_execution_service.apply_result``).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from config import MAX_REDELIVERY
from database import db
from models import TaskExecutionStatus
from services import channel_completion_report
from services import event_dispatch_service
from services import subscription_auto_switch
from services.activity_service import activity_service
from services.platform_prompt_service import (
    ExecutionContext,
    compose_system_prompt,
    is_execution_context_enabled,
)
from services.slot_service import SLOT_TTL_BUFFER
from services.runtime_secret_scrub import get_staged_values, scrub_obj, scrub_text
from utils.credential_sanitizer import sanitize_execution_log, sanitize_response
from db.write_params import ExecutionResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Platform system-prompt composition on the pull path (#1629)
# ---------------------------------------------------------------------------
# A pull-claimed turn used to run with ONLY the caller's `system_prompt` override
# (or nothing) — the operator-queue protocol, file-sharing/user-memory rules, and
# the #1402 async human-gate contract (all delivered via the platform prompt)
# never reached it. Compose the same runtime-aware platform prompt the push path
# builds (task_execution_service) at claim time, folding in the caller override.


def _resolve_agent_runtime(agent_name: Optional[str]) -> str:
    """Best-effort runtime resolution for the platform prompt (#1187). Mirrors
    task_execution_service — lazy, guarded, Claude default on any failure so
    prompt composition never blocks a claim."""
    if not agent_name:
        return "claude-code"
    try:
        from services.docker_service import get_agent_runtime

        return get_agent_runtime(agent_name)
    except Exception:
        return "claude-code"


def _served_person(row: dict) -> Optional[str]:
    """The person the claimed run serves (ent#814/ent#886), read off the row by
    the same helper the push composer uses. Lazy import (as below), fail-soft."""
    try:
        from services.task_execution_service import served_person_for_row

        return served_person_for_row(row)
    except Exception as e:  # noqa: BLE001 — never block a claim on the seat line
        logger.debug("[PullCoordination] served person for %s: %s", row.get("id"), e)
        return None


def _compose_pull_system_prompt(
    agent_name: Optional[str],
    triggered_by: Optional[str],
    caller_prompt: Optional[str],
    *,
    execution_id: Optional[str],
    model: Optional[str] = None,
    source_user_email: Optional[str] = None,
    source_agent_name: Optional[str] = None,
    source_mcp_key_name: Optional[str] = None,
    timeout_seconds: Optional[int] = None,
    schedule_context: Optional[dict] = None,
    attempt: Optional[int] = None,
    served_person_email: Optional[str] = None,
) -> Optional[str]:
    """Compose platform prompt + execution context + caller override for a
    pull-claimed turn (#1629). Fail-open: on ANY composition error the turn runs
    with the caller prompt only (parity with the pre-#1629 pull path) + a WARN —
    a prompt failure never blocks dispatch.

    ``model`` (ent#243) selects the prompt tier. It is passed explicitly rather
    than left to default because this context previously omitted the field
    entirely — not ``None``-valued, absent — so every pull-claimed turn would
    have resolved VERBOSE forever with nothing to indicate why.

    #3114: the provenance, timeout, schedule and attempt fields match what
    ``task_execution_service._compose_effective_system_prompt`` builds on
    push, so an interactive turn routed to the queue sees the same context.
    ``served_person_email`` (ent#814/ent#886) likewise: the caller reads it off
    the claimed row through the same ``served_person_for_row`` push uses."""
    runtime = _resolve_agent_runtime(agent_name)
    try:
        schedule_context = schedule_context or {}
        exec_ctx = ExecutionContext(
            agent_name=agent_name,
            mode=ExecutionContext.derive_mode(triggered_by),
            triggered_by=triggered_by,
            source_user_email=source_user_email,
            source_agent_name=source_agent_name,
            source_mcp_key_name=source_mcp_key_name,
            timeout_seconds=timeout_seconds,
            attempt=attempt,
            schedule_name=schedule_context.get("name"),
            schedule_cron=schedule_context.get("cron"),
            schedule_next_run=schedule_context.get("next_run"),
            execution_id=execution_id,
            model=model,
            served_person_email=served_person_email,
        )
        return compose_system_prompt(
            execution_context=exec_ctx,
            caller_prompt=caller_prompt,
            include_execution_context=is_execution_context_enabled(),
            runtime=runtime,
        )
    except Exception as e:  # noqa: BLE001 — never block a claim on prompt build
        logger.warning(
            "[PullCoordination] platform prompt composition failed for %s "
            "(caller prompt only): %s",
            agent_name, e,
        )
        return caller_prompt


# Platform-authored constant banner (#1629 / #1402): the model otherwise cannot
# observe that a turn is a re-delivery. Only an integer count is interpolated —
# no agent/user text, so no prompt-injection surface. Prepended at READ time to
# the claimed message; the stored `schedule_executions.message` is never mutated.
_REDELIVERY_BANNER = (
    "[Trinity re-delivery notice] This task is a re-delivery — attempt {n} of {cap}. "
    "A previous attempt did not confirm completion, so it is being retried. Any "
    "irreversible external side effect (a payment, an outbound message, a publish, "
    "etc.) may already have been performed on a prior attempt. Verify before "
    "re-performing it; if you cannot verify, PARK the effect via the operator "
    "queue rather than repeat it."
)


def _redelivery_banner(redelivery_count: int) -> str:
    """Framing banner for a re-delivered turn. attempt N = redelivery_count + 1
    (first delivery is 0); cap is the fleet-wide MAX_REDELIVERY."""
    return _REDELIVERY_BANNER.format(n=int(redelivery_count) + 1, cap=MAX_REDELIVERY)


# An incoming result over one of these is an idempotent replay (never
# re-applied). Mirrors ``routers.agents._AUTHORITATIVE_TERMINALS`` (#1083): a
# FAILED row is intentionally NOT here, so a genuinely late SUCCESS can still
# overwrite (e.g. a reaper LEASE_EXPIRED) via the token-gated CAS.
_AUTHORITATIVE_TERMINALS = frozenset({
    TaskExecutionStatus.SUCCESS,
    TaskExecutionStatus.CANCELLED,
    TaskExecutionStatus.SKIPPED,
})
_ALL_TERMINALS = _AUTHORITATIVE_TERMINALS | {TaskExecutionStatus.FAILED}


@dataclass
class ResultApplyOutcome:
    """Typed outcome of :func:`apply_task_result` (router maps to §3.4).

    kind:
        ``applied``   — CAS won, terminal written (§3.4 → 200 ``{applied: true}``).
        ``replayed``  — already terminal, no-op ACK (§3.4 → 200 ``{replayed: true}``).
        ``not_found`` — no such execution (§3.4 → 404).
        ``conflict``  — row not claimable under this token (§3.4 → 409).
    """
    kind: str
    status: Optional[str] = None
    # #3487: ``retry`` — the turn was refused and the worker should run it
    # again under the same claim; this is the instruction it gets back.
    retry: Optional[Dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Per-task settings carried on the claim envelope (#2317)
# ---------------------------------------------------------------------------
# `backlog_service.enqueue` persists a queued row's per-task settings as FLAT
# keys inside `backlog_metadata`, and the PUSH drain (`_spawn_drain`) reads them
# flat. The pull envelope must consume the SAME keys or a pulled turn silently
# loses them. These are the keys that reach the RUNTIME (they map 1:1 onto
# `execute_headless` kwargs the pull worker passes); the remaining metadata keys
# are backend-side concerns (chat-session persistence + provenance) that neither
# path sends to the agent. `tests/unit/test_2317_pull_envelope_parity.py` pins
# that split against the producer itself, so a key added to `enqueue` cannot
# silently belong to neither set.
_TASK_OVERRIDE_KEYS = (
    "model",           # --model            (push: ParallelTaskRequest.model)
    "allowed_tools",   # --allowedTools     (push: ParallelTaskRequest.allowed_tools)
    "max_turns",       # --max-turns        (push: ParallelTaskRequest.max_turns)
    "timeout_seconds", # turn budget        (push: ParallelTaskRequest.timeout_seconds)
    "system_prompt",   # --append-system-prompt; recomposed below (#1629)
)


# ---------------------------------------------------------------------------
# Claim (GET /api/internal/next-task) — §3.1 / §3.2
# ---------------------------------------------------------------------------


def record_worker_poll(agent_name: str) -> None:
    """Stamp the agent's last claim attempt for canary B-08 (#2840).

    Called on every claim attempt, empty or not: an idle worker polls at least
    every 15s, so a stale stamp on an agent with idle workers means the pool
    is dead or cannot reach the backend. Best-effort — a Redis failure must
    never block a claim.
    """
    try:
        from services.pull_pilot import PULL_POLL_KEY_PREFIX, PULL_POLL_TTL_SECONDS
        from services.slot_service import get_slot_service

        get_slot_service().redis.set(
            f"{PULL_POLL_KEY_PREFIX}{agent_name}", time.time(), ex=PULL_POLL_TTL_SECONDS
        )
    except Exception:  # noqa: BLE001
        logger.debug("[#2840] could not record pull poll for %s", agent_name, exc_info=True)


def claim_next_task(agent_name: str, worker_id: str) -> Optional[Dict[str, Any]]:
    """Atomically claim the next queued task for ``agent_name`` on behalf of
    ``worker_id``: interactive turns first (#2842), never a second turn of a
    conversation already running (#2843). Returns the §3.1 claim response, or None when the queue is
    empty (router → §3.2 empty claim).

    The lease TTL reuses the slot-TTL convention: the agent's
    ``execution_timeout_seconds`` plus ``SLOT_TTL_BUFFER`` (so a legitimately
    long turn's lease outlives its deadline exactly as a slot would).

    #2846: the turn limit is clamped to that same timeout (read once here,
    applied in `_build_claim_response` before the prompt is composed, #3321), so
    the turn always ends inside its lease. This is what keeps a healthy turn
    from being re-delivered while it still runs — the job the (never built)
    lease-renewal heartbeat was specified for.
    """
    record_worker_poll(agent_name)
    cap = int(db.get_execution_timeout(agent_name))
    from services.pull_pilot import INTERACTIVE_TRIGGERS, WAITING_CONVERSATION_PREFIX

    row = db.claim_next_queued(
        agent_name,
        worker_id=worker_id,
        lease_seconds=cap + SLOT_TTL_BUFFER,
        interactive_triggers=INTERACTIVE_TRIGGERS,
        waiting_conversation_prefix=WAITING_CONVERSATION_PREFIX,
    )
    if not row:
        return None
    _record_starting_assignment(row)
    return _build_claim_response(row, cap=cap)


def _record_starting_assignment(row: Dict[str, Any]) -> None:
    """Point the claimed row's SUB-004 attribution at the assignment the turn
    starts on (#3487 review). The row was stamped at ENQUEUE, which can be long
    before the claim; the pulled-turn walk reads this as its baseline, the way
    push snapshots the assignment at turn start. Written only when it moved;
    never blocks the claim."""
    try:
        current = db.get_agent_subscription_id(row["agent_name"])
        if current and current != row.get("subscription_id"):
            db.set_execution_subscription(row["id"], current)
            row["subscription_id"] = current
    except Exception as e:  # noqa: BLE001 — a claim must never fail on attribution
        logger.warning("[#3487] could not record the starting assignment for %s: %s",
                       row.get("id"), type(e).__name__)


def _turn_limit(requested: Any, cap: int) -> Tuple[int, Optional[int]]:
    """``(limit, shortened_from)`` — ``requested`` capped at the agent timeout.

    A row carries the timeout copied at enqueue; it exceeds ``cap`` when the
    agent's timeout was lowered while the row waited. Absent/invalid → ``cap``.
    """
    try:
        asked = int(requested)
    except (TypeError, ValueError):
        return cap, None
    if asked <= 0:
        return cap, None
    return (cap, asked) if asked > cap else (asked, None)


def _shortened_note(execution: Any) -> str:
    """Suffix for a timeout error when the row asked for more than the agent allows.

    Best-effort: runs before the terminal CAS write, so it must never raise.
    """
    try:
        meta = json.loads(execution.backlog_metadata or "{}")
        nested = meta.get("task_overrides") if isinstance(meta, dict) else None
        requested = (nested or {}).get("timeout_seconds") or meta.get("timeout_seconds")
        # Compares against the agent's CURRENT timeout, so the note states
        # the two numbers rather than claiming the claim shortened this run
        # (the timeout may have changed after the claim).
        cap = int(db.get_execution_timeout(execution.agent_name))
    except Exception:  # noqa: BLE001 — a missing note must not block the terminal
        return ""
    limit, shortened_from = _turn_limit(requested, cap)
    if shortened_from is None:
        return ""
    return (
        f" (this job asked for {shortened_from}s but the agent's timeout is "
        f"{limit}s; raise the agent's timeout to give it longer)"
    )


def _build_claim_response(row: Dict[str, Any], cap: Optional[int] = None) -> Dict[str, Any]:
    """Reconstruct the §3.1 claim response from a claimed ``schedule_executions``
    row + its ``backlog_metadata`` JSON.

    ``cap`` is the agent timeout: when given, the turn limit is clamped to it
    (#2846) BEFORE the platform prompt is composed, so the prompt states the
    limit the turn actually runs under (#3321).

    Per the schema's honest-scope caveat, the pilot "rides the existing
    ``backlog_metadata`` reconstruction shape" — the coordination frame fields
    (kind/from/correlation/idempotency) come from the metadata when present and
    fall back to sensible defaults derived from the row otherwise.
    """
    meta: Dict[str, Any] = {}
    raw = row.get("backlog_metadata")
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                meta = parsed
        except (TypeError, ValueError):
            meta = {}

    eid = row["id"]
    # Message id is distinct from execution_id (§3.1) but is not a stored column
    # today — reuse the metadata's id when present, else the execution id.
    message_id = meta.get("id") or meta.get("envelope_id") or eid

    frm = meta.get("from")
    if not frm:
        if row.get("source_agent_name"):
            frm = row["source_agent_name"]
        elif row.get("source_user_id") is not None:
            frm = str(row["source_user_id"])
        else:
            frm = "system"

    lease = row.get("lease_expires_at")

    payload: Dict[str, Any] = {
        "message": row.get("message"),
        # #2317: session identity comes from the key the PRODUCER writes.
        # `backlog_service.enqueue` records `resume_session_id` (the Claude Code
        # session the caller asked to resume — EXEC-023) and `chat_session_id`
        # (a Trinity chat-session row id, a BACKEND-side persistence concern the
        # pull sink does not own). The envelope's `session_id` is the Claude Code
        # session UUID (§2 shared payload fields) and the worker feeds it straight
        # to `execute_headless(resume_session_id=...)`, so ONLY `resume_session_id`
        # may source it — `chat_session_id` here would hand the runtime a Trinity
        # row id and resume the wrong (or no) session. `session_id` is still read
        # first for forward-compat with the §2 producer shape nothing writes yet.
        "session_id": meta.get("session_id") or meta.get("resume_session_id"),
    }
    if meta.get("file_ids") is not None:
        payload["file_ids"] = meta.get("file_ids")
    # #3114: the push payload's `persist_session` and `images`, carried for
    # rows `execute_task` queued. Absent on every other producer's row.
    if meta.get("persist_session") is not None:
        payload["persist_session"] = bool(meta.get("persist_session"))
    if meta.get("images"):
        payload["images"] = meta.get("images")

    # #1629: compose the platform system prompt (parity with the push path) and
    # hand it to the worker via task_overrides.system_prompt — the field the pull
    # worker reads (`execute_headless(system_prompt=overrides.get("system_prompt"))`).
    # Fold in any caller override; fail-open leaves the caller prompt (or None).
    #
    # #2317: the per-task settings are sourced from the FLAT metadata keys the
    # producer actually writes (see _TASK_OVERRIDE_KEYS above). This block used to
    # read a nested `task_overrides` object alone — a key no producer has ever
    # written — so `overrides` was always `{}` and every pulled turn silently ran
    # with agent/global defaults instead of the row's model, tool allow-list, turn
    # cap and timeout. The nested object is still honoured as an OVERLAY (the
    # §2.2 quarantine shape a future producer may write) so it wins when present.
    overrides: Dict[str, Any] = {
        key: meta[key]
        for key in _TASK_OVERRIDE_KEYS
        if meta.get(key) is not None
    }
    nested = meta.get("task_overrides")
    if isinstance(nested, dict):
        overrides.update({k: v for k, v in nested.items() if v is not None})
    if cap is not None:
        limit, shortened_from = _turn_limit(overrides.get("timeout_seconds"), cap)
        overrides["timeout_seconds"] = limit
        if shortened_from is not None:
            logger.warning(
                "[#2846] %s: turn limit shortened from %ss to %ss (agent %s timeout)",
                row["id"], shortened_from, limit, row.get("agent_name"),
            )
    # ent#243: a caller override is what the worker will actually run, so it wins
    # over the row's recorded model_used; either may be absent → VERBOSE.
    overrides["system_prompt"] = _compose_pull_system_prompt(
        row.get("agent_name"),
        row.get("triggered_by"),
        overrides.get("system_prompt"),
        execution_id=row["id"],
        model=overrides.get("model") or row.get("model_used"),
        source_user_email=row.get("source_user_email"),
        source_agent_name=row.get("source_agent_name"),
        source_mcp_key_name=row.get("source_mcp_key_name"),
        timeout_seconds=overrides.get("timeout_seconds"),
        schedule_context=meta.get("schedule_context") if isinstance(meta.get("schedule_context"), dict) else None,
        attempt=meta.get("attempt"),
        served_person_email=_served_person(row),
    )
    payload["task_overrides"] = overrides

    # #1629: re-delivered turns get a deterministic framing banner prepended to
    # the message at READ time (never persisted — the stored row.message is
    # untouched). Platform-authored constant + an integer; no injection surface.
    redelivery_count = row.get("redelivery_count") or 0
    if redelivery_count > 0:
        base_message = payload.get("message") or ""
        payload["message"] = f"{_redelivery_banner(redelivery_count)}\n\n{base_message}"

    envelope = {
        "id": message_id,
        "kind": meta.get("kind") or "task",
        "from": frm,
        "to": row.get("agent_name"),
        "correlation_id": meta.get("correlation_id") or message_id,
        "causation_id": meta.get("causation_id"),
        "idempotency_key": meta.get("idempotency_key"),
        # §1: deadline drives the lease. In the dark phase we surface the stamped
        # lease deadline directly (deadline == lease_expires_at); the deadline-vs-
        # lease-grace split is a Phase 2 concern (schema OPEN items).
        "deadline": lease,
        "payload": payload,
    }
    return {
        "envelope": envelope,
        "execution_id": eid,
        # The claim token the worker MUST echo on the §3.3 result POST — it gates
        # the CAS terminal write (#1082). §3.1's field table omitted it, but §3.3
        # (PullTaskResultRequest.claim_token) requires it, so the claim response
        # must surface it for the worker to round-trip (#946 Phase 2).
        "claim_token": row.get("claim_token"),
        "lease_expires_at": lease,
        "claimed_by_worker": row.get("claimed_by_worker"),
        # Re-delivery counter (#1081 Phase 3 — #429/#1402; distinct from
        # retry_count, §3.1). First delivery is 0; the lease reaper bumps the
        # SAME row's redelivery_count on each re-queue, so a re-claimed task
        # surfaces its true count here.
        "redelivery_count": row.get("redelivery_count") or 0,
        # Reserved (v2 #1401) — name + nullability only.
        "prior_trace": None,
    }


# ---------------------------------------------------------------------------
# Result (POST /api/internal/tasks/{id}/result) — §3.3 / §3.4
# ---------------------------------------------------------------------------


def _context_used(metadata: Dict[str, Any], tokens: Optional[int]) -> Optional[int]:
    """Context-window pressure from the agent metadata (cache tokens are the
    stable signal), falling back to the reported ``tokens``. None when unknown."""
    cache_read = metadata.get("cache_read_tokens") or 0
    cache_create = metadata.get("cache_creation_tokens") or 0
    if cache_read + cache_create > 0:
        return cache_read + cache_create
    input_tokens = metadata.get("input_tokens") or 0
    if input_tokens > 0:
        return input_tokens
    return tokens if tokens else None


# #2643: the worker's typed `error_code` and SUB-003's `failure_kind` are two
# different vocabularies, and the quota class does not share a name between
# them — it is `billing` on the wire (`result_callback._STATUS_MAP` maps an
# agent 429 to it) and `rate_limit` in `subscription_rate_limit_events`. A
# pass-through would file every 429 under a kind no reader knows.
_SWITCH_FAILURE_KINDS = {"billing": "rate_limit", "auth": "auth"}


def _switch_failure_kind(
    error_code: Optional[str], error_text: str = ""
) -> Optional[str]:
    """Which SUB-003 failure kind, if any, a pull terminal's code means (#2643).

    An ALLOWLIST, deliberately: a code this map has not heard of switches
    nothing. The inverse — "switch unless the code is one we know is benign" —
    would churn an agent through every subscription it owns the first time a
    worker reports a crash class nobody has taught this map about.

    #3012: a worker older than #3012 reports a model the CLI refused as 503 →
    ``auth``. No subscription can fix that, so the text overrides the code.
    """
    from services.failure_classifier import is_model_rejection

    if is_model_rejection(error_text):
        return None
    return _SWITCH_FAILURE_KINDS.get((error_code or "").strip().lower())


def _spawn_breaker_verdict(agent_name: str, row_status, is_auth: bool) -> None:
    """Record a pulled terminal's dispatch-breaker outcome in the background.

    SUCCESS records a success, an auth failure records ``auth``, anything else
    records nothing (a failure's ``None`` would read as a success). Fail-open.
    """
    if row_status == TaskExecutionStatus.SUCCESS:
        code = None
    elif is_auth:
        code = "auth"
    else:
        return
    try:
        # Lazy: task_execution_service imports half the service layer.
        from services import task_execution_service as tes

        tes._spawn_bg(
            tes._record_dispatch_terminal(
                agent_name, tes.dispatch_breaker_active(agent_name), code
            )
        )
    except Exception as e:
        logger.warning("[#2514] breaker verdict for %s not recorded: %s", agent_name, e)


# Strong refs to in-flight delivery tasks: a bare create_task can be collected
# mid-flight (same reason as activity_service._inflight_close_tasks).
_inflight_delivery_tasks: "set[asyncio.Task[Any]]" = set()


def _delivery_metadata(execution: Any) -> Optional[Dict[str, Any]]:
    """The queued row's metadata when it asks for post-turn delivery, else None.

    Read from the row fetched BEFORE the terminal CAS. The #1449 scrub is a
    retention job over authoritative terminals, so the blob is still present.
    """
    try:
        meta = json.loads(getattr(execution, "backlog_metadata", None) or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(meta, dict):
        return None
    wants = (
        meta.get("save_to_session")
        or meta.get("collaboration_activity_id")
        or (meta.get("is_self_task") and meta.get("self_task_activity_id"))
    )
    return meta if wants else None


async def _deliver_and_signal(
    execution: Any, meta: Dict[str, Any], applied_status: Any
) -> None:
    """Run the shared post-turn delivery for a pulled turn, then wake the sync
    waiter with the chat session it was saved to. The waiter is signalled even
    when delivery fails: the terminal is already committed."""
    from services.backlog_service import request_from_metadata
    from services.chat_execution_service import run_post_turn_delivery
    from services.sync_waiter import signal_sync_waiter
    from services.task_execution_service import result_from_execution_row

    chat_session_id = None
    try:
        result = result_from_execution_row(execution.id)
        # Deliver only the terminal this CAS wrote. A late SUCCESS correcting a
        # FAILED row spawns its own delivery; the FAILED one must not re-read
        # the corrected row and deliver it a second time.
        if result is not None and result.status == applied_status:
            # The terminal CAS wrote duration_ms from the claim-time started_at.
            applied = db.get_execution(execution.id)
            execution_time_ms = getattr(applied, "duration_ms", None)
            chat_session_id = await run_post_turn_delivery(
                agent_name=execution.agent_name,
                request=request_from_metadata(meta),
                result=result,
                execution_id=execution.id,
                collaboration_activity_id=meta.get("collaboration_activity_id"),
                user_id=meta.get("user_id"),
                user_email=meta.get("user_email"),
                subscription_id=meta.get("subscription_id"),
                is_self_task=meta.get("is_self_task") or False,
                self_task_activity_id=meta.get("self_task_activity_id"),
                execution_time_ms=execution_time_ms,
            )
    except Exception as e:  # noqa: BLE001 — never affect a committed terminal
        logger.warning(
            "[#2329] post-turn delivery failed for %s: %s", execution.id, type(e).__name__
        )
    finally:
        signal_sync_waiter(execution.id, None, chat_session_id)


def _spawn_post_turn_delivery(execution: Any, applied_status: Any) -> None:
    """Sync entry from the CAS-won branch. Nothing to deliver, or no running
    loop: wake the waiter now. Otherwise deliver first, then wake it."""
    from services.sync_waiter import signal_sync_waiter

    meta = _delivery_metadata(execution)
    if meta is not None:
        try:
            task = asyncio.get_running_loop().create_task(
                _deliver_and_signal(execution, meta, applied_status)
            )
        except RuntimeError as e:
            logger.warning("[#2329] post-turn delivery skipped for %s (no loop): %s",
                           execution.id, e)
        else:
            _inflight_delivery_tasks.add(task)
            task.add_done_callback(_inflight_delivery_tasks.discard)
            return
    signal_sync_waiter(execution.id, None, None)


def apply_task_result(
    execution_id: str,
    claim_token: str,
    *,
    status: str,
    content: Optional[str] = None,
    error_code: Optional[str] = None,
    cost: Optional[float] = None,
    tokens: Optional[int] = None,
    session_id: Optional[str] = None,
    execution_log: Optional[list] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> ResultApplyOutcome:
    """CAS-apply a worker's terminal (§3.3) under the claim-token gate.

    Fail-closed and idempotent (§3.4):
      * unknown execution ⇒ ``not_found`` (404).
      * an authoritative terminal (SUCCESS/CANCELLED/SKIPPED) already present ⇒
        ``replayed`` (200, no write). A FAILED row falls through so a late
        SUCCESS can still correct it via the CAS.
      * the token-gated CAS write wins ⇒ ``applied`` (200). The status
        precondition (#1082) + ``claim_token`` match is a SINGLE atomic UPDATE —
        no read-then-write.
      * CAS lost + row now terminal ⇒ ``replayed`` (duplicate/late — never
        double-applies). CAS lost + row still non-terminal ⇒ ``conflict`` (409):
        the token didn't match (stale / wrong worker).
    """
    execution = db.get_execution(execution_id)
    if execution is None:
        return ResultApplyOutcome("not_found")

    if execution.status in _AUTHORITATIVE_TERMINALS:
        # Already final and authoritative — idempotent replay, no re-apply.
        return ResultApplyOutcome("replayed", execution.status)

    # ent#279: identity-scrub the worker's RAW text BEFORE sanitize_response /
    # json.dumps below and before it lands in schedule_executions.response /
    # error / execution_log. This is the pull sink -- SYNC, which is why the
    # scrub seam ships a sync API. Empty staged set -> no-op.
    _staged = get_staged_values()
    if _staged:
        content = scrub_text(_staged, content)
        execution_log = scrub_obj(_staged, execution_log)

    # Map the typed reply status → row status (mirror the #1083 3-way map). An
    # auth failure the agent mislabels "cancelled" must NOT become a clean cancel
    # (backend trust boundary): only `auth` matters for that guard here.
    is_auth = (error_code or "").strip().lower() == "auth"
    if status == "success":
        row_status = TaskExecutionStatus.SUCCESS
    elif status == "cancelled" and not is_auth:
        row_status = TaskExecutionStatus.CANCELLED
    else:
        row_status = TaskExecutionStatus.FAILED

    metadata = metadata or {}
    context_used = _context_used(metadata, tokens)
    context_max = metadata.get("context_window") or 200000

    # #3487: a turn that walked the pool owns its attempts' cost and its trail.
    # The refused attempts already spent (a mid-turn 429), so their cost rolls
    # into the terminal — the push path's `previous_attempt_cost` rule.
    walk = _walked(execution, claim_token)
    if walk is not None:
        prior_cost = _walk_state(execution, claim_token).get("attempt_cost") or 0
        if prior_cost:
            cost = (cost or 0) + float(prior_cost)

    log_json = None
    if isinstance(execution_log, list) and execution_log:
        try:
            log_json = sanitize_execution_log(json.dumps(execution_log))
        except (TypeError, ValueError):
            log_json = None

    sanitized_content = sanitize_response(content) if content is not None else None

    # #3114: the Session tab reads compact events off the result; persist them
    # so a sync caller rebuilding its result from this row still gets them.
    compact_events = metadata.get("compact_events") or []
    compact_json = json.dumps(compact_events) if compact_events else None

    if row_status == TaskExecutionStatus.SUCCESS:
        won = db.update_execution_status(
            execution_id=execution_id,
            status=TaskExecutionStatus.SUCCESS,
            result=ExecutionResult(
                response=sanitized_content,
                cost=cost,
                context_used=context_used,
                context_max=context_max,
                execution_log=log_json,
                tool_calls=log_json,
                claude_session_id=session_id,
                compact_metadata=compact_json,
            ),
            claim_token=claim_token,
        )
    else:
        # No error_code column on schedule_executions — fold the typed class into
        # the persisted error text so it survives for observability.
        err_text = sanitized_content or ""
        if error_code:
            err_text = f"[{error_code}] {err_text}".strip()
        if (error_code or "").strip().lower() == "timeout":
            # #2846: name the setting to change when the claim clamped the limit.
            err_text += _shortened_note(execution)
        if walk is not None:
            # #3487: "Tried: A (rate limit) -> B (auth) -> the platform API key …"
            err_text = f"{err_text}{walk.trail_text()}"
        won = db.update_execution_status(
            execution_id=execution_id,
            status=row_status,
            result=ExecutionResult(
                error=err_text or None,
                response=sanitized_content,
                cost=cost,
                context_used=context_used,
                context_max=context_max,
                compact_metadata=compact_json,
            ),
            claim_token=claim_token,
        )

    if won:
        logger.info(
            "[#1081] pull result applied for %s: status=%s error_code=%s",
            execution_id, row_status, error_code,
        )
        # #1578: emit agent.task.completed/failed at the pull sink terminal too,
        # on the CAS-won branch only (a replayed/late report short-circuits above
        # or loses the CAS — no double-wake). Dark until a pull pilot is enabled,
        # but wired now so a pilot doesn't silently dark-fail report-back.
        # Fire-and-forget + fail-open. `apply_task_result` is sync but is called
        # from an async router handler, so create_task has a running loop.
        summary = (
            sanitized_content
            if row_status == TaskExecutionStatus.SUCCESS
            else (err_text or None)
        )
        event_dispatch_service.spawn_task_terminal_event(
            execution.agent_name,
            execution_id,
            terminal_status=row_status,
            summary_or_error=summary,
            cost=cost,
        )
        # #3114: a delegated child that ran on a pull pilot reports back to the
        # channel / portal / room it came from, exactly as the push terminals
        # do. `report_completion` skips INLINE_CHANNEL_TRIGGERS turns itself.
        channel_completion_report.spawn_completion_report(
            execution_id=execution_id,
            agent_name=execution.agent_name,
            status=str(getattr(row_status, "value", row_status)),
            summary_or_error=summary,
        )
        # #1804: the pull sink is a CAS-won terminal writer, so it owns closing
        # the paired dispatch activity — the issue names this as one of the next
        # two victims of the old per-site model. Sync function, async caller:
        # use the spawn wrapper (mirrors spawn_task_terminal_event above).
        # An authoritative SUCCESS may upgrade an activity a reaper already
        # FAILED, matching this function's own late-SUCCESS-corrects-FAILED rule.
        # Dark until a pull pilot is enabled, wired now exactly as #1578 did.
        activity_service.spawn_close_execution_activity(
            execution_id,
            row_status,
            error=(None if row_status == TaskExecutionStatus.SUCCESS else (err_text or None)),
        )
        # #2643: SUB-003. This sink had every other terminal hook and not this
        # one, so a pull-owned turn that died on a quota or credential failure
        # recorded no `subscription_rate_limit_events` row (no skip-list entry,
        # no usage card, no pressure badge) and left the agent pinned to the
        # subscription that had just refused it — the push path has had all of
        # that since #441/#471/#792.
        #
        # CAS-won branch only, beside the two hooks above: a replayed or late
        # terminal short-circuits or loses the CAS, so it can never spend a
        # second switch (the #1083 rule). `handle_subscription_failure` owns
        # the rest — it records the event unconditionally, then takes the #799
        # per-agent `agent_switch_lock` and re-reads under it, so two failures
        # racing on one agent still switch once.
        #
        # Keyed on `error_code` rather than on the FAILED/CANCELLED split: a
        # quota refusal is a quota refusal whether the worker labelled the turn
        # failed or cancelled, and the push path likewise decides from the
        # failure class the agent reported. A SUCCESS terminal is the one
        # exclusion, and on the merits rather than for tidiness — the provider
        # served that turn, which is evidence the subscription WORKS, so a
        # stray `error_code` riding a success must not move the agent off it.
        # (It is also the only branch where `err_text` is never bound.)
        #
        # It does NOT re-deliver. Re-delivery is the lease reaper's call and the
        # #1085 governor's correlated-cause pause still gates it; this only puts
        # the agent somewhere the next attempt can succeed. Dark until a pull
        # pilot is enabled, wired now exactly as #1578 and #1804 were.
        # #3487: once the turn has walked the pool, the walk already recorded
        # every refusal against the credential it ran on — the one-shot switch
        # would record the last one twice and move the agent again (the push
        # path's `subscription_switch_attempted` rule).
        switch_kind = (
            None
            if row_status == TaskExecutionStatus.SUCCESS or walk is not None
            else _switch_failure_kind(error_code, err_text)
        )
        if switch_kind is not None:
            subscription_auto_switch.spawn_subscription_failure(
                execution.agent_name,
                error_message=err_text or f"[{error_code}] pull terminal",
                failure_kind=switch_kind,
            )
        # #2514: the dispatch-breaker verdict (#526 D10), as the push terminals
        # record it. Without it a pilot's breaker never trips on a pulled auth
        # failure and never closes on the half-open probe it pulled.
        _spawn_breaker_verdict(execution.agent_name, row_status, is_auth)
        # #2329: apply the caller's post-turn delivery settings (chat-session
        # save, collaboration / self-task finalisation) exactly as the push
        # drain does, then wake a sync waiter in this process. Its DB poll
        # covers a caller held by another worker (#3114).
        _spawn_post_turn_delivery(execution, row_status)
        return ResultApplyOutcome("applied", row_status)

    # CAS lost — reclassify against the freshly-read row.
    current = db.get_execution(execution_id)
    if current is not None and current.status in _ALL_TERMINALS:
        # Duplicate/late over an already-terminal row — idempotent, never clobbers.
        return ResultApplyOutcome("replayed", current.status)
    return ResultApplyOutcome("conflict", current.status if current else None)


# ---------------------------------------------------------------------------
# #3487 — SUB-003 walk on a pulled turn, in place under the same claim
# ---------------------------------------------------------------------------
#
# #3470 made a refused turn walk every subscription and then the platform API
# key before it fails — on PUSH, where the walk is a loop inside one dispatch
# call. A pulled turn's attempts are separate result POSTs instead, possibly to
# different backend workers, so here the walk lives on the row
# (`backlog_metadata["subscription_walk"]`, never the secret) and each refused
# attempt is answered with an instruction rather than a terminal: run the turn
# again, under the SAME claim and lease, on this credential. The worker re-spawns
# it and POSTs again. Nothing is re-queued — re-delivery stays the lease
# reaper's (TARGET_ARCHITECTURE: the only recovery primitive), and the sync
# waiter, which ignores a `running` row, keeps waiting for the one terminal.
#
# One attempt, one winner: the refused POST must rotate the claim token
# (`db.rotate_claim_token`) before it may record anything, so a duplicate or
# late POST of the same attempt can neither double-record the refusal nor write
# a FAILED terminal over the retry. A duplicate gets the stored answer again.
#
# The walk state belongs to ONE claim. It records the digest of the token that
# owns it (`owner`); a state left by an earlier claim — the lease reaper
# re-queued the row and another worker re-claimed it — is ignored, so a new
# claim starts its own walk and a late POST of the old claim is never told to
# run the turn again.
#
# Only for a worker that says it can take a per-spawn credential
# (`auth_override_capable` — a Claude runtime on a #3487 image). Anything else
# keeps the pre-#3487 behaviour: the one-shot switch and a FAILED terminal.
# The #1085 governor is deliberately not consulted: push has no such gate, and
# its pause is armed by several agents failing on shared credentials — exactly
# when moving to another credential helps. The rung cap and the turn's
# remaining time are the bounds.

WALK_STATE_KEY = "subscription_walk"


def _row_meta(execution: Any) -> Dict[str, Any]:
    try:
        meta = json.loads(getattr(execution, "backlog_metadata", None) or "{}")
    except (TypeError, ValueError):
        return {}
    return meta if isinstance(meta, dict) else {}


def _token_digest(token: Optional[str]) -> str:
    import hashlib
    return hashlib.sha256((token or "").encode()).hexdigest()[:24]


def _walk_state(execution: Any, claim_token: Optional[str] = None) -> Dict[str, Any]:
    """The row's walk state. With ``claim_token``: only when that token owns it
    (a state another claim wrote is not this claim's walk)."""
    state = _row_meta(execution).get(WALK_STATE_KEY)
    if not isinstance(state, dict):
        return {}
    if claim_token is not None and state.get("owner") != _token_digest(claim_token):
        return {}
    return state


def _walked(execution: Any, claim_token: str):
    """The claim's walk, when it has run at least one attempt; else None."""
    state = _walk_state(execution, claim_token).get("walk")
    if not isinstance(state, dict):
        return None
    try:
        walk = subscription_auto_switch.walk_from_state(state)
    except Exception:  # noqa: BLE001 — an unreadable state is "no walk", never a crash
        logger.warning("[#3487] unreadable walk state on %s", getattr(execution, "id", "?"))
        return None
    return walk if walk.attempts else None


def _save_walk_state(execution: Any, claim_token: str, state: Optional[Dict[str, Any]]) -> bool:
    """Write the walk state owned by ``claim_token`` (None drops it)."""
    meta = _row_meta(execution)
    if state is None:
        meta.pop(WALK_STATE_KEY, None)
    else:
        meta[WALK_STATE_KEY] = {**state, "owner": _token_digest(claim_token)}
    try:
        return db.set_backlog_metadata_for_token(execution.id, claim_token, json.dumps(meta))
    except Exception as e:  # noqa: BLE001
        logger.error("[#3487] could not save the walk state for %s: %s", execution.id, type(e).__name__)
        return False


def _turn_limit_s(execution: Any) -> int:
    """The row's own turn limit, as the claim computed it (#2846): the row's
    ``timeout_seconds`` clamped to the agent timeout."""
    meta = _row_meta(execution)
    nested = meta.get("task_overrides")
    requested = (nested if isinstance(nested, dict) else {}).get("timeout_seconds") or meta.get("timeout_seconds")
    limit, _ = _turn_limit(requested, int(db.get_execution_timeout(execution.agent_name)))
    return limit


def _remaining_budget_s(execution: Any) -> Optional[float]:
    """Seconds the turn may still spend: the row's own turn limit minus the time
    since this claim started. `started_at` is fixed for the life of a claim. The
    limit is the row's, not the agent's, so a retry never outlives the timeout
    the job asked for (push counts from the turn's `timeout_seconds` too).
    None when unreadable."""
    from datetime import datetime, timezone

    from utils.helpers import parse_iso_timestamp

    try:
        limit = float(_turn_limit_s(execution))
        started = parse_iso_timestamp(execution.started_at)
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    except Exception:  # noqa: BLE001
        return None
    return limit - elapsed


def _refusal_text(content: Optional[str]) -> str:
    """The worker's error text, made safe to record: identity-scrubbed against
    the staged runtime secrets (ent#279) and pattern-sanitized, BEFORE it
    reaches the rate-limit event, the walk state or the audit."""
    from utils.credential_sanitizer import sanitize_text

    text = content or ""
    staged = get_staged_values()
    if staged:
        text = scrub_text(staged, text)
    return sanitize_text(text) or ""


def _reconcile_ran_on(walk: Any, metadata: Dict[str, Any]) -> None:
    """Make `walk.active` say what the attempt that just came back ran on.

    The worker reports which credential it applied (`auth_override`, the pull
    analogue of `X-Trinity-Auth-Override`). A trial it did not apply — the
    secret could not be resolved, or a restarted worker ran the turn afresh —
    ran on the baseline, so the refusal belongs to the baseline. Unlike push,
    that does not mean the image cannot take one: the worker said it can.
    """
    if walk.active is None:
        return
    if (metadata or {}).get("auth_override") == walk.active.override_kind:
        walk.override_supported = True
    else:
        walk.active = None


def _retry_outcome(walk_state: Dict[str, Any], pending: Dict[str, Any]) -> Optional[ResultApplyOutcome]:
    """The `retry` instruction for a stored attempt. The credential is read
    fresh, never stored."""
    walk = subscription_auto_switch.walk_from_state(walk_state)
    rung = walk.active
    override = None
    if rung is not None:
        secret = subscription_auto_switch.rung_credential(rung)
        if secret:
            rung.credential = secret
            override = rung.payload()
    return ResultApplyOutcome(
        "retry",
        TaskExecutionStatus.RUNNING,
        retry={
            "claim_token": pending["claim_token"],
            "auth_override": override,
            "timeout_seconds": pending.get("timeout_seconds"),
            "attempt": pending.get("attempt"),
        },
    )


async def _walk_refused_attempt(
    execution_id: str,
    claim_token: str,
    *,
    status: str,
    error_code: Optional[str],
    content: Optional[str],
    cost: Optional[float],
    metadata: Optional[Dict[str, Any]],
) -> Tuple[Optional[ResultApplyOutcome], str]:
    """Walk one refused attempt. Returns ``(retry_outcome, token)``:
    a retry instruction to send back, or None to apply the result as a
    terminal under ``token`` (rotated when this call won the attempt)."""
    if status == "success":
        return None, claim_token
    text = _refusal_text(content)
    failure_kind = _switch_failure_kind(error_code, text)
    if failure_kind is None:
        return None, claim_token
    from services.failure_classifier import is_container_fault

    if is_container_fault(text):
        # The container cannot run Claude at all — no credential fixes that, and
        # a walk would record a refusal against every subscription and the key
        # (`classify_switch_failure`'s #3470 guard on push).
        return None, claim_token
    md = metadata or {}
    if not md.get("auth_override_capable") or md.get("terminated"):
        return None, claim_token
    execution = db.get_execution(execution_id)
    if execution is None or execution.status != TaskExecutionStatus.RUNNING:
        return None, claim_token

    stored = _walk_state(execution)
    pending = stored.get("pending_retry")
    if (
        pending
        and stored.get("walk")
        and stored.get("superseded_token") == _token_digest(claim_token)
        # The retry it was given must still own the row: after a lease requeue
        # and a re-claim the new claim owns it, and a late POST of the old one
        # must not run the turn a second time.
        and db.holds_claim_token(execution_id, pending.get("claim_token") or "")
    ):
        # A duplicate of the POST that already won this attempt (its 200 was
        # lost, or the worker resent it): the same answer again.
        return _retry_outcome(stored["walk"], pending), claim_token
    # The token itself is checked by the rotation below — atomically, with the
    # row still `running` (the execution model does not carry `claim_token`).
    import secrets as _secrets
    new_token = _secrets.token_urlsafe(24)
    if not db.rotate_claim_token(execution_id, claim_token, new_token):
        return None, claim_token  # lost the attempt — the result applies (and conflicts) as before

    from services.task_execution_service import _AGENT_HTTP_SLACK_S, _SWITCH_RETRY_MIN_REMAINING_S

    # Only THIS claim's walk continues; a state another claim left is dropped.
    state = _walk_state(execution, claim_token)
    if state.get("walk"):
        walk = subscription_auto_switch.walk_from_state(state["walk"])
    else:
        # The baseline is the assignment this claim STARTED on (recorded at
        # claim, `_record_starting_assignment`), as push records it at turn
        # start — not the one in force now, which a concurrent walk may have moved.
        walk = subscription_auto_switch.start_subscription_walk(
            execution.agent_name, getattr(execution, "triggered_by", None),
            baseline_subscription_id=getattr(execution, "subscription_id", None),
        )
    _reconcile_ran_on(walk, md)
    prior_cost = float(state.get("attempt_cost") or 0)

    remaining = _remaining_budget_s(execution)
    budget_ok = remaining is None or remaining > _SWITCH_RETRY_MIN_REMAINING_S
    rung = await subscription_auto_switch.advance_subscription_walk(
        walk, failure_kind=failure_kind, error_message=text, budget_ok=budget_ok,
    )
    new_state: Dict[str, Any] = {"walk": subscription_auto_switch.walk_to_state(walk)}

    if rung is None:
        logger.warning(
            "[#3487] %s: SUB-003 walk on pulled turn %s stopped after %d attempt(s) (%s)",
            execution.agent_name, execution_id, len(walk.attempts), walk.stop_reason,
        )
        if walk.stop_reason != "budget":
            try:
                subscription_auto_switch.notify_pool_exhausted(
                    walk, subscription_auto_switch.earliest_known_reset(
                        subscription_auto_switch.walk_reset_candidates(walk)
                    ),
                )
            except Exception as e:  # noqa: BLE001
                logger.debug("[#3487] exhausted notice failed: %s", e)
        # This attempt becomes the terminal and carries its own cost; only the
        # attempts before it roll in.
        new_state["attempt_cost"] = prior_cost
        new_state["summary"] = walk.summary()
        _save_walk_state(execution, new_token, new_state)
        return None, new_token

    new_state["attempt_cost"] = prior_cost + float(cost or md.get("cost_usd") or 0)
    timeout_s = None
    if remaining is not None:
        timeout_s = max(1, int(remaining - _AGENT_HTTP_SLACK_S))
    new_state["pending_retry"] = {
        "claim_token": new_token,
        "timeout_seconds": timeout_s,
        "attempt": len(walk.attempts),
    }
    new_state["superseded_token"] = _token_digest(claim_token)
    new_state["summary"] = walk.summary()
    _save_walk_state(execution, new_token, new_state)
    logger.warning(
        "[#3487] %s: SUB-003 walk on pulled turn %s (%s) — retry %d under the same claim",
        execution.agent_name, execution_id, failure_kind, len(walk.attempts),
    )
    try:
        from services.platform_audit_service import AuditEventType, platform_audit_service

        await platform_audit_service.log(
            event_type=AuditEventType.EXECUTION,
            event_action="auto_retry",
            source="pull_coordination_service",
            actor_agent_name=execution.agent_name,
            target_type="execution",
            target_id=execution_id,
            details={
                "reason": "subscription_auto_switch",
                "attempt": len(walk.attempts) + 1,
                "phase": "initiated",
                "failure_kind": failure_kind,
                "rung": rung.kind,
                "attempts": [dict(a) for a in walk.attempts],
            },
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("[#3487] audit failed (non-fatal): %s", e)
    return _retry_outcome(new_state["walk"], new_state["pending_retry"]), new_token


async def _commit_before_success(
    execution_id: str, claim_token: str, metadata: Optional[Dict[str, Any]]
) -> str:
    """A pulled turn SERVED. BEFORE its terminal is written — the CAS wakes the
    caller, who must already read the switch — make the credential it ran on
    durable where that is right (`commit_subscription_walk`, exactly as push
    does after a served trial) and point the row's SUB-004 attribution at it.

    Won by a token rotation, like a refused attempt, so a duplicate success POST
    commits nothing a second time. Returns the token the terminal must be
    written under. A turn that never walked costs one Redis GET (the "returned
    to its subscription" transition). Never raises."""
    token = claim_token
    try:
        execution = db.get_execution(execution_id)
        if execution is None or execution.status != TaskExecutionStatus.RUNNING:
            return token
        state = _walk_state(execution, claim_token)
        stale = bool(_walk_state(execution)) and not state
        if state.get("walk"):
            walk = subscription_auto_switch.walk_from_state(state["walk"])
            _reconcile_ran_on(walk, metadata or {})
        else:
            marker = subscription_auto_switch.API_KEY_ROUTE_NOTICE_KEY.format(name=execution.agent_name)
            if not subscription_auto_switch._notice_marker_present(marker):
                if stale:
                    # An earlier claim's walk is not this turn's: its summary
                    # must not ride this success.
                    _save_walk_state(execution, claim_token, None)
                return token
            walk = subscription_auto_switch.start_subscription_walk(
                execution.agent_name, getattr(execution, "triggered_by", None)
            )
        import secrets as _secrets
        new_token = _secrets.token_urlsafe(24)
        if not db.rotate_claim_token(execution_id, claim_token, new_token):
            return token  # a duplicate (or stale) POST — the CAS replays or conflicts it
        token = new_token
        last_kind = (walk.attempts[-1].get("failure") if walk.attempts else None) or "rate_limit"
        await subscription_auto_switch.commit_subscription_walk(walk, failure_kind=last_kind)
        served_kind, served_id, _ = walk.ran_on()
        if served_kind == "api_key" or (served_id and walk.committed and walk.committed.get("switched")):
            db.set_execution_subscription(execution_id, served_id)
        if walk.started:
            new_state = {k: v for k, v in state.items() if k not in ("pending_retry", "superseded_token")}
            new_state["walk"] = subscription_auto_switch.walk_to_state(walk)
            new_state["summary"] = walk.summary()
            _save_walk_state(execution, token, new_state)
        elif stale:
            _save_walk_state(execution, token, None)
    except Exception as e:  # noqa: BLE001
        logger.error("[#3487] commit for a served pulled turn failed for %s: %s", execution_id, e)
    return token


async def handle_task_result(
    execution_id: str,
    claim_token: str,
    *,
    status: str,
    content: Optional[str] = None,
    error_code: Optional[str] = None,
    cost: Optional[float] = None,
    tokens: Optional[int] = None,
    session_id: Optional[str] = None,
    execution_log: Optional[list] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> ResultApplyOutcome:
    """The pull result seam (§3.3): walk a refused attempt in place (#3487);
    commit a served walk; then CAS-apply the terminal (`apply_task_result`)."""
    retry, token = await _walk_refused_attempt(
        execution_id, claim_token, status=status, error_code=error_code,
        content=content, cost=cost, metadata=metadata,
    )
    if retry is not None:
        return retry
    if status == "success":
        token = await _commit_before_success(execution_id, token, metadata)
    return apply_task_result(
        execution_id, token, status=status, content=content, error_code=error_code,
        cost=cost, tokens=tokens, session_id=session_id,
        execution_log=execution_log, metadata=metadata,
    )
