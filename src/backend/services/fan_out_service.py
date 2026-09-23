"""
Fan-Out Service — Parallel task dispatch and result collection (FANOUT-001),
DB-joined since #2524.

Dispatches N independent tasks to an agent in parallel and reports the batch
aggregate. Each subtask follows the standard TaskExecutionService path so all
executions appear on the dashboard with full observability (cost, tokens, logs).

Why the aggregate is a query now (#2524)
----------------------------------------
It used to be a `dict[task_id, FanOutTaskResult]` built inside one `asyncio`
`gather` from `execute_task`'s return values. Under `PULL_MODE_PILOT_AGENTS`
`execute_task` returns as soon as the row is on the durable queue and the turn
runs later, in the agent's worker — so a collector built on the return value
reads an empty result for every subtask. That is why `fan_out` sat in the
stranded half of `pull_pilot.PULL_REACHABLE_TRIGGERS` (#2048).

So the batch lives on `schedule_executions`: every subtask row carries
`fan_out_id` plus the caller's own `fan_out_task_id` (#2524's column), and the
sync aggregate is rebuilt from those rows by `build_aggregate`. A queued
subtask is waited on through `sync_waiter.wait_for_fan_out_batch` — the "sync
edge adapter" of #1081 Phase 4 — which the terminal fan-out wakes once the last
row of the batch is terminal. The poll surface for an `async_mode` batch (or a
caller whose gateway gave up) is #2670's `GET /{name}/fan-out/{fan_out_id}`.

Row lifecycle: created at slot grant, never up front
----------------------------------------------------
A subtask's row is created INSIDE the semaphore, immediately before its
`execute_task` call — as it was before #2524. A row created up front and left
to wait behind the semaphore is a hidden queue that every recovery path
misreads:

* as `RUNNING`, it matches the #106 no-session sweep (60s), the watchdog's
  orphan reconcile (absent from the agent, no live dispatcher marker) and the
  stale sweep — all anchored at `started_at`, i.e. at admission (#2433/#2435) —
  so the undispatched tail is bulk-FAILed, the join wakes the caller with a
  fabricated aggregate, and the turns still run and bill afterwards;
* as `QUEUED`, it is claimable by `claim_next_queued` — the backlog drain and
  the pull workers — while `_dispatch_all` also dispatches it: a double run.

The cost is that a batch is not fully visible until its last subtask has been
granted a slot: a status poll mid-batch sees only the rows dispatched so far,
and a backend restart loses the not-yet-dispatched tail (in-process dispatcher,
no startup recovery). #2670's receipt already treats its `execution_ids` as
"evidence, never a manifest" for the same reason. Under pull the semaphore
self-releases in milliseconds, so every row exists almost immediately.

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

⚠️ **Contract change.** On deadline, a still-open subtask — including one still
waiting for a slot — now reports `status="running"`, not `failed`. The batch
still reports `status="deadline_exceeded"`, so a caller that branches on the
batch status is unaffected; a caller that treats every non-`completed` subtask
as failed will now see a third value. After a deadline the status endpoint is
the source of truth, not the returned aggregate.

**With no caller deadline the wait covers the whole batch.** Previously an
unbounded `gather`; now `ceil(N / effective_concurrency) × execution_timeout +
buffer` (see `_wait_budget`) — the longest a batch of individually-bounded
subtasks can take at that concurrency, so a batch that used to return complete
results still does.
"""

import asyncio
import logging
import math
import secrets
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from database import db
from models import TaskExecutionStatus
from services.sync_waiter import (
    signal_fan_out_batch,
    wait_for_fan_out_batch,
)
from services.task_execution_service import get_task_execution_service

logger = logging.getLogger(__name__)


# Per-subtask bound when the agent's timeout cannot be read. Mirrors the
# scheduler's `_POLL_DEADLINE_WHEN_NULL` reasoning: the per-agent
# `execution_timeout_seconds` is the real bound on a subtask.
_WAIT_BUDGET_FALLBACK_S = 7200
_WAIT_BUDGET_BUFFER_S = 120

# Strong references to spawned dispatch tasks — asyncio holds only a weak one, so
# an un-referenced task can be collected mid-flight and silently strand a batch.
_inflight_batches: "set[asyncio.Task[Any]]" = set()


def _spawn(coro) -> Optional["asyncio.Task[Any]"]:
    try:
        task = asyncio.create_task(coro)
    except RuntimeError as exc:  # no running loop — nothing can dispatch
        coro.close()
        logger.error("[FanOut] dispatch not spawned, no running event loop: %s", exc)
        return None
    _inflight_batches.add(task)
    task.add_done_callback(_inflight_batches.discard)
    return task


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
    status: str           # "completed" | "deadline_exceeded" | "accepted"
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
    agent_name: str,
    fan_out_id: str,
    order: List[str],
    *,
    status: Optional[str] = None,
    dispatch_finished: bool = True,
    error_codes: Optional[Dict[str, str]] = None,
) -> FanOutResult:
    """Rebuild the sync caller's aggregate from the batch's rows (#2524).

    Results come back in *order* — the caller's task-id order, which the
    dispatching request still holds — not the rows' order.

    A task with no row is either still waiting for a slot (the dispatch is still
    running, so it reports `running`) or its row could not be created (the
    dispatch finished, so it reports `failed`).

    *error_codes* are the `TaskExecutionErrorCode`s `execute_task` returned on
    the push path. `schedule_executions` has no column for them, so they exist
    only in the dispatching process: a pull subtask, or a batch read back through
    the status endpoint, carries none.
    """
    error_codes = error_codes or {}
    by_task: Dict[str, FanOutTaskResult] = {}
    for row in db.get_fan_out_executions(agent_name, fan_out_id):
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
            error_code=error_codes.get(task_id) if task_status == "failed" else None,
            execution_id=row.get("id"),
            cost=row.get("cost"),
            context_used=row.get("context_used"),
            duration_ms=row.get("duration_ms"),
        )

    def _missing(task_id: str) -> FanOutTaskResult:
        if not dispatch_finished:
            return FanOutTaskResult(id=task_id, status="running")
        return FanOutTaskResult(
            id=task_id,
            status="failed",
            error="No execution row was created for this subtask",
            error_code=error_codes.get(task_id, "agent_error"),
        )

    ordered = [by_task.get(task_id) or _missing(task_id) for task_id in order]
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
    """Wake a sync fan-out caller once no row of its batch is open (#2524).

    The join, called from the terminal fan-out
    (`event_dispatch_service.spawn_task_terminal_event`, the wrapper every
    CAS-won terminal writer already goes through). Two indexed reads on a
    terminal that belongs to a batch, and one PK read on every other terminal in
    the fleet — a fan-out row is identified by `fan_out_id` on its own row.

    "No row open" is not "batch complete" while subtasks are still waiting for a
    slot and have no row yet. That is safe because the sync caller registers its
    waiter only after every subtask has been dispatched (`FanOutService.execute`)
    — a signal before then finds no waiter and is a no-op.

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
    logger.info("[FanOut] %s has no open rows — waking any sync caller", fan_out_id)
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
        # #2670: called once with the batch id, before the first dispatch.
        on_started: Optional[Callable[[str], None]] = None,
        # #2806: stamped on every subtask row; captured by the router at
        # request time. None for a non-agent principal (a root).
        chain_depth: Optional[int] = None,
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
                None, a budget covering the whole batch is derived
                (`_wait_budget`). Reaching it does not stop the subtasks
                (#2524) — see the module docstring.
            model / system_prompt / allowed_tools: subtask overrides.
            async_mode: return `{fan_out_id, status="accepted"}` immediately,
                without waiting. The caller polls
                `GET /api/agents/{name}/fan-out/{fan_out_id}` (#2670).
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

        deadline_desc = f"{timeout_seconds}s" if timeout_seconds is not None else "per-batch"
        logger.info(
            f"[FanOut] Starting {fan_out_id}: {len(tasks)} tasks on '{agent_name}' "
            f"(concurrency={max_concurrency}, deadline={deadline_desc}, "
            f"async={async_mode})"
        )

        error_codes: Dict[str, str] = {}
        dispatch = _spawn(self._dispatch_all(
            fan_out_id=fan_out_id,
            agent_name=agent_name,
            tasks=tasks,
            error_codes=error_codes,
            max_concurrency=max_concurrency,
            model=model,
            system_prompt=system_prompt,
            allowed_tools=allowed_tools,
            source_user_id=source_user_id,
            source_user_email=source_user_email,
            source_agent_name=source_agent_name or agent_name,
            source_mcp_key_id=source_mcp_key_id,
            source_mcp_key_name=source_mcp_key_name,
            chain_depth=chain_depth,
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

        budget = self._wait_budget(agent_name, timeout_seconds, len(tasks), max_concurrency)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + budget
        status = None
        try:
            # 1. Every subtask dispatched. On push that is every turn finished;
            #    under pull it is every row queued. Shielded: the deadline stops
            #    the wait, never the dispatch.
            if dispatch is not None:
                await asyncio.wait_for(asyncio.shield(dispatch), budget)
            # 2. Every row terminal — only now does "no open row" mean "done".
            await wait_for_fan_out_batch(fan_out_id, max(0.0, deadline - loop.time()))
        except asyncio.TimeoutError:
            status = "deadline_exceeded"
            logger.warning(
                f"[FanOut] {fan_out_id} deadline exceeded after {budget:.0f}s; "
                f"still-open subtasks keep running and their terminals land on the rows"
            )

        result = build_aggregate(
            agent_name,
            fan_out_id,
            [t.id for t in tasks],
            status=status,
            dispatch_finished=dispatch is None or dispatch.done(),
            error_codes=error_codes,
        )
        logger.info(
            f"[FanOut] {fan_out_id} finished: {result.completed}/{result.total} completed, "
            f"{result.failed} failed"
        )
        return result

    # ---- Internals ----------------------------------------------------------

    @staticmethod
    def _wait_budget(
        agent_name: str,
        timeout_seconds: Optional[int],
        task_count: int,
        max_concurrency: int,
    ) -> float:
        """How long the sync caller waits on the batch.

        An explicit `timeout_seconds` is honoured verbatim. Otherwise the bound
        is the batch's own worst case: `ceil(N / c)` waves of one
        `execution_timeout_seconds` each, plus a buffer, where `c` is the lower
        of `max_concurrency` and the agent's `max_parallel_tasks` — the real
        parallelism on push (the excess fails fast with `CapacityFull`) and the
        worker pool under pull. A one-subtask bound here would return
        `deadline_exceeded` on a batch that used to return complete results
        (#2524 review).
        """
        if timeout_seconds is not None:
            return float(timeout_seconds)
        try:
            per_run = float(db.get_execution_timeout(agent_name))
            parallel = int(db.get_max_parallel_tasks(agent_name) or max_concurrency)
        except Exception:  # noqa: BLE001 — a config read must not break dispatch
            per_run, parallel = float(_WAIT_BUDGET_FALLBACK_S), 1
        effective = max(1, min(max_concurrency, parallel))
        waves = math.ceil(max(1, task_count) / effective)
        return waves * per_run + _WAIT_BUDGET_BUFFER_S

    async def _dispatch_all(
        self,
        *,
        fan_out_id: str,
        agent_name: str,
        tasks: List[FanOutTaskInput],
        error_codes: Dict[str, str],
        max_concurrency: int,
        model: Optional[str],
        system_prompt: Optional[str],
        allowed_tools: Optional[list],
        source_user_id: Optional[int],
        source_user_email: Optional[str],
        source_agent_name: Optional[str],
        source_mcp_key_id: Optional[str],
        source_mcp_key_name: Optional[str],
        chain_depth: Optional[int] = None,
    ) -> None:
        """Dispatch every subtask, paced by the semaphore. The batch's outcome
        is read from the rows, not from here — this only records the
        `error_code`s the rows cannot hold."""
        task_service = get_task_execution_service()
        semaphore = asyncio.Semaphore(max_concurrency)
        # SUB-004: `execute_task` snapshots the subscription only on rows it
        # creates itself; these are created here, so snapshot it here.
        try:
            subscription_id = db.get_agent_subscription_id(agent_name)
        except Exception:  # noqa: BLE001 — best-effort, as in execute_task
            subscription_id = None

        async def run_subtask(task: FanOutTaskInput) -> None:
            async with semaphore:
                # Created at slot grant, not before — see "Row lifecycle" in
                # the module docstring.
                execution = db.create_task_execution(
                    agent_name=agent_name,
                    message=task.message,
                    triggered_by="fan_out",
                    source_user_id=source_user_id,
                    source_user_email=source_user_email,
                    source_agent_name=source_agent_name,
                    source_mcp_key_id=source_mcp_key_id,
                    source_mcp_key_name=source_mcp_key_name,
                    model_used=model,
                    fan_out_id=fan_out_id,
                    fan_out_task_id=task.id,
                    subscription_id=subscription_id,
                    chain_depth=chain_depth,
                )
                if execution is None:
                    logger.error(
                        "[FanOut] %s could not create the execution row for subtask '%s'",
                        fan_out_id, task.id,
                    )
                    return
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
                        execution_id=execution.id,
                        fan_out_id=fan_out_id,
                        subscription_id=subscription_id,
                    )
                except Exception as exc:  # noqa: BLE001 — must not strand the batch
                    logger.error(
                        f"[FanOut] {fan_out_id} subtask '{task.id}' raised: {exc}"
                    )
                    await self._fail_subtask(agent_name, execution.id, exc)
                    return
                code = getattr(result, "error_code", None)
                if code is not None:
                    error_codes[task.id] = getattr(code, "value", code)
                # `execute_task`'s fast-fail returns (capacity, circuit-open,
                # ephemeral budget) write a FAILED row without reaching a
                # CAS-won terminal writer, so no terminal event fires for them.
                # Nudge the join directly; it is idempotent.
                if getattr(result, "status", None) != TaskExecutionStatus.QUEUED:
                    await join_fan_out_on_terminal(execution.id)

        await asyncio.gather(*(run_subtask(t) for t in tasks), return_exceptions=True)

    @staticmethod
    async def _fail_subtask(agent_name: str, execution_id: str, exc: BaseException) -> None:
        """Close a subtask row a raised dispatch left open, and nudge the join.

        A terminal writer like any other: side effects only on a won CAS
        (#1804 activity close, #1578 `agent.task.failed` emit — which also
        carries the join through `spawn_task_terminal_event`).
        """
        try:
            from services import event_dispatch_service
            from services.activity_service import activity_service
            from services.runtime_secret_scrub import get_staged_values, scrub_text

            # ent#279: exception text is free text on the way into
            # `schedule_executions.error`, and a staged runtime secret can ride
            # an exception message. Scrub before the write, never after.
            error_text = f"{type(exc).__name__}: {exc}"
            staged = get_staged_values()
            if staged:
                error_text = scrub_text(staged, error_text)

            won = db.update_execution_status(
                execution_id=execution_id,
                status=TaskExecutionStatus.FAILED,
                error=error_text,
            )
            if won:
                # #1804: a terminal writer owns closing the paired dispatch activity.
                await activity_service.close_execution_activity(
                    execution_id, TaskExecutionStatus.FAILED, error=error_text,
                )
                event_dispatch_service.spawn_task_terminal_event(
                    agent_name,
                    execution_id,
                    terminal_status=TaskExecutionStatus.FAILED,
                    summary_or_error=error_text,
                )
        except Exception:  # noqa: BLE001
            logger.exception("[FanOut] could not fail execution %s", execution_id)
        await join_fan_out_on_terminal(execution_id)


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
            task_id=r.get("fan_out_task_id"),
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
