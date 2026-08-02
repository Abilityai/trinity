"""JSONL fallback recovery for Claude Code stdout pipe races.

Provides authoritative post-turn recovery from
``~/.claude/projects/<dir>/<uuid>.jsonl`` — Claude Code's session record,
written via a side channel independent of stdout.

When a tool subprocess (or MCP grandchild) inherits claude's stdout fd
and wedges the agent server's reader thread, the stream-json result
event is lost — but the JSONL on disk usually contains the completed
turn.

Four recovery surfaces, all backed by a single file scan
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

4. ``_recover_completed_turn_from_jsonl`` — when the runtime reports
   ``error_during_execution`` for a turn that actually FINISHED, recover
   the completed answer instead of discarding it (#1870). Distinct from
   (1) in both trigger and boundary rule: (1) handles a clean exit whose
   stdout was lost and anchors on the newest user-input record, which the
   trailing ``<task-notification>`` captures — the #1870 defect. This one
   anchors on positive completion evidence (``stop_reason: end_turn``) and
   returns the marker message's ``message.id`` group.

   NOTE — coverage bound: this reads a file that only exists when JSONL
   persistence is on. ``headless_executor`` auto-enables it for
   ``timeout_seconds > 600`` only, and ``execute_task`` never passes
   ``persist_session=True``, so a schedule or webhook whose agent has
   ``execution_timeout_seconds <= 600`` writes NO JSONL and #1870 stays
   unfixed for it. There is no fallback: stream-json carries no completion
   signal (``message.stop_reason`` measured ``None`` in 179/179 real
   assistant records). The operator lever is raising the agent's timeout
   above 600s.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..model_context import pick_context_window
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

# #1870: the only `stop_reason` accepted as evidence that the model finished
# its turn. `tool_use` means interrupted mid-work; `max_tokens` means the
# output was truncated. `stop_sequence` is a legitimately-completed turn (63x
# main-thread across 1,075 transcripts) but stays rejected — a coverage gap
# fails safe, an uncharacterised shape does not.
_COMPLETED_STOP_REASON = "end_turn"

# #1870: tolerance on the marker's upper time bound. `task_start_iso` is a
# naive UTC value stamped `Z` and `_parse_iso_timestamp` coerces naive to UTC,
# so a container clock running AHEAD of UTC would let a previous turn's marker
# pass the lower bound. Bounding both ends makes the staleness guard fail
# closed in either direction.
#
# Calibration note: both sides of this comparison are produced INSIDE the agent
# container — `task_start_iso` by the agent server and the record timestamps by
# the `claude` CLI — and `now()` is read there too, so there is no cross-host
# skew term to size against. In practice the bound catches a corrupt or absurd
# future timestamp (and a container whose CLI ever wrote local-naive rather
# than `Z` times, which would fail closed). The exact value is therefore slop,
# not a tuned threshold; it fails closed either way, so the blast radius of it
# being wrong is "the bug is not fixed for this turn".
_MAX_FUTURE_CLOCK_SKEW_S = 300

# #1870: `stop_reason` comes from an untrusted file, so only a known-shaped
# token is ever echoed into a log line (the decline reason wants the value for
# rollout diagnosis; the real vocabulary is `end_turn`/`tool_use`/`max_tokens`/
# `stop_sequence`/`refusal`/...).
_SAFE_STOP_REASON_RE = re.compile(r"^[a-z_]{1,32}$")


def _safe_stop_reason(value: Any) -> str:
    """A log-safe rendering of an untrusted ``stop_reason``."""
    if isinstance(value, str) and _SAFE_STOP_REASON_RE.match(value):
        return value
    return "absent" if value is None else "other"


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


def _is_before(rec: Dict[str, Any], cutoff: datetime) -> bool:
    """True when a record's timestamp parses AND predates ``cutoff``.

    #1870: applied to EVERY collected record, on every path. After a 10MB seek
    the realistic failure is not "no boundary was found" (the retained tail
    almost always holds some string-content user record) but "a boundary was
    found and it belongs to a PREVIOUS turn" — which a boundary-is-None
    fallback never sees. Filtering the collected records bounds the blast
    radius regardless of which path produced them.

    An ABSENT or unparseable timestamp is not evidence of staleness, so such a
    record is kept: dropping it would silently lose answer text. All 6,663
    markers in the measured corpus carried timestamps, so this costs nothing
    in practice.
    """
    rec_dt = _record_timestamp(rec)
    return rec_dt is not None and rec_dt < cutoff


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
# Completed-turn recovery (#1870)
# ---------------------------------------------------------------------------


def _is_main_thread(entry: Dict[str, Any]) -> bool:
    """True when a record belongs to the MAIN conversation thread.

    ``isSidechain`` marks Task/subagent transcripts; ``isMeta`` marks injected
    user messages (caveats, command wrappers). Both are excluded, because
    either is fatal here: #1870's reproduction is a fan-out turn, and a
    subagent finishing emits an assistant record with ``stop_reason:
    end_turn`` while its prompt is a string-content user record — i.e. it
    satisfies BOTH the marker test and the boundary test. Ungated, a crashed
    main thread would return 200 carrying a subagent's internal thought, which
    is strictly worse than the bug being fixed.

    Measured over 1,075 transcripts (CC 2.1.181-2.1.220): current Claude Code
    writes subagent transcripts to a SEPARATE ``<session>/subagents/agent-*.jsonl``
    (0 ``isSidechain`` records in main-session files), so this is version-proofing
    rather than a live defence — but older CLIs inlined them, sidechain assistant
    records carry ``end_turn`` 961x in that same corpus, and an agent fleet runs
    whatever CLI its base image happens to carry.

    Known residual (accepted): a future CLI introducing a THIRD sub-thread
    marker would slip through. The robust escalation is a ``parentUuid`` chain
    walk rooted at the turn's opening user record; that is a second on-disk
    format dependency and is not worth it here. The decline/hit log lines make
    any such misfire diagnosable.
    """
    return not entry.get("isSidechain") and not entry.get("isMeta")


def _message_of(entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The ``message`` dict of a record, or None when it isn't one."""
    msg = entry.get("message")
    return msg if isinstance(msg, dict) else None


def _text_blocks(msg: Dict[str, Any]) -> List[str]:
    """Non-empty ``text`` block strings of a message, in order.

    Every level is isinstance-guarded: a malformed record must be skipped, not
    raise. Note ``thinking`` blocks are deliberately NOT collected — they are
    the model's internal narration, never the answer.
    """
    content = msg.get("content")
    if not isinstance(content, list):
        return []
    out: List[str] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = block.get("text")
        if isinstance(text, str) and text:
            out.append(text)
    return out


def _recover_completed_turn_from_jsonl(
    session_id: Optional[str], since_iso: Optional[str]
) -> Optional[str]:
    """Recover the answer of a turn the runtime mislabelled as failed (#1870).

    Claude Code can report ``is_error: true`` / ``error_during_execution`` for
    a turn that actually FINISHED: the model reaches ``stop_reason: end_turn``,
    a background subagent's ``<task-notification>`` lands after it, the
    follow-on turn is interrupted, and the CLI's terminal-state check sees a
    non-terminal last message. The completed answer is on disk; the caller
    would otherwise raise 502 and discard it.

    Returns the completed answer, or None when the transcript does not carry
    positive evidence that this turn finished. **Fails closed everywhere** —
    the worst case is "the bug is not fixed for this turn", never a wrong
    answer reported as a success.

    Why this is a NEW function rather than a ``since`` parameter on
    ``_recover_response_from_jsonl``: that function walks backward from the END
    OF FILE to the newest string-content user record, and the trailing
    ``<task-notification>`` IS a string-content user record. The boundary
    therefore lands PAST the completed answer and the forward scan returns
    nothing. The notification is inside any plausible time window, so a window
    could not have rescued it — the boundary rule itself is the defect. The
    #678 path is working; leaving it untouched gives it zero regression risk.

    Three independent gates, all required:

    * **main-thread only** — ``_is_main_thread`` on marker selection, the
      boundary walk AND text collection.
    * **turn-scoped** — the marker's timestamp must parse and fall inside
      ``[since_iso, now + skew]``. Fail closed when it doesn't: without a lower
      bound a genuinely-aborted turn on a ``--resume`` session would recover
      the PREVIOUS turn's answer and report it as this turn's success. The
      upper bound exists because ``task_start_iso`` is a naive-UTC value
      stamped ``Z`` and ``_parse_iso_timestamp`` coerces naive to UTC, so a
      container running AHEAD of UTC would let a stale marker pass the lower
      bound — failing OPEN into exactly the mode the guard exists to prevent.
    * **finished** — the LAST qualifying assistant record must ITSELF carry
      ``stop_reason == "end_turn"``. Deliberately stricter than "the last
      ``end_turn`` anywhere in scope": if the main thread's final record is a
      ``tool_use``, it was interrupted mid-work and no earlier ``end_turn``
      may rescue it.

    The answer is **the marker message's ``message.id`` group**, not a text
    window ending at the marker. This is the single most important property
    here. A thinking-enabled final message is written as TWO records sharing
    one ``message.id`` — ``thinking`` first, answer ``text`` second — and BOTH
    carry ``stop_reason: end_turn``:

        idx 130  assistant  message.id=msg_X  end_turn  content=[thinking]
        idx 131  assistant  message.id=msg_X  end_turn  content=[text]  <- THE ANSWER

    Measured over 1,075 real transcripts (6,663 main-thread markers), **40.6%
    of markers are thinking-only**. A window rule survives that only by
    accident — it takes the LAST marker, and the text record happens to carry
    ``end_turn`` too. Break the accident and it returns a truncated answer as a
    200 SUCCESS: ``_read_jsonl_records`` drops the final partial line on an
    interrupted write, and #1870 IS the interrupted-tail scenario. Lose that
    one line and the thinking-only record becomes the marker, so the "response"
    is the turn's intermediate narration with the final answer missing —
    stored ``success``, no error, no retry. Strictly worse than the bug being
    fixed: today the work is lost loudly, that would lose it quietly behind a
    deliverable that reads like it is about to say something.

    Grouping by ``message.id`` also makes the recovered response the RIGHT
    artifact. A normal success stores only ``result_text`` (``stream_parser``
    clears and replaces), whereas a ``(boundary, marker]`` window stores
    intermediate narration a successful run never stores — measured
    window/final text ratio p90 2.19x, p99 9.95x, max 79.5x. ``message.id``
    grouping is exactly ``result_text`` semantics.

    Concurrency is a non-issue, for a reason worth stating so it isn't
    re-derived: by the time finalize runs, ``process.wait()`` has returned, so
    ``claude`` — the only writer — has exited. The residual is precisely the
    dropped partial tail line handled above.
    """
    # A turn we cannot scope is a turn we cannot safely recover: without a
    # lower bound this degenerates into "return the newest end_turn in the
    # file", which is the stale-answer mode. Fail closed.
    since_dt = _parse_iso_timestamp(since_iso)
    if since_dt is None:
        logger.warning(
            f"event=completed_turn_recovery_declined reason=no_since_iso "
            f"session_id={session_id!r}"
        )
        return None

    records, _truncated, err = _read_jsonl_records(session_id)
    if err:
        logger.warning(
            f"event=completed_turn_recovery_declined reason={err} "
            f"session_id={session_id!r}"
        )
        return None
    if not records:
        logger.warning(
            f"event=completed_turn_recovery_declined reason=no_records "
            f"session_id={session_id!r}"
        )
        return None

    max_dt = datetime.now(timezone.utc) + timedelta(
        seconds=_MAX_FUTURE_CLOCK_SKEW_S
    )

    # --- Gate 1+2: the last main-thread assistant record inside the window.
    #
    # Every skip records a DISTINCT reason. A decline is the only observable
    # this gate emits, so collapsing causes into one label is how it rots: if a
    # future CLI moves the `timestamp` field, EVERY recovery would decline with
    # a reason an operator reads as "working as intended, the marker was old"
    # and never investigates.
    marker_idx: Optional[int] = None
    skipped: set[str] = set()
    for i, entry in enumerate(records):
        if entry.get("type") != "assistant":
            continue
        if not _is_main_thread(entry):
            skipped.add("sub_thread_only")
            continue
        if _message_of(entry) is None:
            skipped.add("malformed_message")
            continue
        rec_dt = _record_timestamp(entry)
        if rec_dt is None:
            skipped.add("marker_no_timestamp")
            continue
        if rec_dt < since_dt:
            skipped.add("stale_marker")
            continue
        if rec_dt > max_dt:
            skipped.add("future_marker")
            continue
        marker_idx = i

    if marker_idx is None:
        # Precedence = "which cause implies an ACTION". An unparseable
        # timestamp means the on-disk format moved (fix the parser); a future
        # one means the container clock is wrong (fix the clock); a malformed
        # message means the record shape moved. `stale_marker` and
        # `sub_thread_only` are the guard working exactly as designed and are
        # therefore reported last.
        reason = "no_marker"
        for candidate in (
            "marker_no_timestamp",
            "future_marker",
            "malformed_message",
            "stale_marker",
            "sub_thread_only",
        ):
            if candidate in skipped:
                reason = candidate
                break
        logger.warning(
            f"event=completed_turn_recovery_declined reason={reason} "
            f"session_id={session_id!r}"
        )
        return None

    # --- Gate 3: that record must ITSELF report the turn finished.
    marker_msg = _message_of(records[marker_idx])
    assert marker_msg is not None  # guaranteed by the scan above
    marker_stop_reason = marker_msg.get("stop_reason")
    if marker_stop_reason != _COMPLETED_STOP_REASON:
        # Includes `tool_use` (interrupted mid-work), `max_tokens` (truncated
        # output is not a completed turn), a missing field, and any
        # non-string junk. `stop_sequence` is also rejected: it is a
        # legitimately-completed turn (63x main-thread in the corpus) but
        # rejecting it merely leaves the bug unfixed for a rare shape, whereas
        # accepting a shape we have not characterised risks a wrong answer.
        #
        # Distinct from `no_marker` on purpose: "the turn was genuinely
        # interrupted" is the EXPECTED steady-state decline and must not read
        # the same as "there were no main-thread assistant records at all".
        # The observed value is the useful bit during rollout, but it comes
        # from an untrusted file, so only a known-shaped token is echoed.
        logger.warning(
            f"event=completed_turn_recovery_declined reason=not_finished "
            f"stop_reason={_safe_stop_reason(marker_stop_reason)} "
            f"session_id={session_id!r}"
        )
        return None

    # --- The answer: the marker message's `message.id` group.
    marker_id = marker_msg.get("id")
    text_parts: List[str] = []
    if isinstance(marker_id, str) and marker_id:
        for entry in records:
            if entry.get("type") != "assistant" or not _is_main_thread(entry):
                continue
            msg = _message_of(entry)
            if msg is None or msg.get("id") != marker_id:
                continue
            if _is_before(entry, since_dt):
                continue
            text_parts.extend(_text_blocks(msg))
    elif not _text_blocks(marker_msg):
        # FAIL CLOSED before the window walk ever runs.
        #
        # The `message.id` rule exists because a thinking-enabled final message
        # is two records sharing one id and BOTH carry `end_turn`, so a dropped
        # tail line leaves a thinking-only marker. Without an id we cannot
        # group — and a `(boundary, marker]` window would then happily return
        # the turn's EARLIER narration, with the answer entirely absent, as a
        # 200 SUCCESS. That is the exact silent-partial-deliverable regression
        # the id rule was written to prevent, re-opened on the only path that
        # would ever run in the world where `message.id` is gone.
        #
        # Requiring the marker record to carry text itself closes it: a
        # thinking-only marker declines here instead of laundering narration
        # into an answer. A multi-record final message still gets its full
        # window text below, because its LAST record carries text.
        logger.warning(
            f"event=completed_turn_recovery_declined reason=no_text "
            f"session_id={session_id!r}"
        )
        return None
    else:
        # Fallback only for a marker with no `message.id`: the window walk.
        # Boundary = the most recent MAIN-THREAD user record with string
        # content, searched backward FROM THE MARKER (not from the end of
        # file), so a post-answer `<task-notification>` can no longer capture
        # it and a subagent prompt can no longer become it.
        boundary_idx: Optional[int] = None
        for i in range(marker_idx - 1, -1, -1):
            entry = records[i]
            if entry.get("type") != "user" or not _is_main_thread(entry):
                continue
            msg = _message_of(entry)
            if msg is not None and isinstance(msg.get("content"), str):
                boundary_idx = i
                break
        if boundary_idx is None:
            logger.warning(
                f"event=completed_turn_recovery_declined reason=no_boundary "
                f"session_id={session_id!r}"
            )
            return None
        for entry in records[boundary_idx + 1: marker_idx + 1]:
            if entry.get("type") != "assistant" or not _is_main_thread(entry):
                continue
            msg = _message_of(entry)
            if msg is None or _is_before(entry, since_dt):
                continue
            text_parts.extend(_text_blocks(msg))

    if not text_parts:
        # FAIL CLOSED. This is the truncated-tail case: the marker is a
        # thinking-only record whose answer line was lost. Never fall back to
        # window text here — that is the silent partial-deliverable regression
        # described above.
        logger.warning(
            f"event=completed_turn_recovery_declined reason=no_text "
            f"session_id={session_id!r}"
        )
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
        # contextWindow lives under modelUsage.*; per-model capacity, not
        # per-call usage, so it is safe to copy — but modelUsage carries one
        # entry PER MODEL the turn touched, so it must be matched to the model
        # that answered rather than taken arbitrarily (#1840). The forward scan
        # above already resolved `last_assistant_model`, so it is available here
        # even though `metadata.model_name` is assigned further down; no match ⇒
        # keep the seeded fallback.
        window = pick_context_window(
            result_record.get("modelUsage"), last_assistant_model
        )
        if window is not None:
            metadata.context_window = window
            populated = True

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
