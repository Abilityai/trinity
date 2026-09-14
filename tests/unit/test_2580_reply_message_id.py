"""A synchronous Workspace reply carries the id of the row it was persisted as (#2580).

The reported symptom was that some agent replies in the Workspace had rating
controls and others did not, with no rule a reader could see. The frontend half
was a dropped field on the streaming path (the persisted row was already being
read back out of history and only its text was kept). This file covers the OTHER
half, which was a genuine gap rather than a slip: the synchronous
`POST .../chat` route had no id to give. `portal_chat` minted one with
`uuid.uuid4().hex` inline, passed it straight to the INSERT and discarded it, so
its caller was handed a reply it could not rate at all — a thumb has to name a
row to post against, and a client-invented id would 404 against the ratings
route.

Three decisions are worth pinning, and only three:

  * **The id is reported only if the row was actually written.** The history
    write is deliberately best-effort — a persistence hiccup must never fail an
    already-billed turn — so "there is a reply" and "there is a row to rate" are
    genuinely different facts here. Returning an id for a row that does not
    exist would be worse than returning none: it offers a control whose POST
    404s, which is the dead affordance this family of issues keeps removing.

  * **The id returned is the id INSERTED.** Two `uuid4()` calls would each look
    correct in isolation and would point the rating at nothing.

  * **The field is DECLARED on the response model.** This is the ent#2320 lesson
    restated one route along: FastAPI's `response_model` strips undeclared keys
    in silence, so a service that returns `message_id` into a model that does
    not declare it is a no-op on the wire while every service-layer test in this
    file still passes. That failure mode is invisible from here, so it is
    asserted against the model directly.
"""

import asyncio
import os
import sys
import tempfile
import types
from pathlib import Path

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")
os.environ.setdefault("AGENT_AUTH_SECRET", "0" * 64)
os.environ.setdefault("SECRET_KEY", "x" * 32)
os.environ.setdefault("INTERNAL_API_SECRET", "y" * 32)
os.environ.setdefault("TRINITY_DB_PATH", str(Path(tempfile.gettempdir()) / "trinity-2580.db"))
os.environ.setdefault("LOG_ARCHIVE_PATH", str(Path(tempfile.gettempdir()) / "trinity-2580-logs"))

_REPO = Path(__file__).resolve().parents[2]
_BACKEND = _REPO / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import pytest  # noqa: E402

pytestmark = pytest.mark.unit

AGENT = "scribe"
EMAIL = "client@example.com"
SESSION = "sess-1"


class _Result:
    def __init__(self, response="the reply"):
        self.status = "success"
        self.response = response
        self.error = None
        self.error_code = None
        self.session_id = None
        self.cost = 0.02


@pytest.fixture()
def chat(monkeypatch):
    """The REAL `portal_chat`, with its boundaries stubbed.

    `add_portal_message` is the seam under test: it records the id it was handed
    so the assertions can compare what was INSERTED with what was RETURNED, and
    it can be made to raise so the best-effort branch is exercised for real
    rather than simulated.
    """
    from client_portal import db as portal_db
    from client_portal import service as svc
    from services import session_turn_service as sts

    state = types.SimpleNamespace(inserted=[], persist_raises=False, result=_Result())

    monkeypatch.setattr(svc, "agent_on_roster", lambda a, e, include_owned=False: True)

    async def _availability(name):
        return "ready"

    monkeypatch.setattr(svc, "_agent_availability", _availability)
    monkeypatch.setattr(svc, "_resolve_session_id", lambda a, e, s, **kw: s or SESSION)
    monkeypatch.setattr(svc, "_build_portal_system_prompt", lambda a, e: None)
    monkeypatch.setattr(svc, "_spawn_title_generation", lambda *a, **kw: None)
    monkeypatch.setattr(svc, "_persist_user_turn", lambda *a, **kw: None)

    async def _no_inbox(agent, email, message):
        return ([], [], [])

    monkeypatch.setattr(svc, "_collect_inbox_for_turn", _no_inbox)

    def _add(msg_id, agent_name, client_email, role, content, cost, now, **kw):
        if state.persist_raises:
            raise RuntimeError("disk full")
        state.inserted.append({"id": msg_id, "role": role, "content": content})

    monkeypatch.setattr(portal_db, "add_portal_message", _add)
    monkeypatch.setattr(portal_db, "get_portal_session", lambda *a, **kw: {"title": "t"})
    monkeypatch.setattr(portal_db, "get_portal_messages", lambda *a, **kw: [])
    monkeypatch.setattr(portal_db, "touch_portal_session", lambda *a, **kw: None)
    monkeypatch.setattr(portal_db, "get_cached_claude_session_id", lambda sid: None)
    monkeypatch.setattr(portal_db, "update_cached_claude_session_id", lambda sid, u: None)

    monkeypatch.setattr(sts, "supports_session_resume", lambda a: True)
    monkeypatch.setattr(sts, "resolve_turn_timeout", lambda a: 600)

    async def _turn(**kwargs):
        return sts.ResumableTurn(result=state.result, real_uuid=None)

    monkeypatch.setattr(sts, "run_resumable_turn", _turn)
    return svc, state


def _chat(svc):
    return asyncio.run(svc.portal_chat(AGENT, "hello", EMAIL, SESSION))


def test_returns_the_id_of_the_row_it_persisted(chat):
    svc, state = chat
    out = _chat(svc)

    assistant = [row for row in state.inserted if row["role"] == "assistant"]
    assert len(assistant) == 1, "the reply is persisted exactly once"
    # The SAME id, not merely some id: two independent uuid4() calls would each
    # look right in isolation and would point the rating at nothing.
    assert out["message_id"] == assistant[0]["id"]
    assert out["response"] == "the reply"


def test_reports_no_id_when_the_row_was_not_written(chat):
    svc, state = chat
    state.persist_raises = True

    out = _chat(svc)

    # The turn still succeeds — the write is best-effort precisely so a
    # persistence hiccup cannot fail a turn the caller has already been billed
    # for — and the reply is still delivered.
    assert out["response"] == "the reply"
    # ...but there is nothing to rate, and saying so is the whole point. An id
    # here would offer a thumb whose POST 404s.
    assert out["message_id"] is None


def test_message_id_is_declared_on_the_response_model():
    """The guard no service-layer test can supply.

    `PortalChatResponse(**result)` is how the route answers, and pydantic
    ignores a key the model does not declare — so without this the feature is a
    no-op on the wire while both tests above pass. Same shape as #2320's
    `PortalHistory.last_turn_outcome`.
    """
    from client_portal.models import PortalChatResponse

    assert "message_id" in PortalChatResponse.model_fields
    # Optional, and defaulted, because the field is genuinely absent whenever the
    # best-effort write did not land. A required field here would 500 the route
    # on exactly the degraded path it exists to describe.
    assert PortalChatResponse.model_fields["message_id"].default is None
    assert PortalChatResponse(response="x").message_id is None
    assert PortalChatResponse(response="x", message_id="m1").message_id == "m1"
