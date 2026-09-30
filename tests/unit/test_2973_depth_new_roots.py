"""#2973 — loops, manual schedule triggers and agent events inherit the
inter-agent chain depth (#2806) instead of starting a fresh depth-0 root.

Before this, an agent near `inter_agent_max_chain_depth` could reset its depth
by starting a loop, triggering a schedule, or emitting an event its own
subscription answers. Each path is driven here through its real entry — the
router over HTTP with a real minted agent-scoped bearer, and for events the
real EVT-001 loopback token into the real `/task` route — with a non-default
max, on the real schema (`tests/db_harness.py`). The depth query is never
mocked; only the scheduler hop, the agent dispatch and the loop's background
turn are doubles, because they talk to another process or a container.

The per-subscriber dispatch budget (CSO Finding 3) lives in
`test_2973_event_dispatch_budget.py`.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
# `src.scheduler` imports as a package (relative imports), so the repo root
# must be importable too — appended, so `database` still resolves to the backend.
_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.append(str(_REPO))

from db_harness import count as _count, db_backend, run as _hrun  # noqa: E402,F401

import routers.chat as _CHAT  # noqa: E402
import routers.event_subscriptions as _EVENTS  # noqa: E402
import routers.loops as _LOOPS  # noqa: E402
import routers.schedules as _SCHED  # noqa: E402
import services.chat_execution_service as _CE  # noqa: E402
import services.dispatch_admission_service as _DISPATCH  # noqa: E402
import services.event_dispatch_service as _EDS  # noqa: E402
import services.loop_service as _LOOPSVC  # noqa: E402
from database import db  # noqa: E402
from db.write_params import TaskExecutionFields  # noqa: E402
from db_models import EventSubscriptionCreate, McpApiKeyCreate, ScheduleCreate, UserCreate  # noqa: E402
from error_handlers import inter_agent_depth_exceeded  # noqa: E402
from services.chat_signals import InterAgentDepthExceeded  # noqa: E402

pytestmark = pytest.mark.unit

import routers.sessions as _SESSIONS_MOD  # noqa: E402

_REAL_SESSION_OR_404 = _SESSIONS_MOD._session_or_404

KEY = "inter_agent_max_chain_depth"
OWNER = "nr-owner"
A, B = "nr-a", "nr-b"
CODE = "inter_agent_depth_exceeded"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def world(db_backend, monkeypatch):
    db.create_user(UserCreate(username=OWNER, role="user", email="nr-owner@example.com"))
    # The EVT-001 loopback JWT is `sub: admin`.
    db.create_user(UserCreate(username="admin", role="admin", email="nr-admin@example.com"))
    db.register_agent_owner(A, OWNER)
    db.register_agent_owner(B, OWNER)

    capacity = SimpleNamespace(
        acquire=AsyncMock(return_value=SimpleNamespace(state="admitted", queue_position=None))
    )
    monkeypatch.setattr(_DISPATCH, "get_capacity_manager", lambda: capacity)
    monkeypatch.setattr(_DISPATCH, "dispatch_breaker_active", lambda _n: False)
    monkeypatch.setattr(_CE, "_dispatch_async", AsyncMock(return_value={"status": "accepted"}))
    monkeypatch.setattr(_CE, "_dispatch_sync", AsyncMock(return_value={"status": "success"}))
    monkeypatch.setattr(_CHAT, "get_agent_container", lambda _n: SimpleNamespace(status="running"))

    # The loop's background turn talks to a container; record and drop it.
    spawned = []

    def _spawn(coro):
        spawned.append(coro)
        coro.close()

    monkeypatch.setattr(_LOOPSVC, "_spawn", _spawn)

    # Event dispatch is fire-and-forget; capture what each emit hands it.
    dispatched = []

    async def _capture(sub, event, **kw):
        dispatched.append((sub.subscriber_agent, kw))

    monkeypatch.setattr(_EDS, "trigger_subscription", _capture)
    return SimpleNamespace(spawned=spawned, dispatched=dispatched)


@pytest.fixture
def client(world):
    app = FastAPI()
    app.add_exception_handler(InterAgentDepthExceeded, inter_agent_depth_exceeded)
    app.include_router(_LOOPS.agent_router)
    app.include_router(_SCHED.router)
    app.include_router(_EVENTS.router)
    app.include_router(_CHAT.router)
    return TestClient(app)


def _running_row(agent: str, depth) -> str:
    return db.create_task_execution(
        agent_name=agent,
        message="in-flight turn",
        triggered_by="agent",
        fields=TaskExecutionFields(chain_depth=depth),
    ).id


def _finish(execution_id: str) -> None:
    _hrun("UPDATE schedule_executions SET status='success' WHERE id=:i", i=execution_id)


def _set_max(value: int) -> None:
    db.set_setting(KEY, str(value))


def _depths(agent: str) -> list:
    from db.engine import get_engine
    from sqlalchemy import text

    with get_engine().connect() as conn:
        return [
            r[0]
            for r in conn.execute(
                text(
                    "SELECT chain_depth FROM schedule_executions "
                    "WHERE agent_name = :a AND message != 'in-flight turn' ORDER BY started_at"
                ),
                {"a": agent},
            )
        ]


def _bearer(agent=A) -> dict:
    return {"Authorization": f"Bearer {db.create_agent_mcp_api_key(agent, OWNER).api_key}"}


def _user_bearer() -> dict:
    key = db.create_mcp_api_key(OWNER, McpApiKeyCreate(name="nr-user-key"))
    return {"Authorization": f"Bearer {key.api_key}"}


def _assert_named_403(resp, depth, max_depth, caller=A):
    assert resp.status_code == 403, resp.text
    assert resp.headers["X-Trinity-Error-Code"] == CODE
    detail = resp.json()["detail"]
    assert (detail["error"], detail["depth"], detail["max_depth"], detail["caller"]) == (
        CODE, depth, max_depth, caller,
    )


# ---------------------------------------------------------------------------
# loops
# ---------------------------------------------------------------------------

LOOP_BODY = {"message": "iterate", "max_runs": 3}


def test_2973_loop_start_at_the_max_is_refused_with_the_named_403(client, world):
    _set_max(3)
    _running_row(A, 3)
    resp = client.post(f"/api/agents/{A}/loops", json=LOOP_BODY, headers=_bearer())
    _assert_named_403(resp, 4, 3)
    assert _count("agent_loops", "agent_name = :a", a=A) == 0
    assert world.spawned == []


def test_2973_loop_iterations_carry_the_starters_depth_after_its_turn_ends(client, world):
    _set_max(3)
    starter = _running_row(A, 1)
    resp = client.post(f"/api/agents/{A}/loops", json=LOOP_BODY, headers=_bearer())
    assert resp.status_code == 202, resp.text
    loop_id = resp.json()["loop_id"]

    loop = db.get_loop(loop_id)
    assert loop["chain_depth"] == 2
    assert _depths(A) == [2]

    # Iteration 2 runs after the starter's turn has finished: its running rows
    # read 0 now, so only the persisted loop depth can carry the chain.
    _finish(starter)
    asyncio.run(_LOOPSVC.get_loop_service()._dispatch_run(db.get_loop(loop_id), run_number=2))
    assert _depths(A) == [2, 2]


def test_2973_a_loop_started_by_a_human_key_stays_a_root(client, world):
    _set_max(1)
    _running_row(A, 5)
    resp = client.post(f"/api/agents/{A}/loops", json=LOOP_BODY, headers=_user_bearer())
    assert resp.status_code == 202, resp.text
    assert db.get_loop(resp.json()["loop_id"])["chain_depth"] is None
    assert _depths(A) == [None]


# ---------------------------------------------------------------------------
# manual schedule trigger
# ---------------------------------------------------------------------------


class _SchedulerHop:
    """Stands in for httpx.AsyncClient on the backend → scheduler hop."""

    def __init__(self):
        self.posts = []

    def __call__(self, *a, **kw):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, timeout=None):
        self.posts.append(json)
        return SimpleNamespace(
            status_code=200,
            json=lambda: {"execution_id": "ex-1", "schedule_name": "s", "agent_name": A},
            text="",
        )


@pytest.fixture
def hop(world, monkeypatch):
    fake = _SchedulerHop()
    monkeypatch.setattr(_SCHED.httpx, "AsyncClient", fake)
    return fake


def _schedule() -> str:
    return db.create_schedule(
        A, OWNER, ScheduleCreate(name="nightly", cron_expression="0 3 * * *", message="run")
    ).id


def test_2973_schedule_trigger_at_the_max_is_refused_before_the_scheduler_hop(client, hop):
    _set_max(2)
    _running_row(A, 2)
    resp = client.post(f"/api/agents/{A}/schedules/{_schedule()}/trigger", headers=_bearer())
    _assert_named_403(resp, 3, 2)
    assert hop.posts == []


def test_2973_schedule_trigger_forwards_the_inherited_depth(client, hop):
    _set_max(2)
    _running_row(A, 1)
    resp = client.post(f"/api/agents/{A}/schedules/{_schedule()}/trigger", headers=_bearer())
    assert resp.status_code == 200, resp.text
    assert hop.posts[0]["chain_depth"] == 2


def test_2973_schedule_trigger_by_a_human_key_forwards_no_depth(client, hop):
    _set_max(1)
    _running_row(A, 5)
    resp = client.post(f"/api/agents/{A}/schedules/{_schedule()}/trigger", headers=_user_bearer())
    assert resp.status_code == 200, resp.text
    assert hop.posts[0]["chain_depth"] is None


# ---------------------------------------------------------------------------
# event emit
# ---------------------------------------------------------------------------


def _subscribe(subscriber: str, source: str, event_type: str = "work.done") -> None:
    db.create_event_subscription(
        subscriber,
        EventSubscriptionCreate(source_agent=source, event_type=event_type, target_message="go"),
        OWNER,
    )


def test_2973_emit_at_the_max_with_a_subscriber_is_refused_and_persists_nothing(client, world):
    _set_max(2)
    _running_row(A, 2)
    _subscribe(A, A)  # self-subscription: the serial-loop shape
    resp = client.post("/api/events", json={"event_type": "work.done"}, headers=_bearer())
    _assert_named_403(resp, 3, 2)
    assert _count("agent_events", "source_agent = :a", a=A) == 0
    assert world.dispatched == []


def test_2973_emit_nobody_listens_to_is_never_refused(client, world):
    _set_max(2)
    _running_row(A, 2)
    resp = client.post("/api/events", json={"event_type": "work.done"}, headers=_bearer())
    assert resp.status_code == 201, resp.text


def test_2973_emit_hands_each_subscriber_the_inherited_depth(client, world):
    _set_max(3)
    _running_row(A, 1)
    _subscribe(A, A)
    resp = client.post("/api/events", json={"event_type": "work.done"}, headers=_bearer())
    assert resp.status_code == 201, resp.text
    assert world.dispatched == [(A, {"agent_originated": True, "chain_depth": 2})]


def test_2973_emit_for_a_sibling_carries_the_emitters_depth_unvouched(client, world):
    """A emits "for" B: the event is B's, the loopback is NOT vouched (ent#614),
    but the depth is A's — the sibling path must not launder it."""
    _set_max(3)
    _running_row(A, 2)
    _subscribe(B, B)
    resp = client.post(
        f"/api/agents/{B}/emit-event", json={"event_type": "work.done"}, headers=_bearer()
    )
    assert resp.status_code == 201, resp.text
    assert world.dispatched == [(B, {"agent_originated": False, "chain_depth": 3})]


def test_2973_a_human_emit_is_a_root(client, world):
    _set_max(1)
    _running_row(A, 5)
    _subscribe(B, B)
    resp = client.post(
        f"/api/agents/{B}/emit-event", json={"event_type": "work.done"}, headers=_user_bearer()
    )
    assert resp.status_code == 201, resp.text
    assert world.dispatched == [(B, {"agent_originated": False, "chain_depth": None})]


# ---------------------------------------------------------------------------
# the EVT-001 loopback into the real /task route
# ---------------------------------------------------------------------------


def _loopback(client, source, depth):
    token = _EDS._get_internal_token(source, depth)
    headers = {"Authorization": f"Bearer {token}", "X-Via-MCP": "true"}
    if source:
        headers["X-Source-Agent"] = source
    return client.post(
        f"/api/agents/{B}/task", json={"message": "event work", "async_mode": True}, headers=headers
    )


def test_2973_loopback_claim_is_stamped_on_the_subscribers_row(client):
    _set_max(3)
    resp = _loopback(client, A, 2)
    assert resp.status_code in (200, 202), resp.text
    assert _depths(B) == [2]


def test_2973_unvouched_loopback_still_carries_its_depth(client):
    _set_max(3)
    resp = _loopback(client, None, 3)
    assert resp.status_code in (200, 202), resp.text
    assert _depths(B) == [3]


def test_2973_loopback_claim_over_the_max_is_refused(client):
    _set_max(2)
    resp = _loopback(client, A, 3)
    _assert_named_403(resp, 3, 2)
    assert _depths(B) == []


def test_2973_unvouched_loopback_refusal_names_no_caller(client):
    _set_max(2)
    resp = _loopback(client, None, 3)
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["caller"] is None
    assert _count("agent_activities", "activity_type = 'agent_collaboration' AND agent_name = :b", b=B) == 0


def test_2973_vouched_loopback_without_a_claim_counts_the_emitters_running_rows(client):
    """AC #2: the caller falls back to `vouched_source_agent`."""
    _set_max(3)
    _running_row(A, 1)
    resp = _loopback(client, A, None)
    assert resp.status_code in (200, 202), resp.text
    assert _depths(B) == [2]


def test_2973_a_forged_claim_is_rejected_by_the_signature(client):
    from jose import jwt

    token = jwt.encode(
        {"sub": "admin", "scope": "event_loopback", "chain_depth": 1}, "not-the-key", algorithm="HS256"
    )
    resp = client.post(
        f"/api/agents/{B}/task", json={"message": "x"}, headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# system-emitted terminal events (#1578)
# ---------------------------------------------------------------------------


def _terminal(execution_id):
    async def _go():
        await _EDS.emit_task_terminal_event(
            A, execution_id, terminal_status="success", summary_or_error="done"
        )
        await asyncio.sleep(0)  # let the spawned dispatch run

    asyncio.run(_go())


@pytest.fixture
def terminal_capture(world, monkeypatch):
    captured = []

    async def _capture(sub, event, **kw):
        captured.append(kw)

    monkeypatch.setattr(_EDS, "trigger_subscription", _capture)
    # Run the dispatch inline instead of as a background task.
    monkeypatch.setattr(_EDS, "_spawn_emit_dispatch", lambda coro: asyncio.get_event_loop().create_task(coro))
    _subscribe(B, A, "agent.task.completed")
    return captured


def test_2973_terminal_event_carries_the_finished_rows_depth_plus_one(terminal_capture):
    _set_max(8)
    row = _running_row(A, 3)
    _finish(row)
    _terminal(row)
    assert terminal_capture == [{"agent_originated": True, "chain_depth": 4}]


def test_2973_terminal_event_with_an_unreadable_row_counts_as_the_max(terminal_capture, monkeypatch):
    _set_max(5)
    row = _running_row(A, 0)
    monkeypatch.setattr(_EDS.db, "get_execution", MagicMock(side_effect=RuntimeError("db down")))
    _terminal(row)
    assert terminal_capture == [{"agent_originated": True, "chain_depth": 5}]


# ---------------------------------------------------------------------------
# the scheduler side
# ---------------------------------------------------------------------------


def _scheduler_models():
    import src.scheduler.models as m

    return m


@pytest.mark.parametrize(
    "raw,expected",
    [(3, 3), (1, 1), (1000, 1000), (0, None), (-1, None), (1001, None), (True, None), ("3", None), (None, None), (2.0, None)],
)
def test_2973_scheduler_accepts_only_a_positive_int_depth(raw, expected):
    origin = _scheduler_models().ExecutionOrigin.from_payload({"chain_depth": raw})
    assert origin.chain_depth == expected


def test_2973_scheduler_manual_trigger_stamps_the_depth(tmp_path):
    import sqlite3

    import src.scheduler.database as sdb

    path = tmp_path / "s.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE schedule_executions (id TEXT PRIMARY KEY, schedule_id TEXT, agent_name TEXT, "
        "status TEXT, started_at TEXT, message TEXT, triggered_by TEXT, model_used TEXT, "
        "attempt_number INTEGER, retry_of_execution_id TEXT, source_user_id INTEGER, "
        "source_user_email TEXT, source_agent_name TEXT, source_mcp_key_id TEXT, "
        "source_mcp_key_name TEXT, chain_depth INTEGER)"
    )
    conn.commit()
    conn.close()
    database = sdb.SchedulerDatabase(str(path))
    execution = database.create_execution(
        schedule_id="s1", agent_name=A, message="m", triggered_by="manual", chain_depth=4
    )
    assert execution.chain_depth == 4
    conn = sqlite3.connect(path)
    stored = conn.execute(
        "SELECT chain_depth FROM schedule_executions WHERE id = ?", (execution.id,)
    ).fetchone()[0]
    conn.close()
    assert stored == 4


def test_2973_scheduler_retry_keeps_the_originals_depth():
    import src.scheduler.service as ssvc

    svc = object.__new__(ssvc.SchedulerService)
    svc.db = MagicMock()
    svc.db.get_schedule.return_value = SimpleNamespace(enabled=True)
    svc.db.get_execution.return_value = SimpleNamespace(
        source_user_id=None, source_user_email=None, source_agent_name=A,
        source_mcp_key_id=None, source_mcp_key_name=None, chain_depth=4,
    )
    svc.db.create_execution.return_value = None  # stop right after the insert
    asyncio.run(
        svc._execute_retry(
            original_execution_id="orig", failed_execution_id="failed", schedule_id="s1",
            agent_name=A, message="m", timeout_seconds=None, model=None, allowed_tools=None,
            next_attempt_number=2,
        )
    )
    assert svc.db.create_execution.call_args.kwargs["chain_depth"] == 4


# ---------------------------------------------------------------------------
# chat-session turns
# ---------------------------------------------------------------------------


@pytest.fixture
def session_client(world, monkeypatch):
    import routers.sessions as _SESSIONS

    monkeypatch.setattr(_SESSIONS, "_enabled_or_404", lambda: None)
    monkeypatch.setattr(
        _SESSIONS, "_session_or_404",
        lambda sid, user, name: SimpleNamespace(id=sid, subscription_id=None),
    )
    added = MagicMock()
    monkeypatch.setattr(_SESSIONS.db, "add_session_message", added)
    turn = AsyncMock(side_effect=RuntimeError("stop after dispatch"))
    monkeypatch.setattr(_SESSIONS, "run_resumable_turn", turn)
    app = FastAPI()
    app.add_exception_handler(InterAgentDepthExceeded, inter_agent_depth_exceeded)
    app.include_router(_SESSIONS.router)
    return SimpleNamespace(
        client=TestClient(app, raise_server_exceptions=False), added=added, turn=turn
    )


def test_2973_session_turn_at_the_max_is_refused_before_the_message_is_kept(session_client):
    _set_max(2)
    _running_row(A, 2)
    resp = session_client.client.post(
        f"/api/agents/{B}/sessions/s1/message", json={"message": "hi"}, headers=_bearer()
    )
    _assert_named_403(resp, 3, 2)
    session_client.added.assert_not_called()
    session_client.turn.assert_not_called()


def test_2973_session_turn_carries_the_inherited_depth(session_client):
    _set_max(3)
    _running_row(A, 1)
    session_client.client.post(
        f"/api/agents/{B}/sessions/s1/message", json={"message": "hi"}, headers=_bearer()
    )
    assert session_client.turn.call_args.kwargs["chain_depth"] == 2


def test_2973_execute_task_stamps_the_depth_on_the_row_it_creates(world, monkeypatch):
    import services.task_execution_service as _TES

    class _Stop(Exception):
        pass

    seen = {}

    def _create(**kw):
        seen.update(kw)
        raise _Stop

    monkeypatch.setattr(_TES, "get_capacity_manager", lambda: SimpleNamespace())
    monkeypatch.setattr(_TES, "dispatch_breaker_active", lambda _n: False)
    monkeypatch.setattr(_TES.db, "create_task_execution", _create)
    with pytest.raises(_Stop):
        asyncio.run(
            _TES.get_task_execution_service().execute_task(
                agent_name=B, message="m", triggered_by="session",
                model="claude-x", timeout_seconds=10, chain_depth=3,
            )
        )
    assert seen["fields"].chain_depth == 3


def test_2973_a_foreign_or_missing_session_is_404_before_the_depth_403(session_client, monkeypatch):
    """Invariant #8: the session lookup answers first, so the depth 403 never
    tells an agent at the max that a session id exists."""
    import routers.sessions as _SESSIONS

    monkeypatch.setattr(_SESSIONS, "_session_or_404", _REAL_SESSION_OR_404)
    _set_max(1)
    _running_row(A, 5)
    resp = session_client.client.post(
        f"/api/agents/{B}/sessions/no-such-session/message", json={"message": "hi"}, headers=_bearer()
    )
    assert resp.status_code == 404, resp.text
    assert "X-Trinity-Error-Code" not in resp.headers


def test_2973_the_cold_retry_keeps_the_depth(world, monkeypatch):
    """`run_resumable_turn` re-issues `execute_task` once when the resume
    JSONL is missing; that second row must carry the same depth."""
    import contextlib

    import services.session_turn_service as _STS
    import services.task_execution_service as _TES

    calls = []

    async def _execute_task(**kw):
        calls.append(kw)
        if len(calls) == 1:
            return SimpleNamespace(status="failed", error=_STS.RESUME_NOT_FOUND_MARKERS[0], session_id=None)
        return SimpleNamespace(status="success", error=None, session_id="new-uuid")

    @contextlib.asynccontextmanager
    async def _no_lock(*a, **kw):
        yield

    monkeypatch.setattr(_STS, "ResumeLock", _no_lock)
    monkeypatch.setattr(_STS, "supports_session_resume", lambda _a: True)
    monkeypatch.setattr(_TES, "get_task_execution_service", lambda: SimpleNamespace(execute_task=_execute_task))
    turn = asyncio.run(
        _STS.run_resumable_turn(
            agent_name=B, session_key="s1", message="m", cached_uuid="stale-uuid",
            triggered_by="session", lock_ttl=30, chain_depth=4,
        )
    )
    assert turn.fallback_fired
    assert [c.get("chain_depth") for c in calls] == [4, 4]


def test_2973_the_budget_alert_writes_a_notification_on_the_subscriber(world, monkeypatch):
    import services.monitoring_alerts as _MA
    from db.engine import get_engine
    from sqlalchemy import text

    svc = _MA.MonitoringAlertService()
    monkeypatch.setattr(svc, "_broadcast_alert", AsyncMock())
    notification_id = asyncio.run(svc.alert_event_dispatch_budget_exhausted(B, A, 120))
    with get_engine().connect() as conn:
        row = conn.execute(
            text("SELECT agent_name, priority, notification_type FROM agent_notifications WHERE id = :i"),
            {"i": notification_id},
        ).first()
    assert tuple(row) == (B, "high", "alert")
    svc._broadcast_alert.assert_awaited_once()
