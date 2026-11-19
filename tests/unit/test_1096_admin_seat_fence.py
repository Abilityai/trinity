"""The admin seat on a gate approval nobody else can decide
(abilityai/trinity-enterprise#1096, OSS sink half).

The operator ruling of 2026-11-13: who decides on an agent's behalf is one
order everywhere — primary, else the owner, else an instance admin. Arms one
and two already exist as people on the row (`resolved_to`). Arm three had no
answering path: `may_end` fences a gate approval to its snapshot, an empty
snapshot is nobody, and an admin may only CANCEL. So a card raised to the
admin seat would be stranded.

Pinned here (C1 of the plan):

* R1 — a gate raise may address nobody ONLY with the platform's admin-seat
  stamp; without it the `ValueError` stands; an agent cannot author the stamp;
* R8 — the admin arm answers a stamped, empty, gate approval and nothing
  else, and only for a principal that passes the admin gate AND is a person
  (agent, connector, `ops`, loopback and stand-in principals refused);
* `addressed_view` gives the card exactly the sink's answer, for every actor;
* R4 — a re-address compares and clears the stamps, so a card moved off the
  admin seat stops answering to admins, and an unchanged audience with a new
  stamp is still written;
* R5 — an answer or cancel read before a re-address loses (`readdressed`);
* R6 — a bulk sweep skips a fenced row the actor is not addressed on;
* R7 — the refusal names who the card waits for: the structured fields always,
  the message only for a gate approval (role refusals stay person-free);
* R9 — the admin arm records `admin:<username>` for an admin with no email;
* `ADMIN_READDRESSED` and the per-family resolver registry exist.

Harness: the unit island's per-process SQLite, the ent#1095 shape.
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

import services.ask_service  # noqa: E402,F401
import services.operator_queue_service  # noqa: E402,F401
import services.operator_resume_service  # noqa: E402,F401
import services.skill_gate_service  # noqa: E402,F401

SEAT = "workspace_decider_seat"
REASON = "workspace_operators_reason"


def _uniq(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}@example.com"


def _consent_id():
    return "gate-project-agent-" + uuid.uuid4().hex[:12]


@pytest.fixture
def real_db():
    from database import db as real
    return real


@pytest.fixture
def agent():
    return f"agent-1096-{uuid.uuid4().hex[:6]}"


@pytest.fixture
def people():
    return {"alice": _uniq("alice"), "bob": _uniq("bob"), "carol": _uniq("carol")}


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


@pytest.fixture
def audit():
    return _Audit()


@pytest.fixture(autouse=True)
def sink(monkeypatch, real_db, audit):
    from services import ask_service as svc
    from services import operator_queue_service as oqs
    from services import operator_resume_service as ors
    from services.rate_limiter import RateLimitResult

    monkeypatch.setattr(ors, "spawn_on_loop", lambda factory: asyncio.run(factory()))
    monkeypatch.setattr(ors, "spawn_ending_dispatch", lambda rows, **kw: None)
    monkeypatch.setattr(ors, "spawn_resume_dispatch", lambda item, **kw: None)
    monkeypatch.setattr(svc, "_opted_in", lambda agent_name: True)
    monkeypatch.setattr(svc, "_observers", [])
    monkeypatch.setattr(svc, "platform_audit_service", audit)
    monkeypatch.setattr(svc, "_websocket_manager", _WS())
    monkeypatch.setattr(svc, "_owner_email", lambda agent: None)
    monkeypatch.setattr(oqs, "_workspace_attachment",
                        lambda agent, email, **_: (f"thread-{email}", False))
    monkeypatch.setattr(oqs.rate_limiter, "check",
                        lambda *a, **k: RateLimitResult(True, 10, 0, 60))
    monkeypatch.setattr(svc, "_readdress_resolvers", {})
    yield


def _svc():
    from services import ask_service
    return ask_service


def _user(**kw):
    """The principal the routes hand the sink (`models.User`)."""
    from models import User
    base = dict(id=1, username="root", role="admin", email=None)
    base.update(kw)
    return User(**base)


def _actor(actor_email="", **user):
    return _svc().Actor(email=actor_email, user=_user(**user) if user else None)


def _consent(agent, people=(), *, seat="primary", reason=None, request_id=None):
    """A consent card as the enterprise module raises it: a gate approval to
    the decider, stamped with the seat that picked the people."""
    ctx = {SEAT: seat}
    if reason:
        ctx[REASON] = reason
    body = {"request_id": request_id or _consent_id(), "type": "approval",
            "title": "Add the agent to a project?", "question": "Allow it?",
            "options": ["Approve", "Decline"]}
    return _svc().raise_ask(agent, body, raised_by="gate", channel="gate",
                            addressees=list(people), to_role_override="primary",
                            attach_chat=False, platform_context=ctx)["id"]


def _row(real_db, ask_id):
    return real_db.get_operator_queue_item(ask_id)


# ===========================================================================
# R1 — raising to the admin seat
# ===========================================================================

class TestRaiseToTheAdminSeat:
    def test_an_empty_audience_with_the_admin_stamp_is_raised(self, real_db, agent):
        ask = _consent(agent, [], seat="admin", reason="owner_no_email")
        row = _row(real_db, ask)
        assert row["status"] == "pending"
        assert row["resolved_to"] is None
        assert row["addressed_to_email"] is None
        assert row["to_role"] == "primary"
        assert row["context"][SEAT] == "admin"
        assert row["context"][REASON] == "owner_no_email"

    @pytest.mark.parametrize("ctx", [None, {SEAT: "primary"}, {SEAT: "owner"}])
    def test_an_empty_audience_without_the_admin_stamp_is_still_refused(self, agent, ctx):
        body = {"request_id": _consent_id(), "type": "approval", "title": "t",
                "question": "q", "options": ["Approve", "Decline"]}
        with pytest.raises(ValueError, match="must address someone"):
            _svc().raise_ask(agent, body, raised_by="gate", channel="gate", addressees=[],
                             to_role_override="primary", platform_context=ctx)

    def test_an_agent_cannot_author_the_stamp(self, real_db, agent):
        body = {"request_id": f"ask-{uuid.uuid4().hex[:8]}", "type": "question",
                "title": "t", "question": "q", "to": "operator",
                "context": {SEAT: "admin", "note": "kept"}}
        ask = _svc().raise_ask(agent, body, raised_by="agent", channel="mcp")["id"]
        ctx = _row(real_db, ask)["context"]
        assert SEAT not in ctx
        assert ctx["note"] == "kept"

    def test_the_stamp_is_a_platform_key(self):
        from services import operator_queue_service as oqs
        assert oqs._WORKSPACE_DECIDER_SEAT_KEY == SEAT
        assert SEAT in oqs._PLATFORM_CONTEXT_KEYS


# ===========================================================================
# R8 — who the admin arm admits
# ===========================================================================

_ADMIN_PERSON = dict(role="admin", email="ops@example.com")
_REFUSED = {
    "non_admin_person": dict(role="creator", email="c@example.com"),
    "admin_agent_key": dict(role="admin", mcp_scope="agent", agent_name="some-agent"),
    "admin_connector_key": dict(role="admin", mcp_scope="user", connector_agent="x"),
    "admin_ops_key": dict(role="admin", mcp_scope="ops"),
    "admin_system_key": dict(role="admin", mcp_scope="system"),
    "admin_loopback": dict(role="admin", is_event_loopback=True),
}


class TestTheAdminArm:
    def test_an_admin_person_may_decide_a_stamped_empty_approval(self, real_db, agent):
        row = _row(real_db, _consent(agent, [], seat="admin"))
        assert _svc().may_end(row, _actor(**_ADMIN_PERSON)) is True
        assert _svc().may_end(row, _actor(**_ADMIN_PERSON), cancelling=True) is True

    def test_an_admin_without_email_may_decide_it(self, real_db, agent):
        row = _row(real_db, _consent(agent, [], seat="admin"))
        assert _svc().may_end(row, _actor("root", role="admin", email=None)) is True

    @pytest.mark.parametrize("who", sorted(_REFUSED))
    def test_no_other_principal_may_decide_it(self, real_db, agent, who):
        row = _row(real_db, _consent(agent, [], seat="admin"))
        actor = _actor(_REFUSED[who].get("email") or "", **_REFUSED[who])
        assert _svc().may_end(row, actor) is False

    def test_a_stand_in_principal_without_mcp_scope_fails_closed(self, real_db, agent):
        row = _row(real_db, _consent(agent, [], seat="admin"))
        actor = _svc().Actor(email="x@example.com", user=SimpleNamespace(role="admin"))
        assert _svc().may_end(row, actor) is False

    def test_an_unstamped_empty_gate_approval_still_waits_on_nobody(self, real_db, agent, people):
        ask = _consent(agent, [people["alice"]], seat="primary")
        # the skill gate's arm-three hole (T3, deferred): emptied, no stamp
        assert _svc().readdress_item(ask, [], None, reason=_svc().ASSIGNMENT_CHANGED,
                                     platform_context={SEAT: None})
        row = _row(real_db, ask)
        assert row["resolved_to"] is None and SEAT not in (row["context"] or {})
        assert _svc().may_end(row, _actor(**_ADMIN_PERSON)) is False

    def test_the_admin_arm_never_opens_a_seated_card(self, real_db, agent, people):
        row = _row(real_db, _consent(agent, [people["alice"]], seat="primary"))
        assert _svc().may_end(row, _actor(**_ADMIN_PERSON)) is False
        assert _svc().may_end(row, _actor(people["alice"])) is True

    def test_the_admin_arm_needs_a_gate_approval(self, real_db, agent):
        """The stamp is never the only authority (R8): a non-approval gate
        row carrying it is not decided through the arm."""
        row = dict(_row(real_db, _consent(agent, [], seat="admin")))
        for field, value in (("type", "question"), ("raised_by", "agent")):
            forged = {**row, field: value}
            assert _svc()._admin_seated(forged) is False

    def test_the_admin_answers_through_the_sink(self, real_db, agent):
        ask = _consent(agent, [], seat="admin")
        ending = _svc().answer(_row(real_db, ask), response="Approve", response_text=None,
                               actor=_actor("root", role="admin", email=None, username="root"))
        row = ending.rows[0]
        assert row["disposition"] == "answered"
        assert row["response"] == "Approve"
        # R9: never a bare username where an email is read
        assert row["responded_by_email"] == "admin:root"

    def test_an_admin_with_an_email_is_recorded_by_it(self, real_db, agent):
        ask = _consent(agent, [], seat="admin")
        ending = _svc().answer(_row(real_db, ask), response="Approve", response_text=None,
                               actor=_actor("ops@example.com", **_ADMIN_PERSON))
        assert ending.rows[0]["responded_by_email"] == "ops@example.com"


# ===========================================================================
# The card and the sink agree
# ===========================================================================

_ACTORS = {
    "admin_person": ("ops@example.com", _ADMIN_PERSON),
    "admin_no_email": ("root", dict(role="admin", email=None)),
    "alice": (None, None),
    "carol": (None, None),
    **{k: (v.get("email") or "", v) for k, v in _REFUSED.items()},
}


class TestCardMatchesSink:
    @pytest.mark.parametrize("seat", ["admin", "primary"])
    @pytest.mark.parametrize("who", sorted(_ACTORS))
    def test_addressed_view_is_may_end(self, real_db, agent, people, seat, who):
        audience = [] if seat == "admin" else [people["alice"]]
        row = _row(real_db, _consent(agent, audience, seat=seat))
        email, user = _ACTORS[who]
        actor = (_svc().Actor(email=people[who]) if user is None
                 else _actor(email, **user))
        view = _svc().addressed_view(row, actor)
        assert view["can_decide"] is _svc().may_end(row, actor)
        assert view["waiting_on"] == audience
        assert view["waiting_on_seat"] == seat

    def test_an_unstamped_row_keeps_its_shape(self, real_db, agent, people):
        body = {"request_id": "gate-" + uuid.uuid4().hex + uuid.uuid4().hex[:8], "type": "approval",
                "title": "t", "question": "q", "options": ["Approve", "Decline"]}
        ask = _svc().raise_ask(agent, body, raised_by="gate", channel="gate",
                               addressees=[people["alice"]], to_role_override="primary")["id"]
        view = _svc().addressed_view(_row(real_db, ask), _svc().Actor(email=people["alice"]))
        assert view == {"waiting_on": [people["alice"]], "can_decide": True}


# ===========================================================================
# R4 — re-address compares and clears the stamps
# ===========================================================================

class TestStampsFollowTheReaddress:
    def test_off_the_admin_seat_the_admins_stop_deciding(self, real_db, agent, people):
        ask = _consent(agent, [], seat="admin", reason="owner_no_email")
        assert _svc().readdress_item(ask, [people["alice"]], people["alice"],
                                     reason=_svc().ASSIGNMENT_CHANGED, attach_chat=False,
                                     platform_context={SEAT: "primary", REASON: None})
        row = _row(real_db, ask)
        assert row["context"][SEAT] == "primary"
        assert REASON not in row["context"]
        assert _svc().may_end(row, _actor(**_ADMIN_PERSON)) is False
        assert _svc().may_end(row, _svc().Actor(email=people["alice"])) is True

    def test_onto_the_admin_seat_the_admins_decide(self, real_db, agent, people):
        ask = _consent(agent, [people["alice"]], seat="owner")
        assert _svc().readdress_item(ask, [], None, reason=_svc().ADMIN_READDRESSED,
                                     platform_context={SEAT: "admin"})
        row = _row(real_db, ask)
        assert row["resolved_to"] is None
        assert _svc().may_end(row, _actor(**_ADMIN_PERSON)) is True
        assert _svc().may_end(row, _svc().Actor(email=people["alice"])) is False

    def test_same_people_new_seat_is_written(self, real_db, agent, people):
        ask = _consent(agent, [people["alice"]], seat="owner")
        assert _svc().readdress_item(ask, [people["alice"]], people["alice"],
                                     reason=_svc().ASSIGNMENT_CHANGED,
                                     platform_context={SEAT: "primary"})
        assert _row(real_db, ask)["context"][SEAT] == "primary"

    def test_same_people_same_seat_writes_nothing(self, real_db, agent, people, audit):
        ask = _consent(agent, [people["alice"]], seat="primary")
        audit.rows.clear()
        assert not _svc().readdress_item(ask, [people["alice"]], people["alice"],
                                         reason=_svc().ASSIGNMENT_CHANGED,
                                         platform_context={SEAT: "primary", REASON: None})
        assert audit.actions("platform_readdressed") == []

    def test_an_admin_readdress_is_audited_with_its_actor(self, real_db, agent, people, audit):
        ask = _consent(agent, [people["alice"]], seat="owner")
        audit.rows.clear()
        actor = _actor("ops@example.com", **_ADMIN_PERSON)
        assert _svc().readdress_item(ask, [], None, reason=_svc().ADMIN_READDRESSED,
                                     platform_context={SEAT: "admin"}, actor=actor,
                                     has_reason=True)
        (row,) = audit.actions("platform_readdressed")
        assert row["source"] == "api"
        assert row["actor_email"] == "ops@example.com"
        assert row["details"]["reason"] == "admin_readdressed"
        assert row["details"]["has_reason"] is True
        assert people["alice"] not in repr(row["details"])

    def test_admin_readdressed_is_a_readdress_reason(self):
        assert _svc().ADMIN_READDRESSED in _svc().PLATFORM_READDRESS_REASONS


# ===========================================================================
# R5 — an ending read before a re-address loses
# ===========================================================================

class TestEndingLosesToReaddress:
    def test_a_replaced_primarys_answer_is_not_recorded(self, real_db, agent, people):
        ask = _consent(agent, [people["alice"]], seat="primary")
        stale = _row(real_db, ask)
        assert _svc().readdress_item(ask, [people["bob"]], people["bob"],
                                     reason=_svc().ASSIGNMENT_CHANGED, attach_chat=False)
        with pytest.raises(_svc().AskConflict) as exc:
            _svc().answer(stale, response="Approve", response_text=None,
                          actor=_svc().Actor(email=people["alice"]))
        assert exc.value.code == "readdressed"
        assert _row(real_db, ask)["status"] == "pending"

    def test_an_admin_answer_read_on_the_admin_seat_loses_once_moved(self, real_db, agent, people):
        ask = _consent(agent, [], seat="admin")
        stale = _row(real_db, ask)
        assert _svc().readdress_item(ask, [people["bob"]], people["bob"],
                                     reason=_svc().ASSIGNMENT_CHANGED, attach_chat=False,
                                     platform_context={SEAT: "primary"})
        with pytest.raises(_svc().AskConflict) as exc:
            _svc().answer(stale, response="Approve", response_text=None,
                          actor=_actor("ops@example.com", **_ADMIN_PERSON))
        assert exc.value.code == "readdressed"
        assert _row(real_db, ask)["status"] == "pending"

    def test_a_cancel_read_before_the_move_loses(self, real_db, agent, people, monkeypatch):
        from services import ask_service as svc
        ask = _consent(agent, [people["alice"]], seat="primary")
        stale = _row(real_db, ask)
        assert svc.readdress_item(ask, [people["bob"]], people["bob"],
                                  reason=svc.ASSIGNMENT_CHANGED, attach_chat=False)
        real_get = svc.db.get_operator_queue_item
        monkeypatch.setattr(svc.db, "get_operator_queue_item",
                            lambda i: stale if i == ask else real_get(i))
        with pytest.raises(svc.AskConflict) as exc:
            svc.cancel(ask, actor=svc.Actor(email=people["alice"]))
        assert exc.value.code == "readdressed"
        assert real_get(ask)["status"] == "pending"

    def test_an_unmoved_card_is_answered(self, real_db, agent, people):
        ask = _consent(agent, [people["alice"]], seat="primary")
        ending = _svc().answer(_row(real_db, ask), response="Approve", response_text=None,
                               actor=_svc().Actor(email=people["alice"]))
        assert ending.rows[0]["disposition"] == "answered"


class TestTheCompareAndSetHoldsTheAddress:
    """The window the service pre-check cannot see: a re-address landing after
    `_ending_expectation` read the address and before the ending's UPDATE.
    Driven at the db layer with the expectation the service built, on a row
    whose `resolved_to` moves while `addressed_to_email` stays the same (two
    people, no single addressee) — so only the `resolved_to` clause can lose."""

    def _moved_under(self, real_db, agent, people):
        ask = _consent(agent, [people["alice"], people["bob"]], seat="primary")
        expect = _svc()._ending_expectation(_row(real_db, ask),
                                            _svc().Actor(email=people["alice"]))
        assert expect is not None and expect["addressed_to_email"] is None
        assert _svc().readdress_item(ask, [people["alice"], people["carol"]], None,
                                     reason=_svc().ASSIGNMENT_CHANGED, attach_chat=False)
        moved = real_db.read_operator_queue_address(ask)
        assert moved["addressed_to_email"] is None
        assert moved["resolved_to"] != expect["resolved_to"]
        return ask, expect

    def test_an_answer_read_before_the_move_does_not_land(self, real_db, agent, people):
        ask, expect = self._moved_under(real_db, agent, people)
        out = real_db.respond_to_operator_queue_item(
            ask, "Approve", None, None, people["alice"], expect_address=expect)
        assert out["_status_conflict"] is True
        assert _row(real_db, ask)["status"] == "pending"

    def test_a_cancel_read_before_the_move_does_not_land(self, real_db, agent, people):
        ask, expect = self._moved_under(real_db, agent, people)
        out = real_db.cancel_operator_queue_item(
            ask, disposed_by_email=people["alice"], expect_address=expect)
        assert out["_status_conflict"] is True
        assert _row(real_db, ask)["status"] == "pending"


# ===========================================================================
# R6 — a bulk sweep skips fenced rows the actor is not on
# ===========================================================================

class TestBulkSkipsFenced:
    def test_an_admin_sweep_skips_a_card_addressed_to_someone_else(self, real_db, agent, people):
        fenced = _consent(agent, [people["alice"]], seat="primary")
        admin_seat = _consent(agent, [], seat="admin")
        body = {"request_id": f"ask-{uuid.uuid4().hex[:8]}", "type": "question",
                "title": "t", "question": "q", "to": "operator"}
        plain = _svc().raise_ask(agent, body, raised_by="agent", channel="mcp")["id"]

        ending = _svc().bulk_cancel([fenced, admin_seat, plain], None,
                                    actor=_actor("ops@example.com", **_ADMIN_PERSON))
        assert [r["id"] for r in ending.rows] == [plain]
        assert ending.skipped_fenced == 2
        assert _row(real_db, fenced)["status"] == "pending"
        assert _row(real_db, admin_seat)["status"] == "pending"

    def test_the_addressee_may_sweep_their_own_card(self, real_db, agent, people):
        mine = _consent(agent, [people["alice"]], seat="primary")
        ending = _svc().bulk_cancel([mine], None, actor=_svc().Actor(email=people["alice"]))
        assert [r["id"] for r in ending.rows] == [mine]
        assert ending.skipped_fenced == 0


# ===========================================================================
# R7 — the refusal names who the card waits for
# ===========================================================================

class TestNamedRefusal:
    def test_a_seated_card_names_its_seat_and_person(self, real_db, agent, people):
        row = _row(real_db, _consent(agent, [people["alice"]], seat="primary"))
        with pytest.raises(_svc().AskNotAddressee) as exc:
            _svc().answer(row, response="Approve", response_text=None,
                          actor=_svc().Actor(email=people["carol"]))
        e = exc.value
        assert e.waiting_on == [people["alice"]]
        assert e.seat == "primary"
        msg = e.message()
        assert people["alice"] in msg and "primary" in msg
        assert "re-address" not in msg

    def test_the_admin_seat_names_the_admins_and_why(self, real_db, agent):
        row = _row(real_db, _consent(agent, [], seat="admin", reason="owner_no_email"))
        with pytest.raises(_svc().AskNotAddressee) as exc:
            _svc().answer(row, response="Approve", response_text=None,
                          actor=_actor("c@example.com", role="creator", email="c@example.com"))
        e = exc.value
        assert e.waiting_on == [] and e.seat == "admin"
        assert "instance admin" in e.message()
        assert "no email" in e.message()

    def test_an_admin_who_may_not_decide_is_told_the_two_verbs(self, real_db, agent, people):
        row = _row(real_db, _consent(agent, [people["alice"]], seat="owner"))
        with pytest.raises(_svc().AskNotAddressee) as exc:
            _svc().answer(row, response="Approve", response_text=None,
                          actor=_actor("ops@example.com", **_ADMIN_PERSON))
        msg = exc.value.message(admin_verbs=True)
        assert people["alice"] in msg and "owner" in msg
        assert "re-address" in msg and "reason" in msg

    def test_a_role_refusal_stays_person_free(self):
        e = _svc().AskNotAddressee("x", role="role:finance", waiting_on=["p@example.com"],
                                   seat="role:finance")
        assert "p@example.com" not in e.message()
        assert e.message() == e.role_message()
        # the ent#1042 contract: no person anywhere in a role refusal's body
        assert "@" not in repr(e.detail())
        assert e.detail()["seat"] == "role:finance"

    def test_a_role_rows_readdressed_body_stays_person_free(self, real_db, agent, people):
        """The 409 an answer gets when a role row moved under it names the
        role and its seat, never a person: the text reaches a model through
        MCP, the same contract as the role row's 403."""
        body = {"request_id": f"gate-{uuid.uuid4().hex[:8]}", "type": "approval",
                "title": "t", "question": "q", "options": ["Approve", "Decline"]}
        ask = _svc().raise_ask(agent, body, raised_by="gate", channel="gate",
                               addressees=[people["alice"]], to_role_override="role:cfo",
                               attach_chat=False)["id"]
        stale = _row(real_db, ask)
        assert _svc().readdress_item(ask, [people["bob"]], people["bob"],
                                     reason=_svc().ASSIGNMENT_CHANGED, attach_chat=False)
        with pytest.raises(_svc().AskConflict) as exc:
            _svc().answer(stale, response="Approve", response_text=None,
                          actor=_svc().Actor(email=people["alice"]))
        assert exc.value.code == "readdressed"
        d = _svc().readdressed_detail(exc.value.item)
        assert d["code"] == "readdressed"
        assert d["seat"] == "role:cfo"
        assert "waiting_on" not in d
        assert "@" not in d["message"]
        assert "Cfo role" in d["message"]
        assert _row(real_db, ask)["status"] == "pending"

    def test_the_detail_carries_the_structured_fields(self, real_db, agent, people):
        row = _row(real_db, _consent(agent, [people["alice"]], seat="primary"))
        e = _svc().not_addressee(row)
        d = e.detail()
        assert d["code"] == "not_addressee"
        assert d["waiting_on"] == [people["alice"]]
        assert d["seat"] == "primary"
        assert d["message"] == e.message()


# ===========================================================================
# The per-family re-address resolver registry
# ===========================================================================

class TestResolverRegistry:
    def test_a_registered_family_resolves_by_prefix(self):
        fn = lambda agent_name: None  # noqa: E731
        _svc().register_readdress_resolver("gate-project-agent-", fn)
        assert _svc().readdress_resolver_for("gate-project-agent-abc") is fn
        assert _svc().readdress_resolver_for("gate-" + "a" * 40) is None
        assert _svc().readdress_resolver_for(None) is None

    @pytest.mark.parametrize("prefix", ["", "ask-", "project-agent-"])
    def test_only_a_platform_family_may_register(self, prefix):
        with pytest.raises(ValueError):
            _svc().register_readdress_resolver(prefix, lambda a: None)

    @pytest.mark.parametrize("prefix", ["gate-a", "gate-0f", "gate-" + "a" * 40])
    def test_a_prefix_a_skill_gate_id_could_carry_is_refused(self, prefix):
        """A prefix of hex digits only could prefix a skill-gate id
        (`gate-` + 40 hex), and its resolver would then put a skill-gate
        approval on the admin seat, which its resume refuses (T3)."""
        with pytest.raises(ValueError):
            _svc().register_readdress_resolver(prefix, lambda a: None)
        assert _svc().readdress_resolver_for("gate-" + "a" * 40) is None
