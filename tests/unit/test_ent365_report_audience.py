"""Reports become deliverables with an audience (ent#365).

Two rules, and the second is the one that mattered on a live install:

1. **A report can be addressed**, and the address is checked against the
   publishing agent's OWN roster. The column decides whose Workspace the
   deliverable appears in, so an unchecked value would let a prompt-injected
   agent post its output into a stranger's surface. Same rule as ent#364's
   `addressed_to_email` on asks: a validated column, never a key inside the
   agent-authored payload.

2. **The Workspace read is scoped to who is asking.** `agent_page.reports` used
   to call `db.get_reports_for_agent` — the OPERATOR question ("everything this
   agent published") — so every rostered client of an agent saw every report it
   had ever produced, including ones produced for a different client, rendered
   from free-form agent JSON on a client-facing surface. ent#428 fixed exactly
   this shape on the sibling surface (asks); this is the same defect over a
   bigger blob.

Unaddressed reports stay operator-only, which is AC #1 and is a deliberate
behaviour change: an install whose agents have not adopted `audience_email`
shows an empty Workspace Reports tab rather than another client's deliverables.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

AGENT = "scribe"
OTHER = "recon"
CLIENT = "client@example.com"
STRANGER = "stranger@nowhere.test"


def _row(agent=AGENT, rid="r1"):
    return {
        "id": rid,
        "agent_name": agent,
        "report_type": "recon.leads",
        "title": "Leads",
        "display_hint": "table",
        "payload": {"columns": ["a"], "rows": [[1]]},
        "period_start": None,
        "period_end": None,
        "created_at": "2026-08-24T00:00:00Z",
    }


# ---------------------------------------------------------------------------
# The Workspace list asks the client-scoped question
# ---------------------------------------------------------------------------

def test_the_list_reads_the_client_scoped_accessor_and_passes_the_reader(monkeypatch):
    """Not `get_reports_for_agent` with a filter bolted on: the operator
    accessor answers a different question, and reaching for it here is how the
    fleet's output ended up on one client's page."""
    from client_portal import agent_page
    seen = {}

    def fake(agent_name, client_email, portal_session_id=None, limit=20, offset=0):
        seen.update(agent=agent_name, email=client_email, session=portal_session_id)
        return [_row()]

    monkeypatch.setattr(agent_page.db, "get_reports_for_client", fake)
    # A canary: if the operator accessor is ever called from this path again the
    # test fails loudly rather than passing on a wider result set.
    monkeypatch.setattr(agent_page.db, "get_reports_for_agent",
                        lambda *a, **k: pytest.fail("the operator accessor must not serve the Workspace"))

    got = agent_page.reports(AGENT, CLIENT)

    assert seen == {"agent": AGENT, "email": CLIENT, "session": None}
    assert [r["id"] for r in got] == ["r1"]


def test_a_chat_scoped_list_narrows_within_the_readers_own_rows(monkeypatch):
    """`session_id` is a narrowing, never a widening — the audience condition is
    applied regardless, so another client's session id returns nothing rather
    than their deliverables."""
    from client_portal import agent_page
    seen = {}
    monkeypatch.setattr(agent_page.db, "get_reports_for_client",
                        lambda a, e, portal_session_id=None, limit=20, offset=0:
                        (seen.update(email=e, session=portal_session_id) or []))

    agent_page.reports(AGENT, CLIENT, portal_session_id="sess-1")

    assert seen == {"email": CLIENT, "session": "sess-1"}


def test_a_read_failure_degrades_to_empty_not_to_someone_elses_reports(monkeypatch):
    from client_portal import agent_page
    monkeypatch.setattr(agent_page.db, "get_reports_for_client",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))

    assert agent_page.reports(AGENT, CLIENT) == []


# ---------------------------------------------------------------------------
# The detail read carries the same gate, and keeps the ent#360 one
# ---------------------------------------------------------------------------

def test_a_report_addressed_to_someone_else_is_not_readable(monkeypatch):
    """The accessor answers None for a foreign audience, and the route turns
    that into the same 404 a missing report gets (invariant #8)."""
    from client_portal import agent_page
    monkeypatch.setattr(agent_page.db, "get_report_for_client",
                        lambda rid, email: None if email != CLIENT else _row())

    assert agent_page.report_detail(AGENT, "r1", client_email=STRANGER) is None
    assert agent_page.report_detail(AGENT, "r1", client_email=CLIENT) is not None


def test_the_agent_check_still_applies_on_top_of_the_audience(monkeypatch):
    """Both gates, not either: a report addressed to this reader but belonging
    to a different agent must not be readable from this agent's page."""
    from client_portal import agent_page
    monkeypatch.setattr(agent_page.db, "get_report_for_client",
                        lambda rid, email: _row(agent=OTHER))

    assert agent_page.report_detail(AGENT, "r1", client_email=CLIENT) is None


def test_the_reader_identity_is_required_not_defaulted():
    """A caller that forgets the identity must fail loudly. A default would make
    the gate fail OPEN — the whole defect this issue fixes."""
    from client_portal import agent_page
    with pytest.raises(TypeError):
        agent_page.report_detail(AGENT, "r1")


# ---------------------------------------------------------------------------
# Publishing: the address is checked against the agent's own roster
# ---------------------------------------------------------------------------

@pytest.fixture
def publish(monkeypatch):
    """Call the create route directly with its dependencies supplied."""
    from routers import reports as mod
    from models import ReportCreate, User

    async def _call(**overrides):
        body = ReportCreate(**{
            "report_type": "recon.leads",
            "title": "Leads",
            "payload": {"rows": []},
            **overrides,
        })
        user = User(id=1, username="admin", role="admin", email="admin@example.com")
        return await mod.create_report(body, AGENT, request=None, current_user=user)

    return mod, _call


@pytest.mark.asyncio
async def test_an_address_the_agent_does_not_already_talk_to_is_refused(publish, monkeypatch):
    from fastapi import HTTPException
    mod, call = publish
    monkeypatch.setattr(mod.db, "email_has_agent_access", lambda agent, email: False)
    monkeypatch.setattr(mod.rate_limiter, "enforce", lambda *a, **k: None)

    with pytest.raises(HTTPException) as exc:
        await call(audience_email=STRANGER)

    assert exc.value.status_code == 400
    # Named, with the fix in it — a client-facing capability that fails with a
    # bare 400 teaches the agent nothing.
    assert "share the agent" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_an_unreadable_roster_refuses_rather_than_publishing(publish, monkeypatch):
    """Fail-closed: if we cannot say whether the address is reachable, we do not
    file the report addressed to it."""
    from fastapi import HTTPException
    mod, call = publish
    monkeypatch.setattr(mod.db, "email_has_agent_access",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    monkeypatch.setattr(mod.rate_limiter, "enforce", lambda *a, **k: None)

    with pytest.raises(HTTPException) as exc:
        await call(audience_email=CLIENT)

    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_a_reachable_address_is_stored_with_the_report(publish, monkeypatch):
    mod, call = publish
    stored = {}

    async def fake_create(**kwargs):
        stored.update(kwargs)
        return {**_row(), "user_id": 1}

    monkeypatch.setattr(mod.db, "email_has_agent_access", lambda agent, email: True)
    monkeypatch.setattr(mod.rate_limiter, "enforce", lambda *a, **k: None)
    monkeypatch.setattr(mod.report_service, "create_report", fake_create)
    monkeypatch.setattr(mod, "_resolve_portal_session", lambda eid, agent: "sess-1")

    await call(audience_email=CLIENT, execution_id="exec-1")

    assert stored["addressed_to_email"] == CLIENT
    assert stored["portal_session_id"] == "sess-1"


@pytest.mark.asyncio
async def test_an_unaddressed_report_stays_operator_only(publish, monkeypatch):
    """The default is unchanged: no audience, no session, and no roster lookup
    at all — every report published before this feature meant exactly this."""
    mod, call = publish
    stored = {}
    called = []

    async def fake_create(**kwargs):
        stored.update(kwargs)
        return {**_row(), "user_id": 1}

    monkeypatch.setattr(mod.db, "email_has_agent_access",
                        lambda *a, **k: called.append(1) or True)
    monkeypatch.setattr(mod.rate_limiter, "enforce", lambda *a, **k: None)
    monkeypatch.setattr(mod.report_service, "create_report", fake_create)

    await call()

    assert stored["addressed_to_email"] is None
    assert stored["portal_session_id"] is None
    assert called == []


# ---------------------------------------------------------------------------
# The session is resolved server-side, never taken from the agent
# ---------------------------------------------------------------------------

def test_the_session_is_only_resolved_for_an_execution_this_agent_owns(monkeypatch):
    """The agent supplies an execution id, never a conversation. An id it does
    not own resolves to nothing, so it cannot post a card into a chat it was
    never part of."""
    from routers import reports as mod
    import services.idempotency_service as idem
    from client_portal import service as portal_service

    monkeypatch.setattr(idem, "resolve_and_validate_execution", lambda eid, agent: None)
    monkeypatch.setattr(portal_service, "get_inflight_session_for_execution",
                        lambda eid: pytest.fail("must not be asked for a foreign execution"))

    assert mod._resolve_portal_session("exec-1", AGENT) is None


def test_a_non_portal_turn_resolves_to_no_chat(monkeypatch):
    """A scheduled run has no Workspace session. The deliverable still lists on
    the agent page; it simply has no card."""
    from routers import reports as mod
    import services.idempotency_service as idem
    from client_portal import service as portal_service

    monkeypatch.setattr(idem, "resolve_and_validate_execution", lambda eid, agent: object())
    monkeypatch.setattr(portal_service, "get_inflight_session_for_execution", lambda eid: None)

    assert mod._resolve_portal_session("exec-1", AGENT) is None
