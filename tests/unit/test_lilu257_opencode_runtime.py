"""OpenCode runtime unit tests (Abilityai/lilu#257 step 1).

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
  * factory: `opencode` resolves, an unknown value RAISES.
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
from agent_server.services.runtime_adapter import (  # noqa: E402
    KNOWN_RUNTIMES,
    RuntimeCapabilities,
    get_runtime,
)
from agent_server.models import ExecutionMetadata  # noqa: E402

pytestmark = pytest.mark.unit


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
    assert caps.session_tab_resume is True
    assert caps.mcp_support is True
    assert caps.cost_reporting == "native"
    assert caps.async_dispatch is True


def test_configure_mcp_persists_into_the_single_store(monkeypatch):
    """Step 1 deliberately returned False here — a stub returning True would
    make "configured" and "silently does nothing" indistinguishable. Step 2
    implements it against the ONE store (`.mcp.json`), which
    `_prepare_turn_config` translates into each turn's `mcp:` block. The
    per-shape assertions live in test_lilu257_opencode_mcp_prompt.py."""
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
# factory + registration
# ---------------------------------------------------------------------------

def test_factory_resolves_opencode(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME", "opencode")
    assert isinstance(get_runtime(), OpenCodeRuntime)


def test_factory_is_case_insensitive(monkeypatch):
    """#1187 casing trap: `OpenCode` must not fall through to Claude."""
    monkeypatch.setenv("AGENT_RUNTIME", "OpenCode")
    assert isinstance(get_runtime(), OpenCodeRuntime)


def test_factory_unknown_still_raises(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME", "opencodex")
    with pytest.raises(ValueError) as exc:
        get_runtime()
    assert "opencodex" in str(exc.value)


def test_opencode_is_a_known_runtime():
    assert "opencode" in KNOWN_RUNTIMES


def test_singleton():
    from agent_server.services.opencode_runtime import get_opencode_runtime
    assert get_opencode_runtime() is get_opencode_runtime()


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

class _FakeStdin:
    """Records what the runtime hands the child on stdin (#3838)."""

    def __init__(self):
        self.text = ""
        self.closed = False

    def write(self, data):
        self.text += data
        return len(data)

    def close(self):
        self.closed = True


def _install_fake_subprocess(monkeypatch, *, stdout_lines, return_code=0, stderr=""):
    """Replace ``subprocess.Popen`` in the runtime module with a stub that
    replays ``stdout_lines``. The real binary is never invoked in CI."""
    import io

    class _FakeProcess:
        def __init__(self):
            self.stdin = _FakeStdin()
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

    monkeypatch.setattr(opencode_runtime.subprocess, "Popen", lambda *a, **k: _FakeProcess())

    class _FakeRegistry:
        def register(self, *a, **k):
            pass

        def unregister(self, *a, **k):
            pass

        def publish_log_entry(self, *a, **k):
            pass

    monkeypatch.setattr(opencode_runtime, "get_process_registry", lambda: _FakeRegistry())
    monkeypatch.setattr(opencode_runtime, "_capture_pgid", lambda p: 4242)
    monkeypatch.setattr(
        opencode_runtime, "_drain_bounded", lambda *a, **k: None
    )
    monkeypatch.setattr(
        opencode_runtime, "_terminate_process_group", lambda *a, **k: None
    )


# ---------------------------------------------------------------------------
# isolated_session, continuity hygiene, reset_session (Abilityai/lilu#651)
# ---------------------------------------------------------------------------

def _stub_stall_retry(monkeypatch, runtime, *, session_id="ses_stub"):
    """Replace _execute_with_stall_retry with a stub returning a canned 5-tuple."""
    from agent_server.models import ExecutionMetadata, ExecutionLogEntry

    async def _stub(**_kwargs):
        return ("response", [], ExecutionMetadata(), [], session_id)

    monkeypatch.setattr(runtime, "_execute_with_stall_retry", _stub)


@pytest.mark.asyncio
async def test_isolated_session_does_not_capture_session_id(monkeypatch):
    """isolated_session=True must NOT store the session id on agent_state —
    the next non-isolated turn should be unaffected."""
    from agent_server.state import agent_state

    runtime = OpenCodeRuntime()
    monkeypatch.setattr(runtime, "is_available", lambda: True)
    _stub_stall_retry(monkeypatch, runtime, session_id="ses_iso")

    # Ensure we start clean
    agent_state.opencode_chat_session_id = None
    agent_state.opencode_chat_session_model = None

    await runtime.execute("prompt", model="anthropic/x", isolated_session=True)

    assert agent_state.opencode_chat_session_id is None, (
        "isolated turn must not capture session id into shared state"
    )
    assert agent_state.opencode_chat_session_model is None


@pytest.mark.asyncio
async def test_isolated_session_does_not_change_current_model(monkeypatch):
    """isolated_session=True must not overwrite agent_state.current_model."""
    from agent_server.state import agent_state

    runtime = OpenCodeRuntime()
    monkeypatch.setattr(runtime, "is_available", lambda: True)
    _stub_stall_retry(monkeypatch, runtime)

    agent_state.current_model = "anthropic/original"

    await runtime.execute("prompt", model="anthropic/override", isolated_session=True)

    assert agent_state.current_model == "anthropic/original", (
        "isolated turn must not update the shared current_model"
    )


@pytest.mark.asyncio
async def test_non_isolated_execute_persists_model(monkeypatch):
    """A non-isolated turn must save the model to agent_state.current_model."""
    from agent_server.state import agent_state

    runtime = OpenCodeRuntime()
    monkeypatch.setattr(runtime, "is_available", lambda: True)
    _stub_stall_retry(monkeypatch, runtime, session_id="ses_1")

    agent_state.current_model = None
    agent_state.session_started = False
    agent_state.opencode_chat_session_id = None
    agent_state.opencode_chat_session_model = None

    await runtime.execute("prompt", model="anthropic/claude-sonnet-5")

    assert agent_state.current_model == "anthropic/claude-sonnet-5"


@pytest.mark.asyncio
async def test_session_is_captured_after_non_isolated_execute(monkeypatch):
    """After a non-isolated turn the session id must be stored so the next
    continue_session=True turn can resume it."""
    from agent_server.state import agent_state

    runtime = OpenCodeRuntime()
    monkeypatch.setattr(runtime, "is_available", lambda: True)
    _stub_stall_retry(monkeypatch, runtime, session_id="ses_saved")

    agent_state.session_started = False
    agent_state.opencode_chat_session_id = None
    agent_state.opencode_chat_session_model = None

    await runtime.execute("prompt", model="anthropic/x", continue_session=False)

    assert agent_state.opencode_chat_session_id == "ses_saved"


@pytest.mark.asyncio
async def test_model_change_starts_fresh_session(monkeypatch):
    """If the model changes between turns the old session must be dropped so the
    next call doesn't try to resume a session from a different model."""
    from agent_server.state import agent_state

    runtime = OpenCodeRuntime()
    monkeypatch.setattr(runtime, "is_available", lambda: True)
    _stub_stall_retry(monkeypatch, runtime, session_id="ses_new")

    agent_state.session_started = True
    agent_state.opencode_chat_session_id = "ses_old"
    agent_state.opencode_chat_session_model = "anthropic/old-model"
    agent_state.current_model = "anthropic/old-model"

    # New turn with a DIFFERENT model and continue_session=True — the old
    # session should be discarded, not resumed.
    await runtime.execute(
        "prompt", model="anthropic/new-model", continue_session=True
    )

    # The stub always returns "ses_new", so the new session id is stored.
    # The key invariant is that we did NOT pass the old session id to the stub.
    assert agent_state.opencode_chat_session_id == "ses_new"
    assert agent_state.opencode_chat_session_model == "anthropic/new-model"


@pytest.mark.asyncio
async def test_model_change_passes_none_session_id_to_stall_retry(monkeypatch):
    """When the model changes the stall-retry helper must receive session_id=None
    (fresh start), not the old id."""
    from agent_server.state import agent_state

    runtime = OpenCodeRuntime()
    monkeypatch.setattr(runtime, "is_available", lambda: True)

    seen_session_id = []

    async def _spy(**kwargs):
        from agent_server.models import ExecutionMetadata
        seen_session_id.append(kwargs.get("session_id"))
        return ("response", [], ExecutionMetadata(), [], "ses_new")

    monkeypatch.setattr(runtime, "_execute_with_stall_retry", _spy)

    agent_state.session_started = True
    agent_state.opencode_chat_session_id = "ses_old"
    agent_state.opencode_chat_session_model = "anthropic/old"
    agent_state.current_model = "anthropic/old"

    await runtime.execute("prompt", model="anthropic/new", continue_session=True)

    assert seen_session_id == [None], (
        "model change must pass session_id=None, not the stale session id"
    )


def test_reset_session_clears_opencode_fields():
    """DELETE /api/chat/history (reset_session) must clear opencode session
    fields so the next turn does NOT silently resume the 'cleared' conversation."""
    from agent_server.state import agent_state

    agent_state.opencode_chat_session_id = "ses_to_clear"
    agent_state.opencode_chat_session_model = "anthropic/x"

    agent_state.reset_session()

    assert agent_state.opencode_chat_session_id is None
    assert agent_state.opencode_chat_session_model is None


# ---------------------------------------------------------------------------
# a model is always resolvable (Abilityai/lilu#652) — the backend now sends
# none when no OpenCode platform default is set; the CLI must never pick.
# ---------------------------------------------------------------------------

def _spy_stall_retry(monkeypatch, runtime):
    seen = []

    async def _spy(**kwargs):
        from agent_server.models import ExecutionMetadata
        seen.append(kwargs.get("model"))
        return ("response", [], ExecutionMetadata(), [], "ses_x")

    monkeypatch.setattr(runtime, "_execute_with_stall_retry", _spy)
    return seen


@pytest.mark.asyncio
@pytest.mark.parametrize("isolated", [False, True])
async def test_lilu652_chat_with_no_model_uses_the_agent_default(monkeypatch, isolated):
    from agent_server.state import agent_state

    monkeypatch.delenv("AGENT_RUNTIME_MODEL", raising=False)
    runtime = OpenCodeRuntime()
    monkeypatch.setattr(runtime, "is_available", lambda: True)
    seen = _spy_stall_retry(monkeypatch, runtime)
    agent_state.current_model = None
    agent_state.session_started = False
    agent_state.opencode_chat_session_id = None
    agent_state.opencode_chat_session_model = None

    await runtime.execute("prompt", model=None, isolated_session=isolated)

    assert seen == [opencode_runtime.DEFAULT_OPENCODE_MODEL]
    # Not written back: a platform default set later must still win next turn.
    assert agent_state.current_model is None


@pytest.mark.asyncio
async def test_lilu652_chat_default_honours_agent_runtime_model(monkeypatch):
    from agent_server.state import agent_state

    monkeypatch.setenv("AGENT_RUNTIME_MODEL", "ollama/gemma4:12b")
    runtime = OpenCodeRuntime()
    monkeypatch.setattr(runtime, "is_available", lambda: True)
    seen = _spy_stall_retry(monkeypatch, runtime)
    agent_state.current_model = None

    await runtime.execute("prompt", model=None)

    assert seen == ["ollama/gemma4:12b"]


@pytest.mark.asyncio
async def test_lilu652_headless_with_no_model_uses_the_agent_default(monkeypatch):
    monkeypatch.delenv("AGENT_RUNTIME_MODEL", raising=False)
    monkeypatch.delenv("CLAUDE_MODEL", raising=False)
    runtime = OpenCodeRuntime()
    monkeypatch.setattr(runtime, "is_available", lambda: True)
    seen = []

    async def _spy(**kwargs):
        seen.append(kwargs.get("model"))
        raise RuntimeError("stop here")

    monkeypatch.setattr(runtime, "_execute_opencode", _spy)
    with pytest.raises(HTTPException):
        await runtime.execute_headless("task", model=None)
    assert seen == [opencode_runtime.DEFAULT_OPENCODE_MODEL]


# ---------------------------------------------------------------------------
# unattended-safe: read-only, deny-list, max_turns, error parity (Abilityai/lilu#654)
# ---------------------------------------------------------------------------

import os  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import threading  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402

from agent_server.services._runtime_config import (  # noqa: E402
    OPENCODE_TOOL_IDS,
    PLATFORM_DENIED_TOOLS,
    map_disallowed_to_opencode,
    merged_disallowed_tools,
)


def _read_only(monkeypatch, tmp_path, enabled=True):
    cfg = tmp_path / "read-only-config.json"
    cfg.write_text(json.dumps({"enabled": enabled}))
    monkeypatch.setattr(opencode_runtime, "_READ_ONLY_CONFIG", cfg)


def _turn_config(monkeypatch, tmp_path, guardrails=None):
    monkeypatch.setattr(opencode_runtime, "_opencode_root", lambda: str(tmp_path / "root"))
    monkeypatch.setattr(opencode_runtime, "_shared_config_dir", lambda: str(tmp_path / "shared"))
    monkeypatch.setattr(opencode_runtime, "_load_guardrails", lambda: dict(guardrails or {}))
    turn_dir = opencode_runtime._prepare_turn_config("exec-654", None, None, env={})
    with open(os.path.join(turn_dir, "opencode", "opencode.json")) as fh:
        return json.load(fh)


def test_read_only_config_emits_write_denies(monkeypatch, tmp_path):
    _read_only(monkeypatch, tmp_path, enabled=True)
    config = _turn_config(monkeypatch, tmp_path)
    assert config["permission"]["edit"] == "deny"
    assert config["permission"]["bash"] == "deny"
    # `permission.write` was measured to be a silent no-op on 1.18.32 —
    # emitting it would read as enforcement and be none.
    assert "write" not in config["permission"]


def test_read_only_off_emits_no_permission_denies(monkeypatch, tmp_path):
    _read_only(monkeypatch, tmp_path, enabled=False)
    config = _turn_config(monkeypatch, tmp_path)
    assert "permission" not in config


def test_read_only_deny_overrides_an_operator_allow(monkeypatch, tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "opencode.json").write_text(json.dumps({"permission": {"bash": "allow", "webfetch": "ask"}}))
    _read_only(monkeypatch, tmp_path, enabled=True)
    config = _turn_config(monkeypatch, tmp_path)
    assert config["permission"] == {"bash": "deny", "webfetch": "ask", "edit": "deny"}


def test_disallowed_tools_are_mapped_or_surfaced(monkeypatch, tmp_path, caplog):
    _read_only(monkeypatch, tmp_path, enabled=False)
    guardrails = {"disallowed_tools": ["Bash", "Write", "MultiEdit", "WebSearch", "NotebookEdit", "mcp__x__y"]}
    with caplog.at_level("WARNING"):
        config = _turn_config(monkeypatch, tmp_path, guardrails)
    assert config["tools"] == {"bash": False, "write": False, "edit": False, "websearch": False}
    warning = " ".join(r.getMessage() for r in caplog.records)
    assert "NotebookEdit" in warning and "mcp__x__y" in warning
    # Platform-denied harness tools have no OpenCode counterpart: absence is
    # the deny, so they are neither mapped nor warned about every turn.
    for name in PLATFORM_DENIED_TOOLS:
        assert name not in warning


def test_mapping_splits_and_dedupes():
    ids, unmapped = map_disallowed_to_opencode(
        merged_disallowed_tools({"disallowed_tools": ["Edit", "MultiEdit", "Zap", "Zap", 7, " "]})
    )
    assert ids == ["edit"]
    assert unmapped == ["Zap"]
    assert map_disallowed_to_opencode(None) == ([], [])


class _SlowProcess:
    """A process that keeps emitting steps and never exits on its own — the
    looping local model the cap exists for. Exits only when killed."""

    def __init__(self, lines):
        import io
        self.stdin = _FakeStdin()
        self.stdout = io.StringIO("".join(l + "\n" for l in lines))
        self.stderr = io.StringIO("")
        self.pid = 4242
        self.returncode = None
        self.killed = threading.Event()

    def wait(self, timeout=None):
        if self.killed.wait(timeout):
            return -15
        raise subprocess.TimeoutExpired("opencode", timeout)

    def poll(self):
        return -15 if self.killed.is_set() else None

    def kill(self):
        self.killed.set()

    def terminate(self):
        self.killed.set()


@pytest.mark.asyncio
async def test_max_turns_kills_and_returns_422_with_turn_limit_prefix(monkeypatch, tmp_path):
    runtime = OpenCodeRuntime()
    monkeypatch.setattr(runtime, "is_available", lambda: True)
    monkeypatch.setattr(opencode_runtime, "_opencode_root", lambda: str(tmp_path))
    _install_fake_subprocess(monkeypatch, stdout_lines=[])
    stream = []
    for n in range(10):
        stream += _lines(
            {"type": "step_start", "sessionID": "s"},
            {"type": "tool_use", "sessionID": "s", "id": f"t{n}", "tool": "read", "input": {}},
            {"type": "step_finish", "sessionID": "s"},
        )
    proc = _SlowProcess(stream)
    monkeypatch.setattr(opencode_runtime.subprocess, "Popen", lambda *a, **k: proc)
    monkeypatch.setattr(
        opencode_runtime, "_terminate_process_group", lambda p, **k: p.kill()
    )

    with pytest.raises(HTTPException) as exc:
        await runtime.execute_headless(
            "count to 1000 one tool call at a time", model="ollama/m", max_turns=3,
            timeout_seconds=30,
        )
    assert exc.value.status_code == 422
    assert str(exc.value.detail).startswith("Task exceeded turn limit:")
    assert proc.killed.is_set()
    # Same literal the Claude headless path raises — one classifier for both.
    import inspect
    from agent_server.services import headless_executor
    assert 'detail=f"Task exceeded turn limit: ' in inspect.getsource(headless_executor)


@pytest.mark.asyncio
async def test_max_turns_defaults_to_guardrails(monkeypatch):
    runtime = OpenCodeRuntime()
    monkeypatch.setattr(runtime, "is_available", lambda: True)
    monkeypatch.setattr(opencode_runtime, "_load_guardrails", lambda: {"max_turns_task": 7})
    seen = {}

    async def _stub(**kwargs):
        seen.update(kwargs)
        return ("ok", [], ExecutionMetadata(), [], "s")

    monkeypatch.setattr(runtime, "_execute_with_stall_retry", _stub)
    await runtime.execute_headless("t", model="ollama/m")
    assert seen["max_turns"] == 7
    await runtime.execute_headless("t", model="ollama/m", max_turns=2)
    assert seen["max_turns"] == 2


_OLLAMA_404 = {
    "type": "error", "sessionID": "s",
    "error": {"name": "APIError", "data": {
        "message": 'model "does-not-exist" not found, try pulling it first',
        "statusCode": 404, "isRetryable": False}},
}


@pytest.mark.asyncio
async def test_unknown_model_error_is_a_400_model_unsupported(monkeypatch, tmp_path):
    """The event is the one the pinned CLI emitted for an OpenAI-compatible
    endpoint answering like Ollama does for an unpulled model."""
    runtime = OpenCodeRuntime()
    monkeypatch.setattr(runtime, "is_available", lambda: True)
    monkeypatch.setattr(opencode_runtime, "_opencode_root", lambda: str(tmp_path))
    _install_fake_subprocess(monkeypatch, stdout_lines=_lines(_OLLAMA_404), return_code=1)
    with pytest.raises(HTTPException) as exc:
        await runtime.execute_headless("t", model="ollama/does-not-exist")
    assert exc.value.status_code == 400
    assert exc.value.detail["error_code"] == "model_unsupported"
    assert exc.value.detail["message"].startswith("Model unsupported: ")
    assert "does-not-exist" in exc.value.detail["message"]


@pytest.mark.parametrize(
    "status,message,expected,error_type",
    [
        (401, "Invalid API key provided", 503, "authentication_failed"),
        (403, "forbidden", 503, "authentication_failed"),
        (429, "slow down", 429, "rate_limit"),
        (404, "route not found", 500, "execution_error"),
        (None, "Unexpected server error. Check server logs for details.", 500, "execution_error"),
    ],
)
def test_structured_error_events_classify(status, message, expected, error_type):
    event = {"type": "error", "error": {"name": "APIError", "data": {"message": message}}}
    if status is not None:
        event["error"]["data"]["statusCode"] = status
    metadata = ExecutionMetadata()
    state = opencode_runtime._OpenCodeParseState(execution_log=[], metadata=metadata)
    opencode_runtime._process_opencode_event(event, state)
    assert metadata.error_message == message
    code, _ = classify_opencode_failure(
        1, "", metadata, error_status=state.error_status, error_name=state.error_name
    )
    assert code == expected
    assert metadata.error_type == error_type
    assert metadata.error_code


def test_cache_write_sets_cache_creation_tokens():
    _text, _log, metadata, _raw, _out = parse_opencode_jsonl(_lines(
        {"type": "step_start"},
        {"type": "text", "text": "hi"},
        {"type": "step_finish", "tokens": {"input": 10, "output": 2, "cache": {"read": 3, "write": 5}}},
    ))
    assert metadata.cache_creation_tokens == 5
    assert metadata.cache_read_tokens == 3


def test_health_reports_config_enforcement_for_opencode_only(monkeypatch):
    from agent_server.routers import info
    from agent_server.state import agent_state
    monkeypatch.setattr(agent_state, "agent_runtime", "opencode")
    assert info._read_only_enforcement() == {"read_only_enforcement": "config"}
    for other in ("claude-code", "codex", "gemini-cli"):
        monkeypatch.setattr(agent_state, "agent_runtime", other)
        assert info._read_only_enforcement() == {}


# -- real denied turns against the PINNED binary ------------------------------
# Every opencode.json key the runtime relies on is proven here by a real turn of
# opencode 1.18.32 against a local fake OpenAI-compatible model (127.0.0.1, no
# network, no live model). Skipped when the binary is absent (CI does not build
# the base image); run with OPENCODE_BIN=/path/to/opencode or it on PATH.

_OPENCODE_BIN = os.environ.get("OPENCODE_BIN") or shutil.which("opencode")
_needs_binary = pytest.mark.skipif(not _OPENCODE_BIN, reason="pinned opencode binary not available")


class _FakeModel(BaseHTTPRequestHandler):
    calls: list = []
    tool = ("write", {"filePath": "rw-probe.txt", "content": "x"})
    loop = False
    status = None

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if type(self).status:
            code, msg = type(self).status
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": {"message": msg}}).encode())
            return
        msgs = body.get("messages", [])
        type(self).calls.append({
            "tools": [t["function"]["name"] for t in body.get("tools") or []],
            "tool_results": [m.get("content") for m in msgs if m.get("role") == "tool"],
        })
        if not type(self).loop and any(m.get("role") == "tool" for m in msgs):
            delta, finish = {"role": "assistant", "content": "done"}, "stop"
        else:
            name, args = type(self).tool
            delta = {"role": "assistant", "tool_calls": [{
                "index": 0, "id": f"call_{len(type(self).calls)}", "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)}}]}
            finish = "tool_calls"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for chunk in ({"choices": [{"index": 0, "delta": delta}]},
                      {"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]}):
            chunk.update(id="x", object="chat.completion.chunk", created=0, model="m")
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")


@pytest.fixture
def fake_model():
    _FakeModel.calls = []
    _FakeModel.loop = False
    _FakeModel.status = None
    _FakeModel.tool = ("write", {"filePath": "rw-probe.txt", "content": "x"})
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeModel)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()


@pytest.fixture
def real_runtime(monkeypatch, tmp_path, fake_model):
    """The real `_execute_opencode` driving the real binary at the fake model."""
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "opencode.json").write_text(json.dumps({"provider": {"fake": {
        "npm": "@ai-sdk/openai-compatible", "name": "Fake",
        "options": {"baseURL": f"http://127.0.0.1:{fake_model.server_address[1]}/v1", "apiKey": "x"},
        "models": {"m": {"name": "m", "tool_call": True}}}}}))
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(opencode_runtime, "_opencode_root", lambda: str(tmp_path / "root"))
    monkeypatch.setattr(opencode_runtime, "_shared_config_dir", lambda: str(shared))
    monkeypatch.setattr(opencode_runtime, "_AGENT_HOME", str(home))
    monkeypatch.setattr(opencode_runtime, "apply_mcp_config", lambda config: None)
    monkeypatch.setattr(opencode_runtime, "refresh_agents_md", lambda: None)
    monkeypatch.setenv("PATH", os.path.dirname(_OPENCODE_BIN) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("HOME", str(tmp_path))
    runtime = OpenCodeRuntime()
    monkeypatch.setattr(runtime, "is_available", lambda: True)
    # Kill only the child's OWN group (it runs with start_new_session): the
    # production sweep also hunts the execution tag across the container, which
    # in a test run reaches the test runner's siblings.
    import signal

    def _kill_group(process, **kwargs):
        try:
            os.killpg(kwargs.get("pgid") or process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    monkeypatch.setattr(opencode_runtime, "_terminate_process_group", _kill_group)
    monkeypatch.setattr(
        opencode_runtime, "_drain_bounded",
        lambda process, out_t, err_t, grace=3, **k: (out_t.join(grace), err_t.join(grace)),
    )
    build = runtime._build_command
    # The spawn env carries the container's PATH, not the test's: name the
    # binary absolutely instead.
    monkeypatch.setattr(runtime, "_build_command", lambda **k: [_OPENCODE_BIN, *build(**k)[1:]])
    return runtime, home


@_needs_binary
@pytest.mark.asyncio
@pytest.mark.parametrize("tool,args", [
    ("write", {"filePath": "rw-probe.txt", "content": "x"}),
    ("edit", {"filePath": "rw-probe.txt", "oldString": "", "newString": "x"}),
    ("bash", {"command": "echo x > rw-probe.txt", "description": "probe"}),
])
async def test_real_binary_read_only_denies_the_write(monkeypatch, tmp_path, real_runtime, tool, args):
    runtime, home = real_runtime
    _read_only(monkeypatch, tmp_path, enabled=True)
    monkeypatch.setattr(opencode_runtime, "_load_guardrails", lambda: {})
    _FakeModel.tool = (tool, args)
    await runtime.execute_headless("create a file named rw-probe.txt", model="fake/m", timeout_seconds=90)
    assert not (home / "rw-probe.txt").exists()
    offered = _FakeModel.calls[-1]["tools"]
    assert not {"edit", "write", "bash"} & set(offered)
    assert "unavailable tool" in _FakeModel.calls[-1]["tool_results"][-1]


@_needs_binary
@pytest.mark.asyncio
async def test_real_binary_read_only_off_still_writes(monkeypatch, tmp_path, real_runtime):
    """The control: without the deny the same turn DOES write the file."""
    runtime, home = real_runtime
    _read_only(monkeypatch, tmp_path, enabled=False)
    monkeypatch.setattr(opencode_runtime, "_load_guardrails", lambda: {})
    await runtime.execute_headless("create a file named rw-probe.txt", model="fake/m", timeout_seconds=90)
    assert (home / "rw-probe.txt").exists()


@_needs_binary
@pytest.mark.asyncio
async def test_real_binary_every_mapped_id_is_removed(monkeypatch, tmp_path, real_runtime):
    runtime, home = real_runtime
    _read_only(monkeypatch, tmp_path, enabled=False)
    monkeypatch.setattr(
        opencode_runtime, "_load_guardrails",
        lambda: {"disallowed_tools": sorted(OPENCODE_TOOL_IDS)},
    )
    await runtime.execute_headless("create a file named rw-probe.txt", model="fake/m", timeout_seconds=90)
    assert not (home / "rw-probe.txt").exists()
    mapped = {i for ids in OPENCODE_TOOL_IDS.values() for i in ids}
    offered = set(_FakeModel.calls[-1]["tools"])
    # Every tool 1.18.32 offers by default is in the table, so none is left.
    assert len(_FakeModel.calls) >= 2 and not (mapped & offered), offered


@_needs_binary
@pytest.mark.asyncio
async def test_real_binary_max_turns_kills_a_looping_model(monkeypatch, tmp_path, real_runtime):
    runtime, _home = real_runtime
    _read_only(monkeypatch, tmp_path, enabled=False)
    monkeypatch.setattr(opencode_runtime, "_load_guardrails", lambda: {})
    _FakeModel.loop = True
    _FakeModel.tool = ("read", {"filePath": "x"})
    with pytest.raises(HTTPException) as exc:
        await runtime.execute_headless("count to 1000", model="fake/m", max_turns=3, timeout_seconds=90)
    assert exc.value.status_code == 422
    assert str(exc.value.detail).startswith("Task exceeded turn limit:")
    assert len([c for c in _FakeModel.calls if c["tools"]]) <= 5


@_needs_binary
@pytest.mark.asyncio
async def test_real_binary_unknown_model_is_400(monkeypatch, tmp_path, real_runtime):
    runtime, _home = real_runtime
    _read_only(monkeypatch, tmp_path, enabled=False)
    monkeypatch.setattr(opencode_runtime, "_load_guardrails", lambda: {})
    _FakeModel.status = (404, 'model "does-not-exist" not found, try pulling it first')
    with pytest.raises(HTTPException) as exc:
        await runtime.execute_headless("t", model="fake/m", timeout_seconds=60)
    assert exc.value.status_code == 400
    assert exc.value.detail["error_code"] == "model_unsupported"


# ---------------------------------------------------------------------------
# Abilityai/lilu#655 — full platform citizen
# ---------------------------------------------------------------------------

from agent_server.services.opencode_runtime import (  # noqa: E402
    load_workspace_commands,
    resolve_context_window_hint,
    split_slash_command,
)


def _write_skill_tree(home):
    skills = home / ".claude" / "skills"
    (skills / "self-diagnostic").mkdir(parents=True)
    (skills / "self-diagnostic" / "SKILL.md").write_text(
        "---\nname: self-diagnostic\ndescription: Check yourself\n---\n\nRun the checks.\n"
    )
    (skills / "with-args").mkdir()
    (skills / "with-args" / "SKILL.md").write_text("Target: $ARGUMENTS\n")
    (skills / "bad name").mkdir()
    (skills / "bad name" / "SKILL.md").write_text("never")
    (skills / "empty").mkdir()
    (skills / "empty" / "SKILL.md").write_text("---\ndescription: x\n---\n")
    commands = home / ".claude" / "commands"
    commands.mkdir()
    (commands / "deploy.md").write_text("Deploy it.")
    (commands / "with-args.md").write_text("the command copy loses to the skill")
    (commands / "notes.txt").write_text("not a command")


def test_workspace_commands_mirror_skills_and_commands(tmp_path):
    _write_skill_tree(tmp_path)
    commands = load_workspace_commands(str(tmp_path))
    assert set(commands) == {"self-diagnostic", "with-args", "deploy"}
    # Frontmatter is metadata, not prompt; the description is carried over.
    diag = commands["self-diagnostic"]
    assert diag["description"] == "Check yourself"
    assert "---" not in diag["template"]
    # A body without $ARGUMENTS gets it appended, so args always reach the model.
    assert diag["template"] == "Run the checks.\n\n$ARGUMENTS"
    # An explicit placeholder is left where the author put it; the skill wins
    # a name clash with a command.
    assert commands["with-args"]["template"] == "Target: $ARGUMENTS"
    assert commands["deploy"]["template"] == "Deploy it.\n\n$ARGUMENTS"


@pytest.mark.parametrize(
    "prompt,expected",
    [
        ("/self-diagnostic", ("self-diagnostic", "")),
        ("  /with-args alpha beta ", ("with-args", "alpha beta")),
        ("/plugin:skill x", ("plugin:skill", "x")),
        ("/deploy\nline two", ("deploy", "line two")),
        ("/home/developer/notes.md what is this?", None),
        ("plain message", None),
        ("/", None),
        ("", None),
    ],
)
def test_split_slash_command(prompt, expected):
    assert split_slash_command(prompt) == expected


def test_turn_config_carries_the_command_map(monkeypatch, tmp_path):
    monkeypatch.setattr(opencode_runtime, "_opencode_root", lambda: str(tmp_path / "oc"))
    monkeypatch.setattr(
        opencode_runtime, "_load_shared_config",
        lambda: {"command": {"deploy": {"template": "operator's own"}}},
    )
    monkeypatch.setattr(opencode_runtime, "apply_mcp_config", lambda config: None)
    turn_dir = opencode_runtime._prepare_turn_config(
        "e-cmd", None, None,
        commands={"deploy": {"template": "mirrored"}, "self-diagnostic": {"template": "x"}},
    )
    with open(f"{turn_dir}/opencode/opencode.json") as fh:
        config = json.load(fh)
    assert config["command"]["self-diagnostic"] == {"template": "x"}
    # Additive: the operator's declared command keeps its name.
    assert config["command"]["deploy"] == {"template": "operator's own"}


class _Seen(list):
    """argv per spawn, plus the fake processes (their ``stdin.text``)."""

    def __init__(self):
        super().__init__()
        self.procs = []

    def stdin(self, i=0):
        return self.procs[i].stdin.text


def _spawn_capture(monkeypatch, tmp_path, stream):
    """Fake subprocess + a skill tree; returns the list every argv lands in."""
    _write_skill_tree(tmp_path)
    monkeypatch.setattr(opencode_runtime, "_AGENT_HOME", str(tmp_path))
    monkeypatch.setattr(opencode_runtime, "_opencode_root", lambda: str(tmp_path / "oc"))
    monkeypatch.setattr(opencode_runtime, "refresh_agents_md", lambda: None)
    monkeypatch.setattr(opencode_runtime, "apply_mcp_config", lambda config: None)
    _install_fake_subprocess(monkeypatch, stdout_lines=stream, return_code=0)
    seen = _Seen()
    fake_popen = opencode_runtime.subprocess.Popen

    def _popen(cmd, *a, **k):
        seen.append(list(cmd))
        proc = fake_popen(cmd, *a, **k)
        seen.procs.append(proc)
        return proc

    monkeypatch.setattr(opencode_runtime.subprocess, "Popen", _popen)
    runtime = OpenCodeRuntime()
    monkeypatch.setattr(runtime, "is_available", lambda: True)
    return runtime, seen


@pytest.mark.asyncio
async def test_playbook_runs_as_a_command_with_args(monkeypatch, tmp_path):
    runtime, seen = _spawn_capture(monkeypatch, tmp_path, _step("done"))
    response, *_ = await runtime.execute_headless(
        "/with-args alpha beta", model="anthropic/claude-sonnet-5"
    )
    assert response == "done"
    cmd = seen[0]
    assert cmd[cmd.index("--command") + 1] == "with-args"
    assert cmd[-1] == "--"
    assert "alpha beta" not in cmd  # the args travel on stdin (#3838)
    assert seen.stdin() == "alpha beta"


@pytest.mark.asyncio
async def test_argless_playbook_sends_no_message(monkeypatch, tmp_path):
    runtime, seen = _spawn_capture(monkeypatch, tmp_path, _step("ok"))
    await runtime.execute_headless("/self-diagnostic", model="anthropic/claude-sonnet-5")
    cmd = seen[0]
    assert cmd[-2:] == ["--command", "self-diagnostic"]
    assert "--" not in cmd
    assert seen.stdin() == ""


@pytest.mark.asyncio
async def test_unknown_playbook_answers_like_claude_without_spawning(monkeypatch, tmp_path):
    """The backend's #1410 detector keys on this exact reply, so a missing
    skill on an OpenCode agent records SKILL_NOT_FOUND instead of letting a
    model improvise an answer that reads as success."""
    runtime, seen = _spawn_capture(monkeypatch, tmp_path, _step("should not run"))
    response, log, metadata, _session = await runtime.execute_headless(
        "/nope now", model="anthropic/claude-sonnet-5"
    )
    assert response == "Unknown command: /nope"
    assert seen == []
    assert log == []
    assert metadata.cost_usd is None


@pytest.mark.asyncio
async def test_a_path_is_a_message_not_a_command(monkeypatch, tmp_path):
    runtime, seen = _spawn_capture(monkeypatch, tmp_path, _step("it is a file"))
    await runtime.execute_headless(
        "/home/developer/notes.md what is this?", model="anthropic/claude-sonnet-5"
    )
    assert "--command" not in seen[0]
    assert seen.stdin() == "/home/developer/notes.md what is this?"


@pytest.mark.asyncio
async def test_a_prompt_over_the_argv_limit_goes_on_stdin(monkeypatch, tmp_path):
    """#3838 class (review B2): a 200 KiB message — over Linux's 131,072-byte
    MAX_ARG_STRLEN — must never be an argv element, or Popen raises E2BIG."""
    runtime, seen = _spawn_capture(monkeypatch, tmp_path, _step("ok"))
    big = "x" * (200 * 1024)
    await runtime.execute_headless(big, model="anthropic/claude-sonnet-5")
    assert all(len(arg) < 131072 for arg in seen[0])
    assert seen[0][-1] == "--"
    assert seen.stdin() == big
    assert seen.procs[0].stdin.closed


@pytest.mark.asyncio
async def test_a_failed_stdin_handoff_ends_the_child_and_unregisters(monkeypatch, tmp_path):
    """A child that dies at startup breaks the pipe; the write raises before the
    try/finally that unregisters, so the handoff path must clean up itself."""
    runtime, seen = _spawn_capture(monkeypatch, tmp_path, _step("ok"))
    killed, unregistered = [], []

    class _Registry:
        def register(self, *a, **k):
            pass

        def unregister(self, execution_id, *a, **k):
            unregistered.append(execution_id)

        def publish_log_entry(self, *a, **k):
            pass

    monkeypatch.setattr(opencode_runtime, "get_process_registry", lambda: _Registry())
    monkeypatch.setattr(
        opencode_runtime, "_terminate_process_group", lambda *a, **k: killed.append(1)
    )

    def _broken(_data):
        raise BrokenPipeError("child exited")

    real_popen = opencode_runtime.subprocess.Popen

    def _popen(cmd, *a, **k):
        proc = real_popen(cmd, *a, **k)
        proc.stdin.write = _broken
        return proc

    monkeypatch.setattr(opencode_runtime.subprocess, "Popen", _popen)
    # The runtime maps the error at its boundary; what matters here is that it
    # surfaces as a failure, not a hang, and that the child and entry are gone.
    with pytest.raises(Exception):
        await runtime.execute_headless("hi", model="anthropic/claude-sonnet-5",
                                       execution_id="exec-b2")
    assert killed and "exec-b2" in unregistered


@pytest.mark.asyncio
@pytest.mark.parametrize("read_only", [False, True])
async def test_share_kill_switch_and_read_only_deny_ride_the_env(
    monkeypatch, tmp_path, read_only
):
    """Review B1: a workspace `opencode.json` merges OVER the per-turn global
    config, so the share kill switch and the read-only deny set are also
    delivered through env, which OpenCode applies after every config file."""
    runtime, seen = _spawn_capture(monkeypatch, tmp_path, _step("ok"))
    envs = []
    inner = opencode_runtime.subprocess.Popen

    def _popen(cmd, *a, **k):
        envs.append(dict(k.get("env") or {}))
        return inner(cmd, *a, **k)

    monkeypatch.setattr(opencode_runtime.subprocess, "Popen", _popen)
    monkeypatch.setattr(opencode_runtime, "_is_read_only", lambda: read_only)
    await runtime.execute_headless("hi", model="anthropic/claude-sonnet-5")
    env = envs[0]
    assert env["OPENCODE_DISABLE_SHARE"] == "1"
    if read_only:
        assert json.loads(env["OPENCODE_PERMISSION"]) == {"edit": "deny", "bash": "deny"}
    else:
        assert "OPENCODE_PERMISSION" not in env


def test_input_tokens_is_the_last_steps_input_not_the_sum():
    """Every step re-sends the session, so the sum counts the context once per
    step. 3 steps of 1000/1200/1500 occupy 1500, not 3700."""
    lines = (
        _step("a", tokens={"input": 1000, "output": 10})
        + _step("b", tokens={"input": 1200, "output": 20})
        + _step("c", tokens={"input": 1500, "output": 30}, cost=0.01)
    )
    _text, _log, metadata, _raw, _outcome = parse_opencode_jsonl(lines)
    assert metadata.input_tokens == 1500
    assert metadata.input_tokens < 1000 + 1200 + 1500
    assert metadata.output_tokens == 60  # output still sums


def test_a_step_without_usage_does_not_zero_the_input():
    lines = _step("a", tokens={"input": 900, "output": 1}) + _step("b")
    _text, _log, metadata, _raw, _outcome = parse_opencode_jsonl(lines)
    assert metadata.input_tokens == 900


def test_context_window_hint_from_provider_config_then_env():
    config = {"provider": {"ollama": {"models": {"gemma4:12b": {"limit": {"context": 32768}}}}}}
    assert resolve_context_window_hint("ollama/gemma4:12b", config, {}) == 32768
    env = {"OPENCODE_PROVIDER_OLLAMA_CONTEXT": "8192"}
    assert resolve_context_window_hint("ollama/qwen3:8b", config, env) == 8192
    # Config beats env for the model it names.
    assert resolve_context_window_hint("ollama/gemma4:12b", config, env) == 32768
    # Nothing declared -> None, so the caller keeps the catalog value.
    assert resolve_context_window_hint("ollama/qwen3:8b", {}, {}) is None
    assert resolve_context_window_hint("anthropic/claude-sonnet-5", None, {}) is None
    assert resolve_context_window_hint(None, config, env) is None
    assert resolve_context_window_hint("ollama/x", {}, {"OPENCODE_PROVIDER_OLLAMA_CONTEXT": "lots"}) is None


@pytest.mark.asyncio
async def test_declared_context_reaches_metadata(monkeypatch, tmp_path):
    runtime, _seen = _spawn_capture(monkeypatch, tmp_path, _step("hi"))
    # The hint is read from the turn's SPAWN env (the layers the child gets).
    spawn_env = {
        "OPENCODE_PROVIDER_OLLAMA_BASE_URL": "http://127.0.0.1:11434",
        "OPENCODE_PROVIDER_OLLAMA_CONTEXT": "16384",
    }
    monkeypatch.setattr(
        opencode_runtime, "build_execution_env",
        lambda extra=None, **kw: {**spawn_env, **(extra or {})},
    )
    _r, _l, metadata, _s = await runtime.execute_headless("hi", model="ollama/gemma4:12b")
    assert metadata.context_window == 16384


@pytest.mark.asyncio
async def test_images_are_attached_with_dash_f(monkeypatch, tmp_path):
    import base64

    png = b"\x89PNG\r\n\x1a\nfake"
    runtime, seen = _spawn_capture(monkeypatch, tmp_path, _step("a red square"))
    captured = {}
    real_cleanup = opencode_runtime._cleanup_turn_dir

    def _keep(turn_dir):
        cmd = seen[0]
        path = cmd[cmd.index("-f") + 1]
        with open(path, "rb") as fh:
            captured["bytes"] = fh.read()
        real_cleanup(turn_dir)

    monkeypatch.setattr(opencode_runtime, "_cleanup_turn_dir", _keep)
    response, *_ = await runtime.execute_headless(
        "describe the image", model="anthropic/claude-sonnet-5",
        images=[
            {"media_type": "image/png", "data": base64.b64encode(png).decode()},
            {"media_type": "image/png", "data": "!!not base64!!"},
            {"media_type": "application/pdf", "data": base64.b64encode(b"x").decode()},
        ],
    )
    assert response == "a red square"
    cmd = seen[0]
    assert cmd.count("-f") == 1  # the two unusable entries are skipped
    assert cmd[cmd.index("-f") + 1].endswith(".png")
    assert cmd.index("-f") < cmd.index("--")  # flags before the separator
    assert captured["bytes"] == png
