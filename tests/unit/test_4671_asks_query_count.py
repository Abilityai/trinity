"""`list_asks_page` runs a fixed number of queries, whatever it lists (#4671).

On a hosted instance one `GET /api/enterprise/client-portal/asks` took 0.45 s
for 63 rows and made about 130 extra lookups on `agent_ownership`,
`agent_sharing` and `users`. Read out of the code, per call:

- the roster re-check ran the two roster queries once per AGENT the viewer's
  asks spanned (`_on_roster` → `agent_on_roster` → `roster_agent_names`);
- `_platform_username` read the viewer's own `users` row once per ROW;
- a gate approval read its record, then the viewer's user row and their access
  to the gated agent and to the requesting agent, per ROW (`card_view`);
- a replaced / replacing ask read the row it links to, per ROW;
- a shared ask someone else answered read that person's user row, per ROW.

Each test seeds the same shape at two sizes and asserts the count is equal —
not "small", equal: a count that grows by one per agent is the defect.

Visibility is pinned beside it, because a batched read that drops the
per-agent fail-closed check or turns an unreadable roster into an empty list
would pass every count here.

Harness: the unit island's per-process SQLite, real roster, real access rules.
"""
from __future__ import annotations

import os
import sys
import uuid

import pytest

pytest.importorskip("sqlalchemy")

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")

_TESTS = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_BACKEND = os.path.abspath(os.path.join(_TESTS, "..", "src", "backend"))
for _p in (_BACKEND, _TESTS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

pytestmark = pytest.mark.unit

import services.ask_service  # noqa: E402,F401
import services.operator_queue_service  # noqa: E402,F401
import client_portal.asks.service  # noqa: E402,F401
import client_portal.db  # noqa: E402,F401

from query_counter import count_queries  # noqa: E402


def _uid() -> str:
    return uuid.uuid4().hex[:8]


@pytest.fixture
def real_db():
    from database import db as real
    return real


@pytest.fixture(autouse=True)
def no_redis(monkeypatch):
    """The gate card's skill descriptions come from Redis; there is none here."""
    from services import agent_skills_listing
    monkeypatch.setattr(agent_skills_listing, "recall", lambda agent_name: None)


def _user(real_db, prefix: str, role: str = "user") -> dict:
    from db_models import UserCreate
    name = f"{prefix}-{_uid()}"
    real_db.create_user(UserCreate(username=name, role=role, email=f"{name}@example.com",
                                   name=prefix.title()))
    return {"username": name, "email": f"{name}@example.com"}


def _row(real_db, agent, request_id, *, addressed_to=None, resolved_to=None,
         kind="question", raised_by="agent", replaces=None):
    from utils.helpers import utc_now_iso
    item = {"id": request_id, "type": kind, "priority": "medium", "title": "Pick one",
            "question": "Which?", "options": ["a", "b"], "context": {},
            "created_at": utc_now_iso(), "expires_at": None,
            "addressed_to_email": addressed_to}
    out = real_db.create_native_operator_queue_item(
        agent, item, max_pending=None, channel="gate" if raised_by == "gate" else "mcp",
        raised_by=raised_by, to_role="primary", resolved_to=resolved_to, proposal=None,
        supersedes_expired=None, replaces=replaces)
    assert out["outcome"] == "created", out
    return out["row"]["id"]


class _World:
    """One viewer, one owner, and agents the owner shared with the viewer."""

    def __init__(self, real_db, viewer_role: str = "operator"):
        self.db = real_db
        self.owner = _user(real_db, "owner")
        self.viewer = _user(real_db, "viewer", role=viewer_role)
        self.other = _user(real_db, "colleague")
        self.agents: list = []

    def add_agent(self, shared: bool = True) -> str:
        agent = f"agent-4671-{_uid()}"
        assert self.db.register_agent_owner(agent, self.owner["username"])
        if shared:
            assert self.db.share_agent(agent, self.owner["username"], self.viewer["email"])
        self.agents.append(agent)
        return agent

    def add_rows(self, agent: str, source_agent: str) -> None:
        """One of every row shape that used to cost a lookup of its own."""
        from db_harness import run
        from utils.helpers import utc_now_iso
        me, tag = self.viewer["email"], _uid()
        # A plain question: the per-row `_platform_username` read.
        _row(self.db, agent, f"q-{tag}", addressed_to=me)
        # A replaced ask and its replacement: `_linked_request_id`, both ways.
        old = _row(self.db, agent, f"old-{tag}", addressed_to=me)
        _row(self.db, agent, f"new-{tag}", addressed_to=me, replaces=old)
        # A gate approval with a requester run on ANOTHER agent: `card_view`.
        gate_id = f"gate-{tag}"
        _row(self.db, agent, gate_id, addressed_to=me, resolved_to=[me],
             kind="approval", raised_by="gate")
        self.db.create_gate_request(
            request_id=gate_id, agent_name=agent, skills=["deploy"], request_text="ship it",
            requester_kind="agent", requester_key=f"agent:{source_agent}",
            source_agent=source_agent,
            requester_execution_id=f"exec-{tag}", fingerprints={}, dispatch={})
        # A shared ask a co-addressee answered: `_answered_by`.
        shared = _row(self.db, agent, f"shared-{tag}", resolved_to=[me, self.other["email"]])
        run("UPDATE operator_queue SET status='responded', disposition='answered',"
            " response='a', responded_by_email=:by, disposed_by_email=:by,"
            " responded_at=:at, disposed_at=:at WHERE id=:id",
            by=self.other["email"], at=utc_now_iso(), id=shared)

    def grow(self, agents: int, rows_per_agent: int) -> None:
        for _ in range(agents):
            agent = self.add_agent()
            for _ in range(rows_per_agent):
                self.add_rows(agent, source_agent=self.agents[0])

    def page(self, **kw):
        from client_portal.asks import service
        return service.list_asks_page(self.viewer["email"], True, include_ended=True, **kw)


class TestQueryCountIsFlat:
    def test_the_count_does_not_grow_with_agents_or_rows(self, real_db):
        world = _World(real_db)
        world.grow(agents=2, rows_per_agent=1)
        with count_queries() as small:
            first = world.page()
        assert first.total == 2 * 5

        world.grow(agents=5, rows_per_agent=4)
        with count_queries() as large:
            second = world.page()
        assert second.total == 2 * 5 + 5 * 4 * 5

        assert large.count == small.count, f"small: {small.report()}; large: {large.report()}"

    def test_the_count_is_small(self, real_db):
        """Flat is not enough if flat means "200 every time". The ceiling is
        loose on purpose — it catches a return of the per-row class, not a
        change of one query."""
        world = _World(real_db)
        world.grow(agents=6, rows_per_agent=4)
        with count_queries() as log:
            world.page()
        assert log.count <= 15, log.report()

    def test_an_external_client_is_flat_too(self, real_db):
        """`is_platform=False`: no owned roster, no run links — the other arm."""
        from client_portal.asks import service
        world = _World(real_db)
        world.grow(agents=2, rows_per_agent=1)
        with count_queries() as small:
            service.list_asks_page(world.viewer["email"], False, include_ended=True)
        world.grow(agents=5, rows_per_agent=4)
        with count_queries() as large:
            page = service.list_asks_page(world.viewer["email"], False, include_ended=True)
        assert page.total == 7 * 5 + 5 * 3 * 5
        assert large.count == small.count, f"small: {small.report()}; large: {large.report()}"


class TestTheAnswerIsUnchanged:
    def test_the_projection_matches_the_unbatched_one(self, real_db):
        """Every row, projected inside the list and projected alone, is the
        same object — the batch is a faster way to the same answer."""
        from client_portal.asks import service
        world = _World(real_db)
        world.grow(agents=3, rows_per_agent=2)

        listed = {a.id: a for a in world.page().items}
        assert len(listed) == 3 * 2 * 5
        for ask_id, ask in listed.items():
            alone = service._project(real_db.get_operator_queue_item(ask_id),
                                     viewer_email=world.viewer["email"], is_platform=True)
            assert ask == alone, ask_id

        kinds = {(a.kind, a.status, a.ended_by) for a in listed.values()}
        assert ("question", "answered", "someone_else") in kinds
        gates = [a for a in listed.values() if a.gate]
        assert gates and all(a.gate["can_decide"] for a in gates)
        # The viewer has no access to nothing here: they are shared on every
        # agent, so the requester's run link crosses.
        assert all(a.gate["requester"]["execution_id"] for a in gates)
        assert [a for a in listed.values() if a.replaces]
        assert [a for a in listed.values() if a.replaced_by]
        answered = [a for a in listed.values() if a.ended_by == "someone_else"]
        assert answered and all(a.answered_by == "Colleague" for a in answered)

    def test_a_revoked_share_hides_only_that_agents_asks(self, real_db):
        """The roster re-check still decides per agent, at read time."""
        world = _World(real_db)
        world.grow(agents=3, rows_per_agent=1)
        gone = world.agents[1]
        assert {a.agent_name for a in world.page().items} == set(world.agents)

        assert real_db.unshare_agent(gone, world.owner["username"], world.viewer["email"])

        page = world.page()
        assert {a.agent_name for a in page.items} == set(world.agents) - {gone}
        assert page.total == 2 * 5

    def test_a_run_link_needs_access_to_that_runs_agent(self, real_db):
        """`_openable_run` is per agent: the requester's run is on an agent the
        viewer was never shared, so its link stays hidden while the gated
        agent's own would show."""
        world = _World(real_db)
        private = world.add_agent(shared=False)
        agent = world.add_agent()
        world.add_rows(agent, source_agent=private)

        gates = [a for a in world.page().items if a.gate]
        assert len(gates) == 1
        assert gates[0].gate["requester"]["execution_id"] is None

    def test_an_unreadable_roster_is_unavailable_not_empty(self, real_db, monkeypatch):
        from client_portal import db as portal_db
        from client_portal.asks import service
        world = _World(real_db)
        world.grow(agents=2, rows_per_agent=1)

        def _down(email):
            raise RuntimeError("roster down")

        monkeypatch.setattr(portal_db, "get_shared_roster", _down)
        with pytest.raises(service.AsksUnavailable):
            world.page()

    def test_an_unreadable_queue_is_unavailable_not_empty(self, real_db, monkeypatch):
        from client_portal.asks import service
        world = _World(real_db)
        world.grow(agents=1, rows_per_agent=1)

        def _down(**kw):
            raise RuntimeError("queue down")

        monkeypatch.setattr(service.db, "list_operator_queue_items", _down)
        with pytest.raises(service.AsksUnavailable):
            world.page()
