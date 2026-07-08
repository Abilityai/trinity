"""Allowlist resolution for the cgroup orphan sweep.

Issue #817 (follow-up): the previous cleanup passes (pgid kill,
pipe-writer sweep, env-tag sweep) each tried to identify orphans by
some artifact they inherited from claude. Production evidence
(Eugene's 2026-05-13 capture on cornelius-m) showed an orphan that
escaped all three — different pgid (setsid), no shared pipes (FDs
detached), no ``TRINITY_EXECUTION_ID`` env tag (env scrubbed).

The only inescapable boundary for a process the container is
attributing CPU to is its cgroup. The new cleanup model is therefore
a single cgroup walk: read ``/sys/fs/cgroup/cgroup.procs`` (cgroup v2
unified hierarchy), then SIGKILL every PID that is NOT on the
allowlist this module produces.

Building the allowlist correctly is the load-bearing part — kill the
wrong PID and the agent-server wedges itself. This module is the
single source of truth for "is PID N allowed to live."

The allowlist composes three sources:

1. **Hard-protected PIDs** — process attributes the platform itself
   needs regardless of any config:
     - PID 1 (the container init / ``/app/startup.sh``)
     - The sweep caller's PID and every PID in its parent chain up to
       PID 1 (covers worker threads calling on behalf of the
       agent-server master process)
     - Every PID whose comm name is ``sshd`` (operator SSH sessions
       must survive a cleanup sweep)
     - Every live ``docker exec`` session (PPID 0) + its descendants —
       operator/platform exec sessions, the same actor class as SSH
       (#1153). PID 1 (also PPID 0) is excluded as it's already covered.

2. **In-flight execution descendants** — for each active claude PID
   passed in ``extra_pids``, the full descendant tree by ppid walk.
   Covers tool subprocesses, MCP server children, hook children
   spawned during that execution.

3. **User-configured persistent patterns** — cmdline glob patterns
   from ``~/.trinity/persistent-processes.allow``. One pattern per
   line; lines starting with ``#`` are comments. Pattern matching uses
   :func:`fnmatch.fnmatchcase` against the full argv joined by spaces.
   A match protects the matched pid **and its descendant tree**
   (#1501) — a daemon's worker children are part of the daemon, and
   protecting only the bare pid made the escape hatch ineffective for
   anything that forks. Self-healing is preserved: when the daemon
   exits, its leftovers reparent to PID 1, fall out of the descendant
   walk, and are reaped on the next sweep.

   This is the escape hatch for templates that legitimately run
   long-lived daemons via ``SessionStart`` hooks (e.g. the
   ``moltbook-http-mcp`` server on cornelius-m). Default file is
   empty; templates that ship persistent daemons must declare them.

The sweep itself lives in :mod:`orphan_sweep`. This module only
computes the set of allowed PIDs.
"""
from __future__ import annotations

import fnmatch
import logging
import os
from pathlib import Path
from typing import Iterable, Optional, Set

logger = logging.getLogger(__name__)


# Default location of the cmdline-pattern allowlist file. Templates may
# ship one or operators may write one at runtime. Missing file = empty
# pattern set; readable but malformed file is logged once and treated
# as empty.
_DEFAULT_ALLOWLIST_PATH = Path("/home/developer/.trinity/persistent-processes.allow")

# Override path — for tests and operators who want to point at a
# different file. Read on every resolve() call so test-time
# monkeypatching works.
_ALLOWLIST_ENV = "TRINITY_ORPHAN_ALLOWLIST_PATH"


def _allowlist_path() -> Path:
    override = os.environ.get(_ALLOWLIST_ENV)
    if override:
        return Path(override)
    return _DEFAULT_ALLOWLIST_PATH


def load_cmdline_patterns(path: Optional[Path] = None) -> list[str]:
    """Read cmdline glob patterns from the allowlist file.

    One pattern per line. Lines that are empty or start with ``#``
    after stripping are ignored. Patterns are :func:`fnmatch`-style,
    typical use::

        # cornelius-m persistent MCP server
        *moltbook-http-mcp*
        *moltbook-mcp*

    Returns an empty list on missing file, empty file, or read error.
    Read errors are logged at WARNING; the sweep continues with no
    patterns (so only hard-protected and registered PIDs survive).
    """
    target = path if path is not None else _allowlist_path()
    try:
        text = target.read_text()
    except FileNotFoundError:
        return []
    except OSError as exc:
        logger.warning(
            "[OrphanAllowlist] Could not read %s: %s — proceeding with no patterns",
            target, exc,
        )
        return []

    patterns: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        patterns.append(stripped)
    return patterns


def _read_cmdline(pid: int) -> Optional[str]:
    """Read ``/proc/<pid>/cmdline`` as space-joined argv. Returns None
    on error. NULs become spaces; trailing whitespace stripped."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read()
    except OSError:
        return None
    if not raw:
        return None
    return raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()


def _read_ppid(pid: int) -> Optional[int]:
    """Read parent pid from ``/proc/<pid>/status``. Returns None on
    error (process gone, permission denied)."""
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("PPid:"):
                    parts = line.split(None, 1)
                    if len(parts) == 2:
                        try:
                            return int(parts[1].strip())
                        except ValueError:
                            return None
    except OSError:
        return None
    return None


def _read_comm(pid: int) -> Optional[str]:
    """Read process short name from ``/proc/<pid>/comm``."""
    try:
        with open(f"/proc/{pid}/comm") as f:
            return f.read().strip()
    except OSError:
        return None


def descendants_of(root_pid: int, *, max_iterations: int = 4096) -> Set[int]:
    """Return ``{root_pid}`` plus every PID descended from it.

    Walks ``/proc`` once to build a ppid → children map, then BFS from
    ``root_pid``. ``max_iterations`` is a safety cap against
    pathological ``/proc`` states (in practice the agent container has
    < 50 PIDs).

    Used to keep tool subprocesses and MCP-server children of an active
    claude execution on the allowlist. Note: a child reparented to PID
    1 after its parent exited will *not* appear in this set — the ppid
    edge to claude is broken. The sweep handles that via the in-flight
    execution PID list at the call site rather than only ppid walks.
    """
    try:
        entries = os.listdir("/proc")
    except OSError:
        return {root_pid}

    children: dict[int, list[int]] = {}
    for name in entries:
        if not name.isdigit():
            continue
        pid = int(name)
        ppid = _read_ppid(pid)
        if ppid is None:
            continue
        children.setdefault(ppid, []).append(pid)

    result: Set[int] = {root_pid}
    queue = [root_pid]
    iterations = 0
    while queue and iterations < max_iterations:
        iterations += 1
        cur = queue.pop()
        for child in children.get(cur, ()):
            if child in result:
                continue
            result.add(child)
            queue.append(child)
    return result


def _hard_protected_pids(sweep_pid: int) -> Set[int]:
    """PIDs that must never be killed regardless of allowlist config.

    - PID 1 (container init)
    - sweep_pid (caller of the sweep)
    - sweep_pid's parent chain up to PID 1 — covers the case where the
      sweep runs from a worker thread of agent-server; we want
      agent-server itself protected too.
    """
    protected: Set[int] = {1, sweep_pid}
    cur = sweep_pid
    for _ in range(32):  # depth cap; typical container depth is 1–3
        ppid = _read_ppid(cur)
        if ppid is None or ppid <= 0 or ppid in protected:
            break
        protected.add(ppid)
        cur = ppid
    return protected


def _ssh_session_pids() -> Set[int]:
    """Return PIDs of every sshd-related process in /proc.

    sshd's listener forks per-connection children that themselves fork
    shells. Operators using ``trinity ssh`` to debug an agent expect
    their session to survive a cleanup sweep. Matching by comm name
    ``sshd`` is coarse but adequate — no legitimate Trinity workload
    runs a process literally named ``sshd``."""
    result: Set[int] = set()
    try:
        entries = os.listdir("/proc")
    except OSError:
        return result
    for name in entries:
        if not name.isdigit():
            continue
        pid = int(name)
        if _read_comm(pid) == "sshd":
            result.add(pid)
    return result


def _docker_exec_session_pids() -> Set[int]:
    """Return PIDs of live ``docker exec`` sessions plus their descendants (#1153).

    Inside the container's PID namespace, a ``docker exec`` entry process
    has **PPID 0** — its real parent (the containerd exec shim) lives
    outside the namespace, so the kernel reports the parent as 0. Genuinely
    leaked orphans, by contrast, are reparented to **PID 1**, never to PID
    0. PPID 0 is therefore an unspoofable signal that the process is an
    operator/platform exec session — the same class of actor that
    :func:`_ssh_session_pids` already protects (an operator debugging or
    maintaining the agent). It also covers the platform's own exec paths
    (pre-check hooks, git recovery, session-JSONL reaping, the agent
    terminal) that run via ``docker exec`` / ``execute_command_in_container``.

    PID 1 also has PPID 0 but is already hard-protected; it is excluded
    here so this does NOT degrade into "protect every child of PID 1"
    (which would shield real orphans reparented to 1 — the thing the sweep
    exists to kill).

    Each exec entry's descendant tree is included so the operator's actual
    command and its children survive. When the session exits, any leftover
    children reparent to PID 1 (PPID 1, not 0) and fall out of this set —
    so they are reaped on the next sweep, preserving the #817 guarantee.
    """
    result: Set[int] = set()
    try:
        entries = os.listdir("/proc")
    except OSError:
        return result
    for name in entries:
        if not name.isdigit():
            continue
        pid = int(name)
        if pid == 1:
            continue  # container init also has PPID 0; already hard-protected
        if _read_ppid(pid) == 0:
            result.update(descendants_of(pid))
    return result


# Cmdline patterns that are ALWAYS protected, regardless of user
# allowlist config. These are processes the base image starts that
# the container's continued operation depends on. They live as direct
# children of PID 1 (``/app/startup.sh``) and are not subject to user
# control.
#
# Why they need their own list instead of relying on the hard-protect
# ppid walk:
#   - The hard-protect walk goes from sweep_pid (agent-server) UP to
#     PID 1, not DOWN to PID 1's other children.
#   - "Protect every child of PID 1" would also blanket-protect
#     orphans that get reparented to PID 1 when their parent exits —
#     defeating the whole point of the sweep.
#   - So we protect specific cmdline patterns instead. Narrow enough
#     to not shield real orphans, broad enough to cover the
#     base-image essentials.
_PLATFORM_ESSENTIAL_PATTERNS = (
    "tail -f /dev/null",                # startup.sh keep-alive (PID 1 exit gate)
    "sudo */sbin/sshd*",                # sudo wrapper around sshd -D
    "sudo -E /usr/bin/python3 /opt/trinity/hooks/*",  # guardrail config writer
)


def _platform_essential_pids() -> Set[int]:
    """Return PIDs whose cmdline matches a base-image essential pattern.

    Same /proc walk as :func:`resolve_allowlist`'s pattern matching,
    but using the hard-coded :data:`_PLATFORM_ESSENTIAL_PATTERNS`
    rather than the user's allowlist file. Always runs.
    """
    result: Set[int] = set()
    try:
        entries = os.listdir("/proc")
    except OSError:
        return result
    for name in entries:
        if not name.isdigit():
            continue
        pid = int(name)
        cmdline = _read_cmdline(pid)
        if cmdline is None:
            continue
        for pat in _PLATFORM_ESSENTIAL_PATTERNS:
            if fnmatch.fnmatchcase(cmdline, pat):
                result.add(pid)
                break
    return result


def resolve_allowlist(
    sweep_pid: int,
    *,
    extra_pids: Iterable[int] = (),
    cmdline_patterns: Optional[list[str]] = None,
) -> Set[int]:
    """Compute the full set of PIDs the sweep must NOT kill.

    Args:
        sweep_pid: pid of the process running the sweep — used to
            protect that process and its parent chain.
        extra_pids: explicit PIDs to keep alive, plus everything they
            spawned. Typical caller passes every active claude PID
            and pgid from :class:`ProcessRegistry`. Descendants are
            resolved by ppid walk.
        cmdline_patterns: optional precomputed patterns. ``None`` =
            load from the file at ``_allowlist_path()``.

    Returns the set of allowed PID ints. The sweep then iterates
    ``/sys/fs/cgroup/cgroup.procs`` and kills every PID *not* in this
    set.
    """
    allowlist: Set[int] = set()
    allowlist.update(_hard_protected_pids(sweep_pid))
    allowlist.update(_ssh_session_pids())
    allowlist.update(_docker_exec_session_pids())  # #1153
    allowlist.update(_platform_essential_pids())

    for pid in extra_pids:
        if pid <= 0:
            continue
        allowlist.update(descendants_of(pid))

    patterns = (
        cmdline_patterns
        if cmdline_patterns is not None
        else load_cmdline_patterns()
    )
    if patterns:
        try:
            entries = os.listdir("/proc")
        except OSError:
            entries = []
        for name in entries:
            if not name.isdigit():
                continue
            pid = int(name)
            if pid in allowlist:
                continue
            cmdline = _read_cmdline(pid)
            if cmdline is None:
                continue
            for pattern in patterns:
                if fnmatch.fnmatchcase(cmdline, pattern):
                    # #1501: protect the matched daemon's descendant tree,
                    # not just the bare pid — its worker children (which may
                    # carry a different cmdline) are part of the daemon.
                    # NOTE: applies to USER patterns only;
                    # _platform_essential_pids stays bare-pid (known leaf
                    # processes; widening it would shield orphans).
                    allowlist.update(descendants_of(pid))
                    break

    return allowlist
