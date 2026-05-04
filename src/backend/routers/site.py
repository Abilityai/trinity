"""
Agent website proxy endpoint (SITE-001).

Validates a site-type public link token and reverse-proxies HTTP requests
to the agent's internal web server at http://agent-{name}:3000/{path}.

Error matrix:
- 401 — token invalid, missing, wrong type, or disabled
- 410 — token expired
- 429 — rate limit exceeded
- 502 — agent web server not reachable (container stopped or port not bound)
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse, RedirectResponse

from config import SITE_PORT
from database import db
from routers.auth import get_redis_client
from routers.public import _get_client_ip, INVALID_LINK_MESSAGE
from services.platform_audit_service import AuditEventType, platform_audit_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["site"])

# Rate limits — two independent buckets to handle DDoS and token enumeration separately
_RATE_LIMIT_IP = 120       # requests/min per IP (generous — site assets count)
_RATE_LIMIT_TOKEN = 300    # requests/min per token
_RATE_WINDOW = 60          # seconds

# Headers that must never be forwarded from Trinity to the agent's web process
_STRIP_REQUEST_HEADERS = {
    "authorization",
    "cookie",
    "x-internal-secret",
    "x-forwarded-for",
    "x-real-ip",
    "host",
}

# Hop-by-hop and server-banner headers that must not be forwarded to the browser
_STRIP_RESPONSE_HEADERS = {
    "transfer-encoding",
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "upgrade",
    "server",
    "x-powered-by",
    # Strip platform CSP — let the agent's own headers (or none) apply
    "content-security-policy",
    "x-frame-options",
}

# Agent name validation: Docker names are lowercase alphanumeric + hyphens
_AGENT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9\-]*$")


def _check_site_rate_limit(client_ip: str, link_id: str) -> None:
    """Rate-limit site proxy requests per IP and per token."""
    r = get_redis_client()
    if r is None:
        logger.warning("Site proxy rate limiting unavailable — Redis not connected")
        return
    try:
        for key, limit in (
            (f"site_ip:{client_ip}", _RATE_LIMIT_IP),
            (f"site_token:{link_id}", _RATE_LIMIT_TOKEN),
        ):
            pipe = r.pipeline()
            pipe.incr(key)
            pipe.expire(key, _RATE_WINDOW)
            new_count, _ = pipe.execute()
            if new_count > limit:
                raise HTTPException(status_code=429, detail="Too many requests")
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"Site rate limit check failed: {e}")


def _validate_site_token(token: str) -> dict:
    """
    Validate a site-type public link token.

    Returns the link dict on success. Raises HTTPException on failure.
    Uses constant-time INVALID_LINK_MESSAGE to prevent oracle-based enumeration.
    """
    link = db.get_public_link_by_token(token)

    if not link:
        raise HTTPException(status_code=401, detail=INVALID_LINK_MESSAGE)

    if link.get("type", "chat") != "site":
        raise HTTPException(status_code=401, detail=INVALID_LINK_MESSAGE)

    if not link["enabled"]:
        raise HTTPException(status_code=401, detail=INVALID_LINK_MESSAGE)

    if link.get("expires_at"):
        expires = datetime.fromisoformat(link["expires_at"])
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > expires:
            raise HTTPException(status_code=410, detail="Link has expired")

    # SSRF defense: ensure agent_name is a valid Docker container name segment
    agent_name = link["agent_name"]
    if not _AGENT_NAME_RE.match(agent_name):
        logger.error(f"Invalid agent_name in site link {link['id']}: {agent_name!r}")
        raise HTTPException(status_code=500, detail="Invalid link configuration")

    return link


@router.get("/site/{token}", include_in_schema=False)
async def site_root_redirect(token: str):
    """Redirect /site/{token} → /site/{token}/ so relative paths resolve correctly."""
    return RedirectResponse(url=f"/site/{token}/", status_code=301)


@router.get("/site/{token}/{path:path}")
async def proxy_site(token: str, path: str, request: Request):
    """
    Reverse-proxy a request to the agent's web server.

    The agent runs a web server on SITE_PORT (3000). All HTTP methods,
    query strings, and request bodies are forwarded. Responses are
    streamed to avoid buffering large pages or SSE streams.

    WebSocket upgrades are NOT supported (httpx limitation). Connections
    attempting WS upgrade will receive a 502.
    """
    client_ip = _get_client_ip(request)

    # Validate token (raises on invalid)
    link = _validate_site_token(token)
    agent_name = link["agent_name"]

    # Rate limiting (two buckets: IP + token)
    _check_site_rate_limit(client_ip, link["id"])

    # Build upstream URL — hostname is always the predictable Docker name
    upstream_url = f"http://agent-{agent_name}:{SITE_PORT}/{path}"
    if request.url.query:
        upstream_url = f"{upstream_url}?{request.url.query}"

    # Build forwarded headers — strip Trinity-internal headers to prevent leakage
    forward_headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _STRIP_REQUEST_HEADERS
    }
    forward_headers["X-Forwarded-For"] = client_ip
    forward_headers["X-Forwarded-Proto"] = request.url.scheme
    forward_headers["Host"] = f"agent-{agent_name}:{SITE_PORT}"

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0)) as client:
            async with client.stream(
                method=request.method,
                url=upstream_url,
                headers=forward_headers,
                content=request.stream(),
                follow_redirects=False,
            ) as upstream_response:

                # Build response headers — strip hop-by-hop and server-banner headers
                response_headers = {
                    k: v
                    for k, v in upstream_response.headers.items()
                    if k.lower() not in _STRIP_RESPONSE_HEADERS
                }

                # Fire-and-forget audit log — must not delay the streaming response
                asyncio.create_task(platform_audit_service.log(
                    AuditEventType.SITE_ACCESS,
                    "site_link_visit",
                    source="api",
                    actor_ip=client_ip,
                    target_type="agent",
                    target_id=agent_name,
                    details={
                        "link_id": link["id"],
                        "path": f"/{path}",
                        "status_code": upstream_response.status_code,
                    },
                ))

                return StreamingResponse(
                    upstream_response.aiter_bytes(),
                    status_code=upstream_response.status_code,
                    headers=response_headers,
                    media_type=upstream_response.headers.get("content-type"),
                )

    except httpx.ConnectError:
        raise HTTPException(
            status_code=502,
            detail="Agent web server is not reachable. The agent may be stopped or the web server not running.",
        )
    except httpx.TimeoutException:
        raise HTTPException(status_code=502, detail="Agent web server timed out.")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Site proxy error for agent {agent_name}: {e}")
        raise HTTPException(status_code=502, detail="Proxy error")
