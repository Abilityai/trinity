"""The outbound AAuth resource client — the challenge ladder (ent#879).

An agent calls a resource (``{resource}{path}``) as itself and climbs the
draft's requirement ladder (§Resource Access) until the resource answers or a
person has to act:

    token to present ← best held: auth > person > agent (honouring access_mode)
    send signed (Signature-Key carries the token; the AGENT KEY always signs)
      2xx                               → result
      202 (+Location)                   → pending(resource) — never polled inline (S5)
      401 requirement=agent-token       → once more with the agent token, else refusal
      401 requirement=person-token      → person token from the PS (ent#878) → retry once
      401 requirement=auth-token        → VERIFY the resource token (the sink) → exchange at
                                          the PS → auth token → retry once (step-up: once)
                                          (four-party, ent#928: the token's aud names an
                                          access server; still OUR PS's endpoint, the PS
                                          federates — the agent never dials the AS)
      401 Signature-Error expired/revoked on an auth token → evict, person token, ONE re-auth
      PS: expired/invalid presented     → forget the person token, ONE fresh chain
      402 / 403 / 408 / 410 / 429       → payment_required / denied / expired / gone / rate_limited
      anything unknown                  → a named refusal, never a blind retry

Bounds (derived from the longest legal chain, H3): ≤ 6 resource sends, ≤ 3 PS
exchanges, one 25 s budget (the MCP gateway's patience, S5), per-leg timeout
``min(10 s, remaining)``. A timeout after a POST was sent is reported with
``possibly_executed`` (#914) so the caller does not double-spend credits.

Every token this call holds — agent, person, auth, resource — is scrubbed
from any body or error text before it reaches the LLM (H2), on top of the
generic compact-JWS shape.

``resume(agent, handle)`` continues a pending call: one signed GET of the
pending URL (a PS exchange, or a resource-side deferral) and, when the PS has
minted the auth token, the STORED request is re-sent — what runs is what the
person consented to, never caller-supplied arguments (decision #11).

Before any of this, the allow-list (``resource_allowlist``) decides whether
the resource may be called at all — origin equality, fail closed — the only
thing between an LLM-supplied URL and a signed server-side request.

FastAPI-free (Invariant #1). The auth-token cache and the pending records live
in Redis behind ``ps_client`` (T1); a cache that cannot be read is "not held",
a record that cannot be written is a ``state_unavailable`` refusal.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union
from urllib.parse import urljoin, urlsplit

import httpx

from services.aauth import (auth_token, config, discovery, egress, httpsig, person_token as pt, ps_client,
                            requirement, resource_allowlist, resource_meta, resource_token, signer)

logger = logging.getLogger(__name__)

ROUTE_BUDGET = 25.0
LEG_TIMEOUT = 10.0
PS_WAIT = 10
RESUME_WAIT = 15
SLOW_DOWN_STEP = 5
MAX_RESOURCE_SENDS = 6
MAX_PS_EXCHANGES = 3
RESOURCE_BODY_MAX_BYTES = 1024 * 1024
MAX_PATH_CHARS = 512
MAX_DETAIL_CHARS = 500
MAX_JUSTIFICATION_CHARS = 500
PENDING_TTL = pt.PENDING_TTL
AGENT_TOKEN_MIN_REMAINING = pt.AGENT_TOKEN_MIN_REMAINING
CAPABILITIES_HEADER = "interaction"
METHODS = ("GET", "POST")
TOKEN_KINDS = ("agent", "person", "auth")
PENDING_KINDS = ("ps_interaction", "ps_approval", "resource_polling")

#: pchar minus ``%``: ``@path`` is signed byte-for-byte as sent, and a
#: percent-encoded ``..``/``/`` is a path the allow-list never judged (M3).
_PATH_RE = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@/-]*$")
_JWS_RE = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]*")
_ERROR_RE = re.compile(r"^[a-z_]{1,64}$")
_DENIAL_CODES = ("denied", "abandoned", "revoked", "user_unreachable")
_REAUTH_SIGNATURE_ERRORS = ("expired_jwt", "revoked_jwt")
_FRESH_CHAIN_CODES = ("expired_presented_token", "invalid_presented_token")


# --------------------------------------------------------------------------- #
# Outcomes (the response union, discriminated by `outcome`)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ResourceResult:
    status: int
    body: str
    truncated: bool
    token_kind: str
    challenges: List[str]
    resource: str
    method: str
    path: str
    budget: Optional[resource_meta.Budget] = None
    content_type: Optional[str] = None
    #: ent#928: the access server the auth token came from (four-party), None otherwise.
    access_server: Optional[str] = None
    outcome: str = "result"


@dataclass(frozen=True)
class InteractionRequired:
    handle: str
    url: Optional[str]
    code: Optional[str]
    link: Optional[str]
    retry_after: int
    at: str                      # person_server
    challenges: List[str]
    expires_at: int
    resource: str
    outcome: str = "interaction_required"


@dataclass(frozen=True)
class ResourcePending:
    handle: str
    retry_after: int
    requirement: str             # approval | deferred | interaction
    at: str                      # person_server | resource
    challenges: List[str]
    expires_at: int
    resource: str
    outcome: str = "pending"


@dataclass(frozen=True)
class Denied:
    reason: str
    detail: str
    challenges: List[str]
    resource: str
    budget: Optional[resource_meta.Budget] = None
    do_not_retry: bool = True
    outcome: str = "denied"


@dataclass(frozen=True)
class Expired:
    reason: str
    challenges: List[str]
    resource: str
    do_not_retry: bool = True
    outcome: str = "expired"


@dataclass(frozen=True)
class Refusal:
    code: str
    detail: str
    challenges: List[str] = field(default_factory=list)
    resource: Optional[str] = None
    status: Optional[int] = None
    retry_after: Optional[int] = None
    possibly_executed: bool = False
    budget: Optional[resource_meta.Budget] = None
    outcome: str = "refusal"


Outcome = Union[ResourceResult, InteractionRequired, ResourcePending, Denied, Expired, Refusal]
TERMINAL_OUTCOMES = (ResourceResult, Denied)


@dataclass(frozen=True)
class Presented:
    """The token in ``Signature-Key`` for one send and the facts a resource token must echo."""

    kind: str
    token: str = field(repr=False)
    sub: str
    jti: str
    exp: int


@dataclass
class _Call:
    agent: str
    resource: str
    method: str
    path: str
    body: Optional[bytes]
    content_type: Optional[str]
    justification: Optional[str]
    now: int
    our_issuer: str
    ps: Optional[str]
    meta: resource_meta.ResourceMetadata
    jkt: str
    agent_info: signer.AgentToken
    factory: Callable
    deadline: float
    challenges: List[str] = field(default_factory=list)
    tokens: Set[str] = field(default_factory=set)
    sends: int = 0
    exchanges: int = 0
    agent_retried: bool = False
    person_used: bool = False
    reauthed: bool = False
    fresh_chain: bool = False
    stepped_up: bool = False
    override_url: Optional[str] = None                       # a poll of a pending URL (resume)
    polling: Optional[Tuple[str, Dict[str, Any]]] = None     # (handle, record) being polled
    access_server: Optional[str] = None                      # ent#928: the auth token's AS (four-party)

    @property
    def url(self) -> str:
        return self.override_url or (self.resource + self.path)

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def refusal(self, code: str, detail: str, **kw: Any) -> Refusal:
        return Refusal(code, detail, challenges=list(self.challenges), resource=self.resource, **kw)


# --------------------------------------------------------------------------- #
# State and the PS exchange (ps_client; each seam fails closed on its own)
# --------------------------------------------------------------------------- #
def _held_auth(call: _Call) -> Optional[auth_token.HeldAuthToken]:
    try:
        held = ps_client.held_auth_token(call.agent, resource=call.resource, key_thumbprint=call.jkt, now=call.now)
    except Exception:  # noqa: BLE001 — a cache that cannot be read is "not held"
        logger.warning("[aauth] auth-token cache unreadable for %s", call.agent, exc_info=True)
        return None
    if held is not None:
        _note_auth_issuer(call, held)
    return held


def _note_auth_issuer(call: _Call, held: auth_token.HeldAuthToken) -> None:
    """Record which server minted the auth token in use (ent#928): our PS, or an access server."""
    call.access_server = held.iss if held.iss and held.iss != call.ps else None


def _store_auth(call: _Call, held: auth_token.HeldAuthToken) -> None:
    try:
        ps_client.store_auth_token(call.agent, held, now=call.now)
    except Exception:  # noqa: BLE001 — a miss next call costs one PS exchange, never safety
        logger.warning("[aauth] auth-token cache unwritable for %s", call.agent, exc_info=True)


def _forget_auth(call: _Call) -> None:
    try:
        ps_client.forget_auth_token(call.agent, resource=call.resource, key_thumbprint=call.jkt)
    except Exception:  # noqa: BLE001
        logger.warning("[aauth] auth-token cache eviction failed for %s", call.agent, exc_info=True)


def _save_pending(call: _Call, kind: str, extra: Dict[str, Any]) -> Union[str, Refusal]:
    """Persist a pending record; the handle, or a refusal when it cannot be recorded (fail closed)."""
    record = {
        "kind": kind, "agent": call.agent, "our_issuer": call.our_issuer, "ps_issuer": call.ps,
        "resource": call.resource, "method": call.method, "path": call.path,
        "body": call.body.decode("utf-8") if call.body else None, "content_type": call.content_type,
        "key_thumbprint": call.jkt, "challenges": list(call.challenges),
        "created_at": call.now, "expires_at": call.now + PENDING_TTL, **extra,
    }
    try:
        return ps_client.save_pending(call.agent, record, now=call.now)
    except ps_client.PendingBudgetExceeded as exc:
        return call.refusal("too_many_pending", str(exc))
    except Exception:  # noqa: BLE001
        logger.error("[aauth] could not persist pending record for %s", call.agent, exc_info=True)
        what = "the deferred answer" if kind == "resource_polling" else "the pending consent"
        return call.refusal("state_unavailable", f"{what} could not be recorded (Redis)")


async def _exchange(call: _Call, verified: resource_token.ResourceToken,
                    presented: Presented) -> auth_token.ExchangeOutcome:
    wait = max(0, min(PS_WAIT, int(call.remaining()) - 2))
    return await ps_client.exchange_auth_token(
        call.agent, resource=call.resource, resource_token=verified, presented=presented,
        justification=call.justification, wait=wait, now=call.now, client_factory=call.factory,
        key_thumbprint=call.jkt,
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _is_control_path(path: str, meta: resource_meta.ResourceMetadata) -> bool:
    """Equal to a declared control endpoint once one trailing slash is stripped (case-sensitive)."""
    def strip(p: str) -> str:
        return p[:-1] if len(p) > 1 and p.endswith("/") else p
    return strip(path) in {strip(cp) for cp in meta.control_paths()}


def path_ok(path: Any) -> bool:
    """``/…`` of pchar-without-``%``, no ``..`` segment, no ``//``, bounded, not ``/.well-known/``."""
    if not isinstance(path, str) or not path or len(path) > MAX_PATH_CHARS or not _PATH_RE.match(path):
        return False
    if "//" in path or ".." in path.split("/") or path.startswith("/.well-known/"):
        return False
    return True


def scrub_tokens(text: str, tokens: Set[str]) -> str:
    """Redact every token this call holds, then the generic compact-JWS shape (H2)."""
    for token in sorted(tokens, key=len, reverse=True):
        if token:
            text = text.replace(token, "[redacted-token]")
    return _JWS_RE.sub("[redacted-jws]", text)


def _sanitize(text: str, call: _Call, cap: Optional[int] = None):
    from services import a2a_client

    clean, truncated = a2a_client.sanitize_outbound_text(scrub_tokens(text, call.tokens), None)
    if cap is not None and clean and len(clean) > cap:
        clean, truncated = clean[:cap], True
    return clean or "", truncated


def _problem(body: bytes) -> Dict[str, str]:
    """``{error, detail}`` from a problem+json body (bounded), else empty strings."""
    try:
        doc = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {"error": "", "detail": ""}
    if not isinstance(doc, dict):
        return {"error": "", "detail": ""}
    err = doc.get("error")
    detail = doc.get("detail") if isinstance(doc.get("detail"), str) else doc.get("title")
    return {"error": err if isinstance(err, str) and _ERROR_RE.match(err) else "",
            "detail": detail[:MAX_DETAIL_CHARS] if isinstance(detail, str) else ""}


def _signature_error(headers) -> str:
    value = headers.get("signature-error") or ""
    m = re.search(r"error=\"?([a-z_]{1,64})\"?", value)
    return m.group(1) if m else "invalid_signature"


def _agent_presented(info: signer.AgentToken) -> Presented:
    _, claims, _, _ = jose_decode(info.token)
    return Presented(kind="agent", token=info.token, sub=info.identity, jti=str(claims.get("jti", "")), exp=info.exp)


def jose_decode(token: str):
    from services.aauth import jose

    return jose.decode_unverified(token)


def _budget(resp: egress.CappedResponse) -> Optional[resource_meta.Budget]:
    return resource_meta.parse_budget(resp.headers.get("aauth-budget"))


# --------------------------------------------------------------------------- #
# Public surface
# --------------------------------------------------------------------------- #
async def call_resource(
    agent_name: str,
    *,
    resource: str,
    method: str,
    path: str,
    body: Optional[bytes] = None,
    content_type: Optional[str] = "application/json",
    justification: Optional[str] = None,
    now: Optional[int] = None,
    client_factory: Optional[Callable] = None,
    budget_seconds: float = ROUTE_BUDGET,
) -> Outcome:
    """Climb the ladder once. Raises ``signer.SigningUnavailable`` when AAuth is inert."""
    from services import a2a_client

    deadline = time.monotonic() + max(0.0, float(budget_seconds))    # the budget covers discovery too
    our_issuer = config.active_issuer()
    if not our_issuer:
        raise signer.SigningUnavailable("AAuth is not enabled on this instance (flag off or no issuer)")
    now = int(time.time()) if now is None else now
    norm = config.normalize_issuer(resource)
    if not norm:
        return Refusal("invalid_resource", "resource must be an https server identifier (host only, no port/path/query)")
    resource = norm
    if not resource_allowlist.is_allowed(resource):
        return Refusal("resource_not_allowed",
                       "the resource is not on this instance's allow-list (setting aauth_resource_allowlist)",
                       resource=resource)
    method = (method or "").upper()
    if method not in METHODS:
        return Refusal("invalid_method", "method must be GET or POST", resource=resource)
    if not path_ok(path):
        return Refusal("invalid_path", "path must be an absolute, unencoded resource path (no '..', '//', query or .well-known)",
                       resource=resource)
    if method == "GET":
        body, content_type = None, None
    elif body is None:
        body = b""
    if justification is not None:
        justification = a2a_client.sanitize_text(justification)[:MAX_JUSTIFICATION_CHARS]
        logger.info("[aauth] %s → %s %s%s justification=%r", agent_name, method, resource, path, justification)

    try:
        meta = await resource_meta.metadata(resource)
    except resource_meta.ResourceMetadataError as exc:
        return Refusal(exc.code, exc.detail, resource=resource)
    if _is_control_path(path, meta):
        return Refusal("invalid_path", "path is one of the resource's declared control endpoints", resource=resource)
    extra = tuple(meta.additional_signature_components)
    if set(extra) - set(httpsig.REQUIRED_COMPONENTS) - httpsig.SUPPORTED_EXTRA_COMPONENTS:
        return Refusal("unsupported_signature_components",
                       f"the resource requires signature components this client cannot produce: {sorted(extra)}",
                       resource=resource)

    info = signer.agent_token_info(agent_name, now=now, min_remaining=AGENT_TOKEN_MIN_REMAINING)
    call = _Call(agent=agent_name, resource=resource, method=method, path=path, body=body,
                 content_type=content_type, justification=justification, now=now, our_issuer=our_issuer,
                 ps=config.get_person_server(), meta=meta, jkt=info.thumbprint, agent_info=info,
                 factory=client_factory or a2a_client._http_client,
                 deadline=deadline)
    call.tokens.add(info.token)

    held = _held_auth(call)
    if held is not None:
        call.tokens.add(held.token)
        presented: Union[Presented, Outcome] = Presented("auth", held.token, held.sub, held.jti, held.exp)
    elif meta.starts_with_person_token():
        presented = await _person(call)
    else:
        presented = _agent_presented(info)
    if not isinstance(presented, Presented):
        return presented
    return await _run(call, presented)


# --------------------------------------------------------------------------- #
# The ladder
# --------------------------------------------------------------------------- #
async def _run(call: _Call, presented: Presented) -> Outcome:
    while True:
        if call.sends >= MAX_RESOURCE_SENDS:
            return call.refusal("too_many_rounds", f"the resource challenged more than {MAX_RESOURCE_SENDS} times in one call")
        resp = await _send(call, presented)
        if not isinstance(resp, egress.CappedResponse):
            return resp
        status = resp.status
        if 200 <= status < 300 and status != 202:
            if call.polling is not None:
                ps_client.delete_pending(*call.polling)
            return _result(call, resp, presented)
        if status == 202:
            if call.polling is not None:
                return _still_pending(call, resp)
            return await _deferred(call, resp, presented)
        if status == 401:
            nxt = await _challenge(call, resp, presented)
            if isinstance(nxt, Presented):
                presented = nxt
                continue
            return nxt
        problem = _problem(resp.body)
        detail, _ = _sanitize(problem["detail"], call, cap=MAX_DETAIL_CHARS)
        if status == 402:
            return call.refusal("payment_required", detail or "the resource requires payment (out of scope)",
                                status=402, budget=_budget(resp))
        if status == 403:
            reason = problem["error"] if problem["error"] in _DENIAL_CODES else "forbidden"
            return Denied(reason, detail, list(call.challenges), call.resource, budget=_budget(resp))
        if status == 408:
            return Expired("request_expired", list(call.challenges), call.resource)
        if status == 410:
            return call.refusal("gone", detail or "the resource reports the target is gone", status=410)
        if status == 429:
            return call.refusal("rate_limited", detail or "the resource asked us to slow down", status=429,
                                retry_after=requirement.parse_retry_after(resp.headers.get("retry-after")))
        return call.refusal("resource_error", detail or f"the resource answered HTTP {status}", status=status,
                            budget=_budget(resp))


async def _send(call: _Call, presented: Presented) -> Union[egress.CappedResponse, Outcome]:
    try:
        headers = signer.sign_request(
            agent_name=call.agent, method=call.method, url=call.url, body=call.body or b"",
            content_type=call.content_type, now=call.now, min_remaining=AGENT_TOKEN_MIN_REMAINING,
            presented_token=None if presented.kind == "agent" else presented.token,
            expected_key_thumbprint=call.jkt, extra_components=call.meta.additional_signature_components,
        )
    except signer.KeyRotated:
        return call.refusal("key_rotated", "the agent key rotated under this call; start a fresh call")
    except signer.SigningUnavailable as exc:
        return call.refusal("unsupported_signature_components", str(exc))
    headers["Accept"] = "application/json"
    headers["AAuth-Capabilities"] = CAPABILITIES_HEADER
    if call.method == "POST" and call.content_type:
        headers["Content-Type"] = call.content_type
    remaining = call.remaining()
    if remaining <= 0.5:
        return call.refusal("timeout", "the call budget is exhausted before the request could be sent")
    leg = min(LEG_TIMEOUT, remaining)
    call.sends += 1
    try:
        async with call.factory(httpx.Timeout(leg, connect=min(5.0, leg))) as client:
            return await asyncio.wait_for(
                egress.request_capped(client, call.method, call.url, max_bytes=RESOURCE_BODY_MAX_BYTES,
                                      headers=headers, content=call.body if call.method == "POST" else None,
                                      secret=presented.token, peer="resource"),
                timeout=leg,
            )
    except egress.EgressError as exc:
        # A dropped connection after the write is as unknown as a timeout (#914).
        possibly = exc.code in ("timeout", "unreachable") and call.method == "POST"
        return call.refusal(exc.code, scrub_tokens(exc.detail, call.tokens), possibly_executed=possibly)
    except asyncio.TimeoutError:
        return call.refusal("timeout", "the resource did not answer within the call budget",
                            possibly_executed=call.method == "POST")


def _result(call: _Call, resp: egress.CappedResponse, presented: Presented) -> ResourceResult:
    text, truncated = _sanitize(resp.body.decode("utf-8", "replace"), call)
    return ResourceResult(status=resp.status, body=text, truncated=truncated, token_kind=presented.kind,
                          challenges=list(call.challenges), resource=call.resource, method=call.method,
                          path=call.path, budget=_budget(resp), content_type=resp.headers.get("content-type"),
                          access_server=call.access_server if presented.kind == "auth" else None)


async def _person(call: _Call) -> Union[Presented, Outcome]:
    """Present the held person token, or obtain one from the PS (ent#878)."""
    held = pt.held_token(call.agent, call.resource, now=call.now)
    if held is None:
        wait = max(0, min(pt.POST_WAIT, int(call.remaining()) - 2))
        out = await pt.person_token(call.agent, call.resource, wait=wait, now=call.now, client_factory=call.factory)
        if isinstance(out, pt.PendingInteraction):
            return InteractionRequired(handle=out.handle, url=out.url, code=out.code, link=out.link,
                                       retry_after=out.retry_after, at="person_server",
                                       challenges=list(call.challenges), expires_at=out.expires_at,
                                       resource=call.resource)
        if isinstance(out, pt.PersonTokenRefused):
            if out.code in pt.DENIAL_CODES:
                return Denied(out.code, out.detail, list(call.challenges), call.resource)
            if out.code in ("expired", "gone"):
                return Expired(f"pending_{out.code}", list(call.challenges), call.resource)
            return call.refusal(out.code, out.detail)
        held = pt.held_token(call.agent, call.resource, now=call.now)
        if held is None:
            return call.refusal("person_token_unavailable", "the person token was issued but is not presentable on this key")
    if held.key_thumbprint != call.jkt:
        return call.refusal("key_rotated", "the person token is bound to a key that is not this call's key")
    call.person_used = True
    call.tokens.add(held.token)
    return Presented("person", held.token, held.sub, held.jti, held.exp)


async def _challenge(call: _Call, resp: egress.CappedResponse, presented: Presented) -> Union[Presented, Outcome]:
    header = resp.headers.get("aauth-requirement")
    if header is None:
        err = _signature_error(resp.headers)
        if presented.kind == "auth" and err in _REAUTH_SIGNATURE_ERRORS and not call.reauthed:
            call.reauthed = True
            call.challenges.append(f"signature-error:{err}")
            _forget_auth(call)
            return await _person(call)
        return call.refusal("signature_rejected", f"the resource rejected the {presented.kind} token signature ({err})",
                            status=401)
    try:
        name, params = requirement.parse_aauth_requirement(header)
    except requirement.RequirementFormatError as exc:
        return call.refusal("unsupported_requirement", f"AAuth-Requirement is malformed: {exc}", status=401)
    call.challenges.append(name)

    if name == "agent-token":
        if presented.kind != "agent" and not call.agent_retried:
            call.agent_retried = True
            return _agent_presented(call.agent_info)
        return call.refusal("agent_token_rejected", "the resource does not accept this agent's token", status=401)

    if name == "person-token":
        if presented.kind == "agent" and not call.person_used:
            return await _person(call)
        if presented.kind == "auth" and not call.reauthed:
            call.reauthed = True
            _forget_auth(call)
            return await _person(call)
        return call.refusal("person_token_rejected", "the resource does not accept the person token we hold", status=401)

    if name == "auth-token":
        return await _auth_token_challenge(call, params, presented)

    return call.refusal(f"unsupported_requirement:{name}",
                        f"the resource requires '{name}', which this client cannot satisfy", status=401)


async def _auth_token_challenge(call: _Call, params: Dict[str, str], presented: Presented) -> Union[Presented, Outcome]:
    raw = params.get("resource-token")
    if not raw:
        return call.refusal("resource_token_missing", "the auth-token challenge carried no resource-token", status=401)
    if presented.kind == "agent":
        return call.refusal("unexpected_challenge",
                            "the resource demanded an auth token of an agent token; it must challenge person-token first",
                            status=401)
    if not call.ps:
        return call.refusal("person_server_unconfigured", "no person server is configured (setting aauth_person_server)")
    try:
        verified = await resource_token.verify_resource_token(
            raw, resource=call.resource, agent_jkt=call.jkt, ps=call.ps, presented_sub=presented.sub,
            presented_jti=presented.jti, now=call.now)
    except resource_token.ResourceTokenError as exc:
        logger.warning("[aauth] resource token from %s refused (%s) — possible tampering: %s",
                       call.resource, exc.check, exc.detail)
        return call.refusal(f"resource_token_invalid:{exc.check}", exc.detail, status=401)
    call.tokens.add(verified.token)
    if presented.kind == "auth":
        if call.stepped_up:
            return call.refusal("step_up_loop", "the resource demanded a second step-up in one call", status=401)
        call.stepped_up = True
    return await _exchange_and_continue(call, verified, presented)


async def _exchange_and_continue(call: _Call, verified: resource_token.ResourceToken,
                                 presented: Presented) -> Union[Presented, Outcome]:
    if call.exchanges >= MAX_PS_EXCHANGES:
        return call.refusal("too_many_rounds", f"more than {MAX_PS_EXCHANGES} person-server exchanges in one call")
    call.exchanges += 1
    out = await _exchange(call, verified, presented)
    if isinstance(out, auth_token.AuthTokenIssued):
        held = out.held
        call.tokens.add(held.token)
        _store_auth(call, held)
        _note_auth_issuer(call, held)
        return Presented("auth", held.token, held.sub, held.jti, held.exp)
    if isinstance(out, auth_token.ExchangePending):
        kind = "ps_interaction" if out.requirement == "interaction" else "ps_approval"
        handle = _save_pending(call, kind, {
            "pending_url": out.pending_url, "retry_after": out.retry_after, "url": out.url, "code": out.code,
            "status": out.status, "presented_jti": presented.jti, "presented_kind": presented.kind,
            "presented_sub": presented.sub, "presented_exp": presented.exp,
            "resource_token_jti": verified.jti, "next_poll_at": call.now + out.retry_after,
            "as_url": verified.as_url,
        })
        if isinstance(handle, Refusal):
            return handle
        expires_at = call.now + PENDING_TTL
        if out.requirement == "interaction":
            link = requirement.interaction_link(out.url, out.code) if out.url and out.code else None
            return InteractionRequired(handle=handle, url=out.url, code=out.code, link=link,
                                       retry_after=out.retry_after, at="person_server",
                                       challenges=list(call.challenges), expires_at=expires_at, resource=call.resource)
        return ResourcePending(handle=handle, retry_after=out.retry_after, requirement="approval", at="person_server",
                               challenges=list(call.challenges), expires_at=expires_at, resource=call.resource)
    code = out.code
    if code in _FRESH_CHAIN_CODES and presented.kind == "person" and not call.fresh_chain:
        call.fresh_chain = True
        call.challenges.append(f"ps:{code}")
        pt.forget(call.agent, call.resource)
        return await _person(call)
    if code in _DENIAL_CODES:
        return Denied(code, out.detail, list(call.challenges), call.resource)
    if code == "as_unreachable":
        return call.refusal("ps_unreachable", out.detail, status=out.ps_status)
    return call.refusal(f"ps_rejected:{code}", out.detail, status=out.ps_status)


async def _deferred(call: _Call, resp: egress.CappedResponse, presented: Presented) -> Outcome:
    """A 202 from the resource: record it, hand back a handle; never poll inline (S5)."""
    header = resp.headers.get("aauth-requirement")
    if header is not None:
        try:
            name, params = requirement.parse_aauth_requirement(header)
        except requirement.RequirementFormatError as exc:
            return call.refusal("unsupported_requirement", f"AAuth-Requirement is malformed: {exc}", status=202)
        call.challenges.append(name)
        if name == "auth-token":
            # Deferred delivery (draft §Auth Token Required): the resource will
            # deliver once we hold an auth token. Verify and exchange NOW (the
            # sink still applies); the poll then presents the auth token.
            nxt = await _auth_token_challenge(call, params, presented)
            if not isinstance(nxt, Presented):
                return nxt
            presented = nxt
        else:
            return call.refusal(f"unsupported_requirement:{name}",
                                f"the resource deferred with '{name}', which this client cannot satisfy", status=202)
    location = resp.headers.get("location")
    if not location:
        return call.refusal("resource_protocol_error", "a deferred (202) answer carried no Location", status=202)
    pending_url = urljoin(call.url, location.strip())
    if not discovery._same_origin(pending_url, call.resource) or \
            not requirement.interaction_url_ok(pending_url.split("?", 1)[0]):
        return call.refusal("resource_protocol_error", "the deferred Location is not on the resource", status=202)
    retry_after = requirement.parse_retry_after(resp.headers.get("retry-after"))
    handle = _save_pending(call, "resource_polling", {
        "pending_url": pending_url, "retry_after": retry_after, "token_kind": presented.kind,
        "presented_jti": presented.jti, "next_poll_at": call.now + retry_after,
    })
    if isinstance(handle, Refusal):
        return handle
    return ResourcePending(handle=handle, retry_after=retry_after, requirement="deferred", at="resource",
                           challenges=list(call.challenges), expires_at=call.now + PENDING_TTL,
                           resource=call.resource)


def _still_pending(call: _Call, resp: egress.CappedResponse) -> Outcome:
    """A poll answered 202: refresh the record's interval, hand the same handle back."""
    handle, record = call.polling
    retry_after = requirement.parse_retry_after(resp.headers.get("retry-after"), default=int(record["retry_after"]))
    record["retry_after"] = retry_after
    record["next_poll_at"] = call.now + retry_after
    try:
        ps_client.update_pending(handle, record, now=call.now)
    except Exception:  # noqa: BLE001 — the record stands until its TTL
        logger.warning("[aauth] could not refresh pending record for %s", call.agent, exc_info=True)
    return ResourcePending(handle=handle, retry_after=retry_after, requirement="deferred", at="resource",
                           challenges=list(call.challenges), expires_at=int(record["expires_at"]),
                           resource=call.resource)


def _pending_shape(call: _Call, handle: str, record: Dict[str, Any]) -> Outcome:
    """The outcome a still-pending record presents (same handle, current interval)."""
    kind = record["kind"]
    if kind == "ps_interaction":
        url, code = record.get("url"), record.get("code")
        return InteractionRequired(handle=handle, url=url, code=code,
                                   link=requirement.interaction_link(url, code) if url and code else None,
                                   retry_after=int(record["retry_after"]), at="person_server",
                                   challenges=list(call.challenges), expires_at=int(record["expires_at"]),
                                   resource=call.resource)
    return ResourcePending(handle=handle, retry_after=int(record["retry_after"]),
                           requirement="approval" if kind == "ps_approval" else "deferred",
                           at="person_server" if kind == "ps_approval" else "resource",
                           challenges=list(call.challenges), expires_at=int(record["expires_at"]),
                           resource=call.resource)


async def resume(agent_name: str, handle: Any, *, now: Optional[int] = None, wait: int = RESUME_WAIT,
                 client_factory: Optional[Callable] = None, budget_seconds: float = ROUTE_BUDGET) -> Outcome:
    """Continue a pending call by its handle (fail closed: ``handle_unknown``)."""
    from services import a2a_client

    deadline = time.monotonic() + max(0.0, float(budget_seconds))    # the budget covers discovery too
    our_issuer = config.active_issuer()
    if not our_issuer:
        raise signer.SigningUnavailable("AAuth is not enabled on this instance (flag off or no issuer)")
    now = int(time.time()) if now is None else now
    try:
        record = ps_client.load_pending(agent_name, handle)
    except Exception:  # noqa: BLE001
        return Refusal("state_unavailable", "pending state is unavailable (Redis)")
    if record is None:
        return Expired("handle_unknown", [], "")
    resource = record["resource"]
    if record.get("our_issuer") != our_issuer or record.get("ps_issuer") != config.get_person_server():
        ps_client.delete_pending(handle, record)     # a re-pointed PS or a new tunnel host: the consent is moot
        return Expired("handle_unknown", list(record.get("challenges") or []), resource)
    if not resource_allowlist.is_allowed(resource):
        ps_client.delete_pending(handle, record)
        return Refusal("resource_not_allowed", "the resource is no longer on the allow-list", resource=resource)
    try:
        meta = await resource_meta.metadata(resource)
    except resource_meta.ResourceMetadataError as exc:
        return Refusal(exc.code, exc.detail, resource=resource)
    info = signer.agent_token_info(agent_name, now=now, min_remaining=AGENT_TOKEN_MIN_REMAINING)
    body = record.get("body")
    call = _Call(agent=agent_name, resource=resource, method=record["method"], path=record["path"],
                 body=body.encode("utf-8") if isinstance(body, str) else None,
                 content_type=record.get("content_type"), justification=None, now=now, our_issuer=our_issuer,
                 ps=config.get_person_server(), meta=meta, jkt=info.thumbprint, agent_info=info,
                 factory=client_factory or a2a_client._http_client,
                 deadline=deadline,
                 challenges=list(record.get("challenges") or []))
    call.tokens.add(info.token)
    if info.thumbprint != record.get("key_thumbprint"):
        # The consent was given against the previous key window; the PS's auth
        # token would be bound to that key. Start over rather than sign a lie.
        ps_client.delete_pending(handle, record)
        return call.refusal("key_rotated", "the agent key rotated while the call was pending; call again")
    if now < int(record.get("next_poll_at", 0)):
        return _pending_shape(call, handle, record)          # early: no network call

    if record["kind"] in ("ps_interaction", "ps_approval"):
        return await _resume_exchange(call, handle, record, wait=max(0, min(wait, pt.MAX_WAIT)))
    return await _resume_resource_poll(call, handle, record)


async def _resume_exchange(call: _Call, handle: str, record: Dict[str, Any], *, wait: int) -> Outcome:
    out = await ps_client.poll_exchange(call.agent, record, wait=wait, now=call.now,
                                        client_factory=call.factory, key_thumbprint=call.jkt)
    if isinstance(out, auth_token.AuthTokenIssued):
        held = out.held
        call.tokens.add(held.token)
        _store_auth(call, held)
        _note_auth_issuer(call, held)
        ps_client.delete_pending(handle, record)
        return await _run(call, Presented("auth", held.token, held.sub, held.jti, held.exp))
    if isinstance(out, auth_token.ExchangePending):
        record.update(retry_after=out.retry_after, next_poll_at=call.now + out.retry_after, status=out.status)
        if out.url and out.code:
            record.update(url=out.url, code=out.code)
        try:
            ps_client.update_pending(handle, record, now=call.now)
        except Exception:  # noqa: BLE001
            logger.warning("[aauth] could not refresh pending record for %s", call.agent, exc_info=True)
        return _pending_shape(call, handle, record)
    code = out.code
    if code in _DENIAL_CODES:
        ps_client.delete_pending(handle, record)
        return Denied(code, out.detail, list(call.challenges), call.resource)
    if code == "expired":
        ps_client.delete_pending(handle, record)
        return Expired("pending_expired", list(call.challenges), call.resource)
    if code == "gone":
        ps_client.delete_pending(handle, record)
        return call.refusal("gone", out.detail, status=410)
    if code == "slow_down":
        record["retry_after"] = min(requirement.MAX_RETRY_AFTER, int(record["retry_after"]) + SLOW_DOWN_STEP)
        record["next_poll_at"] = call.now + record["retry_after"]
        try:
            ps_client.update_pending(handle, record, now=call.now)
        except Exception:  # noqa: BLE001
            logger.warning("[aauth] could not persist the slow-down for %s", call.agent, exc_info=True)
        return _pending_shape(call, handle, record)
    if code == "as_unreachable":
        return call.refusal("ps_unreachable", out.detail, status=out.ps_status)
    return call.refusal(f"ps_rejected:{code}", out.detail, status=out.ps_status)


async def _resume_resource_poll(call: _Call, handle: str, record: Dict[str, Any]) -> Outcome:
    kind = record.get("token_kind")
    if kind == "auth":
        held = _held_auth(call)
        if held is None:
            return call.refusal("token_unavailable", "the auth token this deferred call used is no longer held; call again")
        call.tokens.add(held.token)
        presented = Presented("auth", held.token, held.sub, held.jti, held.exp)
    elif kind == "person":
        held = pt.held_token(call.agent, call.resource, now=call.now)
        if held is None or held.key_thumbprint != call.jkt:
            return call.refusal("token_unavailable", "the person token this deferred call used is no longer held; call again")
        call.tokens.add(held.token)
        presented = Presented("person", held.token, held.sub, held.jti, held.exp)
    else:
        presented = _agent_presented(call.agent_info)
    call.method, call.body, call.content_type = "GET", None, None
    call.override_url = record["pending_url"]
    call.polling = (handle, record)
    return await _run(call, presented)
