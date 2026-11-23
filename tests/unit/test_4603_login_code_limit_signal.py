"""#4603 — the web code door says so when the shared per-address budget refuses.

``POST /api/auth/email/request`` has two per-address gates. Gate 1 is the
ent#1253 budget shared with the channels and MCP (``channel_login_limit``):
charged BEFORE the whitelist check, so it counts every address alike and its
refusal says nothing about membership. It now answers 429 with the same
sentence the channel doors send, instead of the generic "you'll receive a
code" body that left people waiting for mail that never came.

Gate 2 counts minted code rows, which exist only for allow-listed addresses, so
it must stay byte-identical to the success and not-allow-listed answers (#186,
pentest 3.3.3). These tests drive the real handler.
"""
import asyncio
from unittest.mock import patch

import pytest
from fastapi import HTTPException

pytestmark = pytest.mark.unit

_GENERIC = {
    "success": True,
    "message": "If your email is registered, you'll receive a code shortly",
}


@pytest.fixture(autouse=True)
def _inprocess_limiter():
    """Force the in-process limiter and empty it, so no bucket leaks between tests."""
    from services import rate_limiter
    rate_limiter.clear_inprocess()
    with patch.object(rate_limiter, "_get_redis", return_value=None):
        yield
    rate_limiter.clear_inprocess()


@pytest.fixture(autouse=True)
def _no_policy():
    from services import login_policy_gate
    login_policy_gate.clear_provider()
    yield
    login_policy_gate.clear_provider()


class _FakeRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


def _request(email, *, whitelisted, recent_requests, monkeypatch):
    """Drive the handler once. Returns ("ok", body) or ("http", status, detail),
    plus the codes dispatched."""
    import routers.auth as auth_mod
    import services.email_service as email_service_mod

    sent = []

    class _FakeEmailService:
        async def send_verification_code(self, target, code, context_label=None):
            sent.append(target)
            return True

    monkeypatch.setattr(auth_mod, "is_setup_completed", lambda: True)
    monkeypatch.setattr(auth_mod.db, "get_setting_value", lambda *a, **k: "true")
    monkeypatch.setattr(auth_mod.db, "is_email_whitelisted", lambda e: whitelisted)
    monkeypatch.setattr(auth_mod.db, "count_recent_code_requests",
                        lambda e, minutes=10: recent_requests)
    monkeypatch.setattr(auth_mod.db, "create_login_code",
                        lambda e, expiry_minutes=10: {"code": "123456"})
    monkeypatch.setattr(email_service_mod, "EmailService", _FakeEmailService)

    async def _go():
        try:
            body = await auth_mod.request_email_login_code(_FakeRequest({"email": email}))
            out = ("ok", body)
        except HTTPException as exc:
            out = ("http", exc.status_code, exc.detail)
        await asyncio.sleep(0.05)
        return out

    return asyncio.run(_go()), sent


def _outcomes(email, *, whitelisted, recent_requests, monkeypatch, n=4):
    return [_request(email, whitelisted=whitelisted, recent_requests=recent_requests,
                     monkeypatch=monkeypatch)[0] for _ in range(n)]


class TestSharedBudgetRefusalIsStated:
    def test_fourth_request_is_429_with_the_channel_sentence(self, monkeypatch):
        from services import channel_login_limit as m
        got = _outcomes("v@x.com", whitelisted=True, recent_requests=0,
                        monkeypatch=monkeypatch)
        assert got[:3] == [("ok", _GENERIC)] * 3
        assert got[3] == ("http", 429, m.OVER_LIMIT_REPLY)

    def test_refusal_sends_no_code(self, monkeypatch):
        for _ in range(3):
            _request("v@x.com", whitelisted=True, recent_requests=0, monkeypatch=monkeypatch)
        _, sent = _request("v@x.com", whitelisted=True, recent_requests=0,
                           monkeypatch=monkeypatch)
        assert sent == []

    def test_refusal_is_identical_for_members_and_outsiders(self, monkeypatch):
        """Same request number, same status, same detail — no membership oracle."""
        member = _outcomes("member@x.com", whitelisted=True, recent_requests=0,
                           monkeypatch=monkeypatch)
        outsider = _outcomes("outsider@x.com", whitelisted=False, recent_requests=0,
                             monkeypatch=monkeypatch)
        assert member == outsider

    def test_budget_spent_by_a_channel_is_refused_on_the_web(self, monkeypatch):
        from services import channel_login_limit as m
        for i in range(3):
            assert m.login_code_request_allowed("slack", "T1", f"T1:U{i}", "v@x.com")
        out, sent = _request("v@x.com", whitelisted=False, recent_requests=0,
                             monkeypatch=monkeypatch)
        assert out == ("http", 429, m.OVER_LIMIT_REPLY)
        assert sent == []


class TestMembershipGateStaysSilent:
    def test_gate_two_suppression_is_byte_identical(self, monkeypatch):
        """With gate 1 open, the row-count suppression, the success and the
        not-allow-listed answer are the same body."""
        suppressed, s_sent = _request("a@x.com", whitelisted=True, recent_requests=99,
                                      monkeypatch=monkeypatch)
        success, ok_sent = _request("b@x.com", whitelisted=True, recent_requests=0,
                                    monkeypatch=monkeypatch)
        outsider, o_sent = _request("c@x.com", whitelisted=False, recent_requests=0,
                                    monkeypatch=monkeypatch)
        assert suppressed == success == outsider == ("ok", _GENERIC)
        assert ok_sent == ["b@x.com"] and s_sent == [] and o_sent == []

    def test_gate_two_never_raises(self, monkeypatch):
        """A saturated row count adds no differential: the first three requests
        answer the generic body whatever the row count says."""
        saturated = _outcomes("a@x.com", whitelisted=True, recent_requests=99,
                              monkeypatch=monkeypatch, n=3)
        assert saturated == [("ok", _GENERIC)] * 3
