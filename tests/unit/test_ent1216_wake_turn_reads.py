"""ent#1216 — an agent's key keeps its execution reads while a wake turn runs.

When a run an agent delegated ends, the platform wakes the delegator with a
turn of its own (`caller_wake`, ent#1102). That turn had nobody on its row, so
`turn_tier_service.turn_actor` rated it a visitor's, and the ledger rule
(`dependencies.acting_turn_user_id`, ent#926) then hid every execution row from
the agent's key for as long as the wake turn ran — including from the wake turn
itself, which was woken to read the result.

The wake now carries the delegation's person when that person is owner tier on
the delegator, and nobody otherwise. So a wake for the owner's own work reads
at the full view, and every other wake (a shared person's, a visitor's, one for
a person who cannot open the delegator) is rated exactly as before.

Everything here runs the real chain on a real database: the `/task` admission
writes the callee row, the real `maybe_wake_caller` decides what the wake
carries, the ledger is `db.get_running_turn_attribution`, and the reads are
`dependencies.acting_turn_user_id` / `viewer_row_scope`. Only the wake's
dispatch is replaced, by a writer that records the RUNNING row from the kwargs
the service handed it.
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

import dependencies as _DEP  # noqa: E402
import services.caller_wake_service as _WAKE  # noqa: E402
import services.chat_execution_service as _CE  # noqa: E402
import services.dispatch_admission_service as _DISPATCH  # noqa: E402
import services.docker_utils  # noqa: E402,F401
from database import db  # noqa: E402
from db_models import UserCreate  # noqa: E402
from models import ParallelTaskRequest, User  # noqa: E402
from services import memory_subject, turn_tier_service  # noqa: E402

pytestmark = pytest.mark.unit

OWNER = "zq1216-owner"
OWNER_EMAIL = "zq1216-owner@example.com"
MATE = "zq1216-mate"
MATE_EMAIL = "zq1216-mate@example.com"
STRANGER = "zq1216-stranger"
A, B = "zq1216-delegator", "zq1216-worker"

#: The row columns a dispatch kwarg of the same name is written to.
_ROW_KWARGS = ("source_user_id", "source_user_email", "source_agent_name", "chain_depth")


@pytest.fixture
def world(db_backend, monkeypatch):
    db.create_user(UserCreate(username=OWNER, role="operator", email=OWNER_EMAIL))
    db.create_user(UserCreate(username=MATE, role="user", email=MATE_EMAIL))
    db.create_user(UserCreate(username=STRANGER, role="user", email="zq1216-stranger@example.com"))
    db.register_agent_owner(A, OWNER)
    db.register_agent_owner(B, OWNER)
    db.add_agent_permission(A, B, created_by=OWNER)
    db.share_agent(A, OWNER, MATE_EMAIL)

    capacity = SimpleNamespace(
        acquire=AsyncMock(return_value=SimpleNamespace(state="admitted", queue_position=None))
    )
    monkeypatch.setattr(_DISPATCH, "get_capacity_manager", lambda: capacity)
    monkeypatch.setattr(_DISPATCH, "dispatch_breaker_active", lambda _n: False)
    monkeypatch.setattr(_CE, "_dispatch_sync", AsyncMock(return_value={"status": "success", "response": "ok"}))
    monkeypatch.setattr(_CE, "_dispatch_async", AsyncMock(return_value={"status": "accepted"}))

    sent, audits = [], []

    async def _dispatch_wake(**kwargs):
        # What the dispatch writes before the agent runs: a RUNNING row on the
        # delegator carrying exactly the attribution it was handed.
        sent.append(kwargs)
        row = db.create_task_execution(
            agent_name=kwargs["agent_name"],
            message=kwargs["message"],
            triggered_by=kwargs["triggered_by"],
            fields=TaskExecutionFields(**{k: kwargs[k] for k in _ROW_KWARGS if k in kwargs}),
        )
        return SimpleNamespace(execution_id=row.id, status="running", error=None)

    async def _state(_agent):
        return "running"

    async def _audit(**kw):
        audits.append(kw)

    def _policy(_agent):
        from services.wake_policy import WakeVerdict
        return WakeVerdict(enabled=True, source="instance")

    monkeypatch.setattr(_WAKE, "_dispatch_wake", _dispatch_wake)
    monkeypatch.setattr(_WAKE, "_container_state", _state)
    monkeypatch.setattr(_WAKE, "_audit", _audit)
    monkeypatch.setattr(_WAKE, "_resolve_policy", _policy)
    monkeypatch.setattr(
        _WAKE, "_begin_idempotency",
        lambda scope, key: SimpleNamespace(replay=False, enabled=True, in_flight=False),
    )
    monkeypatch.setattr(_WAKE, "_complete_idempotency", lambda *a, **k: None)
    monkeypatch.setattr(_WAKE, "_fail_idempotency", lambda *a, **k: None)
    redis = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(_WAKE, "_wake_budget_redis", lambda: redis)

    ids = {u: db.get_user_by_username(u)["id"] for u in (OWNER, MATE, STRANGER)}
    return SimpleNamespace(ids=ids, sent=sent, audits=audits)


def _key(world, agent=A) -> User:
    """The agent's own injected key: it resolves to the agent's owner, and
    names no turn (the header is absent, as from a shell `curl`)."""
    return User(
        id=world.ids[OWNER], username=OWNER, email=OWNER_EMAIL, role="operator",
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


def _delegate(world) -> str:
    """`chat_with_agent(async)` from A's key through the real `/task` admission.
    Returns the callee row's id."""
    _N["idem"] += 1
    before = _ids_on(B)
    asyncio.run(
        _CE.dispatch_parallel_task(
            request=ParallelTaskRequest(message="do the thing", async_mode=True),
            name=B,
            current_user=_key(world),
            container=SimpleNamespace(status="running"),
            x_source_agent=A,
            x_via_mcp="true",
            idempotency_key=f"zq1216-{_N['idem']}",
            x_event_trigger=None,
            x_internal_secret=None,
        )
    )
    (child,) = _ids_on(B) - before
    return child


def _delegated_for(person) -> str:
    """A callee row A dispatched for `person` (a user id or None), as the
    admission writes one when A's delegating turn acted for that person."""
    return db.create_task_execution(
        agent_name=B, message="do the thing", triggered_by="agent",
        fields=TaskExecutionFields(source_user_id=person, source_agent_name=A),
    ).id


def _end(execution_id: str) -> None:
    _hrun("UPDATE schedule_executions SET status='completed' WHERE id=:i", i=execution_id)


def _wake(execution_id: str) -> str:
    woken = asyncio.run(_WAKE.maybe_wake_caller(execution_id))
    assert woken, "the delegator should have been woken"
    return woken


def _rating(wake_id: str):
    return turn_tier_service.turn_actor(db, A, wake_id)


# ---------------------------------------------------------------------------
# the defect: the owner's own delegation
# ---------------------------------------------------------------------------


def test_the_callee_row_of_an_owners_delegation_carries_the_owner(world):
    """The premise the fix stands on, through the real admission: a key that
    names no share turn dispatches as its agent's owner."""
    child = _delegate(world)
    assert db.get_execution(child).source_user_id == world.ids[OWNER]


def test_a_wake_for_the_owners_delegation_is_an_owner_turn(world):
    child = _delegate(world)
    _end(child)
    wake_id = _wake(child)
    assert _rating(wake_id) == (turn_tier_service.OWNER, None, None)


def test_the_agents_key_reads_at_the_full_view_while_that_wake_runs(world):
    """The reported failure: `get_execution_result` 404 and empty lists for as
    long as the wake turn ran. `None` is the full view; `NO_ROWS_USER_ID` is
    what every one of those reads was scoped to."""
    child = _delegate(world)
    _end(child)
    wake_id = _wake(child)

    # The wake turn is RUNNING: it is the only row in the delegator's ledger.
    assert [r["id"] for r in db.get_running_turn_attribution(A)] == [wake_id]

    key = _key(world)
    assert _DEP.acting_turn_user_id(key) is None
    assert _DEP.viewer_row_scope(key, B) is None   # the callee's receipt
    assert _DEP.viewer_row_scope(key, A) is None   # its own runs, the wake included


def test_a_wake_with_nobody_on_it_still_hides_every_row(world):
    """The same chain with the person left off, which is what every wake was.
    It pins the mechanism the fix removes for the owner's work, and what the
    wakes below still get."""
    child = _delegated_for(None)
    _end(child)
    wake_id = _wake(child)
    assert _rating(wake_id) == (turn_tier_service.SHARE, turn_tier_service.VISITOR, None)
    assert _DEP.acting_turn_user_id(_key(world)) == _DEP.NO_ROWS_USER_ID


# ---------------------------------------------------------------------------
# what the wake hands the dispatch
# ---------------------------------------------------------------------------


def test_the_wake_dispatch_is_handed_the_owner_and_no_email(world):
    child = _delegate(world)
    _end(child)
    _wake(child)
    (sent,) = world.sent
    assert sent["source_user_id"] == world.ids[OWNER]
    assert "source_user_email" not in sent      # ent#886: never the person in the prompt
    assert "source_agent_name" not in sent      # a wake is not a delegation


def test_an_admins_delegation_wakes_at_owner_standing(world):
    db.create_user(UserCreate(username="zq1216-admin", role="admin", email="zq1216-admin@example.com"))
    admin = db.get_user_by_username("zq1216-admin")["id"]
    child = _delegated_for(admin)
    _end(child)
    wake_id = _wake(child)
    assert world.sent[0]["source_user_id"] == admin
    assert _rating(wake_id)[0] == turn_tier_service.OWNER


@pytest.mark.parametrize("who", ["shared", "cannot_open", "deleted", "nobody"])
def test_every_other_wake_carries_nobody_as_before(world, who):
    """A person the delegator is only shared with, a person who cannot open it
    at all (a hub's member), a user id with no row, and a visitor: the wake is
    the no-person turn it has always been: this fix changes the owner's
    wakes and no others."""
    person = {
        "shared": world.ids[MATE],
        "cannot_open": world.ids[STRANGER],
        "deleted": 987654,
        "nobody": None,
    }[who]
    child = _delegated_for(person)
    _end(child)
    wake_id = _wake(child)
    (sent,) = world.sent
    assert "source_user_id" not in sent
    assert _rating(wake_id) == (turn_tier_service.SHARE, turn_tier_service.VISITOR, None)


def test_a_rating_that_cannot_be_read_carries_nobody(world, monkeypatch):
    """Never raises on the billed terminal path, and never guesses upward."""
    child = _delegate(world)
    _end(child)

    def _boom(*a, **k):
        raise RuntimeError("ledger down")

    monkeypatch.setattr(turn_tier_service, "turn_actor", _boom)
    _wake(child)
    assert "source_user_id" not in world.sent[0]


# ---------------------------------------------------------------------------
# what else the person on the row changes
# ---------------------------------------------------------------------------


def test_the_audit_names_the_standing_and_never_the_person(world):
    owner_child = _delegate(world)
    _end(owner_child)
    _wake(owner_child)
    other_child = _delegated_for(world.ids[MATE])
    _end(other_child)
    _wake(other_child)
    assert [a["standing"] for a in world.audits] == ["owner", "none"]
    for audit in world.audits:
        assert not {k for k in audit if "user" in k or "person" in k or "email" in k}


def test_auto_memory_follows_the_owner_rule_on_a_wake(world):
    """`memory_subject` rule 5: a turn that serves the owner keeps the agent's
    own notes. A wake with nobody on it never had them (`caller_wake` is not a
    headless trigger), and still does not."""
    owner_child = _delegate(world)
    _end(owner_child)
    owner_wake = db.get_execution(_wake(owner_child))
    other_child = _delegated_for(world.ids[MATE])
    _end(other_child)
    other_wake = db.get_execution(_wake(other_child))

    keeps = memory_subject.keeps_shared_auto_memory
    assert keeps(owner_wake, OWNER_EMAIL, owner_id=world.ids[OWNER]) is True
    assert keeps(other_wake, OWNER_EMAIL, owner_id=world.ids[OWNER]) is False
