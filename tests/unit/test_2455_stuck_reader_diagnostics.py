"""#2455 — when a reader thread wedges, record WHERE it is.

The #728 / #1502 / #1661 family has produced three fixes, each built on an
assumption about the stuck thread's location, each followed by a recurrence in
a new shape. The #2455 occurrence fits none of them: the reader was still stuck
**603.7s after the process group was SIGKILLed**, in state `R` at 93.8% CPU —
and a thread blocked on a pipe consumes no CPU.

So this ships the measurement rather than a fourth guess. These tests pin the
three properties that make it trustworthy on a production box:

  1. it fires from the branches that CONCLUDE a thread is stuck, automatically;
  2. it names the stuck thread and shows the frame it is parked in;
  3. it cannot make the incident worse — never raises, bounded output.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_BASE = Path(__file__).resolve().parents[2] / "docker" / "base-image"
if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))

from agent_server.utils import thread_diagnostics as td  # noqa: E402


# --------------------------------------------------------------------------- #
# It answers the question the issue asks
# --------------------------------------------------------------------------- #

def test_the_dump_names_the_stuck_thread_and_shows_where_it_is():
    """The whole deliverable. `stuck_count=1` told us a thread was stuck and
    nothing else; three fixes were then written against guesses about which
    line it sat on."""
    release = threading.Event()

    def parked():
        release.wait(30)

    t = threading.Thread(target=parked, name="reader-stdout-probe", daemon=True)
    t.start()
    try:
        time.sleep(0.15)
        text = td.dump_all_threads("probe", context="pid=4392 stuck_count=1")
    finally:
        release.set()
        t.join(5)

    assert "reader-stdout-probe" in text, "the dump must name the stuck thread"
    assert "in parked" in text, "the dump must show the frame it is parked in"
    assert "daemon=True" in text, "leaked readers are daemon threads — say so"


def test_the_dump_is_bounded():
    """A wedged process can hold dozens of threads and this log is shipped to
    Vector. Diagnostic, not archive."""
    assert td.MAX_DUMP_CHARS <= 100_000
    text = td.dump_all_threads("bound check")
    assert len(text) <= td.MAX_DUMP_CHARS + 64


def test_the_dump_never_raises_into_the_teardown_path():
    """It runs on the teardown of an execution that has ALREADY gone wrong. A
    diagnostic that can break that path is worse than no diagnostic."""
    broken = lambda: (_ for _ in ()).throw(RuntimeError("frames unavailable"))
    original = sys._current_frames
    sys._current_frames = broken            # type: ignore[assignment]
    try:
        assert td.dump_all_threads("forced failure") == ""
    finally:
        sys._current_frames = original      # type: ignore[assignment]


def test_enable_is_idempotent_and_never_raises():
    td.enable()
    td.enable()          # second call must be a no-op, not a double handler
    assert td._enabled is True


# --------------------------------------------------------------------------- #
# It is wired to the moments that matter — asserted against real source
# --------------------------------------------------------------------------- #

def _src(rel: str) -> str:
    return (_BASE / "agent_server" / rel).read_text()


def test_it_fires_from_the_post_kill_stuck_branch():
    """The branch that force-closes pipes and accepts data loss. Dumping BEFORE
    that call is the point — after it, the stack is no longer the wedged one."""
    src = _src("utils/subprocess_pgroup.py")
    stuck_log = src.index("still stuck after %.1fs post-kill")
    dump = src.index("_dump_all_threads(", stuck_log)
    close = src.index("safe_close_pipes(process)", stuck_log)
    assert dump < close, "dump must run before the pipes are force-closed"


def test_it_fires_from_the_budget_exceeded_branch_too():
    """The EARLIEST moment we know a reader is wedged. The two branches are
    minutes apart in the #2455 timeline (90s vs 603.7s), so one dump cannot
    tell you whether the thread moved."""
    src = _src("services/subprocess_lifecycle.py")
    budget_log = src.index("Drain budget (%ds) exceeded")
    assert "_dump_all_threads(" in src[budget_log:budget_log + 1500]


def test_the_import_is_module_scope_not_inside_the_wedged_branch():
    """A lazy import would run while the process is already wedged — the worst
    moment to take the import lock."""
    for rel in ("utils/subprocess_pgroup.py", "services/subprocess_lifecycle.py"):
        src = _src(rel)
        imp = src.index("import dump_all_threads as _dump_all_threads")
        first_use = src.index("_dump_all_threads(")
        assert imp < first_use, f"{rel}: import must precede the call site"


def test_the_agent_server_arms_it_at_startup():
    """`kill -USR1 <pid>` has to work on a container that is ALREADY wedged, so
    the handler must be installed before anything can wedge — not lazily."""
    src = _src("main.py")
    assert "_enable_thread_diagnostics()" in src
    assert src.index("_enable_thread_diagnostics()") < src.index("app = FastAPI(")


def test_sigusr1_is_registered_without_chaining():
    """Chaining to SIGUSR1's default disposition TERMINATES the process — that
    would turn an operator's diagnostic into an outage on a live agent."""
    src = _src("utils/thread_diagnostics.py")
    assert "signal.SIGUSR1" in src
    assert "chain=False" in src


def test_the_dump_reports_module_and_line_never_values():
    """Tracebacks carry frames, not locals. This runs on agent output that has
    held credentials before (canary G-04), so the distinction is load-bearing —
    an argument dump here would be a new leak surface."""
    src = _src("utils/thread_diagnostics.py")
    assert "format_stack" in src
    for leaky in ("f_locals", "format_exception", "repr(frame"):
        assert leaky not in src, f"{leaky} would put VALUES in the log"


# --------------------------------------------------------------------------- #
# End to end through the REAL drain, with a genuinely stuck reader
# --------------------------------------------------------------------------- #

def test_the_real_drain_dumps_a_genuinely_stuck_reader(caplog, monkeypatch):
    """Drives `drain_reader_threads` itself rather than asserting on its source.

    A source scan proves the call is written; only this proves it FIRES, that
    the dump reaches the log, and that it names the thread an operator would
    otherwise only know as `stuck_count=1`.
    """
    import asyncio
    import logging
    from agent_server.utils import subprocess_pgroup as sp

    release = threading.Event()

    def wedged():
        release.wait(30)

    stuck = threading.Thread(target=wedged, name="reader-wedged-probe", daemon=True)
    stuck.start()

    class _FakeProc:
        pid = 4392
        stdout = None
        stderr = None
        def poll(self): return 0

    # Neither the kill nor the cgroup sweep is under test here, and both would
    # reach for real processes.
    monkeypatch.setattr(sp, "terminate_process_group", lambda *a, **k: None)
    monkeypatch.setattr(sp, "safe_close_pipes", lambda *a, **k: None)
    monkeypatch.setattr(sp, "kill_cgroup_orphans", lambda *a, **k: 0, raising=False)

    try:
        with caplog.at_level(logging.ERROR):
            asyncio.run(sp.drain_reader_threads(
                _FakeProc(), stuck, grace=1, post_kill_grace=1, pgid=4392,
            ))
    finally:
        release.set()
        stuck.join(5)

    dumps = [r.getMessage() for r in caplog.records if "THREAD DUMP" in r.getMessage()]
    assert dumps, "the stuck branch ran but no thread dump reached the log"
    body = "\n".join(dumps)
    assert "reader-wedged-probe" in body, "the dump must name the wedged reader"
    assert "in wedged" in body, "the dump must show the frame it is parked in"
    assert "stuck_count=1" in body, "carry the same identifier the old log gave"


def test_subprocess_pgroup_stays_flat_importable():
    """Regression on my own change (#2455).

    `subprocess_pgroup.py` is deliberately importable FLAT — its own test adds
    `agent_server/utils` to `sys.path` and imports it by bare name. A plain
    relative import for the diagnostics helper broke that whole module with
    "attempted relative import with no known parent package", taking 14 tests
    with it. The dual-path import keeps both callers working; this pins it so
    the fallback is not "tidied" away.
    """
    import importlib.util

    utils_dir = _BASE / "agent_server" / "utils"
    if str(utils_dir) not in sys.path:
        sys.path.insert(0, str(utils_dir))
    spec = importlib.util.spec_from_file_location(
        "subprocess_pgroup_flat", utils_dir / "subprocess_pgroup.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)          # must not raise
    assert hasattr(mod, "_dump_all_threads")
