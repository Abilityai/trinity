"""One fenced, two-phase, one-effect delivery-conductor control tick."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import sqlite3
from typing import Literal

from .adapter import PolicyAdapterPort
from .contracts import (
    AdapterDecision,
    AdapterRequest,
    BudgetView,
    CheckpointView,
    ProposedAction,
    ReminderSpec,
    Wake,
)
from .ledger import (
    ActionReservation,
    Checkpoint,
    ControlLedger,
    EffectResult,
    Lease,
    TickOutcome,
)


TickStatus = Literal[
    "not-claimed",
    "rejected",
    "blocked",
    "noop",
    "action-ready",
    "reminder-ready",
    "reminder",
    "completed",
    "investigate",
]
HandoffKind = Literal["action", "reminder"]

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_REMINDER_REFERENCE_MARKER = "delivery-conductor-reminder-v1"


class TickCorrelationError(ValueError):
    """Raised when a result does not match the prepared action and fence."""


@dataclass(frozen=True)
class TickHandoff:
    """Safe state required to accept one externally produced result."""

    kind: HandoffKind
    lease: Lease
    observed_revision: str
    budget_view: BudgetView
    action: ProposedAction
    payload_sha256: str
    reminder: ReminderSpec | None = None

    def __post_init__(self) -> None:
        if self.kind not in ("action", "reminder"):
            raise TickCorrelationError("handoff kind is invalid")
        if not isinstance(self.lease, Lease):
            raise TickCorrelationError("handoff lease is invalid")
        if not _is_identifier(self.observed_revision):
            raise TickCorrelationError("handoff revision is invalid")
        if not isinstance(self.budget_view, BudgetView):
            raise TickCorrelationError("handoff budget is invalid")
        if not isinstance(self.action, ProposedAction):
            raise TickCorrelationError("handoff action is invalid")
        if not re.fullmatch(r"[a-f0-9]{64}", self.payload_sha256):
            raise TickCorrelationError("handoff payload digest is invalid")
        if self.kind == "reminder" and not isinstance(self.reminder, ReminderSpec):
            raise TickCorrelationError("reminder handoff requires a reminder")
        if self.reminder is not None and not isinstance(self.reminder, ReminderSpec):
            raise TickCorrelationError("handoff reminder is invalid")


@dataclass(frozen=True)
class TickResult:
    """Sanitized result of one phase and at most one executable action."""

    status: TickStatus
    reason_code: str
    action: ProposedAction | None = None
    handoff: TickHandoff | None = None
    action_key: str | None = None
    action_status: str | None = None
    result_sha256: str | None = None
    reminder: ReminderSpec | None = None


class DeliveryConductorTick:
    """Prepare one effect, then accept its correlated sanitized result."""

    def __init__(
        self,
        *,
        ledger: ControlLedger,
        adapter: PolicyAdapterPort,
        installed_capabilities: frozenset[str],
        lease_seconds: int,
        ambiguity_reminder_seconds: int = 300,
    ) -> None:
        if not isinstance(ledger, ControlLedger):
            raise TypeError("ledger must be a ControlLedger")
        if (
            not isinstance(installed_capabilities, frozenset)
            or any(not _is_identifier(value) for value in installed_capabilities)
        ):
            raise ValueError("installed_capabilities must contain sanitized identifiers")
        if type(lease_seconds) is not int or lease_seconds <= 0:
            raise ValueError("lease_seconds must be a positive integer")
        if type(ambiguity_reminder_seconds) is not int or ambiguity_reminder_seconds <= 0:
            raise ValueError("ambiguity_reminder_seconds must be a positive integer")
        self._ledger = ledger
        self._adapter = adapter
        self._installed_capabilities = installed_capabilities
        self._lease_seconds = lease_seconds
        self._ambiguity_reminder_seconds = ambiguity_reminder_seconds

    def run(
        self,
        wake: Wake,
        now: datetime,
        checkpoint: CheckpointView | None,
        budget_view: BudgetView,
        *,
        breaker_allows_effect: bool,
    ) -> TickResult:
        """Prepare zero or one effect without invoking a capability."""
        request = AdapterRequest(
            schema_version=1,
            wake=wake,
            now_utc=_utc_timestamp(now),
            checkpoint=checkpoint,
            budget_view=budget_view,
        )
        lease = self._ledger.claim_wake(wake, now, self._lease_seconds)
        if lease is None:
            return TickResult("not-claimed", "wake-not-claimed")

        try:
            recovered_reminder = self._load_recoverable_reminder(lease.wake_id)
        except (RuntimeError, sqlite3.Error, ValueError):
            return self._reject(
                lease,
                "reminder-recovery-invalid",
                status="blocked",
            )
        if recovered_reminder is not None:
            action, reminder = recovered_reminder
            return self._prepare_reminder_action(
                lease,
                action,
                reminder,
                budget_view,
                breaker_allows_effect,
            )

        try:
            decision = self._adapter.observe_and_decide(request)
        except Exception:
            return self._reject(lease, "adapter-unavailable", status="blocked")
        if not _valid_decision(decision):
            return self._reject(lease, "invalid-adapter-decision")

        if decision.decision != "execute":
            if decision.next_reminder is not None:
                return self._prepare_reminder(
                    lease,
                    decision.observed_revision,
                    decision.next_reminder,
                    budget_view,
                    breaker_allows_effect,
                )
            return self._finish_noop(lease, decision, budget_view)

        action = decision.proposed_action
        if action is None:
            return self._reject(lease, "invalid-adapter-decision")
        if action.capability_name not in self._installed_capabilities:
            return self._reject(lease, "capability-not-installed")
        if decision.next_reminder is not None:
            try:
                action = _with_embedded_reminder(action, decision.next_reminder)
            except (TypeError, ValueError):
                return self._reject(lease, "invalid-adapter-decision")

        reservation = self._ledger.reserve_action(lease, action)
        if reservation.status == "completed":
            if decision.next_reminder is not None:
                return self._prepare_reminder(
                    lease,
                    decision.observed_revision,
                    decision.next_reminder,
                    budget_view,
                    breaker_allows_effect,
                    source_action_key=action.action_key,
                )
            return self._finish_terminal_replay(
                lease, decision.observed_revision, budget_view, reservation
            )
        if reservation.status == "ambiguous":
            reminder = decision.next_reminder
            if reminder is None:
                reminder = self._terminal_investigation_reminder(
                    action.action_key,
                    reservation.reason_code or "result-unknown",
                )
            return self._prepare_reminder(
                lease,
                decision.observed_revision,
                reminder,
                budget_view,
                breaker_allows_effect,
                source_action_key=action.action_key,
            )

        gate_reason = _effect_gate_reason(budget_view, breaker_allows_effect)
        if gate_reason is not None:
            return self._block_reserved(
                lease,
                decision.observed_revision,
                budget_view,
                reservation,
                gate_reason,
            )

        self._write_checkpoint(
            lease,
            revision=decision.observed_revision,
            reason_code="action-ready",
            budget_view=budget_view,
            reservation=reservation,
        )
        handoff = TickHandoff(
            kind="action",
            lease=lease,
            observed_revision=decision.observed_revision,
            budget_view=budget_view,
            action=action,
            payload_sha256=reservation.payload_sha256,
            reminder=decision.next_reminder,
        )
        return TickResult(
            "action-ready",
            decision.reason_code,
            action=action,
            handoff=handoff,
            action_key=action.action_key,
            action_status="reserved",
            reminder=decision.next_reminder,
        )

    def accept_result(
        self,
        handoff: TickHandoff,
        *,
        action_key: str,
        result: EffectResult,
    ) -> TickResult:
        """Accept one prepared action result under its action key and fence."""
        self._validate_handoff(handoff, correlation=action_key)
        if not isinstance(result, EffectResult):
            raise TickCorrelationError("result must be a sanitized EffectResult")
        pending_reminder: ReminderSpec | None = None
        pending_reservation: ActionReservation | None = None
        if handoff.kind == "action" and result.status == "ambiguous":
            pending_reminder = handoff.reminder
            if pending_reminder is None:
                pending_reminder = self._terminal_investigation_reminder(
                    action_key,
                    result.reason_code,
                )
        elif handoff.kind == "action" and handoff.reminder is not None:
            pending_reminder = handoff.reminder
        self._ledger.record_result(handoff.lease, action_key, result)
        if pending_reminder is not None:
            reminder_action = _reminder_action(
                pending_reminder,
                handoff.observed_revision,
                source_action_key=action_key,
            )
            existing = self._load_reminder_by_key(reminder_action.action_key)
            if existing is not None:
                reminder_action, pending_reminder = existing
            pending_reservation = self._ledger.reserve_action(
                handoff.lease,
                reminder_action,
            )
        remaining_budget = _spend_one(handoff.budget_view)
        reservation = ActionReservation(
            action_key,
            result.status,
            handoff.payload_sha256,
            result.result_sha256,
            result.reason_code,
        )
        self._write_checkpoint(
            handoff.lease,
            revision=handoff.observed_revision,
            reason_code=result.reason_code,
            budget_view=remaining_budget,
            reservation=reservation,
        )
        if pending_reservation is not None:
            self._write_checkpoint(
                handoff.lease,
                revision=handoff.observed_revision,
                reason_code="reminder-pending",
                budget_view=remaining_budget,
                reservation=pending_reservation,
            )

        if handoff.kind == "reminder":
            acknowledged = result.status == "completed"
            self._ledger.release(
                handoff.lease,
                TickOutcome(acknowledged, result.reason_code, 1, 1, 1),
            )
            return TickResult(
                "reminder" if acknowledged else "investigate",
                result.reason_code,
                action_key=action_key,
                action_status=result.status,
                result_sha256=result.result_sha256,
                reminder=handoff.reminder,
            )

        if result.status == "ambiguous":
            self._ledger.release(
                handoff.lease,
                TickOutcome(False, result.reason_code, 1, 1, 1),
            )
            return TickResult(
                "investigate",
                result.reason_code,
                action_key=action_key,
                action_status="ambiguous",
                result_sha256=result.result_sha256,
                reminder=pending_reminder,
            )

        if pending_reminder is not None:
            self._ledger.release(
                handoff.lease,
                TickOutcome(False, "reminder-pending", 1, 1, 1),
            )
            return TickResult(
                "reminder",
                "reminder-pending",
                action_key=action_key,
                action_status="completed",
                result_sha256=result.result_sha256,
                reminder=pending_reminder,
            )

        self._ledger.release(
            handoff.lease,
            TickOutcome(True, result.reason_code, 1, 1, 1),
        )
        return TickResult(
            "completed",
            result.reason_code,
            action_key=action_key,
            action_status="completed",
            result_sha256=result.result_sha256,
        )

    def _validate_handoff(
        self,
        handoff: TickHandoff,
        *,
        correlation: str,
    ) -> None:
        if not isinstance(handoff, TickHandoff):
            raise TickCorrelationError("result does not correlate to the prepared handoff")
        if correlation != handoff.action.action_key:
            raise TickCorrelationError("result does not correlate to the prepared handoff")

    def _prepare_reminder(
        self,
        lease: Lease,
        revision: str,
        reminder: ReminderSpec,
        budget_view: BudgetView,
        breaker_allows_effect: bool,
        *,
        source_action_key: str | None = None,
    ) -> TickResult:
        action = _reminder_action(
            reminder,
            revision,
            source_action_key=source_action_key,
        )
        try:
            existing = self._load_reminder_by_key(action.action_key)
        except (RuntimeError, sqlite3.Error, ValueError):
            return self._reject(
                lease,
                "reminder-recovery-invalid",
                status="blocked",
            )
        if existing is not None:
            action, reminder = existing
        return self._prepare_reminder_action(
            lease,
            action,
            reminder,
            budget_view,
            breaker_allows_effect,
        )

    def _prepare_reminder_action(
        self,
        lease: Lease,
        action: ProposedAction,
        reminder: ReminderSpec,
        budget_view: BudgetView,
        breaker_allows_effect: bool,
    ) -> TickResult:
        if "reminders" not in self._installed_capabilities:
            return self._reject(lease, "capability-not-installed")
        try:
            _, source_action_key = _reminder_from_action(action)
        except (TypeError, ValueError):
            return self._reject(
                lease,
                "reminder-recovery-invalid",
                status="blocked",
            )
        if source_action_key is not None and not self._source_action_is_terminal(
            source_action_key
        ):
            return self._reject(
                lease,
                "reminder-source-not-terminal",
                status="blocked",
            )
        reservation = self._ledger.reserve_action(lease, action)
        if reservation.status != "reserved":
            return self._finish_terminal_replay(
                lease,
                action.target_revision,
                budget_view,
                reservation,
                reminder=reminder,
            )
        gate_reason = _effect_gate_reason(budget_view, breaker_allows_effect)
        if gate_reason is not None:
            return self._block_reserved(
                lease,
                action.target_revision,
                budget_view,
                reservation,
                gate_reason,
                reminder=reminder,
            )
        self._write_checkpoint(
            lease,
            revision=action.target_revision,
            reason_code="reminder-ready",
            budget_view=budget_view,
            reservation=reservation,
        )
        handoff = TickHandoff(
            kind="reminder",
            lease=lease,
            observed_revision=action.target_revision,
            budget_view=budget_view,
            action=action,
            payload_sha256=reservation.payload_sha256,
            reminder=reminder,
        )
        return TickResult(
            "reminder-ready",
            "reminder-ready",
            action=action,
            handoff=handoff,
            action_key=action.action_key,
            action_status="reserved",
            reminder=reminder,
        )

    def _load_recoverable_reminder(
        self,
        wake_id: str,
    ) -> tuple[ProposedAction, ReminderSpec] | None:
        with sqlite3.connect(self._ledger.database_path) as connection:
            connection.execute("PRAGMA query_only = ON")
            rows = connection.execute(
                """
                SELECT capability_name, action_key, payload_json,
                       target_revision, invalidation_class
                FROM action_journal AS journal
                WHERE capability_name = 'reminders'
                      AND (
                          wake_id = ?
                          OR EXISTS (
                              SELECT 1 FROM run_checkpoint AS checkpoint
                              WHERE checkpoint.acknowledged_wake_id = ?
                                    AND checkpoint.action_key = journal.action_key
                          )
                      )
                ORDER BY action_key
                """,
                (wake_id, wake_id),
            ).fetchall()
            source_rows = connection.execute(
                """
                SELECT journal.capability_name, journal.action_key,
                       journal.payload_json, journal.target_revision,
                       journal.invalidation_class, journal.status,
                       journal.reason_code, inbox.first_seen_at
                FROM action_journal AS journal
                JOIN event_inbox AS inbox ON inbox.wake_id = journal.wake_id
                WHERE journal.wake_id = ?
                      AND journal.capability_name != 'reminders'
                      AND journal.status IN ('completed', 'ambiguous')
                ORDER BY journal.action_key
                """,
                (wake_id,),
            ).fetchall()
        if len(rows) > 1:
            raise RuntimeError("wake has multiple recoverable reminder intents")
        if rows:
            action = ProposedAction(*rows[0])
            reminder, source_action_key = _reminder_from_action(action)
            if _reminder_action(
                reminder,
                action.target_revision,
                source_action_key=source_action_key,
            ) != action:
                raise RuntimeError("reserved reminder intent is not canonical")
            return action, reminder

        recovered: list[tuple[ProposedAction, ReminderSpec]] = []
        for row in source_rows:
            source_action = ProposedAction(*row[:5])
            reminder = _embedded_reminder(source_action)
            if reminder is None and row[5] == "ambiguous":
                reminder = _investigation_reminder(
                    source_action.action_key,
                    row[6] or "result-unknown",
                    _parse_utc_timestamp(row[7])
                    + timedelta(seconds=self._ambiguity_reminder_seconds),
                )
            if reminder is not None:
                recovered.append(
                    (
                        _reminder_action(
                            reminder,
                            source_action.target_revision,
                            source_action_key=source_action.action_key,
                        ),
                        reminder,
                    )
                )
        if len(recovered) > 1:
            raise RuntimeError("wake has multiple terminal reminder intents")
        return recovered[0] if recovered else None

    def _load_reminder_by_key(
        self,
        action_key: str,
    ) -> tuple[ProposedAction, ReminderSpec] | None:
        with sqlite3.connect(self._ledger.database_path) as connection:
            connection.execute("PRAGMA query_only = ON")
            row = connection.execute(
                """
                SELECT capability_name, action_key, payload_json,
                       target_revision, invalidation_class
                FROM action_journal
                WHERE action_key = ? AND capability_name = 'reminders'
                """,
                (action_key,),
            ).fetchone()
        if row is None:
            return None
        action = ProposedAction(*row)
        reminder, source_action_key = _reminder_from_action(action)
        if _reminder_action(
            reminder,
            action.target_revision,
            source_action_key=source_action_key,
        ) != action:
            raise RuntimeError("reserved reminder intent is not canonical")
        return action, reminder

    def _source_action_is_terminal(self, action_key: str) -> bool:
        with sqlite3.connect(self._ledger.database_path) as connection:
            connection.execute("PRAGMA query_only = ON")
            row = connection.execute(
                """
                SELECT status FROM action_journal
                WHERE action_key = ? AND capability_name != 'reminders'
                """,
                (action_key,),
            ).fetchone()
        return row is not None and row[0] in ("completed", "ambiguous")

    def _terminal_investigation_reminder(
        self,
        action_key: str,
        reason_code: str,
    ) -> ReminderSpec:
        with sqlite3.connect(self._ledger.database_path) as connection:
            connection.execute("PRAGMA query_only = ON")
            row = connection.execute(
                """
                SELECT inbox.first_seen_at
                FROM action_journal AS journal
                JOIN event_inbox AS inbox ON inbox.wake_id = journal.wake_id
                WHERE journal.action_key = ?
                      AND journal.capability_name != 'reminders'
                """,
                (action_key,),
            ).fetchone()
        if row is None:
            raise RuntimeError("source action has no durable wake time")
        return _investigation_reminder(
            action_key,
            reason_code,
            _parse_utc_timestamp(row[0])
            + timedelta(seconds=self._ambiguity_reminder_seconds),
        )

    def _block_reserved(
        self,
        lease: Lease,
        revision: str,
        budget_view: BudgetView,
        reservation: ActionReservation,
        reason_code: str,
        *,
        reminder: ReminderSpec | None = None,
    ) -> TickResult:
        self._write_checkpoint(
            lease,
            revision=revision,
            reason_code=reason_code,
            budget_view=budget_view,
            reservation=reservation,
        )
        self._ledger.release(lease, TickOutcome(False, reason_code, 0, 0, 0))
        return TickResult(
            "blocked",
            reason_code,
            action_key=reservation.action_key,
            action_status="reserved",
            reminder=reminder,
        )

    def _reject(
        self,
        lease: Lease,
        reason_code: str,
        *,
        status: Literal["rejected", "blocked"] = "rejected",
    ) -> TickResult:
        self._ledger.release(lease, TickOutcome(False, reason_code, 0, 0, 0))
        return TickResult(status, reason_code)

    def _finish_noop(
        self,
        lease: Lease,
        decision: AdapterDecision,
        budget_view: BudgetView,
    ) -> TickResult:
        self._write_checkpoint(
            lease,
            revision=decision.observed_revision,
            reason_code=decision.reason_code,
            budget_view=budget_view,
        )
        self._ledger.release(
            lease,
            TickOutcome(True, decision.reason_code, 0, 0, 0),
        )
        return TickResult("noop", decision.reason_code)

    def _finish_terminal_replay(
        self,
        lease: Lease,
        revision: str,
        budget_view: BudgetView,
        reservation: ActionReservation,
        *,
        reminder: ReminderSpec | None = None,
    ) -> TickResult:
        if reservation.reason_code is None:
            raise RuntimeError("terminal reservation has no reason code")
        self._write_checkpoint(
            lease,
            revision=revision,
            reason_code=reservation.reason_code,
            budget_view=budget_view,
            reservation=reservation,
        )
        acknowledged = reservation.status == "completed"
        self._ledger.release(
            lease,
            TickOutcome(acknowledged, reservation.reason_code, 0, 0, 0),
        )
        return TickResult(
            "reminder" if reminder and acknowledged else (
                "completed" if acknowledged else "investigate"
            ),
            reservation.reason_code,
            action_key=reservation.action_key,
            action_status=reservation.status,
            result_sha256=reservation.result_sha256,
            reminder=reminder,
        )

    def _write_checkpoint(
        self,
        lease: Lease,
        *,
        revision: str,
        reason_code: str,
        budget_view: BudgetView,
        reservation: ActionReservation | None = None,
    ) -> None:
        checkpoint_fields: dict[str, object] = {
            "revision": revision,
            "wake_id": lease.wake_id,
            "fence_token": lease.fence_token,
            "reason_code": reason_code,
            "run_units_remaining": budget_view.run_units_remaining,
            "issue_units_remaining": budget_view.issue_units_remaining,
            "daily_units_remaining": budget_view.daily_units_remaining,
        }
        if reservation is not None:
            checkpoint_fields.update(
                {
                    "action_key": reservation.action_key,
                    "action_status": reservation.status,
                    "action_result_sha256": reservation.result_sha256,
                }
            )
        checkpoint_sha256 = hashlib.sha256(
            json.dumps(checkpoint_fields, separators=(",", ":"), sort_keys=True).encode(
                "utf-8"
            )
        ).hexdigest()
        self._ledger.checkpoint(
            lease,
            Checkpoint(
                revision=revision,
                checkpoint_sha256=checkpoint_sha256,
                acknowledged_wake_id=lease.wake_id,
                reason_code=reason_code,
                run_units_remaining=budget_view.run_units_remaining,
                issue_units_remaining=budget_view.issue_units_remaining,
                daily_units_remaining=budget_view.daily_units_remaining,
                action_key=reservation.action_key if reservation else None,
                action_status=reservation.status if reservation else None,
                action_result_sha256=reservation.result_sha256 if reservation else None,
            ),
        )


def _valid_decision(value: object) -> bool:
    if not isinstance(value, AdapterDecision):
        return False
    if value.decision == "execute":
        action = value.proposed_action
        return (
            action is not None
            and value.target_id is not None
            and action.target_revision == value.observed_revision
        )
    if value.decision == "noop":
        return value.target_id is None and value.proposed_action is None
    if value.decision in ("remind", "investigate"):
        return (
            value.target_id is None
            and value.proposed_action is None
            and value.next_reminder is not None
        )
    return False


def _effect_gate_reason(
    budget_view: BudgetView,
    breaker_allows_effect: object,
) -> str | None:
    if type(breaker_allows_effect) is not bool:
        return "invalid-breaker-state"
    if not breaker_allows_effect:
        return "breaker-open"
    if min(
        budget_view.run_units_remaining,
        budget_view.issue_units_remaining,
        budget_view.daily_units_remaining,
    ) < 1:
        return "budget-exhausted"
    return None


def _spend_one(budget_view: BudgetView) -> BudgetView:
    return BudgetView(
        budget_view.run_units_remaining - 1,
        budget_view.issue_units_remaining - 1,
        budget_view.daily_units_remaining - 1,
    )


def _reminder_digest(reminder: ReminderSpec) -> str:
    reminder_wire = {
        "due_at_utc": reminder.due_at_utc,
        "reason_code": reminder.reason_code,
        "reminder_id": reminder.reminder_id,
    }
    return hashlib.sha256(
        json.dumps(reminder_wire, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()


def _with_embedded_reminder(
    action: ProposedAction,
    reminder: ReminderSpec,
) -> ProposedAction:
    payload = json.loads(action.payload_json)
    if not isinstance(payload, dict):
        raise ValueError("action payload is invalid")
    marker = {
        "digest": _reminder_digest(reminder),
        "identifier": _REMINDER_REFERENCE_MARKER,
        "identifiers": [reminder.reminder_id],
        "reason_code": reminder.reason_code,
        "utc_timestamp": reminder.due_at_utc,
    }
    references = payload.get("references")
    if references is None:
        payload["references"] = marker
    elif isinstance(references, dict):
        if references.get("identifier") == _REMINDER_REFERENCE_MARKER:
            if references != marker:
                raise ValueError("action has a conflicting embedded reminder")
        else:
            payload["references"] = [references, marker]
    elif isinstance(references, list):
        existing = [
            item
            for item in references
            if isinstance(item, dict)
            and item.get("identifier") == _REMINDER_REFERENCE_MARKER
        ]
        if existing:
            if len(existing) != 1 or existing[0] != marker:
                raise ValueError("action has a conflicting embedded reminder")
        else:
            payload["references"] = [*references, marker]
    else:
        raise ValueError("action references are invalid")
    return ProposedAction(
        action.capability_name,
        action.action_key,
        json.dumps(payload, separators=(",", ":"), sort_keys=True),
        action.target_revision,
        action.invalidation_class,
    )


def _embedded_reminder(action: ProposedAction) -> ReminderSpec | None:
    payload = json.loads(action.payload_json)
    if not isinstance(payload, dict):
        raise ValueError("action payload is invalid")
    references = payload.get("references")
    children = references if isinstance(references, list) else [references]
    markers = [
        item
        for item in children
        if isinstance(item, dict)
        and item.get("identifier") == _REMINDER_REFERENCE_MARKER
    ]
    if not markers:
        return None
    if len(markers) != 1:
        raise ValueError("action has multiple embedded reminders")
    marker = markers[0]
    if set(marker) != {
        "digest",
        "identifier",
        "identifiers",
        "reason_code",
        "utc_timestamp",
    }:
        raise ValueError("embedded reminder is invalid")
    identifiers = marker["identifiers"]
    if not isinstance(identifiers, list) or len(identifiers) != 1:
        raise ValueError("embedded reminder identifier is invalid")
    reminder = ReminderSpec(
        identifiers[0],
        marker["utc_timestamp"],
        marker["reason_code"],
    )
    if marker["digest"] != _reminder_digest(reminder):
        raise ValueError("embedded reminder digest is invalid")
    return reminder


def _reminder_action(
    reminder: ReminderSpec,
    revision: str,
    *,
    source_action_key: str | None = None,
) -> ProposedAction:
    identity_digest = hashlib.sha256(reminder.reminder_id.encode("utf-8")).hexdigest()
    identifiers = [reminder.reminder_id]
    if source_action_key is not None:
        identifiers.append(source_action_key)
    payload_json = json.dumps(
        {
            "digest": _reminder_digest(reminder),
            "references": {
                "identifiers": identifiers,
                "reason_code": reminder.reason_code,
                "utc_timestamp": reminder.due_at_utc,
            },
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return ProposedAction(
        capability_name="reminders",
        action_key=f"reminder:{identity_digest}",
        payload_json=payload_json,
        target_revision=revision,
        invalidation_class="reminder-intent",
    )


def _reminder_from_action(
    action: ProposedAction,
) -> tuple[ReminderSpec, str | None]:
    payload = json.loads(action.payload_json)
    if not isinstance(payload, dict) or set(payload) != {"digest", "references"}:
        raise ValueError("reminder action payload is invalid")
    references = payload["references"]
    if not isinstance(references, dict) or set(references) != {
        "identifiers",
        "reason_code",
        "utc_timestamp",
    }:
        raise ValueError("reminder action references are invalid")
    identifiers = references["identifiers"]
    if not isinstance(identifiers, list) or len(identifiers) not in (1, 2):
        raise ValueError("reminder action identifiers are invalid")
    reminder = ReminderSpec(
        identifiers[0],
        references["utc_timestamp"],
        references["reason_code"],
    )
    if payload["digest"] != _reminder_digest(reminder):
        raise ValueError("reminder action digest is invalid")
    return reminder, identifiers[1] if len(identifiers) == 2 else None


def _investigation_reminder(
    action_key: str,
    reason_code: str,
    due_at: datetime,
) -> ReminderSpec:
    reminder_id = f"investigate:{action_key}"
    if len(reminder_id) > 256:
        reminder_id = "investigate:" + hashlib.sha256(action_key.encode("utf-8")).hexdigest()
    return ReminderSpec(reminder_id, _utc_timestamp(due_at), reason_code)


def _utc_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("now must be an aware UTC datetime")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_utc_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("stored wake time must be a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise ValueError("stored wake time must be a valid UTC timestamp") from error
    if parsed.utcoffset() != timedelta(0):
        raise ValueError("stored wake time must be UTC")
    return parsed


def _is_identifier(value: object) -> bool:
    return isinstance(value, str) and _IDENTIFIER.fullmatch(value) is not None
