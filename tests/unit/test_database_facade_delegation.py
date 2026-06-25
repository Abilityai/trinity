"""Lint-style guard: every `db.<method>(...)` call site in routers/ and
services/ must resolve to a real method on `DatabaseManager`.

Background: WEBHOOK-001 (#291) added `generate_webhook_token`,
`get_schedule_by_webhook_token`, `revoke_webhook_token`, and
`get_webhook_status` to `ScheduleOperations` in `src/backend/db/schedules.py`
but forgot to add the matching pass-through methods on the `DatabaseManager`
facade in `src/backend/database.py`. Because there is no `__getattr__` proxy,
every webhook endpoint blew up with `AttributeError` on a live stack — and
this went undetected because integration tests don't run in CI.

This test statically scans every `db.<method>(...)` call in routers and
services and asserts the attribute exists on `DatabaseManager`. AST-based,
no imports of backend modules required (so it runs without a venv).

Issue: https://github.com/abilityai/trinity/issues/647
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Set

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND = PROJECT_ROOT / "src" / "backend"
DATABASE_PY = BACKEND / "database.py"
SCAN_DIRS = [BACKEND / "routers", BACKEND / "services"]

# Pre-existing facade gaps discovered while writing this test for #647.
# Each entry is a real `AttributeError`-at-runtime bug, but fixing them is
# out of scope for the WEBHOOK-001 patch. Tracked separately so this lint
# test catches NEW regressions without forcing one giant cleanup PR.
#
# REMOVE entries from this set as the corresponding methods are added to
# DatabaseManager. Do NOT add new entries — fix the facade instead.
KNOWN_FACADE_GAPS: frozenset[str] = frozenset(
    {
        "create_validation_execution",
        "get_agent_folder_config",
        "get_agent_last_activity",
        "get_agent_permissions",
        "get_agent_schedules",
        "update_business_status",
    }
)


def _databasemanager_methods() -> Set[str]:
    """Return the set of method names defined on the DatabaseManager class.

    Only includes methods declared directly in `database.py`. Methods
    inherited via mixins or proxied through `__getattr__` would require
    runtime imports and are out of scope for this static check.
    """
    tree = ast.parse(DATABASE_PY.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "DatabaseManager":
            return {
                child.name
                for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
    raise AssertionError("DatabaseManager class not found in database.py")


def _db_attribute_calls(py_file: Path) -> Set[str]:
    """Extract every `db.<attr>(...)` call attribute name in a Python file.

    Matches AST nodes shaped as `Call(func=Attribute(value=Name(id='db'), attr=...))`.
    Ignores attribute access without a call (e.g., `db.X` standalone) and
    keyword arguments named `db` (e.g., `redis.Redis(db=0)`).
    """
    try:
        tree = ast.parse(py_file.read_text())
    except SyntaxError:
        return set()

    attrs: Set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "db"
        ):
            attrs.add(func.attr)
    return attrs


def test_every_db_call_resolves_on_databasemanager():
    """Every `db.<method>(...)` call in routers/ and services/ must exist
    on DatabaseManager. Catches the WEBHOOK-001 facade-delegation regression.
    """
    methods = _databasemanager_methods()
    assert methods, "Failed to extract DatabaseManager methods"

    missing: dict[str, list[str]] = {}
    for scan_dir in SCAN_DIRS:
        for py in scan_dir.rglob("*.py"):
            for attr in _db_attribute_calls(py):
                if attr not in methods and attr not in KNOWN_FACADE_GAPS:
                    missing.setdefault(attr, []).append(
                        str(py.relative_to(PROJECT_ROOT))
                    )

    assert not missing, (
        "New db.<method>(...) call sites found with no matching method on "
        "DatabaseManager (facade gap — calls will fail with AttributeError "
        "at runtime). Either add a pass-through method on DatabaseManager, "
        "or — only if the gap pre-exists this PR — add the name to "
        "KNOWN_FACADE_GAPS at the top of this file:\n"
        + "\n".join(
            f"  - db.{name}() called from: {', '.join(sorted(set(files)))}"
            for name, files in sorted(missing.items())
        )
    )


def test_webhook_001_methods_delegated():
    """Explicit regression check for #647: the four WEBHOOK-001 methods must
    be delegated on DatabaseManager.
    """
    methods = _databasemanager_methods()
    required = {
        "generate_webhook_token",
        "get_schedule_by_webhook_token",
        "revoke_webhook_token",
        "get_webhook_status",
    }
    missing = required - methods
    assert not missing, (
        f"WEBHOOK-001 methods missing from DatabaseManager: {sorted(missing)}. "
        "Add pass-through methods that delegate to self._schedule_ops."
    )


def test_1200_capabilities_methods_delegated():
    """Regression check for #1200: the full-capabilities pair must be delegated
    on DatabaseManager. They exist on SecurityMixin (composed into
    AgentOperations) but the #1093 db decomposition forgot the facade
    pass-through, so GET/PUT /api/agents/{name}/capabilities 500'd with
    AttributeError.
    """
    methods = _databasemanager_methods()
    required = {"get_full_capabilities", "set_full_capabilities"}
    missing = required - methods
    assert not missing, (
        f"capabilities methods missing from DatabaseManager: {sorted(missing)}. "
        "Add pass-through methods that delegate to self._agent_ops."
    )
