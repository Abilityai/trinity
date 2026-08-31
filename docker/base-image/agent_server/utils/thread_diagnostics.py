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

WHY NOT py-spy. It needs installing into a running production container, root
or `SYS_PTRACE` (agents run non-root with `CAP_DROP: ALL`, Invariant #17), and a
human present *while the wedge is live*. The wedge is rare, is noticed hours
later, and self-clears on the container restart that ops reach for. `faulthandler`
is in the standard library, needs no privileges, and can fire the instant the
code itself concludes a thread is stuck — which is the only moment guaranteed to
be during the event.

WHAT IT COSTS WHEN NOTHING IS WRONG. Nothing. `dump_all_threads` is called only
from the already-exceptional stuck/leaked branches, and `enable()` installs
signal handlers that fire on request or on a fatal signal. There is no polling,
no timer, and no per-line work.

SAFETY. Output goes to the agent log via `logger`, never to a file the agent
could read back and echo. Tracebacks name modules, functions and line numbers —
never values — so this cannot surface a credential the way an argument dump
would.
"""
from __future__ import annotations

import faulthandler
import logging
import signal
import sys
import threading
import traceback

logger = logging.getLogger(__name__)

# Bound the emitted text. A wedged process can hold dozens of threads and the
# agent log is shipped to Vector; a dump is diagnostic, not an archive.
MAX_DUMP_CHARS = 60_000

_enabled = False


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


def dump_all_threads(reason: str, *, context: str = "") -> str:
    """Log every thread's stack, and return what was logged.

    Uses ``sys._current_frames()`` rather than ``faulthandler.dump_traceback``
    because faulthandler writes to a real file DESCRIPTOR — it rejects an
    in-memory buffer with ``fileno`` — and the whole point here is to get the
    stacks into the agent's own structured log, which Vector already ships, not
    onto a raw stderr line nobody correlates with the execution. faulthandler
    keeps the jobs it is actually good at: the fatal-signal handler and the
    ``SIGUSR1`` on-demand dump in :func:`enable`, both of which target stderr.

    Returns the text (rather than None) so a caller can attach it to a result
    envelope, or a test can assert on it without parsing the log.

    Never raises: this runs on the teardown path of an execution that has
    already gone wrong, and a diagnostic that can break that path is worse than
    no diagnostic.
    """
    try:
        frames = sys._current_frames()          # CPython; the agent runs CPython
        by_ident = {t.ident: t for t in threading.enumerate()}
        chunks = []
        for ident, frame in frames.items():
            t = by_ident.get(ident)
            name = t.name if t is not None else "<unknown>"
            daemon = t.daemon if t is not None else "?"
            stack = "".join(traceback.format_stack(frame))
            chunks.append(f"--- thread {name} (ident={ident}, daemon={daemon})\n{stack}")
        text = "\n".join(chunks)
        if len(text) > MAX_DUMP_CHARS:
            text = text[:MAX_DUMP_CHARS] + f"\n… truncated at {MAX_DUMP_CHARS} chars"
        logger.error(
            "[Diagnostics] THREAD DUMP — %s%s | %d thread(s)\n%s",
            reason, f" | {context}" if context else "", len(frames), text,
        )
        return text
    except Exception as e:  # noqa: BLE001
        logger.warning("[Diagnostics] thread dump failed (%s): %s", reason, e)
        return ""
