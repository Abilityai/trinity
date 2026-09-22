"""Declared metric registry — store + reconcile service (trinity-enterprise#477, R2).

Assertions land on **real rows** through the real `database.db` facade, never on
`mock.assert_called_with`. Two recorded lessons make that non-negotiable:

  * a mock-`db` suite is structurally blind to a facade gap (2026-07-06) —
    `DatabaseManager` delegates BY NAME, so a method the service calls but the
    facade never declared `AttributeError`s at runtime while every mocked test
    stays green. `test_every_db_call_resolves_on_the_real_facade` below derives
    that check from the service's own source rather than restating a list;
  * a column added to the DDL but not to `tables.py` is invisible until
    something SELECTs it (2026-06-23) — so
    `test_every_new_column_is_selectable` reads each one back.

`db_backend` parametrizes onto SQLite and, when `TEST_POSTGRES_URL` is set,
real PostgreSQL — which is the only place the `UNIQUE(agent_name, name)`
upsert path can be proven against the dialect it will run on in production.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend  # noqa: E402,F401

pytest.importorskip("sqlalchemy", reason="backend venv required")

from database import db  # noqa: E402
from db.tables import metric_definitions  # noqa: E402
from services import metric_registry, template_metrics  # noqa: E402

AGENT = "registry-agent"


def _declare(*entries):
    """Normalize a raw `metrics:` block the way every trigger does."""
    return template_metrics.normalize_declared_metrics(list(entries))


def _entry(**overrides):
    entry = {"name": "cycles", "type": "counter"}
    entry.update(overrides)
    return entry


def _reconcile(entries, source="create", agent=AGENT):
    return metric_registry.reconcile_declared_metrics(
        agent, _declare(*entries), source=source)


def _rows(agent=AGENT, include_retired=True):
    return {r["name"]: r for r in db.list_metric_definitions(
        agent, include_retired=include_retired)}


# ---------------------------------------------------------------------------
# Insert
# ---------------------------------------------------------------------------

def test_a_declared_metric_becomes_a_real_row(db_backend):
    summary = _reconcile([_entry(
        label="Cycles", description="research cycles", unit="runs",
        warning_threshold=5, critical_threshold=1, cadence="1h",
        direction="up_good", aggregation="sum", dimensions=["region"],
        **{"x-owner": "team-a"},
    )])

    assert summary.created == ["cycles"]
    row = _rows()["cycles"]
    assert row["agent_name"] == AGENT
    assert row["type"] == "counter"
    assert row["label"] == "Cycles"
    assert row["description"] == "research cycles"
    assert row["unit"] == "runs"
    assert row["warning_threshold"] == 5
    assert row["critical_threshold"] == 1
    assert row["cadence"] == "1h"
    assert row["cadence_seconds"] == 3600
    assert row["direction"] == "up_good"
    assert row["aggregation"] == "sum"
    assert row["dimensions"] == ["region"]
    assert row["extensions"] == {"x-owner": "team-a"}
    assert row["status"] == "active"
    assert row["source"] == "create"
    assert row["type_conflict"] is None
    assert row["first_declared_at"] and row["last_synced_at"]
    assert row["retired_at"] is None
    assert len(row["definition_hash"]) == 64


def test_status_values_round_trip(db_backend):
    _reconcile([_entry(name="mood", type="status", values=[
        {"value": "ok", "color": "green", "label": "Healthy"}])])
    assert _rows()["mood"]["values"] == [
        {"value": "ok", "color": "green", "label": "Healthy"}]


def test_no_declaration_writes_nothing(db_backend):
    """Bar 1: an agent with no `metrics:` block acquires no rows and no noise."""
    summary = _reconcile([])
    assert summary.declared == 0
    assert summary.created == [] and summary.retired == []
    assert db.list_metric_definitions(AGENT, include_retired=True) == []


def test_a_malformed_entry_never_reaches_the_registry(db_backend):
    """The reader drops it; this proves the store never sees it either — and
    that the finding travels with the reconcile (D-009 is a pure static check
    with no DB read, so the summary is the only place a refresh caller learns)."""
    summary = metric_registry.reconcile_metric_definitions(
        AGENT, {"metrics": [_entry(name="good"), _entry(name="BAD")]},
        source="create")
    assert list(_rows()) == ["good"]
    assert any("name: must match" in e for e in summary.errors)


# ---------------------------------------------------------------------------
# Update / unchanged
# ---------------------------------------------------------------------------

def test_an_unchanged_declaration_is_reported_as_unchanged(db_backend):
    _reconcile([_entry(label="Cycles")])
    summary = _reconcile([_entry(label="Cycles")], source="pull")
    assert summary.unchanged == 1
    assert summary.updated == [] and summary.created == []
    assert summary.changed is False


def test_a_changed_field_updates_in_place(db_backend):
    """The template is the ONLY writer of a definition — there is no operator
    edit surface — so update-in-place cannot clobber a human choice. That is
    what makes set-diff correct here where ent#89's schedules had to be
    skip-by-name."""
    _reconcile([_entry(label="Cycles")])
    first = _rows()["cycles"]

    summary = _reconcile([_entry(label="Research Cycles", unit="runs")],
                         source="pull")
    assert summary.updated == ["cycles"]
    row = _rows()["cycles"]
    assert row["label"] == "Research Cycles"
    assert row["unit"] == "runs"
    assert row["definition_hash"] != first["definition_hash"]
    # Identity survives an update: one metric, one row, one first-declared date.
    assert row["id"] == first["id"]
    assert row["first_declared_at"] == first["first_declared_at"]


def test_source_records_the_trigger_that_last_wrote(db_backend):
    _reconcile([_entry()])
    _reconcile([_entry(label="x")], source="start")
    assert _rows()["cycles"]["source"] == "start"


# ---------------------------------------------------------------------------
# Retire / revive — rows are never deleted
# ---------------------------------------------------------------------------

def test_a_metric_dropped_from_the_template_is_retired_not_deleted(db_backend):
    """ent#478 stores points by `(agent, name)`, so the definition is the only
    thing that can interpret points already recorded under that name."""
    _reconcile([_entry(name="a"), _entry(name="b")])
    summary = _reconcile([_entry(name="a")], source="pull")

    assert summary.retired == ["b"]
    rows = _rows()
    assert rows["b"]["status"] == "retired"
    assert rows["b"]["retired_at"] is not None
    assert list(_rows(include_retired=False)) == ["a"]


def test_an_emptied_metrics_block_retires_everything(db_backend):
    """Intentional: the template IS the truth. Distinct from an UNREADABLE
    template, which leaves the registry untouched (#2196) — see the refresh
    tests in test_ent477_definitions_endpoint.py."""
    _reconcile([_entry(name="a"), _entry(name="b")])
    summary = _reconcile([], source="pull")
    assert summary.retired == ["a", "b"]
    assert _rows(include_retired=False) == {}


def test_redeclaring_a_retired_metric_revives_the_same_row(db_backend):
    _reconcile([_entry()])
    original = _rows()["cycles"]
    _reconcile([], source="pull")

    summary = _reconcile([_entry()], source="refresh")
    assert summary.revived == ["cycles"]
    row = _rows()["cycles"]
    assert row["status"] == "active"
    assert row["retired_at"] is None
    assert row["id"] == original["id"], "a revive must not mint a second row"
    assert row["first_declared_at"] == original["first_declared_at"]


def test_retiring_twice_is_idempotent(db_backend):
    _reconcile([_entry()])
    _reconcile([], source="pull")
    summary = _reconcile([], source="pull")
    assert summary.retired == [], "an already-retired row must not re-retire"


# ---------------------------------------------------------------------------
# T5 — a `type` change is refused, recorded and surfaced
# ---------------------------------------------------------------------------

def test_a_type_change_is_refused_and_recorded(db_backend):
    _reconcile([_entry(type="counter")])
    summary = _reconcile([_entry(type="gauge", label="new")], source="pull")

    assert summary.type_change_refused == [
        {"name": "cycles", "from": "counter", "to": "gauge"}]
    row = _rows()["cycles"]
    assert row["type"] == "counter", "the stored shape must not flip"
    assert row["type_conflict"] == "gauge"
    # Everything else still updates — only the shape is frozen.
    assert row["label"] == "new"


def test_a_refused_type_change_is_never_counted_as_unchanged(db_backend):
    """The disagreement has to be visible on every reconcile, not only the one
    that introduced it — otherwise the second pull reports a clean run over a
    template the platform is still refusing to honor."""
    _reconcile([_entry(type="counter")])
    _reconcile([_entry(type="gauge")], source="pull")
    again = _reconcile([_entry(type="gauge")], source="pull")

    assert again.type_change_refused == [
        {"name": "cycles", "from": "counter", "to": "gauge"}]
    assert again.unchanged == 0
    assert again.changed is True


def test_a_refused_type_change_keeps_the_declared_status_values(db_backend):
    """A refused type change must not strip the domain of the type it kept.

    The stored `type` is frozen at `status`, but the re-declaration that was
    refused is a `counter` — it carries no `values:`. Copying the whole
    declaration payload over the row would null `status_values_json` and leave
    a status metric with no declared domain, which is exactly the widening
    ent#478's point validator must never have to absorb.
    """
    _reconcile([_entry(name="mood", type="status", values=[
        {"value": "ok"}, {"value": "degraded"}])])
    declared = _rows()["mood"]["values"]
    assert [v["value"] for v in declared] == ["ok", "degraded"]

    summary = _reconcile([_entry(name="mood", type="counter")], source="pull")

    assert summary.type_change_refused == [
        {"name": "mood", "from": "status", "to": "counter"}]
    row = _rows()["mood"]
    assert row["type"] == "status"
    assert row["values"] == declared, \
        "the refused declaration must not take the domain with it"


def test_the_conflict_clears_when_the_template_agrees_again(db_backend):
    _reconcile([_entry(type="counter")])
    _reconcile([_entry(type="gauge")], source="pull")
    assert _rows()["cycles"]["type_conflict"] == "gauge"

    _reconcile([_entry(type="counter")], source="pull")
    assert _rows()["cycles"]["type_conflict"] is None


def test_the_refusal_is_logged_as_a_warning(db_backend, caplog):
    import logging

    _reconcile([_entry(type="counter")])
    with caplog.at_level(logging.WARNING):
        _reconcile([_entry(type="gauge")], source="pull")
    assert any("refused a type change" in r.message for r in caplog.records)


def test_a_renamed_metric_is_an_honest_series_break(db_backend):
    """The documented remedy for a type change: a new name, the old one
    retires. Both rows survive, so ent#479 can render the break."""
    _reconcile([_entry(name="latency", type="counter")])
    _reconcile([_entry(name="latency_ms", type="duration")], source="pull")

    rows = _rows()
    assert rows["latency"]["status"] == "retired"
    assert rows["latency_ms"]["status"] == "active"
    assert rows["latency_ms"]["type"] == "duration"


# ---------------------------------------------------------------------------
# Isolation and reads
# ---------------------------------------------------------------------------

def test_one_agents_reconcile_never_touches_another(db_backend):
    _reconcile([_entry(name="a")], agent="agent-one")
    _reconcile([_entry(name="b")], agent="agent-two")
    _reconcile([], agent="agent-two", source="pull")

    assert list(_rows(agent="agent-one")) == ["a"]
    assert _rows(agent="agent-one")["a"]["status"] == "active"


def test_the_default_read_hides_retired_rows(db_backend):
    _reconcile([_entry(name="a"), _entry(name="b")])
    _reconcile([_entry(name="a")], source="pull")

    assert [d["name"] for d in
            metric_registry.list_metric_definitions(AGENT)] == ["a"]
    assert sorted(d["name"] for d in metric_registry.list_metric_definitions(
        AGENT, include_retired=True)) == ["a", "b"]


def test_the_read_never_leaks_the_raw_json_columns(db_backend):
    """The API shape is `values` / `dimensions` / `extensions` — a consumer must
    never have to json.loads a response field."""
    _reconcile([_entry(dimensions=["region"], **{"x-a": 1})])
    row = _rows()["cycles"]
    assert "status_values_json" not in row
    assert "dimensions_json" not in row
    assert "extensions_json" not in row


# ---------------------------------------------------------------------------
# Schema reach — the two guards the recorded lessons demand
# ---------------------------------------------------------------------------

_NEW_COLUMNS = (
    "id", "agent_name", "name", "type", "label", "description", "unit",
    "warning_threshold", "critical_threshold", "status_values_json",
    "cadence", "cadence_seconds", "direction", "aggregation",
    "dimensions_json", "extensions_json", "definition_hash", "type_conflict",
    "status", "source", "first_declared_at", "last_synced_at", "retired_at",
    "created_at", "updated_at",
)


def test_every_new_column_is_selectable(db_backend):
    """Learning 2026-06-23: a column in the DDL but not in `tables.py` (or the
    reverse) is invisible until something SELECTs it by name. This does."""
    from sqlalchemy import select
    from db.engine import get_engine

    _reconcile([_entry()])
    stmt = select(*[metric_definitions.c[c] for c in _NEW_COLUMNS]).where(
        metric_definitions.c.agent_name == AGENT)
    with get_engine().connect() as conn:
        assert conn.execute(stmt).mappings().first() is not None


def test_every_db_call_resolves_on_the_real_facade(db_backend):
    """E3 / learning 2026-07-06 — DERIVED, not restated. `DatabaseManager`
    delegates by name with no `__getattr__`, so a `db.<name>(...)` the facade
    never declared raises at runtime while a mocked suite stays green. Scan the
    service's own source for its call names and resolve each one."""
    source = (_BACKEND / "services" / "metric_registry.py").read_text()
    called = set(re.findall(r"\bdb\.(\w+)\(", source))
    assert called, "no db.<name>( calls found — has the service stopped using the facade?"
    missing = [name for name in sorted(called) if not callable(getattr(db, name, None))]
    assert not missing, f"metric_registry calls db.{missing} — not on DatabaseManager"


def test_the_table_is_registered_for_agent_cleanup():
    """R6's other half: without an AGENT_REFS row a deleted agent's definitions
    survive and a reused name inherits them (ent#478 would then ACCEPT points
    against another tenant's declaration). The cascade/rename behaviour itself
    is covered by the existing parity suites."""
    from db.agent_cleanup import AGENT_REFS

    refs = [r for r in AGENT_REFS if r.table == "metric_definitions"]
    assert len(refs) == 1
    assert refs[0].column == "agent_name"
    assert refs[0].policy.name == "CASCADE"


def test_the_unique_rule_is_declared_in_tables_not_only_in_the_ddl():
    """ent#366 lesson: `migrations/env.py` autogenerates against this MetaData,
    so a rule it does not know about is proposed for DROP by the first
    `--autogenerate` — and accepting that turns one reconcile into a second row
    per metric on every pull."""
    from sqlalchemy import UniqueConstraint

    uniques = [
        c for c in metric_definitions.constraints
        if isinstance(c, UniqueConstraint)
    ]
    assert any(
        sorted(col.name for col in c.columns) == ["agent_name", "name"]
        for c in uniques
    ), "UNIQUE(agent_name, name) missing from db/tables.py"


def test_the_ddl_carries_no_check_constraint():
    """E11 — `test_1819_rename_cascade_parity` seeds a placeholder row per
    AGENT_REFS table from NOT NULL introspection; a `CHECK (type IN …)` breaks
    that seed. The enums live in the reader, which is the one writer."""
    from db.schema import TABLES

    assert "CHECK" not in TABLES["metric_definitions"].upper()


# ---------------------------------------------------------------------------
# Concurrency + the PostgreSQL leg
# ---------------------------------------------------------------------------

def test_a_concurrent_insert_converges_instead_of_duplicating(db_backend):
    """E5 — the reconcile SELECTs, then upserts. A second worker inserting
    between the two must not produce a second row for one metric name; the
    `on_conflict_do_update` on `UNIQUE(agent_name, name)` is what makes the
    loser converge. Simulated by racing the insert in ahead of the upsert."""
    from sqlalchemy import select
    from db.engine import get_engine

    declared = _declare(_entry(label="from-worker-b"))
    for entry in declared:
        entry["definition_hash"] = template_metrics.definition_hash(entry)

    # Worker A has already written the row when worker B's reconcile runs.
    db.reconcile_metric_definitions(AGENT, declared, "pull")
    db.reconcile_metric_definitions(AGENT, declared, "start")

    with get_engine().connect() as conn:
        count = len(conn.execute(
            select(metric_definitions.c.id).where(
                metric_definitions.c.agent_name == AGENT,
                metric_definitions.c.name == "cycles",
            )).all())
    assert count == 1


@pytest.mark.requires_postgres
def test_the_alembic_revision_builds_the_table_on_postgres():
    """E1 — the SQLite track and `schema.py` agreeing proves nothing about the
    Alembic revision, which is the ONLY thing that runs on an existing
    PostgreSQL install. Run the migrations and read the table back."""
    import os

    if not os.environ.get("TEST_POSTGRES_URL"):
        pytest.skip("TEST_POSTGRES_URL not set")

    from sqlalchemy import select
    from db.engine import get_engine

    from db.alembic_runner import upgrade_to_head

    upgrade_to_head()
    with get_engine().connect() as conn:
        conn.execute(select(metric_definitions).limit(1)).all()


# ---------------------------------------------------------------------------
# The summary shape the hooks and the route both publish
# ---------------------------------------------------------------------------

def test_the_summary_serializes_to_the_documented_keys(db_backend):
    summary = _reconcile([_entry()])
    payload = summary.to_dict()
    assert set(payload) == {
        "agent_name", "source", "declared", "created", "updated", "revived",
        "retired", "unchanged", "type_change_refused", "errors",
    }
    assert json.dumps(payload)          # must be JSON-safe: it reaches the API


def test_changed_is_false_only_when_nothing_moved(db_backend):
    assert _reconcile([_entry()]).changed is True
    assert _reconcile([_entry()], source="pull").changed is False
    assert _reconcile([], source="pull").changed is True
