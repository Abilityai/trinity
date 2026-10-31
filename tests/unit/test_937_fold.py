"""Evaluation read service — period parsing and the fold (trinity-enterprise#937, C2).

`services/evaluation_read_service.py` is pure: it turns the window rows of
`db/evaluation_aggregates.py` into the aggregate numbers. Pinned here with
hand-built rows (no DB):

  * `evaluated` counts Tier-0-graded runs; `completion_rate` is over them;
  * `checks_passed_rate` uses the shared `tier0_gate_outcome` — skip-only
    and malformed rows are in neither numerator nor denominator;
  * per-check fail counts come from `checks[]`, wrong-typed entries ignored;
  * `judged` / `quality_mean` come from the Tier-1 columns; agreement is the
    share of judged runs with a human label where both sides agree;
  * zero rows is zeros and null rates, never an error;
  * the failed list is bounded and says `truncated`;
  * periods are UTC, bounded to 92 days, and bad input is `invalid_period`.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import pytest  # noqa: E402

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)


def row(eid, *, gate_pass=1, gate_fail=0, completion=True, checks=None,
        judge_quality=None, agent="b", caller=None, completed_at="2026-10-18T10:00:00.000000Z"):
    return {
        "execution_id": eid, "evaluation_id": f"ev-{eid}", "agent_name": agent,
        "caller_agent": caller, "completion": completion, "status": "success" if completion else "failed",
        "schedule_id": "s1", "completed_at": completed_at, "parent_execution_id": None,
        "delegation_mode": None, "judge_quality": judge_quality, "judge": None,
        "judge_evaluator": "platform:tier1:m" if judge_quality is not None else None,
        "checks": {"headline": {"passed": gate_fail == 0, "gate_pass": gate_pass,
                                "gate_fail": gate_fail, "gate_skip": 0},
                   "checks": checks if checks is not None else []},
    }


@pytest.fixture
def svc():
    from services import evaluation_read_service
    return evaluation_read_service


class TestFold:
    def test_zero_rows_is_zeros_and_null_rates(self, svc):
        out = svc.fold([], {})
        assert out["evaluated"] == 0 and out["graded"] == 0 and out["judged"] == 0
        assert out["completion_rate"] is None
        assert out["checks_passed_rate"] is None
        assert out["quality_mean"] is None
        assert out["judge_human_agreement"] is None
        assert out["per_check_fail"] == {}
        assert out["failed"] == [] and out["failed_truncated"] is False

    def test_rates(self, svc):
        rows = [row("e1"), row("e2", gate_fail=1, completion=False),
                row("e3", gate_pass=0), row("e4")]
        rows[2]["checks"]["headline"]["gate_skip"] = 2      # skip-only: not graded
        out = svc.fold(rows, {})
        assert out["evaluated"] == 4
        assert out["completed"] == 3 and out["completion_rate"] == 0.75
        assert out["graded"] == 3 and out["passed"] == 2
        assert out["checks_passed_rate"] == round(2 / 3, 4)

    def test_per_check_fail_counts_and_junk(self, svc):
        rows = [
            row("e1", gate_fail=1, checks=[{"id": "deliverable_landed", "status": "fail", "reason": "no_receipt"},
                                           {"id": "exit_clean", "status": "pass"}]),
            row("e2", gate_fail=1, checks=[{"id": "deliverable_landed", "status": "fail"},
                                           "junk", {"status": "fail"}, {"id": 7, "status": "fail"}]),
        ]
        rows.append({**row("e3"), "checks": {"headline": "x", "checks": "nope"}})
        out = svc.fold(rows, {})
        assert out["per_check_fail"] == {"deliverable_landed": 2}
        assert out["graded"] == 2          # e3 is malformed -> not graded, never raises

    def test_failed_list_is_bounded_and_carries_both_ids(self, svc):
        rows = [row(f"e{i}", gate_fail=1, caller="a",
                    checks=[{"id": "exit_clean", "status": "fail", "reason": "nonzero_exit",
                             "evidence": {"claim": "free text"}}]) for i in range(4)]
        out = svc.fold(rows, {}, failed_limit=3)
        assert len(out["failed"]) == 3 and out["failed_truncated"] is True
        first = out["failed"][0]
        assert first["execution_id"] == "e0" and first["agent_name"] == "b" and first["caller_agent"] == "a"
        assert first["failed_checks"] == [{"id": "exit_clean", "reason": "nonzero_exit"}]
        assert "evidence" not in str(out["failed"])     # no free text re-enters a context

    def test_a_completion_failure_with_passing_gates_is_failed(self, svc):
        out = svc.fold([row("e1", completion=False)], {})
        assert [f["execution_id"] for f in out["failed"]] == ["e1"]

    def test_judged_quality_and_agreement(self, svc):
        rows = [row("e1", judge_quality=0.9), row("e2", judge_quality=0.2),
                row("e3", judge_quality=0.7), row("e4")]
        labels = {"e1": [1.0], "e2": [1.0, 1.0], "e4": [0.0]}   # e4 not judged: ignored
        out = svc.fold(rows, labels)
        assert out["judged"] == 3
        assert out["quality_mean"] == round((0.9 + 0.2 + 0.7) / 3, 4)
        assert out["judge_human_agreement"] == 0.5          # e1 agrees, e2 disagrees
        assert out["agreement_n"] == 2

    def test_agreement_is_null_without_labels(self, svc):
        assert svc.fold([row("e1", judge_quality=0.9)], {})["judge_human_agreement"] is None


class TestPeriod:
    def test_day_and_week_are_rolling_utc(self, svc):
        assert svc.parse_period("day", now=NOW) == ("2026-10-17T12:00:00.000000Z", "2026-10-18T12:00:00.000001Z")
        since, until = svc.parse_period("week", now=NOW)
        assert since == "2026-10-11T12:00:00.000000Z"

    def test_custom(self, svc):
        assert svc.parse_period("custom", since="2026-10-01T00:00:00Z", until="2026-10-02T00:00:00Z",
                                now=NOW) == ("2026-10-01T00:00:00.000000Z", "2026-10-02T00:00:00.000000Z")

    @pytest.mark.parametrize("kw", [
        {"period": "month"},
        {"period": "custom"},
        {"period": "custom", "since": "2026-10-02T00:00:00Z", "until": "2026-10-01T00:00:00Z"},
        {"period": "custom", "since": "2026-01-01T00:00:00Z", "until": "2026-10-01T00:00:00Z"},
        {"period": "custom", "since": "yesterday", "until": "2026-10-01T00:00:00Z"},
        {"period": "custom", "since": "2026-10-01", "until": "2026-10-02T00:00:00Z"},
    ])
    def test_invalid(self, svc, kw):
        with pytest.raises(svc.PeriodError) as e:
            svc.parse_period(kw.pop("period"), now=NOW, **kw)
        assert e.value.code == "invalid_period"
