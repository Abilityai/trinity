"""
Fan-Out Service — Parallel task dispatch and result collection (FANOUT-001),
DB-joined since #2524.

Dispatches N independent tasks to an agent in parallel and reports the batch
aggregate. Each subtask follows the standard TaskExecutionService path so all
executions appear on the dashboard with full observability (cost, tokens, logs).

Why the aggregate is a query now (#2524)
----------------------------------------
It used to be a `dict[task_id, FanOutTaskResult]` built inside one `asyncio`
`gather`, which meant the batch existed only for as long as the HTTP request
that started it. Two things follow from that, and both are why `fan_out` sat in
the stranded half of `pull_pilot.PULL_REACHABLE_TRIGGERS` (#2048):

* **A pull-claimed subtask returns nothing to collect.** Under
  `PULL_MODE_PILOT_AGENTS`, `execute_task` returns as soon as the row is on the
  durable queue; the turn runs later, in the agent's worker. A collector built
  on the return value therefore reads an empty result for every subtask.
* **Nothing could answer about the batch afterwards.** No `async_mode`, no
  status endpoint, and a disconnect lost the batch entirely.

So the batch lives on `schedule_executions`: every subtask row carries
`fan_out_id` plus the caller's own `fan_out_task_id` (#2524's column), and the
aggregate is rebuilt from those rows by `build_aggregate`. The sync caller waits
on `sync_waiter.wait_for_fan_out_batch` — the "sync edge adapter" of #1081 Phase
4 — which the terminal fan-out wakes once the last row of the batch is terminal.

What did NOT change, and why
----------------------------
**`max_concurrency` keeps its meaning, and needed no branch.** The semaphore
still wraps the `execute_task` call. On the push path that call spans the whole
turn, so the semaphore paces dispatch exactly as before — deleting it would fire
N concurrent dispatches at an agent whose `max_parallel_tasks` is 3 and turn the
excess into `CapacityFull` failures. Under pull the same call returns in
milliseconds (the row is queued, not run), so the semaphore self-releases and
real concurrency becomes the agent's worker pool — #1081 Phase 5's "capacity
becomes physical", arrived at by construction rather than by a flag.

**The outer deadline bounds the WAIT, not the work.** It used to wrap the
`gather` in `asyncio.timeout`, cancelling in-flight subtasks and reporting them
`failed`/`timeout`. That is not available for a queued or claimed row — it is not
the backend's to cancel — and it was always half-illusory on push too
(cancelling the HTTP call abandons the request; the agent container keeps running
the turn, and bills for it).

⚠️ **Contract change.** On deadline, a still-open subtask now reports
`status="running"`, not `failed`. The batch still reports
`status="deadline_exceeded"`, so a caller that branches on the batch status is
unaffected; a caller that treats every non-`completed` subtask as failed will now
see a third value. Reporting `failed` would have been a lie the moment the
subtask succeeded — which it usually does, since nothing stopped it. After a
deadline the **status endpoint is the source of truth**, not the returned
aggregate.
"""

import asyncio
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

from database import db
from models import TaskExecutionStatus
from services.sync_waiter import (
    signal_fan_out_batch,
    wait_for_fan_out_batch,
)
from services.task_execution_service import get_task_execution_service

logger = logging.getLogger(__name__)


# Wait budget when the caller sets no outer deadline. Mirrors the scheduler's
# `_POLL_DEADLINE_WHEN_NULL` reasoning: the per-agent `execution_timeout_seconds`
# is the real bound on a subtask, so the batch wait only has to outlast it.
_WAIT_BUDGET_FALLBACK_S = 7200
_WAIT_BUDGET_BUFFER_S = 120

# Strong references to spawned dispatch tasks — asyncio holds only a weak one, so
# an un-referenced task can be collected mid-flight and silently strand a batch.
_inflight_batches: "set[asyncio.Task[Any]]" = set()


def _spawn(coro) -> None:
    try:
        task = asyncio.create_task(coro)
    except RuntimeError as exc:  # no running loop — nothing can dispatch
        coro.close()
        logger.error("[FanOut] dispatch not spawned, no running event loop: %s", exc)
        return
    _inflight_batches.add(task)
    task.add_done_callback(_inflight_batches.discard)


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
    status: str           # "completed" | "failed" | "running"
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
    status: str           # "completed" | "deadline_exceeded" | "accepted" | "running"
    total: int
    completed: int
    failed: int
    results: List[FanOutTaskResult]


# Row statuses that mean the subtask has not finished. Mirrors
# `db.count_fan_out_open`'s predicate — `queued` (waiting for a pull worker) and
# `pending_retry` are deliberately NOT terminal.
_OPEN_STATUSES = frozenset({
    TaskExecutionStatus.QUEUED,
    TaskExecutionStatus.RUNNING,
    TaskExecutionStatus.PENDING_RETRY,
})


def _row_status(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value)


def build_aggregate(
    fan_out_id: str,
    *,
    status: Optional[str] = None,
    order: Optional[List[str]] = None,
) -> FanOutResult:
    """Rebuild a batch's aggregate from its execution rows (#2524).

    *order* is the caller's task-id order, available on the synchronous path
    where the request still holds the input list. Without it (the status
    endpoint, which answers long after that request is gone) rows come back in
    the DB's order — deliberately NOT `started_at`, because a subtask that spills
    onto the durable queue has its `started_at` rewritten to its queue time, so
    ordering by it would reorder the batch the moment pull is enabled. Callers
    match subtasks by `id`; batch order is not part of the contract there.
    """
    rows = db.list_fan_out_executions(fan_out_id)
    by_task: Dict[str, FanOutTaskResult] = {}
    for row in rows:
        row_status = _row_status(row.get("status"))
        if row_status == TaskExecutionStatus.SUCCESS:
            task_status = "completed"
        elif row_status in _OPEN_STATUSES:
            task_status = "running"
        else:
            task_status = "failed"
        task_id = row.get("fan_out_task_id") or row.get("id")
        by_task[task_id] = FanOutTaskResult(
            id=task_id,
            status=task_status,
            response=row.get("response") if task_status == "completed" else None,
            error=row.get("error") if task_status != "completed" else None,
            execution_id=row.get("id"),
            cost=row.get("cost"),
            context_used=row.get("context_used"),
            duration_ms=row.get("duration_ms"),
        )

    if order:
        ordered = [
            by_task.get(task_id) or FanOutTaskResult(
                id=task_id,
                status="failed",
                error="No execution row was created for this subtask",
                error_code="agent_error",
            )
            for task_id in order
        ]
    else:
        ordered = list(by_task.values())

    return FanOutResult(
        fan_out_id=fan_out_id,
        status=status or (
            "running" if any(r.status == "running" for r in ordered) else "completed"
        ),
        total=len(ordered),
        completed=sum(1 for r in ordered if r.status == "completed"),
        failed=sum(1 for r in ordered if r.status == "failed"),
        results=ordered,
    )


async def join_fan_out_on_terminal(execution_id: Optional[str]) -> bool:
    """Wake a sync fan-out caller once the LAST row of its batch is terminal (#2524).

    The join, called from the terminal fan-out
    (`event_dispatch_service.spawn_task_terminal_event`, the wrapper every
    CAS-won terminal writer already goes through). Two indexed reads on a
    terminal that belongs to a batch, and one PK read on every other terminal in
    the fleet — a fan-out row is identified by `fan_out_id` on its own row.

    Idempotent by construction: it only signals, and signalling an absent or
    already-resolved waiter is a no-op. Returns True when it woke a batch.
    """
    if not execution_id:
        return False
    execution = db.get_execution(execution_id)
    fan_out_id = getattr(execution, "fan_out_id", None) if execution else None
    if not fan_out_id:
        return False
    if db.count_fan_out_open(fan_out_id) > 0:
        return False
    logger.info("[FanOut] %s complete — waking any sync caller", fan_out_id)
    signal_fan_out_batch(fan_out_id)
    return True


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
        async_mode: bool = False,
        # Origin tracking (passed through to TaskExecutionService)
        source_user_id: Optional[int] = None,
        source_user_email: Optional[str] = None,
        source_agent_name: Optional[str] = None,
        source_mcp_key_id: Optional[str] = None,
        source_mcp_key_name: Optional[str] = None,
    ) -> FanOutResult:
        """Dispatch tasks in parallel and (unless *async_mode*) collect results.

        Args:
            agent_name: Target agent (typically self).
            tasks: List of tasks to execute.
            max_concurrency: Max concurrent subtask DISPATCHES. Still the real
                concurrency cap on the push path, where a dispatch spans the
                whole turn; under pull a dispatch returns as soon as the row is
                queued, so the agent's worker pool becomes the cap instead.
            timeout_seconds: Optional deadline for WAITING on the batch. When
                None, a budget derived from the agent's configured
                `execution_timeout_seconds` is used. Reaching it does not stop
                the subtasks (#2524) — see the module docstring.
            model / system_prompt / allowed_tools: subtask overrides.
            async_mode: return `{fan_out_id, status="accepted"}` as soon as the
                rows exist, without waiting. The caller polls `get_status`.
            source_*: Origin tracking fields forwarded to execution records.

        Returns:
            FanOutResult with per-task results and aggregate counts.
        """
        fan_out_id = f"fo_{secrets.token_urlsafe(12)}"

        # Rows first, dispatch second. The batch has to be discoverable by
        # `fan_out_id` BEFORE any subtask can reach a terminal, or the join
        # could fire against a partially-created batch and wake the caller early.
        execution_ids: Dict[str, str] = {}
        for task in tasks:
            execution = db.create_task_execution(
                agent_name=agent_name,
                message=task.message,
                triggered_by="fan_out",
                source_user_id=source_user_id,
                source_user_email=source_user_email,
                source_agent_name=source_agent_name or agent_name,
                source_mcp_key_id=source_mcp_key_id,
                source_mcp_key_name=source_mcp_key_name,
                model_used=model,
                fan_out_id=fan_out_id,
                fan_out_task_id=task.id,
            )
            if execution is None:
                logger.error(
                    "[FanOut] %s could not create the execution row for subtask '%s'",
                    fan_out_id, task.id,
                )
                continue
            execution_ids[task.id] = execution.id

        deadline_desc = f"{timeout_seconds}s" if timeout_seconds is not None else "per-agent"
        logger.info(
            f"[FanOut] Starting {fan_out_id}: {len(tasks)} tasks on '{agent_name}' "
            f"(concurrency={max_concurrency}, deadline={deadline_desc}, "
            f"async={async_mode})"
        )

        _spawn(self._dispatch_all(
            fan_out_id=fan_out_id,
            agent_name=agent_name,
            tasks=tasks,
            execution_ids=execution_ids,
            max_concurrency=max_concurrency,
            model=model,
            system_prompt=system_prompt,
            allowed_tools=allowed_tools,
            source_user_id=source_user_id,
            source_user_email=source_user_email,
            source_agent_name=source_agent_name or agent_name,
            source_mcp_key_id=source_mcp_key_id,
            source_mcp_key_name=source_mcp_key_name,
        ))

        if async_mode:
            return FanOutResult(
                fan_out_id=fan_out_id,
                status="accepted",
                total=len(tasks),
                completed=0,
                failed=0,
                results=[],
            )

        order = [t.id for t in tasks]
        try:
            await wait_for_fan_out_batch(fan_out_id, self._wait_budget(agent_name, timeout_seconds))
            status = None
        except asyncio.TimeoutError:
            status = "deadline_exceeded"
            logger.warning(
                f"[FanOut] {fan_out_id} deadline exceeded after {timeout_seconds}s; "
                f"still-open subtasks keep running and their terminals land on the rows"
            )

        result = build_aggregate(fan_out_id, status=status, order=order)
        logger.info(
            f"[FanOut] {fan_out_id} finished: {result.completed}/{result.total} completed, "
            f"{result.failed} failed"
        )
        return result

    def get_status(self, fan_out_id: str) -> Optional[FanOutResult]:
        """Aggregate for a batch, or None when no row carries that id (#2524)."""
        result = build_aggregate(fan_out_id)
        return result if result.total else None

    @staticmethod
    def batch_belongs_to(fan_out_id: str, agent_name: str) -> bool:
        """True when every row of the batch belongs to *agent_name* (#2524).

        The status endpoint is reached through an agent the caller is already
        authorized for, so without this a valid `fan_out_id` belonging to a
        DIFFERENT agent would be readable through any agent the caller owns —
        the id is opaque but it is not a secret (it is returned to whoever
        started the batch, and appears on execution rows).
        """
        rows = db.list_fan_out_executions(fan_out_id)
        return bool(rows) and all(r.get("agent_name") == agent_name for r in rows)

    # ---- Internals ----------------------------------------------------------

    @staticmethod
    def _wait_budget(agent_name: str, timeout_seconds: Optional[int]) -> float:
        """How long the sync caller waits on the batch.

        An explicit `timeout_seconds` is honoured verbatim. Otherwise the bound
        is the agent's own per-execution timeout plus a buffer — the same
        reasoning as the scheduler's `_POLL_DEADLINE_WHEN_NULL`: the subtask is
        already bounded, so the batch wait only has to outlast it.
        """
        if timeout_seconds is not None:
            return float(timeout_seconds)
        try:
            per_run = float(db.get_execution_timeout(agent_name))
        except Exception:  # noqa: BLE001 — a config read must not break dispatch
            per_run = float(_WAIT_BUDGET_FALLBACK_S)
        return per_run + _WAIT_BUDGET_BUFFER_S

    async def _dispatch_all(
        self,
        *,
        fan_out_id: str,
        agent_name: str,
        tasks: List[FanOutTaskInput],
        execution_ids: Dict[str, str],
        max_concurrency: int,
        model: Optional[str],
        system_prompt: Optional[str],
        allowed_tools: Optional[list],
        source_user_id: Optional[int],
        source_user_email: Optional[str],
        source_agent_name: Optional[str],
        source_mcp_key_id: Optional[str],
        source_mcp_key_name: Optional[str],
    ) -> None:
        """Dispatch every subtask, paced by the semaphore. Never awaited by a
        caller — the batch's outcome is read from the rows, not from here."""
        task_service = get_task_execution_service()
        semaphore = asyncio.Semaphore(max_concurrency)

        async def run_subtask(task: FanOutTaskInput) -> None:
            execution_id = execution_ids.get(task.id)
            if execution_id is None:
                return  # row creation failed; already logged and counted
            async with semaphore:
                try:
                    # Per-subtask timeout: pass None so TaskExecutionService
                    # resolves the target agent's configured
                    # execution_timeout_seconds (TIMEOUT-001). The optional
                    # overall `timeout_seconds` governs how long the CALLER
                    # waits, not the individual task ceiling.
                    result = await task_service.execute_task(
                        agent_name=agent_name,
                        message=task.message,
                        triggered_by="fan_out",
                        source_user_id=source_user_id,
                        source_user_email=source_user_email,
                        source_agent_name=source_agent_name,
                        source_mcp_key_id=source_mcp_key_id,
                        source_mcp_key_name=source_mcp_key_name,
                        model=model,
                        timeout_seconds=None,
                        system_prompt=system_prompt,
                        allowed_tools=allowed_tools,
                        execution_id=execution_id,
                        fan_out_id=fan_out_id,
                    )
                except Exception as exc:  # noqa: BLE001 — must not strand the batch
                    logger.error(
                        f"[FanOut] {fan_out_id} subtask '{task.id}' raised: {exc}"
                    )
                    await self._fail_subtask(execution_id, exc)
                    return
                # `execute_task`'s fast-fail returns (capacity, circuit-open,
                # ephemeral budget) write a FAILED row without reaching a
                # CAS-won terminal writer, so no terminal event fires for them.
                # Nudge the join directly; it is idempotent.
                if getattr(result, "status", None) != TaskExecutionStatus.QUEUED:
                    await join_fan_out_on_terminal(execution_id)

        await asyncio.gather(*(run_subtask(t) for t in tasks), return_exceptions=True)

    @staticmethod
    async def _fail_subtask(execution_id: str, exc: BaseException) -> None:
        """Close a subtask row a raised dispatch left open, and nudge the join."""
        try:
            from services.activity_service import activity_service
            from services.runtime_secret_scrub import get_staged_values, scrub_text

            # ent#279: exception text is free text on the way into
            # `schedule_executions.error`, and a staged runtime secret can ride
            # an exception message. Scrub before the write, never after.
            error_text = f"{type(exc).__name__}: {exc}"
            staged = get_staged_values()
            if staged:
                error_text = scrub_text(staged, error_text)

            db.update_execution_status(
                execution_id=execution_id,
                status=TaskExecutionStatus.FAILED,
                error=error_text,
            )
            # #1804: a terminal writer owns closing the paired dispatch activity.
            await activity_service.close_execution_activity(
                execution_id, TaskExecutionStatus.FAILED, error=error_text,
            )
        except Exception:  # noqa: BLE001
            logger.exception("[FanOut] could not fail execution %s", execution_id)
        await join_fan_out_on_terminal(execution_id)


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
