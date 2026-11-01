"""#4132 — an export round-trips: export → dry-run apply on the same system has
nothing to create, nothing to write and no drift for every field the export
writes. A field the export cannot observe is named in its `# unobserved:`
header and the dry run reads it as `unobserved`, never as drift.

Drives the REAL exporter (`system_service.export_manifest`) and the REAL apply
route (`routers.systems.apply_system`, `dry_run=True`) over the real database
facade; only Docker and the create/start seams are the `_ent695_world` fakes.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
import yaml

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
if _BACKEND_STR not in sys.path:
    sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend  # noqa: E402,F401

pytest.importorskip("docker", reason="backend venv required (system_service chain)")

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _ent695_world as W  # noqa: E402
from models import SystemApplyRequest  # noqa: E402
from services import assignment_provider, system_service  # noqa: E402

pytestmark = pytest.mark.unit

SYSTEM = W.SYSTEM
LEAD = f"{SYSTEM}-lead"


@pytest.fixture
def world(db_backend, monkeypatch, tmp_path):
    w = W.make_world(monkeypatch, tmp_path)
    W.install_executor_seams(w, monkeypatch)
    assignment_provider.clear_provider()
    yield w
    assignment_provider.clear_provider()


def _row(name):
    return {"name": name, "status": "running", "template": "local:plain"}


def _export(names, *, naming=None, viewer=None):
    text = system_service.export_manifest(SYSTEM, [_row(n) for n in names], naming=naming,
                                          viewer=viewer or W.owner_viewer())
    return text, yaml.safe_load(text)


def _dry_run(world, monkeypatch, text, user=None):
    """A real `POST /api/systems/apply` with `dry_run: true`."""
    from starlette.requests import Request

    from routers import systems

    real = system_service.apply_manifest

    async def apply_with_seams(manifest, u, request, *, dry_run):
        return await real(manifest, u, request, dry_run=dry_run,
                          create_agent_fn=world.create, start_agent_fn=world.docker.start)

    monkeypatch.setattr(systems, "apply_manifest", apply_with_seams)
    request = Request({"type": "http", "method": "POST", "path": "/api/systems/apply",
                       "headers": [], "client": ("127.0.0.1", 1), "query_string": b""})
    return asyncio.run(systems.apply_system(
        SystemApplyRequest(manifest=text, dry_run=True), request,
        current_user=user or world.user(W.ALICE)))


def _member(res, name=LEAD):
    return next(m for m in res.members if m.name == name)


def _codes(member):
    return [(c.field, c.action, c.code) for c in member.changes]


def _assert_round_trips(res):
    """Nothing to create, nothing to write, no drift, no blocker."""
    assert res.status == "valid", res
    assert res.blockers == []
    assert res.drift == []
    for m in res.members:
        assert m.verdict == "unchanged", (m.name, _codes(m))
        assert [c for c in m.changes if c.action == "write"] == [], (m.name, _codes(m))


# ===========================================================================
# CP1 — a provider that answers nothing for a seat cannot say: unobserved on
# both sides, never "holds none" and never a `seat_missing` write.
# ===========================================================================

class _NoAnswer:
    """A provider with both seat hooks whose `seat_for` answers nothing."""

    def __init__(self):
        self.calls = []

    def assignment_for(self, agent_name, triggered_by):
        return None

    def seat_for(self, agent_name):
        return None

    def set_seat(self, agent_name, role_id, created_by):
        self.calls.append(("set_seat", agent_name, role_id))
        return {"changed": True}


class _HoldsReadOnly:
    """Can say which seat an agent holds; cannot write one."""

    def __init__(self, held):
        self.held = dict(held)

    def assignment_for(self, agent_name, triggered_by):
        return None

    def seat_for(self, agent_name):
        role = self.held.get(agent_name)
        return {"case": "holds" if role else "none", "role_id": role,
                "seats": [role] if role else []}


class TestSeatNoAnswer:
    def test_export_names_a_seat_the_provider_did_not_answer(self, world):
        assignment_provider.register_provider(_NoAnswer())
        world.seed_agent(LEAD)
        text, doc = _export([LEAD])
        assert "seat" not in doc["agents"]["lead"]
        assert f"seat of {LEAD}" in text.splitlines()[0]
        assert text.splitlines()[0].startswith("# unobserved: could not be read:")

    def test_dry_run_reads_no_answer_as_unobserved_never_a_write(self, world, monkeypatch):
        provider = _NoAnswer()
        assignment_provider.register_provider(provider)
        world.seed_agent(LEAD)
        text = (f"name: {SYSTEM}\nagents:\n  lead:\n    template: local:plain\n"
                f"    seat: lead-role\n")
        res = _dry_run(world, monkeypatch, text, user=world.user(W.ADMIN))
        assert _codes(_member(res)) == [("seat", "report", "seat_unobserved")]
        assert _member(res).verdict == "unchanged"
        assert res.blockers == []
        assert provider.calls == []

    def test_export_of_a_no_answer_seat_round_trips(self, world, monkeypatch):
        assignment_provider.register_provider(_NoAnswer())
        world.seed_agent(LEAD)
        text, _ = _export([LEAD])
        _assert_round_trips(_dry_run(world, monkeypatch, text))


class TestUnobservedRoundTrip:
    def test_an_unobservable_field_round_trips_as_unobserved(self, world, monkeypatch):
        """The export states the seat; this install cannot write one, so the
        dry run reads it back as `seat_unobserved` — a report, never drift."""
        assignment_provider.register_provider(_HoldsReadOnly({LEAD: "lead-role"}))
        world.seed_agent(LEAD)
        text, doc = _export([LEAD])
        assert doc["agents"]["lead"]["seat"] == "lead-role"
        res = _dry_run(world, monkeypatch, text)
        _assert_round_trips(res)
        assert _codes(_member(res)) == [("seat", "report", "seat_unobserved")]
        assert res.summary["roles"]["unobserved"] == 1


# ===========================================================================
# CP2 — a value the member already holds is in sync: it is never written, so
# no library, catalog or validator verdict can block it.
# ===========================================================================

LIBRARY = {"deploy", "notes"}


@pytest.fixture
def lib(world, monkeypatch):
    """A skills library holding `LIBRARY` and no sets."""
    from services import skill_set_service
    from services.skill_service import skill_service
    monkeypatch.setattr(skill_service, "get_skill",
                        lambda n: {"name": n} if n in LIBRARY else None)
    monkeypatch.setattr(skill_set_service, "library_sets", lambda **_k: {})
    return world


class TestHeldValues:
    def test_a_held_skill_that_left_the_library_round_trips(self, lib, monkeypatch):
        from database import db
        lib.seed_agent(LEAD)
        db.assign_skill(LEAD, "research", W.ALICE)  # held; no longer in LIBRARY
        text, doc = _export([LEAD])
        assert doc["agents"]["lead"]["skills"] == ["research"]
        _assert_round_trips(_dry_run(lib, monkeypatch, text))

    def test_a_skill_not_held_still_gets_the_library_verdict(self, lib, monkeypatch):
        lib.seed_agent(LEAD)
        text = f"name: {SYSTEM}\nagents:\n  lead:\n    template: local:plain\n    skills: [research]\n"
        res = _dry_run(lib, monkeypatch, text)
        assert [(b.code, b.member) for b in res.blockers] == [("skill_unknown", LEAD)]

    def test_a_held_set_that_left_the_catalog_round_trips(self, lib, monkeypatch):
        from database import db
        lib.seed_agent(LEAD)
        db.assign_skill_set(LEAD, "ops-core", "src-1", W.ALICE, None, {"ops-core": ["notes"]})
        text, doc = _export([LEAD])
        assert doc["agents"]["lead"]["skill_sets"] == ["ops-core"]
        _assert_round_trips(_dry_run(lib, monkeypatch, text))

    def test_a_held_grant_whose_capability_was_retired_round_trips(self, world, monkeypatch):
        from database import db
        from db.capability_grants import CAPABILITY_SKILLS_MANAGE
        from services.system_apply import grants as apply_grants
        world.seed_agent(LEAD)
        assert db.grant_agent_capability(LEAD, CAPABILITY_SKILLS_MANAGE, W.ADMIN)
        monkeypatch.setattr(apply_grants, "CAPABILITIES",
                            apply_grants.CAPABILITIES - {CAPABILITY_SKILLS_MANAGE})
        admin = world.user(W.ADMIN)
        text, doc = _export([LEAD], viewer=admin)
        assert doc["agents"]["lead"]["grants"] == [CAPABILITY_SKILLS_MANAGE]
        _assert_round_trips(_dry_run(world, monkeypatch, text, user=admin))

    @pytest.mark.parametrize("approver", ["approver", "role:lead-role"])
    def test_a_stored_gate_the_validator_cannot_resolve_round_trips(self, world, monkeypatch,
                                                                    approver):
        """No assignment module: `approver` / `role:` no longer validate, but
        the row stands and the export states it."""
        from database import db
        world.seed_agent(LEAD)
        db.write_skill_gate(LEAD, "deploy", changes={"approver": approver, "deadline_hours": 4},
                            origin="set", set_by=W.ALICE, set_by_agent=None)
        text, doc = _export([LEAD])
        assert doc["agents"]["lead"]["gates"] == {"deploy": {"approver": approver,
                                                             "deadline_hours": 4}}
        _assert_round_trips(_dry_run(world, monkeypatch, text))

    def test_a_gate_restating_a_library_default_is_still_a_write(self, world, monkeypatch):
        """T3: only an explicit stored row is in sync."""
        from database import db
        world.seed_agent(LEAD)
        db.write_skill_gate(LEAD, "deploy", changes={"approver": "primary"},
                            origin="library_default", set_by="library", set_by_agent=None)
        text = (f"name: {SYSTEM}\nagents:\n  lead:\n    template: local:plain\n"
                f"    gates:\n      deploy: {{approver: primary}}\n")
        res = _dry_run(world, monkeypatch, text)
        assert [c.code for c in _member(res).changes] == ["gate_differs"]


# ===========================================================================
# CP3 — deterministic text, the person-seats line, the per-group round trip.
# ===========================================================================

B = f"{SYSTEM}-b"


def _seed_full(world, order):
    from database import db
    from db_models import ScheduleCreate
    for n in order:
        world.seed_agent(n, tags=(SYSTEM, "zeta", "alpha") if n == LEAD else (SYSTEM, "alpha", "zeta"))
    for sched in (("nightly", "0 1 * * *"), ("daily", "0 9 * * *"))[:: 1 if order[0] == LEAD else -1]:
        db.create_schedule(LEAD, W.ALICE, ScheduleCreate(name=sched[0], cron_expression=sched[1],
                                                        message=f"/{sched[0]}"))
    db.upsert_shared_folder_config(LEAD, expose_enabled=True, consume_enabled=False)
    db.set_resource_limits(LEAD, memory="8g", cpu="4")
    db.add_agent_permission(LEAD, B, W.ALICE)
    db.add_agent_permission(B, LEAD, W.ALICE)


class TestDeterministicText:
    def test_two_exports_in_reverse_roster_order_are_byte_identical(self, world, monkeypatch):
        W.install_seat_reader(monkeypatch)
        _seed_full(world, [B, LEAD])
        one, _ = _export([B, LEAD])
        two, _ = _export([LEAD, B])
        assert one == two
        doc = yaml.safe_load(one)
        assert list(doc["agents"]) == ["b", "lead"]
        assert doc["agents"]["lead"]["tags"] == ["alpha", "zeta"]
        assert [s["name"] for s in doc["agents"]["lead"]["schedules"]] == ["daily", "nightly"]
        assert list(doc["agents"]["lead"]["resources"]) == sorted(doc["agents"]["lead"]["resources"])


class TestPersonSeatsHeader:
    def test_every_export_names_the_person_seats_it_cannot_state(self, world, monkeypatch):
        W.install_seat_reader(monkeypatch)
        world.seed_agent(LEAD)
        world.seed_agent(B)
        text, doc = _export([LEAD, B])
        line = next(l for l in text.splitlines() if "person seats" in l)
        assert line.startswith("# unobserved: person seats are not exported")
        assert line.endswith(f": {B}, {LEAD}")
        assert all("person_seats" not in a for a in doc["agents"].values())

        # A non-owner viewer cannot read the agent seat, but the person-seats
        # line is about the seam, not the caller: it names the members too.
        from database import db
        for n in (LEAD, B):
            db.share_agent(n, W.ALICE, world.user(W.BOB).email)
        shared, _ = _export([LEAD, B], viewer=world.user(W.BOB))
        assert f"seat of {B}" in shared
        assert line in shared.splitlines()


class TestGroupRoundTrips:
    def test_folders_tags_resources_schedules_mesh_template_round_trip(self, world, monkeypatch):
        W.install_seat_reader(monkeypatch)
        _seed_full(world, [LEAD, B])
        text, doc = _export([LEAD, B])
        lead = doc["agents"]["lead"]
        assert lead["folders"] == {"expose": True, "consume": False}
        assert lead["tags"] == ["alpha", "zeta"] and lead["template"] == "local:plain"
        assert lead["resources"] and len(lead["schedules"]) == 2
        assert doc["permissions"] == {"preset": "full-mesh"}
        _assert_round_trips(_dry_run(world, monkeypatch, text))

    def test_explicit_edges_round_trip(self, world, monkeypatch):
        from database import db
        W.install_seat_reader(monkeypatch)
        world.seed_agent(LEAD)
        world.seed_agent(B)
        world.seed_agent(f"{SYSTEM}-c")
        db.add_agent_permission(LEAD, B, W.ALICE)
        text, doc = _export([LEAD, B, f"{SYSTEM}-c"])
        assert doc["permissions"] == {"explicit": {"lead": ["b"]}}
        _assert_round_trips(_dry_run(world, monkeypatch, text))

    @pytest.mark.parametrize("naming", [None, "bare"])
    def test_naming_round_trips(self, world, monkeypatch, naming):
        W.install_seat_reader(monkeypatch)
        world.seed_agent(LEAD)
        world.seed_agent("helper")
        text, doc = _export([LEAD, "helper"], naming=naming)
        if naming is None:
            assert doc.get("naming") is None and "members omitted" in text.splitlines()[0]
            assert list(doc["agents"]) == ["lead"]
        else:
            assert doc["naming"] == "bare" and list(doc["agents"]) == [LEAD, "helper"]
            _assert_round_trips(_dry_run(world, monkeypatch, text))

    def test_deploy_false_is_named_and_never_exported(self, world, monkeypatch):
        W.install_seat_reader(monkeypatch)
        world.seed_agent(LEAD)
        text, doc = _export([LEAD])
        assert system_service.DECLARED_ONLY_EXPORT_HEADER in text.splitlines()
        assert all("deploy" not in a for a in doc["agents"].values())
        _assert_round_trips(_dry_run(world, monkeypatch, text))


class TestTemplateAlone:
    def test_a_member_whose_only_field_is_its_template_round_trips(self, world, monkeypatch):
        """The export writes the `trinity.template` label verbatim and apply
        compares that same label: nothing to report, nothing to write."""
        W.install_seat_reader(monkeypatch)
        template = "github:Org/scout"
        world.seed_agent(LEAD, template=template)
        text = system_service.export_manifest(
            SYSTEM, [{"name": LEAD, "status": "running", "template": template}],
            viewer=W.owner_viewer())
        doc = yaml.safe_load(text)
        assert doc["agents"]["lead"]["template"] == template
        res = _dry_run(world, monkeypatch, text)
        _assert_round_trips(res)
        assert "template" not in [c.field for c in _member(res).changes]

        # The comparison is live: a different template is reported, never written.
        edited = text.replace(f"template: {template}", "template: local:plain")
        codes = _codes(_member(_dry_run(world, monkeypatch, edited)))
        assert ("template", "report", "template_differs") in codes


class TestPersonSeatsRoundTrip:
    def test_declared_person_seats_round_trip_as_unobserved_through_the_header(
            self, world, monkeypatch):
        """The export names the member under `person seats are not exported`;
        a manifest that declares them reads back as `seat_unobserved` — a
        report, never drift and never a write."""
        W.install_seat_reader(monkeypatch)
        world.seed_agent(LEAD)
        text, _ = _export([LEAD])
        header = next(l for l in text.splitlines() if "person seats" in l)
        assert header.startswith("# unobserved: person seats are not exported")
        assert header.endswith(f": {LEAD}")

        declared = text.replace(
            "  lead:\n",
            "  lead:\n    person_seats:\n    - role: fleet-owner\n      kind: approver\n", 1)
        assert declared != text and declared.splitlines()[:3] == text.splitlines()[:3]
        res = _dry_run(world, monkeypatch, declared)
        _assert_round_trips(res)
        assert _codes(_member(res)) == [("person_seats", "report", "seat_unobserved")]
        assert _member(res).changes[0].desired == [{"role": "fleet-owner", "kind": "approver"}]
