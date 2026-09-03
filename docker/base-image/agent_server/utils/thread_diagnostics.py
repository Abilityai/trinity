"""Capture WHERE a stuck thread actually is, at the moment we notice (#2455).

WHY THIS EXISTS. The #728 / #1502 / #1661 family all reason about a reader
thread that will not return, and every mitigation so far has been built on an
*assumption* about where it is stuck — blocked on a pipe (#728), holding a
grandchild's write end open (#1502), inside a catastrophic sanitizer regex
(#1661). Each assumption produced a real fix, and the failure kept coming back
in a new shape, because nothing in the fleet ever recorded the thread's actual
stack.

The #2455 occurrence is the case that breaks the guessing: the reader was still
stuck **603.7 s after the process group was SIGKILLed**, and `top -bH` showed it
in state **R at 93.8% CPU** — a thread blocked on a pipe consumes no CPU, so
whatever it was doing, it was not the thing three previous fixes assumed. The
issue asks for exactly this: *"worth capturing where the thread actually is
(py-spy / faulthandler on a live occurrence) rather than assuming
blocked-on-pipe."*

WHAT THE FIRST FIELD CAPTURE TAUGHT (2026-09-02, issue comment). The v1 dump
fired on a real wedge and came back with exactly two frames for MainThread —
the wedged thread — and no async caller. Four defects, each fixed here:

  1. **Frame-object racing truncates chains.** ``sys._current_frames()`` hands
     back live frame objects; formatting them races the running thread, and on
     CPython 3.11+ a frame that exits mid-format DETACHES its ``f_back`` — the
     chain silently ends there. That is how a MainThread running a full event
     loop dumped as two mutually-inconsistent frames. The authoritative stacks
     now come from ``faulthandler.dump_traceback`` into a temp file — C-level,
     per-thread-atomic, immune to both races — with the pretty Python sample
     kept as the human-readable, thread-NAMED view.
  2. **The async caller is structurally absent from every thread stack.** A
     suspended awaiting coroutine is not on any thread's C stack, so even a
     perfect thread dump cannot say WHICH task (the /api/task response? the
     result callback?) owns the frame that is stuck in ``aclose``. A new
     section walks the registered main loop's tasks and each task's
     ``cr_await`` chain — names, files, lines; never values.
  3. **One sample cannot tell a spin from a block.** Two samples, ~250 ms
     apart, in one dump. Identical line = parked; moving line = looping.
  4. **Emission time lied about capture time.** On 09-02 the dump *printed*
     hours after the branch that called it (both appeared the second the loop
     unwedged), so the sample instant was unknowable. Every dump now embeds
     ``captured_at`` (and per-sample offsets), and the raw text is emitted via
     ``os.write(2, …)`` FIRST — bypassing the logging module's handler locks
     and any wedged Python-level machinery — before the structured logger copy
     that Vector correlates.

WHY NOT py-spy. It needs installing into a running production container, root
or `SYS_PTRACE` (agents run non-root with `CAP_DROP: ALL`, Invariant #17), and a
human present *while the wedge is live*. The wedge is rare, is noticed hours
later, and self-clears on the container restart that ops reach for. `faulthandler`
is in the standard library, needs no privileges, and can fire the instant the
code itself concludes a thread is stuck — which is the only moment guaranteed to
be during the event.

THE LOOP WATCHDOG (this file's second job, same issue). The 09-02 capture also
proved the wedge is not a teardown artifact: the event loop stopped completing
HTTP requests ~10 minutes BEFORE claude finished and ~3h before any drain
branch could notice, while heartbeats kept the agent looking healthy. The only
component positioned to notice a wedged loop is a plain thread watching a beat
the loop must keep. ``schedule_loop_watchdog`` runs one: an async task stamps a
monotonic beat every few seconds; a daemon thread checks it, and past the stall
threshold it dumps (rate-limited while the stall persists — the periodic dumps
answer spin-vs-block over hours, and the FIRST one catches the onset stack, not
the teardown) and logs the stall duration on recovery. Costs one 5s timer when
healthy.

WHAT IT COSTS WHEN NOTHING IS WRONG. A beat task waking every few seconds and
a watchdog thread waking every few seconds to compare two floats. Dumps happen
only from already-exceptional branches or a stalled loop.

SAFETY. Output goes to the agent log via `logger`, never to a file the agent
could read back and echo. Tracebacks name modules, functions and line numbers
— never values — so this cannot surface a credential the way an argument dump
would. The async-task section prints code names and awaitable TYPE names only.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import faulthandler
import logging
import os
import signal
import sys
import tempfile
import threading
import time
import traceback

logger = logging.getLogger(__name__)

# Bound the emitted text. A wedged process can hold dozens of threads and the
# agent log is shipped to Vector; a dump is diagnostic, not an archive.
# Section order is by diagnostic value (faulthandler stacks, then async tasks,
# then the two Python samples) so truncation eats the most redundant part.
MAX_DUMP_CHARS = 60_000

# Gap between the two Python-level samples inside one dump. Long enough that a
# looping thread shows a different line, short enough to cost nothing on a
# teardown path already minutes deep.
SECOND_SAMPLE_DELAY_SECONDS = 0.25

# Loop-watchdog cadence. The beat task stamps every BEAT; the watchdog thread
# checks every CHECK; a lag past STALL_THRESHOLD is a stalled loop (60s is far
# beyond any legitimate sync work the agent-server does on the loop); while the
# stall persists it re-dumps every STALL_REDUMP so a multi-hour wedge yields a
# bounded series of samples instead of one.
BEAT_INTERVAL_SECONDS = 5.0
WATCHDOG_CHECK_SECONDS = 5.0
STALL_THRESHOLD_SECONDS = 60.0
STALL_REDUMP_SECONDS = 300.0

_enabled = False

# The running event loop, registered at startup so dump_all_threads can walk
# its tasks from ANY thread (the drain branches and the watchdog are threads).
_main_loop: asyncio.AbstractEventLoop | None = None

# Watchdog state. _beat_at is written by the beat task (loop thread) and read
# by the watchdog thread — a float store is atomic under the GIL.
_beat_at: float | None = None
_watchdog_stop = threading.Event()
_watchdog_thread: threading.Thread | None = None
_beat_task_ref: list = []  # strong ref — the asyncio GC footgun


def enable() -> None:
    """Arm faulthandler for this process. Idempotent, never raises.

    Two capabilities, both zero-cost until used:

    * a fatal-signal handler (SIGSEGV/SIGABRT/…) so a hard crash leaves a stack
      rather than a bare exit code;
    * ``SIGUSR1`` → dump every thread's stack to stderr, so an operator holding
      a wedged container can get the answer with ``kill -USR1 1`` and no extra
      tooling, no privileges and no restart.

    SIGUSR1 is chosen because nothing in the agent image uses it (SIGTERM/SIGINT
    are the shutdown path, SIGUSR2 is left free) and because it is deliverable
    from `docker kill --signal`.
    """
    global _enabled
    if _enabled:
        return
    try:
        faulthandler.enable(file=sys.stderr, all_threads=True)
        if hasattr(faulthandler, "register") and hasattr(signal, "SIGUSR1"):
            # chain=False: we are the only SIGUSR1 handler, and chaining to a
            # default that terminates the process would turn a diagnostic into
            # an outage.
            faulthandler.register(
                signal.SIGUSR1, file=sys.stderr, all_threads=True, chain=False,
            )
        _enabled = True
        logger.info(
            "[Diagnostics] faulthandler armed — `kill -USR1 <pid>` dumps every "
            "thread's stack to the container log (#2455)"
        )
    except Exception as e:  # noqa: BLE001 — diagnostics must never break boot
        logger.warning("[Diagnostics] could not arm faulthandler: %s", e)


def register_main_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Record the serving event loop so dumps can walk its tasks (#2455)."""
    global _main_loop
    _main_loop = loop


# --------------------------------------------------------------------------- #
# Dump sections
# --------------------------------------------------------------------------- #

def _faulthandler_section() -> str:
    """Authoritative per-thread stacks, captured at C level.

    ``faulthandler.dump_traceback`` walks each thread's interpreter frame stack
    without executing Python between capture and format, so it is immune to the
    f_back-detach race that truncated the 09-02 MainThread dump to two frames.
    It only takes a real file DESCRIPTOR, so it goes through a temp file that
    is read straight back. Its output names threads by hex ident only, so an
    ident→name map is prepended.
    """
    try:
        names = ", ".join(
            f"{t.ident:#x}={t.name}" for t in threading.enumerate() if t.ident
        )
        with tempfile.TemporaryFile() as f:
            faulthandler.dump_traceback(file=f, all_threads=True)
            f.seek(0)
            raw = f.read().decode("utf-8", errors="replace")
        return f"thread idents: {names}\n{raw}"
    except Exception as e:  # noqa: BLE001 — a section may fail, the dump must not
        return f"<faulthandler capture failed: {type(e).__name__}>"


def _awaitable_chain(obj, limit: int = 32) -> list[str]:
    """Follow a task's coroutine down its await chain — names/lines only.

    A suspended awaiter is on NO thread's stack, so this is the only way to
    answer "which async caller owns the stuck frame". Frames are read
    best-effort off a possibly-running coroutine; lines may lag by a tick.
    """
    hops: list[str] = []
    seen: set[int] = set()
    while obj is not None and len(hops) < limit and id(obj) not in seen:
        seen.add(id(obj))
        frame = (
            getattr(obj, "cr_frame", None)
            or getattr(obj, "gi_frame", None)
            or getattr(obj, "ag_frame", None)
        )
        if frame is not None:
            code = frame.f_code
            hops.append(f"{code.co_filename}:{frame.f_lineno} in {code.co_name}")
        else:
            # A Future/Event/etc. at the end of the chain: its TYPE says what
            # is being awaited on without echoing any value it carries.
            hops.append(f"<awaiting {type(obj).__name__}>")
        obj = (
            getattr(obj, "cr_await", None)
            or getattr(obj, "gi_yieldfrom", None)
            or getattr(obj, "ag_await", None)
        )
    return hops


def _async_tasks_section() -> str:
    """Every task on the registered main loop, with its await chain."""
    loop = _main_loop
    if loop is None:
        return "<no main loop registered>"
    try:
        tasks = asyncio.all_tasks(loop)
    except Exception as e:  # noqa: BLE001 — WeakSet iteration can race
        return f"<task enumeration failed: {type(e).__name__}>"
    if not tasks:
        return "<no pending tasks>"
    chunks = []
    for task in tasks:
        try:
            chain = _awaitable_chain(task.get_coro())
            body = "\n".join(f"    {hop}" for hop in chain) or "    <no frame>"
            chunks.append(f"  task {task.get_name()}:\n{body}")
        except Exception as e:  # noqa: BLE001
            chunks.append(f"  task <unreadable: {type(e).__name__}>")
    return "\n".join(chunks)


def _python_sample() -> str:
    """One human-readable pass over ``sys._current_frames`` — thread names
    included, but racy against running threads (see module docstring). The
    faulthandler section is the authoritative copy."""
    frames = sys._current_frames()          # CPython; the agent runs CPython
    by_ident = {t.ident: t for t in threading.enumerate()}
    chunks = []
    for ident, frame in frames.items():
        t = by_ident.get(ident)
        name = t.name if t is not None else "<unknown>"
        daemon = t.daemon if t is not None else "?"
        stack = "".join(traceback.format_stack(frame))
        chunks.append(f"--- thread {name} (ident={ident}, daemon={daemon})\n{stack}")
    return "\n".join(chunks)


def _raw_emit(text: str) -> None:
    """Write straight to fd 2, bypassing the logging module entirely.

    On 09-02 both dumps reached the log HOURS after their call sites ran —
    something between the call and the record being written was wedged. An
    ``os.write`` to stderr takes no Python-level lock and cannot be delayed by
    a stuck handler; Vector ships stderr, so the evidence lands even when the
    structured copy below is stuck behind the very wedge being reported.
    """
    try:
        os.write(2, text.encode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001 — best-effort by definition
        pass


def dump_all_threads(reason: str, *, context: str = "") -> str:
    """Log every thread's stack plus the main loop's task chains; return it.

    Layout, ordered by diagnostic value so the size bound truncates the most
    redundant part first:

      1. faulthandler stacks (C-atomic, full chains, ident-keyed)
      2. async task await-chains (the caller no thread stack can show)
      3. two Python samples ~250 ms apart (named threads; spin vs block)

    ``captured_at`` is embedded because emission time has been observed to lag
    capture by hours on a wedged process. Never raises: this runs on the
    teardown path of an execution that has already gone wrong, and a diagnostic
    that can break that path is worse than no diagnostic.
    """
    try:
        captured_at = _dt.datetime.now(_dt.timezone.utc).isoformat()
        t0 = time.monotonic()
        fh = _faulthandler_section()
        tasks = _async_tasks_section()
        sample1 = _python_sample()
        time.sleep(SECOND_SAMPLE_DELAY_SECONDS)
        sample2 = _python_sample()
        n_threads = threading.active_count()
        text = (
            f"captured_at={captured_at}\n"
            f"== faulthandler stacks (authoritative) ==\n{fh}\n"
            f"== async tasks on main loop ==\n{tasks}\n"
            f"== python sample 1 (+0.00s) ==\n{sample1}\n"
            f"== python sample 2 (+{time.monotonic() - t0:.2f}s) ==\n{sample2}"
        )
        if len(text) > MAX_DUMP_CHARS:
            text = text[:MAX_DUMP_CHARS] + f"\n… truncated at {MAX_DUMP_CHARS} chars"
        header = (
            f"[Diagnostics] THREAD DUMP — {reason}"
            f"{' | ' + context if context else ''} | {n_threads} thread(s)\n"
        )
        _raw_emit(header + text + "\n")
        logger.error("%s%s", header, text)
        return text
    except Exception as e:  # noqa: BLE001
        logger.warning("[Diagnostics] thread dump failed (%s): %s", reason, e)
        return ""


# --------------------------------------------------------------------------- #
# Event-loop stall watchdog (#2455, 09-02 occurrence)
# --------------------------------------------------------------------------- #

def _stall_decision(
    lag: float,
    now: float,
    stalled_since: float | None,
    last_dump_at: float | None,
    *,
    threshold: float = STALL_THRESHOLD_SECONDS,
    redump: float = STALL_REDUMP_SECONDS,
):
    """Pure decision core of the watchdog — returns (action, stalled_since,
    last_dump_at). Actions: "none" | "dump" | "recovered"."""
    if lag >= threshold:
        if stalled_since is None:
            return "dump", now - lag, now
        if last_dump_at is None or now - last_dump_at >= redump:
            return "dump", stalled_since, now
        return "none", stalled_since, last_dump_at
    if stalled_since is not None:
        return "recovered", None, None
    return "none", None, None


def _run_watchdog(
    stop: threading.Event,
    *,
    check: float = WATCHDOG_CHECK_SECONDS,
    threshold: float = STALL_THRESHOLD_SECONDS,
    redump: float = STALL_REDUMP_SECONDS,
    dump_fn=None,
) -> None:
    """Thread body. Parameters are injectable for tests; production uses the
    module constants."""
    dump = dump_fn or dump_all_threads
    stalled_since: float | None = None
    last_dump_at: float | None = None
    while not stop.wait(check):
        beat = _beat_at
        if beat is None:
            continue
        now = time.monotonic()
        lag = now - beat
        action, stalled_since, last_dump_at = _stall_decision(
            lag, now, stalled_since, last_dump_at,
            threshold=threshold, redump=redump,
        )
        if action == "dump":
            # The stall itself is the headline — raw first (the loop being
            # stalled is exactly the condition under which structured logging
            # has been observed to wedge).
            _raw_emit(
                f"[Diagnostics] EVENT LOOP STALLED — no beat for {lag:.1f}s "
                f"(threshold {threshold:.0f}s) — dumping (#2455)\n"
            )
            dump(
                "event loop stalled — beat task has not run",
                context=f"lag={lag:.1f}s threshold={threshold:.0f}s",
            )
        elif action == "recovered":
            msg = (
                "[Diagnostics] event loop RECOVERED after a stall (#2455) — "
                "the beat is fresh again. The stall window bounds the wedge; "
                "see the THREAD DUMPs emitted during it."
            )
            _raw_emit(msg + "\n")
            logger.error(msg)


async def _beat_loop() -> None:
    global _beat_at
    while True:
        _beat_at = time.monotonic()
        await asyncio.sleep(BEAT_INTERVAL_SECONDS)


def schedule_loop_watchdog(app) -> None:
    """Attach the loop-stall watchdog to the FastAPI app. Idempotent-ish (one
    watchdog thread per process); never raises out of startup."""

    @app.on_event("startup")
    async def _start_loop_watchdog() -> None:  # pragma: no cover — wiring
        global _watchdog_thread, _beat_at
        try:
            register_main_loop(asyncio.get_running_loop())
            _beat_at = time.monotonic()
            _beat_task_ref.append(asyncio.get_running_loop().create_task(_beat_loop()))
            if _watchdog_thread is None or not _watchdog_thread.is_alive():
                _watchdog_stop.clear()
                _watchdog_thread = threading.Thread(
                    target=_run_watchdog,
                    args=(_watchdog_stop,),
                    name="loop-watchdog",
                    daemon=True,
                )
                _watchdog_thread.start()
            logger.info(
                "[Diagnostics] loop watchdog armed — a %.0fs event-loop stall "
                "dumps all threads + task chains, re-dumps every %.0fs while "
                "stalled (#2455)",
                STALL_THRESHOLD_SECONDS, STALL_REDUMP_SECONDS,
            )
        except Exception as e:  # noqa: BLE001 — diagnostics must never break boot
            logger.warning("[Diagnostics] could not arm loop watchdog: %s", e)

    @app.on_event("shutdown")
    async def _stop_loop_watchdog() -> None:  # pragma: no cover — wiring
        _watchdog_stop.set()
        for task in _beat_task_ref:
            task.cancel()
        _beat_task_ref.clear()
