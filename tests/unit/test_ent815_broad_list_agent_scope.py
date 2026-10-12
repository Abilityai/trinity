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
    portable `lower(replace(replace(ltrim(coalesce(...)))))` shape on both
    dialects — the trim set and the two code-point rewrites are plain
    functions, no dialect branch."""
    from sqlalchemy import and_
    from sqlalchemy.dialects import postgresql, sqlite
    from db.operator_queue import OperatorQueueOperations

    conds = OperatorQueueOperations()._list_conditions(
        exclude_request_id_prefixes=("gate-", "workspace-problem-")
    )
    d = postgresql.dialect() if dialect == "postgresql" else sqlite.dialect()
    sql = str(and_(*conds).compile(dialect=d))
    assert sql.count(
        "lower(replace(replace(ltrim(coalesce(operator_queue.request_id") == 2, sql


def test_b11b_the_trim_set_is_exactly_pythons_whitespace():
    """B11b: the characters SQL trims are exactly the ones `str.strip()` removes
    (`str.isspace()`), so a legacy row led by any of them is excluded in SQL,
    not only by the Python belt — computed here, never copied from the code."""
    from db.operator_queue import OperatorQueueOperations

    expected = {c for c in map(chr, range(0x110000)) if c.isspace()}
    trim = OperatorQueueOperations._PY_WHITESPACE
    assert len(trim) == len(set(trim)), "a duplicated character"
    assert set(trim) == expected


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


def test_b14_a_belt_drop_nulls_total_and_warns(qdb, names, monkeypatch):
    """B14 (`total` honest): when the Python belt drops a row the SQL page kept,
    the page says so — `total` null plus a warning, while the paging fields
    still come from the SQL page. The SQL rule now equals the Python rule, so
    the drop is forced through the real route: the tuple the SQL reads
    (`ABOUT_A_PERSON_ID_PREFIXES`) omits `gate-`, the one the belt reads
    (`_ABOUT_A_PERSON_ID_PREFIXES`) does not."""
    from services import operator_queue_service as svc

    monkeypatch.setattr(svc, "ABOUT_A_PERSON_ID_PREFIXES",
                        ("workspace-problem-", "portal-inbox-collision-"))
    me = names("me")
    _seed(qdb, me, priority="critical", rid="gate-x")
    for _ in range(12):
        _seed(qdb, me)
    _as(mcp_scope="system")
    body = _get(limit=10)
    assert body["count"] == 9
    assert body["total"] is None
    assert body["warnings"], body
    assert body["has_more"] is True
    assert body["next_offset"] == 10


# Legacy ids ingest refuses today (`_ID_RE`) but an older row can carry: led by
# whitespace only `str.isspace()` knows (an ASCII separator, NBSP, ideographic
# space), and the KELVIN SIGN, whose Python `.lower()` is ASCII `k`.
_LEGACY_ABOUT_A_PERSON = ("\u00a0gate-x", "\x1cgate-y", "\u3000GATE-z",
                          "wor\u212aspace-problem-w")


@pytest.mark.parametrize("principal", MACHINE_KEYS)
def test_b16_total_is_exact_for_legacy_about_a_person_ids(qdb, names, principal):
    """B16 (T1): the SQL exclusion equals `is_about_a_person` exactly, so a
    legacy about-a-person row that sorts OFF the page is not counted either.
    Before, SQL trimmed ASCII whitespace only and lowered ASCII only: these rows
    were counted in `total` and pushed `has_more` true, while the belt never
    saw them (they were not on the page) — an overcount nothing flagged."""
    me = names("me")
    own = _seed(qdb, me, priority="high")
    lead = sorted(c for c in map(chr, range(0x110000)) if c.isspace())
    rids = list(_LEGACY_ABOUT_A_PERSON) + [f"{c}gate-{ord(c):x}" for c in lead]
    legacy = [_seed(qdb, me, priority="low", rid=r) for r in rids]
    if principal.get("mcp_scope") == "agent":
        principal = {**principal, "agent_name": me}
    _as(**principal)
    body = _get(limit=1)
    assert [i["id"] for i in body["items"]] == [own]
    assert body["total"] == 1, body
    assert body["has_more"] is False
    assert body["next_offset"] is None
    assert "warnings" not in body

    _as()
    person = _get(limit=100)
    assert {i["id"] for i in person["items"]} == {own, *legacy}
    assert person["total"] == 1 + len(legacy)


def _glibc_lower(value):
    """PostgreSQL's `lower` on a UTF-8, non-Turkic database: glibc `towlower`,
    a simple per-character mapping. U+0130 becomes a bare `i` (Python gives
    `i` + U+0307); any other character takes its one-character Python
    `.lower()`, and a character whose `.lower()` is longer stays as it is."""
    if value is None:
        return None
    out = []
    for ch in str(value):
        if ch == "\u0130":
            out.append("i")
            continue
        low = ch.lower()
        out.append(low if len(low) == 1 else ch)
    return "".join(out)


@pytest.fixture()
def sql_lower(request, qdb):
    """`sqlite`: SQLite's built-in, ASCII-only `lower` (what CI runs).
    `glibc`: every connection the route's engine opens gets `lower` replaced
    by `_glibc_lower`, so the PostgreSQL behaviour the U+0130 rewrite exists
    for runs here too. The listener is removed and the engine disposed
    afterwards, so no other test inherits it under pytest-randomly."""
    if request.param == "sqlite":
        yield request.param
        return
    from sqlalchemy import event
    from db.engine import get_engine

    engine = get_engine()

    def _install(dbapi_conn, _record):
        dbapi_conn.create_function("lower", 1, _glibc_lower, deterministic=True)

    event.listen(engine, "connect", _install)
    engine.dispose()
    try:
        yield request.param
    finally:
        event.remove(engine, "connect", _install)
        engine.dispose()


@pytest.mark.parametrize("sql_lower", ["sqlite", "glibc"], indirect=True)
@pytest.mark.parametrize("principal", MACHINE_KEYS)
def test_b17_a_dotted_capital_i_is_not_an_about_a_person_match(
        qdb, names, principal, sql_lower):
    """B17 (T1, the reverse direction): Python lowers U+0130 to `i` + U+0307,
    so `portal-\u0130nbox-collision-…` is NOT about a person and a machine
    receives it. PostgreSQL's glibc `lower` gives a bare `i`, which would match
    — the SQL rewrites U+0130 to Python's two code points first, so the row is
    kept on both. SQLite's own `lower` is ASCII-only and keeps it with or
    without the rewrite, so the `glibc` case is the one that can go red."""
    me = names("me")
    kept = _seed(qdb, me, rid="portal-\u0130nbox-collision-x")
    if principal.get("mcp_scope") == "agent":
        principal = {**principal, "agent_name": me}
    _as(**principal)
    body = _get(limit=10)
    assert [i["id"] for i in body["items"]] == [kept]
    assert body["total"] == 1
    assert "warnings" not in body


# ---------------------------------------------------------------------------
# Commit 2 — an agent key's broad read is narrowed to {self} ∪ permitted in
# SQL; `agent_names` narrows further; `permissions?strict=true`.
# ---------------------------------------------------------------------------


def _fleet(db, names, *, strangers=30, mine=3, peers=3, stranger_priority="high"):
    me, peer, stranger = names("me"), names("peer"), names("stranger")
    db.add_agent_permission(me, peer, "op-815")
    own = [_seed(db, me) for _ in range(mine)]
    theirs = [_seed(db, peer) for _ in range(peers)]
    alien = [_seed(db, stranger, priority=stranger_priority) for _ in range(strangers)]
    return me, peer, stranger, own, theirs, alien


OWNERS = [
    pytest.param(None, id="admin-owner"),
    pytest.param("user", id="non-admin-owner"),
]


def _owner(monkeypatch, db, role, visible):
    """An agent key resolves to its OWNER. An admin owner's accessible set is
    None (no filter); a non-admin's is the agents it owns or was shared."""
    if role is None:
        return {}
    monkeypatch.setattr(db, "get_accessible_agent_names",
                        lambda email, is_admin=False: sorted(visible))
    return {"role": role}


@pytest.mark.parametrize("owner_role", OWNERS)
def test_b1_peer_and_own_rows_below_a_saturated_window_are_returned(
        qdb, names, monkeypatch, owner_role):
    """B1 (AC1, AC2, AC5 — the issue's window test): 30 high-priority rows on
    a stranger the agent may not see sort above its own and its permitted
    peer's medium rows. limit=10 used to return 10 stranger rows, which the MCP
    then dropped: count 0. Now the page is self's and the peer's rows only."""
    me, peer, stranger, own, theirs, alien = _fleet(qdb, names)
    extra = _owner(monkeypatch, qdb, owner_role, {me, peer, stranger})
    _as(mcp_scope="agent", agent_name=me, **extra)
    body = _get(limit=10)
    got = {i["id"] for i in body["items"]}
    assert got == set(own) | set(theirs)
    assert {i["agent_name"] for i in body["items"]} == {me, peer}
    assert body["total"] == 6
    assert body["has_more"] is False


def test_b2_offset_mode_paging_fields(qdb, names):
    """B2 (AC3): `total` is the visible count; `has_more` true on a cut page and
    false on the last; `next_offset` = offset + limit or null; `next_cursor`
    null in offset mode."""
    me, peer, stranger, own, theirs, alien = _fleet(qdb, names, mine=5, peers=4, strangers=8)
    _as(mcp_scope="agent", agent_name=me)
    first = _get(limit=4)
    assert (first["total"], first["count"], first["has_more"],
            first["next_offset"], first["next_cursor"]) == (9, 4, True, 4, None)
    last = _get(limit=4, offset=8)
    assert (last["total"], last["count"], last["has_more"],
            last["next_offset"], last["next_cursor"]) == (9, 1, False, None, None)


def test_b3b_offset_walk_under_an_agent_key_interleaved_with_strangers(qdb, names):
    """B3b (AC4): the offset walk under an agent key, with stranger rows
    interleaved at every priority and one shared timestamp — every visible row
    exactly once, no stranger row, every page but the last full."""
    same = "2026-10-01T10:00:00.000000Z"
    me, peer, stranger = names("me"), names("peer"), names("stranger")
    qdb.add_agent_permission(me, peer, "op-815")
    prios = ("critical", "high", "medium", "low")
    visible = set()
    for i in range(23):
        agent = me if i % 2 else peer
        visible.add(_seed(qdb, agent, priority=prios[i % 4], created_at=same))
        _seed(qdb, stranger, priority=prios[(i + 1) % 4], created_at=same)
    _as(mcp_scope="agent", agent_name=me)
    ids, pages = _offset_walk(7)
    assert len(ids) == len(set(ids))
    assert set(ids) == visible
    for page in pages[:-1]:
        assert page["count"] == 7


@pytest.mark.parametrize("principal", PERSON_PRINCIPALS)
def test_b4_persons_see_every_row_and_total_counts_them(qdb, names, principal):
    """B4 (guard): a JWT person and a person's own user key are not narrowed and
    still receive the about-a-person rows; `total` counts them."""
    me, peer, stranger, own, theirs, alien = _fleet(qdb, names, strangers=4)
    gate = [_seed(qdb, me, priority="critical", rid=f"gate-{i}") for i in range(2)]
    _as(**principal)
    body = _get(limit=50)
    assert {i["id"] for i in body["items"]} == set(own + theirs + alien + gate)
    assert body["total"] == len(own + theirs + alien + gate)


def test_b6_an_agent_key_naming_a_stranger_gets_an_empty_page(qdb, names):
    """B6 (no widening, self-uniform): an explicit `agent_name` outside
    {self} ∪ permitted is an empty page with total 0 — the same answer whether
    that agent exists or not."""
    me, peer, stranger, own, theirs, alien = _fleet(qdb, names, strangers=3)
    _as(mcp_scope="agent", agent_name=me)
    real = _get(agent_name=stranger, limit=10)
    ghost = _get(agent_name=names("ghost"), limit=10)
    for body in (real, ghost):
        assert (body["items"], body["count"], body["total"], body["has_more"]) == ([], 0, 0, False)


def test_b9_an_agent_scope_without_an_agent_identity_is_refused(qdb):
    """B9: an agent-scoped principal that carries no agent name cannot be
    narrowed, so it is refused with a reason — never a quiet empty page."""
    _as(mcp_scope="agent")
    res = _client().get("/api/operator-queue", params={"limit": 10})
    assert res.status_code == 403, res.text
    assert "agent identity" in res.json()["detail"]


def test_b10_flags_count_only_the_agents_the_key_may_see(qdb, names):
    """B10: `undelivered_count` / `closed_by_filer_count` under an agent key
    count only {self} ∪ permitted."""
    from sqlalchemy import update
    from db.engine import get_engine
    from db.tables import operator_queue
    me, peer, stranger, own, theirs, alien = _fleet(qdb, names, strangers=2)
    with get_engine().begin() as conn:
        for uid in (own[0], alien[0]):
            conn.execute(update(operator_queue).where(operator_queue.c.id == uid)
                         .values(delivery_state="undelivered"))
        for uid in (theirs[0], alien[1]):
            conn.execute(update(operator_queue).where(operator_queue.c.id == uid)
                         .values(sync_state="closed_by_filer"))
    _as(mcp_scope="agent", agent_name=me)
    body = _get(limit=10)
    assert (body["undelivered_count"], body["closed_by_filer_count"]) == (1, 1)


def test_b12_agent_names_only_ever_narrows(qdb, names):
    """B12 (D1): `agent_names` intersects with what the caller may see; it never
    widens. Bounded (≤500, no blank) and applied to the page, total and flags."""
    from sqlalchemy import update
    from db.engine import get_engine
    from db.tables import operator_queue
    me, peer, stranger, own, theirs, alien = _fleet(qdb, names, strangers=4)
    with get_engine().begin() as conn:
        conn.execute(update(operator_queue).where(operator_queue.c.id == theirs[0])
                     .values(delivery_state="undelivered"))

    _as()
    person = _get(limit=50, agent_names=[stranger])
    assert {i["id"] for i in person["items"]} == set(alien)
    assert person["total"] == len(alien)
    assert person["undelivered_count"] == 0

    _as(mcp_scope="agent", agent_name=me)
    agent = _get(limit=50, agent_names=[me, stranger])
    assert {i["id"] for i in agent["items"]} == set(own)
    assert agent["total"] == len(own)

    both = _get(limit=50, agent_names=[me], agent_name=peer)
    assert (both["items"], both["total"]) == ([], 0)
    peer_only = _get(limit=50, agent_names=[peer, me], agent_name=peer)
    assert {i["id"] for i in peer_only["items"]} == set(theirs)
    assert peer_only["undelivered_count"] == 1

    too_many = _client().get("/api/operator-queue",
                             params={"agent_names": [f"a{i}" for i in range(501)]})
    assert too_many.status_code == 422 and "agent_names" in too_many.text
    blank = _client().get("/api/operator-queue", params={"agent_names": [me, " "]})
    assert blank.status_code == 422 and "agent_names" in blank.text


# --- B15: GET /api/agents/{name}/permissions?strict=true ---------------------

_PERM_APP = {}


def _perm_client(monkeypatch, db, *, states, me, peer, stranger):
    """The real agent_files router; Docker and the DB access rules stubbed on
    the globals the permissions logic actually reads (module-identity safe)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import routers.agent_files as route_mod
    from models import User

    g = route_mod.get_agent_permissions_logic.__globals__
    helpers_g = g["get_accessible_agents"].__globals__
    perm_db = g["db"]

    def _states():
        if isinstance(states, Exception):
            raise states
        return states

    def _boom(*a, **k):
        raise AssertionError("strict mode must not call a fail-silent Docker helper")

    monkeypatch.setitem(g, "agent_container_states", _states)
    monkeypatch.setitem(g, "get_agent_container", _boom)
    monkeypatch.setitem(helpers_g, "list_all_agents_fast", _boom)
    monkeypatch.setattr(perm_db, "can_user_access_agent", lambda u, a: True)
    monkeypatch.setattr(perm_db, "get_user_by_username",
                        lambda u: {"role": "user", "email": "op-815@example.com"})
    monkeypatch.setattr(perm_db, "get_all_agent_metadata", lambda email: {
        n: {"owner_username": "op-815", "is_shared_with_user": False}
        for n in (me, peer, stranger)})

    app = FastAPI()
    app.include_router(route_mod.router)
    human = User(id=7, username="op-815", email="op-815@example.com", role="user")
    for route in app.routes:
        for dep in getattr(getattr(route, "dependant", None), "dependencies", []) or []:
            if getattr(dep.call, "__name__", "") == "get_current_user":
                app.dependency_overrides[dep.call] = lambda: human
    return TestClient(app), g, helpers_g


def test_b15_strict_permissions_never_answer_a_docker_fault_with_no_peers(
        qdb, names, monkeypatch):
    """B15 (D1): without `strict`, a Docker fault inside `list_all_agents_fast`
    reads as "200, no peers" — and the MCP would call a self-only view
    complete. `strict=true` takes ONE tri-state snapshot: unreadable → 503;
    readable → the permitted peers that have a container, with the
    fail-silent helpers never called."""
    me, peer, stranger = names("me"), names("peer"), names("stranger")
    qdb.add_agent_permission(me, peer, "op-815")
    snapshot = {me: "running", peer: "stopped", stranger: "running"}

    client, g, helpers_g = _perm_client(monkeypatch, qdb, states=None,
                                        me=me, peer=peer, stranger=stranger)
    res = client.get(f"/api/agents/{me}/permissions", params={"strict": "true"})
    assert res.status_code == 503, res.text

    client, g, helpers_g = _perm_client(monkeypatch, qdb, states=snapshot,
                                        me=me, peer=peer, stranger=stranger)
    res = client.get(f"/api/agents/{me}/permissions", params={"strict": "true"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert [a["name"] for a in body["permitted_agents"]] == [peer]
    assert body["permitted_agents"][0]["status"] == "stopped"
    assert {a["name"] for a in body["available_agents"]} == {peer, stranger}

    missing = client.get(f"/api/agents/{names('ghost')}/permissions",
                         params={"strict": "true"})
    assert missing.status_code == 404

    # Without the flag: today's lenient behaviour, byte for byte — a container
    # lookup, then the fleet list; a fleet-list fault is "200 with no peers".
    monkeypatch.setitem(g, "get_agent_container", lambda name: object())
    monkeypatch.setitem(helpers_g, "list_all_agents_fast", lambda: [])
    monkeypatch.setitem(g, "agent_container_states",
                        lambda: (_ for _ in ()).throw(AssertionError("lenient read took a snapshot")))
    lenient = client.get(f"/api/agents/{me}/permissions")
    assert lenient.status_code == 200
    assert lenient.json()["permitted_agents"] == []
