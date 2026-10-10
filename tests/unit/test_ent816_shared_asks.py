"""An ask to a role several people fill reaches every one of them
(abilityai/trinity-enterprise#816).

`role_addressing.resolve` records every person a role resolves to in
`resolved_to`, but names a single Workspace addressee (`addressed_to_email`)
only when exactly one person resolved. The Workspace asks list filtered on
`addressed_to_email = viewer`, so an ask to a role two people fill was stored
with no addressee and rendered in NOBODY's Inbox — while the receipt said
`resolved: true`.

The operator ruling of 2026-10-07 (in the issue thread): deliver the ONE ask to
every person the role resolves to (fan-out on read against `resolved_to`, no
duplicate rows); the first answer wins and the others see that someone else
answered; Dismiss and Discuss are refused on a shared ask for now. Refusing the
raise with `role_resolves_to_several` was rejected.

Harness: the unit island's per-process SQLite. Agent names and emails are
unique per test, so the shared DB never crosses tests. The roster, the
assignment provider and the sink's side effects are stubbed.
"""
from __future__ import annotations

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
import client_portal.asks.service  # noqa: E402,F401
import client_portal.db  # noqa: E402,F401


def _uniq(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}@example.com"


@pytest.fixture
def real_db():
    from database import db as real
    return real


@pytest.fixture
def agent():
    return f"agent-816-{uuid.uuid4().hex[:6]}"


@pytest.fixture
def people():
    return {"alice": _uniq("alice"), "bob": _uniq("bob"), "carol": _uniq("carol")}


@pytest.fixture(autouse=True)
def roster(monkeypatch):
    """Everyone is on the roster — so a non-addressee's refusal is about the
    addressing, never about access to the agent."""
    import client_portal.service as portal_service
    monkeypatch.setattr(portal_service, "agent_on_roster", lambda *_a, **_kw: True)


@pytest.fixture(autouse=True)
def sink(monkeypatch, real_db):
    """The real sink over the real SQLite; its side effects recorded."""
    import json as _json
    from services import ask_service as svc
    from services import assignment_provider
    from services import operator_queue_service as oqs
    from services import operator_resume_service as ors
    from services.rate_limiter import RateLimitResult

    endings = []

    class _Audit:
        async def log(self, **kw):
            return "evt"

    class _WS:
        async def broadcast(self, message):
            _json.loads(message)

    monkeypatch.setattr(ors, "spawn_on_loop", lambda factory: None)
    monkeypatch.setattr(ors, "spawn_ending_dispatch",
                        lambda rows, **kw: endings.append((list(rows), kw)))
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
    yield {"endings": endings}
    assignment_provider.clear_provider()


class _Provider:
    def __init__(self, people):
        self.people = people

    def assignment_for(self, agent_name, triggered_by):
        return None

    def people_for(self, agent_name, role):
        value = self.people.get(role)
        return None if value is None else {"emails": value}


def _provide(roles):
    from services import assignment_provider
    assignment_provider.register_provider(_Provider(roles))


def _raise(agent, request_id=None, *, to="approver", kind="approval", **over):
    from services import ask_service
    rid = request_id or f"r-{uuid.uuid4().hex[:10]}"
    body = {"request_id": rid, "type": kind, "title": "Pay invoice",
            "question": "Release the payment?", "options": ["approve", "reject"],
            "to": to}
    if kind == "approval":
        body["proposal"] = {"pay": 500, "to": f"vendor-{rid}"}
    body.update(over)
    return ask_service.raise_ask(agent, body, raised_by="agent", channel="mcp")


def _svc():
    from client_portal.asks import service
    return service


def _ids(email, **kw):
    return [a.id for a in _svc().list_asks_page(email, is_platform=False, **kw).items]


# ===========================================================================
# Fan-out on read — every resolved person's Inbox, nobody else's
# ===========================================================================

class TestEveryResolvedPersonSeesIt:
    def test_an_ask_to_a_role_two_people_fill_lands_in_both_inboxes(self, agent, people):
        _provide({"approver": [people["alice"], people["bob"]]})
        receipt = _raise(agent)

        assert _ids(people["alice"]) == [receipt["id"]]
        assert _ids(people["bob"]) == [receipt["id"]]

    def test_the_count_is_each_persons_own(self, agent, people):
        _provide({"approver": [people["alice"], people["bob"]]})
        _raise(agent)

        for who in ("alice", "bob"):
            page = _svc().list_asks_page(people[who], is_platform=False)
            assert page.total == 1 and len(page.items) == 1

    def test_one_row_not_one_per_person(self, real_db, agent, people):
        _provide({"approver": [people["alice"], people["bob"]]})
        receipt = _raise(agent)

        rows = real_db.list_operator_queue_items(agent_name=agent, limit=50)
        assert [r["id"] for r in rows] == [receipt["id"]]

    def test_someone_the_role_does_not_resolve_to_never_sees_it(self, agent, people):
        """Invariant #8: on the roster, but not addressed — the list omits it and
        every per-ask door answers the uniform 404."""
        _provide({"approver": [people["alice"], people["bob"]]})
        receipt = _raise(agent)
        carol = people["carol"]

        assert _ids(carol) == []
        assert _ids(carol, include_ended=True) == []
        for door in (
            lambda: _svc().answer_ask(receipt["id"], carol, False, "approve", None),
            lambda: _svc().dismiss_ask(receipt["id"], carol, False),
            lambda: _svc().discuss_ask(receipt["id"], carol, False),
            lambda: _svc().get_ask_context(receipt["id"], carol, False),
        ):
            with pytest.raises(_svc().AskError) as exc:
                door()
            assert (exc.value.status_code, exc.value.code) == (404, "not_found")

    def test_the_projection_says_it_is_shared(self, agent, people):
        _provide({"approver": [people["alice"], people["bob"]]})
        _raise(agent)

        [ask] = _svc().list_asks_page(people["bob"], is_platform=False).items
        assert ask.shared is True

    def test_a_one_person_ask_is_not_shared(self, agent, people):
        _provide({"approver": [people["alice"]]})
        _raise(agent)

        [ask] = _svc().list_asks_page(people["alice"], is_platform=False).items
        assert ask.shared is False

    def test_the_receipt_is_resolved_because_a_person_will_see_it(self, agent, people):
        _provide({"approver": [people["alice"], people["bob"]]})
        receipt = _raise(agent)
        assert receipt["resolved"] is True
        assert _ids(people["alice"]) == [receipt["id"]]

    def test_an_expired_shared_ask_reads_expired_in_each_inbox(self, agent, people):
        _provide({"approver": [people["alice"], people["bob"]]})
        receipt = _raise(agent)
        from sqlalchemy import update
        from db.engine import get_engine
        from db.tables import operator_queue
        with get_engine().begin() as conn:
            conn.execute(update(operator_queue).where(operator_queue.c.id == receipt["id"])
                         .values(expires_at="2020-01-01T00:00:00Z"))

        for who in ("alice", "bob"):
            [ask] = _svc().list_asks_page(people[who], is_platform=False,
                                          include_ended=True).items
            assert (ask.id, ask.status) == (receipt["id"], "expired")


# ===========================================================================
# The first answer wins; the others see who answered
# ===========================================================================

class TestFirstAnswerWins:
    def test_the_second_person_is_told_it_is_already_answered(self, real_db, agent, people):
        _provide({"approver": [people["alice"], people["bob"]]})
        receipt = _raise(agent)

        mine = _svc().answer_ask(receipt["id"], people["alice"], False, "approve", None)
        assert (mine.status, mine.ended_by) == ("answered", "you")

        with pytest.raises(_svc().AskError) as exc:
            _svc().answer_ask(receipt["id"], people["bob"], False, "reject", None)
        assert exc.value.code == "already_resolved"
        row = real_db.get_operator_queue_item(receipt["id"])
        assert row["response"] == "approve" and row["responded_by_email"] == people["alice"]

    def test_the_others_see_that_someone_else_answered_and_who(self, agent, people):
        _provide({"approver": [people["alice"], people["bob"]]})
        receipt = _raise(agent)
        _svc().answer_ask(receipt["id"], people["alice"], False, "approve", None)

        assert _ids(people["bob"]) == []          # no longer waiting on Bob
        [ask] = _svc().list_asks_page(people["bob"], is_platform=False,
                                      include_ended=True).items
        assert ask.status == "answered"
        assert ask.ended_by == "someone_else"
        assert ask.answered_by == people["alice"]

    def test_an_operator_answer_is_still_the_operator_and_unnamed(self, real_db, agent, people):
        """Only a co-addressee is named — an operator's email never crosses."""
        _provide({"approver": [people["alice"], people["bob"]]})
        receipt = _raise(agent)
        real_db.respond_to_operator_queue_item(receipt["id"], "approve", None, None,
                                               "operator@example.com")

        [ask] = _svc().list_asks_page(people["bob"], is_platform=False,
                                      include_ended=True).items
        assert ask.ended_by == "operator" and ask.answered_by is None


# ===========================================================================
# Dismiss and Discuss are refused on a shared ask (per-person is a follow-up)
# ===========================================================================

class TestSharedAskRefusals:
    def test_dismiss_is_refused_by_name_and_ends_nothing(self, real_db, agent, people, sink):
        _provide({"approver": [people["alice"], people["bob"]]})
        receipt = _raise(agent)

        with pytest.raises(_svc().AskError) as exc:
            _svc().dismiss_ask(receipt["id"], people["alice"], False)
        assert (exc.value.status_code, exc.value.code) == (422, "shared_ask")
        assert real_db.get_operator_queue_item(receipt["id"])["status"] == "pending"
        assert sink["endings"] == []
        assert _ids(people["bob"]) == [receipt["id"]]

    def test_discuss_is_refused_by_name_and_opens_no_chat(self, real_db, agent, people):
        _provide({"approver": [people["alice"], people["bob"]]})
        receipt = _raise(agent)

        with pytest.raises(_svc().AskError) as exc:
            _svc().discuss_ask(receipt["id"], people["bob"], False)
        assert (exc.value.status_code, exc.value.code) == (422, "shared_ask")
        ctx = real_db.get_operator_queue_item(receipt["id"]).get("context") or {}
        assert "workspace_discussion_id" not in ctx


# ===========================================================================
# The SQL membership check is exact
# ===========================================================================

class TestMembershipIsExact:
    @pytest.mark.parametrize("stored,viewer", [
        ("aa{u}@example.com", "a{u}@example.com"),           # a suffix of another
        ("a{u}@example.community", "a{u}@example.com"),       # a prefix of another
        ("axb{u}@example.com", "a_b{u}@example.com"),         # `_` is not a wildcard
        ("ab{u}@example.com", "a%b{u}@example.com"),          # `%` is not a wildcard
    ])
    def test_a_lookalike_email_does_not_match(self, real_db, agent, stored, viewer):
        u = uuid.uuid4().hex[:6]
        stored, viewer = stored.format(u=u), viewer.format(u=u)
        other = _uniq("other")
        out = real_db.create_native_operator_queue_item(
            agent, {"id": f"m-{u}", "type": "question", "title": "t", "question": "q",
                    "options": None, "context": {}, "expires_at": None,
                    "addressed_to_email": None},
            max_pending=None, channel="mcp", raised_by="agent", to_role="approver",
            resolved_to=[stored, other], proposal=None, supersedes_expired=None,
        )
        assert out["outcome"] == "created"
        assert _ids(viewer) == []
        assert _ids(stored) == [out["row"]["id"]]

    def test_the_match_ignores_case(self, real_db, agent):
        u = uuid.uuid4().hex[:6]
        a, b = f"dana-{u}@example.com", f"eve-{u}@example.com"
        out = real_db.create_native_operator_queue_item(
            agent, {"id": f"c-{u}", "type": "question", "title": "t", "question": "q",
                    "options": None, "context": {}, "expires_at": None,
                    "addressed_to_email": None},
            max_pending=None, channel="mcp", raised_by="agent", to_role="approver",
            resolved_to=[a, b], proposal=None, supersedes_expired=None,
        )
        assert _ids(b.upper()) == [out["row"]["id"]]


# ===========================================================================
# Review fixes (PR #3513 pre-landing review)
# ===========================================================================

class TestOnlyAnAnswerNamesTheCoAddressee:
    def test_a_co_addressee_who_cancels_from_the_operating_room_stays_the_operator(
            self, real_db, agent, people):
        """A person the role resolves to may also be an operator. Their CANCEL
        (the Operating Room's verb — the Workspace refuses dismissal on a shared
        ask) is an operator's ending: coarse `operator`, unnamed. Only an ANSWER
        by a co-addressee reads `someone_else` + `answered_by`."""
        _provide({"approver": [people["alice"], people["bob"]]})
        receipt = _raise(agent)
        real_db.cancel_operator_queue_item(receipt["id"], disposed_by_email=people["alice"])

        [ask] = _svc().list_asks_page(people["bob"], is_platform=False,
                                      include_ended=True).items
        assert ask.status == "cancelled"
        assert (ask.ended_by, ask.answered_by) == ("operator", None)


class TestReplaceLinkOnASharedAsk:
    """`replaces` / `replaced_by` are projected when the OTHER ask is the
    viewer's too — by the same addressing rule as the list (shared included)."""

    def test_a_shared_ask_that_replaces_a_shared_ask_links_both_ways(self, agent, people):
        _provide({"approver": [people["alice"], people["bob"]]})
        first = _raise(agent, "q-first", kind="question")
        second = _raise(agent, "q-second", kind="question", replaces="q-first")

        by_id = {a.id: a for a in _svc().list_asks_page(
            people["bob"], is_platform=False, include_ended=True).items}
        assert by_id[second["id"]].replaces == "q-first"
        assert by_id[first["id"]].replaced_by == "q-second"

    def test_the_link_is_withheld_when_the_other_ask_was_never_the_viewers(self, agent, people):
        _provide({"approver": [people["alice"]]})
        _raise(agent, "q-alices", kind="question")
        _provide({"approver": [people["alice"], people["bob"]]})
        second = _raise(agent, "q-shared", kind="question", replaces="q-alices")

        [ask] = _svc().list_asks_page(people["bob"], is_platform=False,
                                      include_ended=True).items
        assert ask.id == second["id"]
        assert ask.replaces is None
