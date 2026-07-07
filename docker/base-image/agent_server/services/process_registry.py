"""
Process Registry for tracking running subprocess handles.

Enables termination of executions by execution_id.
Used by both Claude Code and Gemini runtimes.

Also provides log streaming infrastructure for live execution monitoring.
"""

import signal
import subprocess
import asyncio
import logging
import time
from datetime import datetime
from typing import Dict, Optional, List, AsyncIterator
from threading import Lock

from ..utils.subprocess_pgroup import (
    signal_process_tree as _signal_process_tree,
)
from ..utils.orphan_sweep import kill_cgroup_orphans

logger = logging.getLogger(__name__)

# Issue #921: window during which a just-finished execution is still surfaced
# by `/api/executions/running` to the backend watchdog. Covers the race
# between this process's `finally: unregister()` and the backend's
# `task_execution_service.update_execution_status(SUCCESS)` write. Sized
# above the worst observed in-flight delay in the original incident (~55s)
# with comfortable headroom for backend slowness.
RECENTLY_COMPLETED_TTL_SECONDS = 300  # 5 minutes

# Issue #679: window during which a just-terminated execution is still surfaced
# as "cancelled by user" to the chat handler / async result callback. Mirrors
# RECENTLY_COMPLETED_TTL_SECONDS — the marker is best-effort observability (the
# DB CANCELLED write is the durable authority), so it just needs to comfortably
# outlast the gap between a SIGINT send and the turn's graceful exit + finalize.
TERMINATED_TTL_SECONDS = 300  # 5 minutes

# Issue #1501: default protection window for a transient-pid registration
# (see add_transient_pid) when the caller doesn't derive one from its own
# subprocess budget. Kept deliberately LOW: a transient entry whose removal
# was missed (agent-server crash between spawn and finally) shields that pid
# — and, via kernel pid reuse, potentially an unrelated future process —
# from the orphan sweep until the deadline lapses. The #817 "leaked
# processes eventually die" guarantee therefore degrades by minutes,
# never permanently. Callers with a known subprocess timeout should pass
# ``ttl_seconds=timeout + headroom`` so the protection window can never be
# shorter than the run it protects (a TTL below the subprocess budget would
# silently reintroduce the #1501 mid-run kill for long hooks).
TRANSIENT_PID_TTL_SECONDS = 300  # 5 minutes


class ProcessRegistry:
    """
    Registry for tracking running subprocess handles.
    Enables termination of executions by execution_id.

    Thread-safe via mutex lock for all operations.

    Also provides log streaming infrastructure:
    - Each execution can have multiple log subscribers (asyncio.Queue)
    - Log entries are published to all subscribers as they arrive
    - Subscribers receive entries until execution completes
    """

    def __init__(self):
        self._processes: Dict[str, dict] = {}
        self._lock = Lock()
        # Log streaming: execution_id -> list of subscriber queues
        self._log_subscribers: Dict[str, List[asyncio.Queue]] = {}
        # Buffered logs: execution_id -> list of log entries (for late joiners)
        self._log_buffers: Dict[str, List[dict]] = {}
        # Maximum buffer size per execution (prevents memory bloat)
        self._max_buffer_size = 1000
        # Issue #921: execution_id -> unix timestamp when unregister() ran.
        # Entries past RECENTLY_COMPLETED_TTL_SECONDS are evicted lazily on
        # read. Capped indirectly by traffic — at agent's ~10 concurrent
        # max with 5 min retention this is a few dozen entries at peak.
        self._recently_completed: Dict[str, float] = {}
        # Issue #679: execution_id -> unix timestamp when terminate() sent a
        # kill-signal. Read (not popped) by was_terminated() so the chat handler
        # and async result callback can label a graceful-exit-0 / SIGKILL→504
        # turn as "cancelled". Lazy TTL eviction on read; cleared on register()
        # so a reused execution_id (#678 in-line retry) can't inherit the label.
        self._terminated: Dict[str, float] = {}
        # Issue #1501: pid -> unix-timestamp EXPIRY DEADLINE for short-lived
        # agent-server child subprocesses (brain-orb convention hooks) that
        # must survive the orphan sweep for the duration of their run. Fed
        # into active_execution_pids() (the #912 canonical sweep-allowlist
        # source), so every sweep site inherits the protection. Entries are
        # removed by the spawner's finally; the stored deadline is the
        # lazy-evicted backstop against a missed removal.
        self._transient_pids: Dict[int, float] = {}

    def register(self, execution_id: str, process: subprocess.Popen, metadata: dict = None):
        """
        Register a running process.

        Args:
            execution_id: Unique identifier for this execution
            process: The subprocess.Popen handle
            metadata: Optional metadata (type, message preview, etc.)
        """
        with self._lock:
            self._processes[execution_id] = {
                "process": process,
                "started_at": datetime.utcnow(),
                "metadata": metadata or {}
            }
            # Initialize log streaming structures
            self._log_subscribers[execution_id] = []
            self._log_buffers[execution_id] = []
            # Issue #679 (C10): clear any stale cancel marker so an execution_id
            # reused by the #678 in-line reader-race retry can't inherit the
            # previous attempt's "cancelled" label.
            self._terminated.pop(execution_id, None)
            logger.info(f"[ProcessRegistry] Registered execution {execution_id}")

    def unregister(self, execution_id: str):
        """Unregister a completed process and signal stream end to subscribers."""
        with self._lock:
            if execution_id in self._processes:
                del self._processes[execution_id]
                logger.info(f"[ProcessRegistry] Unregistered execution {execution_id}")

            # Signal end of stream to all subscribers
            if execution_id in self._log_subscribers:
                for queue in self._log_subscribers[execution_id]:
                    try:
                        queue.put_nowait({"type": "stream_end"})
                    except asyncio.QueueFull:
                        pass
                del self._log_subscribers[execution_id]

            # Clean up buffer (keep for a bit for late requests, but this is fine)
            if execution_id in self._log_buffers:
                del self._log_buffers[execution_id]

            # Issue #921: mark for the recently-completed window so the
            # backend watchdog doesn't see this as missing during the race
            # between this `finally: unregister()` and the backend writing
            # `success` to the schedule_executions row.
            self._recently_completed[execution_id] = time.time()

    def terminate(self, execution_id: str, graceful_timeout: int = 5) -> dict:
        """
        Terminate a running process.

        Uses graceful termination (SIGINT) first, then force kills (SIGKILL)
        if the process doesn't respond within the timeout.

        Args:
            execution_id: The execution to terminate
            graceful_timeout: Seconds to wait after SIGINT before SIGKILL

        Returns:
            dict with termination status:
            - {"success": True, "returncode": int} on success
            - {"success": False, "reason": "not_found"} if not registered
            - {"success": False, "reason": "already_finished", "returncode": int}
            - {"success": False, "reason": "error", "error": str}
        """
        with self._lock:
            entry = self._processes.get(execution_id)
            if not entry:
                return {"success": False, "reason": "not_found"}

            process = entry["process"]
            if process.poll() is not None:
                # Already finished
                returncode = process.returncode
                del self._processes[execution_id]
                return {"success": False, "reason": "already_finished", "returncode": returncode}

        # Read pgid from the entry metadata (captured at register time)
        # so we can signal the full process group even if the parent has
        # already been reaped (Issue #407).
        pgid = (entry.get("metadata") or {}).get("pgid")

        # Terminate outside lock to avoid blocking other operations
        try:
            # Graceful termination first (SIGINT = Ctrl+C)
            # Claude Code handles SIGINT gracefully, finishing current tool.
            # Issue #407: signal the whole process group so hook
            # grandchildren don't linger holding our pipe FDs.
            logger.info(f"[ProcessRegistry] Sending SIGINT to execution {execution_id} (process group)")
            _signal_process_tree(process, signal.SIGINT, pgid=pgid)

            # Issue #679: record the cancel marker immediately after a successful
            # SIGINT send — still-running branch only (NOT already_finished /
            # not_found, handled above; NOT on signal-failure, which raises into
            # the `except` below and skips this). The send is causally before the
            # subprocess's graceful exit, so was_terminated() is set before the
            # chat handler observes execute_headless returning — race-free.
            with self._lock:
                self._terminated[execution_id] = time.time()

            try:
                process.wait(timeout=graceful_timeout)
                logger.info(f"[ProcessRegistry] Execution {execution_id} terminated gracefully")
            except subprocess.TimeoutExpired:
                # Force kill if graceful didn't work
                logger.warning(f"[ProcessRegistry] Force killing execution {execution_id} (process group)")
                _signal_process_tree(process, signal.SIGKILL, pgid=pgid)
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    logger.error(
                        f"[ProcessRegistry] Execution {execution_id} did not exit after SIGKILL"
                    )

            returncode = process.returncode

            # Issue #817 follow-up: cgroup-walk sweep for descendants
            # that escaped the pgid kill via setsid, FD detachment, or
            # env stripping. Best-effort — never fail termination on
            # this. Issue #912: delegated to active_execution_pids()
            # which is the single canonical source for this allowlist,
            # also used by the periodic orphan sweeper and the drain-
            # time sweep in subprocess_pgroup. Excluding ``execution_id``
            # so the sweep doesn't try to preserve the process we just
            # killed.
            try:
                preserve = self.active_execution_pids(
                    exclude_execution_id=execution_id
                )
                killed = kill_cgroup_orphans(extra_pids=preserve)
                if killed:
                    logger.info(
                        f"[ProcessRegistry] Cgroup sweep killed {killed} "
                        f"orphan(s) after terminating {execution_id} "
                        f"(preserved {len(preserve)} allowlisted pid(s): "
                        f"other in-flight executions + transient "
                        f"subprocesses)"
                    )
            except Exception:
                logger.exception(
                    f"[ProcessRegistry] cgroup sweep raised after "
                    f"terminating {execution_id} — continuing"
                )

            with self._lock:
                if execution_id in self._processes:
                    del self._processes[execution_id]

            return {"success": True, "returncode": returncode}

        except Exception as e:
            logger.error(f"[ProcessRegistry] Error terminating {execution_id}: {e}")
            return {"success": False, "reason": "error", "error": str(e)}

    def get_status(self, execution_id: str) -> Optional[dict]:
        """
        Get status of a registered process.

        Returns None if execution not found.
        """
        with self._lock:
            entry = self._processes.get(execution_id)
            if not entry:
                return None

            process = entry["process"]
            poll_result = process.poll()

            return {
                "execution_id": execution_id,
                "running": poll_result is None,
                "returncode": poll_result,
                "started_at": entry["started_at"].isoformat(),
                "metadata": entry["metadata"]
            }

    def list_running(self) -> list:
        """List all currently running executions.

        ``pid`` was added to the returned shape (#817 follow-up) so the
        periodic orphan sweeper can preserve every active claude
        process and its descendants. Existing callers that only read
        ``execution_id`` / ``started_at`` / ``metadata`` are
        unaffected.
        """
        with self._lock:
            result = []
            for exec_id, entry in self._processes.items():
                process = entry["process"]
                if process.poll() is None:
                    result.append({
                        "execution_id": exec_id,
                        "pid": process.pid,
                        "started_at": entry["started_at"].isoformat(),
                        "metadata": entry["metadata"]
                    })
            return result

    def add_transient_pid(self, pid: int, *, ttl_seconds: Optional[float] = None):
        """Issue #1501: shield a short-lived agent-server child subprocess
        from the orphan sweep for the duration of its run.

        The sweep's hard-protect walk goes from the sweeper UP to PID 1,
        never down to the agent-server's other children — so a subprocess
        the agent-server itself spawns (a brain-orb convention hook) is
        indistinguishable from a genuine orphan and gets SIGKILLed by any
        sweep that fires while it runs. Registering the pid here puts it
        (and, via the allowlist's ppid walk, its descendants) on every
        sweep site's allowlist. The spawner MUST pair this with
        :meth:`remove_transient_pid` in a ``finally``; the TTL backstop
        only bounds the damage of a missed removal, it is not the
        removal mechanism.

        Args:
            pid: the spawned subprocess pid. Non-positive/None is ignored.
            ttl_seconds: protection window. Callers with a known subprocess
                timeout MUST derive this from it (``timeout + headroom``) —
                a window shorter than the run would lazily evict mid-run
                and reintroduce the #1501 kill. ``None`` / non-finite /
                non-positive falls back to TRANSIENT_PID_TTL_SECONDS.
        """
        if not isinstance(pid, int) or pid <= 0:
            return
        window = TRANSIENT_PID_TTL_SECONDS
        if isinstance(ttl_seconds, (int, float)) and ttl_seconds > 0 and ttl_seconds != float("inf"):
            window = float(ttl_seconds)
        with self._lock:
            self._transient_pids[pid] = time.time() + window
        logger.debug(f"[ProcessRegistry] Registered transient pid {pid} (window={window:.0f}s)")

    def remove_transient_pid(self, pid: int):
        """Issue #1501: drop a transient pid from the sweep allowlist.

        Idempotent — a double remove or a remove of an unknown/expired pid
        is a no-op, so ``finally`` blocks can call it unconditionally.
        """
        with self._lock:
            self._transient_pids.pop(pid, None)
        logger.debug(f"[ProcessRegistry] Unregistered transient pid {pid}")

    def active_execution_pids(self, exclude_execution_id: Optional[str] = None) -> List[int]:
        """Snapshot of the registry-vouched sweep-allowlist pids.

        Returned for the orphan-sweep allowlist. Issue #912: any caller of
        :func:`kill_cgroup_orphans` that runs while *other* executions are
        still in flight must pass these so the sweep doesn't SIGKILL their
        claude subprocesses. The list intentionally includes both the
        ``pid`` (resolves descendants via ppid walk in
        :mod:`orphan_allowlist`) and the captured ``pgid`` from metadata
        (covers grandchildren spawned with ``setsid`` that escape the ppid
        chain). Duplicates are fine — the allowlist resolver de-dupes.

        Issue #1501: despite the (kept-for-compat) name, the snapshot is
        no longer *strictly* execution pids — it also carries live
        transient pids (agent-server hook subprocesses registered via
        :meth:`add_transient_pid`), which every sweep site must preserve
        for exactly the same reason. Expired transient entries are lazily
        evicted here.

        Args:
            exclude_execution_id: When set, the entry with that id is
                omitted from the snapshot. Used by ``terminate()`` so the
                cgroup sweep doesn't try to preserve a process we just
                killed. Transient pids are never affected by this filter.

        Returns:
            A new list (snapshot under the registry lock) of ints. Empty
            list when no other executions are running.
        """
        result: List[int] = []
        with self._lock:
            for exec_id, entry in self._processes.items():
                if exclude_execution_id is not None and exec_id == exclude_execution_id:
                    continue
                process = entry["process"]
                if process.poll() is not None:
                    continue
                result.append(process.pid)
                pgid = (entry.get("metadata") or {}).get("pgid")
                if isinstance(pgid, int) and pgid > 0:
                    result.append(pgid)
            # Issue #1501: live transient pids ride along. Collect-then-delete
            # eviction (never mutate while iterating), mirroring the other
            # lazy-TTL maps in this class. Entries store their expiry
            # deadline directly (per-registration window).
            now = time.time()
            expired = [pid for pid, deadline in self._transient_pids.items() if deadline < now]
            for pid in expired:
                del self._transient_pids[pid]
            result.extend(self._transient_pids.keys())
        return result

    def list_recently_completed_ids(self) -> List[str]:
        """Issue #921: IDs of executions that finished within the last
        RECENTLY_COMPLETED_TTL_SECONDS.

        Surfaced via /api/executions/running so the backend watchdog can
        treat "agent finished it moments ago" the same as "agent still
        has it" — no orphan recovery, no false-positive. Expired entries
        are dropped lazily here; no separate sweeper needed.
        """
        cutoff = time.time() - RECENTLY_COMPLETED_TTL_SECONDS
        with self._lock:
            # Drop expired entries while we're holding the lock.
            expired = [eid for eid, ts in self._recently_completed.items() if ts < cutoff]
            for eid in expired:
                del self._recently_completed[eid]
            return list(self._recently_completed.keys())

    def was_terminated(self, execution_id: str) -> bool:
        """Issue #679: True if terminate() sent a kill-signal for this execution
        within the last TERMINATED_TTL_SECONDS.

        Read-only — it does NOT consume the marker, so the graceful-exit relabel
        path and a later SIGKILL→504 check both observe it. Expired entries are
        dropped lazily here (mirrors list_recently_completed_ids); no separate
        sweeper needed.
        """
        cutoff = time.time() - TERMINATED_TTL_SECONDS
        with self._lock:
            expired = [eid for eid, ts in self._terminated.items() if ts < cutoff]
            for eid in expired:
                del self._terminated[eid]
            return execution_id in self._terminated

    def cleanup_finished(self) -> int:
        """
        Remove entries for finished processes.

        Returns the count of cleaned up entries.
        """
        with self._lock:
            finished = [
                exec_id for exec_id, entry in self._processes.items()
                if entry["process"].poll() is not None
            ]
            for exec_id in finished:
                del self._processes[exec_id]
            if finished:
                logger.info(f"[ProcessRegistry] Cleaned up {len(finished)} finished processes")
            return len(finished)

    # ========================================================================
    # Log Streaming Methods
    # ========================================================================

    def publish_log_entry(self, execution_id: str, entry: dict):
        """
        Publish a log entry to all subscribers for an execution.

        Called from claude_code.py (chat path) and headless_executor.py
        (task path) as each stdout line is processed.
        Non-blocking: if a subscriber's queue is full, the entry is dropped for that subscriber.

        Args:
            execution_id: The execution ID
            entry: The raw JSON log entry from Claude Code
        """
        with self._lock:
            # Add to buffer for late joiners
            if execution_id in self._log_buffers:
                buffer = self._log_buffers[execution_id]
                buffer.append(entry)
                # Trim buffer if too large
                if len(buffer) > self._max_buffer_size:
                    self._log_buffers[execution_id] = buffer[-self._max_buffer_size:]

            # Publish to all subscribers
            if execution_id in self._log_subscribers:
                for queue in self._log_subscribers[execution_id]:
                    try:
                        queue.put_nowait(entry)
                    except asyncio.QueueFull:
                        # Drop entry for this slow subscriber
                        logger.warning(f"[ProcessRegistry] Log queue full for execution {execution_id}, dropping entry")

    def subscribe_logs(self, execution_id: str) -> asyncio.Queue:
        """
        Subscribe to log entries for an execution.

        Returns a queue that will receive log entries as they arrive.
        First sends all buffered entries, then streams new ones.
        Returns None if execution not found.

        Args:
            execution_id: The execution ID to subscribe to

        Returns:
            asyncio.Queue to receive log entries, or None if not found
        """
        queue = asyncio.Queue(maxsize=500)

        with self._lock:
            # Check if execution exists (or recently existed with buffer)
            if execution_id not in self._log_subscribers and execution_id not in self._log_buffers:
                return None

            # Send buffered entries first
            if execution_id in self._log_buffers:
                for entry in self._log_buffers[execution_id]:
                    try:
                        queue.put_nowait(entry)
                    except asyncio.QueueFull:
                        break

            # Register as subscriber if execution is still running
            if execution_id in self._log_subscribers:
                self._log_subscribers[execution_id].append(queue)
            else:
                # Execution finished, just send stream_end
                try:
                    queue.put_nowait({"type": "stream_end"})
                except asyncio.QueueFull:
                    pass

        return queue

    def unsubscribe_logs(self, execution_id: str, queue: asyncio.Queue):
        """
        Unsubscribe from log entries.

        Args:
            execution_id: The execution ID
            queue: The queue to unsubscribe
        """
        with self._lock:
            if execution_id in self._log_subscribers:
                try:
                    self._log_subscribers[execution_id].remove(queue)
                except ValueError:
                    pass

    def get_buffered_logs(self, execution_id: str) -> Optional[List[dict]]:
        """
        Get all buffered log entries for an execution.

        Used for non-streaming requests (e.g., page refresh on completed execution).

        Args:
            execution_id: The execution ID

        Returns:
            List of log entries, or None if execution not found
        """
        with self._lock:
            if execution_id in self._log_buffers:
                return list(self._log_buffers[execution_id])
            return None

    def is_execution_running(self, execution_id: str) -> bool:
        """Check if an execution is currently running."""
        with self._lock:
            return execution_id in self._processes

    def get_last_error(self, execution_id: str) -> Optional[dict]:
        """
        Extract the last error from an execution's log buffer.

        Scans the log buffer for error indicators:
        - Messages with is_error=True
        - Messages with error_type set
        - Result messages indicating failure

        Args:
            execution_id: The execution ID

        Returns:
            Dict with error_type and error_message, or None if no error found
        """
        with self._lock:
            if execution_id not in self._log_buffers:
                return None

            buffer = self._log_buffers[execution_id]
            if not buffer:
                return None

            # Scan buffer in reverse (most recent first) for error indicators
            last_error_type = None
            last_error_message = None

            for entry in reversed(buffer):
                if not isinstance(entry, dict):
                    continue

                # Check for is_error flag on result messages
                if entry.get("is_error"):
                    last_error_type = "execution_error"
                    last_error_message = entry.get("result", "")
                    break

                # Check for error field on assistant messages
                if entry.get("error"):
                    last_error_type = entry.get("error")
                    # Try to extract error text from message content
                    message = entry.get("message", {})
                    content = message.get("content", [])
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            last_error_message = block.get("text", "")
                            break
                    if last_error_type:
                        break

                # Check for tool_result with is_error
                if entry.get("type") == "assistant" or entry.get("type") == "user":
                    message = entry.get("message", {})
                    content = message.get("content", [])
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_result":
                            if block.get("is_error"):
                                last_error_type = "tool_error"
                                result_content = block.get("content", [])
                                for item in result_content:
                                    if isinstance(item, dict) and item.get("type") == "text":
                                        last_error_message = item.get("text", "")
                                        break
                                break

            if last_error_type or last_error_message:
                return {
                    "error_type": last_error_type,
                    "error_message": last_error_message[:2000] if last_error_message else None
                }

            return None


# Global instance
_process_registry: Optional[ProcessRegistry] = None


def get_process_registry() -> ProcessRegistry:
    """Get the global process registry instance."""
    global _process_registry
    if _process_registry is None:
        _process_registry = ProcessRegistry()
    return _process_registry
