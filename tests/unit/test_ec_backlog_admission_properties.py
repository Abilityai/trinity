"""Hypothesis properties for the persistent backlog (BACKLOG-001) and the
dispatch-admission depth guard — /edge-cases run 2026-10-06.

The sibling ``test_ec_backlog_admission_edges.py`` pins the discrete matrix
rows; these properties cover the invariants those rows sample:

  P1  enqueue cap (oracle): accepted ⇔ depth < cap, for every depth/cap pair
      incl. depth > cap; a refusal writes nothing.
  P2  round-trip: the metadata ``enqueue`` writes, JSON-serialised and read back
      through ``request_from_metadata``, reproduces the delivered request.
  P3  drain no-crash-total + slot balance: for ANY ``backlog_metadata`` text
      (or NULL), ``drain_next`` returns a bool, never raises, and leaves held
      slots == {execution_id} on success, {} otherwise; a False after a claim is
      always either a requeue or exactly one FAILED write.
  P4  ``_max_chain_depth`` bounds: always within the validated [1, 32].
  P5  ``enforce_inter_agent_depth`` oracle: depth = max(1 + running if caller,
      claim), returned iff ≤ max, else refused.
  P6  real SQL, ties allowed: claiming a random queue (duplicate ``queued_at``
      included) hands out every row exactly once, in non-decreasing
      ``queued_at`` order (order *among* ties is UNSPECIFIED and not asserted).
"""

from __future__ import annotations

import asyncio
import itertools
import json
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

_THIS = Path(__file__).resolve()
_BACKEND_STR = str(_THIS.parent.parent.parent / "src" / "backend")
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend, run as _hrun  # noqa: E402,F401

_DB_MODULES = ("db.connection", "db.schedules", "database")
_STUBBED_MODULE_NAMES = [*_DB_MODULES]


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    saved = {name: sys.modules.get(name) for name in _STUBBED_MODULE_NAMES}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


_counter = itertools.count()


def _uid(prefix):
    return f"{prefix}-{next(_counter)}"


# ---------------------------------------------------------------------------
# Fakes (service seam)
# ---------------------------------------------------------------------------


class _Slots:
    def __init__(self):
        self.held: set = set()

    async def acquire_slot(self, **kw):
        if self.held:
            return False
        self.held.add(kw["execution_id"])
        return True

    async def release_slot(self, agent_name, execution_id):
        self.held.discard(execution_id)


class _FakeDb:
    def __init__(self, depth=0, cap=50, claim=None):
        self.depth, self.cap, self.claim = depth, cap, claim
        self.queued: dict = {}
        self.requeued: list = []
        self.failed: list = []

    def get_queued_count(self, a):
        return self.depth

    def get_max_backlog_depth(self, a):
        return self.cap

    def get_execution_timeout(self, a):
        return 900

    def update_execution_to_queued(self, eid, meta, queued_at, conversation_key=None):
        self.queued[eid] = meta
        return True

    def claim_next_queued(self, a):
        return self.claim

    def release_claim_to_queued(self, eid):
        self.requeued.append(eid)
        return True

    def update_execution_status(self, execution_id, status, result=None, **_):
        self.failed.append((execution_id, status))
        return True


def _install(monkeypatch, db, slots):
    import services.backlog_service as bl

    monkeypatch.setitem(sys.modules, "database", types.SimpleNamespace(db=db))
    monkeypatch.setattr(bl, "get_slot_service", lambda: slots)
    monkeypatch.setitem(
        sys.modules, "services.settings_service",
        types.SimpleNamespace(get_effective_max_parallel_tasks=lambda _n: 1),
    )
    monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", "")

    async def _run_async_task(**kw):
        return None

    monkeypatch.setitem(
        sys.modules, "services.chat_execution_service",
        types.SimpleNamespace(run_async_task=_run_async_task),
    )
    return bl.BacklogService()


_FN_SCOPED = [HealthCheck.function_scoped_fixture]


def _enqueue(svc, request, eid="e"):
    return asyncio.run(svc.enqueue(
        agent_name="alpha", execution_id=eid, request=request, effective_timeout=300,
        user_id=None, user_email=None, subscription_id=None, x_source_agent=None,
        triggered_by="manual", collaboration_activity_id=None,
    ))


# ===========================================================================
# P1 — enqueue cap oracle
# ===========================================================================


@settings(max_examples=200, deadline=None, suppress_health_check=_FN_SCOPED)
@given(depth=st.integers(0, 400), cap=st.integers(1, 200))
@example(depth=49, cap=50)
@example(depth=50, cap=50)
@example(depth=51, cap=50)
@example(depth=0, cap=1)
def test_p1_enqueue_accepts_iff_below_cap(monkeypatch, depth, cap):
    from models import ParallelTaskRequest

    db = _FakeDb(depth=depth, cap=cap)
    svc = _install(monkeypatch, db, _Slots())
    ok = _enqueue(svc, ParallelTaskRequest(message="m", async_mode=True))
    assert ok is (depth < cap)
    assert bool(db.queued) is ok


# ===========================================================================
# P2 — metadata round-trip
# ===========================================================================

_opt_text = st.one_of(st.none(), st.text(max_size=40))
_opt_bool = st.one_of(st.none(), st.booleans())


@settings(max_examples=200, deadline=None, suppress_health_check=_FN_SCOPED)
@given(
    message=st.text(max_size=200),
    model=_opt_text,
    tools=st.one_of(st.none(), st.lists(st.text(max_size=10), max_size=4)),
    system_prompt=_opt_text,
    max_turns=st.one_of(st.none(), st.integers(1, 500)),
    save=_opt_bool, create_new=_opt_bool, inject=_opt_bool,
    user_message=_opt_text, chat_id=_opt_text, resume_id=_opt_text,
    timeout=st.integers(1, 86400),
)
def test_p2_enqueue_metadata_round_trips(
    monkeypatch, message, model, tools, system_prompt, max_turns, save,
    create_new, inject, user_message, chat_id, resume_id, timeout,
):
    from models import ParallelTaskRequest
    from services.backlog_service import request_from_metadata

    req = ParallelTaskRequest(
        message=message, model=model, allowed_tools=tools, system_prompt=system_prompt,
        max_turns=max_turns, save_to_session=save, create_new_session=create_new,
        inject_result=inject, user_message=user_message, chat_session_id=chat_id,
        resume_session_id=resume_id, async_mode=True,
    )
    db = _FakeDb()
    svc = _install(monkeypatch, db, _Slots())
    assert asyncio.run(svc.enqueue(
        agent_name="alpha", execution_id="e", request=req, effective_timeout=timeout,
        user_id=None, user_email=None, subscription_id=None, x_source_agent=None,
        triggered_by="manual", collaboration_activity_id=None,
    ))
    back = request_from_metadata(json.loads(db.queued["e"]))

    assert back.message == message
    assert (back.model, back.allowed_tools, back.system_prompt, back.max_turns) == (
        model, tools, system_prompt, max_turns)
    assert back.timeout_seconds == timeout  # the EFFECTIVE timeout, not the request's
    assert (back.user_message, back.chat_session_id, back.resume_session_id) == (
        user_message, chat_id, resume_id)
    # tri-state bools collapse None → False on the way back
    assert back.save_to_session is bool(save)
    assert back.create_new_session is bool(create_new)
    assert back.inject_result is bool(inject)
    assert back.async_mode is True


# ===========================================================================
# P3 — drain total + slot balance for arbitrary metadata
# ===========================================================================

_json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(max_size=20),
    lambda c: st.lists(c, max_size=3) | st.dictionaries(st.text(max_size=10), c, max_size=3),
    max_leaves=8,
)
_blobs = st.one_of(
    st.none(),
    st.text(max_size=80),
    _json_values.map(json.dumps),
    st.fixed_dictionaries({"message": st.text(max_size=30)}).map(json.dumps),
)


@settings(max_examples=200, deadline=None, suppress_health_check=_FN_SCOPED)
@given(blob=_blobs, message=st.one_of(st.none(), st.text(max_size=150)))
@example(blob=None, message=None)
@example(blob="null", message="m")
@example(blob="{", message="m")
@example(blob='{"message": 5}', message="m")
def test_p3_drain_is_total_and_slot_balanced(monkeypatch, blob, message):
    db = _FakeDb(depth=1, claim={"id": "x1", "message": message, "backlog_metadata": blob})
    slots = _Slots()
    svc = _install(monkeypatch, db, slots)

    async def _go():
        r = await svc.drain_next("alpha")
        for _ in range(3):
            await asyncio.sleep(0)
        return r

    ok = asyncio.run(_go())
    assert isinstance(ok, bool)
    if ok:
        assert slots.held == {"x1"}
        assert db.failed == [] and db.requeued == []
    else:
        assert slots.held == set()
        # a claimed row is never silently dropped: requeued or failed exactly once
        assert len(db.requeued) + len(db.failed) == 1
        assert all(s == "failed" for _, s in db.failed)


# ===========================================================================
# P4 / P5 — depth guard
# ===========================================================================

import services.dispatch_admission_service as _DISPATCH  # noqa: E402


@settings(max_examples=200, deadline=None)
@given(stored=st.integers(min_value=-(10**12), max_value=10**12))
@example(stored=0)
@example(stored=33)
def test_p4_max_chain_depth_is_bounded(stored):
    with patch.object(_DISPATCH.settings_service, "get_ops_setting", return_value=stored):
        v = _DISPATCH._max_chain_depth()
    assert 1 <= v <= 32
    if 1 <= stored <= 32:
        assert v == stored


@settings(max_examples=200, deadline=None)
@given(
    is_agent=st.booleans(),
    running=st.integers(0, 40),
    claimed=st.one_of(st.none(), st.integers(1, 40)),
    max_depth=st.integers(1, 32),
)
@example(is_agent=True, running=7, claimed=None, max_depth=8)
@example(is_agent=True, running=8, claimed=None, max_depth=8)
@example(is_agent=False, running=0, claimed=8, max_depth=8)
@example(is_agent=False, running=0, claimed=9, max_depth=8)
def test_p5_depth_guard_oracle(is_agent, running, claimed, max_depth):
    from services.chat_signals import InterAgentDepthExceeded

    user = MagicMock()
    user.agent_name = "caller" if is_agent else None
    user.mcp_scope = None
    user.vouched_source_agent = None
    user.loopback_chain_depth = claimed
    db = MagicMock()
    db.get_max_running_chain_depth.return_value = running

    if not is_agent and claimed is None:
        expected = None
    else:
        expected = max(1 + running if is_agent else 0, claimed or 0)

    with patch.object(_DISPATCH, "db", db), \
         patch.object(_DISPATCH, "_max_chain_depth", return_value=max_depth), \
         patch.object(_DISPATCH, "_record_depth_refusal", AsyncMock()):
        coro = _DISPATCH.enforce_inter_agent_depth(
            current_user=user, target="t", endpoint="/e", x_via_mcp=None)
        if expected is not None and expected > max_depth:
            with pytest.raises(InterAgentDepthExceeded) as ei:
                asyncio.run(coro)
            assert ei.value.depth == expected and ei.value.max_depth == max_depth
        else:
            assert asyncio.run(coro) == expected
    if not is_agent:
        db.get_max_running_chain_depth.assert_not_called()


# ===========================================================================
# P6 — real SQL: exactly-once, non-decreasing queued_at, ties allowed
# ===========================================================================


@pytest.fixture
def ops(db_backend):
    for m in _DB_MODULES:
        sys.modules.pop(m, None)
    from db.schedules import ScheduleOperations

    yield ScheduleOperations(user_ops=MagicMock(), agent_ops=MagicMock())
    for m in _DB_MODULES:
        sys.modules.pop(m, None)


@settings(max_examples=40, deadline=None, suppress_health_check=_FN_SCOPED)
@given(offsets=st.lists(st.integers(0, 3), min_size=1, max_size=6))
@example(offsets=[0, 0, 0])
def test_p6_claim_is_exactly_once_and_ordered(ops, offsets):
    agent = _uid("p6")
    base = datetime.now(timezone.utc) - timedelta(minutes=10)
    rows = {}
    for off in offsets:
        eid = _uid("p6r")
        q = (base + timedelta(seconds=off)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        _hrun(
            "INSERT INTO schedule_executions (id, schedule_id, agent_name, status, "
            "started_at, queued_at, message, triggered_by) VALUES "
            "(:i, '__manual__', :a, 'queued', :q, :q, 'm', 'manual')",
            i=eid, a=agent, q=q,
        )
        rows[eid] = q
    got = []
    while (r := ops.claim_next_queued(agent)) is not None:
        got.append(r["id"])
        assert len(got) <= len(rows), "claim handed out more rows than were queued"
    assert sorted(got) == sorted(rows)
    qs = [rows[i] for i in got]
    assert qs == sorted(qs)
    assert ops.get_queued_count(agent) == 0
