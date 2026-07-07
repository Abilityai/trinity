"""Tests for #1501 — the orphan sweep must not SIGKILL in-flight brain-orb
hook runs (and other registry-vouched transient agent-server subprocesses).

Root cause: a hook spawned by ``_run_hook`` (routers/brain_orb.py) is a live
agent-server child, but ``resolve_allowlist``'s hard-protect walk only goes
UP the parent chain — so the periodic 30s sweep killed any hook that
straddled a sweep boundary (the 180s ``refresh`` reliably did → exit -9 →
502).

Four surfaces:

  * ``ProcessRegistry.add_transient_pid`` / ``remove_transient_pid`` /
    the transient entries riding along in ``active_execution_pids()``
    (the #912 single canonical sweep-allowlist source) with a lazy
    TTL backstop.
  * ``brain_orb._run_hook`` — registers the hook pid for the duration of
    the run and unregisters in a ``finally`` on every exit path
    (success, non-zero exit, timeout-kill, invalid JSON, cancellation);
    registration is fail-open.
  * ``orphan_allowlist.resolve_allowlist`` — a user cmdline-pattern match
    now protects the matched daemon's descendant tree, not just the bare
    pid (the issue's operator-workaround caveat). Platform-essential
    patterns stay bare-pid.
  * ``gemini_runtime._sweep_allowlist_pids`` — Gemini's formerly-blanket
    post-turn sweeps now forward the registry allowlist (fail-open).

Follows the sys.path-injection harness of test_912 / test_1153; the
allowlist tests drive a synthetic /proc tree, never the real one.
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
import time
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

_REPO = Path(__file__).resolve().parents[2]
_BASE_IMAGE = _REPO / "docker" / "base-image"
if str(_BASE_IMAGE) not in sys.path:
    sys.path.insert(0, str(_BASE_IMAGE))

from agent_server.routers import brain_orb as asbo  # noqa: E402
from agent_server.services.process_registry import (  # noqa: E402
    ProcessRegistry,
    TRANSIENT_PID_TTL_SECONDS,
)
from agent_server.utils import orphan_allowlist as oa  # noqa: E402

# Production code resolves the registry via CALL-TIME lazy imports
# (`from ..services.process_registry import get_process_registry` in
# brain_orb / orphan_sweeper / gemini_runtime). Sibling test modules
# (test_agent_server_auto_sync.py, test_git_status_dual_ahead_behind.py)
# evict `agent_server*` from sys.modules at module level during collection,
# so an attribute patch on the module object THIS file imported would be
# invisible to the fresh module a later lazy import re-creates — the #1446
# stub-leak class (this exact mode failed 7/27 of these tests in the full
# CI suite while passing in isolation). Fix per that lesson: OWN the
# sys.modules key for the test's duration via monkeypatch.setitem
# (auto-restored, last-write-wins over any prior eviction/replacement).
_PR_KEY = "agent_server.services.process_registry"


def _own_registry_module(monkeypatch, get_process_registry):
    mod = types.ModuleType(_PR_KEY)
    mod.get_process_registry = get_process_registry
    monkeypatch.setitem(sys.modules, _PR_KEY, mod)


def _exec_entry(pid: int, *, poll_result=None, pgid: int | None = None) -> dict:
    """Registry-shaped execution entry (mirrors test_912's helper)."""
    process = MagicMock(spec=subprocess.Popen)
    process.pid = pid
    process.poll.return_value = poll_result
    return {
        "process": process,
        "started_at": __import__("datetime").datetime.utcnow(),
        "metadata": {"pgid": pgid} if pgid is not None else {},
    }


# ---------------------------------------------------------------------------
# ProcessRegistry transient-pid API
# ---------------------------------------------------------------------------


def test_add_transient_pid_included_in_allowlist_source():
    registry = ProcessRegistry()
    registry.add_transient_pid(4242)
    assert 4242 in registry.active_execution_pids()


def test_transient_coexists_with_execution_pids():
    registry = ProcessRegistry()
    registry._processes = {"exec-a": _exec_entry(1001, pgid=3003)}
    registry.add_transient_pid(4242)
    pids = registry.active_execution_pids()
    assert {1001, 3003, 4242}.issubset(set(pids))


def test_remove_transient_pid_drops_it():
    registry = ProcessRegistry()
    registry.add_transient_pid(4242)
    registry.remove_transient_pid(4242)
    assert 4242 not in registry.active_execution_pids()


def test_remove_absent_and_double_remove_are_noops():
    registry = ProcessRegistry()
    registry.remove_transient_pid(999)  # never added
    registry.add_transient_pid(4242)
    registry.remove_transient_pid(4242)
    registry.remove_transient_pid(4242)  # double remove
    assert registry.active_execution_pids() == []


def test_expired_transient_evicted_lazily():
    registry = ProcessRegistry()
    registry.add_transient_pid(4242)
    # Entries store an expiry DEADLINE; force it into the past and read —
    # the read both omits and evicts it (collect-then-delete, like the
    # registry's other TTL maps).
    registry._transient_pids[4242] = time.time() - 1
    assert 4242 not in registry.active_execution_pids()
    assert 4242 not in registry._transient_pids


def test_transient_window_derived_from_ttl_seconds():
    """The protection window must scale with the caller's subprocess budget —
    a fixed TTL below a hook's timeout would evict mid-run (finder C1)."""
    registry = ProcessRegistry()
    now = time.time()
    registry.add_transient_pid(4242, ttl_seconds=600)
    assert registry._transient_pids[4242] > now + TRANSIENT_PID_TTL_SECONDS
    assert registry._transient_pids[4242] <= now + 600 + 5


@pytest.mark.parametrize("bad_ttl", [0, -5, float("nan"), float("inf"), None])
def test_transient_window_bad_ttl_falls_back_to_default(bad_ttl):
    registry = ProcessRegistry()
    now = time.time()
    registry.add_transient_pid(4242, ttl_seconds=bad_ttl)
    deadline = registry._transient_pids[4242]
    assert now < deadline <= now + TRANSIENT_PID_TTL_SECONDS + 5


def test_exclude_execution_id_never_drops_transients():
    registry = ProcessRegistry()
    registry._processes = {"exec-a": _exec_entry(1001)}
    registry.add_transient_pid(4242)
    pids = registry.active_execution_pids(exclude_execution_id="exec-a")
    assert 1001 not in pids
    assert 4242 in pids


def test_invalid_transient_pids_ignored():
    registry = ProcessRegistry()
    registry.add_transient_pid(0)
    registry.add_transient_pid(-5)
    registry.add_transient_pid(None)  # type: ignore[arg-type]
    assert registry.active_execution_pids() == []
    assert registry._transient_pids == {}


def test_periodic_sweeper_source_includes_transients(monkeypatch):
    """The 30s periodic sweep reads the same canonical source — a registered
    hook pid must reach it (this is the exact #1501 kill path)."""
    from agent_server.services import orphan_sweeper as sweeper

    fresh = ProcessRegistry()
    fresh.add_transient_pid(4242)
    _own_registry_module(monkeypatch, lambda: fresh)
    assert 4242 in sweeper._active_execution_pids()


# ---------------------------------------------------------------------------
# _run_hook registers for the duration of the run, unregisters on every path
# ---------------------------------------------------------------------------


class _RecordingRegistry:
    def __init__(self, *, raise_on_add: bool = False):
        self.calls: list[tuple[str, int]] = []
        self.ttls: list[float | None] = []
        self.raise_on_add = raise_on_add

    def add_transient_pid(self, pid: int, *, ttl_seconds: float | None = None):
        if self.raise_on_add:
            raise RuntimeError("registry unavailable")
        self.calls.append(("add", pid))
        self.ttls.append(ttl_seconds)

    def remove_transient_pid(self, pid: int):
        self.calls.append(("remove", pid))


def _write_hook(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


@pytest.fixture
def hook_env(tmp_path, monkeypatch):
    """Tmp hook dir + recording registry stub behind the lazy import."""
    monkeypatch.setattr(asbo, "_HOME", tmp_path)  # subprocess cwd must exist
    stub = _RecordingRegistry()
    _own_registry_module(monkeypatch, lambda: stub)
    return tmp_path, stub


def _assert_paired(stub: _RecordingRegistry):
    """One add followed by one remove, same positive pid."""
    assert [op for op, _ in stub.calls] == ["add", "remove"]
    add_pid = stub.calls[0][1]
    assert stub.calls[1][1] == add_pid
    assert add_pid > 0
    return add_pid


def test_run_hook_success_registers_then_unregisters(hook_env):
    tmp, stub = hook_env
    hook = tmp / "hook"
    _write_hook(hook, '#!/bin/sh\necho \'{"ok": true}\'\n')
    result = asyncio.run(asbo._run_hook(hook, timeout=10))
    assert result == {"ok": True}
    _assert_paired(stub)
    # The protection window is derived from this run's budget (+headroom).
    assert stub.ttls == [10 + 60]


def test_run_hook_nonzero_exit_unregisters(hook_env):
    tmp, stub = hook_env
    hook = tmp / "hook"
    _write_hook(hook, '#!/bin/sh\necho "{}"\nexit 3\n')
    with pytest.raises(HTTPException) as ei:
        asyncio.run(asbo._run_hook(hook, timeout=10))
    assert ei.value.status_code == 502
    _assert_paired(stub)


def test_run_hook_timeout_kill_unregisters(hook_env):
    tmp, stub = hook_env
    hook = tmp / "hook"
    _write_hook(hook, '#!/bin/sh\nsleep 5\necho "{}"\n')
    with pytest.raises(HTTPException) as ei:
        asyncio.run(asbo._run_hook(hook, timeout=0.5))
    assert ei.value.status_code == 504
    _assert_paired(stub)


def test_run_hook_invalid_json_unregisters(hook_env):
    tmp, stub = hook_env
    hook = tmp / "hook"
    _write_hook(hook, '#!/bin/sh\necho "not json at all"\n')
    with pytest.raises(HTTPException) as ei:
        asyncio.run(asbo._run_hook(hook, timeout=10))
    assert ei.value.status_code == 502
    _assert_paired(stub)


def test_run_hook_registration_failure_is_fail_open(tmp_path, monkeypatch):
    """A registry error must never break the hook call — the hook simply
    runs sweep-unprotected (pre-#1501 behavior)."""
    monkeypatch.setattr(asbo, "_HOME", tmp_path)
    stub = _RecordingRegistry(raise_on_add=True)
    _own_registry_module(monkeypatch, lambda: stub)
    hook = tmp_path / "hook"
    _write_hook(hook, '#!/bin/sh\necho \'{"ok": true}\'\n')
    result = asyncio.run(asbo._run_hook(hook, timeout=10))
    assert result == {"ok": True}
    # add raised → registry treated as absent → no remove call either.
    assert stub.calls == []


def test_run_hook_cancellation_unregisters_and_kills(hook_env):
    """Client-disconnect (task cancellation) must unregister AND kill the
    hook deterministically — not leave an unprotected child running until
    a random sweep SIGKILLs it mid-write."""
    tmp, stub = hook_env
    beat = tmp / "beats.log"
    hook = tmp / "hook"
    # Heartbeat loop: appends ~10 lines/second for up to 30s, then exits.
    _write_hook(
        hook,
        '#!/bin/sh\ni=0\nwhile [ $i -lt 300 ]; do\n'
        f'  echo tick >> "{beat}"\n'
        '  i=$((i+1))\n  sleep 0.1\ndone\necho "{}"\n',
    )

    async def _cancel_mid_run():
        task = asyncio.create_task(asbo._run_hook(hook, timeout=30))
        await asyncio.sleep(0.6)  # let the hook start beating
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_cancel_mid_run())
    _assert_paired(stub)
    # The kill is what stops the heartbeat: after a settle window the file
    # must stop growing. (Without the kill it beats for 30s.)
    time.sleep(0.4)
    size_1 = beat.stat().st_size if beat.exists() else 0
    time.sleep(0.6)
    size_2 = beat.stat().st_size if beat.exists() else 0
    assert size_1 == size_2, "hook kept writing after cancellation — not killed"


# ---------------------------------------------------------------------------
# resolve_allowlist: user cmdline-pattern matches protect descendants
# ---------------------------------------------------------------------------

# Synthetic container PID namespace:
#   1    ppid 0    init
#   50   ppid 1    agent-server (sweep caller)
#   500  ppid 1    operator daemon  (matches user pattern)
#   501  ppid 500  daemon worker    (different cmdline — the #1501 caveat)
#   502  ppid 501  worker grandchild
#   600  ppid 1    leaked orphan    (must stay killable)
#   700  ppid 1    platform keep-alive "tail -f /dev/null"
#   701  ppid 700  synthetic child of the keep-alive (must NOT be protected)
_TREE_PPID = {1: 0, 50: 1, 500: 1, 501: 500, 502: 501, 600: 1, 700: 1, 701: 700}
_TREE_COMM = {1: "startup.sh", 50: "python3", 500: "python3", 501: "node",
              502: "sh", 600: "python3", 700: "tail", 701: "sh"}
_TREE_CMD = {
    1: "/app/startup.sh",
    50: "python3 -m uvicorn agent_server.main:app",
    500: "python3 /opt/moltbook-http-mcp serve",
    501: "node worker.js",
    502: "sh -c compress",
    600: "python3 leaked.py",
    700: "tail -f /dev/null",
    701: "sh -c essential-child",
}


@pytest.fixture
def fake_proc(monkeypatch):
    monkeypatch.setattr(oa.os, "listdir",
                        lambda p: [str(pid) for pid in _TREE_PPID])
    monkeypatch.setattr(oa, "_read_ppid", lambda pid: _TREE_PPID.get(pid))
    monkeypatch.setattr(oa, "_read_comm", lambda pid: _TREE_COMM.get(pid))
    monkeypatch.setattr(oa, "_read_cmdline", lambda pid: _TREE_CMD.get(pid))
    return monkeypatch


def test_pattern_match_protects_descendant_tree(fake_proc):
    allow = oa.resolve_allowlist(
        50, cmdline_patterns=["*moltbook-http-mcp*"]
    )
    # Daemon + its whole worker subtree survive…
    assert {500, 501, 502}.issubset(allow)
    # …while an unrelated leaked orphan stays killable.
    assert 600 not in allow


def test_pattern_descendants_do_not_shield_reparented_leftovers(monkeypatch):
    """Daemon exited: its worker reparented to PID 1 → falls out of the
    descendant walk → reaped on the next sweep (self-healing preserved)."""
    tree_ppid = {1: 0, 50: 1, 501: 1}  # 500 gone; 501 reparented to init
    tree_cmd = {1: "/app/startup.sh", 50: "python3 agent-server",
                501: "node worker.js"}
    monkeypatch.setattr(oa.os, "listdir",
                        lambda p: [str(pid) for pid in tree_ppid])
    monkeypatch.setattr(oa, "_read_ppid", lambda pid: tree_ppid.get(pid))
    monkeypatch.setattr(oa, "_read_comm", lambda pid: None)
    monkeypatch.setattr(oa, "_read_cmdline", lambda pid: tree_cmd.get(pid))
    allow = oa.resolve_allowlist(50, cmdline_patterns=["*moltbook-http-mcp*"])
    assert 501 not in allow


def test_platform_essential_patterns_stay_bare_pid(fake_proc):
    allow = oa.resolve_allowlist(50, cmdline_patterns=[])
    assert 700 in allow       # keep-alive itself protected
    assert 701 not in allow   # …but NOT its children (would shield orphans)


def test_transient_extra_pid_protects_hook_and_children(fake_proc):
    """End-to-end shape of the #1501 fix: the hook pid arrives via
    extra_pids (from the transient registry) and its subtree survives."""
    # Reuse 500/501/502 as hook + children; no patterns this time.
    allow = oa.resolve_allowlist(50, extra_pids=[500], cmdline_patterns=[])
    assert {500, 501, 502}.issubset(allow)
    assert 600 not in allow


# ---------------------------------------------------------------------------
# Gemini post-turn sweeps forward the registry allowlist (formerly blanket)
# ---------------------------------------------------------------------------


def test_gemini_sweep_allowlist_pids_reads_registry(monkeypatch):
    from agent_server.services import gemini_runtime as gr

    fresh = ProcessRegistry()
    fresh._processes = {"exec-a": _exec_entry(1001)}
    fresh.add_transient_pid(4242)
    _own_registry_module(monkeypatch, lambda: fresh)
    pids = gr._sweep_allowlist_pids()
    assert {1001, 4242}.issubset(set(pids))


def test_gemini_sweep_allowlist_pids_fail_open(monkeypatch):
    from agent_server.services import gemini_runtime as gr

    def _boom():
        raise RuntimeError("registry unavailable")

    _own_registry_module(monkeypatch, _boom)
    assert gr._sweep_allowlist_pids() == []


def test_gemini_has_no_blanket_sweep_calls_left():
    """Static guard: every live Gemini sweep call must forward extra_pids."""
    src = (_BASE_IMAGE / "agent_server" / "services" / "gemini_runtime.py").read_text()
    # A live call line starts (post-indent) with the call or an assignment of
    # it — comment/docstring mentions ("``kill_cgroup_orphans()``…") don't.
    live_calls = [
        line for line in src.splitlines()
        if line.lstrip().startswith(
            ("kill_cgroup_orphans(", "killed = kill_cgroup_orphans(")
        )
    ]
    assert len(live_calls) == 3, f"expected the 3 Gemini sweep call sites, got: {live_calls}"
    for line in live_calls:
        assert "extra_pids=" in line, f"blanket sweep call reintroduced: {line.strip()}"
