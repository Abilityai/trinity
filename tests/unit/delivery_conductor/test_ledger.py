"""Behavior tests for the delivery-conductor's local SQLite control ledger."""
# ruff: noqa: E402
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

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

from delivery_conductor.contracts import MAX_MESSAGE_BYTES, ProposedAction, Wake
from delivery_conductor.ledger import (
    ActionConflictError,
    Checkpoint,
    ControlLedger,
    EffectResult,
    LedgerValidationError,
    StaleLeaseError,
    TickOutcome,
)


NOW = datetime(2026, 8, 2, 9, 10, 11, tzinfo=timezone.utc)


def _wake(number: int) -> Wake:
    return Wake(
        wake_id=f"wake-{number}",
        source="event",
        source_event_id=f"event-{number}",
        payload_sha256=f"{number:x}" * 64,
    )


def _action(action_key: str = "action-1") -> ProposedAction:
    return ProposedAction(
        capability_name="chat",
        action_key=action_key,
        payload_json='{"references":{"digest":"' + "a" * 64 + '","identifier":"target-1"}}',
        target_revision="repo-4",
        invalidation_class="observation-change",
    )


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    return tmp_path / "control.db"


@pytest.fixture
def ledger(database_path: Path) -> ControlLedger:
    value = ControlLedger(database_path)
    value.initialize()
    return value


def _fetchall(database_path: Path, query: str) -> list[tuple[object, ...]]:
    with sqlite3.connect(database_path) as connection:
        return connection.execute(query).fetchall()


def test_initialize_enables_wal_foreign_keys_and_all_control_tables(
    ledger: ControlLedger, database_path: Path
):
    """Dropping a required table or connection pragma must break durable safety."""
    with ledger._connect() as connection:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]

    tables = {
        row[0]
        for row in _fetchall(
            database_path,
            "SELECT name FROM sqlite_master WHERE type = 'table'",
        )
    }
    assert journal_mode == "wal"
    assert foreign_keys == 1
    assert {
        "event_inbox",
        "repo_lease",
        "action_journal",
        "action_events",
        "run_checkpoint",
        "budget_usage",
        "controller_state",
    } <= tables


def test_claim_wake_deduplicates_one_stable_source_event(
    ledger: ControlLedger, database_path: Path
):
    """A duplicate at-least-once delivery must never create duplicate inbox work."""
    first = ledger.claim_wake(_wake(1), NOW, lease_seconds=30)
    assert first is not None
    assert ledger.claim_wake(_wake(1), NOW, lease_seconds=30) is None

    ledger.release(first, TickOutcome(True, "completed", 0, 0, 0))
    assert ledger.claim_wake(_wake(1), NOW + timedelta(minutes=1), lease_seconds=30) is None
    assert _fetchall(database_path, "SELECT wake_id, state FROM event_inbox") == [
        ("wake-1", "acknowledged")
    ]


def test_claim_wake_rejects_a_conflicting_deduplication_identity(ledger: ControlLedger):
    """Reusing a source event for different bytes must fail closed rather than alias data."""
    assert ledger.claim_wake(_wake(1), NOW, lease_seconds=30) is not None
    conflicting = Wake("wake-other", "event", "event-1", "f" * 64)

    with pytest.raises(LedgerValidationError, match="conflicting wake"):
        ledger.claim_wake(conflicting, NOW, lease_seconds=30)


def test_only_one_connection_holds_the_repository_lease(database_path: Path):
    """A second SQLite connection cannot claim work while the first lease is live."""
    first_ledger = ControlLedger(database_path)
    second_ledger = ControlLedger(database_path)
    first_ledger.initialize()

    first = first_ledger.claim_wake(_wake(1), NOW, lease_seconds=30)
    assert first is not None
    assert second_ledger.claim_wake(_wake(2), NOW, lease_seconds=30) is None
    assert _fetchall(database_path, "SELECT wake_id, fence_token FROM repo_lease") == [
        ("wake-1", 1)
    ]


def test_expiry_advances_the_fence_and_rejects_the_stale_holder(database_path: Path):
    """Removing lease fencing must let an expired worker overwrite its successor."""
    first_ledger = ControlLedger(database_path)
    second_ledger = ControlLedger(database_path)
    first_ledger.initialize()
    first = first_ledger.claim_wake(_wake(1), NOW, lease_seconds=10)
    assert first is not None

    second = second_ledger.claim_wake(_wake(2), NOW + timedelta(seconds=10), lease_seconds=10)
    assert second is not None
    assert second.fence_token == 2
    with pytest.raises(StaleLeaseError):
        first_ledger.checkpoint(
            first,
            Checkpoint("checkpoint-1", "b" * 64, "wake-1", "progress", 3, 4, 5),
        )

    second_ledger.release(second, TickOutcome(False, "retry", 0, 0, 0))
    third = first_ledger.claim_wake(_wake(3), NOW + timedelta(seconds=20), lease_seconds=10)
    assert third is not None
    assert third.fence_token == 3


def test_release_without_acknowledgement_leaves_the_wake_recoverable(ledger: ControlLedger):
    """A failed tick must not consume its wake merely because it released the lease."""
    first = ledger.claim_wake(_wake(1), NOW, lease_seconds=10)
    assert first is not None
    ledger.release(first, TickOutcome(False, "retry", 0, 0, 0))

    replay = ledger.claim_wake(_wake(1), NOW + timedelta(seconds=1), lease_seconds=10)
    assert replay is not None
    assert replay.fence_token == 2


def test_claim_wake_requires_caller_supplied_aware_utc_time(ledger: ControlLedger):
    """A local or naive clock must not make lease expiry nondeterministic."""
    with pytest.raises(LedgerValidationError, match="aware UTC"):
        ledger.claim_wake(_wake(1), NOW.replace(tzinfo=None), lease_seconds=10)
    with pytest.raises(LedgerValidationError, match="positive integer"):
        ledger.claim_wake(_wake(1), NOW, lease_seconds=0)


def test_reservation_persists_one_canonical_capped_reference_payload(
    ledger: ControlLedger, database_path: Path
):
    """Bypassing canonicalization or action-key idempotency must corrupt safe replay."""
    lease = ledger.claim_wake(_wake(1), NOW, lease_seconds=30)
    assert lease is not None
    action = _action()

    first = ledger.reserve_action(lease, action)
    replay = ledger.reserve_action(lease, action)

    assert first == replay
    assert first.status == "reserved"
    assert first.payload_sha256 == hashlib.sha256(action.payload_json.encode()).hexdigest()
    assert _fetchall(
        database_path,
        "SELECT action_key, status, payload_json FROM action_journal",
    ) == [("action-1", "reserved", action.payload_json)]

    with pytest.raises(ActionConflictError):
        ledger.reserve_action(lease, replace(action, target_revision="repo-5"))


def test_reservation_revalidates_and_rejects_oversized_or_sensitive_payloads(
    ledger: ControlLedger, database_path: Path
):
    """A direct caller must not persist over-limit, secret-like, or forged PII content."""
    lease = ledger.claim_wake(_wake(1), NOW, lease_seconds=30)
    assert lease is not None
    oversized = ProposedAction(
        "chat",
        "action-large",
        json.dumps(
            {"digests": ["a" * 64] * 16000},
            separators=(",", ":"),
            sort_keys=True,
        ),
        "repo-4",
        "observation-change",
    )
    assert len(oversized.payload_json.encode()) > MAX_MESSAGE_BYTES
    with pytest.raises(LedgerValidationError, match="1 MiB"):
        ledger.reserve_action(lease, oversized)

    secret_marker = "gh" + "p_" + "abcdefghijklmnopqrstuvwxyz0123456789"
    secret_like = ProposedAction(
        "chat",
        "action-secret",
        json.dumps({"identifier": secret_marker}, separators=(",", ":"), sort_keys=True),
        "repo-4",
        "observation-change",
    )
    with pytest.raises(LedgerValidationError, match="sensitive"):
        ledger.reserve_action(lease, secret_like)

    forged = _action("action-forged")
    pii_marker = "person" + chr(64) + "example.com"
    object.__setattr__(forged, "payload_json", json.dumps({"email": pii_marker}))
    with pytest.raises(LedgerValidationError):
        ledger.reserve_action(lease, forged)

    assert _fetchall(database_path, "SELECT action_key FROM action_journal") == []


def test_completed_result_is_returned_on_action_replay(ledger: ControlLedger):
    """Losing a completed replay branch must expose a completed action as executable."""
    lease = ledger.claim_wake(_wake(1), NOW, lease_seconds=30)
    assert lease is not None
    action = _action()
    ledger.reserve_action(lease, action)
    ledger.record_result(lease, action.action_key, EffectResult("completed", "c" * 64, "ok"))

    replay = ledger.reserve_action(lease, action)
    assert replay.status == "completed"
    assert replay.result_sha256 == "c" * 64
    assert replay.reason_code == "ok"


def test_ambiguous_result_is_terminal_for_immediate_replay(ledger: ControlLedger):
    """Treating an ambiguous effect as merely reserved could execute it a second time."""
    lease = ledger.claim_wake(_wake(1), NOW, lease_seconds=30)
    assert lease is not None
    action = _action()
    ledger.reserve_action(lease, action)
    result = EffectResult("ambiguous", "d" * 64, "result-unknown")
    ledger.record_result(lease, action.action_key, result)

    replay = ledger.reserve_action(lease, action)
    assert replay.status == "ambiguous"
    assert replay.result_sha256 == "d" * 64
    with pytest.raises(ActionConflictError):
        ledger.record_result(
            lease,
            action.action_key,
            EffectResult("completed", "e" * 64, "late-success"),
        )


def test_new_lease_must_replay_a_reservation_before_recording_its_result(
    ledger: ControlLedger,
):
    """A current repo lease must not finalize a reservation owned by an older fence."""
    action = _action()
    first = ledger.claim_wake(_wake(1), NOW, lease_seconds=10)
    assert first is not None
    ledger.reserve_action(first, action)

    second = ledger.claim_wake(_wake(2), NOW + timedelta(seconds=10), lease_seconds=10)
    assert second is not None
    with pytest.raises(StaleLeaseError, match="reservation fence"):
        ledger.record_result(
            second,
            action.action_key,
            EffectResult("completed", "c" * 64, "ok"),
        )

    assert ledger.reserve_action(second, action).status == "reserved"
    ledger.record_result(second, action.action_key, EffectResult("completed", "c" * 64, "ok"))


def test_checkpoint_replaces_prior_state_and_carries_a_verified_action_outcome(
    ledger: ControlLedger, database_path: Path
):
    """Appending checkpoints or accepting invented action outcomes breaks recovery."""
    lease = ledger.claim_wake(_wake(1), NOW, lease_seconds=30)
    assert lease is not None
    action = _action()
    ledger.reserve_action(lease, action)
    ledger.record_result(lease, action.action_key, EffectResult("completed", "c" * 64, "ok"))

    ledger.checkpoint(
        lease,
        Checkpoint("checkpoint-1", "1" * 64, "wake-1", "progress", 8, 9, 10),
    )
    ledger.checkpoint(
        lease,
        Checkpoint(
            "checkpoint-2",
            "2" * 64,
            "wake-1",
            "effect-completed",
            7,
            8,
            9,
            action_key="action-1",
            action_status="completed",
            action_result_sha256="c" * 64,
        ),
    )

    assert _fetchall(
        database_path,
        "SELECT revision, fence_token, action_key, action_status, action_result_sha256 "
        "FROM run_checkpoint",
    ) == [("checkpoint-2", 1, "action-1", "completed", "c" * 64)]
    with pytest.raises(LedgerValidationError, match="action outcome"):
        ledger.checkpoint(
            lease,
            Checkpoint(
                "checkpoint-3",
                "3" * 64,
                "wake-1",
                "effect-completed",
                7,
                8,
                9,
                action_key="action-1",
                action_status="completed",
                action_result_sha256="f" * 64,
            ),
        )


def test_release_accumulates_budget_usage_without_overwriting_history(
    ledger: ControlLedger, database_path: Path
):
    """Replacing usage rows would restore spent run, issue, or daily budget."""
    first = ledger.claim_wake(_wake(1), NOW, lease_seconds=30)
    assert first is not None
    ledger.release(first, TickOutcome(True, "completed", 1, 2, 3))
    second = ledger.claim_wake(_wake(2), NOW + timedelta(seconds=1), lease_seconds=30)
    assert second is not None
    ledger.release(second, TickOutcome(True, "completed", 4, 5, 6))

    assert _fetchall(
        database_path,
        "SELECT SUM(run_units), SUM(issue_units), SUM(daily_units) FROM budget_usage",
    ) == [(5, 7, 9)]


def test_action_events_are_database_enforced_append_only(
    ledger: ControlLedger, database_path: Path
):
    """Removing the SQLite guards must allow audit history to be rewritten or erased."""
    lease = ledger.claim_wake(_wake(1), NOW, lease_seconds=30)
    assert lease is not None
    action = _action()
    ledger.reserve_action(lease, action)
    ledger.record_result(lease, action.action_key, EffectResult("completed", "c" * 64, "ok"))

    with sqlite3.connect(database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE action_events SET event_type = 'ambiguous'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM action_events")

    assert _fetchall(database_path, "SELECT event_type FROM action_events ORDER BY id") == [
        ("reserved",),
        ("completed",),
    ]
