"""Process-signal safety net for the unit suite (trinity-enterprise#620).

Several unit files exercise the agent server's process-killing code —
`terminate_process_group`, `_drain_bounded`, the cgroup orphan sweep — with
mocked processes, relying on monkeypatches to keep the real `os.kill` /
`os.killpg` from running. Those patches are fragile: a later-collected file
that re-registers the `agent_server` package leaves a second module copy, the
patch lands on the copy, and the REAL killer runs. That SIGKILLed a
developer's whole desktop session (twice) and the GitHub Actions runner on
every CI shard before it was understood (#728 class).

This module wraps `os.kill` and `os.killpg` themselves — `os` is one module,
so no package copy can bypass it — and applies two rules:

  1. NEVER signal the test session itself: our own process group, this
     process, or any ancestor of it. Those kills take pytest / the xdist
     controller / the runner shell down and are never a legitimate test.
  2. Otherwise, only signal a process in the SESSION'S OWN CGROUP (or a
     cgroup nested under it). A subprocess a test spawned stays in that
     cgroup even after `setsid` reparents it to init — the case a
     parent-chain check gets wrong — while a foreign process (systemd
     --user, PID 1, an unrelated container) sits in a different cgroup.
     This is the same membership the production sweep keys on.

A refused signal raises `ForeignProcessSignal` (NOT an `OSError`, which the
production code swallows) AND is recorded, so the autouse fixture in
tests/unit/conftest.py fails the test loudly even when the caller swallowed
the exception.

Linux/cgroup-v2 only. Where `/proc/self/cgroup` is unreadable the guard is a
pass-through (it cannot answer the membership question, and a guard that
guesses would refuse legitimate child cleanup).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional, Set

_PROC = Path("/proc")
_real_kill = os.kill
_real_killpg = os.killpg
_installed = False
_session_cgroup: Optional[str] = None

#: Every refused signal since the last `consume_violations()` — the autouse
#: fixture turns a non-empty list into a test failure.
violations: List[str] = []


class ForeignProcessSignal(RuntimeError):
    """A test tried to signal a process outside the test session."""


def _cgroup_of(pid: int) -> Optional[str]:
    """The unified (v2) cgroup path of `pid`, e.g. `/user.slice/....scope`."""
    try:
        for line in (_PROC / str(pid) / "cgroup").read_text().splitlines():
            # v2 unified line: "0::/path"
            if line.startswith("0::"):
                return line[3:]
    except OSError:
        return None
    return None


def _in_session_cgroup(pid: int) -> bool:
    if _session_cgroup is None:
        return True  # no membership question we can answer → pass through
    cg = _cgroup_of(pid)
    if cg is None:
        return False
    return cg == _session_cgroup or cg.startswith(_session_cgroup.rstrip("/") + "/")


def _is_gone(pid: int) -> bool:
    """The process no longer exists (or is a zombie the group kill cannot
    reach anyway). Distinct from "cgroup unreadable": a LIVE pid whose cgroup
    we cannot read is treated as foreign (fail closed); a pid that vanished
    between the `/proc` walk and the membership read is simply not a member.

    This is the flake behind `test_subprocess_pgroup` on CI: the harness's
    parent exits (that is the scenario) while `guarded_killpg` is still
    reading its cgroup, `_cgroup_of` answers None, and the group read as
    "contains a process outside the session cgroup" — refusing a kill of a
    group that was entirely ours a millisecond earlier."""
    if not (_PROC / str(pid)).exists():
        return True
    try:
        for line in (_PROC / str(pid) / "status").read_text().splitlines():
            if line.startswith("State:"):
                return line.split()[1] == "Z"
    except (OSError, IndexError):
        return True
    return False


def _ppid(pid: int) -> Optional[int]:
    try:
        for line in (_PROC / str(pid) / "status").read_text().splitlines():
            if line.startswith("PPid:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def _is_self_or_ancestor(pid: int) -> bool:
    """`pid` is this process or one of its parents — killing it ends the
    session (pytest, the xdist controller, the runner shell)."""
    me = os.getpid()
    seen: Set[int] = set()
    cur: Optional[int] = me
    while cur is not None and cur > 1 and cur not in seen:
        if cur == pid:
            return True
        seen.add(cur)
        cur = _ppid(cur)
    return False


def _pgid_members(pgid: int) -> List[int]:
    out: List[int] = []
    try:
        for entry in _PROC.iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            try:
                if os.getpgid(pid) == pgid:
                    out.append(pid)
            except OSError:
                continue
    except OSError:
        pass
    return out


def _refuse(kind: str, target: int, sig: int, why: str) -> None:
    msg = (
        f"{kind}({target}, {sig}) refused: {why} (test session pid {os.getpid()}). "
        "A test reached the REAL process killer — usually a monkeypatch that "
        "landed on the wrong module copy (tests/lint_sys_modules.py, "
        "test_drain_bounded.py::_patch). Unguarded, this SIGKILLs the CI runner "
        "/ your desktop session (trinity-enterprise#620)."
    )
    violations.append(msg)
    raise ForeignProcessSignal(msg)


def guarded_kill(pid: int, sig: int, /) -> None:
    if sig == 0:
        return _real_kill(pid, sig)  # liveness probe — delivers nothing
    if pid <= 0:
        return guarded_killpg(os.getpgrp() if pid == 0 else -pid, sig)
    if _is_self_or_ancestor(pid):
        _refuse("os.kill", pid, sig, "target is this process or an ancestor of it")
    if not (_PROC / str(pid)).exists():
        return _real_kill(pid, sig)  # gone — let the real ESRCH surface
    if _in_session_cgroup(pid):
        return _real_kill(pid, sig)
    _refuse("os.kill", pid, sig, "target is not in the test session's cgroup")


def guarded_killpg(pgid: int, sig: int, /) -> None:
    if sig == 0:
        return _real_killpg(pgid, sig)
    if pgid == os.getpgrp():
        _refuse("os.killpg", pgid, sig, "target is the test session's own process group")
    members = _pgid_members(pgid)
    if not members:
        return _real_killpg(pgid, sig)  # nothing there — real ESRCH surfaces
    if any(_is_self_or_ancestor(m) for m in members):
        _refuse("os.killpg", pgid, sig, "group contains this process or an ancestor")
    # Membership is decided per LIVE member. A member that exited after the
    # walk (see `_is_gone`) is neither ours nor foreign — it is not there.
    live = [m for m in members if not _is_gone(m)]
    if not live or all(_in_session_cgroup(m) for m in live):
        return _real_killpg(pgid, sig)
    _refuse("os.killpg", pgid, sig, "group contains a process outside the session cgroup")


def install() -> bool:
    """Install once; returns whether the guard is active (cgroup readable)."""
    global _installed, _session_cgroup
    if _installed:
        return True
    _session_cgroup = _cgroup_of(os.getpid())
    if _session_cgroup is None:
        return False
    os.kill = guarded_kill  # type: ignore[assignment]
    os.killpg = guarded_killpg  # type: ignore[assignment]
    _installed = True
    return True


def consume_violations() -> List[str]:
    out = list(violations)
    violations.clear()
    return out


__all__ = [
    "ForeignProcessSignal", "consume_violations", "guarded_kill",
    "guarded_killpg", "install", "violations",
]
