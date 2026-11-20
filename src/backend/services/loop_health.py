"""Who blocked the event loop, and for how long (#4671, #4674).

A hosted instance answered `/health` in more than 150 ms in 24 of 1,500
samples, the worst at 1.5 s, clustered near the minute mark — and nothing in
the logs said why. `/health` does no I/O on PostgreSQL, so its latency IS the
event loop's lag: something synchronous ran on the loop and every request in
that worker waited behind it. A code audit can list hundreds of candidates
(every `async def` that calls the sync `db` layer); it cannot say which one
ran at :58. This module can, from inside the process, at the moment it
happens.

Two instruments, one registry:

**The loop probe.** A coroutine sleeps `PROBE_INTERVAL_S` and measures how
late it woke: that lateness is the lag every other coroutine also paid. A
daemon thread watches the coroutine's last wake-up; when the loop has been
silent past `STALL_THRESHOLD_S` it snapshots the loop thread's stack — the
frames that are blocking right now. When the loop comes back, ONE warning is
logged with the blocked time, the worker pid and that stack. A stall that
outlives `STUCK_REPORT_S` is also reported while it is still going, in case
the process never comes back.

**Tick timing.** `tick("name")` wraps one cycle of a background loop and logs
a warning with the worker pid when the cycle's wall time passes
`TICK_WARN_S`. Wall time includes awaits, so a slow tick is not by itself a
blocked loop — it says which cycle was running; the probe says whether the
loop was blocked, and by which frame.

What is logged is code location only (file, line, function): never a local,
an argument or a query. `snapshot()` is numbers only and is what the host
telemetry endpoint reports for the worker that answered.

Every loop in `main.py` starts in EVERY uvicorn worker, and so does this
probe — by design, with no leader lease: a worker can only measure its own
loop, and the pid in each line says which one it was.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time
import traceback
from collections import deque
from contextlib import contextmanager
from typing import Any, Deque, Dict, Iterator, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _env_ms(name: str, default_ms: float) -> float:
    try:
        value = float(os.getenv(name, "") or default_ms)
    except ValueError:
        value = default_ms
    return max(1.0, value) / 1000.0


# How late the probe may wake before it counts as a stall: the 150 ms bar
# #4671 and #4674 measure `/health` against, so every sample that would fail
# that acceptance check is also a line in the log saying why.
STALL_THRESHOLD_S = _env_ms("EVENT_LOOP_STALL_THRESHOLD_MS", 150)
# A background cycle slower than this is named in the log.
TICK_WARN_S = _env_ms("LOOP_TICK_WARN_MS", 250)
PROBE_INTERVAL_S = 0.05
# A stall still going after this long is reported before it ends.
STUCK_REPORT_S = 5.0
# `snapshot()` windows.
LAG_WINDOW_S = 60.0
STALL_WINDOW_S = 600.0
STACK_FRAMES = 12

_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _short(path: str) -> str:
    """A path as a reader recognises it: relative to the backend, or the last
    two components of a library file. Never an absolute host path."""
    if path.startswith(_BACKEND_ROOT + os.sep):
        return path[len(_BACKEND_ROOT) + 1:]
    parts = path.replace("\\", "/").split("/")
    return "/".join(parts[-2:])


def format_stack(frame, limit: int = STACK_FRAMES) -> List[str]:
    """`file:line in function`, innermost first — locations only, no values."""
    if frame is None:
        return []
    entries = traceback.extract_stack(frame)[-limit:]
    return [f"{_short(e.filename)}:{e.lineno} in {e.name}" for e in reversed(entries)]


def _first_own_frame(stack: List[str]) -> Optional[str]:
    """The innermost frame that is Trinity's code rather than a library's —
    the line a reader should open first."""
    for entry in stack:
        location = entry.split(":", 1)[0]
        if "/" in location and location.split("/", 1)[0] in (
                "routers", "services", "db", "adapters", "client_portal", "utils",
                "canary", "shared_sessions", "enterprise"):
            return entry
        if "/" not in location and location.endswith(".py"):
            return entry
    return None


class EventLoopProbe:
    """Measures one event loop's lag and names what blocked it."""

    def __init__(self, *, interval_s: float = PROBE_INTERVAL_S,
                 threshold_s: float = STALL_THRESHOLD_S,
                 stuck_report_s: float = STUCK_REPORT_S,
                 clock=time.monotonic, read_stack=None) -> None:
        self.interval_s = interval_s
        self.threshold_s = threshold_s
        self.stuck_report_s = stuck_report_s
        self._clock = clock
        # How the loop thread's stack is read; a test hands in its own.
        self._read_stack = read_stack or self._loop_stack
        self._lock = threading.Lock()
        self._task: Optional[asyncio.Task] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._loop_thread_id: Optional[int] = None
        self._last_beat: float = 0.0
        self._captured: Optional[List[str]] = None   # the stack of the stall in progress
        self._stuck_reported = False
        self._last_lag_s = 0.0
        self._lags: Deque[Tuple[float, float]] = deque()      # (at, lag) over LAG_WINDOW_S
        self._stalls: Deque[Tuple[float, float]] = deque()    # (at, lag) over STALL_WINDOW_S

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        """Start measuring the RUNNING loop. Idempotent; call it from the loop."""
        if self._task is not None and not self._task.done():
            return
        self._loop_thread_id = threading.get_ident()
        self._last_beat = self._clock()
        self._stop.clear()
        self._task = asyncio.get_running_loop().create_task(self._beat(), name="loop-health-probe")
        self._thread = threading.Thread(target=self._watch, name="loop-health-watch", daemon=True)
        self._thread.start()

    async def stop(self) -> None:
        self._stop.set()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 — shutdown
                pass
        thread, self._thread = self._thread, None
        if thread is not None:
            await asyncio.to_thread(thread.join, 1.0)

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    # ------------------------------------------------------------- the loop

    async def _beat(self) -> None:
        while True:
            started = self._clock()
            await asyncio.sleep(self.interval_s)
            self.record_beat(self._clock() - started - self.interval_s)

    def record_beat(self, lag_s: float) -> None:
        """The probe woke `lag_s` late. Runs ON the loop."""
        lag_s = max(0.0, lag_s)
        now = self._clock()
        with self._lock:
            self._last_beat = now
            self._last_lag_s = lag_s
            stack, self._captured = self._captured, None
            self._stuck_reported = False
            self._lags.append((now, lag_s))
            self._trim(self._lags, now - LAG_WINDOW_S)
            if lag_s >= self.threshold_s:
                self._stalls.append((now, lag_s))
            self._trim(self._stalls, now - STALL_WINDOW_S)
        if lag_s >= self.threshold_s:
            self._report(lag_s, stack)

    def _report(self, lag_s: float, stack: Optional[List[str]]) -> None:
        where = _first_own_frame(stack or []) or (stack[0] if stack else "unknown (stack not captured)")
        logger.warning(
            "[LoopHealth] event loop blocked %d ms pid=%d at %s | stack: %s",
            round(lag_s * 1000), os.getpid(), where, " <- ".join(stack or []) or "-",
            extra={"event": "event_loop_blocked", "lag_ms": round(lag_s * 1000),
                   "pid": os.getpid(), "blocked_at": where},
        )

    # --------------------------------------------------------- the watchdog

    def _loop_stack(self) -> List[str]:
        return format_stack(sys._current_frames().get(self._loop_thread_id))

    def _watch(self) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                self.check()
            except Exception:  # noqa: BLE001 — a diagnostic must never take the worker down
                logger.debug("[LoopHealth] watchdog check failed", exc_info=True)

    def check(self) -> None:
        """One watchdog look. Runs OFF the loop (the watch thread)."""
        now = self._clock()
        with self._lock:
            silent_for = now - self._last_beat - self.interval_s
            if silent_for < self.threshold_s:
                return
            capture = self._captured is None
            report_stuck = silent_for >= self.stuck_report_s and not self._stuck_reported
            if report_stuck:
                self._stuck_reported = True
            beat_seen = self._last_beat
        if not (capture or report_stuck):
            return
        stack = self._read_stack()
        with self._lock:
            # Only while the SAME silence is still going: if the loop woke
            # between the look and the snapshot, these frames are whatever ran
            # next, and keeping them would pin them on the following stall.
            if capture and self._captured is None and self._last_beat == beat_seen:
                self._captured = stack
        if report_stuck:
            logger.warning(
                "[LoopHealth] event loop STILL blocked after %d ms pid=%d at %s | stack: %s",
                round(silent_for * 1000), os.getpid(),
                _first_own_frame(stack) or (stack[0] if stack else "unknown"),
                " <- ".join(stack) or "-",
                extra={"event": "event_loop_stuck", "lag_ms": round(silent_for * 1000),
                       "pid": os.getpid()},
            )

    # ------------------------------------------------------------- readback

    @staticmethod
    def _trim(window: Deque[Tuple[float, float]], cutoff: float) -> None:
        while window and window[0][0] < cutoff:
            window.popleft()

    def snapshot(self) -> Dict[str, Any]:
        """Numbers only — safe to return from an endpoint."""
        now = self._clock()
        with self._lock:
            self._trim(self._lags, now - LAG_WINDOW_S)
            self._trim(self._stalls, now - STALL_WINDOW_S)
            lags = [lag for _, lag in self._lags]
            stalls = [lag for _, lag in self._stalls]
            last = self._last_lag_s
        return {
            "pid": os.getpid(),
            "running": self.running,
            "lag_ms": round(last * 1000, 1),
            "max_lag_ms_60s": round(max(lags, default=0.0) * 1000, 1),
            "stalls_10m": len(stalls),
            "worst_stall_ms_10m": round(max(stalls, default=0.0) * 1000, 1),
            "stall_threshold_ms": round(self.threshold_s * 1000),
        }


# ---------------------------------------------------------------------------
# Tick timing
# ---------------------------------------------------------------------------

_ticks_lock = threading.Lock()
_ticks: Dict[str, Dict[str, Any]] = {}


def record_tick(name: str, seconds: float) -> None:
    slow = seconds >= TICK_WARN_S
    with _ticks_lock:
        entry = _ticks.setdefault(name, {"ticks": 0, "slow_ticks": 0, "last_ms": 0.0, "max_ms": 0.0})
        entry["ticks"] += 1
        entry["slow_ticks"] += 1 if slow else 0
        entry["last_ms"] = round(seconds * 1000, 1)
        entry["max_ms"] = max(entry["max_ms"], entry["last_ms"])
    if slow:
        logger.warning(
            "[LoopHealth] %s tick took %d ms pid=%d", name, round(seconds * 1000), os.getpid(),
            extra={"event": "loop_tick_slow", "loop": name, "tick_ms": round(seconds * 1000),
                   "pid": os.getpid()},
        )


@contextmanager
def tick(name: str) -> Iterator[None]:
    """Time one cycle of the background loop `name`:

        while True:
            with loop_health.tick("capacity_maintenance"):
                await capacity.run_maintenance()
            await asyncio.sleep(60)

    The sleep stays OUTSIDE the block. An exception passes through untouched
    and the cycle is still timed.
    """
    started = time.monotonic()
    try:
        yield
    finally:
        record_tick(name, time.monotonic() - started)


def tick_snapshot() -> Dict[str, Dict[str, Any]]:
    with _ticks_lock:
        return {name: dict(entry) for name, entry in sorted(_ticks.items())}


# ---------------------------------------------------------------------------
# The process's probe
# ---------------------------------------------------------------------------

_probe = EventLoopProbe()


def get_probe() -> EventLoopProbe:
    return _probe


def start() -> None:
    """Start this worker's probe. `EVENT_LOOP_PROBE_ENABLED=false` turns it off."""
    if os.getenv("EVENT_LOOP_PROBE_ENABLED", "true").strip().lower() in ("0", "false", "no", "off"):
        logger.info("[LoopHealth] event-loop probe disabled by EVENT_LOOP_PROBE_ENABLED")
        return
    _probe.start()
    logger.info("[LoopHealth] event-loop probe started pid=%d (stall >= %d ms, slow tick >= %d ms)",
                os.getpid(), round(_probe.threshold_s * 1000), round(TICK_WARN_S * 1000))


async def stop() -> None:
    await _probe.stop()


def snapshot() -> Dict[str, Any]:
    """This worker's loop lag and its background loops' tick times."""
    return {"event_loop": _probe.snapshot(), "loops": tick_snapshot()}
