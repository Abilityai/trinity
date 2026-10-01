"""#2971 — the Gemini runtime can run a turn on the pinned gemini-cli.

On an unpinned `npm install -g @google/gemini-cli` (0.60.0 at the time of the
report) every Gemini turn failed before reaching the model:

1. `--system-prompt` / `--max-turns` are not gemini-cli flags, so yargs exits 1
   with `Unknown arguments: system-prompt, systemPrompt`. The backend always
   sends a composed system prompt for headless runs, so every scheduled, MCP
   and fan-out run died during argument parsing.
2. The agent home is not a trusted folder, so the CLI exits 55 ("not running
   in a trusted directory") and silently downgrades `--yolo` to `default`.
3. Chat turns used a bare `--resume`, which takes the newest session in the
   store headless runs also write to — the #2958 class, fixed for Claude only.

These tests drive the real `GeminiRuntime.execute` / `execute_headless` with a
captured `Popen`, and the in-image smoke harness against a fake `gemini` that
emulates gemini-cli 0.62.0's argument parser (strict: unknown flag -> exit 1,
`--resume` of a session that does not exist -> exit 42).

Module under test: docker/base-image/agent_server/services/gemini_runtime.py
and docker/base-image/agent_server/services/gemini_cli_args.py.
"""
from __future__ import annotations

import json
import os
import stat
import sys
import textwrap
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from agent_server.services import gemini_cli_args, gemini_runtime  # noqa: E402
from agent_server.state import agent_state  # noqa: E402

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "docker" / "base-image" / "Dockerfile"

# The option surface of gemini-cli 0.62.0, transcribed from `gemini --help`
# (verified 2026-10-01). A flag the runtime emits that is not here would be
# rejected by the pinned CLI's strict parser.
GEMINI_0_62_OPTIONS = {
    "--debug", "-d", "--model", "-m", "--prompt", "-p", "--prompt-interactive",
    "-i", "--skip-trust", "--worktree", "-w", "--sandbox", "-s", "--yolo", "-y",
    "--approval-mode", "--policy", "--admin-policy", "--acp",
    "--experimental-acp", "--allowed-mcp-server-names", "--allowed-tools",
    "--extensions", "-e", "--list-extensions", "-l", "--resume", "-r",
    "--session-file", "--session-id", "--list-sessions", "--delete-session",
    "--include-directories", "--screen-reader", "--output-format", "-o",
    "--raw-output", "--accept-raw-output-risk", "--version", "-v", "--help",
    "-h",
}


def _flags(argv):
    return [a for a in argv[1:] if a.startswith("-")]


# --------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_chat_state():
    saved = (
        agent_state.chat_session_id,
        agent_state.chat_session_model,
        agent_state.session_started,
    )
    agent_state.chat_session_id = None
    agent_state.chat_session_model = None
    agent_state.session_started = False
    yield
    (
        agent_state.chat_session_id,
        agent_state.chat_session_model,
        agent_state.session_started,
    ) = saved


@pytest.fixture
def runtime(monkeypatch):
    rt = gemini_runtime.GeminiRuntime()
    monkeypatch.setattr(rt, "is_available", lambda: True)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    monkeypatch.setattr(gemini_runtime, "kill_cgroup_orphans", lambda **k: 0)
    monkeypatch.setattr(gemini_runtime, "get_process_registry", lambda: MagicMock(
        was_terminated=MagicMock(return_value=False)
    ))
    return rt


class FakeGemini:
    """Captures every spawn; each spawn emits the scripted stream for its turn.

    `script` is a list of (session_id | None, return_code, stderr); a None
    session id with a non-zero code models a run that dies before `init`.
    """

    def __init__(self, script):
        self.script = list(script)
        self.calls = []  # (argv, stdin_text)

    def popen(self, cmd, *a, **k):
        session_id, rc, stderr = self.script.pop(0)
        lines = []
        if session_id:
            lines += [
                json.dumps({"type": "init", "session_id": session_id}) + "\n",
                json.dumps({"type": "message", "role": "assistant", "content": "ok"}) + "\n",
                json.dumps({"type": "result", "stats": {}}) + "\n",
            ]
        lines.append("")
        written = []
        proc = MagicMock()
        proc.pid = 4242
        proc.stdout.readline.side_effect = lines
        proc.stderr.read.return_value = stderr
        proc.wait.return_value = rc
        proc.poll.return_value = rc
        proc.returncode = rc
        proc.stdin.write.side_effect = written.append
        self.calls.append((list(cmd), written))
        return proc

    def argv(self, i):
        return self.calls[i][0]

    def stdin(self, i):
        return "".join(self.calls[i][1])


def _install(monkeypatch, script):
    fake = FakeGemini(script)
    monkeypatch.setattr(gemini_runtime.subprocess, "Popen", fake.popen)
    return fake


def _sid():
    return str(uuid.uuid4())


# --------------------------------------------------------------------------
# headless: only flags the pinned CLI accepts; system prompt still delivered
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_headless_never_passes_flags_the_pinned_cli_rejects(runtime, monkeypatch):
    fake = _install(monkeypatch, [(_sid(), 0, "")])

    await runtime.execute_headless(
        prompt="do the task",
        model="gemini-3-flash",
        allowed_tools=["read_file"],
        system_prompt="PLATFORM INSTRUCTIONS",
        max_turns=7,
        execution_id="exec-h1",
    )

    argv = fake.argv(0)
    assert "--system-prompt" not in argv
    assert "--max-turns" not in argv
    unknown = [f for f in _flags(argv) if f not in GEMINI_0_62_OPTIONS]
    assert unknown == [], f"flags gemini-cli 0.62.0 rejects: {unknown}"


@pytest.mark.asyncio
async def test_headless_delivers_the_system_prompt_in_the_turn_input(runtime, monkeypatch):
    fake = _install(monkeypatch, [(_sid(), 0, "")])

    await runtime.execute_headless(
        prompt="do the task", system_prompt="PLATFORM INSTRUCTIONS",
        execution_id="exec-h2",
    )

    sent = fake.stdin(0)
    assert "PLATFORM INSTRUCTIONS" in sent
    assert "do the task" in sent
    assert sent.index("PLATFORM INSTRUCTIONS") < sent.index("do the task")
    assert "PLATFORM INSTRUCTIONS" not in " ".join(fake.argv(0))


@pytest.mark.asyncio
async def test_headless_without_system_prompt_sends_the_prompt_unchanged(runtime, monkeypatch):
    fake = _install(monkeypatch, [(_sid(), 0, "")])

    await runtime.execute_headless(prompt="just this", execution_id="exec-h3")

    assert fake.stdin(0) == "just this"


@pytest.mark.asyncio
async def test_headless_trusts_the_workspace(runtime, monkeypatch):
    fake = _install(monkeypatch, [(_sid(), 0, "")])

    await runtime.execute_headless(prompt="x", execution_id="exec-h4")

    assert "--skip-trust" in fake.argv(0)
    assert "--yolo" in fake.argv(0)


@pytest.mark.asyncio
async def test_headless_never_resumes(runtime, monkeypatch):
    fake = _install(monkeypatch, [(_sid(), 0, "")])
    agent_state.chat_session_id = _sid()

    await runtime.execute_headless(prompt="x", execution_id="exec-h5")

    assert "--resume" not in fake.argv(0)


# --------------------------------------------------------------------------
# chat: trusted, supported flags, resumes ONLY its own session (#2958 class)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_first_turn_is_cold_and_trusted(runtime, monkeypatch):
    fake = _install(monkeypatch, [(_sid(), 0, "")])

    await runtime.execute(prompt="hi", continue_session=True, execution_id="c1")

    argv = fake.argv(0)
    assert "--resume" not in argv
    assert "--skip-trust" in argv
    assert [f for f in _flags(argv) if f not in GEMINI_0_62_OPTIONS] == []


@pytest.mark.asyncio
async def test_chat_resumes_its_own_session_id_not_the_latest(runtime, monkeypatch):
    chat_sid, task_sid = _sid(), _sid()
    fake = _install(monkeypatch, [
        (chat_sid, 0, ""),   # chat turn 1
        (task_sid, 0, ""),   # a headless run writes a NEWER session
        (chat_sid, 0, ""),   # chat turn 2
    ])

    await runtime.execute(prompt="one", continue_session=True, execution_id="c1")
    await runtime.execute_headless(prompt="task", execution_id="t1")
    await runtime.execute(prompt="two", continue_session=True, execution_id="c2")

    argv = fake.argv(2)
    i = argv.index("--resume")
    assert argv[i + 1] == chat_sid
    assert task_sid not in argv


@pytest.mark.asyncio
async def test_chat_never_uses_a_bare_resume(runtime, monkeypatch):
    sid = _sid()
    fake = _install(monkeypatch, [(sid, 0, ""), (sid, 0, "")])

    await runtime.execute(prompt="one", continue_session=True, execution_id="c1")
    await runtime.execute(prompt="two", continue_session=True, execution_id="c2")

    for argv, _ in fake.calls:
        if "--resume" in argv:
            value = argv[argv.index("--resume") + 1]
            assert value not in ("latest",) and not value.startswith("-")
            assert uuid.UUID(value)


@pytest.mark.asyncio
async def test_chat_model_change_starts_a_fresh_session(runtime, monkeypatch):
    fake = _install(monkeypatch, [(_sid(), 0, ""), (_sid(), 0, "")])

    await runtime.execute(prompt="one", model="gemini-3-flash", continue_session=True, execution_id="c1")
    await runtime.execute(prompt="two", model="gemini-3-pro", continue_session=True, execution_id="c2")

    assert "--resume" not in fake.argv(1)


@pytest.mark.asyncio
async def test_chat_reset_forgets_the_session(runtime, monkeypatch):
    fake = _install(monkeypatch, [(_sid(), 0, ""), (_sid(), 0, "")])

    await runtime.execute(prompt="one", continue_session=True, execution_id="c1")
    agent_state.reset_session()
    await runtime.execute(prompt="two", continue_session=True, execution_id="c2")

    assert "--resume" not in fake.argv(1)


@pytest.mark.asyncio
async def test_failed_chat_turn_does_not_move_the_session(runtime, monkeypatch):
    first = _sid()
    fake = _install(monkeypatch, [
        (first, 0, ""),
        (_sid(), 1, "model exploded"),
        (first, 0, ""),
    ])

    await runtime.execute(prompt="one", continue_session=True, execution_id="c1")
    with pytest.raises(HTTPException):
        await runtime.execute(prompt="two", continue_session=True, execution_id="c2")
    await runtime.execute(prompt="three", continue_session=True, execution_id="c3")

    argv = fake.argv(2)
    assert argv[argv.index("--resume") + 1] == first


@pytest.mark.asyncio
async def test_chat_whose_session_is_gone_retries_once_cold(runtime, monkeypatch):
    first, fresh = _sid(), _sid()
    fake = _install(monkeypatch, [
        (first, 0, ""),
        (None, 42, f'Error resuming session: Invalid session identifier "{first}".'),
        (fresh, 0, ""),
        (fresh, 0, ""),
    ])

    await runtime.execute(prompt="one", continue_session=True, execution_id="c1")
    text, *_ = await runtime.execute(prompt="two", continue_session=True, execution_id="c2")
    await runtime.execute(prompt="three", continue_session=True, execution_id="c3")

    assert text == "ok"
    assert "--resume" in fake.argv(1)
    assert "--resume" not in fake.argv(2)          # the one cold retry
    assert fake.stdin(2) == fake.stdin(1)          # same turn re-sent
    argv = fake.argv(3)
    assert argv[argv.index("--resume") + 1] == fresh


@pytest.mark.asyncio
async def test_chat_resume_failure_that_is_not_a_missing_session_is_not_retried(runtime, monkeypatch):
    fake = _install(monkeypatch, [(_sid(), 0, ""), (None, 1, "quota exceeded")])

    await runtime.execute(prompt="one", continue_session=True, execution_id="c1")
    with pytest.raises(HTTPException):
        await runtime.execute(prompt="two", continue_session=True, execution_id="c2")

    assert len(fake.calls) == 2


@pytest.mark.asyncio
async def test_cold_retry_is_skipped_when_the_turn_was_cancelled(runtime, monkeypatch):
    registry = MagicMock(was_terminated=MagicMock(return_value=True))
    monkeypatch.setattr(gemini_runtime, "get_process_registry", lambda: registry)
    first = _sid()
    fake = _install(monkeypatch, [
        (first, 0, ""),
        (None, 42, "Error resuming session: No previous sessions found for this project."),
    ])

    await runtime.execute(prompt="one", continue_session=True, execution_id="c1")
    with pytest.raises(HTTPException):
        await runtime.execute(prompt="two", continue_session=True, execution_id="c2")

    assert len(fake.calls) == 2


@pytest.mark.asyncio
async def test_chat_delivers_the_system_prompt_in_the_turn_input(runtime, monkeypatch):
    fake = _install(monkeypatch, [(_sid(), 0, "")])

    await runtime.execute(prompt="hello", system_prompt="PLATFORM", continue_session=True, execution_id="c1")

    assert "--system-prompt" not in fake.argv(0)
    assert "PLATFORM" in fake.stdin(0) and "hello" in fake.stdin(0)


# --------------------------------------------------------------------------
# in-image smoke: a real argument parse of the exact argv the runtime builds
# --------------------------------------------------------------------------

_FAKE_GEMINI = textwrap.dedent(
    """\
    #!{python}
    # Emulates gemini-cli 0.62.0's strict yargs parse (#2971).
    import sys
    OPTS = {opts!r}
    VALUED = {{"--model", "--resume", "--output-format", "--allowed-tools",
              "--session-id", "--approval-mode"}}
    args, i = sys.argv[1:], 0
    seen = []
    while i < len(args):
        a = args[i]
        if a.startswith("-"):
            if a not in OPTS:
                print("Unknown arguments: " + a.lstrip("-"), file=sys.stderr)
                sys.exit(1)
            seen.append(a)
            i += 2 if a in VALUED else 1
        else:
            i += 1
    if "--resume" in seen:
        print("Error resuming session: No previous sessions found for this project.", file=sys.stderr)
        sys.exit(42)
    sys.exit(0 if "--list-sessions" in seen else 3)
    """
)


@pytest.fixture
def fake_gemini_bin(tmp_path):
    def make(opts=GEMINI_0_62_OPTIONS):
        path = tmp_path / "gemini"
        path.write_text(_FAKE_GEMINI.format(python=sys.executable, opts=set(opts)))
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return str(path)
    return make


def test_smoke_passes_on_a_cli_that_accepts_every_runtime_flag(fake_gemini_bin):
    assert gemini_cli_args.smoke(fake_gemini_bin()) == []


def test_smoke_fails_when_the_cli_rejects_a_runtime_flag(fake_gemini_bin):
    failures = gemini_cli_args.smoke(fake_gemini_bin(GEMINI_0_62_OPTIONS - {"--skip-trust"}))
    assert failures
    assert any("skip-trust" in f for f in failures)


def test_smoke_fails_when_the_resume_flag_is_gone(fake_gemini_bin):
    failures = gemini_cli_args.smoke(fake_gemini_bin(GEMINI_0_62_OPTIONS - {"--resume"}))
    assert failures


def test_smoke_cli_entrypoint_exit_code(fake_gemini_bin):
    assert gemini_cli_args.main(["--smoke", "--gemini", fake_gemini_bin()]) == 0
    bad = fake_gemini_bin(GEMINI_0_62_OPTIONS - {"--yolo"})
    assert gemini_cli_args.main(["--smoke", "--gemini", bad]) != 0


# --------------------------------------------------------------------------
# image: the CLI is pinned and the smoke runs at build time
# --------------------------------------------------------------------------


def test_dockerfile_pins_the_tested_gemini_cli_version():
    text = DOCKERFILE.read_text()
    assert f"@google/gemini-cli@{gemini_cli_args.GEMINI_CLI_VERSION}" in text
    assert "npm install -g @google/gemini-cli\n" not in text


def test_dockerfile_runs_the_gemini_arg_parse_smoke_after_copying_the_agent_server():
    lines = DOCKERFILE.read_text().splitlines()
    copy_idx = next(i for i, l in enumerate(lines) if l.startswith("COPY") and "./agent_server " in l)
    smoke_idx = next(
        i for i, l in enumerate(lines)
        if l.startswith("RUN") and "gemini_cli_args.py" in l and "--smoke" in l
    )
    assert smoke_idx > copy_idx
