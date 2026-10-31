"""trinity-enterprise#1030 CP4: a manifest member's `seat:` and `person_seats:`.

`seat: <role_id>` is the canon role the AGENT holds; `person_seats: [{role,
kind}]` names seats a PERSON must fill — the role and the kind, never the
person. The assignment record belongs to a registered module, so this repo
reads and writes it only through two optional provider hooks read through
`getattr` (`set_seat`, `person_seat_status`, beside the existing `seat_for`).
Without them the class is UNOBSERVED — reported `seat_unobserved`, never "in
sync" and never a blocker — and nothing writes a seat. A person seat is
report-only. A provider error is a READ failure (the retryable
`member_reads_unavailable` blocker), not unobserved. No export or report
carries an email.

Runs over the REAL `db` on a full schema; Docker is faked at its read seams
(`_ent695_world.py`). The provider is a fake registered on the real seam.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend  # noqa: E402,F401

pytest.importorskip("docker", reason="backend venv required (system_service chain)")

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _ent695_world as W  # noqa: E402
from services import assignment_provider, system_service  # noqa: E402
from services.system_apply import seats as apply_seats  # noqa: E402

pytestmark = pytest.mark.unit

SYSTEM = W.SYSTEM
LEAD = f"{SYSTEM}-lead"
HOLDER = "seat-holder@example.org"


class _Reader:
    """A provider that can say which seat an agent holds, and no more."""

    def __init__(self, held=None):
        self.held = dict(held or {})
        self.calls = []

    def assignment_for(self, agent_name, triggered_by):
        return None

    def seat_for(self, agent_name):
        role = self.held.get(agent_name)
        return {"case": "holds" if role else "none", "role_id": role,
                "seats": [role] if role else []}


class _Full(_Reader):
    """Both CP4 hooks."""

    def __init__(self, held=None, people=None, refuse=None):
        super().__init__(held)
        self.people = dict(people or {})
        self.refuse = refuse

    def set_seat(self, agent_name, role_id, created_by):
        self.calls.append(("set_seat", agent_name, role_id, created_by))
        if self.refuse:
            return {"refused": self.refuse}
        changed = self.held.get(agent_name) != role_id
        self.held[agent_name] = role_id
        return {"changed": changed}

    def person_seat_status(self, agent_name, role_id, kind):
        self.calls.append(("person_seat_status", agent_name, role_id, kind))
        filled = self.people.get((role_id, kind))
        # A provider that over-answers: the emails must never reach a report.
        return None if filled is None else {"filled": filled, "emails": [HOLDER]}


@pytest.fixture
def world(db_backend, monkeypatch, tmp_path):
    w = W.make_world(monkeypatch, tmp_path)
    W.install_executor_seams(w, monkeypatch)
    assignment_provider.clear_provider()
    yield w
    assignment_provider.clear_provider()


def _manifest(seat=None, person_seats=None, *, short="lead") -> str:
    lines = [f"name: {SYSTEM}", "agents:", f"  {short}:", "    template: local:plain"]
    if seat is not None:
        lines.append(f"    seat: {seat}")
    if person_seats is not None:
        lines.append("    person_seats:")
        for entry in person_seats:
            lines.append("      - {" + ", ".join(f"{k}: {v}" for k, v in entry.items()) + "}")
    return "\n".join(lines) + "\n"


def _apply(world, text, *, dry_run, user=None):
    return asyncio.run(system_service.apply_manifest(
        text, user or world.user(W.ALICE), None, dry_run=dry_run,
        create_agent_fn=world.create, start_agent_fn=world.docker.start))


def _deploy(world, text, *, dry_run=False, user=None):
    return asyncio.run(system_service.deploy_manifest(
        text, user or world.user(W.ADMIN), None, dry_run=dry_run, create_agent_fn=world.create))


def _agent_key(world):
    """An agent's injected key, resolved to its admin owner carrying the role."""
    return world.user(W.ADMIN).model_copy(update={"mcp_scope": "agent", "agent_name": "orchestrator"})


def _user_key(world):
    """An admin's own MCP key: passes `assert_admin`, fails the interactive gate."""
    return world.user(W.ADMIN).model_copy(update={"mcp_scope": "user"})


def _member(res, name=LEAD):
    return next(m for m in res.members if m.name == name)


def _codes(member):
    return [(c.field, c.action, c.code) for c in member.changes]


# ===========================================================================
# Parse and validate
# ===========================================================================

class TestParse:
    def test_seat_and_person_seats_are_read_and_are_not_unknown_keys(self):
        m = system_service.parse_manifest(_manifest(
            "Fleet-Architect", [{"role": "fleet-owner", "kind": "approver"},
                                {"role": "fleet-owner", "kind": "approver"}]))
        assert m.agents["lead"].seat == "fleet-architect"
        assert m.agents["lead"].person_seats == [{"role": "fleet-owner", "kind": "approver"}]
        assert m.unknown_agent_keys == {}

    def test_an_email_in_seat_is_refused_by_name_and_never_echoed(self):
        with pytest.raises(ValueError) as exc:
            system_service.parse_manifest(_manifest(HOLDER))
        assert "seat names a canon role, never a person" in str(exc.value)
        assert HOLDER not in str(exc.value) and "@" not in str(exc.value)

    @pytest.mark.parametrize("entry,where", [
        ({"role": "fleet-owner", "kind": "approver", "person": HOLDER}, "'person'"),
        ({"role": HOLDER, "kind": "approver"}, "'role'"),
        ({"role": "fleet-owner", "kind": HOLDER}, "'kind'"),
    ])
    def test_an_email_in_person_seats_is_refused_by_name_and_never_echoed(self, entry, where):
        with pytest.raises(ValueError) as exc:
            system_service.parse_manifest(_manifest(person_seats=[entry]))
        msg = str(exc.value)
        assert f"person_seats[0]: {where} looks like an email address" in msg
        assert "never a person" in msg
        assert HOLDER not in msg and "@" not in msg

    def test_a_person_key_that_is_not_an_email_is_still_refused(self):
        with pytest.raises(ValueError, match=r"person_seats\[0\]: unknown key\(s\): person"):
            system_service.parse_manifest(_manifest(
                person_seats=[{"role": "fleet-owner", "kind": "approver", "person": "alice"}]))

    @pytest.mark.parametrize("text,match", [
        (_manifest("not a role!"), "seat must be a canon role id"),
        (_manifest(person_seats=[{"role": "fleet-owner", "kind": "boss"}]), "kind must be one of"),
        (_manifest(person_seats=[{"role": "-x", "kind": "approver"}]), "role must be a canon role id"),
        (f"name: {SYSTEM}\nagents:\n  lead:\n    template: local:plain\n    person_seats: x\n",
         "person_seats must be a list"),
    ])
    def test_a_bad_shape_is_a_named_refusal(self, text, match):
        with pytest.raises(ValueError, match=match):
            system_service.parse_manifest(text)

    def test_the_kinds_are_the_seams_reader_kinds(self):
        from services.seat_decision_service import READER_KINDS
        assert set(apply_seats.PERSON_SEAT_KINDS) == set(READER_KINDS)

    def test_the_recognised_key_text_stays_the_create_mode_text(self):
        text = _manifest("lead-role").replace("seat:", "credentials: {}\n    seat:")
        warnings = system_service.validate_manifest(system_service.parse_manifest(text))
        assert warnings == ["Agent 'lead': ignored unknown key(s): credentials. Recognised "
                            "per-agent keys are: folders, kind, resources, schedules, tags, "
                            "template."]

    def test_classification(self):
        c = system_service.APPLY_FIELD_CLASSIFICATION
        assert c["SystemAgentConfig.seat"][0] == "reconciled"
        assert c["SystemAgentConfig.person_seats"][0] == "report-only"


# ===========================================================================
# Unobserved: no hook, no write, never in sync, never a blocker
# ===========================================================================

PEOPLE = [{"role": "fleet-owner", "kind": "approver"}]


class TestUnobserved:
    def test_no_provider_reads_unobserved_and_writes_nothing(self, world):
        world.seed_agent(LEAD)
        res = _apply(world, _manifest("lead-role", PEOPLE), dry_run=False)
        assert res.status in ("applied", "unchanged") and res.blockers == []
        m = _member(res)
        assert m.verdict == "unchanged"
        assert _codes(m) == [("seat", "report", "seat_unobserved"),
                             ("person_seats", "report", "seat_unobserved")]
        assert res.summary["roles"] == {"write": 0, "report": 0, "unobserved": 2,
                                        "held_back": []}

    def test_a_reader_without_the_writer_is_never_in_sync(self, world):
        """The provider says the agent holds the declared seat, but without
        `set_seat` the class is unobserved — not "in sync"."""
        assignment_provider.register_provider(_Reader({LEAD: "lead-role"}))
        world.seed_agent(LEAD)
        res = _apply(world, _manifest("lead-role"), dry_run=True)
        assert _codes(_member(res)) == [("seat", "report", "seat_unobserved")]
        assert res.summary["roles"]["unobserved"] == 1

    def test_a_provider_without_person_seat_status_leaves_person_seats_unobserved(self, world):
        assignment_provider.register_provider(_Reader())
        world.seed_agent(LEAD)
        res = _apply(world, _manifest(person_seats=PEOPLE), dry_run=True)
        assert _codes(_member(res)) == [("person_seats", "report", "seat_unobserved")]

    def test_nothing_in_the_engine_writes_a_seat_without_the_hook(self):
        """The one writer call is `assignment_provider.write_seat`, which
        raises without `set_seat`; no module reaches a provider any other way."""
        import ast
        for path in (_BACKEND / "services" / "system_apply" / "seats.py",
                     _BACKEND / "services" / "system_service.py"):
            tree = ast.parse(path.read_text())
            attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
            assert "set_seat" not in attrs and "_provider" not in attrs, path.name
        with pytest.raises(assignment_provider.SeatWriterUnavailable):
            assignment_provider.write_seat(LEAD, "lead-role", "alice")


# ===========================================================================
# With both hooks
# ===========================================================================

class TestWrite:
    def test_held_seat_is_in_sync(self, world):
        assignment_provider.register_provider(_Full({LEAD: "lead-role"}))
        world.seed_agent(LEAD)
        res = _apply(world, _manifest("Lead-Role"), dry_run=True)
        assert _codes(_member(res)) == [] and _member(res).verdict == "unchanged"
        assert res.summary["roles"] == {"write": 0, "report": 0, "unobserved": 0,
                                        "held_back": []}

    def test_a_missing_seat_is_written_through_the_hook(self, world):
        p = _Full()
        assignment_provider.register_provider(p)
        world.seed_agent(LEAD)
        admin = world.user(W.ADMIN)
        dry = _apply(world, _manifest("lead-role"), dry_run=True, user=admin)
        assert _codes(_member(dry)) == [("seat", "write", "seat_missing")]
        assert p.calls == []
        res = _apply(world, _manifest("lead-role"), dry_run=False, user=admin)
        assert res.status == "applied", res.failed
        assert p.calls == [("set_seat", LEAD, "lead-role", W.ADMIN)]
        assert p.held[LEAD] == "lead-role"
        assert _codes(_member(_apply(world, _manifest("lead-role"), dry_run=True))) == []

    def test_another_seat_is_reported_as_differs_with_the_current(self, world):
        assignment_provider.register_provider(_Full({LEAD: "other-role"}))
        world.seed_agent(LEAD)
        res = _apply(world, _manifest("lead-role"), dry_run=True, user=world.user(W.ADMIN))
        (c,) = _member(res).changes
        assert (c.code, c.current, c.desired) == (
            "seat_differs", {"case": "holds", "role_id": "other-role"}, "lead-role")
        assert "another declaration may hold this seat" in c.note

    def test_a_refusal_lands_in_failed_with_the_modules_code(self, world):
        assignment_provider.register_provider(_Full(refuse="not_assignment_admin"))
        world.seed_agent(LEAD)
        res = _apply(world, _manifest("lead-role"), dry_run=False, user=world.user(W.ADMIN))
        assert [(f.name, f.step, f.reason.split(":")[0]) for f in res.failed] == [
            (LEAD, "seat", "not_assignment_admin")]

    def test_a_created_member_is_seated_after_it_is_created(self, world):
        p = _Full()
        assignment_provider.register_provider(p)
        admin = world.user(W.ADMIN)
        dry = _apply(world, _manifest("lead-role"), dry_run=True, user=admin)
        assert _member(dry).verdict == "create"
        assert ("seat", "write", "seat_missing") in _codes(_member(dry))
        res = _apply(world, _manifest("lead-role"), dry_run=False, user=admin)
        assert res.status == "applied", res.failed
        assert [c.name for c in world.create_calls] == [LEAD] and p.calls == [
            ("set_seat", LEAD, "lead-role", W.ADMIN)]

    def test_person_seats_on_a_member_apply_will_create_are_unobserved_not_vacant(self, world):
        """The member does not exist yet, so nothing was read: the report says
        so instead of inferring `vacant`."""
        p = _Full(people={("fleet-owner", "approver"): True})
        assignment_provider.register_provider(p)
        res = _apply(world, _manifest(person_seats=PEOPLE), dry_run=True)
        m = _member(res)
        assert m.verdict == "create"
        assert [(c.field, c.action, c.code, c.desired) for c in m.changes
                if c.field == "person_seats"] == [
            ("person_seats", "report", "seat_unobserved", PEOPLE)]
        note = next(c.note for c in m.changes if c.field == "person_seats")
        assert "does not exist yet" in note and "nothing was read" in note
        assert p.calls == []
        assert res.summary["roles"]["unobserved"] == 1

    def test_person_seats_are_reported_and_never_filled(self, world):
        p = _Full(people={("fleet-owner", "approver"): True, ("reviewer", "viewer"): False})
        assignment_provider.register_provider(p)
        world.seed_agent(LEAD)
        seats = PEOPLE + [{"role": "reviewer", "kind": "viewer"},
                          {"role": "ghost", "kind": "collaborator"}]
        res = _apply(world, _manifest(person_seats=seats), dry_run=False)
        m = _member(res)
        assert m.verdict == "unchanged" and res.blockers == []
        assert [(c.code, c.desired) for c in m.changes] == [
            ("seat_vacant", [{"role": "reviewer", "kind": "viewer"}]),
            ("seat_filled", [{"role": "fleet-owner", "kind": "approver"}]),
            ("seat_unobserved", [{"role": "ghost", "kind": "collaborator"}]),
        ]
        assert all(c.action == "report" for c in m.changes)
        assert {c[0] for c in p.calls} == {"person_seat_status"}
        assert res.summary["roles"] == {"write": 0, "report": 2, "unobserved": 1,
                                        "held_back": []}


# ===========================================================================
# Apply mode: a seat is written under the grant gate, like a grant
# ===========================================================================

class TestApplyGate:
    """Apply is person-only, but a person is not necessarily an admin: a
    creator who is not one writes no seat through apply. The plan checks the
    caller with the grant gate (`dependencies.is_interactive_admin`) and holds
    the write back the way grants do — a per-member report, a
    `summary.roles.held_back` entry and one warning line, shown by the dry run
    before any write, and never a blocker."""

    @pytest.mark.parametrize("who,verdict", [
        # An agent key never reaches an existing sibling (ent#629), so its case
        # runs on the member apply CREATES. A held-back seat alone changes
        # nothing about an existing member.
        ("agent_key", "create"), ("admin_user_key", "unchanged"),
        ("non_admin_person", "unchanged")])
    def test_every_other_caller_is_held_back_and_the_rest_still_applies(self, world, who, verdict):
        assignment_provider.register_provider(_Full({LEAD: "other-role"}))
        if verdict != "create":
            world.seed_agent(LEAD)
        user = {"agent_key": lambda: _agent_key(world),
                "admin_user_key": lambda: _user_key(world),
                "non_admin_person": lambda: world.user(W.ALICE)}[who]()
        res = _apply(world, _manifest("lead-role"), dry_run=True, user=user)
        assert res.status == "valid" and not res.blockers
        lead = _member(res)
        assert lead.verdict == verdict
        (held,) = [c for c in lead.changes if c.field == "seat"]
        assert (held.action, held.code, held.desired) == (
            "report", "seat_requires_interactive_admin", "lead-role")
        assert res.summary["roles"]["held_back"] == [{"name": LEAD, "seat": "lead-role"}]
        assert res.summary["roles"]["write"] == 0
        seat_warnings = [w for w in res.warnings if "seat_requires_interactive_admin" in w]
        assert len(seat_warnings) == 1 and f"{LEAD}: lead-role" in seat_warnings[0]

    def test_a_creator_writes_no_seat_on_a_real_run(self, world):
        p = _Full()
        assignment_provider.register_provider(p)
        world.seed_agent(LEAD)
        alice = world.user(W.ALICE)
        dry = _apply(world, _manifest("lead-role"), dry_run=True, user=alice)
        assert _codes(_member(dry)) == [("seat", "report", "seat_requires_interactive_admin")]
        assert _member(dry).verdict == "unchanged"
        res = _apply(world, _manifest("lead-role"), dry_run=False, user=alice)
        assert res.status == "applied", res.failed
        assert [c for c in p.calls if c[0] == "set_seat"] == [] and p.held == {}
        assert any("seat_requires_interactive_admin" in w for w in res.warnings)

    def test_a_creator_creating_a_member_writes_no_seat(self, world):
        p = _Full()
        assignment_provider.register_provider(p)
        res = _apply(world, _manifest("lead-role"), dry_run=False, user=world.user(W.ALICE))
        assert res.status == "applied", res.failed
        assert [c.name for c in world.create_calls] == [LEAD]
        assert p.calls == [] and p.held == {}

    def test_the_plan_consults_the_shared_gate_not_its_own_copy(self, world, monkeypatch):
        import dependencies
        monkeypatch.setattr(dependencies, "is_interactive_admin", lambda _u: False)
        assignment_provider.register_provider(_Full())
        world.seed_agent(LEAD)
        res = _apply(world, _manifest("lead-role"), dry_run=True, user=world.user(W.ADMIN))
        assert _codes(_member(res)) == [("seat", "report", "seat_requires_interactive_admin")]

    def test_without_the_writer_it_stays_unobserved_not_held_back(self, world):
        """Nothing could be written either way: the truer report wins."""
        assignment_provider.register_provider(_Reader())
        world.seed_agent(LEAD)
        res = _apply(world, _manifest("lead-role"), dry_run=True, user=world.user(W.ALICE))
        assert _codes(_member(res)) == [("seat", "report", "seat_unobserved")]
        assert res.summary["roles"]["held_back"] == []
        assert not [w for w in res.warnings if "seat_requires_interactive_admin" in w]

    def test_a_held_seat_needs_no_write_and_holds_nothing_back(self, world):
        assignment_provider.register_provider(_Full({LEAD: "lead-role"}))
        world.seed_agent(LEAD)
        res = _apply(world, _manifest("lead-role"), dry_run=True, user=world.user(W.ALICE))
        assert _codes(_member(res)) == [] and res.summary["roles"]["held_back"] == []


# ===========================================================================
# A read error is the retryable blocker, never unobserved
# ===========================================================================

class TestReadErrors:
    def test_a_person_seat_read_error_blocks_retryably(self, world):
        class Boom(_Full):
            def person_seat_status(self, *a):
                raise RuntimeError("db down")
        assignment_provider.register_provider(Boom())
        world.seed_agent(LEAD)
        res = _apply(world, _manifest(person_seats=PEOPLE), dry_run=True)
        assert [(b.code, b.member, b.retryable) for b in res.blockers] == [
            ("member_reads_unavailable", LEAD, True)]
        assert _member(res).verdict == "blocked"

    def test_a_seat_read_error_blocks_retryably(self, world):
        class Boom(_Full):
            def seat_for(self, agent_name):
                raise RuntimeError("db down")
        assignment_provider.register_provider(Boom())
        world.seed_agent(LEAD)
        res = _apply(world, _manifest("lead-role"), dry_run=True)
        assert [b.code for b in res.blockers] == ["member_reads_unavailable"]


# ===========================================================================
# No export or report carries an email
# ===========================================================================

class TestNoEmail:
    def test_a_report_never_carries_what_the_provider_over_answers(self, world):
        assignment_provider.register_provider(_Full(people={("fleet-owner", "approver"): True}))
        world.seed_agent(LEAD)
        res = _apply(world, _manifest(person_seats=PEOPLE), dry_run=False)
        assert HOLDER not in res.model_dump_json()
        assert "@" not in str([c.model_dump() for c in _member(res).changes])

    def test_an_email_shaped_seat_from_the_provider_is_a_read_failure_never_echoed(self, world):
        assignment_provider.register_provider(_Full({LEAD: HOLDER}))
        world.seed_agent(LEAD)
        res = _apply(world, _manifest("lead-role"), dry_run=True)
        assert [b.code for b in res.blockers] == ["member_reads_unavailable"]
        assert HOLDER not in res.model_dump_json()

    def _export(self):
        import yaml
        text = system_service.export_manifest(SYSTEM, [{"name": LEAD, "template": "local:plain"}],
                                              viewer=W.owner_viewer())
        return text, yaml.safe_load(text)

    def test_export_emits_the_held_seat_and_round_trips(self, world):
        assignment_provider.register_provider(_Full({LEAD: "lead-role"}))
        world.seed_agent(LEAD)
        text, doc = self._export()
        assert doc["agents"]["lead"]["seat"] == "lead-role"
        assert "person_seats" not in doc["agents"]["lead"]
        res = _apply(world, text, dry_run=True)
        assert _codes(_member(res)) == []

    def test_export_never_emits_an_email_shaped_seat_and_names_the_gap(self, world):
        assignment_provider.register_provider(_Full({LEAD: HOLDER}))
        world.seed_agent(LEAD)
        text, doc = self._export()
        assert "seat" not in doc["agents"]["lead"]
        assert HOLDER not in text
        assert text.splitlines()[0].startswith("# unobserved:") and f"seat of {LEAD}" in text

    @pytest.mark.parametrize("provider", [None, "without_seat_for"])
    def test_export_without_a_seat_reader_names_the_gap_like_a_raise(self, world, provider):
        """No `seat:` is "cannot say", not "holds none": the `# unobserved:`
        line names it, the same as a provider raise does."""
        if provider:
            class _NoSeatFor:
                def assignment_for(self, agent_name, triggered_by):
                    return None
            assignment_provider.register_provider(_NoSeatFor())
        world.seed_agent(LEAD)
        text, doc = self._export()
        assert "seat" not in doc["agents"]["lead"]
        assert text.splitlines()[0] == f"# unobserved: could not be read: seat of {LEAD}"

    def test_export_of_an_agent_that_holds_no_seat_names_no_gap(self, world):
        assignment_provider.register_provider(_Full())
        world.seed_agent(LEAD)
        text, doc = self._export()
        assert "seat" not in doc["agents"]["lead"]
        assert "seat of" not in text


# ===========================================================================
# Deploy
# ===========================================================================

class TestDeploy:
    @pytest.fixture(autouse=True)
    def _started(self, monkeypatch):
        async def started(names):
            return {n: "started" for n in names}
        monkeypatch.setattr(system_service, "start_all_agents", started)

    def test_without_the_writer_no_seat_is_written_and_the_caller_is_told(self, world):
        assignment_provider.register_provider(_Reader())
        res = _deploy(world, _manifest("lead-role", PEOPLE))
        assert res.status == "deployed", res.warnings
        held = [w for w in res.warnings if "Seats not written" in w]
        assert len(held) == 1 and f"{LEAD}: lead-role" in held[0]
        assert any("Seats not written" in w
                   for w in _deploy(world, _manifest("lead-role"), dry_run=True).warnings)

    def test_with_the_writer_the_created_member_is_seated(self, world):
        p = _Full()
        assignment_provider.register_provider(p)
        res = _deploy(world, _manifest("lead-role", PEOPLE))
        assert res.status == "deployed", res.warnings
        # The actor is attribution only, and it is the username — the same
        # actor `set_grant` and `assign_skill` record, never an email.
        assert p.calls == [("set_seat", LEAD, "lead-role", W.ADMIN)]
        assert not [w for w in res.warnings if "seat" in w.lower()]

    @pytest.mark.parametrize("who", ["agent_key", "admin_user_key", "non_admin_person"])
    def test_any_other_deployer_writes_no_seat_and_is_told(self, world, who):
        """`deploy_system` is `capability_fence("agents.manage")`, which an agent
        key satisfies. The provider is handed only a name, and an agent key's
        name is its admin owner's, so the core judges the principal and a
        manifest deployed by anything but a signed-in admin writes no seat."""
        user = {"agent_key": lambda: _agent_key(world),
                "admin_user_key": lambda: _user_key(world),
                "non_admin_person": lambda: world.user(W.ALICE)}[who]()
        p = _Full()
        assignment_provider.register_provider(p)
        res = _deploy(world, _manifest("lead-role"), user=user)
        assert res.status == "deployed", res.failed
        assert [c for c in p.calls if c[0] == "set_seat"] == [] and p.held == {}
        held = [w for w in res.warnings if "seats_require_interactive_admin" in w]
        assert len(held) == 1 and f"{LEAD}: lead-role" in held[0]
        dry = _deploy(world, _manifest("lead-role"), dry_run=True, user=user)
        assert any("seats_require_interactive_admin" in w for w in dry.warnings)

    def test_the_dry_run_of_a_signed_in_admin_holds_nothing_back(self, world):
        assignment_provider.register_provider(_Full())
        res = _deploy(world, _manifest("lead-role"), dry_run=True)
        assert not [w for w in res.warnings if "seat" in w.lower()]

    def test_a_refused_seat_is_a_warning_and_the_deploy_stands(self, world):
        assignment_provider.register_provider(_Full(refuse="not_assignment_admin"))
        res = _deploy(world, _manifest("lead-role"))
        assert res.status == "deployed"
        assert any("not_assignment_admin" in w and LEAD in w for w in res.warnings)
