"""trinity-enterprise#1046: Direct rows and the #2694 delta, on a real database.

A Direct reply is the agent's own session answering, so it must move the cursor. If it
did not, every turn after a Direct call would be re-sent the thread since the last typed
reply. Live calls must keep their behaviour.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from db_harness import db_backend  # noqa: E402,F401

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_ent751_gate_entries import world  # noqa: E402,F401

pytestmark = pytest.mark.unit

AGENT = "direct-agent"
CLIENT = "client-1046@example.com"
SESSION = "ps_1046"


@pytest.fixture
def pdb(world):
    from client_portal import db as portal_db
    return portal_db


def _row(pdb, row_id, role, content, at, *, source=None, call=None, execution_id=None, gate_hold=None):
    pdb.add_portal_message(row_id, AGENT, CLIENT, role, content, None, at, session_id=SESSION,
                           source=source, voice_call_id=call, execution_id=execution_id,
                           gate_hold=gate_hold)


def _delta(pdb):
    return [r["content"] for r in pdb.get_platform_rows_since_last_reply(AGENT, CLIENT, SESSION)]


def test_a_direct_reply_moves_the_cursor_past_an_earlier_live_call(pdb):
    _row(pdb, "1", "user", "live words", "2026-11-02T10:00:00.000000Z", source="voice", call="vs_a")
    _row(pdb, "2", "user", "direct words", "2026-11-02T10:01:00.000000Z", call="vd_b")
    _row(pdb, "3", "assistant", "direct reply", "2026-11-02T10:01:05.000000Z", call="vd_b")
    assert _delta(pdb) == []


def test_a_direct_calls_label_never_reenters_the_delta(pdb):
    _row(pdb, "1", "assistant", "direct reply", "2026-11-02T10:01:05.000000Z", call="vd_b")
    _row(pdb, "2", "system", "Direct call · 2 min", "2026-11-02T10:02:00.000000Z", source="voice", call="vd_b")
    assert _delta(pdb) == []


def test_a_live_call_after_a_direct_reply_is_still_delivered(pdb):
    _row(pdb, "1", "assistant", "direct reply", "2026-11-02T10:01:05.000000Z", call="vd_b")
    _row(pdb, "2", "user", "live words", "2026-11-02T10:03:00.000000Z", source="voice", call="vs_c")
    assert _delta(pdb) == ["live words"]


def test_a_live_calls_background_task_reply_is_still_not_the_cursor(pdb):
    _row(pdb, "1", "user", "live words", "2026-11-02T10:00:00.000000Z", source="voice", call="vs_a")
    _row(pdb, "2", "assistant", "task landed", "2026-11-02T10:00:30.000000Z", call="vs_a")
    assert _delta(pdb) == ["live words"]


def test_the_turn_reply_row_is_found_by_execution_and_carries_the_hold(pdb):
    assert pdb.get_turn_reply_row("ex-1") is None
    _row(pdb, "1", "user", "ask", "2026-11-02T10:00:00.000000Z", execution_id="ex-1")
    _row(pdb, "2", "assistant", "Not run", "2026-11-02T10:00:01.000000Z", execution_id="ex-1", gate_hold="g")
    row = pdb.get_turn_reply_row("ex-1")
    assert row["content"] == "Not run" and row["gate_hold"] == "g"
