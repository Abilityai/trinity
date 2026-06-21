"""Inbound auth for the in-container agent server (#1159).

Every backend→agent call carries a per-agent ``X-Trinity-Agent-Token`` (derived
backend-side from a stable master secret — see
``src/backend/services/agent_auth.py``). This middleware verifies it on every
route — HTTP **and** WebSocket — so a compromised/prompt-injected sibling on
``trinity-agent-network`` can no longer read this agent's ``.env`` secrets or run
arbitrary Claude code on it.

**Pure ASGI, not ``BaseHTTPMiddleware``** (deliberate): the agent server serves
long-lived SSE streams (``/api/executions/{id}/stream``, ``text/event-stream``).
``BaseHTTPMiddleware`` only sees ``http`` scopes and historically buffers
streaming response bodies; a pure-ASGI pass-through never touches the body, so
SSE keeps streaming incrementally. It also enforces on ``websocket`` scopes —
which ``BaseHTTPMiddleware`` cannot see at all — so the boundary is
scope-complete. That mattered because a ``/ws/chat`` route used to run
``runtime.execute`` (arbitrary Claude) with no auth; it has since been removed
(dead code), and WebSocket coverage here keeps any *future* WS route
authenticated by default instead of silently bypassing the guard.

Two deliberate HTTP exemptions:
  * exact path ``/health`` — the Docker healthcheck / readiness probe curls it
    with no token, and its payload is low-sensitivity runtime counts (F1).
  * ``OPTIONS`` — CORS preflight carries no custom header.
WebSocket has no exemptions (no ``/health`` WS, no preflight).

**Grace path:** when ``TRINITY_AGENT_AUTH_TOKEN`` is empty/unset the middleware
allows everything. This makes the rollout non-breaking — a fleet still running
the old base image (no token injected) keeps working until the operator-timed
recreate injects tokens and flips enforcement on. The backend half is the
opposite of fail-open: it *hard-fails* if ``AGENT_AUTH_SECRET`` is unset, so the
only way to reach this grace path is an un-recreated agent, never a misconfig.
"""
import hmac
import os

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Wire header + injected env var. Must stay in lockstep with the backend
# (``services/agent_auth.AGENT_AUTH_HEADER`` and the ``TRINITY_AGENT_AUTH_TOKEN``
# env injected at create/recreate).
AGENT_AUTH_HEADER = "X-Trinity-Agent-Token"
AGENT_AUTH_TOKEN_ENV = "TRINITY_AGENT_AUTH_TOKEN"
_EXEMPT_PATH = "/health"
# RFC 6455 policy-violation close code for a rejected WebSocket handshake.
_WS_POLICY_VIOLATION = 1008


class AgentAuthMiddleware:
    """Reject any request lacking the correct per-agent token (with grace).

    Pure-ASGI so it covers ``http`` + ``websocket`` and never buffers streaming
    response bodies.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Only HTTP and WebSocket carry inbound auth; lifespan/other pass through.
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        # Read per-request (not at construction) so a hot-reloaded token env
        # takes effect on the next request without restarting the server, and
        # so the test suite can flip the env between cases.
        expected = os.getenv(AGENT_AUTH_TOKEN_ENV, "")

        # Grace: no token configured → don't enforce (rollout / old image).
        if not expected:
            await self.app(scope, receive, send)
            return

        if scope["type"] == "http":
            # Exemptions: Docker healthcheck and CORS preflight.
            if scope.get("method") == "OPTIONS" or scope.get("path") == _EXEMPT_PATH:
                await self.app(scope, receive, send)
                return
            if self._token_ok(scope, expected):
                await self.app(scope, receive, send)
                return
            response = JSONResponse({"detail": "Unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return

        # WebSocket: no exemptions. Reject before the app can accept() by
        # sending a close in response to the initial connect (the Starlette
        # WebSocketClose pattern) so a sibling can't open any websocket untokened.
        if self._token_ok(scope, expected):
            await self.app(scope, receive, send)
            return
        await self._deny_websocket(receive, send)

    @staticmethod
    def _token_ok(scope: Scope, expected: str) -> bool:
        provided = Headers(scope=scope).get(AGENT_AUTH_HEADER, "")
        return bool(provided) and hmac.compare_digest(provided, expected)

    @staticmethod
    async def _deny_websocket(receive: Receive, send: Send) -> None:
        message: Message = await receive()
        if message["type"] == "websocket.connect":
            await send({"type": "websocket.close", "code": _WS_POLICY_VIOLATION})


__all__ = ["AgentAuthMiddleware", "AGENT_AUTH_HEADER", "AGENT_AUTH_TOKEN_ENV"]
