"""The cgroup orphan sweep refuses to run anywhere but a container root.

Run outside a container, `kill_cgroup_orphans()` reads the HOST's root
`/sys/fs/cgroup/cgroup.procs` and SIGKILLs everything it finds that it can:
it took down a developer's whole desktop session (`systemd --user`, twice)
and the GitHub Actions runner on every CI shard ("The runner has received a
shutdown signal") when a unit test's monkeypatch landed on the wrong module
copy and the real drain ran (trinity-enterprise#620, the #728 class). The
test-side fixes live in test_drain_bounded.py and tests/lint_sys_modules.py;
this is the production fence that does not depend on them: a container's
root cgroup lists PID 1, a host's never does.

Loaded straight from the file (no `agent_server` package registration), so
this file can never be the evictor it guards against.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

_SWEEP = Path(__file__).resolve().parents[2] / "docker" / "base-image" / "agent_server" / "utils" / "orphan_sweep.py"


@pytest.fixture
def sweep(monkeypatch):
    monkeypatch.syspath_prepend(str(_SWEEP.parent))
    spec = importlib.util.spec_from_file_location("orphan_sweep_fence_2845", _SWEEP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # Nothing in this file may ever signal a real process.
    sent = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append((pid, sig)))
    mod._sent = sent
    return mod


def test_pid_1_is_the_container_root_signal(sweep):
    assert sweep.cgroup_is_container_root([1, 7, 42]) is True
    assert sweep.cgroup_is_container_root([7, 42]) is False
    assert sweep.cgroup_is_container_root([]) is False


def test_a_host_root_cgroup_is_never_swept(sweep, monkeypatch, caplog):
    """The host shape: a populated list without PID 1 (systemd keeps itself
    in init.scope). Nothing is killed, even a pid outside the allowlist."""
    monkeypatch.setattr(sweep, "read_cgroup_procs", lambda *a, **k: [4242, 4243, os.getpid()])
    monkeypatch.setattr(sweep, "resolve_allowlist", lambda *a, **k: {os.getpid()})
    with caplog.at_level("WARNING"):
        killed = sweep.kill_cgroup_orphans()
    assert killed == 0
    assert sweep._sent == []
    assert any("not a container root" in r.getMessage() for r in caplog.records)


def test_a_container_root_cgroup_is_still_swept(sweep, monkeypatch):
    """The fence must not disable the real thing: with PID 1 present the
    orphan outside the allowlist is (dry-run) counted exactly as before."""
    monkeypatch.setattr(sweep, "read_cgroup_procs", lambda *a, **k: [1, 4242, os.getpid()])
    monkeypatch.setattr(sweep, "resolve_allowlist", lambda *a, **k: {1, os.getpid()})
    assert sweep.kill_cgroup_orphans(dry_run=True) == 1
    assert sweep._sent == []  # dry_run
    assert sweep.kill_cgroup_orphans() == 1
    assert [pid for pid, _ in sweep._sent] == [4242]


def test_an_unreadable_cgroup_file_still_skips(sweep, monkeypatch):
    monkeypatch.setattr(sweep, "read_cgroup_procs", lambda *a, **k: None)
    assert sweep.kill_cgroup_orphans() == 0
    assert sweep._sent == []
