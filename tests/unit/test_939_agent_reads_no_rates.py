"""An agent reads its failed checks, never a rate (trinity-enterprise#939).

#939: "agents see only failed checks", and "any agent-visible scalar" is out
of scope until ruled. The operator ruled (2026-10-29) to hold that line on
every read an agent key reaches: the summary/aggregate copy a machine gets
carries no rate and no success counter a rate could be rebuilt from, and the
trend series refuses those metrics to a machine. A person reads all of it.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")

import pytest  # noqa: E402

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

pytestmark = pytest.mark.unit

AGENT = "agent-a"
WITHHELD = ("completion_rate", "checks_passed_rate", "completed", "passed",
            "quality_mean", "judge_human_agreement", "agreement_n")
PAIR_WITHHELD = ("pair_passed", "pair_checks_passed_rate")


def _rows():
    fail = {"headline": {"passed": False, "gate_pass": 0, "gate_fail": 1},
            "checks": [{"id": "credential_echo", "tier": "gate", "status": "fail",
                        "reason": "echoed"}]}
    ok = {"headline": {"passed": True, "gate_pass": 1, "gate_fail": 0}, "checks": []}
    pair_fail = {"headline": {"passed": False, "gate_pass": 0, "gate_fail": 1},
                 "checks": [{"id": "relay_unverified", "tier": "gate", "status": "fail",
                             "reason": "no_readback"}]}
    return [
        {"execution_id": "e1", "agent_name": AGENT, "caller_agent": "agent-b",
         "completion": True, "status": "success", "completed_at": "x",
         "checks": fail, "pair_checks": pair_fail, "judge_quality": 0.2},
        {"execution_id": "e2", "agent_name": AGENT, "caller_agent": None,
         "completion": True, "status": "success", "completed_at": "x", "checks": ok},
    ]


def test_a_machine_summary_holds_failures_and_sample_size_only():
    from services.evaluation_read_service import fold, project_summary
    full = fold(_rows(), {}, pair=True)
    assert full["checks_passed_rate"] == 0.5 and full["passed"] == 1      # precondition
    served = project_summary(full, machine=True)
    for k in WITHHELD:
        assert k not in served, k
    for k in PAIR_WITHHELD:
        assert k not in served["pair"], k
    assert served["evaluated"] == 2 and served["graded"] == 2
    assert served["per_check_fail"] == {"credential_echo": 1}
    assert served["pair"]["pair_per_check_fail"] == {"relay_unverified": 1}
    assert project_summary(full, machine=False) == full


def test_the_response_model_keeps_a_withheld_key_absent_not_null():
    """Absent by rule — a null would read as "nothing counted"."""
    from models import EvaluationSummary
    from services.evaluation_read_service import fold, project_summary
    served = project_summary(fold(_rows(), {}), machine=True)
    dumped = EvaluationSummary(**served).model_dump()
    for k in WITHHELD:
        assert k not in dumped, k
    human = EvaluationSummary(**fold(_rows(), {})).model_dump()
    assert human["checks_passed_rate"] == 0.5 and human["passed"] == 1


@pytest.mark.parametrize("metric", [
    "eval.checks_passed_rate", "eval.completion_rate", "eval.pair_checks_passed_rate",
    "eval.passed", "eval.completed", "eval.pair_passed",
    "eval.quality_mean", "eval.judge_human_agreement"])
def test_a_machine_is_refused_every_rate_and_success_series(metric):
    from services.evaluation_read_service import MetricError, trend_metric
    with pytest.raises(MetricError):
        trend_metric(metric, machine=True)
    assert trend_metric(metric, machine=False) == metric


@pytest.mark.parametrize("metric", ["eval.evaluated", "eval.graded", "eval.judged",
                                    "eval.pair_graded"])
def test_a_machine_may_read_sample_size_series(metric):
    from services.evaluation_read_service import trend_metric
    assert trend_metric(metric, machine=True) == metric
