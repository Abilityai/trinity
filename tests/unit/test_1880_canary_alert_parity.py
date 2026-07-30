"""Regression guard: every canary invariant must be renderable as a Slack alert.

Bug (#1880): `services/canary_alerts.py` carries FOUR per-invariant surfaces —
`_INVARIANT_NAMES`, `_INVARIANT_RUNBOOKS`, and the id-branch chains inside
`_render_message` and `_render_forensic`. Five registry invariants (E-03, E-04,
E-06, G-03, G-04) shipped with an entry in none of them, so a green→red alert
degraded to a fallback that printed the bare id twice, a count, and nothing
else. G-04 is `critical` and is the credential-leak check.

Root cause is a mechanism gap, not diligence: Phase 2/3 (#882) added all four
surfaces for its invariants, Phase 4 (#1497) and E-06 (#1472) added none. The
registry and the alert surfaces are hand-maintained lists with nothing tying
them together — the `learnings.md` 2026-07-16 class ("a mirrored constant is a
copy with no owner; *'mirrors X'* in a comment is a hope, not a mechanism").

Lives under tests/unit/ so the CI unit job (`cd tests && pytest unit/`)
collects it — a guard must run where the bug would regress. The rest of the
canary suite is at `tests/test_canary_invariants.py`, which no CI workflow
executes; a guard placed beside it would never have gone red.

Structure, and why:

* The PARITY tests read source via `ast` and import NOTHING. Two reasons.
  (1) `services/__init__.py` eagerly imports `docker_service`, so
  `from services.canary_alerts import …` drags the Docker SDK and pydantic.
  (2) `services` is a named stub-leak target under pytest-randomly
  (`learnings.md` 2026-07-05) — an import-based guard can bind to a sibling
  test's stub and pass vacuously. `learnings.md` 2026-07-16 lesson (1) is
  explicit: read the source, don't import it.
* The BEHAVIOURAL tests must call the renderers, so they import — but lazily,
  inside each test body, so an import failure degrades those tests only and
  never takes the parity gate down with it.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, Set

import pytest

# tests/unit/<this file> → parent=tests/unit, .parent=tests, .parent=repo root
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_ALERTS_SRC = _REPO_ROOT / "src" / "backend" / "services" / "canary_alerts.py"
_REGISTRY_SRC = _REPO_ROOT / "src" / "backend" / "canary" / "invariants" / "__init__.py"

_DICT_SURFACES = ("_INVARIANT_NAMES", "_INVARIANT_RUNBOOKS")
_BRANCH_SURFACES = ("_render_message", "_render_forensic")
_ALL_SURFACES = _DICT_SURFACES + _BRANCH_SURFACES

# Self-test floor. If a refactor blinds a visitor it returns an empty set, and
# `registry <= set()` would still be a *failure* — but the reverse-direction
# test and the anti-stub tests would silently pass over nothing. Assert the
# parser is still finding roughly what we know is there.
_MIN_IDS_PER_SURFACE = 10


# --------------------------------------------------------------------------
# AST extraction — no imports, either side.
# --------------------------------------------------------------------------
def _dict_keys(tree: ast.AST, name: str) -> Set[str]:
    """String keys of a dict assigned to `name`, anywhere in `tree`.

    Three traps, each of which silently turns this into a no-op:

    * `ast.walk`, not a scan of `tree.body` — `_INVARIANT_NAMES` and
      `_INVARIANT_RUNBOOKS` are *class-body* assignments inside a `ClassDef`.
    * Both `Assign` AND `AnnAssign` — `INVARIANTS` is annotated
      (`INVARIANTS: Dict[str, Callable[...]] = {...}`), so an `Assign`-only
      walk finds nothing there. This is `learnings.md` 2026-07-16 lesson (1)
      verbatim: "annotating the upstream constant silently turns the guard
      into a no-op".
    * Keys only — never `literal_eval` the whole dict. `INVARIANTS`' values
      are `Name` nodes (`s01_check`, …) and would raise.
    """
    found: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if not any(getattr(t, "id", None) == name for t in targets):
            continue
        if not isinstance(node.value, ast.Dict):
            continue
        for key in node.value.keys:
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                found.add(key.value)
    return found


def _dict_items(tree: ast.AST, name: str) -> Dict[str, str]:
    """`{key: value}` for a dict of string→string literals assigned to `name`.

    Adjacent parenthesised string fragments are folded into one `Constant` by
    the parser, so the multi-line runbook entries read back whole.
    """
    items: Dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(getattr(t, "id", None) == name for t in node.targets):
            continue
        if not isinstance(node.value, ast.Dict):
            continue
        for key, val in zip(node.value.keys, node.value.values):
            if (
                isinstance(key, ast.Constant)
                and isinstance(key.value, str)
                and isinstance(val, ast.Constant)
                and isinstance(val.value, str)
            ):
                items[key.value] = val.value
    return items


def _branch_ids(tree: ast.AST, func_name: str) -> Set[str]:
    """Invariant ids compared against `invariant_id` inside `func_name`.

    Deliberately scoped to the target `FunctionDef`. A repo-wide sweep for
    `[A-Z]-\\d\\d` string constants would be the fail-OPEN version: docstrings
    are `Constant` nodes too, and this module's own prose mentions every id —
    which would manufacture coverage for ids that have no branch at all.

    Handles `==` and `in (...)`/`in [...]` so a future grouped branch
    (`if invariant_id in ("E-03", "G-03")` — those two share a row shape) is
    not read as zero coverage.
    """
    found: Set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != func_name:
            continue
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Compare):
                continue
            if getattr(sub.left, "id", None) != "invariant_id":
                continue
            for comparator in sub.comparators:
                if isinstance(comparator, ast.Constant) and isinstance(
                    comparator.value, str
                ):
                    found.add(comparator.value)
                elif isinstance(comparator, (ast.Tuple, ast.List, ast.Set)):
                    for elt in comparator.elts:
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                            found.add(elt.value)
    return found


def _registry_ids() -> Set[str]:
    return _dict_keys(ast.parse(_REGISTRY_SRC.read_text(encoding="utf-8")), "INVARIANTS")


def _coverage() -> Dict[str, Set[str]]:
    """Ids covered by each of the four alert surfaces."""
    tree = ast.parse(_ALERTS_SRC.read_text(encoding="utf-8"))
    covered = {name: _dict_keys(tree, name) for name in _DICT_SURFACES}
    covered.update({name: _branch_ids(tree, name) for name in _BRANCH_SURFACES})
    return covered


# --------------------------------------------------------------------------
# Parity — the CI gate.
# --------------------------------------------------------------------------
@pytest.mark.parametrize("surface", _ALL_SURFACES)
def test_every_registry_invariant_has_an_alert_surface_entry(surface):
    """Forward direction: registry ⊆ surface, for all four surfaces.

    All four matter, and the two the issue did NOT name are load-bearing:
    `_render_forensic` is where a per-row invariant's actual evidence goes, so
    an invariant with a name and a runbook but no forensic branch still pages
    the on-call with a count and no rows. For G-04 that is the difference
    between "a credential leaked somewhere" and "*which* row, on which agent".
    """
    covered = _coverage()[surface]
    missing = sorted(_registry_ids() - covered)
    assert not missing, (
        f"{surface} is missing {missing}. Every id in "
        f"canary/invariants/__init__.py::INVARIANTS needs an entry in ALL FOUR "
        f"alert surfaces {list(_ALL_SURFACES)}, or its green→red Slack alert "
        f"degrades to a bare-id fallback with no name, detail, or next step "
        f"(#1880). Name it from the invariant module's docstring title — NOT "
        f"from docs/testing/orchestration-invariant-catalog.md, whose ids do "
        f"not all match the registry."
    )


@pytest.mark.parametrize("surface", _ALL_SURFACES)
def test_no_alert_surface_names_an_unknown_invariant(surface):
    """Reverse direction: surface ⊆ registry.

    Catches a typo (`"G-4"`, `"E-06 "`) that the forward test cannot see, and
    a stale entry left behind when an invariant is renamed or retired — S-01
    is slated to retire with the slot ZSET in #1081 Phase 5, so this half will
    earn its keep.
    """
    unknown = sorted(_coverage()[surface] - _registry_ids())
    assert not unknown, (
        f"{surface} references {unknown}, which are not in "
        f"canary/invariants/__init__.py::INVARIANTS. Either the id is a typo "
        f"(dead code that will never render) or the invariant was removed and "
        f"its alert entry was left behind."
    )


def test_extractors_still_see_what_we_know_is_there():
    """Self-test: a blinded visitor must fail loudly, not compare empty sets.

    If a refactor moves a dict or reshapes the branch chains, the extractors
    return `set()`. The forward test would still fail (good), but the reverse
    test and every anti-stub test below would pass over nothing (bad) — the
    guard would half-rot without anyone noticing.
    """
    registry = _registry_ids()
    assert len(registry) >= 15, (
        f"only parsed {len(registry)} ids out of INVARIANTS "
        f"({sorted(registry)}) — the registry extractor is likely blind. "
        f"Check that INVARIANTS is still a dict literal with string keys."
    )
    for surface, ids in _coverage().items():
        assert len(ids) >= _MIN_IDS_PER_SURFACE, (
            f"only parsed {len(ids)} ids out of {surface} ({sorted(ids)}). "
            f"The extractor is probably blind to a refactor (a dict moved out "
            f"of the class body, an if-chain replaced by a dispatch table). "
            f"Fix the extractor — do not lower this floor."
        )


def test_guard_would_catch_a_regression():
    """Meta-test: prove the extractors fail on pre-fix content.

    Without this, an extractor typo makes every assertion above vacuously
    true and the guard rots into a no-op — the same way the bug it guards
    shipped green. Mirrors `test_canary_env_prod_parity.py`.
    """
    pre_fix = (
        "class CanaryAlerts:\n"
        "    _INVARIANT_NAMES = {\n"
        '        "S-01": "Slot–row bijection",\n'
        "    }\n"
        "    @staticmethod\n"
        "    def _render_message(invariant_id, violations, snapshot_time):\n"
        '        if invariant_id == "S-01":\n'
        '            return "boom"\n'
        '        return f"{invariant_id} fired."\n'
    )
    tree = ast.parse(pre_fix)
    assert _dict_keys(tree, "_INVARIANT_NAMES") == {"S-01"}, (
        "dict extractor must read class-body assignments (ast.walk, not a "
        "top-level body scan)"
    )
    assert _branch_ids(tree, "_render_message") == {"S-01"}
    assert "G-04" not in _dict_keys(tree, "_INVARIANT_NAMES"), (
        "extractor must find nothing for an id that has no entry — otherwise "
        "the parity assertions are vacuously true"
    )

    annotated = 'INVARIANTS: Dict[str, int] = {"E-03": 1, "G-04": 2}\n'
    assert _dict_keys(ast.parse(annotated), "INVARIANTS") == {"E-03", "G-04"}, (
        "AnnAssign must be handled — INVARIANTS is annotated, and an "
        "Assign-only walk would silently return an empty registry"
    )

    grouped = (
        "def _render_forensic(cls, invariant_id, violations):\n"
        '    if invariant_id in ("E-03", "G-03"):\n'
        "        return None\n"
    )
    assert _branch_ids(ast.parse(grouped), "_render_forensic") == {"E-03", "G-03"}, (
        "`in (...)` grouping must count as coverage for every id it names"
    )

    docstring_only = (
        "def _render_message(invariant_id, violations, snapshot_time):\n"
        '    """Renders E-99 and Z-01 among others."""\n'
        "    return None\n"
    )
    assert _branch_ids(ast.parse(docstring_only), "_render_message") == set(), (
        "prose mentioning an id must NOT count as coverage — that is the "
        "fail-open failure mode of a naive regex/constant sweep"
    )


# --------------------------------------------------------------------------
# Anti-stub — presence is necessary, not sufficient.
# --------------------------------------------------------------------------
def test_names_are_not_id_stubs():
    """A name equal to (or merely containing) the bare id defeats the point.

    The predictable way to satisfy the parity gate under time pressure is
    `"S-04": "S-04 check"`. That is green, useless, and worse than failing —
    the guard would then *certify* the uselessness.
    """
    tree = ast.parse(_ALERTS_SRC.read_text(encoding="utf-8"))
    for inv_id, name in sorted(_dict_items(tree, "_INVARIANT_NAMES").items()):
        assert name.strip() != inv_id, f"{inv_id}: name is just the id"
        assert inv_id not in name, (
            f"{inv_id}: name {name!r} embeds the id, which already appears "
            f"beside it in the Slack header — that renders as a stutter "
            f"('E-03 E-03 …'), the exact symptom of #1880."
        )
        assert len(name.strip()) >= 10, (
            f"{inv_id}: name {name!r} is too short to tell an on-call what "
            f"broke. Derive it from the invariant module's docstring title."
        )


def test_runbooks_are_substantive():
    """A runbook must orient someone, not restate the alert.

    Length is the honest signal for the regression this guards — a stub added
    to silence the parity gate ("See the catalog.", 17 chars). The floor sits
    below the shortest legitimate entry (L-03, 115) with room to spare.

    Deliberately NOT asserted: "contains a backticked identifier". It reads as
    a good proxy for the stated contract ("where to start looking"), but
    measured against the real dict it flags two entries that meet the contract
    fine — E-02 names a bug class rather than a file, and L-03 points at its
    own forensic block ("the table(s) listed above"), which lists the tables
    dynamically. A heuristic that fires on correct content is the one a future
    contributor weakens or deletes, and then the length floor goes with it.
    `learnings.md` 2026-07-16 lesson (2): assert the regression's signature,
    not a proxy for it.
    """
    tree = ast.parse(_ALERTS_SRC.read_text(encoding="utf-8"))
    runbooks = _dict_items(tree, "_INVARIANT_RUNBOOKS")
    assert runbooks, "runbook extractor found nothing — see the self-test"
    for inv_id, text in sorted(runbooks.items()):
        assert len(text) >= 80, (
            f"{inv_id}: runbook is {len(text)} chars — too thin to orient "
            f"anyone at 2am. Say what the state means and where to look. "
            f"(A one-line stub silences the parity gate while leaving the "
            f"alert as useless as having no entry at all.)"
        )


def test_g04_runbook_never_sends_anyone_to_the_raw_blob():
    """#1880 AC3 — G-04's runbook must not instruct reading `backlog_metadata`.

    The invariant reports the matched pattern NAME only, on purpose: the blob
    may hold a live credential and violations persist to `canary_violations`.
    A runbook saying "check the metadata" would undo that by instruction —
    moving the secret from a DB column into a Slack channel, then into
    whatever the responder pastes it in to.
    """
    tree = ast.parse(_ALERTS_SRC.read_text(encoding="utf-8"))
    runbook = _dict_items(tree, "_INVARIANT_RUNBOOKS").get("G-04", "")
    assert runbook, "G-04 has no runbook"
    lowered = runbook.lower()
    for phrase in (
        "read the backlog_metadata",
        "inspect the backlog_metadata",
        "read `backlog_metadata`",
        "inspect `backlog_metadata`",
        "dump the metadata",
        "paste the metadata",
    ):
        assert phrase not in lowered, (
            f"G-04 runbook contains {phrase!r}. The check deliberately "
            f"reports only the pattern name; the runbook must not route "
            f"around that."
        )
    assert "rotate" in lowered, (
        "G-04 runbook must lead with containment — rotating the matched "
        "credential comes before any triage, because the value is stored "
        "plaintext and outlives the alert."
    )
