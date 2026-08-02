"""Durable local control state for one delivery-conductor workspace.

The ledger stores only closed contract references, hashes, revisions, numeric
budgets, fence tokens, and sanitized reason codes.  It deliberately has no
dependency on Trinity's backend database.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Iterator, Literal

from .contracts import ContractValidationError, MAX_MESSAGE_BYTES, ProposedAction, Wake


ActionStatus = Literal["reserved", "completed", "ambiguous"]
ResultStatus = Literal["completed", "ambiguous"]

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_KNOWN_SECRET = re.compile(
    r"(?:ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"sk-[A-Za-z0-9_-]{20,}|pk_[A-Za-z0-9_-]{20,}|"
    r"xox[baprs]-[A-Za-z0-9-]{10,}|AKIA[A-Z0-9]{16}|"
    r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})"
)
_SENSITIVE_LABEL = re.compile(
    r"(?:^|[._:-])(?:password|passwd|secret|api[-_]?key|access[-_]?token|"
    r"refresh[-_]?token|private[-_]?key|authorization|bearer|ssn|passport|"
    r"email|phone|telephone|street[-_]?address|date[-_]?of[-_]?birth|dob)"
    r"(?:$|[._:-])",
    re.IGNORECASE,
)


class LedgerError(RuntimeError):
    """Base class for ledger failures that must stop a control tick."""


class LedgerValidationError(LedgerError, ValueError):
    """Raised before data outside the durable-state allowlist is stored."""


class StaleLeaseError(LedgerError):
    """Raised when a lease no longer owns the current fencing token."""


class ActionConflictError(LedgerError):
    """Raised when one action key is reused for different intent or outcome."""


@dataclass(frozen=True)
class Lease:
    wake_id: str
    fence_token: int
    claimed_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        _validate_identifier("wake_id", self.wake_id)
        _validate_non_negative_int("fence_token", self.fence_token)
        if self.fence_token == 0:
            raise LedgerValidationError("fence_token must be positive")
        _validate_utc_datetime("claimed_at", self.claimed_at)
        _validate_utc_datetime("expires_at", self.expires_at)
        if self.expires_at <= self.claimed_at:
            raise LedgerValidationError("expires_at must be later than claimed_at")


@dataclass(frozen=True)
class ActionReservation:
    action_key: str
    status: ActionStatus
    payload_sha256: str
    result_sha256: str | None = None
    reason_code: str | None = None

    def __post_init__(self) -> None:
        _validate_identifier("action_key", self.action_key)
        if self.status not in ("reserved", "completed", "ambiguous"):
            raise LedgerValidationError("status must be reserved, completed, or ambiguous")
        _validate_sha256("payload_sha256", self.payload_sha256)
        if self.status == "reserved":
            if self.result_sha256 is not None or self.reason_code is not None:
                raise LedgerValidationError("a reserved action cannot have a result")
            return
        _validate_sha256("result_sha256", self.result_sha256)
        _validate_identifier("reason_code", self.reason_code)


@dataclass(frozen=True)
class EffectResult:
    status: ResultStatus
    result_sha256: str
    reason_code: str

    def __post_init__(self) -> None:
        if self.status not in ("completed", "ambiguous"):
            raise LedgerValidationError("effect status must be completed or ambiguous")
        _validate_sha256("result_sha256", self.result_sha256)
        _validate_identifier("reason_code", self.reason_code)


@dataclass(frozen=True)
class Checkpoint:
    revision: str
    checkpoint_sha256: str
    acknowledged_wake_id: str
    reason_code: str
    run_units_remaining: int
    issue_units_remaining: int
    daily_units_remaining: int
    action_key: str | None = None
    action_status: ActionStatus | None = None
    action_result_sha256: str | None = None

    def __post_init__(self) -> None:
        _validate_identifier("revision", self.revision)
        _validate_sha256("checkpoint_sha256", self.checkpoint_sha256)
        _validate_identifier("acknowledged_wake_id", self.acknowledged_wake_id)
        _validate_identifier("reason_code", self.reason_code)
        _validate_non_negative_int("run_units_remaining", self.run_units_remaining)
        _validate_non_negative_int("issue_units_remaining", self.issue_units_remaining)
        _validate_non_negative_int("daily_units_remaining", self.daily_units_remaining)
        if self.action_key is None and self.action_status is None:
            if self.action_result_sha256 is not None:
                raise LedgerValidationError("action_result_sha256 requires an action outcome")
            return
        if self.action_key is None or self.action_status is None:
            raise LedgerValidationError("action_key and action_status must be provided together")
        _validate_identifier("action_key", self.action_key)
        if self.action_status not in ("reserved", "completed", "ambiguous"):
            raise LedgerValidationError("invalid checkpoint action_status")
        if self.action_status == "reserved":
            if self.action_result_sha256 is not None:
                raise LedgerValidationError("a reserved action cannot have a result")
        else:
            _validate_sha256("action_result_sha256", self.action_result_sha256)


@dataclass(frozen=True)
class TickOutcome:
    acknowledged: bool
    reason_code: str
    run_units: int
    issue_units: int
    daily_units: int

    def __post_init__(self) -> None:
        if not isinstance(self.acknowledged, bool):
            raise LedgerValidationError("acknowledged must be a boolean")
        _validate_identifier("reason_code", self.reason_code)
        _validate_non_negative_int("run_units", self.run_units)
        _validate_non_negative_int("issue_units", self.issue_units)
        _validate_non_negative_int("daily_units", self.daily_units)


class ControlLedger:
    """SQLite-backed inbox, fencing, replay, checkpoint, and budget ledger."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)

    def initialize(self) -> None:
        """Create the durable control schema idempotently."""
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS event_inbox (
                    wake_id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    source_event_id TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending'
                        CHECK (state IN ('pending', 'acknowledged')),
                    first_seen_at TEXT NOT NULL,
                    acknowledged_fence_token INTEGER,
                    UNIQUE (source, source_event_id)
                );

                CREATE TABLE IF NOT EXISTS controller_state (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    last_fence_token INTEGER NOT NULL CHECK (last_fence_token >= 0),
                    state TEXT NOT NULL CHECK (state IN ('idle', 'leased')),
                    reason_code TEXT
                );

                INSERT OR IGNORE INTO controller_state
                    (singleton, last_fence_token, state, reason_code)
                VALUES (1, 0, 'idle', NULL);

                CREATE TABLE IF NOT EXISTS repo_lease (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    wake_id TEXT NOT NULL REFERENCES event_inbox(wake_id),
                    fence_token INTEGER NOT NULL UNIQUE CHECK (fence_token > 0),
                    claimed_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS action_journal (
                    action_key TEXT PRIMARY KEY,
                    capability_name TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    target_revision TEXT NOT NULL,
                    invalidation_class TEXT NOT NULL,
                    status TEXT NOT NULL
                        CHECK (status IN ('reserved', 'completed', 'ambiguous')),
                    result_sha256 TEXT,
                    reason_code TEXT,
                    wake_id TEXT NOT NULL REFERENCES event_inbox(wake_id),
                    fence_token INTEGER NOT NULL CHECK (fence_token > 0),
                    CHECK (
                        (status = 'reserved' AND result_sha256 IS NULL AND reason_code IS NULL)
                        OR
                        (status IN ('completed', 'ambiguous')
                         AND result_sha256 IS NOT NULL AND reason_code IS NOT NULL)
                    )
                );

                CREATE TABLE IF NOT EXISTS action_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action_key TEXT NOT NULL REFERENCES action_journal(action_key),
                    event_type TEXT NOT NULL
                        CHECK (event_type IN ('reserved', 'completed', 'ambiguous')),
                    wake_id TEXT NOT NULL REFERENCES event_inbox(wake_id),
                    fence_token INTEGER NOT NULL CHECK (fence_token > 0),
                    payload_sha256 TEXT NOT NULL,
                    result_sha256 TEXT,
                    reason_code TEXT
                );

                CREATE TRIGGER IF NOT EXISTS action_events_reject_update
                BEFORE UPDATE ON action_events
                BEGIN
                    SELECT RAISE(ABORT, 'action_events is append-only');
                END;

                CREATE TRIGGER IF NOT EXISTS action_events_reject_delete
                BEFORE DELETE ON action_events
                BEGIN
                    SELECT RAISE(ABORT, 'action_events is append-only');
                END;

                CREATE TABLE IF NOT EXISTS run_checkpoint (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    revision TEXT NOT NULL,
                    checkpoint_sha256 TEXT NOT NULL,
                    acknowledged_wake_id TEXT NOT NULL REFERENCES event_inbox(wake_id),
                    reason_code TEXT NOT NULL,
                    fence_token INTEGER NOT NULL CHECK (fence_token > 0),
                    run_units_remaining INTEGER NOT NULL CHECK (run_units_remaining >= 0),
                    issue_units_remaining INTEGER NOT NULL CHECK (issue_units_remaining >= 0),
                    daily_units_remaining INTEGER NOT NULL CHECK (daily_units_remaining >= 0),
                    action_key TEXT REFERENCES action_journal(action_key),
                    action_status TEXT
                        CHECK (action_status IN ('reserved', 'completed', 'ambiguous')),
                    action_result_sha256 TEXT,
                    CHECK (
                        (action_key IS NULL AND action_status IS NULL
                         AND action_result_sha256 IS NULL)
                        OR
                        (action_key IS NOT NULL AND action_status = 'reserved'
                         AND action_result_sha256 IS NULL)
                        OR
                        (action_key IS NOT NULL
                         AND action_status IN ('completed', 'ambiguous')
                         AND action_result_sha256 IS NOT NULL)
                    )
                );

                CREATE TABLE IF NOT EXISTS budget_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    wake_id TEXT NOT NULL REFERENCES event_inbox(wake_id),
                    fence_token INTEGER NOT NULL UNIQUE CHECK (fence_token > 0),
                    run_units INTEGER NOT NULL CHECK (run_units >= 0),
                    issue_units INTEGER NOT NULL CHECK (issue_units >= 0),
                    daily_units INTEGER NOT NULL CHECK (daily_units >= 0),
                    reason_code TEXT NOT NULL
                );
                """
            )

    def claim_wake(self, wake: Wake, now: datetime, lease_seconds: int) -> Lease | None:
        """Persist one wake and acquire the sole lease when it is available."""
        if not isinstance(wake, Wake):
            raise LedgerValidationError("wake must be a Wake")
        _validate_utc_datetime("now", now)
        if type(lease_seconds) is not int or lease_seconds <= 0:
            raise LedgerValidationError("lease_seconds must be a positive integer")
        now_text = _utc_text(now)
        expires_at = now + timedelta(seconds=lease_seconds)
        expires_text = _utc_text(expires_at)

        with self._transaction() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO event_inbox
                    (wake_id, source, source_event_id, payload_sha256, first_seen_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    wake.wake_id,
                    wake.source,
                    wake.source_event_id,
                    wake.payload_sha256,
                    now_text,
                ),
            )
            stored = connection.execute(
                """
                SELECT wake_id, source, source_event_id, payload_sha256, state
                FROM event_inbox
                WHERE wake_id = ? OR (source = ? AND source_event_id = ?)
                """,
                (wake.wake_id, wake.source, wake.source_event_id),
            ).fetchall()
            expected = (wake.wake_id, wake.source, wake.source_event_id, wake.payload_sha256)
            if len(stored) != 1 or tuple(stored[0][:4]) != expected:
                raise LedgerValidationError("conflicting wake deduplication identity")
            if stored[0][4] == "acknowledged":
                return None

            current = connection.execute(
                "SELECT expires_at FROM repo_lease WHERE singleton = 1"
            ).fetchone()
            if current is not None and current[0] > now_text:
                return None
            if current is not None:
                connection.execute("DELETE FROM repo_lease WHERE singleton = 1")

            row = connection.execute(
                "SELECT last_fence_token FROM controller_state WHERE singleton = 1"
            ).fetchone()
            if row is None:
                raise LedgerError("controller_state is not initialized")
            fence_token = row[0] + 1
            connection.execute(
                """
                UPDATE controller_state
                SET last_fence_token = ?, state = 'leased', reason_code = NULL
                WHERE singleton = 1
                """,
                (fence_token,),
            )
            connection.execute(
                """
                INSERT INTO repo_lease
                    (singleton, wake_id, fence_token, claimed_at, expires_at)
                VALUES (1, ?, ?, ?, ?)
                """,
                (wake.wake_id, fence_token, now_text, expires_text),
            )

        return Lease(wake.wake_id, fence_token, now, expires_at)

    def reserve_action(self, lease: Lease, action: ProposedAction) -> ActionReservation:
        """Reserve one stable action identity or return its durable replay state."""
        canonical_payload = _validated_action_payload(action)
        payload_sha256 = hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()

        with self._transaction() as connection:
            self._require_current_lease(connection, lease)
            row = connection.execute(
                """
                SELECT capability_name, payload_json, payload_sha256, target_revision,
                       invalidation_class, status, result_sha256, reason_code
                FROM action_journal WHERE action_key = ?
                """,
                (action.action_key,),
            ).fetchone()
            identity = (
                action.capability_name,
                canonical_payload,
                payload_sha256,
                action.target_revision,
                action.invalidation_class,
            )
            if row is None:
                connection.execute(
                    """
                    INSERT INTO action_journal
                        (action_key, capability_name, payload_json, payload_sha256,
                         target_revision, invalidation_class, status, result_sha256,
                         reason_code, wake_id, fence_token)
                    VALUES (?, ?, ?, ?, ?, ?, 'reserved', NULL, NULL, ?, ?)
                    """,
                    (action.action_key, *identity, lease.wake_id, lease.fence_token),
                )
                connection.execute(
                    """
                    INSERT INTO action_events
                        (action_key, event_type, wake_id, fence_token, payload_sha256)
                    VALUES (?, 'reserved', ?, ?, ?)
                    """,
                    (action.action_key, lease.wake_id, lease.fence_token, payload_sha256),
                )
                return ActionReservation(action.action_key, "reserved", payload_sha256)

            if tuple(row[:5]) != identity:
                raise ActionConflictError("action_key is already reserved for different intent")
            status = row[5]
            if status == "reserved":
                connection.execute(
                    """
                    UPDATE action_journal SET wake_id = ?, fence_token = ?
                    WHERE action_key = ?
                    """,
                    (lease.wake_id, lease.fence_token, action.action_key),
                )
            return ActionReservation(action.action_key, status, payload_sha256, row[6], row[7])

    def record_result(self, lease: Lease, action_key: str, result: EffectResult) -> None:
        """Record one completed or ambiguous executor outcome idempotently."""
        _validate_identifier("action_key", action_key)
        if not isinstance(result, EffectResult):
            raise LedgerValidationError("result must be an EffectResult")
        with self._transaction() as connection:
            self._require_current_lease(connection, lease)
            row = connection.execute(
                """
                SELECT status, payload_sha256, result_sha256, reason_code,
                       wake_id, fence_token
                FROM action_journal WHERE action_key = ?
                """,
                (action_key,),
            ).fetchone()
            if row is None:
                raise ActionConflictError("action was not reserved")
            if (row[4], row[5]) != (lease.wake_id, lease.fence_token):
                raise StaleLeaseError("action reservation fence does not match the lease")
            if row[0] != "reserved":
                if tuple(row[::2]) == (result.status, result.result_sha256) and row[3] == result.reason_code:
                    return
                raise ActionConflictError("action already has a different terminal result")
            connection.execute(
                """
                UPDATE action_journal
                SET status = ?, result_sha256 = ?, reason_code = ?,
                    wake_id = ?, fence_token = ?
                WHERE action_key = ?
                """,
                (
                    result.status,
                    result.result_sha256,
                    result.reason_code,
                    lease.wake_id,
                    lease.fence_token,
                    action_key,
                ),
            )
            connection.execute(
                """
                INSERT INTO action_events
                    (action_key, event_type, wake_id, fence_token, payload_sha256,
                     result_sha256, reason_code)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    action_key,
                    result.status,
                    lease.wake_id,
                    lease.fence_token,
                    row[1],
                    result.result_sha256,
                    result.reason_code,
                ),
            )

    def checkpoint(self, lease: Lease, checkpoint: Checkpoint) -> None:
        """Replace the current fenced checkpoint with one verified safe snapshot."""
        if not isinstance(checkpoint, Checkpoint):
            raise LedgerValidationError("checkpoint must be a Checkpoint")
        if checkpoint.acknowledged_wake_id != lease.wake_id:
            raise LedgerValidationError("checkpoint must acknowledge the leased wake")
        with self._transaction() as connection:
            self._require_current_lease(connection, lease)
            self._require_action_outcome(connection, checkpoint)
            connection.execute(
                """
                INSERT INTO run_checkpoint
                    (singleton, revision, checkpoint_sha256, acknowledged_wake_id,
                     reason_code, fence_token, run_units_remaining,
                     issue_units_remaining, daily_units_remaining, action_key,
                     action_status, action_result_sha256)
                VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(singleton) DO UPDATE SET
                    revision = excluded.revision,
                    checkpoint_sha256 = excluded.checkpoint_sha256,
                    acknowledged_wake_id = excluded.acknowledged_wake_id,
                    reason_code = excluded.reason_code,
                    fence_token = excluded.fence_token,
                    run_units_remaining = excluded.run_units_remaining,
                    issue_units_remaining = excluded.issue_units_remaining,
                    daily_units_remaining = excluded.daily_units_remaining,
                    action_key = excluded.action_key,
                    action_status = excluded.action_status,
                    action_result_sha256 = excluded.action_result_sha256
                """,
                (
                    checkpoint.revision,
                    checkpoint.checkpoint_sha256,
                    checkpoint.acknowledged_wake_id,
                    checkpoint.reason_code,
                    lease.fence_token,
                    checkpoint.run_units_remaining,
                    checkpoint.issue_units_remaining,
                    checkpoint.daily_units_remaining,
                    checkpoint.action_key,
                    checkpoint.action_status,
                    checkpoint.action_result_sha256,
                ),
            )

    def release(self, lease: Lease, outcome: TickOutcome) -> None:
        """Accumulate usage, optionally acknowledge the wake, and release the lease."""
        if not isinstance(outcome, TickOutcome):
            raise LedgerValidationError("outcome must be a TickOutcome")
        with self._transaction() as connection:
            self._require_current_lease(connection, lease)
            connection.execute(
                """
                INSERT INTO budget_usage
                    (wake_id, fence_token, run_units, issue_units, daily_units, reason_code)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    lease.wake_id,
                    lease.fence_token,
                    outcome.run_units,
                    outcome.issue_units,
                    outcome.daily_units,
                    outcome.reason_code,
                ),
            )
            if outcome.acknowledged:
                connection.execute(
                    """
                    UPDATE event_inbox
                    SET state = 'acknowledged', acknowledged_fence_token = ?
                    WHERE wake_id = ?
                    """,
                    (lease.fence_token, lease.wake_id),
                )
            connection.execute("DELETE FROM repo_lease WHERE singleton = 1")
            connection.execute(
                """
                UPDATE controller_state
                SET state = 'idle', reason_code = ? WHERE singleton = 1
                """,
                (outcome.reason_code,),
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5.0, isolation_level=None)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _require_current_lease(connection: sqlite3.Connection, lease: Lease) -> None:
        if not isinstance(lease, Lease):
            raise LedgerValidationError("lease must be a Lease")
        row = connection.execute(
            """
            SELECT wake_id, fence_token, claimed_at, expires_at
            FROM repo_lease WHERE singleton = 1
            """
        ).fetchone()
        expected = (
            lease.wake_id,
            lease.fence_token,
            _utc_text(lease.claimed_at),
            _utc_text(lease.expires_at),
        )
        if row is None or tuple(row) != expected:
            raise StaleLeaseError("lease is stale or has a mismatched fencing token")

    @staticmethod
    def _require_action_outcome(
        connection: sqlite3.Connection, checkpoint: Checkpoint
    ) -> None:
        if checkpoint.action_key is None:
            return
        row = connection.execute(
            """
            SELECT status, result_sha256 FROM action_journal WHERE action_key = ?
            """,
            (checkpoint.action_key,),
        ).fetchone()
        expected = (checkpoint.action_status, checkpoint.action_result_sha256)
        if row is None or tuple(row) != expected:
            raise LedgerValidationError("checkpoint action outcome does not match the journal")


def _validated_action_payload(action: ProposedAction) -> str:
    if not isinstance(action, ProposedAction):
        raise LedgerValidationError("action must be a ProposedAction")
    if len(action.payload_json.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise LedgerValidationError("proposed action payload exceeds 1 MiB")
    try:
        validated = ProposedAction(
            capability_name=action.capability_name,
            action_key=action.action_key,
            payload_json=action.payload_json,
            target_revision=action.target_revision,
            invalidation_class=action.invalidation_class,
        )
    except ContractValidationError as error:
        raise LedgerValidationError("proposed action violates the closed reference contract") from error
    payload = json.loads(validated.payload_json)
    for value in _iter_strings(payload):
        if _KNOWN_SECRET.search(value) or _SENSITIVE_LABEL.search(value):
            raise LedgerValidationError("proposed action contains a sensitive-data indicator")
    return validated.payload_json


def _iter_strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _iter_strings(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _iter_strings(item)


def _validate_identifier(name: str, value: object) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise LedgerValidationError(f"{name} must be a sanitized identifier")


def _validate_sha256(name: str, value: object) -> None:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise LedgerValidationError(f"{name} must be a lowercase SHA-256 digest")


def _validate_non_negative_int(name: str, value: object) -> None:
    if type(value) is not int or value < 0:
        raise LedgerValidationError(f"{name} must be a non-negative integer")


def _validate_utc_datetime(name: str, value: object) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise LedgerValidationError(f"{name} must be an aware UTC datetime")


def _utc_text(value: datetime) -> str:
    _validate_utc_datetime("datetime", value)
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
