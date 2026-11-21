"""Registration terms at first-run setup — the OSS seam (abilityai/trinity-enterprise#1329).

The shape of ``terms_gate.register_provider``: an optional module may register
ONE provider of registration terms — the terms a person accepts when they
become the registered admin of an instance set up from an emailed link
(PROV-020). With none registered this module is inert: ``provider()`` is None,
``GET /api/setup/registration-terms`` is 404 and first-run setup behaves
exactly as before, so a self-installed instance sees no change.

Protocol (what a provider implements)::

    class RegistrationTermsProvider(Protocol):
        async def current_terms(self) -> dict:
            # {"version": str (non-empty, <= 100 chars),
            #  "title": str | None (<= 200), "text": str | None (<= 200 000),
            #  "url": str | None (<= 2 000)}
            # Raise any exception when the terms cannot be read right now;
            # setup then refuses with the retryable
            # ``registration_terms_unavailable`` and never completes.

The provider is called with NOTHING from the caller: no claim code, no email,
no address. A malformed answer is treated as unavailable.

The acceptance (``{"version", "accepted_at", "ip"}``, ip <= 64 chars — the
address this instance saw) is kept in the ``registration_terms_acceptance``
setting, and ``recorded_acceptance()`` is how the provider's module reads it to
report it onward.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional, Protocol

from database import db
from utils.helpers import utc_now_iso

logger = logging.getLogger(__name__)

REQUIRED = "registration_terms_required"
CHANGED = "registration_terms_changed"
UNAVAILABLE = "registration_terms_unavailable"
REFUSAL_CODES = frozenset({REQUIRED, CHANGED, UNAVAILABLE})

SETTING_ACCEPTANCE = "registration_terms_acceptance"
CACHE_TTL_S = 60.0
IP_MAX_CHARS = 64
VERSION_MAX_CHARS = 100
TITLE_MAX_CHARS = 200
TEXT_MAX_CHARS = 200_000
URL_MAX_CHARS = 2_000


class RegistrationTermsProvider(Protocol):
    async def current_terms(self) -> dict:
        ...


class RegistrationTermsError(Exception):
    """A refusal the setup router turns into ``{status, code, message, version, retryable}``."""

    def __init__(self, status_code: int, code: str, message: str, *,
                 version: Optional[str] = None, retryable: bool = False):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.version = version
        self.retryable = retryable
        super().__init__(message)

    def detail(self) -> dict:
        return {"code": self.code, "message": self.message,
                "version": self.version, "retryable": self.retryable}


_provider: Optional[RegistrationTermsProvider] = None
_cache: Optional[tuple] = None  # (fetched_at, terms)


def _now() -> float:
    return time.monotonic()


def register_provider(p: RegistrationTermsProvider) -> None:
    global _provider, _cache
    _provider = p
    _cache = None


def clear_provider() -> None:
    global _provider, _cache
    _provider = None
    _cache = None


def provider() -> Optional[RegistrationTermsProvider]:
    return _provider


def _unavailable() -> RegistrationTermsError:
    return RegistrationTermsError(
        503, UNAVAILABLE,
        "The registration terms cannot be read right now, so setup cannot finish. "
        "Nothing was changed; try again in a moment.",
        retryable=True,
    )


def _opt(value: Any, cap: int) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > cap:
        raise ValueError("field out of shape")
    return value


def _validate(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("not a mapping")
    version = raw.get("version")
    if not isinstance(version, str) or not version.strip() or len(version.strip()) > VERSION_MAX_CHARS:
        raise ValueError("version out of shape")
    return {
        "version": version.strip(),
        "title": _opt(raw.get("title"), TITLE_MAX_CHARS),
        "text": _opt(raw.get("text"), TEXT_MAX_CHARS),
        "url": _opt(raw.get("url"), URL_MAX_CHARS),
    }


async def current_terms(*, fresh: bool = False) -> dict:
    """The provider's current terms. Cached ``CACHE_TTL_S``; ``fresh`` bypasses
    (and refreshes) the cache — the submit re-check uses it. Raises
    ``RegistrationTermsError`` (503) when no answer can be had."""
    global _cache
    p = _provider
    if p is None:
        raise RuntimeError("no registration terms provider is registered")
    if not fresh and _cache is not None and _now() - _cache[0] < CACHE_TTL_S:
        return dict(_cache[1])
    try:
        terms = _validate(await p.current_terms())
    except Exception as e:  # noqa: BLE001 — any provider fault is "unavailable"
        logger.warning("[setup] registration terms unavailable: %s", type(e).__name__)
        raise _unavailable() from None
    _cache = (_now(), terms)
    return dict(terms)


async def check_accepted(given_version: Optional[str]) -> str:
    """Re-check ``given_version`` against the provider's CURRENT version.

    Returns the version on a match; raises ``RegistrationTermsError`` —
    503 ``registration_terms_unavailable`` (retryable), 400
    ``registration_terms_required`` or 409 ``registration_terms_changed``,
    each carrying the current version where known."""
    current = (await current_terms(fresh=True))["version"]
    given = (given_version or "").strip()
    if not given:
        raise RegistrationTermsError(
            400, REQUIRED, "Accept the registration terms to finish setup.", version=current)
    if given != current:
        raise RegistrationTermsError(
            409, CHANGED, "The registration terms have changed. Read the current version and accept it.",
            version=current)
    return current


def record_acceptance(version: str, ip: Optional[str], *, now: Optional[str] = None) -> dict:
    """Keep the acceptance where the provider's module can read it. Raises on
    a write failure — the caller fails setup with nothing completed."""
    record = {
        "version": version,
        "accepted_at": now or utc_now_iso(),
        "ip": (ip or "")[:IP_MAX_CHARS] or None,
    }
    db.set_setting(SETTING_ACCEPTANCE, json.dumps(record))
    return record


def recorded_acceptance() -> Optional[dict]:
    """The acceptance recorded at setup, or None."""
    raw = db.get_setting_value(SETTING_ACCEPTANCE, None)
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None
