"""
Tests for Issue #912 — drain-time orphan sweep must forward an
allowlist of in-flight execution pids/pgids so concurrent legitimate
claude subprocesses don't get SIGKILLed when a sibling task drains.

Three surfaces:

  * ``ProcessRegistry.active_execution_pids()`` — single canonical
    source of the allowlist, replacing the duplicated walks that lived
    in ``orphan_sweeper`` and ``ProcessRegistry.terminate``.
  * ``subprocess_pgroup._active_execution_pids_for_drain`` — the lazy
    registry-read helper used in the drain-time finally block.
  * ``subprocess_pgroup.drain_reader_threads`` — proof-of-wire-up that
    ``kill_cgroup_orphans`` is called with ``extra_pids=`` populated.

Tests bypass FastAPI and the real cgroup; ``kill_cgroup_orphans`` is
monkey-patched into a recording stub.
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_REPO = Path(__file__).resolve().parents[2]
_BASE_IMAGE = _REPO / "docker" / "base-image"
if str(_BASE_IMAGE) not in sys.path:
    sys.path.insert(0, str(_BASE_IMAGE))


def _make_proc_entry(pid: int, *, poll_result=None, pgid: int | None = None) -> dict:
    """Build a registry-shaped entry: process is a Mock that reports the
    requested pid and poll() result; metadata carries the optional pgid."""
    process = MagicMock(spec=subprocess.Popen)
    process.pid = pid
    process.poll.return_value = poll_result
    return {
        "process": process,
        "started_at": __import__("datetime").datetime.utcnow(),
        "metadata": {"pgid": pgid} if pgid is not None else {},
    }


# ---------------------------------------------------------------------------
# ProcessRegistry.active_execution_pids
# ---------------------------------------------------------------------------


def test_active_execution_pids_includes_pid_and_pgid():
    from agent_server.services.process_registry import ProcessRegistry

    registry = ProcessRegistry()
    registry._processes = {
        "exec-a": _make_proc_entry(1001, pgid=1001),
        "exec-b": _make_proc_entry(2002, pgid=3003),
    }

    pids = registry.active_execution_pids()
    # pid + pgid for each entry; duplicates fine (allowlist resolver dedupes).
    assert sorted(pids) == [1001, 1001, 2002, 3003]


def test_active_execution_pids_excludes_self():
    from agent_server.services.process_registry import ProcessRegistry

    registry = ProcessRegistry()
    registry._processes = {
        "exec-a": _make_proc_entry(1001, pgid=1001),
        "exec-b": _make_proc_entry(2002, pgid=2002),
    }

    pids = registry.active_execution_pids(exclude_execution_id="exec-a")
    assert 1001 not in pids
    assert sorted(pids) == [2002, 2002]


def test_active_execution_pids_skips_finished_processes():
    """A process whose ``.poll()`` returned non-None is no longer
    running; do not forward its pid as a preserve-target."""
    from agent_server.services.process_registry import ProcessRegistry

    registry = ProcessRegistry()
    registry._processes = {
        "exec-alive": _make_proc_entry(1001, poll_result=None, pgid=1001),
        "exec-dead":  _make_proc_entry(2002, poll_result=0,    pgid=2002),
    }

    pids = registry.active_execution_pids()
    assert sorted(pids) == [1001, 1001]


def test_active_execution_pids_skips_pgid_when_missing_or_invalid():
    from agent_server.services.process_registry import ProcessRegistry

    registry = ProcessRegistry()
    registry._processes = {
        "no-pgid":    _make_proc_entry(1001),                       # no metadata.pgid
        "bad-pgid":   _make_proc_entry(2002, pgid=0),                # 0 = invalid
        "neg-pgid":   _make_proc_entry(3003, pgid=-1),               # negative = invalid
        "good-pgid":  _make_proc_entry(4004, pgid=4004),
    }

    pids = registry.active_execution_pids()
    # All pids in; only the good pgid (4004) gets appended.
    assert sorted(pids) == [1001, 2002, 3003, 4004, 4004]


def test_active_execution_pids_empty_registry():
    from agent_server.services.process_registry import ProcessRegistry

    assert ProcessRegistry().active_execution_pids() == []


# ---------------------------------------------------------------------------
# subprocess_pgroup._active_execution_pids_for_drain
# ---------------------------------------------------------------------------


def test_drain_helper_returns_registry_pids(monkeypatch):
    from agent_server.utils import subprocess_pgroup

    fake_registry = MagicMock()
    fake_registry.active_execution_pids.return_value = [1001, 1001, 2002]
    monkeypatch.setattr(
        "agent_server.services.process_registry.get_process_registry",
        lambda: fake_registry,
    )
    assert subprocess_pgroup._active_execution_pids_for_drain() == [1001, 1001, 2002]


def test_drain_helper_swallows_registry_errors(monkeypatch):
    """A registry hiccup at drain time must never crash the drain — the
    finally block runs even when readers are wedged."""
    from agent_server.utils import subprocess_pgroup

    def _raises():
        raise RuntimeError("registry exploded")

    fake_registry = MagicMock()
    fake_registry.active_execution_pids.side_effect = _raises
    monkeypatch.setattr(
        "agent_server.services.process_registry.get_process_registry",
        lambda: fake_registry,
    )
    assert subprocess_pgroup._active_execution_pids_for_drain() == []


# ---------------------------------------------------------------------------
# drain_reader_threads forwards the allowlist
# ---------------------------------------------------------------------------


def test_drain_reader_threads_forwards_extra_pids(monkeypatch):
    """The bug-fix proof: ``kill_cgroup_orphans`` is called with
    ``extra_pids=`` from the registry. Pre-#912 it was called bare,
    causing the false-kill of concurrent claude subprocesses."""
    from agent_server.utils import subprocess_pgroup

    # Record kill_cgroup_orphans calls.
    calls = []

    def _stub_kill(extra_pids=(), sweep_pid=None, dry_run=False):  # noqa: ARG001
        calls.append({"extra_pids": list(extra_pids), "sweep_pid": sweep_pid})
        return 0

    monkeypatch.setattr(subprocess_pgroup, "kill_cgroup_orphans", _stub_kill)
    monkeypatch.setattr(
        subprocess_pgroup,
        "_active_execution_pids_for_drain",
        lambda: [1001, 1001, 2002],
    )

    # A quickly-exiting subprocess + no reader threads to wedge: drain
    # short-circuits and falls straight into the finally sweep.
    proc = subprocess.Popen(["true"])
    proc.wait()

    asyncio.run(
        subprocess_pgroup.drain_reader_threads(
            proc, grace=1, post_kill_grace=1, pgid=None
        )
    )

    assert len(calls) == 1
    assert calls[0]["extra_pids"] == [1001, 1001, 2002]
