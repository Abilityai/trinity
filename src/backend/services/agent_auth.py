"""Per-agent auth tokens for the in-container agent server (#1159).

The in-container agent server (``docker/base-image/agent_server``, port 8000)
historically had **zero** inbound authentication: any process on
``trinity-agent-network`` could read another agent's ``.env`` secrets or run
arbitrary Claude executions on a sibling. This module is the backend half of
the fix — every backend→agent HTTP call carries a per-agent token that the
agent middleware verifies.

**Derive, don't store.** ``start.sh`` persists ``AGENT_AUTH_SECRET`` once (a
stable openssl-hex-32 master, like ``SECRET_KEY`` / ``INTERNAL_API_SECRET``),
so each agent's token is a pure function of (master, name) rather than a stored
column — no DB migration, no encryption mixin. The backend is the sole holder
of the master; an agent only ever receives *its own* token, so a compromised
agent A holds ``token_A`` but cannot compute ``token_B`` without the master
(which never leaves the backend). Security is identical to random per-agent
tokens; revocation is rotate-master + recreate-fleet (not an AC requirement).

Fail-closed: deriving from an empty/missing secret raises rather than silently
producing a token off ``""`` (matches the no-silent-fallback rule). The backend
therefore hard-fails the first time it tries to talk to an agent if the operator
ran without ``AGENT_AUTH_SECRET`` set.
"""
from __future__ import annotations

import hashlib
import hmac
import os
from typing import Any, Mapping, Optional

import httpx

# Wire header carrying the per-agent token. Named so the credential sanitizer's
# ``.*TOKEN.*`` pattern still redacts it in any transcript that echoes headers.
AGENT_AUTH_HEADER = "X-Trinity-Agent-Token"

# Versioned domain-separation prefix. Bump the ``v1`` if the derivation ever
# changes so old and new tokens can't be confused (a future per-agent ``:vN``
# revocation label would extend this).
_TOKEN_DOMAIN_PREFIX = b"trinity-agent-auth:v1:"


def _agent_auth_secret() -> bytes:
    """Return the master secret as bytes, or raise if unset/empty.

    Fail-closed by design: we never derive a token from ``""`` because that
    would hand every agent the same predictable token and silently defeat the
    whole feature. A missing secret is an operator misconfiguration that must
    surface loudly, not degrade to "no auth".
    """
    secret = os.getenv("AGENT_AUTH_SECRET")
    if not secret:
        raise RuntimeError(
            "AGENT_AUTH_SECRET is not set. The backend cannot derive per-agent "
            "auth tokens for the in-container agent server (#1159). start.sh "
            "auto-generates it on first boot; ensure it is set in .env and that "
            "docker-compose forwards it to the backend service."
        )
    return secret.encode()


def derive_agent_token(agent_name: str) -> str:
    """Derive the stable per-agent token: ``HMAC-SHA256(secret, prefix+name)``."""
    return hmac.new(
        _agent_auth_secret(),
        _TOKEN_DOMAIN_PREFIX + agent_name.encode(),
        hashlib.sha256,
    ).hexdigest()


def build_agent_auth_headers(agent_name: str) -> dict[str, str]:
    """Build the auth header dict for a backend→agent request."""
    return {AGENT_AUTH_HEADER: derive_agent_token(agent_name)}


def merge_auth_headers(
    agent_name: str, existing: Optional[Mapping[str, str]]
) -> dict[str, str]:
    """Merge the derived auth header into ``existing``, overriding any
    caller-supplied token **case-insensitively**.

    Used by the two central HTTP clients so a caller can pass its own headers
    (Content-Type, etc.) without being able to spoof — or accidentally pin a
    stale — ``X-Trinity-Agent-Token`` (Codex #9).
    """
    merged = {
        k: v
        for k, v in (existing or {}).items()
        if k.lower() != AGENT_AUTH_HEADER.lower()
    }
    merged[AGENT_AUTH_HEADER] = derive_agent_token(agent_name)
    return merged


def agent_httpx_client(agent_name: str, **kwargs: Any) -> httpx.AsyncClient:
    """Return an ``httpx.AsyncClient`` pre-stamped with the agent's auth header.

    The token rides as a *default* header, so every request the client makes —
    including ``client.stream(...)`` and any fallback call inside the same
    ``async with`` block — carries it. Residual inline callers get auth by the
    one-line swap ``httpx.AsyncClient(...)`` → ``agent_httpx_client(name, ...)``.

    Only use this for clients that talk **exclusively** to ``agent-{name}:8000``
    (the default header would otherwise be sent to whatever other host the
    client hit). For mixed clients, pass ``headers=build_agent_auth_headers(name)``
    on the individual agent request instead.
    """
    caller_headers = kwargs.pop("headers", None) or {}
    headers = merge_auth_headers(agent_name, caller_headers)
    return httpx.AsyncClient(headers=headers, **kwargs)
