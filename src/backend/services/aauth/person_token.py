"""Person tokens from the configured person server (ent#878).

An agent obtains a **person token** for one resource from its owner's person
server (PS), as itself (#623 signature profile), after its owner has bound it
once through the PS's link-and-code flow. The pieces, in call order:

* ``person_token(agent, resource)`` — cache → pending record → signed POST to
  the PS's ``person_token_endpoint`` with ``{"resource", "capabilities":
  ["interaction"]}`` and ``Prefer: wait=N``;
* ``resume(agent, handle)`` — a signed GET of the pending URL the PS gave us,
  driving the draft's deferred-response state machine (202 continue · 200 done
  · 403 denied/abandoned/revoked · 408 expired · 410 gone · 429 slow down);
* ``held_token(agent, resource)`` / ``held_token_by_jti(agent, jti)`` — what
  ent#879 presents, with the token VALUE (service-internal; no route or tool
  serialises it).

State. The held token lives in **worker memory** only, keyed by
``(our_issuer, ps_issuer, agent, resource)`` and stamped with the thumbprint of
the agent key the PS bound it to (``cnf.jwk``); it is served only while that
key is still the agent's current key (``signer.current_agent_key``). The
pending-interaction record lives in **Redis** (TTL'd), because the next call
lands on either worker: ``aauth:pt:handle:{sha256(handle)}`` → the record,
``aauth:pt:pending:{sha256(scope)}`` → the handle, and a per-agent set that
bounds open interactions. The record carries no token. A 202 that cannot be
persisted is a refusal (``state_unavailable``), never an in-memory fallback —
the other worker would answer ``pending_unknown``.

FastAPI-free (Invariant #1): the router maps outcomes to HTTP, enforces the
rate budgets and writes the audit row from :func:`audit_details` — which never
carries the token, the handle, the URL or the code.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Tuple, Union
from urllib.parse import urljoin

import httpx

from services.aauth import config, discovery, egress, jose, person_server, requirement, signer

logger = logging.getLogger(__name__)

PERSON_TOKEN_TYPE = "aa-person+jwt"
PERSON_METADATA_DOCUMENT = "aauth-person.json"
CAPABILITIES = ("interaction",)

POST_WAIT = 5
RESUME_WAIT = 15
MAX_WAIT = 15
DEADLINE_SLACK = 8
PS_BODY_MAX_BYTES = 16 * 1024

HELD_MARGIN = 300                      # draft §Refresh Margin
MAX_PERSON_TOKEN_LIFETIME = 3600       # "exp ≤ 1 h"
CLOCK_SKEW = 60
AGENT_TOKEN_MIN_REMAINING = 1200       # refresh order: a fresh agent token before a PS request

PENDING_TTL = 1800
MAX_PENDING_PER_AGENT = 3
SLOW_DOWN_STEP = 5
HANDLE_RE = re.compile(r"^[A-Za-z0-9_-]{32}$")
_ERROR_RE = re.compile(r"^[a-z_]{1,64}$")

_HANDLE_KEY = "aauth:pt:handle:{}"
_POINTER_KEY = "aauth:pt:pending:{}"
_AGENT_SET_KEY = "aauth:pt:agent:{}"

#: PS problem ``error`` values on a 403 poll that are distinct outcomes (draft §Polling Error Codes).
_DENIAL_CODES = ("denied", "abandoned", "revoked")
TERMINAL_REFUSALS = frozenset(_DENIAL_CODES) | {"expired", "gone"}


# --------------------------------------------------------------------------- #
# Outcomes
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class HeldPersonToken:
    """Service-internal; routes and tools never serialise ``token``."""

    token: str = field(repr=False)
    jti: str = ""
    aud: str = ""
    sub: str = ""
    iss: str = ""
    exp: int = 0
    key_thumbprint: str = ""


@dataclass(frozen=True)
class PersonTokenIssued:
    jti: str
    aud: str
    sub: str
    iss: str
    exp: int
    cached: bool
    key_thumbprint: str
    reissued_for_key: bool = False


@dataclass(frozen=True)
class PendingInteraction:
    handle: str
    url: Optional[str]
    code: Optional[str]
    link: Optional[str]
    retry_after: int
    status: str                 # pending | interacting | slow_down
    expires_at: int
    resource: str
    iss: str
    created: bool = False       # True on the call that minted the record (audited)


@dataclass(frozen=True)
class PersonTokenRefused:
    code: str
    detail: str
    ps_status: Optional[int] = None
    ps_error: Optional[str] = None
    resource: Optional[str] = None
    iss: Optional[str] = None


Outcome = Union[PersonTokenIssued, PendingInteraction, PersonTokenRefused]

_held: Dict[Tuple[str, str, str, str], HeldPersonToken] = {}


# --------------------------------------------------------------------------- #
# Redis-backed pending records
# --------------------------------------------------------------------------- #
def _get_redis():
    from services.rate_limiter import _get_redis as get

    return get()


def _h(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _scope(our_issuer: str, ps_issuer: str, agent_name: str, resource: str) -> str:
    return "\n".join([our_issuer, ps_issuer, agent_name, resource])


def _text(value: Any) -> Optional[str]:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value if isinstance(value, str) else None


def _load_record(client, handle: str) -> Optional[Dict[str, Any]]:
    raw = _text(client.get(_HANDLE_KEY.format(_h(handle))))
    if not raw:
        return None
    try:
        record = json.loads(raw)
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def _save_record(client, handle: str, record: Dict[str, Any], *, now: int) -> None:
    ttl = max(1, int(record["expires_at"]) - now)
    client.set(_HANDLE_KEY.format(_h(handle)), json.dumps(record, separators=(",", ":")), ex=ttl)
    client.set(_POINTER_KEY.format(_h(record["scope"])), handle, ex=ttl)
    agent_key = _AGENT_SET_KEY.format(record["agent"])
    client.sadd(agent_key, _h(handle))
    client.expire(agent_key, PENDING_TTL)


def _delete_record(client, handle: str, record: Dict[str, Any]) -> None:
    client.delete(_HANDLE_KEY.format(_h(handle)))
    client.delete(_POINTER_KEY.format(_h(record["scope"])))
    client.srem(_AGENT_SET_KEY.format(record["agent"]), _h(handle))


def _pending_handle(client, scope: str) -> Optional[str]:
    return _text(client.get(_POINTER_KEY.format(_h(scope))))


def _open_pending_count(client, agent_name: str) -> int:
    key = _AGENT_SET_KEY.format(agent_name)
    members = client.smembers(key) or ()
    live = 0
    for member in members:
        digest = _text(member) or ""
        if client.exists(_HANDLE_KEY.format(digest)):
            live += 1
        else:
            client.srem(key, member)
    return live


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _problem_error(body: bytes) -> str:
    """The bounded ``error`` of a problem+json body, or ``unparseable``."""
    try:
        doc = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return "unparseable"
    err = doc.get("error") if isinstance(doc, dict) else None
    return err if isinstance(err, str) and _ERROR_RE.match(err) else "unparseable"


def _signature_error(headers) -> str:
    """``Signature-Error: error=<code>`` → code (bounded), else ``invalid_signature``."""
    value = headers.get("signature-error") or ""
    m = re.search(r"error=\"?([a-z_]{1,64})\"?", value)
    return m.group(1) if m else "invalid_signature"


def _body_status(body: bytes) -> str:
    try:
        doc = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return "pending"
    status = doc.get("status") if isinstance(doc, dict) else None
    return status if status in ("pending", "interacting") else "pending"


def _clamp_wait(wait: int) -> int:
    return max(0, min(MAX_WAIT, int(wait)))


async def _signed_request(agent_name: str, method: str, url: str, *, body: bytes, content_type: Optional[str],
                          wait: int, now: int, client_factory: Optional[Callable]) -> Tuple[egress.CappedResponse, signer.AgentToken]:
    from services import a2a_client

    info = signer.agent_token_info(agent_name, now=now, min_remaining=AGENT_TOKEN_MIN_REMAINING)
    headers = signer.sign_request(agent_name=agent_name, method=method, url=url, body=body,
                                  content_type=content_type, now=now, min_remaining=AGENT_TOKEN_MIN_REMAINING)
    headers["Accept"] = "application/json"
    headers["Prefer"] = f"wait={wait}"
    if content_type:
        headers["Content-Type"] = content_type
    deadline = wait + DEADLINE_SLACK
    factory = client_factory or a2a_client._http_client
    timeout = httpx.Timeout(deadline, connect=5.0, read=wait + 2)
    async with factory(timeout) as client:
        resp = await asyncio.wait_for(
            egress.request_capped(client, method, url, max_bytes=PS_BODY_MAX_BYTES, headers=headers,
                                  content=body if body else None, secret=info.token),
            timeout=deadline,
        )
    return resp, info


def _inspect(raw: Any, *, resource: str, ps_issuer: str, info: signer.AgentToken,
             expires_in: Any, now: int) -> HeldPersonToken:
    """The held-token checks (AC "Verification of what we hold"). No PS signature check (T4)."""
    if not isinstance(raw, str):
        raise ValueError("person_token is not a string")
    header, claims, _, _ = jose.decode_unverified(raw)
    if header.get("typ") != PERSON_TOKEN_TYPE:
        raise ValueError(f"person token typ must be {PERSON_TOKEN_TYPE}")
    if claims.get("dwk") != PERSON_METADATA_DOCUMENT:
        raise ValueError(f"person token dwk must be {PERSON_METADATA_DOCUMENT}")
    if claims.get("iss") != ps_issuer:
        raise ValueError("person token iss is not the configured person server")
    if claims.get("aud") != resource:
        raise ValueError("person token aud is not the requested resource")
    sub, jti = claims.get("sub"), claims.get("jti")
    if not isinstance(sub, str) or not sub or not isinstance(jti, str) or not jti:
        raise ValueError("person token has no sub/jti")
    iat, exp = claims.get("iat"), claims.get("exp")
    if any(isinstance(v, bool) or not isinstance(v, int) for v in (iat, exp)):
        raise ValueError("person token iat/exp are not integers")
    if exp <= now:
        raise ValueError("person token has already expired")
    if iat - now > CLOCK_SKEW or exp - iat > MAX_PERSON_TOKEN_LIFETIME + CLOCK_SKEW:
        raise ValueError("person token lifetime is not acceptable")
    cnf = claims.get("cnf")
    cnf_jwk = cnf.get("jwk") if isinstance(cnf, dict) else None
    try:
        jose.jwk_to_public_key(cnf_jwk)
        thumbprint = jose.thumbprint(cnf_jwk)
    except (jose.JoseError, KeyError, TypeError):
        raise ValueError("person token cnf.jwk is not a usable key") from None
    if thumbprint != info.thumbprint:
        raise KeyMismatch(thumbprint)
    held_exp = min(exp, info.exp)
    if isinstance(expires_in, int) and not isinstance(expires_in, bool) and expires_in > 0:
        held_exp = min(held_exp, now + expires_in)
    return HeldPersonToken(token=raw, jti=jti, aud=resource, sub=sub, iss=ps_issuer,
                           exp=held_exp, key_thumbprint=thumbprint)


class KeyMismatch(Exception):
    """The PS bound the token to a key that is not this agent's current key."""

    def __init__(self, thumbprint: str):
        super().__init__("cnf mismatch")
        self.thumbprint = thumbprint


def _accept(resp: egress.CappedResponse, *, our_issuer: str, ps_issuer: str, agent_name: str,
            resource: str, info: signer.AgentToken, now: int) -> Union[PersonTokenIssued, PersonTokenRefused, KeyMismatch]:
    try:
        doc = json.loads(resp.body.decode("utf-8"))
        if not isinstance(doc, dict):
            raise ValueError("person token response is not an object")
        held = _inspect(doc.get("person_token"), resource=resource, ps_issuer=ps_issuer, info=info,
                        expires_in=doc.get("expires_in"), now=now)
    except KeyMismatch as exc:
        return exc
    except (ValueError, UnicodeDecodeError, jose.JoseError) as exc:
        return PersonTokenRefused("token_invalid", f"person token failed the held-token checks: {exc}",
                                  ps_status=200, resource=resource, iss=ps_issuer)
    _held[(our_issuer, ps_issuer, agent_name, resource)] = held
    return PersonTokenIssued(jti=held.jti, aud=held.aud, sub=held.sub, iss=held.iss, exp=held.exp,
                             cached=False, key_thumbprint=held.key_thumbprint)


def _refusal(resp: egress.CappedResponse, *, resource: str, ps_issuer: str) -> PersonTokenRefused:
    """Every non-200/202 status → a named refusal. Never auto-retried here."""
    status = resp.status
    kw = dict(ps_status=status, resource=resource, iss=ps_issuer)
    if status == 401:
        err = _signature_error(resp.headers)
        return PersonTokenRefused("signature_rejected", "the person server rejected the request signature",
                                  ps_error=err, **kw)
    if status == 403:
        err = _problem_error(resp.body)
        code = err if err in _DENIAL_CODES else "denied"
        return PersonTokenRefused(code, f"the person declined or the binding was {code}", ps_error=err, **kw)
    if status == 408:
        return PersonTokenRefused("expired", "the interaction expired before the person answered",
                                  ps_error=_problem_error(resp.body), **kw)
    if status == 410:
        return PersonTokenRefused("gone", "the pending interaction is gone; start a fresh request",
                                  ps_error=_problem_error(resp.body), **kw)
    if status == 429:
        return PersonTokenRefused("slow_down", "the person server asked us to slow down",
                                  ps_error=_problem_error(resp.body), **kw)
    if status == 404 and resp.body == b"":
        return PersonTokenRefused("poll_rejected", "the person server does not know this pending URL", **kw)
    return PersonTokenRefused("ps_error", f"the person server answered HTTP {status}",
                              ps_error=_problem_error(resp.body), **kw)


def _pending_from(resp: egress.CappedResponse, *, endpoint: str, meta: person_server.PersonServerMetadata,
                  record: Optional[Dict[str, Any]]) -> Union[Dict[str, Any], PersonTokenRefused]:
    """Validate a 202 and return the record fields it establishes (or refreshes)."""
    kw = dict(ps_status=202, resource=record["resource"] if record else None, iss=meta.issuer)
    url = code = None
    header = resp.headers.get("aauth-requirement")
    if header is not None:
        try:
            name, params = requirement.parse_aauth_requirement(header)
        except requirement.RequirementFormatError as exc:
            return PersonTokenRefused("ps_protocol_error", f"AAuth-Requirement is malformed: {exc}", **kw)
        if name != "interaction":
            return PersonTokenRefused("unsupported_requirement",
                                      f"the person server requires '{name}', which this prototype cannot satisfy",
                                      ps_error=name, **kw)
        url, code = params.get("url"), requirement.normalize_code(params.get("code"))
        if not requirement.interaction_url_ok(url) or code is None:
            return PersonTokenRefused("ps_protocol_error", "the interaction url or code is not acceptable", **kw)
        if person_server._origin(url) not in meta.interaction_origins():
            return PersonTokenRefused("ps_protocol_error", "the interaction url is not on the person server", **kw)
    location = resp.headers.get("location")
    if record is None:
        if not location:
            return PersonTokenRefused("ps_protocol_error", "a pending answer carried no Location", **kw)
        pending_url = urljoin(endpoint, location.strip())
        if not discovery._same_origin(pending_url, meta.issuer) or not requirement.interaction_url_ok(pending_url.split("?", 1)[0]):
            return PersonTokenRefused("ps_protocol_error", "the pending Location is not on the person server", **kw)
    else:
        pending_url = record["pending_url"]
        url = url or record.get("url")
        code = code or record.get("code")
    retry_after = requirement.parse_retry_after(resp.headers.get("retry-after"))
    return {"pending_url": pending_url, "url": url, "code": code,
            "status": _body_status(resp.body), "retry_after": retry_after}


def _as_pending(handle: str, record: Dict[str, Any], *, created: bool = False, status: Optional[str] = None) -> PendingInteraction:
    url, code = record.get("url"), record.get("code")
    return PendingInteraction(
        handle=handle, url=url, code=code,
        link=requirement.interaction_link(url, code) if url and code else None,
        retry_after=int(record["retry_after"]), status=status or record["status"],
        expires_at=int(record["expires_at"]), resource=record["resource"], iss=record["ps_issuer"],
        created=created,
    )


# --------------------------------------------------------------------------- #
# Public surface
# --------------------------------------------------------------------------- #
def held_token(agent_name: str, resource: str, *, now: Optional[int] = None) -> Optional[HeldPersonToken]:
    """The cached token for ``(agent, resource)`` if it has ≥ 5 min left and
    its ``cnf`` is the agent's current key; else None."""
    our_issuer = config.active_issuer()
    ps_issuer = config.get_person_server()
    norm = config.normalize_issuer(resource)
    if not our_issuer or not ps_issuer or not norm:
        return None
    held = _held.get((our_issuer, ps_issuer, agent_name, norm))
    if held is None:
        return None
    now = int(time.time()) if now is None else now
    if held.exp - now <= HELD_MARGIN or held.iss != ps_issuer:
        return None
    try:
        _, thumbprint = signer.current_agent_key(agent_name, now=now)
    except signer.SigningUnavailable:
        return None
    return held if held.key_thumbprint == thumbprint else None


def held_token_by_jti(agent_name: str, jti: str, *, now: Optional[int] = None) -> Optional[HeldPersonToken]:
    """Same-worker convenience for ent#879's ``presented_token`` bookkeeping."""
    for (_, _, agent, resource), held in list(_held.items()):
        if agent == agent_name and held.jti == jti:
            return held_token(agent_name, resource, now=now)
    return None


async def person_token(agent_name: str, resource: str, *, wait: int = POST_WAIT, now: Optional[int] = None,
                       client_factory: Optional[Callable] = None) -> Outcome:
    """Issued | PendingInteraction | Refused. Raises ``signer.SigningUnavailable`` when AAuth is inert."""
    our_issuer = config.active_issuer()
    if not our_issuer:
        raise signer.SigningUnavailable("AAuth is not enabled on this instance (flag off or no issuer)")
    now = int(time.time()) if now is None else now
    wait = _clamp_wait(wait)
    ps_issuer = config.get_person_server()
    if not ps_issuer:
        return PersonTokenRefused("person_server_unconfigured",
                                  "no person server is configured (setting aauth_person_server)", resource=resource)
    norm = config.normalize_issuer(resource)
    if not norm:
        return PersonTokenRefused("invalid_resource",
                                  "resource must be an https server identifier (host only, no port/path/query)",
                                  iss=ps_issuer)
    resource = norm

    held = held_token(agent_name, resource, now=now)
    if held is not None:
        return PersonTokenIssued(jti=held.jti, aud=held.aud, sub=held.sub, iss=held.iss, exp=held.exp,
                                 cached=True, key_thumbprint=held.key_thumbprint)

    client = _get_redis()
    if client is None:
        return PersonTokenRefused("state_unavailable", "pending-interaction state is unavailable (Redis)",
                                  resource=resource, iss=ps_issuer)
    scope = _scope(our_issuer, ps_issuer, agent_name, resource)
    try:
        handle = _pending_handle(client, scope)
        if handle:
            record = _load_record(client, handle)
            if record and record.get("agent") == agent_name:
                return await _resume_record(agent_name, handle, record, wait=wait, now=now,
                                            client_factory=client_factory, redis=client)
            client.delete(_POINTER_KEY.format(_h(scope)))
        if _open_pending_count(client, agent_name) >= MAX_PENDING_PER_AGENT:
            return PersonTokenRefused("too_many_pending",
                                      f"this agent already has {MAX_PENDING_PER_AGENT} open interactions",
                                      resource=resource, iss=ps_issuer)
    except Exception:  # noqa: BLE001 — state unknowable ⇒ do not mint a code
        logger.warning("[aauth] pending state unreadable for %s", agent_name, exc_info=True)
        return PersonTokenRefused("state_unavailable", "pending-interaction state is unavailable (Redis)",
                                  resource=resource, iss=ps_issuer)

    try:
        meta = await person_server.metadata(ps_issuer)
    except person_server.PersonServerError as exc:
        return PersonTokenRefused(exc.code, exc.detail, resource=resource, iss=ps_issuer)

    body = json.dumps({"resource": resource, "capabilities": list(CAPABILITIES)},
                      separators=(",", ":")).encode("utf-8")
    try:
        resp, info = await _signed_request(agent_name, "POST", meta.person_token_endpoint, body=body,
                                           content_type="application/json", wait=wait, now=now,
                                           client_factory=client_factory)
    except egress.EgressError as exc:
        return PersonTokenRefused("person_server_unreachable", exc.detail, resource=resource, iss=ps_issuer)
    except asyncio.TimeoutError:
        return PersonTokenRefused("person_server_unreachable", "the person server did not answer in time",
                                  resource=resource, iss=ps_issuer)

    if resp.status == 200:
        outcome = _accept(resp, our_issuer=our_issuer, ps_issuer=ps_issuer, agent_name=agent_name,
                          resource=resource, info=info, now=now)
        if isinstance(outcome, KeyMismatch):
            return PersonTokenRefused("token_invalid", "the person token is bound to a key that is not ours",
                                      ps_status=200, resource=resource, iss=ps_issuer)
        return outcome
    if resp.status != 202:
        return _refusal(resp, resource=resource, ps_issuer=ps_issuer)

    fields = _pending_from(resp, endpoint=meta.person_token_endpoint, meta=meta, record=None)
    if isinstance(fields, PersonTokenRefused):
        return PersonTokenRefused(fields.code, fields.detail, ps_status=202, ps_error=fields.ps_error,
                                  resource=resource, iss=ps_issuer)
    handle = secrets.token_urlsafe(24)
    record = {
        "agent": agent_name, "resource": resource, "our_issuer": our_issuer, "ps_issuer": ps_issuer,
        "scope": scope, **fields,
        "next_poll_at": now + fields["retry_after"], "created_at": now, "expires_at": now + PENDING_TTL,
    }
    try:
        _save_record(client, handle, record, now=now)
    except Exception:  # noqa: BLE001 — fail closed, audited by the router as a refusal
        logger.error("[aauth] could not persist pending interaction for %s", agent_name, exc_info=True)
        return PersonTokenRefused("state_unavailable", "the pending interaction could not be recorded (Redis)",
                                  ps_status=202, resource=resource, iss=ps_issuer)
    return _as_pending(handle, record, created=True)


async def resume(agent_name: str, handle: str, *, wait: int = RESUME_WAIT, now: Optional[int] = None,
                 client_factory: Optional[Callable] = None) -> Outcome:
    """Poll a pending interaction by its handle (fail closed: ``pending_unknown``)."""
    if not config.active_issuer():
        raise signer.SigningUnavailable("AAuth is not enabled on this instance (flag off or no issuer)")
    now = int(time.time()) if now is None else now
    if not isinstance(handle, str) or not HANDLE_RE.match(handle):
        return PersonTokenRefused("pending_unknown", "no pending interaction under that handle")
    client = _get_redis()
    if client is None:
        return PersonTokenRefused("state_unavailable", "pending-interaction state is unavailable (Redis)")
    try:
        record = _load_record(client, handle)
    except Exception:  # noqa: BLE001
        return PersonTokenRefused("state_unavailable", "pending-interaction state is unavailable (Redis)")
    if record is None or record.get("agent") != agent_name:
        return PersonTokenRefused("pending_unknown", "no pending interaction under that handle")
    return await _resume_record(agent_name, handle, record, wait=_clamp_wait(wait), now=now,
                                client_factory=client_factory, redis=client)


async def _resume_record(agent_name: str, handle: str, record: Dict[str, Any], *, wait: int, now: int,
                         client_factory: Optional[Callable], redis) -> Outcome:
    our_issuer, ps_issuer, resource = record["our_issuer"], record["ps_issuer"], record["resource"]
    if ps_issuer != config.get_person_server() or our_issuer != config.active_issuer():
        _delete_record(redis, handle, record)   # a re-pointed PS or a new tunnel host: the binding is moot
        return PersonTokenRefused("pending_unknown", "the pending interaction belongs to another configuration")
    if now < int(record.get("next_poll_at", 0)):
        return _as_pending(handle, record)       # early: no network call (E8)
    try:
        meta = await person_server.metadata(ps_issuer)
    except person_server.PersonServerError as exc:
        return PersonTokenRefused(exc.code, exc.detail, resource=resource, iss=ps_issuer)
    try:
        resp, info = await _signed_request(agent_name, "GET", record["pending_url"], body=b"", content_type=None,
                                           wait=wait, now=now, client_factory=client_factory)
    except egress.EgressError as exc:
        return PersonTokenRefused("person_server_unreachable", exc.detail, resource=resource, iss=ps_issuer)
    except asyncio.TimeoutError:
        return PersonTokenRefused("person_server_unreachable", "the person server did not answer in time",
                                  resource=resource, iss=ps_issuer)

    if resp.status == 200:
        outcome = _accept(resp, our_issuer=our_issuer, ps_issuer=ps_issuer, agent_name=agent_name,
                          resource=resource, info=info, now=now)
        if isinstance(outcome, KeyMismatch):
            # The PS bound the token to the key of the original POST; ours has
            # rolled (window edge, other worker). Clear the record FIRST, then
            # ask afresh — a bound agent gets a direct 200 on the new key.
            _delete_record(redis, handle, record)
            fresh = await person_token(agent_name, resource, wait=min(wait, POST_WAIT), now=now,
                                       client_factory=client_factory)
            if isinstance(fresh, PersonTokenIssued):
                return PersonTokenIssued(**{**fresh.__dict__, "reissued_for_key": True})
            return fresh
        if isinstance(outcome, PersonTokenIssued):
            _delete_record(redis, handle, record)
        return outcome
    if resp.status == 202:
        fields = _pending_from(resp, endpoint=record["pending_url"], meta=meta, record=record)
        if isinstance(fields, PersonTokenRefused):
            return fields
        record.update(fields)
        record["next_poll_at"] = now + fields["retry_after"]
        try:
            _save_record(redis, handle, record, now=now)
        except Exception:  # noqa: BLE001 — the record still stands until its TTL
            logger.warning("[aauth] could not refresh pending interaction for %s", agent_name, exc_info=True)
        return _as_pending(handle, record)
    refused = _refusal(resp, resource=resource, ps_issuer=ps_issuer)
    if refused.code == "slow_down":
        record["retry_after"] = min(requirement.MAX_RETRY_AFTER,
                                    max(int(record["retry_after"]), refused_retry_after(resp)) + SLOW_DOWN_STEP)
        record["next_poll_at"] = now + record["retry_after"]
        try:
            _save_record(redis, handle, record, now=now)
        except Exception:  # noqa: BLE001
            pass
        return _as_pending(handle, record, status="slow_down")
    if refused.code in TERMINAL_REFUSALS:
        _delete_record(redis, handle, record)
    return refused


def refused_retry_after(resp: egress.CappedResponse) -> int:
    return requirement.parse_retry_after(resp.headers.get("retry-after"))


# --------------------------------------------------------------------------- #
# Audit (built here, written by the router) — never the token, handle, url or code
# --------------------------------------------------------------------------- #
def audit_action(outcome: Outcome) -> Optional[str]:
    """The audit ``event_action`` for a state change, or None (cache hit, still pending)."""
    if isinstance(outcome, PersonTokenIssued):
        return None if outcome.cached else "aauth_person_token_issued"
    if isinstance(outcome, PendingInteraction):
        return "aauth_person_token_pending" if outcome.created else None
    if outcome.code in TERMINAL_REFUSALS:
        return "aauth_person_token_denied"
    return "aauth_person_token_refused"


def audit_details(agent_name: str, outcome: Outcome, *, execution_id: Optional[str] = None,
                  trigger_source: Optional[str] = None) -> Dict[str, Any]:
    details: Dict[str, Any] = {"auth": "aauth", "agent": agent_name}
    if isinstance(outcome, PersonTokenIssued):
        details.update(outcome="issued", ps_issuer=outcome.iss, resource=outcome.aud, token_jti=outcome.jti,
                       sub=outcome.sub, exp=outcome.exp, key_thumbprint=outcome.key_thumbprint,
                       cached=outcome.cached, reissued_for_key=outcome.reissued_for_key)
    elif isinstance(outcome, PendingInteraction):
        details.update(outcome="pending", ps_issuer=outcome.iss, resource=outcome.resource, pending=True,
                       status=outcome.status, expires_at=outcome.expires_at)
    else:
        details.update(outcome="refused", reason=outcome.code)
        if outcome.iss:
            details["ps_issuer"] = outcome.iss
        if outcome.resource:
            details["resource"] = outcome.resource
        if outcome.ps_status is not None:
            details["ps_status"] = outcome.ps_status
        if outcome.ps_error:
            details["ps_error"] = outcome.ps_error
    if execution_id:
        details["execution_id"] = execution_id
    if trigger_source:
        details["trigger_source"] = trigger_source
    return details


def reset_cache() -> None:
    """Tests only."""
    _held.clear()
