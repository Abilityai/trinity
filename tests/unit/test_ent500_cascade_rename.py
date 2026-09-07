"""B3 — `cascade_rename` must sweep the tables it says it sweeps.

`register_agent_owned_table` is how an entitled module joins the OSS agent
cascade, and its docstring said the "delete + rename paths sweep/re-key" the
registered table. Only the delete path did: `cascade_delete` iterated
`EXTRA_AGENT_REFS`, `cascade_rename` iterated `AGENT_REFS` alone.

The consequence is not a stale row, it is a cross-wire. After
`PUT /api/agents/{name}/rename` the registered rows still carry the OLD name, so
the renamed agent silently loses them — and a NEW agent created later under that
freed name INHERITS them. For a table that records which human an agent serves,
that means one agent inheriting another's people. The codebase names this exact
class one function away, in `delete_reports_to_refs`: *"a dangling ref would
silently re-attach to an unrelated agent that reuses the name."*

The behavioural test uses a table that is deliberately NOT in the OSS
`db/tables.py` MetaData, because that is the whole reason the extra registry
exists — `_table()` cannot build a statement for a private table, so a rename
sweep has to go through raw `text()` the way the delete sweep does. A test
against an OSS table would pass on the pre-fix code.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

_TESTS = Path(__file__).resolve().parents[1]
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

PROBE_TABLE = "probe_module_agent_owned"


@pytest.fixture
def registry_isolated():
    """`EXTRA_AGENT_REFS` is process-global; a registration left behind would
    leak into whichever test runs next under pytest-randomly."""
    from db import agent_cleanup

    saved = list(agent_cleanup.EXTRA_AGENT_REFS)
    yield agent_cleanup
    agent_cleanup.EXTRA_AGENT_REFS[:] = saved


@pytest.fixture
def probe_db(tmp_path, monkeypatch):
    """A throwaway SQLite file carrying one table that the OSS MetaData has
    never heard of — exactly the shape of an entitled module's private table."""
    monkeypatch.setenv("TRINITY_DB_PATH", str(tmp_path / "rename.db"))
    import db.connection as conn_mod

    monkeypatch.setattr(conn_mod, "DB_PATH", str(tmp_path / "rename.db"))

    from sqlalchemy import text

    from db.engine import dispose_engines, get_engine

    dispose_engines()
    engine = get_engine()
    # The OSS tables the cascade touches must exist — `cascade_delete` ends in
    # `delete_reports_to_refs`, which is a real statement against `agent_tags`,
    # not an existence-guarded sweep.
    from conftest import ensure_schema_tables

    ensure_schema_tables("agent_tags", "agent_ownership")
    with engine.begin() as conn:
        conn.execute(
            text(
                f"CREATE TABLE {PROBE_TABLE} ("
                " id INTEGER PRIMARY KEY, agent_name TEXT NOT NULL, payload TEXT)"
            )
        )
    yield engine
    dispose_engines()


def _rows(engine):
    from sqlalchemy import text

    with engine.begin() as conn:
        return dict(
            conn.execute(
                text(f"SELECT payload, agent_name FROM {PROBE_TABLE}")
            ).all()
        )


def test_rename_rekeys_a_registered_module_table(probe_db, registry_isolated):
    from sqlalchemy import text

    registry_isolated.register_agent_owned_table(PROBE_TABLE, "agent_name")

    with probe_db.begin() as conn:
        for payload, agent in (("mine", "scout"), ("theirs", "other-agent")):
            conn.execute(
                text(
                    f"INSERT INTO {PROBE_TABLE} (agent_name, payload) "
                    "VALUES (:a, :p)"
                ),
                {"a": agent, "p": payload},
            )

    with probe_db.begin() as conn:
        updated = registry_isolated.cascade_rename(conn, "scout", "ranger")

    rows = _rows(probe_db)
    assert rows["mine"] == "ranger", (
        "the registered table kept the OLD name — the renamed agent has lost "
        "its rows, and the next agent to take the freed name will inherit them"
    )
    assert rows["theirs"] == "other-agent", "an unrelated agent's row was rewritten"
    assert updated.get(PROBE_TABLE) == 1


def test_rename_reports_the_registered_table_in_its_counts(
    probe_db, registry_isolated
):
    from sqlalchemy import text

    registry_isolated.register_agent_owned_table(PROBE_TABLE, "agent_name")
    with probe_db.begin() as conn:
        for i in range(3):
            conn.execute(
                text(
                    f"INSERT INTO {PROBE_TABLE} (agent_name, payload) "
                    "VALUES ('scout', :p)"
                ),
                {"p": f"row-{i}"},
            )
    with probe_db.begin() as conn:
        updated = registry_isolated.cascade_rename(conn, "scout", "ranger")
    assert updated[PROBE_TABLE] == 3


def test_rename_skips_a_registered_table_that_is_not_installed(
    probe_db, registry_isolated
):
    """OSS-only and partial installs register nothing, but a build that
    registers a table its database does not have must not fail the rename."""
    registry_isolated.register_agent_owned_table("table_that_does_not_exist")
    with probe_db.begin() as conn:
        updated = registry_isolated.cascade_rename(conn, "scout", "ranger")
    assert "table_that_does_not_exist" not in updated


def test_delete_and_rename_consume_the_SAME_registry(probe_db, registry_isolated):
    """The property the docstring promised. Asserted behaviourally rather than
    by reading the source: both paths must act on a table that is registered,
    and neither may act on one that is not."""
    from sqlalchemy import text

    registry_isolated.register_agent_owned_table(PROBE_TABLE, "agent_name")
    with probe_db.begin() as conn:
        conn.execute(
            text(
                f"INSERT INTO {PROBE_TABLE} (agent_name, payload) "
                "VALUES ('scout', 'row')"
            )
        )
    with probe_db.begin() as conn:
        assert registry_isolated.cascade_rename(conn, "scout", "ranger").get(
            PROBE_TABLE
        ) == 1
    with probe_db.begin() as conn:
        assert registry_isolated.cascade_delete(conn, "ranger").get(PROBE_TABLE) == 1
    assert _rows(probe_db) == {}


def test_the_docstring_no_longer_promises_what_the_code_did_not_do():
    """The docstring is the only place the contract is written down for a
    cross-repo module author, so a lie there is the defect's real cause."""
    from db.agent_cleanup import register_agent_owned_table

    doc = register_agent_owned_table.__doc__ or ""
    assert "rename" in doc
    assert "recycled-name" in doc or "reuses the name" in doc, (
        "the docstring should say WHY both paths matter, not just that they do"
    )
