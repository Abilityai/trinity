"""Behavior tests for one delivery-conductor control tick through its ports."""
# ruff: noqa: E402
from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any

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

from delivery_conductor.adapter import (
    BoundedJsonLinesExchange,
    JsonLinesPolicyAdapter,
    PortExchangeError,
)
from delivery_conductor.contracts import (
    MAX_MESSAGE_BYTES,
    AdapterDecision,
    AdapterRequest,
    BudgetView,
    CheckpointView,
    ProposedAction,
    ReminderSpec,
    Wake,
)
from delivery_conductor.executor import (
    CapabilityNotInstalledError,
    JsonLinesCapabilityExecutor,
)
from delivery_conductor.ledger import ControlLedger, EffectResult
from delivery_conductor.tick import DeliveryConductorTick


NOW = datetime(2026, 8, 2, 9, 10, 11, tzinfo=timezone.utc)
HEALTHY_BUDGET = BudgetView(5, 6, 7)


def _wake(number: int) -> Wake:
    return Wake(
        wake_id=f"wake-{number}",
        source="event",
        source_event_id=f"event-{number}",
        payload_sha256=f"{number:x}" * 64,
    )


def _action(
    action_key: str = "action-1",
    *,
    capability_name: str = "chat",
    target_revision: str = "repo-4",
) -> ProposedAction:
    return ProposedAction(
        capability_name=capability_name,
        action_key=action_key,
        payload_json='{"references":{"digest":"'
        + "a" * 64
        + '","identifier":"target-1"}}',
        target_revision=target_revision,
        invalidation_class="observation-change",
    )


def _reminder(reminder_id: str = "reminder-1") -> ReminderSpec:
    return ReminderSpec(reminder_id, "2026-08-02T09:15:11Z", "observe-later")


def _decision(
    decision: str,
    *,
    action: ProposedAction | None = None,
    reminder: ReminderSpec | None = None,
    observed_revision: str = "repo-4",
) -> AdapterDecision:
    return AdapterDecision(
        schema_version=1,
        observed_revision=observed_revision,
        decision=decision,
        reason_code=f"decision-{decision}",
        target_id="target-1" if action is not None else None,
        proposed_action=action,
        next_reminder=reminder,
    )


class FakeAdapter:
    def __init__(self, *decisions: AdapterDecision | Any) -> None:
        self.decisions = deque(decisions)
        self.requests: list[AdapterRequest] = []

    def observe_and_decide(self, request: AdapterRequest) -> AdapterDecision:
        self.requests.append(request)
        value = self.decisions.popleft()
        return value  # type: ignore[return-value]


class FakeExecutor:
    def __init__(self, *results: EffectResult | BaseException | Any) -> None:
        self.results = deque(results)
        self.calls: list[ProposedAction] = []

    def execute(self, action: ProposedAction) -> EffectResult:
        self.calls.append(action)
        value = self.results.popleft()
        if isinstance(value, BaseException):
            raise value
        return value  # type: ignore[return-value]


class ReplayExecutor:
    """A capability fake whose stable action key prevents duplicate effects."""

    def __init__(self) -> None:
        self.calls: list[ProposedAction] = []
        self.effects: dict[str, EffectResult] = {}
        self.crash_after_first_effect = True

    def execute(self, action: ProposedAction) -> EffectResult:
        self.calls.append(action)
        result = self.effects.setdefault(
            action.action_key,
            EffectResult("completed", "c" * 64, "capability-replay"),
        )
        if self.crash_after_first_effect:
            self.crash_after_first_effect = False
            raise SystemExit("crash after effect")
        return result


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    return tmp_path / "control.db"


@pytest.fixture
def ledger(database_path: Path) -> ControlLedger:
    value = ControlLedger(database_path)
    value.initialize()
    return value


def _runner(
    ledger: ControlLedger,
    adapter: FakeAdapter,
    executor: FakeExecutor | ReplayExecutor,
) -> DeliveryConductorTick:
    return DeliveryConductorTick(
        ledger=ledger,
        adapter=adapter,
        executor=executor,
        installed_capabilities=frozenset({"chat"}),
        lease_seconds=30,
    )


def _fetchall(database_path: Path, query: str) -> list[tuple[object, ...]]:
    with sqlite3.connect(database_path) as connection:
        return connection.execute(query).fetchall()


def test_completed_tick_records_one_effect_checkpoint_budget_and_acknowledgement(
    ledger: ControlLedger, database_path: Path
):
    """Skipping any commit stage must leave the durable completed tick incomplete."""
    action = _action()
    adapter = FakeAdapter(_decision("execute", action=action))
    executor = FakeExecutor(EffectResult("completed", "c" * 64, "accepted"))
    checkpoint = CheckpointView("checkpoint-0", "0" * 64, 0, "wake-0")

    result = _runner(ledger, adapter, executor).run(
        _wake(1),
        NOW,
        checkpoint=checkpoint,
        budget_view=HEALTHY_BUDGET,
        breaker_allows_effect=True,
    )

    assert result.status == "completed"
    assert result.action_key == "action-1"
    assert result.action_status == "completed"
    assert result.result_sha256 == "c" * 64
    assert result.reminder is None
    assert adapter.requests == [
        AdapterRequest(1, _wake(1), "2026-08-02T09:10:11Z", checkpoint, HEALTHY_BUDGET)
    ]
    assert executor.calls == [action]
    assert _fetchall(database_path, "SELECT wake_id, state FROM event_inbox") == [
        ("wake-1", "acknowledged")
    ]
    assert _fetchall(
        database_path,
        "SELECT action_key, status, result_sha256 FROM action_journal",
    ) == [("action-1", "completed", "c" * 64)]
    checkpoint_rows = _fetchall(
        database_path,
        "SELECT revision, acknowledged_wake_id, reason_code, "
        "run_units_remaining, issue_units_remaining, daily_units_remaining, "
        "action_key, action_status, action_result_sha256 FROM run_checkpoint",
    )
    assert checkpoint_rows == [
        (
            "repo-4",
            "wake-1",
            "accepted",
            4,
            5,
            6,
            "action-1",
            "completed",
            "c" * 64,
        )
    ]
    assert _fetchall(
        database_path,
        "SELECT run_units, issue_units, daily_units FROM budget_usage",
    ) == [(1, 1, 1)]
    assert _fetchall(database_path, "SELECT * FROM repo_lease") == []


def test_acknowledged_duplicate_never_reobserves_or_reexecutes(ledger: ControlLedger):
    """Removing inbox deduplication from the tick must call both ports twice."""
    action = _action()
    adapter = FakeAdapter(
        _decision("execute", action=action),
        _decision("execute", action=action),
    )
    executor = FakeExecutor(
        EffectResult("completed", "c" * 64, "accepted"),
        EffectResult("completed", "c" * 64, "accepted"),
    )
    runner = _runner(ledger, adapter, executor)
    first = runner.run(
        _wake(1), NOW, None, HEALTHY_BUDGET, breaker_allows_effect=True
    )
    duplicate = runner.run(
        _wake(1), NOW + timedelta(seconds=1), None, HEALTHY_BUDGET,
        breaker_allows_effect=True,
    )

    assert first.status == "completed"
    assert duplicate.status == "not-claimed"
    assert len(adapter.requests) == 1
    assert executor.calls == [action]


def test_out_of_order_wake_reobserves_and_can_become_noop(ledger: ControlLedger):
    """Trusting event order must execute a stale event without current observation."""
    action = _action()
    adapter = FakeAdapter(
        _decision("execute", action=action),
        _decision("noop"),
    )
    executor = FakeExecutor(EffectResult("completed", "c" * 64, "accepted"))
    runner = _runner(ledger, adapter, executor)

    first = runner.run(_wake(2), NOW, None, HEALTHY_BUDGET, breaker_allows_effect=True)
    older = runner.run(
        _wake(1), NOW + timedelta(seconds=1), None, HEALTHY_BUDGET,
        breaker_allows_effect=True,
    )

    assert first.status == "completed"
    assert older.status == "noop"
    assert [request.wake.wake_id for request in adapter.requests] == ["wake-2", "wake-1"]
    assert executor.calls == [action]


def test_live_lease_makes_a_stale_competing_wake_a_noop(ledger: ControlLedger):
    """Ignoring the live lease must let a competing wake produce a second effect."""
    action = _action()
    adapter = FakeAdapter(
        _decision("execute", action=action),
        _decision("execute", action=_action("action-2")),
    )
    executor = FakeExecutor(SystemExit("crash before effect"))
    runner = _runner(ledger, adapter, executor)
    with pytest.raises(SystemExit, match="crash before effect"):
        runner.run(_wake(1), NOW, None, HEALTHY_BUDGET, breaker_allows_effect=True)

    competing = runner.run(
        _wake(2), NOW + timedelta(seconds=29), None, HEALTHY_BUDGET,
        breaker_allows_effect=True,
    )

    assert competing.status == "not-claimed"
    assert len(adapter.requests) == 1
    assert len(executor.calls) == 1


@pytest.mark.parametrize(
    "decision",
    [
        _decision("execute"),
        _decision("noop", action=_action()),
        _decision("remind"),
        _decision("investigate"),
        _decision("execute", action=_action(target_revision="repo-3")),
        _decision("unknown"),
        object(),
    ],
    ids=[
        "execute-without-action",
        "noop-with-action",
        "remind-without-reminder",
        "investigate-without-reminder",
        "stale-target-revision",
        "unknown-decision",
        "wrong-result-type",
    ],
)
def test_invalid_adapter_decision_fails_closed_without_reserving_or_executing(
    ledger: ControlLedger, database_path: Path, decision: AdapterDecision | object
):
    """Weak semantic validation must turn malformed policy output into authority."""
    adapter = FakeAdapter(decision)
    executor = FakeExecutor(EffectResult("completed", "c" * 64, "accepted"))

    result = _runner(ledger, adapter, executor).run(
        _wake(1), NOW, None, HEALTHY_BUDGET, breaker_allows_effect=True
    )

    assert result.status == "rejected"
    assert result.reason_code == "invalid-adapter-decision"
    assert executor.calls == []
    assert _fetchall(database_path, "SELECT action_key FROM action_journal") == []
    assert _fetchall(database_path, "SELECT state FROM event_inbox") == [("pending",)]
    assert _fetchall(database_path, "SELECT * FROM repo_lease") == []


def test_disallowed_capability_fails_closed_before_reservation(
    ledger: ControlLedger, database_path: Path
):
    """Letting adapter output choose an uninstalled capability grants new authority."""
    adapter = FakeAdapter(
        _decision("execute", action=_action(capability_name="uninstalled"))
    )
    executor = FakeExecutor(EffectResult("completed", "c" * 64, "accepted"))

    result = _runner(ledger, adapter, executor).run(
        _wake(1), NOW, None, HEALTHY_BUDGET, breaker_allows_effect=True
    )

    assert result.status == "rejected"
    assert result.reason_code == "capability-not-installed"
    assert executor.calls == []
    assert _fetchall(database_path, "SELECT action_key FROM action_journal") == []
    assert _fetchall(database_path, "SELECT state FROM event_inbox") == [("pending",)]


@pytest.mark.parametrize(
    ("budget", "breaker_allows_effect", "reason_code"),
    [
        (BudgetView(0, 6, 7), True, "budget-exhausted"),
        (BudgetView(5, 0, 7), True, "budget-exhausted"),
        (BudgetView(5, 6, 0), True, "budget-exhausted"),
        (HEALTHY_BUDGET, False, "breaker-open"),
        (HEALTHY_BUDGET, None, "invalid-breaker-state"),
    ],
)
def test_budget_and_breaker_fail_closed_after_durable_reservation(
    ledger: ControlLedger,
    database_path: Path,
    budget: BudgetView,
    breaker_allows_effect: bool | None,
    reason_code: str,
):
    """Moving either gate after execution must spend authority while blocked."""
    action = _action()
    adapter = FakeAdapter(_decision("execute", action=action))
    executor = FakeExecutor(EffectResult("completed", "c" * 64, "accepted"))

    result = _runner(ledger, adapter, executor).run(
        _wake(1), NOW, None, budget, breaker_allows_effect=breaker_allows_effect
    )

    assert result.status == "blocked"
    assert result.reason_code == reason_code
    assert result.action_status == "reserved"
    assert executor.calls == []
    assert _fetchall(
        database_path,
        "SELECT action_key, status FROM action_journal",
    ) == [("action-1", "reserved")]
    assert _fetchall(
        database_path,
        "SELECT action_key, action_status FROM run_checkpoint",
    ) == [("action-1", "reserved")]
    assert _fetchall(database_path, "SELECT state FROM event_inbox") == [("pending",)]
    assert _fetchall(
        database_path,
        "SELECT run_units, issue_units, daily_units FROM budget_usage",
    ) == [(0, 0, 0)]


def test_noop_checkpoints_and_acknowledges_without_executor(
    ledger: ControlLedger, database_path: Path
):
    """Treating no-op as an error must keep a settled wake pending forever."""
    adapter = FakeAdapter(_decision("noop"))
    executor = FakeExecutor(EffectResult("completed", "c" * 64, "accepted"))

    result = _runner(ledger, adapter, executor).run(
        _wake(1), NOW, None, HEALTHY_BUDGET, breaker_allows_effect=True
    )

    assert result.status == "noop"
    assert result.reminder is None
    assert executor.calls == []
    assert _fetchall(database_path, "SELECT state FROM event_inbox") == [
        ("acknowledged",)
    ]
    assert _fetchall(
        database_path,
        "SELECT revision, reason_code, action_key FROM run_checkpoint",
    ) == [("repo-4", "decision-noop", None)]


def test_reminder_decision_returns_one_reminder_without_executor(
    ledger: ControlLedger, database_path: Path
):
    """Dropping a policy reminder must lose the only requested future wake."""
    reminder = _reminder()
    adapter = FakeAdapter(_decision("remind", reminder=reminder))
    executor = FakeExecutor(EffectResult("completed", "c" * 64, "accepted"))

    result = _runner(ledger, adapter, executor).run(
        _wake(1), NOW, None, HEALTHY_BUDGET, breaker_allows_effect=True
    )

    assert result.status == "reminder"
    assert result.reminder == reminder
    assert executor.calls == []
    assert _fetchall(database_path, "SELECT state FROM event_inbox") == [
        ("acknowledged",)
    ]


def test_reserved_action_is_not_replayed_until_fresh_adapter_observes_absence(
    ledger: ControlLedger, database_path: Path
):
    """Automatic reservation replay must execute without a fresh absence observation."""
    action = _action()
    adapter = FakeAdapter(
        _decision("execute", action=action),
        _decision("noop"),
    )
    executor = FakeExecutor(
        SystemExit("crash before effect"),
        EffectResult("completed", "c" * 64, "accepted"),
    )
    runner = _runner(ledger, adapter, executor)
    with pytest.raises(SystemExit, match="crash before effect"):
        runner.run(_wake(1), NOW, None, HEALTHY_BUDGET, breaker_allows_effect=True)

    recovered = runner.run(
        _wake(1), NOW + timedelta(seconds=30), None, HEALTHY_BUDGET,
        breaker_allows_effect=True,
    )

    assert recovered.status == "noop"
    assert len(adapter.requests) == 2
    assert executor.calls == [action]
    assert _fetchall(
        database_path,
        "SELECT action_key, status FROM action_journal",
    ) == [("action-1", "reserved")]
    assert _fetchall(database_path, "SELECT state FROM event_inbox") == [
        ("acknowledged",)
    ]


def test_reserved_action_retries_after_fresh_adapter_observes_absence(
    ledger: ControlLedger, database_path: Path
):
    """Refusing a freshly re-observed reservation must strand known-absent work."""
    action = _action()
    adapter = FakeAdapter(
        _decision("execute", action=action),
        _decision("execute", action=action),
    )
    executor = FakeExecutor(
        SystemExit("crash before effect"),
        EffectResult("completed", "c" * 64, "accepted"),
    )
    runner = _runner(ledger, adapter, executor)
    with pytest.raises(SystemExit, match="crash before effect"):
        runner.run(_wake(1), NOW, None, HEALTHY_BUDGET, breaker_allows_effect=True)

    recovered = runner.run(
        _wake(1), NOW + timedelta(seconds=30), None, HEALTHY_BUDGET,
        breaker_allows_effect=True,
    )

    assert recovered.status == "completed"
    assert len(adapter.requests) == 2
    assert executor.calls == [action, action]
    assert _fetchall(
        database_path,
        "SELECT event_type FROM action_events ORDER BY id",
    ) == [("reserved",), ("completed",)]


def test_crash_after_effect_reuses_action_key_and_records_capability_replay(
    ledger: ControlLedger, database_path: Path
):
    """Minting a replacement action after a lost result must duplicate the effect."""
    action = _action()
    adapter = FakeAdapter(
        _decision("execute", action=action),
        _decision("execute", action=action),
    )
    executor = ReplayExecutor()
    runner = _runner(ledger, adapter, executor)
    with pytest.raises(SystemExit, match="crash after effect"):
        runner.run(_wake(1), NOW, None, HEALTHY_BUDGET, breaker_allows_effect=True)

    recovered = runner.run(
        _wake(1), NOW + timedelta(seconds=30), None, HEALTHY_BUDGET,
        breaker_allows_effect=True,
    )

    assert recovered.status == "completed"
    assert len(executor.calls) == 2
    assert list(executor.effects) == ["action-1"]
    assert _fetchall(
        database_path,
        "SELECT action_key, status, result_sha256 FROM action_journal",
    ) == [("action-1", "completed", "c" * 64)]


def test_completed_action_key_replay_never_calls_executor_again(ledger: ControlLedger):
    """Ignoring a completed reservation must call an idempotent rail unnecessarily."""
    action = _action()
    adapter = FakeAdapter(
        _decision("execute", action=action),
        _decision("execute", action=action),
    )
    executor = FakeExecutor(EffectResult("completed", "c" * 64, "accepted"))
    runner = _runner(ledger, adapter, executor)
    first = runner.run(_wake(1), NOW, None, HEALTHY_BUDGET, breaker_allows_effect=True)
    replay = runner.run(
        _wake(2), NOW + timedelta(seconds=1), None, HEALTHY_BUDGET,
        breaker_allows_effect=True,
    )

    assert first.status == "completed"
    assert replay.status == "completed"
    assert executor.calls == [action]


def test_ambiguous_result_becomes_investigate_reminder_and_never_reexecutes(
    ledger: ControlLedger, database_path: Path
):
    """Treating ambiguity as retryable must perform the same effect immediately again."""
    action = _action()
    adapter = FakeAdapter(
        _decision("execute", action=action),
        _decision("execute", action=action),
    )
    executor = FakeExecutor(EffectResult("ambiguous", "d" * 64, "result-unknown"))
    runner = _runner(ledger, adapter, executor)
    first = runner.run(_wake(1), NOW, None, HEALTHY_BUDGET, breaker_allows_effect=True)
    replay = runner.run(
        _wake(2), NOW + timedelta(seconds=1), None, HEALTHY_BUDGET,
        breaker_allows_effect=True,
    )

    assert first.status == "investigate"
    assert first.reminder == ReminderSpec(
        "investigate:action-1",
        "2026-08-02T09:15:11Z",
        "result-unknown",
    )
    assert replay.status == "investigate"
    assert replay.reminder == ReminderSpec(
        "investigate:action-1",
        "2026-08-02T09:15:12Z",
        "result-unknown",
    )
    assert executor.calls == [action]
    assert _fetchall(
        database_path,
        "SELECT event_type FROM action_events ORDER BY id",
    ) == [("reserved",), ("ambiguous",)]


def test_invalid_executor_result_is_recorded_ambiguous_not_retried(
    ledger: ControlLedger, database_path: Path
):
    """Trusting a malformed result must checkpoint invented success or retry the effect."""
    action = _action()
    adapter = FakeAdapter(_decision("execute", action=action))
    executor = FakeExecutor(object())

    result = _runner(ledger, adapter, executor).run(
        _wake(1), NOW, None, HEALTHY_BUDGET, breaker_allows_effect=True
    )

    assert result.status == "investigate"
    assert result.reason_code == "invalid-executor-result"
    assert len(executor.calls) == 1
    assert _fetchall(
        database_path,
        "SELECT status, reason_code FROM action_journal",
    ) == [("ambiguous", "invalid-executor-result")]


def test_executor_mapping_mismatch_is_pre_effect_block_not_ambiguity(
    ledger: ControlLedger, database_path: Path
):
    """Calling a missing installed rail must not invent an ambiguous external effect."""
    action = _action()
    adapter = FakeAdapter(_decision("execute", action=action))
    executor = FakeExecutor(CapabilityNotInstalledError("mapping mismatch"))

    result = _runner(ledger, adapter, executor).run(
        _wake(1), NOW, None, HEALTHY_BUDGET, breaker_allows_effect=True
    )

    assert result.status == "blocked"
    assert result.reason_code == "capability-not-installed"
    assert result.action_status == "reserved"
    assert _fetchall(
        database_path,
        "SELECT status, reason_code FROM action_journal",
    ) == [("reserved", None)]
    assert _fetchall(database_path, "SELECT state FROM event_inbox") == [("pending",)]


def test_json_lines_adapter_uses_one_bounded_template_owned_stream_exchange():
    """Unbounded framing must let adapter output escape the closed policy port."""
    response = BytesIO(
        b'{"schema_version":1,"observed_revision":"repo-4","decision":"noop",'
        b'"reason_code":"observed-current","target_id":null,'
        b'"proposed_action":null,"next_reminder":null}\n'
    )
    request_output = BytesIO()
    adapter = JsonLinesPolicyAdapter(BoundedJsonLinesExchange(response, request_output))
    request = AdapterRequest(
        1, _wake(1), "2026-08-02T09:10:11Z", None, HEALTHY_BUDGET
    )

    assert adapter.observe_and_decide(request) == AdapterDecision(
        1, "repo-4", "noop", "observed-current", None, None, None
    )
    written = json.loads(request_output.getvalue())
    assert written["wake"] == {
        "payload_sha256": "1" * 64,
        "source": "event",
        "source_event_id": "event-1",
        "wake_id": "wake-1",
    }

    oversized = JsonLinesPolicyAdapter(
        BoundedJsonLinesExchange(
            BytesIO(b"x" * (MAX_MESSAGE_BYTES + 2)),
            BytesIO(),
        )
    )
    with pytest.raises(PortExchangeError, match="exceeds"):
        oversized.observe_and_decide(request)


def test_json_lines_executor_is_capability_mapped_and_returns_closed_result():
    """Using adapter-selected invocation data must execute outside the installed mapping."""
    response = BytesIO(
        b'{"schema_version":1,"action_key":"action-1","status":"completed",'
        b'"result_sha256":"' + b"c" * 64 + b'","reason_code":"accepted"}\n'
    )
    request_output = BytesIO()
    executor = JsonLinesCapabilityExecutor(
        {"chat": BoundedJsonLinesExchange(response, request_output)}
    )

    assert executor.execute(_action()) == EffectResult("completed", "c" * 64, "accepted")
    written = json.loads(request_output.getvalue())
    assert written["capability_name"] == "chat"
    assert written["action_key"] == "action-1"
    with pytest.raises(CapabilityNotInstalledError):
        executor.execute(_action("action-2", capability_name="uninstalled"))
