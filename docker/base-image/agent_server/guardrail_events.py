"""
Guardrail event forwarder (trinity-enterprise#165, GUARD-002 §28.2.2).

The guardrail hooks (`/opt/trinity/hooks/*.py`) append one JSON record per
decision to the spool `/logs/guardrails.jsonl` (`lib.log_event`). Nothing read
that file before this, and it is on no volume, so no guardrail decision ever
left the container. This loop tails the spool and posts the guardrail events to
the platform under the agent's own `TRINITY_MCP_API_KEY` (the heartbeat's
least-privilege rule: no key, no loop).

What leaves the container is a projection through an allowlist (`project`):
never the command text, a traceback, an error string or the deny's prose.

Delivery is debounced, not swept: a record is posted within ~2 s of the first
unsent append. Nothing relies on a shutdown flush: PID 1 is bash, which has no
SIGTERM handler, so `docker stop` kills this process without running its
shutdown handlers. Records still in the spool at a stop or recreate are lost;
the window is the delivery bound plus whatever queued while the platform was
unreachable.

Fails open everywhere: no error here reaches a tool call, a turn or an
execution. The spool and this file are both writable by the agent, so the
events are agent-reported, not an audit trail.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import random
import re
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote

import httpx

from .utils.credential_sanitizer import sanitize_text

logger = logging.getLogger(__name__)

SPOOL_PATH = "/logs/guardrails.jsonl"
ROTATED_SUFFIX = ".1"
STATE_PATH = "/logs/.guardrails-forwarder.json"
INGEST_PATH = "/api/agents/{name}/guardrail-events"
SCHEMA = 1

MAX_BATCH_EVENTS = 500
MAX_BATCH_BYTES = 256 * 1024
# Room for the envelope around the events, so the whole body stays under the cap.
_ENVELOPE_RESERVE = 1024
MAX_LINE_BYTES = 64 * 1024

POLL_SECONDS = 0.5
# Poll + debounce stay under the 2 s delivery bound (ent#165 §6.2).
FLUSH_DEBOUNCE_SECONDS = 1.0
KEEPALIVE_SECONDS = 300.0
STARTUP_DELAY_SECONDS = 5.0
POST_TIMEOUT_SECONDS = 5.0
STEP_ERROR_PAUSE_SECONDS = 5.0
BACKOFF_BASE_SECONDS = 2.0
BACKOFF_MAX_SECONDS = 300.0
UNSUPPORTED_RETRY_SECONDS = 3600.0
RETRY_AFTER_DEFAULT_SECONDS = 60.0
RETRY_AFTER_MAX_SECONDS = 3600.0
# Two rotated generations at most: the live spool and `.1`.
ROTATE_BYTES = 5 * 1024 * 1024
# A hook opens, appends and closes in milliseconds; a write that opened the path
# just before the rename lands in `.1`, so `.1` is read to its end for this long.
ROTATE_GRACE_SECONDS = 2.0

# The projection allowlist (ent#165 §6.2). Ids that would be cut are dropped
# instead: a truncated id names something else.
_ID_FIELDS = {"event_id": 64, "execution_id": 128, "session_id": 128, "tool_use_id": 128}
_ENUM_FIELDS = {"kind": 64, "hook": 64, "code": 64, "verb": 32, "tier": 32, "turn_kind": 32}
_TEXT_FIELDS = {"tool": 128, "rule": 200, "path": 512}
_PATTERNS_MAX = 16
_PATTERN_NAME_MAX = 64
# A lone surrogate survives the spool's json round trip (a hook input can carry
# one escaped) but not httpx's strict UTF-8 encoding of the body, so one would
# fail every POST of its batch. Same rule as the platform: U+FFFD in text, and
# an id holding one is dropped (the id check below sees the change).
_LONE_SURROGATE = re.compile("[\ud800-\udfff]")

Post = Callable[[Dict[str, Any]], Awaitable[Tuple[int, Optional[str]]]]


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------
def _iso_z(ts: Any) -> Optional[str]:
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return None
    try:
        # isfinite itself raises OverflowError on an int too large for a float.
        if not math.isfinite(ts):
            return None
        moment = datetime.fromtimestamp(ts, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _clean(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    return sanitize_text(_LONE_SURROGATE.sub("\ufffd", value))


def project(record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The part of a spool record that may leave the container, or None when the
    record is not a forwardable event (no `kind`, no `hook`, no id, no time)."""
    kind, hook = record.get("kind"), record.get("hook")
    if not isinstance(kind, str) or not kind or not isinstance(hook, str) or not hook:
        return None
    ts = _iso_z(record.get("ts"))
    event_id = record.get("event_id")
    if ts is None or not isinstance(event_id, str) or not event_id:
        return None
    out: Dict[str, Any] = {"ts": ts}
    for name, bound in _ID_FIELDS.items():
        if name not in record:
            continue
        raw = record[name]
        value = _clean(raw)
        # An id the sanitizer changed carried a credential: drop it, never ship half.
        out[name] = value if value is not None and value == raw and len(value) <= bound else None
    if out.get("event_id") is None:
        return None
    for name, bound in _ENUM_FIELDS.items():
        if name in record:
            value = _clean(record[name])
            out[name] = value if value is not None and len(value) <= bound else None
    for name, bound in _TEXT_FIELDS.items():
        if name in record:
            value = _clean(record[name])
            out[name] = value[:bound] if value is not None else None
    patterns = record.get("patterns")
    if isinstance(patterns, list):
        out["patterns"] = [
            _clean(p)[:_PATTERN_NAME_MAX] for p in patterns[:_PATTERNS_MAX] if isinstance(p, str)
        ]
    return out


def _classify(line: bytes) -> Tuple[str, Optional[Dict[str, Any]]]:
    """('event', projected) | ('skip', None) | ('legacy', None) | ('malformed', None)."""
    try:
        record = json.loads(line)
    except (ValueError, UnicodeDecodeError):
        return "malformed", None
    if not isinstance(record, dict):
        return "malformed", None
    if "v" not in record:
        return "legacy", None
    if record.get("kind") is None or record.get("hook") is None:
        return "skip", None  # allow-type or not from a guardrail hook: local only
    projected = project(record)
    return ("event", projected) if projected is not None else ("malformed", None)


# ---------------------------------------------------------------------------
# The forwarder
# ---------------------------------------------------------------------------
class _Batch:
    __slots__ = ("events", "size", "malformed", "legacy", "end_ino", "end_offset")

    def __init__(self, ino: Optional[int], offset: int) -> None:
        self.events: List[Dict[str, Any]] = []
        self.size = 0
        self.malformed = 0
        self.legacy = 0
        self.end_ino = ino
        self.end_offset = offset


def _stat(path: str) -> Optional[os.stat_result]:
    try:
        return os.stat(path)
    except FileNotFoundError:
        return None


class Forwarder:
    """Spool position, batching and the response policy. `tick(now)` does one
    step on a monotonic clock and returns how long to wait before the next."""

    def __init__(self, post: Post, *, runtime: str, rand: Callable[[], float] = random.random) -> None:
        self._post = post
        self._runtime = runtime
        self._rand = rand
        self._ino: Optional[int] = None
        self._offset = 0
        self._dropped = 0
        self._rotated_at: Optional[float] = None
        self._pending_since: Optional[float] = None
        self._last_ok_at = -math.inf
        self._next_attempt_at = -math.inf
        self._failures = 0
        self._cap = MAX_BATCH_EVENTS
        self._logged: set = set()
        self._load_state()

    # -- state ------------------------------------------------------------------
    def _load_state(self) -> None:
        try:
            with open(STATE_PATH) as f:
                state = json.load(f)
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            self._log_once("state", logging.WARNING, "guardrail events: unreadable forwarder state, starting over")
            return
        if isinstance(state, dict):
            ino, offset, dropped = state.get("ino"), state.get("offset"), state.get("dropped")
            if isinstance(ino, int) and isinstance(offset, int) and offset >= 0:
                self._ino, self._offset = ino, offset
            if isinstance(dropped, int) and dropped >= 0:
                self._dropped = dropped

    def _save_state(self) -> None:
        tmp = STATE_PATH + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump({"ino": self._ino, "offset": self._offset, "dropped": self._dropped}, f)
            os.replace(tmp, STATE_PATH)
        except OSError:
            self._log_once("state-write", logging.WARNING, "guardrail events: cannot persist the spool offset")

    def _log_once(self, key: str, level: int, message: str, *args: Any) -> None:
        if key in self._logged:
            logger.debug(message, *args)
            return
        self._logged.add(key)
        logger.log(level, message, *args)

    # -- position -----------------------------------------------------------------
    def _rotated_path(self) -> str:
        return SPOOL_PATH + ROTATED_SUFFIX

    def _locate(self) -> Tuple[Optional[str], Optional[os.stat_result]]:
        """The file the position is in. A position that matches neither file
        (first start, recreate, the file replaced) restarts at the live spool."""
        live, rotated = _stat(SPOOL_PATH), _stat(self._rotated_path())
        if self._ino is not None:
            if rotated is not None and rotated.st_ino == self._ino:
                return self._rotated_path(), rotated
            if live is not None and live.st_ino == self._ino:
                if live.st_size < self._offset:
                    self._offset = 0  # truncated under us
                return SPOOL_PATH, live
        self._ino = live.st_ino if live is not None else None
        self._offset = 0
        return (SPOOL_PATH, live) if live is not None else (None, None)

    def _grace_over(self, now: float) -> bool:
        return self._rotated_at is None or now - self._rotated_at >= ROTATE_GRACE_SECONDS

    def _only_a_cut_line_left(self, path: str, size: int) -> bool:
        """What is left of `path` is one line with no newline. A longer one is
        skipped whole by `_read_batch`, so only a short one can stall."""
        if size - self._offset >= MAX_LINE_BYTES:
            return False
        with open(path, "rb") as f:
            f.seek(self._offset)
            return b"\n" not in f.read()

    def _advance_past_rotated(self, now: float) -> Tuple[Optional[str], Optional[os.stat_result]]:
        path, st = self._locate()
        if path == self._rotated_path() and st is not None and self._grace_over(now):
            if self._offset < st.st_size and self._only_a_cut_line_left(path, st.st_size):
                # A write cut short (a full disk, a kill mid-write). Nothing appends
                # to `.1` past the grace, so no newline will ever finish it.
                self._dropped += 1
                self._offset = st.st_size
            if self._offset >= st.st_size:
                live = _stat(SPOOL_PATH)
                self._ino = live.st_ino if live is not None else None
                self._offset = 0
                self._save_state()
                return (SPOOL_PATH, live) if live is not None else (None, None)
        return path, st

    def _has_unread(self) -> bool:
        path, st = self._locate()
        if st is not None and st.st_size > self._offset:
            return True
        if path == self._rotated_path():
            live = _stat(SPOOL_PATH)
            return live is not None and live.st_size > 0
        return False

    def _maybe_rotate(self, now: float) -> None:
        if not self._grace_over(now):
            # A hook may still be appending to `.1`; replacing it now would put
            # that write in an unlinked file, neither delivered nor counted.
            return
        live = _stat(SPOOL_PATH)
        if live is None or live.st_size < ROTATE_BYTES:
            return
        rotated = _stat(self._rotated_path())
        if self._ino == live.st_ino:
            # Reading the live file: it becomes `.1` and the position follows it.
            # The old `.1` was drained before the position moved to the live file.
            os.replace(SPOOL_PATH, self._rotated_path())
        else:
            # Still draining `.1`: what is left there is the oldest, and it goes.
            if rotated is not None and self._ino == rotated.st_ino:
                lost = self._count_events(self._rotated_path(), self._offset)
                if lost:
                    self._dropped += lost
                    self._log_once("spool-full", logging.WARNING,
                                   "guardrail events: spool full, dropped the oldest %d", lost)
            os.replace(SPOOL_PATH, self._rotated_path())
            self._ino, self._offset = live.st_ino, 0
        self._rotated_at = now
        self._save_state()

    def _count_events(self, path: str, offset: int) -> int:
        count = 0
        try:
            with open(path, "rb") as f:
                f.seek(offset)
                for line in f:
                    if line.endswith(b"\n") and _classify(line)[0] == "event":
                        count += 1
        except OSError:
            return 0
        return count

    def _read_batch(self, now: float) -> _Batch:
        path, st = self._advance_past_rotated(now)
        batch = _Batch(self._ino, self._offset)
        if path is None or st is None:
            return batch
        cap_bytes = MAX_BATCH_BYTES - _ENVELOPE_RESERVE
        with open(path, "rb") as f:
            f.seek(self._offset)
            while len(batch.events) < min(self._cap, MAX_BATCH_EVENTS):
                start = f.tell()
                line = f.readline(MAX_LINE_BYTES)
                if not line:
                    break
                if not line.endswith(b"\n"):
                    if len(line) < MAX_LINE_BYTES:
                        break  # a hook is still writing this line
                    while line and not line.endswith(b"\n"):  # oversize: skip it whole
                        line = f.readline(MAX_LINE_BYTES)
                    batch.malformed += 1
                    batch.end_offset = f.tell()
                    continue
                verdict, event = _classify(line)
                if verdict == "event":
                    size = len(json.dumps(event)) + 2
                    if batch.events and batch.size + size > cap_bytes:
                        f.seek(start)
                        break
                    batch.events.append(event)
                    batch.size += size
                elif verdict == "malformed":
                    batch.malformed += 1
                elif verdict == "legacy":
                    batch.legacy += 1
                batch.end_offset = f.tell()
        return batch

    def _commit(self, batch: _Batch) -> None:
        self._ino, self._offset = batch.end_ino, batch.end_offset
        if batch.legacy:
            self._log_once("legacy", logging.INFO,
                           "guardrail events: skipped %d record(s) written by an older hook", batch.legacy)
        self._save_state()

    # -- the step -------------------------------------------------------------------
    async def tick(self, now: float) -> float:
        self._maybe_rotate(now)
        if now < self._next_attempt_at:
            return POLL_SECONDS
        if self._has_unread():
            if self._pending_since is None:
                self._pending_since = now
        else:
            self._pending_since = None
        flush_due = self._pending_since is not None and now - self._pending_since >= FLUSH_DEBOUNCE_SECONDS
        keepalive_due = now - self._last_ok_at >= KEEPALIVE_SECONDS
        if not (flush_due or keepalive_due):
            return POLL_SECONDS

        start = (self._ino, self._offset)
        batch = self._read_batch(now)
        if not batch.events and not keepalive_due:
            # Only records that stay local: move past them, nothing to post.
            self._dropped += batch.malformed
            self._commit(batch)
            self._pending_since = None
            # No progress (a rotated file still in its grace window): wait a poll.
            progressed = (batch.end_ino, batch.end_offset) != start
            return 0.0 if progressed and self._has_unread() else POLL_SECONDS

        dropped = self._dropped + batch.malformed
        body = {"schema": SCHEMA, "runtime": self._runtime, "events": batch.events, "dropped": dropped}
        try:
            status, retry_after = await self._post(body)
        except (httpx.HTTPError, OSError) as exc:  # OSError covers connection and timeout errors
            return self._back_off(now, f"transport error ({type(exc).__name__})")
        return self._handle(now, status, retry_after, batch)

    def _handle(self, now: float, status: int, retry_after: Optional[str], batch: _Batch) -> float:
        n = len(batch.events)
        if 200 <= status < 300:
            self._dropped = 0
            self._commit(batch)
            self._last_ok_at = now
            self._failures = 0
            self._cap = min(MAX_BATCH_EVENTS, self._cap * 2)
            self._logged.discard("unreachable")
            if self._has_unread():
                self._pending_since = now - FLUSH_DEBOUNCE_SECONDS  # drain the backlog now
                return 0.0
            self._pending_since = None
            return POLL_SECONDS
        if status == 413 and n > 1:
            self._cap = max(1, n // 2)
            return 0.0
        if status in (413, 422) and n >= 1:
            # 413 on one event: the platform refuses it at any size. 422: the
            # envelope was malformed. Either way this batch is dropped and counted.
            self._dropped += n + batch.malformed
            self._commit(batch)
            self._log_once(f"refused-{status}", logging.WARNING,
                           "guardrail events: the platform refused a batch (%s), dropped %d", status, n)
            return 0.0
        if status == 429:
            self._next_attempt_at = now + _retry_after_seconds(retry_after)
            return POLL_SECONDS
        if status in (401, 403, 404):
            # 404: a platform without the ingest route. 401/403: the key is not
            # accepted. Keep the spool (bounded by rotation) and ask again hourly.
            self._next_attempt_at = now + UNSUPPORTED_RETRY_SECONDS
            self._log_once(f"status-{status}", logging.INFO,
                           "guardrail events: the platform answered %s; keeping the spool, retrying hourly", status)
            return POLL_SECONDS
        return self._back_off(now, f"status {status}")

    def _back_off(self, now: float, why: str) -> float:
        self._failures += 1
        ceiling = min(BACKOFF_MAX_SECONDS, BACKOFF_BASE_SECONDS * (2 ** min(self._failures - 1, 16)))
        delay = ceiling / 2 + self._rand() * ceiling / 2
        self._next_attempt_at = now + delay
        self._log_once("unreachable", logging.INFO,
                       "guardrail events: delivery failed (%s); retrying with backoff", why)
        return POLL_SECONDS


def _retry_after_seconds(value: Optional[str]) -> float:
    try:
        seconds = float(value) if value is not None else RETRY_AFTER_DEFAULT_SECONDS
    except ValueError:
        seconds = RETRY_AFTER_DEFAULT_SECONDS
    if not math.isfinite(seconds):
        seconds = RETRY_AFTER_DEFAULT_SECONDS
    return min(max(seconds, 1.0), RETRY_AFTER_MAX_SECONDS)


# ---------------------------------------------------------------------------
# Loop + wiring
# ---------------------------------------------------------------------------
_WIRE_RUNTIME = {"claude": "claude-code", "codex": "codex", "gemini": "gemini"}


def _runtime() -> str:
    from .services.execution_env import runtime_family

    return _WIRE_RUNTIME.get(runtime_family() or "", "unknown")


def _agent_name() -> str:
    from .state import agent_state

    return agent_state.agent_name or ""


async def run_guardrail_forwarder() -> None:
    """Background loop. A fault in one step is logged and the loop goes on."""
    backend_url = os.getenv("TRINITY_BACKEND_URL")
    mcp_key = os.getenv("TRINITY_MCP_API_KEY")
    agent_name = _agent_name()
    if not backend_url or not mcp_key or not agent_name:
        logger.info("guardrail events: no backend URL, agent key or agent name; not forwarding")
        return
    url = backend_url.rstrip("/") + INGEST_PATH.format(name=quote(agent_name, safe=""))
    headers = {"Authorization": f"Bearer {mcp_key}"}
    logger.info("guardrail event forwarder started (agent=%s)", agent_name)
    await asyncio.sleep(STARTUP_DELAY_SECONDS)
    async with httpx.AsyncClient(timeout=POST_TIMEOUT_SECONDS) as client:

        async def post(body: Dict[str, Any]) -> Tuple[int, Optional[str]]:
            response = await client.post(url, json=body, headers=headers)
            return response.status_code, response.headers.get("Retry-After")

        forwarder = Forwarder(post, runtime=_runtime())
        warned = False
        while True:
            try:
                delay = await forwarder.tick(time.monotonic())
            except Exception:  # noqa: BLE001 — observability fails open; the loop must not die
                # Once at WARNING with the traceback, then quiet: no log spam.
                logger.log(logging.DEBUG if warned else logging.WARNING,
                           "guardrail events: forwarder step failed", exc_info=True)
                warned = True
                delay = STEP_ERROR_PAUSE_SECONDS
            await asyncio.sleep(delay)


def schedule_guardrail_forwarder(app) -> None:
    """Attach startup/shutdown handlers, gated on backend URL + agent key (the
    heartbeat's rule). Shutdown cancels the task; nothing is flushed then."""
    if not (os.getenv("TRINITY_BACKEND_URL") and os.getenv("TRINITY_MCP_API_KEY")):
        logger.info("guardrail event forwarder disabled (needs TRINITY_BACKEND_URL + TRINITY_MCP_API_KEY)")
        return

    task_ref: list = []

    @app.on_event("startup")
    async def _start_guardrail_forwarder() -> None:
        task_ref.append(asyncio.create_task(run_guardrail_forwarder()))

    @app.on_event("shutdown")
    async def _stop_guardrail_forwarder() -> None:
        for task in task_ref:
            task.cancel()
