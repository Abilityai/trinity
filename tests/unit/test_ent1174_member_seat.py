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


# ===========================================================================
# Plan + run (Phase 2): the seam hooks, the op, the gate per member
# ===========================================================================

class _Member:
    """A provider with the agent-seat hooks AND the two member-seat hooks."""

    def __init__(self, state="vacant", refuse=None, extra=None):
        self.state = state
        self.refuse = refuse
        self.extra = extra or {}
        self.calls = []
        self.held = {}

    def assignment_for(self, agent_name, triggered_by):
        return None

    def seat_for(self, agent_name):
        role = self.held.get(agent_name)
        return {"case": "holds" if role else "none", "role_id": role,
                "seats": [role] if role else []}

    def set_seat(self, agent_name, role_id, created_by):
        self.calls.append(("set_seat", agent_name, role_id, created_by))
        self.held[agent_name] = role_id
        return {"changed": True}

    def person_seat_status(self, agent_name, role_id, kind):
        return {"filled": False}

    def member_seat_status(self, agent_name, role_id, kind):
        self.calls.append(("member_seat_status", agent_name, role_id, kind))
        return None if self.state is None else {"state": self.state, **self.extra}

    def seat_member(self, agent_name, role_id, kind, created_by):
        self.calls.append(("seat_member", agent_name, role_id, kind, created_by))
        if self.refuse:
            return {"refused": self.refuse}
        self.state = "filled"
        return {"changed": True, "shared": True}


class _NoMemberHooks(_Member):
    member_seat_status = None
    seat_member = None


def _apply(world, text, *, dry_run, user):
    return asyncio.run(system_service.apply_manifest(
        text, user, None, dry_run=dry_run,
        create_agent_fn=world.create, start_agent_fn=world.docker.start))


def _m(res, name=LEAD):
    return next(m for m in res.members if m.name == name)


def _person_changes(member):
    return [(c.action, c.code) for c in member.changes if c.field == "person_seats"]


def _writes(p):
    return [c for c in p.calls if c[0] == "seat_member"]


class TestPlan:
    def test_without_the_member_hooks_it_is_an_ordinary_report(self, world):
        assignment_provider.register_provider(_NoMemberHooks())
        world.seed_agent(LEAD, owner=W.ADMIN)
        res = _apply(world, _manifest([MEMBER]), dry_run=True, user=_key(world))
        assert _person_changes(_m(res)) == [("report", "seat_vacant")]

    def test_an_admin_key_seats_the_member_on_a_member_it_owns(self, world):
        p = _Member()
        assignment_provider.register_provider(p)
        world.seed_agent(LEAD, owner=W.ADMIN)
        key = _key(world)
        dry = _apply(world, _manifest([MEMBER]), dry_run=True, user=key)
        assert _person_changes(_m(dry)) == [("write", "seat_missing")]
        assert _m(dry).verdict == "update"
        assert _writes(p) == []  # the dry run writes nothing
        res = _apply(world, _manifest([MEMBER]), dry_run=False, user=key)
        assert res.status == "applied", res.failed
        assert _writes(p) == [("seat_member", LEAD, "digest-reader", "primary", W.ADMIN)]
        again = _apply(world, _manifest([MEMBER]), dry_run=True, user=key)
        assert _person_changes(_m(again)) == [("report", "seat_filled")]
        assert _m(again).verdict == "unchanged"

    def test_a_partial_seat_is_written_again(self, world):
        assignment_provider.register_provider(_Member(state="partial"))
        world.seed_agent(LEAD, owner=W.ADMIN)
        dry = _apply(world, _manifest([MEMBER]), dry_run=True, user=_key(world))
        assert _person_changes(_m(dry)) == [("write", "seat_missing")]

    def test_a_member_this_run_creates_is_seated(self, world):
        """E1: the issue's own case — apply creates the member's agent."""
        p = _Member()
        assignment_provider.register_provider(p)
        key = _key(world)
        dry = _apply(world, _manifest([MEMBER]), dry_run=True, user=key)
        assert _m(dry).verdict == "create"
        assert _person_changes(_m(dry)) == [("write", "seat_missing")]
        assert _writes(p) == []
        res = _apply(world, _manifest([MEMBER]), dry_run=False, user=key)
        assert res.status == "applied", res.failed
        assert [c.name for c in world.create_calls] == [LEAD]
        assert _writes(p) == [("seat_member", LEAD, "digest-reader", "primary", W.ADMIN)]

    def test_a_member_to_create_with_no_member_recorded_says_so_in_the_dry_run(self, world):
        assignment_provider.register_provider(_Member(state="no_member"))
        res = _apply(world, _manifest([MEMBER]), dry_run=True, user=_key(world))
        assert _person_changes(_m(res)) == [("report", "member_not_recorded")]

    def test_an_admin_key_is_held_back_on_a_member_someone_else_owns(self, world):
        """E4: a personal admin key must not share an unrelated agent."""
        p = _Member()
        assignment_provider.register_provider(p)
        world.seed_agent(LEAD, owner=W.ALICE)
        res = _apply(world, _manifest([MEMBER]), dry_run=False, user=_key(world))
        assert _person_changes(_m(res)) == [("report", "seat_requires_interactive_admin")]
        assert res.summary["roles"]["held_back"] == [
            {"name": LEAD, "seat": "digest-reader (member, primary)"}]
        assert _writes(p) == []

    def test_an_interactive_admin_seats_the_member_on_any_managed_member(self, world):
        p = _Member()
        assignment_provider.register_provider(p)
        world.seed_agent(LEAD, owner=W.ALICE)
        res = _apply(world, _manifest([MEMBER]), dry_run=False, user=world.user(W.ADMIN))
        assert res.status == "applied", res.failed
        assert len(_writes(p)) == 1

    @pytest.mark.parametrize("who", ["creator_key", "creator_jwt"])
    def test_a_non_admin_person_is_held_back(self, world, who):
        p = _Member()
        assignment_provider.register_provider(p)
        world.seed_agent(LEAD, owner=W.ALICE)
        user = _key(world, W.ALICE) if who == "creator_key" else world.user(W.ALICE)
        res = _apply(world, _manifest([MEMBER]), dry_run=False, user=user)
        assert _person_changes(_m(res)) == [("report", "seat_requires_interactive_admin")]
        assert _writes(p) == []

    def test_a_non_admin_creating_the_member_is_held_back(self, world):
        p = _Member()
        assignment_provider.register_provider(p)
        res = _apply(world, _manifest([MEMBER]), dry_run=False, user=_key(world, W.ALICE))
        assert _person_changes(_m(res)) == [("report", "seat_requires_interactive_admin")]
        assert _writes(p) == []

    def test_another_primary_is_reported_and_never_moved(self, world):
        p = _Member(state="differs")
        assignment_provider.register_provider(p)
        world.seed_agent(LEAD, owner=W.ADMIN)
        res = _apply(world, _manifest([MEMBER]), dry_run=False, user=_key(world))
        m = _m(res)
        assert _person_changes(m) == [("report", "member_seat_differs")]
        note = next(c.note for c in m.changes if c.field == "person_seats")
        assert "never moves" in note and "@" not in note
        assert _writes(p) == []

    def test_no_member_recorded_is_a_report_naming_the_fix(self, world):
        assignment_provider.register_provider(_Member(state="no_member"))
        world.seed_agent(LEAD, owner=W.ADMIN)
        res = _apply(world, _manifest([MEMBER]), dry_run=True, user=_key(world))
        m = _m(res)
        assert _person_changes(m) == [("report", "member_not_recorded")]
        assert res.blockers == []
        note = next(c.note for c in m.changes if c.field == "person_seats")
        assert "PUT /api/enterprise/hub/member" in note

    def test_a_refusal_lands_in_failed_with_the_modules_code(self, world):
        assignment_provider.register_provider(_Member(refuse="member_not_known"))
        world.seed_agent(LEAD, owner=W.ADMIN)
        res = _apply(world, _manifest([MEMBER]), dry_run=False, user=_key(world))
        assert [(f.name, f.step, f.reason.split(":")[0]) for f in res.failed] == [
            (LEAD, "person_seats", "member_not_known")]

    def test_a_read_error_blocks_retryably(self, world):
        class _Raises(_Member):
            def member_seat_status(self, agent_name, role_id, kind):
                raise RuntimeError("db down")
        assignment_provider.register_provider(_Raises())
        world.seed_agent(LEAD, owner=W.ADMIN)
        res = _apply(world, _manifest([MEMBER]), dry_run=True, user=_key(world))
        assert _m(res).verdict == "blocked"
        assert [b.code for b in res.blockers] == ["member_reads_unavailable"]

    def test_the_landed_write_is_audited_with_the_caller_and_no_address(self, world, monkeypatch):
        from services.platform_audit_service import platform_audit_service
        seen = []

        async def _log(**kw):
            seen.append(kw)
        monkeypatch.setattr(platform_audit_service, "log", _log)
        assignment_provider.register_provider(_Member())
        world.seed_agent(LEAD, owner=W.ADMIN)
        _apply(world, _manifest([MEMBER]), dry_run=False, user=_key(world))
        shares = [e for e in seen if e.get("event_action") == "share"]
        assert len(shares) == 1
        assert shares[0]["actor_user"].username == W.ADMIN
        assert shares[0]["details"]["via"] == "apply_member_seat"
        assert "@" not in repr(shares[0]["details"])

    @pytest.mark.parametrize("extra,named", [
        ({"entitlement": "seat-module"}, "`seat-module` entitlement"),
        ({}, "module's entitlement"),
        ({"entitlement": "not an id!"}, "module's entitlement"),
    ], ids=["named", "unnamed", "malformed_name"])
    def test_an_unentitled_module_is_named_not_unobserved(self, world, extra, named):
        """Plan T7 / gap 5: the dry run says which entitlement is missing —
        the provider's own name for it; OSS names none."""
        p = _Member(state="entitlement_missing", extra=extra)
        assignment_provider.register_provider(p)
        world.seed_agent(LEAD, owner=W.ADMIN)
        res = _apply(world, _manifest([MEMBER]), dry_run=True, user=_key(world))
        m = _m(res)
        assert _person_changes(m) == [("report", "entitlement_missing")]
        note = next(c.note for c in m.changes if c.field == "person_seats")
        assert named in note
        assert res.blockers == []
        real = _apply(world, _manifest([MEMBER]), dry_run=False, user=_key(world))
        assert _writes(p) == [] and real.failed == []

    def test_a_landed_share_broadcasts_agent_shared_with_identifiers_only(
            self, world, monkeypatch):
        """Plan E5 / gap 2: the share route's own event, so open panels and
        every worker's roster refresh; no address on the unfiltered /ws."""
        import json

        from routers import sharing
        sent = []

        class _Mgr:
            async def broadcast(self, msg):
                sent.append(json.loads(msg))
        monkeypatch.setattr(sharing, "manager", _Mgr())
        assignment_provider.register_provider(_Member())
        world.seed_agent(LEAD, owner=W.ADMIN)
        res = _apply(world, _manifest([MEMBER]), dry_run=False, user=_key(world))
        assert res.status == "applied", res.failed
        assert [e for e in sent if e.get("event") == "agent_shared"] == [
            {"event": "agent_shared", "data": {"name": LEAD}}]

    def test_a_seat_without_a_new_share_broadcasts_nothing(self, world, monkeypatch):
        import json

        from routers import sharing
        sent = []

        class _Mgr:
            async def broadcast(self, msg):
                sent.append(json.loads(msg))

        class _SeatOnly(_Member):
            def seat_member(self, agent_name, role_id, kind, created_by):
                self.calls.append(("seat_member", agent_name, role_id, kind, created_by))
                return {"changed": True, "shared": False}
        monkeypatch.setattr(sharing, "manager", _Mgr())
        assignment_provider.register_provider(_SeatOnly(state="partial"))
        world.seed_agent(LEAD, owner=W.ADMIN)
        _apply(world, _manifest([MEMBER]), dry_run=False, user=_key(world))
        assert [e for e in sent if e.get("event") == "agent_shared"] == []


class TestDeployIgnoresHolderMember:
    """Plan E6 / gap 4 (review I3): create mode (`deploy_system`, which an
    agent key reaches through `agents.manage`) never reads or writes the
    `holder: member` seat — only apply does. The manifest declares a `seat:`
    too, so create mode's seat phase runs and is the code under test."""

    @pytest.fixture(autouse=True)
    def _started(self, monkeypatch):
        async def started(names):
            return {n: "started" for n in names}
        monkeypatch.setattr(system_service, "start_all_agents", started)

    @pytest.mark.parametrize("who", ["admin_session", "admin_user_key", "agent_key"])
    def test_deploy_never_seats_the_member(self, world, who):
        user = {"admin_session": lambda: world.user(W.ADMIN),
                "admin_user_key": lambda: _key(world),
                "agent_key": lambda: _key(world, scope="agent",
                                          agent_name="orchestrator")}[who]()
        p = _Member()
        assignment_provider.register_provider(p)
        for dry_run in (True, False):
            res = asyncio.run(system_service.deploy_manifest(
                _manifest([MEMBER], seat="lead-role"), user, None, dry_run=dry_run,
                create_agent_fn=world.create))
            assert res.status == ("valid" if dry_run else "deployed"), (res.failed, res.warnings)
        assert [c.name for c in world.create_calls] == [LEAD]
        assert [c for c in p.calls if c[0] in ("seat_member", "member_seat_status")] == []


class TestRegressions:
    def test_the_agent_seat_stays_interactive_admin_only_under_an_admin_key(self, world):
        p = _Member()
        assignment_provider.register_provider(p)
        world.seed_agent(LEAD, owner=W.ADMIN)
        res = _apply(world, _manifest([MEMBER], seat="lead-role"), dry_run=False,
                     user=_key(world))
        codes = [(c.field, c.code) for c in _m(res).changes]
        assert ("seat", "seat_requires_interactive_admin") in codes
        assert [c for c in p.calls if c[0] == "set_seat"] == []
        assert len(_writes(p)) == 1

    def test_other_person_seats_stay_report_only(self, world):
        p = _Member()
        assignment_provider.register_provider(p)
        world.seed_agent(LEAD, owner=W.ADMIN)
        res = _apply(world, _manifest([{"role": "reviewer", "kind": "viewer"}]),
                     dry_run=False, user=world.user(W.ADMIN))
        assert _person_changes(_m(res)) == [("report", "seat_vacant")]
        assert _writes(p) == []

    def test_an_agent_key_is_refused_before_planning(self, world):
        """ent#695 unchanged: apply is person-credential only (the route's
        `require_person`); `can_seat_member` refuses the agent key too."""
        from dependencies import can_seat_member
        assert can_seat_member(_key(world, scope="agent", agent_name="orchestrator")) is False


class TestSeamCarriesNoEmail:
    def test_the_member_seat_hooks_take_no_person(self):
        """S1: the seam hook takes NO email; the provider resolves who."""
        params = inspect.signature(assignment_provider.write_member_seat).parameters
        assert list(params) == ["agent_name", "role_id", "kind", "created_by"]
        params = inspect.signature(assignment_provider.resolve_member_seat).parameters
        assert list(params) == ["agent_name", "role_id", "kind"]

    def test_a_malformed_write_answer_is_an_error_not_a_success(self, world):
        class _Bad(_Member):
            def seat_member(self, *a):
                return "yes"
        assignment_provider.register_provider(_Bad())
        with pytest.raises(ValueError):
            assignment_provider.write_member_seat(LEAD, "digest-reader", "primary", "x")
