"""
Issue #1849 — Claude Code's internal ``[ede_diagnostic]`` header must never be
surfaced as the error cause.

#1673 made ``error_during_execution`` fall back to ``msg["errors"]`` when
``msg["result"]`` is empty, reading ``errors[0]``. On current Claude Code
(observed 2.1.215) ``errors[0]`` is *never* a real error — the CLI prepends its
own internal diagnostic header and then filters exactly those entries out of its
own user-facing output::

    errors: [
      `[ede_diagnostic] result_type=${…} last_content_type=${…} stop_reason=${…}`,
      ...realErrors                      // ← the actual causes
    ]

    // the CLI's own consumer:
    let t = e.errors.filter((r) => !r.startsWith("[ede_diagnostic]"));

Three consequences, all reproduced:

1. Every failure on this path was labelled with a meaningless marker.
2. Real errors at ``errors[1..]`` were dropped entirely.
3. The backend's resume-not-found self-healing
   (``routers/sessions.py::_is_resume_not_found``, a substring match on the
   502 detail) stopped matching — re-wedging exactly the sessions #1673 was
   filed to unwedge. That contract is the acceptance criterion of this file
   (``test_resume_fallback_fires_through_the_502_detail``), per
   ``docs/memory/learnings.md`` #1521: assert at the consumer, not at the
   resolver.

Four latent defects in the same two expressions are fixed and pinned here too:

* a malformed ``errors`` shape (a dict, an int, a nested list) made
  ``errors[0]`` **raise** inside the parser; the raise is swallowed by the
  per-line ``except Exception`` in ``headless_executor`` and the execution fell
  through to the #160 ``context: fork`` placeholder — HTTP 200, status=success.
  That is #1673's own bug in a new shape;
* a bare-string ``errors`` was indexed character-by-character (``"boom"`` →
  ``'b'``);
* a blank/None entry could join to ``""``, re-introducing the empty
  ``error_message`` #1673 fixed;
* a leading newline slipped a marker past a ``.startswith()`` filter.

Modules under test:
    docker/base-image/agent_server/services/stream_parser.py::process_stream_line
Consumers asserted against (unchanged, acceptance criteria):
    docker/base-image/agent_server/services/headless_executor.py::_finalize_headless_result
    src/backend/routers/sessions.py::_is_resume_not_found
"""
from __future__ import annotations

import importlib.util
import json
import logging
import types
from pathlib import Path

import pytest
from fastapi import HTTPException

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_AGENT_SERVER_DIR = _PROJECT_ROOT / "docker" / "base-image" / "agent_server"

# tests/unit/conftest.py:_preload_real_agent_server() registers
# docker/base-image/agent_server as a namespace package in sys.modules.
from agent_server.models import ExecutionMetadata  # noqa: E402
from agent_server.services.headless_executor import (  # noqa: E402
    HeadlessRunContext,
    _finalize_headless_result,
)

# The exact header observed in production (four executions on an ops-managed
# instance recorded this verbatim as their stored `error`).
_MARKER = "[ede_diagnostic] result_type=user last_content_type=n/a stop_reason=null"
_MARKER_PAYLOAD = "result_type=user last_content_type=n/a stop_reason=null"

_STALE_UUID = "2f1c9a44-0d3e-4c7a-9b21-1f0e5d8c7a63"
_CLAUDE_MSG = f"No conversation found with session ID: {_STALE_UUID}"

# A real hex UUID contains the bare substring "401" or "403" ~1.09% of the time
# (measured over 200k UUIDs). AUTH_INDICATORS matches those bare — see R1 /
# test_resume_fallback_survives_a_403_bearing_uuid.
_403_UUID = "403e1c9a-0d3e-4c7a-9b21-1f0e5d8c7a63"
_CLAUDE_MSG_403 = f"No conversation found with session ID: {_403_UUID}"


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _parse_result(msg) -> ExecutionMetadata:
    """Drive the parser's `result` branch, returning populated metadata.

    ``process_stream_line`` is imported INSIDE the function on purpose: two
    sibling agent_server test files evict + reload the package at collection,
    so an import-time capture and a call-time resolution can be different
    module generations (docs/memory/learnings.md, module-reload trap). Same
    shape as tests/unit/test_1673_execution_error_not_success.py.
    """
    from agent_server.services.stream_parser import process_stream_line

    metadata = ExecutionMetadata()
    process_stream_line(json.dumps(msg), [], metadata, {}, [])
    return metadata


def _execution_error(errors, *, result: str = "") -> ExecutionMetadata:
    return _parse_result(
        {
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "num_turns": 0,
            "total_cost_usd": 0,
            "result": result,
            "errors": errors,
        }
    )


def _max_turns(errors, *, num_turns: int = 5) -> ExecutionMetadata:
    return _parse_result(
        {
            "type": "result",
            "subtype": "error_max_turns",
            "is_error": True,
            "num_turns": num_turns,
            "result": "",
            "errors": errors,
        }
    )


def _make_ctx(*, error_message: str, error_type: str = "execution_error") -> HeadlessRunContext:
    """HeadlessRunContext in the post-subprocess state (mirrors the #1673 test)."""
    ctx = HeadlessRunContext(
        cmd=["claude", "--print", "--resume", _STALE_UUID],
        task_session_id="task-1849",
        task_start_iso="2026-08-01T00:00:00Z",
        effective_timeout=900,
        images=None,
        prompt="dummy",
    )
    ctx.return_code = 0
    ctx.metadata = ExecutionMetadata(cost_usd=0.0, duration_ms=0, tool_count=0, num_turns=0)
    ctx.metadata.session_id = _STALE_UUID
    ctx.metadata.error_type = error_type
    ctx.metadata.error_message = error_message
    ctx.response_parts = []
    ctx.raw_messages = []
    return ctx


def _detail_for(errors) -> str:
    """Full cross-surface drive: parser → _finalize_headless_result → 502 detail."""
    metadata = _execution_error(errors)
    ctx = _make_ctx(error_type=metadata.error_type, error_message=metadata.error_message)
    with pytest.raises(HTTPException) as exc_info:
        _finalize_headless_result(ctx)
    assert exc_info.value.status_code == 502
    detail = exc_info.value.detail
    assert isinstance(detail, str), "execution_error detail is a plain string"
    return detail


_SESSIONS_CACHE: dict = {}


def _load_sessions_router() -> types.ModuleType:
    """Load src/backend/routers/sessions.py in isolation.

    NOT ``from routers.sessions import _is_resume_not_found``: that executes
    routers/__init__.py, which eagerly imports 50+ routers including
    routers/agents.py → a broad ``from services.agent_service import (...)``.
    Sibling unit tests install a stub ``services.agent_service`` in sys.modules
    at COLLECTION time (so an in-function import does not help), and under CI's
    ``pytest -p randomly --randomly-seed=…`` that broad import raises
    ImportError — the documented #1187 regression-diff failure. Exec'ing
    sessions.py in isolation sidesteps the pollution without mutating
    sys.modules. Verbatim from tests/unit/test_session_tab_gate_codex.py.
    """
    if "mod" in _SESSIONS_CACHE:
        return _SESSIONS_CACHE["mod"]
    for base in (
        _PROJECT_ROOT / "src" / "backend",  # host / CI
        Path("/app"),  # trinity-backend container
    ):
        path = base / "routers" / "sessions.py"
        if path.exists():
            spec = importlib.util.spec_from_file_location(
                "routers_sessions_under_test_1849", str(path)
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)  # type: ignore[union-attr]
            _SESSIONS_CACHE["mod"] = module
            return module
    raise RuntimeError("Cannot locate routers/sessions.py")


# ---------------------------------------------------------------------------
# The core defect — the marker must never win over a real error.
# ---------------------------------------------------------------------------


def test_marker_first_real_error_wins():
    """THE bug: errors[0] is the CLI's own header; the cause is at errors[1..]."""
    metadata = _execution_error([_MARKER, _CLAUDE_MSG])

    assert metadata.error_type == "execution_error"
    assert metadata.error_message == _CLAUDE_MSG
    assert "ede_diagnostic" not in metadata.error_message


def test_all_diagnostics_keeps_payload_but_strips_token():
    """The modal production case: `errors` is marker-only.

    The diagnostic's key=value payload is the ONLY technical context this
    failure produces (cost, tool_calls, response and claude_session_id are all
    empty on this path), so it is kept as clearly-labelled context rather than
    dropped. The `[ede_diagnostic]` TOKEN is stripped: it exists nowhere in
    this codebase, so it is unsearchable and misdirects investigators (#1849),
    and it must never masquerade as the cause.
    """
    metadata = _execution_error([_MARKER])

    assert metadata.error_message
    assert "ede_diagnostic" not in metadata.error_message
    assert "stop_reason=null" in metadata.error_message
    assert "diagnostic:" in metadata.error_message


def test_discarded_diagnostic_appears_in_warning_log(caplog):
    """The diagnostic stays observable at BOTH call sites.

    Not DEBUG (which the issue suggested): agent_server/main.py pins
    logging.basicConfig(level=logging.INFO), so a DEBUG record emits nothing.
    """
    with caplog.at_level(logging.WARNING, logger="agent_server.services.stream_parser"):
        _execution_error([_MARKER])
    assert any("stop_reason=null" in r.getMessage() for r in caplog.records), caplog.text

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="agent_server.services.stream_parser"):
        _max_turns([_MARKER])
    assert any("stop_reason=null" in r.getMessage() for r in caplog.records), caplog.text


def test_multiple_real_errors_are_joined():
    """Joining (not errors[0]) preserves a multi-error result. Pins ", "."""
    metadata = _execution_error([_MARKER, "first cause", "second cause"])

    assert metadata.error_message == "first cause, second cause"


def test_result_text_still_wins_over_errors():
    """#1673 precedence guard: a populated result text beats errors[]."""
    metadata = _execution_error([_MARKER, _CLAUDE_MSG], result="explicit failure text")

    assert metadata.error_message == "explicit failure text"


# ---------------------------------------------------------------------------
# Shape defects in the same expressions.
# ---------------------------------------------------------------------------


def test_bare_string_errors_is_one_error_not_characters():
    """A bare string is ONE error. `errors[0]` on "boom" returned 'b'."""
    metadata = _execution_error("boom")

    assert metadata.error_message == "boom"


def test_blank_and_none_entries_never_yield_empty_message():
    """A join over blank/None entries must never produce "" or "None".

    An empty error_message is exactly the bug #1673 fixed; a stringified None
    is a lie about what the CLI reported.
    """
    metadata = _execution_error([_MARKER, "   ", None])

    assert metadata.error_message
    assert "None" not in metadata.error_message
    assert "ede_diagnostic" not in metadata.error_message


def test_leading_whitespace_cannot_smuggle_the_marker():
    """A leading newline must not bypass the prefix test (filter bypass)."""
    metadata = _execution_error(["\n  " + _MARKER, _CLAUDE_MSG])

    assert metadata.error_message == _CLAUDE_MSG


def test_uppercase_marker_is_still_filtered():
    """Deliberately more lenient than the CLI's case-sensitive filter.

    No legitimate cause begins with this token in any case, so leniency can
    only ever prevent a marker leaking.
    """
    metadata = _execution_error(["[EDE_DIAGNOSTIC] result_type=user", _CLAUDE_MSG])

    assert metadata.error_message == _CLAUDE_MSG


def test_dict_shaped_entries_are_unwrapped_not_reprd():
    """Forward-compat: a nested marker must not str() into a Python repr.

    If a future CLI emits {"type": "ede_diagnostic", ...}, a naive str(entry)
    no longer starts with "[", the filter silently no-ops, and #1849 reopens
    with no signal.
    """
    metadata = _execution_error([{"text": _MARKER}, {"message": _CLAUDE_MSG}])

    assert metadata.error_message == _CLAUDE_MSG
    assert "{'" not in metadata.error_message


@pytest.mark.parametrize(
    "errors",
    [
        pytest.param({"message": "boom"}, id="dict"),
        pytest.param(5, id="int"),
        pytest.param(None, id="explicit-null"),
        pytest.param([["nested"]], id="nested-list"),
        pytest.param(True, id="bool"),
    ],
)
def test_malformed_errors_shapes_never_raise_and_never_succeed(errors):
    """A malformed `errors` shape must not raise inside the parser.

    THE HTTP-200 class (#1673's own bug in a new shape): `errors[0]` on a
    non-sequence raises; the raise is swallowed by headless_executor's per-line
    `except Exception` (which literally anticipates "process_stream_line
    tripping on weird input"). error_type/error_message are then never set,
    while cost_usd/duration_ms already are — so `_classify_empty_result`
    returns None and `_finalize_headless_result` falls into the #160
    `context: fork` placeholder: HTTP 200, status=success, error=null.
    """
    metadata = _execution_error(errors)  # must not raise

    assert metadata.error_type == "execution_error"
    assert metadata.error_message
    assert isinstance(metadata.error_message, str)
    assert "ede_diagnostic" not in metadata.error_message


# ---------------------------------------------------------------------------
# The second errors[0] site — max_turns (#361), same array, same emitter.
# ---------------------------------------------------------------------------


def test_max_turns_ignores_diagnostic_marker():
    """A marker-only errors[] must fall through to the informative default."""
    metadata = _max_turns([_MARKER], num_turns=5)

    assert metadata.error_type == "max_turns"
    assert metadata.error_message == "Task stopped after 5 turns"


def test_max_turns_surfaces_real_errors_after_marker():
    """Both branches join for the same reason: same array, same emitter."""
    metadata = _max_turns([_MARKER, "first cause", "second cause"])

    assert metadata.error_type == "max_turns"
    assert metadata.error_message == "first cause, second cause"


# ---------------------------------------------------------------------------
# Cross-surface contracts (the acceptance criteria).
# ---------------------------------------------------------------------------


def test_no_error_detail_literal_is_inert_downstream():
    """The fallback wording is load-bearing — pin that it trips no classifier.

    "Claude Code reported no error detail" must match none of the three
    substring classifiers this string flows into, or an inert placeholder
    would start rotating subscriptions / clearing session caches / claiming a
    rate limit.
    """
    from services.failure_classifier import is_auth_failure

    from agent_server.services.error_classifier import _is_rate_limit_message
    from agent_server.services.stream_parser import _NO_ERROR_DETAIL

    sessions = _load_sessions_router()

    candidates = [
        _NO_ERROR_DETAIL,
        _execution_error([]).error_message,
        _execution_error([_MARKER]).error_message,
    ]
    for text in candidates:
        assert text, "the fallback must never be empty (#1673)"
        assert not is_auth_failure(text), text
        assert not sessions._is_resume_not_found(text), text
        assert not _is_rate_limit_message(text), text


def test_resume_fallback_fires_through_the_502_detail():
    """#1849 acceptance: a marker-first errors[] must still reach the backend's
    resume-not-found self-healing.

    Pre-fix this is False (reproduced end to end): the cached UUID is never
    cleared by routers/sessions.py step 5, and the session re-wedges into
    exactly the #1673 state.
    """
    sessions = _load_sessions_router()

    detail = _detail_for([_MARKER, _CLAUDE_MSG])

    assert sessions._is_resume_not_found(detail), detail  # ← the contract
    assert _CLAUDE_MSG in detail  # payload survived truncation
    assert "ede_diagnostic" not in detail


def test_resume_fallback_survives_a_403_bearing_uuid():
    """R1 proof: the SUB-003 misfire is cost, not incorrectness.

    Real error text on this path now reaches the backend's `is_auth_failure`
    substring classifier, which matches a BARE "403". A hex session UUID
    contains one ~1.09% of the time, so ~1 in 92 resume-not-found failures
    will rotate a healthy subscription and re-run the turn once. This test
    pins that the resume fallback STILL fires in that case — the outcome is
    correct, only a subscription slot is wasted. Word-boundary anchoring of
    AUTH_INDICATORS is a tracked follow-up (out of this fix's scope: the
    classifier is a byte-identical vendored mirror affecting every path).
    """
    from services.failure_classifier import is_auth_failure

    sessions = _load_sessions_router()

    detail = _detail_for([_MARKER, _CLAUDE_MSG_403])

    assert sessions._is_resume_not_found(detail), detail
    # Documents the known misfire rather than asserting it away.
    assert is_auth_failure(detail), "expected the documented bare-403 misfire"


def test_multi_error_truncation_limit_is_known():
    """Known residual (R5): `err[:300]` is applied AFTER the join.

    A long first error can push a later resume marker past the budget. Pinned
    so a future truncation fix (widen, or prioritise the resume-relevant
    entry) flips this deliberately rather than by accident.
    """
    sessions = _load_sessions_router()

    detail = _detail_for([_MARKER, "A" * 400, _CLAUDE_MSG])

    assert len(detail) <= len("Execution error: ") + 300
    assert not sessions._is_resume_not_found(detail), (
        "truncation residual fixed — update R5 and this pin"
    )


def test_async_callback_maps_502_to_no_auth_error_code():
    """R2: the dispatch circuit breaker cannot be affected by this path.

    Sync: error_code=AUTH is gated strictly on HTTP 503. Async (#1083): the
    result-callback status map gives 502 error_code=None. The breaker counts
    AUTH only.
    """
    from agent_server.services import result_callback

    assert result_callback._STATUS_MAP[502] == (None, "empty_result")


# ---------------------------------------------------------------------------
# Regression-pin: source-level signature.
# ---------------------------------------------------------------------------


def test_parser_source_pins_both_issue_tokens():
    """#1673's comment must be EXTENDED, not replaced.

    tests/unit/test_1673_execution_error_not_success.py::
    test_parser_source_reads_errors_array asserts only `"#1673" in src`;
    dropping the token would red a correct fix for a non-behavioural reason.
    """
    src = (_AGENT_SERVER_DIR / "services" / "stream_parser.py").read_text()

    assert "#1673" in src
    assert "#1849" in src
    assert "ede_diagnostic" in src
