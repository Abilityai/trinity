"""#2338 — the journey catalog is valid, and carries no status (Rail R4).

The catalog is the durable record of what Trinity promises. Two of its rules are
the kind that decay into comments unless something fails when they are broken,
so they are asserted here:

* **No status in git.** A `last_green` or `green|red` field either rots (a human
  owns it) or writes a commit on every CI run (a bot owns it) — history spam and
  merge conflicts on a file everybody edits. Git holds intent, CI holds state.
* **One invariant namespace.** Records REFERENCE ids in
  `docs/testing/orchestration-invariant-catalog.md`; they never restate an
  invariant, and they never invent an id that does not resolve.

`built: false` is legal and expected — declaring ten promises before any harness
exists is the point. What is not legal is a promise with no issue, a lane the
catalog never defined, or an invariant id that resolves nowhere.
"""
import re
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
CATALOG = REPO / "tests/journeys/catalog.yaml"
INVARIANTS = REPO / "docs/testing/orchestration-invariant-catalog.md"
REGISTRY = REPO / "tests/registry.json"

# Anything that smells like a run RESULT. Matched against every key at every
# depth, so a nested `status:` cannot slip in under a new parent.
# Status vocabulary, matched by TOKEN rather than by substring.
#
# This was one unanchored alternation applied with `.search()` to every key at
# every depth, and `red` is a substring of **`credential`** and of
# **`required`** — so the moment any record gained a `credentials:` key, or
# anyone added `required:` to the schema, this rule would have failed accusing
# the author of committing run status. J06 is literally *"I can give an agent a
# credential…"*, so that key was coming. `green` had the same shape
# (`evergreen`, `greenfield`) and `result` matched `results`/`resultant`. A rule
# whose whole value is that it fails CLEARLY cannot afford a false positive that
# lands on someone who did nothing wrong.
#
# Word boundaries alone are not enough either: `_` is a word character, so
# `\bstatus\b` does NOT match `journey_status` — the rule would have gone quiet
# on the exact keys it exists to catch. Splitting the key into tokens catches
# `journey_status` and `last_green` while leaving `credential` alone.
_STATUS_TOKENS = frozenset({
    "green", "red", "passing", "failing", "status",
    "result", "results", "coverage", "flaky",
})
# Multi-token names whose parts are individually innocent (`last`, `run`, `rate`).
_STATUS_KEYS = frozenset({
    "last_green", "last_run", "last_seen", "pass_rate", "passrate",
})


def _is_status_key(key: str) -> bool:
    """True when a key names RUN STATE rather than intent."""
    norm = str(key).strip().lower()
    if norm in _STATUS_KEYS:
        return True
    tokens = {t for t in re.split(r"[^a-z0-9]+", norm) if t}
    return bool(tokens & _STATUS_TOKENS)

REQUIRED_FIELDS = {"id", "issue", "promise", "actor", "lanes", "tier",
                   "harness", "built", "owner", "invariants"}
TIERS = {"journey-smoke", "live-stack", "soak"}


@pytest.fixture(scope="module")
def catalog():
    return yaml.safe_load(CATALOG.read_text())


def _walk_keys(node, path=""):
    if isinstance(node, dict):
        for k, v in node.items():
            yield f"{path}.{k}".lstrip("."), k
            yield from _walk_keys(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk_keys(v, f"{path}[{i}]")


@pytest.mark.parametrize("key", [
    # The two that actually broke it: `red` is inside both.
    "credentials", "credential", "required",
    # Same shape, different word.
    "evergreen", "greenfield", "resultant", "predicate", "hundred",
    # Ordinary catalog vocabulary that must never trip the rule.
    "id", "issue", "promise", "actor", "lanes", "tier", "harness",
    "built", "owner", "invariants", "test_files",
])
def test_ordinary_keys_are_not_read_as_status(key):
    """The rule's value is that it fails clearly, so a false positive is worse
    than most true ones — it lands on an author who did nothing wrong, on a
    schema addition the catalog explicitly anticipates (J06 is about giving an
    agent a credential)."""
    assert not _is_status_key(key)


@pytest.mark.parametrize("key", [
    "status", "green", "red", "passing", "failing", "coverage", "flaky",
    "result", "results",
    "last_green", "last_run", "last_seen", "pass_rate",
    # Compound forms a `\b`-anchored regex would have missed, because `_` is a
    # word character: `\bstatus\b` does not match `journey_status`.
    "journey_status", "run_status", "last_result", "ci_coverage",
])
def test_status_keys_are_still_caught(key):
    assert _is_status_key(key)


def test_no_status_field_is_committed(catalog):
    """AC #3. The whole reason coverage is computed at report time."""
    offenders = [p for p, k in _walk_keys(catalog) if _is_status_key(k)]
    assert not offenders, (
        f"status-shaped keys committed to the catalog: {offenders}. Git holds "
        f"INTENT; CI holds STATE. Coverage is computed at report time by "
        f"scripts/ci/generate_journeys_md.py from CI artifacts — a status field "
        f"here either rots or spams history."
    )


def test_all_ten_journeys_are_declared(catalog):
    ids = [j["id"] for j in catalog["journeys"]]
    assert ids == [f"J{n:02d}" for n in range(1, 11)], (
        f"expected J01..J10 in order, got {ids}"
    )


def test_every_record_carries_the_required_fields(catalog):
    for j in catalog["journeys"]:
        missing = REQUIRED_FIELDS - set(j)
        assert not missing, f"{j.get('id')} is missing {sorted(missing)}"
        assert j["promise"].strip(), f"{j['id']} has an empty promise"
        assert j["tier"] in TIERS, f"{j['id']} has tier {j['tier']!r}"
        assert isinstance(j["built"], bool), f"{j['id']}.built must be a bool"


def test_promises_are_written_in_the_actors_words(catalog):
    """AC #2. The catalog is what the PM layer reads; a promise phrased as a
    component name ("the dispatch path handles…") is a harness description, not
    a promise. First person is the cheap, checkable proxy."""
    for j in catalog["journeys"]:
        assert j["promise"].startswith(("I ", "My ", "A ")), (
            f"{j['id']}: {j['promise']!r} does not read as something a person "
            f"would say about their own use of Trinity"
        )


def test_every_lane_is_one_the_catalog_defined(catalog):
    known = set(catalog["lanes"])
    for j in catalog["journeys"]:
        unknown = set(j["lanes"]) - known
        assert not unknown, f"{j['id']} names undefined lane(s) {sorted(unknown)}"


def test_every_invariant_id_resolves(catalog):
    """AC #7, and the one-namespace rule. An id that resolves nowhere is worse
    than an empty list: it reads as coverage that does not exist."""
    text = INVARIANTS.read_text()
    declared = set(re.findall(r"^\|\s*([A-Z]+-[0-9]+)\s*\|", text, re.MULTILINE))
    assert declared, "no invariant ids parsed — the catalog format changed"
    for j in catalog["journeys"]:
        unresolved = [i for i in j["invariants"] if i not in declared]
        assert not unresolved, (
            f"{j['id']} references {unresolved}, which resolve nowhere in "
            f"{INVARIANTS.name}. Reference an existing id or leave the list "
            f"EMPTY — #2337 (Rail R3) is what adds journey-level invariants. "
            f"An invented id is coverage theatre."
        )


def test_a_built_journey_names_a_harness_that_exists(catalog):
    for j in catalog["journeys"]:
        if j["built"]:
            assert j["harness"], f"{j['id']} is built but names no harness"
            assert (REPO / j["harness"]).exists(), (
                f"{j['id']} names harness {j['harness']}, which does not exist"
            )


def test_a_named_harness_exists_even_when_not_yet_built(catalog):
    """A path may be recorded before the promise is fully covered (J03), but a
    path that points nowhere is a lie in either state."""
    for j in catalog["journeys"]:
        if j["harness"]:
            assert (REPO / j["harness"]).exists(), (
                f"{j['id']} names harness {j['harness']}, which does not exist"
            )


def test_the_catalog_links_to_the_registry_rather_than_merging_with_it():
    """AC #6. The registry indexes TESTS; the catalog indexes PROMISES. Merging
    them would make every new test file a catalog edit and bury ten promises in
    a 240 KB index."""
    assert REGISTRY.exists(), "tests/registry.json is the index this links to"
    body = CATALOG.read_text()
    assert "registry.json" not in body, (
        "the catalog must not inline the registry — the generator joins them at "
        "report time"
    )


# --------------------------------------------------------------------------
# The generated JOURNEYS.md
# --------------------------------------------------------------------------

JOURNEYS_MD = REPO / "docs/testing/JOURNEYS.md"


def test_journeys_md_is_generated_from_the_catalog_and_not_stale():
    """AC #5. Hand-editing it, or editing the catalog without regenerating,
    both produce a document that disagrees with the source of truth — and the
    disagreement is invisible until someone acts on the wrong one."""
    import subprocess
    result = subprocess.run(
        [sys.executable, str(REPO / "scripts/ci/generate_journeys_md.py"),
         "--out", str(JOURNEYS_MD), "--check"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        f"docs/testing/JOURNEYS.md is stale or hand-edited:\n"
        f"{result.stderr.strip()}"
    )


def test_the_generated_doc_carries_no_run_status():
    """The committed form is INTENT. Status arrives only when the generator is
    pointed at CI artifacts with --junit, which CI does into its own summary."""
    body = JOURNEYS_MD.read_text()
    for word in ("last_green", "green (", "· green", "Coverage at report time"):
        assert word not in body, (
            f"the committed JOURNEYS.md contains {word!r} — status must not be "
            f"committed; regenerate without --junit"
        )


# ---------------------------------------------------------------------------
# The generator's JOIN functions (review findings 2-4)
#
# These were unreachable by the PR's own verification, and the review said why:
# the generator was checked against a real downloaded artifact set, but that set
# contains no journey harness at all — so the run exercised the PARSE path and
# never the MATCH path. The verification was real; it could not reach the code
# the findings were in. Every case below is a match-path case, and each fails
# against the shape that shipped.
# ---------------------------------------------------------------------------

def _gen():
    sys.path.insert(0, str(REPO / "scripts/ci"))
    import generate_journeys_md as mod
    return mod


def test_the_registry_join_matches_a_real_registry_entry():
    """Finding 2. `test_files` is a list of DICTS and its paths are
    tests/-relative, while catalog harnesses are repo-relative — so `str(f)` was
    a dict repr and the join could never match. AC #6 is 'link to the registry',
    and the link was inert: the doc would have carried 'Harnesses not present in
    tests/registry.json' for every journey, permanently and wrongly. Invisible
    only because all ten harnesses are still null."""
    import json
    mod = _gen()
    registry = json.loads((REPO / "tests/registry.json").read_text())
    sample = registry["test_files"][0]["file"]          # e.g. unit/test_x.py
    assert mod.harness_is_indexed(f"tests/{sample}") is True
    assert mod.harness_is_indexed(sample) is True       # either spelling
    assert mod.harness_is_indexed("tests/journeys/test_does_not_exist.py") is False


def test_junit_paths_join_against_repo_relative_harnesses(tmp_path):
    """Finding 3. JUnit `file` attributes are rootdir-relative
    (`journeys/test_x.py`); the catalog is repo-relative
    (`tests/journeys/test_x.py`). Compared raw, every journey reads 'no
    evidence' forever."""
    mod = _gen()
    (tmp_path / "r.xml").write_text(
        '<testsuites><testsuite name="p">'
        '<testcase file="journeys/test_a.py" name="t"/>'
        '</testsuite></testsuites>'
    )
    greens = mod.green_harnesses(str(tmp_path))
    assert mod._norm_test_path("tests/journeys/test_a.py") in greens


def test_a_harness_whose_every_case_skipped_is_not_green(tmp_path):
    """Finding 4. `skipped` is neither `failure` nor `error`, so an all-skipped
    file was recorded green — and J03's harness is precisely the
    credential-bound one that skips on every PR-triggered run, so the first
    journey to carry a path would also have been the first misreported. The
    docstring already said 'absence of evidence is not green'; this is the shape
    it was silent about."""
    mod = _gen()
    (tmp_path / "r.xml").write_text(
        '<testsuites><testsuite name="p">'
        '<testcase file="journeys/test_skipped.py" name="s"><skipped/></testcase>'
        '<testcase file="journeys/test_mixed.py" name="s"><skipped/></testcase>'
        '<testcase file="journeys/test_mixed.py" name="p"/>'
        '<testcase file="journeys/test_failed.py" name="f"><failure/></testcase>'
        '</testsuite></testsuites>'
    )
    greens = mod.green_harnesses(str(tmp_path))
    assert "journeys/test_skipped.py" not in greens, "all-skipped is not evidence of green"
    assert "journeys/test_mixed.py" in greens, "one real pass and no failure is green"
    assert "journeys/test_failed.py" not in greens
