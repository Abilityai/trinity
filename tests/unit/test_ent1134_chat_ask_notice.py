"""An ask a chat's turn raised that FELL to the operators shows in that chat as
one content-free line (abilityai/trinity-enterprise#1134, AC4).

The owner stands in for an unassigned primary (ruling 1), so what still falls
to the Operating Room from a `to: primary` ask is: nobody is assigned AND the
owner has no email, or the agent has no owner. The person in the chat used to
see nothing — the agent said "I asked" and the ask was invisible to them.

Pinned here:

* the sink stamps the chat keys on an in-turn UNADDRESSED `primary` ask
  (`client_portal.service.chat_of_turn` — no email to match, the turn's own
  client) and records WHY it fell (`workspace_operators_reason`, a platform key
  an agent cannot author);
* the chat read (`list_asks_page(chat_id=)`) carries one row `notice: true`
  with `chat_id, kind, status, created_at, destination` — never a title,
  question, options, proposal or context — to the chat's OWNER only;
* absent in another chat, for another person reading the same chat id, for a
  resolved primary (their normal ask row is unchanged) and for an explicit
  `to: operator`;
* the real route serialises it (its body is a list of asks; a notice row is
  not one, so the response model must admit it).

`destination` is the field ent#1135 reuses for the room line:
`{"kind": "operators", "reason": "owner_no_email" | "no_owner"}` here,
`{"kind": "project", "seat": "steward" | "creator"}` on a project ask.
"""
from __future__ import annotations

import uuid

import pytest

from unit.test_ent611_native_ask import ask, real_db, _body, _seed_owner, OWNER  # noqa: F401 — fixtures

import services.operator_queue_service as oqs

pytestmark = pytest.mark.unit

_REAL = {name: getattr(oqs, name, None) for name in ("_workspace_thread_for", "_workspace_attachment")}

PERSON = "person-1134n@example.com"
OTHER = "other-1134n@example.com"
CONTENT = {"title", "question", "options", "proposal", "context", "priority"}


@pytest.fixture()
def world(ask, monkeypatch):
    for name, real in _REAL.items():
        if real is not None:
            monkeypatch.setattr(oqs, name, real)
    import client_portal.service as portal_service
    monkeypatch.setattr(portal_service, "agent_on_roster", lambda agent, email, include_owned=False: True)
    ask.agent = f"agent-1134n-{uuid.uuid4().hex[:8]}"
    ask.state["owner"] = None     # nobody stands in: the ask falls to the operators
    return ask


def _chat(agent, email):
    from client_portal import db as portal_db
    sid = uuid.uuid4().hex
    portal_db.create_portal_session(sid, agent, email, "2026-11-07T00:00:00Z")
    return sid


def _turn(real_db, agent, chat, client=PERSON, triggered_by="public"):
    from db.write_params import TaskExecutionFields
    fields = TaskExecutionFields(source_user_email=client, source_channel="portal",
                                 source_channel_chat_id=chat, source_channel_client=client)
    return real_db.create_task_execution(agent, "hi", triggered_by=triggered_by, fields=fields).id


def _raise(world, rid, turn, **body):
    return world.svc.raise_ask(world.agent, _body(rid, **body), raised_by="agent",
                               channel="mcp", platform_execution_id=turn)


def _read(email, chat):
    from client_portal.asks import service
    return service.list_asks_page(email, is_platform=False, chat_id=chat).items


def _notices(rows):
    return [r for r in rows if getattr(r, "notice", False) is True]


def test_a_fallen_through_ask_is_one_content_free_notice_in_its_own_chat(world, real_db):
    chat = _chat(world.agent, PERSON)
    r = _raise(world, "n1134-fell", _turn(real_db, world.agent, chat))
    assert r["resolved"] is False

    notes = _notices(_read(PERSON, chat))
    assert [n.id for n in notes] == [r["id"]]
    row = notes[0].model_dump()
    assert row["notice"] is True
    assert row["chat_id"] == chat
    assert row["agent_name"] == world.agent
    assert (row["kind"], row["status"]) == ("approval", "pending")
    assert row["created_at"]
    assert row["destination"] == {"kind": "operators", "reason": "no_owner"}
    assert not CONTENT & set(row), f"content crossed: {CONTENT & set(row)}"


def test_an_owner_without_an_email_is_named_as_the_reason(world, real_db, monkeypatch):
    monkeypatch.setattr(world.svc, "_owner_email", world.real_owner_email)
    _seed_owner(world.db, world.agent, f"owner-1134n-{world.agent[-8:]}", None)
    chat = _chat(world.agent, PERSON)
    r = _raise(world, "n1134-noemail", _turn(real_db, world.agent, chat))

    notes = _notices(_read(PERSON, chat))
    assert [n.id for n in notes] == [r["id"]]
    assert notes[0].destination == {"kind": "operators", "reason": "owner_no_email"}


def test_absent_in_another_chat_and_for_another_reader(world, real_db):
    chat = _chat(world.agent, PERSON)
    _raise(world, "n1134-own", _turn(real_db, world.agent, chat))

    assert _notices(_read(PERSON, _chat(world.agent, PERSON))) == []
    # the same chat id, read by someone whose chat it is not
    assert _notices(_read(OTHER, chat)) == []


def test_a_resolved_primary_gets_the_normal_ask_and_no_notice(world, real_db):
    world.state["owner"] = PERSON
    chat = _chat(world.agent, PERSON)
    r = _raise(world, "n1134-resolved", _turn(real_db, world.agent, chat))
    assert r["resolved"] is True

    rows = _read(PERSON, chat)
    assert _notices(rows) == []
    asks = [a for a in rows if a.id == r["id"]]
    assert len(asks) == 1 and asks[0].title == "Pay invoice" and asks[0].destination is None


def test_an_explicit_operator_ask_is_never_shown(world, real_db):
    chat = _chat(world.agent, PERSON)
    _raise(world, "n1134-op", _turn(real_db, world.agent, chat), to="operator")

    assert _notices(_read(PERSON, chat)) == []


def test_a_background_run_leaves_no_notice(world, real_db):
    """A schedule delivering into the Workspace is not a chat turn (ent#610)."""
    chat = _chat(world.agent, PERSON)
    _raise(world, "n1134-sched", _turn(real_db, world.agent, chat, triggered_by="schedule"))

    assert _notices(_read(PERSON, chat)) == []


def test_an_agent_cannot_author_the_reason(world, real_db):
    world.state["owner"] = PERSON
    chat = _chat(world.agent, PERSON)
    r = _raise(world, "n1134-forged", _turn(real_db, world.agent, chat),
               context={"workspace_operators_reason": "no_owner"})

    stored = world.db.get_operator_queue_item(r["id"])["context"] or {}
    assert "workspace_operators_reason" not in stored
    assert _notices(_read(PERSON, chat)) == []


def test_the_route_serialises_the_notice_without_content(world, real_db):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from client_portal.asks.router import router
    from client_portal.portal_auth import PortalPrincipal, get_portal_principal

    chat = _chat(world.agent, PERSON)
    r = _raise(world, "n1134-route", _turn(real_db, world.agent, chat))

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_portal_principal] = lambda: PortalPrincipal(PERSON, False)
    res = TestClient(app).get(router.prefix, params={"chat_id": chat})

    assert res.status_code == 200, res.text
    notes = [row for row in res.json() if row.get("notice") is True]
    assert [n["id"] for n in notes] == [r["id"]]
    assert notes[0]["destination"] == {"kind": "operators", "reason": "no_owner"}
    assert not CONTENT & set(notes[0])
