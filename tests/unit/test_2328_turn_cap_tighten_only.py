"""#2328 (slice S2): a per-task ``max_turns`` may only TIGHTEN the agent's turn guardrail.

``POST /api/agents/{name}/task`` accepts a per-task ``max_turns`` from any caller who
can reach the agent (shared users, agents with a permission edge). The agent used
to let that value win outright, so a caller could run an agent past the
owner-set ``max_turns_task`` guardrail with any number at all. The rule now
lives where the guardrail is known — the agent's ``_setup_headless_command`` —
and resolves the cap as the lower of a valid per-task value and the guardrail;
a non-positive or non-integer value is ignored. The turn-limit 422 then names
the cap that actually stopped the turn.

A turn-limit 422 at a cap of 401 or 403 carries that number in the CLI's own
error text. #2328 left it reading as an auth failure on push; #3984 fixed that
in the backend classifier (``failure_classifier.is_turn_limit``), and the last
test below pins the fix through the per-task and the guardrail wording alike.

Driven through the REAL ``_setup_headless_command`` (argv after ``--max-turns``)
and the REAL ``_finalize_headless_result`` (the 422). The "other shape" is the
fresh install: no per-agent override was ever written, so the agent reads only
the image's baseline file, or nothing at all — and a huge per-task value must
still resolve to that default, not to itself.

Module under test: docker/base-image/agent_server/services/headless_executor.py
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException

# conftest.py preloads the real agent_server package; just import.
from agent_server.services import _runtime_config as rc  # noqa: E402
from agent_server.services import headless_executor as he  # noqa: E402

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_SHIPPED_BASELINE = _ROOT / "docker" / "base-image" / "hooks" / "guardrails-baseline.json"


def _setup(max_turns):
    return he._setup_headless_command(
        prompt="do the thing",
        model="claude-sonnet-4-6",
        allowed_tools=None,
        system_prompt=None,
        timeout_seconds=60,
        max_turns=max_turns,
        execution_id="exec-2328",
        resume_session_id=None,
        persist_session=False,
        images=None,
    )


def _argv_cap(ctx) -> int:
    cmd = ctx.cmd
    assert cmd.count("--max-turns") == 1, cmd
    return int(cmd[cmd.index("--max-turns") + 1])


def _with_guardrail(monkeypatch, value: int) -> None:
    monkeypatch.setattr(he, "_load_guardrails", lambda: {"max_turns_task": value})


# ---------------------------------------------------------------------------
# Resolution: min(valid per-task value, guardrail)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "guardrail, per_task, expected",
    [
        pytest.param(200, None, 200, id="absent-uses-guardrail"),
        pytest.param(200, 20, 20, id="tighten-honoured"),
        pytest.param(50, 50, 50, id="equal-is-the-guardrail"),
        pytest.param(50, 500, 50, id="raise-refused"),
        pytest.param(50, 0, 50, id="zero-ignored"),
        pytest.param(50, -3, 50, id="negative-ignored"),
    ],
)
def test_the_task_turn_cap_is_the_tighter_of_a_valid_per_task_value_and_the_guardrail(
    monkeypatch, guardrail, per_task, expected
):
    _with_guardrail(monkeypatch, guardrail)
    assert _argv_cap(_setup(per_task)) == expected


def test_a_caller_cannot_raise_the_owners_guardrail(monkeypatch):
    """The headline mutation line: restoring "caller value wins" sends 500 here."""
    _with_guardrail(monkeypatch, 50)
    ctx = _setup(500)
    assert _argv_cap(ctx) == 50
    assert ctx.max_turns_source == "guardrail"


def test_a_tighter_per_task_value_is_recorded_as_the_binding_cap(monkeypatch):
    _with_guardrail(monkeypatch, 200)
    ctx = _setup(20)
    assert (ctx.max_turns_effective, ctx.max_turns_source) == (20, "per_task")


@pytest.mark.parametrize("per_task", ["7", 3.5, True], ids=["str", "float", "bool"])
def test_a_non_integer_per_task_value_falls_back_to_the_guardrail(monkeypatch, caplog, per_task):
    """The pull worker hands over the claimed envelope's JSON value raw — no
    pydantic coercion as on ``/api/task`` — so the resolver itself must fail
    toward the guardrail: "7" used to raise a TypeError, and 3.5 / True reached
    argv as ``--max-turns 3.5`` / ``--max-turns True`` recorded as per-task."""
    _with_guardrail(monkeypatch, 50)
    with caplog.at_level(logging.WARNING, logger=he.logger.name):
        ctx = _setup(per_task)

    cmd = ctx.cmd
    assert cmd[cmd.index("--max-turns") + 1] == "50"
    assert (ctx.max_turns_effective, ctx.max_turns_source) == (50, "guardrail")
    assert any("non-integer per-task max_turns" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Other shape: fresh install — no override, baseline only or nothing readable
# ---------------------------------------------------------------------------


def _shipped_baseline_cap() -> int:
    return int(json.loads(_SHIPPED_BASELINE.read_text())["max_turns_task"])


@pytest.mark.parametrize("shape", ["baseline-only", "nothing-readable", "corrupt-runtime"])
def test_fresh_install_a_huge_per_task_value_resolves_to_the_default(monkeypatch, tmp_path, shape):
    """No ``guardrails-runtime.json`` override was ever written for this agent.

    The REAL ``_load_guardrails`` reads the image baseline (the shipped file), or
    finds nothing (both files missing, or the runtime file unreadable) and falls
    back to ``_DEFAULT_MAX_TURNS_TASK``. Either way 10_000 must not reach argv.
    """
    missing = tmp_path / "absent.json"
    if shape == "baseline-only":
        runtime, baseline, expected = missing, _SHIPPED_BASELINE, _shipped_baseline_cap()
    elif shape == "nothing-readable":
        runtime, baseline, expected = missing, missing, rc._DEFAULT_MAX_TURNS_TASK
    else:
        corrupt = tmp_path / "guardrails-runtime.json"
        corrupt.write_text("{not json")
        runtime, baseline, expected = corrupt, missing, rc._DEFAULT_MAX_TURNS_TASK
    monkeypatch.setattr(rc, "_GUARDRAILS_RUNTIME_PATH", str(runtime))
    monkeypatch.setattr(rc, "_GUARDRAILS_BASELINE_PATH", str(baseline))

    cap = _argv_cap(_setup(10_000))

    assert cap == expected
    assert cap != 10_000


# ---------------------------------------------------------------------------
# The 422 names the cap that stopped the turn
# ---------------------------------------------------------------------------


def _turn_limit_422(ctx, *, reader_may_be_live: bool = False, error_message: str | None = None) -> str:
    ctx.metadata.error_type = "max_turns"
    ctx.metadata.error_message = (
        error_message
        if error_message is not None
        else f"Reached maximum number of turns ({ctx.max_turns_effective})"
    )
    ctx.return_code = 1
    ctx.reader_may_be_live = reader_may_be_live
    with pytest.raises(HTTPException) as exc:
        he._finalize_headless_result(ctx)
    assert exc.value.status_code == 422
    return exc.value.detail


@pytest.mark.parametrize("reader_may_be_live", [False, True], ids=["clean-drain", "snapshot"])
def test_a_turn_stopped_by_the_per_task_cap_says_so(monkeypatch, reader_may_be_live):
    """The snapshot case pins that the binding cap survives ``_snapshot_for_finalize``."""
    _with_guardrail(monkeypatch, 50)
    detail = _turn_limit_422(_setup(20), reader_may_be_live=reader_may_be_live)

    assert "The caller's per-task max_turns" in detail
    # Raising the guardrail is not the remedy when the caller's own cap bound.
    assert "increasing max_turns_task" not in detail


@pytest.mark.parametrize("per_task", [None, 500], ids=["no-per-task", "raise-refused"])
def test_a_turn_stopped_by_the_guardrail_keeps_the_guardrail_advice(monkeypatch, per_task):
    _with_guardrail(monkeypatch, 50)
    detail = _turn_limit_422(_setup(per_task))

    assert "Consider increasing max_turns_task in guardrails" in detail
    assert "per-task" not in detail


@pytest.mark.parametrize(
    "guardrail, per_task",
    [(50, 20), (401, 20), (403, 20), (500, 401), (500, 403)],
    ids=["g50-p20", "g401-p20", "g403-p20", "g500-p401", "g500-p403"],
)
def test_the_per_task_422_wording_adds_no_number_the_auth_classifier_reads(
    monkeypatch, guardrail, per_task
):
    """Only this slice's OWN text is under test here: the CLI's error text is
    number-free, so any "401"/"403" in the detail would have come from the
    remedy wording. Neither the guardrail nor the per-task value is in it."""
    from services.failure_classifier import is_auth_failure

    _with_guardrail(monkeypatch, guardrail)
    detail = _turn_limit_422(_setup(per_task), error_message="Reached maximum number of turns")

    assert "The caller's per-task max_turns" in detail
    assert not is_auth_failure(detail)
    assert not he._is_auth_failure_message(detail)


@pytest.mark.parametrize(
    "guardrail, per_task",
    [(500, 401), (500, 403), (401, None), (403, None)],
    ids=["per-task-401", "per-task-403", "guardrail-401", "guardrail-403"],
)
def test_a_turn_limit_422_at_401_or_403_is_not_auth_on_push(
    monkeypatch, guardrail, per_task
):
    """#3984: when the cap that stopped the turn is 401 or 403, the CLI's own
    error text carries that number, and the bare "401"/"403" auth indicators
    used to read the 422 as a credential failure (a SUB-003 switch plus a
    retry). A turn-limit 422 is never a credential failure: the backend
    classifier recognises the agent's turn-limit prefix first. This is the
    inverse of #2328's known-exposure pin, and the only test that drives the
    per-task remedy wording (#3970) through the classifier."""
    from services.execution_classification import classify_switch_failure
    from services.failure_classifier import is_auth_failure, is_turn_limit

    _with_guardrail(monkeypatch, guardrail)
    detail = _turn_limit_422(_setup(per_task))
    response = httpx.Response(
        422,
        json={"detail": detail},
        request=httpx.Request("POST", "http://localhost/api/task"),
    )

    assert is_turn_limit(detail)
    assert not is_auth_failure(detail)
    assert classify_switch_failure(response) is None
