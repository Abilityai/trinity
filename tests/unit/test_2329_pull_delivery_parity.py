"""Pull sink honours the post-turn delivery settings push applies (#2329).

A queued `/task` row carries delivery instructions in `backlog_metadata`
(`save_to_session`, `chat_session_id`, `user_message`, `create_new_session`,
`inject_result`, plus the collaboration / self-task activity ids). The push
drain applies them in `run_async_task`; the pull sink `apply_task_result` must
apply the same ones through the same helper, or a pulled Chat-tab turn succeeds
and never reaches the session.

Also pinned here:
  * a duplicate SUCCESS report under the same claim token wins the CAS once,
    so delivery (which is not idempotent) runs once;
  * the collaboration / self-task activity a queued row carries is closed by
    the single activity-close owner, so a terminal written outside the sink
    (lease-reaper park, expire, watchdog) does not leave it `started`.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

_BACKEND = str(Path(__file__).resolve().parent.parent.parent / "src" / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from db_harness import db_backend  # noqa: E402,F401  (fixture)

# Sibling import (the unit dir is not implicitly importable) — reuse the
# pull-endpoint DB harness rather than fork it.
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_1081_pull_endpoints import (  # noqa: E402,F401  (fixtures)
    _restore_sys_modules,
    enqueue,
    seed_agent,
    tmp_db,
)

pytestmark = pytest.mark.unit

AGENT = "alpha"
USER_ID = 7
USER_EMAIL = "owner@example.com"


def _meta(**overrides) -> str:
    meta = {
        "message": "context + hi",
        "save_to_session": True,
        "user_message": "hi",
        "create_new_session": False,
        "chat_session_id": None,
        "inject_result": False,
        "user_id": USER_ID,
        "user_email": USER_EMAIL,
        "collaboration_activity_id": None,
        "is_self_task": False,
        "self_task_activity_id": None,
    }
    meta.update(overrides)
    return json.dumps(meta)


def _claim():
    from database import db

    row = db.claim_next_queued(AGENT, worker_id="w1", lease_seconds=900)
    return row["id"], row["claim_token"]


async def _settle():
    from services import pull_coordination_service as pcs

    while pcs._inflight_delivery_tasks:
        await asyncio.gather(*list(pcs._inflight_delivery_tasks), return_exceptions=True)


def _messages(session_id):
    from database import db

    return [(m.role, m.content) for m in db.get_chat_messages(session_id)]


def _own_session_id():
    from database import db

    return db.get_or_create_chat_session(AGENT, USER_ID, USER_EMAIL).id


def _collab_activity(execution_id=None):
    from database import db
    from models import ActivityCreate, ActivityType

    return db.create_activity(ActivityCreate(
        agent_name=AGENT,
        activity_type=ActivityType.AGENT_COLLABORATION,
        triggered_by="agent",
        related_execution_id=execution_id,
    ))


@pytest.fixture
def waiter():
    from services import sync_waiter

    created = []

    def _register(eid):
        fut = asyncio.get_running_loop().create_future()
        sync_waiter._sync_waiters[eid] = fut
        created.append(eid)
        return fut

    yield _register
    for eid in created:
        sync_waiter._sync_waiters.pop(eid, None)


# ---------------------------------------------------------------------------
# Chat-session delivery
# ---------------------------------------------------------------------------


class TestSessionDelivery:
    @pytest.mark.asyncio
    async def test_success_persists_turn_and_signals_session_id(
        self, seed_agent, enqueue, waiter
    ):
        from services import pull_coordination_service as pcs

        seed_agent(AGENT)
        enqueue(AGENT, backlog_metadata=_meta())
        eid, token = _claim()
        fut = waiter(eid)

        outcome = pcs.apply_task_result(eid, token, status="success", content="hello back")
        await _settle()

        assert outcome.kind == "applied"
        sid = _own_session_id()
        assert _messages(sid) == [("user", "hi"), ("assistant", "hello back")]
        assert fut.done() and fut.result()["chat_session_id"] == sid

    @pytest.mark.asyncio
    async def test_failed_turn_persists_nothing(self, seed_agent, enqueue):
        from services import pull_coordination_service as pcs

        seed_agent(AGENT)
        enqueue(AGENT, backlog_metadata=_meta())
        eid, token = _claim()

        pcs.apply_task_result(eid, token, status="failed", content="boom", error_code="runtime")
        await _settle()

        assert _messages(_own_session_id()) == []

    @pytest.mark.asyncio
    async def test_foreign_session_id_falls_back_to_callers_own(self, seed_agent, enqueue):
        from database import db
        from services import pull_coordination_service as pcs

        seed_agent(AGENT)
        foreign = db.create_new_chat_session(AGENT, 99, "other@example.com").id
        enqueue(AGENT, backlog_metadata=_meta(chat_session_id=foreign))
        eid, token = _claim()

        pcs.apply_task_result(eid, token, status="success", content="reply")
        await _settle()

        assert _messages(foreign) == []
        assert _messages(_own_session_id()) == [("user", "hi"), ("assistant", "reply")]

    @pytest.mark.asyncio
    async def test_replayed_result_does_not_persist_again(self, seed_agent, enqueue):
        from services import pull_coordination_service as pcs

        seed_agent(AGENT)
        enqueue(AGENT, backlog_metadata=_meta())
        eid, token = _claim()

        assert pcs.apply_task_result(eid, token, status="success", content="once").kind == "applied"
        await _settle()
        assert pcs.apply_task_result(eid, token, status="success", content="once").kind == "replayed"
        await _settle()

        assert len(_messages(_own_session_id())) == 2

    @pytest.mark.asyncio
    async def test_nothing_to_deliver_signals_waiter_synchronously(
        self, seed_agent, enqueue, waiter
    ):
        from services import pull_coordination_service as pcs

        seed_agent(AGENT)
        enqueue(AGENT, backlog_metadata=_meta(save_to_session=False))
        eid, token = _claim()
        fut = waiter(eid)

        pcs.apply_task_result(eid, token, status="success", content="x")

        # No await between the apply and the check: no delivery task was needed.
        assert fut.done()
        assert not pcs._inflight_delivery_tasks

    @pytest.mark.asyncio
    async def test_delivery_failure_still_signals_waiter(
        self, seed_agent, enqueue, waiter, monkeypatch
    ):
        from services import chat_execution_service as ces
        from services import pull_coordination_service as pcs

        async def _boom(**_kw):
            raise RuntimeError("delivery down")

        monkeypatch.setattr(ces, "run_post_turn_delivery", _boom)
        seed_agent(AGENT)
        enqueue(AGENT, backlog_metadata=_meta())
        eid, token = _claim()
        fut = waiter(eid)

        assert pcs.apply_task_result(eid, token, status="success", content="x").kind == "applied"
        await _settle()

        assert fut.done()


# ---------------------------------------------------------------------------
# Duplicate SUCCESS under one claim token
# ---------------------------------------------------------------------------


class TestDuplicateSuccess:
    def test_second_success_under_same_token_loses_the_cas(self, seed_agent, enqueue):
        from database import db
        from db.write_params import ExecutionResult
        from models import TaskExecutionStatus

        seed_agent(AGENT)
        enqueue(AGENT)
        eid, token = _claim()

        def _write():
            return db.update_execution_status(
                execution_id=eid,
                status=TaskExecutionStatus.SUCCESS,
                result=ExecutionResult(response="r"),
                claim_token=token,
            )

        assert _write() is True
        assert _write() is False

    def test_late_success_still_corrects_a_failed_row(self, seed_agent, enqueue):
        from database import db
        from db.write_params import ExecutionResult
        from models import TaskExecutionStatus

        seed_agent(AGENT)
        enqueue(AGENT)
        eid, token = _claim()

        assert db.update_execution_status(
            execution_id=eid, status=TaskExecutionStatus.FAILED,
            result=ExecutionResult(error="lease expired"), claim_token=token,
        )
        assert db.update_execution_status(
            execution_id=eid, status=TaskExecutionStatus.SUCCESS,
            result=ExecutionResult(response="late"), claim_token=token,
        )
        assert db.get_execution(eid).status == "success"

    @pytest.mark.asyncio
    async def test_two_success_reports_persist_once(
        self, seed_agent, enqueue, monkeypatch
    ):
        from database import db
        from services import pull_coordination_service as pcs

        seed_agent(AGENT)
        enqueue(AGENT, backlog_metadata=_meta())
        eid, token = _claim()

        # Both reports read the row while it is still running (the race the
        # worker's 15s retry opens); only the atomic CAS can tell them apart.
        running = db.get_execution(eid)
        with monkeypatch.context() as m:
            m.setattr(pcs.db, "get_execution", lambda _id: running)
            kinds = [
                pcs.apply_task_result(eid, token, status="success", content="dup").kind
                for _ in range(2)
            ]
        await _settle()

        assert kinds.count("applied") == 1
        assert len(_messages(_own_session_id())) == 2


# ---------------------------------------------------------------------------
# Collaboration / self-task activity close
# ---------------------------------------------------------------------------


class TestQueuedActivityClose:
    @pytest.mark.asyncio
    async def test_sink_success_completes_collaboration_activity(self, seed_agent, enqueue):
        from database import db
        from services import pull_coordination_service as pcs

        seed_agent(AGENT)
        collab = _collab_activity()
        enqueue(AGENT, backlog_metadata=_meta(
            save_to_session=False, collaboration_activity_id=collab,
        ))
        eid, token = _claim()

        pcs.apply_task_result(eid, token, status="success", content="12345")
        await _settle()
        await asyncio.gather(*list(_close_tasks()), return_exceptions=True)

        act = db.get_activity(collab)
        assert act["activity_state"] == "completed"
        details = act["details"]
        details = json.loads(details) if isinstance(details, str) else details
        assert details["response_length"] == 5

    @pytest.mark.asyncio
    async def test_terminal_outside_the_sink_closes_collaboration_activity(
        self, seed_agent, enqueue
    ):
        """Lease-reaper park, expire_stale, watchdog: none run the sink, all
        call the single close owner."""
        from database import db
        from db.write_params import ExecutionResult
        from models import TaskExecutionStatus
        from services.activity_service import activity_service

        seed_agent(AGENT)
        collab = _collab_activity()
        self_task = _collab_activity()
        enqueue(AGENT, backlog_metadata=_meta(
            save_to_session=False,
            collaboration_activity_id=collab,
            is_self_task=True,
            self_task_activity_id=self_task,
        ))
        eid, _token = _claim()
        db.update_execution_status(
            execution_id=eid, status=TaskExecutionStatus.FAILED,
            result=ExecutionResult(error="lease expired"),
        )

        await activity_service.close_execution_activity(
            eid, TaskExecutionStatus.FAILED, error="lease expired"
        )

        assert db.get_activity(collab)["activity_state"] == "failed"
        assert db.get_activity(self_task)["activity_state"] == "failed"

    @pytest.mark.asyncio
    async def test_queued_close_failure_never_blocks_the_dispatch_close(
        self, seed_agent, enqueue, monkeypatch
    ):
        from database import db
        from models import ActivityCreate, ActivityType, TaskExecutionStatus
        from services.activity_service import activity_service

        seed_agent(AGENT)
        eid = enqueue(AGENT, backlog_metadata=_meta(collaboration_activity_id="c1"))
        dispatch = db.create_activity(ActivityCreate(
            agent_name=AGENT, activity_type=ActivityType.CHAT_START,
            related_execution_id=eid,
        ))

        async def _boom(*_a, **_kw):
            raise RuntimeError("metadata unreadable")

        monkeypatch.setattr(activity_service, "_close_queued_activities", _boom)
        await activity_service.close_execution_activity(eid, TaskExecutionStatus.FAILED)

        assert db.get_activity(dispatch)["activity_state"] == "failed"

    def test_bulk_close_covers_collaboration_activity(self, seed_agent, enqueue):
        from database import db

        seed_agent(AGENT)
        collab = _collab_activity()
        eid = enqueue(AGENT, backlog_metadata=_meta(
            save_to_session=False, collaboration_activity_id=collab,
        ))

        db.close_open_activities_for_executions([eid], error="watchdog")

        assert db.get_activity(collab)["activity_state"] == "failed"

    @pytest.mark.asyncio
    async def test_unqueued_execution_close_is_unchanged(self, seed_agent, enqueue):
        """A row with no backlog_metadata (an admitted push turn) closes only
        its dispatch activity, exactly as before."""
        from database import db
        from models import TaskExecutionStatus
        from services.activity_service import activity_service

        seed_agent(AGENT)
        eid = enqueue(AGENT)
        assert await activity_service.close_execution_activity(
            eid, TaskExecutionStatus.SUCCESS
        ) is False
        assert db.get_execution(eid) is not None


def _close_tasks():
    from services import activity_service as mod

    return set(mod._inflight_close_tasks)


# ---------------------------------------------------------------------------
# One implementation, two callers
# ---------------------------------------------------------------------------


class TestSharedSeams:
    @pytest.mark.asyncio
    async def test_push_run_async_task_delivers_through_the_shared_helper(self, monkeypatch):
        from types import SimpleNamespace

        from models import ParallelTaskRequest, TaskExecutionStatus
        from services import chat_execution_service as ces

        result = SimpleNamespace(status=TaskExecutionStatus.SUCCESS, response="ok")

        class _Svc:
            async def execute_task(self, **_kw):
                return result

        seen = {}

        async def _spy(**kw):
            seen.update(kw)
            return "sess-1"

        signalled = []
        monkeypatch.setattr(ces, "get_task_execution_service", lambda: _Svc())
        monkeypatch.setattr(ces, "run_post_turn_delivery", _spy)
        monkeypatch.setattr(ces, "signal_sync_waiter", lambda *a: signalled.append(a))
        request = ParallelTaskRequest(message="m", save_to_session=True)

        await ces.run_async_task(
            agent_name=AGENT, request=request, execution_id="e1",
            collaboration_activity_id="c1", x_source_agent=None,
            user_id=USER_ID, user_email=USER_EMAIL,
        )

        assert seen["request"] is request and seen["result"] is result
        assert seen["collaboration_activity_id"] == "c1"
        assert signalled == [("e1", result, "sess-1")]

    @pytest.mark.asyncio
    async def test_pull_sink_delivers_through_the_shared_helper(
        self, seed_agent, enqueue, monkeypatch
    ):
        from services import chat_execution_service as ces
        from services import pull_coordination_service as pcs

        seen = {}

        async def _spy(**kw):
            seen.update(kw)

        monkeypatch.setattr(ces, "run_post_turn_delivery", _spy)
        seed_agent(AGENT)
        enqueue(AGENT, backlog_metadata=_meta(chat_session_id="s9", create_new_session=True))
        eid, token = _claim()

        pcs.apply_task_result(eid, token, status="success", content="x")
        await _settle()

        req = seen["request"]
        assert (req.save_to_session, req.user_message, req.create_new_session,
                req.chat_session_id) == (True, "hi", True, "s9")
        assert (seen["user_id"], seen["user_email"]) == (USER_ID, USER_EMAIL)
        assert seen["result"].status == "success"

    @pytest.mark.asyncio
    async def test_push_drain_rebuilds_the_request_from_metadata(self, monkeypatch):
        from services import backlog_service as bs
        from services import chat_execution_service as ces

        seen = {}

        async def _spy(**kw):
            seen.update(kw)

        monkeypatch.setattr(ces, "run_async_task", _spy)
        await bs.BacklogService()._spawn_drain(
            AGENT, "e1", json.loads(_meta(chat_session_id="s1", inject_result=True)),
        )
        await asyncio.sleep(0)

        req = seen["request"]
        assert (req.save_to_session, req.chat_session_id, req.inject_result) == (
            True, "s1", True,
        )

    @pytest.mark.asyncio
    async def test_sync_task_returns_the_session_the_pull_sink_saved_to(
        self, seed_agent, enqueue, monkeypatch
    ):
        """The sink signals ``result=None`` + chat_session_id; the sync /task
        branch rebuilds the result from the row and must keep the session id."""
        from database import db
        from db.write_params import ExecutionResult
        from models import TaskExecutionStatus
        from services import chat_execution_service as ces

        seed_agent(AGENT)
        eid = enqueue(AGENT)
        db.update_execution_status(
            execution_id=eid, status=TaskExecutionStatus.SUCCESS,
            result=ExecutionResult(response="done"),
        )

        async def _wait(_eid, timeout):
            return {"result": None, "chat_session_id": "sess-7"}

        monkeypatch.setattr(ces, "wait_for_sync_terminal", _wait)
        monkeypatch.setattr(ces.idempotency_service, "complete", lambda *a: None)

        out = await ces._dispatch_sync_backlog(
            name=AGENT, execution_id=eid, sync_effective_timeout=5, idem=None,
        )

        assert out["chat_session_id"] == "sess-7"
        assert out["response"] == "done"

    def test_request_from_metadata_round_trips_every_delivery_key(self):
        from services.backlog_service import request_from_metadata

        req = request_from_metadata(json.loads(_meta(
            chat_session_id="s1", create_new_session=True, inject_result=True,
        )))
        assert (req.save_to_session, req.user_message, req.create_new_session,
                req.chat_session_id, req.inject_result) == (True, "hi", True, "s1", True)
