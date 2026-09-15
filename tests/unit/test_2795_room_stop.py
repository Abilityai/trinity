"""#2795 — a running room turn can be stopped, and a stop is not a failure.

Two halves, matching the two gaps in the issue:

1. **`can_stop` admits `kind: "room"`.** It is not a cosmetic widening: the
   terminate route's own gates are `_require_roster(agent)` and
   `execution_belongs_to_caller` (agent match + `source_user_email` match), and
   `shared_sessions.service._wake_agent` satisfies both by construction — it
   runs every wake through `execute_task(..., source_user_email=<the poster>)`
   on an agent that is a room participant. So the route accepted these rows all
   along and the projection hid the button. That asymmetry is what this file
   pins, by asserting the ROUTE's predicate and the PROJECTION's verdict agree.

2. **A cancelled turn reads as stopped, not as a fault.** `_wake_agent`'s
   terminal branch treated CANCELLED exactly like FAILED: it posted
   "<agent> could not respond (no response)." and dropped the cached resume
   handle. The first is the surface blaming the agent for something the reader
   asked for; the second makes the next turn pay for a cold context rebuild on
   no evidence the handle was bad.

The frontend half (the tile wiring and the Escape rule) is
`src/frontend/tests/unit/roomStopWork.spec.js`.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")
os.environ.setdefault("AGENT_AUTH_SECRET", "0" * 64)
os.environ.setdefault("SECRET_KEY", "x" * 32)
os.environ.setdefault("INTERNAL_API_SECRET", "y" * 32)
os.environ.setdefault("TRINITY_DB_PATH", str(Path(tempfile.gettempdir()) / "trinity-2795.db"))
os.environ.setdefault("LOG_ARCHIVE_PATH", str(Path(tempfile.gettempdir()) / "trinity-2795-logs"))

_REPO = Path(__file__).resolve().parents[2]
_BACKEND = _REPO / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import pytest  # noqa: E402

pytestmark = pytest.mark.unit

EMAIL = "bob@example.com"
AGENT = "scout"
ROOM = "room-1"


@pytest.fixture
def svc():
    from client_portal.work import service as mod
    return mod


@pytest.fixture
def rooms():
    from shared_sessions import service as mod
    return mod


def _recent(seconds_ago: int = 30) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _room_row(**over):
    """A row exactly as `_wake_agent` creates one: `triggered_by="room"`, the
    poster's email, and NO channel stamp (a room is not a portal thread)."""
    base = dict(
        id="exec-room-1", agent_name=AGENT, status="running", started_at=_recent(),
        completed_at=None, duration_ms=None, message="Summarise the thread",
        triggered_by="room", source_user_email=EMAIL, source_agent_name=None,
        source_channel=None, source_channel_chat_id=None, loop_id=None,
        error_summary=None,
    )
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# 1. can_stop
# ---------------------------------------------------------------------------

def test_a_room_turn_is_still_kind_room(svc):
    """The widening must not be smuggled in by relabelling the kind: the card
    says "Room turn" and the Work tab filters on it."""
    assert svc.work_kind(_room_row()) == "room"


@pytest.mark.parametrize("status, expected", [("running", True), ("queued", True)])
def test_a_room_turn_is_stoppable(svc, status, expected):
    assert svc.can_stop("room", status, mine=True, on_roster=True, stale=False) is expected


@pytest.mark.parametrize("kw", [
    dict(mine=False, on_roster=True, stale=False),   # someone else's run
    dict(mine=True, on_roster=False, stale=False),   # route would 404
    dict(mine=True, on_roster=True, stale=True),     # lost: nothing to stop
])
def test_a_room_turn_obeys_every_other_gate(svc, kw):
    """`room` is added to the kind allowlist and NOTHING else moves — in
    particular "only the person who started it may stop it" is untouched."""
    assert svc.can_stop("room", "running", **kw) is False


def test_loops_are_still_not_stoppable_here(svc):
    """A loop is stopped from the Loops tab; cancelling one iteration just
    leaves the runner to start the next."""
    assert svc.can_stop("loop", "running", mine=True, on_roster=True, stale=False) is False


def test_stoppable_kinds_is_an_allowlist(svc):
    """A blocklist would offer Stop on any trigger nobody has thought about
    yet. An unknown trigger projects as `other`, which must stay unstoppable."""
    assert svc.can_stop("other", "running", mine=True, on_roster=True, stale=False) is False
    assert isinstance(svc.STOPPABLE_KINDS, frozenset)


def test_the_projection_offers_stop_on_a_room_row(svc):
    """End to end through `_project`, which is what the client actually reads."""
    from datetime import datetime, timezone
    item = svc._project(_room_row(), email=EMAIL, roster={AGENT},
                        turn_timeout=3600, now=datetime.now(timezone.utc))
    assert item.kind == "room"
    assert item.mine is True
    assert item.can_stop is True
    # A room is not a portal thread, so there is no chat id to open — the tile
    # links via "Open in Work", not via a chat.
    assert item.chat_id is None


def test_the_projection_and_the_terminate_route_agree(svc):
    """The one property worth a test rather than a comment: `can_stop` claims to
    mirror `POST .../executions/{id}/terminate`, so the two predicates are
    evaluated against the SAME row and compared.

    `execution_belongs_to_caller` is the route's caller gate; the roster gate is
    the route's `_require_roster`, modelled here by the roster set.
    """
    from datetime import datetime, timezone
    import client_portal.service as portal_service

    row = _room_row()
    execution = SimpleNamespace(agent_name=row["agent_name"], status=row["status"],
                                source_user_email=row["source_user_email"])

    class _FakeDB:
        def get_execution(self, _id):
            return execution

    import database
    real = database.db
    database.db = _FakeDB()
    try:
        route_would_accept = portal_service.execution_belongs_to_caller(row["id"], AGENT, EMAIL)
    finally:
        database.db = real

    item = svc._project(row, email=EMAIL, roster={AGENT},
                        turn_timeout=3600, now=datetime.now(timezone.utc))
    assert route_would_accept is True
    assert item.can_stop is route_would_accept


# ---------------------------------------------------------------------------
# 2. a cancel is not a failure
# ---------------------------------------------------------------------------

class _WakeHarness:
    """`_wake_agent` up to its terminal branch, with every collaborator faked.

    Only the branch under test is exercised: the wake is driven to the point
    where `execute_task` has returned, and what the room DOES with that result
    is the assertion.
    """

    def __init__(self, monkeypatch, rooms, result, persisted_status=None):
        self.system_lines = []
        self.cleared_sessions = []
        self.advanced = []
        self.posted = []
        self.reread = []

        monkeypatch.setattr(rooms.db, "get_participant",
                            lambda *a, **k: {"last_read_seq": 0, "cached_session_id": "sess-cached"})
        monkeypatch.setattr(rooms.db, "get_room", lambda *a, **k: {"status": "open", "id": ROOM})
        monkeypatch.setattr(rooms.db, "get_messages",
                            lambda *a, **k: [{"seq": 4, "body": "hi", "sender_kind": "user",
                                              "sender_identity": EMAIL}])
        monkeypatch.setattr(rooms.db, "list_participants", lambda *a, **k: [])
        monkeypatch.setattr(rooms.db, "clear_cached_session",
                            lambda room_id, agent: self.cleared_sessions.append((room_id, agent)))
        monkeypatch.setattr(rooms.db, "advance_read_cursor",
                            lambda *a, **k: self.advanced.append(a))
        monkeypatch.setattr(rooms, "_post_system",
                            lambda room_id, text: self.system_lines.append(text))
        monkeypatch.setattr(rooms, "_mark_agent_working", lambda *a, **k: None)
        monkeypatch.setattr(rooms, "_clear_agent_working", lambda *a, **k: None)
        monkeypatch.setattr(rooms, "_broadcast", lambda *a, **k: None)
        monkeypatch.setattr(rooms, "_build_turn_prompt", lambda *a, **k: "prompt")
        monkeypatch.setattr(rooms, "room_is_user_facing", lambda *a, **k: True)
        monkeypatch.setattr(rooms, "build_user_facing_room_prompt", lambda *a, **k: None)

        async def _post_message(*a, **k):
            self.posted.append(a)

        monkeypatch.setattr(rooms, "post_message", _post_message)

        async def _execute_task(**kwargs):
            self.kwargs = kwargs
            return result

        import services.task_execution_service as tes
        monkeypatch.setattr(tes, "get_task_execution_service",
                            lambda: SimpleNamespace(execute_task=_execute_task))

        # The #2795 label re-read. `persisted_status=None` models a row that
        # cannot be read at all, which must leave the returned status in force.
        harness = self

        class _CoreDB:
            def get_execution(self, eid):
                harness.reread.append(eid)
                if persisted_status is _UNREADABLE:
                    raise RuntimeError("db down")
                if persisted_status is None:
                    return None
                return SimpleNamespace(status=persisted_status)

        import database
        monkeypatch.setattr(database, "db", _CoreDB())


#: Distinguishes "the row read as nothing" from "the read raised".
_UNREADABLE = object()


def _result(status, response="", error=""):
    return SimpleNamespace(status=status, response=response, error=error,
                           execution_id="exec-room-1", session_id="sess-new")


def test_a_cancelled_turn_says_it_was_stopped(monkeypatch, rooms):
    h = _WakeHarness(monkeypatch, rooms, _result("cancelled", error="Execution cancelled by user"))
    asyncio.run(rooms._wake_agent(SimpleNamespace(email=EMAIL), ROOM, AGENT, 1))

    assert h.system_lines == [f"{AGENT}'s turn was stopped."]
    # The words a person asked for must not come back as the agent's fault.
    assert not any("could not respond" in line for line in h.system_lines)


def test_a_cancelled_turn_keeps_the_resume_handle(monkeypatch, rooms):
    """The drop exists for a DEAD handle. A cancel is no evidence of one, and
    dropping it makes the next turn pay for a cold context rebuild."""
    h = _WakeHarness(monkeypatch, rooms, _result("cancelled"))
    asyncio.run(rooms._wake_agent(SimpleNamespace(email=EMAIL), ROOM, AGENT, 1))

    assert h.cleared_sessions == []


def test_a_cancelled_turn_does_not_advance_the_read_cursor(monkeypatch, rooms):
    """Unchanged, and worth pinning: the delta this turn never answered must be
    re-delivered on the next wake."""
    h = _WakeHarness(monkeypatch, rooms, _result("cancelled"))
    asyncio.run(rooms._wake_agent(SimpleNamespace(email=EMAIL), ROOM, AGENT, 1))

    assert h.advanced == []
    assert h.posted == []


def test_a_failed_turn_is_unchanged(monkeypatch, rooms):
    """The regression guard on the split: FAILED keeps both behaviours."""
    h = _WakeHarness(monkeypatch, rooms, _result("failed", error="boom"))
    asyncio.run(rooms._wake_agent(SimpleNamespace(email=EMAIL), ROOM, AGENT, 1))

    assert h.system_lines == [f"{AGENT} could not respond: boom"]
    assert h.cleared_sessions == [(ROOM, AGENT)]


def test_a_success_with_no_reply_is_still_a_failure(monkeypatch, rooms):
    h = _WakeHarness(monkeypatch, rooms, _result("success", response="   "))
    asyncio.run(rooms._wake_agent(SimpleNamespace(email=EMAIL), ROOM, AGENT, 1))

    assert h.system_lines == [f"{AGENT} could not respond (no response)."]
    assert h.cleared_sessions == [(ROOM, AGENT)]


def test_a_successful_turn_still_posts_and_advances(monkeypatch, rooms):
    h = _WakeHarness(monkeypatch, rooms, _result("success", response="Here you go."))
    asyncio.run(rooms._wake_agent(SimpleNamespace(email=EMAIL), ROOM, AGENT, 1))

    assert h.system_lines == []
    assert h.posted and h.advanced


def test_the_wake_stamps_the_poster_so_only_they_can_stop_it(monkeypatch, rooms):
    """The fact `can_stop`'s `mine` gate rests on. If a wake ever stopped
    carrying the poster's email, Stop would silently vanish again."""
    h = _WakeHarness(monkeypatch, rooms, _result("success", response="ok"))
    asyncio.run(rooms._wake_agent(SimpleNamespace(email=EMAIL), ROOM, AGENT, 1))

    assert h.kwargs["source_user_email"] == EMAIL
    assert h.kwargs["triggered_by"] == "room"


# ---------------------------------------------------------------------------
# 3. the label that actually stands (old agent images)
# ---------------------------------------------------------------------------

def test_a_failed_label_over_a_cancelled_row_reads_as_stopped(monkeypatch, rooms):
    """The old-image path. The agent re-raises instead of relabelling, so
    `execute_task` writes FAILED, that write LOSES the CAS to the CANCELLED the
    terminate route already wrote — and returns FAILED anyway. Without the
    re-read the room blames the agent for a stop the reader asked for."""
    h = _WakeHarness(monkeypatch, rooms, _result("failed", error="Timeout"),
                     persisted_status="cancelled")
    asyncio.run(rooms._wake_agent(SimpleNamespace(email=EMAIL), ROOM, AGENT, 1))

    assert h.reread == ["exec-room-1"]
    assert h.system_lines == [f"{AGENT}'s turn was stopped."]
    # And the resume handle survives: a cancel is no evidence of a dead one.
    assert h.cleared_sessions == []


def test_a_genuine_failure_is_still_a_failure(monkeypatch, rooms):
    """The re-read must not turn every failure into a cancel."""
    h = _WakeHarness(monkeypatch, rooms, _result("failed", error="boom"),
                     persisted_status="failed")
    asyncio.run(rooms._wake_agent(SimpleNamespace(email=EMAIL), ROOM, AGENT, 1))

    assert h.system_lines == [f"{AGENT} could not respond: boom"]
    assert h.cleared_sessions == [(ROOM, AGENT)]


def test_an_already_cancelled_label_is_not_re_read(monkeypatch, rooms):
    """No read on the path that is already exact — the common case pays nothing."""
    h = _WakeHarness(monkeypatch, rooms, _result("cancelled"), persisted_status="cancelled")
    asyncio.run(rooms._wake_agent(SimpleNamespace(email=EMAIL), ROOM, AGENT, 1))

    assert h.reread == []
    assert h.system_lines == [f"{AGENT}'s turn was stopped."]


def test_a_success_with_a_reply_is_not_re_read(monkeypatch, rooms):
    """The hot path pays nothing. The first draft re-read on EVERY turn — one
    extra DB read per successful room reply, for a label that could not change."""
    h = _WakeHarness(monkeypatch, rooms, _result("success", response="ok"),
                     persisted_status="cancelled")
    asyncio.run(rooms._wake_agent(SimpleNamespace(email=EMAIL), ROOM, AGENT, 1))

    assert h.reread == []
    assert h.system_lines == []
    assert h.posted


def test_a_success_with_no_reply_IS_re_read(monkeypatch, rooms):
    """An empty reply takes the failure branch, so it is a place the label can
    still be wrong — a SIGKILL'd turn on an old image can land here."""
    h = _WakeHarness(monkeypatch, rooms, _result("success", response="  "),
                     persisted_status="cancelled")
    asyncio.run(rooms._wake_agent(SimpleNamespace(email=EMAIL), ROOM, AGENT, 1))

    assert h.reread == ["exec-room-1"]
    assert h.system_lines == [f"{AGENT}'s turn was stopped."]
    assert h.cleared_sessions == []


def test_an_unreadable_row_leaves_the_returned_status_in_force(monkeypatch, rooms):
    """Fail-OPEN: a label read must never be able to break the turn."""
    h = _WakeHarness(monkeypatch, rooms, _result("failed", error="boom"),
                     persisted_status=_UNREADABLE)
    asyncio.run(rooms._wake_agent(SimpleNamespace(email=EMAIL), ROOM, AGENT, 1))

    assert h.system_lines == [f"{AGENT} could not respond: boom"]


def test_a_missing_row_leaves_the_returned_status_in_force(monkeypatch, rooms):
    h = _WakeHarness(monkeypatch, rooms, _result("failed", error="boom"),
                     persisted_status=None)
    asyncio.run(rooms._wake_agent(SimpleNamespace(email=EMAIL), ROOM, AGENT, 1))

    assert h.system_lines == [f"{AGENT} could not respond: boom"]
