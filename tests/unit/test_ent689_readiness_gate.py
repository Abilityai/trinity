"""trinity-enterprise#689 — the readiness gate on a companion's proactive brief.

Requirement: docs/memory/requirements/core-agent.md §5.36 (enforcement).

Pinned here, and why:

* **The unstamped companion — the case that exists on every install today** —
  is held, and an unstamped NON-companion is not (the gate must not mute every
  scheduled delivery). Since trinity-enterprise#813 a companion is an agent whose
  primary holds a seat (the seat on record); the template is not read at all.
* **Every ambiguity fires** (#1638): stamp unreadable, no seat lookup, a lookup
  error.
* **The rollout seed** stamps exactly the agents whose seat brief fires today —
  never over an existing stamp — on BOTH migration tracks, idempotently.
* **The card** calls a rollout stamp "carried over", never a person's act, and
  says when a brief is held.
"""
from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit

AGENT = "sales-companion"


def _gate():
    from services import role_readiness_gate
    return role_readiness_gate


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------

class TestDecide:
    def test_stamp_ready_fires(self):
        v = _gate().decide(AGENT, {"status": "ready"}, None)
        assert v.fire and v.basis == "stamp"

    def test_stamp_calibrating_holds_whatever_the_template_says(self):
        v = _gate().decide(AGENT, {"status": "calibrating"}, True)
        assert not v.fire and "marks it ready" in v.reason and AGENT in v.reason

    def test_unstamped_companion_is_held(self):
        """DoD 4: the unstamped agent — every install's case — under the ruled direction."""
        v = _gate().decide(AGENT, None, True)
        assert not v.fire and v.basis == "unstamped_companion"

    def test_unstamped_non_companion_fires(self):
        v = _gate().decide(AGENT, None, False)
        assert v.fire and v.basis == "not_companion"

    def test_unknown_companionship_fails_open(self):
        v = _gate().decide(AGENT, None, None)
        assert v.fire and v.basis == "fail_open"


class TestBriefReadiness:
    """trinity-enterprise#813: who is a companion comes from the seat on record
    (`assignment_provider.resolve_seat`), not from the template — a companion is
    an agent whose primary holds a seat. The template is never read."""

    @pytest.fixture()
    def wired(self, monkeypatch):
        import database
        from services import agent_client
        from services import assignment_provider as ap
        gate = _gate()
        state = {"stamp": None, "raise": None, "asked": 0,
                 "seat": {"case": "serves", "role_id": "sales-lead", "seats": ["sales-lead"]}}

        def stamp(agent):
            if state["raise"] == "stamp":
                raise RuntimeError("db down")
            return state["stamp"]
        monkeypatch.setattr(database.db, "get_agent_role_readiness", stamp)
        monkeypatch.setattr(database.db, "get_agent_owner", lambda name: {"owner_id": 1})

        def no_template_read(name):
            raise AssertionError("the gate must not read the agent's files (#813)")
        monkeypatch.setattr(agent_client, "get_agent_client", no_template_read)

        class Seats:
            def assignment_for(self, agent_name, triggered_by):
                return None

            def seat_for(self, agent_name):
                state["asked"] += 1
                if state["raise"] == "seat":
                    raise RuntimeError("provider down")
                return state["seat"]
        ap.register_provider(Seats())
        yield gate, state
        ap.clear_provider()

    @pytest.mark.asyncio
    async def test_an_unstamped_companion_is_held(self, wired):
        gate, state = wired
        v = await gate.brief_readiness(AGENT)
        assert not v.fire and v.basis == "unstamped_companion"

    @pytest.mark.asyncio
    async def test_a_stamped_calibrating_companion_with_a_primary_is_still_held(self, wired):
        """The ent#689 behaviour this change must keep (#813 AC)."""
        gate, state = wired
        state["stamp"] = {"status": "calibrating"}
        v = await gate.brief_readiness(AGENT)
        assert not v.fire and v.basis == "stamp" and "marks it ready" in v.reason

    @pytest.mark.asyncio
    async def test_the_stamp_decides_without_asking_for_the_seat(self, wired):
        gate, state = wired
        state["stamp"] = {"status": "ready"}
        v = await gate.brief_readiness(AGENT)
        assert v.fire and v.basis == "stamp"
        assert state["asked"] == 0, "a stamped agent never asks for its seat"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("seat,why", [
        ({"case": "holds", "role_id": "orchestrator", "seats": []}, "holds the seat orchestrator itself"),
        ({"case": "none", "role_id": None, "seats": []}, "no primary with a seat"),
    ], ids=["holds-a-seat-itself", "no-primary-with-a-seat"])
    async def test_an_agent_that_is_not_a_companion_fires_and_says_why(self, wired, seat, why):
        gate, state = wired
        state["seat"] = seat
        v = await gate.brief_readiness(AGENT)
        assert v.fire and v.basis == "not_companion" and why in v.reason

    @pytest.mark.asyncio
    @pytest.mark.parametrize("case", ["no_provider", "seat_error", "no_answer", "malformed"])
    async def test_an_unknown_seat_reads_as_not_a_companion_and_fires(self, wired, case):
        """#813 AC: no provider, no primary, or a resolver error → not a companion."""
        from services import assignment_provider as ap
        gate, state = wired
        if case == "no_provider":
            ap.clear_provider()
        elif case == "seat_error":
            state["raise"] = "seat"
        elif case == "no_answer":
            state["seat"] = None
        else:
            state["seat"] = {"case": "serves"}
        v = await gate.brief_readiness(AGENT)
        assert v.fire and v.basis == "not_companion" and "seat lookup unavailable" in v.reason

    @pytest.mark.asyncio
    async def test_an_unreadable_stamp_still_fails_open(self, wired):
        gate, state = wired
        state["raise"] = "stamp"
        v = await gate.brief_readiness(AGENT)
        assert v.fire and v.basis == "fail_open"

    @pytest.mark.asyncio
    async def test_the_decision_is_logged_with_its_reason(self, wired, caplog):
        import logging
        gate, state = wired
        with caplog.at_level(logging.INFO, logger="services.role_readiness_gate"):
            await gate.brief_readiness(AGENT)
        assert any("[ent#813]" in r.message and "its primary holds the seat sales-lead" in r.message
                   and "held" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------

class TestEndpoint:
    @pytest.mark.asyncio
    async def test_unknown_agent_is_404(self, monkeypatch):
        """Through the real service: the existence check lives there (Invariant #1)."""
        import database
        from fastapi import HTTPException
        from routers import internal
        monkeypatch.setattr(database.db, "get_agent_owner", lambda name: None)
        with pytest.raises(HTTPException) as e:
            await internal.internal_agent_brief_readiness("ghost")
        assert e.value.status_code == 404

    @pytest.mark.asyncio
    async def test_shape(self, monkeypatch):
        from routers import internal
        gate = _gate()

        async def verdict(agent):
            return gate.Verdict(False, gate.held_reason(agent), "unstamped_companion")
        monkeypatch.setattr(gate, "brief_readiness", verdict)
        out = await internal.internal_agent_brief_readiness(AGENT)
        assert out == {"agent_name": AGENT, "fire": False, "reason": gate.held_reason(AGENT),
                       "basis": "unstamped_companion"}


# ---------------------------------------------------------------------------
# The role card
# ---------------------------------------------------------------------------

class TestRoleCard:
    def test_a_rollout_stamp_is_not_an_owners_act(self):
        from client_portal.role_card import effective_readiness
        r = effective_readiness("ready", {"status": "ready", "changed_at": "t", "changed_by": _gate().ROLLOUT_CHANGED_BY})
        assert r["source"] == "rollout" and r["changed_by"] is None
        owner = effective_readiness(None, {"status": "ready", "changed_at": "t", "changed_by": "o@example.com"})
        assert owner["source"] == "owner" and owner["changed_by"] == "o@example.com"

    def test_the_rollout_source_passes_the_response_model(self):
        from client_portal.models import PortalRoleReadiness
        PortalRoleReadiness(status="ready", source="rollout")

    @pytest.mark.parametrize("stamp,schedules,held", [
        (None, [SimpleNamespace(enabled=1, deliver_to_workspace_email="s@example.com")], True),
        ({"status": "calibrating"}, [SimpleNamespace(enabled=1, deliver_to_workspace_email="s@example.com")], True),
        ({"status": "ready"}, [SimpleNamespace(enabled=1, deliver_to_workspace_email="s@example.com")], False),
        (None, [SimpleNamespace(enabled=0, deliver_to_workspace_email="s@example.com")], False),
        (None, [SimpleNamespace(enabled=1, deliver_to_workspace_email=None)], False),
        (None, [], False),
    ])
    def test_brief_held(self, monkeypatch, stamp, schedules, held):
        from client_portal import role_card
        monkeypatch.setattr(role_card.db, "list_agent_schedules", lambda agent: schedules)
        monkeypatch.setattr(role_card.db, "get_autonomy_enabled", lambda agent: True)
        assert role_card._brief_held(AGENT, stamp) is held

    def test_autonomy_off_is_not_reported_as_a_held_brief(self, monkeypatch):
        """The autonomy gate stops the schedule first; "mark it ready" would start nothing."""
        from client_portal import role_card
        monkeypatch.setattr(role_card.db, "list_agent_schedules",
                            lambda agent: [SimpleNamespace(enabled=1, deliver_to_workspace_email="s@example.com")])
        monkeypatch.setattr(role_card.db, "get_autonomy_enabled", lambda agent: False)
        assert role_card._brief_held(AGENT, None) is False

    @pytest.mark.asyncio
    async def test_an_external_client_is_never_told_about_a_held_brief(self, monkeypatch):
        """The card serves both doors; `brief_held` is computed for platform viewers only."""
        from client_portal import role_card
        from services import docker_utils
        from services import agent_client

        async def running(agent):
            return "running"

        class Client:
            async def read_file(self, path, timeout=30.0):
                if path == "template.yaml":
                    return {"success": True, "content": "x-role:\n  role: sales-lead\n"}
                return {"success": True, "not_found": True, "content": None}
        monkeypatch.setattr(docker_utils, "agent_container_state_async", running)
        monkeypatch.setattr(agent_client, "get_agent_client", lambda name: Client())
        monkeypatch.setattr(role_card, "_readiness_stamp", lambda agent: None)
        monkeypatch.setattr(role_card, "_walkthrough", lambda *a: {"asks": 0, "target": 10, "rated_down": 0, "unavailable": False})
        monkeypatch.setattr(role_card, "_is_owner", lambda *a: False)
        monkeypatch.setattr(role_card, "_brief_held", lambda agent, stamp: True)
        client = await role_card.build_role_card(AGENT, "client@example.com", is_platform=False,
                                                 admit_objectives=lambda: True)
        platform = await role_card.build_role_card(AGENT, "user@example.com", is_platform=True,
                                                   admit_objectives=lambda: True)
        assert client["brief_held"] is False and platform["brief_held"] is True

    def test_brief_held_says_nothing_when_schedules_are_unreadable(self, monkeypatch):
        from client_portal import role_card

        def boom(agent):
            raise RuntimeError("db")
        monkeypatch.setattr(role_card.db, "list_agent_schedules", boom)
        assert role_card._brief_held(AGENT, None) is False


# ---------------------------------------------------------------------------
# The rollout seed — both tracks
# ---------------------------------------------------------------------------

@pytest.fixture()
def seed_db():
    from db.schema import TABLES
    conn = sqlite3.connect(":memory:")
    for t in ("agent_ownership", "agent_schedules", "agent_role_readiness"):
        conn.execute(TABLES[t])
    now = "2026-09-24T00:00:00.000000Z"

    def agent(name, deleted=None, autonomy=1):
        conn.execute("INSERT INTO agent_ownership (agent_name, owner_id, created_at, deleted_at, autonomy_enabled) "
                     "VALUES (?,1,?,?,?)", (name, now, deleted, autonomy))

    def sched(sid, name, enabled=1, seat="s@example.com", deleted=None):
        conn.execute(
            "INSERT INTO agent_schedules (id, agent_name, name, cron_expression, message, enabled, owner_id, "
            "created_at, updated_at, deliver_to_workspace_email, deleted_at) VALUES (?,?,?,?,?,?,1,?,?,?,?)",
            (sid, name, sid, "0 9 * * *", "brief", enabled, now, now, seat, deleted))

    agent("live-brief"); sched("a1", "live-brief"); sched("a2", "live-brief")   # two briefs, one stamp
    agent("stamped-calibrating"); sched("b1", "stamped-calibrating")
    conn.execute("INSERT INTO agent_role_readiness VALUES ('stamped-calibrating','calibrating',?, 'owner@example.com')", (now,))
    agent("disabled-brief"); sched("c1", "disabled-brief", enabled=0)
    agent("deleted-schedule"); sched("d1", "deleted-schedule", deleted=now)
    agent("no-seat"); sched("e1", "no-seat", seat=None)
    agent("blank-seat"); sched("f1", "blank-seat", seat="")
    agent("deleted-agent", deleted=now); sched("g1", "deleted-agent")
    agent("autonomy-off", autonomy=0); sched("h1", "autonomy-off")   # its brief does not fire today
    conn.commit()
    yield conn
    conn.close()


def _stamps(conn):
    return {r[0]: (r[1], r[3]) for r in conn.execute("SELECT * FROM agent_role_readiness")}


EXPECTED = {
    "live-brief": ("ready", "rollout:ent#689"),
    "stamped-calibrating": ("calibrating", "owner@example.com"),
}


def test_sqlite_seed_stamps_only_live_briefs_and_never_overwrites(seed_db):
    from db.migrations import _migrate_role_readiness_rollout_seed
    _migrate_role_readiness_rollout_seed(seed_db.cursor(), seed_db)
    assert _stamps(seed_db) == EXPECTED
    _migrate_role_readiness_rollout_seed(seed_db.cursor(), seed_db)   # idempotent
    assert _stamps(seed_db) == EXPECTED


def test_alembic_seed_matches_the_sqlite_track(seed_db, monkeypatch):
    """The PG revision's SQL, executed on SQLite (ON CONFLICT DO NOTHING is valid
    on both): the two tracks must stamp the same set."""
    import importlib.util
    from pathlib import Path
    import sqlalchemy as sa
    path = Path(__file__).resolve().parents[2] / "src/backend/migrations/versions/0074_role_readiness_rollout_seed.py"
    spec = importlib.util.spec_from_file_location("rev0073", path)
    rev = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rev)

    engine = sa.create_engine("sqlite://", creator=lambda: seed_db)
    with engine.begin() as conn:
        monkeypatch.setattr(rev.op, "get_bind", lambda: conn)
        rev.upgrade()
        rev.upgrade()
    assert _stamps(seed_db) == EXPECTED


def test_the_seed_is_registered_on_the_sqlite_track():
    from db.migrations import MIGRATIONS
    assert "role_readiness_rollout_seed" in [name for name, _ in MIGRATIONS]
