"""Executable contract tests for the delivery-conductor adapter boundary."""
# ruff: noqa: E402
from __future__ import annotations

import json
from pathlib import Path
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

from delivery_conductor.contracts import (
    AdapterDecision,
    AdapterRequest,
    ContractValidationError,
    MAX_MESSAGE_BYTES,
    ProposedAction,
    ReminderSpec,
    Wake,
    parse_adapter_decision_json,
    parse_adapter_request_json,
    serialize_adapter_decision,
    serialize_adapter_request,
)


def _request_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "wake": {
            "wake_id": "wake-017",
            "source": "reminder",
            "source_event_id": "rem-017",
            "payload_sha256": "a" * 64,
        },
        "now_utc": "2026-08-02T09:10:11Z",
        "checkpoint": {
            "revision": "checkpoint-9",
            "checkpoint_sha256": "b" * 64,
            "fence_token": 4,
            "acknowledged_wake_id": "wake-016",
        },
        "budget_view": {
            "run_units_remaining": 3,
            "issue_units_remaining": 8,
            "daily_units_remaining": 20,
        },
    }


def _decision_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "observed_revision": "repo-4",
        "decision": "dispatch",
        "reason_code": "eligible",
        "target_id": "target-7",
        "proposed_action": {
            "capability_name": "chat",
            "action_key": "action-0007",
            "payload": {"references": {"digest": "c" * 64, "identifier": "notify-7"}},
            "target_revision": "repo-4",
            "invalidation_class": "observation-change",
        },
        "next_reminder": {
            "reminder_id": "follow-up-7",
            "due_at_utc": "2026-08-02T10:10:11Z",
            "reason_code": "await-result",
        },
    }


def test_request_round_trip_is_closed_immutable_and_canonical():
    """Changing a valid adapter input must remain detectable after JSONL transport."""
    request = parse_adapter_request_json(json.dumps(_request_payload()))

    assert isinstance(request, AdapterRequest)
    assert request.schema_version == 1
    assert request.wake.source == "reminder"
    assert serialize_adapter_request(request) == (
        '{"budget_view":{"daily_units_remaining":20,"issue_units_remaining":8,'
        '"run_units_remaining":3},"checkpoint":{"acknowledged_wake_id":"wake-016",'
        '"checkpoint_sha256":"' + "b" * 64 + '","fence_token":4,'
        '"revision":"checkpoint-9"},"now_utc":"2026-08-02T09:10:11Z",'
        '"schema_version":1,"wake":{"payload_sha256":"' + "a" * 64 + '",'
        '"source":"reminder","source_event_id":"rem-017","wake_id":"wake-017"}}'
    )
    with pytest.raises(AttributeError):
        request.now_utc = "2026-08-03T00:00:00Z"  # type: ignore[misc]


def test_decision_canonicalizes_payload_and_round_trips_once():
    """A capability payload has one stable representation and one action/reminder slot."""
    decision = parse_adapter_decision_json(json.dumps(_decision_payload()))

    assert isinstance(decision, AdapterDecision)
    assert decision.proposed_action is not None
    assert decision.proposed_action.payload_json == (
        '{"references":{"digest":"' + "c" * 64 + '","identifier":"notify-7"}}'
    )
    assert serialize_adapter_decision(decision) == (
        '{"decision":"dispatch","next_reminder":{"due_at_utc":"2026-08-02T10:10:11Z",'
        '"reason_code":"await-result","reminder_id":"follow-up-7"},'
        '"observed_revision":"repo-4","proposed_action":{"action_key":"action-0007",'
        '"capability_name":"chat","invalidation_class":"observation-change",'
        '"payload":{"references":{"digest":"' + "c" * 64
        + '","identifier":"notify-7"}},'
        '"target_revision":"repo-4"},"reason_code":"eligible","schema_version":1,'
        '"target_id":"target-7"}'
    )


def test_generic_reference_utc_timestamp_round_trips_direct_and_parsed():
    """A reminder due time must remain typed data throughout the closed payload."""
    timestamp = "2026-08-02T10:10:11Z"
    action = ProposedAction(
        capability_name="reminders",
        action_key="reminder-typed-time",
        payload_json=json.dumps(
            {
                "references": {
                    "identifier": "follow-up-7",
                    "utc_timestamp": timestamp,
                }
            },
            separators=(",", ":"),
            sort_keys=True,
        ),
        target_revision="repo-4",
        invalidation_class="reminder-intent",
    )
    decision = AdapterDecision(
        1,
        "repo-4",
        "dispatch",
        "eligible",
        "target-7",
        action,
        None,
    )

    serialized = serialize_adapter_decision(decision)
    reparsed = parse_adapter_decision_json(serialized)

    assert reparsed == decision
    assert json.loads(reparsed.proposed_action.payload_json)["references"] == {
        "identifier": "follow-up-7",
        "utc_timestamp": timestamp,
    }


@pytest.mark.parametrize(
    "value",
    (
        "2026-08-02T10:10:11+00:00",
        "2026-08-02T10:10:11",
        "2026-02-30T10:10:11Z",
        7,
    ),
)
def test_generic_reference_utc_timestamp_rejects_non_exact_values(value: object):
    """The generic timestamp field cannot accept offsets, local time, or invalid dates."""
    with pytest.raises(ContractValidationError, match="UTC|timestamp|string"):
        ProposedAction(
            capability_name="reminders",
            action_key="reminder-invalid-time",
            payload_json=json.dumps(
                {"references": {"utc_timestamp": value}},
                separators=(",", ":"),
                sort_keys=True,
            ),
            target_revision="repo-4",
            invalidation_class="reminder-intent",
        )


@pytest.mark.parametrize(
    ("parser", "payload"),
    [
        (parse_adapter_request_json, '{"schema_version":1,"schema_version":1}'),
        (
            parse_adapter_decision_json,
            '{"schema_version":1,"observed_revision":"r","decision":"noop",'
            '"reason_code":"none","target_id":null,"proposed_action":null,'
            '"next_reminder":null,"extra":"blocked"}',
        ),
    ],
)
def test_closed_schema_rejects_duplicate_and_unknown_keys(parser, payload: str):
    """A parser change that accepts uncontracted data must fail at the boundary."""
    with pytest.raises(ContractValidationError):
        parser(payload)


@pytest.mark.parametrize(
    "field,value",
    [
        ("now_utc", "2026-08-02T09:10:11+00:00"),
        ("now_utc", "2026-08-02T09:10:11"),
        ("now_utc", "not-a-time"),
    ],
)
def test_request_rejects_non_utc_timestamp(field: str, value: str):
    """A non-UTC clock value cannot influence a deterministic control tick."""
    payload = _request_payload()
    payload[field] = value

    with pytest.raises(ContractValidationError, match="UTC"):
        parse_adapter_request_json(json.dumps(payload))


def test_rejects_oversized_message_and_string():
    """Large transport data and raw-content-sized references stay outside the protocol."""
    payload = _request_payload()
    payload["wake"] = {**payload["wake"], "wake_id": "x" * 4097}  # type: ignore[arg-type]
    with pytest.raises(ContractValidationError, match="string"):
        parse_adapter_request_json(json.dumps(payload))

    with pytest.raises(ContractValidationError, match="1 MiB"):
        parse_adapter_request_json(" " * (MAX_MESSAGE_BYTES + 1))


def test_serializer_rejects_an_oversized_direct_envelope():
    """A future direct caller cannot bypass the JSON Lines transport cap."""
    action = ProposedAction(
        capability_name="chat",
        action_key="action-oversized",
        payload_json=json.dumps(
            {"digests": ["a" * 64] * 16000}, separators=(",", ":"), sort_keys=True
        ),
        target_revision="repo-4",
        invalidation_class="observation-change",
    )
    decision = AdapterDecision(1, "repo-4", "dispatch", "eligible", "target-7", action, None)

    with pytest.raises(ContractValidationError, match="1 MiB"):
        serialize_adapter_decision(decision)


@pytest.mark.parametrize("hash_key", ("payload_sha256", "checkpoint_sha256"))
def test_request_rejects_malformed_hashes(hash_key: str):
    """Digest-shaped references must remain SHA-256 values, not arbitrary content."""
    payload = _request_payload()
    container = "wake" if hash_key == "payload_sha256" else "checkpoint"
    payload[container] = {**payload[container], hash_key: "not-a-sha256"}  # type: ignore[index]

    with pytest.raises(ContractValidationError, match="SHA-256"):
        parse_adapter_request_json(json.dumps(payload))


def test_decision_rejects_deep_payload_and_unknown_authority_fields():
    """A payload cannot smuggle executable authority or evade review by nesting."""
    payload = _decision_payload()
    nested: object = "leaf"
    for _ in range(21):
        nested = {"next": nested}
    payload["proposed_action"] = {**payload["proposed_action"], "payload": nested}  # type: ignore[arg-type]
    with pytest.raises(ContractValidationError, match="depth"):
        parse_adapter_decision_json(json.dumps(payload))

    payload = _decision_payload()
    payload["proposed_action"] = {  # type: ignore[arg-type]
        **payload["proposed_action"],
        "payload": {"references": {"command": "outside-contract"}},
    }
    with pytest.raises(ContractValidationError, match="unknown"):
        parse_adapter_decision_json(json.dumps(payload))


@pytest.mark.parametrize(
    "raw_key",
    (
        "command",
        "Command",
        "%63ommand",
        "ｃｏｍｍａｎｄ",
        "fileContent",
        "issue_body",
    ),
)
def test_payload_accepts_only_generic_reference_fields_recursively(raw_key: str):
    """Aliases, encoding, and confusables cannot bypass the closed reference schema."""
    payload = _decision_payload()
    payload["proposed_action"] = {  # type: ignore[arg-type]
        **payload["proposed_action"],
        "payload": {"references": {raw_key: "outside-contract"}},
    }

    with pytest.raises(ContractValidationError, match="unknown"):
        parse_adapter_decision_json(json.dumps(payload))


def test_payload_rejects_json_escaped_authority_key_before_payload_construction():
    """JSON escape decoding still reaches the same closed key validator."""
    message = json.dumps(_decision_payload()).replace("references", "\\u0072eferences")
    message = message.replace("identifier", "\\u0063ommand")

    with pytest.raises(ContractValidationError, match="unknown"):
        parse_adapter_decision_json(message)


@pytest.mark.parametrize(
    "raw_key",
    ("Command", "%63ommand", "ｃｏｍｍａｎｄ", "fileContent", "issue_body"),
)
def test_direct_proposed_action_rejects_the_same_closed_payload_aliases(raw_key: str):
    """Direct construction cannot bypass the parser's recursive reference schema."""
    with pytest.raises(ContractValidationError, match="unknown"):
        ProposedAction(
            capability_name="chat",
            action_key="action-direct",
            payload_json=json.dumps({"references": {raw_key: "outside-contract"}}, separators=(",", ":")),
            target_revision="repo-4",
            invalidation_class="observation-change",
        )


@pytest.mark.parametrize(
    "factory",
    (
        lambda: Wake(7, "reminder", "rem-017", "a" * 64),
        lambda: Wake("wake-017", "reminder", "rem-017", 7),
        lambda: ReminderSpec("follow-up-7", 7, "await-result"),
    ),
)
def test_direct_string_fields_raise_contract_validation_error(factory):
    """Wrong direct-construction types must not leak implementation TypeErrors."""
    with pytest.raises(ContractValidationError):
        factory()


@pytest.mark.parametrize("field", ("proposed_action", "next_reminder"))
def test_decision_rejects_multiple_actions_or_reminders(field: str):
    """An array cannot turn the one-effect and one-reminder slots into plural work."""
    payload = _decision_payload()
    payload[field] = [payload[field], payload[field]]

    with pytest.raises(ContractValidationError):
        parse_adapter_decision_json(json.dumps(payload))
