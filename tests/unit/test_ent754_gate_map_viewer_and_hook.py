"""trinity-enterprise#754 — the gate map tells a viewer who approves, and the
owner whether the gate is enforced inside the agent.

Two additive reads on `GET /api/agents/{name}/skill-gates` (no new route):

* `approvers: [{kind, reachable, viewer_fills}]` — every approver kind this
  install resolves, whether it reaches anyone now (the picker disables a kind
  nobody fills), and whether the CALLER is one of its people (the card's "you
  approve this"). `viewer_fills` is computed with the SAME two functions
  `skill_gate_service.enforce` decides self-approval with, so the card can
  never say "you approve this" for a run the gate would hold, or the reverse.
  Machine principals never fill a kind. No person data leaves: only booleans.
* `?probe=true` → `hook`: the agent's `/health → skill_gate_hook` (ent#752),
  read with one direct call and no breaker bookkeeping, honoured only for a
  person who may manage the agent's skills (the #3052 probe precedent). A 200
  without the field is `predates` (an image older than the hook — the common
  case the warning exists for); no answer at all is `unknown`.

Everything runs through the real route with only the gate store, the role
provider and the agent's HTTP answer faked.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

pytest.importorskip("fastapi", reason="backend venv required")
httpx = pytest.importorskip("httpx")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import routers.skill_gate as route_mod  # noqa: E402
from database import db  # noqa: E402
from models import User  # noqa: E402
from services import assignment_provider, role_addressing, skill_gate_map_service, skill_gate_service  # noqa: E402

AGENT = "gated-agent"

OWNER = User(id=1, username="owner", email="Owner@Example.com", role="user")
OTHER = User(id=2, username="viewer", email="viewer@example.com", role="user")
ADMIN = User(id=3, username="admin", email="admin@example.com", role="admin")
AGENT_KEY = User(id=1, username="owner", email="Owner@Example.com", role="user",
                 mcp_scope="agent", agent_name="sibling")
SYSTEM_KEY = User(id=3, username="admin", email="admin@example.com", role="admin", mcp_scope="system")


class FakeProvider:
    """An enterprise assignments provider answering `people_for`."""

    def __init__(self, people):
        self.people = people

    def assignment_for(self, agent_name, triggered_by):
        return None

    def people_for(self, agent_name, role):
        return {"emails": list(self.people.get(role, []))}


class FakeHealth:
    def __init__(self):
        self.answer = (200, {"status": "healthy", "skill_gate_hook": "ok"})
        self.calls = []
        self.delay = 0.0                                   # an agent slow to answer

    def client(self, agent_name, **kw):
        probe = self

        class _C:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, url):
                probe.calls.append((url, kw.get("timeout")))
                if probe.delay:
                    await asyncio.sleep(probe.delay)
                if isinstance(probe.answer, Exception):
                    raise probe.answer
                status, body = probe.answer

                class R:
                    status_code = status

                    def json(self_inner):
                        if isinstance(body, Exception):
                            raise body
                        return body
                return R()
        return _C()


@pytest.fixture
def env(monkeypatch):
    rows = [{"skill_name": "send-invoice", "approver": "primary", "deadline_hours": None,
             "origin": "set", "set_by": "owner", "set_by_agent": None, "set_at": "2026-10-08T10:00:00Z"}]
    owners = {"email": "owner@example.com"}          # what primary resolves to on OSS
    managers = {"owner", "admin"}                      # who may share / manage the agent
    health = FakeHealth()

    monkeypatch.setattr(db, "list_agent_skill_gates", lambda name: list(rows))
    monkeypatch.setattr(role_addressing, "owner_email", lambda name: owners["email"])
    monkeypatch.setattr(db, "can_user_share_agent", lambda username, name: username in managers)
    monkeypatch.setattr(assignment_provider, "_provider", None)
    monkeypatch.setattr(skill_gate_map_service, "agent_httpx_client", health.client)

    app = FastAPI()
    app.include_router(route_mod.agent_router)
    holder = {"user": OWNER}

    def _override(dependant):
        for dep in getattr(dependant, "dependencies", []):
            name = getattr(dep.call, "__name__", "")
            if name == "get_skill_gate_readable_agent_by_name":
                app.dependency_overrides[dep.call] = lambda agent_name: agent_name
            if name == "get_current_user":
                app.dependency_overrides[dep.call] = lambda: holder["user"]
            _override(dep)

    for r in app.routes:
        _override(getattr(r, "dependant", None))

    class Ctx:
        client = TestClient(app, raise_server_exceptions=False)

    ctx = Ctx()
    ctx.holder = holder
    ctx.owners = owners
    ctx.health = health
    ctx.monkeypatch = monkeypatch
    return ctx


def _get(ctx, user, **params):
    ctx.holder["user"] = user
    r = ctx.client.get(f"/api/agents/{AGENT}/skill-gates", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def _kinds(body):
    return {a["kind"]: a for a in body["approvers"]}


# ---------------------------------------------------------------------------
# approvers
# ---------------------------------------------------------------------------


def test_oss_lists_primary_only_and_the_owner_fills_it(env):
    body = _get(env, OWNER)

    assert body["approver_kinds"] == ["primary"]
    assert _kinds(body) == {"primary": {"kind": "primary", "reachable": True, "viewer_fills": True}}


def test_the_compare_is_casefolded_like_enforce(env):
    """OWNER's address is `Owner@Example.com`; primary resolves to the
    lower-case form. enforce casefolds both sides, so does this."""
    assert _kinds(_get(env, OWNER))["primary"]["viewer_fills"] is True


def test_another_viewer_sees_the_kind_but_does_not_fill_it(env):
    k = _kinds(_get(env, OTHER))["primary"]

    assert k["reachable"] is True and k["viewer_fills"] is False


def test_an_admin_who_is_not_the_owner_does_not_fill_primary(env):
    assert _kinds(_get(env, ADMIN))["primary"]["viewer_fills"] is False


def test_an_owner_without_an_email_reaches_nobody(env):
    env.owners["email"] = None

    k = _kinds(_get(env, OWNER))["primary"]

    assert k == {"kind": "primary", "reachable": False, "viewer_fills": False}


def test_an_agent_key_never_fills_a_kind_even_for_its_owners_address(env):
    """An agent key resolves to its owner carrying the owner's email; enforce
    never lets it self-approve (requester_from_principal makes it an agent)."""
    assert _kinds(_get(env, AGENT_KEY))["primary"]["viewer_fills"] is False


def test_enterprise_lists_both_kinds_with_their_own_reach(env):
    env.monkeypatch.setattr(assignment_provider, "_provider",
                            FakeProvider({"primary": ["viewer@example.com"], "approver": []}))

    body = _get(env, OTHER)

    assert body["approver_kinds"] == ["primary", "approver"]
    assert _kinds(body) == {
        "primary": {"kind": "primary", "reachable": True, "viewer_fills": True},
        "approver": {"kind": "approver", "reachable": False, "viewer_fills": False},
    }


def test_viewer_fills_agrees_with_what_enforce_does(env):
    """The point of reusing enforce's functions: for the same principal and
    kind, "you approve this" is true exactly when a gated request would
    self-approve."""
    import asyncio

    gates = {"send-invoice": skill_gate_service.SkillGate(approver="primary", deadline_hours=None)}
    for user in (OWNER, OTHER, ADMIN, AGENT_KEY):
        fills = _kinds(_get(env, user))["primary"]["viewer_fills"]
        requester = skill_gate_service.requester_from_principal(user)
        try:
            # refuse_only: a request that would NOT self-approve is refused
            # outright (nothing is raised or written), so the decision is pure.
            decision = asyncio.run(skill_gate_service.enforce(
                AGENT, request_text="/send-invoice now", requester=requester,
                triggered_by="manual", gates=gates, refuse_only=True))
            self_approves = decision.self_approved_by is not None
        except skill_gate_service.SkillGateRefused:
            self_approves = False
        assert fills is self_approves, user.username


def test_no_person_data_in_the_new_field(env):
    body = _get(env, OWNER)

    assert set(body["approvers"][0]) == {"kind", "reachable", "viewer_fills"}
    assert "example.com" not in str(body["approvers"]).lower()


# ---------------------------------------------------------------------------
# hook probe
# ---------------------------------------------------------------------------


def test_no_probe_no_health_call(env):
    body = _get(env, OWNER)

    assert body["hook"] is None
    assert env.health.calls == []


def test_probe_for_the_owner_reads_health_once_with_a_short_timeout(env):
    body = _get(env, OWNER, probe="true")

    assert body["hook"] == "ok"
    assert env.health.calls == [(f"http://agent-{AGENT}:8000/health", 3.0)]


@pytest.mark.parametrize("user", [OTHER, AGENT_KEY])
def test_probe_is_ignored_for_anyone_who_cannot_manage(env, user):
    body = _get(env, user, probe="true")

    assert body["hook"] is None
    assert env.health.calls == []


@pytest.mark.parametrize("principal", [SYSTEM_KEY, AGENT_KEY], ids=["system_key", "agent_key_with_skills_manage"])
def test_probe_is_a_persons_even_for_a_key_that_may_manage_skills(env, principal):
    """`can_manage_agent_skills` admits both — a system-scoped key, and an agent
    key holding `skills.manage` on an agent its owner owns — so only the
    person check keeps the in-agent probe from a machine caller."""
    env.monkeypatch.setattr(db, "agent_has_capability", lambda agent, cap: agent == "sibling")
    assert route_mod.can_manage_agent_skills(principal, AGENT) is True     # the manage check alone admits it

    body = _get(env, principal, probe="true")

    assert body["hook"] is None
    assert env.health.calls == []


def test_an_unexpected_probe_failure_is_unknown_and_says_why(env, caplog):
    """No answer is `unknown` (never a warning), but a fault that recurs on
    every call — #1159's fail-closed client, say — must leave a trace, or the
    owner would never see a warning and nobody would know why."""
    import logging

    def _boom(name, **kw):
        raise RuntimeError("agent auth secret missing")

    env.monkeypatch.setattr(skill_gate_map_service, "agent_httpx_client", _boom)
    with caplog.at_level(logging.DEBUG, logger=skill_gate_map_service.logger.name):
        body = _get(env, OWNER, probe="true")

    assert body["hook"] == "unknown"
    assert any(r.exc_info for r in caplog.records if "hook probe" in r.getMessage())


def test_the_probe_timeout_caps_the_whole_probe(env):
    """httpx's timeout is per phase: an agent trickling its answer held one
    probe far past it (PR review). The cap is on the whole probe."""
    import time

    env.monkeypatch.setattr(skill_gate_map_service, "HOOK_PROBE_TIMEOUT_SECONDS", 0.05)
    env.health.delay = 1.0

    started = time.monotonic()
    body = _get(env, OWNER, probe="true")

    assert body["hook"] == "unknown"
    assert time.monotonic() - started < 0.5


def test_probe_is_honoured_for_an_admin(env):
    assert _get(env, ADMIN, probe="true")["hook"] == "ok"


@pytest.mark.parametrize("value", ["missing", "not_root_owned", "writable", "unsupported_runtime"])
def test_each_agent_state_passes_through(env, value):
    env.health.answer = (200, {"skill_gate_hook": value})

    assert _get(env, OWNER, probe="true")["hook"] == value


def test_a_200_without_the_field_is_an_image_that_predates_the_hook(env):
    env.health.answer = (200, {"status": "healthy"})

    assert _get(env, OWNER, probe="true")["hook"] == "predates"


@pytest.mark.parametrize("answer", [
    httpx.ConnectError("stopped"),
    httpx.ReadTimeout("slow"),
    (503, {"skill_gate_hook": "ok"}),
    (200, ValueError("not json")),
    (200, ["not", "a", "dict"]),
    (200, {"skill_gate_hook": "something-new"}),
    (200, {"skill_gate_hook": None}),
])
def test_no_usable_answer_is_unknown_never_not_ok(env, answer):
    env.health.answer = answer

    assert _get(env, OWNER, probe="true")["hook"] == "unknown"
