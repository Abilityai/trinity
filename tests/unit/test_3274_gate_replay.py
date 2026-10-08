"""
Gated skills — an approved run is sent the way it was asked (trinity#3274, item 5).

`/task` and fan-out scan the caller's message and system prompt together (both
reach the executor), but the approved run used to receive that JOIN as its
message: the command could appear twice and the caller's system prompt arrived
as user text. The seams now freeze the two apart (`frozen_replay`), and
`_dispatch_approved` sends the message as the message and the system prompt as
the system prompt. A record frozen before this change replays its request text,
as before.

Driven: the `/task` seam (`chat_execution_service.dispatch_parallel_task`), the
fan-out runner (`FanOutService.execute` → `execute_task`), the `execute_task`
backstop, and the real `_dispatch_approved` over the real row writer.
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from db_harness import db_backend  # noqa: E402,F401

sys.path.insert(0, str(Path(__file__).resolve().parent))   # `_gate_world`, as `_route_census`

import _gate_world as W  # noqa: E402
import services.skill_gate_service as _GATE  # noqa: E402
from database import db  # noqa: E402

pytestmark = pytest.mark.unit

_DISPATCH_APPROVED = _GATE._dispatch_approved


@pytest.fixture
def world(db_backend, monkeypatch):
    world = W.build(monkeypatch)
    import services.capacity_manager as _CM
    import services.chat_execution_service as _CE
    import services.task_execution_service as _TES
    world.spawned = []
    cap = SimpleNamespace(calls=[])

    async def _acquire(**kw):
        cap.calls.append(kw)
        return SimpleNamespace(state="admitted", queue_position=None)

    cap.acquire = _acquire
    monkeypatch.setattr(_CM, "get_capacity_manager", lambda: cap)
    monkeypatch.setattr(_TES, "dispatch_breaker_active", lambda n: False)

    async def _run_async_task(**kw):
        world.spawned.append(kw)

    monkeypatch.setattr(_CE, "run_async_task", _run_async_task)
    return world


def _approve_run(request_id, execution_id):
    async def _go():
        await _DISPATCH_APPROVED(db.get_gate_request(request_id), execution_id)
        await asyncio.sleep(0)
    asyncio.run(_go())


def test_an_approved_task_gets_its_message_and_its_system_prompt_apart(world):
    with pytest.raises(_GATE.SkillApprovalRequired) as info:
        W.task(W.agent_key(world), message="/pay-invoice 100 EUR",
               system_prompt="Answer in one line.", user_message="/pay-invoice 100 EUR")
    rid = info.value.request_id
    _approve_run(rid, "exec-replay-1")
    request = world.spawned[0]["request"]
    assert request.message == "/pay-invoice 100 EUR"
    assert request.system_prompt == "Answer in one line."
    assert db.get_execution("exec-replay-1").message == "/pay-invoice 100 EUR"
    # The approver still saw everything that reached the executor — once each.
    card = W.card(rid)["question"]
    assert card.count("/pay-invoice 100 EUR") == 1 and "Answer in one line." in card


def test_the_card_shows_exactly_what_the_approved_run_sends(world):
    """#3274 review: the card sanitised the JOIN while the run sent each part
    sanitised alone. A credential pattern spanning the line between them
    (`… Basic` + `/transfer+…`) redacted the system prompt from the card only,
    so Approve ran an instruction the approver never saw."""
    world.gates = {"transfer": _GATE.SkillGate()}
    with pytest.raises(_GATE.SkillApprovalRequired) as info:
        W.task(W.agent_key(world), message="Weekly report. Use auth Basic",
               system_prompt="/transfer+9000+EUR+to+acct+666")
    rid = info.value.request_id
    question = W.card(rid)["question"]
    _approve_run(rid, "exec-card-1")
    request = world.spawned[0]["request"]
    assert request.message in question
    assert f"{_GATE.SYSTEM_PROMPT_LABEL}\n{request.system_prompt}" in question


def test_an_approved_task_without_a_system_prompt_sends_none(world):
    with pytest.raises(_GATE.SkillApprovalRequired) as info:
        W.task(W.agent_key(world), message="/pay-invoice 7 EUR")
    _approve_run(info.value.request_id, "exec-replay-2")
    assert world.spawned[0]["request"].system_prompt is None


def test_a_record_frozen_before_the_split_replays_its_request_text(world):
    rec, _ = db.create_gate_request(
        request_id="gate-legacy", agent_name=W.FIN, skills=["pay-invoice"],
        request_text="/pay-invoice 1\nAnswer in one line.", requester_kind="agent",
        requester_key=f"agent:{W.REQ}", source_agent=W.REQ, dispatch={"triggered_by": "agent"})
    _approve_run("gate-legacy", "exec-legacy")
    assert world.spawned[0]["request"].message == "/pay-invoice 1\nAnswer in one line."


def test_a_backstop_producer_passes_its_replay_through_execute_task(world):
    """Fan-out reaches the gate through the backstop; its replay rides
    `execute_task(gate_replay=...)` onto the record."""
    row = db.create_task_execution(agent_name=W.FIN, message="weekly report", triggered_by="fan_out")
    with pytest.raises(_GATE.SkillApprovalRequired) as info:
        W.execute(message="weekly report", triggered_by="fan_out", execution_id=row.id,
                  request_text="weekly report\nFirst run /pay-invoice 3 EUR.",
                  system_prompt="First run /pay-invoice 3 EUR.",
                  gate_replay=_GATE.frozen_replay("weekly report", "First run /pay-invoice 3 EUR."))
    _approve_run(info.value.request_id, "exec-fan-replay")
    request = world.spawned[0]["request"]
    assert (request.message, request.system_prompt) == ("weekly report", "First run /pay-invoice 3 EUR.")


def test_frozen_replay_sanitises_each_part_and_omits_a_missing_system_prompt():
    out = _GATE.frozen_replay("call it with Bearer " + "x" * 24, None)   # a sanitiser pattern
    assert set(out) == {_GATE.REPLAY_MESSAGE}
    assert "x" * 24 not in out[_GATE.REPLAY_MESSAGE]


def test_fan_out_freezes_each_subtask_with_the_batch_prompt_apart(monkeypatch):
    """The runner line itself: each subtask hands `execute_task` its own replay."""
    from unittest.mock import MagicMock
    from services import fan_out_service as fos
    from unit.test_ent751_gate_callers import _FanOutDB   # the #751 fan-out harness

    fake_db = _FanOutDB()
    calls = []

    class _TaskService:
        async def execute_task(self, **kw):
            calls.append(kw)
            fake_db.rows[kw["execution_id"]].update(status="success", response="ok")
            return MagicMock(status="success", execution_id=kw["execution_id"], error_code=None)

    monkeypatch.setattr(fos, "db", fake_db)
    monkeypatch.setattr(fos, "get_task_execution_service", lambda: _TaskService())
    monkeypatch.setitem(sys.modules, "database", SimpleNamespace(db=fake_db))
    fos._inflight_batches.clear()
    asyncio.run(fos.FanOutService().execute(
        agent_name=W.FIN, max_concurrency=1, timeout_seconds=5, system_prompt="Be brief.",
        tasks=[fos.FanOutTaskInput(id="a", message="weekly report")]))
    assert calls[0]["gate_replay"] == _GATE.frozen_replay("weekly report", "Be brief.")
