"""A Claude Code agent that is not applying Trinity's managed hooks says so, and
its restricted turns are refused (abilityai/trinity-enterprise#1176).

Trinity's guardrail hooks are registered in file-based managed settings
(`/etc/claude-code/managed-settings.json` and `managed-settings.d/`). When
Claude Code holds a server-managed settings payload with any policy key, it uses
that source and skips the files ("first-wins"); only `env` still merges per key.
The payload is cached at `<config dir>/remote-settings.json` and is what the CLI
starts on. So the agent server reads that cache:

* `/health` reports `skipped` on `guardrails_registration` and `skill_gate_hook`;
* a turn whose rule lives in a hook (a shared person's turn, a visitor's turn
  with `visitor_shell` off, a schedule run rated below the owner) is refused
  before the CLI starts, on every entry: `/api/chat` (a sequential MCP chat),
  `/api/task` (a parallel MCP chat, a Workspace turn), the #1083 async path and
  the pull worker — all of them reach ClaudeCodeRuntime.execute or
  .execute_headless;
* on an agent with skill gates the owner's turns run without the Skill, Agent
  and Task tools.

Modules under test: docker/base-image/agent_server/services/managed_hooks.py,
its call in services/claude_code.py, the spawn sites' --disallowedTools, and
routers/info.py /health.
Related flow: docs/memory/feature-flows/skill-gate.md (and docs/memory/architecture/agent-runtime.md)
"""
from __future__ import annotations

import ast
import asyncio
import json
import os
import threading
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from agent_server.routers import chat as chat_router
from agent_server.routers import info
from agent_server.services import (
    claude_code,
    execution_env,
    headless_executor,
    managed_hooks,
    pull_worker,
    result_callback,
    turn_scope,
)
from agent_server.state import agent_state

pytestmark = pytest.mark.unit

_AGENT_SERVER = Path(__file__).resolve().parents[2] / "docker" / "base-image" / "agent_server"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """A private home: never the developer machine's own ~/.claude."""
    root = tmp_path / "home"
    (root / ".claude").mkdir(parents=True)
    monkeypatch.setattr(managed_hooks, "AGENT_HOME", str(root))
    monkeypatch.setattr(managed_hooks, "SKILL_GATE_MARKER", str(tmp_path / "skill-gates-active"))
    monkeypatch.setattr(execution_env, "ENV_FILE", root / ".env")
    monkeypatch.setattr(execution_env, "INITIAL_ENV", {})
    monkeypatch.setattr(agent_state, "agent_runtime", "claude-code")
    managed_hooks._memo.clear()
    yield root
    managed_hooks._memo.clear()


def _cache(home: Path, payload, *, raw: bool = False, where: Path = None) -> Path:
    path = (where or home / ".claude") / managed_hooks.CACHE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload if raw else json.dumps(payload, indent=2))
    return path


def _gated(tmp_path):
    (tmp_path / "skill-gates-active").write_text("")


# ---------------------------------------------------------------------------
# The detector: what Claude Code will start on
# ---------------------------------------------------------------------------

def test_no_cache_is_ok():
    assert managed_hooks.state() == managed_hooks.OK


@pytest.mark.parametrize("payload, expected", [
    ({}, "ok"),
    ({"managedSourcesBehavior": "merge"}, "ok"),             # control keys only
    ({"wslInheritsWindowsSettings": True}, "ok"),
    ({"$meta": {"v": 1}}, "ok"),                              # stripped by the CLI
    ({"channelsEnabled": True}, "skipped"),                   # one unrelated key is enough
    ({"$schema": "https://json.schemastore.org/claude-code-settings.json"}, "skipped"),
    ({"channelsEnabled": True, "managedSourcesBehavior": "first-wins"}, "skipped"),
    ({"channelsEnabled": True, "managedSourcesBehavior": "merge"}, "ok"),
    ({"channelsEnabled": True, "managedSourcesBehavior": "merge", "disableAllHooks": False}, "ok"),
    # under merge a disable* switch takes the strictest value any source sets
    ({"channelsEnabled": True, "managedSourcesBehavior": "merge", "disableAllHooks": True}, "skipped"),
    ({"managedSourcesBehavior": "merge", "disableAllHooks": "true"}, "skipped"),
    # a string is truthy to the CLI whatever it says: fail closed
    ({"managedSourcesBehavior": "merge", "disableAllHooks": "false"}, "skipped"),
    ({"disableAllHooks": False}, "skipped"),                  # still a policy key, no merge
])
def test_a_cached_payload_decides(home, payload, expected):
    _cache(home, payload)
    assert managed_hooks.state() == expected


@pytest.mark.parametrize("raw", ["", "[]", "null", "{not json", '"a string"', "42"])
def test_a_cache_that_cannot_be_read_as_settings_fails_closed(home, raw):
    _cache(home, raw, raw=True)
    assert managed_hooks.state() == managed_hooks.SKIPPED


def test_an_oversized_cache_fails_closed(home, monkeypatch):
    monkeypatch.setattr(managed_hooks, "CACHE_MAX_BYTES", 64)
    _cache(home, {"managedSourcesBehavior": "merge", "pad": "x" * 200})
    assert managed_hooks.state() == managed_hooks.SKIPPED


def test_a_directory_at_the_cache_path_fails_closed(home):
    (home / ".claude" / managed_hooks.CACHE_NAME).mkdir()
    assert managed_hooks.state() == managed_hooks.SKIPPED


def test_a_symlink_at_the_cache_path_fails_closed(home, tmp_path):
    target = tmp_path / "clean.json"
    target.write_text("{}")
    os.symlink(target, home / ".claude" / managed_hooks.CACHE_NAME)
    assert managed_hooks.state() == managed_hooks.SKIPPED


def test_a_fifo_at_the_cache_path_fails_closed_without_blocking(home):
    os.mkfifo(home / ".claude" / managed_hooks.CACHE_NAME)
    box = []
    reader = threading.Thread(target=lambda: box.append(managed_hooks.state()), daemon=True)
    reader.start()
    reader.join(5)
    assert box == [managed_hooks.SKIPPED], "reading a FIFO must neither block nor pass"


@pytest.mark.skipif(os.getuid() == 0, reason="root reads a 000 file")
def test_an_unreadable_cache_fails_closed(home):
    path = _cache(home, {})
    path.chmod(0)
    try:
        assert managed_hooks.state() == managed_hooks.SKIPPED
    finally:
        path.chmod(0o600)


@pytest.mark.parametrize("source", ["env_file", "baseline"])
def test_a_config_dir_set_for_the_spawn_is_read_too(home, tmp_path, monkeypatch, source):
    other = tmp_path / "other-config"
    _cache(home, {}, where=other)
    _cache(home, {"channelsEnabled": True}, where=other)
    if source == "env_file":
        (home / ".env").write_text(f'CLAUDE_CONFIG_DIR="{other}"\n')
    else:
        monkeypatch.setattr(execution_env, "INITIAL_ENV", {"CLAUDE_CONFIG_DIR": str(other)})
    assert managed_hooks.state() == managed_hooks.SKIPPED


@pytest.mark.parametrize("value", ["claude-other", "~/claude-other"])
def test_a_relative_or_home_relative_config_dir_is_read_too(home, monkeypatch, value):
    """The CLI resolves a relative CLAUDE_CONFIG_DIR against its working
    directory (the spawn passes none, so the agent server's: the home)."""
    monkeypatch.chdir(home)
    _cache(home, {"channelsEnabled": True}, where=home / "claude-other")
    (home / ".env").write_text(f"CLAUDE_CONFIG_DIR={value}\n")
    assert managed_hooks.state() == managed_hooks.SKIPPED


def test_a_same_size_rewrite_with_its_mtime_restored_is_read_again(home):
    """The memo keys on the file's change time too, which a rewrite cannot restore."""
    path = _cache(home, '{"a": 1}', raw=True)
    assert managed_hooks.state() == managed_hooks.SKIPPED
    st = os.stat(path)
    path.write_text('{"$": 1}')                       # same size; `$` keys are metadata
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert managed_hooks.state() == managed_hooks.OK


def test_a_literal_tilde_config_dir_is_read_as_a_relative_dir_too(home, monkeypatch):
    """If the CLI does not expand `~`, `~/x` is a directory named `~` under its cwd."""
    monkeypatch.chdir(home)
    _cache(home, {"channelsEnabled": True}, where=home / "~" / "claude-other")
    (home / ".env").write_text("CLAUDE_CONFIG_DIR=~/claude-other\n")
    assert managed_hooks.state() == managed_hooks.SKIPPED


def test_a_rewritten_cache_is_read_again(home):
    _cache(home, {"channelsEnabled": True})
    assert managed_hooks.state() == managed_hooks.SKIPPED
    _cache(home, {"channelsEnabled": True, "managedSourcesBehavior": "merge"})
    assert managed_hooks.state() == managed_hooks.OK


@pytest.mark.parametrize("runtime", ["gemini", "codex", "opencode"])
def test_a_runtime_without_claude_hooks_is_unsupported(home, monkeypatch, runtime):
    _cache(home, {"channelsEnabled": True})
    monkeypatch.setattr(agent_state, "agent_runtime", runtime)
    assert managed_hooks.state() == managed_hooks.UNSUPPORTED_RUNTIME


# ---------------------------------------------------------------------------
# The refusal before the CLI starts
# ---------------------------------------------------------------------------

def _answer(monkeypatch, tier, shell=False, kind=None, schedule=None):
    asked = []

    async def fake_fetch(agent, run):
        asked.append(run)
        return tier, shell, kind, schedule
    monkeypatch.setattr(turn_scope, "fetch_turn", fake_fetch)
    monkeypatch.setattr(turn_scope, "_agent_name", lambda: "agent-a")
    return asked


def _require(execution_id="exec-1"):
    asyncio.run(managed_hooks.require_hooks_or_owner(execution_id))


def test_an_agent_whose_hooks_run_asks_nobody(home, monkeypatch):
    asked = _answer(monkeypatch, "share", kind="person")
    _require()
    assert asked == []


def test_a_call_with_no_execution_id_is_no_platform_turn(home, monkeypatch):
    _cache(home, {"channelsEnabled": True})
    asked = _answer(monkeypatch, "share", kind="person")
    _require(execution_id=None)
    assert asked == []


@pytest.mark.parametrize("tier, shell, kind", [
    ("owner", True, None),
    ("share", True, "visitor"),          # visitor_shell on: the turn already holds the shell
])
def test_turns_that_hold_the_shell_run(home, monkeypatch, tier, shell, kind):
    _cache(home, {"channelsEnabled": True})
    asked = _answer(monkeypatch, tier, shell, kind)
    _require()
    assert asked == ["exec-1"]


@pytest.mark.parametrize("tier, kind", [
    ("share", "person"),                 # Workspace, MCP chat with the person's own key, ...
    ("share", "visitor"),                # visitor_shell off
    ("unknown", None),                   # a run the platform does not know
])
def test_a_restricted_turn_is_refused_with_the_approved_words(home, monkeypatch, tier, kind):
    _cache(home, {"channelsEnabled": True})
    _answer(monkeypatch, tier, False, kind)
    with pytest.raises(HTTPException) as exc:
        _require()
    assert exc.value.status_code == 403
    assert exc.value.detail == {
        "message": ("This agent cannot limit what this turn may do right now, because its "
                    "guardrails are not running. Only the agent's owner can use it until that "
                    "is fixed."),
        "error_code": "turn_refused",
    }


def test_no_answer_from_the_platform_is_a_502_never_a_503(home, monkeypatch):
    """A 503 from a Claude agent reads as an auth failure on every caller (the
    subscription walk, the AUTH code). This is not one."""
    _cache(home, {"channelsEnabled": True})
    _answer(monkeypatch, "unreachable")
    with pytest.raises(HTTPException) as exc:
        _require()
    assert exc.value.status_code == 502
    assert exc.value.detail == {"message": turn_scope.COULD_NOT_CHECK.format(runtime="Claude Code"),
                                "terminal_reason": "turn_check_unavailable"}


def test_an_async_could_not_check_is_not_recorded_as_an_empty_result(home, monkeypatch):
    """#4778: the async result callback maps every bare 502 to empty_result."""
    from agent_server.services import result_callback

    _cache(home, {"channelsEnabled": True})
    _answer(monkeypatch, "unreachable")
    with pytest.raises(HTTPException) as exc:
        _require()
    env = result_callback._envelope_from_http_exception(exc.value)
    assert env["terminal_reason"] == "turn_check_unavailable"
    assert env["error_code"] is None


def test_a_schedule_run_gets_its_own_words_and_no_take_over_remedy(home, monkeypatch):
    _cache(home, {"channelsEnabled": True})
    _answer(monkeypatch, "share", False, "person", {
        "cause": "creator_not_owner_tier", "schedule": "Daily report", "creator": "pat",
        "remedy": "take_over"})
    with pytest.raises(HTTPException) as exc:
        _require()
    text = exc.value.detail["message"]
    assert exc.value.status_code == 403 and exc.value.detail["error_code"] == "turn_refused"
    assert text == ("This run is the scheduled task 'Daily report', not a chat. It runs with the "
                    "access of the person who created it, pat, who is not this agent's owner or an "
                    "admin, and this agent's guardrails are not running right now, so it did not "
                    "run. It will run again once the guardrails are running.")
    assert "take it over" not in text


def test_an_unreadable_schedule_says_so(home, monkeypatch):
    _cache(home, {"channelsEnabled": True})
    _answer(monkeypatch, "share", False, "person", {
        "cause": "schedule_unreadable", "schedule": None, "creator": None, "remedy": None})
    with pytest.raises(HTTPException) as exc:
        _require()
    assert exc.value.detail["message"].startswith(
        "This run is a scheduled task whose schedule can no longer be read")
    assert "guardrails are not running" in exc.value.detail["message"]


# ---------------------------------------------------------------------------
# Every Claude entry passes the check before it spawns
# ---------------------------------------------------------------------------

@pytest.fixture
def spawns(monkeypatch):
    calls = []

    async def fake_chat(*args, **kwargs):
        calls.append("chat")
        return "done", [], claude_code.ExecutionMetadata(), []

    async def fake_task(*args, **kwargs):
        calls.append("task")
        return "done", [], claude_code.ExecutionMetadata(), "session-1"
    monkeypatch.setattr(claude_code, "execute_claude_code", fake_chat)
    monkeypatch.setattr(claude_code, "execute_headless_task", fake_task)
    return calls


@pytest.mark.parametrize("method", ["execute", "execute_headless"])
def test_the_runtime_refuses_before_it_spawns(home, monkeypatch, spawns, method):
    _cache(home, {"channelsEnabled": True})
    _answer(monkeypatch, "share", False, "person")
    runtime = claude_code.ClaudeCodeRuntime()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(getattr(runtime, method)(prompt="hi", execution_id="exec-1"))
    assert exc.value.status_code == 403
    assert spawns == []


@pytest.mark.parametrize("method, spawned", [("execute", "chat"), ("execute_headless", "task")])
def test_the_owners_turn_still_spawns(home, monkeypatch, spawns, method, spawned):
    _cache(home, {"channelsEnabled": True})
    _answer(monkeypatch, "owner", True)
    asyncio.run(getattr(claude_code.ClaudeCodeRuntime(), method)(prompt="hi", execution_id="exec-1"))
    assert spawns == [spawned]


def _awaits_gate_before_spawn(func: ast.AST, spawn: str) -> bool:
    """The gate is an awaited statement that precedes the spawn call."""
    gate_line = spawn_line = None
    for node in ast.walk(func):
        if (isinstance(node, ast.Await) and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Attribute)
                and node.value.func.attr == "require_hooks_or_owner"):
            gate_line = node.lineno if gate_line is None else min(gate_line, node.lineno)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == spawn:
            spawn_line = node.lineno
    return gate_line is not None and spawn_line is not None and gate_line < spawn_line


@pytest.mark.parametrize("method, spawn", [("execute", "execute_claude_code"),
                                           ("execute_headless", "execute_headless_task")])
def test_the_gate_is_the_first_await_of_each_entry(method, spawn):
    tree = ast.parse((_AGENT_SERVER / "services" / "claude_code.py").read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ClaudeCodeRuntime")
    func = next(n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name == method)
    assert _awaits_gate_before_spawn(func, spawn)


@pytest.fixture
def agent_app(monkeypatch):
    """The agent server's real chat router: the two routes an MCP chat lands on
    (sequential → /api/chat, parallel → /api/task)."""
    monkeypatch.setattr(chat_router, "get_runtime", lambda: claude_code.ClaudeCodeRuntime())
    monkeypatch.setattr(chat_router, "_retain_terminal", lambda *a, **k: None)
    monkeypatch.setattr(result_callback, "try_spawn_async", lambda request: False)
    app = FastAPI()
    app.include_router(chat_router.router)
    return TestClient(app)


@pytest.mark.parametrize("route", ["/api/chat", "/api/task"])
def test_an_mcp_chat_by_a_shared_person_is_refused_on_both_routes(home, monkeypatch, spawns,
                                                                 agent_app, route):
    """An MCP chat made with a shared person's own MCP key is rated share/person
    by the platform, like a Workspace turn."""
    _cache(home, {"channelsEnabled": True})
    _answer(monkeypatch, "share", False, "person")
    response = agent_app.post(route, json={"message": "run ls", "execution_id": "exec-1"})
    assert response.status_code == 403
    assert response.json()["detail"]["error_code"] == "turn_refused"
    assert spawns == []


def test_the_async_and_pull_terminals_type_the_refusal():
    refused = HTTPException(403, {"message": managed_hooks.REFUSED, "error_code": "turn_refused"})
    unreachable = HTTPException(502, {"message": turn_scope.COULD_NOT_CHECK.format(runtime="Claude Code")})
    assert result_callback._envelope_from_http_exception(refused)["error_code"] == "turn_refused"
    assert pull_worker._failed_result_body_from_http("t", refused)["error_code"] == "turn_refused"
    assert result_callback._envelope_from_http_exception(unreachable)["error_code"] != "auth"
    assert pull_worker._failed_result_body_from_http("t", unreachable)["error_code"] == "agent_error"


# ---------------------------------------------------------------------------
# An agent with skill gates: the owner's turns run without skills
# ---------------------------------------------------------------------------

def test_a_gated_agent_in_the_skipped_state_drops_the_skill_tools(home, tmp_path):
    _gated(tmp_path)
    _cache(home, {"channelsEnabled": True})
    assert managed_hooks.gated_fallback_denials() == ("Skill", "Agent", "Task")


@pytest.mark.parametrize("gated, payload", [(True, {}), (False, {"channelsEnabled": True})])
def test_otherwise_nothing_is_dropped(home, tmp_path, gated, payload):
    if gated:
        _gated(tmp_path)
    _cache(home, payload)
    assert managed_hooks.gated_fallback_denials() == ()


@pytest.mark.parametrize("module, func", [(claude_code, "_execute_claude_code_once"),
                                          (headless_executor, "_setup_headless_command")])
def test_both_spawn_sites_pass_the_fallback_to_the_merge(module, func):
    tree = ast.parse(Path(module.__file__).read_text())
    target = next(n for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == func)
    merges = [n for n in ast.walk(target) if isinstance(n, ast.Call)
              and isinstance(n.func, ast.Name) and n.func.id == "merged_disallowed_tools"]
    assert merges, f"{func} no longer builds --disallowedTools through the merge"
    for call in merges:
        extra = next((k.value for k in call.keywords if k.arg == "extra"), None)
        assert (isinstance(extra, ast.Call) and isinstance(extra.func, ast.Attribute)
                and extra.func.attr == "gated_fallback_denials"), func


@pytest.mark.parametrize("gated, payload, dropped", [
    (True, {"channelsEnabled": True}, True),
    (True, {}, False),
    (False, {"channelsEnabled": True}, False),
])
def test_the_headless_command_carries_the_fallback(home, tmp_path, gated, payload, dropped):
    """Executed, not read: the headless spawn's real --disallowedTools value."""
    if gated:
        _gated(tmp_path)
    _cache(home, payload)
    ctx = headless_executor._setup_headless_command(
        "hi", "sonnet", None, None, 60, None, "exec-1", None, False, None)
    denied = ctx.cmd[ctx.cmd.index("--disallowedTools") + 1].split(",")
    assert all(t in denied for t in ("Skill", "Agent", "Task")) is dropped


def test_the_merge_appends_the_fallback_once():
    merged = claude_code.merged_disallowed_tools({"disallowed_tools": ["Skill"]},
                                                 extra=("Skill", "Agent", "Task"))
    assert merged.count("Skill") == 1 and "Agent" in merged and "Task" in merged


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------

@pytest.fixture
def intact(monkeypatch):
    monkeypatch.setattr(info, "_guardrails_registration", lambda: "ok")
    monkeypatch.setattr(info, "_skill_gate_hook", lambda: "ok")
    monkeypatch.setattr(info, "_vendor_telemetry_opt_out", lambda: "ok")


def test_health_reports_skipped_hooks(home, intact):
    _cache(home, {"channelsEnabled": True})
    body = asyncio.run(info.health_check())
    assert body["guardrails_registration"] == "skipped"
    assert body["skill_gate_hook"] == "skipped"
    # the env-only drop-in still merges per key (G9)
    assert body["vendor_telemetry_opt_out"] == "ok"


def test_health_reads_ok_when_the_hooks_run(home, intact):
    _cache(home, {"channelsEnabled": True, "managedSourcesBehavior": "merge"})
    body = asyncio.run(info.health_check())
    assert body["guardrails_registration"] == "ok" and body["skill_gate_hook"] == "ok"


def test_a_broken_registration_keeps_its_own_value(home, monkeypatch, intact):
    monkeypatch.setattr(info, "_guardrails_registration", lambda: "missing")
    monkeypatch.setattr(info, "_skill_gate_hook", lambda: "writable")
    _cache(home, {"channelsEnabled": True})
    body = asyncio.run(info.health_check())
    assert body["guardrails_registration"] == "missing" and body["skill_gate_hook"] == "writable"


def test_health_never_carries_the_cached_settings(home, intact):
    _cache(home, {"channelsEnabled": True, "someOrgSecretishKey": "value-1176"})
    body = json.dumps(asyncio.run(info.health_check()))
    assert "someOrgSecretishKey" not in body and "value-1176" not in body and "channelsEnabled" not in body
