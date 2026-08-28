"""
Cleanup Service for Trinity platform.

Background service that periodically cleans up stale resources:
- Reconciles DB execution state against agent process registries (Issue #129)
- Auto-terminates executions exceeding their schedule timeout (Issue #129)
- Marks stale executions (running > threshold) as failed
- Marks stale activities (started > threshold) as failed
- Cleans up stale Redis slots

Runs every 5 minutes with a one-shot startup sweep.
"""

import asyncio
import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Dict, List

import httpx

from database import db
from models import ActivityState, TaskExecutionStatus
from services import event_dispatch_service
# #2433: eager, module-level — a stdlib leaf. A call-time lazy import here is
# the learnings-2026-08-12 shape: under a leaked `sys.modules` MagicMock every
# candidate would read as "in flight" and the watchdog would silently skip
# every row. Bound once so tests patch `cleanup_service._inflight_verdicts`,
# never a package attribute a lazy import would bypass.
from services import agent_call_limiter
from services.agent_auth import build_agent_auth_headers
from services.capacity_manager import get_capacity_manager
from services.slot_service import SLOT_TTL_BUFFER
from utils.helpers import utc_now, utc_now_iso, parse_iso_timestamp
from utils.credential_sanitizer import sanitize_text

logger = logging.getLogger(__name__)

_inflight_verdicts = agent_call_limiter.inflight_verdicts


async def _inflight_verdict_map(execution_ids) -> Dict[str, str]:
    """#2433: ``{execution_id: "alive" | "absent" | "unknown"}`` for the given
    ids, guarded so a stub leak can never read as "everything is in flight":
    anything that is not a dict of string verdicts collapses to ``absent``."""
    ids = [eid for eid in execution_ids if isinstance(eid, str) and eid]
    if not ids:
        return {}
    try:
        raw = await _inflight_verdicts(ids)
    except Exception as e:  # noqa: BLE001 — fail-open to the pre-#2433 behaviour
        logger.warning(f"[Watchdog] in-flight verdict lookup failed ({e}) — treating as absent")
        return {eid: "absent" for eid in ids}
    if not isinstance(raw, dict):
        return {eid: "absent" for eid in ids}
    out: Dict[str, str] = {}
    for eid in ids:
        verdict = raw.get(eid)
        out[eid] = verdict if verdict in ("alive", "absent", "unknown") else "absent"
    return out


def _inflight_skip(verdict: str, age_seconds: float) -> bool:
    """#2433: should orphan recovery be withheld for this row?

    ``alive`` — a live backend dispatcher owns it: never orphan.
    ``unknown`` — the cross-worker marker could not be asked (slow/flapping
    Redis): withhold for as long as a dispatcher COULD still own the row
    (#2196's rule — a read that could not be asked is not a read that said
    no); rows older than that bound cannot be in flight and are orphaned.
    ``absent`` — orphan.
    """
    if verdict == "alive":
        return True
    if verdict == "unknown":
        return age_seconds < agent_call_limiter.inflight_max_age_seconds()
    return False


def _row_age_seconds(execution: Dict) -> float:
    """Age of an execution row from its `started_at`; unparseable ⇒ +inf, so
    an `unknown` in-flight verdict never withholds recovery on garbage."""
    raw = execution.get("started_at")
    try:
        return (utc_now() - parse_iso_timestamp(raw)).total_seconds()
    except Exception:  # noqa: BLE001
        return float("inf")


def _orphan_error_message(agent_name: str, agent_reports_pending: bool) -> str:
    """#2433: say what was OBSERVED. The old text — "Execution completed on
    agent but status not reported" — asserted a completion for a row the agent
    had never received."""
    agent_side = (
        "not running, not pending, not recently completed"
        if agent_reports_pending
        else "not running, not recently completed"
    )
    return (
        f"Execution not tracked by agent '{agent_name}' ({agent_side}) and no live "
        f"backend dispatcher (not parked in this worker, no cross-worker marker) "
        f"— recovered by watchdog"
    )

# Configuration
CLEANUP_INTERVAL_SECONDS = 300  # 5 minutes
EXECUTION_STALE_TIMEOUT_MINUTES = 120  # SCHED-ASYNC-001: increased from 30 to support long-running tasks
# #1083: prefix tagged onto the slot-reclaim FAILED message so a fire-and-forget
# lease expiry (no result callback before the slot TTL) is identifiable. Mirrors
# TaskExecutionErrorCode.LEASE_EXPIRED.value; kept as a literal to avoid importing
# the execution service into the cleanup background loop. The existing descriptive
# text is preserved after the prefix (substring assertions stay green).
_LEASE_EXPIRED_TAG = "lease_expired"

# #1714: bulk watchdog sweeps (stale / no-session) fail many rows in one cycle
# with no per-row context, so they emitted no agent.task.failed event and never
# woke a subscribed orchestrator (the #1578 residual). We now emit per CAS-won
# row. To avoid a thundering herd on a large sweep, the emits are paced: after
# each BATCH the loop yields for PACE_S so N create_task spawns don't fire at
# once. No row is dropped (unlike a hard cap) — pacing bounds the burst, matching
# subscription gating in emit_task_terminal_event bounds who is woken, and the
# cheap has_task_terminal_subscribers() gate makes a no-subscriber sweep free.
_BULK_TERMINAL_EMIT_BATCH = 50
_BULK_TERMINAL_EMIT_PACE_S = 0.1
ACTIVITY_STALE_TIMEOUT_MINUTES = 120  # SCHED-ASYNC-001: increased from 30 to support long-running tasks
NO_SESSION_TIMEOUT_SECONDS = 60  # Issue #106: fast-fail executions that never got a Claude session
WATCHDOG_HTTP_TIMEOUT = 15.0  # Timeout for agent HTTP calls during reconciliation (#869: increased from 5s to handle agents under load)
WATCHDOG_MIN_AGE_SECONDS = 60  # Don't orphan-recover executions younger than this (dispatch window)
STARTUP_RECOVERY_GRACE_SECONDS = 15  # #748: skip startup orphan-recovery for rows
                                     # whose started_at is within this window — they
                                     # may be from an in-flight /internal/execute-task
                                     # call that races the backend startup.
# #749: grace window for the Redis-side orphan-slot sweep. A slot whose
# ZSET score (unix seconds, recorded at ZADD time) is within this many
# seconds of "now" may belong to a concurrent /internal/execute-task
# handler that has done its ZADD but not yet inserted the SQL row.
# Symmetric with the SQL-side window in `recover_orphaned_executions`.
SLOT_RECOVERY_GRACE_SECONDS = 15
# Terminal SQL statuses that mean "the row is done; any matching Redis
# slot is a leak." Mirrors the values in models.TaskExecutionStatus.
_TERMINAL_EXECUTION_STATUSES = {"success", "failed", "cancelled", "skipped"}
# #749: members starting with this prefix are drain sentinels used by
# the capacity manager — not real executions — and must be skipped by
# the orphan-slot sweep. Matches the canary S-01 filter
# (`canary/invariants/s01_slot_row_bijection.py:DRAIN_PREFIX`).
_DRAIN_SENTINEL_PREFIX = "drain-"
ERROR_FETCH_TIMEOUT = 2.0  # Issue #286: short timeout for fetching error context from agent
MAX_ERROR_MESSAGE_LENGTH = 2000  # Issue #286: truncate combined error messages
# Issue #772: per-TRANSACTION row budget for retention sweeps. Bounds how many
# rows each commit touches, so a large backfill isn't held in a single
# multi-minute write lock.
#
# #1644 — READ THIS, THE NAME LIES. This is NOT a per-cycle cap. Six of the seven
# prune accessors (`prune_execution_logs`, `prune_execution_rows`,
# `cleanup_old_health_records`, `prune_agent_reports`, and the two soft-delete
# finders) loop `while True` and drain the ENTIRE candidate set in one call —
# `chunk_size` only sizes each transaction inside that loop. So a single sweep
# deletes everything past the cutoff, immediately. #1638 is the proof: it
# destroyed 5352 rows in one startup sweep, which a real 5000-row cap could not
# have produced. Only `prune_operator_queue_terminal_items` (`limit=`) truly caps.
#
# This matters for anyone reasoning about blast radius: there is no "spread over
# hours" to rely on. The bound on destruction is `retention_guard`, not this
# constant. `_log_prune`'s `pruned >= 5000` WARNING is therefore a heuristic
# ("this was a big prune"), not the cap-detector its name suggests.
RETENTION_CHUNK_SIZE_PER_CYCLE = 5000

# Issue #1581: orphan agent-volume reclaim. A data volume whose ownership row is
# gone (backend crashed mid-create, or the purge-time removal hit an in-use
# volume and skipped it) is reclaimable — but bounded per cycle so a large
# backlog can't stall the loop's other sweeps, and gated by a grace window so a
# volume created seconds before its ownership row is never mistaken for an
# orphan (creation writes the volume BEFORE the row).
ORPHAN_VOLUME_RECLAIM_PER_CYCLE = 100
ORPHAN_VOLUME_GRACE_SECONDS = 3600  # 1h — comfortably past any create latency
# Issue #1664: consecutive cycles a candidate volume must be observed unattached
# before it may be reclaimed. A container recreate (guardrails / resources /
# shared-folder change, operator rebuild) removes the old container before
# creating the new one, so a LIVE agent's volume is briefly unattached; one
# sighting is not evidence of an orphan. 3 strikes ≈ 15 min at the 5-min cycle —
# orders of magnitude longer than any recreate gap, and this is a backstop sweep
# with no urgency (the purge path is the primary reclaim).
ORPHAN_VOLUME_UNATTACHED_STRIKES = 3

# Issue #1142: `responded` operator_queue rows carry an operator answer the 5s
# write-back loop still has to deliver to the agent file (a stopped agent picks
# it up on restart), so they are never deleted younger than this generous floor —
# independent of a shorter operator_queue_retention_days set for terminal rows.
OPERATOR_QUEUE_RESPONDED_MIN_RETENTION_DAYS = 30

# trinity-enterprise#69: ephemeral ghost GC bounds. Discards do serial Docker
# I/O (stop/remove) — cap per cycle + per-discard timeout so a burst of expired
# ghosts can't stall the watchdog/slot sweeps for the whole fleet.
EPHEMERAL_DISCARDS_PER_CYCLE = 10
EPHEMERAL_DISCARD_TIMEOUT_S = 60
# Newborn grace: creation writes the ownership row LAST (template clone/image
# pull can stretch the gap to minutes) — the orphan pass must not reap a
# healthy mid-creation ghost. ~3 sweep cycles.
EPHEMERAL_ORPHAN_GRACE_S = 900

# WebSocket manager (injected from main.py)
_ws_manager = None


def set_cleanup_ws_manager(manager):
    """Set the WebSocket manager for watchdog event broadcasting."""
    global _ws_manager
    _ws_manager = manager


class _KnownIds(set):
    """The agent-known id set, tagged with whether the agent's image reports
    the #2433 `pending_ids` field (so the orphan error string can say
    "not pending" only when that was actually observed)."""

    reports_pending: bool = False


def _extract_agent_known_ids(payload: Dict) -> set:
    """Set of execution IDs the agent considers 'known': currently-running
    plus the recently-completed window (#921) plus, since #2433, the ids the
    agent has ACCEPTED but not yet spawned (`pending_ids` — queued in its
    thread pool, or waiting on the chat execution lock).

    Single source of truth for parsing the `/api/executions/running`
    response so the periodic watchdog (`_reconcile_orphaned_executions`)
    and the startup recovery (`recover_orphaned_executions`) can't drift
    out of sync. Defensive against malformed entries and missing fields:
    older agent images that haven't shipped the buffer return only the
    `executions` field — the union degrades silently to pre-#921 behaviour,
    and an image without `pending_ids` degrades to pre-#2433 behaviour.
    """
    ids = _KnownIds(
        eid for ex in (payload.get("executions") or [])
        if isinstance(ex, dict) and (eid := ex.get("execution_id"))
    )
    recent = payload.get("recently_completed_ids")
    if isinstance(recent, (list, tuple, set)):
        ids.update(eid for eid in recent if isinstance(eid, str))
    pending = payload.get("pending_ids")
    if isinstance(pending, (list, tuple, set)):
        ids.reports_pending = True
        ids.update(eid for eid in pending if isinstance(eid, str))
    return ids


def _read_retention_settings() -> tuple[int, int, int, int]:
    """Read retention windows from ops settings (#772, #918).

    Returns:
        (execution_log_retention_days, execution_row_retention_days,
         health_check_retention_days, agent_reports_retention_days).
        0 means the corresponding sweep is disabled. Invalid (non-integer or
        negative) values are coerced to 0 so a malformed setting can't
        accidentally enable an unbounded prune.
    """
    from services.settings_service import OPS_SETTINGS_DEFAULTS

    def _read(key: str) -> int:
        raw = db.get_setting_value(key, OPS_SETTINGS_DEFAULTS.get(key, "0"))
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return 0
        return max(value, 0)

    return (
        _read("execution_log_retention_days"),
        _read("execution_row_retention_days"),
        _read("health_check_retention_days"),
        _read("agent_reports_retention_days"),
    )


def _read_retention_setting(key: str) -> int:
    """Read ONE retention window, with the same coercion as the 4-tuple reader.

    Sweeps added after `_read_retention_settings` was written read their own key
    rather than growing that tuple (the #1296 precedent). This is that read,
    factored out so a third and fourth caller don't re-inline it.

    Fail-safe in the #1638 direction: anything unparseable, negative, or
    unreadable becomes `0`, which DISABLES the sweep. A malformed setting must
    never enable an unbounded prune.
    """
    from services.settings_service import OPS_SETTINGS_DEFAULTS

    try:
        raw = db.get_setting_value(key, OPS_SETTINGS_DEFAULTS.get(key, "0"))
        return max(int(raw), 0)
    except (TypeError, ValueError):
        return 0
    except Exception as e:  # noqa: BLE001 — an unreadable setting disables, never enables
        logger.error(f"[Cleanup] Could not read retention setting {key}: {e}")
        return 0


def _log_prune(pruned: int, message: str) -> None:
    """Log a completed prune, escalating a chunk-capped sweep to WARNING (#1638).

    Mass deletion used to log at INFO — the same level as a routine no-op — so
    destroying 95% of a table produced no signal an operator or alert could see.

    A trickle stays INFO: a healthy install prunes a few rows every cycle, and
    WARNING there would be pure alarm fatigue. Hitting the per-cycle cap means a
    backlog is still draining, which is exactly the signature of a retention
    window that just got narrower — the #1638 case. Chunking is why this needs a
    level rule at all: it splits one catastrophic delete into a long sequence of
    individually-unremarkable lines.
    """
    if pruned >= RETENTION_CHUNK_SIZE_PER_CYCLE:
        logger.warning(f"{message} — per-cycle cap hit, more rows remain (#1638)")
    else:
        logger.info(message)


def _guard_allows(
    setting_key: str,
    label: str,
    window_days: int,
    count_fn,
    floor=None,
) -> bool:
    """#1644 blast-radius gate. True = the prune may run.

    Called INLINE inside each sweep's existing try/except and immediately before
    its `db.prune_*` call — so even an unexpected raise here skips the prune
    (fail-closed) rather than falling through to it.

    `retention_guard.evaluate` does not raise (#1833): every failure inside it —
    including a `count_fn` that returns a non-number or a negative sentinel —
    returns a REFUSING verdict, so the refusal keeps its ERROR and its durable
    operator-queue alarm. That claim used to be nearly-true and load-bearing in
    the wrong direction; it is now true of the code, not just of the docstring.

    This belt stays anyway. It is the difference between "the prune is skipped"
    and "the prune is skipped *and nobody is told*": a raise here would still
    protect the DATA (control never reaches `db.prune_*`) while destroying the
    SIGNAL, surfacing as a nondescript "Error pruning ..." with no refusal alarm.
    Belt and braces on the one code path where being wrong is unrecoverable.
    """
    from services import retention_guard

    verdict = retention_guard.evaluate(setting_key, window_days, count_fn, floor)
    if verdict.allowed:
        retention_guard.note_allowed(setting_key)
        return True
    retention_guard.announce_refusal(setting_key, label, window_days, verdict)
    return False


def _after_guarded_prune(setting_key: str) -> None:
    """Consume a single-use ack once its prune has actually completed (#1644).

    No-op when no ack exists (the common, under-threshold path). Called only after
    the prune returns — if it raised, the ack survives so the operator isn't asked
    to approve the same intent twice.
    """
    from services import retention_guard

    try:
        retention_guard.consume_acknowledgement(setting_key)
    except Exception as e:
        # A stale ack is a (small) safety hole, not a data-loss one: it would let
        # ONE more mass prune through at this same window. Log loudly, never raise
        # into a sweep that has already deleted rows.
        logger.error(
            f"[Cleanup] Could not consume retention ack for {setting_key}: {e}"
        )


def log_effective_retention_windows() -> None:
    """Log each retention window and WHERE IT CAME FROM, before any sweep (#1638).

    This is the signal whose absence made #1638 invisible. A `code-default`
    window is one that a future edit to `OPS_SETTINGS_DEFAULTS` can move under
    the operator's feet; a `db-row` window is one they chose. Emitting both at
    boot — *before* the startup sweep deletes anything — turns a silent
    retroactive default change into something visible in the first screen of
    logs, and gives a soak test something to catch.

    Never raises: this is observability on the boot path.
    """
    try:
        from services.settings_service import OPS_SETTINGS_DEFAULTS, RETENTION_OPS_KEYS

        parts = []
        for key in RETENTION_OPS_KEYS:
            row = db.get_setting_value(key, None)
            source = "db-row" if row is not None else "code-default"
            if key == "backup_retention_days":
                # #2216: rendered through the ONE shared reader — its coercion
                # is inverted (garbage → 14, never → 0/keep-forever), so this
                # log must not disagree with the backup service on a malformed
                # row (the two-readers-disagree defect the plan names).
                from services.db_backup_service import (
                    effective_backup_retention_days,
                )
                value = effective_backup_retention_days()
            else:
                value = row if row is not None else OPS_SETTINGS_DEFAULTS.get(key, "?")
            parts.append(f"{key}={value}d ({source})")
        logger.info(f"[Cleanup] Effective retention windows: {'; '.join(parts)}")
    except Exception as e:
        logger.warning(f"[Cleanup] Could not report effective retention windows: {e}")


def _wal_checkpoint_truncate() -> None:
    """Run PRAGMA wal_checkpoint(TRUNCATE) to return freed pages to the OS.

    Called after retention sweeps reclaim measurable space. TRUNCATE mode is
    safe under concurrent readers — it only blocks if another writer holds
    the lock, in which case the checkpoint returns busy and we move on.

    SQLite-only: WAL is a SQLite concept. On PostgreSQL (#300) this is a no-op
    — the server manages its own WAL/autovacuum.
    """
    from db.engine import is_sqlite
    if not is_sqlite():
        return
    from db.connection import get_db_connection

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        # PRAGMA result is (busy, log_pages, checkpointed) — log at debug
        # only; non-zero busy is normal under contention.
        try:
            row = cursor.fetchone()
            if row is not None:
                logger.debug(
                    "[Cleanup] wal_checkpoint(TRUNCATE) "
                    f"busy={row[0]} log_pages={row[1]} checkpointed={row[2]}"
                )
        except Exception:
            pass


@dataclass
class CleanupReport:
    """Results from a single cleanup cycle."""
    orphaned_executions: int = 0
    auto_terminated: int = 0
    stale_executions: int = 0
    no_session_executions: int = 0
    orphaned_skipped: int = 0
    stale_activities: int = 0
    stale_slots: int = 0
    stale_slot_executions: int = 0  # Issue #219: executions failed when their slot was reclaimed
    shared_files_purged: int = 0  # C4 / FILES-001: expired or old-revoked file shares
    # Issue #772: retention sweeps
    execution_logs_pruned: int = 0
    execution_rows_pruned: int = 0
    health_checks_pruned: int = 0
    # Issue #1449: backlog_metadata PII scrubbed on authoritative-terminal rows
    backlog_metadata_scrubbed: int = 0
    legacy_tool_calls_converted: int = 0  # #1741
    orphaned_agent_keys_revoked: int = 0  # #1745
    # Issue #834 Phase 1a: soft-deleted agents purged past their retention window
    soft_deleted_agents_purged: int = 0
    # Issue #834 Phase 1b: soft-deleted schedules purged past their retention window
    soft_deleted_schedules_purged: int = 0
    # RELIABILITY-006 / #525: idempotency keys purged past their 24h TTL
    idempotency_keys_purged: int = 0
    # Issue #918: agent_reports rows pruned past their retention window
    agent_reports_pruned: int = 0
    # #1081 Phase 3 (#429/#1402): lease-reaper — pull rows re-queued / poison-parked
    expired_leases_requeued: int = 0
    expired_leases_parked: int = 0
    # Issue #1581: Docker data volumes removed at retention hard-purge +
    # orphan volumes (no ownership row, past grace) reclaimed by the sweep
    agent_volumes_removed: int = 0
    orphan_agent_volumes_reclaimed: int = 0
    # trinity-enterprise#69: ephemeral ghosts discarded (DB pass) + orphan
    # ephemeral containers reclaimed (Docker-as-truth pass)
    ephemeral_agents_discarded: int = 0
    ephemeral_orphans_reclaimed: int = 0
    # Issue #1142: terminal operator_queue rows deleted past their retention window
    operator_queue_pruned: int = 0
    # Issue #1616: expired ephemeral SSH keys removed from agent authorized_keys
    ssh_credentials_expired: int = 0
    # Issue #1296: terminal agent_reminders rows deleted past their retention window
    agent_reminders_pruned: int = 0
    # ent#433: subscription headroom probe history pruned past its window, and
    # subscription rate-limit/auth failure events pruned past theirs (the latter
    # replaces a hardcoded 24h sweep that had no window and no guard).
    headroom_history_pruned: int = 0
    rate_limit_events_pruned: int = 0
    # Issue #1804: dispatch activities closed by a recovery path that won the
    # terminal CAS (watchdog, startup recovery, the bulk sweeps). Post-merge
    # signal: `stale_activities` should trend to ~0 while this picks up the
    # volume — a non-zero `stale_activities` means a producer is still unowned.
    activities_closed_on_recovery: int = 0
    # #2433: rows the watchdog / Phase-3 re-verify would have orphaned but
    # withheld because a live backend dispatcher owns them (parked in the
    # agent-call queue or mid-call) or the cross-worker marker could not be
    # asked. Observability only — not a recovery, so NOT summed into `total`.
    dispatch_inflight_skipped: int = 0

    @property
    def total(self) -> int:
        return (self.orphaned_executions + self.auto_terminated +
                self.stale_executions + self.no_session_executions +
                self.orphaned_skipped + self.stale_activities + self.stale_slots +
                self.stale_slot_executions + self.shared_files_purged +
                self.execution_logs_pruned + self.execution_rows_pruned +
                self.backlog_metadata_scrubbed +
                self.health_checks_pruned + self.soft_deleted_agents_purged +
                self.soft_deleted_schedules_purged + self.idempotency_keys_purged +
                self.agent_reports_pruned +
                self.expired_leases_requeued + self.expired_leases_parked +
                self.agent_volumes_removed + self.orphan_agent_volumes_reclaimed +
                self.ephemeral_agents_discarded + self.ephemeral_orphans_reclaimed +
                self.operator_queue_pruned + self.ssh_credentials_expired +
                self.agent_reminders_pruned +
                self.headroom_history_pruned + self.rate_limit_events_pruned)
    # NOTE (#1804): activities_closed_on_recovery is deliberately NOT summed
    # into `total` — it is an observability counter over work already counted
    # by the sweep that closed the execution, not additional cleanup work.

    def to_dict(self) -> Dict:
        return {
            "orphaned_executions": self.orphaned_executions,
            "auto_terminated": self.auto_terminated,
            "stale_executions": self.stale_executions,
            "no_session_executions": self.no_session_executions,
            "orphaned_skipped": self.orphaned_skipped,
            "dispatch_inflight_skipped": self.dispatch_inflight_skipped,
            "stale_activities": self.stale_activities,
            "stale_slots": self.stale_slots,
            "stale_slot_executions": self.stale_slot_executions,
            "shared_files_purged": self.shared_files_purged,
            "execution_logs_pruned": self.execution_logs_pruned,
            "execution_rows_pruned": self.execution_rows_pruned,
            "backlog_metadata_scrubbed": self.backlog_metadata_scrubbed,
            "legacy_tool_calls_converted": self.legacy_tool_calls_converted,
            "orphaned_agent_keys_revoked": self.orphaned_agent_keys_revoked,
            "health_checks_pruned": self.health_checks_pruned,
            "soft_deleted_agents_purged": self.soft_deleted_agents_purged,
            "soft_deleted_schedules_purged": self.soft_deleted_schedules_purged,
            "idempotency_keys_purged": self.idempotency_keys_purged,
            "agent_reports_pruned": self.agent_reports_pruned,
            "expired_leases_requeued": self.expired_leases_requeued,
            "expired_leases_parked": self.expired_leases_parked,
            "agent_volumes_removed": self.agent_volumes_removed,
            "orphan_agent_volumes_reclaimed": self.orphan_agent_volumes_reclaimed,
            "operator_queue_pruned": self.operator_queue_pruned,
            "ssh_credentials_expired": self.ssh_credentials_expired,
            "agent_reminders_pruned": self.agent_reminders_pruned,
            "headroom_history_pruned": self.headroom_history_pruned,
            "rate_limit_events_pruned": self.rate_limit_events_pruned,
            "activities_closed_on_recovery": self.activities_closed_on_recovery,
            "total": self.total,
        }


class CleanupService:
    """Background service that cleans up stale resources."""

    def __init__(self, poll_interval: int = CLEANUP_INTERVAL_SECONDS):
        self.poll_interval = poll_interval
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._lock = asyncio.Lock()
        self.last_run_at: Optional[str] = None
        self.last_report: Optional[CleanupReport] = None
        # Cumulative counters for the #306 soak dashboard. Monotonic, reset on
        # process restart. Zero orphan recoveries over 2 weeks is the gate.
        self.cumulative_orphaned: int = 0
        self.cumulative_auto_terminated: int = 0
        # #476: Cycle counter gates hourly maintenance (rate-limit-event prune)
        # inside the 5-min cleanup loop. First cycle runs maintenance
        # immediately; then every 12th cycle (60 min at 5-min interval).
        self._cycle_count: int = 0
        # #1664: volume name -> consecutive cycles observed unattached AND
        # unowned. In-process only (a restart just restarts the count — the
        # safe direction).
        self._unattached_volume_strikes: Dict[str, int] = {}

    def start(self):
        """Start the background cleanup loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._cleanup_loop())
        logger.info(f"Cleanup service started (interval={self.poll_interval}s)")

    def stop(self):
        """Stop the background cleanup loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info("Cleanup service stopped")

    async def run_cleanup(self) -> CleanupReport:
        """Run a single cleanup cycle. Called by loop and on startup."""
        if self._lock.locked():
            logger.debug("[Cleanup] Cycle already in progress, skipping")
            return self.last_report or CleanupReport()
        async with self._lock:
            return await self._run_cleanup_inner()

    async def _run_cleanup_inner(self) -> CleanupReport:
        """Inner cleanup logic, called under lock.

        Each sweep is an independent strategy method that owns its own
        try/except, so one sweep failing never aborts the cycle (#1026). The
        watchdog runs FIRST (releases resources before stale cleanup marks
        executions failed) and hands `confirmed_running_ids` (#226) to the slot
        sweep so it won't falsely fail executions verified alive.
        """
        report = CleanupReport()

        confirmed_running_ids = await self._sweep_watchdog(report)
        # #1081 Phase 3 (#429/#1402): additive lease-reaper. Runs BEFORE the
        # generic stale/no-session sweeps so a pull-claimed expired-lease row is
        # re-queued (or poison-parked) — preserving execution_id — rather than
        # blanket-FAILED by them; those sweeps then skip it (no longer `running`).
        # Inert until a PULL_MODE_PILOT_AGENTS agent is opted in.
        await self._sweep_expired_leases(report)
        stale_rows = self._sweep_stale_executions(report)
        no_session_rows = self._sweep_no_session_executions(report)
        bulk_swept_rows = (stale_rows or []) + (no_session_rows or [])
        # #1804: close the dispatch activity of every CAS-won bulk-swept row.
        # Runs BEFORE the event emit and is deliberately NOT folded into it —
        # `_emit_bulk_terminal_events` short-circuits when nobody subscribes,
        # which would skip the close on every install with no subscribers.
        report.activities_closed_on_recovery += await self._close_bulk_swept_activities(
            bulk_swept_rows
        )
        # #1714: wake subscribers of bulk-swept executions (parity with the
        # individually-reaped writers). One combined, gated, paced, fail-open emit.
        await self._emit_bulk_terminal_events(
            bulk_swept_rows,
            "marked failed by cleanup watchdog sweep",
        )
        self._sweep_orphaned_skipped(report)
        await self._sweep_stale_slots(report, confirmed_running_ids)
        # #1804: the 120-minute activity backstop runs LAST, after every
        # legitimate closer in this cycle. It used to run one line BEFORE
        # `_sweep_stale_slots` — which closes activities for the executions it
        # reclaims — so within a single cycle the duration fabricator could beat
        # a real closer and permanently record a 15-minute run as a 2-hour one.
        self._sweep_stale_activities(report)
        self._sweep_rate_limit_events(report)
        self._sweep_shared_files(report)
        self._sweep_retention_772(report)
        self._sweep_operator_queue_retention(report)
        self._sweep_agent_reminders_retention(report)
        self._sweep_headroom_history(report)
        await self._sweep_soft_deleted_agents(report)
        await self._sweep_orphan_agent_volumes(report)
        await self._sweep_ephemeral_agents(report)
        self._sweep_soft_deleted_schedules(report)
        self._sweep_idempotency_keys(report)
        await self._sweep_expired_ssh_credentials(report)
        self._maybe_wal_checkpoint(report)

        self._cycle_count += 1

        self.last_run_at = utc_now_iso()
        self.last_report = report

        if report.total > 0:
            logger.info(f"[Cleanup] Cycle complete: {report.to_dict()}")

        return report

    # ------------------------------------------------------------------
    # Sweep strategies (#1026). Each is self-contained: owns its try/except,
    # writes its own CleanupReport field(s), and never raises to the caller.
    # ------------------------------------------------------------------

    async def _sweep_watchdog(self, report: CleanupReport) -> set:
        """0. Reconcile DB vs agent process registries (Issue #129).

        Returns confirmed_running_ids (#226) so the slot sweep won't fail
        executions the watchdog verified as alive. Returns an empty set on
        error so the rest of the cycle proceeds.
        """
        try:
            orphaned, terminated, confirmed_running_ids = (
                await self._reconcile_orphaned_executions(report)
            )
            report.orphaned_executions = orphaned
            report.auto_terminated = terminated
            self.cumulative_orphaned += orphaned
            self.cumulative_auto_terminated += terminated
            if orphaned > 0:
                logger.info(f"[Watchdog] Recovered {orphaned} orphaned executions")
            if terminated > 0:
                logger.info(f"[Watchdog] Auto-terminated {terminated} timed-out executions")
            return confirmed_running_ids
        except Exception as e:
            logger.error(f"[Watchdog] Reconciliation error: {e}")
            return set()

    async def _sweep_expired_leases(self, report: CleanupReport) -> None:
        """0b. Lease reaper for pull-claimed tasks (#1081 Phase 3 — #429/#1402).

        Additive path: recovers a pull worker's expired lease (a `running` row
        with a past `lease_expires_at`) by re-queuing the SAME execution_id under
        the re-delivery cap, or poison-parking it (FAILED + operator-queue item)
        at the cap. Disjoint from the #1083 stale-slot sweep by construction —
        that keys off Redis slot TTL; this keys off the `lease_expires_at` column
        (NULL on every non-pull row). Owns its try/except (never aborts the
        cycle). Inert when no agent is opted in.

        #1085: honor the re-delivery governor's shared-cause pause — during a
        fleet outage the worker's result callback is throttled/held (not lost);
        re-queuing or parking now would race the throttled-then-resumed result.
        Fail-open: a gate error degrades to running the reaper.
        """
        try:
            import config as _config

            if _config.REDELIVERY_GOVERNOR_ENABLED:
                from services.redelivery_governor import get_redelivery_governor

                if get_redelivery_governor().should_hold_reaper():
                    logger.info(
                        "[Cleanup] lease reaper held — re-delivery paused (#1085)"
                    )
                    return
        except Exception as e:  # noqa: BLE001 — never block the sweep on a gate error
            logger.debug("[Cleanup] governor lease-reaper gate skipped: %s", e)

        try:
            from services import lease_reaper_service

            result = lease_reaper_service.reap_expired_leases(db)
            report.expired_leases_requeued = result.requeued
            report.expired_leases_parked = result.parked
            # Close the open dispatch activity for each poison-parked execution
            # (its worker never reported a terminal, so nothing else closes it).
            for execution_id in result.parked_execution_ids:
                await self._close_reaped_activity(execution_id)
            # #1804: same for a re-queued row — the superseded attempt's activity
            # is closed CANCELLED (not FAILED: the attempt was superseded, not
            # failed) so the re-delivery's fresh dispatch activity is the only
            # open one. Without this the agent reads as running two turns.
            for execution_id in result.requeued_execution_ids:
                await self._close_requeued_activity(execution_id)
            if result.requeued or result.parked:
                logger.info(
                    "[Cleanup] lease reaper: re-queued=%d parked=%d",
                    result.requeued, result.parked,
                )
        except Exception as e:
            logger.error(f"[Cleanup] Error reaping expired leases: {e}")

    async def _close_reaped_activity(self, execution_id: str) -> None:
        """Close the open dispatch activity for a poison-parked execution (#429).

        Mirror of ``_close_stale_slot_activity`` (#1083) for the lease-reaper
        park path. #1804: both now delegate to the one owner,
        ``activity_service.close_execution_activity`` (same filtered lookup, plus
        the close CAS so a double close can't clobber a real duration).
        Best-effort — the helper is fail-open.
        """
        from services.activity_service import activity_service

        await activity_service.close_execution_activity(
            execution_id,
            TaskExecutionStatus.FAILED,
            error=f"{_LEASE_EXPIRED_TAG}: pull lease expired, poison-parked (#429)",
        )

    async def _close_requeued_activity(self, execution_id: str) -> None:
        """Close the superseded attempt's dispatch activity on lease re-queue (#1804).

        The re-queue preserves ``execution_id`` by construction (#1402), so the
        re-delivered turn opens a NEW dispatch activity against the same id. The
        dead worker's activity must therefore be closed here, or the execution
        accumulates one permanently-`started` row per re-delivery.

        CANCELLED, not FAILED (Codex 9): the attempt was superseded, not failed —
        the #1332 distinction exists precisely so activity-derived views don't
        collapse the two. Fail-open via the shared helper.
        """
        from services.activity_service import activity_service

        await activity_service.close_execution_activity(
            execution_id,
            TaskExecutionStatus.CANCELLED,
            error=f"{_LEASE_EXPIRED_TAG}: attempt superseded by lease re-delivery (#1402)",
        )

    def _sweep_stale_executions(self, report: CleanupReport) -> list:
        """1. Mark stale executions failed (agent-unreachable safety net).

        #1083 Finding 1: use the per-agent ``timeout + SLOT_TTL_BUFFER`` window
        (same as the slot reaper / E-01) instead of the flat 120-min default, so
        a legitimately-running max-timeout async turn isn't failed ~5 min early.
        ``EXECUTION_STALE_TIMEOUT_MINUTES`` stays the fallback for any agent
        absent from the timeout map (e.g. soft-deleted).

        Returns the CAS-won ``(execution_id, agent_name)`` rows (#1714) — also
        the input to the #1804 bulk activity close. Annotated ``-> list``: it
        has always returned one, the old ``-> None`` was simply wrong.
        """
        try:
            agent_timeouts = db.get_all_execution_timeouts()
            failed_rows: list = []
            count = db.mark_stale_executions_failed(
                EXECUTION_STALE_TIMEOUT_MINUTES,
                agent_timeouts=agent_timeouts,
                buffer_seconds=SLOT_TTL_BUFFER,
                collect_failed=failed_rows,
            )
            report.stale_executions = count
            if count > 0:
                logger.info(f"[Cleanup] Marked {count} stale executions as failed")
            # #1714: emit agent.task.failed for each CAS-won bulk-swept row so a
            # subscribed orchestrator is woken (parity with individually-reaped
            # executions). Gated + paced + fail-open in the helper.
            return failed_rows
        except Exception as e:
            logger.error(f"[Cleanup] Error marking stale executions: {e}")
            return []

    def _sweep_no_session_executions(self, report: CleanupReport) -> list:
        """1b. Fast-fail running executions with no Claude session (Issue #106)."""
        try:
            failed_rows: list = []
            count = db.mark_no_session_executions_failed(
                NO_SESSION_TIMEOUT_SECONDS, collect_failed=failed_rows
            )
            report.no_session_executions = count
            if count > 0:
                logger.info(f"[Cleanup] Marked {count} no-session executions as failed")
            return failed_rows  # #1714: for the completion-event emit
        except Exception as e:
            logger.error(f"[Cleanup] Error marking no-session executions: {e}")
            return []

    async def _emit_bulk_terminal_events(self, rows: list, reason: str) -> None:
        """#1714: emit ``agent.task.failed`` for each CAS-won bulk-swept row.

        Closes the #1578 residual — a bulk watchdog sweep now wakes a subscribed
        orchestrator exactly like an individually-reaped execution does. Properties:

        * **Costs nothing with no subscribers** — one cheap
          ``has_task_terminal_subscribers()`` gate short-circuits the whole loop
          (no per-row ``get_execution``/``find_matching`` when nobody listens).
        * **No thundering herd** — emits are paced in batches of
          ``_BULK_TERMINAL_EMIT_BATCH`` with a short yield between them, so a large
          sweep never fires N dispatch tasks at once. No row is dropped.
        * **All #1578 invariants inherited** — the shared
          ``spawn_task_terminal_event`` → ``emit_task_terminal_event`` helper does
          per-agent matching-subscription gating (no row/dispatch when the specific
          agent has no sub), the reserved ``agent.task.*`` namespace + recursion
          break (suppresses a ``triggered_by="event"`` origin), and is fail-open.
        * **Never affects the terminal write** — the FAILED rows are already
          committed by the sweep; this whole method is best-effort and swallows.
        """
        if not rows:
            return
        try:
            if not db.has_task_terminal_subscribers():
                return  # nobody listening — skip all per-row work
            emitted = 0
            for execution_id, agent_name in rows:
                if not (execution_id and agent_name):
                    continue
                event_dispatch_service.spawn_task_terminal_event(
                    agent_name,
                    execution_id,
                    terminal_status=TaskExecutionStatus.FAILED,
                    summary_or_error=reason,
                )
                emitted += 1
                if emitted % _BULK_TERMINAL_EMIT_BATCH == 0:
                    # Pace the herd: yield so the just-spawned dispatch tasks run
                    # before the next batch is queued.
                    await asyncio.sleep(_BULK_TERMINAL_EMIT_PACE_S)
            if emitted:
                logger.info(
                    "[#1714] Emitted %d bulk-sweep agent.task.failed event(s)", emitted
                )
        except Exception as e:  # fail-open: never affect the sweep's terminal writes
            logger.warning("[#1714] bulk terminal-event emit failed: %s", e)

    async def _close_bulk_swept_activities(self, rows: list) -> int:
        """#1804: close the open dispatch activity for every CAS-won bulk-swept row.

        Batched at the db layer (ONE transaction, no per-row WS) — mirrors what
        ``mark_stale_activities_failed`` already does, and avoids a WebSocket
        herd during exactly the outage-recovery moment #1085 exists to protect.
        Routing N rows through the per-row helper would be N transactions AND N
        broadcasts when the system is least healthy.

        A **sibling** of ``_emit_bulk_terminal_events``, never folded into it:
        that method short-circuits on ``has_task_terminal_subscribers() is
        False``, so folding would skip the close entirely on every install with
        no event subscribers. The #1714 gate scopes the *event*, never the close.

        Takes the ``(execution_id, agent_name)`` rows the sweeps already collect
        via ``collect_failed`` — no new query, no second collection pass.
        Returns the number of activities closed. Fail-open: the FAILED rows are
        already committed; this is best-effort and swallows.
        """
        if not rows:
            return 0
        try:
            execution_ids = [eid for eid, _agent in rows if eid]
            if not execution_ids:
                return 0
            closed = db.close_open_activities_for_executions(
                execution_ids,
                ActivityState.FAILED,
                error="Marked as failed by cleanup: execution swept as stale",
            )
            if closed:
                logger.info(
                    "[#1804] Closed %d dispatch activity(ies) for bulk-swept executions",
                    closed,
                )
            return closed
        except Exception as e:  # fail-open: never affect the sweep's terminal writes
            logger.warning("[#1804] bulk activity close failed: %s", e)
            return 0

    def _sweep_orphaned_skipped(self, report: CleanupReport) -> None:
        """1c. Finalize orphaned skipped executions (Issue #106)."""
        try:
            count = db.finalize_orphaned_skipped_executions()
            report.orphaned_skipped = count
            if count > 0:
                logger.info(f"[Cleanup] Finalized {count} orphaned skipped executions")
        except Exception as e:
            logger.error(f"[Cleanup] Error finalizing orphaned skipped executions: {e}")

    def _sweep_stale_activities(self, report: CleanupReport) -> None:
        """2. Mark stale activities failed."""
        try:
            count = db.mark_stale_activities_failed(ACTIVITY_STALE_TIMEOUT_MINUTES)
            report.stale_activities = count
            if count > 0:
                logger.info(f"[Cleanup] Marked {count} stale activities as failed")
        except Exception as e:
            logger.error(f"[Cleanup] Error marking stale activities: {e}")

    async def _sweep_stale_slots(
        self, report: CleanupReport, confirmed_running_ids: set
    ) -> None:
        """3. Reclaim stale Redis slots + fail their execution records.

        (#219, #226, #378 — see _process_stale_slot_reclaims docstring.)

        #1085: while the re-delivery governor's shared-cause pause is armed, hold
        off this destructive sweep entirely. During a fleet outage an async row's
        callback is being throttled/held (not lost) — failing it to LEASE_EXPIRED
        now would race the throttled-then-resumed callback. The pause TTL (300s)
        stays well under the lease window (timeout + SLOT_TTL_BUFFER), and a late
        SUCCESS still corrects any reaper FAIL via the apply_result CAS — but the
        hold-off avoids the churn. Fail-open: governor degrades to not-paused.
        """
        try:
            import config

            if config.REDELIVERY_GOVERNOR_ENABLED:
                from services.redelivery_governor import get_redelivery_governor

                if get_redelivery_governor().should_hold_reaper():
                    logger.info(
                        "[Cleanup] stale-slot reaper held — re-delivery paused (#1085)"
                    )
                    return
        except Exception as e:  # noqa: BLE001 — never block the sweep on a gate error
            logger.debug("[Cleanup] governor reaper-gate skipped: %s", e)

        try:
            capacity = get_capacity_manager()

            # #226: Query per-agent timeouts from DB so slot cleanup uses the
            # correct TTL instead of a fixed 20-min default.
            agent_timeouts = db.get_all_execution_timeouts()

            reclaimed = await capacity.reclaim_stale(
                agent_timeouts=agent_timeouts
            )
            report.stale_slots = sum(len(ids) for ids in reclaimed.values())

            await self._process_stale_slot_reclaims(
                reclaimed, confirmed_running_ids, report
            )
        except Exception as e:
            logger.error(f"[Cleanup] Error cleaning stale slots: {e}")

    def _sweep_rate_limit_events(self, report: CleanupReport) -> None:
        """4. Hourly maintenance: prune rate-limit events past their window.

        Runs every 12th cycle (60 min at 5-min interval) plus the first cycle
        after startup so we don't wait an hour on boot. The gate reads
        `_cycle_count` before the orchestrator increments it.

        ent#433: the window used to be a hardcoded 24 hours inside the db
        accessor — this table held the platform's only durable record of real
        agent work hitting a provider rate limit (timestamped, attributed to
        the causing agent) and destroyed it daily, with no operator-visible
        window, no #1644 blast-radius guard, and no `GET /api/settings/retention`
        entry, while every sibling table had all three. It is now a real
        retention window like the others, and goes through the same guard.
        """
        if self._cycle_count % 12 != 0:
            return
        days = _read_retention_setting("subscription_failure_event_retention_days")
        if days <= 0:
            return
        try:
            if _guard_allows(
                "subscription_failure_event_retention_days",
                "subscription_rate_limit_events", days,
                lambda limit: db.count_rate_limit_event_candidates(days, limit),
            ):
                pruned = db.cleanup_old_rate_limit_events(
                    retention_days=days,
                    chunk_size=RETENTION_CHUNK_SIZE_PER_CYCLE,
                )
                report.rate_limit_events_pruned = pruned
                _after_guarded_prune("subscription_failure_event_retention_days")
                if pruned > 0:
                    _log_prune(
                        pruned,
                        f"[Cleanup] Pruned {pruned} subscription rate-limit events "
                        f"older than {days} days (ent#433)",
                    )
        except Exception as e:
            logger.error(f"[Cleanup] Error pruning rate-limit events: {e}")

    def _sweep_headroom_history(self, report: CleanupReport) -> None:
        """Prune subscription headroom probe history past its window (ent#433).

        Steady state is a trickle: #471's own floors bound probing to <=1
        ambient probe per 15 minutes per subscription (demand-driven — an
        unwatched instance probes nothing), so a 5-minute cycle's candidate set
        is single digits, three orders of magnitude under the guard threshold.
        The guard therefore only ever fires if an operator NARROWS the window,
        which is exactly the #1644 case it exists for — do not "optimize" it
        away on the grounds that it never trips.
        """
        days = _read_retention_setting("subscription_headroom_retention_days")
        if days <= 0:
            return
        try:
            if _guard_allows(
                "subscription_headroom_retention_days",
                "subscription_headroom_history", days,
                lambda limit: db.count_headroom_history_candidates(days, limit),
            ):
                pruned = db.prune_headroom_history(
                    retention_days=days,
                    chunk_size=RETENTION_CHUNK_SIZE_PER_CYCLE,
                )
                report.headroom_history_pruned = pruned
                _after_guarded_prune("subscription_headroom_retention_days")
                if pruned > 0:
                    _log_prune(
                        pruned,
                        f"[Cleanup] Deleted {pruned} subscription_headroom_history "
                        f"rows older than {days} days (ent#433)",
                    )
        except Exception as e:
            logger.error(f"[Cleanup] Error pruning headroom history: {e}")

    def _sweep_shared_files(self, report: CleanupReport) -> None:
        """4b. Purge expired / old-revoked shared files (C4 / FILES-001).

        Every cycle — the set is usually small and both DB row + disk unlink
        are cheap. Grace period for revoked rows keeps them queryable for a day
        post-revoke (incident diagnosis).
        """
        try:
            from pathlib import Path
            stored_filenames = db.delete_expired_and_revoked_shared_files(
                revoke_grace_hours=24
            )
            if stored_filenames:
                storage_root = Path("/data/agent-files")
                unlinked = 0
                for sf in stored_filenames:
                    try:
                        p = storage_root / sf
                        if p.exists():
                            p.unlink()
                            unlinked += 1
                    except Exception as e:
                        logger.warning(f"[Cleanup] failed to unlink {sf}: {e}")
                report.shared_files_purged = len(stored_filenames)
                logger.info(
                    f"[Cleanup] Purged {len(stored_filenames)} shared-file "
                    f"rows ({unlinked} files unlinked from /data/agent-files/)"
                )
        except Exception as e:
            logger.error(f"[Cleanup] Error purging shared files: {e}")

    def _sweep_retention_772(self, report: CleanupReport) -> None:
        """4c. Issue #772: retention pruning for execution_log, execution rows,
        and agent_health_checks. All three obey the configurable retention
        window from ops settings; "0" disables the corresponding sweep.
        Per-cycle budget caps each sweep so the first post-deploy backfill
        spans multiple cycles instead of holding the write lock end-to-end.
        """
        try:
            log_days, row_days, hc_days, reports_days = _read_retention_settings()
        except Exception as e:
            logger.error(f"[Cleanup] Error reading retention settings: {e}")
            log_days = row_days = hc_days = reports_days = 0

        if log_days > 0:
            try:
                if _guard_allows(
                    "execution_log_retention_days", "execution_log", log_days,
                    lambda limit: db.count_execution_log_candidates(log_days, limit),
                ):
                    pruned = db.prune_execution_logs(
                        retention_days=log_days,
                        chunk_size=RETENTION_CHUNK_SIZE_PER_CYCLE,
                    )
                    report.execution_logs_pruned = pruned
                    _after_guarded_prune("execution_log_retention_days")
                    if pruned > 0:
                        _log_prune(
                            pruned,
                            f"[Cleanup] Nulled execution_log on {pruned} executions "
                            f"older than {log_days} days (#772)",
                        )
            except Exception as e:
                logger.error(f"[Cleanup] Error pruning execution_log: {e}")

        if row_days > 0:
            try:
                if _guard_allows(
                    "execution_row_retention_days", "schedule_executions rows",
                    row_days,
                    lambda limit: db.count_execution_row_candidates(row_days, limit),
                ):
                    pruned = db.prune_execution_rows(
                        retention_days=row_days,
                        chunk_size=RETENTION_CHUNK_SIZE_PER_CYCLE,
                    )
                    report.execution_rows_pruned = pruned
                    _after_guarded_prune("execution_row_retention_days")
                    if pruned > 0:
                        _log_prune(
                            pruned,
                            f"[Cleanup] Deleted {pruned} schedule_executions rows "
                            f"older than {row_days} days (#772)",
                        )
            except Exception as e:
                logger.error(f"[Cleanup] Error pruning execution rows: {e}")

        if hc_days > 0:
            try:
                if _guard_allows(
                    "health_check_retention_days", "agent_health_checks", hc_days,
                    lambda limit: db.count_health_check_candidates(hc_days, limit),
                ):
                    pruned = db.cleanup_old_health_records(
                        days=hc_days,
                        chunk_size=RETENTION_CHUNK_SIZE_PER_CYCLE,
                    )
                    report.health_checks_pruned = pruned
                    _after_guarded_prune("health_check_retention_days")
                    if pruned > 0:
                        _log_prune(
                            pruned,
                            f"[Cleanup] Deleted {pruned} agent_health_checks rows "
                            f"older than {hc_days} days (#772)",
                        )
            except Exception as e:
                logger.error(f"[Cleanup] Error pruning health checks: {e}")

        if reports_days > 0:
            try:
                if _guard_allows(
                    "agent_reports_retention_days", "agent_reports", reports_days,
                    lambda limit: db.count_agent_reports_candidates(
                        reports_days, limit
                    ),
                ):
                    pruned = db.prune_agent_reports(
                        retention_days=reports_days,
                        chunk_size=RETENTION_CHUNK_SIZE_PER_CYCLE,
                    )
                    report.agent_reports_pruned = pruned
                    _after_guarded_prune("agent_reports_retention_days")
                    if pruned > 0:
                        logger.info(
                            f"[Cleanup] Deleted {pruned} agent_reports rows "
                            f"older than {reports_days} days (#918)"
                        )
            except Exception as e:
                logger.error(f"[Cleanup] Error pruning agent_reports: {e}")

        # #1449: scrub stale drain-replay PII (user_message/user_email/
        # system_prompt) from backlog_metadata on authoritative-terminal rows.
        # NOT gated on a retention window — it is a security invariant, not an
        # operator knob (a fixed default avoids the #1638 floor-by-seed trap).
        # Count-only logging — the blob carries PII and must never be logged.
        try:
            scrubbed = db.scrub_terminal_backlog_metadata(
                chunk_size=RETENTION_CHUNK_SIZE_PER_CYCLE,
            )
            report.backlog_metadata_scrubbed = scrubbed
            if scrubbed > 0:
                _log_prune(
                    scrubbed,
                    f"[Cleanup] Scrubbed backlog_metadata on {scrubbed} "
                    f"terminal executions (#1449)",
                )
        except Exception as e:
            logger.error(f"[Cleanup] Error scrubbing backlog_metadata: {e}")

        # #1741: convert legacy raw-transcript `tool_calls` blobs to the summary
        # shape. The writer is fixed going forward, but existing rows would keep
        # reporting 0 tool calls and rendering an empty panel until they aged
        # out. Non-destructive (the transcript stays in `execution_log`) and
        # idempotent, so it simply finds nothing once history is converted.
        try:
            converted = db.resummarize_legacy_tool_calls()
            report.legacy_tool_calls_converted = converted
            if converted > 0:
                _log_prune(
                    converted,
                    f"[Cleanup] Converted legacy tool_calls on {converted} "
                    f"executions to the summary shape (#1741)",
                )
        except Exception as e:
            logger.error(f"[Cleanup] Error converting legacy tool_calls: {e}")

        # #1745: deactivate per-agent MCP keys whose agent is no longer live.
        # The delete/recover paths keep this in sync going forward; this catches
        # keys orphaned BEFORE the fix, and any that slip through a path that
        # removes an agent without going through delete_agent_ownership. Not
        # age-gated — an orphaned credential is a security invariant, not a
        # retention window (the #1449 reasoning).
        try:
            revoked = db.deactivate_orphaned_agent_keys()
            report.orphaned_agent_keys_revoked = revoked
            if revoked > 0:
                _log_prune(
                    revoked,
                    f"[Cleanup] Deactivated {revoked} MCP key(s) belonging to "
                    f"agents that are no longer live (#1745)",
                )
        except Exception as e:
            logger.error(f"[Cleanup] Error deactivating orphaned agent keys: {e}")

    def _sweep_operator_queue_retention(self, report: CleanupReport) -> None:
        """4c-quinquies. Issue #1142: delete terminal operator_queue rows past
        their retention window. `operator_queue` was the one #772-adjacent table
        with no automatic sweep — terminal rows (acknowledged/cancelled/expired)
        accumulated forever; #1017's Clear All only *hid* them. `responded` rows
        are protected by a more generous floor (they still carry an undelivered
        operator answer). `0` disables; capped per cycle like the other sweeps.
        """
        try:
            from services.settings_service import OPS_SETTINGS_DEFAULTS
            raw = db.get_setting_value(
                "operator_queue_retention_days",
                OPS_SETTINGS_DEFAULTS.get("operator_queue_retention_days", "90"),
            )
            try:
                days = max(int(raw), 0)
            except (TypeError, ValueError):
                days = 0
        except Exception as e:
            logger.error(f"[Cleanup] Error reading operator_queue retention setting: {e}")
            days = 0

        if days <= 0:
            return
        try:
            if _guard_allows(
                "operator_queue_retention_days", "operator_queue", days,
                lambda limit: db.count_operator_queue_terminal_candidates(
                    days, OPERATOR_QUEUE_RESPONDED_MIN_RETENTION_DAYS, limit
                ),
            ):
                pruned = db.prune_operator_queue_terminal_items(
                    retention_days=days,
                    responded_retention_days=OPERATOR_QUEUE_RESPONDED_MIN_RETENTION_DAYS,
                    limit=RETENTION_CHUNK_SIZE_PER_CYCLE,
                )
                report.operator_queue_pruned = pruned
                _after_guarded_prune("operator_queue_retention_days")
                if pruned > 0:
                    logger.info(
                        f"[Cleanup] Deleted {pruned} terminal operator_queue rows "
                        f"older than their retention window (#1142)"
                    )
        except Exception as e:
            logger.error(f"[Cleanup] Error pruning operator_queue: {e}")

    def _sweep_agent_reminders_retention(self, report: CleanupReport) -> None:
        """4c-sexies. Issue #1296: delete TERMINAL agent_reminders rows
        (fired/cancelled/failed) past their retention window. The AGENT_REFS
        CASCADE only cleans on agent delete; terminal rows on a LIVE agent
        accumulate. `pending`/`firing` are never deleted. `0` disables; gated
        through the #1644 blast-radius guard like every other window sweep.
        """
        try:
            from services.settings_service import OPS_SETTINGS_DEFAULTS
            raw = db.get_setting_value(
                "agent_reminders_retention_days",
                OPS_SETTINGS_DEFAULTS.get("agent_reminders_retention_days", "90"),
            )
            try:
                days = max(int(raw), 0)
            except (TypeError, ValueError):
                days = 0
        except Exception as e:
            logger.error(f"[Cleanup] Error reading agent_reminders retention setting: {e}")
            days = 0

        if days <= 0:
            return
        try:
            if _guard_allows(
                "agent_reminders_retention_days", "agent_reminders", days,
                lambda limit: db.count_agent_reminders_candidates(days, limit),
            ):
                pruned = db.prune_agent_reminders(
                    retention_days=days,
                    chunk_size=RETENTION_CHUNK_SIZE_PER_CYCLE,
                )
                report.agent_reminders_pruned = pruned
                _after_guarded_prune("agent_reminders_retention_days")
                if pruned > 0:
                    logger.info(
                        f"[Cleanup] Deleted {pruned} terminal agent_reminders rows "
                        f"older than {days} days (#1296)"
                    )
        except Exception as e:
            logger.error(f"[Cleanup] Error pruning agent_reminders: {e}")

    async def _sweep_soft_deleted_agents(self, report: CleanupReport) -> None:
        """4c-bis. Issue #834 Phase 1a: hard-purge soft-deleted agents past
        their retention window. `purge_agent_ownership` runs the #816
        cascade_delete primitive so every per-agent child row goes with the
        parent in a single transaction. Bounded by the same 5000-row/cycle cap
        as the other sweeps so a backlog after a long-disabled retention
        setting drains gradually.

        Purge is also the moment an agent name becomes *reusable* — up to here
        `is_agent_name_reserved` still matches the soft-deleted row and blocks
        creation. So each purged name gets its per-agent Redis state swept too
        (#1560): the container is long gone, and anything left keyed to the name
        would be inherited by whatever agent claims it next.
        """
        try:
            from services.settings_service import OPS_SETTINGS_DEFAULTS

            # Subscript, not `.get(key, "180")` (#1638): the literal fallback was
            # unreachable and advertised a default the dict no longer held — a
            # reader trap in a destructive path. A missing key now raises into
            # the handler below and disables the sweep (fail-safe).
            raw_sd_days = db.get_setting_value(
                "agent_soft_delete_retention_days",
                OPS_SETTINGS_DEFAULTS["agent_soft_delete_retention_days"],
            )
            try:
                sd_days = max(int(raw_sd_days), 0)
            except (TypeError, ValueError):
                sd_days = 0
        except Exception as e:
            logger.error(f"[Cleanup] Error reading agent retention setting: {e}")
            sd_days = 0

        if sd_days > 0:
            try:
                # #1644: floor 0 — every candidate here destroys an agent's Docker
                # volumes (#1581) and is unrecoverable, so ANY purge is worth one
                # acknowledgement. A row-scale threshold would wave this through:
                # 3 agents is ~0% of any table but 3 destroyed volume sets, which
                # is precisely the inversion that killed the percentage design.
                from services.retention_guard import FLOOR_AGENTS

                if not _guard_allows(
                    "agent_soft_delete_retention_days", "soft-deleted agents",
                    sd_days,
                    lambda limit: db.count_soft_deleted_agents_past_retention(
                        sd_days, limit
                    ),
                    floor=FLOOR_AGENTS,
                ):
                    return

                names = db.find_soft_deleted_agents_past_retention(
                    retention_days=sd_days,
                    limit=RETENTION_CHUNK_SIZE_PER_CYCLE,
                )
                from services.agent_runtime_state import clear_agent_runtime_state
                from services.docker_utils import remove_agent_volumes

                purged = 0
                volumes_removed = 0
                for name in names:
                    try:
                        # #1664: read the volume base BEFORE the purge deletes
                        # the row that holds it — a renamed agent's volumes are
                        # named after its former self, and after the purge
                        # nothing remembers that. Falls back to the agent name
                        # (the never-renamed case, and the pre-#1664 default).
                        try:
                            volume_base = db.get_volume_base_name(name) or name
                        except Exception:
                            volume_base = name
                        if db.purge_agent_ownership(name):
                            purged += 1
                            # #1560: the name is reusable from this instant on.
                            await clear_agent_runtime_state(name)
                            # #1581: the ownership row is gone (agent is now
                            # unrecoverable) — the ONLY safe moment to drop the
                            # data volumes. Guarded + tolerant; a residual
                            # (in-use) volume retries via the orphan sweep, so a
                            # failure here never blocks the purge.
                            try:
                                # Both identities: a renamed agent's workspace
                                # kept the old base, but any volume created
                                # after the rename (public/shared — those name
                                # off the LIVE name) is under the new one. The
                                # set is deduped, so an un-renamed agent is one
                                # call as before (#1664).
                                for base in dict.fromkeys((volume_base, name)):
                                    # `volume_base_name` carries no unique
                                    # constraint, and installs predating the
                                    # create-time gate (crud.py, #1664) can hold
                                    # a collision: agent `new` pinned to base
                                    # `old` PLUS a live agent literally named
                                    # `old`, both on the same volumes. This row
                                    # is already purged, so a still-True answer
                                    # means a DIFFERENT row claims the base —
                                    # its data is live and not ours to drop.
                                    if db.is_volume_base_reserved(base):
                                        logger.warning(
                                            "[#1664] purge of %s: volume base %r "
                                            "still claimed by another agent — "
                                            "skipping removal",
                                            name,
                                            base,
                                        )
                                        continue
                                    volumes_removed += await remove_agent_volumes(
                                        base
                                    )
                            except Exception as e:
                                logger.warning(
                                    f"[#1581] volume removal after purge of {name} "
                                    f"failed (orphan sweep will retry): {e}"
                                )
                    except Exception as e:
                        logger.warning(
                            f"[Cleanup] Failed to purge soft-deleted agent {name}: {e}"
                        )
                report.soft_deleted_agents_purged = purged
                report.agent_volumes_removed = volumes_removed
                _after_guarded_prune("agent_soft_delete_retention_days")
                if purged > 0:
                    # Always WARNING (#1638): unlike a row prune, this destroys
                    # the agent's data volumes (#1581) and is unrecoverable —
                    # there is no such thing as a routine, unremarkable one.
                    logger.warning(
                        f"[Cleanup] Hard-purged {purged} soft-deleted agent(s) "
                        f"past {sd_days}-day retention, removed {volumes_removed} "
                        f"data volume(s) — unrecoverable (#834/#1581)"
                    )
            except Exception as e:
                logger.error(f"[Cleanup] Error pruning soft-deleted agents: {e}")

    async def _sweep_orphan_agent_volumes(self, report: CleanupReport) -> None:
        """4c-bis-2. Issue #1581: reclaim orphaned agent data volumes.

        Docker-as-truth backstop for the purge-time removal: an
        `agent-{base}-{workspace|public|shared}` volume that no ownership row
        claims is reclaimable. Bounded per cycle, plus three independent
        conditions that must ALL hold before anything is destroyed — this sweep
        force-removes durable user data (#1169 `data_paths`), so it fails safe
        on every ambiguity (#1638 principle):

        1. **No owner** — `db.is_volume_base_reserved` (NOT
           `is_agent_name_reserved`, #1664): a renamed agent keeps its
           pre-rename volumes, so the volume's self-declared identity (name +
           immutable `trinity.agent-name` label) names an agent that no longer
           exists while the data is live. Asking the ownership rows which
           volume bases they own answers the real question. Soft-deleted rows
           still match, so the recovery window is respected.
        2. **Unattached** — no container mounts it. The rename case again:
           belt-and-braces over (1) via Docker itself (Invariant #11). When
           attachment can't be established the whole sweep is skipped rather
           than run blind.
        3. **Unattached for `ORPHAN_VOLUME_UNATTACHED_STRIKES` consecutive
           cycles** — closes the recreate race: `recreate_container_with_
           updated_config` removes the old container before creating the new
           one, leaving a live agent's volume momentarily unattached. A single
           unattached observation is therefore not evidence of an orphan; a
           streak spanning ~15 min is. Any attached (or re-owned) observation
           resets the streak.

        Removal still goes through the name+label guard (`remove_agent_volumes`)
        as a last line of defence. Also carries a creation-grace window
        (creation writes the volume before the ownership row).
        """
        try:
            from services.docker_utils import (
                list_agent_data_volumes,
                list_attached_volume_names,
                remove_agent_volumes,
            )

            volumes = await list_agent_data_volumes()
            if not volumes:
                self._unattached_volume_strikes.clear()
                return

            # Fail-closed: "which volumes are in use?" is unanswerable → do not
            # reclaim this cycle. Deleting a live agent's home volume is
            # unrecoverable; waiting 5 minutes costs nothing.
            attached = await list_attached_volume_names()
            if attached is None:
                logger.warning(
                    "[#1581] skipping orphan-volume sweep: container mounts "
                    "unavailable (cannot prove a volume is unused)"
                )
                return

            now = utc_now()
            reclaimed = 0
            # Group orphan volumes by agent so one remove_agent_volumes call
            # drops all three (workspace/public/shared) of a dead agent.
            orphan_agents: set = set()
            seen_volumes: set = set()
            for volume in volumes:
                try:
                    volume_name = getattr(volume, "name", None)
                    if not volume_name:
                        continue
                    seen_volumes.add(volume_name)
                    labels = (volume.attrs.get("Labels") or {}) if volume.attrs else {}
                    volume_base = labels.get("trinity.agent-name")
                    if not volume_base:
                        continue
                    # (1) Some agent — live or soft-deleted — owns this base.
                    # Covers the renamed agent whose volumes still carry the
                    # old base (#1664).
                    if db.is_volume_base_reserved(volume_base):
                        self._unattached_volume_strikes.pop(volume_name, None)
                        continue
                    # (2) In use ⇒ someone's live data, whatever it calls
                    # itself. Reset the streak: the next unattached sighting
                    # starts counting from scratch.
                    if volume_name in attached:
                        self._unattached_volume_strikes.pop(volume_name, None)
                        continue
                    # Grace: skip a volume younger than the window (its ownership
                    # row may still be mid-write during a slow create).
                    created_raw = volume.attrs.get("CreatedAt") if volume.attrs else None
                    age_s = self._volume_age_seconds(created_raw, now)
                    if age_s is None or age_s < ORPHAN_VOLUME_GRACE_SECONDS:
                        # Unparseable timestamp → treat conservatively as young
                        # (never reclaim on ambiguity — the purge path is the
                        # primary reclaim; this is only a backstop).
                        continue
                    # (3) Require a streak of unattached cycles.
                    strikes = self._unattached_volume_strikes.get(volume_name, 0) + 1
                    self._unattached_volume_strikes[volume_name] = strikes
                    if strikes < ORPHAN_VOLUME_UNATTACHED_STRIKES:
                        continue
                    if volume_base in orphan_agents:
                        continue
                    if len(orphan_agents) >= ORPHAN_VOLUME_RECLAIM_PER_CYCLE:
                        continue
                    orphan_agents.add(volume_base)
                except Exception as e:
                    logger.warning(f"[#1581] orphan-volume triage failed: {e}")

            # Drop strike state for volumes that no longer exist so the map
            # can't grow without bound across cycles.
            for stale in set(self._unattached_volume_strikes) - seen_volumes:
                self._unattached_volume_strikes.pop(stale, None)

            for volume_base in orphan_agents:
                try:
                    reclaimed += await remove_agent_volumes(volume_base)
                except Exception as e:
                    logger.warning(
                        f"[#1581] orphan volume reclaim for {volume_base} failed: {e}"
                    )

            report.orphan_agent_volumes_reclaimed = reclaimed
            if reclaimed > 0:
                logger.info(
                    f"[#1581] reclaimed {reclaimed} orphaned agent volume(s)"
                )
        except Exception as e:
            logger.error(f"[Cleanup] Error in orphan agent-volume sweep: {e}")

    @staticmethod
    def _volume_age_seconds(created_raw: Optional[str], now) -> Optional[float]:
        """Age in seconds of a Docker volume from its ``CreatedAt`` string.

        Docker emits RFC3339 (often with sub-second precision and a ``Z``
        suffix, sometimes with a numeric offset). Returns None when absent or
        unparseable — the caller treats None conservatively (does not reclaim).
        """
        if not created_raw:
            return None
        try:
            # Docker may emit RFC3339Nano (up to 9 fractional digits); Python's
            # fromisoformat only accepts 3 or 6 — clamp to microseconds.
            normalized = re.sub(
                r"(\.\d{6})\d+(?=[Z+\-]|$)", r"\1", created_raw
            )
            created_dt = parse_iso_timestamp(normalized)
            if created_dt is None:
                return None
            if created_dt.tzinfo is None:
                from datetime import timezone as _tz
                created_dt = created_dt.replace(tzinfo=_tz.utc)
            return (now - created_dt).total_seconds()
        except Exception:
            return None

    async def _sweep_ephemeral_agents(self, report: CleanupReport) -> None:
        """4c-quater. trinity-enterprise#69: ephemeral "ghost" agent GC.

        Two passes, both bounded per cycle so a burst of expired ghosts can't
        stall the loop's other sweeps:

        1. **DB pass** — ghosts past their TTL or over their exec budget →
           ``discard_ephemeral_agent`` (per-discard timeout; the primitive is
           idempotent and crash-convergent, so a timed-out discard resumes
           next cycle via its own intent marker).
        2. **Docker-as-truth orphan pass** — containers labeled
           ``trinity.ephemeral=true`` with NO ownership row (backend restarted
           mid-create or mid-discard) → removed, with a newborn grace window
           (creation writes the ownership row LAST; a sweep landing in that
           gap must not reap a healthy newborn).

        Interim-until-#429: this reclaim folds into the consolidated lease
        reaper when the cleanup pyramid lands.
        """
        # --- pass 1: DB-driven discard ---
        try:
            from services.agent_service.ephemeral import discard_ephemeral_agent

            names = db.find_discardable_ephemeral_agents(
                limit=EPHEMERAL_DISCARDS_PER_CYCLE
            )
            discarded = 0
            for name in names:
                try:
                    if await asyncio.wait_for(
                        discard_ephemeral_agent(name, reason="gc_sweep"),
                        timeout=EPHEMERAL_DISCARD_TIMEOUT_S,
                    ):
                        discarded += 1
                except asyncio.TimeoutError:
                    logger.warning(
                        f"[Cleanup] Ephemeral discard of {name} timed out — "
                        f"will resume next cycle (intent marker is durable)"
                    )
                except Exception as e:
                    logger.warning(
                        f"[Cleanup] Failed to discard ephemeral agent {name}: {e}"
                    )
            report.ephemeral_agents_discarded = discarded
            if discarded > 0:
                logger.info(
                    f"[Cleanup] Discarded {discarded} expired/exhausted ephemeral agent(s)"
                )
        except Exception as e:
            logger.error(f"[Cleanup] Error in ephemeral DB sweep: {e}")

        # --- pass 2: Docker-as-truth orphan reclaim ---
        try:
            from services.docker_service import list_ephemeral_agent_containers
            from services.docker_utils import container_remove
            from services.agent_runtime_state import clear_agent_runtime_state

            reclaimed = 0
            now = datetime.now(timezone.utc)
            for container in list_ephemeral_agent_containers():
                try:
                    labels = getattr(container, "labels", {}) or {}
                    name = labels.get("trinity.agent-name")
                    if not name:
                        continue
                    # Row still present (live OR mid-discard) → pass 1 owns it.
                    if db.is_agent_name_reserved(name):
                        continue
                    # Newborn grace: creation writes the ownership row LAST —
                    # skip containers younger than the grace window.
                    created_raw = labels.get("trinity.created")
                    if created_raw:
                        try:
                            created_dt = parse_iso_timestamp(created_raw)
                            if created_dt.tzinfo is None:
                                created_dt = created_dt.replace(tzinfo=timezone.utc)
                            age_s = (now - created_dt).total_seconds()
                        except Exception:
                            age_s = None
                        if age_s is not None and age_s < EPHEMERAL_ORPHAN_GRACE_S:
                            continue
                    await container_remove(container, force=True)
                    await clear_agent_runtime_state(name)
                    reclaimed += 1
                    logger.info(
                        f"[Cleanup] Reclaimed orphaned ephemeral container for {name}"
                    )
                except Exception as e:
                    logger.warning(
                        f"[Cleanup] Failed to reclaim orphaned ephemeral container: {e}"
                    )
            report.ephemeral_orphans_reclaimed = reclaimed
        except Exception as e:
            logger.error(f"[Cleanup] Error in ephemeral orphan sweep: {e}")

    def _sweep_soft_deleted_schedules(self, report: CleanupReport) -> None:
        """4c-ter. Issue #834 Phase 1b: hard-purge soft-deleted schedules past
        their retention window. Unlike the agent purge, this does not chain
        into cascade_delete — schedules don't have child rows registered with
        #816 (schedule_executions are KEEP-policy via subscription_id rollups,
        and the per-schedule cleanup of executions belongs to #772's separate
        sweep).
        """
        try:
            from services.settings_service import OPS_SETTINGS_DEFAULTS

            raw_schedule_days = db.get_setting_value(
                "schedule_soft_delete_retention_days",
                OPS_SETTINGS_DEFAULTS["schedule_soft_delete_retention_days"],
            )
            try:
                schedule_days = max(int(raw_schedule_days), 0)
            except (TypeError, ValueError):
                schedule_days = 0
        except Exception as e:
            logger.error(f"[Cleanup] Error reading schedule retention setting: {e}")
            schedule_days = 0

        if schedule_days > 0:
            try:
                from services.retention_guard import FLOOR_SCHEDULES

                if not _guard_allows(
                    "schedule_soft_delete_retention_days", "soft-deleted schedules",
                    schedule_days,
                    lambda limit: db.count_soft_deleted_schedules_past_retention(
                        schedule_days, limit
                    ),
                    floor=FLOOR_SCHEDULES,
                ):
                    return

                ids = db.find_soft_deleted_schedules_past_retention(
                    retention_days=schedule_days,
                    limit=RETENTION_CHUNK_SIZE_PER_CYCLE,
                )
                purged = 0
                for sid in ids:
                    try:
                        if db.purge_schedule(sid):
                            purged += 1
                    except Exception as e:
                        logger.warning(
                            f"[Cleanup] Failed to purge soft-deleted schedule {sid}: {e}"
                        )
                report.soft_deleted_schedules_purged = purged
                _after_guarded_prune("schedule_soft_delete_retention_days")
                if purged > 0:
                    logger.info(
                        f"[Cleanup] Hard-purged {purged} soft-deleted schedule(s) "
                        f"past {schedule_days}-day retention (#834 Phase 1b)"
                    )
            except Exception as e:
                logger.error(f"[Cleanup] Error pruning soft-deleted schedules: {e}")

    def _sweep_idempotency_keys(self, report: CleanupReport) -> None:
        """4c-quater. RELIABILITY-006 (#525): purge idempotency keys past their
        24h TTL. Fixed window (not an ops setting) — the contract guarantees
        dedup for 24h, no longer. Cheap point-delete on the created_at index.
        """
        try:
            purged = db.idempotency_purge_expired(ttl_hours=24)
            report.idempotency_keys_purged = purged
            if purged > 0:
                logger.info(
                    f"[Cleanup] Purged {purged} idempotency key(s) past 24h TTL (#525)"
                )
        except Exception as e:
            logger.error(f"[Cleanup] Error purging idempotency keys: {e}")

    async def _sweep_expired_ssh_credentials(self, report: CleanupReport) -> None:
        """4c-sexies. Issue #1616: remove near-expired ephemeral SSH keys from
        agent ``authorized_keys``.

        The security gap this closes: an ephemeral SSH key's TTL was enforced
        ONLY on its Redis metadata. `SshService.cleanup_expired_credentials()`
        — the code that removes the actual line from the file `sshd` reads —
        existed but had ZERO callers (`SSH_ACCESS_CLEANUP_INTERVAL` was unused),
        so on a preserved (never-recreated) volume an expired key lingered in
        `authorized_keys` and still granted login after its stated expiry. For a
        key-auth credential the Redis TTL revokes nothing — only this file-side
        removal does — so wiring the sweep is what actually enforces the TTL.

        The sweep removes a key's line once its stored `expires_at` has passed;
        `store_credential_metadata` keeps the Redis row alive
        `SSH_ACCESS_CLEANUP_GRACE_SECONDS` past that deadline so every expired key
        is observed by at least one 5-min cycle before Redis forgets it (the
        earlier `ttl in [0,60]` heuristic missed ~80% of keys across the cadence).

        Best-effort and self-contained (#1026): owns its try/except, never
        raises to the cycle. `cleanup_expired_credentials` is itself fail-open
        per key (a Redis or `docker exec` error on one key is logged and
        skipped), and `remove_ssh_key` tolerates a missing container/file — so a
        stopped or deleted agent is a no-op rather than an error.
        """
        try:
            from services.ssh_service import get_ssh_service

            cleaned = await get_ssh_service().cleanup_expired_credentials()
            report.ssh_credentials_expired = cleaned
            if cleaned > 0:
                logger.info(
                    f"[Cleanup] Removed {cleaned} expired SSH key(s) from agent "
                    f"authorized_keys (#1616)"
                )
        except Exception as e:
            logger.error(f"[Cleanup] Error sweeping expired SSH credentials: {e}")

    def _maybe_wal_checkpoint(self, report: CleanupReport) -> None:
        """4d. Issue #772: after a retention sweep reclaims meaningful space,
        truncate the WAL so the OS sees the free pages. Checkpoint is cheap and
        safe to run per-cycle when there's work to do; full VACUUM is gated to a
        daily off-peak job (see start()).
        """
        retention_total = (report.execution_logs_pruned
                           + report.execution_rows_pruned
                           + report.backlog_metadata_scrubbed  # #1449
                           + report.health_checks_pruned
                           + report.soft_deleted_agents_purged
                           + report.soft_deleted_schedules_purged
                           + report.idempotency_keys_purged
                           + report.agent_reports_pruned
                           + report.operator_queue_pruned  # #1142
                           + report.agent_reminders_pruned  # #1296 — was omitted
                           + report.headroom_history_pruned      # ent#433
                           + report.rate_limit_events_pruned)    # ent#433
        if retention_total > 0:
            try:
                _wal_checkpoint_truncate()
            except Exception as e:
                logger.warning(f"[Cleanup] WAL checkpoint failed: {e}")

    async def _close_stale_slot_activity(self, execution_id: str) -> None:
        """Close the open dispatch activity for a reclaimed-slot execution (#1083).

        Under fire-and-forget dispatch the ``execute_task`` coroutine returns at
        the 202 ACK, so its ``finally`` never runs to complete the activity. When
        the lease reaper FAILs the row we close the activity here instead.
        #1804: delegates to ``activity_service.close_execution_activity`` — the
        same filtered lookup (a shared-eid tool_call row is never cross-closed,
        Codex #8) plus the close CAS. Best-effort: the helper is fail-open, so a
        failure here never blocks the reaper.
        """
        from services.activity_service import activity_service

        await activity_service.close_execution_activity(
            execution_id,
            TaskExecutionStatus.FAILED,
            error=f"{_LEASE_EXPIRED_TAG}: slot lease expired (no result callback)",
        )

    async def _process_stale_slot_reclaims(
        self,
        reclaimed: Dict[str, List[str]],
        confirmed_running_ids: set,
        report: CleanupReport,
    ) -> None:
        """Fail execution records whose slots were reclaimed, with just-in-time
        re-verify to prevent phantom failures (#378).

        The bug: cleanup service's Phase 3 sometimes marked executions FAILED
        with "Stale execution — slot TTL expired" even though the task was
        still running (agent had just dropped it from its registry after
        completion, so Phase 0's batch query missed it). The SUCCESS response
        then arrived after Phase 3 already wrote FAILED — user saw the flip.

        The fix:
        - Do a just-in-time re-verify with each agent RIGHT BEFORE writing
          FAILED, closing the window between Phase 0 and Phase 3.
        - Parallel fan-out via asyncio.gather (mirrors Phase 0 pattern at
          _reconcile_orphaned_executions).
        - On agent unreachable (#497): force-fail via the race-guarded
          `fail_stale_slot_execution`. The slot was reclaimed by TTL, so
          the execution is by construction older than `timeout + buffer`.
          Waiting for the 120-min Phase 1 stale cleanup was leaving DB
          rows as zombie `running` for up to 2 hours under sustained
          partial-outage conditions. The race guard preserves any
          SUCCESS that arrived between slot reclaim and this write.
        """
        # #1082 status-as-projection: the reclaimed slots are only *candidates*.
        # Authority for "is running" is the agent registry — every FAILED write
        # below goes through a just-in-time re-verify and the race-guarded
        # `fail_stale_slot_execution` (WHERE status='running'); status is never
        # the standalone authority for a destructive write.
        if not reclaimed:
            return

        agent_names = list(reclaimed.keys())
        async with httpx.AsyncClient(timeout=WATCHDOG_HTTP_TIMEOUT) as client:
            results = await asyncio.gather(
                *(self._get_agent_running_ids(client, name) for name in agent_names),
                return_exceptions=True,
            )
            per_agent_running: Dict[str, Optional[set]] = {}
            for name, result in zip(agent_names, results):
                if isinstance(result, BaseException):
                    logger.warning(
                        f"[Cleanup] Phase 3 re-verify failed for '{name}': {result}"
                    )
                    per_agent_running[name] = None
                else:
                    # result is Optional[set] here after the BaseException branch
                    per_agent_running[name] = result

            for agent_name, execution_ids in reclaimed.items():
                running_ids = per_agent_running.get(agent_name)

                for execution_id in execution_ids:
                    # #226: Phase 0 already confirmed this exec as running —
                    # trust it to save an HTTP call.
                    if execution_id in confirmed_running_ids:
                        logger.info(
                            f"[Cleanup] Skipping {execution_id} for '{agent_name}' "
                            f"— watchdog confirmed still running"
                        )
                        continue

                    # Just-in-time re-verify interpretation
                    if running_ids is None:
                        # Agent unreachable during re-verify (#497).
                        #
                        # The slot was already reclaimed by TTL — by
                        # construction the execution is older than
                        # `timeout_seconds + buffer`, which is Phase 1's
                        # criterion at a much shorter window. Force-fail
                        # via the race-guarded writer instead of waiting
                        # the full 120-min Phase 1 stale-cleanup deadline.
                        #
                        # Race safety: `fail_stale_slot_execution` has a
                        # `WHERE status='running'` guard, so a SUCCESS
                        # that landed between the slot reclaim and this
                        # write is preserved.
                        #
                        # Documented residual risk: if the agent later
                        # recovers and writes SUCCESS via
                        # `update_execution_status`, that path overwrites
                        # FAILED per #378's design. The execution must
                        # have run past its configured `timeout + buffer`
                        # for the slot to be reclaimed in the first
                        # place, so a "late SUCCESS" here represents a
                        # deliverable that exceeded its budget.
                        try:
                            updated = db.fail_stale_slot_execution(
                                execution_id=execution_id,
                                error=(
                                    f"{_LEASE_EXPIRED_TAG}: Stale execution — agent "
                                    f"'{agent_name}' unresponsive during cleanup "
                                    f"re-verify, slot TTL expired (#497)"
                                ),
                            )
                            if updated:
                                report.stale_slot_executions += 1
                                logger.info(
                                    f"[Cleanup] Failed execution {execution_id} for "
                                    f"agent '{agent_name}' "
                                    f"(slot reclaimed, agent unreachable during re-verify)"
                                )
                                # #1083: close the dispatch activity the (now-absent)
                                # fire-and-forget coroutine `finally` would have closed.
                                await self._close_stale_slot_activity(execution_id)
                                # #1578: the async #1083 lease expired with no
                                # result callback — emit agent.task.failed so a
                                # subscribed orchestrator is woken on the wedge.
                                event_dispatch_service.spawn_task_terminal_event(
                                    agent_name,
                                    execution_id,
                                    terminal_status=TaskExecutionStatus.FAILED,
                                    summary_or_error=(
                                        f"{_LEASE_EXPIRED_TAG}: agent '{agent_name}' "
                                        f"unresponsive during cleanup re-verify"
                                    ),
                                )
                            else:
                                # Race-guard refused — a real terminal write
                                # arrived first. Expected and benign.
                                logger.debug(
                                    f"[Cleanup] fail_stale_slot_execution declined "
                                    f"for {execution_id} on '{agent_name}' "
                                    f"(race-guard — already terminal)"
                                )
                        except Exception as e:
                            logger.error(
                                f"[Cleanup] Error failing {execution_id} after slot "
                                f"reclaim (unreachable branch): {e}"
                            )
                        continue

                    if execution_id in running_ids:
                        # #378: agent says this exec is still running — the
                        # slot TTL fired prematurely relative to the task.
                        # Skip; the task's own SUCCESS/FAILED write will
                        # land correctly later.
                        logger.info(
                            f"[Cleanup] Skipping {execution_id} for '{agent_name}' "
                            f"— re-verification shows still running (#378)"
                        )
                        continue

                    # #2433: the agent does not know it — but a live backend
                    # dispatcher may (parked in the agent-call queue past this
                    # slot's TTL, or mid-call on an image that does not yet
                    # report `pending_ids`). A row with a live dispatcher is not
                    # stale: the refresher renews the lease while it is parked
                    # and the dispatcher's own terminal (or its HTTP timeout)
                    # finishes it. An unreadable marker is not "absent" either;
                    # the registry-blind Phase-1 stale sweep stays the backstop.
                    verdict = (await _inflight_verdict_map([execution_id])).get(execution_id, "absent")
                    if verdict != "absent":
                        report.dispatch_inflight_skipped += 1
                        logger.warning(
                            f"[Cleanup] Skipping {execution_id} for '{agent_name}' — slot TTL "
                            f"expired but a backend dispatcher is "
                            f"{'alive' if verdict == 'alive' else 'unverifiable (Redis unreadable)'} (#2433)"
                        )
                        continue

                    # Re-verify confirmed inactive → safe to fail.
                    try:
                        # Issue #61: best-effort terminate before marking failed.
                        try:
                            await self._terminate_on_agent(
                                client, agent_name, execution_id
                            )
                        except Exception as term_err:
                            logger.debug(
                                f"[Cleanup] Could not terminate {execution_id}: {term_err}"
                            )

                        updated = db.fail_stale_slot_execution(
                            execution_id=execution_id,
                            error=f"{_LEASE_EXPIRED_TAG}: Stale execution — slot TTL expired for agent '{agent_name}', cleaned by cleanup service",
                        )
                        if updated:
                            report.stale_slot_executions += 1
                            logger.info(
                                f"[Cleanup] Failed execution {execution_id} for agent "
                                f"'{agent_name}' (slot reclaimed)"
                            )
                            # #1083: close the dispatch activity the (now-absent)
                            # fire-and-forget coroutine `finally` would have closed.
                            await self._close_stale_slot_activity(execution_id)
                            # #1578: async #1083 lease expired (no result
                            # callback) — emit agent.task.failed to wake a
                            # subscribed orchestrator on the wedge.
                            event_dispatch_service.spawn_task_terminal_event(
                                agent_name,
                                execution_id,
                                terminal_status=TaskExecutionStatus.FAILED,
                                summary_or_error=(
                                    f"{_LEASE_EXPIRED_TAG}: slot TTL expired for "
                                    f"agent '{agent_name}' (no result callback)"
                                ),
                            )
                    except Exception as e:
                        logger.error(
                            f"[Cleanup] Error failing {execution_id} after slot reclaim: {e}"
                        )

    async def _reconcile_orphaned_executions(
        self, report: Optional[CleanupReport] = None
    ) -> tuple[int, int, set]:
        """Reconcile DB execution state against agent process registries.

        For each execution marked 'running' in the DB:
        1. Check if the agent's process registry still has it (including
           the #921 recently-completed window — see `_get_agent_running_ids`)
        2. If not found: mark failed, release resources
        3. If found but exceeded timeout: terminate, mark failed, release resources

        Issue #921: the race between the agent's `finally: unregister()`
        and the backend's `update_execution_status(SUCCESS)` is closed at
        the source — agents include recently-completed IDs in their
        `/api/executions/running` response. The watchdog therefore needs
        no two-cycle confirmation: a single observation of "missing from
        agent + DB still running" is a true orphan.

        Returns:
            Tuple of (orphaned_count, auto_terminated_count, confirmed_running_ids)
            where confirmed_running_ids is the set of execution IDs verified as still
            running on their agents (used by slot cleanup to avoid false failures, #226).
        """
        # #1082 status-as-projection: status='running' is a *candidate filter*
        # only — the runtime authority for "is running" is the agent process
        # registry (queried below via _get_agent_running_ids). We never fail a
        # row on its status alone; a destructive write requires the agent to
        # confirm the execution is absent.
        running_executions = db.get_running_executions_with_agent_info()
        if not running_executions:
            return (0, 0, set())

        # Group by agent for batch HTTP calls (one call per agent)
        agents: Dict[str, List[Dict]] = defaultdict(list)
        for ex in running_executions:
            agents[ex["agent_name"]].append(ex)

        # Parallel fan-out: query all agents concurrently with a shared client
        async with httpx.AsyncClient(timeout=WATCHDOG_HTTP_TIMEOUT) as client:
            agent_names = list(agents.keys())
            results = await asyncio.gather(
                *(self._get_agent_running_ids(client, name) for name in agent_names),
                return_exceptions=True,
            )
            agent_running: Dict[str, Optional[set]] = {}
            for name, result in zip(agent_names, results):
                if isinstance(result, Exception):
                    logger.warning(f"[Watchdog] Error querying agent '{name}': {result}")
                    agent_running[name] = None
                else:
                    agent_running[name] = result

            orphaned_count = 0
            terminated_count = 0
            recovery_attempts = 0
            recovery_failures = 0
            confirmed_running: set = set()  # #226: track IDs verified as still running

            # #2433: proof-of-life has a second half — a LIVE BACKEND DISPATCHER.
            # An admitted row can be parked in the backend agent-call queue (or
            # queued on the agent, on an image that does not yet report
            # `pending_ids`) with the agent never having heard of it. Collect
            # every absent-from-agent candidate first so the cross-worker
            # markers are read with ONE Redis round-trip per sweep.
            candidates: List[str] = []
            for agent_name, executions in agents.items():
                known = agent_running.get(agent_name)
                if known is None:
                    continue
                for ex in executions:
                    if ex.get("id") and ex["id"] not in known:
                        candidates.append(ex["id"])
            inflight = await _inflight_verdict_map(candidates)
            skipped_by_agent: Dict[str, Dict[str, int]] = defaultdict(lambda: {"alive": 0, "unknown": 0})

            for agent_name, executions in agents.items():
                agent_running_ids = agent_running.get(agent_name)
                if agent_running_ids is None:
                    # Agent unreachable — skip entirely, retry next cycle
                    continue
                agent_reports_pending = bool(getattr(agent_running_ids, "reports_pending", False))

                for ex in executions:
                    try:
                        execution_id = ex["id"]
                        is_on_agent = execution_id in agent_running_ids

                        # Compute age for both orphan grace period and timeout checks
                        started_at = parse_iso_timestamp(ex["started_at"])
                        age_seconds = (utc_now() - started_at).total_seconds()

                        if not is_on_agent:
                            # Skip very recent executions that may still be dispatching
                            if age_seconds < WATCHDOG_MIN_AGE_SECONDS:
                                logger.debug(
                                    f"[Watchdog] Skipping {execution_id} — only "
                                    f"{int(age_seconds)}s old, may still be dispatching"
                                )
                                continue

                            # #2433: absent from the agent, but a live backend
                            # dispatcher may still own it (parked or mid-call).
                            # Orphan = the agent does not know it AND no
                            # dispatcher is alive for it.
                            verdict = inflight.get(execution_id, "absent")
                            if _inflight_skip(verdict, age_seconds):
                                skipped_by_agent[agent_name][verdict] += 1
                                if report is not None:
                                    report.dispatch_inflight_skipped += 1
                                continue

                            # Orphan: missing from agent's running + pending +
                            # recently-completed sets AND no live dispatcher.
                            # The agent-side window in
                            # `process_registry.list_recently_completed_ids`
                            # already absorbed the success-write race (#921),
                            # so this is a true orphan.
                            recovery_attempts += 1
                            error_msg = _orphan_error_message(agent_name, agent_reports_pending)
                            recovered = await self._recover_execution(
                                execution_id, agent_name, error_msg, "orphan_recovered",
                                client, report,
                            )
                            if recovered:
                                orphaned_count += 1
                        else:
                            # Execution is on agent — check timeout.
                            timeout_seconds = ex.get("timeout_seconds") or 900

                            if age_seconds <= timeout_seconds:
                                # Still running within timeout — mark as confirmed (#226)
                                confirmed_running.add(execution_id)
                                continue

                            # Auto-terminate: exceeded schedule timeout
                            recovery_attempts += 1
                            age_minutes = int(age_seconds / 60)
                            terminated = await self._terminate_on_agent(
                                client, agent_name, execution_id
                            )
                            if not terminated:
                                # Process may still be running — skip DB/resource
                                # cleanup, let the 120-min stale cleanup handle it
                                logger.warning(
                                    f"[Watchdog] Terminate failed for {execution_id} "
                                    f"on '{agent_name}' — deferring to stale cleanup"
                                )
                                continue
                            error_msg = (
                                f"Execution auto-terminated after {age_minutes} minutes "
                                f"by watchdog (exceeded timeout of {timeout_seconds}s)"
                            )
                            recovered = await self._recover_execution(
                                execution_id, agent_name, error_msg, "auto_terminated",
                                client, report,
                            )
                            if recovered:
                                terminated_count += 1

                    except Exception as e:
                        recovery_failures += 1
                        logger.error(
                            f"[Watchdog] Error recovering execution {ex.get('id', '?')} "
                            f"on agent '{agent_name}': {e}"
                        )

        # #2433: one line per agent per cycle, never per row — under a backlog
        # a per-row line would print every parked execution every 5 minutes.
        for _agent, _counts in skipped_by_agent.items():
            _log = logger.warning if _counts["unknown"] else logger.info
            _log(
                f"[Watchdog] Withheld orphan recovery on '{_agent}': {_counts['alive']} "
                f"execution(s) owned by a live backend dispatcher (parked/in-flight), "
                f"{_counts['unknown']} with an unreadable cross-worker marker (Redis) (#2433)"
            )

        # Systemic failure detection: warn if majority of recoveries failed
        if recovery_attempts > 0 and recovery_failures > recovery_attempts / 2:
            logger.warning(
                f"[Watchdog] Systemic failure: {recovery_failures}/{recovery_attempts} "
                f"recovery attempts failed in this cycle"
            )

        return (orphaned_count, terminated_count, confirmed_running)

    async def _get_agent_running_ids(
        self, client: httpx.AsyncClient, agent_name: str
    ) -> Optional[set]:
        """Get the set of execution IDs currently running on an agent.

        Args:
            client: Shared httpx client for the reconciliation cycle.
            agent_name: The agent to query.

        Returns:
            Set of execution IDs, or None if agent is unreachable.
        """
        try:
            response = await client.get(
                f"http://agent-{agent_name}:8000/api/executions/running",
                headers=build_agent_auth_headers(agent_name),
            )
            if response.status_code == 200:
                # #921: union of currently-running + recently-completed via
                # the shared helper — same parsing as `recover_orphaned_executions`.
                return _extract_agent_known_ids(response.json())
            else:
                logger.warning(
                    f"[Watchdog] Agent '{agent_name}' returned {response.status_code} "
                    f"from /api/executions/running"
                )
                return None
        except (httpx.ConnectError, httpx.TimeoutException):
            logger.debug(f"[Watchdog] Agent '{agent_name}' unreachable, skipping")
            return None
        except Exception as e:
            logger.warning(f"[Watchdog] Error checking agent '{agent_name}': {e}")
            return None

    async def _get_execution_error(
        self, client: httpx.AsyncClient, agent_name: str, execution_id: str
    ) -> Optional[str]:
        """Fetch the last error from an agent's execution log buffer.

        Issue #286: Preserves original error context when cleanup recovers
        stale executions. Queries the agent's /api/executions/{id}/last-error
        endpoint to retrieve error details before they're lost.

        Args:
            client: Shared httpx client for the reconciliation cycle.
            agent_name: The agent to query.
            execution_id: The execution to get error for.

        Returns:
            Error message string if found, None otherwise.
        """
        try:
            response = await client.get(
                f"http://agent-{agent_name}:8000/api/executions/{execution_id}/last-error",
                timeout=ERROR_FETCH_TIMEOUT,
                headers=build_agent_auth_headers(agent_name),
            )
            if response.status_code == 200:
                data = response.json()
                error_type = data.get("error_type")
                error_message = data.get("error_message")

                if error_type or error_message:
                    # Sanitize to remove any credential patterns
                    parts = []
                    if error_type:
                        parts.append(f"[{error_type}]")
                    if error_message:
                        sanitized = sanitize_text(error_message)
                        parts.append(sanitized)
                    return " ".join(parts) if parts else None

            return None
        except (httpx.ConnectError, httpx.TimeoutException):
            logger.debug(
                f"[Watchdog] Could not fetch error context for {execution_id} "
                f"from agent '{agent_name}' (unreachable)"
            )
            return None
        except Exception as e:
            logger.debug(
                f"[Watchdog] Error fetching error context for {execution_id}: {e}"
            )
            return None

    async def _recover_execution(
        self,
        execution_id: str,
        agent_name: str,
        error_msg: str,
        action: str,
        client: Optional[httpx.AsyncClient] = None,
        report: Optional[CleanupReport] = None,
    ) -> bool:
        """Mark execution as failed and release all associated resources.

        Shared helper for both orphan recovery and auto-terminate paths (DRY).

        Issue #286: Now attempts to fetch original error context from the agent
        before marking the execution as failed, preserving diagnostic info.

        Args:
            execution_id: The execution to recover.
            agent_name: The agent the execution belongs to.
            error_msg: Descriptive error message (cleanup reason).
            action: Event action type ("orphan_recovered" or "auto_terminated").
            client: Optional httpx client for fetching error context from agent.
            report: Optional CleanupReport — the #1804 activity close is counted
                into `activities_closed_on_recovery` when supplied. Passed
                through rather than returned so the reconciler's tuple arity
                (and its callers' unpacking) is unchanged.

        Returns:
            True if recovery succeeded, False if execution already transitioned.
        """
        # Issue #286: Try to fetch original error context from agent before marking failed
        original_error = None
        if client:
            original_error = await self._get_execution_error(client, agent_name, execution_id)

        # Combine original error with cleanup reason
        if original_error:
            combined_error = f"{original_error}. Cleanup: {error_msg}"
        else:
            combined_error = error_msg

        # Truncate to prevent DB bloat
        if len(combined_error) > MAX_ERROR_MESSAGE_LENGTH:
            combined_error = combined_error[:MAX_ERROR_MESSAGE_LENGTH - 3] + "..."

        updated = db.mark_execution_failed_by_watchdog(execution_id, combined_error)
        if not updated:
            # Race condition: execution completed normally between check and update
            return False

        # #1804: this writer just won the terminal CAS, so it owns closing the
        # paired dispatch activity. Before, the row stayed `started` until the
        # 120-minute backstop closed it with a fabricated
        # `duration_ms = now - started_at` — the Timeline rendered the agent as
        # still working for up to 2h, then recorded a ~120-minute failure.
        # Placed BEFORE the WS broadcast so the frontend's watchdog event and the
        # activity close land in that order. Fail-open (the helper swallows).
        try:
            from services.activity_service import activity_service

            if await activity_service.close_execution_activity(
                execution_id, TaskExecutionStatus.FAILED, error=combined_error
            ) and report is not None:
                report.activities_closed_on_recovery += 1
        except Exception as e:  # noqa: BLE001
            # The terminal write + capacity release already succeeded. A failure
            # in the close must not downgrade this to "recovery failed" — that
            # would make the watchdog retry a row it has already recovered.
            logger.warning(
                f"[Watchdog] Activity close failed for {execution_id}: {e}"
            )

        # Release capacity (idempotent — no error if already released).
        # CAPACITY-CONSOLIDATE (#428): single CapacityManager.release_if_matches
        # replaces the prior slot_service.release_slot + queue.force_release_if_matches
        # pair. The match check preserves the TOCTOU-safety the original Lua
        # script provided.
        try:
            capacity = get_capacity_manager()
            await capacity.release_if_matches(agent_name, execution_id)
        except Exception as e:
            logger.warning(f"[Watchdog] Error releasing capacity for {execution_id}: {e}")

        # Broadcast WebSocket event with combined error (includes original context)
        await self._broadcast_watchdog_event(action, agent_name, execution_id, combined_error)

        logger.info(
            f"[Watchdog] {action}: execution {execution_id} on agent '{agent_name}'"
        )
        return True

    async def _terminate_on_agent(
        self, client: httpx.AsyncClient, agent_name: str, execution_id: str
    ) -> bool:
        """Terminate an execution on an agent.

        Calls POST /api/executions/{id}/terminate on the agent.
        Returns True if the agent confirmed termination (HTTP 2xx),
        False otherwise. Callers should only proceed with DB/resource
        cleanup if termination succeeded.
        """
        try:
            response = await client.post(
                f"http://agent-{agent_name}:8000/api/executions/{execution_id}/terminate",
                headers=build_agent_auth_headers(agent_name),
            )
            if response.status_code < 300:
                return True
            logger.warning(
                f"[Watchdog] Terminate returned {response.status_code} for "
                f"{execution_id} on '{agent_name}'"
            )
            return False
        except Exception as e:
            logger.warning(
                f"[Watchdog] Failed to terminate execution {execution_id} "
                f"on agent '{agent_name}': {e}"
            )
            return False

    async def _broadcast_watchdog_event(
        self,
        action: str,
        agent_name: str,
        execution_id: str,
        reason: str,
    ) -> None:
        """Broadcast a watchdog recovery event via WebSocket."""
        if _ws_manager is None:
            logger.debug("[Watchdog] WebSocket manager not set — recovery event not broadcast")
            return

        event = json.dumps({
            "type": "watchdog_recovery",
            "agent_name": agent_name,
            "execution_id": execution_id,
            "action": action,
            "reason": reason,
            "timestamp": utc_now_iso(),
        })
        try:
            await _ws_manager.broadcast(event)
        except Exception as e:
            logger.debug(f"[Watchdog] WebSocket broadcast error: {e}")

    async def _heal_renamed_volume_bases(self) -> int:
        """One-shot boot heal for #1664: pin `volume_base_name` for agents that
        were renamed before the column existed. Returns the rows healed.

        Rename keeps the agent's volumes under the pre-rename name, and until
        #1664 nothing recorded that — the only surviving evidence is Docker's
        own mount table: container `agent-{current}` mounting
        `agent-{other}-workspace` at the home path IS a completed rename
        (Invariant #11). Any other agent — the overwhelming majority — mounts
        `agent-{current}-workspace`, matches the NULL default, and is skipped.

        Runs before the startup sweep so an already-renamed agent is owned by
        its volumes on the very first triage. Idempotent (the DB write only
        fills a NULL) and best-effort: the orphan sweep's unattached-streak +
        mounted-volume checks still stand between a heal failure and a delete.
        """
        healed = 0
        try:
            from services.docker_utils import (
                get_agent_workspace_volume_map,
                volume_base_from_workspace_volume,
            )

            mounts = await get_agent_workspace_volume_map()
            for agent_name, volume_name in mounts.items():
                base = volume_base_from_workspace_volume(volume_name)
                if not base or base == agent_name:
                    continue
                try:
                    if db.set_volume_base_name(agent_name, base):
                        healed += 1
                        logger.info(
                            "[#1664] Startup: pinned volume base %r for renamed "
                            "agent %r (from its container mount)",
                            base,
                            agent_name,
                        )
                except Exception as e:
                    logger.warning(
                        f"[#1664] could not pin volume base for {agent_name}: {e}"
                    )
        except Exception as e:
            logger.error(f"[#1664] volume-base heal error: {e}")
        return healed

    async def _cleanup_loop(self):
        """Main cleanup loop."""
        # One-shot startup hook for #740: any non-terminal agent_loops left
        # over from a prior process get marked `interrupted`. Loops do not
        # auto-resume. Runs once on boot, not every cycle.
        try:
            interrupted = db.mark_orphan_loops_interrupted()
            if interrupted > 0:
                logger.info(
                    f"[Cleanup] Startup: marked {interrupted} orphan agent_loops as interrupted (#740)"
                )
        except Exception as e:
            logger.error(f"[Cleanup] Loop orphan sweep error: {e}")

        # One-shot startup hook for #1664: agents renamed BEFORE
        # `volume_base_name` existed have an unpinned row, so their (still
        # old-named) volumes read as orphans. Reconstruct the pin from Docker
        # before the startup sweep runs.
        await self._heal_renamed_volume_bases()

        # #1638: report the effective windows + their source BEFORE the startup
        # sweep runs, so a retroactive default change is visible in the logs
        # ahead of the deletion it causes rather than only in its aftermath.
        log_effective_retention_windows()

        # Run initial cleanup on startup
        try:
            startup_report = await self.run_cleanup()
            if startup_report.total > 0:
                logger.info(f"[Cleanup] Startup sweep: {startup_report.to_dict()}")
            else:
                logger.info("[Cleanup] Startup sweep: no stale resources found")
        except Exception as e:
            logger.error(f"[Cleanup] Startup sweep error: {e}")

        while self._running:
            try:
                await asyncio.sleep(self.poll_interval)
            except asyncio.CancelledError:
                break

            try:
                await self.run_cleanup()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[Cleanup] Cycle error: {e}")


# Global service instance
cleanup_service = CleanupService()


# ---------------------------------------------------------------------------
# Startup-recovery readiness flag (#748)
# ---------------------------------------------------------------------------
# Set False at module load; flipped True at the end of
# recover_orphaned_executions(). The /internal/execute-task router returns
# 503 while False so the scheduler retries instead of racing recovery with
# a slot ZADD on a row that's about to be flipped to FAILED.

_startup_recovery_complete: bool = False


def is_startup_recovery_complete() -> bool:
    """Return True once startup orphan-recovery has finished (#748).

    The internal task-execution route uses this as a gate: while False, the
    backend is still in the window where startup recovery may flip in-flight
    rows to FAILED. Returning 503 lets the scheduler retry once the gate
    opens, instead of leaking a Redis capacity slot on the doomed row.
    """
    return _startup_recovery_complete


def mark_startup_recovery_complete() -> None:
    """Set the warming-up gate to admit /internal/execute-task calls (#748)."""
    global _startup_recovery_complete
    _startup_recovery_complete = True


def reset_startup_recovery_flag_for_tests() -> None:
    """Test-only helper: revert the gate to its pre-recovery state."""
    global _startup_recovery_complete
    _startup_recovery_complete = False


async def recover_orphaned_executions() -> Dict:
    """Recover orphaned task executions on backend startup.

    Two passes:

      1. SQL→Redis (legacy): for each 'running' schedule execution row,
         check the agent's container and process registry — if missing,
         mark the row failed and release any capacity slot.
      2. Redis→SQL (#749): scan ``agent:slots:*`` for members whose SQL
         row is terminal or missing and ZREM them. Necessary because a
         backend kill between slot ZADD and the finally-block ZREM leaves
         the slot leaked, and the SQL→Redis pass cannot see Redis-only
         orphans.

    #748: rows whose ``started_at`` is younger than
    ``STARTUP_RECOVERY_GRACE_SECONDS`` are *skipped* — they may be from a
    ``/internal/execute-task`` call that the scheduler queued while the
    backend was still booting and that is about to ZADD a capacity slot.
    Failing such a row would race the handler and leave a permanently
    leaked Redis slot. The skipped row is handled either by the regular
    watchdog cycle (which uses the same grace window) or by the now-late
    handler completing normally.

    Returns:
        Dict with recovered, still_running, skipped_grace, errors, and
        redis_slots_reclaimed counts.
    """
    from services.agent_client import AgentClientError, get_agent_client
    from services.docker_service import get_agent_container

    running = db.get_running_executions()
    if not running:
        # #749: still run the Redis-side sweep — orphan slots can exist
        # even when SQL has zero running rows (that is in fact the
        # textbook symptom of the kill-between-ZADD-and-ZREM bug).
        redis_reclaimed = await _reconcile_orphaned_slots()
        return {
            "recovered": 0,
            "still_running": 0,
            "skipped_grace": 0,
            "cas_lost": 0,
            "errors": 0,
            "redis_slots_reclaimed": sum(redis_reclaimed.values()),
            "activities_closed": 0,
        }

    capacity = get_capacity_manager()

    # Group by agent to minimize container/HTTP checks
    by_agent: Dict[str, list] = {}
    for execution in running:
        by_agent.setdefault(execution["agent_name"], []).append(execution)

    recovered = 0
    still_running = 0
    skipped_grace = 0
    errors = 0
    # #1804: per-pass counters accumulated by `_recover_execution` (the startup
    # path has no CleanupReport). `errors` lives here rather than in a local
    # because only `_recover_execution` can tell a genuine failure from a lost
    # CAS — see the bucketing at the call sites below.
    stats: Dict[str, int] = {"activities_closed": 0, "errors": 0}
    # A False from `_recover_execution` is EITHER a lost CAS or an exception,
    # and the two must PARTITION — counting the same execution into both
    # `cas_lost` and `errors` makes one failure read as two in the startup
    # report. Only `_recover_execution` can tell them apart, so it self-counts
    # errors and we subtract at the end.
    not_written = 0

    for agent_name, executions in by_agent.items():
        # Check if container is running
        container = get_agent_container(agent_name)
        if not container or container.status != "running":
            # Container down — all executions for this agent are orphaned
            for execution in executions:
                if _within_startup_grace(execution):
                    skipped_grace += 1
                    continue
                if await _recover_execution(execution, agent_name, capacity, stats):
                    recovered += 1
                else:
                    # #1804: `_recover_execution` now returns the terminal CAS
                    # bool (it must, to gate the activity close). A False is
                    # USUALLY benign — a real completion won the row during
                    # restart, which is RELIABILITY-005's guarded writer working
                    # exactly as designed — so it must not read as an error.
                    # Genuine failures self-count into stats["errors"] and are
                    # subtracted out below.
                    not_written += 1
            continue

        # Container is up — check agent's process registry
        registry_ids: set = set()
        try:
            client = get_agent_client(agent_name)
            resp = await client.get("/api/executions/running", timeout=5.0)
            if resp.status_code == 200:
                # #921: same union as the periodic watchdog — includes
                # recently-completed IDs so a backend restart that races
                # an in-flight completion doesn't false-orphan it.
                registry_ids = _extract_agent_known_ids(resp.json())
        except AgentClientError as e:
            logger.warning(f"[Recovery] Could not reach agent {agent_name} registry: {e}")

        # #2433: a row this agent does not know may still be owned by a live
        # dispatcher in the OTHER uvicorn worker (this one just booted, so its
        # own in-flight registry is empty — the cross-worker marker is the only
        # signal). One MGET for the whole agent. After a FULL restart every
        # marker lapses within INFLIGHT_MARKER_TTL_SECONDS, so at worst such a
        # row waits one periodic sweep instead of being recovered here.
        absent_ids = [
            e["id"] for e in executions
            if e["id"] not in registry_ids and not _within_startup_grace(e)
        ]
        inflight = await _inflight_verdict_map(absent_ids)

        for execution in executions:
            if execution["id"] in registry_ids:
                still_running += 1
            elif _within_startup_grace(execution):
                skipped_grace += 1
            elif _inflight_skip(
                inflight.get(execution["id"], "absent"), _row_age_seconds(execution)
            ):
                still_running += 1
                logger.info(
                    f"[Recovery] {execution['id']} on '{agent_name}' is owned by a live "
                    f"(or unverifiable) backend dispatcher in another worker — left running (#2433)"
                )
            else:
                if await _recover_execution(execution, agent_name, capacity, stats):
                    recovered += 1
                else:
                    not_written += 1  # #1804: see the partition note above

    errors = stats["errors"]
    cas_lost = not_written - errors
    logger.info(
        f"[Recovery] Task execution recovery complete: "
        f"recovered={recovered}, still_running={still_running}, "
        f"skipped_grace={skipped_grace}, cas_lost={cas_lost}, errors={errors}, "
        f"activities_closed={stats['activities_closed']}"
    )

    # #749: complete the asymmetric pair. The SQL→Redis pass above flips
    # SQL rows to FAILED when Redis lost their slot; the Redis→SQL pass
    # below ZREMs slots whose SQL row is terminal or missing (e.g. backend
    # killed between ZADD and ZREM). Without this pass the leaked slot
    # persists until 1200s TTL or the next acquire on this agent.
    redis_reclaimed = await _reconcile_orphaned_slots()
    redis_reclaimed_total = sum(redis_reclaimed.values())

    return {
        "recovered": recovered,
        "still_running": still_running,
        "skipped_grace": skipped_grace,
        # #1804: a terminal CAS lost to a real completion — benign, and counted
        # apart from `errors` so a healthy restart race can't read as a failure.
        "cas_lost": cas_lost,
        "errors": errors,
        "redis_slots_reclaimed": redis_reclaimed_total,
        # #1804: dispatch activities closed by this recovery pass.
        "activities_closed": stats["activities_closed"],
    }


def _within_startup_grace(execution: Dict) -> bool:
    """Return True if the execution's started_at is within the startup grace window.

    Mirrors the WATCHDOG_MIN_AGE_SECONDS pattern at the regular-cycle path
    (cleanup_service.py:609). Rows are skipped instead of failed during
    startup recovery so an in-flight ``/internal/execute-task`` call cannot
    race the recovery flip and leak a slot (#748).
    """
    raw = execution.get("started_at")
    if not raw:
        # No timestamp — be conservative and allow recovery to proceed.
        return False
    try:
        age_seconds = (utc_now() - parse_iso_timestamp(raw)).total_seconds()
    except Exception:
        return False
    return age_seconds < STARTUP_RECOVERY_GRACE_SECONDS


async def _recover_execution(
    execution: Dict, agent_name: str, capacity, stats: Optional[Dict] = None
) -> bool:
    """Mark a single execution as orphaned and release its capacity.

    #1804: the CAS bool was **discarded** here — the function returned True
    unconditionally absent an exception, so the caller counted a recovery even
    when a real completion had already won the row. It is now captured, gates
    the activity close (only the CAS winner owns it), and is returned.

    ``stats`` (optional) receives the ``activities_closed`` and ``errors``
    counts; the startup path has no ``CleanupReport``, it returns a plain dict.

    **Return contract**: True = this pass wrote the terminal. False = it did
    not, for one of two reasons the caller must NOT conflate — a lost CAS (a
    real completion landed first; benign) or an exception (counted into
    ``stats["errors"]`` above). The caller buckets False as ``cas_lost``.
    """
    error_message = "Execution orphaned — recovered on backend restart"
    try:
        # Use the guarded writer so a real completion that arrived during restart
        # is not overwritten (RELIABILITY-005).
        won = db.mark_execution_failed_by_watchdog(
            execution_id=execution["id"],
            error_message=error_message,
        )
        await capacity.release(agent_name, execution["id"])
        if won:
            # #1804: startup recovery is the issue's own reproduction path —
            # restart the backend mid-run and the execution goes terminal while
            # its chat_start activity stays `started` for the 120-minute
            # backstop to close with a fabricated duration.
            #
            # Own try/except: the terminal write and the capacity release above
            # already succeeded, so a close failure must NOT flip this row from
            # `recovered` to `errors` in the startup report.
            try:
                from services.activity_service import activity_service

                closed = await activity_service.close_execution_activity(
                    execution["id"], TaskExecutionStatus.FAILED, error=error_message
                )
                if closed and stats is not None:
                    stats["activities_closed"] = stats.get("activities_closed", 0) + 1
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    f"[Recovery] Activity close failed for {execution['id']}: {e}"
                )
        return won
    except Exception as e:
        logger.error(f"[Recovery] Error recovering execution {execution['id']}: {e}")
        # #1804: self-count so the caller can tell this apart from a lost CAS —
        # both return False, only this one is an error.
        if stats is not None:
            stats["errors"] = stats.get("errors", 0) + 1
        return False


async def _reconcile_orphaned_slots() -> Dict[str, int]:
    """Sweep Redis for slot members that have no live SQL execution (#749).

    SQL→Redis recovery (the existing path in `recover_orphaned_executions`)
    flips SQL rows to FAILED when Redis lost their slot. It is asymmetric —
    it doesn't catch the inverse leak, where Redis still holds a slot for an
    execution whose SQL row is either terminal (someone wrote SUCCESS /
    FAILED already) or missing entirely. That happens whenever the backend
    is killed between `capacity.acquire()` (ZADD) and the `finally`-block
    `capacity.release()` (ZREM): the in-flight handler dies, the slot
    stays. The canary S-01 invariant (Issue #411) detects exactly this
    shape; rows #26/#27/#28 in `canary_violations` reproduced it three
    cycles in a row during the same incident as #748.

    For each Redis slot member we:

      - skip drain sentinels (members starting with ``drain-``) — they
        are not executions;
      - skip members whose ZSET score is within
        ``SLOT_RECOVERY_GRACE_SECONDS`` of "now" — they may belong to a
        concurrent /internal/execute-task handler that has done its ZADD
        but not yet committed the SQL row (mirrors the SQL-side grace
        window in #748);
      - look up the execution by id in SQL: if the row is missing or its
        status is terminal (success/failed/cancelled/skipped), the slot
        is orphaned → ZREM the member and DELETE its metadata key.

    Returns a dict ``{agent_name: int}`` counting reclaimed slots per
    agent. Never raises — Redis-unreachable errors are logged and the
    call returns whatever was reclaimed before the failure.
    """
    import time

    from services.slot_service import get_slot_service

    try:
        slot_service = get_slot_service()
    except Exception as e:
        logger.error(f"[Recovery] Slot service unavailable; skipping Redis sweep: {e}")
        return {}

    redis_client = slot_service.redis
    prefix = slot_service.slots_prefix
    grace_cutoff = time.time() - SLOT_RECOVERY_GRACE_SECONDS

    reclaimed: Dict[str, int] = {}

    # SCAN the agent:slots:* keyspace (matches canary/snapshot.py:325 and
    # slot_service.cleanup_stale_slots — same SCAN pattern, count=200 to
    # keep network round-trips low under fleet scale).
    cursor = 0
    try:
        while True:
            cursor, keys = redis_client.scan(cursor=cursor, match=f"{prefix}*", count=200)
            for key in keys:
                # decode_responses=True → key is str.
                agent_name = key[len(prefix):]
                try:
                    members_with_scores = redis_client.zrange(
                        key, 0, -1, withscores=True
                    )
                except Exception as exc:
                    logger.warning(
                        f"[Recovery] ZRANGE failed for {key}: {exc}"
                    )
                    continue

                for execution_id, score in members_with_scores:
                    if execution_id.startswith(_DRAIN_SENTINEL_PREFIX):
                        continue
                    if float(score) >= grace_cutoff:
                        # In the grace window — may be an in-flight ZADD
                        # whose SQL row hasn't been written yet.
                        continue

                    # #1082 status-as-projection: the Redis slot member is the
                    # candidate; SQL status only *protects* a slot here (a
                    # non-terminal row is left alone). A slot is reclaimed solely
                    # when its row is terminal or missing — status is never read
                    # as authority to fail a running execution.
                    row = db.get_execution(execution_id)
                    if row is not None and row.status not in _TERMINAL_EXECUTION_STATUSES:
                        # Still active — leave the slot alone.
                        continue

                    # Orphan: SQL row missing OR terminal. Reclaim the slot.
                    try:
                        removed = redis_client.zrem(key, execution_id)
                        if removed:
                            metadata_key = slot_service._metadata_key(
                                agent_name, execution_id
                            )
                            redis_client.delete(metadata_key)
                            reclaimed[agent_name] = reclaimed.get(agent_name, 0) + 1
                            logger.info(
                                f"[Recovery] Reclaimed orphan slot: agent='{agent_name}' "
                                f"execution_id='{execution_id}' "
                                f"sql_status={'<missing>' if row is None else row.status}"
                            )
                    except Exception as exc:
                        logger.warning(
                            f"[Recovery] ZREM failed for {key}/{execution_id}: {exc}"
                        )

            if cursor == 0:
                break
    except Exception as e:
        # SCAN itself blew up — return partial results.
        logger.error(f"[Recovery] Redis SCAN failed during orphan-slot sweep: {e}")

    total = sum(reclaimed.values())
    if total:
        logger.info(
            f"[Recovery] Orphan-slot sweep reclaimed {total} slot(s) across "
            f"{len(reclaimed)} agent(s)"
        )
    return reclaimed
