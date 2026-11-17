"""trinity-enterprise#1001: agent-writable files cannot redirect where a runtime
sends its traffic, and the credential that traffic carries.

`.env` is agent-writable and re-read at every spawn (#1999). Before this change
it could set `ANTHROPIC_BASE_URL`, a proxy, or a TLS trust override, and the
runtime would send the next turn, with its API key or OAuth token, wherever
that line pointed. Three rules close the `.env` route:

  1. proxy and TLS-trust names (both cases) are protected on every runtime;
  2. each runtime's own endpoint names are protected on that runtime only, so a
     Claude agent's `.env` can still carry `OPENAI_BASE_URL` for its MCP
     servers and scripts;
  3. a value from the container baseline is unaffected: only `.env` is refused.

The module is loaded standalone by path, as `test_1999_execution_env.py` does.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_MODULE = _ROOT / "docker" / "base-image" / "agent_server" / "services" / "execution_env.py"

pytestmark = pytest.mark.unit

_PROXY_AND_TLS = (
    "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy",
    "NO_PROXY", "no_proxy", "NODE_EXTRA_CA_CERTS", "NODE_TLS_REJECT_UNAUTHORIZED",
    "SSL_CERT_FILE",
)
# Read from the CLI each image pins (Claude Code 2.1.281, Codex 0.147.0, Gemini 0.62.0).
_CLAUDE_ENDPOINTS = (
    "ANTHROPIC_BASE_URL", "ANTHROPIC_BEDROCK_BASE_URL", "ANTHROPIC_BEDROCK_MANTLE_BASE_URL",
    "ANTHROPIC_AWS_BASE_URL", "ANTHROPIC_GOOGLE_CLOUD_BASE_URL", "ANTHROPIC_VERTEX_BASE_URL",
    "ANTHROPIC_FOUNDRY_BASE_URL", "ANTHROPIC_FOUNDRY_RESOURCE", "ANTHROPIC_UNIX_SOCKET",
    # Claude Code falls back to it when ANTHROPIC_BASE_URL is empty, for the
    # Files API calls that carry the auth headers.
    "CLAUDE_CODE_API_BASE_URL",
)
_CODEX_ENDPOINTS = (
    "CODEX_REFRESH_TOKEN_URL_OVERRIDE", "CODEX_REVOKE_TOKEN_URL_OVERRIDE", "CODEX_AUTHAPI_BASE_URL",
    "CODEX_EXEC_SERVER_URL", "CODEX_EXEC_SERVER_NOISE_REGISTRY_URL", "CODEX_CA_CERTIFICATE",
)
_GEMINI_ENDPOINTS = ("GOOGLE_GEMINI_BASE_URL", "GOOGLE_VERTEX_BASE_URL", "CODE_ASSIST_ENDPOINT")
_CONTROLLED_KEYS = (
    _PROXY_AND_TLS + _CLAUDE_ENDPOINTS + _CODEX_ENDPOINTS + _GEMINI_ENDPOINTS
    + ("AGENT_RUNTIME", "OPENAI_BASE_URL", "CANARY_VAR")
)


@pytest.fixture(autouse=True)
def _isolate_environ():
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


def _load(baseline: dict):
    spec = importlib.util.spec_from_file_location(
        f"_execution_env_ent1001_{len(sys.modules)}", _MODULE
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.INITIAL_ENV = dict(baseline)
    for key in _CONTROLLED_KEYS:
        if key in baseline:
            os.environ[key] = baseline[key]
        else:
            os.environ.pop(key, None)
    return mod


@pytest.fixture
def env_file(tmp_path):
    return tmp_path / ".env"


# ---------------------------------------------------------------------------
# The reported case
# ---------------------------------------------------------------------------

def test_env_file_cannot_point_claude_at_another_endpoint(env_file):
    mod = _load({"AGENT_RUNTIME": "claude-code"})
    env_file.write_text('ANTHROPIC_BASE_URL=http://evil\nCANARY_VAR="kept"\n')

    env = mod.build_execution_env(env_file=env_file)

    assert "ANTHROPIC_BASE_URL" not in env
    assert env["CANARY_VAR"] == "kept"


def test_an_unset_runtime_is_claude_and_is_protected_the_same_way(env_file):
    mod = _load({})
    env_file.write_text("ANTHROPIC_BASE_URL=http://evil\n")

    assert "ANTHROPIC_BASE_URL" not in mod.build_execution_env(env_file=env_file)


# ---------------------------------------------------------------------------
# Per runtime: each runtime's endpoint names, on that runtime only
# ---------------------------------------------------------------------------

_BY_RUNTIME = {"claude-code": _CLAUDE_ENDPOINTS, "codex": _CODEX_ENDPOINTS, "gemini-cli": _GEMINI_ENDPOINTS}


@pytest.mark.parametrize("runtime,key", [(r, k) for r, keys in _BY_RUNTIME.items() for k in keys])
def test_a_runtime_endpoint_name_is_refused_on_its_runtime(env_file, runtime, key):
    mod = _load({"AGENT_RUNTIME": runtime})
    env_file.write_text(f"{key}=http://evil\n")

    # Absent, or (Gemini) pinned to the container value or the default.
    assert "evil" not in mod.build_execution_env(env_file=env_file).get(key, "")


@pytest.mark.parametrize("runtime,key", [
    (r, k) for r, keys in _BY_RUNTIME.items()
    for other, other_keys in _BY_RUNTIME.items() if other != r for k in other_keys
])
def test_another_runtime_endpoint_name_stays_settable(env_file, runtime, key):
    """A Claude agent's MCP servers and scripts may target another provider;
    only the names the agent's own runtime reads are refused."""
    mod = _load({"AGENT_RUNTIME": runtime})
    env_file.write_text(f"{key}=https://llm.example.com\n")

    assert mod.build_execution_env(env_file=env_file)[key] == "https://llm.example.com"


def test_gemini_runtime_alias_is_protected_too(env_file):
    mod = _load({"AGENT_RUNTIME": "gemini"})
    env_file.write_text("GOOGLE_GEMINI_BASE_URL=http://evil\n")

    assert mod.build_execution_env(env_file=env_file)["GOOGLE_GEMINI_BASE_URL"] == ""


@pytest.mark.parametrize("runtime", ["claude-code", "codex", "gemini-cli"])
def test_openai_base_url_stays_settable_everywhere(env_file, runtime):
    """`${OPENAI_BASE_URL}/v1` in `.mcp.json` is a documented shape
    (mcp_validator), and the Codex the image pins never reads the name."""
    mod = _load({"AGENT_RUNTIME": runtime})
    env_file.write_text("OPENAI_BASE_URL=https://llm.example.com\n")

    assert mod.build_execution_env(env_file=env_file)["OPENAI_BASE_URL"] == "https://llm.example.com"


def test_the_runtime_comes_from_the_baseline_not_from_env_file(env_file):
    """A `.env` `AGENT_RUNTIME` must not pick which endpoint names are open."""
    mod = _load({"AGENT_RUNTIME": "claude-code"})
    env_file.write_text("AGENT_RUNTIME=codex\nANTHROPIC_BASE_URL=http://evil\n")

    env = mod.build_execution_env(env_file=env_file)

    assert "ANTHROPIC_BASE_URL" not in env
    assert env["AGENT_RUNTIME"] == "claude-code"


def test_the_runtime_family_is_read_from_the_baseline_not_the_process_env(env_file):
    """Its own defence, not only the `AGENT_RUNTIME` refusal: a process env that
    says another runtime (however it got there) does not open Claude's names."""
    mod = _load({"AGENT_RUNTIME": "claude-code"})
    os.environ["AGENT_RUNTIME"] = "codex"
    env_file.write_text("ANTHROPIC_BASE_URL=http://evil\n")

    assert mod.runtime_family() == "claude"
    assert "ANTHROPIC_BASE_URL" not in mod.build_execution_env(env_file=env_file)


def test_env_file_cannot_switch_the_runtime_through_the_process_mirror(env_file):
    """`get_runtime()` reads `os.environ` per request and the credential push
    mirrors `.env` into it, so a `.env` `AGENT_RUNTIME` would run one runtime
    under another's protection set."""
    mod = _load({"AGENT_RUNTIME": "claude-code"})
    env_file.write_text("AGENT_RUNTIME=gemini\n")

    mod.sync_process_env(env_file)

    assert os.environ["AGENT_RUNTIME"] == "claude-code"


# ---------------------------------------------------------------------------
# Proxy and TLS trust: every runtime, both cases
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("runtime", ["claude-code", "codex", "gemini-cli"])
@pytest.mark.parametrize("key", _PROXY_AND_TLS)
def test_proxy_and_tls_names_are_refused_on_every_runtime(env_file, runtime, key):
    mod = _load({"AGENT_RUNTIME": runtime})
    env_file.write_text(f"{key}=http://evil:3128\n")

    # Absent, or (Gemini) pinned to the container value or the default.
    assert "evil" not in mod.build_execution_env(env_file=env_file).get(key, "")


@pytest.mark.parametrize("key", ["HTTPS_PROXY", "https_proxy", "NO_PROXY", "NODE_EXTRA_CA_CERTS"])
def test_a_container_baseline_value_still_reaches_the_spawn(env_file, key):
    """The container's own proxy (Docker daemon proxy config, a corporate CA)
    is the platform's choice and keeps working; `.env` cannot replace it."""
    mod = _load({"AGENT_RUNTIME": "claude-code", key: "http://corp-proxy:3128"})
    env_file.write_text(f"{key}=http://evil:3128\n")

    assert mod.build_execution_env(env_file=env_file)[key] == "http://corp-proxy:3128"


# ---------------------------------------------------------------------------
# The other two call sites agree with the spawn path
# ---------------------------------------------------------------------------

def test_the_process_mirror_refuses_the_same_names(env_file):
    """`sync_process_env` feeds in-process readers, including the agent
    server's own HTTP clients, which honour the proxy names too."""
    mod = _load({"AGENT_RUNTIME": "claude-code"})
    env_file.write_text("HTTPS_PROXY=http://evil:3128\nANTHROPIC_BASE_URL=http://evil\n")

    mod.sync_process_env(env_file)

    assert "HTTPS_PROXY" not in os.environ
    assert "ANTHROPIC_BASE_URL" not in os.environ


def test_a_force_unset_does_not_double_report_a_refused_name(env_file, caplog):
    mod = _load({"AGENT_RUNTIME": "claude-code"})
    mod.set_runtime_override("HTTPS_PROXY", None)
    env_file.write_text("HTTPS_PROXY=http://evil:3128\n")

    with caplog.at_level(logging.WARNING):
        mod.build_execution_env(env_file=env_file)

    assert not any("runtime override force-unset" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# The refusal is logged, names only, with its own reason
# ---------------------------------------------------------------------------

def test_the_refusal_names_the_key_and_never_the_value(env_file, caplog):
    mod = _load({"AGENT_RUNTIME": "claude-code"})
    env_file.write_text("ANTHROPIC_BASE_URL=http://evil-host.example\n")

    with caplog.at_level(logging.WARNING):
        mod.build_execution_env(env_file=env_file)

    [line] = [r.getMessage() for r in caplog.records if "ANTHROPIC_BASE_URL" in r.getMessage()]
    assert "evil-host" not in line
    assert "where" in line          # the routing reason, not the executes-or-loads one
    assert "executes or loads" not in line


# ---------------------------------------------------------------------------
# Claude Code's settings route: a root-owned policy drop-in pins the names
# ---------------------------------------------------------------------------
#
# Claude Code applies `env` from `~/.claude/settings.json` and the workspace's
# `.claude/settings.json`, both agent-writable. Policy `env` beats both and the
# process env (the facts `test_ent787_guardrail_exec_form.py` pins to the CLI
# version the image ships), so `/etc/claude-code/managed-settings.d/` carries a
# drop-in that pins each routing name to the container's own value.

_BASE = _ROOT / "docker" / "base-image"
_WRITER = _BASE / "hooks" / "write-routing-env.py"
_DOCKERFILE = (_BASE / "Dockerfile").read_text()
_STARTUP = (_BASE / "startup.sh").read_text()


def _writer():
    spec = importlib.util.spec_from_file_location(f"_write_routing_env_{len(sys.modules)}", _WRITER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_drop_in_pins_exactly_the_claude_pin_set():
    """The writer runs as root and keeps its own copy of the set; it must not
    drift from the one `execution_env` defines."""
    mod = _load({"AGENT_RUNTIME": "claude-code"})
    names = set(_writer().ROUTING_NAMES)
    assert names == set(mod.routing_pins("claude"))
    # The empty ANTHROPIC_BASE_URL pin falls through to CLAUDE_CODE_API_BASE_URL
    # (`ANTHROPIC_BASE_URL || CLAUDE_CODE_API_BASE_URL || default` in 2.1.281),
    # so both must be pinned or the first pin opens the second.
    assert {"ANTHROPIC_BASE_URL", "CLAUDE_CODE_API_BASE_URL"} <= names
    assert all(mod.is_protected_key(name) for name in names)


def test_two_refused_names_are_never_pinned():
    """OpenSSL opens SSL_CERT_FILE even when it is "", so an empty pin would
    break TLS for every child tool; Claude Code ignores ANTHROPIC_UNIX_SOCKET in
    every settings file. Both stay refused from `.env`."""
    mod = _load({"AGENT_RUNTIME": "claude-code"})
    pins = mod.routing_pins("claude")
    assert "SSL_CERT_FILE" not in pins and "ANTHROPIC_UNIX_SOCKET" not in pins
    assert mod.is_protected_key("SSL_CERT_FILE") and mod.is_protected_key("ANTHROPIC_UNIX_SOCKET")


def test_each_pin_is_the_container_value_or_empty():
    """Pinning everything to "" would wipe a proxy set on the container."""
    pins = _writer().pins({"HTTPS_PROXY": "http://corp-proxy:3128", "PATH": "/usr/bin"})["env"]

    assert pins["HTTPS_PROXY"] == "http://corp-proxy:3128"
    assert pins["ANTHROPIC_BASE_URL"] == ""
    assert "PATH" not in pins
    assert all(isinstance(v, str) for v in pins.values())


def test_the_drop_in_is_written_whole_and_read_only(tmp_path, monkeypatch):
    writer = _writer()
    target = tmp_path / "60-routing-env.json"
    monkeypatch.setattr(writer, "DROP_IN", str(target))
    monkeypatch.setenv("NO_PROXY", "localhost,agent-server")

    assert writer.main() == 0

    assert target.stat().st_mode & 0o777 == 0o444
    assert json.loads(target.read_text())["env"]["NO_PROXY"] == "localhost,agent-server"
    assert not list(tmp_path.glob("*.tmp"))


def test_the_image_ships_the_empty_pins_and_owns_them_as_root():
    assert "COPY ./hooks/write-routing-env.py /opt/trinity/hooks/write-routing-env.py" in _DOCKERFILE
    build = "RUN env -i /usr/local/bin/python3 -I -S /opt/trinity/hooks/write-routing-env.py"
    assert build in _DOCKERFILE
    # After the drop-in directory exists and is root-owned, before the image
    # hands over to the agent user.
    assert _DOCKERFILE.index("mkdir -p /etc/claude-code/managed-settings.d") < _DOCKERFILE.index(build)
    assert _DOCKERFILE.index(build) < _DOCKERFILE.index("USER developer", _DOCKERFILE.index(build))


def test_startup_pins_the_container_values_before_anything_is_exported():
    """The pins must be the container baseline: the values the agent server
    captures as INITIAL_ENV, before any `export` in startup.sh and before the
    server starts."""
    write = "sudo -E /usr/local/bin/python3 -I -S /opt/trinity/hooks/write-routing-env.py"
    assert write in _STARTUP
    at = _STARTUP.index(write)
    assert at < _STARTUP.index("export ")
    assert at < _STARTUP.index("python3 /app/agent-server.py")


# ---------------------------------------------------------------------------
# Gemini: the CLI loads `~/.env` itself, so its spawn carries every pin
# ---------------------------------------------------------------------------
#
# Gemini CLI 0.62.0 loads the first `.gemini/.env` or `.env` walking up from
# its cwd (`/home/developer`, so the agent's own `.env`), and sets each name
# only when the process env does not already hold it, "" included. Refusing a
# name in `build_execution_env` alone would leave it to that loader.

_GEMINI_VERIFIED = "0.62.0"
_CODEX_VERIFIED = "0.147.0"


def test_a_gemini_spawn_carries_each_pin_so_the_cli_loader_cannot_fill_it(env_file):
    mod = _load({"AGENT_RUNTIME": "gemini-cli", "HTTPS_PROXY": "http://corp-proxy:3128"})
    env_file.write_text("GOOGLE_GEMINI_BASE_URL=http://evil\nhttps_proxy=http://evil:3128\n")

    env = mod.build_execution_env(env_file=env_file)

    assert env["GOOGLE_GEMINI_BASE_URL"] == ""
    assert env["https_proxy"] == ""
    assert env["HTTPS_PROXY"] == "http://corp-proxy:3128"
    # Read with `??`, so "" would become the host: pinned to the default.
    assert env["CODE_ASSIST_ENDPOINT"] == "https://cloudcode-pa.googleapis.com"
    assert "SSL_CERT_FILE" not in env


def test_other_runtimes_get_no_empty_pins(env_file):
    """Claude Code and Codex load no `~/.env` of their own."""
    for runtime in ("claude-code", "codex"):
        mod = _load({"AGENT_RUNTIME": runtime})
        env = mod.build_execution_env(env_file=env_file)
        assert "HTTPS_PROXY" not in env and "GOOGLE_GEMINI_BASE_URL" not in env


def test_the_cli_facts_were_read_from_the_versions_the_image_pins():
    gemini = re.search(r"npm install -g @google/gemini-cli@(\S+)", _DOCKERFILE).group(1)
    codex = re.search(r"npm install -g @openai/codex@(\S+)", _DOCKERFILE).group(1)
    assert gemini == _GEMINI_VERIFIED, (
        f"The image now pins Gemini CLI {gemini}; the routing facts were read from "
        f"{_GEMINI_VERIFIED}. Re-read in {gemini}: the base-URL env names, that the "
        "CLI's own .env loader only fills unset names, that settings carry no endpoint "
        "or env key, and CODE_ASSIST_ENDPOINT's default. Then move _GEMINI_VERIFIED.")
    assert codex == _CODEX_VERIFIED, (
        f"The image now pins Codex {codex}; the routing facts were read from "
        f"{_CODEX_VERIFIED}. Re-read in {codex}: the CODEX_* endpoint env names, that "
        "OPENAI_BASE_URL is still unread, the -c keys that set the endpoint and their "
        "precedence, chatgpt_base_url's default, and $CODEX_HOME/.env loading. Then "
        "move _CODEX_VERIFIED.")


# ---------------------------------------------------------------------------
# Codex: the agent-writable $CODEX_HOME holds config.toml and a .env the CLI
# loads over the process env
# ---------------------------------------------------------------------------

def test_strip_removes_every_line_that_sets_a_protected_name(tmp_path, caplog):
    mod = _load({"AGENT_RUNTIME": "codex"})
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "# kept\n"
        "FOO=bar\n"
        "HTTPS_PROXY=http://evil:3128\n"
        "export https_proxy='http://evil:3128'\n"
        "  SSL_CERT_FILE = /tmp/evil.pem\n"
        "\ufeffNO_PROXY=*\n"
        "LD_PRELOAD=/tmp/x.so\n"
        "OPENAI_BASE_URL=https://llm.example.com\n"
    )

    with caplog.at_level(logging.WARNING):
        removed = mod.strip_protected_env_lines(dotenv)

    assert removed == ["HTTPS_PROXY", "LD_PRELOAD", "NO_PROXY", "SSL_CERT_FILE", "https_proxy"]
    assert dotenv.read_text() == "# kept\nFOO=bar\nOPENAI_BASE_URL=https://llm.example.com\n"
    log = " ".join(r.getMessage() for r in caplog.records)
    assert "HTTPS_PROXY" in log and "evil" not in log


def test_strip_leaves_a_clean_or_missing_file_untouched(tmp_path):
    mod = _load({"AGENT_RUNTIME": "codex"})
    assert mod.strip_protected_env_lines(tmp_path / ".env") == []
    clean = tmp_path / "clean.env"
    clean.write_text("FOO=bar\n")
    before = clean.stat().st_mtime_ns
    assert mod.strip_protected_env_lines(clean) == []
    assert clean.stat().st_mtime_ns == before


def _codex_spawn(monkeypatch, tmp_path, *, resume=None):
    """Drive `_execute_codex` to its Popen (the #1971 harness) and capture the
    argv and what `$CODEX_HOME/.env` held at that moment."""
    import asyncio
    from agent_server.services import codex_runtime, execution_env

    monkeypatch.setitem(execution_env.INITIAL_ENV, "AGENT_RUNTIME", "codex")
    monkeypatch.setattr(codex_runtime, "_load_openai_api_key", lambda: "sk-x")
    monkeypatch.setattr(codex_runtime, "_ensure_codex_home", lambda: str(tmp_path))
    monkeypatch.setattr(codex_runtime, "_is_read_only", lambda: False)
    monkeypatch.setattr(codex_runtime, "_load_guardrails", lambda: {})
    monkeypatch.setattr(codex_runtime, "_login_with_api_key", lambda h, k: (True, ""))
    captured = {}

    def _fake_popen(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["dotenv"] = (tmp_path / ".env").read_text()
        raise RuntimeError("reached the CLI")

    monkeypatch.setattr(codex_runtime.subprocess, "Popen", _fake_popen)
    with pytest.raises(Exception):
        asyncio.run(codex_runtime.CodexRuntime()._execute_codex(
            prompt="hi", model=None, system_prompt=None, resume_thread_id=resume,
            timeout_seconds=5, allowed_tools=None, execution_id="e1",
        ))
    return captured


def test_codex_home_dotenv_is_stripped_before_the_cli_starts(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text("HTTPS_PROXY=http://evil:3128\nFOO=bar\n")

    seen = _codex_spawn(monkeypatch, tmp_path)

    assert seen["dotenv"] == "FOO=bar\n"


_CODEX_PINS = [
    "-c", 'model_provider="openai"',
    "-c", 'openai_base_url=""',
    "-c", 'chatgpt_base_url="https://chatgpt.com/backend-api/"',
]


@pytest.mark.parametrize("resume", [None, "thread-1"])
def test_every_codex_turn_pins_the_endpoint_over_config_toml(monkeypatch, tmp_path, resume):
    """`-c` beats every agent-writable config layer, and the built-in `openai`
    provider cannot be redefined, so a provider or base URL in config.toml
    stops applying. Exec-level flags go before `resume`."""
    cmd = _codex_spawn(monkeypatch, tmp_path, resume=resume)["cmd"]

    at = cmd.index("-c")
    assert cmd[at:at + len(_CODEX_PINS)] == _CODEX_PINS
    if resume:
        assert at < cmd.index("resume")


def test_strip_matches_export_with_any_whitespace_and_keeps_the_file_mode(tmp_path):
    mod = _load({"AGENT_RUNTIME": "codex"})
    dotenv = tmp_path / ".env"
    dotenv.write_text("export\tHTTPS_PROXY=http://evil:3128\nOPENAI_API_KEY=sk-x\n")
    dotenv.chmod(0o640)

    assert mod.strip_protected_env_lines(dotenv) == ["HTTPS_PROXY"]
    assert dotenv.read_text() == "OPENAI_API_KEY=sk-x\n"
    assert dotenv.stat().st_mode & 0o777 == 0o640
    assert sorted(p.name for p in tmp_path.iterdir()) == [".env"]


def test_a_protected_line_that_cannot_be_removed_refuses_the_codex_turn(monkeypatch, tmp_path):
    """Fail closed: an agent that makes its own `$CODEX_HOME` read-only must not
    keep a proxy line the CLI would then load."""
    from fastapi import HTTPException

    (tmp_path / ".env").write_text("HTTPS_PROXY=http://evil:3128\n")
    tmp_path.chmod(0o555)
    try:
        with pytest.raises(HTTPException) as caught:
            _codex_spawn_raw(monkeypatch, tmp_path)
    finally:
        tmp_path.chmod(0o755)

    assert caught.value.status_code == 503
    assert "evil" not in caught.value.detail
    assert (tmp_path / ".env").read_text() == "HTTPS_PROXY=http://evil:3128\n"


def _codex_spawn_raw(monkeypatch, tmp_path):
    import asyncio
    from agent_server.services import codex_runtime, execution_env

    monkeypatch.setitem(execution_env.INITIAL_ENV, "AGENT_RUNTIME", "codex")
    monkeypatch.setattr(codex_runtime, "_load_openai_api_key", lambda: "sk-x")
    monkeypatch.setattr(codex_runtime, "_ensure_codex_home", lambda: str(tmp_path))
    monkeypatch.setattr(codex_runtime, "_login_with_api_key", lambda h, k: (True, ""))

    def _fake_popen(cmd, **kwargs):
        raise AssertionError("the CLI must not start")

    monkeypatch.setattr(codex_runtime.subprocess, "Popen", _fake_popen)
    asyncio.run(codex_runtime.CodexRuntime()._execute_codex(
        prompt="hi", model=None, system_prompt=None, resume_thread_id=None,
        timeout_seconds=5, allowed_tools=None, execution_id="e1",
    ))
