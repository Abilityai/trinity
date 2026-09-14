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

_UNSET = object()


def _resolve(ccr, monkeypatch, session, *, executing=AGENT, status="success",
             summary="all done", context_client=_UNSET):
    from client_portal import db as portal_db
    monkeypatch.setattr(portal_db, "get_portal_session_by_id", lambda sid: session)
    # Default to the session's own client so every pre-existing case still
    # exercises the happy path; the recipient-check tests pass it explicitly.
    if context_client is _UNSET:
        context_client = (session or {}).get("client_email")
    return ccr._resolve_portal(
        binding_agent=AGENT, executing_agent=executing, chat_id=SESSION,
        thread=None, status=status, summary_or_error=summary, execution_id=EXEC,
        context_client=context_client,
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


def test_a_report_for_another_client_is_refused(ccr, monkeypatch):
    """The cross-client routing finding (review, blocking #2).

    The inheritance guard that lets a child carry a portal context checks the
    AGENT only. So an agent shared with clients X and Y can cite one of X's
    portal executions while serving Y — same agent, guard passes — and without
    this check the terminal would file a body the agent chose into X's
    permanent thread. One agent holds both clients' data, so it is a disclosure
    between two different people.
    """
    deliver = _resolve(
        ccr, monkeypatch,
        {"agent_name": AGENT, "client_email": "x@example.com"},
        context_client="y@example.com",
    )
    assert deliver is None


def test_a_context_with_no_recorded_client_fails_closed(ccr, monkeypatch):
    """Every row created before the column exists reports NULL, and an
    unverifiable recipient is precisely the case that must not deliver.
    `_norm_email` maps both sides of a missing value to '', so the check can
    never read "unknown == unknown" as agreement."""
    deliver = _resolve(
        ccr, monkeypatch,
        {"agent_name": AGENT, "client_email": CLIENT},
        context_client=None,
    )
    assert deliver is None


def test_the_recipient_check_ignores_case_and_padding(ccr, monkeypatch):
    """A legitimate report must not be refused over the shape of an address —
    the portal lowercases at sign-in, but the stamp rides an inheritance chain.
    """
    deliver = _resolve(
        ccr, monkeypatch,
        {"agent_name": AGENT, "client_email": CLIENT},
        context_client=f"  {CLIENT.upper()} ",
    )
    assert deliver is not None


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
    # #2426: these were `== 2`, and there are THREE creation sites — ent#365's
    # `_precreate_sync_execution` was added after ent#457 counted. The literal
    # census went red on `dev` the moment the third site appeared, which is the
    # guard working; nobody acted on it, and the bug it names shipped anyway.
    #
    # Asserting the RULE instead of the count: every site that stamps the
    # surface must also stamp the destination. Census-proof, and it now fails
    # for the reason the docstring gives rather than because a number moved.
    surface = src.count("source_channel=PORTAL_SOURCE_CHANNEL")
    destination = src.count("source_channel_chat_id=session_id")
    assert surface >= 2, "expected at least the two known portal row creation sites"
    assert destination == surface, (
        f"{surface} site(s) stamp source_channel but only {destination} stamp "
        "source_channel_chat_id — a row created by the unstamped one can never be "
        "joined back to its chat, so report_completion drops every report from it"
    )


def test_the_portal_body_is_credential_sanitized_like_every_other_channel():
    """Review finding: `_portal_body` did a bare strip-and-slice — no sanitizer
    anywhere on its path — while Slack/Telegram ran `sanitize_text` over a 2x
    window first.

    The failure call sites pass RAW text (`_write_terminal_and_gate` passes
    `error`; `apply_result`'s failure branch passes `envelope.error` — only the
    success branch passes an already-sanitized string), so a traceback carrying
    a key was written verbatim into an external client's Workspace thread,
    permanently, and replayed into the agent's own context on the next cold
    turn.
    """
    from services import channel_completion_report as mod

    leaky = "boom: ANTHROPIC_API_KEY=sk-ant-api03-DEADBEEFDEADBEEFDEADBEEF while cloning"
    body = mod._portal_body(executing_agent="a", session_agent="a",
                            status="failed", summary_or_error=leaky)

    assert "sk-ant-api03-DEADBEEFDEADBEEFDEADBEEF" not in body
    assert "Didn't finish" in body


def test_every_channel_shares_one_sanitize_then_truncate_rule():
    """A second implementation of a redaction rule is a second place to forget
    it — which is exactly how the portal leg shipped without one."""
    import inspect
    from services import channel_completion_report as mod

    assert hasattr(mod, "_sanitized_detail")
    for fn in (mod._summarize, mod._portal_body):
        assert "_sanitized_detail(" in inspect.getsource(fn)
    # ...and neither re-implements the slice.
    assert "sanitize_text" not in inspect.getsource(mod._portal_body)


def test_a_failed_portal_write_claims_the_effect_rather_than_releasing_it():
    """Review finding: `deliver()` had no try/except, so a raise escaped into
    `effect_guard`, which calls `fail()` and RELEASES the claim — and a
    re-delivered terminal then appends a SECOND identical report.

    `add_portal_message` commits before `touch_portal_session` runs, so a
    transient "database is locked" on the second write is exactly that shape.
    Both existing resolvers return False for this reason (D4: failed send
    claims completed — the at-most-once bias).
    """
    import inspect
    from services import channel_completion_report as mod
    src = inspect.getsource(mod._resolve_portal)
    assert "except Exception" in src
    assert "return False" in src


def test_the_portal_writes_do_not_run_on_the_event_loop():
    """Sync SQLAlchemy writes on the loop block the whole worker for up to the
    30s SQLite busy timeout when they land during the nightly backup window —
    `try/except` handles errors, not blocking (architecture.md, ent#433)."""
    import inspect
    from services import channel_completion_report as mod
    assert "asyncio.to_thread(_write)" in inspect.getsource(mod._resolve_portal)


def test_the_docstring_no_longer_claims_the_workspace_polls():
    """It does not: history loads on mount and prop change, `refreshThreads()`
    is documented as event-driven, and the only interval is the asks poll on a
    different surface. A false claim here is what stops anyone building the
    poll that would make it true."""
    import inspect
    from services import channel_completion_report as mod
    doc = inspect.getdoc(mod._resolve_portal) or ""
    # Asserted as the CORRECTED claim, not as the absence of a phrase: the old
    # wording legitimately appears in the sentence that retracts it, and a
    # substring ban would forbid explaining the fix.
    assert "not immediately visible" in doc.lower()
    assert "event-driven" in doc
    assert "does not hold by construction here" in doc
    # And the known limitation is written down where the next reader will hit it.
    assert "answer" in doc and "Known limitation" in doc


# ---------------------------------------------------------------------------
# End-to-end through `report_completion` (review finding 8)
#
# Every assertion above reaches `_portal_body` or `_resolve_portal` directly,
# and the three fixes from the first review round are pinned by
# `inspect.getsource`. That is why two real defects — an unsanitized body and a
# raise that released the effect claim — were both invisible to a green suite:
# nothing drove the actual entry point with a portal row, so nothing exercised
# the gate ordering, the recipient guard and the sanitizer together.
#
# These tests do. They stub only the two edges the module does not own (the
# execution row and the portal DB) and let everything between run for real.
# ---------------------------------------------------------------------------

class _Row:
    """A `schedule_executions` row as `report_completion` reads it."""

    def __init__(self, **kw):
        self.id = EXEC
        self.agent_name = AGENT
        self.triggered_by = "mcp"          # delegated, NOT the inline turn
        self.source_channel = "portal"
        self.source_channel_chat_id = SESSION
        self.source_channel_thread = None
        self.source_channel_agent = None
        self.source_channel_client = CLIENT
        self.__dict__.update(kw)


def _drive(ccr, monkeypatch, row, *, status="success", summary_or_error="done",
           session=None):
    """Run the real `report_completion` against stubbed edges.

    Returns `(result, written)` where `written` collects every
    `add_portal_message` call the run produced.
    """
    import asyncio

    written = []

    class _PortalDb:
        @staticmethod
        def get_portal_session_by_id(sid):
            if session is None:
                return None
            return dict(session, id=sid) if "id" not in session else session

        @staticmethod
        def add_portal_message(*args, **kwargs):
            written.append({"args": args, "kwargs": kwargs})

        @staticmethod
        def touch_portal_session(*args, **kwargs):
            pass

    # `_resolve_portal` does `from client_portal import db as portal_db` at call
    # time, so the stub has to be installed on the package attribute.
    import client_portal
    monkeypatch.setattr(client_portal, "db", _PortalDb, raising=False)
    monkeypatch.setitem(
        __import__("sys").modules, "client_portal.db", _PortalDb,
    )

    import database
    monkeypatch.setattr(database.db, "get_execution", lambda eid: row)

    # The effect guard is #1084's, tested there; here it must not require Redis.
    from services import idempotency_service

    class _Guard:
        replay = False
        snapshot = None

    def _fake_guard(*a, **kw):
        class _Ctx:
            async def __aenter__(self):
                return _Guard()

            async def __aexit__(self, *exc):
                return False
        return _Ctx()

    monkeypatch.setattr(idempotency_service, "effect_guard", _fake_guard)

    result = asyncio.run(ccr.report_completion(
        execution_id=EXEC,
        agent_name=AGENT,
        status=status,
        summary_or_error=summary_or_error,
    ))
    return result, written


_SESSION = {"agent_name": AGENT, "client_email": CLIENT}


def test_end_to_end_a_delegated_terminal_reaches_the_clients_thread(ccr, monkeypatch):
    """The happy path nothing exercised: row → gates → resolver → write."""
    ok, written = _drive(ccr, monkeypatch, _Row(), session=_SESSION)
    assert ok is True
    assert len(written) == 1
    body = written[0]["args"][5 - 1] if len(written[0]["args"]) >= 5 else None
    assert body and "Finished" in body


def test_end_to_end_the_body_is_sanitized_before_it_is_persisted(ccr, monkeypatch):
    """Review finding 1, through the real entry point.

    `_write_terminal_and_gate` passes `error` and `apply_result`'s failure
    branch passes `envelope.error` — both raw, and `_extract_agent_error` falls
    back to the agent's raw HTTP body. The destination is a persisted row an
    external client reads indefinitely, so a secret that reaches it is
    permanent. The direct `_portal_body` test cannot prove the sanitizer is on
    the path callers actually take; this one can.
    """
    secret = "sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    ok, written = _drive(
        ccr, monkeypatch, _Row(), status="failed",
        summary_or_error=f"boom: {secret}", session=_SESSION,
    )
    assert ok is True
    blob = repr(written)
    assert secret not in blob, "a credential reached an external client's thread"


def test_end_to_end_a_report_for_another_client_never_writes(ccr, monkeypatch):
    """Review finding 2, through the real entry point.

    The inheritance guard checks the AGENT, not the client, so an agent shared
    with X and Y can cite one of X's portal executions while serving Y. The
    recipient check is what stops it, and it has to stop it before any write.
    """
    row = _Row(source_channel_client="someone-else@example.com")
    ok, written = _drive(ccr, monkeypatch, row, session=_SESSION)
    assert ok is False
    assert written == []


def test_end_to_end_a_row_with_no_recorded_client_fails_closed(ccr, monkeypatch):
    """Every row created before the column exists reports NULL, and an
    unverifiable recipient is exactly the case that must not deliver."""
    ok, written = _drive(ccr, monkeypatch, _Row(source_channel_client=None),
                         session=_SESSION)
    assert ok is False
    assert written == []


def test_end_to_end_the_inline_turn_is_still_refused_at_the_gate(ccr, monkeypatch):
    """The no-double-post rule, proven where it actually runs rather than by
    asserting the constant's membership."""
    ok, written = _drive(ccr, monkeypatch, _Row(triggered_by="public"),
                         session=_SESSION)
    assert ok is False
    assert written == []


def test_end_to_end_a_vanished_session_writes_nothing(ccr, monkeypatch):
    ok, written = _drive(ccr, monkeypatch, _Row(), session=None)
    assert ok is False
    assert written == []
