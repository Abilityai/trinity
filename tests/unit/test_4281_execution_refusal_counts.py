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
from utils.helpers import parse_iso_timestamp, to_utc_iso, utc_now  # noqa: E402

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


def _run(world, *, ended_seconds_ago=60.0, started_seconds_ago=None, status="success",
         schedule_id=None, agent=AGENT):
    """A schedule run that ended `ended_seconds_ago` (None: still running) and
    started `started_seconds_ago` (default: 30 s before it ended)."""
    sid = world["schedule_id"] if schedule_id is None else schedule_id
    if sid == "__manual__":
        execution = db.create_task_execution(agent, "hello", triggered_by="manual")
    else:
        execution = db.create_schedule_execution(sid, agent, "brief", triggered_by="schedule")
    if started_seconds_ago is None:
        started_seconds_ago = (ended_seconds_ago or 0) + 30
    with get_engine().begin() as conn:
        conn.execute(text("UPDATE schedule_executions SET started_at = :s WHERE id = :i"),
                     {"s": _iso_ago(started_seconds_ago), "i": execution.id})
        if ended_seconds_ago is not None:
            conn.execute(
                text("UPDATE schedule_executions SET status = :s, completed_at = :c WHERE id = :i"),
                {"s": status, "c": _iso_ago(ended_seconds_ago), "i": execution.id})
    return execution.id


# The agent's forwarder has been reporting since well before any run a test
# makes: a run that started before the agent's first report is not covered.
COVERED_SINCE_SECONDS = 2 * 3600


def _reported_since(seconds=COVERED_SINCE_SECONDS, agent=AGENT):
    """The agent's first report reached the backend `seconds` ago."""
    assert svc.record_report(agent, "claude-code", 0, _iso_ago(seconds))


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
    _reported_since()
    run = _run(world)
    _keepalive()
    assert _reading(_list(world)[run]) == (0, None)

    _ingest([_event(run), _event(run, code="mcp_owner_only")])

    assert _reading(_list(world)[run]) == (2, None)
    assert _reading(_detail(world, run)) == (2, None)
    assert _reading(_history(world)[run]) == (2, None)


def test_a_run_absent_from_the_counts_while_the_agent_reports_is_zero(world):
    _reported_since()
    refused, clean = _run(world), _run(world)
    _ingest([_event(refused)])

    rows = _list(world)
    assert _reading(rows[refused]) == (1, None)
    assert _reading(rows[clean]) == (0, None)


# ---------------------------------------------------------------------------
# Only the turn-tier hook's refusals count
# ---------------------------------------------------------------------------

def test_a_credential_redaction_alone_never_makes_a_run_unclean(world):
    _reported_since()
    run = _run(world)
    _ingest([_event(run, hook="output_scanner", code="credential_redacted", kind="credential_redacted")])

    assert _reading(_list(world)[run]) == (0, None)


def test_another_hooks_deny_is_not_a_turn_tier_refusal(world):
    _reported_since()
    run = _run(world)
    _ingest([_event(run, hook="bash_guardrail", code="bash_deny_pattern"),
             _event(run, hook="turn_tier_guard", code="schedule_creator_tier")])

    assert _reading(_list(world)[run]) == (1, None)


def test_an_event_another_agent_files_against_this_run_is_ignored(world):
    """`execution_id` is attribution, not authority (ent#165 §6.1): a sibling
    agent naming this run's id never makes it unclean."""
    _reported_since()
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
    _reported_since()
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


# ---------------------------------------------------------------------------
# Coverage starts at the agent's first report (review E-F3, ruling R5)
# ---------------------------------------------------------------------------

def test_a_run_that_ended_before_the_agents_first_report_reads_unknown_not_zero(world):
    """The upgrade case: a run refused on an image with no forwarder, then the
    agent is recreated and reports. Its history must not turn into a clean 0."""
    before = _run(world, ended_seconds_ago=3600)
    assert _reading(_list(world)[before]) == (None, "never")

    _keepalive()  # the agent's first report, an hour after the run ended

    assert svc.reporting_state(AGENT)["state"] == "reporting"
    assert _reading(_list(world)[before]) == (None, "never")
    assert _reading(_detail(world, before)) == (None, "never")
    assert _reading(_history(world)[before]) == (None, "never")


def test_a_run_that_started_after_the_first_report_is_covered(world):
    """Positive control for the rule above: the same agent, a run inside coverage."""
    before = _run(world, ended_seconds_ago=3600)
    _reported_since(600)  # the first report, ten minutes ago
    after = _run(world, ended_seconds_ago=60, started_seconds_ago=300)
    _keepalive()  # a report after the run ended

    rows = _list(world)
    assert _reading(rows[after]) == (0, None)
    assert _reading(_detail(world, after)) == (0, None)
    assert _reading(_history(world)[after]) == (0, None)
    assert _reading(rows[before]) == (None, "never")


def test_a_run_that_straddles_the_first_report_is_not_read_as_complete(world):
    """It started before the agent first reported and ended after. Its tool
    calls before that report are not provably covered, so even a later report
    does not make its count complete; a refusal that did arrive still shows."""
    clean = _run(world, ended_seconds_ago=60, started_seconds_ago=600)
    refused = _run(world, ended_seconds_ago=60, started_seconds_ago=600)
    _reported_since(300)  # the first report, mid-run
    _ingest([_event(refused)])  # after both runs ended
    assert (parse_iso_timestamp(svc.reporting_state(AGENT)["last_report_at"])
            > parse_iso_timestamp(_list(world)[clean]["completed_at"]))

    rows = _list(world)
    assert _reading(rows[clean]) == (None, "never")
    assert _reading(rows[refused]) == (1, "never")
    assert _reading(_detail(world, refused)) == (1, "never")
    assert _reading(_history(world)[clean]) == (None, "never")


def test_a_lost_fast_store_moves_coverage_later_never_earlier(world):
    """Redis flushed with no stored rows: the next report is the new coverage
    start, so a clean run from before the flush reads unknown, never 0."""
    _reported_since()
    run = _run(world, ended_seconds_ago=600)
    _keepalive()
    assert _reading(_list(world)[run]) == (0, None)

    world["redis"].flushall()
    _keepalive()

    assert _reading(_list(world)[run]) == (None, "never")


def _redeliver(run, *, requeued_seconds_ago):
    """The lease reaper re-queues the run under the SAME id (#1081, pull mode):
    `started_at` is reset, so the first attempt's start is no longer on record."""
    with get_engine().begin() as conn:
        conn.execute(text("UPDATE schedule_executions SET status = 'running', lease_expires_at = :l "
                          "WHERE id = :i"), {"l": _iso_ago(requeued_seconds_ago + 60), "i": run})
    assert db.requeue_expired_lease(run, _iso_ago(requeued_seconds_ago))


@pytest.mark.parametrize("first_report_ago", [900, COVERED_SINCE_SECONDS])
def test_a_redelivered_run_reads_unknown_its_first_attempt_start_is_lost(world, first_report_ago):
    """Second voice: attempt 1 ran 20 min ago, the reaper re-queued it 5 min
    ago and reset `started_at`, so a start check alone would read the earlier
    attempt's calls as covered. No column keeps the first attempt's start, so a
    redelivered run reads `never` even when coverage began long before (the
    second case is that cost, pinned)."""
    run = _run(world, ended_seconds_ago=None, started_seconds_ago=1200)
    _reported_since(first_report_ago)
    _redeliver(run, requeued_seconds_ago=300)
    with get_engine().begin() as conn:
        conn.execute(text("UPDATE schedule_executions SET status = 'success', completed_at = :c "
                          "WHERE id = :i"), {"c": _iso_ago(60), "i": run})
    _keepalive()
    started = parse_iso_timestamp(_list(world)[run]["started_at"])
    assert started > parse_iso_timestamp(svc.reporting_state(AGENT)["first_report_at"])

    assert _reading(_list(world)[run]) == (None, "never")
    assert _reading(_detail(world, run)) == (None, "never")
    assert _reading(_history(world)[run]) == (None, "never")
    # The list's response model still filters the extra column it now reads.
    assert "redelivery_count" not in _list(world)[run]


def test_a_read_inside_the_delivery_bound_is_pending_not_a_bare_zero(world):
    """Review R11: events land up to ~2 s (p99 <= 5 s) after the hook fired."""
    _reported_since()
    _keepalive()
    just_ended = _run(world, ended_seconds_ago=2)
    running = _run(world, ended_seconds_ago=None)
    _ingest([_event(running)])

    rows = _list(world)
    assert _reading(rows[just_ended]) == (0, "pending")
    assert _reading(rows[running]) == (1, "pending")


def test_the_real_terminal_write_reads_pending_at_once(world):
    _reported_since()
    _keepalive()
    run = _run(world, ended_seconds_ago=None)
    assert db.update_execution_status(run, "success")

    assert _reading(_detail(world, run)) == (0, "pending")


def test_a_run_the_reporter_has_not_reported_past_is_pending_not_a_complete_zero(world):
    """The delivery bound holds only while the forwarder can deliver. One that
    cannot (transport errors, 5xx back-off, a refused key) still reads
    `reporting` for STALE_AFTER_SECONDS while the run's refusals sit in its
    spool, so "no events" is evidence only once a report arrived after the run
    ended: every successful post carries the spool's backlog."""
    _reported_since()
    _keepalive()
    run = _run(world, ended_seconds_ago=30)
    # The last report the backend received came 2.5 min before the run ended.
    assert svc.record_report(AGENT, "claude-code", 0, _iso_ago(180))
    assert svc.reporting_state(AGENT)["state"] == "reporting"

    assert _reading(_list(world)[run]) == (0, "pending")
    assert _reading(_detail(world, run)) == (0, "pending")
    assert _reading(_history(world)[run]) == (0, "pending")

    # The backlog lands: the refusal is counted and the count is complete.
    _ingest([_event(run)])
    assert _reading(_list(world)[run]) == (1, None)


def test_a_keepalive_after_the_run_ended_settles_a_clean_run_at_zero(world):
    run = _run(world, ended_seconds_ago=30)
    assert svc.record_report(AGENT, "claude-code", 0, _iso_ago(180))
    assert _reading(_list(world)[run]) == (0, "pending")

    _keepalive()

    assert _reading(_list(world)[run]) == (0, None)


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
    _reported_since()
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


_COVERED = {"first_report_at": "long_ago"}


@pytest.mark.parametrize("reporting, ended_ago, expected", [
    # `never` wins over everything, even a runtime that is not Claude Code.
    ({"state": "never", "runtime": "gemini"}, 60, (None, "never")),
    # A non-Claude-Code reporter that is also stale reads as the runtime case.
    ({"state": "stale", "runtime": "codex", **_COVERED}, 60, (None, "not_claude_code")),
    # ...and so does one whose coverage started after the run did.
    ({"state": "reporting", "runtime": "gemini", "first_report_at": "after_start"}, 60,
     (None, "not_claude_code")),
    # Before coverage wins over stale and pending: the run will never be covered.
    ({"state": "stale", "runtime": "claude-code", "first_report_at": "after_start"}, 1, (None, "never")),
    ({"state": "reporting", "runtime": "claude-code", "first_report_at": "after_start",
      "last_report_at": "now"}, 60, (None, "never")),
    # A coverage start that is missing proves nothing: no data, not a clean 0.
    ({"state": "reporting", "runtime": "claude-code", "last_report_at": "now"}, 60, (None, "never")),
    # Coverage that began the instant the run started covers it.
    ({"state": "reporting", "runtime": "claude-code", "first_report_at": "at_start",
      "last_report_at": "now"}, 60, (0, None)),
    # Stale wins over pending: the more severe doubt is the one stated.
    ({"state": "stale", "runtime": "claude-code", **_COVERED}, 1, (0, "stale")),
    ({"state": "reporting", "runtime": "claude-code", **_COVERED}, 1, (0, "pending")),
    # Past the delivery bound, complete only once the reporter reported after the end.
    ({"state": "reporting", "runtime": "claude-code", "last_report_at": "now", **_COVERED}, 60, (0, None)),
    ({"state": "reporting", "runtime": "claude-code", "last_report_at": "before_end", **_COVERED}, 60,
     (0, "pending")),
    ({"state": "reporting", "runtime": "claude-code", **_COVERED}, 60, (0, "pending")),
])
def test_the_markers_go_most_severe_first(reporting, ended_ago, expected):
    from services import execution_refusals as er

    now = utc_now()
    ended = now - timedelta(seconds=ended_ago)
    started = ended - timedelta(seconds=30)
    stamps = {"now": to_utc_iso(now), "before_end": to_utc_iso(ended - timedelta(seconds=1)),
              "long_ago": to_utc_iso(now - timedelta(days=30)), "at_start": to_utc_iso(started),
              "after_start": to_utc_iso(started + timedelta(microseconds=1))}
    reporting = {k: stamps[v] if k.endswith("_at") else v for k, v in reporting.items()}
    assert er.reading_for(None, reporting, started, ended, now) == expected


def test_a_refusal_on_a_run_from_before_coverage_still_shows():
    """No data is not the same as a known refusal: a count that did arrive is
    a lower bound, so it is kept, with the marker that it may be short."""
    from services import execution_refusals as er

    now = utc_now()
    started = now - timedelta(seconds=600)
    entry = {"by_kind": {"deny": 2}, "by_hook_code": {"turn_tier_guard:shell_owner_only": 2}}
    reporting = {"state": "reporting", "runtime": "claude-code", "last_report_at": to_utc_iso(now),
                 "first_report_at": to_utc_iso(now - timedelta(seconds=300))}
    assert er.reading_for(entry, reporting, started, now - timedelta(seconds=60), now) == (2, "never")
    assert er.reading_for(None, reporting, None, now - timedelta(seconds=60), now) == (None, "never")
    # A redelivered run's start is its last attempt's: the first one's is lost.
    covered = {**reporting, "first_report_at": to_utc_iso(now - timedelta(days=30))}
    assert er.reading_for(entry, covered, started, now - timedelta(seconds=60), now, 0) == (2, None)
    assert er.reading_for(entry, covered, started, now - timedelta(seconds=60), now, 1) == (2, "never")
    assert er.reading_for(None, covered, started, now - timedelta(seconds=60), now, None) == (0, None)


def test_a_naive_completed_at_is_read_as_utc():
    """`ScheduleExecution.completed_at` may come back naive from a legacy row."""
    from services import execution_refusals as er

    now = utc_now()
    naive = (now - timedelta(seconds=60)).replace(tzinfo=None)
    naive_start = naive - timedelta(seconds=30)
    entry = {"by_kind": {"deny": 3}, "by_hook_code": {"turn_tier_guard:shell_owner_only": 3}}
    reporting = {"state": "reporting", "runtime": "claude-code", "last_report_at": to_utc_iso(now),
                 "first_report_at": to_utc_iso(now - timedelta(seconds=120))}
    assert er.reading_for(entry, reporting, naive_start, naive, now) == (3, None)
    # Read as UTC, not as local time: a naive start just after coverage is covered.
    just_after = (now - timedelta(seconds=119)).replace(tzinfo=None)
    assert er.reading_for(entry, reporting, just_after, naive, now) == (3, None)
