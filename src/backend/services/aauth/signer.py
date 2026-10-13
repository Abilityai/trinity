"""Caller side: agent tokens and signed requests (ent#623, ent#878).

Each agent's Ed25519 key is **derived, not generated** (ent#878, taste T6):
``HKDF-SHA256(instance seed, info="aauth-agent-key|<issuer>|<agent>|<window>")``
with ``window = now // TOKEN_LIFETIME`` (an hour number). Every worker of the
instance derives the same key inside a window, so a person token obtained on
one worker — whose ``cnf.jwk`` names this key — is presentable on the other,
and a challenge chain (person token → resource token → auth token → call) that
asks for ``min_remaining`` seconds of key life finishes on one key: the window
is chosen so it has at least that long left, and the cached token is reused
for as long as it covers the margin the caller asks for.

Agent keys are never published ("Agent keys are not published" — bootstrap
draft); the derivation input is the instance seed, which already signs every
agent token, so this adds no exposure. The key, bound into the agent token's
``cnf.jwk`` and signed by the instance key, lives in worker memory next to the
token, which is refreshed when fewer than five minutes remain, when its key
window has ended, or when a caller needs more remaining life than it has.

The cache is keyed by ``(issuer, kid, agent)``: a quick-tunnel restart changes
the issuer, and an instance key change changes the kid; either must retire the
old token immediately rather than at its expiry.
"""
from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from services.aauth import config, httpsig, jose, keys

TOKEN_TYPE = "aa-agent+jwt"
METADATA_DOCUMENT = "aauth-agent.json"
TOKEN_LIFETIME = 3600
REFRESH_MARGIN = 300

_KEY_INFO_PREFIX = b"aauth-agent-key|"

#: The draft's `local` grammar minus `+`, which is reserved as the sub-agent
#: delimiter and must never appear in a top-level agent's identity.
_LOCAL_RE = re.compile(r"^[A-Za-z0-9._-]{1,255}$")


@dataclass(frozen=True)
class AgentToken:
    """One minted agent token with the key it binds (service-internal)."""

    token: str = field(repr=False)
    key: Ed25519PrivateKey = field(repr=False)
    identity: str = ""
    exp: int = 0
    window: int = 0
    thumbprint: str = ""


_tokens: Dict[Tuple[str, str, str], AgentToken] = {}


class SigningUnavailable(RuntimeError):
    """AAuth signing is off, unconfigured, or the agent name is not a valid local part."""


def _issuer_or_raise() -> str:
    issuer = config.active_issuer()
    if not issuer:
        raise SigningUnavailable("AAuth is not enabled on this instance (flag off or no issuer)")
    return issuer


def key_window(now: int, min_remaining: int = 0) -> int:
    """The hour window whose key still has ``min_remaining`` seconds of life at ``now``."""
    return (now + max(0, int(min_remaining))) // TOKEN_LIFETIME


def window_end(window: int) -> int:
    return (window + 1) * TOKEN_LIFETIME


def derive_agent_key(seed: bytes, issuer: str, agent_name: str, window: int) -> Ed25519PrivateKey:
    """HKDF-SHA256 over the instance seed, bound to issuer, agent and window."""
    info = _KEY_INFO_PREFIX + f"{issuer}|{agent_name}|{window}".encode("utf-8")
    raw = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(seed)
    return Ed25519PrivateKey.from_private_bytes(raw)


def agent_token_info(agent_name: str, *, now: Optional[int] = None, min_remaining: int = 0) -> AgentToken:
    """The agent token + key for ``agent_name``, minted or reused.

    ``min_remaining``: the token (and so its key) must stay valid for at least
    this many seconds — a caller about to start a multi-step challenge chain
    asks for the whole chain's length so every step signs with one key.
    """
    if not _LOCAL_RE.match(agent_name or ""):
        raise SigningUnavailable("agent name is not a valid AAuth local part")
    issuer = _issuer_or_raise()
    try:
        instance = keys.get_instance_key()
        seed = keys.instance_seed()
    except keys.SigningKeyUnavailable as exc:
        raise SigningUnavailable(str(exc)) from None
    now = int(time.time()) if now is None else now
    margin = max(REFRESH_MARGIN, int(min_remaining))
    cache_key = (issuer, instance.kid, agent_name)
    cached = _tokens.get(cache_key)
    if cached and cached.exp - now > margin and now < window_end(cached.window):
        return cached

    identity = config.agent_identity(agent_name, issuer)
    window = key_window(now, min_remaining)
    agent_key = derive_agent_key(seed, issuer, agent_name, window)
    public = jose.public_jwk(agent_key.public_key())
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
            "cnf": {"jwk": public},
        },
        instance.private_key,
    )
    minted = AgentToken(token=token, key=agent_key, identity=identity, exp=exp,
                        window=window, thumbprint=jose.thumbprint(public))
    _tokens[cache_key] = minted
    return minted


def agent_token(agent_name: str, *, now: Optional[int] = None,
                min_remaining: int = 0) -> Tuple[str, Ed25519PrivateKey, str]:
    """Return ``(token, agent_private_key, identity)`` for ``agent_name``."""
    info = agent_token_info(agent_name, now=now, min_remaining=min_remaining)
    return info.token, info.key, info.identity


def current_agent_key(agent_name: str, *, now: Optional[int] = None,
                      min_remaining: int = 0) -> Tuple[Ed25519PrivateKey, str]:
    """``(private_key, RFC 7638 thumbprint)`` of the key this agent signs with now.

    The thumbprint is what a person token's ``cnf`` is compared against
    (ent#878) and what ent#879 signs its ``presented_token`` requests with.
    """
    info = agent_token_info(agent_name, now=now, min_remaining=min_remaining)
    return info.key, info.thumbprint


def sign_request(
    *,
    agent_name: str,
    method: str,
    url: str,
    body: bytes = b"",
    content_type: Optional[str] = None,
    now: Optional[int] = None,
    min_remaining: int = 0,
) -> Dict[str, str]:
    """Headers that sign one request as ``agent_name``.

    ``url`` is the LOGICAL target (the registered hostname, not a pinned IP):
    ``@authority`` is its host with a default port dropped, ``@path`` its
    still-encoded path. ``body`` must be the exact bytes that go on the wire.
    """
    info = agent_token_info(agent_name, now=now, min_remaining=min_remaining)
    parts = urlsplit(url)
    scheme = (parts.scheme or "https").lower()
    signed = httpsig.sign(
        method=method,
        authority_value=httpsig.authority(parts.hostname or "", parts.port, scheme),
        path=parts.path or "/",
        agent_token=info.token,
        body=body,
        content_type=content_type,
        created=int(time.time()) if now is None else now,
        private_key=info.key,
    )
    return signed.as_dict()


def identity_for(agent_name: str) -> str:
    return config.agent_identity(agent_name, _issuer_or_raise())


def reset_cache() -> None:
    """Tests only."""
    _tokens.clear()
