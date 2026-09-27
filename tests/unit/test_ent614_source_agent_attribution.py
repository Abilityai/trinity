"""
ent#614 — ``X-Source-Agent`` is honoured only for a principal that can prove it names itself.
Related flows: docs/memory/feature-flows/activity-stream-collaboration-tracking.md,
               docs/memory/feature-flows/agent-event-subscriptions.md

Before this fix the header was a raw client value: three audit sites recorded it as the
actor (``actor_type='agent'``, the human dropped, the hash chain certifying the row), it made
``triggered_by='agent'``, and it forged an ``AGENT_COLLABORATION`` activity plus a WebSocket
edge onto an agent the caller could not access. The SELF-EXEC-001 check fired only for
agent-scoped keys, and only on ``/task``. Third occurrence of the header-keyed-arm class
after #1672 and ent#265 — so the fix is one gate, ``dependencies.resolve_source_agent``.

Layers pinned here:
  A. the helper — the matrix, the order (falsy before any getattr), the fail-closed default;
  B. ``/task`` end-to-end through the #1483 harness — a human's header is refused before any
     row, claim or activity; an agent key and the vouched loopback principal are honoured;
  C. ``/chat`` router wiring, and the ``admit_chat_request`` audit shape (owner email +
     credential carried on the agent branch);
  D. ``/fan-out`` — refusal before the idempotency claim; the replay audit row shape;
  E. loops / reminders (in ``test_1296``) / schedules — validated key first, because the MCP
     client sends no header on those paths (a header-only helper would have wiped the
     origin of every agent-created reminder);
  F. the loopback JWT — scope + vouched claim round-trip through ``get_current_user``, the
     route fence, and the claim ignored on any other token;
  G. ``trigger_subscription`` — header + claim only for ``agent_originated`` events, every
     caller states the flag, both emit endpoints state it correctly;
  H. the MCP schedules tool gates the header on agent scope like ``chat.ts``;
  I. an AST guard over the router tree plus a literal scan for the header name — a route
     added tomorrow that reads the header raw fails here.
"""
from __future__ import annotations

import ast
import asyncio
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

_REPO = Path(__file__).resolve().parents[2]
_BACKEND = _REPO / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

pytestmark = pytest.mark.unit


def _p(**over):
    """A principal shaped like ``models.User`` (every attribute present, nothing invented)."""
    base = dict(
        id=1, username="u", email="u@e.com", role="user",
        agent_name=None, vouched_source_agent=None, mcp_scope=None,
        connector_agent=None, portal_delegate=False, mcp_key_id=None, mcp_key_name=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _mock_user(agent_name=None, vouched=None, role="user"):
    """The #1483-style MagicMock principal, with the two identity attributes pinned —
    a bare MagicMock would invent a truthy ``vouched_source_agent``."""
    u = MagicMock()
    u.id = 1
    u.email = "u@e.com"
    u.username = "u"
    u.role = role
    u.agent_name = agent_name
    u.vouched_source_agent = vouched
    u.mcp_scope = "agent" if agent_name else None
    u.mcp_key_id = "k1" if agent_name else None
    u.mcp_key_name = f"{agent_name}-key" if agent_name else None
    return u


_NON_AGENT_PRINCIPALS = {
    "jwt-human": _p(),
    "admin-jwt": _p(role="admin"),
    "user-key": _p(mcp_scope="user", mcp_key_id="k"),
    "system-key": _p(mcp_scope="system", mcp_key_id="k", role="admin"),
    "ops-key": _p(mcp_scope="ops", mcp_key_id="k"),
    "connector-key": _p(mcp_scope="connector", connector_agent="agent-t"),
    "portal-delegate": _p(mcp_scope="portal_delegate", portal_delegate=True),
    "a-sixth-scope": _p(mcp_scope="something_new", mcp_key_id="k"),
}


# ===========================================================================
# A — the helper
# ===========================================================================
class TestResolveSourceAgent:
    @pytest.mark.parametrize("who", list(_NON_AGENT_PRINCIPALS) + ["agent-key", "loopback"])
    @pytest.mark.parametrize("header", [None, ""])
    def test_no_header_is_none_for_every_principal(self, who, header):
        from dependencies import resolve_source_agent

        principal = {
            **_NON_AGENT_PRINCIPALS,
            "agent-key": _p(agent_name="agent-a", mcp_scope="agent"),
            "loopback": _p(vouched_source_agent="worker-a", role="admin"),
        }[who]
        assert resolve_source_agent(principal, header, endpoint="/t") is None

    def test_agent_key_naming_itself_is_honoured(self):
        from dependencies import resolve_source_agent

        u = _p(agent_name="agent-a", mcp_scope="agent")
        assert resolve_source_agent(u, "agent-a", endpoint="/t") == "agent-a"

    def test_agent_key_naming_another_agent_is_403(self):
        from dependencies import resolve_source_agent

        u = _p(agent_name="agent-a", mcp_scope="agent")
        with pytest.raises(HTTPException) as ei:
            resolve_source_agent(u, "agent-b", endpoint="/t")
        assert ei.value.status_code == 403
        assert "doesn't match API key scope 'agent-a'" in ei.value.detail

    def test_loopback_vouched_name_is_honoured(self):
        from dependencies import resolve_source_agent

        u = _p(vouched_source_agent="worker-a", role="admin")
        assert resolve_source_agent(u, "worker-a", endpoint="/t") == "worker-a"

    def test_loopback_vouched_mismatch_is_403(self):
        from dependencies import resolve_source_agent

        u = _p(vouched_source_agent="worker-a", role="admin")
        with pytest.raises(HTTPException) as ei:
            resolve_source_agent(u, "worker-b", endpoint="/t")
        assert ei.value.status_code == 403

    @pytest.mark.parametrize("who", list(_NON_AGENT_PRINCIPALS))
    def test_every_non_agent_principal_is_refused_with_a_named_403(self, who):
        """The allowlist: only an agent key naming itself or the vouched loopback may
        name a source agent. Everyone else — including a scope no PR has invented yet —
        is refused, not trusted (the defect) and not silently ignored (the next one)."""
        from dependencies import resolve_source_agent

        with pytest.raises(HTTPException) as ei:
            resolve_source_agent(_NON_AGENT_PRINCIPALS[who], "finance-bot", endpoint="/t")
        assert ei.value.status_code == 403
        assert "agent-scoped" in ei.value.detail

    def test_falsy_header_never_touches_the_principal(self):
        """Order is load-bearing: older suites call handlers with ``x_source_agent=None``
        on bare-MagicMock principals whose every attribute is a truthy child."""
        from dependencies import resolve_source_agent

        class _Explosive:
            def __getattr__(self, name):
                raise RuntimeError(f"touched {name}")

        assert resolve_source_agent(_Explosive(), None, endpoint="/t") is None
        assert resolve_source_agent(_Explosive(), "", endpoint="/t") is None

    def test_a_principal_without_the_attributes_fails_closed(self):
        """A stand-in carrying neither ``agent_name`` nor ``vouched_source_agent`` is
        "not an agent" (refused), never "trusted" — the #2323 getattr-default rule."""
        from dependencies import resolve_source_agent

        with pytest.raises(HTTPException) as ei:
            resolve_source_agent(SimpleNamespace(id=1), "finance-bot", endpoint="/t")
        assert ei.value.status_code == 403

    def test_the_refusal_is_logged_with_the_header_escaped(self, caplog):
        from dependencies import resolve_source_agent

        with caplog.at_level("WARNING", logger="dependencies"):
            with pytest.raises(HTTPException):
                resolve_source_agent(_p(), "evil\nagent", endpoint="/api/agents/x/chat")
        assert "/api/agents/x/chat" in caplog.text
        assert "'evil\\nagent'" in caplog.text  # %r — no raw newline reaches the log line


# ===========================================================================
# B — /task through the #1483 harness
# ===========================================================================
# The canonical `/task` harness lives in the #1483 characterization module; reusing it
# means these assertions run against the same wiring that suite pins, rather than a
# second mock stack that could drift from it. Sibling import follows the precedent in
# test_2337 / test_2338 / test_2339 (the unit dir is not implicitly importable).
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_1483_execute_parallel_task_characterization import (  # noqa: E402
    _call as _task_call,
    _env as _task_env,
)


def _collab_calls(activity):
    return [
        c for c in activity.track_activity.call_args_list
        if getattr(c.kwargs.get("activity_type"), "name", None) == "AGENT_COLLABORATION"
    ]


class TestTaskRoute:
    def test_human_header_is_refused_before_any_row_claim_or_activity(self):
        from models import ParallelTaskRequest

        with _task_env() as m:
            with pytest.raises(HTTPException) as ei:
                _task_call(
                    ParallelTaskRequest(message="hi"),
                    current_user=_mock_user(agent_name=None),
                    x_source_agent="finance-bot",
                )
        assert ei.value.status_code == 403
        m["isvc"].begin.assert_not_called()               # no idempotency claim opened
        m["db"].create_task_execution.assert_not_called()  # no execution row
        m["activity"].track_activity.assert_not_called()   # no forged collaboration activity

    def test_agent_key_naming_itself_is_an_agent_call(self):
        """Unchanged behaviour for the legitimate case: the caller's own name on the
        row, ``triggered_by='agent'``, and the collaboration activity on the CALLER."""
        from models import ParallelTaskRequest

        with _task_env() as m:
            out = _task_call(
                ParallelTaskRequest(message="hi"),
                current_user=_mock_user(agent_name="agent-a"),
                x_source_agent="agent-a",
            )
        assert out["task_execution_id"] == "exec1"
        row = m["db"].create_task_execution.call_args.kwargs
        assert row["source_agent_name"] == "agent-a"
        assert row["triggered_by"] == "agent"
        collab = _collab_calls(m["activity"])
        assert len(collab) == 1 and collab[0].kwargs["agent_name"] == "agent-a"

    def test_loopback_principal_with_vouched_header_is_an_agent_call(self):
        from models import ParallelTaskRequest

        with _task_env() as m:
            _task_call(
                ParallelTaskRequest(message="hi"),
                current_user=_mock_user(agent_name=None, vouched="worker-a", role="admin"),
                x_source_agent="worker-a",
            )
        row = m["db"].create_task_execution.call_args.kwargs
        assert row["source_agent_name"] == "worker-a"
        assert row["triggered_by"] == "agent"

    def test_loopback_without_a_claim_is_an_ordinary_mcp_execution(self):
        """A human-emitted event dispatch carries neither header nor claim: the
        subscriber's task is MCP-triggered, no source agent, no phantom activity."""
        from models import ParallelTaskRequest
        from routers.chat import execute_parallel_task

        with _task_env() as m:
            asyncio.run(execute_parallel_task(
                request=ParallelTaskRequest(message="hi"),
                name="agent1",
                current_user=_mock_user(agent_name=None, role="admin"),
                x_source_agent=None,
                x_via_mcp="true",
                idempotency_key="k1",
                x_event_trigger=None,
                x_internal_secret=None,
            ))
        row = m["db"].create_task_execution.call_args.kwargs
        assert row["source_agent_name"] is None
        assert row["triggered_by"] == "mcp"
        assert _collab_calls(m["activity"]) == []


# ===========================================================================
# C — /chat wiring + the admission audit shape
# ===========================================================================
@contextmanager
def _chat_env():
    import routers.chat as chat_mod

    admit = AsyncMock(return_value=MagicMock())
    prepare = AsyncMock(return_value=MagicMock())
    run = AsyncMock(return_value={"ok": True})
    with (
        patch.object(chat_mod, "get_agent_container", return_value=MagicMock(status="running")),
        patch.object(chat_mod.dispatch_admission_service, "admit_chat_request", admit),
        patch.object(chat_mod.chat_execution_service, "prepare_chat_execution", prepare),
        patch.object(chat_mod.chat_execution_service, "run_chat_turn", run),
    ):
        yield admit, prepare, run


def _chat(user, header):
    from models import ChatMessageRequest
    from routers.chat import chat_with_agent

    return asyncio.run(chat_with_agent(
        request=ChatMessageRequest(message="hi"), name="agent1", current_user=user,
        x_source_agent=header, x_via_mcp=None, idempotency_key=None,
    ))


class TestChatRoute:
    def test_human_header_is_refused_before_admission(self):
        with _chat_env() as (admit, prepare, run):
            with pytest.raises(HTTPException) as ei:
                _chat(_mock_user(agent_name=None), "finance-bot")
        assert ei.value.status_code == 403
        admit.assert_not_awaited()
        prepare.assert_not_awaited()

    def test_agent_key_naming_itself_reaches_every_consumer_resolved(self):
        with _chat_env() as (admit, prepare, run):
            out = _chat(_mock_user(agent_name="agent-a"), "agent-a")
        assert out == {"ok": True}
        assert admit.await_args.kwargs["x_source_agent"] == "agent-a"
        assert prepare.await_args.kwargs["x_source_agent"] == "agent-a"
        assert run.await_args.kwargs["x_source_agent"] == "agent-a"

    def test_agent_key_naming_another_agent_is_refused_before_admission(self):
        with _chat_env() as (admit, _prepare, _run):
            with pytest.raises(HTTPException) as ei:
                _chat(_mock_user(agent_name="agent-a"), "agent-b")
        assert ei.value.status_code == 403
        admit.assert_not_awaited()

    def test_human_without_header_is_a_plain_user_chat(self):
        with _chat_env() as (admit, prepare, _run):
            _chat(_mock_user(agent_name=None), None)
        assert admit.await_args.kwargs["x_source_agent"] is None
        assert prepare.await_args.kwargs["x_source_agent"] is None


@contextmanager
def _admit_env():
    import services.dispatch_admission_service as das

    isvc = MagicMock()
    isvc.begin.return_value = MagicMock(replay=False)
    db = MagicMock()
    db.get_execution_timeout.return_value = 600
    db.get_max_parallel_tasks.return_value = 3
    # #2806: admission now reads the caller's running chain depth first.
    db.get_max_running_chain_depth.return_value = 0
    cap = MagicMock()
    cap.acquire = AsyncMock(return_value=MagicMock(state="admitted", queue_position=0))
    audit = MagicMock(log=AsyncMock())
    with (
        patch.object(das, "idempotency_service", isvc),
        patch.object(das, "db", db),
        patch.object(das, "get_capacity_manager", return_value=cap),
        patch.object(das, "dispatch_breaker_active", return_value=False),
        patch.object(das, "platform_audit_service", audit),
    ):
        yield audit, cap


def _admit(user, resolved_header):
    import services.dispatch_admission_service as das
    from models import ChatMessageRequest

    return asyncio.run(das.admit_chat_request(
        name="agent1", request=ChatMessageRequest(message="hi"), current_user=user,
        x_source_agent=resolved_header, x_via_mcp=None, idempotency_key=None,
    ))


class TestChatAdmissionAuditShape:
    def test_agent_branch_carries_the_agent_the_owner_and_the_credential(self):
        from models import ExecutionSource

        owner = _p(agent_name="agent-a", email="owner@e.com", mcp_scope="agent",
                   mcp_key_id="k1", mcp_key_name="agent-a-key")
        with _admit_env() as (audit, cap):
            _admit(owner, "agent-a")
        row = audit.log.await_args.kwargs
        assert row["event_action"] == "chat_started"
        assert row["actor_user"] is None and row["actor_agent_name"] == "agent-a"
        assert row["actor_email"] == "owner@e.com"     # the join back to the human (T1)
        assert row["mcp_key_id"] == "k1" and row["mcp_scope"] == "agent"
        assert cap.acquire.await_args.kwargs["source"] == ExecutionSource.AGENT
        assert cap.acquire.await_args.kwargs["source_agent"] == "agent-a"

    def test_human_branch_is_the_human(self):
        from models import ExecutionSource

        human = _p(email="alice@e.com")
        with _admit_env() as (audit, cap):
            _admit(human, None)
        row = audit.log.await_args.kwargs
        assert row["actor_user"] is human and row["actor_agent_name"] is None
        assert row["actor_email"] == "alice@e.com"
        assert cap.acquire.await_args.kwargs["source"] == ExecutionSource.USER
        assert cap.acquire.await_args.kwargs["source_agent"] is None


# ===========================================================================
# D — /fan-out
# ===========================================================================
@contextmanager
def _fanout_env(*, replay):
    import routers.fan_out as fo

    isvc = MagicMock()
    isvc.begin.return_value = MagicMock(
        replay=replay, in_flight=False, snapshot={"replayed": True}, execution_id="fo1"
    )
    audit = MagicMock(log=AsyncMock())
    svc = MagicMock()
    svc.execute = AsyncMock(return_value=MagicMock())
    with (
        patch.object(fo, "idempotency_service", isvc),
        patch.object(fo, "platform_audit_service", audit),
        patch.object(fo, "get_fan_out_service", return_value=svc),
    ):
        yield isvc, audit, svc


def _fan_out(user, header):
    from models import FanOutRequest, FanOutTask
    from routers.fan_out import fan_out

    return asyncio.run(fan_out(
        request=FanOutRequest(tasks=[FanOutTask(id="t1", message="hi")]),
        name="agent1", current_user=user, x_source_agent=header,
        x_via_mcp=None, idempotency_key="k",
    ))


class TestFanOutRoute:
    def test_human_header_is_refused_before_the_idempotency_claim(self):
        with _fanout_env(replay=True) as (isvc, audit, _svc):
            with pytest.raises(HTTPException) as ei:
                _fan_out(_mock_user(agent_name=None), "finance-bot")
        assert ei.value.status_code == 403
        isvc.begin.assert_not_called()
        audit.log.assert_not_awaited()

    def test_agent_replay_row_carries_agent_owner_and_credential(self):
        """This row used to record neither the owner nor the credential."""
        with _fanout_env(replay=True) as (_isvc, audit, _svc):
            _fan_out(_mock_user(agent_name="agent-a"), "agent-a")
        row = audit.log.await_args.kwargs
        assert row["event_action"] == "idempotent_replay"
        assert row["actor_user"] is None and row["actor_agent_name"] == "agent-a"
        assert row["actor_email"] == "u@e.com"
        assert row["mcp_key_id"] == "k1" and row["mcp_scope"] == "agent"

    def test_agent_key_naming_another_agent_is_refused(self):
        with _fanout_env(replay=True) as (isvc, _audit, _svc):
            with pytest.raises(HTTPException) as ei:
                _fan_out(_mock_user(agent_name="agent-a"), "agent-b")
        assert ei.value.status_code == 403
        isvc.begin.assert_not_called()

    def test_human_replay_row_is_the_human(self):
        with _fanout_env(replay=True) as (_isvc, audit, _svc):
            user = _mock_user(agent_name=None)
            _fan_out(user, None)
        row = audit.log.await_args.kwargs
        assert row["actor_user"] is user and row["actor_agent_name"] is None


# ===========================================================================
# E — loops and schedules (reminders: test_1296)
# ===========================================================================
def _loop(monkeypatch, user, header):
    from routers import loops as loops_router

    fake_db = MagicMock()
    fake_db.get_execution_timeout.return_value = 900
    monkeypatch.setattr(loops_router, "db", fake_db)
    service = MagicMock()
    service.start_loop = AsyncMock(return_value={"id": "loop_x", "status": "queued"})
    monkeypatch.setattr(loops_router, "get_loop_service", lambda: service)
    payload = loops_router.StartLoopRequest(message="go", max_runs=1)
    asyncio.run(loops_router.start_loop(
        payload=payload, name="a1", current_user=user, x_source_agent=header,
    ))
    return service.start_loop.await_args.kwargs["source_agent_name"]


class TestLoopsRoute:
    def test_agent_key_without_header_records_its_own_name(self, monkeypatch):
        """The MCP client sends no header on this path; before ent#614 an agent-started
        loop recorded NULL here. Validated key first."""
        assert _loop(monkeypatch, _mock_user(agent_name="a1"), None) == "a1"

    def test_agent_key_naming_itself_records_its_own_name(self, monkeypatch):
        assert _loop(monkeypatch, _mock_user(agent_name="a1"), "a1") == "a1"

    def test_human_without_header_records_nothing(self, monkeypatch):
        assert _loop(monkeypatch, _mock_user(agent_name=None), None) is None

    def test_human_with_header_is_refused(self, monkeypatch):
        with pytest.raises(HTTPException) as ei:
            _loop(monkeypatch, _mock_user(agent_name=None), "agent-x")
        assert ei.value.status_code == 403

    def test_agent_key_naming_another_agent_is_refused(self, monkeypatch):
        """The resolve call must NOT sit behind `current_user.agent_name or …`: that
        short-circuit skips the check for an agent principal, so a mismatched header
        would be silently IGNORED rather than refused — the failure mode this whole
        fix exists to end, reintroduced on the routes where the validated key wins."""
        with pytest.raises(HTTPException) as ei:
            _loop(monkeypatch, _mock_user(agent_name="a1"), "agent-x")
        assert ei.value.status_code == 403


class _FakeSchedulerClient:
    captured: dict = {}

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None, timeout=None):
        _FakeSchedulerClient.captured["json"] = json
        return SimpleNamespace(
            status_code=200, text="",
            json=lambda: {"status": "triggered", "execution_id": "e1", "schedule_name": "s"},
        )


def _trigger(user, header):
    from routers import schedules as sch

    _FakeSchedulerClient.captured.clear()
    db = MagicMock()
    db.get_schedule.return_value = SimpleNamespace(agent_name="a1")
    with (
        patch.object(sch, "db", db),
        patch.object(sch, "httpx", SimpleNamespace(AsyncClient=_FakeSchedulerClient)),
        patch.object(sch, "platform_audit_service", MagicMock(log=AsyncMock())),
    ):
        asyncio.run(sch.trigger_schedule(
            name="a1", schedule_id="s1", current_user=user, x_source_agent=header,
        ))
    return _FakeSchedulerClient.captured.get("json")


class TestScheduleTriggerRoute:
    def test_agent_key_without_header_forwards_its_own_name(self):
        assert _trigger(_mock_user(agent_name="a1"), None)["source_agent_name"] == "a1"

    def test_human_with_header_is_refused_before_the_scheduler_hop(self):
        with pytest.raises(HTTPException) as ei:
            _trigger(_mock_user(agent_name=None), "agent-x")
        assert ei.value.status_code == 403
        assert _FakeSchedulerClient.captured == {}

    def test_agent_key_naming_another_agent_is_refused(self):
        """Not short-circuited behind the validated key — see the loops twin."""
        with pytest.raises(HTTPException) as ei:
            _trigger(_mock_user(agent_name="a1"), "agent-x")
        assert ei.value.status_code == 403
        assert _FakeSchedulerClient.captured == {}

    def test_system_key_with_header_is_refused(self):
        """The strategy-review finding: the MCP schedules tool used to send the header
        for a system key too (whose principal is not agent-scoped). The backend refuses
        it; the tool no longer sends it (test H)."""
        u = _mock_user(agent_name=None, role="admin")
        u.mcp_scope = "system"
        with pytest.raises(HTTPException) as ei:
            _trigger(u, "trinity-system")
        assert ei.value.status_code == 403


# ===========================================================================
# F — the loopback JWT
# ===========================================================================
def _admin_row():
    return {"id": 1, "username": "admin", "email": "admin@e.com", "role": "admin",
            "suspended_at": None}


def _resolve_principal(token, *, method="POST", path="/api/agents/orch/task"):
    import dependencies as dep

    req = SimpleNamespace(method=method, url=SimpleNamespace(path=path))
    with patch.object(dep, "db") as db:
        db.get_user_by_username.return_value = _admin_row()
        return asyncio.run(dep.get_current_user(req, token))


class TestLoopbackToken:
    def test_token_carries_scope_and_vouched_source_and_round_trips(self):
        from config import ALGORITHM, SECRET_KEY
        from dependencies import EVENT_LOOPBACK_SCOPE
        from jose import jwt
        from services import event_dispatch_service as eds

        tok = eds._get_internal_token("worker-a")
        payload = jwt.decode(tok, SECRET_KEY, algorithms=[ALGORITHM])
        assert payload["sub"] == "admin"
        assert payload["scope"] == EVENT_LOOPBACK_SCOPE
        assert payload["source_agent"] == "worker-a"

        user = _resolve_principal(tok)
        assert user.vouched_source_agent == "worker-a"
        assert user.agent_name is None and user.mcp_scope is None
        assert user.role == "admin"

    def test_token_without_a_source_vouches_nothing(self):
        from config import ALGORITHM, SECRET_KEY
        from jose import jwt
        from services import event_dispatch_service as eds

        tok = eds._get_internal_token(None)
        assert "source_agent" not in jwt.decode(tok, SECRET_KEY, algorithms=[ALGORITHM])
        assert _resolve_principal(tok).vouched_source_agent is None

    @pytest.mark.parametrize("method,path", [
        ("GET", "/api/agents"),
        ("POST", "/api/agents/orch/chat"),
        ("DELETE", "/api/agents/orch"),
        ("GET", "/api/agents/orch/task"),
        ("POST", "/api/agents/orch/task/extra"),
    ])
    def test_token_is_fenced_to_the_subscriber_task_route(self, method, path):
        from services import event_dispatch_service as eds

        with pytest.raises(HTTPException) as ei:
            _resolve_principal(eds._get_internal_token("worker-a"), method=method, path=path)
        assert ei.value.status_code == 403

    def test_an_ordinary_admin_jwt_is_not_fenced_and_vouches_nothing(self):
        from datetime import datetime, timedelta

        from config import ALGORITHM, SECRET_KEY
        from jose import jwt

        tok = jwt.encode(
            {"sub": "admin", "exp": datetime.utcnow() + timedelta(minutes=5)},
            SECRET_KEY, algorithm=ALGORITHM,
        )
        user = _resolve_principal(tok, method="GET", path="/api/agents")
        assert user.vouched_source_agent is None and user.role == "admin"

    def test_a_source_claim_without_the_scope_is_ignored(self):
        """The claim is honoured only under the loopback scope — a `source_agent` on any
        other SECRET_KEY-signed token vouches nothing."""
        from datetime import datetime, timedelta

        from config import ALGORITHM, SECRET_KEY
        from jose import jwt

        tok = jwt.encode(
            {"sub": "admin", "source_agent": "worker-a",
             "exp": datetime.utcnow() + timedelta(minutes=5)},
            SECRET_KEY, algorithm=ALGORITHM,
        )
        assert _resolve_principal(tok, method="GET", path="/api/agents").vouched_source_agent is None


# ===========================================================================
# G — trigger_subscription and the emit endpoints
# ===========================================================================
class _LoopbackClient:
    captured: dict = {}

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        _LoopbackClient.captured["headers"] = headers
        return SimpleNamespace(status_code=200, text="")


def _dispatch(*, agent_originated, event_type="prediction.resolved"):
    from services import event_dispatch_service as eds

    _LoopbackClient.captured.clear()
    sub = SimpleNamespace(id="s1", subscriber_agent="orch", target_message="done")
    event = SimpleNamespace(id="e1", source_agent="worker-a", event_type=event_type, payload={})
    with patch("httpx.AsyncClient", _LoopbackClient):
        asyncio.run(eds.trigger_subscription(sub, event, agent_originated=agent_originated))
    return _LoopbackClient.captured["headers"]


class TestTriggerSubscription:
    def test_agent_originated_event_carries_header_and_claim(self):
        from config import ALGORITHM, SECRET_KEY
        from dependencies import EVENT_LOOPBACK_SCOPE
        from jose import jwt

        headers = _dispatch(agent_originated=True)
        assert headers["X-Source-Agent"] == "worker-a"
        assert headers["X-Via-MCP"] == "true"
        claim = jwt.decode(headers["Authorization"].split(" ", 1)[1], SECRET_KEY, algorithms=[ALGORITHM])
        assert claim["scope"] == EVENT_LOOPBACK_SCOPE and claim["source_agent"] == "worker-a"
        assert "X-Internal-Secret" not in headers  # the #1578 property survives

    def test_human_originated_event_carries_neither(self):
        from config import ALGORITHM, SECRET_KEY
        from dependencies import EVENT_LOOPBACK_SCOPE
        from jose import jwt

        headers = _dispatch(agent_originated=False)
        assert "X-Source-Agent" not in headers
        assert headers["X-Via-MCP"] == "true"
        claim = jwt.decode(headers["Authorization"].split(" ", 1)[1], SECRET_KEY, algorithms=[ALGORITHM])
        assert claim["scope"] == EVENT_LOOPBACK_SCOPE and "source_agent" not in claim

    def test_every_caller_must_state_the_flag(self):
        from services import event_dispatch_service as eds

        with pytest.raises(TypeError):
            asyncio.run(eds.trigger_subscription(SimpleNamespace(), SimpleNamespace()))


@contextmanager
def _emit_env():
    import routers.event_subscriptions as es

    db = MagicMock()
    db.find_matching_event_subscriptions.return_value = [
        SimpleNamespace(id="s1", subscriber_agent="orch", target_message="x")
    ]

    def _create(**kw):
        fields = {"id": "e1", "subscriptions_triggered": 1, "created_at": "t"}
        fields.update(kw)
        return SimpleNamespace(**fields)

    db.create_agent_event.side_effect = _create
    dispatch = MagicMock(trigger_subscription=AsyncMock())

    def _create_task(coro):
        coro.close()
        return MagicMock()

    with (
        patch.object(es, "db", db),
        patch.object(es, "event_dispatch_service", dispatch),
        patch.object(es, "_broadcast_event", AsyncMock()),
        patch.object(es, "asyncio", SimpleNamespace(create_task=_create_task)),
    ):
        yield db, dispatch


class TestEmitEndpointsStateTheFlag:
    def test_human_emit_is_not_agent_originated(self):
        from models import EmitEventRequest
        from routers.event_subscriptions import emit_event

        with _emit_env() as (db, dispatch):
            asyncio.run(emit_event(
                EmitEventRequest(event_type="prediction.resolved"), _p(username="alice")
            ))
        assert db.create_agent_event.call_args.kwargs["source_agent"] == "alice"  # unchanged
        assert dispatch.trigger_subscription.call_args.kwargs["agent_originated"] is False

    def test_agent_emit_is_agent_originated(self):
        from models import EmitEventRequest
        from routers.event_subscriptions import emit_event

        with _emit_env() as (db, dispatch):
            asyncio.run(emit_event(
                EmitEventRequest(event_type="prediction.resolved"),
                _p(agent_name="worker-a", username="worker-a", mcp_scope="agent"),
            ))
        assert db.create_agent_event.call_args.kwargs["source_agent"] == "worker-a"
        assert dispatch.trigger_subscription.call_args.kwargs["agent_originated"] is True

    def test_emit_for_agent_by_a_human_accessor_is_not_vouched(self):
        """`emit_event_for_agent` is `AuthorizedAgent`-gated — READ access, not
        ownership — so `worker-a` here is a name a human accessor CHOSE. The event row
        still records it (that is the endpoint's contract), but certifying it to the
        subscriber would hand a human `triggered_by='agent'`, an `actor_type='agent'`
        SEC-001 row and a collaboration edge on an agent they merely share: this
        issue's own primitive, re-minted one level up and signed by the backend."""
        from models import EmitEventRequest
        from routers.event_subscriptions import emit_event_for_agent

        with _emit_env() as (db, dispatch):
            asyncio.run(emit_event_for_agent(
                "worker-a", EmitEventRequest(event_type="prediction.resolved"), _p(username="alice")
            ))
        assert db.create_agent_event.call_args.kwargs["source_agent"] == "worker-a"
        assert dispatch.trigger_subscription.call_args.kwargs["agent_originated"] is False

    def test_emit_for_agent_by_a_sibling_agent_is_not_vouched(self):
        """An agent-scoped key resolves to the OWNER, so agent B can reach every agent
        its owner has — including A's emit route. B emitting as A is the same forgery."""
        from models import EmitEventRequest
        from routers.event_subscriptions import emit_event_for_agent

        with _emit_env() as (_db, dispatch):
            asyncio.run(emit_event_for_agent(
                "worker-a", EmitEventRequest(event_type="prediction.resolved"),
                _p(agent_name="worker-b", username="worker-b", mcp_scope="agent"),
            ))
        assert dispatch.trigger_subscription.call_args.kwargs["agent_originated"] is False

    def test_emit_for_agent_by_the_agent_itself_is_vouched(self):
        """The one case that IS an agent-originated event on this route."""
        from models import EmitEventRequest
        from routers.event_subscriptions import emit_event_for_agent

        with _emit_env() as (_db, dispatch):
            asyncio.run(emit_event_for_agent(
                "worker-a", EmitEventRequest(event_type="prediction.resolved"),
                _p(agent_name="worker-a", username="worker-a", mcp_scope="agent"),
            ))
        assert dispatch.trigger_subscription.call_args.kwargs["agent_originated"] is True


# ===========================================================================
# H — the MCP server sends the header only for agent scope, on every tool
# ===========================================================================
_GATED = 'authContext?.scope === "agent" ? authContext'


def test_mcp_schedules_tool_gates_the_header_on_agent_scope():
    tool = (_REPO / "src" / "mcp-server" / "src" / "tools" / "schedules.ts").read_text(encoding="utf-8")
    trigger = tool[tool.index("triggerAgentSchedule("):][:1500]
    assert _GATED in trigger, "schedules.ts must gate X-Source-Agent on scope === 'agent' like chat.ts"


def test_mcp_chat_tools_gate_the_header_on_agent_scope():
    chat = (_REPO / "src" / "mcp-server" / "src" / "tools" / "chat.ts").read_text(encoding="utf-8")
    assert chat.count(_GATED) >= 2  # chat_with_agent + fan_out


# ===========================================================================
# I — the guard: every router that reads the header routes it through the helper
# ===========================================================================
_PARAM = "x_source_agent"
_HELPER = "resolve_source_agent"
# An HTTP header read is ALWAYS the dashed spelling — FastAPI maps the
# `x_source_agent` parameter onto `X-Source-Agent`, and `request.headers` is keyed
# by the wire name. The underscore spelling as a string is therefore never a header
# read; it is a key into Trinity's own durable queue blob, pinned separately below.
_HEADER_LITERALS = {"x-source-agent"}
_LITERAL_ALLOWLIST = {
    "main.py",                              # the CORS allow_headers list
    "services/event_dispatch_service.py",   # SETS the header on the loopback
}

# The persisted backlog blob (#525/#2391): `enqueue` writes the key, `_spawn_drain`
# reads it back when the row is dispatched. Both are inside one service and both
# carry the value the ROUTER already resolved, so the blob is not a second entry
# point for the raw header — but a third consumer of that key would be, which is
# what the test below fails on.
_BLOB_KEY_SITES = {"services/backlog_service.py"}


def _is_helper_call(node) -> bool:
    return isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", None)) == _HELPER


def _param_violations(source: str, label: str) -> list[str]:
    """Every function declaring ``x_source_agent`` — nested and class-bodied ones
    included, since `tree.body` alone cannot see a handler defined inside an `if` or a
    class — must route every load of the name through the helper: as an argument of a
    ``resolve_source_agent(...)`` call, or after the rebind statement
    ``x_source_agent = resolve_source_agent(...)``.

    A resolve call sitting on the RIGHT of an ``or`` is also a violation. That is the
    ``current_user.agent_name or resolve_source_agent(...)`` short-circuit: for an agent
    principal the left side is truthy, the helper never runs, and a mismatched header is
    silently IGNORED instead of refused — the failure mode this whole fix exists to end,
    reintroduced on the routes where the validated key legitimately wins.
    """
    out = []
    tree = ast.parse(source)
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        args = fn.args
        names = [a.arg for a in args.posonlyargs + args.args + args.kwonlyargs]
        if _PARAM not in names:
            continue
        inside_helper = set()
        for call in ast.walk(fn):
            if _is_helper_call(call):
                for a in list(call.args) + [k.value for k in call.keywords]:
                    for n in ast.walk(a):
                        if isinstance(n, ast.Name) and n.id == _PARAM:
                            inside_helper.add(id(n))
        for node in ast.walk(fn):
            if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
                for operand in node.values[1:]:
                    if _is_helper_call(operand):
                        out.append(
                            f"{label}:{fn.name}:L{operand.lineno} resolve_source_agent "
                            "is short-circuited behind an `or`"
                        )
        rebound = False
        for stmt in fn.body:
            is_rebind = (
                isinstance(stmt, ast.Assign)
                and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)
                and stmt.targets[0].id == _PARAM
                and _is_helper_call(stmt.value)
            )
            for n in ast.walk(stmt):
                if isinstance(n, ast.Name) and n.id == _PARAM and isinstance(n.ctx, ast.Load):
                    if not rebound and id(n) not in inside_helper:
                        out.append(f"{label}:{fn.name}:L{n.lineno} reads the raw header")
            if is_rebind:
                rebound = True
    return out


def _docstring_nodes(tree) -> set:
    """The Constant nodes that are docstrings — prose may name the header freely."""
    out = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            out.add(id(body[0].value))
    return out


def _literal_hits(source: str, label: str) -> list[str]:
    """EVERY string constant naming the wire header — not only the ones sitting in a
    call argument or a subscript. Scoping the scan to those positions missed the alias
    form (``H = "X-Source-Agent"`` … ``request.headers.get(H)``), which is the shape a
    reader reaches for when the direct one is guarded. Docstrings are excluded so prose
    can name the header."""
    out = []
    tree = ast.parse(source)
    skip = _docstring_nodes(tree)
    for n in ast.walk(tree):
        if (
            isinstance(n, ast.Constant)
            and isinstance(n.value, str)
            and n.value.casefold() in _HEADER_LITERALS
            and id(n) not in skip
        ):
            out.append(f"{label}:L{n.lineno} {n.value!r}")
    return out


def _oss_backend_files():
    for path in sorted(_BACKEND.rglob("*.py")):
        rel = path.relative_to(_BACKEND).as_posix()
        if rel.startswith("enterprise/") or "__pycache__" in path.parts:
            continue
        yield rel, path


def test_every_router_reading_the_header_resolves_it_first():
    violations = []
    for rel, path in _oss_backend_files():
        if not rel.startswith("routers/"):
            continue
        src = path.read_text(encoding="utf-8")
        if _PARAM in src:
            violations += _param_violations(src, rel)
    assert violations == [], "\n".join(violations)


def test_the_six_readers_are_all_still_guarded():
    """The guard is only as good as its population — pin the readers it must see."""
    readers = set()
    for rel, path in _oss_backend_files():
        if not rel.startswith("routers/"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in tree.body:
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names = [a.arg for a in fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs]
                if _PARAM in names:
                    readers.add(f"{rel}::{fn.name}")
    assert readers >= {
        "routers/chat.py::chat_with_agent",
        "routers/chat.py::execute_parallel_task",
        "routers/fan_out.py::fan_out",
        "routers/loops.py::start_loop",
        "routers/reminders.py::create_reminder_endpoint",
        "routers/schedules.py::trigger_schedule",
    }, readers


def test_no_other_spelling_reads_the_header():
    hits = []
    for rel, path in _oss_backend_files():
        if rel in _LITERAL_ALLOWLIST:
            continue
        hits += _literal_hits(path.read_text(encoding="utf-8"), rel)
    assert hits == [], "\n".join(hits)


def test_the_persisted_blob_key_has_exactly_two_sites_and_both_are_the_queue():
    """`x_source_agent` also lives as a KEY in the durable backlog payload — written by
    `BacklogService.enqueue`, read back by `_spawn_drain` when the row is dispatched.
    That value is the router-resolved one, so the queue is not a second door for the
    raw header. It would become one if a third module started reading the key, or if
    the write site moved somewhere the router does not resolve first — hence this pin
    rather than a silent allowlist entry.

    Honest residual: a row queued BEFORE this fix shipped still carries whatever value
    was written then, and drains unchanged. The backlog drains continuously, so the
    window is the rows in flight at deploy time.
    """
    sites = {
        rel
        for rel, path in _oss_backend_files()
        if '"x_source_agent"' in path.read_text(encoding="utf-8")
    }
    assert sites == _BLOB_KEY_SITES, sites

    src = (_BACKEND / "services" / "backlog_service.py").read_text(encoding="utf-8")
    assert '"x_source_agent": x_source_agent,' in src          # enqueue writes the parameter
    assert 'x_source_agent=metadata.get("x_source_agent")' in src  # the drain reads it back


class TestGuardSelfTests:
    """The guard judged in both directions on synthetic sources, so a scanner regression
    cannot pass as an empty violation list."""

    def test_raw_forwarding_is_a_violation(self):
        src = (
            "async def h(name, current_user, x_source_agent=None):\n"
            "    await svc(name, x_source_agent=x_source_agent)\n"
        )
        assert _param_violations(src, "t") == ["t:h:L2 reads the raw header"]

    def test_rebind_first_is_clean(self):
        src = (
            "async def h(name, current_user, x_source_agent=None):\n"
            "    x_source_agent = resolve_source_agent(current_user, x_source_agent, endpoint='e')\n"
            "    await svc(name, x_source_agent=x_source_agent)\n"
        )
        assert _param_violations(src, "t") == []

    def test_validated_first_or_form_is_a_violation(self):
        """The shape this fix removed: behind an `or`, the helper never runs for an
        agent principal, so a mismatched header is silently ignored."""
        src = (
            "async def h(name, current_user, x_source_agent=None):\n"
            "    row = dict(source_agent_name=current_user.agent_name\n"
            "               or resolve_source_agent(current_user, x_source_agent, endpoint='e'))\n"
        )
        assert any("short-circuited" in v for v in _param_violations(src, "t"))

    def test_resolve_then_prefer_the_validated_key_is_clean(self):
        """The shape it was replaced with — the helper always runs, the validated key
        still wins."""
        src = (
            "async def h(name, current_user, x_source_agent=None):\n"
            "    resolved = resolve_source_agent(current_user, x_source_agent, endpoint='e')\n"
            "    row = dict(source_agent_name=current_user.agent_name or resolved)\n"
        )
        assert _param_violations(src, "t") == []

    def test_a_nested_handler_is_not_invisible(self):
        """`tree.body` alone cannot see a function defined inside a class or an `if`."""
        src = (
            "class R:\n"
            "    async def h(self, name, current_user, x_source_agent=None):\n"
            "        await svc(name, x_source_agent=x_source_agent)\n"
        )
        assert _param_violations(src, "t") == ["t:h:L3 reads the raw header"]

    def test_a_use_before_the_rebind_is_a_violation(self):
        src = (
            "async def h(name, current_user, x_source_agent=None):\n"
            "    audit(actor=x_source_agent)\n"
            "    x_source_agent = resolve_source_agent(current_user, x_source_agent, endpoint='e')\n"
        )
        assert _param_violations(src, "t") == ["t:h:L2 reads the raw header"]

    def test_the_helper_argument_is_not_a_violation_even_without_a_rebind(self):
        src = (
            "async def h(name, current_user, x_source_agent=None):\n"
            "    resolved = resolve_source_agent(current_user, x_source_agent, endpoint='e')\n"
            "    await svc(name, resolved)\n"
        )
        assert _param_violations(src, "t") == []

    def test_a_function_without_the_parameter_is_ignored(self):
        assert _param_violations("def h(a):\n    return a\n", "t") == []

    @pytest.mark.parametrize("src", [
        "v = request.headers.get('X-Source-Agent')\n",
        "x = Header(None, alias='x-source-agent')\n",
        "v = request.headers['X-SOURCE-AGENT']\n",
    ])
    def test_other_spellings_are_literal_hits(self, src):
        assert _literal_hits(src, "t") != []

    def test_the_alias_form_is_a_literal_hit(self):
        """`H = "X-Source-Agent"` then `headers.get(H)` — the shape a reader reaches for
        once the direct spelling is guarded."""
        assert _literal_hits('H = "X-Source-Agent"\nv = request.headers.get(H)\n', "t") != []

    def test_the_blob_key_is_not_a_header_hit(self):
        """The underscore spelling is a dict key, not a wire header — scanning for it
        would flag the queue payload and train the next reader to allowlist rather
        than think."""
        assert _literal_hits('v = metadata.get("x_source_agent")\n', "t") == []

    def test_a_docstring_mention_is_not_a_literal_hit(self):
        assert _literal_hits('def h():\n    """Reads X-Source-Agent."""\n', "t") == []
