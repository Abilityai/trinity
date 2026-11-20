"""The event-loop probe names what blocked the loop (#4671, #4674).

#4671 measured `/health` over 150 ms in 24 of 1,500 samples, the worst at
1.5 s, and nothing in the logs said why. `services/loop_health.py` is the
instrument that says why. These tests pin the three things that make its
output usable as evidence:

- a blocked loop is reported ONCE, with the pid, the blocked time and the
  frame that was blocking — and a loop that is merely busy is not reported;
- the stack a stall is reported with is that stall's own, never an earlier
  one's and never whatever ran after the loop woke;
- only code locations are logged — no locals, no arguments.

Plus the tick timer, and that the loops #4674 lists are actually wrapped.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import time
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_BACKEND = str(_REPO / "src" / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

pytestmark = pytest.mark.unit

from services import loop_health  # noqa: E402
from services.loop_health import EventLoopProbe  # noqa: E402


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _probe(clock, **kw):
    probe = EventLoopProbe(interval_s=0.05, threshold_s=0.25, stuck_report_s=5.0, clock=clock, **kw)
    probe._loop_thread_id = None      # no real loop thread in the clocked tests
    probe._last_beat = clock()
    return probe


def _messages(caplog, event=None):
    return [r for r in caplog.records
            if r.name == "services.loop_health" and (event is None or getattr(r, "event", None) == event)]


# ===========================================================================
# What counts as a stall
# ===========================================================================

class TestStallThreshold:
    def test_a_busy_loop_is_not_reported(self, caplog):
        clock = _Clock()
        probe = _probe(clock)
        with caplog.at_level(logging.WARNING, logger="services.loop_health"):
            for lag in (0.0, 0.01, 0.12, 0.249):
                clock.advance(0.05 + lag)
                probe.record_beat(lag)
        assert _messages(caplog) == []
        snap = probe.snapshot()
        assert snap["stalls_10m"] == 0
        assert snap["max_lag_ms_60s"] == 249.0
        assert snap["lag_ms"] == 249.0

    def test_a_stall_is_reported_once_with_pid_and_duration(self, caplog):
        clock = _Clock()
        probe = _probe(clock)
        with caplog.at_level(logging.WARNING, logger="services.loop_health"):
            clock.advance(1.55)
            probe.record_beat(1.5)
        (record,) = _messages(caplog, "event_loop_blocked")
        assert record.lag_ms == 1500
        assert record.pid == os.getpid()
        assert "blocked 1500 ms" in record.getMessage()
        assert f"pid={os.getpid()}" in record.getMessage()
        snap = probe.snapshot()
        assert (snap["stalls_10m"], snap["worst_stall_ms_10m"]) == (1, 1500.0)

    def test_the_windows_forget(self):
        clock = _Clock()
        probe = _probe(clock)
        clock.advance(1.0)
        probe.record_beat(0.9)
        clock.advance(loop_health.LAG_WINDOW_S + 1)
        probe.record_beat(0.0)
        snap = probe.snapshot()
        assert snap["max_lag_ms_60s"] == 0.0
        assert snap["stalls_10m"] == 1, "the stall window is longer than the lag window"
        clock.advance(loop_health.STALL_WINDOW_S + 1)
        assert probe.snapshot()["stalls_10m"] == 0

    def test_a_negative_lag_is_zero(self):
        clock = _Clock()
        probe = _probe(clock)
        probe.record_beat(-0.01)
        assert probe.snapshot()["lag_ms"] == 0.0


# ===========================================================================
# The watchdog's capture belongs to the stall it was taken in
# ===========================================================================

class TestCapture:
    def test_nothing_is_captured_below_the_threshold(self):
        clock = _Clock()
        probe = _probe(clock)
        clock.advance(0.05 + 0.2)
        probe.check()
        assert probe._captured is None

    def test_a_silent_loop_is_captured_once(self, monkeypatch):
        clock = _Clock()
        probe = _probe(clock)
        calls = []
        monkeypatch.setattr(loop_health, "format_stack",
                            lambda frame, limit=12: calls.append(1) or ["services/x.py:1 in culprit"])
        clock.advance(0.05 + 0.3)
        probe.check()
        clock.advance(0.3)
        probe.check()
        assert probe._captured == ["services/x.py:1 in culprit"]
        assert len(calls) == 1, "the stack is the one at the threshold, not re-taken per look"

    def test_the_report_carries_the_captured_stack_and_clears_it(self, caplog, monkeypatch):
        clock = _Clock()
        probe = _probe(clock)
        monkeypatch.setattr(loop_health, "format_stack", lambda frame, limit=12: [
            "site-packages/sqlalchemy/engine/base.py:1969 in _exec_single_context",
            "db/users.py:132 in get_user_by_email",
            "dependencies.py:410 in get_current_user",
        ])
        clock.advance(0.05 + 0.4)
        probe.check()
        with caplog.at_level(logging.WARNING, logger="services.loop_health"):
            clock.advance(0.2)
            probe.record_beat(0.6)
        (record,) = _messages(caplog, "event_loop_blocked")
        # The first frame of OUR code, not the library frame it was inside.
        assert record.blocked_at == "db/users.py:132 in get_user_by_email"
        assert "dependencies.py:410 in get_current_user" in record.getMessage()
        assert probe._captured is None

    def test_a_capture_taken_after_the_loop_woke_is_dropped(self, monkeypatch):
        """The watchdog looks, the loop wakes, THEN the frames are read: they
        are whatever ran next. Keeping them would blame them for the NEXT stall."""
        clock = _Clock()
        probe = _probe(clock)

        def frames_read_late(frame, limit=12):
            clock.advance(0.01)
            probe.record_beat(0.36)          # the loop woke while we were reading
            return ["routers/innocent.py:1 in bystander"]

        monkeypatch.setattr(loop_health, "format_stack", frames_read_late)
        clock.advance(0.05 + 0.3)
        probe.check()
        assert probe._captured is None

    def test_a_stall_with_no_capture_says_so(self, caplog):
        clock = _Clock()
        probe = _probe(clock)
        with caplog.at_level(logging.WARNING, logger="services.loop_health"):
            clock.advance(0.5)
            probe.record_beat(0.45)
        (record,) = _messages(caplog, "event_loop_blocked")
        assert "unknown" in record.blocked_at

    def test_a_stall_that_does_not_end_is_reported_while_it_lasts(self, caplog, monkeypatch):
        clock = _Clock()
        probe = _probe(clock)
        monkeypatch.setattr(loop_health, "format_stack",
                            lambda frame, limit=12: ["services/x.py:9 in forever"])
        with caplog.at_level(logging.WARNING, logger="services.loop_health"):
            clock.advance(0.05 + 1.0)
            probe.check()
            assert _messages(caplog, "event_loop_stuck") == []
            clock.advance(5.0)
            probe.check()
            clock.advance(5.0)
            probe.check()
        (record,) = _messages(caplog, "event_loop_stuck")
        assert "STILL blocked" in record.getMessage()
        assert "services/x.py:9 in forever" in record.getMessage()


# ===========================================================================
# Only locations are logged
# ===========================================================================

class TestLocationsOnly:
    def test_a_stack_names_file_line_and_function_and_no_values(self):
        secret_token = "sk-not-a-real-key-but-must-never-be-logged"  # noqa: F841 — a local on the frame

        def culprit(password="hunter2-also-never-logged"):
            return loop_health.format_stack(sys._getframe())

        stack = culprit()
        assert re.fullmatch(r".+:\d+ in culprit", stack[0])
        joined = " ".join(stack)
        assert "sk-not-a-real-key" not in joined and "hunter2" not in joined

    def test_paths_are_relative_never_absolute(self):
        stack = loop_health.format_stack(sys._getframe())
        assert all(not entry.startswith("/") for entry in stack)
        assert loop_health._short(os.path.join(_BACKEND, "services", "loop_health.py")) \
            == os.path.join("services", "loop_health.py")
        assert loop_health._short("/usr/lib/python3/site-packages/redis/client.py") == "redis/client.py"

    def test_the_snapshot_is_numbers_only(self):
        clock = _Clock()
        snap = _probe(clock).snapshot()
        assert set(snap) == {"pid", "running", "lag_ms", "max_lag_ms_60s", "stalls_10m",
                             "worst_stall_ms_10m", "stall_threshold_ms"}
        assert all(isinstance(v, (int, float, bool)) for v in snap.values())


# ===========================================================================
# End to end, on a real loop
# ===========================================================================

def _blocking_culprit(seconds: float) -> None:
    time.sleep(seconds)          # the defect class itself: sync work on the loop


class TestOnARealLoop:
    def test_a_blocking_call_is_named(self, caplog):
        async def scenario():
            probe = EventLoopProbe(interval_s=0.01, threshold_s=0.1)
            probe.start()
            try:
                await asyncio.sleep(0.05)
                _blocking_culprit(0.4)
                await asyncio.sleep(0.1)
            finally:
                await probe.stop()
            return probe

        with caplog.at_level(logging.WARNING, logger="services.loop_health"):
            probe = asyncio.run(scenario())
        records = _messages(caplog, "event_loop_blocked")
        assert len(records) == 1, [r.getMessage() for r in records]
        assert records[0].lag_ms >= 300
        assert "_blocking_culprit" in records[0].getMessage()
        assert not probe.running

    def test_an_awaiting_loop_is_never_reported(self, caplog):
        async def scenario():
            probe = EventLoopProbe(interval_s=0.01, threshold_s=0.1)
            probe.start()
            try:
                await asyncio.sleep(0.4)     # the same 0.4 s, but yielding
            finally:
                await probe.stop()
            return probe.snapshot()

        with caplog.at_level(logging.WARNING, logger="services.loop_health"):
            snap = asyncio.run(scenario())
        assert _messages(caplog) == []
        assert snap["stalls_10m"] == 0

    def test_start_is_idempotent_and_stop_ends_the_watch_thread(self):
        async def scenario():
            probe = EventLoopProbe(interval_s=0.01, threshold_s=0.1)
            probe.start()
            task, thread = probe._task, probe._thread
            probe.start()
            assert probe._task is task and probe._thread is thread
            await probe.stop()
            return thread

        thread = asyncio.run(scenario())
        assert not thread.is_alive()

    def test_the_probe_can_be_disabled(self, monkeypatch):
        monkeypatch.setenv("EVENT_LOOP_PROBE_ENABLED", "false")
        monkeypatch.setattr(loop_health, "_probe", EventLoopProbe())

        async def scenario():
            loop_health.start()
            return loop_health.get_probe().running

        assert asyncio.run(scenario()) is False


# ===========================================================================
# Tick timing
# ===========================================================================

class TestTick:
    @pytest.fixture(autouse=True)
    def clean(self, monkeypatch):
        monkeypatch.setattr(loop_health, "_ticks", {})
        monkeypatch.setattr(loop_health, "TICK_WARN_S", 0.25)

    def test_a_fast_tick_is_counted_and_silent(self, caplog):
        with caplog.at_level(logging.WARNING, logger="services.loop_health"):
            loop_health.record_tick("heartbeat_watch", 0.01)
        assert _messages(caplog) == []
        assert loop_health.tick_snapshot() == {
            "heartbeat_watch": {"ticks": 1, "slow_ticks": 0, "last_ms": 10.0, "max_ms": 10.0}}

    def test_a_slow_tick_is_named_with_the_pid(self, caplog):
        with caplog.at_level(logging.WARNING, logger="services.loop_health"):
            loop_health.record_tick("capacity_maintenance", 1.2)
            loop_health.record_tick("capacity_maintenance", 0.02)
        (record,) = _messages(caplog, "loop_tick_slow")
        assert (record.loop, record.tick_ms, record.pid) == ("capacity_maintenance", 1200, os.getpid())
        entry = loop_health.tick_snapshot()["capacity_maintenance"]
        assert entry == {"ticks": 2, "slow_ticks": 1, "last_ms": 20.0, "max_ms": 1200.0}

    def test_tick_times_the_block_and_lets_the_exception_through(self):
        with pytest.raises(RuntimeError):
            with loop_health.tick("sync_health"):
                raise RuntimeError("cycle failed")
        assert loop_health.tick_snapshot()["sync_health"]["ticks"] == 1

    def test_tick_wraps_awaited_work(self):
        async def cycle():
            with loop_health.tick("operator_queue_sync"):
                await asyncio.sleep(0.03)

        asyncio.run(cycle())
        assert loop_health.tick_snapshot()["operator_queue_sync"]["last_ms"] >= 25

    def test_snapshot_joins_both(self):
        loop_health.record_tick("monitoring", 0.01)
        snap = loop_health.snapshot()
        assert set(snap) == {"event_loop", "loops"}
        assert "monitoring" in snap["loops"]


# ===========================================================================
# Wiring
# ===========================================================================

def _source(*parts) -> str:
    return (_REPO.joinpath(*parts)).read_text()


class TestWiring:
    @pytest.mark.parametrize("path, name", [
        (("src", "backend", "main.py"), "capacity_maintenance"),
        (("src", "backend", "main.py"), "loop_due_sweep"),
        (("src", "backend", "services", "heartbeat_service.py"), "heartbeat_watch"),
        (("src", "backend", "services", "operator_queue_service", "poller.py"), "operator_queue_sync"),
        (("src", "backend", "services", "monitoring_service.py"), "monitoring"),
        (("src", "backend", "services", "sync_health_service.py"), "sync_health"),
    ])
    def test_each_loop_times_its_cycle_not_its_sleep(self, path, name):
        """#4674's five loops (and the 5 s due-run sweep): the cycle is inside
        `tick(...)`, the sleep is not — a tick that included its own sleep
        would report every loop as slow."""
        source = _source(*path)
        marker = f'with loop_health.tick("{name}"):'
        assert source.count(marker) == 1, f"{name} must be timed exactly once in {'/'.join(path)}"
        at = source.index(marker)
        indent = len(source[:at]) - len(source[:at].rstrip(" "))
        block = []
        for line in source[at:].split("\n")[1:]:
            if line.strip() and len(line) - len(line.lstrip(" ")) <= indent:
                break
            block.append(line)
        assert block, name
        assert "asyncio.sleep" not in "\n".join(block), f"{name}: the sleep must stay outside the tick"

    def test_the_probe_starts_and_stops_with_the_lifespan(self):
        main = _source("src", "backend", "main.py")
        assert "loop_health.start()" in main
        assert "await loop_health.stop()" in main

    @pytest.mark.parametrize("compose", ["docker-compose.prod.yml", "docker-compose.hosted.yml"])
    def test_the_worker_count_is_configurable_and_defaults_to_two(self, compose):
        """AC (#4674): configurable by env, default unchanged."""
        text = _source(compose)
        assert "--workers ${BACKEND_WORKERS:-2} " in text
        assert not re.search(r"--workers \d", text), "no hard-coded worker count beside it"

    @pytest.mark.parametrize("compose", ["docker-compose.yml", "docker-compose.prod.yml",
                                         "docker-compose.hosted.yml"])
    def test_the_probe_settings_reach_the_container(self, compose):
        text = _source(compose)
        for var in ("EVENT_LOOP_PROBE_ENABLED", "EVENT_LOOP_STALL_THRESHOLD_MS", "LOOP_TICK_WARN_MS"):
            assert f"- {var}=${{{var}:-" in text, f"{var} is not forwarded in {compose}"
