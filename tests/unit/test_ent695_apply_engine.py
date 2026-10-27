"""trinity-enterprise#695: apply mode, the engine (`services/system_service.py`).

Every case runs over the REAL `db` on a full schema (`db_backend`), seeded with
the real writers; Docker is faked at its read seams only (`_ent695_world.py`).
Sections follow the build order: snapshot + gates, the schedule matcher, the
field reconcilers, the executor.
"""
from __future__ import annotations

import sys
from pathlib import Path

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
        world.seed_agent("acme-a")
        world.docker.readable = False
        snap = world.snapshot(_manifest("a"))

        (b,) = _blocker(snap, "roster_unreadable")
        assert b.retryable is True

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
