"""trinity#4302 — a paused mention chain says so, and a person can continue it.

When agents hand a room back and forth `ROOM_MAX_CHAIN_DEPTH` times from one
human message, the engine stops the chain. Before this change the stop was a
plain system line ("Mention chain stopped at depth 8 ...") and a dead end. Now:

* the stop is a typed row — `kind="chain_paused"`, `mentions` = the agents that
  were next, content in plain words;
* the room payload carries `paused_chain` = `{"seq", "next"}` while the offer
  stands (no conversational row and no later pause/continue row after it; the
  room open; a named agent still seated), else `null`;
* `POST /api/rooms/{room_id}/continue` with `{"seq"}` lets a PERSON wake the
  agents that were next at a fresh depth. It consumes the offer by inserting a
  `kind="chain_continued"` receipt row under the same predicate, inside the seq
  allocation transaction — so two workers that both pass every check still wake
  the agents once. The route also accepts an optional `Idempotency-Key` under
  its own scope `room-continue:{room_id}`; the receipt, not the key, is the
  once-per-stop guard.

Everything below runs through the real rooms router, service, db layer and
idempotency service over a temp SQLite file; the agent turn itself is a fake
`execute_task`, as in `test_ent169_shared_sessions.py`. Sync throughout
(`tests/unit/pytest.ini` runs pytest-asyncio in strict mode).
"""
from __future__ import annotations

import asyncio
import threading
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete, insert, select, text

import shared_sessions.router as rooms_router
from models import User
from shared_sessions import db as rooms_db
from shared_sessions import service as rooms_service

NOW = "2026-11-07T10:00:00.000000Z"
GHOST_ROOM = "room_doesnotexist0"


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Principals, in the shapes `get_room_principal` returns
# --------------------------------------------------------------------------- #

def _person(user_id: int, username: str, *, role="creator", mcp_scope=None) -> User:
    keyed = mcp_scope is not None
    return User(
        id=user_id, username=username, email=f"{username}@example.com", role=role,
        agent_name=None, connector_agent=None, portal_delegate=False,
        mcp_scope=mcp_scope,
        mcp_key_id=f"mk-{username}" if keyed else None,
        mcp_key_name=f"{username} key" if keyed else None,
        vouched_source_agent=None, loopback_chain_depth=None, is_event_loopback=False,
    )


def _agent_key(agent_name: str, *, owner_id: int = 2, owner: str = "bob",
               role: str = "creator") -> User:
    return User(
        id=owner_id, username=owner, email=f"{owner}@example.com", role=role,
        agent_name=agent_name, connector_agent=None, portal_delegate=False,
        mcp_scope="agent", mcp_key_id=f"mk-{agent_name}", mcp_key_name=f"{agent_name} key",
        vouched_source_agent=None, loopback_chain_depth=None, is_event_loopback=False,
    )


def _system_key() -> User:
    return User(
        id=1, username="admin", email="admin@example.com", role="admin",
        agent_name=None, connector_agent=None, portal_delegate=False,
        mcp_scope="system", mcp_key_id="mk-system", mcp_key_name="system key",
        vouched_source_agent=None, loopback_chain_depth=None, is_event_loopback=False,
    )


BOB = lambda: _person(2, "bob")                       # noqa: E731 — the room's creator
CAROL = lambda: _person(3, "carol")                   # noqa: E731 — not seated unless a test seats her


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #

@pytest.fixture
def env(tmp_path, monkeypatch):
    db_file = tmp_path / "trinity-4302-rooms.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_file))
    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", str(db_file))

    from db.engine import get_engine
    from db.tables import (
        agent_ownership, idempotency_keys, metadata, operator_queue, schedule_executions, users)
    metadata.create_all(get_engine(), tables=[
        agent_ownership, users, schedule_executions, idempotency_keys, operator_queue])
    from db.schema import INDEXES, TABLES
    room_tables = ("enterprise_rooms", "enterprise_room_participants",
                   "enterprise_room_messages", "room_gate_holds")
    markers = tuple(f"{name}(" for name in room_tables)
    with get_engine().begin() as conn:
        for name in room_tables:
            conn.execute(text(TABLES[name]))
        for stmt in INDEXES:
            if any(marker in stmt for marker in markers):
                conn.execute(text(stmt))
    with get_engine().begin() as conn:
        for uid, name, role in ((1, "admin", "admin"), (2, "bob", "creator"),
                                (3, "carol", "creator")):
            conn.execute(insert(users).values(
                id=uid, username=name, role=role, email=f"{name}@example.com",
                created_at=NOW, updated_at=NOW))
        for agent in ("agent-a", "agent-b", "agent-c"):
            conn.execute(insert(agent_ownership).values(
                agent_name=agent, owner_id=2, created_at=NOW))

    # The WS fan-out would import the whole app.
    monkeypatch.setattr(rooms_service, "_broadcast", lambda *_a, **_k: None)
    # The room ACL delegates to the OSS agent-access guard; allowed unless a
    # test installs its own gate (`env.gate`).
    import dependencies
    gate = {"deny": set()}

    def _assert_agent_access(_user, agent_name, *_a, **_k):
        from fastapi import HTTPException
        if agent_name in gate["deny"]:
            raise HTTPException(status_code=403, detail="nope")

    monkeypatch.setattr(dependencies, "assert_agent_access", _assert_agent_access)

    # Every claim the real idempotency layer receives.
    from database import db as platform_db
    claims = []
    real_claim = platform_db.idempotency_claim

    def _recording_claim(scope, key, *args, **kwargs):
        claims.append((scope, key))
        return real_claim(scope, key, *args, **kwargs)

    monkeypatch.setattr(platform_db, "idempotency_claim", _recording_claim)

    # The agent turn: capture what each woken agent was shown, control its
    # reply, and optionally HOLD a turn open until the test releases it.
    calls, replies, holds = [], {}, {}

    async def _execute_task(**kwargs):
        calls.append(kwargs)
        agent = kwargs["agent_name"]
        hold = holds.get(agent)
        if hold is not None:
            hold["started"].set()
            while not hold["release"].is_set():
                await asyncio.sleep(0.01)
        reply = replies.get(agent, f"{agent} acknowledges")
        return types.SimpleNamespace(status="success", response=reply, cost=0.01,
                                     error=None, execution_id=f"ex_{agent}_{len(calls)}",
                                     session_id=f"sess_{agent}")

    import services.task_execution_service as tes
    monkeypatch.setattr(tes, "get_task_execution_service",
                        lambda: types.SimpleNamespace(execute_task=_execute_task))

    room = rooms_service.create_room(BOB(), "Ops", ["agent-a", "agent-b"], ttl_hours=0)

    app = FastAPI()
    app.include_router(rooms_router.router)
    holder = {}
    app.dependency_overrides[rooms_router.get_room_principal] = lambda: holder["principal"]
    yield types.SimpleNamespace(
        app=app, client=TestClient(app), holder=holder, room_id=room["id"],
        engine=get_engine(), keys=idempotency_keys, claims=claims,
        calls=calls, replies=replies, holds=holds, gate=gate, monkeypatch=monkeypatch,
    )
    app.dependency_overrides.clear()


def _rows(env, room_id=None):
    return rooms_db.get_messages(room_id or env.room_id)


def _of_kind(env, kind, room_id=None):
    return [m for m in _rows(env, room_id) if m["kind"] == kind]


def _get(env, principal, room_id=None):
    env.holder["principal"] = principal
    return env.client.get(f"/api/rooms/{room_id or env.room_id}")


def _post_message(env, principal, content, room_id=None):
    env.holder["principal"] = principal
    return env.client.post(f"/api/rooms/{room_id or env.room_id}/messages",
                           json={"content": content})


def _continue(env, principal, seq, *, room_id=None, key=None, client=None):
    env.holder["principal"] = principal
    headers = {"Idempotency-Key": key} if key is not None else {}
    return (client or env.client).post(
        f"/api/rooms/{room_id or env.room_id}/continue", json={"seq": seq}, headers=headers)


def _pause(env, *, cap=2, room_id=None, opener="@agent-a begin"):
    """Run a chain to the cap: a→b→a… from bob's one message. Returns the
    pause row. With cap=2 the chain is a (depth 1) → b (depth 2) → stop, and
    `agent-a` is next."""
    env.monkeypatch.setattr(rooms_service, "ROOM_MAX_CHAIN_DEPTH", cap)
    env.replies["agent-a"] = "@agent-b your turn"
    env.replies["agent-b"] = "@agent-a no, yours"
    r = _post_message(env, BOB(), opener, room_id=room_id)
    assert r.status_code == 200, r.text
    paused = _of_kind(env, "chain_paused", room_id)
    assert paused, [m["content"] for m in _rows(env, room_id)]
    return paused[-1]


def _quiet(env):
    """From now on a woken agent answers without mentioning anyone."""
    env.replies["agent-a"] = "on it"
    env.replies["agent-b"] = "on it"


def _in_thread(fn):
    out = {}

    def run():
        try:
            out["value"] = fn()
        except BaseException as e:  # noqa: BLE001 — surfaced by the caller
            out["error"] = e

    t = threading.Thread(target=run)
    t.start()
    return t, out


# --------------------------------------------------------------------------- #
# The stop is a typed row the payload names
# --------------------------------------------------------------------------- #

def test_cap_writes_typed_pause_row_in_plain_words(env):
    pause = _pause(env, cap=2)

    assert [c["agent_name"] for c in env.calls] == ["agent-a", "agent-b"]
    assert pause is _rows(env)[-1] or pause["seq"] == _rows(env)[-1]["seq"]
    assert pause["sender_kind"] == "system"
    assert pause["mentions"] == ["agent-a"]
    assert pause["content"] == ("The agents paused after 2 hand-offs and are waiting "
                                "for a person. agent-a was next.")
    assert "Mention chain stopped" not in pause["content"]

    r = _get(env, BOB())
    assert r.status_code == 200, r.text
    assert r.json()["paused_chain"] == {"seq": pause["seq"], "next": ["agent-a"]}


def test_several_agents_next_are_named_in_one_plural_line(env):
    trio = rooms_service.create_room(BOB(), "Trio", ["agent-a", "agent-b", "agent-c"],
                                     ttl_hours=0)["id"]
    env.monkeypatch.setattr(rooms_service, "ROOM_MAX_CHAIN_DEPTH", 1)
    env.replies["agent-c"] = "@agent-a @agent-b please both review"
    assert _post_message(env, BOB(), "@agent-c go", room_id=trio).status_code == 200

    (pause,) = _of_kind(env, "chain_paused", trio)
    assert pause["mentions"] == ["agent-a", "agent-b"]
    assert pause["content"].endswith("agent-a and agent-b were next.")
    assert _get(env, BOB(), trio).json()["paused_chain"] == {
        "seq": pause["seq"], "next": ["agent-a", "agent-b"]}


def test_offer_survives_bookkeeping_lines_but_not_a_message(env):
    pause = _pause(env)

    rooms_service.post_system_line(env.room_id, "carol joined the room.")
    assert _get(env, BOB()).json()["paused_chain"] == {"seq": pause["seq"], "next": ["agent-a"]}

    _quiet(env)
    assert _post_message(env, BOB(), "never mind, I'll take it from here").status_code == 200
    assert _get(env, BOB()).json()["paused_chain"] is None


def test_offer_is_null_when_the_room_is_closed(env):
    _pause(env)
    rooms_service.close_room(BOB(), env.room_id)
    assert _get(env, BOB()).json()["paused_chain"] is None


def test_offer_is_null_when_everyone_next_has_left(env):
    _pause(env)
    rooms_service.remove_participant(BOB(), env.room_id, "agent-a")
    # The "left the room" line is bookkeeping; it is the empty `next` that
    # withdraws the offer.
    assert _get(env, BOB()).json()["paused_chain"] is None


def test_collaborator_seated_above_the_pause_still_gets_the_offer(env):
    pause = _pause(env)
    rooms_db.seat_person(env.room_id, "user", "carol", "collaborator",
                         visible_from_seq=pause["seq"] + 1, now=NOW)

    body = _get(env, CAROL()).json()
    assert all(m["seq"] > pause["seq"] for m in body["messages"])  # the row is below her floor
    assert body["paused_chain"] == {"seq": pause["seq"], "next": ["agent-a"]}
    assert "cost" not in body  # the collaborator projection still applies


def test_plain_system_lines_carry_no_mentions(env):
    """The pause row is the only system row that names agents; `_post_system`
    stays `mentions=[]`, `kind="system"`, so the two cannot be confused."""
    seq = rooms_service.post_system_line(env.room_id, "a note")
    (row,) = [m for m in _rows(env) if m["seq"] == seq]
    assert (row["kind"], row["sender_kind"], row["mentions"]) == ("system", "system", [])


# --------------------------------------------------------------------------- #
# Continue, through the real route
# --------------------------------------------------------------------------- #

def test_continue_wakes_next_at_a_fresh_depth_and_may_pause_again(env):
    pause = _pause(env, cap=2)
    assert len(env.calls) == 2

    r = _continue(env, BOB(), pause["seq"])

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["room_id"] == env.room_id and body["woke"] == ["agent-a"]
    (receipt,) = _of_kind(env, "chain_continued")
    assert body["seq"] == receipt["seq"]
    assert receipt["content"] == "bob asked agent-a to carry on."
    assert receipt["sender_kind"] == "system" and receipt["mentions"] == ["agent-a"]
    # A fresh count: two more hops (a at depth 1, b at depth 2), then a NEW pause.
    assert [c["agent_name"] for c in env.calls] == ["agent-a", "agent-b", "agent-a", "agent-b"]
    pauses = _of_kind(env, "chain_paused")
    assert len(pauses) == 2 and pauses[-1]["seq"] > receipt["seq"]
    assert _get(env, BOB()).json()["paused_chain"] == {"seq": pauses[-1]["seq"],
                                                       "next": ["agent-a"]}


def test_next_agent_is_shown_what_it_missed(env):
    pause = _pause(env, cap=2)
    _quiet(env)

    assert _continue(env, BOB(), pause["seq"]).status_code == 200

    prompt = env.calls[-1]["message"]
    assert env.calls[-1]["agent_name"] == "agent-a"
    assert "no, yours" in prompt                    # b's hand-off, the message it was owed
    assert "waiting for a person" in prompt          # the pause line, as `System:` text
    assert "bob asked agent-a to carry on." in prompt
    assert "begin" not in prompt                     # what it had already seen is not re-sent
    assert "Continue" not in prompt                  # no UI verb reaches the model


def test_continue_spends_no_message_budget(env):
    pause = _pause(env)
    _quiet(env)
    before = rooms_db.count_budget_messages(env.room_id)

    assert _continue(env, BOB(), pause["seq"]).status_code == 200

    # Only the reply the Continue woke counts; the receipt is a system row.
    assert rooms_db.count_budget_messages(env.room_id) == before + 1
    assert _get(env, BOB()).json()["paused_chain"] is None


def test_a_second_continue_for_the_same_pause_has_moved_on(env):
    pause = _pause(env)
    _quiet(env)
    assert _continue(env, BOB(), pause["seq"]).status_code == 200

    r = _continue(env, BOB(), pause["seq"])

    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "room_moved_on"
    assert len(_of_kind(env, "chain_continued")) == 1
    assert [c["agent_name"] for c in env.calls[2:]] == ["agent-a"]


def test_a_stale_seq_has_moved_on(env):
    pause = _pause(env)
    r = _continue(env, BOB(), pause["seq"] - 1)
    assert (r.status_code, r.json()["detail"]["code"]) == (409, "room_moved_on")
    assert _of_kind(env, "chain_continued") == [] and len(env.calls) == 2


def test_seq_is_validated(env):
    _pause(env)
    env.holder["principal"] = BOB()
    assert env.client.post(f"/api/rooms/{env.room_id}/continue", json={"seq": 0}).status_code == 422
    assert env.client.post(f"/api/rooms/{env.room_id}/continue", json={}).status_code == 422


# --------------------------------------------------------------------------- #
# Who may continue
# --------------------------------------------------------------------------- #

def test_a_seated_collaborator_may_continue(env):
    pause = _pause(env)
    _quiet(env)
    rooms_db.seat_person(env.room_id, "user", "carol", "collaborator",
                         visible_from_seq=0, now=NOW)

    r = _continue(env, CAROL(), pause["seq"])

    assert r.status_code == 200, r.text
    assert r.json()["woke"] == ["agent-a"]
    assert _of_kind(env, "chain_continued")[0]["content"] == "carol asked agent-a to carry on."
    assert env.calls[-1]["agent_name"] == "agent-a"


def test_a_person_on_their_own_user_key_may_continue(env):
    pause = _pause(env)
    _quiet(env)
    r = _continue(env, _person(2, "bob", mcp_scope="user"), pause["seq"])
    assert r.status_code == 200, r.text
    assert r.json()["woke"] == ["agent-a"]


def test_a_workspace_client_may_continue(env):
    pause = _pause(env)
    _quiet(env)
    import client_portal.service as portal_service
    env.monkeypatch.setattr(portal_service, "agent_on_roster", lambda *_a, **_k: True)
    rooms_db.seat_person(env.room_id, rooms_service.WORKSPACE_KIND, "client@example.com",
                         "collaborator", visible_from_seq=0, now=NOW)

    r = _continue(env, rooms_router.WorkspacePrincipal(email="client@example.com",
                                                      is_platform=False), pause["seq"])

    assert r.status_code == 200, r.text
    assert r.json()["woke"] == ["agent-a"]
    assert _of_kind(env, "chain_continued")[0]["content"] == (
        "client@example.com asked agent-a to carry on.")


@pytest.mark.parametrize("who", ["seated_agent_key", "system_key"])
def test_an_agent_or_system_key_reads_the_offer_but_may_not_continue(env, who):
    pause = _pause(env)
    principal = {"seated_agent_key": lambda: _agent_key("agent-a"),
                 "system_key": _system_key}[who]()

    # A member sees the offer (who was next is transcript, not privileged)...
    assert _get(env, principal).json()["paused_chain"] == {"seq": pause["seq"], "next": ["agent-a"]}

    # ...but only a person may act on it.
    r = _continue(env, principal, pause["seq"])
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "not_a_person"
    assert _of_kind(env, "chain_continued") == []
    assert len(env.calls) == 2
    assert _get(env, BOB()).json()["paused_chain"] == {"seq": pause["seq"], "next": ["agent-a"]}


def test_a_non_member_gets_the_uniform_404_and_claims_no_key(env):
    pause = _pause(env)
    seen = len(env.claims)

    ghost = _continue(env, CAROL(), pause["seq"], room_id=GHOST_ROOM, key="k-outsider")
    real = _continue(env, CAROL(), pause["seq"], key="k-outsider")

    assert ghost.status_code == real.status_code == 404
    assert ghost.json() == real.json() == {
        "detail": {"code": "room_not_found", "message": "Room not found"}}
    assert env.claims[seen:] == []
    assert _of_kind(env, "chain_continued") == [] and len(env.calls) == 2


def test_continuer_who_cannot_reach_next_is_refused_and_nothing_is_consumed(env):
    pause = _pause(env)
    env.gate["deny"].add("agent-a")

    r = _continue(env, BOB(), pause["seq"])

    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "agent_not_accessible"
    assert _of_kind(env, "chain_continued") == []
    assert len(env.calls) == 2
    assert _get(env, BOB()).json()["paused_chain"] == {"seq": pause["seq"], "next": ["agent-a"]}


def test_partly_reachable_next_wakes_who_it_can_and_names_the_rest(env):
    trio = rooms_service.create_room(BOB(), "Trio", ["agent-a", "agent-b", "agent-c"],
                                     ttl_hours=0)["id"]
    env.monkeypatch.setattr(rooms_service, "ROOM_MAX_CHAIN_DEPTH", 1)
    env.replies["agent-c"] = "@agent-a @agent-b please both review"
    assert _post_message(env, BOB(), "@agent-c go", room_id=trio).status_code == 200
    (pause,) = _of_kind(env, "chain_paused", trio)
    _quiet(env)
    env.gate["deny"].add("agent-b")

    r = _continue(env, BOB(), pause["seq"], room_id=trio)

    assert r.status_code == 200, r.text
    assert r.json()["woke"] == ["agent-a"]
    (receipt,) = _of_kind(env, "chain_continued", trio)
    assert receipt["content"] == "bob asked agent-a to carry on."
    assert any("agent-b could not be reached" in m["content"] for m in _rows(env, trio)
               if m["seq"] > receipt["seq"])
    assert [c["agent_name"] for c in env.calls] == ["agent-c", "agent-a"]


def test_no_one_left_to_wake_is_named(env):
    pause = _pause(env)
    rooms_service.remove_participant(BOB(), env.room_id, "agent-a")

    r = _continue(env, BOB(), pause["seq"])

    assert (r.status_code, r.json()["detail"]["code"]) == (409, "no_one_to_wake")
    assert _of_kind(env, "chain_continued") == [] and len(env.calls) == 2


# --------------------------------------------------------------------------- #
# Budgets still apply, and a refusal says why
# --------------------------------------------------------------------------- #

def test_a_closed_room_refuses_with_its_reason(env):
    pause = _pause(env)
    rooms_service.close_room(BOB(), env.room_id)

    r = _continue(env, BOB(), pause["seq"])

    assert r.status_code == 410, r.text
    assert r.json()["detail"] == {"code": "room_closed", "message": "This room is closed",
                                  "stop_reason": "user_closed"}
    assert _of_kind(env, "chain_continued") == [] and len(env.calls) == 2


def test_a_room_at_its_message_budget_refuses_and_says_which_budget(env):
    pause = _pause(env)
    env.monkeypatch.setattr(rooms_db, "count_budget_messages", lambda _room_id: 10 ** 6)

    r = _continue(env, BOB(), pause["seq"])

    assert r.status_code == 410, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "room_closed" and detail["stop_reason"] == "max_messages"
    assert "message budget" in detail["message"]
    assert _of_kind(env, "chain_continued") == [] and len(env.calls) == 2


# --------------------------------------------------------------------------- #
# Once per stop: a double click, two workers, a restart
# --------------------------------------------------------------------------- #

def test_double_click_with_a_key_is_409_then_a_replay_and_one_wake(env):
    pause = _pause(env)
    _quiet(env)
    hold = env.holds["agent-a"] = {"started": threading.Event(), "release": threading.Event()}
    first_client = TestClient(env.app)
    t, out = _in_thread(lambda: _continue(env, BOB(), pause["seq"], key="k-click",
                                          client=first_client))
    assert hold["started"].wait(10), "the first Continue never reached the wake"

    second = _continue(env, BOB(), pause["seq"], key="k-click")
    assert second.status_code == 409, second.text
    assert second.json()["detail"]["error"] == "request_in_progress"

    hold["release"].set()
    t.join(10)
    assert "error" not in out, out.get("error")
    first = out["value"]
    assert first.status_code == 200, first.text

    third = _continue(env, BOB(), pause["seq"], key="k-click")
    assert third.status_code == 200, third.text
    assert third.json() == {**first.json(), "replayed": True}
    assert len(_of_kind(env, "chain_continued")) == 1
    assert [c["agent_name"] for c in env.calls[2:]] == ["agent-a"]
    # The key lives under the route's own scope, never the /messages one.
    with env.engine.connect() as conn:
        scopes = {r[0] for r in conn.execute(select(env.keys.c.scope))}
    assert scopes == {f"room-continue:{env.room_id}"}


def test_two_workers_without_a_key_wake_once(env):
    """Both requests pass every check the layer above the receipt can run (no
    key, so the idempotency layer is disabled for both); the second finds the
    first's receipt and is told the room moved on. One wake."""
    pause = _pause(env)
    _quiet(env)
    hold = env.holds["agent-a"] = {"started": threading.Event(), "release": threading.Event()}
    first_client = TestClient(env.app)
    t, out = _in_thread(lambda: _continue(env, BOB(), pause["seq"], client=first_client))
    assert hold["started"].wait(10), "the first Continue never reached the wake"

    second = _continue(env, BOB(), pause["seq"])
    assert second.status_code == 409, second.text
    assert second.json()["detail"]["code"] == "room_moved_on"

    hold["release"].set()
    t.join(10)
    assert "error" not in out, out.get("error")
    assert out["value"].status_code == 200
    assert out["value"].json()["woke"] == ["agent-a"]
    assert len(_of_kind(env, "chain_continued")) == 1
    assert [c["agent_name"] for c in env.calls[2:]] == ["agent-a"]
    assert env.claims == []


def test_the_receipt_insert_is_the_guard_not_the_check(env):
    """A stale `offer_stands` read (what a second worker sees across the await)
    does not wake twice: the receipt insert carries the same predicate."""
    pause = _pause(env)
    _quiet(env)
    env.monkeypatch.setattr(rooms_db, "offer_stands", lambda *_a, **_k: True)

    assert _continue(env, BOB(), pause["seq"]).status_code == 200
    r = _continue(env, BOB(), pause["seq"])

    assert (r.status_code, r.json()["detail"]["code"]) == (409, "room_moved_on")
    assert len(_of_kind(env, "chain_continued")) == 1
    assert [c["agent_name"] for c in env.calls[2:]] == ["agent-a"]


def test_simultaneous_receipt_inserts_land_exactly_one(env):
    """The db layer under two writers hitting the same pause at the same instant:
    exactly one receipt row, exactly one caller told it won."""
    pause = _pause(env)
    barrier = threading.Barrier(2)
    results = {}

    def attempt(tag):
        barrier.wait(5)
        results[tag] = rooms_db.append_continue_receipt(
            f"msg-{tag}", env.room_id, pause["seq"], f"{tag} asked agent-a to carry on.",
            ["agent-a"], NOW)

    threads = [threading.Thread(target=attempt, args=(tag,)) for tag in ("one", "two")]
    for th in threads:
        th.start()
    for th in threads:
        th.join(30)

    assert sorted(results) == ["one", "two"]
    won = [tag for tag, seq in results.items() if seq is not None]
    assert len(won) == 1, results
    (receipt,) = _of_kind(env, "chain_continued")
    assert receipt["seq"] == results[won[0]] and receipt["seq"] == pause["seq"] + 1
    assert rooms_db.offer_stands(env.room_id, pause["seq"]) is False


def test_after_a_restart_a_repeated_continue_has_moved_on(env):
    pause = _pause(env)
    _quiet(env)
    assert _continue(env, BOB(), pause["seq"], key="k-restart").status_code == 200
    with env.engine.begin() as conn:            # the key window is gone with the restart
        conn.execute(delete(env.keys))

    r = _continue(env, BOB(), pause["seq"], key="k-restart")

    assert (r.status_code, r.json()["detail"]["code"]) == (409, "room_moved_on")
    assert len(_of_kind(env, "chain_continued")) == 1
    assert [c["agent_name"] for c in env.calls[2:]] == ["agent-a"]
