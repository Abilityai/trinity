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
  after startup recovery, reclaiming only in-flight claims taken before the
  backend booted, however young — a long-lived worker that gains the lease
  never takes back a sibling's live wake — and re-offering only endings that
  happened before the boot, whose own ending wake died with the old process.

Harness: the ent#611 `ask` fixture over the real per-process SQLite and the
real idempotency table; the dispatch seam and Docker state stubbed.

Requirement: docs/memory/requirements/security.md §26.9 (Restart survival)
"""
from __future__ import annotations

import ast
import asyncio
import pathlib
import sys
import uuid
from datetime import datetime, timedelta, timezone
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

    def test_a_failed_owed_stamp_never_costs_the_wake(self, wake, monkeypatch):
        """The stamp is best-effort: raising, it is logged and the spawn runs."""
        seen = _held_spawns(wake, monkeypatch)

        def _boom(rows):
            raise RuntimeError("db write failed")

        monkeypatch.setattr(wake.ors, "mark_wake_owed", _boom)
        _answer(wake, _raise(wake, "o-1f"))
        wake.a.svc.cancel(_raise(wake, "o-2f"), actor=wake.a.svc.Actor(email=OWNER))
        assert seen == [("answer", None), ("ending", [None])]

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

    @pytest.mark.parametrize("booted,dispatched", [("after", 1), ("before", 0)])
    def test_only_an_in_flight_claim_older_than_the_boot_is_reclaimed(
            self, wake, monkeypatch, booted, dispatched):
        """A claim the dead process left in flight blocks the re-dispatch
        unless it predates this boot; one taken since is this boot's own."""
        from services import idempotency_service as idem
        _held_spawns(wake, monkeypatch)
        shift = timedelta(hours=1) if booted == "after" else -timedelta(hours=1)
        monkeypatch.setattr(wake.ors, "_booted_at", datetime.now(timezone.utc) + shift)
        item = _ended(wake, f"r-5-{booted}", "answered")
        idem.begin(idem.make_agent_scope(wake.a.agent),
                   wake.ors._idempotency_key(item, "approve", None))   # in flight, never completed
        asyncio.run(wake.ors.redispatch_pending_wakes(wake.a.agent, boot_reoffer=True))
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

    def test_the_offer_runs_in_boot_mode(self, wake, boot, monkeypatch):
        seen = []

        async def _redispatch(agent, boot_reoffer=False):
            seen.append((agent, boot_reoffer))
            return 0

        monkeypatch.setattr(wake.ors, "redispatch_pending_wakes", _redispatch)
        boot.cleanup.mark_startup_recovery_complete()
        wake.ors.redispatch_pending_wakes_after_boot({"a-run": "running"})
        asyncio.run(boot.offered[0]())
        assert seen == [("a-run", True)]


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


def test_a_wake_with_no_room_never_loads_the_room_engine(monkeypatch):
    """Every wake asks for its room. Loading `shared_sessions.service` there
    imports `platform_prompt_service`, which binds `database.db` at import — so
    an ask with no room must not load it (the ent#611 seed 3382264541 leak: a
    first import under a stubbed `database` kept the stub for the session)."""
    import shared_sessions
    import services.operator_resume_service as ors
    tried = []

    class _Blocked:
        def find_spec(self, name, path=None, target=None):
            if name == "shared_sessions.service":
                tried.append(name)
                raise ImportError("the room engine was loaded")

    # any import of the room engine now goes through the finder, and is seen
    monkeypatch.delitem(sys.modules, "shared_sessions.service", raising=False)
    monkeypatch.delattr(shared_sessions, "service", raising=False)
    monkeypatch.setattr(sys, "meta_path", [_Blocked(), *sys.meta_path])
    assert ors._return_room({"id": "q", "context": {"thread": "t"}}) is None
    assert ors._return_room({"id": "q", "context": '{"thread": "t"}'}) is None
    assert ors._return_room({"id": "q"}) is None
    assert tried == []


class TestLiveClaimAcrossWorkers:
    """C1: "once per process" is not "once per boot". The poller lease can move
    to a worker that has been up for hours (the leader crashed and was
    respawned, a renewal missed); its first leader cycle runs the re-offer, and
    must not take back a wake another worker is delivering right now."""

    @pytest.fixture()
    def long_lived_leader(self, wake, monkeypatch):
        from services import cleanup_service
        from services import idempotency_service as idem
        offered = []
        _held_spawns(wake, monkeypatch)
        item = _ended(wake, "c1-1", "answered")
        # another worker took the claim and is delivering the turn now
        idem.begin(idem.make_agent_scope(wake.a.agent),
                   wake.ors._idempotency_key(item, "approve", None))
        real_datetime = wake.ors.datetime

        class _FiveMinutesLater(real_datetime):
            @classmethod
            def now(cls, tz=None):
                return real_datetime.now(tz) + timedelta(minutes=5)

        # five minutes into that turn: an age alone never proves its owner dead
        monkeypatch.setattr(wake.ors, "datetime", _FiveMinutesLater)
        monkeypatch.setattr(wake.ors, "_boot_reoffer_done", False)
        monkeypatch.setattr(wake.ors, "spawn_on_loop", lambda factory: offered.append(factory))
        cleanup_service.mark_startup_recovery_complete()
        return SimpleNamespace(item=item, offered=offered)

    def _reoffer(self, wake, offered):
        assert wake.ors.redispatch_pending_wakes_after_boot({wake.a.agent: "running"}) == 1
        for factory in offered:
            asyncio.run(factory())

    def test_a_long_lived_worker_never_re_dispatches_a_live_claim(
            self, wake, long_lived_leader, monkeypatch):
        # this backend booted an hour ago; the claim is five minutes old
        monkeypatch.setattr(wake.ors, "_booted_at",
                            datetime.now(timezone.utc) - timedelta(hours=1))
        self._reoffer(wake, long_lived_leader.offered)
        assert wake.calls == []
        assert _state(wake, long_lived_leader.item) == "owed"

    def test_a_claim_the_previous_backend_left_is_re_dispatched(
            self, wake, long_lived_leader, monkeypatch):
        # the same claim, taken before this backend booted: nobody delivers it
        monkeypatch.setattr(wake.ors, "_booted_at",
                            datetime.now(timezone.utc) + timedelta(minutes=1))
        self._reoffer(wake, long_lived_leader.offered)
        assert [c["triggered_by"] for c in wake.calls] == ["operator_response"]
        assert _state(wake, long_lived_leader.item) == "woken"


def _reoffer_after_boot(wake, monkeypatch):
    """Run the boot re-offer the way the poller's leader cycle does."""
    from services import cleanup_service
    offered = []
    monkeypatch.setattr(wake.ors, "_boot_reoffer_done", False)
    monkeypatch.setattr(wake.ors, "spawn_on_loop", lambda factory: offered.append(factory))
    cleanup_service.mark_startup_recovery_complete()
    assert wake.ors.redispatch_pending_wakes_after_boot({wake.a.agent: "running"}) == 1
    for factory in offered:
        asyncio.run(factory())


class TestYoungClaimTheDeadProcessLeft:
    """F2 (PR #4367 review): the boot comparison alone proves a claim's owner
    is dead. A claim the old process took seconds before it went away is as
    dead as an old one; an age floor on top left that wake owed for 24 h."""

    def test_a_claim_taken_just_before_the_boot_is_re_dispatched_once(self, wake, monkeypatch):
        from services import idempotency_service as idem
        _held_spawns(wake, monkeypatch)
        item = _ended(wake, "f2-1", "answered")
        idem.begin(idem.make_agent_scope(wake.a.agent),
                   wake.ors._idempotency_key(item, "approve", None))   # the old process's claim
        # the backend booted a moment after that claim was taken
        monkeypatch.setattr(wake.ors, "_booted_at",
                            datetime.now(timezone.utc) + timedelta(seconds=1))
        _reoffer_after_boot(wake, monkeypatch)
        assert [c["triggered_by"] for c in wake.calls] == ["operator_response"]
        assert _state(wake, item) == "woken"


def _set_disposed_at(item_id, when):
    from sqlalchemy import update
    from db.engine import get_engine
    from db.tables import operator_queue
    with get_engine().begin() as conn:
        conn.execute(update(operator_queue).where(operator_queue.c.id == item_id)
                     .values(disposed_at=when.strftime("%Y-%m-%dT%H:%M:%S.%fZ")))


class TestBootReofferTakesOnlyPreBootEndings:
    """F1 (PR #4367 review): the boot re-offer regroups owed endings under a
    fresh set key, which can never match the live ending wake's own key. So an
    ending that happened since this backend booted — its wake is live in some
    worker, or will turn the row `pending` itself — is left to that wake."""

    def _live_ending(self, wake, rid):
        from services import idempotency_service as idem
        item = _ended(wake, rid, "cancelled")
        # the live ending wake holds the claim keyed on its own event set
        idem.begin(idem.make_agent_scope(wake.a.agent),
                   wake.ors._ending_idempotency_key(wake.a.agent, "cancelled", [item]))
        return item

    def test_a_pre_restart_ending_is_re_offered_without_a_live_one(self, wake, monkeypatch):
        _held_spawns(wake, monkeypatch)
        now = datetime.now(timezone.utc)
        old = _ended(wake, "f1a-old", "cancelled")
        _set_disposed_at(old, now - timedelta(hours=1))
        monkeypatch.setattr(wake.ors, "_booted_at", now - timedelta(minutes=30))
        live = self._live_ending(wake, "f1a-live")
        _reoffer_after_boot(wake, monkeypatch)
        assert [c["triggered_by"] for c in wake.calls] == ["operator_ending"]
        assert "f1a-old" in wake.calls[0]["message"]
        assert "f1a-live" not in wake.calls[0]["message"]
        assert _state(wake, old) == "woken"
        assert _state(wake, live) == "owed"            # still its live wake's

    def test_endings_since_the_boot_are_never_re_offered(self, wake, monkeypatch):
        _held_spawns(wake, monkeypatch)
        monkeypatch.setattr(wake.ors, "_booted_at",
                            datetime.now(timezone.utc) - timedelta(minutes=30))
        live = [self._live_ending(wake, f"f1b-{n}") for n in range(2)]
        _reoffer_after_boot(wake, monkeypatch)
        assert [c for c in wake.calls if c["triggered_by"] == "operator_ending"] == []
        assert [_state(wake, i) for i in live] == ["owed", "owed"]
