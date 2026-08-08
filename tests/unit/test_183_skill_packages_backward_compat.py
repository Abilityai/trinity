"""#183 full-directory skill packages — backward-compatibility regression (ent#187).

ent#187 decision: **accept the risk, no kill-switch** — #183 is strictly additive
and backward-compatible, so a flag is unwarranted (and would have a limited life:
Phases 2-3, #139/#178, build on this contract). This test is the AC-required proof
that a **legacy-format skill (no frontmatter)** and a **legacy-synced agent
(no prior injection manifest)** both survive the new sync/list/get path.

The two backward-compat questions ent#187 asks, each pinned here:

1. **A skill directory with NO frontmatter** — parses cleanly to an empty
   contract, stays `user_invocable=True`, gets a first-paragraph description
   fallback, and raises no contract warnings. It lists and loads exactly as
   before #183 (no regression on the universal skill-load path).

2. **An agent whose skills were synced under the OLD format** (no
   `.trinity-skill.json` manifest) — `compute_prune` deletes **nothing** without a
   prior manifest ("no-meta → full inject, no prune — safe direction"), so the
   platform never destroys files it did not write. The agent self-heals to the
   new package format on the next sync.

Pure/deterministic — packaging functions + `_parse_skill_info` over a temp file;
no container, no git, no network.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


@pytest.fixture
def pkg():
    try:
        import services.skill_packaging as pkg
    except ImportError:
        pytest.skip("backend venv required")
    return pkg


# ---------------------------------------------------------------------------
# Q1 — a skill with no frontmatter is strictly additive
# ---------------------------------------------------------------------------

def test_missing_frontmatter_parses_clean(pkg):
    """No `---` block → (None, None): missing frontmatter is NOT an error."""
    fm, warning = pkg.parse_frontmatter("# My Skill\n\nDoes a useful thing.\n")
    assert fm is None
    assert warning is None  # never flagged frontmatter_invalid


def test_empty_contract_defaults_are_backward_compatible(pkg):
    """extract_contract(None) → the pre-#183 defaults: invocable, no deps."""
    contract, warnings = pkg.extract_contract(None)
    assert contract["user_invocable"] is True          # legacy skills stay invocable
    assert contract["description"] is None
    assert contract["automation"] is None
    assert contract["allowed_tools"] is None
    assert contract["requires"] == {"packages": [], "binaries": [], "env": []}
    assert warnings == []                               # no dep/contract warnings


def test_legacy_skill_md_lists_and_loads(pkg, tmp_path, monkeypatch):
    """A legacy no-frontmatter SKILL.md is parsed into a valid, invocable entry
    with a first-paragraph description — the list/get surface is unchanged."""
    from services.skill_service import SkillService

    skill_md = tmp_path / "SKILL.md"
    skill_md.write_text("# Legacy Skill\n\nRuns the legacy thing.\n")

    svc = SkillService()
    # _parse_skill_info only calls _skill_files for file_count/size — stub it so
    # this stays a pure parse test (no library dir needed). ent#237 added the
    # owning-clone argument to both (the clone is passed explicitly rather than
    # re-resolved, so a non-winning copy can't be described as a different
    # source's files); a stub stands in for it here.
    monkeypatch.setattr(svc, "_skill_files", lambda clone, name: [])

    info = svc._parse_skill_info(None, "legacy-skill", skill_md)
    assert info["name"] == "legacy-skill"
    assert info["user_invocable"] is True
    assert info["description"] == "Runs the legacy thing."   # first-paragraph fallback
    assert info["contract_warnings"] == []
    assert info["requires"] == {"packages": [], "binaries": [], "env": []}


# ---------------------------------------------------------------------------
# Q2 — a legacy-synced agent (no prior manifest) is never destructively pruned
# ---------------------------------------------------------------------------

def test_no_prior_manifest_prunes_nothing(pkg):
    """The 'safe direction': without a previous injection manifest, compute_prune
    deletes nothing — a legacy-synced (or unmanaged) agent dir is re-injected,
    never destroyed."""
    stale, truncated = pkg.compute_prune(None, [".claude/skills/s/SKILL.md"], "s")
    assert stale == []
    assert truncated is False
    # A non-list junk value is treated the same (untrusted agent-side state).
    assert pkg.compute_prune("not-a-list", [".claude/skills/s/a"], "s") == ([], False)


def test_prune_only_removes_previously_written_files(pkg):
    """When a prior #183 manifest DOES exist, prune removes only what changed —
    proving the new-format path is well-behaved (the counterpart to Q2)."""
    prev = [".claude/skills/s/old.md", ".claude/skills/s/keep.md"]
    new = [".claude/skills/s/keep.md"]
    stale, truncated = pkg.compute_prune(prev, new, "s")
    assert stale == [".claude/skills/s/old.md"]
    assert truncated is False
