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


def test_a_writable_directory_files_no_alarm(tmp_path):
    db = MagicMock()
    with patch("database.db", db):
        storage = _storage(tmp_path / "archives")

    assert storage.archive_path.exists()
    db.create_operator_queue_item.assert_not_called()
    # And the probe cleans up after itself — an archive listing must not grow a
    # `.perm-probe-<pid>` file per boot.
    assert not list(storage.archive_path.glob(".perm-probe*"))


def test_an_unwritable_directory_alarms_with_the_cause(tmp_path):
    """The alarm has to name ownership, because "storage failed" is what the old
    log line already said and it is what nobody acted on."""
    archives = tmp_path / "archives"
    archives.mkdir()
    db = MagicMock()

    # Simulate the real condition (root-owned dir, non-root process) rather than
    # chmod 000, which pytest may run as a user that can bypass.
    with patch("database.db", db), \
         patch("pathlib.Path.touch", side_effect=PermissionError(13, "Permission denied")):
        _storage(archives)

    db.create_operator_queue_item.assert_called_once()
    agent, item = db.create_operator_queue_item.call_args.args
    assert agent == "_log-archive"
    assert item["id"].startswith("log-archive-")
    assert item["type"] == "alert" and item["priority"] == "high"
    haystack = (item["title"] + " " + item["question"]).lower()
    assert "not writable" in haystack or "cannot write" in haystack
    assert "1000" in item["question"], "the fix is an ownership change — say so"
    assert "archives-init" in item["question"], "name the remedy, not just the fault"
    assert item["context"]["path"] == str(archives)


def test_the_alarm_id_is_stable_within_a_day(tmp_path):
    """A crash-looping backend must not flood the queue while an operator is
    already looking at the alarm. `create_operator_queue_item` dedupes on the id
    (ON CONFLICT DO NOTHING), so the id has to repeat."""
    archives = tmp_path / "archives"
    archives.mkdir()
    ids = []
    for _ in range(3):
        db = MagicMock()
        with patch("database.db", db), \
             patch("pathlib.Path.touch", side_effect=PermissionError(13, "denied")):
            _storage(archives)
        ids.append(db.create_operator_queue_item.call_args.args[1]["id"])
    assert len(set(ids)) == 1, ids


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
    a maintenance problem into a boot failure."""
    archives = tmp_path / "archives"
    archives.mkdir()
    db = MagicMock()
    db.create_operator_queue_item.side_effect = RuntimeError("db down")
    with patch("database.db", db), \
         patch("pathlib.Path.touch", side_effect=PermissionError(13, "denied")):
        _storage(archives)                    # must not raise


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
