"""Outbound A2A protocol client (#736) — the one place Trinity dials a peer.

FastAPI-free by construction: it raises `A2ACallError` carrying a stable
`reason`, and `routers/a2a.py` maps that 1:1 to HTTP (Invariant #1). It never
raises anything carrying the endpoint credential.

────────────────────────────────────────────────────────────────────────────
Why the fetcher lives in the backend and not in the agent container
────────────────────────────────────────────────────────────────────────────
A third placement was available and is worth naming, because a decision that is
never stated reads later as an accident: the call could have been made from
inside the agent container, which would put the egress on the agent network and
away from the platform network entirely. It is still the backend, for three
reasons that are all about *what has to happen around the fetch*: the credential
is an AES-256-GCM envelope only the backend can open (Invariant #12); the
response has to be credential-sanitised before an LLM sees it; and the audit row
is a Python write. Handing an agent container the decryption key to move the
socket one network over is a bad trade.

The MCP server is likewise excluded: Invariant #13 makes it a proxy over the
backend API, the SSRF controls are Python, and giving the Node process a
plaintext credential would break the property `tools/a2a.ts` documents as a
design guarantee ("no tool echoes a secret back").

`a2a-python` (the reference SDK) is not used: we need exactly two methods, and
the part that matters — the SSRF/pinning/cap path — is the part we must own
rather than inherit.

────────────────────────────────────────────────────────────────────────────
The egress controls, and why each one is here
────────────────────────────────────────────────────────────────────────────
* **One resolution, one pin, both hops.** The card GET and the RPC POST connect
  to an address `validate_a2a_endpoint_url` approved, presenting the registered
  hostname for `Host`, SNI and certificate verification. Two fetches means two
  egress paths, and a control applied to one of them is a control with a hole.
* **No redirects, at all.** `follow_redirects=False`; a 3xx is a failure. The
  bounded re-validated redirect loops elsewhere in this codebase (Slack,
  WhatsApp) exist because those vendors genuinely 302 to their CDNs. A2A has no
  such requirement, and "no redirects" is strictly safer than "3 validated ones".
* **`trust_env=False`.** Every other control reasons about the *target* IP; an
  `HTTPS_PROXY` in the environment makes the target irrelevant because the
  socket goes to the proxy. This also disables httpx's `SSL_CERT_FILE` /
  `SSL_CERT_DIR` handling, which would silently break TLS-inspecting-proxy
  installs — so the CA context is rebuilt explicitly below, honouring those two
  variables and nothing else.
* **Wire-byte ceilings over `aiter_raw()`**, and any `Content-Encoding` other
  than `identity` refused outright rather than decoded (measured elsewhere in
  this codebase: a 199 KiB gzip body inflating ~1030:1 to 458 MB).
* **A total wall-clock deadline.** httpx's `read` timeout is per-read, so a
  tarpit trickling one byte at a time resets it forever while staying under the
  byte cap. Only a deadline bounds that. It wraps genuinely cancellable awaits —
  never a `to_thread` call, where `wait_for` would 504 the caller while the
  socket stayed open and the thread ran on.
* **DNS off the event loop.** `socket.getaddrinfo` is synchronous; on a per-call
  agent path a host whose nameserver stalls freezes an entire worker's loop,
  which is a far cheaper denial of service than holding one coroutine and which
  no per-agent rate limit bounds.
"""
from __future__ import annotations

import asyncio
import logging
import os
import ssl
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

import httpx

from services import a2a_protocol
from services.a2a_protocol import Dialect, UnsupportedProtocolVersion
from utils.credential_sanitizer import (
    redact_url_userinfo,
    sanitize_text,
    scrub_secret_and_urls,
)
from utils.url_validation import (
    A2AEndpointUrlError,
    ValidatedPublicUrl,
    validate_a2a_endpoint_url,
)

logger = logging.getLogger(__name__)

# --- Caps and deadlines (§32.5 FR-9) ---------------------------------------
# Module-level constants, deliberately NOT settings-backed: a knob here would be
# a knob on a security boundary, and the operator control that matters is the
# kill switch. Recorded in requirements so a future reviewer does not "promote"
# them.
A2A_CARD_FETCH_TIMEOUT = 10.0        # seconds, whole card fetch
A2A_RPC_TIMEOUT = 30.0               # seconds; strictly below the MCP client's
                                     # own 30-60s gateway abort (see H4 below)
A2A_CONNECT_TIMEOUT = 10.0
A2A_DNS_TIMEOUT = 5.0                # budget for the off-loop getaddrinfo
A2A_TOTAL_DEADLINE = 45.0            # wall clock for card + RPC together
A2A_CARD_MAX_BYTES = 256 * 1024
A2A_RPC_MAX_BYTES = a2a_protocol.MAX_RPC_BODY_BYTES     # 1 MiB, same as inbound
A2A_MAX_MESSAGE_CHARS = 100_000
A2A_MAX_RESPONSE_CHARS = 32 * 1024   # what the agent's context window can afford

#: How long a negotiated dialect stays cached per origin. Without this every
#: call AND every `get_a2a_task` poll pays a second full egress just to re-read
#: one field from the card — and a poll would burn the caller's own rate budget
#: doing it.
A2A_DIALECT_CACHE_TTL = 300.0

_USER_AGENT = "Trinity-A2A-Client/1"


class A2ACallError(Exception):
    """A refused or failed outbound call, carrying a stable machine-readable reason.

    `reason` — not the message — is what the router maps to HTTP and what the
    tool surfaces to the agent. Matching on prose is how a status mapping and
    its error text drift apart.
    """

    def __init__(self, reason: str, detail: str, *, remote_code: Optional[int] = None):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.remote_code = remote_code


@dataclass
class A2AResult:
    """The allowlisted shape returned to the caller. Never the raw response."""

    state: str
    text: Optional[str] = None
    task_id: Optional[str] = None
    context_id: Optional[str] = None
    remote_error: Optional[str] = None
    truncated: bool = False
    protocol_version: str = "0.3"
    #: Host only — never the full URL (it may carry a path/query the operator
    #: considers sensitive, and audit `details` is durable).
    host: str = field(default="")


# ---------------------------------------------------------------------------
# TLS / transport plumbing
# ---------------------------------------------------------------------------

def _build_ssl_context() -> ssl.SSLContext:
    """A CA context that survives `trust_env=False`.

    httpx builds its default context inside `create_ssl_context(..., trust_env)`
    and consults `SSL_CERT_FILE` / `SSL_CERT_DIR` **only when `trust_env` is
    True** — so turning `trust_env` off to kill proxy environment variables also
    silently drops an operator's custom CA bundle, breaking every install behind
    a TLS-inspecting proxy. Rebuilt here so the two concerns are separated: we
    refuse the environment's *proxies*, we still honour its *trust store*.
    """
    cert_file = os.environ.get("SSL_CERT_FILE")
    cert_dir = os.environ.get("SSL_CERT_DIR")
    if cert_file:
        return ssl.create_default_context(cafile=cert_file)
    if cert_dir:
        return ssl.create_default_context(capath=cert_dir)
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001 — a system default beats no TLS at all
        return ssl.create_default_context()


def _http_client(timeout: httpx.Timeout) -> httpx.AsyncClient:
    """The outbound client. Seam for tests (`httpx.MockTransport`).

    Every argument is a control, not a preference:
      * `follow_redirects=False` — a 3xx is a failure, never a hop.
      * `trust_env=False` — proxy env vars would make the validated target
        irrelevant.
      * explicit `verify=` — see `_build_ssl_context`.
    """
    return httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
        trust_env=False,
        verify=_build_ssl_context(),
    )


def _pinned_url(url: str, address: str) -> str:
    """Rewrite `url`'s host to a validated IP, preserving everything else.

    The connection then goes to an address the validator approved, while
    `Host` + `sni_hostname` carry the registered name so TLS still
    authenticates it. IPv6 needs bracket form or the authority is unparseable.

    **The mechanism is a pinned-version dependency, so state it.** httpx passes
    per-request `extensions` through to httpcore, whose connection code reads
    `extensions["sni_hostname"]` and uses it as `server_hostname` for
    `start_tls` — which is what Python's `ssl` verifies the certificate against.
    Verified against the pinned httpx 0.28.1 / httpcore 1.0.9. `httpcore` is NOT
    pinned in `docker/backend/Dockerfile` (only `httpx==0.28.1`, which requires
    `httpcore==1.*`), so a future 1.x that ignored the extension would leave the
    connection pinned but the SNI wrong — TLS would then fail closed against the
    IP's certificate rather than silently connect somewhere unvalidated, which
    is the safe direction. `tests/unit/test_736_a2a_outbound_transport.py` pins
    that WE set it; nothing can pin that httpcore keeps honouring it short of a
    live TLS handshake, so this comment is the record.
    """
    parts = urlsplit(url)
    host = f"[{address}]" if ":" in address else address
    netloc = f"{host}:{parts.port}" if parts.port else host
    return urlunsplit((parts.scheme, netloc, parts.path or "/", parts.query, ""))


def _host_header(validated: ValidatedPublicUrl) -> str:
    """`Host` for a pinned request: the registered name, with its explicit port
    only if the original URL carried one (an added `:443` is legal but changes
    the header a peer sees, and some peers compare it)."""
    parts = urlsplit(validated.url)
    if parts.port:
        return f"{validated.hostname}:{parts.port}"
    return validated.hostname


def _same_origin(a: str, b: str) -> bool:
    """Scheme + host + port equality, normalised the way a browser would.

    Every clause here is load-bearing:
      * default-port equivalence — **Trinity's own card emits no explicit
        port**, so treating `https://h` and `https://h:443` as different would
        make Trinity unreachable by its own rule, breaking #738 federation;
      * case-insensitive host and trailing-dot stripping — `Host.` and `host`
        resolve identically;
      * IPv6 bracket forms compared after normalisation.
    """
    def _key(url: str) -> Optional[Tuple[str, str, int]]:
        try:
            p = urlsplit(url)
        except ValueError:
            return None
        scheme = (p.scheme or "").lower()
        host = (p.hostname or "").lower().rstrip(".")
        if not scheme or not host:
            return None
        try:
            port = p.port
        except ValueError:
            return None
        if port is None:
            port = 443 if scheme == "https" else 80 if scheme == "http" else -1
        return (scheme, host, port)

    ka, kb = _key(a), _key(b)
    return ka is not None and ka == kb


# ---------------------------------------------------------------------------
# Validation (off the event loop)
# ---------------------------------------------------------------------------

async def validate_endpoint(url: str) -> ValidatedPublicUrl:
    """`validate_a2a_endpoint_url` without blocking the event loop.

    `socket.getaddrinfo` is synchronous and can hang for the resolver's own
    timeout. On an admin settings write that is tolerable; on a per-call agent
    path it freezes every other request on the worker — a far cheaper denial of
    service than holding one coroutine, and one that no per-agent rate limit
    bounds.

    Two residuals, stated because `wait_for` around `to_thread` looks like a
    deadline and is not one:

    * **The thread is not cancelled.** On timeout this raises and the caller
      gets a clean refusal, but the worker thread keeps sitting in
      `getaddrinfo` until the resolver gives up. That is why the budget is
      small, and why this await is deliberately NOT the call's total deadline
      (`_with_deadline` wraps genuinely cancellable network awaits instead).
    * **The default executor is bounded** (`min(32, cpu+4)` threads). A flood
      against a stalling resolver exhausts it, after which further calls queue
      and time out *here* — a refusal, not a blocked loop. Fail-closed, and the
      per-agent + fleet rate bounds run before this is ever reached.
    """
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(validate_a2a_endpoint_url, url),
            timeout=A2A_DNS_TIMEOUT,
        )
    except asyncio.TimeoutError:
        raise A2ACallError(
            "endpoint_dns_failure",
            "Resolving the A2A endpoint hostname timed out.",
        ) from None
    except A2AEndpointUrlError as exc:
        raise A2ACallError(exc.reason, str(exc)) from None
    except ValueError as exc:
        raise A2ACallError("endpoint_invalid", str(exc)) from None


# ---------------------------------------------------------------------------
# Capped, pinned fetch
# ---------------------------------------------------------------------------

async def _read_capped(
    client: httpx.AsyncClient,
    method: str,
    pinned_url: str,
    *,
    sni: str,
    host_header: str,
    max_bytes: int,
    headers: Dict[str, str],
    content: Optional[bytes] = None,
    error_prefix: str,
) -> bytes:
    """Issue one pinned request and read the body under a hard WIRE-byte ceiling.

    `aiter_raw()`, not `aiter_bytes()`: the latter yields DECODED chunks, so a
    body whose wire size passes the ceiling can inflate past it before the
    running total is ever consulted. `Accept-Encoding: identity` is the polite
    half and cannot bind a hostile server, which is why any `Content-Encoding`
    is refused outright rather than accommodated.
    """
    request_headers = {
        "Host": host_header,
        "Accept-Encoding": "identity",
        "User-Agent": _USER_AGENT,
        **headers,
    }
    try:
        async with client.stream(
            method,
            pinned_url,
            headers=request_headers,
            content=content,
            extensions={"sni_hostname": sni},
        ) as resp:
            if 300 <= resp.status_code < 400:
                raise A2ACallError(
                    f"{error_prefix}_redirect",
                    "The A2A endpoint returned a redirect. Redirects are refused: a "
                    "validated destination that redirects is an SSRF bypass, not a hop.",
                )
            encoding = (resp.headers.get("content-encoding") or "").strip().lower()
            if encoding and encoding != "identity":
                raise A2ACallError(
                    f"{error_prefix}_encoding",
                    "The A2A endpoint returned a compressed response; refused rather "
                    "than decoded.",
                )
            declared = resp.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise A2ACallError(
                    f"{error_prefix}_too_large",
                    f"The A2A endpoint response exceeds the {max_bytes}-byte ceiling.",
                )
            if resp.status_code >= 400:
                # An HTTP-level failure. The body is NOT read: it is peer-
                # controlled, unbounded until we cap it, and carries nothing we
                # act on (A2A errors ride in a 200 body — see `send_message`).
                raise A2ACallError(
                    f"{error_prefix}_http_error",
                    f"The A2A endpoint returned HTTP {resp.status_code}.",
                )

            chunks = []
            total = 0
            async for chunk in resp.aiter_raw():
                total += len(chunk)
                if total > max_bytes:
                    raise A2ACallError(
                        f"{error_prefix}_too_large",
                        f"The A2A endpoint response exceeds the {max_bytes}-byte ceiling.",
                    )
                chunks.append(chunk)
            return b"".join(chunks)
    except A2ACallError:
        raise
    except httpx.TimeoutException:
        raise A2ACallError("timeout", "The A2A endpoint timed out.") from None
    except httpx.HTTPError as exc:
        # `exc` may echo the URL; it never carries the credential (that is a
        # header), but scrub userinfo anyway — the URL could have come from a
        # future path that did not reject it.
        raise A2ACallError(
            f"{error_prefix}_unreachable",
            f"The A2A endpoint could not be reached: {redact_url_userinfo(str(exc))}",
        ) from None


# ---------------------------------------------------------------------------
# Dialect cache
# ---------------------------------------------------------------------------

#: origin → (stored_at, dialect_version, resolved_rpc_url)
#:
#: The rpc target is cached WITH the dialect, not derived again on a cache hit.
#: Deriving it would be wrong whenever the operator registered the ORIGIN rather
#: than a path: the first call learns the real endpoint from the card's `url`
#: (e.g. `/a2a/bot`), and a later poll re-deriving from the registered URL would
#: POST to `/` — a different endpoint, silently. The two values are learned from
#: the same card read, so they expire together.
_dialect_cache: Dict[str, Tuple[float, str, str]] = {}


def _cached_target(origin: str) -> Optional[Tuple[Dialect, str]]:
    """`(dialect, rpc_url)` learned from a recent card read, or None."""
    entry = _dialect_cache.get(origin)
    if not entry:
        return None
    stored_at, version, rpc_url = entry
    if time.monotonic() - stored_at > A2A_DIALECT_CACHE_TTL:
        _dialect_cache.pop(origin, None)
        return None
    if version != "0.3":
        # Only v0.3 is claimed; an unrecognised cached version is a miss rather
        # than a guess.
        return None
    return a2a_protocol.DIALECT_V03, rpc_url


def _cache_target(origin: str, dialect: Dialect, rpc_url: str) -> None:
    _dialect_cache[origin] = (time.monotonic(), dialect.version, rpc_url)


def clear_dialect_cache() -> None:
    """Drop the negotiated dialect + target cache (tests; any future admin action)."""
    _dialect_cache.clear()


# ---------------------------------------------------------------------------
# Card
# ---------------------------------------------------------------------------

def _card_url_for(validated: ValidatedPublicUrl) -> str:
    """`{scheme}://{netloc}/.well-known/agent-card.json` — origin only.

    Deriving from the ORIGIN and never from the registered path is the F5 rule:
    the registry field is documented as "endpoint **or** Agent Card URL", so a
    registered URL may legitimately carry a path, and appending `/.well-known/…`
    to it would fetch a different agent's card without saying so.
    """
    parts = urlsplit(validated.url)
    return urlunsplit((parts.scheme, parts.netloc, "/.well-known/agent-card.json", "", ""))


async def fetch_card(
    client: httpx.AsyncClient, validated: ValidatedPublicUrl
) -> Dict[str, Any]:
    """Fetch the peer's Agent Card. **Uncredentialed**, pinned, capped.

    The card fetch carries no credential — mirroring the rule the Slack and
    WhatsApp media fetchers apply to a followed hop, applied here to the one hop
    we make before we know anything about the peer.
    """
    import json

    address = validated.addresses[0]
    raw = await _read_capped(
        client,
        "GET",
        _pinned_url(_card_url_for(validated), address),
        sni=validated.hostname,
        host_header=_host_header(validated),
        max_bytes=A2A_CARD_MAX_BYTES,
        headers={"Accept": "application/json"},
        error_prefix="card",
    )
    try:
        card = json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001
        raise A2ACallError(
            "card_invalid",
            "The A2A endpoint's agent card is not valid JSON.",
        ) from None
    if not isinstance(card, dict):
        raise A2ACallError("card_invalid", "The A2A endpoint's agent card is not an object.")
    return card


def resolve_rpc_target(validated: ValidatedPublicUrl, card: Dict[str, Any]) -> str:
    """Decide where the credentialed POST goes. The card is a HINT, not an authority.

    Two rules, and the ordering matters:

    1. **Same-origin pin.** A card-declared `url` on a different origin is
       refused outright and logged at ERROR. This is the single control that
       stops a hostile card redirecting a credentialed POST — and it is what
       lets #736 ship while ent#159 (signed cards) is blocked, because it
       removes the card's authority rather than trying to verify it. A
       signature scheme whose own scope is "validate when signed, warn when
       unsigned" cannot be the boundary for a credentialed fetch: an attacker
       just does not sign.
    2. **Path disambiguation (F5).** If the registered URL carries a path, it is
       accepted as the RPC target only when the card's declared `url` matches it
       exactly. Otherwise there are two candidate targets and no principled way
       to pick, so the ambiguity is refused by name instead of resolved by luck.
    """
    declared = card.get("url")
    parts = urlsplit(validated.url)
    registered_path = (parts.path or "").rstrip("/")

    if isinstance(declared, str) and declared.strip():
        # `_same_origin` compares `hostname`, which STRIPS userinfo — so
        # `https://u:p@peer.example.com/a2a` would compare equal to
        # `https://peer.example.com/a2a`. `_pinned_url` happens to drop the
        # userinfo when it rebuilds the authority, so nothing leaks today, but
        # that is a coincidence of one helper rather than a decision. A card
        # declaring credentials in its own URL is anomalous; refuse it by name
        # instead of relying on a downstream accident.
        declared_parts = urlsplit(declared.strip())
        if declared_parts.username or declared_parts.password or "@" in (declared_parts.netloc or ""):
            logger.error(
                "[a2a_client] card for %s declares a url embedding credentials; refusing",
                validated.hostname,
            )
            raise A2ACallError(
                "card_url_invalid",
                "The A2A endpoint's agent card declares a url embedding credentials. "
                "Refused.",
            )
        if not _same_origin(declared, validated.url):
            logger.error(
                "[a2a_client] card for %s declares a cross-origin url; refusing",
                validated.hostname,
            )
            raise A2ACallError(
                "card_origin_mismatch",
                "The A2A endpoint's agent card points at a different origin than the "
                "registered endpoint. Refused — a card cannot redirect a credentialed "
                "call.",
            )
        if registered_path and urlsplit(declared).path.rstrip("/") != registered_path:
            raise A2ACallError(
                "card_url_ambiguous",
                "The registered A2A endpoint URL carries a path that the peer's card "
                "does not declare. Register the endpoint's origin, or a URL matching "
                "the card's declared url exactly.",
            )
        return declared.strip()

    if registered_path:
        # No declared url and a registered path: the operator named a specific
        # endpoint and the card offered no opinion. Honour the operator.
        return validated.url
    return urlunsplit((parts.scheme, parts.netloc, "/", "", ""))


# ---------------------------------------------------------------------------
# Response sanitisation
# ---------------------------------------------------------------------------

def sanitize_outbound_text(text: Optional[str], credential: Optional[str]) -> Tuple[Optional[str], bool]:
    """Redact, then truncate. **In that order, over a 2x window.**

    Three layers, because the remote controls this text:

    1. `scrub_secret_and_urls(text, credential)` — EXACT-VALUE redaction of the
       resolved credential. This is the load-bearing one and the reason
       `sanitize_text` alone is not enough: `sanitize_text` matches *patterns*
       (`sk-`, `ghp_`, `trinity_mcp_`, `Bearer …`), and a partner's credential is
       an arbitrary operator-supplied string that matches none of them. A
       credential-leak test written with a `trinity_mcp_`-shaped secret exercises
       only the case that already worked.
    2. `sanitize_text` — the platform patterns, for anything else the peer
       echoed.
    3. `redact_url_userinfo` — a URL in the body carrying userinfo.

    Then truncation, and **sanitisation runs over `text[:2*cap]` before the
    `[:cap]` slice**: a bare slice can cut a secret in half so the redaction
    pattern no longer matches, publishing the surviving prefix. The 2x window
    bounds the work while guaranteeing that anything landing near the boundary
    was seen whole.

    **Not fixable here, and stated in the docs instead:** a cooperating remote
    that base64s, rot13s or splits the credential defeats exact-value redaction.
    Registering an endpoint grants that endpoint the ability to exfiltrate its
    own credential.
    """
    if not text:
        return text, False
    window = text[: A2A_MAX_RESPONSE_CHARS * 2]
    cleaned = scrub_secret_and_urls(window, credential or "")
    cleaned = sanitize_text(cleaned)
    cleaned = redact_url_userinfo(cleaned)
    truncated = len(text) > A2A_MAX_RESPONSE_CHARS or len(cleaned) > A2A_MAX_RESPONSE_CHARS
    if len(cleaned) > A2A_MAX_RESPONSE_CHARS:
        cleaned = cleaned[:A2A_MAX_RESPONSE_CHARS] + "\n…[truncated by Trinity]"
    elif truncated:
        cleaned = cleaned + "\n…[truncated by Trinity]"
    return cleaned, truncated


# ---------------------------------------------------------------------------
# The two RPC calls
# ---------------------------------------------------------------------------

def _parse_task(payload: Any) -> Tuple[str, Optional[str], Optional[str], Optional[str]]:
    """`(state, text, task_id, context_id)` from an A2A result. Tolerant by contract.

    Every field is peer-controlled, so a shape we do not recognise yields
    `unknown` rather than an exception — a malformed success must not become a
    500 on our side.
    """
    if not isinstance(payload, dict):
        return "unknown", None, None, None
    status = payload.get("status") if isinstance(payload.get("status"), dict) else {}
    state = status.get("state") if isinstance(status.get("state"), str) else None
    task_id = payload.get("id") if isinstance(payload.get("id"), str) else None
    context_id = payload.get("contextId") if isinstance(payload.get("contextId"), str) else None

    texts = []
    artifacts = payload.get("artifacts")
    if isinstance(artifacts, list):
        for artifact in artifacts:
            chunk = a2a_protocol.text_from_parts(artifact)
            if chunk:
                texts.append(chunk)
    status_message = status.get("message")
    if isinstance(status_message, dict):
        chunk = a2a_protocol.text_from_parts(status_message)
        if chunk:
            texts.append(chunk)
    # A bare `message` result (a peer answering without a Task envelope).
    if not texts and payload.get("kind") == "message":
        chunk = a2a_protocol.text_from_parts(payload)
        if chunk:
            texts.append(chunk)
            state = state or "completed"

    return state or "unknown", ("\n".join(texts) or None), task_id, context_id


async def _rpc(
    client: httpx.AsyncClient,
    validated: ValidatedPublicUrl,
    rpc_url: str,
    credential: Optional[str],
    method: str,
    params: Dict[str, Any],
) -> Dict[str, Any]:
    """One credentialed JSON-RPC POST, pinned + capped. Returns the parsed body."""
    import json

    address = validated.addresses[0]
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if credential:
        headers["Authorization"] = f"Bearer {credential}"
    envelope = a2a_protocol.build_request(uuid.uuid4().hex, method, params)

    raw = await _read_capped(
        client,
        "POST",
        _pinned_url(rpc_url, address),
        sni=validated.hostname,
        host_header=_host_header(validated),
        max_bytes=A2A_RPC_MAX_BYTES,
        headers=headers,
        content=json.dumps(envelope).encode("utf-8"),
        error_prefix="rpc",
    )
    try:
        body = json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001
        raise A2ACallError("rpc_invalid", "The A2A endpoint returned a non-JSON body.") from None
    if not isinstance(body, dict):
        raise A2ACallError("rpc_invalid", "The A2A endpoint returned a non-object body.")
    return body


def _raise_for_rpc_error(body: Dict[str, Any], credential: Optional[str]) -> Dict[str, Any]:
    """Surface a JSON-RPC error that arrived on **HTTP 200**.

    A2A carries errors in the body with a 200 transport status — Trinity's own
    inbound server does exactly this. A client that checks only the status code
    reads every remote failure as a success and hands the agent an error object
    as if it were an answer. This is the single most important line in the file
    for correctness, and it has its own test.
    """
    error = body.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        code = error.get("code") if isinstance(error.get("code"), int) else None
        text, _ = sanitize_outbound_text(
            str(message) if message is not None else "The A2A endpoint reported an error.",
            credential,
        )
        raise A2ACallError("remote_error", text or "The A2A endpoint reported an error.",
                           remote_code=code)
    result = body.get("result")
    if result is None:
        raise A2ACallError(
            "rpc_invalid",
            "The A2A endpoint returned neither a result nor an error.",
        )
    return result


async def call_endpoint(
    *,
    endpoint_url: str,
    credential: Optional[str],
    message: str,
    context_id: Optional[str] = None,
    task_id: Optional[str] = None,
    client_factory=None,
    validated: Optional[ValidatedPublicUrl] = None,
) -> A2AResult:
    """Send one message to a registered A2A endpoint and return its answer.

    The whole call — validation, card fetch and RPC — runs under one wall-clock
    deadline wrapping cancellable awaits.

    `validated` lets the caller hand in an endpoint it has ALREADY resolved and
    approved. That is not an optimisation: resolving twice means the address the
    caller vetted and the address the socket connects to came from two different
    `getaddrinfo` calls, which is the TOCTOU window the pin exists to close. The
    orchestration service always passes it; the parameter stays optional so this
    module is usable standalone (and in tests) without a two-step dance.
    """
    if len(message or "") > A2A_MAX_MESSAGE_CHARS:
        raise A2ACallError(
            "message_too_long",
            f"Message exceeds the {A2A_MAX_MESSAGE_CHARS}-character outbound cap.",
        )

    async def _run() -> A2AResult:
        endpoint = validated or await validate_endpoint(endpoint_url)
        timeout = httpx.Timeout(A2A_RPC_TIMEOUT, connect=A2A_CONNECT_TIMEOUT)
        factory = client_factory or _http_client
        async with factory(timeout) as client:
            origin = f"{endpoint.hostname}:{endpoint.port}"
            card = await fetch_card(client, endpoint)
            try:
                dialect = a2a_protocol.resolve_dialect(card.get("protocolVersion"))
            except UnsupportedProtocolVersion as exc:
                raise A2ACallError("unsupported_protocol_version", str(exc)) from None
            rpc_url = resolve_rpc_target(endpoint, card)
            _cache_target(origin, dialect, rpc_url)

            params = {
                "message": a2a_protocol.text_message(
                    message, uuid.uuid4().hex, context_id=context_id, task_id=task_id
                )
            }
            body = await _rpc(
                client, endpoint, rpc_url, credential, dialect.send_message, params
            )
            result = _raise_for_rpc_error(body, credential)
            state, text, remote_task_id, remote_context_id = _parse_task(result)
            clean, truncated = sanitize_outbound_text(text, credential)
            return A2AResult(
                state=state,
                text=clean,
                task_id=remote_task_id,
                context_id=remote_context_id or context_id,
                truncated=truncated,
                protocol_version=dialect.version,
                host=endpoint.hostname,
            )

    return await _with_deadline(_run())


async def get_task(
    *,
    endpoint_url: str,
    credential: Optional[str],
    task_id: str,
    client_factory=None,
    validated: Optional[ValidatedPublicUrl] = None,
) -> A2AResult:
    """Poll a remote task by id (`tasks/get`) on the same resolved endpoint.

    This is what makes the aggressive `A2A_RPC_TIMEOUT` safe: without it, any
    remote task exceeding 30s would be unrecoverable and the agent would hold an
    id it could do nothing with. The two are one decision, not two.
    """
    async def _run() -> A2AResult:
        endpoint = validated or await validate_endpoint(endpoint_url)
        timeout = httpx.Timeout(A2A_RPC_TIMEOUT, connect=A2A_CONNECT_TIMEOUT)
        factory = client_factory or _http_client
        async with factory(timeout) as client:
            origin = f"{endpoint.hostname}:{endpoint.port}"
            cached = _cached_target(origin)
            if cached is None:
                card = await fetch_card(client, endpoint)
                try:
                    dialect = a2a_protocol.resolve_dialect(card.get("protocolVersion"))
                except UnsupportedProtocolVersion as exc:
                    raise A2ACallError("unsupported_protocol_version", str(exc)) from None
                rpc_url = resolve_rpc_target(endpoint, card)
                _cache_target(origin, dialect, rpc_url)
            else:
                # Both values came from ONE card read of this origin, which the
                # same-origin pin already validated. Re-deriving the target here
                # instead would be wrong for an operator who registered the
                # origin: the card named the real endpoint, and a re-derivation
                # would poll `/`.
                dialect, rpc_url = cached

            body = await _rpc(
                client, endpoint, rpc_url, credential, dialect.get_task, {"id": task_id}
            )
            result = _raise_for_rpc_error(body, credential)
            state, text, remote_task_id, remote_context_id = _parse_task(result)
            clean, truncated = sanitize_outbound_text(text, credential)
            return A2AResult(
                state=state,
                text=clean,
                task_id=remote_task_id or task_id,
                context_id=remote_context_id,
                truncated=truncated,
                protocol_version=dialect.version,
                host=endpoint.hostname,
            )

    return await _with_deadline(_run())


async def _with_deadline(coro) -> A2AResult:
    """Bound the whole call on wall clock.

    httpx's `read` timeout is per-read, so a tarpit that trickles one byte every
    few seconds resets it forever and never trips it — and the byte cap does not
    help either, because the attacker simply stays under it. Only a wall-clock
    deadline bounds this shape, and it must wrap awaits that are genuinely
    cancellable (a `wait_for` around `to_thread` would 504 the caller while the
    socket stayed open).
    """
    try:
        return await asyncio.wait_for(coro, timeout=A2A_TOTAL_DEADLINE)
    except asyncio.TimeoutError:
        raise A2ACallError(
            "timeout",
            f"The A2A call exceeded the {A2A_TOTAL_DEADLINE:.0f}s deadline. If the "
            "remote task is long-running, call it again and poll with get_a2a_task.",
        ) from None
