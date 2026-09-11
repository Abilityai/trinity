"""Unit tests for trinity-enterprise#236 — skills library lifecycle automation.

Three features, each with its own failure surface:

1. **Removal-on-unassign** — `compute_removal` (pure) and `remove_skills`
   (orchestration). The load-bearing property is *what is NOT deleted*: an
   unmanaged directory, an agent-authored file, anything outside the skill's own
   prefix.
2. **Start-path reconciliation** — how a stopped agent learns about an unassign,
   plus the blast-radius refusal that stops a wiped `agent_skills` table from
   erasing every package in the fleet.
3. **Scheduled auto-sync + fleet re-inject** — durable sync status (the
   `--workers 2` gap), commit-changed gating, ghost/stopped exclusion, and
   skip-and-report on lock contention.

Harness follows test_ent183_skill_packages.py: file-location loads for pure
modules, and heavy backend deps stubbed ONLY when a real module isn't already
imported (so a combined run keeps its real modules — the #762 class).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import types as _types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = _PROJECT_ROOT / "src" / "backend"

_TMP_DB = Path(tempfile.gettempdir()) / "trinity_test_ent236.db"
os.environ.setdefault("TRINITY_DB_PATH", str(_TMP_DB))
# `config.py` refuses to import without credentialed Redis (#589/#645). Set a
# dummy so the REAL settings_service can be imported below — the interval clamp
# is a guard, and a guard verified only against a hand-written stub is not
# verified at all.
os.environ.setdefault("REDIS_URL", "redis://test:test@localhost:6379")

if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


def _load(path: str, name: str):
    spec = importlib.util.spec_from_file_location(name, str(_PROJECT_ROOT / path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pkg = _load("src/backend/services/skill_packaging.py", "_ent236_pkg")


def _as_cloned_repo(service):
    """Put the library path in the state 'already cloned' means on disk.

    `sync_library` selects pull-vs-clone on `.git` being a directory, not on the
    path merely existing — a path without `.git` is a broken clone and must be
    re-cloned, not pulled into. Tests that mean "the library is already here"
    therefore have to create `.git`, or they silently exercise the clone path
    (and, with `_git_clone` unmocked, reach for the network).
    """
    (service.library_path / ".git").mkdir(parents=True, exist_ok=True)


# ─────────────────────────────────────────────────────────────────────────────
# Part A — compute_removal (pure)
# ─────────────────────────────────────────────────────────────────────────────

class TestComputeRemoval:
    def test_removes_every_manifest_path_plus_meta(self):
        manifest = [
            ".claude/skills/clip/SKILL.md",
            ".claude/skills/clip/scripts/run.sh",
        ]
        paths, truncated = pkg.compute_removal(manifest, "clip")
        assert truncated is False
        assert set(paths) == set(manifest) | {
            ".claude/skills/clip/.trinity-skill.json"
        }

    def test_meta_is_last(self):
        """Ordering mirrors build_injection_tar: while the meta exists the
        package is still platform-managed, so an interrupted removal resumes."""
        manifest = [f".claude/skills/clip/f{i}.txt" for i in range(5)]
        paths, _ = pkg.compute_removal(manifest, "clip")
        assert paths[-1] == ".claude/skills/clip/.trinity-skill.json"

    def test_missing_or_unusable_manifest_still_removes_the_meta(self):
        """The caller only reaches compute_removal for a package that HAS a meta
        (unmanaged dirs are filtered one level up), so the marker provably
        exists. Leaving it behind would keep the skill in every future
        reconcile's inventory with nothing left to delete — an unremovable
        package retried on every agent start."""
        for empty in (None, [], "not-a-list", {}):
            assert pkg.compute_removal(empty, "clip") == (
                [".claude/skills/clip/.trinity-skill.json"], False
            )

    def test_paths_outside_the_skill_prefix_are_dropped(self):
        """The manifest is read back from AGENT-side state, i.e. untrusted: a
        doctored one must not steer a backend-driven delete anywhere else."""
        manifest = [
            ".claude/skills/clip/SKILL.md",
            ".claude/skills/other/SKILL.md",
            ".env",
            "/etc/passwd",
            ".claude/skills/clip/../../../.ssh/id_rsa",
        ]
        paths, _ = pkg.compute_removal(manifest, "clip")
        assert paths == [
            ".claude/skills/clip/SKILL.md",
            ".claude/skills/clip/.trinity-skill.json",
        ]

    def test_truncation_keeps_the_meta(self):
        """Dropping the meta while files remain would strand them as unmanaged
        orphans no later prune could ever reach."""
        manifest = [
            f".claude/skills/clip/f{i:04d}.txt"
            for i in range(pkg.PRUNE_CAP_PER_SKILL + 50)
        ]
        paths, truncated = pkg.compute_removal(manifest, "clip")
        assert truncated is True
        assert len(paths) == pkg.PRUNE_CAP_PER_SKILL
        assert ".claude/skills/clip/.trinity-skill.json" not in paths

    def test_garbage_manifest_still_drops_the_meta(self):
        """Otherwise the directory stays 'managed' forever with nothing
        removable, and every start would re-attempt the same no-op removal."""
        paths, truncated = pkg.compute_removal(["../../etc/passwd"], "clip")
        assert truncated is False
        assert paths == [".claude/skills/clip/.trinity-skill.json"]


# ─────────────────────────────────────────────────────────────────────────────
# Service-level harness
# ─────────────────────────────────────────────────────────────────────────────

_STUBBED_MODULE_NAMES = [
    "database",
    "services.settings_service",
    "services.agent_client",
    "utils.url_validation",
    "redis_breaker_util",
    "services.docker_service",
]


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    saved = {name: sys.modules.get(name) for name in _STUBBED_MODULE_NAMES}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


# `database` must be stubbed BEFORE attempting the real settings_service import
# below (it reaches `database.db` at module scope).
if "database" not in sys.modules:
    _db_mod = _types.ModuleType("database")
    _db_mod.db = MagicMock()
    sys.modules["database"] = _db_mod

# Prefer the REAL settings_service so the interval clamp is exercised against
# production code. Falls back to the stub if the import can't be satisfied in
# this environment (the clamp tests then skip rather than pass vacuously).
try:  # noqa: SIM105
    import services.settings_service as _real_settings  # noqa: F401
except Exception:  # noqa: BLE001
    pass

_STUBS = {
    "database": {"db": MagicMock()},
    "services.settings_service": {
        "get_skills_library_url": lambda: "https://github.com/owner/repo",
        "get_skills_library_branch": lambda: "main",
        "get_github_pat": lambda: None,
        "settings_service": MagicMock(),
        "is_skills_auto_sync_enabled": lambda: False,
        "is_skills_auto_reinject_enabled": lambda: False,
        "get_skills_auto_sync_interval": lambda: 3600,
        # The stub can outlive this module in a combined run, and
        # `routers/settings.py` imports these by name — a stub missing them
        # turns into an ImportError in an unrelated test file.
        "SKILLS_AUTO_SYNC_ENABLED_KEY": "skills_library_auto_sync_enabled",
        "SKILLS_AUTO_SYNC_INTERVAL_KEY": "skills_library_auto_sync_interval_seconds",
        "SKILLS_AUTO_REINJECT_ENABLED_KEY": "skills_library_auto_reinject_enabled",
        "SKILLS_AUTO_SYNC_INTERVAL_DEFAULT": 3600,
        "SKILLS_AUTO_SYNC_INTERVAL_MIN": 300,
        "SKILLS_AUTO_SYNC_INTERVAL_MAX": 86400,
    },
    "services.agent_client": {
        "get_agent_client": MagicMock(),
        "AgentClientError": type("AgentClientError", (Exception,), {}),
    },
    # Must carry EVERY name `skill_service` imports from this module — the stub
    # is installed only when the key is absent from sys.modules, so a missing
    # name fails by FILE ORDER rather than deterministically (ent#237 added
    # `ALLOWED_SKILLS_LIBRARY_HOSTS` to that import list).
    "utils.url_validation": {
        "validate_skills_library_url": lambda url: url,
        "ALLOWED_SKILLS_LIBRARY_HOSTS": {"github.com", "www.github.com"},
        "EmbeddedCredentialError": type("EmbeddedCredentialError", (ValueError,), {}),
        "reject_embedded_credentials": lambda url: url,
    },
    # `get_breaker_redis` is unused by skill_service (it holds its own client),
    # but skills_sync_service imports it here; `SingleFlightLock` (#1920) is now
    # imported by skill_service's lock helpers, so the stub must carry it or a
    # 'redis_breaker_util-stub-first' ordering fails skill_service's import. The
    # fake fails open (no Redis in unit tests → sole-worker acquire, no-op
    # release) so the REAL `_acquire/_release_sync_lock` still run in the
    # status-persistence tests.
    "redis_breaker_util": {
        "get_breaker_redis": lambda: None,
        "SingleFlightLock": type(
            "SingleFlightLock",
            (),
            {
                "__init__": lambda self, *a, **kw: None,
                "acquire": lambda self: True,
                "release_if_owned": lambda self: None,
                "held": False,
            },
        ),
    },
    "services.docker_service": {
        "list_all_agents_fast": lambda: [],
        "execute_command_in_container": AsyncMock(),
    },
}
for _name, _attrs in _STUBS.items():
    if _name not in sys.modules:
        _mod = _types.ModuleType(_name)
        for _k, _v in _attrs.items():
            setattr(_mod, _k, _v)
        sys.modules[_name] = _mod

import services.skill_service as skill_service_module  # noqa: E402
from services.skill_service import SkillService, SkillInjectionBusy  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def service(tmp_path):
    svc = SkillService()
    svc.library_path = tmp_path / "library"
    # ent#237: sync is per-source under `library_root`; without this the
    # multi-source path reaches for the real `/data` (read-only off-container).
    svc.library_root = tmp_path / "library"
    (svc.library_path / ".claude" / "skills").mkdir(parents=True)
    # No Redis in unit tests → the injection lock fails open (returns None).
    svc._acquire_inject_lock = MagicMock(return_value=None)
    svc._release_inject_lock = MagicMock()
    return svc


def _meta(manifest, version="v1"):
    return {"version": version, "commit": "abc", "manifest": manifest}


# ─────────────────────────────────────────────────────────────────────────────
# Part B — remove_skills orchestration
# ─────────────────────────────────────────────────────────────────────────────

class TestRemoveSkills:
    def test_deletes_manifest_paths_and_reports_removed(self, service):
        manifest = [
            ".claude/skills/clip/SKILL.md",
            ".claude/skills/clip/scripts/run.sh",
        ]
        deleted: list = []

        service._read_agent_skill_metas = AsyncMock(
            return_value={"clip": {"exists": True, "meta": _meta(manifest)}}
        )
        service._finalize_removed_dirs = AsyncMock(return_value={"errors": []})
        service._rebuild_claude_md_after_removal = AsyncMock()

        async def _delete(_client, path):
            deleted.append(path)
            return True

        with patch.object(SkillService, "_delete_agent_file", staticmethod(_delete)):
            result = _run(service.remove_skills("a1", ["clip"]))

        assert result["success"] is True
        assert result["skills_removed"] == 1
        assert result["results"]["clip"]["status"] == "removed"
        assert result["results"]["clip"]["files_deleted"] == 3
        assert set(deleted) == set(manifest) | {
            ".claude/skills/clip/.trinity-skill.json"
        }

    def test_unmanaged_directory_is_never_deleted(self, service):
        """No meta ⇒ the platform never wrote it (an agent-authored Playbook of
        the same name). Overwrite is recoverable; deletion is not."""
        service._read_agent_skill_metas = AsyncMock(
            return_value={"mine": {"exists": True, "meta": None}}
        )
        service._finalize_removed_dirs = AsyncMock(return_value={"errors": []})
        service._rebuild_claude_md_after_removal = AsyncMock()

        delete = AsyncMock(return_value=True)
        with patch.object(SkillService, "_delete_agent_file", staticmethod(delete)):
            result = _run(service.remove_skills("a1", ["mine"]))

        assert result["results"]["mine"]["status"] == "not_managed"
        assert "unmanaged_dir_kept" in result["results"]["mine"]["warnings"]
        delete.assert_not_called()

    def test_absent_directory_is_a_clean_noop(self, service):
        service._read_agent_skill_metas = AsyncMock(
            return_value={"gone": {"exists": False, "meta": None}}
        )
        service._finalize_removed_dirs = AsyncMock(return_value={"errors": []})
        service._rebuild_claude_md_after_removal = AsyncMock()

        result = _run(service.remove_skills("a1", ["gone"]))
        assert result["success"] is True
        assert result["results"]["gone"]["status"] == "not_present"

    def test_probe_failure_defers_instead_of_claiming_success(self, service):
        """`_read_agent_skill_metas` returns {} on ANY exec failure. Reporting
        `not_present` there would claim a removal that never happened."""
        service._read_agent_skill_metas = AsyncMock(return_value={})

        result = _run(service.remove_skills("a1", ["clip"]))
        assert result["success"] is False
        assert result["results"]["clip"]["status"] == "deferred"
        assert "removal_deferred:probe_unavailable" in result["results"]["clip"]["warnings"]

    def test_delete_failure_is_reported_not_swallowed(self, service):
        service._read_agent_skill_metas = AsyncMock(
            return_value={
                "clip": {"exists": True, "meta": _meta([".claude/skills/clip/SKILL.md"])}
            }
        )
        service._finalize_removed_dirs = AsyncMock(return_value={"errors": []})
        service._rebuild_claude_md_after_removal = AsyncMock()

        with patch.object(
            SkillService, "_delete_agent_file", staticmethod(AsyncMock(return_value=False))
        ):
            result = _run(service.remove_skills("a1", ["clip"]))

        assert result["success"] is False
        assert result["results"]["clip"]["status"] == "partial"
        assert any(
            w.startswith("stale_delete_failed:")
            for w in result["results"]["clip"]["warnings"]
        )

    def test_partial_failure_keeps_the_meta_and_the_gitignore_line(self, service):
        """Two guards that only matter together: the package must stay
        platform-managed (retryable), and the ignore line must survive while
        injected files do — otherwise the 15-min auto-sync commits them."""
        manifest = [".claude/skills/clip/SKILL.md", ".claude/skills/clip/big.bin"]
        attempted: list = []

        async def _delete(_client, path):
            attempted.append(path)
            return not path.endswith("big.bin")  # one file refuses to go

        service._read_agent_skill_metas = AsyncMock(
            return_value={"clip": {"exists": True, "meta": _meta(manifest)}}
        )
        finalize = AsyncMock(return_value={"errors": []})
        service._finalize_removed_dirs = finalize
        service._rebuild_claude_md_after_removal = AsyncMock()

        with patch.object(SkillService, "_delete_agent_file", staticmethod(_delete)):
            result = _run(service.remove_skills("a1", ["clip"]))

        assert result["results"]["clip"]["status"] == "partial"
        assert "removal_incomplete_meta_kept" in result["results"]["clip"]["warnings"]
        assert ".claude/skills/clip/.trinity-skill.json" not in attempted
        # finalize still reaps emptied dirs, but clears NO gitignore lines
        assert finalize.await_args.args[1] == []

    def test_truncated_removal_keeps_the_gitignore_line(self, service):
        manifest = [
            f".claude/skills/clip/f{i:04d}.txt"
            for i in range(pkg.PRUNE_CAP_PER_SKILL + 10)
        ]
        service._read_agent_skill_metas = AsyncMock(
            return_value={"clip": {"exists": True, "meta": _meta(manifest)}}
        )
        finalize = AsyncMock(return_value={"errors": []})
        service._finalize_removed_dirs = finalize
        service._rebuild_claude_md_after_removal = AsyncMock()

        with patch.object(
            SkillService, "_delete_agent_file", staticmethod(AsyncMock(return_value=True))
        ):
            result = _run(service.remove_skills("a1", ["clip"]))

        assert "removal_truncated" in result["results"]["clip"]["warnings"]
        assert finalize.await_args.args[1] == []

    def test_traversal_name_never_reaches_the_agent(self, service):
        service._read_agent_skill_metas = AsyncMock(return_value={})
        result = _run(service.remove_skills("a1", ["../../etc", ".", ""]))
        assert result["skills_removed"] == 0
        assert result["results"] == {}
        service._read_agent_skill_metas.assert_not_called()

    def test_refuses_to_remove_a_still_assigned_skill(self, service, monkeypatch):
        """The bulk PUT computes `previous - new` BEFORE taking the lock, so two
        concurrent PUTs can each hand us a name the other just re-assigned.
        Without this re-read the agent silently loses a skill it should have."""
        monkeypatch.setattr(
            skill_service_module.db, "get_agent_skill_names",
            MagicMock(return_value=["keep"]), raising=False,
        )
        probe = AsyncMock(return_value={})
        service._read_agent_skill_metas = probe

        result = _run(service.remove_skills("a1", ["keep"]))

        assert result["success"] is True
        assert result["results"]["keep"]["status"] == "still_assigned"
        probe.assert_not_called()  # never even looked at the agent

    def test_still_assigned_entries_survive_a_probe_failure(self, service, monkeypatch):
        """Both outcomes must be reported — the early `deferred` return used to
        rebuild `results` from scratch and drop the skipped names."""
        monkeypatch.setattr(
            skill_service_module.db, "get_agent_skill_names",
            MagicMock(return_value=["keep"]), raising=False,
        )
        service._read_agent_skill_metas = AsyncMock(return_value={})

        result = _run(service.remove_skills("a1", ["keep", "drop"]))

        assert result["results"]["keep"]["status"] == "still_assigned"
        assert result["results"]["drop"]["status"] == "deferred"
        assert result["skills_failed"] == 1

    def test_takes_the_injection_lock(self, service):
        """Removal and injection both mutate ~/.claude/skills and CLAUDE.md, so
        they must serialize on the same per-agent lock."""
        service._acquire_inject_lock = MagicMock(
            side_effect=SkillInjectionBusy("busy")
        )
        with pytest.raises(SkillInjectionBusy):
            _run(service.remove_skills("a1", ["clip"]))


# ─────────────────────────────────────────────────────────────────────────────
# Part C — start-path reconciliation
# ─────────────────────────────────────────────────────────────────────────────

class TestReconcile:
    def test_removes_managed_skills_that_are_no_longer_assigned(self, service):
        service._list_managed_skills = AsyncMock(return_value=["keep", "drop"])
        service.remove_skills = AsyncMock(
            return_value={"skills_removed": 1, "skills_failed": 0, "results": {}}
        )
        service._audit_removal = AsyncMock()

        result = _run(service.reconcile_agent_skills("a1", ["keep"]))

        assert result["status"] == "reconciled"
        assert result["skills"] == ["drop"]
        service.remove_skills.assert_awaited_once_with("a1", ["drop"])

    def test_zero_assigned_still_reconciles(self, service):
        """The "unassigned the last skill while stopped" case — an early return
        on an empty assignment set would strand that package forever."""
        service._list_managed_skills = AsyncMock(return_value=["orphan"])
        service.remove_skills = AsyncMock(
            return_value={"skills_removed": 1, "skills_failed": 0, "results": {}}
        )
        service._audit_removal = AsyncMock()

        result = _run(service.reconcile_agent_skills("a1", []))
        assert result["status"] == "reconciled"
        service.remove_skills.assert_awaited_once_with("a1", ["orphan"])

    def test_agent_authored_skills_are_invisible_to_reconcile(self, service):
        """Only directories carrying a generated meta are 'managed'; a Playbook
        the agent wrote itself is not in the inventory at all."""
        service._list_managed_skills = AsyncMock(return_value=["assigned"])
        service.remove_skills = AsyncMock()

        result = _run(service.reconcile_agent_skills("a1", ["assigned"]))
        assert result == {"status": "clean", "removed": 0}
        service.remove_skills.assert_not_called()

    def test_unreadable_inventory_removes_nothing(self, service):
        """None means 'could not look' — it must never read as 'nothing is
        assigned, remove everything'."""
        service._list_managed_skills = AsyncMock(return_value=None)
        service.remove_skills = AsyncMock()

        result = _run(service.reconcile_agent_skills("a1", []))
        assert result["status"] == "skipped"
        assert result["reason"] == "inventory_unavailable"
        service.remove_skills.assert_not_called()

    def test_refuses_over_the_blast_radius_cap(self, service):
        """A wiped agent_skills table looks exactly like a mass-unassign from
        here. Keeping files is the recoverable direction."""
        orphans = [f"skill{i}" for i in range(25)]
        service._list_managed_skills = AsyncMock(return_value=orphans)
        service.remove_skills = AsyncMock()
        service._announce_reconcile_refusal = MagicMock()

        result = _run(service.reconcile_agent_skills("a1", []))

        assert result["status"] == "refused"
        assert result["removed"] == 0
        assert result["orphans"] == 25
        service.remove_skills.assert_not_called()
        service._announce_reconcile_refusal.assert_called_once()

    def test_cap_is_env_tunable(self, service, monkeypatch):
        monkeypatch.setenv("SKILLS_RECONCILE_MAX_REMOVALS", "50")
        orphans = [f"skill{i}" for i in range(25)]
        service._list_managed_skills = AsyncMock(return_value=orphans)
        service.remove_skills = AsyncMock(
            return_value={"skills_removed": 25, "skills_failed": 0, "results": {}}
        )
        service._audit_removal = AsyncMock()

        result = _run(service.reconcile_agent_skills("a1", []))
        assert result["status"] == "reconciled"

    def test_busy_lock_is_a_skip_not_a_crash(self, service):
        service._list_managed_skills = AsyncMock(return_value=["drop"])
        service.remove_skills = AsyncMock(side_effect=SkillInjectionBusy("busy"))

        result = _run(service.reconcile_agent_skills("a1", []))
        assert result["status"] == "skipped"
        assert result["reason"] == "injection_already_running"

    def test_never_raises(self, service):
        """Reconciliation is cleanup — it must not be able to fail an agent start."""
        service._list_managed_skills = AsyncMock(side_effect=RuntimeError("boom"))
        result = _run(service.reconcile_agent_skills("a1", []))
        assert result["status"] == "skipped"

    def test_alarm_hosts_on_an_uncreatable_sentinel_agent(self):
        """A real agent name would inherit its owner into the alarm's ACL and
        the 5s queue-sync loop would write it into that agent's queue file."""
        from utils.helpers import sanitize_agent_name

        name = skill_service_module.RECONCILE_ALARM_AGENT_NAME
        assert name.startswith("_")
        assert sanitize_agent_name(name) != name

    def test_refusal_alarm_carries_counts_not_skill_names(self, service):
        """G-04's lesson: durable operator-visible state must not accumulate
        content just to be helpful."""
        orphans = [f"secret-skill-{i}" for i in range(25)]
        captured = {}

        def _create(agent_name, item):
            captured["agent"] = agent_name
            captured["item"] = item

        with patch.object(skill_service_module.db, "create_operator_queue_item", _create):
            service._announce_reconcile_refusal("a1", len(orphans), 10)

        blob = json.dumps(captured["item"])
        assert "secret-skill" not in blob
        assert captured["item"]["context"]["orphan_count"] == 25
        assert captured["item"]["expires_at"] is None


# ─────────────────────────────────────────────────────────────────────────────
# Part D — durable sync status
# ─────────────────────────────────────────────────────────────────────────────

class TestSyncStatusPersistence:
    @pytest.fixture(autouse=True)
    def _library_configured(self, monkeypatch):
        """Pin the library config on the SERVICE module's own bindings.

        `skill_service` does `from services.settings_service import ...`, so the
        names live here — patching them directly keeps these tests correct
        whether the settings module is the real one (combined run) or the stub
        (standalone), instead of silently reading a real, unconfigured install.
        """
        monkeypatch.setattr(
            skill_service_module, "get_skills_library_url",
            lambda: "https://github.com/owner/repo", raising=False,
        )
        monkeypatch.setattr(
            skill_service_module, "get_skills_library_branch",
            lambda: "main", raising=False,
        )
        monkeypatch.setattr(
            skill_service_module, "get_github_pat", lambda: None, raising=False,
        )
        monkeypatch.setattr(
            skill_service_module, "validate_skills_library_url",
            lambda url: url, raising=False,
        )

    @pytest.fixture()
    def store(self, monkeypatch):
        """Settings-row store backed by monkeypatch, NOT bare assignment.

        `skill_service_module.db` is a process-wide singleton shared with every
        other test module, so assigning onto it directly leaks these fakes into
        unrelated suites for the rest of the session (the #762 class — it broke
        test_terminal_write_cas_gate exactly this way). `default` is a keyword
        in the real accessor's signature, so the fake must accept it as one.
        """
        data: dict = {}
        db = skill_service_module.db
        monkeypatch.setattr(
            db, "set_setting",
            MagicMock(side_effect=lambda k, v: data.__setitem__(k, v)),
            raising=False,
        )
        monkeypatch.setattr(
            db, "get_setting_value",
            MagicMock(side_effect=lambda k, default=None: data.get(k, default)),
            raising=False,
        )
        return data

    @pytest.fixture()
    def one_source(self, monkeypatch, service):
        """One enabled source, with a scriptable clone.

        ent#237 moved the git half out of `SkillService` into `SkillSourceClone`,
        so these tests can no longer mock `service._git_pull`. They drive the
        real `_sync_sources_locked` — which is what actually writes the durable
        status — and script only the per-source git outcome.
        """
        src = _types.SimpleNamespace(
            id="src_" + "a" * 12, name="Community",
            url="https://github.com/owner/repo", ref="main", ref_type="branch",
            is_default=True, enabled=True, priority=1000,
            last_commit_sha=None, last_sync_at=None,
            last_sync_status=None, last_error=None,
        )
        db = skill_service_module.db
        monkeypatch.setattr(db, "list_skill_sources",
                            MagicMock(return_value=[src]), raising=False)
        monkeypatch.setattr(db, "record_skill_source_sync",
                            MagicMock(), raising=False)

        outcome = {"success": True, "action": "pulled", "commit_sha": "deadbeef1234"}

        class _FakeClone:
            def __init__(self, *a, **kw):
                self.path = service.library_root / src.id
            def sync(self, auth_url, expected_sha=None):
                if isinstance(outcome, BaseException):
                    raise outcome
                return dict(outcome)
            def current_commit(self):
                return outcome.get("commit_sha") if isinstance(outcome, dict) else None

        monkeypatch.setattr(skill_service_module, "SkillSourceClone",
                            _FakeClone, raising=False)
        service.list_skills = MagicMock(return_value=[])

        def _script(**kw):
            nonlocal outcome
            if "raises" in kw:
                outcome = kw["raises"]
            else:
                outcome = kw
            return src

        return _types.SimpleNamespace(src=src, script=_script)

    def test_success_persists_status_and_commit(self, service, store, one_source):
        one_source.script(success=True, action="pulled", commit_sha="deadbeef1234")

        result = service.sync_library()

        assert result["success"] is True
        assert store[skill_service_module.SKILLS_LAST_STATUS_KEY] == "success"
        assert store[skill_service_module.SKILLS_LAST_COMMIT_KEY] == "deadbeef1234"
        assert store[skill_service_module.SKILLS_LAST_ERROR_KEY] == ""

    def test_failure_persists_the_error(self, service, store, one_source):
        """The AC's 'never silent' half — a failure path that skipped this write
        would leave the last SUCCESS on screen."""
        one_source.script(success=False, error="Pull failed: boom")

        result = service.sync_library()

        assert result["success"] is False
        assert store[skill_service_module.SKILLS_LAST_STATUS_KEY] == "failed"
        assert "boom" in store[skill_service_module.SKILLS_LAST_ERROR_KEY]

    def test_error_is_length_capped(self, service, store, one_source):
        one_source.script(success=False, error="x" * 5000)
        service.sync_library()
        assert len(store[skill_service_module.SKILLS_LAST_ERROR_KEY]) <= 500

    def test_commit_changed_compares_against_the_durable_row(
        self, service, store, one_source
    ):
        """Not `self._last_commit_sha`: an in-memory None on a fresh process
        would read as 'changed' and sweep the whole fleet on every restart.

        ent#237 keeps the property per SOURCE — the durable row is now the
        source's own `last_commit_sha` rather than one library-wide setting.
        """
        one_source.src.last_commit_sha = "deadbeef1234"
        one_source.script(success=True, action="pulled", commit_sha="deadbeef1234")
        assert service.sync_library()["commit_changed"] is False

        one_source.script(success=True, action="pulled", commit_sha="feedface5678")
        assert service.sync_library()["commit_changed"] is True

    def test_persisted_error_is_pat_scrubbed(self, service, store, one_source):
        """An unexpected OSError can carry the token into `system_settings` and
        the admin Settings panel (G-04's class)."""
        one_source.script(
            success=False,
            error=(
                "failed running ['git', 'clone', "
                "'https://ghp_AAAABBBBCCCCDDDDEEEEFFFFGGGG@github.com/o/r']"
            ),
        )

        service.sync_library()

        stored = store[skill_service_module.SKILLS_LAST_ERROR_KEY]
        assert "ghp_" not in stored
        assert "https://***@github.com" in stored

    def test_concurrent_sync_reports_busy_without_marking_a_failure(
        self, service, store, one_source
    ):
        """Scheduled + manual sync now overlap routinely; both run
        `git reset --hard` on the SAME clones. A contended click must not
        overwrite the panel with "Last sync failed" — nothing failed."""
        service._acquire_sync_lock = MagicMock(return_value=False)

        result = service.sync_library()

        assert result["busy"] is True
        assert result["success"] is False
        # crucially: no status row written at all
        assert skill_service_module.SKILLS_LAST_STATUS_KEY not in store

    def test_sync_lock_is_released_even_when_git_raises(
        self, service, store, one_source
    ):
        service._acquire_sync_lock = MagicMock(return_value="tok")
        service._release_sync_lock = MagicMock()
        one_source.script(raises=RuntimeError("boom"))

        try:
            service.sync_library()
        except RuntimeError:
            pass

        service._release_sync_lock.assert_called_once_with("tok")

    def test_status_endpoint_prefers_the_durable_row(self, service, store):
        """Under --workers 2 the worker answering /status is usually not the one
        that synced, and the auto-sync loop runs on a single leader."""
        store.update({
            skill_service_module.SKILLS_LAST_SYNC_KEY: "2026-07-29T10:00:00Z",
            skill_service_module.SKILLS_LAST_STATUS_KEY: "failed",
            skill_service_module.SKILLS_LAST_ERROR_KEY: "auth failed",
        })
        service._last_sync = None
        service.list_skills = MagicMock(return_value=[])

        status = service.get_library_status()
        assert status["last_sync"] == "2026-07-29T10:00:00Z"
        assert status["last_sync_status"] == "failed"
        assert status["last_sync_error"] == "auth failed"


# ─────────────────────────────────────────────────────────────────────────────
# Part D2 — a directory is not a repository
#
# `sync_library` used to branch on `library_path.exists()`. A path that exists
# but holds no `.git` (clone interrupted by a full disk, a stray mkdir, a
# restored backup) made `git pull` fail with "not a git repository" on every
# attempt, with nothing in the code path able to re-clone — permanent, and
# fixable only by shell access. Survivable while sync was a button someone
# clicked and watched; ent#236 puts it on an unattended timer, where it means
# every scheduled sync fails and fleet re-inject never runs.
# ─────────────────────────────────────────────────────────────────────────────

class TestNonRepoDirectoryRecovers:
    """ent#237 moved this logic from `SkillService` to `SkillSourceClone`.

    The property is unchanged and now holds PER SOURCE: `sync` selects
    clone-vs-update on `.git`, and the clone path quarantines a non-repo
    directory by rename so `git clone` (which refuses a non-empty destination)
    can proceed. Tests target the clone object directly — the seam that owns it.
    """

    @pytest.fixture()
    def clone(self, tmp_path):
        from services.skill_source_clone import SkillSourceClone
        return SkillSourceClone(
            "src_" + "b" * 12, "https://github.com/owner/repo",
            "main", "branch", tmp_path / "library",
        )

    def test_directory_without_git_clones_instead_of_pulling(self, clone):
        """The core regression: the bug was choosing pull for this state."""
        clone.path.mkdir(parents=True, exist_ok=True)   # exists, no .git
        clone._clone = MagicMock(return_value={"success": True, "action": "cloned"})
        clone._update = MagicMock(return_value={"success": True, "action": "pulled"})
        clone.current_commit = MagicMock(return_value="abc123")

        result = clone.sync("https://github.com/owner/repo")

        clone._update.assert_not_called()
        clone._clone.assert_called_once()
        assert result["action"] == "cloned"

    def test_real_repo_still_pulls(self, clone):
        """The fix must not turn every sync into a re-clone."""
        (clone.path / ".git").mkdir(parents=True, exist_ok=True)
        clone._clone = MagicMock(return_value={"success": True, "action": "cloned"})
        clone._update = MagicMock(return_value={"success": True, "action": "pulled"})
        clone.current_commit = MagicMock(return_value="abc123")

        assert clone.sync("https://github.com/owner/repo")["action"] == "pulled"
        clone._clone.assert_not_called()

    def test_non_empty_non_repo_dir_is_moved_aside_not_deleted(self, clone):
        """`git clone` refuses a non-empty destination, so detecting the broken
        state is only half a fix. Quarantine by rename — the operator's bytes
        survive, and an unattended timer never deletes a directory."""
        clone.path.mkdir(parents=True, exist_ok=True)
        (clone.path / "leftover.txt").write_text("partial clone debris")

        clone._quarantine_non_repo_dir()

        assert not clone.path.exists()
        quarantine = clone.path.with_name(clone.path.name + ".broken")
        assert (quarantine / "leftover.txt").read_text() == "partial clone debris"

    def test_repeat_quarantine_replaces_the_previous_one(self, clone):
        """Bounded: a recurring fault cannot fill the disk with quarantines."""
        quarantine = clone.path.with_name(clone.path.name + ".broken")
        quarantine.mkdir(parents=True, exist_ok=True)
        (quarantine / "old.txt").write_text("first failure")
        clone.path.mkdir(parents=True, exist_ok=True)
        (clone.path / "new.txt").write_text("second failure")

        clone._quarantine_non_repo_dir()

        assert (quarantine / "new.txt").exists()
        assert not (quarantine / "old.txt").exists()

    def test_quarantine_is_per_source_not_library_wide(self, clone, tmp_path):
        """ent#237: one broken source must not quarantine a sibling's checkout.
        Under the shared-library shape there was only one directory to move; the
        per-source shape makes 'which directory' a real question."""
        sibling = tmp_path / "library" / ("src_" + "c" * 12)
        sibling.mkdir(parents=True, exist_ok=True)
        (sibling / "keep.txt").write_text("healthy sibling")
        clone.path.mkdir(parents=True, exist_ok=True)

        clone._quarantine_non_repo_dir()

        assert (sibling / "keep.txt").read_text() == "healthy sibling"
        assert not sibling.with_name(sibling.name + ".broken").exists()

    def test_quarantine_failure_never_raises(self, clone, monkeypatch):
        """Runs inside sync; an OSError here must not become a 500 — the clone
        that follows reports the real problem."""
        clone.path.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(
            type(clone.path), "rename",
            MagicMock(side_effect=OSError("read-only fs")), raising=False,
        )
        clone._quarantine_non_repo_dir()   # must not raise


# ─────────────────────────────────────────────────────────────────────────────
# Part E — auto-sync interval resolution
# ─────────────────────────────────────────────────────────────────────────────

class TestIntervalResolution:
    """Read-side clamping (the #506 shape): a stray DB value must not be able to
    spin the loop into a fetch flood or park it past a day."""

    def _resolver(self, monkeypatch, stored):
        """MUST go through monkeypatch.

        `settings_service.settings_service` is a process-wide singleton shared
        with every other suite. A bare `get_setting = MagicMock(...)` here is
        never undone, so every later `_resolve_bool_flag` caller in the session
        reads this test's canned value — it silently broke seven brain-orb flag
        tests under a random test order (#762 class, the same trap as the db
        singleton above).
        """
        real = sys.modules.get("services.settings_service")
        # Only meaningful against the REAL module; skip under the stub.
        if real is None or not hasattr(real, "SKILLS_AUTO_SYNC_INTERVAL_MIN"):
            pytest.skip("settings_service stubbed in this run")
        monkeypatch.setattr(
            real.settings_service, "get_setting",
            MagicMock(return_value=stored), raising=False,
        )
        return real.get_skills_auto_sync_interval()

    @pytest.mark.parametrize(
        "stored,expected",
        [
            ("3600", 3600),
            ("10", 300),          # below floor → clamped up
            ("999999", 86400),    # above ceiling → clamped down
            ("garbage", 3600),    # unparseable → default, never a crash
            (None, 3600),
            ("", 3600),
        ],
    )
    def test_clamped(self, monkeypatch, stored, expected):
        assert self._resolver(monkeypatch, stored) == expected


# ─────────────────────────────────────────────────────────────────────────────
# Part F — fleet re-inject
# ─────────────────────────────────────────────────────────────────────────────

import services.skills_sync_service as sync_module  # noqa: E402
from services.skills_sync_service import SkillsLibrarySyncService  # noqa: E402


def _agent(name, status="running"):
    a = MagicMock()
    a.name = name
    a.status = status
    return a


class TestFleetReinject:
    def test_excludes_stopped_agents_and_ghosts(self):
        """Stopped agents self-heal via the start path; pushing to them would
        only manufacture failures. Ghosts are disposable (#69 precedent)."""
        agents = [_agent("live"), _agent("ghost"), _agent("down", "stopped")]

        with patch.dict(
            sys.modules,
            {"services.docker_service": _types.SimpleNamespace(
                list_all_agents_fast=lambda: agents
            )},
        ), patch.object(
            sync_module.db,
            "get_agent_ephemeral_info",
            lambda n: {"is_ephemeral": n == "ghost"},
        ):
            assert SkillsLibrarySyncService._eligible_agents() == ["live"]

    def test_unknown_ephemeral_state_treats_the_agent_as_durable(self):
        """Fail-open: a DB hiccup must not silently shrink the sweep."""
        with patch.dict(
            sys.modules,
            {"services.docker_service": _types.SimpleNamespace(
                list_all_agents_fast=lambda: [_agent("live")]
            )},
        ), patch.object(
            sync_module.db,
            "get_agent_ephemeral_info",
            MagicMock(side_effect=RuntimeError("db down")),
        ):
            assert SkillsLibrarySyncService._eligible_agents() == ["live"]

    def test_busy_agent_is_skipped_not_awaited(self):
        """The agent is already mid-injection against the same library clone."""
        with patch.object(
            sync_module.db, "get_agent_skill_names", lambda n: ["clip"]
        ), patch.object(
            sync_module.skill_service,
            "inject_skills",
            AsyncMock(side_effect=SkillInjectionBusy("busy")),
        ):
            result = _run(SkillsLibrarySyncService._reinject_agent("a1"))
        assert result == {"status": "skipped", "reason": "busy"}

    def test_agent_with_no_skills_is_skipped(self):
        with patch.object(sync_module.db, "get_agent_skill_names", lambda n: []):
            result = _run(SkillsLibrarySyncService._reinject_agent("a1"))
        assert result == {"status": "skipped", "reason": "no_skills"}

    def test_uses_force_false_so_unchanged_skills_are_free(self):
        inject = AsyncMock(return_value={"success": True, "skills_injected": 0,
                                         "skills_unchanged": 3})
        with patch.object(
            sync_module.db, "get_agent_skill_names", lambda n: ["a", "b", "c"]
        ), patch.object(sync_module.skill_service, "inject_skills", inject):
            _run(SkillsLibrarySyncService._reinject_agent("a1"))
        assert inject.await_args.kwargs["force"] is False

    def test_report_counts_and_persists(self):
        svc = SkillsLibrarySyncService()
        outcomes = {
            "ok": {"status": "injected"},
            "bad": {"status": "failed", "error": "transport"},
            "busy": {"status": "skipped", "reason": "busy"},
        }
        stored: dict = {}

        with patch.object(
            SkillsLibrarySyncService, "_eligible_agents",
            staticmethod(lambda: ["ok", "bad", "busy"]),
        ), patch.object(
            SkillsLibrarySyncService, "_reinject_agent",
            staticmethod(AsyncMock(side_effect=lambda n: outcomes[n])),
        ), patch.object(
            sync_module.db, "set_setting",
            MagicMock(side_effect=lambda k, v: stored.__setitem__(k, v)),
        ), patch.object(
            SkillsLibrarySyncService, "_announce_fleet_failures", MagicMock()
        ) as alarm, patch.object(
            SkillsLibrarySyncService, "_audit_fleet", AsyncMock()
        ):
            report = _run(svc.run_fleet_reinject(commit_sha="abc123"))

        assert report["agents_total"] == 3
        assert report["agents_injected"] == 1
        assert report["agents_skipped"] == 1
        assert report["agents_failed"] == 1
        assert report["failures"] == {"bad": "transport"}
        assert sync_module.FLEET_LAST_RUN_KEY in stored
        alarm.assert_called_once()

    def test_no_alarm_when_every_agent_succeeds(self):
        svc = SkillsLibrarySyncService()
        with patch.object(
            SkillsLibrarySyncService, "_eligible_agents", staticmethod(lambda: ["ok"])
        ), patch.object(
            SkillsLibrarySyncService, "_reinject_agent",
            staticmethod(AsyncMock(return_value={"status": "injected"})),
        ), patch.object(
            sync_module.db, "set_setting", MagicMock()
        ), patch.object(
            SkillsLibrarySyncService, "_announce_fleet_failures", MagicMock()
        ) as alarm, patch.object(
            SkillsLibrarySyncService, "_audit_fleet", AsyncMock()
        ):
            _run(svc.run_fleet_reinject())
        alarm.assert_not_called()


class TestCycleGating:
    def _svc(self):
        return SkillsLibrarySyncService()

    def test_failed_sync_does_not_sweep(self):
        svc = self._svc()
        with patch.object(
            sync_module.skill_service, "sync_library",
            MagicMock(return_value={"success": False, "error": "nope"}),
        ), patch.object(svc, "run_fleet_reinject", AsyncMock()) as sweep:
            result = _run(svc.run_cycle())
        assert result["synced"] is False
        sweep.assert_not_called()

    def test_busy_sync_is_not_reported_as_a_failure(self):
        svc = self._svc()
        with patch.object(
            sync_module.skill_service, "sync_library",
            MagicMock(return_value={"success": False, "busy": True, "error": "running"}),
        ), patch.object(svc, "run_fleet_reinject", AsyncMock()) as sweep:
            result = _run(svc.run_cycle())
        assert result == {"synced": False, "busy": True}
        sweep.assert_not_called()

    def test_unchanged_commit_does_not_sweep(self):
        """A no-op pull must not re-inject across every running agent."""
        svc = self._svc()
        with patch.object(
            sync_module.skill_service, "sync_library",
            MagicMock(return_value={"success": True, "commit_changed": False}),
        ), patch.object(svc, "run_fleet_reinject", AsyncMock()) as sweep, \
                patch.object(SkillsLibrarySyncService, "_audit_sync", AsyncMock()):
            result = _run(svc.run_cycle())
        assert result["fleet"] is None
        sweep.assert_not_called()

    def test_changed_commit_sweeps_only_when_opted_in(self):
        svc = self._svc()
        with patch.object(
            sync_module.skill_service, "sync_library",
            MagicMock(return_value={"success": True, "commit_changed": True,
                                    "commit_sha": "abc"}),
        ), patch.object(
            sync_module, "is_skills_auto_reinject_enabled", lambda: False
        ), patch.object(svc, "run_fleet_reinject", AsyncMock()) as sweep, \
                patch.object(SkillsLibrarySyncService, "_audit_sync", AsyncMock()):
            assert _run(svc.run_cycle())["fleet"] is None
            sweep.assert_not_called()

        with patch.object(
            sync_module.skill_service, "sync_library",
            MagicMock(return_value={"success": True, "commit_changed": True,
                                    "commit_sha": "abc"}),
        ), patch.object(
            sync_module, "is_skills_auto_reinject_enabled", lambda: True
        ), patch.object(
            svc, "run_fleet_reinject", AsyncMock(return_value={"agents_total": 0})
        ) as sweep, patch.object(
            SkillsLibrarySyncService, "_audit_sync", AsyncMock()
        ):
            assert _run(svc.run_cycle())["fleet"] == {"agents_total": 0}
            sweep.assert_awaited_once()


class TestAutomationSwitchIsHumanOnly:
    """The ON-switch for unattended fleet-wide writes must reject agent keys.

    `assert_admin` answers "what role", never "is this a human": an agent-scoped
    MCP key resolves to its owner CARRYING the owner's role, so on a default
    admin-owned install every agent's injected `TRINITY_MCP_API_KEY` passes it.
    Enabling auto-sync + fleet re-inject makes Trinity write `SKILL.md` files —
    instructions Claude executes — into every running agent's `~/.claude/skills/`
    with no human in the loop. Third occurrence of the trinity-ops-agent#232
    class (#1644, #1816).
    """

    @staticmethod
    def _caller(agent_name):
        """`User.agent_name` is set only for scope='agent' keys, and
        `reject_agent_principal` keys off exactly that — a bare MagicMock has a
        truthy `.agent_name` and would read as an agent key."""
        caller = MagicMock()
        caller.agent_name = agent_name
        caller.role = "admin"
        return caller

    @pytest.fixture()
    def router_mod(self):
        try:
            # #1028: the skills-library automation handlers live in the
            # package's `integrations` module.
            import routers.settings.integrations as mod
        except Exception:  # noqa: BLE001
            pytest.skip("routers.settings not importable in this run")
        return mod

    def test_agent_scoped_key_is_rejected(self, router_mod, monkeypatch):
        from fastapi import HTTPException

        # The real gate is deliberately NOT stubbed — that is what's under test.
        monkeypatch.setattr(router_mod, "assert_admin", lambda user, **kw: None)
        body = MagicMock(
            auto_sync_enabled=True, auto_sync_interval_seconds=None,
            auto_reinject_enabled=True,
        )
        with pytest.raises(HTTPException) as exc:
            _run(router_mod.update_skills_library_automation_setting(
                body, MagicMock(), self._caller("some-agent")
            ))
        assert exc.value.status_code == 403
        assert "human-only" in str(exc.value.detail)

    def test_human_admin_still_passes_the_gate(self, router_mod, monkeypatch):
        """The guard must not lock out the operator it exists to protect."""
        monkeypatch.setattr(router_mod, "assert_admin", lambda user, **kw: None)
        monkeypatch.setattr(router_mod.db, "set_setting", MagicMock())
        monkeypatch.setattr(
            router_mod.platform_audit_service, "log", AsyncMock()
        )
        body = MagicMock(
            auto_sync_enabled=True, auto_sync_interval_seconds=None,
            auto_reinject_enabled=None,
        )
        request = MagicMock()
        request.client.host = "127.0.0.1"

        result = _run(router_mod.update_skills_library_automation_setting(
            body, request, self._caller(None)
        ))
        assert result["success"] is True
        assert result["changed"]["auto_sync_enabled"] is True


class TestLeadership:
    def test_fails_open_without_redis(self):
        """A duplicated git pull is wasteful; silently stopping auto-sync is the
        mode the operator cannot see."""
        svc = SkillsLibrarySyncService()
        with patch.object(sync_module, "get_breaker_redis", lambda: None):
            assert svc._try_acquire_leadership(60) is True

    def test_only_one_worker_wins(self):
        redis = MagicMock()
        redis.set.return_value = False
        redis.get.return_value = "other-worker"
        svc = SkillsLibrarySyncService()
        with patch.object(sync_module, "get_breaker_redis", lambda: redis):
            assert svc._try_acquire_leadership(60) is False

    def test_refreshes_only_its_own_lease(self):
        redis = MagicMock()
        redis.set.return_value = False
        svc = SkillsLibrarySyncService()
        redis.get.return_value = svc._worker_id
        with patch.object(sync_module, "get_breaker_redis", lambda: redis):
            assert svc._try_acquire_leadership(60) is True
        redis.expire.assert_called_once()

    def test_redis_error_fails_open(self):
        redis = MagicMock()
        redis.set.side_effect = RuntimeError("redis down")
        svc = SkillsLibrarySyncService()
        with patch.object(sync_module, "get_breaker_redis", lambda: redis):
            assert svc._try_acquire_leadership(60) is True
