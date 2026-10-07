"""trinity-enterprise#794 — the system chain report's verdicts.

The chains run against a release candidate; their report is the go / no-go
evidence attached to #783. What these tests pin is the one rule the AC states
twice: **a chain that did not run is never reported as passed** — not a skip,
not a chain with a step it could not run, not a test that recorded no step.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_PATH = Path(__file__).resolve().parents[1] / "system_chains" / "chain_report.py"
_spec = importlib.util.spec_from_file_location("chain_report_under_test", _PATH)
cr = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = cr  # dataclasses resolve their module by name
_spec.loader.exec_module(cr)


class Skipped(Exception):
    """Stands in for pytest's Skipped, which the module matches by name."""


def _run(*steps):
    run = cr.ChainRun("J99", "A test chain")
    for name, fails in steps:
        if fails:
            with pytest.raises(AssertionError):
                with run.step(name):
                    raise AssertionError(f"{name} broke")
        else:
            with run.step(name):
                pass
    return run


def test_every_step_passing_is_passed():
    v = cr.verdict(_run(("a", False), ("b", False)), outcome="passed")
    assert (v["verdict"], v["step"]) == ("passed", None)


def test_a_failing_step_is_failed_at_that_step_by_name():
    v = cr.verdict(_run(("deploy", False), ("skills reach every agent", True)), outcome="failed")
    assert v["verdict"] == "failed"
    assert v["step"] == "skills reach every agent"
    assert "skills reach every agent broke" in v["reason"]
    assert [s["status"] for s in v["steps"]] == ["passed", "failed"]


def test_a_skip_is_not_run_with_its_reason_never_passed():
    v = cr.verdict(cr.ChainRun("J15", "t"), outcome="skipped",
                   skip_reason=cr.NOT_RUN_PREFIX + "blocked by ent#793")
    assert (v["verdict"], v["reason"]) == ("not_run", "blocked by ent#793")


def test_a_skip_raised_inside_a_step_marks_that_step_not_run():
    run = cr.ChainRun("J18", "t")
    with pytest.raises(Skipped):
        with run.step("read the agent's key"):
            raise Skipped(cr.NOT_RUN_PREFIX + "no docker socket")
    assert run.steps[0].status == "not_run" and run.steps[0].detail == "no docker socket"
    assert cr.verdict(run, outcome="skipped", skip_reason=cr.NOT_RUN_PREFIX + "no docker socket")["verdict"] == "not_run"


def test_a_step_that_could_not_run_makes_it_partial_never_passed():
    run = _run(("card shows the number", False))
    run.not_run_step("the brief shows it", "no platform record")
    v = cr.verdict(run, outcome="passed")
    assert (v["verdict"], v["step"], v["reason"]) == ("partial", "the brief shows it", "no platform record")


def test_a_pass_with_no_step_recorded_is_a_failure():
    v = cr.verdict(cr.ChainRun("J99", "t"), outcome="passed")
    assert v["verdict"] == "failed" and "no step" in v["reason"]


def test_a_failure_outside_any_step_is_failed_with_the_error():
    v = cr.verdict(_run(("a", False)), outcome="failed", error="fixture setup broke")
    assert (v["verdict"], v["step"], v["reason"]) == ("failed", None, "fixture setup broke")


def test_the_summary_is_a_go_only_when_every_chain_passed():
    passed = cr.verdict(_run(("a", False)), outcome="passed")
    partial_run = _run(("a", False))
    partial_run.not_run_step("b", "x")
    partial = cr.verdict(partial_run, outcome="passed")
    not_run = cr.verdict(cr.ChainRun("J2", "t"), outcome="skipped", skip_reason="x")
    assert cr.summary([passed])["all_passed"] is True
    assert cr.summary([passed, partial])["all_passed"] is False
    assert cr.summary([passed, not_run])["all_passed"] is False
    assert cr.summary([])["all_passed"] is False, "an empty run is not evidence"


def test_the_reports_carry_every_chain_and_say_why_it_is_not_a_go():
    results = [cr.verdict(_run(("a", False)), outcome="passed"),
               cr.verdict(cr.ChainRun("J15", "Roll-out"), outcome="skipped",
                          skip_reason=cr.NOT_RUN_PREFIX + "blocked | by ent#793")]
    body = json.loads(cr.to_json(results, {"version": "1.0.0-rc1"}))
    assert body["summary"]["counts"] == {"passed": 1, "failed": 0, "not_run": 1, "partial": 0}
    assert [c["chain"] for c in body["chains"]] == ["J15", "J99"]
    md = cr.to_markdown(results, {"version": "1.0.0-rc1"})
    assert "Not a go" in md and "1.0.0-rc1" in md
    assert "blocked \\| by ent#793" in md, "a pipe in a reason must not break the table"
