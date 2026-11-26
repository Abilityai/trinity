"""List endpoints run a fixed number of queries, whatever they list (#4672).

The class #4671 found in `asks`, in the other hot lists: a per-agent or
per-row lookup inside the loop that builds the response, so the query count
grows with what is returned. Each is polled, and each is fixed the same way —
the predicate is unchanged and still decides; it is asked inside one
`request_memo.scope()` after one batch read per kind.

Every test seeds the same shape at two sizes and asserts the counts are EQUAL,
then pins that the answer is the one the unbatched code gives.

| Rank | Read | What used to repeat |
|---|---|---|
| 1 | Workspace roster (`canvas_managers`) | the caller's user row + one owner row, per agent |
| 2 | operator queue (`_for_principal`) | gate record, approver names, run access, per gate row |
| 4 | loops list | one runs query per loop |
| 5 | autonomy-status | 2–5 access queries per agent |
| 7 | Workspace Work feed | one timeout read per agent; one execution read per live turn |
| 8 | schedules list | the caller's tier on the agent, per schedule |
| 9 | evaluations | the owner check, per distinct agent |
"""
from __future__ import annotations

import asyncio
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
import client_portal.service  # noqa: E402,F401
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
    from services import agent_skills_listing
    monkeypatch.setattr(agent_skills_listing, "recall", lambda agent_name: None)


def _user(real_db, prefix: str, role: str = "user"):
    from db_models import UserCreate
    from models import User
    name = f"{prefix}-{_uid()}"
    email = f"{name}@example.com"
    real_db.create_user(UserCreate(username=name, role=role, email=email, name=prefix.title()))
    row = real_db.get_user_by_username(name)
    return User(id=row["id"], username=name, email=email, role=role)


class _Fleet:
    """An owner, a viewer the owner shares with, an outsider, and agents."""

    def __init__(self, real_db, viewer_role: str = "operator"):
        self.db = real_db
        self.owner = _user(real_db, "owner", role="operator")
        self.viewer = _user(real_db, "viewer", role=viewer_role)
        self.shared: list = []
        self.private: list = []

    def add(self, shared: int = 0, private: int = 0) -> None:
        for _ in range(shared):
            agent = f"agent-4672-{_uid()}"
            assert self.db.register_agent_owner(agent, self.owner.username)
            assert self.db.share_agent(agent, self.owner.username, self.viewer.email)
            self.shared.append(agent)
        for _ in range(private):
            agent = f"agent-4672-{_uid()}"
            assert self.db.register_agent_owner(agent, self.owner.username)
            self.private.append(agent)

    @property
    def agents(self) -> list:
        return self.shared + self.private


def _flat(measure, grow):
    """Run `measure` at two sizes; return both logs after asserting equal counts."""
    with count_queries() as small:
        measure()
    grow()
    with count_queries() as large:
        measure()
    assert large.count == small.count, f"small: {small.report()}; large: {large.report()}"
    return small, large


# ===========================================================================
# Rank 1 — the Workspace roster's canvas-management flag
# ===========================================================================

class TestRosterCanvasManagers:
    def _managers(self, fleet, who):
        from client_portal.service import canvas_managers
        return canvas_managers(fleet.agents, who.email, is_platform=True)

    def test_flat_for_an_owner(self, real_db):
        fleet = _Fleet(real_db)
        fleet.add(shared=2, private=1)
        _, large = _flat(lambda: self._managers(fleet, fleet.owner),
                         lambda: fleet.add(shared=6, private=5))
        assert large.count <= 4, large.report()

    def test_flat_for_someone_who_owns_nothing(self, real_db):
        fleet = _Fleet(real_db)
        fleet.add(shared=2)
        _flat(lambda: self._managers(fleet, fleet.viewer), lambda: fleet.add(shared=9))

    def test_the_answer_is_may_manage_canvases_per_agent(self, real_db):
        from client_portal.service import may_manage_canvases
        fleet = _Fleet(real_db)
        fleet.add(shared=3, private=2)
        admin = _user(real_db, "admin", role="admin")
        for who in (fleet.owner, fleet.viewer, admin):
            expected = {a for a in fleet.agents if may_manage_canvases(a, who.email, is_platform=True)}
            assert self._managers(fleet, who) == expected, who.username
        assert self._managers(fleet, fleet.owner) == set(fleet.agents)
        assert self._managers(fleet, fleet.viewer) == set(), "a share is not ownership"
        assert self._managers(fleet, admin) == set(fleet.agents)

    def test_an_external_client_never_manages_and_costs_nothing(self, real_db):
        from client_portal.service import canvas_managers
        fleet = _Fleet(real_db)
        fleet.add(shared=3)
        with count_queries() as log:
            assert canvas_managers(fleet.agents, fleet.owner.email, is_platform=False) == set()
            assert canvas_managers(fleet.agents, None, is_platform=True) == set()
        assert log.count == 0

    def test_a_failed_prefetch_falls_back_to_the_per_agent_answer(self, real_db, monkeypatch):
        from database import db as core_db
        fleet = _Fleet(real_db)
        fleet.add(private=3)

        def _down(names):
            raise RuntimeError("batch read down")

        monkeypatch.setattr(core_db, "get_agent_owners", _down)
        assert self._managers(fleet, fleet.owner) == set(fleet.agents)


# ===========================================================================
# Rank 2 — the operator queue's gate approval cards
# ===========================================================================

def _gate_row(real_db, agent, source_agent, approvers):
    from utils.helpers import utc_now_iso
    rid = f"gate-{_uid()}"
    out = real_db.create_native_operator_queue_item(
        agent,
        {"id": rid, "type": "approval", "priority": "medium", "title": "Run deploy?",
         "question": "Approve?", "options": ["approve", "deny"], "context": {},
         "created_at": utc_now_iso(), "expires_at": None, "addressed_to_email": approvers[0]},
        max_pending=None, channel="gate", raised_by="gate", to_role="primary",
        resolved_to=approvers, proposal=None, supersedes_expired=None)
    assert out["outcome"] == "created", out
    real_db.create_gate_request(
        request_id=rid, agent_name=agent, skills=["deploy"], request_text="ship it",
        requester_kind="agent", requester_key=f"agent:{source_agent}", source_agent=source_agent,
        requester_execution_id=f"exec-{_uid()}", fingerprints={}, dispatch={})
    return out["row"]["id"]


class TestOperatorQueueGateCards:
    def _seed(self, real_db, fleet, per_agent):
        approvers = [fleet.viewer.email, fleet.owner.email]
        for agent in fleet.shared:
            for _ in range(per_agent):
                _gate_row(real_db, agent, source_agent=fleet.private[0], approvers=approvers)

    def test_flat_in_rows_and_agents(self, real_db):
        fleet = _Fleet(real_db)
        fleet.add(shared=2, private=1)
        self._seed(real_db, fleet, per_agent=1)

        def grow():
            before = list(fleet.shared)
            fleet.add(shared=5)
            approvers = [fleet.viewer.email, fleet.owner.email]
            for agent in fleet.shared:
                for _ in range(1 if agent in before else 4):
                    _gate_row(real_db, agent, fleet.private[0], approvers)

        # The list read itself is outside the projection under test.
        small_items = self._items(real_db, fleet)
        with count_queries() as small:
            self._project(small_items, fleet.viewer)
        grow()
        large_items = self._items(real_db, fleet)
        assert len(large_items) > len(small_items) * 5
        with count_queries() as large:
            self._project(large_items, fleet.viewer)
        assert large.count == small.count, f"small: {small.report()}; large: {large.report()}"
        assert large.count <= 8, large.report()

    @staticmethod
    def _items(real_db, fleet):
        return [i for i in real_db.list_operator_queue_items(limit=500, types=("approval",))
                if i["agent_name"] in fleet.agents]

    @staticmethod
    def _project(items, who):
        from routers.operator_queue import _for_principal
        return _for_principal(items, who)

    def test_the_cards_are_the_unbatched_cards(self, real_db):
        from services import skill_gate_service
        fleet = _Fleet(real_db)
        fleet.add(shared=3, private=1)
        self._seed(real_db, fleet, per_agent=2)
        items = self._items(real_db, fleet)
        assert len(items) == 6

        for who in (fleet.viewer, fleet.owner):
            rows = self._project(items, who)
            for item, row in zip(items, rows):
                alone = skill_gate_service.card_view(
                    item, viewer_email=who.email, run_viewer=who.username, name_approvers=True)
                assert row["gate"] == alone, (who.username, item["id"])
            assert all(r["gate"]["can_decide"] for r in rows)
            assert all(sorted(r["gate"]["waiting_on"]) == ["Owner", "Viewer"] for r in rows)

        # The run links are per agent, per viewer: the requester's run is on an
        # agent only the owner can open.
        viewer_rows = self._project(items, fleet.viewer)
        owner_rows = self._project(items, fleet.owner)
        assert all(r["gate"]["requester"]["execution_id"] is None for r in viewer_rows)
        assert all(r["gate"]["requester"]["execution_id"] for r in owner_rows)

    def test_a_machine_principal_gets_no_cards_and_no_prefetch(self, real_db):
        from models import User
        fleet = _Fleet(real_db)
        fleet.add(shared=1, private=1)
        self._seed(real_db, fleet, per_agent=2)
        items = self._items(real_db, fleet)
        machine = User(id=fleet.owner.id, username=fleet.owner.username, email=fleet.owner.email,
                       role="operator", agent_name=fleet.shared[0], mcp_scope="agent")
        with count_queries() as log:
            rows = self._project(items, machine)
        assert all("gate" not in r for r in rows)
        assert log.count == 0, log.report()


# ===========================================================================
# Rank 4 — the loops list
# ===========================================================================

class TestLoopsList:
    def _loop(self, real_db, agent, runs):
        from db_harness import run
        loop = real_db.create_loop(agent, "do the thing", 10)
        # Written straight in, newest first: a real run is claimed one at a
        # time, and the read must put them back in order.
        for n in range(runs, 0, -1):
            run("INSERT INTO agent_loop_runs (id, loop_id, run_number, execution_id, status, cost,"
                " started_at) VALUES (:id, :loop, :n, :exec, 'success', 0.5, '2026-11-20T10:00:00Z')",
                id=f"run-{_uid()}", loop=loop["id"], n=n, exec=f"exec-{_uid()}")
        return loop["id"]

    def _list(self, agent):
        """The route's own body — called past its auth dependency."""
        from routers import loops
        return loops.list_loops(name=agent, status=None, limit=200)

    def test_flat_in_loops(self, real_db):
        fleet = _Fleet(real_db)
        fleet.add(private=1)
        agent = fleet.private[0]
        self._loop(real_db, agent, runs=2)
        _, large = _flat(lambda: self._list(agent),
                         lambda: [self._loop(real_db, agent, runs=3) for _ in range(12)])
        assert large.count == 2, large.report()      # the loops, then every loop's runs

    def test_each_loop_gets_its_own_runs_in_order(self, real_db):
        from routers.loops import _build_status_response
        fleet = _Fleet(real_db)
        fleet.add(private=1)
        agent = fleet.private[0]
        ids = [self._loop(real_db, agent, runs=n) for n in (0, 1, 3)]
        listed = {r.loop_id: r for r in self._list(agent)}
        assert [len(listed[i].runs) for i in ids] == [0, 1, 3]
        for loop_id in ids:
            alone = _build_status_response(real_db.get_loop(loop_id))     # the single-loop path
            assert listed[loop_id] == alone
            assert [r.run_number for r in alone.runs] == list(range(1, len(alone.runs) + 1))
            assert alone.total_cost == 0.5 * len(alone.runs)


# ===========================================================================
# Rank 5 — autonomy-status
# ===========================================================================

class TestAutonomyStatus:
    def _status(self, who):
        from services.agent_service.autonomy import get_all_autonomy_status_logic
        return asyncio.run(get_all_autonomy_status_logic(who))

    def test_flat_for_a_non_admin(self, real_db):
        fleet = _Fleet(real_db)
        fleet.add(shared=2, private=2)
        _, large = _flat(lambda: self._status(fleet.viewer),
                         lambda: fleet.add(shared=7, private=7))
        assert large.count <= 5, large.report()

    def test_flat_for_an_owner_and_an_admin(self, real_db):
        fleet = _Fleet(real_db)
        fleet.add(shared=2, private=2)
        admin = _user(real_db, "admin", role="admin")
        for who in (fleet.owner, admin):
            _flat(lambda: self._status(who), lambda: fleet.add(shared=3, private=3))

    def test_the_answer_is_the_per_agent_access_rule(self, real_db):
        fleet = _Fleet(real_db)
        fleet.add(shared=3, private=3)
        real_db.set_autonomy_enabled(fleet.shared[0], True)

        seen = self._status(fleet.viewer)
        mine = {a: v for a, v in seen.items() if a in fleet.agents}
        assert set(mine) == set(fleet.shared), "shared agents only — never the owner's private ones"
        assert mine[fleet.shared[0]] == {"autonomy_enabled": True}
        assert mine[fleet.shared[1]] == {"autonomy_enabled": False}

        owner_view = self._status(fleet.owner)
        assert set(fleet.agents) <= set(owner_view)

    def test_a_revoked_share_is_gone_on_the_next_read(self, real_db):
        fleet = _Fleet(real_db)
        fleet.add(shared=2)
        gone = fleet.shared[0]
        assert gone in self._status(fleet.viewer)
        assert real_db.unshare_agent(gone, fleet.owner.username, fleet.viewer.email)
        assert gone not in self._status(fleet.viewer)


# ===========================================================================
# Rank 7 — the Workspace Work feed
# ===========================================================================

def _execution(agent, *, status="running", email=None):
    from db_harness import run, seed_execution
    from utils.helpers import utc_now_iso
    eid = seed_execution(f"sched-{_uid()}", agent, exec_id=f"exec-{_uid()}", status=status,
                         started_at=utc_now_iso(), triggered_by="chat")
    if email:
        run("UPDATE schedule_executions SET source_user_email=:e WHERE id=:id", e=email, id=eid)
    return eid


class TestWorkFeed:
    @pytest.fixture(autouse=True)
    def no_agents(self, monkeypatch):
        """No agent is reachable here: the pipeline files and the heartbeat's
        activity lines are the agents' own, and neither is a database read."""
        from client_portal.work import pipeline_state, service
        from client_portal.work.models import WorkSteps

        async def _steps(agent, started_at, roster):
            return WorkSteps(state="none")

        monkeypatch.setattr(pipeline_state, "read_pipeline_steps", _steps)
        self.activity = {}
        monkeypatch.setattr(service, "_activity_by_agent",
                            lambda names: {n: dict(self.activity.get(n, {})) for n in names})

    def _work(self, who, agents):
        from client_portal.work import service
        return asyncio.run(service.get_work(who.email, list(agents)))

    def _activity(self, who, agents):
        from client_portal.work import service
        return asyncio.run(service.get_work_activity(who.email, list(agents)))

    def test_the_work_read_is_flat_in_agents_and_rows(self, real_db):
        fleet = _Fleet(real_db)
        fleet.add(private=2)
        for agent in fleet.private:
            _execution(agent)
            _execution(agent, status="success")

        def grow():
            fleet.add(private=6)
            for agent in fleet.private:
                for _ in range(3):
                    _execution(agent)
                    _execution(agent, status="success")

        with count_queries() as small:
            first = self._work(fleet.owner, fleet.private)
        assert len(first.now) == 2
        grow()
        with count_queries() as large:
            second = self._work(fleet.owner, fleet.private)
        assert len(second.now) == 2 + 8 * 3
        assert large.count == small.count, f"small: {small.report()}; large: {large.report()}"

    def test_the_timeouts_are_each_agents_own(self, real_db):
        """The batch read answers per agent: a stale bound is that agent's
        timeout, not a neighbour's."""
        from db_harness import run
        fleet = _Fleet(real_db)
        fleet.add(private=2)
        quick, slow = fleet.private
        real_db.set_execution_timeout(quick, 60)
        real_db.set_execution_timeout(slow, 7200)
        for agent in (quick, slow):
            eid = _execution(agent)
            run("UPDATE schedule_executions SET started_at='2026-11-20T00:00:00.000000Z' WHERE id=:id",
                id=eid)
        assert real_db.get_execution_timeouts([quick, slow, "agent-missing"]) == {
            quick: 60, slow: 7200, "agent-missing": 3600}
        for agent in (quick, slow, "agent-missing"):
            assert real_db.get_execution_timeout(agent) == \
                real_db.get_execution_timeouts([agent])[agent]

    def test_the_activity_read_is_flat_in_executions(self, real_db):
        fleet = _Fleet(real_db, viewer_role="user")
        fleet.add(shared=2)

        def beat(n):
            for agent in fleet.shared:
                for _ in range(n):
                    mine = _execution(agent, email=fleet.viewer.email)
                    theirs = _execution(agent, email="someone-else@example.com")
                    self.activity.setdefault(agent, {}).update({
                        mine: {"tool": "Read", "at": "2026-11-20T10:00:00Z"},
                        theirs: {"tool": "Bash", "at": "2026-11-20T10:00:00Z"}})

        beat(1)
        with count_queries() as small:
            self._activity(fleet.viewer, fleet.shared)
        beat(6)
        with count_queries() as large:
            self._activity(fleet.viewer, fleet.shared)
        assert large.count == small.count, f"small: {small.report()}; large: {large.report()}"

    def test_a_share_holder_still_sees_only_their_own_turns(self, real_db):
        """ent#875, unchanged: on a shared agent, another person's running turn
        is never returned — the batch read decides nothing."""
        from client_portal.work import service
        fleet = _Fleet(real_db, viewer_role="user")
        fleet.add(shared=1)
        agent = fleet.shared[0]
        mine = _execution(agent, email=fleet.viewer.email)
        theirs = _execution(agent, email="someone-else@example.com")
        elsewhere = _execution(fleet.shared[0] + "-other", email=fleet.viewer.email)
        own = service._own_rows(fleet.viewer.email)
        verdicts = {e: service._execution_is_viewers(e, agent, own) for e in (mine, theirs, elsewhere)}
        assert verdicts == {mine: True, theirs: False, elsewhere: False}

        from utils import request_memo
        with request_memo.scope():
            real_db.get_executions_by_ids([mine, theirs, elsewhere, "exec-missing"])
            with count_queries() as log:
                warm = {e: service._execution_is_viewers(e, agent, own)
                        for e in (mine, theirs, elsewhere, "exec-missing")}
        assert log.count == 0, log.report()
        assert warm == {**verdicts, "exec-missing": False}

    def test_executions_by_ids_matches_the_single_read(self, real_db):
        fleet = _Fleet(real_db)
        fleet.add(private=1)
        ids = [_execution(fleet.private[0]) for _ in range(3)] + ["exec-missing"]
        with count_queries() as log:
            batch = real_db.get_executions_by_ids(ids + [None, ""])
        assert log.count == 1
        assert batch == {i: real_db.get_execution(i) for i in ids}
        assert batch["exec-missing"] is None


# ===========================================================================
# Ranks 8 and 9 — schedules list, evaluations
# ===========================================================================

class TestSchedulesList:
    def _list(self, agent, who):
        from routers import schedules
        return asyncio.run(schedules.list_agent_schedules(name=agent, current_user=who))

    def _seed(self, agent, n):
        from db_harness import seed_schedule
        for _ in range(n):
            seed_schedule(f"sched-{_uid()}", agent)

    @pytest.mark.parametrize("role", ["viewer", "owner"])
    def test_flat_in_schedules(self, real_db, role):
        fleet = _Fleet(real_db)
        fleet.add(shared=1)
        agent, who = fleet.shared[0], getattr(fleet, role)
        self._seed(agent, 2)
        _flat(lambda: self._list(agent, who), lambda: self._seed(agent, 10))

    def test_the_flag_is_the_per_row_answer(self, real_db):
        from routers import schedules
        fleet = _Fleet(real_db)
        fleet.add(shared=1)
        agent = fleet.shared[0]
        self._seed(agent, 3)
        for who in (fleet.owner, fleet.viewer):
            listed = self._list(agent, who)
            assert len(listed) == 3
            for row, schedule in zip(listed, real_db.list_agent_schedules(agent)):
                assert row == schedules._schedule_response(schedule, agent, who)
        assert all(r.can_manage_webhook for r in self._list(agent, fleet.owner))
        assert not any(r.can_manage_webhook for r in self._list(agent, fleet.viewer)), \
            "a share holder who created none of them manages none of them"


class TestEvaluationsOperatorAgents:
    def _operators(self, fleet, who):
        from routers import evaluations
        return evaluations._operator_for([{"agent_name": a} for a in fleet.agents] * 3, who)

    def test_flat_in_agents(self, real_db):
        fleet = _Fleet(real_db)
        fleet.add(shared=2, private=1)
        for who in (fleet.owner, fleet.viewer):
            _flat(lambda: self._operators(fleet, who), lambda: fleet.add(shared=4, private=4))

    def test_owner_or_admin_only_and_fail_closed(self, real_db, monkeypatch):
        fleet = _Fleet(real_db)
        fleet.add(shared=2, private=2)
        assert self._operators(fleet, fleet.owner) == set(fleet.agents)
        assert self._operators(fleet, fleet.viewer) == set(), "a share holder is not an operator"

        def _down(username, agent):
            raise RuntimeError("down")

        monkeypatch.setattr(real_db, "can_user_share_agent", _down)
        assert self._operators(fleet, fleet.owner) == set()
