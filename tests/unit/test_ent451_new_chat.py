"""ent#451 — "New chat" has to mean a new chat.

Reported: pressing New chat in the Workspace drops you back into the existing
conversation with that agent. Decided at the 2026-08-21 weekly.

THE CAUSE IS ONE VALUE CARRYING TWO MEANINGS. An absent `session_id` meant both
"I don't know which thread" and "I want a fresh one", and the platform resolved
it as the first:

    _resolve_session_id(...)   # None -> resume the client's latest
    get_history(..., None)     # None -> return the most-recent thread

Both readings are right for the case they were written for — a deep link, a
refresh, an API caller who never had a session id — so neither can simply be
inverted. The fix is a THIRD state that says which one is meant.

WHAT THIS SLICE DOES NOT TOUCH, because ent#451's harder ACs are already
satisfied and re-deciding them would be the risk, not the work:

* the data model already allows many sessions per (agent, client) — no UNIQUE
  constraint, a `title` column, and an index on
  `(agent_name, client_email, last_message_at)`. So AC #4's "migrates cleanly"
  is nothing to migrate.
* AC #3's landing rule for agent-initiated items is already decided and
  documented in `ensure_thread_for_ask`: reuse the client's latest thread, open
  one only if they have never chatted, "so an ask does not accumulate threads
  beside the conversation". That stays exactly as it is, and the test below
  pins it so this change cannot quietly move it.
"""
from __future__ import annotations

import inspect

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture()
def svc():
    from client_portal import service as m
    return m


class _Db:
    """Records what the resolver did, with one pre-existing thread."""

    def __init__(self, latest="ps_existing"):
        self.latest = latest
        self.created = []

    def get_portal_session(self, session_id, agent_name, client_email):
        return {"id": session_id} if session_id == self.latest else None

    def get_latest_portal_session_id(self, agent_name, client_email):
        return self.latest

    def create_portal_session(self, session_id, agent_name, client_email, now, **kw):
        self.created.append(session_id)


# ---------------------------------------------------------------------------
# The three states
# ---------------------------------------------------------------------------
def test_an_explicit_thread_is_still_honoured(svc, monkeypatch):
    db = _Db()
    monkeypatch.setattr(svc, "db", db, raising=False)
    assert svc._resolve_session_id("a", "x@example.com", "ps_existing") == "ps_existing"
    assert db.created == []


def test_no_session_still_resumes_the_latest(svc, monkeypatch):
    """Unchanged, and deliberately so: a deep link, a refresh and an API caller
    that never held a session id all arrive this way, and for them resuming is
    the right answer. Inverting this would fix New chat by breaking those."""
    db = _Db()
    monkeypatch.setattr(svc, "db", db, raising=False)
    assert svc._resolve_session_id("a", "x@example.com", None) == "ps_existing"
    assert db.created == []


def test_new_thread_opens_one_even_though_a_thread_exists(svc, monkeypatch):
    """The reported bug, as a test."""
    db = _Db()
    monkeypatch.setattr(svc, "db", db, raising=False)
    sid = svc._resolve_session_id("a", "x@example.com", None, new_thread=True)
    assert sid not in (None, "ps_existing")
    assert db.created == [sid], "a new thread must actually be persisted, not just named"


def test_new_thread_works_for_a_first_ever_chat(svc, monkeypatch):
    """No latest to ignore — the flag must not depend on one existing."""
    db = _Db(latest=None)
    monkeypatch.setattr(svc, "db", db, raising=False)
    sid = svc._resolve_session_id("a", "x@example.com", None, new_thread=True)
    assert db.created == [sid]


def test_an_explicit_thread_beats_the_flag(svc, monkeypatch):
    """A caller naming a thread AND asking for a new one is contradicting
    itself. Honour the specific instruction: the id is a fact, the flag is an
    intent, and silently abandoning a named thread would be the more surprising
    of the two — it would strand a turn the caller meant for a conversation it
    could see.
    """
    db = _Db()
    monkeypatch.setattr(svc, "db", db, raising=False)
    assert svc._resolve_session_id("a", "x@example.com", "ps_existing",
                                   new_thread=True) == "ps_existing"
    assert db.created == []


def test_a_named_thread_that_is_not_yours_still_404s(svc, monkeypatch):
    """The flag must not become a way past the ownership check."""
    from client_portal.service import ClientPortalError

    db = _Db()
    monkeypatch.setattr(svc, "db", db, raising=False)
    with pytest.raises(ClientPortalError):
        svc._resolve_session_id("a", "x@example.com", "ps_someone_else", new_thread=True)
    assert db.created == []


# ---------------------------------------------------------------------------
# The rules this slice must NOT move
# ---------------------------------------------------------------------------
def test_an_ask_still_lands_in_the_latest_thread(svc):
    """AC #3 is already decided; this pins it against drift.

    `ensure_thread_for_ask` reuses the client's latest thread so asks do not
    accumulate threads beside the conversation. Once several chats can exist
    that rule matters MORE, not less — an ask that opened its own thread each
    time would bury itself.
    """
    src = inspect.getsource(svc.ensure_thread_for_ask)
    assert "new_thread=True" not in src, (
        "an ask must not open a thread of its own — see ent#429's landing rule"
    )


def test_history_without_a_session_is_unchanged(svc):
    """The other reader of the two-meaning value. It keeps resuming, because a
    refresh must still show the conversation; the FRONTEND stops asking for
    history when it is deliberately starting fresh (see the spec in
    tests/unit/... frontend suite)."""
    src = inspect.getsource(svc.get_history)
    assert "new_thread" not in src


# ---------------------------------------------------------------------------
# The request surface
# ---------------------------------------------------------------------------
def test_the_chat_request_carries_the_intent():
    from client_portal.models import PortalChatRequest

    assert "new_thread" in PortalChatRequest.model_fields
    assert PortalChatRequest(message="hi").new_thread is False, (
        "defaulting to True would make every API caller open a thread per turn"
    )
    assert PortalChatRequest(message="hi", new_thread=True).new_thread is True


def test_both_turn_entry_points_forward_it():
    """Sync `/chat` and streaming `/chat/stream` must agree — the Workspace uses
    the streaming one and falls back to the sync one, so a flag honoured by only
    one makes the bug come back exactly when streaming fails."""
    from client_portal import router

    for fn in (router.portal_chat, router.portal_chat_stream):
        src = inspect.getsource(fn)
        assert "new_thread" in src, f"{fn.__name__} drops the new-thread intent"
