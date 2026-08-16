"""#2216 — database-backup primitives (db/backup_primitives.py).

The properties that matter more than any single assertion:

1. **The copy is the safe primitive** — a consistent, standalone snapshot via
   the online-backup API, never a raw file copy (the incident class).
2. **Prune can never delete the last recovery points** — the fixed
   ``BACKUP_MIN_KEEP`` floor holds at any window, including the #1644 "small
   valid integer" catastrophic input (``retention_days=1``).
3. **Prune touches only the artifact namespace** — a failed copy never entered
   it (verify-before-replace), and foreign files are never collateral.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
if _BACKEND_STR not in sys.path:
    sys.path.insert(0, _BACKEND_STR)

from db import backup_primitives as bp  # noqa: E402

pytestmark = pytest.mark.unit


def _make_db(path: Path, rows: int = 50) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        conn.executemany(
            "INSERT INTO t (v) VALUES (?)", [(f"row-{i}",) for i in range(rows)]
        )
        conn.commit()
    finally:
        conn.close()


def _row_count(path: Path) -> int:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    finally:
        conn.close()


def _touch_artifact(dirpath: Path, name: str, *, age_days: float = 0) -> Path:
    p = dirpath / name
    p.write_bytes(b"x" * 16)
    if age_days:
        old = time.time() - age_days * 86400
        os.utime(p, (old, old))
    return p


# ---------------------------------------------------------------------------
# Copy + verify
# ---------------------------------------------------------------------------

class TestSqliteBackup:

    def test_produces_consistent_standalone_copy(self, tmp_path):
        src = tmp_path / "src.db"
        _make_db(src, rows=50)
        dest = tmp_path / "copy.db"
        stats = bp.sqlite_backup_to(str(src), str(dest))
        assert stats["size_bytes"] > 0
        bp.verify_sqlite_backup(str(dest))  # quick_check ok + sqlite_master
        assert _row_count(dest) == 50
        # Standalone: no sidecars materialize beside the copy.
        assert not (tmp_path / "copy.db-wal").exists()
        assert not (tmp_path / "copy.db-journal").exists()

    def test_copy_is_a_snapshot_not_a_live_view(self, tmp_path):
        """Writes to the source AFTER the backup never reach the copy."""
        src = tmp_path / "src.db"
        _make_db(src, rows=10)
        dest = tmp_path / "copy.db"
        bp.sqlite_backup_to(str(src), str(dest))
        conn = sqlite3.connect(str(src))
        conn.execute("INSERT INTO t (v) VALUES ('after')")
        conn.commit()
        conn.close()
        assert _row_count(src) == 11
        assert _row_count(dest) == 10
        bp.verify_sqlite_backup(str(dest))

    def test_copy_survives_concurrent_committing_writer(self, tmp_path):
        """A writer committing around the backup window never corrupts the
        copy: it is a valid standalone DB whose row count is between the
        initial and final counts (DELETE-mode: the writer may briefly wait on
        the copy's read lock — bounded, and that is the documented cost)."""
        src = tmp_path / "src.db"
        _make_db(src, rows=100)
        dest = tmp_path / "copy.db"
        stop = threading.Event()

        def writer():
            conn = sqlite3.connect(str(src), timeout=30.0)
            try:
                i = 0
                while not stop.is_set() and i < 500:
                    conn.execute("INSERT INTO t (v) VALUES (?)", (f"w{i}",))
                    conn.commit()
                    i += 1
            finally:
                conn.close()

        t = threading.Thread(target=writer)
        t.start()
        try:
            bp.sqlite_backup_to(str(src), str(dest))
        finally:
            stop.set()
            t.join(timeout=30)
        bp.verify_sqlite_backup(str(dest))
        copied = _row_count(dest)
        assert 100 <= copied <= _row_count(src)

    def test_thread_affinity_contract_via_to_thread(self, tmp_path):
        """The service calls the sync worker via asyncio.to_thread; both
        connections open AND close inside it (learnings 2026-07-20 — a
        connection crossing threads raises sqlite3.ProgrammingError)."""
        src = tmp_path / "src.db"
        _make_db(src, rows=5)
        dest = tmp_path / "copy.db"

        async def run():
            return await asyncio.to_thread(
                bp.sqlite_backup_to, str(src), str(dest)
            )

        stats = asyncio.run(run())
        assert stats["size_bytes"] > 0
        bp.verify_sqlite_backup(str(dest))

    def test_duration_warning_names_the_busy_timeout_wall(self, tmp_path, monkeypatch, caplog):
        src = tmp_path / "src.db"
        _make_db(src, rows=5)

        class _SlowClock:
            _values = iter([0.0, 25.0])  # start, end: 25s copy

            @classmethod
            def monotonic(cls):
                try:
                    return next(cls._values)
                except StopIteration:
                    return 25.0

        monkeypatch.setattr(bp, "time", _SlowClock)
        with caplog.at_level(logging.WARNING, logger="db.backup_primitives"):
            bp.sqlite_backup_to(str(src), str(tmp_path / "copy.db"))
        assert any("30s busy" in r.message for r in caplog.records), (
            "a copy past _COPY_DURATION_WARN_SECONDS must WARN naming the "
            "30s busy-timeout wall (D1 tripwire)"
        )

    def test_verification_rejects_corrupt_copy(self, tmp_path):
        src = tmp_path / "src.db"
        _make_db(src, rows=5)
        dest = tmp_path / "copy.db"
        bp.sqlite_backup_to(str(src), str(dest))
        # Corrupt the COPY (not the source): stomp the header.
        with open(dest, "r+b") as fh:
            fh.write(b"HTTP/1.1 200 OK\r\n")
        with pytest.raises(bp.BackupVerificationError):
            bp.verify_sqlite_backup(str(dest))

    def test_verification_rejects_empty_db(self, tmp_path):
        empty = tmp_path / "empty.db"
        sqlite3.connect(str(empty)).close()  # valid file, zero tables
        with pytest.raises(bp.BackupVerificationError):
            bp.verify_sqlite_backup(str(empty))

    def test_atomic_replace_is_last_writer_wins_never_torn(self, tmp_path):
        """Two concurrent writers to the same day-keyed final name resolve via
        os.replace: last writer wins on ONE intact file — the 'lease is
        suppression, not correctness' framing (D3)."""
        src = tmp_path / "src.db"
        _make_db(src, rows=7)
        final = tmp_path / bp.sqlite_artifact_name("20260816")
        tmp_a = f"{final}.tmp.1111"
        tmp_b = f"{final}.tmp.2222"
        bp.sqlite_backup_to(str(src), tmp_a)
        bp.sqlite_backup_to(str(src), tmp_b)
        os.replace(tmp_a, final)
        os.replace(tmp_b, final)  # no exception, no torn file
        bp.verify_sqlite_backup(str(final))
        assert _row_count(final) == 7


# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------

class TestNaming:

    def test_day_keyed_names(self):
        assert bp.sqlite_artifact_name("20260816") == "trinity-backup-20260816.db"
        assert bp.pg_artifact_name("20260816") == "trinity-backup-20260816.dump"
        assert len(bp.utc_day_key()) == 8 and bp.utc_day_key().isdigit()
        pm = bp.pre_migration_artifact_name()
        assert pm.startswith("pre-migration-") and pm.endswith(".db")

    def test_every_artifact_name_matches_a_prune_pattern(self):
        """A produced name outside ARTIFACT_PATTERNS would be invisible to
        prune forever — unbounded growth."""
        import fnmatch
        for name in (
            bp.sqlite_artifact_name(bp.utc_day_key()),
            bp.pg_artifact_name(bp.utc_day_key()),
            bp.pre_migration_artifact_name(),
        ):
            assert any(
                fnmatch.fnmatch(name, pat) for pat in bp.ARTIFACT_PATTERNS
            ), f"{name} matches no prune pattern"

    def test_tmp_path_is_pid_suffixed_and_not_an_artifact(self):
        import fnmatch
        tmp = bp.tmp_path_for("/x/trinity-backup-20260816.db")
        assert f".tmp.{os.getpid()}" in tmp
        assert not any(
            fnmatch.fnmatch(Path(tmp).name, pat) for pat in bp.ARTIFACT_PATTERNS
        ), "a tmp must never be inside the prune's artifact namespace"


# ---------------------------------------------------------------------------
# Prune — THE guard between a retention slip and data loss. Merciless.
# ---------------------------------------------------------------------------

class TestPrune:

    def test_min_keep_floor_is_at_least_two_and_not_a_knob(self):
        """The ONLY guard between a `backup_retention_days=1` slip and zero
        recovery points. Direction-pinned (#1644 shape): lowering below 2
        needs a reviewer, not a one-line diff."""
        assert bp.BACKUP_MIN_KEEP >= 2
        # Not settings-backed: it must be a module constant, not resolved
        # through any settings read.
        import inspect
        src = inspect.getsource(bp)
        assert "get_setting_value" not in src, (
            "BACKUP_MIN_KEEP (or anything in the primitives leaf) must not "
            "read settings — the floor is a constant by design"
        )

    def test_retention_one_day_keeps_min_keep_newest(self, tmp_path):
        """THE test: retention_days=1 with everything ancient still keeps the
        MIN_KEEP newest artifacts."""
        for i in range(6):
            _touch_artifact(
                tmp_path, f"trinity-backup-2026010{i}.db", age_days=30 + i
            )
        deleted = bp.prune_backups(str(tmp_path), 1)
        survivors = sorted(p.name for p in tmp_path.iterdir())
        assert len(survivors) == bp.BACKUP_MIN_KEEP
        assert len(deleted) == 6 - bp.BACKUP_MIN_KEEP
        # The survivors are the NEWEST ones (lowest age = highest index here).
        assert survivors == sorted(
            f"trinity-backup-2026010{i}.db" for i in range(bp.BACKUP_MIN_KEEP)
        )

    def test_floor_holds_across_patterns(self, tmp_path):
        """The floor counts across the union of artifact patterns — mixed
        sqlite + pre-migration artifacts still leave MIN_KEEP total."""
        _touch_artifact(tmp_path, "trinity-backup-20260101.db", age_days=40)
        _touch_artifact(tmp_path, "pre-migration-20260102-010101.db", age_days=39)
        _touch_artifact(tmp_path, "trinity-backup-20260103.dump", age_days=38)
        _touch_artifact(tmp_path, "trinity-backup-20260104.db", age_days=37)
        bp.prune_backups(str(tmp_path), 1)
        assert len(list(tmp_path.iterdir())) == bp.BACKUP_MIN_KEEP

    def test_inside_window_is_never_pruned(self, tmp_path):
        for i in range(6):
            _touch_artifact(tmp_path, f"trinity-backup-2026080{i}.db", age_days=2)
        deleted = bp.prune_backups(str(tmp_path), 14)
        assert deleted == []
        assert len(list(tmp_path.iterdir())) == 6

    def test_out_of_window_pruned_beyond_floor(self, tmp_path):
        for i in range(3):
            _touch_artifact(tmp_path, f"trinity-backup-2026081{i}.db", age_days=1)
        old = _touch_artifact(tmp_path, "trinity-backup-20260101.db", age_days=40)
        deleted = bp.prune_backups(str(tmp_path), 14)
        assert deleted == [old.name]
        assert not old.exists()

    def test_prune_touches_only_known_patterns(self, tmp_path):
        foreign = [
            _touch_artifact(tmp_path, "notes.txt", age_days=400),
            _touch_artifact(tmp_path, "trinity.db", age_days=400),
            _touch_artifact(tmp_path, "trinity-backup-20260101.db.tmp.99", age_days=400),
            _touch_artifact(tmp_path, "somebackup.dump", age_days=400),
        ]
        deleted = bp.prune_backups(str(tmp_path), 1)
        assert deleted == []
        for f in foreign:
            assert f.exists(), f"{f.name} is not an artifact and must survive"

    def test_nonpositive_retention_deletes_nothing(self, tmp_path):
        """Defensive: a smuggled 0/negative (validated away at the API, but a
        direct DB write exists) must not mean 'delete everything'."""
        _touch_artifact(tmp_path, "trinity-backup-20260101.db", age_days=400)
        assert bp.prune_backups(str(tmp_path), 0) == []
        assert bp.prune_backups(str(tmp_path), -5) == []
        assert len(list(tmp_path.iterdir())) == 1

    def test_prune_missing_dir_is_a_noop(self, tmp_path):
        assert bp.prune_backups(str(tmp_path / "nope"), 14) == []


# ---------------------------------------------------------------------------
# Tmp hygiene
# ---------------------------------------------------------------------------

class TestTmpSweep:

    def test_sweeps_aged_orphan_keeps_fresh(self, tmp_path):
        old = _touch_artifact(
            tmp_path, "trinity-backup-20260101.db.tmp.4242", age_days=25 / 24
        )  # 25h — past the 24h crash window
        fresh = _touch_artifact(tmp_path, "trinity-backup-20260816.db.tmp.4243")
        removed = bp.sweep_stale_tmps(str(tmp_path))
        assert removed == [old.name]
        assert not old.exists()
        assert fresh.exists(), "a fresh tmp may belong to a live run"

    def test_sweep_ignores_artifacts(self, tmp_path):
        keep = _touch_artifact(tmp_path, "trinity-backup-20260101.db", age_days=400)
        assert bp.sweep_stale_tmps(str(tmp_path)) == []
        assert keep.exists()


# ---------------------------------------------------------------------------
# default_backup_dir
# ---------------------------------------------------------------------------

def test_default_backup_dir_is_beside_the_db():
    assert bp.default_backup_dir("/data/trinity.db") == "/data/backups"
