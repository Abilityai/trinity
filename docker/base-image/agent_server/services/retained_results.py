"""Retained terminals — a completed turn's result kept where the backend can ask
for it after the connection that was waiting for it is gone (#2944).

A synchronously dispatched turn (``/api/task`` or ``/api/chat`` with the backend
awaiting the response — every trigger while ``DISPATCH_ASYNC`` is off) has
exactly one consumer: the backend coroutine holding the HTTP connection. A
backend recreate destroys that consumer; the turn still finishes and bills, and
the process registry's recently-completed marker (#921) is a bare timestamp.
The backend watchdog then had only ``/last-error`` to ask before writing
``failed`` over a run whose success sat on this disk.

So every terminal a sync handler produces is written here as the SAME typed
envelope the #1083 result callback POSTs — success, cancelled, or the failure
built from the executor's ``HTTPException`` — and the watchdog reads it back
through ``GET /api/executions/{id}/result`` before it ever fails a row.

Deliberately a **separate** directory from ``result_callback._PENDING_DIR``:
the startup sweep re-POSTs everything in that directory to the callback
endpoint, where a sync envelope is a permanent 409 (no async marker) and is
then deleted — the retained result would be lost by the machinery meant to
save it. The write/containment shape is the same on purpose.

Disk-backed so a record survives an agent-server restart between completion
and the claim. Bounded three ways — a TTL long enough to outlive every window
the backend may legitimately withhold a claim for (its 120-minute stale sweep,
the widest execution timeout, an ``unknown`` in-flight verdict while Redis is
down), a file-count cap, and a per-record transcript cap (the response,
metadata and session id are always kept; a transcript past the callback's own
16 MB bound is dropped and the record says so).

Best-effort at every sink: retention can never fail the turn it records.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Env-overridable so the unit suite (tests/unit/conftest.py) never writes into
# the developer's real home directory — the same reason `TRINITY_DB_PATH` exists.
RETAINED_DIR = Path(
    os.getenv("TRINITY_RETAINED_RESULTS_DIR")
    or os.path.expanduser("~/.trinity/retained-results")
)

#: Outlives the backend's `EXECUTION_STALE_TIMEOUT_MINUTES` (120) + the widest
#: execution timeout (7200 s) + the bound on an `unknown` in-flight verdict.
RETAINED_RESULT_TTL_SECONDS = 6 * 3600
#: Newest kept; a busy agent on a small volume must not accumulate transcripts.
RETAINED_RESULT_MAX_FILES = 50
#: Mirrors `routers/agents.py::_MAX_CALLBACK_LOG_BYTES` — the backend would
#: refuse a larger transcript anyway. Over the cap the transcript is dropped,
#: never the result.
RETAINED_LOG_MAX_BYTES = 16_000_000

# Same charset rule as `result_callback._SAFE_EXECUTION_ID`: execution ids are
# backend-generated (urlsafe token / UUID). The `temp-…` fallback id carries a
# `.` and legitimately no-ops here — it only exists when the backend sent no id,
# so there is no row for a watchdog to claim.
_SAFE_EXECUTION_ID = re.compile(r"\A[A-Za-z0-9_-]{1,128}\Z")


def is_safe_execution_id(execution_id: Any) -> bool:
    return bool(isinstance(execution_id, str) and _SAFE_EXECUTION_ID.match(execution_id))


def _path_for(execution_id: str) -> Optional[str]:
    """Contained path under RETAINED_DIR, or None when the id would escape it.

    The #950 normpath+startswith guard, inlined at every sink that uses it
    (CodeQL only honours a CWE-022 barrier in the same function as the sink)."""
    base = os.path.normpath(str(RETAINED_DIR))
    dest = os.path.normpath(os.path.join(base, f"{execution_id}.json"))
    if dest == base or not dest.startswith(base + os.sep):
        return None
    return dest


def _bounded_envelope(envelope: Dict) -> tuple[Dict, bool]:
    """Drop a transcript past the cap; keep everything else. Returns
    (envelope, truncated_log)."""
    log = envelope.get("execution_log")
    if log is None:
        return envelope, False
    try:
        if len(json.dumps(log)) <= RETAINED_LOG_MAX_BYTES:
            return envelope, False
    except (TypeError, ValueError):
        pass  # unserializable → drop it below; the record must still be valid JSON
    bounded = dict(envelope)
    bounded["execution_log"] = None
    return bounded, True


def record(execution_id: Any, envelope: Dict) -> bool:
    """Retain ``envelope`` for ``execution_id``. Returns True when written.

    Overwrites an existing record for the same id — a SUB-003 subscription-switch
    retry reuses the execution id, and the LAST terminal is the one that stands
    (attempt-1 FAILED(auth) → final SUCCESS)."""
    if not is_safe_execution_id(execution_id):
        return False
    try:
        RETAINED_DIR.mkdir(parents=True, exist_ok=True)
        base = os.path.normpath(str(RETAINED_DIR))
        dest = os.path.normpath(os.path.join(base, f"{execution_id}.json"))
        if dest == base or not dest.startswith(base + os.sep):
            raise ValueError(f"retained-result path escapes {base}: {execution_id!r}")
        bounded, truncated = _bounded_envelope(envelope)
        now = time.time()
        payload = {
            "execution_id": execution_id,
            "retained_at": now,
            "retained_at_iso": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
            "truncated_log": truncated,
            "envelope": bounded,
        }
        tmp = f"{dest}.tmp"
        Path(tmp).write_text(json.dumps(payload))
        Path(tmp).replace(dest)
        _evict(keep=dest)
        return True
    except Exception:  # noqa: BLE001 — retention is best-effort, never fails the turn
        logger.debug("[#2944] could not retain result for %r", execution_id, exc_info=True)
        return False


def get(execution_id: Any) -> Optional[Dict]:
    """The retained record for ``execution_id`` (the dict written by ``record``),
    or None when there is none / it expired / the id is unsafe."""
    if not is_safe_execution_id(execution_id):
        return None
    dest = _path_for(execution_id)
    if dest is None:
        return None
    try:
        raw = json.loads(Path(dest).read_text())
    except FileNotFoundError:
        return None
    except Exception:  # noqa: BLE001 — a half-written/garbage file reads as absent
        logger.debug("[#2944] unreadable retained result %r", execution_id, exc_info=True)
        return None
    retained_at = raw.get("retained_at") if isinstance(raw, dict) else None
    if not isinstance(retained_at, (int, float)):
        return None
    if time.time() - retained_at > RETAINED_RESULT_TTL_SECONDS:
        _unlink(dest)
        return None
    if not isinstance(raw.get("envelope"), dict):
        return None
    return raw


def _unlink(path: str) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


def _evict(keep: Optional[str] = None) -> None:
    """Drop expired records, then the oldest past the file cap. Called after
    every write so the directory is bounded without a sweeper."""
    try:
        files = [p for p in RETAINED_DIR.glob("*.json")]
    except Exception:  # noqa: BLE001
        return
    cutoff = time.time() - RETAINED_RESULT_TTL_SECONDS
    live: list[tuple[float, Path]] = []
    for p in files:
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff and str(p) != keep:
            _unlink(str(p))
            continue
        live.append((mtime, p))
    excess = len(live) - RETAINED_RESULT_MAX_FILES
    if excess > 0:
        # The record just written is never a candidate, whatever its mtime.
        others = sorted((t for t in live if str(t[1]) != keep), key=lambda t: t[0])
        for _, p in others[:excess]:
            _unlink(str(p))
