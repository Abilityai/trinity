"""An install with no mail provider can deliver through a registered relay.

`services/email_service.py` lets a module register a relay: something that can
deliver a few STRUCTURED mails (a kind and short fields) for this install.

Pinned here:

* a provider of the install's own always wins: the relay is not consulted;
* with no provider, a verification code and a mail that names its relay form go
  through the relay, and `can_deliver()` is true;
* a mail that names no relay form is never relayed (the relay words mails
  itself, so it cannot carry free text);
* a relay that fails is NOT reported as sent, and a code it could not send is
  still left in the log for an operator;
* a relay that raises, or cannot say whether it is available, is treated as absent.
"""
from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def svc(monkeypatch):
    import services.email_service as es

    service = es.EmailService()
    state = {"cfg": ("console", "", "noreply@example.com"), "relayed": [], "ok": True, "sent": [], "console": []}
    monkeypatch.setattr(service, "_runtime_config", lambda: state["cfg"])

    async def send_email(to, subject, body, html_body=None):
        state["sent"].append((to, subject))
        return True
    monkeypatch.setattr(service, "send_email", send_email)
    monkeypatch.setattr(service, "_send_console", lambda to, subject, body: state["console"].append((to, body)) or True)

    async def relay(kind, to, fields):
        state["relayed"].append((kind, to, fields))
        return state["ok"]
    es.register_relay(lambda: True, relay)
    yield service, es, state
    es.clear_relay()


def run(coro):
    return asyncio.run(coro)


def test_with_no_provider_a_code_goes_through_the_relay(svc):
    service, _, state = svc
    assert service.can_deliver() is True
    assert run(service.send_verification_code("ada@example.com", "482913", context_label="Trinity login")) is True
    assert state["relayed"] == [("code", "ada@example.com", {"code": "482913", "label": "Trinity login"})]
    assert state["sent"] == [] and state["console"] == []


def test_a_provider_of_its_own_always_wins(svc):
    service, _, state = svc
    state["cfg"] = ("resend", "re_key", "noreply@example.com")
    assert run(service.send_verification_code("ada@example.com", "482913")) is True
    assert run(service.send_html("bo@example.com", "Hi", intro="x", relay=("agent_invite", {"agent": "a"}))) is True
    assert state["relayed"] == [] and len(state["sent"]) == 2


def test_only_a_mail_that_names_its_relay_form_is_relayed(svc):
    service, _, state = svc
    assert run(service.send_html("bo@example.com", "Hi", intro="Anything at all")) is True
    assert state["relayed"] == [] and state["sent"] == [("bo@example.com", "Hi")]
    assert run(service.send_html("bo@example.com", "Hi", intro="x",
                                 relay=("agent_invite", {"agent": "a", "inviter": "Ada", "url": ""}))) is True
    assert state["relayed"] == [("agent_invite", "bo@example.com", {"agent": "a", "inviter": "Ada", "url": ""})]


def test_a_relay_that_fails_is_not_reported_as_sent_and_the_code_stays_out_of_the_log(svc, caplog):
    service, _, state = svc
    state["ok"] = False
    with caplog.at_level("DEBUG"):
        assert run(service.send_verification_code("ada@example.com", "482913")) is False
    # A sign-in code in the log is one hop from whoever reads the logs.
    assert state["console"] == [] and "482913" not in caplog.text and "not sent" in caplog.text
    assert run(service.send_html("bo@example.com", "Hi", intro="x", relay=("agent_invite", {}))) is False


def test_a_relay_that_raises_or_cannot_answer_is_treated_as_absent(svc):
    service, es, state = svc

    async def boom(kind, to, fields):
        raise RuntimeError("down")
    es.register_relay(lambda: True, boom)
    assert run(service.send_verification_code("ada@example.com", "482913")) is False

    def cannot_say():
        raise RuntimeError("unreadable")
    es.register_relay(cannot_say, boom)
    assert service.can_deliver() is False
    assert run(service.send_verification_code("ada@example.com", "482913")) is True     # as before: the console
    es.clear_relay()
    assert service.can_deliver() is False
