"""@mentioning an agent that is not in the room brings it in (ent#361 AC#4).

Before this, `resolve_mentions` matched only names ALREADY in the room and said
so outright — *"an @name that isn't in the room is left as plain text — a
mention can never reach outside"*. So the issue's own Context, which described
this path as existing and merely needing preserving, described something that
was never built.

Adding it puts a way INTO the room in the hands of whoever is typing, which is
why most of what is tested here is the boundary rather than the happy path:

  * **Only a human may do it.** An agent that could pull arbitrary agents into a
    room is a spend amplifier and a prompt-injection lever — a compromised
    workspace could assemble every agent its operator can reach and bill the
    operator for the conversation. A human's mention is a decision; an agent's
    is text it generated.
  * **An unreachable name stays plain text, and is not an error.** Raising would
    turn the composer into an oracle for which agent names exist, and would fail
    a message whose only problem is a word shaped like a handle.
  * **The participant cap holds** even when several newcomers arrive in one
    message.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")
os.environ.setdefault("AGENT_AUTH_SECRET", "0" * 64)
os.environ.setdefault("SECRET_KEY", "x" * 32)
os.environ.setdefault("INTERNAL_API_SECRET", "y" * 32)
os.environ.setdefault("TRINITY_DB_PATH", str(Path(tempfile.gettempdir()) / "trinity-ent361.db"))
os.environ.setdefault("LOG_ARCHIVE_PATH", str(Path(tempfile.gettempdir()) / "trinity-ent361-logs"))

import pytest

ROOM = "room_abc"


def _participants(*names):
    return [{"kind": "agent", "identity": n, "left_at": None} for n in names]


@pytest.fixture()
def svc(monkeypatch):
    """The engine with its DB and ACL stubbed — the join decision is the code
    under test, not the storage."""
    from shared_sessions import service as s

    state = {"added": [], "system": [], "reachable": {"recon", "scout", "sage"}}

    monkeypatch.setattr(s.db, "add_participant",
                        lambda room_id, kind, name, role, now: state["added"].append(name))
    monkeypatch.setattr(s, "_post_system",
                        lambda room_id, text: state["system"].append(text))

    def _reach(current_user, agent_name):
        if agent_name not in state["reachable"]:
            raise s.RoomError(403, "agent_not_accessible", "nope")

    monkeypatch.setattr(s, "_assert_can_reach_agent", _reach)
    return s, state


OPEN_ROOM = {"id": ROOM, "status": "open"}


def test_a_mentioned_agent_the_caller_can_reach_joins(svc):
    s, state = svc

    joined = s._join_mentioned_newcomers(
        object(), ROOM, OPEN_ROOM, "can @recon take a look?", _participants("scribe"),
    )

    assert joined == ["recon"]
    assert state["added"] == ["recon"]
    assert state["system"] == ["recon joined the room."]


def test_an_unreachable_name_stays_plain_text_and_does_not_raise(svc):
    """The composer must not become an oracle for which agents exist, and a
    message whose only problem is a word shaped like a handle must still send."""
    s, state = svc

    joined = s._join_mentioned_newcomers(
        object(), ROOM, OPEN_ROOM, "ping @nobody about it", _participants("scribe"),
    )

    assert joined == [] and state["added"] == []


def test_an_agent_already_in_the_room_is_not_re_added(svc):
    s, state = svc

    joined = s._join_mentioned_newcomers(
        object(), ROOM, OPEN_ROOM, "@scribe thanks", _participants("scribe"),
    )

    assert joined == [] and state["system"] == []


def test_an_agent_that_left_can_be_mentioned_back_in(svc):
    s, state = svc
    left = [{"kind": "agent", "identity": "recon", "left_at": "2026-08-01T00:00:00Z"}]

    joined = s._join_mentioned_newcomers(object(), ROOM, OPEN_ROOM, "@recon again", left)

    assert joined == ["recon"]


def test_several_newcomers_in_one_message_all_join(svc):
    s, state = svc

    joined = s._join_mentioned_newcomers(
        object(), ROOM, OPEN_ROOM, "@recon @scout together", _participants("scribe"),
    )

    assert joined == ["recon", "scout"]


def test_the_participant_cap_holds_across_one_message(svc, monkeypatch):
    """Three newcomers in one message must not step over the cap together — the
    check is per addition, not once up front."""
    s, state = svc
    monkeypatch.setattr(s, "MAX_PARTICIPANTS", 2)

    joined = s._join_mentioned_newcomers(
        object(), ROOM, OPEN_ROOM, "@recon @scout @sage", _participants("scribe"),
    )

    assert joined == ["recon"]          # one seat was free
    assert state["added"] == ["recon"]


def test_nobody_joins_a_closed_room(svc):
    s, state = svc

    joined = s._join_mentioned_newcomers(
        object(), ROOM, {"id": ROOM, "status": "closed"}, "@recon", _participants("scribe"),
    )

    assert joined == [] and state["added"] == []


def test_a_failed_join_does_not_fail_the_message(svc, monkeypatch):
    """The user's message already exists as far as they are concerned; losing it
    because a participant row would not write is the wrong trade."""
    s, state = svc

    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(s.db, "add_participant", boom)

    joined = s._join_mentioned_newcomers(
        object(), ROOM, OPEN_ROOM, "@recon", _participants("scribe"),
    )

    assert joined == []


def test_an_agents_own_mention_never_recruits(monkeypatch):
    """The gate that matters: `post_message` only offers this to a human sender.

    An agent that could pull agents into a room is a spend amplifier and a
    prompt-injection lever — text it generated would become membership.
    """
    import inspect
    from shared_sessions import service as s

    src = inspect.getsource(s.post_message)
    call = src.index("_join_mentioned_newcomers")
    guard = src.rindex("if not is_agent_reply:", 0, call)

    # The call sits inside the not-an-agent-reply branch, and nothing else
    # re-enables it further down.
    assert guard < call
    assert src.count("_join_mentioned_newcomers") == 1
