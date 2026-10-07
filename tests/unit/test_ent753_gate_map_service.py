"""
The per-agent skill gate map — the write service (trinity-enterprise#753).

Target: ``services/skill_gate_map_service.py`` over the REAL per-test database
(``db_backend``). Stubbed: the container state and the marker exec (Docker), the
skills library listing, and the audit sink.

What these pin:

* named refusals — bad name, bad approver, ``approver`` on an install that
  cannot resolve it, a deadline that is not an int in 1..168 (``true`` and
  ``"24"`` included), a ghost (decision 2);
* an update keeps what it was not told to change; an explicit null deadline
  resets to the default;
* clearing a gate on a library-assigned name leaves a ``cleared`` tombstone, so
  the reconcile never re-applies a default the owner just cleared;
* ``reconcile_library_gates`` is state-driven: it applies ``approval:
  recommended`` defaults to every assigned skill (new and existing holders —
  the backfill ruling), removes defaults and tombstones whose skill is gone,
  never loosens on a metadata change, skips ghosts, retries an unreadable
  library at the next call, and writes nothing — no row, no audit — when
  nothing changed;
* a person's unassign drops explicit gates only for names still unassigned,
  and never one whose library row was in ``conflict`` (the agent's own skill
  of that name is what runs — decision 1);
* the marker ordering contract (#752 note 2): written BEFORE the first gate's
  insert on a running agent, removed only AFTER the last gate is gone, never
  touched on a stopped agent; a failed write still saves, with a warning; and a
  concurrent sync cannot remove the marker inside the critical section;
* honest status: ``approver_unassigned`` when the approver kind reaches nobody
  (the default admin has no email).

Related flow: docs/memory/feature-flows/skill-gate.md
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from db_harness import db_backend  # noqa: E402,F401

pytestmark = pytest.mark.unit

OWNER, OWNER_EMAIL = "owner-753-svc", "owner-753-svc@example.com"
NOMAIL = "nomail-753-svc"
FIN = "fin-753-svc"
BARE = "bare-753-svc"           # owner without an email
GHOST = "ghost-753-svc"


def _run(coro):
    return asyncio.run(asyncio.wait_for(coro, timeout=10))


@pytest.fixture
def world(db_backend, monkeypatch):
    import importlib
    from database import db
    from db_models import UserCreate
    from services import docker_utils, assignment_provider
    from services.skill_service import skill_service
    from services import skill_gate_map_service as gms
    # Through sys.modules, which the code under test imports from at call time
    # — never `import services.x as X`, which reads the package attribute and
    # can be a stale copy after another test's re-import (learning 2026-10-05).
    DS = importlib.import_module("services.docker_service")
    PAS = importlib.import_module("services.platform_audit_service")

    db.create_user(UserCreate(username=OWNER, role="user", email=OWNER_EMAIL))
    db.create_user(UserCreate(username=NOMAIL, role="user"))
    db.register_agent_owner(FIN, OWNER)
    db.register_agent_owner(BARE, NOMAIL)
    db.register_agent_owner(GHOST, OWNER, is_ephemeral=True)

    state = SimpleNamespace(
        containers={FIN: "running", BARE: "stopped", GHOST: "running"},
        marker={},              # agent -> present?
        events=[],              # ordered: ("marker", agent, create) / ("db", op, agent)
        exec_result={"exit_code": 0, "output": "", "timed_out": False},
        library=[{"name": "deploy", "approval": "recommended"},
                 {"name": "notes", "approval": None}],
        library_fail=False,
        library_calls=0,
        state_calls=0,
        audits=[],
        hold=None,              # (entered, release, on_create) for the race tests
    )

    async def _state(name):
        state.state_calls += 1
        return state.containers.get(name, "missing")

    async def _exec(container_name, command, timeout=60, *, environment=None, user="developer"):
        from services import skill_gate_service as sgs
        agent = container_name[len("agent-"):]
        create = list(command) == sgs.marker_command(True)
        state.events.append(("marker", agent, create))
        if state.exec_result.get("exit_code") == 0:
            state.marker[agent] = create
        if state.hold is not None and state.hold[2] == create:
            entered, release, _ = state.hold
            state.hold = None
            entered.set()
            await release.wait()
        return dict(state.exec_result)

    def _library():
        state.library_calls += 1
        if state.library_fail:
            raise RuntimeError("library clone unreadable")
        return [dict(s) for s in state.library]

    async def _audit(event_type, event_action, source, **kw):
        state.audits.append(SimpleNamespace(type=event_type, action=event_action, source=source, **kw))
        return "evt"

    monkeypatch.setattr(docker_utils, "agent_container_state_async", _state)
    monkeypatch.setattr(DS, "execute_command_in_container", _exec)
    monkeypatch.setattr(skill_service, "list_skills", _library)
    monkeypatch.setattr(PAS.platform_audit_service, "log", _audit)
    monkeypatch.setattr(assignment_provider, "get_provider", lambda: None)

    for op in ("write_skill_gate", "clear_skill_gate",
               "reconcile_library_skill_gates", "drop_unassigned_skill_gates"):
        real = getattr(db, op)

        def _wrap(*a, __real=real, __op=op, **kw):
            state.events.append(("db", __op, a[0]))
            return __real(*a, **kw)
        monkeypatch.setattr(db, op, _wrap)

    person = SimpleNamespace(username=OWNER, email=OWNER_EMAIL, role="user", mcp_scope=None,
                             agent_name=None, connector_agent=None)
    state.ctx = gms.GateContext(actor=person, via="ui", trigger="direct")
    state.db, state.gms = db, gms
    return state


def _gates(world, agent=FIN):
    from services import skill_gate_service as sgs
    return sgs.list_skill_gates(agent)


# ---- named refusals -------------------------------------------------------------

@pytest.mark.parametrize("skill,changes,code", [
    ("../etc", {}, "invalid_skill_name"),
    ("-lead", {}, "invalid_skill_name"),
    ("ok", {"approver": "boss"}, "invalid_approver"),
    ("ok", {"approver": 1}, "invalid_approver"),
    ("ok", {"approver": "approver"}, "approver_unavailable"),
    ("ok", {"deadline_hours": True}, "invalid_deadline"),
    ("ok", {"deadline_hours": "24"}, "invalid_deadline"),
    ("ok", {"deadline_hours": 0}, "invalid_deadline"),
    ("ok", {"deadline_hours": 169}, "invalid_deadline"),
    ("ok", {"deadline_hours": 2.5}, "invalid_deadline"),
])
def test_bad_input_is_refused_by_name_and_writes_nothing(world, skill, changes, code):
    with pytest.raises(world.gms.SkillGateMapRefused) as e:
        _run(world.gms.set_gate(FIN, skill, changes=changes, ctx=world.ctx))
    assert (e.value.status_code, e.value.code) == (422, code)
    assert e.value.message
    assert world.db.list_agent_skill_gates(FIN) == []
    assert [ev for ev in world.events if ev[0] == "marker"] == []


@pytest.mark.parametrize("hours", [1, 168, None])
def test_the_deadline_bounds_are_inclusive(world, hours):
    out = _run(world.gms.set_gate(FIN, "pay-invoice", changes={"deadline_hours": hours}, ctx=world.ctx))
    assert out["gate"]["deadline_hours"] == hours


def test_the_approver_kind_is_offered_only_where_it_resolves(world, monkeypatch):
    from services import assignment_provider
    assert world.gms.approver_kinds() == ["primary"]
    monkeypatch.setattr(assignment_provider, "get_provider", lambda: object())
    assert world.gms.approver_kinds() == ["primary", "approver"]
    out = _run(world.gms.set_gate(FIN, "pay-invoice", changes={"approver": "approver"}, ctx=world.ctx))
    assert out["gate"]["approver"] == "approver"


def test_a_ghost_cannot_hold_a_gate_but_one_can_be_cleared(world):
    with pytest.raises(world.gms.SkillGateMapRefused) as e:
        _run(world.gms.set_gate(GHOST, "pay-invoice", changes={}, ctx=world.ctx))
    assert (e.value.status_code, e.value.code) == (409, "ephemeral_agent")
    assert _run(world.gms.clear_gate(GHOST, "pay-invoice", ctx=world.ctx))["changed"] is False


# ---- set / clear semantics ------------------------------------------------------

def test_a_new_gate_defaults_to_primary_and_the_24h_deadline(world):
    out = _run(world.gms.set_gate(FIN, "Pay-Invoice", changes={}, ctx=world.ctx))
    assert out["skill_name"] == "pay-invoice" and out["changed"] is True
    assert out["gate"]["approver"] == "primary" and out["gate"]["deadline_hours"] is None
    assert out["gate"]["origin"] == "set" and out["gate"]["set_by"] == OWNER
    assert out["warnings"] == []
    assert _gates(world) == {"pay-invoice": world.gms.SkillGate("primary", None)}


def test_an_update_keeps_what_it_was_not_told_and_null_resets_the_deadline(world, monkeypatch):
    from services import assignment_provider
    monkeypatch.setattr(assignment_provider, "get_provider", lambda: object())
    _run(world.gms.set_gate(FIN, "pay-invoice", changes={"approver": "approver", "deadline_hours": 8},
                            ctx=world.ctx))
    out = _run(world.gms.set_gate(FIN, "pay-invoice", changes={"deadline_hours": 5}, ctx=world.ctx))
    assert (out["gate"]["approver"], out["gate"]["deadline_hours"]) == ("approver", 5)
    out = _run(world.gms.set_gate(FIN, "pay-invoice", changes={"deadline_hours": None}, ctx=world.ctx))
    assert (out["gate"]["approver"], out["gate"]["deadline_hours"]) == ("approver", None)
    again = _run(world.gms.set_gate(FIN, "pay-invoice", changes={}, ctx=world.ctx))
    assert again["changed"] is False
    assert [a.action for a in world.audits].count("skill_gate_set") == 3


def test_an_approver_nobody_fills_is_saved_with_a_warning(world):
    """The default admin has no email: `primary` reaches nobody, so every
    gated request would be refused `role_unassigned`. Said, not hidden."""
    out = _run(world.gms.set_gate(BARE, "pay-invoice", changes={}, ctx=world.ctx))
    assert out["warnings"] == ["approver_unassigned"]
    view = world.gms.list_gates(BARE)
    assert [g["approver_reachable"] for g in view["gates"]] == [False]
    assert [g["approver_reachable"] for g in world.gms.list_gates(FIN)["gates"]] == []


def test_clearing_is_idempotent_and_deletes_an_unassigned_name(world):
    _run(world.gms.set_gate(FIN, "pay-invoice", changes={}, ctx=world.ctx))
    out = _run(world.gms.clear_gate(FIN, "PAY-INVOICE", ctx=world.ctx))
    assert (out["changed"], out["cleared"]) == (True, "deleted")
    assert world.db.list_agent_skill_gates(FIN) == []
    assert _run(world.gms.clear_gate(FIN, "pay-invoice", ctx=world.ctx))["changed"] is False
    assert [a.action for a in world.audits] == ["skill_gate_set", "skill_gate_cleared"]


def test_clearing_a_library_default_leaves_a_tombstone_the_reconcile_honours(world):
    world.db.assign_skill(FIN, "deploy", OWNER)
    applied = _run(world.gms.reconcile_library_gates(FIN, ctx=world.ctx))
    assert applied["applied"] == ["deploy"]
    out = _run(world.gms.clear_gate(FIN, "deploy", ctx=world.ctx))
    assert (out["changed"], out["cleared"]) == (True, "tombstoned")
    assert _gates(world) == {}
    assert world.gms.list_gates(FIN)["cleared_defaults"] == ["deploy"]
    again = _run(world.gms.reconcile_library_gates(FIN, ctx=world.ctx))
    assert again is None or again["applied"] == []
    assert _gates(world) == {}
    # an explicit set over the tombstone gates it again
    _run(world.gms.set_gate(FIN, "deploy", changes={}, ctx=world.ctx))
    assert set(_gates(world)) == {"deploy"}


# ---- the reconcile ----------------------------------------------------------------

def test_reconcile_backfills_existing_holders_once_and_then_writes_nothing(world):
    world.db.assign_skill(FIN, "Deploy", OWNER)
    world.db.assign_skill(FIN, "notes", OWNER)
    first = _run(world.gms.reconcile_library_gates(FIN, ctx=world.ctx))
    assert first["applied"] == ["deploy"]
    row = world.db.list_agent_skill_gates(FIN)[0]
    assert (row["origin"], row["approver"], row["deadline_hours"]) == ("library_default", "primary", None)
    audits = len(world.audits)
    second = _run(world.gms.reconcile_library_gates(FIN, ctx=world.ctx))
    assert second is None or (second["applied"] == [] and second["removed"] == [])
    assert len(world.audits) == audits


def test_a_default_another_worker_already_applied_writes_no_second_audit(world, monkeypatch):
    """Two workers reconcile the same agent (the sweep's leader lease fails
    open): the pre-read of the second still sees no row, but the insert finds
    one. The audit follows what the DB wrote, never what the pre-read guessed."""
    world.db.assign_skill(FIN, "deploy", OWNER)
    _run(world.gms.reconcile_library_gates(FIN, ctx=world.ctx))
    audits = len(world.audits)
    real = world.db.list_agent_skill_gates
    monkeypatch.setattr(world.db, "list_agent_skill_gates",
                        lambda agent: [] if agent == FIN else real(agent))
    out = _run(world.gms.reconcile_library_gates(FIN, ctx=world.ctx))
    assert out is None or out["applied"] == []
    assert len(world.audits) == audits


def test_an_explicit_gate_is_never_replaced_by_the_default(world, monkeypatch):
    from services import assignment_provider
    monkeypatch.setattr(assignment_provider, "get_provider", lambda: object())
    _run(world.gms.set_gate(FIN, "deploy", changes={"approver": "approver", "deadline_hours": 3},
                            ctx=world.ctx))
    world.db.assign_skill(FIN, "deploy", OWNER)
    _run(world.gms.reconcile_library_gates(FIN, ctx=world.ctx))
    row = world.db.list_agent_skill_gates(FIN)[0]
    assert (row["origin"], row["approver"], row["deadline_hours"]) == ("set", "approver", 3)


def test_the_tombstone_goes_with_the_assignment_and_readding_restores_the_default(world):
    world.db.assign_skill(FIN, "deploy", OWNER)
    _run(world.gms.reconcile_library_gates(FIN, ctx=world.ctx))
    _run(world.gms.clear_gate(FIN, "deploy", ctx=world.ctx))                    # tombstone
    world.db.unassign_skill(FIN, "deploy")
    out = _run(world.gms.reconcile_library_gates(FIN, ctx=world.ctx))           # no package report needed
    assert out["removed"] == ["deploy"]
    assert world.db.list_agent_skill_gates(FIN) == []
    world.db.assign_skill(FIN, "deploy", OWNER)
    assert _run(world.gms.reconcile_library_gates(FIN, ctx=world.ctx))["applied"] == ["deploy"]


@pytest.mark.parametrize("drop,kept", [
    pytest.param(None, True, id="removal-deferred"),
    pytest.param(set(), True, id="nothing-reported-gone"),
    pytest.param({"Deploy"}, False, id="the-route-saw-it-go"),
    pytest.param("all", False, id="after-the-start-prune"),
    pytest.param("all-but-deploy", True, id="the-prune-could-not-remove-it"),
])
def test_a_default_stays_until_the_package_is_gone(world, drop, kept):
    """An unassigned skill whose package removal deferred is still on the agent;
    its default must not go before its files do (plan-gate ruling 2026-10-07)."""
    world.db.assign_skill(FIN, "deploy", OWNER)
    _run(world.gms.reconcile_library_gates(FIN, ctx=world.ctx))
    world.db.unassign_skill(FIN, "deploy")
    drop = {"all": world.gms.AllExcept(), "all-but-deploy": world.gms.AllExcept(frozenset({"deploy"}))}.get(
        drop, drop) if isinstance(drop, str) else drop
    _run(world.gms.reconcile_library_gates(FIN, ctx=world.ctx, add=False, drop_defaults=drop))
    assert (set(_gates(world)) == {"deploy"}) is kept


def test_the_write_itself_keeps_a_default_whose_package_may_remain(world):
    """The same rule at the DB write, reached on a call that has other work (a
    default to add): an unassigned skill's default is not swept along."""
    world.db.assign_skill(FIN, "deploy", OWNER)
    _run(world.gms.reconcile_library_gates(FIN, ctx=world.ctx))
    world.db.unassign_skill(FIN, "deploy")                          # removal deferred
    world.library = [{"name": "deploy", "approval": "recommended"},
                     {"name": "notes", "approval": "recommended"}]
    world.db.assign_skill(FIN, "notes", OWNER)
    out = _run(world.gms.reconcile_library_gates(FIN, ctx=world.ctx))
    assert out["applied"] == ["notes"] and out["removed"] == []
    assert set(_gates(world)) == {"deploy", "notes"}


def test_what_counts_as_a_package_gone():
    from services import skill_gate_map_service as gms
    removal = {"status": "partial", "results": {
        "A": {"status": "removed"}, "b": {"status": "not_present"}, "c": {"status": "deferred"},
        "d": {"status": "partial"}, "e": {"status": "not_managed"}, "f": {"status": "still_assigned"}}}
    assert gms.packages_gone(removal) == {"a", "b"}
    assert gms.packages_gone({"status": "deferred", "reason": "injection_in_progress"}) == set()
    assert gms.packages_gone(None) == set()
    assert gms.drop_after_prune({"status": "clean", "removed": 0}) == gms.AllExcept(frozenset())
    assert gms.drop_after_prune({"status": "reconciled", "results": removal["results"]}) == \
        gms.AllExcept(frozenset({"c", "d", "e", "f"}))
    for skipped in ({"status": "skipped"}, {"status": "refused"}, None):
        assert gms.drop_after_prune(skipped) is None


def test_clearing_an_explicit_gate_on_a_skill_the_library_does_not_recommend_deletes_it(world):
    """Only a recommended skill needs the tombstone; any other clear deletes, so
    a recommendation that arrives later can still apply."""
    world.db.assign_skill(FIN, "notes", OWNER)
    _run(world.gms.set_gate(FIN, "notes", changes={}, ctx=world.ctx))
    out = _run(world.gms.clear_gate(FIN, "notes", ctx=world.ctx))
    assert out["cleared"] == "deleted"
    world.library = [{"name": "notes", "approval": "recommended"}]
    assert _run(world.gms.reconcile_library_gates(FIN, ctx=world.ctx))["applied"] == ["notes"]


def test_two_partial_puts_at_once_keep_both_fields(world, monkeypatch):
    from services import assignment_provider
    monkeypatch.setattr(assignment_provider, "get_provider", lambda: object())
    _run(world.gms.set_gate(FIN, "pay-invoice", changes={}, ctx=world.ctx))

    async def both():
        await asyncio.gather(
            world.gms.set_gate(FIN, "pay-invoice", changes={"approver": "approver"}, ctx=world.ctx),
            world.gms.set_gate(FIN, "pay-invoice", changes={"deadline_hours": 6}, ctx=world.ctx))
    _run(both())
    row = world.db.list_agent_skill_gates(FIN)[0]
    assert (row["approver"], row["deadline_hours"]) == ("approver", 6)


def test_the_library_can_tighten_but_never_loosen(world):
    world.db.assign_skill(FIN, "deploy", OWNER)
    _run(world.gms.reconcile_library_gates(FIN, ctx=world.ctx))
    world.library = [{"name": "deploy", "approval": None}]
    _run(world.gms.reconcile_library_gates(FIN, ctx=world.ctx))
    assert set(_gates(world)) == {"deploy"}


def test_an_unreadable_library_inserts_nothing_and_the_next_call_retries(world):
    world.db.assign_skill(FIN, "deploy", OWNER)
    world.library_fail = True
    out = _run(world.gms.reconcile_library_gates(FIN, ctx=world.ctx))
    assert out["library_unreadable"] is True and out["applied"] == []
    assert _gates(world) == {}
    world.library_fail = False
    assert _run(world.gms.reconcile_library_gates(FIN, ctx=world.ctx))["applied"] == ["deploy"]


def test_a_ghost_gets_no_default(world):
    world.db.assign_skill(GHOST, "deploy", OWNER)
    out = _run(world.gms.reconcile_library_gates(GHOST, ctx=world.ctx))
    assert out is None or out["applied"] == []
    assert world.db.list_agent_skill_gates(GHOST) == []


def test_reconcile_costs_nothing_on_an_agent_with_no_skills_and_no_defaults(world):
    _run(world.gms.set_gate(FIN, "own-skill", changes={}, ctx=world.ctx))   # explicit: not its business
    world.events.clear()
    world.library_calls = world.state_calls = 0
    assert _run(world.gms.reconcile_library_gates(FIN, ctx=world.ctx)) is None
    assert (world.library_calls, world.state_calls, world.events) == (0, 0, [])
    assert set(_gates(world)) == {"own-skill"}        # decision 1: sticky


def test_a_default_reaching_nobody_is_reported(world):
    world.db.assign_skill(BARE, "deploy", NOMAIL)
    out = _run(world.gms.reconcile_library_gates(BARE, ctx=world.ctx))
    assert out["applied"] == ["deploy"] and out["unreachable"] == ["deploy"]


# ---- the person's unassign ---------------------------------------------------------

def test_a_person_unassign_drops_explicit_gates_only_for_names_still_unassigned(world):
    for name in ("pay-invoice", "refund", "deploy"):
        _run(world.gms.set_gate(FIN, name, changes={}, ctx=world.ctx))
    world.db.assign_skill(FIN, "refund", OWNER)            # re-added concurrently: stays gated
    removed = _run(world.gms.drop_explicit_gates_on_unassign(
        FIN, ["Pay-Invoice", "refund", "deploy"], ctx=world.ctx, keep=["deploy"]))
    assert removed == ["pay-invoice"]
    assert set(_gates(world)) == {"refund", "deploy"}
    assert world.gms.explicit_gate_names(FIN, ["REFUND", "nope"]) == ["refund"]


# ---- the marker ordering contract ---------------------------------------------------

def _trace(world):
    return [(e[0], e[2] if e[0] == "marker" else e[1]) for e in world.events]


def test_the_marker_is_written_before_the_first_gate_and_removed_after_the_last(world):
    _run(world.gms.set_gate(FIN, "a", changes={}, ctx=world.ctx))
    assert _trace(world) == [("marker", True), ("db", "write_skill_gate"), ("marker", True)]
    world.events.clear()
    _run(world.gms.set_gate(FIN, "b", changes={}, ctx=world.ctx))     # not the first: no pre-write
    assert _trace(world) == [("db", "write_skill_gate"), ("marker", True)]
    world.events.clear()
    _run(world.gms.clear_gate(FIN, "a", ctx=world.ctx))
    assert _trace(world) == [("db", "clear_skill_gate"), ("marker", True)]
    world.events.clear()
    _run(world.gms.clear_gate(FIN, "b", ctx=world.ctx))
    assert _trace(world) == [("db", "clear_skill_gate"), ("marker", False)]
    assert world.marker[FIN] is False


def test_a_stopped_agent_is_never_execd(world):
    _run(world.gms.set_gate(BARE, "a", changes={}, ctx=world.ctx))
    _run(world.gms.clear_gate(BARE, "a", ctx=world.ctx))
    assert [e for e in world.events if e[0] == "marker"] == []


def test_a_failed_marker_write_still_saves_and_says_so(world):
    world.exec_result = {"exit_code": 1, "output": "denied", "timed_out": False}
    out = _run(world.gms.set_gate(FIN, "a", changes={}, ctx=world.ctx))
    assert out["changed"] is True and "marker_not_written" in out["warnings"]
    assert set(_gates(world)) == {"a"}


def test_a_concurrent_sync_cannot_remove_the_marker_inside_the_critical_section(world):
    """Hold the first writer between its marker write and its insert; a sync
    started meanwhile (the start path, a heal) reads an empty map. Without the
    shared per-agent lock it removes the marker and the insert lands with no
    marker — the window the ordering contract exists to close."""
    from services import skill_gate_service as sgs
    at_insert = []
    real = world.db.write_skill_gate

    def _record(*a, **kw):
        at_insert.append(world.marker.get(FIN))
        return real(*a, **kw)

    world.db.write_skill_gate = _record

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        world.hold = (entered, release, True)
        writer = asyncio.create_task(world.gms.set_gate(FIN, "a", changes={}, ctx=world.ctx))
        await entered.wait()
        syncer = asyncio.create_task(sgs.sync_gate_marker(FIN))
        for _ in range(20):
            await asyncio.sleep(0)
        release.set()
        await writer
        await syncer

    try:
        _run(scenario())
    finally:
        world.db.write_skill_gate = real
    assert at_insert == [True]
    assert world.marker[FIN] is True


def test_a_set_racing_the_clear_of_the_last_gate_still_writes_the_marker_first(world):
    """Gates {a}. A clear of `a` holds the lock and removes the marker; a set of
    `b`, started meanwhile, must decide "first gate" under the lock — from a
    read taken before it, it would insert `b` with no marker."""
    _run(world.gms.set_gate(FIN, "a", changes={}, ctx=world.ctx))
    at_insert = []
    real = world.db.write_skill_gate

    def _record(*a, **kw):
        at_insert.append(world.marker.get(FIN))
        return real(*a, **kw)

    world.db.write_skill_gate = _record

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        world.hold = (entered, release, False)          # hold the clear's marker REMOVAL
        clearer = asyncio.create_task(world.gms.clear_gate(FIN, "a", ctx=world.ctx))
        await entered.wait()
        setter = asyncio.create_task(world.gms.set_gate(FIN, "b", changes={}, ctx=world.ctx))
        for _ in range(20):
            await asyncio.sleep(0)
        release.set()
        await clearer
        await setter

    try:
        _run(scenario())
    finally:
        world.db.write_skill_gate = real
    assert at_insert == [True]
    assert world.marker[FIN] is True


# ---- audit ------------------------------------------------------------------------

def test_the_audit_says_who_and_from_where(world):
    _run(world.gms.set_gate(FIN, "pay-invoice", changes={"deadline_hours": 6}, ctx=world.ctx))
    [row] = world.audits
    assert (row.action, row.source, row.target_type, row.target_id) == (
        "skill_gate_set", "api", "agent", FIN)
    assert row.details["skill"] == "pay-invoice" and row.details["deadline_hours"] == 6
    assert (row.details["via"], row.details["trigger"]) == ("ui", "direct")
    assert row.details["previous"] is None


def test_an_agent_actor_is_recorded_as_the_agent(world):
    agent = SimpleNamespace(username=OWNER, email=OWNER_EMAIL, role="user", mcp_scope="agent",
                            agent_name="orchestrator-753", connector_agent=None,
                            mcp_key_id="k1", mcp_key_name="orch")
    ctx = world.gms.GateContext(actor=agent, via="orchestrator", trigger="direct")
    _run(world.gms.set_gate(FIN, "pay-invoice", changes={}, ctx=ctx))
    [row] = world.audits
    assert row.actor_agent_name == "orchestrator-753" and row.actor_email == OWNER_EMAIL
    assert world.db.list_agent_skill_gates(FIN)[0]["set_by_agent"] == "orchestrator-753"


def test_a_failed_audit_never_fails_the_write(world, monkeypatch):
    import importlib

    async def _boom(*a, **kw):
        raise RuntimeError("audit sink down")

    monkeypatch.setattr(importlib.import_module("services.platform_audit_service").platform_audit_service,
                        "log", _boom)
    out = _run(world.gms.set_gate(FIN, "pay-invoice", changes={}, ctx=world.ctx))
    assert out["changed"] is True


def test_via_comes_from_the_principal(world):
    v = world.gms.via_for
    assert v(SimpleNamespace(mcp_scope=None, agent_name=None)) == "ui"
    assert v(SimpleNamespace(mcp_scope="user", agent_name=None)) == "api"
    assert v(SimpleNamespace(mcp_scope="agent", agent_name="x")) == "orchestrator"
    assert v(SimpleNamespace(mcp_scope="system", agent_name=None)) == "system"
    assert v(None) == "system"
    assert v(SimpleNamespace()) == "system"          # no scope at all: never the JWT value
