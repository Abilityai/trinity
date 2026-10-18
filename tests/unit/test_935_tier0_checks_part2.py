"""Tier-0 checks, part 2 (trinity-enterprise#935, checkpoint A2).

`playbook_outputs` (gate) — the skill's declared outputs must have left
receipts. Declared outputs come from an `outputs:` frontmatter key read
through the ent#183 hardened parser (flat or under `trinity:`) first, and from
the `## Output` prose section only when the frontmatter declares none (TD-5).

`budget_vs_baseline` (signal) — cost / duration / tool calls / context against
the p5–p95 band of the same series' last 20 successful runs (min 5).

The headline (TD-7) is computed from gate checks only: a failing signal is
recorded and never moves it.

The null tests the epic demands for this layer: a do-nothing run and a
claims-everything-done run both fail the headline; an honest abort passes it.
"""
from __future__ import annotations

import json
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

from services import tier0_evaluator as t0  # noqa: E402

pytestmark = pytest.mark.unit

FAKE_PAT = "ghp_" + "B" * 36


def _receipts(asks=0, effects=0, metrics=0, reports=0):
    return {
        "asks": {"count": asks, "ids": [f"q{i}" for i in range(asks)]},
        "effects": {"count": effects},
        "metrics": {"count": metrics},
        "reports_in_window": {"count": reports, "ids": [f"r{i}" for i in range(reports)]},
    }


def _skill(content, **extra):
    return {"status": "found", "content": content, "source_id": "src-1",
            "version": "abc123", **extra}


FM_REPORT_ASK = "---\nname: weekly\noutputs:\n  - report\n  - kind: ask\n---\n# Weekly\n"
PROSE_EFFECT = "---\nname: digest\n---\n# Digest\n\n## Output\n\nSends a Slack message to #ops.\n\n## Notes\nA report is not produced.\n"


def _ctx(response="", *, message="/weekly", receipts=None, skill=None, series=None,
         status="success", **row):
    execution = {"id": "ex-1", "agent_name": "agent-a", "status": status,
                 "triggered_by": "schedule", "schedule_id": "sched-1", "message": message,
                 "response": response, "error": None, "turn_integrity": None,
                 "business_status": None, "validation_execution_id": None,
                 "cost": None, "duration_ms": None, "context_used": None,
                 "tool_call_count": None}
    execution.update(row)
    return {"execution": execution,
            "intent": t0.resolve_intent(message, response=response),
            "receipts": receipts if receipts is not None else _receipts(),
            "skill": skill, "series": series, "staged_values": []}


def _run(check_id, ctx):
    return {c.id: c.fn for c in t0.CHECKS}[check_id](ctx)


# --- registry ---------------------------------------------------------------------

def test_a2_checks_are_registered_with_their_tier():
    by_id = {c.id: c for c in t0.CHECKS}
    assert by_id["playbook_outputs"].tier == "gate"
    assert by_id["budget_vs_baseline"].tier == "signal"
    assert {c.source for c in t0.CHECKS} == {"platform"}
    assert len(by_id) == len(t0.CHECKS)


# --- declared outputs (TD-5) --------------------------------------------------------

def test_frontmatter_outputs_are_read_first():
    d = t0.declared_outputs(FM_REPORT_ASK + "\n## Output\nSends a Slack message.\n")
    assert d == {"source": "frontmatter", "kinds": ["ask", "report"], "unmapped": 0}


def test_a_trinity_block_outputs_key_wins_over_the_flat_one():
    content = "---\noutputs: [report]\ntrinity:\n  outputs: [metric]\n---\n"
    assert t0.declared_outputs(content)["kinds"] == ["metric"]


def test_prose_output_section_is_the_fallback_and_stops_at_the_next_heading():
    d = t0.declared_outputs(PROSE_EFFECT)
    assert d == {"source": "prose", "kinds": ["effect"], "unmapped": 0}


def test_unmapped_declared_outputs_are_counted_not_guessed():
    d = t0.declared_outputs("---\noutputs: [file, {kind: canvas}]\n---\n")
    assert d == {"source": "frontmatter", "kinds": [], "unmapped": 2}


def test_no_declaration_anywhere():
    assert t0.declared_outputs("---\nname: x\n---\n# X\nDoes things.\n") is None
    assert t0.declared_outputs("") is None


def test_a_hostile_frontmatter_never_raises():
    assert t0.declared_outputs("---\na: &x 1\nb: *x\n---\n## Output\nposts a report\n") == \
        {"source": "prose", "kinds": ["report"], "unmapped": 0}


# --- playbook_outputs -----------------------------------------------------------------

def test_playbook_outputs_passes_when_every_declared_output_left_a_receipt():
    r = _run("playbook_outputs", _ctx("Filed.", skill=_skill(PROSE_EFFECT),
                                      message="/digest", receipts=_receipts(effects=1)))
    assert r["status"] == "pass"
    assert r["evidence"]["declared"] == ["effect"]
    assert r["evidence"]["contract_source"] == "prose"
    assert r["evidence"]["skill_source_id"] == "src-1"
    assert r["evidence"]["skill_version"] == "abc123"


def test_playbook_outputs_fails_on_a_declared_output_with_no_receipt():
    r = _run("playbook_outputs", _ctx("Done.", skill=_skill(FM_REPORT_ASK),
                                      receipts=_receipts(asks=1)))
    assert (r["status"], r["reason"]) == ("fail", "declared_output_missing")
    assert r["evidence"]["missing"] == ["report"]


def test_a_window_matched_report_is_unattributable_never_a_pass():
    r = _run("playbook_outputs", _ctx("Done.", skill=_skill(FM_REPORT_ASK),
                                      receipts=_receipts(asks=1, reports=1)))
    assert (r["status"], r["reason"]) == ("skip", "unattributable")
    assert r["evidence"]["unattributable"] == ["report"]


@pytest.mark.parametrize("ctx_kw, reason", [
    (dict(message="summarise the market"), "not_a_playbook"),
    (dict(skill=None), "contract_unresolved"),
    (dict(skill={"status": "not_found"}), "contract_unresolved"),
    (dict(skill={"status": "unreadable", "error": "OSError"}), "skill_unreadable"),
    (dict(skill=_skill("---\nname: x\n---\n")), "no_declared_outputs"),
    (dict(skill=_skill("---\noutputs: [file]\n---\n")), "no_checkable_outputs"),
])
def test_playbook_outputs_skips_name_their_reason(ctx_kw, reason):
    r = _run("playbook_outputs", _ctx("Done.", **ctx_kw))
    assert (r["status"], r["reason"]) == ("skip", reason)


def test_an_unresolved_playbook_fails():
    """#1410: the runtime answered 'Unknown command' — the playbook never ran."""
    r = _run("playbook_outputs", _ctx("Unknown command: /weekly", skill=_skill(FM_REPORT_ASK)))
    assert (r["status"], r["reason"]) == ("fail", "playbook_unresolved")


def test_a_run_that_says_what_it_did_not_do_is_a_skip_not_a_fail():
    """The fleet's honest-abort footer: the run itself declared the work missing."""
    reply = "Could not reach the CRM.\nNot done: the weekly report\nDeviations: none"
    r = _run("playbook_outputs", _ctx(reply, skill=_skill(FM_REPORT_ASK)))
    assert (r["status"], r["reason"]) == ("skip", "declared_not_done")


def test_not_done_none_is_not_an_abort():
    reply = "Published.\nNot done: none\nDeviations: none"
    r = _run("playbook_outputs", _ctx(reply, skill=_skill(FM_REPORT_ASK)))
    assert r["status"] == "fail"


def test_playbook_outputs_evidence_carries_no_skill_text():
    secret_skill = FM_REPORT_ASK + f"\nkey {FAKE_PAT}\n"
    r = _run("playbook_outputs", _ctx("Done.", skill=_skill(secret_skill)))
    assert FAKE_PAT not in json.dumps(r) and "Weekly" not in json.dumps(r)


# --- budget_vs_baseline ---------------------------------------------------------------

def _series(n, cost=1.0, duration_ms=60_000, tool_call_count=10, context_used=50_000):
    return [{"cost": cost + i * 0.01, "duration_ms": duration_ms + i * 100,
             "tool_call_count": tool_call_count + (i % 3), "context_used": context_used + i}
            for i in range(n)]


def test_budget_passes_inside_the_band():
    r = _run("budget_vs_baseline", _ctx(series=_series(20), cost=1.05, duration_ms=60_500,
                                        tool_call_count=11, context_used=50_010))
    assert r["status"] == "pass"
    assert r["evidence"]["n"] == 20
    assert set(r["evidence"]["metrics"]) == {"cost", "duration_ms", "tool_call_count",
                                              "context_used"}


def test_budget_fails_outside_the_band_and_names_the_metric():
    r = _run("budget_vs_baseline", _ctx(series=_series(20), cost=9.0, duration_ms=60_500))
    assert (r["status"], r["reason"]) == ("fail", "outside_baseline")
    assert r["evidence"]["outside"] == ["cost"]
    band = r["evidence"]["metrics"]["cost"]
    assert band["observed"] == 9.0 and band["p5"] <= band["p95"] < 9.0


def test_a_run_far_below_the_band_is_flagged_too():
    r = _run("budget_vs_baseline", _ctx(series=_series(20), duration_ms=500))
    assert r["status"] == "fail" and r["evidence"]["outside"] == ["duration_ms"]


@pytest.mark.parametrize("series", [[], _series(4)])
def test_budget_skips_on_insufficient_history(series):
    r = _run("budget_vs_baseline", _ctx(series=series, cost=1.0))
    assert (r["status"], r["reason"]) == ("skip", "insufficient_history")
    assert r["evidence"]["n"] == len(series)


def test_null_fields_are_left_out_not_read_as_zero():
    history = _series(10)
    for h in history:
        h["cost"] = None                                   # cost never recorded
    r = _run("budget_vs_baseline", _ctx(series=history, cost=None, duration_ms=60_400,
                                        tool_call_count=None, context_used=None))
    assert r["status"] == "pass"
    assert set(r["evidence"]["metrics"]) == {"duration_ms"}


def test_no_comparable_metric_is_a_skip():
    r = _run("budget_vs_baseline", _ctx(series=_series(10)))   # run recorded nothing
    assert (r["status"], r["reason"]) == ("skip", "no_comparable_metrics")


def test_unreadable_history_is_a_skip():
    r = _run("budget_vs_baseline", _ctx(series=None, cost=1.0))
    assert (r["status"], r["reason"]) == ("skip", "history_unreadable")


# --- headline (TD-7) -------------------------------------------------------------------

def _result(cid, tier, status):
    return {"id": cid, "tier": tier, "source": "platform", "status": status, "evidence": {}}


def test_the_headline_counts_gates_only():
    h = t0.headline([_result("a", "gate", "pass"), _result("b", "gate", "skip"),
                     _result("c", "signal", "fail")])
    assert h == {"passed": True, "gate_pass": 1, "gate_fail": 0, "gate_skip": 1}


def test_one_gate_fail_fails_the_headline():
    h = t0.headline([_result("a", "gate", "pass"), _result("b", "gate", "fail")])
    assert h["passed"] is False and h["gate_fail"] == 1


# --- end to end: series read, skill lookup, headline, the null tests ---------------------

def _ago(seconds):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ")


def _seed(exec_id, **cols):
    from sqlalchemy import insert
    from db.engine import get_engine
    from db.tables import schedule_executions
    values = dict(id=exec_id, schedule_id="sched-1", agent_name="agent-a",
                  status="success", triggered_by="schedule", message="/weekly",
                  response="Done", started_at=_ago(300), completed_at=_ago(120))
    values.update(cols)
    with get_engine().begin() as conn:
        conn.execute(insert(schedule_executions).values(**values))


def _seed_history(n, **cols):
    for i in range(n):
        _seed(f"hist-{i}", started_at=_ago(10_000 + i * 600), completed_at=_ago(9_900 + i * 600),
              cost=1.0 + i * 0.01, duration_ms=60_000 + i * 100, context_used=50_000 + i,
              tool_calls=json.dumps([{"type": "tool_use", "tool": "Read"}] * (10 + i % 3)),
              **cols)


@pytest.fixture
def stub_world(monkeypatch):
    monkeypatch.setattr(t0, "_staged_values", lambda: [])
    holder = {"skill": _skill(FM_REPORT_ASK)}
    monkeypatch.setattr(t0, "_load_skill", lambda ref: holder["skill"])
    return holder


def _checks(row):
    return {c["id"]: c for c in row["checks"]["checks"]}


def test_recent_series_metrics_reads_the_same_schedule_only(db_backend):
    from database import db
    _seed_history(6)
    _seed("other-sched", schedule_id="sched-2", cost=99.0, completed_at=_ago(5000))
    _seed("failed-run", status="failed", cost=50.0, completed_at=_ago(5000))
    _seed("ex-now", cost=1.02)
    rows = db.recent_series_metrics("ex-now")
    assert len(rows) == 6
    assert all(r["cost"] < 2 for r in rows)
    assert {r["tool_call_count"] for r in rows} <= {10, 11, 12}


def test_recent_series_metrics_for_an_agent_trigger_keys_on_the_playbook(db_backend):
    from database import db
    for i in range(5):
        _seed(f"a-{i}", schedule_id="__manual__", triggered_by="agent", message="/weekly now",
              cost=1.0, completed_at=_ago(5000 + i))
    _seed("a-other", schedule_id="__manual__", triggered_by="agent", message="/daily", cost=7.0,
          completed_at=_ago(5000))
    _seed("a-now", schedule_id="__manual__", triggered_by="agent", message="/weekly")
    rows = db.recent_series_metrics("a-now")
    assert len(rows) == 5 and {r["cost"] for r in rows} == {1.0}


def test_a_signal_failure_never_moves_the_headline(db_backend, stub_world):
    """TD-7 live: the run is wildly over budget, every gate is clean."""
    stub_world["skill"] = _skill("---\noutputs: [ask]\n---\n")
    _seed_history(10)
    _seed("ex-now", cost=50.0, response="Raised the approval.")
    from sqlalchemy import insert
    from db.engine import get_engine
    from db.tables import operator_queue
    with get_engine().begin() as conn:
        conn.execute(insert(operator_queue).values(
            id="oq-1", agent_name="agent-a", request_id="r-1", status="pending", title="t",
            question="q", type="approval", execution_id="ex-now", created_at=_ago(200)))
    row = t0.evaluate_execution("ex-now")
    checks = _checks(row)
    assert checks["budget_vs_baseline"]["status"] == "fail"
    assert checks["playbook_outputs"]["status"] == "pass"
    assert row["checks"]["headline"]["passed"] is True


def test_the_skill_lookup_failing_is_a_skip_and_the_row_is_written(db_backend, monkeypatch):
    monkeypatch.setattr(t0, "_staged_values", lambda: [])

    import services.skill_service as ss

    def broken(name):
        raise OSError("clone gone")
    monkeypatch.setattr(ss.skill_service, "get_skill", broken)
    _seed("ex-now")
    row = t0.evaluate_execution("ex-now")
    po = _checks(row)["playbook_outputs"]
    assert (po["status"], po["reason"]) == ("skip", "skill_unreadable")
    assert po["evidence"] == {"error": "OSError"}


def test_null_tests_do_nothing_and_claim_everything_score_below_an_honest_abort(
        db_backend, stub_world):
    from sqlalchemy import insert
    from db.engine import get_engine
    from db.tables import operator_queue
    # 1. do-nothing: the playbook ran, said nothing, produced nothing.
    _seed("do-nothing", response="ok")
    # 2. claims-everything-done: says it published and filed it all, no receipt.
    _seed("claims-done", response="Done. Published the weekly report and filed the approval.")
    # 3. honest abort: says what it could not do and raised an ask about it.
    _seed("honest-abort", response=("Could not reach the CRM, so I stopped.\n"
                                    "Not done: the weekly report\nDeviations: none"))
    with get_engine().begin() as conn:
        conn.execute(insert(operator_queue).values(
            id="oq-abort", agent_name="agent-a", request_id="r-a", status="pending",
            title="CRM down", question="q", type="alert", execution_id="honest-abort",
            created_at=_ago(200)))

    heads = {eid: t0.evaluate_execution(eid)["checks"]["headline"]
             for eid in ("do-nothing", "claims-done", "honest-abort")}
    assert heads["do-nothing"]["passed"] is False
    assert heads["claims-done"]["passed"] is False
    assert heads["honest-abort"]["passed"] is True
    assert heads["honest-abort"]["gate_fail"] == 0
    for bad in ("do-nothing", "claims-done"):
        assert heads[bad]["gate_fail"] >= 1
