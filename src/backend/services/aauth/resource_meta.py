"""Resource metadata and the ``AAuth-Budget`` header (ent#879).

``{resource}/.well-known/aauth-resource.json`` → the fields the outbound
resource client needs before it signs anything to that resource:

* ``issuer`` must equal the resource the agent named (the allow-list judged
  that string; the document confirms it, it never re-points it);
* ``jwks_uri`` must be on the resource's own origin — local policy (the #623
  discovery rule), stated as such: a listed peer must not steer this backend
  at any public URL on its say-so. Verified live: Senzing complies;
* ``authorization_endpoint`` / ``revocation_endpoint`` / ``usage_endpoint``,
  when present, must be absolute https URLs on the resource's origin; their
  PATHS are the control surface the agent-supplied ``path`` must never hit;
* ``access_mode`` ∈ agent-token | person-token | auth-token (anything else is
  carried as ``unknown`` and treated like absent: start at the agent token);
* ``additional_signature_components`` is read as-is; the ladder refuses the
  call (``unsupported_signature_components``) when it names a component the
  signer cannot produce, never silently covering fewer;
* ``signature_window`` is read for the record (the resource enforces it).

``AAuth-Budget`` is NOT in the protocol draft (verified 2026-10-13); it is a
Senzing/R3-side RFC 8941 dictionary (``cost``, ``balance``, ``unit``-shaped
members). :func:`parse_budget` is opportunistic and tolerant — anything it
cannot read is ``None``, never a failure.

Same fetch discipline as ``person_server.py``: pinned egress through
``egress.request_capped``, 64 KB cap, one fetch per resource at a time,
positive and negative caches per worker.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import urlsplit

import httpx

from services.aauth import discovery, egress, requirement

logger = logging.getLogger(__name__)

METADATA_PATH = "/.well-known/aauth-resource.json"
MAX_DOCUMENT_BYTES = 64 * 1024
DISCOVERY_DEADLINE = 8.0
CACHE_TTL = 300.0
NEGATIVE_TTL = 60.0

ACCESS_MODES = ("agent-token", "person-token", "auth-token")
CONTROL_ENDPOINT_FIELDS = ("authorization_endpoint", "revocation_endpoint", "usage_endpoint")
DEFAULT_SIGNATURE_WINDOW = 60
MAX_COMPONENT_NAME = 64
_COMPONENT_RE = re.compile(r"^[a-z@][a-z0-9-]{0,63}$")
_UNIT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,31}$")


class ResourceMetadataError(Exception):
    """``code`` ∈ resource_unreachable | resource_metadata_invalid."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class ResourceMetadata:
    issuer: str
    jwks_uri: str
    access_mode: Optional[str] = None                    # one of ACCESS_MODES, "unknown", or None
    authorization_endpoint: Optional[str] = None
    revocation_endpoint: Optional[str] = None
    usage_endpoint: Optional[str] = None
    scopes: Tuple[str, ...] = ()
    signature_window: int = DEFAULT_SIGNATURE_WINDOW
    additional_signature_components: Tuple[str, ...] = ()

    def control_paths(self) -> Tuple[str, ...]:
        """The paths of the declared control endpoints — never a target for
        an agent-supplied ``path`` (M3)."""
        out = []
        for url in (self.authorization_endpoint, self.revocation_endpoint, self.usage_endpoint):
            if url:
                out.append(urlsplit(url).path or "/")
        return tuple(out)

    def starts_with_person_token(self) -> bool:
        """Draft §Resource Metadata: an ``auth-token``/``person-token`` resource
        MUST be approached with a person token; an agent token first wastes a trip."""
        return self.access_mode in ("person-token", "auth-token")


@dataclass(frozen=True)
class Budget:
    cost: Optional[int] = None
    balance: Optional[int] = None
    unit: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {"cost": self.cost, "balance": self.balance, "unit": self.unit}


@dataclass
class _Entry:
    fetched_at: float
    metadata: Optional[ResourceMetadata] = None
    error: Optional[ResourceMetadataError] = None


_cache: Dict[str, _Entry] = {}
_locks: Dict[str, asyncio.Lock] = {}


def _https_absolute(value: Any) -> bool:
    if not isinstance(value, str) or not value or len(value) > 2048:
        return False
    try:
        parts = urlsplit(value)
        parts.port
    except ValueError:
        return False
    return (parts.scheme == "https" and bool(parts.hostname) and parts.username is None
            and not parts.query and not parts.fragment)


def _invalid(detail: str) -> ResourceMetadataError:
    return ResourceMetadataError("resource_metadata_invalid", detail)


def parse_metadata(document: Any, resource: str) -> ResourceMetadata:
    """Validate a fetched document against the rules in the module docstring."""
    if not isinstance(document, dict):
        raise _invalid("resource metadata is not an object")
    issuer = document.get("issuer")
    if not isinstance(issuer, str) or not issuer:
        raise _invalid("resource metadata has no issuer")
    if issuer != resource:
        raise _invalid("resource metadata names a different issuer")
    jwks_uri = document.get("jwks_uri")
    if not _https_absolute(jwks_uri):
        raise _invalid("resource jwks_uri is not an absolute https URL")
    if not discovery._same_origin(jwks_uri, resource):
        raise ResourceMetadataError("resource_metadata_invalid:jwks_uri_origin",
                                    "resource jwks_uri is not on the resource's origin (local policy)")
    endpoints: Dict[str, Optional[str]] = {}
    for name in CONTROL_ENDPOINT_FIELDS:
        value = document.get(name)
        if value is None:
            endpoints[name] = None
            continue
        if not _https_absolute(value):
            raise _invalid(f"resource {name} is not an absolute https URL")
        if not discovery._same_origin(value, resource):
            raise _invalid(f"resource {name} is not on the resource's origin")
        endpoints[name] = value
    mode = document.get("access_mode")
    if mode is None:
        access_mode = None
    elif isinstance(mode, str) and mode in ACCESS_MODES:
        access_mode = mode
    else:
        access_mode = "unknown"
    raw_scopes = document.get("scopes_supported")
    if raw_scopes is None:
        raw_scopes = document.get("scopes")
    scopes: Tuple[str, ...] = ()
    if isinstance(raw_scopes, list):
        scopes = tuple(s for s in raw_scopes[:50] if isinstance(s, str) and 0 < len(s) <= 128)
    window = document.get("signature_window")
    if isinstance(window, bool) or not isinstance(window, int) or not (1 <= window <= 3600):
        window = DEFAULT_SIGNATURE_WINDOW
    raw_components = document.get("additional_signature_components")
    components: Tuple[str, ...] = ()
    if raw_components is not None:
        if not isinstance(raw_components, list):
            raise _invalid("resource additional_signature_components is not a list")
        cleaned = []
        for item in raw_components[:20]:
            if not isinstance(item, str) or not _COMPONENT_RE.match(item):
                raise _invalid("resource additional_signature_components names a malformed component")
            cleaned.append(item)
        components = tuple(cleaned)
    return ResourceMetadata(
        issuer=issuer, jwks_uri=jwks_uri, access_mode=access_mode,
        authorization_endpoint=endpoints["authorization_endpoint"],
        revocation_endpoint=endpoints["revocation_endpoint"],
        usage_endpoint=endpoints["usage_endpoint"],
        scopes=scopes, signature_window=window, additional_signature_components=components,
    )


async def fetch_metadata(resource: str, client_factory: Optional[Callable] = None) -> ResourceMetadata:
    """One bounded, pinned GET of the resource metadata document, validated."""
    from services import a2a_client

    factory = client_factory or a2a_client._http_client
    timeout = httpx.Timeout(DISCOVERY_DEADLINE, connect=5.0)
    try:
        async with factory(timeout) as client:
            resp = await egress.request_capped(
                client, "GET", resource + METADATA_PATH,
                max_bytes=MAX_DOCUMENT_BYTES, headers={"Accept": "application/json"},
            )
    except egress.EgressError as exc:
        logger.info("[aauth] resource metadata fetch refused (%s)", exc.code)
        raise ResourceMetadataError("resource_unreachable",
                                    f"could not fetch resource metadata ({exc.code})") from None
    if resp.status != 200:
        raise ResourceMetadataError("resource_unreachable",
                                    f"resource metadata answered HTTP {resp.status}")
    try:
        document = json.loads(resp.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise _invalid("resource metadata is not JSON") from None
    return parse_metadata(document, resource)


async def metadata(resource: str, *, now: Optional[float] = None,
                   fetcher: Optional[Callable] = None) -> ResourceMetadata:
    """The resource metadata from cache or one bounded, single-flight fetch."""
    clock = time.monotonic if now is None else (lambda: now)
    fetch = fetcher or fetch_metadata

    def _from(entry: _Entry) -> ResourceMetadata:
        if entry.error is not None:
            raise entry.error
        assert entry.metadata is not None
        return entry.metadata

    entry = _cache.get(resource)
    if entry is not None and clock() - entry.fetched_at < (NEGATIVE_TTL if entry.error else CACHE_TTL):
        return _from(entry)

    lock = _locks.setdefault(resource, asyncio.Lock())
    async with lock:
        latest = _cache.get(resource)
        if latest is not None and latest is not entry:
            return _from(latest)
        try:
            result = await asyncio.wait_for(fetch(resource), timeout=DISCOVERY_DEADLINE)
            _cache[resource] = _Entry(fetched_at=clock(), metadata=result)
        except asyncio.TimeoutError:
            err = ResourceMetadataError("resource_unreachable", "resource metadata fetch timed out")
            _cache[resource] = _Entry(fetched_at=clock(), error=err)
            raise err from None
        except ResourceMetadataError as err:
            _cache[resource] = _Entry(fetched_at=clock(), error=err)
            raise
        except Exception:  # noqa: BLE001 — a malformed document must not skip the negative cache
            logger.warning("[aauth] resource discovery failed for %s", resource, exc_info=True)
            err = _invalid("resource metadata could not be read")
            _cache[resource] = _Entry(fetched_at=clock(), error=err)
            raise err from None
    return result


# --------------------------------------------------------------------------- #
# AAuth-Budget (tolerant; never required)
# --------------------------------------------------------------------------- #
def _int_member(members, key: str) -> Optional[int]:
    entry = members.get(key)
    if entry is None or entry[0] is None:
        return None
    text = entry[0]
    if not re.fullmatch(r"-?[0-9]{1,15}", text):
        return None
    return int(text)


def parse_budget(value: Optional[str]) -> Optional[Budget]:
    """``AAuth-Budget: cost=2, balance=38, unit=credits`` → Budget; anything
    unreadable → None."""
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        return None
    try:
        members = requirement.parse_sf_dictionary(value)
    except requirement.RequirementFormatError:
        return None
    cost = _int_member(members, "cost")
    balance = _int_member(members, "balance")
    unit_entry = members.get("unit")
    unit = unit_entry[0] if unit_entry and unit_entry[0] and _UNIT_RE.match(unit_entry[0]) else None
    if cost is None and balance is None and unit is None:
        return None
    return Budget(cost=cost, balance=balance, unit=unit)


def reset_cache() -> None:
    """Tests only."""
    _cache.clear()
    _locks.clear()
