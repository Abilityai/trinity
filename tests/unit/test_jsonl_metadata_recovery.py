"""Tests for #678 JSONL metadata recovery.

When the stdout reader thread loses the trailing ``result`` line, the
on-disk JSONL is the side-channel ground truth. ``_recover_metadata_from_jsonl``
walks the records, back-fills cost / duration / tokens / model_name from
the latest assistant message + (optionally) a JSONL ``result`` record.

Token-accounting invariant: per-call ``usage`` on the LATEST assistant
message wins over cumulative ``result.usage``. Tests pin this.

Module under test:
    docker/base-image/agent_server/services/jsonl_recovery.py::
        _recover_metadata_from_jsonl
        _read_jsonl_records
        _recover_response_from_jsonl
        _extract_compact_events_from_jsonl
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_server.models import ExecutionMetadata
from agent_server.services import jsonl_recovery


# ---------------------------------------------------------------------------
# JSONL fixture builders
# ---------------------------------------------------------------------------


def _write_jsonl(tmp_path: Path, session_id: str, records: list[dict]) -> Path:
    """Write a JSONL file at the path layout the recovery helpers expect.

    Patches ``_JSONL_PROJECTS_DIR`` so the helpers read from tmp_path.
    Returns the path written so callers can inspect / mutate.
    """
    proj_dir = tmp_path / "projects"
    proj_dir.mkdir(parents=True, exist_ok=True)
    target = proj_dir / f"{session_id}.jsonl"
    with target.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    return target


def _user_input(text: str, ts: str | None = None) -> dict:
    rec = {"type": "user", "message": {"content": text}}
    if ts:
        rec["timestamp"] = ts
    return rec


def _assistant_message(
    text: str,
    usage: dict | None = None,
    model: str | None = None,
    ts: str | None = None,
) -> dict:
    msg: dict = {
        "type": "assistant",
        "message": {
            "content": [{"type": "text", "text": text}],
        },
    }
    if usage is not None:
        msg["message"]["usage"] = usage
    if model is not None:
        msg["message"]["model"] = model
    if ts is not None:
        msg["timestamp"] = ts
    return msg


def _result_record(
    cost: float | None = None,
    duration_ms: int | None = None,
    num_turns: int | None = None,
    context_window: int | None = None,
    cumulative_usage: dict | None = None,
    ts: str | None = None,
) -> dict:
    rec: dict = {"type": "result"}
    if cost is not None:
        rec["total_cost_usd"] = cost
    if duration_ms is not None:
        rec["duration_ms"] = duration_ms
    if num_turns is not None:
        rec["num_turns"] = num_turns
    if cumulative_usage is not None:
        rec["usage"] = cumulative_usage
    if context_window is not None:
        rec["modelUsage"] = {"claude-sonnet-4-5": {"contextWindow": context_window}}
    if ts is not None:
        rec["timestamp"] = ts
    return rec


@pytest.fixture
def patch_projects_dir(tmp_path, monkeypatch):
    """Re-route the JSONL projects dir to tmp_path for the duration of a test."""
    proj_dir = tmp_path / "projects"
    proj_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(jsonl_recovery, "_JSONL_PROJECTS_DIR", str(proj_dir))
    return proj_dir


# ---------------------------------------------------------------------------
# _recover_metadata_from_jsonl — happy path
# ---------------------------------------------------------------------------


def test_full_recovery_from_complete_turn(tmp_path, patch_projects_dir):
    """Complete turn with assistant.usage + result record: cost, duration,
    num_turns, context_window, model_name, and per-call tokens all populate."""
    session_id = "abc"
    records = [
        _user_input("hello", ts="2026-05-11T10:00:00.000Z"),
        _assistant_message(
            "hi",
            usage={
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 500,
            },
            model="claude-sonnet-4-5",
            ts="2026-05-11T10:00:01.000Z",
        ),
        _result_record(
            cost=0.0123,
            duration_ms=5000,
            num_turns=2,
            context_window=200000,
            ts="2026-05-11T10:00:02.000Z",
        ),
    ]
    _write_jsonl(tmp_path, session_id, records)

    meta = ExecutionMetadata(session_id=session_id)
    ok = jsonl_recovery._recover_metadata_from_jsonl(session_id, since_iso=None, metadata=meta)

    assert ok is True
    assert meta.cost_usd == pytest.approx(0.0123)
    assert meta.duration_ms == 5000
    assert meta.num_turns == 2
    assert meta.context_window == 200000
    assert meta.model_name == "claude-sonnet-4-5"
    assert meta.input_tokens == 100
    assert meta.output_tokens == 20
    assert meta.cache_read_tokens == 500
    assert meta.recovered_from_jsonl is True


def test_per_call_usage_wins_over_cumulative_result_usage(tmp_path, patch_projects_dir):
    """Token-accounting invariant: per-call usage on the latest assistant
    message wins. Result.usage is cumulative across all API calls in the
    turn and would double-count cached tokens if used directly."""
    session_id = "tok"
    records = [
        _user_input("q"),
        _assistant_message(
            "step 1",
            usage={
                "input_tokens": 200,
                "output_tokens": 30,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 800,
            },
            model="claude-sonnet-4-5",
        ),
        _result_record(
            cost=0.05,
            duration_ms=10000,
            num_turns=10,
            cumulative_usage={
                # Cumulative across all calls — much larger
                "input_tokens": 5000,
                "output_tokens": 300,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 8000,
            },
        ),
    ]
    _write_jsonl(tmp_path, session_id, records)
    meta = ExecutionMetadata(session_id=session_id)

    ok = jsonl_recovery._recover_metadata_from_jsonl(session_id, since_iso=None, metadata=meta)

    assert ok is True
    # Cost / duration / num_turns from result record (cumulative is fine here)
    assert meta.cost_usd == pytest.approx(0.05)
    # But tokens MUST come from the assistant message, not result.usage
    assert meta.input_tokens == 200
    assert meta.cache_read_tokens == 800


def test_recovery_with_no_result_record(tmp_path, patch_projects_dir):
    """When the JSONL has no result-shaped record (some Claude versions
    don't emit one), we still recover assistant tokens + model_name."""
    session_id = "noresult"
    records = [
        _user_input("q"),
        _assistant_message(
            "ans",
            usage={
                "input_tokens": 50,
                "output_tokens": 10,
                "cache_creation_input_tokens": 100,
                "cache_read_input_tokens": 200,
            },
            model="claude-haiku-4-5",
        ),
    ]
    _write_jsonl(tmp_path, session_id, records)
    meta = ExecutionMetadata(session_id=session_id)

    ok = jsonl_recovery._recover_metadata_from_jsonl(session_id, since_iso=None, metadata=meta)

    assert ok is True
    # No result record → cost/duration/num_turns stay None — honest
    assert meta.cost_usd is None
    assert meta.duration_ms is None
    assert meta.num_turns is None
    # But tokens + model are recovered
    assert meta.input_tokens == 50
    assert meta.cache_read_tokens == 200
    assert meta.model_name == "claude-haiku-4-5"
    assert meta.recovered_from_jsonl is True


# ---------------------------------------------------------------------------
# since_iso scoping
# ---------------------------------------------------------------------------


def test_since_iso_filters_prior_turn_pollution(tmp_path, patch_projects_dir):
    """JSONL accumulates across turns of a resumed session. ``since_iso``
    must scope recovery to records at or after the current turn's start —
    prior turns' tokens MUST NOT leak into this turn's metadata."""
    session_id = "scoped"
    records = [
        # PRIOR turn — should be filtered out
        _user_input("old q", ts="2026-05-11T09:00:00.000Z"),
        _assistant_message(
            "old answer",
            usage={"input_tokens": 9999, "output_tokens": 9999, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 9999},
            model="claude-old-model",
            ts="2026-05-11T09:00:01.000Z",
        ),
        # CURRENT turn — should be the only data recovered
        _user_input("new q", ts="2026-05-11T10:00:00.000Z"),
        _assistant_message(
            "new answer",
            usage={"input_tokens": 100, "output_tokens": 20, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 500},
            model="claude-sonnet-4-5",
            ts="2026-05-11T10:00:01.000Z",
        ),
    ]
    _write_jsonl(tmp_path, session_id, records)
    meta = ExecutionMetadata(session_id=session_id)

    # since_iso anchors AT the new turn's start
    ok = jsonl_recovery._recover_metadata_from_jsonl(
        session_id, since_iso="2026-05-11T10:00:00.000Z", metadata=meta
    )

    assert ok is True
    assert meta.input_tokens == 100
    assert meta.cache_read_tokens == 500
    assert meta.model_name == "claude-sonnet-4-5"


def test_since_iso_filters_everything_returns_false(tmp_path, patch_projects_dir):
    """When all records pre-date since_iso, recovery emits unavailable
    and returns False (caller stays on the hard-failure path)."""
    session_id = "nothing-in-scope"
    records = [
        _user_input("q", ts="2026-05-11T09:00:00.000Z"),
        _assistant_message(
            "a",
            usage={"input_tokens": 1, "output_tokens": 1, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
            model="x",
            ts="2026-05-11T09:00:01.000Z",
        ),
    ]
    _write_jsonl(tmp_path, session_id, records)
    meta = ExecutionMetadata(session_id=session_id)

    ok = jsonl_recovery._recover_metadata_from_jsonl(
        session_id, since_iso="2026-05-11T10:00:00.000Z", metadata=meta
    )

    assert ok is False
    assert meta.cost_usd is None
    assert meta.input_tokens == 0
    assert meta.recovered_from_jsonl is False


# ---------------------------------------------------------------------------
# Edge cases: missing file, malformed lines, short-circuit, timestamps
# ---------------------------------------------------------------------------


def test_missing_file_returns_false(tmp_path, patch_projects_dir):
    meta = ExecutionMetadata(session_id="nope")
    ok = jsonl_recovery._recover_metadata_from_jsonl("nope", since_iso=None, metadata=meta)
    assert ok is False


def test_no_session_id_returns_false():
    meta = ExecutionMetadata()
    ok = jsonl_recovery._recover_metadata_from_jsonl(None, since_iso=None, metadata=meta)
    assert ok is False


def test_short_circuit_when_metadata_already_populated(tmp_path, patch_projects_dir):
    """If cost_usd or duration_ms is already set, don't bother reading the JSONL."""
    session_id = "short"
    records = [
        _user_input("q"),
        _assistant_message("a", usage={"input_tokens": 5}, model="m"),
        _result_record(cost=0.99, duration_ms=999, num_turns=99),
    ]
    _write_jsonl(tmp_path, session_id, records)
    meta = ExecutionMetadata(session_id=session_id, cost_usd=0.0001)

    ok = jsonl_recovery._recover_metadata_from_jsonl(session_id, since_iso=None, metadata=meta)
    assert ok is False  # short-circuit
    assert meta.cost_usd == pytest.approx(0.0001)  # unchanged
    assert meta.duration_ms is None  # unchanged


def test_malformed_lines_are_skipped(tmp_path, patch_projects_dir):
    """Concurrent writes can leave a partial tail line. _read_jsonl_records
    drops non-JSON lines; recovery proceeds with the rest."""
    session_id = "partial"
    proj_dir = tmp_path / "projects"
    target = proj_dir / f"{session_id}.jsonl"
    valid = _assistant_message(
        "ok",
        usage={"input_tokens": 10, "output_tokens": 1, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
        model="m",
    )
    with target.open("w", encoding="utf-8") as f:
        f.write(json.dumps({"type": "user", "message": {"content": "q"}}) + "\n")
        f.write(json.dumps(valid) + "\n")
        # Partial / corrupt line:
        f.write('{"type": "assistant", "message": {"unterminated')

    meta = ExecutionMetadata(session_id=session_id)
    ok = jsonl_recovery._recover_metadata_from_jsonl(session_id, since_iso=None, metadata=meta)

    assert ok is True
    assert meta.input_tokens == 10
    assert meta.model_name == "m"


def test_z_and_offset_timestamps_both_parse(tmp_path, patch_projects_dir):
    """Records can carry either ``Z`` or ``+00:00`` timestamp suffixes.
    Both forms must compare correctly against since_iso."""
    session_id = "tz"
    records = [
        _user_input("q", ts="2026-05-11T10:00:00+00:00"),
        _assistant_message(
            "a",
            usage={"input_tokens": 7, "output_tokens": 1, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
            model="m",
            ts="2026-05-11T10:00:01.500Z",
        ),
    ]
    _write_jsonl(tmp_path, session_id, records)
    meta = ExecutionMetadata(session_id=session_id)

    ok = jsonl_recovery._recover_metadata_from_jsonl(
        session_id, since_iso="2026-05-11T09:59:59.000Z", metadata=meta
    )
    assert ok is True
    assert meta.input_tokens == 7


# ---------------------------------------------------------------------------
# _read_jsonl_records — direct
# ---------------------------------------------------------------------------


def test_read_jsonl_records_file_missing(tmp_path, patch_projects_dir):
    records, truncated, err = jsonl_recovery._read_jsonl_records("missing")
    assert records == []
    assert truncated is False
    assert err == "file_missing"


@pytest.mark.parametrize(
    "session_id",
    [
        "../etc/passwd",
        "../../escape",
        "foo/bar",
        "foo\\bar",
        "with space",
        "../",
        ".",
        "..",
        "name.with.dots",
        "name;rm -rf",
        "name\x00null",
    ],
)
def test_read_jsonl_records_rejects_unsafe_session_id(
    tmp_path, patch_projects_dir, session_id
):
    """session_id originates from a trusted subprocess but defense-in-depth
    still requires shape validation before path construction. Anything
    outside ``[A-Za-z0-9_-]`` is rejected upfront — no file read, no
    path resolution, no log of bytes that don't look like a session id."""
    records, truncated, err = jsonl_recovery._read_jsonl_records(session_id)
    assert records == []
    assert truncated is False
    assert err == "invalid_session_id"


def test_read_jsonl_records_rejects_path_outside_projects_dir(
    tmp_path, patch_projects_dir, monkeypatch
):
    """Symlinks on the projects dir itself shouldn't break containment.
    Point ``_JSONL_PROJECTS_DIR`` at a symlink that resolves to an
    unrelated dir, then prove the resolved path containment check still
    fires when something targets a file via the symlinked path."""
    # Build: projects_dir is a symlink → real_dir; resolved root matches
    # the real_dir. Build a session id that, when resolved through the
    # symlinked root, would land *inside* real_dir — that's still fine
    # (it's relative to the resolved root). The real risk we care about
    # is shape-based traversal, which the regex catches first. To prove
    # the containment branch in isolation, monkey-patch
    # _SAFE_SESSION_ID_RE to permissive and use a session_id whose path
    # resolves outside the projects dir via "..".
    import re as _re
    monkeypatch.setattr(
        jsonl_recovery,
        "_SAFE_SESSION_ID_RE",
        _re.compile(r".*"),
    )
    records, truncated, err = jsonl_recovery._read_jsonl_records("../escape")
    assert records == []
    assert truncated is False
    assert err == "path_outside_projects_dir"


def test_read_jsonl_records_truncates_large_file(tmp_path, patch_projects_dir, monkeypatch):
    """Files larger than the 10MB cap must seek to the tail and emit
    ``truncated=True`` so callers can downgrade their confidence."""
    # Shrink the cap for the test so we don't actually need 10MB of disk.
    monkeypatch.setattr(jsonl_recovery, "_MAX_JSONL_BYTES_FOR_RECOVERY", 256)

    session_id = "big"
    proj_dir = tmp_path / "projects"
    target = proj_dir / f"{session_id}.jsonl"
    payload_line = json.dumps({"type": "noise", "filler": "x" * 200}) + "\n"
    tail_record = json.dumps(_assistant_message("tail", usage={"input_tokens": 9}, model="m")) + "\n"
    with target.open("w", encoding="utf-8") as f:
        for _ in range(10):
            f.write(payload_line)
        f.write(tail_record)

    records, truncated, err = jsonl_recovery._read_jsonl_records(session_id)
    assert truncated is True
    assert err is None
    # We should have at least the tail record in there (others got skipped
    # by the seek; the tail is what matters for metadata recovery).
    assert any(r.get("type") == "assistant" and r.get("message", {}).get("model") == "m" for r in records)
