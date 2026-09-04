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

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

from config import MAX_REDELIVERY
from database import db
from models import TaskExecutionStatus
from services import event_dispatch_service
from services.activity_service import activity_service
from services.platform_prompt_service import (
    ExecutionContext,
    compose_system_prompt,
    is_execution_context_enabled,
)
from services.slot_service import SLOT_TTL_BUFFER
from services.runtime_secret_scrub import get_staged_values, scrub_obj, scrub_text
from utils.credential_sanitizer import sanitize_execution_log, sanitize_response

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


def _compose_pull_system_prompt(
    agent_name: Optional[str],
    triggered_by: Optional[str],
    caller_prompt: Optional[str],
    *,
    execution_id: Optional[str],
    model: Optional[str] = None,
) -> Optional[str]:
    """Compose platform prompt + execution context + caller override for a
    pull-claimed turn (#1629). Fail-open: on ANY composition error the turn runs
    with the caller prompt only (parity with the pre-#1629 pull path) + a WARN —
    a prompt failure never blocks dispatch.

    ``model`` (ent#243) selects the prompt tier. It is passed explicitly rather
    than left to default because this context previously omitted the field
    entirely — not ``None``-valued, absent — so every pull-claimed turn would
    have resolved VERBOSE forever with nothing to indicate why."""
    runtime = _resolve_agent_runtime(agent_name)
    try:
        exec_ctx = ExecutionContext(
            agent_name=agent_name,
            mode=ExecutionContext.derive_mode(triggered_by),
            triggered_by=triggered_by,
            execution_id=execution_id,
            model=model,
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


def claim_next_task(agent_name: str, worker_id: str) -> Optional[Dict[str, Any]]:
    """Atomically claim the oldest queued task for ``agent_name`` on behalf of
    ``worker_id``. Returns the §3.1 claim response, or None when the queue is
    empty (router → §3.2 empty claim).

    The lease TTL reuses the slot-TTL convention: the agent's
    ``execution_timeout_seconds`` plus ``SLOT_TTL_BUFFER`` (so a legitimately
    long turn's lease outlives its deadline exactly as a slot would).
    """
    lease_seconds = int(db.get_execution_timeout(agent_name)) + SLOT_TTL_BUFFER
    row = db.claim_next_queued(agent_name, worker_id=worker_id, lease_seconds=lease_seconds)
    if not row:
        return None
    return _build_claim_response(row)


def _build_claim_response(row: Dict[str, Any]) -> Dict[str, Any]:
    """Reconstruct the §3.1 claim response from a claimed ``schedule_executions``
    row + its ``backlog_metadata`` JSON.

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
    # ent#243: a caller override is what the worker will actually run, so it wins
    # over the row's recorded model_used; either may be absent → VERBOSE.
    overrides["system_prompt"] = _compose_pull_system_prompt(
        row.get("agent_name"),
        row.get("triggered_by"),
        overrides.get("system_prompt"),
        execution_id=row["id"],
        model=overrides.get("model") or row.get("model_used"),
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

    log_json = None
    if isinstance(execution_log, list) and execution_log:
        try:
            log_json = sanitize_execution_log(json.dumps(execution_log))
        except (TypeError, ValueError):
            log_json = None

    sanitized_content = sanitize_response(content) if content is not None else None

    if row_status == TaskExecutionStatus.SUCCESS:
        won = db.update_execution_status(
            execution_id=execution_id,
            status=TaskExecutionStatus.SUCCESS,
            response=sanitized_content,
            cost=cost,
            context_used=context_used,
            context_max=context_max,
            execution_log=log_json,
            tool_calls=log_json,
            claude_session_id=session_id,
            claim_token=claim_token,
        )
    else:
        # No error_code column on schedule_executions — fold the typed class into
        # the persisted error text so it survives for observability.
        err_text = sanitized_content or ""
        if error_code:
            err_text = f"[{error_code}] {err_text}".strip()
        won = db.update_execution_status(
            execution_id=execution_id,
            status=row_status,
            error=err_text or None,
            response=sanitized_content,
            cost=cost,
            context_used=context_used,
            context_max=context_max,
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
        return ResultApplyOutcome("applied", row_status)

    # CAS lost — reclassify against the freshly-read row.
    current = db.get_execution(execution_id)
    if current is not None and current.status in _ALL_TERMINALS:
        # Duplicate/late over an already-terminal row — idempotent, never clobbers.
        return ResultApplyOutcome("replayed", current.status)
    return ResultApplyOutcome("conflict", current.status if current else None)
