"""Regression tests for Issue #728: _drain_bounded caps executor-thread block time.

Background
----------
safe_close_pipes() acquires Python's BufferedReader internal lock. readline()
holds that same lock while waiting for pipe data. When both run concurrently —
safe_close_pipes() from drain_reader_threads(), readline() from a reader
thread stuck on a grandchild-held pipe — they deadlock indefinitely.

Before this fix, asyncio.run(_drain_reader_threads(...)) inside an executor
thread would block for the full task timeout (up to timeout_seconds + 60,
e.g. 7260 s for a 7200 s agent), because the outer asyncio.wait_for only
fires once the executor thread returns — and a deadlocked safe_close_pipes
prevents that.

The fix: _drain_bounded wraps asyncio.run() in a daemon thread and limits
total drain time to _DRAIN_BUDGET_SECONDS (90 s).  These tests verify:

1. _drain_bounded returns within budget even when the drain is stuck.
2. _drain_bounded completes normally (no warning) when the drain is fast.
3. The budget constant is exposed so callers can monkeypatch it in tests.
"""
from __future__ import annotations

import asyncio
import threading
import time
from unittest.mock import MagicMock, patch

import pytest


# conftest.py preloads the real agent_server package; just import.
from agent_server.services.subprocess_lifecycle import (  # noqa: E402
    _drain_bounded,
    _DRAIN_BUDGET_SECONDS,
)


def _patch(monkeypatch, name: str, value) -> None:
    """Patch a name in the globals the imported `_drain_bounded` ACTUALLY reads.

    Never by dotted string: `monkeypatch.setattr("agent_server.services...", …)`
    resolves through `sys.modules`, and a later-collected file that evicts and
    re-registers the `agent_server` package leaves a SECOND module copy there.
    The patch then lands on the copy while the `_drain_bounded` bound above
    keeps its original globals — so the REAL drain runs, and its
    `terminate_process_group` + cgroup orphan sweep SIGKILL whatever real
    process group `pid=99999` resolves to and everything in the host's root
    cgroup. That took down a developer desktop session twice and the CI runner
    on every shard ("The runner has received a shutdown signal") before it was
    understood (trinity-enterprise#620 / #728 class). `__globals__` is the
    one dict the function reads regardless of how many copies exist.
    """
    monkeypatch.setitem(_drain_bounded.__globals__, name, value)


@pytest.fixture(autouse=True)
def _never_signal_a_real_process(monkeypatch):
    """The budget-exceeded branch calls `_terminate_process_group(process, 0,
    pgid=pgid)`. With the MagicMock process (`pid=99999`) and `pgid=None`
    that is `os.getpgid(99999)` + `os.killpg(...)` on whatever REAL process
    happens to hold that pid on the host. Stub it for every test here; a test
    that wants to observe the call re-patches with its own mock on top."""
    _patch(monkeypatch, "_terminate_process_group", MagicMock())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fake_process():
    p = MagicMock()
    p.pid = 99999
    return p


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_drain_bounded_returns_within_budget_when_drain_hangs(monkeypatch):
    """When drain_reader_threads deadlocks, _drain_bounded must return within
    budget and log a warning — not wedge for the full task timeout."""

    async def _hanging_drain(*args, **kwargs):
        await asyncio.sleep(600)  # simulate indefinite block

    _patch(monkeypatch, "_drain_reader_threads", _hanging_drain,
    )
    _patch(monkeypatch, "_DRAIN_BUDGET_SECONDS", 2,
    )

    process = _make_fake_process()
    stub_thread = MagicMock(spec=threading.Thread)

    start = time.monotonic()
    _drain_bounded(process, stub_thread, grace=1, pgid=None)
    elapsed = time.monotonic() - start

    # Must return within budget + small scheduling slack
    assert elapsed < 5.0, (
        f"_drain_bounded took {elapsed:.2f}s — budget was 2s; "
        "safe_close_pipes deadlock may not be bounded"
    )


def test_drain_bounded_kills_group_on_budget_exceeded(monkeypatch):
    """#1502: a budget-exceeded drain must SIGKILL the process group so the
    leaked reader can EOF and stop pegging a core — #728 only unblocked the
    executor thread and left the group (and its CPU spin) alive."""

    async def _hanging_drain(*args, **kwargs):
        await asyncio.sleep(600)

    _patch(monkeypatch, "_drain_reader_threads", _hanging_drain,
    )
    _patch(monkeypatch, "_DRAIN_BUDGET_SECONDS", 1,
    )
    kill = MagicMock()
    _patch(monkeypatch, "_terminate_process_group", kill,
    )

    process = _make_fake_process()
    outcome = _drain_bounded(process, MagicMock(spec=threading.Thread), grace=1, pgid=4242)

    assert outcome == "budget_exceeded"
    kill.assert_called_once()
    # the captured pgid is forwarded so grandchildren (not just the reaped pid) die
    _args, _kwargs = kill.call_args
    assert _kwargs.get("pgid") == 4242 or 4242 in _args


def test_drain_bounded_budget_exceed_kill_failure_is_swallowed(monkeypatch):
    """A failing group-kill on budget-exceed must not raise into the caller."""

    async def _hanging_drain(*args, **kwargs):
        await asyncio.sleep(600)

    _patch(monkeypatch, "_drain_reader_threads", _hanging_drain)
    _patch(monkeypatch, "_DRAIN_BUDGET_SECONDS", 1)
    _patch(monkeypatch, "_terminate_process_group", MagicMock(side_effect=OSError("boom")))

    outcome = _drain_bounded(_make_fake_process(), MagicMock(spec=threading.Thread), pgid=1)
    assert outcome == "budget_exceeded"  # still returns cleanly


def test_drain_bounded_completes_fast_when_drain_is_quick(monkeypatch):
    """When drain_reader_threads finishes quickly, _drain_bounded must not add
    significant latency (no extra sleeping)."""

    call_log: list[str] = []

    async def _fast_drain(*args, **kwargs):
        call_log.append("drain_called")

    _patch(monkeypatch, "_drain_reader_threads", _fast_drain,
    )

    process = _make_fake_process()
    stub_thread = MagicMock(spec=threading.Thread)

    start = time.monotonic()
    _drain_bounded(process, stub_thread, grace=5, pgid=None)
    elapsed = time.monotonic() - start

    assert "drain_called" in call_log, "_drain_reader_threads was not called"
    assert elapsed < 3.0, f"_drain_bounded took {elapsed:.2f}s for a fast drain"


def test_drain_bounded_budget_constant_is_90():
    """_DRAIN_BUDGET_SECONDS must be 90 — changing it is a breaking change
    that affects the executor-thread block time guarantee in Issue #728."""
    assert _DRAIN_BUDGET_SECONDS == 90, (
        f"_DRAIN_BUDGET_SECONDS changed to {_DRAIN_BUDGET_SECONDS}; "
        "update this test and the Issue #728 comment if intentional"
    )


def test_drain_bounded_forwards_grace_and_pgid(monkeypatch):
    """_drain_bounded must pass grace and pgid through to drain_reader_threads."""

    received: dict = {}

    async def _recording_drain(process, *threads, grace=5, pgid=None, **kwargs):
        received["grace"] = grace
        received["pgid"] = pgid

    _patch(monkeypatch, "_drain_reader_threads", _recording_drain,
    )

    process = _make_fake_process()
    _drain_bounded(process, grace=3, pgid=42)

    # Give the daemon thread a moment to run
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and "grace" not in received:
        time.sleep(0.05)

    assert received.get("grace") == 3, f"grace not forwarded: {received}"
    assert received.get("pgid") == 42, f"pgid not forwarded: {received}"


# ---------------------------------------------------------------------------
# Issue #1025 (salvaged from #980): _drain_bounded returns a DrainOutcome and
# no longer swallows daemon-thread exceptions.
# ---------------------------------------------------------------------------

def test_drain_bounded_returns_completed_on_clean_drain(monkeypatch):
    """A drain that finishes within budget must report ``completed``."""

    async def _fast_drain(*args, **kwargs):
        return None

    _patch(monkeypatch, "_drain_reader_threads", _fast_drain,
    )

    assert _drain_bounded(_make_fake_process(), grace=5, pgid=None) == "completed"


def test_drain_bounded_returns_budget_exceeded_on_hang(monkeypatch):
    """A drain that overruns the budget must report ``budget_exceeded`` (a
    leaked reader thread the finalize path must defend against)."""

    async def _hanging_drain(*args, **kwargs):
        await asyncio.sleep(600)

    _patch(monkeypatch, "_drain_reader_threads", _hanging_drain,
    )
    _patch(monkeypatch, "_DRAIN_BUDGET_SECONDS", 1,
    )

    assert _drain_bounded(_make_fake_process(), grace=1, pgid=None) == "budget_exceeded"


def test_drain_bounded_returns_errored_and_logs_when_drain_raises(monkeypatch, caplog):
    """A drain that raises must be reported as ``errored`` and logged — not
    silently swallowed as a clean ``completed`` (the pre-#1025 bug)."""

    async def _raising_drain(*args, **kwargs):
        raise RuntimeError("boom in drain")

    _patch(monkeypatch, "_drain_reader_threads", _raising_drain,
    )

    with caplog.at_level("ERROR"):
        outcome = _drain_bounded(_make_fake_process(), grace=5, pgid=None)

    assert outcome == "errored"
    assert any("Drain raised" in r.message for r in caplog.records), (
        "errored drain must be logged, not swallowed"
    )


def test_drain_bounded_returns_leaked_when_reader_survives(monkeypatch):
    """A drain that returns within budget WITHOUT raising but leaves a reader
    thread alive must report ``leaked`` — drain_reader_threads force-closes and
    continues on the #586 leaked-reader case, so a within-budget return does not
    by itself prove the readers are dead (review finding on the #1025 PR)."""

    async def _fast_drain(*args, **kwargs):
        return None

    _patch(monkeypatch, "_drain_reader_threads", _fast_drain,
    )

    # A real, still-alive reader thread (the leaked case).
    stop = threading.Event()
    leaked_reader = threading.Thread(target=stop.wait, daemon=True)
    leaked_reader.start()
    try:
        outcome = _drain_bounded(_make_fake_process(), leaked_reader, grace=5, pgid=None)
    finally:
        stop.set()
        leaked_reader.join(timeout=2)

    assert outcome == "leaked", (
        "a within-budget drain that leaves a reader alive must surface 'leaked' "
        "so finalize snapshots instead of trusting the live buffers"
    )
