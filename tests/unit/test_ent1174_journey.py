"""trinity-enterprise#1174 AC4 + the door (AC2), end to end at the service layer.

The Regent holds a user-scoped MCP key owned by the instance admin. An
interactive admin recorded the hub member. Apply, run with that key, creates
the member's agent and — through the REAL enterprise assignments module on
the REAL seam — seats the member as primary and shares the agent, so:

* the member's Workspace roster lists the agent right after apply (AC4);
* the Workspace door takes the member's email: a code is minted, and the
  correct code hands a platform account on to `/login` (#3922/#4157) — never
  a refusal (AC2);
* operator ruling R31: the member is the instance's administrator unless they
  said otherwise — covered for an admin-role AND a user-role member; neither
  account is created at, or moved to, role `user` by the door.

Needs the private submodule; skips on an OSS clone.
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
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db_harness import db_backend  # noqa: E402,F401

pytest.importorskip("docker", reason="backend venv required (system_service chain)")
if not (_BACKEND / "enterprise" / "backend" / "assignments" / "hub_member.py").exists():
    pytest.skip("private enterprise submodule not mounted", allow_module_level=True)

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _ent695_world as W  # noqa: E402
from services import assignment_provider, system_service  # noqa: E402

MEMBER = "member1174@example.com"
LEAD = f"{W.SYSTEM}-lead"
MANIFEST = (f"name: {W.SYSTEM}\nagents:\n  lead:\n    template: local:plain\n"
            "    person_seats:\n      - {role: digest-reader, kind: primary, holder: member}\n")


@pytest.fixture
def world(db_backend, monkeypatch, tmp_path):
    from services.entitlement_service import entitlement_service

    from enterprise.backend.assignments import _register_prompt_provider
    from db.engine import get_engine

    from enterprise.backend.tables import enterprise_assignments
    w = W.make_world(monkeypatch, tmp_path)
    W.install_executor_seams(w, monkeypatch)
    # The private table, on whichever backend the harness activated (the
    # module's own SQLite DDL path reads a different DB_PATH).
    enterprise_assignments.create(get_engine(), checkfirst=True)
    entitlement_service.register_module("assignments")
    assignment_provider.clear_provider()
    _register_prompt_provider()
    yield w
    assignment_provider.clear_provider()
    entitlement_service.unregister_module("assignments")


def _controller_key(world):
    return world.user(W.ADMIN).model_copy(update={"mcp_scope": "user"})


def _record_member(role):
    from database import db

    from enterprise.backend.assignments import hub_member
    db.add_to_whitelist(MEMBER, W.ADMIN, source="manual", default_role=role)
    hub_member.set_record(MEMBER, "public")


@pytest.mark.parametrize("role", ["admin", "user"])
def test_apply_with_the_controller_key_puts_the_agent_on_the_members_workspace(world, role):
    from client_portal import service as portal
    from database import db

    from enterprise.backend.assignments import db as adb
    _record_member(role)
    key = _controller_key(world)

    dry = asyncio.run(system_service.apply_manifest(
        MANIFEST, key, None, dry_run=True,
        create_agent_fn=world.create, start_agent_fn=world.docker.start))
    lead = next(m for m in dry.members if m.name == LEAD)
    assert [(c.action, c.code) for c in lead.changes if c.field == "person_seats"] == [
        ("write", "seat_missing")]
    assert db.get_user_by_email(MEMBER) is None  # the dry run created nothing

    res = asyncio.run(system_service.apply_manifest(
        MANIFEST, key, None, dry_run=False,
        create_agent_fn=world.create, start_agent_fn=world.docker.start))
    assert res.status == "applied", res.failed

    member = db.get_user_by_email(MEMBER)
    assert member["role"] == role  # R31: never created at, or moved to, `user`
    primaries = [r for r in adb.list_for_agent(LEAD) if r["kind"] == "primary"]
    assert [r["user_id"] for r in primaries] == [member["id"]]
    assert db.get_agent_owner(LEAD)["owner_username"] == W.ADMIN

    roster = asyncio.run(portal.get_roster(MEMBER))
    assert LEAD in [a.name for a in roster.agents]

    # AC2: the door takes the member — a code, then on to `/login`.
    code = portal.portal_signin_request(MEMBER)
    assert code is not None
    assert portal.portal_signin_verify(MEMBER, code) is portal.PLATFORM_LOGIN_REQUIRED

    again = asyncio.run(system_service.apply_manifest(
        MANIFEST, key, None, dry_run=True,
        create_agent_fn=world.create, start_agent_fn=world.docker.start))
    lead = next(m for m in again.members if m.name == LEAD)
    assert [(c.action, c.code) for c in lead.changes if c.field == "person_seats"] == [
        ("report", "seat_filled")]


def test_an_existing_account_keeps_its_role(world):
    from database import db
    from db_models import UserCreate
    db.create_user(UserCreate(username=MEMBER, role="admin", email=MEMBER))
    _record_member("user")  # the whitelist row says `user`; the account already says admin
    res = asyncio.run(system_service.apply_manifest(
        MANIFEST, _controller_key(world), None, dry_run=False,
        create_agent_fn=world.create, start_agent_fn=world.docker.start))
    assert res.status == "applied", res.failed
    assert db.get_user_by_email(MEMBER)["role"] == "admin"
