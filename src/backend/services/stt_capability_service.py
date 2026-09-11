"""Does the configured ElevenLabs key actually transcribe? (#2695)

`stt_available` on the Workspace card used to be `bool(tts_service.is_available())`
— a non-empty check on the resolved key. ElevenLabs keys carry PER-ENDPOINT
permissions, so a key granted Text-to-Speech but not Speech-to-Text passed that
check and rendered a mic that failed on every press with "Could not transcribe
the audio" (the provider answers `401 missing_permissions`). Voice-out working
proved nothing about voice-in, which is exactly what the presence gate assumed.

This module answers the CAPABILITY question with one provider probe per key,
cached, and keeps the answer fail-soft:

* **Probe, not presence.** `probe()` POSTs a deliberately invalid body to the
  speech-to-text endpoint. The provider authorises BEFORE it validates, so the
  status code partitions cleanly: a 401/403 means the key cannot transcribe
  (`refused`, with the provider's own status word as `detail`); any other
  definitive answer — 2xx, 400, 422, 429 — means the key got past the permission
  gate (`capable`); a transport error, timeout or 5xx says nothing (`unknown`).
  No audio is ever sent, so a probe costs no transcription minutes.

* **Cached, keyed on the KEY.** The roster is read on every Workspace load, so
  the verdict lives in Redis under `stt:capability:<sha256(key)[:16]>` (6 h for a
  decided verdict, 2 min for `unknown` so an outage is re-asked soon). Keying on
  a digest of the key — never the key itself — means a key change is a cache
  MISS by construction: nothing has to remember to invalidate, and the resolver
  it reads through stays uncached (the `--workers 2` rule #506 / ent#117 set).
  Redis down ⇒ a per-process fallback with the same key and TTLs, so the two
  workers can at worst each probe once.

* **Says WHY (#2696).** `classify_stt_failure()` maps a live `/stt` provider
  error onto a category, an HTTP status and a client-facing sentence, so an
  auth/permission failure, a quota/plan condition, a provider rate limit and a
  rejected audio container stop collapsing into one opaque 422. The client
  never sees the provider's body; the operator sees the status word and the
  category on the admin Settings panel via `record_live_failure()`.

* **Fails SOFT.** `unknown` renders the mic. Hiding a control that would have
  worked is the defect this exists to prevent in the other direction, so only a
  definitive provider refusal hides it. The live `/stt` call feeds back: a real
  401 from a genuine transcription attempt stores `refused`, so the symptom the
  issue describes self-heals the cache even if the probe never ran.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, asdict
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

STT_URL = "https://api.elevenlabs.io/v1/speech-to-text"
STT_MODEL = "scribe_v1"

VERDICT_CAPABLE = "capable"
VERDICT_REFUSED = "refused"
VERDICT_UNKNOWN = "unknown"
VERDICT_UNCONFIGURED = "unconfigured"

# A decided verdict is worth keeping: per-endpoint permissions change only when
# an admin edits the key at the provider. `unknown` is a transient (outage,
# timeout) and is re-asked soon.
TTL_DECIDED_SECONDS = 6 * 3600
TTL_UNKNOWN_SECONDS = 120
# The probe's own HTTP timeout. Below the roster's patience (`WAIT_BUDGET`),
# so a slow provider degrades to `unknown` rather than stalling sign-in.
PROBE_TIMEOUT_SECONDS = 8.0
# How long a cache-missing reader (the roster, the Settings panel) waits for
# the probe before answering `unknown` and letting it finish in the background.
WAIT_BUDGET_SECONDS = 4.0

_CACHE_PREFIX = "stt:capability:"


@dataclass(frozen=True)
class SttCapability:
    verdict: str
    detail: Optional[str] = None      # the provider's own status word on a refusal
    checked_at: Optional[float] = None

    @property
    def allowed(self) -> bool:
        """May the mic render / may `/stt` run? Everything but a definitive
        refusal — the fail-SOFT direction."""
        return self.verdict != VERDICT_REFUSED

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> Optional["SttCapability"]:
        try:
            d = json.loads(raw)
            return cls(verdict=str(d["verdict"]), detail=d.get("detail"),
                       checked_at=d.get("checked_at"))
        except Exception:  # noqa: BLE001 — a corrupt row is a miss, never a 500
            return None


UNCONFIGURED = SttCapability(VERDICT_UNCONFIGURED)


def cache_key(api_key: str) -> str:
    """Digest of the key, never the key — the row name lands in Redis listings."""
    return _CACHE_PREFIX + hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


def _ttl_for(cap: SttCapability) -> int:
    return TTL_DECIDED_SECONDS if cap.verdict in (VERDICT_CAPABLE, VERDICT_REFUSED) \
        else TTL_UNKNOWN_SECONDS


# ---- cache ------------------------------------------------------------------

# Per-process fallback for a Redis outage: {cache_key: (expires_at, cap)}.
_local: dict[str, tuple[float, SttCapability]] = {}
_inflight: dict[str, "asyncio.Task[SttCapability]"] = {}


def _redis():
    from redis_breaker_util import get_breaker_redis
    return get_breaker_redis()


def read_cached(api_key: str) -> Optional[SttCapability]:
    if not api_key:
        return UNCONFIGURED
    k = cache_key(api_key)
    r = _redis()
    if r is not None:
        try:
            raw = r.get(k)
            if raw:
                cap = SttCapability.from_json(raw)
                if cap is not None:
                    return cap
        except Exception as e:  # noqa: BLE001
            logger.warning("stt capability cache read failed-open (%s)", e)
    hit = _local.get(k)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    return None


def store(api_key: str, cap: SttCapability) -> None:
    if not api_key:
        return
    k = cache_key(api_key)
    ttl = _ttl_for(cap)
    _local[k] = (time.monotonic() + ttl, cap)
    r = _redis()
    if r is not None:
        try:
            r.set(k, cap.to_json(), ex=ttl)
        except Exception as e:  # noqa: BLE001
            logger.warning("stt capability cache write failed-open (%s)", e)


def invalidate(api_key: str) -> None:
    """Forget the verdict for THIS key. A changed key needs no call here — its
    digest is a different row — but re-saving the same key after fixing its
    permissions at the provider does."""
    if not api_key:
        return
    k = cache_key(api_key)
    _local.pop(k, None)
    r = _redis()
    if r is not None:
        try:
            r.delete(k)
        except Exception as e:  # noqa: BLE001
            logger.warning("stt capability cache delete failed-open (%s)", e)


# ---- probe ------------------------------------------------------------------

def provider_status_word(body: str) -> Optional[str]:
    """The provider's own short status token (`missing_permissions`,
    `quota_exceeded`, `invalid_audio`, ...) out of an ElevenLabs error body,
    or None. Bounded and never the free-text message when a token exists — the
    token is what an operator greps the provider's docs for."""
    try:
        d = json.loads(body or "{}")
    except Exception:  # noqa: BLE001
        return None
    det = d.get("detail") if isinstance(d, dict) else None
    if isinstance(det, dict):
        word = det.get("status") or det.get("code") or det.get("message")
    elif isinstance(det, str):
        word = det
    else:
        word = None
    return str(word)[:120] if word else None


def classify_response(status_code: int, body: str) -> SttCapability:
    """Map one provider answer to a verdict. Pure, so the partition is testable
    without HTTP. The provider authorises before it validates the upload, which
    is what makes an invalid body a free permission probe."""
    now = time.time()
    if status_code in (401, 403):
        detail = provider_status_word(body)
        return SttCapability(VERDICT_REFUSED, detail=(detail or f"http_{status_code}")[:120],
                             checked_at=now)
    if status_code < 500:
        # 2xx would mean the provider transcribed one byte of nonsense; 400/422
        # is the expected "bad upload"; 429 is rate-limited — all past the gate.
        return SttCapability(VERDICT_CAPABLE, checked_at=now)
    return SttCapability(VERDICT_UNKNOWN, detail=f"http_{status_code}", checked_at=now)


async def probe(api_key: str, *, timeout: float = PROBE_TIMEOUT_SECONDS) -> SttCapability:
    """Ask the provider whether THIS key may call speech-to-text. Never raises;
    a probe that cannot complete is `unknown`, never `refused`."""
    if not api_key:
        return UNCONFIGURED
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                STT_URL,
                headers={"xi-api-key": api_key},
                data={"model_id": STT_MODEL},
                # One byte, not audio: rejected by validation, never billed.
                files={"file": ("probe.bin", b"\0", "application/octet-stream")},
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("stt capability probe could not complete (%s) — treating as unknown", e)
        return SttCapability(VERDICT_UNKNOWN, detail=type(e).__name__, checked_at=time.time())
    cap = classify_response(resp.status_code, resp.text)
    if cap.verdict == VERDICT_REFUSED:
        logger.warning("ElevenLabs key cannot transcribe (%s %s) — the Workspace mic is hidden",
                       resp.status_code, cap.detail)
    return cap


async def _probe_and_store(api_key: str) -> SttCapability:
    cap = await probe(api_key)
    store(api_key, cap)
    return cap


async def ensure_capability(api_key: Optional[str] = None, *,
                            wait_seconds: float = WAIT_BUDGET_SECONDS) -> SttCapability:
    """The cached verdict, probing on a miss — bounded by `wait_seconds`, past
    which the caller gets `unknown` NOW and the probe keeps running to fill the
    cache for the next reader. Single-flighted per key within a process so a
    burst of roster loads costs one provider call, not one each."""
    if api_key is None:
        from services.settings_service import settings_service
        api_key = settings_service.get_elevenlabs_api_key()
    if not api_key:
        return UNCONFIGURED
    cached = read_cached(api_key)
    if cached is not None:
        return cached
    k = cache_key(api_key)
    task = _inflight.get(k)
    if task is None or task.done():
        task = asyncio.create_task(_probe_and_store(api_key))
        _inflight[k] = task
        task.add_done_callback(lambda t, _k=k: _inflight.pop(_k, None) if _inflight.get(_k) is t else None)
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=wait_seconds)
    except asyncio.TimeoutError:
        return SttCapability(VERDICT_UNKNOWN, detail="probe_pending", checked_at=time.time())
    except Exception as e:  # noqa: BLE001 — the task itself never raises, belt only
        logger.warning("stt capability probe task failed (%s)", e)
        return SttCapability(VERDICT_UNKNOWN, detail=type(e).__name__, checked_at=time.time())


def record_live_refusal(api_key: str, status_code: int, body: str) -> None:
    """A genuine `/stt` call was refused by the provider: learn from it, so the
    next roster load hides the mic without waiting for a probe. Only a 401/403
    is a verdict about the key; anything else says nothing about permissions."""
    if status_code in (401, 403) and api_key:
        store(api_key, classify_response(status_code, body))


# ---- live-failure mapping (#2696) ---------------------------------------------

CATEGORY_PERMISSION = "permission"    # the key may not call speech-to-text
CATEGORY_AUTH = "auth"                # the key itself was rejected
CATEGORY_QUOTA = "quota"              # credits / plan / entitlement
CATEGORY_RATE_LIMIT = "rate_limit"    # transient; retry
CATEGORY_AUDIO = "audio"              # the recording was rejected, not the key
CATEGORY_PROVIDER = "provider"        # the provider itself failed
CATEGORY_UNKNOWN = "unknown"

# Provider status words that mean "the account cannot pay for this", seen on
# 401/402 bodies. Matched as substrings of the status token, lower-cased.
_QUOTA_WORDS = ("quota", "credit", "plan", "payment", "subscription", "billing",
                "insufficient", "free_users", "entitlement", "trial")


@dataclass(frozen=True)
class SttFailure:
    category: str
    http_status: int          # what the client receives
    client_message: str       # never carries the provider's body
    provider_status: int      # what the provider answered
    detail: Optional[str]     # the provider's status word, operator-facing only

    def to_json(self) -> str:
        return json.dumps({**asdict(self), "at": time.time()})


_LAST_FAILURE_PREFIX = "stt:last_failure:"
TTL_LAST_FAILURE_SECONDS = 24 * 3600
# Per-process fallback, {row: (expires_at, json)} — the same shape as `_local`.
_local_failures: dict[str, tuple[float, str]] = {}


def _failure_row(api_key: str) -> str:
    return _LAST_FAILURE_PREFIX + cache_key(api_key)[len(_CACHE_PREFIX):]

_MSG_PERMISSION = ("Voice input is not enabled for this workspace: the speech "
                   "recognition key is missing the speech-to-text permission. "
                   "Ask your operator to update it — you can type instead.")
_MSG_AUTH = ("Voice input is not working: the speech recognition key was rejected. "
             "Ask your operator to check it — you can type instead.")
_MSG_QUOTA = ("Voice input is unavailable: the speech recognition account is out of "
              "credits or not on a plan that allows it. Ask your operator — "
              "you can type instead.")
_MSG_RATE_LIMIT = "Too many voice messages just now — wait a moment and try again."
_MSG_AUDIO = ("That recording could not be read. Try again — or type your message instead.")
_MSG_PROVIDER = "Voice input failed — please type instead"
_MSG_UNKNOWN = ("Voice input failed (the speech recognition service answered with an "
                "error). Try again, or type your message instead.")


def classify_stt_failure(status_code: int, body: str) -> SttFailure:
    """One live `/stt` provider error → what the client is told and what the
    operator is shown. Pure. Every status lands in a NAMED category; there is
    no arm that hands back the old opaque string, so an unrecognised provider
    answer is `unknown` — still specific about who failed — never a regression
    to "Could not transcribe the audio"."""
    word = provider_status_word(body)
    lw = (word or "").lower()

    if status_code in (401, 403):
        if "permission" in lw:
            return SttFailure(CATEGORY_PERMISSION, 503, _MSG_PERMISSION, status_code, word)
        if any(q in lw for q in _QUOTA_WORDS):
            return SttFailure(CATEGORY_QUOTA, 503, _MSG_QUOTA, status_code, word)
        return SttFailure(CATEGORY_AUTH, 503, _MSG_AUTH, status_code, word)
    if status_code == 402:
        return SttFailure(CATEGORY_QUOTA, 503, _MSG_QUOTA, status_code, word)
    if status_code == 429:
        return SttFailure(CATEGORY_RATE_LIMIT, 429, _MSG_RATE_LIMIT, status_code, word)
    if status_code in (400, 413, 415, 422):
        return SttFailure(CATEGORY_AUDIO, 422, _MSG_AUDIO, status_code, word)
    if status_code >= 500:
        return SttFailure(CATEGORY_PROVIDER, 502, _MSG_PROVIDER, status_code, word)
    return SttFailure(CATEGORY_UNKNOWN, 502, _MSG_UNKNOWN, status_code, word)


def record_live_failure(api_key: str, status_code: int, body: str) -> SttFailure:
    """A genuine `/stt` call failed at the provider. Classify it, remember the
    operator-facing half beside the key's capability row (so Settings → Voice
    can name the cause without a container log), and — for a 401/403 — let
    the capability verdict learn from it (#2695). Returns the classification
    so the caller can raise the client-facing half."""
    failure = classify_stt_failure(status_code, body)
    record_live_refusal(api_key, status_code, body)
    if api_key:
        k = _failure_row(api_key)
        payload = failure.to_json()
        _local_failures[k] = (time.monotonic() + TTL_LAST_FAILURE_SECONDS, payload)
        r = _redis()
        if r is not None:
            try:
                r.set(k, payload, ex=TTL_LAST_FAILURE_SECONDS)
            except Exception as e:  # noqa: BLE001
                logger.warning("stt last-failure write failed-open (%s)", e)
    return failure


def read_last_failure(api_key: str) -> Optional[dict]:
    """The most recent live failure for THIS key, as stored — operator-facing."""
    if not api_key:
        return None
    k = _failure_row(api_key)
    raw = None
    r = _redis()
    if r is not None:
        try:
            raw = r.get(k)
        except Exception as e:  # noqa: BLE001
            logger.warning("stt last-failure read failed-open (%s)", e)
    if not raw:
        hit = _local_failures.get(k)
        if hit and hit[0] > time.monotonic():
            raw = hit[1]
    if not raw:
        return None
    try:
        d = json.loads(raw)
    except Exception:  # noqa: BLE001
        return None
    return {
        "category": d.get("category"),
        "provider_status": d.get("provider_status"),
        "detail": d.get("detail"),
        "at": d.get("at"),
    }


def describe(cap: SttCapability, api_key: Optional[str] = None) -> dict:
    """The admin-panel shape: verdict + detail + when, never the key. With the
    key, also the last live failure (#2696) so an operator can read the cause
    of a client's "voice input failed" without a container log."""
    out = {
        "stt_capability": cap.verdict,
        "stt_detail": cap.detail,
        "stt_checked_at": cap.checked_at,
    }
    if api_key is not None:
        out["stt_last_failure"] = read_last_failure(api_key)
    return out
