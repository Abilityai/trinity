"""#2958: a `/api/chat` turn resumes only the chat's OWN session.

Before the fix the Claude chat path ran `claude --continue`, which resumes the
newest JSONL in the shared project dir whoever wrote it. Every `/api/task` with
an effective timeout above 600 s persists its JSONL there (#678), so the next
chat turn silently continued a scheduled run's near-full context and paid an
auto-compaction first.

These tests drive the REAL `execute_claude_code` and `execute_headless_task`
against a fake `claude` executable that emulates the session flags faithfully:

- `--session-id X` writes `X.jsonl`; no session flag mints a fresh UUID file
- `--no-session-persistence` writes nothing
- `--resume X` appends to `X.jsonl`; a missing file gives the real CLI's
  shape (exit 0 + an `is_error` `error_during_execution` result, #1673), or
  exit 1 + stderr with `SHIM_RESUME_FAIL_MODE=stderr`
- `--continue` appends to the newest `*.jsonl` by mtime (mtimes are pinned)

A JSONL seeded `LARGE` "compacts" when resumed: stdout gets a bare
`compact_boundary` (no numbers, as the real CLI) and the JSONL gets the record
with `compactMetadata`. Only the file a test seeds is ever `LARGE`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException

from agent_server.services import claude_code, execution_env, jsonl_recovery
from agent_server.services.headless_executor import execute_headless_task
from agent_server.services.process_registry import get_process_registry
from agent_server.state import agent_state

pytestmark = pytest.mark.asyncio

M1 = "claude-sentinel-m1-2958"
M2 = "claude-sentinel-m2-2958"
TASK_MODEL = "claude-sentinel-task-2958"

PRE, POST, DUR = 173771, 5600, 162585

_SHIM = r"""#!__PYTHON__
import json, os, sys, time, uuid
from datetime import datetime, timezone

args = sys.argv[1:]


def opt(name):
    return args[args.index(name) + 1] if name in args else None


proj = os.environ["SHIM_PROJECTS_DIR"]
prompt = sys.stdin.read()
with open(os.environ["SHIM_RUN_LOG"], "a") as f:
    f.write(json.dumps(args) + "\n")

gate = os.environ.get("SHIM_GATE_FILE")
if gate:
    open(gate + ".ready", "w").close()
    for _ in range(2000):
        if os.path.exists(gate):
            break
        time.sleep(0.01)


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def path(sid):
    return os.path.join(proj, sid + ".jsonl")


def bump(p):
    # Deterministic, strictly increasing mtimes: --continue must not depend
    # on filesystem timestamp resolution.
    ctr = os.path.join(proj, ".mtime-counter")
    n = (int(open(ctr).read()) if os.path.exists(ctr) else 0) + 1
    with open(ctr, "w") as f:
        f.write(str(n))
    t = 1_900_000_000 + n
    os.utime(p, (t, t))


def tool_use():
    emit({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "toolu_2958", "name": "Bash", "input": {"command": "true"}}]}})


def resume_failure(sid):
    if os.environ.get("SHIM_TOOL_USE") == "1":
        tool_use()
    if os.environ.get("SHIM_RESUME_FAIL_MODE") == "stderr":
        sys.stderr.write("No conversation found with session ID: %s\n" % sid)
        sys.exit(1)
    emit({"type": "result", "subtype": "error_during_execution", "is_error": True,
          "num_turns": 0, "result": "", "session_id": str(uuid.uuid4()),
          "total_cost_usd": 0, "duration_ms": 1,
          "errors": ["[ede_diagnostic] result_type=user last_content_type=n/a stop_reason=null",
                     "No conversation found with session ID: %s" % sid]})
    sys.exit(0)


persist = "--no-session-persistence" not in args
resume = opt("--resume")
if resume is not None:
    if not os.path.exists(path(resume)) or os.environ.get("SHIM_RESUME_FAIL_ALWAYS") == "1":
        resume_failure(resume)
    sid = resume
    if os.environ.get("SHIM_ERROR_AFTER_TEXT") == "1":
        emit({"type": "system", "subtype": "init", "session_id": sid})
        emit({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "partial answer from " + sid}]}})
        emit({"type": "result", "subtype": "error_during_execution", "is_error": True,
              "num_turns": 1, "result": "", "session_id": sid, "total_cost_usd": 0,
              "duration_ms": 1, "errors": ["mid-turn failure 2958"]})
        sys.exit(0)
elif "--continue" in args:
    files = [f for f in os.listdir(proj) if f.endswith(".jsonl")]
    if files:
        sid = max(files, key=lambda f: os.path.getmtime(os.path.join(proj, f)))[:-6]
    else:
        sid = str(uuid.uuid4())
else:
    sid = opt("--session-id") or str(uuid.uuid4())

p = path(sid)
large = os.path.exists(p) and "LARGE" in open(p).read()
emit({"type": "system", "subtype": "init", "session_id": sid, "permissionMode": "bypassPermissions"})
if large:
    emit({"type": "system", "subtype": "compact_boundary", "session_id": sid, "timestamp": now_iso()})
text = "reply from " + sid
emit({"type": "assistant", "message": {"model": opt("--model") or "", "content": [
    {"type": "text", "text": text}], "usage": {"input_tokens": 1000, "output_tokens": 10}}})
emit({"type": "result", "subtype": "success", "is_error": False, "result": text,
      "session_id": sid, "total_cost_usd": 0.001, "duration_ms": 5, "num_turns": 1})
if persist:
    with open(p, "a") as f:
        f.write(json.dumps({"type": "user", "timestamp": now_iso(), "message": prompt[:60]}) + "\n")
        if "SEED_LARGE" in prompt:
            f.write(json.dumps({"type": "marker", "note": "LARGE"}) + "\n")
        if large:
            f.write(json.dumps({"type": "system", "subtype": "compact_boundary", "timestamp": now_iso(),
                                "compactMetadata": {"trigger": "auto", "preTokens": __PRE__,
                                                    "postTokens": __POST__, "durationMs": __DUR__}}) + "\n")
    bump(p)
"""


class Shim:
    def __init__(self, root: Path):
        self.root = root
        self.projects = root / "projects"
        self.run_log = root / "runs.log"
        self.marker = root / "marker" / "chat-session.json"

    def runs(self) -> list[list[str]]:
        if not self.run_log.exists():
            return []
        return [json.loads(line) for line in self.run_log.read_text().splitlines()]

    def jsonl(self, sid: str) -> Path:
        return self.projects / f"{sid}.jsonl"

    def marker_id(self):
        if not self.marker.exists():
            return None
        return json.loads(self.marker.read_text()).get("session_id")


@pytest.fixture
def shim(tmp_path, monkeypatch):
    s = Shim(tmp_path)
    s.projects.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "claude"
    exe.write_text(
        _SHIM.replace("__PYTHON__", sys.executable)
        .replace("__PRE__", str(PRE))
        .replace("__POST__", str(POST))
        .replace("__DUR__", str(DUR))
    )
    exe.chmod(0o755)

    # build_execution_env starts from INITIAL_ENV, not os.environ. PREPEND:
    # the rest of PATH still has to resolve for the spawned process.
    old_path = execution_env.INITIAL_ENV.get("PATH", os.environ.get("PATH", ""))
    monkeypatch.setitem(
        execution_env.INITIAL_ENV, "PATH", str(bin_dir) + os.pathsep + old_path
    )
    monkeypatch.setitem(execution_env.INITIAL_ENV, "HOME", str(home))
    monkeypatch.setitem(execution_env.INITIAL_ENV, "SHIM_PROJECTS_DIR", str(s.projects))
    monkeypatch.setitem(execution_env.INITIAL_ENV, "SHIM_RUN_LOG", str(s.run_log))
    for key in (
        "SHIM_RESUME_FAIL_MODE",
        "SHIM_RESUME_FAIL_ALWAYS",
        "SHIM_TOOL_USE",
        "SHIM_GATE_FILE",
        "SHIM_ERROR_AFTER_TEXT",
    ):
        execution_env.INITIAL_ENV.pop(key, None)
    monkeypatch.setenv("HOME", str(home))  # Path.home() -> no ~/.mcp.json in argv
    monkeypatch.setenv("TRINITY_CHAT_SESSION_FILE", str(s.marker))
    monkeypatch.setattr(jsonl_recovery, "_JSONL_PROJECTS_DIR", str(s.projects))

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
    # raising=False: these fields are new in #2958, so the unfixed code fails
    # on an assertion, not an AttributeError.
    monkeypatch.setattr(agent_state, "chat_session_id", None, raising=False)
    monkeypatch.setattr(agent_state, "chat_session_model", None, raising=False)
    monkeypatch.setattr(agent_state, "chat_session_generation", 0, raising=False)
    yield s
    for key in (
        "SHIM_RESUME_FAIL_MODE",
        "SHIM_RESUME_FAIL_ALWAYS",
        "SHIM_TOOL_USE",
        "SHIM_GATE_FILE",
        "SHIM_ERROR_AFTER_TEXT",
    ):
        execution_env.INITIAL_ENV.pop(key, None)


def _env(key: str, value: str) -> None:
    # Popped again by the fixture's teardown.
    execution_env.INITIAL_ENV[key] = value


async def _chat(prompt: str, model: str = M1):
    _text, _log, metadata, _raw = await claude_code.execute_claude_code(
        prompt, model=model, execution_id=f"exec-{uuid.uuid4().hex[:12]}"
    )
    return metadata


async def _task(prompt: str, **kwargs):
    _text, _log, metadata, session_id = await execute_headless_task(
        prompt, model=TASK_MODEL, execution_id=f"task-{uuid.uuid4().hex[:12]}", **kwargs
    )
    return session_id


# --------------------------------------------------------------------------
# T1 / T2 — AC1 + AC5: a persisted headless session never reaches the chat
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "task_kwargs",
    [
        pytest.param({"timeout_seconds": 900}, id="auto-persist-over-600s"),
        pytest.param(
            {"timeout_seconds": 300, "persist_session": True}, id="persist_session"
        ),
    ],
)
async def test_chat_never_continues_a_headless_session(shim, task_kwargs):
    chat1 = await _chat("hello chat")
    task_sid = await _task("SEED_LARGE scheduled report", **task_kwargs)
    assert shim.jsonl(
        task_sid
    ).exists(), "harness: the task must have persisted its JSONL"

    chat2 = await _chat("follow-up chat")

    assert chat2.session_id == chat1.session_id
    assert chat2.session_id != task_sid
    assert (
        not chat2.compact_events
    ), "chat 2 compacted: it resumed the task's large session"
    assert "--continue" not in shim.runs()[-1]


# --------------------------------------------------------------------------
# T3 — AC2: a different effective model starts a fresh session
# --------------------------------------------------------------------------


async def test_model_change_starts_a_fresh_session(shim, caplog):
    caplog.set_level(logging.INFO)
    chat1 = await _chat("hello", model=M1)
    agent_state.session_context_tokens = 170_000  # what the router recorded

    chat2 = await _chat("hello again", model=M2)

    assert chat2.session_id != chat1.session_id
    assert "--resume" not in shim.runs()[-1]
    assert "event=chat_session_model_change" in caplog.text
    assert (
        agent_state.session_context_tokens == 0
    ), "a fresh session must not report the old context"
    assert getattr(agent_state, "chat_session_model", None) == M2


async def test_same_model_resumes_by_id(shim):
    chat1 = await _chat("hello", model=M1)
    chat2 = await _chat("hello again", model=M1)
    assert chat2.session_id == chat1.session_id
    assert shim.runs()[-1][shim.runs()[-1].index("--resume") + 1] == chat1.session_id


# --------------------------------------------------------------------------
# T4 — a dead handle gets ONE cold retry, and only when that is safe
# --------------------------------------------------------------------------


@pytest.mark.parametrize("fail_mode", ["is_error_exit0", "stderr"])
async def test_missing_jsonl_retries_cold_once(shim, caplog, fail_mode):
    caplog.set_level(logging.INFO)
    if fail_mode == "stderr":
        _env("SHIM_RESUME_FAIL_MODE", "stderr")
    chat1 = await _chat("hello")
    shim.jsonl(chat1.session_id).unlink()  # reaped
    runs_before = len(shim.runs())

    chat2 = await _chat("are you there")

    assert len(shim.runs()) - runs_before == 2
    assert "--resume" in shim.runs()[-2] and "--resume" not in shim.runs()[-1]
    assert chat2.session_id != chat1.session_id
    assert agent_state.chat_session_id == chat2.session_id
    assert shim.marker_id() == chat2.session_id
    assert "event=chat_resume_fallback" in caplog.text
    assert "reason=resume_jsonl_missing" in caplog.text


async def test_no_retry_after_a_tool_ran(shim):
    chat1 = await _chat("hello")
    shim.jsonl(chat1.session_id).unlink()
    _env("SHIM_TOOL_USE", "1")
    runs_before = len(shim.runs())

    with pytest.raises(HTTPException) as exc:
        await _chat("do a thing")

    assert exc.value.status_code == 502
    assert len(shim.runs()) - runs_before == 1


async def test_no_retry_when_the_jsonl_still_exists(shim):
    chat1 = await _chat("hello")
    _env("SHIM_RESUME_FAIL_ALWAYS", "1")
    runs_before = len(shim.runs())

    with pytest.raises(HTTPException) as exc:
        await _chat("again")

    # The new resume-only branch: visible, not "returned empty response".
    assert exc.value.status_code == 502
    assert "No conversation found" in str(exc.value.detail)
    assert len(shim.runs()) - runs_before == 1
    assert (
        agent_state.chat_session_id == chat1.session_id
    ), "a failed turn never moves the id"
    assert shim.marker_id() == chat1.session_id


async def test_no_retry_when_cancelled(shim, monkeypatch):
    chat1 = await _chat("hello")
    shim.jsonl(chat1.session_id).unlink()
    monkeypatch.setattr(get_process_registry(), "was_terminated", lambda _eid: True)
    runs_before = len(shim.runs())

    with pytest.raises(HTTPException):
        await _chat("again")

    assert len(shim.runs()) - runs_before == 1


# --------------------------------------------------------------------------
# T5 — the capture and the keep-set marker
# --------------------------------------------------------------------------


async def test_resume_error_after_text_still_returns_the_text(shim):
    """The dead-session 502 is for an EMPTY resume turn. A resumed turn that
    answered and then reported `is_error` returns its text, as under
    `--continue` and as a cold turn still does — not a 502."""
    chat1 = await _chat("hello")
    _env("SHIM_ERROR_AFTER_TEXT", "1")
    runs_before = len(shim.runs())

    text, _log, _meta, _raw = await claude_code.execute_claude_code(
        "go on", model=M1, execution_id=f"exec-{uuid.uuid4().hex[:12]}"
    )

    assert text == f"partial answer from {chat1.session_id}"
    assert len(shim.runs()) - runs_before == 1, "no cold retry for a live session"


async def test_marker_written_after_success_and_kept_on_failure(shim):
    chat1 = await _chat("hello")
    assert shim.marker_id() == chat1.session_id
    assert json.loads(shim.marker.read_text())["model"] == M1
    assert (shim.marker.stat().st_mode & 0o777) == 0o600

    _env("SHIM_RESUME_FAIL_ALWAYS", "1")
    with pytest.raises(HTTPException):
        await _chat("fails")
    assert shim.marker_id() == chat1.session_id


async def test_reset_clears_id_and_marker_and_bumps_generation(shim):
    await _chat("hello")
    gen = getattr(agent_state, "chat_session_generation", 0)

    agent_state.reset_session()

    assert getattr(agent_state, "chat_session_id", "unset") is None
    assert not shim.marker.exists()
    assert getattr(agent_state, "chat_session_generation", 0) == gen + 1


async def test_after_reset_the_chat_still_never_continues_a_task(shim):
    await _chat("hello")
    agent_state.reset_session()
    fresh = await _chat("new conversation")
    task_sid = await _task("SEED_LARGE scheduled report", timeout_seconds=900)

    nxt = await _chat("follow-up")

    assert nxt.session_id == fresh.session_id
    assert nxt.session_id != task_sid


async def test_reset_during_a_turn_discards_the_capture(shim, tmp_path, caplog):
    caplog.set_level(logging.INFO)
    gate = tmp_path / "gate"
    _env("SHIM_GATE_FILE", str(gate))
    turn = asyncio.create_task(_chat("slow turn"))
    for _ in range(1000):
        if Path(str(gate) + ".ready").exists():
            break
        await asyncio.sleep(0.01)
    else:
        turn.cancel()
        pytest.fail("harness: the shim never reached its gate")

    agent_state.reset_session()
    gate.touch()
    await turn

    assert agent_state.chat_session_id is None
    assert not shim.marker.exists()
    assert "event=chat_session_capture_discarded" in caplog.text


# --------------------------------------------------------------------------
# T6 — AC3 (agent side): the real compaction numbers, not stdout placeholders
# --------------------------------------------------------------------------


async def test_compaction_numbers_come_from_the_jsonl(shim):
    await _chat("SEED_LARGE long conversation")

    chat2 = await _chat("next")

    assert len(chat2.compact_events) == 1
    ev = chat2.compact_events[0]
    assert (ev.trigger, ev.pre_tokens, ev.post_tokens, ev.duration_ms) == (
        "auto",
        PRE,
        POST,
        DUR,
    )


async def test_a_failed_marker_write_keeps_the_in_memory_id(shim, monkeypatch, tmp_path, caplog):
    """T-C: a full disk must not disable continuity. The write fails loudly and
    returns False; the chat still resumes its own id next turn."""
    caplog.set_level(logging.WARNING)
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    monkeypatch.setenv("TRINITY_CHAT_SESSION_FILE", str(blocker / "chat-session.json"))

    chat1 = await _chat("hello")
    assert "event=chat_session_marker_write_failed" in caplog.text
    assert agent_state.chat_session_id == chat1.session_id

    chat2 = await _chat("again")
    assert chat2.session_id == chat1.session_id


def test_marker_write_rejects_a_non_uuid(tmp_path, monkeypatch):
    from agent_server.services import chat_session_marker

    target = tmp_path / "m.json"
    monkeypatch.setenv("TRINITY_CHAT_SESSION_FILE", str(target))
    assert chat_session_marker.write("../../etc/passwd", "m") is False
    assert not target.exists()
    assert chat_session_marker.write("c4a7c4a7-2958-4958-8958-295829582958", "m") is True
    assert list(tmp_path.iterdir()) == [target], "no temp file left behind"
