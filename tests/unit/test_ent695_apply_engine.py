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
def world(db_backend, monkeypatch):
    return W.make_world(monkeypatch)


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
