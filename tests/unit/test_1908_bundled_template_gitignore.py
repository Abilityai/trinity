"""Bundled-template `.gitignore` hygiene guard + regenerator (#1908).

Trinity grades every agent against `docs/agent-validation-spec.md`, but nothing
graded the templates Trinity itself ships. All 14 **visible** bundled templates
failed the same four HARD security checks at birth:

  * `sage` / `scout` / `scribe` shipped **no** `.gitignore` at all;
  * the 11 `dd-*` templates shipped a two-line one (`outputs/` + `*.log`) that
    covered none of `.env`, `.mcp.json`, `.claude/projects/`, `.trinity/`.

This module does two jobs:

1. **Regenerator** — ``python tests/unit/test_1908_bundled_template_gitignore.py
   --regenerate`` rewrites every guarded template's `.gitignore` from the
   canonical ```gitignore``` block in `docs/TRINITY_COMPATIBLE_AGENT_GUIDE.md`
   (itself parity-tested against `git_service._GITIGNORE_PATTERNS` by
   `test_github_init_gitignore.py::test_doc_and_constant_in_sync`). Same
   pattern as `tests/lint_sys_modules.py --regenerate-baseline`: the guard and
   the fix live in one file, so "the constant gained an entry" is a one-command
   change no matter how many templates are guarded.

2. **Guard** — the tests below evaluate the **real**
   `services.compatibility.static_checks.run_static` over a collector-shaped
   snapshot built from each template directory, so they can never drift into a
   second, private definition of the check rules (if `S-005` later accepts a new
   form, the guard follows the spec automatically).

Scope note — `hidden: true` is a **catalog** flag, not a security boundary.
`services/agent_service/crud.py` has no `hidden` gate and
`template_service.get_local_templates()`'s own docstring says hidden templates
stay "fully resolvable by id … and creatable by id". So hiding a template does
NOT stop it birthing an agent with these findings; `GUARDED_TEMPLATES` names the
templates this change actually fixes and claims nothing beyond them. The one
universal assertion (`G-001`, no wholesale `.claude/` exclusion) runs over
**every** bundled directory, hidden included.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Bootstrap so this file also runs standalone as the regenerator
# (`python tests/unit/test_1908_bundled_template_gitignore.py --regenerate`).
# Under pytest every line here is a no-op: tests/unit/conftest.py has already
# put src/backend on sys.path and set the same env sentinels, and `setdefault`
# never overwrites. Kept above the backend imports because module-level imports
# run before anything else in this file.
# ---------------------------------------------------------------------------
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = REPO_ROOT / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")
os.environ.setdefault("AGENT_AUTH_SECRET", "0" * 64)
os.environ.setdefault("TRINITY_DB_PATH", "/tmp/trinity-1908-regen.db")

import pytest  # noqa: E402

from services import template_service  # noqa: E402
from services.compatibility import spec  # noqa: E402
from services.compatibility.collector import (  # noqa: E402
    _FILE_CAP,
    _FIXED_FILES,
    _MAX_SKILL_FILES,
)
from services.compatibility.static_checks import run_static  # noqa: E402
from services.git_service import _GITIGNORE_PATTERNS as CANONICAL  # noqa: E402


TEMPLATES_DIR = REPO_ROOT / "config" / "agent-templates"
GUIDE = REPO_ROOT / "docs" / "TRINITY_COMPATIBLE_AGENT_GUIDE.md"

# The templates this change brings to the `.gitignore` HARD-check bar. An
# explicit tuple, NOT a live `hidden`-derived query: a later catalog change that
# hides `dd-*` (#1931) must not silently shrink this guard's coverage, because
# a hidden template is still creatable by id. `test_every_visible_template_is_guarded`
# catches the opposite direction — a NEW visible template added without one.
GUARDED_TEMPLATES = (
    "dd-bizmodel",
    "dd-captable",
    "dd-competitor",
    "dd-compliance",
    "dd-founder",
    "dd-intake",
    "dd-lead",
    "dd-legal",
    "dd-market",
    "dd-tech",
    "dd-traction",
    "sage",
    "scout",
    "scribe",
)

# Template-specific exclusions preserved from a template's pre-existing
# `.gitignore`, appended after the canonical block. The regenerator carries them
# so re-running it never silently drops a template's own intent. `*.log` is NOT
# here: it is already in the canonical list.
_DD_EXTRAS = ("outputs/",)
TEMPLATE_EXTRAS: dict[str, tuple[str, ...]] = {
    name: _DD_EXTRAS for name in GUARDED_TEMPLATES if name.startswith("dd-")
}

# HARD static checks that these templates still fail after this change, with the
# reason. Check-level, not template-level: it cannot hide a whole template, and
# `test_known_failing_checks_are_not_stale` fails the day it stops being true.
_KNOWN_FAILING_CHECKS = {
    # `resources.cpu` / `resources.memory` absent from the starters'
    # `template.yaml`. A template-level `resources` block OVERRIDES the admin's
    # fleet-wide default (RES-001), so whether the default starters should pin
    # or inherit is a product decision, not a hygiene fix — filed as a
    # follow-up, deliberately not fixed here. The 11 `dd-*` already pin both.
    "T-004",
    "T-005",
}

# HARD static checks that legitimately return "skipped" on their own
# precondition, with the exact reason. A precondition skip is NOT a finding —
# `compatibility.__init__._counts` counts only `status == "fail"` — but the pair
# is pinned so that a *new* skip (e.g. a vanished `template.yaml` taking every
# `T-*` check dark, or a renamed id returning "not_implemented") fails the guard
# instead of quietly greening it.
_EXPECTED_SKIPS = {
    # No bundled template ships a `dashboard.yaml` (that gap is F-010, soft).
    "D-003": "no_dashboard",
}

# The `.gitignore`-decidable checks: the four HARD security ones plus G-001.
GITIGNORE_HARD_IDS = ["S-001", "S-002", "S-004", "S-005", "G-001"]
# Every check whose verdict is decided by `.gitignore` content alone.
GITIGNORE_ALL_IDS = [
    "F-003", "S-001", "S-002", "S-004", "S-005",
    "S-006", "S-007", "S-008", "G-001", "G-002",
]

_HEADER = """\
# Trinity bundled-template .gitignore -- GENERATED, do not hand-edit.
#
# Mirrors `_GITIGNORE_PATTERNS` in src/backend/services/git_service.py, the
# single source of truth the platform re-appends to every agent's .gitignore on
# each git sync. Shipping it in the template moves that protection from *first
# sync* to *first boot* -- the window a freshly created agent's compatibility
# report observes (#1908).
#
# Regenerate every bundled template with:
#   python tests/unit/test_1908_bundled_template_gitignore.py --regenerate
#
# Do NOT add a bare `.claude/` line: Claude Code commands/skills/agents must
# stay committed (compatibility check G-001 is HARD and fails on it).

"""

REGEN_CMD = "python tests/unit/test_1908_bundled_template_gitignore.py --regenerate"


# ---------------------------------------------------------------------------
# Regenerator
# ---------------------------------------------------------------------------

def canonical_block() -> str:
    """The ```gitignore``` fence from the agent guide (no trailing newline)."""
    text = GUIDE.read_text(encoding="utf-8")
    m = re.search(r"```gitignore\n(.*?)\n```", text, re.S)
    assert m, f"no ```gitignore``` block found in {GUIDE}"
    return m.group(1)


def render(name: str, block: str | None = None) -> str:
    """The exact bytes template ``name``'s `.gitignore` must contain."""
    body = _HEADER + (canonical_block() if block is None else block) + "\n"
    extras = TEMPLATE_EXTRAS.get(name)
    if extras:
        body += (
            "\n# Template-specific (preserved from this template's own .gitignore)\n"
            + "".join(f"{line}\n" for line in extras)
        )
    return body


def regenerate() -> list[Path]:
    """Rewrite every guarded template's `.gitignore`. Idempotent."""
    block = canonical_block()
    written = []
    for name in GUARDED_TEMPLATES:
        path = TEMPLATES_DIR / name / ".gitignore"
        path.write_text(render(name, block), encoding="utf-8")
        written.append(path)
    return written


# ---------------------------------------------------------------------------
# Snapshot builder — mirrors services/compatibility/collector.py's in-container
# script over a template directory on the host. Imported constants (not retyped
# lists) so the guard tracks the collector instead of drifting from it.
# ---------------------------------------------------------------------------

def _read_file(root: Path, rel: str, read_content: bool) -> dict:
    p = root / rel
    if not p.exists():
        return {"exists": False}
    info: dict = {"exists": True, "is_file": p.is_file()}
    if not info["is_file"]:
        return info
    st = p.stat()
    info["size"] = st.st_size
    info["mode_exec"] = bool(st.st_mode & 0o111)
    if not read_content:
        return info
    raw = p.read_bytes()
    info["truncated"] = len(raw) > _FILE_CAP
    raw = raw[:_FILE_CAP]
    info["binary"] = b"\x00" in raw
    info["content"] = None if info["binary"] else raw.decode("utf-8", "replace")
    return info


def _list_dir(root: Path, rel: str):
    p = root / rel
    return sorted(os.listdir(p)) if p.is_dir() else None


def _walk_md(root: Path, base: str, limit: int) -> list[str]:
    out: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(root / base):
        for fn in sorted(filenames):
            if fn.endswith(".md"):
                out.append(os.path.relpath(os.path.join(dirpath, fn), root))
                if len(out) >= limit:
                    return out
    return out


def snapshot_for(template_dir: Path) -> dict:
    """A collector-shaped snapshot of a template directory."""
    skill_rels: list[str] = []
    if (template_dir / ".claude/skills").is_dir():
        skill_rels += _walk_md(template_dir, ".claude/skills", _MAX_SKILL_FILES)
    if (template_dir / ".claude/commands").is_dir():
        skill_rels += _walk_md(template_dir, ".claude/commands", _MAX_SKILL_FILES)
    return {
        "schema": 1,
        "root": str(template_dir),
        "files": {rel: _read_file(template_dir, rel, rc) for rel, rc in _FIXED_FILES},
        "dirs": {
            ".claude/commands": _list_dir(template_dir, ".claude/commands"),
            ".claude/skills": _list_dir(template_dir, ".claude/skills"),
            ".claude/agents": _list_dir(template_dir, ".claude/agents"),
            "schemas": _list_dir(template_dir, "schemas"),
        },
        "skills": {
            rel: _read_file(template_dir, rel, True)
            for rel in skill_rels[:_MAX_SKILL_FILES]
        },
        "hit_total_cap": False,
    }


def _bundled_template_dirs() -> list[Path]:
    """Every bundled template directory with a `template.yaml` — hidden ones
    included. Mirrors `get_local_templates()`'s own "no template.yaml ⇒ not a
    template" rule without re-deriving its `hidden` predicate."""
    return sorted(
        d for d in TEMPLATES_DIR.iterdir()
        if d.is_dir() and (d / "template.yaml").exists()
    )


def _assert_all_pass(results: dict, ids: list[str], label: str) -> None:
    """`run_static` maps an unknown/renamed id AND any raising check to
    "skipped", so "nothing failed" is an assertion that empties itself the day a
    check is renamed. Assert `pass` positively AND that every requested id came
    back."""
    assert set(results) == set(ids), (
        f"{label}: run_static returned {sorted(results)}, expected {sorted(ids)}"
    )
    bad = {
        cid: (status, msg)
        for cid, (status, msg, _detail) in results.items()
        if status != "pass"
    }
    assert not bad, f"{label}: checks did not pass: {bad}"


def _assert_hard_static_clean(results: dict, ids: list[str], label: str) -> None:
    """Like `_assert_all_pass`, but tolerates the pinned precondition skips in
    `_EXPECTED_SKIPS`. Any other non-pass — including "skipped" with a
    `not_implemented` (renamed id) or `check_error` (raising check) reason — is
    a failure, so the guard can never empty itself."""
    assert set(results) == set(ids), (
        f"{label}: run_static returned {sorted(results)}, expected {sorted(ids)}"
    )
    problems = {}
    for cid, (status, msg, detail) in results.items():
        if status == "pass":
            continue
        if status == "skipped":
            reason = (detail or {}).get("skip_reason")
            if _EXPECTED_SKIPS.get(cid) == reason:
                continue
            problems[cid] = f"unexpected skip ({reason}): {msg}"
        else:
            problems[cid] = f"{status}: {msg}"
    assert not problems, f"{label}: HARD static checks not clean: {problems}"


# ---------------------------------------------------------------------------
# Anti-vacuity floors — every loop below iterates something real
# ---------------------------------------------------------------------------

def test_templates_dir_exists():
    """Hard-fail, never `pytest.skip`: `backend-unit-test` gates on a base-vs-head
    diff of *failing* test IDs, so a test that skips on both sides is
    indistinguishable from one that passes. `config/agent-templates` is an
    in-repo path that exists in every real checkout."""
    assert TEMPLATES_DIR.is_dir(), f"missing in-repo path: {TEMPLATES_DIR}"
    assert len(_bundled_template_dirs()) >= 14


def test_canonical_constant_is_real():
    """A stubbed or leaked `services.git_service` would hand us a MagicMock and
    every parity loop below would iterate nothing."""
    assert isinstance(CANONICAL, tuple)
    assert len(CANONICAL) >= 40  # 46 today
    assert all(isinstance(p, str) and p for p in CANONICAL)
    assert ".env" in CANONICAL and ".mcp.json" in CANONICAL


def test_guarded_templates_exist_and_ship_a_gitignore():
    """Catches a renamed/removed template silently emptying the guard."""
    for name in GUARDED_TEMPLATES:
        d = TEMPLATES_DIR / name
        assert d.is_dir(), f"guarded template {name!r} is not on disk"
        assert (d / ".gitignore").is_file(), f"{name} ships no .gitignore"


def test_every_visible_template_is_guarded(monkeypatch):
    """A new **visible** (user-pickable) template must be added to
    `GUARDED_TEMPLATES` with its `.gitignore` — otherwise the catalog silently
    regains a template that births 4 HARD findings.

    Visibility comes from `template_service.get_local_templates()` (the
    product's own predicate), never a hand-rolled `hidden` read — #1931 may
    change the catalog-intent mechanism."""
    monkeypatch.setattr(template_service, "_local_templates_dir", lambda: TEMPLATES_DIR)
    visible = {t["id"].split(":", 1)[1] for t in template_service.get_local_templates()}
    assert visible, "template_service surfaced no templates — helper is broken"
    unguarded = sorted(visible - set(GUARDED_TEMPLATES))
    assert not unguarded, (
        "visible bundled templates missing from GUARDED_TEMPLATES: "
        f"{unguarded}. Add them to GUARDED_TEMPLATES and run: {REGEN_CMD}"
    )


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", GUARDED_TEMPLATES)
def test_guarded_template_passes_gitignore_hard_checks(name):
    """AC#3 — the four HARD `.gitignore` security checks, plus G-001."""
    results = run_static(snapshot_for(TEMPLATES_DIR / name), GITIGNORE_HARD_IDS)
    _assert_all_pass(results, GITIGNORE_HARD_IDS, f"template {name}")


@pytest.mark.parametrize("name", GUARDED_TEMPLATES)
def test_guarded_template_has_no_gitignore_findings(name):
    """The user-visible outcome: no `.gitignore`-decidable finding of any
    severity remains (F-003/S-006/S-007/S-008 soft, G-002 canonical parity)."""
    results = run_static(snapshot_for(TEMPLATES_DIR / name), GITIGNORE_ALL_IDS)
    _assert_all_pass(results, GITIGNORE_ALL_IDS, f"template {name}")


@pytest.mark.parametrize("name", GUARDED_TEMPLATES)
def test_guarded_template_passes_every_hard_static_check(name):
    """AC#3 in full — every statically-decidable HARD check except the two this
    change deliberately does not fix (`_KNOWN_FAILING_CHECKS`).

    This couples the guard to `spec.CHECKS`: a NEW HARD static check that a
    bundled template fails turns this red. That is the correct signal — Trinity
    shipping a template that fails its own HARD gate is a real finding — but it
    means a PR adding a HARD check may see it fire here."""
    ids = sorted(
        c.id for c in spec.CHECKS
        if c.severity == "hard" and c.type == "static" and c.id not in _KNOWN_FAILING_CHECKS
    )
    assert len(ids) >= 15, f"HARD static check set collapsed to {ids}"
    results = run_static(snapshot_for(TEMPLATES_DIR / name), ids)
    _assert_hard_static_clean(results, ids, f"template {name}")


def test_waiver_sets_name_real_checks():
    """Both waiver sets must reference ids the spec still declares, so a renamed
    check can never leave a dead exemption behind."""
    for cid in sorted(_KNOWN_FAILING_CHECKS | set(_EXPECTED_SKIPS)):
        assert cid in spec.BY_ID, f"waiver names unknown check {cid}"


def test_known_failing_checks_are_not_stale():
    """The check-level waiver self-retires. If every guarded template starts
    passing a waived check, the waiver is stale and must be deleted — otherwise
    it silently becomes an exemption nobody re-earns."""
    for cid in sorted(_KNOWN_FAILING_CHECKS):
        statuses = {
            name: run_static(snapshot_for(TEMPLATES_DIR / name), [cid])[cid][0]
            for name in GUARDED_TEMPLATES
        }
        assert "fail" in statuses.values(), (
            f"{cid} no longer fails any guarded template "
            f"({statuses}) — remove it from _KNOWN_FAILING_CHECKS"
        )


@pytest.mark.parametrize("name", GUARDED_TEMPLATES)
def test_guarded_gitignore_mirrors_canonical_patterns(name):
    """The anti-divergence ratchet: the shipped file is a projection of
    `_GITIGNORE_PATTERNS`, and a new canonical entry must reach every template."""
    path = TEMPLATES_DIR / name / ".gitignore"
    lines = {
        ln.strip()
        for ln in path.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    }
    missing = [p for p in CANONICAL if p not in lines]
    assert not missing, (
        f"{path} is missing {len(missing)} canonical pattern(s): {missing}. "
        f"Regenerate every guarded template with: {REGEN_CMD}"
    )


@pytest.mark.parametrize("name", GUARDED_TEMPLATES)
def test_guarded_gitignore_matches_the_regenerator(name):
    """Byte-identity with `render()`. Makes the regenerator load-bearing: a
    hand-edit, or a drifting guide block, fails here with the one command that
    fixes it."""
    path = TEMPLATES_DIR / name / ".gitignore"
    assert path.read_text(encoding="utf-8") == render(name), (
        f"{path} does not match the generated content. Run: {REGEN_CMD}"
    )


def test_no_bundled_template_excludes_claude_wholesale():
    """Universal, hidden templates included. A bare `.claude/` line would trade
    four HARD findings for one and stop the agent's own commands/skills/agents
    ever reaching Trinity — the single most likely wrong fix for this issue."""
    dirs = _bundled_template_dirs()
    assert dirs, "no bundled templates discovered"
    for d in dirs:
        results = run_static(snapshot_for(d), ["G-001"])
        _assert_all_pass(results, ["G-001"], f"template {d.name}")


def test_g001_guard_actually_fires():
    """A guard never shown to fail on a real leak is decorative. Prove G-001
    fires on the exact mistake the shipped header warns about."""
    for blanket in (".claude/", ".claude"):
        snap = {
            "schema": 1,
            "root": "/home/developer",
            "files": {
                ".gitignore": {
                    "exists": True, "is_file": True, "size": len(blanket) + 1,
                    "mode_exec": False, "binary": False, "truncated": False,
                    "content": f"# header\n{blanket}\n",
                }
            },
            "dirs": {}, "skills": {},
        }
        status, _msg, _detail = run_static(snap, ["G-001"])["G-001"]
        assert status == "fail", f"G-001 did not fire on a bare {blanket!r} line"


if __name__ == "__main__":  # pragma: no cover — the regenerator entrypoint
    if "--regenerate" in sys.argv:
        for p in regenerate():
            print(f"wrote {p.relative_to(REPO_ROOT)}")
    else:
        print(f"usage: {REGEN_CMD}")
        raise SystemExit(2)
