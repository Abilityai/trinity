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

    monkeypatch.setattr(service, "_read_inbox", _inbox)
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


def test_the_request_is_bounded_like_an_upload_batch():
    from pydantic import ValidationError
    from client_portal.models import PortalChatRequest
    PortalChatRequest(message="hi", attachments=[{"filename": f"f{i}"} for i in range(20)])
    with pytest.raises(ValidationError):
        PortalChatRequest(message="hi", attachments=[{"filename": f"f{i}"} for i in range(21)])
    with pytest.raises(ValidationError):
        PortalChatRequest(message="hi", attachments=[{"filename": ""}])


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
