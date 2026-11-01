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
