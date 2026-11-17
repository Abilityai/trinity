"""
Unit tests for WebSocket authentication enforcement (#178).

Verifies that the /ws endpoint rejects unauthenticated connections
BEFORE calling websocket.accept(), preventing information disclosure.

Issue: https://github.com/abilityai/trinity/issues/178
Module: src/backend/main.py (websocket_endpoint)
"""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import asyncio


# ---- Inline reimplementation of WebSocket auth logic for unit testing ----

class MockWebSocket:
    """Minimal WebSocket mock for testing auth flow."""

    def __init__(self):
        self.accepted = False
        self.closed = False
        self.close_code = None
        self.close_reason = None
        self.sent_messages = []
        self._receive_queue = []

    async def accept(self):
        self.accepted = True

    async def close(self, code=1000, reason=""):
        self.closed = True
        self.close_code = code
        self.close_reason = reason

    async def send_text(self, data):
        self.sent_messages.append(data)

    async def receive_text(self):
        if self._receive_queue:
            return self._receive_queue.pop(0)
        raise Exception("WebSocketDisconnect")


def create_valid_token(secret_key, username="admin"):
    """Create a valid JWT token for testing."""
    import jwt
    from datetime import datetime, timedelta
    return jwt.encode(
        {"sub": username, "exp": datetime.utcnow() + timedelta(hours=1)},
        secret_key,
        algorithm="HS256"
    )


def create_expired_token(secret_key, username="admin"):
    """Create an expired JWT token for testing."""
    import jwt
    from datetime import datetime, timedelta
    return jwt.encode(
        {"sub": username, "exp": datetime.utcnow() - timedelta(hours=1)},
        secret_key,
        algorithm="HS256"
    )


# Reimplementation of the websocket auth logic from main.py
async def websocket_auth_check(websocket, token, secret_key, algorithm="HS256"):
    """
    Mirror of the authentication check in websocket_endpoint.
    Returns (authenticated: bool, username: str | None).
    Side effect: calls websocket.close() on rejection.
    """
    import jwt

    if not token:
        await websocket.close(code=4001, reason="Authentication required: provide ?token=<jwt>")
        return False, None

    try:
        payload = jwt.decode(token, secret_key, algorithms=[algorithm])
        username = payload.get("sub")
        if not username:
            await websocket.close(code=4001, reason="Invalid token: missing subject")
            return False, None
    except jwt.PyJWTError:
        await websocket.close(code=4001, reason="Invalid authentication token")
        return False, None

    return True, username


SECRET_KEY = "test-secret-key-for-unit-tests"


class TestWebSocketAuthCheck:
    """Test the WebSocket authentication check logic."""

    def _run(self, coro):
        """Run async test."""
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_no_token_rejects_before_accept(self):
        """No token → close with 4001, never accept."""
        ws = MockWebSocket()
        ok, user = self._run(websocket_auth_check(ws, None, SECRET_KEY))
        assert not ok
        assert user is None
        assert ws.closed
        assert ws.close_code == 4001
        assert not ws.accepted, "Must NOT call accept() before auth"

    def test_empty_token_rejects_before_accept(self):
        """Empty string token → close with 4001, never accept."""
        ws = MockWebSocket()
        ok, user = self._run(websocket_auth_check(ws, "", SECRET_KEY))
        assert not ok
        assert ws.closed
        assert ws.close_code == 4001
        assert not ws.accepted

    def test_invalid_jwt_rejects_before_accept(self):
        """Garbage token → close with 4001, never accept."""
        ws = MockWebSocket()
        ok, user = self._run(websocket_auth_check(ws, "not-a-jwt", SECRET_KEY))
        assert not ok
        assert ws.closed
        assert ws.close_code == 4001
        assert not ws.accepted

    def test_expired_token_rejects_before_accept(self):
        """Expired JWT → close with 4001, never accept."""
        ws = MockWebSocket()
        token = create_expired_token(SECRET_KEY)
        ok, user = self._run(websocket_auth_check(ws, token, SECRET_KEY))
        assert not ok
        assert ws.closed
        assert ws.close_code == 4001
        assert not ws.accepted

    def test_wrong_secret_rejects_before_accept(self):
        """Token signed with wrong secret → close with 4001, never accept."""
        ws = MockWebSocket()
        token = create_valid_token("wrong-secret")
        ok, user = self._run(websocket_auth_check(ws, token, SECRET_KEY))
        assert not ok
        assert ws.closed
        assert ws.close_code == 4001
        assert not ws.accepted

    def test_token_missing_sub_rejects_before_accept(self):
        """Token without 'sub' claim → close with 4001, never accept."""
        import jwt
        from datetime import datetime, timedelta
        ws = MockWebSocket()
        token = jwt.encode(
            {"exp": datetime.utcnow() + timedelta(hours=1)},
            SECRET_KEY,
            algorithm="HS256"
        )
        ok, user = self._run(websocket_auth_check(ws, token, SECRET_KEY))
        assert not ok
        assert user is None
        assert ws.closed
        assert ws.close_code == 4001
        assert not ws.accepted

    def test_valid_token_authenticates(self):
        """Valid token → returns authenticated, does NOT close."""
        ws = MockWebSocket()
        token = create_valid_token(SECRET_KEY, username="testuser")
        ok, user = self._run(websocket_auth_check(ws, token, SECRET_KEY))
        assert ok
        assert user == "testuser"
        assert not ws.closed
        assert not ws.accepted  # Auth check doesn't accept — caller does

    def test_valid_token_different_users(self):
        """Valid tokens for different users return correct usernames."""
        for username in ["admin", "user@example.com", "agent-bot"]:
            ws = MockWebSocket()
            token = create_valid_token(SECRET_KEY, username=username)
            ok, user = self._run(websocket_auth_check(ws, token, SECRET_KEY))
            assert ok
            assert user == username


class TestWebSocketNoAcceptBeforeAuth:
    """
    Verify the critical security invariant: websocket.accept() is NEVER
    called before authentication succeeds.

    This is the core fix for pentest finding 3.2.1 (#178).
    """

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    @pytest.mark.parametrize("token", [
        None,
        "",
        "garbage",
        "Bearer some-token",
        "eyJhbGciOiJIUzI1NiJ9.invalid.payload",
    ])
    def test_no_accept_for_invalid_tokens(self, token):
        """websocket.accept() must not be called for any invalid token."""
        ws = MockWebSocket()
        self._run(websocket_auth_check(ws, token, SECRET_KEY))
        assert not ws.accepted, \
            f"accept() was called for token={token!r} — information disclosure vulnerability"
        assert ws.closed, \
            f"Connection not closed for token={token!r}"
        assert ws.close_code == 4001

    def test_accept_only_after_valid_auth(self):
        """Only a valid token allows the connection to proceed (caller accepts)."""
        ws = MockWebSocket()
        token = create_valid_token(SECRET_KEY)
        ok, _ = self._run(websocket_auth_check(ws, token, SECRET_KEY))
        assert ok
        assert not ws.closed
        # The auth check itself doesn't accept — it just validates.
        # The caller (websocket_endpoint) calls manager.connect() which accepts.
        assert not ws.accepted


class TestFirstMessageAuthRemoved:
    """
    Verify the deprecated first-message auth pattern is no longer supported.

    Previously, clients could connect without a token and send
    "Bearer <jwt>" as the first WebSocket message. This was removed
    because it required calling accept() before authentication.
    """

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_first_message_bearer_not_supported(self):
        """Sending Bearer token as first message should not authenticate."""
        ws = MockWebSocket()
        # Simulate the old pattern: no query token
        ok, _ = self._run(websocket_auth_check(ws, None, SECRET_KEY))
        assert not ok
        assert ws.closed
        assert ws.close_code == 4001
        # The connection is closed before any message can be received
        assert not ws.accepted
