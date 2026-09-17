"""#2306 — architecture.md is a lean always-loaded core over on-demand area files.

The split only pays if three things stay true, and each fails silently:

1. The core stays small. It is `@`-imported by CLAUDE.md, so every line is paid
   by every session. Nothing errors when it grows back; it just costs more.
2. The map and the files stay in bijection. An area file with no map row is
   unreachable (nothing tells an agent it exists); a row with no file points at
   nothing.
3. The map has one reader and no second copy. `scripts/docs/architecture_map.py`
   parses the core's table — it carries no map of its own — so this suite proves
   the parse still agrees with the published table rather than trusting the
   comment that says so. Loading an area file is a deliberate step (#2642):
   the core must say so, and nothing may promise an ambient hook again.

Also guards link integrity across the new file boundary: the split turned ~79
in-page `(#anchor)` links into cross-file links, and a wrong one is invisible
until a human clicks it.
"""
from __future__ import annotations

import importlib.util
import re
import unicodedata
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CORE = REPO / "docs/memory/architecture.md"
AREA_DIR = REPO / "docs/memory/architecture"
MAP_MODULE = REPO / "scripts/docs/architecture_map.py"
RETIRED_HOOK = REPO / "scripts/docs/architecture_context_hook.py"
CLAUDE_MD = REPO / "CLAUDE.md"

CORE_LINE_BUDGET = 500


def _load_map_module():
    spec = importlib.util.spec_from_file_location("architecture_map", MAP_MODULE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _slug(heading: str) -> str:
    """GitHub's heading-anchor slug, closely enough for link checking."""
    s = heading.strip().lower()
    s = re.sub(r"`([^`]*)`", r"\1", s)
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)
    s = unicodedata.normalize("NFKD", s)
    s = re.sub(r"[^\w\s-]", "", s, flags=re.U)
    return s.strip().replace(" ", "-")


def _headings(path: Path) -> set[str]:
    out, fence = set(), False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("```"):
            fence = not fence
            continue
        if fence:
            continue
        m = re.match(r"^#{1,6}\s+(.*?)\s*$", line)
        if m:
            out.add(_slug(m.group(1)))
    return out


def _area_files() -> list[Path]:
    return sorted(AREA_DIR.glob("*.md"))


# ---------------------------------------------------------------- budget

def test_core_within_line_budget():
    n = len(CORE.read_text(encoding="utf-8").splitlines())
    assert n <= CORE_LINE_BUDGET, (
        f"docs/memory/architecture.md is {n} lines, over the {CORE_LINE_BUDGET}-line "
        "budget stated in its own editorial rules. It is @-imported by CLAUDE.md, so "
        "this cost is paid by every session — move the detail into the owning file "
        "under docs/memory/architecture/ and keep only the map row here (#2306)."
    )


def test_core_states_its_own_budget():
    """The budget must be visible where an author is editing, not only in this test."""
    assert f"under **{CORE_LINE_BUDGET} lines**" in CORE.read_text(encoding="utf-8")


def test_only_the_core_is_auto_imported():
    imports = re.findall(r"@(docs/memory/architecture[^\s|)]*)", CLAUDE_MD.read_text(encoding="utf-8"))
    assert imports, "CLAUDE.md no longer @-imports the architecture core"
    for target in imports:
        assert target == "docs/memory/architecture.md", (
            f"CLAUDE.md @-imports {target!r}. Area files are referenced by path and never "
            "imported — importing one puts it back in every session's context (#2306)."
        )


# ---------------------------------------------------------------- map <-> disk

def test_map_and_area_files_are_in_bijection():
    mapped = {name for name, _, _ in _load_map_module().parse_map(CORE)}
    on_disk = {p.name for p in _area_files()}
    assert mapped == on_disk, (
        f"map rows without a file: {sorted(mapped - on_disk)}; "
        f"files with no map row: {sorted(on_disk - mapped)}. "
        "An unmapped area file is unreachable — nothing tells a session it exists."
    )


def test_every_map_row_carries_owned_paths_and_a_consequence():
    for name, globs, why in _load_map_module().parse_map(CORE):
        assert globs, f"{name}: no owned code paths — nothing can ever resolve to it"
        for g in globs:
            root = g.split("*")[0].rstrip("/")
            assert (REPO / root).exists(), f"{name}: owned path {g!r} does not exist in the repo"
        # A topic label gives an agent no reason to spend the tool call; the row
        # must name the regression that reading the file prevents (#2306).
        assert len(why) >= 120, f"{name}: consequence too thin to motivate a read: {why!r}"
        assert re.search(r"#\d{2,4}|ent#\d{1,4}|Invariant #\d+", why), (
            f"{name}: consequence cites no issue or invariant — it reads as an opinion "
            f"rather than a recorded regression: {why!r}"
        )


def test_area_files_carry_the_standard_header():
    for p in _area_files():
        head = p.read_text(encoding="utf-8")[:1600]
        assert "[architecture.md](../architecture.md)" in head, f"{p.name}: no backlink to the core"
        assert "**Owns**:" in head, f"{p.name}: header does not state what it owns"
        assert "**Read this before changing the paths above**:" in head, (
            f"{p.name}: header does not state the consequence"
        )
        assert "**Write path**:" in head, f"{p.name}: header does not state the write path"


# ---------------------------------------------------------------- anti-drift

def test_map_module_has_no_map_of_its_own():
    """The table in the core is the only source. A second copy is a second drift.

    Checked over the AST rather than the raw text, so prose is exempt by
    construction: comments never reach the AST, and docstrings are skipped
    explicitly. An area filename appearing in *executable* string data is a
    routing decision the module made for itself.
    """
    import ast

    tree = ast.parse(MAP_MODULE.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None:
                docstrings.add(doc)
    literals = [
        n.value for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value not in docstrings
    ]
    areas = {p.name for p in _area_files()}
    for lit in literals:
        for area in areas:
            assert area not in lit, (
                f"{area} is hardcoded in scripts/docs/architecture_map.py (string literal "
                f"{lit!r}). The module must derive its map from the core's Architecture Map "
                "table so the two cannot disagree (#2306)."
            )


def test_parse_matches_the_published_table():
    """Parse the markdown table independently and require the module to agree."""
    text = CORE.read_text(encoding="utf-8")
    section = text[text.index("## Architecture Map"):]
    nxt = section.find("\n## ", 1)
    if nxt != -1:
        section = section[:nxt]
    rows = re.findall(r"^\|\s*\[`([^`]+\.md)`\]", section, re.M)
    assert rows, "no rows found in the Architecture Map table"
    assert [n for n, _, _ in _load_map_module().parse_map(CORE)] == rows


@pytest.mark.parametrize("rel,expected", [
    ("src/backend/services/cleanup_service.py", "reliability.md"),
    ("src/backend/dependencies.py", "security.md"),
    ("src/backend/db/schema.py", "database.md"),
    ("docker/base-image/agent_server/state.py", "agent-runtime.md"),
    ("src/backend/client_portal/service.py", "workspace.md"),
    # falls through to the catalog: owned only by the broad services glob
    ("src/backend/services/settings_service.py", "backend.md"),
])
def test_owner_resolution_prefers_the_most_specific_row(rel, expected):
    """cleanup_service.py is matched by backend.md's broad services glob too.

    A resolver that named both would point at the catalog on every service
    edit; a skill acting on that would read the wrong file first.
    """
    owners = _load_map_module().owner_for(rel, CORE)
    assert [f for f, _ in owners] == [expected], f"{rel} resolved to {owners}"


def test_owner_resolution_is_empty_for_unowned_paths():
    assert _load_map_module().owner_for("README.md", CORE) == []


# ---------------------------------------------------------------- deliberate loading (#2642)

def test_core_states_the_deliberate_read_rule():
    """Area files are read as an explicit step. The core must say so where a
    reader is looking, and must not promise an ambient hook that was never
    wired anywhere (#2642)."""
    text = CORE.read_text(encoding="utf-8")
    section = text[text.index("## Architecture Map"):]
    assert "deliberate step" in section, "the Architecture Map no longer states the deliberate-read rule"
    for stale in ("PreToolUse", "architecture_context_hook", "Hook setup", "#hook-setup"):
        assert stale not in text, f"core still refers to the retired hook: {stale!r}"


def test_no_surface_promises_the_retired_hook():
    assert not RETIRED_HOOK.exists(), "scripts/docs/architecture_context_hook.py is back (#2642 deleted it)"
    claude_md = CLAUDE_MD.read_text(encoding="utf-8")
    assert "PreToolUse` hook will point you at it" not in claude_md
    assert "architecture_context_hook" not in claude_md


# ---------------------------------------------------------------- link integrity

def test_no_dangling_anchor_links_across_the_split():
    files = [CORE] + _area_files()
    headings = {p: _headings(p) for p in files}
    by_name = {p.name: p for p in files}
    dangling = []
    for p in files:
        for m in re.finditer(r"\]\(([^)]*?)#([A-Za-z0-9_-]+)\)", p.read_text(encoding="utf-8")):
            target, anchor = m.group(1), m.group(2)
            if target == "":
                owner = p
            elif target == "../architecture.md":
                owner = CORE
            elif target.startswith("architecture/"):
                owner = by_name.get(target.split("/", 1)[1])
            elif target in by_name:
                owner = by_name[target]
            else:
                continue  # link out of the architecture set; not this test's business
            if owner is None or anchor not in headings[owner]:
                dangling.append(f"{p.name}: ]({target}#{anchor})")
    assert not dangling, "anchor links with no target heading:\n  " + "\n  ".join(dangling)
