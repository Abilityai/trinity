"""trinity-enterprise#815 — a broad operator-queue read is complete before the limit.

`GET /api/operator-queue` sorted and cut the fleet's rows in SQL, and only then
dropped the rows the caller does not receive: the platform's heads-ups "about a
person" for every machine key (#715), and — in the MCP — every agent outside an
agent key's `{self} ∪ permitted`. A page could come back short, or empty, while
rows the caller may see sat just below the cut, and `count < limit` read as
"that is everything". An agent then concluded "never asked" and asked again.

The rule this file pins: every visibility filter runs in SQL, in the one WHERE
builder (`_list_conditions`), before `LIMIT`. The page, `total`, the flags and
`has_more` are then computed over one predicate and cannot disagree.

Harness: the ent#715 one — the real router in a small FastAPI app with
`get_current_user` overridden on the dependency the routes captured — over a
fresh SQLite file per test built by `init_schema` (the #3059 fixture), so a
broad read sees only this test's rows. Small explicit limits, never the default.

Related flow: docs/memory/feature-flows/operating-room.md
"""

import os
import sys
import uuid

import pytest

pytest.importorskip("sqlalchemy")

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")

_BACKEND = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend")
)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

pytestmark = pytest.mark.unit

# Test-local literal, never imported from the service: importing the code's own
# tuple would make the exclusion check circular.
ABOUT_A_PERSON = ("workspace-problem-", "portal-inbox-collision-", "gate-")

PERSON_PRINCIPALS = [
    pytest.param({}, id="jwt"),
    pytest.param({"mcp_scope": "user"}, id="user-key"),
]


@pytest.fixture()
def qdb(tmp_path, monkeypatch):
    """A fresh SQLite file per test: a broad read sees only this test's rows."""
    db_file = tmp_path / "ent815.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_file))
    import db.connection as conn_mod

    monkeypatch.setattr(conn_mod, "DB_PATH", str(db_file))
    import sqlite3
    from db.schema import init_schema

    raw = sqlite3.connect(db_file)
    init_schema(raw.cursor(), raw)
    raw.commit()
    raw.close()
    from database import db

    return db


@pytest.fixture()
def names():
    """A unique agent-name prefix per test (pytest-randomly runs three seeds)."""
    tag = uuid.uuid4().hex[:8]
    return lambda role: f"ent815-{tag}-{role}"


# ---------------------------------------------------------------------------
# One app over the real router (the ent#715 harness).
# ---------------------------------------------------------------------------

_APP = None
_PRINCIPAL = {"user": None}


def _client():
    global _APP
    from fastapi.testclient import TestClient

    if _APP is None:
        from fastapi import FastAPI
        from routers import operator_queue as r

        app = FastAPI()
        app.include_router(r.router)
        found = set()

        def walk(dependant):
            for sub in dependant.dependencies:
                if getattr(sub.call, "__name__", "") == "get_current_user":
                    found.add(sub.call)
                walk(sub)

        for route in r.router.routes:
            if getattr(route, "dependant", None) is not None:
                walk(route.dependant)
        assert found, "no get_current_user dependency on the operator-queue routes"
        for call in found:
            app.dependency_overrides[call] = lambda: _PRINCIPAL["user"]
        _APP = app
    return TestClient(_APP, raise_server_exceptions=True)


def _as(**principal):
    from models import User

    base = {
        "id": 7,
        "username": "op-815",
        "email": "op-815@example.com",
        "role": "admin",
    }
    base.update(principal)
    _PRINCIPAL["user"] = User(**base)


def _seed(db, agent, *, priority="medium", rid=None, created_at=None, status=None):
    item = {
        "id": rid if rid is not None else f"r-{uuid.uuid4().hex[:12]}",
        "type": "question",
        "priority": priority,
        "title": "t",
        "question": "q",
    }
    if created_at:
        item["created_at"] = created_at
    if status:
        item["status"] = status
    return db.create_operator_queue_item(agent, item)


def _insert_raw(**values):
    """A row no native path would write (a NULL request_id, NULL keys)."""
    from db.engine import get_engine
    from db.tables import operator_queue

    row = {"id": uuid.uuid4().hex, "title": "t", "question": "q"}
    row.update(values)
    with get_engine().begin() as conn:
        conn.execute(operator_queue.insert().values(**row))
    return row["id"]


def _get(path="/api/operator-queue", expect=200, **params):
    res = _client().get(path, params=params)
    assert res.status_code == expect, res.text
    return res.json()


def _offset_walk(limit, **params):
    """Walk `next_offset` from 0; returns (ids in order, pages)."""
    ids, pages, offset = [], [], 0
    for _ in range(200):
        body = _get(limit=limit, offset=offset, **params)
        assert "has_more" in body and "next_offset" in body, sorted(body)
        pages.append(body)
        ids += [i["id"] for i in body["items"]]
        if not body["has_more"]:
            assert body["next_offset"] is None
            return ids, pages
        assert body["next_offset"] == offset + limit
        offset = body["next_offset"]
    raise AssertionError("offset walk did not end")


def _about_a_person(rid):
    return rid is not None and rid.strip().lower().startswith(ABOUT_A_PERSON)


# ---------------------------------------------------------------------------
# Commit 1 — the about-a-person exclusion runs in SQL; offset paging fields.
# ---------------------------------------------------------------------------

STATIC_SET_PRINCIPALS = [
    pytest.param({"mcp_scope": "system"}, False, id="system-key"),
    pytest.param({}, True, id="jwt-person"),
]


@pytest.mark.parametrize("principal,is_person", STATIC_SET_PRINCIPALS)
def test_b3a_offset_walk_over_a_static_set_returns_each_visible_row_once(
    qdb, names, principal, is_person
):
    """B3a (AC4, offset mode): ~23 rows raised in the same instant, some about a
    person; pages of 7. Every visible row exactly once, every page but the last
    full — a machine's page is no longer emptied after the cut."""
    same = "2026-10-01T10:00:00.000000Z"
    a, b = names("a"), names("b")
    expected = set()
    for i in range(23):
        agent = a if i % 2 else b
        rid = f"gate-{i}" if i % 4 == 0 else f"q-{i}"
        uid = _seed(
            qdb, agent, priority="high" if i % 3 else "medium", rid=rid, created_at=same
        )
        if is_person or not _about_a_person(rid):
            expected.add(uid)
    _as(**principal)
    ids, pages = _offset_walk(7)
    assert len(ids) == len(set(ids)), "a row was returned twice"
    assert set(ids) == expected
    for page in pages[:-1]:
        assert page["count"] == 7, [p["count"] for p in pages]
    assert all(p["total"] == len(expected) for p in pages)


def test_b5_system_key_is_not_narrowed_and_total_excludes_about_a_person(qdb, names):
    """B5: a system key sees every agent (it is not an agent), and `total` counts
    only rows it receives — ≥2 about-a-person rows on a stranger agent are
    neither returned nor counted."""
    stranger, other = names("stranger"), names("other")
    visible = {_seed(qdb, stranger) for _ in range(4)} | {
        _seed(qdb, other) for _ in range(2)
    }
    for i in range(3):
        _seed(qdb, stranger, priority="critical", rid=f"gate-{i}")
    _as(mcp_scope="system")
    body = _get(limit=10)
    assert {i["id"] for i in body["items"]} == visible
    assert body["total"] == len(visible)
    assert body["has_more"] is False


def _saturate_with_gate_rows(db, me):
    """15 critical `gate-` rows plus case/whitespace variants and a NULL
    request_id row, on the caller's own agent, above 12 medium question rows."""
    gate = [_seed(db, me, priority="critical", rid=f"gate-{i}") for i in range(15)]
    gate += [
        _seed(db, me, priority="critical", rid=r)
        for r in ("GATE-x", " gate-y", "\tgate-z")
    ]
    null_rid = _insert_raw(
        agent_name=me,
        request_id=None,
        status="pending",
        priority="critical",
        type="question",
        created_at="2026-10-01T09:00:00.000000Z",
    )
    own = [_seed(db, me) for _ in range(12)]
    return gate, null_rid, own


MACHINE_KEYS = [
    pytest.param({"mcp_scope": "system"}, id="system-key"),
    pytest.param({"mcp_scope": "agent"}, id="agent-key"),
]


@pytest.mark.parametrize("principal", MACHINE_KEYS)
def test_b7_about_a_person_rows_cannot_starve_a_machine_page(qdb, names, principal):
    """B7 (D4): the gate rows sort first and used to fill the SQL window, then be
    dropped — a machine got 0 rows. Now the page fills with the caller's own
    rows; `total` excludes every variant (case, leading whitespace); the NULL
    request_id row is still returned; a person still sees the gate rows."""
    me = names("me")
    gate, null_rid, own = _saturate_with_gate_rows(qdb, me)
    if principal.get("mcp_scope") == "agent":
        principal = {**principal, "agent_name": me}
    _as(**principal)
    body = _get(limit=10)
    got = {i["id"] for i in body["items"]}
    assert body["count"] == 10
    assert not got & set(gate)
    assert null_rid in got
    assert body["total"] == len(own) + 1
    assert body["has_more"] is True

    _as()
    person = _get(limit=10)
    assert {i["id"] for i in person["items"]} & set(gate)
    assert person["total"] == len(gate) + 1 + len(own)


def test_b8_agent_route_excludes_about_a_person_in_sql(qdb, names):
    """B8 (D4, the second list route): `/agents/{name}` as a system key under the
    same saturation fills its page instead of returning nothing."""
    me = names("me")
    gate, null_rid, own = _saturate_with_gate_rows(qdb, me)
    _as(mcp_scope="system")
    body = _get(f"/api/operator-queue/agents/{me}", limit=10)
    got = {i["id"] for i in body["items"]}
    assert body["count"] == 10
    assert not got & set(gate)
    assert null_rid in got


@pytest.mark.parametrize("dialect", ["postgresql", "sqlite"])
def test_b11_exclusion_compiles_on_both_dialects(dialect):
    """B11 (dialect parity; CI has no PostgreSQL pytest): the exclusion is the
    portable `lower(ltrim(coalesce(...)))` shape on both dialects."""
    from sqlalchemy import and_
    from sqlalchemy.dialects import postgresql, sqlite
    from db.operator_queue import OperatorQueueOperations

    conds = OperatorQueueOperations()._list_conditions(
        exclude_request_id_prefixes=("gate-", "workspace-problem-")
    )
    d = postgresql.dialect() if dialect == "postgresql" else sqlite.dialect()
    sql = str(and_(*conds).compile(dialect=d))
    assert sql.count("lower(ltrim(coalesce(operator_queue.request_id") == 2, sql


def test_b13_has_more_comes_from_the_page_read_not_the_count(qdb, names, monkeypatch):
    """B13 (AC3): a row landing between the page read and the count must not
    make `has_more` disagree with the page. Exactly `limit` rows → the page is
    the last one, whatever the count says a moment later."""
    agent = names("a")
    for _ in range(7):
        _seed(qdb, agent)
    real_count = qdb.count_operator_queue_items

    def count_after_a_write(**filters):
        _seed(qdb, agent)
        return real_count(**filters)

    monkeypatch.setattr(qdb, "count_operator_queue_items", count_after_a_write)
    _as()
    body = _get(limit=7)
    assert body["count"] == 7
    assert body["has_more"] is False
    assert body["next_offset"] is None
    assert body["total"] == 8


def test_b14_a_belt_drop_nulls_total_and_warns(qdb, names):
    """B14 (`total` honest): a legacy request_id led by a non-ASCII space
    (U+00A0) passes the SQL exclusion — portable SQL trims ASCII whitespace
    only — and the Python belt drops it. The page then says so: `total` null
    plus a warning, while the paging fields still come from the SQL page."""
    me = names("me")
    _seed(qdb, me, priority="critical", rid=" gate-x")
    for _ in range(12):
        _seed(qdb, me)
    _as(mcp_scope="system")
    body = _get(limit=10)
    assert body["count"] == 9
    assert body["total"] is None
    assert body["warnings"], body
    assert body["has_more"] is True
    assert body["next_offset"] == 10
