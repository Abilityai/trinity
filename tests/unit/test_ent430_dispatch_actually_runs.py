"""ent#430 review — the dispatch must actually run from the route that calls it.

WHY THIS FILE EXISTS. The ent#430 suite was fully green while the feature was
inert in production, because every test replaced `spawn_resume_dispatch` with a
synchronous lambda — stubbing out the one call whose RUNTIME CONTEXT was the
defect.

`client_portal/asks/router.py` declares `answer_ask` as a plain `def`. FastAPI
runs a non-coroutine endpoint through `run_in_threadpool`, i.e. a worker thread
with NO running event loop, and `asyncio.create_task` raises
`RuntimeError: no running event loop` there. The caller's `except Exception`
then swallowed it, so every client answer recorded the answer and dispatched
nothing.

So these tests drive the REAL `spawn_resume_dispatch` from a real worker thread.
The stubbing rule for this file: stub what the dispatch DOES, never how it is
scheduled.
"""
from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def resume_mod(monkeypatch):
    """The real `operator_resume_service`, with only `maybe_dispatch_resume`
    replaced — so scheduling is exercised for real and the work is not."""
    import services.operator_resume_service as mod

    ran: list = []

    async def _fake_dispatch(item, **kwargs):
        ran.append((item, kwargs))
        return "exec-1"

    monkeypatch.setattr(mod, "maybe_dispatch_resume", _fake_dispatch)
    return mod, ran


@pytest.mark.asyncio
async def test_the_spawn_works_from_a_worker_thread(resume_mod):
    """THE REGRESSION. A sync FastAPI endpoint runs here; `create_task` cannot.

    Uses anyio's threadpool — the same one Starlette's `run_in_threadpool` uses —
    so this is the production context, not an approximation.
    """
    import anyio.to_thread

    mod, ran = resume_mod

    def _sync_caller():
        # Proves the premise rather than assuming it: no loop on this thread.
        with pytest.raises(RuntimeError):
            asyncio.get_running_loop()
        mod.spawn_resume_dispatch({"id": "q1", "agent_name": "a"}, response="PDF")

    await anyio.to_thread.run_sync(_sync_caller)
    await asyncio.sleep(0)          # let the scheduled task start
    for _ in range(20):
        if ran:
            break
        await asyncio.sleep(0.01)

    assert ran, (
        "spawn_resume_dispatch scheduled nothing from a worker thread — this is "
        "the ent#430 defect: the Workspace ask route is a sync `def`, so it runs "
        "in a threadpool with no event loop and `create_task` raises."
    )
    assert ran[0][0]["id"] == "q1"


@pytest.mark.asyncio
async def test_the_spawn_still_works_from_the_async_operator_route(resume_mod):
    """The operator route is `async def` and must keep working unchanged."""
    mod, ran = resume_mod
    mod.spawn_resume_dispatch({"id": "q2", "agent_name": "a"}, response="Approve")
    for _ in range(20):
        if ran:
            break
        await asyncio.sleep(0.01)
    assert ran and ran[0][0]["id"] == "q2"


# ---------------------------------------------------------------------------
# The lost race — the shape that actually occurs
# ---------------------------------------------------------------------------

def _install(monkeypatch, *, respond_result, enabled=True):
    """Stub the DB and the roster; leave the dispatch decision real."""
    import client_portal.asks.service as svc

    calls: list = []
    monkeypatch.setattr(svc, "_on_roster", lambda *a, **k: True, raising=False)
    monkeypatch.setattr(
        svc, "db",
        SimpleNamespace(
            get_operator_queue_item=lambda i: {
                "id": i, "agent_name": "a", "type": "question",
                "status": "pending", "options": ["PDF"],
                "addressed_to_email": "x@example.com",
            },
            respond_to_operator_queue_item=lambda **kw: respond_result,
            get_operator_resume_enabled=lambda a: enabled,
        ),
        raising=False,
    )
    # BOTH, not one. `answer_ask` does `from services import
    # operator_resume_service`, which resolves the PACKAGE ATTRIBUTE and shadows
    # the `sys.modules` entry — patching only the latter leaves the real function
    # running, which is how the first draft of this file drove the real spawn by
    # accident. (learnings.md 2026-08-27 records this exact resolution trap.)
    import services as services_pkg

    stub = SimpleNamespace(spawn_resume_dispatch=lambda item, **kw: calls.append(item))
    monkeypatch.setitem(sys.modules, "services.operator_resume_service", stub)
    monkeypatch.setattr(services_pkg, "operator_resume_service", stub, raising=False)
    return svc, calls


def test_the_lost_race_shape_that_actually_happens_dispatches_nothing(monkeypatch):
    """`respond_to_operator_queue_item` returns None only when the row is GONE.

    When the row exists and has left `pending` — the race that actually occurs —
    it returns a TRUTHY dict carrying `_status_conflict`, having written nothing.
    `if not updated` alone falls through on exactly that shape, and this path then
    SPENDS: a paid execution for an answer not in the database, under an
    idempotency key hashed from the losing text, so one queue item yields two
    dispatches.
    """
    svc, calls = _install(
        monkeypatch,
        respond_result={"id": "q1", "agent_name": "a", "status": "responded",
                        "_status_conflict": True},
    )
    with pytest.raises(svc.AskError) as e:
        svc.answer_ask("q1", "x@example.com", False, "PDF", None)
    assert e.value.status_code == 409
    assert not calls, "a race loser dispatched a paid execution"


def test_a_missing_row_still_409s(monkeypatch):
    """The falsy shape must keep working — this is the case the old check saw."""
    svc, calls = _install(monkeypatch, respond_result=None)
    with pytest.raises(svc.AskError) as e:
        svc.answer_ask("q1", "x@example.com", False, "PDF", None)
    assert e.value.status_code == 409
    assert not calls


def test_the_conflict_sentinel_never_reaches_the_client(monkeypatch):
    """Popped, not read — otherwise `_status_conflict` serializes to the client."""
    svc, _ = _install(
        monkeypatch,
        respond_result={"id": "q1", "agent_name": "a", "status": "responded",
                        "_status_conflict": True},
    )
    with pytest.raises(svc.AskError):
        svc.answer_ask("q1", "x@example.com", False, "PDF", None)


# ---------------------------------------------------------------------------
# resume_requested must describe what happened, not what was permitted
# ---------------------------------------------------------------------------

def test_resume_requested_is_false_when_the_spawn_raised(monkeypatch):
    """AC #5's over-claim. The flag being ON is not evidence anything ran."""
    svc, _ = _install(
        monkeypatch,
        respond_result={"id": "q1", "agent_name": "a", "status": "responded"},
    )

    def _boom(item, **kw):
        raise RuntimeError("no running event loop")

    import services as services_pkg

    stub = SimpleNamespace(spawn_resume_dispatch=_boom)
    monkeypatch.setitem(sys.modules, "services.operator_resume_service", stub)
    monkeypatch.setattr(services_pkg, "operator_resume_service", stub, raising=False)
    out = svc.answer_ask("q1", "x@example.com", False, "PDF", None)
    assert out.resume_requested is False, (
        "a spawn that raised still reported resume_requested=true — an ask that "
        "reads as acted upon while nothing happened (AC #5)"
    )


def test_resume_requested_is_false_when_the_agent_opted_out(monkeypatch):
    svc, calls = _install(
        monkeypatch,
        respond_result={"id": "q1", "agent_name": "a", "status": "responded"},
        enabled=False,
    )
    out = svc.answer_ask("q1", "x@example.com", False, "PDF", None)
    assert out.resume_requested is False
    assert not calls, "opted-out agent must not be dispatched at all"


def test_resume_requested_is_true_only_on_a_real_schedule(monkeypatch):
    svc, calls = _install(
        monkeypatch,
        respond_result={"id": "q1", "agent_name": "a", "status": "responded"},
    )
    out = svc.answer_ask("q1", "x@example.com", False, "PDF", None)
    assert out.resume_requested is True
    assert len(calls) == 1
