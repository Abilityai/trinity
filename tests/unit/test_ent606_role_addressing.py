"""Address by role — an agent names a role, the platform resolves the person
(abilityai/trinity-enterprise#606).

One resolution rule (`services/role_addressing.resolve`) behind every outbound
object an agent produces for a human: asks (the #611 sink), reports and
messages. The properties pinned here:

* the core defaults hold with no provider — primary → owner, operator → nobody,
  approver/viewer refused — and a provider's answer wins when it gives one;
* an unknown or unfilled role is refused by NAME, never dropped or defaulted;
* a report and a message have one reader, so a role several people fill is
  refused rather than delivered to one of them;
* the agent never receives the person a role resolved to;
* the old email field still works (deprecated) and cannot be combined with `to`.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from client_portal import service as cps

pytestmark = pytest.mark.unit

AGENT = "scribe"
OWNER = "owner@example.com"
ALICE = "alice@example.com"
BOB = "bob@example.com"


class _Provider:
    def __init__(self, people):
        self.people = people

    def assignment_for(self, agent_name, triggered_by):
        return None

    def people_for(self, agent_name, role):
        value = self.people.get(role)
        return None if value is None else {"emails": value}


@pytest.fixture
def ra(monkeypatch):
    from services import assignment_provider, role_addressing

    assignment_provider.clear_provider()
    monkeypatch.setattr(role_addressing, "owner_email", lambda agent: OWNER)
    yield role_addressing
    assignment_provider.clear_provider()


def _provide(people):
    from services import assignment_provider
    assignment_provider.register_provider(_Provider(people))


# ---------------------------------------------------------------------------
# The one rule
# ---------------------------------------------------------------------------

def test_with_no_provider_primary_is_the_owner(ra):
    r = ra.resolve(AGENT, "primary")
    assert (r.people, r.single, r.resolved) == ([OWNER], OWNER, True)


def test_operator_reaches_the_operators_and_names_no_person(ra):
    r = ra.resolve(AGENT, "operator")
    assert (r.people, r.single, r.resolved) == ([], None, True)


@pytest.mark.parametrize("role", ["approver", "viewer"])
def test_an_unfilled_role_is_refused_by_name(ra, role):
    with pytest.raises(ra.RoleRefused) as exc:
        ra.resolve(AGENT, role)
    assert (exc.value.code, exc.value.role) == ("role_unassigned", role)


def test_an_unknown_role_is_refused_by_name(ra):
    with pytest.raises(ra.RoleRefused) as exc:
        ra.resolve(AGENT, "boss")
    assert exc.value.code == "invalid_to"


def test_the_providers_answer_wins_over_the_owner(ra):
    _provide({"primary": [ALICE], "approver": [ALICE, BOB]})
    assert ra.resolve(AGENT, "primary").single == ALICE
    r = ra.resolve(AGENT, "approver")
    assert (r.people, r.single) == ([ALICE, BOB], None)


def test_a_provider_saying_nobody_is_primary_falls_back_to_the_operators(ra):
    """The ruling: no primary assigned → operator, and the owner stands in only
    when no provider answers at all."""
    _provide({"primary": []})
    r = ra.resolve(AGENT, "primary")
    assert (r.people, r.single, r.resolved) == ([], None, False)


def test_a_provider_saying_nobody_approves_is_still_a_refusal(ra):
    _provide({"approver": []})
    with pytest.raises(ra.RoleRefused):
        ra.resolve(AGENT, "approver")


def test_the_ask_sink_uses_the_same_rule(ra, monkeypatch):
    """An ask and a report addressed to the same role must reach the same
    people — the sink delegates, keeping its own owner lookup patchable."""
    from services import ask_service

    _provide({"approver": [ALICE, BOB]})
    assert ask_service._address(AGENT, "approver") == ([ALICE, BOB], None, True)
    with pytest.raises(ask_service.AskRejected) as exc:
        ask_service._address(AGENT, "viewer")
    assert exc.value.code == "role_unassigned"


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

@pytest.fixture
def publish(monkeypatch, ra):
    from models import ReportCreate, User
    from routers import reports as mod

    stored = {}

    async def fake_create(**kwargs):
        stored.update(kwargs)
        return {"id": "r1", "agent_name": AGENT, "report_type": "t", "title": "T",
                "display_hint": None, "payload": {}, "period_start": None,
                "period_end": None, "created_at": "2026-09-28T00:00:00Z", "user_id": 1,
                "addressed_to_email": kwargs.get("addressed_to_email")}

    monkeypatch.setattr(mod.rate_limiter, "enforce", lambda *a, **k: None)
    monkeypatch.setattr(mod.report_service, "create_report", fake_create)
    roster = {"reachable": True}
    monkeypatch.setattr(cps, "agent_on_roster",
                        lambda agent, email, include_owned=False: roster["reachable"])

    async def _call(_user=None, **body):
        data = ReportCreate(**{"report_type": "recon.leads", "title": "T", "payload": {}, **body})
        user = _user or User(id=1, username=AGENT, role="user", email=None, agent_name=AGENT)
        return await mod.create_report(data, AGENT, request=None, current_user=user)

    return _call, stored, roster


@pytest.mark.asyncio
async def test_a_report_to_a_role_reaches_the_person_who_fills_it(publish):
    call, stored, _ = publish
    _provide({"primary": [ALICE]})
    await call(to="primary")
    assert stored["addressed_to_email"] == ALICE


@pytest.mark.asyncio
async def test_the_agent_never_reads_back_the_person_a_role_resolved_to(publish):
    # The response body, not just the stored row: an agent key calling the REST
    # route directly must not learn who fills the role.
    from models import User
    call, stored, _ = publish
    _provide({"primary": [ALICE]})
    agent = User(id=1, username=AGENT, role="user", email=None, agent_name=AGENT, mcp_scope="agent")
    result = await call(_user=agent, to="primary")
    assert stored["addressed_to_email"] == ALICE
    assert result.addressed_to is None


@pytest.mark.asyncio
async def test_a_human_publishing_still_sees_the_audience(publish):
    from models import User
    call, _, _ = publish
    _provide({"primary": [ALICE]})
    human = User(id=1, username="owner", role="user", email=OWNER)
    result = await call(_user=human, to="primary")
    assert result.addressed_to == ALICE


@pytest.mark.asyncio
async def test_a_report_to_primary_on_a_plain_agent_stays_on_the_operator_surface(publish):
    """With no assignments primary is the owner, who reads reports on the
    operator surface — the audience column is for someone else."""
    call, stored, _ = publish
    await call(to="primary")
    assert stored["addressed_to_email"] is None


@pytest.mark.asyncio
async def test_a_report_to_operator_is_operator_only(publish):
    call, stored, _ = publish
    await call(to="operator")
    assert stored["addressed_to_email"] is None


@pytest.mark.asyncio
async def test_an_unaddressed_report_is_still_operator_only(publish):
    call, stored, _ = publish
    await call()
    assert stored["addressed_to_email"] is None


@pytest.mark.asyncio
async def test_a_report_to_an_unfilled_role_is_refused_by_name(publish):
    call, stored, _ = publish
    with pytest.raises(HTTPException) as exc:
        await call(to="approver")
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "role_unassigned"
    assert stored == {}


@pytest.mark.asyncio
async def test_a_report_to_a_role_several_people_fill_is_refused_not_split(publish):
    call, stored, _ = publish
    _provide({"viewer": [ALICE, BOB]})
    with pytest.raises(HTTPException) as exc:
        await call(to="viewer")
    assert exc.value.detail["code"] == "role_resolves_to_several"
    assert exc.value.detail["count"] == 2
    assert stored == {}


@pytest.mark.asyncio
async def test_a_role_resolving_to_someone_off_the_roster_is_refused_by_name(publish):
    call, stored, roster = publish
    _provide({"approver": [ALICE]})
    roster["reachable"] = False
    with pytest.raises(HTTPException) as exc:
        await call(to="approver")
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "role_unreachable"
    # The person is never named back to the agent.
    assert ALICE not in str(exc.value.detail)


@pytest.mark.asyncio
async def test_a_role_and_an_email_together_are_refused(publish):
    call, stored, _ = publish
    with pytest.raises(HTTPException) as exc:
        await call(to="primary", audience_email=ALICE)
    assert exc.value.detail["code"] == "addressing_conflict"


@pytest.mark.asyncio
async def test_the_email_still_works_and_is_logged_as_deprecated(publish, ra, caplog):
    call, stored, _ = publish
    ra._deprecation_logged.clear()
    with caplog.at_level("INFO", logger="services.role_addressing"):
        await call(audience_email=ALICE)
    assert stored["addressed_to_email"] == ALICE
    assert any("deprecated" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

def _msg(**body):
    from models import SendMessageRequest
    return SendMessageRequest(**{"text": "hi", **body})


def test_a_message_needs_exactly_one_address():
    with pytest.raises(ValidationError):
        _msg()
    with pytest.raises(ValidationError):
        _msg(to="primary", recipient_email=ALICE)


def test_a_message_cannot_be_addressed_to_the_operators():
    """The operator's door is the Operating Room — an alert ask, not a DM."""
    with pytest.raises(ValidationError):
        _msg(to="operator")


def test_a_message_to_a_role_goes_to_the_person_who_fills_it(ra):
    from routers import messages
    _provide({"approver": [ALICE]})
    assert messages._recipient(AGENT, _msg(to="approver")) == ALICE


def test_a_message_to_primary_on_a_plain_agent_goes_to_the_owner(ra):
    from routers import messages
    assert messages._recipient(AGENT, _msg(to="primary")) == OWNER


@pytest.mark.parametrize("people,code", [
    ({"viewer": [ALICE, BOB]}, "role_resolves_to_several"),
    ({}, "role_unassigned"),
])
def test_a_message_that_cannot_reach_exactly_one_person_is_refused(ra, people, code):
    from routers import messages
    _provide(people)
    with pytest.raises(HTTPException) as exc:
        messages._recipient(AGENT, _msg(to="viewer"))
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == code


def test_a_message_to_primary_when_nobody_is_primary_is_refused(ra):
    from routers import messages
    _provide({"primary": []})
    with pytest.raises(HTTPException) as exc:
        messages._recipient(AGENT, _msg(to="primary"))
    assert exc.value.detail["code"] == "role_unassigned"


def test_a_message_by_email_still_works(ra):
    from routers import messages
    assert messages._recipient(AGENT, _msg(recipient_email=ALICE)) == ALICE


@pytest.mark.asyncio
async def test_the_route_sends_to_the_resolved_person(ra, monkeypatch):
    from routers import messages
    sent = {}

    class _Result:
        success, channel, message_id, error = True, "web", "m1", None

    async def fake_send(**kwargs):
        sent.update(kwargs)
        return _Result()

    monkeypatch.setattr(messages.proactive_message_service, "send_message", fake_send)
    _provide({"approver": [ALICE]})
    await messages.send_proactive_message(_msg(to="approver"), AGENT, current_user=_agent_key())
    assert sent["recipient_email"] == ALICE


def _agent_key():
    from models import User
    return User(id=1, username="owner", role="user", email=OWNER, agent_name=AGENT)


def _human():
    from models import User
    return User(id=1, username="owner", role="user", email=OWNER)


# ---------------------------------------------------------------------------
# Messages: the deprecation log fires under the same condition as reports
# ---------------------------------------------------------------------------

def _deprecations(caplog):
    return [r for r in caplog.records
            if r.name == "services.role_addressing" and "deprecated" in r.getMessage()]


def test_an_agent_key_messaging_by_email_is_logged_as_deprecated(ra, caplog):
    from routers import messages
    ra._deprecation_logged.clear()
    with caplog.at_level("INFO", logger="services.role_addressing"):
        messages._recipient(AGENT, _msg(recipient_email=ALICE), _agent_key())
    assert len(_deprecations(caplog)) == 1


def test_a_human_messaging_by_email_is_not_logged_as_deprecated(ra, caplog):
    """Reports log only for an agent key; messages match — a person typing an
    email is not the agent-side migration the log exists to track."""
    from routers import messages
    ra._deprecation_logged.clear()
    with caplog.at_level("INFO", logger="services.role_addressing"):
        messages._recipient(AGENT, _msg(recipient_email=ALICE), _human())
    assert _deprecations(caplog) == []


# ---------------------------------------------------------------------------
# Messages: a role-addressed delivery failure names the role, never the person
# (driven through the real route with an agent key)
# ---------------------------------------------------------------------------

@pytest.fixture
def route(ra, monkeypatch):
    """POST /api/agents/{name}/messages as the agent's own key, over the real
    proactive service with its collaborators (db, audit, rate limit) stubbed."""
    from unittest.mock import MagicMock

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from routers import messages
    from services import proactive_message_service as pms

    svc_db = MagicMock()
    svc_db.can_agent_message_email.return_value = True
    svc_db.get_telegram_binding.return_value = {"id": 1}
    svc_db.get_telegram_chat_link_by_verified_email.return_value = None
    svc_db.get_all_slack_workspaces.return_value = [{"bot_token": None}]
    svc_db.get_whatsapp_binding.return_value = {"id": 1}
    svc_db.get_whatsapp_chat_link_by_verified_email.return_value = None
    monkeypatch.setattr(pms, "db", svc_db)
    monkeypatch.setattr(pms.proactive_message_service, "_check_rate_limit", lambda *a: True)

    async def _no_audit(*a, **k):
        return None

    monkeypatch.setattr(pms.proactive_message_service, "_audit_send", _no_audit)

    app = FastAPI()
    app.include_router(messages.router)
    # Override the objects the ROUTE holds (an earlier test may have re-imported
    # `dependencies`), and stub the access check's db on that same module.
    from fastapi.routing import APIRoute

    def _walk(dep):
        for sub in dep.dependencies:
            yield sub
            yield from _walk(sub)

    for r in messages.router.routes:
        if isinstance(r, APIRoute) and r.path.endswith("/messages"):
            for d in _walk(r.dependant):
                if getattr(d.call, "__name__", "") == "get_current_user":
                    app.dependency_overrides[d.call] = _agent_key
                if getattr(d.call, "__name__", "") == "get_authorized_agent_by_name":
                    dep_db = d.call.__globals__["db"]
                    monkeypatch.setattr(dep_db, "get_agent_owner", lambda name: "owner")
                    monkeypatch.setattr(dep_db, "can_user_access_agent", lambda u, n: True)
    _provide({"approver": [ALICE]})

    def post(**body):
        return TestClient(app).post(f"/api/agents/{AGENT}/messages", json={"text": "hi", **body})

    return post, svc_db, pms


def _no_person(resp):
    text = resp.text
    assert "@" not in text and ALICE not in text, text


def test_a_role_addressed_message_to_someone_not_opted_in_names_only_the_role(route):
    post, svc_db, _ = route
    svc_db.can_agent_message_email.return_value = False
    resp = post(to="approver", channel="telegram")
    assert resp.status_code == 403
    _no_person(resp)
    assert resp.json()["detail"] == {
        "code": "role_not_opted_in", "role": "approver",
        "message": "The person in the approver role has not opted in to proactive "
                   "messages from this agent."}


# WhatsApp is not an accepted `channel` on this route (the request model
# refuses it with 422), so it is unreachable here.
@pytest.mark.parametrize("channel", ["telegram", "slack", "web", "auto"])
def test_a_role_addressed_message_no_channel_reaches_names_only_the_role(route, channel):
    post, _, _ = route
    resp = post(to="approver", channel=channel)
    assert resp.status_code == 404
    _no_person(resp)
    assert resp.json()["detail"]["code"] == "role_no_channel"
    assert resp.json()["detail"]["role"] == "approver"


def test_a_channel_error_carrying_the_email_never_reaches_a_role_addressed_caller(route, monkeypatch):
    """The per-channel RecipientNotFoundError texts name the person (Telegram,
    Slack, WhatsApp, web). Whatever text the service raises, a role-addressed
    caller gets the role-only detail."""
    post, _, pms = route

    async def leaky(**kwargs):
        raise pms.RecipientNotFoundError(
            f"No Telegram user with verified email '{kwargs['recipient_email']}' for agent '{AGENT}'")

    monkeypatch.setattr(pms.proactive_message_service, "send_message", leaky)
    resp = post(to="approver", channel="telegram")
    assert resp.status_code == 404
    _no_person(resp)


def test_an_email_addressed_message_keeps_its_existing_detail(route):
    """The agent supplied the email itself, so naming it back discloses nothing."""
    post, svc_db, _ = route
    svc_db.can_agent_message_email.return_value = False
    resp = post(recipient_email=ALICE, channel="telegram")
    assert resp.status_code == 403
    assert resp.json()["detail"] == (
        f"Agent '{AGENT}' is not authorized to message '{ALICE}'. "
        "Recipient must opt in via allow_proactive flag.")
    svc_db.can_agent_message_email.return_value = True
    resp = post(recipient_email=ALICE, channel="telegram")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "No delivery channel available for recipient"
