"""ent#332 — vendor-neutral library layout: per-source skills root.

The library-side layout (`.claude/skills/` in a SOURCE repo) is now resolvable
per source: a root `catalog.yaml` `skills_root:` declaration wins, else a
`skills/` directory carrying SKILL.md evidence, else the legacy
`.claude/skills/` fallback. The agent-side destination is INVARIANT — packaging
rewrites tar arcnames to `.claude/skills/<name>/…` at `filter_skill_archive`,
so manifests, prune confinement and the whole ent#236 removal machinery stay
destination-canonical with zero migration.

Three properties carry the design and are what these tests pin:

1. **Resolution order + fail-safe fallthrough** — every invalid tier (bad
   catalog, symlinked probe dir, escaping declared root) falls through to the
   next; a source serving skills today can never be blanked by a tier it does
   not use. A dual-layout repo (evidence under BOTH roots, no catalog) keeps
   `.claude/skills/` — switching executable content requires the explicit
   declaration, never a silent probe flip.
2. **Segment-wise validation** — a whole-string charset regex admits `.`,
   `./skills` and `skills//x`, each of which breaks archive-prefix math into
   per-skill "empty package" failures. Rejection is per segment.
3. **Arcname rewrite at the ONE packaging point** — post-filter members are
   destination-canonical for any source layout (identity for the legacy one),
   which is what keeps `executable_paths`, restore accounting, the
   legacy-fallback SKILL.md lookup and cross-layout prune continuity correct.
"""

from __future__ import annotations

import asyncio
import io
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

_BACKEND_STR = str(Path(__file__).resolve().parents[2] / "src" / "backend")
if _BACKEND_STR not in sys.path:
    sys.path.insert(0, _BACKEND_STR)

pytestmark = pytest.mark.unit


# =============================================================================
# Fixtures (the ent#237 harness shapes)
# =============================================================================

@pytest.fixture(autouse=True)
def _real_modules_not_stubs(monkeypatch):
    """Undo another module's import-time `sys.modules` stubs for this file.

    `test_ent183_skill_packages` installs fake modules at IMPORT time (#1898).
    A stub has no `__file__`, so it is trivially detectable — evict it and let
    the next import load the real module. `monkeypatch.delitem` so the eviction
    is undone at teardown (`tests/lint_sys_modules.py` enforces this).
    """
    import importlib

    for name in ("utils.url_validation", "utils.safe_yaml",
                 "services.skill_service", "services.skill_source_clone",
                 "services.skill_packaging"):
        mod = sys.modules.get(name)
        if mod is not None and getattr(mod, "__file__", None) is None:
            monkeypatch.delitem(sys.modules, name, raising=False)
    try:
        importlib.import_module("utils.url_validation")
    except Exception:  # noqa: BLE001 — a genuinely broken import fails in the test
        pass
    yield


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _mkrepo(
    root: Path,
    name: str,
    skills: dict,
    *,
    layout: str = ".claude/skills",
    catalog: str | None = None,
    extra_files: dict | None = None,
    commit: bool = True,
) -> Path:
    """A real git repo laid out as a skills library under `layout`."""
    repo = root / name
    repo.mkdir(parents=True)
    for skill, body in skills.items():
        d = repo / layout / skill
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {skill}\ndescription: {body}\n---\n{body}\n"
        )
    if catalog is not None:
        (repo / "catalog.yaml").write_text(catalog)
    for rel, content in (extra_files or {}).items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    if commit:
        _git(repo, "init", "-q", "-b", "main")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "seed")
    return repo


def _clone_of(repo: Path, tmp_path: Path, source_id: str = "src_aaaaaaaa"):
    """A synced SkillSourceClone over a fixture repo (branch source)."""
    from services.skill_source_clone import SkillSourceClone

    clone = SkillSourceClone(source_id, str(repo), "main", "branch", tmp_path / "clones")
    outcome = clone.sync(str(repo))
    assert outcome.get("success"), outcome
    return clone


def _run(coro):
    return asyncio.run(coro)


# =============================================================================
# 1. Resolution order + probe evidence gating
# =============================================================================

class TestRootResolution:
    def test_catalog_declaration_wins_over_both_layouts(self, tmp_path):
        repo = _mkrepo(
            tmp_path / "r", "u", {"a": "custom"}, layout="my/skills",
            catalog="schema_version: 1\nskills_root: my/skills\n",
            extra_files={
                # Both conventional roots ALSO carry evidence — the explicit
                # declaration outranks every probe.
                "skills/decoy/SKILL.md": "---\nname: decoy\n---\n",
                ".claude/skills/decoy2/SKILL.md": "---\nname: decoy2\n---\n",
            },
        )
        clone = _clone_of(repo, tmp_path)
        assert clone.skills_rel_root() == "my/skills"
        assert clone.skill_names() == ["a"]

    def test_skills_dir_with_evidence_wins_without_catalog(self, tmp_path):
        repo = _mkrepo(tmp_path / "r", "u", {"a": "x"}, layout="skills")
        clone = _clone_of(repo, tmp_path)
        assert clone.skills_rel_root() == "skills"
        assert clone.skill_names() == ["a"]

    def test_legacy_layout_needs_zero_config(self, tmp_path):
        repo = _mkrepo(tmp_path / "r", "u", {"a": "x"})
        clone = _clone_of(repo, tmp_path)
        assert clone.skills_rel_root() == ".claude/skills"
        assert clone.skill_names() == ["a"]

    def test_skills_dir_without_evidence_falls_back(self, tmp_path):
        repo = _mkrepo(
            tmp_path / "r", "u", {"a": "x"},
            extra_files={"skills/README.md": "not a skill dir\n"},
        )
        clone = _clone_of(repo, tmp_path)
        assert clone.skills_rel_root() == ".claude/skills"
        assert clone.skill_names() == ["a"]

    def test_dual_layout_keeps_legacy_and_flags_conflict(self, tmp_path):
        """Evidence under BOTH roots + no catalog ⇒ NO silent content swap."""
        repo = _mkrepo(
            tmp_path / "r", "u", {"a": "legacy copy"},
            extra_files={"skills/b/SKILL.md": "---\nname: b\n---\nnew copy\n"},
        )
        clone = _clone_of(repo, tmp_path)
        assert clone.skills_rel_root() == ".claude/skills"
        assert clone.dual_layout is True
        assert clone.skill_names() == ["a"]

    def test_declared_but_absent_root_is_honest_empty(self, tmp_path):
        repo = _mkrepo(
            tmp_path / "r", "u", {"a": "x"},
            catalog="skills_root: skills\n",  # declared; dir does not exist
        )
        clone = _clone_of(repo, tmp_path)
        assert clone.skills_rel_root() == "skills"
        assert clone.skill_names() == []

    def test_explicit_legacy_declaration_and_trailing_slash(self, tmp_path):
        repo = _mkrepo(
            tmp_path / "r", "u", {"a": "x"},
            catalog="skills_root: .claude/skills/\n",
        )
        clone = _clone_of(repo, tmp_path)
        assert clone.skills_rel_root() == ".claude/skills"
        assert clone.skill_names() == ["a"]

    def test_missing_checkout_never_raises(self, tmp_path):
        """A never-cloned / mid-operation-rmtree'd source resolves calmly."""
        from services.skill_source_clone import SkillSourceClone

        clone = SkillSourceClone(
            "src_deadbeef", "unused", "main", "branch", tmp_path / "clones"
        )
        assert clone.skills_rel_root() == ".claude/skills"
        assert clone.skill_names() == []
        assert clone.tree_shas() == {}


# =============================================================================
# 2. catalog.yaml validation — every bad shape falls back, never raises
# =============================================================================

class TestCatalogValidation:
    @pytest.mark.parametrize("value", [
        ".", "./skills", "skills//x", "skills/.", "../x", "/abs",
        "a/../b", "-flag", "", "skills/", "a" * 201,
    ])
    def test_validate_declared_root_rejects(self, value):
        from services.skill_source_clone import validate_declared_root

        if value == "skills/":
            # Trailing slash is NORMALIZED, not rejected.
            assert validate_declared_root(value) == "skills"
        else:
            assert validate_declared_root(value) is None

    @pytest.mark.parametrize("value,expected", [
        ("skills", "skills"),
        ("nested/skills", "nested/skills"),
        (".claude/skills", ".claude/skills"),
    ])
    def test_validate_declared_root_accepts(self, value, expected):
        from services.skill_source_clone import validate_declared_root

        assert validate_declared_root(value) == expected

    def test_non_string_value_rejected(self):
        from services.skill_source_clone import validate_declared_root

        assert validate_declared_root(["skills"]) is None
        assert validate_declared_root(1) is None
        assert validate_declared_root(None) is None

    @pytest.mark.parametrize("catalog", [
        "skills_root: [not, a, string]\n",           # non-string value
        "- just\n- a\n- list\n",                      # non-mapping document
        "skills_root: &a [*a]\n",                     # alias-bearing (REJECT)
        "{{{{ not yaml",                              # unparseable
        "schema_version: 2\nskills_root: skills\n",   # unknown schema
        "skills_root: ../escape\n",                   # traversal
    ])
    def test_bad_catalog_falls_back_to_probe(self, tmp_path, catalog):
        """A present-but-unusable catalog degrades to the probe tiers —
        HardenedYamlError (a ValueError) must be caught, never escape."""
        repo = _mkrepo(
            tmp_path / "r", "u", {}, catalog=catalog,
            extra_files={"skills/a/SKILL.md": "---\nname: a\n---\nx\n"},
        )
        clone = _clone_of(repo, tmp_path)
        assert clone.skills_rel_root() == "skills"
        assert clone.skill_names() == ["a"]

    def test_string_schema_version_1_is_tolerated(self, tmp_path):
        repo = _mkrepo(
            tmp_path / "r", "u", {"a": "x"}, layout="lib",
            catalog='schema_version: "1"\nskills_root: lib\n',
        )
        clone = _clone_of(repo, tmp_path)
        assert clone.skills_rel_root() == "lib"

    def test_oversized_catalog_ignored(self, tmp_path):
        repo = _mkrepo(
            tmp_path / "r", "u", {"a": "x"},
            catalog="skills_root: skills\n" + ("# pad\n" * 20000),
        )
        clone = _clone_of(repo, tmp_path)
        assert clone.skills_rel_root() == ".claude/skills"

    def test_symlinked_catalog_is_never_opened(self, tmp_path):
        target = tmp_path / "outside.yaml"
        target.write_text("skills_root: skills\n")
        repo = _mkrepo(
            tmp_path / "r", "u", {"a": "x"},
            extra_files={"skills/b/SKILL.md": "---\nname: b\n---\n"},
            commit=False,
        )
        (repo / "catalog.yaml").symlink_to(target)
        _git(repo, "init", "-q", "-b", "main")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "seed")
        clone = _clone_of(repo, tmp_path)
        # The symlinked catalog is refused; the probe still finds skills/,
        # BUT dual evidence keeps legacy — here .claude/skills has evidence
        # too, so legacy wins. The point: the symlink target's declaration
        # ("skills") must not be what decided it.
        assert clone.skills_rel_root() == ".claude/skills"


# =============================================================================
# 3. Symlink guards on the roots themselves
# =============================================================================

class TestSymlinkGuards:
    def test_symlinked_probe_dir_falls_back_not_blanks(self, tmp_path):
        """`skills -> .claude/skills` lists fine but HEAD sees a blob — the
        probe must lstat-refuse it so the source keeps serving via legacy."""
        repo = _mkrepo(tmp_path / "r", "u", {"a": "x"}, commit=False)
        (repo / "skills").symlink_to(repo / ".claude" / "skills")
        _git(repo, "init", "-q", "-b", "main")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "seed")
        clone = _clone_of(repo, tmp_path)
        assert clone.skills_rel_root() == ".claude/skills"
        assert clone.skill_names() == ["a"]

    def test_symlinked_declared_root_falls_back_to_probe(self, tmp_path):
        repo = _mkrepo(
            tmp_path / "r", "u", {"a": "x"},
            catalog="skills_root: lib\n", commit=False,
        )
        (repo / "lib").symlink_to(repo / ".claude" / "skills")
        _git(repo, "init", "-q", "-b", "main")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "seed")
        clone = _clone_of(repo, tmp_path)
        assert clone.skills_rel_root() == ".claude/skills"
        assert clone.skill_names() == ["a"]

    def test_escaping_final_tier_yields_empty_never_typeerror(self, tmp_path):
        """A symlinked `.claude/skills` escaping the clone refuses containment;
        every read path empty/None-propagates (eng F5)."""
        outside = tmp_path / "outside"
        (outside / "evil").mkdir(parents=True)
        (outside / "evil" / "SKILL.md").write_text("---\nname: evil\n---\n")
        repo = _mkrepo(tmp_path / "r", "u", {}, commit=False)
        (repo / ".claude").mkdir(parents=True, exist_ok=True)
        (repo / ".claude" / "skills").symlink_to(outside)
        _git(repo, "init", "-q", "-b", "main")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "seed")
        clone = _clone_of(repo, tmp_path)
        assert clone.skills_root() is None
        assert clone.skill_names() == []
        assert clone.skill_dir("evil") is None


# =============================================================================
# 4. Version parity across layouts
# =============================================================================

class TestVersionParity:
    def test_tree_sha_identical_for_identical_content(self, tmp_path):
        """The per-skill version is the tree SHA of the skill DIRECTORY —
        layout-independent, so a repo restructure with unchanged content keeps
        versions stable (no spurious fleet re-inject)."""
        legacy = _mkrepo(tmp_path / "r1", "legacy", {"a": "same body"})
        modern = _mkrepo(tmp_path / "r2", "modern", {"a": "same body"}, layout="skills")
        c1 = _clone_of(legacy, tmp_path / "c1", "src_aaaaaaaa")
        c2 = _clone_of(modern, tmp_path / "c2", "src_bbbbbbbb")
        shas1 = c1.tree_shas()
        shas2 = c2.tree_shas()
        assert shas1["a"] == shas2["a"]

    def test_archive_from_custom_root_packages_identically(self, tmp_path):
        import services.skill_packaging as pkg

        legacy = _mkrepo(tmp_path / "r1", "legacy", {"a": "same body"})
        modern = _mkrepo(tmp_path / "r2", "modern", {"a": "same body"}, layout="skills")
        c1 = _clone_of(legacy, tmp_path / "c1", "src_aaaaaaaa")
        c2 = _clone_of(modern, tmp_path / "c2", "src_bbbbbbbb")
        m1, w1, t1 = pkg.filter_skill_archive(
            c1.archive_skill("a"), "a", source_root=c1.skills_rel_root()
        )
        m2, w2, t2 = pkg.filter_skill_archive(
            c2.archive_skill("a"), "a", source_root=c2.skills_rel_root()
        )
        assert [(n, c) for n, c, _m in m1] == [(n, c) for n, c, _m in m2]
        assert t1 == t2
        assert m1[0][0] == ".claude/skills/a/SKILL.md"


# =============================================================================
# 5. The packaging rewrite (pure, synthetic tars)
# =============================================================================

def _tar_bytes(entries: dict[str, tuple[bytes, int]]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, (content, mode) in entries.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            info.mode = mode
            tf.addfile(info, io.BytesIO(content))
    return buf.getvalue()


class TestPackagingRewrite:
    def test_arcnames_rewritten_to_destination(self):
        import services.skill_packaging as pkg

        tar = _tar_bytes({
            "skills/x/SKILL.md": (b"---\nname: x\n---\n", 0o644),
            "skills/x/scripts/run.sh": (b"#!/bin/sh\n", 0o755),
        })
        members, warnings, total = pkg.filter_skill_archive(tar, "x", source_root="skills")
        names = sorted(n for n, _c, _m in members)
        assert names == [
            ".claude/skills/x/SKILL.md",
            ".claude/skills/x/scripts/run.sh",
        ]
        # executable_paths (→ agent-side chmod) must be destination-canonical.
        assert pkg.executable_paths(members) == [".claude/skills/x/scripts/run.sh"]
        assert not warnings

    def test_legacy_layout_rewrite_is_identity(self):
        import services.skill_packaging as pkg

        tar = _tar_bytes({".claude/skills/x/SKILL.md": (b"body", 0o644)})
        members, _w, _t = pkg.filter_skill_archive(tar, "x")
        assert members[0][0] == ".claude/skills/x/SKILL.md"

    def test_layout_mismatch_is_fail_visible(self):
        """A sync flipping the layout mid-inject yields the NAMED empty-package
        failure, never a silent half-package (eng F6)."""
        import services.skill_packaging as pkg

        tar = _tar_bytes({".claude/skills/x/SKILL.md": (b"body", 0o644)})
        members, warnings, total = pkg.filter_skill_archive(tar, "x", source_root="skills")
        assert members == []
        assert any(w.startswith("restore_skipped:") for w in warnings)

    @pytest.mark.parametrize("bad_root", ["/abs", "-flag", "a/../b", ".", "a//b", 5])
    def test_unsafe_source_root_fails_loud(self, bad_root):
        import services.skill_packaging as pkg

        with pytest.raises(ValueError):
            pkg.filter_skill_archive(b"", "x", source_root=bad_root)

    def test_cross_layout_prune_continuity(self):
        """A legacy-era installed manifest diffed against a skills/-layout
        re-injection of the same content deletes NOTHING — both sides are
        destination-canonical by construction."""
        import services.skill_packaging as pkg

        tar = _tar_bytes({
            "skills/x/SKILL.md": (b"---\nname: x\n---\n", 0o644),
            "skills/x/scripts/run.sh": (b"#!/bin/sh\n", 0o755),
        })
        members, _w, _t = pkg.filter_skill_archive(tar, "x", source_root="skills")
        new_manifest = [n for n, _c, _m in members]
        legacy_manifest = [
            ".claude/skills/x/SKILL.md",
            ".claude/skills/x/scripts/run.sh",
        ]
        stale, truncated = pkg.compute_prune(legacy_manifest, new_manifest, "x")
        assert stale == [] and truncated is False


# =============================================================================
# 6. Legacy old-image fallback with rewritten members (eng F7)
# =============================================================================

class TestLegacyFallback:
    def test_fallback_lookup_hits_rewritten_skill_md(self):
        import services.skill_packaging as pkg
        import services.skill_service as ss

        tar = _tar_bytes({"skills/x/SKILL.md": (b"---\nname: x\n---\nbody\n", 0o644)})
        members, _w, _t = pkg.filter_skill_archive(tar, "x", source_root="skills")

        class _Client:
            async def write_file(self, path, content):
                assert path == ".claude/skills/x/SKILL.md"
                return {"success": True}

        svc = ss.SkillService()
        result = _run(svc._legacy_fallback(_Client(), "x", members, []))
        assert result["status"] == "fallback"
        assert result["files_written"] == 1


# =============================================================================
# 7. End-to-end: mixed-layout sources through the real service merge
# =============================================================================

@pytest.fixture
def sources_db(tmp_path, monkeypatch):
    """A SkillSourcesOperations bound to a throwaway SQLite file."""
    import sqlite3

    db_path = tmp_path / "trinity.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")

    from db.schema import init_schema

    conn = sqlite3.connect(db_path)
    init_schema(conn.cursor(), conn)
    conn.commit()
    conn.close()

    import db.engine as engine_mod

    if hasattr(engine_mod.get_engine, "cache_clear"):
        engine_mod.get_engine.cache_clear()

    from db.skill_sources import SkillSourcesOperations

    return SkillSourcesOperations()


class _SourcesFacade:
    """The slice of the `db` facade skill_service uses (the ent#237 harness —
    injected so another module's sys.modules stubs can't decide these tests)."""

    def __init__(self, ops):
        self._ops = ops

    def list_skill_sources(self, enabled_only=False):
        return self._ops.list_sources(enabled_only)

    def get_default_skill_source(self):
        return self._ops.get_default_source()

    def count_skill_sources(self):
        return self._ops.count_sources()

    def create_skill_source(self, **kwargs):
        return self._ops.create_source(**kwargs)

    def record_skill_source_sync(self, source_id, **kwargs):
        return self._ops.record_sync(source_id, **kwargs)

    def get_setting_value(self, key, default=None):
        return default

    def set_setting(self, key, value):
        return True

    def delete_setting(self, key):
        return True


@pytest.fixture
def service(sources_db, tmp_path, monkeypatch):
    import services.skill_service as ss

    monkeypatch.setattr(ss, "validate_skills_library_url", lambda u: u)
    monkeypatch.setattr(ss, "db", _SourcesFacade(sources_db))
    monkeypatch.setattr(ss, "get_skills_library_url", lambda: None)
    monkeypatch.setattr(ss, "get_skills_library_branch", lambda: "main")
    svc = ss.SkillService()
    svc.library_root = tmp_path / "clones"
    svc.library_path = tmp_path / "clones"
    monkeypatch.setattr(svc, "_authenticated_url", lambda url, pat: url, raising=False)
    return svc


class TestMixedLayoutMerge:
    @pytest.fixture
    def mixed(self, service, sources_db, tmp_path):
        legacy = _mkrepo(tmp_path / "repos", "legacy", {"old-skill": "legacy body"})
        modern = _mkrepo(
            tmp_path / "repos", "modern", {"new-skill": "modern body"},
            layout="skills",
            catalog="schema_version: 1\nskills_root: skills/\n",
        )
        sources_db.create_source(name="Legacy", url=str(legacy), ref="main")
        sources_db.create_source(name="Modern", url=str(modern), ref="main")
        result = service.sync_library()
        assert result["success"], result
        return service

    def test_merged_listing_spans_both_layouts(self, mixed):
        skills = {s["name"]: s for s in mixed.list_skills()}
        assert set(skills) == {"old-skill", "new-skill"}
        # `path` is honest per-source provenance.
        assert skills["old-skill"]["path"] == ".claude/skills/old-skill/SKILL.md"
        assert skills["new-skill"]["path"] == "skills/new-skill/SKILL.md"
        # Versions stamped for both layouts.
        assert skills["new-skill"]["version"]

    def test_get_skill_reads_the_declared_root(self, mixed):
        skill = mixed.get_skill("new-skill")
        assert skill is not None
        assert "modern body" in skill["content"]

    def test_status_reports_resolved_root_per_source(self, mixed):
        status = mixed.get_library_status()
        roots = {s["name"]: s["skills_root"] for s in status["sources"]}
        assert roots == {"Legacy": ".claude/skills", "Modern": "skills"}
        assert all(s["layout_conflict"] is False for s in status["sources"])

    def test_status_surfaces_dual_layout_conflict(self, service, sources_db, tmp_path):
        """The dual-layout warning is operator-visible, not log-only."""
        dual = _mkrepo(
            tmp_path / "repos", "dual", {"a": "legacy copy"},
            extra_files={"skills/b/SKILL.md": "---\nname: b\n---\nnew copy\n"},
        )
        sources_db.create_source(name="Dual", url=str(dual), ref="main")
        assert service.sync_library()["success"]
        status = service.get_library_status()
        (entry,) = status["sources"]
        assert entry["skills_root"] == ".claude/skills"
        assert entry["layout_conflict"] is True
