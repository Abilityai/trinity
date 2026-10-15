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

Three-party and four-party (ent#928, draft §Federated Authorization Access).
``aud`` is our PS → three-party, as before. Any other ``aud`` is accepted
ONLY as a discoverable access server: a strict server identifier (the
``config.normalize_issuer`` rule — https, host only) whose
``{aud}/.well-known/aauth-access.json`` is fetchable through the pinned
public-URL egress and names itself as ``issuer``. That fetch happens AFTER
the signature verifies, so only a token the allow-listed resource actually
signed can make this backend dial the host it names. Every other ``aud`` is
still the named refusal ``aud`` (the ``unsupported_aud`` of plan decision
#18 survives only for what is not an access server). This is not the silent
widening #18 refused: the agent's part is unchanged — the token still goes
to OUR person server, which verifies it and federates with the AS; what
stopped is our refusing to forward it. ``ResourceToken.as_url`` carries the
mode (None = three-party) so the auth token that comes back is held to the
matching ``iss`` / ``dwk`` rule (``auth_token.inspect_auth_token``).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Optional

from services.aauth import config, discovery, jose

logger = logging.getLogger(__name__)

TOKEN_TYPE = "aa-resource+jwt"
METADATA_DOCUMENT = discovery.RESOURCE_DOCUMENT
ACCESS_DOCUMENT = discovery.ACCESS_DOCUMENT
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
    #: ent#928: the access server ``aud`` named (four-party), None when ``aud`` is our PS (three-party).
    as_url: Optional[str] = None

    @property
    def auth_mode(self) -> str:
        return "four-party" if self.as_url else "three-party"


KeyResolver = Callable[..., Awaitable[Dict[str, Any]]]
AccessServerResolver = Callable[..., Awaitable[Dict[str, Any]]]


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
    access_server_resolver: Optional[AccessServerResolver] = None,
) -> ResourceToken:
    """Verify ``token`` against what this agent presented. Raises :class:`ResourceTokenError`."""
    resolve = key_resolver or discovery.resolve_key
    resolve_access_server = access_server_resolver or discovery.access_server_metadata
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
    if not isinstance(aud, str) or not aud:
        raise ResourceTokenError("aud", "resource token has no aud")
    as_url: Optional[str] = None
    if aud != ps:
        # Four-party candidate: a server identifier that is not our PS. Whether
        # it IS an access server is a fetch, which waits for the signature.
        if config.normalize_issuer(aud) is None:
            raise ResourceTokenError(
                "aud", "resource token aud is neither our person server nor an https server identifier (unsupported_aud)")
        as_url = aud
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

    if as_url is not None:
        # Signed by the resource we called: now, and only now, is the aud worth
        # a fetch. The AS is trusted exactly as far as its published metadata —
        # it must exist on the public internet and name itself as issuer.
        try:
            await resolve_access_server(as_url)
        except discovery.DiscoveryError as exc:
            raise ResourceTokenError(
                "aud", f"resource token aud is not a discoverable access server ({exc.code}: {exc.detail})") from None
        logger.info("[aauth] resource token from %s names access server %s (four-party)", resource, as_url)

    return ResourceToken(token=token, jti=jti, iss=iss, aud=aud, sub=sub, agent_jkt=agent_jkt, ps=ps,
                         presented_jti=presented_jti, scope=scope, iat=iat, exp=exp, interaction=interaction,
                         as_url=as_url)
