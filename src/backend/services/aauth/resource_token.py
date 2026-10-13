"""Resource-token verification — the security sink of the outbound resource
client (ent#879; draft §Resource Token Structure / §Resource Challenge
Verification).

A resource answers ``401 AAuth-Requirement: requirement=auth-token;
resource-token="…"`` with a token that tells OUR person server what to mint:
for which ``sub``, which scope, bound to which agent key. If anything that can
answer our 401 could hand us an arbitrary resource token, it could make the PS
mint an auth token for a person we never presented. So every claim is checked
against what WE sent — before the PS is contacted — and a failure is a named
refusal plus a WARN ("possible tampering"), never a retry.

Order: cheap and local first, the network (key discovery) last, the signature
after the claims so a forged token costs no fetch for a claim it already fails.
Each check has a ``check`` name; ``tests/unit/test_ent879_aauth_resource_client.py``
turns every one red with one mutation.

Three-party only: ``aud`` must be our PS. A four-party resource (an access
server named in ``aud``) is refused as ``unsupported_aud`` — accepting it
without AS verification would be a silent widening (plan decision #18).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Optional

from services.aauth import discovery, jose

logger = logging.getLogger(__name__)

TOKEN_TYPE = "aa-resource+jwt"
METADATA_DOCUMENT = discovery.RESOURCE_DOCUMENT
CLOCK_SKEW = 60
#: Local policy (not the draft's MUST): a resource token older than an hour is
#: refused; one longer than five minutes is accepted with a WARN (SHOULD).
MAX_LIFETIME = 3600
WARN_LIFETIME = 300
MAX_SCOPE_CHARS = 256


class ResourceTokenError(Exception):
    """``check`` names the failed step (closed vocabulary — see ``CHECKS``)."""

    def __init__(self, check: str, detail: str):
        super().__init__(detail)
        self.check = check
        self.detail = detail


CHECKS = (
    "malformed", "alg", "typ", "header_params", "kid", "iss", "dwk", "aud", "agent_jkt", "ps",
    "sub", "presented_jti", "timestamps", "expired", "clock_skew", "lifetime", "scope",
    "unknown_kid", "signature",
)


@dataclass(frozen=True)
class ResourceToken:
    """A verified resource token (service-internal; ``token`` is re-presented to the PS)."""

    token: str = field(repr=False)
    jti: str
    iss: str
    aud: str
    sub: str
    agent_jkt: str
    ps: str
    presented_jti: str
    scope: Optional[str]
    iat: int
    exp: int
    interaction: Optional[Dict[str, Any]] = None


KeyResolver = Callable[..., Awaitable[Dict[str, Any]]]


def _str_claim(claims: Dict[str, Any], name: str, check: str) -> str:
    value = claims.get(name)
    if not isinstance(value, str) or not value:
        raise ResourceTokenError(check, f"resource token has no {name}")
    return value


async def verify_resource_token(
    token: Any,
    *,
    resource: str,
    agent_jkt: str,
    ps: str,
    presented_sub: str,
    presented_jti: str,
    now: int,
    key_resolver: Optional[KeyResolver] = None,
) -> ResourceToken:
    """Verify ``token`` against what this agent presented. Raises :class:`ResourceTokenError`."""
    resolve = key_resolver or discovery.resolve_key
    try:
        header, claims, signing_input, signature = jose.decode_unverified(token)
    except jose.JoseError as exc:
        raise ResourceTokenError("malformed", f"resource token is not a compact JWS: {exc}") from None

    if header.get("alg") != jose.ALG:
        raise ResourceTokenError("alg", "resource token alg must be Ed25519")
    if header.get("typ") != TOKEN_TYPE:
        raise ResourceTokenError("typ", f"resource token typ must be {TOKEN_TYPE}")
    forbidden = jose.FORBIDDEN_HEADER_PARAMS.intersection(header)
    if forbidden:
        raise ResourceTokenError("header_params", f"resource token header carries {sorted(forbidden)}")
    kid = header.get("kid")
    if not isinstance(kid, str) or not kid or len(kid) > 256:
        raise ResourceTokenError("kid", "resource token has no usable kid")

    iss = _str_claim(claims, "iss", "iss")
    if iss != resource:
        raise ResourceTokenError("iss", "resource token iss is not the resource we called")
    if claims.get("dwk") != METADATA_DOCUMENT:
        raise ResourceTokenError("dwk", f"resource token dwk must be {METADATA_DOCUMENT}")
    aud = claims.get("aud")
    if isinstance(aud, str) and aud and aud != ps:
        raise ResourceTokenError("aud", "resource token aud is not our person server (unsupported_aud)")
    if not isinstance(aud, str) or not aud:
        raise ResourceTokenError("aud", "resource token has no aud")
    if claims.get("agent_jkt") != agent_jkt:
        raise ResourceTokenError("agent_jkt", "resource token agent_jkt is not this agent's current key")
    if claims.get("ps") != ps:
        raise ResourceTokenError("ps", "resource token ps is not our person server")
    sub = _str_claim(claims, "sub", "sub")
    if sub != presented_sub:
        raise ResourceTokenError("sub", "resource token sub is not the sub we presented")
    if claims.get("presented_jti") != presented_jti:
        raise ResourceTokenError("presented_jti", "resource token presented_jti is not the token we presented")
    jti = _str_claim(claims, "jti", "timestamps") if "jti" in claims else ""

    iat, exp = claims.get("iat"), claims.get("exp")
    if any(isinstance(v, bool) or not isinstance(v, int) for v in (iat, exp)):
        raise ResourceTokenError("timestamps", "resource token iat/exp are not integers")
    if exp <= now:
        raise ResourceTokenError("expired", "resource token has expired")
    if iat - now > CLOCK_SKEW:
        raise ResourceTokenError("clock_skew", "resource token iat is in the future")
    if exp - iat > MAX_LIFETIME:
        raise ResourceTokenError("lifetime", "resource token lifetime exceeds one hour (local policy)")
    if exp - iat > WARN_LIFETIME:
        logger.warning("[aauth] resource token from %s lives %ss (> %ss SHOULD)", resource, exp - iat, WARN_LIFETIME)

    scope = claims.get("scope")
    if scope is not None and (not isinstance(scope, str) or len(scope) > MAX_SCOPE_CHARS):
        raise ResourceTokenError("scope", "resource token scope is not a bounded string")
    interaction = claims.get("interaction")
    if interaction is not None and not isinstance(interaction, dict):
        interaction = None

    try:
        jwk = await resolve(iss, kid, dwk=METADATA_DOCUMENT)
    except discovery.DiscoveryError as exc:
        raise ResourceTokenError("unknown_kid", f"resource key could not be resolved: {exc.detail}") from None
    try:
        jose.verify_ed25519(signing_input, signature, jose.jwk_to_public_key(jwk))
    except jose.JoseError as exc:
        raise ResourceTokenError("signature", f"resource token signature does not verify: {exc}") from None

    return ResourceToken(token=token, jti=jti, iss=iss, aud=aud, sub=sub, agent_jkt=agent_jkt, ps=ps,
                         presented_jti=presented_jti, scope=scope, iat=iat, exp=exp, interaction=interaction)
