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
    assert caps.session_tab_resume is False
    assert caps.mcp_support is True
    assert caps.cost_reporting == "native"


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
