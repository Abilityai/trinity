"""Callers of the sync edge adapter on a pull pilot (#3114).

Every sync ``execute_task`` caller that #3114 moved onto
``dispatch_and_await_terminal`` is driven here through its real function with
collaborators patched. Each test asserts what the caller hands the adapter
(``triggered_by``, ``conversation_key``, ``service``) and how it maps the
``TaskExecutionResult`` it gets back.

The callers import the adapter lazily (``from services.task_execution_service
import dispatch_and_await_terminal`` inside the function), so it is patched on
``services.task_execution_service``.

Also covered: the portal in-flight marker TTL and wait budgets grow by the
queue allowance on a pilot, ``CapacityManager.acquire`` forwards the #3114
payload fields to the backlog, and the public + portal live-stream proxies
hold while the row is queued.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

_BACKEND_STR = str(Path(__file__).resolve().parent.parent.parent / "src" / "backend")
if _BACKEND_STR not in sys.path:
    sys.path.insert(0, _BACKEND_STR)

pytestmark = pytest.mark.unit

AGENT = "alpha"
CAPACITY_ERR = "Agent at capacity (queued turn not claimed in 900s)"
TIMEOUT_ERR = "Execution timed out after 1020s waiting for the queued turn"


def _result(status="success", response="answer", error=None, eid="e1"):
    from services.execution_envelope import TaskExecutionResult

    return TaskExecutionResult(execution_id=eid, status=status, response=response,
                               error=error)


@pytest.fixture
def adapter(monkeypatch):
    """Patch the adapter where the lazy imports look it up."""
    import services.task_execution_service as tes

    mock = AsyncMock(return_value=_result())
    monkeypatch.setattr(tes, "dispatch_and_await_terminal", mock)
    return mock


def _timeouts(monkeypatch, seconds: int):
    """Every reader of the agent's execution timeout sees ``seconds``."""
    import database
    from services import session_turn_service as sts

    for db in {id(database.db): database.db, id(sts.db): sts.db}.values():
        monkeypatch.setattr(db, "get_execution_timeout", lambda _n: seconds)


# ---------------------------------------------------------------------------
# 1. public_chat_service
# ---------------------------------------------------------------------------


def _public(monkeypatch):
    from services import public_chat_service as pcs

    db = MagicMock()
    db.get_access_policy.return_value = {}
    db.count_recent_messages_by_ip.return_value = 0
    db.count_recent_messages_by_token.return_value = 0
    db.get_or_create_public_chat_session.return_value = SimpleNamespace(id="cs-7")
    db.build_public_chat_context.return_value = "ctx"
    db.get_public_chat_session.return_value = SimpleNamespace(message_count=2)
    db.get_public_channel_model.return_value = None
    monkeypatch.setattr(pcs, "db", db)
    monkeypatch.setattr(pcs, "get_agent_container", lambda n: SimpleNamespace(status="running"))
    monkeypatch.setattr(pcs, "build_public_channel_caller_prompt", lambda *a, **k: None)
    return pcs, db


def _public_request():
    return SimpleNamespace(session_token=None, session_id="anon-1", files=None,
                           message="hi", async_mode=False)


@pytest.mark.asyncio
async def test_public_sync_turn_is_keyed_by_its_chat_session(monkeypatch, adapter):
    pcs, db = _public(monkeypatch)

    out = await pcs.run_public_chat({"id": "link-1", "agent_name": AGENT},
                                    _public_request(), "1.2.3.4")

    kw = adapter.await_args.kwargs
    assert kw["triggered_by"] == "public"
    assert kw["conversation_key"] == "public:cs-7"
    assert out.response == "answer"
    db.add_public_chat_message.assert_called_with(
        session_id="cs-7", role="assistant", content="answer", cost=None,
        sender_email=None)


@pytest.mark.asyncio
@pytest.mark.parametrize("error,status", [(CAPACITY_ERR, 429), (TIMEOUT_ERR, 504),
                                          ("boom", 502)])
async def test_public_sync_maps_the_adapter_failure(monkeypatch, adapter, error, status):
    pcs, _ = _public(monkeypatch)
    adapter.return_value = _result(status="failed", response="", error=error)

    with pytest.raises(pcs.PublicChatError) as exc:
        await pcs.run_public_chat({"id": "link-1", "agent_name": AGENT},
                                  _public_request(), "1.2.3.4")
    assert exc.value.status_code == status


@pytest.mark.asyncio
async def test_public_background_turn_is_keyed_by_its_chat_session(monkeypatch, adapter):
    pcs, db = _public(monkeypatch)

    await pcs._execute_public_chat_background(
        agent_name=AGENT, context_prompt="ctx", source_email="anonymous (x)",
        execution_id="e1", chat_session_id="cs-9", session_identifier="anon",
        identifier_type="anonymous")

    kw = adapter.await_args.kwargs
    assert (kw["triggered_by"], kw["conversation_key"], kw["execution_id"]) == (
        "public", "public:cs-9", "e1")
    assert db.add_public_chat_message.call_args.kwargs["content"] == "answer"


@pytest.mark.asyncio
async def test_public_background_posts_nothing_on_capacity(monkeypatch, adapter):
    pcs, db = _public(monkeypatch)
    adapter.return_value = _result(status="failed", response="", error=CAPACITY_ERR)

    await pcs._execute_public_chat_background(
        agent_name=AGENT, context_prompt="ctx", source_email="anonymous (x)",
        execution_id="e1", chat_session_id="cs-9", session_identifier="anon",
        identifier_type="anonymous")
    db.add_public_chat_message.assert_not_called()


# ---------------------------------------------------------------------------
# 2. shared_sessions room wake
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_room_wake_is_keyed_by_the_room(monkeypatch, adapter):
    from shared_sessions import service

    room = "room-1"
    monkeypatch.setattr(service.db, "get_participant",
                        lambda *a, **k: {"last_read_seq": 0, "cached_session_id": "s1"})
    monkeypatch.setattr(service.db, "get_room", lambda *a, **k: {
        "id": room, "name": "Room", "status": "open", "topic": None})
    monkeypatch.setattr(service.db, "get_messages", lambda *a, **k: [
        {"seq": 1, "sender_kind": "user", "sender_identity": "c@example.com",
         "content": f"@{AGENT} hi", "kind": "message"}])
    monkeypatch.setattr(service.db, "list_participants", lambda _r: [
        {"kind": "agent", "identity": AGENT, "left_at": None}])
    monkeypatch.setattr(service.db, "advance_read_cursor", lambda *a, **k: None)
    for name in ("_mark_agent_working", "_clear_agent_working", "_broadcast", "_post_system"):
        monkeypatch.setattr(service, name, lambda *a, **k: None)
    monkeypatch.setattr(service, "post_message", AsyncMock(return_value={}))

    await service._wake_agent(SimpleNamespace(email="c@example.com"), room, AGENT, 0)

    kw = adapter.await_args.kwargs
    assert kw["triggered_by"] == "room"
    assert kw["conversation_key"] == f"room:{room}"
    assert kw["resume_session_id"] == "s1" and kw["persist_session"] is True


# ---------------------------------------------------------------------------
# 3. routers/paid.py
# ---------------------------------------------------------------------------


async def _drive_paid(monkeypatch, adapter, session_id, result):
    import routers.paid as paid

    config = SimpleNamespace(enabled=True, nvm_environment="testnet", credits_per_request=1)
    db = MagicMock()
    db.get_nevermined_config_with_key.return_value = {"config": config, "nvm_api_key": "k"}
    payment = MagicMock(
        verify_payment=AsyncMock(return_value=SimpleNamespace(
            success=True, payer="0xp", agent_request_id="r1", error=None)),
        settle_payment_once=AsyncMock(return_value=SimpleNamespace(
            success=True, tx_hash="0xt", remaining_balance=1, error=None)),
    )
    idem = MagicMock()
    idem.begin.return_value = MagicMock(replay=False)
    task_service = object()
    monkeypatch.setattr(paid, "NEVERMINED_AVAILABLE", True)
    monkeypatch.setattr(paid, "db", db)
    monkeypatch.setattr(paid, "idempotency_service", idem)
    monkeypatch.setattr(paid, "get_nevermined_payment_service", lambda: payment)
    monkeypatch.setattr(paid, "get_task_execution_service", lambda: task_service)
    monkeypatch.setattr(paid, "build_public_channel_caller_prompt", lambda *a, **k: None)
    adapter.return_value = result

    resp = await paid.paid_chat(
        AGENT, SimpleNamespace(message="hi", session_id=session_id),
        SimpleNamespace(headers={"payment-signature": "tok"}, base_url="http://localhost/"))
    body = json.loads(bytes(resp.body).decode()) if hasattr(resp, "body") else resp
    return adapter.await_args.kwargs, body, task_service, payment


@pytest.mark.asyncio
async def test_paid_resumed_session_is_its_conversation_key(monkeypatch, adapter):
    kw, _, task_service, _ = await _drive_paid(monkeypatch, adapter, "sess-3", _result())
    assert kw["triggered_by"] == "paid"
    assert kw["conversation_key"] == "paid:sess-3"
    assert kw["resume_session_id"] == "sess-3"
    assert kw["service"] is task_service


@pytest.mark.asyncio
async def test_paid_without_session_has_no_key(monkeypatch, adapter):
    kw, _, _, _ = await _drive_paid(monkeypatch, adapter, None, _result())
    assert kw["conversation_key"] is None


@pytest.mark.asyncio
async def test_paid_cancelled_returns_partial_and_does_not_settle(monkeypatch, adapter):
    _, body, _, payment = await _drive_paid(
        monkeypatch, adapter, "sess-3",
        _result(status="cancelled", response="partial work", eid="e9"))
    assert body["status"] == "cancelled"
    assert body["response"] == "partial work"
    assert body["execution_id"] == "e9"
    assert body["payment"]["settled"] is False
    payment.settle_payment_once.assert_not_called()


# ---------------------------------------------------------------------------
# 4. mcp_auth_service.dispatch_chat
# ---------------------------------------------------------------------------


def _mcp(monkeypatch):
    from services import mcp_auth_service as mas

    monkeypatch.setattr(mas, "assert_email_may_reach_agent", lambda *a: {})
    monkeypatch.setattr(mas, "db", MagicMock(get_public_channel_model=lambda a: None))
    return mas


@pytest.mark.asyncio
async def test_mcp_inline_chat_has_no_conversation_key(monkeypatch, adapter):
    mas = _mcp(monkeypatch)

    out = await mas.dispatch_chat("User@Example.com", AGENT, "hi")

    kw = adapter.await_args.kwargs
    assert kw["triggered_by"] == mas.CHAT_TRIGGERED_BY == "mcp"
    assert "conversation_key" not in kw
    assert kw["source_user_email"] == "user@example.com"
    assert out.response == "answer"


@pytest.mark.asyncio
@pytest.mark.parametrize("error,status", [(CAPACITY_ERR, 429), (TIMEOUT_ERR, 504),
                                          ("boom", 502)])
async def test_mcp_inline_chat_maps_the_adapter_failure(monkeypatch, adapter, error, status):
    from fastapi import HTTPException

    mas = _mcp(monkeypatch)
    adapter.return_value = _result(status="failed", response="", error=error)
    with pytest.raises(HTTPException) as exc:
        await mas.dispatch_chat("u@example.com", AGENT, "hi")
    assert exc.value.status_code == status


# ---------------------------------------------------------------------------
# 5. validation_service
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validation_goes_through_the_adapter_with_its_own_service(monkeypatch, adapter):
    from services import validation_service as vs

    db = MagicMock()
    db.create_validation_execution.return_value = SimpleNamespace(id="v1")
    monkeypatch.setattr(vs, "db", db)
    monkeypatch.setattr(vs, "get_referee", lambda: None)
    svc = MagicMock()
    adapter.return_value = _result(response='{"status": "pass", "summary": "ok", "items": []}')

    await vs.ValidationService(svc).validate_execution(
        execution_id="e1", agent_name=AGENT, schedule_id="s1",
        original_message="do it", execution_response="done")

    kw = adapter.await_args.kwargs
    assert kw["service"] is svc
    assert kw["triggered_by"] == "validation"
    assert kw["execution_id"] == "v1"
    assert "conversation_key" not in kw
    svc.execute_task.assert_not_called()


# ---------------------------------------------------------------------------
# 6. routers/internal.py sync run-now
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_internal_sync_run_goes_through_the_adapter(monkeypatch, adapter):
    import routers.internal as internal
    import services.cleanup_service as cleanup_service
    from models import InternalTaskExecutionRequest

    svc = MagicMock()
    monkeypatch.setattr(cleanup_service, "is_startup_recovery_complete", lambda: True)
    monkeypatch.setattr(internal, "get_task_execution_service", lambda: svc)
    monkeypatch.setattr(internal.idempotency_service, "begin",
                        lambda *a, **k: SimpleNamespace(replay=False))
    for name in ("attach_execution", "complete", "fail"):
        monkeypatch.setattr(internal.idempotency_service, name, lambda *a, **k: None)
    monkeypatch.setattr(internal.platform_audit_service, "log", AsyncMock())

    out = await internal.execute_task_internal(
        InternalTaskExecutionRequest(agent_name=AGENT, message="run", triggered_by="manual",
                                     execution_id="e1", schedule_id="sch-1", attempt=2),
        idempotency_key=None)

    kw = adapter.await_args.kwargs
    assert kw["service"] is svc
    assert kw["triggered_by"] == "manual"
    assert kw["execution_id"] == "e1" and kw["attempt"] == 2
    assert "conversation_key" not in kw
    assert (out["status"], out["response"]) == ("success", "answer")
    svc.execute_task.assert_not_called()


# ---------------------------------------------------------------------------
# 7. client_portal/service.py — marker TTL and wait budgets
# ---------------------------------------------------------------------------

TURN = 600


class _Stop(Exception):
    pass


def _allowance_expected(pilot: bool) -> int:
    return 2 * TURN if pilot else 0


@pytest.mark.asyncio
@pytest.mark.parametrize("pilot", [True, False])
async def test_portal_sync_marker_ttl_adds_the_allowance(monkeypatch, pilot):
    from client_portal import service

    monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", AGENT if pilot else "")
    _timeouts(monkeypatch, TURN)
    db = MagicMock()
    db.get_cached_claude_session_id.return_value = None
    db.get_portal_thread_window.return_value = SimpleNamespace(rows=[])
    monkeypatch.setattr(service, "db", db)
    monkeypatch.setattr(service, "agent_on_roster", lambda *a: True)
    monkeypatch.setattr(service, "_availability_allows_turn", lambda a: True)
    monkeypatch.setattr(service, "resolve_turn_model", lambda *a: None)
    monkeypatch.setattr(service, "_resolve_session_id", lambda *a, **k: "s1")
    monkeypatch.setattr(service, "_refuse_turn_during_voice_call", lambda *a, **k: None)
    monkeypatch.setattr(service, "_title_plan", lambda *a: None)
    monkeypatch.setattr(service, "_persist_user_turn", lambda *a, **k: None)
    monkeypatch.setattr(service, "collect_inbox_context", AsyncMock(return_value=("", [])))
    monkeypatch.setattr(service, "_build_portal_system_prompt", lambda *a: None)
    monkeypatch.setattr(service, "_precreate_sync_execution", lambda *a, **k: "e1")
    marks = []
    monkeypatch.setattr(service, "mark_turn_inflight", lambda *a, **k: marks.append(a))
    monkeypatch.setattr(service, "_run_sync_turn_and_clear_marker",
                        AsyncMock(side_effect=_Stop()))

    with pytest.raises(_Stop):
        await service.portal_chat(AGENT, "hi", "c@example.com", availability="running",
                                  turn_timeout_seconds=TURN)
    assert marks == [("s1", "e1", TURN + 60 + _allowance_expected(pilot))]


@pytest.mark.asyncio
@pytest.mark.parametrize("pilot", [True, False])
async def test_portal_streaming_budget_adds_the_allowance(monkeypatch, pilot):
    import database
    from client_portal import service

    monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", AGENT if pilot else "")
    _timeouts(monkeypatch, TURN)
    monkeypatch.setattr(service, "agent_on_roster", lambda *a: True)
    monkeypatch.setattr(service, "_agent_availability", AsyncMock(return_value="running"))
    monkeypatch.setattr(service, "_availability_allows_turn", lambda a: True)
    monkeypatch.setattr(service, "resolve_turn_model", lambda *a: None)
    monkeypatch.setattr(service, "_resolve_session_id", lambda *a, **k: "s1")
    monkeypatch.setattr(service, "_refuse_turn_during_voice_call", lambda *a, **k: None)
    monkeypatch.setattr(database.db, "get_agent_subscription_id", lambda a: None)
    monkeypatch.setattr(database.db, "create_task_execution",
                        lambda **k: SimpleNamespace(id="e1"))
    marks = []
    monkeypatch.setattr(service, "mark_turn_inflight", lambda *a, **k: marks.append(k))
    for name in ("clear_turn_outcome", "clear_turn_inflight", "record_turn_outcome"):
        monkeypatch.setattr(service, name, lambda *a, **k: None)
    monkeypatch.setattr(service, "portal_chat", AsyncMock(return_value={}))

    out = await service.start_portal_turn(AGENT, "hi", "c@example.com")
    await asyncio.gather(*list(service._INFLIGHT_TURNS))

    expected = service.portal_max_turn_seconds(TURN) + _allowance_expected(pilot)
    assert out["wait_budget_seconds"] == expected
    assert marks == [{"ttl_seconds": expected}]


@pytest.mark.parametrize("pilot", [True, False])
def test_portal_history_budget_adds_the_allowance(monkeypatch, pilot):
    import redis_breaker_util
    from client_portal import service

    monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", AGENT if pilot else "")
    _timeouts(monkeypatch, TURN)
    db = MagicMock()
    db.get_latest_portal_session_id.return_value = "s1"
    db.get_portal_thread_window.return_value = SimpleNamespace(rows=[], truncated=False)
    monkeypatch.setattr(service, "db", db)
    monkeypatch.setattr(service, "agent_on_roster", lambda *a: True)
    monkeypatch.setattr(service, "_attach_own_ratings", lambda *a, **k: None)
    monkeypatch.setattr(service, "get_turn_inflight", lambda s: "e1")
    monkeypatch.setattr(service, "get_turn_outcome", lambda s: None)
    # TTL -1: no expiry on the marker → the full per-agent budget.
    monkeypatch.setattr(redis_breaker_util, "get_breaker_redis",
                        lambda: SimpleNamespace(ttl=lambda k: -1))

    out = service.get_history(AGENT, "c@example.com")
    assert out["in_flight_wait_budget_seconds"] == (
        service.portal_max_turn_seconds(TURN) + _allowance_expected(pilot))


# ---------------------------------------------------------------------------
# 8. CapacityManager.acquire → backlog_service.enqueue
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acquire_forwards_the_turn_fields_to_enqueue(monkeypatch):
    from services import capacity_manager as cm

    monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", AGENT)
    monkeypatch.setattr(cm.redis, "from_url",
                        lambda *a, **k: SimpleNamespace(set=lambda *a, **k: True))
    slots = MagicMock(acquire_slot=AsyncMock(return_value=True),
                      register_on_release=lambda cb: None)
    backlog = MagicMock(enqueue=AsyncMock(return_value=True))
    manager = cm.CapacityManager(redis_url="redis://test", slot_service=slots,
                                 backlog_service=backlog)
    images = [{"media_type": "image/png", "data": "AAAA"}]

    out = await manager.acquire(
        agent_name=AGENT, execution_id="e1", max_concurrent=2,
        overflow_policy="queue_persistent",
        overflow_payload=cm.PersistentTaskPayload(
            request=MagicMock(), effective_timeout=900, user_id=1, user_email="u@x",
            subscription_id=None, x_source_agent=None, triggered_by="public",
            collaboration_activity_id=None, conversation_key="public:cs-1",
            persist_session=True, schedule_context={"name": "nightly"}, attempt=3,
            images=images))

    assert out.state == "queued_persistent"
    slots.acquire_slot.assert_not_called()
    kw = backlog.enqueue.await_args.kwargs
    assert kw["conversation_key"] == "public:cs-1"
    assert kw["persist_session"] is True
    assert kw["schedule_context"] == {"name": "nightly"}
    assert kw["attempt"] == 3
    assert kw["images"] == images


# ---------------------------------------------------------------------------
# 9. public + portal live-stream proxies hold while queued
# (fakes copied from test_3114_pull_route_interactive.py)
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status, chunks=()):
        self.status_code = status
        self._chunks = chunks

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def aiter_text(self):
        for c in self._chunks:
            yield c


class _FakeAgentClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def __call__(self, *a, **k):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def stream(self, method, url):
        self.calls += 1
        return self._responses.pop(0)


def _stream_env(monkeypatch, router_mod, pilot: bool):
    """A mutable execution row every reader sees, plus a fake agent client."""
    import database
    from services import sync_waiter

    row = SimpleNamespace(status="queued", agent_name=AGENT)
    monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", AGENT if pilot else "")
    _timeouts(monkeypatch, TURN)
    monkeypatch.setattr(database.db, "get_execution", lambda eid: row)
    # "running" here stands for a claimed row (lease set).
    monkeypatch.setattr(database.db, "execution_awaits_claim", lambda eid: row.status == "queued")
    monkeypatch.setattr(sync_waiter, "STREAM_QUEUED_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(sync_waiter, "STREAM_ATTACH_RETRY_INTERVAL", 0.01)
    monkeypatch.setattr(router_mod, "get_agent_container",
                        lambda n: SimpleNamespace(status="running"))
    client = _FakeAgentClient([_FakeResponse(404), _FakeResponse(200, ["data: x\n\n"])])
    monkeypatch.setattr(router_mod, "agent_httpx_client", client)
    return row, client


async def _public_stream(monkeypatch, pilot):
    from routers import public

    row, client = _stream_env(monkeypatch, public, pilot)
    monkeypatch.setattr(public.db, "get_execution", lambda eid: row)
    monkeypatch.setattr(public, "check_public_link_rate_limit", lambda ip: None)
    monkeypatch.setattr(public, "_validate_public_link", lambda t: {"agent_name": AGENT})
    resp = await public.public_stream_execution(
        "tok", "e1", SimpleNamespace(client=SimpleNamespace(host="1.2.3.4"), headers={}))
    return row, client, resp.body_iterator


async def _portal_stream(monkeypatch, pilot):
    from client_portal import router as portal_router
    from client_portal import service

    row, client = _stream_env(monkeypatch, portal_router, pilot)
    monkeypatch.setattr(service, "agent_on_roster", lambda *a: True)
    monkeypatch.setattr(service, "execution_belongs_to_caller", lambda *a: True)
    monkeypatch.setattr(service, "get_turn_inflight_matches", lambda eid: True)
    monkeypatch.setattr(portal_router, "_STREAM_ATTACH_POLL_S", 0.01)
    resp = await portal_router.portal_stream_execution(
        AGENT, "e1", SimpleNamespace(email="c@example.com", is_platform=False))
    return row, client, resp.body_iterator


@pytest.mark.asyncio
@pytest.mark.parametrize("open_stream", [_public_stream, _portal_stream],
                         ids=["public", "portal"])
async def test_stream_holds_while_queued_then_attaches(monkeypatch, open_stream):
    row, client, body = await open_stream(monkeypatch, pilot=True)

    assert await body.__anext__() == ": queued\n\n"
    assert await body.__anext__() == ": queued\n\n"      # still queued: still holding
    assert client.calls == 0
    row.status = "running"                                 # claimed
    rest = [c async for c in body]
    assert rest[-1] == "data: x\n\n"                       # 404 before registration retried
    assert client.calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("open_stream", [_public_stream, _portal_stream],
                         ids=["public", "portal"])
async def test_stream_off_pilot_does_not_hold(monkeypatch, open_stream):
    _, client, body = await open_stream(monkeypatch, pilot=False)
    chunks = [c async for c in body]
    assert not any(c.startswith(":") for c in chunks)
    assert client.calls >= 1
