"""
Gated skills — the text a requester controls beside the request (trinity#3274, item 1).

The executor's system prompt carries the schedule's name, the MCP key's name and
the requester's email next to the request (`platform_prompt_service`
`_render_triggered_by` / `_render_schedule_line`). Each is free text the
requester sets, so the gate scans it too — a schedule named `/pay-invoice
weekly` with a neutral message used to dispatch every tick unapproved. It is
scanned as given AND as the prompt renders it (the renderer collapses `---` and
truncates with `…`, both of which can make a name appear), apart from the request
(never joined to it), shown on the approver's card when it is what matched, and
never replayed as the approved run's message.

Driven through each seam that reads it: the `execute_task` backstop (schedules),
`/task` (`chat_execution_service.dispatch_parallel_task`) and `/chat`
(`dispatch_admission_service.admit_chat_request`).
"""
import asyncio
import dataclasses
import sys
import typing
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from db_harness import db_backend  # noqa: E402,F401

sys.path.insert(0, str(Path(__file__).resolve().parent))   # `_gate_world`, as `_route_census`

import _gate_world as W  # noqa: E402
import services.skill_gate_service as _GATE  # noqa: E402
from database import db  # noqa: E402
from utils.skill_invocation import find_gated_invocations  # noqa: E402

pytestmark = pytest.mark.unit


@pytest.fixture
def world(db_backend, monkeypatch):
    return W.build(monkeypatch)


def _schedule_row(schedule_id="sched-3274"):
    from db.write_params import ExecutionSource
    return db.create_schedule_execution(schedule_id, W.FIN, "Do the weekly run", "schedule",
                                        ExecutionSource())


# ---------------------------------------------------------------------------
# The seams
# ---------------------------------------------------------------------------

def test_a_schedule_whose_name_invokes_a_gated_skill_asks_though_its_message_does_not(world):
    row = _schedule_row()
    with pytest.raises(_GATE.SkillApprovalRequired) as info:
        W.execute(message="Do the weekly run", execution_id=row.id,
                  schedule_context={"name": "/pay-invoice weekly", "cron": "0 3 * * 1"})
    record = db.get_gate_request(info.value.request_id)
    # The request is what the schedule sends; the name is context, never replayed.
    assert record["request_text"] == "Do the weekly run"
    question = W.card(info.value.request_id)["question"]
    assert f"Also sent to {W.FIN} with this request:\nSchedule name: /pay-invoice weekly" in question


def test_a_neutral_schedule_name_and_message_dispatch_as_before(world, monkeypatch):
    reached = []

    async def _stop(self, **kw):
        reached.append(kw)
        from types import SimpleNamespace
        return False, SimpleNamespace(stopped=True)

    # The class `W.execute` runs (the db fixture re-imports modules per test).
    monkeypatch.setattr(W._TES.TaskExecutionService, "_admission_gate", _stop)
    W.execute(message="Do the weekly run", execution_id=_schedule_row("sched-n").id,
              schedule_context={"name": "Weekly run", "cron": "0 3 * * 1"})
    assert reached and db.count_pending_gate_requests(W.FIN) == 0


def test_task_from_a_key_named_after_a_gated_skill_asks(world):
    principal = W.agent_key(world, mcp_key_name="/pay-invoice bot")
    with pytest.raises(_GATE.SkillApprovalRequired) as info:
        W.task(principal, message="weekly report")
    question = W.card(info.value.request_id)["question"]
    assert "MCP key name: /pay-invoice bot" in question
    assert db.get_gate_request(info.value.request_id)["request_text"] == "weekly report"


def test_chat_from_a_key_named_after_a_gated_skill_asks(world):
    with pytest.raises(_GATE.SkillApprovalRequired):
        W.chat(W.agent_key(world, mcp_key_name="/pay-invoice bot"), message="weekly report")


def test_a_requester_email_that_invokes_a_gated_skill_asks(world):
    with pytest.raises(_GATE.SkillApprovalRequired):
        W.execute(message="hello", triggered_by="public", source_user_email="/pay-invoice@example.com")


def test_the_card_carries_no_context_line_when_the_request_itself_names_the_skill(world):
    principal = W.agent_key(world, mcp_key_name="/pay-invoice bot")
    with pytest.raises(_GATE.SkillApprovalRequired) as info:
        W.task(principal, message="/pay-invoice 100 EUR")
    assert "Also sent to" not in W.card(info.value.request_id)["question"]


# ---------------------------------------------------------------------------
# What is scanned
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "run /pay---invoice",                       # the renderer collapses `---` to `-`
    "x" * 66 + " /pay-invoicexyz",              # cut at 79 chars + `…`, which NFKC reads as `...`
])
def test_the_value_is_scanned_as_the_prompt_renders_it(name):
    assert find_gated_invocations(name, {"pay-invoice"}) == []          # the raw value misses
    context = _GATE.requester_context_text(schedule_name=name)
    assert find_gated_invocations(context, {"pay-invoice"}) == ["pay-invoice"]


def test_request_and_context_are_scanned_apart_never_joined():
    """Joined, `/pay` + `-invoice` would spell a gated name neither part holds."""
    async def _go():
        return await _GATE.enforce(
            W.FIN, request_text="see /pay", context_text="-invoice",
            requester=_GATE.Requester(kind="other", key="t"), triggered_by="agent",
            gates={"pay-invoice": _GATE.SkillGate()})
    assert asyncio.run(_go()).ungated


def test_every_free_text_field_the_prompt_renders_is_scanned_or_named_platform_controlled():
    """A new `ExecutionContext` field the executor reads must be classified here:
    scanned by `requester_context_text`, or platform-controlled with a reason."""
    from services.platform_prompt_service import ExecutionContext

    scanned = {"schedule_name", "source_mcp_key_name", "source_user_email"}
    platform = {
        "agent_name": "slug, validated at creation",
        "mode": "derived from triggered_by",
        "triggered_by": "a platform trigger label",
        "source_agent_name": "an agent slug",
        "model": "a model id the platform resolved",
        "schedule_cron": "validated cron",
        "schedule_next_run": "a timestamp the scheduler computed",
        "platform_url": "platform config",
        "timestamp": "the clock",
        "execution_id": "a platform id",
        "primary_user_display": "assignment provider display name (ent#500)",
        "role_id": "an assignment id",
        "served_role_id": "a seat id from the assignment provider, sanitized like role_id (ent#814)",
        "served_person_email": "never rendered: it only picks the seat served_role_id names (ent#814)",
    }
    strings = {f.name for f in dataclasses.fields(ExecutionContext)
               if typing.get_type_hints(ExecutionContext)[f.name] == typing.Optional[str]}
    assert strings == scanned | set(platform), strings ^ (scanned | set(platform))
    context = _GATE.requester_context_text(schedule_name="/a", mcp_key_name="/b",
                                           source_email="/c@example.com")
    assert find_gated_invocations(context, {"a", "b", "c"}) == ["a", "b", "c"]
