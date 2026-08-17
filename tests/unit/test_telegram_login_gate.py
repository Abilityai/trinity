"""
Telegram /login access-gate unit tests.

Covers the change to `TelegramAdapter._handle_login_command` that runs the
same access gate as `ChannelMessageRouter` inline after a successful
verification, so the user learns immediately whether they're in or queued
for approval — instead of seeing "you can chat normally now" followed by
"access pending" on the next message.

Related flow: docs/memory/feature-flows/unified-channel-access-control.md
"""

import asyncio
import os
import sys
import types
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

# Add backend to path for direct imports.
_backend_path = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "src", "backend")
)
if _backend_path not in sys.path:
    sys.path.insert(0, _backend_path)

# Stub utils.helpers (tests/utils shadows src/backend/utils in this env).
if "utils.helpers" not in sys.modules:
    _helpers = types.ModuleType("utils.helpers")
    _helpers.utc_now = lambda: datetime.utcnow()
    _helpers.utc_now_iso = lambda: datetime.utcnow().isoformat() + "Z"
    _helpers.to_utc_iso = lambda v: str(v)
    _helpers.parse_iso_timestamp = lambda s: datetime.fromisoformat(s.rstrip("Z"))
    sys.modules["utils.helpers"] = _helpers

# Stub the `database` module so importing telegram_adapter doesn't try to
# initialize /data/trinity.db on the host. Tests replace individual attrs
# of this stub per-case via patch("adapters.telegram_adapter.db", ...).
if "database" not in sys.modules:
    _database_stub = types.ModuleType("database")
    _database_stub.db = MagicMock()
    sys.modules["database"] = _database_stub

# Stub routers.auth.get_redis_client so the pending-login helpers don't
# try to talk to Redis; tests patch the helpers directly anyway.
if "routers" not in sys.modules:
    _routers_pkg = types.ModuleType("routers")
    _routers_pkg.__path__ = []  # mark as package
    sys.modules["routers"] = _routers_pkg
if "routers.auth" not in sys.modules:
    _routers_auth = types.ModuleType("routers.auth")
    _routers_auth.get_redis_client = lambda: None
    sys.modules["routers.auth"] = _routers_auth
    sys.modules["routers"].auth = _routers_auth


@pytest.fixture
def adapter():
    from adapters.telegram_adapter import TelegramAdapter
    return TelegramAdapter()


@pytest.fixture
def message():
    from adapters.base import NormalizedMessage
    return NormalizedMessage(
        sender_id="tg_user_42",
        text="/login 123456",
        channel_id="tg_chat_42",
        thread_id="1",
        timestamp="0",
        files=[],
        metadata={
            "bot_id": "bot_1",
            "bot_username": "mybot",
            "agent_name": "my-agent",
            "username": "alice",
            "is_group": False,
            "chat_type": "private",
        },
    )


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class TestLoginGateBehavior:
    """Verifies the /login {code} success branch runs the access gate."""

    @pytest.mark.smoke
    def test_verified_and_already_shared_returns_ready(self, adapter, message):
        """Owner/admin/shared user gets 'chat normally' on verify."""
        with patch("adapters.telegram_adapter.db") as mock_db, \
             patch("adapters.telegram_adapter._get_pending_login") as mock_get, \
             patch("adapters.telegram_adapter._clear_pending_login"):
            mock_db.get_telegram_binding.return_value = {"id": 1, "agent_name": "my-agent"}
            mock_get.return_value = "alice@example.com"
            mock_db.verify_login_code.return_value = {"email": "alice@example.com"}
            mock_db.set_telegram_verified_email.return_value = None
            mock_db.get_access_policy.return_value = {
                "require_email": True, "open_access": False, "group_auth_mode": "none"
            }
            mock_db.email_has_agent_access.return_value = True

            reply = _run(adapter._handle_login_command(message, "/login 123456"))

            assert reply is not None
            assert "Verified" in reply
            assert "chat normally" in reply.lower()
            assert "pending" not in reply.lower()
            mock_db.upsert_access_request.assert_not_called()

    @pytest.mark.smoke
    def test_verified_and_open_access_returns_ready(self, adapter, message):
        """open_access=True treats everyone as approved."""
        with patch("adapters.telegram_adapter.db") as mock_db, \
             patch("adapters.telegram_adapter._get_pending_login") as mock_get, \
             patch("adapters.telegram_adapter._clear_pending_login"):
            mock_db.get_telegram_binding.return_value = {"id": 1, "agent_name": "my-agent"}
            mock_get.return_value = "bob@example.com"
            mock_db.verify_login_code.return_value = {"email": "bob@example.com"}
            mock_db.get_access_policy.return_value = {
                "require_email": True, "open_access": True, "group_auth_mode": "none"
            }
            mock_db.email_has_agent_access.return_value = False

            reply = _run(adapter._handle_login_command(message, "/login 123456"))

            assert "chat normally" in reply.lower()
            assert "pending" not in reply.lower()
            mock_db.upsert_access_request.assert_not_called()

    @pytest.mark.smoke
    def test_verified_but_restricted_queues_access_request(self, adapter, message):
        """Restrictive policy: access_request is upserted and reply says 'pending'."""
        with patch("adapters.telegram_adapter.db") as mock_db, \
             patch("adapters.telegram_adapter._get_pending_login") as mock_get, \
             patch("adapters.telegram_adapter._clear_pending_login"):
            mock_db.get_telegram_binding.return_value = {"id": 1, "agent_name": "my-agent"}
            mock_get.return_value = "stranger@example.com"
            mock_db.verify_login_code.return_value = {"email": "stranger@example.com"}
            mock_db.get_access_policy.return_value = {
                "require_email": True, "open_access": False, "group_auth_mode": "none"
            }
            mock_db.email_has_agent_access.return_value = False

            reply = _run(adapter._handle_login_command(message, "/login 123456"))

            assert reply is not None
            assert "Verified" in reply
            assert "pending approval" in reply.lower()
            mock_db.upsert_access_request.assert_called_once_with(
                "my-agent", "stranger@example.com", "telegram"
            )

    @pytest.mark.smoke
    def test_upsert_failure_still_returns_pending_message(self, adapter, message):
        """An error in upsert_access_request must not break the reply."""
        with patch("adapters.telegram_adapter.db") as mock_db, \
             patch("adapters.telegram_adapter._get_pending_login") as mock_get, \
             patch("adapters.telegram_adapter._clear_pending_login"):
            mock_db.get_telegram_binding.return_value = {"id": 1, "agent_name": "my-agent"}
            mock_get.return_value = "stranger@example.com"
            mock_db.verify_login_code.return_value = {"email": "stranger@example.com"}
            mock_db.get_access_policy.return_value = {
                "require_email": True, "open_access": False, "group_auth_mode": "none"
            }
            mock_db.email_has_agent_access.return_value = False
            mock_db.upsert_access_request.side_effect = RuntimeError("db down")

            reply = _run(adapter._handle_login_command(message, "/login 123456"))

            assert "pending approval" in reply.lower()

    @pytest.mark.smoke
    def test_invalid_code_does_not_run_gate(self, adapter, message):
        """A bad code short-circuits; gate primitives must not be called."""
        with patch("adapters.telegram_adapter.db") as mock_db, \
             patch("adapters.telegram_adapter._get_pending_login") as mock_get, \
             patch("adapters.telegram_adapter._clear_pending_login"):
            mock_db.get_telegram_binding.return_value = {"id": 1, "agent_name": "my-agent"}
            mock_get.return_value = "alice@example.com"
            mock_db.verify_login_code.return_value = None  # invalid code

            reply = _run(adapter._handle_login_command(message, "/login 123456"))

            assert "Invalid" in reply or "expired" in reply.lower()
            mock_db.set_telegram_verified_email.assert_not_called()
            mock_db.get_access_policy.assert_not_called()
            mock_db.email_has_agent_access.assert_not_called()
            mock_db.upsert_access_request.assert_not_called()
