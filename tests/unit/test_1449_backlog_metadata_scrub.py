"""
Unit tests for the terminal ``backlog_metadata`` PII scrub (Issue #1449).

``backlog_service.enqueue`` json.dumps the full drain-replay request — including
``user_message``/``user_email``/``system_prompt`` — into
``schedule_executions.backlog_metadata`` so a queued task can be reconstructed at
drain. Nothing reads that blob once a row leaves ``status='queued'`` (drain claims
only queued rows; the #1083/#1081 result callbacks read the POST payload; canary
E-04/G-04 are queued-scoped), so on a terminal row it is stale PII. The scrub NULLs
it — but ONLY on the authoritative terminals (success/cancelled/skipped). A FAILED
row is resurrectable to SUCCESS via a late token-gated CAS, so its intent blob must
survive.

These assert the real column write through the db_harness (temp SQLite / full
schema) — never a mocked ``database``: a column with no live writer is a silent lie
(learning 2026-07-09), so we read back the actual NULL-out, not ``mock.assert_called``.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend, run as _hrun  # noqa: E402


@pytest.fixture
def db_setup(db_backend, monkeypatch):
    """Active backend with a fresh full schema (db_harness, #300).

    Returns (backend_marker, schedule_ops). The scrub method needs neither
    user_ops nor agent_ops, so pass None placeholders. Evicts any sibling-stubbed
    db.schedules so the import re-resolves fresh (auto-restored after the test).
    """
    monkeypatch.delitem(sys.modules, "db.schedules", raising=False)
    from db.schedules import ScheduleOperations

    schedule_ops = ScheduleOperations(None, None)
    yield db_backend, schedule_ops


def _iso(days_ago: float) -> str:
    return (
        datetime.now(timezone.utc) - timedelta(days=days_ago)
    ).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _rows(sql: str, **binds):
    from db.engine import get_engine
    from sqlalchemy import text

    with get_engine().connect() as conn:
        return conn.execute(text(sql), binds).fetchall()


# A realistic drain-replay blob — the exact shape `backlog_service.enqueue`
# persists (carries `user_message`/`user_email`/`system_prompt` PII).
_PII_BLOB = (
    '{"user_message": "my SSN is 123-45-6789", '
    '"user_email": "victim@example.com", '
    '"system_prompt": "you are helpful"}'
)


def _insert_execution(
    *,
    id_: str,
    status: str,
    backlog_metadata: str | None,
    completed_days_ago: float | None = 10,
) -> None:
    started = _iso((completed_days_ago or 0.1) + 0.1)
    completed = _iso(completed_days_ago) if completed_days_ago is not None else None
    _hrun(
        "INSERT INTO schedule_executions "
        "(id, schedule_id, agent_name, status, started_at, completed_at, "
        " queued_at, message, triggered_by, backlog_metadata) "
        "VALUES (:id, 'sched-x', 'agent-x', :st, :sa, :ca, :qa, 'msg', 'manual', :bm)",
        id=id_, st=status, sa=started, ca=completed, qa=started, bm=backlog_metadata,
    )


def _backlog(id_: str) -> str | None:
    return {r[0]: r[1] for r in _rows(
        "SELECT id, backlog_metadata FROM schedule_executions"
    )}[id_]


# ---------------------------------------------------------------------------
# Core: authoritative terminals are scrubbed
# ---------------------------------------------------------------------------


def test_scrub_nulls_authoritative_terminals(db_setup):
    _, schedule_ops = db_setup

    _insert_execution(id_="t-success", status="success", backlog_metadata=_PII_BLOB)
    _insert_execution(id_="t-cancelled", status="cancelled", backlog_metadata=_PII_BLOB)
    _insert_execution(id_="t-skipped", status="skipped", backlog_metadata=_PII_BLOB)

    scrubbed = schedule_ops.scrub_terminal_backlog_metadata(chunk_size=1000)
    assert scrubbed == 3

    assert _backlog("t-success") is None
    assert _backlog("t-cancelled") is None
    assert _backlog("t-skipped") is None


# ---------------------------------------------------------------------------
# The load-bearing exclusion: FAILED rows keep their intent (resurrection guard)
# ---------------------------------------------------------------------------


def test_failed_row_is_not_scrubbed(db_setup):
    """A FAILED row is resurrectable to SUCCESS via a late token-gated CAS
    (park_expired_lease keeps claim_token). Scrubbing its intent would silently
    lose the drain-replay request on resurrection — so FAILED is EXCLUDED."""
    _, schedule_ops = db_setup

    _insert_execution(id_="t-failed", status="failed", backlog_metadata=_PII_BLOB)
    _insert_execution(id_="t-success", status="success", backlog_metadata=_PII_BLOB)

    scrubbed = schedule_ops.scrub_terminal_backlog_metadata(chunk_size=1000)
    assert scrubbed == 1  # only the success row

    assert _backlog("t-failed") == _PII_BLOB   # intent survives
    assert _backlog("t-success") is None


# ---------------------------------------------------------------------------
# Non-terminal rows: the drain still needs their metadata
# ---------------------------------------------------------------------------


def test_queued_and_running_rows_untouched(db_setup):
    _, schedule_ops = db_setup

    _insert_execution(id_="e-queued", status="queued",
                      backlog_metadata=_PII_BLOB, completed_days_ago=None)
    _insert_execution(id_="e-running", status="running",
                      backlog_metadata=_PII_BLOB, completed_days_ago=None)
    _insert_execution(id_="e-pending", status="pending_retry",
                      backlog_metadata=_PII_BLOB, completed_days_ago=None)

    scrubbed = schedule_ops.scrub_terminal_backlog_metadata(chunk_size=1000)
    assert scrubbed == 0

    assert _backlog("e-queued") == _PII_BLOB
    assert _backlog("e-running") == _PII_BLOB
    assert _backlog("e-pending") == _PII_BLOB


# ---------------------------------------------------------------------------
# Chunking + idempotency
# ---------------------------------------------------------------------------


def test_chunk_size_drains_everything(db_setup):
    """A small chunk_size must still drain every in-scope row (multi-pass loop)."""
    _, schedule_ops = db_setup

    for i in range(7):
        _insert_execution(id_=f"t-{i}", status="success", backlog_metadata=_PII_BLOB)

    scrubbed = schedule_ops.scrub_terminal_backlog_metadata(chunk_size=2)
    assert scrubbed == 7
    assert all(
        r[0] is None
        for r in _rows("SELECT backlog_metadata FROM schedule_executions")
    )


def test_scrub_is_idempotent(db_setup):
    _, schedule_ops = db_setup

    _insert_execution(id_="t-success", status="success", backlog_metadata=_PII_BLOB)

    assert schedule_ops.scrub_terminal_backlog_metadata(chunk_size=1000) == 1
    # already-NULL row is not counted / re-written
    assert schedule_ops.scrub_terminal_backlog_metadata(chunk_size=1000) == 0
    assert _backlog("t-success") is None


def test_scrub_disabled_when_chunk_zero(db_setup):
    _, schedule_ops = db_setup
    _insert_execution(id_="t-success", status="success", backlog_metadata=_PII_BLOB)

    assert schedule_ops.scrub_terminal_backlog_metadata(chunk_size=0) == 0
    assert schedule_ops.scrub_terminal_backlog_metadata(chunk_size=-1) == 0
    assert _backlog("t-success") == _PII_BLOB


def test_scrub_empty_table(db_setup):
    _, schedule_ops = db_setup
    assert schedule_ops.scrub_terminal_backlog_metadata(chunk_size=1000) == 0


# ---------------------------------------------------------------------------
# Canary E-04/G-04 smoke: the scrub cannot touch the queued-scoped snapshot
# ---------------------------------------------------------------------------


def test_scrub_leaves_queued_scoped_snapshot_intact(db_setup):
    """Canary E-04/G-04 read `backlog_metadata` STRICTLY on `status IN
    ('running','queued')` rows (snapshot.py). A scrubbed row is terminal, so it
    falls outside that WHERE and the queued-scoped snapshot is unaffected — the
    scrub can never make E-04 (metadata NULL) or G-04 (creds in metadata) fire."""
    _, schedule_ops = db_setup

    _insert_execution(id_="q-live", status="queued",
                      backlog_metadata=_PII_BLOB, completed_days_ago=None)
    _insert_execution(id_="t-done", status="success", backlog_metadata=_PII_BLOB)

    schedule_ops.scrub_terminal_backlog_metadata(chunk_size=1000)

    # Mirror the canary snapshot's exact WHERE clause.
    queued_scope = _rows(
        "SELECT id, backlog_metadata FROM schedule_executions "
        "WHERE status IN ('running', 'queued')"
    )
    observed = {r[0]: r[1] for r in queued_scope}
    assert observed == {"q-live": _PII_BLOB}  # queued row intact, terminal absent
