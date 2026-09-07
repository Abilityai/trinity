"""trinity-enterprise#491 — a room carries a real last-message time.

`enterprise_rooms` has no `last_message_at` column, so `list_rooms` returned only
`created_at` and the Workspace's `normalizeRoomRow` fell back to it. A room's
"recency" was therefore its CREATION time: a busy month-old room sorted below one
opened this morning and never used — the opposite of the ordering this issue asks
for.
"""
import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
DB = REPO / "src/backend/shared_sessions/db.py"
SERVICE = REPO / "src/backend/shared_sessions/service.py"


def test_the_batched_reader_exists_and_groups_by_room():
    src = DB.read_text()
    assert "def last_message_for_rooms" in src
    fn = src[src.index("def last_message_for_rooms"):]
    fn = fn[:fn.index("\ndef ", 1)]
    assert "MAX(created_at)" in fn
    assert "GROUP BY room_id" in fn


def test_it_is_one_query_for_many_rooms_not_a_read_per_room():
    """The sibling `count_messages_for_rooms` batches for the same reason; a
    per-room read here would put an N+1 on the sidebar's bootstrap."""
    src = DB.read_text()
    fn = src[src.index("def last_message_for_rooms"):]
    fn = fn[:fn.index("\ndef ", 1)]
    tree = ast.parse("def _f():\n" + "\n".join("    " + l for l in fn.splitlines()[1:]))
    executes = [n for n in ast.walk(tree)
                if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "execute"]
    assert len(executes) == 1, "more than one execute() — that is a read per room"


def test_an_empty_room_is_absent_rather_than_invented():
    """The caller keeps `created_at`, which for a room nobody has spoken in is
    the honest answer — not epoch, and not a fabricated timestamp."""
    src = DB.read_text()
    fn = src[src.index("def last_message_for_rooms"):]
    fn = fn[:fn.index("\ndef ", 1)]
    assert 'if r["last_at"]' in fn


def test_list_rooms_stamps_it_on_every_row():
    src = SERVICE.read_text()
    fn = src[src.index("def list_rooms("):]
    fn = fn[:fn.index("\ndef ", 1)]
    assert "last_message_for_rooms" in fn
    assert 'r["last_message_at"] = last_by_room.get(r["id"])' in fn


def test_it_rides_the_existing_batch_rather_than_adding_a_round_trip():
    """`list_rooms` already makes two batched reads (participants, counts). This
    is a third of the same shape, not a per-room query inside the loop."""
    src = SERVICE.read_text()
    fn = src[src.index("def list_rooms("):]
    fn = fn[:fn.index("\ndef ", 1)]
    body = fn[fn.index("for r in rooms:"):]
    assert "db." not in body, "a db call inside the per-room loop is an N+1"
