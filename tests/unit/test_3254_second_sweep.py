"""
#3254 — the second run of the #3246 upgrade sweep, on both tracks.

#3246's sweep (SQLite ``platform_alert_subjects`` / Alembic ``0090``) ran
once, on the upgrade that carried it. Every emitter #3254 moves onto the
seam kept filing subject-less rows until its move landed, and the seam
cannot see a row without a subject: ``observe`` files a second pending row
beside it, ``clear`` cannot end it, and the snooze cannot see a person's
ending of it. The data-only pair ``platform_alert_subjects_resweep``
(SQLite) / ``*_platform_alert_resweep`` (Alembic) runs the unchanged
``run_platform_alert_sweep`` again.

Driven against the real post-#3246 ``operator_queue`` DDL from ``schema.py``
(the subject columns and the partial unique index already present), this
file pins:

* registered on both tracks after the first sweep, and data only;
* idempotent: a second run changes nothing and plans nothing;
* per derivable subject, exactly one pending row survives: the subject's
  existing holder (the seam's live row, or a row the first sweep stamped)
  keeps the slot whatever its age (#3255's pinned rule); with no holder the
  newest subject-less row survives, stamped; every other pending row of the
  subject ends as ONE ``superseded`` batch by the platform;
* a must-act survivor keeps ``expires_at`` NULL: superseding a transition
  duplicate never ends the condition itself;
* gitignore and problem-report legacy rows are untouched (``_no_subject``),
  and agent-raised and gate rows are untouched;
* fresh install: no rows -> an empty plan and no writes;
* the Alembic revision's own ``upgrade()`` runs the same sweep (SQLite
  through an Alembic operations context; PostgreSQL under
  ``requires_postgres``).

The decision function is the leaf's ``plan_sweep``; its rules are pinned in
``test_3246_platform_alerts_leaf.py``. This file proves the second run
applies them to a database that already holds seam rows.
"""
import importlib.util
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

import pytest

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from db.migrations import (  # noqa: E402
    MIGRATIONS,
    _PLATFORM_ALERT_SWEEP_SELECT,
    _migrate_platform_alert_subjects_resweep,
    run_platform_alert_sweep,
)
from db.schema import INDEXES, TABLES  # noqa: E402
from services.platform_alerts import KINDS, derive_legacy_subject, plan_sweep  # noqa: E402

SQLITE_NAME = "platform_alert_subjects_resweep"
VERSIONS = Path(_BACKEND) / "migrations" / "versions"
UNIQUE_INDEX = "uq_operator_queue_pending_subject"

# Pinned dates (never `now`): the first sweep ran on the day 0090 shipped;
# the legacy rows below were filed by the off-seam emitters after it.
HOLDER_TS = "2026-10-06T12:00:00.000000Z"
TS1, TS2, TS3 = ("2026-10-08T00:00:00.000000Z", "2026-10-12T00:00:00.000000Z",
                 "2026-10-16T00:00:00.000000Z")
DAY1, DAY2, DAY3 = TS1[:10], TS2[:10], TS3[:10]

# One derivable moved kind per row: its host, the ids its OLD emitter minted
# for ONE condition (one id where the old id was already deterministic), and
# the context the legacy parser reads.
FAMILIES = (
    ("skills_reconcile_refused", "_skills-sync",
     ("skills-reconcile-worker-3", "skills-reconcile-worker-5", "skills-reconcile-worker-7"), None),
    ("skills_fleet_reinject", "_skills-sync",
     (f"skills-fleet-reinject-{TS1}", f"skills-fleet-reinject-{TS2}", f"skills-fleet-reinject-{TS3}"), None),
    ("sync_failing", "worker",
     (f"sync-failing-worker-{TS1}", f"sync-failing-worker-{TS2}", f"sync-failing-worker-{TS3}"), None),
    ("sync_diverged", "worker",
     (f"sync-diverged-worker-{TS1}", f"sync-diverged-worker-{TS2}", f"sync-diverged-worker-{TS3}"), None),
    ("db_backup_failure", "_db-backup",
     (f"db-backup-{TS1}", f"db-backup-{TS2}", f"db-backup-{TS3}"),
     {"alert_type": "db_backup_failure", "backup_dir": "/data/backups"}),
    ("db_backup_stale", "_db-backup",
     ("db-backup-2026-10-09T00:00:00.000000Z", "db-backup-2026-10-13T00:00:00.000000Z",
      "db-backup-2026-10-17T00:00:00.000000Z"),
     {"alert_type": "db_backup_stale", "backup_dir": "/data/backups"}),
    ("log_archive_unwritable", "_log-archive",
     (f"log-archive-data-archive-{DAY1}", f"log-archive-data-archive-{DAY2}",
      f"log-archive-data-archive-{DAY3}"), {"path": "/data/archive"}),
    ("retention_refused", "_retention-guard",
     ("retention-guard-execution_retention_days-30", "retention-guard-execution_retention_days-45",
      "retention-guard-execution_retention_days-60"), None),
    ("system_seed", "trinity-system", ("system-seed-cornelius-failed",), None),
    ("git_token_scrub_refused", "worker",
     (f"ent615-git-token-scrub-worker-{DAY1}", f"ent615-git-token-scrub-worker-{DAY2}",
      f"ent615-git-token-scrub-worker-{DAY3}"), None),
    ("git_token_scrub_unreadable", "worker",
     (f"ent615-git-token-scrub-unreadable-worker-{DAY1}",
      f"ent615-git-token-scrub-unreadable-worker-{DAY2}",
      f"ent615-git-token-scrub-unreadable-worker-{DAY3}"), None),
    ("poison", "worker", ("poison-exec-9",), None),
    ("validation_failed", "worker",
     ("val_exec-4_20261008000000", "val_exec-4_20261012000000", "val_exec-4_20261016000000"), None),
    ("portal_inbox_collision", "worker", ("portal-inbox-collision-acme",), None),
    ("alert_budget", "worker",
     ("alert-budget-worker-skill_not_found-b1", "alert-budget-worker-skill_not_found-b2",
      "alert-budget-worker-skill_not_found-b3"),
     {"reason": "platform_alert_budget", "alert_type": "skill_not_found", "cap": 5}),
    ("queue_flood", "worker",
     (f"queue-flood-worker-{TS1}", f"queue-flood-worker-{TS2}", f"queue-flood-worker-{TS3}"),
     {"held": 3, "reason": "ingestion_cap"}),
    ("effect_unguarded", "worker",
     (f"effect-unguarded-worker-{TS1}", f"effect-unguarded-worker-{TS2}",
      f"effect-unguarded-worker-{TS3}"), {"effect_type": "git_push", "reason": "no_execution"}),
    ("skill_not_found", "worker",
     (f"skill-not-found-worker-{TS1}", f"skill-not-found-worker-{TS2}", f"skill-not-found-worker-{TS3}"),
     {"command": "/deploy"}),
)
FAMILY_IDS = [f[0] for f in FAMILIES]
# The holder as the seam files it, for every family; as a legacy row the first
# sweep stamped, only where the old id could repeat (a deterministic old id —
# system seed, poison, inbox collision — can only be duplicated by the seam row).
HOLDER_CASES = [pytest.param(*f, "seam", id=f"{f[0]}-seam") for f in FAMILIES] + [
    pytest.param(*f, "stamped", id=f"{f[0]}-stamped") for f in FAMILIES if len(f[2]) > 1]
_CREATED = (TS1, TS2, TS3)
_ENDING = ("cancelled", "cancelled", "platform", None, "superseded")


def _subject_of(rid, context):
    match = derive_legacy_subject(rid, context)
    assert match is not None and match.subject is not None, rid
    return match.subject


@pytest.fixture
def conn():
    """An installed post-#3246 database: the full current ``operator_queue``
    DDL with every one of its indexes, the partial unique index included."""
    conn = sqlite3.connect(":memory:")
    conn.execute(TABLES["operator_queue"])
    for ddl in INDEXES:
        if " ON operator_queue(" in ddl:
            conn.execute(ddl)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert UNIQUE_INDEX in names
    yield conn
    conn.close()


def _add(conn, id_, agent, rid, created, *, context=None, subject=None, last_seen_at=None,
         status="pending", raised_by=None, expires_at=None, disposed_by=None, disposed_at=None,
         type_="alert"):
    conn.execute(
        "INSERT INTO operator_queue (id, agent_name, request_id, type, status, priority, title, "
        "question, created_at, context, subject, last_seen_at, raised_by, expires_at, "
        "disposed_by, disposed_at) VALUES (?, ?, ?, ?, ?, 'high', 't', 'q', ?, ?, ?, ?, ?, ?, ?, ?)",
        (id_, agent, rid, type_, status, created,
         json.dumps(context) if context is not None else None, subject, last_seen_at, raised_by,
         expires_at, disposed_by, disposed_at),
    )


def _run(conn):
    _migrate_platform_alert_subjects_resweep(conn.cursor(), conn)


def _rows(conn):
    cur = conn.execute("SELECT * FROM operator_queue")
    names = [d[0] for d in cur.description]
    return {r[0]: dict(zip(names, r)) for r in cur.fetchall()}


def _ending(row):
    return (row["status"], row["disposition"], row["disposed_by"], row["disposed_by_email"],
            row["disposition_reason"])


def _holder_row(conn, kind, agent, legacy, context, form):
    """The subject's existing holder, OLDER than every legacy row.

    ``seam``: the row ``observe`` files (``<prefix><belted key>-<ts>``).
    ``stamped``: a legacy id the first sweep stamped with its subject.
    """
    subject = _subject_of(legacy[0], context)
    if form == "seam":
        rid = f"{KINDS[kind].prefix}{subject.split(':', 1)[1]}-{HOLDER_TS}"
    else:
        rid = legacy[0]
    _add(conn, f"{kind}-holder", agent, rid, HOLDER_TS, context=context, subject=subject,
         last_seen_at=HOLDER_TS, expires_at=None if KINDS[kind].lifetime is None else
         "2026-11-05T12:00:00.000000Z")
    return subject


def _seed_legacy(conn, kind, agent, legacy, context, skip_first=False):
    ids = []
    for n, rid in enumerate(legacy):
        if skip_first and n == 0:
            continue
        id_ = f"{kind}-{n}"
        _add(conn, id_, agent, rid, _CREATED[n], context=context)
        ids.append(id_)
    return ids


def _plan_now(conn):
    """What a further run would plan, read through the sweep's own SELECT."""
    from datetime import datetime, timezone

    from services.platform_alerts import snooze_window
    now = datetime.now(timezone.utc)
    since = (now - snooze_window()).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    cur = conn.execute(_PLATFORM_ALERT_SWEEP_SELECT, {"since": since})
    names = [d[0] for d in cur.description]
    return plan_sweep([dict(zip(names, r)) for r in cur.fetchall()], now=now)


def _revision_files():
    found = {}
    for path in VERSIONS.glob("*.py"):
        text = path.read_text()
        rev = re.search(r'^revision\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
        down = re.search(r'^down_revision\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
        if rev:
            found[rev.group(1)] = (down.group(1) if down else None, path)
    return found


def _resweep_revision():
    files = list(VERSIONS.glob("*_platform_alert_resweep.py"))
    assert len(files) == 1, files
    spec = importlib.util.spec_from_file_location("resweep_revision_under_test", files[0])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

class TestRegistration:
    def test_is_registered_on_both_tracks(self):
        names = [name for name, _ in MIGRATIONS]
        assert names.count(SQLITE_NAME) == 1
        assert dict(MIGRATIONS)[SQLITE_NAME] is _migrate_platform_alert_subjects_resweep
        # after the first sweep and its heal, so a pre-#3246 install sweeps once,
        # then plans nothing on the second run in the same boot
        assert names.index(SQLITE_NAME) > names.index("platform_alert_subjects")
        assert names.index(SQLITE_NAME) > names.index("platform_alert_responded_heal")

        module = _resweep_revision()
        graph = _revision_files()
        assert module.revision in graph and module.down_revision == graph[module.revision][0]
        # a descendant of the first sweep's revision
        seen, rev = set(), module.down_revision
        while rev and rev not in seen:
            seen.add(rev)
            if rev == "0090_platform_alert_subjects":
                break
            rev = graph[rev][0]
        assert rev == "0090_platform_alert_subjects"

    def test_both_halves_are_data_only(self):
        ddl = re.compile(r"\b(CREATE|ALTER|DROP)\s+(UNIQUE\s+)?(TABLE|INDEX|COLUMN|TRIGGER)\b", re.I)
        import inspect
        assert not ddl.search(inspect.getsource(_migrate_platform_alert_subjects_resweep))
        module = _resweep_revision()
        assert not ddl.search(inspect.getsource(module.upgrade))


# ---------------------------------------------------------------------------
# The second run over a database that already holds seam rows
# ---------------------------------------------------------------------------

class TestOneRowPerSubject:
    @pytest.mark.parametrize("kind,agent,legacy,context,form", HOLDER_CASES)
    def test_the_holder_keeps_the_slot_whatever_its_age(self, conn, kind, agent, legacy, context, form):
        # Older than every legacy row: newest-by-creation would pick a legacy row.
        subject = _holder_row(conn, kind, agent, legacy, context, form)
        legacy_ids = _seed_legacy(conn, kind, agent, legacy, context, skip_first=(form == "stamped"))
        before = _rows(conn)[f"{kind}-holder"]
        _run(conn)
        rows = _rows(conn)
        assert rows[f"{kind}-holder"] == before, "the holder is never restamped"
        for id_ in legacy_ids:
            assert _ending(rows[id_]) == _ENDING, (id_, rows[id_])
            assert rows[id_]["subject"] is None and rows[id_]["batch_id"]
        pending = [r for r in rows.values() if r["status"] == "pending"]
        assert [r["id"] for r in pending] == [f"{kind}-holder"]
        assert pending[0]["subject"] == subject

    @pytest.mark.parametrize("kind,agent,legacy,context", FAMILIES, ids=FAMILY_IDS)
    def test_with_no_holder_the_newest_survives_stamped(self, conn, kind, agent, legacy, context):
        legacy_ids = _seed_legacy(conn, kind, agent, legacy, context)
        _run(conn)
        rows = _rows(conn)
        survivor = rows[legacy_ids[-1]]
        assert survivor["status"] == "pending"
        assert survivor["subject"] == _subject_of(legacy[-1], context)
        assert survivor["last_seen_at"] == _CREATED[len(legacy) - 1]
        assert json.loads(survivor["context"])["seen_count"] == 1
        if KINDS[kind].lifetime is None:
            assert survivor["expires_at"] is None, "a must-act kind never expires"
        else:
            assert survivor["expires_at"], "a lifetime kind leaves on its own"
        for id_ in legacy_ids[:-1]:
            assert _ending(rows[id_]) == _ENDING, (id_, rows[id_])
        assert sum(r["status"] == "pending" for r in rows.values()) == 1

    def test_every_family_at_once_ends_as_one_batch_and_one_row_per_subject_survives(self, conn):
        expected = {}
        for n, (kind, agent, legacy, context) in enumerate(FAMILIES):
            if n % 2:   # every other family already has a seam holder
                expected[(agent, _holder_row(conn, kind, agent, legacy, context, "seam"))] = f"{kind}-holder"
                _seed_legacy(conn, kind, agent, legacy, context)
            else:
                ids = _seed_legacy(conn, kind, agent, legacy, context)
                expected[(agent, _subject_of(legacy[-1], context))] = ids[-1]
        _run(conn)
        rows = _rows(conn)
        pending = {}
        for row in rows.values():
            if row["status"] == "pending":
                key = (row["agent_name"], row["subject"])
                assert key not in pending, f"two pending rows for {key}"
                pending[key] = row["id"]
        assert pending == expected
        ended = [r for r in rows.values() if r["status"] != "pending"]
        assert ended and {_ending(r) for r in ended} == {_ENDING}
        assert len({r["batch_id"] for r in ended}) == 1

    def test_a_must_act_transition_duplicate_is_superseded_and_the_condition_stays(self, conn):
        # AC 4 vs AC 6 (D19): the platform ends the duplicate, never the condition.
        for kind, agent, legacy, context in FAMILIES:
            if KINDS[kind].lifetime is not None:
                continue
            _holder_row(conn, kind, agent, legacy, context, "seam")
            _seed_legacy(conn, kind, agent, legacy, context)
        _run(conn)
        rows = _rows(conn)
        holders = [r for r in rows.values() if r["id"].endswith("-holder")]
        assert {r["id"] for r in holders} == {"poison-holder", "portal_inbox_collision-holder"}
        for row in holders:
            assert (row["status"], row["expires_at"]) == ("pending", None)
        assert {_ending(r) for r in rows.values() if not r["id"].endswith("-holder")} == {_ENDING}

    def test_a_legacy_row_a_person_ended_since_the_first_sweep_gets_its_subject(self, conn):
        # The seam's snooze reads person-ended rows by subject (#3255). Relative to the
        # real clock because the sweep's window is: inside it, and outside it.
        from datetime import datetime, timedelta, timezone
        recent = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        stale = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        _add(conn, "acked", "worker", f"sync-failing-worker-{TS1}", TS1, status="acknowledged",
             disposed_by="person", disposed_at=recent)
        _add(conn, "acked-old", "worker", f"sync-failing-worker-{TS2}", TS2, status="acknowledged",
             disposed_by="person", disposed_at=stale)
        _run(conn)
        rows = _rows(conn)
        assert rows["acked"]["subject"] == "sync_failing:worker"
        assert rows["acked-old"]["subject"] is None
        for id_ in ("acked", "acked-old"):
            assert rows[id_]["status"] == "acknowledged" and rows[id_]["disposed_by"] == "person"


class TestNeverTouched:
    def test_gitignore_and_problem_report_legacy_rows_are_untouched(self, conn):
        # Both parsers are `_no_subject` on purpose (D1, D14): the problem report's
        # legacy row may hold the only copy of the words, and neither is must-act
        # enough to merge on a guess. A seam row of each (as #3254 files them) sits
        # beside them and is untouched too.
        _add(conn, "gi-1", "worker", f"gitignore-untracked-worker-{TS1}", TS1, type_="gitignore_untracked")
        _add(conn, "gi-2", "worker", f"gitignore-untracked-worker-{TS2}", TS2, type_="gitignore_untracked")
        _add(conn, "gi-seam", "worker", f"gitignore-untracked-{'a' * 32}-{TS3}", TS3,
             subject=f"gitignore_untracked:{'a' * 32}", last_seen_at=TS3, type_="gitignore_untracked")
        _add(conn, "wp-1", "worker", f"workspace-problem-{'b' * 32}", TS1,
             context={"has_comment": True}, type_="workspace_problem_report")
        _add(conn, "wp-2", "worker", f"workspace-problem-{'c' * 32}", TS2,
             type_="workspace_problem_report")
        _add(conn, "wp-seam", "worker", f"workspace-problem-{'b' * 32}-{TS3}", TS3,
             subject=f"workspace_problem_report:{'b' * 32}", last_seen_at=TS3,
             type_="workspace_problem_report")
        before = _rows(conn)
        _run(conn)
        assert _rows(conn) == before

    @pytest.mark.parametrize("raised_by", ["agent", "gate"])
    def test_agent_raised_and_gate_rows_never_hold_the_slot_and_are_never_ended(self, conn, raised_by):
        _add(conn, "older", "worker", f"sync-failing-worker-{TS1}", TS1, raised_by=raised_by)
        _add(conn, "platform", "worker", f"sync-failing-worker-{TS2}", TS2)
        _add(conn, "newer", "worker", f"sync-failing-worker-{TS3}", TS3, raised_by=raised_by)
        _add(conn, "own-poison", "worker", "poison-exec-1", TS3, raised_by=raised_by)
        before = _rows(conn)
        _run(conn)
        rows = _rows(conn)
        assert rows["platform"]["status"] == "pending"
        assert rows["platform"]["subject"] == "sync_failing:worker"
        for id_ in ("older", "newer", "own-poison"):
            assert rows[id_] == before[id_], id_

    def test_git_bloat_legacy_rows_get_a_lifetime_stamp_only(self, conn):
        # R1's stated residual: `git-bloat-` carries two conditions told apart only
        # in prose, so the legacy row is never merged into the seam's row; it
        # leaves on the 30-day net instead.
        _add(conn, "gb-legacy", "worker", f"git-bloat-worker-{TS1}", TS1)
        _add(conn, "gb-seam", "worker", f"git-bloat-worker-{TS2}", TS2,
             subject="git_bloat_size:worker", last_seen_at=TS2, expires_at="2026-11-11T00:00:00.000000Z")
        before = _rows(conn)
        _run(conn)
        rows = _rows(conn)
        assert rows["gb-seam"] == before["gb-seam"]
        legacy = rows["gb-legacy"]
        assert (legacy["status"], legacy["subject"]) == ("pending", None)
        assert legacy["expires_at"]


class TestIdempotentAndFreshInstall:
    def test_a_second_run_changes_nothing_and_plans_nothing(self, conn):
        for n, (kind, agent, legacy, context) in enumerate(FAMILIES):
            if n % 2:
                _holder_row(conn, kind, agent, legacy, context, "seam")
            _seed_legacy(conn, kind, agent, legacy, context)
        _run(conn)
        after_first = _rows(conn)
        assert any(r["status"] == "cancelled" for r in after_first.values())
        _run(conn)
        assert _rows(conn) == after_first
        assert _plan_now(conn).is_empty()

    def test_fresh_install_no_rows_is_an_empty_plan_and_no_writes(self, conn):
        statements = []

        def recording(sql, params, fetch=False):
            statements.append(" ".join(sql.split()))
            cur = conn.execute(sql, params)
            return cur.fetchall() if fetch else None

        counts = run_platform_alert_sweep(recording)
        assert counts == {"survivors": 0, "ended": 0, "lifetime_only": 0, "snoozed": 0}
        assert len(statements) == 1 and statements[0].startswith("SELECT")
        changes = conn.total_changes
        _run(conn)
        assert conn.total_changes == changes
        assert conn.execute("SELECT COUNT(*) FROM operator_queue").fetchone()[0] == 0

    def test_fresh_install_lifecycle_records_the_migration(self):
        # init_database()'s order: migrations (no tables yet: the sweep's SELECT
        # meets "no such table", the fresh-install skip), init_schema, migrations.
        from db.migrations import run_all_migrations
        from db.schema import init_schema
        conn = sqlite3.connect(":memory:")
        cur = conn.cursor()
        try:
            run_all_migrations(cur, conn)
            init_schema(cur, conn)
            run_all_migrations(cur, conn)
            applied = {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}
            assert SQLITE_NAME in applied
            assert conn.execute("SELECT COUNT(*) FROM operator_queue").fetchone()[0] == 0
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# The Alembic revision's own data path
# ---------------------------------------------------------------------------

def _seed_mixed(execute):
    """Rows for the Alembic cases, through any `execute(sql, params)`."""
    rows = [
        # a seam holder OLDER than two subject-less duplicates (D19)
        ("sf-holder", "worker", f"sync-failing-worker-{HOLDER_TS}", HOLDER_TS, None,
         "sync_failing:worker", HOLDER_TS, None),
        ("sf-1", "worker", f"sync-failing-worker-{TS1}", TS1, None, None, None, None),
        ("sf-2", "worker", f"sync-failing-worker-{TS2}", TS2, None, None, None, None),
        # no holder: the newest of three survives
        ("qf-1", "worker", f"queue-flood-worker-{TS1}", TS1, json.dumps({"reason": "ingestion_cap"}),
         None, None, None),
        ("qf-2", "worker", f"queue-flood-worker-{TS2}", TS2, json.dumps({"reason": "ingestion_cap"}),
         None, None, None),
        ("qf-3", "worker", f"queue-flood-worker-{TS3}", TS3, json.dumps({"reason": "ingestion_cap"}),
         None, None, None),
        # a must-act transition duplicate
        ("po-holder", "worker", f"poison-exec-9-{HOLDER_TS}", HOLDER_TS, None, "poison:exec-9",
         HOLDER_TS, None),
        ("po-1", "worker", "poison-exec-9", TS1, None, None, None, None),
        # never touched
        ("gi-1", "worker", f"gitignore-untracked-worker-{TS1}", TS1, None, None, None, None),
        ("wp-1", "worker", f"workspace-problem-{'b' * 32}", TS1, None, None, None, None),
        ("ag-1", "worker", f"sync-failing-worker-{TS3}", TS3, None, None, None, "agent"),
    ]
    for id_, agent, rid, created, ctx, subject, last_seen, raised_by in rows:
        execute(
            "INSERT INTO operator_queue (id, agent_name, request_id, type, status, priority, title, "
            "question, created_at, context, subject, last_seen_at, raised_by) VALUES (:id, :agent, "
            ":rid, 'alert', 'pending', 'high', 't', 'q', :created, :ctx, :subject, :last_seen, :raised_by)",
            {"id": id_, "agent": agent, "rid": rid, "created": created, "ctx": ctx,
             "subject": subject, "last_seen": last_seen, "raised_by": raised_by},
        )


def _assert_mixed(rows):
    pending = {id_ for id_, r in rows.items() if r["status"] == "pending"}
    assert pending == {"sf-holder", "qf-3", "po-holder", "gi-1", "wp-1", "ag-1"}, rows
    ended = {id_: r for id_, r in rows.items() if r["status"] != "pending"}
    assert set(ended) == {"sf-1", "sf-2", "qf-1", "qf-2", "po-1"}
    assert {_ending(r) for r in ended.values()} == {_ENDING}
    assert len({r["batch_id"] for r in ended.values()}) == 1
    assert rows["qf-3"]["subject"] == "queue_flood:ingestion_cap" and rows["qf-3"]["expires_at"]
    assert (rows["po-holder"]["expires_at"], rows["po-holder"]["subject"]) == (None, "poison:exec-9")
    for id_ in ("gi-1", "wp-1", "ag-1"):
        assert (rows[id_]["subject"], rows[id_]["expires_at"]) == (None, None), id_


def test_the_alembic_revision_runs_the_sweep(tmp_path):
    """The revision's `upgrade()` itself, through an Alembic operations context
    on SQLite: what PostgreSQL runs, in the default tier."""
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy import create_engine, text

    engine = create_engine(f"sqlite:///{tmp_path / 'resweep.db'}")
    try:
        with engine.begin() as c:
            c.execute(text(TABLES["operator_queue"]))
            for ddl in INDEXES:
                if " ON operator_queue(" in ddl:
                    c.execute(text(ddl))
            _seed_mixed(lambda sql, params: c.execute(text(sql), params))
        module = _resweep_revision()
        with engine.begin() as c:
            with Operations.context(MigrationContext.configure(c)):
                module.upgrade()
        with engine.connect() as c:
            rows = {r._mapping["id"]: dict(r._mapping) for r in c.execute(text("SELECT * FROM operator_queue"))}
        _assert_mixed(rows)
    finally:
        engine.dispose()


@pytest.mark.requires_postgres
def test_the_alembic_revision_runs_the_sweep_on_postgres(monkeypatch):
    """The ONLY path an existing PostgreSQL install takes: bring a real database
    to the revision before this one (the first sweep and its unique index are
    in place), file legacy rows next to seam rows, upgrade through this
    revision, read the rows back; a further run plans nothing."""
    pg_url = os.environ.get("TEST_POSTGRES_URL")
    if not pg_url:
        pytest.skip("TEST_POSTGRES_URL not set")

    from alembic import command
    from sqlalchemy import text

    from db.alembic_runner import _config
    from db.engine import dispose_engines, get_engine
    from db_harness import _reset_postgres

    module = _resweep_revision()
    monkeypatch.setenv("DATABASE_URL", pg_url)
    dispose_engines()
    try:
        _reset_postgres()
        cfg = _config()
        command.upgrade(cfg, module.down_revision)
        with get_engine().begin() as c:
            _seed_mixed(lambda sql, params: c.execute(text(sql), params))

        command.upgrade(cfg, module.revision)

        with get_engine().connect() as c:
            version = c.execute(text("SELECT version_num FROM alembic_version")).scalar()
            rows = {r._mapping["id"]: dict(r._mapping) for r in c.execute(text("SELECT * FROM operator_queue"))}
        assert version == module.revision
        _assert_mixed(rows)

        with get_engine().begin() as c:
            def _pg_run(sql, params, fetch=False):
                result = c.execute(text(sql), params)
                return result.fetchall() if fetch else None
            assert run_platform_alert_sweep(_pg_run) == {
                "survivors": 0, "ended": 0, "lifetime_only": 0, "snoozed": 0}
    finally:
        dispose_engines()
