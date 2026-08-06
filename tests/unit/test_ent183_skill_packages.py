"""Unit tests for trinity-enterprise#183 — full-directory skill packages.

Covers the three layers of the feature:

1. **Pure packaging** (`services/skill_packaging.py`): frontmatter contract
   parse (tolerant + alias-bomb hardened), git-archive member vetting
   (symlinks/litter/protected names dropped with named warnings), injection
   tar assembly (meta member LAST), manifest-based prune diff.
2. **Round-trip vs the REAL agent-server restore primitive**: a tar built by
   `build_injection_tar` is fed to the actual `restore_from_tar`
   (docker/base-image/agent_server/routers/snapshot.py — pure by design) and
   must land every member inside the allowlist, meta included.
3. **Injection orchestration** (`services/skill_service.py`): skip-if-unchanged
   vs force, old-image 404 fallback, restore-failure repair path, prune wiring,
   dep-check warnings, honest per-skill result shape, CLAUDE.md rebuild from
   ALL present skills.

Module-ownership hygiene (#762 class): packaging + snapshot are loaded via
file-location with unique module names; the service import reuses the
established stub pattern from test_skill_service_user_agent.py and only
stubs modules that are not already present.
"""

from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = _PROJECT_ROOT / "src" / "backend"

_TMP_DB = Path(tempfile.gettempdir()) / "trinity_test_ent183.db"
os.environ.setdefault("TRINITY_DB_PATH", str(_TMP_DB))

if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


def _load(path: str, name: str):
    spec = importlib.util.spec_from_file_location(name, str(_PROJECT_ROOT / path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pkg = _load("src/backend/services/skill_packaging.py", "_ent183_pkg")
snapshot = _load(
    "docker/base-image/agent_server/routers/snapshot.py", "_ent183_snapshot"
)


# ─────────────────────────────────────────────────────────────────────────────
# Part A — pure packaging primitives
# ─────────────────────────────────────────────────────────────────────────────

class TestSkillNameValidation:
    def test_good_names(self):
        for name in ("clip-video", "tdd", "a", "Skill_2.0", "x" * 64):
            assert pkg.validate_skill_name(name), name

    def test_bad_names(self):
        for name in ("..", ".", "a/b", "../etc", ".hidden", "", "x" * 65,
                     "a b", "a;b", None, 42, "-lead", "_lead"):
            assert not pkg.validate_skill_name(name), repr(name)


class TestFrontmatterParse:
    def test_valid_flat_contract(self):
        text = (
            "---\n"
            "name: clip-video\n"
            "description: Cut videos\n"
            "automation: gated\n"
            "user_invocable: false\n"
            "allowed-tools: Bash, Read\n"
            "requires:\n"
            "  packages: [pillow]\n"
            "  binaries: [ffmpeg]\n"
            "  env: [ELEVENLABS_API_KEY]\n"
            "---\n# Body\n"
        )
        fm, warning = pkg.parse_frontmatter(text)
        assert warning is None
        contract, warnings = pkg.extract_contract(fm)
        assert warnings == []
        assert contract["description"] == "Cut videos"
        assert contract["automation"] == "gated"
        assert contract["user_invocable"] is False
        assert contract["allowed_tools"] == "Bash, Read"
        assert contract["requires"] == {
            "packages": ["pillow"], "binaries": ["ffmpeg"],
            "env": ["ELEVENLABS_API_KEY"],
        }

    def test_trinity_block_wins_over_flat(self):
        text = (
            "---\n"
            "automation: manual\n"
            "trinity:\n"
            "  automation: autonomous\n"
            "---\nbody\n"
        )
        fm, _ = pkg.parse_frontmatter(text)
        contract, _ = pkg.extract_contract(fm)
        assert contract["automation"] == "autonomous"

    def test_missing_frontmatter_is_not_a_warning(self):
        fm, warning = pkg.parse_frontmatter("# Just a doc\n")
        assert fm is None and warning is None
        contract, warnings = pkg.extract_contract(fm)
        assert contract["user_invocable"] is True
        assert warnings == []

    def test_unknown_keys_tolerated(self):
        fm, warning = pkg.parse_frontmatter(
            "---\nname: x\nfuture_field: [1, 2]\nnested:\n  a: b\n---\nbody"
        )
        assert warning is None
        contract, warnings = pkg.extract_contract(fm)
        assert warnings == []

    def test_invalid_yaml_is_named_not_fatal(self):
        fm, warning = pkg.parse_frontmatter("---\n: : :\n  - ][\n---\nbody")
        assert fm is None
        assert warning == "frontmatter_invalid"

    def test_non_mapping_frontmatter_invalid(self):
        fm, warning = pkg.parse_frontmatter("---\n- just\n- a list\n---\nbody")
        assert fm is None and warning == "frontmatter_invalid"

    def test_oversized_frontmatter_rejected(self):
        big = "---\n" + ("k: " + "v" * 70_000) + "\n---\nbody"
        fm, warning = pkg.parse_frontmatter(big)
        assert fm is None and warning == "frontmatter_invalid"

    def test_alias_bomb_rejected(self):
        # safe_load would EXPAND this (billion-laughs); the hardened loader
        # must refuse aliases outright instead of expanding anything.
        bomb = (
            "---\n"
            "a: &a [x, x, x, x, x, x, x, x, x]\n"
            "b: &b [*a, *a, *a, *a, *a, *a, *a, *a, *a]\n"
            "c: &c [*b, *b, *b, *b, *b, *b, *b, *b, *b]\n"
            "d: [*c, *c, *c, *c, *c, *c, *c, *c, *c]\n"
            "---\nbody"
        )
        fm, warning = pkg.parse_frontmatter(bomb)
        assert fm is None and warning == "frontmatter_invalid"

    def test_garbage_requires_is_warned(self):
        fm, _ = pkg.parse_frontmatter("---\nrequires: just-a-string\n---\nbody")
        contract, warnings = pkg.extract_contract(fm)
        assert contract["requires"] == {"packages": [], "binaries": [], "env": []}
        assert "frontmatter_invalid:requires" in warnings

    def test_malicious_dep_names_never_survive(self):
        fm, _ = pkg.parse_frontmatter(
            "---\n"
            "requires:\n"
            "  binaries: ['ffmpeg', 'x; rm -rf ~', '$(curl evil)']\n"
            "  env: ['GOOD_KEY', 'bad key', 'k;v']\n"
            "---\nbody"
        )
        contract, warnings = pkg.extract_contract(fm)
        assert contract["requires"]["binaries"] == ["ffmpeg"]
        assert contract["requires"]["env"] == ["GOOD_KEY"]
        assert len([w for w in warnings if w.startswith("frontmatter_invalid:")]) == 4


def _make_archive(members: list[tuple[str, bytes | None, int, str]]) -> bytes:
    """Build git-archive-shaped tar bytes. type: 'file'|'dir'|'symlink'."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, content, mode, kind in members:
            info = tarfile.TarInfo(name=name)
            info.mode = mode
            if kind == "dir":
                info.type = tarfile.DIRTYPE
                tf.addfile(info)
            elif kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = "../../../etc/passwd"
                tf.addfile(info)
            else:
                info.size = len(content or b"")
                tf.addfile(info, io.BytesIO(content or b""))
    return buf.getvalue()


class TestFilterSkillArchive:
    PREFIX = ".claude/skills/demo/"

    def test_vets_members(self):
        archive = _make_archive([
            (self.PREFIX.rstrip("/"), None, 0o755, "dir"),
            (self.PREFIX + "SKILL.md", b"# demo", 0o644, "file"),
            (self.PREFIX + "scripts/run.sh", b"#!/bin/sh\n", 0o755, "file"),
            (self.PREFIX + "link", None, 0o777, "symlink"),
            (self.PREFIX + ".DS_Store", b"junk", 0o644, "file"),
            (self.PREFIX + "__pycache__/m.pyc", b"x", 0o644, "file"),
            (self.PREFIX + ".env", b"SECRET=1", 0o644, "file"),
            (self.PREFIX + "CLAUDE.md", b"override", 0o644, "file"),
            (self.PREFIX + ".trinity-skill.json", b"{}", 0o644, "file"),
            (".claude/skills/other/SKILL.md", b"stray", 0o644, "file"),
        ])
        members, warnings, total = pkg.filter_skill_archive(archive, "demo")
        names = [m[0] for m in members]
        assert names == [self.PREFIX + "SKILL.md", self.PREFIX + "scripts/run.sh"]
        assert total == len(b"# demo") + len(b"#!/bin/sh\n")
        assert "symlink_skipped:link" in warnings
        assert "protected_name_skipped:.env" in warnings
        assert "protected_name_skipped:CLAUDE.md" in warnings
        assert "protected_name_skipped:.trinity-skill.json" in warnings
        assert any(w.startswith("restore_skipped:.claude/skills/other/") for w in warnings)
        # litter is dropped silently — not legitimate content, not a warning
        assert not any(".DS_Store" in w or "pycache" in w for w in warnings)

    def test_exec_bits_survive_filtering(self):
        archive = _make_archive([
            (self.PREFIX + "SKILL.md", b"x", 0o644, "file"),
            (self.PREFIX + "scripts/go.sh", b"#!/bin/sh\n", 0o755, "file"),
        ])
        members, _, _ = pkg.filter_skill_archive(archive, "demo")
        assert pkg.executable_paths(members) == [self.PREFIX + "scripts/go.sh"]


class TestBuildInjectionTar:
    def test_meta_member_is_last_and_tar_uncompressed(self):
        members = [
            (".claude/skills/demo/SKILL.md", b"# demo", 0o644),
            (".claude/skills/demo/scripts/run.sh", b"#!/bin/sh\n", 0o755),
        ]
        meta = {"version": "abc123", "manifest": [m[0] for m in members]}
        blob = pkg.build_injection_tar("demo", members, meta)
        # plain tar magic at offset 257, not gzip magic at 0
        assert blob[:2] != b"\x1f\x8b"
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:") as tf:
            names = tf.getnames()
            assert names[-1] == ".claude/skills/demo/.trinity-skill.json"
            loaded = json.load(tf.extractfile(names[-1]))
            assert loaded["version"] == "abc123"
            modes = {m.name: m.mode for m in tf.getmembers()}
            assert modes[".claude/skills/demo/scripts/run.sh"] & 0o111


class TestComputePrune:
    def test_no_previous_manifest_prunes_nothing(self):
        assert pkg.compute_prune(None, ["a"], "d") == ([], False)
        assert pkg.compute_prune([], ["a"], "d") == ([], False)
        assert pkg.compute_prune("garbage", ["a"], "d") == ([], False)

    def test_stale_diff_and_meta_exemption(self):
        prev = [
            ".claude/skills/d/SKILL.md",
            ".claude/skills/d/old.py",
            ".claude/skills/d/.trinity-skill.json",
        ]
        new = [".claude/skills/d/SKILL.md"]
        stale, truncated = pkg.compute_prune(prev, new, "d")
        assert stale == [".claude/skills/d/old.py"]
        assert truncated is False

    def test_cap_truncates(self):
        prev = [f".claude/skills/d/f{i}" for i in range(300)]
        stale, truncated = pkg.compute_prune(prev, [], "d")
        assert len(stale) == pkg.PRUNE_CAP_PER_SKILL
        assert truncated is True

    def test_prune_is_confined_to_the_skill_dir(self):
        # The previous manifest is AGENT-side state (untrusted): a doctored
        # manifest must never steer a backend-driven delete outside
        # .claude/skills/<name>/ (review H1).
        prev = [
            "workspace/important.db",
            ".claude/settings.json",
            "/etc/passwd",
            ".claude/skills/other/file.py",
            ".claude/skills/d/../../../.env",
            ".trinity-skill.json",
            ".claude/skills/d/legit-stale.py",
        ]
        stale, _ = pkg.compute_prune(prev, [], "d")
        assert stale == [".claude/skills/d/legit-stale.py"]


class TestCaps:
    def test_env_tunable_caps(self, monkeypatch):
        monkeypatch.setenv("SKILL_MAX_BYTES", "1234")
        assert pkg.skill_max_bytes() == 1234
        monkeypatch.setenv("SKILL_MAX_BYTES", "not-a-number")
        assert pkg.skill_max_bytes() == 10 * 1024 * 1024
        monkeypatch.setenv("SKILLS_TOTAL_MAX_BYTES", "-5")
        assert pkg.skills_total_max_bytes() == 50 * 1024 * 1024


# ─────────────────────────────────────────────────────────────────────────────
# Part B — round trip against the REAL agent-server restore primitive
# ─────────────────────────────────────────────────────────────────────────────

class TestRestoreRoundTrip:
    def _restore(self, home: Path, blob: bytes, skill: str):
        return snapshot.restore_from_tar(
            home, blob, [f".claude/skills/{skill}/**"]
        )

    def test_built_tar_restores_completely(self, tmp_path):
        members = [
            (".claude/skills/demo/SKILL.md", b"# demo", 0o644),
            (".claude/skills/demo/scripts/run.sh", b"#!/bin/sh\necho hi\n", 0o755),
            (".claude/skills/demo/ref/data.json", b"{}", 0o644),
        ]
        meta = {"version": "abc", "manifest": [m[0] for m in members]}
        blob = pkg.build_injection_tar("demo", members, meta)
        restored, skipped = self._restore(tmp_path, blob, "demo")
        assert skipped == []
        assert set(restored) == {m[0] for m in members} | {
            ".claude/skills/demo/.trinity-skill.json"
        }
        assert (tmp_path / ".claude/skills/demo/scripts/run.sh").read_bytes() == \
            b"#!/bin/sh\necho hi\n"
        on_disk_meta = json.loads(
            (tmp_path / ".claude/skills/demo/.trinity-skill.json").read_text()
        )
        assert on_disk_meta["version"] == "abc"

    def test_overwrite_in_place(self, tmp_path):
        target = tmp_path / ".claude/skills/demo/SKILL.md"
        target.parent.mkdir(parents=True)
        target.write_text("old")
        blob = pkg.build_injection_tar(
            "demo", [(".claude/skills/demo/SKILL.md", b"new", 0o644)], {"v": 1}
        )
        restored, _ = self._restore(tmp_path, blob, "demo")
        assert ".claude/skills/demo/SKILL.md" in restored
        assert target.read_text() == "new"

    def test_allowlist_confines_to_skill_dir(self, tmp_path):
        # A member outside the requested skill's allowlist is skipped, even if
        # it slipped past the build-side filter.
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            info = tarfile.TarInfo(name=".claude/skills/other/SKILL.md")
            info.size = 5
            tf.addfile(info, io.BytesIO(b"stray"))
        restored, skipped = self._restore(tmp_path, buf.getvalue(), "demo")
        assert restored == []
        assert skipped == [".claude/skills/other/SKILL.md"]


# ─────────────────────────────────────────────────────────────────────────────
# Part C — injection orchestration (service-level, stubbed transport)
# ─────────────────────────────────────────────────────────────────────────────

# Stub heavy backend deps ONLY if a real module isn't already loaded (combined
# runs where the full backend imported first must keep their real modules).
import types as _types  # noqa: E402

_STUBBED_MODULE_NAMES = [
    "database",
    "services.settings_service",
    "services.agent_client",
    "utils.url_validation",
]


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    """Snapshot sys.modules before each test and restore after.

    The stubs below are installed at import time (skill_service reaches them
    at module scope, before any fixture could run), so without this they would
    leak into other files in the same pytest session — the #762 class.
    """
    saved = {name: sys.modules.get(name) for name in _STUBBED_MODULE_NAMES}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


_STUBS = {
    "database": {"db": MagicMock()},
    "services.settings_service": {
        "get_skills_library_url": lambda: "https://github.com/owner/repo",
        "get_skills_library_branch": lambda: "main",
        "get_github_pat": lambda: None,
        "get_anthropic_api_key": lambda: "sk-test",
        "get_agent_full_capabilities": lambda: False,
        "get_agent_default_resources": lambda: {"cpu": "2", "memory": "4g"},
        "settings_service": MagicMock(),
    },
    "services.agent_client": {
        "get_agent_client": MagicMock(),
        "AgentClientError": type("AgentClientError", (Exception,), {}),
        "AgentClient": MagicMock(),
        "AgentNotReachableError": type("AgentNotReachableError", (Exception,), {}),
        "AgentRequestError": type("AgentRequestError", (Exception,), {}),
        "get_all_circuit_states": MagicMock(return_value={}),
    },
    # The stub must mirror EVERY name skill_service imports from the real
    # module — a missing constant is an ImportError at collection, not a
    # graceful degradation (see abilityai/trinity#1898 for the wider fragility).
    "utils.url_validation": {
        "validate_skills_library_url": lambda url: url,
        "ALLOWED_SKILLS_LIBRARY_HOSTS": {"github.com", "www.github.com"},
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
from services.skill_source_clone import SkillSourceClone  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


# ent#237 made the library multi-source: `skill_service` orchestrates N
# `SkillSourceClone`s under /data/skills-library/<source_id>/ instead of being a
# single clone itself. These tests predate that and are written against ONE
# library, which is still a valid configuration — so the fixture wires exactly
# one source and the assertions below are unchanged. The directory is named
# `src_*` because that is the id format a clone validates.
_TEST_SOURCE_ID = "src_aaaaaaaa"


@pytest.fixture()
def service(tmp_path):
    svc = SkillService()
    svc.library_root = tmp_path
    svc.library_path = tmp_path / _TEST_SOURCE_ID
    (svc.library_path / ".claude" / "skills").mkdir(parents=True)

    clone = SkillSourceClone(
        _TEST_SOURCE_ID, "https://github.com/owner/repo", "main", "branch", tmp_path
    )
    # Bypass the DB: these tests assert injection/listing behaviour, not source
    # persistence (that is test_ent237_skill_sources.py).
    svc._clones = lambda enabled_only=True: [clone]
    svc._source_names = lambda: {_TEST_SOURCE_ID: "Test library"}
    svc.test_clone = clone
    return svc


def _write_skill(svc, name, files: dict[str, str], frontmatter: str = ""):
    skill_dir = svc.library_path / ".claude" / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        target = skill_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    if "SKILL.md" not in files:
        (skill_dir / "SKILL.md").write_text(frontmatter + "# skill\n")


def _fake_archive_from_library(svc, name) -> bytes:
    """Simulate `git archive HEAD -- .claude/skills/<name>` from the tmp lib."""
    skill_dir = svc.library_path / ".claude" / "skills" / name
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for f in sorted(skill_dir.rglob("*")):
            if f.is_file() and not f.is_symlink():
                rel = f.relative_to(svc.library_path).as_posix()
                tf.add(str(f), arcname=rel)
    return buf.getvalue()


def _resp(status_code, body=None, text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = body if body is not None else {}
    resp.text = text
    return resp


def _wire(service, *, metas=None, restore=None, probe=None):
    """Patch the exec/transport seams; returns the mocks."""
    service._read_agent_skill_metas = AsyncMock(return_value=metas or {})
    # ent#237: these three are per-clone now, not per-service.
    service.test_clone.tree_shas = MagicMock(
        return_value={
            p.name: f"tree-{p.name}"
            for p in (service.library_path / ".claude" / "skills").iterdir()
            if p.is_dir()
        }
    )
    service.test_clone.archive_skill = MagicMock(
        side_effect=lambda name: _fake_archive_from_library(service, name)
    )
    service.test_clone.current_commit = MagicMock(return_value="commit123")
    if callable(restore) and not isinstance(restore, MagicMock):
        service._post_restore = AsyncMock(side_effect=restore)
    else:
        service._post_restore = AsyncMock(return_value=restore)
    service._delete_agent_file = AsyncMock(return_value=True)
    service._probe_dependencies = AsyncMock(return_value=probe)
    service._finalize_injected_dirs = AsyncMock(
        return_value={"chmod": 0, "gitignore": True, "untracked": True, "errors": []}
    )
    service._acquire_inject_lock = MagicMock(return_value=None)
    client = MagicMock()
    client.read_file = AsyncMock(return_value={"success": True, "content": "# Agent\n"})
    client.write_file = AsyncMock(return_value={"success": True})
    client.delete = AsyncMock(return_value=_resp(200))
    return client


def _restore_ok_for(members_by_skill):
    def _side_effect(agent_name, skill_name, tar_bytes):
        with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as tf:
            names = tf.getnames()
        return _resp(200, {"restored": names, "skipped_outside_allowlist": []})
    return _side_effect


class TestInjectOrchestration:
    def test_happy_path_full_package(self, service):
        _write_skill(service, "demo", {
            "SKILL.md": "---\ndescription: d\n---\n# demo\n",
            "scripts/run.py": "print('hi')\n",
        })
        client = _wire(service, restore=_restore_ok_for(None))
        with patch.object(skill_service_module, "get_agent_client", return_value=client):
            result = _run(service.inject_skills("agent-x", ["demo"], force=True))
        assert result["success"] is True
        assert result["skills_injected"] == 1
        demo = result["results"]["demo"]
        assert demo["status"] == "injected"
        assert demo["files_written"] == 3  # SKILL.md + script + meta
        assert demo["warnings"] == []
        # meta member was sent inside the tar (not a separate write)
        service._post_restore.assert_awaited_once()

    def test_skip_if_unchanged_vs_force(self, service):
        _write_skill(service, "demo", {"SKILL.md": "# demo\n"})
        metas = {"demo": {"exists": True, "meta": {"version": "tree-demo",
                                                   "manifest": []}}}
        client = _wire(service, metas=metas, restore=_restore_ok_for(None))
        with patch.object(skill_service_module, "get_agent_client", return_value=client):
            unforced = _run(service.inject_skills("agent-x", ["demo"], force=False))
        assert unforced["results"]["demo"]["status"] == "unchanged"
        assert unforced["skills_unchanged"] == 1
        service._post_restore.assert_not_awaited()

        with patch.object(skill_service_module, "get_agent_client", return_value=client):
            forced = _run(service.inject_skills("agent-x", ["demo"], force=True))
        assert forced["results"]["demo"]["status"] == "injected"
        service._post_restore.assert_awaited()

    def test_old_image_404_falls_back_to_skill_md(self, service):
        _write_skill(service, "multi", {
            "SKILL.md": "# multi\n", "scripts/x.sh": "echo\n",
        })
        _write_skill(service, "single", {"SKILL.md": "# single\n"})
        client = _wire(service, restore=_resp(404))
        with patch.object(skill_service_module, "get_agent_client", return_value=client):
            result = _run(service.inject_skills("agent-x", ["multi", "single"],
                                                force=True))
        multi = result["results"]["multi"]
        single = result["results"]["single"]
        assert multi["status"] == "fallback"
        assert "multi_file_dropped_old_image" in multi["warnings"]
        assert single["status"] == "fallback"
        assert "multi_file_dropped_old_image" not in single["warnings"]
        assert client.write_file.await_count >= 2  # SKILL.md per skill (+ CLAUDE.md)

    def test_restore_failure_takes_repair_path_once(self, service):
        _write_skill(service, "demo", {"SKILL.md": "# demo\n"})
        calls = {"n": 0}

        def flaky(agent_name, skill_name, tar_bytes):
            calls["n"] += 1
            if calls["n"] == 1:
                return _resp(500, text="boom")
            with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as tf:
                names = tf.getnames()
            return _resp(200, {"restored": names})

        client = _wire(service, restore=flaky)
        with patch.object(skill_service_module, "get_agent_client", return_value=client):
            result = _run(service.inject_skills("agent-x", ["demo"], force=True))
        demo = result["results"]["demo"]
        assert demo["status"] == "injected"
        assert "repair_reinjected" in demo["warnings"]
        service._delete_agent_file.assert_any_await(client, ".claude/skills/demo")

    def test_double_restore_failure_is_honest(self, service):
        _write_skill(service, "demo", {"SKILL.md": "# demo\n"})
        client = _wire(service, restore=_resp(500, text="down"))
        with patch.object(skill_service_module, "get_agent_client", return_value=client):
            result = _run(service.inject_skills("agent-x", ["demo"], force=True))
        demo = result["results"]["demo"]
        assert demo["status"] == "failed" and demo["success"] is False
        assert "restore failed" in demo["error"]
        assert result["success"] is False

    def test_manifest_prune_deletes_only_platform_files(self, service):
        _write_skill(service, "demo", {"SKILL.md": "# demo\n"})
        prev_manifest = [
            ".claude/skills/demo/SKILL.md",
            ".claude/skills/demo/scripts/removed.py",
        ]
        metas = {"demo": {"exists": True, "meta": {"version": "old",
                                                   "manifest": prev_manifest}}}
        client = _wire(service, metas=metas, restore=_restore_ok_for(None))
        with patch.object(skill_service_module, "get_agent_client", return_value=client):
            result = _run(service.inject_skills("agent-x", ["demo"], force=True))
        assert result["results"]["demo"]["status"] == "injected"
        deleted = [c.args[1] for c in service._delete_agent_file.await_args_list]
        assert deleted == [".claude/skills/demo/scripts/removed.py"]

    def test_unmanaged_dir_is_overwritten_but_never_pruned(self, service):
        _write_skill(service, "demo", {"SKILL.md": "# demo\n"})
        metas = {"demo": {"exists": True, "meta": None}}  # agent-authored dir
        client = _wire(service, metas=metas, restore=_restore_ok_for(None))
        with patch.object(skill_service_module, "get_agent_client", return_value=client):
            result = _run(service.inject_skills("agent-x", ["demo"], force=True))
        demo = result["results"]["demo"]
        assert demo["status"] == "injected"
        assert "unmanaged_dir_overwritten" in demo["warnings"]
        service._delete_agent_file.assert_not_awaited()

    def test_missing_skill_and_invalid_name(self, service):
        _write_skill(service, "real", {"SKILL.md": "# real\n"})
        client = _wire(service, restore=_restore_ok_for(None))
        with patch.object(skill_service_module, "get_agent_client", return_value=client):
            result = _run(service.inject_skills(
                "agent-x", ["real", "ghost", "../evil"], force=True))
        assert result["results"]["real"]["status"] == "injected"
        assert result["results"]["ghost"]["error"] == "Skill not found in library"
        assert result["results"]["../evil"]["error"] == "invalid_skill_name"
        assert result["skills_failed"] == 2

    def test_skill_too_large_is_named_and_skipped(self, service, monkeypatch):
        monkeypatch.setenv("SKILL_MAX_BYTES", "10")
        _write_skill(service, "big", {"SKILL.md": "x" * 100})
        _write_skill(service, "ok", {"SKILL.md": "y"})
        client = _wire(service, restore=_restore_ok_for(None))
        with patch.object(skill_service_module, "get_agent_client", return_value=client):
            result = _run(service.inject_skills("agent-x", ["big", "ok"], force=True))
        assert "skill_too_large" in result["results"]["big"]["error"]
        assert result["results"]["ok"]["status"] == "injected"

    def test_dep_warnings_from_probe(self, service):
        _write_skill(service, "demo", {
            "SKILL.md": (
                "---\nrequires:\n  packages: [pillow]\n"
                "  binaries: [ffmpeg, jq]\n  env: [GOOD, MISSING]\n---\n# d\n"
            ),
        })
        probe = {"binaries": {"ffmpeg": False, "jq": True},
                 "env": {"GOOD": True, "MISSING": False}}
        client = _wire(service, restore=_restore_ok_for(None), probe=probe)
        with patch.object(skill_service_module, "get_agent_client", return_value=client):
            result = _run(service.inject_skills("agent-x", ["demo"], force=True))
        warnings = result["results"]["demo"]["warnings"]
        assert "missing_binary:ffmpeg" in warnings
        assert "missing_binary:jq" not in str(warnings)
        assert "missing_env:MISSING" in warnings
        assert "missing_env:GOOD" not in str(warnings)
        assert "packages_not_checked" in warnings

    def test_probe_unavailable_is_fail_open_and_named(self, service):
        _write_skill(service, "demo", {
            "SKILL.md": "---\nrequires:\n  binaries: [ffmpeg]\n---\n# d\n",
        })
        client = _wire(service, restore=_restore_ok_for(None), probe=None)
        with patch.object(skill_service_module, "get_agent_client", return_value=client):
            result = _run(service.inject_skills("agent-x", ["demo"], force=True))
        assert "dep_check_skipped" in result["results"]["demo"]["warnings"]
        assert result["results"]["demo"]["status"] == "injected"

    def test_dep_check_runs_for_unchanged_skills(self, service):
        _write_skill(service, "demo", {
            "SKILL.md": "---\nrequires:\n  binaries: [ffmpeg]\n---\n# d\n",
        })
        metas = {"demo": {"exists": True,
                          "meta": {"version": "tree-demo", "manifest": []}}}
        probe = {"binaries": {"ffmpeg": False}, "env": {}}
        client = _wire(service, metas=metas, restore=_restore_ok_for(None), probe=probe)
        with patch.object(skill_service_module, "get_agent_client", return_value=client):
            result = _run(service.inject_skills("agent-x", ["demo"], force=False))
        demo = result["results"]["demo"]
        assert demo["status"] == "unchanged"
        assert "missing_binary:ffmpeg" in demo["warnings"]

    def test_lock_contention_raises_busy(self, service):
        _write_skill(service, "demo", {"SKILL.md": "# demo\n"})
        service._acquire_inject_lock = MagicMock(
            side_effect=SkillInjectionBusy("busy"))
        with pytest.raises(SkillInjectionBusy):
            _run(service.inject_skills("agent-x", ["demo"], force=True))

    def test_transport_failure_never_triggers_repair_delete(self, service):
        # A timeout/connection drop (resp None) may mean slow-but-alive with a
        # WORKING install — deleting the dir then failing again would be a
        # data-loss window (review M2). Only HTTP error statuses repair.
        _write_skill(service, "demo", {"SKILL.md": "# demo\n"})
        client = _wire(service, restore=None)
        service._post_restore = AsyncMock(return_value=None)
        with patch.object(skill_service_module, "get_agent_client", return_value=client):
            result = _run(service.inject_skills("agent-x", ["demo"], force=True))
        demo = result["results"]["demo"]
        assert demo["status"] == "failed"
        assert "transport" in demo["error"]
        service._delete_agent_file.assert_not_awaited()

    def test_fallback_skills_queue_no_chmod_paths(self, service):
        # fallback wrote ONLY SKILL.md — queuing executables would chmod
        # nonexistent files and stamp finalize_partial on every old-image
        # start (review M4). Gitignore names still include the fallback skill.
        _write_skill(service, "multi", {"SKILL.md": "# m\n"})
        exec_script = (
            service.library_path / ".claude" / "skills" / "multi" / "run.sh"
        )
        exec_script.write_text("#!/bin/sh\n")
        exec_script.chmod(0o755)
        client = _wire(service, restore=_resp(404))
        with patch.object(skill_service_module, "get_agent_client", return_value=client):
            _run(service.inject_skills("agent-x", ["multi"], force=True))
        service._finalize_injected_dirs.assert_awaited_once()
        # #1842 added a 4th arg: the paths the prune deleted, so finalize can
        # reap the directories they emptied.
        _agent, names, exec_paths, pruned = service._finalize_injected_dirs.await_args.args
        assert names == ["multi"]
        assert exec_paths == []
        assert pruned == []   # fallback wrote only SKILL.md; nothing was pruned

    def test_total_cap_skips_overflow_skill(self, service, monkeypatch):
        monkeypatch.setenv("SKILLS_TOTAL_MAX_BYTES", "150")
        _write_skill(service, "first", {"SKILL.md": "x" * 100})
        _write_skill(service, "second", {"SKILL.md": "y" * 100})
        client = _wire(service, restore=_restore_ok_for(None))
        with patch.object(skill_service_module, "get_agent_client", return_value=client):
            result = _run(service.inject_skills(
                "agent-x", ["first", "second"], force=True))
        statuses = {n: r["status"] for n, r in result["results"].items()}
        assert sorted(statuses.values()) == ["failed", "injected"]
        failed = next(r for r in result["results"].values()
                      if r["status"] == "failed")
        assert "skills_total_cap_exceeded" in failed["error"]

    def test_invalid_names_never_reach_the_meta_exec(self, service):
        # The ONE name guard must run BEFORE any in-container exec (review M3):
        # inside the agent, os.path.join(base, "/abs") REPLACES base.
        _write_skill(service, "real", {"SKILL.md": "# r\n"})
        client = _wire(service, restore=_restore_ok_for(None))
        with patch.object(skill_service_module, "get_agent_client", return_value=client):
            _run(service.inject_skills(
                "agent-x", ["real", "../evil", "/abs"], force=True))
        passed_names = service._read_agent_skill_metas.await_args.args[1]
        assert passed_names == ["real"]


class TestLockSemantics:
    def test_release_only_deletes_own_token(self, service):
        fake = MagicMock()
        service._redis = fake
        # Successor holds a different token → release must NOT delete.
        fake.get.return_value = "someone-elses-token"
        service._release_inject_lock("my-token", "agent-x")
        fake.delete.assert_not_called()
        # Own token → release deletes.
        fake.get.return_value = "my-token"
        service._release_inject_lock("my-token", "agent-x")
        fake.delete.assert_called_once_with("skill_inject:agent-x")

    def test_lock_ttl_covers_one_restore_attempt(self):
        # A lock that expires mid-injection evaporates exactly in the slow
        # case it exists for (review M1): TTL must exceed one restore attempt
        # plus its repair retry.
        assert skill_service_module._INJECT_LOCK_TTL_SECONDS >= \
            2 * skill_service_module._RESTORE_TIMEOUT


class TestInjectedScriptsCompile:
    """The three injected-python scripts are f-strings — a brace-escaping or
    quoting regression would ship green if only mocked paths ran (review gap).
    Capture each rendered script via the _run_agent_python seam and compile it,
    with hostile (json-escaped) inputs."""

    def _capture(self, service):
        captured = []

        async def grab(agent_name, script):
            captured.append(script)
            return None  # fail-open path

        service._run_agent_python = grab
        return captured

    def test_all_three_scripts_compile_with_hostile_inputs(self, service):
        captured = self._capture(service)
        hostile_names = ['weird"quote', "back\\slash", "uni line"]
        hostile_paths = ['.claude/skills/x/a"b.sh', ".claude/skills/x/c\\d.py"]
        _run(service._read_agent_skill_metas("agent-x", hostile_names))
        _run(service._probe_dependencies("agent-x", ["ffmpeg"], ["KEY_A"]))
        _run(service._finalize_injected_dirs(
            "agent-x", hostile_names, hostile_paths))
        assert len(captured) == 3
        for script in captured:
            compile(script, "<injected>", "exec")

    def test_meta_script_executes_against_tmp_home(self, service, tmp_path,
                                                   monkeypatch):
        # Actually EXECUTE the meta-read script the way the agent would.
        captured = self._capture(service)
        _run(service._read_agent_skill_metas("agent-x", ["demo", "absent"]))
        script = captured[0]
        skill_dir = tmp_path / ".claude" / "skills" / "demo"
        skill_dir.mkdir(parents=True)
        (skill_dir / pkg.META_FILENAME).write_text(
            json.dumps({"version": "v1", "manifest": []}))
        monkeypatch.setenv("HOME", str(tmp_path))
        proc = subprocess.run(
            [sys.executable, "-"], input=script, text=True,
            capture_output=True, timeout=15,
        )
        assert proc.returncode == 0, proc.stderr
        out = json.loads(proc.stdout)
        assert out["demo"]["meta"]["version"] == "v1"
        assert out["absent"] == {"exists": False, "meta": None}


class TestAgentDeleteGuardInterplay:
    def test_skill_dirs_are_not_protected_paths(self):
        # Prune + the repair retry DELETE through the agent files router; if
        # someone later adds `.claude` to PROTECTED_PATHS both silently
        # degrade to warnings while mocked tests stay green (review gap).
        files_mod = _load(
            "docker/base-image/agent_server/routers/files.py",
            "_ent183_agent_files",
        )
        assert not files_mod._is_protected_path(
            Path("/home/developer/.claude/skills/demo"))
        assert not files_mod._is_protected_path(
            Path("/home/developer/.claude/skills/demo/scripts/run.sh"))
        # ...while the actually-protected names stay protected.
        assert files_mod._is_protected_path(Path("/home/developer/.env"))


class TestClaudeMdSection:
    def test_section_lists_all_present_skills_with_annotations(self, service):
        _write_skill(service, "changed", {"SKILL.md": "# c\n"})
        _write_skill(service, "same", {"SKILL.md": "# s\n"})
        metas = {"same": {"exists": True,
                          "meta": {"version": "tree-same", "manifest": []}},
                 "changed": {"exists": False, "meta": None}}
        client = _wire(service, metas=metas, restore=_restore_ok_for(None))
        with patch.object(skill_service_module, "get_agent_client", return_value=client):
            result = _run(service.inject_skills(
                "agent-x", ["changed", "same"], force=False))
        assert result["results"]["same"]["status"] == "unchanged"
        assert result["results"]["changed"]["status"] == "injected"
        written = client.write_file.await_args_list[-1].args
        assert written[0] == "CLAUDE.md"
        content = written[1]
        # BOTH skills listed, not just this pass's write (eng review #5)
        assert "- `/changed`" in content and "- `/same`" in content

    def test_section_replace_does_not_eat_h3_lookalike(self, service):
        _write_skill(service, "demo", {"SKILL.md": "# d\n"})
        existing = (
            "# Agent\n\n### Platform Skills Advice\nkeep me\n\n"
            "## Platform Skills\n\nold list\n\n## After\nkeep this too\n"
        )
        client = _wire(service, restore=_restore_ok_for(None))
        client.read_file = AsyncMock(
            return_value={"success": True, "content": existing})
        with patch.object(skill_service_module, "get_agent_client", return_value=client):
            _run(service.inject_skills("agent-x", ["demo"], force=True))
        content = client.write_file.await_args_list[-1].args[1]
        assert "### Platform Skills Advice" in content
        assert "keep me" in content and "keep this too" in content
        assert "old list" not in content
        assert content.count("## Platform Skills\n") >= 1


# ─────────────────────────────────────────────────────────────────────────────
# Part D — real git end-to-end: repo → archive → filter → tar → REAL restore
# ─────────────────────────────────────────────────────────────────────────────

def _git(cwd, *args):
    subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    )


class TestRealGitPipeline:
    def test_git_archive_to_real_restore(self, tmp_path):
        library = tmp_path / "library"
        skill_dir = library / ".claude" / "skills" / "demo"
        (skill_dir / "scripts").mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\ndescription: end-to-end\nautomation: autonomous\n---\n# demo\n")
        script = skill_dir / "scripts" / "run.sh"
        script.write_text("#!/bin/sh\necho ok\n")
        script.chmod(0o755)
        (skill_dir / "link.md").symlink_to("SKILL.md")
        _git(library, "init", "-q")
        _git(library, "add", "-A")
        _git(library, "commit", "-qm", "seed")

        # ent#237: git operations are per-source (SkillSourceClone), so the
        # clone is constructed directly here — this test is about the real
        # archive→filter→restore pipeline, not about source resolution.
        clone = SkillSourceClone(
            _TEST_SOURCE_ID, str(library), "main", "branch", library.parent
        )
        clone.path = library

        shas = clone.tree_shas()
        assert "demo" in shas and len(shas["demo"]) == 40

        archive = clone.archive_skill("demo")
        assert archive
        members, warnings, _ = pkg.filter_skill_archive(archive, "demo")
        names = [m[0] for m in members]
        assert ".claude/skills/demo/SKILL.md" in names
        assert ".claude/skills/demo/scripts/run.sh" in names
        assert "symlink_skipped:link.md" in warnings

        # git modes carry the exec bit through the filter
        assert pkg.executable_paths(members) == [".claude/skills/demo/scripts/run.sh"]

        meta = {"version": shas["demo"], "manifest": names}
        blob = pkg.build_injection_tar("demo", members, meta)
        agent_home = tmp_path / "agent-home"
        agent_home.mkdir()
        restored, skipped = snapshot.restore_from_tar(
            agent_home, blob, [".claude/skills/demo/**"]
        )
        assert skipped == []
        assert (agent_home / ".claude/skills/demo/scripts/run.sh").exists()
        on_disk = json.loads(
            (agent_home / ".claude/skills/demo/.trinity-skill.json").read_text())
        assert on_disk["version"] == shas["demo"]

    def test_tree_sha_is_checkout_independent(self, tmp_path):
        # Two clones of identical content must agree on the version — the
        # whole point of tree-SHA versioning over content hashing (mtimes).
        origin = tmp_path / "origin"
        skill = origin / ".claude" / "skills" / "demo"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("# demo\n")
        _git(origin, "init", "-q")
        _git(origin, "add", "-A")
        _git(origin, "commit", "-qm", "seed")

        clone_a = tmp_path / "a"
        clone_b = tmp_path / "b"
        _git(tmp_path, "clone", "-q", str(origin), str(clone_a))
        _git(tmp_path, "clone", "-q", str(origin), str(clone_b))

        a = SkillSourceClone(_TEST_SOURCE_ID, str(origin), "main", "branch", tmp_path)
        b = SkillSourceClone("src_bbbbbbbb", str(origin), "main", "branch", tmp_path)
        a.path, b.path = clone_a, clone_b
        assert a.tree_shas()["demo"] == b.tree_shas()["demo"]


class TestListAndGetSurface:
    def test_list_skills_carries_contract(self, service):
        _write_skill(service, "demo", {
            "SKILL.md": (
                "---\ndescription: d\nautomation: gated\nuser_invocable: false\n"
                "requires:\n  binaries: [ffmpeg]\n---\n# demo\n"
            ),
            "scripts/x.py": "pass\n",
        })
        service.test_clone.tree_shas = MagicMock(return_value={"demo": "sha"})
        service.test_clone.current_commit = MagicMock(return_value=None)
        skills = service.list_skills()
        assert len(skills) == 1
        s = skills[0]
        assert s["automation"] == "gated"
        assert s["user_invocable"] is False
        assert s["requires"]["binaries"] == ["ffmpeg"]
        assert s["multi_file"] is True and s["file_count"] == 2
        assert s["version"] == "sha"

    def test_get_skill_rejects_traversal_names(self, service):
        assert service.get_skill("../../etc/passwd") is None
        assert service.get_skill("..") is None

    def test_skill_dir_containment_holds_without_the_regex(self, service,
                                                           monkeypatch):
        # The realpath containment check is the guard that must still hold if
        # the name regex is ever loosened — pin it independently by disabling
        # the regex (CodeQL flagged these joins; both guards are load-bearing).
        # ent#237: the realpath containment check moved onto SkillSourceClone,
        # applied against the OWNING source's root (a shared root would let a
        # symlink in one source resolve into another's checkout). The service's
        # _skill_dir now also requires the skill to EXIST in a source, so the
        # containment property is pinned at its new home to keep it independent
        # of existence.
        monkeypatch.setattr(pkg, "validate_skill_name", lambda name: True)
        clone = service.test_clone
        assert clone.skill_dir("../../../etc") is None
        assert clone.skill_dir("..") is None
        assert clone.skill_dir("ok") is not None

    def test_skill_dir_refuses_symlink_escaping_the_root(self, service,
                                                         tmp_path):
        # A symlinked skill dir pointing outside the library must not resolve
        # into a readable package (realpath, not lexical, containment).
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "SKILL.md").write_text("# stolen\n")
        link = service.library_path / ".claude" / "skills" / "sneaky"
        link.symlink_to(outside, target_is_directory=True)
        assert service._skill_dir("sneaky") is None
        assert service.get_skill("sneaky") is None

    def test_get_skill_lists_files(self, service):
        _write_skill(service, "demo", {"SKILL.md": "# d\n", "ref/a.txt": "x"})
        service._git_tree_shas = MagicMock(return_value={"demo": "sha"})
        skill = service.get_skill("demo")
        paths = {f["path"] for f in skill["files"]}
        assert paths == {"SKILL.md", "ref/a.txt"}
        assert skill["content"].startswith("# d")


class TestPrunedEmptyDirReap:
    """#1842: `compute_prune` diffs MANIFESTS and a manifest holds files, so a
    package that drops a subdirectory had its files deleted and the directory
    left standing. Reproduced live: after `reference/notes.md` was pruned,
    `~/.claude/skills/pkg-probe/reference/` remained as an empty directory that
    no later injection ever removed.

    These execute the real finalize script against a tmp home — a mocked
    assertion on the arg list would not catch a bad rmdir climb."""

    def _run_finalize(self, service, tmp_path, monkeypatch, pruned):
        captured = []

        async def grab(agent_name, script):
            captured.append(script)
            return None

        service._run_agent_python = grab
        _run(service._finalize_injected_dirs("agent-x", ["pkg"], [], pruned))
        monkeypatch.setenv("HOME", str(tmp_path))
        proc = subprocess.run(
            [sys.executable, "-"], input=captured[0], text=True,
            capture_output=True, timeout=15,
        )
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout)

    def _skill(self, tmp_path):
        d = tmp_path / ".claude" / "skills" / "pkg"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("# pkg\n")
        return d

    def test_directory_emptied_by_the_prune_is_removed(self, service, tmp_path, monkeypatch):
        skill = self._skill(tmp_path)
        (skill / "reference").mkdir()          # its only file was just pruned
        out = self._run_finalize(
            service, tmp_path, monkeypatch,
            [".claude/skills/pkg/reference/notes.md"],
        )
        assert out["rmdir"] == 1
        assert not (skill / "reference").exists()
        assert (skill / "SKILL.md").exists()   # the package itself is untouched

    def test_directory_still_holding_files_is_kept(self, service, tmp_path, monkeypatch):
        """`os.rmdir` IS the safety property: runtime artifacts a skill's own
        scripts wrote (__pycache__, downloaded models) keep the dir alive."""
        skill = self._skill(tmp_path)
        (skill / "scripts").mkdir()
        (skill / "scripts" / "run.sh").write_text("#!/bin/sh\n")
        (skill / "scripts" / "__pycache__").mkdir()
        (skill / "scripts" / "__pycache__" / "x.pyc").write_bytes(b"\x00")
        out = self._run_finalize(
            service, tmp_path, monkeypatch,
            [".claude/skills/pkg/scripts/old.py"],
        )
        assert out["rmdir"] == 0
        assert (skill / "scripts" / "run.sh").exists()
        assert (skill / "scripts" / "__pycache__" / "x.pyc").exists()

    def test_climb_stops_at_the_skill_root(self, service, tmp_path, monkeypatch):
        """Even with every level empty, the skill root and `.claude/skills`
        above it must survive — the package is still installed."""
        skill = self._skill(tmp_path)
        (skill / "a" / "b").mkdir(parents=True)
        out = self._run_finalize(
            service, tmp_path, monkeypatch, [".claude/skills/pkg/a/b/gone.txt"],
        )
        assert out["rmdir"] == 2                    # b/, then a/
        assert not (skill / "a").exists()
        assert skill.exists() and (skill / "SKILL.md").exists()
        assert (tmp_path / ".claude" / "skills").is_dir()

    def test_paths_outside_the_skills_prefix_are_ignored(self, service, tmp_path, monkeypatch):
        """Prefix confinement mirrors compute_prune's own guard: a doctored
        manifest must never steer an rmdir elsewhere in the agent home."""
        self._skill(tmp_path)
        (tmp_path / "workspace").mkdir()
        (tmp_path / ".claude" / "loose").mkdir()
        out = self._run_finalize(
            service, tmp_path, monkeypatch,
            ["workspace/data.csv", ".claude/loose/x.txt", "../../etc/passwd"],
        )
        assert out["rmdir"] == 0
        assert (tmp_path / "workspace").is_dir()
        assert (tmp_path / ".claude" / "loose").is_dir()

    def test_symlinked_directory_is_not_followed(self, service, tmp_path, monkeypatch):
        """A symlink where a package dir used to be must not be unlinked —
        os.rmdir refuses it (NotADirectoryError), so the target is safe."""
        skill = self._skill(tmp_path)
        target = tmp_path / "elsewhere"
        target.mkdir()
        (skill / "reference").symlink_to(target, target_is_directory=True)
        out = self._run_finalize(
            service, tmp_path, monkeypatch,
            [".claude/skills/pkg/reference/notes.md"],
        )
        assert out["rmdir"] == 0
        assert target.is_dir()
        assert (skill / "reference").is_symlink()

    def test_traversal_segment_is_rejected_by_the_reap(self, service, tmp_path, monkeypatch):
        """#1842 review: the script's own prefix check is satisfied by
        `.claude/skills/pkg/../../..`, so a `..` segment would climb out of the
        skill root. compute_prune filters these upstream — this pins the reap's
        last line of defense rather than trusting the caller."""
        skill = self._skill(tmp_path)
        (skill / "reference").mkdir()
        victim = tmp_path / "victim"
        victim.mkdir()
        out = self._run_finalize(
            service, tmp_path, monkeypatch,
            [".claude/skills/pkg/../../victim/x.txt", ".claude/skills/pkg/reference/notes.md"],
        )
        assert victim.is_dir()          # never touched
        assert out["rmdir"] == 1        # only the legitimate one was reaped
        assert not (skill / "reference").exists()
