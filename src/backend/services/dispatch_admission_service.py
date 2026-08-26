"""
Dispatch admission for the chat/task endpoints (#1483, Invariant #1).

Owns the request-admission orchestration extracted from ``routers/chat.py``:
the idempotency gate (RELIABILITY-006, #525 — begin / replay / release), the
audit-log emission, the ``/chat`` pure-state dispatch-breaker read (#526 F1),
and the ``/chat`` ``CapacityManager.acquire`` (#428). It **composes**
``capacity_manager`` — it does not reimplement capacity or the breaker
(``capacity_manager`` already gates the breaker for ``/task``).

Named "dispatch" not "chat" because it serves BOTH endpoints (RD2): ``/chat`` via
``admit_chat_request`` and ``/task`` via ``begin_task_idempotency`` (the shared
idempotency+audit piece — ``/task`` capacity acquire is genuinely different and
stays with the task-dispatch orchestrator, RD-E11).

**HTTP-free** (Invariant #1): deny/replay outcomes are domain signals —
``ChatAdmission`` / ``ChatAdmissionReplay`` return values and the already-domain
``CapacityFull`` / ``CircuitOpen`` / ``EphemeralBudgetExhausted`` exceptions
(from ``capacity_manager``). The thin router maps them to HTTP. The exact
release-vs-keep idempotency semantics are preserved: an upfront deny releases the
claim (``idempotency_service.fail``) so the caller may retry.
"""
import logging
import uuid as _uuid
from typing import Optional

from models import User, ChatMessageRequest, ExecutionSource
from database import db
from services import idempotency_service
from services.capacity_manager import (
    CapacityFull,
    CircuitOpen,
    EphemeralBudgetExhausted,
    get_capacity_manager,
)
from services.task_execution_service import dispatch_breaker_active
from services.platform_audit_service import platform_audit_service, AuditEventType
from services.chat_signals import ChatAdmission, ChatAdmissionReplay

logger = logging.getLogger(__name__)


async def _audit_idempotent_replay(
    *, name, endpoint, x_via_mcp, x_source_agent,
    current_user, idempotency_key, idem,
):
    """Emit the idempotent-replay audit row (shared by /chat and /task)."""
    # #2323 — attribution comes from the presented credential on `current_user`,
    # never from `X-MCP-Key-Id`/`X-MCP-Key-Name`. Those arrived as plain
    # `Header(None)` on routes gated by `get_current_user` alone and were
    # validated nowhere, so honouring them let any authenticated caller forge the
    # credential named in the two highest-volume audit events on the platform.
    # #2389 finished the removal: the headers are no longer declared on any route
    # and `backlog_service` no longer persists them into the replay blob, so the
    # forgery can no longer surface minutes later on queue drain either. The
    # principal knows which key it presented; that is the only trustworthy
    # source, and it works on the agent-to-agent branch too, where `actor_user`
    # is deliberately None.
    await platform_audit_service.log(
        event_type=AuditEventType.EXECUTION,
        event_action="idempotent_replay",
        source="mcp" if x_via_mcp else "api",
        actor_user=current_user if not x_source_agent else None,
        actor_agent_name=x_source_agent,
        mcp_key_id=getattr(current_user, "mcp_key_id", None),
        mcp_key_name=getattr(current_user, "mcp_key_name", None),
        mcp_scope=getattr(current_user, "mcp_scope", None),
        target_type="agent",
        target_id=name,
        endpoint=endpoint,
        details={
            "idempotency_key": idempotency_key,
            "execution_id": idem.execution_id,
            "in_flight": idem.in_flight,
        },
    )


async def _audit_chat_started(
    *, name, x_via_mcp, x_source_agent, current_user,
    execution_id, queue_result, source, message,
):
    """Emit the chat_started audit row on a successful /chat admission."""
    # #2323 — attribution comes from the presented credential on `current_user`,
    # never from `X-MCP-Key-Id`/`X-MCP-Key-Name`. Those arrived as plain
    # `Header(None)` on routes gated by `get_current_user` alone and were
    # validated nowhere, so honouring them let any authenticated caller forge the
    # credential named in the two highest-volume audit events on the platform.
    # #2389 finished the removal: the headers are no longer declared on any route
    # and `backlog_service` no longer persists them into the replay blob, so the
    # forgery can no longer surface minutes later on queue drain either. The
    # principal knows which key it presented; that is the only trustworthy
    # source, and it works on the agent-to-agent branch too, where `actor_user`
    # is deliberately None.
    await platform_audit_service.log(
        event_type=AuditEventType.EXECUTION,
        event_action="chat_started",
        source="mcp" if x_via_mcp else "api",
        actor_user=current_user if not x_source_agent else None,
        actor_agent_name=x_source_agent,
        mcp_key_id=getattr(current_user, "mcp_key_id", None),
        mcp_key_name=getattr(current_user, "mcp_key_name", None),
        # Was `"agent" if x_source_agent else ("user" if x_via_mcp else None)` —
        # a guess assembled from two client-set headers, which would have
        # reported a bounded #2323 ops key as an unbounded "user" one.
        mcp_scope=getattr(current_user, "mcp_scope", None),
        target_type="agent",
        target_id=name,
        endpoint=f"/api/agents/{name}/chat",
        request_id=None,
        details={
            "execution_id": execution_id,
            "queue_result": queue_result,
            "source": source.value if hasattr(source, "value") else str(source),
            "message_length": len(message) if message else 0,
        },
    )


async def admit_chat_request(
    *,
    name: str,
    request: ChatMessageRequest,
    current_user: User,
    x_source_agent: Optional[str],
    x_via_mcp: Optional[str],
    idempotency_key: Optional[str],
):
    """Admission gate for chat_with_agent (#1026 slice 1), HTTP-free.

    Runs the idempotency gate (#525), the dispatch-breaker fast-fail (#526), and
    ``CapacityManager.acquire`` (#428). Returns either:
      - a ``ChatAdmissionReplay`` — idempotent replay (router builds the 200
        snapshot response or the 409 in-flight error); or
      - a ``ChatAdmission`` — the request is cleared and carries the
        ``idem`` / ``execution_id`` / ``capacity_result`` / ``capacity`` the rest
        of the endpoint consumes.

    Raises the already-domain ``CircuitOpen`` (breaker open) / ``CapacityFull``
    (queue full) / ``EphemeralBudgetExhausted`` (ghost spent) — each after
    releasing the idempotency claim so the caller can retry. The router maps them.
    """
    # RELIABILITY-006 (#525): idempotency gate. Short-circuit duplicate
    # requests before consuming a capacity slot. The header is optional — when
    # absent, dedup is off and the request proceeds normally (back-compat).
    idem = idempotency_service.begin(
        idempotency_service.make_agent_scope(name), idempotency_key
    )
    if idem.replay:
        await _audit_idempotent_replay(
            name=name, endpoint=f"/api/agents/{name}/chat", x_via_mcp=x_via_mcp,
            x_source_agent=x_source_agent, current_user=current_user,
            idempotency_key=idempotency_key, idem=idem,
        )
        return ChatAdmissionReplay(
            execution_id=idem.execution_id, in_flight=idem.in_flight, snapshot=idem.snapshot,
        )

    # Determine execution source
    source = ExecutionSource.AGENT if x_source_agent else ExecutionSource.USER

    # CAPACITY-CONSOLIDATE (#428): single CapacityManager.acquire call. /chat
    # shares the agent's parallel pool with /task and spills to an in-memory
    # queue (depth 3) when the pool is full.
    capacity = get_capacity_manager()
    chat_execution_id = str(_uuid.uuid4())
    chat_timeout = db.get_execution_timeout(name)
    max_parallel_tasks = db.get_max_parallel_tasks(name)
    # #526 F1: /chat does NOT record dispatch outcomes, so it must NOT consume a
    # half-open probe permit — gate with a PURE STATE READ and let /task drive
    # recovery. acquire() runs WITHOUT breaker_enabled.
    if dispatch_breaker_active(name):
        from services.dispatch_breaker import DispatchBreaker
        _disp = DispatchBreaker(name).to_dict()
        if _disp.get("state") == "open":
            logger.warning(f"[Chat] Agent '{name}' dispatch circuit open, rejecting request")
            # Nothing dispatched — release the idempotency claim so the caller
            # can retry with the same key once the breaker recovers (#525).
            idempotency_service.fail(idem)
            raise CircuitOpen(name, int(_disp.get("retry_after_seconds") or 0))
    try:
        capacity_result = await capacity.acquire(
            agent_name=name,
            execution_id=chat_execution_id,
            max_concurrent=max_parallel_tasks,
            message_preview=request.message[:100] if request.message else "",
            timeout_seconds=chat_timeout,
            overflow_policy="queue_in_memory",
            source=source,
            source_agent=x_source_agent,
            source_user_id=str(current_user.id),
            source_user_email=current_user.email or current_user.username,
            message=request.message,
        )
        queue_result = (
            "running"
            if capacity_result.state == "admitted"
            else f"queued:{capacity_result.queue_position}"
        )
        logger.info(f"[Chat] Agent '{name}' execution {chat_execution_id}: {queue_result}")
        await _audit_chat_started(
            name=name, x_via_mcp=x_via_mcp, x_source_agent=x_source_agent,
            current_user=current_user, execution_id=chat_execution_id,
            queue_result=queue_result, source=source, message=request.message,
        )
    except EphemeralBudgetExhausted:
        # trinity-enterprise#69: ghost budget spent — nothing admitted/enqueued.
        idempotency_service.fail(idem)
        raise
    except CapacityFull as e:
        logger.warning(f"[Chat] Agent '{name}' at capacity, rejecting request (reason={e.reason})")
        # Nothing dispatched — release the idempotency claim so the caller can
        # retry with the same key once capacity frees up (#525).
        idempotency_service.fail(idem)
        raise

    return ChatAdmission(
        idem=idem,
        execution_id=chat_execution_id,
        capacity_result=capacity_result,
        capacity=capacity,
        queue_result=queue_result,
        chat_timeout=chat_timeout,
    )


def begin_task_idempotency(
    *,
    name: str,
    idempotency_key: Optional[str],
):
    """Open the ``/task`` idempotency claim (#525).

    Returns ``(idem, replay)`` — ``replay`` is a ``ChatAdmissionReplay`` when the
    key matched a prior request (router builds the 409 / snapshot response),
    else ``None`` and the caller proceeds with ``idem``. Auditing the replay is
    the caller's job (it must run under the request's audit context), done via
    ``audit_idempotent_replay`` below.
    """
    idem = idempotency_service.begin(
        idempotency_service.make_agent_scope(name), idempotency_key
    )
    if idem.replay:
        return idem, ChatAdmissionReplay(
            execution_id=idem.execution_id, in_flight=idem.in_flight, snapshot=idem.snapshot,
        )
    return idem, None


async def audit_idempotent_replay(
    *, name, endpoint, x_via_mcp, x_source_agent,
    current_user, idempotency_key, idem,
):
    """Public wrapper so ``/task`` can emit the same replay audit row.

    #2323 dropped `x_mcp_key_id`/`x_mcp_key_name` from this signature rather
    than accepting-and-ignoring them: a parameter nothing reads is how a
    forgeable value quietly finds a new consumer later.
    """
    await _audit_idempotent_replay(
        name=name, endpoint=endpoint, x_via_mcp=x_via_mcp,
        x_source_agent=x_source_agent, current_user=current_user,
        idempotency_key=idempotency_key, idem=idem,
    )
