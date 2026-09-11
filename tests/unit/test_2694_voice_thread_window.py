"""trinity#2694 — a voice call sits where it happened (the read side).

The stored order was never wrong: both writers stamp the same ISO-Z microsecond
format and the read orders by it. What hoisted a call to the head of a thread
was the WINDOW — `get_history` returned the newest 100 ROWS, and a 30-minute
call is ~180 rows, so one call pushed every typed turn before it off the
screen, and the block (anchored at the call's first row IN THE WINDOW) rendered
first. The window is now counted in TYPED turns; the spoken rows of the calls
among them ride along; a row ceiling bounds the payload and says so.

Runs against a throwaway sqlite carrying the real portal tables, driven by the
REAL writers (`add_portal_message`, `persist_voice_turn`,
`persist_voice_call_end`) — the invariant under test is the schema's own.
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
THREAD = "thread-2694"


@pytest.fixture()
def portal_db(tmp_path, monkeypatch):
    db_file = tmp_path / "trinity-2694.db"
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


def _typed(role: str, text: str) -> None:
    from client_portal import db as pdb
    from utils.helpers import utc_now_iso
    pdb.add_portal_message(uuid.uuid4().hex, AGENT, ALICE, role, text, None, utc_now_iso(),
                           session_id=THREAD)


def _call(call_id: str, n_turns: int, prefix: str = "said") -> None:
    """A whole call through the real persistence helpers: n spoken rows + the label."""
    from client_portal import voice as pv
    s = types.SimpleNamespace(session_id=call_id, agent_name=AGENT, client_email=ALICE,
                              portal_session_id=THREAD, max_duration=1800)
    for i in range(n_turns):
        pv.persist_voice_turn(s, "user" if i % 2 == 0 else "assistant", f"{prefix} {call_id} {i}")
    pv.persist_voice_call_end(s, 120.0)


def _shape(rows):
    return [(r["role"], r.get("source") or "-", r["content"]) for r in rows]


# ---------------------------------------------------------------------------
# The window is counted in typed turns
# ---------------------------------------------------------------------------

def test_the_thread_is_one_timeline_typed_call_typed_call_typed(portal_db):
    """The AC's shape: typed → call → typed → call → typed, with the first call
    LONGER than the old 100-row window. Every typed turn survives, in order."""
    from client_portal import db as pdb
    _typed("user", "t1 user"); _typed("assistant", "t1 reply")
    _call("vs_1", 150)
    _typed("user", "t2 user"); _typed("assistant", "t2 reply")
    _call("vs_2", 4)
    _typed("user", "t3 user"); _typed("assistant", "t3 reply")

    win = pdb.get_portal_thread_window(AGENT, ALICE, THREAD, typed_limit=100)
    rows = win.rows
    assert win.truncated is False
    assert len(rows) == 6 + (150 + 1) + (4 + 1)
    contents = [r["content"] for r in rows]
    assert contents[0] == "t1 user" and contents[1] == "t1 reply"
    # the first call sits between t1 and t2, the second between t2 and t3
    assert contents.index("t1 reply") < contents.index("said vs_1 0") < contents.index("t2 user")
    assert contents.index("t2 reply") < contents.index("said vs_2 0") < contents.index("t3 user")
    assert contents[-2:] == ["t3 user", "t3 reply"]
    # each call's rows are contiguous and keep their spoken order, label last
    calls = [(r["voice_call_id"], r["content"]) for r in rows if r.get("source") == "voice"]
    first = [c for cid, c in calls if cid == "vs_1"]
    assert first[:2] == ["said vs_1 0", "said vs_1 1"] and first[-1] == "Voice call · 2 min"


def test_the_window_counts_typed_rows_not_rows(portal_db):
    """120 typed rows with a call in the newest half: `typed_limit=100` keeps
    exactly the newest 100 typed rows AND the whole call — the old row window
    would have kept the call and 5 typed rows."""
    from client_portal import db as pdb
    for i in range(30):
        _typed("user", f"early {i}")
    _typed("assistant", "boundary")           # typed row 31 (oldest kept)
    for i in range(50):
        _typed("user", f"mid {i}")
    _call("vs_long", 95)                       # 96 rows
    for i in range(49):
        _typed("user", f"late {i}")

    win = pdb.get_portal_thread_window(AGENT, ALICE, THREAD, typed_limit=100)
    typed = [r for r in win.rows if r.get("source") is None]
    spoken = [r for r in win.rows if r.get("source") == "voice"]
    assert len(typed) == 100
    assert typed[0]["content"] == "boundary"          # the 100th newest typed row
    assert len(spoken) == 96                            # the whole call rides along
    assert not any(r["content"].startswith("early") for r in win.rows)
    assert win.truncated is False


def test_fewer_typed_rows_than_the_limit_returns_the_whole_thread(portal_db):
    from client_portal import db as pdb
    _typed("user", "u"); _call("vs_a", 3); _typed("assistant", "a")
    win = pdb.get_portal_thread_window(AGENT, ALICE, THREAD, typed_limit=100)
    assert [r["content"] for r in win.rows][0] == "u"
    assert len(win.rows) == 2 + 4 and win.truncated is False


def test_a_call_only_thread_is_returned_whole(portal_db):
    from client_portal import db as pdb
    _call("vs_only", 5)
    win = pdb.get_portal_thread_window(AGENT, ALICE, THREAD, typed_limit=100)
    assert len(win.rows) == 6 and win.truncated is False


def test_an_empty_thread_is_empty_not_an_error(portal_db):
    from client_portal import db as pdb
    win = pdb.get_portal_thread_window(AGENT, ALICE, THREAD, typed_limit=100)
    assert win.rows == [] and win.truncated is False


def test_the_ceiling_keeps_the_newest_rows_and_reports_it(portal_db):
    """A call longer than the ceiling: the NEWEST rows survive (the label and
    the typed turn after the call), the oldest fall off, and `truncated` says
    so — the UI can name what is missing instead of silently re-creating the
    symptom at call #9."""
    from client_portal import db as pdb
    _typed("user", "before")
    _call("vs_huge", 80)
    _typed("user", "after")
    win = pdb.get_portal_thread_window(AGENT, ALICE, THREAD, typed_limit=100, ceiling=50)
    assert win.truncated is True
    assert len(win.rows) == 50
    assert win.rows[-1]["content"] == "after"
    assert win.rows[-2]["content"] == "Voice call · 2 min"   # the label survives the cut
    assert not any(r["content"] == "before" for r in win.rows)
    # still oldest-first
    stamps = [r["created_at"] for r in win.rows]
    assert stamps == sorted(stamps)


def test_a_non_positive_limit_is_treated_as_one(portal_db):
    from client_portal import db as pdb
    _typed("user", "a"); _typed("user", "b")
    win = pdb.get_portal_thread_window(AGENT, ALICE, THREAD, typed_limit=0)
    assert [r["content"] for r in win.rows] == ["b"]


def test_the_window_is_scoped_to_its_thread(portal_db):
    from client_portal import db as pdb
    from utils.helpers import utc_now_iso
    pdb.create_portal_session("other", AGENT, ALICE, utc_now_iso())
    pdb.add_portal_message(uuid.uuid4().hex, AGENT, ALICE, "user", "elsewhere", None,
                           utc_now_iso(), session_id="other")
    _typed("user", "here")
    win = pdb.get_portal_thread_window(AGENT, ALICE, THREAD, typed_limit=100)
    assert [r["content"] for r in win.rows] == ["here"]


# ---------------------------------------------------------------------------
# The readers that must NOT change shape
# ---------------------------------------------------------------------------

def test_the_resend_dedup_read_keeps_row_semantics(portal_db):
    """`_persist_user_turn` asks for the LAST ROW whatever its source — after a
    call that is the label, which is exactly what lets a typed repeat of the
    spoken words through as a new message (ent#534)."""
    from client_portal import db as pdb
    _typed("user", "show me the plan")
    _call("vs_x", 2)
    last = pdb.get_portal_messages(AGENT, ALICE, limit=1, session_id=THREAD)
    assert isinstance(last, list) and len(last) == 1
    assert last[0]["role"] == "system" and last[0]["source"] == "voice"


def test_get_portal_messages_still_returns_a_plain_list(portal_db):
    from client_portal import db as pdb
    _typed("user", "a")
    rows = pdb.get_portal_messages(AGENT, ALICE, session_id=THREAD)
    assert isinstance(rows, list) and rows[0]["content"] == "a"


def test_equal_stamps_order_deterministically_on_every_read(portal_db):
    """Two rows in one microsecond cannot happen across the live writers (one
    clock, per-session monotonic stamps), but a read must still be stable: the
    `id` tiebreak is uuid — stable, NOT chronological — and both statements of
    the window use it, so the threshold and the range agree."""
    from client_portal import db as pdb
    stamp = "2026-09-10T10:00:00.000000Z"
    for i in range(3):
        pdb.add_portal_message(f"id-{i}", AGENT, ALICE, "user", f"tie {i}", None, stamp,
                               session_id=THREAD)
    a = [r["id"] for r in pdb.get_portal_thread_window(AGENT, ALICE, THREAD, typed_limit=2).rows]
    b = [r["id"] for r in pdb.get_portal_thread_window(AGENT, ALICE, THREAD, typed_limit=2).rows]
    assert a == b == ["id-1", "id-2"]
    c = [r["id"] for r in pdb.get_portal_messages(AGENT, ALICE, session_id=THREAD)]
    assert c == ["id-0", "id-1", "id-2"]


# ---------------------------------------------------------------------------
# What the live session never saw
# ---------------------------------------------------------------------------

def test_platform_rows_since_last_reply_is_cursored_on_the_typed_reply(portal_db):
    """The cursor is the newest TYPED ASSISTANT row — the last thing the live
    session itself produced. A failed typed turn leaves a user row with no
    reply; a user-row cursor would then erase the call from every later delta."""
    from client_portal import db as pdb
    _typed("user", "u0"); _typed("assistant", "a0")
    _call("vs_1", 2)
    _typed("user", "u1 (failed, no reply)")
    _call("vs_2", 2)
    rows = pdb.get_platform_rows_since_last_reply(AGENT, ALICE, THREAD)
    assert _shape(rows) == [
        ("user", "voice", "said vs_1 0"), ("assistant", "voice", "said vs_1 1"),
        ("system", "voice", "Voice call · 2 min"),
        ("user", "voice", "said vs_2 0"), ("assistant", "voice", "said vs_2 1"),
        ("system", "voice", "Voice call · 2 min"),
    ]
    # the retry of u1 gets a reply → the session has now heard everything
    _typed("assistant", "a1")
    assert pdb.get_platform_rows_since_last_reply(AGENT, ALICE, THREAD) == []
    # a second call later → only the new call
    _call("vs_3", 2)
    rows = pdb.get_platform_rows_since_last_reply(AGENT, ALICE, THREAD)
    assert [r["voice_call_id"] for r in rows] == ["vs_3"] * 3


def test_platform_rows_include_system_rows_and_exclude_typed_ones(portal_db):
    from client_portal import db as pdb
    from utils.helpers import utc_now_iso
    _typed("assistant", "a0")
    pdb.add_portal_message(uuid.uuid4().hex, AGENT, ALICE, "system", "Main was reset.", None,
                           utc_now_iso(), session_id=THREAD)
    _typed("user", "typed after")
    rows = pdb.get_platform_rows_since_last_reply(AGENT, ALICE, THREAD)
    assert _shape(rows) == [("system", "-", "Main was reset.")]


def test_platform_rows_with_no_reply_yet_are_every_platform_row(portal_db):
    from client_portal import db as pdb
    _call("vs_first", 3)
    _typed("user", "first typed question")
    rows = pdb.get_platform_rows_since_last_reply(AGENT, ALICE, THREAD)
    assert len(rows) == 4 and all(r["voice_call_id"] == "vs_first" for r in rows)


# ---------------------------------------------------------------------------
# The history endpoint carries the window
# ---------------------------------------------------------------------------

def test_get_history_uses_the_typed_window_and_says_when_it_cut(portal_db, monkeypatch):
    from client_portal import service as svc
    from client_portal import db as pdb
    monkeypatch.setattr(svc, "agent_on_roster", lambda a, e, include_owned=False: True)
    monkeypatch.setattr(svc, "_attach_own_ratings", lambda *a, **kw: None)
    monkeypatch.setattr(svc, "get_turn_inflight", lambda sid: None)
    _typed("user", "before"); _call("vs_h", 150); _typed("user", "after")
    out = svc.get_history(AGENT, ALICE, session_id=THREAD)
    assert out["messages"][0]["content"] == "before"        # the old 100-row window lost this
    assert out["messages"][-1]["content"] == "after"
    assert out["truncated"] is False
    monkeypatch.setattr(pdb, "PORTAL_HISTORY_ROW_CEILING", 20)
    out = svc.get_history(AGENT, ALICE, session_id=THREAD)
    assert out["truncated"] is True and len(out["messages"]) == 20


def test_get_history_with_an_explicit_limit_is_a_narrow_row_read(portal_db, monkeypatch):
    """The reply poll's read: newest N ROWS, never the window — it runs every
    700 ms while a turn is in flight, and it only needs the newest reply."""
    from client_portal import service as svc
    monkeypatch.setattr(svc, "agent_on_roster", lambda a, e, include_owned=False: True)
    monkeypatch.setattr(svc, "_attach_own_ratings", lambda *a, **kw: None)
    monkeypatch.setattr(svc, "get_turn_inflight", lambda sid: None)
    _typed("user", "q"); _call("vs_p", 10); _typed("assistant", "reply")
    out = svc.get_history(AGENT, ALICE, session_id=THREAD, limit=3)
    assert [m["content"] for m in out["messages"]][-1] == "reply"
    assert len(out["messages"]) == 3
    assert out["truncated"] is False              # a narrow read is not a cut window


def test_the_history_model_declares_truncated():
    from client_portal.models import PortalHistory
    assert PortalHistory(agent_name="a", messages=[]).truncated is False
    assert PortalHistory(agent_name="a", messages=[], truncated=True).truncated is True


def test_the_history_route_accepts_a_bounded_limit():
    """The router exposes `limit` (1–50, row semantics) — the poll's narrow read —
    and nothing that could widen the window."""
    import ast
    import inspect
    import textwrap
    from client_portal import router as r
    params = inspect.signature(r.portal_history).parameters
    assert "limit" in params
    assert "typed_window" not in params and "ceiling" not in params
    fn = ast.parse(textwrap.dedent(inspect.getsource(r.portal_history))).body[0]
    bounds = {}
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Query":
            bounds = {kw.arg: getattr(kw.value, "value", None) for kw in node.keywords}
    assert bounds.get("ge") == 1 and bounds.get("le") == 50
