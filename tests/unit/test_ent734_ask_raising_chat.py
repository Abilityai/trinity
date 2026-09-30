"""An ask raised during a chat turn attaches to THAT chat, not Main (ent#734).

ent#429 gave every addressed ask a chat at raise time, and ent#523 made that
chat the pair's Main — because nothing at raise time knew which execution
raised the ask. The native path does know: the MCP call carries the platform's
`X-Trinity-Execution-Id` (#2392), and every Workspace turn's execution row is
stamped at creation with the chat it serves (`source_channel="portal"`,
`source_channel_chat_id`, `source_channel_client` — ent#457/#2426). So the link
is read, not inferred, and needs no new column.

The ent#610 amendment (2026-09-30) splits asks in two:

* raised by the execution serving a Workspace chat turn → that chat;
* raised by anything else (a schedule — even one delivering INTO the
  Workspace, a loop, a gate, no or an unknown execution) → Main, unchanged.

And the ent#429 rule stands: the chat is platform-written. An agent can neither
name one in `context` nor, by citing an execution, place an ask in a chat that
belongs to a different person than the one it asks.

Requirement: docs/memory/requirements/security.md §26 (ent#429 / ent#734)
Harness: the ent#611 `ask` fixture over the real per-process SQLite, with the
REAL `_workspace_thread_for` restored (the fixture stubs it). Agent names are
unique to this file.
"""

from __future__ import annotations

import logging
import uuid

import pytest

from unit.test_ent611_native_ask import ask, real_db, _body, OWNER  # noqa: F401 — fixtures

import services.operator_queue_service as oqs

pytestmark = pytest.mark.unit

# Captured at import, before the `ask` fixture swaps in its stub.
_REAL_THREAD_FOR = oqs._workspace_thread_for

OTHER = "someone-else-734@example.com"


@pytest.fixture()
def world(ask, monkeypatch):
    """The `ask` fixture, with the real chat resolution put back and a fresh
    agent name per test (the database outlives the test, and Main is unique
    per pair)."""
    monkeypatch.setattr(oqs, "_workspace_thread_for", _REAL_THREAD_FOR)
    ask.agent = f"agent-734-{uuid.uuid4().hex[:8]}"
    return ask


def _chat(agent, email):
    from client_portal import db as portal_db

    sid = uuid.uuid4().hex
    portal_db.create_portal_session(sid, agent, email, "2026-09-30T00:00:00Z")
    return sid


def _main(agent, email):
    from client_portal.service import ensure_main_session

    return ensure_main_session(agent, email)


def _execution(
    real_db, agent, *, triggered_by="public", chat=None, client=OWNER, channel="portal"
):
    """An execution row shaped like the one its creation site writes."""
    from db.write_params import TaskExecutionFields

    fields = TaskExecutionFields(
        source_user_email=client,
        source_channel=channel if chat else None,
        source_channel_chat_id=chat,
        source_channel_client=client if chat else None,
    )
    return real_db.create_task_execution(
        agent, "hi", triggered_by=triggered_by, fields=fields
    ).id


def _raise(ask, rid, **kw):
    return ask.svc.raise_ask(
        ask.agent,
        _body(rid, **kw.pop("body", {})),
        raised_by="agent",
        channel="mcp",
        **kw,
    )


def _thread(ask, receipt):
    return (ask.db.get_operator_queue_item(receipt["id"])["context"] or {}).get(
        "workspace_session_id"
    )


def test_an_ask_from_a_chat_turn_lands_in_that_chat(world, real_db):
    """AC 1 / AC 5 — the chat, and NOT Main (Main exists, so a Main answer is a
    real wrong answer rather than a coincidence)."""
    main = _main(world.agent, OWNER)
    chat = _chat(world.agent, OWNER)
    turn = _execution(real_db, world.agent, chat=chat)

    r = _raise(world, "t734-chat", platform_execution_id=turn)

    assert _thread(world, r) == chat != main


def test_the_same_call_from_a_scheduled_run_keeps_main(world, real_db):
    """AC 5's second half — shaped like a schedule that DELIVERS into the
    Workspace (`schedule_workspace_delivery` stamps the portal destination on a
    schedule row), which is the case a channel-only check would get wrong."""
    chat = _chat(world.agent, OWNER)
    run = _execution(real_db, world.agent, triggered_by="schedule", chat=chat)

    r = _raise(world, "t734-sched", platform_execution_id=run)

    assert _thread(world, r) == _main(world.agent, OWNER) != chat


@pytest.mark.parametrize("trigger", ["loop", "agent", "mcp", "room"])
def test_no_other_trigger_attaches_to_a_chat(world, real_db, trigger):
    chat = _chat(world.agent, OWNER)
    run = _execution(real_db, world.agent, triggered_by=trigger, chat=chat)

    r = _raise(world, f"t734-{trigger}", platform_execution_id=run)

    assert _thread(world, r) == _main(world.agent, OWNER)


@pytest.mark.parametrize("platform", [None, "manual", "exec-unknown-734"])
def test_no_usable_execution_keeps_main(world, platform):
    r = _raise(world, f"t734-none-{platform}", platform_execution_id=platform)

    assert _thread(world, r) == _main(world.agent, OWNER)


def test_another_agents_chat_turn_is_not_a_link(world, real_db):
    """`_platform_turn` already refuses a foreign execution; pinned here so the
    chat lookup can never be reached with one."""
    other_agent = f"{world.agent}-x"
    chat = _chat(other_agent, OWNER)
    turn = _execution(real_db, other_agent, chat=chat)

    r = _raise(world, "t734-foreign", platform_execution_id=turn)

    assert _thread(world, r) == _main(world.agent, OWNER)


def test_another_addressees_chat_never_becomes_the_link(world, real_db):
    """The turn is for OTHER; the ask is addressed to OWNER. Attaching it to
    OTHER's chat would show OWNER's question in someone else's conversation."""
    chat = _chat(world.agent, OTHER)
    turn = _execution(real_db, world.agent, chat=chat, client=OTHER)

    r = _raise(world, "t734-addr", platform_execution_id=turn)

    assert _thread(world, r) == _main(world.agent, OWNER) != chat


def test_a_chat_stamp_naming_someone_elses_session_is_not_trusted(world, real_db):
    """The row's client matches the addressee but its chat id is a session of
    another pair — the session itself must belong to (agent, addressee)."""
    chat = _chat(world.agent, OTHER)
    turn = _execution(real_db, world.agent, chat=chat, client=OWNER)

    r = _raise(world, "t734-session", platform_execution_id=turn)

    assert _thread(world, r) == _main(world.agent, OWNER) != chat


def test_an_agent_supplied_chat_is_still_stripped(world, real_db):
    """AC 4 — even alongside a real chat turn, the agent's value never wins."""
    chat = _chat(world.agent, OWNER)
    turn = _execution(real_db, world.agent, chat=chat)
    planted = _chat(world.agent, OWNER)

    r = _raise(
        world,
        "t734-planted",
        platform_execution_id=turn,
        body={"context": {"workspace_session_id": planted}},
    )

    assert _thread(world, r) == chat


def test_an_agent_cited_execution_is_not_a_link(world, real_db):
    """Only the PLATFORM's execution id is read. An agent writing a chat turn's
    id into `context.execution_id` names a chat by proxy — it gets Main."""
    chat = _chat(world.agent, OWNER)
    turn = _execution(real_db, world.agent, chat=chat)

    r = _raise(world, "t734-cited", body={"context": {"execution_id": turn}})

    assert _thread(world, r) == _main(world.agent, OWNER)


def test_a_lookup_failure_falls_back_to_main_with_a_warning(
    world, real_db, monkeypatch, caplog
):
    """Fail-soft (the #1632 clamp contract): a link we could not resolve costs a
    click, never the ask."""
    import client_portal.service as portal_service

    chat = _chat(world.agent, OWNER)
    turn = _execution(real_db, world.agent, chat=chat)

    def _boom(*a, **k):
        raise RuntimeError("executions table down")

    monkeypatch.setattr(portal_service, "chat_for_execution", _boom)
    with caplog.at_level(logging.WARNING, logger=oqs.logger.name):
        r = _raise(world, "t734-boom", platform_execution_id=turn)

    assert _thread(world, r) == _main(world.agent, OWNER)
    assert any("ent#734" in rec.getMessage() for rec in caplog.records)


def test_a_file_ingested_ask_keeps_main(world, real_db, monkeypatch):
    """The file path has no platform-known execution — only what the agent
    wrote — so it stays on Main whatever it cites."""
    chat = _chat(world.agent, OWNER)
    turn = _execution(real_db, world.agent, chat=chat)
    monkeypatch.setattr(oqs, "_validated_addressee", lambda agent, raw: raw)

    out = oqs._clamp_ingested_item(
        {"id": "f734", "title": "t", "question": "q", "addressed_to_email": OWNER,
         "context": {"execution_id": turn}},
        world.agent,
    )

    assert out["context"]["workspace_session_id"] == _main(world.agent, OWNER)


def test_a_gate_raise_keeps_main_even_with_a_chat_turn(world, real_db):
    """A gate's ask is a background ask (the ent#610 amendment): Inbox only,
    Main as the reply target — even when the platform hands it a chat turn."""
    chat = _chat(world.agent, OWNER)
    turn = _execution(real_db, world.agent, chat=chat)

    r = world.svc.raise_ask(world.agent, _body("gate-t734"), raised_by="gate", channel="gate",
                            addressee=OWNER, platform_execution_id=turn)

    assert _thread(world, r) == _main(world.agent, OWNER) != chat


def test_a_public_turn_on_another_channel_is_not_a_workspace_chat(world, real_db):
    """`public` is shared with public links and x402 chat, so the trigger alone
    does not say "Workspace". A chat id stamped under another channel is that
    channel's conversation, even when it happens to equal a session id."""
    chat = _chat(world.agent, OWNER)
    turn = _execution(real_db, world.agent, chat=chat, channel="slack")

    r = _raise(world, "t734-slack", platform_execution_id=turn)

    assert _thread(world, r) == _main(world.agent, OWNER) != chat


def test_a_finished_turn_is_not_a_link(world, real_db):
    """cso r1: the header is platform-set but the agent PROCESS can forge it with
    its own key. An ask can only be raised by a turn that is still running, so a
    finished turn of another chat (same addressee) must not attach the ask there."""
    old_chat = _chat(world.agent, OWNER)
    old_turn = _execution(real_db, world.agent, chat=old_chat)
    real_db.update_execution_status(old_turn, "success")

    r = _raise(world, "t734-stale", platform_execution_id=old_turn)

    assert _thread(world, r) == _main(world.agent, OWNER) != old_chat


def test_a_running_turn_in_an_archived_chat_keeps_that_chat(world, real_db):
    """review r1 N4: a reset Main stays listed, readable and RESUMABLE
    (`reset_main_session`), and Reset is refused mid-turn — so a running turn in
    an archived chat is someone talking in a chat they can see, and its ask
    belongs there, not in the new Main."""
    from client_portal import db as portal_db
    old_main = _main(world.agent, OWNER)
    new_main = uuid.uuid4().hex
    assert portal_db.archive_main_and_mint(world.agent, OWNER, main_id=old_main, new_id=new_main,
                                           now="2026-09-30T01:00:00Z")
    turn = _execution(real_db, world.agent, chat=old_main)

    r = _raise(world, "t734-archived", platform_execution_id=turn)

    assert _thread(world, r) == old_main != _main(world.agent, OWNER)


def test_the_addressee_match_ignores_email_case(world, real_db, monkeypatch):
    """review r1 N1: the row keeps the client's email as the turn was started;
    the addressee resolves separately. Case must not decide the link."""
    import services.ask_service as svc
    mixed = "Owner-734@Example.COM"
    monkeypatch.setattr(svc, "_owner_email", lambda agent: mixed)
    chat = _chat(world.agent, mixed.lower())
    turn = _execution(real_db, world.agent, chat=chat, client=mixed.lower())   # sides differ in case

    r = _raise(world, "t734-case", platform_execution_id=turn)

    assert _thread(world, r) == chat
