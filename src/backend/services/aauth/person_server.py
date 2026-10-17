"""Person-server metadata discovery (ent#878).

``{ps}/.well-known/aauth-person.json`` → the four REQUIRED fields
(``issuer``, ``jwks_uri``, ``auth_token_endpoint``, ``person_token_endpoint``)
plus the optional ``interaction_endpoint`` / ``revocation_endpoint``.

Same fetch discipline as ``discovery.py`` (pinned egress, 64 KB cap, one
fetch per issuer at a time, positive and negative caches per worker) with
**different rules**, because a person server is not an agent provider:

* ``issuer`` must equal the configured PS — the operator typed it, the
  document confirms it;
* the two token endpoints must be on the PS's own origin — they are the only
  URLs this backend sends a SIGNED request to on the PS's say-so;
* ``jwks_uri`` MAY be on another host (Hellō publishes it on
  ``issuer.hello.coop``; the #623 same-origin rule does not apply here);
* every endpoint is an absolute https URL.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlsplit

import httpx

from services.aauth import discovery, egress

logger = logging.getLogger(__name__)

METADATA_PATH = "/.well-known/aauth-person.json"
MAX_DOCUMENT_BYTES = 64 * 1024
DISCOVERY_DEADLINE = 8.0
CACHE_TTL = 300.0
NEGATIVE_TTL = 60.0

REQUIRED_FIELDS = ("issuer", "jwks_uri", "auth_token_endpoint", "person_token_endpoint")
SAME_ORIGIN_FIELDS = ("auth_token_endpoint", "person_token_endpoint")
OPTIONAL_FIELDS = ("interaction_endpoint", "revocation_endpoint")


class PersonServerError(Exception):
    """``code`` ∈ person_server_unreachable | person_server_metadata_invalid."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class PersonServerMetadata:
    issuer: str
    person_token_endpoint: str
    auth_token_endpoint: str
    jwks_uri: str
    interaction_endpoint: Optional[str] = None
    revocation_endpoint: Optional[str] = None

    def interaction_origins(self) -> set:
        """Origins an interaction ``url`` may live on: the PS, or its interaction endpoint."""
        out = {_origin(self.issuer)}
        if self.interaction_endpoint:
            out.add(_origin(self.interaction_endpoint))
        return out


@dataclass
class _Entry:
    fetched_at: float
    metadata: Optional[PersonServerMetadata] = None
    error: Optional[PersonServerError] = None


_cache: Dict[str, _Entry] = {}
_locks: Dict[str, asyncio.Lock] = {}


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{(parts.hostname or '').lower()}:{parts.port or 443}"


def _https_absolute(value: Any) -> bool:
    if not isinstance(value, str) or not value or len(value) > 2048:
        return False
    try:
        parts = urlsplit(value)
        parts.port
    except ValueError:
        return False
    return parts.scheme == "https" and bool(parts.hostname) and parts.username is None and not parts.fragment


def parse_metadata(document: Any, issuer: str) -> PersonServerMetadata:
    """Validate a fetched document against the rules in the module docstring."""
    if not isinstance(document, dict):
        raise PersonServerError("person_server_metadata_invalid", "person server metadata is not an object")
    for name in REQUIRED_FIELDS:
        if not isinstance(document.get(name), str) or not document.get(name):
            raise PersonServerError("person_server_metadata_invalid", f"person server metadata has no {name}")
    if document["issuer"] != issuer:
        raise PersonServerError("person_server_metadata_invalid", "person server metadata names a different issuer")
    for name in REQUIRED_FIELDS[1:] + OPTIONAL_FIELDS:
        value = document.get(name)
        if value is None and name in OPTIONAL_FIELDS:
            continue
        if not _https_absolute(value):
            raise PersonServerError("person_server_metadata_invalid", f"person server {name} is not an absolute https URL")
    for name in SAME_ORIGIN_FIELDS:
        if not discovery._same_origin(document[name], issuer):
            raise PersonServerError("person_server_metadata_invalid", f"person server {name} is not on the issuer's origin")
    return PersonServerMetadata(
        issuer=issuer,
        person_token_endpoint=document["person_token_endpoint"],
        auth_token_endpoint=document["auth_token_endpoint"],
        jwks_uri=document["jwks_uri"],
        interaction_endpoint=document.get("interaction_endpoint"),
        revocation_endpoint=document.get("revocation_endpoint"),
    )


async def fetch_metadata(issuer: str, client_factory: Optional[Callable] = None) -> PersonServerMetadata:
    """One bounded, pinned GET of the PS metadata document, validated."""
    from services import a2a_client

    factory = client_factory or a2a_client._http_client
    timeout = httpx.Timeout(DISCOVERY_DEADLINE, connect=5.0)
    try:
        async with factory(timeout) as client:
            resp = await egress.request_capped(
                client, "GET", issuer + METADATA_PATH,
                max_bytes=MAX_DOCUMENT_BYTES, headers={"Accept": "application/json"},
            )
    except egress.EgressError as exc:
        logger.info("[aauth] person server metadata fetch refused (%s)", exc.code)
        raise PersonServerError("person_server_unreachable",
                                f"could not fetch person server metadata ({exc.code})") from None
    if resp.status != 200:
        raise PersonServerError("person_server_unreachable",
                                f"person server metadata answered HTTP {resp.status}")
    try:
        document = json.loads(resp.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise PersonServerError("person_server_metadata_invalid", "person server metadata is not JSON") from None
    return parse_metadata(document, issuer)


async def metadata(issuer: str, *, now: Optional[float] = None,
                   fetcher: Optional[Callable] = None) -> PersonServerMetadata:
    """The PS metadata from cache or one bounded, single-flight fetch."""
    clock = time.monotonic if now is None else (lambda: now)
    fetch = fetcher or fetch_metadata

    def _from(entry: _Entry) -> PersonServerMetadata:
        if entry.error is not None:
            raise entry.error
        assert entry.metadata is not None
        return entry.metadata

    entry = _cache.get(issuer)
    if entry is not None and clock() - entry.fetched_at < (NEGATIVE_TTL if entry.error else CACHE_TTL):
        return _from(entry)

    lock = _locks.setdefault(issuer, asyncio.Lock())
    async with lock:
        latest = _cache.get(issuer)
        if latest is not None and latest is not entry:
            return _from(latest)   # someone refreshed while we waited
        try:
            result = await asyncio.wait_for(fetch(issuer), timeout=DISCOVERY_DEADLINE)
            _cache[issuer] = _Entry(fetched_at=clock(), metadata=result)
        except asyncio.TimeoutError:
            err = PersonServerError("person_server_unreachable", "person server metadata fetch timed out")
            _cache[issuer] = _Entry(fetched_at=clock(), error=err)
            raise err from None
        except PersonServerError as err:
            _cache[issuer] = _Entry(fetched_at=clock(), error=err)
            raise
        except Exception:  # noqa: BLE001 — a malformed document must not skip the negative cache
            logger.warning("[aauth] person server discovery failed for %s", issuer, exc_info=True)
            err = PersonServerError("person_server_metadata_invalid", "person server metadata could not be read")
            _cache[issuer] = _Entry(fetched_at=clock(), error=err)
            raise err from None
    return result


def reset_cache() -> None:
    """Tests only."""
    _cache.clear()
    _locks.clear()
