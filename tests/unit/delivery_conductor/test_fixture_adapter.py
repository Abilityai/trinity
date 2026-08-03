from __future__ import annotations

# ruff: noqa: E402

import ast
import json
from pathlib import Path
import subprocess
import sys

import pytest

sys.dont_write_bytecode = True
REPO_ROOT = Path(__file__).resolve().parents[3]
TEMPLATE_LIB = (
    REPO_ROOT
    / "config"
    / "agent-templates"
    / "delivery-conductor"
    / "lib"
)
sys.path.insert(0, str(TEMPLATE_LIB))

from delivery_conductor.contracts import (
    AdapterRequest,
    BudgetView,
    parse_adapter_decision_json,
    serialize_adapter_request,
)
from delivery_conductor.safety import (
    SafetyPolicyRequest,
    parse_safety_policy_json,
    serialize_safety_policy_request,
)
from delivery_conductor.wakes import normalize_wake


FIXTURE = (
    REPO_ROOT
    / "config"
    / "agent-templates"
    / "delivery-conductor"
    / "examples"
    / "fixture-adapter.py"
)
RUNBOOK = REPO_ROOT / "docs" / "memory" / "feature-flows" / "delivery-conductor-runtime.md"


def _wake(sequence: int):
    return normalize_wake("direct", f"fixture-{sequence}", f"{sequence + 1:064x}")


def _exchange(
    message: str,
    *,
    ambient: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = {"PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    if ambient is not None:
        environment.update(ambient)
    return subprocess.run(
        [sys.executable, str(FIXTURE)],
        input=message + "\n",
        capture_output=True,
        text=True,
        timeout=2,
        check=False,
        env=environment,
    )


def _decision(sequence: int):
    wake = _wake(sequence)
    request = AdapterRequest(
        1,
        wake,
        "2026-08-03T12:00:00Z",
        None,
        BudgetView(4, 4, 8),
    )
    result = _exchange(serialize_adapter_request(request))
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    return wake, parse_adapter_decision_json(result.stdout.rstrip("\n")), result.stdout


def test_fixture_alternates_noop_and_one_idempotent_chat_effect():
    wake_zero, noop, _ = _decision(0)
    wake_one, effect, first_wire = _decision(1)
    _, repeated, repeated_wire = _decision(1)

    assert noop.decision == "noop"
    assert noop.proposed_action is None
    assert noop.target_id is None
    assert effect.decision == "execute"
    assert effect.target_id == "delivery-conductor-fixture-sink"
    assert effect.proposed_action is not None
    assert effect.proposed_action.capability_name == "chat"
    assert effect.proposed_action.action_key.startswith("fixture-action-")
    assert json.loads(effect.proposed_action.payload_json) == {
        "identifier": "delivery-conductor-fixture-sink",
        "references": {
            "digest": wake_one.payload_sha256,
            "revision": "fixture-v1",
        },
    }
    assert wake_zero.wake_id != wake_one.wake_id
    assert repeated == effect
    assert repeated_wire == first_wire


def test_fixture_returns_one_closed_safety_policy_for_the_same_wake_scope():
    wake = _wake(1)
    request = SafetyPolicyRequest(1, wake, "2026-08-03T12:00:00Z")

    first = _exchange(serialize_safety_policy_request(request))
    second = _exchange(serialize_safety_policy_request(request))

    assert first.returncode == second.returncode == 0
    assert first.stdout == second.stdout
    policy = parse_safety_policy_json(first.stdout.rstrip("\n"))
    assert policy.scope.run_id == "fixture-run"
    assert policy.scope.issue_id == "fixture-scope"
    assert policy.scope.signature == "fixture-signature-direct"
    assert policy.limits.max_attempts_per_signature == 2
    assert policy.limits.max_issue_units == 4
    assert policy.limits.max_daily_units == 8


def _adapter_message(checkpoint: object, now_utc: str) -> str:
    wake = _wake(1)
    return json.dumps(
        {
            "budget_view": {
                "daily_units_remaining": 8,
                "issue_units_remaining": 4,
                "run_units_remaining": 4,
            },
            "checkpoint": checkpoint,
            "now_utc": now_utc,
            "schema_version": 1,
            "wake": {
                "payload_sha256": wake.payload_sha256,
                "source": wake.source,
                "source_event_id": wake.source_event_id,
                "wake_id": wake.wake_id,
            },
        },
        separators=(",", ":"),
        sort_keys=True,
    )


@pytest.mark.parametrize(
    ("field", "invalid"),
    (
        ("revision", None),
        ("revision", "invalid revision"),
        ("checkpoint_sha256", None),
        ("checkpoint_sha256", "not-a-digest"),
        ("fence_token", True),
        ("fence_token", -1),
        ("acknowledged_wake_id", None),
        ("acknowledged_wake_id", "invalid wake"),
    ),
)
def test_fixture_rejects_invalid_checkpoint_field_types_and_ranges(
    field: str,
    invalid: object,
):
    checkpoint: dict[str, object] = {
        "acknowledged_wake_id": "wake-1",
        "checkpoint_sha256": "a" * 64,
        "fence_token": 1,
        "revision": "revision-1",
    }
    checkpoint[field] = invalid

    result = _exchange(_adapter_message(checkpoint, "2026-08-03T12:00:00Z"))

    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr == ""


@pytest.mark.parametrize(
    "checkpoint",
    (
        {
            "checkpoint_sha256": "a" * 64,
            "fence_token": 1,
            "revision": "revision-1",
        },
        {
            "acknowledged_wake_id": "wake-1",
            "checkpoint_sha256": "a" * 64,
            "extra": None,
            "fence_token": 1,
            "revision": "revision-1",
        },
    ),
)
def test_fixture_rejects_non_closed_checkpoint_shape(checkpoint: dict[str, object]):
    result = _exchange(_adapter_message(checkpoint, "2026-08-03T12:00:00Z"))

    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_fixture_rejects_semantically_impossible_utc_timestamp():
    result = _exchange(_adapter_message(None, "2026-02-30T12:00:00Z"))

    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr == ""


@pytest.mark.parametrize(
    "message",
    (
        "not-json",
        '{"schema_version":1,"kind":"unknown"}',
        '{"schema_version":1,"kind":"safety-policy","extra":true}',
        "x" * (1024 * 1024 + 1),
    ),
)
def test_fixture_rejects_malformed_unknown_and_oversized_input_closed(message: str):
    result = _exchange(message)

    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_fixture_source_uses_only_the_reviewed_standard_library_surface():
    source = FIXTURE.read_text()
    tree = ast.parse(source)
    imported_roots: set[str] = set()
    called_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported_roots.add(node.module.split(".", 1)[0])
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            called_names.add(node.func.id)

    assert imported_roots == {
        "__future__",
        "datetime",
        "hashlib",
        "json",
        "re",
        "sys",
        "typing",
    }
    assert {"__import__", "compile", "eval", "exec", "open"}.isdisjoint(called_names)


def test_fixture_decision_is_independent_of_ambient_credentials_and_proxies():
    wake = _wake(1)
    message = serialize_adapter_request(
        AdapterRequest(
            1,
            wake,
            "2026-08-03T12:00:00Z",
            None,
            BudgetView(4, 4, 8),
        )
    )

    baseline = _exchange(message)
    with_ambient_values = _exchange(
        message,
        ambient={
            "FIXTURE_UNUSED_VALUE": "must-not-influence-fixture",
            "HTTPS_PROXY": "http://127.0.0.1:1",
        },
    )

    assert baseline.returncode == with_ambient_values.returncode == 0
    assert baseline.stdout == with_ambient_values.stdout
    assert baseline.stderr == with_ambient_values.stderr == ""


def test_runbook_records_captured_correlations_and_cleans_exact_resources():
    runbook = RUNBOOK.read_text()

    assert "ACTION_KEY_FROM_PREPARE" not in runbook
    assert '"fence_token":1' not in runbook
    assert "SELECT fence_token FROM action_journal ORDER BY fence_token" not in runbook
    assert "(key, value[key])" not in runbook
    for required in (
        "record_fixture_result()",
        "assert_blocked_no_effect()",
        'prepared["correlation"]',
        'prepared["effect_arguments"]',
        'assert_status "${CAPTURE_DIR}/hourly.json" action-ready',
        'assert_status "${CAPTURE_DIR}/worker.json" action-ready',
        'assert_status "${CAPTURE_DIR}/budget-manual.json" action-ready',
        'assert_status "${CAPTURE_DIR}/budget-reminder.json" action-ready',
        'assert_blocked_no_effect "${CAPTURE_DIR}/budget-blocked.json"',
        '"${MAIN_CONTAINER}" 4,4,4 open issue-cost-budget-exhausted',
        '"${REPLAY_CONTAINER}" 1,1,1 open attempt-budget-exhausted',
        "SELECT fence_token FROM action_events ORDER BY id",
        "assert unsettled_prior == []",
        'ambiguous investigate "${CAPTURE_DIR}/restart-result.json"',
        'delete_agent "${MAIN_NAME}"',
        'delete_agent "${REPLAY_NAME}"',
        'docker container inspect "${resource}"',
        'docker volume inspect "${resource}"',
        "alpine:3.22@sha256:14358309a308569c32bdc37e2e0e9694be33a9d99e68afb0f5ff33cc1f695dce",
        'url = "http://localhost:8000/api/agents/${name}"',
        "export ADMIN_PASSWORD='Aa1!'\"$(openssl rand -hex 24)\"",  # pragma: allowlist secret
        "MAIN_CREATED=1\ncreate_agent \"${MAIN_NAME}\"",
        "REPLAY_CREATED=1\ncreate_agent \"${REPLAY_NAME}\"",
        "docker volume inspect agent-trinity-system-workspace",
        '"${COMPOSE[@]}" down -v --remove-orphans',
    ):
        assert required in runbook
