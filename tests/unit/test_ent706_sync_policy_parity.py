"""The scheduler's copy of the sync policy is the backend's, byte for byte
(trinity-enterprise#706).

`src/scheduler` is a standalone image that cannot import `src/backend`, so the
policy is vendored rather than imported — the `failure_classifier.py` precedent
(#1088, Invariant #5). A copy that drifts is two policies: the dashboard would
say one thing and the scheduler would freeze on another.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
_BACKEND_POLICY = _REPO / "src" / "backend" / "services" / "sync_freeze_policy.py"
_SCHEDULER_POLICY = _REPO / "src" / "scheduler" / "sync_freeze_policy.py"
_SCHEDULER_DB = _REPO / "src" / "scheduler" / "database.py"


def test_policy_files_are_byte_identical():
    assert _BACKEND_POLICY.read_bytes() == _SCHEDULER_POLICY.read_bytes(), (
        "src/scheduler/sync_freeze_policy.py has drifted from the canonical "
        "src/backend/services/sync_freeze_policy.py. Re-sync the mirror:\n"
        "    cp src/backend/services/sync_freeze_policy.py "
        "src/scheduler/sync_freeze_policy.py"
    )


def test_policy_is_a_stdlib_leaf():
    """The mirror must import in the scheduler image, which has neither the
    backend's packages nor its modules on the path."""
    tree = ast.parse(_BACKEND_POLICY.read_text())
    allowed = {"__future__", "datetime", "typing"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "no relative imports in a vendored leaf"
            names = [(node.module or "").split(".")[0]]
        else:
            continue
        for name in names:
            assert name in allowed, f"sync_freeze_policy imports {name!r}"


def test_scheduler_threshold_is_imported_not_redefined():
    """One number: the scheduler's freeze threshold comes from the policy."""
    tree = ast.parse(_SCHEDULER_DB.read_text())
    assigned = {
        t.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for t in node.targets
        if isinstance(t, ast.Name)
    }
    assert "SYNC_FAILURE_FREEZE_THRESHOLD" not in assigned
    imported = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "sync_freeze_policy"
        for alias in node.names
    }
    assert "SYNC_FAILURE_FREEZE_THRESHOLD" in imported
