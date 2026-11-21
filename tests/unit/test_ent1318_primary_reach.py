"""A person seated as primary on an agent has it work in full for them
(abilityai/trinity-enterprise#1318, operator ruling 2026-11-20).

Before this, a turn's reach was owner/admin or "shared with"; seats were never
read, so a primary who is not the owner or an admin got no shell, none of the
owner's MCP servers, no standing work, and no Gemini/Codex turn at all — on the
agent built for them, and at every agent-to-agent hop.

The OSS rule lives in `services/turn_tier_service._person_tier`: after the
owner/admin and the ent#1148 member rule, a registered primary-seat provider
(the private assignments module) may rate the person `(owner, primary, P)`.
The owner turns it off per agent with the `primary_reach` guardrail (default
on); the refusal then says so. Ruling 4: the permission edge between two agents
never decides reach — the provider is asked only about (person, this agent).
Ruling 3: managing the agent stays with the owner and admins — the agent key in
a primary's turn may set up standing work, but is still refused every other
owner-tier platform route.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from services import turn_tier_service as tts  # noqa: E402

pytestmark = pytest.mark.unit

A, B, C = "agent-a", "agent-b", "agent-c"
OWNER, MATE, ADMIN, OTHER = 1, 9, 3, 7
USERS = {
    OWNER: {"id": OWNER, "username": "owner", "role": "user", "email": "owner@example.com"},
    MATE: {"id": MATE, "username": "mate", "role": "user", "email": "mate@example.com"},
    ADMIN: {"id": ADMIN, "username": "admin", "role": "admin", "email": "admin@example.com"},
    OTHER: {"id": OTHER, "username": "other", "role": "user", "email": "other@example.com"},
}
BY_EMAIL = {u["email"]: u for u in USERS.values()}
BY_NAME = {u["username"]: u for u in USERS.values()}


class _Db:
    def __init__(self, *, owners=None, shares=None, guardrails=None):
        self.owners = owners if owners is not None else {A: OWNER, B: OWNER, C: OWNER}
        self.shares = shares if shares is not None else {("mate", A), ("mate", B), ("mate", C)}
        self.guardrails = guardrails or {}
        self.executions = {}
        self.schedules = {}
        self.running = []

    def get_user_by_id(self, uid):
        return USERS.get(int(uid))

    def get_user_by_email(self, email):
        return BY_EMAIL.get(email)

    def get_execution(self, run):
        return self.executions.get(run)

    def get_schedule(self, sid):
        return self.schedules.get(sid)

    def get_guardrails_config(self, agent):
        return self.guardrails.get(agent, {})

    def get_running_turn_attribution(self, agent):
        return [r for r in self.running if r["agent_name"] == agent]

    def get_agent_owner(self, agent):
        oid = self.owners.get(agent)
        return None if oid is None else {"owner_id": oid, "owner_username": USERS[oid]["username"]}

    def can_user_share_agent(self, username, agent):
        user = BY_NAME.get(username)
        return bool(user) and (user["role"] == "admin" or self.owners.get(agent) == user["id"])

    def can_user_access_agent(self, username, agent):
        return self.can_user_share_agent(username, agent) or (username, agent) in self.shares


def _row(agent, *, uid=MATE, caller=None, trigger="user", rid="e1", channel=None, email=None,
         schedule_id="__manual__"):
    return {"id": rid, "agent_name": agent, "source_user_id": uid, "triggered_by": trigger,
            "schedule_id": schedule_id, "source_channel": channel, "source_user_email": email,
            "source_agent_name": caller}


class _Seats:
    """The private module's answer: who is primary where. Records every call."""

    def __init__(self, primaries):
        self.primaries = set(primaries)
        self.calls = []

    def __call__(self, db, user_id, agent_name, *extra):
        self.calls.append((user_id, agent_name) + tuple(extra))
        return (user_id, agent_name) in self.primaries


@pytest.fixture(autouse=True)
def _no_providers():
    tts.clear_person_tier_providers()
    yield
    tts.clear_person_tier_providers()


def _seat(*pairs):
    seats = _Seats(pairs)
    tts.register_primary_seat_provider(seats)
    return seats


# ---- AC1/AC6: a primary's own turn runs in full ----------------------------------------

class TestPrimaryTurn:
    def test_a_primarys_direct_turn_is_owner_tier(self):
        db = _Db()
        _seat((MATE, A))
        tier, kind, person = tts.turn_actor(db, A, execution=_row(A))
        assert (tier, kind, person) == (tts.OWNER, tts.PRIMARY, MATE)
        assert tts.shell_allowed(db, A, tier, kind) is True          # shell, MCP, Gemini/Codex
        assert tts.restricts_standing_work(db, A, tier, kind) is False

    def test_a_primarys_workspace_turn_and_schedule_run_alike(self):
        db = _Db()
        _seat((MATE, A))
        ws = _row(A, uid=None, channel=tts.PORTAL_SOURCE_CHANNEL, email="mate@example.com")
        assert tts.turn_actor(db, A, execution=ws) == (tts.OWNER, tts.PRIMARY, MATE)
        db.schedules["s1"] = {"agent_name": A, "owner_id": MATE}
        cron = _row(A, uid=None, trigger="schedule", schedule_id="s1")
        assert tts.turn_actor(db, A, execution=cron) == (tts.OWNER, tts.PRIMARY, MATE)

    def test_primary_elsewhere_gives_nothing_here(self):
        db = _Db()
        _seat((MATE, B))
        assert tts.turn_actor(db, A, execution=_row(A)) == (tts.SHARE, tts.PERSON, MATE)

    def test_the_owner_and_an_admin_are_rated_as_before(self):
        db = _Db()
        seats = _seat((MATE, A))
        assert tts.turn_actor(db, A, execution=_row(A, uid=OWNER)) == (tts.OWNER, None, None)
        assert tts.turn_actor(db, A, execution=_row(A, uid=ADMIN)) == (tts.OWNER, None, None)
        assert seats.calls == []                                       # never asked for them

    def test_the_member_rule_answers_first(self):
        db = _Db()
        tts.register_person_tier_provider(lambda db, uid, agent: (uid, agent) == (MATE, A))
        _seat((MATE, A))
        assert tts.turn_actor(db, A, execution=_row(A)) == (tts.OWNER, tts.MEMBER, MATE)


# ---- AC2: every agent-to-agent hop -------------------------------------------------------

class TestHops:
    def _principal(self, agent, run):
        return SimpleNamespace(id=OWNER, mcp_scope="agent", agent_name=agent, _acting_execution_id=run)

    def test_a_primarys_turn_dispatches_as_the_primary_and_the_callee_rerates(self):
        """Mira -> Finance -> a third agent, all in the primary's turn. Each
        callee rates the person; where they are primary, the playbook runs."""
        db = _Db()
        _seat((MATE, A), (MATE, B), (MATE, C))
        db.executions["e-a"] = _row(A, rid="e-a")
        # The caller's key dispatches: the row on B carries MATE, not the owner.
        assert tts.turn_origin_user_id(db, self._principal(A, "e-a")) == MATE
        db.executions["e-b"] = _row(B, rid="e-b", caller=A)
        assert tts.turn_actor(db, B, "e-b") == (tts.OWNER, tts.PRIMARY, MATE)
        assert tts.turn_origin_user_id(db, self._principal(B, "e-b")) == MATE
        db.executions["e-c"] = _row(C, rid="e-c", caller=B)
        assert tts.turn_actor(db, C, "e-c") == (tts.OWNER, tts.PRIMARY, MATE)

    def test_a_hop_to_an_agent_where_the_person_is_only_shared_stays_share(self):
        db = _Db()
        _seat((MATE, A))
        db.executions["e-b"] = _row(B, rid="e-b", caller=A)
        assert tts.turn_actor(db, B, "e-b") == (tts.SHARE, tts.PERSON, MATE)


# ---- ruling 4: the edge never decides reach ---------------------------------------------

class TestEdgeNeverDecides:
    def test_owning_the_calling_agent_lifts_nothing_on_a_shared_callee(self):
        """MATE owns A and is merely shared B. A row on B dispatched by A for
        MATE stays share: owning an agent with an edge to B is no seat on B."""
        db = _Db(owners={A: MATE, B: OWNER, C: OWNER}, shares={("mate", B)})
        seats = _seat((MATE, A))
        row = _row(B, caller=A)
        assert tts.turn_actor(db, B, execution=row) == (tts.SHARE, tts.PERSON, MATE)
        assert all(call == (MATE, B) for call in seats.calls)

    def test_the_provider_is_asked_only_about_the_person_and_this_agent(self):
        db = _Db()
        seats = _seat((MATE, B))
        with_caller = tts.turn_actor(db, B, execution=_row(B, caller=A))
        without = tts.turn_actor(db, B, execution=_row(B, caller=None))
        assert with_caller == without == (tts.OWNER, tts.PRIMARY, MATE)
        assert set(seats.calls) == {(MATE, B)}


# ---- AC3/AC5: the owner's switch -------------------------------------------------------

class TestSwitch:
    def test_default_is_on(self):
        db = _Db()
        assert tts.primary_reach_allowed(db, A) is True

    def test_off_rates_the_primary_as_a_shared_person_and_says_why(self):
        db = _Db(guardrails={A: {tts.PRIMARY_REACH_KEY: False}})
        _seat((MATE, A))
        tier, kind, person = tts.turn_actor(db, A, execution=_row(A))
        assert (tier, kind, person) == (tts.SHARE, tts.PERSON, MATE)
        assert tts.shell_allowed(db, A, tier, kind) is False
        assert tts.restricts_standing_work(db, A, tier, kind) is True
        assert tts.primary_reach_off(db, A, person) is True

    def test_off_says_nothing_about_a_shared_person_who_is_not_primary(self):
        db = _Db(guardrails={A: {tts.PRIMARY_REACH_KEY: False}})
        _seat((OTHER, A))
        assert tts.primary_reach_off(db, A, MATE) is False

    def test_an_unreadable_setting_keeps_the_default(self):
        db = _Db()
        db.get_guardrails_config = lambda agent: (_ for _ in ()).throw(RuntimeError("down"))
        assert tts.primary_reach_allowed(db, A) is True


# ---- AC4/AC8: everyone else unchanged; no module, no change -----------------------------

class TestUnchanged:
    @pytest.mark.parametrize("provider", [
        None,
        lambda db, uid, agent: (_ for _ in ()).throw(RuntimeError("boom")),
        lambda db, uid, agent: "yes",
        lambda db, uid, agent: 1,
    ])
    def test_no_provider_or_an_unclear_answer_rates_as_today(self, provider):
        db = _Db()
        if provider is not None:
            tts.register_primary_seat_provider(provider)
        assert tts.turn_actor(db, A, execution=_row(A)) == (tts.SHARE, tts.PERSON, MATE)
        assert tts.primary_reach_off(db, A, MATE) is False

    def test_a_primary_kind_carries_its_person_forward(self):
        assert tts._is_the_owners_own((tts.OWNER, tts.PRIMARY, MATE)) is False


# ---- the route the container asks -------------------------------------------------------

class TestTierRoute:
    def _call(self, monkeypatch, db, run):
        import routers.agents as agents_router
        from services import heartbeat_service
        monkeypatch.setattr(agents_router, "db", db, raising=False)
        db.validate_mcp_api_key = lambda key, track_usage=False: {"scope": "agent", "agent_name": A}
        monkeypatch.setattr(heartbeat_service, "authorize_heartbeat", lambda res, agent: True)
        request = SimpleNamespace(headers={"Authorization": "Bearer k"})
        return asyncio.run(agents_router.agent_execution_turn_tier(A, run, request))

    def test_a_primarys_turn_may_run_commands(self, monkeypatch):
        db = _Db()
        _seat((MATE, A))
        db.executions["e1"] = _row(A)
        answer = self._call(monkeypatch, db, "e1")
        assert answer["tier"] == "owner" and answer["shell"] is True
        assert answer.get("reason") is None

    def test_with_the_switch_off_the_answer_names_it(self, monkeypatch):
        db = _Db(guardrails={A: {tts.PRIMARY_REACH_KEY: False}})
        _seat((MATE, A))
        db.executions["e1"] = _row(A)
        answer = self._call(monkeypatch, db, "e1")
        assert answer["tier"] == "share" and answer["shell"] is False
        assert answer["reason"] == "primary_off"

    def test_a_shared_person_gets_no_reason(self, monkeypatch):
        db = _Db(guardrails={A: {tts.PRIMARY_REACH_KEY: False}})
        db.executions["e1"] = _row(A)
        answer = self._call(monkeypatch, db, "e1")
        assert answer["shell"] is False and answer.get("reason") is None


# ---- ruling 3: the agent key's platform routes in a primary's turn ----------------------

class TestFence:
    @pytest.fixture
    def fenced(self, monkeypatch):
        import dependencies as dep
        db = _Db()
        for name in ("get_execution", "get_user_by_id", "get_user_by_email", "get_schedule",
                     "get_guardrails_config", "get_running_turn_attribution",
                     "can_user_share_agent", "can_user_access_agent", "get_agent_owner"):
            monkeypatch.setattr(dep.db, name, getattr(db, name))
        db.executions["e-primary"] = _row(A, rid="e-primary")
        db.executions["e-owner"] = _row(A, rid="e-owner", uid=OWNER)
        return dep, db

    @staticmethod
    def _key(run):
        return SimpleNamespace(
            id=OWNER, username="owner", role="user", mcp_scope="agent", agent_name=A,
            connector_agent=None, portal_delegate=False, is_event_loopback=False,
            vouched_source_agent=None, _acting_execution_id=run,
        )

    class _Req:
        def __init__(self, method, path):
            self.method, self.scope = method, {"path": path}

    def test_a_primarys_turn_may_set_up_standing_work(self, fenced):
        dep, db = fenced
        _seat((MATE, A))
        db.running = [db.executions["e-primary"]]
        dep._enforce_standing_work_fence(self._Req("POST", f"/api/agents/{A}/schedules"),
                                         self._key("e-primary"))
        dep._enforce_standing_work_fence(self._Req("POST", f"/api/agents/{A}/reminders"),
                                         self._key("e-primary"))

    def test_a_primarys_turn_may_not_manage_the_agent(self, fenced):
        """Owner-tier routes other than standing work stay the owner's (ruling 3),
        by the header and by the ledger when the header is left off."""
        dep, db = fenced
        _seat((MATE, A))
        db.running = [db.executions["e-primary"]]
        for run in ("e-primary", None, "e-owner"):
            with pytest.raises(dep.HTTPException) as exc:
                dep._enforce_standing_work_fence(self._Req("POST", "/api/agents"), self._key(run))
            assert exc.value.status_code == 403

    def test_the_owners_turn_alone_keeps_both(self, fenced):
        dep, db = fenced
        _seat((MATE, A))
        db.running = [db.executions["e-owner"]]
        dep._enforce_standing_work_fence(self._Req("POST", "/api/agents"), self._key("e-owner"))
        dep._enforce_standing_work_fence(self._Req("POST", f"/api/agents/{A}/schedules"),
                                         self._key("e-owner"))

    def test_switch_off_refuses_standing_work_in_plain_words(self, fenced):
        dep, db = fenced
        db.guardrails[A] = {tts.PRIMARY_REACH_KEY: False}
        _seat((MATE, A))
        db.running = [db.executions["e-primary"]]
        with pytest.raises(dep.HTTPException) as exc:
            dep._enforce_standing_work_fence(self._Req("POST", f"/api/agents/{A}/schedules"),
                                             self._key("e-primary"))
        assert exc.value.status_code == 403
        assert exc.value.detail == dep.STANDING_WORK_PRIMARY_OFF

    def test_a_shared_person_who_is_not_primary_is_refused_as_today(self, fenced):
        dep, db = fenced
        _seat((OTHER, A))
        db.running = [db.executions["e-primary"]]
        with pytest.raises(dep.HTTPException) as exc:
            dep._enforce_standing_work_fence(self._Req("POST", f"/api/agents/{A}/schedules"),
                                             self._key("e-primary"))
        assert exc.value.detail == dep.STANDING_WORK_REFUSED


# ---- AC5: the owner's switch is a guardrail beside `visitor_shell` ---------------------

class TestGuardrailSetting:
    def test_the_switch_is_accepted_as_a_boolean(self):
        from routers.agent_config import _validate_guardrails_payload
        assert _validate_guardrails_payload({"primary_reach": False})["primary_reach"] is False
        assert _validate_guardrails_payload({"primary_reach": True})["primary_reach"] is True

    @pytest.mark.parametrize("value", ["false", 0, None, [], {}])
    def test_anything_else_is_a_named_400(self, value):
        from fastapi import HTTPException
        from routers.agent_config import _validate_guardrails_payload
        with pytest.raises(HTTPException) as exc:
            _validate_guardrails_payload({"primary_reach": value})
        assert exc.value.status_code == 400 and "primary_reach" in exc.value.detail

    def test_flipping_it_does_not_recreate_the_container(self, monkeypatch):
        """Read live on every turn, like `visitor_shell`: a running container
        whose env predates the key still matches."""
        from services.agent_service import helpers
        container = SimpleNamespace(attrs={"Config": {"Env": ["AGENT_GUARDRAILS="]}})
        monkeypatch.setattr(helpers.db, "get_guardrails_config",
                            lambda agent: {"primary_reach": False, "visitor_shell": False})
        assert helpers.check_guardrails_env_matches(container, A) is True
