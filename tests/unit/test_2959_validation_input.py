"""The validator must be handed the run it is judging (#2959).

Field report: four validated weekly schedules, 1 PASS in 12. Every FAIL was the
validator saying it had "no run output or transcript ... to inspect", recorded
as `business_status = failed_validation` on a parent that had succeeded, with a
high-priority "Validation Failed" alert each time.

Two input defects, and one missing outcome:

  * A custom `validation_prompt` REPLACED the whole auditor template. The docs
    call it "your own auditor instructions", so an operator writes
    instructions — and unless they happened to know the undocumented
    `{execution_response}` placeholder, the run's output never reached the
    validator at all.
  * A long response was cut to its first 10,000 characters, and a run's status
    line is its LAST line.
  * A validator with nothing to inspect is not a verdict about the job.
    Product decision: it is recorded as `validation_unavailable`, never files
    the "Validation Failed" alert, and can never count as a PASS.

These drive the real `ValidationService` with the DB and the agent call
replaced by recorders; the fake auditor only passes when the status line is
actually in front of it.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

STATUS_LINE = "RUN-STATUS: COMPLETE | posts=2 | errors=0"


def _vs():
    try:
        from services import validation_service
    except ImportError:  # pragma: no cover - backend venv required
        pytest.skip("backend venv required")
    return validation_service


@pytest.fixture(autouse=True)
def _no_referee():
    vs = _vs()
    original = vs.get_referee()
    vs._referee = None
    yield
    vs._referee = original


@pytest.fixture()
def harness(monkeypatch):
    """A ValidationService whose auditor PASSes only if it can see STATUS_LINE,
    with every DB write, spawned run and operator alert recorded."""
    vs = _vs()
    calls = {"status": [], "notified": [], "prompts": [], "rows": 0}

    def _update(*a, **k):
        calls["status"].append(k.get("business_status", a[1] if len(a) > 1 else None))
        return True

    def _create(**k):
        calls["rows"] += 1
        return SimpleNamespace(id="val-1")

    monkeypatch.setattr(vs.db, "update_business_status", _update)
    monkeypatch.setattr(vs.db, "create_validation_execution", _create)

    async def execute_task(**kwargs):
        prompt = kwargs["message"]
        calls["prompts"].append(prompt)
        verdict = "pass" if STATUS_LINE in prompt else "fail"
        return SimpleNamespace(
            status="success",
            error=None,
            response=f'{{"status": "{verdict}", "summary": "auditor", "items": []}}',
        )

    service = vs.ValidationService(
        task_execution_service=SimpleNamespace(execute_task=execute_task)
    )

    async def _notify(**kwargs):
        calls["notified"].append(kwargs)

    monkeypatch.setattr(service, "_notify_operator_on_failure", _notify)
    return vs, service, calls


async def _validate(service, response, custom_prompt=None):
    return await service.validate_execution(
        execution_id="e1",
        agent_name="writer",
        schedule_id="s1",
        original_message="/publish-weekly",
        execution_response=response,
        custom_prompt=custom_prompt,
    )


# ---------------------------------------------------------------------------
# (a) A long response is judged against its last line
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_long_response_validates_against_its_last_line(harness):
    vs, service, calls = harness
    response = ("drafting post body ... " * 2000) + "\n" + STATUS_LINE
    assert len(response) > 10_000

    out = await _validate(service, response)

    assert out.status is vs.ValidationStatus.PASS
    assert calls["status"][-1] == vs.BusinessStatus.VALIDATED
    assert calls["notified"] == []
    # The head is kept too — the run's opening context is still in front of it.
    assert "drafting post body" in calls["prompts"][0]


@pytest.mark.asyncio
async def test_custom_instructions_without_placeholders_still_carry_the_run(harness):
    """The field configuration: instructions, no `{execution_response}`."""
    vs, service, calls = harness
    instructions = (
        "Check that the run ended with a RUN-STATUS: COMPLETE line and that "
        "posts= is at least 1. Reply PASS or FAIL."
    )

    out = await _validate(service, "Published 2 posts.\n" + STATUS_LINE, instructions)

    assert out.status is vs.ValidationStatus.PASS
    prompt = calls["prompts"][0]
    assert instructions in prompt
    assert "/publish-weekly" in prompt
    assert calls["notified"] == []


@pytest.mark.asyncio
async def test_custom_template_with_placeholders_is_still_honoured(harness):
    vs, service, calls = harness
    template = "Task: {original_message}\nOutput:\n{execution_response}\nJudge it."

    out = await _validate(service, STATUS_LINE, template)

    assert out.status is vs.ValidationStatus.PASS
    assert calls["prompts"][0].startswith("Task: /publish-weekly\nOutput:\n" + STATUS_LINE)


@pytest.mark.asyncio
async def test_custom_prompt_with_literal_braces_does_not_crash(harness):
    """An operator pasting a JSON example used to raise from str.format before
    any validator ran, leaving the parent stuck in pending_validation."""
    vs, service, calls = harness
    template = (
        'Output:\n{execution_response}\nAnswer as {"status": "pass"|"fail"}.'
    )

    out = await _validate(service, STATUS_LINE, template)

    assert out.status is vs.ValidationStatus.PASS
    assert '{"status": "pass"|"fail"}' in calls["prompts"][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("field", [
    "{original_message[ticket]}",   # TypeError: str indices must be integers
    "{execution_response.status}",  # AttributeError on str
])
async def test_template_field_lookup_errors_do_not_crash(harness, field):
    """Any str.format failure, not only KeyError/ValueError, must fall back to
    plain substitution — a raise here leaves the parent in pending_validation
    forever (the background task only logs it)."""
    vs, service, calls = harness
    template = f"Output:\n{{execution_response}}\nAlso check {field}."

    out = await _validate(service, STATUS_LINE, template)

    assert out.status is vs.ValidationStatus.PASS
    assert field in calls["prompts"][0]


# ---------------------------------------------------------------------------
# (b) Nothing to inspect is a distinct outcome — not a failure, never a pass
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("response", ["", "   \n\t ", None])
async def test_empty_response_is_validation_unavailable(harness, response):
    vs, service, calls = harness

    out = await _validate(service, response)

    assert out.status is vs.ValidationStatus.UNAVAILABLE
    assert out.status is not vs.ValidationStatus.PASS
    assert calls["status"][-1] == "validation_unavailable"
    assert vs.BusinessStatus.FAILED_VALIDATION not in calls["status"]
    assert vs.BusinessStatus.VALIDATED not in calls["status"]
    # Decided before spawning: no blind validator run, no row, no alert.
    assert calls["prompts"] == []
    assert calls["rows"] == 0
    assert calls["notified"] == []


@pytest.mark.asyncio
async def test_empty_response_does_not_consult_the_referee(harness):
    vs, service, calls = harness

    async def referee(**kwargs):
        pytest.fail("a referee handed nothing would be judging blind too")

    vs.register_referee(referee)
    out = await _validate(service, "")

    assert out.status is vs.ValidationStatus.UNAVAILABLE
    assert calls["notified"] == []


@pytest.mark.asyncio
async def test_empty_response_files_no_operator_queue_item(monkeypatch):
    """End-to-end on the notifier itself, not a stub of it: no high-priority
    "Validation Failed" item is created for a run with nothing to inspect."""
    vs = _vs()
    created = []
    monkeypatch.setattr(vs.db, "update_business_status", lambda *a, **k: True)
    monkeypatch.setattr(vs.db, "create_operator_queue_item",
                        lambda agent, item: created.append(item))
    monkeypatch.setattr(vs.db, "create_validation_execution",
                        lambda **k: pytest.fail("no validator run for empty input"))
    service = vs.ValidationService(task_execution_service=SimpleNamespace())

    await _validate(service, "")

    assert created == []


def test_unavailable_maps_to_its_own_business_status():
    vs = _vs()
    service = vs.ValidationService(task_execution_service=SimpleNamespace())
    mapped = service._map_validation_to_business_status(vs.ValidationStatus.UNAVAILABLE)
    assert mapped == vs.BusinessStatus.VALIDATION_UNAVAILABLE == "validation_unavailable"


def test_a_validator_cannot_award_itself_unavailable():
    """`unavailable` is decided by the platform from the input, not by the
    model: a validator answering it would otherwise silence its own alert."""
    vs = _vs()
    service = vs.ValidationService(task_execution_service=SimpleNamespace())
    result = SimpleNamespace(
        status="success", error=None,
        response='{"status": "unavailable", "summary": "could not tell"}',
    )
    parsed = service._parse_validation_response(result)
    assert parsed.status is not vs.ValidationStatus.UNAVAILABLE
    assert parsed.status is not vs.ValidationStatus.PASS
