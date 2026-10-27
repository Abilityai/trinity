"""#3254: the database-backup and log-archive alarms report through the
platform alert seam (#3246), against the unit island's real SQLite.

Pinned here, for `db_backup_failure`, `db_backup_stale` and
`log_archive_unwritable`:

* Two readings of one condition are ONE pending row updated in place. The row
  carries the subject `<kind>:<key>`, and a second reading bumps
  `context.seen_count` and moves `last_seen_at` and `expires_at` (the
  seam-property assertion the old direct create fails).
* The place the code knows the condition cleared ends the row as the PLATFORM
  (`cancelled` / `platform` / `condition_cleared`): a successful backup ends
  both backup kinds, a fresh success the staleness check reads ends the stale
  row, and a successful write probe ends the archive row. An outcome that is
  not positive evidence of recovery ends nothing: a further failure, a
  no-space skip, a status reset with no success on record, a skipped probe.
* The backup's two conditions are two subjects on one host (AC 5).
* A person's ending through `ask_service.answer` lands `acknowledged`, is
  never overwritten by the platform's clear, and snoozes the next reading.
* Key parity: the key equals what the upgrade sweep derives from a pre-move
  row, and the seam's own row reads back as the same kind (the two backup
  kinds share the `db-backup-` prefix and are told apart by
  `context.alert_type`).
* The other shape, the clock 30 days ahead: a persisting staleness, re-alarmed
  weekly by the real `_staleness_check`, keeps exactly one pending row through
  day 30; the one-off failure reading has expired; a person's ending of the
  stale row silences one weekly re-alarm, not more.

The clock is frozen, at a pinned date in the past (see DAY0), for every module
the asserted path reads it from (the seam, the DB layer, the backup service,
`utils.helpers`), by patching each running function's own globals. No literal is
compared to the real clock.
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

# tests/unit/conftest.py puts src/backend on sys.path before collection.
from services import platform_alerts as pa

pytestmark = pytest.mark.unit

# A pinned Monday, 03:30 UTC: the nightly backup's default slot. Pinned in
# the past on purpose. The 30-day tests run the queue's real expiry sweep,
# which is fleet-wide over the unit tier's shared per-process DB. A sweep at a
# frozen instant before the real clock can only end rows whose deadline is
# already past in real time, which any other test's real-time sweep would end
# too, so it never ends another test's live row.
DAY0 = datetime(2026, 1, 5, 3, 30, tzinfo=timezone.utc)
_REAL_DATETIME = datetime


def _iso(dt):
    """The shape `utils.helpers.utc_now_iso` writes."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

class _Clock:
    """A frozen UTC clock the test moves by hand."""

    def __init__(self, at):
        self.at = at

    def set(self, at):
        self.at = at
        return at


@pytest.fixture
def clock(monkeypatch):
    """Freeze `datetime.now` in every module the asserted path reads it from.

    Patched in each running function's own `__globals__` (module identity),
    so a sibling file's `sys.modules` stub cannot detach the patch.
    `utils.helpers.utc_now_iso` reads `datetime` from its module, so freezing
    that module freezes every `utc_now_iso()` caller, whatever name it was
    imported under."""
    import services.db_backup_service as svc_mod
    from database import db

    c = _Clock(DAY0)

    class _Meta(type):
        def __instancecheck__(cls, obj):
            return isinstance(obj, _REAL_DATETIME)

    class _Frozen(_REAL_DATETIME, metaclass=_Meta):
        @classmethod
        def now(cls, tz=None):
            return c.at.astimezone(tz) if tz is not None else c.at.replace(tzinfo=None)

        @classmethod
        def utcnow(cls):
            return c.at.replace(tzinfo=None)

    ops = type(db._operator_queue_ops)
    module_globals = (
        pa._snoozed_by.__globals__,
        svc_mod.DBBackupService._staleness_check.__globals__,
        ops.create_platform_item.__globals__,
        ops.create_platform_item.__globals__["utc_now_iso"].__globals__,
        sys.modules["utils.helpers"].utc_now_iso.__globals__,
    )
    for g in {id(g): g for g in module_globals}.values():
        monkeypatch.setitem(g, "datetime", _Frozen)
    return c


def _purge(db):
    """Remove every row on the two sentinel hosts. The unit island's DB lives
    for the whole process, and these hosts are fixed names."""
    import services.db_backup_service as svc_mod
    from services import archive_storage

    g = type(db._operator_queue_ops).create_platform_item.__globals__
    table = g["operator_queue"]
    with g["get_engine"]().begin() as conn:
        conn.execute(g["delete"](table).where(table.c.agent_name.in_(
            (svc_mod.ALARM_AGENT_NAME, archive_storage.ALARM_AGENT_NAME))))


@pytest.fixture
def db(monkeypatch):
    from database import db
    from services import operator_resume_service
    monkeypatch.setattr(operator_resume_service, "spawn_on_loop", lambda factory: None)
    _purge(db)
    yield db
    _purge(db)


@pytest.fixture
def outcomes(monkeypatch):
    """Every `observe` outcome, as `(kind, outcome)`; the emitters import the
    seam module and call `platform_alerts.observe` on it."""
    seen = []
    real = pa.observe

    def spy(agent_name, kind, key=None, **kw):
        out = real(agent_name, kind, key, **kw)
        seen.append((kind, out))
        return out

    monkeypatch.setattr(pa, "observe", spy)
    return seen


@pytest.fixture
def backup(db, clock, monkeypatch, tmp_path):
    """The real backup service on the real queue, with its `db_backup_*`
    status keys held in a dict (they are process-wide settings rows) and its
    backup directory, which is the alarms' key, under tmp_path.

    `hooks[key]` runs once, right after the service writes that status key:
    the seam a test uses to land the sibling worker's outcome in the middle of
    this one's."""
    import services.db_backup_service as svc_mod

    settings, hooks = {}, {}
    real_get, real_set = db.get_setting_value, db.set_setting

    def _get(key, default=None):
        return settings.get(key, default) if key.startswith("db_backup_") else real_get(key, default)

    def _set(key, value):
        if key.startswith("db_backup_"):
            settings[key] = str(value)
            hook = hooks.pop(key, None)
            if hook is not None:
                hook()
        else:
            real_set(key, value)

    monkeypatch.setattr(db, "get_setting_value", _get)
    monkeypatch.setattr(db, "set_setting", _set)
    backup_dir = str(tmp_path / "backups")
    monkeypatch.setattr(svc_mod.DBBackupService, "backup_dir", staticmethod(lambda: backup_dir))
    return SimpleNamespace(svc=svc_mod.DBBackupService(), settings=settings, hooks=hooks,
                           dir=backup_dir, host=svc_mod.ALARM_AGENT_NAME)


@pytest.fixture
def archive(db, clock, monkeypatch, tmp_path):
    from services import archive_storage

    monkeypatch.setenv("LOG_ARCHIVE_ENABLED", "true")
    path = tmp_path / "archives"
    storage = archive_storage.LocalArchiveStorage(str(path))
    monkeypatch.setattr(archive_storage, "get_archive_storage", lambda: storage)
    return SimpleNamespace(mod=archive_storage, path=str(path), host=archive_storage.ALARM_AGENT_NAME)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _record(b, status, **kw):
    asyncio.run(b.svc._record_outcome(status, trigger="scheduled", **kw))


def _ok(b):
    _record(b, "ok", path=os.path.join(b.dir, "trinity-backup-x.db"), size_bytes=1, duration_ms=1)


def _check(b):
    asyncio.run(b.svc._staleness_check())


def _pending(db, host, kind=None):
    rows = db.list_operator_queue_items(agent_name=host, status="pending")
    return [r for r in rows if kind is None or (r.get("subject") or "").startswith(f"{kind}:")]


def _all_rows(db, host):
    return db.list_operator_queue_items(agent_name=host)


def _assert_platform_ended(db, row):
    ended = db.get_operator_queue_item(row["id"])
    assert (ended["status"], ended["disposed_by"], ended["disposition_reason"]) == (
        "cancelled", "platform", "condition_cleared"), ended


def _ack(row):
    from services import ask_service
    return ask_service.answer(row, response="acknowledged", response_text=None,
                              actor=ask_service.Actor(email="op@example.com")).rows[0]


def _probe(a, *, writable):
    if writable:
        return a.mod.probe_archive_writability()
    with patch("pathlib.Path.touch", side_effect=PermissionError(13, "Permission denied")):
        return a.mod.probe_archive_writability()


# ---------------------------------------------------------------------------
# db_backup_failure: the edge alarm
# ---------------------------------------------------------------------------

class TestBackupFailure:
    def test_two_readings_are_one_row_with_the_seam_properties(self, db, backup, clock):
        _record(backup, "failed", error="boom")
        rows = _pending(db, backup.host)
        assert len(rows) == 1
        first = rows[0]
        assert first["subject"] == pa.subject_for("db_backup_failure", backup.dir)
        assert first["request_id"].startswith("db-backup-")
        assert first["context"]["seen_count"] == 1
        assert first["expires_at"] is not None

        # The sibling worker read the prior status before this one wrote it
        # (`--workers 2`: the Redis lease is duplicate suppression only and
        # fails open), so it sees the same edge and reports it again.
        backup.settings["db_backup_last_status"] = "ok"
        clock.set(DAY0 + timedelta(hours=1))
        _record(backup, "skipped_no_space", error="disk full")
        rows = _pending(db, backup.host)
        assert [r["id"] for r in rows] == [first["id"]]
        row = rows[0]
        assert row["context"]["seen_count"] == 2
        assert row["last_seen_at"] > first["last_seen_at"]
        assert row["expires_at"] > first["expires_at"]
        assert row["title"] == "Database backup skipped: not enough disk space"

        clock.set(DAY0 + timedelta(days=1))
        _ok(backup)
        assert _pending(db, backup.host) == []
        _assert_platform_ended(db, row)

    def test_only_an_ok_ends_it(self, db, backup, clock):
        """A further failure or a no-space skip is not evidence of recovery,
        and continued failure does not re-read the edge alarm: the
        staleness re-alarm carries a persisting failure."""
        _record(backup, "failed", error="boom")
        clock.set(DAY0 + timedelta(days=1))
        _record(backup, "failed", error="boom again")
        clock.set(DAY0 + timedelta(days=2))
        _record(backup, "skipped_no_space", error="disk full")
        rows = _pending(db, backup.host, "db_backup_failure")
        assert len(rows) == 1 and rows[0]["context"]["seen_count"] == 1

    def test_a_persons_ending_stands_and_snoozes_the_next_edge(self, db, backup, clock, outcomes):
        _record(backup, "failed", error="boom")
        row = _pending(db, backup.host)[0]
        clock.set(DAY0 + timedelta(hours=2))
        acked = _ack(row)
        assert acked["status"] == "acknowledged"

        clock.set(DAY0 + timedelta(days=1))
        _ok(backup)                                   # the platform's clear finds nothing pending
        clock.set(DAY0 + timedelta(days=2))
        _record(backup, "failed", error="boom")       # a new edge inside the 7-day window

        assert _pending(db, backup.host) == []
        assert outcomes[-1] == ("db_backup_failure", pa.OBSERVED_SNOOZED)
        kept = db.get_operator_queue_item(row["id"])
        assert (kept["status"], kept["disposed_by"]) == ("acknowledged", "person")
        assert len(_all_rows(db, backup.host)) == 1

    def test_the_key_is_the_sweeps_reading_of_a_legacy_row(self, db, backup, clock):
        # The pre-move `_emit_alarm` id and context, byte for byte.
        legacy_id = f"db-backup-{_iso(DAY0 - timedelta(days=2))}"
        legacy_ctx = {"alert_type": "db_backup_failure", "status": "failed",
                      "trigger": "scheduled", "backup_dir": backup.dir}
        _record(backup, "failed", error="boom")
        row = _pending(db, backup.host)[0]
        assert pa.derive_legacy_subject(legacy_id, legacy_ctx).subject == row["subject"]
        assert pa.derive_legacy_subject(row["request_id"], row["context"]) == \
            pa.LegacyMatch("db_backup_failure", row["subject"])


class TestTwoWorkers:
    """With Redis down the lease fails open and both uvicorn workers run the
    nightly job. Each records its own outcome; the alarms must follow the
    status that stands once both have written, never the one each wrote."""

    @staticmethod
    def _sibling(status, **kw):
        import services.db_backup_service as svc_mod
        sibling = svc_mod.DBBackupService()
        return lambda: asyncio.run(sibling._record_outcome(status, trigger="scheduled", **kw))

    def test_a_failure_recorded_after_this_workers_success_keeps_its_alarm(self, db, backup):
        backup.settings["db_backup_last_status"] = "ok"
        # The sibling's failure lands after this worker wrote `ok` and before
        # it reached its clear: the sibling saw `ok` as the prior status, so it
        # raised the edge alarm.
        backup.hooks["db_backup_last_success_at"] = self._sibling("failed", error="ENOSPC on its tmp")
        _ok(backup)
        assert backup.settings["db_backup_last_status"] == "failed"
        assert len(_pending(db, backup.host, "db_backup_failure")) == 1

    def test_a_success_recorded_after_this_workers_failure_files_no_alarm(self, db, backup):
        backup.settings["db_backup_last_status"] = "ok"
        # The sibling's success lands after this worker wrote `failed` and
        # before it filed the edge alarm.
        backup.hooks["db_backup_last_trigger"] = self._sibling(
            "ok", path=os.path.join(backup.dir, "trinity-backup-x.db"), size_bytes=1, duration_ms=1)
        _record(backup, "failed", error="ENOSPC on its tmp")
        assert backup.settings["db_backup_last_status"] == "ok"
        assert _pending(db, backup.host) == []


# ---------------------------------------------------------------------------
# db_backup_stale: the weekly re-alarm
# ---------------------------------------------------------------------------

class TestBackupStale:
    @pytest.fixture
    def stale(self, backup):
        backup.settings["db_backup_last_status"] = "failed"
        backup.settings["db_backup_last_success_at"] = _iso(DAY0 - timedelta(days=4))
        return backup

    def test_two_readings_are_one_row_with_the_seam_properties(self, db, stale, clock):
        _check(stale)
        rows = _pending(db, stale.host)
        assert len(rows) == 1
        first = rows[0]
        assert first["subject"] == pa.subject_for("db_backup_stale", stale.dir)
        assert first["context"]["seen_count"] == 1

        clock.set(DAY0 + timedelta(days=7))          # the weekly re-alarm
        _check(stale)
        rows = _pending(db, stale.host)
        assert [r["id"] for r in rows] == [first["id"]]
        row = rows[0]
        assert row["context"]["seen_count"] == 2
        assert row["context"]["last_success_age_days"] == 11.0
        assert row["last_seen_at"] > first["last_seen_at"]
        assert row["expires_at"] > first["expires_at"]

        clock.set(DAY0 + timedelta(days=8))
        _ok(stale)
        assert _pending(db, stale.host) == []
        _assert_platform_ended(db, row)

    def test_the_two_conditions_are_two_subjects_and_ok_ends_both(self, db, backup, clock):
        """AC 5: one host, two conditions, two subjects."""
        backup.settings["db_backup_last_status"] = "ok"
        backup.settings["db_backup_last_success_at"] = _iso(DAY0 - timedelta(days=4))
        _record(backup, "failed", error="boom")
        clock.set(DAY0 + timedelta(minutes=5))       # the run's tail
        _check(backup)
        rows = _pending(db, backup.host)
        assert sorted(r["subject"] for r in rows) == sorted([
            pa.subject_for("db_backup_failure", backup.dir),
            pa.subject_for("db_backup_stale", backup.dir),
        ])

        clock.set(DAY0 + timedelta(days=1))
        _ok(backup)
        assert _pending(db, backup.host) == []
        for row in rows:
            _assert_platform_ended(db, row)

    def test_a_success_the_boot_hook_recorded_ends_the_stale_row(self, db, stale, clock):
        """The boot pre-migration backup writes `db_backup_last_success_at`
        with hand-rolled SQL before the db facade exists, so it can call no
        seam. The next staleness check reads that success and ends the row."""
        _check(stale)
        row = _pending(db, stale.host)[0]
        clock.set(DAY0 + timedelta(days=1))
        stale.settings["db_backup_last_status"] = "ok"
        stale.settings["db_backup_last_success_at"] = _iso(DAY0 + timedelta(hours=20))
        _check(stale)
        assert _pending(db, stale.host) == []
        _assert_platform_ended(db, row)

    def test_a_status_reset_with_no_success_on_record_ends_nothing(self, db, backup, clock):
        """An install that never succeeded alarms off its first attempt. If an
        operator resets the status keys, the first attempt is re-stamped
        young, but no backup has succeeded: not evidence of recovery."""
        backup.settings["db_backup_first_attempt_at"] = _iso(DAY0 - timedelta(days=4))
        _check(backup)
        assert len(_pending(db, backup.host, "db_backup_stale")) == 1
        clock.set(DAY0 + timedelta(days=1))
        backup.settings.clear()
        backup.settings["db_backup_first_attempt_at"] = _iso(DAY0 + timedelta(hours=1))
        _check(backup)
        assert len(_pending(db, backup.host, "db_backup_stale")) == 1

    def test_a_persons_ending_stands_and_snoozes_the_next_re_alarm(self, db, stale, clock, outcomes):
        _check(stale)
        row = _pending(db, stale.host)[0]
        clock.set(DAY0 + timedelta(hours=1))
        assert _ack(row)["status"] == "acknowledged"
        clock.set(DAY0 + timedelta(days=1))
        _ok(stale)
        clock.set(DAY0 + timedelta(days=5))
        stale.settings["db_backup_last_success_at"] = _iso(DAY0 - timedelta(days=4))
        stale.settings.pop("db_backup_last_staleness_alarm_at")
        _check(stale)
        assert _pending(db, stale.host) == []
        assert outcomes[-1] == ("db_backup_stale", pa.OBSERVED_SNOOZED)
        kept = db.get_operator_queue_item(row["id"])
        assert (kept["status"], kept["disposed_by"]) == ("acknowledged", "person")

    def test_the_key_is_the_sweeps_reading_of_a_legacy_row(self, db, stale, clock):
        legacy_id = f"db-backup-{_iso(DAY0 - timedelta(days=2))}"
        legacy_ctx = {"alert_type": "db_backup_stale", "last_success_age_days": 4.0,
                      "backup_dir": stale.dir}
        _check(stale)
        row = _pending(db, stale.host)[0]
        assert pa.derive_legacy_subject(legacy_id, legacy_ctx).subject == row["subject"]
        assert pa.derive_legacy_subject(row["request_id"], row["context"]) == \
            pa.LegacyMatch("db_backup_stale", row["subject"])


# ---------------------------------------------------------------------------
# The other shape: the clock 30 days ahead
# ---------------------------------------------------------------------------

class TestThirtyDaysAhead:
    """Each simulated day runs what `run_backup` runs on a failing night: the
    outcome at 03:30, the staleness check in the tail, and later that day the
    queue's expiry sweep (every 5 s in production)."""

    @pytest.fixture
    def failing(self, backup):
        backup.settings["db_backup_last_status"] = "ok"
        backup.settings["db_backup_last_success_at"] = _iso(DAY0 - timedelta(days=4))
        return backup

    @staticmethod
    def _day(b, db, clock, d):
        clock.set(DAY0 + timedelta(days=d))
        _record(b, "failed", error="boom")
        clock.set(DAY0 + timedelta(days=d, minutes=5))
        _check(b)
        clock.set(DAY0 + timedelta(days=d, hours=8))
        db.mark_operator_queue_expired()

    def test_a_persisting_staleness_keeps_one_row_and_a_one_off_failure_expires(
            self, db, failing, clock):
        failure_id = None
        stale_ids = set()
        for d in range(31):
            self._day(failing, db, clock, d)
            if d == 0:
                failure_id = _pending(db, failing.host, "db_backup_failure")[0]["id"]
            stale = _pending(db, failing.host, "db_backup_stale")
            assert len(stale) == 1, (d, stale)
            stale_ids.add(stale[0]["id"])
            failure = db.get_operator_queue_item(failure_id)
            assert failure["status"] == ("pending" if d < 14 else "expired"), (d, failure)

        assert len(stale_ids) == 1
        assert stale[0]["context"]["seen_count"] == 5          # days 0, 7, 14, 21, 28
        assert db.get_operator_queue_item(failure_id)["disposed_by"] == "timeout"
        assert len(_all_rows(db, failing.host)) == 2

    def test_a_persons_ending_of_the_stale_row_silences_one_weekly_re_alarm_not_more(
            self, db, failing, clock, outcomes):
        for d in range(8):
            self._day(failing, db, clock, d)
        row = _pending(db, failing.host, "db_backup_stale")[0]
        assert row["context"]["seen_count"] == 2               # days 0 and 7
        clock.set(DAY0 + timedelta(days=7, hours=9))
        assert _ack(row)["status"] == "acknowledged"

        for d in range(8, 21):
            self._day(failing, db, clock, d)
            assert _pending(db, failing.host, "db_backup_stale") == [], d
        self._day(failing, db, clock, 21)

        stale_outcomes = [o for k, o in outcomes if k == "db_backup_stale"]
        assert stale_outcomes == [pa.OBSERVED_CREATED, pa.OBSERVED_UPDATED,
                                  pa.OBSERVED_SNOOZED, pa.OBSERVED_CREATED]
        fresh = _pending(db, failing.host, "db_backup_stale")
        assert len(fresh) == 1 and fresh[0]["id"] != row["id"]
        assert fresh[0]["context"]["seen_count"] == 1
        kept = db.get_operator_queue_item(row["id"])
        assert (kept["status"], kept["disposed_by"]) == ("acknowledged", "person")


# ---------------------------------------------------------------------------
# log_archive_unwritable
# ---------------------------------------------------------------------------

class TestArchiveUnwritable:
    def test_two_readings_are_one_row_with_the_seam_properties(self, db, archive, clock):
        assert _probe(archive, writable=False) is False
        rows = _pending(db, archive.host)
        assert len(rows) == 1
        first = rows[0]
        assert first["subject"] == pa.subject_for("log_archive_unwritable", archive.path)
        assert first["request_id"].startswith("log-archive-")
        assert (first["type"], first["priority"]) == ("alert", "high")
        assert first["context"]["seen_count"] == 1

        clock.set(DAY0 + timedelta(days=1))          # the next nightly archival run
        assert _probe(archive, writable=False) is False
        rows = _pending(db, archive.host)
        assert [r["id"] for r in rows] == [first["id"]]
        row = rows[0]
        assert row["context"]["seen_count"] == 2
        assert row["last_seen_at"] > first["last_seen_at"]
        assert row["expires_at"] > first["expires_at"]

        clock.set(DAY0 + timedelta(days=2))
        assert _probe(archive, writable=True) is True
        assert _pending(db, archive.host) == []
        _assert_platform_ended(db, row)

    def test_a_skipped_probe_ends_nothing(self, db, archive, clock, monkeypatch):
        """Archival switched off, or a non-local backend, probes nothing: not
        evidence that the directory became writable."""
        _probe(archive, writable=False)
        clock.set(DAY0 + timedelta(days=1))
        monkeypatch.setenv("LOG_ARCHIVE_ENABLED", "false")
        assert _probe(archive, writable=True) is True
        monkeypatch.setenv("LOG_ARCHIVE_ENABLED", "true")
        monkeypatch.setattr(archive.mod, "get_archive_storage", lambda: object())
        assert _probe(archive, writable=True) is True
        assert len(_pending(db, archive.host)) == 1

    def test_a_persons_ending_stands_and_snoozes_the_next_reading(self, db, archive, clock, outcomes):
        _probe(archive, writable=False)
        row = _pending(db, archive.host)[0]
        clock.set(DAY0 + timedelta(hours=1))
        assert _ack(row)["status"] == "acknowledged"
        clock.set(DAY0 + timedelta(days=1))
        _probe(archive, writable=False)
        assert _pending(db, archive.host) == []
        assert outcomes[-1] == ("log_archive_unwritable", pa.OBSERVED_SNOOZED)
        clock.set(DAY0 + timedelta(days=2))
        _probe(archive, writable=True)
        kept = db.get_operator_queue_item(row["id"])
        assert (kept["status"], kept["disposed_by"]) == ("acknowledged", "person")

    def test_the_key_is_the_sweeps_reading_of_a_legacy_row(self, db, archive, clock):
        # The pre-move `_alarm_unwritable_archive_dir` id and context.
        legacy_id = (f"log-archive-{archive.path.strip('/').replace('/', '-')}-"
                     f"{_iso(DAY0 - timedelta(days=1))[:10]}")
        legacy_ctx = {"path": archive.path, "detail": "PermissionError: [Errno 13] Permission denied"}
        _probe(archive, writable=False)
        row = _pending(db, archive.host)[0]
        assert pa.derive_legacy_subject(legacy_id, legacy_ctx).subject == row["subject"]
        assert pa.derive_legacy_subject(row["request_id"], row["context"]) == \
            pa.LegacyMatch("log_archive_unwritable", row["subject"])


def test_the_module_prefix_constants_are_the_kinds_prefixes():
    """The two modules keep their `ALARM_ID_PREFIX` constants (the reserved-prefix
    tests read them); the seam mints ids from the kind registry, so they must agree."""
    import services.db_backup_service as svc_mod
    from services import archive_storage
    assert svc_mod.ALARM_ID_PREFIX == pa.KINDS["db_backup_failure"].prefix == \
        pa.KINDS["db_backup_stale"].prefix
    assert archive_storage.ALARM_ID_PREFIX == pa.KINDS["log_archive_unwritable"].prefix
