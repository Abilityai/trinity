"""#2216 — services/db_backup_service.py.

Covers the run orchestration: day-keyed idempotence, the fail-open lease
(duplicate-I/O suppression, never a correctness boundary), the free-space
preflight, prune-on-EVERY-attempt (the Catch-22 regression), durable status,
edge-triggered + staleness alarms, the inverted retention reader, and the PG
arm's conninfo/PGPASSWORD/timeout contracts (subprocess mocked).
"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
if _BACKEND_STR not in sys.path:
    sys.path.insert(0, _BACKEND_STR)

import db.connection as db_connection  # noqa: E402
import redis_breaker_util  # noqa: E402
import database  # noqa: E402
import services.db_backup_service as svc_mod  # noqa: E402
from db import backup_primitives as bp  # noqa: E402
from services.db_backup_service import (  # noqa: E402
    ALARM_AGENT_NAME,
    ALARM_ID_PREFIX,
    DBBackupService,
    _int_env,
    _pg_conninfo_and_password,
    effective_backup_retention_days,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeDB:
    """In-memory stand-in for the database facade's settings + queue writes."""

    def __init__(self):
        self.settings = {}
        self.queue_items = []

    def get_setting_value(self, key, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = str(value)

    def create_operator_queue_item(self, agent_name, item):
        self.queue_items.append((agent_name, item))
        return item["id"]


class FakeRedis:
    def __init__(self):
        self.store = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    def get(self, key):
        return self.store.get(key)

    def delete(self, key):
        self.store.pop(key, None)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Scratch DB + fake db facade + no Redis (fail-open default)."""
    db_path = tmp_path / "trinity.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.executemany("INSERT INTO t (v) VALUES (?)", [(f"r{i}",) for i in range(30)])
    conn.commit()
    conn.close()

    fake = FakeDB()
    monkeypatch.setattr(db_connection, "DB_PATH", str(db_path))
    monkeypatch.setattr(database, "db", fake)
    monkeypatch.setattr(redis_breaker_util, "get_breaker_redis", lambda: None)
    monkeypatch.setattr(svc_mod, "DB_BACKUP_ENABLED", True)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    return SimpleNamespace(
        db_path=db_path,
        backup_dir=tmp_path / "backups",
        fake=fake,
        svc=DBBackupService(),
    )


def _run(env, **kw):
    return asyncio.run(env.svc.run_backup(**kw))


def _plant(dirpath: Path, name: str, *, age_days: float = 0) -> Path:
    dirpath.mkdir(parents=True, exist_ok=True)
    p = dirpath / name
    p.write_bytes(b"x")
    if age_days:
        old = time.time() - age_days * 86400
        os.utime(p, (old, old))
    return p


# ---------------------------------------------------------------------------
# Happy path + day-keyed idempotence
# ---------------------------------------------------------------------------

class TestRunAndDayKey:

    def test_success_writes_artifact_and_status(self, env):
        res = _run(env)
        assert res["status"] == "ok"
        artifact = env.backup_dir / bp.sqlite_artifact_name(bp.utc_day_key())
        assert artifact.exists()
        bp.verify_sqlite_backup(str(artifact))
        s = env.fake.settings
        assert s["db_backup_last_status"] == "ok"
        assert s["db_backup_last_trigger"] == "scheduled"
        assert s["db_backup_last_path"] == str(artifact)
        assert int(s["db_backup_last_size_bytes"]) > 0
        assert "db_backup_last_duration_ms" in s
        assert "db_backup_last_success_at" in s
        assert env.fake.queue_items == []  # success never alarms

    def test_second_run_same_day_noops(self, env):
        assert _run(env)["status"] == "ok"
        res = _run(env)
        assert res["status"] == "skipped_exists"
        artifacts = list(env.backup_dir.glob("trinity-backup-*.db"))
        assert len(artifacts) == 1
        # The skip must not clobber the durable status of the real run.
        assert env.fake.settings["db_backup_last_status"] == "ok"

    def test_disabled_returns_without_touching_anything(self, env, monkeypatch):
        monkeypatch.setattr(svc_mod, "DB_BACKUP_ENABLED", False)
        assert _run(env) == {"status": "disabled"}
        assert not env.backup_dir.exists()
        env.svc.start()
        assert not env.svc.scheduler.running, (
            "DB_BACKUP_ENABLED=false must schedule no job"
        )


# ---------------------------------------------------------------------------
# Lease — duplicate-I/O suppression, never correctness
# ---------------------------------------------------------------------------

class TestLease:

    def test_lease_taken_and_released(self, env, monkeypatch):
        fake_redis = FakeRedis()
        monkeypatch.setattr(
            redis_breaker_util, "get_breaker_redis", lambda: fake_redis
        )
        assert _run(env)["status"] == "ok"
        assert "db_backup:running" not in fake_redis.store, (
            "the lease must be released in the tail"
        )

    def test_held_lease_skips(self, env, monkeypatch):
        fake_redis = FakeRedis()
        fake_redis.store["db_backup:running"] = "someone-else"
        monkeypatch.setattr(
            redis_breaker_util, "get_breaker_redis", lambda: fake_redis
        )
        assert _run(env)["status"] == "skipped_lease"
        assert not env.backup_dir.exists()
        # And the foreign token survives untouched.
        assert fake_redis.store["db_backup:running"] == "someone-else"

    def test_redis_down_fails_open(self, env, monkeypatch):
        monkeypatch.setattr(redis_breaker_util, "get_breaker_redis", lambda: None)
        assert _run(env)["status"] == "ok", (
            "Redis down must degrade to the day-key guard alone, never block "
            "the backup"
        )

    def test_release_is_own_token_compare_and_delete(self):
        fake_redis = FakeRedis()
        fake_redis.store["db_backup:running"] = "foreign-token"
        import services.db_backup_service as m
        orig = redis_breaker_util.get_breaker_redis
        redis_breaker_util.get_breaker_redis = lambda: fake_redis
        try:
            m.DBBackupService._release_lease("my-token")
            assert fake_redis.store["db_backup:running"] == "foreign-token", (
                "a foreign token must NEVER be deleted (own-token "
                "compare-and-delete, #1919 shape)"
            )
            m.DBBackupService._release_lease("foreign-token")
            assert "db_backup:running" not in fake_redis.store
        finally:
            redis_breaker_util.get_breaker_redis = orig

    def test_lease_ttl_is_derived_from_pg_dump_timeout(self):
        """learnings 2026-07-31: the TTL must exceed the slowest guarded op,
        and retuning the timeout must move the TTL with it (comment-linked)."""
        assert svc_mod._LEASE_TTL_SECONDS == svc_mod.PG_DUMP_TIMEOUT_SECONDS + 300


# ---------------------------------------------------------------------------
# Preflight + failure paths + prune-on-every-attempt
# ---------------------------------------------------------------------------

class TestPreflightAndFailure:

    def test_no_space_skips_loud_with_alarm(self, env, monkeypatch):
        monkeypatch.setattr(
            svc_mod, "shutil",
            SimpleNamespace(disk_usage=lambda p: SimpleNamespace(
                total=100, used=99, free=1)),
        )
        res = _run(env)
        assert res["status"] == "skipped_no_space"
        assert env.fake.settings["db_backup_last_status"] == "skipped_no_space"
        assert len(env.fake.queue_items) == 1
        agent, item = env.fake.queue_items[0]
        assert agent == ALARM_AGENT_NAME
        assert item["id"].startswith(ALARM_ID_PREFIX)
        assert item["context"]["alert_type"] == "db_backup_failure"
        # G-04 rule: context carries status/paths only — never row data.
        assert set(item["context"]) <= {
            "alert_type", "status", "trigger", "backup_dir"
        }

    def test_failed_backup_writes_status_and_alarm(self, env, monkeypatch):
        monkeypatch.setattr(
            DBBackupService, "_sqlite_backup_and_verify",
            staticmethod(lambda *a: (_ for _ in ()).throw(RuntimeError("boom"))),
        )
        res = _run(env)
        assert res["status"] == "failed"
        assert "boom" in env.fake.settings["db_backup_last_error"]
        assert len(env.fake.queue_items) == 1
        # No tmp litter, no artifact from the failed run.
        assert list(env.backup_dir.glob("*.tmp.*")) == []
        assert list(env.backup_dir.glob("trinity-backup-*")) == []

    def test_prune_runs_even_when_backup_fails(self, env, monkeypatch):
        """THE Catch-22 regression (D4): disk trouble must not stop pruning —
        an operator lowering the window must free space even while backups
        fail/skip."""
        for i in range(3):
            _plant(env.backup_dir, f"pre-migration-2026081{i}-000000.db")
        old = _plant(
            env.backup_dir, "trinity-backup-20260101.db", age_days=40
        )
        monkeypatch.setattr(
            DBBackupService, "_sqlite_backup_and_verify",
            staticmethod(lambda *a: (_ for _ in ()).throw(RuntimeError("boom"))),
        )
        res = _run(env)
        assert res["status"] == "failed"
        assert not old.exists(), (
            "the out-of-window artifact must be pruned even on a failed run"
        )

    def test_prune_runs_on_no_space_skip_too(self, env, monkeypatch):
        for i in range(3):
            _plant(env.backup_dir, f"pre-migration-2026081{i}-000000.db")
        old = _plant(env.backup_dir, "trinity-backup-20260101.db", age_days=40)
        monkeypatch.setattr(
            svc_mod, "shutil",
            SimpleNamespace(disk_usage=lambda p: SimpleNamespace(
                total=100, used=99, free=1)),
        )
        assert _run(env)["status"] == "skipped_no_space"
        assert not old.exists()

    def test_unavailable_estimate_proceeds(self, env, monkeypatch):
        async def _none(self, sqlite_backend):
            return 0
        monkeypatch.setattr(DBBackupService, "_estimate_source_bytes", _none)
        assert _run(env)["status"] == "ok", (
            "an unavailable size estimate must proceed — ENOSPC fails loudly "
            "on its own; a skipped backup on a transient would be silent loss"
        )


# ---------------------------------------------------------------------------
# Edge alarm — once per failure episode
# ---------------------------------------------------------------------------

class TestEdgeAlarm:

    def _fail(self, monkeypatch):
        monkeypatch.setattr(
            DBBackupService, "_sqlite_backup_and_verify",
            staticmethod(lambda *a: (_ for _ in ()).throw(RuntimeError("boom"))),
        )

    def test_fires_once_per_episode_rearmed_by_success(self, env, monkeypatch):
        self._fail(monkeypatch)
        assert _run(env)["status"] == "failed"
        assert len(env.fake.queue_items) == 1, "first failure → alarm"
        assert _run(env)["status"] == "failed"
        assert len(env.fake.queue_items) == 1, (
            "continued failure must NOT re-alarm (the staleness re-alarm owns "
            "duration; 288 identical alerts a day is how an alert gets muted)"
        )
        monkeypatch.undo()
        monkeypatch.setattr(svc_mod, "DB_BACKUP_ENABLED", True)
        monkeypatch.setattr(database, "db", env.fake)
        monkeypatch.setattr(db_connection, "DB_PATH", str(env.db_path))
        monkeypatch.setattr(redis_breaker_util, "get_breaker_redis", lambda: None)
        assert _run(env)["status"] == "ok"
        assert len(env.fake.queue_items) == 1, "success alarms nothing"
        # New episode: delete today's artifact so the day-key gate re-opens.
        for p in env.backup_dir.glob("trinity-backup-*"):
            p.unlink()
        self._fail(monkeypatch)
        assert _run(env)["status"] == "failed"
        assert len(env.fake.queue_items) == 2, (
            "an intervening success re-arms the edge"
        )


# ---------------------------------------------------------------------------
# Staleness re-alarm
# ---------------------------------------------------------------------------

class TestStaleness:

    def _age(self, days: float) -> str:
        from datetime import datetime, timedelta, timezone
        return (
            datetime.now(timezone.utc) - timedelta(days=days)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _stale_env(self, env, monkeypatch, *, success_age_days):
        env.fake.settings["db_backup_last_success_at"] = self._age(success_age_days)
        env.fake.settings["db_backup_last_status"] = "failed"  # mid-episode
        monkeypatch.setattr(
            DBBackupService, "_sqlite_backup_and_verify",
            staticmethod(lambda *a: (_ for _ in ()).throw(RuntimeError("boom"))),
        )

    def _staleness_alarms(self, env):
        return [
            item for _agent, item in env.fake.queue_items
            if item["context"].get("alert_type") == "db_backup_stale"
        ]

    def test_never_fires_while_fresh(self, env, monkeypatch):
        self._stale_env(env, monkeypatch, success_age_days=1)
        _run(env)
        assert self._staleness_alarms(env) == []

    def test_fires_past_threshold_then_respects_weekly_window(self, env, monkeypatch):
        self._stale_env(env, monkeypatch, success_age_days=4)
        _run(env)
        assert len(self._staleness_alarms(env)) == 1
        _run(env)
        assert len(self._staleness_alarms(env)) == 1, (
            "while stale, re-alarm at most once per 7 days"
        )
        env.fake.settings["db_backup_last_staleness_alarm_at"] = self._age(8)
        _run(env)
        assert len(self._staleness_alarms(env)) == 2, (
            "past the 7-day window the stale state must re-surface"
        )

    def test_never_succeeded_installs_still_go_stale(self, env, monkeypatch):
        """first_attempt_at is the fallback reference — a from-day-one failure
        must not be forever silent after its single edge alarm."""
        env.fake.settings["db_backup_first_attempt_at"] = self._age(4)
        env.fake.settings["db_backup_last_status"] = "failed"
        monkeypatch.setattr(
            DBBackupService, "_sqlite_backup_and_verify",
            staticmethod(lambda *a: (_ for _ in ()).throw(RuntimeError("boom"))),
        )
        _run(env)
        assert len(self._staleness_alarms(env)) == 1


# ---------------------------------------------------------------------------
# Env parsing + retention reader (inverted coercion)
# ---------------------------------------------------------------------------

class TestKnobs:

    def test_int_env_malformed_falls_back_with_warning(self, monkeypatch, caplog):
        monkeypatch.setenv("X_2216_TEST", "nonsense")
        with caplog.at_level(logging.WARNING, logger="services.db_backup_service"):
            assert _int_env("X_2216_TEST", 3, 0, 23) == 3
        assert any("not an integer" in r.message for r in caplog.records)

    def test_int_env_out_of_range_falls_back(self, monkeypatch, caplog):
        monkeypatch.setenv("X_2216_TEST", "99")
        with caplog.at_level(logging.WARNING, logger="services.db_backup_service"):
            assert _int_env("X_2216_TEST", 3, 0, 23) == 3
        assert any("out of range" in r.message for r in caplog.records)

    def test_int_env_unset_is_silent_default(self, monkeypatch, caplog):
        monkeypatch.delenv("X_2216_TEST", raising=False)
        with caplog.at_level(logging.WARNING, logger="services.db_backup_service"):
            assert _int_env("X_2216_TEST", 3, 0, 23) == 3
        assert caplog.records == []

    @pytest.mark.parametrize("stored,expected", [
        (None, 14),          # no row → default
        ("30", 30),          # valid → honored
        ("1", 1), ("3650", 3650),
        ("garbage", 14),     # unparseable → default, NEVER 0
        ("0", 14),           # out of bounds (0 = keep-forever trap) → default
        ("-3", 14),
        ("99999", 14),
    ])
    def test_retention_reader_inverted_coercion(self, monkeypatch, stored, expected):
        fake = FakeDB()
        if stored is not None:
            fake.settings["backup_retention_days"] = stored
        monkeypatch.setattr(database, "db", fake)
        assert effective_backup_retention_days() == expected

    def test_retention_reader_warns_on_garbage(self, monkeypatch, caplog):
        fake = FakeDB()
        fake.settings["backup_retention_days"] = "garbage"
        monkeypatch.setattr(database, "db", fake)
        with caplog.at_level(logging.WARNING, logger="services.db_backup_service"):
            effective_backup_retention_days()
        assert any("unparseable" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# PG arm — conninfo, PGPASSWORD, timeout kill+reap
# ---------------------------------------------------------------------------

class TestPgConninfo:

    def test_driver_normalized_password_stripped_query_preserved(self):
        conninfo, password = _pg_conninfo_and_password(
            "postgresql+psycopg2://trinity:s3cret@pg.example.com:5432/trinity"
            "?sslmode=require&sslrootcert=/etc/ssl/rds.pem"
        )
        assert conninfo.startswith("postgresql://"), "driver suffix must go"
        assert "psycopg2" not in conninfo
        assert "s3cret" not in conninfo, "password must never reach argv"
        # Query params are what managed PG mandates — dropping them makes
        # backups permanently fail on RDS/Cloud SQL. render_as_string
        # percent-encodes query VALUES (sslrootcert=%2Fetc%2F...), which libpq
        # percent-decodes on parse — assert the DECODED semantics, not bytes.
        from urllib.parse import urlsplit, parse_qs
        query = parse_qs(urlsplit(conninfo).query)
        assert query["sslmode"] == ["require"]
        assert query["sslrootcert"] == ["/etc/ssl/rds.pem"]
        assert "trinity@pg.example.com:5432/trinity" in conninfo
        assert password == "s3cret"

    def test_no_password_url(self):
        conninfo, password = _pg_conninfo_and_password(
            "postgresql://trinity@pg:5432/trinity"
        )
        assert password is None
        assert conninfo == "postgresql://trinity@pg:5432/trinity"

    def test_url_encoded_password_returned_raw(self):
        _conninfo, password = _pg_conninfo_and_password(
            "postgresql://u:p%40ss%3Aword@h/db"
        )
        assert password == "p@ss:word", (
            "PGPASSWORD needs the raw decoded password, not the URL-escaped form"
        )


class _FakeProc:
    def __init__(self, *, rc=0, stderr=b"", hang=False):
        self.returncode = rc
        self._stderr = stderr
        self._hang = hang
        self.killed = False
        self.waited = False

    async def communicate(self):
        if self._hang:
            await asyncio.sleep(3600)
        return b"", self._stderr

    def kill(self):
        self.killed = True

    async def wait(self):
        self.waited = True
        return self.returncode


class TestPgDump:

    def _capture_exec(self, monkeypatch, proc, tmp_path, *, write_magic=True):
        captured = {}

        async def fake_exec(*argv, stdout=None, stderr=None, env=None):
            captured["argv"] = list(argv)
            captured["env"] = env
            if write_magic:
                # pg_dump writes the -f target itself.
                Path(argv[argv.index("-f") + 1]).write_bytes(b"PGDMP" + b"\0" * 64)
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        return captured

    def test_password_env_only_never_argv(self, env, monkeypatch, tmp_path):
        proc = _FakeProc(rc=0)
        captured = self._capture_exec(monkeypatch, proc, tmp_path)
        tmp = tmp_path / "out.dump.tmp.1"
        stats = asyncio.run(
            env.svc._pg_dump_to(
                "postgresql://u@h:5432/db?sslmode=require", "sekret", str(tmp)
            )
        )
        assert stats["size_bytes"] > 0
        argv = captured["argv"]
        assert argv[0] == "pg_dump" and "-Fc" in argv
        assert argv[argv.index("-d") + 1] == "postgresql://u@h:5432/db?sslmode=require"
        assert all("sekret" not in a for a in argv), "argv is world-readable /proc"
        assert captured["env"]["PGPASSWORD"] == "sekret"

    def test_no_password_leaves_pgpassword_unset(self, env, monkeypatch, tmp_path):
        monkeypatch.delenv("PGPASSWORD", raising=False)
        proc = _FakeProc(rc=0)
        captured = self._capture_exec(monkeypatch, proc, tmp_path)
        asyncio.run(
            env.svc._pg_dump_to("postgresql://u@h/db", None, str(tmp_path / "o.tmp"))
        )
        assert "PGPASSWORD" not in captured["env"], (
            "a URL with no password means peer/trust auth — never invent one"
        )

    def test_nonzero_exit_raises_with_capped_stderr(self, env, monkeypatch, tmp_path):
        proc = _FakeProc(rc=1, stderr=b"connection refused\n" + b"x" * 5000)
        self._capture_exec(monkeypatch, proc, tmp_path)
        with pytest.raises(svc_mod.BackupRunError) as exc:
            asyncio.run(
                env.svc._pg_dump_to("postgresql://u@h/db", None,
                                    str(tmp_path / "o.tmp"))
            )
        assert "exited 1" in str(exc.value)
        assert len(str(exc.value)) < 2500, "stderr must be size-capped"

    def test_timeout_kills_AND_reaps(self, env, monkeypatch, tmp_path):
        """wait_for cancels the await, never the child — the explicit
        kill() → await wait() is the whole point (no zombie, no pinned PG
        connection)."""
        proc = _FakeProc(hang=True)
        self._capture_exec(monkeypatch, proc, tmp_path)
        monkeypatch.setattr(svc_mod, "PG_DUMP_TIMEOUT_SECONDS", 0.05)
        with pytest.raises(svc_mod.BackupRunError, match="timed out"):
            asyncio.run(
                env.svc._pg_dump_to("postgresql://u@h/db", None,
                                    str(tmp_path / "o.tmp"))
            )
        assert proc.killed, "the child must be explicitly killed"
        assert proc.waited, "the child must be reaped (await proc.wait())"

    def test_missing_binary_names_the_image_rebuild(self, env, monkeypatch, tmp_path):
        async def raise_fnf(*a, **k):
            raise FileNotFoundError("pg_dump")
        monkeypatch.setattr(asyncio, "create_subprocess_exec", raise_fnf)
        with pytest.raises(svc_mod.BackupRunError, match="[Rr]ebuild"):
            asyncio.run(
                env.svc._pg_dump_to("postgresql://u@h/db", None,
                                    str(tmp_path / "o.tmp"))
            )

    def test_pg_run_failure_reaches_status_and_alarm_and_unlinks_tmp(
        self, env, monkeypatch
    ):
        import db.engine as db_engine
        monkeypatch.setattr(db_engine, "is_sqlite", lambda url=None: False)
        monkeypatch.setattr(
            db_engine, "resolve_database_url",
            lambda: "postgresql://u:pw@h:5432/db",
        )

        async def zero(self, sqlite_backend):
            return 0
        monkeypatch.setattr(DBBackupService, "_estimate_source_bytes", zero)

        async def boom(self, conninfo, password, tmp):
            Path(tmp).write_bytes(b"partial")
            raise svc_mod.BackupRunError("pg_dump exited 1: nope")
        monkeypatch.setattr(DBBackupService, "_pg_dump_to", boom)

        res = _run(env)
        assert res["status"] == "failed"
        assert env.fake.settings["db_backup_last_status"] == "failed"
        assert len(env.fake.queue_items) == 1
        assert list(env.backup_dir.glob("*.tmp.*")) == [], (
            "the partial tmp must be unlinked — a leaked partial is invisible "
            "to the pattern-scoped prune forever"
        )
