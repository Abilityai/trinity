"""trinity-enterprise#1174: apply seats the instance's ONE recorded hub member.

A member instance's Regent holds a user-scoped MCP key (the "controller key").
With it, apply could create the member's agent but never make the member its
primary seat nor share it, so the member's Workspace stayed empty. The manifest
now names that person without naming a person:

    person_seats: [{role: digest-reader, kind: primary, holder: member}]

`holder: member` resolves on the instance (an interactive admin recorded the
member at bootstrap); no email ever enters the manifest (ent#1030 kept). The
write goes through two NEW optional hooks on the assignment seam,
`member_seat_status` (read) and `seat_member` (write) — neither takes an email:
the provider resolves the person from its own record.

Operator ruling T2 (2026-11-09): any admin-owned person-scope key may seat and
share the ONE recorded member as primary — nothing else. `seat:` (the agent's
own seat), grants and every other person seat keep the interactive-admin gate.
A key may write the member seat only on a member this run creates or a managed
member the caller owns (E4).

Runs over the REAL `db` on a full schema; Docker is faked at its read seams
(`_ent695_world.py`). The provider is a fake registered on the real seam.
"""
from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db_harness import db_backend  # noqa: E402,F401

pytest.importorskip("docker", reason="backend venv required (system_service chain)")

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _ent695_world as W  # noqa: E402
from services import assignment_provider, system_service  # noqa: E402


SYSTEM = W.SYSTEM
LEAD = f"{SYSTEM}-lead"
MEMBER = {"role": "digest-reader", "kind": "primary", "holder": "member"}


@pytest.fixture
def world(db_backend, monkeypatch, tmp_path):
    w = W.make_world(monkeypatch, tmp_path)
    W.install_executor_seams(w, monkeypatch)
    assignment_provider.clear_provider()
    yield w
    assignment_provider.clear_provider()


def _manifest(person_seats=None, *, seat=None) -> str:
    lines = [f"name: {SYSTEM}", "agents:", "  lead:", "    template: local:plain"]
    if seat is not None:
        lines.append(f"    seat: {seat}")
    if person_seats is not None:
        lines.append("    person_seats:")
        for entry in person_seats:
            lines.append("      - {" + ", ".join(f"{k}: {v}" for k, v in entry.items()) + "}")
    return "\n".join(lines) + "\n"


def _key(world, username=W.ADMIN, scope="user", **extra):
    """A person's own MCP key: resolves to its owner carrying the owner's role."""
    return world.user(username).model_copy(update={"mcp_scope": scope, **extra})


# ===========================================================================
# Parse: `holder: member`, primary only, never a person (T1, T6)
# ===========================================================================

class TestParse:
    def test_holder_member_with_primary_is_read(self):
        m = system_service.parse_manifest(_manifest([MEMBER]))
        assert m.agents["lead"].person_seats == [MEMBER]

    def test_a_plain_person_seat_is_unchanged(self):
        m = system_service.parse_manifest(_manifest([{"role": "reviewer", "kind": "viewer"}]))
        assert m.agents["lead"].person_seats == [{"role": "reviewer", "kind": "viewer"}]

    def test_an_email_is_refused_and_the_refusal_names_holder_member(self):
        entry = {"role": "digest-reader", "kind": "primary", "holder": "pat@example.org"}
        with pytest.raises(ValueError) as exc:
            system_service.parse_manifest(_manifest([entry]))
        msg = str(exc.value)
        assert "holder: member" in msg and "never a person" in msg
        assert "@" not in msg

    @pytest.mark.parametrize("kind", ["viewer", "approver", "collaborator"])
    def test_holder_with_any_other_kind_is_a_named_refusal(self, kind):
        with pytest.raises(ValueError, match=r"holder: member is allowed only with kind: primary"):
            system_service.parse_manifest(_manifest([{**MEMBER, "kind": kind}]))

    def test_holder_with_any_other_value_is_a_named_refusal(self):
        with pytest.raises(ValueError, match=r"holder's only legal value is 'member'"):
            system_service.parse_manifest(_manifest([{**MEMBER, "holder": "alice"}]))

    def test_two_member_entries_on_one_agent_are_refused(self):
        with pytest.raises(ValueError, match=r"at most one .*holder: member"):
            system_service.parse_manifest(_manifest(
                [MEMBER, {**MEMBER, "role": "other-role"}]))


# ===========================================================================
# The gate (T2): a person, an admin — nothing else
# ===========================================================================

class TestGate:
    @pytest.mark.parametrize("who,expected", [
        ("jwt_admin", True),
        ("user_key_admin", True),
        ("user_key_creator", False),
        ("jwt_creator", False),
        ("agent_key", False),
        ("system_key", False),
        ("connector_key", False),
        ("portal_delegate", False),
        ("ops_key", False),
        ("loopback", False),
        ("vouched", False),
    ])
    def test_can_seat_member(self, world, who, expected):
        from dependencies import can_seat_member
        u = {
            "jwt_admin": lambda: world.user(W.ADMIN),
            "user_key_admin": lambda: _key(world),
            "user_key_creator": lambda: _key(world, W.ALICE),
            "jwt_creator": lambda: world.user(W.ALICE),
            "agent_key": lambda: _key(world, scope="agent", agent_name="orchestrator"),
            "system_key": lambda: _key(world, scope="system"),
            "connector_key": lambda: _key(world, scope="connector", connector_agent="x"),
            "portal_delegate": lambda: _key(world, scope="portal_delegate",
                                            portal_delegate=True),
            "ops_key": lambda: _key(world, scope="ops"),
            "loopback": lambda: world.user(W.ADMIN).model_copy(
                update={"is_event_loopback": True}),
            "vouched": lambda: world.user(W.ADMIN).model_copy(
                update={"vouched_source_agent": "orchestrator"}),
        }[who]()
        assert can_seat_member(u) is expected

    def test_an_object_without_mcp_scope_fails_closed(self):
        from dependencies import can_seat_member
        assert can_seat_member(SimpleNamespace(role="admin", username="x")) is False


# ===========================================================================
# Reserved settings (E3): a key the generic catch-all must not reach
# ===========================================================================

class TestReservedSettings:
    KEY = "ent1174_test_reserved"

    @pytest.fixture
    def reserved(self, world):
        from services import reserved_settings
        reserved_settings.register_reserved_setting(
            self.KEY, "PUT /api/enterprise/hub/member (admin, signed-in session)")
        from database import db
        db.set_setting(self.KEY, '{"door": "public"}')
        yield
        reserved_settings.unregister_reserved_setting(self.KEY)

    def _call(self, coro):
        from fastapi import HTTPException
        try:
            return asyncio.run(coro), None
        except HTTPException as e:
            return None, e

    def test_generic_put_is_refused_for_every_caller_naming_the_route(self, world, reserved):
        from database import SystemSettingUpdate
        from routers.settings.generic import update_setting
        for user in (world.user(W.ADMIN), _key(world)):
            _, err = self._call(update_setting(
                self.KEY, SystemSettingUpdate(value="{}"), None, user))
            assert err is not None and err.status_code == 422
            assert "PUT /api/enterprise/hub/member" in str(err.detail)
        from database import db
        assert db.get_setting_value(self.KEY, None) == '{"door": "public"}'

    def test_generic_delete_is_refused(self, world, reserved):
        from routers.settings.generic import delete_setting
        _, err = self._call(delete_setting(self.KEY, None, world.user(W.ADMIN)))
        assert err is not None and err.status_code == 422
        from database import db
        assert db.get_setting_value(self.KEY, None) is not None

    def test_a_key_cannot_read_it_and_the_list_omits_it(self, world, reserved):
        from routers.settings.generic import get_all_settings, get_setting
        _, err = self._call(get_setting(self.KEY, None, _key(world)))
        assert err is not None and err.status_code == 404
        listed, _ = self._call(get_all_settings(None, _key(world)))
        assert self.KEY not in {s.key for s in listed}

    def test_a_signed_in_admin_still_reads_it(self, world, reserved):
        from routers.settings.generic import get_all_settings, get_setting
        got, err = self._call(get_setting(self.KEY, None, world.user(W.ADMIN)))
        assert err is None and got.key == self.KEY
        listed, _ = self._call(get_all_settings(None, world.user(W.ADMIN)))
        assert self.KEY in {s.key for s in listed}

    def test_an_unreserved_key_still_reads_through_the_generic_route(self, world):
        """The reservation is per key: nothing else changes for a key caller."""
        from database import db
        from routers.settings.generic import get_setting
        db.set_setting("ent1174_plain", "x")
        got, err = self._call(get_setting("ent1174_plain", None, _key(world)))
        assert err is None and got.value == "x"
