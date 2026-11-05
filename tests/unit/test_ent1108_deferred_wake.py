"""
An answered ask whose agent could not be woken is delivered when the agent is
next able (abilityai/trinity-enterprise#1108, AC #4).
Related flow: docs/memory/feature-flows/operating-room.md (the wake)

The answer wake (`operator_resume_service.maybe_dispatch_resume`) skips an
agent that is stopped or missing — a wake for it would be a FAILED execution
and nothing else. The ask now records that: `operator_queue.wake_state` is
`pending` ("agent not yet woken"), and the agent's next start re-dispatches
every pending answered ask of its ONCE through the same path, after which the
ask reads `woken`. A wake that produced a turn reads `woken` at once. The
Execution Context line (`ended_asks`) is the belt-and-braces path: a turn
composed with a pending ask in that line marks it `woken` too, so the status
never claims a wake that already happened.

Shapes, each at the layer the change lives in: the column and its three
accessors (tmp SQLite over the real DDL); the dispatch decision (collaborators
stubbed, the service real); the re-dispatch; the start hook (the real
`start_agent_internal` under the #421 harness); the receipt and the Workspace
projection; both migration tracks.
"""
from __future__ import annotations

import asyncio
import importlib.util
import inspect
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_backend = str(Path(__file__).resolve().parents[2] / "src" / "backend")
if _backend not in sys.path:
    sys.path.insert(0, _backend)

pytestmark = pytest.mark.unit

_REVISION = "0117_operator_queue_wake_state"
_PARENT = "0116_operator_resume_default_on"
NOW = "2026-11-04T10:00:00.000000Z"


# ---------------------------------------------------------------------------
# The column and its accessors
# ---------------------------------------------------------------------------

@pytest.fixture
def queue_db(tmp_path, monkeypatch):
    db_path = tmp_path / "trinity.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_path))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    from db.schema import TABLES
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute(TABLES["operator_queue"])
    conn.commit()
    import db.engine as engine_mod
    engine_mod.dispose_engines()
    yield conn
    engine_mod.dispose_engines()
    conn.close()


@pytest.fixture
def ops(queue_db):
    from db.operator_queue import OperatorQueueOperations
    return OperatorQueueOperations()


def _answered(conn, uid, *, agent="agent-w", wake_state=None, status="responded",
              disposition="answered", request_id=None):
    conn.execute(
        "INSERT INTO operator_queue (id, agent_name, request_id, type, status, priority, title,"
        " question, created_at, response, response_text, responded_by_email, disposition,"
        " disposed_at, wake_state) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (uid, agent, request_id or f"req-{uid}", "question", status, "medium", "t", "q", NOW,
         "Build it", None, "person@example.com", disposition, NOW, wake_state))
    conn.commit()


class TestTheColumn:
    def test_the_row_carries_the_wake_state(self, ops, queue_db):
        _answered(queue_db, "a1", wake_state="pending")
        assert ops.get_item("a1")["wake_state"] == "pending"

    def test_set_wake_state_writes_and_reads_back(self, ops, queue_db):
        _answered(queue_db, "a1")
        assert ops.set_wake_state("a1", "pending") is True
        assert ops.get_item("a1")["wake_state"] == "pending"
        assert ops.set_wake_state("a1", "woken") is True
        assert ops.get_item("a1")["wake_state"] == "woken"
        assert ops.set_wake_state("no-such", "woken") is False

    def test_pending_wakes_are_this_agents_answered_asks_not_yet_woken(self, ops, queue_db):
        _answered(queue_db, "p1", wake_state="pending")
        _answered(queue_db, "p2", wake_state="pending", status="acknowledged")
        _answered(queue_db, "w1", wake_state="woken")
        _answered(queue_db, "n1", wake_state=None)
        _answered(queue_db, "o1", wake_state="pending", agent="other")
        _answered(queue_db, "c1", wake_state="pending", status="cancelled", disposition="cancelled")
        assert sorted(r["id"] for r in ops.list_pending_wakes_for_agent("agent-w")) == ["p1", "p2"]
        row = ops.list_pending_wakes_for_agent("agent-w")[0]
        assert row["response"] == "Build it" and row["responded_by_email"] == "person@example.com"

    def test_mark_wakes_woken_flips_only_the_named_pending_rows(self, ops, queue_db):
        _answered(queue_db, "p1", wake_state="pending", request_id="r1")
        _answered(queue_db, "p2", wake_state="pending", request_id="r2")
        _answered(queue_db, "o1", wake_state="pending", request_id="r1", agent="other")
        assert ops.mark_wakes_woken("agent-w", ["r1", "nope"]) == 1
        assert ops.get_item("p1")["wake_state"] == "woken"
        assert ops.get_item("p2")["wake_state"] == "pending"
        assert ops.get_item("o1")["wake_state"] == "pending"
        assert ops.mark_wakes_woken("agent-w", []) == 0


# ---------------------------------------------------------------------------
# The dispatch decision and the re-dispatch (service layer)
# ---------------------------------------------------------------------------

ITEM = {"id": "item-1", "request_id": "req-1", "agent_name": "parked-agent",
        "title": "Approve?", "question": "Build it?", "response": "Build it",
        "response_text": None, "responded_by_email": "person@example.com"}


def _install(monkeypatch, *, state="running", enabled=True, pending=None, raises=None,
             result_status="success", result_error_code=None):
    """The ent#329 / ent#611 stub shape: collaborators in BOTH sys.modules and
    the `services` package attribute; `docker_utils` loaded for real first."""
    import services as _services_pkg
    import services.docker_utils  # noqa: F401
    # The ending wake imports this lazily (`_wakes_on_ending`); loaded for real
    # BEFORE the fake `database` is in `sys.modules`, or its `from database
    # import db` binds the fake for every later test in the session.
    import services.operator_queue_service  # noqa: F401

    calls, audit, wake_writes, idem = [], [], [], []

    def _mod(dotted, module):
        monkeypatch.setitem(sys.modules, dotted, module)
        if dotted.startswith("services."):
            monkeypatch.setattr(_services_pkg, dotted.split(".", 1)[1], module, raising=False)

    fake_db = SimpleNamespace(
        get_operator_resume_enabled=lambda name: enabled,
        set_operator_queue_wake_state=lambda item_id, s: wake_writes.append((item_id, s)) or True,
        list_operator_queue_pending_wakes=lambda name: list(pending or []),
    )
    monkeypatch.setitem(sys.modules, "database", SimpleNamespace(db=fake_db))
    _mod("services.idempotency_service", SimpleNamespace(
        begin=lambda scope, key: SimpleNamespace(replay=False, in_flight=False),
        complete=lambda *a, **k: idem.append("complete"),
        fail=lambda *a, **k: idem.append("fail"),
        make_agent_scope=lambda name: f"agent:{name}"))

    class _Audit:
        async def log(self, **kw):
            audit.append(kw)

    _mod("services.platform_audit_service", SimpleNamespace(
        platform_audit_service=_Audit(), AuditEventType=SimpleNamespace(EXECUTION="execution")))

    async def _adapter(**kw):
        calls.append(kw)
        if raises:
            raise raises
        return SimpleNamespace(execution_id=f"exec-{len(calls)}", status=result_status,
                               error="refused" if result_error_code else None,
                               error_code=result_error_code)

    _mod("services.task_execution_service", SimpleNamespace(
        dispatch_and_await_terminal=_adapter, get_task_execution_service=lambda: None))
    _mod("services.docker_service", SimpleNamespace(agent_container_state=lambda name: state))
    return SimpleNamespace(calls=calls, audit=audit, wake_writes=wake_writes, idem=idem, db=fake_db)


def _readiness(ors, monkeypatch, *, ready=True, events=None):
    """Stub the agent-server readiness wait the re-dispatch must pass first."""
    async def _probe(agent_name):
        if events is not None:
            events.append("ready-wait")
        return ready
    monkeypatch.setattr(ors, "_agent_ready", _probe, raising=False)


@pytest.fixture
def ors():
    import services.operator_resume_service as mod
    return mod


class TestTheAnswerWake:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("state", ["stopped", "missing"])
    async def test_a_stopped_agents_answered_ask_reads_pending(self, ors, monkeypatch, state):
        w = _install(monkeypatch, state=state)
        assert await ors.maybe_dispatch_resume(ITEM, response="Build it",
                                               responded_by_email="person@example.com") is None
        assert w.calls == [], "a wake for a stopped agent is a FAILED execution and nothing else"
        assert w.wake_writes == [("item-1", "pending")]
        assert [a["details"]["status"] for a in w.audit] == ["skipped_not_running"]

    @pytest.mark.asyncio
    async def test_a_normal_wake_reads_woken(self, ors, monkeypatch):
        w = _install(monkeypatch)
        assert await ors.maybe_dispatch_resume(ITEM, response="Build it") == "exec-1"
        assert w.wake_writes == [("item-1", "woken")]

    @pytest.mark.asyncio
    async def test_a_dispatch_that_raised_before_a_turn_reads_pending(self, ors, monkeypatch):
        w = _install(monkeypatch, raises=RuntimeError("refused"))
        assert await ors.maybe_dispatch_resume(ITEM, response="Build it") is None
        assert w.wake_writes == [("item-1", "pending")]

    @pytest.mark.asyncio
    async def test_a_turn_that_never_reached_the_agent_reads_pending(self, ors, monkeypatch):
        """`error_code = network` is the platform's own "HTTP/connection error to
        the agent container": no turn ran, so the wake is still owed and the
        idempotency claim is RELEASED (`fail`), or the next start's re-dispatch
        would replay into nothing."""
        w = _install(monkeypatch, result_status="failed", result_error_code="network")
        assert await ors.maybe_dispatch_resume(ITEM, response="Build it") is None
        assert w.wake_writes == [("item-1", "pending")]
        assert w.idem == ["fail"]

    @pytest.mark.asyncio
    async def test_a_turn_that_ran_and_failed_still_reads_woken(self, ors, monkeypatch):
        w = _install(monkeypatch, result_status="failed", result_error_code="agent_error")
        assert await ors.maybe_dispatch_resume(ITEM, response="Build it") == "exec-1"
        assert w.wake_writes == [("item-1", "woken")]
        assert w.idem == ["complete"]

    @pytest.mark.asyncio
    async def test_an_owner_off_agent_records_nothing(self, ors, monkeypatch):
        w = _install(monkeypatch, state="stopped", enabled=False)
        assert await ors.maybe_dispatch_resume(ITEM, response="Build it") is None
        assert w.wake_writes == [] and w.calls == []


class TestTheRedispatch:
    @pytest.mark.asyncio
    async def test_starting_the_agent_redispatches_each_pending_ask_once(self, ors, monkeypatch):
        row = {**ITEM, "wake_state": "pending"}
        w = _install(monkeypatch, pending=[row])
        _readiness(ors, monkeypatch, ready=True)
        assert await ors.redispatch_pending_wakes("parked-agent") == 1
        assert len(w.calls) == 1 and w.calls[0]["agent_name"] == "parked-agent"
        assert "Build it" in w.calls[0]["message"]
        assert w.wake_writes == [("item-1", "woken")]

    @pytest.mark.asyncio
    async def test_readiness_that_never_arrives_dispatches_nothing_and_rows_stay_pending(self, ors, monkeypatch):
        """The container is "running" the moment it is started, but the agent
        server is not up yet; a dispatch now fails on a refused connection and
        would complete the idempotency key with the ask stamped `woken`."""
        row = {**ITEM, "wake_state": "pending"}
        w = _install(monkeypatch, pending=[row])
        _readiness(ors, monkeypatch, ready=False)
        assert await ors.redispatch_pending_wakes("parked-agent") == 0
        assert w.calls == [] and w.wake_writes == []

    @pytest.mark.asyncio
    async def test_readiness_is_awaited_before_the_first_dispatch(self, ors, monkeypatch):
        events = []
        row = {**ITEM, "wake_state": "pending"}
        w = _install(monkeypatch, pending=[row])
        _readiness(ors, monkeypatch, ready=True, events=events)
        original = w.db.list_operator_queue_pending_wakes
        w.db.list_operator_queue_pending_wakes = lambda name: events.append("list") or original(name)
        await ors.redispatch_pending_wakes("parked-agent")
        events.append("dispatched") if w.calls else None
        assert events == ["ready-wait", "list", "dispatched"]

    @pytest.mark.asyncio
    async def test_a_second_start_with_nothing_pending_dispatches_nothing(self, ors, monkeypatch):
        w = _install(monkeypatch, pending=[])
        _readiness(ors, monkeypatch, ready=True)
        assert await ors.redispatch_pending_wakes("parked-agent") == 0
        assert w.calls == [] and w.wake_writes == []

    @pytest.mark.asyncio
    async def test_a_list_failure_never_raises(self, ors, monkeypatch):
        w = _install(monkeypatch)
        _readiness(ors, monkeypatch, ready=True)

        def _boom(name):
            raise RuntimeError("db gone")

        w.db.list_operator_queue_pending_wakes = _boom
        assert await ors.redispatch_pending_wakes("parked-agent") == 0

    def test_spawn_hands_the_redispatch_to_the_loop(self, ors, monkeypatch):
        spawned = []
        monkeypatch.setattr(ors, "spawn_on_loop", lambda factory: spawned.append(factory))
        ors.spawn_pending_wakes("parked-agent")
        assert len(spawned) == 1
        assert inspect.iscoroutine(coro := spawned[0]())
        coro.close()


# ---------------------------------------------------------------------------
# The readiness probe is lifecycle's own wait, verdict and all
# ---------------------------------------------------------------------------

class TestTheReadinessProbe:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("verdict", [True, False])
    async def test_agent_ready_hands_off_to_wait_for_agent_ready(self, ors, monkeypatch, verdict):
        """`_agent_ready` is `lifecycle.wait_for_agent_ready` for this agent —
        its verdict, both ways. A `return True` stand-in dispatches into a
        container whose server is not up (a refused connection, then an ask
        stamped `woken` under a completed key); this pins the hand-off."""
        import importlib
        from unittest.mock import AsyncMock
        lifecycle = importlib.import_module("services.agent_service.lifecycle")
        probe = AsyncMock(return_value=verdict)
        monkeypatch.setattr(lifecycle, "wait_for_agent_ready", probe)
        assert await ors._agent_ready("agent-w") is verdict
        probe.assert_awaited_once_with("agent-w")


# ---------------------------------------------------------------------------
# The start hook (tdd question 4: delete the registration line → red)
# ---------------------------------------------------------------------------

class TestTheStartHook:
    def test_the_real_start_path_spawns_the_redispatch(self, monkeypatch):
        """`start_agent_internal` under the #421 harness (every Docker and DB
        collaborator stubbed), with the hook's one entry point recorded."""
        # Loaded by path (the unit island is not a package); its own `conftest`
        # import needs this directory on sys.path. A load failure FAILS — a
        # skipped pin would hide a deleted hook line.
        here = str(Path(__file__).parent)
        if here not in sys.path:
            sys.path.insert(0, here)
        spec = importlib.util.spec_from_file_location(
            "start_harness_ent1108", Path(__file__).with_name("test_start_agent_skip_inject.py"))
        harness = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(harness)
        from unittest.mock import AsyncMock, Mock
        import services.operator_resume_service as ors

        mod = harness._mod
        container = harness._make_container("running")
        harness._mock_docker_service.get_agent_container.return_value = container
        mod.get_agent_container = harness._mock_docker_service.get_agent_container
        for name in ("check_api_key_env_matches", "check_github_pat_env_matches",
                     "check_resource_limits_match", "check_full_capabilities_match",
                     "check_guardrails_env_matches", "check_public_folder_mount_matches"):
            monkeypatch.setattr(mod, name, Mock(return_value=True), raising=False)
        monkeypatch.setattr(mod, "check_shared_folder_mounts_match", AsyncMock(return_value=True), raising=False)
        monkeypatch.setattr(mod, "check_base_image_matches", AsyncMock(return_value=True), raising=False)
        harness._mock_db.get_read_only_mode.return_value = {"enabled": False}

        # The start path also spawns the ent#752 skill-gate marker sync — a
        # task on `harness._run`'s THROWAWAY loop, which that harness closes.
        # Left in `skill_gate_service._inflight`, the dead task makes every
        # later `gather(*_inflight)` in test_ent752_marker_sync raise "The
        # future belongs to a different loop". Stubbed here, so test order
        # cannot matter; this test pins the wake hook, not the marker.
        import services.skill_gate_service as gate
        monkeypatch.setattr(gate, "spawn_gate_marker_sync", Mock(return_value=False))

        woken = []
        monkeypatch.setattr(ors, "spawn_pending_wakes", lambda name: woken.append(name))
        harness._run(mod.start_agent_internal("agent-a"))
        assert woken == ["agent-a"]
        assert gate.spawn_gate_marker_sync.call_args.args == ("agent-a",)

    def test_the_hook_line_is_in_the_start_path(self):
        """Source pin on top of the executing test above (the ent#477 shape)."""
        from services.agent_service import lifecycle
        assert "spawn_pending_wakes(agent_name)" in inspect.getsource(lifecycle.start_agent_internal)


# ---------------------------------------------------------------------------
# A turn that carries a pending answer marks it woken at ITS terminal, not
# when its prompt is composed (merge-train ruling on AC #4)
# ---------------------------------------------------------------------------

def _ended(rid, disposition="answered"):
    return {"request_id": rid, "disposition": disposition, "disposed_at": NOW,
            "disposed_by": "person", "replaced_by": None}


def _ctx(pps, ended, execution_id="exec-9"):
    """A context with every auto-filled field pre-filled, so composing reads no
    DB and the ended-asks line is the only thing under test."""
    return pps.ExecutionContext(
        agent_name="agent-w", triggered_by="schedule", execution_id=execution_id,
        ended_asks=ended, collaborators=[], platform_url="http://localhost",
        primary_user_display="", role_id="", stakeholders=[], proactive_consent=False,
        pending_asks=[], pending_asks_total=0, served_role_id="",
    )


class TestTheCarriedWakeIsMarkedAtTheTerminal:
    """Composing a turn proves nothing reached the agent: a schedule firing at
    a stopped agent composes, the HTTP call fails, and marking at compose time
    dropped the pending wake silently (AC #4 forbids that). The compose step
    stashes the answers the turn LISTS; the execution's SUCCESS terminal marks
    them; any other terminal leaves them `pending` for the next start."""

    @pytest.fixture(autouse=True)
    def _clean(self, ors):
        ors._carried_wakes.clear()
        yield
        ors._carried_wakes.clear()

    @pytest.fixture
    def marked(self, monkeypatch):
        """The real `database.db` facade with the one writer recorded — never a
        fake module in `sys.modules`, which a lazy `from database import db`
        in a module first imported here would bind for the whole session."""
        import database
        import services.platform_prompt_service as pps
        marked = []
        monkeypatch.setattr(database.db, "mark_operator_queue_wakes_woken",
                            lambda agent, ids: marked.append((agent, list(ids))) or 1)
        monkeypatch.setattr(pps, "db", database.db)
        # The platform instructions read a setting; not under test here.
        monkeypatch.setattr(pps, "get_platform_system_prompt", lambda **k: "PLATFORM")
        return marked

    def test_the_resolver_reads_and_marks_nothing(self, monkeypatch, marked):
        import database
        import services.platform_prompt_service as pps
        rows = [_ended("r1"), _ended("r2", "cancelled")]
        monkeypatch.setattr(database.db, "list_recent_operator_queue_endings",
                            lambda *a, **k: list(rows))
        assert pps._resolve_ended_asks("agent-w") == rows
        assert marked == []

    def test_composing_marks_nothing_and_stashes_only_the_listed_answers(self, ors, marked):
        import services.platform_prompt_service as pps
        # MAX+2 rows: the renderer lists the first MAX; the overflow row only
        # makes the line say "and more" and is NOT carried.
        ended = [_ended(f"r{i}") for i in range(1, pps.MAX_ENDED_ASKS + 1)]
        ended[2] = _ended("r3", "cancelled")
        ended += [_ended("r-overflow-1"), _ended("r-overflow-2")]
        prompt = pps.compose_system_prompt(_ctx(pps, ended))
        assert "r1 answered" in prompt and "and more" in prompt
        assert marked == []
        assert ors._carried_wakes["exec-9"] == ("agent-w", ("r1", "r2", "r4", "r5"))

    def test_a_turn_with_no_execution_id_stashes_nothing(self, ors, marked):
        import services.platform_prompt_service as pps
        pps.compose_system_prompt(_ctx(pps, [_ended("r1")], execution_id=None))
        assert ors._carried_wakes == {}

    @pytest.mark.asyncio
    async def test_the_success_terminal_marks_the_carried_answers_once(self, ors, marked):
        ors.note_carried_wakes("exec-9", "agent-w", ["r1", "r2"])
        await ors.mark_carried_wakes("exec-9", "success")
        assert marked == [("agent-w", ["r1", "r2"])]
        await ors.mark_carried_wakes("exec-9", "success")
        assert marked == [("agent-w", ["r1", "r2"])], "popped on first terminal"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["failed", "cancelled", "skipped"])
    async def test_any_other_terminal_leaves_the_row_pending(self, ors, marked, status):
        from models import TaskExecutionStatus
        ors.note_carried_wakes("exec-9", "agent-w", ["r1"])
        await ors.mark_carried_wakes("exec-9", TaskExecutionStatus(status))
        assert marked == []
        assert "exec-9" not in ors._carried_wakes

    @pytest.mark.asyncio
    async def test_a_terminal_for_an_unknown_execution_is_a_no_op(self, ors, marked):
        await ors.mark_carried_wakes("exec-none", "success")
        await ors.mark_carried_wakes(None, "success")
        assert marked == []

    @pytest.mark.asyncio
    async def test_the_stopped_agent_scenario_end_to_end(self, ors, ops, queue_db, monkeypatch):
        """The row is real: answered, `pending`. A composed turn whose dispatch
        FAILS leaves it pending; a later turn that completes marks it woken."""
        import services.platform_prompt_service as pps
        from database import db as real_db
        monkeypatch.setattr(pps, "db", real_db)
        monkeypatch.setattr(pps, "get_platform_system_prompt", lambda **k: "PLATFORM")
        _answered(queue_db, "p1", wake_state="pending", request_id="r1")
        pps.compose_system_prompt(_ctx(pps, [_ended("r1")], execution_id="exec-fail"))
        await ors.mark_carried_wakes("exec-fail", "failed")
        assert ops.get_item("p1")["wake_state"] == "pending"
        pps.compose_system_prompt(_ctx(pps, [_ended("r1")], execution_id="exec-ok"))
        await ors.mark_carried_wakes("exec-ok", "success")
        assert ops.get_item("p1")["wake_state"] == "woken"

    def test_every_terminal_writer_reaches_the_marker(self, ors, monkeypatch):
        """`spawn_task_terminal_event` is the one fan-out every CAS-won terminal
        writer calls (push, #1083 callback, pull sink, sweeps): the marker hangs
        there, beside the loop advance and the fan-out join."""
        import services.event_dispatch_service as eds
        import services.memory_service as mem
        seen = []

        async def _noop(*a, **k):
            return None

        async def _mark(execution_id, terminal_status):
            seen.append((execution_id, getattr(terminal_status, "value", terminal_status)))

        monkeypatch.setattr(eds, "emit_task_terminal_event", _noop)
        monkeypatch.setattr(eds, "_terminal_side_effects", _noop)
        monkeypatch.setattr(mem, "spawn_retain_for_execution", lambda eid: None)
        monkeypatch.setattr(ors, "mark_carried_wakes", _mark)

        async def main():
            from models import TaskExecutionStatus
            eds.spawn_task_terminal_event("agent-w", "exec-9", terminal_status=TaskExecutionStatus.SUCCESS)
            for _ in range(3):
                await asyncio.sleep(0)

        asyncio.run(main())
        assert seen == [("exec-9", "success")]


# ---------------------------------------------------------------------------
# The receipt and the Workspace projection carry the status
# ---------------------------------------------------------------------------

def test_the_receipt_carries_the_wake_state(monkeypatch):
    import services.ask_service as ask
    monkeypatch.setattr(ask, "_opted_in", lambda name: True)
    row = {"id": "u1", "request_id": "r1", "agent_name": "agent-w", "status": "responded",
           "raised_by": "agent", "channel": "mcp", "type": "question", "to_role": None,
           "disposition": "answered", "disposed_at": NOW, "expires_at": None,
           "wake_state": "pending"}
    assert ask._receipt(row, status="created")["wake_state"] == "pending"
    assert ask._receipt({**row, "wake_state": None}, status="created")["wake_state"] is None


def test_the_workspace_ask_carries_the_wake_state():
    from client_portal.asks import service as asks
    item = {"id": "u1", "agent_name": "agent-w", "type": "question", "status": "responded",
            "title": "t", "question": "q", "created_at": NOW, "disposition": "answered",
            "disposed_at": NOW, "responded_by_email": "person@example.com",
            "addressed_to_email": "person@example.com", "wake_state": "pending"}
    projected = asks._project(item, viewer_email="person@example.com")
    assert projected.status == "answered" and projected.wake_state == "pending"
    assert asks._project({**item, "wake_state": None}, viewer_email="person@example.com").wake_state is None


# ---------------------------------------------------------------------------
# Both migration tracks
# ---------------------------------------------------------------------------

def test_the_sqlite_migration_adds_the_column_and_is_registered():
    from db.migrations import MIGRATIONS, _migrate_operator_queue_wake_state
    names = [name for name, _ in MIGRATIONS]
    assert "operator_queue_wake_state" in names
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE operator_queue (id TEXT PRIMARY KEY, agent_name TEXT NOT NULL)")
    _migrate_operator_queue_wake_state(conn.cursor(), conn)
    _migrate_operator_queue_wake_state(conn.cursor(), conn)  # idempotent
    cols = [r[1] for r in conn.execute("PRAGMA table_info(operator_queue)")]
    assert cols.count("wake_state") == 1


def test_the_alembic_revision_is_chained_on_0116():
    path = Path(__file__).resolve().parents[2] / "src/backend/migrations/versions" / f"{_REVISION}.py"
    spec = importlib.util.spec_from_file_location("rev0117_ent1108", path)
    rev = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rev)
    assert (rev.revision, rev.down_revision) == (_REVISION, _PARENT)
    assert "wake_state" in inspect.getsource(rev.upgrade)


def test_the_fresh_ddl_carries_the_column():
    from db.schema import TABLES
    from db.tables import operator_queue
    assert "wake_state" in TABLES["operator_queue"]
    assert "wake_state" in operator_queue.c


# ---------------------------------------------------------------------------
# Which failures keep the wake owed (merge-train ruling on AC #4)
# ---------------------------------------------------------------------------

# No turn started on the agent: the platform never reached it (network), never
# admitted the turn (capacity, circuit_open, ephemeral_exhausted) or the agent
# could not start one (auth, billing). The wake is still owed.
NO_TURN_CLASSES = ["network", "capacity", "circuit_open", "billing", "auth", "ephemeral_exhausted"]
# A turn ran and ended badly — its own execution row says how. Not owed again.
TURN_RAN_CLASSES = ["timeout", "agent_error", "max_turns", "skill_not_found",
                    "model_unsupported", "turn_refused"]


class TestWhichFailuresKeepTheWakeOwed:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("code", NO_TURN_CLASSES)
    async def test_a_failure_before_any_turn_keeps_pending_and_releases_the_claim(
            self, ors, monkeypatch, code):
        w = _install(monkeypatch, result_status="failed", result_error_code=code)
        assert await ors.maybe_dispatch_resume(ITEM, response="Build it") is None
        assert w.wake_writes == [("item-1", "pending")]
        assert w.idem == ["fail"], "a completed key would make the next start's re-dispatch replay into nothing"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("code", TURN_RAN_CLASSES)
    async def test_a_turn_that_ran_and_failed_reads_woken_and_completes_the_claim(
            self, ors, monkeypatch, code):
        w = _install(monkeypatch, result_status="failed", result_error_code=code)
        assert await ors.maybe_dispatch_resume(ITEM, response="Build it") == "exec-1"
        assert w.wake_writes == [("item-1", "woken")]
        assert w.idem == ["complete"]

    @pytest.mark.asyncio
    async def test_the_enum_member_is_read_like_its_value(self, ors, monkeypatch):
        from services.execution_envelope import TaskExecutionErrorCode
        w = _install(monkeypatch, result_status="failed",
                     result_error_code=TaskExecutionErrorCode.CAPACITY)
        assert await ors.maybe_dispatch_resume(ITEM, response="Build it") is None
        assert w.wake_writes == [("item-1", "pending")]

    def test_the_no_turn_set_is_spelled_from_the_enum(self, ors):
        from services.execution_envelope import TaskExecutionErrorCode
        values = {c.value for c in TaskExecutionErrorCode}
        assert set(ors.NO_TURN_ERROR_CODES) == set(NO_TURN_CLASSES)
        assert set(ors.NO_TURN_ERROR_CODES) <= values
        assert "timeout" not in ors.NO_TURN_ERROR_CODES, "a timeout means a turn ran"


# ---------------------------------------------------------------------------
# At capacity the wake queues on the durable backlog instead of failing
# ---------------------------------------------------------------------------

class TestTheWakeQueuesAtCapacity:
    """A wake rejected at capacity used to be a FAILED row and a completed
    idempotency key — never retried. This producer now asks admission for the
    durable backlog (`queue_persistent`), pilot or not, so the turn is drained
    by the existing backlog machinery and `dispatch_and_await_terminal` waits
    out the queue for the real terminal."""

    @pytest.mark.asyncio
    async def test_the_answer_wake_asks_for_the_durable_backlog(self, ors, monkeypatch):
        w = _install(monkeypatch)
        await ors.maybe_dispatch_resume(ITEM, response="Build it")
        assert w.calls[0]["queue_on_capacity"] is True

    @pytest.mark.asyncio
    async def test_the_ending_wake_asks_for_the_durable_backlog(self, ors, monkeypatch):
        w = _install(monkeypatch)
        rows = [{"id": "e1", "request_id": "req-e1", "agent_name": "parked-agent", "title": "t"}]
        await ors.maybe_dispatch_ending(rows, disposition="cancelled", disposed_by_email=None)
        assert w.calls[0]["queue_on_capacity"] is True

    def test_execute_task_accepts_the_producer_opt_in(self):
        from services.task_execution_service import TaskExecutionService
        params = inspect.signature(TaskExecutionService.execute_task).parameters
        assert params["queue_on_capacity"].default is False

    def test_the_opt_in_builds_the_payload_for_an_agent_outside_the_pilot(self, monkeypatch):
        import services.task_execution_service as tes
        monkeypatch.setattr(tes, "pull_owns_dispatch", lambda agent, trigger: False)
        kwargs = dict(
            agent_name="parked-agent", triggered_by=tes.__dict__.get("TRIGGERED_BY", "operator_response"),
            execution_id="exec-1", message="m", model=None, allowed_tools=None, system_prompt=None,
            timeout_seconds=300, resume_session_id=None, subscription_id=None, source_user_id=None,
            source_user_email=None, source_agent_name=None, slot_already_held=False,
        )
        assert tes.build_pull_queue_payload(**kwargs) is None, "the #2391 default is unchanged"
        payload = tes.build_pull_queue_payload(**kwargs, queue_on_capacity=True)
        assert payload is not None and payload.triggered_by == "operator_response"
        assert tes.build_pull_queue_payload(**{**kwargs, "execution_id": None}, queue_on_capacity=True) is None
