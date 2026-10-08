"""Trusted internal networks for A2A (trinity-enterprise#838).

An operator running several Trinity instances on one private network (a
tailnet, a VPN) declares that network here, once, as an admin. Two things then
follow, and nothing else changes:

* **Outbound** — an A2A endpoint whose host matches a trusted entry may resolve
  to a private or CGNAT address and may use ``http://``
  (``utils/url_validation.validate_a2a_endpoint_url``). Every OTHER endpoint
  keeps today's public-HTTPS rule, and the check still runs on every call, so a
  trusted name that later resolves outside the trusted set is refused.
* **Inbound** — an agent whose A2A scope is ``internal`` answers only a request
  whose source address is inside a trusted CIDR (``trusted_source``), and, when
  its keyless switch is on, answers it without a key.

Entries are a CIDR (``100.64.0.0/10``) or a host pattern (``host.example.net``
exactly, or ``*.example.net`` for any name under it). **A host pattern never
grants inbound trust:** the name a caller presents is a header it chose, so the
inbound side reads addresses only.

Refused entries, and why each one is load-bearing:

* a CIDR overlapping the platform's own Docker networks — agents and the public
  tunnel (cloudflared) reach the backend from there, so trusting them would
  make every agent, and the public internet, a keyless caller;
* loopback, link-local (cloud metadata lives at 169.254.169.254), unspecified
  and multicast ranges;
* anything broader than a /8 (IPv4) or a /32 (IPv6), and a host pattern of a
  single label (``*.com``).

Stored as a plain JSON list in ``system_settings`` under ``SETTING_KEY``; the
generic ``PUT /api/settings/{key}`` refuses that key so the guards above cannot
be walked around.
"""
from __future__ import annotations

import ipaddress
import json
import logging
from typing import Iterable, List, Optional, Sequence, Union

logger = logging.getLogger(__name__)

SETTING_KEY = "a2a_trusted_networks"
INTERNAL_BASE_URL_KEY = "a2a_internal_base_url"

MAX_ENTRIES = 32
MAX_ENTRY_LEN = 253

# The two bridges in docker-compose (Network Topology, #589). Agents sit on the
# agent network; cloudflared bridges both. Neither may ever be trusted.
PLATFORM_NETWORKS = (
    ipaddress.ip_network("172.28.0.0/16"),
    ipaddress.ip_network("172.29.0.0/16"),
)

# Ranges no trusted-network entry may touch.
_FORBIDDEN = PLATFORM_NETWORKS + (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("240.0.0.0/4"),
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("ff00::/8"),
)

_MIN_PREFIX = {4: 8, 6: 32}

# The ONE peer whose X-Real-IP is read rather than the peer itself: the
# platform's own reverse proxy (nginx in production, the Vite dev server
# locally), found by its container name. NOT "any Docker-bridge peer": agents
# sit on the same bridge and reach the backend directly, so trusting the header
# from any bridge address would let an agent name its own source. Any other
# peer IS the source.
_PROXY_HOSTS_ENV = "A2A_PROXY_HOSTS"
_PROXY_HOSTS_DEFAULT = "trinity-frontend"
_PROXY_CACHE_TTL = 60.0
_proxy_cache: dict = {"at": 0.0, "ips": frozenset()}

# Headers cloudflared adds to every request it forwards. Their presence means
# the request came through the public tunnel, whatever else it claims.
_PUBLIC_INGRESS_HEADERS = ("cf-connecting-ip", "cf-ray")

Network = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]


# A canonical (IDNA A-label) DNS name: labels of letters, digits and hyphens.
_HOST_RE = __import__("re").compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$")


class TrustedNetworkError(ValueError):
    """An entry the guards refuse. The message names the entry and the rule."""


def _parse_cidr(entry: str) -> Optional[Network]:
    try:
        return ipaddress.ip_network(entry, strict=False)
    except ValueError:
        return None


def _check_cidr(entry: str, net: Network) -> None:
    if net.prefixlen < _MIN_PREFIX[net.version]:
        raise TrustedNetworkError(
            f"{entry}: too broad — narrower than /{_MIN_PREFIX[net.version]} only")
    for bad in _FORBIDDEN:
        if bad.version == net.version and net.overlaps(bad):
            if bad in PLATFORM_NETWORKS:
                raise TrustedNetworkError(
                    f"{entry}: overlaps the platform's own Docker network {bad} — agents "
                    "and the public tunnel reach the backend from there")
            raise TrustedNetworkError(
                f"{entry}: loopback, link-local, multicast and reserved ranges cannot be trusted")


def _check_host_pattern(entry: str) -> str:
    from utils.url_validation import canonical_host

    wildcard = entry.startswith("*.")
    name = entry[2:] if wildcard else entry
    host = canonical_host(name)
    if not host or not _HOST_RE.match(host):
        raise TrustedNetworkError(f"{entry}: not a CIDR, a host name or *.domain")
    if wildcard and host.count(".") < 1:
        raise TrustedNetworkError(f"{entry}: a wildcard needs at least two labels (*.example.net)")
    return f"*.{host}" if wildcard else host


def normalize_entries(entries: Sequence[str]) -> List[str]:
    """Validate and canonicalise an admin's list. Raises ``TrustedNetworkError``.

    De-duplicated, order kept. An empty list is valid: it turns the feature off.
    """
    if not isinstance(entries, (list, tuple)):
        raise TrustedNetworkError("entries must be a list")
    if len(entries) > MAX_ENTRIES:
        raise TrustedNetworkError(f"at most {MAX_ENTRIES} entries")
    out: List[str] = []
    for raw in entries:
        if not isinstance(raw, str):
            raise TrustedNetworkError("every entry must be a string")
        entry = raw.strip().lower()
        if not entry or len(entry) > MAX_ENTRY_LEN:
            raise TrustedNetworkError("an entry is empty or too long")
        net = _parse_cidr(entry)
        if net is not None:
            _check_cidr(entry, net)
            value = str(net)
        else:
            value = _check_host_pattern(entry)
        if value not in out:
            out.append(value)
    return out


# The list is read on the anonymous A2A door BEFORE its rate limiter (it decides
# which budget applies), so it must not cost a DB read per request: cached per
# worker for a few seconds. A write here drops this worker's copy; another
# worker sees the change within the TTL.
_ENTRIES_TTL = 5.0
_entries_cache: dict = {"at": float("-inf"), "entries": []}


def get_entries() -> List[str]:
    """The stored list, re-validated on read. Fail-CLOSED: an unreadable or
    invalid row trusts nothing (and says so in the log)."""
    import time

    now = time.monotonic()
    if now - _entries_cache["at"] < _ENTRIES_TTL:
        return list(_entries_cache["entries"])
    from database import db

    try:
        raw = db.get_setting_value(SETTING_KEY, None)
        entries = normalize_entries(json.loads(raw)) if raw else []
    except Exception as exc:  # noqa: BLE001 — never widen on a bad row
        logger.warning("[ent#838] trusted networks unreadable, trusting none: %s", exc)
        entries = []
    _entries_cache.update(at=now, entries=entries)
    return list(entries)


def set_entries(entries: Sequence[str]) -> List[str]:
    from database import db

    clean = normalize_entries(entries)
    gateways = [ipaddress.ip_address(g) for g in _local_gateways()]
    for net in _networks(clean):
        hit = next((g for g in gateways if g.version == net.version and g in net), None)
        if hit is not None:
            raise TrustedNetworkError(
                f"{net}: contains {hit}, the gateway of a network this backend is attached "
                "to — calls the host forwards (a published port) arrive from there, so "
                "trusting it would trust them")
    db.set_setting(SETTING_KEY, json.dumps(clean))
    _entries_cache.update(at=float("-inf"), entries=[])
    return clean


def _networks(entries: Iterable[str]) -> List[Network]:
    return [n for n in (_parse_cidr(e) for e in entries) if n is not None]


def host_is_trusted(hostname: str, entries: Optional[Sequence[str]] = None) -> bool:
    """Outbound only: the endpoint's canonical host matches an exact name or a
    ``*.suffix`` entry."""
    entries = get_entries() if entries is None else entries
    host = (hostname or "").lower().rstrip(".")
    if not host:
        return False
    for e in entries:
        if e.startswith("*."):
            if host.endswith(e[1:]):
                return True
        elif _parse_cidr(e) is None and host == e:
            return True
    return False


def address_is_trusted(address: str, entries: Optional[Sequence[str]] = None) -> bool:
    entries = get_entries() if entries is None else entries
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return any(ip.version == n.version and ip in n for n in _networks(entries))


_GATEWAY_CACHE_TTL = 60.0
_gateway_cache: dict = {"at": float("-inf"), "ips": frozenset()}


def _parse_route_table(text: str) -> frozenset:
    """Gateway addresses from ``/proc/net/route`` text (little-endian hex).

    The default route's gateway, plus the first host of every directly
    connected network — the address a Docker bridge's gateway takes. Traffic
    the host forwards into this container (a call to a published port from the
    host or from another container) arrives FROM that address, so it says
    nothing about who called.
    """
    import socket
    import struct

    ips = set()
    for line in text.splitlines()[1:]:
        cols = line.split()
        if len(cols) < 8:
            continue
        try:
            dest, gateway, mask = (int(cols[i], 16) for i in (1, 2, 7))
        except ValueError:
            continue
        if gateway:
            ips.add(socket.inet_ntoa(struct.pack("<I", gateway)))
        elif dest and mask:
            network = struct.unpack(">I", struct.pack("<I", dest))[0]
            ips.add(socket.inet_ntoa(struct.pack(">I", network + 1)))
    return frozenset(ips)


def _local_gateways() -> frozenset:
    """The gateways of the networks this backend is attached to, cached. A
    failed read is an empty set (the CIDR and platform guards still apply)."""
    import time

    now = time.monotonic()
    if now - _gateway_cache["at"] < _GATEWAY_CACHE_TTL:
        return _gateway_cache["ips"]
    try:
        with open("/proc/net/route", encoding="ascii") as f:
            ips = _parse_route_table(f.read())
    except OSError:
        ips = frozenset()
    _gateway_cache.update(at=now, ips=ips)
    return ips


def _proxy_ips() -> frozenset:
    """The platform proxy's addresses, resolved by container name and cached.
    A failed lookup is an empty set: the header is then never read, and the
    peer — a bridge address no entry may trust — is the source (fail closed)."""
    import os
    import socket
    import time

    now = time.monotonic()
    if now - _proxy_cache["at"] < _PROXY_CACHE_TTL:
        return _proxy_cache["ips"]
    ips = set()
    for host in os.environ.get(_PROXY_HOSTS_ENV, _PROXY_HOSTS_DEFAULT).split(","):
        host = host.strip()
        if not host:
            continue
        try:
            for info in socket.getaddrinfo(host, None):
                ips.add(info[4][0])
        except OSError:
            continue
    _proxy_cache.update(at=now, ips=frozenset(ips))
    return _proxy_cache["ips"]


def _source_address(request) -> Optional[str]:
    peer = request.client.host if getattr(request, "client", None) else None
    if not peer:
        return None
    if peer in _proxy_ips():
        real = (request.headers.get("x-real-ip") or "").strip()
        return real or peer
    return peer


def trusted_source(request, entries: Optional[Sequence[str]] = None) -> Optional[str]:
    """The caller's address when it is inside a trusted CIDR, else ``None``.

    * A request carrying cloudflared's headers came through the public tunnel:
      never trusted, whatever address it claims.
    * When the peer is the platform's own proxy (by container name), the
      source is its ``X-Real-IP`` — nginx overwrites it with ``$remote_addr``
      and the Vite dev proxy sets it from the socket, so a client cannot
      choose it. Any other peer — an agent on the bridge included — is the
      source itself.
    """
    if any(request.headers.get(h) for h in _PUBLIC_INGRESS_HEADERS):
        return None
    entries = get_entries() if entries is None else entries
    if not entries:
        return None
    source = _source_address(request)
    if not source:
        return None
    plain = source[7:] if source.startswith("::ffff:") else source
    if plain in _local_gateways():
        # Forwarded by the host (a published port, hairpin NAT): the real
        # caller is unknowable from here, so never trusted (#838 live test).
        return None
    if address_is_trusted(source, entries):
        return source
    return None


def internal_base_url() -> Optional[str]:
    """The origin an internal-scope agent's card advertises, or ``None``."""
    from database import db

    try:
        raw = (db.get_setting_value(INTERNAL_BASE_URL_KEY, None) or "").strip()
    except Exception:  # noqa: BLE001
        return None
    return raw.rstrip("/") or None


def validate_internal_base_url(url: str) -> str:
    """An ``http(s)://host[:port]`` origin with no path, query or credentials."""
    from urllib.parse import urlsplit

    url = (url or "").strip()
    if not url:
        return ""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise TrustedNetworkError("internal base URL must be http(s)://host[:port]")
    if parts.username or parts.password or "@" in parts.netloc:
        raise TrustedNetworkError("internal base URL must not carry credentials")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise TrustedNetworkError("internal base URL is an origin only — no path or query")
    return f"{parts.scheme}://{parts.netloc}"
