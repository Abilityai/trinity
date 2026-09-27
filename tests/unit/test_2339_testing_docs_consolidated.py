"""#2339 (Rail R5) — `docs/testing/` is one strategy plus pointers, and stays so.

Before #2339 testing guidance lived in six places and the directory held 17
top-level files plus a 30-file scenario tree, most of it dated. The issue's own
rule — "three files, or it fragments again within a quarter" — is not kept by a
document; nothing in a document can notice a fourth file. This guard is what
keeps it, and it pins the strategy's structural CLAIMS to the code they
describe, because every testing doc in this repo that stayed true is one a
test reads (the catalog, the generated JOURNEYS.md, the placement lint).

What it asserts:

1. The git-tracked top level of `docs/testing/` is exactly the live set, and
   its only subdirectories are `phases/` (the click-through scenarios
   `/ui-sweep` runs) and `ui-sweep/` (its dated reports and cursor).
2. STRATEGY.md carries the required sections.
3. Every relative link in the live set and in the four entry points resolves,
   anchors included — there is no link checker in CI.
4. STRATEGY.md links its pointer targets, and the entry points link STRATEGY.md
   (a doc nothing routes to is never loaded, #2642).
5. "Linked, not copied": no run of prose lines is shared with either README or
   the catalog.
6. STRATEGY.md defines no invariant and cites only ids that resolve — the
   catalog stays the single definition site (#2337).
7. Every archived file is indexed in `docs/archive/README.md`.
8. No live tracked file still references a retired `docs/testing/` path, in
   either link form (`docs/testing/X`, `../testing/X`).
9. STRATEGY.md names every runner tier `tests/run-full.sh` declares and every
   workflow that runs tests, and every workflow it names exists.

Design notes, each the residue of a reviewed trap:
* Directory contents come from `git ls-files` (cwd = repo root, `check=True`,
  empty output is a failure) — the filesystem carries `.DS_Store` and
  `__pycache__`, and CI runs `pytest` from `tests/`, where a bare listing would
  be silently empty.
* The reference scan reads BYTES over tracked files only: the tree carries
  PNGs and two gitlinks, so `read_text()` raises on the first image and
  `IsADirectoryError` on `.claude`. Retired names are DERIVED from the archive
  directory, never hand-listed (a hand-written list validates the fix, not the
  codebase). The guard excludes itself so its own needles cannot match.
* Invariant-id checks are restricted to families the catalog defines: the bare
  id regex also matches `SHA-256` and `ISO-8601`.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _invariant_catalog as inv  # noqa: E402
import _markdown_links as md  # noqa: E402

DOCS_TESTING = REPO / "docs/testing"
STRATEGY = DOCS_TESTING / "STRATEGY.md"
CATALOG = DOCS_TESTING / "orchestration-invariant-catalog.md"
JOURNEYS = DOCS_TESTING / "JOURNEYS.md"
ARCHIVE = REPO / "docs/archive/testing"
ARCHIVE_INDEX = REPO / "docs/archive/README.md"
RUN_FULL = REPO / "tests/run-full.sh"
WORKFLOWS = REPO / ".github/workflows"
TESTS_README = REPO / "tests/README.md"
E2E_README = REPO / "src/frontend/e2e/README.md"
AGENTS_MD = REPO / "AGENTS.md"
README_MD = REPO / "README.md"

LIVE_FILES = {"STRATEGY.md", "orchestration-invariant-catalog.md", "JOURNEYS.md"}
LIVE_DIRS = {"phases", "ui-sweep"}
LIVE_SET = [STRATEGY, CATALOG, JOURNEYS]
ENTRY_POINTS = [AGENTS_MD, README_MD, TESTS_README, E2E_README]
REQUIRED_SECTIONS = (
    "method",
    "lanes",
    "acceptance-bar-for-a-harness",
    "where-a-new-testing-document-goes",
)
REQUIRED_TARGETS = {
    CATALOG,
    JOURNEYS,
    TESTS_README,
    E2E_README,
    REPO / "tests/journeys/catalog.yaml",
    ARCHIVE_INDEX,
}
# Files that moved OUT of docs/testing to somewhere other than the archive, and
# the one file that moved INTO the archive from the docs root. The archive
# listing derives every other retired name.
RELOCATED_FROM_TESTING = {"PULL_MIGRATION_TESTING.md"}
RETIRED_ROOT_PATHS = {b"docs/TESTING_GUIDE.md"}
SCAN_EXCLUDE_PREFIXES = ("docs/archive/", "docs/releases/", "docs/security-reports/")
SCAN_EXCLUDE_FILES = {
    "docs/memory/learnings.md",  # a dated, append-only ledger quotes old paths on purpose
    str(Path(__file__).resolve().relative_to(REPO)),
}
BINARY_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg", ".pdf", ".woff",
    ".woff2", ".ttf", ".otf", ".zip", ".gz", ".tgz", ".db", ".sqlite", ".pyc",
    ".wasm", ".mp3", ".mp4", ".wav", ".lock",
}
TEST_RUNNER_RE = re.compile(rb"pytest|npm run test|playwright|run-full\.sh")
WORKFLOWS_THAT_RUN_NO_TESTS = {
    # Names the check contexts it waits on; executes nothing itself.
    "dependabot-auto-merge.yml",
}
SHINGLE = 4


def _git_ls_files(*pathspecs: str) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "--", *pathspecs],
        cwd=REPO, capture_output=True, text=True, check=True,
    ).stdout
    return [line for line in out.splitlines() if line]


def _top_level(listing: list[str], prefix: str) -> tuple[set[str], set[str]]:
    files, dirs = set(), set()
    for path in listing:
        rel = path[len(prefix):]
        if "/" in rel:
            dirs.add(rel.split("/", 1)[0])
        else:
            files.add(rel)
    return files, dirs


# --------------------------------------------------------------------------- 1

def test_docs_testing_top_level_is_exactly_the_live_set():
    listing = _git_ls_files("docs/testing")
    assert listing, "git ls-files -- docs/testing returned nothing; run from a checkout"
    files, dirs = _top_level(listing, "docs/testing/")
    assert files == LIVE_FILES, (
        f"docs/testing/ top level must be exactly {sorted(LIVE_FILES)}; "
        f"unexpected: {sorted(files - LIVE_FILES)}, missing: {sorted(LIVE_FILES - files)}. "
        "Guidance goes in STRATEGY.md; a feature's test record goes beside that "
        "feature's docs; anything dated goes to docs/archive/testing/ with an index row."
    )
    assert dirs <= LIVE_DIRS, (
        f"docs/testing/ may hold only the subdirectories {sorted(LIVE_DIRS)}; "
        f"unexpected: {sorted(dirs - LIVE_DIRS)}"
    )
    assert "phases" in dirs, "docs/testing/phases/ is live (the /ui-sweep scenarios) and must stay"


# --------------------------------------------------------------------------- 2

def test_strategy_carries_the_required_sections():
    slugs = md.headings(STRATEGY)
    missing = [s for s in REQUIRED_SECTIONS if not any(h.startswith(s) for h in slugs)]
    assert not missing, f"STRATEGY.md lacks section(s) {missing}; headings found: {slugs}"


# --------------------------------------------------------------------------- 3

@pytest.mark.parametrize("path", LIVE_SET + [TESTS_README, E2E_README],
                         ids=lambda p: str(p.relative_to(REPO)))
def test_every_relative_link_resolves(path: Path):
    broken = []
    for link in md.relative_links(path):
        target = md.resolve(path, link)
        if not target.exists():
            broken.append(f"line {link.line}: {link.target!r} -> {target.relative_to(REPO) if target.is_relative_to(REPO) else target} does not exist")
        elif link.anchor and not md.anchor_exists(target, link.anchor):
            broken.append(f"line {link.line}: {link.target or path.name}#{link.anchor} names no heading")
    assert not broken, f"{path.relative_to(REPO)} has broken links:\n  " + "\n  ".join(broken)


# --------------------------------------------------------------------------- 4

def _resolved_targets(path: Path) -> set[Path]:
    return {md.resolve(path, link) for link in md.relative_links(path)}


def test_strategy_links_its_pointer_targets():
    targets = _resolved_targets(STRATEGY)
    missing = sorted(str(t.relative_to(REPO)) for t in REQUIRED_TARGETS - targets)
    assert not missing, f"STRATEGY.md must link (not restate) {missing}"


@pytest.mark.parametrize("path", ENTRY_POINTS, ids=lambda p: str(p.relative_to(REPO)))
def test_entry_points_route_to_strategy(path: Path):
    assert STRATEGY.resolve() in _resolved_targets(path), (
        f"{path.relative_to(REPO)} has no link to docs/testing/STRATEGY.md — a strategy "
        "nothing routes to is never loaded (#2642)"
    )


# --------------------------------------------------------------------------- 5

@pytest.mark.parametrize("corpus", [TESTS_README, E2E_README, CATALOG],
                         ids=lambda p: str(p.relative_to(REPO)))
def test_strategy_copies_nothing_from(corpus: Path):
    mine = md.shingles(md.normalised_prose(STRATEGY), SHINGLE)
    theirs = md.shingles(md.normalised_prose(corpus), SHINGLE)
    shared = mine & theirs
    assert not shared, (
        f"STRATEGY.md repeats {len(shared)} run(s) of {SHINGLE} lines from "
        f"{corpus.relative_to(REPO)} — link it instead. First run:\n  "
        + "\n  ".join(next(iter(shared)))
    )


# --------------------------------------------------------------------------- 6

def test_strategy_defines_no_invariant_and_cites_only_resolvable_ids():
    text = STRATEGY.read_text(encoding="utf-8")
    defined_here = re.findall(rf"(?m)^\s*\*\*({inv.ID})\*\*", text)
    assert not defined_here, (
        f"STRATEGY.md starts a line with a bold invariant id {defined_here} — that is the "
        "definition shape; definitions live only in the catalog (#2337)"
    )
    catalog_ids = set(inv.catalog_ids(inv.read(CATALOG)))
    families = {inv.family(i) for i in catalog_ids}
    cited = {tok for tok in re.findall(inv.ID, text) if inv.family(tok) in families}
    unresolved = sorted(cited - catalog_ids)
    assert not unresolved, f"STRATEGY.md cites invariant id(s) that do not resolve: {unresolved}"
    assert cited, "STRATEGY.md cites no invariant id at all — the method section should name examples"


# --------------------------------------------------------------------------- 7

def test_every_archived_file_is_indexed():
    listing = _git_ls_files("docs/archive/testing")
    assert listing, "docs/archive/testing/ is empty — the archive moved?"
    files, dirs = _top_level(listing, "docs/archive/testing/")
    index = ARCHIVE_INDEX.read_text(encoding="utf-8")
    missing = sorted(n for n in files | dirs if n not in index)
    assert not missing, (
        f"docs/archive/README.md does not index {missing}; every archived testing "
        "file gets a row saying what it was and what superseded it"
    )


# --------------------------------------------------------------------------- 8

def _retired_names() -> set[str]:
    files, dirs = _top_level(_git_ls_files("docs/archive/testing"), "docs/archive/testing/")
    return files | {d + "/" for d in dirs} | RELOCATED_FROM_TESTING


def _retired_needle() -> re.Pattern[bytes]:
    names = sorted(_retired_names())
    assert names, "no retired names derived — the archive listing is empty"
    alternation = b"|".join(re.escape(n.encode()) for n in names)
    return re.compile(
        rb"(?<!archive/)testing/(?:" + alternation + rb")"
        + rb"|" + rb"|".join(re.escape(p) for p in RETIRED_ROOT_PATHS)
    )


def test_retired_needle_matches_both_link_forms_and_spares_the_new_homes():
    needle = _retired_needle()
    assert needle.search(b"see docs/testing/UI_INTEGRATION_TEST.md")
    assert needle.search(b"see ../testing/QUICK_START.txt")
    assert needle.search(b"see testing/TESTING_GUIDE.md")
    assert needle.search(b"the old docs/TESTING_GUIDE.md")
    assert needle.search(b"docs/testing/PULL_MIGRATION_TESTING.md")
    assert not needle.search(b"docs/archive/testing/UI_INTEGRATION_TEST.md")
    assert not needle.search(b"docs/planning/PULL_MIGRATION_TESTING.md")
    assert not needle.search(b"docs/testing/STRATEGY.md docs/testing/phases/INDEX.md")


def test_no_live_file_references_a_retired_testing_path():
    needle = _retired_needle()
    hits = []
    for rel in _git_ls_files():
        if rel.startswith(SCAN_EXCLUDE_PREFIXES) or rel in SCAN_EXCLUDE_FILES:
            continue
        p = REPO / rel
        if p.suffix.lower() in BINARY_SUFFIXES or not p.is_file():
            continue
        data = p.read_bytes()
        for m in needle.finditer(data):
            line_no = data.count(b"\n", 0, m.start()) + 1
            hits.append(f"{rel}:{line_no}: {m.group(0).decode(errors='replace')}")
    assert not hits, (
        "live files still reference retired docs/testing paths (repoint to the archive, "
        "docs/planning, or STRATEGY.md):\n  " + "\n  ".join(hits)
    )


# --------------------------------------------------------------------------- 9

def _tier_dirs() -> list[str]:
    m = re.search(r"(?m)^TIER_DIRS=\(([^)]*)\)", RUN_FULL.read_text(encoding="utf-8"))
    assert m, "tests/run-full.sh no longer declares TIER_DIRS=(...) — update this guard's parser"
    tiers = m.group(1).split()
    assert tiers, "TIER_DIRS is empty"
    return tiers


def test_strategy_names_every_runner_tier():
    text = STRATEGY.read_text(encoding="utf-8")
    missing = [t for t in _tier_dirs() if not re.search(rf"(?<![\w-]){re.escape(t)}(?![\w-])", text)]
    assert not missing, (
        f"tests/run-full.sh declares runner tier(s) {missing} that STRATEGY.md never names — "
        "add them to the glossary and the lanes section"
    )


def test_strategy_names_every_test_running_workflow_and_only_real_ones():
    text = STRATEGY.read_text(encoding="utf-8")
    on_disk = {p.name for p in WORKFLOWS.glob("*.yml")}
    assert on_disk, ".github/workflows/ has no *.yml"
    runs_tests = {
        p.name for p in WORKFLOWS.glob("*.yml")
        if TEST_RUNNER_RE.search(p.read_bytes()) and p.name not in WORKFLOWS_THAT_RUN_NO_TESTS
    }
    unnamed = sorted(w for w in runs_tests if w not in text)
    assert not unnamed, (
        f"workflow(s) {unnamed} run tests but STRATEGY.md's lanes table never names them; "
        "add a row, or list the file in WORKFLOWS_THAT_RUN_NO_TESTS with its reason"
    )
    named = set(re.findall(r"`([A-Za-z0-9_.-]+\.yml)`", text))
    phantom = sorted(named - on_disk)
    assert not phantom, f"STRATEGY.md names workflow(s) that do not exist: {phantom}"


# ------------------------------------------------------- helper self-tests
# The checks above are only as good as the markdown helper under them; these
# pin the behaviours they lean on against synthetic input.

def test_markdown_helper_skips_fences_and_absolute_links(tmp_path):
    (tmp_path / "real.md").write_text("# Real Heading\n", encoding="utf-8")
    doc = tmp_path / "doc.md"
    doc.write_text(
        "# Title\n"
        "See [real](real.md#real-heading), [web](https://example.com), [top](#title).\n"
        "```md\n"
        "## Not A Heading\n"
        "[fenced](missing.md)\n"
        "```\n"
        "[gone](missing.md)\n",
        encoding="utf-8",
    )
    assert md.headings(doc) == ["title"]
    links = md.relative_links(doc)
    assert [(l.target, l.anchor) for l in links] == [
        ("real.md", "real-heading"), ("", "title"), ("missing.md", ""),
    ]
    real = md.resolve(doc, links[0])
    assert real.exists() and md.anchor_exists(real, "real-heading")
    assert not md.anchor_exists(real, "no-such-heading")
    assert md.resolve(doc, links[1]) == doc.resolve()
    assert not md.resolve(doc, links[2]).exists()


def test_slug_matches_github_for_the_headings_this_guard_relies_on():
    assert md.slug("Where does a new test go? (#1895)") == "where-does-a-new-test-go-1895"
    assert md.slug("Acceptance bar for a harness") == "acceptance-bar-for-a-harness"
    assert md.slug("`run-full.sh` — the honest full run (#2080)") == "run-fullsh--the-honest-full-run-2080"


def test_normalised_prose_keeps_only_lines_that_can_carry_copied_text(tmp_path):
    doc = tmp_path / "doc.md"
    doc.write_text(
        "short\n"
        "\n"
        "|---|---|\n"
        "| a | b |\n"
        "```\n"
        "this fenced line is long enough to count but it is code\n"
        "```\n"
        "   this prose line is long enough to count   \n",
        encoding="utf-8",
    )
    assert md.normalised_prose(doc) == ["this prose line is long enough to count"]
    assert md.shingles(["a", "b", "c"], 2) == {("a", "b"), ("b", "c")}
    assert md.shingles(["a"], 2) == set()
