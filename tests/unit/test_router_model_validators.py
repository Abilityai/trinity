"""Validator-preservation tests for the router models relocated by #654.

The #654 move was mechanical (cut-paste each class verbatim into models.py),
and the normalized OpenAPI snapshot diff proves the *schema* is byte-identical.
But two behaviour-bearing aspects are INVISIBLE to an OpenAPI diff (Codex #3/#8):

  1. ``@field_validator`` logic — a dropped/mangled validator changes which
     inputs are rejected while the JSON schema stays the same.
  2. ``Config`` / ``Field(default_factory=...)`` — ORM-mode (``from_attributes``)
     and per-instance mutable defaults don't surface in the schema.

These pure-unit tests feed known-bad input (or check the default/config) for the
five behaviour-bearing router files (fan_out, loops, canary, audit_log,
schedules) so a careless re-paste fails loudly here, not silently in prod.

(The plan named tests/test_fan_out.py, but that file is an *integration* suite
requiring a live backend; validator preservation is a pure model concern, so it
lives here under tests/unit/ where the real models module is preloaded.)
"""
import pytest
from pydantic import ValidationError

import models


# ---------------------------------------------------------------------------
# fan_out — FanOutTask / FanOutRequest validators + moved constants
# ---------------------------------------------------------------------------

def test_fan_out_constants_moved():
    assert models.MAX_TASKS == 50
    assert models.MAX_CONCURRENCY == 10
    assert models.TASK_ID_RE.match("abc-123_X") is not None
    assert models.TASK_ID_RE.match("bad id!") is None


def test_fan_out_task_id_validator_rejects_bad_chars():
    with pytest.raises(ValidationError):
        models.FanOutTask(id="has spaces!", message="hi")
    # good id still constructs
    assert models.FanOutTask(id="task_1", message="hi").id == "task_1"


def test_fan_out_request_rejects_empty_tasks():
    with pytest.raises(ValidationError):
        models.FanOutRequest(tasks=[])


def test_fan_out_request_rejects_too_many_tasks():
    tasks = [{"id": f"t{i}", "message": "x"} for i in range(models.MAX_TASKS + 1)]
    with pytest.raises(ValidationError):
        models.FanOutRequest(tasks=tasks)


def test_fan_out_request_rejects_duplicate_ids():
    with pytest.raises(ValidationError):
        models.FanOutRequest(
            tasks=[{"id": "dup", "message": "a"}, {"id": "dup", "message": "b"}]
        )


def test_fan_out_request_rejects_bad_concurrency_and_policy_and_timeout():
    base = [{"id": "t1", "message": "x"}]
    with pytest.raises(ValidationError):
        models.FanOutRequest(tasks=base, max_concurrency=0)
    with pytest.raises(ValidationError):
        models.FanOutRequest(tasks=base, max_concurrency=models.MAX_CONCURRENCY + 1)
    with pytest.raises(ValidationError):
        models.FanOutRequest(tasks=base, policy="all-or-nothing")
    with pytest.raises(ValidationError):
        models.FanOutRequest(tasks=base, timeout_seconds=5)  # < 10
    # valid request still constructs
    ok = models.FanOutRequest(tasks=base, max_concurrency=3, policy="best-effort")
    assert ok.max_concurrency == 3


# ---------------------------------------------------------------------------
# loops — StartLoopRequest validator + Field bounds + moved constants
# ---------------------------------------------------------------------------

def test_loops_constants_moved():
    assert models.MAX_RUNS_LIMIT == 100
    assert models.MAX_MESSAGE_LEN == 100_000
    assert models.MAX_DELAY_SECONDS == 3600
    assert models.MAX_TIMEOUT_PER_RUN == 7200
    assert models.MAX_STOP_SIGNAL_LEN == 200


def test_start_loop_stop_signal_normalizer_preserved():
    # whitespace-only stop_signal collapses to None (fixed mode)
    assert models.StartLoopRequest(message="m", max_runs=1, stop_signal="   ").stop_signal is None
    # surrounding whitespace is stripped
    assert models.StartLoopRequest(message="m", max_runs=1, stop_signal="  done  ").stop_signal == "done"


def test_start_loop_max_runs_bounds_preserved():
    with pytest.raises(ValidationError):
        models.StartLoopRequest(message="m", max_runs=0)              # < 1
    with pytest.raises(ValidationError):
        models.StartLoopRequest(message="m", max_runs=models.MAX_RUNS_LIMIT + 1)
    with pytest.raises(ValidationError):
        models.StartLoopRequest(message="", max_runs=1)              # empty message


# ---------------------------------------------------------------------------
# canary / audit_log — Field(default_factory=dict) preserved (independent dicts)
# ---------------------------------------------------------------------------

def test_canary_default_factory_dicts_are_independent():
    a = models.CanaryStatsResponse(total=0)
    b = models.CanaryStatsResponse(total=0)
    assert a.by_invariant == {} and a.by_severity == {}
    a.by_invariant["S-01"] = 1
    assert b.by_invariant == {}, "default_factory must give each instance its own dict"

    v = models.CanaryViolation(
        id=1, invariant_id="S-01", tier="A", severity="critical", snapshot_time="t"
    )
    assert v.observed_state == {}


def test_audit_log_stats_default_factory_dicts_are_independent():
    a = models.AuditLogStatsResponse(total=0)
    b = models.AuditLogStatsResponse(total=0)
    assert a.by_event_type == {} and a.by_actor_type == {}
    a.by_event_type["login"] = 3
    assert b.by_event_type == {}


# ---------------------------------------------------------------------------
# schedules — Config.from_attributes preserved (ORM mode; invisible to OpenAPI)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "model_name",
    ["ScheduleResponse", "ExecutionSummary", "ExecutionResponse"],
)
def test_schedules_from_attributes_config_preserved(model_name):
    model = getattr(models, model_name)
    assert model.model_config.get("from_attributes") is True, (
        f"{model_name}.Config(from_attributes=True) was lost in the move — "
        "ORM-mode construction would silently break (not visible to OpenAPI)"
    )


# ---------------------------------------------------------------------------
# webhooks — CONTEXT_MAX_CHARS constant moved + Field bound preserved
# ---------------------------------------------------------------------------

def test_webhooks_context_max_chars_moved_and_enforced():
    assert models.CONTEXT_MAX_CHARS == 4000
    with pytest.raises(ValidationError):
        models.WebhookTriggerRequest(context="x" * (models.CONTEXT_MAX_CHARS + 1))
    assert models.WebhookTriggerRequest(context="ok").context == "ok"
