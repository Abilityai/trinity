"""Every schedule read says what tier its runs get, and who created it (#4281).

A share holder may create a schedule on an agent shared with them (ent#498),
and its runs are rated by them, the creator: a shared person's turn, with no
shell. Before #4281 no API read said so. Now list, get, create, update, enable,
disable and the admin fleet read carry:

* `runs_as` — `owner` | `shared_person` | null (not computed, never a guess);
* `runs_as_reason` — `{code, message}`;
* `created_by_you`, and `created_by` (display name, else username) for the
  agent's owner tier and an agent key that is not acting for a shared person.

On a Gemini or Codex agent a shared-person schedule could never run, so
switching one on is refused with 409 `schedule_cannot_run_for_creator_on_runtime`
(decision D2); on Claude Code it is allowed and the response says how it runs
(D1).

Real routes, the real `get_current_user`, real JWTs and a real agent key, on
the unit conftest's SQLite. Only the runtime label and the ledger of running
turns are pinned, each to a value the environment would not produce.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event

from database import db
from db.engine import get_engine
from db_models import UserCreate
from dependencies import create_access_token
from routers import ops as ops_router
from routers import schedules as schedules_router
from services import docker_service

pytestmark = pytest.mark.unit

BODY = {"name": "brief", "cron_expression": "0 9 * * 1", "message": "Weekly brief", "timezone": "UTC"}
REFUSAL = "schedule_cannot_run_for_creator_on_runtime"


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(ops_router.router)
    app.include_router(schedules_router.router)
    return TestClient(app)


def _person(role, *, name=None):
    username = f"p4281-{uuid.uuid4().hex[:8]}@example.com"
    db.create_user(UserCreate(username=username, email=username, role=role, password="x", name=name))
    user = db.get_user_by_username(username)
    return SimpleNamespace(username=username, id=user["id"], jwt=create_access_token({"sub": username}))


@pytest.fixture
def world():
    owner = _person("creator")
    mate = _person("operator", name="Mate Shared")
    admin = _person("admin")
    agent = f"a4281-{uuid.uuid4().hex[:8]}"
    db.register_agent_owner(agent, owner.username)
    db.share_agent(agent, owner.username, mate.username)
    key = db.create_agent_mcp_api_key(agent, owner.username).api_key
    return SimpleNamespace(agent=agent, owner=owner, mate=mate, admin=admin, key=key)


def call(client, world, who, method, path="", **kw):
    token = world.key if who == "key" else getattr(world, who).jwt
    return client.request(method, f"/api/agents/{world.agent}/schedules{path}",
                          headers={"Authorization": f"Bearer {token}"}, **kw)


def created(client, world, who, **extra):
    resp = call(client, world, who, "POST", json={**BODY, **extra})
    assert resp.status_code == 201, resp.text
    return resp.json()


def by_id(rows, sid):
    return next(r for r in rows if r["id"] == sid)


def on_runtime(monkeypatch, runtime):
    """The agent's runtime label. Unit tests have no Docker, where the read
    falls back to Claude Code, so a refusal needs a pinned label."""
    monkeypatch.setattr(docker_service, "get_agent_runtime", lambda name: runtime)


# ---- the rating, on every read ---------------------------------------------------------

def test_a_share_holders_schedule_is_flagged_at_creation(client, world):
    body = created(client, world, "mate")
    assert body["runs_as"] == "shared_person"
    assert body["runs_as_reason"]["code"] == "creator_not_owner_tier"
    assert "not the agent's owner or an admin" in body["runs_as_reason"]["message"]
    assert body["created_by_you"] is True
    assert body["created_by"] is None          # a share holder is not told names
    assert "owner_id" not in body


@pytest.mark.parametrize("who", ["owner", "admin"])
def test_an_owner_tier_creator_runs_as_owner(client, world, who):
    body = created(client, world, who)
    assert (body["runs_as"], body["runs_as_reason"]["code"]) == ("owner", "creator_owner_tier")
    assert body["created_by_you"] is True


def test_the_owner_reads_who_created_each_schedule(client, world):
    mates = created(client, world, "mate")["id"]
    owners = created(client, world, "owner")["id"]
    rows = call(client, world, "owner", "GET").json()
    assert {k: by_id(rows, mates)[k] for k in ("runs_as", "created_by", "created_by_you")} == {
        "runs_as": "shared_person", "created_by": "Mate Shared", "created_by_you": False}
    assert {k: by_id(rows, owners)[k] for k in ("runs_as", "created_by", "created_by_you")} == {
        "runs_as": "owner", "created_by": world.owner.username, "created_by_you": True}
    one = call(client, world, "owner", "GET", f"/{mates}").json()
    assert (one["runs_as"], one["created_by"]) == ("shared_person", "Mate Shared")


def test_a_share_holder_reads_no_creator_names(client, world):
    owners = created(client, world, "owner")["id"]
    mates = created(client, world, "mate")["id"]
    rows = call(client, world, "mate", "GET").json()
    assert all(r["created_by"] is None for r in rows)
    assert by_id(rows, owners)["created_by_you"] is False and by_id(rows, mates)["created_by_you"] is True
    assert by_id(rows, owners)["runs_as"] == "owner"


def test_the_agents_key_reads_names_outside_a_shared_persons_turn(client, world):
    mates = created(client, world, "mate")["id"]
    assert by_id(call(client, world, "key", "GET").json(), mates)["created_by"] == "Mate Shared"


def test_the_agents_key_acting_for_a_shared_person_reads_no_names(client, world, monkeypatch):
    """ent#926: while a shared person's turn runs, the key's reads are theirs."""
    mates = created(client, world, "mate")["id"]
    owners = created(client, world, "owner")["id"]
    running = [{"id": "e-mate", "agent_name": world.agent, "source_user_id": world.mate.id,
                "schedule_id": None, "triggered_by": "user", "source_channel": None, "source_user_email": None}]
    monkeypatch.setattr(db, "get_running_turn_attribution", lambda agent: running if agent == world.agent else [])
    rows = call(client, world, "key", "GET").json()
    assert all(r["created_by"] is None for r in rows)
    # "You" is the person the key acts for.
    assert by_id(rows, mates)["created_by_you"] is True and by_id(rows, owners)["created_by_you"] is False


def test_a_creator_demoted_between_two_reads_flips_the_flag(client, world):
    """Live, not stored: an admin's schedule runs as owner until they lose the role."""
    creator = _person("admin", name="Was Admin")
    db.share_agent(world.agent, world.owner.username, creator.username)
    sid = created(client, SimpleNamespace(**{**vars(world), "admin": creator}), "admin")["id"]
    assert by_id(call(client, world, "owner", "GET").json(), sid)["runs_as"] == "owner"
    db.update_user_role(creator.username, "operator")
    after = by_id(call(client, world, "owner", "GET").json(), sid)
    assert (after["runs_as"], after["runs_as_reason"]["code"]) == ("shared_person", "creator_not_owner_tier")


def test_a_dangling_creator_reads_as_creator_missing(client, world):
    sid = created(client, world, "mate")["id"]
    from db.engine import get_engine as _engine
    from sqlalchemy import text
    with _engine().begin() as conn:
        conn.execute(text("UPDATE agent_schedules SET owner_id = :gone WHERE id = :sid"),
                     {"gone": 987654321, "sid": sid})
    row = by_id(call(client, world, "owner", "GET").json(), sid)
    assert (row["runs_as"], row["runs_as_reason"]["code"], row["created_by"]) == (
        "shared_person", "creator_missing", None)


def test_a_failing_rating_reads_as_not_computed_never_owner(client, world, monkeypatch):
    sid = created(client, world, "owner")["id"]

    def boom(*a, **k):
        raise RuntimeError("owner read failed")

    from services import turn_tier_service
    monkeypatch.setattr(turn_tier_service.RunTierLookups, "can_user_share_agent", boom)
    row = by_id(call(client, world, "owner", "GET").json(), sid)
    assert (row["runs_as"], row["runs_as_reason"]) == (None, None)


def test_update_enable_and_disable_answer_the_rating(client, world):
    sid = created(client, world, "mate")["id"]
    put = call(client, world, "mate", "PUT", f"/{sid}", json={"message": "New brief"})
    assert put.status_code == 200 and put.json()["runs_as"] == "shared_person"
    for verb in ("disable", "enable"):
        resp = call(client, world, "owner", "POST", f"/{sid}/{verb}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert (body["status"], body["runs_as"], body["runs_as_reason"]["code"], body["created_by"]) == (
            f"{verb}d", "shared_person", "creator_not_owner_tier", "Mate Shared")


# ---- D2: a schedule that could never run is not switched on ------------------------------

@pytest.mark.parametrize("runtime", ["gemini-cli", "codex"])
def test_a_share_holders_schedule_is_refused_on_a_runtime_that_cannot_run_it(client, world, monkeypatch, runtime):
    on_runtime(monkeypatch, runtime)
    before = len(db.list_agent_schedules(world.agent))
    resp = call(client, world, "mate", "POST", json=BODY)
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert (detail["code"], detail["runtime"], detail["cause"]) == (REFUSAL, runtime, "creator_not_owner_tier")
    assert runtime in detail["message"]
    assert len(db.list_agent_schedules(world.agent)) == before


@pytest.mark.parametrize("runtime", ["gemini-cli", "codex"])
def test_turning_one_on_is_refused_there_too(client, world, monkeypatch, runtime):
    sid = created(client, world, "mate", enabled=False)["id"]
    on_runtime(monkeypatch, runtime)
    enable = call(client, world, "owner", "POST", f"/{sid}/enable")
    put = call(client, world, "mate", "PUT", f"/{sid}", json={"enabled": True})
    assert (enable.status_code, put.status_code) == (409, 409)
    assert enable.json()["detail"]["code"] == put.json()["detail"]["code"] == REFUSAL
    assert db.get_schedule(sid).enabled is False
    # An edit that does not switch it on is not refused.
    assert call(client, world, "mate", "PUT", f"/{sid}", json={"message": "x"}).status_code == 200


@pytest.mark.parametrize("who", ["owner", "admin"])
def test_an_owner_tier_creator_is_not_refused_on_any_runtime(client, world, monkeypatch, who):
    on_runtime(monkeypatch, "codex")
    assert call(client, world, who, "POST", json=BODY).status_code == 201


def test_on_claude_code_it_is_created_and_flagged(client, world, monkeypatch):
    on_runtime(monkeypatch, "claude-code")
    assert created(client, world, "mate")["runs_as"] == "shared_person"


def test_when_docker_cannot_be_read_it_warns_and_does_not_refuse(client, world, monkeypatch):
    """The runtime read falls back to Claude Code on any Docker fault."""
    class _Unreadable:
        class containers:
            @staticmethod
            def get(name):
                raise RuntimeError("docker down")
    monkeypatch.setattr(docker_service, "docker_client", _Unreadable())
    assert created(client, world, "mate")["runs_as"] == "shared_person"


# ---- no per-row lookups ------------------------------------------------------------------

class _Queries:
    def __init__(self):
        self.count = 0

    def __call__(self, *args, **kwargs):
        self.count += 1


def _counted(fn):
    counter = _Queries()
    engine = get_engine()
    event.listen(engine, "before_cursor_execute", counter)
    try:
        fn()
    finally:
        event.remove(engine, "before_cursor_execute", counter)
    return counter.count


def test_a_long_list_costs_what_a_short_one_does(client, world):
    """20 schedules from 2 creators read in the same number of queries as 2."""
    created(client, world, "mate")
    created(client, world, "owner")

    def read():
        resp = call(client, world, "owner", "GET")
        assert resp.status_code == 200
        assert {r["runs_as"] for r in resp.json()} == {"owner", "shared_person"}

    read()
    short = _counted(read)
    for i in range(18):
        created(client, world, "mate" if i % 2 else "owner")
    assert len(db.list_agent_schedules(world.agent)) == 20
    assert _counted(read) == short


def _fleet(n):
    owner, mate = _person("creator"), _person("operator", name="Fleet Mate")
    agents = []
    for _ in range(n):
        agent = f"f4281-{uuid.uuid4().hex[:8]}"
        db.register_agent_owner(agent, owner.username)
        db.share_agent(agent, owner.username, mate.username)
        for who in (owner, mate):
            assert db.create_schedule(agent, who.username, schedules_router.ScheduleCreate(**BODY))
        agents.append(agent)
    return agents


def test_the_fleet_read_costs_the_same_on_3_agents_and_on_30(client, world):
    headers = {"Authorization": f"Bearer {world.admin.jwt}"}
    small = set(_fleet(3))

    def read(expect):
        resp = client.get("/api/ops/schedules", headers=headers)
        assert resp.status_code == 200, resp.text
        mine = [r for r in resp.json()["schedules"] if r["agent_name"] in expect]
        assert len(mine) == 2 * len(expect)
        assert sorted((r["runs_as"], r["created_by_you"]) for r in mine) == sorted(
            [("owner", False), ("shared_person", False)] * len(expect))
        assert {r["created_by"] for r in mine if r["runs_as"] == "shared_person"} == {"Fleet Mate"}
        assert all(r["runs_as_reason"]["code"] in ("creator_owner_tier", "creator_not_owner_tier") for r in mine)

    read(small)
    on_three = _counted(lambda: read(small))
    large = small | set(_fleet(27))
    assert _counted(lambda: read(large)) == on_three


# ---- the memo and the bulk reads agree with the database -----------------------------------

def test_the_memo_and_the_bulk_reads_answer_as_the_database_does(world):
    """`RunTierLookups.can_user_share_agent` restates `db.can_user_share_agent`'s
    rule over the memo, and the two bulk reads restate `get_agent_owner` and
    `get_user_by_id`. Driven over the same rows, they must agree, including an
    agent whose owner's user row is gone (the INNER join)."""
    from sqlalchemy import text
    from services.turn_tier_service import RunTierLookups
    orphan = f"o4281-{uuid.uuid4().hex[:8]}"
    db.register_agent_owner(orphan, world.owner.username)
    with get_engine().begin() as conn:
        conn.execute(text("UPDATE agent_ownership SET owner_id = :gone WHERE agent_name = :a"),
                     {"gone": 987654320, "a": orphan})
    agents = [world.agent, orphan, "a4281-never-created"]
    people = [world.owner, world.mate, world.admin]
    names = [p.username for p in people] + ["nobody-4281@example.com"]

    memo = RunTierLookups(db)
    for agent in agents:
        for username in names:
            assert memo.can_user_share_agent(username, agent) == bool(db.can_user_share_agent(username, agent)), (
                agent, username)
    assert db.get_agent_owners(agents) == {a: db.get_agent_owner(a) for a in agents if db.get_agent_owner(a)}
    assert db.get_agent_owner(orphan) is None and orphan not in db.get_agent_owners(agents)
    ids = [p.id for p in people] + [987654320]
    assert db.get_users_by_ids(ids) == {i: db.get_user_by_id(i) for i in ids if db.get_user_by_id(i)}
    assert db.get_users_by_ids([]) == {} and db.get_agent_owners([]) == {}
