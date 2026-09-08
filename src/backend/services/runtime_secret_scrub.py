"""Runtime secret-scrub seam — identity-based redaction of staged values.

A generic OSS mechanism: a producer STAGES a known secret value (by identity),
and the execution-terminal persistence chokepoints SCRUB every staged value out
of the transcript / exec-log / idempotency snapshot BEFORE they are written —
covering arbitrary values that the pattern-based `utils/credential_sanitizer`
(prefix regexes + `KEY=value`) cannot catch. The live tool result stays whatever
the producer delivered; only the persisted copy is scrubbed.

Mechanism-only by design — this file names no consumer (it is a SEAM file grepped
by the enterprise-docs-guard). Its one enterprise consumer stages a fetched
credential before delivering it; here that is just "a value someone asked us to
scrub".

Store (Redis, GLOBAL — cross-agent relay is always correct to scrub regardless of
whose text the value appears in):
  * HASH  `secret_scrub:staged`     field = sha256(value) -> AES-256-GCM envelope
  * ZSET  `secret_scrub:staged_at`  member = same field    -> score = stage time

HASH-by-value-hash gives exact dedup (AES-GCM's random nonce means a SET of
envelopes never dedups → unbounded growth); the ZSET gives per-member 24h expiry
(pruned at stage time), so one busy producer can't keep the whole store alive.
Values are enveloped because Redis AOF persists them; the encryption key is the
shared credential-encryption singleton.

Failure asymmetry (deliberate, documented in the enterprise threat model):
  * STAGE side FAILS CLOSED — any failure (Redis down, hard cap) raises
    `StagingUnavailable`, and the enterprise fetch refuses delivery. Delivering an
    unstageable secret is a silent security downgrade.
  * SCRUB side FAILS OPEN — a Redis/decrypt error at persistence time returns an
    empty value set (no identity scrub; the pattern pass still runs) + ONE
    throttled ERROR. The terminal MUST persist regardless; no NEW secret is
    delivered during the outage (stage side is closed), so the open scrub side
    never widens exposure beyond turns already in flight.

Behaviour-neutral for OSS: no staged values ⇒ `get_staged_values()` returns `[]`
⇒ every scrub call is a no-op.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
from typing import Any, Optional

from redis_breaker_util import get_breaker_redis
from utils.credential_sanitizer import REDACTION_PLACEHOLDER

logger = logging.getLogger(__name__)

_STAGED_HASH = "secret_scrub:staged"
_STAGED_ZSET = "secret_scrub:staged_at"

# 24h — chosen to outlive the #1083 lease window + same-day crash resends (a
# late-SUCCESS-after-LEASE_EXPIRED CAS win and crash-resends provably outlive the
# earlier 7500s draft; aligns with the idempotency-key window).
_TTL_SECONDS = 24 * 60 * 60
# A value shorter than this is NOT staged — scrubbing a 3-char value would shred
# transcripts. Matches the agent-side sanitizer registry's own >=8 floor.
_MIN_VALUE_LEN = 8
# Bounds on the distinct staged-value set (dedup keeps this small in practice).
_SOFT_WARN_FIELDS = 200
_HARD_CAP_FIELDS = 1000
# Memoize a Redis outage so the scrub/stage path doesn't pay a connect attempt
# every terminal for the duration.
_DOWN_MEMO_SECONDS = 30.0
# Throttle the fail-open scrub ERROR so an outage doesn't emit one line per
# chokepoint per terminal.
_ERROR_LOG_THROTTLE_SECONDS = 60.0

_redis_down_until = 0.0
_last_error_log_ts = 0.0


class StagingUnavailable(Exception):
    """A value could not be staged (Redis down, or the hard cap was hit). The
    stage side fails CLOSED — the caller must refuse delivery."""


# --- encryption (shared singleton) -------------------------------------------


def _enc_svc():
    from services.credential_encryption import get_credential_encryption_service

    return get_credential_encryption_service()


# --- Redis (memoized down-state) ---------------------------------------------


def _redis():
    global _redis_down_until
    now = time.time()
    if now < _redis_down_until:
        return None
    try:
        client = get_breaker_redis()
    except Exception:  # noqa: BLE001 — treat any accessor error as down
        client = None
    if client is None:
        _redis_down_until = now + _DOWN_MEMO_SECONDS
    return client


def _throttled_error(msg: str, *args) -> None:
    global _last_error_log_ts
    now = time.time()
    if now - _last_error_log_ts >= _ERROR_LOG_THROTTLE_SECONDS:
        _last_error_log_ts = now
        logger.error(msg, *args)


def _field(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# --- stage side (fail CLOSED) ------------------------------------------------


def stage_secret(agent_name: Optional[str], value: str) -> None:
    """Stage ``value`` for identity-scrubbing. ``agent_name`` is for the log line
    only — the store is global. Raises ``StagingUnavailable`` on any failure so
    the caller can fail closed. A value shorter than the floor is SKIPPED (not an
    error) with a WARNING."""
    if not value or len(value) < _MIN_VALUE_LEN:
        logger.warning(
            "runtime_secret_scrub: value below the %d-char floor NOT staged "
            "(agent=%s) — it will not be identity-scrubbed",
            _MIN_VALUE_LEN,
            agent_name,
        )
        return

    client = _redis()
    if client is None:
        raise StagingUnavailable("secret-scrub store unavailable (Redis down)")

    field = _field(value)
    now = time.time()
    try:
        # Prune expired members first (member-scoped, not a whole-key TTL).
        expired = client.zrangebyscore(_STAGED_ZSET, 0, now - _TTL_SECONDS)
        if expired:
            client.hdel(_STAGED_HASH, *expired)
            client.zrem(_STAGED_ZSET, *expired)

        count = client.hlen(_STAGED_HASH) or 0
        already = client.hexists(_STAGED_HASH, field)
        if not already and count >= _HARD_CAP_FIELDS:
            raise StagingUnavailable(
                f"secret-scrub store at hard cap ({_HARD_CAP_FIELDS} values)"
            )
        if not already and count >= _SOFT_WARN_FIELDS:
            logger.warning(
                "runtime_secret_scrub: staged-value set is large (%d)", count
            )

        envelope = _enc_svc().encrypt({"v": value})
        client.hset(_STAGED_HASH, field, envelope)
        client.zadd(_STAGED_ZSET, {field: now})
    except StagingUnavailable:
        raise
    except Exception as e:  # noqa: BLE001 — any store/encrypt error fails closed
        raise StagingUnavailable(
            f"secret-scrub staging failed: {type(e).__name__}"
        ) from e


# --- scrub side (fail OPEN) --------------------------------------------------


def get_staged_values() -> list[str]:
    """Every currently-staged plaintext value. ONE HGETALL + one decrypt pass.
    Empty/down → `[]` fast path (agents/installs that never staged pay one cheap
    Redis call per terminal, zero decrypts). Per-member decrypt failure skips
    that member with a WARNING and scrubs the rest. FAILS OPEN — a store error
    returns `[]` + one throttled ERROR."""
    client = _redis()
    if client is None:
        return []
    try:
        raw = client.hgetall(_STAGED_HASH)
    except Exception as e:  # noqa: BLE001 — fail open
        _throttled_error("runtime_secret_scrub: staged read failed (fail-open): %s", e)
        return []
    if not raw:
        return []
    svc = _enc_svc()
    values: list[str] = []
    for field, envelope in raw.items():
        try:
            v = svc.decrypt(envelope).get("v")
        except (
            Exception
        ) as e:  # noqa: BLE001 — skip a corrupt/rotated member, scrub the rest
            logger.warning(
                "runtime_secret_scrub: skipping an unreadable staged member: %s", e
            )
            continue
        if v:
            values.append(v)
    return values


def _renditions(value: str) -> list[str]:
    """The three renditions a staged value can appear as in persisted output:
    raw, once-JSON-escaped (values embedded in already-dumped JSON), and base64
    (the most common trivial encoding)."""
    out = [value]
    try:
        escaped = json.dumps(value)[1:-1]
        if escaped and escaped != value:
            out.append(escaped)
    except (TypeError, ValueError):
        pass
    try:
        b64 = base64.b64encode(value.encode("utf-8")).decode("ascii")
        if b64:
            out.append(b64)
    except Exception:  # noqa: BLE001
        pass
    return out


def scrub_text(values: list[str], text):
    """Replace every rendition of every staged value in ``text`` with the
    sanitizer's `***REDACTED***` marker. FALSY PASSTHROUGH — a falsy input is
    returned UNCHANGED (never `None` → `""`, which would corrupt a NULL error
    column). LONGEST-FIRST so a staged value that is a substring of a longer one
    doesn't shred the longer match."""
    if not text or not values:
        return text
    result = text
    for value in sorted(values, key=len, reverse=True):
        if not value:
            continue
        for rendition in _renditions(value):
            if rendition:
                result = result.replace(rendition, REDACTION_PLACEHOLDER)
    return result


def scrub_obj(values: list[str], obj: Any) -> Any:
    """Recursively scrub string leaves of a dict/list structure, returning a NEW
    structure (never mutating the input in place). Non-string, non-container
    leaves pass through unchanged."""
    if not values:
        return obj
    if isinstance(obj, str):
        return scrub_text(values, obj)
    if isinstance(obj, dict):
        return {k: scrub_obj(values, v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub_obj(values, v) for v in obj]
    return obj


# --- test hook ---------------------------------------------------------------


def clear_staged() -> None:
    """Wipe the staged store (tests only). Best-effort; DEL is ACL-safe (unlike
    KEYS)."""
    global _redis_down_until
    _redis_down_until = 0.0
    client = _redis()
    if client is None:
        return
    try:
        client.delete(_STAGED_HASH, _STAGED_ZSET)
    except Exception:  # noqa: BLE001
        pass
