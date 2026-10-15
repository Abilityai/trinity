"""The caller's address behind the bundled proxy (abilityai/trinity-enterprise#873).

Every per-IP limiter and every audit `actor_ip` wants the address of the
caller. `request.client.host` is the TCP peer, which for a browser user is the
bundled nginx — so keying on it puts every visitor in one bucket. Forwarded
headers carry the real address, but any caller can send them, so they are read
only when the peer is a proxy Trinity trusts. This module is the one place that
decides that; `routers/public.py::_get_client_ip` is the same function.

Trusted proxy peers:

* loopback;
* the addresses the bundled proxy containers' names resolve to
  (`TRUSTED_PROXY_HOSTS`, default the frontend and cloudflared names). The
  frontend shares the agent subnet and has no fixed address, so a name is the
  only way to tell it from an agent;
* `TRUSTED_PROXIES` (CIDRs; default the RFC-1918 ranges, for an operator's own
  load balancer) MINUS the two Trinity subnets. An agent-network address is
  trusted only by name, so an agent cannot speak for someone else.

A bridge gateway is never a trusted peer: an agent hairpinning through a
published port, a host-local process and a userland-proxied caller all arrive
from it. A host-side front door (Caddy) that puts the gateway in
`X-Forwarded-For` is handled by `TRUSTED_PROXY_HOPS` — addresses that may be
SKIPPED inside the header, never trusted as the peer. Default empty;
`start.sh --hosted` writes the agent-network gateway.

Names are resolved by a daemon thread every few seconds; the request path only
reads the cache (`getaddrinfo` has no timeout and would block the event loop).

Leaf module: stdlib only, duck-typed `request` (`.client.host`, `.headers`).
"""
from __future__ import annotations

import functools
import ipaddress
import logging
import os
import socket
import threading
import time

logger = logging.getLogger(__name__)

# The agent and platform bridge subnets (docker-compose*.yml). Never trusted by
# CIDR, whatever TRUSTED_PROXIES says — only by resolved name.
TRINITY_SUBNETS = (
    ipaddress.ip_network("172.28.0.0/16"),
    ipaddress.ip_network("172.29.0.0/16"),
)
LOOPBACK = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
)
DEFAULT_TRUSTED_PROXIES = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
DEFAULT_TRUSTED_PROXY_HOSTS = "trinity-frontend,frontend,trinity-cloudflared,cloudflared"
REFRESH_SECONDS = 5.0

_getaddrinfo = socket.getaddrinfo

_lock = threading.Lock()
_resolved: frozenset = frozenset()
_refresher_started = False
_logged_effective = False
_warned_untrusted_xff = False


def _parse_ip(value):
    """A valid address (v4-mapped unwrapped) or None."""
    if not isinstance(value, str):
        return None
    try:
        addr = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    if addr.version == 6 and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


@functools.lru_cache(maxsize=16)
def _parse_networks(raw: str, var: str) -> tuple:
    networks = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            logger.warning("Invalid %s entry ignored: %s", var, entry)
    return tuple(networks)


def _env_networks(var: str, default: str) -> tuple:
    return _parse_networks(os.environ.get(var, default), var)


def _host_names() -> list:
    raw = os.environ.get("TRUSTED_PROXY_HOSTS", DEFAULT_TRUSTED_PROXY_HOSTS)
    return [n.strip() for n in raw.split(",") if n.strip()]


def _in(addr, networks) -> bool:
    return any(addr.version == net.version and addr in net for net in networks)


def _trusted(addr) -> bool:
    if _in(addr, LOOPBACK) or addr in _resolved:
        return True
    if _in(addr, TRINITY_SUBNETS):
        return False
    return _in(addr, _env_networks("TRUSTED_PROXIES", DEFAULT_TRUSTED_PROXIES))


def _skippable(addr) -> bool:
    return _trusted(addr) or _in(addr, _env_networks("TRUSTED_PROXY_HOPS", ""))


def is_trusted_proxy(ip_str: str) -> bool:
    """True when a TCP peer at `ip_str` may speak for another address."""
    addr = _parse_ip(ip_str)
    return addr is not None and _trusted(addr)


def trusted_networks() -> list:
    """The CIDR part of the trusted set (loopback + TRUSTED_PROXIES)."""
    return list(LOOPBACK) + list(_env_networks("TRUSTED_PROXIES", DEFAULT_TRUSTED_PROXIES))


def _refresh_once() -> None:
    """Resolve TRUSTED_PROXY_HOSTS into the cache. A name that does not resolve
    contributes nothing — never its previous address, which may now be an agent's."""
    global _resolved, _logged_effective
    found = set()
    for name in _host_names():
        try:
            infos = _getaddrinfo(name, None, proto=socket.IPPROTO_TCP)
        except (OSError, UnicodeError):
            continue
        for info in infos:
            addr = _parse_ip(info[4][0])
            if addr is not None:
                found.add(addr)
    _resolved = frozenset(found)
    if not _logged_effective:
        _logged_effective = True
        logger.info(
            "client_ip: trusted proxy names resolved to %s; TRUSTED_PROXIES=%s; TRUSTED_PROXY_HOPS=%s",
            sorted(str(a) for a in found) or "nothing",
            ",".join(str(n) for n in _env_networks("TRUSTED_PROXIES", DEFAULT_TRUSTED_PROXIES)) or "none",
            ",".join(str(n) for n in _env_networks("TRUSTED_PROXY_HOPS", "")) or "none",
        )


def _refresh_loop() -> None:
    while True:
        try:
            _refresh_once()
        except Exception:  # never let the refresher die
            logger.exception("client_ip: proxy name refresh failed")
        time.sleep(REFRESH_SECONDS)


def _start_refresher() -> None:
    threading.Thread(target=_refresh_loop, name="client-ip-proxy-names", daemon=True).start()


def _ensure_refresher() -> None:
    global _refresher_started
    if _refresher_started:
        return
    with _lock:
        if _refresher_started:
            return
        _refresher_started = True
    _start_refresher()


def _reset_cache_for_tests() -> None:
    global _resolved, _refresher_started, _logged_effective, _warned_untrusted_xff
    _resolved = frozenset()
    _refresher_started = False
    _logged_effective = False
    _warned_untrusted_xff = False
    _parse_networks.cache_clear()


def _header_values(headers, name: str) -> list:
    getlist = getattr(headers, "getlist", None)
    if callable(getlist):
        values = getlist(name)
        if isinstance(values, (list, tuple)):
            return [v for v in values if isinstance(v, str)]
    value = headers.get(name)
    return [value] if isinstance(value, str) else []


def client_ip(request) -> str:
    """The address to key per-IP limits and audit rows on.

    Untrusted peer → the peer. Trusted peer → walk `X-Forwarded-For` right to
    left, skipping trusted proxies and hops; the first other address wins, and
    a token that is not an address returns the peer (never walk into text the
    caller wrote). No `X-Forwarded-For` → `X-Real-IP` when valid and untrusted.
    """
    _ensure_refresher()
    raw_peer = request.client.host if request.client else None
    if not raw_peer:
        return "unknown"
    peer = _parse_ip(raw_peer)
    if peer is None:
        return raw_peer
    peer_str = str(peer)

    forwarded = _header_values(request.headers, "x-forwarded-for")
    if not _trusted(peer):
        global _warned_untrusted_xff
        if forwarded and not _warned_untrusted_xff and not _in(peer, TRINITY_SUBNETS):
            _warned_untrusted_xff = True
            logger.warning(
                "client_ip: X-Forwarded-For from untrusted peer %s ignored; if this is "
                "your reverse proxy, add it to TRUSTED_PROXIES", peer_str,
            )
        return peer_str

    if forwarded:
        for token in reversed(",".join(forwarded).split(",")):
            addr = _parse_ip(token)
            if addr is None:
                return peer_str
            if not _skippable(addr):
                return str(addr)
        return peer_str

    real = _parse_ip(request.headers.get("x-real-ip"))
    if real is not None and not _skippable(real):
        return str(real)
    return peer_str


def check_uvicorn_proxy_trust() -> bool:
    """False (and one ERROR line) when uvicorn is told to trust forwarded
    headers from every peer. uvicorn then rewrites `request.client.host` from a
    caller-chosen `X-Forwarded-For` before this module sees it, so no peer check
    here can hold. Reported, not refused: refusing to boot on upgrade would
    take an install down over a setting it may have carried for months."""
    raw = os.environ.get("FORWARDED_ALLOW_IPS", "")
    entries = {e.strip() for e in raw.split(",")}
    if entries & {"*", "0.0.0.0/0", "::/0"}:
        logger.error(
            "FORWARDED_ALLOW_IPS=%s trusts forwarded headers from any peer, so any caller "
            "can choose the address it is rate-limited and audited under. Unset it (the "
            "backend resolves the client address itself) or list your proxy's address.",
            raw.strip(),
        )
        return False
    return True
