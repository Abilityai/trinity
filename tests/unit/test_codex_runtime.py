"""Unit tests for the Codex runtime (#1187).

Covers the pure, deterministic pieces of ``codex_runtime.py``:

  * ``calculate_codex_cost`` — token→cost with cached-input pricing and the
    "reasoning is a subset of output, don't double-count" rule.
  * ``parse_codex_jsonl`` — Codex ``--json`` event stream → ExecutionMetadata,
    response text, tool activity log, and error classification.
  * ``_finalize_codex_response`` — the ``-o`` output file is authoritative; the
    JSONL ``agent_message`` is only a fallback.
  * ``CodexRuntime.capabilities`` — the conservative-but-honest capability flags.
  * error→HTTP mapping in ``execute_headless`` — a dropped subprocess pipe is a
    502 (NOT 503, which collides with the SUB-003 auth switch); a generic
    runtime failure is a 500 (NOT 503 — it must not be read as AUTH).

The async subprocess itself is stubbed; these tests never spawn ``codex``.
"""

from __future__ import annotations

import json
import logging
import os

import pytest
from fastapi import HTTPException

# conftest wires the real docker/base-image/agent_server as `agent_server`.
from agent_server.services import codex_runtime  # noqa: E402
from agent_server.services.codex_runtime import (  # noqa: E402
    CodexRuntime,
    calculate_codex_cost,
    parse_codex_jsonl,
    _finalize_codex_response,
)
from agent_server.services.runtime_adapter import RuntimeCapabilities  # noqa: E402


# ---------------------------------------------------------------------------
# calculate_codex_cost
# ---------------------------------------------------------------------------

def test_calculate_codex_cost_no_reasoning_double_count():
    """reasoning_output_tokens is a SUBSET of output_tokens — cost must be
    output_tokens * rate, never output_tokens + reasoning_output_tokens."""
    model = "gpt-5.1-codex"
    rates = codex_runtime.CODEX_PRICING[model]

    # 0 cached so the input side is unambiguous.
    cost = calculate_codex_cost(
        input_tokens=1000,
        cached_input_tokens=0,
        output_tokens=500,  # of which 300 were reasoning
        model=model,
    )
    expected = (1000 / 1000) * rates["input"] + (500 / 1000) * rates["output"]
    assert cost == pytest.approx(round(expected, 6))

    # Sanity: a (buggy) double-count would be strictly larger.
    double_counted = (1000 / 1000) * rates["input"] + (
        (500 + 300) / 1000
    ) * rates["output"]
    assert cost < double_counted


def test_calculate_codex_cost_cached_pricing():
    """Cached input tokens bill at the cheaper cached rate; only the
    uncached remainder bills at the full input rate."""
    model = "gpt-5.1-codex"
    rates = codex_runtime.CODEX_PRICING[model]

    cost = calculate_codex_cost(
        input_tokens=1000,
        cached_input_tokens=400,
        output_tokens=0,
        model=model,
    )
    expected = (600 / 1000) * rates["input"] + (400 / 1000) * rates["cached"]
    assert cost == pytest.approx(round(expected, 6))
    # Cached must be cheaper than billing all 1000 at the full input rate.
    assert cost < (1000 / 1000) * rates["input"]


def test_calculate_codex_cost_default_fallback():
    """An unknown model falls back to the 'default' pricing, never KeyErrors."""
    cost = calculate_codex_cost(
        input_tokens=1000,
        cached_input_tokens=0,
        output_tokens=1000,
        model="some-future-model-x",
    )
    rates = codex_runtime.CODEX_PRICING["default"]
    expected = (1000 / 1000) * rates["input"] + (1000 / 1000) * rates["output"]
    assert cost == pytest.approx(round(expected, 6))


def test_calculate_codex_cost_cached_never_exceeds_input():
    """Defensive: cached_input_tokens > input_tokens must not produce a
    negative uncached charge."""
    cost = calculate_codex_cost(
        input_tokens=100,
        cached_input_tokens=500,  # nonsensical, but must not go negative
        output_tokens=0,
        model="gpt-5.1-codex",
    )
    assert cost >= 0


# ---------------------------------------------------------------------------
# parse_codex_jsonl
# ---------------------------------------------------------------------------

def _events_to_lines(events) -> list[str]:
    return [json.dumps(e) for e in events]


def test_codex_runtime_parser_thread_and_usage():
    """thread.started → session_id; turn.completed.usage → tokens + cost
    (cached → cache_read_tokens; reasoning NOT double-counted)."""
    events = [
        {"type": "thread.started", "thread_id": "thr_abc123"},
        {"type": "turn.started"},
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 2000,
                "cached_input_tokens": 500,
                "output_tokens": 800,
                "reasoning_output_tokens": 300,
            },
        },
    ]
    response, log, metadata, raw = parse_codex_jsonl(
        _events_to_lines(events), model="gpt-5.1-codex"
    )

    assert metadata.session_id == "thr_abc123"
    assert metadata.input_tokens == 2000
    assert metadata.output_tokens == 800
    assert metadata.cache_read_tokens == 500
    assert metadata.cost_usd is not None and metadata.cost_usd > 0
    # raw transcript captured every line
    assert len(raw) == len(events)


def test_codex_runtime_parser_agent_message_response():
    """item.completed/agent_message text becomes the response (JSONL fallback)."""
    events = [
        {"type": "thread.started", "thread_id": "thr_x"},
        {
            "type": "item.completed",
            "item": {"id": "item_1", "type": "agent_message", "text": "Hello world"},
        },
        {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}},
    ]
    response, log, metadata, raw = parse_codex_jsonl(_events_to_lines(events))
    assert "Hello world" in response


def test_codex_runtime_parser_tool_items():
    """command_execution / web_search items produce tool activity log entries
    and bump the tool count."""
    events = [
        {"type": "thread.started", "thread_id": "thr_y"},
        {
            "type": "item.completed",
            "item": {
                "id": "cmd_1",
                "type": "command_execution",
                "command": "ls -la",
                "exit_code": 0,
                "status": "completed",
                "aggregated_output": "a\nb\n",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "id": "ws_1",
                "type": "web_search",
                "query": "trinity agent platform",
            },
        },
        {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
    ]
    response, log, metadata, raw = parse_codex_jsonl(_events_to_lines(events))
    tool_uses = [e for e in log if e.type == "tool_use"]
    assert len(tool_uses) >= 2
    tools = {e.tool for e in tool_uses}
    # command_execution maps to a shell-ish tool; web_search to a search tool
    assert any("ash" in t or "hell" in t or "ommand" in t for t in tools)
    assert any("earch" in t for t in tools)


def test_codex_runtime_parser_turn_failed():
    """turn.failed → error_type/error_message captured on metadata."""
    events = [
        {"type": "thread.started", "thread_id": "thr_z"},
        {"type": "turn.failed", "error": {"message": "model is overloaded"}},
    ]
    response, log, metadata, raw = parse_codex_jsonl(_events_to_lines(events))
    assert metadata.error_message and "overloaded" in metadata.error_message


def test_codex_runtime_parser_tolerates_garbage_lines():
    """A non-JSON line must not kill the parser — later events still parse."""
    lines = [
        "not json at all",
        json.dumps({"type": "thread.started", "thread_id": "thr_g"}),
        "{ partial",
        json.dumps(
            {"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 2}}
        ),
    ]
    response, log, metadata, raw = parse_codex_jsonl(lines)
    assert metadata.session_id == "thr_g"
    assert metadata.input_tokens == 5


# ---------------------------------------------------------------------------
# -o output file is authoritative
# ---------------------------------------------------------------------------

def test_finalize_response_prefers_output_file():
    """The -o file content (durable record) wins over JSONL-assembled parts."""
    assert (
        _finalize_codex_response("FROM FILE", ["from jsonl"]) == "FROM FILE"
    )


def test_finalize_response_falls_back_to_jsonl():
    """When the -o file is empty/missing, fall back to JSONL response parts."""
    assert _finalize_codex_response(None, ["part a", "part b"]) == "part a\npart b"
    assert _finalize_codex_response("", ["only jsonl"]) == "only jsonl"


# ---------------------------------------------------------------------------
# capabilities
# ---------------------------------------------------------------------------

def test_codex_capabilities():
    caps = CodexRuntime.capabilities()
    assert isinstance(caps, RuntimeCapabilities)
    assert caps.chat_continuity is True
    assert caps.session_tab_resume is False  # MVP: Session tab stays Claude/Gemini
    assert caps.mcp_support is True
    assert caps.cost_reporting == "estimated"  # no native cost → derived from tokens


# ---------------------------------------------------------------------------
# error → HTTP status mapping (execute_headless)
# ---------------------------------------------------------------------------

@pytest.fixture
def available_runtime(monkeypatch):
    """A CodexRuntime whose is_available() is forced True so the early 503
    availability guard doesn't fire."""
    rt = CodexRuntime()
    monkeypatch.setattr(rt, "is_available", lambda: True)
    return rt


@pytest.mark.asyncio
async def test_codex_runtime_pipe_drop(available_runtime, monkeypatch):
    """A BrokenPipeError from the subprocess collector → HTTP 502, NOT 503.
    503 collides with the SUB-003 auth-switch; an early child exit is not auth."""

    async def _raise_pipe(**_kw):
        raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setattr(available_runtime, "_execute_codex", _raise_pipe)

    with pytest.raises(HTTPException) as exc_info:
        await available_runtime.execute_headless(prompt="hello")
    assert exc_info.value.status_code == 502


@pytest.mark.asyncio
async def test_codex_generic_failure_not_auth(available_runtime, monkeypatch, caplog):
    """A generic runtime failure surfaces as 500 (runtime-unavailable), NEVER
    503 — so the backend never infers error_code=AUTH for a non-auth Codex
    failure (#1187 decision 3)."""

    async def _raise_runtime(**_kw):
        raise RuntimeError("codex blew up for an unrelated reason")

    monkeypatch.setattr(available_runtime, "_execute_codex", _raise_runtime)

    with pytest.raises(HTTPException) as exc_info:
        await available_runtime.execute_headless(prompt="hello")
    assert exc_info.value.status_code == 500
    assert exc_info.value.status_code != 503


@pytest.mark.asyncio
async def test_codex_unavailable_raises_503(monkeypatch):
    """When the codex CLI isn't installed, execute_headless fails fast 503."""
    rt = CodexRuntime()
    monkeypatch.setattr(rt, "is_available", lambda: False)
    with pytest.raises(HTTPException) as exc_info:
        await rt.execute_headless(prompt="hi")
    assert exc_info.value.status_code == 503


# ---------------------------------------------------------------------------
# I1 — _classify_codex_failure: AUTH detection must be anchored, not a bare
# substring match (a non-auth failure that merely contains "401" → 500, not 503)
# ---------------------------------------------------------------------------

from agent_server.models import ExecutionMetadata  # noqa: E402


def _classify(stderr: str = "", error_message=None, return_code: int = 1):
    meta = ExecutionMetadata()
    meta.error_message = error_message
    return codex_runtime._classify_codex_failure(return_code, stderr, meta)


def test_classify_bare_401_is_not_auth():
    """A non-auth failure whose output merely contains '401' (e.g. an upstream
    tool returning 401) must map to 500, NOT 503 — 503 is the backend AUTH
    signal the dispatch breaker counts (#1187 decision 3)."""
    status, _ = _classify(stderr="tool exited: upstream returned 401 Not Found")
    assert status == 500


def test_classify_bare_api_key_phrase_is_not_auth():
    """A failure mentioning 'api key' in passing (no auth verb) is not an auth
    failure — drop the over-broad bare 'api key' marker."""
    status, _ = _classify(stderr="please set the weather api key in your tool config")
    assert status == 500


def test_classify_real_unauthorized_is_auth():
    status, detail = _classify(stderr="Error: 401 Unauthorized")
    assert status == 503
    assert "OPENAI_API_KEY" in detail


def test_classify_invalid_api_key_is_auth():
    status, _ = _classify(error_message="invalid_api_key: the provided key is wrong")
    assert status == 503


def test_classify_rate_limit_still_429():
    status, _ = _classify(stderr="429 Too Many Requests: rate limit exceeded")
    assert status == 429


def test_classify_generic_failure_is_500():
    status, _ = _classify(stderr="segfault in tool runner", return_code=139)
    assert status == 500


# ---------------------------------------------------------------------------
# I3 — end-of-options separator: the built command ends with "--" so a prompt
# starting with "-"/"--" is parsed as the positional prompt, never as a flag.
# ---------------------------------------------------------------------------

def test_build_command_ends_with_end_of_options_separator():
    rt = CodexRuntime()
    cmd = rt._build_codex_command(
        model="gpt-5.1-codex",
        sandbox_mode="danger-full-access",
        result_file="/tmp/out.txt",
        agent_home="/home/developer",
        resume_thread_id=None,
    )
    # The positional prompt is appended by _execute_codex right after this list;
    # "--" must be the final token so the prompt can never be read as options.
    assert cmd[-1] == "--"


# ---------------------------------------------------------------------------
# F-TOOLS (#1187 E2E): codex's own bubblewrap sandbox cannot create a user
# namespace inside the hardened Trinity container ("bwrap: No permissions to
# create a new namespace"), which blocks EVERY shell tool. Normal (writable)
# agents must therefore run with --sandbox danger-full-access (no internal
# sandbox; the Trinity container is the boundary, same posture as Claude/Gemini).
# Read-only mode keeps --sandbox read-only.
# ---------------------------------------------------------------------------

def test_resolve_sandbox_normal_is_danger_full_access(monkeypatch):
    """Normal (writable) mode maps to danger-full-access so codex skips its own
    bwrap sandbox (which can't run in the hardened container)."""
    monkeypatch.setattr(codex_runtime, "_is_read_only", lambda: False)
    assert codex_runtime._resolve_sandbox_mode() == "danger-full-access"


def test_resolve_sandbox_read_only_stays_read_only(monkeypatch):
    """Read-only mode keeps the sandbox-native read-only enforcement."""
    monkeypatch.setattr(codex_runtime, "_is_read_only", lambda: True)
    assert codex_runtime._resolve_sandbox_mode() == "read-only"


def test_build_command_normal_mode_has_no_network_access_override():
    """danger-full-access already permits network — the old
    `-c sandbox_workspace_write.network_access=true` override is dead and must
    not be emitted (it would be ignored at best, confusing at worst)."""
    rt = CodexRuntime()
    cmd = rt._build_codex_command(
        model="gpt-5.1-codex",
        sandbox_mode="danger-full-access",
        result_file="/tmp/out.txt",
        agent_home="/home/developer",
        resume_thread_id=None,
    )
    joined = " ".join(cmd)
    assert "danger-full-access" in cmd
    assert "network_access" not in joined
    assert "sandbox_workspace_write" not in joined


def test_build_command_resume_still_ends_with_separator():
    rt = CodexRuntime()
    cmd = rt._build_codex_command(
        model=None,
        sandbox_mode="read-only",
        result_file="/tmp/out.txt",
        agent_home="/home/developer",
        resume_thread_id="thr_123",
    )
    assert "resume" in cmd and "thr_123" in cmd
    assert cmd[-1] == "--"
    # Arg ORDER is load-bearing: the `resume` sub-subcommand (codex 0.139.0) has
    # a narrower option set and rejects exec-level flags that trail it
    # ("unexpected argument '-C' found", exit 2). They MUST precede `resume`, and
    # `resume <id>` must be the last tokens before the "--" separator. Guards the
    # turn-2+ continuity regression.
    resume_at = cmd.index("resume")
    for exec_flag in ("--json", "-C", "--sandbox", "-o", "--skip-git-repo-check"):
        assert cmd.index(exec_flag) < resume_at, f"{exec_flag} must precede resume"
    assert cmd[resume_at + 1] == "thr_123"
    assert cmd[-2:] == ["thr_123", "--"]


# ---------------------------------------------------------------------------
# I4 — thread-id fallback: when Codex emits no thread.started, degrade to a
# fresh next turn (None), never a fabricated id that would wedge resume.
# ---------------------------------------------------------------------------

def test_resolve_session_id_none_when_thread_missing():
    meta = ExecutionMetadata()  # session_id stays None
    assert codex_runtime._resolve_returned_session_id(meta) is None


def test_resolve_session_id_is_thread_id_when_present():
    meta = ExecutionMetadata()
    meta.session_id = "thr_real"
    assert codex_runtime._resolve_returned_session_id(meta) == "thr_real"


@pytest.mark.asyncio
async def test_chat_does_not_cache_fake_thread_id(available_runtime, monkeypatch):
    """A chat turn that returns no thread id must leave _chat_thread_id None so
    the next turn runs fresh rather than `codex exec resume <fake-uuid>`."""

    async def _fake(**_kw):
        return ("resp", [], ExecutionMetadata(), [], None)

    monkeypatch.setattr(available_runtime, "_execute_codex", _fake)
    await available_runtime.execute("hello")
    assert available_runtime._chat_thread_id is None


# ---------------------------------------------------------------------------
# I5 — .env key parser tolerates `export ` prefix and inline comments.
# ---------------------------------------------------------------------------

def _write_env(tmp_path, monkeypatch, contents: str):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    monkeypatch.setattr(codex_runtime, "_AGENT_HOME", str(tmp_path))
    (tmp_path / ".env").write_text(contents)


def test_load_api_key_tolerates_export_prefix(tmp_path, monkeypatch):
    _write_env(tmp_path, monkeypatch, "export OPENAI_API_KEY=sk-exported\n")
    assert codex_runtime._load_openai_api_key() == "sk-exported"


def test_load_api_key_strips_inline_comment(tmp_path, monkeypatch):
    _write_env(tmp_path, monkeypatch, "OPENAI_API_KEY=sk-plain # primary key\n")
    assert codex_runtime._load_openai_api_key() == "sk-plain"


def test_load_api_key_quoted_value_keeps_hash(tmp_path, monkeypatch):
    """A '#' inside a quoted value is part of the value, not a comment."""
    _write_env(tmp_path, monkeypatch, 'OPENAI_API_KEY="sk-a#b"\n')
    assert codex_runtime._load_openai_api_key() == "sk-a#b"


def test_load_api_key_plain_still_works(tmp_path, monkeypatch):
    _write_env(tmp_path, monkeypatch, "CODEX_API_KEY=ck-123\n")
    assert codex_runtime._load_openai_api_key() == "ck-123"


# ---------------------------------------------------------------------------
# I2 — reader-executor selection: the concurrent /api/task path uses the
# unbounded default executor; the lock-serialized chat path keeps the bounded
# single-worker executor (parity with Claude's headless path).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_headless_requests_concurrent_reader(available_runtime, monkeypatch):
    captured = {}

    async def _fake(**kw):
        captured.update(kw)
        return ("r", [], ExecutionMetadata(), [], "thr")

    monkeypatch.setattr(available_runtime, "_execute_codex", _fake)
    await available_runtime.execute_headless(prompt="hi")
    assert captured.get("concurrent_reader") is True


@pytest.mark.asyncio
async def test_chat_requests_serialized_reader(available_runtime, monkeypatch):
    captured = {}

    async def _fake(**kw):
        captured.update(kw)
        return ("r", [], ExecutionMetadata(), [], "thr")

    monkeypatch.setattr(available_runtime, "_execute_codex", _fake)
    await available_runtime.execute("hi")
    assert captured.get("concurrent_reader") is False


# ---------------------------------------------------------------------------
# CSO #1187 finding 2 — the -o result filename is derived from execution_id;
# a '/' or '..' must never let it escape CODEX_HOME (defense-in-depth).
# ---------------------------------------------------------------------------

def test_safe_result_token_blocks_traversal(tmp_path):
    codex_home = str(tmp_path)
    for hostile in ("../../etc/passwd", "a/b/c", "..", "../sibling"):
        token = codex_runtime._safe_result_token(hostile)
        assert "/" not in token and "\\" not in token
        path = os.path.join(codex_home, f"{token}-last.txt")
        # The result file stays directly inside CODEX_HOME — no escape.
        assert os.path.dirname(os.path.realpath(path)) == os.path.realpath(codex_home)


def test_safe_result_token_passes_clean_id_through():
    clean = "exec_AbC-123_def.456"
    assert codex_runtime._safe_result_token(clean) == clean


def test_safe_result_token_never_empty():
    # A pathological all-separator id still yields a usable filename token.
    assert codex_runtime._safe_result_token("/") == "codex"
    assert codex_runtime._safe_result_token("") == "codex"


# ---------------------------------------------------------------------------
# CSO #1187 finding 3 — read-only detection: an absent config is the normal
# writable state (silent); a present-but-corrupt config fails OPEN but LOGS
# (parity with the Claude reference hook, which also fails open + logs).
# ---------------------------------------------------------------------------

def test_is_read_only_missing_file_is_silent(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(codex_runtime, "_READ_ONLY_CONFIG", tmp_path / "ro.json")
    caplog.set_level(logging.WARNING, logger="agent_server.services.codex_runtime")
    assert codex_runtime._is_read_only() is False
    assert not any("read-only config" in r.getMessage() for r in caplog.records)


def test_is_read_only_enabled_true(tmp_path, monkeypatch):
    cfg = tmp_path / "ro.json"
    cfg.write_text(json.dumps({"enabled": True}))
    monkeypatch.setattr(codex_runtime, "_READ_ONLY_CONFIG", cfg)
    assert codex_runtime._is_read_only() is True


def test_is_read_only_enabled_false(tmp_path, monkeypatch):
    cfg = tmp_path / "ro.json"
    cfg.write_text(json.dumps({"enabled": False}))
    monkeypatch.setattr(codex_runtime, "_READ_ONLY_CONFIG", cfg)
    assert codex_runtime._is_read_only() is False


def test_is_read_only_malformed_fails_open_and_logs(tmp_path, monkeypatch, caplog):
    cfg = tmp_path / "ro.json"
    cfg.write_text("{not valid json")
    monkeypatch.setattr(codex_runtime, "_READ_ONLY_CONFIG", cfg)
    caplog.set_level(logging.WARNING, logger="agent_server.services.codex_runtime")
    assert codex_runtime._is_read_only() is False
    assert any("malformed" in r.getMessage() for r in caplog.records)


def test_is_read_only_unreadable_fails_open_and_logs(tmp_path, monkeypatch, caplog):
    """A present-but-unreadable config (e.g. it is a directory → OSError, not
    FileNotFoundError) fails OPEN and LOGS — parity with the malformed path."""
    cfg = tmp_path / "ro_is_a_dir.json"
    cfg.mkdir()  # reading a directory as a file raises OSError (not FileNotFound)
    monkeypatch.setattr(codex_runtime, "_READ_ONLY_CONFIG", cfg)
    caplog.set_level(logging.WARNING, logger="agent_server.services.codex_runtime")
    assert codex_runtime._is_read_only() is False
    assert any("unreadable" in r.getMessage() for r in caplog.records)


# ===========================================================================
# Added coverage (#1187 follow-up): pure helpers, getters, parser branches,
# the execute() chat path, and the _execute_codex orchestration body — all
# previously exercised only indirectly or not at all.
# ===========================================================================

import threading  # noqa: E402
from pathlib import Path  # noqa: E402

from agent_server.services.codex_runtime import (  # noqa: E402
    _compose_prompt,
    _read_and_consume_result_file,
    _resolve_pricing,
    _safe_unlink,
)


# ---------------------------------------------------------------------------
# _resolve_pricing — longest-prefix match (the documented "gpt-5.1-codex-2025-xx
# resolves to the codex rate" behavior, distinct from exact-key and default).
# ---------------------------------------------------------------------------

def test_resolve_pricing_longest_prefix_match():
    """A versioned/suffixed model name resolves to its base model's rate via the
    longest-prefix branch, NOT the default fallback."""
    rates = _resolve_pricing("gpt-5.1-codex-2025-11-01")
    assert rates is codex_runtime.CODEX_PRICING["gpt-5.1-codex"]


def test_resolve_pricing_prefers_longest_prefix():
    """When several keys are prefixes, the LONGEST wins ('gpt-5-mini-2025' →
    the mini rate, not the broader 'gpt-5' rate)."""
    rates = _resolve_pricing("gpt-5-mini-2025-xx")
    assert rates is codex_runtime.CODEX_PRICING["gpt-5-mini"]


def test_resolve_pricing_none_and_unknown_use_default():
    assert _resolve_pricing(None) is codex_runtime.CODEX_PRICING["default"]
    assert _resolve_pricing("anthropic-claude") is codex_runtime.CODEX_PRICING["default"]


# ---------------------------------------------------------------------------
# _compose_prompt — Codex exec has no system-prompt flag, so the effective
# platform prompt is PREPENDED with a "---" separator; no system prompt passes
# the user message through unchanged.
# ---------------------------------------------------------------------------

def test_compose_prompt_prepends_system_prompt():
    out = _compose_prompt("PLATFORM RULES", "do the thing")
    assert out == "PLATFORM RULES\n\n---\n\ndo the thing"


def test_compose_prompt_passthrough_without_system_prompt():
    assert _compose_prompt(None, "just the user message") == "just the user message"
    assert _compose_prompt("", "user msg") == "user msg"


# ---------------------------------------------------------------------------
# _read_and_consume_result_file — reads the -o file; missing file → None.
# NOTE the read does NOT delete (deletion is the caller's finally).
# ---------------------------------------------------------------------------

def test_read_result_file_reads_content(tmp_path):
    f = tmp_path / "out.txt"
    f.write_text("the durable answer")
    assert _read_and_consume_result_file(str(f), str(tmp_path)) == "the durable answer"
    # The reader itself must not delete — finally owns deletion.
    assert f.exists()


def test_read_result_file_missing_returns_none(tmp_path):
    assert _read_and_consume_result_file(str(tmp_path / "nope.txt"), str(tmp_path)) is None


def test_safe_unlink_removes_and_tolerates_missing(tmp_path):
    f = tmp_path / "gone.txt"
    f.write_text("x")
    _safe_unlink(str(f), str(tmp_path))
    assert not f.exists()
    # Second unlink (already gone) must not raise.
    _safe_unlink(str(f), str(tmp_path))


def test_ensure_within_rejects_escape(tmp_path):
    """Sink-side containment guard: a path resolving outside ``base`` is
    rejected, and the two wrappers degrade safely (no read, no unlink) rather
    than touching the out-of-bounds file."""
    base = tmp_path / "codex_home"
    base.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")

    with pytest.raises(ValueError):
        codex_runtime._ensure_within(str(base), str(outside))

    # Reader refuses → returns None and leaves the file untouched.
    assert _read_and_consume_result_file(str(outside), str(base)) is None
    assert outside.exists()
    # Unlink refuses → no-op, must not raise, file still present.
    _safe_unlink(str(outside), str(base))
    assert outside.exists()


# ---------------------------------------------------------------------------
# _surface_unmapped_guardrails — Codex exec has no per-tool CLI toggle in the
# MVP, so disallowed tools are SURFACED (logged), never silently dropped.
# ---------------------------------------------------------------------------

def test_surface_unmapped_guardrails_logs_disallowed(monkeypatch, caplog):
    monkeypatch.setattr(
        codex_runtime, "_load_guardrails", lambda: {"disallowed_tools": ["Bash", "Write"]}
    )
    caplog.set_level(logging.WARNING, logger="agent_server.services.codex_runtime")
    # Must not raise — surfacing is best-effort.
    codex_runtime._surface_unmapped_guardrails(allowed_tools=None)
    msg = " ".join(r.getMessage() for r in caplog.records)
    assert "Bash" in msg and "Write" in msg


def test_surface_unmapped_guardrails_logs_allowed_tools(monkeypatch, caplog):
    monkeypatch.setattr(codex_runtime, "_load_guardrails", lambda: {})
    caplog.set_level(logging.INFO, logger="agent_server.services.codex_runtime")
    codex_runtime._surface_unmapped_guardrails(allowed_tools=["Read", "Grep"])
    assert any("allowed_tools" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# _load_openai_api_key — process env wins; otherwise parse .env; nothing
# anywhere → None (drives the upstream "key not configured" 503).
# ---------------------------------------------------------------------------

def test_load_api_key_process_env_wins(tmp_path, monkeypatch):
    """The container env var is the fast path — used before .env is even read."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    # Point _AGENT_HOME at an empty dir so a stray real .env can't interfere.
    monkeypatch.setattr(codex_runtime, "_AGENT_HOME", str(tmp_path))
    assert codex_runtime._load_openai_api_key() == "sk-from-env"


def test_load_api_key_skips_blank_and_comment_lines(tmp_path, monkeypatch):
    _write_env(
        tmp_path,
        monkeypatch,
        "# a comment\n\nNOISE_WITHOUT_EQUALS\nOPENAI_API_KEY=sk-after-noise\n",
    )
    assert codex_runtime._load_openai_api_key() == "sk-after-noise"


def test_load_api_key_none_when_absent_everywhere(tmp_path, monkeypatch):
    """No env var and no .env file → None (not a crash, not a sentinel)."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    monkeypatch.setattr(codex_runtime, "_AGENT_HOME", str(tmp_path))  # no .env here
    assert codex_runtime._load_openai_api_key() is None


# ---------------------------------------------------------------------------
# Trivial-but-load-bearing getters + is_available probe.
# ---------------------------------------------------------------------------

def test_get_default_model_and_context_window():
    rt = CodexRuntime()
    assert rt.get_default_model() == "gpt-5.1-codex"
    assert rt.get_context_window() == codex_runtime.CODEX_CONTEXT_WINDOW
    # Model arg is cosmetic for the window — always the GPT-5 input window.
    assert rt.get_context_window("gpt-5-nano") == codex_runtime.CODEX_CONTEXT_WINDOW


def test_is_available_true_when_version_succeeds(monkeypatch):
    class _OK:
        returncode = 0

    monkeypatch.setattr(codex_runtime.subprocess, "run", lambda *a, **k: _OK())
    assert CodexRuntime().is_available() is True


def test_is_available_false_on_nonzero_and_exception(monkeypatch):
    class _Fail:
        returncode = 1

    monkeypatch.setattr(codex_runtime.subprocess, "run", lambda *a, **k: _Fail())
    assert CodexRuntime().is_available() is False

    def _boom(*a, **k):
        raise FileNotFoundError("codex not installed")

    monkeypatch.setattr(codex_runtime.subprocess, "run", _boom)
    assert CodexRuntime().is_available() is False


def test_configure_mcp_delegates_to_trinity_writer(tmp_path, monkeypatch):
    """configure_mcp() routes to the shared Codex config writer, which writes
    $CODEX_HOME/config.toml."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    rt = CodexRuntime()
    assert rt.configure_mcp({"github": {"command": "npx"}}) is True
    assert (tmp_path / "config.toml").exists()


# ---------------------------------------------------------------------------
# parse_codex_jsonl — additional event/item shapes not covered by the base set:
# file_change + mcp_tool_call items, the mcp "server.tool" display name, started
# →completed dedup, tool failure, top-level + item-level error events, and the
# blank-line skip.
# ---------------------------------------------------------------------------

def test_parser_mcp_and_file_change_tools():
    events = [
        {"type": "thread.started", "thread_id": "thr_m"},
        {
            "type": "item.completed",
            "item": {
                "id": "mcp_1",
                "type": "mcp_tool_call",
                "server": "trinity",
                "tool": "list_agents",
                "arguments": {"q": "x"},
                "status": "completed",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "id": "fc_1",
                "type": "file_change",
                "changes": [{"path": "a.py", "kind": "modify"}],
                "status": "completed",
            },
        },
    ]
    response, log, metadata, raw = parse_codex_jsonl(_events_to_lines(events))
    tool_uses = {e.tool for e in log if e.type == "tool_use"}
    # mcp_tool_call with a server renders "server.tool".
    assert "trinity.list_agents" in tool_uses
    assert "FileChange" in tool_uses
    assert metadata.tool_count == 2


def test_parser_tool_started_then_completed_dedup():
    """An item.started followed by item.completed for the SAME tool id yields a
    single tool_use (dedup), plus one tool_result."""
    events = [
        {"type": "thread.started", "thread_id": "thr_d"},
        {
            "type": "item.started",
            "item": {"id": "cmd_dup", "type": "command_execution", "command": "ls"},
        },
        {
            "type": "item.completed",
            "item": {
                "id": "cmd_dup",
                "type": "command_execution",
                "command": "ls",
                "exit_code": 0,
                "status": "completed",
                "aggregated_output": "ok",
            },
        },
    ]
    response, log, metadata, raw = parse_codex_jsonl(_events_to_lines(events))
    tool_uses = [e for e in log if e.type == "tool_use"]
    tool_results = [e for e in log if e.type == "tool_result"]
    assert len(tool_uses) == 1  # not 2 — the started/completed pair is deduped
    assert len(tool_results) == 1
    assert tool_results[0].success is True


def test_parser_tool_failure_marks_result_unsuccessful():
    events = [
        {"type": "thread.started", "thread_id": "thr_f"},
        {
            "type": "item.completed",
            "item": {
                "id": "cmd_bad",
                "type": "command_execution",
                "command": "false",
                "exit_code": 1,
                "status": "failed",
            },
        },
    ]
    _, log, _, _ = parse_codex_jsonl(_events_to_lines(events))
    results = [e for e in log if e.type == "tool_result"]
    assert results and results[0].success is False


def test_parser_top_level_error_event():
    events = [
        {"type": "thread.started", "thread_id": "thr_e"},
        {"type": "error", "message": "stream aborted"},
    ]
    _, _, metadata, _ = parse_codex_jsonl(_events_to_lines(events))
    assert metadata.error_message == "stream aborted"


def test_parser_item_level_error():
    events = [
        {"type": "thread.started", "thread_id": "thr_ie"},
        {"type": "item.completed", "item": {"id": "x", "type": "error", "message": "boom"}},
    ]
    _, _, metadata, _ = parse_codex_jsonl(_events_to_lines(events))
    assert metadata.error_message == "boom"


def test_parser_skips_blank_lines():
    lines = ["", "   ", json.dumps({"type": "thread.started", "thread_id": "thr_b"})]
    _, _, metadata, raw = parse_codex_jsonl(lines)
    assert metadata.session_id == "thr_b"
    assert len(raw) == 1  # blank lines never reach raw_messages


# ---------------------------------------------------------------------------
# execute() — the interactive chat path (mirrors the execute_headless error
# mapping, plus continuity, the fresh-session reset, and the session rollups).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_execute_unavailable_raises_503(monkeypatch):
    rt = CodexRuntime()
    monkeypatch.setattr(rt, "is_available", lambda: False)
    with pytest.raises(HTTPException) as exc_info:
        await rt.execute("hi")
    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_execute_pipe_drop_is_502(available_runtime, monkeypatch):
    async def _raise_pipe(**_kw):
        raise ConnectionResetError("peer reset")

    monkeypatch.setattr(available_runtime, "_execute_codex", _raise_pipe)
    with pytest.raises(HTTPException) as exc_info:
        await available_runtime.execute("hi")
    assert exc_info.value.status_code == 502


@pytest.mark.asyncio
async def test_execute_generic_failure_is_500(available_runtime, monkeypatch):
    async def _raise_runtime(**_kw):
        raise RuntimeError("unrelated")

    monkeypatch.setattr(available_runtime, "_execute_codex", _raise_runtime)
    with pytest.raises(HTTPException) as exc_info:
        await available_runtime.execute("hi")
    assert exc_info.value.status_code == 500


@pytest.mark.asyncio
async def test_execute_continuity_uses_cached_thread_id(available_runtime, monkeypatch):
    """A continue_session turn with a prior thread id resumes it."""
    captured = {}

    async def _fake(**kw):
        captured.update(kw)
        return ("r", [], ExecutionMetadata(), [], "thr_next")

    monkeypatch.setattr(available_runtime, "_execute_codex", _fake)
    monkeypatch.setattr(codex_runtime.agent_state, "session_started", True)
    available_runtime._chat_thread_id = "thr_prev"

    await available_runtime.execute("hello", continue_session=True)
    assert captured.get("resume_thread_id") == "thr_prev"
    # The returned thread id is cached for the NEXT turn.
    assert available_runtime._chat_thread_id == "thr_next"


@pytest.mark.asyncio
async def test_execute_fresh_session_does_not_resume(available_runtime, monkeypatch):
    """Without continue_session the turn runs fresh (resume_thread_id None) and
    marks the session started."""
    captured = {}

    async def _fake(**kw):
        captured.update(kw)
        return ("r", [], ExecutionMetadata(), [], None)

    monkeypatch.setattr(available_runtime, "_execute_codex", _fake)
    available_runtime._chat_thread_id = "thr_stale"
    await available_runtime.execute("hello", continue_session=False)
    assert captured.get("resume_thread_id") is None
    assert codex_runtime.agent_state.session_started is True


@pytest.mark.asyncio
async def test_execute_updates_session_rollups(available_runtime, monkeypatch):
    """A successful chat turn folds cost/tokens/context into agent_state."""
    meta = ExecutionMetadata()
    meta.cost_usd = 0.05
    meta.output_tokens = 340
    meta.input_tokens = 9_000
    meta.context_window = codex_runtime.CODEX_CONTEXT_WINDOW

    async def _fake(**_kw):
        return ("answer", [], meta, [], "thr_roll")

    monkeypatch.setattr(available_runtime, "_execute_codex", _fake)
    # Force the context-tokens high-water comparison to take the update branch.
    monkeypatch.setattr(codex_runtime.agent_state, "session_context_tokens", 0)
    before_cost = codex_runtime.agent_state.session_total_cost
    before_out = codex_runtime.agent_state.session_total_output_tokens

    response, _, _, _ = await available_runtime.execute("hi")
    assert response == "answer"
    assert codex_runtime.agent_state.session_total_cost == pytest.approx(before_cost + 0.05)
    assert codex_runtime.agent_state.session_total_output_tokens == before_out + 340
    assert codex_runtime.agent_state.session_context_tokens == 9_000
    assert codex_runtime.agent_state.session_context_window == codex_runtime.CODEX_CONTEXT_WINDOW


# ---------------------------------------------------------------------------
# _execute_codex — the orchestration body, end-to-end, with subprocess.Popen
# stubbed. Never spawns the real codex CLI; the fake writes the -o result file
# and emits a JSONL event stream the real reader threads + parser consume.
# ---------------------------------------------------------------------------

class _FakePipe:
    """Minimal stdout/stderr stand-in: readline() yields each line then ''."""

    def __init__(self, lines):
        self._it = iter(lines)

    def readline(self):
        return next(self._it, "")

    def close(self):
        pass


class _FakeRegistry:
    def __init__(self):
        self.registered = []
        self.unregistered = []

    def register(self, execution_id, process, metadata=None):
        self.registered.append(execution_id)

    def unregister(self, execution_id):
        self.unregistered.append(execution_id)

    def publish_log_entry(self, execution_id, event):
        pass


def _install_fake_codex(
    monkeypatch,
    *,
    result_text,
    stdout_events,
    returncode=0,
    extra_raw_lines=(),
    wait_exc=None,
):
    """Wire codex_runtime so _execute_codex runs its real body against a fake
    subprocess. Returns the registry so the caller can assert register/unregister.

    ``extra_raw_lines`` are appended to stdout verbatim (un-JSON-encoded) to
    exercise the reader's malformed-line tolerance. ``wait_exc``, when set, is
    raised by ``process.wait`` to exercise the subprocess-timeout path.
    """
    monkeypatch.setattr(codex_runtime, "_load_openai_api_key", lambda: "sk-test")
    monkeypatch.setattr(codex_runtime, "_is_read_only", lambda: False)
    monkeypatch.setattr(codex_runtime, "_load_guardrails", lambda: {})
    # Neutralize OS-level process-group operations — there is no real pgid.
    monkeypatch.setattr(codex_runtime, "_capture_pgid", lambda proc: None)
    monkeypatch.setattr(codex_runtime, "_terminate_process_group", lambda *a, **k: None)

    async def _noop_bounded_close(*a, **k):
        return None

    monkeypatch.setattr(codex_runtime, "_bounded_safe_close_pipes", _noop_bounded_close)

    def _drain(process, t_out, t_err, grace=5, pgid=None, execution_tag=None):
        # Join the reader threads so parsed state is settled before we read it.
        t_out.join(timeout=5)
        t_err.join(timeout=5)

    monkeypatch.setattr(codex_runtime, "_drain_bounded", _drain)

    registry = _FakeRegistry()
    monkeypatch.setattr(codex_runtime, "get_process_registry", lambda: registry)

    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            self.cmd = cmd
            self.pid = 4242
            self.returncode = returncode
            # The real codex writes the -o file; emulate that for the happy path.
            if result_text is not None:
                oidx = cmd.index("-o")
                Path(cmd[oidx + 1]).write_text(result_text)
            self.stdout = _FakePipe(
                [json.dumps(e) + "\n" for e in stdout_events] + list(extra_raw_lines)
            )
            self.stderr = _FakePipe([])

        def wait(self, timeout=None):
            if wait_exc is not None:
                raise wait_exc
            return self.returncode

        def poll(self):
            return self.returncode

    monkeypatch.setattr(codex_runtime.subprocess, "Popen", _FakePopen)
    return registry


@pytest.mark.asyncio
async def test_execute_codex_body_happy_path(tmp_path, monkeypatch):
    """The -o file is the authoritative response; tokens/cost/session_id come
    from the JSONL stream; the result file is unlinked and the registry is
    register/unregister-balanced."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    events = [
        {"type": "thread.started", "thread_id": "thr_live_42"},
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 1200,
                "cached_input_tokens": 200,
                "output_tokens": 340,
            },
        },
    ]
    registry = _install_fake_codex(
        monkeypatch, result_text="FINAL ANSWER FROM -o FILE", stdout_events=events
    )

    rt = CodexRuntime()
    response, log, metadata, raw, session_id = await rt._execute_codex(
        prompt="do it",
        model="gpt-5.1-codex",
        system_prompt="PLATFORM",
        resume_thread_id=None,
        timeout_seconds=30,
        allowed_tools=None,
        execution_id="exec_body_1",
        concurrent_reader=True,
    )

    assert response == "FINAL ANSWER FROM -o FILE"
    assert metadata.input_tokens == 1200
    assert metadata.output_tokens == 340
    assert metadata.cache_read_tokens == 200
    assert metadata.cost_usd and metadata.cost_usd > 0
    assert metadata.status == "success"
    assert session_id == "thr_live_42"
    # finally: result file consumed, registry balanced.
    assert not (tmp_path / "codex" / "exec_body_1-last.txt").exists()
    assert registry.registered == ["exec_body_1"]
    assert registry.unregistered == ["exec_body_1"]


@pytest.mark.asyncio
async def test_execute_codex_body_nonzero_exit_classifies_failure(tmp_path, monkeypatch):
    """A non-zero exit routes through _classify_codex_failure → HTTPException,
    and the result file is still cleaned up in finally."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    registry = _install_fake_codex(
        monkeypatch,
        result_text=None,  # codex wrote nothing
        stdout_events=[{"type": "thread.started", "thread_id": "thr_x"}],
        returncode=1,
    )
    rt = CodexRuntime()
    with pytest.raises(HTTPException) as exc_info:
        await rt._execute_codex(
            prompt="boom",
            model=None,
            system_prompt=None,
            resume_thread_id=None,
            timeout_seconds=30,
            allowed_tools=None,
            execution_id="exec_body_2",
            concurrent_reader=False,
        )
    # Generic failure (no auth/rate markers) → 500, never 503.
    assert exc_info.value.status_code == 500
    assert registry.unregistered == ["exec_body_2"]


@pytest.mark.asyncio
async def test_execute_codex_body_missing_key_is_503(tmp_path, monkeypatch):
    """No API key resolvable → 503 before any subprocess is spawned."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setattr(codex_runtime, "_load_openai_api_key", lambda: None)
    rt = CodexRuntime()
    with pytest.raises(HTTPException) as exc_info:
        await rt._execute_codex(
            prompt="x",
            model=None,
            system_prompt=None,
            resume_thread_id=None,
            timeout_seconds=30,
            allowed_tools=None,
            execution_id="exec_body_3",
            concurrent_reader=False,
        )
    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_execute_headless_happy_path_through_real_body(tmp_path, monkeypatch):
    """execute_headless() drives the real _execute_codex body (concurrent reader)
    and returns the -o response + thread id."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setattr(CodexRuntime, "is_available", lambda self: True)
    _install_fake_codex(
        monkeypatch,
        result_text="HEADLESS RESULT",
        stdout_events=[
            {"type": "thread.started", "thread_id": "thr_headless"},
            {"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 2}},
        ],
    )
    rt = CodexRuntime()
    response, log, metadata, session_id = await rt.execute_headless(
        prompt="task", execution_id="exec_hl_1", timeout_seconds=30
    )
    assert response == "HEADLESS RESULT"
    assert session_id == "thr_headless"


@pytest.mark.asyncio
async def test_execute_headless_warns_on_images_and_max_turns(available_runtime, monkeypatch, caplog):
    """images are unsupported (warned + ignored) and max_turns has no CLI flag
    (info-logged), but neither aborts the run."""
    captured = {}

    async def _fake(**kw):
        captured.update(kw)
        return ("ok", [], ExecutionMetadata(), [], "thr")

    monkeypatch.setattr(available_runtime, "_execute_codex", _fake)
    caplog.set_level(logging.INFO, logger="agent_server.services.codex_runtime")
    await available_runtime.execute_headless(
        prompt="t", images=[{"data": "x"}], max_turns=5
    )
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "images are not supported" in text
    assert "max_turns" in text


# ---------------------------------------------------------------------------
# CODEX_HOME resolution — explicit env wins; else under $TMPDIR; else the home
# default. Kept out of the git-tracked repo so codex state never dirties sync.
# ---------------------------------------------------------------------------

def test_codex_home_prefers_explicit_env(monkeypatch):
    monkeypatch.setenv("CODEX_HOME", "/somewhere/codex-home")
    assert codex_runtime._codex_home() == "/somewhere/codex-home"


def test_codex_home_falls_back_to_tmpdir(monkeypatch):
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setenv("TMPDIR", "/scratch/tmp")
    assert codex_runtime._codex_home() == "/scratch/tmp/codex"


def test_codex_home_falls_back_to_agent_home_tmp(monkeypatch):
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("TMPDIR", raising=False)
    # _AGENT_HOME/.tmp/codex when neither env is set.
    assert codex_runtime._codex_home().endswith("/.tmp/codex")


def test_ensure_codex_home_creates_dir(tmp_path, monkeypatch):
    target = tmp_path / "ch"
    monkeypatch.setenv("CODEX_HOME", str(target))
    assert codex_runtime._ensure_codex_home() == str(target)
    assert target.is_dir()


# ---------------------------------------------------------------------------
# Parser/tracking resilience: an item with no resolvable type is skipped; a
# failure inside best-effort activity tracking never breaks parsing.
# ---------------------------------------------------------------------------

def test_parser_item_without_type_is_skipped():
    events = [
        {"type": "thread.started", "thread_id": "thr_nt"},
        {"type": "item.completed", "item": {"id": "x"}},  # no 'type' / details.type
        {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
    ]
    _, log, metadata, _ = parse_codex_jsonl(_events_to_lines(events))
    assert metadata.tool_count == 0
    assert not log


def test_parser_survives_activity_tracking_exceptions(monkeypatch):
    """start/complete_tool_execution are best-effort — a raise inside them is
    swallowed and the tool log entries are still recorded."""

    def _boom(*a, **k):
        raise RuntimeError("activity sink down")

    monkeypatch.setattr(codex_runtime, "start_tool_execution", _boom)
    monkeypatch.setattr(codex_runtime, "complete_tool_execution", _boom)
    events = [
        {"type": "thread.started", "thread_id": "thr_at"},
        {
            "type": "item.completed",
            "item": {
                "id": "cmd_at",
                "type": "command_execution",
                "command": "ls",
                "exit_code": 0,
                "status": "completed",
                "aggregated_output": "ok",
            },
        },
    ]
    _, log, _, _ = parse_codex_jsonl(_events_to_lines(events))  # must not raise
    assert any(e.type == "tool_use" for e in log)
    assert any(e.type == "tool_result" for e in log)


# ---------------------------------------------------------------------------
# _execute_codex — additional body branches: malformed stdout lines are
# tolerated; an empty -o file with no JSONL parts yields a sentinel response;
# a subprocess wall-clock timeout maps to 504.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_execute_codex_body_tolerates_malformed_stdout(tmp_path, monkeypatch):
    """A non-JSON line and a non-dict JSON line on stdout are skipped; the run
    still completes off the -o file."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    _install_fake_codex(
        monkeypatch,
        result_text="STILL FINE",
        stdout_events=[{"type": "thread.started", "thread_id": "thr_mal"}],
        extra_raw_lines=["this is not json\n", "12345\n"],  # garbage + non-dict
    )
    rt = CodexRuntime()
    response, _, _, _, _ = await rt._execute_codex(
        prompt="p", model=None, system_prompt=None, resume_thread_id=None,
        timeout_seconds=30, allowed_tools=None, execution_id="exec_mal",
        concurrent_reader=False,
    )
    assert response == "STILL FINE"


@pytest.mark.asyncio
async def test_execute_codex_body_empty_response_sentinel(tmp_path, monkeypatch):
    """Empty -o file + no agent_message parts + no tools → '(No response from
    Codex)' rather than an empty string."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    _install_fake_codex(
        monkeypatch,
        result_text="",  # codex produced an empty result file
        stdout_events=[
            {"type": "thread.started", "thread_id": "thr_empty"},
            {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 0}},
        ],
    )
    rt = CodexRuntime()
    response, _, _, _, _ = await rt._execute_codex(
        prompt="p", model=None, system_prompt=None, resume_thread_id=None,
        timeout_seconds=30, allowed_tools=None, execution_id="exec_empty",
        concurrent_reader=False,
    )
    assert response == "(No response from Codex)"


@pytest.mark.asyncio
async def test_execute_codex_body_subprocess_timeout_is_504(tmp_path, monkeypatch):
    """A subprocess wall-clock timeout (process.wait raises TimeoutExpired) maps
    to HTTP 504 and still unregisters the execution."""
    import subprocess as _sp

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    registry = _install_fake_codex(
        monkeypatch,
        result_text=None,
        stdout_events=[{"type": "thread.started", "thread_id": "thr_to"}],
        wait_exc=_sp.TimeoutExpired(cmd="codex", timeout=1),
    )
    rt = CodexRuntime()
    with pytest.raises(HTTPException) as exc_info:
        await rt._execute_codex(
            prompt="p", model=None, system_prompt=None, resume_thread_id=None,
            timeout_seconds=1, allowed_tools=None, execution_id="exec_to",
            concurrent_reader=False,
        )
    assert exc_info.value.status_code == 504
    assert registry.unregistered == ["exec_to"]


@pytest.mark.asyncio
async def test_execute_chat_timeout_maps_to_504(available_runtime, monkeypatch):
    """execute() maps a bare TimeoutError from the body to HTTP 504."""

    async def _raise_timeout(**_kw):
        raise TimeoutError("slow")

    monkeypatch.setattr(available_runtime, "_execute_codex", _raise_timeout)
    with pytest.raises(HTTPException) as exc_info:
        await available_runtime.execute("hi")
    assert exc_info.value.status_code == 504


@pytest.mark.asyncio
async def test_execute_headless_timeout_maps_to_504(available_runtime, monkeypatch):
    async def _raise_timeout(**_kw):
        raise TimeoutError("slow")

    monkeypatch.setattr(available_runtime, "_execute_codex", _raise_timeout)
    with pytest.raises(HTTPException) as exc_info:
        await available_runtime.execute_headless(prompt="hi")
    assert exc_info.value.status_code == 504
