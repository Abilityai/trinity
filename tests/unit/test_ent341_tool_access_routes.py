"""Tool-access profiles through the real routes (trinity-enterprise#341).

`routers/agent_config.py` mounted on a bare app over the real schema
(`db_harness`), with the container, the live read-only apply and the audit
service stubbed on the modules the routes actually call. Pinned here:

* the tool-access GET/PUT, the Read-only shortcut and the guardrails PUT keep
  the profile and the enforced layers in step (one compare-and-set; a lost
  race is a 409, never a contradiction);
* the read-only layer is applied live and the tool list waits for a restart,
  and `restart_required` is honest (env drift, or a live apply that failed);
* the gates: owner or `agents.manage` holder, never the agent on itself, never
  the system agent; an audit row per change;
* the drift check ignores the bookkeeping key, and an untouched legacy agent's
  env is unchanged (no recreate on upgrade).

Related: docs/memory/requirements/security.md §28.10.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Optional

import pytest

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from db_harness import db_backend  # noqa: E402,F401

import dependencies  # noqa: E402
import routers.agent_config as _CFG  # noqa: E402
from database import db  # noqa: E402
from db_models import UserCreate  # noqa: E402
from services import tool_access_profiles as tap  # noqa: E402

pytestmark = pytest.mark.unit

OWNER = "ta341-owner"
OTHER = "ta341-other"
ADMIN = "ta341-admin"
AGENT = "ta341-agent"
SYSTEM = "ta341-system"

SEND = [tap.mcp_name(t) for t in tap.SEND_TOOLS]
STRICT_LIST = SEND + [tap.mcp_name("report")]


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


def _real_module(monkeypatch, name: str, filename: str) -> ModuleType:
    """The real module object, pinned into `sys.modules` for this test, even
    when a sibling unit file evicted or stubbed the entry (the routes import
    these lazily)."""
    mod = sys.modules.get(name)
    if not (isinstance(mod, ModuleType) and (getattr(mod, "__file__", "") or "").endswith(filename)):
        monkeypatch.delitem(sys.modules, name, raising=False)
        mod = importlib.import_module(name)
    monkeypatch.setitem(sys.modules, name, mod)
    return mod


class _Audit:
    def __init__(self):
        self.rows = []

    async def log(self, *args, **kwargs):
        self.rows.append(kwargs)
        return "evt"


class _Live:
    """The live read-only apply (a root `docker exec` in production)."""

    def __init__(self):
        self.calls = []
        self.succeed = True

    async def inject(self, agent_name, config=None):
        self.calls.append(("inject", agent_name, config))
        return {"success": self.succeed}

    async def remove(self, agent_name):
        self.calls.append(("remove", agent_name))
        return {"success": self.succeed}


@pytest.fixture
def world(db_backend, monkeypatch):
    monkeypatch.setattr(dependencies, "get_breaker_redis", lambda: None)
    db.create_user(UserCreate(username=OWNER, role="operator", email="ta341-owner@example.com"))
    db.create_user(UserCreate(username=OTHER, role="operator", email="ta341-other@example.com"))
    db.create_user(UserCreate(username=ADMIN, role="admin", email="ta341-admin@example.com"))
    db.register_agent_owner(AGENT, OWNER)
    db.register_agent_owner(SYSTEM, ADMIN, is_system=True)

    container = SimpleNamespace(
        status="running",
        attrs={"Config": {"Env": [], "Labels": {"trinity.agent-runtime": "claude-code"}}},
    )
    lookup = lambda _n: container  # noqa: E731

    ta = _real_module(monkeypatch, "services.agent_service.tool_access", "tool_access.py")
    ro = _real_module(monkeypatch, "services.agent_service.read_only", "read_only.py")
    helpers = _real_module(monkeypatch, "services.agent_service.helpers", "helpers.py")
    live, audit = _Live(), _Audit()
    monkeypatch.setattr(ta, "get_agent_container", lookup)
    monkeypatch.setattr(ta, "inject_read_only_hooks", live.inject)
    monkeypatch.setattr(ta, "remove_read_only_hooks", live.remove)
    monkeypatch.setattr(ta, "platform_audit_service", audit)
    monkeypatch.setattr(ro, "get_agent_container", lookup)
    monkeypatch.setattr(_CFG, "get_agent_container", lookup)

    app = FastAPI()
    app.include_router(_CFG.router)
    client = TestClient(app)
    ids = {u: db.get_user_by_username(u)["id"] for u in (OWNER, OTHER, ADMIN)}

    def as_(username=OWNER, role="operator", **fields):
        base = {"id": ids[username], "username": username,
                "email": f"{username}@example.com", "role": role}
        base.update(fields)
        app.dependency_overrides[dependencies.get_current_user] = lambda: _Principal(**base)

    as_()

    def set_env(cfg):
        """The container now runs `cfg` as its AGENT_GUARDRAILS."""
        container.attrs["Config"]["Env"] = [f"AGENT_GUARDRAILS={json.dumps(cfg)}"] if cfg else []

    yield SimpleNamespace(client=client, as_=as_, container=container, live=live,
                          audit=audit, set_env=set_env, ta=ta, helpers=helpers)
    app.dependency_overrides.clear()


def _row(agent=AGENT):
    return db.get_tool_access_row(agent)


def _put(w, body, agent=AGENT):
    return w.client.put(f"/api/agents/{agent}/tool-access", json=body)


# ---------------------------------------------------------------------------
# GET /tool-access
# ---------------------------------------------------------------------------

class TestRead:
    def test_untouched_agent_is_full_with_five_cards(self, world):
        res = world.client.get(f"/api/agents/{AGENT}/tool-access")
        assert res.status_code == 200, res.text
        body = res.json()
        assert (body["profile"], body["derived"], body["applied"]) == ("full", True, True)
        assert [p["id"] for p in body["profiles"]] == list(tap.PROFILE_IDS)
        assert body["runtime"] == "claude-code" and body["restart_required"] is False
        strict = next(p for p in body["profiles"] if p["id"] == "strict_read_only")
        assert "shell commands aren't covered" in strict["coverage"]

    def test_coverage_follows_the_runtime(self, world):
        world.container.attrs["Config"]["Labels"]["trinity.agent-runtime"] = "codex"
        body = world.client.get(f"/api/agents/{AGENT}/tool-access").json()
        strict = next(p for p in body["profiles"] if p["id"] == "strict_read_only")
        assert "every shell command" in strict["coverage"]

    def test_legacy_read_only_agent_reads_strict_not_applied(self, world):
        db.set_read_only_mode(AGENT, True, None)
        body = world.client.get(f"/api/agents/{AGENT}/tool-access").json()
        assert (body["profile"], body["applied"]) == ("strict_read_only", False)

    def test_not_the_owner_gets_a_uniform_404(self, world):
        world.as_(OTHER)
        assert world.client.get(f"/api/agents/{AGENT}/tool-access").status_code == 404


# ---------------------------------------------------------------------------
# PUT /tool-access
# ---------------------------------------------------------------------------

class TestSelect:
    def test_workspace_only_writes_the_list_and_waits_for_a_restart(self, world):
        res = _put(world, {"profile": "workspace_only"})
        assert res.status_code == 200, res.text
        body = res.json()
        assert (body["profile"], body["applied"], body["restart_required"]) == ("workspace_only", True, True)
        row = _row()
        assert row["guardrails"]["disallowed_tools"] == SEND and row["read_only"] is False
        assert world.live.calls == []  # the read-only layer did not move

    def test_strict_applies_read_only_live_with_default_patterns(self, world):
        res = _put(world, {"profile": "strict_read_only"})
        assert res.status_code == 200, res.text
        assert res.json()["read_only_applied"] is True
        row = _row()
        assert row["read_only"] is True and row["guardrails"]["disallowed_tools"] == STRICT_LIST
        assert row["read_only_config"]["blocked_patterns"]  # defaults written
        assert world.live.calls[0][0] == "inject"

    def test_no_restart_when_the_container_already_runs_the_list(self, world):
        _put(world, {"profile": "workspace_only"})
        world.set_env(_row()["guardrails"])
        res = _put(world, {"profile": "read_only_report"})  # same list, read-only on
        assert res.json()["restart_required"] is False

    def test_a_failed_live_apply_is_reported_as_a_restart(self, world):
        _put(world, {"profile": "workspace_only"})
        world.set_env(_row()["guardrails"])
        world.live.succeed = False
        body = _put(world, {"profile": "read_only_report"}).json()
        assert body["read_only_applied"] is False and body["restart_required"] is True

    def test_stopped_agent_applies_nothing_live(self, world):
        world.container.status = "exited"
        assert _put(world, {"profile": "strict_read_only"}).status_code == 200
        assert world.live.calls == [] and _row()["read_only"] is True

    def test_custom_validation_is_a_named_400(self, world):
        res = _put(world, {"profile": "custom", "custom": {"read_only": False, "disallowed_tools": [""]}})
        assert res.status_code == 400 and "custom.disallowed_tools" in res.json()["detail"]
        assert _row()["guardrails"] == {}

    def test_unknown_profile_is_a_named_400(self, world):
        res = _put(world, {"profile": "everything"})
        assert res.status_code == 400 and "profile" in res.json()["detail"]

    def test_turning_read_only_off_keeps_the_patterns(self, world):
        db.set_read_only_mode(AGENT, True, {"blocked_patterns": ["*.md"], "allowed_patterns": []})
        _put(world, {"profile": "full"})
        row = _row()
        assert row["read_only"] is False
        assert row["read_only_config"] == {"blocked_patterns": ["*.md"], "allowed_patterns": []}

    def test_audit_row_names_actor_and_change(self, world):
        _put(world, {"profile": "workspace_only"})
        (entry,) = world.audit.rows
        assert entry["target_id"] == AGENT and entry["event_action"] == "tool_access_change"
        assert entry["details"]["from"] == "full" and entry["details"]["to"] == "workspace_only"
        assert entry["actor_user"].username == OWNER


class TestReviewFixes:
    def test_turn_cap_drift_is_not_a_tool_access_restart(self, world):
        _put(world, {"profile": "workspace_only"})
        world.set_env({**_row()["guardrails"], "max_turns_chat": 999})
        body = world.client.get(f"/api/agents/{AGENT}/tool-access").json()
        assert body["restart_required"] is False

    def test_an_unreadable_container_env_reads_as_pending(self, world):
        world.container.attrs["Config"]["Env"] = ["AGENT_GUARDRAILS={not json"]
        assert world.client.get(f"/api/agents/{AGENT}/tool-access").json()["restart_required"] is True

    def test_a_concurrent_writer_that_finished_first_is_converged_to(self, world):
        """Two writers win their compare-and-set in turn; their execs race. The
        one whose exec ends last re-reads the row and applies what is stored."""
        real_inject = world.live.inject

        async def inject_while_someone_turns_it_off(name, config=None):
            await real_inject(name, config)
            db.set_read_only_mode(AGENT, False, None)   # the other tab's OFF lands first
            return {"success": True}

        world.ta.inject_read_only_hooks = inject_while_someone_turns_it_off
        _put(world, {"profile": "strict_read_only"})
        assert [c[0] for c in world.live.calls] == ["inject", "remove"]

    def test_audit_row_carries_the_before_state(self, world):
        _put(world, {"profile": "custom", "custom": {"read_only": False, "disallowed_tools": ["Bash"]}})
        _put(world, {"profile": "custom", "custom": {"read_only": True, "disallowed_tools": ["Edit"]}})
        last = world.audit.rows[-1]["details"]
        assert last["from_disallowed_tools"] == ["Bash"] and last["disallowed_tools"] == ["Edit"]
        assert last["from_read_only"] is False and last["read_only"] is True

    def test_get_guardrails_shows_the_profile_id_only(self, world):
        _put(world, {"profile": "custom", "custom": {"read_only": False, "disallowed_tools": ["Bash"]}})
        _put(world, {"profile": "strict_read_only"})
        shown = world.client.get(f"/api/agents/{AGENT}/guardrails").json()["guardrails"]
        assert shown["tool_access"] == {"profile": "strict_read_only"}
        assert "Bash" not in json.dumps(shown)


class TestGates:
    def test_not_the_owner_is_refused_and_nothing_moves(self, world):
        world.as_(OTHER)
        assert _put(world, {"profile": "strict_read_only"}).status_code in (403, 404)
        assert _row()["read_only"] is False

    def test_the_agent_on_itself_is_refused(self, world):
        world.as_(OWNER, agent_name=AGENT, mcp_scope="agent")
        assert _put(world, {"profile": "full"}).status_code == 403
        assert _row()["guardrails"] == {}

    def test_the_system_agent_is_refused(self, world):
        world.as_(ADMIN, role="admin")
        res = _put(world, {"profile": "strict_read_only"}, agent=SYSTEM)
        assert res.status_code == 403
        assert _row(SYSTEM)["read_only"] is False


class TestCompareAndSet:
    def test_a_write_that_lost_the_race_is_a_409_and_changes_nothing(self, world):
        real_read = db.get_tool_access_row

        def read_then_someone_else_writes(name):
            row = real_read(name)
            db.set_guardrails_config(name, {"max_turns_chat": 9})  # the other tab
            return row

        db.get_tool_access_row = read_then_someone_else_writes
        try:
            res = _put(world, {"profile": "strict_read_only"})
        finally:
            del db.get_tool_access_row  # back to the class method
        assert res.status_code == 409
        row = _row()
        assert row["guardrails"] == {"max_turns_chat": 9} and row["read_only"] is False


# ---------------------------------------------------------------------------
# PUT /read-only — the header and list toggle (ruling 5)
# ---------------------------------------------------------------------------

class TestShortcut:
    def _toggle(self, w, enabled, **extra):
        return w.client.put(f"/api/agents/{AGENT}/read-only", json={"enabled": enabled, **extra})

    def test_on_then_off_returns_to_workspace_only(self, world):
        _put(world, {"profile": "workspace_only"})
        on = self._toggle(world, True)
        assert on.status_code == 200, on.text
        assert on.json()["profile"] == "strict_read_only" and on.json()["enabled"] is True
        assert _row()["read_only"] is True and _row()["guardrails"]["disallowed_tools"] == STRICT_LIST
        off = self._toggle(world, False)
        assert off.json()["profile"] == "workspace_only" and off.json()["enabled"] is False
        assert _row()["read_only"] is False and _row()["guardrails"]["disallowed_tools"] == SEND

    def test_get_reports_the_toggle_as_strict(self, world):
        _put(world, {"profile": "read_only_report"})
        body = world.client.get(f"/api/agents/{AGENT}/read-only").json()
        assert body["enabled"] is False and body["profile"] == "read_only_report"
        assert body["read_only_layer"] is True

    def test_off_on_read_only_report_is_a_409(self, world):
        _put(world, {"profile": "read_only_report"})
        res = self._toggle(world, False)
        assert res.status_code == 409 and "Read-only + report" in res.json()["detail"]
        assert _row()["read_only"] is True

    def test_off_when_off_is_unchanged(self, world):
        res = self._toggle(world, False)
        assert res.status_code == 200 and res.json()["profile"] == "full"
        assert world.audit.rows == []

    def test_legacy_read_only_agent_toggled_off_is_full(self, world):
        db.set_read_only_mode(AGENT, True, None)
        assert self._toggle(world, False).json()["profile"] == "full"
        assert _row()["read_only"] is False

    def test_on_that_would_hand_back_removed_tools_is_a_409(self, world):
        db.set_guardrails_config(AGENT, {"disallowed_tools": ["Bash"]})
        db.set_read_only_mode(AGENT, True, None)
        res = self._toggle(world, True)
        assert res.status_code == 409 and "Bash" in res.json()["detail"]
        assert _row()["guardrails"] == {"disallowed_tools": ["Bash"]}
        assert world.live.calls == [] and world.audit.rows == []

    def test_patterns_sent_with_on_are_stored(self, world):
        cfg = {"blocked_patterns": ["*.py"], "allowed_patterns": ["out/*"]}
        assert self._toggle(world, True, config=cfg).status_code == 200
        assert _row()["read_only_config"] == cfg
        assert world.live.calls[-1] == ("inject", AGENT, cfg)

    def test_the_system_agent_is_refused(self, world):
        world.as_(ADMIN, role="admin")
        res = world.client.put(f"/api/agents/{SYSTEM}/read-only", json={"enabled": True})
        assert res.status_code == 403


# ---------------------------------------------------------------------------
# PUT /guardrails — never forges or wipes the profile
# ---------------------------------------------------------------------------

class TestGuardrailsPut:
    def _gput(self, w, body):
        return w.client.put(f"/api/agents/{AGENT}/guardrails", json=body)

    def test_turn_caps_save_keeps_the_profile_and_its_list(self, world):
        _put(world, {"profile": "workspace_only"})
        res = self._gput(world, {"max_turns_chat": 50, "tool_access": {"profile": "full"}})
        assert res.status_code == 200, res.text
        g = _row()["guardrails"]
        assert g["max_turns_chat"] == 50 and g["disallowed_tools"] == SEND
        assert g["tool_access"]["profile"] == "workspace_only"

    def test_empty_body_keeps_the_profile(self, world):
        _put(world, {"profile": "strict_read_only"})
        assert self._gput(world, {}).status_code == 200
        assert tap.resolve(_row()["guardrails"], _row()["read_only"])["profile"] == "strict_read_only"

    def test_a_different_list_under_a_profile_is_a_409(self, world):
        _put(world, {"profile": "workspace_only"})
        res = self._gput(world, {"disallowed_tools": ["Bash"]})
        assert res.status_code == 409
        assert _row()["guardrails"]["disallowed_tools"] == SEND

    def test_profile_less_agent_keeps_the_old_contract(self, world):
        assert self._gput(world, {"disallowed_tools": ["Bash"]}).status_code == 200
        assert _row()["guardrails"] == {"disallowed_tools": ["Bash"]}
        assert self._gput(world, {}).status_code == 200
        assert _row()["guardrails"] == {}


# ---------------------------------------------------------------------------
# Drift: the bookkeeping key never recreates; an untouched agent's env is unchanged
# ---------------------------------------------------------------------------

class TestDrift:
    def _matches(self, w):
        return w.helpers.check_guardrails_env_matches(w.container, AGENT)

    def test_profile_change_with_the_same_list_does_not_drift(self, world):
        _put(world, {"profile": "workspace_only"})
        world.set_env({"disallowed_tools": SEND})  # a container baked before the key existed
        _put(world, {"profile": "custom", "custom": {"read_only": False, "disallowed_tools": SEND}})
        assert self._matches(world) is True

    def test_untouched_legacy_read_only_agent_does_not_drift(self, world):
        db.set_guardrails_config(AGENT, {"max_turns_chat": 40})
        db.set_read_only_mode(AGENT, True, None)
        world.set_env({"max_turns_chat": 40})
        assert self._matches(world) is True

    def test_a_list_change_still_drifts(self, world):
        _put(world, {"profile": "workspace_only"})
        world.set_env({})
        assert self._matches(world) is False


# ---------------------------------------------------------------------------
# The profile rides the payloads the header and list toggles read
# ---------------------------------------------------------------------------

class TestPayloads:
    """The header (detail) and list (fleet) toggles read `tool_access_profile`,
    resolved the same way on both paths."""

    def _detail(self, monkeypatch):
        import asyncio
        import config
        import routers.agents as agents_router
        from models import User

        monkeypatch.setattr(config, "DISPATCH_BREAKER_ENABLED", False)
        monkeypatch.setattr(agents_router, "get_agent_by_name", lambda n: {
            "name": n, "status": "running", "port": 2222, "created": "2026-01-01T00:00:00Z",
            "resources": {}, "container_id": "c", "template": "local:x", "runtime": "claude-code",
        })
        owner = db.get_user_by_username(OWNER)
        user = User(id=owner["id"], username=OWNER, role="operator")
        return asyncio.run(agents_router.get_agent_endpoint(
            agent_name=AGENT, request=SimpleNamespace(), current_user=user))

    def _fleet(self, w, monkeypatch):
        from models import User
        helpers = w.helpers
        monkeypatch.setattr(helpers, "list_all_agents_fast", lambda: [{"name": AGENT, "status": "running"}])
        owner = db.get_user_by_username(OWNER)
        user = User(id=owner["id"], username=OWNER, role="operator")
        (row,) = [a for a in helpers.get_accessible_agents(user) if a["name"] == AGENT]
        return row

    @pytest.mark.parametrize("setup,expected", [
        (lambda w: None, "full"),
        (lambda w: db.set_read_only_mode(AGENT, True, None), "strict_read_only"),
        (lambda w: (db.set_guardrails_config(AGENT, {"disallowed_tools": ["Bash"]}),
                    db.set_read_only_mode(AGENT, True, None)), "custom"),
        (lambda w: _put(w, {"profile": "read_only_report"}), "read_only_report"),
    ])
    def test_detail_and_fleet_agree(self, world, monkeypatch, setup, expected):
        setup(world)
        assert self._detail(monkeypatch)["tool_access_profile"] == expected
        assert self._fleet(world, monkeypatch)["tool_access_profile"] == expected


def test_guardrails_list_bounds_match_the_custom_editor():
    """`_validate_guardrails_payload` and the tool-access Custom editor share
    one contract (≤50 entries, 1–256 chars) — the client mirrors both."""
    assert _CFG._GUARDRAILS_MAX_LIST == tap.MAX_LIST_LEN
    assert _CFG._GUARDRAILS_MAX_STRING == tap.MAX_STRING_LEN
