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

from database import db
from models import TaskExecutionStatus
from services.slot_service import SLOT_TTL_BUFFER
from utils.credential_sanitizer import sanitize_execution_log, sanitize_response

logger = logging.getLogger(__name__)

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
        "session_id": meta.get("session_id"),
    }
    if meta.get("file_ids") is not None:
        payload["file_ids"] = meta.get("file_ids")
    if meta.get("task_overrides") is not None:
        payload["task_overrides"] = meta.get("task_overrides")

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
        return ResultApplyOutcome("applied", row_status)

    # CAS lost — reclassify against the freshly-read row.
    current = db.get_execution(execution_id)
    if current is not None and current.status in _ALL_TERMINALS:
        # Duplicate/late over an already-terminal row — idempotent, never clobbers.
        return ResultApplyOutcome("replayed", current.status)
    return ResultApplyOutcome("conflict", current.status if current else None)
