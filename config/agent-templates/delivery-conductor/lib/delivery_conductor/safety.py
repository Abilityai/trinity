"""Durable safety budgets and breaker state derived from the control ledger."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, fields
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Callable, Iterator, Literal, Mapping

from .contracts import BudgetView, MAX_MESSAGE_BYTES, Wake


BreakerState = Literal["closed", "open"]
SafetyEventKind = Literal[
    "attempt",
    "repair-cycle",
    "stale-lease",
    "orphaned-worker",
    "safety-violation",
    "no-work",
    "deterministic-failure",
    "transient-failure",
]

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_EVENT_KINDS = frozenset(
    {
        "attempt",
        "repair-cycle",
        "stale-lease",
        "orphaned-worker",
        "safety-violation",
        "no-work",
        "deterministic-failure",
        "transient-failure",
    }
)


class SafetyValidationError(ValueError):
    """Raised when policy or durable safety state is malformed or conflicting."""


class BreakerAuthorizationError(PermissionError):
    """Raised when an opaque authorization input cannot reset a breaker."""


@dataclass(frozen=True)
class SafetyLimits:
    max_attempts_per_signature: int
    max_repair_cycles: int
    max_run_seconds: int
    max_issue_units: int
    max_daily_units: int
    max_stale_leases: int
    max_orphaned_workers: int
    max_safety_events: int
    max_no_work_ticks: int

    def __post_init__(self) -> None:
        for item in fields(self):
            _validate_non_negative_int(item.name, getattr(self, item.name))

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> SafetyLimits:
        if not isinstance(value, Mapping):
            raise SafetyValidationError("ceilings must be an object")
        expected = {item.name for item in fields(cls)}
        actual = set(value)
        if actual != expected:
            raise SafetyValidationError("all required safety ceilings must be provided")
        return cls(**{name: value[name] for name in expected})  # type: ignore[arg-type]


@dataclass(frozen=True)
class SafetyScope:
    run_id: str
    issue_id: str
    signature: str

    def __post_init__(self) -> None:
        _validate_identifier("run_id", self.run_id)
        _validate_identifier("issue_id", self.issue_id)
        _validate_identifier("signature", self.signature)


@dataclass(frozen=True)
class SafetyPolicyRequest:
    schema_version: Literal[1]
    wake: Wake
    now_utc: str

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise SafetyValidationError("schema_version must be 1")
        if not isinstance(self.wake, Wake):
            raise SafetyValidationError("wake must be a Wake")
        _parse_utc_text(self.now_utc)


@dataclass(frozen=True)
class SafetyPolicy:
    schema_version: Literal[1]
    scope: SafetyScope
    limits: SafetyLimits


@dataclass(frozen=True)
class SafetyUsage:
    attempts: int
    repair_cycles: int
    run_seconds: int
    issue_units: int
    daily_units: int
    stale_leases: int
    orphaned_workers: int
    safety_events: int
    no_work_ticks: int
    deterministic_failures: int
    transient_failures: int

    def __post_init__(self) -> None:
        for item in fields(self):
            _validate_non_negative_int(item.name, getattr(self, item.name))


@dataclass(frozen=True)
class BreakerView:
    state: BreakerState
    reason_code: str
    transition_sequence: int
    updated_at_utc: str

    def __post_init__(self) -> None:
        if self.state not in ("closed", "open"):
            raise SafetyValidationError("breaker state is invalid")
        _validate_identifier("reason_code", self.reason_code)
        _validate_non_negative_int("transition_sequence", self.transition_sequence)
        _parse_utc_text(self.updated_at_utc)


@dataclass(frozen=True)
class BreakerTransition:
    transition_sequence: int
    scope: SafetyScope
    from_state: BreakerState
    to_state: BreakerState
    reason_code: str
    occurred_at_utc: str


@dataclass(frozen=True)
class SafetyAssessment:
    scope: SafetyScope
    limits: SafetyLimits
    usage: SafetyUsage
    budget_view: BudgetView
    breaker: BreakerView
    allows_effect: bool


@dataclass(frozen=True)
class BreakerResetContext:
    """Non-secret identity of the one open breaker generation being reset."""

    scope: SafetyScope
    transition_sequence: int
    reason_code: str
    safety_event_sequence: int

    def __post_init__(self) -> None:
        _require_scope(self.scope)
        if type(self.transition_sequence) is not int or self.transition_sequence <= 0:
            raise SafetyValidationError(
                "reset transition_sequence must be a positive integer"
            )
        _validate_identifier("reason_code", self.reason_code)
        _validate_non_negative_int(
            "safety_event_sequence",
            self.safety_event_sequence,
        )


class BreakerResetAuthorizer:
    """Opaque authorization boundary supplied by trusted operator policy."""

    def __init__(
        self,
        verifier: Callable[[object, BreakerResetContext], bool],
    ) -> None:
        if not callable(verifier):
            raise SafetyValidationError("reset authorization verifier is invalid")
        self._verifier = verifier

    def authorize(
        self,
        authorization_input: object,
        context: BreakerResetContext,
    ) -> bool:
        try:
            return self._verifier(authorization_input, context) is True
        except Exception:
            return False


def serialize_safety_policy_request(request: SafetyPolicyRequest) -> str:
    if not isinstance(request, SafetyPolicyRequest):
        raise SafetyValidationError("request must be a SafetyPolicyRequest")
    value = {
        "schema_version": 1,
        "kind": "safety-policy",
        "wake": {
            "wake_id": request.wake.wake_id,
            "source": request.wake.source,
            "source_event_id": request.wake.source_event_id,
            "payload_sha256": request.wake.payload_sha256,
        },
        "now_utc": request.now_utc,
    }
    return _bounded_json(value)


def parse_safety_policy_json(message: str) -> SafetyPolicy:
    value = _parse_bounded_object(message)
    _require_exact_keys(
        value,
        {"schema_version", "kind", "run_id", "issue_id", "signature", "ceilings"},
        "safety policy",
    )
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise SafetyValidationError("schema_version must be 1")
    if value["kind"] != "safety-policy":
        raise SafetyValidationError("safety policy kind is invalid")
    scope = SafetyScope(
        _required_string(value, "run_id"),
        _required_string(value, "issue_id"),
        _required_string(value, "signature"),
    )
    ceilings = value["ceilings"]
    if not isinstance(ceilings, dict):
        raise SafetyValidationError("ceilings must be an object")
    return SafetyPolicy(1, scope, SafetyLimits.from_mapping(ceilings))


class SafetyController:
    """Atomically reconcile durable usage, ceilings, and breaker transitions."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        reset_authorizer: BreakerResetAuthorizer | None = None,
    ) -> None:
        self.database_path = Path(database_path)
        if reset_authorizer is not None and not isinstance(
            reset_authorizer, BreakerResetAuthorizer
        ):
            raise TypeError("reset_authorizer must be a BreakerResetAuthorizer")
        self._reset_authorizer = reset_authorizer

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            base = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'budget_usage'"
            ).fetchone()
            if base is None:
                raise SafetyValidationError("control ledger must be initialized first")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS conductor_wake_scope (
                    wake_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    issue_id TEXT NOT NULL,
                    signature TEXT NOT NULL,
                    bound_at_utc TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS conductor_safety_limits (
                    run_id TEXT NOT NULL,
                    issue_id TEXT NOT NULL,
                    signature TEXT NOT NULL,
                    started_at_utc TEXT NOT NULL,
                    last_observed_at_utc TEXT NOT NULL,
                    max_attempts_per_signature INTEGER NOT NULL CHECK (max_attempts_per_signature >= 0),
                    max_repair_cycles INTEGER NOT NULL CHECK (max_repair_cycles >= 0),
                    max_run_seconds INTEGER NOT NULL CHECK (max_run_seconds >= 0),
                    max_issue_units INTEGER NOT NULL CHECK (max_issue_units >= 0),
                    max_daily_units INTEGER NOT NULL CHECK (max_daily_units >= 0),
                    max_stale_leases INTEGER NOT NULL CHECK (max_stale_leases >= 0),
                    max_orphaned_workers INTEGER NOT NULL CHECK (max_orphaned_workers >= 0),
                    max_safety_events INTEGER NOT NULL CHECK (max_safety_events >= 0),
                    max_no_work_ticks INTEGER NOT NULL CHECK (max_no_work_ticks >= 0),
                    PRIMARY KEY (run_id, issue_id, signature)
                );

                CREATE TABLE IF NOT EXISTS conductor_safety_events (
                    event_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_key TEXT NOT NULL UNIQUE,
                    event_kind TEXT NOT NULL CHECK (
                        event_kind IN (
                            'attempt', 'repair-cycle', 'stale-lease',
                            'orphaned-worker', 'safety-violation', 'no-work',
                            'deterministic-failure', 'transient-failure'
                        )
                    ),
                    run_id TEXT NOT NULL,
                    issue_id TEXT NOT NULL,
                    signature TEXT NOT NULL,
                    units INTEGER NOT NULL CHECK (units > 0),
                    occurred_at_utc TEXT NOT NULL
                );

                CREATE TRIGGER IF NOT EXISTS conductor_safety_events_reject_update
                BEFORE UPDATE ON conductor_safety_events
                BEGIN
                    SELECT RAISE(ABORT, 'conductor_safety_events is append-only');
                END;

                CREATE TRIGGER IF NOT EXISTS conductor_safety_events_reject_delete
                BEFORE DELETE ON conductor_safety_events
                BEGIN
                    SELECT RAISE(ABORT, 'conductor_safety_events is append-only');
                END;

                CREATE TABLE IF NOT EXISTS conductor_result_observations (
                    fence_token INTEGER PRIMARY KEY CHECK (fence_token > 0),
                    wake_id TEXT NOT NULL REFERENCES event_inbox(wake_id),
                    run_id TEXT NOT NULL,
                    issue_id TEXT NOT NULL,
                    signature TEXT NOT NULL,
                    run_units INTEGER NOT NULL CHECK (run_units >= 0),
                    issue_units INTEGER NOT NULL CHECK (issue_units >= 0),
                    daily_units INTEGER NOT NULL CHECK (daily_units >= 0),
                    observed_at_utc TEXT NOT NULL,
                    action_key TEXT NOT NULL,
                    result_status TEXT NOT NULL CHECK (
                        result_status IN ('completed', 'ambiguous')
                    ),
                    result_sha256 TEXT NOT NULL,
                    reason_code TEXT NOT NULL
                );

                CREATE TRIGGER IF NOT EXISTS conductor_result_observations_reject_update
                BEFORE UPDATE ON conductor_result_observations
                BEGIN
                    SELECT RAISE(ABORT, 'conductor_result_observations is append-only');
                END;

                CREATE TRIGGER IF NOT EXISTS conductor_result_observations_reject_delete
                BEFORE DELETE ON conductor_result_observations
                BEGIN
                    SELECT RAISE(ABORT, 'conductor_result_observations is append-only');
                END;

                CREATE TABLE IF NOT EXISTS conductor_cost_usage (
                    fence_token INTEGER PRIMARY KEY CHECK (fence_token > 0),
                    wake_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    issue_id TEXT NOT NULL,
                    signature TEXT NOT NULL,
                    run_units INTEGER NOT NULL CHECK (run_units >= 0),
                    issue_units INTEGER NOT NULL CHECK (issue_units >= 0),
                    daily_units INTEGER NOT NULL CHECK (daily_units >= 0),
                    recorded_at_utc TEXT NOT NULL
                );

                CREATE TRIGGER IF NOT EXISTS conductor_cost_usage_reject_update
                BEFORE UPDATE ON conductor_cost_usage
                BEGIN
                    SELECT RAISE(ABORT, 'conductor_cost_usage is append-only');
                END;

                CREATE TRIGGER IF NOT EXISTS conductor_cost_usage_reject_delete
                BEFORE DELETE ON conductor_cost_usage
                BEGIN
                    SELECT RAISE(ABORT, 'conductor_cost_usage is append-only');
                END;

                CREATE TABLE IF NOT EXISTS conductor_breaker_current (
                    run_id TEXT NOT NULL,
                    issue_id TEXT NOT NULL,
                    signature TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('closed', 'open')),
                    reason_code TEXT NOT NULL,
                    transition_sequence INTEGER NOT NULL CHECK (transition_sequence >= 0),
                    updated_at_utc TEXT NOT NULL,
                    PRIMARY KEY (run_id, issue_id, signature)
                );

                CREATE TABLE IF NOT EXISTS conductor_breaker_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    issue_id TEXT NOT NULL,
                    signature TEXT NOT NULL,
                    transition_sequence INTEGER NOT NULL CHECK (transition_sequence > 0),
                    from_state TEXT NOT NULL CHECK (from_state IN ('closed', 'open')),
                    to_state TEXT NOT NULL CHECK (to_state IN ('closed', 'open')),
                    reason_code TEXT NOT NULL,
                    occurred_at_utc TEXT NOT NULL,
                    UNIQUE (run_id, issue_id, signature, transition_sequence)
                );

                CREATE TRIGGER IF NOT EXISTS conductor_breaker_events_reject_update
                BEFORE UPDATE ON conductor_breaker_events
                BEGIN
                    SELECT RAISE(ABORT, 'conductor_breaker_events is append-only');
                END;

                CREATE TRIGGER IF NOT EXISTS conductor_breaker_events_reject_delete
                BEFORE DELETE ON conductor_breaker_events
                BEGIN
                    SELECT RAISE(ABORT, 'conductor_breaker_events is append-only');
                END;

                CREATE TABLE IF NOT EXISTS conductor_safety_snapshot (
                    run_id TEXT NOT NULL,
                    issue_id TEXT NOT NULL,
                    signature TEXT NOT NULL,
                    observed_at_utc TEXT NOT NULL,
                    attempts INTEGER NOT NULL,
                    repair_cycles INTEGER NOT NULL,
                    run_seconds INTEGER NOT NULL,
                    issue_units INTEGER NOT NULL,
                    daily_units INTEGER NOT NULL,
                    stale_leases INTEGER NOT NULL,
                    orphaned_workers INTEGER NOT NULL,
                    safety_events INTEGER NOT NULL,
                    no_work_ticks INTEGER NOT NULL,
                    deterministic_failures INTEGER NOT NULL,
                    transient_failures INTEGER NOT NULL,
                    run_units_remaining INTEGER NOT NULL,
                    issue_units_remaining INTEGER NOT NULL,
                    daily_units_remaining INTEGER NOT NULL,
                    breaker_state TEXT NOT NULL CHECK (breaker_state IN ('closed', 'open')),
                    breaker_reason_code TEXT NOT NULL,
                    breaker_transition_sequence INTEGER NOT NULL,
                    PRIMARY KEY (run_id, issue_id, signature)
                );
                """
            )

    def bind_wake(
        self,
        wake_id: str,
        scope: SafetyScope,
        now: datetime,
    ) -> None:
        _validate_identifier("wake_id", wake_id)
        _require_scope(scope)
        now_text = _utc_text(now)
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT run_id, issue_id, signature
                FROM conductor_wake_scope WHERE wake_id = ?
                """,
                (wake_id,),
            ).fetchone()
            identity = (scope.run_id, scope.issue_id, scope.signature)
            if row is None:
                connection.execute(
                    """
                    INSERT INTO conductor_wake_scope
                        (wake_id, run_id, issue_id, signature, bound_at_utc)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (wake_id, *identity, now_text),
                )
            elif tuple(row) != identity:
                raise SafetyValidationError(
                    "wake is already bound to another safety scope"
                )

    def record_event(
        self,
        event_key: str,
        event_kind: SafetyEventKind,
        scope: SafetyScope,
        occurred_at: datetime,
        *,
        units: int = 1,
    ) -> None:
        _validate_identifier("event_key", event_key)
        if event_kind not in _EVENT_KINDS:
            raise SafetyValidationError("safety event kind is invalid")
        _require_scope(scope)
        if type(units) is not int or units <= 0:
            raise SafetyValidationError("safety event units must be a positive integer")
        occurred_at_text = _utc_text(occurred_at)
        with self._transaction() as connection:
            self._insert_event(
                connection,
                event_key,
                event_kind,
                scope,
                occurred_at_text,
                units,
            )

    def record_result_observation(
        self,
        fence_token: int,
        wake_id: str,
        scope: SafetyScope,
        observed_at: datetime,
        *,
        action_key: str,
        result_status: str,
        result_sha256: str,
        reason_code: str,
        run_units: int,
        issue_units: int,
        daily_units: int,
        event_key: str | None = None,
        event_kind: SafetyEventKind | None = None,
    ) -> None:
        """Pin actual result-observation time before terminal ledger mutation."""
        if type(fence_token) is not int or fence_token <= 0:
            raise SafetyValidationError("fence_token must be a positive integer")
        _validate_identifier("wake_id", wake_id)
        _require_scope(scope)
        _validate_identifier("action_key", action_key)
        if result_status not in ("completed", "ambiguous"):
            raise SafetyValidationError("result_status is invalid")
        if not isinstance(result_sha256, str) or not _SHA256.fullmatch(
            result_sha256
        ):
            raise SafetyValidationError("result_sha256 is invalid")
        _validate_identifier("reason_code", reason_code)
        for name, units in (
            ("run_units", run_units),
            ("issue_units", issue_units),
            ("daily_units", daily_units),
        ):
            _validate_non_negative_int(name, units)
        if (event_key is None) != (event_kind is None):
            raise SafetyValidationError(
                "result observation safety event must be complete"
            )
        if event_key is not None:
            _validate_identifier("event_key", event_key)
        if event_kind is not None and event_kind not in _EVENT_KINDS:
            raise SafetyValidationError("safety event kind is invalid")
        observed_at_text = _utc_text(observed_at)
        with self._transaction() as connection:
            lease = connection.execute(
                """
                SELECT wake_id, fence_token FROM repo_lease WHERE singleton = 1
                """
            ).fetchone()
            if lease is None or tuple(lease) != (wake_id, fence_token):
                raise SafetyValidationError(
                    "result observation does not correlate to the current lease"
                )
            binding = connection.execute(
                """
                SELECT run_id, issue_id, signature
                FROM conductor_wake_scope WHERE wake_id = ?
                """,
                (wake_id,),
            ).fetchone()
            identity = (
                wake_id,
                *_scope_values(scope),
                run_units,
                issue_units,
                daily_units,
                action_key,
                result_status,
                result_sha256,
                reason_code,
            )
            if binding is None or tuple(binding) != _scope_values(scope):
                raise SafetyValidationError(
                    "result observation does not correlate to its safety scope"
                )
            row = connection.execute(
                """
                SELECT wake_id, run_id, issue_id, signature,
                       run_units, issue_units, daily_units, action_key,
                       result_status, result_sha256, reason_code
                FROM conductor_result_observations WHERE fence_token = ?
                """,
                (fence_token,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO conductor_result_observations
                        (fence_token, wake_id, run_id, issue_id, signature,
                         run_units, issue_units, daily_units, observed_at_utc,
                         action_key, result_status, result_sha256, reason_code)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        fence_token,
                        *identity[:7],
                        observed_at_text,
                        *identity[7:],
                    ),
                )
            elif tuple(row) != identity:
                raise SafetyValidationError(
                    "result observation conflicts with durable correlation"
                )
            if event_key is not None and event_kind is not None:
                self._insert_event(
                    connection,
                    event_key,
                    event_kind,
                    scope,
                    observed_at_text,
                    1,
                )

    def assess(
        self,
        scope: SafetyScope,
        now: datetime,
        limits: SafetyLimits,
    ) -> SafetyAssessment:
        _require_scope(scope)
        if not isinstance(limits, SafetyLimits):
            raise SafetyValidationError("limits must be SafetyLimits")
        now_text = _utc_text(now)
        with self._transaction() as connection:
            effective, started_at = self._bind_limits(
                connection, scope, now_text, limits
            )
            self._reconcile_result_observations(connection)
            self._reconcile_cost_usage(connection)
            self._reconcile_expired_lease(connection, scope, now_text)
            usage = self._read_usage(
                connection,
                scope,
                now,
                _parse_utc_text(started_at),
            )
            reason_code = _exhaustion_reason(usage, effective)
            breaker = self._read_or_create_breaker(connection, scope, now_text)
            if reason_code is not None and breaker.state == "closed":
                breaker = self._transition(
                    connection,
                    scope,
                    breaker,
                    "open",
                    reason_code,
                    now_text,
                )
            budget = _budget_view(usage, effective)
            allows_effect = (
                breaker.state == "closed"
                and min(
                    budget.run_units_remaining,
                    budget.issue_units_remaining,
                    budget.daily_units_remaining,
                )
                >= 1
            )
            self._write_snapshot(
                connection,
                scope,
                now_text,
                usage,
                budget,
                breaker,
            )
        return SafetyAssessment(
            scope,
            effective,
            usage,
            budget,
            breaker,
            allows_effect,
        )

    def reset_breaker(
        self,
        scope: SafetyScope,
        authorization_input: object,
        now: datetime,
    ) -> BreakerView:
        _require_scope(scope)
        now_text = _utc_text(now)
        with self._transaction() as connection:
            breaker = self._read_or_create_breaker(connection, scope, now_text)
            if breaker.state == "closed":
                raise BreakerAuthorizationError("breaker reset is not authorized")
            context = BreakerResetContext(
                scope,
                breaker.transition_sequence,
                breaker.reason_code,
                self._latest_safety_event_sequence(connection, scope),
            )
            authorized = (
                self._reset_authorizer is not None
                and self._reset_authorizer.authorize(
                    authorization_input,
                    context,
                )
            )
            if not authorized:
                raise BreakerAuthorizationError("breaker reset is not authorized")
            return self._transition(
                connection,
                scope,
                breaker,
                "closed",
                "authorized-reset",
                now_text,
            )

    @staticmethod
    def _latest_safety_event_sequence(
        connection: sqlite3.Connection,
        scope: SafetyScope,
    ) -> int:
        row = connection.execute(
            """
            SELECT COALESCE(MAX(event_sequence), 0)
            FROM conductor_safety_events
            WHERE
                (run_id = ? AND issue_id = ? AND signature = ?)
                OR (
                    issue_id = ? AND signature = ?
                    AND event_kind IN (
                        'attempt', 'deterministic-failure', 'transient-failure'
                    )
                )
            """,
            (
                *_scope_values(scope),
                scope.issue_id,
                scope.signature,
            ),
        ).fetchone()
        return int(row[0])

    def current_breaker(self, scope: SafetyScope) -> BreakerView:
        _require_scope(scope)
        with self._connect() as connection:
            connection.execute("PRAGMA query_only = ON")
            row = connection.execute(
                """
                SELECT state, reason_code, transition_sequence, updated_at_utc
                FROM conductor_breaker_current
                WHERE run_id = ? AND issue_id = ? AND signature = ?
                """,
                _scope_values(scope),
            ).fetchone()
        if row is None:
            raise SafetyValidationError("breaker has not been assessed")
        return BreakerView(row[0], row[1], row[2], row[3])

    def breaker_transitions(self, scope: SafetyScope) -> tuple[BreakerTransition, ...]:
        _require_scope(scope)
        with self._connect() as connection:
            connection.execute("PRAGMA query_only = ON")
            rows = connection.execute(
                """
                SELECT transition_sequence, from_state, to_state,
                       reason_code, occurred_at_utc
                FROM conductor_breaker_events
                WHERE run_id = ? AND issue_id = ? AND signature = ?
                ORDER BY transition_sequence
                """,
                _scope_values(scope),
            ).fetchall()
        return tuple(
            BreakerTransition(row[0], scope, row[1], row[2], row[3], row[4])
            for row in rows
        )

    def usage_snapshot(self, scope: SafetyScope) -> SafetyUsage:
        _require_scope(scope)
        with self._connect() as connection:
            connection.execute("PRAGMA query_only = ON")
            row = connection.execute(
                """
                SELECT attempts, repair_cycles, run_seconds, issue_units,
                       daily_units, stale_leases, orphaned_workers, safety_events,
                       no_work_ticks, deterministic_failures, transient_failures
                FROM conductor_safety_snapshot
                WHERE run_id = ? AND issue_id = ? AND signature = ?
                """,
                _scope_values(scope),
            ).fetchone()
        if row is None:
            raise SafetyValidationError("safety usage has not been assessed")
        return SafetyUsage(*row)

    def load_limits(self, scope: SafetyScope) -> SafetyLimits:
        """Load the effective durable ceilings for a previously assessed scope."""
        _require_scope(scope)
        with self._connect() as connection:
            connection.execute("PRAGMA query_only = ON")
            row = connection.execute(
                """
                SELECT max_attempts_per_signature, max_repair_cycles,
                       max_run_seconds, max_issue_units, max_daily_units,
                       max_stale_leases, max_orphaned_workers,
                       max_safety_events, max_no_work_ticks
                FROM conductor_safety_limits
                WHERE run_id = ? AND issue_id = ? AND signature = ?
                """,
                _scope_values(scope),
            ).fetchone()
        if row is None:
            raise SafetyValidationError("safety limits have not been assessed")
        return SafetyLimits(*row)

    def _bind_limits(
        self,
        connection: sqlite3.Connection,
        scope: SafetyScope,
        now_text: str,
        requested: SafetyLimits,
    ) -> tuple[SafetyLimits, str]:
        row = connection.execute(
            """
            SELECT started_at_utc, last_observed_at_utc,
                   max_attempts_per_signature, max_repair_cycles,
                   max_run_seconds, max_issue_units, max_daily_units,
                   max_stale_leases, max_orphaned_workers,
                   max_safety_events, max_no_work_ticks
            FROM conductor_safety_limits
            WHERE run_id = ? AND issue_id = ? AND signature = ?
            """,
            _scope_values(scope),
        ).fetchone()
        requested_values = _limit_values(requested)
        if row is None:
            connection.execute(
                """
                INSERT INTO conductor_safety_limits
                    (run_id, issue_id, signature, started_at_utc,
                     last_observed_at_utc, max_attempts_per_signature,
                     max_repair_cycles, max_run_seconds, max_issue_units,
                     max_daily_units, max_stale_leases, max_orphaned_workers,
                     max_safety_events, max_no_work_ticks)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (*_scope_values(scope), now_text, now_text, *requested_values),
            )
            return requested, now_text
        if now_text < row[1]:
            raise SafetyValidationError("safety observation time cannot move backwards")
        stored_values = tuple(row[2:])
        if any(
            requested_value > stored
            for requested_value, stored in zip(
                requested_values, stored_values, strict=True
            )
        ):
            raise SafetyValidationError("a durable safety ceiling cannot be increased")
        effective_values = tuple(
            min(requested_value, stored)
            for requested_value, stored in zip(
                requested_values, stored_values, strict=True
            )
        )
        connection.execute(
            """
            UPDATE conductor_safety_limits SET
                last_observed_at_utc = ?,
                max_attempts_per_signature = ?, max_repair_cycles = ?,
                max_run_seconds = ?, max_issue_units = ?, max_daily_units = ?,
                max_stale_leases = ?, max_orphaned_workers = ?,
                max_safety_events = ?, max_no_work_ticks = ?
            WHERE run_id = ? AND issue_id = ? AND signature = ?
            """,
            (now_text, *effective_values, *_scope_values(scope)),
        )
        return SafetyLimits(*effective_values), row[0]

    def _reconcile_result_observations(
        self,
        connection: sqlite3.Connection,
    ) -> None:
        rows = connection.execute(
            """
            SELECT observation.fence_token, observation.wake_id,
                   observation.run_id, observation.issue_id,
                   observation.signature, observation.run_units,
                   observation.issue_units, observation.daily_units,
                   observation.observed_at_utc,
                   binding.run_id, binding.issue_id, binding.signature
            FROM conductor_result_observations AS observation
            LEFT JOIN conductor_cost_usage AS reconciled
                ON reconciled.fence_token = observation.fence_token
            LEFT JOIN conductor_wake_scope AS binding
                ON binding.wake_id = observation.wake_id
            WHERE reconciled.fence_token IS NULL
            ORDER BY observation.fence_token
            """
        ).fetchall()
        for row in rows:
            if row[9] is None or tuple(row[9:12]) != tuple(row[2:5]):
                raise SafetyValidationError(
                    "durable result observation is not bound to its safety scope"
                )
            _parse_utc_text(row[8])
            connection.execute(
                """
                INSERT INTO conductor_cost_usage
                    (fence_token, wake_id, run_id, issue_id, signature,
                     run_units, issue_units, daily_units, recorded_at_utc)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(row[:9]),
            )

    def _reconcile_cost_usage(
        self,
        connection: sqlite3.Connection,
    ) -> None:
        rows = connection.execute(
            """
            SELECT usage.fence_token, usage.wake_id, usage.run_units,
                   usage.issue_units, usage.daily_units,
                   binding.run_id, binding.issue_id, binding.signature,
                   binding.bound_at_utc,
                   reconciled.wake_id, reconciled.run_id,
                   reconciled.issue_id, reconciled.signature,
                   reconciled.run_units, reconciled.issue_units,
                   reconciled.daily_units
            FROM budget_usage AS usage
            LEFT JOIN conductor_cost_usage AS reconciled
                ON reconciled.fence_token = usage.fence_token
            LEFT JOIN conductor_wake_scope AS binding
                ON binding.wake_id = usage.wake_id
            ORDER BY usage.fence_token
            """
        ).fetchall()
        for row in rows:
            if row[5] is None:
                raise SafetyValidationError(
                    "durable budget usage is not bound to a safety scope"
                )
            reconciled_scope = SafetyScope(row[5], row[6], row[7])
            expected_identity = (
                row[1],
                *_scope_values(reconciled_scope),
                row[2],
                row[3],
                row[4],
            )
            if row[9] is not None:
                if tuple(row[9:16]) != expected_identity:
                    raise SafetyValidationError(
                        "durable budget usage conflicts with result cost"
                    )
                continue
            has_cost = any(row[index] > 0 for index in (2, 3, 4))
            if has_cost:
                raise SafetyValidationError(
                    "durable budget usage has no result observation"
                )
            recorded_at = row[8]
            _parse_utc_text(recorded_at)
            connection.execute(
                """
                INSERT INTO conductor_cost_usage
                    (fence_token, wake_id, run_id, issue_id, signature,
                     run_units, issue_units, daily_units, recorded_at_utc)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row[0],
                    row[1],
                    *_scope_values(reconciled_scope),
                    row[2],
                    row[3],
                    row[4],
                    recorded_at,
                ),
            )

    def _reconcile_expired_lease(
        self,
        connection: sqlite3.Connection,
        scope: SafetyScope,
        now_text: str,
    ) -> None:
        row = connection.execute(
            """
            SELECT lease.wake_id, lease.fence_token,
                   binding.run_id, binding.issue_id, binding.signature
            FROM repo_lease AS lease
            LEFT JOIN conductor_wake_scope AS binding
                ON binding.wake_id = lease.wake_id
            WHERE lease.expires_at <= ?
            """,
            (now_text,),
        ).fetchone()
        if row is None:
            return
        event_scope = scope if row[2] is None else SafetyScope(row[2], row[3], row[4])
        for kind in ("stale-lease", "orphaned-worker"):
            digest = hashlib.sha256(
                f"{kind}:{row[0]}:{row[1]}".encode("utf-8")
            ).hexdigest()
            self._insert_event(
                connection,
                f"{kind}:{digest}",
                kind,
                event_scope,
                now_text,
                1,
            )

    def _read_usage(
        self,
        connection: sqlite3.Connection,
        scope: SafetyScope,
        now: datetime,
        started_at: datetime,
    ) -> SafetyUsage:
        def event_units(kind: str, *, signature_scoped: bool = False) -> int:
            if signature_scoped:
                row = connection.execute(
                    """
                    SELECT COALESCE(SUM(units), 0) FROM conductor_safety_events
                    WHERE issue_id = ? AND signature = ? AND event_kind = ?
                    """,
                    (scope.issue_id, scope.signature, kind),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT COALESCE(SUM(units), 0) FROM conductor_safety_events
                    WHERE run_id = ? AND issue_id = ? AND signature = ?
                          AND event_kind = ?
                    """,
                    (*_scope_values(scope), kind),
                ).fetchone()
            return int(row[0])

        issue_row = connection.execute(
            """
            SELECT COALESCE(SUM(issue_units), 0)
            FROM conductor_cost_usage WHERE issue_id = ?
            """,
            (scope.issue_id,),
        ).fetchone()
        day = _utc_text(now)[:10]
        daily_row = connection.execute(
            """
            SELECT COALESCE(SUM(daily_units), 0)
            FROM conductor_cost_usage WHERE substr(recorded_at_utc, 1, 10) = ?
            """,
            (day,),
        ).fetchone()
        run_seconds = max(0, int((now - started_at).total_seconds()))
        return SafetyUsage(
            attempts=event_units("attempt", signature_scoped=True),
            repair_cycles=event_units("repair-cycle"),
            run_seconds=run_seconds,
            issue_units=int(issue_row[0]),
            daily_units=int(daily_row[0]),
            stale_leases=event_units("stale-lease"),
            orphaned_workers=event_units("orphaned-worker"),
            safety_events=event_units("safety-violation"),
            no_work_ticks=event_units("no-work"),
            deterministic_failures=event_units(
                "deterministic-failure", signature_scoped=True
            ),
            transient_failures=event_units("transient-failure", signature_scoped=True),
        )

    def _read_or_create_breaker(
        self,
        connection: sqlite3.Connection,
        scope: SafetyScope,
        now_text: str,
    ) -> BreakerView:
        row = connection.execute(
            """
            SELECT state, reason_code, transition_sequence, updated_at_utc
            FROM conductor_breaker_current
            WHERE run_id = ? AND issue_id = ? AND signature = ?
            """,
            _scope_values(scope),
        ).fetchone()
        if row is None:
            connection.execute(
                """
                INSERT INTO conductor_breaker_current
                    (run_id, issue_id, signature, state, reason_code,
                     transition_sequence, updated_at_utc)
                VALUES (?, ?, ?, 'closed', 'ready', 0, ?)
                """,
                (*_scope_values(scope), now_text),
            )
            return BreakerView("closed", "ready", 0, now_text)
        return BreakerView(row[0], row[1], row[2], row[3])

    def _transition(
        self,
        connection: sqlite3.Connection,
        scope: SafetyScope,
        current: BreakerView,
        target: BreakerState,
        reason_code: str,
        now_text: str,
    ) -> BreakerView:
        _validate_identifier("reason_code", reason_code)
        sequence = current.transition_sequence + 1
        connection.execute(
            """
            INSERT INTO conductor_breaker_events
                (run_id, issue_id, signature, transition_sequence,
                 from_state, to_state, reason_code, occurred_at_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                *_scope_values(scope),
                sequence,
                current.state,
                target,
                reason_code,
                now_text,
            ),
        )
        connection.execute(
            """
            UPDATE conductor_breaker_current
            SET state = ?, reason_code = ?, transition_sequence = ?,
                updated_at_utc = ?
            WHERE run_id = ? AND issue_id = ? AND signature = ?
            """,
            (target, reason_code, sequence, now_text, *_scope_values(scope)),
        )
        return BreakerView(target, reason_code, sequence, now_text)

    def _write_snapshot(
        self,
        connection: sqlite3.Connection,
        scope: SafetyScope,
        now_text: str,
        usage: SafetyUsage,
        budget: BudgetView,
        breaker: BreakerView,
    ) -> None:
        connection.execute(
            """
            INSERT INTO conductor_safety_snapshot
                (run_id, issue_id, signature, observed_at_utc,
                 attempts, repair_cycles, run_seconds, issue_units, daily_units,
                 stale_leases, orphaned_workers, safety_events, no_work_ticks,
                 deterministic_failures, transient_failures,
                 run_units_remaining, issue_units_remaining, daily_units_remaining,
                 breaker_state, breaker_reason_code,
                 breaker_transition_sequence)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, issue_id, signature) DO UPDATE SET
                observed_at_utc = excluded.observed_at_utc,
                attempts = excluded.attempts,
                repair_cycles = excluded.repair_cycles,
                run_seconds = excluded.run_seconds,
                issue_units = excluded.issue_units,
                daily_units = excluded.daily_units,
                stale_leases = excluded.stale_leases,
                orphaned_workers = excluded.orphaned_workers,
                safety_events = excluded.safety_events,
                no_work_ticks = excluded.no_work_ticks,
                deterministic_failures = excluded.deterministic_failures,
                transient_failures = excluded.transient_failures,
                run_units_remaining = excluded.run_units_remaining,
                issue_units_remaining = excluded.issue_units_remaining,
                daily_units_remaining = excluded.daily_units_remaining,
                breaker_state = excluded.breaker_state,
                breaker_reason_code = excluded.breaker_reason_code,
                breaker_transition_sequence = excluded.breaker_transition_sequence
            """,
            (
                *_scope_values(scope),
                now_text,
                *tuple(getattr(usage, item.name) for item in fields(usage)),
                budget.run_units_remaining,
                budget.issue_units_remaining,
                budget.daily_units_remaining,
                breaker.state,
                breaker.reason_code,
                breaker.transition_sequence,
            ),
        )

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        event_key: str,
        event_kind: str,
        scope: SafetyScope,
        occurred_at_text: str,
        units: int,
    ) -> None:
        row = connection.execute(
            """
            SELECT event_kind, run_id, issue_id, signature, units
            FROM conductor_safety_events WHERE event_key = ?
            """,
            (event_key,),
        ).fetchone()
        identity = (event_kind, *_scope_values(scope), units)
        if row is None:
            connection.execute(
                """
                INSERT INTO conductor_safety_events
                    (event_key, event_kind, run_id, issue_id, signature,
                     units, occurred_at_utc)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (event_key, *identity, occurred_at_text),
            )
        elif tuple(row) != identity:
            raise SafetyValidationError(
                "safety event key has conflicting durable intent"
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path, timeout=5.0, isolation_level=None
        )
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


def _exhaustion_reason(usage: SafetyUsage, limits: SafetyLimits) -> str | None:
    if usage.deterministic_failures:
        return "deterministic-failure"
    gates = (
        (usage.safety_events, limits.max_safety_events, "safety-budget-exhausted"),
        (usage.stale_leases, limits.max_stale_leases, "stale-lease-budget-exhausted"),
        (
            usage.orphaned_workers,
            limits.max_orphaned_workers,
            "orphaned-worker-budget-exhausted",
        ),
        (
            usage.attempts,
            limits.max_attempts_per_signature,
            "attempt-budget-exhausted",
        ),
        (usage.repair_cycles, limits.max_repair_cycles, "repair-budget-exhausted"),
        (usage.run_seconds, limits.max_run_seconds, "run-time-budget-exhausted"),
        (usage.issue_units, limits.max_issue_units, "issue-cost-budget-exhausted"),
        (usage.daily_units, limits.max_daily_units, "daily-cost-budget-exhausted"),
        (usage.no_work_ticks, limits.max_no_work_ticks, "no-work-budget-exhausted"),
    )
    return next((reason for used, ceiling, reason in gates if used >= ceiling), None)


def _budget_view(usage: SafetyUsage, limits: SafetyLimits) -> BudgetView:
    run_headroom = (
        limits.max_attempts_per_signature - usage.attempts,
        limits.max_repair_cycles - usage.repair_cycles,
        limits.max_run_seconds - usage.run_seconds,
        limits.max_stale_leases - usage.stale_leases,
        limits.max_orphaned_workers - usage.orphaned_workers,
        limits.max_safety_events - usage.safety_events,
        limits.max_no_work_ticks - usage.no_work_ticks,
    )
    if usage.deterministic_failures:
        run_headroom = (*run_headroom, 0)
    return BudgetView(
        max(0, min(run_headroom)),
        max(0, limits.max_issue_units - usage.issue_units),
        max(0, limits.max_daily_units - usage.daily_units),
    )


def _limit_values(limits: SafetyLimits) -> tuple[int, ...]:
    return tuple(getattr(limits, item.name) for item in fields(limits))


def _scope_values(scope: SafetyScope) -> tuple[str, str, str]:
    return (scope.run_id, scope.issue_id, scope.signature)


def _require_scope(scope: object) -> SafetyScope:
    if not isinstance(scope, SafetyScope):
        raise SafetyValidationError("scope must be a SafetyScope")
    return scope


def _validate_identifier(name: str, value: object) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise SafetyValidationError(f"{name} must be a sanitized identifier")


def _validate_non_negative_int(name: str, value: object) -> None:
    if type(value) is not int or value < 0:
        raise SafetyValidationError(f"{name} must be a non-negative integer")


def _utc_text(value: datetime) -> str:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != timedelta(0)
    ):
        raise SafetyValidationError("time must be an aware UTC datetime")
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _parse_utc_text(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise SafetyValidationError("time must be a UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise SafetyValidationError("time must be a valid UTC timestamp") from error
    if parsed.utcoffset() != timedelta(0):
        raise SafetyValidationError("time must be UTC")
    return parsed


def _bounded_json(value: object) -> str:
    try:
        message = json.dumps(
            value,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise SafetyValidationError("value is not closed JSON") from error
    if len(message.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise SafetyValidationError("JSON message exceeds 1 MiB")
    return message


def _parse_bounded_object(message: str) -> dict[str, Any]:
    if not isinstance(message, str):
        raise SafetyValidationError("JSON message must be a string")
    if len(message.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise SafetyValidationError("JSON message exceeds 1 MiB")
    try:
        value = json.loads(message, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as error:
        raise SafetyValidationError("JSON message is invalid") from error
    if not isinstance(value, dict):
        raise SafetyValidationError("JSON message must be an object")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise SafetyValidationError("JSON message contains a duplicate key")
        value[key] = item
    return value


def _require_exact_keys(value: dict[str, Any], expected: set[str], name: str) -> None:
    if set(value) != expected:
        raise SafetyValidationError(f"{name} must use the closed schema")


def _required_string(value: dict[str, Any], field: str) -> str:
    item = value.get(field)
    if not isinstance(item, str):
        raise SafetyValidationError(f"{field} must be a string")
    return item
