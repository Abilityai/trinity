"""#2216 — boot-time pre-migration backup (BKUP-012).

The contract under test is mostly about what the hook must NOT do:

- it must never raise out of ``init_database()`` (which runs at import — a
  raise crash-loops the backend permanently, the #1638 seed contract);
- a fresh install must skip SILENTLY (INFO, no ERROR on a first boot);
- a corrupt DB must leave the downstream exception fingerprint byte-compatible
  with the incident signature — ``run_all_migrations`` on the same connection
  raises exactly what it raised before this feature existed;
- a failed backup must never evict a healthy artifact.
"""
from __future__ import annotations

import logging
import sqlite3
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
if _BACKEND_STR not in sys.path:
    sys.path.insert(0, _BACKEND_STR)

from db import backup_primitives as bp  # noqa: E402
from db.migrations import MIGRATIONS, run_all_migrations  # noqa: E402

pytestmark = pytest.mark.unit

# The incident's literal corruption: an HTTP response body over the SQLite
# header at offset 0 (issue #2216 hexdump).
_INCIDENT_BYTES = (
    b"HTTP/1.1 200 OK\r\nserver: uvicorn\r\ncontent-length: 13\r\n\r\n"
)


def _existing_install(path: Path, *, applied: int) -> None:
    """A DB that looks like an existing install: real content plus a
    schema_migrations table recording the first `applied` migration names —
    so `migration_health` reports the rest as pending."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
        conn.executemany(
            "INSERT INTO users (name) VALUES (?)",
            [(f"u{i}",) for i in range(200)],  # push the file past 4 KiB
        )
        conn.execute(
            "CREATE TABLE system_settings ("
            "key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        conn.executemany(
            "INSERT INTO schema_migrations (name, applied_at) VALUES (?, '2026-01-01')",
            [(name,) for name, _fn in MIGRATIONS[:applied]],
        )
        conn.commit()
    finally:
        conn.close()


def _hook(conn, db_path: Path, backup_dir: Path):
    cursor = conn.cursor()
    return bp.maybe_backup_before_migrations(
        cursor, conn, db_path=str(db_path), backup_dir=str(backup_dir)
    )


class TestSkipBranches:

    def test_no_pending_migrations_no_backup(self, tmp_path):
        db_path = tmp_path / "t.db"
        _existing_install(db_path, applied=len(MIGRATIONS))  # all recorded
        conn = sqlite3.connect(str(db_path))
        try:
            assert _hook(conn, db_path, tmp_path / "backups") is None
        finally:
            conn.close()
        assert not (tmp_path / "backups").exists(), (
            "the common no-op boot must not even touch the filesystem"
        )

    def test_fresh_install_is_a_SILENT_skip(self, tmp_path, caplog):
        """No schema_migrations table yet (the hook runs BEFORE the first
        migration pass creates it) — a first boot must log INFO, never ERROR."""
        db_path = tmp_path / "t.db"
        sqlite3.connect(str(db_path)).close()  # empty fresh file
        conn = sqlite3.connect(str(db_path))
        try:
            with caplog.at_level(logging.INFO, logger="db.backup_primitives"):
                assert _hook(conn, db_path, tmp_path / "backups") is None
        finally:
            conn.close()
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors == [], (
            f"a fresh install must not emit a scary ERROR on first boot: {errors}"
        )
        assert any("fresh install" in r.message for r in caplog.records)
        assert not (tmp_path / "backups").exists()

    def test_empty_schema_migrations_is_fresh_too(self, tmp_path):
        db_path = tmp_path / "t.db"
        _existing_install(db_path, applied=0)  # table exists, zero rows
        conn = sqlite3.connect(str(db_path))
        try:
            assert _hook(conn, db_path, tmp_path / "backups") is None
        finally:
            conn.close()
        assert not (tmp_path / "backups").exists()


class TestBackupBranch:

    def test_pending_migrations_take_artifact_before_mutation(self, tmp_path):
        db_path = tmp_path / "t.db"
        _existing_install(db_path, applied=1)  # rest pending
        backups = tmp_path / "backups"
        conn = sqlite3.connect(str(db_path))
        try:
            result = _hook(conn, db_path, backups)
        finally:
            conn.close()
        assert result is not None and result["status"] == "ok"
        artifacts = list(backups.glob("pre-migration-*.db"))
        assert len(artifacts) == 1
        # The artifact is a valid pre-mutation snapshot of the install.
        copy = sqlite3.connect(f"file:{artifacts[0]}?mode=ro", uri=True)
        try:
            assert copy.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 200
            assert (
                copy.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
                == 1
            ), "the copy must predate the migration run (only 1 recorded)"
        finally:
            copy.close()

    def test_status_rows_written_durably(self, tmp_path):
        db_path = tmp_path / "t.db"
        _existing_install(db_path, applied=1)
        conn = sqlite3.connect(str(db_path))
        try:
            _hook(conn, db_path, tmp_path / "backups")
            rows = dict(
                conn.execute(
                    "SELECT key, value FROM system_settings WHERE key LIKE 'db_backup%'"
                ).fetchall()
            )
        finally:
            conn.close()
        assert rows.get("db_backup_last_status") == "ok"
        assert rows.get("db_backup_last_trigger") == "boot_pre_migration"
        assert rows.get("db_backup_last_success_at")


class TestCorruptAndFailOpen:

    def _corrupt_db(self, tmp_path) -> Path:
        db_path = tmp_path / "t.db"
        _existing_install(db_path, applied=1)
        with open(db_path, "r+b") as fh:
            fh.write(_INCIDENT_BYTES)  # overwrite the header at offset 0
        return db_path

    def test_corrupt_db_fails_open_with_unchanged_downstream_fingerprint(
        self, tmp_path, caplog
    ):
        """THE incident-fingerprint test. Baseline (no hook): what
        run_all_migrations raises on the corrupt DB. With the hook first: the
        hook swallows its own failure, and the SAME run_all_migrations call
        raises the SAME exception type + message — uvicorn dies at import with
        an identical signature, so nothing downstream (healthchecks, log
        greps, runbooks keyed on 'file is not a database') changes."""
        # Baseline.
        db_a = self._corrupt_db(tmp_path / "a")
        (tmp_path / "a").mkdir(exist_ok=True)
        conn_a = sqlite3.connect(str(db_a))
        with pytest.raises(sqlite3.DatabaseError) as baseline:
            run_all_migrations(conn_a.cursor(), conn_a)
        conn_a.close()

        # With the hook running first (as database.init_database now does).
        db_b = self._corrupt_db(tmp_path / "b")
        conn_b = sqlite3.connect(str(db_b))
        with caplog.at_level(logging.ERROR, logger="db.backup_primitives"):
            result = _hook(conn_b, db_b, tmp_path / "b" / "backups")
        assert result is None, "the hook must swallow the corrupt-DB failure"
        assert any(
            "FAILED" in r.message for r in caplog.records
        ), "the corrupt branch must be LOUD (ERROR), unlike the fresh skip"
        with pytest.raises(sqlite3.DatabaseError) as with_hook:
            run_all_migrations(conn_b.cursor(), conn_b)
        conn_b.close()

        assert type(with_hook.value) is type(baseline.value), (
            f"exception TYPE changed: {type(baseline.value)} → "
            f"{type(with_hook.value)} — the incident fingerprint must be "
            f"byte-compatible"
        )
        assert str(with_hook.value) == str(baseline.value), (
            "exception MESSAGE changed — runbooks/log greps keyed on the "
            "incident signature would miss it"
        )

    @pytest.fixture(autouse=True)
    def _mk_subdirs(self, tmp_path):
        (tmp_path / "a").mkdir(exist_ok=True)
        (tmp_path / "b").mkdir(exist_ok=True)

    def test_corrupt_source_never_evicts_healthy_artifact(self, tmp_path):
        db_path = self._corrupt_db(tmp_path / "a")
        backups = tmp_path / "a" / "backups"
        backups.mkdir()
        healthy = backups / "trinity-backup-20260101.db"
        healthy.write_bytes(b"healthy artifact bytes")
        conn = sqlite3.connect(str(db_path))
        try:
            assert _hook(conn, db_path, backups) is None
        finally:
            conn.close()
        assert healthy.exists()
        assert healthy.read_bytes() == b"healthy artifact bytes"
        # And no tmp litter from the failed attempt.
        assert list(backups.glob("*.tmp.*")) == []

    def test_arbitrary_exception_never_propagates(self, tmp_path, monkeypatch):
        db_path = tmp_path / "a" / "t.db"
        _existing_install(db_path, applied=1)
        monkeypatch.setattr(
            bp, "sqlite_backup_to",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        conn = sqlite3.connect(str(db_path))
        try:
            # Must not raise — init_database runs at import; a raise here is a
            # permanent boot crash-loop.
            assert _hook(conn, db_path, tmp_path / "a" / "backups") is None
        finally:
            conn.close()

    def test_failed_verify_unlinks_tmp(self, tmp_path, monkeypatch):
        db_path = tmp_path / "a" / "t.db"
        _existing_install(db_path, applied=1)
        backups = tmp_path / "a" / "backups"
        monkeypatch.setattr(
            bp, "verify_sqlite_backup",
            lambda *a, **k: (_ for _ in ()).throw(
                bp.BackupVerificationError("bad copy")
            ),
        )
        conn = sqlite3.connect(str(db_path))
        try:
            assert _hook(conn, db_path, backups) is None
        finally:
            conn.close()
        assert list(backups.glob("*.tmp.*")) == [], (
            "the finally must unlink the run's own tmp on any failure"
        )
        assert list(backups.glob("pre-migration-*.db")) == [], (
            "a failed copy must never enter the artifact namespace"
        )
