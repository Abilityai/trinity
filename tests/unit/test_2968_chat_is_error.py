"""#2968: an ``is_error`` result on the synchronous chat path is a failure.

#1673 made the headless path (``_finalize_headless_result``) raise 502 when a
Claude Code ``result`` has ``is_error: true`` (``error_type ==
"execution_error"``), after first trying #1870's evidence-gated completed-turn
recovery. The chat path (``claude_code.execute_claude_code`` behind agent-server
``/api/chat``) never got that check, so a clean-exit ``is_error`` result:

- with empty ``result`` text  → 500 "returned empty response", real cause lost
- with non-empty ``result`` text → SUCCESS, the error text as the answer
- with earlier assistant text  → SUCCESS, the partial text as the answer

These tests drive the REAL ``execute_claude_code`` against a fake ``claude``
executable on PATH that prints a scripted stream-json transcript and exits 0,
optionally writing a session JSONL (the on-disk evidence #1870 reads).
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException

from agent_server.services import claude_code, execution_env, jsonl_recovery
from agent_server.state import agent_state

pytestmark = pytest.mark.asyncio

SID = "29680000-0000-4000-8000-000000000001"
DEAD_SID = "29680000-0000-4000-8000-0000000000de"
NOT_FOUND = f"No conversation found with session ID: {DEAD_SID}"
ANSWER = "The completed answer for 2968."

# The stub prints SHIM_STREAM (a JSON list of stream-json lines) and, when
# SHIM_JSONL_RECORDS is set, writes those records (stamped with the current
# time) to <SHIM_PROJECTS_DIR>/<SHIM_JSONL_SID>.jsonl. Always exits 0.
_SHIM = r"""#!__PYTHON__
import json, os, sys
from datetime import datetime, timezone

sys.stdin.read()
with open(os.environ["SHIM_RUN_LOG"], "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")

records = os.environ.get("SHIM_JSONL_RECORDS")
if records:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    path = os.path.join(os.environ["SHIM_PROJECTS_DIR"], os.environ["SHIM_JSONL_SID"] + ".jsonl")
    with open(path, "a") as f:
        for rec in json.loads(records):
            rec["timestamp"] = now
            f.write(json.dumps(rec) + "\n")

for line in json.loads(os.environ["SHIM_STREAM"]):
    sys.stdout.write(json.dumps(line) + "\n")
    sys.stdout.flush()
sys.exit(0)
"""

_SHIM_KEYS = ("SHIM_STREAM", "SHIM_JSONL_RECORDS", "SHIM_JSONL_SID")


@pytest.fixture
def shim(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    projects.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "claude"
    exe.write_text(_SHIM.replace("__PYTHON__", sys.executable))
    exe.chmod(0o755)
    run_log = tmp_path / "runs.log"

    old_path = execution_env.INITIAL_ENV.get("PATH", os.environ.get("PATH", ""))
    monkeypatch.setitem(
        execution_env.INITIAL_ENV, "PATH", str(bin_dir) + os.pathsep + old_path
    )
    monkeypatch.setitem(execution_env.INITIAL_ENV, "HOME", str(home))
    monkeypatch.setitem(execution_env.INITIAL_ENV, "SHIM_PROJECTS_DIR", str(projects))
    monkeypatch.setitem(execution_env.INITIAL_ENV, "SHIM_RUN_LOG", str(run_log))
    for key in _SHIM_KEYS:
        execution_env.INITIAL_ENV.pop(key, None)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TRINITY_CHAT_SESSION_FILE", str(tmp_path / "marker.json"))
    monkeypatch.setattr(jsonl_recovery, "_JSONL_PROJECTS_DIR", str(projects))

    monkeypatch.setattr(agent_state, "claude_code_available", True)
    monkeypatch.setattr(agent_state, "current_model", None)
    monkeypatch.setattr(agent_state, "session_started", False)
    monkeypatch.setattr(agent_state, "session_context_tokens", 0)
    monkeypatch.setattr(agent_state, "session_total_cost", 0.0)
    monkeypatch.setattr(agent_state, "session_total_output_tokens", 0)
    monkeypatch.setattr(agent_state, "conversation_history", [])
    monkeypatch.setattr(
        agent_state, "session_activity", agent_state._create_empty_activity()
    )
    monkeypatch.setattr(agent_state, "tool_outputs", {})
    monkeypatch.setattr(agent_state, "chat_session_id", None)
    monkeypatch.setattr(agent_state, "chat_session_model", None)
    monkeypatch.setattr(agent_state, "chat_session_generation", 0)
    yield {"projects": projects, "run_log": run_log}
    for key in _SHIM_KEYS:
        execution_env.INITIAL_ENV.pop(key, None)


def _script(stream: list, jsonl_records: list | None = None) -> None:
    execution_env.INITIAL_ENV["SHIM_STREAM"] = json.dumps(stream)
    if jsonl_records is not None:
        execution_env.INITIAL_ENV["SHIM_JSONL_RECORDS"] = json.dumps(jsonl_records)
        execution_env.INITIAL_ENV["SHIM_JSONL_SID"] = SID


def _init() -> dict:
    return {"type": "system", "subtype": "init", "session_id": SID}


def _assistant_text(text: str) -> dict:
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def _result(*, is_error: bool, result: str = "", errors: list | None = None) -> dict:
    line = {
        "type": "result",
        "subtype": "error_during_execution" if is_error else "success",
        "is_error": is_error,
        "result": result,
        "session_id": SID,
        "total_cost_usd": 0.0123,
        "duration_ms": 7,
        "num_turns": 1,
    }
    if errors is not None:
        line["errors"] = errors
    return line


_EDE = "[ede_diagnostic] result_type=user last_content_type=n/a stop_reason=null"


async def _chat(prompt: str = "hello"):
    return await claude_code.execute_claude_code(
        prompt, model="claude-sentinel-2968", execution_id=f"exec-{uuid.uuid4().hex[:12]}"
    )


def _message(exc: HTTPException) -> str:
    detail = exc.detail
    return detail["message"] if isinstance(detail, dict) else str(detail)


# --------------------------------------------------------------------------
# The four repro variants from the issue
# --------------------------------------------------------------------------


async def test_empty_result_is_error_raises_502_with_the_real_cause(shim):
    """Variant 1: was 500 "returned empty response" with errors[] lost."""
    _script([_init(), _result(is_error=True, errors=[_EDE, NOT_FOUND])])

    with pytest.raises(HTTPException) as exc:
        await _chat()

    assert exc.value.status_code == 502
    assert NOT_FOUND in _message(exc.value)
    assert "empty response" not in _message(exc.value)
    assert "ede_diagnostic" not in _message(exc.value)


async def test_error_text_in_result_is_not_returned_as_the_answer(shim):
    """Variant 2: was SUCCESS with "API Error: 500 ..." as the assistant answer."""
    api_err = "API Error: 500 Internal server error"
    _script([_init(), _result(is_error=True, result=api_err)])

    with pytest.raises(HTTPException) as exc:
        await _chat()

    assert exc.value.status_code == 502
    assert api_err in _message(exc.value)


async def test_partial_text_then_is_error_is_not_a_success(shim):
    """Variant 3: was SUCCESS with the partial text, no transcript evidence."""
    _script([
        _init(),
        _assistant_text("partial thought before the failure"),
        _result(is_error=True, errors=["mid-turn failure 2968"]),
    ])

    with pytest.raises(HTTPException) as exc:
        await _chat()

    assert exc.value.status_code == 502
    assert "mid-turn failure 2968" in _message(exc.value)


async def test_clean_result_is_still_a_success(shim):
    """Variant 4 (control): is_error false stays a success."""
    _script([_init(), _assistant_text(ANSWER), _result(is_error=False, result=ANSWER)])

    text, _log, metadata, _raw = await _chat()

    assert text == ANSWER
    assert metadata.error_type is None
    assert agent_state.chat_session_id == SID


# --------------------------------------------------------------------------
# #1870 parity: evidence-gated completed-turn recovery runs first
# --------------------------------------------------------------------------


async def test_completed_turn_on_disk_is_recovered_not_failed(shim):
    """The transcript shows the turn FINISHED (stop_reason=end_turn) even though
    the CLI reported error_during_execution: keep the answer, flagged."""
    _script(
        [_init(), _result(is_error=True, errors=[_EDE])],
        jsonl_records=[
            {"type": "user", "message": {"role": "user", "content": "hello"}},
            {"type": "assistant", "message": {
                "id": "msg_2968", "role": "assistant", "stop_reason": "end_turn",
                "content": [{"type": "text", "text": ANSWER}]}},
            {"type": "user", "message": {"role": "user", "content":
                "<task-notification>\n<status>completed</status>\n</task-notification>"}},
        ],
    )

    text, _log, metadata, _raw = await _chat()

    assert ANSWER in text
    assert "Recovered transcript" in text
    assert metadata.recovered_terminal is True
    assert metadata.recovered_from_jsonl is True


async def test_unfinished_turn_on_disk_is_not_recovered(shim):
    """Evidence gate: a transcript whose last assistant record did not reach
    end_turn is no evidence — the turn still fails."""
    _script(
        [_init(), _assistant_text("half"), _result(is_error=True, errors=["boom 2968"])],
        jsonl_records=[
            {"type": "user", "message": {"role": "user", "content": "hello"}},
            {"type": "assistant", "message": {
                "id": "msg_2968", "role": "assistant", "stop_reason": "tool_use",
                "content": [{"type": "text", "text": "half"}]}},
        ],
    )

    with pytest.raises(HTTPException) as exc:
        await _chat()

    assert exc.value.status_code == 502
    assert "boom 2968" in _message(exc.value)


# --------------------------------------------------------------------------
# Failure side effects
# --------------------------------------------------------------------------


async def test_failed_turn_does_not_capture_its_session(shim):
    """A failed turn never becomes the chat's session to resume (#2958 F1)."""
    _script([_init(), _result(is_error=True, result="API Error: 500")])

    with pytest.raises(HTTPException):
        await _chat()

    assert agent_state.chat_session_id is None


async def test_502_body_carries_telemetry_for_salvage(shim):
    """The structured body lets the backend salvage cost onto the FAILED row
    (`_parse_agent_http_error` reads detail.message + detail.metadata)."""
    _script([_init(), _result(is_error=True, errors=["boom 2968"])])

    with pytest.raises(HTTPException) as exc:
        await _chat()

    detail = exc.value.detail
    assert isinstance(detail, dict)
    assert detail["message"] == "Execution error: boom 2968"
    assert detail["metadata"]["cost_usd"] == pytest.approx(0.0123)
    assert detail["metadata"]["session_id"] == SID


async def test_502_body_is_sanitized(shim):
    """A credential the CLI echoes into its error text is redacted in BOTH the
    user-facing message and the metadata the backend persists."""
    secret = "sk-ant-api03-" + "A" * 40
    _script([_init(), _result(is_error=True, errors=[f"boom with key {secret}"])])

    with pytest.raises(HTTPException) as exc:
        await _chat()

    assert secret not in json.dumps(exc.value.detail)
    assert "boom with key" in _message(exc.value)


async def test_completed_turn_recovery_runs_off_the_event_loop(shim, monkeypatch):
    """The recovery parses the chat's JSONL, which grows for the life of the
    session (read capped at 10 MB) — the same reason the compact-events read
    is already off-loaded. It must not block the agent server's event loop."""
    import threading

    seen = {}
    real = claude_code._recover_completed_turn_into

    def spy(**kwargs):
        seen["thread"] = threading.current_thread()
        return real(**kwargs)

    monkeypatch.setattr(claude_code, "_recover_completed_turn_into", spy)
    _script([_init(), _result(is_error=True, errors=["boom 2968"])])

    with pytest.raises(HTTPException):
        await _chat()

    assert seen["thread"] is not threading.main_thread()
