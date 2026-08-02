"""Behavior tests for one two-phase delivery-conductor control tick."""
# ruff: noqa: E402
from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone
import gc
from io import BytesIO
import json
from pathlib import Path
import sqlite3
import sys
import threading
import time
from typing import Any
import weakref

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
import delivery_conductor.adapter as adapter_module
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


class BlockingCloseReader:
    def __init__(self) -> None:
        self.read_released = threading.Event()
        self.close_started = threading.Event()
        self.close_released = threading.Event()

    def readline(self, _limit: int) -> bytes:
        self.read_released.wait()
        return b""

    def close(self) -> None:
        self.close_started.set()
        self.close_released.wait(0.4)
        self.read_released.set()


class CloseReturningBlockedReader:
    def __init__(self) -> None:
        self.read_released = threading.Event()
        self.read_started = threading.Event()
        self.close_called = threading.Event()
        self.read_calls = 0
        self.close_calls = 0

    def readline(self, _limit: int) -> bytes:
        self.read_calls += 1
        self.read_started.set()
        self.read_released.wait()
        return b""

    def close(self) -> None:
        self.close_calls += 1
        self.close_called.set()


class CloseTrackingWriter:
    def __init__(self) -> None:
        self.value = bytearray()
        self.written = threading.Event()
        self.closed = threading.Event()
        self.write_calls = 0
        self.close_calls = 0

    def write(self, value: bytes) -> int:
        self.write_calls += 1
        self.value.extend(value)
        self.written.set()
        return len(value)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.close_calls += 1
        self.closed.set()

    def getvalue(self) -> bytes:
        return bytes(self.value)


class BlockingRejectExchange:
    """Protocol-valid rejected port whose arbitrary cleanup callback blocks."""

    def __init__(
        self,
        reader: CloseReturningBlockedReader,
        writer: CloseTrackingWriter,
        release_reject: threading.Event,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._release_reject = release_reject
        self._cleanup_complete = threading.Event()
        self._release_complete = threading.Event()
        self.reject_started = threading.Event()
        self.preserved: tuple[object, ...] = ()
        self.exchange_calls = 0

    @property
    def channel_identity(self) -> tuple[object, object]:
        return (self._reader, self._writer)

    @property
    def cleanup_complete(self) -> threading.Event:
        return self._cleanup_complete

    @property
    def release_complete(self) -> threading.Event:
        return self._release_complete

    def reject(self, *, preserve: tuple[object, ...] = ()) -> None:
        self.preserved = preserve
        self.reject_started.set()
        self._release_reject.wait(0.4)
        for stream in (self._reader, self._writer):
            if not any(stream is item for item in preserve):
                stream.close()
        self._cleanup_complete.set()
        self._release_complete.set()

    def exchange(self, _request_line: str) -> str:
        self.exchange_calls += 1
        raise AssertionError("a rejected exchange must not start I/O")


class ObservableBlockingRejectExchange(BlockingRejectExchange):
    """Rejected port that reveals accidental admission without stalling the test."""

    def __init__(
        self,
        reader: object,
        writer: object,
        release_reject: threading.Event,
        response_line: str,
        *,
        raise_after_release: bool = False,
        return_after_release: threading.Event | None = None,
    ) -> None:
        super().__init__(reader, writer, release_reject)  # type: ignore[arg-type]
        self._response_line = response_line
        self._raise_after_release = raise_after_release
        self._return_after_release = return_after_release
        self.reject_returned = threading.Event()

    def reject(self, *, preserve: tuple[object, ...] = ()) -> None:
        try:
            super().reject(preserve=preserve)
            if self._return_after_release is not None:
                self._return_after_release.wait()
            if self._raise_after_release:
                raise RuntimeError("reject callback failed after releasing resources")
        finally:
            self.reject_returned.set()

    def exchange(self, _request_line: str) -> str:
        self.exchange_calls += 1
        self._cleanup_complete.set()
        self._release_complete.set()
        return self._response_line


class CompletingIdentityExchange:
    """Protocol fake proving a retired identity can be admitted again."""

    def __init__(self, identity: tuple[object, object], response_line: str) -> None:
        self._identity = identity
        self._response_line = response_line
        self._cleanup_complete = threading.Event()
        self._release_complete = threading.Event()
        self.exchange_calls = 0

    @property
    def channel_identity(self) -> tuple[object, object]:
        return self._identity

    @property
    def cleanup_complete(self) -> threading.Event:
        return self._cleanup_complete

    @property
    def release_complete(self) -> threading.Event:
        return self._release_complete

    def reject(self, *, preserve: tuple[object, ...] = ()) -> None:
        del preserve
        self._cleanup_complete.set()
        self._release_complete.set()

    def exchange(self, _request_line: str) -> str:
        self.exchange_calls += 1
        self._cleanup_complete.set()
        self._release_complete.set()
        return self._response_line


class CrashInjectingLedger(ControlLedger):
    """Real SQLite ledger with process-crash injection at transaction boundaries."""

    def __init__(self, database_path: Path) -> None:
        super().__init__(database_path)
        self.crash_before_terminal_result = False
        self.crash_before_reminder_reservation = False

    def record_result(
        self,
        lease,
        action_key: str,
        result: EffectResult,
    ) -> None:
        if self.crash_before_terminal_result:
            self.crash_before_terminal_result = False
            raise SystemExit("crash before terminal result")
        super().record_result(lease, action_key, result)

    def reserve_action(self, lease, action: ProposedAction):
        if action.capability_name == "reminders" and self.crash_before_reminder_reservation:
            self.crash_before_reminder_reservation = False
            raise SystemExit("crash before reminder reservation")
        return super().reserve_action(lease, action)


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


def test_crash_before_terminal_result_never_exposes_a_reminder(
    database_path: Path,
):
    """A later reminder cannot change bytes under one reserved source action key."""
    ledger = CrashInjectingLedger(database_path)
    ledger.initialize()
    action = _action()
    reminder = _reminder()
    later_reminder = ReminderSpec(
        "reminder-1",
        "2026-08-02T09:45:42Z",
        "later-observation",
    )
    adapter = FakeAdapter(
        _decision("execute", action=action, reminder=reminder),
        _decision("execute", action=action, reminder=later_reminder),
    )
    runner = _runner(ledger, adapter)
    prepared = runner.run(
        _wake(1), NOW, None, HEALTHY_BUDGET, breaker_allows_effect=True
    )
    payload = json.loads(prepared.action.payload_json)
    embedded = payload["references"][1]
    assert embedded["identifier"] == "delivery-conductor-reminder-v1"
    assert embedded["identifiers"] == ["reminder-1"]
    assert embedded["utc_timestamp"] == "2026-08-02T09:15:11Z"
    canonical_source_payload = prepared.action.payload_json

    ledger.crash_before_terminal_result = True
    with pytest.raises(SystemExit, match="before terminal result"):
        runner.accept_result(
            prepared.handoff,
            action_key=action.action_key,
            result=EffectResult("completed", "c" * 64, "accepted"),
        )

    assert _fetchall(
        database_path,
        "SELECT capability_name, status, payload_json "
        "FROM action_journal ORDER BY action_key",
    ) == [("chat", "reserved", canonical_source_payload)]

    source_replay = runner.run(
        _wake(1),
        NOW + timedelta(seconds=31),
        None,
        HEALTHY_BUDGET,
        breaker_allows_effect=True,
    )
    assert source_replay.status == "action-ready"
    assert source_replay.action == prepared.action
    assert source_replay.reminder == reminder
    assert len(adapter.requests) == 2
    runner.accept_result(
        source_replay.handoff,
        action_key=action.action_key,
        result=EffectResult("completed", "c" * 64, "accepted"),
    )

    reminder_tick = runner.run(
        _wake(1),
        NOW + timedelta(seconds=62),
        None,
        HEALTHY_BUDGET,
        breaker_allows_effect=True,
    )
    assert reminder_tick.status == "reminder-ready"
    assert reminder_tick.reminder == reminder
    assert reminder_tick.reminder != later_reminder
    runner.accept_result(
        reminder_tick.handoff,
        action_key=reminder_tick.action_key,
        result=EffectResult("completed", "e" * 64, "reminder-established"),
    )
    assert _fetchall(database_path, "SELECT state FROM event_inbox") == [
        ("acknowledged",)
    ]
    assert _fetchall(
        database_path,
        "SELECT payload_json FROM action_journal WHERE capability_name = 'chat'",
    ) == [(canonical_source_payload,)]


def test_pre_terminal_recovery_rejects_conflicting_same_key_observation(
    ledger: ControlLedger,
    database_path: Path,
):
    """A changed source payload under one reserved key cannot reach an effect."""
    action = _action()
    conflicting_action = ProposedAction(
        capability_name="chat",
        action_key=action.action_key,
        payload_json='{"references":{"digest":"'
        + "b" * 64
        + '","identifier":"target-1"}}',
        target_revision=action.target_revision,
        invalidation_class=action.invalidation_class,
    )
    adapter = FakeAdapter(
        _decision("execute", action=action, reminder=_reminder()),
        _decision("execute", action=conflicting_action, reminder=_reminder()),
    )
    runner = _runner(ledger, adapter)
    prepared = runner.run(
        _wake(1), NOW, None, HEALTHY_BUDGET, breaker_allows_effect=True
    )

    recovered = runner.run(
        _wake(1),
        NOW + timedelta(seconds=31),
        None,
        HEALTHY_BUDGET,
        breaker_allows_effect=True,
    )

    assert recovered.status == "rejected"
    assert recovered.reason_code == "reserved-action-conflict"
    assert recovered.action is None
    assert recovered.handoff is None
    assert len(adapter.requests) == 2
    assert _fetchall(
        database_path,
        "SELECT status, payload_json FROM action_journal",
    ) == [("reserved", prepared.action.payload_json)]
    assert _fetchall(database_path, "SELECT state FROM event_inbox") == [("pending",)]
    assert _fetchall(database_path, "SELECT * FROM repo_lease") == []


def test_crash_after_terminal_result_recovers_reminder_before_adapter(
    database_path: Path,
):
    """The terminal source row must durably reconstruct an unreserved reminder."""
    ledger = CrashInjectingLedger(database_path)
    ledger.initialize()
    action = _action()
    reminder = _reminder()
    adapter = FakeAdapter(
        _decision("execute", action=action, reminder=reminder),
        _decision("noop"),
    )
    runner = _runner(ledger, adapter)
    prepared = runner.run(
        _wake(1), NOW, None, HEALTHY_BUDGET, breaker_allows_effect=True
    )

    ledger.crash_before_reminder_reservation = True
    with pytest.raises(SystemExit, match="before reminder reservation"):
        runner.accept_result(
            prepared.handoff,
            action_key=action.action_key,
            result=EffectResult("completed", "c" * 64, "accepted"),
        )

    assert _fetchall(
        database_path,
        "SELECT capability_name, status FROM action_journal ORDER BY action_key",
    ) == [("chat", "completed")]

    recovered = runner.run(
        _wake(1),
        NOW + timedelta(seconds=31),
        None,
        HEALTHY_BUDGET,
        breaker_allows_effect=True,
    )
    assert recovered.status == "reminder-ready"
    assert recovered.reminder == reminder
    assert len(adapter.requests) == 1
    recovered_payload = json.loads(recovered.action.payload_json)
    assert recovered_payload["references"] == {
        "identifiers": ["reminder-1", "action-1"],
        "reason_code": "observe-later",
        "utc_timestamp": "2026-08-02T09:15:11Z",
    }

    runner.accept_result(
        recovered.handoff,
        action_key=recovered.action_key,
        result=EffectResult("completed", "e" * 64, "reminder-established"),
    )
    assert _fetchall(database_path, "SELECT state FROM event_inbox") == [
        ("acknowledged",)
    ]


def test_ambiguous_result_reserves_stable_executable_reminder_before_release(
    ledger: ControlLedger, database_path: Path
):
    """Deferring reminder reservation must let a later noop lose ambiguous recovery."""
    action = _action()
    adapter = FakeAdapter(
        _decision("execute", action=action),
        _decision("noop"),
    )
    runner = _runner(ledger, adapter)
    prepared = runner.run(
        _wake(1), NOW, None, HEALTHY_BUDGET, breaker_allows_effect=True
    )

    runner.accept_result(
        prepared.handoff,
        action_key="action-1",
        result=EffectResult("ambiguous", "d" * 64, "result-unknown"),
    )

    assert _fetchall(
        database_path,
        "SELECT capability_name, status FROM action_journal ORDER BY action_key",
    ) == [("chat", "ambiguous"), ("reminders", "reserved")]

    recovered = runner.run(
        _wake(1),
        NOW + timedelta(seconds=90),
        None,
        HEALTHY_BUDGET,
        breaker_allows_effect=True,
    )

    assert recovered.status == "reminder-ready"
    assert recovered.action.capability_name == "reminders"
    assert recovered.reminder.due_at_utc == "2026-08-02T09:15:11Z"
    payload = json.loads(recovered.action.payload_json)
    assert payload["references"]["identifiers"] == [
        "investigate:action-1",
        "action-1",
    ]
    assert payload["references"]["utc_timestamp"] == "2026-08-02T09:15:11Z"
    assert len(adapter.requests) == 1


def test_repeated_ambiguous_recovery_reuses_one_reminder_key_and_due_time(
    ledger: ControlLedger, database_path: Path
):
    """Deriving reminder due time from each recovery now must mint duplicate effects."""
    action = _action()
    adapter = FakeAdapter(
        _decision("execute", action=action),
        _decision("execute", action=action),
    )
    runner = _runner(ledger, adapter)
    prepared = runner.run(
        _wake(1), NOW, None, HEALTHY_BUDGET, breaker_allows_effect=True
    )
    runner.accept_result(
        prepared.handoff,
        action_key="action-1",
        result=EffectResult("ambiguous", "d" * 64, "result-unknown"),
    )

    first = runner.run(
        _wake(1),
        NOW + timedelta(seconds=60),
        None,
        HEALTHY_BUDGET,
        breaker_allows_effect=True,
    )
    second = runner.run(
        _wake(2),
        NOW + timedelta(seconds=120),
        None,
        HEALTHY_BUDGET,
        breaker_allows_effect=True,
    )

    assert first.action.action_key == second.action.action_key
    assert first.action.payload_json == second.action.payload_json
    assert first.reminder.due_at_utc == second.reminder.due_at_utc
    assert _fetchall(
        database_path,
        "SELECT capability_name, COUNT(*) FROM action_journal "
        "GROUP BY capability_name ORDER BY capability_name",
    ) == [("chat", 1), ("reminders", 1)]
    assert [request.wake.wake_id for request in adapter.requests] == ["wake-1", "wake-2"]


def test_completed_follow_up_reminder_preempts_recovery_noop(
    ledger: ControlLedger, database_path: Path
):
    """Consulting the adapter first must let noop acknowledge a durable follow-up."""
    action = _action()
    reminder = _reminder()
    adapter = FakeAdapter(
        _decision("execute", action=action, reminder=reminder),
        _decision("noop"),
    )
    runner = _runner(ledger, adapter)
    prepared = runner.run(
        _wake(1), NOW, None, HEALTHY_BUDGET, breaker_allows_effect=True
    )
    accepted = runner.accept_result(
        prepared.handoff,
        action_key="action-1",
        result=EffectResult("completed", "c" * 64, "accepted"),
    )

    assert accepted.action is None
    assert accepted.handoff is None
    assert _fetchall(
        database_path,
        "SELECT capability_name, status FROM action_journal ORDER BY action_key",
    ) == [("chat", "completed"), ("reminders", "reserved")]

    recovered = runner.run(
        _wake(1),
        NOW + timedelta(seconds=60),
        None,
        HEALTHY_BUDGET,
        breaker_allows_effect=True,
    )

    assert recovered.status == "reminder-ready"
    assert recovered.action.capability_name == "reminders"
    assert recovered.reminder == reminder
    assert json.loads(recovered.action.payload_json)["references"] == {
        "identifiers": ["reminder-1", "action-1"],
        "reason_code": "observe-later",
        "utc_timestamp": "2026-08-02T09:15:11Z",
    }
    assert len(adapter.requests) == 1
    assert _fetchall(database_path, "SELECT state FROM event_inbox") == [("pending",)]


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
    with pytest.raises(PortExchangeError):
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
    assert reader.closed.wait(0.1)


def test_timeout_does_not_wait_for_a_blocking_stream_close():
    """Calling arbitrary close synchronously must let cleanup exceed the deadline."""
    reader = BlockingCloseReader()
    writer = CloseTrackingWriter()
    exchange = BoundedJsonLinesExchange(
        reader,  # type: ignore[arg-type]
        writer,  # type: ignore[arg-type]
        deadline_seconds=0.02,
    )
    started = time.monotonic()

    try:
        with pytest.raises(PortExchangeError, match="timed out"):
            exchange.exchange("{}")
        elapsed = time.monotonic() - started
        assert elapsed < 0.2
        assert reader.close_started.wait(0.1)
        assert writer.closed.wait(0.1)
    finally:
        reader.close_released.set()
        reader.read_released.set()


def test_exchange_closes_both_streams_after_success():
    """Leaving one-shot streams open must leak descriptors after success."""
    success_reader = BytesIO(_noop_response("closed-success"))
    success_writer = BytesIO()
    successful = BoundedJsonLinesExchange(
        success_reader,
        success_writer,
        deadline_seconds=1,
    )

    assert "closed-success" in successful.exchange("{}")
    assert success_reader.closed is True
    assert success_writer.closed is True


def test_exchange_closes_both_streams_after_decode_error():
    """Skipping cleanup on a decode error must leak both one-shot streams."""
    error_reader = BytesIO(b"\xff\n")
    error_writer = BytesIO()
    failing = BoundedJsonLinesExchange(
        error_reader,
        error_writer,
        deadline_seconds=1,
    )

    with pytest.raises(PortExchangeError, match="UTF-8"):
        failing.exchange("{}")
    assert error_reader.closed is True
    assert error_writer.closed is True


def test_policy_adapter_registry_does_not_retain_completed_channels():
    """Remembering every completed stream pair must grow memory without a bound."""
    channel_refs: list[weakref.ReferenceType[BytesIO]] = []

    def fresh_exchange() -> BoundedJsonLinesExchange:
        reader = BytesIO(_noop_response("many"))
        writer = BytesIO()
        channel_refs.extend((weakref.ref(reader), weakref.ref(writer)))
        return BoundedJsonLinesExchange(reader, writer, deadline_seconds=1)

    adapter = JsonLinesPolicyAdapter(fresh_exchange)
    for _ in range(64):
        request = AdapterRequest(
            1,
            _wake(1),
            "2026-08-02T09:10:11Z",
            None,
            HEALTHY_BUDGET,
        )
        assert adapter.observe_and_decide(request).reason_code == "many"

    gc.collect()
    assert all(channel_ref() is None for channel_ref in channel_refs)


def test_policy_adapter_rejects_a_second_wrapper_over_one_live_channel():
    """Dropping active identity tracking must allow concurrent response theft."""
    reader = BlockingReader()
    writer = CloseTrackingWriter()
    adapter = JsonLinesPolicyAdapter(
        lambda: BoundedJsonLinesExchange(
            reader,  # type: ignore[arg-type]
            writer,  # type: ignore[arg-type]
            deadline_seconds=1,
        )
    )
    request = AdapterRequest(
        1, _wake(1), "2026-08-02T09:10:11Z", None, HEALTHY_BUDGET
    )
    first_errors: list[BaseException] = []

    def first_observation() -> None:
        try:
            adapter.observe_and_decide(request)
        except BaseException as error:
            first_errors.append(error)

    worker = threading.Thread(target=first_observation)
    worker.start()
    assert writer.written.wait(0.2)
    with pytest.raises(PortExchangeError):
        adapter.observe_and_decide(request)
    reader.closed.set()
    worker.join(0.5)
    assert not worker.is_alive()
    assert len(first_errors) == 1
    assert isinstance(first_errors[0], PortExchangeError)


@pytest.mark.parametrize("reused_side", ("reader", "writer"))
def test_policy_adapter_rejects_either_reused_live_stream(reused_side: str):
    """A new wrapper cannot share either half of a still-running channel."""
    first_reader = CloseReturningBlockedReader()
    first_writer = CloseTrackingWriter()
    second_reader = first_reader if reused_side == "reader" else CloseReturningBlockedReader()
    second_writer = first_writer if reused_side == "writer" else CloseTrackingWriter()
    exchanges = deque(
        (
            BoundedJsonLinesExchange(
                first_reader,  # type: ignore[arg-type]
                first_writer,  # type: ignore[arg-type]
                deadline_seconds=0.02,
            ),
            BoundedJsonLinesExchange(
                second_reader,  # type: ignore[arg-type]
                second_writer,  # type: ignore[arg-type]
                deadline_seconds=0.02,
            ),
        )
    )
    adapter = JsonLinesPolicyAdapter(exchanges.popleft)
    request = AdapterRequest(
        1, _wake(1), "2026-08-02T09:10:11Z", None, HEALTHY_BUDGET
    )

    try:
        with pytest.raises(PortExchangeError, match="timed out"):
            adapter.observe_and_decide(request)
        with pytest.raises(PortExchangeError, match="fresh channel"):
            adapter.observe_and_decide(request)

        assert first_reader.read_calls == 1
        if reused_side == "reader":
            assert second_writer.write_calls == 0
            assert second_writer.closed.wait(0.1)
        else:
            assert second_reader.read_calls == 0
            assert second_reader.close_called.wait(0.1)
    finally:
        first_reader.read_released.set()
        second_reader.read_released.set()


def test_policy_adapter_cap_rejection_closes_channels_without_starting_more_workers(
    monkeypatch: pytest.MonkeyPatch,
):
    """Repeated pre-exchange cap rejection must keep worker and resource use bounded."""
    cap = 4
    monkeypatch.setattr(adapter_module, "_MAX_LIVE_CHANNELS", cap)
    readers: list[CloseReturningBlockedReader] = []
    writers: list[CloseTrackingWriter] = []

    def fresh_blocked_exchange() -> BoundedJsonLinesExchange:
        reader = CloseReturningBlockedReader()
        writer = CloseTrackingWriter()
        readers.append(reader)
        writers.append(writer)
        return BoundedJsonLinesExchange(
            reader,  # type: ignore[arg-type]
            writer,  # type: ignore[arg-type]
            deadline_seconds=0.01,
        )

    adapter = JsonLinesPolicyAdapter(fresh_blocked_exchange)
    request = AdapterRequest(
        1, _wake(1), "2026-08-02T09:10:11Z", None, HEALTHY_BUDGET
    )

    try:
        for _ in range(cap + 8):
            with pytest.raises(PortExchangeError):
                adapter.observe_and_decide(request)

        assert sum(reader.read_calls for reader in readers) == cap
        assert sum(writer.write_calls for writer in writers) == cap
        assert all(reader.close_called.wait(0.1) for reader in readers)
        assert all(writer.closed.wait(0.1) for writer in writers)
    finally:
        for reader in readers:
            reader.read_released.set()


@pytest.mark.parametrize("reused_side", ("reader", "writer"))
def test_blocking_collision_rejection_has_one_global_fixed_resource_ceiling(
    monkeypatch: pytest.MonkeyPatch,
    reused_side: str,
):
    """Blocking reject callbacks cannot grow workers or close a live shared half."""
    cap = 3
    monkeypatch.setattr(adapter_module, "_MAX_LIVE_CHANNELS", cap)
    first_reader = CloseReturningBlockedReader()
    first_writer = CloseTrackingWriter()
    release_reject = threading.Event()
    rejected: list[BlockingRejectExchange] = []
    adapter_rejected: list[BlockingRejectExchange] = []
    executor_rejected: list[BlockingRejectExchange] = []
    first_exchange = BoundedJsonLinesExchange(
        first_reader,  # type: ignore[arg-type]
        first_writer,  # type: ignore[arg-type]
        deadline_seconds=0.01,
    )
    adapter_factory_calls = 0
    executor_factory_calls = 0

    def rejected_exchange() -> BlockingRejectExchange:
        reader = (
            first_reader
            if reused_side == "reader"
            else CloseReturningBlockedReader()
        )
        writer = first_writer if reused_side == "writer" else CloseTrackingWriter()
        exchange = BlockingRejectExchange(reader, writer, release_reject)
        rejected.append(exchange)
        return exchange

    def adapter_factory():
        nonlocal adapter_factory_calls
        adapter_factory_calls += 1
        if adapter_factory_calls == 1:
            return first_exchange
        exchange = rejected_exchange()
        adapter_rejected.append(exchange)
        return exchange

    def executor_factory():
        nonlocal executor_factory_calls
        executor_factory_calls += 1
        exchange = rejected_exchange()
        executor_rejected.append(exchange)
        return exchange

    request = AdapterRequest(
        1, _wake(1), "2026-08-02T09:10:11Z", None, HEALTHY_BUDGET
    )
    adapter = JsonLinesPolicyAdapter(adapter_factory)
    executor = JsonLinesCapabilityExecutor({"chat": executor_factory})

    started = time.monotonic()
    try:
        with pytest.raises(PortExchangeError):
            adapter.observe_and_decide(request)
        for number in range(cap + 6):
            if number % 2:
                result = executor.execute(_action(f"action-{number + 1}"))
                assert result.reason_code == "executor-exchange-ambiguous"
            else:
                with pytest.raises(PortExchangeError):
                    adapter.observe_and_decide(request)
        elapsed = time.monotonic() - started

        assert elapsed < 0.25
        assert adapter_factory_calls + executor_factory_calls == cap
        assert len(rejected) == cap - 1
        assert len(adapter_rejected) == 1
        assert len(executor_rejected) == 1
        assert all(exchange.reject_started.wait(0.1) for exchange in rejected)
        assert all(exchange.exchange_calls == 0 for exchange in rejected)
        shared = first_reader if reused_side == "reader" else first_writer
        assert all(
            any(shared is preserved for preserved in exchange.preserved)
            for exchange in rejected
        )
    finally:
        release_reject.set()
        for exchange in rejected:
            exchange.release_complete.wait(0.2)
        first_reader.read_released.set()
        first_exchange.release_complete.wait(0.2)

    assert first_reader.close_calls == 1
    assert first_writer.close_calls == 1
    if reused_side == "reader":
        assert all(exchange._writer.close_calls == 1 for exchange in rejected)
    else:
        assert all(exchange._reader.close_calls == 1 for exchange in rejected)


@pytest.mark.parametrize("pending_side", ("reader", "writer"))
def test_blocked_rejection_reserves_its_unique_endpoint_across_port_types(
    monkeypatch: pytest.MonkeyPatch,
    pending_side: str,
):
    """Dropping rejected identities must admit their cleanup-owned half to new I/O."""
    monkeypatch.setattr(adapter_module, "_MAX_LIVE_CHANNELS", 4)
    first_reader = CloseReturningBlockedReader()
    first_writer = CloseTrackingWriter()
    release_reject = threading.Event()
    return_reject = threading.Event()
    first_exchange = BoundedJsonLinesExchange(
        first_reader,  # type: ignore[arg-type]
        first_writer,  # type: ignore[arg-type]
        deadline_seconds=0.01,
    )
    first_adapter = JsonLinesPolicyAdapter(lambda: first_exchange)
    request = AdapterRequest(
        1, _wake(1), "2026-08-02T09:10:11Z", None, HEALTHY_BUDGET
    )

    pending_reader = (
        CloseReturningBlockedReader() if pending_side == "reader" else first_reader
    )
    pending_writer = (
        CloseTrackingWriter() if pending_side == "writer" else first_writer
    )
    pending_endpoint = pending_reader if pending_side == "reader" else pending_writer
    original_reject = ObservableBlockingRejectExchange(
        pending_reader,
        pending_writer,
        release_reject,
        "{}",
        return_after_release=return_reject,
    )
    original_executor = JsonLinesCapabilityExecutor(
        {"chat": lambda: original_reject}
    )

    adapter_other = (
        CloseTrackingWriter()
        if pending_side == "reader"
        else CloseReturningBlockedReader()
    )
    adapter_identity = (
        (pending_endpoint, adapter_other)
        if pending_side == "reader"
        else (adapter_other, pending_endpoint)
    )
    adapter_reuse = ObservableBlockingRejectExchange(
        adapter_identity[0],
        adapter_identity[1],
        release_reject,
        _noop_response("unsafe-adapter-reuse").decode().strip(),
    )
    reuse_adapter = JsonLinesPolicyAdapter(lambda: adapter_reuse)

    executor_other = (
        CloseTrackingWriter()
        if pending_side == "reader"
        else CloseReturningBlockedReader()
    )
    executor_identity = (
        (pending_endpoint, executor_other)
        if pending_side == "reader"
        else (executor_other, pending_endpoint)
    )
    executor_response = json.dumps(
        {
            "schema_version": 1,
            "action_key": "action-3",
            "status": "completed",
            "result_sha256": "c" * 64,
            "reason_code": "unsafe-executor-reuse",
        },
        separators=(",", ":"),
    )
    executor_reuse = ObservableBlockingRejectExchange(
        executor_identity[0],
        executor_identity[1],
        release_reject,
        executor_response,
    )
    reuse_executor = JsonLinesCapabilityExecutor({"chat": lambda: executor_reuse})

    try:
        with pytest.raises(PortExchangeError, match="timed out"):
            first_adapter.observe_and_decide(request)
        first_result = original_executor.execute(_action("action-2"))
        assert first_result.reason_code == "executor-exchange-ambiguous"
        assert original_reject.reject_started.wait(0.1)

        adapter_error: PortExchangeError | None = None
        try:
            reuse_adapter.observe_and_decide(request)
        except PortExchangeError as error:
            adapter_error = error
        executor_result = reuse_executor.execute(_action("action-3"))

        assert adapter_error is not None
        assert "fresh channel" in str(adapter_error)
        assert executor_result.reason_code == "executor-exchange-ambiguous"
        assert adapter_reuse.reject_started.wait(0.1)
        assert executor_reuse.reject_started.wait(0.1)
        assert original_reject.exchange_calls == 0
        assert adapter_reuse.exchange_calls == 0
        assert executor_reuse.exchange_calls == 0
        assert pending_endpoint.close_calls == 0
        assert any(
            pending_endpoint is preserved for preserved in adapter_reuse.preserved
        )
        assert any(
            pending_endpoint is preserved for preserved in executor_reuse.preserved
        )

        release_reject.set()
        for exchange in (original_reject, adapter_reuse, executor_reuse):
            assert exchange.release_complete.wait(0.2)
        assert original_reject.reject_returned.is_set() is False

        # One live channel plus the reject callback blocked after release must
        # fill the cap. Its factory cannot run and create another worker.
        monkeypatch.setattr(adapter_module, "_MAX_LIVE_CHANNELS", 2)
        blocked_factory_calls = 0

        def blocked_factory() -> CompletingIdentityExchange:
            nonlocal blocked_factory_calls
            blocked_factory_calls += 1
            return CompletingIdentityExchange(
                (object(), object()),
                _noop_response("unsafe-worker-growth").decode().strip(),
            )

        with pytest.raises(PortExchangeError, match="too many channels"):
            JsonLinesPolicyAdapter(blocked_factory).observe_and_decide(request)
        assert blocked_factory_calls == 0

        return_reject.set()
        assert original_reject.reject_returned.wait(0.2)
        post_adapter_identity = (
            (pending_endpoint, object())
            if pending_side == "reader"
            else (object(), pending_endpoint)
        )
        post_adapter_exchange = CompletingIdentityExchange(
            post_adapter_identity,
            _noop_response("identity-retired").decode().strip(),
        )
        post_adapter = JsonLinesPolicyAdapter(lambda: post_adapter_exchange)
        deadline = time.monotonic() + 0.2
        while True:
            try:
                post_decision = post_adapter.observe_and_decide(request)
                break
            except PortExchangeError as error:
                assert "too many channels" in str(error)
                if time.monotonic() >= deadline:
                    raise AssertionError(
                        "returned reject callback did not release capacity"
                    ) from error
                time.sleep(0.001)
        assert post_decision.reason_code == "identity-retired"
        assert post_adapter_exchange.exchange_calls == 1

        post_executor_identity = (
            (pending_endpoint, object())
            if pending_side == "reader"
            else (object(), pending_endpoint)
        )
        post_executor_exchange = CompletingIdentityExchange(
            post_executor_identity,
            json.dumps(
                {
                    "schema_version": 1,
                    "action_key": "action-4",
                    "status": "completed",
                    "result_sha256": "d" * 64,
                    "reason_code": "identity-retired",
                },
                separators=(",", ":"),
            ),
        )
        post_executor = JsonLinesCapabilityExecutor(
            {"chat": lambda: post_executor_exchange}
        )
        assert post_executor.execute(_action("action-4")) == EffectResult(
            "completed", "d" * 64, "identity-retired"
        )
        assert post_executor_exchange.exchange_calls == 1
    finally:
        release_reject.set()
        return_reject.set()
        first_reader.read_released.set()
        first_exchange.release_complete.wait(0.2)

    assert pending_endpoint.close_calls == 1
    assert first_reader.close_calls == 1
    assert first_writer.close_calls == 1
    assert adapter_other.close_calls == 1
    assert executor_other.close_calls == 1


def test_reject_callback_exception_releases_capacity_after_cleanup(
    monkeypatch: pytest.MonkeyPatch,
):
    """Returning early on a reject exception must strand its admission forever."""
    monkeypatch.setattr(adapter_module, "_MAX_LIVE_CHANNELS", 2)
    first_reader = CloseReturningBlockedReader()
    first_writer = CloseTrackingWriter()
    first_exchange = BoundedJsonLinesExchange(
        first_reader,  # type: ignore[arg-type]
        first_writer,  # type: ignore[arg-type]
        deadline_seconds=0.01,
    )
    request = AdapterRequest(
        1, _wake(1), "2026-08-02T09:10:11Z", None, HEALTHY_BUDGET
    )
    first_adapter = JsonLinesPolicyAdapter(lambda: first_exchange)
    release_reject = threading.Event()
    exception_reject = ObservableBlockingRejectExchange(
        first_reader,
        first_writer,
        release_reject,
        "{}",
        raise_after_release=True,
    )
    exception_executor = JsonLinesCapabilityExecutor(
        {"chat": lambda: exception_reject}
    )

    try:
        with pytest.raises(PortExchangeError, match="timed out"):
            first_adapter.observe_and_decide(request)
        result = exception_executor.execute(_action("action-5"))
        assert result.reason_code == "executor-exchange-ambiguous"
        assert exception_reject.reject_started.wait(0.1)
        assert any(first_reader is item for item in exception_reject.preserved)
        assert any(first_writer is item for item in exception_reject.preserved)

        release_reject.set()
        assert exception_reject.release_complete.wait(0.2)
        assert exception_reject.reject_returned.wait(0.2)

        factory_calls = 0
        completing_exchange = CompletingIdentityExchange(
            (object(), object()),
            _noop_response("exception-capacity-retired").decode().strip(),
        )

        def completing_factory() -> CompletingIdentityExchange:
            nonlocal factory_calls
            factory_calls += 1
            return completing_exchange

        deadline = time.monotonic() + 0.2
        while True:
            try:
                decision = JsonLinesPolicyAdapter(
                    completing_factory
                ).observe_and_decide(request)
                break
            except PortExchangeError as error:
                assert "too many channels" in str(error)
                if time.monotonic() >= deadline:
                    raise AssertionError(
                        "reject callback exception did not release capacity"
                    ) from error
                time.sleep(0.001)

        assert decision.reason_code == "exception-capacity-retired"
        assert factory_calls == 1
        assert completing_exchange.exchange_calls == 1
    finally:
        release_reject.set()
        first_reader.read_released.set()
        first_exchange.release_complete.wait(0.2)

    assert first_reader.close_calls == 1
    assert first_writer.close_calls == 1


def test_policy_adapter_uses_closed_bounded_fresh_json_line():
    """Changing the production adapter framing must bypass the reviewed contract parser."""
    request_output = CloseTrackingWriter()
    adapter = JsonLinesPolicyAdapter(
        lambda: BoundedJsonLinesExchange(
            BytesIO(_noop_response("observed-current")),
            request_output,  # type: ignore[arg-type]
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
    request_output = CloseTrackingWriter()
    executor = JsonLinesCapabilityExecutor(
        {
            "chat": lambda: BoundedJsonLinesExchange(
                response,
                request_output,  # type: ignore[arg-type]
                deadline_seconds=1,
            )
        }
    )

    assert executor.execute(_action()) == EffectResult("completed", "c" * 64, "accepted")
    written = json.loads(request_output.getvalue())
    assert written["capability_name"] == "chat"
    assert written["action_key"] == "action-1"
    assert response.closed is True
    assert request_output.closed.is_set()
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


@pytest.mark.parametrize("reused_side", ("reader", "writer"))
def test_external_executor_rejects_either_reused_live_stream(reused_side: str):
    """Executor confinement applies to each stream, even after close returns."""
    first_reader = CloseReturningBlockedReader()
    first_writer = CloseTrackingWriter()
    second_reader = first_reader if reused_side == "reader" else CloseReturningBlockedReader()
    second_writer = first_writer if reused_side == "writer" else CloseTrackingWriter()
    exchanges = deque(
        (
            BoundedJsonLinesExchange(
                first_reader,  # type: ignore[arg-type]
                first_writer,  # type: ignore[arg-type]
                deadline_seconds=0.02,
            ),
            BoundedJsonLinesExchange(
                second_reader,  # type: ignore[arg-type]
                second_writer,  # type: ignore[arg-type]
                deadline_seconds=0.02,
            ),
        )
    )
    executor = JsonLinesCapabilityExecutor({"chat": exchanges.popleft})

    try:
        first = executor.execute(_action())
        second = executor.execute(_action("action-2"))

        assert first.reason_code == "executor-exchange-ambiguous"
        assert second.reason_code == "executor-exchange-ambiguous"
        assert first_reader.read_calls == 1
        if reused_side == "reader":
            assert second_writer.write_calls == 0
            assert second_writer.closed.wait(0.1)
        else:
            assert second_reader.read_calls == 0
            assert second_reader.close_called.wait(0.1)
    finally:
        first_reader.read_released.set()
        second_reader.read_released.set()
