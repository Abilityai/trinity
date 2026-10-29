"""trinity-enterprise#529 — the untouched default source follows the bundled pin.

ent#237 seeds the bundled community catalog on a FRESH install only, pinned to
`config.DEFAULT_SKILL_SOURCE_REF`. Without this, an existing install stays on
the tag it was born with forever, and "update the library → the fleet picks it
up" (ent#236) is true only for fresh installs.

What the tests pin, and why each is a behaviour rather than a spelling:

* **The follow** (AC1): an untouched seed behind the pin moves to it, through
  the same bookkeeping reset an admin ref edit uses, with a system audit row
  and the two row fields the Settings line reads (AC5).
* **"Where it points" decides** (AC2, owner ruling 2026-10-29): an admin change
  to url, ref or ref_type through ANY door opts the row out for good. Disabling
  only pauses it; rename and priority never opt out.
* **Forward only**: strict `vMAJOR.MINOR.PATCH` on both sides, compared as
  numbers.
* **AC4 over real git**: the next sync fetches the new tag (annotated, like
  every `trinity-skills` release, #2550), is NOT refused as `moved_tag`, counts
  as a moved commit, and `run_cycle` re-injects the fleet.
* **The in-flight race** both plan reviewers found: a sync that started on the
  old tag must not stamp its commit onto the followed ref, or every later sync
  is refused as `moved_tag`.
* **The backfill** for rows seeded before the marker existed, through the real
  SQLite migration (the Alembic revision executes the same SQL constant).
"""

from __future__ import annotations

import ast
import asyncio
import json
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

pytestmark = pytest.mark.unit

BUILTIN_URL = "github.com/abilityai/trinity-skills"


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture(autouse=True)
def _real_modules_not_stubs(monkeypatch):
    """Evict another module's import-time `sys.modules` stubs (#1898, #762).

    A stub has no `__file__`. Evicted through `monkeypatch.delitem`, so the
    eviction is undone at teardown and this file cannot become the next source
    of the pollution it works around (`tests/lint_sys_modules.py`).
    """
    import importlib

    names = (
        "database",
        "utils.url_validation",
        "services.platform_audit_service",
        "services.skill_service",
        "services.skills_sync_service",
    )
    for name in names:
        mod = sys.modules.get(name)
        if mod is not None and getattr(mod, "__file__", None) is None:
            monkeypatch.delitem(sys.modules, name, raising=False)
    for name in names:
        try:
            importlib.import_module(name)
        except Exception:  # noqa: BLE001 — a genuinely broken import fails in the test
            pass
    yield


@pytest.fixture
def db_path(tmp_path, monkeypatch) -> Path:
    """A throwaway SQLite DB at the CURRENT schema, bound via DATABASE_URL."""
    from db.schema import init_schema

    path = tmp_path / "trinity.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path}")
    conn = sqlite3.connect(path)
    init_schema(conn.cursor(), conn)
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def ops(db_path):
    from db.skill_sources import SkillSourcesOperations

    return SkillSourcesOperations()


class _Facade:
    """The slice of the `db` facade skill_service and the audit service use.

    Injected rather than relying on the `database.db` singleton, which another
    test module can bind to a MagicMock for the rest of the session (see
    test_ent237_skill_sources._SourcesFacade). The audit half captures entries
    so a test asserts the row the real `platform_audit_service.log` builds.
    """

    def __init__(self, ops):
        self._ops = ops
        self.audit_entries = []

    # -- sources ------------------------------------------------------------
    def list_skill_sources(self, enabled_only=False):
        return self._ops.list_sources(enabled_only)

    def get_skill_source(self, source_id):
        return self._ops.get_source(source_id)

    def get_default_skill_source(self):
        return self._ops.get_default_source()

    def count_skill_sources(self):
        return self._ops.count_sources()

    def create_skill_source(self, **kwargs):
        return self._ops.create_source(**kwargs)

    def update_skill_source(self, source_id, **fields):
        return self._ops.update_source(source_id, **fields)

    def delete_skill_source(self, source_id):
        return self._ops.delete_source(source_id)

    def record_skill_source_sync(self, source_id, **kwargs):
        return self._ops.record_sync(source_id, **kwargs)

    def follow_skill_source_pin(self, source_id, **kwargs):
        return self._ops.follow_bundled_pin(source_id, **kwargs)

    # -- settings / audit -----------------------------------------------------
    def get_setting_value(self, key, default=None):
        return default

    def delete_setting(self, key):
        return True

    def create_audit_entry(self, entry):
        self.audit_entries.append(entry)

    def create_audit_entry_chained(self, entry, compute_hash):  # pragma: no cover
        self.audit_entries.append(entry)


@pytest.fixture
def facade(ops, monkeypatch):
    import services.platform_audit_service as pas
    import services.skill_service as ss

    f = _Facade(ops)
    monkeypatch.setattr(ss, "db", f)
    monkeypatch.setattr(pas, "db", f)
    return f


@pytest.fixture
def pin(monkeypatch):
    """Set the bundled catalog the follow reads: `pin("v0.5.0")`."""
    import config

    monkeypatch.setattr(config, "DEFAULT_SKILL_SOURCE_URL", BUILTIN_URL)

    def _set(ref, url=None):
        monkeypatch.setattr(config, "DEFAULT_SKILL_SOURCE_REF", ref)
        if url is not None:
            monkeypatch.setattr(config, "DEFAULT_SKILL_SOURCE_URL", url)

    return _set


def _seed(db_path: Path, ref: str, url: str = BUILTIN_URL) -> str:
    """Seed the default source through the REAL fresh-install seed.

    The marker this file is about is written by the seed, so seeding through it
    (not an INSERT in the test) is what proves the seed writes it.
    """
    import config
    import database as D

    old_url, old_ref = config.DEFAULT_SKILL_SOURCE_URL, config.DEFAULT_SKILL_SOURCE_REF
    config.DEFAULT_SKILL_SOURCE_URL, config.DEFAULT_SKILL_SOURCE_REF = url, ref
    try:
        conn = sqlite3.connect(db_path)
        D._seed_fresh_install_skill_source(conn.cursor(), conn)
        conn.close()
    finally:
        config.DEFAULT_SKILL_SOURCE_URL, config.DEFAULT_SKILL_SOURCE_REF = old_url, old_ref
    row = sqlite3.connect(db_path).execute(
        "SELECT id FROM skill_sources WHERE is_default = 1"
    ).fetchone()
    assert row, "the fresh-install seed wrote no default row"
    return row[0]


def _row(db_path: Path, source_id: str) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    r = conn.execute("SELECT * FROM skill_sources WHERE id = ?", (source_id,)).fetchone()
    conn.close()
    return dict(r) if r else {}


def _set_columns(db_path: Path, source_id: str, **cols) -> None:
    conn = sqlite3.connect(db_path)
    sets = ", ".join(f"{k} = ?" for k in cols)
    conn.execute(f"UPDATE skill_sources SET {sets} WHERE id = ?", (*cols.values(), source_id))
    conn.commit()
    conn.close()


def _follow():
    import services.skill_service as ss

    return asyncio.run(ss.follow_bundled_pin())


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


# =============================================================================
# AC1 — the follow itself
# =============================================================================

class TestFollow:
    def test_untouched_seed_moves_to_the_newer_pin(self, db_path, ops, facade, pin):
        sid = _seed(db_path, "v0.3.0")
        ops.record_sync(sid, success=True, commit_sha="a" * 40)
        pin("v0.5.0")

        result = _follow()

        assert result == {"source_id": sid, "from_ref": "v0.3.0", "to_ref": "v0.5.0"}
        row = _row(db_path, sid)
        assert row["ref"] == "v0.5.0"
        # The retarget reset: without it the new tag is compared against
        # v0.3.0's commit and refused as `moved_tag` (see TestSyncAfterFollow).
        assert row["last_commit_sha"] is None
        assert row["last_sync_status"] == "never"
        assert row["last_sync_at"] is None
        assert row["last_error"] is None
        # AC5 data, and the row still follows the NEXT pin.
        assert row["followed_from_ref"] == "v0.3.0"
        assert row["followed_at"]
        assert row["follows_bundled_pin"] == 1

    def test_writes_one_system_audit_row(self, db_path, facade, pin):
        sid = _seed(db_path, "v0.3.0")
        pin("v0.5.0")

        _follow()
        _follow()  # second boot: already on the pin, nothing to do

        assert len(facade.audit_entries) == 1
        entry = facade.audit_entries[0]
        assert entry["event_type"] == "configuration"
        assert entry["event_action"] == "skill_source_follow_bundled_pin"
        assert (entry["actor_type"], entry["actor_id"]) == ("system", "trinity-system")
        assert entry["source"] == "system"
        assert (entry["target_type"], entry["target_id"]) == ("skill_source", sid)
        assert json.loads(entry["details"]) == {"from_ref": "v0.3.0", "to_ref": "v0.5.0"}

    def test_the_status_api_carries_the_follow(self, db_path, facade, pin, tmp_path):
        import services.skill_service as ss

        sid = _seed(db_path, "v0.3.0")
        pin("v0.5.0")
        _follow()

        svc = ss.SkillService()
        svc.library_root = tmp_path / "clones"
        svc.library_path = tmp_path / "clones"
        status = svc.get_library_status()

        entry = next(s for s in status["sources"] if s["id"] == sid)
        assert entry["followed_from_ref"] == "v0.3.0"
        assert entry["followed_at"]
        assert entry["follows_bundled_pin"] is True

    def test_never_raises(self, db_path, facade, pin, monkeypatch):
        """It runs at boot in front of the auto-sync loop's start."""
        pin("v0.5.0")

        def boom():
            raise RuntimeError("db unavailable")

        monkeypatch.setattr(facade, "get_default_skill_source", boom)
        assert _follow() is None


# =============================================================================
# AC2 — "where it points" decides (owner ruling 2026-10-29)
# =============================================================================

def _door_ref(ops, sid):
    ops.update_source(sid, ref="v0.2.0")


def _door_url(ops, sid):
    # Away and back: the row ends on the configured URL again, so only the
    # marker can be what stops the follow.
    ops.update_source(sid, url="https://github.com/abilityai/trinity-skills")
    ops.update_source(sid, url=BUILTIN_URL)


def _door_ref_type(ops, sid):
    ops.update_source(sid, ref_type="branch")
    ops.update_source(sid, ref_type="tag")


def _door_apply(ops, sid):
    """The MCP `register/update_skill_source` door (ent#692)."""
    import services.skill_service as ss

    action, _ = ss.apply_skill_source(url=BUILTIN_URL, fields={"ref": "v0.2.0"})
    assert action == "updated"


class TestOptOut:
    @pytest.mark.parametrize(
        "door", [_door_ref, _door_url, _door_ref_type, _door_apply],
        ids=["put-ref", "put-url", "put-ref_type", "apply"],
    )
    def test_an_admin_retarget_opts_out_for_good(self, db_path, ops, facade, pin, door):
        sid = _seed(db_path, "v0.3.0")
        door(ops, sid)
        before = _row(db_path, sid)
        pin("v0.5.0")

        assert _follow() is None
        after = _row(db_path, sid)
        assert after["ref"] == before["ref"]
        assert after["follows_bundled_pin"] == 0
        assert facade.audit_entries == []

    @pytest.mark.parametrize(
        "fields", [{"name": "Our catalog"}, {"priority": 5}],
        ids=["rename", "priority"],
    )
    def test_rename_and_priority_do_not_opt_out(self, db_path, ops, facade, pin, fields):
        sid = _seed(db_path, "v0.3.0")
        ops.update_source(sid, **fields)
        pin("v0.5.0")

        assert _follow() is not None
        assert _row(db_path, sid)["ref"] == "v0.5.0"

    def test_disabling_pauses_and_re_enabling_resumes(self, db_path, ops, facade, pin):
        sid = _seed(db_path, "v0.3.0")
        ops.update_source(sid, enabled=False)
        pin("v0.5.0")

        assert _follow() is None, "a disabled default is left alone (AC2/AC3)"
        assert _row(db_path, sid)["ref"] == "v0.3.0"
        assert _row(db_path, sid)["enabled"] == 0, "the follow never re-enables (AC3)"

        ops.update_source(sid, enabled=True)
        assert _follow() is not None
        assert _row(db_path, sid)["ref"] == "v0.5.0"

    def test_a_save_that_changes_nothing_keeps_following(self, db_path, ops, facade, pin):
        sid = _seed(db_path, "v0.3.0")
        ops.update_source(sid, ref="v0.3.0", url=BUILTIN_URL, ref_type="tag")
        pin("v0.5.0")

        assert _follow() is not None

    def test_an_admin_retarget_clears_the_follow_line(self, db_path, ops, facade, pin):
        """The line explains the CURRENT ref; after a hand edit it would lie."""
        sid = _seed(db_path, "v0.3.0")
        pin("v0.5.0")
        _follow()

        ops.update_source(sid, ref="v0.4.0")

        row = _row(db_path, sid)
        assert row["followed_from_ref"] is None
        assert row["followed_at"] is None

    def test_a_deleted_default_stays_deleted(self, db_path, ops, facade, pin):
        sid = _seed(db_path, "v0.3.0")
        ops.delete_source(sid)
        pin("v0.5.0")

        assert _follow() is None
        assert ops.count_sources() == 0

    def test_a_custom_source_never_follows(self, db_path, ops, facade, pin):
        src = ops.create_source(name="Ours", url=BUILTIN_URL, ref="v0.3.0", ref_type="tag")
        pin("v0.5.0")

        assert _follow() is None
        assert ops.get_source(src.id).ref == "v0.3.0"


# =============================================================================
# Forward only, and only the configured catalog
# =============================================================================

class TestForwardOnly:
    @pytest.mark.parametrize(
        "row_ref,bundled",
        [
            ("v0.5.0", "v0.3.0"),       # a downgraded image
            ("v0.5.0", "v0.5.0"),       # already there
            ("v0.3.0", "main"),         # a branch name is not a version
            ("v0.3.0", "v0.6.0-rc1"),   # a pre-release is not a release
            ("v0.3.0", "0.6.0"),        # mixed spellings name different tags
            ("0.3.0", "v0.6.0"),
            ("v0.3.0", ""),
        ],
    )
    def test_does_not_move(self, db_path, facade, pin, row_ref, bundled):
        sid = _seed(db_path, "v0.3.0")
        _set_columns(db_path, sid, ref=row_ref)
        pin(bundled)

        assert _follow() is None
        assert _row(db_path, sid)["ref"] == row_ref

    def test_versions_compare_as_numbers(self, db_path, facade, pin):
        sid = _seed(db_path, "v0.9.0")
        pin("v0.10.0")

        assert _follow() is not None
        assert _row(db_path, sid)["ref"] == "v0.10.0"

    def test_not_when_the_configured_catalog_is_another_repo(self, db_path, facade, pin):
        sid = _seed(db_path, "v0.3.0")
        pin("v0.5.0", url="github.com/example/skills-fork")

        assert _follow() is None
        assert _row(db_path, sid)["ref"] == "v0.3.0"

    def test_not_when_the_seed_is_disabled_by_env(self, db_path, facade, pin):
        sid = _seed(db_path, "v0.3.0")
        pin("v0.5.0", url="")

        assert _follow() is None
        assert _row(db_path, sid)["ref"] == "v0.3.0"


# =============================================================================
# The compare-and-set
# =============================================================================

class TestCompareAndSet:
    def test_a_lost_race_writes_no_audit_row(self, db_path, ops, facade, pin, monkeypatch):
        """Two workers boot together; the one whose CAS loses must not audit."""
        sid = _seed(db_path, "v0.3.0")
        pin("v0.5.0")
        real = facade.follow_skill_source_pin

        def other_worker_won_first(source_id, **kwargs):
            _set_columns(db_path, source_id, ref="v0.5.0")
            return real(source_id, **kwargs)

        monkeypatch.setattr(facade, "follow_skill_source_pin", other_worker_won_first)

        assert _follow() is None
        assert facade.audit_entries == []

    def test_a_clashing_custom_source_is_a_no_op_not_an_error(self, db_path, ops, facade, pin):
        """UNIQUE(url, ref): a custom source already at the new tag."""
        sid = _seed(db_path, "v0.3.0")
        ops.create_source(name="Mine", url=BUILTIN_URL, ref="v0.5.0", ref_type="tag")
        pin("v0.5.0")

        assert _follow() is None
        assert _row(db_path, sid)["ref"] == "v0.3.0"
        assert facade.audit_entries == []


class TestCompareAndSetPredicates:
    """Every eligibility condition is in the UPDATE's WHERE, not only in the
    caller's pre-check. A disable, a marker clear or a retarget that lands
    between the service's read and the move must still stop it, so each
    condition is driven here with the pre-check out of the way."""

    @pytest.mark.parametrize(
        "cols",
        [
            {"follows_bundled_pin": 0},
            {"enabled": 0},
            {"ref_type": "branch"},
            {"is_default": 0},
            {"created_by": "1"},
            {"url": "github.com/example/skills-fork"},
            {"ref": "v0.4.0"},
        ],
        ids=["marker-cleared", "disabled", "branch", "not-default", "not-the-seed",
             "other-url", "ref-moved"],
    )
    def test_the_update_itself_refuses(self, db_path, ops, cols):
        sid = _seed(db_path, "v0.3.0")
        _set_columns(db_path, sid, **cols)
        before = _row(db_path, sid)

        moved = ops.follow_bundled_pin(
            sid, from_ref="v0.3.0", to_ref="v0.5.0", url=BUILTIN_URL
        )

        assert moved is None
        assert _row(db_path, sid) == before

    def test_a_disable_between_the_read_and_the_move_writes_no_audit_row(
        self, db_path, facade, pin, monkeypatch
    ):
        sid = _seed(db_path, "v0.3.0")
        pin("v0.5.0")
        real = facade.follow_skill_source_pin

        def disabled_meanwhile(source_id, **kwargs):
            _set_columns(db_path, source_id, enabled=0)
            return real(source_id, **kwargs)

        monkeypatch.setattr(facade, "follow_skill_source_pin", disabled_meanwhile)

        assert _follow() is None
        assert _row(db_path, sid)["ref"] == "v0.3.0"
        assert facade.audit_entries == []


# =============================================================================
# The seed writes the marker
# =============================================================================

class TestSeedMarker:
    def test_cursor_seed_marks_the_builtin_catalog(self, db_path):
        sid = _seed(db_path, "v0.5.0")
        assert _row(db_path, sid)["follows_bundled_pin"] == 1

    def test_a_fork_seed_never_follows(self, db_path):
        """A fork may not carry the platform's next tag."""
        sid = _seed(db_path, "v0.5.0", url="github.com/example/skills-fork")
        assert _row(db_path, sid)["follows_bundled_pin"] == 0

    def test_engine_seed_marks_the_builtin_catalog(self, db_path, monkeypatch):
        import config
        import database as D

        monkeypatch.setattr(config, "DEFAULT_SKILL_SOURCE_URL", BUILTIN_URL)
        monkeypatch.setattr(config, "DEFAULT_SKILL_SOURCE_REF", "v0.5.0")
        D._seed_fresh_install_skill_source_engine()

        row = sqlite3.connect(db_path).execute(
            "SELECT follows_bundled_pin FROM skill_sources WHERE is_default = 1"
        ).fetchone()
        assert row == (1,)


# =============================================================================
# AC4 — the next sync over real git, and the in-flight race
# =============================================================================

@pytest.fixture
def upstream(tmp_path) -> Path:
    """A catalog with two ANNOTATED release tags (#2550: every trinity-skills
    release tag is annotated, and a lightweight-only fixture hides that class)."""
    repo = tmp_path / "upstream"
    skill = repo / ".claude" / "skills" / "alpha"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: alpha\ndescription: a\n---\na\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "v0.3.0")
    _git(repo, "tag", "-a", "v0.3.0", "-m", "v0.3.0")
    skill = repo / ".claude" / "skills" / "beta"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: beta\ndescription: b\n---\nb\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "v0.5.0")
    _git(repo, "tag", "-a", "v0.5.0", "-m", "v0.5.0")
    return repo


@pytest.fixture
def service(facade, tmp_path, monkeypatch):
    import services.skill_service as ss

    monkeypatch.setattr(ss, "validate_skills_library_url", lambda u: u)
    monkeypatch.setattr(ss, "get_skills_library_url", lambda: None)
    monkeypatch.setattr(ss, "get_skills_library_branch", lambda: "main")
    svc = ss.SkillService()
    svc.library_root = tmp_path / "clones"
    svc.library_path = tmp_path / "clones"
    monkeypatch.setattr(svc, "_normalized_url", lambda url: url, raising=False)
    return svc


def _seed_local(db_path, upstream, pin) -> str:
    """The seed, pointed at a file:// upstream (shallow clone, real fetches)."""
    url = f"file://{upstream}"
    sid = _seed(db_path, "v0.3.0")
    _set_columns(db_path, sid, url=url)
    pin("v0.3.0", url=url)
    return sid


def _tag_commit(upstream: Path, tag: str) -> str:
    return _git(upstream, "rev-parse", f"refs/tags/{tag}^{{commit}}").stdout.strip()


def _is_commit(recorded, upstream: Path, tag: str) -> bool:
    """The platform records an abbreviated SHA; compare by prefix."""
    return bool(recorded) and _tag_commit(upstream, tag).startswith(recorded)


class TestSyncAfterFollow:
    @pytest.mark.parametrize("lost_checkout", [False, True], ids=["update-path", "clone-path"])
    def test_the_next_sync_fetches_the_new_tag_as_a_moved_commit(
        self, db_path, facade, pin, service, upstream, lost_checkout
    ):
        import shutil

        sid = _seed_local(db_path, upstream, pin)
        first = service.sync_library()
        assert first["success"] is True, first
        assert _is_commit(_row(db_path, sid)["last_commit_sha"], upstream, "v0.3.0")
        if lost_checkout:
            # The case `_refuse_moved_pin_after_clone` guards: no checkout left.
            shutil.rmtree(service.library_root / sid)

        pin("v0.5.0")
        assert _follow() is not None
        result = service.sync_library()

        entry = next(s for s in result["sources"] if s["source_id"] == sid)
        assert entry["success"] is True, entry
        assert not entry.get("moved_tag"), entry
        assert _is_commit(entry["commit_sha"], upstream, "v0.5.0")
        assert entry["commit_changed"] is True
        assert result["commit_changed"] is True
        assert sorted(s["name"] for s in service.list_skills()) == ["alpha", "beta"]

    def test_run_cycle_reinjects_the_fleet_after_a_follow(
        self, db_path, facade, pin, service, upstream, monkeypatch
    ):
        """AC4 through the real ent#236 cycle, not just the sync's return value."""
        import services.skills_sync_service as sss

        sid = _seed_local(db_path, upstream, pin)
        service.sync_library()
        pin("v0.5.0")
        _follow()

        cycle = sss.SkillsLibrarySyncService()
        monkeypatch.setattr(sss, "skill_service", service)
        monkeypatch.setattr(sss, "is_skills_auto_reinject_enabled", lambda: True)
        monkeypatch.setattr(cycle, "_audit_sync", AsyncMock())
        reinject = AsyncMock(return_value={"agents": 0})
        monkeypatch.setattr(cycle, "run_fleet_reinject", reinject)

        out = asyncio.run(cycle.run_cycle())

        assert out["commit_changed"] is True, out
        reinject.assert_awaited_once()
        assert _is_commit(reinject.await_args.kwargs["commit_sha"], upstream, "v0.5.0")
        assert _row(db_path, sid)["ref"] == "v0.5.0"

    def test_a_sync_in_flight_across_the_follow_cannot_stamp_the_old_commit(
        self, db_path, ops, facade, pin, service, upstream, monkeypatch
    ):
        """The race: a sync admitted at v0.3.0 finishes after the follow.

        Its success write used to land by id alone, putting v0.3.0's commit on a
        row now at v0.5.0, and every later sync compared v0.5.0 against it and
        was refused as `moved_tag`, permanently.
        """
        import services.skill_service as ss

        sid = _seed_local(db_path, upstream, pin)
        service.sync_library()
        real_sync = ss.SkillSourceClone.sync

        def follow_mid_sync(self, *args, **kwargs):
            outcome = real_sync(self, *args, **kwargs)
            ops.follow_bundled_pin(
                sid, from_ref="v0.3.0", to_ref="v0.5.0", url=f"file://{upstream}"
            )
            return outcome

        monkeypatch.setattr(ss.SkillSourceClone, "sync", follow_mid_sync)
        service.sync_library()
        monkeypatch.setattr(ss.SkillSourceClone, "sync", real_sync)

        row = _row(db_path, sid)
        assert row["ref"] == "v0.5.0"
        assert row["last_commit_sha"] is None, "the stale sync stamped the old commit"
        assert row["last_sync_status"] == "never"

        result = service.sync_library()
        entry = next(s for s in result["sources"] if s["source_id"] == sid)
        assert entry["success"] is True, entry
        assert _is_commit(entry["commit_sha"], upstream, "v0.5.0")


def _admin():
    from models import User

    return User(id=1, username="admin", role="admin")


class TestRowSyncAfterFollow:
    """After a follow the row shows "Never synced" beside its own Sync button,
    which is the click an admin makes. That route used to record the moved
    commit without starting the fleet re-inject, so the signal was spent and
    the next full sync or scheduled cycle saw nothing to propagate: running
    agents kept the old tag's skills until they restarted. Both sync routes now
    start the re-inject the same way when the commit moved and automatic
    re-inject is on (the `/review` finding on PR #4091)."""

    @pytest.fixture
    def routes(self, facade, service, monkeypatch):
        import routers.skills as rs
        import services.settings_service as settings
        import services.skills_sync_service as sss

        monkeypatch.setattr(rs, "db", facade)
        monkeypatch.setattr(rs, "skill_service", service)
        reinject = AsyncMock(return_value={"agents_total": 0})
        monkeypatch.setattr(sss.skills_sync_service, "run_fleet_reinject", reinject)

        def auto_reinject(on):
            monkeypatch.setattr(settings, "is_skills_auto_reinject_enabled", lambda: on)

        return rs, reinject, auto_reinject

    @staticmethod
    def _call(route, **kwargs):
        async def go():
            out = await route(admin_user=_admin(), **kwargs)
            await asyncio.sleep(0)  # let the background sweep start
            return out

        return asyncio.run(go())

    @pytest.mark.parametrize("which", ["row", "all"])
    def test_the_sync_after_a_follow_reinjects_the_fleet(
        self, db_path, pin, service, upstream, routes, which
    ):
        rs, reinject, auto_reinject = routes
        sid = _seed_local(db_path, upstream, pin)
        service.sync_library()
        pin("v0.5.0")
        assert _follow() is not None
        auto_reinject(True)

        if which == "row":
            out = self._call(rs.sync_skill_source, source_id=sid)
        else:
            out = self._call(rs.sync_library)

        assert out["commit_changed"] is True, out
        assert out["fleet_reinject_started"] is True
        reinject.assert_awaited_once()
        assert _is_commit(reinject.await_args.kwargs["commit_sha"], upstream, "v0.5.0")

    @pytest.mark.parametrize("which", ["row", "all"])
    def test_no_sweep_when_automatic_reinject_is_off(
        self, db_path, pin, service, upstream, routes, which
    ):
        rs, reinject, auto_reinject = routes
        sid = _seed_local(db_path, upstream, pin)
        service.sync_library()
        pin("v0.5.0")
        _follow()
        auto_reinject(False)

        if which == "row":
            out = self._call(rs.sync_skill_source, source_id=sid)
        else:
            out = self._call(rs.sync_library)

        assert out["commit_changed"] is True
        assert out["fleet_reinject_started"] is False
        reinject.assert_not_called()

    def test_no_sweep_when_the_commit_did_not_move(
        self, db_path, pin, service, upstream, routes
    ):
        rs, reinject, auto_reinject = routes
        sid = _seed_local(db_path, upstream, pin)
        service.sync_library()
        auto_reinject(True)

        out = self._call(rs.sync_skill_source, source_id=sid)

        assert out["commit_changed"] is False
        assert out["fleet_reinject_started"] is False
        reinject.assert_not_called()


class TestAdminEditAcrossTheFollow:
    """`update_source` decides "did the target change?" from a read. When the
    boot follow commits between that read and the write, the write must be
    re-decided against the row as it now is, or a save naming the old ref
    puts it back while keeping the follow's marker and record (the panel then
    read "v0.3.0 → v0.3.0" and the next boot moved it again)."""

    @staticmethod
    def _follow_inside_the_read(ops, monkeypatch):
        real = ops.get_source
        state = {"followed": False}

        def read_then_follow(source_id):
            row = real(source_id)
            if not state["followed"]:
                state["followed"] = True
                ops.follow_bundled_pin(
                    source_id, from_ref="v0.3.0", to_ref="v0.5.0", url=BUILTIN_URL
                )
            return row

        monkeypatch.setattr(ops, "get_source", read_then_follow)

    def test_a_stale_save_of_the_old_ref_is_a_retarget(self, db_path, ops, monkeypatch):
        sid = _seed(db_path, "v0.3.0")
        self._follow_inside_the_read(ops, monkeypatch)

        ops.update_source(sid, ref="v0.3.0", name="Ours")

        row = _row(db_path, sid)
        assert (row["ref"], row["name"]) == ("v0.3.0", "Ours")
        assert row["follows_bundled_pin"] == 0
        assert (row["followed_from_ref"], row["followed_at"]) == (None, None)
        assert row["last_sync_status"] == "never"

    def test_a_rename_across_the_follow_keeps_the_follow(self, db_path, ops, monkeypatch):
        sid = _seed(db_path, "v0.3.0")
        self._follow_inside_the_read(ops, monkeypatch)

        ops.update_source(sid, name="Ours")

        row = _row(db_path, sid)
        assert (row["ref"], row["name"]) == ("v0.5.0", "Ours")
        assert (row["follows_bundled_pin"], row["followed_from_ref"]) == (1, "v0.3.0")


# =============================================================================
# Backfill — rows seeded before the marker existed
# =============================================================================

_PRE_529_SKILL_SOURCES = """
    CREATE TABLE skill_sources (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        url TEXT NOT NULL,
        ref TEXT NOT NULL DEFAULT 'main',
        ref_type TEXT NOT NULL DEFAULT 'branch',
        is_default INTEGER NOT NULL DEFAULT 0,
        enabled INTEGER NOT NULL DEFAULT 1,
        priority INTEGER NOT NULL DEFAULT 100,
        last_sync_at TEXT,
        last_sync_status TEXT,
        last_commit_sha TEXT,
        last_error TEXT,
        created_by TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(url, ref)
    )
"""


class TestBackfill:
    @pytest.fixture
    def legacy(self, tmp_path):
        """An install at the pre-ent#529 schema, with rows of every kind."""
        from db.schema import init_schema

        conn = sqlite3.connect(tmp_path / "legacy.db")
        init_schema(conn.cursor(), conn)
        conn.execute("DROP TABLE skill_sources")
        conn.execute(_PRE_529_SKILL_SOURCES)

        def add(sid, url=BUILTIN_URL, ref="v0.3.0", ref_type="tag", is_default=1,
                enabled=1, created_by="seed:ent237"):
            conn.execute(
                "INSERT INTO skill_sources (id, name, url, ref, ref_type, is_default, "
                "enabled, priority, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 1000, ?, 'x', 'x')",
                (sid, sid, url, ref, ref_type, is_default, enabled, created_by),
            )

        def audit(sid, details, action="skill_source_update"):
            conn.execute(
                "INSERT INTO audit_log (event_id, event_type, event_action, actor_type, "
                "target_type, target_id, timestamp, details, source) "
                "VALUES (?, 'configuration', ?, 'user', 'skill_source', ?, 'x', ?, 'api')",
                (f"e-{uuid.uuid4().hex}", action, sid, json.dumps(details)),
            )

        conn.commit()
        return conn, add, audit

    def _run(self, conn):
        from db.migrations import _migrate_skill_sources_follow_bundled_pin

        _migrate_skill_sources_follow_bundled_pin(conn.cursor(), conn)
        return {
            r[0]: r[1]
            for r in conn.execute("SELECT id, follows_bundled_pin FROM skill_sources")
        }

    @pytest.mark.parametrize("ref", ["v0.1.0", "v0.2.0", "v0.3.0", "v0.5.0"])
    def test_an_untouched_seed_on_any_bundled_pin_follows(self, legacy, ref):
        conn, add, _ = legacy
        add("seeded", ref=ref)
        assert self._run(conn) == {"seeded": 1}

    def test_a_toggle_or_rename_in_the_audit_does_not_opt_out(self, legacy):
        """Only a retarget opts out; the panel's toggle audits `enabled`."""
        conn, add, audit = legacy
        add("toggled")
        audit("toggled", {"changed": ["enabled"]})
        audit("toggled", {"changed": ["name"]})
        assert self._run(conn) == {"toggled": 1}

    def test_a_disabled_untouched_seed_is_marked_and_stays_paused(self, legacy):
        conn, add, _ = legacy
        add("paused", enabled=0)
        assert self._run(conn) == {"paused": 1}

    @pytest.mark.parametrize(
        "details",
        [{"changed": ["ref"]}, {"changed": ["enabled", "url"]},
         {"changed": ["ref_type"]}, {"named": ["ref"], "via": "apply"}],
        ids=["put-ref", "put-url", "put-ref_type", "apply-ref"],
    )
    def test_an_audited_retarget_opts_out(self, legacy, details):
        conn, add, audit = legacy
        add("retargeted")
        audit("retargeted", details)
        assert self._run(conn) == {"retargeted": 0}

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"ref": "v0.4.0"},                       # never a bundled pin
            {"ref": "main", "ref_type": "branch"},
            {"url": "github.com/example/skills-fork"},
            {"is_default": 0, "created_by": "1"},    # a custom source
            {"created_by": "legacy"},
        ],
        ids=["off-history-ref", "branch", "fork", "custom", "not-the-seed"],
    )
    def test_rows_that_are_not_the_untouched_seed_stay_off(self, legacy, kwargs):
        conn, add, _ = legacy
        add("other", **kwargs)
        assert self._run(conn) == {"other": 0}

    def test_idempotent(self, legacy):
        conn, add, _ = legacy
        add("seeded")
        self._run(conn)
        assert self._run(conn) == {"seeded": 1}

    def test_a_fresh_install_records_the_migration_on_the_second_pass(self, tmp_path):
        """init_database's order: migrations (tables absent) → schema → migrations."""
        from db.migrations import run_all_migrations
        from db.schema import init_schema

        conn = sqlite3.connect(tmp_path / "fresh.db")
        cur = conn.cursor()
        run_all_migrations(cur, conn)
        init_schema(cur, conn)
        run_all_migrations(cur, conn)

        applied = {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}
        assert "skill_sources_follow_bundled_pin" in applied
        cols = {r[1] for r in conn.execute("PRAGMA table_info(skill_sources)")}
        assert {"follows_bundled_pin", "followed_from_ref", "followed_at"} <= cols


# =============================================================================
# Boot wiring — the hand-off line, pinned (tdd question 4)
# =============================================================================

def test_boot_follows_before_the_auto_sync_loop_starts():
    """Delete the follow from `_start_skills_sync_delayed` and this goes red.

    AST over main.py rather than importing it (the whole backend), the same
    choice tests/unit/test_1028_lifespan_phases.py makes.
    """
    tree = ast.parse((_BACKEND / "main.py").read_text())
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_start_skills_sync_delayed"
    )
    order = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
            f = node.value.func
            if isinstance(f, ast.Attribute) and f.attr == "follow_bundled_pin":
                order.append(("follow", node.lineno))
        if isinstance(node, ast.Call):
            f = node.func
            if (isinstance(f, ast.Attribute) and f.attr == "start"
                    and isinstance(f.value, ast.Name) and f.value.id == "skills_sync_service"):
                order.append(("start", node.lineno))
    order.sort(key=lambda x: x[1])
    assert [name for name, _ in order] == ["follow", "start"], order


def test_the_real_db_facade_reaches_both_new_paths(db_path, ops):
    """The service talks to `database.db`; every other test injects a facade.

    A misnamed pass-through on `DatabaseManager` would make the follow a silent
    no-op in production (it never raises), so the real methods are executed
    here against the real operations object.
    """
    from types import SimpleNamespace

    import database as D

    manager = SimpleNamespace(_skill_sources_ops=ops)
    sid = _seed(db_path, "v0.3.0")

    moved = D.DatabaseManager.follow_skill_source_pin(
        manager, sid, from_ref="v0.3.0", to_ref="v0.5.0", url=BUILTIN_URL
    )
    assert moved is not None and moved.ref == "v0.5.0"

    D.DatabaseManager.record_skill_source_sync(
        manager, sid, success=True, commit_sha="a" * 12,
        url=BUILTIN_URL, ref="v0.3.0", ref_type="tag",
    )
    assert _row(db_path, sid)["last_commit_sha"] is None, "the stale outcome landed"
