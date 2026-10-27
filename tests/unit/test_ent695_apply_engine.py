"""trinity-enterprise#695: apply mode, the engine (`services/system_service.py`).

Every case runs over the REAL `db` on a full schema (`db_backend`), seeded with
the real writers; Docker is faked at its read seams only (`_ent695_world.py`).
Sections follow the build order: snapshot + gates, the schedule matcher, the
field reconcilers, the executor.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend, run as _hrun  # noqa: E402,F401

pytest.importorskip("docker", reason="backend venv required (system_service chain)")

sys.path.insert(0, str(Path(__file__).resolve().parent))  # `_ent695_world`, as `_route_census`

import _ent695_world as W  # noqa: E402
from database import db  # noqa: E402
from services import system_service  # noqa: E402

pytestmark = pytest.mark.unit

SYSTEM = W.SYSTEM


@pytest.fixture
def world(db_backend, monkeypatch, tmp_path):
    return W.make_world(monkeypatch, tmp_path)


def _manifest(*shorts: str, extra: str = "") -> str:
    agents = "\n".join(f"  {s}:\n    template: local:plain" for s in shorts)
    return f"name: {SYSTEM}\nagents:\n{agents}\n{extra}"


def _codes(snapshot):
    return [(b.code, b.member) for b in snapshot.blockers]


def _blocker(snapshot, code):
    found = [b for b in snapshot.blockers if b.code == code]
    assert found, f"no {code} blocker in {_codes(snapshot)}"
    return found


# ===========================================================================
# 1. Snapshot + gates (AC5, H1, the roster)
# ===========================================================================

class TestEvidenceGate:
    """AC5, literally: weak membership evidence is refused by name."""

    def test_a_declared_untagged_member_is_refused_with_the_additive_remedy(self, world):
        world.seed_agent("acme-a", tags=())
        snap = world.snapshot(_manifest("a", "b"))

        (b,) = _blocker(snap, "membership_evidence_prefix_only")
        assert b.member == "acme-a" and b.retryable is False
        assert "POST /api/agents/acme-a/tags/acme" in b.remedy
        assert "not `PUT" in b.remedy  # PUT replaces the whole set
        assert snap.members["a"].blocked is True

    def test_an_undeclared_untagged_member_is_refused_too(self, world):
        """The issue names `evidence == "prefix"` without restricting it to
        declared members; the AC is the bar (Codex 5)."""
        world.seed_agent("acme-notes", tags=())
        snap = world.snapshot(_manifest("a"))

        (b,) = _blocker(snap, "membership_evidence_prefix_only")
        assert b.member == "acme-notes"
        assert "belongs to another system" in b.remedy and "rename" in b.remedy.lower()

    def test_unreadable_tags_are_retryable(self, world, monkeypatch):
        world.seed_agent("acme-a")

        def _boom(names):
            raise RuntimeError("tag table locked")

        monkeypatch.setattr(db, "get_tags_for_agents", _boom)
        snap = world.snapshot(_manifest("a"))

        (b,) = _blocker(snap, "membership_tags_unreadable")
        assert b.retryable is True
        # Nothing to narrow with: every member would read as prefix. The one
        # blocker says why; it is not repeated per member.
        assert not [x for x in snap.blockers if x.code == "membership_evidence_prefix_only"]

    def test_unreadable_docker_is_retryable_never_an_empty_roster(self, world):
        """The ONLY blocker: an existing member's container is unknown, not
        missing, so no non-retryable `member_container_missing` turns the
        503 into a 409 (review I1)."""
        world.seed_agent("acme-a")
        world.docker.readable = False
        snap = world.snapshot(_manifest("a", "b"))

        assert [(b.code, b.retryable) for b in snap.blockers] == [("roster_unreadable", True)]

    def test_a_label_read_returning_nothing_is_a_read_failure_not_a_missing_container(self, world):
        """`get_agent_container` turns every Docker error into None; a name the
        tri-state roster already showed is unreadable, not absent (Codex B-AC5)."""
        world.seed_agent("acme-a")
        world.docker.unreadable_labels.add("acme-a")
        snap = world.snapshot(_manifest("a"))

        (b,) = _blocker(snap, "member_reads_unavailable")
        assert b.member == "acme-a" and b.retryable is True
        assert "member_container_missing" not in [c for c, _ in _codes(snap)]


class TestNameClassification:
    """H1: classified globally, never from the caller's roster; one uniform
    refusal for anything the caller may not manage."""

    def _unavailable_detail(self, world, setup) -> tuple:
        setup()
        snap = world.snapshot(_manifest("api"))
        (b,) = _blocker(snap, "member_name_unavailable")
        assert b.member == "acme-api"
        assert snap.members["api"].status == "unavailable"
        return b.detail, b.remedy

    @pytest.mark.parametrize("case", ["other_users_agent", "other_users_system_agent",
                                      "soft_deleted_name", "volume_reserved_name",
                                      "container_nobody_owns"])
    def test_every_unmanageable_name_gets_the_same_code_and_text(self, world, case):
        def other_users_agent():
            world.seed_agent("acme-api", owner=W.BOB)

        def other_users_system_agent():
            db.register_agent_owner("acme-api", W.BOB, is_system=True)
            world.docker.add("acme-api")

        def soft_deleted_name():
            world.seed_agent("acme-api", owner=W.BOB, container=False)
            db.delete_agent_ownership("acme-api")

        def volume_reserved_name():
            world.seed_agent("acme-renamed", owner=W.BOB)
            _hrun("UPDATE agent_ownership SET volume_base_name = 'acme-api' "
                  "WHERE agent_name = 'acme-renamed'")

        def container_nobody_owns():
            world.docker.add("acme-api")

        detail, remedy = self._unavailable_detail(world, locals()[case])
        assert detail == "The agent name 'acme-api' is in use or reserved by an agent you cannot manage."
        assert remedy == system_service._NAME_UNAVAILABLE_REMEDY
        assert W.BOB not in detail + remedy  # the owner is never named

    def test_a_shared_agent_the_caller_can_see_but_not_manage_is_unavailable(self, world):
        world.seed_agent("acme-api", owner=W.BOB)
        db.share_agent("acme-api", W.BOB, f"{W.ALICE}@example.com")
        assert db.can_user_access_agent(W.ALICE, "acme-api")
        snap = world.snapshot(_manifest("api"))
        assert [c for c, _ in _codes(snap)] == ["member_name_unavailable"]

    def test_existence_comes_from_the_owner_row_not_the_callers_roster(self, world):
        """Alice cannot see Bob's agent, so it is not in her roster; it is still
        taken, never 'absent -> create' (which would then suffix)."""
        world.seed_agent("acme-api", owner=W.BOB)
        snap = world.snapshot(_manifest("api"))
        assert "acme-api" not in snap.roster
        assert snap.members["api"].status == "unavailable"

    def test_an_admin_may_manage_another_users_member(self, world):
        world.seed_agent("acme-api", owner=W.BOB)
        snap = world.snapshot(_manifest("api"), username=W.ADMIN)
        assert snap.members["api"].status == "managed" and not snap.blockers
        assert snap.members["api"].owner_username == W.BOB

    def test_a_free_name_is_absent(self, world):
        snap = world.snapshot(_manifest("api"))
        assert snap.members["api"].status == "absent" and not snap.blockers


class TestManagedMemberGates:
    """Only for agents the caller may manage: the specific codes."""

    def test_a_system_agent(self, world):
        db.register_agent_owner("acme-api", W.ALICE, is_system=True)
        db.set_agent_tags("acme-api", [SYSTEM])
        world.docker.add("acme-api")
        assert [c for c, _ in _codes(world.snapshot(_manifest("api")))] == ["member_is_system_agent"]

    def test_an_ephemeral_agent(self, world):
        db.register_agent_owner("acme-api", W.ALICE, is_ephemeral=True,
                                ephemeral_max_executions=3,
                                ephemeral_expires_at="2026-10-28T00:00:00Z")
        db.set_agent_tags("acme-api", [SYSTEM])
        world.docker.add("acme-api")
        assert [c for c, _ in _codes(world.snapshot(_manifest("api")))] == ["member_is_ephemeral"]

    def test_a_live_row_without_a_container(self, world):
        world.seed_agent("acme-api", container=False)
        assert [c for c, _ in _codes(world.snapshot(_manifest("api")))] == ["member_container_missing"]

    def test_a_member_another_system_claims(self, world):
        """`acme-extra-worker` tagged `acme-extra` belongs to system acme-extra;
        declaring `extra-worker` under acme does not take it."""
        world.seed_agent("acme-extra-worker", tags=("acme-extra",))
        snap = world.snapshot(_manifest("extra-worker"))
        assert [c for c, _ in _codes(snap)] == ["member_claimed_by_another_system"]

    def test_a_clean_member_is_read_for_the_plan(self, world):
        world.seed_agent("acme-api", tags=(SYSTEM, "prod"), cpu="4", template="local:plain")
        db.add_agent_permission("acme-api", "acme-web", W.ALICE)
        db.set_resource_limits("acme-api", memory="8g", cpu=None)
        snap = world.snapshot(_manifest("api"))
        state = snap.members["api"]
        assert not snap.blockers and state.status == "managed"
        assert state.tags == ["acme", "prod"] and state.edges == ["acme-web"]
        assert state.labels["trinity.cpu"] == "4"
        assert state.resource_limits == {"memory": "8g", "cpu": None}
        # A stored override the container does not run: the platform's own
        # predicate says a restart is pending.
        assert state.restart_pending is True


class TestRoster:
    """Engineering F1 + Codex 6: containers UNION live ownership rows,
    filtered by access; never `list_all_agents_fast` (the world makes it raise)."""

    def test_a_live_row_without_a_container_is_a_member(self, world):
        world.seed_agent("acme-restored", container=False)
        snap = world.snapshot(_manifest("a"))
        assert "acme-restored" in snap.roster
        assert snap.membership.evidence.get("acme-restored") == "both"

    def test_an_untagged_declared_member_is_refused_without_list_all_agents_fast(self, world):
        world.seed_agent("acme-a", tags=())
        snap = world.snapshot(_manifest("a"))
        assert ("membership_evidence_prefix_only", "acme-a") in _codes(snap)

    def test_an_unreadable_docker_is_seen_through_the_tri_state_reader_only(self, world):
        """`get_accessible_agents` answers `[]` when Docker cannot be asked, which
        would read as "no containers"; the tri-state reader answers None."""
        world.seed_agent("acme-a")
        world.docker.readable = False
        assert ("roster_unreadable", None) in _codes(world.snapshot(_manifest("a")))

    def test_names_the_caller_cannot_access_are_not_in_the_roster(self, world):
        world.seed_agent("acme-bobs", owner=W.BOB)
        snap = world.snapshot(_manifest("a"))
        assert "acme-bobs" not in snap.roster
        assert "acme-bobs" not in snap.membership.members


# ===========================================================================
# 2. The schedule matcher (H3, section 7: the ent#694 seam). Pure.
# ===========================================================================

def _row(name, *, cron="0 9 * * *", message="/daily", enabled=True, timezone="UTC",
         description=None, owner_id=1, sid=None):
    """A real `db_models.Schedule`, never a mock."""
    from datetime import datetime, timezone as _tz

    from db_models import Schedule
    pinned = datetime(2026, 10, 1, tzinfo=_tz.utc)
    return Schedule(id=sid or f"sid-{name}", agent_name="acme-a", name=name,
                    cron_expression=cron, message=message, enabled=enabled,
                    timezone=timezone, description=description, owner_id=owner_id,
                    created_at=pinned, updated_at=pinned)


def _kinds(actions):
    return [(a.kind, a.name) for a in actions]


class TestScheduleMatcher:
    resolve = staticmethod(system_service.resolve_declared_schedule_actions)

    def test_a_matched_row_updates_only_the_declared_fields_that_differ(self):
        live = [_row("daily", timezone="Europe/Berlin", description="from the template")]
        (action,) = self.resolve([{"name": "daily", "cron": "0 10 * * *", "message": "/daily"}], live)
        assert action.kind == "update"
        # An omitted timezone / description keeps the row's (engineering F2).
        assert action.fields == {"cron_expression": "0 10 * * *"}
        assert action.current == {"cron_expression": "0 9 * * *"}

    def test_declared_timezone_and_description_are_compared_when_present(self):
        live = [_row("daily")]
        (action,) = self.resolve([{"name": "daily", "cron": "0 9 * * *", "message": "/daily",
                                   "timezone": "Europe/London", "description": "d"}], live)
        assert action.fields == {"timezone": "Europe/London", "description": "d"}

    def test_an_unchanged_row_is_in_sync(self):
        assert _kinds(self.resolve([{"name": "daily", "cron": "0 9 * * *", "message": "/daily"}],
                                   [_row("daily")])) == [("in_sync", "daily")]

    def test_enabled_is_never_a_write_only_a_report(self):
        """A template row left disabled under a manifest whose entry defaults
        to armed: reported, never armed (#1198 §5)."""
        actions = self.resolve([{"name": "daily", "cron": "0 9 * * *", "message": "/daily"}],
                               [_row("daily", enabled=False)])
        assert _kinds(actions) == [("in_sync", "daily"), ("enabled_differs", "daily")]
        assert "enabled" not in actions[0].fields

    def test_an_undeclared_row_is_reported_and_left(self):
        assert ("undeclared", "weekly") in _kinds(self.resolve([], [_row("weekly")]))

    def test_two_live_rows_with_one_name_are_ambiguous(self):
        actions = self.resolve([{"name": "daily", "cron": "0 9 * * *", "message": "/daily"}],
                               [_row("daily", sid="s1"), _row("daily", sid="s2")])
        assert _kinds(actions) == [("ambiguous", "daily")]

    def test_one_name_declared_twice(self):
        entry = {"name": "daily", "cron": "0 9 * * *", "message": "/daily"}
        assert _kinds(self.resolve([entry, dict(entry)], [])) == [("declared_twice", "daily")]

    def test_a_rename_is_a_create_and_the_old_row_reported_still_armed(self):
        actions = self.resolve([{"name": "morning", "cron": "0 9 * * *", "message": "/daily"}],
                               [_row("daily", enabled=True)])
        assert _kinds(actions) == [("create", "morning"), ("undeclared", "daily")]
        assert actions[1].row.enabled is True

    def test_the_default_policy_never_produces_unlinked(self):
        entry = {"name": "daily", "cron": "0 9 * * *", "message": "/daily"}
        for live in ([], [_row("daily")], [_row("daily", sid="a"), _row("daily", sid="b")]):
            assert system_service.schedule_match_policy(entry, live).kind != "unlinked"

    def test_an_identity_policy_plugs_in_without_touching_the_writers(self):
        """ent#694 / #1198: declared id first; the same name but no id link is
        `unlinked` — reported, never adopted. Only the policy changes."""
        def by_declared_id(entry, rows):
            same_id = [r for r in rows if r.id == entry.get("id")]
            if same_id:
                return system_service.ScheduleMatch("matched", row=same_id[0])
            if any(r.name == entry["name"] for r in rows):
                return system_service.ScheduleMatch("unlinked",
                                                    row=next(r for r in rows if r.name == entry["name"]))
            return system_service.ScheduleMatch("missing")

        actions = self.resolve([{"name": "daily", "cron": "0 9 * * *", "message": "/daily", "id": "x"}],
                               [_row("daily", sid="y")], match=by_declared_id)
        assert _kinds(actions) == [("unlinked", "daily")]


# ===========================================================================
# 3. The plan: field reconcilers, drift, prompt, view, census
# ===========================================================================

def _plan(world, manifest_yaml, username=W.ALICE):
    manifest = system_service.parse_manifest(manifest_yaml)
    system_service.validate_manifest(manifest)
    snap = world.snapshot(manifest_yaml, username=username)
    return system_service.resolve_apply_plan(manifest, snap)


def _member(plan, short):
    return plan.members[short]


def _ops(mp, kind=None):
    return [(op.kind, op.args) for op in mp.ops if kind is None or op.kind == kind]


def _change_codes(mp):
    return [(c.field, c.action, c.code) for c in mp.changes]


def _one_member(world, entry: str, **seed):
    world.seed_agent("acme-a", **seed)
    return f"name: {SYSTEM}\nagents:\n  a:\n    template: local:plain\n{entry}"


class TestTagsAndEdges:
    def test_tags_are_added_never_replaced_and_compared_as_stored(self, world):
        """`Prod` declared, `prod` stored: equal (AC1). An operator tag stays
        and is not reported (H2)."""
        world.seed_agent("acme-a", tags=(SYSTEM, "prod", "operator-pick"))
        plan = _plan(world, f"name: {SYSTEM}\ndefault_tags: [Prod]\nagents:\n"
                            "  a:\n    template: local:plain\n    tags: [team-x]\n")
        mp = _member(plan, "a")
        assert _ops(mp) == [("tag", {"tag": "team-x"})]
        assert mp.verdict == "update" and not plan.blockers
        assert "operator-pick" not in str([c.desired for c in mp.changes])

    def test_missing_edges_are_added_and_extras_reported_never_removed(self, world):
        """`none` would CLEAR every edge in create mode; apply adds nothing and
        reports the intra-system edge, and never reads an edge to a non-member."""
        world.seed_agent("acme-a")
        world.seed_agent("acme-b")
        db.add_agent_permission("acme-a", "acme-b", W.ALICE)
        db.add_agent_permission("acme-a", "outsider", W.ALICE)
        plan = _plan(world, _manifest("a", "b", extra="permissions:\n  preset: none\n"))
        mp = _member(plan, "a")
        assert _ops(mp, "edge") == []
        assert [(c.code, c.current) for c in mp.changes] == [("permission_extra", ["acme-b"])]
        assert mp.verdict == "unchanged"

    def test_an_explicit_self_edge_is_dropped_like_set_permissions_drops_it(self, world):
        world.seed_agent("acme-a")
        world.seed_agent("acme-b")
        plan = _plan(world, _manifest("a", "b", extra="permissions:\n  explicit:\n    a: [a, b]\n"))
        assert _ops(_member(plan, "a"), "edge") == [("edge", {"target": "acme-b"})]

    def test_full_mesh_adds_only_what_is_missing(self, world):
        world.seed_agent("acme-a")
        world.seed_agent("acme-b")
        db.add_agent_permission("acme-a", "acme-b", W.ALICE)
        plan = _plan(world, _manifest("a", "b", extra="permissions:\n  preset: full-mesh\n"))
        assert _ops(_member(plan, "a"), "edge") == []
        assert _ops(_member(plan, "b"), "edge") == [("edge", {"target": "acme-a"})]


class TestFolders:
    def test_only_the_declared_key_is_compared_and_written(self, world):
        mf = _one_member(world, "    folders:\n      expose: true\n")
        db.upsert_shared_folder_config("acme-a", expose_enabled=False, consume_enabled=True)
        mp = _member(_plan(world, mf), "a")
        assert _ops(mp, "folders") == [("folders", {"expose_enabled": True, "consume_enabled": None})]
        assert mp.restart_required is True  # the mounts no longer match the config

    @pytest.mark.parametrize("folders", ["{expose: null}", "{expose: 'yes'}", "{share: true}"])
    def test_invalid_folders_are_refused(self, world, folders):
        mf = _one_member(world, f"    folders: {folders}\n")
        plan = _plan(world, mf)
        assert [(b.code, b.member) for b in plan.blockers] == [("folders_invalid", "acme-a")]
        assert _member(plan, "a").verdict == "blocked"


class TestResources:
    def test_a_changed_cpu_is_an_override_keeping_the_stored_memory(self, world):
        mf = _one_member(world, "    resources:\n      cpu: '4'\n")
        db.set_resource_limits("acme-a", memory="8g", cpu=None)
        mp = _member(_plan(world, mf), "a")
        assert _ops(mp, "resources") == [("resources", {"cpu": "4", "memory": "8g"})]
        assert mp.restart_required is True and mp.verdict == "update"

    def test_the_effective_value_is_override_then_label_then_default(self, world):
        mf = _one_member(world, "    resources:\n      cpu: '4'\n", cpu="4")
        assert _ops(_member(_plan(world, mf), "a")) == []

    def test_a_stored_override_not_yet_applied_keeps_reporting_restart_required(self, world):
        """Codex pass 3: computed from the platform's predicates over the
        container every run, never from this run's writes."""
        mf = _one_member(world, "    resources:\n      cpu: '4'\n", cpu="2")
        db.set_resource_limits("acme-a", memory=None, cpu="4")
        mp = _member(_plan(world, mf), "a")
        assert _ops(mp) == [] and mp.verdict == "unchanged" and mp.restart_required is True
        world.docker.containers["acme-a"].labels["trinity.cpu"] = "4"  # restarted
        assert _member(_plan(world, mf), "a").restart_required is False

    @pytest.mark.parametrize("value", ["1.0", "'0.5'", "'512Mi'"])
    def test_an_unusable_value_is_refused_with_creates_own_validator(self, world, value):
        key = "memory" if "Mi" in value else "cpu"
        plan = _plan(world, _one_member(world, f"    resources:\n      {key}: {value}\n"))
        assert [b.code for b in plan.blockers] == ["resources_invalid"]


class TestTemplate:
    def test_a_different_template_is_reported_never_recreated(self, world):
        world.seed_agent("acme-a", template="local:old")
        mp = _member(_plan(world, _manifest("a")), "a")
        assert _change_codes(mp) == [("template", "report", "template_differs")]
        assert mp.verdict == "unchanged" and mp.ops == []


class TestSchedulesInThePlan:
    def test_a_row_apply_creates_on_an_existing_member_is_owned_by_the_agent_owner(self, world):
        """H3b: an admin applying to Alice's agent must not lock her out of the
        row (`update_schedule` is owner-or-admin)."""
        mf = _one_member(world, "    schedules:\n      - {name: daily, cron: '0 9 * * *', message: /d}\n")
        mp = _member(_plan(world, mf, username=W.ADMIN), "a")
        (op,) = [op for op in mp.ops if op.kind == "schedule_create"]
        assert op.args["owner_username"] == W.ALICE

    def test_a_row_the_caller_cannot_update_is_refused_up_front(self, world):
        mf = _one_member(world, "    schedules:\n      - {name: daily, cron: '0 10 * * *', message: /d}\n")
        db.create_schedule("acme-a", W.ADMIN, system_service._build_schedule_create(
            {"name": "daily", "cron": "0 9 * * *", "message": "/d"}))
        plan = _plan(world, mf)
        assert [(b.code, b.member) for b in plan.blockers] == [
            ("schedule_not_updatable_by_caller", "acme-a")]

    @pytest.mark.parametrize("entry,code", [
        ("{name: d, cron: '99 99 * * *', message: /d}", "schedule_cron_invalid"),
        ("{name: d, cron: '@daily', message: /d}", "schedule_cron_invalid"),
        ("{name: d, cron: '0 9 * * *', message: /d, timezone: Mars/Olympus}", "schedule_timezone_invalid"),
        ("{name: d, cron: '0 9 * * *', message: [not, a, string]}", "schedule_definition_invalid"),
    ])
    def test_an_invalid_schedule_is_refused_before_any_write(self, world, entry, code):
        plan = _plan(world, _one_member(world, f"    schedules:\n      - {entry}\n"))
        assert [b.code for b in plan.blockers] == [code]

    def test_an_invalid_cron_on_an_update_is_refused(self, world):
        mf = _one_member(world, "    schedules:\n      - {name: daily, cron: '61 9 * * *', message: /d}\n")
        db.create_schedule("acme-a", W.ALICE, system_service._build_schedule_create(
            {"name": "daily", "cron": "0 9 * * *", "message": "/d"}))
        assert [b.code for b in _plan(world, mf).blockers] == ["schedule_cron_invalid"]

    def test_two_live_rows_with_one_name_refuse(self, world):
        mf = _one_member(world, "    schedules:\n      - {name: daily, cron: '0 9 * * *', message: /d}\n")
        for _ in range(2):
            db.create_schedule("acme-a", W.ALICE, system_service._build_schedule_create(
                {"name": "daily", "cron": "0 9 * * *", "message": "/d"}))
        assert [b.code for b in _plan(world, mf).blockers] == ["schedule_name_ambiguous"]


class TestCreatedMemberPlan:
    def test_a_member_to_create_marks_its_template_dependent_fields_pending(self, world):
        mf = (f"name: {SYSTEM}\nagents:\n  a:\n    template: local:sched\n"
              "    resources: {cpu: '2'}\n"
              "    schedules:\n      - {name: daily, cron: '0 7 * * *', message: /m}\n")
        mp = _member(_plan(world, mf), "a")
        assert mp.verdict == "create" and mp.creating
        pending = [c.field for c in mp.changes if c.code == "pending_template_resolution"]
        assert pending == ["resources", "schedule:daily"]

    def test_an_unresolvable_local_template_is_refused(self, world):
        plan = _plan(world, f"name: {SYSTEM}\nagents:\n  a:\n    template: local:nope\n")
        assert [b.code for b in plan.blockers] == ["template_unresolvable"]


class TestDrift:
    def test_undeclared_tagged_members_are_drift_with_the_suffix_note(self, world):
        world.seed_agent("acme-a")
        world.seed_agent("acme-old")
        world.seed_agent("acme-a_2")
        world.seed_agent("acme-restored", container=False)
        plan = _plan(world, _manifest("a"))
        drift = {d.name: d for d in plan.drift}
        assert sorted(drift) == ["acme-a_2", "acme-old", "acme-restored"]
        assert "suffixed copy" in drift["acme-a_2"].note and drift["acme-old"].note is None
        assert not plan.blockers


class TestPromptAndView:
    def test_prompt_verdicts(self, world):
        db.set_setting("trinity_prompt", "current")
        base = _manifest("a")
        assert _plan(world, base).prompt.verdict == "not_declared"
        assert _plan(world, base + 'prompt: ""\n').prompt.verdict == "not_declared"
        assert _plan(world, base + "prompt: current\n").prompt.verdict == "unchanged"
        creator = _plan(world, base + "prompt: new\n")
        assert creator.prompt.verdict == "requires_admin" and creator.prompt_op is None
        assert not creator.blockers  # a report, never a blocker
        admin = _plan(world, base + "prompt: new\n", username=W.ADMIN)
        assert admin.prompt.verdict == "update" and admin.prompt_op.args == {"prompt": "new"}

    def _view(self, owner, *, shared, name="Acme"):
        from db_models import SystemViewCreate
        user_id = str(db.get_user_by_username(owner)["id"])
        return db.create_system_view(user_id, SystemViewCreate(
            name=name, description=system_service.SYSTEM_VIEW_AUTO_DESCRIPTION.format(system_name=SYSTEM),
            filter_tags=[SYSTEM], is_shared=shared))

    def test_view_create_unchanged_and_ambiguous(self, world):
        mf = _manifest("a", extra="system_view:\n  name: Acme\n  shared: false\n")
        assert _plan(world, mf).system_view.verdict == "create"
        own = self._view(W.ALICE, shared=True, name="Old name")
        plan = _plan(world, mf)
        assert plan.system_view.verdict == "unchanged" and plan.system_view.id == own.id
        assert [c.field for c in plan.system_view.changes] == ["system_view.name", "system_view.shared"]
        assert plan.view_op is None
        self._view(W.ALICE, shared=False)
        plan = _plan(world, mf)
        assert plan.system_view.verdict == "ambiguous" and plan.view_op is None and plan.warnings

    def test_another_users_private_view_is_never_matched(self, world):
        """Engineering F8: the lookup is over the caller's own or shared views."""
        self._view(W.BOB, shared=False)
        assert _plan(world, _manifest("a", extra="system_view:\n  name: Acme\n")).system_view.verdict == "create"


class TestFieldCoverageCensus:
    """Strategy F4: a new manifest key fails CI until it is classified."""

    @staticmethod
    def _schedule_keys():
        import ast
        import inspect
        import textwrap
        tree = ast.parse(textwrap.dedent(inspect.getsource(system_service._build_schedule_create)))
        keys = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
                keys.add(node.slice.value)
            if (isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "get"
                    and node.args and isinstance(node.args[0], ast.Constant)):
                keys.add(node.args[0].value)
        return keys

    def test_every_field_is_classified_exactly_once(self):
        from models import SystemAgentConfig, SystemManifest
        expected = (
            {f"SystemManifest.{f}" for f in SystemManifest.model_fields}
            | {f"SystemAgentConfig.{f}" for f in SystemAgentConfig.model_fields}
            | {f"schedule.{k}" for k in self._schedule_keys()}
        )
        table = system_service.APPLY_FIELD_CLASSIFICATION
        assert set(table) == expected
        assert {cls for cls, _why in table.values()} <= system_service.APPLY_FIELD_CLASSES
        assert table["SystemAgentConfig.kind"][0] == "create-only"
        assert self._schedule_keys() == {"name", "cron", "message", "enabled", "timezone", "description"}


# ===========================================================================
# 4. The executor and the orchestrator: the AC rows end to end
# ===========================================================================

@pytest.fixture
def ew(world, monkeypatch):
    """The world plus the executor's seams (create, start, the lock's Redis)."""
    W.install_executor_seams(world, monkeypatch)
    return world


def _witness(world, tmp_path):
    return W.Witness(world, tmp_path)


FLEET = f"""\
name: {SYSTEM}
default_tags: [Prod]
permissions:
  explicit:
    a: [a, b]
agents:
  a:
    template: local:sched
    folders: {{expose: true}}
    schedules:
      - {{name: daily, cron: '0 7 * * *', message: /manifest-daily}}
      - {{name: weekly, cron: '0 9 * * MON', message: /weekly}}
  b:
    template: local:heavy
    resources: {{cpu: '2'}}
"""


def _verdicts(result):
    return {m.short_name: m.verdict for m in result.members}


def _sched(name, agent="acme-a"):
    (row,) = [s for s in db.list_agent_schedules(agent) if s.name == name]
    return row


class TestAC1ApplyTwiceIsANoOp:
    def test_fresh_install_then_apply_again_writes_nothing(self, ew, tmp_path):
        """The template declares a same-named schedule, a `Prod` tag, a
        schedule with no timezone, an explicit self-edge, partial folders,
        and a template that overrides a declared resource."""
        first = W.apply(ew, FLEET, dry_run=False)
        assert first.status == "applied", first.failed
        assert _verdicts(first) == {"a": "create", "b": "create"}

        with _witness(ew, tmp_path) as w:
            second = W.apply(ew, FLEET, dry_run=False)
        assert second.status == "applied"
        assert _verdicts(second) == {"a": "unchanged", "b": "unchanged"}
        assert w.fleet_statements == []
        assert w.creates == [] and w.docker_events == []
        assert w.changed_tables() == []

    @pytest.mark.parametrize("writer,change", [
        ("schedule", ("cron: '0 9 * * MON'", "cron: '0 8 * * MON'")),
        ("tag", ("    resources: {cpu: '2'}", "    resources: {cpu: '2'}\n    tags: [team-x]")),
        ("edge", ("    a: [a, b]", "    a: [a, b]\n    b: [a]")),
        ("folder", ("folders: {expose: true}", "folders: {expose: true, consume: true}")),
        ("resource", ("resources: {cpu: '2'}", "resources: {cpu: '2', memory: '16g'}")),
        ("prompt", ("default_tags: [Prod]", "default_tags: [Prod]\nprompt: be brief")),
        ("view", ("default_tags: [Prod]", "default_tags: [Prod]\nsystem_view: {name: Acme}")),
    ])
    def test_each_writer_converges_after_one_change(self, ew, tmp_path, writer, change):
        """Codex B-AC1: change one declared value -> apply -> apply again ->
        zero fleet-state statements, for every writer."""
        assert W.apply(ew, FLEET, W.ADMIN, dry_run=False).status == "applied"
        changed = FLEET.replace(*change)
        assert changed != FLEET
        updated = W.apply(ew, changed, W.ADMIN, dry_run=False)
        assert updated.status == "applied", updated.failed
        assert "update" in _verdicts(updated).values() or writer in ("prompt", "view")
        with _witness(ew, tmp_path) as w:
            again = W.apply(ew, changed, W.ADMIN, dry_run=False)
        assert set(_verdicts(again).values()) == {"unchanged"}
        assert w.fleet_statements == [], w.fleet_statements
        assert again.prompt.verdict in ("not_declared", "unchanged")
        assert again.system_view.verdict in ("not_requested", "unchanged")


class TestAC2AChangedFieldUpdatesInPlace:
    def test_cron_message_tag_folder_and_cpu_update_the_existing_agent(self, ew):
        assert W.apply(ew, FLEET, dry_run=False).status == "applied"
        daily_id = _sched("daily").id
        changed = (FLEET.replace("'0 7 * * *', message: /manifest-daily", "'0 6 * * *', message: /new")
                        .replace("folders: {expose: true}", "folders: {expose: false}")
                        .replace("resources: {cpu: '2'}", "resources: {cpu: '8'}\n    tags: [team-x]"))
        db.set_resource_limits("acme-b", memory="16g", cpu="2")  # an override the manifest omits

        result = W.apply(ew, changed, dry_run=False)

        assert result.status == "applied", result.failed
        assert _verdicts(result) == {"a": "update", "b": "update"}
        daily = _sched("daily")
        assert daily.id == daily_id and daily.cron_expression == "0 6 * * *" and daily.message == "/new"
        assert daily.enabled is False  # the template's state; never written
        a = next(m for m in result.members if m.short_name == "a")
        assert ("schedule:daily", "report", "enabled_differs") in [
            (c.field, c.action, c.code) for c in a.changes]
        assert not db.get_agent_owner("acme-a_2") and not db.get_agent_owner("acme-b_2")
        assert db.get_shared_folder_config("acme-a").expose_enabled is False
        assert "team-x" in db.get_agent_tags("acme-b")
        assert db.get_resource_limits("acme-b") == {"memory": "16g", "cpu": "8"}
        b = next(m for m in result.members if m.short_name == "b")
        assert b.restart_required is True
        assert [e for e in ew.docker.events if e[1] in ("acme-a", "acme-b")
                and e[0] != "start"] == []
        # Existing members are never started by this run.
        assert ew.docker.events.count(("start", "acme-b")) == 1  # only the first run's

    def test_an_omitted_timezone_and_description_survive_an_update(self, ew):
        """Engineering F2: a row on Europe/Berlin with a template description,
        under a manifest entry that omits both, keeps both."""
        ew.seed_agent("acme-a")
        db.create_schedule("acme-a", W.ALICE, system_service._build_schedule_create(
            {"name": "daily", "cron": "0 9 * * *", "message": "/d",
             "timezone": "Europe/Berlin", "description": "the template's purpose"}))
        mf = (f"name: {SYSTEM}\nagents:\n  a:\n    template: local:plain\n"
              "    schedules:\n      - {name: daily, cron: '0 10 * * *', message: /d}\n")
        assert W.apply(ew, mf, dry_run=False).status == "applied"
        row = _sched("daily")
        assert (row.cron_expression, row.timezone, row.description) == (
            "0 10 * * *", "Europe/Berlin", "the template's purpose")


class TestAC3DriftIsReportedAndUntouched:
    def test_undeclared_members_are_drift_and_nothing_touches_them(self, ew, tmp_path):
        ew.seed_agent("acme-a")
        ew.seed_agent("acme-old")
        ew.seed_agent("acme-a_2")
        ew.seed_agent("acme-restored", container=False)
        for name in ("acme-old", "acme-a_2"):
            db.create_schedule(name, W.ALICE, system_service._build_schedule_create(
                {"name": "s", "cron": "0 9 * * *", "message": "/s"}))
        before = {n: (db.get_agent_owner(n), db.get_agent_tags(n),
                      [s.id for s in db.list_agent_schedules(n)])
                  for n in ("acme-old", "acme-a_2", "acme-restored")}

        result = W.apply(ew, _manifest("a"), dry_run=False)

        assert result.status == "applied"
        drift = {d.name: d.note for d in result.drift}
        assert sorted(drift) == ["acme-a_2", "acme-old", "acme-restored"]
        assert "suffixed copy" in drift["acme-a_2"]
        after = {n: (db.get_agent_owner(n), db.get_agent_tags(n),
                     [s.id for s in db.list_agent_schedules(n)]) for n in before}
        assert after == before
        assert [e for e in ew.docker.events if e[0] != "start"] == []
        assert ("start", "acme-old") not in ew.docker.events


class TestAC4DryRunWritesNothing:
    def test_a_plan_with_creates_updates_and_drift_writes_nothing(self, ew, tmp_path):
        ew.seed_agent("acme-a", tags=(SYSTEM,))
        ew.seed_agent("acme-old")
        mf = (f"name: {SYSTEM}\nagents:\n  a:\n    template: local:plain\n    tags: [team-x]\n"
              "  b:\n    template: local:sched\n")
        with _witness(ew, tmp_path) as w:
            result = W.apply(ew, mf, dry_run=True)
        assert result.status == "valid" and result.dry_run is True
        assert _verdicts(result) == {"a": "update", "b": "create"}
        assert [d.name for d in result.drift] == ["acme-old"]
        assert all(m.applied is None for m in result.members)
        w.assert_no_fleet_write()
        assert w.statements == []  # no request bookkeeping on the service path either

    def test_a_dry_run_with_blockers_is_invalid_and_writes_nothing(self, ew, tmp_path):
        ew.seed_agent("acme-a", tags=())
        with _witness(ew, tmp_path) as w:
            result = W.apply(ew, _manifest("a", "b"), dry_run=True)
        assert result.status == "invalid"
        assert [b.code for b in result.blockers] == ["membership_evidence_prefix_only"]
        w.assert_no_fleet_write()


class TestAC5WeakEvidenceRefusedBeforeAnyWrite:
    def _refused(self, ew, tmp_path, mf):
        with _witness(ew, tmp_path) as w:
            result = W.apply(ew, mf, dry_run=False)
        assert result.status == "refused"
        w.assert_no_fleet_write()  # incl. no Redis command: no lock key
        return result

    def test_a_declared_untagged_member_refuses_even_though_another_is_absent(self, ew, tmp_path):
        """The input only the ORDER decides: `b` is absent, so a gate placed
        after the create step would have created it first."""
        ew.seed_agent("acme-a", tags=())
        result = self._refused(ew, tmp_path, _manifest("a", "b"))
        (b,) = result.blockers
        assert (b.code, b.member) == ("membership_evidence_prefix_only", "acme-a")
        assert "POST /api/agents/acme-a/tags/acme" in b.remedy

    def test_an_undeclared_untagged_member_alone_refuses(self, ew, tmp_path):
        ew.seed_agent("acme-a")
        ew.seed_agent("acme-notes", tags=())
        (b,) = self._refused(ew, tmp_path, _manifest("a")).blockers
        assert (b.code, b.member) == ("membership_evidence_prefix_only", "acme-notes")
        assert "belongs to another system" in b.remedy

    def test_an_unreadable_tag_table_refuses_retryably(self, ew, tmp_path, monkeypatch):
        ew.seed_agent("acme-a")

        def _boom(names):
            raise RuntimeError("database is locked")

        monkeypatch.setattr(db, "get_tags_for_agents", _boom)
        (b,) = self._refused(ew, tmp_path, _manifest("a")).blockers
        assert (b.code, b.retryable) == ("membership_tags_unreadable", True)

    def test_unreadable_docker_refuses_retryably(self, ew, tmp_path):
        ew.seed_agent("acme-a")
        ew.docker.readable = False
        blockers = self._refused(ew, tmp_path, _manifest("a")).blockers
        assert [(b.code, b.retryable) for b in blockers] == [("roster_unreadable", True)]

    def test_a_label_read_returning_nothing_refuses_retryably(self, ew, tmp_path):
        ew.seed_agent("acme-a")
        ew.docker.unreadable_labels.add("acme-a")
        (b,) = self._refused(ew, tmp_path, _manifest("a")).blockers
        assert (b.code, b.retryable) == ("member_reads_unavailable", True)


class TestF3NothingNamesAMemberWhoseCreateFailed:
    def test_a_failed_create_leaves_no_row_naming_it(self, ew):
        ew.fail_create.add("acme-b")
        mf = FLEET.replace("    a: [a, b]", "    a: [a, b]\n    b: [a]").replace(
            "    resources: {cpu: '2'}",
            "    resources: {cpu: '2'}\n    folders: {expose: true}\n"
            "    schedules:\n      - {name: s, cron: '0 9 * * *', message: /s}")
        result = W.apply(ew, mf, dry_run=False)

        assert result.status == "partial"
        assert not db.get_agent_tags("acme-b") and not db.get_shared_folder_config("acme-b")
        assert not db.list_agent_schedules("acme-b") and not db.get_permitted_agents("acme-b")
        assert "acme-b" not in db.get_permitted_agents("acme-a")
        steps = sorted((f.name, f.step) for f in result.failed)
        assert ("acme-b", "create") in steps
        assert {s for n, s in steps if n == "acme-b"} >= {"tag", "edge", "folders", "created_schedules"}
        assert ("acme-a", "edge") in steps  # a -> b skipped, b was not created
        create_failure = next(f for f in result.failed if f.step == "create")
        assert "secret-pat" not in create_failure.reason  # through _failure_reason
        assert ("start", "acme-b") not in ew.docker.events
        b = next(m for m in result.members if m.short_name == "b")
        assert b.applied is False

    def test_every_attempted_write_failing_is_failed(self, ew):
        ew.fail_create.update({"acme-a", "acme-b"})
        assert W.apply(ew, FLEET, dry_run=False).status == "failed"


class TestH2NeverRemoves:
    def test_operator_tag_foreign_edge_grant_and_intra_edge_survive(self, ew):
        ew.seed_agent("acme-a", tags=(SYSTEM, "operator-pick"))
        ew.seed_agent("acme-b")
        db.add_agent_permission("acme-a", "outsider", W.ALICE)
        db.add_agent_permission("acme-a", "acme-b", W.ALICE)
        _hrun("INSERT INTO agent_peer_skill_grants (caller_agent, target_agent, skill_name, "
              "granted_by, granted_at) VALUES ('acme-a', 'acme-b', 'review', 'alice695', "
              "'2026-10-01T00:00:00Z')")
        result = W.apply(ew, _manifest("a", "b", extra="permissions:\n  preset: none\n"), dry_run=False)

        assert result.status == "applied"
        assert db.get_agent_tags("acme-a") == ["acme", "operator-pick"]
        assert db.get_permitted_agents("acme-a") == ["acme-b", "outsider"]
        assert W.Witness.dump_tables()["agent_peer_skill_grants"] != []
        a = next(m for m in result.members if m.short_name == "a")
        assert [(c.code, c.current) for c in a.changes] == [("permission_extra", ["acme-b"])]

    def test_adding_a_tag_keeps_the_operators_tags(self, ew):
        """The write path: `set_agent_tags` is a full replacement. Apply adds
        `team-x` and leaves the operator's `operator-pick` where it was."""
        ew.seed_agent("acme-a", tags=(SYSTEM, "operator-pick"))
        mf = f"name: {SYSTEM}\nagents:\n  a:\n    template: local:plain\n    tags: [team-x]\n"
        result = W.apply(ew, mf, dry_run=False)
        assert result.status == "applied", result.failed
        assert db.get_agent_tags("acme-a") == ["acme", "operator-pick", "team-x"]

    def test_adding_an_edge_keeps_a_hand_granted_edge_and_its_skill_grant(self, ew):
        """The write path, not only the report: `set_permissions` is a full
        replacement that also deletes the peer skill grants on the edges it
        drops (ent#662). Apply adds `a -> b` and leaves `a -> outsider`."""
        ew.seed_agent("acme-a")
        ew.seed_agent("acme-b")
        db.add_agent_permission("acme-a", "outsider", W.ALICE)
        _hrun("INSERT INTO agent_peer_skill_grants (caller_agent, target_agent, skill_name, "
              "granted_by, granted_at) VALUES ('acme-a', 'outsider', 'review', 'alice695', "
              "'2026-10-01T00:00:00Z')")
        result = W.apply(ew, _manifest("a", "b", extra="permissions:\n  preset: full-mesh\n"),
                         dry_run=False)
        assert result.status == "applied", result.failed
        assert db.get_permitted_agents("acme-a") == ["acme-b", "outsider"]
        assert db.get_permitted_agents("acme-b") == ["acme-a"]
        assert W.Witness.dump_tables()["agent_peer_skill_grants"] == [
            ("acme-a", "outsider", "review", "alice695", "2026-10-01T00:00:00Z")]


class TestH3SchedulesOnACreatedMember:
    def test_the_templates_same_named_row_is_updated_after_creation(self, ew):
        dry = W.apply(ew, FLEET, dry_run=True)
        a = next(m for m in dry.members if m.short_name == "a")
        assert ("schedule:daily", "pending_template_resolution") in [(c.field, c.code) for c in a.changes]

        real = W.apply(ew, FLEET, dry_run=False)
        rows = {s.name: s for s in db.list_agent_schedules("acme-a")}
        assert sorted(rows) == ["daily", "weekly"]  # never two `daily` rows
        assert (rows["daily"].cron_expression, rows["daily"].message) == ("0 7 * * *", "/manifest-daily")
        assert rows["daily"].description == "from the template"  # not declared: kept
        assert rows["daily"].enabled is False and rows["weekly"].enabled is True
        a = next(m for m in real.members if m.short_name == "a")
        codes = [(c.field, c.code) for c in a.changes]
        assert ("schedule:daily", "schedule_update") in codes
        assert ("schedule:weekly", "schedule_create") in codes
        assert "pending_template_resolution" not in [c for _f, c in codes]

    def test_a_failed_post_create_read_never_creates_unfiltered(self, ew, monkeypatch):
        real_list = db.list_agent_schedules
        calls = {"n": 0}

        def flaky(agent_name):
            if agent_name == "acme-a":
                calls["n"] += 1
                if calls["n"] >= 2:  # the template's own read succeeds; apply's fails
                    raise RuntimeError("read failed")
            return real_list(agent_name)

        monkeypatch.setattr(db, "list_agent_schedules", flaky)
        result = W.apply(ew, FLEET, dry_run=False)
        monkeypatch.setattr(db, "list_agent_schedules", real_list)
        assert result.status == "partial"
        assert [s.name for s in db.list_agent_schedules("acme-a")] == ["daily"]  # the template's only
        assert sorted(f.reason.split("'")[1] for f in result.failed if f.name == "acme-a") == [
            "daily", "weekly"]


class TestH3bOwnerAndInvoker:
    def test_an_admins_apply_creates_the_row_as_the_agent_owner(self, ew):
        ew.seed_agent("acme-a")
        mf = (f"name: {SYSTEM}\nagents:\n  a:\n    template: local:plain\n"
              "    schedules:\n      - {name: daily, cron: '0 9 * * *', message: /d}\n")
        assert W.apply(ew, mf, W.ADMIN, dry_run=False).status == "applied"
        assert _sched("daily").owner_id == ew.user(W.ALICE).id

    def test_an_update_refused_at_write_time_is_a_failure_not_a_success(self, ew, monkeypatch):
        ew.seed_agent("acme-a")
        db.create_schedule("acme-a", W.ALICE, system_service._build_schedule_create(
            {"name": "daily", "cron": "0 9 * * *", "message": "/d"}))
        monkeypatch.setattr(db, "update_schedule", lambda *a, **k: None)  # the race
        mf = (f"name: {SYSTEM}\nagents:\n  a:\n    template: local:plain\n"
              "    schedules:\n      - {name: daily, cron: '0 10 * * *', message: /d}\n")
        result = W.apply(ew, mf, dry_run=False)
        assert result.status == "failed"
        assert [(f.name, f.step) for f in result.failed] == [("acme-a", "schedules")]


class TestH4Resources:
    def test_a_created_member_gets_its_override_before_its_first_start(self, ew, monkeypatch):
        """The template ran the container with cpu 4; the declared 2 is
        written BEFORE apply's start step, and the platform's own predicate
        then reports the mismatch that drives the start path's recreate."""
        from services.agent_service.helpers import check_resource_limits_match

        seen = {}
        real_start = ew.docker.start

        async def start(name):
            seen[name] = (db.get_resource_limits(name),
                          check_resource_limits_match(ew.docker.containers[name], name))
            return await real_start(name)

        result = asyncio.run(system_service.apply_manifest(
            FLEET, ew.user(W.ALICE), None, dry_run=False,
            create_agent_fn=ew.create, start_agent_fn=start))
        assert result.status == "applied", result.failed
        assert seen["acme-b"] == ({"memory": None, "cpu": "2"}, False)
        assert ew.docker.containers["acme-b"].labels["trinity.cpu"] == "2"
        b = next(m for m in result.members if m.short_name == "b")
        assert ("resources.cpu", "write", "resource_differs") in [
            (c.field, c.action, c.code) for c in b.changes]
        # The created member was started once; nothing else was.
        assert sorted(n for k, n in ew.docker.events if k == "start") == ["acme-a", "acme-b"]

    def test_an_existing_member_is_written_never_restarted(self, ew):
        ew.seed_agent("acme-a", cpu="2")
        db.set_resource_limits("acme-a", memory="8g", cpu=None)
        mf = f"name: {SYSTEM}\nagents:\n  a:\n    template: local:plain\n    resources: {{cpu: '4'}}\n"
        result = W.apply(ew, mf, dry_run=False)
        assert db.get_resource_limits("acme-a") == {"memory": "8g", "cpu": "4"}
        assert ew.docker.events == []
        (a,) = result.members
        assert a.restart_required is True
        again = W.apply(ew, mf, dry_run=False)
        assert again.members[0].verdict == "unchanged" and again.members[0].restart_required is True
        ew.docker.containers["acme-a"].labels.update({"trinity.cpu": "4", "trinity.memory": "8g"})
        assert W.apply(ew, mf, dry_run=False).members[0].restart_required is False


class TestH4RealStartPathKeepsTheVolumes:
    """Codex pass 3 point 1: through the REAL `start_agent_internal` ->
    `recreate_container_with_updated_config`, with Docker mocked at its
    calls. The just-created member's replacement gets the same named volumes,
    and the declared resources."""

    def test_the_recreate_reuses_the_members_volumes(self, ew, monkeypatch):
        from unittest.mock import AsyncMock, MagicMock

        from services.agent_service import lifecycle as lc

        ew.seed_agent("acme-b", cpu="4", memory="8g")  # as the template created it
        db.set_resource_limits("acme-b", memory=None, cpu="2")  # apply's override
        container = ew.docker.containers["acme-b"]
        container.attrs["Config"]["Image"] = "trinity-agent-base:latest"
        container.attrs["Config"]["Labels"]["trinity.ssh-port"] = "2222"

        provisioned = {}

        async def provision(agent_name, **kwargs):
            provisioned.update(kwargs, agent_name=agent_name)
            return container

        for name in ("check_shared_folder_mounts_match",):
            monkeypatch.setattr(lc, name, AsyncMock(return_value=True))
        for name in ("check_public_folder_mount_matches", "check_api_key_env_matches",
                     "check_github_pat_env_matches", "check_full_capabilities_match",
                     "check_guardrails_env_matches", "check_agent_auth_token_env_matches",
                     "check_pull_mode_env_matches", "check_agent_mcp_key_matches",
                     "check_agent_name_env_matches"):
            monkeypatch.setattr(lc, name, MagicMock(return_value=True))
        if hasattr(lc, "check_base_image_matches"):
            monkeypatch.setattr(lc, "check_base_image_matches", AsyncMock(return_value=True))
        monkeypatch.setattr(lc, "get_agent_container", lambda n: container)
        monkeypatch.setattr(lc, "container_reload", AsyncMock())
        monkeypatch.setattr(lc, "container_stop", AsyncMock())
        monkeypatch.setattr(lc, "container_remove", AsyncMock())
        monkeypatch.setattr(lc, "container_start", AsyncMock())
        monkeypatch.setattr(lc, "validate_base_image", lambda image: None)
        monkeypatch.setattr(lc, "image_get", AsyncMock(side_effect=RuntimeError("no daemon")))
        monkeypatch.setattr(lc, "reserve_port_for_recreate", lambda port: None)
        monkeypatch.setattr(lc, "_provision_folders_and_run_agent_container", provision)
        for name in ("wait_for_agent_ready", "inject_assigned_credentials",
                     "inject_assigned_skills", "inject_read_only_hooks", "remove_read_only_hooks"):
            if hasattr(lc, name):
                monkeypatch.setattr(lc, name, AsyncMock(return_value={"status": "stubbed"}))
        for name in ("clear_agent_runtime_state", "clear_agent_breakers"):
            monkeypatch.setattr(lc, name, MagicMock(), raising=False)

        asyncio.run(lc.start_agent_internal("acme-b"))

        assert provisioned["agent_name"] == "acme-b"
        assert provisioned["base_volumes"] == {
            "agent-acme-b-workspace": {"bind": "/home/developer", "mode": "rw"}}
        assert (provisioned["cpu"], provisioned["memory"]) == ("2", "8g")


class TestRestartRequiredFollowsTheMounts:
    """Codex pass 1 #3 and #4: `restart_required` comes from the platform's
    own mount predicate, read strictly, for every existing member."""

    def _consumer_and_exposer(self, ew):
        ew.seed_agent("acme-a")
        ew.seed_agent("acme-b")
        db.upsert_shared_folder_config("acme-a", expose_enabled=False, consume_enabled=True)
        db.upsert_shared_folder_config("acme-b", expose_enabled=True, consume_enabled=False)
        # B's container exposes (its shared-out mount), so B itself is in step.
        ew.docker.containers["acme-b"].attrs["Mounts"].append(
            {"Destination": "/home/developer/shared-out", "Type": "volume",
             "Name": "agent-acme-b-shared", "RW": True})
        return _manifest("a", "b", extra="permissions:\n  explicit:\n    a: [b]\n")

    def test_a_new_edge_into_an_exposer_makes_the_consumer_restart_required(self, ew, monkeypatch):
        from unittest.mock import AsyncMock

        from services.agent_service import helpers
        monkeypatch.setattr(helpers, "volume_get", AsyncMock(return_value=object()))
        mf = self._consumer_and_exposer(ew)
        assert W.apply(ew, mf, dry_run=True).members[0].restart_required is True  # predicted

        result = W.apply(ew, mf, dry_run=False)
        a = next(m for m in result.members if m.short_name == "a")
        assert db.get_permitted_agents("acme-a") == ["acme-b"]
        assert a.restart_required is True  # B's mount is now required and missing
        assert ew.docker.events == []      # and still never restarted

    def test_the_real_run_reports_the_predicate_not_the_prediction(self, ew, monkeypatch):
        """The dry run can only predict (a consumer gaining an edge MAY need a
        new mount); the real run re-reads the platform's predicate. Here B does
        not expose, so the new edge changes no mount: predicted true, false."""
        from unittest.mock import AsyncMock

        from services.agent_service import helpers
        monkeypatch.setattr(helpers, "volume_get", AsyncMock(return_value=object()))
        mf = self._consumer_and_exposer(ew)
        db.upsert_shared_folder_config("acme-b", expose_enabled=False, consume_enabled=False)
        ew.docker.containers["acme-b"].attrs["Mounts"] = [
            m for m in ew.docker.containers["acme-b"].attrs["Mounts"]
            if m["Destination"] != "/home/developer/shared-out"]
        assert W.apply(ew, mf, dry_run=True).members[0].restart_required is True
        result = W.apply(ew, mf, dry_run=False)
        assert db.get_permitted_agents("acme-a") == ["acme-b"]
        assert next(m for m in result.members if m.short_name == "a").restart_required is False

    def test_a_failed_volume_inspect_is_a_read_failure_not_matching_mounts(self, ew, monkeypatch):
        from unittest.mock import AsyncMock

        from services.agent_service import helpers
        mf = self._consumer_and_exposer(ew)
        db.add_agent_permission("acme-a", "acme-b", W.ALICE)
        monkeypatch.setattr(helpers, "volume_get", AsyncMock(side_effect=RuntimeError("docker timeout")))
        snap = world_snapshot = ew.snapshot(mf)
        assert ("member_reads_unavailable", "acme-a") in _codes(world_snapshot)
        assert snap.members["a"].blocked is True


class TestH4bFolders:
    def test_an_existing_members_omitted_key_keeps_its_value(self, ew):
        ew.seed_agent("acme-a")
        db.upsert_shared_folder_config("acme-a", expose_enabled=False, consume_enabled=True)
        mf = f"name: {SYSTEM}\nagents:\n  a:\n    template: local:plain\n    folders: {{expose: true}}\n"
        assert W.apply(ew, mf, dry_run=False).status == "applied"
        cfg = db.get_shared_folder_config("acme-a")
        assert (cfg.expose_enabled, cfg.consume_enabled) == (True, True)


class TestH6Prompt:
    def test_written_only_by_an_admin_and_only_when_different(self, ew, monkeypatch):
        ew.seed_agent("acme-a")
        db.set_setting("trinity_prompt", "old")
        writes = []
        real = db.set_setting
        monkeypatch.setattr(db, "set_setting", lambda k, v: (writes.append((k, v)), real(k, v))[1])

        creator = W.apply(ew, _manifest("a", extra="prompt: new\n"), dry_run=False)
        assert creator.prompt.verdict == "requires_admin" and writes == []
        assert W.apply(ew, _manifest("a", extra="prompt: old\n"), W.ADMIN, dry_run=False).prompt.verdict == "unchanged"
        assert writes == []
        admin = W.apply(ew, _manifest("a", extra="prompt: new\n"), W.ADMIN, dry_run=False)
        assert admin.prompt.verdict == "update" and writes == [("trinity_prompt", "new")]
        assert W.apply(ew, _manifest("a", extra='prompt: ""\n'), W.ADMIN, dry_run=False).prompt.verdict == "not_declared"


class TestH7H8ViewAndStart:
    def test_a_second_apply_creates_no_second_view(self, ew):
        mf = _manifest("a", extra="system_view: {name: Acme}\n")
        first = W.apply(ew, mf, dry_run=False)
        assert first.system_view.verdict == "create"
        second = W.apply(ew, mf, dry_run=False)
        assert second.system_view.verdict == "unchanged"
        views = db.list_user_system_views(str(ew.user(W.ALICE).id))
        assert len(views) == 1 and second.system_view.id == views[0].id

    def test_another_users_private_view_does_not_stop_the_callers_own(self, ew):
        from db_models import SystemViewCreate
        db.create_system_view(str(ew.user(W.BOB).id), SystemViewCreate(
            name="Bob's", description=system_service.SYSTEM_VIEW_AUTO_DESCRIPTION.format(system_name=SYSTEM),
            filter_tags=[SYSTEM], is_shared=False))
        result = W.apply(ew, _manifest("a", extra="system_view: {name: Acme}\n"), dry_run=False)
        assert result.system_view.verdict == "create"
        assert len(db.list_user_system_views(str(ew.user(W.ALICE).id))) == 1

    def test_a_stopped_existing_member_is_not_started_a_created_one_is(self, ew):
        ew.seed_agent("acme-a", status="exited")
        assert W.apply(ew, _manifest("a", "b"), dry_run=False).status == "applied"
        assert ew.docker.events == [("start", "acme-b")]
        assert ew.docker.containers["acme-a"].status == "exited"


class TestH11bCreatedMemberTaggedFirst:
    def test_a_failed_system_tag_is_named_and_the_next_apply_refuses_it(self, ew, monkeypatch):
        real_add = db.add_agent_tag

        def add(name, tag):
            if name == "acme-b" and tag == SYSTEM:
                raise RuntimeError("tag write failed")
            return real_add(name, tag)

        monkeypatch.setattr(db, "add_agent_tag", add)
        result = W.apply(ew, _manifest("a", "b"), dry_run=False)
        failure = next(f for f in result.failed if f.name == "acme-b" and f.step == "tag")
        assert "was created but not tagged 'acme'" in failure.reason
        monkeypatch.setattr(db, "add_agent_tag", real_add)

        again = W.apply(ew, _manifest("a", "b"), dry_run=False)
        assert again.status == "refused"
        assert [(b.code, b.member) for b in again.blockers] == [
            ("membership_evidence_prefix_only", "acme-b")]


KEY = f"system_apply:{SYSTEM}"


class TestH13TheLock:
    def test_a_held_lock_refuses_and_writes_nothing(self, ew, tmp_path):
        ew.redis.store[KEY] = "someone-else"
        with _witness(ew, tmp_path) as w:
            result = W.apply(ew, _manifest("a"), dry_run=False)
        assert result.status == "refused"
        assert [(b.code, b.retryable) for b in result.blockers] == [("apply_in_progress", True)]
        assert w.fleet_statements == [] and w.creates == []
        assert ew.redis.store[KEY] == "someone-else"

    def test_no_redis_refuses_never_runs_unlocked(self, ew, tmp_path, monkeypatch):
        monkeypatch.setattr(system_service, "get_breaker_redis", lambda: None)
        with _witness(ew, tmp_path) as w:
            result = W.apply(ew, _manifest("a"), dry_run=False)
        assert [(b.code, b.retryable) for b in result.blockers] == [("apply_lock_unavailable", True)]
        assert w.fleet_statements == [] and w.creates == []

    def test_the_lock_is_released_and_never_read_back_client_side(self, ew):
        assert W.apply(ew, _manifest("a"), dry_run=False).status == "applied"
        assert KEY not in ew.redis.store
        # SET NX to take it; scripts to renew and release. A client-side GET
        # would mean the non-atomic GET-then-EXPIRE/DELETE path.
        assert ("get", KEY) not in ew.redis.commands
        assert ("script", "release", KEY) in ew.redis.commands

    def test_a_blocker_that_appears_only_under_the_lock_refuses_and_releases(self, ew, monkeypatch):
        ew.seed_agent("acme-a")
        real = system_service.read_apply_snapshot
        calls = {"n": 0}

        async def snapshot(manifest, user):
            calls["n"] += 1
            if calls["n"] == 2:
                db.set_agent_tags("acme-a", [])  # someone untags it meanwhile
            return await real(manifest, user)

        monkeypatch.setattr(system_service, "read_apply_snapshot", snapshot)
        result = W.apply(ew, _manifest("a", "b"), dry_run=False)
        assert result.status == "refused" and calls["n"] == 2
        assert [b.code for b in result.blockers] == ["membership_evidence_prefix_only"]
        assert KEY not in ew.redis.store and ew.create_calls == []

    def test_the_keeper_renews_from_acquisition_through_the_replan(self, ew, monkeypatch):
        monkeypatch.setattr(system_service, "APPLY_LEASE_RENEW_SECONDS", 0.01)
        real = system_service.read_apply_snapshot
        seen = {}

        async def slow_snapshot(manifest, user):
            result = await real(manifest, user)
            if "first" in seen:
                await asyncio.sleep(0.1)
                seen["renewals_during_replan"] = ew.redis.commands.count(("script", "refresh", KEY))
            seen["first"] = True
            return result

        monkeypatch.setattr(system_service, "read_apply_snapshot", slow_snapshot)
        assert W.apply(ew, _manifest("a"), dry_run=False).status == "applied"
        assert seen["renewals_during_replan"] >= 1

    @pytest.mark.parametrize("loss", ["lost", "degraded"])
    def test_a_lease_lost_during_a_slow_create_stops_every_later_write(self, ew, monkeypatch, loss):
        monkeypatch.setattr(system_service, "APPLY_LEASE_RENEW_SECONDS", 0.01)
        real_create = ew.create

        async def slow_create(config, current_user, request=None, skip_name_sanitization=False):
            if config.name == "acme-a":
                if loss == "lost":
                    ew.redis.store[KEY] = "successor"
                else:
                    def _down(target, key):
                        raise ConnectionError("redis down")
                    ew.redis.on_refresh = _down
                await asyncio.sleep(0.1)
            return await real_create(config, current_user, request, skip_name_sanitization)

        ew.create = slow_create
        result = W.apply(ew, _manifest("a", "b"), dry_run=False)
        ew.redis.on_refresh = None
        assert db.get_agent_owner("acme-a")  # the in-flight create finished
        assert not db.get_agent_owner("acme-b")
        assert result.status == "partial"
        lost = [(f.name, f.step) for f in result.failed if f.reason.startswith("lock_lost")]
        assert ("acme-b", "create") in lost and ("acme-a", "tag") in lost
        assert ew.docker.events == []  # nothing started after the loss
        if loss == "lost":
            assert ew.redis.store[KEY] == "successor"  # a stale release never deletes it

    def test_a_write_that_blocks_the_loop_past_the_ttl_stops_every_later_write(self, ew, monkeypatch):
        """Codex pass 1 #1: renewals run on the event loop and the writes are
        synchronous, so a write that blocks the loop past the TTL lets the
        lease lapse (and a successor take it) before the keeper runs again.
        The keeper here never gets to run (300 s interval); only the local
        deadline can stop the later writes."""
        import time as _time

        monkeypatch.setattr(system_service, "APPLY_LOCK_TTL_SECONDS", 2)
        ew.seed_agent("acme-a")
        ew.seed_agent("acme-b")
        real_add = db.add_agent_tag
        blocked = {}

        def add(name, tag):
            if name == "acme-a" and not blocked:
                blocked["at"] = True
                _time.sleep(2.0)                       # blocks the event loop
                ew.redis.store[KEY] = "successor"      # the lease lapsed; B took it
            return real_add(name, tag)

        monkeypatch.setattr(db, "add_agent_tag", add)
        mf = (f"name: {SYSTEM}\nagents:\n  a:\n    template: local:plain\n    tags: [x]\n"
              "  b:\n    template: local:plain\n    tags: [y]\n")
        result = W.apply(ew, mf, dry_run=False)

        assert "x" in db.get_agent_tags("acme-a")      # the in-flight write finished
        assert "y" not in db.get_agent_tags("acme-b")  # nothing after the deadline
        assert [(f.name, f.step) for f in result.failed if f.reason.startswith("lock_lost")] == [
            ("acme-b", "tag")]
        assert ew.redis.store[KEY] == "successor"

    def test_the_lease_lost_after_the_first_start_stops_the_second(self, ew, monkeypatch):
        """H8: started one by one behind the stop flag, never in bulk."""
        monkeypatch.setattr(system_service, "APPLY_LEASE_RENEW_SECONDS", 0.01)
        real_start = ew.docker.start

        async def start(name):
            out = await real_start(name)
            if name == "acme-a":
                ew.redis.store[KEY] = "successor"
                await asyncio.sleep(0.1)
            return out

        result = asyncio.run(system_service.apply_manifest(
            _manifest("a", "b"), ew.user(W.ALICE), None, dry_run=False,
            create_agent_fn=ew.create, start_agent_fn=start))
        assert [n for k, n in ew.docker.events if k == "start"] == ["acme-a"]
        assert ("acme-b", "start") in [(f.name, f.step) for f in result.failed
                                       if f.reason.startswith("lock_lost")]


class TestOtherShapeFreshInstall:
    """Bootstrap equivalence: on an instance with no agents, tags, schedules
    or prior deploy, apply builds what create mode builds, but for the two
    stated differences, asserted as such (Codex B-AC6)."""

    def _state(self):
        names = ["acme-a", "acme-b"]
        return {
            "tags": {n: db.get_agent_tags(n) for n in names},
            "folders": {n: (lambda c: (c.expose_enabled, c.consume_enabled) if c else None)(
                db.get_shared_folder_config(n)) for n in names},
            "edges": {n: db.get_permitted_agents(n) for n in names},
            "prompt": (db.get_setting("trinity_prompt") or SimpleNamespace(value=None)).value,
            "views": [(v.name, v.description, sorted(v.filter_tags), v.is_shared)
                      for v in db.list_user_system_views(str(db.get_user_by_username(W.ADMIN)["id"]))],
            "schedules": {n: sorted((s.name, s.cron_expression, s.message, s.enabled, s.timezone,
                                     s.description) for s in db.list_agent_schedules(n))
                          for n in names},
            "limits": {n: db.get_resource_limits(n) for n in names},
        }

    def test_apply_on_a_fresh_install_builds_what_create_builds(self, db_backend, monkeypatch, tmp_path):
        import db_harness
        import routers.agents as agents_router

        manifest = FLEET + "prompt: the system prompt\nsystem_view: {name: Acme, icon: A}\n"

        create_world = W.make_world(monkeypatch, tmp_path / "create")
        W.install_executor_seams(create_world, monkeypatch)
        monkeypatch.setattr(agents_router, "start_agent_internal", create_world.docker.start)
        created = asyncio.run(system_service.deploy_manifest(
            manifest, create_world.user(W.ADMIN), None, dry_run=False,
            create_agent_fn=create_world.create))
        assert created.status == "deployed", created
        create_configs = [c.model_dump() for c in create_world.create_calls]
        create_state = self._state()

        (tmp_path / "apply").mkdir()
        db_harness._activate_backend(db_backend, tmp_path / "apply", monkeypatch)
        apply_world = W.make_world(monkeypatch, tmp_path / "apply")
        W.install_executor_seams(apply_world, monkeypatch)
        applied = W.apply(apply_world, manifest, W.ADMIN, dry_run=False)
        assert applied.status == "applied", applied.failed
        assert set(_verdicts(applied).values()) == {"create"}
        apply_state = self._state()

        assert [c.model_dump() for c in apply_world.create_calls] == create_configs
        for key in ("tags", "folders", "edges", "prompt", "views"):
            assert apply_state[key] == create_state[key], key
        # Stated difference 1: the template's same-named schedule carries the
        # manifest's declared fields (create skipped the manifest's entry).
        assert create_state["schedules"]["acme-a"] == [
            ("daily", "0 9 * * *", "/template-daily", False, "UTC", "from the template"),
            ("weekly", "0 9 * * MON", "/weekly", True, "UTC", None)]
        assert apply_state["schedules"]["acme-a"] == [
            ("daily", "0 7 * * *", "/manifest-daily", False, "UTC", "from the template"),
            ("weekly", "0 9 * * MON", "/weekly", True, "UTC", None)]
        assert apply_state["schedules"]["acme-b"] == create_state["schedules"]["acme-b"] == []
        # Stated difference 2: a declared resource the template overrode is an
        # override row (create keeps template-wins).
        assert create_state["limits"] == {"acme-a": None, "acme-b": None}
        assert apply_state["limits"] == {"acme-a": None, "acme-b": {"memory": None, "cpu": "2"}}


class TestOtherShapeEmptyData:
    def test_an_empty_manifest_is_creates_named_400_and_writes_nothing(self, ew, tmp_path):
        from fastapi import HTTPException
        mf = f"name: {SYSTEM}\nagents: {{}}\n"
        with pytest.raises(HTTPException) as create_error:
            asyncio.run(system_service.deploy_manifest(mf, ew.user(W.ALICE), None, dry_run=True))
        for dry_run in (True, False):
            with _witness(ew, tmp_path) as w:
                with pytest.raises(HTTPException) as apply_error:
                    W.apply(ew, mf, dry_run=dry_run)
            assert (apply_error.value.status_code, apply_error.value.detail) == (
                create_error.value.status_code, create_error.value.detail) == (
                400, "Missing required field: agents (must have at least 1)")
            w.assert_no_fleet_write()


class TestExecutionNeverLosesTheReport:
    def test_an_unreadable_resource_default_is_a_retryable_blocker_not_a_500(self, ew, tmp_path, monkeypatch):
        """Codex pass 1 #2: the fleet default a member to create takes is a
        settings read; it happens in the snapshot, so its failure refuses the
        run before any write instead of raising after the first create."""
        def _boom():
            raise RuntimeError("settings table locked")

        monkeypatch.setattr(system_service, "_manifest_default_resources", _boom)
        with _witness(ew, tmp_path) as w:
            result = W.apply(ew, _manifest("a", "b"), dry_run=False)
        assert result.status == "refused"
        assert [(b.code, b.retryable) for b in result.blockers] == [("member_reads_unavailable", True)]
        w.assert_no_fleet_write()

    def test_a_failure_preparing_a_later_create_keeps_the_report(self, ew, monkeypatch):
        """Anything that can raise while preparing a create runs inside the
        write's own attempt, so member A's create and tag are reported and
        member B's failure is a `failed[]` row, never a bare 500."""
        real = system_service._member_agent_config

        def flaky(name, cfg, defaults):
            if name == "acme-b":
                raise RuntimeError("config build failed")
            return real(name, cfg, defaults)

        monkeypatch.setattr(system_service, "_member_agent_config", flaky)
        result = W.apply(ew, _manifest("a", "b"), dry_run=False)
        assert result.status == "partial"
        assert db.get_agent_owner("acme-a") and SYSTEM in db.get_agent_tags("acme-a")
        assert ("acme-b", "create") in [(f.name, f.step) for f in result.failed]

    def test_a_writer_that_raises_is_recorded_and_the_run_continues(self, ew, monkeypatch):
        ew.seed_agent("acme-a")
        ew.seed_agent("acme-b")

        def boom(*a, **k):
            raise RuntimeError("edge table locked")

        monkeypatch.setattr(db, "add_agent_permission", boom)
        mf = _manifest("a", "b", extra="permissions:\n  preset: full-mesh\n") + ""
        mf = mf.replace("  b:\n    template: local:plain", "  b:\n    template: local:plain\n    tags: [x]")
        result = W.apply(ew, mf, dry_run=False)
        assert result.status == "partial"
        assert sorted((f.name, f.step) for f in result.failed) == [
            ("acme-a", "permissions"), ("acme-b", "permissions")]
        assert "x" in db.get_agent_tags("acme-b")
