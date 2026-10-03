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
    SCHEME_DEFAULT_PORTS,
    ValidatedPublicUrl,
    canonical_origin_host,
    effective_port,
    validate_a2a_endpoint_url,
)

logger = logging.getLogger(__name__)

# --- Caps and deadlines (§32.5 FR-9) ---------------------------------------
# Module-level constants, deliberately NOT settings-backed: a knob here would be
# a knob on a security boundary, and the operator control that matters is the
# kill switch. Recorded in requirements so a future reviewer does not "promote"
# them.
A2A_CARD_FETCH_TIMEOUT = 10.0        # seconds, whole card fetch (its own
                                     # per-request budget, NOT the client default)
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

# --- x402 payment caps (#3185) ---------------------------------------------
#: Ceiling on the body of a 402/403 — the ONLY statuses whose body we read.
#: Deliberately far below `A2A_RPC_MAX_BYTES`: a refusal carries a price, not a
#: payload, and the 1 MiB answer ceiling would make a hostile "pay me" an
#: amplifier. Reading it at all is a departure from "never read an error body",
#: justified by the one fact that lives nowhere else — what the remote charges.
A2A_ERROR_BODY_MAX_BYTES = 64 * 1024
#: Cap the peer-controlled `payment-required` header before decoding it.
A2A_PAYMENT_HEADER_MAX_CHARS = 32 * 1024
#: Free-text taken from a peer's refusal body.
A2A_MAX_ERROR_TEXT_CHARS = 512
#: Serialized ceiling on the whole `payment` block handed to the agent.
A2A_PAYMENT_BLOCK_MAX_BYTES = 16 * 1024
A2A_PAYMENT_LEAF_MAX_CHARS = 512
A2A_PAYMENT_MAX_ACCEPTS = 8
A2A_PAYMENT_MAX_LEAVES = 64
#: A decoded payload's string leaves are scrubbed from every outbound string,
#: not just the token: a remote echoing the DECODED signature back would
#: otherwise walk straight past exact-value redaction of the base64 token.
A2A_SECRET_LEAF_MIN_CHARS = 16
A2A_SECRET_MAX_LEAVES = 256
A2A_SECRET_MAX_DEPTH = 8

#: Send the token as an HTTP `payment-signature` header **in addition to** the
#: in-band `x402.payment.payload` metadata.
#:
#: The 2026-10-03 design note names the header the DEPRECATED fallback and the
#: metadata the primary carriage. Both ride the SAME request, because a
#: fallback that waits for a 402 would be an automatic retry, which this
#: feature's own acceptance criteria forbid. Two generations of provider SDK
#: are in the field — one reads only the header, one prefers the metadata — and
#: one request satisfies both.
#:
#: **When to remove it:** once the Trinity provider side ships on a payments-py
#: that reads the in-band metadata (abilityai/trinity-enterprise#679 and the
#: pin bump it carries) AND the poll path (`tasks/get`, which has no message to
#: hang metadata on) has another carrier. Flipping this to False before then
#: makes every priced poll fail.
A2A_SEND_PAYMENT_SIGNATURE_HEADER = True

#: The credential kinds `a2a_outbound.ResolvedEndpoint` can carry. Anything
#: else is treated as `api_key` — the fail-SAFE direction: a payment token sent
#: as a Bearer header is refused by the remote, never leaked to a third party.
CREDENTIAL_KIND_PAYMENT_TOKEN = "payment_token"

#: Statuses whose body we read on the RPC hop. Both mean "the peer answered
#: about money", and both are useless without the body.
_PAYMENT_STATUSES = frozenset({402, 403})

#: Top-level keys of an `X402PaymentRequired` object that may reach the agent.
#: An ALLOWLIST, because every value here is peer-controlled text destined for
#: an LLM — an unknown key is dropped, never passed through.
_X402_TOP_LEVEL_KEYS = ("x402Version", "error", "resource", "accepts", "extensions")


class A2ACallError(Exception):
    """A refused or failed outbound call, carrying a stable machine-readable reason.

    `reason` — not the message — is what the router maps to HTTP and what the
    tool surfaces to the agent. Matching on prose is how a status mapping and
    its error text drift apart.
    """

    def __init__(
        self,
        reason: str,
        detail: str,
        *,
        remote_code: Optional[int] = None,
        remote_status: Optional[int] = None,
        payment: Optional[Dict[str, Any]] = None,
        task_id: Optional[str] = None,
    ):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.remote_code = remote_code
        #: The peer's HTTP status, when the refusal came from one. Present on
        #: every `*_http_error` too, so any 4xx/5xx is diagnosable — and it is
        #: what lets a caller tell a 402 ("buy this") from a 403 ("your token
        #: was refused") even when both map to the same reason family (#3185).
        self.remote_status = remote_status
        #: Bounded, scrubbed payment requirements — `payment_required` only.
        self.payment = payment
        #: The remote task id to quote on the follow-up call (a2a-x402 §4.5
        #: requires it), when the refusal arrived in-band on a Task.
        self.task_id = task_id


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
    #: `x402.payment.status` when the peer reported one on a SUCCESSFUL call —
    #: in practice `payment-completed`. Internal: it reaches the activity row
    #: and the audit `details` (money leaving must be visible to the operator)
    #: and deliberately NOT `A2ACallResponse`, whose allowlist does not grow.
    #: Receipts themselves are dropped — out of scope, and no consumer.
    payment_status: Optional[str] = None


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
    # ent#398: through the shared normalisation, so the port this connects to is
    # the same one the validator approved and `_same_origin` compares. A port
    # equal to the scheme default is omitted rather than spelled out — same
    # destination, and it keeps the pinned URL in the form the default-port
    # equivalence rule below is written against.
    port = effective_port(parts.port, parts.scheme)
    default = SCHEME_DEFAULT_PORTS.get((parts.scheme or "").lower())
    netloc = host if (port is None or port == default) else f"{host}:{port}"
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
      * **IP literals compared as addresses, not as text** (ent#399). This clause
        used to read "IPv6 bracket forms compared after normalisation" and was
        false: `canonical_host` leaves a literal untouched (idna rejects it, the
        ASCII fallback passes it through), so `[2606:4700:4700::1111]` and
        `[2606:4700:4700:0:0:0:0:1111]` — one address, two spellings — were
        refused as cross-origin. A docstring asserting a property the code does
        not have is worse than the gap: it is what the next reader builds on.
        `canonical_origin_host` parses a literal through `ipaddress` and compares
        the canonical form, keeping the scope id, so `fe80::1%eth0` and
        `fe80::1%eth1` stay the different destinations they are;
      * **the SAME host canonicalisation the validator used** (`canonical_host`,
        reached through `canonical_origin_host`,
        UTS-46 nontransitional IDNA). The registered URL keeps whatever form the
        operator typed while `ValidatedPublicUrl.hostname` holds the A-label, and
        a peer's card declares whichever form its own server emits — usually the
        A-label. Comparing raw strings therefore refused a perfectly ordinary IDN
        peer as "cross-origin". One normalisation, used by the parser, the
        resolver and this comparison, is the whole point of the rule (SV-7); the
        card comparison was the one place left out of it.
    """
    def _key(url: str) -> Optional[Tuple[str, str, int]]:
        try:
            p = urlsplit(url)
        except ValueError:
            return None
        scheme = (p.scheme or "").lower()
        try:
            raw_host = p.hostname or ""
        except ValueError:
            return None
        # ent#399: literals as addresses, names as before. The textual fallback
        # stays for a host neither path can canonicalise, so an unusual name is
        # still comparable to itself rather than becoming un-callable.
        host = canonical_origin_host(raw_host) or raw_host.lower().rstrip(".")
        if not scheme or not host:
            return None
        try:
            # ent#398: the same normalisation the validator and the pinned URL
            # use. Spelling it a third time here is how `:0` came to mean port 0
            # on this side and 443 on the other two — the registered endpoint
            # validated, would have connected, and was refused card_origin_mismatch
            # forever. `-1` stands for "unknown scheme, no default": it makes two
            # such URLs comparable to each other without ever matching a real port.
            port = effective_port(p.port, scheme)
        except ValueError:
            return None
        if port is None:
            port = -1
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

async def _read_payment_error_and_raise(
    resp: httpx.Response,
    *,
    secrets,
    credential_kind: str,
    max_bytes: int,
) -> None:
    """Read what we safely can off a 402/403 and raise the payment outcome.

    The two refusals the other guards would have issued are DELIBERATELY not
    issued here: a compressed body is skipped (never decoded — that rule is
    absolute) and an oversized one is abandoned mid-stream, and in both cases
    the outcome still goes out, flagged `truncated`. The alternative is the
    defect this exists to fix: the operator reads "the peer sent a compressed
    response" about a peer that is simply charging money.

    The `payment-required` header is read FIRST and is the preferred source, so
    a priced peer behind a gzipping CDN still delivers its price.
    """
    header = resp.headers.get(a2a_protocol.X402_PAYMENT_REQUIRED_HEADER)
    encoding = (resp.headers.get("content-encoding") or "").strip().lower()
    declared = resp.headers.get("content-length")
    identity = (not encoding) or encoding == "identity"
    over_declared = bool(declared and declared.isdigit() and int(declared) > max_bytes)

    body: Optional[bytes] = None
    dropped = True
    if identity and not over_declared:
        chunks = []
        total = 0
        oversized = False
        async for chunk in resp.aiter_raw():
            total += len(chunk)
            if total > max_bytes:
                oversized = True
                break
            chunks.append(chunk)
        if not oversized:
            body = b"".join(chunks)
            dropped = False

    _raise_payment_outcome(
        resp.status_code,
        payment_header=header,
        body=body,
        secrets=secrets,
        credential_kind=credential_kind,
        body_dropped=dropped,
    )


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
    secret: Optional[str] = None,
    secrets=None,
    credential_kind: str = "api_key",
    timeout: Optional[httpx.Timeout] = None,
) -> bytes:
    """Issue one pinned request and read the body under a hard WIRE-byte ceiling.

    `aiter_raw()`, not `aiter_bytes()`: the latter yields DECODED chunks, so a
    body whose wire size passes the ceiling can inflate past it before the
    running total is ever consulted. `Accept-Encoding: identity` is the polite
    half and cannot bind a hostile server, which is why any `Content-Encoding`
    is refused outright rather than accommodated.

    **One exception to "the error body is never read" (#3185):** on the RPC hop
    only, a 402 or 403 carries the one fact that lives nowhere else — what the
    remote charges, or that it refused the token we paid with. Those two
    statuses are classified BEFORE the encoding and length guards, because a
    CDN-gzipped or oversized "pay me" was otherwise reported as `rpc_encoding`
    / `rpc_too_large`: an outage, for an endpoint working perfectly. The body is
    still never decoded and still bounded, by a ceiling 16x tighter than the
    answer cap; when it cannot be read the OUTCOME survives from the status
    alone, flagged `truncated`. The card hop keeps the original contract —
    `error_prefix` is what keys the branch — because an uncredentialed card
    fetch cannot be paid for from here.
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
            **({"timeout": timeout} if timeout is not None else {}),
        ) as resp:
            if 300 <= resp.status_code < 400:
                raise A2ACallError(
                    f"{error_prefix}_redirect",
                    "The A2A endpoint returned a redirect. Redirects are refused: a "
                    "validated destination that redirects is an SSRF bypass, not a hop.",
                )
            # #3185: the payment statuses, on the RPC hop only, before the
            # encoding/length guards. The redirect guard stays ahead of this —
            # an SSRF question outranks a price.
            if error_prefix == "rpc" and resp.status_code in _PAYMENT_STATUSES:
                await _read_payment_error_and_raise(
                    resp,
                    secrets=secrets if secrets is not None else ([secret] if secret else []),
                    credential_kind=credential_kind,
                    max_bytes=min(max_bytes, A2A_ERROR_BODY_MAX_BYTES),
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
                    remote_status=resp.status_code,
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
        # This is the ONE place a foreign string becomes an error the calling
        # LLM reads (through the 502 body) and the backend logs, so it gets the
        # same treatment as the response body — exact-value first.
        #
        # "it never carries the credential (that is a header)" was wrong: h11
        # rejects an illegal header value by ECHOING it
        # (`LocalProtocolError: Illegal header value b'Bearer <token>'`), which
        # is the documented reason `models._validate_pat_secret` exists
        # (ent#109). A stored credential carrying a stray line break — the
        # routine paste artifact — turned a transport error into a credential
        # disclosure. The write path now rejects such a credential; this is the
        # layer that holds when a row was written by some other path.
        # #3185 amendment 6: the SAME secrets list every other error builder
        # uses — the token AND its decoded string leaves. h11 echoes an illegal
        # header value verbatim, and for a payment endpoint that value is the
        # token.
        all_secrets = secrets if secrets is not None else ([secret] if secret else [])
        detail = str(exc)
        for item in all_secrets:
            if item:
                detail = scrub_secret_and_urls(detail, item)
        detail = redact_url_userinfo(sanitize_text(detail))
        raise A2ACallError(
            f"{error_prefix}_unreachable",
            f"The A2A endpoint could not be reached: {detail}",
        ) from None


# ---------------------------------------------------------------------------
# Dialect cache
# ---------------------------------------------------------------------------

#: REGISTERED ENDPOINT URL → (stored_at, dialect_version, resolved_rpc_url)
#:
#: The rpc target is cached WITH the dialect, not derived again on a cache hit.
#: Deriving it would be wrong whenever the operator registered the ORIGIN rather
#: than a path: the first call learns the real endpoint from the card's `url`
#: (e.g. `/a2a/bot`), and a later poll re-deriving from the registered URL would
#: POST to `/` — a different endpoint, silently. The two values are learned from
#: the same card read, so they expire together.
#:
#: **Keyed on the registered URL, NOT on the origin.** One host can carry several
#: separately-registered endpoints — a multi-tenant peer with an agent per path is
#: exactly what the registered-path rule exists to support — and those are
#: different trust relationships with different credentials. Under an origin key,
#: a call to `https://host/a2a/alice` populated the entry that a poll for
#: `https://host/a2a/bob` then read, so Bob's poll went to Alice's URL carrying
#: **Bob's credential**. The registered URL is what `resolve_rpc_target` derives
#: from, so it is the only key under which the cached answer is the same answer.
_dialect_cache: Dict[str, Tuple[float, str, str]] = {}


def _target_cache_key(validated: ValidatedPublicUrl) -> str:
    """The cache identity of a resolved endpoint: its registered URL."""
    return validated.url


def _cached_target(cache_key: str) -> Optional[Tuple[Dialect, str]]:
    """`(dialect, rpc_url)` learned from a recent card read, or None."""
    entry = _dialect_cache.get(cache_key)
    if not entry:
        return None
    stored_at, version, rpc_url = entry
    if time.monotonic() - stored_at > A2A_DIALECT_CACHE_TTL:
        _dialect_cache.pop(cache_key, None)
        return None
    if version != "0.3":
        # Only v0.3 is claimed; an unrecognised cached version is a miss rather
        # than a guess.
        return None
    return a2a_protocol.DIALECT_V03, rpc_url


def _cache_target(cache_key: str, dialect: Dialect, rpc_url: str) -> None:
    _dialect_cache[cache_key] = (time.monotonic(), dialect.version, rpc_url)


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
        # The card gets its OWN, shorter budget. The client is built with the
        # RPC timeout because that hop is the one that matters, but leaving the
        # card on it breaks the arithmetic the total deadline rests on (10 s
        # card + 30 s RPC + slack = 45 s): a slow card would otherwise eat 30 s
        # of the window and leave the CREDENTIALED send 15 s.
        timeout=httpx.Timeout(A2A_CARD_FETCH_TIMEOUT, connect=A2A_CONNECT_TIMEOUT),
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
        declared = declared.strip()
        # EVERY field of the card is peer-controlled, so a malformed one must
        # produce a named refusal. `urlsplit` RAISES on an unterminated IPv6
        # authority (`https://[::1`), and that ValueError escaped this function,
        # the orchestrator's `except A2ACallError` and the router's error map —
        # surfacing as a peer-triggerable HTTP 500 with a logged traceback.
        try:
            declared_parts = urlsplit(declared)
            _ = declared_parts.port          # raises on a junk port, same class
        except ValueError:
            logger.error(
                "[a2a_client] card for %s declares an unparseable url; refusing",
                validated.hostname,
            )
            raise A2ACallError(
                "card_url_invalid",
                "The A2A endpoint's agent card declares a url that is not a valid "
                "URL. Refused.",
            ) from None
        # `_same_origin` compares `hostname`, which STRIPS userinfo — so
        # `https://u:p@peer.example.com/a2a` would compare equal to
        # `https://peer.example.com/a2a`. `_pinned_url` happens to drop the
        # userinfo when it rebuilds the authority, so nothing leaks today, but
        # that is a coincidence of one helper rather than a decision. A card
        # declaring credentials in its own URL is anomalous; refuse it by name
        # instead of relying on a downstream accident.
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
        if registered_path and (declared_parts.path or "").rstrip("/") != registered_path:
            raise A2ACallError(
                "card_url_ambiguous",
                "The registered A2A endpoint URL carries a path that the peer's card "
                "does not declare. Register the endpoint's origin, or a URL matching "
                "the card's declared url exactly.",
            )
        return declared

    if registered_path:
        # No declared url and a registered path: the operator named a specific
        # endpoint and the card offered no opinion. Honour the operator.
        return validated.url
    return urlunsplit((parts.scheme, parts.netloc, "/", "", ""))


# ---------------------------------------------------------------------------
# Response sanitisation
# ---------------------------------------------------------------------------

def sanitize_outbound_text(text: Optional[str], credential: Optional[str],
                           secrets=None) -> Tuple[Optional[str], bool]:
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
    # `secrets` (#3185) widens layer 1 from "the credential" to "the credential
    # AND the string leaves of its decoded payment payload": a remote echoing
    # the DECODED signature bypasses exact-value redaction of the base64 token.
    # It defaults to the credential alone, so every pre-existing caller is
    # unchanged.
    cleaned = scrub_secret_and_urls(window, credential or "")
    for secret in secrets or ():
        if secret and secret != credential:
            cleaned = scrub_secret_and_urls(cleaned, secret)
    cleaned = sanitize_text(cleaned)
    cleaned = redact_url_userinfo(cleaned)
    truncated = len(text) > A2A_MAX_RESPONSE_CHARS or len(cleaned) > A2A_MAX_RESPONSE_CHARS
    if len(cleaned) > A2A_MAX_RESPONSE_CHARS:
        cleaned = cleaned[:A2A_MAX_RESPONSE_CHARS] + "\n…[truncated by Trinity]"
    elif truncated:
        cleaned = cleaned + "\n…[truncated by Trinity]"
    return cleaned, truncated


# ---------------------------------------------------------------------------
# x402 payment plumbing (#3185)
#
# A priced peer answers "pay me" on one of two rails: an HTTP 402 on the RPC
# POST (requirements in a base64 `payment-required` header and/or the body), or
# an HTTP 200 Task whose metadata carries `x402.payment.status =
# payment-required`. Both become ONE outcome — `payment_required` — because the
# caller's next move is identical and the difference is the provider's SDK
# generation, not a decision the agent can act on.
#
# Everything here treats the peer's answer as hostile text: bounded, allowlisted
# and scrubbed before it reaches an LLM (or, via the add flow, a UI).
#
# No payments SDK is imported. The token codec below is a ten-line stdlib mirror
# of `payments_py.x402.token.decode_access_token` (a pure base64-JSON codec, the
# EIP-712 signature living INSIDE the payload so the round trip is byte-safe).
# The SDK is optional in OSS and pinned old in the image; an outbound OSS path
# must not depend on it.
# ---------------------------------------------------------------------------

def _try_json_b64(raw: Any, *, max_len: int = A2A_PAYMENT_HEADER_MAX_CHARS) -> Optional[Dict[str, Any]]:
    """A peer-controlled base64-JSON **object**, or `None`. Never raises.

    The codec itself lives in `a2a_protocol` — it is read by the endpoint store
    too (which infers a credential's kind from its shape, #3185 T6), and a
    second copy of a decoder is how two callers come to disagree about what a
    token *is*. This wrapper exists only to pin the outbound ceiling.
    """
    return a2a_protocol.json_b64_object(raw, max_len=max_len)


def _decode_payment_token(credential: Optional[str]) -> Optional[Dict[str, Any]]:
    """The stored token as an x402 `PaymentPayload`, or `None`.

    `None` is the **degrade, not a refusal** (decision 23/29): an opaque token
    is still sent as the `payment-signature` header, which is exactly today's
    working x402 path. What `None` prevents is shipping base64 garbage as
    `x402.payment.payload`. The shape check is `a2a_protocol`'s, shared with the
    store so "is this a payment token?" has one answer on both sides.
    """
    return a2a_protocol.decode_payment_token(
        credential, max_len=A2A_PAYMENT_HEADER_MAX_CHARS
    )


def _long_string_leaves(obj: Any, *, depth: int = 0) -> list:
    """Every string leaf worth treating as a secret, bounded on depth and count."""
    if depth >= A2A_SECRET_MAX_DEPTH:
        return []
    out: list = []
    if isinstance(obj, dict):
        values = list(obj.values())
    elif isinstance(obj, (list, tuple)):
        values = list(obj)
    else:
        return out
    for value in values:
        if len(out) >= A2A_SECRET_MAX_LEAVES:
            break
        if isinstance(value, str):
            if len(value) >= A2A_SECRET_LEAF_MIN_CHARS:
                out.append(value)
        elif isinstance(value, (dict, list, tuple)):
            out.extend(_long_string_leaves(value, depth=depth + 1))
    return out[:A2A_SECRET_MAX_LEAVES]


def _payment_secrets(credential: Optional[str],
                     decoded: Optional[Dict[str, Any]]) -> list:
    """The values that must not survive in ANY string we hand back.

    The token itself, plus the long string leaves of its decoded payload. The
    second half is the load-bearing one: exact-value redaction of the base64
    token does nothing about a remote that echoes the DECODED signature
    (`Validation error: signature 0xdead… is invalid`), and that body is
    peer-controlled text we are about to put in front of an LLM. A one-example
    redaction test proves one branch, so the suite parametrizes over the raw
    token, the decoded signature and the b64 of the token.

    The `>= 16 chars` floor keeps short field values (`0xabc`, `exact`,
    `base-sepolia`) out of the set — scrubbing those would redact ordinary prose
    and make a price unreadable.
    """
    import base64

    secrets = [credential] if credential else []
    if credential and len(credential) >= A2A_SECRET_LEAF_MIN_CHARS:
        # `scrub_secret`'s own "b64 form" pass covers ONLY the git
        # `x-access-token:<secret>` basic-auth spelling, which is not the
        # encoding a priced peer would echo. A remote that base64s the token
        # back at us would otherwise walk past exact-value redaction, so both
        # alphabets go in the set explicitly. Gated on length so a short
        # credential cannot turn ordinary prose into asterisks.
        raw = credential.encode("utf-8", errors="ignore")
        for encoder in (base64.b64encode, base64.urlsafe_b64encode):
            try:
                secrets.append(encoder(raw).decode("ascii").rstrip("="))
            except Exception:  # noqa: BLE001 — an unencodable credential is not a leak
                pass
    if decoded:
        secrets.extend(_long_string_leaves(decoded))
    return secrets


def _scrub(text: Any, secrets, *, cap: int = A2A_MAX_ERROR_TEXT_CHARS) -> str:
    """Redact every secret, then the platform patterns, then truncate.

    Same order and the same three layers as `sanitize_outbound_text` (see its
    docstring for why each is needed), over the whole string before the slice so
    a secret cannot be cut in half into a surviving prefix.
    """
    if not isinstance(text, str) or not text:
        return ""
    window = text[: cap * 2]
    for secret in secrets or ():
        if secret:
            window = scrub_secret_and_urls(window, secret)
    window = redact_url_userinfo(sanitize_text(window))
    if len(window) > cap:
        window = window[:cap] + "…[truncated by Trinity]"
    return window


def _json_object(raw: Optional[bytes]) -> Optional[Dict[str, Any]]:
    """A bounded error body as a JSON object, or `None`. Never raises."""
    import json

    if not raw:
        return None
    try:
        parsed = json.loads(raw.decode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001 — a non-JSON refusal body is normal
        return None
    return parsed if isinstance(parsed, dict) else None


def _bounded_leaf(value: Any, secrets) -> Any:
    """One leaf of the requirements object, bounded and scrubbed."""
    if isinstance(value, str):
        return _scrub(value, secrets, cap=A2A_PAYMENT_LEAF_MAX_CHARS)
    if isinstance(value, bool) or isinstance(value, (int, float)) or value is None:
        return value
    return None


def _bounded_x402(obj: Any, secrets) -> Tuple[Dict[str, Any], bool]:
    """The raw requirements object, allowlisted + leaf-capped. `(block, truncated)`.

    Per-leaf capping rather than replacing the whole block when it is too big:
    dropping everything at the ceiling loses the PRICE, which is the one thing
    the operator called this endpoint to learn (F8).
    """
    if not isinstance(obj, dict):
        return {}, False
    truncated = False
    out: Dict[str, Any] = {}
    leaves = 0

    def _walk(value: Any, depth: int) -> Any:
        nonlocal truncated, leaves
        if depth >= A2A_SECRET_MAX_DEPTH:
            truncated = True
            return None
        if isinstance(value, dict):
            inner: Dict[str, Any] = {}
            for key, item in value.items():
                if leaves >= A2A_PAYMENT_MAX_LEAVES:
                    truncated = True
                    break
                if not isinstance(key, str):
                    continue
                inner[_scrub(key, secrets, cap=64)] = _walk(item, depth + 1)
            return inner
        if isinstance(value, (list, tuple)):
            items = list(value)
            if len(items) > A2A_PAYMENT_MAX_ACCEPTS:
                items = items[:A2A_PAYMENT_MAX_ACCEPTS]
                truncated = True
            return [_walk(item, depth + 1) for item in items]
        leaves += 1
        bounded = _bounded_leaf(value, secrets)
        if isinstance(value, str) and len(value) > A2A_PAYMENT_LEAF_MAX_CHARS:
            truncated = True
        return bounded

    for key in _X402_TOP_LEVEL_KEYS:
        if key in obj:
            out[key] = _walk(obj[key], 0)
    if len(obj) > len(out):
        # Unknown top-level keys were dropped. Not "truncated" — that flag means
        # "a value you can see was shortened", and an allowlist miss is a
        # refusal to carry peer-chosen keys at all.
        pass
    return out, truncated


def _payment_summary(requirements: Any, secrets,
                     credits_per_request: Any = None) -> Dict[str, Any]:
    """The flat, Trinity-OWNED view of a price. Present keys only.

    The x402 v2 object has no `checkout_url`, no `plan` and no `credits` as
    named fields, so a fixed extracted schema would be inventing the protocol.
    This is the compromise the review landed on (T7): a stable flat summary an
    LLM, a human or the add-flow UI can read in one line, beside the bounded raw
    object for anything the provider puts in `extensions`. Nothing is invented —
    `purchase_url` is absent until a provider defines where it lives.
    """
    summary: Dict[str, Any] = {}
    req = requirements if isinstance(requirements, dict) else {}
    accepts = req.get("accepts")
    first = accepts[0] if isinstance(accepts, list) and accepts and isinstance(accepts[0], dict) else {}
    resource = req.get("resource") if isinstance(req.get("resource"), dict) else {}

    for key, value in (
        ("plan_id", first.get("planId")),
        ("scheme", first.get("scheme")),
        ("network", first.get("network")),
        ("resource_url", resource.get("url")),
        ("description", resource.get("description")),
    ):
        if isinstance(value, str) and value.strip():
            summary[key] = _scrub(value, secrets, cap=A2A_PAYMENT_LEAF_MAX_CHARS)
    if isinstance(credits_per_request, (int, float)) and not isinstance(credits_per_request, bool):
        summary["credits_per_request"] = credits_per_request
    error = req.get("error")
    if isinstance(error, str) and error.strip():
        summary["error"] = _scrub(error, secrets, cap=A2A_PAYMENT_LEAF_MAX_CHARS)
    return summary


def _bounded_payment_block(requirements: Any, *, secrets,
                           credits_per_request: Any = None,
                           truncated: bool = False) -> Dict[str, Any]:
    """`{summary, x402, truncated}` — the only payment shape that leaves this module.

    `truncated` is honest rather than cosmetic: it is True when anything the
    peer sent was shortened or dropped, including an oversized body we refused
    to read at all. An agent relaying a price to a human needs to know the price
    it is relaying may be partial.
    """
    import json

    x402, bounded_away = _bounded_x402(requirements, secrets)
    block = {
        "summary": _payment_summary(requirements, secrets, credits_per_request),
        "x402": x402,
        "truncated": bool(truncated or bounded_away),
    }
    try:
        if len(json.dumps(block)) > A2A_PAYMENT_BLOCK_MAX_BYTES:
            # Last resort, after per-leaf capping already ran: keep the summary
            # (the price) and drop the raw object (the forward-compat extra).
            block = {"summary": block["summary"], "x402": {}, "truncated": True}
            if len(json.dumps(block)) > A2A_PAYMENT_BLOCK_MAX_BYTES:
                block = {"summary": {}, "x402": {}, "truncated": True}
    except Exception:  # noqa: BLE001 — unserialisable means "do not hand it on"
        block = {"summary": {}, "x402": {}, "truncated": True}
    return block


def _error_message_from_body(body: Optional[Dict[str, Any]], *,
                             error_first: bool = False) -> Optional[str]:
    """Free text out of a refusal body, in the order the vocabulary blesses it.

    Three shapes, all already in use and none invented here: Trinity's own paid
    door emits `{"detail": …}` (and `{"detail": …, "error": …}` on a 403), while
    payments-py's A2A server emits a JSON-RPC `{"error": {"code", "message"}}`.
    """
    if not body:
        return None

    def _detail() -> Optional[str]:
        value = body.get("detail")
        return value if isinstance(value, str) and value.strip() else None

    def _error() -> Optional[str]:
        value = body.get("error")
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, dict):
            message = value.get("message")
            if isinstance(message, str) and message.strip():
                return message
        return None

    # `error_first` is for a 403: the paid door's 403 body is
    # `{"detail": "Payment verification failed", "error": "<code>"}`, where the
    # CODE is the actionable half ("already spent" vs "underfunded") and the
    # detail is the same sentence on every refusal.
    order = (_error, _detail) if error_first else (_detail, _error)
    for source in order:
        value = source()
        if value:
            return value
    message = body.get("message")
    if isinstance(message, str) and message.strip():
        return message
    return None


def _raise_payment_outcome(
    status: int,
    *,
    payment_header: Optional[str],
    body: Optional[bytes],
    secrets,
    credential_kind: str = "api_key",
    body_dropped: bool = False,
) -> None:
    """Classify a 402/403 from the RPC hop and raise. Never returns.

    **402 is terminal, never a retry.** The platform does not buy anything: a
    human reads the price, pays, registers the token on the endpoint, and the
    NEXT call carries it. Retrying here would spend money nobody approved.

    An unparseable 402 is still a 402. Degrading to `rpc_invalid` because the
    header was not base64 would hide the one fact that is unambiguous — the
    status — behind a parse failure of the decoration around it.

    **403 is split by our OWN credential kind, not by the peer's prose** (T3).
    A peer that refuses a `payment_token` is telling us the token was rejected
    (expired, spent, underfunded), which is the "top up" case the caller must be
    able to tell from "buy". A peer that refuses an `api_key` is a plain auth
    failure. Neither reading comes from matching words in the body, and both
    carry `remote_status: 403`, so 402-vs-403 is always answerable.
    """
    parsed = _json_object(body)
    message_raw = _error_message_from_body(parsed, error_first=(status == 403))

    if status == 402:
        requirements = _try_json_b64(payment_header)
        credits = None
        if requirements is None and parsed:
            candidate = parsed.get("payment_required")
            if isinstance(candidate, dict):
                requirements = candidate
        if parsed is not None:
            credits = parsed.get("credits_per_request")
        payment = _bounded_payment_block(
            requirements, secrets=secrets, credits_per_request=credits,
            truncated=body_dropped,
        )
        message = _scrub(message_raw, secrets) if message_raw else ""
        raise A2ACallError(
            "payment_required",
            message or (
                "The A2A endpoint requires payment before it will answer. Relay the "
                "price to a person once; do not retry. Once the token is registered "
                "on the endpoint, call again quoting the returned task_id."
            ),
            remote_status=402,
            payment=payment,
        )

    # 403.
    rejected = credential_kind == CREDENTIAL_KIND_PAYMENT_TOKEN
    message = _scrub(message_raw, secrets) if message_raw else ""
    if rejected:
        detail = message or "The A2A endpoint refused the payment token."
        raise A2ACallError(
            "payment_rejected",
            f"{detail} The registered payment token was refused (HTTP 403) — it may "
            "be expired, already spent, or underfunded. A person must register a new "
            "token; this call was not retried.",
            remote_status=403,
        )
    detail = message or "The A2A endpoint refused the call."
    raise A2ACallError(
        "rpc_forbidden",
        f"{detail} The A2A endpoint returned HTTP 403. If this endpoint is priced, "
        "register the token with credential_kind=payment_token.",
        remote_status=403,
    )


def _x402_metadata(result: Any) -> Dict[str, Any]:
    """The peer's x402 metadata dict, from either location it may ride in.

    `status.message.metadata` is where the a2a-x402 extension puts it; the
    task-level `metadata` is a tolerated fallback, because the two SDK
    generations in the field do not agree and a payment state we fail to see is
    handed to the agent as a pollable "input-required" prompt.
    """
    if not isinstance(result, dict):
        return {}
    status = result.get("status") if isinstance(result.get("status"), dict) else {}
    message = status.get("message") if isinstance(status.get("message"), dict) else {}
    for candidate in (message.get("metadata"), result.get("metadata")):
        if isinstance(candidate, dict) and isinstance(
            candidate.get(a2a_protocol.X402_STATUS_KEY), str
        ):
            return candidate
    return {}


def _raise_for_payment_state(result: Any, secrets) -> Optional[str]:
    """Surface an IN-BAND payment state that arrived on HTTP 200.

    Runs **before** `_parse_task` on both the send and the poll path. That order
    is the whole point: a priced peer answers `input-required` with "pay me" in
    the metadata, and parsed as a task that is an ordinary prompt the agent will
    poll forever — a non-answer presented as progress.

    Returns `payment-completed` (recorded by the caller, never surfaced to the
    agent) or `None`. Raises on `payment-required` / `payment-failed`.
    """
    metadata = _x402_metadata(result)
    if not metadata:
        return None
    state = metadata.get(a2a_protocol.X402_STATUS_KEY)

    if state == a2a_protocol.X402_STATUS_REQUIRED:
        requirements = metadata.get(a2a_protocol.X402_REQUIRED_KEY)
        task_id = result.get("id") if isinstance(result.get("id"), str) else None
        raise A2ACallError(
            "payment_required",
            "The A2A endpoint requires payment before it will answer. Relay the price "
            "to a person once; do not retry. Once the token is registered on the "
            "endpoint, call again quoting the returned task_id.",
            payment=_bounded_payment_block(requirements, secrets=secrets),
            task_id=task_id,
        )

    if state == a2a_protocol.X402_STATUS_FAILED:
        error = metadata.get(a2a_protocol.X402_ERROR_KEY)
        parts = []
        if isinstance(error, dict):
            for key in ("code", "reason", "message"):
                value = error.get(key)
                if isinstance(value, str) and value.strip():
                    parts.append(_scrub(value, secrets))
        elif isinstance(error, str):
            parts.append(_scrub(error, secrets))
        suffix = f" ({': '.join(parts)})" if parts else ""
        raise A2ACallError(
            "payment_rejected",
            f"The A2A endpoint rejected the payment token{suffix}. A person must "
            "register a new token; this call was not retried.",
            task_id=result.get("id") if isinstance(result.get("id"), str) else None,
        )

    if state == a2a_protocol.X402_STATUS_COMPLETED:
        return state
    return None


def _payment_context(credential: Optional[str], credential_kind: str) -> Tuple[Optional[Dict[str, Any]], list]:
    """`(in-band payload or None, secrets)` for one call. Computed ONCE per call.

    Decoding twice would be harmless but the secrets list must be the same one
    every error builder sees, so it is built here and threaded through.
    """
    decoded = (
        _decode_payment_token(credential)
        if credential and credential_kind == CREDENTIAL_KIND_PAYMENT_TOKEN
        else None
    )
    return decoded, _payment_secrets(credential, decoded)


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
    *,
    credential_kind: str = "api_key",
    payment: Optional[Tuple[Optional[Dict[str, Any]], list]] = None,
) -> Dict[str, Any]:
    """One credentialed JSON-RPC POST, pinned + capped. Returns the parsed body.

    `credential_kind` changes **only** what rides along with the credential, and
    only when it is `payment_token`: the decoded token under
    `x402.payment.payload` in the message metadata (the primary carriage per the
    2026-10-03 design note) plus the deprecated `payment-signature` header on
    the same request. An `api_key` endpoint — which is every endpoint registered
    before #3185, since the field defaults — sends exactly the bytes it sent
    before: `Authorization: Bearer …` and no `metadata` key.

    `payment` lets the caller hand in the `(decoded, secrets)` pair it already
    computed, so one call decodes the token once and every error builder in it
    scrubs against the same set.
    """
    import json

    address = validated.addresses[0]
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if credential:
        headers["Authorization"] = f"Bearer {credential}"

    decoded, secrets = payment if payment is not None else _payment_context(
        credential, credential_kind
    )
    if credential and credential_kind == CREDENTIAL_KIND_PAYMENT_TOKEN:
        if A2A_SEND_PAYMENT_SIGNATURE_HEADER:
            headers[a2a_protocol.X402_PAYMENT_SIGNATURE_HEADER] = credential
        message = params.get("message")
        if decoded is not None and isinstance(message, dict):
            # `setdefault`, not assignment: a future caller that builds its own
            # metadata must not have it replaced, and `tasks/get` has no message
            # at all — which is why the header above is still the poll path's
            # only carriage (F2).
            metadata = message.setdefault("metadata", {})
            if isinstance(metadata, dict):
                metadata[a2a_protocol.X402_STATUS_KEY] = a2a_protocol.X402_STATUS_SUBMITTED
                metadata[a2a_protocol.X402_PAYLOAD_KEY] = decoded

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
        secret=credential,
        secrets=secrets,
        credential_kind=credential_kind,
    )
    try:
        body = json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001
        raise A2ACallError("rpc_invalid", "The A2A endpoint returned a non-JSON body.") from None
    if not isinstance(body, dict):
        raise A2ACallError("rpc_invalid", "The A2A endpoint returned a non-object body.")
    return body


def _raise_for_rpc_error(body: Dict[str, Any], credential: Optional[str],
                         secrets=None) -> Dict[str, Any]:
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
            secrets=secrets,
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
    credential_kind: str = "api_key",
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
            card = await fetch_card(client, endpoint)
            try:
                dialect = a2a_protocol.resolve_dialect(card.get("protocolVersion"))
            except UnsupportedProtocolVersion as exc:
                raise A2ACallError("unsupported_protocol_version", str(exc)) from None
            rpc_url = resolve_rpc_target(endpoint, card)
            _cache_target(_target_cache_key(endpoint), dialect, rpc_url)

            params = {
                "message": a2a_protocol.text_message(
                    message, uuid.uuid4().hex, context_id=context_id, task_id=task_id
                )
            }
            payment = _payment_context(credential, credential_kind)
            secrets = payment[1]
            body = await _rpc(
                client, endpoint, rpc_url, credential, dialect.send_message, params,
                credential_kind=credential_kind, payment=payment,
            )
            result = _raise_for_rpc_error(body, credential, secrets)
            # BEFORE `_parse_task`: a priced peer answers `input-required` with
            # "pay me" in the metadata, and parsed as a task that is an ordinary
            # prompt the agent will poll forever (decision 13).
            payment_status = _raise_for_payment_state(result, secrets)
            state, text, remote_task_id, remote_context_id = _parse_task(result)
            clean, truncated = sanitize_outbound_text(text, credential, secrets)
            return A2AResult(
                state=state,
                text=clean,
                task_id=remote_task_id,
                context_id=remote_context_id or context_id,
                truncated=truncated,
                protocol_version=dialect.version,
                host=endpoint.hostname,
                payment_status=payment_status,
            )

    return await _with_deadline(_run())


async def get_task(
    *,
    endpoint_url: str,
    credential: Optional[str],
    task_id: str,
    client_factory=None,
    validated: Optional[ValidatedPublicUrl] = None,
    credential_kind: str = "api_key",
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
            cache_key = _target_cache_key(endpoint)
            cached = _cached_target(cache_key)
            if cached is None:
                card = await fetch_card(client, endpoint)
                try:
                    dialect = a2a_protocol.resolve_dialect(card.get("protocolVersion"))
                except UnsupportedProtocolVersion as exc:
                    raise A2ACallError("unsupported_protocol_version", str(exc)) from None
                rpc_url = resolve_rpc_target(endpoint, card)
                _cache_target(cache_key, dialect, rpc_url)
            else:
                # Both values came from ONE card read of this origin, which the
                # same-origin pin already validated. Re-deriving the target here
                # instead would be wrong for an operator who registered the
                # origin: the card named the real endpoint, and a re-derivation
                # would poll `/`.
                dialect, rpc_url = cached

            payment = _payment_context(credential, credential_kind)
            secrets = payment[1]
            body = await _rpc(
                client, endpoint, rpc_url, credential, dialect.get_task, {"id": task_id},
                credential_kind=credential_kind, payment=payment,
            )
            result = _raise_for_rpc_error(body, credential, secrets)
            # The poll path runs the SAME in-band check (decision 7): without it
            # a poll of a priced task answers "input-required: pay me", which an
            # agent reads as progress and polls again.
            payment_status = _raise_for_payment_state(result, secrets)
            state, text, remote_task_id, remote_context_id = _parse_task(result)
            clean, truncated = sanitize_outbound_text(text, credential, secrets)
            return A2AResult(
                state=state,
                text=clean,
                task_id=remote_task_id or task_id,
                context_id=remote_context_id,
                truncated=truncated,
                protocol_version=dialect.version,
                host=endpoint.hostname,
                payment_status=payment_status,
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
