"""Person-server leg of the resource client: the auth-token exchange, the
auth-token cache and the pending records (ent#879, checkpoint B; taste T1).

**Exchange** (draft §Auth Token Endpoint): a POST to the PS's
``auth_token_endpoint``, signed with the AGENT token (the same leg as
``person_token.signed_ps_request``), carrying ``{resource_token,
presented_token, justification, capabilities}`` and ``Prefer: wait=N``. The
answer is inspected by ``auth_token.inspect_auth_token`` (T5) and mapped to
``AuthTokenIssued`` / ``ExchangePending`` / ``ExchangeRefused`` — the PS's
own error vocabulary (``expired_presented_token``, ``invalid_resource_token``,
``user_unreachable``, ``as_unreachable``, the polling codes) survives as the
refusal ``code`` so the ladder can decide; nothing is retried here.

**Cache**: ``aauth:at:{sha256(our_issuer|ps|agent|key_thumbprint|resource)}``
→ the held token (JSON), TTL to its expiry. Redis already holds transient
secrets (Invariant #12) and the token is proof-of-possession-bound: useless
without the agent key, which never leaves ``signer``. Served only while ≥ 5
min remain and its ``cnf`` is the key the caller is signing with; a cache
that cannot be read is "not held" (one more PS exchange, never an unsigned
call).

**Pending records** share ent#878's handle namespace (``aauth:pt:handle:*``)
through its public ``save_record`` / ``load_record`` / ``delete_record``, with
``kind`` ∈ ``ps_interaction`` | ``ps_approval`` | ``resource_polling`` so
either tool answers ``pending_unknown`` for the other's handles. A record
carries the stored request (what consent was given for — never re-supplied by
the caller on resume) and never a token.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import urljoin

from services.aauth import (auth_token, config, discovery, egress, person_server, person_token as pt,
                            requirement, resource_token)

logger = logging.getLogger(__name__)

CAPABILITIES = ("interaction",)
HELD_MARGIN = 300
PENDING_KINDS = ("ps_interaction", "ps_approval", "resource_polling")
_AUTH_KEY = "aauth:at:{}"
_DENIAL_CODES = ("denied", "abandoned", "revoked", "user_unreachable")
_POLL_CODES = {408: "expired", 410: "gone", 429: "slow_down"}


class PendingBudgetExceeded(Exception):
    """The agent already holds ``person_token.MAX_PENDING_PER_AGENT`` open records (shared with ent#878)."""


def _get_redis():
    from services import rate_limiter

    return rate_limiter.redis_client()


def _h(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Auth-token cache (Redis)
# --------------------------------------------------------------------------- #
def _auth_key(agent_name: str, resource: str, key_thumbprint: str) -> Optional[str]:
    our_issuer, ps = config.active_issuer(), config.get_person_server()
    if not our_issuer or not ps:
        return None
    return _AUTH_KEY.format(_h("\n".join([our_issuer, ps, agent_name, key_thumbprint, resource])))


def held_auth_token(agent_name: str, *, resource: str, key_thumbprint: str,
                    now: int) -> Optional[auth_token.HeldAuthToken]:
    """The cached auth token for ``(agent, key, resource)`` with ≥ 5 min left, else None.

    Raises when Redis is unreadable — the caller treats that as "not held".
    """
    key = _auth_key(agent_name, resource, key_thumbprint)
    client = _get_redis()
    if key is None or client is None:
        return None
    raw = client.get(key)
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    if not raw:
        return None
    try:
        doc = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(doc, dict) or doc.get("key_thumbprint") != key_thumbprint or doc.get("aud") != resource:
        return None
    exp = doc.get("exp")
    if not isinstance(exp, int) or exp - now <= HELD_MARGIN or not isinstance(doc.get("token"), str):
        return None
    return auth_token.HeldAuthToken(token=doc["token"], jti=str(doc.get("jti", "")), iss=str(doc.get("iss", "")),
                                    aud=resource, sub=str(doc.get("sub", "")), scope=doc.get("scope"),
                                    exp=exp, key_thumbprint=key_thumbprint)


def store_auth_token(agent_name: str, held: auth_token.HeldAuthToken, *, now: int) -> bool:
    key = _auth_key(agent_name, held.aud, held.key_thumbprint)
    client = _get_redis()
    if key is None or client is None:
        return False
    ttl = held.exp - now
    if ttl <= HELD_MARGIN:
        return False
    doc = {"token": held.token, "jti": held.jti, "iss": held.iss, "aud": held.aud, "sub": held.sub,
           "scope": held.scope, "exp": held.exp, "key_thumbprint": held.key_thumbprint}
    client.set(key, json.dumps(doc, separators=(",", ":")), ex=ttl)
    return True


def forget_auth_token(agent_name: str, *, resource: str, key_thumbprint: str) -> None:
    key = _auth_key(agent_name, resource, key_thumbprint)
    client = _get_redis()
    if key is not None and client is not None:
        client.delete(key)


# --------------------------------------------------------------------------- #
# Pending records (ent#878's namespace, ent#879's kinds)
# --------------------------------------------------------------------------- #
def _scope(record: Dict[str, Any]) -> str:
    return "\n".join(["879", record.get("our_issuer") or "", record.get("ps_issuer") or "", record["agent"],
                      record["resource"], record["kind"], record.get("method", ""), record.get("path", "")])


def save_pending(agent_name: str, record: Dict[str, Any], *, now: int) -> str:
    """Persist ``record`` (``kind`` ∈ PENDING_KINDS) and return its handle.

    Raises ``PendingBudgetExceeded`` at the shared per-agent bound, anything else when Redis fails.
    """
    if record.get("kind") not in PENDING_KINDS or record.get("agent") != agent_name:
        raise ValueError("not an ent#879 pending record")
    client = _get_redis()
    if client is None:
        raise RuntimeError("pending state unavailable (Redis)")
    # One budget for both issues: these records join ent#878's per-agent set,
    # so the bound person_token() enforces is enforced here too.
    if pt._open_pending_count(client, agent_name) >= pt.MAX_PENDING_PER_AGENT:
        raise PendingBudgetExceeded(f"this agent already has {pt.MAX_PENDING_PER_AGENT} open interactions")
    handle = secrets.token_urlsafe(24)
    record = dict(record)
    record["scope"] = _scope(record)
    pt.save_record(client, handle, record, now=now)
    return handle


def load_pending(agent_name: str, handle: Any) -> Optional[Dict[str, Any]]:
    """The record under ``handle`` if it is this agent's and one of our kinds. Raises when Redis fails."""
    if not isinstance(handle, str) or not pt.HANDLE_RE.match(handle):
        return None
    client = _get_redis()
    if client is None:
        raise RuntimeError("pending state unavailable (Redis)")
    record = pt.load_record(client, handle)
    if record is None or record.get("agent") != agent_name or record.get("kind") not in PENDING_KINDS:
        return None
    return record


def update_pending(handle: str, record: Dict[str, Any], *, now: int) -> None:
    client = _get_redis()
    if client is not None:
        pt.save_record(client, handle, record, now=now)


def delete_pending(handle: str, record: Dict[str, Any]) -> None:
    client = _get_redis()
    if client is None:
        return
    try:
        pt.delete_record(client, handle, record)
    except Exception:  # noqa: BLE001 — the record expires on its own TTL
        logger.warning("[aauth] could not delete pending record", exc_info=True)


# --------------------------------------------------------------------------- #
# The exchange (draft §Auth Token Endpoint) and its polls
# --------------------------------------------------------------------------- #
def _problem_error(body: bytes) -> str:
    return pt._problem_error(body)


def _accept(resp: egress.CappedResponse, *, endpoint: str, meta: person_server.PersonServerMetadata,
            resource: str, agent_jkt: str, presented_sub: str, presented_exp: int,
            now: int, as_url: Optional[str] = None) -> auth_token.ExchangeOutcome:
    """Map one PS answer (to the POST or to a poll) to an exchange outcome.

    ``as_url`` is the verified resource token's access server (four-party,
    ent#928): the auth token must then be the AS's, not the PS's.
    """
    status = resp.status
    if status == 200:
        try:
            doc = json.loads(resp.body.decode("utf-8"))
            if not isinstance(doc, dict):
                raise ValueError("auth token response is not an object")
            held = auth_token.inspect_auth_token(doc.get("auth_token"), resource=resource, ps=meta.issuer,
                                                 agent_jkt=agent_jkt, presented_sub=presented_sub,
                                                 presented_exp=presented_exp, now=now,
                                                 expires_in=doc.get("expires_in"), expected_iss=as_url)
        except (ValueError, UnicodeDecodeError) as exc:
            return auth_token.ExchangeRefused("auth_token_invalid",
                                              f"the auth token failed the held-token checks: {exc}", 200)
        return auth_token.AuthTokenIssued(held)
    if status == 202:
        return _pending(resp, endpoint=endpoint, meta=meta)
    if status == 401:
        return auth_token.ExchangeRefused("signature_rejected", "the person server rejected the request signature", 401)
    if status == 403:
        err = _problem_error(resp.body)
        code = err if err in _DENIAL_CODES else "denied"
        return auth_token.ExchangeRefused(code, f"the person server refused: {code}", 403)
    if status in _POLL_CODES:
        return auth_token.ExchangeRefused(_POLL_CODES[status], f"the person server answered HTTP {status}", status)
    if status == 400:
        err = _problem_error(resp.body)
        return auth_token.ExchangeRefused(err if err != "unparseable" else "ps_error",
                                          f"the person server rejected the exchange ({err})", 400)
    if status == 502:
        return auth_token.ExchangeRefused("as_unreachable", "the person server could not reach the access server", 502)
    return auth_token.ExchangeRefused("ps_error", f"the person server answered HTTP {status}", status)


def _pending(resp: egress.CappedResponse, *, endpoint: str,
             meta: person_server.PersonServerMetadata) -> auth_token.ExchangeOutcome:
    header = resp.headers.get("aauth-requirement")
    url = code = None
    name = "approval"
    if header is not None:
        try:
            name, params = requirement.parse_aauth_requirement(header)
        except requirement.RequirementFormatError as exc:
            return auth_token.ExchangeRefused("ps_protocol_error", f"AAuth-Requirement is malformed: {exc}", 202)
        if name == "interaction":
            url, code = params.get("url"), requirement.normalize_code(params.get("code"))
            if not requirement.interaction_url_ok(url) or code is None:
                return auth_token.ExchangeRefused("ps_protocol_error", "the interaction url or code is not acceptable", 202)
            if person_server._origin(url) not in meta.interaction_origins():
                return auth_token.ExchangeRefused("ps_protocol_error", "the interaction url is not on the person server", 202)
        elif name != "approval":
            return auth_token.ExchangeRefused("unsupported_requirement",
                                              f"the person server requires '{name}', which this client cannot satisfy", 202)
    location = resp.headers.get("location")
    pending_url = urljoin(endpoint, location.strip()) if location else endpoint
    if not discovery._same_origin(pending_url, meta.issuer) or \
            not requirement.interaction_url_ok(pending_url.split("?", 1)[0]):
        return auth_token.ExchangeRefused("ps_protocol_error", "the pending Location is not on the person server", 202)
    return auth_token.ExchangePending(requirement=name, pending_url=pending_url,
                                      retry_after=requirement.parse_retry_after(resp.headers.get("retry-after")),
                                      url=url, code=code, status=pt._body_status(resp.body))


async def exchange_auth_token(agent_name: str, *, resource: str, resource_token: resource_token.ResourceToken,
                              presented, justification: Optional[str], wait: int, now: int,
                              client_factory: Optional[Callable], key_thumbprint: str) -> auth_token.ExchangeOutcome:
    """POST the resource token + the presented token to the PS; one answer, mapped."""
    ps = config.get_person_server()
    if not ps:
        return auth_token.ExchangeRefused("person_server_unconfigured", "no person server is configured")
    try:
        meta = await person_server.metadata(ps)
    except person_server.PersonServerError as exc:
        return auth_token.ExchangeRefused(exc.code, exc.detail)
    doc: Dict[str, Any] = {"resource_token": resource_token.token, "presented_token": presented.token,
                           "capabilities": list(CAPABILITIES)}
    if justification:
        doc["justification"] = justification
    body = json.dumps(doc, separators=(",", ":")).encode("utf-8")
    try:
        resp, info = await pt.signed_ps_request(agent_name, "POST", meta.auth_token_endpoint, body=body,
                                                content_type="application/json", wait=wait, now=now,
                                                client_factory=client_factory)
    except egress.EgressError as exc:
        return auth_token.ExchangeRefused("person_server_unreachable", exc.detail)
    except asyncio.TimeoutError:
        return auth_token.ExchangeRefused("person_server_unreachable", "the person server did not answer in time")
    if info.thumbprint != key_thumbprint:
        return auth_token.ExchangeRefused("key_rotated", "the agent key rotated under the exchange")
    return _accept(resp, endpoint=meta.auth_token_endpoint, meta=meta, resource=resource, agent_jkt=key_thumbprint,
                   presented_sub=presented.sub, presented_exp=presented.exp, now=now,
                   as_url=resource_token.as_url)


async def poll_exchange(agent_name: str, record: Dict[str, Any], *, wait: int, now: int,
                        client_factory: Optional[Callable], key_thumbprint: str) -> auth_token.ExchangeOutcome:
    """One signed GET of a pending exchange's URL (draft §Deferred Responses)."""
    ps = record.get("ps_issuer")
    try:
        meta = await person_server.metadata(ps)
    except person_server.PersonServerError as exc:
        return auth_token.ExchangeRefused(exc.code, exc.detail)
    try:
        resp, info = await pt.signed_ps_request(agent_name, "GET", record["pending_url"], body=b"", content_type=None,
                                                wait=wait, now=now, client_factory=client_factory)
    except egress.EgressError as exc:
        return auth_token.ExchangeRefused("person_server_unreachable", exc.detail)
    except asyncio.TimeoutError:
        return auth_token.ExchangeRefused("person_server_unreachable", "the person server did not answer in time")
    if info.thumbprint != key_thumbprint:
        return auth_token.ExchangeRefused("key_rotated", "the agent key rotated under the pending exchange")
    as_url = record.get("as_url")
    return _accept(resp, endpoint=record["pending_url"], meta=meta, resource=record["resource"],
                   agent_jkt=key_thumbprint, presented_sub=record.get("presented_sub", ""),
                   presented_exp=int(record.get("presented_exp") or 0), now=now,
                   as_url=as_url if isinstance(as_url, str) and as_url else None)
