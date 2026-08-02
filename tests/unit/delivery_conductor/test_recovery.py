"""Recovery, safety, projection, and CLI tests for the delivery conductor."""

# ruff: noqa: E402
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
import sqlite3
import sys
import threading

import pytest

TEMPLATE_LIB = (
    Path(__file__).resolve().parents[3]
    / "config"
    / "agent-templates"
    / "delivery-conductor"
    / "lib"
)
sys.dont_write_bytecode = True
sys.path.insert(0, str(TEMPLATE_LIB))

from delivery_conductor.contracts import ProposedAction, ReminderSpec, Wake
import delivery_conductor.cli as cli_module
from delivery_conductor.cli import (
    CliCorrelationError,
    CliValidationError,
    build_runtime_prepare_message,
    guard_agent_workspace,
    parse_cli_input,
    resolve_effect_tool,
    run_cli,
)
from delivery_conductor.ledger import ControlLedger, TickOutcome
import delivery_conductor.projection as projection_module
from delivery_conductor.projection import ProjectionError, publish_current_projection
from delivery_conductor.safety import (
    BreakerAuthorizationError,
    BreakerResetAuthorizer,
    BreakerResetContext,
    SafetyController,
    SafetyLimits,
    SafetyPolicyRequest,
    SafetyScope,
    SafetyValidationError,
    parse_safety_policy_json,
    serialize_safety_policy_request,
)
from delivery_conductor.wakes import normalize_wake
import delivery_conductor.tick as tick_module


NOW = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)


def _limits(**overrides: int) -> SafetyLimits:
    values = {
        "max_attempts_per_signature": 10,
        "max_repair_cycles": 10,
        "max_run_seconds": 600,
        "max_issue_units": 10,
        "max_daily_units": 10,
        "max_stale_leases": 10,
        "max_orphaned_workers": 10,
        "max_safety_events": 10,
        "max_no_work_ticks": 10,
    }
    values.update(overrides)
    return SafetyLimits(**values)


def _scope(number: int = 1) -> SafetyScope:
    return SafetyScope(f"run-{number}", f"issue-{number}", f"signature-{number}")


def _bound_reset_authorizer(
    authorization_input: str,
    scope: SafetyScope,
    *,
    transition_sequence: int = 1,
    reason_code: str = "deterministic-failure",
    safety_event_sequence: int = 1,
) -> BreakerResetAuthorizer:
    expected_input = authorization_input.encode("utf-8")
    expected_context = BreakerResetContext(
        scope,
        transition_sequence,
        reason_code,
        safety_event_sequence,
    )

    def verify(candidate: object, context: BreakerResetContext) -> bool:
        candidate_bytes = (
            candidate.encode("utf-8") if isinstance(candidate, str) else b""
        )
        credential_matches = hmac.compare_digest(candidate_bytes, expected_input)
        return credential_matches and context == expected_context

    return BreakerResetAuthorizer(verify)


def _safety_controller(
    tmp_path: Path,
    *,
    authorizer: BreakerResetAuthorizer | None = None,
) -> tuple[ControlLedger, SafetyController]:
    ledger = ControlLedger(tmp_path / "control.sqlite3")
    ledger.initialize()
    safety = SafetyController(ledger.database_path, reset_authorizer=authorizer)
    safety.initialize()
    return ledger, safety


def _prepare_message(
    source: str,
    source_event_id: str,
) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "operation": "prepare",
            "wake": {
                "source": source,
                "source_event_id": source_event_id,
                "payload_sha256": "f" * 64,
            },
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def _runtime_provenance(
    triggered_by: str,
    execution_id: str,
    *,
    event_type: str | None = None,
    event_id: str | None = None,
    reminder_message: str | None = None,
) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "triggered_by": triggered_by,
            "execution_id": execution_id,
            "event_type": event_type,
            "event_id": event_id,
            "reminder_message": reminder_message,
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def _provenance_digest(
    source: str,
    source_event_id: str,
    triggered_by: str,
    event_type: str | None,
) -> str:
    payload = b"\0".join(
        (
            b"delivery-conductor-wake-v1",
            source.encode("ascii"),
            source_event_id.encode("ascii"),
            triggered_by.encode("ascii"),
            (event_type or "").encode("ascii"),
        )
    )
    return hashlib.sha256(payload).hexdigest()


def _record_message(
    prepared: dict[str, object],
    *,
    status: str = "completed",
    reason_code: str = "completed",
    result_sha256: str = "9" * 64,
    fence_token: int | None = None,
    action_key: str | None = None,
) -> str:
    correlation = prepared["correlation"]
    assert isinstance(correlation, dict)
    return json.dumps(
        {
            "schema_version": 1,
            "operation": "record-result",
            "action_key": action_key or correlation["action_key"],
            "fence_token": fence_token or correlation["fence_token"],
            "status": status,
            "result_sha256": result_sha256,
            "reason_code": reason_code,
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def _write_fixed_adapter(
    workspace: Path,
    *,
    limits: SafetyLimits | None = None,
    decision: str = "execute",
    reminder: bool = False,
    invalidation_class: str = "delivery-intent",
    run_id: str = "run-1",
    action_key: str | None = None,
    target_id: str = "target-1",
    payload_identifier: str = "target-1",
    capability_name: str = "chat",
    payload_references: object | None = None,
) -> Path:
    workspace.mkdir(parents=True, exist_ok=True)
    effective = limits or _limits()
    ceilings = {
        item.name: getattr(effective, item.name)
        for item in effective.__dataclass_fields__.values()
    }
    policy = {
        "schema_version": 1,
        "kind": "safety-policy",
        "run_id": run_id,
        "issue_id": "issue-1",
        "signature": "signature-1",
        "ceilings": ceilings,
    }
    action_payload: dict[str, object] = {"identifier": payload_identifier}
    if payload_references is not None:
        action_payload["references"] = payload_references
    source = f"""from __future__ import annotations
import json
import sys

request = json.loads(sys.stdin.readline())
if request.get("kind") == "safety-policy":
    response = {policy!r}
else:
    wake = request["wake"]
    event_id = wake["source_event_id"]
    execute = {decision!r} == "execute"
    response = {{
        "schema_version": 1,
        "observed_revision": "revision-1",
        "decision": "execute" if execute else "noop",
        "reason_code": "work-ready" if execute else "no-work",
        "target_id": {target_id!r} if execute else None,
        "proposed_action": {{
            "capability_name": {capability_name!r},
            "action_key": {action_key!r} or "action-" + event_id,
            "payload": {action_payload!r},
            "target_revision": "revision-1",
            "invalidation_class": {invalidation_class!r},
        }} if execute else None,
        "next_reminder": {{
            "reminder_id": "reminder-" + event_id,
            "due_at_utc": "2026-08-02T12:30:00Z",
            "reason_code": "follow-up",
        }} if execute and {reminder!r} else None,
    }}
sys.stdout.write(json.dumps(response, separators=(",", ":"), sort_keys=True) + "\\n")
sys.stdout.flush()
"""
    adapter = workspace / "adapter.py"
    adapter.write_text(source)
    return adapter


def _write_due_reconciliation_adapter(workspace: Path) -> Path:
    workspace.mkdir(parents=True, exist_ok=True)
    ceilings = {
        item.name: getattr(_limits(), item.name)
        for item in _limits().__dataclass_fields__.values()
    }
    source = f"""from __future__ import annotations
import json
import sys

request = json.loads(sys.stdin.readline())
wake = request["wake"]
source = wake["source"]
if request.get("kind") == "safety-policy":
    response = {{
        "schema_version": 1,
        "kind": "safety-policy",
        "run_id": "run-" + source,
        "issue_id": "issue-1",
        "signature": "signature-1",
        "ceilings": {ceilings!r},
    }}
else:
    due = source == "reminder"
    response = {{
        "schema_version": 1,
        "observed_revision": "revision-1",
        "decision": "execute",
        "reason_code": "due-work" if due else "source-work",
        "target_id": "target-1",
        "proposed_action": {{
            "capability_name": "chat",
            "action_key": "action-due" if due else "action-source",
            "payload": {{"identifier": "target-1"}},
            "target_revision": "revision-1",
            "invalidation_class": "delivery-intent",
        }},
        "next_reminder": None if due else {{
            "reminder_id": "reminder-source",
            "due_at_utc": "2026-08-02T12:01:00Z",
            "reason_code": "follow-up",
        }},
    }}
sys.stdout.write(json.dumps(response, separators=(",", ":"), sort_keys=True) + "\\n")
sys.stdout.flush()
"""
    adapter = workspace / "adapter.py"
    adapter.write_text(source)
    return adapter


@pytest.mark.parametrize(
    ("source", "canonical", "expected_wake_id"),
    (
        (
            "direct",
            "direct",
            "1f3b783511f95718fe8dcaa873276e436a828bfcfc78ef8beea614d1e3b805c4",
        ),
        (
            "schedule",
            "schedule",
            "93aa2db511e03cb498bd8610e02e0baa12c339e679ee98a415d27073be27d4a4",
        ),
        (
            "reminder",
            "reminder",
            "391e6b2b7d5e756792d1e430c6aae9d9d369bb2ddec38bab29aabac22c5ba098",
        ),
        (
            "worker-completion",
            "worker-completion",
            "1f902831d3112b90c8b8ec727b5731d94579dbeb5f8043d8a622c4bc4f03a65b",
        ),
    ),
)
def test_source_kinds_normalize_to_stable_wake_identity(
    source: str,
    canonical: str,
    expected_wake_id: str,
):
    """Each canonical source produces one stable identity for its source event."""
    wake = normalize_wake(
        source,
        "evt-1",
        "a" * 64,
    )

    assert wake.source == canonical
    assert wake.source_event_id == "evt-1"
    assert wake.wake_id == expected_wake_id
    assert wake.payload_sha256 == "a" * 64


def test_wake_source_is_part_of_the_deduplication_identity():
    """A schedule and completion carrying one event id must remain distinct wakes."""
    schedule = normalize_wake("schedule", "shared-1", "a" * 64)
    completion = normalize_wake("worker-completion", "shared-1", "a" * 64)

    assert schedule.source == "schedule"
    assert completion.source == "worker-completion"
    assert schedule.wake_id != completion.wake_id


@pytest.mark.parametrize(
    "source",
    (
        "",
        "unknown",
        "../schedule",
        "manual",
        "scheduled",
        "hourly",
        "reminders",
        "event",
        "completion",
        "worker_completion",
        " Direct ",
        3,
        None,
    ),
)
def test_unknown_wake_sources_fail_closed_without_echoing_input(source: object):
    """Expanding the source allowlist accidentally must remain observable."""
    with pytest.raises(ValueError, match="wake source kind is not supported") as caught:
        normalize_wake(source, "evt-1", "a" * 64)  # type: ignore[arg-type]

    if str(source):
        assert str(source) not in str(caught.value)


def test_safety_policy_exchange_is_closed_and_carries_no_raw_wake_payload():
    """Adding ambient policy fields must fail before budget enforcement."""
    wake = normalize_wake("direct", "evt-1", "a" * 64)
    request = SafetyPolicyRequest(1, wake, "2026-08-02T12:00:00Z")

    request_wire = serialize_safety_policy_request(request)
    policy = parse_safety_policy_json(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "safety-policy",
                "run_id": "run-1",
                "issue_id": "issue-1",
                "signature": "signature-1",
                "ceilings": {
                    "max_attempts_per_signature": 2,
                    "max_repair_cycles": 3,
                    "max_run_seconds": 3600,
                    "max_issue_units": 20,
                    "max_daily_units": 50,
                    "max_stale_leases": 1,
                    "max_orphaned_workers": 1,
                    "max_safety_events": 1,
                    "max_no_work_ticks": 4,
                },
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )

    assert json.loads(request_wire) == {
        "kind": "safety-policy",
        "now_utc": "2026-08-02T12:00:00Z",
        "schema_version": 1,
        "wake": {
            "payload_sha256": "a" * 64,
            "source": "direct",
            "source_event_id": "evt-1",
            "wake_id": "1f3b783511f95718fe8dcaa873276e436a828bfcfc78ef8beea614d1e3b805c4",
        },
    }
    assert policy.scope == _scope()
    assert policy.limits.max_attempts_per_signature == 2
    assert "payload" not in request_wire.replace("payload_sha256", "")


@pytest.mark.parametrize(
    "mutation",
    (
        {"max_daily_units": None},
        {"max_daily_units": -1},
        {"max_daily_units": True},
        {"max_daily_units": "10"},
        {"unexpected_ceiling": 10},
    ),
)
def test_invalid_or_missing_required_ceilings_fail_closed(mutation: dict[str, object]):
    """A null or malformed ceiling must never become an unlimited budget."""
    ceilings: dict[str, object] = {
        "max_attempts_per_signature": 2,
        "max_repair_cycles": 3,
        "max_run_seconds": 3600,
        "max_issue_units": 20,
        "max_daily_units": 50,
        "max_stale_leases": 1,
        "max_orphaned_workers": 1,
        "max_safety_events": 1,
        "max_no_work_ticks": 4,
    }
    if mutation == {"max_daily_units": None}:
        ceilings.pop("max_daily_units")
    else:
        ceilings.update(mutation)
    message = json.dumps(
        {
            "schema_version": 1,
            "kind": "safety-policy",
            "run_id": "run-1",
            "issue_id": "issue-1",
            "signature": "signature-1",
            "ceilings": ceilings,
        },
        separators=(",", ":"),
        sort_keys=True,
    )

    with pytest.raises(SafetyValidationError):
        parse_safety_policy_json(message)


def test_duplicate_and_oversized_safety_responses_fail_closed():
    """A second key or over-cap response must not choose the effective ceiling."""
    with pytest.raises(SafetyValidationError, match="duplicate"):
        parse_safety_policy_json(
            '{"schema_version":1,"schema_version":1,"kind":"safety-policy"}'
        )
    with pytest.raises(SafetyValidationError, match="exceeds"):
        parse_safety_policy_json("{" + " " * (1024 * 1024) + "}")


def test_usage_is_reconciled_from_the_durable_ledger_across_restarts(tmp_path: Path):
    """Repeating a caller view must not erase already-recorded issue or daily usage."""
    ledger, safety = _safety_controller(tmp_path)
    scope = _scope()
    wake = normalize_wake("direct", "cost-1", "b" * 64)
    safety.bind_wake(wake.wake_id, scope, NOW)
    lease = ledger.claim_wake(wake, NOW, 60)
    assert lease is not None
    safety.record_result_observation(
        lease.fence_token,
        wake.wake_id,
        scope,
        NOW,
        action_key="action-cost-1",
        result_status="completed",
        result_sha256="7" * 64,
        reason_code="completed",
        run_units=1,
        issue_units=2,
        daily_units=3,
    )
    ledger.release(lease, TickOutcome(True, "completed", 1, 2, 3))

    first = safety.assess(scope, NOW + timedelta(seconds=1), _limits())
    restarted = SafetyController(ledger.database_path)
    restarted.initialize()
    second = restarted.assess(scope, NOW + timedelta(seconds=2), _limits())

    assert first.usage.issue_units == 2
    assert first.usage.daily_units == 3
    assert first.budget_view.issue_units_remaining == 8
    assert first.budget_view.daily_units_remaining == 7
    assert second.usage.issue_units == first.usage.issue_units
    assert second.usage.daily_units == first.usage.daily_units
    assert second.usage.run_seconds > first.usage.run_seconds
    assert second.budget_view.issue_units_remaining == 8
    assert second.budget_view.daily_units_remaining == 7


def test_cost_reconciliation_uses_durable_result_time_across_utc_midnight(
    tmp_path: Path,
):
    """A delayed restart must not move already-spent units into a later UTC day."""
    ledger, safety = _safety_controller(tmp_path)
    scope = _scope()
    dispatched_at = datetime(2026, 8, 2, 23, 59, 59, tzinfo=timezone.utc)
    wake = normalize_wake("direct", "midnight-cost", "b" * 64)
    safety.bind_wake(wake.wake_id, scope, dispatched_at)
    lease = ledger.claim_wake(wake, dispatched_at, 60)
    assert lease is not None
    safety.record_result_observation(
        lease.fence_token,
        wake.wake_id,
        scope,
        dispatched_at,
        action_key="action-midnight-cost",
        result_status="completed",
        result_sha256="7" * 64,
        reason_code="completed",
        run_units=1,
        issue_units=1,
        daily_units=1,
    )
    ledger.release(lease, TickOutcome(True, "completed", 1, 1, 1))

    restarted = SafetyController(ledger.database_path)
    restarted.initialize()
    assessment = restarted.assess(
        scope,
        dispatched_at + timedelta(seconds=2),
        _limits(),
    )

    assert assessment.usage.issue_units == 1
    assert assessment.usage.daily_units == 0
    with sqlite3.connect(ledger.database_path) as connection:
        assert connection.execute(
            "SELECT recorded_at_utc FROM conductor_cost_usage"
        ).fetchone() == ("2026-08-02T23:59:59.000000Z",)


def test_unbound_durable_cost_fails_closed_instead_of_charging_another_scope(
    tmp_path: Path,
):
    """Legacy or corrupt usage without a wake binding cannot borrow caller scope."""
    ledger, safety = _safety_controller(tmp_path)
    wake = normalize_wake("direct", "unbound-cost", "b" * 64)
    lease = ledger.claim_wake(wake, NOW, 60)
    assert lease is not None
    ledger.release(lease, TickOutcome(True, "completed", 1, 1, 1))

    with pytest.raises(SafetyValidationError, match="not bound"):
        safety.assess(_scope(), NOW + timedelta(seconds=1), _limits())


def test_adapter_cannot_raise_a_durable_ceiling_to_reset_usage(tmp_path: Path):
    """A later larger policy must not replenish an already-bound scope."""
    _, safety = _safety_controller(tmp_path)
    scope = _scope()
    safety.assess(scope, NOW, _limits(max_attempts_per_signature=2))
    safety.record_event("attempt-1", "attempt", scope, NOW, units=2)

    with pytest.raises(SafetyValidationError, match="cannot be increased"):
        safety.assess(
            scope,
            NOW + timedelta(seconds=1),
            _limits(max_attempts_per_signature=20),
        )

    assessment = safety.assess(
        scope,
        NOW + timedelta(seconds=2),
        _limits(max_attempts_per_signature=2),
    )
    assert assessment.breaker.state == "open"
    assert assessment.breaker.reason_code == "attempt-budget-exhausted"


@pytest.mark.parametrize(
    ("event_kind", "limit_field", "usage_field", "reason_code"),
    (
        (
            "attempt",
            "max_attempts_per_signature",
            "attempts",
            "attempt-budget-exhausted",
        ),
        (
            "repair-cycle",
            "max_repair_cycles",
            "repair_cycles",
            "repair-budget-exhausted",
        ),
        (
            "stale-lease",
            "max_stale_leases",
            "stale_leases",
            "stale-lease-budget-exhausted",
        ),
        (
            "orphaned-worker",
            "max_orphaned_workers",
            "orphaned_workers",
            "orphaned-worker-budget-exhausted",
        ),
        (
            "safety-violation",
            "max_safety_events",
            "safety_events",
            "safety-budget-exhausted",
        ),
        ("no-work", "max_no_work_ticks", "no_work_ticks", "no-work-budget-exhausted"),
    ),
)
def test_each_durable_safety_counter_opens_the_breaker_at_its_ceiling(
    tmp_path: Path,
    event_kind: str,
    limit_field: str,
    usage_field: str,
    reason_code: str,
):
    """Dropping any individual counter gate must expose an otherwise-blocked effect."""
    _, safety = _safety_controller(tmp_path)
    scope = _scope()
    limits = replace(_limits(), **{limit_field: 1})
    safety.record_event(f"event-{event_kind}", event_kind, scope, NOW)

    assessment = safety.assess(scope, NOW + timedelta(seconds=1), limits)

    assert getattr(assessment.usage, usage_field) == 1
    assert assessment.breaker.state == "open"
    assert assessment.breaker.reason_code == reason_code
    assert assessment.allows_effect is False


def test_run_time_limit_uses_durable_utc_start_time(tmp_path: Path):
    """Restarting the controller must not restart the run-time clock."""
    ledger, safety = _safety_controller(tmp_path)
    scope = _scope()
    limits = _limits(max_run_seconds=60)
    assert safety.assess(scope, NOW, limits).allows_effect is True

    restarted = SafetyController(ledger.database_path)
    restarted.initialize()
    assessment = restarted.assess(scope, NOW + timedelta(seconds=60), limits)

    assert assessment.usage.run_seconds == 60
    assert assessment.breaker.reason_code == "run-time-budget-exhausted"
    assert assessment.allows_effect is False


@pytest.mark.parametrize(
    ("limit_field", "reason_code"),
    (
        ("max_issue_units", "issue-cost-budget-exhausted"),
        ("max_daily_units", "daily-cost-budget-exhausted"),
    ),
)
def test_issue_and_daily_cost_limits_use_ledger_usage(
    tmp_path: Path,
    limit_field: str,
    reason_code: str,
):
    """A fresh caller object must not authorize a dispatch after durable cost is spent."""
    ledger, safety = _safety_controller(tmp_path)
    scope = _scope()
    wake = normalize_wake("direct", f"cost-{limit_field}", "c" * 64)
    safety.bind_wake(wake.wake_id, scope, NOW)
    lease = ledger.claim_wake(wake, NOW, 60)
    assert lease is not None
    safety.record_result_observation(
        lease.fence_token,
        wake.wake_id,
        scope,
        NOW,
        action_key=f"action-cost-{limit_field}",
        result_status="completed",
        result_sha256="7" * 64,
        reason_code="completed",
        run_units=1,
        issue_units=1,
        daily_units=1,
    )
    ledger.release(lease, TickOutcome(True, "completed", 1, 1, 1))

    assessment = safety.assess(
        scope,
        NOW + timedelta(seconds=1),
        replace(_limits(), **{limit_field: 1}),
    )

    assert assessment.breaker.reason_code == reason_code
    assert assessment.allows_effect is False


def test_expired_lease_is_atomically_counted_once_as_stale_and_orphaned(tmp_path: Path):
    """Repeated recovery checks must not multiply one abandoned worker lease."""
    ledger, safety = _safety_controller(tmp_path)
    scope = _scope()
    wake = normalize_wake("direct", "stale-1", "d" * 64)
    safety.bind_wake(wake.wake_id, scope, NOW)
    assert ledger.claim_wake(wake, NOW, 1) is not None
    limits = _limits(max_stale_leases=2, max_orphaned_workers=2)

    first = safety.assess(scope, NOW + timedelta(seconds=2), limits)
    second = safety.assess(scope, NOW + timedelta(seconds=3), limits)

    assert first.usage.stale_leases == 1
    assert first.usage.orphaned_workers == 1
    assert second.usage.stale_leases == 1
    assert second.usage.orphaned_workers == 1


def test_deterministic_failure_opens_without_a_retry(tmp_path: Path):
    """A deterministic result must never consume a transient reproduction attempt."""
    _, safety = _safety_controller(tmp_path)
    scope = _scope()
    safety.record_event("failure-1", "deterministic-failure", scope, NOW)

    assessment = safety.assess(scope, NOW + timedelta(seconds=1), _limits())

    assert assessment.usage.deterministic_failures == 1
    assert assessment.breaker.reason_code == "deterministic-failure"
    assert assessment.allows_effect is False


def test_one_transient_flake_reproduction_then_attempt_budget_opens(tmp_path: Path):
    """Two configured attempts mean the original plus exactly one reproduction."""
    _, safety = _safety_controller(tmp_path)
    scope = _scope()
    limits = _limits(max_attempts_per_signature=2)
    safety.record_event("attempt-1", "attempt", scope, NOW)
    safety.record_event("flake-1", "transient-failure", scope, NOW)

    reproduction = safety.assess(scope, NOW + timedelta(seconds=1), limits)
    safety.record_event("attempt-2", "attempt", scope, NOW + timedelta(seconds=2))
    exhausted = safety.assess(scope, NOW + timedelta(seconds=3), limits)

    assert reproduction.usage.transient_failures == 1
    assert reproduction.allows_effect is True
    assert exhausted.usage.attempts == 2
    assert exhausted.breaker.reason_code == "attempt-budget-exhausted"
    assert exhausted.allows_effect is False


def test_breaker_reset_requires_opaque_constant_time_authorization(tmp_path: Path):
    """Removing authorization or persisting its value must make reset observably unsafe."""
    authorization_input = secrets.token_urlsafe(32)
    scope = _scope()
    authorizer = _bound_reset_authorizer(authorization_input, scope)
    ledger, safety = _safety_controller(tmp_path, authorizer=authorizer)
    safety.record_event("failure-1", "deterministic-failure", scope, NOW)
    assert (
        safety.assess(scope, NOW + timedelta(seconds=1), _limits()).breaker.state
        == "open"
    )

    rejected_input = secrets.token_urlsafe(32)
    with pytest.raises(BreakerAuthorizationError, match="not authorized") as caught:
        safety.reset_breaker(scope, rejected_input, NOW + timedelta(seconds=2))
    assert rejected_input not in str(caught.value)
    assert safety.current_breaker(scope).state == "open"

    other_scope = _scope(2)
    safety.record_event("failure-2", "deterministic-failure", other_scope, NOW)
    safety.assess(other_scope, NOW + timedelta(seconds=1), _limits())
    with pytest.raises(BreakerAuthorizationError, match="not authorized"):
        safety.reset_breaker(
            other_scope,
            authorization_input,
            NOW + timedelta(seconds=2),
        )

    reset = safety.reset_breaker(scope, authorization_input, NOW + timedelta(seconds=3))

    assert reset.state == "closed"
    assert reset.reason_code == "authorized-reset"
    reopened = safety.assess(scope, NOW + timedelta(seconds=4), _limits()).breaker
    assert reopened.state == "open"
    assert reopened.transition_sequence == 3
    with pytest.raises(BreakerAuthorizationError, match="not authorized"):
        safety.reset_breaker(
            scope,
            authorization_input,
            NOW + timedelta(seconds=5),
        )
    authorization_bytes = authorization_input.encode("utf-8")
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert authorization_bytes not in path.read_bytes()


def test_breaker_transitions_are_append_only_and_resets_do_not_clear_usage(
    tmp_path: Path,
):
    """A reset must be auditable and must not silently replenish safety counters."""
    authorization_input = secrets.token_urlsafe(32)
    scope = _scope()
    authorizer = _bound_reset_authorizer(authorization_input, scope)
    ledger, safety = _safety_controller(tmp_path, authorizer=authorizer)
    safety.record_event("failure-1", "deterministic-failure", scope, NOW)
    safety.assess(scope, NOW + timedelta(seconds=1), _limits())
    safety.reset_breaker(scope, authorization_input, NOW + timedelta(seconds=2))

    transitions = safety.breaker_transitions(scope)

    assert [(item.from_state, item.to_state) for item in transitions] == [
        ("closed", "open"),
        ("open", "closed"),
    ]
    assert safety.usage_snapshot(scope).deterministic_failures == 1
    with sqlite3.connect(ledger.database_path) as connection:
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            connection.execute("DELETE FROM conductor_breaker_events")


def test_breaker_reset_authorization_expires_after_a_new_open_state_failure(
    tmp_path: Path,
):
    """A token minted for one latest failure cannot reset after another arrives."""
    authorization_input = secrets.token_urlsafe(32)
    scope = _scope()
    old_authorizer = _bound_reset_authorizer(authorization_input, scope)
    ledger, safety = _safety_controller(tmp_path, authorizer=old_authorizer)
    safety.record_event("failure-1", "deterministic-failure", scope, NOW)
    safety.assess(scope, NOW + timedelta(seconds=1), _limits())
    safety.record_event(
        "failure-2",
        "safety-violation",
        scope,
        NOW + timedelta(seconds=2),
    )

    with pytest.raises(BreakerAuthorizationError, match="not authorized"):
        safety.reset_breaker(
            scope,
            authorization_input,
            NOW + timedelta(seconds=3),
        )

    replacement = SafetyController(
        ledger.database_path,
        reset_authorizer=_bound_reset_authorizer(
            authorization_input,
            scope,
            safety_event_sequence=2,
        ),
    )
    replacement.initialize()
    assert (
        replacement.reset_breaker(
            scope,
            authorization_input,
            NOW + timedelta(seconds=4),
        ).state
        == "closed"
    )


def test_projection_is_sanitized_read_only_and_written_to_the_pipeline_path(
    tmp_path: Path,
):
    """Selecting a journal payload or mutating SQLite while projecting must fail this test."""
    ledger, safety = _safety_controller(tmp_path)
    scope = _scope()
    safety.record_event("failure-1", "deterministic-failure", scope, NOW)
    safety.assess(scope, NOW + timedelta(seconds=1), _limits())
    wake = normalize_wake("direct", "projection-1", "e" * 64)
    safety.bind_wake(wake.wake_id, scope, NOW)
    lease = ledger.claim_wake(wake, NOW, 60)
    assert lease is not None
    ledger.reserve_action(
        lease,
        ProposedAction(
            "chat",
            "projection-action",
            '{"identifier":"payload-marker"}',
            "revision-1",
            "delivery-intent",
        ),
    )
    database_before = ledger.database_path.read_bytes()

    path = publish_current_projection(
        tmp_path,
        ledger.database_path,
        NOW + timedelta(seconds=2),
    )
    projection = json.loads(path.read_text())

    assert path == (
        tmp_path / ".trinity" / "pipeline-state" / "delivery-conductor" / "current.json"
    )
    assert projection["instance_id"] == "current"
    assert projection["pipeline_id"] == "delivery-conductor"
    assert projection["current_stage"] == "blocked"
    assert projection["health"] == "blocked"
    assert projection["updated_at"] == "2026-08-02T12:00:02Z"
    assert projection["escalations"] == []
    assert projection["blockers"] == [
        {"reason_code": "deterministic-failure", "state": "open"}
    ]
    assert projection["controller"]["fence_token"] == lease.fence_token
    assert projection["lease"]["wake_id"] == wake.wake_id
    assert projection["safety"]["usage"]["deterministic_failures"] == 1
    projected_text = path.read_text()
    assert "payload-marker" not in projected_text
    assert "payload_json" not in projected_text
    assert "result" not in projected_text
    assert "evidence" not in projected_text
    assert ledger.database_path.read_bytes() == database_before


def test_projection_replace_failure_preserves_the_previous_complete_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A process failure before replace must never expose partial JSON."""
    ledger, _ = _safety_controller(tmp_path)
    path = publish_current_projection(tmp_path, ledger.database_path, NOW)
    previous = path.read_bytes()

    def fail_replace(source: object, target: object) -> None:
        raise OSError("replace-failed")

    monkeypatch.setattr(projection_module.os, "replace", fail_replace)
    with pytest.raises(ProjectionError, match="atomic projection replace failed"):
        publish_current_projection(
            tmp_path,
            ledger.database_path,
            NOW + timedelta(seconds=1),
        )

    assert path.read_bytes() == previous
    assert list(path.parent.glob(".current.*.tmp")) == []


@pytest.mark.parametrize(
    "unsafe_identifier", ("unsafe value", "safe..escape", "a" * 129)
)
def test_invalid_durable_identifier_never_replaces_a_valid_projection(
    tmp_path: Path,
    unsafe_identifier: str,
):
    """Database tampering must fail closed instead of leaking raw state to operators."""
    ledger, _ = _safety_controller(tmp_path)
    path = publish_current_projection(tmp_path, ledger.database_path, NOW)
    previous = path.read_bytes()
    with sqlite3.connect(ledger.database_path) as connection:
        connection.execute(
            "UPDATE controller_state SET reason_code = ? WHERE singleton = 1",
            (unsafe_identifier,),
        )

    with pytest.raises(ProjectionError, match="durable projection state is invalid"):
        publish_current_projection(
            tmp_path,
            ledger.database_path,
            NOW + timedelta(seconds=1),
        )

    assert path.read_bytes() == previous


def test_projection_is_never_used_as_recovery_authority(tmp_path: Path):
    """An edited read model must be replaced from SQLite on the next publish."""
    ledger, _ = _safety_controller(tmp_path)
    path = publish_current_projection(tmp_path, ledger.database_path, NOW)
    path.write_text('{"current_stage":"forged"}')

    publish_current_projection(
        tmp_path,
        ledger.database_path,
        NOW + timedelta(seconds=1),
    )

    projection = json.loads(path.read_text())
    assert projection["current_stage"] == "idle"
    assert projection["health"] == "green"


def test_projection_correlates_safety_to_the_active_wake_not_latest_timestamp(
    tmp_path: Path,
):
    """A future-dated stale scope cannot mask the active wake's breaker state."""
    ledger, safety = _safety_controller(tmp_path)
    stale_scope = _scope(2)
    safety.record_event("stale-failure", "deterministic-failure", stale_scope, NOW)
    safety.assess(stale_scope, NOW + timedelta(days=1), _limits())
    active_scope = _scope(1)
    safety.assess(active_scope, NOW, _limits())
    wake = normalize_wake("direct", "active-projection", "e" * 64)
    safety.bind_wake(wake.wake_id, active_scope, NOW)
    assert ledger.claim_wake(wake, NOW, 60) is not None

    path = publish_current_projection(tmp_path, ledger.database_path, NOW)
    projected = json.loads(path.read_text())

    assert projected["health"] == "yellow"
    assert projected["safety"]["scope"] == {
        "run_id": active_scope.run_id,
        "issue_id": active_scope.issue_id,
        "signature": active_scope.signature,
    }
    assert projected["safety"]["breaker"]["state"] == "closed"


def test_projection_publication_lock_prevents_an_older_snapshot_winning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Concurrent publishers must serialize the DB read together with replacement."""
    ledger, safety = _safety_controller(tmp_path)
    active_scope = _scope()
    safety.assess(active_scope, NOW, _limits())
    real_read = projection_module._read_projection
    first_read = threading.Event()
    release_first = threading.Event()
    second_read = threading.Event()
    failures: list[BaseException] = []

    def controlled_read(database_path: Path, now: datetime) -> dict[str, object]:
        value = real_read(database_path, now)
        if now == NOW:
            first_read.set()
            if not release_first.wait(timeout=2):
                raise AssertionError("first projection was not released")
        else:
            second_read.set()
        return value

    def publish(at: datetime) -> None:
        try:
            publish_current_projection(tmp_path, ledger.database_path, at)
        except BaseException as error:
            failures.append(error)

    monkeypatch.setattr(projection_module, "_read_projection", controlled_read)
    first = threading.Thread(target=publish, args=(NOW,))
    first.start()
    assert first_read.wait(timeout=2)
    wake = normalize_wake("direct", "concurrent-projection", "e" * 64)
    safety.bind_wake(wake.wake_id, active_scope, NOW)
    assert ledger.claim_wake(wake, NOW, 60) is not None
    second = threading.Thread(target=publish, args=(NOW + timedelta(seconds=1),))
    second.start()
    if second_read.wait(timeout=0.2):
        second.join(timeout=2)
    release_first.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert failures == []
    projected = json.loads(
        (
            tmp_path
            / ".trinity"
            / "pipeline-state"
            / "delivery-conductor"
            / "current.json"
        ).read_text()
    )
    assert projected["current_stage"] == "leased"
    assert projected["updated_at"] == "2026-08-02T12:00:01Z"


@pytest.mark.parametrize(
    "message",
    (
        '{"schema_version":1,"schema_version":1,"operation":"prepare"}',
        '{"schema_version":1,"operation":"unknown"}',
        json.dumps(
            {
                "schema_version": 1,
                "operation": "prepare",
                "wake": {
                    "source": "direct",
                    "source_event_id": "event-1",
                    "payload_sha256": "f" * 64,
                },
                "now_utc": "2026-08-02T13:00:00+01:00",
            }
        ),
        "{" + " " * (1024 * 1024) + "}",
    ),
)
def test_cli_input_is_one_closed_bounded_utc_json_object(message: str):
    """A duplicate, unknown operation, caller time, or oversize input fails closed."""
    with pytest.raises(CliValidationError):
        parse_cli_input(message)


@pytest.mark.parametrize(
    ("triggered_by", "source", "source_event_id", "event_type", "event_id"),
    (
        ("manual", "direct", "exec-manual-1", None, None),
        ("chat", "direct", "exec-chat-1", None, None),
        ("schedule", "schedule", "exec-schedule-1", None, None),
        ("reminder", "reminder", "exec-reminder-1", None, None),
        (
            "event",
            "worker-completion",
            "evt-worker-1",
            "agent.task.completed",
            "evt-worker-1",
        ),
        (
            "event",
            "worker-completion",
            "evt-worker-2",
            "agent.task.failed",
            "evt-worker-2",
        ),
    ),
)
def test_runtime_provenance_builds_one_deterministic_closed_wake(
    triggered_by: str,
    source: str,
    source_event_id: str,
    event_type: str | None,
    event_id: str | None,
):
    """Trusted prompt provenance maps to one wake without persisting prompt text."""
    execution_id = f"exec-{triggered_by}-1"
    message = build_runtime_prepare_message(
        _runtime_provenance(
            triggered_by,
            execution_id,
            event_type=event_type,
            event_id=event_id,
        ),
        execution_id,
    )

    parsed = parse_cli_input(message)

    assert parsed.wake.source == source
    assert parsed.wake.source_event_id == source_event_id
    assert parsed.wake.payload_sha256 == _provenance_digest(
        source,
        source_event_id,
        triggered_by,
        event_type,
    )
    assert "prompt" not in message
    assert "message" not in message


def test_canonical_fired_reminder_and_local_due_promotion_have_one_wake_identity():
    """Changing fired-reminder identity must let actual and local recovery both run."""
    reminder = ReminderSpec("reminder-1", "2026-08-02T09:15:11.1Z", "observe-later")
    action = tick_module._reminder_action(reminder, "repo-4")
    payload_sha256 = hashlib.sha256(action.payload_json.encode("utf-8")).hexdigest()
    message = json.dumps(
        {
            "action_key": action.action_key,
            "payload_sha256": payload_sha256,
            "references": json.loads(action.payload_json),
        },
        separators=(",", ":"),
        sort_keys=True,
    )

    parsed = parse_cli_input(
        build_runtime_prepare_message(
            _runtime_provenance(
                "reminder",
                "exec-fired-1",
                reminder_message=message,
            ),
            "exec-fired-1",
        )
    )

    assert parsed.wake == Wake(
        "bb11bebb6e5a305fac1f29b3399b3c213c2c99e0ab34867e77770031f56fb59f",
        "reminder",
        "reminder-6e65617c9573c280babf77f6707f358d7d495996c58b586aab53442a8f75c843",
        payload_sha256,
    )


def test_fired_reminder_requires_the_exact_durable_intent_and_due_time(
    tmp_path: Path,
):
    """A forged or early envelope cannot pre-consume the real reminder wake."""
    workspace = tmp_path / "workspace"
    _write_fixed_adapter(workspace, decision="noop")
    due_at = "2026-08-02T12:01:00.1Z"
    due_now = NOW + timedelta(seconds=60, microseconds=100_000)
    reminder = ReminderSpec("reminder-durable-1", due_at, "observe-later")
    action = tick_module._reminder_action(reminder, "revision-1")
    payload_sha256 = hashlib.sha256(action.payload_json.encode("utf-8")).hexdigest()
    reminder_message = json.dumps(
        {
            "action_key": action.action_key,
            "payload_sha256": payload_sha256,
            "references": json.loads(action.payload_json),
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    prepare_message = build_runtime_prepare_message(
        _runtime_provenance(
            "reminder",
            "exec-fired-durable-1",
            reminder_message=reminder_message,
        ),
        "exec-fired-durable-1",
    )

    with pytest.raises(CliValidationError, match="no durable intent"):
        run_cli(prepare_message, workspace, clock=lambda: due_now)

    database = workspace / "data" / "delivery-conductor" / "control.sqlite3"
    ledger = ControlLedger(database)
    ledger.initialize()
    source_wake = normalize_wake("direct", "source-durable-1", "e" * 64)
    source_lease = ledger.claim_wake(source_wake, NOW, 300)
    assert source_lease is not None
    ledger.reserve_action(source_lease, action)
    safety = SafetyController(database)
    safety.initialize()
    safety.bind_wake(source_wake.wake_id, _scope(), NOW)
    safety.assess(_scope(), NOW, _limits())

    with pytest.raises(CliValidationError, match="not due"):
        run_cli(prepare_message, workspace, clock=lambda: NOW)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM event_inbox WHERE source = 'reminder'"
        ).fetchone() == (0,)

    ledger.release(source_lease, TickOutcome(False, "awaiting-reminder", 0, 0, 0))
    fired = run_cli(prepare_message, workspace, clock=lambda: due_now)

    assert fired["status"] == "noop"
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT source_event_id, payload_sha256, state FROM event_inbox "
            "WHERE source = 'reminder'"
        ).fetchone() == (action.action_key, payload_sha256, "acknowledged")


@pytest.mark.parametrize(
    ("triggered_by", "reminder_message"),
    (
        ("schedule", "{}"),
        ("reminder", "{}"),
        (
            "reminder",
            '{"action_key":"reminder-wrong","payload_sha256":"'
            + "a" * 64
            + '","references":{}}',
        ),
        (
            "reminder",
            '{"action_key":"reminder-'
            + "a" * 64
            + '","payload_sha256":"'
            + "b" * 64
            + '","references":{"digest":"'
            + "c" * 64
            + '","references":{"identifiers":["reminder-1"],'
            '"reason_code":"observe-later",'
            '"utc_timestamp":"2026-08-02T09:15:11Z"}}}',
        ),
    ),
)
def test_runtime_provenance_rejects_invalid_conductor_reminder_envelope(
    triggered_by: str,
    reminder_message: str,
):
    """Partial, mismatched, or non-reminder envelopes cannot select a stable wake."""
    with pytest.raises(CliValidationError):
        build_runtime_prepare_message(
            _runtime_provenance(
                triggered_by,
                "exec-envelope-1",
                reminder_message=reminder_message,
            ),
            "exec-envelope-1",
        )


@pytest.mark.parametrize(
    ("message", "runtime_execution_id"),
    (
        (_runtime_provenance("mcp", "exec-1"), "exec-1"),
        (_runtime_provenance("retry", "exec-1"), "exec-1"),
        (_runtime_provenance("event", "exec-1"), "exec-1"),
        (
            _runtime_provenance(
                "event",
                "exec-1",
                event_type="custom.event",
                event_id="evt-1",
            ),
            "exec-1",
        ),
        (
            _runtime_provenance(
                "event",
                "exec-1",
                event_type="agent.task.completed",
                event_id=None,
            ),
            "exec-1",
        ),
        (_runtime_provenance("schedule", "forged-exec"), "exec-1"),
        (_runtime_provenance("schedule", "exec-1"), ""),
        (
            '{"schema_version":1,"triggered_by":"schedule",'
            '"execution_id":"exec-1","event_type":null,"event_id":null,'
            '"extra":"forbidden"}',
            "exec-1",
        ),
    ),
)
def test_runtime_provenance_rejects_missing_unsupported_or_mismatched_context(
    message: str,
    runtime_execution_id: str,
):
    """Unsupported triggers and incomplete worker events fail before a wake exists."""
    with pytest.raises(CliValidationError):
        build_runtime_prepare_message(message, runtime_execution_id)


@pytest.mark.parametrize("poisoned", ("bad:reason", "r" * 129))
def test_record_result_reason_identifier_is_rejected_before_state_mutation(
    tmp_path: Path,
    poisoned: str,
):
    """A projection-incompatible result reason cannot reach checkpoint state."""
    workspace = tmp_path / "workspace"
    _write_fixed_adapter(workspace)
    prepared = run_cli(
        _prepare_message("direct", "result-reason-poison"),
        workspace,
        clock=lambda: NOW,
    )

    with pytest.raises(CliValidationError):
        run_cli(
            _record_message(prepared, reason_code=poisoned),
            workspace,
            clock=lambda: NOW + timedelta(seconds=1),
        )

    database = workspace / "data" / "delivery-conductor" / "control.sqlite3"
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT status FROM action_journal"
        ).fetchone() == ("reserved",)
        assert connection.execute(
            "SELECT COUNT(*) FROM conductor_result_observations"
        ).fetchone()[0] == 0
    publish_current_projection(workspace, database, NOW + timedelta(seconds=2))


def test_prepare_uses_only_the_injected_trusted_utc_clock(tmp_path: Path):
    """Untrusted input cannot mint a future lease or move safety accounting in time."""
    workspace = tmp_path / "workspace"
    _write_fixed_adapter(workspace)
    trusted_now = NOW + timedelta(days=2)

    prepared = run_cli(
        _prepare_message("direct", "trusted-clock"),
        workspace,
        clock=lambda: trusted_now,
    )

    assert prepared["status"] == "action-ready"
    database = workspace / "data" / "delivery-conductor" / "control.sqlite3"
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT claimed_at, expires_at FROM repo_lease"
        ).fetchone() == (
            "2026-08-04T12:00:00.000000Z",
            "2026-08-04T12:05:00.000000Z",
        )


@pytest.mark.parametrize("poisoned", ("run:poison", "r" * 129))
def test_untrusted_safety_scope_identifier_cannot_poison_durable_projection(
    tmp_path: Path,
    poisoned: str,
):
    """Projection-incompatible policy identifiers fail before safety mutation."""
    workspace = tmp_path / "workspace"
    _write_fixed_adapter(workspace, run_id=poisoned)

    with pytest.raises(CliValidationError, match="safety policy"):
        run_cli(
            _prepare_message("direct", "scope-poison"),
            workspace,
            clock=lambda: NOW,
        )

    database = workspace / "data" / "delivery-conductor" / "control.sqlite3"
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM conductor_wake_scope"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM conductor_safety_limits"
        ).fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM event_inbox").fetchone()[0] == 0


@pytest.mark.parametrize("poisoned", ("action:poison", "a" * 129))
def test_untrusted_action_identifier_cannot_enter_ledger_or_break_projection(
    tmp_path: Path,
    poisoned: str,
):
    """Adapter action IDs use the same <=128/no-colon grammar as projections."""
    workspace = tmp_path / "workspace"
    _write_fixed_adapter(workspace, action_key=poisoned)

    rejected = run_cli(
        _prepare_message("direct", "action-poison"),
        workspace,
        clock=lambda: NOW,
    )

    assert rejected["status"] == "blocked"
    assert rejected["reason_code"] == "adapter-unavailable"
    database = workspace / "data" / "delivery-conductor" / "control.sqlite3"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM action_journal").fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM run_checkpoint"
        ).fetchone()[0] == 0
    publish_current_projection(workspace, database, NOW + timedelta(seconds=1))


@pytest.mark.parametrize(
    "adapter_overrides",
    (
        {"target_id": "target-1", "payload_identifier": "target-2"},
        {"capability_name": "reminders"},
        {
            "payload_references": {
                "identifiers": [
                    f"reference-{number}-" + "x" * 110 for number in range(40)
                ]
            }
        },
        {"payload_references": {"revision": "repo:poison"}},
    ),
)
def test_cli_rejects_noncanonical_capability_payload_before_action_reservation(
    tmp_path: Path,
    adapter_overrides: dict[str, object],
):
    """The adapter cannot invent tool arguments outside the local closed schema."""
    workspace = tmp_path / "workspace"
    _write_fixed_adapter(workspace, **adapter_overrides)

    blocked = run_cli(
        _prepare_message("direct", "closed-effect-schema"),
        workspace,
        clock=lambda: NOW,
    )

    assert blocked["status"] == "blocked"
    assert blocked["reason_code"] == "adapter-unavailable"
    database = workspace / "data" / "delivery-conductor" / "control.sqlite3"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM action_journal").fetchone()[0] == 0


def test_cli_workspace_guard_requires_the_exact_physical_agent_root(tmp_path: Path):
    """A caller-controlled cwd must never select another adapter or state directory."""
    root = tmp_path / "agent-root"
    inside = root / "project"
    outside = tmp_path / "outside"
    inside.mkdir(parents=True)
    outside.mkdir()
    alias = root / "alias"
    alias.symlink_to(outside, target_is_directory=True)

    assert guard_agent_workspace(root, allowed_root=root) == root.resolve()
    with pytest.raises(CliValidationError, match="agent workspace"):
        guard_agent_workspace(inside, allowed_root=root)
    with pytest.raises(CliValidationError, match="agent workspace"):
        guard_agent_workspace(outside, allowed_root=root)
    with pytest.raises(CliValidationError, match="agent workspace"):
        guard_agent_workspace(alias, allowed_root=root)


def test_fixed_adapter_entrypoint_rejects_missing_and_symlinked_files(tmp_path: Path):
    """No input field, symlink, or alternate path may choose executable adapter code."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(CliValidationError, match="fixed adapter entrypoint"):
        run_cli(_prepare_message("direct", "missing-1"), workspace, clock=lambda: NOW)

    outside = tmp_path / "outside.py"
    outside.write_text("raise SystemExit(0)\n")
    (workspace / "adapter.py").symlink_to(outside)
    with pytest.raises(CliValidationError, match="fixed adapter entrypoint"):
        run_cli(_prepare_message("direct", "symlink-1"), workspace, clock=lambda: NOW)


def test_capability_resolution_is_a_closed_two_tool_map():
    """An adapter capability must never become an MCP tool name by convention."""
    assert resolve_effect_tool("chat") == "mcp__trinity__chat_with_agent"
    assert resolve_effect_tool("reminders") == "mcp__trinity__set_reminder"
    with pytest.raises(CliValidationError, match="capability is not installed"):
        resolve_effect_tool("executions")


def test_cli_prepare_and_record_result_are_two_correlated_processes(tmp_path: Path):
    """A result must use the DB-derived action and fence before checkpoint/release."""
    workspace = tmp_path / "workspace"
    _write_fixed_adapter(workspace)

    prepared = run_cli(
        _prepare_message("direct", "cli-1"),
        workspace,
        clock=lambda: NOW,
    )

    assert prepared["status"] == "action-ready"
    assert prepared["effect_tool"] == "mcp__trinity__chat_with_agent"
    expected_payload = '{"identifier":"target-1"}'
    expected_payload_sha256 = hashlib.sha256(expected_payload.encode()).hexdigest()
    expected_effect_message = json.dumps(
        {
            "action_key": "action-cli-1",
            "payload_sha256": expected_payload_sha256,
            "references": {"identifier": "target-1"},
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    assert prepared["effect_arguments"] == {
        "agent_name": "target-1",
        "message": expected_effect_message,
    }
    assert prepared["action"] == {
        "capability_name": "chat",
        "action_key": "action-cli-1",
        "payload_sha256": expected_payload_sha256,
        "target_revision": "revision-1",
        "invalidation_class": "delivery-intent",
    }
    correlation = prepared["correlation"]
    assert isinstance(correlation, dict)
    assert set(correlation) == {"action_key", "fence_token"}

    with pytest.raises(CliCorrelationError, match="correlate"):
        run_cli(
            _record_message(
                prepared,
                fence_token=int(correlation["fence_token"]) + 1,
            ),
            workspace,
            clock=lambda: NOW + timedelta(seconds=1),
        )
    database = workspace / "data" / "delivery-conductor" / "control.sqlite3"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM repo_lease").fetchone()[0] == 1

    recorded = run_cli(
        _record_message(prepared),
        workspace,
        clock=lambda: NOW + timedelta(seconds=2),
    )
    duplicate = run_cli(
        _record_message(prepared),
        workspace,
        clock=lambda: NOW + timedelta(seconds=3),
    )

    assert recorded["status"] == "completed"
    assert recorded["action_key"] == "action-cli-1"
    assert recorded["fence_token"] == correlation["fence_token"]
    assert duplicate == recorded
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM repo_lease").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT status FROM action_journal WHERE action_key = 'action-cli-1'"
            ).fetchone()[0]
            == "completed"
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM budget_usage").fetchone()[0] == 1
        )
    projection = json.loads(
        (
            workspace
            / ".trinity"
            / "pipeline-state"
            / "delivery-conductor"
            / "current.json"
        ).read_text()
    )
    assert projection["current_stage"] == "idle"


def test_record_result_repairs_a_crash_after_terminal_journal_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A terminal journal row with a live lease must finish checkpoint and release."""
    workspace = tmp_path / "workspace"
    _write_fixed_adapter(workspace)
    prepared = run_cli(
        _prepare_message("direct", "partial-terminal"),
        workspace,
        clock=lambda: NOW,
    )
    message = _record_message(prepared)
    real_record_result = ControlLedger.record_result

    def crash_after_journal(
        ledger: ControlLedger,
        lease: object,
        action_key: str,
        result: object,
    ) -> None:
        real_record_result(ledger, lease, action_key, result)  # type: ignore[arg-type]
        raise RuntimeError("simulated-crash-after-journal")

    monkeypatch.setattr(ControlLedger, "record_result", crash_after_journal)
    with pytest.raises(RuntimeError, match="simulated-crash"):
        run_cli(message, workspace, clock=lambda: NOW + timedelta(seconds=1))
    monkeypatch.setattr(ControlLedger, "record_result", real_record_result)

    recovered = run_cli(
        message,
        workspace,
        clock=lambda: NOW + timedelta(seconds=2),
    )

    assert recovered["status"] == "completed"
    database = workspace / "data" / "delivery-conductor" / "control.sqlite3"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM repo_lease").fetchone() == (0,)
        assert connection.execute(
            "SELECT action_status FROM run_checkpoint WHERE singleton = 1"
        ).fetchone() == ("completed",)
        assert connection.execute("SELECT COUNT(*) FROM budget_usage").fetchone() == (
            1,
        )


def test_observed_result_identity_rejects_a_contradictory_retry_before_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A crash after observation cannot let a retry rewrite the external outcome."""
    workspace = tmp_path / "workspace"
    _write_fixed_adapter(workspace)
    prepared = run_cli(
        _prepare_message("direct", "observed-identity"),
        workspace,
        clock=lambda: NOW,
    )
    original = _record_message(prepared)
    real_record_result = ControlLedger.record_result

    def crash_before_ledger(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated-crash-before-ledger")

    monkeypatch.setattr(ControlLedger, "record_result", crash_before_ledger)
    with pytest.raises(RuntimeError, match="simulated-crash"):
        run_cli(original, workspace, clock=lambda: NOW + timedelta(seconds=1))
    monkeypatch.setattr(ControlLedger, "record_result", real_record_result)

    contradictory = _record_message(
        prepared,
        reason_code="contradictory-result",
        result_sha256="8" * 64,
    )
    with pytest.raises(SafetyValidationError, match="conflicts"):
        run_cli(
            contradictory,
            workspace,
            clock=lambda: NOW + timedelta(seconds=2),
        )

    database = workspace / "data" / "delivery-conductor" / "control.sqlite3"
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            """
            SELECT action_key, result_status, result_sha256, reason_code
            FROM conductor_result_observations
            """
        ).fetchone() == (
            "action-observed-identity",
            "completed",
            "9" * 64,
            "completed",
        )
        assert connection.execute(
            "SELECT status FROM action_journal"
        ).fetchone() == ("reserved",)


def test_prepare_recovers_observation_before_expired_lease_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """An observed effect owns its original fence even after nominal lease expiry."""
    workspace = tmp_path / "workspace"
    _write_fixed_adapter(workspace)
    message = _prepare_message("direct", "observed-before-ledger")
    prepared = run_cli(message, workspace, clock=lambda: NOW)
    real_record_result = ControlLedger.record_result

    def crash_before_ledger(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated-crash-before-ledger")

    monkeypatch.setattr(ControlLedger, "record_result", crash_before_ledger)
    with pytest.raises(RuntimeError, match="simulated-crash"):
        run_cli(
            _record_message(prepared),
            workspace,
            clock=lambda: NOW + timedelta(seconds=1),
        )
    monkeypatch.setattr(ControlLedger, "record_result", real_record_result)

    recovered = run_cli(
        message,
        workspace,
        clock=lambda: NOW + timedelta(seconds=301),
    )

    assert recovered["status"] == "not-claimed"
    assert recovered["action"] is None
    database = workspace / "data" / "delivery-conductor" / "control.sqlite3"
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT event_type FROM action_events ORDER BY id"
        ).fetchall() == [("reserved",), ("completed",)]
        assert connection.execute(
            "SELECT COUNT(*) FROM conductor_cli_receipts"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT run_units, issue_units, daily_units FROM conductor_cost_usage"
        ).fetchone() == (1, 1, 1)
        assert connection.execute("SELECT COUNT(*) FROM repo_lease").fetchone() == (0,)


def test_expired_partial_terminal_result_is_charged_before_fenced_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A newer fence may replay completion but cannot erase the observed effect cost."""
    workspace = tmp_path / "workspace"
    _write_fixed_adapter(workspace)
    message = _prepare_message("direct", "expired-partial")
    prepared = run_cli(message, workspace, clock=lambda: NOW)
    real_record_result = ControlLedger.record_result

    def crash_after_journal(
        ledger: ControlLedger,
        lease: object,
        action_key: str,
        result: object,
    ) -> None:
        real_record_result(ledger, lease, action_key, result)  # type: ignore[arg-type]
        raise RuntimeError("simulated-crash-after-journal")

    monkeypatch.setattr(ControlLedger, "record_result", crash_after_journal)
    with pytest.raises(RuntimeError, match="simulated-crash"):
        run_cli(
            _record_message(prepared),
            workspace,
            clock=lambda: NOW + timedelta(seconds=1),
        )
    monkeypatch.setattr(ControlLedger, "record_result", real_record_result)

    recovered = run_cli(
        _prepare_message("direct", "expired-partial"),
        workspace,
        clock=lambda: NOW + timedelta(seconds=301),
    )

    assert recovered["status"] == "not-claimed"
    assert recovered["action"] is None
    database = workspace / "data" / "delivery-conductor" / "control.sqlite3"
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            """
            SELECT run_units, issue_units, daily_units
            FROM conductor_cost_usage
            WHERE fence_token = ?
            """,
            (prepared["correlation"]["fence_token"],),
        ).fetchone() == (1, 1, 1)
        assert connection.execute(
            "SELECT COUNT(*) FROM action_events WHERE event_type = 'completed'"
        ).fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM budget_usage").fetchone() == (
            1,
        )


def test_terminal_result_without_matching_handoff_fails_closed(tmp_path: Path):
    """A terminal journal row alone cannot mint a receipt or skip safety accounting."""
    workspace = tmp_path / "workspace"
    _write_fixed_adapter(workspace)
    prepared = run_cli(
        _prepare_message("direct", "terminal-without-handoff"),
        workspace,
        clock=lambda: NOW,
    )
    database = workspace / "data" / "delivery-conductor" / "control.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            UPDATE action_journal
            SET status = 'completed', result_sha256 = ?, reason_code = 'completed'
            WHERE action_key = ?
            """,
            ("9" * 64, prepared["correlation"]["action_key"]),
        )
        connection.execute("DELETE FROM conductor_cli_handoff")

    with pytest.raises(CliCorrelationError, match="correlate"):
        run_cli(
            _record_message(prepared),
            workspace,
            clock=lambda: NOW + timedelta(seconds=1),
        )

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM conductor_cli_receipts"
        ).fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM repo_lease").fetchone() == (1,)


def test_record_result_recovers_after_release_before_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A fully released correlated result can create its missing receipt once."""
    workspace = tmp_path / "workspace"
    _write_fixed_adapter(workspace)
    prepared = run_cli(
        _prepare_message("direct", "released-before-receipt"),
        workspace,
        clock=lambda: NOW,
    )
    message = _record_message(prepared)
    real_store = cli_module._store_receipt_and_clear_handoff

    def crash_before_receipt(*args: object, **kwargs: object) -> None:
        raise RuntimeError("simulated-crash-before-receipt")

    monkeypatch.setattr(
        cli_module,
        "_store_receipt_and_clear_handoff",
        crash_before_receipt,
    )
    with pytest.raises(RuntimeError, match="simulated-crash"):
        run_cli(message, workspace, clock=lambda: NOW + timedelta(seconds=1))
    monkeypatch.setattr(cli_module, "_store_receipt_and_clear_handoff", real_store)

    recovered = run_cli(
        message,
        workspace,
        clock=lambda: NOW + timedelta(seconds=2),
    )

    assert recovered["status"] == "completed"
    database = workspace / "data" / "delivery-conductor" / "control.sqlite3"
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM conductor_cli_receipts"
        ).fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM budget_usage").fetchone() == (
            1,
        )


def test_receipt_replay_republishes_after_a_post_commit_projection_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A durable receipt retry must repair a stale non-authoritative projection."""
    workspace = tmp_path / "workspace"
    _write_fixed_adapter(workspace)
    prepared = run_cli(
        _prepare_message("direct", "receipt-projection"),
        workspace,
        clock=lambda: NOW,
    )
    message = _record_message(prepared)
    real_publish = cli_module.publish_current_projection

    def crash_before_projection(*args: object, **kwargs: object) -> Path:
        raise RuntimeError("simulated-crash-before-projection")

    monkeypatch.setattr(
        cli_module, "publish_current_projection", crash_before_projection
    )
    with pytest.raises(RuntimeError, match="simulated-crash"):
        run_cli(message, workspace, clock=lambda: NOW + timedelta(seconds=1))
    monkeypatch.setattr(cli_module, "publish_current_projection", real_publish)

    duplicate = run_cli(
        message,
        workspace,
        clock=lambda: NOW + timedelta(seconds=2),
    )

    assert duplicate["status"] == "completed"
    projection = json.loads(
        (
            workspace
            / ".trinity"
            / "pipeline-state"
            / "delivery-conductor"
            / "current.json"
        ).read_text()
    )
    assert projection["current_stage"] == "idle"
    assert projection["updated_at"] == "2026-08-02T12:00:02Z"


def test_duplicate_and_schedule_completion_race_expose_one_effect(tmp_path: Path):
    """Two wake paths for one stable action key must not emit a second effect."""
    workspace = tmp_path / "workspace"
    _write_fixed_adapter(workspace)
    schedule_message = _prepare_message("schedule", "shared-1")
    completion_message = _prepare_message("worker-completion", "shared-1")

    schedule = run_cli(schedule_message, workspace, clock=lambda: NOW)
    racing = run_cli(completion_message, workspace, clock=lambda: NOW)
    run_cli(
        _record_message(schedule),
        workspace,
        clock=lambda: NOW + timedelta(seconds=1),
    )
    replay = run_cli(
        _prepare_message("worker-completion", "shared-1"),
        workspace,
        clock=lambda: NOW + timedelta(seconds=2),
    )
    duplicate = run_cli(
        _prepare_message("schedule", "shared-1"),
        workspace,
        clock=lambda: NOW + timedelta(seconds=3),
    )

    assert schedule["status"] == "action-ready"
    assert racing["status"] == "not-claimed"
    assert racing["action"] is None
    assert replay["status"] == "completed"
    assert replay["action"] is None
    assert duplicate["status"] == "not-claimed"
    database = workspace / "data" / "delivery-conductor" / "control.sqlite3"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM event_inbox").fetchone()[0] == 2
        assert (
            connection.execute("SELECT COUNT(*) FROM action_journal").fetchone()[0] == 1
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM action_events WHERE event_type = 'reserved'"
            ).fetchone()[0]
            == 1
        )


def test_forced_restart_fences_stale_handoff_and_recovers_stable_action(tmp_path: Path):
    """A lost completion may replay one key, but an older process cannot settle it."""
    workspace = tmp_path / "workspace"
    _write_fixed_adapter(workspace)
    message = _prepare_message("direct", "restart-1")
    first = run_cli(message, workspace, clock=lambda: NOW)
    second = run_cli(
        _prepare_message("direct", "restart-1"),
        workspace,
        clock=lambda: NOW + timedelta(seconds=301),
    )

    assert first["action"]["action_key"] == second["action"]["action_key"]
    assert first["correlation"]["fence_token"] < second["correlation"]["fence_token"]
    with pytest.raises(CliCorrelationError, match="correlate"):
        run_cli(
            _record_message(first),
            workspace,
            clock=lambda: NOW + timedelta(seconds=302),
        )

    recovered = run_cli(
        _record_message(second),
        workspace,
        clock=lambda: NOW + timedelta(seconds=303),
    )

    assert recovered["status"] == "completed"
    database = workspace / "data" / "delivery-conductor" / "control.sqlite3"
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM action_journal").fetchone()[0] == 1
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM conductor_safety_events WHERE event_kind = 'stale-lease'"
            ).fetchone()[0]
            == 1
        )


def test_completed_action_reconciles_one_reminder_then_late_wake_is_noop(
    tmp_path: Path,
):
    """A restart between completion and reminder establishment must not duplicate either."""
    workspace = tmp_path / "workspace"
    _write_fixed_adapter(workspace, reminder=True)
    message = _prepare_message("direct", "reminder-1")
    action = run_cli(message, workspace, clock=lambda: NOW)
    completed = run_cli(
        _record_message(action),
        workspace,
        clock=lambda: NOW + timedelta(seconds=1),
    )

    reminder = run_cli(
        _prepare_message("direct", "reminder-1"),
        workspace,
        clock=lambda: NOW + timedelta(seconds=2),
    )

    assert completed["status"] == "reminder"
    assert reminder["status"] == "reminder-ready"
    assert reminder["effect_tool"] == "mcp__trinity__set_reminder"
    reminder_action = reminder["action"]
    assert isinstance(reminder_action, dict)
    reminder_arguments = reminder["effect_arguments"]
    assert isinstance(reminder_arguments, dict)
    assert set(reminder_arguments) == {"message", "fire_at"}
    assert reminder_arguments["fire_at"] == "2026-08-02T12:30:00Z"
    effect_message = json.loads(reminder_arguments["message"])
    assert effect_message["action_key"] == reminder_action["action_key"]
    assert effect_message["payload_sha256"] == reminder_action["payload_sha256"]
    assert set(effect_message) == {"action_key", "payload_sha256", "references"}
    effect_references = effect_message["references"]
    assert set(effect_references) == {"digest", "references"}
    assert re.fullmatch(r"[a-f0-9]{64}", effect_references["digest"])
    assert effect_references["references"] == {
        "identifiers": ["reminder-reminder-1", "action-reminder-1"],
        "reason_code": "follow-up",
        "utc_timestamp": "2026-08-02T12:30:00Z",
    }
    assert reminder["action"]["capability_name"] == "reminders"
    run_cli(
        _record_message(reminder),
        workspace,
        clock=lambda: NOW + timedelta(seconds=3),
    )
    late = run_cli(
        _prepare_message("direct", "reminder-1"),
        workspace,
        clock=lambda: NOW + timedelta(seconds=4),
    )

    assert late["status"] == "not-claimed"
    assert late["action"] is None
    database = workspace / "data" / "delivery-conductor" / "control.sqlite3"
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM action_journal WHERE capability_name = 'reminders'"
            ).fetchone()[0]
            == 1
        )


def test_overdue_cli_rebinds_safety_to_stable_due_wake_without_reminder_effect(
    tmp_path: Path,
):
    """Promoting a due intent must use its own policy scope before one next effect."""
    workspace = tmp_path / "workspace"
    _write_due_reconciliation_adapter(workspace)
    message = _prepare_message("direct", "due-scope-1")
    source = run_cli(message, workspace, clock=lambda: NOW)
    run_cli(
        _record_message(source),
        workspace,
        clock=lambda: NOW + timedelta(seconds=1),
    )

    due = run_cli(
        message,
        workspace,
        clock=lambda: NOW + timedelta(seconds=61),
    )

    assert due["status"] == "action-ready"
    assert due["effect_tool"] == "mcp__trinity__chat_with_agent"
    assert due["action"]["action_key"] == "action-due"
    assert due["reminder"] is None
    database = workspace / "data" / "delivery-conductor" / "control.sqlite3"
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT run_id FROM conductor_cli_handoff"
        ).fetchone() == ("run-reminder",)
        assert connection.execute(
            "SELECT inbox.source, scope.run_id "
            "FROM event_inbox AS inbox "
            "JOIN conductor_wake_scope AS scope ON scope.wake_id = inbox.wake_id "
            "ORDER BY inbox.source"
        ).fetchall() == [
            ("direct", "run-direct"),
            ("reminder", "run-reminder"),
        ]
        assert connection.execute(
            "SELECT run_units, issue_units, daily_units, reason_code "
            "FROM budget_usage WHERE reason_code = 'reminder-due'"
        ).fetchone() == (0, 0, 0, "reminder-due")
        assert connection.execute(
            "SELECT capability_name, status, reason_code FROM action_journal "
            "WHERE capability_name = 'reminders'"
        ).fetchone() == ("reminders", "completed", "reminder-due")


def test_deterministic_result_opens_breaker_before_later_dispatch(tmp_path: Path):
    """A deterministic outcome must reserve but never expose the next action."""
    workspace = tmp_path / "workspace"
    _write_fixed_adapter(workspace)
    first = run_cli(
        _prepare_message("direct", "defect-1"), workspace, clock=lambda: NOW
    )
    run_cli(
        _record_message(first, reason_code="deterministic-failure"),
        workspace,
        clock=lambda: NOW + timedelta(seconds=1),
    )

    blocked = run_cli(
        _prepare_message("direct", "defect-2"),
        workspace,
        clock=lambda: NOW + timedelta(seconds=2),
    )

    assert blocked["status"] == "blocked"
    assert blocked["reason_code"] == "breaker-open"
    assert blocked["action"] is None
    projection = json.loads(
        (
            workspace
            / ".trinity"
            / "pipeline-state"
            / "delivery-conductor"
            / "current.json"
        ).read_text()
    )
    assert projection["blockers"][0]["reason_code"] == "deterministic-failure"


def test_post_claim_gate_closes_concurrent_breaker_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A prepare with a stale pre-claim view must recheck after gaining the lease."""
    workspace = tmp_path / "workspace"
    _write_fixed_adapter(workspace)
    active = run_cli(
        _prepare_message("direct", "race-active"),
        workspace,
        clock=lambda: NOW,
    )
    assessed_before_release = threading.Event()
    resume_prepare = threading.Event()
    real_assess = SafetyController.assess
    first_racing_assessment = True

    def pause_after_stale_assessment(
        controller: SafetyController,
        scope: SafetyScope,
        now: datetime,
        limits: SafetyLimits,
    ):
        nonlocal first_racing_assessment
        assessment = real_assess(controller, scope, now, limits)
        if threading.current_thread().name == "racing-prepare" and first_racing_assessment:
            first_racing_assessment = False
            assessed_before_release.set()
            if not resume_prepare.wait(timeout=3):
                raise AssertionError("racing prepare was not resumed")
        return assessment

    monkeypatch.setattr(SafetyController, "assess", pause_after_stale_assessment)
    outputs: list[dict[str, object]] = []
    failures: list[BaseException] = []

    def prepare_after_failure() -> None:
        try:
            outputs.append(
                run_cli(
                    _prepare_message("direct", "race-next"),
                    workspace,
                    clock=lambda: NOW + timedelta(seconds=2),
                )
            )
        except BaseException as error:
            failures.append(error)

    racing = threading.Thread(target=prepare_after_failure, name="racing-prepare")
    racing.start()
    assert assessed_before_release.wait(timeout=3)
    run_cli(
        _record_message(active, reason_code="deterministic-failure"),
        workspace,
        clock=lambda: NOW + timedelta(seconds=2),
    )
    resume_prepare.set()
    racing.join(timeout=3)

    assert not racing.is_alive()
    assert failures == []
    assert len(outputs) == 1
    assert outputs[0]["status"] == "blocked"
    assert outputs[0]["reason_code"] == "breaker-open"
    assert outputs[0]["action"] is None


def test_crash_after_release_cannot_bypass_a_classified_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Failure evidence precedes terminal mutation so a later prepare is blocked."""
    workspace = tmp_path / "workspace"
    _write_fixed_adapter(workspace)
    prepared = run_cli(
        _prepare_message("direct", "failure-crash-1"),
        workspace,
        clock=lambda: NOW,
    )
    real_accept = cli_module.DeliveryConductorTick.accept_result

    def crash_after_release(
        tick: object,
        handoff: object,
        *,
        action_key: str,
        result: object,
    ) -> object:
        real_accept(
            tick,
            handoff,  # type: ignore[arg-type]
            action_key=action_key,
            result=result,  # type: ignore[arg-type]
        )
        raise RuntimeError("simulated-crash-after-release")

    monkeypatch.setattr(
        cli_module.DeliveryConductorTick,
        "accept_result",
        crash_after_release,
    )
    with pytest.raises(RuntimeError, match="simulated-crash"):
        run_cli(
            _record_message(prepared, reason_code="deterministic-failure"),
            workspace,
            clock=lambda: NOW + timedelta(seconds=1),
        )
    monkeypatch.setattr(
        cli_module.DeliveryConductorTick,
        "accept_result",
        real_accept,
    )

    blocked = run_cli(
        _prepare_message("direct", "failure-crash-2"),
        workspace,
        clock=lambda: NOW + timedelta(seconds=2),
    )

    assert blocked["status"] == "blocked"
    assert blocked["reason_code"] == "breaker-open"
    assert blocked["action"] is None
    database = workspace / "data" / "delivery-conductor" / "control.sqlite3"
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            """
            SELECT COUNT(*) FROM conductor_safety_events
            WHERE event_kind = 'deterministic-failure'
            """
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT run_units, issue_units, daily_units FROM conductor_cost_usage"
        ).fetchone() == (1, 1, 1)


def test_one_flake_reproduction_then_cli_blocks_the_third_signature_attempt(
    tmp_path: Path,
):
    """A limit of two attempts must return exactly two actions for one signature."""
    workspace = tmp_path / "workspace"
    _write_fixed_adapter(workspace, limits=_limits(max_attempts_per_signature=2))
    first = run_cli(_prepare_message("direct", "flake-1"), workspace, clock=lambda: NOW)
    run_cli(
        _record_message(first, reason_code="transient-failure"),
        workspace,
        clock=lambda: NOW + timedelta(seconds=1),
    )
    second = run_cli(
        _prepare_message("direct", "flake-2"),
        workspace,
        clock=lambda: NOW + timedelta(seconds=2),
    )
    run_cli(
        _record_message(second, reason_code="transient-failure"),
        workspace,
        clock=lambda: NOW + timedelta(seconds=3),
    )
    third = run_cli(
        _prepare_message("direct", "flake-3"),
        workspace,
        clock=lambda: NOW + timedelta(seconds=4),
    )

    assert first["status"] == "action-ready"
    assert second["status"] == "action-ready"
    assert third["status"] == "blocked"
    assert third["action"] is None


def test_over_cost_dispatch_is_reserved_but_returns_no_action_or_usage(tmp_path: Path):
    """Durable cost exhaustion must gate before a second external effect is exposed."""
    workspace = tmp_path / "workspace"
    _write_fixed_adapter(workspace, limits=_limits(max_issue_units=1))
    first = run_cli(
        _prepare_message("direct", "cost-cli-1"), workspace, clock=lambda: NOW
    )
    run_cli(
        _record_message(first),
        workspace,
        clock=lambda: NOW + timedelta(seconds=1),
    )

    blocked = run_cli(
        _prepare_message("direct", "cost-cli-2"),
        workspace,
        clock=lambda: NOW + timedelta(seconds=2),
    )

    assert blocked["status"] == "blocked"
    assert blocked["action"] is None
    database = workspace / "data" / "delivery-conductor" / "control.sqlite3"
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT status FROM action_journal WHERE action_key = 'action-cost-cli-2'"
            ).fetchone()[0]
            == "reserved"
        )
        assert connection.execute(
            "SELECT run_units, issue_units, daily_units FROM budget_usage ORDER BY id DESC LIMIT 1"
        ).fetchone() == (0, 0, 0)


def test_zero_usage_noop_reconciles_without_a_result_observation(tmp_path: Path):
    """A legitimate no-effect release cannot poison the next safety assessment."""
    workspace = tmp_path / "workspace"
    _write_fixed_adapter(workspace, decision="noop")

    first = run_cli(
        _prepare_message("direct", "no-work-1"),
        workspace,
        clock=lambda: NOW,
    )
    second = run_cli(
        _prepare_message("direct", "no-work-2"),
        workspace,
        clock=lambda: NOW + timedelta(seconds=1),
    )

    assert first["status"] == "noop"
    assert second["status"] == "noop"
    database = workspace / "data" / "delivery-conductor" / "control.sqlite3"
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM conductor_result_observations"
        ).fetchone() == (0,)
        assert connection.execute(
            """
            SELECT run_units, issue_units, daily_units
            FROM conductor_cost_usage ORDER BY fence_token
            """
        ).fetchall() == [(0, 0, 0), (0, 0, 0)]


def test_no_work_breaker_is_active_before_noop_releases_its_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A concurrent execute wake cannot pass between noop release and classification."""
    workspace = tmp_path / "workspace"
    limits = _limits(max_no_work_ticks=1)
    _write_fixed_adapter(workspace, limits=limits, decision="noop")
    classification_started = threading.Event()
    resume_classification = threading.Event()
    real_record_event = SafetyController.record_event

    def pause_no_work_classification(
        controller: SafetyController,
        event_key: str,
        event_kind: object,
        scope: SafetyScope,
        occurred_at: datetime,
        *,
        units: int = 1,
    ) -> None:
        if event_kind == "no-work" and threading.current_thread().name == "noop-prepare":
            classification_started.set()
            if not resume_classification.wait(timeout=3):
                raise AssertionError("no-work classification was not resumed")
        real_record_event(
            controller,
            event_key,
            event_kind,  # type: ignore[arg-type]
            scope,
            occurred_at,
            units=units,
        )

    monkeypatch.setattr(SafetyController, "record_event", pause_no_work_classification)
    noop_outputs: list[dict[str, object]] = []
    failures: list[BaseException] = []

    def prepare_noop() -> None:
        try:
            noop_outputs.append(
                run_cli(
                    _prepare_message("direct", "noop-race-first"),
                    workspace,
                    clock=lambda: NOW,
                )
            )
        except BaseException as error:
            failures.append(error)

    noop = threading.Thread(target=prepare_noop, name="noop-prepare")
    noop.start()
    assert classification_started.wait(timeout=3)
    _write_fixed_adapter(workspace, limits=limits, decision="execute")

    racing = run_cli(
        _prepare_message("direct", "noop-race-second"),
        workspace,
        clock=lambda: NOW,
    )
    resume_classification.set()
    noop.join(timeout=3)

    assert not noop.is_alive()
    assert failures == []
    assert noop_outputs[0]["status"] == "noop"
    assert racing["status"] == "not-claimed"
    assert racing["action"] is None
    blocked = run_cli(
        _prepare_message("direct", "noop-race-second"),
        workspace,
        clock=lambda: NOW + timedelta(seconds=1),
    )
    assert blocked["status"] == "blocked"
    assert blocked["reason_code"] == "breaker-open"
    assert blocked["action"] is None
