"""
Internal endpoints with shared-secret authentication (C-003).

These endpoints are called by:
- Agent containers on the Docker network to communicate back to the backend
- Dedicated scheduler service (trinity-scheduler) for task execution and activity tracking

Security: Requires X-Internal-Secret header matching INTERNAL_API_SECRET env var.
Falls back to SECRET_KEY if INTERNAL_API_SECRET is not set.
"""
import asyncio
import os
import hmac
from fastapi import APIRouter, HTTPException, Depends, Request, Header
from typing import Optional, Dict
import logging

from database import db
from models import (
    ActivityCompleteRequest,
    ActivityTrackRequest,
    ActivityType,
    InternalAuditRequest,
    InternalTaskExecutionRequest,
    PullTaskResultRequest,
    ShareFileRequest,
    ShareFileResponse,
    TaskExecutionStatus,
    ValidateExecutionRequest,
)
from services.activity_service import activity_service
from services.task_execution_service import get_task_execution_service
from services.platform_audit_service import platform_audit_service, AuditEventType
from services import heartbeat_service, idempotency_service

logger = logging.getLogger(__name__)


def _get_internal_secret() -> str:
    """Get the internal API shared secret."""
    from config import SECRET_KEY
    return os.getenv("INTERNAL_API_SECRET") or SECRET_KEY


def _internal_secret_valid(request: Request) -> bool:
    """True iff a valid ``X-Internal-Secret`` header is present (the trusted
    backend / scheduler path). Constant-time compare; missing/empty ⇒ False."""
    secret = _get_internal_secret()
    provided = request.headers.get("X-Internal-Secret", "")
    return bool(provided) and hmac.compare_digest(provided, secret)


async def verify_internal_secret(request: Request):
    """
    Dependency to verify internal API shared secret (C-003).

    Checks the X-Internal-Secret header against the configured secret. This is
    the blanket gate for every backend-only internal endpoint. The two pull
    seams (``pull_router`` below) deliberately do NOT use it — they additionally
    accept the calling agent's OWN scoped MCP key, so the master secret never has
    to reach an agent container.
    """
    if not _internal_secret_valid(request):
        logger.warning(f"Internal API request rejected: invalid or missing X-Internal-Secret from {request.client.host}")
        raise HTTPException(
            status_code=403,
            detail="Invalid or missing internal API secret"
        )


router = APIRouter(
    prefix="/api/internal",
    tags=["internal"],
    dependencies=[Depends(verify_internal_secret)],
)

# Pull / work-stealing seams (#1081 Phase 2) live on their OWN router with NO
# blanket internal-secret dependency, so each can accept EITHER the internal
# secret (trusted backend) OR the calling agent's own agent-scoped MCP key. The
# master internal secret is therefore never required by — and never injected
# into — a pilot agent (least-privilege, #307/#1159). A compromised pilot can
# only ever claim/report for ITSELF, never reach the other /api/internal/*
# endpoints (which stay strictly internal-secret gated on ``router`` above).
pull_router = APIRouter(prefix="/api/internal", tags=["internal"])

_PULL_AUTH_DENIED = (
    "Pull seam requires a valid X-Internal-Secret or the agent's own agent-scoped MCP key"
)


def _validated_agent_key(request: Request) -> Optional[Dict]:
    """Validate a ``Bearer`` MCP key WITHOUT amplifying usage (the pull poll is
    high-frequency, like the heartbeat — #307). Returns the validation dict, or
    None when there is no Bearer token / the key is invalid."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None
    return db.validate_mcp_api_key(auth_header[7:], track_usage=False)


def _pull_authorized(request: Request, agent_name: str) -> bool:
    """Dual-auth predicate for a pull seam bound to ``agent_name``: the trusted
    backend (valid internal secret) OR the agent's OWN agent-scoped MCP key,
    reusing ``heartbeat_service.authorize_heartbeat`` (the same "an agent may act
    only on itself" rule that gates the heartbeat + #1083 callback). A
    user/system/mismatched key ⇒ False (caller raises 403)."""
    if _internal_secret_valid(request):
        return True
    # #1081 B1: the agent-key (worker) path is additionally gated on the pilot
    # allowlist — the single on/off switch is a CONSUMER backstop, not only a
    # producer-injection gate. A de-piloted agent whose worker is still running
    # (stale baked env / in-flight poll before recreate) is refused here even
    # with a valid scoped key, so rollback takes effect on backend restart rather
    # than only after a container recreate. The trusted-backend (internal-secret)
    # path is unchanged.
    from services.pull_pilot import is_pull_pilot_agent
    if not is_pull_pilot_agent(agent_name):
        return False
    return heartbeat_service.authorize_heartbeat(_validated_agent_key(request), agent_name)


@router.get("/health")
async def internal_health():
    """Internal health check for agent containers."""
    return {"status": "ok"}


# =============================================================================
# Pull / work-stealing coordination (#1081 Phase 2)
# =============================================================================
#
# Two internal seams for the pull coordination model
# (MESSAGE_ENVELOPE_SCHEMA.md §3), on ``pull_router`` with DUAL auth (see
# ``_pull_authorized``): a valid X-Internal-Secret OR the calling agent's own
# agent-scoped MCP key. This keeps the master internal secret out of agent
# containers (#307/#1159) — a compromised pilot can only ever claim/report for
# ITSELF. All policy lives in services/pull_coordination_service.py; the atomic
# claim + CAS terminal write live in db/schedules.py (Invariant #1).


@pull_router.get("/next-task")
async def internal_next_task(request: Request, agent_name: str, worker_id: str):
    """Atomically claim the oldest queued task for a worker (§3.1).

    Auth (dual): a valid ``X-Internal-Secret`` (trusted backend) OR the agent's
    own agent-scoped MCP key bound to ``agent_name`` — an agent may only claim
    its OWN queue. A user/system/mismatched scoped key → 403.

    Query params: ``agent_name`` + ``worker_id``. On success returns the §3.1
    claim response (envelope frame + lease metadata). When the queue is empty it
    returns the §3.2 empty-claim control response (200 ``{"envelope": null}`` —
    the doc leaves 204-vs-empty-200 an implementation choice; empty-200 is
    chosen so a drained poll and a real claim are one unambiguous 200 shape).
    """
    if not _pull_authorized(request, agent_name):
        raise HTTPException(status_code=403, detail=_PULL_AUTH_DENIED)

    from services import pull_coordination_service

    claim = pull_coordination_service.claim_next_task(agent_name, worker_id)
    if claim is None:
        # §3.2 — no work available.
        return {"envelope": None}
    return claim


@pull_router.post("/tasks/{execution_id}/result")
async def internal_task_result(
    request: Request,
    execution_id: str,
    payload: PullTaskResultRequest,
    idempotency_key: Optional[str] = Header(None),
):
    """CAS-apply a worker's terminal result (§3.3 → §3.4).

    Auth (dual): a valid ``X-Internal-Secret`` (trusted backend) OR the agent's
    own agent-scoped MCP key that OWNS the target execution (its bound
    ``agent_name`` must equal the execution's) — an agent may only report results
    for its OWN executions. This ownership gate is IN ADDITION to the
    ``claim_token`` CAS below (which independently blocks a stale/wrong-token
    write). A user/system/non-agent key → 403; an unknown OR not-owned execution
    → a **uniform 404** (self-uniform enumeration-safety, Invariant #186 — a
    404/403 split would leak the existence of another agent's execution id).

    The result payload (``reply`` §2.4) + the ``claim_token`` from the §3.1 claim
    response. The terminal write is a single atomic compare-and-set carrying BOTH
    the status precondition (#1082 status-as-projection) and the ``claim_token``
    match, so a stale / duplicate / wrong-token POST can never clobber a terminal
    row. ``Idempotency-Key`` is accepted for trigger-boundary consistency
    (Invariant #18) but dedup is the CAS itself — we do not double-implement a
    second dedup layer.

    Responses (§3.4): 200 ``{applied: true}`` · 200 ``{replayed: true}`` (an
    authoritative terminal already exists — never double-applied) · 404 (unknown
    execution) · 409 (row not claimable under this token).
    """
    # Dual-auth. The trusted-backend path (internal secret) skips the ownership
    # check; the agent path requires the scoped key to OWN this execution.
    if not _internal_secret_valid(request):
        key = _validated_agent_key(request)
        if not (key and key.get("scope") == "agent"):
            raise HTTPException(status_code=403, detail=_PULL_AUTH_DENIED)
        execution = db.get_execution(execution_id)
        if execution is None or not heartbeat_service.authorize_heartbeat(
            key, execution.agent_name
        ):
            # Uniform 404 for BOTH a missing AND a not-owned execution — never a
            # 404/403 split, which would leak the existence of another agent's
            # execution id (self-uniform enumeration-safety, Invariant #186).
            # Mirrors the #1083 callback (routers/agents.py::agent_execution_result).
            raise HTTPException(status_code=404, detail="Execution not found")

    from services import pull_coordination_service

    outcome = pull_coordination_service.apply_task_result(
        execution_id,
        payload.claim_token,
        status=payload.status,
        content=payload.content,
        error_code=payload.error_code,
        cost=payload.cost,
        tokens=payload.tokens,
        session_id=payload.session_id,
        execution_log=payload.execution_log,
        metadata=payload.metadata,
    )

    if outcome.kind == "applied":
        return {"applied": True, "status": outcome.status}
    if outcome.kind == "replayed":
        return {"replayed": True, "status": outcome.status}
    if outcome.kind == "not_found":
        raise HTTPException(status_code=404, detail="Execution not found")
    # conflict — row left `claimed`/RUNNING under a different (or no) token.
    raise HTTPException(
        status_code=409,
        detail="Execution not claimable under this claim_token (stale or wrong worker)",
    )


# ---------------------------------------------------------------------------
# Scheduler pre-check (#454, SCHED-COND-001)
# ---------------------------------------------------------------------------


@router.post("/agents/{agent_name}/pre-check")
async def internal_agent_pre_check(agent_name: str):
    """Run the agent's optional pre-check hook (SCHED-COND-001 / #454).

    Thin passthrough — all logic lives in
    ``services/pre_check_service.py`` (Invariant #1: Router → Service
    → DB). See that module for the full contract.
    """
    from services.pre_check_service import run_pre_check, AgentNotFound

    try:
        return await run_pre_check(agent_name)
    except AgentNotFound:
        raise HTTPException(status_code=404, detail="Agent not found")


@router.get("/agents/{agent_name}/sync-health-status")
async def internal_agent_sync_health(agent_name: str):
    """#389: lightweight read used by the dedicated scheduler before dispatching.

    Returns both the per-agent `freeze_schedules_if_sync_failing` flag and
    whether the current sync state would trip it. The scheduler multiplies
    the two to decide whether to skip the fire.
    """
    from database import db as _db
    freeze_flag = _db.get_freeze_schedules_if_sync_failing(agent_name)
    state = _db.get_sync_state(agent_name) or {}
    failing = (
        state.get("last_sync_status") == "failed"
        and (state.get("consecutive_failures") or 0) >= 3
    )
    return {
        "agent_name": agent_name,
        "freeze_schedules_if_sync_failing": bool(freeze_flag),
        "sync_failing": bool(failing),
        "should_freeze": bool(freeze_flag and failing),
        "consecutive_failures": state.get("consecutive_failures") or 0,
    }


# =============================================================================
# Activity Tracking Endpoints (for dedicated scheduler)
# =============================================================================


@router.post("/activities/track")
async def track_activity(request: ActivityTrackRequest):
    """
    Track the start of a new activity.

    Called by the dedicated scheduler when a cron-triggered execution starts.
    Creates an activity record and broadcasts via WebSocket.

    Returns:
        activity_id: UUID of the created activity
    """
    try:
        # Map string to ActivityType enum
        activity_type_map = {
            "schedule_start": ActivityType.SCHEDULE_START,
            "schedule_end": ActivityType.SCHEDULE_END,
            "chat_start": ActivityType.CHAT_START,
            "chat_end": ActivityType.CHAT_END,
            "agent_collaboration": ActivityType.AGENT_COLLABORATION,
        }

        activity_type = activity_type_map.get(request.activity_type)
        if not activity_type:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid activity_type: {request.activity_type}"
            )

        activity_id = await activity_service.track_activity(
            agent_name=request.agent_name,
            activity_type=activity_type,
            user_id=request.user_id,
            triggered_by=request.triggered_by,
            related_execution_id=request.related_execution_id,
            details=request.details
        )

        logger.info(f"Activity tracked: {activity_id} for agent {request.agent_name} ({request.activity_type})")

        return {
            "activity_id": activity_id,
            "agent_name": request.agent_name,
            "activity_type": request.activity_type
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to track activity: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/activities/{activity_id}/complete")
async def complete_activity(activity_id: str, request: ActivityCompleteRequest):
    """
    Mark an activity as completed or failed.

    Called by the dedicated scheduler when an execution completes.
    Updates the activity record and broadcasts via WebSocket.
    """
    try:
        success = await activity_service.complete_activity(
            activity_id=activity_id,
            status=request.status,
            details=request.details,
            error=request.error
        )

        if not success:
            raise HTTPException(
                status_code=404,
                detail=f"Activity not found: {activity_id}"
            )

        logger.info(f"Activity completed: {activity_id} ({request.status})")

        return {
            "activity_id": activity_id,
            "status": request.status,
            "completed": True
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to complete activity: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Task Execution Endpoint (for dedicated scheduler)
# =============================================================================


def _schedule_context_from(request: "InternalTaskExecutionRequest") -> Optional[Dict]:
    """Build the schedule_context dict passed to TaskExecutionService, or None."""
    if not (request.schedule_name or request.schedule_cron or request.schedule_next_run):
        return None
    return {
        "name": request.schedule_name,
        "cron": request.schedule_cron,
        "next_run": request.schedule_next_run,
    }


@router.post("/execute-task")
async def execute_task_internal(
    request: InternalTaskExecutionRequest,
    idempotency_key: Optional[str] = Header(None),
):
    """
    Execute a task via the unified TaskExecutionService.

    Called by the dedicated scheduler for cron-triggered and manually-triggered
    schedule executions. Routes through the same code path as authenticated
    /task and public chat endpoints, ensuring consistent slot management,
    activity tracking, credential sanitization, and dashboard visibility.

    The scheduler creates the execution record before calling this endpoint
    and passes the execution_id so the service skips record creation.

    When async_mode=True (SCHED-ASYNC-001), the endpoint spawns a background
    task and returns immediately with {"status": "accepted"}. The scheduler
    then polls the DB for completion instead of holding the HTTP connection.
    """
    # #748: warming-up gate. Until startup orphan-recovery finishes, refuse new
    # task starts so we don't ZADD a capacity slot for an execution row that
    # recovery is about to flip to FAILED (which would leak the slot until
    # TTL). The scheduler treats 503 as transient and retries.
    from services.cleanup_service import is_startup_recovery_complete
    if not is_startup_recovery_complete():
        raise HTTPException(
            status_code=503,
            detail="Backend warming up — startup execution recovery still in progress. Retry.",
        )

    task_service = get_task_execution_service()

    # RELIABILITY-006 (#525): idempotency gate. The scheduler sends a
    # deterministic key (sched:{execution_id}); a network blip + resend of the
    # same dispatch resolves to the same key and is short-circuited here instead
    # of double-dispatching. Optional header — absent → no dedup.
    idem = idempotency_service.begin(
        idempotency_service.make_agent_scope(request.agent_name), idempotency_key
    )
    if idem.replay:
        await platform_audit_service.log(
            event_type=AuditEventType.EXECUTION,
            event_action="idempotent_replay",
            source="scheduler",
            target_type="agent",
            target_id=request.agent_name,
            endpoint="/api/internal/execute-task",
            details={
                "idempotency_key": idempotency_key,
                "execution_id": idem.execution_id,
                "in_flight": idem.in_flight,
            },
        )
        if idem.snapshot is not None:
            return idem.snapshot
        # Still in flight (or no snapshot stored) — acknowledge without
        # re-dispatching; the scheduler polls the DB by execution_id.
        return {
            "status": "accepted",
            "execution_id": idem.execution_id or request.execution_id,
            "async_mode": bool(request.async_mode),
            "idempotent_replay": True,
        }
    idempotency_service.attach_execution(idem, request.execution_id)

    # Audit schedule-triggered execution (source=scheduler, actor=system)
    if request.triggered_by == "schedule":
        await platform_audit_service.log(
            event_type=AuditEventType.EXECUTION,
            event_action="schedule_triggered",
            source="scheduler",
            target_type="agent",
            target_id=request.agent_name,
            endpoint="/api/internal/execute-task",
            details={
                "execution_id": request.execution_id,
                "schedule_id": getattr(request, "schedule_id", None),
                "schedule_name": getattr(request, "schedule_name", None),
                "async_mode": bool(request.async_mode),
                "attempt": request.attempt,
            },
        )

    if request.async_mode:
        # Fire-and-forget: spawn background task, return immediately
        asyncio.create_task(_execute_task_internal_background(
            task_service, request
        ))
        accepted = {
            "status": "accepted",
            "execution_id": request.execution_id,
            "async_mode": True,
        }
        # Mark the claim completed with the accepted ack so a resend replays it
        # rather than re-dispatching (the background task owns the real result).
        idempotency_service.complete(idem, request.execution_id, accepted)
        return accepted

    # Synchronous mode (default, backward compatible)
    try:
        result = await task_service.execute_task(
            agent_name=request.agent_name,
            message=request.message,
            triggered_by=request.triggered_by,
            model=request.model,
            timeout_seconds=request.timeout_seconds,
            allowed_tools=request.allowed_tools,
            execution_id=request.execution_id,
            schedule_context=_schedule_context_from(request),
            attempt=request.attempt,
        )

        result_payload = {
            "execution_id": result.execution_id,
            "status": result.status,
            "response": result.response,
            "cost": result.cost,
            "context_used": result.context_used,
            "context_max": result.context_max,
            "session_id": result.session_id,
            "error": result.error,
        }
        idempotency_service.complete(idem, result.execution_id, result_payload)
        return result_payload

    except Exception as e:
        logger.error(f"Internal task execution failed for {request.agent_name}: {e}")
        idempotency_service.fail(idem)
        raise HTTPException(status_code=500, detail=str(e))


async def _execute_task_internal_background(task_service, request: InternalTaskExecutionRequest):
    """
    Background coroutine for async task execution (SCHED-ASYNC-001).

    TaskExecutionService handles all lifecycle: slot acquisition, activity
    tracking, DB updates, and cleanup. This wrapper logs outcomes and ensures
    execution status is updated on any uncaught exception (fixes issue #90).
    """
    try:
        result = await task_service.execute_task(
            agent_name=request.agent_name,
            message=request.message,
            triggered_by=request.triggered_by,
            model=request.model,
            timeout_seconds=request.timeout_seconds,
            allowed_tools=request.allowed_tools,
            execution_id=request.execution_id,
            schedule_context=_schedule_context_from(request),
            attempt=request.attempt,
        )
        logger.info(
            f"Async task completed for {request.agent_name}: "
            f"status={result.status}, execution_id={result.execution_id}"
        )
    except asyncio.CancelledError:
        # Python 3.11+: CancelledError is BaseException, bypasses except Exception.
        # On backend shutdown, in-flight background tasks are cancelled; close the
        # record synchronously so cleanup_service doesn't inflate duration (#767).
        if request.execution_id:
            try:
                existing = db.get_execution(request.execution_id)
                if existing and existing.status not in (
                    TaskExecutionStatus.SUCCESS,
                    TaskExecutionStatus.FAILED,
                    TaskExecutionStatus.CANCELLED,
                ):
                    won = db.update_execution_status(
                        execution_id=request.execution_id,
                        status=TaskExecutionStatus.FAILED,
                        error="Execution cancelled (backend shutdown)",
                    )
                    logger.info(f"Updated execution {request.execution_id} to FAILED on cancel")
                    # #1804: the second backend-shutdown terminal writer (the
                    # first is task_execution_service's own CancelledError
                    # handler). Both wrote the execution terminal and left the
                    # paired activity open — and because the row is now `failed`,
                    # startup recovery never revisits it, so nothing but the
                    # 120-minute duration-fabricating backstop ever closed it.
                    # No activity_id in scope here; the helper looks it up.
                    if won:
                        await activity_service.close_execution_activity(
                            request.execution_id,
                            TaskExecutionStatus.FAILED,
                            error="Execution cancelled (backend shutdown)",
                        )
            except Exception as db_err:
                logger.error(f"Failed to update execution status on cancel: {db_err}")
        raise

    except Exception as e:
        # If an exception escapes TaskExecutionService, ensure execution is marked failed
        # to prevent stuck 'running' status (fixes issue #90)
        error_msg = f"Background execution failed: {e}"
        logger.error(
            f"Async task failed for {request.agent_name}: {e}"
        )
        if request.execution_id:
            try:
                existing = db.get_execution(request.execution_id)
                if existing and existing.status not in (
                    TaskExecutionStatus.SUCCESS,
                    TaskExecutionStatus.FAILED,
                    TaskExecutionStatus.CANCELLED,
                ):
                    db.update_execution_status(
                        execution_id=request.execution_id,
                        status=TaskExecutionStatus.FAILED,
                        error=error_msg,
                    )
                    logger.info(f"Updated execution {request.execution_id} to FAILED")
            except Exception as db_err:
                logger.error(f"Failed to update execution status: {db_err}")


# =============================================================================
# Validation Endpoints (VALIDATE-001)
# =============================================================================


@router.post("/validate-execution")
async def validate_execution(request: ValidateExecutionRequest):
    """Trigger validation for a completed execution.

    Called by the scheduler service after a successful execution
    when validation is enabled for the schedule.

    Returns:
        dict with validation status and result.
    """
    from services.validation_service import get_validation_service

    logger.info(
        f"Received validation request for execution {request.execution_id} "
        f"on agent '{request.agent_name}'"
    )

    validation_service = get_validation_service()

    # Run validation in background to not block the scheduler
    asyncio.create_task(
        _run_validation_background(
            validation_service=validation_service,
            execution_id=request.execution_id,
            agent_name=request.agent_name,
            schedule_id=request.schedule_id,
            original_message=request.original_message,
            execution_response=request.execution_response,
            custom_prompt=request.custom_prompt,
            timeout_seconds=request.timeout_seconds,
        )
    )

    return {
        "status": "accepted",
        "message": f"Validation triggered for execution {request.execution_id}",
    }


async def _run_validation_background(
    validation_service,
    execution_id: str,
    agent_name: str,
    schedule_id: str,
    original_message: str,
    execution_response: str,
    custom_prompt: str = None,
    timeout_seconds: int = 120,
):
    """Run validation in background.

    This allows the internal endpoint to return immediately while
    validation runs asynchronously.
    """
    try:
        result = await validation_service.validate_execution(
            execution_id=execution_id,
            agent_name=agent_name,
            schedule_id=schedule_id,
            original_message=original_message,
            execution_response=execution_response,
            custom_prompt=custom_prompt,
            timeout_seconds=timeout_seconds,
        )
        logger.info(
            f"Validation completed for execution {execution_id}: "
            f"status={result.status.value}, summary={result.summary}"
        )
    except Exception as e:
        logger.error(f"Validation failed for execution {execution_id}: {e}")


# =============================================================================
# Audit Logging Endpoint (SEC-001 Phase 3 — MCP server integration)
# =============================================================================


@router.post("/audit")
async def log_audit_entry(request: InternalAuditRequest):
    """
    Log an audit entry from the MCP server (SEC-001 Phase 3).

    Called by the MCP server after each tool execution to record
    tool calls with full MCP auth context (key_id, scope, agent_name).
    Uses the internal shared-secret auth (C-003), not JWT.
    """
    try:
        event_type_map = {e.value: e for e in AuditEventType}
        event_type = event_type_map.get(request.event_type)
        if not event_type:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid event_type: {request.event_type}"
            )

        event_id = await platform_audit_service.log(
            event_type=event_type,
            event_action=request.event_action,
            source=request.source,
            mcp_key_id=request.mcp_key_id,
            mcp_key_name=request.mcp_key_name,
            mcp_scope=request.mcp_scope,
            actor_agent_name=request.actor_agent_name,
            actor_email=request.actor_email,
            target_type=request.target_type,
            target_id=request.target_id,
            request_id=request.request_id,
            details=request.details,
        )

        return {"event_id": event_id, "status": "logged"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to log audit entry: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Agent Shared Files (outbound — FILES-001 Step 3)
# =============================================================================

@router.post("/agent-files/share", response_model=ShareFileResponse)
async def agent_files_share(payload: ShareFileRequest):
    """
    Mint a public download URL for a file the agent wrote to its publish dir.

    Authentication: X-Internal-Secret (already enforced by router dependency).
    Agent identity: carried by `payload.agent_name`. The agent server is
    responsible for passing its own name here — same trust model as
    /internal/execute-task (forging requires the internal secret).
    """
    from services.agent_shared_files_service import create_share

    result = await create_share(
        agent_name=payload.agent_name,
        filename=payload.filename,
        display_name=payload.display_name,
        expires_in=payload.expires_in,
        created_by=payload.agent_name,
    )
    return ShareFileResponse(**result)


# ---------------------------------------------------------------------------
# MCP-exposed agents poll endpoint (#846)
# ---------------------------------------------------------------------------


@router.get("/mcp-exposed-agents")
async def mcp_exposed_agents():
    """Authoritative list of agents to expose as dedicated MCP tools (#846).

    Polled by the Trinity MCP server (over the existing X-Internal-Secret path)
    every ~20s. The backend is the single source of truth for the deterministic,
    collision-free ``tool_name`` per exposed agent (computed over the full set),
    so restarts/replicas of the MCP server agree and there is no client-side
    slug split-brain.

    ``description`` is intentionally **name-only** (see ``build_tool_description``):
    the dedicated tool's description is advertised globally to every non-connector
    MCP session, so it must not carry per-agent metadata (the agent's
    ``trinity.template`` label was a cross-tenant leak + injection surface, #846).
    """
    from services.agent_service.mcp_tool_names import (
        compute_tool_names,
        build_tool_description,
    )

    exposed = db.get_mcp_exposed_agents()
    names = [a["agent_name"] for a in exposed]
    tool_names = compute_tool_names(names)

    return {
        "agents": [
            {
                "agent_name": name,
                "tool_name": tool_names[name],
                "description": build_tool_description(name),
            }
            for name in names
        ]
    }
