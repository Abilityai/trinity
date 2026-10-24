"""One canonical client address for every HTTP and WebSocket request
(trinity-enterprise#873, #3047).

Every per-IP limit and every request-derived audit address reads
`request.client.host`, and behind a trusted proxy that must be the client the
proxy saw, never the proxy itself. This module decides, once per request and
before any route runs, which address the request is attributed to, and writes
it back into the ASGI scope so every consumer — limiter, audit row, uvicorn's
access log — reads the same answer.

The rule is short. Forwarding headers are evidence only when the TCP peer is an
explicitly trusted proxy:

* the bundled nginx and cloudflared, at their reserved addresses on the
  prod/hosted ingress network (``TRINITY_BUNDLED_PROXY_TRUST=1``,
  ``TRINITY_PROXY_PREFIX``: ``<prefix>.2`` and ``<prefix>.3``);
* an operator's own proxy, listed in ``TRUSTED_PROXIES``.

Anything else — a direct caller to the published backend port, an agent, a
bridge gateway, the development Vite proxy — is attributed to its own socket
address whatever it sends. No address is trusted for being private, and the
bridge gateway never is: a published port delivers every host-forwarded caller
from there.

From a trusted peer, ``X-Forwarded-For`` is read right to left. Trusted proxy
entries are skipped; the first untrusted entry is the client and everything to
its left is ignored, because only the right-hand suffix was appended by hops we
trust. When that suffix reaches the bundled cloudflared, its single
``CF-Connecting-IP`` is the client. ``X-Real-IP`` and RFC 7239 ``Forwarded`` are
never read. Headers are left untouched for the routes that read them.

Absent identity is not invalid identity. A trusted proxy that forwards no
identity leaves the request attributed to that proxy (provenance
``proxy_fallback``, a shared bucket, logged at most once a minute); identity
that is present but empty, malformed, duplicated where it must be single, made
only of trusted hops, or longer than the parsing budget is refused — HTTP 400,
or a WebSocket close 1008 before the handshake is accepted.

Pure leaf: no routers, services, DNS, Docker, threads or background refresh.
The configuration is read from the environment and validated as a whole; an
invalid ``TRUSTED_PROXIES`` stops the backend at startup with the offending
entry named (``main.py`` loads it at import).
"""
from __future__ import annotations

import ipaddress
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Iterable, List, Optional, Tuple, Union

logger = logging.getLogger(__name__)

Address = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]
Network = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]

# The two platform bridges every compose file pins (#589).
AGENT_NETWORK = ipaddress.ip_network("172.28.0.0/16")
PLATFORM_NETWORK = ipaddress.ip_network("172.29.0.0/16")
DEFAULT_PROXY_PREFIX = "172.30.0"

# Budget for the part of X-Forwarded-For the walk actually consumes. Only the
# authenticated right-hand suffix is ever read, so a long client-supplied left
# prefix costs nothing and is never a reason to refuse.
MAX_SUFFIX_BYTES = 4096
MAX_SUFFIX_ELEMENTS = 32
# A CF-Connecting-IP value is one address literal (<= 45 bytes) plus optional
# whitespace; anything longer is refused without being decoded.
_MAX_CF_BYTES = 64

# Scope key carrying the socket peer and how the address was decided.
SCOPE_KEY = "trinity.client_address"

PROVENANCE_PEER = "peer"
PROVENANCE_XFF = "xff"
PROVENANCE_CF = "cf"
PROVENANCE_FALLBACK = "proxy_fallback"

_OCTET = re.compile(r"^(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])$")
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"", "0", "false", "no", "off"}


class ClientIPConfigError(ValueError):
    """The proxy-trust configuration is invalid. The message names the setting,
    the entry and the rule it breaks."""


class IdentityRefused(Exception):
    """Forwarded identity that is required but unusable. `reason` is a fixed
    label, never header content, so it is safe to log."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ClientIPConfig:
    frontend: Optional[Address] = None       # bundled nginx: <prefix>.2
    cloudflared: Optional[Address] = None    # bundled cloudflared: <prefix>.3
    proxy_subnet: Optional[Network] = None   # <prefix>.0/24 when bundled trust is on
    operator: Tuple[Network, ...] = ()

    def is_trusted(self, addr: Address) -> bool:
        if addr == self.frontend or addr == self.cloudflared:
            return True
        return any(addr.version == n.version and addr in n for n in self.operator)


@dataclass(frozen=True)
class Resolution:
    address: str
    provenance: str


# ---------------------------------------------------------------------------
# Literal parsing
# ---------------------------------------------------------------------------

def parse_literal(text: str) -> Optional[Address]:
    """A bare IPv4/IPv6 literal, canonicalised; None for anything else.

    Refused: host names, ports, brackets, zone identifiers, control characters,
    the unspecified and multicast addresses. Accepted: private and loopback
    addresses (as clients, never as trust) and Cloudflare's pseudo-IPv4 range
    240.0.0.0/4. An IPv4-mapped IPv6 address becomes its IPv4 form.
    """
    if not text or len(text) > 45:
        return None
    if any(c in text for c in "[]%/") or any(ord(c) < 33 or ord(c) > 126 for c in text):
        return None
    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        return None
    if addr.version == 6 and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    if addr.is_unspecified or addr.is_multicast:
        return None
    return addr


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _proxy_prefix(raw: str) -> Tuple[Network, Address, Address]:
    parts = raw.split(".")
    if len(parts) != 3 or not all(_OCTET.match(p) for p in parts):
        raise ClientIPConfigError(
            f"TRINITY_PROXY_PREFIX={raw!r}: expected three decimal IPv4 octets, e.g. 172.30.0")
    subnet = ipaddress.ip_network(f"{raw}.0/24")
    for managed in (AGENT_NETWORK, PLATFORM_NETWORK):
        if subnet.overlaps(managed):
            raise ClientIPConfigError(
                f"TRINITY_PROXY_PREFIX={raw!r}: {subnet} overlaps the platform network {managed}")
    return subnet, ipaddress.ip_address(f"{raw}.2"), ipaddress.ip_address(f"{raw}.3")


def _operator_networks(raw: str, managed: Iterable[Network]) -> Tuple[Network, ...]:
    managed = list(managed)
    out: List[Network] = []
    for token in re.split(r"[,\s]+", raw.strip()):
        if not token:
            continue
        where = f"TRUSTED_PROXIES entry {token!r}"
        if "*" in token:
            raise ClientIPConfigError(f"{where}: wildcards are not allowed — list the proxy's address")
        try:
            net = ipaddress.ip_network(token, strict=False)
        except ValueError:
            raise ClientIPConfigError(f"{where}: not an IP address or CIDR") from None
        if net.prefixlen == 0:
            raise ClientIPConfigError(f"{where}: a default route trusts every caller")
        if net.version == 6 and net.network_address.ipv4_mapped is not None:
            raise ClientIPConfigError(f"{where}: write IPv4 proxies in IPv4 form")
        if net.overlaps(ipaddress.ip_network("127.0.0.0/8") if net.version == 4
                        else ipaddress.ip_network("::1/128")):
            raise ClientIPConfigError(
                f"{where}: loopback cannot be trusted — a host-local proxy uses the "
                "protected PROXY-protocol listener instead")
        for m in managed:
            if m.version == net.version and net.overlaps(m):
                raise ClientIPConfigError(
                    f"{where}: overlaps the platform-managed network {m}; agents, "
                    "gateways and published ports deliver callers from there")
        if net not in out:
            out.append(net)
    return tuple(out)


def load_config(environ=None) -> ClientIPConfig:
    """Build and validate the whole configuration; raise `ClientIPConfigError`
    naming the first broken setting. Never returns a partial list."""
    env = os.environ if environ is None else environ
    raw_trust = env.get("TRINITY_BUNDLED_PROXY_TRUST", "").strip().lower()
    if raw_trust not in _TRUE | _FALSE:
        raise ClientIPConfigError(
            f"TRINITY_BUNDLED_PROXY_TRUST={raw_trust!r}: expected 1 or 0")
    frontend = cloudflared = subnet = None
    managed: List[Network] = [AGENT_NETWORK, PLATFORM_NETWORK]
    if raw_trust in _TRUE:
        prefix = env.get("TRINITY_PROXY_PREFIX", "").strip() or DEFAULT_PROXY_PREFIX
        subnet, frontend, cloudflared = _proxy_prefix(prefix)
        managed.append(subnet)
    operator = _operator_networks(env.get("TRUSTED_PROXIES", ""), managed)
    return ClientIPConfig(frontend=frontend, cloudflared=cloudflared,
                          proxy_subnet=subnet, operator=operator)


_ENV_KEYS = ("TRINITY_BUNDLED_PROXY_TRUST", "TRINITY_PROXY_PREFIX", "TRUSTED_PROXIES")
_cache: dict = {"key": None, "config": None}


def get_config() -> ClientIPConfig:
    """The validated configuration for the current environment.

    Re-read only when one of its three variables changes (in a deployed
    process they never do), so a request pays three dictionary lookups.
    """
    key = tuple(os.environ.get(k) for k in _ENV_KEYS)
    if _cache["key"] != key:
        config = load_config()
        _cache.update(key=key, config=config)
    return _cache["config"]


def managed_proxy_subnet() -> Optional[Network]:
    """The active ingress subnet, or None when bundled trust is off. Read by
    `services/a2a_trusted_networks.py` so an admin cannot declare it trusted."""
    return get_config().proxy_subnet


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def _header_fields(headers, name: bytes) -> List[bytes]:
    return [v for k, v in headers if k == name]


def _cf_identity(headers) -> Optional[str]:
    """The single CF-Connecting-IP value, or None when absent. Refuses a
    duplicated, empty or malformed one."""
    fields = _header_fields(headers, b"cf-connecting-ip")
    if not fields:
        return None
    if len(fields) > 1:
        raise IdentityRefused("duplicate CF-Connecting-IP")
    if len(fields[0]) > _MAX_CF_BYTES:
        # Checked before decoding: an address literal is at most 45 bytes.
        raise IdentityRefused("malformed CF-Connecting-IP")
    value = fields[0].decode("latin-1").strip(" \t")
    if not value:
        raise IdentityRefused("empty CF-Connecting-IP")
    addr = parse_literal(value)
    if addr is None:
        raise IdentityRefused("malformed CF-Connecting-IP")
    return str(addr)


def _xff_suffix(fields: List[bytes]):
    """Yield X-Forwarded-For entries right to left across every field in
    reverse wire order (repeated fields are one ordered list, RFC 9110 §5.3),
    without splitting or copying the unread left-hand prefix. Raises when the
    consumed suffix exceeds its budget.

    The budget bounds the WORK, not only the answer: each separator search
    looks back no further than the bytes the budget still allows, and an entry
    with no separator inside that window is refused before it is sliced. So a
    header of any length costs at most MAX_SUFFIX_BYTES of searching and
    copying."""
    consumed = 0
    elements = 0
    for field in reversed(fields):
        end = len(field)
        while True:
            remaining = MAX_SUFFIX_BYTES - consumed
            lo = max(0, end - remaining)
            comma = field.rfind(b",", lo, end)
            if comma < 0 and lo > 0:
                # The entry alone is longer than what the budget has left.
                raise IdentityRefused("X-Forwarded-For suffix exceeds its budget")
            start = comma + 1
            token = field[start:end]
            consumed += len(token) + 1
            elements += 1
            if consumed > MAX_SUFFIX_BYTES or elements > MAX_SUFFIX_ELEMENTS:
                raise IdentityRefused("X-Forwarded-For suffix exceeds its budget")
            yield token
            if start == 0:
                break
            end = start - 1


def _resolve_cf(config: ClientIPConfig, headers, peer_text: str) -> Resolution:
    cf = _cf_identity(headers)
    if cf is None:
        return Resolution(peer_text, PROVENANCE_FALLBACK)
    return Resolution(cf, PROVENANCE_CF)


def resolve(config: ClientIPConfig, peer_text: str, headers) -> Optional[Resolution]:
    """Attribute one request. `peer_text` is the socket peer's host string,
    `headers` the raw ASGI header list. None means the peer is not an address
    (absent, a Unix socket, a test client name) — the scope is then left alone
    and no header can supply one."""
    peer = parse_literal(peer_text) if peer_text else None
    if peer is None:
        return None
    canonical = str(peer)
    if not config.is_trusted(peer):
        return Resolution(canonical, PROVENANCE_PEER)
    if peer == config.cloudflared:
        return _resolve_cf(config, headers, canonical)

    fields = _header_fields(headers, b"x-forwarded-for")
    if not fields:
        return Resolution(canonical, PROVENANCE_FALLBACK)
    for raw in _xff_suffix(fields):
        token = raw.decode("latin-1").strip(" \t")
        if not token:
            raise IdentityRefused("empty X-Forwarded-For entry")
        hop = parse_literal(token)
        if hop is None:
            raise IdentityRefused("malformed X-Forwarded-For entry")
        if config.cloudflared is not None and hop == config.cloudflared:
            # The authenticated suffix reached the bundled tunnel: its own
            # client header is the identity, and nothing left of it is read.
            # No CF identity keeps the original peer (the nginx hop), never a
            # guess from the unverified prefix.
            return _resolve_cf(config, headers, canonical)
        if config.is_trusted(hop):
            continue
        return Resolution(str(hop), PROVENANCE_XFF)
    raise IdentityRefused("X-Forwarded-For names only trusted proxies")


# ---------------------------------------------------------------------------
# ASGI middleware
# ---------------------------------------------------------------------------

_FALLBACK_LOG_INTERVAL = 60.0
_last_fallback_log = [float("-inf")]


def _note_fallback(peer: str) -> None:
    now = time.monotonic()
    if now - _last_fallback_log[0] >= _FALLBACK_LOG_INTERVAL:
        _last_fallback_log[0] = now
        logger.warning(
            "[client-ip] trusted proxy %s forwarded no client identity; requests "
            "without it are attributed to the proxy (shared rate-limit bucket). "
            "Logged at most once a minute.", peer)


class ClientAddressMiddleware:
    """Pure ASGI, so WebSocket handshakes are covered too. Registered just
    inside `HostHeaderGuard` (main.py), outside every other middleware."""

    def __init__(self, app, config: Optional[ClientIPConfig] = None):
        self.app = app
        self._config = config

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            config = self._config or get_config()
            client = scope.get("client")
            peer_text = client[0] if client else ""
            try:
                result = resolve(config, peer_text, scope.get("headers") or [])
            except IdentityRefused as exc:
                logger.warning("[client-ip] refused request from trusted proxy %s: %s",
                               peer_text, exc.reason)
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                    return
                await send({"type": "http.response.start", "status": 400,
                            "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": b"Invalid client identity"})
                return
            if result is not None:
                scope[SCOPE_KEY] = {"peer": tuple(client), "provenance": result.provenance}
                if result.provenance == PROVENANCE_FALLBACK:
                    _note_fallback(peer_text)
                # In place, so uvicorn's access log reads the same address.
                port = client[1] if result.provenance in (PROVENANCE_PEER, PROVENANCE_FALLBACK) else 0
                scope["client"] = (result.address, port)
        await self.app(scope, receive, send)
