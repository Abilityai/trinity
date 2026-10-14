"""Pinned, capped egress for the person-server leg (ent#878, taste T5).

``a2a_client._read_capped`` serves the A2A hop: it refuses every ≥400 unread
and returns bytes only. The person-token protocol is driven by status codes
and headers — ``AAuth-Requirement``, ``Location``, ``Retry-After``,
``Signature-Error`` on 202 / 401 / 403 / 408 / 410 / 429 — so this sibling
returns **every** status with its headers and a capped body, and keeps the
same guards, in the same order, as the A2A reader (the ``discovery.py``
precedent of importing the primitives rather than copying the loop):

* the target is validated and the connection pinned to an approved address,
  with the registered host carried for SNI and ``Host``;
* a 3xx is a failure, never a hop;
* any ``Content-Encoding`` other than identity is refused, not decoded;
* a declared or streamed body over ``max_bytes`` is refused on the WIRE size;
* a transport error is scrubbed of the agent token before it becomes text.

``tests/unit/test_ent878_aauth_person_token.py`` pins each guard.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, Mapping, Optional

import httpx

logger = logging.getLogger(__name__)


class EgressError(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class CappedResponse:
    status: int
    headers: Mapping[str, str]       # lower-case names; repeated fields joined with ", "
    body: bytes


async def request_capped(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    max_bytes: int,
    headers: Dict[str, str],
    content: Optional[bytes] = None,
    secret: Optional[str] = None,
    timeout: Optional[httpx.Timeout] = None,
    peer: str = "person server",
) -> CappedResponse:
    """One pinned request to the LOGICAL ``url``; every status comes back.

    ``secret`` is scrubbed from any transport error text (h11 echoes an illegal
    header value verbatim, and for a signed request that value is the token).
    ``peer`` names the party in error text (ent#879 dials resources too).
    """
    from services import a2a_client
    from services.a2a_client import A2ACallError

    try:
        validated = await a2a_client.validate_endpoint(url)
    except A2ACallError as exc:
        raise EgressError(exc.reason, exc.detail) from None
    pinned = a2a_client._pinned_url(url, validated.addresses[0])
    request_headers = {
        "Host": a2a_client._host_header(validated),
        "Accept-Encoding": "identity",
        "User-Agent": a2a_client._USER_AGENT,
        **headers,
    }
    try:
        async with client.stream(
            method.upper(),
            pinned,
            headers=request_headers,
            content=content,
            extensions={"sni_hostname": validated.hostname},
            **({"timeout": timeout} if timeout is not None else {}),
        ) as resp:
            if 300 <= resp.status_code < 400:
                raise EgressError(
                    "redirect",
                    f"The {peer} returned a redirect. Redirects are refused: a "
                    "validated destination that redirects is an SSRF bypass, not a hop.",
                )
            encoding = (resp.headers.get("content-encoding") or "").strip().lower()
            if encoding and encoding != "identity":
                raise EgressError("encoding", f"The {peer} returned a compressed response; refused rather than decoded.")
            declared = resp.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise EgressError("too_large", f"The {peer} response exceeds the {max_bytes}-byte ceiling.")
            chunks = []
            total = 0
            async for chunk in resp.aiter_raw():
                total += len(chunk)
                if total > max_bytes:
                    raise EgressError("too_large", f"The {peer} response exceeds the {max_bytes}-byte ceiling.")
                chunks.append(chunk)
            return CappedResponse(
                status=resp.status_code,
                headers={k.lower(): v for k, v in resp.headers.items()},
                body=b"".join(chunks),
            )
    except EgressError:
        raise
    except httpx.TimeoutException:
        raise EgressError("timeout", f"The {peer} timed out.") from None
    except httpx.HTTPError as exc:
        detail = str(exc)
        if secret:
            detail = a2a_client.scrub_secret_and_urls(detail, secret)
        detail = a2a_client.redact_url_userinfo(a2a_client.sanitize_text(detail))
        raise EgressError("unreachable", f"The {peer} could not be reached: {detail}") from None
