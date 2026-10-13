"""Caller side: agent tokens and signed requests (ent#623).

Each agent gets a short-lived Ed25519 key of its own, bound into its agent
token's ``cnf.jwk`` and signed by the instance key. Agent keys are never
published ("Agent keys are not published" — bootstrap draft); they live only in
this worker's memory next to the token, which is refreshed when fewer than five
minutes remain. Two workers each hold their own agent key — harmless, because a
verifier only needs the token, which carries the key.

The cache is keyed by ``(issuer, kid, agent)``: a quick-tunnel restart changes
the issuer, and an instance key change changes the kid; either must retire the
old token immediately rather than at its expiry.
"""
from __future__ import annotations

import re
import time
import uuid
from typing import Dict, Optional, Tuple
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from services.aauth import config, httpsig, jose, keys

TOKEN_TYPE = "aa-agent+jwt"
METADATA_DOCUMENT = "aauth-agent.json"
TOKEN_LIFETIME = 3600
REFRESH_MARGIN = 300

#: The draft's `local` grammar minus `+`, which is reserved as the sub-agent
#: delimiter and must never appear in a top-level agent's identity.
_LOCAL_RE = re.compile(r"^[A-Za-z0-9._-]{1,255}$")

_tokens: Dict[Tuple[str, str, str], Tuple[str, Ed25519PrivateKey, int]] = {}


class SigningUnavailable(RuntimeError):
    """AAuth signing is off, unconfigured, or the agent name is not a valid local part."""


def _issuer_or_raise() -> str:
    issuer = config.active_issuer()
    if not issuer:
        raise SigningUnavailable("AAuth is not enabled on this instance (flag off or no issuer)")
    return issuer


def agent_token(agent_name: str, *, now: Optional[int] = None) -> Tuple[str, Ed25519PrivateKey, str]:
    """Return ``(token, agent_private_key, identity)`` for ``agent_name``."""
    if not _LOCAL_RE.match(agent_name or ""):
        raise SigningUnavailable("agent name is not a valid AAuth local part")
    issuer = _issuer_or_raise()
    try:
        instance = keys.get_instance_key()
    except keys.SigningKeyUnavailable as exc:
        raise SigningUnavailable(str(exc)) from None
    now = int(time.time()) if now is None else now
    cache_key = (issuer, instance.kid, agent_name)
    cached = _tokens.get(cache_key)
    identity = config.agent_identity(agent_name, issuer)
    if cached and cached[2] - now > REFRESH_MARGIN:
        return cached[0], cached[1], identity

    agent_key = Ed25519PrivateKey.generate()
    exp = now + TOKEN_LIFETIME
    token = jose.sign_jws(
        {"alg": jose.ALG, "typ": TOKEN_TYPE, "kid": instance.kid},
        {
            "iss": issuer,
            "dwk": METADATA_DOCUMENT,
            "sub": identity,
            "jti": uuid.uuid4().hex,
            "iat": now,
            "exp": exp,
            "cnf": {"jwk": jose.public_jwk(agent_key.public_key())},
        },
        instance.private_key,
    )
    _tokens[cache_key] = (token, agent_key, exp)
    return token, agent_key, identity


def sign_request(
    *,
    agent_name: str,
    method: str,
    url: str,
    body: bytes = b"",
    content_type: Optional[str] = None,
    now: Optional[int] = None,
) -> Dict[str, str]:
    """Headers that sign one request as ``agent_name``.

    ``url`` is the LOGICAL target (the registered hostname, not a pinned IP):
    ``@authority`` is its host with a default port dropped, ``@path`` its
    still-encoded path. ``body`` must be the exact bytes that go on the wire.
    """
    token, agent_key, _ = agent_token(agent_name, now=now)
    parts = urlsplit(url)
    scheme = (parts.scheme or "https").lower()
    signed = httpsig.sign(
        method=method,
        authority_value=httpsig.authority(parts.hostname or "", parts.port, scheme),
        path=parts.path or "/",
        agent_token=token,
        body=body,
        content_type=content_type,
        created=int(time.time()) if now is None else now,
        private_key=agent_key,
    )
    return signed.as_dict()


def identity_for(agent_name: str) -> str:
    return config.agent_identity(agent_name, _issuer_or_raise())


def reset_cache() -> None:
    """Tests only."""
    _tokens.clear()
