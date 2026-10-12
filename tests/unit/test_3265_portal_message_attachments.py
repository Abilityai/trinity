"""#3265 — a sent Workspace message keeps what was attached to it.

The composer uploads a file on attach, then clears the chip when the turn goes
out, and nothing about the attachment reached the user's row. So the thread
showed only the typed text, and a reload could not show the file either.

Under test:

* `resolve_turn_attachments` keeps a successful entry only when the filename is
  in the caller's own uploads to this agent, with size and type from there; a
  failed upload keeps its name and reason; a name not in the uploads is recorded
  as failed, never trusted; duplicates collapse; nothing attached → None;
* the user row stores the JSON (real writer, real SQLite), and the history route
  returns it as a list on that message — the reload path;
* the streaming route resolves the request's attachments and hands them to the
  turn, so the row the turn writes carries them.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

CLIENT = "client@example.com"
AGENT = "scribe"
THREAD = "thread-3265"

INBOX = [
    {"filename": "q3-chart.png", "size_bytes": 830, "uploaded_at": None, "mime_type": "image/png"},
    {"filename": "notes.pdf", "size_bytes": 12000, "uploaded_at": None, "mime_type": "application/pdf"},
]


def _req(**kw):
    from client_portal.models import PortalTurnAttachment
    return PortalTurnAttachment(**kw)


@pytest.fixture()
def svc(monkeypatch):
    from client_portal import service

    async def _inbox(agent_name, email):
        assert (agent_name, email) == (AGENT, CLIENT), "the CALLER's own uploads to THIS agent"
        return [dict(i) for i in INBOX]

    monkeypatch.setattr(service, "_read_inbox_or_none", _inbox)
    return service


def _resolve(svc, requested):
    raw = asyncio.run(svc.resolve_turn_attachments(AGENT, CLIENT, requested))
    return None if raw is None else json.loads(raw)


# --- resolution ---------------------------------------------------------------

def test_an_uploaded_file_is_kept_with_the_servers_size_and_type(svc):
    assert _resolve(svc, [_req(filename="q3-chart.png")]) == [
        {"filename": "q3-chart.png", "size_bytes": 830, "mime_type": "image/png"},
    ]


def test_a_name_not_in_the_callers_uploads_is_never_trusted(svc):
    out = _resolve(svc, [_req(filename="someone-elses.png")])
    assert out == [{"filename": "someone-elses.png", "failed": True,
                    "error": svc._NOT_IN_UPLOADS}]


def test_a_failed_upload_is_recorded_with_its_reason(svc):
    out = _resolve(svc, [_req(filename="big.mov", failed=True, error="Too large (40 MB).")])
    assert out == [{"filename": "big.mov", "failed": True, "error": "Too large (40 MB)."}]
    out = _resolve(svc, [_req(filename="x.bin", failed=True)])
    assert out[0]["error"] == svc._FAILED_UPLOAD


def test_order_is_kept_and_duplicates_collapse(svc):
    out = _resolve(svc, [_req(filename="notes.pdf"), _req(filename="q3-chart.png"),
                         _req(filename="notes.pdf")])
    assert [a["filename"] for a in out] == ["notes.pdf", "q3-chart.png"]


@pytest.mark.parametrize("requested", [None, []])
def test_nothing_attached_stores_nothing(svc, requested):
    assert _resolve(svc, requested) is None


def test_the_request_keeps_the_first_20_rather_than_refusing_the_turn():
    """Review: a 422 here fails a message that sent fine before #3265, and
    Retry resends the same list. The bound trims; it never rejects."""
    from pydantic import ValidationError
    from client_portal.models import PortalChatRequest, MAX_TURN_ATTACHMENTS
    body = PortalChatRequest(message="hi", attachments=[{"filename": f"f{i}"} for i in range(22)])
    assert [a.filename for a in body.attachments] == [f"f{i}" for i in range(MAX_TURN_ATTACHMENTS)]
    with pytest.raises(ValidationError):
        PortalChatRequest(message="hi", attachments=[{"filename": ""}])


def test_a_long_upload_error_is_trimmed_not_rejected():
    from client_portal.models import PortalChatRequest
    body = PortalChatRequest(message="hi", attachments=[
        {"filename": "big.mov", "failed": True, "error": "x" * 1000}])
    assert body.attachments[0].error == "x" * 300


def test_uploads_that_could_not_be_read_are_not_recorded_as_missing(monkeypatch):
    """Review (the #2196 collapse): a Docker/agent read failure used to store
    every real attachment as permanently "not in your uploads". Unreadable is
    not absent — the name is kept, without a size or type to vouch for."""
    from client_portal import service

    async def _unreadable(agent_name, email):
        return None

    monkeypatch.setattr(service, "_read_inbox_or_none", _unreadable)
    out = json.loads(asyncio.run(service.resolve_turn_attachments(
        AGENT, CLIENT, [_req(filename="q3-chart.png"), _req(filename="big.mov", failed=True)])))
    assert out == [{"filename": "q3-chart.png"},
                   {"filename": "big.mov", "failed": True, "error": service._FAILED_UPLOAD}]


def test_an_agent_that_is_not_running_reads_as_unreadable(monkeypatch):
    from client_portal import service
    import services.docker_service as ds
    monkeypatch.setattr(ds, "get_agent_container", lambda name: None)
    assert asyncio.run(service._read_inbox_or_none(AGENT, CLIENT)) is None
    assert asyncio.run(service._read_inbox(AGENT, CLIENT)) == []


@pytest.mark.parametrize("raw,expected", [
    (None, None), ("", None), ("not json", None), ('{"filename": "a"}', None), ("[]", None),
    ('[{"filename": "a.png"}, "junk", {"no": "name"}]', [{"filename": "a.png"}]),
])
def test_decode_tolerates_whatever_the_column_holds(raw, expected):
    from client_portal import service
    assert service.decode_turn_attachments(raw) == expected


# --- persistence + the reload path --------------------------------------------

@pytest.fixture()
def portal_db(tmp_path, monkeypatch):
    db_file = tmp_path / "trinity-3265.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_file))
    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", str(db_file))
    from db.engine import get_engine
    from db.tables import metadata, enterprise_portal_messages, enterprise_portal_sessions
    metadata.create_all(get_engine(), tables=[enterprise_portal_messages, enterprise_portal_sessions])
    from client_portal import db as pdb
    from utils.helpers import utc_now_iso
    pdb.create_portal_session(THREAD, AGENT, CLIENT, utc_now_iso())
    return pdb


def _history_client(monkeypatch):
    from client_portal import service
    monkeypatch.setattr(service, "agent_on_roster", lambda a, e, include_owned=False: True)
    monkeypatch.setattr(service, "_attach_own_ratings", lambda *a, **kw: None)
    monkeypatch.setattr(service, "get_turn_inflight", lambda sid: None)
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from client_portal import router
    from client_portal.portal_auth import PortalPrincipal, get_portal_principal
    app = FastAPI()
    app.include_router(router.router)
    app.dependency_overrides[get_portal_principal] = lambda: PortalPrincipal(email=CLIENT, is_platform=False)
    return TestClient(app)


def test_the_user_row_keeps_its_attachments_and_a_reload_shows_them(portal_db, svc, monkeypatch):
    stored = asyncio.run(svc.resolve_turn_attachments(
        AGENT, CLIENT, [_req(filename="q3-chart.png"), _req(filename="big.mov", failed=True, error="Too large.")]))
    svc._persist_user_turn(AGENT, CLIENT, THREAD, "here is the chart", attachments=stored)
    from utils.helpers import utc_now_iso
    portal_db.add_portal_message("m-reply", AGENT, CLIENT, "assistant", "four bars", None,
                                 utc_now_iso(), session_id=THREAD)

    r = _history_client(monkeypatch).get(f"/api/enterprise/client-portal/agents/{AGENT}/history",
                                         params={"session_id": THREAD})
    assert r.status_code == 200, r.text
    msgs = r.json()["messages"]
    user = next(m for m in msgs if m["role"] == "user")
    reply = next(m for m in msgs if m["role"] == "assistant")
    assert user["attachments"] == [
        {"filename": "q3-chart.png", "size_bytes": 830, "mime_type": "image/png", "failed": False, "error": None},
        {"filename": "big.mov", "size_bytes": None, "mime_type": None, "failed": True, "error": "Too large."},
    ]
    assert reply["attachments"] is None


def test_a_turn_without_attachments_reads_back_none(portal_db, svc, monkeypatch):
    svc._persist_user_turn(AGENT, CLIENT, THREAD, "just text")
    r = _history_client(monkeypatch).get(f"/api/enterprise/client-portal/agents/{AGENT}/history",
                                         params={"session_id": THREAD})
    assert r.json()["messages"][0]["attachments"] is None


# --- the streaming route hands them to the turn --------------------------------

def test_the_streaming_route_resolves_and_forwards_the_attachments(svc, monkeypatch):
    seen = {}

    async def _start(agent_name, message, email, **kw):
        seen.update(kw)
        return {"execution_id": "exec-1", "session_id": THREAD}

    monkeypatch.setattr(svc, "start_portal_turn", _start)
    monkeypatch.setattr(svc, "reply_context", lambda *a, **kw: "")
    monkeypatch.setattr(svc, "agent_on_roster", lambda a, e, include_owned=False: True)
    client = _history_client(monkeypatch)
    r = client.post(f"/api/enterprise/client-portal/agents/{AGENT}/chat/stream",
                    json={"message": "see attached", "session_id": THREAD,
                          "attachments": [{"filename": "q3-chart.png"}, {"filename": "nope.txt"}]})
    assert r.status_code == 202, r.text
    assert json.loads(seen["attachments"]) == [
        {"filename": "q3-chart.png", "size_bytes": 830, "mime_type": "image/png"},
        {"filename": "nope.txt", "failed": True, "error": svc._NOT_IN_UPLOADS},
    ]


def test_the_sync_chat_route_resolves_and_forwards_the_attachments(svc, monkeypatch):
    """Review: with the sync route's `attachments=` removed every test still
    passed. The inline path stores the row too, so it is pinned the same way."""
    seen = {}

    async def _chat(agent_name, message, email, **kw):
        seen.update(kw)
        return {"response": "ok", "session_id": THREAD}

    monkeypatch.setattr(svc, "portal_chat", _chat)
    monkeypatch.setattr(svc, "reply_context", lambda *a, **kw: "")
    client = _history_client(monkeypatch)
    client.post(f"/api/enterprise/client-portal/agents/{AGENT}/chat",
                json={"message": "see attached", "session_id": THREAD,
                      "attachments": [{"filename": "notes.pdf"}]})
    assert "attachments" in seen, "the sync route never reached portal_chat"
    assert json.loads(seen["attachments"]) == [
        {"filename": "notes.pdf", "size_bytes": 12000, "mime_type": "application/pdf"},
    ]


# --- the two hops between the route and the row --------------------------------

class _Stop(Exception):
    pass


def test_start_portal_turn_hands_the_attachments_to_the_turn(svc, monkeypatch):
    """Hop 1 (review): start_portal_turn → portal_chat."""
    from types import SimpleNamespace
    import database
    import services.session_turn_service as sts
    import services.pull_pilot as pp

    seen = {}

    async def _portal_chat(agent_name, message, email, **kw):
        seen.update(kw)
        return {}

    async def _available(name):
        return "running"

    monkeypatch.setattr(svc, "agent_on_roster", lambda a, e, include_owned=False: True)
    monkeypatch.setattr(svc, "_agent_availability", _available)
    monkeypatch.setattr(svc, "_availability_allows_turn", lambda a: True)
    monkeypatch.setattr(svc, "resolve_turn_model", lambda a, m: None)
    monkeypatch.setattr(svc, "_resolve_session_id", lambda *a, **kw: THREAD)
    monkeypatch.setattr(svc, "_refuse_turn_during_voice_call", lambda *a, **kw: None)
    monkeypatch.setattr(svc, "clear_turn_outcome", lambda sid: None)
    monkeypatch.setattr(svc, "mark_turn_inflight", lambda *a, **kw: None)
    monkeypatch.setattr(svc, "portal_chat", _portal_chat)
    monkeypatch.setattr(database.db, "get_agent_subscription_id", lambda a: None)
    monkeypatch.setattr(database.db, "create_task_execution",
                        lambda **kw: SimpleNamespace(id="exec-3265"))
    monkeypatch.setattr(sts, "resolve_turn_timeout", lambda a: 60)
    monkeypatch.setattr(pp, "pull_queue_allowance", lambda a: 0)

    stored = json.dumps([{"filename": "notes.pdf", "size_bytes": 12000, "mime_type": "application/pdf"}])

    async def _go():
        await svc.start_portal_turn(AGENT, "see attached", CLIENT, session_id=THREAD,
                                    attachments=stored)
        for _ in range(20):
            if seen:
                break
            await asyncio.sleep(0)

    asyncio.run(_go())
    assert seen.get("attachments") == stored


def test_portal_chat_stores_the_attachments_on_the_user_row(svc, monkeypatch):
    """Hop 2 (review): portal_chat → _persist_user_turn."""
    seen = {}

    def _persist(agent_name, email, session_id, content, **kw):
        seen.update(kw, content=content)
        raise _Stop

    monkeypatch.setattr(svc, "agent_on_roster", lambda a, e, include_owned=False: True)
    monkeypatch.setattr(svc, "_availability_allows_turn", lambda a: True)
    monkeypatch.setattr(svc, "resolve_turn_model", lambda a, m: None)
    monkeypatch.setattr(svc, "_resolve_session_id", lambda *a, **kw: THREAD)
    monkeypatch.setattr(svc, "_refuse_turn_during_voice_call", lambda *a, **kw: None)
    monkeypatch.setattr(svc.db, "get_portal_session", lambda *a: None)
    monkeypatch.setattr(svc.db, "get_cached_claude_session_id", lambda sid: None)
    monkeypatch.setattr(svc.db, "get_portal_thread_window",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("no history")))
    monkeypatch.setattr(svc, "_title_plan", lambda *a: None)
    monkeypatch.setattr(svc, "_persist_user_turn", _persist)

    stored = json.dumps([{"filename": "q3-chart.png", "size_bytes": 830, "mime_type": "image/png"}])
    with pytest.raises(_Stop):
        asyncio.run(svc.portal_chat(AGENT, "here", CLIENT, session_id=THREAD, availability="running",
                                    turn_timeout_seconds=60, attachments=stored))
    # #3166: the turn id rides along too (None on this synchronous call, which
    # stamps the row once its execution exists).
    assert seen == {"content": "here", "voice_call_id": None, "attachments": stored,
                    "execution_id": None}
