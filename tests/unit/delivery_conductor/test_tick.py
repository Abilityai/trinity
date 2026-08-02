"""Behavior tests for one two-phase delivery-conductor control tick."""
# ruff: noqa: E402
from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
from pathlib import Path
import sqlite3
import sys
import threading
import time
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
    ContractValidationError,
    ProposedAction,
    ReminderSpec,
    Wake,
)
from delivery_conductor.executor import (
    CapabilityNotInstalledError,
    JsonLinesCapabilityExecutor,
)
from delivery_conductor.ledger import ControlLedger, EffectResult, StaleLeaseError
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


class ReplayExecutor:
    """External capability fake with action-key replay, outside the tick."""

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


class ShortWriter(BytesIO):
    def write(self, value: bytes) -> int:
        super().write(value[:1])
        return 1


class FailIfRead:
    def __init__(self) -> None:
        self.called = False

    def readline(self, _limit: int) -> bytes:
        self.called = True
        raise AssertionError("read must not start after a short write")

    def close(self) -> None:
        pass


class BlockingReader:
    def __init__(self) -> None:
        self.closed = threading.Event()

    def readline(self, _limit: int) -> bytes:
        self.closed.wait()
        return b""

    def close(self) -> None:
        self.closed.set()


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    return tmp_path / "control.db"


@pytest.fixture
def ledger(database_path: Path) -> ControlLedger:
    value = ControlLedger(database_path)
    value.initialize()
    return value


def _runner(ledger: ControlLedger, adapter: FakeAdapter) -> DeliveryConductorTick:
    return DeliveryConductorTick(
        ledger=ledger,
        adapter=adapter,
        installed_capabilities=frozenset({"chat", "reminders"}),
        lease_seconds=30,
    )


def _fetchall(database_path: Path, query: str) -> list[tuple[object, ...]]:
    with sqlite3.connect(database_path) as connection:
        return connection.execute(query).fetchall()


def _prepare_action(
    ledger: ControlLedger,
    adapter: FakeAdapter,
    *,
    wake: Wake | None = None,
    now: datetime = NOW,
):
    return _runner(ledger, adapter).run(
        wake or _wake(1),
        now,
        checkpoint=None,
        budget_view=HEALTHY_BUDGET,
        breaker_allows_effect=True,
    )


def test_prepare_returns_one_reserved_action_without_invoking_or_releasing(
    ledger: ControlLedger, database_path: Path
):
    """Calling the capability inside run must bypass the approved model handoff."""
    action = _action()
    adapter = FakeAdapter(_decision("execute", action=action))

    prepared = _prepare_action(ledger, adapter)

    assert prepared.status == "action-ready"
    assert prepared.action == action
    assert prepared.handoff.kind == "action"
    assert prepared.handoff.action == action
    assert prepared.action_key == "action-1"
    assert prepared.action_status == "reserved"
    assert _fetchall(
        database_path,
        "SELECT action_key, status FROM action_journal",
    ) == [("action-1", "reserved")]
    assert _fetchall(
        database_path,
        "SELECT action_key, action_status FROM run_checkpoint",
    ) == [("action-1", "reserved")]
    assert _fetchall(database_path, "SELECT state FROM event_inbox") == [("pending",)]
    assert _fetchall(database_path, "SELECT wake_id, fence_token FROM repo_lease") == [
        ("wake-1", 1)
    ]
    assert _fetchall(database_path, "SELECT * FROM budget_usage") == []


def test_accept_matching_result_checkpoints_acknowledges_and_releases(
    ledger: ControlLedger, database_path: Path
):
    """Dropping correlated acceptance must leave a completed tool result uncommitted."""
    action = _action()
    runner = _runner(ledger, FakeAdapter(_decision("execute", action=action)))
    prepared = runner.run(
        _wake(1), NOW, None, HEALTHY_BUDGET, breaker_allows_effect=True
    )

    completed = runner.accept_result(
        prepared.handoff,
        action_key="action-1",
        result=EffectResult("completed", "c" * 64, "accepted"),
    )

    assert completed.status == "completed"
    assert completed.action is None
    assert completed.handoff is None
    assert completed.result_sha256 == "c" * 64
    assert _fetchall(database_path, "SELECT state FROM event_inbox") == [
        ("acknowledged",)
    ]
    assert _fetchall(
        database_path,
        "SELECT status, result_sha256 FROM action_journal",
    ) == [("completed", "c" * 64)]
    assert _fetchall(
        database_path,
        "SELECT run_units_remaining, issue_units_remaining, daily_units_remaining, "
        "action_status FROM run_checkpoint",
    ) == [(4, 5, 6, "completed")]
    assert _fetchall(
        database_path,
        "SELECT run_units, issue_units, daily_units FROM budget_usage",
    ) == [(1, 1, 1)]
    assert _fetchall(database_path, "SELECT * FROM repo_lease") == []


def test_accept_rejects_wrong_action_key_under_the_live_fence(
    ledger: ControlLedger, database_path: Path
):
    """Uncorrelated record-result input must not finalize another reserved action."""
    runner = _runner(ledger, FakeAdapter(_decision("execute", action=_action())))
    prepared = runner.run(
        _wake(1), NOW, None, HEALTHY_BUDGET, breaker_allows_effect=True
    )

    with pytest.raises(ValueError, match="correl"):
        runner.accept_result(
            prepared.handoff,
            action_key="action-other",
            result=EffectResult("completed", "c" * 64, "accepted"),
        )

    assert _fetchall(database_path, "SELECT status FROM action_journal") == [
        ("reserved",)
    ]
    assert _fetchall(database_path, "SELECT wake_id FROM repo_lease") == [("wake-1",)]
    assert _fetchall(database_path, "SELECT * FROM budget_usage") == []


def test_stale_result_after_a_newer_fence_is_rejected(
    ledger: ControlLedger, database_path: Path
):
    """Accepting a late old result must let a stale model turn overwrite new work."""
    adapter = FakeAdapter(
        _decision("execute", action=_action()),
        _decision("noop"),
    )
    runner = _runner(ledger, adapter)
    old = runner.run(_wake(1), NOW, None, HEALTHY_BUDGET, breaker_allows_effect=True)
    newer = runner.run(
        _wake(2),
        NOW + timedelta(seconds=30),
        None,
        HEALTHY_BUDGET,
        breaker_allows_effect=True,
    )
    assert newer.status == "noop"

    with pytest.raises(StaleLeaseError):
        runner.accept_result(
            old.handoff,
            action_key="action-1",
            result=EffectResult("completed", "c" * 64, "accepted"),
        )

    assert _fetchall(
        database_path,
        "SELECT wake_id, state FROM event_inbox ORDER BY wake_id",
    ) == [("wake-1", "pending"), ("wake-2", "acknowledged")]


def test_duplicate_while_action_handoff_is_live_does_not_reobserve(
    ledger: ControlLedger,
):
    """A live two-phase handoff must retain the sole repository lease."""
    adapter = FakeAdapter(
        _decision("execute", action=_action()),
        _decision("execute", action=_action()),
    )
    runner = _runner(ledger, adapter)
    first = runner.run(_wake(1), NOW, None, HEALTHY_BUDGET, breaker_allows_effect=True)
    duplicate = runner.run(
        _wake(1),
        NOW + timedelta(seconds=1),
        None,
        HEALTHY_BUDGET,
        breaker_allows_effect=True,
    )

    assert first.status == "action-ready"
    assert duplicate.status == "not-claimed"
    assert len(adapter.requests) == 1


def test_invalid_request_is_rejected_before_claiming_the_wake(
    ledger: ControlLedger, database_path: Path
):
    """Constructing AdapterRequest after claim must strand a lease on bad caller input."""
    runner = _runner(ledger, FakeAdapter(_decision("noop")))

    with pytest.raises(ContractValidationError, match="checkpoint"):
        runner.run(
            _wake(1),
            NOW,
            checkpoint=object(),  # type: ignore[arg-type]
            budget_view=HEALTHY_BUDGET,
            breaker_allows_effect=True,
        )

    assert _fetchall(database_path, "SELECT * FROM event_inbox") == []
    assert _fetchall(database_path, "SELECT * FROM repo_lease") == []


@pytest.mark.parametrize(
    "decision",
    [
        _decision("execute"),
        _decision("remind"),
        _decision("investigate"),
        _decision("execute", action=_action(target_revision="repo-3")),
        _decision("unknown"),
        object(),
    ],
    ids=[
        "execute-without-action",
        "remind-without-reminder",
        "investigate-without-reminder",
        "stale-target-revision",
        "unknown-decision",
        "wrong-result-type",
    ],
)
def test_invalid_adapter_decision_fails_closed_without_reservation(
    ledger: ControlLedger, database_path: Path, decision: AdapterDecision | object
):
    """Weak semantic validation must turn malformed adapter data into authority."""
    result = _prepare_action(ledger, FakeAdapter(decision))

    assert result.status == "rejected"
    assert result.reason_code == "invalid-adapter-decision"
    assert _fetchall(database_path, "SELECT action_key FROM action_journal") == []
    assert _fetchall(database_path, "SELECT state FROM event_inbox") == [("pending",)]
    assert _fetchall(database_path, "SELECT * FROM repo_lease") == []


def test_disallowed_capability_fails_closed_before_reservation(
    ledger: ControlLedger, database_path: Path
):
    """Letting adapter output choose an uninstalled capability grants authority."""
    result = _prepare_action(
        ledger,
        FakeAdapter(
            _decision("execute", action=_action(capability_name="uninstalled"))
        ),
    )

    assert result.status == "rejected"
    assert result.reason_code == "capability-not-installed"
    assert result.action is None
    assert _fetchall(database_path, "SELECT action_key FROM action_journal") == []


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
def test_budget_and_breaker_block_after_reservation_without_returning_action(
    ledger: ControlLedger,
    database_path: Path,
    budget: BudgetView,
    breaker_allows_effect: bool | None,
    reason_code: str,
):
    """Returning an action before either gate must spend blocked authority."""
    runner = _runner(ledger, FakeAdapter(_decision("execute", action=_action())))

    result = runner.run(
        _wake(1), NOW, None, budget, breaker_allows_effect=breaker_allows_effect
    )

    assert result.status == "blocked"
    assert result.reason_code == reason_code
    assert result.action is None
    assert result.handoff is None
    assert result.action_status == "reserved"
    assert _fetchall(database_path, "SELECT status FROM action_journal") == [
        ("reserved",)
    ]
    assert _fetchall(database_path, "SELECT state FROM event_inbox") == [("pending",)]
    assert _fetchall(database_path, "SELECT * FROM repo_lease") == []


def test_noop_without_reminder_checkpoints_and_acknowledges(
    ledger: ControlLedger, database_path: Path
):
    """A settled no-op must not leave its wake or lease live."""
    result = _prepare_action(ledger, FakeAdapter(_decision("noop")))

    assert result.status == "noop"
    assert result.action is None
    assert result.reminder is None
    assert _fetchall(database_path, "SELECT state FROM event_inbox") == [
        ("acknowledged",)
    ]
    assert _fetchall(database_path, "SELECT * FROM repo_lease") == []


@pytest.mark.parametrize("decision_name", ["noop", "remind", "investigate"])
def test_reminder_intent_is_reserved_before_return_and_wake_stays_unacknowledged(
    ledger: ControlLedger, database_path: Path, decision_name: str
):
    """Acknowledging before reminder confirmation must lose the only recovery wake."""
    reminder = _reminder()
    result = _prepare_action(
        ledger,
        FakeAdapter(_decision(decision_name, reminder=reminder)),
    )

    assert result.status == "reminder-ready"
    assert result.action == result.handoff.action
    assert result.action.capability_name == "reminders"
    assert result.reminder == reminder
    assert result.handoff.kind == "reminder"
    assert result.handoff.reminder == reminder
    assert result.action_status == "reserved"
    assert _fetchall(
        database_path,
        "SELECT capability_name, status FROM action_journal",
    ) == [("reminders", "reserved")]
    assert _fetchall(
        database_path,
        "SELECT action_key, action_status FROM run_checkpoint",
    ) == [(result.action_key, "reserved")]
    assert _fetchall(database_path, "SELECT state FROM event_inbox") == [("pending",)]
    assert _fetchall(database_path, "SELECT wake_id FROM repo_lease") == [("wake-1",)]


def test_confirmed_reminder_records_result_before_acknowledgement(
    ledger: ControlLedger, database_path: Path
):
    """Reminder confirmation without journal completion must permit invented success."""
    reminder = _reminder()
    runner = _runner(ledger, FakeAdapter(_decision("remind", reminder=reminder)))
    prepared = runner.run(
        _wake(1), NOW, None, HEALTHY_BUDGET, breaker_allows_effect=True
    )

    confirmed = runner.accept_result(
        prepared.handoff,
        action_key=prepared.action_key,
        result=EffectResult("completed", "e" * 64, "reminder-established"),
    )

    assert confirmed.status == "reminder"
    assert confirmed.handoff is None
    assert _fetchall(
        database_path,
        "SELECT status, result_sha256 FROM action_journal",
    ) == [("completed", "e" * 64)]
    assert _fetchall(database_path, "SELECT state FROM event_inbox") == [
        ("acknowledged",)
    ]
    assert _fetchall(database_path, "SELECT * FROM repo_lease") == []


def test_wrong_reminder_confirmation_cannot_acknowledge(
    ledger: ControlLedger, database_path: Path
):
    """A mismatched reminder result must not consume the pending wake."""
    runner = _runner(ledger, FakeAdapter(_decision("remind", reminder=_reminder())))
    prepared = runner.run(
        _wake(1), NOW, None, HEALTHY_BUDGET, breaker_allows_effect=True
    )

    with pytest.raises(ValueError, match="correl"):
        runner.accept_result(
            prepared.handoff,
            action_key="reminder:other",
            result=EffectResult("completed", "e" * 64, "reminder-established"),
        )

    assert _fetchall(database_path, "SELECT status FROM action_journal") == [
        ("reserved",)
    ]
    assert _fetchall(database_path, "SELECT state FROM event_inbox") == [("pending",)]


def test_ambiguous_action_is_terminal_and_leaves_wake_for_reminder_only_tick(
    ledger: ControlLedger, database_path: Path
):
    """Acknowledging ambiguity or returning the action again can lose or duplicate work."""
    action = _action()
    adapter = FakeAdapter(
        _decision("execute", action=action),
        _decision("execute", action=action),
    )
    runner = _runner(ledger, adapter)
    prepared = runner.run(
        _wake(1), NOW, None, HEALTHY_BUDGET, breaker_allows_effect=True
    )
    ambiguous = runner.accept_result(
        prepared.handoff,
        action_key="action-1",
        result=EffectResult("ambiguous", "d" * 64, "result-unknown"),
    )

    assert ambiguous.status == "investigate"
    assert ambiguous.action is None
    assert ambiguous.handoff is None
    assert ambiguous.reminder == ReminderSpec(
        "investigate:action-1",
        "2026-08-02T09:15:11Z",
        "result-unknown",
    )
    assert _fetchall(database_path, "SELECT state FROM event_inbox") == [("pending",)]
    assert _fetchall(database_path, "SELECT * FROM repo_lease") == []

    reminder_tick = runner.run(
        _wake(1),
        NOW + timedelta(seconds=1),
        None,
        HEALTHY_BUDGET,
        breaker_allows_effect=True,
    )
    assert reminder_tick.status == "reminder-ready"
    assert reminder_tick.action == reminder_tick.handoff.action
    assert reminder_tick.action.capability_name == "reminders"
    assert reminder_tick.action.action_key != action.action_key
    assert reminder_tick.handoff.kind == "reminder"
    assert _fetchall(
        database_path,
        "SELECT action_key, status FROM action_journal ORDER BY action_key",
    ) == [("action-1", "ambiguous"), (reminder_tick.action_key, "reserved")]


def test_completed_action_with_next_reminder_keeps_wake_pending(
    ledger: ControlLedger, database_path: Path
):
    """One effect plus reminder acknowledgement must create an unrecoverable crash gap."""
    action = _action()
    reminder = _reminder()
    runner = _runner(
        ledger,
        FakeAdapter(_decision("execute", action=action, reminder=reminder)),
    )
    prepared = runner.run(
        _wake(1), NOW, None, HEALTHY_BUDGET, breaker_allows_effect=True
    )

    result = runner.accept_result(
        prepared.handoff,
        action_key="action-1",
        result=EffectResult("completed", "c" * 64, "accepted"),
    )

    assert result.status == "reminder"
    assert result.reminder == reminder
    assert result.action is None
    assert result.handoff is None
    assert _fetchall(database_path, "SELECT state FROM event_inbox") == [("pending",)]
    assert _fetchall(database_path, "SELECT * FROM repo_lease") == []


def test_pre_effect_recovery_requires_fresh_observation_before_returning_action(
    ledger: ControlLedger, database_path: Path
):
    """Automatic reservation replay must bypass a fresh absence observation."""
    action = _action()
    adapter = FakeAdapter(
        _decision("execute", action=action),
        _decision("noop"),
    )
    runner = _runner(ledger, adapter)
    first = runner.run(_wake(1), NOW, None, HEALTHY_BUDGET, breaker_allows_effect=True)
    assert first.status == "action-ready"

    recovered = runner.run(
        _wake(1),
        NOW + timedelta(seconds=30),
        None,
        HEALTHY_BUDGET,
        breaker_allows_effect=True,
    )

    assert recovered.status == "noop"
    assert recovered.action is None
    assert len(adapter.requests) == 2
    assert _fetchall(database_path, "SELECT status FROM action_journal") == [
        ("reserved",)
    ]


def test_reserved_action_replay_returns_same_action_after_fresh_absence(
    ledger: ControlLedger, database_path: Path
):
    """Minting or hiding an action on recovery must break action-key replay."""
    action = _action()
    adapter = FakeAdapter(
        _decision("execute", action=action),
        _decision("execute", action=action),
    )
    runner = _runner(ledger, adapter)
    first = runner.run(_wake(1), NOW, None, HEALTHY_BUDGET, breaker_allows_effect=True)
    recovered = runner.run(
        _wake(1),
        NOW + timedelta(seconds=30),
        None,
        HEALTHY_BUDGET,
        breaker_allows_effect=True,
    )

    assert first.action == action
    assert recovered.action == action
    assert recovered.handoff.lease.fence_token == 2
    assert _fetchall(
        database_path,
        "SELECT event_type FROM action_events ORDER BY id",
    ) == [("reserved",)]


def test_post_effect_recovery_uses_external_action_key_replay(
    ledger: ControlLedger, database_path: Path
):
    """A lost result must not cause the conductor to mint a replacement action."""
    action = _action()
    adapter = FakeAdapter(
        _decision("execute", action=action),
        _decision("execute", action=action),
    )
    runner = _runner(ledger, adapter)
    executor = ReplayExecutor()
    first = runner.run(_wake(1), NOW, None, HEALTHY_BUDGET, breaker_allows_effect=True)
    with pytest.raises(SystemExit, match="crash after effect"):
        executor.execute(first.action)

    recovered = runner.run(
        _wake(1),
        NOW + timedelta(seconds=30),
        None,
        HEALTHY_BUDGET,
        breaker_allows_effect=True,
    )
    result = executor.execute(recovered.action)
    completed = runner.accept_result(
        recovered.handoff,
        action_key="action-1",
        result=result,
    )

    assert completed.status == "completed"
    assert len(executor.calls) == 2
    assert list(executor.effects) == ["action-1"]
    assert _fetchall(database_path, "SELECT status FROM action_journal") == [
        ("completed",)
    ]


def test_completed_action_replay_returns_no_action(ledger: ControlLedger):
    """A terminal journal replay must not return an executable action again."""
    action = _action()
    adapter = FakeAdapter(
        _decision("execute", action=action),
        _decision("execute", action=action),
    )
    runner = _runner(ledger, adapter)
    first = runner.run(_wake(1), NOW, None, HEALTHY_BUDGET, breaker_allows_effect=True)
    runner.accept_result(
        first.handoff,
        action_key="action-1",
        result=EffectResult("completed", "c" * 64, "accepted"),
    )

    replay = runner.run(
        _wake(2),
        NOW + timedelta(seconds=1),
        None,
        HEALTHY_BUDGET,
        breaker_allows_effect=True,
    )

    assert replay.status == "completed"
    assert replay.action is None
    assert replay.handoff is None


def test_out_of_order_wake_reobserves_and_can_become_noop(ledger: ControlLedger):
    """Trusting event order must return stale executable work without observation."""
    action = _action()
    adapter = FakeAdapter(
        _decision("execute", action=action),
        _decision("noop"),
    )
    runner = _runner(ledger, adapter)
    first = runner.run(_wake(2), NOW, None, HEALTHY_BUDGET, breaker_allows_effect=True)
    runner.accept_result(
        first.handoff,
        action_key="action-1",
        result=EffectResult("completed", "c" * 64, "accepted"),
    )
    older = runner.run(
        _wake(1),
        NOW + timedelta(seconds=1),
        None,
        HEALTHY_BUDGET,
        breaker_allows_effect=True,
    )

    assert older.status == "noop"
    assert older.action is None
    assert [request.wake.wake_id for request in adapter.requests] == ["wake-2", "wake-1"]


def _noop_response(reason_code: str) -> bytes:
    value = {
        "schema_version": 1,
        "observed_revision": "repo-4",
        "decision": "noop",
        "reason_code": reason_code,
        "target_id": None,
        "proposed_action": None,
        "next_reminder": None,
    }
    return json.dumps(value, separators=(",", ":")).encode() + b"\n"


def test_policy_adapter_requires_a_fresh_exchange_for_every_observation():
    """Reusing one buffered channel must let a queued stale decision answer a new wake."""
    shared_reader = BytesIO(_noop_response("first") + _noop_response("stale-second"))
    shared_writer = BytesIO()
    adapter = JsonLinesPolicyAdapter(
        lambda: BoundedJsonLinesExchange(
            shared_reader,
            shared_writer,
            deadline_seconds=1,
        )
    )
    first_request = AdapterRequest(
        1, _wake(1), "2026-08-02T09:10:11Z", None, HEALTHY_BUDGET
    )
    second_request = AdapterRequest(
        1, _wake(2), "2026-08-02T09:10:12Z", None, HEALTHY_BUDGET
    )

    assert adapter.observe_and_decide(first_request).reason_code == "first"
    with pytest.raises(PortExchangeError, match="fresh|one request"):
        adapter.observe_and_decide(second_request)


def test_bounded_exchange_rejects_short_write_before_reading():
    """Waiting for a response after a partial request write can deadlock forever."""
    reader = FailIfRead()
    exchange = BoundedJsonLinesExchange(
        reader,  # type: ignore[arg-type]
        ShortWriter(),
        deadline_seconds=1,
    )

    with pytest.raises(PortExchangeError, match="partially written"):
        exchange.exchange("{}")
    assert reader.called is False


def test_bounded_exchange_cancels_a_stalled_read_at_its_deadline():
    """A byte cap without a deadline must let a silent peer hang the tick."""
    reader = BlockingReader()
    exchange = BoundedJsonLinesExchange(
        reader,  # type: ignore[arg-type]
        BytesIO(),
        deadline_seconds=0.02,
    )
    started = time.monotonic()

    with pytest.raises(PortExchangeError, match="timed out"):
        exchange.exchange("{}")

    assert time.monotonic() - started < 0.5
    assert reader.closed.is_set()


def test_policy_adapter_uses_closed_bounded_fresh_json_line():
    """Changing the production adapter framing must bypass the reviewed contract parser."""
    request_output = BytesIO()
    adapter = JsonLinesPolicyAdapter(
        lambda: BoundedJsonLinesExchange(
            BytesIO(_noop_response("observed-current")),
            request_output,
            deadline_seconds=1,
        )
    )
    request = AdapterRequest(
        1, _wake(1), "2026-08-02T09:10:11Z", None, HEALTHY_BUDGET
    )

    assert adapter.observe_and_decide(request) == AdapterDecision(
        1, "repo-4", "noop", "observed-current", None, None, None
    )
    written = json.loads(request_output.getvalue())
    assert written["wake"]["wake_id"] == "wake-1"

    oversized = JsonLinesPolicyAdapter(
        lambda: BoundedJsonLinesExchange(
            BytesIO(b"x" * (MAX_MESSAGE_BYTES + 2)),
            BytesIO(),
            deadline_seconds=1,
        )
    )
    with pytest.raises(PortExchangeError, match="exceeds"):
        oversized.observe_and_decide(request)


def test_external_json_lines_executor_is_fresh_capability_mapped_and_closed():
    """Letting adapter data select a channel or stale response must bypass confinement."""
    response = BytesIO(
        b'{"schema_version":1,"action_key":"action-1","status":"completed",'
        b'"result_sha256":"' + b"c" * 64 + b'","reason_code":"accepted"}\n'
    )
    request_output = BytesIO()
    executor = JsonLinesCapabilityExecutor(
        {
            "chat": lambda: BoundedJsonLinesExchange(
                response,
                request_output,
                deadline_seconds=1,
            )
        }
    )

    assert executor.execute(_action()) == EffectResult("completed", "c" * 64, "accepted")
    written = json.loads(request_output.getvalue())
    assert written["capability_name"] == "chat"
    assert written["action_key"] == "action-1"
    with pytest.raises(CapabilityNotInstalledError):
        executor.execute(_action("action-2", capability_name="uninstalled"))


def test_external_executor_rejects_a_reused_channel_with_queued_results():
    """A queued result on a reused capability channel must not satisfy another action."""
    first_result = (
        b'{"schema_version":1,"action_key":"action-1","status":"completed",'
        b'"result_sha256":"' + b"c" * 64 + b'","reason_code":"accepted"}\n'
    )
    stale_result = (
        b'{"schema_version":1,"action_key":"action-2","status":"completed",'
        b'"result_sha256":"' + b"d" * 64 + b'","reason_code":"stale"}\n'
    )
    shared_reader = BytesIO(first_result + stale_result)
    shared_writer = BytesIO()
    executor = JsonLinesCapabilityExecutor(
        {
            "chat": lambda: BoundedJsonLinesExchange(
                shared_reader,
                shared_writer,
                deadline_seconds=1,
            )
        }
    )

    assert executor.execute(_action()) == EffectResult("completed", "c" * 64, "accepted")
    rejected = executor.execute(_action("action-2"))
    assert rejected.status == "ambiguous"
    assert rejected.reason_code == "executor-exchange-ambiguous"
    assert len(rejected.result_sha256) == 64
