"""
Unit tests for #951 — post-approval channel notification.

`ProactiveMessageService.send_access_grant_notification` is a one-shot
notification path that bypasses the `allow_proactive` opt-in and the
per-recipient rate limit (the user explicitly initiated the request; this
is the response). The router's decide endpoint fires it as a background
task so a missing channel binding or transport hiccup never blocks the
approval response.

These tests mock the per-channel `_deliver_*` helpers and assert the
audit envelope captures the delivered/skipped/failed outcome.
"""

import asyncio
import os
import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest


_backend_path = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "src", "backend")
)
if _backend_path not in sys.path:
    sys.path.insert(0, _backend_path)


# Modules stubbed by the fixture below — snapshot/restored around each test so
# the stubs don't leak into sibling test files. Pattern matches
# tests/unit/test_telegram_webhook_backfill.py (see tests/lint_sys_modules.py).
_STUBBED_MODULE_NAMES = [
    "database",
    "services.platform_audit_service",
    "services.proactive_message_service",
    "proactive_message_service",
]


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    saved = {name: sys.modules.get(name) for name in _STUBBED_MODULE_NAMES}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


@pytest.fixture
def proactive_service(monkeypatch):
    """Load ProactiveMessageService with stubbed database + audit modules."""
    fake_db_mod = types.ModuleType("database")
    fake_db_mod.db = MagicMock()
    monkeypatch.setitem(sys.modules, "database", fake_db_mod)

    fake_audit_mod = types.ModuleType("services.platform_audit_service")

    class _AuditEventType:
        PROACTIVE_MESSAGE = "proactive_message"

    fake_audit = MagicMock()
    fake_audit.log = AsyncMock(return_value="evt-1")
    fake_audit_mod.platform_audit_service = fake_audit
    fake_audit_mod.AuditEventType = _AuditEventType
    monkeypatch.setitem(
        sys.modules, "services.platform_audit_service", fake_audit_mod
    )

    # Force a fresh import so the stubs above take effect. The
    # autouse `_restore_sys_modules` fixture above puts the prior
    # cached modules back, so this is safe.
    monkeypatch.delitem(sys.modules, "services.proactive_message_service", raising=False)
    monkeypatch.delitem(sys.modules, "proactive_message_service", raising=False)

    from services.proactive_message_service import ProactiveMessageService

    return ProactiveMessageService(), fake_audit


def _run(coro):
    return asyncio.run(coro)


def test_telegram_delivery_success_audited(proactive_service):
    service, audit = proactive_service
    service._deliver_via_channel = AsyncMock(
        return_value=MagicMock(success=True, channel="telegram", error=None)
    )

    result = _run(
        service.send_access_grant_notification(
            agent_name="research-bot",
            recipient_email="alice@example.com",
            channel="telegram",
            text="Access granted!",
        )
    )

    assert result.success is True
    assert result.channel == "telegram"

    audit.log.assert_awaited_once()
    kwargs = audit.log.call_args.kwargs
    assert kwargs["actor_agent_name"] == "research-bot"
    assert kwargs["target_type"] == "user"
    assert kwargs["target_id"] == "alice@example.com"
    assert kwargs["details"]["channel"] == "telegram"
    assert kwargs["details"]["success"] is True
    assert kwargs["details"]["error"] is None


def test_recipient_not_found_returns_failure_not_raise(proactive_service):
    """If the requester unbound the channel between request and approval,
    we must NOT raise — the approval succeeded; the notification is best-
    effort, and the audit row records the miss."""
    service, audit = proactive_service
    from services.proactive_message_service import RecipientNotFoundError

    service._deliver_via_channel = AsyncMock(
        side_effect=RecipientNotFoundError("no telegram link")
    )

    result = _run(
        service.send_access_grant_notification(
            agent_name="research-bot",
            recipient_email="alice@example.com",
            channel="telegram",
            text="Access granted!",
        )
    )

    assert result.success is False
    assert result.channel == "telegram"
    assert "no telegram link" in (result.error or "")

    audit.log.assert_awaited_once()
    kwargs = audit.log.call_args.kwargs
    assert kwargs["details"]["success"] is False
    assert "recipient_not_found" in kwargs["details"]["error"]


def test_unexpected_exception_audited_and_swallowed(proactive_service):
    """Transport-layer crash must not surface to the caller — audit only."""
    service, audit = proactive_service

    service._deliver_via_channel = AsyncMock(
        side_effect=RuntimeError("twilio went pop")
    )

    result = _run(
        service.send_access_grant_notification(
            agent_name="research-bot",
            recipient_email="bob@example.com",
            channel="whatsapp",
            text="Access granted!",
        )
    )

    assert result.success is False
    assert result.error == "twilio went pop"

    audit.log.assert_awaited_once()
    kwargs = audit.log.call_args.kwargs
    assert kwargs["details"]["channel"] == "whatsapp"
    assert kwargs["details"]["success"] is False
    assert "twilio went pop" in kwargs["details"]["error"]


def test_opt_in_check_is_bypassed(proactive_service):
    """The opt-in `allow_proactive` check that gates `send_message()` must
    NOT apply here — the user explicitly initiated the request."""
    service, audit = proactive_service

    # Wire the db mock so `can_agent_message_email` would say NO if asked.
    import database
    database.db.can_agent_message_email = MagicMock(return_value=False)

    service._deliver_via_channel = AsyncMock(
        return_value=MagicMock(success=True, channel="slack", error=None)
    )

    result = _run(
        service.send_access_grant_notification(
            agent_name="research-bot",
            recipient_email="alice@example.com",
            channel="slack",
            text="Access granted!",
        )
    )

    # Delivered, audit recorded — opt-in was never consulted.
    assert result.success is True
    database.db.can_agent_message_email.assert_not_called()
