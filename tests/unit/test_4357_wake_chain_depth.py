"""#4357 — a wake, and a reminder set from one, stand where the agent stood.

An agent that delegates with ``async=true`` is woken when the run ends
(ent#1102). The wake was stamped ``callee + 1``, and the next delegation from
the wake turn is ``1 + MAX(running)`` — which now includes the wake row. So
every delegate→wake round added TWO to the depth (``A0 → B1 → wakeA2 → B3 →
wakeA4 …``), and an orchestrator that kept working through wakes was refused
``inter_agent_depth_exceeded`` after about four rounds, with no person and no
genuine chain anywhere. A reminder set from such a wake was refused too: its
own row is a root, but the guard reads the MAX over every running row of the
agent, and the deep wake was one of them.

A wake continues the delegator's own work, so it now carries the delegator's
standing: ``max(callee − 1, MAX(running caller), 0)``, NULL counted as 0.

These tests drive the REAL ``caller_wake_service.maybe_wake_caller`` and the
REAL ``/task`` admission (``dispatch_parallel_task`` → ``enforce_inter_agent_depth``)
over one real database, so the wake's stamp and the guard's MAX read the same
rows. Only the seams that would start a container are doubles: the wake's
dispatch writes the RUNNING row ``dispatch_and_await_terminal`` would write,
with the depth it was handed.

Depth no longer bounds how many wakes follow one another, so a per-pair hourly
wake budget does (``caller_wake_max_per_hour``, the #2973 event-budget shape):
a wake past it is skipped and audited ``skipped_budget`` with one notification
per window; Redis trouble fails open; a value of 0 or less is unlimited.

Pinned as unchanged (AC 2): a synchronous chain is still refused past the max;
an answered or ended ask still starts a root turn. The ``agent.task.*`` event
depth and the loop depth stay pinned in ``test_2973_depth_new_roots.py``.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import fakeredis
import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from db.write_params import TaskExecutionFields  # noqa: E402
from db_harness import db_backend, run as _hrun  # noqa: E402,F401

# Real modules, bound BEFORE any test stubs `database` / `services.*` in
# sys.modules (the operator-resume pin below does).
import services.caller_wake_service as _WAKE  # noqa: E402
import services.chat_execution_service as _CE  # noqa: E402
import services.dispatch_admission_service as _DISPATCH  # noqa: E402
import services.docker_utils  # noqa: E402,F401
import services.operator_queue_service  # noqa: E402,F401
import services.operator_resume_service as _ORS  # noqa: E402
from database import db  # noqa: E402
from db_models import UserCreate  # noqa: E402
from models import ParallelTaskRequest, User  # noqa: E402
from services.chat_signals import InterAgentDepthExceeded  # noqa: E402

pytestmark = pytest.mark.unit

KEY = "inter_agent_max_chain_depth"
MAX = 8
OWNER = "zq4357-owner"
A, B = "zq4357-orchestrator", "zq4357-worker"
ROUNDS = 20


# ---------------------------------------------------------------------------
# the world: two agents that may call each other, one real DB
# ---------------------------------------------------------------------------


@pytest.fixture
def world(db_backend, monkeypatch):
    db.create_user(UserCreate(username=OWNER, role="operator", email="owner@example.com"))
    db.register_agent_owner(A, OWNER)
    db.register_agent_owner(B, OWNER)
    db.add_agent_permission(A, B, created_by=OWNER)
    db.add_agent_permission(B, A, created_by=OWNER)
    # Pinned, not inherited: the default is 8 today, but the rounds below are
    # counted against this number.
    db.set_setting(KEY, str(MAX))
    owner = db.get_user_by_username(OWNER)

    capacity = SimpleNamespace(
        acquire=AsyncMock(return_value=SimpleNamespace(state="admitted", queue_position=None))
    )
    monkeypatch.setattr(_DISPATCH, "get_capacity_manager", lambda: capacity)
    monkeypatch.setattr(_DISPATCH, "dispatch_breaker_active", lambda _n: False)
    monkeypatch.setattr(_CE, "_dispatch_sync", AsyncMock(return_value={"status": "success", "response": "ok"}))
    monkeypatch.setattr(_CE, "_dispatch_async", AsyncMock(return_value={"status": "accepted"}))

    wakes = []

    async def _dispatch_wake(**kwargs):
        # What `dispatch_and_await_terminal` writes before the agent runs: a
        # RUNNING row on the caller carrying the depth it was handed.
        row = db.create_task_execution(
            agent_name=kwargs["agent_name"],
            message=kwargs["message"],
            triggered_by=kwargs["triggered_by"],
            fields=TaskExecutionFields(chain_depth=kwargs.get("chain_depth")),
        )
        wakes.append({"id": row.id, "chain_depth": kwargs.get("chain_depth")})
        return SimpleNamespace(execution_id=row.id, status="running", error=None)

    async def _state(_agent):
        return "running"

    async def _audit(**_kw):
        return None

    def _policy(_agent):
        from services.wake_policy import WakeVerdict
        return WakeVerdict(enabled=True, source="instance")

    monkeypatch.setattr(_WAKE, "_dispatch_wake", _dispatch_wake)
    monkeypatch.setattr(_WAKE, "_container_state", _state)
    monkeypatch.setattr(_WAKE, "_audit", _audit)
    monkeypatch.setattr(_WAKE, "_resolve_policy", _policy)
    # The 15 s bucket is the storm bound and has its own tests; here each round
    # is a separate window unless a test says otherwise.
    window = {"replay": False}
    monkeypatch.setattr(
        _WAKE, "_begin_idempotency",
        lambda scope, key: SimpleNamespace(replay=window["replay"], enabled=True, in_flight=False),
    )
    monkeypatch.setattr(_WAKE, "_complete_idempotency", lambda *a, **k: None)
    monkeypatch.setattr(_WAKE, "_fail_idempotency", lambda *a, **k: None)
    # The hourly wake budget (A2) counts in Redis; a fresh in-memory one per test.
    redis = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(_WAKE, "_wake_budget_redis", lambda: redis)

    return SimpleNamespace(owner_id=owner["id"], wakes=wakes, window=window, redis=redis)


def _principal(world, agent) -> User:
    """The agent's own injected key — the only thing the guard keys on."""
    return User(
        id=world.owner_id, username=OWNER, email="owner@example.com", role="operator",
        agent_name=agent, mcp_scope="agent", mcp_key_id="k-" + agent,
        mcp_key_name=f"agent-{agent}-key",
    )


def _ids_on(agent: str) -> set:
    from db.engine import get_engine
    from sqlalchemy import text

    with get_engine().connect() as conn:
        return {
            r[0] for r in conn.execute(
                text("SELECT id FROM schedule_executions WHERE agent_name = :a"), {"a": agent}
            )
        }


_N = {"idem": 0}


def _delegate(world, caller: str, target: str, *, async_mode: bool = True) -> str:
    """``chat_with_agent`` from ``caller``'s key through the real ``/task``
    admission. Returns the child row's id."""
    _N["idem"] += 1
    before = _ids_on(target)
    asyncio.run(
        _CE.dispatch_parallel_task(
            request=ParallelTaskRequest(message="do the thing", async_mode=async_mode),
            name=target,
            current_user=_principal(world, caller),
            container=SimpleNamespace(status="running"),
            # What `dependencies.resolve_source_agent` hands the service for an
            # agent key: its own name (ent#614). It is what makes the row
            # wake-eligible — `source_agent_name` is the caller to wake.
            x_source_agent=caller,
            x_via_mcp="true",
            idempotency_key=f"zq4357-{_N['idem']}",
            x_event_trigger=None,
            x_internal_secret=None,
        )
    )
    (child,) = _ids_on(target) - before
    return child


def _end(execution_id: str) -> None:
    _hrun("UPDATE schedule_executions SET status='completed' WHERE id=:i", i=execution_id)


def _depth(execution_id: str):
    return db.get_execution(execution_id).chain_depth


def _person_turn(agent: str) -> str:
    """A turn a person started: a root, no depth."""
    return db.create_task_execution(agent_name=agent, message="a person asked", triggered_by="chat").id


def _wake_for(execution_id: str) -> str:
    woken = asyncio.run(_WAKE.maybe_wake_caller(execution_id))
    assert woken, "the delegator should have been woken"
    return woken


# ---------------------------------------------------------------------------
# AC 1 — an orchestrator working through wakes is never refused
# ---------------------------------------------------------------------------


def test_twenty_delegate_wake_rounds_are_all_admitted(world):
    """Each round's delegation is made while that round's wake row is still
    RUNNING (the turn is the one delegating), then the wake turn ends, then the
    callee ends and wakes the delegator again."""
    turn = _person_turn(A)
    depths = []
    for round_no in range(1, ROUNDS + 1):
        try:
            child = _delegate(world, A, B)
        except InterAgentDepthExceeded as exc:
            pytest.fail(f"refused at round {round_no}: {exc} (depths so far {depths})")
        depths.append(_depth(child))
        _end(turn)                            # the delegating turn finishes
        _end(child)                           # the delegated run ends ...
        turn = _wake_for(child)               # ... and wakes the delegator
    assert depths == [1] * ROUNDS
    assert {w["chain_depth"] for w in world.wakes} == {0}


def test_the_wake_stands_at_the_delegators_standing_not_above_it(world):
    """A delegator that genuinely stood at 3 is woken at 3 — never laundered
    to 0, never pushed to 5."""
    deep = db.create_task_execution(
        agent_name=A, message="mid-chain", triggered_by="agent",
        fields=TaskExecutionFields(chain_depth=3),
    ).id
    child = _delegate(world, A, B)
    assert _depth(child) == 4
    _end(deep)
    _end(child)
    wake = _wake_for(child)
    assert _depth(wake) == 3
    assert _depth(_delegate(world, A, B)) == 4


def test_a_callee_at_the_max_wakes_its_delegator_one_below_it(world):
    """The wake was never checked against the max and could be stamped 9; it
    now always leaves the woken turn room for one hop."""
    child = db.create_task_execution(
        agent_name=B, message="deep callee", triggered_by="agent",
        fields=TaskExecutionFields(chain_depth=MAX, source_agent_name=A),
    ).id
    _end(child)
    wake = _wake_for(child)
    assert _depth(wake) == MAX - 1
    assert _depth(_delegate(world, A, B)) == MAX


# ---------------------------------------------------------------------------
# AC 3 — a reminder set from a wake starts at the agent's own standing
# ---------------------------------------------------------------------------


def test_a_reminder_fired_while_the_last_wake_still_runs_is_admitted(world):
    """Four rounds put the wake at the max under the old ``callee + 1`` stamp.
    The last wake sets a reminder and is still running when it fires; the
    reminder's row is a root (``scheduler._execute_reminder`` passes no
    depth), and its hop is checked against every running row of the agent."""
    turn = _person_turn(A)
    for _round in range(4):
        child = _delegate(world, A, B)
        _end(turn)
        _end(child)
        turn = _wake_for(child)
    # `turn` (the last wake) is still RUNNING; the reminder fires now.
    reminder = db.create_task_execution(agent_name=A, message="reminder", triggered_by="reminder").id
    assert _depth(reminder) is None
    child = _delegate(world, A, B)            # refused at HEAD: 1 + MAX(running A) = 9
    assert _depth(child) == 1


# ---------------------------------------------------------------------------
# the bucket collapse: one wake answers two endings, at the deeper standing
# ---------------------------------------------------------------------------


def test_a_collapsed_window_wakes_at_the_deeper_standing_while_it_still_runs(world):
    """A shallow and a deep delegation end in the same 15 s window. The first
    ending claims the window and is the one read; the deep delegator turn is
    still running, so the wake must not stand below it."""
    shallow = _delegate(world, A, B)                  # from no turn: depth 1
    deep_turn = db.create_task_execution(
        agent_name=A, message="mid-chain", triggered_by="agent",
        fields=TaskExecutionFields(chain_depth=5),
    ).id
    deep = _delegate(world, A, B)                     # 1 + MAX(running A) = 6
    assert (_depth(shallow), _depth(deep)) == (1, 6)
    _end(shallow)
    _end(deep)
    wake = _wake_for(shallow)
    world.window["replay"] = True                     # the deep ending is collapsed
    assert asyncio.run(_WAKE.maybe_wake_caller(deep)) is None
    assert _depth(wake) == 5
    assert db.get_max_running_chain_depth(A) == 5     # deep_turn still runs
    assert deep_turn


# ---------------------------------------------------------------------------
# AC 2 — genuine chains are still stopped (unchanged)
# ---------------------------------------------------------------------------


def test_a_synchronous_chain_is_still_refused_at_the_hop_past_the_max(world):
    """A0 → B1 → A2 → … with every turn still running: hops from running turns
    grow by one each, and the ninth is refused — no wake can lower that."""
    _person_turn(A)
    caller, target = A, B
    for hop in range(1, MAX + 1):
        child = _delegate(world, caller, target, async_mode=False)
        _hrun("UPDATE schedule_executions SET status='running' WHERE id=:i", i=child)
        assert _depth(child) == hop
        caller, target = target, caller
    with pytest.raises(InterAgentDepthExceeded) as ei:
        _delegate(world, caller, target, async_mode=False)
    assert (ei.value.depth, ei.value.max_depth) == (MAX + 1, MAX)


# ---------------------------------------------------------------------------
# a person's answer or cancel still starts from zero (unchanged)
# ---------------------------------------------------------------------------


def _resume_stubs(monkeypatch):
    """The resume service's lazily imported collaborators (the test_ent886 shape)."""
    import services as _services_pkg

    calls = []

    def _mod(dotted, module):
        monkeypatch.setitem(sys.modules, dotted, module)
        if dotted.startswith("services."):
            monkeypatch.setattr(_services_pkg, dotted.split(".", 1)[1], module, raising=False)

    monkeypatch.setitem(sys.modules, "database", SimpleNamespace(
        db=SimpleNamespace(get_operator_resume_enabled=lambda name: True)))
    import services.wake_policy as wake_policy
    monkeypatch.setattr(wake_policy, "db", SimpleNamespace(
        get_wake_on_ending=lambda name: None,
        get_operator_resume_enabled=lambda name: True,
        get_operator_resume_override=lambda name: True,
        get_setting_value=lambda key, default=None: default,
    ))
    _mod("services.idempotency_service", SimpleNamespace(
        begin=lambda scope, key: SimpleNamespace(replay=False, in_flight=False),
        complete=lambda *a, **k: None, fail=lambda *a, **k: None,
        make_agent_scope=lambda name: f"agent:{name}"))

    class _Audit:
        async def log(self, **kw):
            return None

    _mod("services.platform_audit_service", SimpleNamespace(
        platform_audit_service=_Audit(), AuditEventType=SimpleNamespace(EXECUTION="execution")))

    async def _adapter(**kw):
        calls.append(kw)
        return SimpleNamespace(execution_id=f"exec-{len(calls)}", status="success", error=None)

    _mod("services.task_execution_service", SimpleNamespace(
        dispatch_and_await_terminal=_adapter, get_task_execution_service=lambda: None))
    _mod("services.docker_service", SimpleNamespace(agent_container_state=lambda name: "running"))
    return calls


def test_an_answered_ask_starts_a_root_turn(monkeypatch):
    calls = _resume_stubs(monkeypatch)
    item = {"id": "zq4357-item", "agent_name": A, "title": "Approve?", "question": "Send?",
            "context": {}}
    asyncio.run(_ORS.maybe_dispatch_resume(item, response="approve", responded_by_email=None))
    assert len(calls) == 1
    assert calls[0].get("chain_depth") is None


def test_an_ended_ask_starts_a_root_turn(monkeypatch):
    calls = _resume_stubs(monkeypatch)
    row = {"id": "uuid-zq4357", "request_id": "zq4357-ask", "agent_name": A, "type": "approval",
           "status": "cancelled", "title": "Pay", "question": "Release?", "sync_state": "confirmed"}
    asyncio.run(_ORS.maybe_dispatch_ending([row], disposition="cancelled",
                                           disposed_by_email=None, reason="no"))
    assert len(calls) == 1
    assert calls[0].get("chain_depth") is None


# ---------------------------------------------------------------------------
# A2 — the per-pair hourly wake budget replaces depth as the bound on wakes
# ---------------------------------------------------------------------------

BUDGET_KEY = "caller_wake_max_per_hour"


@pytest.fixture
def budget(world, monkeypatch):
    """Records the wake audits and the budget alert; the limit is a real row."""
    audits = []

    async def _audit(**kw):
        audits.append(kw)

    alert = AsyncMock(return_value="n-1")
    import services.monitoring_alerts as _MA

    monkeypatch.setattr(_WAKE, "_audit", _audit)
    monkeypatch.setattr(
        _MA, "get_alert_service",
        lambda: SimpleNamespace(alert_caller_wake_budget_exhausted=alert),
    )
    return SimpleNamespace(audits=audits, alert=alert)


def _ended_delegation() -> str:
    """A run A delegated to B, already ended — owed a wake."""
    row = db.create_task_execution(
        agent_name=B, message="work", triggered_by="agent",
        fields=TaskExecutionFields(chain_depth=1, source_agent_name=A),
    ).id
    _end(row)
    return row


def test_a_wake_past_the_hourly_budget_is_skipped_and_audited(world, budget):
    db.set_setting(BUDGET_KEY, "3")
    woken = [asyncio.run(_WAKE.maybe_wake_caller(_ended_delegation())) for _ in range(5)]
    assert all(woken[:3]) and woken[3:] == [None, None]
    assert len(world.wakes) == 3
    skipped = [a for a in budget.audits if a.get("status") == "skipped_budget"]
    assert len(skipped) == 2
    assert {(a["caller"], a["callee"], a["limit"]) for a in skipped} == {(A, B, 3)}
    assert all(a["execution_id"] is None for a in skipped)
    # One notification per window, to the agent that was not woken.
    budget.alert.assert_awaited_once_with(A, B, 3)
    key = f"trinity:wake_fires:{B}:{A}"
    assert world.redis.get(key) == "5"
    assert 0 < world.redis.ttl(key) <= 3600


def test_the_default_budget_is_120_an_hour():
    import config

    assert config.OPS_SETTINGS_DEFAULTS[BUDGET_KEY] == "120"
    assert config.ENV_BACKED_OPS_KEYS[BUDGET_KEY] == "CALLER_WAKE_MAX_PER_HOUR"
    from services.settings_service import OPS_SETTINGS_DESCRIPTIONS

    assert "120" in OPS_SETTINGS_DESCRIPTIONS[BUDGET_KEY]


class _BrokenRedis:
    def pipeline(self, *a, **k):
        raise ConnectionError("redis is down")


@pytest.mark.parametrize("client", [None, _BrokenRedis()], ids=["no-client", "raises"])
def test_redis_down_the_wake_is_still_dispatched(world, budget, monkeypatch, client):
    db.set_setting(BUDGET_KEY, "1")
    monkeypatch.setattr(_WAKE, "_wake_budget_redis", lambda: client)
    for _ in range(3):
        assert asyncio.run(_WAKE.maybe_wake_caller(_ended_delegation()))
    assert len(world.wakes) == 3
    assert not [a for a in budget.audits if a.get("status") == "skipped_budget"]
    budget.alert.assert_not_awaited()


@pytest.mark.parametrize("value", ["0", "-5"])
def test_a_budget_of_zero_or_less_is_unlimited(world, budget, value):
    db.set_setting(BUDGET_KEY, value)
    for _ in range(10):
        assert asyncio.run(_WAKE.maybe_wake_caller(_ended_delegation()))
    assert len(world.wakes) == 10
    assert not [a for a in budget.audits if a.get("status") == "skipped_budget"]
    # Unlimited spends nothing: no counter is written.
    assert world.redis.keys("trinity:wake_fires:*") == []


def test_a_collapsed_ending_does_not_spend_the_budget(world, budget):
    """The 15 s window answers a second ending with no turn; only wakes count."""
    db.set_setting(BUDGET_KEY, "2")
    assert asyncio.run(_WAKE.maybe_wake_caller(_ended_delegation()))
    world.window["replay"] = True
    for _ in range(3):
        assert asyncio.run(_WAKE.maybe_wake_caller(_ended_delegation())) is None
    world.window["replay"] = False
    assert asyncio.run(_WAKE.maybe_wake_caller(_ended_delegation()))
    assert len(world.wakes) == 2
    assert world.redis.get(f"trinity:wake_fires:{B}:{A}") == "2"
