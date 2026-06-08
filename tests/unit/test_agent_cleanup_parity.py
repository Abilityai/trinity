"""
Parity enforcement for the #816 agent-reference registry (Issue #834).

`db/agent_cleanup.py`'s docstring promises:

    Adding a new table that references an agent without an `AgentRef`
    entry fails the parity check in
    `tests/unit/test_agent_cleanup_parity.py`.

This is that check. It is the structural guarantee that
soft-delete/purge (`cascade_delete`) and rename (`cascade_rename`) keep
following an agent's lifecycle as the schema grows — the failure mode it
guards is "new agent-referencing table added, registry not updated, rows
silently orphaned on delete / stale on rename" (Issue #129 bug class).

Both `db/schema.py` (just a `TABLES` dict, no imports) and
`db/agent_cleanup.py` (stdlib-only) are import-safe without the backend
venv, so this test is a real CI gate — it does NOT skip when pydantic /
fastapi are absent.

The check is bidirectional:

  forward  — every agent-identifier column declared in `schema.TABLES`
             is registered in `AGENT_REFS` (or explicitly exempt).
  backward — every `AGENT_REFS` entry names a (table, column) that
             actually exists in `schema.TABLES` (catches typos / stale
             entries after a table is dropped or a column renamed).
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"

# Synthetic module names registered into sys.modules at import time so
# `@dataclass` can resolve `sys.modules[cls.__module__]` while exec'ing
# db/agent_cleanup.py (it inspects annotations under
# `from __future__ import annotations`). These are import-time stubs
# that monkeypatch can't reach (loaded before any fixture runs), so we
# use the sanctioned top-level `_STUBBED_MODULE_NAMES` +
# `_restore_sys_modules` pattern recognised by
# tests/lint_sys_modules.py (precedent:
# tests/unit/test_telegram_webhook_backfill.py) — keeps the synthetic
# modules from leaking into sibling test files in the same session.
_STUBBED_MODULE_NAMES = [
    "trinity_db_schema",
    "trinity_db_agent_cleanup",
]


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    saved = {n: sys.modules.get(n) for n in _STUBBED_MODULE_NAMES}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


def _load(mod_name: str, rel_path: str):
    """Load a backend module by file path, bypassing the `db` package
    __init__ (which pulls pydantic/fastapi transitively).

    The module is registered in `sys.modules` before `exec_module` so
    `@dataclass` (which resolves `sys.modules[cls.__module__]` to inspect
    annotations under `from __future__ import annotations`) works."""
    spec = importlib.util.spec_from_file_location(
        mod_name, str(_BACKEND / rel_path)
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


_schema = _load("trinity_db_schema", "db/schema.py")
_cleanup = _load("trinity_db_agent_cleanup", "db/agent_cleanup.py")


# Columns that name an agent identifier. Matches the set the registry
# tracks; a new agent-id column name would need adding here AND in
# AGENT_REFS (the test makes the second omission loud).
_AGENT_ID_COLUMNS = ("agent_name", "source_agent", "target_agent", "subscriber_agent")

_COL_RE = re.compile(
    r"^\s*(" + "|".join(_AGENT_ID_COLUMNS) + r")\b", re.MULTILINE
)

# (table, column) pairs that legitimately carry an agent identifier but
# are NOT cascade/rename-managed via AGENT_REFS. Keep this set minimal
# and documented — every entry is an audited exception.
_EXEMPT: set[tuple[str, str]] = {
    # The parent row itself. `cascade_delete()` is called for its
    # children; the `agent_ownership` row is deleted/soft-deleted by the
    # caller, by design (see agent_cleanup.py "Order" docstring).
    ("agent_ownership", "agent_name"),
}


def _schema_agent_columns() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for table, ddl in _schema.TABLES.items():
        for col in sorted(set(_COL_RE.findall(ddl))):
            found.add((table, col))
    return found


def test_every_schema_agent_column_is_registered_or_exempt():
    """FORWARD parity: no agent-referencing column may be silently
    unmanaged. Adding a new agent-referencing table without an
    `AgentRef` entry fails here."""
    registered = set(_cleanup.registered_columns())
    schema_cols = _schema_agent_columns()

    unmanaged = schema_cols - registered - _EXEMPT

    assert not unmanaged, (
        "Agent-referencing columns missing from AGENT_REFS in "
        "src/backend/db/agent_cleanup.py (rows would be orphaned on "
        "delete / stale on rename): "
        + ", ".join(f"{t}.{c}" for t, c in sorted(unmanaged))
        + ". Add an AgentRef entry (CASCADE or KEEP) or, if the column "
        "is intentionally managed elsewhere, add it to _EXEMPT with a "
        "comment explaining why."
    )


def test_every_registry_entry_exists_in_schema():
    """BACKWARD parity: AGENT_REFS must not reference a table/column
    that no longer exists (stale entry after drop/rename)."""
    schema_cols = _schema_agent_columns()
    stale = [
        (t, c)
        for (t, c) in _cleanup.registered_columns()
        if (t, c) not in schema_cols
    ]
    assert not stale, (
        "AGENT_REFS entries that no longer match db/schema.py "
        "(table dropped or column renamed?): "
        + ", ".join(f"{t}.{c}" for t, c in sorted(stale))
    )


def test_exempt_set_has_no_dead_entries():
    """An _EXEMPT pair must still be a real schema column — otherwise
    the exemption is silently masking nothing and should be removed."""
    schema_cols = _schema_agent_columns()
    dead = sorted(p for p in _EXEMPT if p not in schema_cols)
    assert not dead, (
        f"_EXEMPT contains pairs absent from schema.TABLES: {dead}. "
        "Remove the stale exemption."
    )


def test_keep_policy_tables_have_retention_discipline():
    """The agent_cleanup.py docstring asserts KEEP-policy tables 'must
    have [their] own retention discipline'. Lock the documented KEEP
    set so a new KEEP entry forces a conscious decision (and this
    test's update) rather than slipping in as a silent leak."""
    keep = {
        (r.table, r.column)
        for r in _cleanup.AGENT_REFS
        if r.policy == _cleanup.Policy.KEEP
    }
    assert keep == {
        ("schedule_executions", "agent_name"),   # #772 90d terminal-row sweep
        ("nevermined_payment_log", "agent_name"),  # forever financial record
    }, (
        f"KEEP-policy set changed: {sorted(keep)}. A KEEP table with no "
        "retention sweep is a slow leak — confirm the retention "
        "discipline exists, then update this assertion."
    )
