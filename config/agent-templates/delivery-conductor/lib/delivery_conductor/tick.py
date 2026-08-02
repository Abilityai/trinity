"""One fenced, one-effect delivery-conductor control tick."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
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
from .executor import CapabilityExecutorPort, CapabilityNotInstalledError
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
    "reminder",
    "completed",
    "investigate",
]

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


@dataclass(frozen=True)
class TickResult:
    """Sanitized result of one claimed tick and at most one executor call."""

    status: TickStatus
    reason_code: str
    action_key: str | None = None
    action_status: str | None = None
    result_sha256: str | None = None
    reminder: ReminderSpec | None = None


class DeliveryConductorTick:
    """Advance one wake through read-only policy and one installed capability."""

    def __init__(
        self,
        *,
        ledger: ControlLedger,
        adapter: PolicyAdapterPort,
        executor: CapabilityExecutorPort,
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
        self._executor = executor
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
        """Run one atomic control step, invoking the executor zero or one times."""
        lease = self._ledger.claim_wake(wake, now, self._lease_seconds)
        if lease is None:
            return TickResult("not-claimed", "wake-not-claimed")

        request = AdapterRequest(
            schema_version=1,
            wake=wake,
            now_utc=_utc_timestamp(now),
            checkpoint=checkpoint,
            budget_view=budget_view,
        )
        try:
            decision = self._adapter.observe_and_decide(request)
        except Exception:
            return self._reject(lease, "adapter-unavailable", status="blocked")
        if not _valid_decision(decision):
            return self._reject(lease, "invalid-adapter-decision")

        if decision.decision != "execute":
            return self._finish_without_action(lease, decision, budget_view)

        action = decision.proposed_action
        if action is None:
            return self._reject(lease, "invalid-adapter-decision")
        if action.capability_name not in self._installed_capabilities:
            return self._reject(lease, "capability-not-installed")

        reservation = self._ledger.reserve_action(lease, action)
        if reservation.status != "reserved":
            return self._finish_terminal_replay(
                lease,
                decision,
                budget_view,
                reservation,
                now,
            )

        gate_reason = _effect_gate_reason(budget_view, breaker_allows_effect)
        if gate_reason is not None:
            self._write_checkpoint(
                lease,
                revision=decision.observed_revision,
                reason_code=gate_reason,
                budget_view=budget_view,
                reservation=reservation,
            )
            self._ledger.release(lease, TickOutcome(False, gate_reason, 0, 0, 0))
            return TickResult(
                "blocked",
                gate_reason,
                action_key=action.action_key,
                action_status="reserved",
                reminder=decision.next_reminder,
            )

        try:
            effect_result = self._executor.execute(action)
        except CapabilityNotInstalledError:
            reason_code = "capability-not-installed"
            self._write_checkpoint(
                lease,
                revision=decision.observed_revision,
                reason_code=reason_code,
                budget_view=budget_view,
                reservation=reservation,
            )
            self._ledger.release(
                lease,
                TickOutcome(False, reason_code, 0, 0, 0),
            )
            return TickResult(
                "blocked",
                reason_code,
                action_key=action.action_key,
                action_status="reserved",
                reminder=decision.next_reminder,
            )
        except Exception:
            effect_result = _synthetic_ambiguous_result(action, "executor-result-ambiguous")
        if not isinstance(effect_result, EffectResult):
            effect_result = _synthetic_ambiguous_result(action, "invalid-executor-result")

        self._ledger.record_result(lease, action.action_key, effect_result)
        remaining_budget = _spend_one(budget_view)
        terminal_reservation = ActionReservation(
            action.action_key,
            effect_result.status,
            reservation.payload_sha256,
            effect_result.result_sha256,
            effect_result.reason_code,
        )
        self._write_checkpoint(
            lease,
            revision=decision.observed_revision,
            reason_code=effect_result.reason_code,
            budget_view=remaining_budget,
            reservation=terminal_reservation,
        )
        self._ledger.release(
            lease,
            TickOutcome(True, effect_result.reason_code, 1, 1, 1),
        )
        return self._terminal_result(
            terminal_reservation,
            decision.next_reminder,
            now,
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

    def _finish_without_action(
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
        status: TickStatus = {
            "noop": "noop",
            "remind": "reminder",
            "investigate": "investigate",
        }[decision.decision]
        return TickResult(status, decision.reason_code, reminder=decision.next_reminder)

    def _finish_terminal_replay(
        self,
        lease: Lease,
        decision: AdapterDecision,
        budget_view: BudgetView,
        reservation: ActionReservation,
        now: datetime,
    ) -> TickResult:
        if reservation.reason_code is None:
            raise RuntimeError("terminal reservation has no reason code")
        self._write_checkpoint(
            lease,
            revision=decision.observed_revision,
            reason_code=reservation.reason_code,
            budget_view=budget_view,
            reservation=reservation,
        )
        self._ledger.release(
            lease,
            TickOutcome(True, reservation.reason_code, 0, 0, 0),
        )
        return self._terminal_result(reservation, decision.next_reminder, now)

    def _terminal_result(
        self,
        reservation: ActionReservation,
        reminder: ReminderSpec | None,
        now: datetime,
    ) -> TickResult:
        if reservation.status == "ambiguous":
            if reservation.reason_code is None:
                raise RuntimeError("ambiguous reservation has no reason code")
            reminder = reminder or _investigation_reminder(
                reservation.action_key,
                reservation.reason_code,
                now + timedelta(seconds=self._ambiguity_reminder_seconds),
            )
            status: TickStatus = "investigate"
        else:
            status = "completed"
        return TickResult(
            status,
            reservation.reason_code or "effect-completed",
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
        return (
            value.target_id is None
            and value.proposed_action is None
            and value.next_reminder is None
        )
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


def _synthetic_ambiguous_result(action: ProposedAction, reason_code: str) -> EffectResult:
    result_sha256 = hashlib.sha256(
        f"{action.action_key}:{action.target_revision}:{reason_code}".encode("utf-8")
    ).hexdigest()
    return EffectResult("ambiguous", result_sha256, reason_code)


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


def _is_identifier(value: object) -> bool:
    return isinstance(value, str) and _IDENTIFIER.fullmatch(value) is not None
