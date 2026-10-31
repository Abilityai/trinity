"""An agent's ask may address a canon role by id — `to: role:<id>`
(abilityai/trinity-enterprise#1007, item 1).

Until now `ask_operator.to` took the four kinds only (`primary | approver |
viewer | operator`); a canon role id (`role:<id>`, the form the skill gate
already stores as an approver since ent#848) was refused `invalid_to`. A
reconciler that works through asks had no way to put a decision in front of
"whoever is seated in Head of sales on this agent".

The sink now routes a role id through `role_addressing.resolve_role_id_for_ask`:
the raising agent's OWN roster answers through the assignments provider, each
seated person is checked for ent#881 reachability (`can_open_agent`, the rule
that moved out of the gate), and every failure arm is closed and NAMED —
`role_unavailable` (no provider, or the provider failed), `role_unassigned`
(nobody seated), `role_cannot_open_agent` (nobody seated can open the agent).
The refusal names the role's label, never a person. The four kinds, replay,
`already_pending` and the gate's own `_can_open` patch surface are unchanged.

Harness: the unit island's per-process SQLite and the real sink (the ent#816
world). Agent names and emails are unique per test. The roster predicate and
the assignments provider are stubbed; the sink's side effects are recorded.
"""
from __future__ import annotations

import json
import os
import sys
import uuid

import pytest

pytest.importorskip("sqlalchemy")

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

pytestmark = pytest.mark.unit

# Bound at collection time — see test_ent747_748_ask_discuss_dismiss.py.
import services.ask_service  # noqa: E402,F401
import services.operator_queue_service  # noqa: E402,F401
import services.operator_resume_service  # noqa: E402,F401
import services.role_addressing  # noqa: E402,F401
import services.skill_gate_service  # noqa: E402,F401
import client_portal.asks.service  # noqa: E402,F401
import client_portal.db  # noqa: E402,F401

ROLE = "head-of-sales"
LABEL = "Head of sales"


def _uniq(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}@example.com"


@pytest.fixture
def real_db():
    from database import db as real
    return real


@pytest.fixture
def agent():
    return f"agent-1007-{uuid.uuid4().hex[:6]}"


@pytest.fixture
def people():
    return {"alice": _uniq("alice"), "bob": _uniq("bob"), "carol": _uniq("carol")}


@pytest.fixture(autouse=True)
def roster(monkeypatch):
    """Who can open which agent, as the Workspace roster answers it. Nobody
    here has a platform account (every email is fresh), so `can_open_agent`
    takes the roster door. `reach[email] = False` puts a person outside the
    agent; every call is recorded so a test can see WHOSE roster was read."""
    import client_portal.service as portal_service

    state = {"reach": {}, "calls": []}

    def agent_on_roster(agent_name, email, include_owned=True):
        state["calls"].append((agent_name, email))
        return state["reach"].get(email, True)

    monkeypatch.setattr(portal_service, "agent_on_roster", agent_on_roster)
    return state


@pytest.fixture(autouse=True)
def sink(monkeypatch, real_db):
    """The real sink over the real SQLite; its side effects recorded."""
    from services import ask_service as svc
    from services import assignment_provider
    from services import operator_queue_service as oqs
    from services import operator_resume_service as ors
    from services.rate_limiter import RateLimitResult

    class _Audit:
        async def log(self, **kw):
            return "evt"

    class _WS:
        async def broadcast(self, message):
            json.loads(message)

    monkeypatch.setattr(ors, "spawn_on_loop", lambda factory: None)
    monkeypatch.setattr(ors, "spawn_ending_dispatch", lambda rows, **kw: None)
    monkeypatch.setattr(ors, "spawn_resume_dispatch", lambda item, **kw: None)
    monkeypatch.setattr(svc, "_opted_in", lambda agent_name: True)
    monkeypatch.setattr(svc, "_observers", [])
    monkeypatch.setattr(svc, "platform_audit_service", _Audit())
    monkeypatch.setattr(svc, "_websocket_manager", _WS())
    monkeypatch.setattr(svc, "_owner_email", lambda agent: None)
    monkeypatch.setattr(oqs, "_workspace_attachment",
                        lambda agent, email, **_: (f"thread-{email}", False))
    monkeypatch.setattr(oqs.rate_limiter, "check",
                        lambda *a, **k: RateLimitResult(True, 10, 0, 60))
    assignment_provider.clear_provider()
    yield
    assignment_provider.clear_provider()


class _Provider:
    """A fake assignments provider: `roles` maps a canon role id to the emails
    seated in it (`None` = the provider has no answer for it); `kinds` the same
    for the four kinds. Every role-id question is recorded with the agent it
    was asked about."""

    def __init__(self, roles=None, kinds=None, raise_for=None):
        self.roles = roles or {}
        self.kinds = kinds or {}
        self.raise_for = raise_for
        self.asked = []

    def assignment_for(self, agent_name, triggered_by):
        return None

    def people_for(self, agent_name, role):
        value = self.kinds.get(role)
        return None if value is None else {"emails": value}

    def people_for_role_id(self, agent_name, role_id):
        self.asked.append((agent_name, role_id))
        if self.raise_for == role_id:
            raise RuntimeError("provider down")
        value = self.roles.get(role_id)
        return None if value is None else {"emails": value}


def _provide(**kw):
    from services import assignment_provider
    provider = _Provider(**kw)
    assignment_provider.register_provider(provider)
    return provider


def _raise(agent, request_id=None, *, to=f"role:{ROLE}", kind="approval", **over):
    from services import ask_service
    rid = request_id or f"r-{uuid.uuid4().hex[:10]}"
    body = {"request_id": rid, "type": kind, "title": "Pay invoice",
            "question": "Release the payment?", "options": ["approve", "reject"],
            "to": to}
    if kind == "approval":
        body["proposal"] = {"pay": 500, "to": f"vendor-{rid}"}
    body.update(over)
    return ask_service.raise_ask(agent, body, raised_by="agent", channel="mcp")


def _refused(agent, **kw):
    from services import ask_service
    with pytest.raises(ask_service.AskRejected) as exc:
        _raise(agent, **kw)
    return exc.value


def _row(real_db, agent):
    rows = real_db.list_operator_queue_items(agent_name=agent, limit=50)
    assert len(rows) == 1, rows
    row = rows[0]
    resolved = row.get("resolved_to")
    if isinstance(resolved, str):
        resolved = json.loads(resolved)
    row["resolved_to"] = resolved
    return row


def _rows(real_db, agent):
    return real_db.list_operator_queue_items(agent_name=agent, limit=50)


def _inbox(email):
    from client_portal.asks import service
    return [a.id for a in service.list_asks_page(email, is_platform=False).items]


# ===========================================================================
# The happy path — a role id reaches the people seated in it, and only them
# ===========================================================================

class TestAnAskToACanonRole:
    def test_is_recorded_to_the_role_and_reaches_the_one_seated_person(self, real_db, agent, people):
        _provide(roles={ROLE: [people["alice"]]})
        receipt = _raise(agent)

        assert receipt["status"] == "created"
        assert receipt["to_role"] == f"role:{ROLE}"
        assert receipt["resolved"] is True
        assert people["alice"] not in json.dumps(receipt)   # the role, never a person
        row = _row(real_db, agent)
        assert row["to_role"] == f"role:{ROLE}"
        assert row["resolved_to"] == [people["alice"]]
        assert row["addressed_to_email"] == people["alice"]
        assert _inbox(people["alice"]) == [receipt["id"]]

    def test_several_seated_people_are_all_recorded_and_none_is_the_single_addressee(self, real_db, agent, people):
        _provide(roles={ROLE: [people["alice"], people["bob"]]})
        receipt = _raise(agent)

        row = _row(real_db, agent)
        assert sorted(row["resolved_to"]) == sorted([people["alice"], people["bob"]])
        assert row["addressed_to_email"] is None
        assert receipt["resolved"] is True
        assert _inbox(people["alice"]) == [receipt["id"]]
        assert _inbox(people["bob"]) == [receipt["id"]]
        assert _inbox(people["carol"]) == []

    def test_a_seated_person_who_cannot_open_the_agent_is_left_out(self, real_db, agent, people, roster):
        """ent#881 applied to an ask: the role names alice and bob, bob is
        outside the agent — the ask reaches alice only, and never bob."""
        roster["reach"][people["bob"]] = False
        _provide(roles={ROLE: [people["alice"], people["bob"]]})
        _raise(agent)

        row = _row(real_db, agent)
        assert row["resolved_to"] == [people["alice"]]
        assert row["addressed_to_email"] == people["alice"]

    def test_the_roster_consulted_is_the_raising_agents_own(self, agent, people, roster):
        """Self-acting: the provider and the reachability check are both asked
        about the agent that raised the ask, never another one."""
        provider = _provide(roles={ROLE: [people["alice"]]})
        _raise(agent)

        assert provider.asked == [(agent, ROLE)]
        assert roster["calls"] == [(agent, people["alice"])]

    def test_the_id_is_stored_lower_cased_and_a_mixed_case_retry_replays_without_differs(self, real_db, agent, people):
        provider = _provide(roles={ROLE: [people["alice"]]})
        first = _raise(agent, "deploy-1", to="role:Head-Of-Sales")
        again = _raise(agent, "deploy-1", to="role:HEAD-of-sales")

        assert _row(real_db, agent)["to_role"] == f"role:{ROLE}"
        assert provider.asked == [(agent, ROLE)]          # the provider sees the canonical id
        assert again["status"] == "replayed"
        assert again["id"] == first["id"]
        assert again["differs"] == []

    def test_a_question_and_an_alert_may_address_a_role_too(self, real_db, agent, people):
        _provide(roles={ROLE: [people["alice"]]})
        for kind in ("question", "alert"):
            _raise(agent, kind=kind)
        assert [r["to_role"] for r in _rows(real_db, agent)] == [f"role:{ROLE}"] * 2


# ===========================================================================
# Every failure arm is closed and named — the role's label, never a person
# ===========================================================================

class TestEveryFailureArmIsNamed:
    def test_no_assignments_provider_is_role_unavailable(self, real_db, agent):
        e = _refused(agent)
        assert (e.status_code, e.code) == (422, "role_unavailable")
        assert e.extra.get("role") == f"role:{ROLE}"
        assert "assignments" in e.message
        assert "primary" in e.message and "operator" in e.message
        assert _rows(real_db, agent) == []

    def test_a_provider_with_no_answer_for_the_role_is_role_unavailable(self, real_db, agent, people):
        _provide(roles={"other-role": [people["alice"]]})
        e = _refused(agent)
        assert (e.status_code, e.code) == (422, "role_unavailable")
        assert _rows(real_db, agent) == []

    def test_a_provider_that_raises_is_role_unavailable_never_a_crash(self, real_db, agent, people):
        _provide(roles={ROLE: [people["alice"]]}, raise_for=ROLE)
        e = _refused(agent)
        assert (e.status_code, e.code) == (422, "role_unavailable")
        assert _rows(real_db, agent) == []

    def test_nobody_seated_is_role_unassigned_and_names_the_label(self, real_db, agent):
        _provide(roles={ROLE: []})
        e = _refused(agent)
        assert (e.status_code, e.code) == (422, "role_unassigned")
        assert LABEL in e.message
        assert e.extra.get("role") == f"role:{ROLE}"
        assert _rows(real_db, agent) == []

    def test_nobody_seated_can_open_the_agent_is_role_cannot_open_agent(self, real_db, agent, people, roster):
        roster["reach"][people["alice"]] = False
        roster["reach"][people["bob"]] = False
        _provide(roles={ROLE: [people["alice"], people["bob"]]})
        e = _refused(agent)
        assert (e.status_code, e.code) == (422, "role_cannot_open_agent")
        assert LABEL in e.message and agent in e.message
        assert "share" in e.message.lower()
        assert people["alice"] not in e.message and people["bob"] not in e.message
        assert e.extra.get("role") == f"role:{ROLE}"
        assert _rows(real_db, agent) == []

    def test_a_reachability_lookup_that_fails_counts_as_cannot_open(self, real_db, agent, people, monkeypatch):
        """Fail closed: when the platform cannot tell whether a seated person
        can open the agent, the ask is refused by name — never raised to
        someone who may never see it, never a silent wait."""
        from services import role_addressing

        def boom(email):
            raise RuntimeError("db down")

        monkeypatch.setattr(role_addressing.db, "get_user_by_email", boom)
        _provide(roles={ROLE: [people["alice"]]})
        e = _refused(agent)
        assert (e.status_code, e.code) == (422, "role_cannot_open_agent")
        assert _rows(real_db, agent) == []


# ===========================================================================
# The `to` vocabulary — the four kinds, a well-formed role id, nothing else
# ===========================================================================

class TestTheToVocabulary:
    @pytest.mark.parametrize("to", [
        "role:", "role:-x", "role:" + "x" * 65, "role:x\n", "Role:x", "role:x y", "role:x/y",
    ])
    def test_a_malformed_role_id_is_invalid_to_and_the_refusal_teaches_the_form(self, real_db, agent, to):
        _provide(roles={"x": ["someone@example.com"]})
        e = _refused(agent, to=to)
        assert (e.status_code, e.code) == (422, "invalid_to")
        assert "role:<id>" in e.extra["allowed"]
        assert set(e.extra["allowed"]) >= {"primary", "approver", "viewer", "operator"}
        assert _rows(real_db, agent) == []

    def test_the_four_kinds_still_resolve_as_before(self, real_db, agent, people):
        _provide(kinds={"approver": [people["alice"]]})
        approver = _raise(agent, to="approver")
        alert = _raise(agent, kind="alert", to="operator")
        assert approver["to_role"] == "approver" and approver["resolved"] is True
        assert alert["to_role"] == "operator"
        assert _rows(real_db, agent)[0]["to_role"] in ("approver", "operator")

    def test_an_unfilled_kind_keeps_its_refusal(self, agent):
        e = _refused(agent, to="viewer")
        assert (e.status_code, e.code) == (422, "role_unassigned")

    def test_a_report_or_a_message_still_cannot_address_a_role_id(self, agent):
        """Scope pin (ruling T4): only the ask sink takes a role id. The one
        rule reports and messages resolve through (`role_addressing.resolve`)
        still refuses it `invalid_to`."""
        from services import role_addressing
        _provide(roles={ROLE: ["someone@example.com"]})
        with pytest.raises(role_addressing.RoleRefused) as info:
            role_addressing.resolve(agent, f"role:{ROLE}")
        assert info.value.code == "invalid_to"


# ===========================================================================
# The ask contract holds on the role-id path
# ===========================================================================

class TestTheAskContractHolds:
    def test_repeating_a_pending_proposal_to_a_role_is_already_pending(self, agent, people):
        from services import ask_service
        _provide(roles={ROLE: [people["alice"]]})
        _raise(agent, "deploy-1", proposal={"pay": 500})
        with pytest.raises(ask_service.AskRejected) as exc:
            _raise(agent, "deploy-2", proposal={"pay": 500})
        assert (exc.value.status_code, exc.value.code) == (409, "already_pending")

    def test_replacing_a_pending_role_ask_works(self, real_db, agent, people):
        _provide(roles={ROLE: [people["alice"]]})
        _raise(agent, "deploy-1", proposal={"pay": 500})
        receipt = _raise(agent, "deploy-2", proposal={"pay": 600}, replaces="deploy-1")
        assert receipt["status"] == "created"
        assert receipt["replaces"] == "deploy-1"


# ===========================================================================
# The ent#881 rule moved: `role_addressing.can_open_agent`, the gate delegates
# ===========================================================================

class TestTheReachabilityRuleMoved:
    def test_the_gates_can_open_delegates_to_role_addressing(self, monkeypatch):
        from services import role_addressing
        from services import skill_gate_service

        calls = []
        monkeypatch.setattr(role_addressing, "can_open_agent",
                            lambda agent_name, email: calls.append((agent_name, email)) or False)
        assert skill_gate_service._can_open("fin", "x@example.com") is False
        assert calls == [("fin", "x@example.com")]

    def test_a_patch_on_the_gates_can_open_still_governs_its_approvers(self, agent, people, monkeypatch):
        """The existing tests patch `skill_gate_service._can_open`; the
        delegate keeps that surface live."""
        from services import skill_gate_service

        _provide(kinds={"approver": [people["alice"]]})
        monkeypatch.setattr(skill_gate_service, "_can_open", lambda a, e: False)
        with pytest.raises(skill_gate_service.SkillGateRefused) as exc:
            skill_gate_service._approvers(agent, "approver")
        assert exc.value.code == "approver_cannot_open_agent"
        monkeypatch.setattr(skill_gate_service, "_can_open", lambda a, e: True)
        assert skill_gate_service._approvers(agent, "approver") == [people["alice"]]

    @pytest.mark.parametrize("account,on_roster,expected", [
        (None, True, True), (None, False, False),
    ])
    def test_can_open_agent_takes_the_roster_door_for_an_email_without_an_account(
            self, agent, people, roster, account, on_roster, expected):
        from services import role_addressing
        roster["reach"][people["alice"]] = on_roster
        assert role_addressing.can_open_agent(agent, people["alice"]) is expected
        assert roster["calls"] == [(agent, people["alice"])]

    def test_can_open_agent_asks_access_for_a_platform_account(self, agent, people, roster, monkeypatch):
        from services import role_addressing
        asked = []
        monkeypatch.setattr(role_addressing.db, "get_user_by_email",
                            lambda email: {"username": "alice-u", "email": email})
        monkeypatch.setattr(role_addressing.db, "can_user_access_agent",
                            lambda username, agent_name: asked.append((username, agent_name)) or True)
        assert role_addressing.can_open_agent(agent, people["alice"]) is True
        assert asked == [("alice-u", agent)]
        assert roster["calls"] == []

    def test_can_open_agent_fails_closed(self, agent, people, monkeypatch):
        from services import role_addressing

        def boom(email):
            raise RuntimeError("db down")

        monkeypatch.setattr(role_addressing.db, "get_user_by_email", boom)
        assert role_addressing.can_open_agent(agent, people["alice"]) is False
