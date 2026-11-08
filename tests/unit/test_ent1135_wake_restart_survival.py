"""An ask's wake survives a backend restart (abilityai/trinity-enterprise#1135, T4).

Before this, `wake_state` was written only when a dispatch was SKIPPED or
failed, so a restart between an ending and its turn left NULL and nothing ever
re-offered the wake; an ending wake had no lifecycle at all, and its set-keyed
claim would have fired a fresh turn on every boot once re-listed. Now:

- the ask sink stamps `owed` synchronously BEFORE the spawn, only on rows it
  will wake (never an expiry, an agent-authored ending, an opted-out agent);
- the dispatch flips it `woken` on a turn, `pending` when none ran — for the
  ending wake too;
- the re-offer lists `pending` AND `owed` answered/cancelled/dismissed rows,
  one ending turn per disposition;
- once per process, the operator-queue poller's leader cycle re-offers them
  after startup recovery, reclaiming only in-flight claims older than 30 s.

Harness: the ent#611 `ask` fixture over the real per-process SQLite and the
real idempotency table; the dispatch seam and Docker state stubbed.

Requirement: docs/memory/requirements/security.md §26.9 (Restart survival)
"""
from __future__ import annotations

import ast
import asyncio
import pathlib
import uuid
from types import SimpleNamespace

import pytest

from unit.test_ent611_native_ask import ask, real_db, _body, OWNER  # noqa: F401

pytestmark = pytest.mark.unit


@pytest.fixture()
def wake(ask, monkeypatch):
    import services.docker_utils as docker_utils
    import services.operator_resume_service as ors
    import services.platform_audit_service as pas
    import services.task_execution_service as tes

    calls, spawned = [], []
    state = {"container": "running", "error_code": None}

    async def _dispatch(**kw):
        calls.append(kw)
        return SimpleNamespace(status="failed" if state["error_code"] else "success",
                               response="ok", execution_id=f"exec-{uuid.uuid4().hex[:6]}",
                               error=None, error_code=state["error_code"], session_id=None)

    async def _container(agent):
        return state["container"]

    class _Audit:
        async def log(self, **kw):
            pass

    async def _ready(agent):
        return True

    monkeypatch.setattr(tes, "dispatch_and_await_terminal", _dispatch)
    monkeypatch.setattr(docker_utils, "agent_container_state_async", _container)
    monkeypatch.setattr(pas, "platform_audit_service", _Audit())
    monkeypatch.setattr(ors, "_agent_ready", _ready)
    ask.state["opted_in"] = True
    ask.agent = f"agent-1135r-{uuid.uuid4().hex[:8]}"
    return SimpleNamespace(a=ask, ors=ors, calls=calls, spawned=spawned, state=state)


def _raise(w, rid, **body):
    receipt = w.a.svc.raise_ask(w.a.agent, _body(rid, **body), raised_by="agent", channel="mcp")
    return receipt["id"]


def _state(w, item_id):
    return w.a.db.get_operator_queue_item(item_id)["wake_state"]


def _held_spawns(w, monkeypatch):
    """Record the wake state at the moment of each spawn, and do not run it."""
    seen = []
    monkeypatch.setattr(w.ors, "spawn_resume_dispatch",
                        lambda item, **k: seen.append(("answer", _state(w, item["id"]))))
    monkeypatch.setattr(w.ors, "spawn_ending_dispatch",
                        lambda rows, **k: seen.append(("ending", [_state(w, r["id"]) for r in rows])))
    return seen


def _answer(w, item_id):
    return w.a.svc.answer(w.a.db.get_operator_queue_item(item_id), response="approve",
                          response_text=None, actor=w.a.svc.Actor(email=OWNER))


# ---------------------------------------------------------------------------
# The stamp, before the spawn
# ---------------------------------------------------------------------------

class TestOwedBeforeSpawn:
    def test_an_answer_is_owed_when_its_wake_is_spawned(self, wake, monkeypatch):
        seen = _held_spawns(wake, monkeypatch)
        _answer(wake, _raise(wake, "o-1"))
        assert seen == [("answer", "owed")]

    def test_a_cancel_and_a_dismissal_are_owed_when_spawned(self, wake, monkeypatch):
        seen = _held_spawns(wake, monkeypatch)
        wake.a.svc.cancel(_raise(wake, "o-2"), actor=wake.a.svc.Actor(email=OWNER))
        wake.a.svc.dismiss(_raise(wake, "o-3"), actor=wake.a.svc.Actor(email=OWNER))
        assert seen == [("ending", ["owed"]), ("ending", ["owed"])]

    def test_an_opted_out_agent_is_never_owed(self, wake, monkeypatch):
        seen = _held_spawns(wake, monkeypatch)
        wake.a.state["opted_in"] = False
        item = _raise(wake, "o-4")
        _answer(wake, item)
        assert seen == [] and _state(wake, item) is None

    def test_an_agent_authored_ending_is_never_owed(self, wake, monkeypatch):
        _held_spawns(wake, monkeypatch)
        first = _raise(wake, "o-5")
        _raise(wake, "o-5b", replaces="o-5", proposal={"pay": 1, "to": "o-5b"})
        assert _state(wake, first) is None


# ---------------------------------------------------------------------------
# The dispatch flips it
# ---------------------------------------------------------------------------

class TestLifecycle:
    def test_an_answer_turn_flips_owed_to_woken(self, wake):
        item = _raise(wake, "l-1")
        wake.ors.mark_wake_owed([{"id": item}])
        row = wake.a.db.get_operator_queue_item(item)
        asyncio.run(wake.ors.maybe_dispatch_resume(row, response="approve"))
        assert _state(wake, item) == "woken"

    def test_an_ending_turn_flips_owed_to_woken(self, wake):
        item = _raise(wake, "l-2")
        wake.ors.mark_wake_owed([{"id": item}])
        row = wake.a.db.get_operator_queue_item(item)
        asyncio.run(wake.ors.maybe_dispatch_ending([row], disposition="cancelled"))
        assert _state(wake, item) == "woken"

    def test_an_ending_that_reached_no_agent_stays_owed_as_pending(self, wake):
        item = _raise(wake, "l-3")
        row = wake.a.db.get_operator_queue_item(item)
        wake.state["error_code"] = "network"
        asyncio.run(wake.ors.maybe_dispatch_ending([row], disposition="cancelled"))
        assert _state(wake, item) == "pending"
        # ...and its claim was released: the next try dispatches again
        wake.state["error_code"] = None
        asyncio.run(wake.ors.maybe_dispatch_ending([row], disposition="cancelled"))
        assert len(wake.calls) == 2 and _state(wake, item) == "woken"

    def test_an_ending_for_a_stopped_agent_is_pending(self, wake):
        item = _raise(wake, "l-4")
        wake.state["container"] = "stopped"
        asyncio.run(wake.ors.maybe_dispatch_ending(
            [wake.a.db.get_operator_queue_item(item)], disposition="dismissed"))
        assert wake.calls == [] and _state(wake, item) == "pending"


# ---------------------------------------------------------------------------
# The re-offer
# ---------------------------------------------------------------------------

def _ended(w, rid, how):
    item = _raise(w, rid)
    actor = w.a.svc.Actor(email=OWNER)
    if how == "answered":
        _answer(w, item)
    elif how == "cancelled":
        w.a.svc.cancel(item, actor=actor)
    else:
        w.a.svc.dismiss(item, actor=actor)
    return item


class TestReoffer:
    def test_owed_answers_and_endings_are_re_offered_one_ending_turn_each(self, wake, monkeypatch):
        _held_spawns(wake, monkeypatch)        # the sink's own spawns never run
        answered = _ended(wake, "r-1", "answered")
        cancels = [_ended(wake, f"r-2-{n}", "cancelled") for n in range(2)]
        dismissed = _ended(wake, "r-3", "dismissed")
        n = asyncio.run(wake.ors.redispatch_pending_wakes(wake.a.agent))
        triggers = sorted(c["triggered_by"] for c in wake.calls)
        assert triggers == ["operator_ending", "operator_ending", "operator_response"]
        assert n == 3
        for item in [answered, *cancels, dismissed]:
            assert _state(wake, item) == "woken"
        # nothing owed now: a second pass dispatches nothing
        asyncio.run(wake.ors.redispatch_pending_wakes(wake.a.agent))
        assert len(wake.calls) == 3

    def test_an_ask_that_has_not_ended_is_never_re_offered(self, wake, real_db):
        """Only an ENDED ask is owed a wake — answered, cancelled or dismissed
        (an expiry never wakes, ent#1108, so the sink never stamps it)."""
        item = _raise(wake, "r-4")
        real_db.set_operator_queue_wake_state(item, "owed")
        assert asyncio.run(wake.ors.redispatch_pending_wakes(wake.a.agent)) == 0
        assert wake.calls == []

    @pytest.mark.parametrize("older_than,dispatched", [(0, 1), (3600, 0)])
    def test_only_an_old_in_flight_claim_is_reclaimed(self, wake, monkeypatch,
                                                       older_than, dispatched):
        """A claim the dead process left in flight blocks the re-dispatch
        unless it predates this boot; a young one is this boot's own."""
        from services import idempotency_service as idem
        _held_spawns(wake, monkeypatch)
        item = _ended(wake, f"r-5-{older_than}", "answered")
        idem.begin(idem.make_agent_scope(wake.a.agent),
                   wake.ors._idempotency_key(item, "approve", None))   # in flight, never completed
        asyncio.run(wake.ors.redispatch_pending_wakes(
            wake.a.agent, reclaim_claims_older_than=older_than))
        assert len(wake.calls) == dispatched


class TestBootReoffer:
    @pytest.fixture()
    def boot(self, wake, monkeypatch):
        from services import cleanup_service
        offered = []
        monkeypatch.setattr(wake.ors, "_boot_reoffer_done", False)
        monkeypatch.setattr(wake.ors, "spawn_on_loop", lambda factory: offered.append(factory))
        cleanup_service.reset_startup_recovery_flag_for_tests()
        yield SimpleNamespace(offered=offered, cleanup=cleanup_service)
        cleanup_service.mark_startup_recovery_complete()

    def test_nothing_before_startup_recovery_then_once(self, wake, boot):
        states = {"a-run": "running", "a-stop": "stopped"}
        assert wake.ors.redispatch_pending_wakes_after_boot(states) == 0
        boot.cleanup.mark_startup_recovery_complete()
        assert wake.ors.redispatch_pending_wakes_after_boot(states) == 1
        assert len(boot.offered) == 1                     # the running agent only
        assert wake.ors.redispatch_pending_wakes_after_boot(states) == 0
        assert len(boot.offered) == 1                     # once per process

    def test_unreadable_states_do_nothing_and_try_again(self, wake, boot):
        boot.cleanup.mark_startup_recovery_complete()
        assert wake.ors.redispatch_pending_wakes_after_boot(None) == 0
        assert wake.ors.redispatch_pending_wakes_after_boot({"a": "running"}) == 1

    def test_the_offer_reclaims_with_the_boot_age(self, wake, boot, monkeypatch):
        seen = []

        async def _redispatch(agent, reclaim_claims_older_than=None):
            seen.append((agent, reclaim_claims_older_than))
            return 0

        monkeypatch.setattr(wake.ors, "redispatch_pending_wakes", _redispatch)
        boot.cleanup.mark_startup_recovery_complete()
        wake.ors.redispatch_pending_wakes_after_boot({"a-run": "running"})
        asyncio.run(boot.offered[0]())
        assert seen == [("a-run", wake.ors.BOOT_RECLAIM_AGE_SECONDS)]
        assert wake.ors.BOOT_RECLAIM_AGE_SECONDS == 30


def test_the_poller_leader_cycle_runs_the_boot_reoffer_after_reading_states():
    """The production wiring (tdd question 4), pinned by AST on top of the
    executed tests above: the poller's cycle calls the boot re-offer with the
    tri-state `states`, after the lease check and the `states is None` exit."""
    src = (pathlib.Path(__file__).resolve().parents[2] / "src" / "backend" / "services"
           / "operator_queue_service" / "poller.py").read_text()
    tree = ast.parse(src)
    cycle = next(n for n in ast.walk(tree)
                 if isinstance(n, ast.AsyncFunctionDef) and n.name == "_poll_cycle")
    calls = [n for n in ast.walk(cycle) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute)
             and n.func.attr == "redispatch_pending_wakes_after_boot"]
    assert len(calls) == 1
    assert [a.id for a in calls[0].args if isinstance(a, ast.Name)] == ["states"]
    body = ast.get_source_segment(src, cycle)
    assert body.index("if not leader:") < body.index("if states is None:") \
        < body.index("redispatch_pending_wakes_after_boot")
