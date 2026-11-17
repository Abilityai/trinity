"""#3993 — a turn-limit failure (#3988) is never re-run by a retry path.

`max_turns` is deterministic: the same task under the same cap stops at the
same place, so every retry burns a full turn budget on the same failure.

* RETRY-001: the scheduler's `_maybe_schedule_retry` skips a row that stopped
  at its turn limit, on both dispatch paths — a push row carries the agent's
  own "Task exceeded turn limit:" text, a pull row the sink's `[max_turns]`
  prefix (`pull_coordination_service`; `schedule_executions` has no code
  column). Any other failure is still retried.
* `/task` idempotency: `_map_task_failure` COMPLETES the claim with the 422
  instead of releasing it, so a replay of the same key answers the stored 422
  (with `X-Trinity-Error-Code: max_turns` and `X-Idempotent-Replay: true`) and
  never dispatches again. Every other failure still releases the claim.

`src/scheduler` cannot import the backend, so its service is driven via
`object.__new__` with a fake DB (the `test_1823_scheduler_permanent_classification`
precedent). The idempotency half runs on the unit island's real SQLite.
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.append(str(_REPO))

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")

pytestmark = pytest.mark.unit

# The agent's own answer (headless_executor, #361), pinned against the producer
# by test_3984_3849_not_a_credential_failure.py.
_TURN_LIMIT = "Task exceeded turn limit: Reached maximum number of turns (50)"


# --------------------------------------------------------------------------- #
# 1. The scheduler's RETRY-001
# --------------------------------------------------------------------------- #


class _FakeDB:
    def __init__(self):
        self.retries = []

    def get_schedule(self, _schedule_id):
        return SimpleNamespace(id="s1", max_retries=2, retry_delay_seconds=60)

    def schedule_retry(self, execution_id, retry_at):
        self.retries.append(execution_id)


def _scheduler():
    import src.scheduler.service as scheduler_service

    svc = object.__new__(scheduler_service.SchedulerService)
    svc.db = _FakeDB()
    svc.armed = []
    svc._schedule_retry_job = lambda **kw: svc.armed.append(kw["execution"].id)
    return scheduler_service, svc


def _failed_row(error):
    from src.scheduler.models import ExecutionStatus

    return SimpleNamespace(id=f"e-{uuid.uuid4().hex[:6]}", status=ExecutionStatus.FAILED,
                           schedule_id="s1", attempt_number=1, error=error)


@pytest.mark.parametrize("error", [
    _TURN_LIMIT,                                         # push row: the agent's text
    f"[max_turns] {_TURN_LIMIT}",                        # pull row: the sink's prefix
    "[max_turns] pull terminal",                         # pull row with no text
    f"  {_TURN_LIMIT}\nTried: sub-a (auth) -> api key",  # leading space, walk trail
], ids=["push", "pull", "pull-bare", "padded"])
def test_a_turn_limit_failure_is_not_retried(error, caplog):
    _mod, svc = _scheduler()
    row = _failed_row(error)

    with caplog.at_level("INFO"):
        asyncio.run(svc._maybe_schedule_retry(execution=row, error_msg=error))

    assert svc.db.retries == [] and svc.armed == [], "a turn-limit failure was retried"
    assert any("turn limit" in r.getMessage() and row.id in r.getMessage()
               for r in caplog.records), "the skip is not logged"


@pytest.mark.parametrize("error", [
    "Execution timed out after 900s",
    "Authentication failure: 401",
    # Anchored: a failure that merely mentions the words is still retried.
    "Agent error: the upstream said 'Task exceeded turn limit: 3' in its output",
    "[timeout] Execution timed out after 900s",
])
def test_any_other_failure_is_still_retried(error):
    _mod, svc = _scheduler()
    row = _failed_row(error)

    asyncio.run(svc._maybe_schedule_retry(execution=row, error_msg=error))

    assert svc.db.retries == [row.id] and svc.armed == [row.id]


# --------------------------------------------------------------------------- #
# 2. /task idempotency — real claims on the unit island's SQLite
# --------------------------------------------------------------------------- #


def _result(error, code):
    from services.execution_envelope import TaskExecutionResult

    return TaskExecutionResult(execution_id=f"ex-{uuid.uuid4().hex[:6]}", status="failed",
                               response="", error=error, error_code=code)


def _fail_first_attempt(scope, key, result):
    from services import idempotency_service
    from services.chat_execution_service import ChatDispatchError, _map_task_failure

    first = idempotency_service.begin(scope, key)
    assert not first.replay, "control: a fresh claim"
    with pytest.raises(ChatDispatchError) as err:
        _map_task_failure("agent1", result, idem=first)
    return err.value, idempotency_service.begin(scope, key)


def test_a_turn_limit_replay_returns_the_stored_422_without_running():
    from services.chat_execution_service import ERROR_CODE_HEADER, stored_error
    from services.dispatch_admission_service import _is_dispatch_receipt
    from services.execution_envelope import TaskExecutionErrorCode

    result = _result(_TURN_LIMIT, TaskExecutionErrorCode.MAX_TURNS)
    first, again = _fail_first_attempt("task:agent1", f"k-{uuid.uuid4().hex}", result)

    assert first.status_code == 422 and first.headers[ERROR_CODE_HEADER] == "max_turns"
    assert again.replay and not again.in_flight, "the claim was released: a replay would run again"
    assert again.execution_id == result.execution_id
    assert stored_error(again.snapshot) == {
        "status_code": 422, "detail": _TURN_LIMIT, "error_code": "max_turns"}
    assert not _is_dispatch_receipt(again.snapshot), "the #3245 receipt reclaim would re-run it"


@pytest.mark.parametrize("error,code_name", [
    ("Execution timed out after 900s", "TIMEOUT"),
    ("Authentication failure: x", "AUTH"),
    ("Task execution failed (exit code 1): x", None),
])
def test_any_other_failure_still_releases_the_claim(error, code_name):
    from services.execution_envelope import TaskExecutionErrorCode

    code = getattr(TaskExecutionErrorCode, code_name) if code_name else None
    _first, again = _fail_first_attempt("task:agent1", f"k-{uuid.uuid4().hex}", _result(error, code))

    assert not again.replay, "a retriable failure kept its claim"


# --------------------------------------------------------------------------- #
# 3. The /task route's replay branch answers the stored error
# --------------------------------------------------------------------------- #


def _call_task_route(monkeypatch, snapshot):
    from models import ParallelTaskRequest
    from routers import chat as chat_router
    from services.chat_signals import ChatAdmissionReplay

    dispatched = []

    async def _dispatch(**kwargs):
        dispatched.append(kwargs)
        return ChatAdmissionReplay(execution_id="ex-1", in_flight=False, snapshot=snapshot)

    monkeypatch.setattr(chat_router, "resolve_source_agent", lambda user, source, endpoint: source)
    monkeypatch.setattr(chat_router, "get_agent_container",
                        lambda name: SimpleNamespace(status="running"))
    monkeypatch.setattr(chat_router.chat_execution_service, "dispatch_parallel_task", _dispatch)
    user = SimpleNamespace(id=1, username="u", role="user", agent_name=None)
    coro = chat_router.execute_parallel_task(
        request=ParallelTaskRequest(message="hi"), name="agent1", current_user=user,
        x_source_agent=None, x_via_mcp=None, idempotency_key="k", x_event_trigger=None,
        x_internal_secret=None, x_trinity_execution_id=None,
    )
    return asyncio.run(coro), dispatched


def test_the_task_route_replays_the_stored_422(monkeypatch):
    from services.chat_execution_service import ERROR_CODE_HEADER

    snapshot = {"stored_error": {"status_code": 422, "detail": _TURN_LIMIT,
                                 "error_code": "max_turns"}}
    with pytest.raises(HTTPException) as err:
        _call_task_route(monkeypatch, snapshot)

    assert err.value.status_code == 422
    assert err.value.detail == _TURN_LIMIT
    assert err.value.headers[ERROR_CODE_HEADER] == "max_turns"
    assert err.value.headers["X-Idempotent-Replay"] == "true"


def test_a_success_snapshot_still_replays_as_200(monkeypatch):
    response, _ = _call_task_route(monkeypatch, {"response": "done", "task_execution_id": "ex-1"})

    assert response.status_code == 200
    assert response.headers["X-Idempotent-Replay"] == "true"
