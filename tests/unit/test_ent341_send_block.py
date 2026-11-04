"""The send block is refused by Trinity, not only hidden (trinity-enterprise#341).

The operator's decision at the issue's design gate: removing a send tool from
Claude Code's tool list is not a boundary (the agent's shell holds its MCP key,
and its own `.mcp.json` can add a second copy of the Trinity server; Codex and
Gemini ignore the list), so the backend routes behind the seven send tools and
`report` refuse an agent-scoped caller whose ENFORCED list names the tool.

Pinned here:
* `assert_tool_allowed` — who is checked (agent keys only), what is read (the
  enforced list, never a dormant Custom list), what the refusal says;
* every one of the nine routes refuses, driven through its real router with an
  agent principal whose profile denies the tool (deleting the call turns this
  red: the route then runs on and answers something else);
* each call names the right tool and runs before the handler does anything
  (AST: the first statement after the handler's own-agent / owner gate).

Related: docs/memory/requirements/security.md §28.10.
"""
from __future__ import annotations

import ast
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pytest

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from db_harness import db_backend  # noqa: E402,F401

import dependencies  # noqa: E402
from database import db  # noqa: E402
from db_models import UserCreate  # noqa: E402
from services import tool_access_profiles as tap  # noqa: E402
from services.tool_access_guard import assert_tool_allowed  # noqa: E402

pytestmark = pytest.mark.unit

OWNER = "sb341-owner"
AGENT = "sb341-agent"


@dataclass
class _Principal:
    """Every field `models.User` carries, stated — never a MagicMock."""

    id: int
    username: str
    email: Optional[str]
    role: str
    agent_name: Optional[str] = None
    connector_agent: Optional[str] = None
    portal_delegate: bool = False
    mcp_scope: Optional[str] = None
    mcp_key_id: Optional[str] = None
    mcp_key_name: Optional[str] = None
    vouched_source_agent: Optional[str] = None


def _agent_principal(owner_id: int) -> _Principal:
    return _Principal(id=owner_id, username=OWNER, email="sb341-owner@example.com",
                      role="operator", agent_name=AGENT, mcp_scope="agent")


def _select(profile: str, custom=None) -> None:
    cfg, ro = tap.plan_selection(db.get_guardrails_config(AGENT), False, profile, custom)
    db.set_guardrails_config(AGENT, cfg)
    db.set_read_only_mode(AGENT, ro, None)


@pytest.fixture
def world(db_backend, monkeypatch):
    monkeypatch.setattr(dependencies, "get_breaker_redis", lambda: None)
    db.create_user(UserCreate(username=OWNER, role="operator", email="sb341-owner@example.com"))
    db.register_agent_owner(AGENT, OWNER)
    return db.get_user_by_username(OWNER)["id"]


# ---------------------------------------------------------------------------
# The helper
# ---------------------------------------------------------------------------

class TestHelper:
    def test_denied_tool_is_a_403_naming_tool_and_profile(self, world):
        _select("workspace_only")
        with pytest.raises(HTTPException) as e:
            assert_tool_allowed(_agent_principal(world), "send_message")
        assert e.value.status_code == 403
        assert "send_message" in e.value.detail and "Workspace-only" in e.value.detail

    def test_report_is_allowed_by_workspace_and_refused_by_strict(self, world):
        _select("workspace_only")
        assert_tool_allowed(_agent_principal(world), "report")
        _select("strict_read_only")
        with pytest.raises(HTTPException):
            assert_tool_allowed(_agent_principal(world), "report")

    def test_people_are_never_checked(self, world):
        _select("strict_read_only")
        person = _Principal(id=world, username=OWNER, email=None, role="operator")
        assert_tool_allowed(person, "send_message")

    def test_a_dormant_custom_list_is_not_enforced(self, world):
        _select("custom", {"read_only": False, "disallowed_tools": ["mcp__trinity__send_message"]})
        _select("full")
        assert_tool_allowed(_agent_principal(world), "send_message")

    def test_legacy_read_only_agent_keeps_sending_until_applied(self, world):
        db.set_read_only_mode(AGENT, True, None)
        assert_tool_allowed(_agent_principal(world), "send_message")

    def test_a_custom_list_naming_a_send_tool_is_refused(self, world):
        _select("custom", {"read_only": False, "disallowed_tools": ["mcp__trinity__call_user"]})
        with pytest.raises(HTTPException) as e:
            assert_tool_allowed(_agent_principal(world), "call_user")
        assert "Custom" in e.value.detail

    def test_sending_as_a_strict_sibling_is_refused(self, world):
        """An agent reaching a sibling's send route (a permission edge) sends AS
        that sibling: the sibling's tool access applies too, by name."""
        _select("strict_read_only")
        sibling = _Principal(id=world, username=OWNER, email=None, role="operator",
                             agent_name="sb341-sibling", mcp_scope="agent")
        with pytest.raises(HTTPException) as e:
            assert_tool_allowed(sibling, "send_message", as_agent=AGENT)
        assert f"{AGENT}'s tool access" in e.value.detail
        assert_tool_allowed(sibling, "send_message")  # its own list is empty

    def test_unknown_tool_name_is_a_programming_error(self):
        with pytest.raises(ValueError):
            assert_tool_allowed(None, "chat_with_agent")


# ---------------------------------------------------------------------------
# Every route refuses, through its real router
# ---------------------------------------------------------------------------

# Imported at collection, beside `dependencies`, so the routes depend on the
# same `get_current_user` object this file overrides.
import routers.messages as messages  # noqa: E402
import routers.telegram as telegram  # noqa: E402
import routers.slack as slack  # noqa: E402
import routers.notifications as notifications  # noqa: E402
import routers.agents as agents  # noqa: E402
import routers.voip as voip  # noqa: E402
import routers.agent_files as agent_files  # noqa: E402
import routers.reports as reports  # noqa: E402
from shared_sessions import router as rooms  # noqa: E402


def _routes():
    return [
        ("send_message", messages.router, "post", f"/api/agents/{AGENT}/messages", {"text": "hi", "recipient_email": "someone@example.com"}, None),
        ("send_group_message", telegram.auth_router, "post",
         f"/api/agents/{AGENT}/telegram/groups/-100/messages", {"message": "hi"}, None),
        ("send_group_message", slack.auth_router, "post",
         f"/api/agents/{AGENT}/slack/channels/C1/messages", {"message": "hi"}, None),
        ("send_notification", notifications.router, "post", "/api/notifications",
         {"notification_type": "info", "title": "t"}, None),
        ("send_voice_reply", agents.router, "post", f"/api/agents/{AGENT}/voice-reply",
         {"text": "hi", "execution_id": "e1"}, None),
        ("call_user", voip.auth_router, "post", f"/api/agents/{AGENT}/voip/call", {"to_number": "+15550100"}, None),
        ("post_to_room", rooms.router, "post", "/api/rooms/r1/messages", {"content": "hi"}, "room"),
        ("share_file", agent_files.router, "post", f"/api/agents/{AGENT}/shared-files", {"filename": "a.txt"}, None),
        ("report", reports.router, "post", f"/api/agents/{AGENT}/reports",
         {"report_type": "recon.digest", "title": "t", "payload": {}}, None),
    ]


@pytest.mark.parametrize("idx", range(9))
def test_each_route_refuses_a_denied_agent(world, idx):
    tool, router, method, path, body, special = _routes()[idx]
    _select("strict_read_only")
    principal = _agent_principal(world)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[dependencies.get_current_user] = lambda: principal
    if special == "room":
        app.dependency_overrides[rooms.get_room_principal] = lambda: principal
    client = TestClient(app, raise_server_exceptions=False)
    res = getattr(client, method)(path, json=body)
    assert res.status_code == 403, (tool, path, res.status_code, res.text[:300])
    assert "tool access" in res.json()["detail"], res.text[:300]
    assert tool in res.json()["detail"]


# ---------------------------------------------------------------------------
# The call names the right tool and runs before the handler does anything
# ---------------------------------------------------------------------------

SINK_HANDLERS = {
    ("routers/messages.py", "send_proactive_message"): "send_message",
    ("routers/telegram.py", "send_telegram_group_message"): "send_group_message",
    ("routers/slack.py", "send_agent_slack_channel_message"): "send_group_message",
    ("routers/notifications.py", "create_notification"): "send_notification",
    ("routers/agents.py", "send_voice_reply_endpoint"): "send_voice_reply",
    ("routers/voip.py", "place_voip_call"): "call_user",
    ("shared_sessions/router.py", "post_message"): "post_to_room",
    ("routers/agent_files.py", "share_agent_file"): "share_file",
    ("routers/reports.py", "create_report"): "report",
}

# The leading statements a handler may keep before the check: its docstring,
# imports, and the own-agent / owner gate the route census reads as the first
# statement (ent#840).
_LEADING_GATES = {"assert_own_agent", "assert_agent_owner"}


def _call_name(stmt) -> Optional[str]:
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
        fn = stmt.value.func
        return fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
    return None


def _handler(rel: str, name: str):
    tree = ast.parse((_BACKEND / rel).read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{rel}::{name} not found")


# The routes that send AS their path agent, which can differ from the caller.
SENDS_AS_PATH_AGENT = {
    ("routers/messages.py", "send_proactive_message"): "agent_name",
    ("routers/telegram.py", "send_telegram_group_message"): "agent_name",
    ("routers/slack.py", "send_agent_slack_channel_message"): "name",
    ("routers/voip.py", "place_voip_call"): "agent_name",
}


@pytest.mark.parametrize("key", sorted(SENDS_AS_PATH_AGENT))
def test_a_route_that_sends_as_its_path_agent_checks_that_agent(key):
    rel, name = key
    calls = [n for n in ast.walk(_handler(rel, name)) if isinstance(n, ast.Call)
             and getattr(n.func, "id", None) == "assert_tool_allowed"]
    (call,) = calls
    as_agent = {k.arg: k.value for k in call.keywords}.get("as_agent")
    assert isinstance(as_agent, ast.Name) and as_agent.id == SENDS_AS_PATH_AGENT[key]


@pytest.mark.parametrize("key", sorted(SINK_HANDLERS))
def test_check_is_the_handlers_first_action(key):
    rel, name = key
    body = _handler(rel, name).body
    i = 0
    while i < len(body) and (
        (isinstance(body[i], ast.Expr) and isinstance(body[i].value, ast.Constant))
        or isinstance(body[i], (ast.Import, ast.ImportFrom))
        or _call_name(body[i]) in _LEADING_GATES
    ):
        i += 1
    stmt = body[i]
    assert _call_name(stmt) == "assert_tool_allowed", f"{rel}::{name} line {stmt.lineno}"
    assert stmt.value.args[1].value == SINK_HANDLERS[key]


def test_every_sink_tool_has_a_route():
    assert set(SINK_HANDLERS.values()) == set(tap.SINK_TOOLS)
