"""#4064 — a terminate never signals the agent server's own process group.

The Gemini runtime spawned its child without ``start_new_session`` and
registered it without a ``pgid``. ``ProcessRegistry.terminate()`` (and the
kill-at-spawn of a cancel that landed while pending) then called
``signal_process_tree(process, sig, pgid=None)``, which looked the group up
lazily: ``os.getpgid(child)`` is the agent server's own group for such a
child, and ``_signal_group_or_process`` issued ``os.killpg`` on it. The agent
server runs in the container's PID-1 shell group (``startup.sh``), so that
group kill also took down ``tail -f /dev/null`` and stopped the container.

Two fixes, both pinned here:

1. ``_signal_group_or_process`` signals a group only when the process leads it
   (``pgid == process.pid``), it is positive, and it is not the caller's own
   group. Anything else falls back to the single pid.
2. Every ``Popen`` in ``agent_server/services`` whose handle is registered with
   the process registry passes ``start_new_session=True`` and registers the
   spawn-captured ``pgid`` (AST guard), so the fence is a backstop and not the
   mechanism.

The signal tests use real disposable children and replace ``os.killpg`` with a
recorder that NEVER forwards a refused group: the conftest signal guard is a
pass-through where ``/proc/self/cgroup`` is unreadable, so a forwarding spy
with the fence removed would signal this pytest session. The assertions read
the recorder, not just the child's fate: the pid fallback kills the child with
or without the fence.

Module under test: docker/base-image/agent_server/utils/subprocess_pgroup.py,
docker/base-image/agent_server/services/process_registry.py,
docker/base-image/agent_server/services/gemini_runtime.py
"""
from __future__ import annotations

import ast
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from agent_server.services import process_registry as registry_module
from agent_server.utils import subprocess_pgroup

pytestmark = pytest.mark.unit

_SERVICES = Path(__file__).resolve().parents[2] / "docker" / "base-image" / "agent_server" / "services"
_SLEEPER = [sys.executable, "-c", "import time; time.sleep(60)"]


class _Refused(AssertionError):
    pass


@pytest.fixture
def killpg_log(monkeypatch):
    """Record every ``os.killpg``. Forward only to groups this test spawned."""
    own_group = os.getpgrp()
    real_killpg = os.killpg
    log = {"calls": [], "own_group": own_group, "allowed": set()}

    def killpg(pgid, sig):
        log["calls"].append(pgid)
        if pgid not in log["allowed"]:
            raise _Refused(f"os.killpg({pgid}, {sig}) is not a group this test spawned")
        return real_killpg(pgid, sig)

    monkeypatch.setattr(os, "killpg", killpg)
    return log


@pytest.fixture
def children():
    spawned = []

    def spawn(*, new_session):
        child = subprocess.Popen(_SLEEPER, start_new_session=new_session)
        spawned.append(child)
        return child

    yield spawn
    for child in spawned:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)


@pytest.fixture
def registry(monkeypatch):
    value = registry_module.ProcessRegistry()
    # A unit test owns only its children, never the surrounding cgroup.
    monkeypatch.setattr(registry_module, "kill_cgroup_orphans", lambda **kw: 0)
    monkeypatch.setattr(subprocess_pgroup, "kill_cgroup_orphans", lambda **kw: 0)
    return value


def _assert_killed(child, sig=signal.SIGKILL):
    child.wait(timeout=5)
    assert child.returncode == -sig, f"the child itself was not signalled (returncode {child.returncode})"


# --- the fence ---------------------------------------------------------------


def test_a_lazily_looked_up_group_that_is_ours_is_never_signalled(killpg_log, children):
    child = children(new_session=False)
    assert os.getpgid(child.pid) == killpg_log["own_group"], "control: the child shares our group"

    subprocess_pgroup.signal_process_tree(child, signal.SIGKILL, pgid=None)

    assert killpg_log["own_group"] not in killpg_log["calls"]
    _assert_killed(child)


@pytest.mark.parametrize("pgid", ["own", 0, -1])
def test_an_explicit_own_zero_or_negative_group_is_refused(killpg_log, children, pgid):
    child = children(new_session=False)
    pgid = killpg_log["own_group"] if pgid == "own" else pgid

    subprocess_pgroup.signal_process_tree(child, signal.SIGKILL, pgid=pgid)

    assert killpg_log["calls"] == []
    _assert_killed(child)


def test_a_group_the_process_does_not_lead_is_refused(killpg_log, children):
    """A stale pgid (reaped pid reused, another execution's group) is never signalled."""
    target = children(new_session=False)
    bystander = children(new_session=True)

    subprocess_pgroup.signal_process_tree(target, signal.SIGKILL, pgid=bystander.pid)

    assert killpg_log["calls"] == []
    _assert_killed(target)
    assert bystander.poll() is None, "another process's group was signalled"


def test_terminate_process_group_refuses_our_own_group(killpg_log, children):
    child = children(new_session=False)

    subprocess_pgroup.terminate_process_group(child, graceful_timeout=0)

    assert killpg_log["own_group"] not in killpg_log["calls"]
    child.wait(timeout=5)
    assert child.returncode in (-signal.SIGTERM, -signal.SIGKILL)


def test_a_group_leader_is_still_signalled_as_a_group(killpg_log, children):
    """Positive control: the fence must not disable group kills for a session leader."""
    child = children(new_session=True)
    killpg_log["allowed"].add(child.pid)

    subprocess_pgroup.signal_process_tree(child, signal.SIGKILL, pgid=child.pid)

    assert killpg_log["calls"] == [child.pid]
    _assert_killed(child)


# --- the registry entry points ------------------------------------------------


def test_terminate_of_an_entry_without_a_pgid_never_signals_our_group(killpg_log, children, registry):
    child = children(new_session=False)
    registry.register("4064-terminate", child, metadata={"type": "chat", "runtime": "gemini"})

    result = registry.terminate("4064-terminate", graceful_timeout=0)

    assert killpg_log["own_group"] not in killpg_log["calls"]
    child.wait(timeout=5)
    assert child.returncode in (-signal.SIGINT, -signal.SIGKILL)
    assert result["success"] is True


def test_kill_at_spawn_of_a_pending_cancel_never_signals_our_group(killpg_log, children, registry):
    registry.register_pending("4064-pending", timeout_seconds=60)
    assert registry.terminate("4064-pending")["reason"] == "cancelled_before_start"
    child = children(new_session=False)

    registry.register("4064-pending", child, metadata={"type": "task", "runtime": "gemini"})

    assert killpg_log["own_group"] not in killpg_log["calls"]
    _assert_killed(child)


# --- every registered spawn leads its own group (AST guard) -------------------


def _call_name(node):
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _registered_spawn_sites():
    """(file, function, Popen call, [register calls]) for every function in
    agent_server/services that both spawns with Popen and registers."""
    sites = []
    for path in sorted(_SERVICES.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)]
            popens = [c for c in calls if _call_name(c) == "Popen"]
            registers = [
                c for c in calls
                if _call_name(c) == "register" and isinstance(c.func, ast.Attribute)
            ]
            if popens and registers:
                for popen in popens:
                    sites.append((path.name, fn.name, popen, registers))
    return sites


def test_the_guard_sees_every_runtime():
    """Control: a guard that walks nothing passes vacuously."""
    files = {name for name, *_ in _registered_spawn_sites()}
    assert {"claude_code.py", "codex_runtime.py", "gemini_runtime.py", "headless_executor.py"} <= files


@pytest.mark.parametrize(
    "site", _registered_spawn_sites(), ids=lambda s: f"{s[0]}::{s[1]}:{s[2].lineno}"
)
def test_a_registered_spawn_starts_its_own_session_and_registers_its_pgid(site):
    name, fn, popen, registers = site
    new_session = {k.arg: k.value for k in popen.keywords}.get("start_new_session")
    assert isinstance(new_session, ast.Constant) and new_session.value is True, (
        f"{name}::{fn} registers a Popen child without start_new_session=True; "
        "it would share the agent server's process group (#4064)"
    )
    for call in registers:
        metadata = {k.arg: k.value for k in call.keywords}.get("metadata")
        keys = {k.value for k in getattr(metadata, "keys", []) if isinstance(k, ast.Constant)}
        assert "pgid" in keys, (
            f"{name}::{fn}:{call.lineno} registers without the spawn-captured pgid (#4064)"
        )
