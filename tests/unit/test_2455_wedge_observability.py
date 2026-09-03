"""#2455 follow-up — what the first field capture (2026-09-02) demanded.

The v1 dump fired on a real wedge and came back nearly useless in four ways,
each recorded in the issue comment:

  * MainThread — the wedged thread — dumped as TWO mutually-inconsistent
    frames: ``sys._current_frames`` hands back live frames, and CPython 3.11+
    detaches ``f_back`` when a racing frame exits, silently truncating chains.
  * The async CALLER (which task owned the frame stuck in ``aclose``) is
    structurally absent from every thread stack — suspended awaiters are on no
    thread's C stack.
  * One sample cannot tell a spin from a block.
  * The dumps PRINTED hours after their call sites ran, so the sample instant
    was unknowable.

These tests pin the fixes: faulthandler-based full-chain stacks, an async-task
await-chain section, dual samples, an embedded capture timestamp, a raw fd-2
emission path — and the loop watchdog that notices the wedge in ~a minute
instead of at the executor's outer timeout hours later.
"""
from __future__ import annotations

import asyncio
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
# The dump answers the questions the 09-02 capture could not
# --------------------------------------------------------------------------- #

def test_the_dump_embeds_its_capture_instant():
    """On 09-02 both dumps printed the second the loop unwedged — hours after
    the branches that called them — so nobody could say WHEN the frames were
    sampled. The timestamp must live in the dump text itself."""
    text = td.dump_all_threads("capture-instant probe")
    assert "captured_at=" in text


def test_the_dump_takes_two_samples_so_spin_and_block_are_distinguishable():
    text = td.dump_all_threads("dual-sample probe")
    assert "python sample 1" in text
    assert "python sample 2" in text


def test_the_authoritative_stacks_come_from_faulthandler_with_full_chains():
    """The two-frame MainThread truncation is a property of formatting live
    frame objects. faulthandler walks the interpreter stack at C level; the
    proof of a full chain is that the dump shows THIS test function — a caller
    several frames above dump_all_threads — in the dumping thread's stack."""
    text = td.dump_all_threads("faulthandler probe")
    assert "faulthandler stacks" in text
    assert "most recent call first" in text, "faulthandler output missing"
    fh_section = text.split("== async tasks")[0]
    assert "test_the_authoritative_stacks_come_from_faulthandler" in fh_section, (
        "faulthandler section must carry the full call chain, not just the "
        "innermost frames"
    )


def test_the_faulthandler_section_maps_idents_to_thread_names():
    """faulthandler names threads by hex ident only; the map is what lets an
    operator match a stack to `top -bH` output and to the Python samples."""
    text = td.dump_all_threads("ident-map probe")
    assert "thread idents:" in text
    assert "MainThread" in text


def test_the_dump_walks_the_main_loops_task_await_chains():
    """The wedged frame's async CALLER — /api/task response vs result callback
    vs something else — is on no thread's stack. Only the task await-chain can
    name it; the 09-02 comment asks for exactly this."""
    parked = threading.Event()      # signals the chain reached its await
    release_holder: list = []
    loop_holder: list = []

    async def wedge_probe_inner(evt):
        parked.set()
        await evt.wait()

    async def wedge_probe_outer(evt):
        await wedge_probe_inner(evt)

    def run_loop():
        loop = asyncio.new_event_loop()
        loop_holder.append(loop)
        evt = asyncio.Event()
        release_holder.append((loop, evt))
        loop.create_task(wedge_probe_outer(evt), name="wedge-probe-task")
        loop.run_forever()

    t = threading.Thread(target=run_loop, daemon=True)
    t.start()
    assert parked.wait(5), "probe task never reached its await"
    prev = td._main_loop
    try:
        td.register_main_loop(loop_holder[0])
        text = td.dump_all_threads("async-chain probe")
    finally:
        td._main_loop = prev
        loop, evt = release_holder[0]
        loop.call_soon_threadsafe(evt.set)
        loop.call_soon_threadsafe(loop.stop)
        t.join(5)

    assert "wedge-probe-task" in text, "the task must be named"
    assert "in wedge_probe_outer" in text, "the OUTER caller is the answer"
    assert "in wedge_probe_inner" in text, "the chain must be walked, not one frame"


def test_the_task_section_degrades_honestly_without_a_registered_loop():
    prev = td._main_loop
    try:
        td._main_loop = None
        text = td.dump_all_threads("no-loop probe")
    finally:
        td._main_loop = prev
    assert "<no main loop registered>" in text


def test_the_await_chain_names_types_never_values():
    """The chain may end on a Future/Event; printing its repr could echo a
    result VALUE (canary G-04 class). Type name only."""
    async def tail():
        pass
    coro = tail()
    try:
        hops = td._awaitable_chain(coro)
    finally:
        coro.close()
    assert hops and "in tail" in hops[0]
    src = (Path(td.__file__)).read_text()
    assert "type(obj).__name__" in src, "chain must print awaitable TYPE, not repr"


# --------------------------------------------------------------------------- #
# The loop watchdog — noticing the wedge while it is happening
# --------------------------------------------------------------------------- #

def test_stall_decision_dumps_on_crossing_then_rate_limits_then_recovers():
    th, rd = 60.0, 300.0
    # healthy
    action, since, last = td._stall_decision(3.0, 1000.0, None, None, threshold=th, redump=rd)
    assert action == "none" and since is None
    # crossing: dump, onset back-dated to the last beat
    action, since, last = td._stall_decision(61.0, 1000.0, None, None, threshold=th, redump=rd)
    assert action == "dump" and since == pytest.approx(939.0) and last == 1000.0
    # still stalled, inside the redump window: quiet
    action, since2, last2 = td._stall_decision(120.0, 1059.0, since, last, threshold=th, redump=rd)
    assert action == "none" and since2 == since and last2 == last
    # past the redump window: dump again
    action, _, last3 = td._stall_decision(400.0, 1301.0, since, last, threshold=th, redump=rd)
    assert action == "dump" and last3 == 1301.0
    # beat resumes: recovery, state cleared
    action, since4, last4 = td._stall_decision(2.0, 1400.0, since, last3, threshold=th, redump=rd)
    assert action == "recovered" and since4 is None and last4 is None


def test_the_watchdog_thread_fires_the_dump_on_a_stale_beat():
    """End to end through the real thread body with injected cadence: a beat
    that stops advancing must produce dumps — more than one, so a multi-hour
    wedge yields a series (spin vs block over time), not a single sample."""
    calls: list = []
    stop = threading.Event()
    prev_beat = td._beat_at
    td._beat_at = time.monotonic() - 100.0     # loop "stalled" 100s ago
    t = threading.Thread(
        target=td._run_watchdog,
        kwargs=dict(stop=stop, check=0.02, threshold=1.0, redump=0.05,
                    dump_fn=lambda reason, context="": calls.append((reason, context))),
        daemon=True,
    )
    try:
        t.start()
        deadline = time.monotonic() + 3.0
        while len(calls) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        stop.set()
        t.join(3)
        td._beat_at = prev_beat
    assert len(calls) >= 2, "a persisting stall must re-dump, not fire once"
    reason, context = calls[0]
    assert "event loop stalled" in reason
    assert "lag=" in context


def test_the_watchdog_stays_quiet_while_the_beat_is_fresh():
    calls: list = []
    stop = threading.Event()
    prev_beat = td._beat_at
    td._beat_at = time.monotonic()
    t = threading.Thread(
        target=td._run_watchdog,
        kwargs=dict(stop=stop, check=0.02, threshold=5.0, redump=0.05,
                    dump_fn=lambda *a, **k: calls.append(a)),
        daemon=True,
    )
    try:
        t.start()
        time.sleep(0.2)
    finally:
        stop.set()
        t.join(3)
        td._beat_at = prev_beat
    assert calls == [], "a healthy loop must cost zero dumps"


# --------------------------------------------------------------------------- #
# Wiring + the emission path that cannot be delayed by the wedge
# --------------------------------------------------------------------------- #

def _src(rel: str) -> str:
    return (_BASE / "agent_server" / rel).read_text()


def test_the_agent_server_arms_the_loop_watchdog():
    src = _src("main.py")
    assert "_schedule_loop_watchdog(app)" in src


def test_the_dump_emits_raw_to_fd2_before_the_structured_logger():
    """On 09-02 the structured records surfaced ~2h late. os.write(2, …) takes
    no Python-level lock; it must run FIRST so the evidence lands even when
    logging is stuck behind the wedge being reported."""
    src = _src("utils/thread_diagnostics.py")
    body = src[src.index("def dump_all_threads"):]
    assert body.index("_raw_emit(") < body.index("logger.error("), (
        "raw fd-2 emission must precede the logger call"
    )
    assert "os.write(2" in src


def test_section_order_puts_the_authoritative_stacks_first():
    """Truncation cuts the tail; the faulthandler stacks and the task chains
    are the parts three prior fixes were missing, so they must survive it."""
    text = td.dump_all_threads("order probe")
    fh = text.index("== faulthandler stacks")
    tasks = text.index("== async tasks")
    s1 = text.index("== python sample 1")
    assert fh < tasks < s1
