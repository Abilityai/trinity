"""ent#1253 — channel sign-in code requests are rate limited.

Telegram ``/login <email>``, WhatsApp ``/login <email>`` and the Slack
``require_email`` email step each emailed a code on every request. They are now
capped per address (3 / 10 min, one budget shared with web + MCP login, which
charge it before their whitelist / known-address check), per sender
(5 / 10 min) and per bot / binding / workspace (20 / 10 min). Over the limit the
bot says so and sends nothing. The check runs before the ent#849 membership
branch, so the reply is the same for members and outsiders.
"""
import asyncio
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


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


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _send_ok(*a, **k):
    return asyncio.sleep(0, result=True)


# ------------------------------------------------------------ the helper

class TestHelper:
    def test_per_address_cap_is_case_insensitive(self):
        from services import channel_login_limit as m
        got = [m.login_code_request_allowed("telegram", "bot1", f"u{i}", e)
               for i, e in enumerate(["V@x.com", "v@x.com", " v@X.com", "v@x.com"])]
        assert got == [True, True, True, False]

    def test_per_sender_cap(self):
        from services import channel_login_limit as m
        got = [m.login_code_request_allowed("telegram", "bot1", "u1", f"a{i}@x.com")
               for i in range(6)]
        assert got == [True] * 5 + [False]

    def test_sender_key_ignores_bot(self):
        from services import channel_login_limit as m
        got = [m.login_code_request_allowed("telegram", f"bot{i}", "u1", f"a{i}@x.com")
               for i in range(6)]
        assert got[-1] is False

    def test_per_bot_cap(self):
        from services import channel_login_limit as m
        got = [m.login_code_request_allowed("whatsapp", "bot1", f"u{i}", f"a{i}@x.com")
               for i in range(21)]
        assert got == [True] * 20 + [False]

    def test_sender_block_does_not_burn_address_budget(self):
        from services import channel_login_limit as m
        for i in range(5):
            m.login_code_request_allowed("telegram", "bot1", "attacker", f"a{i}@x.com")
        for _ in range(5):
            assert not m.login_code_request_allowed("telegram", "bot1", "attacker", "v@x.com")
        assert [m.login_code_request_allowed("telegram", "bot1", f"u{i}", "v@x.com")
                for i in range(4)] == [True, True, True, False]

    def test_web_code_rows_do_not_change_the_reply(self):
        """Web login writes code rows only for whitelisted addresses. A channel
        cap that read them would tell an attacker which addresses are whitelisted.
        Patched on the shared ``db`` object, so a module-level ``from database
        import db`` is caught as well as an import inside the function."""
        import database
        from services import channel_login_limit as m
        with patch.object(database.db, "count_recent_code_requests",
                          side_effect=AssertionError("code rows read")) as count:
            assert m.login_code_request_allowed("telegram", "bot1", "u1", "whitelisted@x.com")
        count.assert_not_called()

    def test_bot_cap_refusal_spends_no_sender_or_address_budget(self):
        from services import channel_login_limit as m
        for i in range(20):
            assert m.login_code_request_allowed("telegram", "bot1", f"u{i}", f"a{i}@x.com")
        for _ in range(5):
            assert not m.login_code_request_allowed("telegram", "bot1", "s", "v@x.com")
        # Sender "s" still has 5, address v@x.com still has 3, on another bot.
        got = [m.login_code_request_allowed("telegram", "bot2", "s", e)
               for e in ["v@x.com", "v@x.com", "v@x.com", "b@x.com", "c@x.com"]]
        assert got == [True] * 5
        assert not m.login_code_request_allowed("telegram", "bot2", "s", "d@x.com")

    def test_address_refusal_spends_no_sender_budget(self):
        from services import channel_login_limit as m
        for i in range(3):
            m.login_code_request_allowed("telegram", "bot1", f"u{i}", "v@x.com")
        for _ in range(5):
            assert not m.login_code_request_allowed("telegram", "bot1", "s", "v@x.com")
        assert [m.login_code_request_allowed("telegram", "bot1", "s", f"a{i}@x.com")
                for i in range(6)] == [True] * 5 + [False]


# ------------------------------------------- one address budget with web + MCP

class _FakeRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


def _web_request(email, *, whitelisted, monkeypatch):
    """Drive the web request handler once; return the codes it dispatched."""
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
    monkeypatch.setattr(auth_mod.db, "count_recent_code_requests", lambda e, minutes=10: 0)
    monkeypatch.setattr(auth_mod.db, "create_login_code",
                        lambda e, expiry_minutes=10: {"code": "123456"})
    monkeypatch.setattr(email_service_mod, "EmailService", _FakeEmailService)

    async def _go():
        await auth_mod.request_email_login_code(_FakeRequest({"email": email}))
        await asyncio.sleep(0.05)

    asyncio.run(_go())
    return sent


class TestSharedAddressBudget:
    def test_web_requests_for_unlisted_address_use_the_channel_budget(self, monkeypatch):
        """Charged before the whitelist check, so it counts every address alike."""
        from services import channel_login_limit as m
        for _ in range(3):
            _web_request("V@x.com", whitelisted=False, monkeypatch=monkeypatch)
        assert not m.login_code_request_allowed("telegram", "bot1", "u1", "v@x.com")

    def test_channel_requests_use_the_web_budget(self, monkeypatch):
        from services import channel_login_limit as m
        for i in range(3):
            assert m.login_code_request_allowed("slack", "T1", f"T1:U{i}", "v@x.com")
        assert _web_request("v@x.com", whitelisted=True, monkeypatch=monkeypatch) == []

    def test_mcp_requests_for_unknown_address_use_the_channel_budget(self):
        from services import channel_login_limit as m
        from services import mcp_auth_service as svc
        db = MagicMock()
        db.count_recent_code_requests.return_value = 0
        with patch.object(svc, "db", db), patch.object(svc, "_email_is_known", return_value=False):
            for _ in range(3):
                assert svc._resolve_and_create_code("v@x.com", "s1") is None
        assert not m.login_code_request_allowed("telegram", "bot1", "u1", "v@x.com")

    def test_mcp_over_budget_mints_nothing(self):
        from services import channel_login_limit as m
        from services import mcp_auth_service as svc
        for i in range(3):
            m.login_code_request_allowed("telegram", "bot1", f"u{i}", "v@x.com")
        db = MagicMock()
        db.count_recent_code_requests.return_value = 0
        with patch.object(svc, "db", db), patch.object(svc, "_email_is_known", return_value=True):
            assert svc._resolve_and_create_code("v@x.com", "s1") is None
        db.create_login_code.assert_not_called()


# ----------------------------------------------------- Telegram / WhatsApp

def _message(sender="u1", is_group=False, chat="c1"):
    from adapters.base import NormalizedMessage
    return NormalizedMessage(
        sender_id=sender, text="", channel_id=chat, thread_id="1", timestamp="0", files=[],
        metadata={"bot_id": "b", "agent_name": "my-agent", "is_group": is_group,
                  "chat_type": "group" if is_group else "private", "username": sender},
    )


@pytest.mark.parametrize("mod_name,cls_name,getter", [
    ("adapters.telegram_adapter", "TelegramAdapter", "get_telegram_binding"),
    ("adapters.whatsapp_adapter", "WhatsAppAdapter", "get_whatsapp_binding"),
])
class TestCommandChannels:
    def _setup(self, mod_name, cls_name, getter):
        import importlib
        mod = importlib.import_module(mod_name)
        db = MagicMock()
        getattr(db, getter).return_value = {"id": 1, "agent_name": "my-agent"}
        db.count_recent_code_requests.return_value = 0
        db.create_login_code.return_value = {"code": "111111"}
        email_cls = MagicMock()
        email_cls.return_value.send_verification_code = MagicMock(side_effect=_send_ok)
        return mod, getattr(mod, cls_name)(), db, email_cls

    def _login(self, mod, adapter, db, email_cls, email, sender="u1", **kw):
        text = f"/login {email}"
        with patch.object(mod, "db", db), \
             patch("database.db", db), patch.object(mod, "_set_pending_login"), \
             patch.object(mod, "EmailService", email_cls):
            return _run(adapter._handle_login_command(_message(sender, **kw), text))

    def test_fourth_request_for_an_address_sends_nothing(self, mod_name, cls_name, getter):
        mod, adapter, db, email_cls = self._setup(mod_name, cls_name, getter)
        replies = [self._login(mod, adapter, db, email_cls, "v@example.com", sender=f"u{i}")
                   for i in range(4)]
        assert email_cls.return_value.send_verification_code.call_count == 3
        assert db.create_login_code.call_count == 3
        assert "Too many" in replies[3]
        assert "Too many" not in replies[2]

    def test_sender_rotating_addresses_is_capped(self, mod_name, cls_name, getter):
        mod, adapter, db, email_cls = self._setup(mod_name, cls_name, getter)
        for i in range(6):
            self._login(mod, adapter, db, email_cls, f"a{i}@example.com")
        assert email_cls.return_value.send_verification_code.call_count == 5

    def test_policy_refused_mode_is_capped_too(self, mod_name, cls_name, getter):
        from services import login_policy_gate

        class _Refuse:
            def email_code_allowed(self):
                return False

        login_policy_gate.register_provider(_Refuse())
        mod, adapter, db, email_cls = self._setup(mod_name, cls_name, getter)
        mod._login_code_tasks.clear()
        try:
            replies = [self._login(mod, adapter, db, email_cls, "v@example.com", sender=f"u{i}")
                       for i in range(4)]
            assert len(mod._login_code_tasks) == 3
            assert "Too many" in replies[3]
        finally:
            # The tasks belong to loops that are gone; leaving them would break
            # the next test that gathers this set.
            mod._login_code_tasks.clear()

    def test_invalid_email_does_not_burn_budget(self, mod_name, cls_name, getter):
        mod, adapter, db, email_cls = self._setup(mod_name, cls_name, getter)
        for _ in range(6):
            self._login(mod, adapter, db, email_cls, "not-an-email")
        self._login(mod, adapter, db, email_cls, "v@example.com")
        assert email_cls.return_value.send_verification_code.call_count == 1


def test_telegram_group_members_have_their_own_budget():
    import adapters.telegram_adapter as mod
    db = MagicMock()
    db.get_telegram_binding.return_value = {"id": 1, "agent_name": "my-agent"}
    db.count_recent_code_requests.return_value = 0
    db.create_login_code.return_value = {"code": "111111"}
    email_cls = MagicMock()
    email_cls.return_value.send_verification_code = MagicMock(side_effect=_send_ok)
    adapter = mod.TelegramAdapter()
    with patch.object(mod, "db", db), patch("database.db", db), \
         patch.object(mod, "_set_pending_login"), patch.object(mod, "EmailService", email_cls):
        for i in range(5):
            _run(adapter._handle_login_command(
                _message("u1", is_group=True, chat="g1"), f"/login a{i}@example.com"))
        reply = _run(adapter._handle_login_command(
            _message("u2", is_group=True, chat="g1"), "/login b@example.com"))
    assert "Too many" not in reply
    assert email_cls.return_value.send_verification_code.call_count == 6


# ------------------------------------------------------------------ Slack

class TestSlack:
    def _msg(self, text, user):
        from adapters.base import NormalizedMessage
        return NormalizedMessage(
            sender_id=user, text=text, channel_id="D1", thread_id=None, timestamp="0",
            files=[], metadata={"team_id": "T1"},
        )

    def test_fourth_request_for_an_address_sends_nothing(self):
        import adapters.slack_adapter as mod
        db = MagicMock()
        db.get_slack_workspace_bot_token.return_value = "xoxb-test"
        db.get_slack_connection_by_team.return_value = {"require_email": True, "link_id": "L1"}
        db.get_slack_user_verification.return_value = None
        db.get_slack_pending_verification.return_value = {"state": "awaiting_email"}
        db.count_recent_code_requests.return_value = 0
        sent, emailed = [], []

        async def _send_message(token, channel, text, *a, **k):
            sent.append(text)

        async def _send_code(email, code, *a, **k):
            emailed.append(email)
            return True

        adapter = mod.SlackAdapter()
        with patch.object(mod, "db", db), patch("database.db", db), \
             patch.object(mod.slack_service, "send_message", _send_message), \
             patch.object(mod.email_service, "send_verification_code", _send_code):
            for i in range(4):
                _run(adapter.handle_verification(self._msg("v@example.com", f"U{i}")))
        assert emailed == ["v@example.com"] * 3
        assert "Too many" in sent[3]
        assert "Too many" not in sent[2]


# ------------------------------------------------- rate_limiter fallback

def test_inprocess_fallback_is_bounded(monkeypatch):
    """Channel login keys on user input, so the fallback must not grow without bound."""
    from services import rate_limiter
    monkeypatch.setattr(rate_limiter, "_INPROCESS_MAX_KEYS", 3)
    for i in range(5):
        rate_limiter.check(f"k{i}", 1, 600)
    assert list(rate_limiter._inprocess_buckets) == ["k2", "k3", "k4"]


def test_refund_returns_the_hit():
    from services import rate_limiter
    r = rate_limiter.check("k", 1, 600)
    assert not rate_limiter.check("k", 1, 600).allowed
    rate_limiter.refund("k", r)
    assert rate_limiter.check("k", 1, 600).allowed
