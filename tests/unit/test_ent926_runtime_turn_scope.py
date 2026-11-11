"""A runtime that cannot scope a shared person's turn does not run one (ent#926).

Claude Code agents carry the turn-tier hook, which holds a shared person's turn
to the paths the Files API allows and gives it no shell. Gemini CLI and Codex
run no Claude Code hooks, so nothing would hold such a turn to anything. Ruling
2026-10-19: those runtimes refuse it, with a named reason, before the CLI starts.

The check sits at runtime spawn because every route a turn takes ends there:
the backend's push to /api/task, the sync /api/chat, and the pull worker
(#3127) that claims a turn the backend never pushes. The backend still decides
the tier (the same `turn-tier` route the hook asks); the agent server enforces.

Module under test: docker/base-image/agent_server/services/turn_scope.py, and
its call at the top of GeminiRuntime / CodexRuntime execute + execute_headless.
"""
from __future__ import annotations

import asyncio
import http.server
import json
import threading
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from agent_server.services import codex_runtime, gemini_runtime, turn_scope

pytestmark = pytest.mark.unit


def _require(monkeypatch, tier, execution_id="exec-1", may_run=None, kind=None):
    asked = []
    may_run = (tier == "owner") if may_run is None else may_run

    async def fake_fetch(agent, run):
        asked.append((agent, run))
        return tier, may_run, kind, None
    monkeypatch.setattr(turn_scope, "fetch_turn", fake_fetch)
    monkeypatch.setattr(turn_scope, "_agent_name", lambda: "agent-a")
    asyncio.run(turn_scope.require_owner_tier("Gemini CLI", execution_id))
    return asked


# ---- the decision ---------------------------------------------------------------------

def test_the_owners_turn_runs(monkeypatch):
    assert _require(monkeypatch, "owner") == [("agent-a", "exec-1")]


@pytest.mark.parametrize("tier", ["share", "unknown"])
def test_a_shared_or_unknown_turn_is_refused_with_a_named_reason(monkeypatch, tier):
    with pytest.raises(HTTPException) as exc:
        _require(monkeypatch, tier)
    assert exc.value.status_code == 403
    assert exc.value.detail["error_code"] == "turn_refused"
    assert "Gemini CLI" in exc.value.detail["message"] and "shared" in exc.value.detail["message"]


def test_no_answer_refuses_rather_than_running(monkeypatch):
    with pytest.raises(HTTPException) as exc:
        _require(monkeypatch, "unreachable")
    assert exc.value.status_code == 503


def test_a_call_with_no_execution_id_is_no_platform_turn_and_asks_nobody(monkeypatch):
    assert _require(monkeypatch, "share", execution_id=None) == []


# ---- the lookup, over real HTTP ----------------------------------------------------------

class _Platform(http.server.BaseHTTPRequestHandler):
    reply = (200, {"tier": "owner"})
    seen: list = []

    def do_GET(self):  # noqa: N802
        _Platform.seen.append((self.path, self.headers.get("Authorization")))
        status, body = _Platform.reply
        payload = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):
        pass


@pytest.fixture()
def platform(monkeypatch):
    server = http.server.HTTPServer(("127.0.0.1", 0), _Platform)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _Platform.seen = []
    monkeypatch.setenv("TRINITY_BACKEND_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("TRINITY_MCP_API_KEY", "agent-key")
    yield _Platform
    server.shutdown()


def test_the_lookup_sends_the_agents_own_key_to_the_turn_tier_route(platform):
    assert asyncio.run(turn_scope.fetch_tier("agent a", "exec/1")) == "owner"
    assert platform.seen == [("/api/agents/agent%20a/executions/exec%2F1/turn-tier", "Bearer agent-key")]


@pytest.mark.parametrize("status,body,expected", [
    (200, {"tier": "share"}, "share"),
    (200, {"tier": "admin"}, "unreachable"),
    (200, b"not json", "unreachable"),
    (404, {"detail": "nope"}, "unknown"),
    (500, {"detail": "boom"}, "unreachable"),
])
def test_the_lookup_reads_only_a_clean_owner_or_share_answer(platform, status, body, expected):
    platform.reply = (status, body)
    try:
        assert asyncio.run(turn_scope.fetch_tier("agent-a", "exec-1")) == expected
    finally:
        platform.reply = (200, {"tier": "owner"})


def test_no_key_is_no_answer(monkeypatch):
    monkeypatch.delenv("TRINITY_MCP_API_KEY", raising=False)
    assert asyncio.run(turn_scope.fetch_tier("agent-a", "exec-1")) == "unreachable"


def test_an_unreachable_platform_is_no_answer(monkeypatch):
    monkeypatch.setenv("TRINITY_BACKEND_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("TRINITY_MCP_API_KEY", "agent-key")
    assert asyncio.run(turn_scope.fetch_tier("agent-a", "exec-1")) == "unreachable"


# ---- wired into every Gemini / Codex entry point, before the CLI starts --------------------

def _runtimes(monkeypatch):
    gem = gemini_runtime.GeminiRuntime()
    cod = codex_runtime.CodexRuntime()
    for rt in (gem, cod):
        monkeypatch.setattr(rt, "is_available", lambda: True)

    def no_spawn(*a, **k):
        raise AssertionError("the CLI was started for a refused turn")
    monkeypatch.setattr(gemini_runtime.subprocess, "Popen", no_spawn)
    monkeypatch.setattr(codex_runtime.subprocess, "Popen", no_spawn)
    monkeypatch.setattr(gemini_runtime, "get_process_registry", lambda: MagicMock())
    return gem, cod


@pytest.mark.parametrize("which", ["gemini", "codex"])
@pytest.mark.parametrize("entry", ["execute", "execute_headless"])
def test_a_shared_persons_turn_never_starts_the_cli(monkeypatch, which, entry):
    gem, cod = _runtimes(monkeypatch)
    rt = gem if which == "gemini" else cod

    async def share(agent, run):
        return "share", False, "person", None
    monkeypatch.setattr(turn_scope, "fetch_turn", share)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(getattr(rt, entry)(prompt="hi", execution_id="exec-1"))
    assert exc.value.status_code == 403


def test_a_channel_visitor_runs_while_the_owner_allows_it(monkeypatch):
    """`share` with the platform's shell answer True: a Slack/Telegram/public-link
    turn on an agent whose owner left `visitor_shell` on (the default)."""
    assert _require(monkeypatch, "share", may_run=True) == [("agent-a", "exec-1")]


@pytest.mark.parametrize("body,expected", [
    ({"tier": "share", "kind": "visitor", "shell": True}, ("share", True, "visitor", None)),
    ({"tier": "share", "kind": "person", "shell": "yes"}, ("share", False, "person", None)),
    ({"tier": "owner"}, ("owner", False, None, None)),
    # ent#1148: an owner-by-rule turn is read by tier and shell; its kind reads as None.
    ({"tier": "owner", "kind": "member", "shell": True}, ("owner", True, None, None)),
    ({"tier": "owner", "kind": "delegated", "shell": True}, ("owner", True, None, None)),
])
def test_the_may_run_answer_is_a_strict_true(platform, body, expected):
    platform.reply = (200, body)
    try:
        assert asyncio.run(turn_scope.fetch_turn("agent-a", "exec-1")) == expected
    finally:
        platform.reply = (200, {"tier": "owner"})


def test_a_visitor_the_owner_limited_is_told_so_not_called_a_shared_person(monkeypatch):
    with pytest.raises(HTTPException) as exc:
        _require(monkeypatch, "share", may_run=False, kind="visitor")
    assert exc.value.status_code == 403
    assert "channel or a public link" in exc.value.detail["message"]
    assert "shared with" not in exc.value.detail["message"]


@pytest.mark.parametrize("kind", ["member", "delegated"])
def test_an_owner_by_rule_turn_runs(monkeypatch, kind):
    """ent#1148: a member's turn or a delegated turn runs on Gemini/Codex like the owner's."""
    assert _require(monkeypatch, "owner", may_run=True, kind=kind) == [("agent-a", "exec-1")]


# ---- a run refused because of its schedule's creator (trinity#4281) -----------------------
# On these runtimes such a run fails visibly (`turn_refused`), but the text called it "a
# person the agent is shared with" and told the owner to switch to Claude Code, which would
# not give the run the shell either. The refusal keys on the answer's `cause`.

def _schedule_answer(**fields):
    body = {"tier": "share", "kind": "person", "shell": False, "cause": "creator_not_owner_tier",
            "schedule": "Morning brief", "creator": "Dana", "remedy": "recreate"}
    body.update(fields)
    return body


def _refused(platform, monkeypatch, runtime, body):
    platform.reply = (200, body)
    monkeypatch.setattr(turn_scope, "_agent_name", lambda: "agent-a")
    try:
        with pytest.raises(HTTPException) as exc:
            asyncio.run(turn_scope.require_owner_tier(runtime, "exec-1"))
    finally:
        platform.reply = (200, {"tier": "owner"})
    assert exc.value.status_code == 403 and exc.value.detail["error_code"] == "turn_refused"
    return exc.value.detail["message"]


@pytest.mark.parametrize("runtime", ["Gemini CLI", "Codex"])
def test_a_schedule_creator_refusal_names_the_schedule_its_creator_and_the_remedy(platform, monkeypatch, runtime):
    text = _refused(platform, monkeypatch, runtime, _schedule_answer())
    assert "scheduled task 'Morning brief'" in text and "not a chat" in text
    assert "Dana" in text and "re-create it as their own" in text and runtime in text
    assert "shared with" not in text
    assert "Claude Code" not in text          # switching runtime would not give it the shell


@pytest.mark.parametrize("runtime", ["Gemini CLI", "Codex"])
@pytest.mark.parametrize("kind", ["visitor", "person"])
def test_an_unreadable_schedule_gets_its_own_text(platform, monkeypatch, runtime, kind):
    body = _schedule_answer(kind=kind, cause="schedule_unreadable", schedule=None, creator=None, remedy=None)
    text = _refused(platform, monkeypatch, runtime, body)
    assert "scheduled task whose schedule can no longer be read" in text and runtime in text
    assert "channel or a public link" not in text and "shared with" not in text
    assert "Claude Code" not in text and "Guardrails" not in text


@pytest.mark.parametrize("cause,creator,expected", [
    ("creator_missing", None, "an account that no longer exists"),
    ("agent_has_no_owner", "Dana", "This agent has no owner on record"),
    ("unknown", None, "the person who created it, not with an owner's"),
])
def test_each_creator_cause_has_honest_text(platform, monkeypatch, cause, creator, expected):
    text = _refused(platform, monkeypatch, "Codex", _schedule_answer(cause=cause, creator=creator))
    assert expected in text and "shared with" not in text


@pytest.mark.parametrize("remedy,expected", [
    ("take_over", "the agent's owner or an admin to take it over."),
    ("repair_agent_owner", "an admin to restore this agent's owner record."),
    ("not_a_remedy_yet", "the agent's owner or an admin to take it over or re-create it as their own."),
    ({"code": "take_over"}, "the agent's owner or an admin to take it over or re-create it as their own."),
])
def test_the_remedy_comes_from_a_fixed_table(platform, monkeypatch, remedy, expected):
    text = _refused(platform, monkeypatch, "Codex", _schedule_answer(remedy=remedy))
    assert text.endswith(expected) and "not_a_remedy_yet" not in text


@pytest.mark.parametrize("body,expected", [
    ({"tier": "share", "kind": "person", "shell": False}, "REFUSED"),
    ({"tier": "share", "kind": "visitor", "shell": False}, "REFUSED_VISITOR"),
    ({"tier": "share", "kind": "person", "shell": False, "cause": None}, "REFUSED"),
    ({"tier": "share", "kind": "visitor", "shell": False, "cause": "Not A Code"}, "REFUSED_VISITOR"),
])
def test_an_answer_without_a_cause_keeps_todays_text(platform, monkeypatch, body, expected):
    """Skew: a backend from before #4281 sends no `cause`."""
    text = _refused(platform, monkeypatch, "Codex", body)
    assert text == getattr(turn_scope, expected).format(runtime="Codex")


def test_todays_texts_are_unchanged():
    assert turn_scope.REFUSED == (
        "This turn is for a person the agent is shared with, and this agent runs on {runtime}, "
        "which cannot limit what such a turn may do. Only the agent's owner can run it here; "
        "the owner can switch the agent to Claude Code to let shared people use it.")


def test_the_labels_are_bounded_before_they_reach_the_refusal(platform, monkeypatch):
    body = _schedule_answer(schedule="Brief\n\x1b[2J‮ " + "n" * 2500,
                            creator="dana\x07@example.com")
    text = _refused(platform, monkeypatch, "Codex", body)
    assert "scheduled task '" + ("Brief [2J " + "n" * 2500)[:80] + "'" in text
    assert "the person who created it, dana," in text
    for bad in ("\n", "\x1b", "‮", "\x07", "example.com"):
        assert bad not in text
    assert len(text) < 1000


def test_the_lookup_reads_the_schedule_fields_only_from_a_shared_answer(platform):
    platform.reply = (200, _schedule_answer(remedy="take_over"))
    try:
        assert asyncio.run(turn_scope.fetch_turn("agent-a", "exec-1")) == (
            "share", False, "person",
            {"cause": "creator_not_owner_tier", "schedule": "Morning brief", "creator": "Dana",
             "remedy": "take_over"})
        platform.reply = (200, {**_schedule_answer(), "tier": "owner", "kind": None, "shell": True})
        assert asyncio.run(turn_scope.fetch_turn("agent-a", "exec-1")) == ("owner", True, None, None)
    finally:
        platform.reply = (200, {"tier": "owner"})


def test_the_hook_and_the_runtime_name_causes_and_remedies_alike():
    """Two copies in one image (the hook runs standalone from /opt/trinity/hooks;
    the agent server is its own package): the facts they state must agree."""
    import importlib.util
    import sys
    from pathlib import Path
    hooks = Path(__file__).resolve().parents[2] / "docker/base-image/hooks"
    with pytest.MonkeyPatch.context() as mp:
        mp.syspath_prepend(str(hooks))
        lib_spec = importlib.util.spec_from_file_location("lib", str(hooks / "lib.py"))
        lib = importlib.util.module_from_spec(lib_spec)
        lib_spec.loader.exec_module(lib)
        mp.setitem(sys.modules, "lib", lib)
        spec = importlib.util.spec_from_file_location("_turn_tier_guard_parity", str(hooks / "turn-tier-guard.py"))
        hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hook)
    assert hook.SCHEDULE_REMEDIES == turn_scope.SCHEDULE_REMEDIES
    assert hook.SCHEDULE_REMEDY_GENERIC == turn_scope.SCHEDULE_REMEDY_GENERIC
    for cause in ("creator_not_owner_tier", "creator_missing", "agent_has_no_owner", "unknown", "new_one"):
        for creator in ("Dana", None):
            assert hook.schedule_runs_as(cause, creator) == turn_scope.schedule_runs_as(cause, creator)
    for raw in ("a\nb\x1b‮ c  d", "x" * 300, "", None, 5, "dana@example.com"):
        for limit in (80, 64):
            assert hook.bounded_label(raw, limit) == turn_scope.bounded_label(raw, limit)
