"""Verifier side: resolve a caller's key from its published metadata (ent#623).

``{iss}/.well-known/aauth-agent.json`` → ``jwks_uri`` → key by ``kid``.

Every fetch goes through the outbound A2A egress (``a2a_client``): public-HTTPS
validation, connect-time IP pinning with the issuer host carried for SNI and
certificate verification, no redirects, ``trust_env=False``, a wire-byte cap.
A caller names the issuer, so this is a server-side fetch an unauthenticated
party can trigger; the router only calls it for issuers an operator listed
(the trusted-issuer pre-gate), and three more bounds hold here:

* one total deadline over metadata + JWKS (``_read_capped`` only has per-read
  timeouts, which a trickling peer resets forever);
* ``jwks_uri`` must be on the issuer's own origin — otherwise a listed peer
  could point this backend at any public URL;
* one fetch per issuer at a time, results (and failures) cached, and a forced
  refetch for an unknown ``kid`` at most once a minute.

Caches are per worker; a JWKS is public data, so a divergent cache between two
workers is a latency difference, never a trust difference.
"""
from __future__ import annotations

import asyncio
import functools
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import urlsplit

import httpx

from services.aauth import jose

logger = logging.getLogger(__name__)

METADATA_PATH = "/.well-known/aauth-agent.json"
#: ent#879: the same metadata → ``jwks_uri`` → key shape serves a RESOURCE's
#: ``aauth-resource.json`` (the resource token's ``dwk``). The well-known
#: document name is the only difference; the same-origin rule, the caps and
#: the caches apply unchanged, keyed by ``(issuer, dwk)``.
AGENT_DOCUMENT = "aauth-agent.json"
RESOURCE_DOCUMENT = "aauth-resource.json"
#: ent#928: an ACCESS server's document (four-party). Only its ``issuer`` is
#: read — :func:`access_server_metadata` — never a key: the auth token an AS
#: mints is inspected, not signature-checked (ent#879 T5), so the AS's JWKS
#: has no consumer here and is not fetched.
ACCESS_DOCUMENT = "aauth-access.json"
KNOWN_DOCUMENTS = frozenset({AGENT_DOCUMENT, RESOURCE_DOCUMENT})
MAX_DOCUMENT_BYTES = 64 * 1024
DISCOVERY_DEADLINE = 8.0
CACHE_TTL = 300.0
NEGATIVE_TTL = 60.0
MIN_REFETCH_INTERVAL = 60.0


class DiscoveryError(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass
class _Entry:
    fetched_at: float
    keys: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    error: Optional[DiscoveryError] = None


@dataclass
class _DocumentEntry:
    fetched_at: float
    document: Optional[Dict[str, Any]] = None
    error: Optional[DiscoveryError] = None


_cache: Dict[Tuple[str, str], _Entry] = {}
_document_cache: Dict[Tuple[str, str], _DocumentEntry] = {}
_locks: Dict[Tuple[str, str], asyncio.Lock] = {}


def _document_or_raise(dwk: str) -> str:
    if dwk not in KNOWN_DOCUMENTS:
        raise DiscoveryError("unknown_key", f"'{dwk}' is not a well-known AAuth document")
    return dwk


def _same_origin(a: str, b: str) -> bool:
    try:
        pa, pb = urlsplit(a), urlsplit(b)
        pa.port, pb.port      # a junk port raises ValueError, not a mismatch
    except ValueError:
        return False
    return (
        pa.scheme == pb.scheme == "https"
        and (pa.hostname or "") == (pb.hostname or "")
        and (pa.port or 443) == (pb.port or 443)
    )


async def _get_json(client: httpx.AsyncClient, url: str) -> Any:
    from services import a2a_client
    from services.a2a_client import A2ACallError

    try:
        validated = await a2a_client.validate_endpoint(url)
        raw = await a2a_client._read_capped(
            client,
            "GET",
            a2a_client._pinned_url(url, validated.addresses[0]),
            sni=validated.hostname,
            host_header=a2a_client._host_header(validated),
            max_bytes=MAX_DOCUMENT_BYTES,
            headers={"Accept": "application/json"},
            error_prefix="aauth_discovery",
        )
    except A2ACallError as exc:
        # One adapter: the egress speaks "The A2A endpoint …", this caller
        # speaks AAuth. The reason survives for the log, not the peer.
        logger.info("[aauth] discovery fetch refused (%s)", exc.reason)
        raise DiscoveryError("unknown_key", f"could not fetch issuer metadata ({exc.reason})") from None
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise DiscoveryError("unknown_key", "issuer metadata is not JSON") from None


async def fetch_issuer_keys(issuer: str, client_factory: Optional[Callable] = None, *,
                            dwk: str = AGENT_DOCUMENT) -> Dict[str, Dict[str, Any]]:
    """Fetch and validate an issuer's metadata + JWKS. Returns ``{kid: jwk}``.

    ``dwk`` names the well-known document (``aauth-agent.json`` for an agent
    provider, ``aauth-resource.json`` for a resource — ent#879).
    """
    from services import a2a_client

    document_name = _document_or_raise(dwk)
    factory = client_factory or a2a_client._http_client
    timeout = httpx.Timeout(DISCOVERY_DEADLINE, connect=5.0)
    async with factory(timeout) as client:
        metadata = await _get_json(client, f"{issuer}/.well-known/{document_name}")
        if not isinstance(metadata, dict):
            raise DiscoveryError("unknown_key", "issuer metadata is not an object")
        if "issuer" not in metadata:
            raise DiscoveryError("issuer_missing", "issuer metadata has no issuer")
        if metadata.get("issuer") != issuer:
            raise DiscoveryError("issuer_mismatch", "issuer metadata names a different issuer")
        jwks_uri = metadata.get("jwks_uri")
        if not isinstance(jwks_uri, str) or not _same_origin(jwks_uri, issuer):
            raise DiscoveryError("unknown_key", "jwks_uri is missing or not on the issuer's origin")
        document = await _get_json(client, jwks_uri)
    raw_keys = document.get("keys") if isinstance(document, dict) else None
    if not isinstance(raw_keys, list):
        raise DiscoveryError("unknown_key", "issuer JWKS has no keys")
    out: Dict[str, Dict[str, Any]] = {}
    for jwk in raw_keys[:20]:
        if not isinstance(jwk, dict) or not isinstance(jwk.get("kid"), str):
            continue
        try:
            jose.jwk_to_public_key(jwk)
        except jose.JoseError:
            continue   # an unusable key is simply not a candidate
        out[jwk["kid"]] = jwk
    return out


async def resolve_key(issuer: str, kid: str, *, now: Optional[float] = None,
                      fetcher: Optional[Callable] = None, dwk: str = AGENT_DOCUMENT) -> Dict[str, Any]:
    """The issuer's public JWK for ``kid``, from cache or a bounded fetch.

    The cache is keyed by ``(issuer, dwk)``: an agent provider and a resource
    on one host publish two documents, and a key found under one must never
    answer for the other.
    """
    clock = time.monotonic if now is None else (lambda: now)
    document_name = _document_or_raise(dwk)
    fetch = fetcher or functools.partial(fetch_issuer_keys, dwk=document_name)
    cache_key = (issuer, document_name)

    def _from(entry: _Entry) -> Optional[Dict[str, Any]]:
        if entry.error is not None:
            raise entry.error
        return entry.keys.get(kid)

    entry = _cache.get(cache_key)
    if entry is not None:
        age = clock() - entry.fetched_at
        fresh = age < (NEGATIVE_TTL if entry.error else CACHE_TTL)
        if fresh:
            found = _from(entry)
            if found is not None:
                return found
            if age < MIN_REFETCH_INTERVAL:
                raise DiscoveryError("unknown_key", "no key with that kid in the issuer JWKS")

    lock = _locks.setdefault(cache_key, asyncio.Lock())
    async with lock:
        latest = _cache.get(cache_key)
        if latest is not None and latest is not entry and clock() - latest.fetched_at < MIN_REFETCH_INTERVAL:
            found = _from(latest)   # someone refreshed while we waited
            if found is not None:
                return found
            raise DiscoveryError("unknown_key", "no key with that kid in the issuer JWKS")
        try:
            keys = await asyncio.wait_for(fetch(issuer), timeout=DISCOVERY_DEADLINE)
            _cache[cache_key] = _Entry(fetched_at=clock(), keys=keys)
        except asyncio.TimeoutError:
            err = DiscoveryError("unknown_key", "issuer metadata fetch timed out")
            _cache[cache_key] = _Entry(fetched_at=clock(), error=err)
            raise err from None
        except DiscoveryError as err:
            _cache[cache_key] = _Entry(fetched_at=clock(), error=err)
            raise
        except Exception:  # noqa: BLE001 — a malformed document must not become
            # a 500 that ALSO skips the negative cache, i.e. an unbounded refetch
            # for every request naming that issuer.
            logger.warning("[aauth] discovery failed for %s", issuer, exc_info=True)
            err = DiscoveryError("unknown_key", "issuer metadata could not be read")
            _cache[cache_key] = _Entry(fetched_at=clock(), error=err)
            raise err from None
    found = _cache[cache_key].keys.get(kid)
    if found is None:
        raise DiscoveryError("unknown_key", "no key with that kid in the issuer JWKS")
    return found


async def fetch_access_server_document(issuer: str, client_factory: Optional[Callable] = None) -> Dict[str, Any]:
    """``{issuer}/.well-known/aauth-access.json``, validated: an object whose ``issuer`` is ``issuer``.

    The same egress as every other discovery fetch (:func:`_get_json`: public
    URL validation, IP pinning, no redirects, the 64 KB cap). Raises
    :class:`DiscoveryError` — ``issuer_mismatch`` when the document names
    another server, ``unknown_key`` for everything that could not be fetched
    or read (the vocabulary the key path already uses).
    """
    from services import a2a_client

    factory = client_factory or a2a_client._http_client
    timeout = httpx.Timeout(DISCOVERY_DEADLINE, connect=5.0)
    async with factory(timeout) as client:
        metadata = await _get_json(client, f"{issuer}/.well-known/{ACCESS_DOCUMENT}")
    if not isinstance(metadata, dict):
        raise DiscoveryError("unknown_key", "access server metadata is not an object")
    if "issuer" not in metadata:
        raise DiscoveryError("issuer_missing", "access server metadata has no issuer")
    if metadata.get("issuer") != issuer:
        raise DiscoveryError("issuer_mismatch", "access server metadata names a different issuer")
    return metadata


async def access_server_metadata(issuer: str, *, now: Optional[float] = None,
                                 fetcher: Optional[Callable] = None) -> Dict[str, Any]:
    """An access server's published metadata (ent#928), from cache or a bounded fetch.

    Same discipline as :func:`resolve_key`: one fetch per ``(issuer, document)``
    at a time, positive results for ``CACHE_TTL``, failures for ``NEGATIVE_TTL``,
    a malformed document negative-cached rather than refetched per request.
    The caller (``resource_token``) treats any :class:`DiscoveryError` as
    "not a discoverable access server".
    """
    clock = time.monotonic if now is None else (lambda: now)
    fetch = fetcher or fetch_access_server_document
    cache_key = (issuer, ACCESS_DOCUMENT)

    def _from(entry: _DocumentEntry) -> Dict[str, Any]:
        if entry.error is not None:
            raise entry.error
        return entry.document or {}

    entry = _document_cache.get(cache_key)
    if entry is not None and clock() - entry.fetched_at < (NEGATIVE_TTL if entry.error else CACHE_TTL):
        return _from(entry)
    lock = _locks.setdefault(cache_key, asyncio.Lock())
    async with lock:
        latest = _document_cache.get(cache_key)
        if latest is not None and latest is not entry and clock() - latest.fetched_at < MIN_REFETCH_INTERVAL:
            return _from(latest)   # someone refreshed while we waited
        try:
            document = await asyncio.wait_for(fetch(issuer), timeout=DISCOVERY_DEADLINE)
            _document_cache[cache_key] = _DocumentEntry(fetched_at=clock(), document=document)
        except asyncio.TimeoutError:
            err = DiscoveryError("unknown_key", "access server metadata fetch timed out")
            _document_cache[cache_key] = _DocumentEntry(fetched_at=clock(), error=err)
            raise err from None
        except DiscoveryError as err:
            _document_cache[cache_key] = _DocumentEntry(fetched_at=clock(), error=err)
            raise
        except Exception:  # noqa: BLE001 — same reason as resolve_key: never an unbounded refetch
            logger.warning("[aauth] access server discovery failed for %s", issuer, exc_info=True)
            err = DiscoveryError("unknown_key", "access server metadata could not be read")
            _document_cache[cache_key] = _DocumentEntry(fetched_at=clock(), error=err)
            raise err from None
    return document


def reset_cache() -> None:
    """Tests only."""
    _cache.clear()
    _document_cache.clear()
    _locks.clear()
