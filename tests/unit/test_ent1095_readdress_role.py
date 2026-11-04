"""A role-addressed pending ask follows the role when the assignment changes
(abilityai/trinity-enterprise#1095).

An ask's audience is resolved ONCE at raise (`ask_service._address`, or the
gate's pre-resolved `addressees`) and stored on the row (`resolved_to`,
`addressed_to_email`). Every ending door reads the stored list, so a primary
replaced AFTER the raise left the pending ask answerable only by the person
who no longer fills the role — and, when nobody did at raise time, by nobody
at all even once someone was seated.

`ask_service.readdress_role(agent, role, reason=...)` re-resolves the role and
rewrites the audience of this agent's pending rows that were addressed BY
ROLE — an allowlist: the agent's own raises, and a gated-skill approval
(`gate-<40 hex>`). A row the platform addressed to a NAMED person (a gate
note, a project consent card, a reach row) is never moved: the role was never
its fence. `ask_service.readdress_item` is the one-row primitive beneath it.

Pinned here:

* a replaced primary: the agent's ask and the gate approval move, the new
  primary passes every ending door, the old one fails them;
* a first primary on an unaddressed row addresses it; a primary removed with
  no replacement falls to the operators (NULL audience, admin cancel only);
* the real replace sequence (DELETE, then POST — the partial UNIQUE index) ends
  on the new primary with one audit row per change and one trigger per call;
* named-addressee rows, other roles' rows, decided rows, expired-but-pending
  rows and an unchanged audience are untouched — no write, no audit, no
  trigger;
* an unfilled `approver` leaves a seat-fenced row as it was (ent#1053: an
  empty snapshot would un-fence it); a credential row keeps exactly one
  reader (ent#1066);
* the Workspace chat keys follow the single addressee: dropped on a change,
  the new person's Main re-attached when there is one;
* a row answered between the read and the write is not overwritten (CAS);
* the `/ws` trigger is a literal the ent#467 guard discovers.

Harness: the unit island's per-process SQLite (the ent#981 shape); the
assignments provider is a controllable stub; the audit and `/ws` sinks are
recorders run synchronously.
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from types import SimpleNamespace

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
import services.skill_gate_service  # noqa: E402,F401
import services.role_addressing  # noqa: E402,F401

THREAD = "workspace_session_id"
TURN = "workspace_raised_in_turn"
DISCUSSION = "workspace_discussion_id"
READDRESSED = "platform_readdressed"


def _uniq(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}@example.com"


def _gate_id():
    """The id shape `skill_gate_service._request_id` mints: `gate-` + 40 hex."""
    return "gate-" + (uuid.uuid4().hex + uuid.uuid4().hex)[:40]


@pytest.fixture
def real_db():
    from database import db as real
    return real


@pytest.fixture
def agent():
    return f"agent-1095-{uuid.uuid4().hex[:6]}"


@pytest.fixture
def people():
    return {"alice": _uniq("alice"), "bob": _uniq("bob"), "carol": _uniq("carol"),
            "dave": _uniq("dave"), "admin": _uniq("admin")}


class _Provider:
    """The assignments provider as the enterprise module answers it: a list
    per role, `[]` being a real "nobody fills it"."""

    def __init__(self):
        self.roles = {}

    def assignment_for(self, *a, **k):
        return None

    def people_for(self, agent_name, role):
        return {"emails": list(self.roles.get(role, []))}


@pytest.fixture
def provider():
    from services import assignment_provider
    p = _Provider()
    assignment_provider.register_provider(p)
    yield p
    assignment_provider.clear_provider()


class _Audit:
    def __init__(self):
        self.rows = []

    async def log(self, **kw):
        self.rows.append(kw)
        return "evt"

    def actions(self, action):
        return [r for r in self.rows if r.get("event_action") == action]


class _WS:
    def __init__(self):
        self.messages = []

    async def broadcast(self, message):
        import json
        self.messages.append(json.loads(message))

    def events(self, kind):
        return [m for m in self.messages if m.get("type") == kind]


@pytest.fixture
def audit():
    return _Audit()


@pytest.fixture
def ws():
    return _WS()


@pytest.fixture(autouse=True)
def sink(monkeypatch, real_db, provider, audit, ws):
    """The real sink over the real SQLite; its side effects recorded, and
    every announcement run synchronously so a test counts what landed."""
    from services import ask_service as svc
    from services import operator_queue_service as oqs
    from services import operator_resume_service as ors
    from services import skill_gate_service as sgs
    from services.rate_limiter import RateLimitResult

    monkeypatch.setattr(ors, "spawn_on_loop", lambda factory: asyncio.run(factory()))
    monkeypatch.setattr(ors, "spawn_ending_dispatch", lambda rows, **kw: None)
    monkeypatch.setattr(ors, "spawn_resume_dispatch", lambda item, **kw: None)
    monkeypatch.setattr(svc, "_opted_in", lambda agent_name: True)
    monkeypatch.setattr(svc, "_observers", [])
    monkeypatch.setattr(svc, "platform_audit_service", audit)
    monkeypatch.setattr(svc, "_websocket_manager", ws)
    monkeypatch.setattr(svc, "_owner_email", lambda agent: None)
    monkeypatch.setattr(oqs, "_workspace_attachment",
                        lambda agent, email, **_: (f"thread-{email}", False))
    monkeypatch.setattr(oqs.rate_limiter, "check",
                        lambda *a, **k: RateLimitResult(True, 10, 0, 60))
    # ent#881 reachability: everyone the role names can open the agent unless
    # a test says otherwise.
    monkeypatch.setattr(sgs, "_can_open", lambda agent_name, email: True)
    yield


def _svc():
    from services import ask_service
    return ask_service


def _agent_ask(agent, *, to="primary", kind="question", request_id=None):
    body = {"request_id": request_id or f"ask-{uuid.uuid4().hex[:10]}", "type": kind,
            "title": "Need a decision", "question": "Which one?", "to": to}
    if kind == "credential":
        body["context"] = {"credential_name": "API_KEY"}
    return _svc().raise_ask(agent, body, raised_by="agent", channel="mcp")["id"]


def _gate_approval(agent, approvers, *, to_role="primary"):
    body = {"request_id": _gate_id(), "type": "approval", "title": "Run the gated skill?",
            "question": "Approve the run?", "options": ["Approve", "Decline"]}
    return _svc().raise_ask(agent, body, raised_by="gate", channel="gate",
                            addressees=list(approvers), to_role_override=to_role)["id"]


def _named_row(agent, prefix, email, *, kind="question"):
    body = {"request_id": prefix + uuid.uuid4().hex[:8], "type": kind, "title": "For you",
            "question": "A note.", "to": "primary"}
    if kind == "approval":
        body["options"] = ["Approve", "Decline"]
    return _svc().raise_ask(agent, body, raised_by="gate", channel="gate", addressee=email)["id"]


def _row(real_db, ask_id):
    return real_db.get_operator_queue_item(ask_id)


def _actor(email, role=None):
    user = SimpleNamespace(role=role) if role else None
    return _svc().Actor(email=email, user=user)


def _readdress(agent, role="primary"):
    svc = _svc()
    return svc.readdress_role(agent, role, reason=svc.ASSIGNMENT_CHANGED)


# ===========================================================================
# A primary replaced
# ===========================================================================

class TestPrimaryReplaced:
    def test_the_agents_ask_and_the_gate_approval_follow_the_new_primary(
            self, real_db, agent, people, provider):
        from services import skill_gate_service as sgs
        provider.roles["primary"] = [people["alice"]]
        ask = _agent_ask(agent)
        approval = _gate_approval(agent, [people["alice"]])
        assert _row(real_db, approval)["resolved_to"] == [people["alice"]]

        provider.roles["primary"] = [people["bob"]]
        out = _readdress(agent)

        assert out == {"readdressed": 2, "unchanged": 0}
        for ask_id in (ask, approval):
            row = _row(real_db, ask_id)
            assert row["status"] == "pending"
            assert row["resolved_to"] == [people["bob"]]
            assert row["addressed_to_email"] == people["bob"]
        gate_row = _row(real_db, approval)
        assert _svc().may_end(gate_row, _actor(people["bob"])) is True
        assert _svc().may_end(gate_row, _actor(people["alice"])) is False
        assert sgs._approved_by_addressee(gate_row, people["bob"]) is True
        assert sgs._approved_by_addressee(gate_row, people["alice"]) is False

    def test_the_receipt_reads_resolved_after_the_move(self, real_db, agent, people, provider):
        provider.roles["primary"] = []
        ask = _agent_ask(agent)
        assert _row(real_db, ask)["resolved_to"] is None
        assert _svc()._receipt(_row(real_db, ask), status="pending")["resolved"] is False

        provider.roles["primary"] = [people["alice"]]
        _readdress(agent)

        row = _row(real_db, ask)
        assert row["resolved_to"] == [people["alice"]]
        assert row["addressed_to_email"] == people["alice"]
        assert _svc()._receipt(row, status="pending")["resolved"] is True

    def test_two_primaries_record_both_and_no_single(self, real_db, agent, people, provider):
        provider.roles["primary"] = [people["alice"]]
        ask = _agent_ask(agent)
        provider.roles["primary"] = [people["bob"], people["carol"]]

        assert _readdress(agent)["readdressed"] == 1
        row = _row(real_db, ask)
        assert row["resolved_to"] == [people["bob"], people["carol"]]
        assert row["addressed_to_email"] is None

    def test_a_primary_removed_with_no_replacement_falls_to_the_operators(
            self, real_db, agent, people, provider):
        provider.roles["primary"] = [people["alice"]]
        ask = _agent_ask(agent)
        approval = _gate_approval(agent, [people["alice"]])

        provider.roles["primary"] = []
        assert _readdress(agent)["readdressed"] == 2

        for ask_id in (ask, approval):
            row = _row(real_db, ask_id)
            assert row["status"] == "pending"
            assert row["resolved_to"] is None
            assert row["addressed_to_email"] is None
        gate_row = _row(real_db, approval)
        admin = _actor(people["admin"], role="admin")
        assert _svc().may_end(gate_row, admin, cancelling=True) is True
        assert _svc().may_end(gate_row, admin) is False
        assert _svc().may_end(gate_row, _actor(people["alice"])) is False

    def test_the_real_replace_is_delete_then_post(self, real_db, agent, people, provider, audit, ws):
        """S1: the partial UNIQUE index makes a replace two writes. The end
        state is the new primary, each change has its audit row, and each
        call sends ONE agent-keyed trigger — never one per row."""
        provider.roles["primary"] = [people["alice"]]
        ask = _agent_ask(agent)
        approval = _gate_approval(agent, [people["alice"]])
        audit.rows.clear()
        ws.messages.clear()

        provider.roles["primary"] = []
        assert _readdress(agent) == {"readdressed": 2, "unchanged": 0}
        provider.roles["primary"] = [people["bob"]]
        assert _readdress(agent) == {"readdressed": 2, "unchanged": 0}

        for ask_id in (ask, approval):
            row = _row(real_db, ask_id)
            assert row["resolved_to"] == [people["bob"]]
            assert row["addressed_to_email"] == people["bob"]
            assert row["context"][THREAD] == f"thread-{people['bob']}"
        rows = audit.actions(READDRESSED)
        assert len(rows) == 4
        assert {r["target_id"] for r in rows} == {ask, approval}
        for r in rows:
            assert r["source"] == "system"
            assert r["details"] == {"reason": "assignment_changed", "to_role": "primary",
                                    "agent_name": agent}
        triggers = ws.events("operator_queue_new")
        assert len(triggers) == 2
        for t in triggers:
            assert set(t["data"]) == {"id", "agent_name"}
            assert t["data"]["agent_name"] == agent
            assert t["data"]["id"] in (ask, approval)


# ===========================================================================
# What stays as it was
# ===========================================================================

class TestUntouched:
    def test_named_addressee_rows_never_move(self, real_db, agent, people, provider, audit):
        """A gate note and a project consent card default `to: primary` but
        the platform CHOSE the person (ent#661) — the role was never their
        fence."""
        provider.roles["primary"] = [people["alice"]]
        note = _named_row(agent, "gate-note-", people["carol"])
        consent = _named_row(agent, "gate-project-agent-", people["carol"], kind="approval")
        audit.rows.clear()

        provider.roles["primary"] = [people["bob"]]
        assert _readdress(agent) == {"readdressed": 0, "unchanged": 0}

        for ask_id in (note, consent):
            row = _row(real_db, ask_id)
            assert row["resolved_to"] == [people["carol"]]
            assert row["addressed_to_email"] == people["carol"]
        assert audit.actions(READDRESSED) == []

    def test_other_roles_rows_are_not_touched_by_a_primary_change(
            self, real_db, agent, people, provider):
        provider.roles["primary"] = [people["alice"]]
        provider.roles["approver"] = [people["carol"]]
        to_approver = _agent_ask(agent, to="approver")
        to_operator = _agent_ask(agent, to="operator")

        provider.roles["primary"] = [people["bob"]]
        assert _readdress(agent) == {"readdressed": 0, "unchanged": 0}

        assert _row(real_db, to_approver)["resolved_to"] == [people["carol"]]
        assert _row(real_db, to_operator)["resolved_to"] is None

    def test_a_decided_row_keeps_the_audience_that_decided_it(self, real_db, agent, people, provider):
        provider.roles["primary"] = [people["alice"]]
        ask = _agent_ask(agent)
        _svc().answer(_row(real_db, ask), response="Yes", response_text=None,
                      actor=_actor(people["alice"]))

        provider.roles["primary"] = [people["bob"]]
        assert _readdress(agent) == {"readdressed": 0, "unchanged": 0}

        row = _row(real_db, ask)
        assert row["status"] != "pending"
        assert row["resolved_to"] == [people["alice"]]

    def test_an_unchanged_audience_writes_nothing(self, real_db, agent, people, provider, audit, ws):
        provider.roles["primary"] = [people["alice"]]
        ask = _agent_ask(agent)
        before = _row(real_db, ask)
        audit.rows.clear()
        ws.messages.clear()

        assert _readdress(agent) == {"readdressed": 0, "unchanged": 1}

        assert _row(real_db, ask) == before
        assert audit.actions(READDRESSED) == []
        assert ws.messages == []

    def test_a_case_variant_of_the_same_audience_is_unchanged(self, real_db, agent, people, provider):
        provider.roles["primary"] = [people["alice"]]
        ask = _agent_ask(agent)
        provider.roles["primary"] = [people["alice"].upper()]

        assert _readdress(agent) == {"readdressed": 0, "unchanged": 1}
        assert _row(real_db, ask)["resolved_to"] == [people["alice"]]

    def test_an_expired_but_unswept_row_is_left_for_the_sweep(self, real_db, agent, people, provider):
        """S4: a new primary is never handed an ask the sweep ends a minute
        later."""
        from utils.helpers import utc_now_iso
        provider.roles["primary"] = [people["alice"]]
        item = {"id": f"ask-{uuid.uuid4().hex[:8]}", "type": "question", "priority": "medium",
                "title": "Old", "question": "Still?", "options": None, "context": {},
                "created_at": utc_now_iso(), "expires_at": "2020-01-01T00:00:00Z",
                "addressed_to_email": people["alice"]}
        out = real_db.create_native_operator_queue_item(
            agent, item, max_pending=None, channel="mcp", raised_by="agent",
            to_role="primary", resolved_to=[people["alice"]], proposal=None,
            supersedes_expired=None)
        assert out["outcome"] == "created", out

        provider.roles["primary"] = [people["bob"]]
        assert _readdress(agent) == {"readdressed": 0, "unchanged": 0}
        assert _row(real_db, out["row"]["id"])["resolved_to"] == [people["alice"]]

    def test_another_agents_rows_are_not_touched(self, real_db, agent, people, provider):
        other = f"agent-1095-{uuid.uuid4().hex[:6]}"
        provider.roles["primary"] = [people["alice"]]
        theirs = _agent_ask(other)
        provider.roles["primary"] = [people["bob"]]

        assert _readdress(agent) == {"readdressed": 0, "unchanged": 0}
        assert _row(real_db, theirs)["resolved_to"] == [people["alice"]]

    def test_an_unknown_role_or_reason_is_a_programming_error(self, agent):
        svc = _svc()
        with pytest.raises(ValueError):
            svc.readdress_role(agent, "owner", reason=svc.ASSIGNMENT_CHANGED)
        with pytest.raises(ValueError):
            svc.readdress_role(agent, "primary", reason="because")
        with pytest.raises(ValueError):
            svc.readdress_item("x", [], None, reason="because")
        assert svc.ASSIGNMENT_CHANGED in svc.PLATFORM_READDRESS_REASONS


# ===========================================================================
# The fences the move must respect
# ===========================================================================

class TestFences:
    def test_an_unfilled_approver_leaves_the_seat_fenced_row_as_it_was(
            self, real_db, agent, people, provider):
        """R1 / ent#1053: an empty `resolved_to` on an approver row reads as
        a legacy row and un-fences it — so nobody filling the role leaves the
        row with the people it had, never with nobody."""
        provider.roles["approver"] = [people["carol"]]
        ask = _agent_ask(agent, to="approver")
        approval = _gate_approval(agent, [people["carol"]], to_role="approver")

        provider.roles["approver"] = []
        assert _readdress(agent, "approver") == {"readdressed": 0, "unchanged": 2}

        for ask_id in (ask, approval):
            row = _row(real_db, ask_id)
            assert row["resolved_to"] == [people["carol"]]
            assert _svc().may_end(row, _actor(people["bob"])) is False
            assert _svc().may_end(row, _actor(people["carol"])) is True

    def test_the_primitive_is_generic_over_the_role(self, real_db, agent, people, provider):
        provider.roles["approver"] = [people["carol"]]
        ask = _agent_ask(agent, to="approver")
        approval = _gate_approval(agent, [people["carol"]], to_role="approver")

        provider.roles["approver"] = [people["dave"]]
        assert _readdress(agent, "approver") == {"readdressed": 2, "unchanged": 0}

        for ask_id in (ask, approval):
            row = _row(real_db, ask_id)
            assert row["resolved_to"] == [people["dave"]]
            assert _svc().may_end(row, _actor(people["dave"])) is True
            assert _svc().may_end(row, _actor(people["carol"])) is False

    def test_a_gate_approval_applies_the_gates_reachability_rule(
            self, real_db, agent, people, provider, monkeypatch):
        """ent#881: the moved approval reaches exactly who a fresh raise would
        — the seated people who can OPEN the agent. An agent's own ask keeps
        the plain resolution."""
        from services import skill_gate_service as sgs
        provider.roles["primary"] = [people["alice"]]
        ask = _agent_ask(agent)
        approval = _gate_approval(agent, [people["alice"]])

        provider.roles["primary"] = [people["bob"], people["carol"]]
        monkeypatch.setattr(sgs, "_can_open", lambda a, e: e == people["bob"])
        assert _readdress(agent)["readdressed"] == 2

        assert _row(real_db, approval)["resolved_to"] == [people["bob"]]
        assert _row(real_db, approval)["addressed_to_email"] == people["bob"]
        assert _row(real_db, ask)["resolved_to"] == [people["bob"], people["carol"]]

    def test_a_gate_approval_nobody_seated_can_open_is_left_as_it_was(
            self, real_db, agent, people, provider, monkeypatch):
        from services import skill_gate_service as sgs
        provider.roles["primary"] = [people["alice"]]
        approval = _gate_approval(agent, [people["alice"]])

        provider.roles["primary"] = [people["bob"]]
        monkeypatch.setattr(sgs, "_can_open", lambda a, e: False)
        assert _readdress(agent) == {"readdressed": 0, "unchanged": 1}
        assert _row(real_db, approval)["resolved_to"] == [people["alice"]]

    def test_a_credential_row_keeps_exactly_one_reader(self, real_db, agent, people, provider):
        """R2 / ent#1066: a secret field has one reader. Two primaries, or
        none, leave it where it was; one moves it."""
        provider.roles["primary"] = [people["alice"]]
        ask = _agent_ask(agent, kind="credential")
        assert _row(real_db, ask)["addressed_to_email"] == people["alice"]

        provider.roles["primary"] = [people["bob"], people["carol"]]
        assert _readdress(agent) == {"readdressed": 0, "unchanged": 1}
        assert _row(real_db, ask)["resolved_to"] == [people["alice"]]
        provider.roles["primary"] = []
        assert _readdress(agent) == {"readdressed": 0, "unchanged": 1}
        assert _row(real_db, ask)["addressed_to_email"] == people["alice"]

        provider.roles["primary"] = [people["bob"]]
        assert _readdress(agent) == {"readdressed": 1, "unchanged": 0}
        row = _row(real_db, ask)
        assert row["resolved_to"] == [people["bob"]]
        assert row["addressed_to_email"] == people["bob"]


# ===========================================================================
# The Workspace chat keys follow the single addressee (R4)
# ===========================================================================

class TestChatKeys:
    def test_the_new_single_addressee_gets_their_main(self, real_db, agent, people, provider):
        provider.roles["primary"] = [people["alice"]]
        ask = _agent_ask(agent)
        approval = _gate_approval(agent, [people["alice"]])
        for ask_id in (ask, approval):
            assert _row(real_db, ask_id)["context"][THREAD] == f"thread-{people['alice']}"
        real_db.set_operator_queue_discussion_link(ask, DISCUSSION, "chat-discuss-1")
        assert _row(real_db, ask)["context"][DISCUSSION] == "chat-discuss-1"

        provider.roles["primary"] = [people["bob"]]
        _readdress(agent)

        for ask_id in (ask, approval):
            ctx = _row(real_db, ask_id)["context"]
            assert ctx[THREAD] == f"thread-{people['bob']}"
            assert DISCUSSION not in ctx
            assert TURN not in ctx

    def test_a_turn_attached_ask_is_re_homed_to_main(self, real_db, agent, people, provider, monkeypatch):
        from services import operator_queue_service as oqs
        provider.roles["primary"] = [people["alice"]]
        monkeypatch.setattr(oqs, "_workspace_attachment",
                            lambda agent, email, **_: (f"turn-{email}", True))
        ask = _agent_ask(agent)
        ctx = _row(real_db, ask)["context"]
        assert ctx[THREAD] == f"turn-{people['alice']}" and ctx[TURN] is True

        monkeypatch.setattr(oqs, "_workspace_attachment",
                            lambda agent, email, **_: (f"thread-{email}", False))
        provider.roles["primary"] = [people["bob"]]
        _readdress(agent)

        ctx = _row(real_db, ask)["context"]
        assert ctx[THREAD] == f"thread-{people['bob']}"
        assert TURN not in ctx

    def test_no_single_addressee_means_no_chat_key(self, real_db, agent, people, provider):
        provider.roles["primary"] = [people["alice"]]
        to_two = _agent_ask(agent)
        to_nobody = _agent_ask(agent)

        provider.roles["primary"] = [people["bob"], people["carol"]]
        _readdress(agent)
        assert THREAD not in (_row(real_db, to_two)["context"] or {})

        provider.roles["primary"] = []
        _readdress(agent)
        assert THREAD not in (_row(real_db, to_nobody)["context"] or {})

    def test_an_unresolved_main_leaves_no_key(self, real_db, agent, people, provider, monkeypatch):
        from services import operator_queue_service as oqs
        provider.roles["primary"] = [people["alice"]]
        ask = _agent_ask(agent)
        monkeypatch.setattr(oqs, "_workspace_attachment", lambda agent, email, **_: (None, False))

        provider.roles["primary"] = [people["bob"]]
        _readdress(agent)

        ctx = _row(real_db, ask)["context"] or {}
        assert THREAD not in ctx
        assert _row(real_db, ask)["addressed_to_email"] == people["bob"]

    def test_other_context_keys_survive_the_move(self, real_db, agent, people, provider):
        provider.roles["primary"] = [people["alice"]]
        body = {"request_id": f"ask-{uuid.uuid4().hex[:8]}", "type": "question", "title": "T",
                "question": "Q?", "to": "primary", "context": {"ticket": "T-7"}}
        ask = _svc().raise_ask(agent, body, raised_by="agent", channel="mcp")["id"]

        provider.roles["primary"] = [people["bob"]]
        _readdress(agent)

        ctx = _row(real_db, ask)["context"]
        assert ctx["ticket"] == "T-7"
        assert ctx[THREAD] == f"thread-{people['bob']}"


# ===========================================================================
# Compare-and-set
# ===========================================================================

class TestCompareAndSet:
    def test_a_row_answered_between_the_read_and_the_write_is_not_overwritten(
            self, real_db, agent, people, provider, monkeypatch, audit):
        provider.roles["primary"] = [people["alice"]]
        ask = _agent_ask(agent)
        original = real_db.readdress_operator_queue_item

        def race(item_id, *a, **k):
            _svc().answer(_row(real_db, item_id), response="Yes", response_text=None,
                          actor=_actor(people["alice"]))
            return original(item_id, *a, **k)

        monkeypatch.setattr(real_db, "readdress_operator_queue_item", race)
        audit.rows.clear()
        provider.roles["primary"] = [people["bob"]]

        assert _readdress(agent) == {"readdressed": 0, "unchanged": 1}
        row = _row(real_db, ask)
        assert row["status"] == "responded"
        assert row["resolved_to"] == [people["alice"]]
        assert row["addressed_to_email"] == people["alice"]
        assert audit.actions(READDRESSED) == []

    def test_the_one_row_primitive_refuses_nothing_and_reports_the_change(
            self, real_db, agent, people, provider):
        svc = _svc()
        provider.roles["primary"] = [people["alice"]]
        ask = _agent_ask(agent)

        assert svc.readdress_item(ask, [people["bob"]], people["bob"],
                                  reason=svc.ASSIGNMENT_CHANGED) is True
        assert _row(real_db, ask)["resolved_to"] == [people["bob"]]
        assert svc.readdress_item(ask, [people["bob"]], people["bob"],
                                  reason=svc.ASSIGNMENT_CHANGED) is False
        assert svc.readdress_item("no-such-row", [people["bob"]], people["bob"],
                                  reason=svc.ASSIGNMENT_CHANGED) is False

    def test_a_row_whose_audience_moved_between_the_read_and_the_write_is_not_overwritten(
            self, real_db, agent, people, provider):
        """The `resolved_to` arm of the CAS. A concurrent `drop_addressee` (or
        another re-address) changes `resolved_to` while the row stays
        `pending` and `addressed_to_email` stays as read — so neither the
        status arm nor the single arm would catch it. Only a write that
        presents the stale raw `resolved_to` proves this arm is live."""
        provider.roles["primary"] = [people["alice"]]
        ask = _agent_ask(agent)
        as_read = real_db.read_operator_queue_address(ask)
        assert as_read["status"] == "pending"

        # Another writer moves the audience first, from the same read — and
        # keeps the single addressee, so only `resolved_to` differs afterwards.
        single = as_read["addressed_to_email"]
        assert real_db.readdress_operator_queue_item(
            ask, [people["bob"]], single,
            expect_single=as_read["addressed_to_email"],
            expect_resolved_raw=as_read["resolved_to"]) is True

        # Our write still carries the stale `resolved_to`: refused, row untouched.
        assert real_db.readdress_operator_queue_item(
            ask, [people["carol"]], single,
            expect_single=as_read["addressed_to_email"],
            expect_resolved_raw=as_read["resolved_to"]) is False
        row = _row(real_db, ask)
        assert row["status"] == "pending"
        assert row["addressed_to_email"] == single
        assert row["resolved_to"] == [people["bob"]]


# ===========================================================================
# The trigger is a literal the ent#467 guard discovers
# ===========================================================================

def test_the_trigger_literal_is_discovered_by_the_ws_guard():
    """The payload is built at the decision site through `_broadcast_payload`,
    so the ent#467 guard (CI) classifies it as agent-keyed. The live consumer
    of this pin is the guard itself."""
    import inspect

    from services import ask_service as svc
    import importlib.util

    guard = os.path.join(os.path.dirname(__file__), "test_ent467_ws_agent_scope.py")
    spec = importlib.util.spec_from_file_location("ent467_guard", guard)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _broadcast_payloads = module._broadcast_payloads

    _, start = inspect.getsourcelines(svc.readdress_role)
    end = start + len(inspect.getsource(svc.readdress_role).splitlines())
    hits = [(rel, event, skeleton, line) for rel, event, skeleton, line in _broadcast_payloads()
            if rel == "services/ask_service.py" and start <= line < end]
    assert hits, "no /ws payload literal found inside readdress_role"
    (rel, event, skeleton, line), = hits
    assert event == "operator_queue_new"
    assert skeleton == {"type": "x", "data": {"id": "x", "agent_name": "x"}}
