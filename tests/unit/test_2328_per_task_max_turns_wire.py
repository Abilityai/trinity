"""A per-task `max_turns` reaches the agent on the push path (trinity#2328).

`POST /api/agents/{name}/task` accepts `max_turns` (`ParallelTaskRequest`), and
the agent honours one it is sent (`headless_executor`: `--max-turns`). On the
push path the value never got there: `run_async_task` and
`_dispatch_sync_immediate` called `execute_task` without it, and `execute_task`
had no such parameter, so the agent payload carried no key and the turn ran on
the agent's guardrail. A queued row kept the value in `backlog_metadata` and the
drain rebuilt it onto the request — then dropped it at `run_async_task`.

#2317's parity guard missed this because its push side stubs `run_async_task`
and reads the rebuilt request, one hop before the hop that dropped the value.
So every proof here is measured at the wire: the payload the REAL
`TaskExecutionService.execute_task` hands to `agent_post_with_retry`.

Harness: the `test_2789_subswitch_retry_budget._run` shape (module seams of
`task_execution_service` patched, each POST recorded). The chat-layer entries
are the real `run_async_task` / `_dispatch_sync_immediate`; the backlog row is
written by the real `BacklogService.enqueue` and drained by the real
`_spawn_drain`. No `sys.modules` surgery (#762): the producer's db calls are
patched as attributes of the real `database.db`, and the dispatch seams are
patched in the globals `execute_task` actually reads (resolved from the service
the chat layer holds), so a module another test re-imported cannot detach them.
"""
from __future__ import annotations

import asyncio
import json
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from models import ParallelTaskRequest
from services.pull_coordination_service import _TASK_OVERRIDE_KEYS

from unit.test_2789_subswitch_retry_budget import (
    _SWITCHED,
    _resp_200,
    _resp_429,
    _resp_reader_race_502,
    _switch_db,
    _walk_seam,
)

pytestmark = pytest.mark.unit

AGENT = "agent-2328"
EXEC = "exec-2328"
CALLER_PROMPT = "CALLER-PROMPT-2328"


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

@contextmanager
def _wire(ce, responses=None, switch_result=None):
    """Patch the seams of the REAL `execute_task` the chat layer `ce` dispatches
    through, and yield the list of payloads it POSTs to the agent, one per
    attempt. `responses` defaults to one 200."""
    import config

    seams = type(ce.get_task_execution_service()).execute_task.__globals__

    mock_db = MagicMock()
    mock_db.get_max_parallel_tasks.return_value = 3
    mock_db.get_execution_timeout.return_value = 900
    mock_db.get_execution.return_value = MagicMock(id=EXEC, status="running")
    mock_db.update_execution_status.return_value = True  # CAS won

    capacity = MagicMock()
    capacity.acquire = AsyncMock(return_value=MagicMock(state="admitted"))
    capacity.release = AsyncMock()
    circuit = MagicMock()
    circuit.allow_request.return_value = True
    activity = MagicMock(
        track_activity=AsyncMock(return_value="act-2328"),
        complete_activity=AsyncMock(),
    )

    payloads: list[dict] = []
    queue = list(responses) if responses is not None else [_resp_200()]

    async def _post(agent_name, endpoint, payload, **kwargs):
        assert endpoint == "/api/task"
        payloads.append(dict(payload))
        if not queue:
            raise AssertionError("agent_post_with_retry called more times than responses provided")
        return queue.pop(0)

    with (
        patch.object(config, "DISPATCH_ASYNC", False),
        patch.dict(seams, {
            "db": mock_db,
            "get_capacity_manager": lambda: capacity,
            "activity_service": activity,
            "CircuitState": lambda *a, **k: circuit,
            "agent_post_with_retry": _post,
            "dispatch_breaker_active": lambda *a, **k: False,
            "_record_dispatch_terminal": AsyncMock(),
            "platform_audit_service": MagicMock(log=AsyncMock()),
            "_SWITCH_RETRY_DELAY_S": 0,
        }),
        patch("services.subscription_auto_switch.db", _switch_db()),
        patch("services.subscription_auto_switch.ensure_serviceable_subscription", AsyncMock(return_value=None)),
        patch("services.subscription_auto_switch.advance_subscription_walk", _walk_seam(switch_result)),
        patch("services.subscription_auto_switch.commit_subscription_walk", AsyncMock(return_value=None)),
        patch("services.subscription_auto_switch.notify_pool_exhausted", MagicMock()),
    ):
        yield payloads


@pytest.fixture
def chat_layer(monkeypatch):
    """The real chat-layer module with its post-turn side effects stubbed —
    they run after the dispatch under test and touch the chat-session store."""
    import importlib

    # The module the backlog drain resolves (`from services.chat_execution_service
    # import run_async_task`), so the stub below is the one the drained turn reads.
    ce = importlib.import_module("services.chat_execution_service")
    monkeypatch.setattr(ce, "run_post_turn_delivery", AsyncMock(return_value=None))
    monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", "")   # push owns the dispatch
    return ce


def _request(**kw):
    from models import ParallelTaskRequest

    kw.setdefault("message", "run the weekly recon")
    kw.setdefault("async_mode", True)
    return ParallelTaskRequest(**kw)


def _run_async_task(ce, request):
    asyncio.run(ce.run_async_task(
        agent_name=AGENT,
        request=request,
        execution_id=EXEC,
        collaboration_activity_id=None,
        x_source_agent=None,
        user_id=1,
        user_email="u@example.com",
    ))


def _enqueue(monkeypatch, request, timeout=90):
    """The `backlog_metadata` blob the REAL `BacklogService.enqueue` writes."""
    from database import db
    from services.backlog_service import BacklogService

    written = {}

    def _to_queued(execution_id, metadata, queued_at, conversation_key=None):
        written[execution_id] = metadata
        return True

    monkeypatch.setattr(db, "get_queued_count", lambda agent: 0)
    monkeypatch.setattr(db, "get_max_backlog_depth", lambda agent: 50)
    monkeypatch.setattr(db, "update_execution_to_queued", _to_queued)
    ok = asyncio.run(BacklogService().enqueue(
        agent_name=AGENT, execution_id=EXEC, request=request, effective_timeout=timeout,
        user_id=1, user_email="u@example.com", subscription_id=None, x_source_agent=None,
        triggered_by="manual", collaboration_activity_id=None,
    ))
    assert ok is True
    return json.loads(written[EXEC])


def _drain(meta):
    """Drain one queued row through the REAL `_spawn_drain` → `run_async_task`
    → `execute_task`, and wait for the spawned turn to finish."""
    from services.backlog_service import BacklogService

    async def _go():
        await BacklogService()._spawn_drain(AGENT, EXEC, meta)
        spawned = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        assert spawned, "_spawn_drain started no turn"
        for t in spawned:
            await t   # re-raises whatever the drained turn raised

    asyncio.run(_go())


# ---------------------------------------------------------------------------
# T1–T3: every /task entry point puts the caller's value on the wire
# ---------------------------------------------------------------------------

def test_async_task_puts_the_callers_max_turns_on_the_wire(chat_layer):
    with _wire(chat_layer) as payloads:
        _run_async_task(chat_layer, _request(max_turns=7))
    assert [p.get("max_turns") for p in payloads] == [7]


def test_sync_task_puts_the_callers_max_turns_on_the_wire(chat_layer):
    with _wire(chat_layer) as payloads:
        asyncio.run(chat_layer._dispatch_sync_immediate(
            request=_request(max_turns=7, async_mode=False),
            name=AGENT,
            current_user=SimpleNamespace(id=1, email="u@example.com", username="u"),
            execution_id=EXEC,
            subscription_id=None,
            triggered_by="manual",
            collaboration_activity_id=None,
            image_data=None,
            idem=SimpleNamespace(enabled=False, replay=False),
            x_source_agent=None,
        ))
    assert [p.get("max_turns") for p in payloads] == [7]


def test_a_drained_backlog_row_puts_max_turns_on_the_wire(chat_layer, monkeypatch):
    """The issue's exact path: a /task at capacity is queued, then drained."""
    meta = _enqueue(monkeypatch, _request(max_turns=7))
    assert meta["max_turns"] == 7   # the producer always kept it
    with _wire(chat_layer) as payloads:
        _drain(meta)
    assert [p.get("max_turns") for p in payloads] == [7]


# ---------------------------------------------------------------------------
# T4: both retries re-send the payload, so they keep the value
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "responses, switch_result",
    [
        pytest.param([_resp_reader_race_502(), _resp_200()], None, id="reader-race-678"),
        pytest.param([_resp_429(), _resp_200()], _SWITCHED, id="subscription-switch-sub003"),
    ],
)
def test_both_retries_keep_the_callers_max_turns(responses, switch_result, chat_layer):
    with _wire(chat_layer, responses, switch_result) as payloads:
        asyncio.run(chat_layer.get_task_execution_service().execute_task(
            agent_name=AGENT, message="hello", triggered_by="manual",
            execution_id=EXEC, timeout_seconds=900, max_turns=7,
        ))
    assert len(payloads) == 2, "the retry never fired — the case proves nothing"
    assert [p.get("max_turns") for p in payloads] == [7, 7]


# ---------------------------------------------------------------------------
# T5: push wire and pull envelope apply the same per-task settings
# ---------------------------------------------------------------------------

def _claim_overrides(meta):
    """`task_overrides` the PULL claim serves for this row, with the platform
    prompt composition reduced to the caller's prompt (#2317's convention)."""
    from services import pull_coordination_service as pcs

    row = {
        "id": EXEC, "agent_name": AGENT, "message": meta["message"],
        "backlog_metadata": json.dumps(meta), "triggered_by": "manual",
        "model_used": None, "lease_expires_at": None, "claim_token": "t",
        "claimed_by_worker": "w", "redelivery_count": 0,
    }
    with patch.object(pcs, "_resolve_agent_runtime", return_value="claude-code"), \
         patch.object(pcs, "is_execution_context_enabled", return_value=False), \
         patch.object(pcs, "compose_system_prompt", side_effect=lambda **kw: kw["caller_prompt"]):
        return pcs._build_claim_response(row)["envelope"]["payload"]["task_overrides"]


@pytest.mark.parametrize("key", _TASK_OVERRIDE_KEYS)
def test_push_wire_and_pull_envelope_apply_the_same_per_task_settings(key, chat_layer, monkeypatch):
    """Derived from the claim's own key list, so the next per-task setting the
    pull envelope learns is wire-checked on the push side automatically — the
    structural guard #2317 needed and stopped one hop short of."""
    meta = _enqueue(monkeypatch, _request(
        model="opus", allowed_tools=["Read", "mcp__trinity__report"],
        system_prompt=CALLER_PROMPT, timeout_seconds=90, max_turns=7,
    ))
    with _wire(chat_layer) as payloads:
        _drain(meta)
    push = payloads[0].get(key)
    pull = _claim_overrides(meta).get(key)
    if key == "system_prompt":
        # Both sides compose the platform prompt around the caller's; what must
        # survive on each is the caller's own text.
        assert CALLER_PROMPT in (push or "") and CALLER_PROMPT in (pull or "")
    else:
        assert meta[key] is not None, f"the fixture left {key} unset — the case proves nothing"
        assert push == pull == meta[key]


# ---------------------------------------------------------------------------
# O1 — the other shape: empty data
# ---------------------------------------------------------------------------

def _approved_run_from(record, monkeypatch):
    """Start an approved gate run from `record` through the REAL
    `_dispatch_approved` → `run_async_task` → `execute_task`, and wait for it."""
    import importlib

    cm = importlib.import_module("services.capacity_manager")
    gate = importlib.import_module("services.skill_gate_service")
    tes = importlib.import_module("services.task_execution_service")
    monkeypatch.setattr(tes, "dispatch_breaker_active", lambda agent: False)

    monkeypatch.setattr(gate.db, "create_task_execution", lambda **kw: SimpleNamespace(id=EXEC))
    monkeypatch.setattr(gate.db, "get_execution_timeout", lambda agent: 900)
    monkeypatch.setattr(gate.db, "get_max_parallel_tasks", lambda agent: 3)
    capacity = SimpleNamespace(
        acquire=AsyncMock(return_value=SimpleNamespace(state="admitted", queue_position=None)))
    monkeypatch.setattr(cm, "get_capacity_manager", lambda: capacity)

    async def _go():
        assert await gate._dispatch_approved(record, EXEC) == "admitted"
        await asyncio.gather(*list(gate._inflight))

    asyncio.run(_go())


@pytest.mark.parametrize("shape", ["no-value", "pre-fix-backlog-row", "pre-fix-gate-record"])
def test_no_per_task_value_sends_none(shape, chat_layer, monkeypatch):
    """Nothing asked for a cap → nothing on the wire, so the agent applies its
    own guardrail; and the rows written before this change (a backlog row with
    no `max_turns` key, a gate record frozen without it) still run."""
    with _wire(chat_layer) as payloads:
        if shape == "no-value":
            _run_async_task(chat_layer, _request())
        elif shape == "pre-fix-backlog-row":
            meta = _enqueue(monkeypatch, _request())
            meta.pop("max_turns")
            _drain(meta)
        else:
            _approved_run_from({
                "agent_name": AGENT, "request_text": "run the weekly recon",
                "dispatch": {"triggered_by": "agent", "model": "sonnet",
                             "source_agent_name": "agent-peer"},
            }, monkeypatch)
    assert len(payloads) == 1
    assert payloads[0].get("max_turns") is None


# ---------------------------------------------------------------------------
# T6: the backend bounds and clamps the value, whatever image the agent runs
# ---------------------------------------------------------------------------
# The wire above reaches every agent as soon as the backend is upgraded, but
# the agent-side min() against the owner's guardrail reaches an agent only on
# its next cold start on a newer image. An older image lets the caller's value
# win. So the bound lives on the backend: the model refuses what no owner could
# configure, and the /task route clamps to what this owner did configure.

def test_the_request_bound_is_the_owner_guardrails_own_range():
    """Pinned to the owner-side validator, so the two ranges cannot drift."""
    from models import ParallelTaskRequest
    from routers.agent_config import _GUARDRAILS_NUMBER_BOUNDS

    lo, hi = _GUARDRAILS_NUMBER_BOUNDS["max_turns_task"]
    assert ParallelTaskRequest(message="x", max_turns=lo).max_turns == lo
    assert ParallelTaskRequest(message="x", max_turns=hi).max_turns == hi
    assert ParallelTaskRequest(message="x").max_turns is None


@pytest.mark.parametrize("value", [0, -1, 501, 10**9])
def test_an_out_of_range_max_turns_is_a_422(value):
    """0 and negatives used to reach `claude --max-turns` on an older image, and
    a large value used to override the owner's cap there. Both are refused at
    the HTTP boundary now, through the real model as FastAPI validates it. (The
    model is imported at module level: FastAPI resolves this file's string
    annotations against the module globals.)"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    seen: list[ParallelTaskRequest] = []

    @app.post("/task")
    def _task(request: ParallelTaskRequest):
        seen.append(request)
        return {}

    client = TestClient(app)
    assert client.post("/task", json={"message": "x", "max_turns": 7}).status_code == 200
    response = client.post("/task", json={"message": "x", "max_turns": value})
    assert response.status_code == 422
    assert [r.max_turns for r in seen] == [7], "an out-of-range value reached the handler"


def _route_dispatches(max_turns, guardrails):
    """Call the REAL `POST /{name}/task` handler and return the `max_turns` it
    hands to the dispatch service (the seam every push and pull path reads)."""
    import routers.chat as chat_router

    route_db = MagicMock()
    route_db.get_guardrails_config.return_value = guardrails
    route_db.get_execution_timeout.return_value = 900
    seen: list = []

    async def _dispatch(**kw):
        seen.append(kw["request"].max_turns)
        return {"status": "queued"}

    request = _request(max_turns=max_turns)
    with patch.object(chat_router, "get_agent_container", return_value=MagicMock(status="running")), \
         patch.object(chat_router, "db", route_db), \
         patch.object(chat_router.chat_execution_service, "dispatch_parallel_task", _dispatch):
        asyncio.run(chat_router.execute_parallel_task(
            request=request, name=AGENT,
            current_user=SimpleNamespace(id=1, email="u@example.com", username="u",
                                         agent_name=None, mcp_scope=None, role="user"),
            x_source_agent=None, x_via_mcp=None, idempotency_key=None,
            x_event_trigger=None, x_internal_secret=None, x_trinity_execution_id=None,
        ))
    assert len(seen) == 1, "the handler never reached the dispatch service"
    return seen[0]


@pytest.mark.parametrize(
    "requested, guardrails, dispatched",
    [
        pytest.param(400, {"max_turns_task": 20}, 20, id="above-owner-cap-clamped"),
        pytest.param(20, {"max_turns_task": 20}, 20, id="at-owner-cap-kept"),
        pytest.param(7, {"max_turns_task": 20}, 7, id="below-owner-cap-kept"),
        pytest.param(400, {}, 400, id="no-owner-cap-passes-within-bound"),
        pytest.param(None, {"max_turns_task": 20}, None, id="no-value-stays-none"),
    ],
)
def test_the_task_route_clamps_max_turns_to_the_owners_cap(requested, guardrails, dispatched):
    assert _route_dispatches(requested, guardrails) == dispatched
