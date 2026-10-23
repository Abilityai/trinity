"""#3679 — a capped read of pending asks keeps the longest-waiting ones.

`list_items` ordered every row by `sort_time` DESCENDING inside its priority, so
pending asks came newest first. The Workspace Inbox and the operator queue both
read ONE capped page (200) and then show it oldest first; past the cap, the
page they received had already dropped the asks that had waited longest.

Pending now sorts oldest first (`created_at` ascending), so the cap cuts the
newest. Ended rows keep their order (#627 AC6): newest-ended first, after every
pending row. Real SQLite, ages set explicitly.
"""
from __future__ import annotations

import sqlite3

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture()
def queue(tmp_path, monkeypatch):
    db_file = tmp_path / "trinity-3679.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_file))
    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", str(db_file))
    from db.schema import init_schema
    raw = sqlite3.connect(db_file)
    init_schema(raw.cursor(), raw)
    raw.commit()
    raw.close()

    from database import db

    def seed(req_id, created, priority="medium", ended=None):
        item_id = db.create_operator_queue_item("scout", {"id": req_id, "title": "t", "question": "q", "priority": priority})
        with sqlite3.connect(db_file) as c:
            c.execute("UPDATE operator_queue SET created_at=? WHERE id=?", (created, item_id))
            if ended:
                c.execute("UPDATE operator_queue SET status='responded', responded_at=?, disposed_at=? WHERE id=?",
                          (ended, ended, item_id))
        return item_id

    return db, seed


def T(n):
    return f"2026-10-{n:02d}T09:00:00Z"


def ids(rows):
    return [r["id"] for r in rows]


def test_a_capped_page_keeps_the_longest_waiting_asks(queue):
    db, seed = queue
    by_day = {n: seed(f"req-{n}", T(n)) for n in (5, 1, 4, 2, 3)}     # inserted out of order
    page = db.list_operator_queue_items(status="pending", limit=3)
    assert ids(page) == [by_day[1], by_day[2], by_day[3]]           # oldest first; 4 and 5 cut


def test_priority_still_comes_before_age(queue):
    db, seed = queue
    old_medium = seed("req-old", T(1), "medium")
    new_high = seed("req-new", T(9), "high")
    assert ids(db.list_operator_queue_items(status="pending", limit=10)) == [new_high, old_medium]


def test_ended_rows_stay_newest_ended_first_after_every_pending_one(queue):
    db, seed = queue
    pending_new = seed("req-p2", T(8))
    pending_old = seed("req-p1", T(2))
    ended_early = seed("req-e1", T(1), ended=T(3))
    ended_late = seed("req-e2", T(1), ended=T(7))
    got = ids(db.list_operator_queue_items(limit=10))
    assert got == [pending_old, pending_new, ended_late, ended_early]


def test_offset_pages_walk_the_pending_queue_oldest_first_without_gaps(queue):
    """#3059 pages with offset; the order must be stable across pages."""
    db, seed = queue
    by_day = {n: seed(f"req-{n}", T(n)) for n in range(1, 8)}
    pages = [ids(db.list_operator_queue_items(status="pending", limit=3, offset=o)) for o in (0, 3, 6)]
    assert sum(pages, []) == [by_day[n] for n in range(1, 8)]
