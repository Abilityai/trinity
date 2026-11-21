"""trinity-enterprise#594 — a long Workspace thread can be read past its window.

#2694 made the thread read a WINDOW (the newest 100 typed turns plus the
spoken rows of the calls among them, under a row ceiling) and said so only
when the ceiling cut. Everything older was unreachable, and a thread with more
than 100 typed turns lost its head with no notice at all, because `truncated`
was set by the ceiling alone.

The read now takes a `before` cursor — the `(created_at, id)` of the oldest row
on screen — and pages the same window shape strictly older than it. It reports
`has_earlier` (something older exists, whichever bound cut it) and
`next_before` (the cursor for the next page). The cursor is compared as stored
strings in SQL, exactly the ORDER BY the window uses, so pages never overlap
and never skip a row that shares a stamp with the boundary.

Runs on a throwaway sqlite with the real portal tables and the real writers
(the #2694 harness).
"""
from __future__ import annotations

import sys
import types
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

ALICE = "alice@example.com"
AGENT = "scribe"
THREAD = "thread-ent594"


@pytest.fixture()
def portal_db(tmp_path, monkeypatch):
    db_file = tmp_path / "trinity-ent594.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_file))
    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", str(db_file))
    from db.engine import get_engine
    from db.tables import (
        metadata as oss_metadata,
        enterprise_portal_messages,
        enterprise_portal_sessions,
    )
    engine = get_engine()
    oss_metadata.create_all(engine, tables=[enterprise_portal_messages, enterprise_portal_sessions])
    from client_portal import db as pdb
    from utils.helpers import utc_now_iso
    pdb.create_portal_session(THREAD, AGENT, ALICE, utc_now_iso())
    yield engine


def _typed(role: str, text: str, stamp: str | None = None, mid: str | None = None) -> None:
    from client_portal import db as pdb
    from utils.helpers import utc_now_iso
    pdb.add_portal_message(mid or uuid.uuid4().hex, AGENT, ALICE, role, text, None,
                           stamp or utc_now_iso(), session_id=THREAD)


def _call(call_id: str, n_turns: int) -> None:
    from client_portal import voice as pv
    s = types.SimpleNamespace(session_id=call_id, agent_name=AGENT, client_email=ALICE,
                              portal_session_id=THREAD, max_duration=1800)
    for i in range(n_turns):
        pv.persist_voice_turn(s, "user" if i % 2 == 0 else "assistant", f"said {call_id} {i}")
    pv.persist_voice_call_end(s, 120.0)


def _cursor(s: str) -> tuple[str, str]:
    at, _, mid = s.rpartition("|")
    return at, mid


def _walk(typed_limit: int, ceiling: int | None = None) -> list[list[dict]]:
    """Every page of the thread, newest page first."""
    from client_portal import db as pdb
    pages, before = [], None
    for _ in range(1000):
        win = pdb.get_portal_thread_window(AGENT, ALICE, THREAD, typed_limit=typed_limit,
                                           ceiling=ceiling, before=before)
        pages.append(win)
        if not win.has_earlier:
            break
        assert win.next_before, "has_earlier without a cursor would strand the client"
        before = _cursor(win.next_before)
    return pages


def _whole_thread() -> list[str]:
    from client_portal import db as pdb
    return [r["id"] for r in pdb.get_portal_messages(AGENT, ALICE, session_id=THREAD, limit=100000)]


# ---------------------------------------------------------------------------
# The db read pages
# ---------------------------------------------------------------------------

def test_pages_are_strictly_older_and_concatenate_to_the_whole_thread(portal_db):
    for i in range(25):
        _typed("user", f"q{i}")
        _typed("assistant", f"a{i}")
        if i % 6 == 0:
            _call(f"vs_{i}", 5)
    pages = _walk(typed_limit=10)
    assert len(pages) > 2
    for newer, older in zip(pages, pages[1:]):
        assert older.rows[-1]["created_at"] <= newer.rows[0]["created_at"]
        assert not {r["id"] for r in older.rows} & {r["id"] for r in newer.rows}
    stitched = [r["id"] for win in reversed(pages) for r in win.rows]
    assert stitched == _whole_thread()


def test_rows_sharing_the_boundary_stamp_are_neither_lost_nor_repeated(portal_db):
    """Six rows in one microsecond, the page boundary falling among them: the
    cursor's `id` half carries the tie, so the next page picks up exactly the
    rows the first left behind."""
    stamp = "2026-09-10T10:00:00.000000Z"
    _typed("user", "older", stamp="2026-09-10T09:00:00.000000Z", mid="z-old")
    for i in range(6):
        _typed("user", f"tie {i}", stamp=stamp, mid=f"id-{i}")
    _typed("user", "newer", stamp="2026-09-10T11:00:00.000000Z", mid="a-new")
    pages = _walk(typed_limit=3)
    stitched = [r["id"] for win in reversed(pages) for r in win.rows]
    assert stitched == ["z-old", "id-0", "id-1", "id-2", "id-3", "id-4", "id-5", "a-new"]
    assert len(stitched) == len(set(stitched))


def test_a_call_cut_by_the_ceiling_continues_on_the_next_page(portal_db):
    """The ceiling cut a call in half; the next page starts with the row right
    before the cut, so the call's rows are contiguous once the pages meet."""
    _typed("user", "before")
    _call("vs_huge", 80)
    _typed("user", "after")
    pages = _walk(typed_limit=100, ceiling=50)
    assert pages[0].truncated is True and pages[0].has_earlier is True
    stitched = [win.rows for win in reversed(pages)]
    flat = [r for rows in stitched for r in rows]
    assert [r["id"] for r in flat] == _whole_thread()
    spoken = [r["content"] for r in flat if r.get("voice_call_id") == "vs_huge" and r["role"] != "system"]
    assert spoken == [f"said vs_huge {i}" for i in range(80)]


def test_the_last_page_says_there_is_nothing_earlier(portal_db):
    from client_portal import db as pdb
    for i in range(5):
        _typed("user", f"m{i}")
    win = pdb.get_portal_thread_window(AGENT, ALICE, THREAD, typed_limit=100)
    assert win.has_earlier is False and win.next_before is None
    pages = _walk(typed_limit=2)
    assert pages[-1].has_earlier is False and pages[-1].next_before is None
    assert all(p.has_earlier for p in pages[:-1])


def test_more_typed_turns_than_the_window_says_so_without_a_ceiling_cut(portal_db):
    """The #2694 hole: 120 typed turns, no ceiling cut — `truncated` stays False
    but the 20 oldest turns are not on screen. `has_earlier` must admit it."""
    from client_portal import db as pdb
    for i in range(120):
        _typed("user", f"m{i}")
    win = pdb.get_portal_thread_window(AGENT, ALICE, THREAD, typed_limit=100)
    assert win.truncated is False
    assert len(win.rows) == 100
    assert win.has_earlier is True
    assert win.next_before == f"{win.rows[0]['created_at']}|{win.rows[0]['id']}"
    older = pdb.get_portal_thread_window(AGENT, ALICE, THREAD, typed_limit=100,
                                         before=_cursor(win.next_before))
    assert [r["content"] for r in older.rows] == [f"m{i}" for i in range(20)]
    assert older.has_earlier is False


def test_an_empty_page_has_no_cursor(portal_db):
    from client_portal import db as pdb
    _typed("user", "only", stamp="2026-09-10T10:00:00.000000Z", mid="m1")
    win = pdb.get_portal_thread_window(AGENT, ALICE, THREAD, typed_limit=10,
                                       before=("2026-09-10T10:00:00.000000Z", "m1"))
    assert win.rows == [] and win.has_earlier is False and win.next_before is None


def test_the_context_read_without_a_cursor_is_the_newest_window(portal_db):
    """The cold-turn context read (`service`, typed_limit=20, never cursored)
    still reads the newest window — the cursor is opt-in only."""
    from client_portal import db as pdb
    for i in range(30):
        _typed("user", f"m{i}")
    rows = pdb.get_portal_thread_window(AGENT, ALICE, THREAD, typed_limit=20).rows
    assert [r["content"] for r in rows] == [f"m{i}" for i in range(10, 30)]


# ---------------------------------------------------------------------------
# The service and the route
# ---------------------------------------------------------------------------

def _quiet_service(monkeypatch):
    from client_portal import service as svc
    monkeypatch.setattr(svc, "agent_on_roster", lambda a, e, include_owned=False: True)
    monkeypatch.setattr(svc, "_attach_own_ratings", lambda *a, **kw: None)
    monkeypatch.setattr(svc, "get_turn_inflight", lambda sid: "exec-live")
    monkeypatch.setattr(svc, "get_turn_outcome", lambda sid: {"execution_id": "exec-old", "category": "x", "message": "y"})
    return svc


def test_get_history_pages_with_before_and_drops_the_live_fields(portal_db, monkeypatch):
    svc = _quiet_service(monkeypatch)
    monkeypatch.setattr(svc, "_HISTORY_TYPED_TURNS", 5)
    for i in range(8):
        _typed("user", f"m{i}")
    first = svc.get_history(AGENT, ALICE, session_id=THREAD)
    assert [m["content"] for m in first["messages"]] == [f"m{i}" for i in range(3, 8)]
    assert first["has_earlier"] is True and first["next_before"]
    assert first["in_flight_execution_id"] == "exec-live"
    page = svc.get_history(AGENT, ALICE, session_id=THREAD, before=first["next_before"])
    assert [m["content"] for m in page["messages"]] == ["m0", "m1", "m2"]
    assert page["has_earlier"] is False and page["next_before"] is None
    # an older page is not the live tail: nothing in flight, no last outcome
    assert page["in_flight_execution_id"] is None and page["last_turn_outcome"] is None


def test_get_history_refuses_a_malformed_cursor(portal_db, monkeypatch):
    svc = _quiet_service(monkeypatch)
    for bad in ("no-separator", "|id", "2026-09-10T10:00:00Z|", "|"):
        with pytest.raises(svc.InvalidHistoryCursor):
            svc.get_history(AGENT, ALICE, session_id=THREAD, before=bad)


def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from client_portal.router import router
    from client_portal.portal_auth import PortalPrincipal, get_portal_principal
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_portal_principal] = lambda: PortalPrincipal(
        email=ALICE, is_platform=False)
    return TestClient(app)


def _history_path():
    from client_portal.router import router
    return f"{router.prefix}/agents/{AGENT}/history"


def test_the_route_pages_and_carries_the_cursor_fields(portal_db, monkeypatch):
    svc = _quiet_service(monkeypatch)
    monkeypatch.setattr(svc, "_HISTORY_TYPED_TURNS", 2)
    for i in range(3):
        _typed("user", f"m{i}")
    c = _client()
    r = c.get(_history_path(), params={"session_id": THREAD})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["has_earlier"] is True and body["next_before"]
    r2 = c.get(_history_path(), params={"session_id": THREAD, "before": body["next_before"]})
    assert r2.status_code == 200, r2.text
    assert [m["content"] for m in r2.json()["messages"]] == ["m0"]
    assert r2.json()["has_earlier"] is False and r2.json()["next_before"] is None


@pytest.mark.parametrize("bad", ["garbage", "|x", "x|"])
def test_the_route_names_a_bad_cursor(portal_db, monkeypatch, bad):
    _quiet_service(monkeypatch)
    r = _client().get(_history_path(), params={"session_id": THREAD, "before": bad})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "invalid_cursor"


def test_the_route_refuses_a_cursor_with_a_row_limit(portal_db, monkeypatch):
    """`limit` is the reply poll's narrow newest-N read; paging it would be a
    second, different pagination. The two do not combine."""
    _quiet_service(monkeypatch)
    r = _client().get(_history_path(), params={"session_id": THREAD, "limit": 3,
                                               "before": "2026-09-10T10:00:00Z|x"})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "invalid_cursor"


def test_the_route_bounds_the_cursor_length(portal_db, monkeypatch):
    _quiet_service(monkeypatch)
    r = _client().get(_history_path(), params={"session_id": THREAD, "before": "a|" + "b" * 300})
    assert r.status_code == 422
