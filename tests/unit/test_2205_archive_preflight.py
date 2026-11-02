"""An unwritable archive directory must be loud, not silent (#2205).

`/data/archives` was root-owned while the backend runs as UID 1000, so log archival
failed on every run for two months. What made it survive that long is where the
error went: into the very log files archival exists to prune. Symptom (8.5 GB of
logs) and diagnosis (`Permission denied`) sat in the same unbounded file nobody
tails.

`LocalArchiveStorage.__init__` also made the failure late and confusing. It calls
`mkdir(parents=True, exist_ok=True)`, which SUCCEEDS on a pre-existing root-owned
directory — so the class logged "initialized" and only the first write failed, with
a message about a file rather than about ownership.

So the constructor now probes writability and, when the probe fails, files one
operator-queue alarm naming the cause. These tests pin that it fires, that it names
the cause, and — the part that matters for a maintenance job — that it NEVER raises:
a broken archive directory must not take the backend down.
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


def _storage(path):
    from services.archive_storage import LocalArchiveStorage

    return LocalArchiveStorage(str(path))


def _seam_db():
    """#3254 re-pin (write-observation point): the alarm reports through
    `platform_alerts.observe` / `clear`, which use the seam's accessors instead
    of `create_operator_queue_item`. This fake holds no pending row and no
    person's ending, so every failing probe reaches the seam's create, where
    these tests now watch it. One row per subject and the platform's clear are
    asserted on the real DB in tests/unit/test_3254_backup_archive_alerts.py."""
    db = MagicMock()
    db.find_pending_operator_queue_by_subject.return_value = None
    db.find_person_ended_operator_queue_by_subject.return_value = None
    db.create_platform_operator_queue_item.return_value = {
        "outcome": "created", "row": None, "changed": False}
    return db


def test_a_writable_directory_files_no_alarm(tmp_path):
    db = _seam_db()
    with patch("database.db", db):
        storage = _storage(tmp_path / "archives")

    assert storage.archive_path.exists()
    db.create_platform_operator_queue_item.assert_not_called()   # #3254 re-pin: the seam's create
    # And the probe cleans up after itself — an archive listing must not grow a
    # `.perm-probe-<pid>` file per boot.
    assert not list(storage.archive_path.glob(".perm-probe*"))


def test_an_unwritable_directory_alarms_with_the_cause(tmp_path):
    """The alarm has to name ownership, because "storage failed" is what the old
    log line already said and it is what nobody acted on."""
    archives = tmp_path / "archives"
    archives.mkdir()
    db = _seam_db()

    # Simulate the real condition (root-owned dir, non-root process) rather than
    # chmod 000, which pytest may run as a user that can bypass.
    from services import archive_storage

    with patch("database.db", db), \
         patch("pathlib.Path.touch", side_effect=PermissionError(13, "Permission denied")):
        storage = _storage(archives)
        # The alarm belongs to the RUN, not to construction (see the module docstring
        # and `probe_archive_writability`).
        with patch.object(archive_storage, "get_archive_storage", return_value=storage):
            assert archive_storage.probe_archive_writability() is False

    # #3254 re-pin (write-observation point): the seam's create, same asserts.
    db.create_platform_operator_queue_item.assert_called_once()
    agent, item = db.create_platform_operator_queue_item.call_args.args
    assert agent == "_log-archive"
    assert item["id"].startswith("log-archive-")
    assert item["type"] == "alert" and item["priority"] == "high"
    haystack = (item["title"] + " " + item["question"]).lower()
    assert "not writable" in haystack or "cannot write" in haystack
    assert "1000" in item["question"], "the fix is an ownership change — say so"
    assert "archives-init" in item["question"], "name the remedy, not just the fault"
    assert item["context"]["path"] == str(archives)


def test_the_alarm_subject_is_stable_across_runs(tmp_path):
    """A crash-looping backend must not flood the queue while an operator is
    already looking at the alarm.

    #3254 re-pin (id shape; was `test_the_alarm_id_is_stable_within_a_day`):
    the old create deduped on a per-day id (ON CONFLICT DO NOTHING), so the id
    had to repeat. The seam dedupes on the SUBJECT: a pending row of the same
    subject is updated in place, so the subject has to repeat."""
    archives = tmp_path / "archives"
    archives.mkdir()
    from services import archive_storage

    subjects = []
    for _ in range(3):
        db = _seam_db()
        with patch("database.db", db), \
             patch("pathlib.Path.touch", side_effect=PermissionError(13, "denied")):
            storage = _storage(archives)
            with patch.object(archive_storage, "get_archive_storage", return_value=storage):
                archive_storage.probe_archive_writability()
        subjects.append(db.create_platform_operator_queue_item.call_args.kwargs["subject"])
    assert len(set(subjects)) == 1 and subjects[0], subjects


def test_a_failed_probe_never_raises(tmp_path):
    """Archival is a maintenance job. An unwritable directory is a reason to alarm,
    never a reason to fail the backend's startup."""
    archives = tmp_path / "archives"
    archives.mkdir()
    with patch("database.db", MagicMock()), \
         patch("pathlib.Path.touch", side_effect=OSError(28, "No space left on device")):
        storage = _storage(archives)          # must not raise
    assert storage.archive_path == archives


def test_alarm_plumbing_failure_does_not_break_startup(tmp_path):
    """The alarm itself is best-effort: a DB that cannot take the row must not turn
    a maintenance problem into a boot failure.

    #3254 re-pin (write-observation point): the fault moves to the seam's create,
    and the probe that raises the alarm is driven. Construction alone never
    reached the alarm (it is the run's job), so the old injection was never hit."""
    from services import archive_storage

    archives = tmp_path / "archives"
    archives.mkdir()
    db = _seam_db()
    db.create_platform_operator_queue_item.side_effect = RuntimeError("db down")
    with patch("database.db", db), \
         patch("pathlib.Path.touch", side_effect=PermissionError(13, "denied")):
        storage = _storage(archives)          # must not raise
        with patch.object(archive_storage, "get_archive_storage", return_value=storage):
            assert archive_storage.probe_archive_writability() is False   # must not raise
    db.create_platform_operator_queue_item.assert_called_once()


def test_the_id_prefix_is_reserved_so_an_agent_cannot_pre_silence_it():
    """#1632: `create_operator_queue_item` is ON CONFLICT DO NOTHING, so an agent
    that pre-creates this id would silence the platform's own alarm. The prefix must
    be in the reserved list, exactly as #2216 did for the backup alarm."""
    from services.operator_queue_service import _RESERVED_ID_PREFIXES
    from services.archive_storage import ALARM_ID_PREFIX

    assert ALARM_ID_PREFIX in _RESERVED_ID_PREFIXES


def test_the_alarm_host_is_uncreatable_and_canary_exempt():
    """The sentinel host must not look like a ghost agent to canary L-03, and must
    not be creatable as a real agent (the leading `_` is stripped by
    `sanitize_agent_name`, so the name can never collide with one)."""
    from canary.snapshot import _PLATFORM_ALARM_SENTINELS
    from services.archive_storage import ALARM_AGENT_NAME
    from utils.helpers import sanitize_agent_name

    assert ALARM_AGENT_NAME in _PLATFORM_ALARM_SENTINELS
    assert sanitize_agent_name(ALARM_AGENT_NAME) != ALARM_AGENT_NAME


# ---------------------------------------------------------------------------
# Review findings (second round) — each of these was a real hole in round one
# ---------------------------------------------------------------------------

def test_an_uncreatable_directory_neither_raises_nor_touches_the_db(tmp_path, monkeypatch):
    """`mkdir` runs at IMPORT time (module-level `log_archive_service` singleton, and
    `main.py` imports it), so an unwritable PARENT used to raise straight out of the
    import and crash-loop the backend with no API and no alarm.

    It now records the fault instead — and, per the CI finding, does NOT reach for the
    database during construction: the alarm path imported `database` at import time on
    any host without a writable `/data`, which is every CI runner, producing three
    cascading tracebacks per test process. The fault surfaces on the next run."""
    from services import archive_storage

    db = _seam_db()
    monkeypatch.setattr(
        archive_storage.Path, "mkdir",
        lambda self, **kw: (_ for _ in ()).throw(PermissionError(13, "denied")),
    )
    with patch("database.db", db):
        storage = archive_storage.LocalArchiveStorage(str(tmp_path / "nope" / "archives"))

    assert storage is not None                       # no raise
    assert storage.init_error                        # ...but it remembers
    assert db.mock_calls == []                       # and stays off the DB (#3254 re-pin: any accessor)

    # The run is what reports it.
    with patch("database.db", db), \
         patch.object(archive_storage, "get_archive_storage", return_value=storage):
        assert archive_storage.probe_archive_writability() is False
    db.create_platform_operator_queue_item.assert_called_once()   # #3254 re-pin: the seam's create


def test_no_alarm_when_archival_is_deliberately_disabled(tmp_path, monkeypatch):
    """Storage is constructed unconditionally while the flag is only read in
    `start()`, so an install with `LOG_ARCHIVE_ENABLED=false` was told every boot
    that "archival fails on every run" — about a job that never runs."""
    from services import archive_storage

    archives = tmp_path / "archives"
    archives.mkdir()
    monkeypatch.setenv("LOG_ARCHIVE_ENABLED", "false")
    db = _seam_db()
    with patch("database.db", db), \
         patch("pathlib.Path.touch", side_effect=PermissionError(13, "denied")):
        storage = _storage(archives)
        # #3254 re-pin (write-observation point): the alarm is raised by the RUN's
        # probe, so that is what is driven; construction alone never reached it.
        with patch.object(archive_storage, "get_archive_storage", return_value=storage):
            assert archive_storage.probe_archive_writability() is True
    assert db.mock_calls == []


def test_the_probe_can_be_re_armed_per_run(tmp_path, monkeypatch):
    """The constructor probes once per PROCESS. Under `restart: unless-stopped` a
    backend runs for weeks, so an acknowledged-but-unfixed fault would go unreported
    while every nightly run failed. `probe_archive_writability()` is the per-run
    re-check (the alarm id is per-day, so it re-alarms at most daily)."""
    from services import archive_storage

    archives = tmp_path / "archives"
    archives.mkdir()
    with patch("database.db", _seam_db()):
        storage = archive_storage.LocalArchiveStorage(str(archives))
    monkeypatch.setattr(archive_storage, "get_archive_storage", lambda: storage)

    with patch("database.db", _seam_db()):
        assert archive_storage.probe_archive_writability() is True

    db = _seam_db()
    with patch("database.db", db), \
         patch("pathlib.Path.touch", side_effect=PermissionError(13, "denied")):
        assert archive_storage.probe_archive_writability() is False
    db.create_platform_operator_queue_item.assert_called_once()   # #3254 re-pin: the seam's create


def test_staging_lands_beside_the_destination_not_on_the_tmpfs(tmp_path, monkeypatch):
    """Both compose files mount `/tmp` as a 100 MB tmpfs. Measured on a real
    instance, platform logs gzip ~42:1 — so the 4.6 GB file this issue was FILED
    against stages to ~110 MB and blows that ceiling with ENOSPC, which
    `archive_old_logs` swallows (`logger.error` + `continue`). Ownership alone would
    not have pruned the largest file. Staging beside the destination removes the
    ceiling and makes the final move a same-filesystem rename."""
    from services import archive_storage, log_archive_service as las

    archives = tmp_path / "archives"
    archives.mkdir()
    with patch("database.db", MagicMock()):
        storage = archive_storage.LocalArchiveStorage(str(archives))
    monkeypatch.setattr(archive_storage, "get_archive_storage", lambda: storage)

    staging = las._staging_dir()
    assert staging == archives / ".staging", staging
    # Not `"/tmp" not in str(staging)` — `tmp_path` is itself under /tmp, so that
    # assertion was nonsense. What matters is that staging is NOT the tmpfs fallback
    # and IS on the same filesystem as the destination (so the store is a rename).
    assert staging != las._TMP_STAGING_FALLBACK
    assert staging.parent == archives


def test_staging_falls_back_when_the_backend_is_not_local(monkeypatch):
    """A non-local storage backend has no local destination to stage beside, so the
    legacy tmp path is still the answer — the fallback must not disappear."""
    from services import archive_storage, log_archive_service as las

    monkeypatch.setattr(archive_storage, "get_archive_storage", lambda: object())
    assert las._staging_dir() == las._TMP_STAGING_FALLBACK


def test_construction_does_not_import_or_touch_the_database(tmp_path, monkeypatch):
    """CI finding, pinned: `log_archive_service` is a module-level singleton imported
    by `main.py`, so anything the constructor reaches for lands on the import path of
    every process that touches this module — including each CI test worker, on a host
    with no writable `/data`. Construction must therefore stay filesystem-light and
    completely DB-free; the alarm is the RUN's job."""
    from services import archive_storage

    sentinel = MagicMock(side_effect=AssertionError("construction reached the DB"))
    with patch("database.db", sentinel):
        # Both the healthy and the broken path.
        archive_storage.LocalArchiveStorage(str(tmp_path / "ok"))
        monkeypatch.setattr(
            archive_storage.Path, "mkdir",
            lambda self, **kw: (_ for _ in ()).throw(PermissionError(13, "denied")),
        )
        archive_storage.LocalArchiveStorage(str(tmp_path / "broken"))
    # #3254 re-pin (write-observation point): no accessor of any name, the seam's
    # included, was called.
    assert sentinel.mock_calls == []
