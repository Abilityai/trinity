"""A row the platform addressed reaches its addressee by address
(abilityai/trinity-enterprise#981).

The Workspace Inbox lists an ask only when its viewer is on the raising
agent's roster, re-checked at read time (ent#364). That fence is right for an
agent's own asks: a share revoked afterwards must stop showing them. It is
wrong for a row the PLATFORM raised about the person — the platform chose this
row's addressee; the roster was never its fence — and such a row is raised
under a platform agent nobody is shared with.

So one narrow family reaches its addressee by address alone. A row is reached
when ALL of:

- its request id starts with the reserved sub-prefix
  `gate-addressee-only-reach-` (inside the ent#980 addressee-only family, so
  the Operating Room already lists it to the addressee and admins, and under
  `gate-`, which an agent's raise is refused);
- it was raised by the gate (`raised_by = 'gate'`, the column itself);
- it is addressed to the viewer — the single addressee, or one of the
  people a shared row was delivered to (`resolved_to`).

The condition is SQL, for the page AND its count, and the per-ask door
(`_owned_ask`) applies the same rule, so a non-addressee gets the uniform 404.
The wider `gate-addressee-only-` family (a proposal to a project's creator)
keeps its roster fence: a creator who loses the share stops seeing proposals.

`ask_service.drop_addressee` takes one person off a pending shared row —
removed from `resolved_to`, and the single addressee cleared when it was them —
so a person who leaves stops reading a row that stays for the others.

Harness: the unit island's per-process SQLite (the ent#816 shape). Agent names
and emails are unique per test; the roster is a controllable stub.
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

REACH = "gate-addressee-only-reach-"
PROPOSAL = "gate-addressee-only-project-proposal-"


def _uniq(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}@example.com"


@pytest.fixture
def real_db():
    from database import db as real
    return real


@pytest.fixture
def agent():
    return f"agent-981-{uuid.uuid4().hex[:6]}"


@pytest.fixture
def people():
    return {"alice": _uniq("alice"), "bob": _uniq("bob"), "carol": _uniq("carol")}


@pytest.fixture(autouse=True)
def roster(monkeypatch):
    """Nobody is on any roster unless a test puts them there — so every row a
    viewer sees below is seen by ADDRESS, never by access to the agent."""
    import client_portal.service as portal_service
    on = set()
    monkeypatch.setattr(portal_service, "agent_on_roster",
                        lambda agent, email, include_owned=False: (agent, email.lower()) in on)
    return on


@pytest.fixture(autouse=True)
def sink(monkeypatch, real_db):
    """The real sink over the real SQLite; its side effects stubbed."""
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
            pass

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


def _seed(real_db, agent, request_id, *, raised_by="gate", addressed_to=None,
          resolved_to=None, kind="alert"):
    """One stored row, written through the native create (the row shape every
    raise produces), with the three columns the reach rule reads set directly —
    so each test varies exactly one of them."""
    from utils.helpers import utc_now_iso
    item = {"id": request_id, "type": kind, "priority": "medium", "title": "T-003 is yours",
            "question": "Collect the evidence.", "options": None, "context": {},
            "created_at": utc_now_iso(), "expires_at": None,
            "addressed_to_email": addressed_to}
    out = real_db.create_native_operator_queue_item(
        agent, item, max_pending=None, channel="gate" if raised_by == "gate" else "mcp",
        raised_by=raised_by, to_role="primary", resolved_to=resolved_to, proposal=None,
        supersedes_expired=None)
    assert out["outcome"] == "created", out
    return out["row"]["id"]


def _svc():
    from client_portal.asks import service
    return service


def _page(email, **kw):
    return _svc().list_asks_page(email, is_platform=False, **kw)


def _ids(email, **kw):
    return [a.id for a in _page(email, **kw).items]


def _refused(door):
    with pytest.raises(_svc().AskError) as exc:
        door()
    return exc.value.status_code, exc.value.code


# ===========================================================================
# Reach by address — the three conditions, each on its own
# ===========================================================================

class TestReachByAddress:
    def test_an_off_roster_addressee_lists_and_counts_it(self, real_db, agent, people):
        rid = _seed(real_db, agent, REACH + "p1-t1-assigned-1", addressed_to=people["alice"])

        page = _page(people["alice"])
        assert [a.id for a in page.items] == [rid]
        assert page.total == 1

    def test_the_addressee_opens_it_off_roster(self, real_db, agent, people):
        rid = _seed(real_db, agent, REACH + "p1-t1-assigned-2", addressed_to=people["alice"])

        assert _svc()._owned_ask(rid, people["alice"], False)["id"] == rid
        assert _svc().get_ask_context(rid, people["alice"], False) is not None

    def test_a_holder_of_a_shared_row_reaches_it_through_resolved_to(self, real_db, agent, people):
        rid = _seed(real_db, agent, REACH + "p1-t1-assigned-3",
                    resolved_to=[people["alice"], people["bob"]])

        assert _ids(people["alice"]) == [rid]
        assert _ids(people["bob"]) == [rid]
        assert _page(people["bob"]).total == 1
        assert _svc()._owned_ask(rid, people["bob"], False)["id"] == rid

    def test_a_non_addressee_gets_the_uniform_404(self, real_db, agent, people):
        """Invariant #8: not addressed — the list omits it, the count is zero,
        and every per-ask door answers exactly as for an id naming nothing."""
        rid = _seed(real_db, agent, REACH + "p1-t1-assigned-4",
                    addressed_to=people["alice"], resolved_to=[people["alice"]])
        carol = people["carol"]

        assert _ids(carol) == []
        assert _page(carol).total == 0
        for door in (
            lambda: _svc()._owned_ask(rid, carol, False),
            lambda: _svc().get_ask_context(rid, carol, False),
            lambda: _svc()._owned_ask("no-such-ask", carol, False),
        ):
            assert _refused(door) == (404, "not_found")

    def test_the_wider_family_keeps_its_roster_fence(self, real_db, agent, people, roster):
        """A proposal to a project's creator (ent#980) is addressee-only in the
        Operating Room, but in the Workspace it is still the agent's ask: off
        the roster it is hidden, on the roster it is listed."""
        rid = _seed(real_db, agent, PROPOSAL + "ppr_1", addressed_to=people["alice"],
                    kind="approval")

        assert _ids(people["alice"]) == []
        assert _page(people["alice"]).total == 0
        assert _refused(lambda: _svc()._owned_ask(rid, people["alice"], False)) == (404, "not_found")

        roster.add((agent, people["alice"]))
        assert _ids(people["alice"]) == [rid]
        assert _svc()._owned_ask(rid, people["alice"], False)["id"] == rid

    def test_a_row_not_raised_by_the_gate_keeps_its_roster_fence(self, real_db, agent, people):
        """The prefix alone is not the key: a stored row under it whose
        `raised_by` column says anything but `gate` is read as the agent's."""
        rid = _seed(real_db, agent, REACH + "p1-t1-assigned-5", raised_by="agent",
                    addressed_to=people["alice"])

        assert _ids(people["alice"]) == []
        assert _page(people["alice"]).total == 0
        assert _refused(lambda: _svc()._owned_ask(rid, people["alice"], False)) == (404, "not_found")

    def test_a_reach_row_addressed_to_nobody_reaches_nobody(self, real_db, agent, people):
        _seed(real_db, agent, REACH + "p1-t1-assigned-6")

        for who in people.values():
            assert _ids(who) == []
            assert _page(who).total == 0

    def test_reach_rows_and_roster_rows_page_and_count_together(self, real_db, people, roster):
        """The reach is an OR beside the access set, not a replacement for it:
        a viewer on one agent's roster with a reach row from another sees both,
        and the total is both."""
        own, platform = f"agent-981-own-{uuid.uuid4().hex[:6]}", f"agent-981-sys-{uuid.uuid4().hex[:6]}"
        roster.add((own, people["alice"]))
        plain = _seed(real_db, own, f"r-{uuid.uuid4().hex[:8]}", raised_by="agent",
                      addressed_to=people["alice"], kind="question")
        reach = _seed(real_db, platform, REACH + "p1-t2-stale-1", addressed_to=people["alice"])

        page = _page(people["alice"])
        assert {a.id for a in page.items} == {plain, reach}
        assert page.total == 2

    def test_an_ended_reach_row_stays_in_the_ended_window(self, real_db, agent, people):
        rid = _seed(real_db, agent, REACH + "p1-t1-verified-1", addressed_to=people["alice"])
        from services import ask_service
        ask_service.clear_platform([rid], reason=ask_service.CONDITION_CLEARED)

        assert _ids(people["alice"]) == []
        assert _ids(people["alice"], include_ended=True) == [rid]

    def test_the_prefix_is_reserved_and_inside_the_addressee_only_family(self):
        from services import operator_queue_service as oqs
        row = {"request_id": "  GATE-ADDRESSEE-ONLY-REACH-p1-t1-assigned-x", "raised_by": "gate"}
        assert oqs.is_addressee_reach(row)
        assert oqs.is_addressee_only(row)      # the Operating Room's listing rule still applies
        assert oqs.is_about_a_person(row)      # under `gate-`: never agent-minted
        assert not oqs.is_addressee_reach({**row, "raised_by": "agent"})
        assert not oqs.is_addressee_reach({**row, "raised_by": None})
        assert not oqs.is_addressee_reach({"request_id": PROPOSAL + "x", "raised_by": "gate"})
        assert set(oqs.ADDRESSEE_REACH_ID_PREFIXES) == {REACH}
        assert all(p.startswith(oqs.ADDRESSEE_ONLY_ID_PREFIXES) for p in oqs.ADDRESSEE_REACH_ID_PREFIXES)


# ===========================================================================
# drop_addressee — one person off a row that stays for the others
# ===========================================================================

class TestDropAddressee:
    def _drop(self, rid, email):
        from services import ask_service
        return ask_service.drop_addressee(rid, email, reason=ask_service.CONDITION_CLEARED)

    def test_one_holder_is_dropped_and_the_row_stays_for_the_rest(self, real_db, agent, people):
        rid = _seed(real_db, agent, REACH + "p1-t1-assigned-7",
                    resolved_to=[people["alice"], people["bob"]])

        assert self._drop(rid, people["bob"]) is True

        row = real_db.get_operator_queue_item(rid)
        assert row["status"] == "pending"
        assert row["resolved_to"] == [people["alice"]]
        assert _ids(people["alice"]) == [rid]
        assert _ids(people["bob"]) == []
        assert _page(people["bob"]).total == 0
        assert _refused(lambda: _svc()._owned_ask(rid, people["bob"], False)) == (404, "not_found")

    def test_the_single_addressee_is_cleared_when_it_was_them(self, real_db, agent, people):
        rid = _seed(real_db, agent, REACH + "p1-t1-assigned-8", addressed_to=people["alice"],
                    resolved_to=[people["alice"], people["bob"]])

        assert self._drop(rid, people["alice"].upper()) is True

        row = real_db.get_operator_queue_item(rid)
        assert row["addressed_to_email"] is None
        assert row["resolved_to"] == [people["bob"]]
        assert _ids(people["alice"]) == []
        assert _ids(people["bob"]) == [rid]

    def test_someone_else_as_single_addressee_is_left_alone(self, real_db, agent, people):
        rid = _seed(real_db, agent, REACH + "p1-t1-assigned-9", addressed_to=people["alice"],
                    resolved_to=[people["alice"], people["bob"]])

        assert self._drop(rid, people["bob"]) is True

        row = real_db.get_operator_queue_item(rid)
        assert row["addressed_to_email"] == people["alice"]
        assert row["resolved_to"] == [people["alice"]]

    def test_a_row_that_never_named_them_is_unchanged(self, real_db, agent, people):
        rid = _seed(real_db, agent, REACH + "p1-t1-assigned-10", addressed_to=people["alice"],
                    resolved_to=[people["alice"]])

        assert self._drop(rid, people["carol"]) is False
        assert self._drop("no-such-ask", people["carol"]) is False

        row = real_db.get_operator_queue_item(rid)
        assert row["addressed_to_email"] == people["alice"]
        assert row["resolved_to"] == [people["alice"]]

    def test_the_reason_must_be_a_platform_ending_reason(self, real_db, agent, people):
        from services import ask_service
        rid = _seed(real_db, agent, REACH + "p1-t1-assigned-11", addressed_to=people["alice"])
        with pytest.raises(ValueError):
            ask_service.drop_addressee(rid, people["alice"], reason="because")
        assert _ids(people["alice"]) == [rid]
