"""trinity-enterprise#1030 — what a manifest export may say, and what it restores.

Two defects the group tests could not see, both pinned by running the real
exporter over the real db:

* the export named `grants` and `gates` to callers the direct read routes refuse
  (a person the agent is shared with; an agent key reaching a target by an edge);
* the export wrote `resources` from the container label, while apply compares
  against the stored override first, so re-applying an export reverted a limit
  an operator had raised and not yet restarted into.
"""
from __future__ import annotations

import importlib
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
from database import db  # noqa: E402
from services import system_service  # noqa: E402

pytestmark = pytest.mark.unit

SYSTEM = W.SYSTEM
LEAD = f"{SYSTEM}-lead"


@pytest.fixture
def world(db_backend, monkeypatch, tmp_path):
    w = W.make_world(monkeypatch, tmp_path)
    W.install_executor_seams(w, monkeypatch)
    w.seed_agent(LEAD)
    db.grant_agent_capability(LEAD, "skills.manage", W.ADMIN)
    db.write_skill_gate(LEAD, "deploy", changes={"approver": "primary"}, origin="set",
                        set_by=W.ALICE, set_by_agent=None)
    return w


def _export(viewer, agent=None):
    row = dict(agent or {"name": LEAD, "template": "local:plain"})
    text = system_service.export_manifest(SYSTEM, [row], viewer=viewer)
    return text, yaml.safe_load(text)["agents"]["lead"]


def _agent_key(world, own):
    return world.user(W.ALICE).model_copy(update={"mcp_scope": "agent", "agent_name": own})


class TestWhoReadsWhat:
    def test_the_owner_reads_grants_and_gates(self, world):
        text, lead = _export(world.user(W.ALICE))
        assert lead["grants"] == ["skills.manage"]
        assert lead["gates"] == {"deploy": {"approver": "primary"}}
        assert "not readable" not in text

    def test_an_admin_reads_grants_and_gates(self, world):
        _text, lead = _export(world.user(W.ADMIN))
        assert lead["grants"] == ["skills.manage"] and "gates" in lead

    def test_a_shared_person_reads_gates_but_not_grants_and_is_told(self, world):
        db.share_agent(LEAD, W.ALICE, world.user(W.BOB).email)
        text, lead = _export(world.user(W.BOB))
        assert "grants" not in lead
        assert lead["gates"] == {"deploy": {"approver": "primary"}}
        assert f"# unobserved: not readable by this caller: grants of {LEAD}" in text
        assert "skills.manage" not in text

    def test_an_agent_key_by_an_edge_reads_neither(self, world):
        db.register_agent_owner("sibling", W.ALICE)
        db.add_agent_permission("sibling", LEAD, W.ALICE)
        text, lead = _export(_agent_key(world, "sibling"))
        assert "grants" not in lead and "gates" not in lead
        assert f"grants of {LEAD}" in text and f"gates of {LEAD}" in text
        assert "skills.manage" not in text

    def test_an_agent_key_reads_its_own(self, world):
        _text, lead = _export(_agent_key(world, LEAD))
        assert lead["grants"] == ["skills.manage"] and "gates" in lead

    def test_a_withheld_group_says_the_same_whether_or_not_anything_is_held(self, world):
        """The header must not become a way to learn who holds a grant."""
        db.share_agent(LEAD, W.ALICE, world.user(W.BOB).email)
        held, _ = _export(world.user(W.BOB))
        db.revoke_agent_capability(LEAD, "skills.manage")
        empty, _ = _export(world.user(W.BOB))
        assert held == empty

    def test_no_principal_reads_nothing_it_was_not_given(self, world):
        text, lead = _export(None)
        assert "grants" not in lead and "gates" not in lead and "seat" not in lead
        assert "not readable by this caller" in text

    def test_the_route_hands_the_caller_to_the_exporter(self, world, monkeypatch):
        """Production wiring: drop `viewer=current_user` and this goes red."""
        import asyncio
        routers_systems = importlib.import_module("routers.systems")
        routers_agents = importlib.import_module("routers.agents")
        monkeypatch.setattr(routers_agents, "get_accessible_agents",
                            lambda _u: [{"name": LEAD, "template": "local:plain"}])
        db.share_agent(LEAD, W.ALICE, world.user(W.BOB).email)
        text = asyncio.run(routers_systems.get_system_manifest(
            SYSTEM, naming=None, current_user=world.user(W.BOB)))
        assert "skills.manage" not in text and f"grants of {LEAD}" in text
        owner_text = asyncio.run(routers_systems.get_system_manifest(
            SYSTEM, naming=None, current_user=world.user(W.ALICE)))
        assert "skills.manage" in owner_text and "not readable" not in owner_text


class TestResources:
    AGENT = {"name": LEAD, "template": "local:plain", "resources": {"cpu": "2", "memory": "4g"}}

    def test_a_stored_override_wins_over_the_label(self, world):
        db.set_resource_limits(LEAD, memory="8g", cpu="4")
        _text, lead = _export(world.user(W.ALICE), self.AGENT)
        assert lead["resources"] == {"cpu": "4", "memory": "8g"}

    def test_a_partial_override_keeps_the_other_label(self, world):
        db.set_resource_limits(LEAD, memory="8g", cpu=None)
        _text, lead = _export(world.user(W.ALICE), self.AGENT)
        assert lead["resources"] == {"cpu": "2", "memory": "8g"}

    def test_no_override_exports_the_label(self, world):
        _text, lead = _export(world.user(W.ALICE), self.AGENT)
        assert lead["resources"] == {"cpu": "2", "memory": "4g"}

    def test_an_unreadable_override_is_named_never_the_label(self, world, monkeypatch):
        def boom(_n):
            raise RuntimeError("db locked")
        monkeypatch.setattr(db, "get_resource_limits", boom)
        text, lead = _export(world.user(W.ALICE), self.AGENT)
        assert "resources" not in lead
        assert f"resources of {LEAD}" in text

    def test_re_applying_an_export_keeps_a_pending_limit(self, world):
        """The defect itself: raised, not yet restarted, exported, re-applied."""
        db.set_resource_limits(LEAD, memory="8g", cpu="4")
        text, _lead = _export(world.user(W.ALICE), self.AGENT)
        res = W.apply(world, text, W.ALICE, dry_run=False)
        assert res.status == "applied", (res.blockers, res.failed)
        assert db.get_resource_limits(LEAD) == {"memory": "8g", "cpu": "4"}
        member = next(m for m in res.members if m.name == LEAD)
        assert not [c for c in member.changes if c.field.startswith("resources")]
