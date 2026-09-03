"""
Tier-2 opt-in fleet telemetry sharing (ent#12, extended by ent#437).

The **opt-in** egress half of the two-tier telemetry model. Tier-1 (ent#184)
records anonymous product events **locally, default-on, zero egress**. This layer
adds — on **explicit, default-off, reversible operator consent** — a periodic
share of **anonymized aggregates only** to the Ability-operated hosted intake, in
exchange for reciprocal value (fleet benchmarks; the hosted aggregation/benchmark
service is ent#190 and did not exist when this shipped — every send 404s until it
does, and the send log below says so).

Guarantees:
- **Never egresses without consent.** Two independent gates: the stored
  ``telemetry_sharing_enabled`` consent (default-off) AND the hard config switch
  ``TELEMETRY_SHARING_ENABLED`` (honors ``DO_NOT_TRACK``). Either off ⇒ nothing
  leaves the box.
- **Anonymized + coarse.** version / platform / edition / feature list / agent &
  execution COUNTS / activation-funnel counts / an outcome mix by trigger bucket
  and status / provider-failure kinds / the install lane. **No PII, no content,
  no prompts, no emails, no agent names.** The exact payload is inspectable
  before send (``build_aggregate_payload`` powers the Settings preview) and the
  last few sends are inspectable afterwards (``recent_sends``).
- **A share identity that is not the install identity (ent#437).** The aggregate
  carries ``sharing_id``, a UUID minted on consent and deleted on revoke — never
  ``installation_id``, which travels with the operator's email and company in the
  ent#38 intake POST and is therefore linkable to a person. The validator bans the
  key outright; the id is logged only as an 8-char prefix.
- **Documented and enforced schema.** ``PAYLOAD_SCHEMA_V2`` is a nested allow-list
  and ``validate_payload`` raises on anything outside it; ``share_now`` refuses to
  send on a violation (fail-CLOSED egress — an undocumented field is a bug, not
  a payload).
- **Fail-open delivery.** A blocked / failed / air-gapped POST never affects the
  platform; every send is best-effort and swallows.
- **Reversible.** Opt-out flips the consent setting and discards the share id;
  the next heartbeat sees it and egress stops immediately.

Reuses the credential-free hosted-intake transport pattern of
``operator_intake_service`` (#38) — but never its identity.
"""
from __future__ import annotations

import asyncio
import json
import logging
import platform
import random
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

import httpx

from config import (
    TELEMETRY_SHARING_ENABLED,
    TELEMETRY_SHARING_URL,
    TELEMETRY_SHARING_INTERVAL_HOURS,
    TELEMETRY_SHARING_BACKFILL_DEFAULT_DAYS,
)
from database import db
from redis_breaker_util import SingleFlightLock, get_breaker_redis
from services import settings_service
from utils.app_version import resolve_release_version
from utils.helpers import utc_now_iso, iso_cutoff

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT_SECONDS = 10.0
PAYLOAD_SCHEMA_VERSION = 2
_PAYLOAD_SCHEMA_VERSION = PAYLOAD_SCHEMA_VERSION  # ent#12 name, kept for readers
RECENT_SENDS_LIMIT = 5

# The documented default. Compared against the configured URL so a 404 can be
# worded honestly: from the default it means "the hosted service is not live
# yet"; from an override it means only "your receiver answered 404".
DEFAULT_SHARE_URL = "https://intake.abilityai.dev/v1/telemetry-share"

# Readers whose time cutoff is unconditional (``db.get_failure_event_counts_by_
# subscription``: ``occurred_at > iso_cutoff(hours)``) cannot be asked for
# "all-time" with ``hours=0`` — that reads as "since now". Their tables are
# retention-bounded (30d default), so "the whole table" is any window that
# exceeds retention. The execution readers treat ``hours=0`` as unbounded and
# get 0 as before.
_ALL_TIME_HOURS = 24 * 3650

# One send per interval fleet-wide under ``--workers 2``: the two heartbeat
# loops tick independently (interval + jitter), so a mutex released after the
# POST would dedupe nothing — this key is a TICK MARKER held by TTL and never
# released. Fail-open (Redis down ⇒ both workers send, today's behaviour).
_TICK_LOCK_KEY = "telemetry_share:tick"

# The preview cannot show a real share id before consent (none exists yet —
# minting one on a preview GET would create identity without consent). A fixed,
# valid-UUID placeholder keeps the preview validator-clean; the panel explains it.
PREVIEW_SHARING_ID = "00000000-0000-4000-8000-000000000000"

# system_settings keys (all local; none contain PII). Every key sits under the
# ``telemetry_sharing_`` prefix the generic PUT /api/settings/{key} refuses, so
# the dedicated human-only routes are the only writers. The generic DELETE stays
# open for the prefix BY DESIGN: it is the reset path, and every deletion moves
# in the safe direction (off / ask again / re-mint).
KEY_ENABLED = "telemetry_sharing_enabled"          # "true"/"false" — the consent
KEY_CONSENT_AT = "telemetry_sharing_consent_at"
KEY_BACKFILL_DAYS = "telemetry_sharing_backfill_days"
KEY_LAST_SHARED_AT = "telemetry_sharing_last_shared_at"
KEY_SHARING_ID = "telemetry_sharing_id"                          # ent#437
KEY_DISMISSED_AT = "telemetry_sharing_dismissed_at"              # ent#437 "don't ask again"
KEY_FIRST_VALUE_AT = "telemetry_sharing_first_value_at"          # ent#437 warm-ask memo
KEY_BACKFILL_DELIVERED_AT = "telemetry_sharing_backfill_delivered_at"  # ent#437
KEY_RECENT_SENDS = "telemetry_sharing_recent_sends"              # ent#437 (JSON list)

# The activation-funnel step events (mirrors the OSS emit allow-list minus the
# intro step, which is a render beacon, not a funnel transition). Kept local so
# the payload never depends on the enterprise funnel module. The SCHEMA is
# derived from this tuple — never hand-typed beside it.
_FUNNEL_STEPS = (
    "setup_started",
    "setup_step_create",
    "setup_step_credential",
    "setup_completed",
    "setup_dismissed",
)

# ---------------------------------------------------------------------------
# Wire vocabularies (ent#437). Telemetry-OWNED enums, never UI labels: the
# executions timeline folds `triggered_by` into display buckets that product
# work renames and extends (ent#220 added Rooms, ent#329 Operator queue). With
# a fail-closed validator, keying the wire on those labels would halt egress
# fleet-wide on the next new bucket. Unmapped labels land in ``other`` until a
# deliberate schema bump; a parity test pins that every label the db emits maps.
# ---------------------------------------------------------------------------
TRIGGER_WIRE_KEYS: Dict[str, str] = {
    "Chat/Tasks": "chat",
    "MCP": "mcp",
    "Channels": "channel",
    "Public": "public",
    "Scheduled": "schedule",
    "Loops": "loop",
    "Reminders": "reminder",
    "Rooms": "room",
    "Operator queue": "operator_queue",
    "Agent-to-agent": "agent",
    "Voice": "voice",
    "Other": "other",
}
WIRE_TRIGGER_BUCKETS = frozenset(TRIGGER_WIRE_KEYS.values())
# Terminal execution statuses the wire carries. Anything else the table holds
# (a future status, a typo'd legacy row) lands in ``other``.
WIRE_STATUSES = frozenset({"success", "failed", "error", "cancelled", "skipped", "other"})
PROVIDER_FAILURE_KINDS = ("rate_limit", "auth")
_INSTALL_SOURCE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

# Schema spec grammar: "int" | "bool" | "str" | "uuid" | ["str"] (list of str)
# | {fixed keys → spec} (exact key set) | ("map", allowed_keys, value_spec).
PAYLOAD_SCHEMA_V2: Dict[str, Any] = {
    "sharing_id": "uuid",
    "schema_version": "int",
    "shared_at": "str",
    "window_days": "int",
    "backfill": "bool",
    "instance": {
        "trinity_version": "str",
        "edition": "str",
        "platform": "str",
        "python_version": "str",
        "install_source": "str",
    },
    "enterprise_features": ["str"],
    "counts": {
        "agents": "int",
        "executions_total": "int",
        "executions_success": "int",
        "executions_failed": "int",
    },
    "activation_funnel": {step: "int" for step in _FUNNEL_STEPS},
    "outcomes": {
        "by_trigger": ("map", WIRE_TRIGGER_BUCKETS, {"total": "int", "success": "int", "failed": "int"}),
        "by_status": ("map", WIRE_STATUSES, "int"),
        "provider_failures": {kind: "int" for kind in PROVIDER_FAILURE_KINDS},
    },
}
# A key that must never appear ANYWHERE in the payload — the identified install
# identity. Exact key-set checks already exclude it; this is the belt.
BANNED_KEYS = frozenset({"installation_id"})


class TelemetryPayloadSchemaError(ValueError):
    """The built payload does not match ``PAYLOAD_SCHEMA_V2``. Nothing is sent."""


def _check(value: Any, spec: Any, path: str, errors: List[str]) -> None:
    if isinstance(spec, str):
        if spec == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                errors.append(f"{path}: expected int")
        elif spec == "bool":
            if not isinstance(value, bool):
                errors.append(f"{path}: expected bool")
        elif spec == "str":
            if not isinstance(value, str) or len(value) > 200:
                errors.append(f"{path}: expected short str")
        elif spec == "uuid":
            if not isinstance(value, str) or not _UUID_RE.match(value):
                errors.append(f"{path}: expected uuid")
        else:  # pragma: no cover - a typo in the spec itself
            errors.append(f"{path}: unknown spec {spec!r}")
        return
    if isinstance(spec, list):
        if not isinstance(value, list):
            errors.append(f"{path}: expected list")
            return
        for i, item in enumerate(value):
            _check(item, spec[0], f"{path}[{i}]", errors)
        return
    if isinstance(spec, tuple) and spec and spec[0] == "map":
        _, allowed, value_spec = spec
        if not isinstance(value, dict):
            errors.append(f"{path}: expected object")
            return
        for k, v in value.items():
            if k not in allowed:
                errors.append(f"{path}.{k}: key not in the documented vocabulary")
                continue
            _check(v, value_spec, f"{path}.{k}", errors)
        return
    if isinstance(spec, dict):
        if not isinstance(value, dict):
            errors.append(f"{path}: expected object")
            return
        unknown = set(value) - set(spec)
        missing = set(spec) - set(value)
        for k in sorted(unknown):
            errors.append(f"{path}.{k}: undocumented key")
        for k in sorted(missing):
            errors.append(f"{path}.{k}: missing")
        for k, sub in spec.items():
            if k in value:
                _check(value[k], sub, f"{path}.{k}", errors)
        return
    errors.append(f"{path}: unknown spec shape")  # pragma: no cover


def _find_banned(value: Any, path: str, errors: List[str]) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            if k in BANNED_KEYS:
                errors.append(f"{path}.{k}: banned key")
            _find_banned(v, f"{path}.{k}", errors)
    elif isinstance(value, list):
        for i, item in enumerate(value):
            _find_banned(item, f"{path}[{i}]", errors)


def validate_payload(payload: Dict) -> None:
    """Raise ``TelemetryPayloadSchemaError`` unless ``payload`` matches
    ``PAYLOAD_SCHEMA_V2`` exactly (no unknown keys, right types, documented
    vocabularies) and carries no banned key."""
    errors: List[str] = []
    _check(payload, PAYLOAD_SCHEMA_V2, "payload", errors)
    _find_banned(payload, "payload", errors)
    if isinstance(payload, dict) and payload.get("schema_version") != PAYLOAD_SCHEMA_VERSION:
        errors.append("payload.schema_version: not the current version")
    if errors:
        raise TelemetryPayloadSchemaError("; ".join(errors[:8]))


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


def _read(key: str, default=None):
    """Fenced settings read — a raise degrades to ``default``."""
    try:
        return db.get_setting_value(key, default)
    except Exception:  # noqa: BLE001
        return default


def _claim(key: str, value: str) -> bool:
    """Write-once claim, atomic across workers (``insert_setting_if_absent``,
    #2380 — a PRIMARY KEY conflict, not a read-then-write). Returns True when
    THIS call wrote the row. Falls back to the upsert only when the write-once
    primitive is unavailable (an isolation harness stubbing ``db`` wholesale),
    so the production path is never the racy one."""
    try:
        inserted = db.insert_setting_if_absent(key, value)
    except Exception:  # noqa: BLE001
        inserted = None
    if inserted is True or inserted is False:
        return inserted
    # Not a real bool: the facade was stubbed. Keep the row consistent anyway.
    if not _read(key, ""):
        try:
            db.set_setting(key, value)
            return True
        except Exception:  # noqa: BLE001
            return False
    return False


def get_or_mint_sharing_id() -> str:
    """The share identity: minted once per consent episode (ent#437).

    Distinct from ``installation_id`` by construction — never derived from it,
    never stored beside it in a payload. Minted with a write-once claim so two
    workers (or a double-clicked consent) cannot persist one id and send another.
    A missing id while consent is on (the documented manual DELETE reset) is
    self-healed here: a fresh id links to nothing.
    """
    existing = _read(KEY_SHARING_ID, "") or ""
    if isinstance(existing, str) and _UUID_RE.match(existing):
        return existing
    _claim(KEY_SHARING_ID, str(uuid.uuid4()))
    minted = _read(KEY_SHARING_ID, "") or ""
    return minted if isinstance(minted, str) else ""


def first_value_at() -> Optional[str]:
    """The warm-ask milestone: the install's first successful autonomous run.

    Derived on read with a persisted memo rather than hooked into the dispatch
    terminal — ``task_execution_service`` is a code-health hotspot under an
    in-progress decomposition (#2314). One ``LIMIT 1`` read until the milestone
    exists, then a settings read forever. None until it exists.
    """
    memo = _read(KEY_FIRST_VALUE_AT, "")
    if isinstance(memo, str) and memo:
        return memo
    try:
        at = db.first_autonomous_success_at()
    except Exception:  # noqa: BLE001
        return None
    if isinstance(at, str) and at:
        _claim(KEY_FIRST_VALUE_AT, at)
        return at
    return None


def _recent_sends() -> List[Dict]:
    """The last ``RECENT_SENDS_LIMIT`` send attempts, newest first. A corrupt or
    absent row reads as an empty list — it must never 500 the status route or
    the consent write that follows it."""
    raw = _read(KEY_RECENT_SENDS, "") or ""
    if not isinstance(raw, str) or not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [e for e in parsed if isinstance(e, dict)][:RECENT_SENDS_LIMIT]


def _record_send(entry: Dict) -> None:
    """Prepend one send attempt (success or failure) to the bounded local log.
    Best-effort: a failed write never changes the egress result."""
    try:
        sends = [entry] + _recent_sends()
        db.set_setting(KEY_RECENT_SENDS, json.dumps(sends[:RECENT_SENDS_LIMIT]))
    except Exception:  # noqa: BLE001
        logger.debug("[telemetry-share] send log write failed", exc_info=True)


def receiver_hint(recent: List[Dict]) -> Optional[str]:
    """What the newest attempt says about the receiver — a hint for the panel,
    never a verdict: ``receiver_not_live`` (404 from the DEFAULT url — ent#190 is
    not deployed), ``receiver_404`` (404 from an overridden url), ``ok``,
    ``failed``, or None when nothing has been attempted."""
    if not recent:
        return None
    newest = recent[0]
    if newest.get("ok") is True:
        return "ok"
    if newest.get("http_status") == 404:
        return "receiver_not_live" if TELEMETRY_SHARING_URL == DEFAULT_SHARE_URL else "receiver_404"
    return "failed"


def public_flags() -> Dict[str, bool]:
    """The four non-sensitive booleans the consent card gates on, served on
    ``GET /api/settings/feature-flags`` to any authenticated user. Fail-safe in
    the HIDDEN direction: a read failure reads as dismissed."""
    try:
        # Read the marker WITHOUT the swallowing `_read`: a failed read must
        # surface here so it lands on the hidden value, not on "never asked".
        try:
            dismissed = bool(db.get_setting_value(KEY_DISMISSED_AT, ""))
        except Exception:  # noqa: BLE001
            dismissed = True
        return {
            "telemetry_sharing_enabled": is_consent_enabled(),
            "telemetry_sharing_hard_disabled": is_hard_disabled(),
            "telemetry_sharing_dismissed": dismissed,
            "telemetry_sharing_first_value": bool(first_value_at()),
        }
    except Exception:  # noqa: BLE001 - never zero every other flag
        return {
            "telemetry_sharing_enabled": False,
            "telemetry_sharing_hard_disabled": is_hard_disabled(),
            "telemetry_sharing_dismissed": True,
            "telemetry_sharing_first_value": False,
        }


def get_status() -> Dict:
    """Operator-facing status for the Settings panel and the consent card."""
    try:
        backfill = int(_read(KEY_BACKFILL_DAYS, str(TELEMETRY_SHARING_BACKFILL_DEFAULT_DAYS)))
    except (TypeError, ValueError):
        backfill = TELEMETRY_SHARING_BACKFILL_DEFAULT_DAYS
    recent = _recent_sends()
    sharing_id = _read(KEY_SHARING_ID, None)
    return {
        "enabled": is_consent_enabled(),
        "hard_disabled": is_hard_disabled(),
        "consent_at": _read(KEY_CONSENT_AT, None),
        "backfill_days": backfill,
        "last_shared_at": _read(KEY_LAST_SHARED_AT, None),
        "share_url": TELEMETRY_SHARING_URL,
        "interval_hours": TELEMETRY_SHARING_INTERVAL_HOURS,
        "schema_version": PAYLOAD_SCHEMA_VERSION,
        # ent#437
        "sharing_id": sharing_id if isinstance(sharing_id, str) and sharing_id else None,
        "dismissed_at": _read(KEY_DISMISSED_AT, None),
        "first_value_at": first_value_at(),
        "backfill_delivered_at": _read(KEY_BACKFILL_DELIVERED_AT, None),
        "recent_sends": recent,
        "receiver_hint": receiver_hint(recent),
    }


def set_consent(enabled: bool, *, backfill_days: Optional[int] = None) -> Dict:
    """Record (or revoke) the sharing consent. Does NOT itself egress — the caller
    schedules an immediate backfill share on enable. Reversible: disabling stops
    the next heartbeat's egress and discards the share id (ent#437)."""
    was_enabled = is_consent_enabled()
    db.set_setting(KEY_ENABLED, "true" if enabled else "false")
    rotated = False
    if enabled:
        db.set_setting(KEY_CONSENT_AT, utc_now_iso())
        if backfill_days is not None:
            db.set_setting(KEY_BACKFILL_DAYS, str(max(int(backfill_days), 0)))
        if not was_enabled:
            # A new consent episode: the disclosed history is owed again until
            # the receiver first acknowledges it.
            try:
                db.delete_setting(KEY_BACKFILL_DELIVERED_AT)
            except Exception:  # noqa: BLE001
                pass
        before = _read(KEY_SHARING_ID, "") or ""
        get_or_mint_sharing_id()
        rotated = (_read(KEY_SHARING_ID, "") or "") != before
        # A consented install is never asked again.
        _claim(KEY_DISMISSED_AT, utc_now_iso())
    else:
        try:
            db.delete_setting(KEY_SHARING_ID)
        except Exception:  # noqa: BLE001
            logger.debug("[telemetry-share] share id delete failed", exc_info=True)
    status = get_status()
    status["sharing_id_rotated"] = bool(rotated)
    return status


def mark_ask_dismissed() -> Dict:
    """"Don't ask again": the explicit, server-side, once-per-install marker
    (ent#437). Idempotent — the first stamp wins and is never overwritten. The
    softer "Not now" is a per-browser snooze the client keeps to itself."""
    _claim(KEY_DISMISSED_AT, utc_now_iso())
    return get_status()


# ---------------------------------------------------------------------------
# Anonymized aggregate payload (inspectable before send)
# ---------------------------------------------------------------------------

def _edition_and_features() -> tuple[str, list]:
    try:
        from services.entitlement_service import entitlement_service
        feats = entitlement_service.list_entitled_features()
    except Exception:  # noqa: BLE001
        feats = []
    if not isinstance(feats, list):
        feats = []
    feats = [f for f in feats if isinstance(f, str)]
    return ("enterprise" if feats else "community"), feats


def _int(value: Any) -> int:
    """Coerce a reader's value to a non-negative int; anything else (a Mock, a
    None, a float NaN) is 0 — a stubbed reader must never reach the validator."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return max(value, 0)
    try:
        return max(int(value), 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _safe_install_source() -> str:
    try:
        raw = settings_service.get_install_source()
    except Exception:  # noqa: BLE001
        return "unknown"
    if isinstance(raw, str) and _INSTALL_SOURCE_RE.match(raw):
        return raw
    return "unknown"


def _safe_release_version() -> str:
    try:
        v = resolve_release_version()
    except Exception:  # noqa: BLE001
        return "unknown"
    return v if isinstance(v, str) and v else "unknown"


def _outcomes(hours: int) -> Dict:
    """The outcome mix (ent#437): terminal executions by trigger bucket and by
    status, plus the provider-failure kinds. Counts only. Labelled an OUTCOME
    MIX, not a failure taxonomy — the error-class taxonomy is ent#418's."""
    by_trigger: Dict[str, Dict[str, int]] = {}
    try:
        rows = db.get_fleet_execution_timeline(None, "trigger", hours)
        shaped = db.shape_execution_timeline(rows, group_by="trigger", hours=hours)
    except Exception:  # noqa: BLE001
        shaped = []
    if isinstance(shaped, list):
        for row in shaped:
            if not isinstance(row, dict):
                continue
            label = row.get("bucket")
            key = TRIGGER_WIRE_KEYS.get(label if isinstance(label, str) else "", "other")
            slot = by_trigger.setdefault(key, {"total": 0, "success": 0, "failed": 0})
            slot["total"] += _int(row.get("total"))
            slot["success"] += _int(row.get("success"))
            slot["failed"] += _int(row.get("failed"))

    by_status: Dict[str, int] = {}
    try:
        raw_status = db.count_terminal_executions_by_status(hours)
    except Exception:  # noqa: BLE001
        raw_status = {}
    if isinstance(raw_status, dict):
        for status, count in raw_status.items():
            key = status if isinstance(status, str) and status in WIRE_STATUSES else "other"
            by_status[key] = by_status.get(key, 0) + _int(count)

    provider = {kind: 0 for kind in PROVIDER_FAILURE_KINDS}
    try:
        # This reader's cutoff is unconditional, so "all-time" is spelled as a
        # window wider than the table's retention — never 0 (= "since now").
        per_sub = db.get_failure_event_counts_by_subscription(hours or _ALL_TIME_HOURS)
    except Exception:  # noqa: BLE001
        per_sub = {}
    if isinstance(per_sub, dict):
        for entry in per_sub.values():
            kinds = entry.get("by_kind") if isinstance(entry, dict) else None
            if not isinstance(kinds, dict):
                continue
            for kind in PROVIDER_FAILURE_KINDS:
                provider[kind] += _int(kinds.get(kind))

    return {"by_trigger": by_trigger, "by_status": by_status, "provider_failures": provider}


def build_aggregate_payload(
    window_days: Optional[int] = None,
    *,
    backfill: bool = False,
    sharing_id: Optional[str] = None,
) -> Dict:
    """Build the anonymized aggregate. Coarse counts + enums ONLY — no PII, no
    content. ``window_days=None``/``0`` ⇒ all-time counts (used for backfill).

    ``sharing_id`` is the real id at send time; a preview before consent shows
    the fixed placeholder so no identity is minted by looking. Every reader is
    fenced and every value coerced at the boundary, because isolation harnesses
    stub ``db`` and ``services.*`` wholesale and a Mock must never reach the
    validator (learnings 2026-08-03).
    """
    since = iso_cutoff(hours=window_days * 24) if (window_days and window_days > 0) else None
    hours = (window_days * 24) if (window_days and window_days > 0) else 0
    edition, features = _edition_and_features()

    try:
        exec_stats = db.get_fleet_execution_stats(None, hours=hours) or {}
    except Exception:  # noqa: BLE001
        exec_stats = {}
    if not isinstance(exec_stats, dict):
        exec_stats = {}

    try:
        funnel_raw = db.count_product_events_by_type(since=since) or {}
    except Exception:  # noqa: BLE001
        funnel_raw = {}
    if not isinstance(funnel_raw, dict):
        funnel_raw = {}
    funnel = {step: _int(funnel_raw.get(step, 0)) for step in _FUNNEL_STEPS}

    try:
        agent_count = _int(db.count_non_system_agents())
    except Exception:  # noqa: BLE001
        agent_count = 0

    if sharing_id is None:
        existing = _read(KEY_SHARING_ID, "") or ""
        sharing_id = existing if (isinstance(existing, str) and _UUID_RE.match(existing)) else PREVIEW_SHARING_ID

    return {
        "sharing_id": sharing_id,
        "schema_version": PAYLOAD_SCHEMA_VERSION,
        "shared_at": utc_now_iso(),
        "window_days": _int(window_days or 0),
        "backfill": bool(backfill),
        "instance": {
            "trinity_version": _safe_release_version(),
            "edition": edition,
            "platform": platform.system() or "unknown",
            "python_version": platform.python_version(),
            "install_source": _safe_install_source(),
        },
        # Coarse capability list — already exposed via /api/version; no secrets.
        "enterprise_features": features,
        "counts": {
            "agents": agent_count,
            "executions_total": _int(exec_stats.get("total", 0)),
            "executions_success": _int(exec_stats.get("success_count", 0)),
            "executions_failed": _int(exec_stats.get("failed_count", 0)),
        },
        "activation_funnel": funnel,
        "outcomes": _outcomes(hours),
    }


# ---------------------------------------------------------------------------
# Egress (gated, validated, fail-open on delivery)
# ---------------------------------------------------------------------------

def _days_since(iso: Any) -> Optional[int]:
    if not isinstance(iso, str) or not iso:
        return None
    try:
        then = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return max(int((datetime.now(timezone.utc) - then).total_seconds() // 86400), 0)


def _resolve_window(backfill: bool, window_days: Optional[int]) -> tuple[bool, int]:
    """Decide what this send covers.

    ent#437: the consent-time backfill is retried by every heartbeat until the
    receiver first acknowledges it — otherwise the disclosed history is lost
    forever for every install that consented before ent#190 existed. Once
    delivered, a heartbeat covers everything since the last successful share
    (cumulative, gap-free), never a fixed one-day slice.
    """
    if window_days is not None:
        return backfill, max(int(window_days), 0)
    backfill_owed = not _read(KEY_BACKFILL_DELIVERED_AT, "")
    if backfill or backfill_owed:
        try:
            days = int(_read(KEY_BACKFILL_DAYS, str(TELEMETRY_SHARING_BACKFILL_DEFAULT_DAYS)))
        except (TypeError, ValueError):
            days = TELEMETRY_SHARING_BACKFILL_DEFAULT_DAYS
        return True, max(days, 0)
    since_last = _days_since(_read(KEY_LAST_SHARED_AT, ""))
    floor = max(TELEMETRY_SHARING_INTERVAL_HOURS // 24, 1)
    return False, max(since_last if since_last is not None else floor, floor)


async def share_now(*, backfill: bool = False, window_days: Optional[int] = None) -> bool:
    """POST one anonymized aggregate to the hosted intake, IF both gates allow.

    Returns True only on a genuine 2xx. Never raises — best-effort. Both gates
    (config hard-switch + stored consent) are re-checked here so a stale caller
    can't force an egress. The payload is validated against the documented
    schema BEFORE it leaves: a violation is refused, logged, and recorded —
    never sent (fail-closed egress).
    """
    try:
        if is_hard_disabled():
            logger.info("[telemetry-share] disabled (TELEMETRY_SHARING_ENABLED / DO_NOT_TRACK)")
            return False
        if not is_consent_enabled():
            return False  # not opted in — nothing leaves the box

        backfill, window_days = _resolve_window(backfill, window_days)
        sharing_id = get_or_mint_sharing_id()

        # Three table scans: off the event loop, so a 03:30 backup-window tick
        # or an admin preview cannot stall every request for the busy timeout.
        payload = await asyncio.to_thread(
            build_aggregate_payload, window_days, backfill=backfill, sharing_id=sharing_id
        )
        entry: Dict[str, Any] = {
            "sent_at": utc_now_iso(),
            "backfill": bool(backfill),
            "window_days": int(window_days),
            "ok": False,
            "http_status": None,
            "error": None,
            "payload": payload,
        }
        try:
            validate_payload(payload)
        except TelemetryPayloadSchemaError as e:
            logger.error("[telemetry-share] payload refused by schema, NOT sent: %s", e)
            entry["error"] = "schema"
            _record_send(entry)
            return False

        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
                resp = await client.post(TELEMETRY_SHARING_URL, json=payload)
        except Exception as e:  # noqa: BLE001 — delivery is best-effort
            entry["error"] = type(e).__name__
            _record_send(entry)
            logger.info("[telemetry-share] skipped (ignored): %s", type(e).__name__)
            return False

        entry["http_status"] = int(resp.status_code)
        if 200 <= resp.status_code < 300:
            entry["ok"] = True
            db.set_setting(KEY_LAST_SHARED_AT, utc_now_iso())
            if backfill:
                _claim(KEY_BACKFILL_DELIVERED_AT, utc_now_iso())
            _record_send(entry)
            logger.info(
                "[telemetry-share] shared (share %s…, backfill=%s, window=%sd)",
                str(sharing_id)[:8], backfill, window_days,
            )
            return True
        _record_send(entry)
        logger.warning("[telemetry-share] POST returned HTTP %s", resp.status_code)
        return False
    except Exception as e:  # noqa: BLE001 — fire-and-forget, swallow everything
        logger.info("[telemetry-share] skipped (ignored): %s", type(e).__name__)
        return False


# Strong references for fire-and-forget sends: a bare ``asyncio.create_task``
# can be garbage-collected mid-flight, and the consent-time backfill would then
# vanish with nothing in the send log to say so (the #526 `_spawn_bg` footgun).
_background_tasks: Set["asyncio.Task"] = set()


def spawn_share(*, backfill: bool = False) -> None:
    task = asyncio.create_task(share_now(backfill=backfill))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


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

    def _claim_tick(self) -> bool:
        """One send per interval across workers: a TTL-held tick marker, never
        released (a mutex released after the POST would dedupe nothing — the
        loops drift by jitter). Fail-open when Redis is unavailable."""
        try:
            lock = SingleFlightLock(
                _TICK_LOCK_KEY, max(self.interval_seconds // 2, 60), client=get_breaker_redis()
            )
            return bool(lock.acquire())
        except Exception:  # noqa: BLE001 - never block a heartbeat on the marker
            return True

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
            if not self._claim_tick():
                logger.debug("[telemetry-share] tick already claimed by a sibling worker")
                continue
            await share_now(backfill=False)


telemetry_sharing_service = TelemetrySharingService()
