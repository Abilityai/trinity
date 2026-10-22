"""Per-agent behavioral indicators — the pure fold (trinity-enterprise#939).

Pinned: each indicator is its own measure (no blended score), a thin sample
reads "insufficient" (never zero), an empty window reads "no_runs", the delta
appears only when both windows carry enough runs, judge dimensions come from
the stored verdict document, cost is the window median over the prior
baseline, and the fix paths group failures by where they are fixed.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")

import pytest  # noqa: E402

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from services import agent_indicators as ai  # noqa: E402

pytestmark = pytest.mark.unit


def _row(i, *, done=True, fail=None, playbook=None, schedule=None, judge=None,
         quality=None, cost=None):
    checks = [{"id": "deliverable_landed", "status": "fail", "reason": "no_receipt"}] if fail else []
    return {
        "execution_id": f"e{i}", "agent_name": "a", "completion": done,
        "schedule_id": schedule, "completed_at": f"2026-10-{10 + i % 9:02d}T09:00:00.000000Z",
        "cost": cost,
        "checks": {"intent_ref": {"kind": "playbook" if playbook else "message", "ref": playbook},
                   "headline": {"passed": not fail, "gate_pass": 1, "gate_fail": 1 if fail else 0,
                                "gate_skip": 0},
                   "checks": checks},
        "judge": judge, "judge_quality": quality,
    }


def _judge(**dims):
    return {"schema": 1, "state": "judged",
            "dimensions": {k: {"verdict": v} for k, v in dims.items()}}


def _ind(out, iid):
    return next(i for i in out["indicators"] if i["id"] == iid)


def test_no_runs_reads_no_runs_never_zero():
    out = ai.compute([], [], {}, [], min_runs=5)
    assert out["runs"] == 0
    for ind in out["indicators"]:
        assert ind["state"] == "no_runs"
        assert ind["value"] is None


def test_thin_sample_is_insufficient_with_counts():
    rows = [_row(i) for i in range(3)]
    out = ai.compute(rows, [], {}, [], min_runs=5)
    c = _ind(out, "completion")
    assert c["state"] == "insufficient"
    assert (c["num"], c["n"]) == (3, 3)
    assert c["value"] is None


def test_rates_and_delta_points():
    rows = [_row(i, fail=i < 2) for i in range(10)]
    prior = [_row(i, fail=i < 5) for i in range(10)]
    out = ai.compute(rows, prior, {}, [], min_runs=5)
    cp = _ind(out, "checks_passed")
    assert cp["state"] == "ok" and cp["value"] == 0.8 and (cp["num"], cp["n"]) == (8, 10)
    assert cp["delta"] == 30  # points: 80% vs 50%


def test_no_delta_when_prior_is_thin():
    rows = [_row(i) for i in range(10)]
    out = ai.compute(rows, [_row(1)], {}, [], min_runs=5)
    assert _ind(out, "completion")["delta"] is None


def test_judge_dimensions_ignore_na():
    rows = [_row(i, judge=_judge(scope_fidelity="pass" if i else "fail",
                                 escalation_correctness="n/a")) for i in range(6)]
    out = ai.compute(rows, [], {}, [], min_runs=5)
    sf = _ind(out, "scope_fidelity")
    assert (sf["num"], sf["n"], sf["state"]) == (5, 6, "ok")
    esc = _ind(out, "escalation_correctness")
    assert esc["n"] == 0 and esc["state"] == "insufficient"


def test_quality_mean_and_agreement():
    rows = [_row(i, quality=0.8) for i in range(5)]
    labels = {"e0": [1.0], "e1": [0.0]}
    out = ai.compute(rows, [], labels, [], min_runs=5)
    q = _ind(out, "quality")
    assert q["value"] == 0.8 and q["n"] == 5
    assert q["agreement"] == 0.5 and q["agreement_n"] == 2


def test_cost_vs_baseline_is_a_ratio_of_medians():
    rows = [_row(i, cost=c) for i, c in enumerate([0.4, 0.42, 0.44, 0.5, 0.42])]
    out = ai.compute(rows, [], {}, [0.3, 0.32, 0.34, 0.32, 0.31], min_runs=5)
    c = _ind(out, "cost_vs_baseline")
    assert c["state"] == "ok"
    assert c["median"] == 0.42 and c["baseline"] == 0.32
    assert c["value"] == round(0.42 / 0.32, 2)


def test_cost_without_baseline_is_insufficient():
    rows = [_row(i, cost=0.4) for i in range(6)]
    c = _ind(ai.compute(rows, [], {}, [], min_runs=5), "cost_vs_baseline")
    assert c["state"] == "insufficient" and c["value"] is None


def test_no_blended_score_anywhere():
    out = ai.compute([_row(i) for i in range(6)], [], {}, [], min_runs=5)
    assert set(out) >= {"indicators", "fix_paths", "runs"}
    assert not ({"score", "overall", "composite"} & set(out))
    assert [i["id"] for i in out["indicators"]] == list(ai.INDICATORS)


def test_fix_paths_group_by_playbook_schedule_and_chat():
    rows = ([_row(i, fail=True, playbook="lead-research") for i in range(3)]
            + [_row(10 + i, fail=True, schedule="s1") for i in range(2)]
            + [_row(20, judge=_judge(scope_fidelity="fail"))])
    paths = ai.compute(rows, [], {}, [], min_runs=5)["fix_paths"]
    assert paths[0] == {"check": "deliverable_landed", "judge": False,
                        "source": {"kind": "playbook", "ref": "lead-research"}, "failed": 3}
    assert {"check": "deliverable_landed", "judge": False,
            "source": {"kind": "schedule", "ref": "s1"}, "failed": 2} in paths
    assert {"check": "scope_fidelity", "judge": True,
            "source": {"kind": "chat", "ref": None}, "failed": 1} in paths


def test_drilldown_groups_failing_runs_by_source():
    rows = ([_row(i, fail=i < 2, playbook="lead-research", cost=0.5) for i in range(4)]
            + [_row(10, fail=True, schedule="s1")])
    groups = ai.failing_runs(rows, "checks_passed", check=None)
    assert groups[0]["source"] == {"kind": "playbook", "ref": "lead-research"}
    assert (groups[0]["failed"], groups[0]["runs"]) == (2, 4)
    assert groups[0]["items"][0]["reasons"] == ["deliverable_landed:no_receipt"]
    assert groups[0]["items"][0]["cost"] == 0.5
    assert groups[1]["source"]["kind"] == "schedule"


def test_drilldown_check_filter_and_judge_indicator():
    rows = [_row(1, fail=True), _row(2, judge=_judge(scope_fidelity="fail"))]
    assert ai.failing_runs(rows, "checks_passed", check="credential_echo") == []
    g = ai.failing_runs(rows, "scope_fidelity", check=None)
    assert g[0]["items"][0]["execution_id"] == "e2"
    assert g[0]["items"][0]["reasons"] == ["scope_fidelity"]


def test_unknown_indicator_refused():
    with pytest.raises(ai.IndicatorError):
        ai.failing_runs([], "cost_vs_baseline", check=None)


def test_plain_words_for_workspace_carry_no_numbers():
    rows = [_row(i, fail=i < 4) for i in range(10)]
    lines = ai.plain_lines(ai.compute(rows, [], {}, [], min_runs=5))
    by = {l["indicator"]: l for l in lines}
    assert by["completion"]["tone"] == "good"
    assert by["checks_passed"]["tone"] == "watch"
    assert by["escalation_correctness"]["tone"] == "unknown"
    for l in lines:
        assert set(l) == {"indicator", "tone"}
    assert "quality" not in by and "cost_vs_baseline" not in by


def test_a_manual_run_is_chat_not_a_schedule():
    row = _row(1, fail=True, schedule="__manual__")
    assert ai.source_of(row) == {"kind": "chat", "ref": None}
