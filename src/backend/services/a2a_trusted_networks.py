"""The admin's outbound A2A endpoint allowlist (trinity-enterprise#838, narrowed
to outbound only by trinity-enterprise#885).

An operator running several Trinity instances on one private network (a
tailnet, a VPN) declares that network here, once, as an admin. ONE thing then
follows, and nothing else changes:

* **Outbound** — an A2A endpoint whose host matches a trusted entry may resolve
  to a private or CGNAT address and may use ``http://``
  (``utils/url_validation.validate_a2a_endpoint_url``). Every OTHER endpoint
  keeps the public-HTTPS rule, and the check still runs on every call, so a
  trusted name that later resolves outside the trusted set is refused.

Nothing inbound reads this list. The keyless inbound path #3361 built on it
(an ``internal``-scope agent answering a trusted source without a credential)
was dropped before 1.0 (ent#885, operator ruling 2026-10-10): a caller with no
Trinity credential gets the 401, whatever network it arrives from.

Entries are a CIDR (``100.64.0.0/10``) or a host pattern (``host.example.net``
exactly, or ``*.example.net`` for any name under it).

Refused entries, and why each one is load-bearing:

* a CIDR overlapping the platform's own Docker networks — agents and the public
  tunnel (cloudflared) reach the backend from there;
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


# The list is read on every outbound call (`validate_a2a_endpoint_url` re-runs
# per call), so it must not cost a DB read per request: cached per worker for a
# few seconds. A write here drops this worker's copy; another worker sees the
# change within the TTL.
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
