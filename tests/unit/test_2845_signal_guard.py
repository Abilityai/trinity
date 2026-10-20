"""The unit suite's process-signal safety net (trinity-enterprise#620).

A test that reaches the real `os.kill` / `os.killpg` against anything but this
process or a subprocess it spawned fails loudly instead of taking the CI
runner or a developer's desktop session down with it.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys

import pytest

import signal_guard as sg

pytest_plugins = ["pytester"]

# Where the session cgroup is unreadable (macOS, some sandboxes) `install()`
# returns False and the REAL os.kill/os.killpg stay in place — the refusal
# tests below would then deliver their signals for real, SIGTERMing this
# session's own process group (pytest, xdist, and the shell above them).
pytestmark = pytest.mark.skipif(
    os.kill is not sg.guarded_kill,
    reason="signal guard not installed (no readable /proc cgroup on this host)",
)


@pytest.fixture(autouse=True)
def _own_violations():
    """Refusals made ON PURPOSE here must not fail this test at teardown."""
    yield
    sg.consume_violations()


def test_the_guard_is_installed_in_the_unit_suite():
    assert os.kill is sg.guarded_kill
    assert os.killpg is sg.guarded_killpg


def test_a_child_of_the_session_can_be_signalled():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        os.kill(child.pid, signal.SIGTERM)
        assert child.wait(timeout=10) != 0
    finally:
        if child.poll() is None:
            child.kill()
    assert sg.consume_violations() == []


def test_a_childs_process_group_can_be_signalled():
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )
    try:
        os.killpg(os.getpgid(child.pid), signal.SIGKILL)
        child.wait(timeout=10)
    finally:
        if child.poll() is None:
            child.kill()
    assert sg.consume_violations() == []


def test_a_reparented_setsid_child_stays_ours(tmp_path):
    """The case a parent-chain guard gets wrong: a grandchild that calls
    setsid and is reparented to init (ppid=1) is no longer a descendant by
    parent chain, but it stays in the session's cgroup, so it is still ours
    to kill. This is exactly what the orphan sweep in the real code does."""
    code = "import os,time; os.setsid(); time.sleep(60)"
    child = subprocess.Popen([sys.executable, "-c", code])
    # let it setsid, then reap the direct parent shell so it reparents
    import time as _t; _t.sleep(0.5)
    grand_pgid = os.getpgid(child.pid)
    try:
        # same cgroup as us → allowed, no refusal recorded
        os.killpg(grand_pgid, signal.SIGKILL)
    finally:
        if child.poll() is None:
            child.kill()
    assert sg.consume_violations() == []


def test_the_existence_probe_is_always_forwarded():
    """Signal 0 delivers nothing — it is how liveness is checked — so it is
    forwarded to the real call for ANY pid and never refused, whatever that
    real call then raises (EPERM for PID 1 as an unprivileged user)."""
    try:
        os.kill(1, 0)
    except OSError:
        pass
    assert not isinstance(sys.exc_info()[1], sg.ForeignProcessSignal)
    assert sg.consume_violations() == []


def test_a_process_outside_the_session_cgroup_is_refused_and_recorded():
    """PID 1 (systemd) lives in the root cgroup; the test session lives in a
    scope beneath it. Signalling out of the session cgroup is refused — the
    class of kill that took the desktop session and the CI runner down.
    (Skipped where PID 1 happens to share our cgroup, e.g. a bare container
    running pytest as PID 1's own tree — not how CI or a dev host looks.)"""
    if sg._cgroup_of(1) == sg._session_cgroup:
        pytest.skip("PID 1 shares the session cgroup (bare-container harness)")
    with pytest.raises(sg.ForeignProcessSignal):
        os.kill(1, signal.SIGTERM)
    refused = sg.consume_violations()
    assert len(refused) == 1 and "os.kill(1, " in refused[0]


def test_our_own_process_group_is_refused():
    """The historical failure: `killpg` on the session's own group kills the
    pytest worker, xdist controller, and the runner/terminal above them."""
    with pytest.raises(sg.ForeignProcessSignal):
        os.killpg(os.getpgrp(), signal.SIGTERM)
    assert len(sg.consume_violations()) == 1


def test_a_swallowed_refusal_still_fails_the_test(pytester):
    """The production drain catches `Exception`; the autouse fixture in
    tests/unit/conftest.py turns the recorded refusal into a failure anyway."""
    pytester.makepyfile(
        test_swallow='''
import os, signal, sys
sys.path.insert(0, %r)
import signal_guard as sg

pytest_plugins = ["pytester"]
sg.install()
import pytest

@pytest.fixture(autouse=True)
def _guard():
    sg.consume_violations()
    yield
    refused = sg.consume_violations()
    if refused:
        pytest.fail("refused: " + refused[0])

def test_swallows():
    try:
        os.kill(1, signal.SIGTERM)
    except Exception:
        pass  # the production shape
'''
        % os.path.dirname(os.path.dirname(__file__))
    )
    result = pytester.runpytest("-p", "no:randomly", "-p", "no:cacheprovider")
    # `pytest.fail` in a fixture TEARDOWN reports as an error, not a failure —
    # equally loud, equally red, and it names the test.
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(["*refused: os.kill(1, 15) refused*"])


def test_the_failure_names_the_cause():
    with pytest.raises(sg.ForeignProcessSignal) as e:
        os.kill(1, signal.SIGTERM)
    assert "wrong module copy" in str(e.value) and "trinity-enterprise#620" in str(e.value)
    sg.consume_violations()


def test_a_member_that_exited_mid_walk_is_gone_not_foreign(monkeypatch):
    """The CI flake behind `test_subprocess_pgroup` (regression-diff reds on
    #2920, #2924, #2927, the 09-18 train): the harness parent exits — that IS
    the scenario under test — while `guarded_killpg` is still reading the
    group's cgroups. `_cgroup_of` answers None for the vanished pid, and the
    group read as "contains a process outside the session cgroup", refusing a
    kill of a group that was entirely ours a millisecond earlier.

    Simulate the race: the walk returns a live child plus a pid that no
    longer exists. The kill must go through — the dead pid is not a member —
    and nothing is recorded as a violation."""
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )
    ghost = subprocess.Popen([sys.executable, "-c", "pass"])
    ghost.wait(timeout=10)                       # reaped: /proc/<pid> is gone
    pgid = os.getpgid(child.pid)
    real_walk = sg._pgid_members
    monkeypatch.setattr(sg, "_pgid_members", lambda g: real_walk(g) + [ghost.pid] if g == pgid else real_walk(g))
    try:
        os.killpg(pgid, signal.SIGKILL)
        child.wait(timeout=10)
    finally:
        if child.poll() is None:
            child.kill()
    assert sg.consume_violations() == []


def test_a_live_pid_with_an_unreadable_cgroup_is_still_foreign(monkeypatch):
    """The fix above must not widen the hole: a pid that EXISTS but whose
    cgroup cannot be read stays foreign (fail closed), exactly as before."""
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )
    pgid = os.getpgid(child.pid)
    real_cg = sg._cgroup_of
    monkeypatch.setattr(sg, "_cgroup_of", lambda pid: None if pid == child.pid else real_cg(pid))
    try:
        with pytest.raises(sg.ForeignProcessSignal):
            os.killpg(pgid, signal.SIGKILL)
    finally:
        monkeypatch.undo()                       # the cleanup kill must not be refused too
        child.kill(); child.wait(timeout=10)
    refused = sg.consume_violations()
    assert len(refused) == 1 and "outside the session cgroup" in refused[0]
