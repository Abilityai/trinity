"""
Loop Service — sequential bounded task execution (#740).

Runs `agent_loops` rows. Each loop is an in-process ``asyncio.Task`` that
invokes ``task_execution_service.execute_task()`` sequentially up to
``max_runs`` times, optionally exiting early when the agent's response
contains ``stop_signal``.

Cancellation:
  - Cooperative. ``stop_loop()`` flips ``should_stop`` on the in-memory
    handle; the runner checks the flag between iterations and finalizes
    with ``stop_reason="user_stopped"``. The currently-executing
    iteration is NOT cancelled — sequential, fire-and-disconnect.

Restart recovery:
  - On startup, ``cleanup_service`` calls
    ``db.mark_orphan_loops_interrupted()`` which flips any leftover
    ``running``/``queued`` rows to ``interrupted``. Loops do not
    auto-resume.

Template substitution:
  - ``{{run}}`` → 1-indexed iteration number.
  - ``{{previous_response}}`` → trailing 2000 chars of the previous
    iteration's response (empty string on iteration 1).
"""

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from database import db
from services.task_execution_service import (
    TaskExecutionResult,
    get_task_execution_service,
)

logger = logging.getLogger(__name__)


# Truncate previous_response to its trailing 2000 chars per spec
PREV_RESPONSE_TRUNCATE_CHARS = 2000

# WebSocket manager injected from main.py
_websocket_manager = None


def set_websocket_manager(manager):
    global _websocket_manager
    _websocket_manager = manager


@dataclass
class _LoopHandle:
    """In-process handle for an active loop."""
    loop_id: str
    agent_name: str
    task: asyncio.Task
    should_stop: bool = False
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)


def _fingerprint(text: Optional[str]) -> str:
    """SHA-256 of normalized response text for no-progress detection (#1157).

    Normalizes by collapsing every whitespace run to a single space and
    stripping (`" ".join(text.split())`). This preserves word boundaries so
    `"foo bar"` and `"foobar"` do NOT collide, while `"hi"` and `"hi  \\n"`
    do. Empty / None / whitespace-only all normalize to `""` — a repeated
    empty response IS a doom loop and counts like any other fingerprint.
    """
    return hashlib.sha256(" ".join((text or "").split()).encode()).hexdigest()


def _render_template(template: str, run_number: int, previous_response: Optional[str]) -> str:
    """Apply `{{run}}` and `{{previous_response}}` substitutions."""
    prev = (previous_response or "")[-PREV_RESPONSE_TRUNCATE_CHARS:]
    return template.replace("{{run}}", str(run_number)).replace(
        "{{previous_response}}", prev
    )


async def _broadcast(event: dict) -> None:
    if _websocket_manager is None:
        return
    try:
        await _websocket_manager.broadcast(json.dumps(event))
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning(f"[Loop] WebSocket broadcast failed: {exc}")


class LoopService:
    """Coordinates sequential agent loop execution."""

    def __init__(self) -> None:
        self._handles: dict[str, _LoopHandle] = {}
        self._lock = asyncio.Lock()

    # ---- Public API ---------------------------------------------------------

    async def start_loop(
        self,
        *,
        agent_name: str,
        message_template: str,
        max_runs: int,
        stop_signal: Optional[str] = None,
        delay_seconds: int = 0,
        timeout_per_run: Optional[int] = None,
        max_duration_seconds: Optional[int] = None,
        no_progress_threshold: Optional[int] = None,
        model: Optional[str] = None,
        allowed_tools: Optional[list] = None,
        started_by_user_id: Optional[int] = None,
        started_by_user_email: Optional[str] = None,
        source_agent_name: Optional[str] = None,
        source_mcp_key_id: Optional[str] = None,
        source_mcp_key_name: Optional[str] = None,
    ) -> dict:
        """Create the loop row + spawn the background runner.

        Returns the loop row snapshot as a dict.
        """
        loop_row = db.create_loop(
            agent_name=agent_name,
            message_template=message_template,
            max_runs=max_runs,
            stop_signal=stop_signal,
            delay_seconds=delay_seconds,
            timeout_per_run=timeout_per_run,
            max_duration_seconds=max_duration_seconds,
            no_progress_threshold=no_progress_threshold,
            model=model,
            allowed_tools=allowed_tools,
            started_by_user_id=started_by_user_id,
            started_by_user_email=started_by_user_email,
            source_agent_name=source_agent_name,
            source_mcp_key_id=source_mcp_key_id,
            source_mcp_key_name=source_mcp_key_name,
        )
        loop_id = loop_row["id"]
        task = asyncio.create_task(self._run(loop_id))
        async with self._lock:
            self._handles[loop_id] = _LoopHandle(
                loop_id=loop_id,
                agent_name=agent_name,
                task=task,
            )
        return loop_row

    async def stop_loop(self, loop_id: str) -> str:
        """Request graceful stop.

        Returns:
            "stopping" — flag set, current iteration will finish then exit.
            "already_done" — loop is already in a terminal state.
        """
        loop = db.get_loop(loop_id)
        if loop is None:
            return "not_found"
        if loop["status"] in {"completed", "stopped", "failed", "interrupted"}:
            return "already_done"

        async with self._lock:
            handle = self._handles.get(loop_id)
        if handle is None:
            # Loop is non-terminal in DB but no in-process handle:
            # backend restarted, runner gone. Finalize as interrupted so
            # state is consistent.
            db.finalize_loop(
                loop_id, status="interrupted", stop_reason="interrupted",
            )
            return "already_done"

        handle.should_stop = True
        return "stopping"

    def get_status(self, loop_id: str) -> Optional[dict]:
        """Return loop row + per-run summaries. ``None`` if loop unknown."""
        loop = db.get_loop(loop_id)
        if loop is None:
            return None
        runs = db.list_loop_runs(loop_id)
        return {**loop, "runs": runs}

    # ---- Runner -------------------------------------------------------------

    async def _run(self, loop_id: str) -> None:
        """Background coroutine: execute up to max_runs iterations."""
        loop = db.get_loop(loop_id)
        if loop is None:  # pragma: no cover — defensive
            logger.error(f"[Loop] _run invoked for unknown loop {loop_id}")
            return

        db.mark_loop_running(loop_id)
        task_service = get_task_execution_service()
        previous_response: Optional[str] = None
        runs_completed = 0
        terminal_status = "completed"
        stop_reason = "max_runs_reached"
        terminal_error: Optional[str] = None

        # #1156: optional wall-clock deadline measured from loop start
        # (≈ started_at, just stamped by mark_loop_running above). NULL/0
        # disables. Enforced only at iteration boundaries, so an in-flight
        # run is never killed mid-turn — overshoot is bounded by one
        # timeout_per_run.
        max_duration = loop.get("max_duration_seconds")
        deadline = (
            datetime.utcnow() + timedelta(seconds=max_duration)
            if max_duration else None
        )

        # #1157: no-progress / doom-loop detection. NULL/0 disables (back-compat
        # for in-flight loops). Fingerprint each successful run's response; stop
        # once `repeat_count` consecutive identical fingerprints reach the
        # threshold. Runner-local — no persistence.
        no_progress_threshold = loop.get("no_progress_threshold")
        last_fingerprint: Optional[str] = None
        repeat_count = 0

        try:
            for run_number in range(1, loop["max_runs"] + 1):
                # Cooperative stop check BEFORE starting the next iteration.
                async with self._lock:
                    handle = self._handles.get(loop_id)
                if handle is not None and handle.should_stop:
                    terminal_status = "stopped"
                    stop_reason = "user_stopped"
                    break

                # #1156: deadline check at the iteration boundary.
                if deadline is not None and datetime.utcnow() >= deadline:
                    terminal_status = "stopped"
                    stop_reason = "deadline_exceeded"
                    break

                rendered = _render_template(
                    loop["message_template"], run_number, previous_response,
                )

                run_id = db.start_loop_run(loop_id, run_number)
                run_start = datetime.utcnow()

                try:
                    result: TaskExecutionResult = await task_service.execute_task(
                        agent_name=loop["agent_name"],
                        message=rendered,
                        triggered_by="loop",
                        source_user_id=loop["started_by_user_id"],
                        source_user_email=loop["started_by_user_email"],
                        source_agent_name=loop["source_agent_name"],
                        source_mcp_key_id=loop["source_mcp_key_id"],
                        source_mcp_key_name=loop["source_mcp_key_name"],
                        model=loop["model"],
                        timeout_seconds=loop["timeout_per_run"],
                        allowed_tools=loop["allowed_tools"],
                        loop_id=loop_id,
                    )
                except Exception as exc:
                    elapsed_ms = int(
                        (datetime.utcnow() - run_start).total_seconds() * 1000
                    )
                    db.finalize_loop_run(
                        run_id,
                        status="failed",
                        response=None,
                        error=f"{type(exc).__name__}: {exc}",
                        cost=None,
                        duration_ms=elapsed_ms,
                    )
                    terminal_status = "failed"
                    stop_reason = "error"
                    terminal_error = f"Iteration {run_number}: {exc}"
                    logger.exception(
                        f"[Loop] {loop_id} iteration {run_number} raised; aborting loop"
                    )
                    break

                elapsed_ms = int(
                    (datetime.utcnow() - run_start).total_seconds() * 1000
                )

                if result.status == "success":
                    db.finalize_loop_run(
                        run_id,
                        status="completed",
                        response=result.response,
                        error=None,
                        cost=result.cost,
                        duration_ms=elapsed_ms,
                        execution_id=result.execution_id,
                    )
                    previous_response = result.response
                    runs_completed = run_number
                    db.update_loop_progress(
                        loop_id,
                        runs_completed=runs_completed,
                        last_response=result.response,
                    )

                    await _broadcast({
                        "type": "loop_run_completed",
                        "loop_id": loop_id,
                        "agent_name": loop["agent_name"],
                        "run_number": run_number,
                        "execution_id": result.execution_id,
                        "cost": result.cost,
                        "duration_ms": elapsed_ms,
                        "timestamp": datetime.utcnow().isoformat() + "Z",
                    })

                    # Stop-signal check — substring match on the full response.
                    # Placed BEFORE no-progress so stop_signal wins if both fire.
                    if loop["stop_signal"] and loop["stop_signal"] in (result.response or ""):
                        stop_reason = "stop_signal_matched"
                        break

                    # #1157: no-progress detection. Fingerprint the full
                    # response and track consecutive identical results.
                    if no_progress_threshold:
                        fp = _fingerprint(result.response)
                        if fp == last_fingerprint:
                            repeat_count += 1
                        else:
                            last_fingerprint = fp
                            repeat_count = 1
                        # A pending user-stop or passed deadline outranks
                        # no_progress — let the next iteration's top-of-loop
                        # checks finalize user_stopped/deadline_exceeded. An
                        # explicit Stop must never be relabeled "no progress".
                        deadline_passed = (
                            deadline is not None
                            and datetime.utcnow() >= deadline
                        )
                        if (
                            repeat_count >= no_progress_threshold
                            and not (handle is not None and handle.should_stop)
                            and not deadline_passed
                        ):
                            terminal_status = "stopped"
                            stop_reason = "no_progress"
                            break
                else:
                    db.finalize_loop_run(
                        run_id,
                        status="failed",
                        response=result.response,
                        error=result.error or "Unknown task failure",
                        cost=result.cost,
                        duration_ms=elapsed_ms,
                        execution_id=result.execution_id,
                    )
                    runs_completed = run_number
                    db.update_loop_progress(
                        loop_id,
                        runs_completed=runs_completed,
                        last_response=result.response,
                    )
                    terminal_status = "failed"
                    stop_reason = "error"
                    terminal_error = (
                        f"Iteration {run_number}: {result.error or 'task failed'}"
                    )
                    break

                # Inter-run delay — also a stop point.
                if loop["delay_seconds"] and run_number < loop["max_runs"]:
                    sleep_for = loop["delay_seconds"]
                    # #1156: never sleep past the deadline — cap the delay to
                    # the remaining budget; the next boundary check then stops
                    # the loop with deadline_exceeded.
                    if deadline is not None:
                        remaining = (deadline - datetime.utcnow()).total_seconds()
                        if remaining <= 0:
                            terminal_status = "stopped"
                            stop_reason = "deadline_exceeded"
                            break
                        sleep_for = min(sleep_for, remaining)
                    try:
                        await asyncio.sleep(sleep_for)
                    except asyncio.CancelledError:
                        terminal_status = "stopped"
                        stop_reason = "user_stopped"
                        break
        finally:
            db.finalize_loop(
                loop_id,
                status=terminal_status,
                stop_reason=stop_reason,
                error=terminal_error,
            )
            await _broadcast({
                "type": "loop_completed",
                "loop_id": loop_id,
                "agent_name": loop["agent_name"],
                "status": terminal_status,
                "stop_reason": stop_reason,
                "runs_completed": runs_completed,
                "timestamp": datetime.utcnow().isoformat() + "Z",
            })
            async with self._lock:
                self._handles.pop(loop_id, None)


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------

_loop_service: Optional[LoopService] = None


def get_loop_service() -> LoopService:
    global _loop_service
    if _loop_service is None:
        _loop_service = LoopService()
    return _loop_service
