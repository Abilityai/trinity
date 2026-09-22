"""#2806 — an agent-to-agent hop past the configured chain depth is refused.

Before this, nothing marked an agent-to-agent call as part of a chain: two
agents allowed to call each other could bounce a call A→B→A→B… until an
incidental limit happened to stop it.

The guard is keyed on the AUTHENTICATED principal (`current_user.agent_name`,
or `trinity-system` for a `scope=system` key), never on the opt-in
`X-Source-Agent` header or the model-typed `parent_execution_id` — so every
refusal test below sends NEITHER. That is the strip case: an agent that leaves
both out must still be counted.

    depth(child) = 1 + MAX(chain_depth) over the caller's `running` rows
    refused when depth > inter_agent_max_chain_depth (default 8, range 1-32)

Everything runs on the real schema through `tests/db_harness.py` and drives the
real service / router entries; the depth query is never mocked. Capacity and
the agent dispatch are the only doubles, because they talk to Redis and a
container.

Covered: `/chat` (`admit_chat_request`), `/task` (`dispatch_parallel_task`),
`/fan-out` (the router function), and all three over HTTP with a real minted
agent-scoped bearer.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from db_harness import (  # noqa: E402,F401
    count as _count,
    db_backend,
    run as _hrun,
    scalar as _scalar,
)

import routers.chat as _CHAT  # noqa: E402
import routers.fan_out as _FANOUT  # noqa: E402
import services.chat_execution_service as _CE  # noqa: E402
import services.dispatch_admission_service as _DISPATCH  # noqa: E402
import services.fan_out_service as _FOS  # noqa: E402
from database import db  # noqa: E402
from db.agents import SYSTEM_AGENT_NAME  # noqa: E402
from db_models import McpApiKeyCreate, UserCreate  # noqa: E402
from models import (  # noqa: E402
    ChatMessageRequest,
    FanOutRequest,
    ParallelTaskRequest,
    TaskExecutionStatus,
    User,
)
from services.chat_signals import ChatAdmission, InterAgentDepthExceeded  # noqa: E402
from services.settings_service import settings_service  # noqa: E402

pytestmark = pytest.mark.unit

KEY = "inter_agent_max_chain_depth"
OWNER = "depth-owner"
A, B = "depth-a", "depth-b"
C_FOREIGN = "depth-foreign"  # owned by someone else, not shared
GHOST = "depth-ghost"  # does not exist
CODE = "inter_agent_depth_exceeded"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


class _Capacity:
    """Stands in for the Redis-backed CapacityManager; records every acquire."""

    def __init__(self):
        self.acquire = AsyncMock(
            return_value=SimpleNamespace(state="admitted", queue_position=None)
        )


@pytest.fixture
def world(db_backend, monkeypatch):
    """Two agents owned by one user, one foreign agent, and spies on the
    Redis/container-touching seams. Returns a namespace of handles."""
    db.create_user(UserCreate(username=OWNER, role="user", email="owner@example.com"))
    db.create_user(
        UserCreate(username="depth-other", role="user", email="other@example.com")
    )
    db.register_agent_owner(A, OWNER)
    db.register_agent_owner(B, OWNER)
    db.register_agent_owner(C_FOREIGN, "depth-other")
    owner = db.get_user_by_username(OWNER)

    capacity = _Capacity()
    monkeypatch.setattr(_DISPATCH, "get_capacity_manager", lambda: capacity)
    monkeypatch.setattr(_DISPATCH, "dispatch_breaker_active", lambda _n: False)

    sync_dispatch = AsyncMock(return_value={"status": "success", "response": "ok"})
    async_dispatch = AsyncMock(return_value={"status": "accepted"})
    monkeypatch.setattr(_CE, "_dispatch_sync", sync_dispatch)
    monkeypatch.setattr(_CE, "_dispatch_async", async_dispatch)

    return SimpleNamespace(
        owner_id=owner["id"],
        capacity=capacity,
        sync_dispatch=sync_dispatch,
        async_dispatch=async_dispatch,
    )


def _agent_principal(world, agent=A, **extra) -> User:
    return User(
        id=world.owner_id,
        username=OWNER,
        email="owner@example.com",
        role="user",
        agent_name=agent,
        mcp_scope="agent",
        mcp_key_id="k-" + agent,
        mcp_key_name=f"agent-{agent}-key",
        **extra,
    )


def _human(world) -> User:
    return User(
        id=world.owner_id, username=OWNER, email="owner@example.com", role="user"
    )


def _running_row(agent: str, depth, status: str = "running") -> str:
    row = db.create_task_execution(
        agent_name=agent,
        message="in-flight turn",
        triggered_by="agent",
        chain_depth=depth,
    )
    if status != "running":
        _hrun(
            "UPDATE schedule_executions SET status=:s WHERE id=:i", s=status, i=row.id
        )
    return row.id


def _set_max(value: str) -> None:
    db.set_setting(KEY, value)


def _rows_for(agent: str, exclude_seeded_by: str = "in-flight turn") -> list:
    """Execution rows on `agent` other than the ones this test seeded."""
    from db.engine import get_engine
    from sqlalchemy import text

    with get_engine().connect() as conn:
        return [
            dict(r._mapping)
            for r in conn.execute(
                text(
                    "SELECT id, chain_depth, source_agent_name FROM schedule_executions "
                    "WHERE agent_name = :a AND message != :m"
                ),
                {"a": agent, "m": exclude_seeded_by},
            )
        ]


def _refusal_records(caller: str) -> tuple:
    audits = _count(
        "audit_log", "event_action = :a AND actor_id = :c", a=CODE, c=caller
    )
    failed_collab = _count(
        "agent_activities",
        "agent_name = :c AND activity_type = 'agent_collaboration' "
        "AND activity_state = 'failed' AND error LIKE :e",
        c=caller,
        e=f"%{CODE}%",
    )
    return audits, failed_collab


def _task(world, principal, target=B, idem="idem-1", **kw):
    return asyncio.run(
        _CE.dispatch_parallel_task(
            request=ParallelTaskRequest(message="do the thing", **kw),
            name=target,
            current_user=principal,
            container=SimpleNamespace(status="running"),
            x_source_agent=None,
            x_via_mcp="true",
            idempotency_key=idem,
            x_event_trigger=None,
            x_internal_secret=None,
        )
    )


def _chat_admit(principal, target=B, idem="idem-chat"):
    return asyncio.run(
        _DISPATCH.admit_chat_request(
            name=target,
            request=ChatMessageRequest(message="hello"),
            current_user=principal,
            x_source_agent=None,
            x_via_mcp="true",
            idempotency_key=idem,
        )
    )


# ---------------------------------------------------------------------------
# 1. the settings seam
# ---------------------------------------------------------------------------


def test_2806_the_max_is_an_ops_setting_read_through_the_real_chain(world):
    """A stored value that differs from the default must come back — a stub
    equal to the default would pass whether or not the seam is wired."""
    from config import OPS_SETTINGS_DEFAULTS, validate_ops_setting

    assert OPS_SETTINGS_DEFAULTS[KEY] == "8"
    _set_max("2")
    assert settings_service.get_ops_setting(KEY, int) == 2
    assert validate_ops_setting(KEY, "1") == "1"
    assert validate_ops_setting(KEY, "32") == "32"
    for bad in ("0", "33", "-1"):
        with pytest.raises(ValueError):
            validate_ops_setting(KEY, bad)


# ---------------------------------------------------------------------------
# 2-3. the strip case on every path: refused before key, slot and row
# ---------------------------------------------------------------------------


def test_2806_task_strip_case_is_refused_before_the_key_the_slot_and_the_row(world):
    _set_max("2")
    _running_row(A, 2)

    with pytest.raises(InterAgentDepthExceeded) as ei:
        _task(world, _agent_principal(world))

    e = ei.value
    assert (e.caller, e.target, e.depth, e.max_depth) == (A, B, 3, 2)
    assert e.detail()["error"] == CODE
    assert _rows_for(B) == []
    assert _count("idempotency_keys") == 0
    world.sync_dispatch.assert_not_called()
    world.async_dispatch.assert_not_called()
    assert _refusal_records(A) == (1, 1)


def test_2806_task_pull_routed_async_path_is_refused_too(world):
    """The #946 pull-routed sequential call is `/task` with async_mode=True."""
    _set_max("2")
    _running_row(A, 2)
    with pytest.raises(InterAgentDepthExceeded):
        _task(world, _agent_principal(world), async_mode=True)
    world.async_dispatch.assert_not_called()
    assert _rows_for(B) == []


def test_2806_chat_strip_case_is_refused_before_the_key_and_the_slot(world):
    _set_max("2")
    _running_row(A, 2)

    with pytest.raises(InterAgentDepthExceeded) as ei:
        _chat_admit(_agent_principal(world))

    assert (ei.value.depth, ei.value.max_depth) == (3, 2)
    world.capacity.acquire.assert_not_called()
    assert _count("idempotency_keys") == 0
    assert _rows_for(B) == []
    assert _refusal_records(A) == (1, 1)


def test_2806_fan_out_strip_case_is_refused_by_the_router_as_a_named_403(
    world, monkeypatch
):
    _set_max("2")
    _running_row(A, 2)
    service = MagicMock()
    service.execute = AsyncMock()
    monkeypatch.setattr(_FANOUT, "get_fan_out_service", lambda: service)

    with pytest.raises(HTTPException) as ei:
        asyncio.run(
            _FANOUT.fan_out(
                request=FanOutRequest(agent=B, tasks=[{"id": "t1", "message": "m"}]),
                name=B,
                current_user=_agent_principal(world),
                x_source_agent=None,
                x_via_mcp="true",
                idempotency_key="idem-fo",
            )
        )

    assert ei.value.status_code == 403
    assert ei.value.detail["error"] == CODE
    assert ei.value.headers["X-Trinity-Error-Code"] == CODE
    service.execute.assert_not_called()
    assert _count("idempotency_keys") == 0
    assert _refusal_records(A) == (1, 1)


# ---------------------------------------------------------------------------
# 4-5. over HTTP with a real minted bearer
# ---------------------------------------------------------------------------


@pytest.fixture
def client(world, monkeypatch):
    monkeypatch.setattr(
        _CHAT, "get_agent_container", lambda _n: SimpleNamespace(status="running")
    )
    app = FastAPI()
    app.include_router(_CHAT.router)
    app.include_router(_FANOUT.router)
    return TestClient(app)


def _agent_bearer(agent=A) -> dict:
    key = db.create_agent_mcp_api_key(agent, OWNER)
    return {"Authorization": f"Bearer {key.api_key}"}


def _assert_named_403(resp, depth, max_depth, target=B):
    assert resp.status_code == 403, resp.text
    assert resp.headers["X-Trinity-Error-Code"] == CODE
    detail = resp.json()["detail"]
    assert detail["error"] == CODE
    assert (detail["depth"], detail["max_depth"]) == (depth, max_depth)
    assert (detail["caller"], detail["target"]) == (A, target)
    assert "Do not retry" in detail["message"]


@pytest.mark.parametrize(
    "route,body",
    [
        ("chat", {"message": "hi"}),
        ("task", {"message": "hi"}),
        ("fan-out", {"agent": B, "tasks": [{"id": "t1", "message": "m"}]}),
    ],
)
def test_2806_agent_bearer_alone_is_refused_on_every_route(client, world, route, body):
    """Only `Authorization` — no X-Source-Agent, no parent_execution_id. The
    principal itself (get_current_user → agent_name) is what the guard reads."""
    _set_max("1")
    _running_row(A, 1)
    resp = client.post(f"/api/agents/{B}/{route}", json=body, headers=_agent_bearer())
    _assert_named_403(resp, 2, 1)
    assert _rows_for(B) == []


def test_2806_agent_bearer_under_the_limit_runs_and_stamps_the_child(
    client, world, monkeypatch
):
    """The admitted path on `/chat`: the router threads the admission's depth
    into the row it creates."""
    monkeypatch.setattr(
        _CE, "run_chat_turn", AsyncMock(return_value={"response": "ok"})
    )
    _running_row(A, 1)
    resp = client.post(
        f"/api/agents/{B}/chat", json={"message": "hi"}, headers=_agent_bearer()
    )
    assert resp.status_code == 200, resp.text
    assert [r["chain_depth"] for r in _rows_for(B)] == [2]


def test_2806_residual_a_a_user_scoped_key_held_by_an_agent_is_a_root(client, world):
    """Named residual (a): a hand-pasted user-scoped key resolves to the human
    owner, so its call is a root and stamps nothing. Pinned so that closing the
    residual flips this test on purpose, not by accident."""
    _set_max("1")
    _running_row(A, 1)
    key = db.create_mcp_api_key(OWNER, McpApiKeyCreate(name="hand-pasted"))
    resp = client.post(
        f"/api/agents/{B}/task",
        json={"message": "hi"},
        headers={"Authorization": f"Bearer {key.api_key}"},
    )
    assert resp.status_code == 200, resp.text
    assert [r["chain_depth"] for r in _rows_for(B)] == [None]


@pytest.mark.parametrize("target", [GHOST, C_FOREIGN])
def test_2806_the_uniform_404_wins_over_the_depth_403(client, world, target):
    """Invariant #8: the depth check runs after target access resolves, so a
    caller over the limit still cannot tell a missing agent from a forbidden
    one — both are the dependency's 404, never the depth 403."""
    _set_max("1")
    _running_row(A, 5)
    for route, body in (
        ("chat", {"message": "hi"}),
        ("task", {"message": "hi"}),
        ("fan-out", {"agent": target, "tasks": [{"id": "t", "message": "m"}]}),
    ):
        resp = client.post(
            f"/api/agents/{target}/{route}", json=body, headers=_agent_bearer()
        )
        assert resp.status_code == 404, (route, resp.text)
        assert "X-Trinity-Error-Code" not in resp.headers
    assert _refusal_records(A) == (0, 0)


# ---------------------------------------------------------------------------
# 6. controls
# ---------------------------------------------------------------------------


def _enforce(principal, target=B):
    return asyncio.run(
        _DISPATCH.enforce_inter_agent_depth(
            current_user=principal,
            target=target,
            endpoint=f"/api/agents/{target}/task",
            x_via_mcp="true",
        )
    )


def test_2806_under_the_limit_the_child_is_stamped_on_the_object_and_the_row(world):
    _running_row(A, 1)
    _task(world, _agent_principal(world))
    world.sync_dispatch.assert_awaited_once()
    rows = _rows_for(B)
    assert [r["chain_depth"] for r in rows] == [2]
    assert db.get_execution(rows[0]["id"]).chain_depth == 2


def test_2806_create_task_execution_returns_the_stamp_it_wrote(world):
    row = db.create_task_execution(agent_name=B, message="m", chain_depth=4)
    assert row.chain_depth == 4
    assert db.get_execution(row.id).chain_depth == 4


def test_2806_a_human_principal_reads_nothing_and_stamps_null(world, monkeypatch):
    probe = MagicMock(side_effect=AssertionError("human path must not read depth"))
    monkeypatch.setattr(db, "get_max_running_chain_depth", probe)
    assert _enforce(_human(world)) is None
    _task(world, _human(world))
    assert [r["chain_depth"] for r in _rows_for(B)] == [None]
    probe.assert_not_called()


def test_2806_depth_is_the_max_over_running_rows_only(world):
    _running_row(A, 0)
    _running_row(A, None)
    _running_row(A, 2)
    _running_row(A, 5, status="success")
    _running_row(A, 7, status="queued")
    assert db.get_max_running_chain_depth(A) == 2
    assert _enforce(_agent_principal(world)) == 3


def test_2806_no_running_row_counts_as_depth_one(world):
    """Named residual (b): a terminal session or orphan has no running row."""
    assert _enforce(_agent_principal(world)) == 1


def test_2806_default_max_is_8_so_depth_8_runs_and_9_is_refused(world):
    _running_row(A, 7)
    assert _enforce(_agent_principal(world)) == 8
    _running_row(A, 8)
    with pytest.raises(InterAgentDepthExceeded) as ei:
        _enforce(_agent_principal(world))
    assert (ei.value.depth, ei.value.max_depth) == (9, 8)


@pytest.mark.parametrize(
    "stored,effective", [("abc", 8), ("99", 32), ("0", 1), ("", 8)]
)
def test_2806_a_bad_stored_max_is_defaulted_or_clamped_on_use(world, stored, effective):
    """`resolve_ops_setting` does not validate the row tier, so a value written
    straight to the DB is clamped (or defaulted) where it is used."""
    _set_max(stored)
    _running_row(A, effective - 1)
    assert _enforce(_agent_principal(world)) == effective
    _running_row(A, effective)
    with pytest.raises(InterAgentDepthExceeded) as ei:
        _enforce(_agent_principal(world))
    assert ei.value.max_depth == effective


def test_2806_a_settings_read_error_falls_back_to_the_default(world, monkeypatch):
    monkeypatch.setattr(
        settings_service,
        "get_ops_setting",
        MagicMock(side_effect=RuntimeError("settings store down")),
    )
    _running_row(A, 7)
    assert _enforce(_agent_principal(world)) == 8
    _running_row(A, 8)
    with pytest.raises(InterAgentDepthExceeded):
        _enforce(_agent_principal(world))


def test_2806_the_system_agent_is_a_caller_like_any_agent(world):
    """Taste call 1: a `scope=system` key has no agent_name but is counted as
    `trinity-system`, so a hop through the system agent does not reset the chain."""
    system = User(
        id=world.owner_id,
        username=OWNER,
        email="owner@example.com",
        role="admin",
        mcp_scope="system",
    )
    _running_row(SYSTEM_AGENT_NAME, 0)
    _task(world, system)
    assert [r["chain_depth"] for r in _rows_for(B)] == [1]

    _running_row(SYSTEM_AGENT_NAME, 8)
    with pytest.raises(InterAgentDepthExceeded) as ei:
        _task(world, system, idem="idem-2")
    assert ei.value.caller == SYSTEM_AGENT_NAME


def test_2806_fan_out_subtasks_carry_the_depth_captured_at_request_time(
    world, monkeypatch
):
    """The router hands the depth to the service; the service stamps every
    subtask row with it, including one granted a slot later."""
    fake_task_service = MagicMock()
    fake_task_service.execute_task = AsyncMock(
        return_value=SimpleNamespace(
            status=TaskExecutionStatus.SUCCESS, error_code=None
        )
    )
    monkeypatch.setattr(_FOS, "get_task_execution_service", lambda: fake_task_service)
    monkeypatch.setattr(_FOS, "join_fan_out_on_terminal", AsyncMock(return_value=True))

    asyncio.run(
        _FOS.FanOutService()._dispatch_all(
            fan_out_id="fo_depth",
            agent_name=B,
            tasks=[_FOS.FanOutTaskInput(id=f"t{i}", message="m") for i in range(3)],
            error_codes={},
            max_concurrency=1,
            model=None,
            system_prompt=None,
            allowed_tools=None,
            source_user_id=world.owner_id,
            source_user_email=None,
            source_agent_name=A,
            source_mcp_key_id=None,
            source_mcp_key_name=None,
            chain_depth=3,
        )
    )
    assert sorted(r["chain_depth"] for r in _rows_for(B)) == [3, 3, 3]


def test_2806_fan_out_router_passes_the_admitted_depth_to_the_service(
    world, monkeypatch
):
    _running_row(A, 1)
    service = MagicMock()
    service.execute = AsyncMock(
        return_value=_FOS.FanOutResult(
            fan_out_id="fo_x",
            status="accepted",
            total=1,
            completed=0,
            failed=0,
            results=[],
        )
    )
    monkeypatch.setattr(_FANOUT, "get_fan_out_service", lambda: service)
    asyncio.run(
        _FANOUT.fan_out(
            request=FanOutRequest(agent=B, tasks=[{"id": "t1", "message": "m"}]),
            name=B,
            current_user=_agent_principal(world),
            x_source_agent=None,
            x_via_mcp="true",
            idempotency_key=None,
        )
    )
    assert service.execute.await_args.kwargs["chain_depth"] == 2


def test_2806_chat_admission_carries_the_depth(world):
    _running_row(A, 3)
    admission = _chat_admit(_agent_principal(world))
    assert isinstance(admission, ChatAdmission)
    assert admission.chain_depth == 4
    world.capacity.acquire.assert_awaited_once()


# ---------------------------------------------------------------------------
# 7. recording is best-effort; the refusal is not
# ---------------------------------------------------------------------------


def test_2806_the_refusal_is_raised_even_when_recording_fails(world, monkeypatch):
    from services.activity_service import activity_service
    from services.platform_audit_service import platform_audit_service

    monkeypatch.setattr(
        platform_audit_service, "log", AsyncMock(side_effect=RuntimeError("audit down"))
    )
    monkeypatch.setattr(
        activity_service,
        "track_activity",
        AsyncMock(side_effect=RuntimeError("activity down")),
    )
    _set_max("2")
    _running_row(A, 2)
    with pytest.raises(InterAgentDepthExceeded):
        _task(world, _agent_principal(world))
    assert _rows_for(B) == []
    assert _count("idempotency_keys") == 0


# ---------------------------------------------------------------------------
# 8. wiring guard: the check precedes every claim / acquire / insert
# ---------------------------------------------------------------------------

_GATES = {
    "idempotency_service.begin",
    "begin_task_idempotency",
    "capacity.acquire",
    "create_task_execution_and_activities",
    "service.execute",
}


def _call_names(fn) -> list:
    """Dotted names of every call in `fn`, in source order. Parsed from the
    source with the docstring and comments dropped by the AST itself — never
    `ast.unparse`, which renormalizes the text a substring check reads."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            parts = []
            while isinstance(f, ast.Attribute):
                parts.append(f.attr)
                f = f.value
            if isinstance(f, ast.Name):
                parts.append(f.id)
            calls.append((node.lineno, node.col_offset, ".".join(reversed(parts))))
    return [name for _l, _c, name in sorted(calls)]


@pytest.mark.parametrize(
    "fn",
    [
        _DISPATCH.admit_chat_request,
        _CE.dispatch_parallel_task,
        _FANOUT.fan_out,
    ],
)
def test_2806_the_depth_check_precedes_every_claim_acquire_and_insert(fn):
    names = _call_names(fn)
    guard = [i for i, n in enumerate(names) if n.endswith("enforce_inter_agent_depth")]
    assert len(guard) == 1, f"{fn.__qualname__}: expected one depth check, got {names}"
    gates = [i for i, n in enumerate(names) if any(n.endswith(g) for g in _GATES)]
    assert gates, f"{fn.__qualname__}: no gate found — the guard list is stale"
    assert guard[0] < min(gates), f"{fn.__qualname__}: {names}"


def test_2806_self_task_is_a_hop_too(world):
    """SELF-EXEC-001 sibling: an agent's `/task` to ITSELF (with its own
    X-Source-Agent) is refused on the same rule — a self-task loop is a chain."""
    _set_max("2")
    _running_row(A, 2)
    with pytest.raises(InterAgentDepthExceeded) as ei:
        asyncio.run(
            _CE.dispatch_parallel_task(
                request=ParallelTaskRequest(message="again"),
                name=A,
                current_user=_agent_principal(world),
                container=SimpleNamespace(status="running"),
                x_source_agent=A,
                x_via_mcp="true",
                idempotency_key=None,
                x_event_trigger=None,
                x_internal_secret=None,
            )
        )
    assert (ei.value.caller, ei.value.target) == (A, A)
    world.sync_dispatch.assert_not_called()
