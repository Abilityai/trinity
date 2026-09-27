"""#2766 — Workspace thread titles must authenticate as the AGENT, not as the instance.

`_resolve_title_auth` preferred the instance `ANTHROPIC_API_KEY` outright and
only fell back to the agent's subscription. On a fleet where every agent runs on
a Claude subscription, titles were therefore billed to — and gated on — a console
account no agent was assigned, so an unfunded or revoked instance key broke a
Workspace feature for agents that were otherwise completely healthy.

Same class as #2114 (a stale `ANTHROPIC_API_KEY` shadowing `CLAUDE_CODE_OAUTH_TOKEN`
on the agent side), one layer up on the backend.
"""
from unittest.mock import patch

import pytest

from client_portal import service as svc

pytestmark = pytest.mark.unit

KEY = "sk-ant-instance-key-example"
TOKEN = "oat-agent-subscription-token"


def _auth(agent="mira", *, sub_id=None, token=None, api_key=None, raises=False):
    class _DB:
        def get_agent_subscription_id(self, name):
            if raises:
                raise RuntimeError("db down")
            return sub_id

        def get_subscription_token(self, sid):
            return token if sid == sub_id else None

    with patch.object(svc, "logger"), \
         patch("database.db", _DB(), create=True), \
         patch("services.settings_service.get_anthropic_api_key", return_value=api_key):
        return svc._resolve_title_auth(agent)


# --- the regression ---------------------------------------------------------

def test_a_subscription_agent_uses_its_own_subscription_even_when_an_instance_key_exists():
    """THE FIX. Before #2766 the instance key won and the title was billed to a
    console account the agent was never assigned."""
    h = _auth(sub_id="sub_1", token=TOKEN, api_key=KEY)
    assert h["authorization"] == f"Bearer {TOKEN}"
    assert "x-api-key" not in h
    assert h["anthropic-beta"] == svc._OAUTH_BETA


def test_a_subscription_agent_with_an_unreadable_token_does_not_borrow_the_instance_key():
    """The shadowing this fixes, in its subtler form: an instance key the agent
    was never assigned is not a credential it holds, so a missing token means
    'no credential', not 'use someone else's'."""
    assert _auth(sub_id="sub_1", token=None, api_key=KEY) is None


# --- what must NOT change ---------------------------------------------------

def test_an_api_key_mode_agent_still_uses_the_instance_key():
    """No subscription assigned -> the instance key is genuinely this agent's
    credential, and titles must keep working exactly as before."""
    h = _auth(sub_id=None, api_key=KEY)
    assert h["x-api-key"] == KEY
    assert "authorization" not in h


def test_no_credential_at_all_returns_none():
    assert _auth(sub_id=None, api_key=None) is None


def test_a_subscription_agent_with_no_instance_key_is_unaffected():
    h = _auth(sub_id="sub_1", token=TOKEN, api_key=None)
    assert h["authorization"] == f"Bearer {TOKEN}"


def test_a_db_failure_keeps_the_derived_title_rather_than_raising():
    assert _auth(raises=True) is None


def test_the_mode_comes_from_the_platforms_one_derivation():
    """Not a second answer to 'what is this agent authenticated as' (#471)."""
    import inspect
    src = inspect.getsource(svc._resolve_title_auth)
    assert "derive_auth_mode" in src


# --- the health-record detail (#2766 second half) ---------------------------

def test_the_failure_detail_names_the_upstream_reason():
    """`HTTP 400` alone reads as a transport fault; the actionable half is in
    the body. This is the exact payload from the issue."""
    body = ('{"type":"error","error":{"type":"invalid_request_error","message":'
            '"Your credit balance is too low to access the Anthropic API. '
            'Please go to Plans & Billing to upgrade or purchase credits."}}')
    d = svc._title_failure_detail(400, body)
    assert "HTTP 400" in d
    assert "invalid_request_error" in d
    assert "credit balance is too low" in d


def test_the_failure_detail_degrades_to_the_status_on_an_unparseable_body():
    assert svc._title_failure_detail(502, "<html>bad gateway</html>") == "HTTP 502"
    assert svc._title_failure_detail(500, "") == "HTTP 500"


def test_the_failure_detail_never_echoes_a_credential():
    body = '{"error":{"type":"authentication_error","message":"invalid key sk-ant-abcdefghijkl"}}'
    d = svc._title_failure_detail(401, body)
    assert "sk-ant-abcdefghijkl" not in d
    assert "[redacted]" in d
    assert "authentication_error" in d


def test_the_failure_detail_stays_within_the_health_record_bound():
    body = '{"error":{"type":"' + "x" * 200 + '","message":"' + "y" * 500 + '"}}'
    d = svc._title_failure_detail(400, body)
    # the caller truncates too, but the builder must not hand it a response body
    assert len(d) < 200
