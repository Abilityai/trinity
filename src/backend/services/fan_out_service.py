"""
Fan-Out Service — Parallel task dispatch and result collection (FANOUT-001).

Dispatches N independent tasks to an agent in parallel, throttled by a
configurable concurrency limit, and collects results with an overall deadline.

Each subtask follows the standard TaskExecutionService path so all executions
appear on the dashboard with full observability (cost, tokens, logs).
"""

import asyncio
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, List, Optional

from services.task_execution_service import (
    TaskExecutionResult,
    TaskExecutionErrorCode,
    get_task_execution_service,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class FanOutTaskInput:
    """A single task in a fan-out request."""
    id: str
    message: str


@dataclass
class FanOutTaskResult:
    """Result of a single fan-out subtask."""
    id: str
    status: str           # "completed" | "failed"
    response: Optional[str] = None
    error: Optional[str] = None
    error_code: Optional[str] = None
    execution_id: Optional[str] = None
    cost: Optional[float] = None
    context_used: Optional[int] = None
    duration_ms: Optional[int] = None


@dataclass
class FanOutResult:
    """Aggregated result of a fan-out operation."""
    fan_out_id: str
    status: str           # "completed" | "deadline_exceeded"
    total: int
    completed: int
    failed: int
    results: List[FanOutTaskResult]


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class FanOutService:
    """Coordinates parallel fan-out task dispatch and result collection."""

    async def execute(
        self,
        agent_name: str,
        tasks: List[FanOutTaskInput],
        max_concurrency: int = 3,
        timeout_seconds: Optional[int] = None,
        model: Optional[str] = None,
        system_prompt: Optional[str] = None,
        allowed_tools: Optional[list] = None,
        # Origin tracking (passed through to TaskExecutionService)
        source_user_id: Optional[int] = None,
        source_user_email: Optional[str] = None,
        source_agent_name: Optional[str] = None,
        source_mcp_key_id: Optional[str] = None,
        source_mcp_key_name: Optional[str] = None,
        # #2670: called once with the batch id, before the first dispatch.
        on_started: Optional[Callable[[str], None]] = None,
    ) -> FanOutResult:
        """
        Dispatch tasks in parallel and collect results.

        Args:
            agent_name: Target agent (typically self).
            tasks: List of tasks to execute.
            max_concurrency: Max concurrent subtasks (semaphore size).
            timeout_seconds: Optional overall deadline for the entire fan-out.
                When None, no outer deadline is applied — each sub-task is
                still bounded by the target agent's configured
                execution_timeout_seconds (TIMEOUT-001).
            model: Model override for subtasks.
            system_prompt: System prompt for subtasks.
            allowed_tools: Tool restrictions for subtasks.
            source_*: Origin tracking fields forwarded to execution records.

        Returns:
            FanOutResult with per-task results and aggregate counts.
        """
        fan_out_id = f"fo_{secrets.token_urlsafe(12)}"
        # #2670: hand the id to the caller the moment it exists, BEFORE any
        # subtask is dispatched. The router uses it to attach the batch id to
        # its idempotency claim, so a concurrent duplicate's 409 carries
        # something pollable instead of a null — and so a caller whose own HTTP
        # call dies mid-batch has an id recorded somewhere durable. Best-effort
        # by construction: a raising hook must not be able to fail a dispatch
        # that is otherwise fine.
        if on_started is not None:
            try:
                on_started(fan_out_id)
            except Exception:  # noqa: BLE001 — bookkeeping, never the batch
                logger.warning(
                    "[FanOut] %s on_started hook raised; continuing", fan_out_id,
                    exc_info=True,
                )
        task_service = get_task_execution_service()
        semaphore = asyncio.Semaphore(max_concurrency)
        # Safe for concurrent writes: asyncio is single-threaded, no preemption between awaits.
        results: dict[str, FanOutTaskResult] = {}

        deadline_desc = f"{timeout_seconds}s" if timeout_seconds is not None else "per-agent"
        logger.info(
            f"[FanOut] Starting {fan_out_id}: {len(tasks)} tasks on '{agent_name}' "
            f"(concurrency={max_concurrency}, deadline={deadline_desc})"
        )

        async def run_subtask(task: FanOutTaskInput) -> None:
            """Execute a single subtask, throttled by semaphore."""
            start = datetime.utcnow()
            async with semaphore:
                try:
                    # Per-subtask timeout: pass None so TaskExecutionService
                    # resolves the target agent's configured
                    # execution_timeout_seconds (TIMEOUT-001). The optional
                    # overall `timeout_seconds` parameter governs the outer
                    # fan-out deadline, not the individual task ceiling.
                    result = await task_service.execute_task(
                        agent_name=agent_name,
                        message=task.message,
                        triggered_by="fan_out",
                        source_user_id=source_user_id,
                        source_user_email=source_user_email,
                        source_agent_name=source_agent_name or agent_name,
                        source_mcp_key_id=source_mcp_key_id,
                        source_mcp_key_name=source_mcp_key_name,
                        model=model,
                        timeout_seconds=None,
                        system_prompt=system_prompt,
                        allowed_tools=allowed_tools,
                        fan_out_id=fan_out_id,
                    )
                    elapsed_ms = int((datetime.utcnow() - start).total_seconds() * 1000)

                    if result.status == "success":
                        results[task.id] = FanOutTaskResult(
                            id=task.id,
                            status="completed",
                            response=result.response,
                            execution_id=result.execution_id,
                            cost=result.cost,
                            context_used=result.context_used,
                            duration_ms=elapsed_ms,
                        )
                    else:
                        results[task.id] = FanOutTaskResult(
                            id=task.id,
                            status="failed",
                            error=result.error,
                            error_code=result.error_code.value if result.error_code else None,
                            execution_id=result.execution_id,
                            cost=result.cost,
                            duration_ms=elapsed_ms,
                        )
                except asyncio.CancelledError:
                    results[task.id] = FanOutTaskResult(
                        id=task.id,
                        status="failed",
                        error="Cancelled (deadline exceeded)",
                        error_code="timeout",
                    )
                except Exception as e:
                    elapsed_ms = int((datetime.utcnow() - start).total_seconds() * 1000)
                    logger.error(f"[FanOut] {fan_out_id} subtask '{task.id}' failed: {e}")
                    results[task.id] = FanOutTaskResult(
                        id=task.id,
                        status="failed",
                        error=str(e),
                        error_code="agent_error",
                        duration_ms=elapsed_ms,
                    )

        # Dispatch all tasks. When an overall deadline is set, wrap in
        # asyncio.timeout so slow subtasks get cancelled once the deadline
        # hits. Without a deadline, each subtask is still individually
        # bounded by the target agent's execution_timeout_seconds.
        # return_exceptions=True ensures all coroutines complete even if one
        # raises — individual failures are handled inside run_subtask.
        deadline_exceeded = False
        coroutines = [run_subtask(t) for t in tasks]

        try:
            if timeout_seconds is not None:
                async with asyncio.timeout(timeout_seconds):
                    await asyncio.gather(*coroutines, return_exceptions=True)
            else:
                await asyncio.gather(*coroutines, return_exceptions=True)
        except TimeoutError:
            deadline_exceeded = True
            logger.warning(f"[FanOut] {fan_out_id} deadline exceeded after {timeout_seconds}s")
            # Fill in results for tasks that didn't complete
            for task in tasks:
                if task.id not in results:
                    results[task.id] = FanOutTaskResult(
                        id=task.id,
                        status="failed",
                        error=f"Deadline exceeded ({timeout_seconds}s)",
                        error_code="timeout",
                    )

        # Build ordered results matching input order
        ordered_results = [results.get(t.id, FanOutTaskResult(
            id=t.id, status="failed", error="Unknown error",
        )) for t in tasks]

        completed_count = sum(1 for r in ordered_results if r.status == "completed")
        failed_count = sum(1 for r in ordered_results if r.status == "failed")

        logger.info(
            f"[FanOut] {fan_out_id} finished: {completed_count}/{len(tasks)} completed, "
            f"{failed_count} failed"
        )

        return FanOutResult(
            fan_out_id=fan_out_id,
            status="deadline_exceeded" if deadline_exceeded else "completed",
            total=len(tasks),
            completed=completed_count,
            failed=failed_count,
            results=ordered_results,
        )


# ---------------------------------------------------------------------------
# #2670 — reading a batch back out of its execution rows
# ---------------------------------------------------------------------------

# The three states an execution can still leave. `pending_retry` is the one that
# is easy to miss and the one that matters most here: a subtask awaiting a #271
# retry is neither done nor lost, and counting it as failed would tell a polling
# caller the batch is finished while a row is about to run again.
_NON_TERMINAL = frozenset({"queued", "running", "pending_retry"})


def build_fan_out_batch_status(
    agent_name: str, fan_out_id: str, rows: List[dict]
) -> "FanOutBatchStatus":
    """Fold a batch's execution rows into an aggregate (#2670).

    Pure — every input is already resolved by the caller, so the rule is
    testable without a database. It lives here rather than in the router because
    "what does this batch add up to" is a business question (Invariant #1).

    Per-task `status` is the EXECUTION status verbatim (`queued`, `running`,
    `success`, …), NOT the dispatch response's `completed`/`failed` pair. The
    dispatch response is written once a subtask has finished, so two values are
    all it can ever need; a poll of a live batch has to distinguish "waiting for
    a slot" from "running", and translating them into `failed` — which is what
    a two-value vocabulary forces — would report a healthy queued subtask as a
    failure. Same reason `FanOutBatchStatus` is a separate model.

    Batch `status` has four values and their ORDER is the rule: `running` while
    anything can still change, and only then a verdict. `completed` /
    `failed` / `partial` are the three ways a finished batch can land, and
    `partial` exists because "best-effort" is the fan-out's default policy — a
    batch where four of five succeeded is neither a success nor a failure, and
    calling it either loses the fact the caller needs.

    `deadline_exceeded` is deliberately absent. That is the DISPATCHER's verdict
    on its own outer deadline, held in memory by the call that timed out; it is
    not a property of any row, so this surface cannot observe it and does not
    invent it.
    """
    from models import FanOutBatchStatus, FanOutBatchTask

    tasks = [
        FanOutBatchTask(
            execution_id=r.get("id"),
            status=r.get("status") or "unknown",
            message=r.get("message"),
            response=r.get("response"),
            error=r.get("error"),
            cost=r.get("cost"),
            context_used=r.get("context_used"),
            duration_ms=r.get("duration_ms"),
            model_used=r.get("model_used"),
            started_at=r.get("started_at"),
            completed_at=r.get("completed_at"),
        )
        for r in rows
        if r.get("id")
    ]

    running = sum(1 for t in tasks if t.status in _NON_TERMINAL)
    completed = sum(1 for t in tasks if t.status == "success")
    failed = len(tasks) - running - completed

    if running:
        status = "running"
    elif failed == 0:
        status = "completed"
    elif completed == 0:
        status = "failed"
    else:
        status = "partial"

    return FanOutBatchStatus(
        agent_name=agent_name,
        fan_out_id=fan_out_id,
        status=status,
        total=len(tasks),
        completed=completed,
        failed=failed,
        running=running,
        results=tasks,
    )


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------

_fan_out_service: Optional[FanOutService] = None


def get_fan_out_service() -> FanOutService:
    """Get the global FanOutService instance."""
    global _fan_out_service
    if _fan_out_service is None:
        _fan_out_service = FanOutService()
    return _fan_out_service
