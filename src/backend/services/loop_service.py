"""
Loop Service — sequential bounded task execution (#740), terminal-driven (#2523).

A loop is `agent_loops` + one `agent_loop_runs` row per iteration. It advances
one iteration at a time: dispatch run N → run N's execution reaches a terminal →
evaluate the stop conditions → dispatch run N+1. **The DB row is the loop.**

Why this is not a `for` loop any more (#2523)
---------------------------------------------
It used to be: `start_loop` spawned an `asyncio.Task` running `_run(loop_id)`,
which iterated `for run_number in range(1, max_runs + 1)` and `await`ed
`execute_task` inside. That made two things impossible.

* **Pull.** `capacity_manager` can only hand a row to the durable queue when its
  producer asks for `overflow_policy="queue_persistent"`, and a queued row is
  claimed and run by the agent's worker *later* — it returns no
  `TaskExecutionResult` for a caller to read. A driver built on reading that
  result therefore cannot run on the queue at all, which is why `loop` was in
  `pull_pilot.PULL_REACHABLE_TRIGGERS`'s stranded set (#2048). Nothing outside
  the backend was ever blocked — `POST /api/agents/{name}/loops` has always
  returned `{loop_id}` immediately — so the only synchronous consumer was this
  module reading its own dispatch.
* **Surviving a restart.** The loop's whole state lived in that coroutine, so a
  backend restart lost it and `cleanup_service` flipped every in-flight loop to
  `interrupted`. Now the state is the row: a restart loses nothing, the
  in-flight execution's terminal (or its recovery) advances the loop, and
  `reconcile_after_restart` re-arms only the loops that have nothing in flight.

Everything the old runner kept in locals is either already persisted
(`last_response`, `runs_completed`, `failed_runs`) or derived from the run rows
(accumulated cost, consecutive failures, the #1157 no-progress fingerprints) —
see `_derive_state`. Only two things needed new columns: `stop_requested_at`
(was `_LoopHandle.should_stop`, which only worked in the process that started
the loop) and `next_run_at` (was `asyncio.sleep`).

Idempotency
-----------
Pull is at-least-once: the same terminal can arrive twice (a re-delivered lease,
a late callback racing the reaper). Every advance goes through
`db.claim_loop_advance`, a CAS on `runs_completed` from N-1 to N — the loser
returns without dispatching, so a duplicate terminal can never double-fire the
next iteration. That is also what makes it safe for `_run_and_advance` to call
the advance directly on a fast-fail return *and* for the terminal hook to call
it: whichever gets there first wins, exactly once.

Stop-condition precedence is preserved from the `for`-loop implementation:
    user_stopped > deadline_exceeded > budget_exhausted   (next-iteration gate)
    stop_signal_matched > no_progress                     (post-run gate)
with `max_runs_reached` ending the loop before any next-iteration gate runs, and
`completed_with_errors` (#1167) promoting only the natural-completion path.

Template substitution:
  - ``{{run}}`` → 1-indexed iteration number.
  - ``{{previous_response}}`` → trailing 2000 chars of the previous
    iteration's response (empty string on iteration 1).
"""

import asyncio
import hashlib
import json
import logging
import math
from datetime import datetime, timedelta
from typing import Any, Optional

from database import db
from services.runtime_secret_scrub import get_staged_values, scrub_text
from services.task_execution_service import get_task_execution_service
from utils.helpers import utc_now_iso

logger = logging.getLogger(__name__)


# Truncate previous_response to its trailing 2000 chars per spec
PREV_RESPONSE_TRUNCATE_CHARS = 2000

# WebSocket manager injected from main.py
_websocket_manager = None


def set_websocket_manager(manager):
    global _websocket_manager
    _websocket_manager = manager


# Strong references to spawned dispatch tasks. asyncio holds only a WEAK
# reference to a bare `create_task` result, so an un-referenced task can be
# garbage-collected mid-flight — here that would silently strand a loop between
# iterations. Mirrors `task_execution_service._spawn_bg`.
_inflight_dispatches: "set[asyncio.Task[Any]]" = set()


def _spawn(coro) -> None:
    try:
        task = asyncio.create_task(coro)
    except RuntimeError as exc:  # no running loop — nothing can dispatch
        coro.close()
        logger.error("[Loop] dispatch not spawned, no running event loop: %s", exc)
        return
    _inflight_dispatches.add(task)
    task.add_done_callback(_inflight_dispatches.discard)


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


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    """Parse a stored ISO-Z timestamp to a naive UTC datetime, or None.

    Every timestamp column is TEXT holding `utc_now_iso()` output (Invariant
    #16). Naive-UTC to match `datetime.utcnow()`, which the deadline arithmetic
    used before and still uses.
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "").replace("+00:00", ""))
    except (TypeError, ValueError):
        return None


class _DerivedState:
    """The runner locals, recomputed from the durable run rows (#2523).

    The old `_run` carried these across iterations in Python. Each is cheap to
    rebuild: `MAX_RUNS_LIMIT` is 100, so a loop has at most 100 run rows and the
    advance reads them once per iteration.
    """

    __slots__ = ("accumulated_cost", "failed_runs", "consecutive_failures",
                 "repeat_count", "last_fingerprint")

    def __init__(self, runs: list) -> None:
        self.accumulated_cost = 0.0
        self.failed_runs = 0
        self.consecutive_failures = 0
        self.repeat_count = 0
        self.last_fingerprint: Optional[str] = None

        for run in runs:
            status = run.get("status")
            if status == "completed":
                self.consecutive_failures = 0
                # #1155: only finite, positive costs count. A NaN/inf cost is
                # ignored so it can't poison the accumulator (NaN >= max_cost is
                # always False → the budget would never trip); NULL is
                # fail-open and counts as 0.
                cost = run.get("cost")
                if cost is not None and math.isfinite(cost) and cost > 0:
                    self.accumulated_cost += cost
                # #1157: the fingerprint chain advances on SUCCESS ONLY — a
                # tolerated failure between two identical successes does not
                # reset it, which is the pre-#2523 behaviour (the failure branch
                # never touched these locals).
                fingerprint = _fingerprint(run.get("response"))
                if fingerprint == self.last_fingerprint:
                    self.repeat_count += 1
                else:
                    self.last_fingerprint = fingerprint
                    self.repeat_count = 1
            elif status == "failed":
                self.failed_runs += 1
                self.consecutive_failures += 1


class LoopService:
    """Coordinates sequential agent loop execution (terminal-driven since #2523)."""

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
        max_cost_usd: Optional[float] = None,
        no_progress_threshold: Optional[int] = None,
        on_failure: str = "abort",
        max_consecutive_failures: int = 3,
        model: Optional[str] = None,
        allowed_tools: Optional[list] = None,
        started_by_user_id: Optional[int] = None,
        started_by_user_email: Optional[str] = None,
        source_agent_name: Optional[str] = None,
        source_mcp_key_id: Optional[str] = None,
        source_mcp_key_name: Optional[str] = None,
    ) -> dict:
        """Create the loop row and dispatch its first iteration.

        Returns the loop row snapshot as a dict. Unchanged contract: the caller
        gets `{loop_id, status}` back immediately and never waits for a run — it
        never did.
        """
        loop_row = db.create_loop(
            agent_name=agent_name,
            message_template=message_template,
            max_runs=max_runs,
            stop_signal=stop_signal,
            delay_seconds=delay_seconds,
            timeout_per_run=timeout_per_run,
            max_duration_seconds=max_duration_seconds,
            max_cost_usd=max_cost_usd,
            no_progress_threshold=no_progress_threshold,
            on_failure=on_failure,
            max_consecutive_failures=max_consecutive_failures,
            model=model,
            allowed_tools=allowed_tools,
            started_by_user_id=started_by_user_id,
            started_by_user_email=started_by_user_email,
            source_agent_name=source_agent_name,
            source_mcp_key_id=source_mcp_key_id,
            source_mcp_key_name=source_mcp_key_name,
        )
        loop_id = loop_row["id"]
        db.mark_loop_running(loop_id)
        # `started_at` is stamped by mark_loop_running and the #1156 deadline is
        # measured from it, so re-read rather than using the pre-stamp snapshot.
        loop = db.get_loop(loop_id) or loop_row
        await self._dispatch_run(loop, run_number=1)
        return db.get_loop(loop_id) or loop_row

    async def stop_loop(self, loop_id: str) -> str:
        """Request graceful stop.

        Returns:
            "stopping"     — recorded; the in-flight iteration finishes, then
                             the loop finalizes as `user_stopped`.
            "already_done" — loop is already in a terminal state.
            "not_found"    — no such loop.

        #2523: this used to flip a flag on an in-memory `_LoopHandle`, so it only
        worked in the process that started the loop — and when it found no
        handle (i.e. after a restart) it gave up and finalized the loop
        `interrupted`, because a loop with no runner was unrecoverable. Both of
        those are gone: the request is a column, and any worker can serve it.
        """
        loop = db.get_loop(loop_id)
        if loop is None:
            return "not_found"
        if not db.request_loop_stop(loop_id):
            return "already_done"

        # A loop parked on `next_run_at` has nothing in flight, so no terminal is
        # coming to notice the request — finalize it here. Claiming the park is a
        # CAS, so this cannot race the sweep into a double finalize.
        loop = db.get_loop(loop_id) or loop
        parked_at = loop.get("next_run_at")
        if parked_at and db.claim_due_loop(loop_id, parked_at):
            await self._finalize(loop, status="stopped", stop_reason="user_stopped")
        return "stopping"

    def get_status(self, loop_id: str) -> Optional[dict]:
        """Return loop row + per-run summaries. ``None`` if loop unknown."""
        loop = db.get_loop(loop_id)
        if loop is None:
            return None
        runs = db.list_loop_runs(loop_id)
        return {**loop, "runs": runs}

    # ---- Terminal-driven advance -------------------------------------------

    async def advance_on_terminal(self, execution_id: Optional[str]) -> bool:
        """Advance the loop that owns ``execution_id``, if any (#2523).

        The replacement for the body of the old `for` loop. Called from the
        terminal fan-out (`event_dispatch_service.spawn_task_terminal_event`,
        which every CAS-won terminal writer already goes through — push applier,
        pull sink, lease reaper, cleanup) and directly by `_run_and_advance` for
        the fast-fail returns that write a FAILED row without emitting a
        terminal event. Both paths are safe because `claim_loop_advance` is a
        CAS; whoever arrives first advances, the other is a no-op.

        Returns True when this call advanced the loop. Never raises — a loop
        bookkeeping fault must not affect an already-billed execution.
        """
        try:
            if not execution_id:
                return False
            # One indexed point read decides "is this a loop run?" for every
            # terminal in the fleet (idx_loop_runs_execution).
            run = db.get_loop_run_by_execution(execution_id)
            if run is None:
                return False
            if run.get("status") != "running":
                # Already finalized by an earlier delivery of this terminal.
                return False

            loop_id = run["loop_id"]
            loop = db.get_loop(loop_id)
            if loop is None:
                return False

            # The idempotency gate. Everything below it happens exactly once per
            # iteration, however many times the terminal is delivered.
            if not db.claim_loop_advance(loop_id, run["run_number"]):
                logger.debug(
                    "[Loop] %s run %s already advanced (duplicate terminal for %s)",
                    loop_id, run["run_number"], execution_id,
                )
                return False

            await self._close_run(loop, run, execution_id)
            return True
        except Exception:  # noqa: BLE001 — never break a terminal write
            logger.exception(
                "[Loop] advance failed for execution %s", execution_id
            )
            return False

    async def dispatch_due_loops(self, *, limit: int = 100) -> int:
        """Dispatch every loop whose `next_run_at` has arrived (#2523).

        The replacement for `asyncio.sleep(delay_seconds)` between iterations.
        Runs in every backend worker, so the claim is a CAS on the exact
        `next_run_at` read — one worker clears it and dispatches, the rest see
        nothing. Returns the number of loops dispatched.
        """
        dispatched = 0
        try:
            due = db.list_due_loops(utc_now_iso(), limit=limit)
        except Exception:  # noqa: BLE001 — a sweep read failure is not fatal
            logger.exception("[Loop] due-loop sweep read failed")
            return 0

        for loop in due:
            try:
                if not db.claim_due_loop(loop["id"], loop["next_run_at"]):
                    continue  # another worker took it
                # Re-evaluate rather than dispatching blind: a stop or a
                # deadline may have arrived while the loop sat parked.
                if await self._continue_or_finalize(loop["id"], parked=True):
                    dispatched += 1
            except Exception:  # noqa: BLE001 — one bad loop must not stall the sweep
                logger.exception("[Loop] due dispatch failed for %s", loop.get("id"))
        return dispatched

    async def reconcile_after_restart(self) -> int:
        """Re-arm loops that lost their dispatch to a backend restart (#2523).

        Replaces `db.mark_orphan_loops_interrupted()`, which flipped EVERY
        non-terminal loop to `interrupted` because its runner coroutine was
        gone. Nothing is interrupted now:

          * a loop already parked on `next_run_at` is left to the sweep;
          * an open run whose execution is still non-terminal is left alone —
            that execution's terminal (or `cleanup_service`'s recovery of it,
            which writes one) advances the loop;
          * an open run whose execution already went terminal is ADVANCED from
            it: the event was lost with the restart, and `runs_completed` has
            not moved, so re-arming would dispatch a second row for the same
            `run_number`;
          * a loop with no open run lost its dispatch and is re-armed by making
            it due now.

        Returns the number of loops this call moved (re-armed or advanced).
        """
        moved = 0
        try:
            loops = db.list_non_terminal_loops()
        except Exception:  # noqa: BLE001
            logger.exception("[Loop] restart reconcile read failed")
            return 0

        for loop in loops:
            try:
                if loop.get("next_run_at"):
                    continue  # parked — the sweep owns it
                open_run = self._open_run(loop["id"])
                if open_run is None:
                    # Nothing in flight and nothing parked: the dispatch was
                    # lost. Make it due now.
                    db.schedule_loop_next_run(loop["id"], utc_now_iso())
                    moved += 1
                    continue
                if self._execution_is_live(open_run.get("execution_id")):
                    continue  # its terminal will advance the loop
                # The run's execution is already terminal (or gone) — the
                # terminal event was lost with the restart. ADVANCE it rather
                # than re-arming: re-arming would dispatch a second row for the
                # same `run_number`, since `runs_completed` has not moved.
                if await self.advance_on_terminal(open_run.get("execution_id")):
                    moved += 1
            except Exception:  # noqa: BLE001
                logger.exception("[Loop] restart reconcile failed for %s", loop.get("id"))
        if moved:
            logger.info("[Loop] recovered %d loop(s) after restart", moved)
        return moved

    # ---- Internals ----------------------------------------------------------

    @staticmethod
    def _open_run(loop_id: str) -> Optional[dict]:
        """The loop's run row still awaiting a terminal, if any."""
        for run in db.list_loop_runs(loop_id):
            if run.get("status") == "running":
                return run
        return None

    @staticmethod
    def _execution_is_live(execution_id: Optional[str]) -> bool:
        """True when the execution has not reached a terminal yet.

        A missing row counts as NOT live: the run is unrecoverable and the
        advance closes it as failed, which is better than leaving the loop
        pinned on a row that no longer exists.
        """
        if not execution_id:
            return False
        execution = db.get_execution(execution_id)
        if execution is None:
            return False
        status = getattr(execution, "status", None)
        status = status.value if hasattr(status, "value") else str(status)
        return status in ("queued", "running", "pending_retry")

    async def _close_run(self, loop: dict, run: dict, execution_id: str) -> None:
        """Finalize the run row from its execution, then decide what happens next.

        This is the body of the old `for` loop's iteration tail, verbatim in
        behaviour: finalize the run, update progress, broadcast, then apply the
        post-run gates (`stop_signal` > `no_progress` on success;
        `_abort_after_failure` on failure) before falling through to the
        next-iteration gates in `_continue_or_finalize`.
        """
        loop_id = loop["id"]
        run_number = run["run_number"]
        execution = db.get_execution(execution_id)
        status = getattr(execution, "status", None)
        status = status.value if hasattr(status, "value") else str(status)
        succeeded = status == "success"
        response = getattr(execution, "response", None)
        error = getattr(execution, "error", None)
        cost = getattr(execution, "cost", None)
        duration_ms = getattr(execution, "duration_ms", None)

        db.finalize_loop_run(
            run["id"],
            status="completed" if succeeded else "failed",
            response=response,
            error=None if succeeded else (error or "Unknown task failure"),
            cost=cost,
            duration_ms=duration_ms,
            execution_id=execution_id,
        )

        runs = db.list_loop_runs(loop_id)
        derived = _DerivedState(runs)

        # `last_response` carries the last *successful* response even on a
        # tolerated-failure iteration, preserving `{{previous_response}}`.
        db.update_loop_progress(
            loop_id,
            runs_completed=run_number,
            last_response=response if succeeded else loop.get("last_response"),
            failed_runs=derived.failed_runs,
        )

        if succeeded and loop.get("max_cost_usd") is not None:
            # #1155: both unusable-cost cases WARN with distinct messages so the
            # "spends while showing $0" blind spot is greppable, not silent.
            if cost is None:
                logger.warning(
                    "[Loop] %s run %d reported no cost; counts as 0 toward the "
                    "$%.4f budget", loop_id, run_number, loop["max_cost_usd"],
                )
            elif not math.isfinite(cost):
                logger.warning(
                    "[Loop] %s run %d reported a non-finite cost (%r); ignored so "
                    "it can't poison the $%.4f budget accumulator (counts as 0)",
                    loop_id, run_number, cost, loop["max_cost_usd"],
                )

        if succeeded:
            await _broadcast({
                "type": "loop_run_completed",
                "loop_id": loop_id,
                "agent_name": loop["agent_name"],
                "run_number": run_number,
                "execution_id": execution_id,
                "cost": cost,
                "duration_ms": duration_ms,
                "timestamp": datetime.utcnow().isoformat() + "Z",
            })

        loop = db.get_loop(loop_id) or loop
        stop_requested = bool(loop.get("stop_requested_at"))
        deadline_passed = self._deadline_passed(loop)

        if succeeded:
            # Stop-signal is checked BEFORE no-progress and is deliberately
            # UNGUARDED, so it wins even over a pending stop or a passed
            # deadline. `terminal_status` stays "completed", which is what lets
            # the #1167 promotion below apply to it.
            stop_signal = loop.get("stop_signal")
            if stop_signal and stop_signal in (response or ""):
                await self._finalize(
                    loop,
                    status=self._promote(loop, "completed", derived),
                    stop_reason="stop_signal_matched",
                )
                return

            # #1157: a pending user-stop or a passed deadline OUTRANKS
            # no_progress — fall through so the next-iteration gates label it
            # `user_stopped` / `deadline_exceeded`. An explicit Stop must never
            # be relabeled "no progress".
            threshold = loop.get("no_progress_threshold")
            if (
                threshold
                and derived.repeat_count >= threshold
                and not stop_requested
                and not deadline_passed
            ):
                await self._finalize(
                    loop, status="stopped", stop_reason="no_progress"
                )
                return
        else:
            logger.warning(
                "[Loop] %s iteration %d failed (%s)",
                loop_id, run_number, error or "task failed",
            )
            decision = self._abort_after_failure(loop, derived, run_number, error)
            if decision is not None:
                status_, reason, terminal_error = decision
                await self._finalize(
                    loop, status=status_, stop_reason=reason, error=terminal_error,
                )
                return

        await self._continue_or_finalize(loop_id)

    async def _continue_or_finalize(self, loop_id: str, *, parked: bool = False) -> bool:
        """The old `for` loop's top-of-iteration gates, then dispatch.

        Order is load-bearing and matches the pre-#2523 runner exactly:
        `max_runs` ends the loop before any gate runs (the `for` simply
        exhausted), then `user_stopped` > `deadline_exceeded` >
        `budget_exhausted`.

        `parked=True` means the caller already cleared `next_run_at` (the sweep,
        or `stop_loop`), so the inter-run pause has been served and must not be
        applied again. Returns True when a run was dispatched.
        """
        loop = db.get_loop(loop_id)
        if loop is None:
            return False
        if loop["status"] in ("completed", "completed_with_errors", "stopped",
                              "failed", "interrupted"):
            return False
        if loop["status"] == "queued":
            # A loop re-armed by `reconcile_after_restart` can still be `queued`
            # — the crash landed between `create_loop` and `mark_loop_running`.
            # Flip it here, which also stamps the `started_at` the #1156
            # deadline is measured from; without this it would run forever with
            # no deadline and report `queued` the whole time.
            db.mark_loop_running(loop_id)
            loop = db.get_loop(loop_id) or loop

        runs = db.list_loop_runs(loop_id)
        derived = _DerivedState(runs)
        next_run_number = (loop.get("runs_completed") or 0) + 1

        if next_run_number > loop["max_runs"]:
            await self._finalize(
                loop,
                status=self._promote(loop, "completed", derived),
                stop_reason="max_runs_reached",
            )
            return False

        if loop.get("stop_requested_at"):
            await self._finalize(loop, status="stopped", stop_reason="user_stopped")
            return False

        if self._deadline_passed(loop):
            await self._finalize(
                loop, status="stopped", stop_reason="deadline_exceeded"
            )
            return False

        max_cost = loop.get("max_cost_usd")
        if max_cost is not None and derived.accumulated_cost >= max_cost:
            await self._finalize(
                loop, status="stopped", stop_reason="budget_exhausted"
            )
            return False

        # Inter-run pause. Park the loop instead of sleeping in-process; the
        # due-loop sweep brings it back. Capped to the remaining #1156 budget so
        # a park can never outlive the deadline it is supposed to respect.
        delay = loop.get("delay_seconds") or 0
        if delay and not parked and next_run_number > 1:
            deadline = self._deadline(loop)
            if deadline is not None:
                remaining = (deadline - datetime.utcnow()).total_seconds()
                delay = min(delay, max(remaining, 0))
            if delay > 0:
                due = datetime.utcnow() + timedelta(seconds=delay)
                db.schedule_loop_next_run(
                    loop_id, due.isoformat(timespec="microseconds") + "Z"
                )
                return False

        await self._dispatch_run(loop, run_number=next_run_number)
        return True

    async def _dispatch_run(self, loop: dict, *, run_number: int) -> None:
        """Create the execution + run rows, then dispatch without awaiting the turn.

        The execution row is created HERE rather than inside `execute_task` so
        `agent_loop_runs.execution_id` is stamped before the turn can produce a
        terminal — the advance starts from an execution id and has to be able to
        find its iteration (#2523). Under pull the dispatch returns as soon as
        the row is queued; under push it returns when the turn ends. Either way
        this coroutine does not wait for it.
        """
        loop_id = loop["id"]
        message = _render_template(
            loop["message_template"], run_number, loop.get("last_response")
        )
        execution = db.create_task_execution(
            agent_name=loop["agent_name"],
            message=message,
            triggered_by="loop",
            source_user_id=loop.get("started_by_user_id"),
            source_user_email=loop.get("started_by_user_email"),
            source_agent_name=loop.get("source_agent_name"),
            source_mcp_key_id=loop.get("source_mcp_key_id"),
            source_mcp_key_name=loop.get("source_mcp_key_name"),
            model_used=loop.get("model"),
            loop_id=loop_id,
        )
        if execution is None:
            logger.error(
                "[Loop] %s could not create the execution row for run %d",
                loop_id, run_number,
            )
            await self._finalize(
                loop,
                status="failed",
                stop_reason="error",
                error=f"Iteration {run_number}: could not create the execution record",
            )
            return

        db.start_loop_run(loop_id, run_number, execution_id=execution.id)
        _spawn(self._run_and_advance(loop, run_number, execution.id, message))

    async def _run_and_advance(
        self, loop: dict, run_number: int, execution_id: str, message: str
    ) -> None:
        """Dispatch one iteration and make sure an advance follows it.

        `execute_task`'s fast-fail returns (capacity, circuit-open, ephemeral
        budget) write a FAILED row directly and never reach a CAS-won terminal
        writer, so no terminal event fires for them — without this the loop
        would stall on a rejected dispatch. Calling the advance here covers
        those, and the `claim_loop_advance` CAS makes the overlap with the
        terminal hook harmless.

        A QUEUED return is the pull path: the row is on the durable queue and
        the agent's worker will produce the terminal, so there is nothing to
        advance yet.
        """
        try:
            result = await get_task_execution_service().execute_task(
                agent_name=loop["agent_name"],
                message=message,
                triggered_by="loop",
                source_user_id=loop.get("started_by_user_id"),
                source_user_email=loop.get("started_by_user_email"),
                source_agent_name=loop.get("source_agent_name"),
                source_mcp_key_id=loop.get("source_mcp_key_id"),
                source_mcp_key_name=loop.get("source_mcp_key_name"),
                model=loop.get("model"),
                timeout_seconds=loop.get("timeout_per_run"),
                allowed_tools=loop.get("allowed_tools"),
                execution_id=execution_id,
                # Passing `execution_id` means `execute_task` does NOT create the
                # row, so this `loop_id` is not what tags it — `_dispatch_run`
                # already did that. Kept because it is the honest description of
                # the call and would matter if the row were ever created there.
                loop_id=loop["id"],
            )
        except Exception as exc:  # noqa: BLE001 — a raise must not strand the loop
            logger.exception(
                "[Loop] %s iteration %d raised during dispatch", loop["id"], run_number
            )
            try:
                from models import TaskExecutionStatus
                from services.activity_service import activity_service

                # ent#279: exception text is free text on the way into
                # `schedule_executions.error`, and a staged runtime secret can
                # ride an exception message. Scrub before the write, never after.
                error_text = f"{type(exc).__name__}: {exc}"
                staged = get_staged_values()
                if staged:
                    error_text = scrub_text(staged, error_text)

                db.update_execution_status(
                    execution_id=execution_id,
                    status=TaskExecutionStatus.FAILED,
                    error=error_text,
                )
                # #1804: this is a terminal writer, so it owns closing the paired
                # dispatch activity. `execute_task` may have opened one before
                # raising; the helper looks it up and is a no-op when there is
                # none (a raise before admission).
                await activity_service.close_execution_activity(
                    execution_id, TaskExecutionStatus.FAILED, error=error_text,
                )
            except Exception:  # noqa: BLE001
                logger.exception("[Loop] could not fail execution %s", execution_id)
            await self.advance_on_terminal(execution_id)
            return

        if getattr(result, "status", None) == "queued":
            return  # pull path — the worker's terminal advances the loop
        await self.advance_on_terminal(execution_id)

    # ---- Gates --------------------------------------------------------------

    @staticmethod
    def _deadline(loop: dict) -> Optional[datetime]:
        """#1156 wall-clock deadline measured from `started_at`. NULL/0 disables."""
        max_duration = loop.get("max_duration_seconds")
        started_at = _parse_iso(loop.get("started_at"))
        if not max_duration or started_at is None:
            return None
        return started_at + timedelta(seconds=max_duration)

    @classmethod
    def _deadline_passed(cls, loop: dict) -> bool:
        deadline = cls._deadline(loop)
        return deadline is not None and datetime.utcnow() >= deadline

    @staticmethod
    def _abort_after_failure(
        loop: dict, derived: "_DerivedState", run_number: int, error: Optional[str]
    ):
        """#1167 failure policy. Returns (status, stop_reason, error) to abort,
        or None to tolerate the failure and continue."""
        err_msg = f"Iteration {run_number}: {error or 'task failed'}"
        if (loop.get("on_failure") or "abort") != "continue":
            return ("failed", "error", err_msg)
        cap = loop.get("max_consecutive_failures") or 3
        if derived.consecutive_failures >= cap:
            return (
                "failed",
                "max_consecutive_failures",
                f"{err_msg} (reached {cap} consecutive failures)",
            )
        return None

    @staticmethod
    def _promote(loop: dict, status: str, derived: "_DerivedState") -> str:
        """#1167: a continue-mode loop that reached its natural end with
        tolerated failures reports `completed_with_errors`. Only the
        natural-completion path promotes — a stop or an abort keeps its own
        status."""
        if status == "completed" and derived.failed_runs > 0:
            return "completed_with_errors"
        return status

    async def _finalize(
        self,
        loop: dict,
        *,
        status: str,
        stop_reason: str,
        error: Optional[str] = None,
    ) -> None:
        loop_id = loop["id"]
        runs = db.list_loop_runs(loop_id)
        derived = _DerivedState(runs)
        db.finalize_loop(
            loop_id,
            status=status,
            stop_reason=stop_reason,
            error=error,
            failed_runs=derived.failed_runs,
        )
        await _broadcast({
            "type": "loop_completed",
            "loop_id": loop_id,
            "agent_name": loop["agent_name"],
            "status": status,
            "stop_reason": stop_reason,
            "runs_completed": (db.get_loop(loop_id) or loop).get("runs_completed"),
            "failed_runs": derived.failed_runs,
            "timestamp": datetime.utcnow().isoformat() + "Z",
        })


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------

_loop_service: Optional[LoopService] = None


def get_loop_service() -> LoopService:
    global _loop_service
    if _loop_service is None:
        _loop_service = LoopService()
    return _loop_service


async def advance_loop_on_terminal(execution_id: Optional[str]) -> bool:
    """Module-level entry point for the terminal fan-out (#2523).

    Kept as a free function so `event_dispatch_service` can lazy-import one name
    without reaching for the singleton. That import must stay lazy on the
    `event_dispatch_service` side: this module imports `task_execution_service`,
    which imports `event_dispatch_service`, so a top-level import there would
    close the cycle.
    """
    return await get_loop_service().advance_on_terminal(execution_id)
