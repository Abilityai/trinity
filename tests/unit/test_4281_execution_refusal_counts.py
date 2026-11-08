"""trinity#4281 slice E: refusal counts on a schedule-backed execution.

A schedule whose creator is not owner-tier runs every tick as a shared person's
turn. The turn-tier hook refuses its shell calls, the agent forwards each
refusal (trinity-enterprise#165), and the run still ends `success`. This file
pins that such a run no longer reads as a clean `success`: the execution reads
carry `guardrail_refusals` and `guardrail_reporting`, computed when the
execution is READ (an event lands seconds after the hook fired, which can be
after the run ended).

Driven end to end: events go in through the REAL ingest service
(`guardrail_event_service.ingest`, the code behind the agent's POST), the
reporter liveness lives in fakeredis, and the execution rows are read back
through the REAL schedules router over a real database (`db_harness`; SQLite
always, PostgreSQL when `TEST_POSTGRES_URL` is set). Only the caller principal
is injected.
"""

from __future__ import annotations

import secrets
import sys
from datetime import timedelta
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend  # noqa: E402,F401

pytest.importorskip("fastapi", reason="backend venv required")
fakeredis = pytest.importorskip("fakeredis")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

import routers.schedules as schedules_router  # noqa: E402
from database import db  # noqa: E402
from db.engine import get_engine  # noqa: E402
from dependencies import get_current_user  # noqa: E402
from models import User  # noqa: E402
from services import guardrail_event_service as svc  # noqa: E402
from utils.helpers import to_utc_iso, utc_now  # noqa: E402

AGENT = "brief-agent"
OTHER_AGENT = "brief-other"
OWNER = "brief-owner"
ADMIN = "brief-admin"


@pytest.fixture
def world(db_backend, monkeypatch):
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", secrets.token_hex(32))
    from db_models import ScheduleCreate, UserCreate

    db.create_user(UserCreate(username=OWNER, password="x", role="creator", email="o@example.com"))
    db.create_user(UserCreate(username=ADMIN, password="x", role="admin", email="a@example.com"))
    db.register_agent_owner(AGENT, OWNER)
    db.register_agent_owner(OTHER_AGENT, OWNER)
    schedule = db.create_schedule(
        AGENT, OWNER, ScheduleCreate(name="morning brief", cron_expression="0 9 * * *", message="brief"))
    assert schedule is not None
    redis = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(svc, "_get_redis", lambda: redis)
    admin = db.get_user_by_username(ADMIN)
    principal = User(id=admin["id"], username=ADMIN, role="admin")
    app = FastAPI()
    app.include_router(schedules_router.router)
    app.dependency_overrides[get_current_user] = lambda: principal
    return {"client": TestClient(app), "schedule_id": schedule.id, "redis": redis}


def _iso_ago(seconds: float) -> str:
    return to_utc_iso(utc_now() - timedelta(seconds=seconds))


def _run(world, *, ended_seconds_ago=60.0, status="success", schedule_id=None, agent=AGENT):
    """A schedule run that ended `ended_seconds_ago` (None: still running)."""
    sid = world["schedule_id"] if schedule_id is None else schedule_id
    if sid == "__manual__":
        execution = db.create_task_execution(agent, "hello", triggered_by="manual")
    else:
        execution = db.create_schedule_execution(sid, agent, "brief", triggered_by="schedule")
    if ended_seconds_ago is not None:
        with get_engine().begin() as conn:
            conn.execute(
                text("UPDATE schedule_executions SET status = :s, completed_at = :c WHERE id = :i"),
                {"s": status, "c": _iso_ago(ended_seconds_ago), "i": execution.id})
    return execution.id


def _keepalive(runtime="claude-code", agent=AGENT):
    svc.ingest(agent, [], runtime=runtime, dropped=0)


_seq = iter(range(1, 10_000))


def _event(execution_id, *, hook="turn_tier_guard", code="shell_owner_only", kind="deny"):
    return {"event_id": f"ev-{next(_seq)}", "ts": _iso_ago(5), "kind": kind, "hook": hook,
            "tool": "Bash", "code": code, "execution_id": execution_id}


def _ingest(events, agent=AGENT, runtime="claude-code"):
    result = svc.ingest(agent, events, runtime=runtime, dropped=0)
    assert result["accepted"] == len(events), result


def _list(world):
    r = world["client"].get(f"/api/agents/{AGENT}/executions")
    assert r.status_code == 200, r.text
    return {row["id"]: row for row in r.json()}


def _detail(world, execution_id):
    r = world["client"].get(f"/api/agents/{AGENT}/executions/{execution_id}")
    assert r.status_code == 200, r.text
    return r.json()


def _history(world):
    r = world["client"].get(f"/api/agents/{AGENT}/schedules/{world['schedule_id']}/executions")
    assert r.status_code == 200, r.text
    return {row["id"]: row for row in r.json()}


def _reading(row):
    return row["guardrail_refusals"], row["guardrail_reporting"]


# ---------------------------------------------------------------------------
# The count is evaluated when the execution is read
# ---------------------------------------------------------------------------

def test_an_event_that_lands_after_the_run_ended_is_counted_on_the_next_read(world):
    run = _run(world)
    _keepalive()
    assert _reading(_list(world)[run]) == (0, None)

    _ingest([_event(run), _event(run, code="mcp_owner_only")])

    assert _reading(_list(world)[run]) == (2, None)
    assert _reading(_detail(world, run)) == (2, None)
    assert _reading(_history(world)[run]) == (2, None)


def test_a_run_absent_from_the_counts_while_the_agent_reports_is_zero(world):
    refused, clean = _run(world), _run(world)
    _ingest([_event(refused)])

    rows = _list(world)
    assert _reading(rows[refused]) == (1, None)
    assert _reading(rows[clean]) == (0, None)


# ---------------------------------------------------------------------------
# Only the turn-tier hook's refusals count
# ---------------------------------------------------------------------------

def test_a_credential_redaction_alone_never_makes_a_run_unclean(world):
    run = _run(world)
    _ingest([_event(run, hook="output_scanner", code="credential_redacted", kind="credential_redacted")])

    assert _reading(_list(world)[run]) == (0, None)


def test_another_hooks_deny_is_not_a_turn_tier_refusal(world):
    run = _run(world)
    _ingest([_event(run, hook="bash_guardrail", code="bash_deny_pattern"),
             _event(run, hook="turn_tier_guard", code="schedule_creator_tier")])

    assert _reading(_list(world)[run]) == (1, None)


def test_an_event_another_agent_files_against_this_run_is_ignored(world):
    """`execution_id` is attribution, not authority (ent#165 §6.1): a sibling
    agent naming this run's id never makes it unclean."""
    run = _run(world)
    _keepalive()
    _ingest([_event(run)], agent=OTHER_AGENT)

    assert _reading(_list(world)[run]) == (0, None)


# ---------------------------------------------------------------------------
# The reporter states
# ---------------------------------------------------------------------------

def test_an_agent_that_never_reported_reads_unknown_not_zero(world):
    run = _run(world)

    assert _reading(_list(world)[run]) == (None, "never")
    assert _reading(_detail(world, run)) == (None, "never")


def test_a_stale_reporter_gives_the_count_marked_stale(world):
    run = _run(world, ended_seconds_ago=3600)
    _ingest([_event(run)])
    stale_at = _iso_ago(svc.STALE_AFTER_SECONDS + 60)
    assert svc.record_report(AGENT, "claude-code", 0, stale_at)
    assert svc.reporting_state(AGENT)["state"] == "stale"

    assert _reading(_list(world)[run]) == (1, "stale")


@pytest.mark.parametrize("runtime", ["gemini", "codex"])
def test_a_gemini_or_codex_agent_reporting_keepalives_reads_unknown_not_zero(world, runtime):
    """Review X12: those runtimes have no hooks, so their forwarder sends only
    keepalives and the agent reads `reporting`. No events is no evidence."""
    run = _run(world)
    _keepalive(runtime=runtime)
    assert svc.reporting_state(AGENT)["state"] == "reporting"

    assert _reading(_list(world)[run]) == (None, "not_claude_code")


def test_a_reporter_with_no_runtime_on_record_reads_unknown_not_zero(world):
    """Redis lost the reporter hash: the state falls back to the newest stored
    row, and the runtime is then not known."""
    old, run = _run(world), _run(world)
    _ingest([_event(old)])
    world["redis"].flushall()
    reporting = svc.reporting_state(AGENT)
    assert (reporting["state"], reporting["runtime"]) == ("reporting", None)

    rows = _list(world)
    assert _reading(rows[run]) == (None, "not_claude_code")
    assert _reading(rows[old]) == (1, "not_claude_code")


def test_a_read_inside_the_delivery_bound_is_pending_not_a_bare_zero(world):
    """Review R11: events land up to ~2 s (p99 <= 5 s) after the hook fired."""
    _keepalive()
    just_ended = _run(world, ended_seconds_ago=2)
    running = _run(world, ended_seconds_ago=None)
    _ingest([_event(running)])

    rows = _list(world)
    assert _reading(rows[just_ended]) == (0, "pending")
    assert _reading(rows[running]) == (1, "pending")


def test_the_real_terminal_write_reads_pending_at_once(world):
    _keepalive()
    run = _run(world, ended_seconds_ago=None)
    assert db.update_execution_status(run, "success")

    assert _reading(_detail(world, run)) == (0, "pending")


def test_a_count_that_cannot_be_read_says_so_and_the_read_still_answers(world, monkeypatch):
    run = _run(world)
    _keepalive()

    def _boom(*a, **kw):
        raise RuntimeError("guardrail store unreadable")

    monkeypatch.setattr(db, "count_guardrail_events_by_execution", _boom)

    assert _reading(_list(world)[run]) == (None, "unavailable")
    assert _reading(_detail(world, run)) == (None, "unavailable")


# ---------------------------------------------------------------------------
# Scope and cost
# ---------------------------------------------------------------------------

def test_a_run_with_no_schedule_carries_neither_field(world, monkeypatch):
    seen = []
    real = db.count_guardrail_events_by_execution
    monkeypatch.setattr(db, "count_guardrail_events_by_execution",
                        lambda agent, ids: seen.append(list(ids)) or real(agent, ids))
    chat = _run(world, schedule_id="__manual__")
    _ingest([_event(chat)])

    assert _reading(_list(world)[chat]) == (None, None)
    assert _reading(_detail(world, chat)) == (None, None)
    assert all(chat not in ids for ids in seen)


def test_one_count_query_per_page_whatever_its_length(world, monkeypatch):
    runs = [_run(world) for _ in range(7)]
    _ingest([_event(runs[0])])
    calls = []
    real = db.count_guardrail_events_by_execution
    monkeypatch.setattr(db, "count_guardrail_events_by_execution",
                        lambda agent, ids: calls.append(sorted(ids)) or real(agent, ids))

    rows = _list(world)

    assert calls == [sorted(runs)]
    assert _reading(rows[runs[0]]) == (1, None)
    assert all(_reading(rows[r]) == (0, None) for r in runs[1:])

    calls.clear()
    _history(world)
    assert calls == [sorted(runs)]


# ---------------------------------------------------------------------------
# The mapping itself: precedence and the wire vocabulary
# ---------------------------------------------------------------------------

def test_the_markers_are_exactly_the_wire_literal():
    from typing import get_args

    from models import GuardrailReadingMarker
    from services import execution_refusals as er

    markers = {v for k, v in vars(er).items() if k.startswith("READING_")}
    assert markers == set(get_args(GuardrailReadingMarker))


@pytest.mark.parametrize("reporting, ended_ago, expected", [
    # `never` wins over everything, even a runtime that is not Claude Code.
    ({"state": "never", "runtime": "gemini"}, 60, (None, "never")),
    # A non-Claude-Code reporter that is also stale reads as the runtime case.
    ({"state": "stale", "runtime": "codex"}, 60, (None, "not_claude_code")),
    # Stale wins over pending: the more severe doubt is the one stated.
    ({"state": "stale", "runtime": "claude-code"}, 1, (0, "stale")),
    ({"state": "reporting", "runtime": "claude-code"}, 1, (0, "pending")),
    ({"state": "reporting", "runtime": "claude-code"}, 60, (0, None)),
])
def test_the_markers_go_most_severe_first(reporting, ended_ago, expected):
    from services import execution_refusals as er

    now = utc_now()
    assert er.reading_for(None, reporting, now - timedelta(seconds=ended_ago), now) == expected


def test_a_naive_completed_at_is_read_as_utc():
    """`ScheduleExecution.completed_at` may come back naive from a legacy row."""
    from services import execution_refusals as er

    now = utc_now()
    naive = (now - timedelta(seconds=60)).replace(tzinfo=None)
    entry = {"by_kind": {"deny": 3}, "by_hook_code": {"turn_tier_guard:shell_owner_only": 3}}
    assert er.reading_for(entry, {"state": "reporting", "runtime": "claude-code"}, naive, now) == (3, None)
