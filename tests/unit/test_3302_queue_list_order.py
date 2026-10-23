"""#3302 — the offset-mode order the operator-queue UI reads past its first page.

The frontend no longer derives counts from one capped page
(`src/frontend/src/utils/operatorQueueRead.js`). Two of its reads rest on the
order `GET /api/operator-queue` returns WITHOUT a cursor, and nothing pinned
that order directly, so these tests do:

1. every pending row precedes every ended row, whatever their priorities and
   timestamps — so a page that reaches an ended row holds every pending row;
2. the rows after the pending section are the ended rows, newest ENDING first —
   so an offset of the pending count reads the latest ended window;
3. `total` counts the page's own WHERE (filters, not the page), and the two
   flags are the same whatever `status` / `limit` / `offset` the read carries —
   so the reader takes them from any one page.

A cursor walk orders differently (a row that ended within the walk's margin
sorts with the pending ones — `test_ent815_queue_walk.py::test_k9`), which is
why the UI only ever walks `status=pending`.

Harness: the real router in a small FastAPI app over a fresh SQLite file per
test (the trinity-enterprise#815 harness).

Related flow: docs/memory/feature-flows/operating-room.md
"""

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("sqlalchemy")

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

pytestmark = pytest.mark.unit

AGENT = "order-3302"
NOW = datetime.now(timezone.utc)


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _ago(seconds):
    return _iso(NOW - timedelta(seconds=seconds))


@pytest.fixture()
def qdb(tmp_path):
    import sqlite3
    import db.connection as conn_mod
    from db.schema import init_schema
    db_file = tmp_path / f"order-{uuid.uuid4().hex[:8]}.db"
    old_env, old_path = os.environ.get("TRINITY_DB_PATH"), conn_mod.DB_PATH
    os.environ["TRINITY_DB_PATH"] = str(db_file)
    conn_mod.DB_PATH = str(db_file)
    raw = sqlite3.connect(db_file)
    init_schema(raw.cursor(), raw)
    raw.commit()
    raw.close()
    try:
        from database import db
        yield db
    finally:
        if old_env is None:
            os.environ.pop("TRINITY_DB_PATH", None)
        else:
            os.environ["TRINITY_DB_PATH"] = old_env
        conn_mod.DB_PATH = old_path


_PRINCIPAL = {"user": None}


def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
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
    assert found
    for call in found:
        app.dependency_overrides[call] = lambda: _PRINCIPAL["user"]
    return TestClient(app, raise_server_exceptions=True)


def _as_admin():
    from models import User
    _PRINCIPAL["user"] = User(id=7, username="op-3302", email="op-3302@example.com", role="admin")


def _update(uid, **values):
    from sqlalchemy import update
    from db.engine import get_engine
    from db.tables import operator_queue
    with get_engine().begin() as conn:
        conn.execute(update(operator_queue).where(operator_queue.c.id == uid).values(**values))


def _seed(db, *, priority="medium", created_at, status=None, ended_at=None, **extra):
    uid = db.create_operator_queue_item(AGENT, {
        "id": f"r-{uuid.uuid4().hex[:12]}", "type": "question",
        "priority": priority, "title": "t", "question": "q", "created_at": created_at,
    })
    values = dict(extra)
    if status:
        values["status"] = status
        values["disposed_at"] = ended_at
        if status == "responded":
            values["responded_at"] = ended_at
    if values:
        _update(uid, **values)
    return uid


@pytest.fixture()
def queue(qdb):
    """Pending rows at every priority, filed both long before and long after the
    ended rows; ended rows of all four ended statuses, one ended seconds ago
    (inside a walk's margin — irrelevant to offset mode, which is the point)."""
    pending, ended = [], []
    for i, prio in enumerate(("low", "critical", "medium", "high", "low", "critical")):
        filed = _ago(10 * 86400) if i % 2 else _ago(60 + i)  # very old and very new
        pending.append(_seed(qdb, priority=prio, created_at=filed))
    for i, status in enumerate(("responded", "acknowledged", "cancelled", "expired",
                                "cancelled", "responded")):
        ended.append(_seed(qdb, priority="critical", created_at=_ago(30 * 86400 + i),
                           status=status, ended_at=_ago(5 + i * 3600)))
    _as_admin()
    return {"pending": pending, "ended": ended}


def _get(**params):
    res = _client().get("/api/operator-queue", params=params)
    assert res.status_code == 200, res.text
    return res.json()


def test_every_pending_row_precedes_every_ended_row(queue):
    statuses = [i["status"] for i in _get(limit=500)["items"]]
    assert len(statuses) == 12
    first_ended = next(n for n, s in enumerate(statuses) if s != "pending")
    assert statuses[:first_ended] == ["pending"] * 6
    assert all(s != "pending" for s in statuses[first_ended:])


def test_offset_pages_concatenate_to_the_same_order(queue):
    whole = [i["id"] for i in _get(limit=500)["items"]]
    paged = []
    for offset in range(0, 12, 5):
        paged += [i["id"] for i in _get(limit=5, offset=offset)["items"]]
    assert paged == whole


def test_rows_after_the_pending_section_are_the_ended_rows_newest_ending_first(queue):
    after = _get(limit=500, offset=len(queue["pending"]))["items"]
    assert [i["id"] for i in after] == queue["ended"]  # seeded newest-ending first
    ended_at = [i["disposed_at"] for i in after]
    assert ended_at == sorted(ended_at, reverse=True)


def test_total_counts_the_where_not_the_page(queue):
    assert _get(status="pending", limit=1)["total"] == 6
    assert _get(status="pending", priority="critical", limit=1)["total"] == 2
    assert _get(limit=1)["total"] == 12
    assert _get(limit=3, offset=9)["total"] == 12


def test_the_flags_ignore_status_limit_and_offset(qdb, queue):
    _update(queue["pending"][0], sync_state="closed_by_filer")
    _update(queue["ended"][0], delivery_state="undelivered")
    reads = [_get(limit=500), _get(status="pending", limit=1), _get(limit=2, offset=8),
             _get(status="responded", limit=1)]
    flags = {(r["undelivered_count"], r["closed_by_filer_count"]) for r in reads}
    assert flags == {(1, 1)}
