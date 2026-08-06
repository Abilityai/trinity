"""#1970 — the scheduler must record WHO initiated an execution.

`schedule_executions` has carried five origin columns (`source_user_id`,
`source_user_email`, `source_agent_name`, `source_mcp_key_id`,
`source_mcp_key_name`) for audit since AUDIT-001, and the backend's own DB layer
populates them on every path it owns. The scheduler is a separate service with
its own DB module, and its `create_execution()` never listed them in the INSERT
— nor accepted them in its signature, so there was nowhere to put a caller even
if one had been forwarded. Every scheduler-created row was written with all five
NULL, on an instance where 100+ runs were observed that way.

`triggered_by='manual'` therefore recorded *that* a human ran something and
never *who*. The attribution existed only in backend/MCP-server logs, bounded by
log retention, so past a few weeks "did anyone trigger this, and who?" was
unanswerable from the durable record. Not a vulnerability — nothing authorizes
on these columns — an audit-completeness gap.

What is pinned here:
  * the INSERT actually writes all five (the literal defect);
  * a cron tick stays NULL — inventing an owner for a caller-less run would be
    a worse failure than the blank it replaces;
  * the manual/webhook trigger body is parsed at the scheduler boundary, and a
    hostile or malformed payload cannot write junk into an audit column;
  * a retry inherits the original run's origin, and a reminder inherits the
    provenance #1296 already stored — the two other non-cron paths, which would
    otherwise still land anonymous;
  * the backend actually sends a body (the endpoint posted none at all, which is
    the specific hop where the identity was dropped).

`src/scheduler` is a standalone package that cannot import the backend, so the
DB object is exercised directly against a temp SQLite file rather than mocked —
a mock would have happily accepted the pre-fix signature.
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]

# The `src.scheduler` namespace import resolves only with the repo root on
# sys.path — true for a repo-root `pytest` run but NOT in CI, whose rootdir is
# `tests/`. Appended (never inserted at 0) so the repo root cannot shadow the
# conftest-managed `src/backend` entries. Mirrors test_1808.
if str(_REPO) not in sys.path:
    sys.path.append(str(_REPO))

# `src/scheduler/config.py` reads these at import time (#589 made the Redis
# credentials mandatory), so they must exist before the package is imported.
os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")

ORIGIN_COLUMNS = (
    "source_user_id",
    "source_user_email",
    "source_agent_name",
    "source_mcp_key_id",
    "source_mcp_key_name",
)


def _scheduler_database_module():
    """Import the scheduler's `database` module as part of its package.

    Relative imports (`from .config import config`) rule out loading it
    standalone by path, and a bare `import database` would resolve to
    `src/backend/database.py`, which sits earlier on the pytest path.
    """
    import src.scheduler.database as scheduler_database

    return scheduler_database


def _scheduler_models_module():
    import src.scheduler.models as scheduler_models

    return scheduler_models


def _seed(db_path: Path) -> None:
    """Just enough `schedule_executions` to exercise the INSERT.

    Deliberately declares the five origin columns: the point of the test is
    that `create_execution` writes them, and a table without them would make a
    regressed INSERT pass by silently having nothing to fill.
    """
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE schedule_executions (
            id TEXT PRIMARY KEY,
            schedule_id TEXT NOT NULL,
            agent_name TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            duration_ms INTEGER,
            message TEXT NOT NULL,
            response TEXT,
            error TEXT,
            triggered_by TEXT NOT NULL,
            model_used TEXT,
            attempt_number INTEGER DEFAULT 1,
            retry_of_execution_id TEXT,
            source_user_id INTEGER,
            source_user_email TEXT,
            source_agent_name TEXT,
            source_mcp_key_id TEXT,
            source_mcp_key_name TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def _db(db_path: Path):
    return _scheduler_database_module().SchedulerDatabase(str(db_path))


def _row(db_path: Path, execution_id: str) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM schedule_executions WHERE id = ?", (execution_id,)
    ).fetchone()
    conn.close()
    assert row is not None, "create_execution did not persist a row"
    return dict(row)


# ---------------------------------------------------------------------------
# The defect itself: the INSERT must carry the origin columns.
# ---------------------------------------------------------------------------


def test_create_execution_persists_all_origin_columns(tmp_path):
    """The literal bug: five columns accepted, none written."""
    db_path = tmp_path / "t.db"
    _seed(db_path)

    execution = _db(db_path).create_execution(
        schedule_id="sch-1",
        agent_name="a1",
        message="go",
        triggered_by="manual",
        source_user_id=7,
        source_user_email="operator@example.com",
        source_agent_name="caller-agent",
        source_mcp_key_id="key-abc",
        source_mcp_key_name="ops laptop",
    )

    row = _row(db_path, execution.id)
    assert row["source_user_id"] == 7
    assert row["source_user_email"] == "operator@example.com"
    assert row["source_agent_name"] == "caller-agent"
    assert row["source_mcp_key_id"] == "key-abc"
    assert row["source_mcp_key_name"] == "ops laptop"


def test_returned_model_mirrors_the_persisted_origin(tmp_path):
    """The in-memory `ScheduleExecution` must agree with the row.

    Callers act on the returned object (broadcasts, follow-up writes); if it
    reported NULLs while the row held an identity, every downstream consumer
    would still see the bug and no DB assertion would catch it.
    """
    db_path = tmp_path / "t.db"
    _seed(db_path)

    execution = _db(db_path).create_execution(
        schedule_id="sch-1",
        agent_name="a1",
        message="go",
        triggered_by="manual",
        source_user_id=7,
        source_user_email="operator@example.com",
        source_agent_name="caller-agent",
        source_mcp_key_id="key-abc",
        source_mcp_key_name="ops laptop",
    )

    assert execution.source_user_id == 7
    assert execution.source_user_email == "operator@example.com"
    assert execution.source_agent_name == "caller-agent"
    assert execution.source_mcp_key_id == "key-abc"
    assert execution.source_mcp_key_name == "ops laptop"


def test_cron_tick_stays_null(tmp_path):
    """A cron fire has no caller. NULL is the correct, honest record.

    This is the half of the fix that must NOT change: attributing an autonomous
    tick to, say, the schedule's owner would make the column actively
    misleading — worse than the blank, because a blank is legible as "unknown"
    while a wrong name is not.
    """
    db_path = tmp_path / "t.db"
    _seed(db_path)

    execution = _db(db_path).create_execution(
        schedule_id="sch-1", agent_name="a1", message="go"
    )

    row = _row(db_path, execution.id)
    assert row["triggered_by"] == "schedule"
    for column in ORIGIN_COLUMNS:
        assert row[column] is None, f"{column} must stay NULL for a cron tick"


def test_origin_is_optional_so_existing_callers_keep_working(tmp_path):
    """Every parameter is keyword-optional — an un-migrated call site still
    inserts a valid row rather than raising."""
    db_path = tmp_path / "t.db"
    _seed(db_path)

    execution = _db(db_path).create_execution(
        schedule_id="sch-1",
        agent_name="a1",
        message="go",
        triggered_by="retry",
        model_used="claude-opus-5",
        attempt_number=2,
        retry_of_execution_id="exec-original",
    )

    row = _row(db_path, execution.id)
    assert row["attempt_number"] == 2
    assert row["retry_of_execution_id"] == "exec-original"


def test_partial_origin_is_accepted(tmp_path):
    """A webhook has no user; a UI click has no MCP key. Neither is an error —
    the columns are independent, not a five-part all-or-nothing record."""
    db_path = tmp_path / "t.db"
    _seed(db_path)

    execution = _db(db_path).create_execution(
        schedule_id="sch-1",
        agent_name="a1",
        message="go",
        triggered_by="manual",
        source_user_id=3,
    )

    row = _row(db_path, execution.id)
    assert row["source_user_id"] == 3
    assert row["source_user_email"] is None
    assert row["source_mcp_key_id"] is None


# ---------------------------------------------------------------------------
# Boundary parsing: the scheduler's trigger endpoint takes an untrusted body.
# ---------------------------------------------------------------------------


def test_origin_from_payload_reads_the_backend_field_names():
    """The wire contract between the two services. If either side renames a
    key, the identity silently becomes None again — the exact failure this
    issue is about, reintroduced without a single error."""
    origin = _scheduler_models_module().ExecutionOrigin.from_payload(
        {
            "triggered_by": "manual",
            "source_user_id": 42,
            "source_user_email": "a@example.com",
            "source_agent_name": "agent-x",
            "source_mcp_key_id": "k1",
            "source_mcp_key_name": "laptop",
        }
    )

    assert origin.user_id == 42
    assert origin.user_email == "a@example.com"
    assert origin.agent_name == "agent-x"
    assert origin.mcp_key_id == "k1"
    assert origin.mcp_key_name == "laptop"
    assert origin.is_empty() is False


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(None, id="no-body"),
        pytest.param("not-a-dict", id="string-body"),
        pytest.param([], id="list-body"),
        pytest.param({}, id="empty-object"),
        pytest.param({"triggered_by": "manual"}, id="legacy-body-without-origin"),
    ],
)
def test_malformed_payload_degrades_to_empty_origin(payload):
    """A bad body must cost attribution, never the run.

    The legacy case matters most: a backend that has not been redeployed still
    posts `{"triggered_by": "webhook"}`, and that must keep triggering
    normally with blank attribution rather than 500.
    """
    origin = _scheduler_models_module().ExecutionOrigin.from_payload(payload)
    assert origin.is_empty() is True


@pytest.mark.parametrize(
    "bad_user_id",
    [
        pytest.param("7", id="string"),
        pytest.param(7.5, id="float"),
        pytest.param(True, id="bool"),
        pytest.param({"id": 7}, id="object"),
        pytest.param(None, id="null"),
    ],
)
def test_non_integer_user_id_is_dropped_not_coerced(bad_user_id):
    """`source_user_id` is an INTEGER column joined against `users.id`.

    Coercion is the trap: `int("7")` succeeds and `bool` IS an `int` in Python,
    so `True` would silently persist as user 1 — a real account, attributed to
    a run it had nothing to do with. Dropping to NULL records "unknown", which
    is true.
    """
    origin = _scheduler_models_module().ExecutionOrigin.from_payload(
        {"source_user_id": bad_user_id}
    )
    assert origin.user_id is None


def test_oversized_string_is_capped():
    """An audit column is not a free-text sink for whatever a caller posts."""
    origin = _scheduler_models_module().ExecutionOrigin.from_payload(
        {"source_user_email": "x" * 5000}
    )
    assert origin.user_email is not None
    assert len(origin.user_email) <= 255


def test_blank_string_becomes_none_not_empty_string():
    """`""` and NULL must not be two different ways to say "unknown" — a query
    for unattributed rows would then have to know about both."""
    origin = _scheduler_models_module().ExecutionOrigin.from_payload(
        {"source_user_email": "   ", "source_mcp_key_name": ""}
    )
    assert origin.user_email is None
    assert origin.mcp_key_name is None


def test_wrong_typed_strings_are_dropped():
    """Non-string values in string fields drop rather than str()-ing into
    something like `{'a': 1}` in an audit column."""
    origin = _scheduler_models_module().ExecutionOrigin.from_payload(
        {"source_agent_name": 123, "source_mcp_key_id": ["k"]}
    )
    assert origin.agent_name is None
    assert origin.mcp_key_id is None


# ---------------------------------------------------------------------------
# The two remaining non-cron paths, which the DB fix alone would leave blank.
# ---------------------------------------------------------------------------


def test_reminder_row_exposes_its_provenance(tmp_path):
    """#1296 persists who set a reminder; the scheduler's `Reminder` model has
    to surface it or the fire path has nothing to attribute with."""
    db_path = tmp_path / "t.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE agent_reminders (
            id TEXT PRIMARY KEY, agent_name TEXT, message TEXT, fire_at TEXT,
            status TEXT, fire_attempts INTEGER, firing_at TEXT, model TEXT,
            timeout_seconds INTEGER, allowed_tools TEXT, execution_id TEXT,
            error TEXT, owner_id INTEGER, created_by_email TEXT,
            source_agent_name TEXT, source_mcp_key_id TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO agent_reminders VALUES "
        "('rem_1','a1','ping','2026-01-01T00:00:00Z','pending',0,NULL,NULL,"
        "NULL,NULL,NULL,NULL,11,'owner@example.com','a1','key-9')"
    )
    conn.commit()
    conn.close()

    reminder = _db(db_path).get_reminder_by_id("rem_1")
    assert reminder.owner_id == 11
    assert reminder.created_by_email == "owner@example.com"
    assert reminder.source_agent_name == "a1"
    assert reminder.source_mcp_key_id == "key-9"


def test_reminder_mapper_tolerates_a_table_without_provenance(tmp_path):
    """The mapper runs `SELECT *` against whatever the deploy has. A bare
    `row["owner_id"]` on an older table raises and takes the whole fire path
    down — trading a missing audit field for a dead reminder."""
    db_path = tmp_path / "t.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE agent_reminders (
            id TEXT PRIMARY KEY, agent_name TEXT, message TEXT, fire_at TEXT,
            status TEXT, fire_attempts INTEGER, firing_at TEXT, model TEXT,
            timeout_seconds INTEGER, allowed_tools TEXT, execution_id TEXT,
            error TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO agent_reminders VALUES "
        "('rem_1','a1','ping','2026-01-01T00:00:00Z','pending',0,NULL,NULL,"
        "NULL,NULL,NULL,NULL)"
    )
    conn.commit()
    conn.close()

    reminder = _db(db_path).get_reminder_by_id("rem_1")
    assert reminder.id == "rem_1"
    assert reminder.owner_id is None


def test_retry_reads_the_original_execution_for_its_origin():
    """A retry has no caller of its own; it must inherit the original's.

    Source-level guard: the retry path is deep inside an async APScheduler
    callback, so this asserts the read exists and feeds the INSERT rather than
    booting the scheduler. Without it a chain of retries makes attempt 1 the
    only attributable attempt.
    """
    source = (_REPO / "src" / "scheduler" / "service.py").read_text(encoding="utf-8")
    retry_block = source.split('triggered_by="retry"')[0][-2500:]
    assert "get_execution(original_execution_id)" in retry_block, (
        "the retry path no longer reads the original execution — its origin "
        "columns will be NULL again (#1970)"
    )


def test_every_create_execution_call_site_passes_an_origin():
    """A new call site that forgets the argument re-opens the gap silently:
    the parameters are optional by design (back-compat), so nothing fails —
    the row is simply anonymous again, which is the whole bug.
    """
    source = (_REPO / "src" / "scheduler" / "service.py").read_text(encoding="utf-8")
    calls = [
        block
        for block in source.split("self.db.create_execution(")[1:]
    ]
    assert calls, "no create_execution call sites found in the scheduler service"
    for index, block in enumerate(calls):
        args = block.split(")")[0]
        assert "source_user_id=" in args, (
            f"create_execution call site #{index + 1} passes no origin — a "
            "scheduler-created execution will be unattributable (#1970)"
        )


# ---------------------------------------------------------------------------
# The hop where the identity was actually dropped.
# ---------------------------------------------------------------------------


def _backend_trigger_endpoint() -> str:
    source = (_REPO / "src" / "backend" / "routers" / "schedules.py").read_text(
        encoding="utf-8"
    )
    marker = "async def trigger_schedule("
    assert marker in source, "the backend manual-trigger endpoint was renamed"
    return source[source.index(marker):][:4000]


def test_backend_forwards_the_caller_to_the_scheduler():
    """The root cause: the delegating POST carried no body at all, so the
    authenticated caller — in scope right there — never crossed the hop."""
    endpoint = _backend_trigger_endpoint()
    assert "current_user.id" in endpoint
    assert "current_user.email" in endpoint
    for field in ("source_user_id", "source_user_email", "source_agent_name"):
        assert field in endpoint, f"{field} is not forwarded to the scheduler"
    assert re.search(r"json\s*=", endpoint), (
        "the scheduler POST sends no JSON body — the identity is dropped at "
        "this hop again (#1970)"
    )


def test_backend_prefers_the_validated_agent_name_over_the_header():
    """`current_user.agent_name` comes from the validated agent-scoped key;
    `X-Source-Agent` is a raw client header anyone may set.

    Header-first (chat.py's order) would let an agent pin its run on a sibling
    agent — harmless for a collaboration hint, wrong for the audit column this
    issue exists to populate. Validated-first; the header only fills the
    user-scoped case, where `agent_name` is None and it is the sole signal.
    """
    endpoint = _backend_trigger_endpoint()
    assert "current_user.agent_name or x_source_agent" in endpoint
    assert "x_source_agent or current_user.agent_name" not in endpoint


def test_mcp_trigger_tool_sends_origin_headers():
    """Invariant #13 — the third surface. Without this an MCP-triggered run
    attributes to the key OWNER but not to which key or agent fired it, which
    is the part that identifies the actor when one human owns many keys."""
    tool = (
        _REPO / "src" / "mcp-server" / "src" / "tools" / "schedules.ts"
    ).read_text(encoding="utf-8")
    trigger = tool[tool.index("trigger_agent_schedule"):]
    assert "triggerAgentSchedule(" in trigger
    assert "keyId" in trigger and "authContext?.agentName" in trigger

    client = (_REPO / "src" / "mcp-server" / "src" / "client.ts").read_text(
        encoding="utf-8"
    )
    method = client[client.index("async triggerAgentSchedule("):][:1500]
    for header in ("X-Source-Agent", "X-MCP-Key-ID", "X-MCP-Key-Name"):
        assert header in method, f"{header} is not sent on the MCP trigger path"


def test_extra_headers_cannot_replace_the_bearer_token():
    """The generic `extraHeaders` hook added for attribution must not double as
    an auth-substitution surface."""
    client = (_REPO / "src" / "mcp-server" / "src" / "client.ts").read_text(
        encoding="utf-8"
    )
    assert 'key.toLowerCase() === "authorization"' in client
