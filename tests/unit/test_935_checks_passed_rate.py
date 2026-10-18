"""The "Checks passed" rate on the analytics rollups (trinity-enterprise#935, C3).

AC: "The Overview and schedules rollup show Completion and Checks passed as two
numbers." Completion is the existing exit-based `success_rate`. Checks passed is
read from the Tier-0 rows (`evaluator='platform:tier0'`) and added beside it as
`checks_passed_rate` + `evaluated_count` on all three analytics payloads:

  * `get_schedule_analytics`       — per schedule (ScheduleAnalyticsCard)
  * `get_agent_schedules_summary`  — the per-schedule rollup rows (Overview)
  * `get_agent_analytics`          — the agent headline (Overview)

Pinned semantics (FR-10a):
  * the denominator is the runs with a gate check GRADED — at least one gate
    passed or failed. A skip-only row (every gate skipped, e.g. an evaluator
    timeout, or nothing to check) is in NEITHER numerator nor denominator: the
    execution page reads it as "No gate check applied", and the rollup must not
    count the same row as a failure (review I5, re-ruling C3);
  * a graded run PASSES when no gate failed (so at least one gate passed);
  * an execution with no Tier-0 row is "not evaluated": it is in neither the
    numerator nor the denominator — never a failure;
  * `evaluated_count == 0` → `checks_passed_rate is None` (the UI shows "—"),
    including when every Tier-0 row in the window is skip-only;
  * only `platform:tier0` rows count — a `workspace:` or `operator:` rating of
    the same execution never enters the rate.

Real schema via `tests/db_harness.py`.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")

import pytest  # noqa: E402

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
from db_harness import db_backend  # noqa: E402,F401

pytestmark = pytest.mark.unit

AGENT = "agent-a"


def _ago(minutes: float) -> str:
    when = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    return when.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def seed_schedule(sid: str, agent: str = AGENT) -> str:
    from sqlalchemy import insert
    from db.engine import get_engine
    from db.tables import agent_schedules
    with get_engine().begin() as conn:
        conn.execute(insert(agent_schedules).values(
            id=sid, agent_name=agent, name=f"name-{sid}", cron_expression="0 * * * *",
            message="/report-weekly", enabled=1, timezone="UTC", owner_id=1,
            created_at=_ago(600), updated_at=_ago(600),
        ))
    return sid


def seed_exec(exec_id: str, *, schedule_id: str = "s1", agent: str = AGENT,
              status: str = "success", triggered_by: str = "schedule",
              minutes_ago: float = 10) -> str:
    from sqlalchemy import insert
    from db.engine import get_engine
    from db.tables import schedule_executions
    with get_engine().begin() as conn:
        conn.execute(insert(schedule_executions).values(
            id=exec_id, schedule_id=schedule_id, agent_name=agent, status=status,
            triggered_by=triggered_by, message="/report-weekly",
            started_at=_ago(minutes_ago + 1), completed_at=_ago(minutes_ago),
            duration_ms=1000,
        ))
    return exec_id


def _headline(gate_pass: int, gate_fail: int, gate_skip: int) -> dict:
    return {"passed": gate_fail == 0, "gate_pass": gate_pass,
            "gate_fail": gate_fail, "gate_skip": gate_skip}


def grade(exec_id: str, *, gate_pass=2, gate_fail=0, gate_skip=0, agent=AGENT):
    """Write the run's Tier-0 row through the real writer."""
    from database import db
    db.upsert_platform_evaluation(
        agent, execution_id=exec_id, completion=True,
        checks={"schema": 1, "headline": _headline(gate_pass, gate_fail, gate_skip),
                "checks": []})


def _rate_fields(payload: dict) -> tuple:
    return payload["checks_passed_rate"], payload["evaluated_count"]


def _schedule(sid="s1"):
    from database import db
    return db.get_schedule_analytics(sid, 24, AGENT)


def _summary_row(sid="s1"):
    from database import db
    rows = db.get_agent_schedules_summary(AGENT, 24)["schedules"]
    return next(r for r in rows if r["schedule_id"] == sid)


def _agent():
    from database import db
    return db.get_agent_analytics(AGENT, 24)


READERS = {"schedule": _schedule, "summary": _summary_row, "agent": _agent}


@pytest.fixture
def seeded(db_backend):
    """Five runs on one schedule: pass, pass, fail, skip-only, not evaluated."""
    seed_schedule("s1")
    for i in range(1, 6):
        seed_exec(f"ex-{i}")
    grade("ex-1", gate_pass=3)
    grade("ex-2", gate_pass=1, gate_skip=2)          # skips beside a pass: passed
    grade("ex-3", gate_pass=2, gate_fail=1)          # a gate failed
    grade("ex-4", gate_pass=0, gate_fail=0, gate_skip=5)  # skip-only: no gate graded
    # ex-5: no Tier-0 row — not evaluated
    return db_backend


@pytest.mark.parametrize("reader", sorted(READERS))
def test_rate_is_passed_over_evaluated(seeded, reader):
    rate, evaluated = _rate_fields(READERS[reader]())
    assert evaluated == 3, ("neither the run with no Tier-0 row nor the skip-only "
                            "row had a gate check graded")
    assert rate == round(2 / 3, 4), "2 passed of 3 graded; a skip-only row is not a fail"


@pytest.mark.parametrize("reader", sorted(READERS))
def test_completion_is_unchanged_and_separate(seeded, reader):
    payload = READERS[reader]()
    assert payload["success_rate"] == 1.0, "Completion is exit-based and untouched"


@pytest.mark.parametrize("reader", sorted(READERS))
def test_zero_evaluated_is_none_not_zero(db_backend, reader):
    seed_schedule("s1")
    seed_exec("ex-1")
    seed_exec("ex-2", status="failed")
    rate, evaluated = _rate_fields(READERS[reader]())
    assert evaluated == 0
    assert rate is None, "no evaluated runs renders '—', never 0%"


@pytest.mark.parametrize("reader", sorted(READERS))
def test_all_skip_only_is_none_not_zero_percent(db_backend, reader):
    seed_schedule("s1")
    seed_exec("ex-1")
    grade("ex-1", gate_pass=0, gate_skip=5)
    assert _rate_fields(READERS[reader]()) == (None, 0), \
        "nothing could be checked renders '—', never 0%"


@pytest.mark.parametrize("reader", sorted(READERS))
@pytest.mark.parametrize("evaluator", ["workspace:user@example.com", "operator:admin"])
def test_other_evaluators_never_enter_the_rate(db_backend, reader, evaluator):
    from database import db
    seed_schedule("s1")
    seed_exec("ex-1")
    seed_exec("ex-2")
    grade("ex-1", gate_pass=0, gate_fail=2)
    # A person's rating of the other run, keyed on the same execution — and a
    # thumbs-up of the failed one. Neither is a Tier-0 grade.
    db.upsert_workspace_rating(AGENT, evaluator=evaluator, target_kind="execution",
                               target_id="ex-2", execution_id="ex-2", quality=1.0)
    db.create_agent_evaluation(AGENT, evaluator=evaluator, execution_id="ex-1", quality=1.0,
                               checks={"headline": _headline(3, 0, 0)})
    assert _rate_fields(READERS[reader]()) == (0.0, 1)


def test_rows_outside_the_window_or_scope_do_not_count(db_backend):
    seed_schedule("s1")
    seed_schedule("s2")
    seed_exec("old", minutes_ago=60 * 48)
    seed_exec("other-sched", schedule_id="s2")
    seed_exec("other-agent", agent="agent-b", schedule_id="sb")
    for ex in ("old", "other-sched"):
        grade(ex, gate_pass=2)
    grade("other-agent", gate_pass=2, agent="agent-b")
    assert _rate_fields(_schedule("s1")) == (None, 0)
    assert _rate_fields(_summary_row("s1")) == (None, 0)
    assert _rate_fields(_summary_row("s2")) == (1.0, 1)
    assert _rate_fields(_agent()) == (1.0, 1), "agent-b's run and the 48h-old run excluded"


def test_response_models_carry_the_fields(seeded):
    """The routers build Pydantic envelopes — a field the model does not
    declare is silently dropped on the wire."""
    from models import (AgentAnalyticsResponse, AgentSchedulesSummaryResponse,
                        ScheduleAnalyticsResponse)
    from database import db
    out = ScheduleAnalyticsResponse(**_schedule()).model_dump()
    assert (out["checks_passed_rate"], out["evaluated_count"]) == (0.6667, 3)
    out = AgentAnalyticsResponse(**_agent()).model_dump()
    assert (out["checks_passed_rate"], out["evaluated_count"]) == (0.6667, 3)
    out = AgentSchedulesSummaryResponse(**db.get_agent_schedules_summary(AGENT, 24)).model_dump()
    assert (out["schedules"][0]["checks_passed_rate"], out["schedules"][0]["evaluated_count"]) == (0.6667, 3)
