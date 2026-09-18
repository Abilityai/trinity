"""Resource side: verify an AAuth-signed request (ent#623).

Pure with respect to the platform: no audit, no activity, no HTTP types. The
router supplies the request facts, the trusted identities for the target agent
and a body reader, and turns :class:`AAuthError` into a response.

Order is cheap-and-local first, network last, body last:

1. header shape (``Signature-Key`` jwt scheme, ``Signature-Input``, ``Signature``)
2. covered components + ``created``/``expires`` window + declared body size
3. agent token decoded WITHOUT trust: alg / typ / dwk / iss / sub / exp / cnf
4. trusted-issuer pre-gate — the issuer host must appear in an ``aauth:``
   allow-list entry for this agent, or nothing is fetched
5. issuer key (bounded discovery) → token signature
6. HTTP signature with the token's ``cnf.jwk`` — this covers the
   ``Content-Digest`` HEADER, so the body is not read before it is proven
7. body read (capped by the caller) → digest must match the signed header
8. replay claim (fail-closed)

Errors raised before step 4 carry ``trusted=False`` so the router can keep
unauthenticated noise out of the audit table.
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Mapping, Optional, Protocol

from services.aauth import config, discovery, httpsig, jose
from services.aauth.signer import METADATA_DOCUMENT, TOKEN_TYPE

logger = logging.getLogger(__name__)

SIGNATURE_WINDOW = 60           # seconds, the draft's default
MAX_TOKEN_LIFETIME = 24 * 3600  # "SHOULD NOT … exceed 24 hours"
REPLAY_TTL = 2 * SIGNATURE_WINDOW + 10

_SUB_RE = re.compile(r"aauth:([A-Za-z0-9._+-]{1,255})@([a-z0-9.-]{1,253})")


class AAuthError(Exception):
    def __init__(self, code: str, detail: str, *, status: int = 401, trusted: bool = False,
                 identity: Optional[str] = None, issuer: Optional[str] = None):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status = status
        self.trusted = trusted
        self.identity = identity
        self.issuer = issuer


@dataclass(frozen=True)
class VerifiedAgent:
    identity: str
    issuer: str
    key_thumbprint: str
    token_id: str


@dataclass(frozen=True)
class InboundRequest:
    method: str
    raw_path: str                    # percent-encoded, as signed
    headers: Mapping[str, str]       # lower-case names; repeated fields joined with ", "
    content_length: Optional[int]


class ReplayStore(Protocol):
    def claim(self, key: str, ttl: int) -> bool:
        """True the first time ``key`` is seen within ``ttl``. Raise if unknowable."""


class RedisReplayStore:
    def claim(self, key: str, ttl: int) -> bool:
        from services.rate_limiter import _get_redis

        client = _get_redis()
        if client is None:
            raise RuntimeError("replay store unavailable")
        return bool(client.set(key, "1", nx=True, ex=ttl))


def normalize_identity(value: str) -> Optional[str]:
    """``aauth:<local>@<host>`` with the host lower-cased; None if not an AAuth id."""
    if not isinstance(value, str) or not value.lower().startswith("aauth:") or "@" not in value:
        return None
    local, _, host = value.rpartition("@")
    # The scheme is case-insensitive (an operator may type `AAuth:`); the local
    # part is not (the draft compares it exactly), the host is lower-cased.
    return f"aauth:{local[len('aauth:'):]}@{host.lower()}"


def trusted_hosts(identities: Iterable[str]) -> set:
    out = set()
    for ident in identities:
        norm = normalize_identity(ident)
        if norm:
            out.add(norm.rpartition("@")[2])
    return out


def _claim_str(claims: Dict[str, Any], name: str) -> str:
    value = claims.get(name)
    if not isinstance(value, str) or not value:
        raise AAuthError("invalid_jwt", f"agent token has no {name}")
    return value


def _claim_int(claims: Dict[str, Any], name: str) -> int:
    value = claims.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise AAuthError("invalid_jwt", f"agent token {name} is not an integer")
    return value


async def verify_request(
    req: InboundRequest,
    *,
    expected_authority: str,
    trusted_identities: List[str],
    read_body: Callable[[], Awaitable[bytes]],
    max_body_bytes: int,
    now: Optional[int] = None,
    replay_store: Optional[ReplayStore] = None,
    key_resolver: Optional[Callable[..., Awaitable[Dict[str, Any]]]] = None,
) -> "tuple[VerifiedAgent, bytes]":
    now = int(time.time()) if now is None else now
    key_resolver = key_resolver or discovery.resolve_key
    method = req.method.upper()
    h = req.headers

    # 1-2. Header shape, coverage, window, size -------------------------------
    try:
        token = httpsig.parse_signature_key(h.get("signature-key"))
        sig_input = httpsig.parse_signature_input(h.get("signature-input"))
        signature = httpsig.parse_signature(h.get("signature"))
    except httpsig.SignatureFormatError as exc:
        raise AAuthError(exc.code, exc.detail) from None
    required = httpsig.components_for(method)
    missing = [c for c in required if c not in sig_input.components]
    if missing:
        raise AAuthError("invalid_input", f"signature must cover {', '.join(missing)}")
    if now - sig_input.created > SIGNATURE_WINDOW:
        raise AAuthError("invalid_signature", "signature created too long ago")
    if sig_input.created - now > SIGNATURE_WINDOW:
        raise AAuthError("clock_skew", "signature created in the future")
    if sig_input.expires is not None and sig_input.expires < now:
        raise AAuthError("invalid_signature", "signature has expired")
    if len(signature) != 64:
        raise AAuthError("invalid_signature", "signature is not an Ed25519 signature")
    if method == "POST":
        if req.content_length is None:
            raise AAuthError("invalid_input", "a signed POST must declare Content-Length")
        if req.content_length > max_body_bytes:
            raise AAuthError("invalid_input", "request body too large", status=413)
        if httpsig.parse_content_digest_sha256(h.get("content-digest")) is None:
            raise AAuthError("invalid_input", "a signed POST must carry a sha-256 Content-Digest")

    # 3. Token, untrusted -------------------------------------------------------
    try:
        header, claims, signing_input, token_sig = jose.decode_unverified(token)
    except jose.JoseError as exc:
        raise AAuthError("invalid_jwt", str(exc)) from None
    alg = header.get("alg")
    if alg != jose.ALG:
        raise AAuthError("unsupported_algorithm", "agent token alg must be Ed25519")
    if header.get("typ") != TOKEN_TYPE:
        raise AAuthError("invalid_jwt", f"agent token typ must be {TOKEN_TYPE}")
    if jose.FORBIDDEN_HEADER_PARAMS & set(header):
        raise AAuthError("invalid_jwt", "agent token header carries a forbidden parameter")
    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        raise AAuthError("invalid_jwt", "agent token has no kid")
    iss = _claim_str(claims, "iss")
    if config.normalize_issuer(iss) is None:
        raise AAuthError("invalid_jwt", "agent token iss is not a valid server identifier")
    if claims.get("dwk") != METADATA_DOCUMENT:
        raise AAuthError("invalid_jwt", f"agent token dwk must be {METADATA_DOCUMENT}")
    sub = _claim_str(claims, "sub")
    m = _SUB_RE.fullmatch(sub)
    if not m or m.group(2) != config.issuer_host(iss):
        raise AAuthError("invalid_jwt", "agent token sub is not an agent identifier of its issuer")
    if "+" in m.group(1):
        raise AAuthError("invalid_jwt", "sub-agent identities are not accepted", identity=sub, issuer=iss)
    jti = _claim_str(claims, "jti")
    iat, exp = _claim_int(claims, "iat"), _claim_int(claims, "exp")
    if exp <= now:
        raise AAuthError("expired_jwt", "agent token has expired")
    if iat - now > SIGNATURE_WINDOW:
        raise AAuthError("clock_skew", "agent token issued in the future")
    if exp - iat > MAX_TOKEN_LIFETIME:
        raise AAuthError("invalid_jwt", "agent token lifetime is not acceptable")
    cnf = claims.get("cnf")
    cnf_jwk = cnf.get("jwk") if isinstance(cnf, dict) else None
    try:
        agent_key = jose.jwk_to_public_key(cnf_jwk)
    except jose.JoseError as exc:
        raise AAuthError("invalid_key", f"agent token cnf.jwk: {exc}") from None

    # 4. Trusted-issuer pre-gate (no fetch for an issuer nobody listed) ---------
    if config.issuer_host(iss) not in trusted_hosts(trusted_identities):
        raise AAuthError("invalid_jwt", "issuer is not trusted by this resource",
                         identity=sub, issuer=iss)

    # 5. Issuer key → token signature ------------------------------------------
    #
    # `trusted` stays FALSE for everything below until the token's own signature
    # verifies. Only then is `sub` a claim the issuer made rather than a string
    # the caller typed — and only then may the router write an audit row naming
    # it. Otherwise anyone who can guess one allow-listed issuer host could fill
    # `audit_log` with rows attributed to an identity they invented.
    try:
        issuer_jwk = await key_resolver(iss, kid)
        jose.verify_ed25519(signing_input, token_sig, jose.jwk_to_public_key(issuer_jwk))
    except discovery.DiscoveryError as exc:
        raise AAuthError(exc.code, exc.detail, identity=sub, issuer=iss) from None
    except jose.JoseError as exc:
        raise AAuthError("invalid_jwt", str(exc), identity=sub, issuer=iss) from None

    def _trusted(code: str, detail: str, status: int = 401) -> AAuthError:
        """The issuer vouched for this `sub`: refusals from here on are auditable."""
        return AAuthError(code, detail, status=status, trusted=True, identity=sub, issuer=iss)

    # 6. HTTP signature over the headers (incl. the Content-Digest header) ------
    values = {
        "@method": method,
        "@authority": expected_authority,
        "@path": req.raw_path or "/",
        "signature-key": (h.get("signature-key") or "").strip(),
    }
    for name in sig_input.components:
        if name in values:
            continue
        if name.startswith("@"):
            raise _trusted("invalid_input", f"unsupported derived component {name}")
        field_value = h.get(name)
        if field_value is None:
            raise _trusted("invalid_input", f"covered field {name} is absent")
        values[name] = field_value.strip()
    base = httpsig.signature_base(values, sig_input.components, sig_input.params)
    try:
        jose.verify_ed25519(base, signature, agent_key)
    except jose.JoseError:
        raise _trusted("invalid_signature", "HTTP message signature does not verify") from None

    # 7. Body, now that its digest header is proven ------------------------------
    try:
        body = await read_body()
    except AAuthError as exc:
        raise _trusted(exc.code, exc.detail, exc.status) from None
    digest = None
    if method == "POST":
        digest = httpsig.parse_content_digest_sha256(h.get("content-digest"))
        if len(body) > max_body_bytes:
            raise _trusted("invalid_input", "request body too large", status=413)
        if httpsig.content_digest(body) != digest:
            raise _trusted("invalid_signature", "Content-Digest does not match the body")

    # 8. Replay -----------------------------------------------------------------
    jkt = jose.thumbprint(cnf_jwk)
    replay_key = "aauth:replay:" + hashlib.sha256(
        "\n".join([jkt, str(sig_input.created), method, expected_authority,
                   values["@path"], digest or ""]).encode("utf-8")
    ).hexdigest()
    try:
        first = (replay_store or RedisReplayStore()).claim(replay_key, REPLAY_TTL)
    except Exception:  # noqa: BLE001 — fail closed: this is the security boundary
        logger.error("[aauth] replay store unavailable; refusing signed request")
        raise _trusted("replay_check_unavailable", "replay protection is unavailable", status=503) from None
    if not first:
        raise _trusted("invalid_signature", "request replayed")

    return VerifiedAgent(identity=sub, issuer=iss, key_thumbprint=jkt, token_id=jti), body
