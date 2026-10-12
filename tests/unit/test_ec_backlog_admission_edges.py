"""Edge-case matrix rows for the persistent backlog (BACKLOG-001) and the
dispatch-admission gate (#1483) — /edge-cases run 2026-10-06.

Targets:
  * ``services/backlog_service.py`` — ``request_from_metadata``,
    ``BacklogService.enqueue`` / ``drain_next`` / ``on_slot_released`` /
    ``expire_stale`` / ``drain_orphans_all`` / ``cancel_all_backlog``.
  * ``services/dispatch_admission_service.py`` — ``_chain_caller``,
    ``enforce_inter_agent_depth``, ``admit_chat_request``,
    ``begin_task_idempotency``.

Scope — only the rows the existing suite left UNCOVERED (the full matrix with
the "covered by" column is the 2026-10-06 /edge-cases matrix):

  * depth cap at ``cap-1`` (accept, reaching exactly cap), ``cap`` (reject) and
    ``cap+1`` (a cap lowered below the live depth), min cap ``1``;
  * enqueue CAS losses — a terminal/missing row and a second enqueue of the same
    execution id (double-enqueue) must both be refused and leave no trace;
  * drain slot accounting on every exit — the re-acquire race, corrupt/odd
    metadata, a spawn failure — so no path leaks a slot or a held claim;
  * drain against REAL SQL (the ``database.db`` seam is a thin adapter over a
    harness-bound ``ScheduleOperations``): cancelled and expired rows are never
    dequeued, two drains of one row dequeue it once, a full drain cascade
    honours FIFO;
  * equal ``queued_at`` ties and the released-row position — both UNSPECIFIED,
    pinned as characterisation so a change is a visible decision;
  * admission boundaries: half-open / unknown breaker states, a ``None``
    retry-after, an in-memory-queued result, ``EphemeralBudgetExhausted``, and
    an unexpected acquire error (UNSPECIFIED: it leaves the idempotency claim
    in flight).

Unit-only: no Docker, Redis or network. Slots are an in-memory fake; the DB is
the db_harness temp SQLite (and PostgreSQL when ``TEST_POSTGRES_URL`` is set).
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_THIS = Path(__file__).resolve()
_BACKEND_STR = str(_THIS.parent.parent.parent / "src" / "backend")
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend, run as _hrun, scalar as _hscalar  # noqa: E402,F401

_DB_MODULES = ("db.connection", "db.schedules", "db.agent_settings.resources", "database")

# #762: the `ops` fixture evicts db.* so they re-import against the harness
# engine; snapshot/restore keeps that from leaking into later files.
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


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Slots:
    """In-memory SlotService: tracks the held set so tests can assert balance.

    ``script`` is an optional list of booleans consumed by successive
    ``acquire_slot`` calls (then falls back to the capacity rule).
    """

    def __init__(self, capacity: int = 1, script=None):
        self.capacity = capacity
        self.script = list(script or [])
        self.held: set = set()
        self.acquires: list = []
        self.releases: list = []

    async def acquire_slot(self, **kw):
        self.acquires.append(kw)
        if self.script:
            ok = self.script.pop(0)
        else:
            ok = len(self.held) < self.capacity
        if ok:
            self.held.add(kw["execution_id"])
        return ok

    async def release_slot(self, agent_name, execution_id):
        self.releases.append(execution_id)
        self.held.discard(execution_id)


class _FakeDb:
    def __init__(self, *, depth=0, cap=50, enqueue_ok=True, claim=None):
        self.depth = depth
        self.cap = cap
        self.enqueue_ok = enqueue_ok
        self.claim = claim
        self.queued: dict = {}
        self.claim_calls = 0
        self.released_claims: list = []
        self.status_writes: list = []

    def get_queued_count(self, agent_name):
        return self.depth

    def get_max_backlog_depth(self, agent_name):
        return self.cap

    def get_execution_timeout(self, agent_name):
        return 900

    def update_execution_to_queued(self, execution_id, metadata, queued_at, conversation_key=None):
        if not self.enqueue_ok:
            return False
        self.queued[execution_id] = (json.loads(metadata), conversation_key)
        return True

    def claim_next_queued(self, agent_name):
        self.claim_calls += 1
        return self.claim

    def release_claim_to_queued(self, execution_id):
        self.released_claims.append(execution_id)
        return True

    def update_execution_status(self, execution_id, status, result=None, **_):
        self.status_writes.append((execution_id, status, getattr(result, "error", None)))
        return True

    def list_agents_with_queued(self):
        return []

    def expire_stale_queued(self, max_age_hours):
        return 0

    def cancel_queued_for_agent(self, agent_name, reason="agent_deleted"):
        return 0


def _install(monkeypatch, fake_db, slots, spawn=None):
    """Point BacklogService's late imports at the fakes."""
    import services.backlog_service as bl

    monkeypatch.setitem(sys.modules, "database", types.SimpleNamespace(db=fake_db))
    monkeypatch.setattr(bl, "get_slot_service", lambda: slots)
    monkeypatch.setitem(
        sys.modules,
        "services.settings_service",
        types.SimpleNamespace(get_effective_max_parallel_tasks=lambda _n: 1),
    )
    monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", "")
    spawned: list = []

    async def _run_async_task(**kw):
        spawned.append(kw)
        if spawn is not None:
            await spawn(**kw)

    monkeypatch.setitem(
        sys.modules,
        "services.chat_execution_service",
        types.SimpleNamespace(run_async_task=_run_async_task),
    )
    return bl.BacklogService(), spawned


def _req(**kw):
    from models import ParallelTaskRequest

    kw.setdefault("message", "hello")
    kw.setdefault("async_mode", True)
    return ParallelTaskRequest(**kw)


def _enqueue(svc, execution_id="e-1", agent="alpha", request=None, **kw):
    return asyncio.run(
        svc.enqueue(
            agent_name=agent,
            execution_id=execution_id,
            request=request or _req(),
            effective_timeout=300,
            user_id=1,
            user_email=None,
            subscription_id=None,
            x_source_agent=None,
            triggered_by="manual",
            collaboration_activity_id=None,
            **kw,
        )
    )


async def _drain_and_settle(svc, agent="alpha"):
    ok = await svc.drain_next(agent)
    # let a spawned task run (and its done-callback fire)
    for _ in range(3):
        await asyncio.sleep(0)
    return ok


# ===========================================================================
# B — enqueue: depth cap boundaries + CAS losses
# ===========================================================================


@pytest.mark.parametrize(
    "depth, cap, accepted",
    [
        (49, 50, True),   # B1 cap-1 → accepted, queue reaches exactly cap
        (50, 50, False),  # B2 exactly at cap (also in test_backlog)
        (51, 50, False),  # B3 cap+1 — cap lowered below the live depth
        (0, 1, True),     # B4 min cap, empty
        (1, 1, False),    # B4 min cap, full
        (199, 200, True),  # B5 max cap, one slot left
        (200, 200, False),  # B5 max cap, full
    ],
    ids=["B1-cap-minus-1", "B2-at-cap", "B3-cap-plus-1", "B4-min-empty",
         "B4-min-full", "B5-max-last", "B5-max-full"],
)
def test_b_enqueue_cap_boundaries(monkeypatch, depth, cap, accepted):
    db = _FakeDb(depth=depth, cap=cap)
    svc, _ = _install(monkeypatch, db, _Slots())
    assert _enqueue(svc) is accepted
    # a refused request writes nothing
    assert ("e-1" in db.queued) is accepted


def test_b6_enqueue_cas_loss_returns_false(monkeypatch):
    """B6 — the row is gone or already terminal (#1082 CAS) → clean refusal."""
    db = _FakeDb(enqueue_ok=False)
    svc, _ = _install(monkeypatch, db, _Slots())
    assert _enqueue(svc) is False


@pytest.mark.parametrize(
    "explicit, chat, resume, expected",
    [
        ("session:k", "chat-1", "res-1", "session:k"),  # B10 explicit wins
        (None, None, "res-1", "res-1"),                  # B10 resume fallback
        (None, None, None, None),                        # B10 nothing → None
        ("", "", "", None),                              # B10 empties → None
    ],
    ids=["B10-explicit", "B10-resume", "B10-none", "B10-empties"],
)
def test_b10_pilot_conversation_key_precedence(monkeypatch, explicit, chat, resume, expected):
    db = _FakeDb()
    svc, _ = _install(monkeypatch, db, _Slots())
    monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", "alpha")
    assert _enqueue(
        svc, request=_req(chat_session_id=chat, resume_session_id=resume),
        conversation_key=explicit,
    )
    assert db.queued["e-1"][1] == expected


def test_b11_non_pilot_drops_an_explicit_conversation_key(monkeypatch):
    """B11 — only a pilot's worker enforces one-turn-per-conversation, so a
    non-pilot never carries the key even when the caller names one."""
    db = _FakeDb()
    svc, _ = _install(monkeypatch, db, _Slots())
    assert _enqueue(svc, conversation_key="session:k")
    assert db.queued["e-1"][1] is None


def test_b9_empty_images_list_is_stored_as_none(monkeypatch):
    db = _FakeDb()
    svc, _ = _install(monkeypatch, db, _Slots())
    assert _enqueue(svc, images=[])
    assert db.queued["e-1"][0]["images"] is None


# ===========================================================================
# R — request_from_metadata: missing / null / unknown keys
# ===========================================================================


def test_r1_empty_metadata_yields_safe_defaults():
    from services.backlog_service import request_from_metadata

    r = request_from_metadata({})
    assert r.message == ""
    assert r.async_mode is True
    assert (r.save_to_session, r.create_new_session, r.inject_result) == (False, False, False)
    assert r.model is None and r.timeout_seconds is None


def test_r2_explicit_nulls_for_bool_keys_coerce_to_false():
    from services.backlog_service import request_from_metadata

    r = request_from_metadata(
        {"message": None, "save_to_session": None, "create_new_session": None,
         "inject_result": None}
    )
    assert r.message == ""
    assert (r.save_to_session, r.create_new_session, r.inject_result) == (False, False, False)


def test_r3_unknown_keys_are_ignored_forward_compat():
    from services.backlog_service import request_from_metadata

    r = request_from_metadata({"message": "m", "future_field": {"x": 1}, "images": ["b64"]})
    assert r.message == "m"
    assert not hasattr(r, "future_field")


# ===========================================================================
# D — drain_next: slot accounting on every exit
# ===========================================================================


def test_d3_failed_slot_acquire_never_claims(monkeypatch):
    db = _FakeDb(depth=1, claim={"id": "x", "backlog_metadata": "{}"})
    slots = _Slots(script=[False])
    svc, _ = _install(monkeypatch, db, slots)
    assert asyncio.run(svc.drain_next("alpha")) is False
    assert db.claim_calls == 0
    assert slots.held == set()


def test_d5_reacquire_race_returns_row_to_queue_and_holds_no_slot(monkeypatch):
    """D5 — sentinel acquired, row claimed, sentinel released, then another
    request takes the slot before the real-id re-acquire. The row must go back
    to the queue and NO slot may stay held (neither sentinel nor real)."""
    db = _FakeDb(depth=1, claim={"id": "exec-9", "message": "m", "backlog_metadata": "{}"})
    slots = _Slots(script=[True, False])
    svc, spawned = _install(monkeypatch, db, slots)
    assert asyncio.run(svc.drain_next("alpha")) is False
    assert db.released_claims == ["exec-9"]
    assert slots.held == set()
    assert spawned == []
    assert db.status_writes == []  # not failed — merely requeued


def test_d6_corrupt_metadata_fails_row_and_releases_the_real_slot(monkeypatch):
    db = _FakeDb(depth=1, claim={"id": "exec-c", "message": "m", "backlog_metadata": "{nope"})
    slots = _Slots()
    svc, spawned = _install(monkeypatch, db, slots)
    assert asyncio.run(svc.drain_next("alpha")) is False
    assert slots.held == set()
    assert "exec-c" in slots.releases
    assert [w[1] for w in db.status_writes] == ["failed"]
    assert spawned == []


@pytest.mark.parametrize("blob", ["null", "[]", '"str"', "42"],
                         ids=["D8-null", "D8-list", "D8-string", "D8-number"])
def test_d8_non_object_json_metadata_fails_closed_without_leaking(monkeypatch, blob):
    """D8 — valid JSON that is not an object passes json.loads but cannot be read
    by request_from_metadata. Defensive (no writer produces it), but the drain
    must still fail the row and release the slot rather than raise."""
    db = _FakeDb(depth=1, claim={"id": "exec-j", "message": "m", "backlog_metadata": blob})
    slots = _Slots()
    svc, spawned = _install(monkeypatch, db, slots)
    assert asyncio.run(svc.drain_next("alpha")) is False
    assert slots.held == set()
    assert [w[1] for w in db.status_writes] == ["failed"]
    assert "spawn failed" in (db.status_writes[0][2] or "")


def test_d7_null_backlog_metadata_drains_as_empty_object(monkeypatch):
    """D7 — a NULL blob is read as ``{}`` (an empty-message request) — the drain
    proceeds and holds the real slot for the spawned task."""
    db = _FakeDb(depth=1, claim={"id": "exec-n", "message": None, "backlog_metadata": None})
    slots = _Slots()
    svc, spawned = _install(monkeypatch, db, slots)
    assert asyncio.run(_drain_and_settle(svc)) is True
    assert slots.held == {"exec-n"}
    assert spawned[0]["request"].message == ""
    # D9: a None message gives an empty preview, never a TypeError
    assert slots.acquires[-1]["message_preview"] == ""


def test_d9_real_acquire_uses_execution_id_and_100_char_preview(monkeypatch):
    long = "x" * 250
    db = _FakeDb(depth=1, claim={"id": "exec-p", "message": long, "backlog_metadata": "{}"})
    slots = _Slots()
    svc, _ = _install(monkeypatch, db, slots)
    assert asyncio.run(_drain_and_settle(svc)) is True
    sentinel, real = slots.acquires
    assert sentinel["execution_id"].startswith("drain-alpha-")
    assert sentinel["execution_id"] in slots.releases
    assert real["execution_id"] == "exec-p"
    assert real["message_preview"] == long[:100]


def test_d_spawn_failure_fails_row_and_releases_slot(monkeypatch):
    """The ``# pragma: no cover`` spawn-failure branch: if spawning raises, the
    row is FAILED and the real slot released (no leak, no stranded running row)."""
    db = _FakeDb(depth=1, claim={"id": "exec-s", "message": "m", "backlog_metadata": "{}"})
    slots = _Slots()
    svc, _ = _install(monkeypatch, db, slots)

    async def _boom(*a, **k):
        raise RuntimeError("import drift")

    monkeypatch.setattr(svc, "_spawn_drain", _boom)
    assert asyncio.run(svc.drain_next("alpha")) is False
    assert slots.held == set()
    assert db.status_writes[0][1] == "failed"
    assert "import drift" in db.status_writes[0][2]


def test_d15_on_slot_released_swallows_drain_errors(monkeypatch):
    db = _FakeDb()
    svc, _ = _install(monkeypatch, db, _Slots())

    async def _boom(_agent):
        raise RuntimeError("db down")

    monkeypatch.setattr(svc, "drain_next", _boom)
    asyncio.run(svc.on_slot_released("alpha"))  # must not raise


def test_d14_orphan_sweep_isolates_a_failing_agent(monkeypatch):
    """D14 — one agent's drain raising must not stop the sweep, and only true
    drains are counted."""
    db = _FakeDb()
    db.list_agents_with_queued = lambda: ["a", "boom", "b", "idle"]
    svc, _ = _install(monkeypatch, db, _Slots())
    seen = []

    async def _drain(agent):
        seen.append(agent)
        if agent == "boom":
            raise RuntimeError("x")
        return agent != "idle"

    monkeypatch.setattr(svc, "drain_next", _drain)
    assert asyncio.run(svc.drain_orphans_all()) == 2
    assert seen == ["a", "boom", "b", "idle"]


def test_e1_expire_stale_passes_window_through(monkeypatch):
    db = _FakeDb()
    seen = []
    db.expire_stale_queued = lambda h: seen.append(h) or 3
    svc, _ = _install(monkeypatch, db, _Slots())
    assert asyncio.run(svc.expire_stale(max_age_hours=0.5)) == 3
    assert seen == [0.5]


# ===========================================================================
# Real SQL: dequeue semantics through the service (database.db seam = adapter)
# ===========================================================================


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class _DbAdapter:
    """Real ``ScheduleOperations`` for the queue SQL + fixed per-agent knobs."""

    def __init__(self, ops, cap=50):
        self._ops = ops
        self.cap = cap

    def __getattr__(self, name):
        return getattr(self._ops, name)

    def get_max_backlog_depth(self, agent_name):
        return self.cap

    def get_execution_timeout(self, agent_name):
        return 900


@pytest.fixture
def ops(db_backend):
    for m in _DB_MODULES:
        sys.modules.pop(m, None)
    from db.schedules import ScheduleOperations

    yield ScheduleOperations(user_ops=MagicMock(), agent_ops=MagicMock())
    for m in _DB_MODULES:
        sys.modules.pop(m, None)


def _row(eid, *, agent="alpha", status="running", queued_at=None, meta=None):
    _hrun(
        "INSERT INTO schedule_executions (id, schedule_id, agent_name, status, "
        "started_at, queued_at, message, triggered_by, backlog_metadata) "
        "VALUES (:id, '__manual__', :a, :s, :st, :q, 'm', 'manual', :bm)",
        id=eid, a=agent, s=status, st=_iso(datetime.now(timezone.utc)), q=queued_at,
        bm=meta,
    )
    return eid


def _status(eid):
    return _hscalar("SELECT status FROM schedule_executions WHERE id = :i", i=eid)


def test_b8_real_cap_fill_then_cap_plus_one_is_refused(ops, monkeypatch):
    """B8 — against real SQL: cap rows fill the backlog, the cap+1-th request is
    refused and its row stays RUNNING (the router then fails it), untouched."""
    db = _DbAdapter(ops, cap=3)
    svc, _ = _install(monkeypatch, db, _Slots())
    ids = [_row(f"b8-{i}") for i in range(4)]
    assert [_enqueue(svc, execution_id=i) for i in ids] == [True, True, True, False]
    assert ops.get_queued_count("alpha") == 3
    assert _status("b8-3") == "running"


@pytest.mark.parametrize("prior", ["success", "failed", "cancelled", "queued"],
                         ids=["B7-success", "B7-failed", "B7-cancelled", "B7-double-enqueue"])
def test_b7_enqueue_never_resurrects_or_double_queues(ops, monkeypatch, prior):
    """B6/B7 — re-enqueueing a terminal row (phantom reversal, E-02) or the same
    already-queued id (double enqueue) is refused; depth does not move."""
    db = _DbAdapter(ops)
    svc, _ = _install(monkeypatch, db, _Slots())
    _row("b7", status=prior, queued_at=_iso(datetime.now(timezone.utc)) if prior == "queued" else None)
    before = ops.get_queued_count("alpha")
    assert _enqueue(svc, execution_id="b7") is False
    assert _status("b7") == prior
    assert ops.get_queued_count("alpha") == before


def test_b_enqueue_missing_row_is_refused(ops, monkeypatch):
    db = _DbAdapter(ops)
    svc, _ = _install(monkeypatch, db, _Slots())
    assert _enqueue(svc, execution_id="ghost") is False
    assert ops.get_queued_count("alpha") == 0


def test_d11_cancelled_and_expired_rows_are_never_dequeued(ops, monkeypatch):
    """D11 — older rows that were cancelled / TTL-expired are skipped; the drain
    takes the oldest row that is still QUEUED."""
    now = datetime.now(timezone.utc)
    _row("d11-cancel", status="queued", queued_at=_iso(now - timedelta(hours=30)), meta="{}")
    _row("d11-stale", status="queued", queued_at=_iso(now - timedelta(hours=25)), meta="{}")
    _row("d11-live", status="queued", queued_at=_iso(now - timedelta(minutes=1)), meta="{}")
    assert ops.cancel_queued_execution("d11-cancel", "user") is True

    db = _DbAdapter(ops)
    slots = _Slots(capacity=5)
    svc, spawned = _install(monkeypatch, db, slots)
    assert asyncio.run(svc.expire_stale(max_age_hours=24)) == 1

    assert asyncio.run(_drain_and_settle(svc)) is True
    assert spawned[0]["execution_id"] == "d11-live"
    assert asyncio.run(_drain_and_settle(svc)) is False  # nothing left to dequeue
    assert (_status("d11-cancel"), _status("d11-stale")) == ("cancelled", "failed")


def test_c1_cancel_all_backlog_empties_the_queue_for_the_drain(ops, monkeypatch):
    now = datetime.now(timezone.utc)
    for i in range(3):
        _row(f"c1-{i}", status="queued", queued_at=_iso(now - timedelta(seconds=10 - i)), meta="{}")
    _row("c1-other", agent="beta", status="queued", queued_at=_iso(now), meta="{}")
    db = _DbAdapter(ops)
    svc, spawned = _install(monkeypatch, db, _Slots(capacity=5))
    assert asyncio.run(svc.cancel_all_backlog("alpha")) == 3
    assert asyncio.run(svc.cancel_all_backlog("alpha")) == 0  # idempotent
    assert asyncio.run(_drain_and_settle(svc)) is False
    assert spawned == []
    assert _status("c1-other") == "queued"


def test_d10_two_drains_dequeue_one_row_once(ops, monkeypatch):
    """D10 — double dequeue: two drains racing for a single queued row (both
    pass the COUNT pre-check). Exactly one wins; the loser's sentinel slot is
    released, so exactly one slot is held — by the real execution id."""
    _row("d10", status="queued", queued_at=_iso(datetime.now(timezone.utc)), meta="{}")
    db = _DbAdapter(ops)
    slots = _Slots(capacity=5)
    svc, spawned = _install(monkeypatch, db, slots)

    async def _both():
        r = await asyncio.gather(svc.drain_next("alpha"), svc.drain_next("alpha"))
        for _ in range(3):
            await asyncio.sleep(0)
        return r

    results = asyncio.run(_both())
    assert sorted(results) == [False, True]
    assert slots.held == {"d10"}
    assert [s["execution_id"] for s in spawned] == ["d10"]


def test_d_full_cascade_is_fifo_with_capacity_one(ops, monkeypatch):
    """Drain on capacity free: with one slot, each release drains the next
    oldest row (FIFO), and a drain while the slot is held is a no-op."""
    base = datetime.now(timezone.utc) - timedelta(minutes=5)
    ids = [_row(f"fifo-{i}", status="queued", queued_at=_iso(base + timedelta(seconds=i)), meta="{}")
           for i in (2, 0, 1)]
    db = _DbAdapter(ops)
    slots = _Slots(capacity=1)
    svc, spawned = _install(monkeypatch, db, slots)

    order = []
    for _ in range(3):
        assert asyncio.run(_drain_and_settle(svc)) is True
        assert asyncio.run(_drain_and_settle(svc)) is False  # slot held → no-op
        (held,) = slots.held
        order.append(held)
        asyncio.run(slots.release_slot("alpha", held))
    assert order == sorted(ids)
    assert ops.get_queued_count("alpha") == 0


def test_d12_equal_queued_at_each_row_dequeued_exactly_once(ops):
    """D12 — UNSPECIFIED ordering among equal ``queued_at`` (no tiebreaker in
    the ORDER BY). What IS specified: every tied row is dequeued exactly once
    and the queue then reads empty."""
    ts = _iso(datetime.now(timezone.utc))
    ids = {_row(f"tie-{i}", status="queued", queued_at=ts) for i in range(5)}
    got = [ops.claim_next_queued("alpha")["id"] for _ in range(5)]
    assert set(got) == ids and len(got) == 5
    assert ops.claim_next_queued("alpha") is None


def test_d13_released_claim_loses_its_head_position_characterisation(ops):
    """D13 — UNSPECIFIED, pinned. ``release_claim_to_queued`` restores
    ``queued_at`` from ``started_at``, but ``claim_next_queued`` overwrote
    ``started_at`` with the claim time. So a row released by the drain's
    re-acquire race re-enters BEHIND rows queued before that claim (and its 24h
    TTL clock restarts). Whether that is intended (cf. ``requeue_expired_lease``
    which sends a redelivery to the back on purpose) is a human decision; this
    test makes any change to it visible."""
    now = datetime.now(timezone.utc)
    _row("d13-a", status="queued", queued_at=_iso(now - timedelta(seconds=20)))
    _row("d13-b", status="queued", queued_at=_iso(now - timedelta(seconds=10)))
    assert ops.claim_next_queued("alpha")["id"] == "d13-a"
    assert ops.release_claim_to_queued("d13-a") is True
    q = _hscalar("SELECT queued_at FROM schedule_executions WHERE id='d13-a'")
    assert q > _iso(now - timedelta(seconds=20))  # original position not restored
    assert ops.claim_next_queued("alpha")["id"] == "d13-b"


def test_d13b_release_of_a_non_running_row_is_a_noop(ops):
    _row("d13b", status="success")
    assert ops.release_claim_to_queued("d13b") is False
    assert ops.release_claim_to_queued("ghost") is False
    assert _status("d13b") == "success"


# ===========================================================================
# A — dispatch admission
# ===========================================================================

import services.dispatch_admission_service as _DISPATCH  # noqa: E402
from services.capacity_manager import (  # noqa: E402
    AcquireResult,
    CapacityFull,
    CircuitOpen,
    EphemeralBudgetExhausted,
)
from services.chat_signals import ChatAdmission, ChatAdmissionReplay  # noqa: E402


def _user(**kw):
    u = MagicMock()
    u.id = 1
    u.email = "u@example.com"
    u.username = "u"
    u.agent_name = kw.get("agent_name")
    u.mcp_scope = kw.get("mcp_scope")
    u.vouched_source_agent = kw.get("vouched")
    u.loopback_chain_depth = kw.get("claimed")
    return u


def _idem(replay=False, in_flight=False):
    m = MagicMock()
    m.replay, m.in_flight = replay, in_flight
    m.execution_id, m.snapshot = "prior", {"x": 1}
    return m


def _admit(*, idem=None, breaker_active=False, breaker=None, acquire=None, pull=False,
           running_depth=0, user=None):
    isvc = MagicMock()
    isvc.begin.return_value = idem or _idem()
    cap = MagicMock()
    cap.acquire = acquire or AsyncMock(
        return_value=AcquireResult(state="admitted", execution_id="x")
    )
    db = MagicMock()
    db.get_execution_timeout.return_value = 900
    db.get_max_parallel_tasks.return_value = 2
    db.get_max_running_chain_depth.return_value = running_depth
    gate = MagicMock()
    gate.enforce = AsyncMock(return_value=None)
    breaker_obj = MagicMock()
    breaker_obj.to_dict.return_value = breaker or {"state": "closed"}
    dbk = MagicMock(return_value=breaker_obj)
    with patch.object(_DISPATCH, "idempotency_service", isvc), \
         patch.object(_DISPATCH, "skill_gate_service", gate), \
         patch.object(_DISPATCH, "dispatch_breaker_active", return_value=breaker_active), \
         patch.object(_DISPATCH, "get_capacity_manager", return_value=cap), \
         patch.object(_DISPATCH, "platform_audit_service", MagicMock(log=AsyncMock())), \
         patch.object(_DISPATCH.pull_pilot, "pull_owns_dispatch", return_value=pull), \
         patch.object(_DISPATCH, "db", db), \
         patch("services.dispatch_breaker.DispatchBreaker", dbk):
        from models import ChatMessageRequest

        try:
            out = asyncio.run(_DISPATCH.admit_chat_request(
                name="agent1", request=ChatMessageRequest(message="hi"),
                current_user=user or _user(), x_source_agent=None, x_via_mcp=None,
                idempotency_key="k",
            ))
            err = None
        except Exception as e:  # noqa: BLE001
            out, err = None, e
    return out, err, isvc, cap, dbk


@pytest.mark.parametrize("state", ["half_open", "closed", "weird-future-state", None],
                         ids=["A7-half-open", "A7-closed", "A7-unknown", "A7-missing"])
def test_a7_only_an_open_breaker_refuses(state, monkeypatch):
    """A7 — /chat reads the breaker as pure state: anything but ``open`` (incl.
    an unknown future state) admits, fail-open, and consumes no probe."""
    out, err, isvc, cap, _ = _admit(breaker_active=True, breaker={"state": state})
    assert err is None and isinstance(out, ChatAdmission)
    cap.acquire.assert_awaited_once()
    isvc.fail.assert_not_called()


def test_a7b_open_breaker_with_null_retry_after_is_zero():
    out, err, isvc, cap, _ = _admit(
        breaker_active=True, breaker={"state": "open", "retry_after_seconds": None}
    )
    assert isinstance(err, CircuitOpen)
    isvc.fail.assert_called_once()
    cap.acquire.assert_not_called()


def test_a7c_inactive_breaker_is_never_read():
    _, err, _, _, dbk = _admit(breaker_active=False)
    assert err is None
    dbk.assert_not_called()


def test_a8_in_memory_queued_result_reports_its_position():
    res = AcquireResult(state="queued_in_memory", execution_id="x", queue_position=3)
    out, err, *_ = _admit(acquire=AsyncMock(return_value=res))
    assert err is None and out.queue_result == "queued:3"


def test_a9_ephemeral_budget_exhausted_releases_the_claim():
    out, err, isvc, *_ = _admit(acquire=AsyncMock(side_effect=EphemeralBudgetExhausted("agent1", "budget_spent")))
    assert isinstance(err, EphemeralBudgetExhausted)
    isvc.fail.assert_called_once()


def test_a9b_capacity_full_releases_the_claim():
    out, err, isvc, *_ = _admit(acquire=AsyncMock(side_effect=CapacityFull("agent1", 2, "in_memory_full")))
    assert isinstance(err, CapacityFull)
    isvc.fail.assert_called_once()


def test_a10_unexpected_acquire_error_leaves_claim_in_flight_characterisation():
    """A10 — UNSPECIFIED, pinned. Only the three domain refusals release the
    idempotency claim; any other acquire error (e.g. a Redis fault) propagates
    with the claim still in flight, so a retry with the same key gets 409 until
    the idempotency purge. Recorded as a spec gap, not a bug."""
    out, err, isvc, *_ = _admit(acquire=AsyncMock(side_effect=RuntimeError("redis down")))
    assert isinstance(err, RuntimeError)
    isvc.fail.assert_not_called()


def test_a11_replay_short_circuits_before_breaker_and_capacity():
    out, err, isvc, cap, dbk = _admit(idem=_idem(replay=True, in_flight=True), breaker_active=True)
    assert isinstance(out, ChatAdmissionReplay) and out.in_flight is True
    cap.acquire.assert_not_called()
    dbk.assert_not_called()


def test_a12_pull_pilot_open_breaker_still_refuses_first():
    """The breaker read precedes the pull branch: an open breaker refuses even a
    pilot's /chat, and nothing is queued."""
    out, err, isvc, cap, _ = _admit(pull=True, breaker_active=True, breaker={"state": "open"})
    assert isinstance(err, CircuitOpen)
    cap.acquire.assert_not_called()


def test_a12b_pull_pilot_takes_no_slot():
    out, err, isvc, cap, _ = _admit(pull=True)
    assert err is None and out.capacity is None
    assert out.capacity_result.state == "queued_persistent"
    cap.acquire.assert_not_called()


# --- _chain_caller / enforce_inter_agent_depth ------------------------------


@pytest.mark.parametrize(
    "kw, expected",
    [
        ({"agent_name": "a", "vouched": "v"}, "a"),       # A2 agent key wins
        ({"agent_name": "", "mcp_scope": "system"}, "trinity-system"),
        ({"agent_name": "", "vouched": ""}, None),         # empties → root
        ({"vouched": 7}, None),                            # non-str vouched → root
        ({"vouched": "emitter"}, "emitter"),
        ({"mcp_scope": "user"}, None),
    ],
    ids=["A2-agent-wins", "A2-system", "A2-empties", "A2-nonstr-vouched",
         "A2-vouched", "A2-user-scope"],
)
def test_a2_chain_caller(kw, expected):
    assert _DISPATCH._chain_caller(_user(**kw)) == expected


def _enforce(user, running=0, max_depth=8):
    db = MagicMock()
    db.get_max_running_chain_depth.return_value = running
    with patch.object(_DISPATCH, "db", db), \
         patch.object(_DISPATCH, "_max_chain_depth", return_value=max_depth), \
         patch.object(_DISPATCH, "_record_depth_refusal", AsyncMock()):
        return asyncio.run(_DISPATCH.enforce_inter_agent_depth(
            current_user=user, target="t", endpoint="/e", x_via_mcp=None))


@pytest.mark.parametrize(
    "kw, running, expected",
    [
        ({"claimed": True}, 0, None),                       # A3 bool claim ignored
        ({"claimed": "3"}, 0, None),                        # A3 str claim ignored
        ({"claimed": 8}, 0, 8),                             # A5 claim exactly max
        ({"agent_name": "a", "claimed": 2}, 5, 6),          # A4 running wins
        ({"agent_name": "a", "claimed": 7}, 1, 7),          # A4 claim wins
        ({"agent_name": "a"}, 7, 8),                        # A5 1+running == max
    ],
    ids=["A3-bool", "A3-str", "A5-claim-at-max", "A4-running-wins",
         "A4-claim-wins", "A5-running-at-max"],
)
def test_a4_depth_is_max_of_running_and_claim(kw, running, expected):
    assert _enforce(_user(**kw), running=running) == expected


@pytest.mark.parametrize("kw, running", [({"claimed": 9}, 0), ({"agent_name": "a"}, 8)],
                         ids=["A5-claim-max-plus-1", "A5-running-max-plus-1"])
def test_a5_max_plus_one_is_refused(kw, running):
    from services.chat_signals import InterAgentDepthExceeded

    with pytest.raises(InterAgentDepthExceeded) as ei:
        _enforce(_user(**kw), running=running)
    assert (ei.value.depth, ei.value.max_depth) == (9, 8)


@pytest.mark.parametrize("stored, effective", [(-5, 1), (10**9, 32), (1, 1), (32, 32)],
                         ids=["A1-negative", "A1-huge", "A1-low", "A1-high"])
def test_a1_max_chain_depth_clamps_int_rows(stored, effective):
    with patch.object(_DISPATCH.settings_service, "get_ops_setting", return_value=stored):
        assert _DISPATCH._max_chain_depth() == effective


@pytest.mark.parametrize("key", [None, ""], ids=["A13-none", "A13-empty"])
def test_a13_begin_task_idempotency_no_key_is_no_replay(key):
    idem, replay = _DISPATCH.begin_task_idempotency(name="agent1", idempotency_key=key)
    assert replay is None and idem.enabled is False


def test_a13b_begin_task_idempotency_replay_carries_snapshot():
    isvc = MagicMock()
    isvc.begin.return_value = _idem(replay=True, in_flight=False)
    with patch.object(_DISPATCH, "idempotency_service", isvc):
        idem, replay = _DISPATCH.begin_task_idempotency(name="agent1", idempotency_key="k")
    assert replay.execution_id == "prior" and replay.snapshot == {"x": 1}
    assert replay.in_flight is False


# ===========================================================================
# Residual branches (coverage sweep)
# ===========================================================================


def test_d16_spawned_task_error_is_logged_not_raised(monkeypatch, caplog):
    """A drained task that later raises is reported by the done-callback; the
    drain itself already returned True (the task owns its slot release)."""
    db = _FakeDb(depth=1, claim={"id": "exec-r", "message": "m", "backlog_metadata": "{}"})

    async def _raise(**kw):
        raise RuntimeError("agent exploded")

    svc, _ = _install(monkeypatch, db, _Slots(), spawn=_raise)
    with caplog.at_level("ERROR"):
        assert asyncio.run(_drain_and_settle(svc)) is True
    assert any("Drain task raised" in r.message for r in caplog.records)


def test_e1b_expire_stale_zero_is_silent(monkeypatch, caplog):
    db = _FakeDb()
    svc, _ = _install(monkeypatch, db, _Slots())
    with caplog.at_level("WARNING"):
        assert asyncio.run(svc.expire_stale()) == 0
    assert not any("Expired" in r.message for r in caplog.records)


def test_a14_public_replay_audit_wrapper_attributes_from_principal():
    log = AsyncMock()
    with patch.object(_DISPATCH, "platform_audit_service", MagicMock(log=log)):
        asyncio.run(_DISPATCH.audit_idempotent_replay(
            name="agent1", endpoint="/api/agents/agent1/task", x_via_mcp="1",
            x_source_agent=None, current_user=_user(), idempotency_key="k",
            idem=_idem(replay=True, in_flight=True),
        ))
    kw = log.await_args.kwargs
    assert kw["event_action"] == "idempotent_replay" and kw["source"] == "mcp"
    assert kw["details"] == {"idempotency_key": "k", "execution_id": "prior", "in_flight": True}
