"""Two-process CLI composition for one model-mediated conductor effect."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from os import environ
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import threading
from types import MappingProxyType
from typing import Any, Callable

from .adapter import (
    BoundedJsonLinesExchange,
    JsonLinesPolicyAdapter,
    PolicyAdapterPort,
)
from .contracts import (
    MAX_MESSAGE_BYTES,
    AdapterDecision,
    AdapterRequest,
    BudgetView,
    CheckpointView,
    ProposedAction,
    ReminderSpec,
    Wake,
)
from .ledger import ControlLedger, EffectResult, Lease
from .identifiers import is_safe_identifier
from .projection import publish_current_projection
from .safety import (
    SafetyController,
    SafetyEventKind,
    SafetyLimits,
    SafetyPolicy,
    SafetyPolicyRequest,
    SafetyScope,
    parse_safety_policy_json,
    serialize_safety_policy_request,
)
from .tick import (
    DeliveryConductorTick,
    TickHandoff,
    TickResult,
    is_runtime_decision_safe,
)
from .wakes import normalize_wake


_AGENT_WORKSPACE = Path("/home/developer")
_DATABASE_RELATIVE_PATH = Path("data/delivery-conductor/control.sqlite3")
_ADAPTER_FILENAME = "adapter.py"
_ADAPTER_DEADLINE_SECONDS = 5.0
_LEASE_SECONDS = 300
_RUNTIME_TRIGGER_SOURCES = MappingProxyType(
    {
        "manual": "direct",
        "chat": "direct",
        "schedule": "schedule",
        "reminder": "reminder",
    }
)
_WORKER_EVENT_TYPES = frozenset(
    {"agent.task.completed", "agent.task.failed"}
)
_WAKE_DIGEST_DOMAIN = b"delivery-conductor-wake-v1"

CAPABILITY_TOOL_MAP = MappingProxyType(
    {
        "chat": "mcp__trinity__chat_with_agent",
        "reminders": "mcp__trinity__set_reminder",
    }
)


class CliValidationError(ValueError):
    """Raised for closed-envelope, workspace, adapter, or state validation."""


class CliCorrelationError(CliValidationError):
    """Raised before mutation when record-result does not match durable state."""


@dataclass(frozen=True)
class PrepareInput:
    wake: Wake


@dataclass(frozen=True)
class RecordResultInput:
    action_key: str
    fence_token: int
    result: EffectResult


CliInput = PrepareInput | RecordResultInput


def parse_cli_input(message: str) -> CliInput:
    """Parse one strict JSON line for exactly prepare or record-result."""
    value = _parse_one_json_line(message)
    operation = value.get("operation")
    if operation == "prepare":
        _require_keys(
            value,
            {"schema_version", "operation", "wake"},
            "prepare input",
        )
        _require_schema_version(value)
        wake_value = value["wake"]
        if not isinstance(wake_value, dict):
            raise CliValidationError("wake must use the closed schema")
        _require_keys(
            wake_value,
            {"source", "source_event_id", "payload_sha256"},
            "wake input",
        )
        try:
            wake = normalize_wake(
                _required_string(wake_value, "source"),
                _required_string(wake_value, "source_event_id"),
                _required_string(wake_value, "payload_sha256"),
            )
        except ValueError as error:
            raise CliValidationError("prepare input is invalid") from error
        return PrepareInput(wake)
    if operation == "record-result":
        _require_keys(
            value,
            {
                "schema_version",
                "operation",
                "action_key",
                "fence_token",
                "status",
                "result_sha256",
                "reason_code",
            },
            "record-result input",
        )
        _require_schema_version(value)
        action_key = _required_string(value, "action_key")
        _identifier("action_key", action_key)
        fence_token = value["fence_token"]
        if type(fence_token) is not int or fence_token <= 0:
            raise CliValidationError("fence_token must be a positive integer")
        reason_code = _required_string(value, "reason_code")
        _identifier("reason_code", reason_code)
        try:
            result = EffectResult(
                _required_string(value, "status"),  # type: ignore[arg-type]
                _required_string(value, "result_sha256"),
                reason_code,
            )
        except ValueError as error:
            raise CliValidationError("record-result input is invalid") from error
        return RecordResultInput(action_key, fence_token, result)
    raise CliValidationError("operation is not supported")


def build_runtime_prepare_message(
    provenance_message: str,
    runtime_execution_id: str,
) -> str:
    """Derive one internal wake from the model-visible trusted runtime context.

    The launcher boundary intentionally accepts provenance rather than a caller-
    selected wake digest. This is a consistency check inside the template's
    explicit model-mediated trust boundary, not cryptographic authentication:
    the model reads Trinity's trusted system-prompt context and must not replace
    its inherited environment. The current execution ID must match that env.
    Worker-completion uses the distinct backend-generated event ID so separate
    terminal events from one worker do not collapse into one wake.
    """
    value = _parse_one_json_line(provenance_message)
    _require_keys(
        value,
        {
            "schema_version",
            "triggered_by",
            "execution_id",
            "event_type",
            "event_id",
        },
        "runtime provenance",
    )
    _require_schema_version(value)
    _identifier("runtime_execution_id", runtime_execution_id)
    execution_id = _required_string(value, "execution_id")
    _identifier("execution_id", execution_id)
    if execution_id != runtime_execution_id:
        raise CliValidationError("runtime provenance does not match the execution")
    triggered_by = _required_string(value, "triggered_by")

    event_type = value["event_type"]
    event_id = value["event_id"]
    if triggered_by == "event":
        if event_type not in _WORKER_EVENT_TYPES:
            raise CliValidationError("runtime event type is not supported")
        event_id = _identifier("event_id", event_id)
        source = "worker-completion"
        source_event_id = event_id
    else:
        source = _RUNTIME_TRIGGER_SOURCES.get(triggered_by)
        if source is None:
            raise CliValidationError("runtime trigger is not supported")
        if event_type is not None or event_id is not None:
            raise CliValidationError("non-event provenance cannot carry event fields")
        source_event_id = execution_id

    payload_sha256 = hashlib.sha256(
        b"\0".join(
            (
                _WAKE_DIGEST_DOMAIN,
                source.encode("ascii"),
                source_event_id.encode("ascii"),
                triggered_by.encode("ascii"),
                (event_type or "").encode("ascii"),
            )
        )
    ).hexdigest()
    return json.dumps(
        {
            "schema_version": 1,
            "operation": "prepare",
            "wake": {
                "source": source,
                "source_event_id": source_event_id,
                "payload_sha256": payload_sha256,
            },
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )


def guard_agent_workspace(
    workspace: str | Path,
    *,
    allowed_root: str | Path = _AGENT_WORKSPACE,
) -> Path:
    """Resolve exactly the fixed physical agent workspace root."""
    candidate = Path(workspace)
    root = Path(allowed_root)
    if (
        not candidate.exists()
        or not candidate.is_dir()
        or candidate.is_symlink()
        or not root.exists()
        or not root.is_dir()
        or root.is_symlink()
    ):
        raise CliValidationError("conductor CLI must run in the agent workspace")
    resolved = candidate.resolve()
    resolved_root = root.resolve()
    if resolved != resolved_root:
        raise CliValidationError("conductor CLI must run in the agent workspace")
    return resolved


def resolve_effect_tool(capability_name: str) -> str:
    """Resolve a capability only through the fixed closed tool map."""
    tool_name = CAPABILITY_TOOL_MAP.get(capability_name)
    if tool_name is None:
        raise CliValidationError("capability is not installed by the template")
    return tool_name


def run_cli(
    message: str,
    workspace: str | Path,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, object]:
    """Run one local phase and return one sanitized, closed output object."""
    parsed = parse_cli_input(message)
    workspace_path = _validate_local_workspace(Path(workspace))
    database_path = _database_path(workspace_path)
    ledger = ControlLedger(database_path)
    ledger.initialize()
    safety = SafetyController(database_path)
    safety.initialize()
    _initialize_cli_state(database_path)
    now = clock()
    _require_aware_utc(now)
    if isinstance(parsed, PrepareInput):
        return _prepare(workspace_path, ledger, safety, parsed, now)
    return _record_result(workspace_path, ledger, safety, parsed, now)


def _prepare(
    workspace: Path,
    ledger: ControlLedger,
    safety: SafetyController,
    parsed: PrepareInput,
    now: datetime,
) -> dict[str, object]:
    _recover_observed_result(
        workspace,
        ledger,
        safety,
        now,
    )
    adapter_path = _fixed_adapter_path(workspace)
    adapter = _FixedWorkspaceAdapter(workspace, adapter_path)
    policy = adapter.safety_policy(parsed.wake, now)
    safety.bind_wake(parsed.wake.wake_id, policy.scope, now)
    assessment = safety.assess(policy.scope, now, policy.limits)
    checkpoint = _load_checkpoint(ledger.database_path)
    tick = DeliveryConductorTick(
        ledger=ledger,
        adapter=adapter,
        installed_capabilities=frozenset(CAPABILITY_TOOL_MAP),
        lease_seconds=_LEASE_SECONDS,
    )
    result = tick.run(
        parsed.wake,
        now,
        checkpoint,
        assessment.budget_view,
        breaker_allows_effect=assessment.allows_effect,
        post_claim_gate=lambda _lease: _fresh_effect_gate(
            safety,
            policy.scope,
            now,
            policy.limits,
        ),
        before_noop_release=lambda _lease: _record_no_work(
            safety,
            parsed.wake.wake_id,
            policy.scope,
            now,
            policy.limits,
        ),
    )
    if result.handoff is not None:
        _store_handoff(ledger.database_path, result.handoff, policy.scope, now)
        if result.handoff.kind == "action":
            safety.record_event(
                _safety_event_key(
                    "attempt",
                    result.handoff.action.action_key,
                    result.handoff.lease.fence_token,
                ),
                "attempt",
                policy.scope,
                now,
            )
            if result.handoff.action.invalidation_class == "repair-cycle":
                safety.record_event(
                    _safety_event_key(
                        "repair-cycle",
                        result.handoff.action.action_key,
                        result.handoff.lease.fence_token,
                    ),
                    "repair-cycle",
                    policy.scope,
                    now,
                )
    elif result.status == "noop":
        safety.assess(policy.scope, now, policy.limits)
    publish_current_projection(workspace, ledger.database_path, now)
    return _prepare_output(result)


def _fresh_effect_gate(
    safety: SafetyController,
    scope: SafetyScope,
    now: datetime,
    limits: SafetyLimits,
) -> tuple[BudgetView, bool]:
    assessment = safety.assess(scope, now, limits)
    return assessment.budget_view, assessment.allows_effect


def _record_no_work(
    safety: SafetyController,
    wake_id: str,
    scope: SafetyScope,
    now: datetime,
    limits: SafetyLimits,
) -> None:
    safety.record_event(
        _safety_event_key("no-work", wake_id, 0),
        "no-work",
        scope,
        now,
    )
    safety.assess(scope, now, limits)


def _recover_observed_result(
    workspace: Path,
    ledger: ControlLedger,
    safety: SafetyController,
    now: datetime,
) -> None:
    with sqlite3.connect(ledger.database_path) as connection:
        rows = connection.execute(
            """
            SELECT observation.action_key, observation.fence_token,
                   observation.result_status, observation.result_sha256,
                   observation.reason_code
            FROM conductor_result_observations AS observation
            LEFT JOIN conductor_cli_receipts AS receipt
              ON receipt.action_key = observation.action_key
             AND receipt.fence_token = observation.fence_token
            WHERE receipt.action_key IS NULL
            ORDER BY observation.fence_token
            """
        ).fetchall()
    if not rows:
        return
    if len(rows) != 1:
        raise CliCorrelationError("observed results do not have one recovery owner")
    row = rows[0]
    try:
        recovered = RecordResultInput(
            row[0],
            row[1],
            EffectResult(row[2], row[3], row[4]),
        )
    except (TypeError, ValueError) as error:
        raise CliCorrelationError("observed result recovery is invalid") from error
    _record_result(workspace, ledger, safety, recovered, now)


def _record_result(
    workspace: Path,
    ledger: ControlLedger,
    safety: SafetyController,
    parsed: RecordResultInput,
    now: datetime,
) -> dict[str, object]:
    receipt = _load_receipt(ledger.database_path, parsed)
    if receipt is not None:
        publish_current_projection(workspace, ledger.database_path, now)
        return receipt
    try:
        handoff, scope = _reconstruct_handoff(ledger.database_path, parsed)
    except CliCorrelationError as live_error:
        terminal = _load_terminal_result(ledger.database_path, parsed)
        if terminal is None:
            raise live_error
        output, scope = terminal
    else:
        event_kind = _result_safety_event_kind(parsed)
        safety.record_result_observation(
            handoff.lease.fence_token,
            handoff.lease.wake_id,
            scope,
            now,
            action_key=parsed.action_key,
            result_status=parsed.result.status,
            result_sha256=parsed.result.result_sha256,
            reason_code=parsed.result.reason_code,
            run_units=1,
            issue_units=1,
            daily_units=1,
            event_key=None
            if event_kind is None
            else _safety_event_key(
                event_kind,
                parsed.action_key,
                parsed.fence_token,
            ),
            event_kind=event_kind,
        )
        tick = DeliveryConductorTick(
            ledger=ledger,
            adapter=_NeverPolicyAdapter(),
            installed_capabilities=frozenset(CAPABILITY_TOOL_MAP),
            lease_seconds=_LEASE_SECONDS,
        )
        result = tick.accept_result(
            handoff,
            action_key=parsed.action_key,
            result=parsed.result,
        )
        output = _record_output(result, handoff.lease.fence_token)
    _record_result_safety(safety, scope, parsed, now)
    safety.assess(scope, now, safety.load_limits(scope))
    _store_receipt_and_clear_handoff(ledger.database_path, parsed, output)
    publish_current_projection(workspace, ledger.database_path, now)
    return output


class _FixedWorkspaceAdapter(PolicyAdapterPort):
    def __init__(self, workspace: Path, adapter_path: Path) -> None:
        self._workspace = workspace
        self._adapter_path = adapter_path
        self._decision_adapter = JsonLinesPolicyAdapter(self._new_exchange)

    def safety_policy(self, wake: Wake, now: datetime) -> SafetyPolicy:
        request = SafetyPolicyRequest(1, wake, _utc_seconds(now))
        exchange = self._new_exchange()
        try:
            response = exchange.exchange(serialize_safety_policy_request(request))
            return parse_safety_policy_json(response)
        except Exception as error:
            raise CliValidationError(
                "fixed adapter safety policy is unavailable"
            ) from error

    def observe_and_decide(self, request: AdapterRequest) -> AdapterDecision:
        decision = self._decision_adapter.observe_and_decide(request)
        if not _is_cli_adapter_decision_safe(decision):
            raise CliValidationError("fixed adapter decision is invalid")
        return decision

    def _new_exchange(self) -> _ProcessJsonLinesExchange:
        return _ProcessJsonLinesExchange(
            self._workspace,
            self._adapter_path,
            deadline_seconds=_ADAPTER_DEADLINE_SECONDS,
        )


class _ProcessJsonLinesExchange:
    """Fresh one-shot exchange around the fixed interpreter and adapter file."""

    def __init__(
        self,
        workspace: Path,
        adapter_path: Path,
        *,
        deadline_seconds: float,
    ) -> None:
        self._process = subprocess.Popen(
            [sys.executable, str(adapter_path)],
            cwd=workspace,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={
                "PYTHONIOENCODING": "utf-8",
                "PYTHONUNBUFFERED": "1",
            },
            bufsize=0,
            close_fds=True,
        )
        if self._process.stdout is None or self._process.stdin is None:
            self._process.kill()
            raise CliValidationError("fixed adapter pipes are unavailable")
        self._inner = BoundedJsonLinesExchange(
            self._process.stdout,
            self._process.stdin,
            deadline_seconds=deadline_seconds,
        )
        self._reaped = threading.Event()
        self._release_complete = threading.Event()
        threading.Thread(target=self._join_release, daemon=True).start()

    @property
    def channel_identity(self) -> tuple[object, object]:
        return self._inner.channel_identity

    @property
    def cleanup_complete(self) -> threading.Event:
        return self._inner.cleanup_complete

    @property
    def release_complete(self) -> threading.Event:
        return self._release_complete

    def reject(self, *, preserve: tuple[object, ...] = ()) -> None:
        try:
            self._inner.reject(preserve=preserve)
        finally:
            self._reap()

    def exchange(self, request_line: str) -> str:
        try:
            return self._inner.exchange(request_line)
        finally:
            self._reap()

    def _reap(self) -> None:
        if self._reaped.is_set():
            return
        try:
            self._process.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            self._process.terminate()
            try:
                self._process.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=0.2)
        finally:
            self._reaped.set()

    def _join_release(self) -> None:
        self._inner.release_complete.wait()
        self._reaped.wait()
        self._release_complete.set()


class _NeverPolicyAdapter:
    def observe_and_decide(self, request: AdapterRequest) -> AdapterDecision:
        raise RuntimeError("record-result cannot observe policy")


def _is_cli_adapter_decision_safe(decision: AdapterDecision) -> bool:
    if not is_runtime_decision_safe(decision):
        return False
    if decision.next_reminder is not None:
        try:
            due_at = _parse_utc(decision.next_reminder.due_at_utc)
        except CliValidationError:
            return False
        if decision.next_reminder.due_at_utc not in {
            _utc_seconds(due_at),
            _utc_text(due_at),
        }:
            return False
    if decision.decision != "execute":
        return True
    action = decision.proposed_action
    if action is None or action.capability_name != "chat":
        return False
    try:
        payload = json.loads(action.payload_json)
    except json.JSONDecodeError:
        return False
    if not (
        isinstance(payload, dict)
        and set(payload) in ({"identifier"}, {"identifier", "references"})
        and payload.get("identifier") == decision.target_id
        and is_safe_identifier(payload.get("identifier"))
    ):
        return False
    message_limit = 3400 if decision.next_reminder is not None else 4000
    return len(_canonical_effect_message(action, payload)) <= message_limit


def _initialize_cli_state(database_path: Path) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS conductor_cli_handoff (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                kind TEXT NOT NULL CHECK (kind IN ('action', 'reminder')),
                wake_id TEXT NOT NULL,
                fence_token INTEGER NOT NULL CHECK (fence_token > 0),
                observed_revision TEXT NOT NULL,
                run_units_remaining INTEGER NOT NULL CHECK (run_units_remaining >= 0),
                issue_units_remaining INTEGER NOT NULL CHECK (issue_units_remaining >= 0),
                daily_units_remaining INTEGER NOT NULL CHECK (daily_units_remaining >= 0),
                action_key TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                reminder_id TEXT,
                reminder_due_at_utc TEXT,
                reminder_reason_code TEXT,
                run_id TEXT NOT NULL,
                issue_id TEXT NOT NULL,
                signature TEXT NOT NULL,
                prepared_at_utc TEXT NOT NULL,
                CHECK (
                    (reminder_id IS NULL AND reminder_due_at_utc IS NULL
                     AND reminder_reason_code IS NULL)
                    OR
                    (reminder_id IS NOT NULL AND reminder_due_at_utc IS NOT NULL
                     AND reminder_reason_code IS NOT NULL)
                )
            );

            CREATE TABLE IF NOT EXISTS conductor_cli_receipts (
                action_key TEXT NOT NULL,
                fence_token INTEGER NOT NULL CHECK (fence_token > 0),
                tick_status TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                action_status TEXT NOT NULL,
                result_sha256 TEXT NOT NULL,
                reminder_id TEXT,
                reminder_due_at_utc TEXT,
                reminder_reason_code TEXT,
                PRIMARY KEY (action_key, fence_token)
            );

            CREATE TRIGGER IF NOT EXISTS conductor_cli_receipts_reject_update
            BEFORE UPDATE ON conductor_cli_receipts
            BEGIN
                SELECT RAISE(ABORT, 'conductor_cli_receipts is append-only');
            END;

            CREATE TRIGGER IF NOT EXISTS conductor_cli_receipts_reject_delete
            BEFORE DELETE ON conductor_cli_receipts
            BEGIN
                SELECT RAISE(ABORT, 'conductor_cli_receipts is append-only');
            END;
            """
        )


def _store_handoff(
    database_path: Path,
    handoff: TickHandoff,
    scope: SafetyScope,
    now: datetime,
) -> None:
    reminder = handoff.reminder
    values = (
        handoff.kind,
        handoff.lease.wake_id,
        handoff.lease.fence_token,
        handoff.observed_revision,
        handoff.budget_view.run_units_remaining,
        handoff.budget_view.issue_units_remaining,
        handoff.budget_view.daily_units_remaining,
        handoff.action.action_key,
        handoff.payload_sha256,
        None if reminder is None else reminder.reminder_id,
        None if reminder is None else reminder.due_at_utc,
        None if reminder is None else reminder.reason_code,
        scope.run_id,
        scope.issue_id,
        scope.signature,
        _utc_text(now),
    )
    with sqlite3.connect(database_path, isolation_level=None) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT fence_token, action_key FROM conductor_cli_handoff WHERE singleton = 1"
        ).fetchone()
        if (
            row is not None
            and row[0] >= handoff.lease.fence_token
            and tuple(row)
            != (
                handoff.lease.fence_token,
                handoff.action.action_key,
            )
        ):
            raise CliCorrelationError("prepared handoff does not correlate")
        connection.execute(
            """
            INSERT INTO conductor_cli_handoff
                (singleton, kind, wake_id, fence_token, observed_revision,
                 run_units_remaining, issue_units_remaining, daily_units_remaining,
                 action_key, payload_sha256, reminder_id, reminder_due_at_utc,
                 reminder_reason_code, run_id, issue_id, signature, prepared_at_utc)
            VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(singleton) DO UPDATE SET
                kind = excluded.kind,
                wake_id = excluded.wake_id,
                fence_token = excluded.fence_token,
                observed_revision = excluded.observed_revision,
                run_units_remaining = excluded.run_units_remaining,
                issue_units_remaining = excluded.issue_units_remaining,
                daily_units_remaining = excluded.daily_units_remaining,
                action_key = excluded.action_key,
                payload_sha256 = excluded.payload_sha256,
                reminder_id = excluded.reminder_id,
                reminder_due_at_utc = excluded.reminder_due_at_utc,
                reminder_reason_code = excluded.reminder_reason_code,
                run_id = excluded.run_id,
                issue_id = excluded.issue_id,
                signature = excluded.signature,
                prepared_at_utc = excluded.prepared_at_utc
            """,
            values,
        )
        connection.commit()


def _reconstruct_handoff(
    database_path: Path,
    parsed: RecordResultInput,
) -> tuple[TickHandoff, SafetyScope]:
    with sqlite3.connect(database_path) as connection:
        handoff = connection.execute(
            """
            SELECT kind, wake_id, fence_token, observed_revision,
                   run_units_remaining, issue_units_remaining,
                   daily_units_remaining, action_key, payload_sha256,
                   reminder_id, reminder_due_at_utc, reminder_reason_code,
                   run_id, issue_id, signature
            FROM conductor_cli_handoff WHERE singleton = 1
            """
        ).fetchone()
        if handoff is None or (handoff[7], handoff[2]) != (
            parsed.action_key,
            parsed.fence_token,
        ):
            raise CliCorrelationError(
                "record-result does not correlate to a prepared handoff"
            )
        lease_row = connection.execute(
            """
            SELECT wake_id, fence_token, claimed_at, expires_at
            FROM repo_lease WHERE singleton = 1
            """
        ).fetchone()
        action_row = connection.execute(
            """
            SELECT capability_name, action_key, payload_json, target_revision,
                   invalidation_class, status, wake_id, fence_token,
                   result_sha256, reason_code
            FROM action_journal WHERE action_key = ?
            """,
            (parsed.action_key,),
        ).fetchone()
        checkpoint = connection.execute(
            """
            SELECT revision, fence_token, run_units_remaining,
                   issue_units_remaining, daily_units_remaining,
                   action_key, action_status, action_result_sha256,
                   acknowledged_wake_id
            FROM run_checkpoint WHERE singleton = 1
            """
        ).fetchone()
        pending_row = None
        if checkpoint is not None and checkpoint[5] != parsed.action_key:
            pending_row = connection.execute(
                """
                SELECT capability_name, status, wake_id, fence_token
                FROM action_journal WHERE action_key = ?
                """,
                (checkpoint[5],),
            ).fetchone()
    if lease_row is None or action_row is None or checkpoint is None:
        raise CliCorrelationError("record-result does not correlate to durable state")
    if tuple(lease_row[:2]) != (handoff[1], handoff[2]):
        raise CliCorrelationError(
            "record-result does not correlate to the current lease"
        )
    if (action_row[6], action_row[7]) != (handoff[1], handoff[2]):
        raise CliCorrelationError("record-result does not correlate to the reservation")
    if action_row[5] == "reserved":
        result_identity = ("reserved", None, None)
    else:
        result_identity = (
            parsed.result.status,
            parsed.result.result_sha256,
            parsed.result.reason_code,
        )
    if (action_row[5], action_row[8], action_row[9]) != result_identity:
        raise CliCorrelationError("record-result conflicts with the terminal action")
    initial_checkpoint = (
        handoff[3],
        handoff[2],
        handoff[4],
        handoff[5],
        handoff[6],
        handoff[7],
        "reserved",
        None,
        handoff[1],
    )
    spent_budget = tuple(max(0, value - 1) for value in handoff[4:7])
    checkpoint_identity_matches = (
        checkpoint[0] == handoff[3]
        and checkpoint[1] == handoff[2]
        and checkpoint[8] == handoff[1]
    )
    terminal_checkpoint_matches = (
        checkpoint_identity_matches
        and tuple(checkpoint[2:5]) == spent_budget
        and checkpoint[5] == handoff[7]
        and (checkpoint[6], checkpoint[7])
        == (parsed.result.status, parsed.result.result_sha256)
    )
    pending_checkpoint_matches = (
        checkpoint_identity_matches
        and tuple(checkpoint[2:5]) == spent_budget
        and checkpoint[5] != handoff[7]
        and (checkpoint[6], checkpoint[7]) == ("reserved", None)
        and (handoff[9] is not None or parsed.result.status == "ambiguous")
        and tuple(pending_row or ())
        == ("reminders", "reserved", handoff[1], handoff[2])
    )
    if action_row[5] == "reserved" and tuple(checkpoint) != initial_checkpoint:
        raise CliCorrelationError("record-result does not correlate to the checkpoint")
    if action_row[5] != "reserved" and not (
        tuple(checkpoint) == initial_checkpoint
        or terminal_checkpoint_matches
        or pending_checkpoint_matches
    ):
        raise CliCorrelationError("record-result does not correlate to the checkpoint")
    action = ProposedAction(*action_row[:5])
    payload_sha256 = hashlib.sha256(action.payload_json.encode("utf-8")).hexdigest()
    if payload_sha256 != handoff[8]:
        raise CliCorrelationError(
            "record-result does not correlate to the action digest"
        )
    lease = Lease(
        lease_row[0],
        lease_row[1],
        _parse_utc(lease_row[2]),
        _parse_utc(lease_row[3]),
    )
    reminder = None
    if handoff[9] is not None:
        reminder = ReminderSpec(handoff[9], handoff[10], handoff[11])
    scope = SafetyScope(handoff[12], handoff[13], handoff[14])
    return (
        TickHandoff(
            handoff[0],
            lease,
            handoff[3],
            BudgetView(handoff[4], handoff[5], handoff[6]),
            action,
            handoff[8],
            reminder,
        ),
        scope,
    )


def _load_receipt(
    database_path: Path,
    parsed: RecordResultInput,
) -> dict[str, object] | None:
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            """
            SELECT tick_status, reason_code, action_status, result_sha256,
                   reminder_id, reminder_due_at_utc, reminder_reason_code
            FROM conductor_cli_receipts
            WHERE action_key = ? AND fence_token = ?
            """,
            (parsed.action_key, parsed.fence_token),
        ).fetchone()
    if row is None:
        return None
    if (row[2], row[3], row[1]) != (
        parsed.result.status,
        parsed.result.result_sha256,
        parsed.result.reason_code,
    ):
        raise CliCorrelationError("record-result conflicts with the durable receipt")
    return _record_output_values(
        row[0],
        row[1],
        parsed.action_key,
        parsed.fence_token,
        row[2],
        row[3],
        row[4],
        row[5],
        row[6],
    )


def _load_terminal_result(
    database_path: Path,
    parsed: RecordResultInput,
) -> tuple[dict[str, object], SafetyScope] | None:
    with sqlite3.connect(database_path) as connection:
        action_row = connection.execute(
            """
            SELECT capability_name, action_key, payload_json, target_revision,
                   invalidation_class, status, wake_id, fence_token,
                   result_sha256, reason_code
            FROM action_journal WHERE action_key = ?
            """,
            (parsed.action_key,),
        ).fetchone()
        handoff = connection.execute(
            """
            SELECT kind, wake_id, fence_token, observed_revision,
                   run_units_remaining, issue_units_remaining,
                   daily_units_remaining, action_key, payload_sha256,
                   reminder_id, reminder_due_at_utc, reminder_reason_code,
                   run_id, issue_id, signature
            FROM conductor_cli_handoff WHERE singleton = 1
            """
        ).fetchone()
        lease = connection.execute(
            "SELECT wake_id, fence_token FROM repo_lease WHERE singleton = 1"
        ).fetchone()
        controller = connection.execute(
            """
            SELECT last_fence_token, state, reason_code
            FROM controller_state WHERE singleton = 1
            """
        ).fetchone()
        checkpoint = connection.execute(
            """
            SELECT revision, fence_token, run_units_remaining,
                   issue_units_remaining, daily_units_remaining,
                   action_key, action_status, action_result_sha256,
                   acknowledged_wake_id
            FROM run_checkpoint WHERE singleton = 1
            """
        ).fetchone()
        usage = connection.execute(
            """
            SELECT wake_id, run_units, issue_units, daily_units, reason_code
            FROM budget_usage WHERE fence_token = ?
            """,
            (parsed.fence_token,),
        ).fetchone()
        observation = connection.execute(
            """
            SELECT wake_id, run_id, issue_id, signature,
                   run_units, issue_units, daily_units, action_key,
                   result_status, result_sha256, reason_code
            FROM conductor_result_observations WHERE fence_token = ?
            """,
            (parsed.fence_token,),
        ).fetchone()
        pending_row = None
        if checkpoint is not None and checkpoint[5] != parsed.action_key:
            pending_row = connection.execute(
                """
                SELECT capability_name, action_key, payload_json,
                       target_revision, invalidation_class, status,
                       wake_id, fence_token, payload_sha256
                FROM action_journal WHERE action_key = ?
                """,
                (checkpoint[5],),
            ).fetchone()
    if action_row is None or action_row[5] == "reserved":
        return None
    if handoff is None or (handoff[7], handoff[2]) != (
        parsed.action_key,
        parsed.fence_token,
    ):
        raise CliCorrelationError(
            "record-result does not correlate to a prepared handoff"
        )
    if (
        action_row[5],
        action_row[8],
        action_row[9],
        action_row[6],
        action_row[7],
    ) != (
        parsed.result.status,
        parsed.result.result_sha256,
        parsed.result.reason_code,
        handoff[1],
        parsed.fence_token,
    ):
        raise CliCorrelationError("record-result conflicts with the terminal action")
    action = ProposedAction(*action_row[:5])
    if hashlib.sha256(action.payload_json.encode("utf-8")).hexdigest() != handoff[8]:
        raise CliCorrelationError(
            "record-result does not correlate to the action digest"
        )
    scope = SafetyScope(handoff[12], handoff[13], handoff[14])
    expected_reason = (
        "reminder-pending"
        if handoff[0] == "action"
        and parsed.result.status == "completed"
        and handoff[9] is not None
        else parsed.result.reason_code
    )
    spent_budget = tuple(max(0, value - 1) for value in handoff[4:7])
    state_matches = (
        lease is None
        and tuple(controller or ()) == (parsed.fence_token, "idle", expected_reason)
        and checkpoint is not None
        and checkpoint[0] == handoff[3]
        and checkpoint[1] == parsed.fence_token
        and tuple(checkpoint[2:5]) == spent_budget
        and checkpoint[8] == handoff[1]
        and tuple(usage or ()) == (handoff[1], 1, 1, 1, expected_reason)
        and tuple(observation or ())
        == (
            handoff[1],
            scope.run_id,
            scope.issue_id,
            scope.signature,
            1,
            1,
            1,
            parsed.action_key,
            parsed.result.status,
            parsed.result.result_sha256,
            parsed.result.reason_code,
        )
    )
    if not state_matches:
        raise CliCorrelationError("terminal result is not fully committed")
    reminder = None
    if handoff[9] is not None:
        reminder = ReminderSpec(handoff[9], handoff[10], handoff[11])
    if checkpoint[5] == parsed.action_key:
        if (checkpoint[6], checkpoint[7]) != (
            parsed.result.status,
            parsed.result.result_sha256,
        ):
            raise CliCorrelationError("terminal checkpoint does not correlate")
    else:
        recovered_reminder = _pending_reminder(
            pending_row,
            handoff,
            parsed,
        )
        if reminder is not None and recovered_reminder != reminder:
            raise CliCorrelationError("terminal reminder does not correlate")
        reminder = recovered_reminder
    tick_status = "completed"
    output_reason = parsed.result.reason_code
    if handoff[0] == "reminder" and parsed.result.status == "completed":
        tick_status = "reminder"
    elif parsed.result.status == "ambiguous":
        tick_status = "investigate"
    elif reminder is not None:
        tick_status = "reminder"
        output_reason = "reminder-pending"
    output = _record_output_values(
        tick_status,
        output_reason,
        parsed.action_key,
        parsed.fence_token,
        parsed.result.status,
        parsed.result.result_sha256,
        None if reminder is None else reminder.reminder_id,
        None if reminder is None else reminder.due_at_utc,
        None if reminder is None else reminder.reason_code,
    )
    return output, scope


def _pending_reminder(
    row: tuple[object, ...] | None,
    handoff: tuple[object, ...],
    parsed: RecordResultInput,
) -> ReminderSpec:
    try:
        if row is None or (row[5], row[6], row[7]) != (
            "reserved",
            handoff[1],
            handoff[2],
        ):
            raise ValueError("pending reminder state is absent")
        action = ProposedAction(*row[:5])  # type: ignore[arg-type]
        if (
            action.capability_name != "reminders"
            or action.target_revision != handoff[3]
            or action.invalidation_class != "reminder-intent"
            or hashlib.sha256(action.payload_json.encode("utf-8")).hexdigest() != row[8]
        ):
            raise ValueError("pending reminder intent is invalid")
        payload = json.loads(action.payload_json)
        if not isinstance(payload, dict) or set(payload) != {"digest", "references"}:
            raise ValueError("pending reminder payload is invalid")
        references = payload["references"]
        if not isinstance(references, dict) or set(references) != {
            "identifiers",
            "reason_code",
            "utc_timestamp",
        }:
            raise ValueError("pending reminder references are invalid")
        identifiers = references["identifiers"]
        if (
            not isinstance(identifiers, list)
            or len(identifiers) != 2
            or identifiers[1] != parsed.action_key
        ):
            raise ValueError("pending reminder correlation is invalid")
        reminder = ReminderSpec(
            identifiers[0],
            references["utc_timestamp"],
            references["reason_code"],
        )
        reminder_wire = {
            "due_at_utc": reminder.due_at_utc,
            "reason_code": reminder.reason_code,
            "reminder_id": reminder.reminder_id,
        }
        reminder_digest = hashlib.sha256(
            json.dumps(
                reminder_wire,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        expected_action_key = (
            "reminder-"
            + hashlib.sha256(reminder.reminder_id.encode("utf-8")).hexdigest()
        )
        if (
            payload["digest"] != reminder_digest
            or action.action_key != expected_action_key
        ):
            raise ValueError("pending reminder digest is invalid")
        return reminder
    except (TypeError, ValueError) as error:
        raise CliCorrelationError("terminal reminder does not correlate") from error


def _store_receipt_and_clear_handoff(
    database_path: Path,
    parsed: RecordResultInput,
    output: dict[str, object],
) -> None:
    reminder = output["reminder"]
    if reminder is not None and not isinstance(reminder, dict):
        raise CliValidationError("record output reminder is invalid")
    with sqlite3.connect(database_path, isolation_level=None) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT OR IGNORE INTO conductor_cli_receipts
                (action_key, fence_token, tick_status, reason_code,
                 action_status, result_sha256, reminder_id,
                 reminder_due_at_utc, reminder_reason_code)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                parsed.action_key,
                parsed.fence_token,
                output["status"],
                output["reason_code"],
                output["action_status"],
                output["result_sha256"],
                None if reminder is None else reminder["reminder_id"],
                None if reminder is None else reminder["due_at_utc"],
                None if reminder is None else reminder["reason_code"],
            ),
        )
        connection.execute(
            """
            DELETE FROM conductor_cli_handoff
            WHERE singleton = 1 AND action_key = ? AND fence_token = ?
            """,
            (parsed.action_key, parsed.fence_token),
        )
        connection.commit()


def _record_result_safety(
    safety: SafetyController,
    scope: SafetyScope,
    parsed: RecordResultInput,
    now: datetime,
) -> None:
    event_kind = _result_safety_event_kind(parsed)
    if event_kind is None:
        return
    safety.record_event(
        _safety_event_key(event_kind, parsed.action_key, parsed.fence_token),
        event_kind,
        scope,
        now,
    )


def _result_safety_event_kind(parsed: RecordResultInput) -> SafetyEventKind | None:
    return {
        "deterministic-failure": "deterministic-failure",
        "transient-failure": "transient-failure",
        "safety-violation": "safety-violation",
        "orphaned-worker": "orphaned-worker",
    }.get(parsed.result.reason_code)


def _load_checkpoint(database_path: Path) -> CheckpointView | None:
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            """
            SELECT revision, checkpoint_sha256, fence_token,
                   acknowledged_wake_id
            FROM run_checkpoint WHERE singleton = 1
            """
        ).fetchone()
    return CheckpointView(*row) if row is not None else None


def _prepare_output(result: TickResult) -> dict[str, object]:
    action = result.action
    handoff = result.handoff
    reminder = result.reminder
    effect_tool = None
    effect_arguments = None
    if action is not None:
        if handoff is None or handoff.action != action:
            raise CliValidationError("prepared action is missing its durable handoff")
        effect_tool = resolve_effect_tool(action.capability_name)
        effect_arguments = _effect_arguments(action, handoff, reminder)
    return {
        "schema_version": 1,
        "operation": "prepare",
        "status": result.status,
        "reason_code": result.reason_code,
        "effect_tool": effect_tool,
        "effect_arguments": effect_arguments,
        "action": None
        if action is None
        else {
            "capability_name": action.capability_name,
            "action_key": action.action_key,
            "payload_sha256": handoff.payload_sha256,
            "target_revision": action.target_revision,
            "invalidation_class": action.invalidation_class,
        },
        "correlation": None
        if handoff is None
        else {
            "action_key": handoff.action.action_key,
            "fence_token": handoff.lease.fence_token,
        },
        "reminder": _reminder_output(reminder),
    }


def _effect_arguments(
    action: ProposedAction,
    handoff: TickHandoff,
    reminder: ReminderSpec | None,
) -> dict[str, str]:
    try:
        references = json.loads(action.payload_json)
    except json.JSONDecodeError as error:
        raise CliValidationError("prepared action payload is invalid") from error
    if not isinstance(references, dict):
        raise CliValidationError("prepared action payload is invalid")
    payload_sha256 = hashlib.sha256(action.payload_json.encode("utf-8")).hexdigest()
    if payload_sha256 != handoff.payload_sha256:
        raise CliValidationError("prepared action digest does not match its handoff")
    message = _canonical_effect_message(action, references)
    if not 1 <= len(message) <= 4000:
        raise CliValidationError("prepared effect message is outside tool bounds")

    if action.capability_name == "chat":
        if set(references) not in ({"identifier"}, {"identifier", "references"}):
            raise CliValidationError("chat action payload must use the closed schema")
        agent_name = _identifier("agent_name", references.get("identifier"))
        arguments = {"agent_name": agent_name, "message": message}
    elif action.capability_name == "reminders":
        if set(references) != {"digest", "references"}:
            raise CliValidationError("reminder action payload must use the closed schema")
        if reminder is None or handoff.reminder != reminder:
            raise CliValidationError("reminder action is missing its durable due time")
        _parse_utc(reminder.due_at_utc)
        arguments = {"message": message, "fire_at": reminder.due_at_utc}
    else:
        raise CliValidationError("prepared effect capability is not supported")
    _validate_effect_arguments(action, payload_sha256, references, arguments)
    return arguments


def _canonical_effect_message(
    action: ProposedAction,
    references: dict[str, object],
) -> str:
    return json.dumps(
        {
            "action_key": action.action_key,
            "payload_sha256": hashlib.sha256(
                action.payload_json.encode("utf-8")
            ).hexdigest(),
            "references": references,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )


def _validate_effect_arguments(
    action: ProposedAction,
    payload_sha256: str,
    references: dict[str, object],
    arguments: dict[str, str],
) -> None:
    expected_keys = {
        "chat": {"agent_name", "message"},
        "reminders": {"message", "fire_at"},
    }.get(action.capability_name)
    if expected_keys is None or set(arguments) != expected_keys:
        raise CliValidationError("effect arguments do not use the closed tool schema")
    message = arguments.get("message")
    if not isinstance(message, str) or not 1 <= len(message) <= 4000:
        raise CliValidationError("effect arguments contain an invalid message")
    try:
        message_value = json.loads(message, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as error:
        raise CliValidationError("effect message is invalid") from error
    if not isinstance(message_value, dict) or set(message_value) != {
        "action_key",
        "payload_sha256",
        "references",
    }:
        raise CliValidationError("effect message must use the closed schema")
    if message_value != {
        "action_key": action.action_key,
        "payload_sha256": payload_sha256,
        "references": references,
    }:
        raise CliValidationError("effect message does not match durable action state")
    if action.capability_name == "chat":
        _identifier("agent_name", arguments.get("agent_name"))
    else:
        _parse_utc(arguments.get("fire_at", ""))


def _record_output(result: TickResult, fence_token: int) -> dict[str, object]:
    if (
        result.action_key is None
        or result.action_status is None
        or result.result_sha256 is None
    ):
        raise CliValidationError("accepted result is missing its sanitized outcome")
    reminder = result.reminder
    return _record_output_values(
        result.status,
        result.reason_code,
        result.action_key,
        fence_token,
        result.action_status,
        result.result_sha256,
        None if reminder is None else reminder.reminder_id,
        None if reminder is None else reminder.due_at_utc,
        None if reminder is None else reminder.reason_code,
    )


def _record_output_values(
    status: str,
    reason_code: str,
    action_key: str,
    fence_token: int,
    action_status: str,
    result_sha256: str,
    reminder_id: str | None,
    reminder_due_at_utc: str | None,
    reminder_reason_code: str | None,
) -> dict[str, object]:
    reminder = None
    if reminder_id is not None:
        reminder = {
            "reminder_id": reminder_id,
            "due_at_utc": reminder_due_at_utc,
            "reason_code": reminder_reason_code,
        }
    return {
        "schema_version": 1,
        "operation": "record-result",
        "status": status,
        "reason_code": reason_code,
        "action_key": action_key,
        "fence_token": fence_token,
        "action_status": action_status,
        "result_sha256": result_sha256,
        "reminder": reminder,
    }


def _reminder_output(reminder: ReminderSpec | None) -> dict[str, object] | None:
    if reminder is None:
        return None
    return {
        "reminder_id": reminder.reminder_id,
        "due_at_utc": reminder.due_at_utc,
        "reason_code": reminder.reason_code,
    }


def _fixed_adapter_path(workspace: Path) -> Path:
    candidate = workspace / _ADAPTER_FILENAME
    try:
        metadata = candidate.lstat()
    except OSError as error:
        raise CliValidationError("fixed adapter entrypoint is unavailable") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise CliValidationError("fixed adapter entrypoint is unavailable")
    resolved = candidate.resolve()
    if resolved.parent != workspace or resolved.name != _ADAPTER_FILENAME:
        raise CliValidationError("fixed adapter entrypoint is unavailable")
    return resolved


def _database_path(workspace: Path) -> Path:
    current = workspace
    for part in _DATABASE_RELATIVE_PATH.parent.parts:
        current = current / part
        if current.exists() and current.is_symlink():
            raise CliValidationError("durable state path is invalid")
        current.mkdir(mode=0o700, exist_ok=True)
    database_path = workspace / _DATABASE_RELATIVE_PATH
    if database_path.exists() and database_path.is_symlink():
        raise CliValidationError("durable state path is invalid")
    return database_path


def _validate_local_workspace(workspace: Path) -> Path:
    if not workspace.exists() or not workspace.is_dir() or workspace.is_symlink():
        raise CliValidationError("workspace is invalid")
    return workspace.resolve()


def _safety_event_key(kind: str, identity: str, fence_token: int) -> str:
    digest = hashlib.sha256(
        f"{kind}:{identity}:{fence_token}".encode("utf-8")
    ).hexdigest()
    return f"{kind}-{digest}"


def _parse_one_json_line(message: str) -> dict[str, Any]:
    if not isinstance(message, str):
        raise CliValidationError("CLI input must be a string")
    if len(message.encode("utf-8")) > MAX_MESSAGE_BYTES + 1:
        raise CliValidationError("CLI input exceeds 1 MiB")
    if message.endswith("\n"):
        message = message[:-1]
    if "\n" in message or "\r" in message:
        raise CliValidationError("CLI input must be one JSON line")
    if len(message.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise CliValidationError("CLI input exceeds 1 MiB")
    try:
        value = json.loads(message, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as error:
        raise CliValidationError("CLI input is invalid JSON") from error
    if not isinstance(value, dict):
        raise CliValidationError("CLI input must be an object")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise CliValidationError("CLI input contains a duplicate key")
        value[key] = item
    return value


def _require_keys(value: dict[str, Any], expected: set[str], name: str) -> None:
    if set(value) != expected:
        raise CliValidationError(f"{name} must use the closed schema")


def _require_schema_version(value: dict[str, Any]) -> None:
    if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise CliValidationError("schema_version must be 1")


def _required_string(value: dict[str, Any], field: str) -> str:
    item = value.get(field)
    if not isinstance(item, str):
        raise CliValidationError(f"{field} must be a string")
    return item


def _identifier(name: str, value: object) -> str:
    if not is_safe_identifier(value):
        raise CliValidationError(f"{name} must be a sanitized identifier")
    return value


def _parse_utc(value: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise CliValidationError("time must be UTC and end in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise CliValidationError("time must be a valid UTC timestamp") from error
    _require_aware_utc(parsed)
    return parsed


def _require_aware_utc(value: datetime) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != timedelta(0)
    ):
        raise CliValidationError("time must be aware UTC")


def _utc_text(value: datetime) -> str:
    _require_aware_utc(value)
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _utc_seconds(value: datetime) -> str:
    _require_aware_utc(value)
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _serialize_output(value: dict[str, object]) -> str:
    message = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )
    if len(message.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise CliValidationError("CLI output exceeds 1 MiB")
    return message


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments not in ([], ["record-result"]):
        sys.stdout.write(
            _serialize_output(
                {
                    "schema_version": 1,
                    "status": "rejected",
                    "reason_code": "invalid-command",
                }
            )
            + "\n"
        )
        return 64
    message = sys.stdin.buffer.read(MAX_MESSAGE_BYTES + 2)
    try:
        decoded = message.decode("utf-8")
        if arguments == []:
            decoded = build_runtime_prepare_message(
                decoded,
                environ.get("TRINITY_EXECUTION_ID", ""),
            )
        parsed = parse_cli_input(decoded)
        if arguments == ["record-result"] and not isinstance(parsed, RecordResultInput):
            raise CliValidationError("command does not match the input operation")
        if arguments == [] and not isinstance(parsed, PrepareInput):
            raise CliValidationError("command does not match the input operation")
        workspace = guard_agent_workspace(Path.cwd())
        output = run_cli(decoded, workspace)
        sys.stdout.write(_serialize_output(output) + "\n")
        return 0
    except (UnicodeDecodeError, CliValidationError, ValueError):
        sys.stdout.write(
            _serialize_output(
                {
                    "schema_version": 1,
                    "status": "rejected",
                    "reason_code": "invalid-input",
                }
            )
            + "\n"
        )
        return 65
    except Exception:
        sys.stdout.write(
            _serialize_output(
                {
                    "schema_version": 1,
                    "status": "blocked",
                    "reason_code": "runtime-unavailable",
                }
            )
            + "\n"
        )
        return 70


if __name__ == "__main__":
    raise SystemExit(main())
