"""Closed, versioned JSON Lines contracts for policy adapters.

The conductor deliberately treats capability names and payloads as validated
data.  This module never resolves a capability or interprets a payload.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
from typing import Any, Literal


SCHEMA_VERSION = 1
MAX_MESSAGE_BYTES = 1024 * 1024
MAX_STRING_LENGTH = 4096
MAX_NESTING_DEPTH = 20

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_UTC_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
_FORBIDDEN_PAYLOAD_KEYS = frozenset(
    {
        "command",
        "cmd",
        "url",
        "uri",
        "environment",
        "env",
        "credential",
        "credentials",
        "token",
        "password",
        "secret",
        "file",
        "file_content",
        "content",
        "contents",
    }
)


class ContractValidationError(ValueError):
    """Raised when a JSON Lines message is outside the adapter contract."""


@dataclass(frozen=True)
class Wake:
    wake_id: str
    source: str
    source_event_id: str
    payload_sha256: str

    def __post_init__(self) -> None:
        _validate_identifier("wake_id", self.wake_id)
        _validate_identifier("source", self.source)
        _validate_identifier("source_event_id", self.source_event_id)
        _validate_sha256("payload_sha256", self.payload_sha256)


@dataclass(frozen=True)
class CheckpointView:
    revision: str
    checkpoint_sha256: str
    fence_token: int
    acknowledged_wake_id: str

    def __post_init__(self) -> None:
        _validate_identifier("revision", self.revision)
        _validate_sha256("checkpoint_sha256", self.checkpoint_sha256)
        _validate_non_negative_int("fence_token", self.fence_token)
        _validate_identifier("acknowledged_wake_id", self.acknowledged_wake_id)


@dataclass(frozen=True)
class BudgetView:
    run_units_remaining: int
    issue_units_remaining: int
    daily_units_remaining: int

    def __post_init__(self) -> None:
        _validate_non_negative_int("run_units_remaining", self.run_units_remaining)
        _validate_non_negative_int("issue_units_remaining", self.issue_units_remaining)
        _validate_non_negative_int("daily_units_remaining", self.daily_units_remaining)


@dataclass(frozen=True)
class ReminderSpec:
    reminder_id: str
    due_at_utc: str
    reason_code: str

    def __post_init__(self) -> None:
        _validate_identifier("reminder_id", self.reminder_id)
        _validate_utc_timestamp("due_at_utc", self.due_at_utc)
        _validate_identifier("reason_code", self.reason_code)


@dataclass(frozen=True)
class ProposedAction:
    capability_name: str
    action_key: str
    payload_json: str
    target_revision: str
    invalidation_class: str

    def __post_init__(self) -> None:
        _validate_identifier("capability_name", self.capability_name)
        _validate_identifier("action_key", self.action_key)
        _validate_identifier("target_revision", self.target_revision)
        _validate_identifier("invalidation_class", self.invalidation_class)
        payload = _parse_json(self.payload_json, name="payload")
        _validate_payload(payload)
        canonical = _canonical_json(payload)
        if self.payload_json != canonical:
            raise ContractValidationError("payload_json must be canonical JSON")


@dataclass(frozen=True)
class AdapterRequest:
    schema_version: Literal[1]
    wake: Wake
    now_utc: str
    checkpoint: CheckpointView | None
    budget_view: BudgetView

    def __post_init__(self) -> None:
        _validate_schema_version(self.schema_version)
        if not isinstance(self.wake, Wake):
            raise ContractValidationError("wake must be a Wake")
        _validate_utc_timestamp("now_utc", self.now_utc)
        if self.checkpoint is not None and not isinstance(self.checkpoint, CheckpointView):
            raise ContractValidationError("checkpoint must be a CheckpointView or null")
        if not isinstance(self.budget_view, BudgetView):
            raise ContractValidationError("budget_view must be a BudgetView")


@dataclass(frozen=True)
class AdapterDecision:
    schema_version: Literal[1]
    observed_revision: str
    decision: str
    reason_code: str
    target_id: str | None
    proposed_action: ProposedAction | None
    next_reminder: ReminderSpec | None

    def __post_init__(self) -> None:
        _validate_schema_version(self.schema_version)
        _validate_identifier("observed_revision", self.observed_revision)
        _validate_identifier("decision", self.decision)
        _validate_identifier("reason_code", self.reason_code)
        if self.target_id is not None:
            _validate_identifier("target_id", self.target_id)
        if self.proposed_action is not None and not isinstance(
            self.proposed_action, ProposedAction
        ):
            raise ContractValidationError("proposed_action must be a ProposedAction or null")
        if self.next_reminder is not None and not isinstance(self.next_reminder, ReminderSpec):
            raise ContractValidationError("next_reminder must be a ReminderSpec or null")


def parse_adapter_request_json(message: str) -> AdapterRequest:
    """Parse one strict version-1 adapter request JSON line."""
    value = _parse_message(message)
    _require_object_keys(
        value,
        {"schema_version", "wake", "now_utc", "checkpoint", "budget_view"},
        "adapter request",
    )
    return AdapterRequest(
        schema_version=_required_schema_version(value),
        wake=_parse_wake(_required_object(value, "wake")),
        now_utc=_required_string(value, "now_utc"),
        checkpoint=_parse_checkpoint(value["checkpoint"]),
        budget_view=_parse_budget_view(_required_object(value, "budget_view")),
    )


def parse_adapter_decision_json(message: str) -> AdapterDecision:
    """Parse one strict version-1 adapter decision JSON line."""
    value = _parse_message(message)
    _require_object_keys(
        value,
        {
            "schema_version",
            "observed_revision",
            "decision",
            "reason_code",
            "target_id",
            "proposed_action",
            "next_reminder",
        },
        "adapter decision",
    )
    return AdapterDecision(
        schema_version=_required_schema_version(value),
        observed_revision=_required_string(value, "observed_revision"),
        decision=_required_string(value, "decision"),
        reason_code=_required_string(value, "reason_code"),
        target_id=_optional_string(value, "target_id"),
        proposed_action=_parse_action(value["proposed_action"]),
        next_reminder=_parse_reminder(value["next_reminder"]),
    )


def serialize_adapter_request(request: AdapterRequest) -> str:
    """Serialize a request as one deterministic JSON line without a newline."""
    if not isinstance(request, AdapterRequest):
        raise ContractValidationError("request must be an AdapterRequest")
    return _serialize_message(_request_to_wire(request))


def serialize_adapter_decision(decision: AdapterDecision) -> str:
    """Serialize a decision as one deterministic JSON line without a newline."""
    if not isinstance(decision, AdapterDecision):
        raise ContractValidationError("decision must be an AdapterDecision")
    return _serialize_message(_decision_to_wire(decision))


def _parse_message(message: str) -> dict[str, Any]:
    if not isinstance(message, str):
        raise ContractValidationError("JSON Lines message must be a string")
    if len(message.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise ContractValidationError("JSON Lines message exceeds 1 MiB")
    value = _parse_json(message, name="JSON Lines message")
    _validate_json_shape(value)
    if not isinstance(value, dict):
        raise ContractValidationError("JSON Lines message must be an object")
    return value


def _parse_json(value: str, *, name: str) -> Any:
    if not isinstance(value, str):
        raise ContractValidationError(f"{name} must be a string")
    try:
        return json.loads(value, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as error:
        raise ContractValidationError(f"invalid {name} JSON") from error


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys: set[str] = set()
    for key, _ in pairs:
        if key in keys:
            raise ContractValidationError(f"duplicate JSON key: {key}")
        keys.add(key)
    return dict(pairs)


def _validate_json_shape(value: Any, depth: int = 0) -> None:
    if depth > MAX_NESTING_DEPTH:
        raise ContractValidationError(f"JSON nesting depth exceeds {MAX_NESTING_DEPTH}")
    if isinstance(value, str):
        _validate_string("JSON string", value)
        return
    if value is None or isinstance(value, (int, float)) and not isinstance(value, bool):
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_shape(item, depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _validate_string("JSON key", key)
            _validate_json_shape(item, depth + 1)
        return
    raise ContractValidationError("JSON contains an unsupported value")


def _parse_wake(value: dict[str, Any]) -> Wake:
    _require_object_keys(value, {"wake_id", "source", "source_event_id", "payload_sha256"}, "wake")
    return Wake(
        wake_id=_required_string(value, "wake_id"),
        source=_required_string(value, "source"),
        source_event_id=_required_string(value, "source_event_id"),
        payload_sha256=_required_string(value, "payload_sha256"),
    )


def _parse_checkpoint(value: Any) -> CheckpointView | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ContractValidationError("checkpoint must be an object or null")
    _require_object_keys(
        value,
        {"revision", "checkpoint_sha256", "fence_token", "acknowledged_wake_id"},
        "checkpoint",
    )
    return CheckpointView(
        revision=_required_string(value, "revision"),
        checkpoint_sha256=_required_string(value, "checkpoint_sha256"),
        fence_token=_required_non_negative_int(value, "fence_token"),
        acknowledged_wake_id=_required_string(value, "acknowledged_wake_id"),
    )


def _parse_budget_view(value: dict[str, Any]) -> BudgetView:
    _require_object_keys(
        value,
        {"run_units_remaining", "issue_units_remaining", "daily_units_remaining"},
        "budget_view",
    )
    return BudgetView(
        run_units_remaining=_required_non_negative_int(value, "run_units_remaining"),
        issue_units_remaining=_required_non_negative_int(value, "issue_units_remaining"),
        daily_units_remaining=_required_non_negative_int(value, "daily_units_remaining"),
    )


def _parse_action(value: Any) -> ProposedAction | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ContractValidationError("proposed_action must be one object or null")
    _require_object_keys(
        value,
        {"capability_name", "action_key", "payload", "target_revision", "invalidation_class"},
        "proposed_action",
    )
    payload = value["payload"]
    _validate_payload(payload)
    return ProposedAction(
        capability_name=_required_string(value, "capability_name"),
        action_key=_required_string(value, "action_key"),
        payload_json=_canonical_json(payload),
        target_revision=_required_string(value, "target_revision"),
        invalidation_class=_required_string(value, "invalidation_class"),
    )


def _parse_reminder(value: Any) -> ReminderSpec | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ContractValidationError("next_reminder must be one object or null")
    _require_object_keys(value, {"reminder_id", "due_at_utc", "reason_code"}, "next_reminder")
    return ReminderSpec(
        reminder_id=_required_string(value, "reminder_id"),
        due_at_utc=_required_string(value, "due_at_utc"),
        reason_code=_required_string(value, "reason_code"),
    )


def _validate_payload(value: Any) -> None:
    _validate_json_shape(value)
    if not isinstance(value, (dict, list)):
        raise ContractValidationError("payload must be a JSON object or array")
    _reject_forbidden_payload_keys(value)


def _reject_forbidden_payload_keys(value: Any) -> None:
    if isinstance(value, list):
        for item in value:
            _reject_forbidden_payload_keys(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in _FORBIDDEN_PAYLOAD_KEYS:
                raise ContractValidationError(f"forbidden payload field: {key}")
            _reject_forbidden_payload_keys(item)


def _require_object_keys(value: dict[str, Any], expected: set[str], name: str) -> None:
    actual = set(value)
    unknown = actual - expected
    missing = expected - actual
    if unknown:
        raise ContractValidationError(f"{name} has unknown keys: {', '.join(sorted(unknown))}")
    if missing:
        raise ContractValidationError(f"{name} is missing keys: {', '.join(sorted(missing))}")


def _required_object(value: dict[str, Any], field: str) -> dict[str, Any]:
    item = value.get(field)
    if not isinstance(item, dict):
        raise ContractValidationError(f"{field} must be an object")
    return item


def _required_string(value: dict[str, Any], field: str) -> str:
    item = value.get(field)
    if not isinstance(item, str):
        raise ContractValidationError(f"{field} must be a string")
    _validate_string(field, item)
    return item


def _optional_string(value: dict[str, Any], field: str) -> str | None:
    item = value.get(field)
    if item is None:
        return None
    if not isinstance(item, str):
        raise ContractValidationError(f"{field} must be a string or null")
    _validate_string(field, item)
    return item


def _required_non_negative_int(value: dict[str, Any], field: str) -> int:
    item = value.get(field)
    _validate_non_negative_int(field, item)
    return item


def _required_schema_version(value: dict[str, Any]) -> Literal[1]:
    version = value.get("schema_version")
    _validate_schema_version(version)
    return 1


def _validate_schema_version(value: Any) -> None:
    if type(value) is not int or value != SCHEMA_VERSION:
        raise ContractValidationError("schema_version must be 1")


def _validate_non_negative_int(name: str, value: Any) -> None:
    if type(value) is not int or value < 0:
        raise ContractValidationError(f"{name} must be a non-negative integer")


def _validate_string(name: str, value: str) -> None:
    if len(value) > MAX_STRING_LENGTH:
        raise ContractValidationError(f"{name} string exceeds {MAX_STRING_LENGTH} characters")


def _validate_identifier(name: str, value: str) -> None:
    _validate_string(name, value)
    if not _IDENTIFIER.fullmatch(value):
        raise ContractValidationError(f"{name} must be a sanitized identifier")


def _validate_sha256(name: str, value: str) -> None:
    _validate_string(name, value)
    if not _SHA256.fullmatch(value):
        raise ContractValidationError(f"{name} must be a lowercase SHA-256 digest")


def _validate_utc_timestamp(name: str, value: str) -> None:
    _validate_string(name, value)
    if not _UTC_TIMESTAMP.fullmatch(value):
        raise ContractValidationError(f"{name} must be a UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise ContractValidationError(f"{name} must be a valid UTC timestamp") from error
    if parsed.tzinfo != timezone.utc:
        raise ContractValidationError(f"{name} must be a UTC timestamp")


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ContractValidationError("value is not canonical JSON") from error


def _serialize_message(value: Any) -> str:
    message = _canonical_json(value)
    if len(message.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise ContractValidationError("JSON Lines message exceeds 1 MiB")
    return message


def _request_to_wire(request: AdapterRequest) -> dict[str, Any]:
    checkpoint = request.checkpoint
    return {
        "schema_version": request.schema_version,
        "wake": {
            "wake_id": request.wake.wake_id,
            "source": request.wake.source,
            "source_event_id": request.wake.source_event_id,
            "payload_sha256": request.wake.payload_sha256,
        },
        "now_utc": request.now_utc,
        "checkpoint": None
        if checkpoint is None
        else {
            "revision": checkpoint.revision,
            "checkpoint_sha256": checkpoint.checkpoint_sha256,
            "fence_token": checkpoint.fence_token,
            "acknowledged_wake_id": checkpoint.acknowledged_wake_id,
        },
        "budget_view": {
            "run_units_remaining": request.budget_view.run_units_remaining,
            "issue_units_remaining": request.budget_view.issue_units_remaining,
            "daily_units_remaining": request.budget_view.daily_units_remaining,
        },
    }


def _decision_to_wire(decision: AdapterDecision) -> dict[str, Any]:
    action = decision.proposed_action
    reminder = decision.next_reminder
    return {
        "schema_version": decision.schema_version,
        "observed_revision": decision.observed_revision,
        "decision": decision.decision,
        "reason_code": decision.reason_code,
        "target_id": decision.target_id,
        "proposed_action": None
        if action is None
        else {
            "capability_name": action.capability_name,
            "action_key": action.action_key,
            "payload": _parse_json(action.payload_json, name="payload"),
            "target_revision": action.target_revision,
            "invalidation_class": action.invalidation_class,
        },
        "next_reminder": None
        if reminder is None
        else {
            "reminder_id": reminder.reminder_id,
            "due_at_utc": reminder.due_at_utc,
            "reason_code": reminder.reason_code,
        },
    }
