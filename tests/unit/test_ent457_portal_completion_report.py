"""The Workspace gets the report-back contract the channels already had (ent#457).

The issue's promise: "dispatch → monitor → report back is a contract, not a
habit… 'forgot to come back' becomes structurally impossible." The machinery for
that already existed for Slack and Telegram (ent#224/#265) — the Workspace was
excluded by ONE missing field. #2157 stamped portal executions with the SURFACE
(`source_channel = "portal"`) and never a destination, so every portal row died
at `report_completion`'s `if not source_channel_chat_id` gate.

So this suite is about the join, and about the two ways a join like this goes
wrong:

1. **Double-posting.** A Workspace turn is synchronous — `portal_chat` persists
   the assistant reply itself — so reporting on the turn's OWN execution would
   append a duplicate "done" to every single chat message.
2. **Delivering to the wrong person.** The destination rides an inheritance
   chain as a string; the session row is the platform's own record of whose chat
   it is, and it is what decides the recipient.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

AGENT = "scribe"
OTHER = "recon"
CLIENT = "client@example.com"
SESSION = "sess-1"
EXEC = "exec-1"


@pytest.fixture
def ccr():
    from services import channel_completion_report as mod
    return mod


# ---------------------------------------------------------------------------
# The no-double-post rule
# ---------------------------------------------------------------------------

def test_the_turns_own_execution_is_never_reported(ccr):
    """`public` is the Workspace turn's trigger and the turn replies inline.
    Without this, every chat message would be followed by a duplicate "done"."""
    assert "public" in ccr.INLINE_CHANNEL_TRIGGERS


def test_the_channel_triggers_are_still_inline(ccr):
    """ent#224/#265's rule is unchanged — this only adds to the set."""
    for t in ("slack", "telegram", "whatsapp"):
        assert t in ccr.INLINE_CHANNEL_TRIGGERS


def test_portal_is_a_resolver_entry_not_a_special_case(ccr):
    """The dispatch map is the extension point (ent#265 D10). A hand-rolled
    if/else for the portal would be the third copy of this decision."""
    assert "portal" in ccr.SUPPORTED_CHANNELS
    assert ccr._CHANNEL_RESOLVERS["portal"] is ccr._resolve_portal


# ---------------------------------------------------------------------------
# Who receives it
# ---------------------------------------------------------------------------

def _resolve(ccr, monkeypatch, session, *, executing=AGENT, status="success",
             summary="all done"):
    from client_portal import db as portal_db
    monkeypatch.setattr(portal_db, "get_portal_session_by_id", lambda sid: session)
    return ccr._resolve_portal(
        binding_agent=AGENT, executing_agent=executing, chat_id=SESSION,
        thread=None, status=status, summary_or_error=summary, execution_id=EXEC,
    )


def test_the_recipient_comes_from_the_session_row_not_the_stamp(ccr, monkeypatch):
    """The stamp is a string that rode through an inheritance chain; the session
    row is the platform's own record of whose chat this is."""
    from client_portal import db as portal_db
    written = {}
    monkeypatch.setattr(portal_db, "add_portal_message",
                        lambda mid, agent, email, role, content, cost, now, session_id=None:
                        written.update(agent=agent, email=email, role=role,
                                       content=content, session=session_id))

    deliver = _resolve(ccr, monkeypatch,
                       {"agent_name": AGENT, "client_email": CLIENT})
    assert deliver is not None

    import asyncio
    assert asyncio.get_event_loop_policy().new_event_loop().run_until_complete(deliver()) is True
    assert written["email"] == CLIENT
    assert written["session"] == SESSION
    assert written["role"] == "assistant"


def test_delivery_moves_the_thread_in_the_sidebar(ccr, monkeypatch):
    """Caught in review. Every other writer of a portal message pairs it with
    `touch_portal_session`, and this one must too: `last_message_at` is what
    orders the thread list. The unread badge fires either way (ent#359 counts
    message ROWS against a read cursor), but a badge on a thread that has not
    moved points at the middle of a list — a report nobody notices is the same
    silence the contract exists to end.
    """
    from client_portal import db as portal_db
    touched = {}
    monkeypatch.setattr(portal_db, "add_portal_message",
                        lambda *a, **k: None)
    monkeypatch.setattr(portal_db, "touch_portal_session",
                        lambda sid, now, added=0, **k: touched.update(session=sid, added=added))

    deliver = _resolve(ccr, monkeypatch, {"agent_name": AGENT, "client_email": CLIENT})
    import asyncio
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(deliver())

    assert touched == {"session": SESSION, "added": 1}


def test_a_vanished_session_suppresses_rather_than_guessing(ccr, monkeypatch):
    assert _resolve(ccr, monkeypatch, None) is None


def test_a_session_with_no_client_suppresses(ccr, monkeypatch):
    """There is no recipient to deliver to, and inventing one is how a report
    lands in a stranger's chat."""
    assert _resolve(ccr, monkeypatch, {"agent_name": AGENT, "client_email": None}) is None


def test_the_message_is_filed_under_the_agent_whose_chat_it_is(ccr, monkeypatch):
    """A delegated child may execute as a DIFFERENT agent (A asks B). The client
    is talking to A, so the message is A's and names B in the body."""
    from client_portal import db as portal_db
    written = {}
    monkeypatch.setattr(portal_db, "add_portal_message",
                        lambda mid, agent, email, role, content, cost, now, session_id=None:
                        written.update(agent=agent, content=content))

    deliver = _resolve(ccr, monkeypatch, {"agent_name": AGENT, "client_email": CLIENT},
                       executing=OTHER)
    import asyncio
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(deliver())

    assert written["agent"] == AGENT       # whose chat it is
    assert OTHER in written["content"]     # who actually did the work


# ---------------------------------------------------------------------------
# What it says
# ---------------------------------------------------------------------------

def test_a_failure_says_so_rather_than_vanishing(ccr):
    """AC #3: failed/timeout states are honest, never a silent vanish."""
    body = ccr._portal_body(executing_agent=AGENT, session_agent=AGENT,
                            status="failed", summary_or_error="quota exhausted")
    assert "Didn't finish" in body and "failed" in body
    assert "quota exhausted" in body


def test_success_does_not_name_the_agent_when_it_is_the_one_youre_talking_to(ccr):
    body = ccr._portal_body(executing_agent=AGENT, session_agent=AGENT,
                            status="success", summary_or_error="done")
    assert body.startswith("**Finished**")


def test_a_long_result_is_truncated_rather_than_posted_whole(ccr):
    body = ccr._portal_body(executing_agent=AGENT, session_agent=AGENT,
                            status="success", summary_or_error="x" * 9000)
    assert len(body) < 9000
    assert body.rstrip().endswith("…")


def test_an_empty_result_still_reports_the_outcome(ccr):
    """"It finished and said nothing" is information; an empty message is not."""
    body = ccr._portal_body(executing_agent=AGENT, session_agent=AGENT,
                            status="success", summary_or_error=None)
    assert body.strip() == "**Finished**"


# ---------------------------------------------------------------------------
# The stamp that makes any of it reachable
# ---------------------------------------------------------------------------

def test_both_portal_row_creation_sites_name_the_chat():
    """#2157 FR-7 established that BOTH sites must stamp the surface, for the
    same reason this needs both to stamp the destination: which one made the row
    depends on whether the client used the streaming path."""
    import inspect
    from client_portal import service

    src = inspect.getsource(service)
    assert src.count("source_channel_chat_id=session_id") == 2
    assert src.count("source_channel=PORTAL_SOURCE_CHANNEL") == 2
