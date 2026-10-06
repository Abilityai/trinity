"""
#3246 — the platform ending seam works from EVERY thread origin.

`platform_alerts.clear / observe` end up in `operator_resume_service.spawn_on_loop`
for the `platform_cleared` audit row and the `operator_queue_cancelled` trigger.
The emitters this PR adds call the seam through `asyncio.to_thread` (the headroom
sweep in `subscription_recovery_service`, the skills reconcile in
`skills_sync_service` / `routers/skills.py`) — the loop's DEFAULT executor, which
is a thread anyio does not own. `spawn_on_loop` handled only a running loop or an
anyio worker thread, so every such ending committed with no audit row and no
trigger, swallowed into a WARNING by `platform_alerts._spawn` / `ask_service._ended`.

The merge-train reproduction: loop thread OK, `anyio.to_thread` OK,
`asyncio.to_thread` → `RuntimeError`. These tests pin all three shapes at the
seam, and the loud failure that remains when no host loop exists at all.
"""
import asyncio
import logging
import os
import pathlib
import sys
import uuid
from types import SimpleNamespace

import pytest

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

pytestmark = pytest.mark.unit


@pytest.fixture
def ors():
    import services.operator_resume_service as mod
    return mod


@pytest.fixture
def sink(monkeypatch):
    """The real sink with its audit writer and broadcast replaced by recorders,
    patched ON the module (the unit conftest's sys.modules round-trip would
    otherwise hand the sink a fresh, unpatched audit service)."""
    import services.ask_service as svc

    audit, sent = [], []

    class _Audit:
        async def log(self, **kw):
            audit.append(kw)
            return "evt"

    class _WS:
        async def broadcast(self, message):
            sent.append(message)

    monkeypatch.setattr(svc, "platform_audit_service", _Audit())
    monkeypatch.setattr(svc, "_websocket_manager", _WS())
    monkeypatch.setattr(svc, "_observers", [])
    return SimpleNamespace(svc=svc, audit=audit, sent=sent)


@pytest.fixture
def pa():
    from services import platform_alerts
    return platform_alerts


@pytest.fixture
def db():
    from database import db
    return db


async def _drain(ors):
    for _ in range(10):
        await asyncio.sleep(0)
        pending = [t for t in ors._inflight if not t.done()]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)


# ---------------------------------------------------------------------------
# The seam, driven the way the emitters drive it
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_clear_from_asyncio_to_thread_lands_the_audit_row_and_trigger(
        ors, sink, pa, db, caplog):
    """`asyncio.to_thread(alerts.clear, …)` is how the headroom sweep ends a
    recovered subscription's row. The ending must be audited and announced —
    not committed silently behind a 'could not schedule' warning."""
    agent = f"agent-3246t-{uuid.uuid4().hex[:6]}"
    # Observe from the loop thread so the row exists (and the spawn works here).
    assert pa.observe(agent, "circuit_dormant", agent, title="t", question="q") == pa.OBSERVED_CREATED
    await _drain(ors)
    sink.audit.clear(); sink.sent.clear()

    with caplog.at_level(logging.WARNING):
        ended = await asyncio.to_thread(pa.clear, agent, "circuit_dormant", agent)
    assert ended == 1
    await _drain(ors)

    assert [a["event_action"] for a in sink.audit] == ["platform_cleared"], (
        "the platform ending lost its audit row — spawn_on_loop raised from the "
        "default-executor thread and _ended swallowed it")
    assert any("operator_queue_cancelled" in m for m in sink.sent)
    assert not [r for r in caplog.records if "could not schedule" in r.getMessage()]
    assert db.list_operator_queue_items(agent_name=agent, status="pending") == []


@pytest.mark.asyncio
async def test_observe_from_asyncio_to_thread_schedules_its_broadcast(ors, sink, pa, caplog):
    """The skills reconcile observes from `asyncio.to_thread` too; a created
    row's thin `operator_queue_sync` trigger must still be scheduled."""
    ors.remember_host_loop()   # what `main.py::lifespan` does first
    agent = f"agent-3246t-{uuid.uuid4().hex[:6]}"
    calls = []
    orig = ors.spawn_on_loop

    def _spy(factory):
        calls.append(factory)
        return orig(factory)

    # Spy at the seam the alert module reaches through (it imports lazily).
    ors.spawn_on_loop = _spy
    try:
        with caplog.at_level(logging.WARNING):
            out = await asyncio.to_thread(
                pa.observe, agent, "circuit_dormant", agent, title="t", question="q")
        assert out == pa.OBSERVED_CREATED
        await _drain(ors)
    finally:
        ors.spawn_on_loop = orig
    assert calls, "observe never reached the spawn"
    assert not [r for r in caplog.records if "could not schedule" in r.getMessage()]


# ---------------------------------------------------------------------------
# spawn_on_loop itself, from each caller shape
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_spawn_works_from_the_default_executor_thread(ors):
    """`asyncio.to_thread` — the loop's default executor, a thread anyio does
    not own. The loop is known the way it is in production: lifespan captured it."""
    ors.remember_host_loop()
    ran = []

    async def work():
        ran.append(1)

    await asyncio.to_thread(ors.spawn_on_loop, lambda: work())
    await _drain(ors)
    assert ran == [1]


@pytest.mark.asyncio
async def test_spawn_works_from_a_plain_thread_once_the_loop_is_known(ors):
    """A thread neither anyio nor the default executor owns — a `threading.Thread`
    a service starts itself — still reaches the loop that last ran the spawn."""
    import threading
    ran = []

    async def work():
        ran.append(1)

    ors.spawn_on_loop(lambda: work())   # from the loop: the host loop is now known
    await _drain(ors)
    err = []

    def _from_plain_thread():
        try:
            ors.spawn_on_loop(lambda: work())
        except Exception as e:  # noqa: BLE001
            err.append(e)

    t = threading.Thread(target=_from_plain_thread)
    t.start()
    await asyncio.to_thread(t.join)
    await _drain(ors)
    assert err == [] and ran == [1, 1]


def test_lifespan_captures_the_host_loop_before_any_phase_runs():
    """The thread-origin hop has a target only because `main.py::lifespan`
    records the loop first — a boot phase that spawns from `asyncio.to_thread`
    before any on-loop spawn (the system-agent adoption) relies on it."""
    main = (pathlib.Path(_BACKEND) / "main.py").read_text()
    body = main.split("async def lifespan(")[1].split("yield")[0]
    assert "remember_host_loop()" in body
    assert body.index("remember_host_loop()") < body.index("await _init_logging_and_first_run_notice()")


def test_spawn_with_no_loop_anywhere_still_fails_loudly(ors):
    """No running loop, no anyio portal, no live host loop: the one shape that
    must NOT degrade into a silent no-op (the ent#430 class)."""
    ors.remember_host_loop(None)
    with pytest.raises(RuntimeError, match="could not be scheduled"):
        ors.spawn_on_loop(lambda: asyncio.sleep(0))


def test_spawn_ignores_a_host_loop_that_was_closed(ors):
    """A captured loop a test (or a shutdown) closed is not a target — the
    'different loop' footgun from `test_1816`'s private loop."""
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(asyncio.sleep(0))
        ors.remember_host_loop(loop)
    finally:
        loop.close()
    with pytest.raises(RuntimeError, match="could not be scheduled"):
        ors.spawn_on_loop(lambda: asyncio.sleep(0))


def test_spawn_ignores_a_host_loop_that_is_open_but_not_running(ors):
    """A captured loop that is open but not running — a test's private loop
    between `run_until_complete` calls, or one whose `run_forever` returned —
    would accept `call_soon_threadsafe` and queue the task forever. It is not a
    target either: the spawn fails loudly and nothing is parked on that loop."""
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(asyncio.sleep(0))
        ors.remember_host_loop(loop)
        assert not loop.is_closed() and not loop.is_running()
        with pytest.raises(RuntimeError, match="could not be scheduled"):
            ors.spawn_on_loop(lambda: asyncio.sleep(0))
        assert not loop._ready, "a callback was parked on a loop that will never run it"
    finally:
        ors.remember_host_loop(None)
        loop.close()
