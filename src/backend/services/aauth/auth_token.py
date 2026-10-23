"""Auth tokens: what the person server mints for one resource (ent#879).

Types shared by the ladder (``resource_client``) and the PS exchange
(``ps_client``, checkpoint B), plus :func:`inspect_auth_token` — the
held-token checks on a PS ``200`` (draft §Auth Token Response Verification,
taste T5: inspection only, no PS signature check; the resource verifies it):

* ``typ`` ``aa-auth+jwt``;
* ``iss`` == the resource token's ``aud`` (``expected_iss``) — our PS in
  three-party, the access server in four-party (ent#928) — and ``dwk`` must
  match that mode (``aauth-person.json`` | ``aauth-access.json``): a token
  from the other kind of server is refused, never accepted as "close enough";
* ``ps`` == our PS in both modes; ``aud`` == the resource;
* ``cnf.jwk`` thumbprint == this agent's current key (proof of possession);
* ``sub`` == the sub we presented; ``exp`` in the future, integers only;
  lifetime ``exp − iat`` ≤ 1 h (draft §Auth Token Structure);
* the held expiry is capped by the presented token's expiry and ``expires_in``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Union

from services.aauth import jose

TOKEN_TYPE = "aa-auth+jwt"
PERSON_DOCUMENT = "aauth-person.json"
ACCESS_DOCUMENT = "aauth-access.json"
METADATA_DOCUMENTS = frozenset({PERSON_DOCUMENT, ACCESS_DOCUMENT})
CLOCK_SKEW = 60
MAX_LIFETIME = 3600                  # draft: an auth token MUST NOT live longer than 1 h


@dataclass(frozen=True)
class HeldAuthToken:
    """Service-internal; routes and tools never serialise ``token``."""

    token: str = field(repr=False)
    jti: str = ""
    iss: str = ""
    aud: str = ""
    sub: str = ""
    scope: Optional[str] = None
    exp: int = 0
    key_thumbprint: str = ""


@dataclass(frozen=True)
class AuthTokenIssued:
    held: HeldAuthToken
    cached: bool = False


@dataclass(frozen=True)
class ExchangePending:
    """A PS 202: ``requirement`` ∈ interaction | approval; url/code only for interaction."""

    requirement: str
    pending_url: str
    retry_after: int
    url: Optional[str] = None
    code: Optional[str] = None
    status: str = "pending"


@dataclass(frozen=True)
class ExchangeRefused:
    code: str
    detail: str
    ps_status: Optional[int] = None


ExchangeOutcome = Union[AuthTokenIssued, ExchangePending, ExchangeRefused]


def inspect_auth_token(raw: Any, *, resource: str, ps: str, agent_jkt: str, presented_sub: str,
                       presented_exp: int, now: int, expires_in: Any = None,
                       expected_iss: Optional[str] = None) -> HeldAuthToken:
    """Raise ``ValueError`` naming the failed check; return the token to hold.

    ``expected_iss`` is the verified resource token's ``aud``: None or our PS
    means three-party, anything else is the access server (ent#928).
    """
    if not isinstance(raw, str):
        raise ValueError("auth_token is not a string")
    expected_iss = expected_iss or ps
    four_party = expected_iss != ps
    expected_dwk = ACCESS_DOCUMENT if four_party else PERSON_DOCUMENT
    header, claims, _, _ = jose.decode_unverified(raw)
    if header.get("typ") != TOKEN_TYPE:
        raise ValueError(f"auth token typ must be {TOKEN_TYPE}")
    if claims.get("dwk") != expected_dwk:
        raise ValueError(f"auth token dwk must be {expected_dwk} "
                         f"({'four' if four_party else 'three'}-party)")
    if claims.get("iss") != expected_iss:
        raise ValueError("auth token iss is not the access server named by the resource token" if four_party
                         else "auth token iss is not our person server")
    if claims.get("ps") != ps:
        raise ValueError("auth token ps is not our person server")
    if claims.get("aud") != resource:
        raise ValueError("auth token aud is not the resource")
    sub, jti = claims.get("sub"), claims.get("jti")
    if not isinstance(sub, str) or not sub or not isinstance(jti, str) or not jti:
        raise ValueError("auth token has no sub/jti")
    if sub != presented_sub:
        raise ValueError("auth token sub is not the sub we presented")
    iat, exp = claims.get("iat"), claims.get("exp")
    if any(isinstance(v, bool) or not isinstance(v, int) for v in (iat, exp)):
        raise ValueError("auth token iat/exp are not integers")
    if exp <= now:
        raise ValueError("auth token has already expired")
    if iat - now > CLOCK_SKEW or exp - iat > MAX_LIFETIME:
        raise ValueError("auth token lifetime is not acceptable")
    cnf = claims.get("cnf")
    cnf_jwk = cnf.get("jwk") if isinstance(cnf, dict) else None
    try:
        jose.jwk_to_public_key(cnf_jwk)
        thumbprint = jose.thumbprint(cnf_jwk)
    except (jose.JoseError, KeyError, TypeError):
        raise ValueError("auth token cnf.jwk is not a usable key") from None
    if thumbprint != agent_jkt:
        raise ValueError("auth token is bound to a key that is not ours")
    scope = claims.get("scope")
    if scope is not None and not isinstance(scope, str):
        raise ValueError("auth token scope is not a string")
    held_exp = min(exp, presented_exp) if presented_exp else exp
    if isinstance(expires_in, int) and not isinstance(expires_in, bool) and expires_in > 0:
        held_exp = min(held_exp, now + expires_in)
    return HeldAuthToken(token=raw, jti=jti, iss=expected_iss, aud=resource, sub=sub, scope=scope,
                         exp=held_exp, key_thumbprint=thumbprint)
