"""#4096: ``db_backend`` copies a per-process schema template instead of
rebuilding the schema for every test.

``_activate_backend`` used to run ``init_schema`` (92 tables, 187 indexes,
triggers) on a fresh file per test — ~330ms × ~2,300 tests, 36% of the unit
suite's phase time. ``init_schema`` is pure DDL, so a file built once per
worker process and copied per test is content-identical. These tests pin
both halves: the copy IS the schema a fresh build produces, and the fixture
no longer pays for a build once the template exists.
"""

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db_harness  # noqa: E402
from db_harness import db_backend  # noqa: E402,F401  (fixture)


def _schema_rows(path: Path) -> set[tuple]:
    with sqlite3.connect(path) as conn:
        return set(
            conn.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            ).fetchall()
        )


def _fresh_build(path: Path) -> set[tuple]:
    from db import schema as sch

    conn = sqlite3.connect(path)
    try:
        sch.init_schema(conn.cursor(), conn)
        conn.commit()
    finally:
        conn.close()
    return _schema_rows(path)


def test_template_is_built_once_per_process(tmp_path_factory):
    first = db_harness.sqlite_template(tmp_path_factory)
    second = db_harness.sqlite_template(tmp_path_factory)
    assert first == second
    assert first.exists()
    assert len(_schema_rows(first)) > 0


def test_activated_sqlite_db_equals_a_fresh_init_schema_build(
    db_backend, tmp_path, tmp_path_factory
):
    if db_backend != db_harness.SQLITE:
        pytest.skip("SQLite template only")
    active = Path(tmp_path / "trinity.db")
    assert active.exists(), "db_backend must leave the per-test file at tmp_path/trinity.db"
    assert _schema_rows(active) == _fresh_build(tmp_path / "fresh.db")


def test_activate_backend_does_not_rebuild_once_the_template_exists(
    tmp_path, tmp_path_factory, monkeypatch
):
    db_harness.sqlite_template(tmp_path_factory)  # prime

    def _boom() -> None:  # pragma: no cover - the assertion is that this is never reached
        raise AssertionError("bootstrap_schema() ran for a test DB — the template was not used")

    monkeypatch.setattr(db_harness, "bootstrap_schema", _boom)
    db_harness._activate_backend(db_harness.SQLITE, tmp_path, monkeypatch, tmp_path_factory)
    try:
        assert len(_schema_rows(tmp_path / "trinity.db")) > 0
    finally:
        from db.engine import dispose_engines

        dispose_engines()


def test_each_test_gets_its_own_writable_copy(db_backend, tmp_path, tmp_path_factory):
    """A row written through the active engine lands in the per-test file,
    never in the shared template."""
    if db_backend != db_harness.SQLITE:
        pytest.skip("SQLite template only")
    db_harness.seed_user(user_id=4096, username="u4096")
    assert db_harness.count("users", "id = :i", i=4096) == 1
    template = db_harness.sqlite_template(tmp_path_factory)
    with sqlite3.connect(template) as conn:
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
