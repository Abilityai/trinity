"""The template-declared provider block in an OpenCode turn (#2981, checkpoint C).

Rulings T4/T5 made concrete:

  * the block renders as ``provider.<id>.{npm, options:{baseURL, apiKey}, models}``
    with ``apiKey`` as the ``{env:NAME}`` REFERENCE — the key's value is set in
    the test environment and must appear nowhere in the serialized config;
  * every validation rule fails NAMED (the field is in the message): id shape,
    the npm allowlist, the http(s) scheme, the env-var-name shape, the models
    mapping;
  * a missing key fails the turn 503 naming the variable BEFORE the subprocess
    starts — for the custom block, OpenRouter, Anthropic and OpenAI — while any
    other models.dev provider is not pre-checked and the turn runs;
  * no block ⇒ no ``provider:`` key; OpenRouter renders no block either;
  * there is no gateway probing anywhere in the agent server (decision 5).

Subprocess stubbed as in ``test_opencode_runtime.py``; no binary, no docker.
"""

from __future__ import annotations

import io
import json
import re
from pathlib import Path

import pytest
from fastapi import HTTPException

# conftest wires the real docker/base-image/agent_server as `agent_server`.
from agent_server.services import opencode_runtime  # noqa: E402
from agent_server.services.opencode_mcp import ENV_MCP_CONFIG_PATH  # noqa: E402
from agent_server.services.opencode_provider import (  # noqa: E402
    ENV_PROVIDER,
    NATIVE_KEY_ENV,
    NPM_ALLOWLIST,
    OpenCodeProviderConfigError,
    load_provider_block,
    missing_key_env,
    provider_block,
    render_provider_config,
    required_key_env,
    validate_provider_block,
)
from agent_server.services.opencode_runtime import OpenCodeRuntime  # noqa: E402

_SERVICES = Path(opencode_runtime.__file__).resolve().parent
_AGENT_SERVER = _SERVICES.parent

SENTINEL_KEY = "sk-local-SENTINEL-9f8e7d6c5b4a3210"


def _block(**overrides) -> dict:
    block = {
        "id": "local",
        "npm": "@ai-sdk/openai-compatible",
        "base_url": "https://inference.example.test/v1",
        "api_key_env": "LOCAL_API_KEY",
        "models": {"qwen3:27b": {}, "glm-4.6": {"cost": {"input": 0.1, "output": 0.2}}},
    }
    block.update(overrides)
    return block


def _set_block(monkeypatch, block: dict | None) -> None:
    if block is None:
        monkeypatch.delenv(ENV_PROVIDER, raising=False)
    else:
        monkeypatch.setenv(ENV_PROVIDER, json.dumps(block))


# ---------------------------------------------------------------------------
# render shape
# ---------------------------------------------------------------------------

def test_block_renders_the_opencode_provider_shape():
    config: dict = {}
    render_provider_config(config, validate_provider_block(_block()))
    assert config == {
        "provider": {
            "local": {
                "npm": "@ai-sdk/openai-compatible",
                "options": {
                    "baseURL": "https://inference.example.test/v1",
                    "apiKey": "{env:LOCAL_API_KEY}",
                },
                "models": {
                    "qwen3:27b": {},
                    "glm-4.6": {"cost": {"input": 0.1, "output": 0.2}},
                },
            }
        }
    }


def test_api_key_is_the_env_reference_never_the_value(monkeypatch):
    """The key IS set in the environment; the rendered text must carry its
    NAME and nothing else (decision 6)."""
    monkeypatch.setenv("LOCAL_API_KEY", SENTINEL_KEY)
    _set_block(monkeypatch, _block())
    config: dict = {}
    render_provider_config(config, provider_block())
    serialized = json.dumps(config)
    assert SENTINEL_KEY not in serialized
    assert config["provider"]["local"]["options"]["apiKey"] == "{env:LOCAL_API_KEY}"


def test_no_block_renders_no_provider_key(monkeypatch):
    _set_block(monkeypatch, None)
    config: dict = {"share": "disabled"}
    assert provider_block() is None
    render_provider_config(config, None)
    assert "provider" not in config


def test_operator_declared_provider_wins_and_models_are_unioned():
    """The shared config's `provider.local` keeps its npm/options; the
    template's models are unioned so the pinned model is never missing."""
    config: dict = {
        "provider": {
            "local": {
                "npm": "@ai-sdk/openai-compatible",
                "options": {"baseURL": "https://operator.example.test/v1", "apiKey": "{env:OP_KEY}"},
                "models": {"operator-model": {}},
            }
        }
    }
    render_provider_config(config, validate_provider_block(_block()))
    local = config["provider"]["local"]
    assert local["options"]["baseURL"] == "https://operator.example.test/v1"
    assert local["options"]["apiKey"] == "{env:OP_KEY}"
    assert set(local["models"]) == {"operator-model", "qwen3:27b", "glm-4.6"}


# ---------------------------------------------------------------------------
# validation — every failure names its field
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "overrides,field",
    [
        ({"id": "Local"}, "id"),
        ({"id": "my provider"}, "id"),
        ({"id": ""}, "id"),
        ({"id": None}, "id"),
        ({"npm": "@ai-sdk/anthropic"}, "npm"),
        ({"npm": "openai-compatible"}, "npm"),
        ({"npm": None}, "npm"),
        ({"base_url": "ftp://inference.example.test/v1"}, "base_url"),
        ({"base_url": "inference.example.test/v1"}, "base_url"),
        ({"base_url": "/v1"}, "base_url"),
        ({"base_url": "file:///etc/passwd"}, "base_url"),
        ({"base_url": None}, "base_url"),
        ({"api_key_env": "local_api_key"}, "api_key_env"),
        ({"api_key_env": "1KEY"}, "api_key_env"),
        ({"api_key_env": "KEY-NAME"}, "api_key_env"),
        ({"api_key_env": None}, "api_key_env"),
        ({"models": ["qwen3:27b"]}, "models"),
        ({"models": None}, "models"),
        ({"models": {"qwen3:27b": "free"}}, "models"),
        ({"models": {"": {}}}, "models"),
    ],
)
def test_each_validation_failure_names_its_field(overrides, field):
    with pytest.raises(OpenCodeProviderConfigError) as exc:
        validate_provider_block(_block(**overrides))
    assert f"runtime.provider.{field}" in str(exc.value), str(exc.value)


def test_npm_allowlist_is_exactly_the_openai_compatible_driver():
    assert NPM_ALLOWLIST == frozenset({"@ai-sdk/openai-compatible"})
    with pytest.raises(OpenCodeProviderConfigError, match="npm"):
        validate_provider_block(_block(npm="@ai-sdk/openai"))


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_http_and_https_are_both_accepted(scheme):
    block = validate_provider_block(_block(base_url=f"{scheme}://inference.example.test/v1"))
    assert block["base_url"].startswith(scheme + "://")


def test_null_model_spec_normalizes_to_an_empty_object():
    block = validate_provider_block(_block(models={"qwen3:27b": None}))
    assert block["models"] == {"qwen3:27b": {}}


def test_malformed_env_json_is_a_named_error(monkeypatch):
    monkeypatch.setenv(ENV_PROVIDER, "{not json")
    with pytest.raises(OpenCodeProviderConfigError, match=ENV_PROVIDER):
        load_provider_block()
    monkeypatch.setenv(ENV_PROVIDER, "[1, 2]")
    with pytest.raises(OpenCodeProviderConfigError, match="object"):
        load_provider_block()


def test_cost_policy_reads_through_the_one_reader(monkeypatch, caplog):
    """B's cost policy and this module parse AGENT_RUNTIME_PROVIDER through a
    single function: a malformed value degrades to None for the cost path
    (one warning) and the shape B keys on is unchanged."""
    import logging

    monkeypatch.setenv(ENV_PROVIDER, "{not json")
    with caplog.at_level(logging.WARNING, logger=opencode_runtime.logger.name):
        assert opencode_runtime._custom_provider_block() is None
    assert any(ENV_PROVIDER in r.getMessage() for r in caplog.records)

    _set_block(monkeypatch, _block())
    block = opencode_runtime._custom_provider_block()
    assert block["id"] == "local"
    assert block["models"]["glm-4.6"]["cost"] == {"input": 0.1, "output": 0.2}
    assert opencode_runtime._PROVIDER_ENV == ENV_PROVIDER


# ---------------------------------------------------------------------------
# key presence (ruling T4)
# ---------------------------------------------------------------------------

def test_required_key_env_table():
    block = validate_provider_block(_block())
    assert required_key_env("local/qwen3:27b", block) == "LOCAL_API_KEY"
    assert required_key_env("openrouter/qwen/qwen3-coder", block) == "OPENROUTER_API_KEY"
    assert required_key_env("anthropic/claude-sonnet-5", None) == "ANTHROPIC_API_KEY"
    assert required_key_env("openai/gpt-5", None) == "OPENAI_API_KEY"
    assert required_key_env("google/gemini-2.5-pro", block) is None
    assert required_key_env(None, block) is None
    assert NATIVE_KEY_ENV == {
        "openrouter": "OPENROUTER_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "openai": "OPENAI_API_KEY",
    }


@pytest.mark.parametrize("value", [None, "", "   "])
def test_unset_or_empty_key_is_reported_by_name(value):
    environ = {} if value is None else {"OPENROUTER_API_KEY": value}
    assert missing_key_env("openrouter/x/y", None, environ) == "OPENROUTER_API_KEY"
    assert missing_key_env("openrouter/x/y", None, {"OPENROUTER_API_KEY": "k"}) is None


# -- the turn boundary ------------------------------------------------------

def _install_fake_subprocess(monkeypatch, tmp_path):
    """A fake Popen replaying one complete step; records argv and the child
    env so a test can assert the spawn happened (or did not)."""

    class _FakeProcess:
        def __init__(self):
            lines = [
                {"type": "step_start", "sessionID": "ses_1"},
                {"type": "text", "sessionID": "ses_1", "part": {"text": "done"}},
                {"type": "step_finish", "sessionID": "ses_1", "part": {"cost": 0.01}},
            ]
            self.stdout = io.StringIO("".join(json.dumps(l) + "\n" for l in lines))
            self.stderr = io.StringIO("")
            self.pid = 4242
            self.returncode = 0

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

        def kill(self):
            pass

        def terminate(self):
            pass

    spawns: list = []

    def _popen(cmd, *a, **k):
        spawns.append({"argv": list(cmd), "env": dict(k.get("env") or {})})
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
        stdout_thread.join(5)
        stderr_thread.join(5)

    monkeypatch.setattr(opencode_runtime, "_drain_bounded", _drain)
    monkeypatch.setattr(opencode_runtime, "_terminate_process_group", lambda *a, **k: None)
    monkeypatch.setattr(opencode_runtime, "_opencode_root", lambda: str(tmp_path))
    monkeypatch.setenv(ENV_MCP_CONFIG_PATH, str(tmp_path / "absent.mcp.json"))
    return spawns


def _runtime(monkeypatch) -> OpenCodeRuntime:
    runtime = OpenCodeRuntime()
    monkeypatch.setattr(runtime, "is_available", lambda: True)
    return runtime


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model,var,with_block",
    [
        ("local/qwen3:27b", "LOCAL_API_KEY", True),
        ("openrouter/qwen/qwen3-coder", "OPENROUTER_API_KEY", False),
        ("anthropic/claude-sonnet-5", "ANTHROPIC_API_KEY", False),
        ("openai/gpt-5", "OPENAI_API_KEY", False),
    ],
)
async def test_missing_key_fails_503_naming_the_variable_before_spawn(
    monkeypatch, tmp_path, model, var, with_block
):
    monkeypatch.delenv(var, raising=False)
    _set_block(monkeypatch, _block() if with_block else None)
    spawns = _install_fake_subprocess(monkeypatch, tmp_path)

    with pytest.raises(HTTPException) as exc:
        await _runtime(monkeypatch).execute_headless("task", model=model)

    assert exc.value.status_code == 503, exc.value.detail
    assert var in str(exc.value.detail)
    assert spawns == [], "the subprocess must not start without the key"
    assert not (tmp_path / "turns").exists(), "no turn dir is built for a refused turn"


@pytest.mark.asyncio
async def test_empty_key_is_missing_too(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    _set_block(monkeypatch, None)
    spawns = _install_fake_subprocess(monkeypatch, tmp_path)
    with pytest.raises(HTTPException) as exc:
        await _runtime(monkeypatch).execute_headless("task", model="openrouter/x/y")
    assert exc.value.status_code == 503
    assert "OPENROUTER_API_KEY" in str(exc.value.detail)
    assert spawns == []


@pytest.mark.asyncio
async def test_present_key_lets_the_turn_run(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCAL_API_KEY", SENTINEL_KEY)
    _set_block(monkeypatch, _block())
    spawns = _install_fake_subprocess(monkeypatch, tmp_path)
    text, _log, metadata, _sid = await _runtime(monkeypatch).execute_headless(
        "task", model="local/qwen3:27b"
    )
    assert text == "done"
    assert len(spawns) == 1
    assert metadata.model_name == "local/qwen3:27b"


@pytest.mark.asyncio
async def test_other_models_dev_provider_is_not_pre_checked(monkeypatch, tmp_path):
    """Its auth failure, if any, arrives as a stream `error` event (B
    classifies it 503); there is no key table row to check against."""
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    _set_block(monkeypatch, None)
    spawns = _install_fake_subprocess(monkeypatch, tmp_path)
    text, *_ = await _runtime(monkeypatch).execute_headless(
        "task", model="google/gemini-2.5-pro"
    )
    assert text == "done"
    assert len(spawns) == 1


@pytest.mark.asyncio
async def test_openrouter_native_path_renders_no_provider_block(monkeypatch, tmp_path):
    """OpenCode knows OpenRouter from models.dev: only the key presence is
    checked, and the turn config carries no `provider:` key."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-SENTINEL-0123456789abcdef")
    _set_block(monkeypatch, None)
    spawns = _install_fake_subprocess(monkeypatch, tmp_path)
    written: list = []
    real_dump = opencode_runtime.json.dump

    def _capture(obj, fh, **kw):
        written.append(json.loads(json.dumps(obj)))
        return real_dump(obj, fh, **kw)

    monkeypatch.setattr(opencode_runtime.json, "dump", _capture)
    await _runtime(monkeypatch).execute_headless("task", model="openrouter/qwen/qwen3-coder")
    assert len(spawns) == 1
    config = written[-1]
    assert "provider" not in config
    assert "or-SENTINEL" not in json.dumps(config)


@pytest.mark.asyncio
async def test_malformed_block_is_a_400_naming_the_field_not_a_failed_turn(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCAL_API_KEY", SENTINEL_KEY)
    _set_block(monkeypatch, _block(base_url="ftp://inference.example.test"))
    spawns = _install_fake_subprocess(monkeypatch, tmp_path)
    with pytest.raises(HTTPException) as exc:
        await _runtime(monkeypatch).execute_headless("task", model="local/qwen3:27b")
    assert exc.value.status_code == 400
    assert "runtime.provider.base_url" in str(exc.value.detail)
    assert spawns == []


@pytest.mark.asyncio
async def test_chat_path_maps_a_malformed_block_to_400_too(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCAL_API_KEY", SENTINEL_KEY)
    _set_block(monkeypatch, _block(npm="@ai-sdk/anthropic"))
    _install_fake_subprocess(monkeypatch, tmp_path)
    with pytest.raises(HTTPException) as exc:
        await _runtime(monkeypatch).execute("hello", model="local/qwen3:27b")
    assert exc.value.status_code == 400
    assert "runtime.provider.npm" in str(exc.value.detail)


# ---------------------------------------------------------------------------
# decision 5: no gateway probing, no host discovery
# ---------------------------------------------------------------------------

_GATEWAY_RE = re.compile(
    r"proc/net/route|derive_gateway|read_default_gateway|OPENCODE_OLLAMA|PROC_NET_ROUTE"
)


def test_no_gateway_code_anywhere_in_the_agent_server():
    """Static, over the whole agent server tree: the lab fork's Ollama-only
    provider derived its base URL from the container's default gateway. That
    is OUT (decision 5) — the URL is what the template says it is."""
    hits = []
    for path in _AGENT_SERVER.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in _GATEWAY_RE.finditer(text):
            hits.append(f"{path.relative_to(_AGENT_SERVER)}: {match.group(0)}")
    assert hits == [], hits


def test_provider_module_does_no_network_or_host_discovery():
    source = (_SERVICES / "opencode_provider.py").read_text(encoding="utf-8")
    for forbidden in ("import socket", "import struct", "/proc/", "host.docker.internal\""):
        assert forbidden not in source, forbidden
    # The one mention of a host-gateway address is the docstring example.
    assert source.count("host.docker.internal") <= 1
