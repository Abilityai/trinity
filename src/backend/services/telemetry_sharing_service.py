"""
Tier-2 opt-in fleet telemetry sharing (ent#12).

The **opt-in** egress half of the two-tier telemetry model. Tier-1 (ent#184)
records anonymous product events **locally, default-on, zero egress**. This layer
adds — on **explicit, default-off, reversible operator consent** — a periodic
share of **anonymized aggregates only** to the Ability-operated hosted intake, in
exchange for reciprocal value (fleet benchmarks; the hosted aggregation/benchmark
service is a separate issue).

Guarantees:
- **Never egresses without consent.** Two independent gates: the stored
  ``telemetry_sharing_enabled`` consent (default-off) AND the hard config switch
  ``TELEMETRY_SHARING_ENABLED`` (honors ``DO_NOT_TRACK``). Either off ⇒ nothing
  leaves the box.
- **Anonymized + coarse.** version / platform / edition / feature list / agent &
  execution COUNTS / activation-funnel counts. **No PII, no content, no prompts,
  no emails, no agent names.** The exact payload is inspectable before send
  (``build_aggregate_payload`` powers the Settings preview).
- **Fail-open.** A blocked / failed / air-gapped POST never affects the platform;
  every send is best-effort and swallows.
- **Reversible.** Opt-out flips the consent setting; the next heartbeat sees it
  and egress stops immediately.

Reuses the credential-free hosted-intake transport pattern of
``operator_intake_service`` (#38).
"""
from __future__ import annotations

import asyncio
import logging
import platform
import os
import random
from typing import Dict, Optional

import httpx

from config import (
    TELEMETRY_SHARING_ENABLED,
    TELEMETRY_SHARING_URL,
    TELEMETRY_SHARING_INTERVAL_HOURS,
    TELEMETRY_SHARING_BACKFILL_DEFAULT_DAYS,
)
from database import db
from services.operator_intake_service import get_or_create_installation_id
from utils.helpers import utc_now_iso, iso_cutoff

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT_SECONDS = 10.0
_PAYLOAD_SCHEMA_VERSION = 1

# system_settings keys (all local; none contain PII).
KEY_ENABLED = "telemetry_sharing_enabled"          # "true"/"false" — the consent
KEY_CONSENT_AT = "telemetry_sharing_consent_at"
KEY_BACKFILL_DAYS = "telemetry_sharing_backfill_days"
KEY_LAST_SHARED_AT = "telemetry_sharing_last_shared_at"

# The activation-funnel step events (mirrors the OSS emit allow-list). Kept local
# so the payload never depends on the enterprise funnel module.
_FUNNEL_STEPS = (
    "setup_started",
    "setup_step_create",
    "setup_step_credential",
    "setup_completed",
    "setup_dismissed",
)


# ---------------------------------------------------------------------------
# Consent state (local, reversible)
# ---------------------------------------------------------------------------

def is_consent_enabled() -> bool:
    """Has the operator opted in? Default-off. Fail-safe (any read error → off)."""
    try:
        return db.get_setting_value(KEY_ENABLED, "false") == "true"
    except Exception:  # pragma: no cover - never egress on a read failure
        return False


def is_hard_disabled() -> bool:
    """Config/air-gap kill switch (``TELEMETRY_SHARING_ENABLED`` / ``DO_NOT_TRACK``)."""
    return not TELEMETRY_SHARING_ENABLED


def get_status() -> Dict:
    """Operator-facing status for the Settings panel."""
    try:
        backfill = int(db.get_setting_value(KEY_BACKFILL_DAYS, str(TELEMETRY_SHARING_BACKFILL_DEFAULT_DAYS)))
    except (TypeError, ValueError):
        backfill = TELEMETRY_SHARING_BACKFILL_DEFAULT_DAYS
    return {
        "enabled": is_consent_enabled(),
        "hard_disabled": is_hard_disabled(),
        "consent_at": db.get_setting_value(KEY_CONSENT_AT, None),
        "backfill_days": backfill,
        "last_shared_at": db.get_setting_value(KEY_LAST_SHARED_AT, None),
        "share_url": TELEMETRY_SHARING_URL,
        "interval_hours": TELEMETRY_SHARING_INTERVAL_HOURS,
    }


def set_consent(enabled: bool, *, backfill_days: Optional[int] = None) -> Dict:
    """Record (or revoke) the sharing consent. Does NOT itself egress — the caller
    schedules an immediate backfill share on enable. Reversible: disabling stops
    the next heartbeat's egress."""
    db.set_setting(KEY_ENABLED, "true" if enabled else "false")
    if enabled:
        db.set_setting(KEY_CONSENT_AT, utc_now_iso())
        if backfill_days is not None:
            db.set_setting(KEY_BACKFILL_DAYS, str(max(int(backfill_days), 0)))
    return get_status()


# ---------------------------------------------------------------------------
# Anonymized aggregate payload (inspectable before send)
# ---------------------------------------------------------------------------

def _edition_and_features() -> tuple[str, list]:
    try:
        from services.entitlement_service import entitlement_service
        feats = entitlement_service.list_entitled_features()
    except Exception:
        feats = []
    return ("enterprise" if feats else "community"), feats


def build_aggregate_payload(window_days: Optional[int] = None, *, backfill: bool = False) -> Dict:
    """Build the anonymized aggregate. Coarse counts + enums ONLY — no PII, no
    content. ``window_days=None``/``0`` ⇒ all-time counts (used for backfill)."""
    since = iso_cutoff(hours=window_days * 24) if (window_days and window_days > 0) else None
    edition, features = _edition_and_features()

    # Execution aggregates over the window (admin scope = all agents).
    try:
        hours = (window_days * 24) if (window_days and window_days > 0) else 0
        exec_stats = db.get_fleet_execution_stats(None, hours=hours) or {}
    except Exception:
        exec_stats = {}

    try:
        funnel_raw = db.count_product_events_by_type(since=since) or {}
    except Exception:
        funnel_raw = {}
    funnel = {step: int(funnel_raw.get(step, 0)) for step in _FUNNEL_STEPS}

    try:
        agent_count = int(db.count_non_system_agents())
    except Exception:
        agent_count = 0

    return {
        "installation_id": get_or_create_installation_id(),
        "schema_version": _PAYLOAD_SCHEMA_VERSION,
        "shared_at": utc_now_iso(),
        "window_days": int(window_days or 0),
        "backfill": bool(backfill),
        "instance": {
            "trinity_version": os.getenv("GIT_COMMIT_SHORT", "unknown"),
            "edition": edition,
            "platform": platform.system() or "unknown",
            "python_version": platform.python_version(),
        },
        # Coarse capability list — already exposed via /api/version; no secrets.
        "enterprise_features": features,
        "counts": {
            "agents": agent_count,
            "executions_total": int(exec_stats.get("total", 0) or 0),
            "executions_success": int(exec_stats.get("success_count", 0) or 0),
            "executions_failed": int(exec_stats.get("failed_count", 0) or 0),
        },
        "activation_funnel": funnel,
    }


# ---------------------------------------------------------------------------
# Egress (gated, fail-open)
# ---------------------------------------------------------------------------

async def share_now(*, backfill: bool = False, window_days: Optional[int] = None) -> bool:
    """POST one anonymized aggregate to the hosted intake, IF both gates allow.

    Returns True only on a genuine 2xx. Never raises — best-effort. Both gates
    (config hard-switch + stored consent) are re-checked here so a stale caller
    can't force an egress.
    """
    try:
        if is_hard_disabled():
            logger.info("[telemetry-share] disabled (TELEMETRY_SHARING_ENABLED / DO_NOT_TRACK)")
            return False
        if not is_consent_enabled():
            return False  # not opted in — nothing leaves the box

        if window_days is None:
            if backfill:
                try:
                    window_days = int(db.get_setting_value(KEY_BACKFILL_DAYS, str(TELEMETRY_SHARING_BACKFILL_DEFAULT_DAYS)))
                except (TypeError, ValueError):
                    window_days = TELEMETRY_SHARING_BACKFILL_DEFAULT_DAYS
            else:
                # A periodic heartbeat covers the interval since the last share.
                window_days = max(TELEMETRY_SHARING_INTERVAL_HOURS // 24, 1)

        payload = build_aggregate_payload(window_days, backfill=backfill)

        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            resp = await client.post(TELEMETRY_SHARING_URL, json=payload)

        if 200 <= resp.status_code < 300:
            db.set_setting(KEY_LAST_SHARED_AT, utc_now_iso())
            logger.info(
                "[telemetry-share] shared (install %s…, backfill=%s, window=%sd)",
                payload["installation_id"][:8], backfill, window_days,
            )
            return True
        logger.warning("[telemetry-share] POST returned HTTP %s", resp.status_code)
        return False
    except Exception as e:  # noqa: BLE001 — fire-and-forget, swallow everything
        logger.info("[telemetry-share] skipped (ignored): %s", type(e).__name__)
        return False


class TelemetrySharingService:
    """Background heartbeat that shares the aggregate on the configured cadence
    when consent is on. Inert (a cheap consent read) when opted out."""

    def __init__(self, interval_hours: int = TELEMETRY_SHARING_INTERVAL_HOURS):
        self.interval_seconds = max(int(interval_hours), 1) * 3600
        self._task: Optional[asyncio.Task] = None
        self._running = False

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("[telemetry-share] heartbeat started (every %sh)", self.interval_seconds // 3600)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        while self._running:
            # Sleep FIRST so boot isn't a share burst; jitter so replicas don't
            # realign. If hard-disabled, idle (the operator can't opt in anyway).
            jitter = random.uniform(0, min(600, self.interval_seconds * 0.1))
            try:
                await asyncio.sleep(self.interval_seconds + jitter)
            except asyncio.CancelledError:
                break
            if not self._running:
                break
            if is_hard_disabled() or not is_consent_enabled():
                continue
            await share_now(backfill=False)


telemetry_sharing_service = TelemetrySharingService()
