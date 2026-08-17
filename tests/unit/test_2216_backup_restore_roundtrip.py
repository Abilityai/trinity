"""#2216 — AC#5: restore is EXERCISED, not merely documented.

Replays the incident end-to-end against a scratch DB:

1. seed rows → take a backup exactly the way the daily job does
   (copy → verify → atomic replace into the day-keyed artifact name);
2. overwrite the SOURCE header with the incident's literal
   ``HTTP/1.1 200 OK`` bytes — the source becomes unreadable with the exact
   production signature (``sqlite3.DatabaseError: file is not a database``);
3. restore per the documented procedure (remove stale ``-wal``/``-shm``/
   ``-journal`` sidecars, copy the artifact over the source);
4. prove row equality with the seed.

The instance in the incident had ~1.37M recoverable rows and nothing to
restore from. This test is the proof the produced artifact IS the recovery
point that was missing.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
if _BACKEND_STR not in sys.path:
    sys.path.insert(0, _BACKEND_STR)

from db import backup_primitives as bp  # noqa: E402

pytestmark = pytest.mark.unit

_INCIDENT_BYTES = (
    b"HTTP/1.1 200 OK\r\nserver: uvicorn\r\ncontent-length: 13\r\n\r\n"
)
_SEED_ROWS = 500


def test_incident_replay_backup_then_restore_roundtrip(tmp_path):
    src = tmp_path / "trinity.db"
    backups = tmp_path / "backups"

    # 1. Seed.
    conn = sqlite3.connect(str(src))
    conn.execute(
        "CREATE TABLE agents (id INTEGER PRIMARY KEY, name TEXT, cfg TEXT)"
    )
    conn.executemany(
        "INSERT INTO agents (name, cfg) VALUES (?, ?)",
        [(f"agent-{i}", f"cfg-{i}") for i in range(_SEED_ROWS)],
    )
    conn.commit()
    conn.close()

    # 2. Back up the way the daily job does: tmp → verify → atomic replace.
    bp.ensure_backup_dir(str(backups))
    artifact = backups / bp.sqlite_artifact_name(bp.utc_day_key())
    tmp = bp.tmp_path_for(str(artifact))
    bp.sqlite_backup_to(str(src), tmp)
    bp.verify_sqlite_backup(tmp)
    os.replace(tmp, str(artifact))
    assert artifact.exists()

    # 3. The incident: an HTTP response body lands over the SQLite header.
    with open(src, "r+b") as fh:
        fh.write(_INCIDENT_BYTES)

    # The source is now unreadable with the production signature.
    broken = sqlite3.connect(str(src))
    with pytest.raises(sqlite3.DatabaseError, match="file is not a database"):
        broken.execute("SELECT COUNT(*) FROM agents")
    broken.close()

    # 4. Restore per the documented procedure: stale sidecars removed first
    #    (plant them to exercise the step — a stale hot journal beside a
    #    restored .db is a corruption hazard), then copy the artifact in.
    for suffix in ("-wal", "-shm", "-journal"):
        (tmp_path / f"trinity.db{suffix}").write_bytes(b"stale sidecar")
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(src) + suffix)
        if sidecar.exists():
            sidecar.unlink()
    shutil.copyfile(str(artifact), str(src))

    # 5. Row equality with the seed.
    restored = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        assert (
            restored.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
            == _SEED_ROWS
        )
        # Spot-check content, not just cardinality.
        assert restored.execute(
            "SELECT cfg FROM agents WHERE name = 'agent-499'"
        ).fetchone()[0] == "cfg-499"
        assert (
            restored.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        )
    finally:
        restored.close()


def test_restore_from_pre_migration_artifact_too(tmp_path):
    """The boot-hook artifact restores identically — same primitive, same
    namespace, same procedure."""
    src = tmp_path / "trinity.db"
    conn = sqlite3.connect(str(src))
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(42)])
    conn.commit()
    conn.close()

    backups = tmp_path / "backups"
    bp.ensure_backup_dir(str(backups))
    artifact = backups / bp.pre_migration_artifact_name()
    tmp = bp.tmp_path_for(str(artifact))
    bp.sqlite_backup_to(str(src), tmp)
    bp.verify_sqlite_backup(tmp)
    os.replace(tmp, str(artifact))

    src.unlink()  # the fat-fingered-delete flavour of the incident class
    shutil.copyfile(str(artifact), str(src))
    restored = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        assert restored.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 42
    finally:
        restored.close()
