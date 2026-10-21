"""OpenCode runtime unit tests (#2981).

Mirrors the Codex suite over the pure, deterministic pieces of
``opencode_runtime.py``. The subprocess is never spawned: every test here runs
against a parsed line stream or a stubbed ``_execute_opencode``.

The load-bearing cases, in the order they matter:

  * **empty final text is a FAILED turn** (#520) — live on qwen3.6:27b via
    Ollama, which answers in a non-standard ``reasoning`` field with
    ``content: ""`` *while reporting success*. Exit code 0 is not proof.
  * **no cost double-count** — ``tokens.reasoning`` ⊂ ``tokens.output`` and
    ``tokens.cache.read`` ⊂ ``tokens.input``. Cost is native, so the assertion
    is that nothing is added to it and no pricing table exists.
  * **final text is the LAST step's text**, not every text ever emitted.
  * error→HTTP: auth 503, rate 429, generic 500 (NOT 503 — that is the
    backend's AUTH signal and the dispatch breaker counts AUTH only), pipe
    drop 502, timeout 504.
  * `provider/model` shape validated at both boundaries with a named reason.
"""

from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

# conftest wires the real docker/base-image/agent_server as `agent_server`.
from agent_server.services import opencode_runtime  # noqa: E402
from agent_server.services.opencode_runtime import (  # noqa: E402
    FINAL_EMPTY,
    FINAL_OK,
    FINAL_REASONING_ONLY,
    FINAL_TOOLS_ONLY,
    OpenCodeModelShapeError,
    OpenCodeRuntime,
    classify_opencode_failure,
    parse_opencode_jsonl,
    validate_provider_model,
)
from agent_server.services.runtime_adapter import RuntimeCapabilities  # noqa: E402
from agent_server.models import ExecutionMetadata  # noqa: E402



def _lines(*events) -> list:
    return [json.dumps(e) for e in events]


def _step(text: str, *, session="ses_1", tokens=None, cost=None) -> list:
    """One complete step: start → text → finish."""
    events = [
        {"type": "step_start", "sessionID": session},
        {"type": "text", "sessionID": session, "text": text},
    ]
    finish = {"type": "step_finish", "sessionID": session}
    if tokens is not None:
        finish["tokens"] = tokens
    if cost is not None:
        finish["cost"] = cost
    events.append(finish)
    return _lines(*events)


# ---------------------------------------------------------------------------
# parser → metadata
# ---------------------------------------------------------------------------

def test_parse_populates_metadata_and_session():
    lines = _step(
        "the answer",
        tokens={"input": 1000, "output": 500, "reasoning": 300,
                "cache": {"read": 200, "write": 100}},
        cost=0.0042,
    )
    text, log, metadata, raw, outcome = parse_opencode_jsonl(lines, model="anthropic/x")

    assert outcome == FINAL_OK
    assert text == "the answer"
    assert metadata.input_tokens == 1000
    assert metadata.output_tokens == 500
    assert metadata.cache_read_tokens == 200
    assert metadata.cost_usd == pytest.approx(0.0042)
    assert metadata.session_id == "ses_1"
    assert len(raw) == 3


def test_session_id_is_captured_from_any_line():
    """sessionID rides on EVERY line, so capture must not depend on one
    blessed event type arriving — a provider that omits step_start would
    otherwise silently break chat continuity."""
    lines = _lines(
        {"type": "text", "sessionID": "ses_only_here", "text": "hi"},
    )
    _text, _log, metadata, _raw, _outcome = parse_opencode_jsonl(lines)
    assert metadata.session_id == "ses_only_here"


def test_final_text_is_the_last_step_only():
    """There is no separate result line; the response is the LAST step's text.
    An earlier step's narration is transcript, not answer."""
    lines = _step("thinking out loud") + _step("the actual answer")
    text, _log, _metadata, _raw, outcome = parse_opencode_jsonl(lines)
    assert text == "the actual answer"
    assert outcome == FINAL_OK


def test_text_fragments_within_a_step_are_concatenated():
    lines = _lines(
        {"type": "step_start", "sessionID": "s"},
        {"type": "text", "text": "Hello, "},
        {"type": "text", "text": "world."},
        {"type": "step_finish", "sessionID": "s"},
    )
    text, _log, _metadata, _raw, _outcome = parse_opencode_jsonl(lines)
    assert text == "Hello, world."


def test_tool_use_lands_in_the_execution_log():
    lines = _lines(
        {"type": "step_start", "sessionID": "s"},
        {"type": "tool_use", "id": "t1", "tool": "bash", "input": {"cmd": "ls"}},
        {"type": "tool_use", "id": "t1", "tool": "bash", "status": "completed",
         "output": "a\nb"},
        {"type": "text", "text": "done"},
        {"type": "step_finish", "sessionID": "s"},
    )
    _text, log, metadata, _raw, _outcome = parse_opencode_jsonl(lines)
    uses = [e for e in log if e.type == "tool_use"]
    results = [e for e in log if e.type == "tool_result"]
    assert len(uses) == 1, "a repeated tool id must not double-count"
    assert uses[0].tool == "bash"
    assert metadata.tool_count == 1
    assert len(results) == 1 and results[0].success is True


def test_failed_tool_is_recorded_as_unsuccessful():
    lines = _lines(
        {"type": "tool_use", "id": "t1", "tool": "bash", "status": "error",
         "output": "boom"},
    )
    _text, log, _metadata, _raw, _outcome = parse_opencode_jsonl(lines)
    results = [e for e in log if e.type == "tool_result"]
    assert results and results[0].success is False


def test_parser_tolerates_garbage_and_unknown_events():
    """An unknown line must cost a turn nothing."""
    lines = ["not json at all", "", json.dumps({"type": "who_knows", "x": 1})] + _step("ok")
    text, _log, _metadata, _raw, outcome = parse_opencode_jsonl(lines)
    assert text == "ok" and outcome == FINAL_OK


def test_error_event_is_recorded_on_metadata():
    lines = _lines({"type": "error", "message": "provider exploded"})
    _text, _log, metadata, _raw, _outcome = parse_opencode_jsonl(lines)
    assert metadata.error_type == "error"
    assert "provider exploded" in metadata.error_message


# ---------------------------------------------------------------------------
# cost: native, and no double-count
# ---------------------------------------------------------------------------

def test_reasoning_tokens_are_not_added_to_output():
    """`tokens.reasoning` is a SUBSET of `tokens.output`. Adding it would
    inflate every thinking-model turn's billed output."""
    lines = _step("x", tokens={"input": 10, "output": 500, "reasoning": 400})
    _text, _log, metadata, _raw, _outcome = parse_opencode_jsonl(lines)
    assert metadata.output_tokens == 500, "reasoning must not be summed into output"


def test_cache_read_is_not_added_to_input():
    """`tokens.cache.read` is a SUBSET of `tokens.input`."""
    lines = _step("x", tokens={"input": 1000, "output": 1, "cache": {"read": 900}})
    _text, _log, metadata, _raw, _outcome = parse_opencode_jsonl(lines)
    assert metadata.input_tokens == 1000
    assert metadata.cache_read_tokens == 900


def test_cost_is_taken_natively_never_computed():
    """`cost_reporting` is "native", so the reported cost must be exactly what
    the CLI said — not a derived number."""
    lines = _step("x", tokens={"input": 999999, "output": 999999}, cost=0.01)
    _text, _log, metadata, _raw, _outcome = parse_opencode_jsonl(lines)
    assert metadata.cost_usd == pytest.approx(0.01)


def test_cost_sums_across_steps():
    lines = _step("a", cost=0.01) + _step("b", cost=0.02)
    _text, _log, metadata, _raw, _outcome = parse_opencode_jsonl(lines)
    assert metadata.cost_usd == pytest.approx(0.03)


def test_no_pricing_table_exists():
    """#2207: a hand-maintained rate card silently goes stale. Cost is native
    here, so there must be nothing to go stale."""
    suspects = [
        name for name in dir(opencode_runtime)
        if "PRICING" in name.upper() or name.startswith("calculate_")
    ]
    assert suspects == [], f"unexpected pricing machinery: {suspects}"


# ---------------------------------------------------------------------------
# #520 — an empty final text is a FAILED turn
# ---------------------------------------------------------------------------

def test_reasoning_only_response_is_a_failure_not_a_success():
    """The live qwen3.6:27b-via-Ollama shape: prose in `reasoning`, empty text,
    and the CLI reports success."""
    lines = _lines(
        {"type": "step_start", "sessionID": "s"},
        {"type": "reasoning", "text": "Let me think... the answer is 42."},
        {"type": "text", "text": ""},
        {"type": "step_finish", "sessionID": "s"},
    )
    text, _log, _metadata, _raw, outcome = parse_opencode_jsonl(lines)
    assert outcome == FINAL_REASONING_ONLY
    assert text == "", "reasoning must NOT be promoted into the response"


def test_reasoning_is_never_promoted_into_the_response():
    """Promoting a scratchpad would launder a failed turn into a plausible
    success — exactly the failure #520 names."""
    lines = _lines({"type": "reasoning", "text": "the answer is 42"})
    text, _log, _metadata, _raw, outcome = parse_opencode_jsonl(lines)
    assert "42" not in text
    assert outcome == FINAL_REASONING_ONLY


def test_completely_empty_stream_is_a_failure():
    lines = _step("")
    _text, _log, _metadata, _raw, outcome = parse_opencode_jsonl(lines)
    assert outcome == FINAL_EMPTY


def test_tool_only_turn_is_not_a_failure():
    """A turn that did real work and ended without a closing sentence is thin,
    not failed — Codex parity. Tightening this is step 3's scope."""
    lines = _lines(
        {"type": "step_start", "sessionID": "s"},
        {"type": "tool_use", "id": "t1", "tool": "bash", "status": "completed",
         "output": "ok"},
        {"type": "step_finish", "sessionID": "s"},
    )
    text, _log, _metadata, _raw, outcome = parse_opencode_jsonl(lines)
    assert outcome == FINAL_TOOLS_ONLY
    assert text == "(Task completed)"


@pytest.mark.asyncio
async def test_empty_turn_raises_500_even_on_exit_zero(monkeypatch, tmp_path):
    """The end-to-end half: the runtime must refuse to report the reasoning-only
    turn as a success, even though the process exited 0."""
    runtime = OpenCodeRuntime()
    monkeypatch.setattr(runtime, "is_available", lambda: True)
    monkeypatch.setattr(opencode_runtime, "_opencode_root", lambda: str(tmp_path))

    stream = _lines(
        {"type": "step_start", "sessionID": "s"},
        {"type": "reasoning", "text": "thinking..."},
        {"type": "step_finish", "sessionID": "s"},
    )
    _install_fake_subprocess(monkeypatch, stdout_lines=stream, return_code=0)

    with pytest.raises(HTTPException) as exc:
        await runtime.execute_headless("do a thing", model="ollama/qwen3.6:27b")
    assert exc.value.status_code == 500
    assert "reasoning" in str(exc.value.detail).lower()
    # The operator needs to know WHICH model did this — the fix is a provider
    # change, which nobody can make from "the turn failed".
    assert "qwen3.6:27b" in str(exc.value.detail)


# ---------------------------------------------------------------------------
# error → HTTP status
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "stderr,expected",
    [
        ("Error: 401 Unauthorized", 503),
        ("authentication failed for provider", 503),
        ("no credentials found", 503),
        ("invalid api key", 503),
        ("429 Too Many Requests", 429),
        ("rate limit exceeded", 429),
        ("quota exhausted", 429),
        ("panic: runtime error", 500),
        ("", 500),
    ],
)
def test_failure_classification(stderr, expected):
    status, detail = classify_opencode_failure(1, stderr, ExecutionMetadata())
    assert status == expected
    assert detail


def test_generic_failure_is_500_not_503():
    """503 is the backend's AUTH signal and the dispatch breaker counts AUTH
    only — a 503 for an unrelated crash would open the breaker on a healthy
    credential."""
    status, _ = classify_opencode_failure(2, "segfault in the sandbox", ExecutionMetadata())
    assert status == 500


def test_incidental_401_in_tool_output_is_not_an_auth_failure():
    """Anchored patterns, not bare substrings: an upstream MCP tool returning
    401 must not be read as the AGENT's credential failing."""
    status, _ = classify_opencode_failure(
        1, "tool 'http_get' returned HTTP 401 from example.invalid", ExecutionMetadata()
    )
    assert status == 500


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raised,expected",
    [
        (BrokenPipeError("pipe"), 502),
        (ConnectionResetError("reset"), 502),
        (TimeoutError("slow"), 504),
        (RuntimeError("something else"), 500),
    ],
)
async def test_headless_error_mapping(monkeypatch, raised, expected):
    runtime = OpenCodeRuntime()
    monkeypatch.setattr(runtime, "is_available", lambda: True)

    async def _boom(**_kwargs):
        raise raised

    monkeypatch.setattr(runtime, "_execute_opencode", _boom)
    with pytest.raises(HTTPException) as exc:
        await runtime.execute_headless("task")
    assert exc.value.status_code == expected


@pytest.mark.asyncio
async def test_unavailable_cli_is_503(monkeypatch):
    runtime = OpenCodeRuntime()
    monkeypatch.setattr(runtime, "is_available", lambda: False)
    with pytest.raises(HTTPException) as exc:
        await runtime.execute_headless("task")
    assert exc.value.status_code == 503


# ---------------------------------------------------------------------------
# capabilities
# ---------------------------------------------------------------------------

def test_capabilities():
    caps = OpenCodeRuntime.capabilities()
    assert isinstance(caps, RuntimeCapabilities)
    assert caps.chat_continuity is True
    assert caps.session_tab_resume is False
    assert caps.mcp_support is True
    assert caps.cost_reporting == "native"


def test_configure_mcp_persists_into_the_single_store(monkeypatch):
    """Persisted into the ONE store (`.mcp.json`), which the per-turn config
    seam translates into each turn's `mcp:` block. The
    per-shape assertions land with the `.mcp.json` → `mcp:` translation."""
    import agent_server.services.trinity_mcp as trinity_mcp

    monkeypatch.setattr(trinity_mcp, "_configure_claude_mcp_servers", lambda s: True)
    assert OpenCodeRuntime().configure_mcp({"trinity": {}}) is True


def test_context_window_strips_the_provider_prefix():
    """The catalog is keyed on model ids; `anthropic/claude-sonnet-5` would
    otherwise miss and silently return the 200K floor."""
    runtime = OpenCodeRuntime()
    bare = runtime.get_context_window("claude-sonnet-5")
    prefixed = runtime.get_context_window("anthropic/claude-sonnet-5")
    assert prefixed == bare


# ---------------------------------------------------------------------------
# provider/model shape
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "model",
    [
        "anthropic/claude-sonnet-5",
        "ollama/qwen3.6:27b",
        "openrouter/meta-llama/llama-4-70b",
        "replicate/meta/meta-llama-3-70b-instruct",
        "my-provider/model.v2",
    ],
)
def test_valid_provider_model_shapes(model):
    assert validate_provider_model(model) == model


@pytest.mark.parametrize(
    "model", ["claude-sonnet-5", "qwen3.6:27b", "/model", "provider/", "-m/x"]
)
def test_invalid_provider_model_shapes(model):
    with pytest.raises(OpenCodeModelShapeError) as exc:
        validate_provider_model(model)
    assert "provider/model" in str(exc.value)


def test_unset_model_is_valid():
    """Empty means "no pin" — the CLI's own default applies."""
    assert validate_provider_model(None) is None
    assert validate_provider_model("") == ""


@pytest.mark.asyncio
async def test_bad_model_shape_is_a_400_not_a_failed_turn(monkeypatch):
    runtime = OpenCodeRuntime()
    monkeypatch.setattr(runtime, "is_available", lambda: True)
    with pytest.raises(HTTPException) as exc:
        await runtime.execute_headless("task", model="qwen3.6:27b")
    assert exc.value.status_code == 400
    assert "provider prefix" in str(exc.value.detail)


# ---------------------------------------------------------------------------
# command construction
# ---------------------------------------------------------------------------

def test_command_shape():
    cmd = OpenCodeRuntime()._build_command(
        model="ollama/qwen3.6:27b", session_id="ses_1", directory="/home/developer"
    )
    assert cmd[:5] == ["opencode", "run", "--format", "json", "--auto"]
    assert cmd[cmd.index("-m") + 1] == "ollama/qwen3.6:27b"
    assert cmd[cmd.index("--session") + 1] == "ses_1"
    assert cmd[cmd.index("--dir") + 1] == "/home/developer"


def test_command_omits_optional_flags_when_unset():
    cmd = OpenCodeRuntime()._build_command(model=None, session_id=None, directory=None)
    assert cmd == ["opencode", "run", "--format", "json", "--auto"]


# ---------------------------------------------------------------------------
# per-turn config (the system-prompt carrier)
# ---------------------------------------------------------------------------

def test_turn_config_carries_the_system_prompt_as_instructions(monkeypatch, tmp_path):
    """`opencode run` has NO system-prompt flag, so the platform prompt has to
    travel through the `instructions:` config key."""
    monkeypatch.setattr(opencode_runtime, "_opencode_root", lambda: str(tmp_path))
    turn_dir = opencode_runtime._prepare_turn_config("exec-1", "PLATFORM PROMPT")

    config = json.loads((tmp_path / "turns" / "exec-1" / "opencode" / "opencode.json").read_text())
    assert config["instructions"], "instructions key missing"
    prompt_file = config["instructions"][0]
    assert open(prompt_file).read() == "PLATFORM PROMPT"
    assert config["share"] == "disabled"
    assert config["autoupdate"] is False
    assert "plugin" not in config
    opencode_runtime._cleanup_turn_dir(turn_dir)


def test_turn_configs_do_not_collide(monkeypatch, tmp_path):
    """Parallel /api/task turns must not overwrite each other's identity — the
    reason this is per-turn rather than one shared file."""
    monkeypatch.setattr(opencode_runtime, "_opencode_root", lambda: str(tmp_path))
    a = opencode_runtime._prepare_turn_config("exec-a", "PROMPT A")
    b = opencode_runtime._prepare_turn_config("exec-b", "PROMPT B")
    read = lambda d: open(
        json.loads((__import__("pathlib").Path(d) / "opencode" / "opencode.json").read_text())
        ["instructions"][0]
    ).read()
    assert read(a) == "PROMPT A"
    assert read(b) == "PROMPT B"


def test_turn_cleanup_does_not_follow_the_shared_symlink(monkeypatch, tmp_path):
    """The provider tree is SYMLINKED in, so cleanup must unlink, never
    recurse — deleting the seeded tree would make every later cold turn reach
    the npm registry."""
    shared = tmp_path / "config" / "opencode"
    (shared / "node_modules" / "pkg").mkdir(parents=True)
    (shared / "node_modules" / "pkg" / "index.js").write_text("x")
    monkeypatch.setattr(opencode_runtime, "_opencode_root", lambda: str(tmp_path))
    monkeypatch.setattr(opencode_runtime, "_shared_config_dir", lambda: str(shared))

    turn_dir = opencode_runtime._prepare_turn_config("exec-1", None)
    opencode_runtime._cleanup_turn_dir(turn_dir)
    assert (shared / "node_modules" / "pkg" / "index.js").exists()


def test_execution_id_cannot_escape_the_turn_root(monkeypatch, tmp_path):
    monkeypatch.setattr(opencode_runtime, "_opencode_root", lambda: str(tmp_path))
    turn_dir = opencode_runtime._prepare_turn_config("../../etc/evil", None)
    assert str(tmp_path / "turns") in turn_dir
    assert "etc" not in turn_dir.split("turns")[1]


# ---------------------------------------------------------------------------
# test helper: a fake subprocess
# ---------------------------------------------------------------------------

def _install_fake_subprocess(monkeypatch, *, stdout_lines, return_code=0, stderr=""):
    """Replace ``subprocess.Popen`` in the runtime module with a stub that
    replays ``stdout_lines``. The real binary is never invoked in CI."""
    import io

    class _FakeProcess:
        def __init__(self):
            self.stdout = io.StringIO("".join(l + "\n" for l in stdout_lines))
            self.stderr = io.StringIO(stderr)
            self.pid = 4242
            self.returncode = return_code

        def wait(self, timeout=None):
            return return_code

        def poll(self):
            return return_code

        def kill(self):
            pass

        def terminate(self):
            pass

    argv = []

    def _popen(cmd, *a, **k):
        argv.append(list(cmd))
        return _FakeProcess()

    monkeypatch.setattr(opencode_runtime.subprocess, "Popen", _popen)

    class _FakeRegistry:
        def register(self, *a, **k):
            pass

        def unregister(self, *a, **k):
            pass

        def publish_log_entry(self, *a, **k):
            pass

    monkeypatch.setattr(opencode_runtime, "get_process_registry", lambda: _FakeRegistry())
    monkeypatch.setattr(opencode_runtime, "_capture_pgid", lambda p: 4242)
    def _drain(process, stdout_thread, stderr_thread, *a, **k):
        # The real `_drain_bounded` joins the readers; a no-op stub would let
        # the result be read before the stdout thread has parsed the stream.
        stdout_thread.join(5)
        stderr_thread.join(5)

    monkeypatch.setattr(opencode_runtime, "_drain_bounded", _drain)
    monkeypatch.setattr(
        opencode_runtime, "_terminate_process_group", lambda *a, **k: None
    )
    return argv


# ---------------------------------------------------------------------------
# adopted to dev (#2981): ledger, activity tracking, availability, read-only
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_model_name_is_the_resolved_model(monkeypatch, tmp_path):
    """The resolved `-m` is what ran, so it is what the ledger names."""
    runtime = OpenCodeRuntime()
    monkeypatch.setattr(runtime, "is_available", lambda: True)
    monkeypatch.setattr(opencode_runtime, "_opencode_root", lambda: str(tmp_path))
    argv = _install_fake_subprocess(monkeypatch, stdout_lines=_step("done"))

    _text, _log, metadata, _sid = await runtime.execute_headless(
        "task", model="openrouter/qwen/qwen3-coder"
    )
    assert metadata.model_name == "openrouter/qwen/qwen3-coder"
    assert argv[0][argv[0].index("-m") + 1] == "openrouter/qwen/qwen3-coder"


@pytest.mark.asyncio
async def test_unpinned_turn_runs_the_runtime_default_explicitly(monkeypatch, tmp_path):
    """No caller model → the runtime default is passed as `-m`, never left to
    the CLI, so `model_name` is a fact rather than a guess."""
    runtime = OpenCodeRuntime()
    monkeypatch.setattr(runtime, "is_available", lambda: True)
    monkeypatch.setattr(opencode_runtime, "_opencode_root", lambda: str(tmp_path))
    monkeypatch.setenv("AGENT_RUNTIME_MODEL", "openrouter/z-ai/glm-4.6")
    argv = _install_fake_subprocess(monkeypatch, stdout_lines=_step("done"))

    _text, _log, metadata, _sid = await runtime.execute_headless("task")
    assert argv[0][argv[0].index("-m") + 1] == "openrouter/z-ai/glm-4.6"
    assert metadata.model_name == "openrouter/z-ai/glm-4.6"


@pytest.mark.asyncio
async def test_tool_activity_is_tracked_under_the_execution_id(monkeypatch, tmp_path):
    """Parallel /api/task turns share one activity store; a tool start/finish
    recorded without the execution id lands on no execution."""
    runtime = OpenCodeRuntime()
    monkeypatch.setattr(runtime, "is_available", lambda: True)
    monkeypatch.setattr(opencode_runtime, "_opencode_root", lambda: str(tmp_path))
    started, completed = [], []
    monkeypatch.setattr(
        opencode_runtime, "start_tool_execution",
        lambda *a, **k: started.append(k.get("execution_id")),
    )
    monkeypatch.setattr(
        opencode_runtime, "complete_tool_execution",
        lambda *a, **k: completed.append(k.get("execution_id")),
    )
    stream = _lines(
        {"type": "step_start", "sessionID": "s"},
        {"type": "tool_use", "sessionID": "s", "id": "t1", "tool": "bash",
         "input": {"command": "ls"}, "state": {"status": "completed", "output": "ok"}},
        {"type": "text", "sessionID": "s", "text": "listed"},
        {"type": "step_finish", "sessionID": "s"},
    )
    _install_fake_subprocess(monkeypatch, stdout_lines=stream)

    await runtime.execute_headless(
        "task", model="openrouter/x/y", execution_id="exec-42"
    )
    assert started == ["exec-42"]
    assert completed == ["exec-42"]


def test_is_available_probes_once_per_process(monkeypatch):
    runtime = OpenCodeRuntime()
    calls = []

    class _Done:
        returncode = 0

    monkeypatch.setattr(
        opencode_runtime.subprocess, "run", lambda *a, **k: calls.append(a) or _Done()
    )
    assert runtime.is_available() is True
    assert runtime.is_available() is True
    assert len(calls) == 1


def test_is_available_false_when_the_binary_is_missing(monkeypatch):
    runtime = OpenCodeRuntime()

    def _missing(*_a, **_k):
        raise FileNotFoundError("opencode")

    monkeypatch.setattr(opencode_runtime.subprocess, "run", _missing)
    assert runtime.is_available() is False


def test_read_only_prefers_the_root_owned_config(monkeypatch, tmp_path):
    """`/opt/trinity` first, the home copy only while the root file is
    missing — the Codex loader's precedence."""
    root = tmp_path / "opt.json"
    legacy = tmp_path / "home.json"
    monkeypatch.setattr(opencode_runtime, "_READ_ONLY_CONFIG", root)
    monkeypatch.setattr(opencode_runtime, "_LEGACY_READ_ONLY_CONFIG", legacy)

    assert opencode_runtime._is_read_only() is False  # neither present
    legacy.write_text(json.dumps({"enabled": True}))
    assert opencode_runtime._is_read_only() is True   # legacy fallback
    root.write_text(json.dumps({"enabled": False}))
    assert opencode_runtime._is_read_only() is False  # root wins


def test_read_only_fails_open_on_a_corrupt_file(monkeypatch, tmp_path):
    root = tmp_path / "opt.json"
    root.write_text("{not json")
    monkeypatch.setattr(opencode_runtime, "_READ_ONLY_CONFIG", root)
    monkeypatch.setattr(opencode_runtime, "_LEGACY_READ_ONLY_CONFIG", tmp_path / "x")
    assert opencode_runtime._is_read_only() is False


# ---------------------------------------------------------------------------
# checkpoint B (#2981) — parser hardening: the `part`-nested stream shape,
# failed-turn outcomes, the text-shaped tool-call detector, dropped lines,
# and custom-provider cost semantics (ruling T6)
# ---------------------------------------------------------------------------

from pathlib import Path as _Path  # noqa: E402

from agent_server.services.opencode_runtime import (  # noqa: E402
    FINAL_ERROR,
    FINAL_INCOMPLETE,
    FINAL_TEXT_TOOL_CALL,
    FINAL_TRUNCATED,
    text_tool_call_shape,
)

# Pinned name: replaced by the captured stream Before merge, same path.
_FIXTURE = (
    _Path(__file__).resolve().parent.parent
    / "fixtures" / "opencode" / "run-format-json.synthetic.jsonl"
)
_S = "ses_part_1"


def _pev(event_type: str, part: dict | None = None, **top) -> dict:
    """One `part`-nested event, the shape `opencode run --format json` emits."""
    event = {"type": event_type, "timestamp": 1, "sessionID": _S}
    event.update(top)
    if part is not None:
        event["part"] = {"sessionID": _S, "messageID": "msg_1", **part}
    return event


def _pstep(text: str, *, reason="stop", tokens=None, cost=None) -> list:
    finish = {"type": "step-finish", "reason": reason}
    if tokens is not None:
        finish["tokens"] = tokens
    if cost is not None:
        finish["cost"] = cost
    return [
        _pev("step_start", {"type": "step-start"}),
        _pev("text", {"type": "text", "text": text}),
        _pev("step_finish", finish),
    ]


def _ptool(call_id="call_1", tool="bash", status="completed", output="ok") -> dict:
    return _pev("tool_use", {
        "type": "tool", "callID": call_id, "tool": tool,
        "state": {"status": status, "input": {"command": "ls"}, "output": output},
    })


def test_fixture_file_is_named_and_declared_synthetic():
    assert _FIXTURE.name == "run-format-json.synthetic.jsonl"
    head = _FIXTURE.read_text().splitlines()[0].lower()
    assert "synthetic" in head and "before" in _FIXTURE.read_text()[:600].lower()


def test_part_nested_fixture_parses_text_tools_usage_and_cost(caplog):
    """The fork read the flat shape while the real stream nests under `part`;
    that is why cost and model were null on the fork. Everything below comes
    out of `part.*`, with the ledger's model from the resolved `-m`."""
    import logging
    lines = _FIXTURE.read_text().splitlines()
    with caplog.at_level(logging.WARNING, logger=opencode_runtime.logger.name):
        text, log, metadata, raw, outcome = parse_opencode_jsonl(
            lines, model="openrouter/qwen/qwen3-coder"
        )
    assert outcome == FINAL_OK
    assert text == "The directory is empty."           # last step only, concatenated
    assert metadata.session_id == "ses_synthetic_01"
    tools = [e for e in log if e.type == "tool_use"]
    assert [t.tool for t in tools] == ["bash"] and tools[0].id == "call_abc123"
    assert tools[0].input == {"command": "ls -la"}
    results = [e for e in log if e.type == "tool_result"]
    assert results and results[0].output == "total 0" and results[0].success is True
    assert metadata.tool_count == 1
    assert metadata.input_tokens == 2500 and metadata.output_tokens == 120
    assert metadata.cache_read_tokens == 2200
    assert metadata.cost_usd == pytest.approx(0.0020)     # native sum, models.dev provider
    assert len(raw) == 9                                   # six comment lines dropped
    dropped = [r for r in caplog.records if "non-JSON" in r.getMessage()]
    assert len(dropped) == 1 and "6" in dropped[0].getMessage()


def test_part_nested_tool_use_reads_tool_callid_and_state():
    lines = _lines(
        _pev("step_start", {"type": "step-start"}),
        _ptool(call_id="call_9", tool="read", status="error", output="ENOENT"),
        _pev("step_finish", {"type": "step-finish", "reason": "tool-calls"}),
        *_pstep("could not read it"),
    )
    _text, log, metadata, _raw, outcome = parse_opencode_jsonl(lines)
    assert outcome == FINAL_OK
    tool = next(e for e in log if e.type == "tool_use")
    assert (tool.id, tool.tool, tool.input) == ("call_9", "read", {"command": "ls"})
    result = next(e for e in log if e.type == "tool_result")
    assert result.success is False and result.output == "ENOENT"
    assert metadata.tool_count == 1


def test_top_level_shape_still_parses():
    """Flat (top-level) events remain accepted as the fallback."""
    lines = _step("flat ok", tokens={"input": 10, "output": 5}, cost=0.5)
    text, _log, metadata, _raw, outcome = parse_opencode_jsonl(lines)
    assert (text, outcome) == ("flat ok", FINAL_OK)
    assert metadata.input_tokens == 10 and metadata.cost_usd == pytest.approx(0.5)


# -- outcome rules, one test each -------------------------------------------

def test_ok_outcome_on_part_shaped_final_text():
    text, _log, _m, _raw, outcome = parse_opencode_jsonl(_lines(*_pstep("hello")))
    assert (text, outcome) == ("hello", FINAL_OK)


def test_tools_only_outcome_on_part_shape():
    lines = _lines(
        _pev("step_start", {"type": "step-start"}), _ptool(),
        _pev("step_finish", {"type": "step-finish", "reason": "stop"}),
    )
    text, _log, _m, _raw, outcome = parse_opencode_jsonl(lines)
    assert (text, outcome) == ("(Task completed)", FINAL_TOOLS_ONLY)


def test_reasoning_only_outcome_on_part_shape():
    lines = _lines(
        _pev("step_start", {"type": "step-start"}),
        _pev("reasoning", {"type": "reasoning", "text": "thinking"}),
        _pev("step_finish", {"type": "step-finish", "reason": "stop"}),
    )
    text, _log, _m, _raw, outcome = parse_opencode_jsonl(lines)
    assert (text, outcome) == ("", FINAL_REASONING_ONLY)


def test_empty_outcome_on_part_shape():
    _t, _log, _m, _raw, outcome = parse_opencode_jsonl(_lines(*_pstep("")))
    assert outcome == FINAL_EMPTY


@pytest.mark.parametrize(
    "final_text,shape",
    [
        ('<tool_call>{"name": "bash", "arguments": {"command": "ls"}}</tool_call>', "tool_call_tag"),
        ('<function_call name="bash">{"command": "ls"}</function_call>', "function_call_tag"),
        ('Sure. {"name": "bash", "arguments": {"command": "ls"}}', "name_arguments_object"),
        ('{"tool_calls": [{"id": "c1", "function": {"name": "bash"}}]}', "tool_calls_array"),
    ],
)
def test_text_tool_call_is_a_failed_turn_for_each_shape(final_text, shape):
    """llama4:scout emitted its tool call as prose on the fork and the ledger
    recorded success. A text-only final carrying a tool-call shape with zero
    `tool_use` events is a failed turn, named."""
    assert text_tool_call_shape(final_text) == shape
    text, _log, _m, _raw, outcome = parse_opencode_jsonl(_lines(*_pstep(final_text)))
    assert outcome == FINAL_TEXT_TOOL_CALL
    assert text == "", "a text-shaped tool call must not be returned as the answer"


def test_text_tool_call_detector_is_silent_once_real_tools_ran():
    """The detector fires on ZERO `tool_use` events only: a model that ran a
    real tool and then quoted a JSON object in its answer is fine."""
    lines = _lines(
        _pev("step_start", {"type": "step-start"}), _ptool(),
        _pev("step_finish", {"type": "step-finish", "reason": "tool-calls"}),
        *_pstep('I ran it: {"name": "bash", "arguments": {"command": "ls"}}'),
    )
    _t, _log, _m, _raw, outcome = parse_opencode_jsonl(lines)
    assert outcome == FINAL_OK


def test_plain_prose_is_not_a_text_tool_call():
    assert text_tool_call_shape("The function name is bash and its arguments are few.") is None
    assert text_tool_call_shape('{"name": "Alice"}') is None


def test_error_event_fails_the_turn_even_with_text():
    """Any `error` event in the stream fails the turn (decision 15) — the
    message is read from the nested `error.data.message`."""
    lines = _lines(
        *_pstep("partial answer"),
        _pev("error", error={"name": "APIError", "data": {"message": "upstream 502 Bad Gateway"}}),
    )
    text, _log, metadata, _raw, outcome = parse_opencode_jsonl(lines)
    assert outcome == FINAL_ERROR and text == ""
    # The message itself, not a serialized blob that happens to contain it.
    assert metadata.error_message == "upstream 502 Bad Gateway"


def test_provider_5xx_after_tools_ran_is_not_laundered_into_task_completed():
    lines = _lines(
        _pev("step_start", {"type": "step-start"}), _ptool(),
        _pev("step_finish", {"type": "step-finish", "reason": "tool-calls"}),
        _pev("error", part={"error": {"data": {"message": "503 Service Unavailable from provider"}}}),
    )
    text, _log, _m, _raw, outcome = parse_opencode_jsonl(lines)
    assert outcome == FINAL_ERROR
    assert text != "(Task completed)"


def test_step_finish_length_is_a_failed_turn():
    lines = _lines(*_pstep("truncated mid-sen", reason="length"))
    text, _log, _m, _raw, outcome = parse_opencode_jsonl(lines)
    assert outcome == FINAL_TRUNCATED and text == ""


def test_step_finish_tool_calls_with_no_following_step_is_a_failed_turn():
    """`reason: tool-calls` promises another step; a stream that ends there
    never produced the answer those tools were fetched for."""
    lines = _lines(
        _pev("step_start", {"type": "step-start"}), _ptool(),
        _pev("step_finish", {"type": "step-finish", "reason": "tool-calls"}),
    )
    _t, _log, _m, _raw, outcome = parse_opencode_jsonl(lines)
    assert outcome == FINAL_INCOMPLETE


def test_step_finish_tool_calls_followed_by_a_step_is_fine():
    lines = _lines(
        _pev("step_start", {"type": "step-start"}), _ptool(),
        _pev("step_finish", {"type": "step-finish", "reason": "tool-calls"}),
        *_pstep("done"),
    )
    _t, _log, _m, _raw, outcome = parse_opencode_jsonl(lines)
    assert outcome == FINAL_OK


def test_a_started_step_clears_the_tool_calls_promise():
    """The rule is "no following STEP", not "no following step_finish": a
    stream that ends mid-step after `step_start` has kept its promise, and
    the text rule decides the outcome."""
    lines = _lines(
        _pev("step_start", {"type": "step-start"}), _ptool(),
        _pev("step_finish", {"type": "step-finish", "reason": "tool-calls"}),
        _pev("step_start", {"type": "step-start"}),
        _pev("text", {"type": "text", "text": "answer"}),
    )
    text, _log, _m, _raw, outcome = parse_opencode_jsonl(lines)
    assert (text, outcome) == ("answer", FINAL_OK)


def test_dropped_lines_are_counted_and_logged_once(caplog):
    import logging
    lines = ["warming up...", "[info] model loaded", "42"] + _lines(*_pstep("ok"))
    with caplog.at_level(logging.WARNING, logger=opencode_runtime.logger.name):
        _t, _log, _m, _raw, outcome = parse_opencode_jsonl(lines)
    assert outcome == FINAL_OK
    dropped = [r for r in caplog.records if "non-JSON" in r.getMessage()]
    assert len(dropped) == 1, "one log line per turn, not one per dropped line"
    assert "3" in dropped[0].getMessage()


def test_clean_stream_logs_nothing_about_dropped_lines(caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger=opencode_runtime.logger.name):
        parse_opencode_jsonl(_lines(*_pstep("ok")))
    assert not [r for r in caplog.records if "non-JSON" in r.getMessage()]


# -- cost semantics (ruling T6) ---------------------------------------------

def _provider_env(monkeypatch, models):
    monkeypatch.setenv("AGENT_RUNTIME_PROVIDER", json.dumps({
        "id": "local", "npm": "@ai-sdk/openai-compatible",
        "base_url": "http://host.docker.internal:11434/v1",
        "api_key_env": "LOCAL_API_KEY", "models": models,
    }))


def test_custom_provider_without_declared_cost_records_none_not_zero(monkeypatch, caplog):
    """OpenCode prices a custom provider at $0; $0 is not "free", it is
    "unknown". Loop budgets must see None."""
    import logging
    _provider_env(monkeypatch, {"qwen3:27b": {}})
    with caplog.at_level(logging.WARNING, logger=opencode_runtime.logger.name):
        _t, _log, metadata, _raw, outcome = parse_opencode_jsonl(
            _lines(*_pstep("ok", cost=0.0, tokens={"input": 10, "output": 5})),
            model="local/qwen3:27b",
        )
    assert outcome == FINAL_OK
    assert metadata.cost_usd is None
    assert metadata.input_tokens == 10, "tokens are still real; only the price is unknown"
    warned = [r for r in caplog.records if "cost" in r.getMessage().lower()]
    assert len(warned) == 1


def test_custom_provider_with_declared_cost_keeps_the_native_sum(monkeypatch):
    _provider_env(monkeypatch, {"qwen3:27b": {"cost": {"input": 0.1, "output": 0.2}}})
    _t, _log, metadata, _raw, _o = parse_opencode_jsonl(
        _lines(*_pstep("a", cost=0.01), *_pstep("b", cost=0.02)), model="local/qwen3:27b",
    )
    assert metadata.cost_usd == pytest.approx(0.03)


def test_native_provider_cost_is_summed_even_when_a_custom_block_exists(monkeypatch):
    """The custom block governs ITS provider id only; a models.dev route keeps
    the native sum."""
    _provider_env(monkeypatch, {"qwen3:27b": {}})
    _t, _log, metadata, _raw, _o = parse_opencode_jsonl(
        _lines(*_pstep("a", cost=0.01), *_pstep("b", cost=0.02)),
        model="openrouter/qwen/qwen3-coder",
    )
    assert metadata.cost_usd == pytest.approx(0.03)


# -- the end-to-end half: exit 0 is not a success for any failed outcome ----

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stream,expected_status,needle",
    [
        (_lines(*_pstep('<tool_call>{"name":"bash","arguments":{}}</tool_call>')), 500, "text_tool_call"),
        (_lines(*_pstep("x", reason="length")), 500, "truncated"),
        (_lines(_pev("step_start", {"type": "step-start"}), _ptool(),
                _pev("step_finish", {"type": "step-finish", "reason": "tool-calls"})), 500, "incomplete"),
        (_lines(*_pstep("x"), _pev("error", error={"data": {"message": "upstream 502"}})), 500, "upstream 502"),
        (_lines(*_pstep("x"), _pev("error", error={"data": {"message": "401 Unauthorized"}})), 503, "authentication"),
        (_lines(*_pstep("x"), _pev("error", error={"data": {"message": "429 rate limit"}})), 429, "rate limit"),
    ],
)
async def test_failed_outcomes_raise_on_exit_zero(monkeypatch, tmp_path, stream, expected_status, needle):
    runtime = OpenCodeRuntime()
    monkeypatch.setattr(runtime, "is_available", lambda: True)
    monkeypatch.setattr(opencode_runtime, "_opencode_root", lambda: str(tmp_path))
    _install_fake_subprocess(monkeypatch, stdout_lines=stream, return_code=0)
    with pytest.raises(HTTPException) as exc:
        await runtime.execute_headless("task", model="openrouter/x/y")
    assert exc.value.status_code == expected_status
    assert needle in str(exc.value.detail).lower()


@pytest.mark.asyncio
async def test_execute_path_reads_part_shape_and_applies_cost_policy(monkeypatch, tmp_path):
    """The subprocess reader and the unit entrypoint share one line parser:
    part-nested usage lands on the ledger, the custom-provider policy applies,
    and `model_name` stays the resolved `-m`."""
    runtime = OpenCodeRuntime()
    monkeypatch.setattr(runtime, "is_available", lambda: True)
    monkeypatch.setattr(opencode_runtime, "_opencode_root", lambda: str(tmp_path))
    _provider_env(monkeypatch, {"qwen3:27b": {}})
    stream = ["not json"] + _lines(*_pstep("done", cost=0.0, tokens={"input": 7, "output": 3}))
    _install_fake_subprocess(monkeypatch, stdout_lines=stream)
    text, _log, metadata, _sid = await runtime.execute_headless("task", model="local/qwen3:27b")
    assert text == "done"
    assert metadata.input_tokens == 7 and metadata.output_tokens == 3
    assert metadata.cost_usd is None
    assert metadata.model_name == "local/qwen3:27b"
