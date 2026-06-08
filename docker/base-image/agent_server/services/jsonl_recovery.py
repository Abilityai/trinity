"""JSONL fallback recovery for Claude Code stdout pipe races.

Provides authoritative post-turn recovery from
``~/.claude/projects/<dir>/<uuid>.jsonl`` — Claude Code's session record,
written via a side channel independent of stdout.

When a tool subprocess (or MCP grandchild) inherits claude's stdout fd
and wedges the agent server's reader thread, the stream-json result
event is lost — but the JSONL on disk usually contains the completed
turn.

Three recovery surfaces, all backed by a single file scan
(``_read_jsonl_records``):

1. ``_recover_response_from_jsonl`` — when stdout dropped assistant text,
   walk forward from the most recent user-input boundary and reconstruct
   the assistant text emitted during this turn.

2. ``_extract_compact_events_from_jsonl`` — Claude Code's stdout
   ``compact_boundary`` event strips the ``compactMetadata`` envelope.
   The JSONL has the canonical shape; extract events ≥ ``since_iso`` to
   scope to the just-completed turn.

3. ``_recover_metadata_from_jsonl`` — when stdout lost the trailing
   ``result`` line, recover ``cost_usd``, ``duration_ms``, ``num_turns``,
   per-call ``usage`` token counts, ``context_window``, and the
   ``model`` name from the JSONL so the failure row carries telemetry
   instead of null everything (#678).
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..models import CompactEvent, ExecutionMetadata

logger = logging.getLogger(__name__)

_JSONL_PROJECTS_DIR = "/home/developer/.claude/projects/-home-developer"
_MAX_JSONL_BYTES_FOR_RECOVERY = 10 * 1024 * 1024  # 10MB cap on read

# session_id originates from Claude Code's stream-json output (UUIDs) or
# our own uuid.uuid4() — both are alnum + hyphen only. Reject anything
# else before path construction so a corrupted stdout line can't drive
# the reader at a file outside the projects dir. Belt: this regex.
# Suspenders: resolve() + is_relative_to() below.
_SAFE_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


# ---------------------------------------------------------------------------
# Shared snapshot reader
# ---------------------------------------------------------------------------


def _read_jsonl_records(session_id: Optional[str]) -> Tuple[List[Dict[str, Any]], bool, Optional[str]]:
    """Single-pass JSONL reader shared by all recovery helpers.

    Returns ``(records, truncated, error)``:

    - ``records`` — parsed JSON dicts in file order. Lines that fail
      ``json.loads`` or aren't dicts are silently dropped (concurrent
      writes can leave a partial tail line).
    - ``truncated`` — True if the JSONL exceeded the 10MB cap and we
      seeked to the tail. The first (possibly partial) line after seek
      is dropped, so prior turns may be missing. For metadata recovery
      this is safe (latest assistant.usage is at the tail anyway); for
      text recovery the user-input boundary may be lost.
    - ``error`` — short reason when the file can't be read at all
      (``no_session_id``, ``invalid_session_id``,
      ``path_outside_projects_dir``, ``file_missing``,
      ``read_failed:<exc>``). None on success.

    Callers handle empty records / truncation per their own semantics.
    """
    if not session_id:
        return [], False, "no_session_id"

    if not _SAFE_SESSION_ID_RE.match(session_id):
        logger.warning(
            f"[JSONL Recovery] Rejecting session_id with unexpected shape: "
            f"{session_id!r}"
        )
        return [], False, "invalid_session_id"

    projects_root = Path(_JSONL_PROJECTS_DIR).resolve()
    jsonl_path = (projects_root / f"{session_id}.jsonl").resolve()
    if not jsonl_path.is_relative_to(projects_root):
        logger.warning(
            f"[JSONL Recovery] Rejecting resolved path outside projects dir: "
            f"{jsonl_path}"
        )
        return [], False, "path_outside_projects_dir"
    if not jsonl_path.exists():
        return [], False, "file_missing"

    truncated = False
    try:
        size = jsonl_path.stat().st_size
        if size > _MAX_JSONL_BYTES_FOR_RECOVERY:
            truncated = True
            with jsonl_path.open("rb") as f:
                f.seek(-_MAX_JSONL_BYTES_FOR_RECOVERY, os.SEEK_END)
                # Skip the partial first line after seeking mid-file.
                f.readline()
                raw = f.read().decode("utf-8", errors="replace")
        else:
            raw = jsonl_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[JSONL Recovery] Failed to read {jsonl_path}: {e}")
        return [], False, f"read_failed:{type(e).__name__}"

    records: List[Dict[str, Any]] = []
    for line in raw.split("\n"):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(entry, dict):
            records.append(entry)

    return records, truncated, None


# ---------------------------------------------------------------------------
# Timestamp parsing
# ---------------------------------------------------------------------------


def _parse_iso_timestamp(ts: Any) -> Optional[datetime]:
    """Parse an ISO 8601 timestamp tolerantly.

    Accepts ``Z`` suffix, ``+00:00``, fractional seconds, or naive ISO.
    Returns aware UTC datetime, or None when ``ts`` isn't parseable.
    String-compare was fragile (`Z` vs `+00:00`); the compact-events
    branch worked because all compact records share the `Z` form, but
    extending to assistant/result records exposed the gap.
    """
    if not isinstance(ts, str) or not ts:
        return None
    s = ts.strip()
    # fromisoformat handles +00:00 natively. Translate trailing Z.
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _record_timestamp(rec: Dict[str, Any]) -> Optional[datetime]:
    """Pull the timestamp out of a JSONL record.

    Claude Code records carry the timestamp at the top level for
    compact_boundary / system events; for assistant and user records
    the wrapper has a top-level ``timestamp`` too. Some result-shaped
    records have no timestamp at all — return None and let the caller
    decide whether to include them.
    """
    return _parse_iso_timestamp(rec.get("timestamp"))


# ---------------------------------------------------------------------------
# Text recovery (refactored on top of the snapshot reader)
# ---------------------------------------------------------------------------


def _recover_response_from_jsonl(session_id: Optional[str]) -> Optional[str]:
    """Try to recover an assistant text response from a Claude Code JSONL.

    Returns the concatenated text of all assistant.text blocks emitted
    after the most recent user-input message in the JSONL, or None when:

    - session_id is missing
    - the JSONL file doesn't exist or can't be read
    - no user-input boundary is found (shouldn't happen in practice)
    - no assistant text was emitted after the boundary (Claude died
      mid-tool-call before writing any text — genuinely incomplete).

    The boundary uses the shape difference between user inputs (string
    content) and tool_results (list-of-dicts content) — Claude Code
    records them with different types in the JSONL.
    """
    records, _truncated, err = _read_jsonl_records(session_id)
    if err == "file_missing":
        logger.info(
            f"event=jsonl_unavailable_for_recovery reason=file_missing "
            f"session_id={session_id}"
        )
        return None
    if not records:
        return None

    # Walk backward to find the boundary: the most recent user-INPUT
    # message (content is a string, not a list). tool_result entries
    # also have type=user but their content is a list of dicts.
    boundary_idx = None
    for i in range(len(records) - 1, -1, -1):
        entry = records[i]
        if entry.get("type") != "user":
            continue
        msg = entry.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            boundary_idx = i
            break

    if boundary_idx is None:
        return None

    text_parts: List[str] = []
    for entry in records[boundary_idx + 1:]:
        if entry.get("type") != "assistant":
            continue
        msg = entry.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text = block.get("text") or ""
                if text:
                    text_parts.append(text)

    if not text_parts:
        return None
    return "\n".join(text_parts)


# ---------------------------------------------------------------------------
# Compact event extraction (refactored on top of the snapshot reader)
# ---------------------------------------------------------------------------


def _extract_compact_events_from_jsonl(
    session_id: Optional[str], since_iso: Optional[str] = None
) -> List["CompactEvent"]:
    """Read compact_boundary records out of a Claude Code JSONL.

    Claude Code's ``--output-format stream-json --verbose`` emits
    ``compact_boundary`` events to stdout but strips the
    ``compactMetadata`` envelope (we get the event-fired signal but no
    pre/post/duration detail). The JSONL on disk has the canonical
    shape:

        {"type": "system", "subtype": "compact_boundary",
         "compactMetadata": {"trigger":"auto", "preTokens":175061,
                             "postTokens":5904, "durationMs":73651},
         "timestamp": "2026-05-04T13:01:56.959Z", ...}

    Called AFTER a turn completes to populate
    ``metadata.compact_events`` with the real detail fields.
    ``since_iso`` filters to compact records at or after the given ISO
    timestamp — used to scope the result to the just-completed turn
    when the JSONL has compact records from prior turns.

    Returns an empty list when the session_id is missing, the file
    doesn't exist, or no compact records are present.
    """
    records, _truncated, _err = _read_jsonl_records(session_id)
    if not records:
        return []

    since_dt = _parse_iso_timestamp(since_iso) if since_iso else None
    events: List["CompactEvent"] = []
    for entry in records:
        if entry.get("type") != "system" or entry.get("subtype") != "compact_boundary":
            continue
        ts_str = entry.get("timestamp") if isinstance(entry.get("timestamp"), str) else None
        rec_dt = _parse_iso_timestamp(ts_str)
        if since_dt and rec_dt and rec_dt < since_dt:
            continue
        cm = entry.get("compactMetadata") or {}
        if not isinstance(cm, dict):
            cm = {}
        events.append(CompactEvent(
            trigger=cm.get("trigger"),
            pre_tokens=cm.get("preTokens"),
            post_tokens=cm.get("postTokens"),
            duration_ms=cm.get("durationMs"),
            timestamp=ts_str,
        ))

    return events


# ---------------------------------------------------------------------------
# Metadata recovery (#678)
# ---------------------------------------------------------------------------


def _recover_metadata_from_jsonl(
    session_id: Optional[str],
    since_iso: Optional[str],
    metadata: ExecutionMetadata,
) -> bool:
    """Back-fill ``metadata`` from the on-disk JSONL when stdout lost
    the trailing ``result`` line.

    Issue #678: when the reader-thread race fires before the parser
    appends the result line, ``_recover_metadata_from_raw_messages``
    cannot recover (nothing to recover from). The JSONL on disk is the
    side-channel ground truth: every assistant message carries
    per-call ``usage``, the ``result`` event carries cumulative cost
    and duration when present.

    Token-accounting invariant (mirrors
    ``_recover_metadata_from_raw_messages``): per-call ``usage`` on the
    LATEST assistant message wins. Cumulative ``result.usage`` is a
    fallback only — using it would double-count cached tokens.

    Short-circuits if ``metadata.cost_usd`` is already populated
    (someone else won). Returns True if any field was populated.

    On miss, emits ``event=jsonl_unavailable_for_recovery`` with a
    reason so operators can distinguish "JSONL salvage tried and
    failed" from "JSONL salvage never ran."
    """
    if metadata is None:
        return False
    if metadata.cost_usd is not None or metadata.duration_ms is not None:
        return False

    records, truncated, err = _read_jsonl_records(session_id)
    if err:
        logger.info(
            f"event=jsonl_unavailable_for_recovery reason={err} "
            f"session_id={session_id}"
        )
        return False
    if not records:
        logger.info(
            f"event=jsonl_unavailable_for_recovery reason=empty_jsonl "
            f"session_id={session_id}"
        )
        return False

    since_dt = _parse_iso_timestamp(since_iso) if since_iso else None

    # Walk forward, tracking:
    #  - the latest assistant.usage block (per-call invariant)
    #  - the latest assistant.message.model (for model_name)
    #  - a result-shaped record if any (cost/duration/num_turns/contextWindow)
    last_assistant_usage: Optional[Dict[str, Any]] = None
    last_assistant_model: Optional[str] = None
    result_record: Optional[Dict[str, Any]] = None
    scanned = 0

    for entry in records:
        rec_dt = _record_timestamp(entry)
        if since_dt and rec_dt and rec_dt < since_dt:
            continue
        scanned += 1
        et = entry.get("type")

        if et == "assistant":
            msg = entry.get("message")
            if isinstance(msg, dict):
                usage = msg.get("usage")
                if isinstance(usage, dict) and usage:
                    last_assistant_usage = usage
                model = msg.get("model")
                if isinstance(model, str) and model:
                    last_assistant_model = model
        elif et == "result":
            # JSONL `result` records mirror the stream-json result event.
            # Some Claude versions emit them, some don't.
            result_record = entry

    if scanned == 0:
        logger.info(
            f"event=jsonl_unavailable_for_recovery reason=pre_dates_turn "
            f"session_id={session_id} since_iso={since_iso}"
        )
        return False

    populated = False

    if result_record is not None:
        cost = result_record.get("total_cost_usd")
        dur = result_record.get("duration_ms")
        turns = result_record.get("num_turns")
        if cost is not None:
            metadata.cost_usd = cost
            populated = True
        if dur is not None:
            metadata.duration_ms = dur
            populated = True
        if turns is not None:
            metadata.num_turns = turns
            populated = True
        # contextWindow lives under modelUsage.*; per-model capacity,
        # not per-call usage, so always safe to copy.
        model_usage = result_record.get("modelUsage") or {}
        if isinstance(model_usage, dict):
            for _, model_data in model_usage.items():
                if isinstance(model_data, dict) and "contextWindow" in model_data:
                    metadata.context_window = model_data["contextWindow"]
                    populated = True
                    break

    # Per-call usage from the LATEST assistant message. This must NOT
    # be overwritten by cumulative result.usage (would double-count
    # cached tokens — see stream_parser.py:10-27).
    if last_assistant_usage is not None:
        metadata.input_tokens = last_assistant_usage.get("input_tokens", 0) or 0
        metadata.output_tokens = last_assistant_usage.get("output_tokens", 0) or 0
        metadata.cache_creation_tokens = last_assistant_usage.get("cache_creation_input_tokens", 0) or 0
        metadata.cache_read_tokens = last_assistant_usage.get("cache_read_input_tokens", 0) or 0
        populated = True
    elif result_record is not None:
        # No assistant.usage in scope — fall back to cumulative result.usage
        # so callers see *some* token signal. Logged so dashboards can
        # treat it differently when the data exists.
        usage = result_record.get("usage")
        if isinstance(usage, dict):
            metadata.input_tokens = usage.get("input_tokens", 0) or 0
            metadata.output_tokens = usage.get("output_tokens", 0) or 0
            metadata.cache_creation_tokens = usage.get("cache_creation_input_tokens", 0) or 0
            metadata.cache_read_tokens = usage.get("cache_read_input_tokens", 0) or 0
            populated = True
            logger.info(
                f"[JSONL Metadata Recovery] No assistant.usage in scope, "
                f"fell back to cumulative result.usage for session_id={session_id}"
            )

    if last_assistant_model:
        metadata.model_name = last_assistant_model
        populated = True

    if populated:
        metadata.recovered_from_jsonl = True
        logger.info(
            f"event=jsonl_metadata_recovery session_id={session_id} "
            f"cost={metadata.cost_usd} duration_ms={metadata.duration_ms} "
            f"num_turns={metadata.num_turns} model={metadata.model_name} "
            f"input_tokens={metadata.input_tokens} cache_read={metadata.cache_read_tokens} "
            f"truncated={truncated}"
        )
    else:
        logger.info(
            f"event=jsonl_unavailable_for_recovery reason=no_recoverable_fields "
            f"session_id={session_id}"
        )

    return populated
