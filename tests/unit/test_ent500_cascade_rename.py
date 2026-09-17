"""`cascade_rename` must sweep the tables it says it sweeps (ent#500).

⚠️ **Scope, stated accurately.** The plan that led here claimed a live
disclosure: that after `PUT /api/agents/{name}/rename` a registered private
table kept the OLD agent name, so the renamed agent lost its rows and a new
agent taking the freed name inherited them. **That was overstated.**
`db/agent_settings/metadata.py::rename_agent` — the only production caller —
carried its own `EXTRA_AGENT_REFS` loop (ent#46), so the production rename path
was already correct. Verified by reading every caller, not assumed.

What WAS wrong is narrower and still worth fixing: the behaviour lived in the
CALLER while `cascade_rename` — the shared function whose docstring
(`register_agent_owned_table`: *"the OSS delete + rename paths sweep/re-key
it"*) promised it — did not do it. That is the #1819 lesson one level down. Two
places answering one question, and a cross-repo module author reading the
contract would have believed the shared function. Any second caller of
`cascade_rename` would have silently dropped the sweep, and the recycled-name
cross-wire `delete_reports_to_refs` exists to prevent — one function below — is
what that would have produced.

So the fix is a CONSOLIDATION: the loop moved into `cascade_rename`, the
duplicate in `rename_agent` was deleted, and the docstring is now true.

The behavioural tests use a table deliberately NOT in the OSS `db/tables.py`
MetaData, because that absence is the whole reason the second registry exists —
`_table()` cannot build a statement for a private table, so the sweep has to go
through raw `text()` the way the delete sweep does. A test written against an
OSS table would pass on the pre-fix code and prove nothing.
"""
from __future__ import annotations

import importlib
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
    leak into whichever test runs next under pytest-randomly.

    Resolved through `importlib.import_module`, deliberately, rather than
    `from db import agent_cleanup`. The two are NOT equivalent under a suite
    that reloads modules: the `from`-form reads the `db` package attribute,
    which keeps pointing at a stale module object when something drops
    `db.agent_cleanup` from `sys.modules`, while the production call site
    (`rename_agent` does `from db.agent_cleanup import cascade_rename` at call
    time) goes through `sys.modules` and re-imports. Registering into the stale
    object then leaves the live registry empty, the sweep silently no-ops, and
    the failure reads as "the consolidation dropped the behaviour" — a green
    production path failing a test that is looking at the wrong module.
    """
    agent_cleanup = importlib.import_module("db.agent_cleanup")

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
        "cascade_rename left the registered table on the OLD name — a second "
        "caller would silently strand a private module's rows, and the next "
        "agent to take the freed name would inherit them"
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
    """The docstring is the only place this contract is written down for a
    cross-repo module author, so a lie there IS the defect — the code it
    described happened to be correct at one caller, which is precisely what made
    the lie survivable and therefore long-lived."""
    from db.agent_cleanup import register_agent_owned_table

    doc = register_agent_owned_table.__doc__ or ""
    assert "rename" in doc
    assert "recycled-name" in doc or "reuses the name" in doc, (
        "the docstring should say WHY both paths matter, not just that they do"
    )


# ---------------------------------------------------------------------------
# The caller level — the property the consolidation could break
# ---------------------------------------------------------------------------


@pytest.fixture
def full_schema_db(tmp_path, monkeypatch):
    """The real production schema, because `rename_agent` needs `agent_ownership`
    and the tag-ref sweep, not just the probe table."""
    monkeypatch.setenv("TRINITY_DB_PATH", str(tmp_path / "rename-full.db"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    import db.connection as conn_mod

    monkeypatch.setattr(conn_mod, "DB_PATH", str(tmp_path / "rename-full.db"))

    from sqlalchemy import text

    from db.engine import dispose_engines, get_engine
    from db_harness import bootstrap_schema

    dispose_engines()
    bootstrap_schema()
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                f"CREATE TABLE {PROBE_TABLE} ("
                " id INTEGER PRIMARY KEY, agent_name TEXT NOT NULL, payload TEXT)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO agent_ownership (agent_name, owner_id, created_at) "
                "VALUES ('scout', 1, '2026-09-07T10:00:00Z')"
            )
        )
    yield engine
    dispose_engines()


def test_the_production_rename_path_still_rekeys_a_registered_table(
    full_schema_db, registry_isolated
):
    """`rename_agent` used to carry its OWN `EXTRA_AGENT_REFS` loop, which is why
    the production path was correct while `cascade_rename` was not. The loop was
    deleted when `cascade_rename` grew the sweep — so this asserts the caller did
    not LOSE the behaviour in the consolidation.

    Without it, moving the loop is an unverified refactor of the one path that
    actually mattered.
    """
    from sqlalchemy import text

    from db.agent_settings.metadata import MetadataMixin

    registry_isolated.register_agent_owned_table(PROBE_TABLE, "agent_name")
    with full_schema_db.begin() as conn:
        conn.execute(
            text(
                f"INSERT INTO {PROBE_TABLE} (agent_name, payload) "
                "VALUES ('scout', 'mine')"
            )
        )

    # Precondition, asserted rather than assumed: the registry the PRODUCTION
    # call site will read must be the one just written to, and the probe table
    # must be visible to the same connection `cascade_rename` gets. Both are
    # process-global state this test does not own, and when either is wrong the
    # row simply does not move — which is indistinguishable, at the final
    # assert, from the regression this test exists to catch. Checking them here
    # means an isolation failure says so instead of impersonating a real one.
    from sqlalchemy import inspect as sa_inspect

    from db.agent_cleanup import EXTRA_AGENT_REFS as live_registry

    assert (PROBE_TABLE, "agent_name") in live_registry, (
        "the probe table is not in the registry the production path reads — "
        "test isolation, not a rename regression. "
        f"live={list(live_registry)} fixture_module={registry_isolated!r}"
    )
    with full_schema_db.begin() as conn:
        assert sa_inspect(conn).has_table(PROBE_TABLE), (
            "the probe table is not visible on the engine the rename will use "
            "— test isolation, not a rename regression"
        )

    assert MetadataMixin().rename_agent("scout", "ranger") is True

    with full_schema_db.begin() as conn:
        left = conn.execute(
            text(f"SELECT COUNT(*) FROM {PROBE_TABLE} WHERE agent_name = 'scout'")
        ).scalar()
        moved = conn.execute(
            text(f"SELECT COUNT(*) FROM {PROBE_TABLE} WHERE agent_name = 'ranger'")
        ).scalar()
    assert (left, moved) == (0, 1), (
        "the production rename path stopped re-keying an entitled module's "
        "table — the consolidation dropped the behaviour instead of moving it"
    )


def test_rename_agent_carries_no_table_loop_of_its_own():
    """Structural, and the point of the whole exercise: one question, one place.
    `rename_agent` regrowing a private sweep is how the two lists diverged in
    #1819, and how `cascade_rename`'s docstring came to disagree with the code.
    """
    import inspect

    from db.agent_settings.metadata import MetadataMixin

    body = inspect.getsource(MetadataMixin.rename_agent)
    assert "cascade_rename(" in body, "rename must derive its tables from the registry"
    assert "for table, column in EXTRA_AGENT_REFS" not in body, (
        "rename_agent has regrown its own EXTRA_AGENT_REFS loop — the sweep "
        "belongs in cascade_rename so every caller inherits it"
    )
