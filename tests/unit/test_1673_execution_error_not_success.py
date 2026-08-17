"""
Issue #1673 — agent-server masks `error_during_execution` as a successful
`context: fork` placeholder.

Claude Code can emit a `result` line with ``is_error: true`` and an EMPTY
``result`` text — the canonical case being ``--resume`` against a session
whose JSONL no longer exists:

    {"type": "result", "subtype": "error_during_execution", "is_error": true,
     "num_turns": 0, "total_cost_usd": 0,
     "errors": ["No conversation found with session ID: <uuid>"]}

Two cooperating defects made that a green success:

1. `stream_parser` read the message from ``msg["result"]`` (empty here) and
   never fell back to ``msg["errors"]``, so ``metadata.error_message`` was "".
2. `_finalize_headless_result` dispatched on ``error_type`` for rate_limit /
   max_turns / authentication_failed but never ``execution_error``. With
   return_code == 0 and cost_usd present, control fell into the #160
   `context: fork` placeholder branch → HTTP 200, status=success, error=null.

Consequence (the actual damage): the Sessions-tab resume fallback in
`routers/sessions.py` only fires on ``result.status != "success"``, so the
stale cached Claude UUID was never cleared — and step 6 re-cached it from the
echoed ``session_id``. The session was permanently wedged: every turn
"succeeded" in ~2s at $0 with placeholder text, and nothing surfaced.

Modules under test:
    docker/base-image/agent_server/services/stream_parser.py::process_stream_line
    docker/base-image/agent_server/services/headless_executor.py::_finalize_headless_result
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_AGENT_SERVER_DIR = _PROJECT_ROOT / "docker" / "base-image" / "agent_server"

# tests/unit/conftest.py:_preload_real_agent_server() registers
# docker/base-image/agent_server as a namespace package in sys.modules.
from agent_server.models import ExecutionMetadata  # noqa: E402
from agent_server.services import jsonl_recovery as _jsonl_module  # noqa: E402
from agent_server.services.headless_executor import (  # noqa: E402
    HeadlessRunContext,
    _finalize_headless_result,
)

_STALE_UUID = "2f1c9a44-0d3e-4c7a-9b21-1f0e5d8c7a63"
_CLAUDE_MSG = f"No conversation found with session ID: {_STALE_UUID}"

_TASK_START = "2026-07-17T00:00:00Z"


@pytest.fixture(autouse=True)
def _empty_jsonl_projects_dir(tmp_path, monkeypatch):
    """Make "no JSONL on disk" an ASSERTED precondition, not an accident.

    #1870 added a recovery step inside the `execution_error` branch that
    consults the on-disk transcript. These tests only stay meaningful while
    `_STALE_UUID` resolves to nothing — which today is true merely because
    `/home/developer/.claude/...` does not exist on a test host. No CI job
    runs tests/unit inside a container, so the premise holds, but it was
    implicit. Pin it.
    """
    target = tmp_path / "projects" / "-home-developer"
    target.mkdir(parents=True)
    monkeypatch.setattr(_jsonl_module, "_JSONL_PROJECTS_DIR", str(target))
    return target


def _write_jsonl(jsonl_dir: Path, session_id: str, records: list) -> Path:
    import json

    p = jsonl_dir / f"{session_id}.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return p


def _make_ctx(
    *,
    return_code: int = 0,
    cost_usd=0.0,
    duration_ms=0,
    response_parts=None,
    raw_messages=None,
    error_type=None,
    error_message=None,
    num_turns=0,
) -> HeadlessRunContext:
    """HeadlessRunContext in the post-subprocess state (mirrors the #160 test)."""
    ctx = HeadlessRunContext(
        cmd=["claude", "--print", "--resume", _STALE_UUID],
        task_session_id="task-1673",
        task_start_iso=_TASK_START,
        effective_timeout=900,
        images=None,
        prompt="dummy",
    )
    ctx.return_code = return_code
    ctx.metadata = ExecutionMetadata(
        cost_usd=cost_usd,
        duration_ms=duration_ms,
        tool_count=0,
        num_turns=num_turns,
    )
    ctx.metadata.session_id = _STALE_UUID
    ctx.metadata.error_type = error_type
    ctx.metadata.error_message = error_message
    ctx.response_parts = list(response_parts or [])
    ctx.raw_messages = list(raw_messages or [])
    return ctx


# ---------------------------------------------------------------------------
# Defect 1 — stream_parser must read errors[] when result text is empty.
# ---------------------------------------------------------------------------


def _parse_result(msg):
    """Drive the parser's `result` branch, returning populated metadata."""
    import json

    from agent_server.services.stream_parser import process_stream_line

    metadata = ExecutionMetadata()
    process_stream_line(json.dumps(msg), [], metadata, {}, [])
    return metadata


def test_parser_reads_error_text_from_errors_array():
    """`error_during_execution` carries its text in errors[], not result."""
    metadata = _parse_result(
        {
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "num_turns": 0,
            "total_cost_usd": 0,
            "result": "",  # empty — the whole trap
            "errors": [_CLAUDE_MSG],
        }
    )

    assert metadata.error_type == "execution_error"
    # Pre-fix this was "" — which made the failure unattributable and, worse,
    # unmatched by the backend's resume-not-found marker.
    assert metadata.error_message == _CLAUDE_MSG


def test_parser_prefers_result_text_when_present():
    """Regression guard: a populated result text still wins over errors[]."""
    metadata = _parse_result(
        {
            "type": "result",
            "is_error": True,
            "result": "explicit failure text",
            "errors": ["secondary"],
        }
    )

    assert metadata.error_type == "execution_error"
    assert metadata.error_message == "explicit failure text"


def test_parser_falls_back_to_placeholder_when_both_empty():
    """No result text and no errors[] must still yield a non-empty message."""
    metadata = _parse_result({"type": "result", "is_error": True, "result": ""})

    assert metadata.error_type == "execution_error"
    assert metadata.error_message  # never empty


# ---------------------------------------------------------------------------
# Defect 2 — finalize must surface execution_error, never the placeholder.
# ---------------------------------------------------------------------------


def test_execution_error_raises_502_not_placeholder_success():
    """THE bug: clean exit + is_error + empty parts must NOT return 200.

    Pre-fix this returned the `context: fork` placeholder text and the
    execution was stored status=success / error=null.
    """
    ctx = _make_ctx(
        cost_usd=0.0,  # present (not None) — this is why #160's branch caught it
        duration_ms=0,
        response_parts=[],  # no assistant text reached the parent stream
        error_type="execution_error",
        error_message=_CLAUDE_MSG,
    )

    with pytest.raises(HTTPException) as exc_info:
        _finalize_headless_result(ctx)

    assert exc_info.value.status_code == 502


def test_502_detail_carries_the_resume_marker_backend_matches_on():
    """The detail must contain Claude's verbatim text.

    `routers/sessions.py::_is_resume_not_found` substring-matches "no
    conversation found" on `result.error`, which the backend derives from
    ``detail["message"]`` via ``_extract_agent_error``. If the message is
    dropped, the fallback never fires and the session stays wedged — so this
    assertion is the contract between the two layers, not a cosmetic check.

    #1853 changed the ``execution_error`` 502 body from a plain string to a
    STRUCTURED body (message + metadata + execution_log). The resume marker now
    lives in ``detail["message"]``, carried verbatim so the self-heal is
    preserved (#1938).
    """
    ctx = _make_ctx(
        response_parts=[],
        error_type="execution_error",
        error_message=_CLAUDE_MSG,
    )

    with pytest.raises(HTTPException) as exc_info:
        _finalize_headless_result(ctx)

    detail = exc_info.value.detail
    assert isinstance(
        detail, dict
    ), "execution_error detail is a structured body (#1853)"
    assert "no conversation found" in detail["message"].lower()


def test_execution_error_body_does_not_trip_reader_race():
    """Must not collide with the #678 auto-retry.

    #1853 changed the ``execution_error`` 502 body from a string to a structured
    dict, so "not a dict" is no longer the discriminator. The real contract is
    that ``task_execution_service._is_reader_race_signature`` gates on
    ``recovery_attempted`` (absent from the #1853 body), so the structured body
    must still NOT trigger the 502 auto-retry path.
    """
    ctx = _make_ctx(
        response_parts=[],
        error_type="execution_error",
        error_message=_CLAUDE_MSG,
    )

    with pytest.raises(HTTPException) as exc_info:
        _finalize_headless_result(ctx)

    # conftest.py makes `services.*` importable (it imports services.agent_client).
    from services.task_execution_service import _is_reader_race_signature

    detail = exc_info.value.detail
    assert isinstance(detail, dict)  # #1853: now a structured body
    assert _is_reader_race_signature(detail) is False


def test_execution_error_fires_even_when_assistant_text_exists():
    """An is_error result is authoritative regardless of accumulated text."""
    ctx = _make_ctx(
        response_parts=["partial output"],
        error_type="execution_error",
        error_message=_CLAUDE_MSG,
    )

    with pytest.raises(HTTPException) as exc_info:
        _finalize_headless_result(ctx)

    assert exc_info.value.status_code == 502


def test_execution_error_message_is_sanitized():
    """Credential sanitization applies to the surfaced detail.

    The dummy value is deliberately NOT key-shaped: this repo is public and a
    realistic prefix would trip secret scanners. The sanitizer keys off the
    variable NAME, so this still exercises the real redaction path.
    """
    ctx = _make_ctx(
        response_parts=[],
        error_type="execution_error",
        error_message="failed with ANTHROPIC_API_KEY=dummy-not-a-real-key-999",
    )

    with pytest.raises(HTTPException) as exc_info:
        _finalize_headless_result(ctx)

    assert "dummy-not-a-real-key-999" not in str(exc_info.value.detail)


# ---------------------------------------------------------------------------
# #1870 added a recovery step in this branch. These pin that it can only ever
# fire on positive on-disk evidence — never on accumulated stdout text.
# ---------------------------------------------------------------------------


def _assistant(text, *, stop, ts, msg_id="msg_x"):
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "id": msg_id,
            "stop_reason": stop,
            "content": [{"type": "text", "text": text}],
        },
        "timestamp": ts,
    }


def _user(text, ts):
    return {
        "type": "user",
        "message": {"role": "user", "content": text},
        "timestamp": ts,
    }


def test_jsonl_present_but_no_end_turn_still_502(_empty_jsonl_projects_dir):
    """A transcript that shows the turn was interrupted is not evidence of
    completion. #1870 recovery must decline and the 502 must stand."""
    _write_jsonl(
        _empty_jsonl_projects_dir,
        _STALE_UUID,
        [
            _user("do the thing", "2026-07-17T00:00:10Z"),
            _assistant("working on it", stop="tool_use", ts="2026-07-17T00:00:20Z"),
        ],
    )
    ctx = _make_ctx(
        response_parts=[],
        error_type="execution_error",
        error_message=_CLAUDE_MSG,
    )

    with pytest.raises(HTTPException) as exc_info:
        _finalize_headless_result(ctx)

    assert exc_info.value.status_code == 502


def test_stale_end_turn_plus_partial_stdout_still_502(_empty_jsonl_projects_dir):
    """THE shape that would re-open #1673.

    A PRIOR turn's `end_turn` sits in the JSONL (a `--resume` session) AND
    partial stdout text sits in `response_parts`. Recovering here would be a
    silent wrong answer — the previous turn's reply reported as this turn's
    success — and would prove the gate had drifted onto accumulated stdout.

    Exercises the staleness guard and the never-recover-from-stdout invariant
    in one test.
    """
    _write_jsonl(
        _empty_jsonl_projects_dir,
        _STALE_UUID,
        [
            _user("an EARLIER question", "2026-07-16T09:00:00Z"),
            _assistant("PRIOR TURN ANSWER", stop="end_turn", ts="2026-07-16T09:00:05Z"),
        ],
    )
    ctx = _make_ctx(
        response_parts=["partial output"],
        error_type="execution_error",
        error_message=_CLAUDE_MSG,
    )

    with pytest.raises(HTTPException) as exc_info:
        _finalize_headless_result(ctx)

    assert exc_info.value.status_code == 502
    assert "PRIOR TURN ANSWER" not in str(exc_info.value.detail)


@pytest.mark.parametrize(
    "error_type,expected_status",
    [
        ("rate_limit", 429),
        ("max_turns", 422),
        ("authentication_failed", 503),
    ],
)
def test_recovery_is_not_attempted_for_other_error_types(
    _empty_jsonl_projects_dir, error_type, expected_status
):
    """#1870 recovery is scoped to `execution_error` ONLY.

    Those branches short-circuit above it, so even a perfectly recoverable
    transcript must not turn a rate-limit / max-turns / auth failure into a
    200. Nothing pinned that before.
    """
    _write_jsonl(
        _empty_jsonl_projects_dir,
        _STALE_UUID,
        [
            _user("do the thing", "2026-07-17T00:00:10Z"),
            _assistant("a complete answer", stop="end_turn", ts="2026-07-17T00:00:20Z"),
        ],
    )
    ctx = _make_ctx(
        response_parts=[],
        error_type=error_type,
        error_message="some failure",
    )

    with pytest.raises(HTTPException) as exc_info:
        _finalize_headless_result(ctx)

    assert exc_info.value.status_code == expected_status


# ---------------------------------------------------------------------------
# The #160 placeholder must still work for genuine clean exits.
# ---------------------------------------------------------------------------


def test_clean_fork_exit_without_error_still_returns_placeholder():
    """#160 regression guard: no error_type ⇒ placeholder still fires.

    This is the case the placeholder branch exists for; the #1673 fix must
    narrow it to non-error results only, not remove it.
    """
    ctx = _make_ctx(
        cost_usd=0.0123,
        duration_ms=4200,
        response_parts=[],
        error_type=None,  # the distinction
        raw_messages=[{"type": "result", "cost_usd": 0.0123}],
    )

    response_text, _, metadata, _ = _finalize_headless_result(ctx)

    assert response_text
    assert "no direct output" in response_text.lower()
    assert metadata.cost_usd == 0.0123


# ---------------------------------------------------------------------------
# Regression-pin: source-level signature.
# ---------------------------------------------------------------------------


def test_finalize_source_contains_execution_error_branch():
    """If the branch is deleted, the bug silently returns."""
    src = (_AGENT_SERVER_DIR / "services" / "headless_executor.py").read_text()
    assert "#1673" in src
    assert 'error_type == "execution_error"' in src


def test_parser_source_reads_errors_array():
    src = (_AGENT_SERVER_DIR / "services" / "stream_parser.py").read_text()
    assert "#1673" in src
