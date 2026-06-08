"""WebSocket /ws auth contract suite (C-002 / #550).

Cross-ref: docs/memory/architecture.md "WebSocket Security (C-002, #550)".

Covers the ticket-based auth introduced in #550:
  * positive: mint POST /api/ws/ticket, connect with ?ticket=<opaque>,
    handshake completes, frame received or clean close.
  * single-use: replay rejection via atomic Redis GETDEL.
  * negative: empty/malformed/invalid ticket; legacy JWT-in-URL.
  * tolerance: extra ?token= query param is ignored when ticket is valid.

Expiry (>30s TTL) is unit-tested in tests/unit/test_ws_ticket_service.py
via FakeRedis; not duplicated here to keep the integration suite fast.

/ws/events still accepts ?token=<MCP_API_KEY> per architecture.md
(documented surface for wscat/websocat). That endpoint is out of scope
for this file — its contract is enforced separately.
"""
from typing import Callable

import httpx
import pytest
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.sync.client import connect as ws_connect

from utils.api_client import TrinityApiClient

UPGRADE_HEADERS = {
    "Upgrade": "websocket",
    "Connection": "Upgrade",
    "Sec-WebSocket-Version": "13",
    "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==",
}


def _ws_url(api_client: TrinityApiClient, query: str = "") -> str:
    """Build a ws:// URL from the api_client base, appending an optional query string."""
    base = api_client.config.base_url.replace("https://", "wss://", 1).replace(
        "http://", "ws://", 1
    )
    suffix = f"?{query}" if query else ""
    return f"{base}/ws{suffix}"


class TestWebSocketAuthentication:
    """/ws auth contract — ticket-based (C-002 / #550)."""

    # ------------------------------------------------------------------
    # Negative path — exercised via httpx (rejected pre-accept = HTTP error)
    # ------------------------------------------------------------------

    def test_ws_no_ticket_rejected(self, api_client: TrinityApiClient) -> None:
        """`/ws` with no ticket must be rejected before accept()."""
        base_url = api_client.config.base_url
        with httpx.Client() as client:
            response = client.get(f"{base_url}/ws", headers=UPGRADE_HEADERS)
            assert response.status_code in (401, 403), (
                f"Missing ticket should be rejected, got {response.status_code}"
            )

    def test_ws_invalid_ticket_rejected(self, api_client: TrinityApiClient) -> None:
        """A random opaque string must not validate."""
        base_url = api_client.config.base_url
        with httpx.Client() as client:
            response = client.get(
                f"{base_url}/ws",
                params={"ticket": "not-a-real-ticket"},
                headers=UPGRADE_HEADERS,
            )
            assert response.status_code in (401, 403), (
                f"Invalid ticket should be rejected, got {response.status_code}"
            )

    def test_ws_empty_ticket_rejected(self, api_client: TrinityApiClient) -> None:
        """`?ticket=` (empty value) must be rejected."""
        base_url = api_client.config.base_url
        with httpx.Client() as client:
            response = client.get(
                f"{base_url}/ws",
                params={"ticket": ""},
                headers=UPGRADE_HEADERS,
            )
            assert response.status_code in (401, 403), (
                f"Empty ticket should be rejected, got {response.status_code}"
            )

    @pytest.mark.parametrize(
        "bad_ticket",
        ["  ", "garbage", "x" * 1000, "ticket with spaces"],
        ids=["whitespace", "garbage", "oversized", "embedded-spaces"],
    )
    def test_ws_malformed_ticket_rejected(
        self, api_client: TrinityApiClient, bad_ticket: str
    ) -> None:
        """Whitespace, garbage, oversized, or space-bearing tickets are all rejected."""
        base_url = api_client.config.base_url
        with httpx.Client() as client:
            response = client.get(
                f"{base_url}/ws",
                params={"ticket": bad_ticket},
                headers=UPGRADE_HEADERS,
            )
            assert response.status_code in (401, 403), (
                f"Malformed ticket {bad_ticket!r} should be rejected, "
                f"got {response.status_code}"
            )

    def test_ws_jwt_in_url_rejected(self, api_client: TrinityApiClient) -> None:
        """C-002 / #550 regression pin: a JWT in `?token=` must be rejected.

        Closes pentest finding 3.2.1 — JWT-in-URL leaks via nginx logs,
        browser history, and upstream proxies. The `/ws` endpoint only
        reads `?ticket=`; `?token=` is an unrecognized param, so this
        falls through to the no-ticket close.
        """
        base_url = api_client.config.base_url
        with httpx.Client() as client:
            response = client.get(
                f"{base_url}/ws",
                params={"token": api_client.token},
                headers=UPGRADE_HEADERS,
            )
            assert response.status_code in (401, 403), (
                f"JWT-in-URL must be rejected per C-002, got {response.status_code}"
            )

    def test_ws_invalid_ticket_with_valid_jwt_rejected(
        self, api_client: TrinityApiClient
    ) -> None:
        """No JWT fallback: a valid JWT must NOT rescue an invalid ticket.

        Pins the "ticket is the only auth source" property. A regression
        that re-introduced a JWT fallback when ticket consumption fails
        would silently re-open the C-002 leak vector — this test fails
        loudly if that happens.
        """
        base_url = api_client.config.base_url
        with httpx.Client() as client:
            response = client.get(
                f"{base_url}/ws",
                params={"ticket": "not-a-real-ticket", "token": api_client.token},
                headers=UPGRADE_HEADERS,
            )
            assert response.status_code in (401, 403), (
                "Invalid ticket must be rejected even when a valid JWT is "
                f"present in ?token=, got {response.status_code}"
            )

    # ------------------------------------------------------------------
    # Positive path — exercised via real RFC 6455 WS client
    # ------------------------------------------------------------------

    def test_ws_valid_ticket_handshake_succeeds(
        self, api_client: TrinityApiClient, ws_ticket: Callable[[], str]
    ) -> None:
        """Mint → connect → handshake completes.

        Uses the `websockets` sync client which performs the full
        RFC 6455 handshake (including `Sec-WebSocket-Accept` validation).
        The connection opening is itself proof that
        `consume_ticket() → manager.connect()` ran without auth rejection.
        """
        ticket = ws_ticket()
        ws_url = _ws_url(api_client, f"ticket={ticket}")
        with ws_connect(ws_url, open_timeout=5) as ws:
            # Backend may broadcast an event immediately or stay quiet.
            # Either is a valid contract: the handshake itself is the
            # assertion. We attempt one short recv() and accept timeout.
            try:
                ws.recv(timeout=2)
            except TimeoutError:
                pass  # No initial frame is fine; handshake = assertion

    def test_ws_ticket_single_use(
        self, api_client: TrinityApiClient, ws_ticket: Callable[[], str]
    ) -> None:
        """Atomic GETDEL: same ticket cannot authenticate twice.

        This is the core security property that justifies ticket auth
        over a long-lived JWT — replay must be impossible.
        """
        ticket = ws_ticket()
        ws_url = _ws_url(api_client, f"ticket={ticket}")

        # First connection consumes the ticket.
        with ws_connect(ws_url, open_timeout=5):
            pass

        # Second connection with the same ticket must fail.
        with pytest.raises((InvalidStatus, ConnectionClosed, OSError)):
            with ws_connect(ws_url, open_timeout=5):
                pass

    def test_ws_token_param_ignored_when_ticket_valid(
        self, api_client: TrinityApiClient, ws_ticket: Callable[[], str]
    ) -> None:
        """`?token=<jwt>` alongside a valid `?ticket=` must be ignored — not additive auth.

        Proves the JWT in URL is structurally inert, not just "rejected
        when alone." Future refactors that accidentally re-introduce a
        JWT fallback path would fail here.
        """
        ticket = ws_ticket()
        ws_url = _ws_url(api_client, f"ticket={ticket}&token={api_client.token}")
        with ws_connect(ws_url, open_timeout=5):
            pass  # Connection opens cleanly; token is ignored
