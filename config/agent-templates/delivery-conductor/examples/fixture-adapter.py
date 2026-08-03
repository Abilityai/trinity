#!/usr/bin/env python3
"""Deterministic read-only fixture policy for conductor recovery verification."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Any


_MAX_MESSAGE_BYTES = 1024 * 1024
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
_TARGET = "delivery-conductor-fixture-sink"


class FixtureInputError(ValueError):
    """Raised when a fixture request is outside the closed protocol."""


def _exact(value: object, keys: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise FixtureInputError(f"{name} must use the closed schema")
    return value


def _identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise FixtureInputError(f"{name} is invalid")
    return value


def _wake(value: object) -> dict[str, Any]:
    wake = _exact(
        value,
        {"wake_id", "source", "source_event_id", "payload_sha256"},
        "wake",
    )
    _identifier(wake["wake_id"], "wake_id")
    _identifier(wake["source"], "source")
    _identifier(wake["source_event_id"], "source_event_id")
    if not isinstance(wake["payload_sha256"], str) or _SHA256.fullmatch(
        wake["payload_sha256"]
    ) is None:
        raise FixtureInputError("payload digest is invalid")
    return wake


def _utc_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or _UTC.fullmatch(value) is None:
        raise FixtureInputError("timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise FixtureInputError("timestamp is invalid") from error
    if parsed.tzinfo != timezone.utc:
        raise FixtureInputError("timestamp is invalid")
    return parsed


def _request() -> dict[str, Any]:
    raw = sys.stdin.buffer.readline(_MAX_MESSAGE_BYTES + 2)
    if not raw.endswith(b"\n") or len(raw) > _MAX_MESSAGE_BYTES + 1:
        raise FixtureInputError("request is not one bounded JSON line")
    try:
        value = json.loads(
            raw[:-1].decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FixtureInputError("request is invalid JSON") from error
    if not isinstance(value, dict):
        raise FixtureInputError("request must be an object")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise FixtureInputError("request contains duplicate keys")
        value[key] = item
    return value


def _validate_common(request: dict[str, Any], expected: set[str]) -> dict[str, Any]:
    _exact(request, expected, "request")
    if type(request["schema_version"]) is not int or request["schema_version"] != 1:
        raise FixtureInputError("schema version is invalid")
    wake = _wake(request["wake"])
    _utc_timestamp(request["now_utc"])
    return wake


def _safety_policy(request: dict[str, Any]) -> dict[str, Any]:
    wake = _validate_common(
        request,
        {"schema_version", "kind", "wake", "now_utc"},
    )
    if request["kind"] != "safety-policy":
        raise FixtureInputError("request kind is invalid")
    return {
        "schema_version": 1,
        "kind": "safety-policy",
        "run_id": "fixture-run",
        "issue_id": "fixture-scope",
        "signature": f"fixture-signature-{wake['source']}",
        "ceilings": {
            "max_attempts_per_signature": 2,
            "max_repair_cycles": 1,
            "max_run_seconds": 3600,
            "max_issue_units": 4,
            "max_daily_units": 8,
            "max_stale_leases": 2,
            "max_orphaned_workers": 2,
            "max_safety_events": 12,
            "max_no_work_ticks": 8,
        },
    }


def _decision(request: dict[str, Any]) -> dict[str, Any]:
    wake = _validate_common(
        request,
        {"schema_version", "wake", "now_utc", "checkpoint", "budget_view"},
    )
    checkpoint = request["checkpoint"]
    if checkpoint is not None:
        _exact(
            checkpoint,
            {"revision", "checkpoint_sha256", "fence_token", "acknowledged_wake_id"},
            "checkpoint",
        )
        _identifier(checkpoint["revision"], "checkpoint revision")
        if not isinstance(checkpoint["checkpoint_sha256"], str) or _SHA256.fullmatch(
            checkpoint["checkpoint_sha256"]
        ) is None:
            raise FixtureInputError("checkpoint digest is invalid")
        if type(checkpoint["fence_token"]) is not int or checkpoint["fence_token"] < 0:
            raise FixtureInputError("checkpoint fence is invalid")
        _identifier(
            checkpoint["acknowledged_wake_id"],
            "checkpoint acknowledged wake",
        )
    budget = _exact(
        request["budget_view"],
        {"run_units_remaining", "issue_units_remaining", "daily_units_remaining"},
        "budget",
    )
    if any(type(value) is not int or value < 0 for value in budget.values()):
        raise FixtureInputError("budget is invalid")

    event_id = wake["source_event_id"]
    runtime_execution = re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
        event_id,
    )
    if runtime_execution is not None:
        observed_at = _utc_timestamp(request["now_utc"])
        due_day = (observed_at + timedelta(days=2)).date()
        reminder_id = "fixture-actual-" + hashlib.sha256(
            wake["wake_id"].encode("utf-8")
        ).hexdigest()[:32]
        return {
            "schema_version": 1,
            "observed_revision": "fixture-v1",
            "decision": "remind",
            "reason_code": "fixture-actual-reminder",
            "target_id": None,
            "proposed_action": None,
            "next_reminder": {
                "reminder_id": reminder_id,
                "due_at_utc": due_day.isoformat() + "T00:00:00Z",
                "reason_code": "fixture-actual-effect",
            },
        }
    suffix = re.search(r"(\d+)$", event_id)
    sequence = (
        int(suffix.group(1))
        if suffix is not None
        else int(hashlib.sha256(event_id.encode("utf-8")).hexdigest()[-1], 16)
    )
    execute = sequence % 2 == 1
    action_key = "fixture-action-" + hashlib.sha256(
        ("fixture-action-v1\0" + wake["wake_id"]).encode("utf-8")
    ).hexdigest()[:32]
    return {
        "schema_version": 1,
        "observed_revision": "fixture-v1",
        "decision": "execute" if execute else "noop",
        "reason_code": "fixture-effect" if execute else "fixture-no-work",
        "target_id": _TARGET if execute else None,
        "proposed_action": (
            {
                "capability_name": "chat",
                "action_key": action_key,
                "payload": {
                    "identifier": _TARGET,
                    "references": {
                        "digest": wake["payload_sha256"],
                        "revision": "fixture-v1",
                    },
                },
                "target_revision": "fixture-v1",
                "invalidation_class": "fixture-effect",
            }
            if execute
            else None
        ),
        "next_reminder": None,
    }


def main() -> int:
    try:
        request = _request()
        response = _safety_policy(request) if "kind" in request else _decision(request)
        wire = json.dumps(
            response,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
        if len(wire.encode("utf-8")) > _MAX_MESSAGE_BYTES:
            raise FixtureInputError("response is too large")
        sys.stdout.write(wire + "\n")
        sys.stdout.flush()
        return 0
    except Exception:
        return 64


if __name__ == "__main__":
    raise SystemExit(main())
