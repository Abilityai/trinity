"""trinity-enterprise#815 — the keyset (cursor) walk over `GET /api/operator-queue`.

Offset paging can only promise "no repeat, no skip" while the queue stands
still, and the queue never does. A walk can: `cursor=start`, then each
response's `next_cursor`. Within one walk no `id` is returned twice, and every
row that matches when the walk starts and still matches when the walk reaches
it is returned exactly once — under concurrent inserts, endings and platform
alert priority changes.

Two writes would move a row's sort key mid-walk, and each has its fix:
  * pending → ended (five writers, plus an agent's replace and the expire-now
    beside it, #3247 — K5). The walk sorts "as of" a watermark
    `W = start − 300 s`: a row ended after `W` keeps its pending-section key
    for the whole walk. The bound: a write must commit within the margin of
    the timestamp it stamped; a breach is logged at error (`_note_commit_lag`).
  * a pending platform alert's priority (#3246 `_touch`). At `cursor=start`
    the alerts' priorities are snapshotted in Redis under a walk id the token
    carries (TTL 1 h); the walk orders them by the snapshot. An expired walk
    is a 410, never a silent restart.

Harness: the ent#715 router harness over a fresh SQLite file per test, and a
fakeredis client patched onto the service's Redis accessor.

Related flow: docs/memory/feature-flows/operating-room.md
"""

import base64
import contextlib
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("sqlalchemy")
fakeredis = pytest.importorskip("fakeredis")

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

pytestmark = pytest.mark.unit

T0 = "2026-10-01T10:00:00.000000Z"
OP = "op-815@example.com"
PRIOS = ("critical", "high", "medium", "low")


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _ago(seconds):
    return _iso(datetime.now(timezone.utc) - timedelta(seconds=seconds))


@contextlib.contextmanager
def _fresh_db(dirpath):
    """A fresh SQLite file, usable inside a hypothesis example (no fixtures)."""
    import sqlite3
    import db.connection as conn_mod
    from db.schema import init_schema
    db_file = dirpath / f"walk-{uuid.uuid4().hex[:8]}.db"
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


@pytest.fixture()
def qdb(tmp_path):
    with _fresh_db(tmp_path) as db:
        yield db


@pytest.fixture(autouse=True)
def walk_redis(monkeypatch):
    """The walk's priority snapshot lives in Redis; a fakeredis stands in,
    patched on the service module the route actually calls."""
    from routers import operator_queue as route
    r = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setitem(route.operator_queue_service.list_for_principal.__globals__,
                        "get_breaker_redis", lambda: r)
    return r


# ---------------------------------------------------------------------------
# The router harness (ent#715).
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
        assert found
        for call in found:
            app.dependency_overrides[call] = lambda: _PRINCIPAL["user"]
        _APP = app
    return TestClient(_APP, raise_server_exceptions=True)


def _as(**principal):
    from models import User
    base = {"id": 7, "username": "op-815", "email": OP, "role": "admin"}
    base.update(principal)
    _PRINCIPAL["user"] = User(**base)


def _seed(db, agent, *, priority="medium", created_at=T0, status=None, rid=None, **extra):
    item = {"id": rid or f"r-{uuid.uuid4().hex[:12]}", "type": "question",
            "priority": priority, "title": "t", "question": "q"}
    if created_at:
        item["created_at"] = created_at
    if status:
        item["status"] = status
    uid = db.create_operator_queue_item(agent, item)
    if extra:
        _update(uid, **extra)
    return uid


def _update(uid, **values):
    from sqlalchemy import update
    from db.engine import get_engine
    from db.tables import operator_queue
    with get_engine().begin() as conn:
        conn.execute(update(operator_queue).where(operator_queue.c.id == uid).values(**values))


def _insert_raw(**values):
    from db.engine import get_engine
    from db.tables import operator_queue
    row = {"id": uuid.uuid4().hex, "title": "t", "question": "q"}
    row.update(values)
    with get_engine().begin() as conn:
        conn.execute(operator_queue.insert().values(**row))
    return row["id"]


def _alert(db, agent, subject, priority):
    out = db.create_platform_operator_queue_item(agent, {
        "id": f"alert-{uuid.uuid4().hex[:10]}", "type": "alert", "priority": priority,
        "title": "condition", "question": "q"}, subject=subject)
    return out["row"]["id"]


def _get(expect=200, **params):
    res = _client().get("/api/operator-queue", params=params)
    assert res.status_code == expect, res.text
    return res.json()


def _walk(limit, between=None, params_for=None, **params):
    """Walk `cursor=start` → `next_cursor`. `between(n, body)` runs after page n
    (the concurrent writes); `params_for(n)` re-derives per-page parameters."""
    ids, pages, cursor = [], [], "start"
    for n in range(500):
        extra = params_for(n) if params_for else {}
        body = _get(limit=limit, cursor=cursor, **params, **extra)
        assert "next_cursor" in body, sorted(body)
        pages.append(body)
        ids += [i["id"] for i in body["items"]]
        if not body["has_more"]:
            assert body["next_cursor"] is None
            return ids, pages
        assert body["next_cursor"] and body["next_offset"] is None
        cursor = body["next_cursor"]
        if between:
            between(n, body)
    raise AssertionError("walk did not end")


def _token(d):
    return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()


def _untoken(t):
    return json.loads(base64.urlsafe_b64decode(t + "=" * (-len(t) % 4)))


# ---------------------------------------------------------------------------
# The five pending → ended writers.
# ---------------------------------------------------------------------------

def _end_respond(db, ids):
    for i in ids:
        db.respond_to_operator_queue_item(i, "yes", None, "7", OP)


def _end_cancel(db, ids):
    for i in ids:
        db.cancel_operator_queue_item(i, disposed_by_email=OP)


def _end_bulk(db, ids):
    db.bulk_cancel_operator_queue_items(list(ids), None, disposed_by_email=OP)


def _end_platform(db, ids):
    db.end_operator_queue_items_by_platform(list(ids), reason="condition cleared")


def _end_expire(db, ids):
    for i in ids:
        _update(i, expires_at=_ago(30))
    db.mark_operator_queue_expired()


WRITERS = {
    "respond": (_end_respond, "responded"),
    "cancel": (_end_cancel, "cancelled"),
    "bulk_cancel": (_end_bulk, "cancelled"),
    "platform_end": (_end_platform, "cancelled"),
    "expire": (_end_expire, "expired"),
}


# ---------------------------------------------------------------------------
# K1–K12
# ---------------------------------------------------------------------------

def test_k1_inserts_between_pages(qdb):
    """K1 (D2, inserts): rows inserted behind the cursor are never returned,
    rows inserted ahead of it once (a file-seam row with an old agent-supplied
    `created_at` included); every original row exactly once."""
    a = "k1-agent"
    originals = []
    counts = {"critical": 6, "high": 6, "medium": 6, "low": 5}
    for p, n in counts.items():
        for i in range(n):
            originals.append(_seed(qdb, a, priority=p,
                                   created_at=T0 if i % 2 else f"2026-10-0{1 + i % 3}T09:00:00.000000Z"))
    for i in range(5):
        originals.append(_seed(qdb, a, priority="high", status="responded",
                               disposed_at=f"2026-09-2{i}T10:00:00.000000Z"))
    behind, ahead = [], []
    _as()

    def between(n, body):
        last = body["items"][-1]["priority"]
        if n == 0:
            assert last == "high", last
            behind.append(_seed(qdb, a, priority="critical", created_at=None))
            ahead.append(_seed(qdb, a, priority="low", created_at=None))
            ahead.append(_seed(qdb, a, priority="medium", created_at="2020-01-01T00:00:00.000000Z"))
        elif n == 1:
            assert last == "medium", last
            behind.append(_seed(qdb, a, priority="high", created_at=None))
            ahead.append(_seed(qdb, a, priority="low", created_at=None))

    ids, pages = _walk(7, between=between)
    assert len(ids) == len(set(ids)), "an id was returned twice"
    assert set(originals) <= set(ids)
    assert set(ahead) <= set(ids)
    assert not set(behind) & set(ids)


@pytest.mark.parametrize("writer", sorted(WRITERS))
def test_k2_status_changes_between_pages(qdb, writer):
    """K2 (D2, status changes): an unfiltered walk, each of the five writers.
    A row ended after it was returned is not returned again; a row ended before
    the walk reached it is returned once, carrying its new status; an ended
    row acknowledged mid-walk does not move."""
    end, ended_status = WRITERS[writer]
    a = "k2-agent"
    pending = [_seed(qdb, a, priority=PRIOS[i % 4], created_at=_ago(3600 + i)) for i in range(20)]
    ended = [_seed(qdb, a, status="responded", disposed_at=_ago(86400 + i), responded_at=_ago(86400 + i),
                   delivery_state="delivered", rid=f"done-{i}") for i in range(3)]
    marks = {}
    _as()

    def between(n, body):
        if n:
            return
        seen = {i["id"] for i in body["items"]}
        marks["returned"] = body["items"][0]["id"]
        marks["ahead"] = next(i for i in reversed(pending) if i not in seen)
        end(qdb, [marks["returned"], marks["ahead"]])
        assert qdb.mark_operator_queue_acknowledged(a, "done-1")

    ids, pages = _walk(5, between=between)
    assert len(ids) == len(set(ids)), "an id was returned twice"
    assert set(ids) == set(pending) | set(ended)
    rows = {i["id"]: i for p in pages for i in p["items"]}
    assert rows[marks["ahead"]]["status"] == ended_status


def test_k3_pending_walk_with_answers_between_pages(qdb):
    """K3 (the MCP's use): a `status=pending` walk. Rows answered ahead of the
    cursor left the filter and are not returned; every row still pending is
    returned exactly once."""
    a = "k3-agent"
    pending = [_seed(qdb, a, priority=PRIOS[i % 4], created_at=_ago(3600 + i)) for i in range(20)]
    answered = []
    _as()

    def between(n, body):
        if n:
            return
        seen = {i["id"] for i in body["items"]}
        answered.extend([body["items"][1]["id"]] + [i for i in reversed(pending) if i not in seen][:2])
        _end_respond(qdb, answered)

    ids, _ = _walk(5, between=between, status="pending")
    assert len(ids) == len(set(ids))
    still_pending = set(pending) - set(answered)
    assert still_pending <= set(ids)
    assert not (set(answered[1:]) & set(ids)), "a row answered ahead of the cursor was returned"


def test_k5_commit_lag_inside_the_margin_never_repeats(qdb):
    """K5 (the bound): a row ended with a timestamp 60 s BEFORE page 1 was
    read — a write in flight when the walk began, committed after page 1 — is
    still ordered as pending by this walk, so it is not returned again."""
    a = "k5-agent"
    rows = [_seed(qdb, a, priority=PRIOS[i % 4], created_at=_ago(3600 + i)) for i in range(12)]
    started = datetime.now(timezone.utc)
    _as()
    marks = {}

    def between(n, body):
        if n:
            return
        r = body["items"][0]["id"]
        marks["r"] = r
        stamp = _iso(started - timedelta(seconds=60))
        _update(r, status="responded", disposition="answered", responded_at=stamp, disposed_at=stamp)

    ids, _ = _walk(4, between=between)
    assert ids.count(marks["r"]) == 1, "a row ended inside the margin was returned twice"
    assert set(ids) == set(rows)


def test_k5_a_commit_past_the_margin_is_logged_at_error(qdb, monkeypatch, caplog):
    """K5 (loud, not silent): a pending → ended write that commits more than
    the margin after the timestamp it stamped logs at error; within the margin
    it logs nothing."""
    a = "k5-lag"
    late, prompt = _seed(qdb, a), _seed(qdb, a)
    ops = type(qdb._operator_queue_ops)
    g = ops.respond_to_item.__globals__
    caplog.set_level(logging.ERROR)

    monkeypatch.setitem(g, "utc_now_iso", lambda: _ago(10))
    qdb.respond_to_operator_queue_item(prompt, "yes", None, "7", OP)
    assert not [r for r in caplog.records if "ent#815" in r.getMessage()]

    monkeypatch.setitem(g, "utc_now_iso", lambda: _ago(301))
    qdb.respond_to_operator_queue_item(late, "yes", None, "7", OP)
    errors = [r for r in caplog.records if r.levelno == logging.ERROR and "ent#815" in r.getMessage()]
    assert errors, [r.getMessage() for r in caplog.records]
    assert "cursor walks" in errors[0].getMessage()


@pytest.mark.parametrize("past_deadline, ended_as", [(False, "cancelled"), (True, "expired")])
def test_k5_a_replace_that_commits_past_the_margin_is_logged_at_error(
        qdb, monkeypatch, caplog, past_deadline, ended_as):
    """K5, the sixth and seventh writers (#3247): an agent's replace ends its
    predecessor by compare-and-set, and a predecessor already past its deadline
    is expired in that same transaction. Both stamp `disposed_at`, so both are
    held to the margin like the five above."""
    a = "k5-replace"
    ops = type(qdb._operator_queue_ops)
    g = ops.create_native_item.__globals__
    caplog.set_level(logging.ERROR)

    def replace(rid, stamp_age):
        old = qdb.create_native_operator_queue_item(
            a, {"id": f"old-{rid}", "type": "approval", "title": "t", "question": "q"},
            max_pending=None, channel="mcp", raised_by="agent", to_role=None,
            resolved_to=None, proposal=None, supersedes_expired=None)["row"]
        if past_deadline:
            _update(old["id"], expires_at=_ago(3600))
        monkeypatch.setitem(g, "utc_now_iso", lambda: _ago(stamp_age))
        out = qdb.create_native_operator_queue_item(
            a, {"id": f"new-{rid}", "type": "approval", "title": "t", "question": "q"},
            max_pending=None, channel="mcp", raised_by="agent", to_role=None,
            resolved_to=None, proposal=None, supersedes_expired=None, replaces=old["id"])
        monkeypatch.setitem(g, "utc_now_iso", utc_now_iso)
        assert qdb.get_operator_queue_item(old["id"])["status"] == ended_as, out
        return [r for r in caplog.records if r.levelno == logging.ERROR and "ent#815" in r.getMessage()]

    from utils.helpers import utc_now_iso
    assert not replace("prompt", 10)
    assert replace("late", 301)


def _valid_token(qdb, **params):
    for i in range(6):
        _seed(qdb, "k6-agent", priority=PRIOS[i % 4], created_at=_ago(100 + i))
    _as()
    body = _get(limit=2, cursor="start", **params)
    assert body["next_cursor"]
    return body["next_cursor"]


def test_k6_cursor_validation_is_strict(qdb):
    """K6 (opaque, strict): every malformed token is a 422 naming `cursor`,
    never a silent restart. `cursor` with a non-zero `offset` is a 422 naming
    both; a token used with different filters is refused; a different `limit`
    is fine."""
    tok = _valid_token(qdb, status="pending")
    good = _untoken(tok)

    def bad(**changes):
        d = json.loads(json.dumps(good))
        for k, v in changes.items():
            if k == "k":
                d["k"] = v
            elif v is None:
                d.pop(k)
            else:
                d[k] = v
        return _token(d)

    k = good["k"]

    def oversized():
        """A token whose ONLY fault is size: the valid token's JSON padded with
        insignificant whitespace to one byte over the cap, still inside the
        base64 length precheck, so only the decoded-bytes check can refuse it."""
        from services.operator_queue_service import CURSOR_MAX_BYTES
        body = json.dumps(good, separators=(",", ":"))
        raw = body[:-1] + " " * (CURSOR_MAX_BYTES + 1 - len(body)) + "}"
        assert len(raw) == CURSOR_MAX_BYTES + 1 and json.loads(raw) == good
        t = base64.urlsafe_b64encode(raw.encode()).rstrip(b"=").decode()
        assert len(t) <= (CURSOR_MAX_BYTES * 4 + 2) // 3 + 4
        return t

    cases = {
        "not base64url": "!!not*base64!!",
        "not an object": _token([1, 2]),
        "an extra key": bad(x=1),
        "a missing key": bad(f=None),
        "v=2": bad(v=2),
        "a bad watermark": bad(w="yesterday"),
        "sec=2": bad(k=[2, k[1], k[2], k[3]]),
        "prk=5": bad(k=[k[0], 5, k[2], k[3]]),
        "a 1,025-char st": bad(k=[k[0], k[1], "x" * 1025, k[3]]),
        "a NUL in st": bad(k=[k[0], k[1], "\u0000", k[3]]),
        "a lone surrogate in id": bad(k=[k[0], k[1], k[2], "\ud800"]),
        "over 4,096 bytes": oversized(),
        "a bad walk id": bad(s="not-hex"),
    }
    for label, cursor in cases.items():
        res = _client().get("/api/operator-queue", params={"cursor": cursor, "limit": 2, "status": "pending"})
        assert res.status_code == 422, (label, res.text)
        assert "cursor" in res.text, (label, res.text)

    both = _client().get("/api/operator-queue", params={"cursor": tok, "offset": 3, "status": "pending"})
    assert both.status_code == 422 and "cursor" in both.text and "offset" in both.text
    other = _client().get("/api/operator-queue", params={"cursor": tok, "status": "responded"})
    assert other.status_code == 422 and "does not match" in other.text
    assert _client().get("/api/operator-queue",
                         params={"cursor": tok, "limit": 3, "status": "pending"}).status_code == 200


def test_k6_an_expired_walk_is_a_410(qdb, walk_redis):
    """Option B: the snapshot expires with the walk (TTL 1 h). A token whose
    walk is gone is a 410 telling the caller to restart — never a silent
    restart that would re-return rows."""
    tok = _valid_token(qdb)
    keys = walk_redis.keys("*")
    assert keys and all(0 < walk_redis.ttl(k) <= 3600 for k in keys)
    walk_redis.flushall()
    res = _client().get("/api/operator-queue", params={"cursor": tok, "limit": 2})
    assert res.status_code == 410, res.text
    assert "cursor=start" in res.text


def test_k7_a_forged_token_never_widens(qdb):
    """K7: the token carries no authority — every page re-applies visibility.
    A token positioned anywhere still yields only {self} ∪ permitted rows."""
    me, peer, stranger = "k7-me", "k7-peer", "k7-stranger"
    qdb.add_agent_permission(me, peer, "op-815")
    mine = {_seed(qdb, a, priority=p) for a in (me, peer) for p in PRIOS}
    for p in PRIOS * 3:
        _seed(qdb, stranger, priority=p)
    _as(mcp_scope="agent", agent_name=me)
    start = _get(limit=2, cursor="start")
    d = _untoken(start["next_cursor"])
    for key in ([0, 0, "9999", ""], [1, 0, "9999", ""], [0, 4, "", "~"]):
        d["k"] = key
        body = _get(limit=50, cursor=_token(d))
        assert {i["agent_name"] for i in body["items"]} <= {me, peer}
        assert body["total"] == len(mine)


def test_k8_an_oversized_key_fails_loud(qdb):
    """K8: a legacy row whose agent-authored id is longer than a cursor may
    carry sits at the page boundary. The page says it is incomplete and how to
    continue — `has_more` true, `next_cursor` null, a warning naming the id —
    never a truncated key or a quiet "complete"."""
    a = "k8-agent"
    for i in range(2):
        _seed(qdb, a, priority="critical", created_at=_ago(100 + i))
    big = "L" * 1500
    _insert_raw(id=big, agent_name=a, request_id="legacy-1", status="pending",
                priority="high", type="question", created_at=_ago(50))
    for i in range(3):
        _seed(qdb, a, priority="medium", created_at=_ago(200 + i))
    _as()
    body = _get(limit=3, cursor="start")
    assert body["items"][-1]["id"] == big
    assert body["has_more"] is True
    assert body["next_cursor"] is None
    assert any(big[:32] in w for w in body.get("warnings", [])), body.get("warnings")


# Keys within the per-field 1,024-character limit whose JSON escaping still
# overflows the 4,096-byte token: a non-ASCII character is six bytes (`\u00e9`),
# a quote or a backslash two.
_ESCAPE_HEAVY = [
    pytest.param(chr(0xE9) * 700, None, id="700-e-acute"),
    pytest.param("\\" * 1024, '"' * 1024, id="quotes-and-backslashes"),
]


@pytest.mark.parametrize("big,created_at", _ESCAPE_HEAVY)
def test_k8b_an_escape_heavy_key_fails_loud_instead_of_issuing_a_dead_cursor(
        qdb, big, created_at):
    """K8b (T2): a boundary key that passes the per-field check but whose
    escaped JSON is over `CURSOR_MAX_BYTES` used to be issued — and the next
    request was a 422 "not a token this server issued". Now the page fails
    loud exactly as K8 does: `has_more` true, `next_cursor` null, a warning."""
    a = "k8b-agent"
    for i in range(2):
        _seed(qdb, a, priority="critical", created_at=_ago(100 + i))
    _insert_raw(id=big, agent_name=a, request_id="legacy-1", status="pending",
                priority="high", type="question", created_at=created_at or _ago(50))
    for i in range(3):
        _seed(qdb, a, priority="medium", created_at=_ago(200 + i))
    _as()
    body = _get(limit=3, cursor="start")
    assert body["items"][-1]["id"] == big
    assert body["has_more"] is True
    assert body["next_cursor"] is None, "issued a cursor its own decoder refuses"
    assert any(big[:32] in w for w in body.get("warnings", [])), body.get("warnings")


@pytest.mark.parametrize("big,created_at", _ESCAPE_HEAVY)
def test_k8c_every_issued_cursor_fits_its_decoder(big, created_at):
    """K8c (T2, the codec): `encode_cursor` returns None for a key whose token
    would exceed the decoder's byte cap, and a token it does issue at the
    per-field limit (1,024 ASCII characters) is one `decode_cursor` accepts."""
    from services.operator_queue_service import decode_cursor, encode_cursor
    walk, fp = "a" * 32, "f" * 16
    assert encode_cursor(T0, (0, 1, created_at or T0, big), fp, walk) is None
    ok = encode_cursor(T0, (0, 1, T0, "L" * 1024), fp, walk)
    assert ok is not None
    assert decode_cursor(ok)["after"] == (0, 1, T0, "L" * 1024)


def test_k9_offset_mode_order_is_unchanged(qdb):
    """K9 (D2 is additive): with no `cursor` the page order is exactly
    `list_items`' order — a row answered 60 s ago is in the ended section —
    and `next_cursor` is null. With `cursor=start` the same row is ordered as
    pending (the documented margin)."""
    a = "k9-agent"
    for i in range(5):
        _seed(qdb, a, priority=PRIOS[i % 4], created_at=_ago(3600 + i))
    recent = _seed(qdb, a, priority="critical", created_at=_ago(1000))
    _end_respond(qdb, [recent])
    _update(recent, disposed_at=_ago(60), responded_at=_ago(60))
    _as()
    offset_page = _get(limit=50)
    assert [i["id"] for i in offset_page["items"]] == [
        i["id"] for i in qdb.list_operator_queue_items(limit=50)]
    assert offset_page["items"][-1]["id"] == recent
    assert offset_page["next_cursor"] is None
    walk_page = _get(limit=50, cursor="start")
    assert walk_page["items"][0]["id"] == recent


@pytest.mark.parametrize("dialect", ["postgresql", "sqlite"])
def test_k10_walk_statement_is_portable_and_shares_its_expressions(dialect):
    """K10 (parity): the predicate, the ORDER BY and the read-back columns are
    built from the SAME expression objects (so they cannot disagree on
    collation or NULLs) and compile with no dialect-only function."""
    from sqlalchemy.dialects import postgresql, sqlite
    from db.operator_queue import OperatorQueueOperations
    stmt, (sec, prk, st) = OperatorQueueOperations()._walk_query(
        watermark=T0, snapshot={"a" * 32: 1}, after=(0, 1, T0, "x"), limit=8,
        status="pending")
    order = list(stmt._order_by_clauses)
    assert order[0] is sec and order[1] is prk and order[2].element is st
    cols = stmt.selected_columns
    assert cols["_k_sec"].element is sec
    assert cols["_k_prk"].element is prk
    assert cols["_k_st"].element is st
    d = postgresql.dialect() if dialect == "postgresql" else sqlite.dialect()
    sql = str(stmt.compile(dialect=d)).lower()
    assert "case" in sql and "coalesce" in sql
    for dialect_only in ("nulls first", "nulls last", "ifnull", "julianday", "datetime("):
        assert dialect_only not in sql, dialect_only


def test_k11_edge_keys_are_each_returned_once(qdb):
    """K11: the edge values a sort key can hold — an empty `created_at`, a
    priority outside the vocabulary (rank 4), a status outside it (the ended
    section) — are each returned exactly once. (NULL is not reachable: the
    DDL declares `status`, `priority` and `created_at` NOT NULL; the walk's
    `coalesce(…, '')` stays as the defence `db/tables.py`'s nullable columns
    call for.)"""
    a = "k11-agent"
    edges = [
        _insert_raw(agent_name=a, request_id="e1", status="pending", priority="high",
                    type="question", created_at=""),
        _insert_raw(agent_name=a, request_id="e2", status="pending", priority="urgent",
                    type="question", created_at=_ago(10)),
        _insert_raw(agent_name=a, request_id="e3", status="weird", priority="low",
                    type="question", created_at=_ago(20)),
        _insert_raw(agent_name=a, request_id="e4", status="responded", priority="low",
                    type="question", created_at=""),
    ]
    others = [_seed(qdb, a, priority=PRIOS[i % 4], created_at=_ago(100 + i)) for i in range(8)]
    _as()
    ids, _ = _walk(2)
    assert len(ids) == len(set(ids))
    assert set(ids) == set(edges) | set(others)


def test_k12_a_permission_change_mid_walk(qdb):
    """K12 (D2 + D1): the MCP re-reads permits each page and re-sends
    `agent_names`. A revoke mid-walk is no 422 (agent_names is not part of the
    cursor's fingerprint), no repeat, and the revoked peer's rows stop."""
    me, p1, p2 = "k12-me", "k12-p1", "k12-p2"
    for p in (p1, p2):
        qdb.add_agent_permission(me, p, "op-815")
    for i in range(18):
        _seed(qdb, (me, p1, p2)[i % 3], priority=PRIOS[i % 4], created_at=_ago(100 + i))
    _as(mcp_scope="agent", agent_name=me)
    state = {"revoked": False}

    def params_for(n):
        names = [me] + qdb.get_permitted_agents(me)
        return {"agent_names": names}

    def between(n, body):
        if n == 0:
            qdb.remove_agent_permission(me, p2)
            state["revoked"] = True

    ids, pages = _walk(4, between=between, params_for=params_for)
    assert len(ids) == len(set(ids))
    for page in pages[1:]:
        assert p2 not in {i["agent_name"] for i in page["items"]}


def test_k_platform_alert_priority_change_mid_walk(qdb):
    """Option B: a pending platform alert whose priority changes mid-walk
    (#3246's `_touch`) is ordered by the walk's snapshot, so it is neither
    returned twice (lowered after it was returned) nor missed (raised past
    the cursor before it was returned)."""
    a = "kb-agent"
    lowered = _alert(qdb, a, "disk:a", "critical")
    raised = _alert(qdb, a, "disk:b", "low")
    for i in range(10):
        _seed(qdb, a, priority=PRIOS[i % 4], created_at=_ago(100 + i))
    _as()

    def between(n, body):
        if n == 0:
            assert lowered in {i["id"] for i in body["items"]}
            assert _alert(qdb, a, "disk:a", "low") == lowered
            assert _alert(qdb, a, "disk:b", "critical") == raised

    ids, _ = _walk(3, between=between)
    assert ids.count(lowered) == 1
    assert ids.count(raised) == 1


class _RaisingRedis:
    """A Redis client whose every walk call faults mid-call."""

    def __init__(self, where):
        self.where = where

    def _boom(self, *a, **k):
        raise ConnectionError(f"redis down ({self.where})")

    def hgetall(self, *a, **k):
        return self._boom()

    def pipeline(self):
        outer = self

        class _Pipe:
            def hset(self, *a, **k):
                if outer.where == "hset":
                    outer._boom()

            def expire(self, *a, **k):
                pass

            def execute(self):
                outer._boom()

        return _Pipe()


@pytest.mark.parametrize("fault", ["unreachable", "hset", "execute", "hgetall"])
def test_k_a_walk_without_redis_is_a_503_and_offset_paging_still_works(
        qdb, monkeypatch, fault):
    """A walk needs its priority snapshot in Redis. With the accessor returning
    None, or the client raising on `hset` / pipeline `execute` (the start) or on
    `hgetall` (a continuation), the walk answers 503 naming the offset fallback
    — never a 500, never a walk silently ordered without its snapshot — and
    offset paging (no cursor) still answers 200."""
    from routers import operator_queue as route
    tok = _valid_token(qdb)
    broken = None if fault == "unreachable" else _RaisingRedis(fault)
    monkeypatch.setitem(route.operator_queue_service.list_for_principal.__globals__,
                        "get_breaker_redis", lambda: broken)
    params = {"limit": 2}
    if fault != "hgetall":
        res = _client().get("/api/operator-queue", params={**params, "cursor": "start"})
        assert res.status_code == 503, res.text
        assert "offset" in res.text
    if fault in ("unreachable", "hgetall"):
        res = _client().get("/api/operator-queue", params={**params, "cursor": tok})
        assert res.status_code == 503, res.text
        assert "offset" in res.text
    body = _get(**params)
    assert body["count"] == 2 and body["has_more"] is True


@pytest.mark.parametrize("raised_at", ["critical", "high"])
def test_k_platform_alert_raised_mid_walk_ranks_last(qdb, raised_at):
    """Option B, the other arm: an alert raised AFTER `cursor=start` is not in
    the snapshot, so it ranks 4 — after every real priority, a fixed key. Were
    it ordered by its live priority instead, `critical` would land behind the
    cursor (missed) and `high` would be returned and then, lowered to `low` by
    `_touch`, land ahead of the cursor again (returned twice)."""
    a = "kc-agent"
    for i in range(10):
        _seed(qdb, a, priority=PRIOS[i % 4], created_at=_ago(100 + i))
    _as()
    state = {"id": None, "lowered": False}

    def between(n, body):
        if n == 0:
            assert {i["priority"] for i in body["items"]} == {"critical"}
            state["id"] = _alert(qdb, a, "disk:new", raised_at)
        elif state["id"] in {i["id"] for i in body["items"]} and not state["lowered"]:
            assert _alert(qdb, a, "disk:new", "low") == state["id"]
            state["lowered"] = True

    ids, _ = _walk(3, between=between)
    assert state["id"] is not None
    assert ids.count(state["id"]) == 1, ids
    assert len(ids) == 11


# ---------------------------------------------------------------------------
# K4 — the property: random interleavings of every write between pages.
# ---------------------------------------------------------------------------

from hypothesis import HealthCheck, given, settings, strategies as hst  # noqa: E402

_OPS = hst.lists(
    hst.tuples(
        hst.sampled_from([
            "insert", "insert_empty_time", "insert_old", "insert_alert",
            "end:respond", "end:cancel", "end:bulk_cancel", "end:platform_end", "end:expire",
            "ack", "clear", "delete", "touch", "revoke", "grant",
        ]),
        hst.integers(min_value=0, max_value=10_000),
    ),
    min_size=1, max_size=10,
)


@settings(derandomize=True, max_examples=30, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(ops=_OPS, agent_key=hst.booleans())
def test_k4_property_no_repeat_and_no_skip(tmp_path_factory, ops, agent_key):
    """K4: under random interleavings of inserts (empty, old and new
    `created_at`), the five end writers, acknowledge, clear, a retention
    delete, platform-alert priority changes and (agent key) a permission
    grant or revoke: no `id` is returned twice, and every row present at the
    walk's start and still matching at its end is returned exactly once."""
    from sqlalchemy import delete, select
    from db.engine import get_engine
    from db.tables import operator_queue

    with _fresh_db(tmp_path_factory.mktemp("k4")) as db:
        me, peer, other = "k4-me", "k4-peer", "k4-other"
        db.add_agent_permission(me, peer, "op-815")
        for i in range(14):
            agent = (me, peer, other)[i % 3]
            _seed(db, agent, priority=PRIOS[i % 4], created_at=T0 if i % 3 else _ago(500 + i))
        for i in range(4):
            _seed(db, (me, peer)[i % 2], status="responded", rid=f"done-{i}",
                  disposed_at=_ago(60 if i % 2 else 9000), responded_at=_ago(9000))
        alerts = {}
        for i, agent in enumerate((me, peer, other)):
            alerts[f"cond:{i}"] = agent
            _alert(db, agent, f"cond:{i}", PRIOS[i])
        state = {"revoked": False, "granted": False}

        def visible_agents():
            return {me, *db.get_permitted_agents(me)} if agent_key else None

        def matching():
            with get_engine().connect() as conn:
                rows = conn.execute(select(operator_queue.c.id, operator_queue.c.agent_name)
                                    .where(operator_queue.c.cleared_at.is_(None))).all()
            agents = visible_agents()
            return {r[0] for r in rows if agents is None or r[1] in agents}

        def rows_where(cond):
            with get_engine().connect() as conn:
                return sorted(r[0] for r in conn.execute(select(operator_queue.c.id).where(cond)).all())

        def apply(op, seed):
            agent = (me, peer, other)[seed % 3]
            pending = rows_where(operator_queue.c.status == "pending")
            if op == "insert":
                _seed(db, agent, priority=PRIOS[seed % 4], created_at=None)
            elif op == "insert_empty_time":
                _insert_raw(agent_name=agent, request_id=f"n-{seed}-{uuid.uuid4().hex[:6]}",
                            status="pending", priority=PRIOS[seed % 4], type="question",
                            created_at="")
            elif op == "insert_old":
                _seed(db, agent, priority=PRIOS[seed % 4], created_at="2020-01-01T00:00:00.000000Z")
            elif op == "insert_alert":
                _alert(db, agent, f"new:{seed}", PRIOS[seed % 4])
            elif op.startswith("end:") and pending:
                WRITERS[op[4:]][0](db, [pending[seed % len(pending)]])
            elif op == "ack":
                responded = rows_where(operator_queue.c.status == "responded")
                if responded:
                    rid = responded[seed % len(responded)]
                    _update(rid, delivery_state="delivered")
                    row = db.get_operator_queue_item(rid)
                    db.mark_operator_queue_acknowledged(row["agent_name"], row["request_id"])
            elif op == "clear":
                db.clear_resolved_operator_queue_items(agent_name=agent)
            elif op == "delete":
                every = rows_where(operator_queue.c.id.isnot(None))
                if every:
                    with get_engine().begin() as conn:
                        conn.execute(delete(operator_queue)
                                     .where(operator_queue.c.id == every[seed % len(every)]))
            elif op == "touch":
                subject = sorted(alerts)[seed % len(alerts)]
                _alert(db, alerts[subject], subject, PRIOS[seed % 4])
            elif op == "revoke" and agent_key and not state["revoked"]:
                db.remove_agent_permission(me, peer)
                state["revoked"] = True
            elif op == "grant" and agent_key and not state["granted"]:
                db.add_agent_permission(me, other, "op-815")
                state["granted"] = True

        if agent_key:
            _as(mcp_scope="agent", agent_name=me)
        else:
            _as()
        at_start = matching()
        queue = list(ops)

        def between(n, body):
            if queue:
                apply(*queue.pop(0))

        def params_for(n):
            agents = visible_agents()
            return {"agent_names": sorted(agents)} if agents else {}

        ids, _ = _walk(4, between=between, params_for=params_for)
        assert len(ids) == len(set(ids)), "an id was returned twice"
        must = at_start & matching()
        missing = must - set(ids)
        assert not missing, f"skipped {len(missing)} row(s) under {ops}"
