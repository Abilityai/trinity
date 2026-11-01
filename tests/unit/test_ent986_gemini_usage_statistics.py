"""trinity-enterprise#986 — a Gemini-runtime agent never sends Google usage
statistics by default.

Gemini CLI (pinned 0.62.0) defaults ``privacy.usageStatisticsEnabled`` to
``true``. With that default a model turn creates ``~/.gemini/installation_id``,
sends usage events carrying it (plus a host fingerprint) to Google, and adds it
as ``x-gemini-api-privileged-user-id`` on every model request. Trinity never
wrote the opt-out, so every Gemini agent did this with no admin opt-in.

Verified offline (``--network none``) against the pinned CLI before writing
these tests:

- ``usageStatisticsEnabled: false`` in ``~/.gemini/settings.json`` stops the
  ``installation_id`` from being created;
- the same key in the system file (``/etc/gemini-cli/settings.json`` or
  ``GEMINI_CLI_SYSTEM_SETTINGS_PATH``) does NOT, so the opt-out has to live in
  the user file;
- only a model turn creates the id (``--version`` / ``mcp add`` / ``mcp list``
  never do), so the file must carry the key before every turn spawns;
- ``GEMINI_CLI_HOME`` relocates the whole settings directory.

Pinned here: the helper's merge/JSONC/invalid-file behaviour; the key on every
platform write (MCP injection, startup, before both turn spawns); the
``runtime: "gemini"`` spelling reaching the Gemini writer; ``.env`` unable to
relocate the CLI's settings; and the Gemini credential files git-ignored (the
settings file carries the Trinity MCP bearer token).

Module under test: docker/base-image/agent_server/services/gemini_settings.py
(+ trinity_mcp.py, gemini_runtime.py, main.py, execution_env.py) and
src/backend/services/git_service/gitignore.py.
"""
from __future__ import annotations

import json
import os
import subprocess
from unittest.mock import MagicMock

import pytest

from agent_server.services import execution_env, gemini_runtime, gemini_settings, trinity_mcp

pytestmark = pytest.mark.unit

_KEY = ("privacy", "usageStatisticsEnabled")


def _stat_disabled(path) -> bool:
    data = json.loads(path.read_text())
    return data[_KEY[0]][_KEY[1]] is False


@pytest.fixture
def settings_path(tmp_path, monkeypatch):
    path = tmp_path / ".gemini" / "settings.json"
    monkeypatch.setattr(gemini_settings, "GEMINI_SETTINGS_PATH", path)
    return path


# --- the helper ----------------------------------------------------------------


def test_a_missing_file_is_created_with_the_opt_out(settings_path):
    assert gemini_settings.ensure_usage_statistics_disabled() is True

    assert json.loads(settings_path.read_text()) == {"privacy": {"usageStatisticsEnabled": False}}


def test_the_merge_keeps_every_other_key(settings_path):
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({
        "mcpServers": {"trinity": {"url": "http://mcp-server:8080/mcp"}},
        "privacy": {"other": 1},
        "model": {"name": "gemini-2.5-pro"},
    }))

    gemini_settings.ensure_usage_statistics_disabled()

    assert json.loads(settings_path.read_text()) == {
        "mcpServers": {"trinity": {"url": "http://mcp-server:8080/mcp"}},
        "privacy": {"other": 1, "usageStatisticsEnabled": False},
        "model": {"name": "gemini-2.5-pro"},
    }


@pytest.mark.parametrize("privacy", [{"usageStatisticsEnabled": True}, "on", None, ["x"]])
def test_an_agent_cannot_turn_it_back_on(settings_path, privacy):
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({"privacy": privacy}))

    gemini_settings.ensure_usage_statistics_disabled()

    assert _stat_disabled(settings_path)


def test_an_already_disabled_file_is_not_rewritten(settings_path):
    """Parallel turns call this before every spawn: no write when nothing drifted."""
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({"privacy": {"usageStatisticsEnabled": False}}))
    before = settings_path.stat()

    assert gemini_settings.ensure_usage_statistics_disabled() is True

    after = settings_path.stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)


def test_a_jsonc_file_the_cli_accepts_still_gets_the_opt_out(settings_path):
    """The CLI accepts // and /* */ comments; json.loads does not. Treating such
    a file as unparseable would leave the CLI running turns with stats ON."""
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        '{\n'
        '  // the agent\'s own servers\n'
        '  "mcpServers": {"x": {"url": "http://host/mcp", "note": "a /* not */ comment"}},\n'
        '  /* block\n comment */\n'
        '  "model": {"name": "gemini-2.5-pro"}\n'
        '}\n'
    )

    assert gemini_settings.ensure_usage_statistics_disabled() is True

    data = json.loads(settings_path.read_text())
    assert data["mcpServers"] == {"x": {"url": "http://host/mcp", "note": "a /* not */ comment"}}
    assert data["model"] == {"name": "gemini-2.5-pro"}
    assert data["privacy"]["usageStatisticsEnabled"] is False


@pytest.mark.parametrize("content", ["{not json", '{"a": 1,}', "[1, 2]"])
def test_an_invalid_file_is_left_alone(settings_path, content):
    """The CLI refuses to run on an invalid settings file (no turn, no id), so
    clobbering would only destroy the agent's config."""
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(content)
    before = settings_path.stat()

    assert gemini_settings.ensure_usage_statistics_disabled() is False

    assert settings_path.read_text() == content
    assert settings_path.stat().st_ino == before.st_ino


def test_the_write_is_owner_only_and_leaves_no_temp_file(settings_path):
    gemini_settings.ensure_usage_statistics_disabled()

    assert settings_path.stat().st_mode & 0o077 == 0, "the file carries the MCP bearer token"
    assert [p.name for p in settings_path.parent.iterdir()] == ["settings.json"]


# --- every platform writer ------------------------------------------------------


def test_trinity_mcp_injection_writes_the_opt_out(tmp_path, monkeypatch):
    monkeypatch.setattr(trinity_mcp, "Path", lambda _p: tmp_path)

    assert trinity_mcp._inject_gemini_mcp("http://mcp-server:8080/mcp", "trinity_mcp_x")

    data = json.loads((tmp_path / ".gemini" / "settings.json").read_text())
    assert data["mcpServers"]["trinity"]["url"] == "http://mcp-server:8080/mcp"
    assert data["privacy"]["usageStatisticsEnabled"] is False


@pytest.mark.parametrize("runtime", ["gemini", "gemini-cli", "GEMINI"])
def test_both_gemini_spellings_reach_the_gemini_writer(monkeypatch, runtime):
    """`runtime: "gemini"` is accepted by the backend and runtime_adapter, but
    trinity_mcp only matched "gemini-cli" and wrote Claude's ~/.mcp.json."""
    monkeypatch.setenv("TRINITY_MCP_URL", "http://mcp-server:8080/mcp")
    monkeypatch.setenv("TRINITY_MCP_API_KEY", "trinity_mcp_x")
    monkeypatch.setenv("AGENT_RUNTIME", runtime)
    called = []
    monkeypatch.setattr(trinity_mcp, "_inject_gemini_mcp", lambda *a: called.append("gemini") or True)
    monkeypatch.setattr(trinity_mcp, "_inject_claude_mcp", lambda *a: called.append("claude") or True)
    monkeypatch.setattr(trinity_mcp, "_configure_gemini_mcp_servers", lambda s: called.append("gemini-servers") or True)
    monkeypatch.setattr(trinity_mcp, "_configure_claude_mcp_servers", lambda s: called.append("claude-servers") or True)

    trinity_mcp.inject_trinity_mcp_if_configured()
    trinity_mcp.configure_mcp_servers({"s": {"command": "echo"}})

    assert called == ["gemini", "gemini-servers"]


def test_startup_writes_the_opt_out_without_mcp(settings_path, monkeypatch):
    """A cold start with no Trinity MCP configured still lands the key."""
    import uvicorn
    from agent_server import main as agent_main
    from agent_server.state import agent_state

    monkeypatch.setattr(agent_state, "agent_runtime", "gemini")
    monkeypatch.setattr(agent_main, "inject_trinity_mcp_if_configured", lambda: False)
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)

    agent_main.run_server()

    assert _stat_disabled(settings_path)


def test_startup_leaves_other_runtimes_alone(settings_path, monkeypatch):
    import uvicorn
    from agent_server import main as agent_main
    from agent_server.state import agent_state

    monkeypatch.setattr(agent_state, "agent_runtime", "claude-code")
    monkeypatch.setattr(agent_main, "inject_trinity_mcp_if_configured", lambda: False)
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)

    agent_main.run_server()

    assert not settings_path.exists()


# --- before every turn ----------------------------------------------------------


@pytest.fixture
def runtime(monkeypatch):
    rt = gemini_runtime.GeminiRuntime()
    monkeypatch.setattr(rt, "is_available", lambda: True)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    monkeypatch.setattr(gemini_runtime, "kill_cgroup_orphans", lambda **k: 0)
    monkeypatch.setattr(gemini_runtime, "get_process_registry", lambda: MagicMock())
    return rt


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["chat", "headless"])
async def test_the_file_carries_the_opt_out_when_the_turn_spawns(runtime, settings_path, monkeypatch, path):
    """The agent can rewrite its own settings between turns (Gemini runs with
    --yolo, and the file is in its workspace), so the check is at spawn time."""
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({"privacy": {"usageStatisticsEnabled": True}, "keep": 1}))
    seen_at_spawn = []

    def popen(*_a, **_k):
        seen_at_spawn.append(json.loads(settings_path.read_text()))
        proc = MagicMock()
        proc.pid = 4242
        proc.stdout.readline.return_value = ""
        proc.stderr.read.return_value = ""
        proc.wait.return_value = 0
        proc.poll.return_value = 0
        proc.returncode = 0
        return proc

    monkeypatch.setattr(gemini_runtime.subprocess, "Popen", popen)

    if path == "chat":
        await runtime.execute(prompt="hi", execution_id="ent986")
    else:
        await runtime.execute_headless(prompt="hi", execution_id="ent986")

    assert seen_at_spawn == [{"privacy": {"usageStatisticsEnabled": False}, "keep": 1}]


# --- .env cannot relocate the CLI's settings -----------------------------------


@pytest.mark.parametrize(
    "name", ["GEMINI_CLI_HOME", "GEMINI_CLI_SYSTEM_SETTINGS_PATH", "GEMINI_CLI_SYSTEM_DEFAULTS_PATH"]
)
def test_env_cannot_point_the_cli_at_other_settings(name):
    """The agent-writable .env reaches every spawn; GEMINI_CLI_HOME moves the
    settings the per-turn check writes out from under it."""
    assert name in execution_env.PROTECTED_KEYS


# --- the Gemini credential files are never committed ----------------------------


@pytest.mark.parametrize("path", [".gemini/settings.json", ".gemini/oauth_creds.json", ".gemini/installation_id"])
def test_gemini_credential_files_are_git_ignored(tmp_path, path):
    """`.gemini/settings.json` carries `Authorization: Bearer <TRINITY_MCP_API_KEY>`
    like `.mcp.json`, which was ignored while this was not; HOME is the repo
    root and sync stages with `git add -A`."""
    from services.git_service import gitignore

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("\n".join(gitignore._GITIGNORE_PATTERNS) + "\n")
    target = tmp_path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{}")

    result = subprocess.run(
        ["git", "-C", str(tmp_path), "check-ignore", "-q", path],
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"},
    )
    assert result.returncode == 0, f"{path} would be committed by sync"


def test_the_rest_of_gemini_dir_stays_committable(tmp_path):
    """Templates legitimately ship `.gemini/GEMINI.md` and `.gemini/commands/`."""
    from services.git_service import gitignore

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("\n".join(gitignore._GITIGNORE_PATTERNS) + "\n")
    for path in (".gemini/GEMINI.md", ".gemini/commands/x.toml"):
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text("x")
        result = subprocess.run(["git", "-C", str(tmp_path), "check-ignore", "-q", path])
        assert result.returncode == 1, f"{path} must stay committable"
