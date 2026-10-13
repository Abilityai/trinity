"""#3210 — a long agent reply must never be the reason a room fails.

An agent reply over 8,000 characters used to be discarded: the turn ran, was
billed and recorded as `success`, and the room showed nothing — no message, no
system line, no log. The 8,000 cap is an INBOUND rule (it mirrors
`RoomMessageCreate.max_length`), but `post_message` applied it to the engine's
own post of the reply, and that post sat outside every handler.

What this file pins, in the order the failure travelled:

* a long reply LANDS, whole, as one message (one row keeps `room_cost` and the
  message budget exact — a split reply would double-count the first and spend
  the second);
* past the reply ceiling it is trimmed with a visible marker, never dropped;
* once a message has landed, nothing downstream can fail the post — not a wake
  that raises, not a budget read that raises;
* a reply that genuinely cannot be posted says so in the room and in the log;
* the transcript handed to the NEXT agent stays bounded, so a long reply cannot
  kill the room one hop later instead.
"""
from __future__ import annotations

import asyncio
import logging
import re
import types

import pytest


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def rooms_db(tmp_path, monkeypatch):
    db_file = tmp_path / "trinity-rooms-3210.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_file))

    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", str(db_file))

    from db.engine import get_engine
    from db.tables import metadata as m, agent_ownership, users, schedule_executions
    m.create_all(get_engine(), tables=[agent_ownership, users, schedule_executions])

    from conftest import ensure_schema_tables
    ensure_schema_tables("enterprise_rooms", "enterprise_room_participants", "enterprise_room_messages")

    from sqlalchemy import insert
    with get_engine().begin() as conn:
        conn.execute(insert(users).values(id=1, username="alice", role="admin",
                                          email="alice@example.com",
                                          created_at="t", updated_at="t"))
        for name in ("agent-a", "agent-b", "agent-c"):
            conn.execute(insert(agent_ownership).values(
                agent_name=name, owner_id=1, created_at="t"))
    yield str(db_file)


def _user(username="alice", role="admin", agent_name=None):
    from models import User
    return User(id=1, username=username, role=role, agent_name=agent_name)


@pytest.fixture()
def allow_all(monkeypatch):
    import dependencies
    monkeypatch.setattr(dependencies, "assert_agent_access", lambda *a, **k: None)


@pytest.fixture()
def fake_execute(monkeypatch):
    """Capture what each woken agent was shown, and control its reply."""
    calls = []
    replies = {}

    async def _execute_task(**kwargs):
        calls.append(kwargs)
        agent = kwargs["agent_name"]
        reply = replies.get(agent, f"{agent} acknowledges")
        return types.SimpleNamespace(status="success", response=reply, cost=0.01,
                                     error=None, execution_id=f"ex_{agent}",
                                     session_id=f"sess_{agent}")

    svc = types.SimpleNamespace(execute_task=_execute_task)
    import services.task_execution_service as tes
    monkeypatch.setattr(tes, "get_task_execution_service", lambda: svc)
    return calls, replies


def _mk_room(agents=("agent-a", "agent-b"), **kw):
    from shared_sessions import service
    return service.create_room(_user(), kw.pop("name", "Room"), list(agents), **kw)


def _agent_messages(room_id):
    from shared_sessions import db
    return [m for m in db.get_messages(room_id) if m["sender_kind"] == "agent"]


def _system_lines(room_id):
    from shared_sessions import db
    return [m["content"] for m in db.get_messages(room_id) if m["kind"] == "system"]


def _participant(room_id, agent):
    from shared_sessions import db
    return db.get_participant(room_id, "agent", agent)


def _brief(chars: int) -> str:
    """A reply of exactly ``chars`` characters that reads like prose."""
    text = ("The findings are set out below in order of importance. " * (chars // 50 + 2))
    return text[:chars - 1].rstrip().ljust(chars - 1, "x") + "."


_MARKER_RE = re.compile(r"\[… [\d,]+ characters omitted")


# ===========================================================================
# 1. A long reply lands
# ===========================================================================

def test_a_reply_over_the_inbound_cap_lands_whole_as_one_message(rooms_db, allow_all, fake_execute):
    """The reported case: 9,119 characters, billed, and absent from the room."""
    from shared_sessions import service
    _calls, replies = fake_execute
    brief = _brief(9119)
    assert len(brief) > service.MAX_CONTENT_CHARS
    replies["agent-a"] = brief

    room = _mk_room()
    out = _run(service.post_message(_user(), room["id"], "@agent-a write the brief"))

    assert out["woke"] == ["agent-a"]
    landed = _agent_messages(room["id"])
    assert len(landed) == 1, "one turn is one room message"
    assert landed[0]["content"] == brief
    assert landed[0]["execution_id"] == "ex_agent-a"

    # The turn counted: the cursor moved and the resume handle was kept, so the
    # next wake is warm instead of a cold repeat of the same failure.
    p = _participant(room["id"], "agent-a")
    assert p["last_read_seq"] > 0
    assert p["cached_session_id"] == "sess_agent-a"
    assert not any("could not" in line for line in _system_lines(room["id"]))


def test_a_long_reply_counts_its_cost_once(rooms_db, allow_all, fake_execute):
    """Why the reply is ONE row: `room_cost` joins messages to executions, so a
    reply split across rows would charge the turn once per part."""
    from shared_sessions import service, db as rdb
    from db.engine import get_engine
    from db.tables import schedule_executions
    from sqlalchemy import insert

    _calls, replies = fake_execute
    replies["agent-a"] = _brief(20_000)
    room = _mk_room()
    _run(service.post_message(_user(), room["id"], "@agent-a go"))

    with get_engine().begin() as conn:
        conn.execute(insert(schedule_executions).values(
            id="ex_agent-a", schedule_id="__manual__", agent_name="agent-a",
            status="success", started_at="t", message="m", triggered_by="room",
            cost=0.73))
    assert rdb.room_cost(room["id"]) == pytest.approx(0.73)
    assert rdb.count_budget_messages(room["id"]) == 2      # the human's, the agent's


def test_a_reply_past_the_ceiling_is_trimmed_not_dropped(rooms_db, allow_all, fake_execute):
    from shared_sessions import service
    _calls, replies = fake_execute
    huge = _brief(service.MAX_AGENT_REPLY_CHARS + 25_000)
    replies["agent-a"] = huge

    room = _mk_room()
    _run(service.post_message(_user(), room["id"], "@agent-a everything you have"))

    landed = _agent_messages(room["id"])
    assert len(landed) == 1
    content = landed[0]["content"]
    assert len(content) <= service.MAX_AGENT_REPLY_CHARS
    assert _MARKER_RE.search(content), "a trimmed reply says it was trimmed"
    assert content.startswith(huge[:1000])
    assert content.endswith(huge[-1000:])
    assert _participant(room["id"], "agent-a")["last_read_seq"] > 0


def test_a_handoff_at_the_end_of_a_trimmed_reply_still_wakes_its_target(
        rooms_db, allow_all, fake_execute, monkeypatch):
    """A long brief ends with "@next please review". Keeping only the head would
    drop the one line that moves the room forward."""
    from shared_sessions import service
    calls, replies = fake_execute
    monkeypatch.setattr(service, "MAX_AGENT_REPLY_CHARS", 2000)
    monkeypatch.setattr(service, "AGENT_REPLY_TAIL_CHARS", 300)
    replies["agent-a"] = _brief(5000) + " @agent-b please review."

    room = _mk_room()
    _run(service.post_message(_user(), room["id"], "@agent-a start"))

    assert [c["agent_name"] for c in calls] == ["agent-a", "agent-b"]


# --- the trimming helper, as a pure function --------------------------------

def test_fit_reply_leaves_a_reply_at_the_bound_alone():
    from shared_sessions import service
    text = "a" * service.MAX_AGENT_REPLY_CHARS
    assert service._fit_reply(text) is text


def test_fit_reply_counts_its_own_marker():
    """Trimmed text PLUS the marker must fit: a result one marker too long is
    how a later length check would bring the dropped reply back."""
    from shared_sessions import service
    for over in (1, 7, 500, 40_000):
        out = service._fit_reply("word " * ((service.MAX_AGENT_REPLY_CHARS + over) // 5 + 1))
        assert len(out) <= service.MAX_AGENT_REPLY_CHARS
        assert _MARKER_RE.search(out)


def test_fit_reply_handles_text_with_no_whitespace(monkeypatch):
    from shared_sessions import service
    monkeypatch.setattr(service, "MAX_AGENT_REPLY_CHARS", 1000)
    monkeypatch.setattr(service, "AGENT_REPLY_TAIL_CHARS", 100)
    out = service._fit_reply("x" * 5000)
    assert len(out) <= 1000 and _MARKER_RE.search(out)


def test_fit_reply_never_cuts_a_mention_in_half(monkeypatch):
    """`@sales-eu` cut to `@sales` resolves to a DIFFERENT participant and wakes
    it. Wherever the cut falls, a surviving mention is the one that was written."""
    from shared_sessions import service
    monkeypatch.setattr(service, "MAX_AGENT_REPLY_CHARS", 1000)
    monkeypatch.setattr(service, "AGENT_REPLY_TAIL_CHARS", 100)
    for offset in range(0, 1100, 3):
        text = "a" * offset + " @sales-eu " + "b " * 2500 + " @ops-team done"
        out = service._fit_reply(text)
        assert len(out) <= 1000
        found = set(service._MENTION_RE.findall(out))
        assert found <= {"sales-eu", "ops-team"}, (offset, found)


def test_fit_reply_never_keeps_half_a_mention_when_there_is_no_whitespace(monkeypatch):
    """No whitespace near the cut means no word boundary to retreat to; the
    mention rule still holds."""
    from shared_sessions import service
    monkeypatch.setattr(service, "MAX_AGENT_REPLY_CHARS", 1000)
    monkeypatch.setattr(service, "AGENT_REPLY_TAIL_CHARS", 100)
    for offset in range(700, 900):
        out = service._fit_reply("." * offset + "@sales-eu" + "." * 3000)
        assert set(service._MENTION_RE.findall(out)) <= {"sales-eu"}, offset


def test_fit_reply_keeps_a_tail_that_starts_exactly_on_a_mention(monkeypatch):
    """Tidying the tail's first word must not eat it when the cut already fell
    on a word boundary — that word is the hand-off."""
    from shared_sessions import service
    handoff = "@agent-b please review"
    monkeypatch.setattr(service, "MAX_AGENT_REPLY_CHARS", 1000)
    monkeypatch.setattr(service, "AGENT_REPLY_TAIL_CHARS", len(handoff))
    out = service._fit_reply("word " * 1000 + handoff)
    assert out.endswith(handoff)
    assert service._MENTION_RE.findall(out) == ["agent-b"]


def test_fit_reply_closes_a_code_fence_the_cut_left_open(monkeypatch):
    """An open fence in the kept head would render the marker — and the tail —
    as code."""
    from shared_sessions import service
    monkeypatch.setattr(service, "MAX_AGENT_REPLY_CHARS", 1000)
    monkeypatch.setattr(service, "AGENT_REPLY_TAIL_CHARS", 100)
    text = "Here is the script:\n```python\n" + "print('x')\n" * 800 + "```\nDone."
    out = service._fit_reply(text)
    head = _MARKER_RE.split(out)[0]
    assert head.count("```") % 2 == 0
    # ...and the tail, which starts inside that block, reopens it — otherwise
    # the block's own closing fence would OPEN one and swallow the last lines.
    assert out.count("```") % 2 == 0
    assert len(out) <= 1000


# ===========================================================================
# 2. The inbound cap is unchanged
# ===========================================================================

def test_a_human_post_over_the_cap_is_still_refused(rooms_db, allow_all, fake_execute):
    from shared_sessions import service
    room = _mk_room()
    with pytest.raises(service.RoomError) as e:
        _run(service.post_message(_user(), room["id"], "x" * (service.MAX_CONTENT_CHARS + 1)))
    assert e.value.status_code == 413 and e.value.code == "message_too_large"


def test_an_agent_posting_through_the_api_is_still_capped(rooms_db, allow_all, fake_execute):
    """The larger bound belongs to the ENGINE's post of a turn's reply. An agent
    calling `post_to_room` is an inbound caller like any other."""
    from shared_sessions import service
    room = _mk_room()
    agent = _user(agent_name="agent-a")
    with pytest.raises(service.RoomError) as e:
        _run(service.post_message(agent, room["id"], "x" * (service.MAX_CONTENT_CHARS + 1)))
    assert e.value.status_code == 413


# ===========================================================================
# 3. A reply that cannot be posted is visible, and nothing climbs the chain
# ===========================================================================

def _fail_appends(monkeypatch, should_fail):
    """Make `db.append_message` raise for the rows ``should_fail`` selects."""
    from shared_sessions import db as rdb
    real = rdb.append_message

    def _append(msg_id, room_id, sender_kind, sender_identity, content, mentions,
                kind, execution_id, now):
        if should_fail(sender_kind, sender_identity, kind):
            raise RuntimeError("disk full")
        return real(msg_id, room_id, sender_kind, sender_identity, content, mentions,
                    kind, execution_id, now)

    monkeypatch.setattr(rdb, "append_message", _append)


def test_a_reply_that_cannot_be_posted_says_so(rooms_db, allow_all, fake_execute,
                                               monkeypatch, caplog):
    from shared_sessions import service
    room = _mk_room()
    _fail_appends(monkeypatch, lambda sender_kind, _identity, kind: sender_kind == "agent")

    with caplog.at_level(logging.WARNING):
        out = _run(service.post_message(_user(), room["id"], "@agent-a please"))

    assert out["seq"] and out["woke"] == ["agent-a"]      # the human's post is fine
    assert _agent_messages(room["id"]) == []
    assert "agent-a's reply could not be posted (RuntimeError)." in _system_lines(room["id"])
    assert "ex_agent-a" in caplog.text, "the log must name the execution that was lost"
    # Not marked as seen: an unposted reply re-shows its delta (ent#220 item 1).
    assert _participant(room["id"], "agent-a")["last_read_seq"] == 0


def test_a_database_outage_after_the_post_landed_does_not_fail_the_post(
        rooms_db, allow_all, fake_execute, monkeypatch):
    """The handler's own system line goes through the same table. If writing it
    could raise, the handler would turn a lost reply into a 500 for the human."""
    from shared_sessions import service
    room = _mk_room()
    landed = []

    def _only_the_first(sender_kind, _identity, kind):
        if sender_kind == "user" and not landed:
            landed.append(1)
            return False
        return True

    _fail_appends(monkeypatch, _only_the_first)
    out = _run(service.post_message(_user(), room["id"], "@agent-a please"))
    assert out["seq"]


def test_a_handler_that_cannot_write_its_line_does_not_stop_the_next_wake(
        rooms_db, allow_all, fake_execute, monkeypatch):
    """Two targets; the first one's reply cannot be posted and neither can the
    line saying so. The second target is a different agent with a different
    reply — it must still get its turn."""
    from shared_sessions import service
    calls, _replies = fake_execute
    room = _mk_room()
    _fail_appends(monkeypatch, lambda sender_kind, identity, kind:
                  kind == "system" or (sender_kind == "agent" and identity == "agent-a"))

    _run(service.post_message(_user(), room["id"], "@agent-a @agent-b both of you"))

    assert [c["agent_name"] for c in calls] == ["agent-a", "agent-b"]
    assert [m["sender_identity"] for m in _agent_messages(room["id"])] == ["agent-b"]


def test_a_failure_after_the_reply_landed_is_not_reported_as_a_lost_reply(
        rooms_db, allow_all, fake_execute, monkeypatch):
    """`post_message` does more after the append (budget close, roster, wake
    cap). An error there is not "could not be posted" — the reply is in the room."""
    from shared_sessions import service
    room = _mk_room()

    def _boom(_room):
        raise RuntimeError("budget read failed")

    monkeypatch.setattr(service, "_budget_exceeded_reason", _boom)
    out = _run(service.post_message(_user(), room["id"], "@agent-a please"))

    assert out["seq"]
    assert [m["content"] for m in _agent_messages(room["id"])] == ["agent-a acknowledges"]
    assert not any("could not be posted" in line for line in _system_lines(room["id"]))
    assert _participant(room["id"], "agent-a")["last_read_seq"] > 0


def _fail_wakes_of(monkeypatch, agent, exc):
    from shared_sessions import service
    real = service._wake_agent

    async def _wake(current_user, room_id, agent_name, chain_depth):
        if agent_name == agent:
            raise exc
        await real(current_user, room_id, agent_name, chain_depth)

    monkeypatch.setattr(service, "_wake_agent", _wake)


def test_a_downstream_wake_that_raises_stops_at_that_wake(rooms_db, allow_all,
                                                         fake_execute, monkeypatch):
    """A's reply mentions B and B's wake raises. A answered: its reply is in the
    room and its turn counts. The human posted: their post returns."""
    from shared_sessions import service
    _calls, replies = fake_execute
    replies["agent-a"] = "@agent-b over to you"
    room = _mk_room()
    _fail_wakes_of(monkeypatch, "agent-b", RuntimeError("boom"))

    out = _run(service.post_message(_user(), room["id"], "@agent-a start"))

    assert out["seq"] and out["woke"] == ["agent-a"]
    assert [m["content"] for m in _agent_messages(room["id"])] == ["@agent-b over to you"]
    p = _participant(room["id"], "agent-a")
    assert p["last_read_seq"] > 0 and p["cached_session_id"] == "sess_agent-a"
    assert "agent-b could not be woken (RuntimeError)." in _system_lines(room["id"])


def test_one_failed_wake_does_not_stop_the_next_target(rooms_db, allow_all,
                                                      fake_execute, monkeypatch):
    from shared_sessions import service
    calls, _replies = fake_execute
    room = _mk_room()
    _fail_wakes_of(monkeypatch, "agent-a", RuntimeError("boom"))

    _run(service.post_message(_user(), room["id"], "@agent-a @agent-b both of you"))

    assert [c["agent_name"] for c in calls] == ["agent-b"]
    assert "agent-a could not be woken (RuntimeError)." in _system_lines(room["id"])


def test_a_failure_before_any_wake_still_leaves_a_line(rooms_db, allow_all,
                                                       fake_execute, monkeypatch):
    """The guard around what follows a landed message must not be a quieter
    version of the original bug: the post returns, AND the room says the
    mention went nowhere."""
    from shared_sessions import service
    calls, _replies = fake_execute
    room = _mk_room()

    def _boom(*_a, **_k):
        raise RuntimeError("limiter down")

    monkeypatch.setattr(service, "_apply_wake_cap", _boom)
    out = _run(service.post_message(_user(), room["id"], "@agent-a please"))

    assert out["seq"] and out["woke"] == [] and calls == []
    assert any("its mentions could not be processed (RuntimeError)" in line
               for line in _system_lines(room["id"]))


def test_a_cursor_that_cannot_advance_is_not_reported_as_a_failed_wake(
        rooms_db, allow_all, fake_execute, monkeypatch):
    """The reply is in the room. Calling that "could not be woken" would be a
    false line about an agent that answered."""
    from shared_sessions import service, db as rdb
    room = _mk_room()

    def _boom(*_a, **_k):
        raise RuntimeError("locked")

    monkeypatch.setattr(rdb, "advance_read_cursor", _boom)
    out = _run(service.post_message(_user(), room["id"], "@agent-a please"))

    assert out["woke"] == ["agent-a"]
    assert [m["content"] for m in _agent_messages(room["id"])] == ["agent-a acknowledges"]
    assert not any("could not" in line for line in _system_lines(room["id"]))


def test_a_cancellation_survives_a_system_line_that_cannot_be_written(
        rooms_db, allow_all, fake_execute, monkeypatch):
    """The cancel handler writes "the sender disconnected". If that write raised,
    an ordinary error would replace the cancellation and be swallowed by the
    guard above it — a task that was told to stop would carry on."""
    from shared_sessions import service
    room = _mk_room()
    _fail_wakes_of(monkeypatch, "agent-a", asyncio.CancelledError())
    _fail_appends(monkeypatch, lambda _sender_kind, _identity, kind: kind == "system")

    with pytest.raises(asyncio.CancelledError):
        _run(service.post_message(_user(), room["id"], "@agent-a @agent-b both of you"))


def test_cancellation_is_never_swallowed(rooms_db, allow_all, fake_execute, monkeypatch):
    """`CancelledError` is how a process shuts down. The new handlers catch
    `Exception`; this pins that they stay that narrow, at both depths."""
    from shared_sessions import service
    _calls, replies = fake_execute
    replies["agent-a"] = "@agent-b over to you"
    room = _mk_room()
    _fail_wakes_of(monkeypatch, "agent-b", asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        _run(service.post_message(_user(), room["id"], "@agent-a start"))


def test_the_http_post_succeeds_and_keeps_its_idempotency_claim(rooms_db, allow_all,
                                                               fake_execute, monkeypatch):
    """The human's message landed. Answering 413/500 for it — and failing the
    idempotency claim — invites a retry that posts it a second time."""
    from shared_sessions import router
    from shared_sessions.models import RoomMessageCreate
    from services import idempotency_service

    room = _mk_room()
    _fail_wakes_of(monkeypatch, "agent-a", RuntimeError("boom"))

    seen = {"complete": 0, "fail": 0}
    decision = types.SimpleNamespace(replay=False, in_flight=False, snapshot=None)
    monkeypatch.setattr(idempotency_service, "begin", lambda scope, key: decision)
    monkeypatch.setattr(idempotency_service, "complete",
                        lambda *a, **k: seen.__setitem__("complete", seen["complete"] + 1))
    monkeypatch.setattr(idempotency_service, "fail",
                        lambda *a, **k: seen.__setitem__("fail", seen["fail"] + 1))

    out = _run(router.post_message(room["id"], RoomMessageCreate(content="@agent-a hi"),
                                   idempotency_key="k-1", current_user=_user()))

    assert out["seq"]
    assert seen == {"complete": 1, "fail": 0}


def test_a_reply_that_arrives_after_the_room_closed_is_not_silent(
        rooms_db, allow_all, fake_execute, monkeypatch, caplog):
    """Same class as the reported bug: the turn ran and was billed, and its reply
    was dropped with no trace."""
    from shared_sessions import service, db as rdb
    room = _mk_room()
    real_clear = service._clear_agent_working

    def _close_mid_turn(room_id, agent_name):
        rdb.close_room(room_id, "user_closed", "2026-01-01T00:00:00Z")
        real_clear(room_id, agent_name)

    # `_clear_agent_working` runs in the dispatch's `finally`: the turn has
    # finished, its reply has not been posted yet.
    monkeypatch.setattr(service, "_clear_agent_working", _close_mid_turn)

    with caplog.at_level(logging.WARNING):
        _run(service.post_message(_user(), room["id"], "@agent-a please"))

    assert _agent_messages(room["id"]) == []
    assert any("arrived after the room closed" in line for line in _system_lines(room["id"]))
    assert "ex_agent-a" in caplog.text


# ===========================================================================
# 4. The transcript handed to the next agent stays bounded
# ===========================================================================

def _row(seq, content, sender="agent-a", kind="agent", mentions=()):
    return {"seq": seq, "sender_kind": kind, "sender_identity": sender,
            "kind": "message", "mentions": list(mentions), "content": content}


def test_a_transcript_under_budget_is_untouched():
    from shared_sessions import service
    rows = [_row(i, f"message {i}") for i in range(1, 11)]
    assert service._fit_transcript(rows, "agent-b") == rows


def test_a_transcript_is_bounded_whatever_the_delta_holds():
    """500 rows is the most a warm delta reads; 100,000 characters is the most
    one reply holds. Excerpts would not be a bound here — whole-or-omitted is."""
    from shared_sessions import service
    big = "y" * service.MAX_AGENT_REPLY_CHARS
    rows = [_row(i, big) for i in range(1, 501)]

    out = service._fit_transcript(rows, "agent-b")

    assert len(service._format_delta(out)) <= service.ROOM_TRANSCRIPT_BUDGET_CHARS
    assert out[-1] is rows[-1], "the newest message is always whole"
    omitted = [m for m in out if "omitted for length" in (m.get("content") or "")]
    assert len(omitted) == 1, "one line for the whole omitted run, not one per message"
    assert "498" in omitted[0]["content"]


def test_the_message_that_mentions_the_agent_is_kept_whole(monkeypatch):
    """In "@a @b question", B's newest message is A's reply — the one that
    mentions B sits further back. Omitting it would hand B a turn with no ask."""
    from shared_sessions import service
    monkeypatch.setattr(service, "ROOM_TRANSCRIPT_BUDGET_CHARS", 3000)
    ask = _row(2, "@agent-b what is our exposure? " + "z" * 500, sender="alice",
               kind="user", mentions=["agent-b"])
    rows = ([_row(1, "old " * 300)] + [ask]
            + [_row(i, "filler " * 200) for i in range(3, 8)]
            + [_row(8, "newest " * 100)])

    out = service._fit_transcript(rows, "agent-b")

    assert ask in out and rows[-1] in out
    assert len(service._format_delta(out)) <= 3000
    assert [m for m in out if m in rows] == [m for m in rows if m in out], "order is preserved"
    assert sum("omitted for length" in (m.get("content") or "") for m in out) <= 2


def test_what_is_kept_is_one_unbroken_run_of_the_newest_messages(monkeypatch):
    """A small old message that happens to fit behind a large one it cannot see
    past is not kept: a transcript with holes reads as a conversation that was
    never had."""
    from shared_sessions import service
    monkeypatch.setattr(service, "ROOM_TRANSCRIPT_BUDGET_CHARS", 1000)
    rows = [_row(1, "tiny"), _row(2, "h" * 900), _row(3, "m" * 300), _row(4, "newest")]

    out = service._fit_transcript(rows, "agent-b")

    assert [m["seq"] for m in out] == [None, 3, 4]
    assert "2 earlier messages" in out[0]["content"]


def test_the_lines_the_helper_adds_are_inside_the_budget(monkeypatch):
    """The "omitted" line is transcript too. Packing messages up to the budget
    and then adding it would overshoot."""
    from shared_sessions import service
    monkeypatch.setattr(service, "ROOM_TRANSCRIPT_BUDGET_CHARS", 1000)
    rows = [_row(1, "a" * 500), _row(2, "b" * 400), _row(3, "c" * 480), _row(4, "d" * 80)]

    out = service._fit_transcript(rows, "agent-b")

    assert len(service._format_delta(out)) <= 1000
    assert out[-1] is rows[-1]


def test_the_newest_message_survives_a_budget_smaller_than_itself(monkeypatch):
    from shared_sessions import service
    monkeypatch.setattr(service, "ROOM_TRANSCRIPT_BUDGET_CHARS", 100)
    rows = [_row(1, "a" * 500), _row(2, "b" * 500)]
    out = service._fit_transcript(rows, "agent-b")
    assert out[-1] is rows[-1]


def test_fit_transcript_tolerates_rows_without_content_or_mentions():
    """Other suites hand `_wake_agent_locked` rows shaped differently from the
    table's; a helper that indexed `content` would break them."""
    from shared_sessions import service
    rows = [{"seq": 4, "body": "hi", "sender_kind": "user", "sender_identity": "x"}]
    assert service._fit_transcript(rows, "agent-a") == rows
    assert service._fit_transcript([], "agent-a") == []


def test_a_cold_agent_after_long_messages_gets_a_bounded_prompt(rooms_db, allow_all,
                                                               fake_execute, monkeypatch):
    from shared_sessions import service
    calls, _replies = fake_execute
    monkeypatch.setattr(service, "ROOM_TRANSCRIPT_BUDGET_CHARS", 5000)
    room = _mk_room()
    for i in range(10):
        _run(service.post_message(_user(), room["id"], f"note {i}: " + "detail " * 200))
    _run(service.post_message(_user(), room["id"], "@agent-a summarise the notes"))

    prompt = calls[0]["message"]
    assert "@agent-a summarise the notes" in prompt
    assert "omitted for length" in prompt
    assert len(prompt) <= 5000 + 1500        # the budget plus the turn header


def test_a_trimmed_wake_still_advances_the_cursor_past_the_whole_delta(
        rooms_db, allow_all, fake_execute, monkeypatch):
    """Omitted messages are not re-queued: the cursor ends on the newest message
    of the delta, so the next wake starts after it."""
    from shared_sessions import service, db as rdb
    monkeypatch.setattr(service, "ROOM_TRANSCRIPT_BUDGET_CHARS", 5000)
    room = _mk_room()
    for i in range(10):
        _run(service.post_message(_user(), room["id"], f"note {i}: " + "detail " * 200))
    out = _run(service.post_message(_user(), room["id"], "@agent-a summarise"))

    assert _participant(room["id"], "agent-a")["last_read_seq"] == out["seq"]
    assert rdb.get_messages(room["id"])[-1]["sender_kind"] == "agent"


# ===========================================================================
# 5. The turn prompt
# ===========================================================================

def test_the_turn_prompt_suggests_a_file_for_a_long_deliverable():
    """A soft hint, not a limit: a long reply still lands. It is there because
    every later-woken agent re-reads what is posted."""
    from shared_sessions import service
    prompt = service._build_turn_prompt({"name": "R"}, "agent-a", [], cold=False)
    assert "long deliverable" in prompt
