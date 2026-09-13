"""#2337 — Rail R3: exactly one invariant namespace.

Three sources describe the same ids and must agree:

* `docs/testing/orchestration-invariant-catalog.md` — the DEFINITIONS, one
  `**X-NN** Title *(Tier A|B …)*` entry per id;
* `src/backend/canary/invariants/` — the LIVE SET: `INVARIANTS` in
  `__init__.py`, and one `INVARIANT_ID` per module;
* the catalog's *Canary mapping* table — the documented join between them.

Before #2337 the journey validator resolved ids from the catalog's one markdown
TABLE (the 12-row "Recommended starting subset"), so 60 of 71 defined
invariants were uncitable, and the catalog's `E-06` (the unimplemented #129
orphan check) shared an id with the registry's `E-06` ("no overdue
`next_run_at`", #1472) — two meanings, one id, and an id-only subset check
would have *certified* the collision (`learnings.md` 2026-07-16: the mirror's
own tests pin the drift as the requirement). The #129 check was re-homed to
`E-09` by operator ruling, and this guard is what keeps the three sources
equal from now on.

Structure, and why (precedent `test_1880_canary_alert_parity.py`):

* Everything is read from SOURCE via `tests/unit/_invariant_catalog.py` —
  `re` over the markdown, `ast` over the registry — and nothing under `src/`
  is imported. `services/__init__.py` eagerly imports the Docker SDK, and
  `services.*` is a `sys.modules` stub-leak target under pytest-randomly; an
  import-based guard would bind to a sibling's stub and pass vacuously.
* Counts are exact PINS, not floors (`DECLARED_JOURNEY_COUNT` in
  `test_2338`): a floor catches a blinded parser but not a deleted entry.
  Adding an invariant means bumping the family's pin in the same commit — the
  number is the deliberate act.
* The parser is proved on synthetic markdown, in both directions: a prose bold
  mention, a dagger row and a table row are NOT definitions; a well-formed
  entry IS. Without the negative cases a parser that matched everything would
  pass every positive one.
* Lives under `tests/unit/` because that is the directory the CI unit job
  collects (`learnings.md` 2026-07-30: put the guard where CI actually looks).
"""
from __future__ import annotations

import collections
import re
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
CATALOG = REPO / "docs/testing/orchestration-invariant-catalog.md"
REGISTRY_INIT = REPO / "src/backend/canary/invariants/__init__.py"
REGISTRY_DIR = REPO / "src/backend/canary/invariants"
SCHEMA = REPO / "src/backend/db/schema.py"


def _lib():
    """Explicit-path import, inside the function, the way `test_2338._gen()`
    imports its sibling — an import failure degrades this module only."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import _invariant_catalog as lib
    return lib


# --------------------------------------------------------------------------
# Pins. Bump in the same commit that adds or removes an entry.
# --------------------------------------------------------------------------
DECLARED_FAMILIES = {
    "AC": 7, "AU": 2, "B": 7, "CH": 4, "E": 9, "EV": 3, "G": 5, "H": 1,
    "IA": 3, "L": 6, "MCP": 3, "OQ": 4, "P": 5, "PLG": 1, "R": 4, "RD": 3,
    "S": 7, "SCH": 5, "SK": 3,
}
DECLARED_TOTAL = 82  # 71 pre-#2337 + E-09 + SK×3 + RD×3 + PLG + IA×3

# The catalog's own bar (§Invariant tiering): "each invariant is expressed so
# it is directly checkable … 'Signal' is the exact query the canary runs". The
# entries below carry NO line beginning `Signal:` — some state their predicate
# inline without the label (E-03, E-04, S-02 …), most are prose. This set is a
# RATCHET: it may only shrink. Paying one down means adding the labelled
# `Signal:` line and deleting the id here in the same commit; an entry that is
# not in this set and lacks the line fails `test_every_other_entry_carries_a_signal`.
# Registered as debt: `.claude/DEBT_INBOX.md`
# `debt:2026-09-12-invariant-catalog-legacy-entries-have-no-runnable-signal`.
LEGACY_NO_SIGNAL_LINE = frozenset({
    "AC-01", "AC-03", "AC-04", "AC-05", "AC-06", "AC-07",
    "AU-01", "AU-02",
    "B-01", "B-03", "B-04", "B-05", "B-06", "B-07",
    "CH-01", "CH-02", "CH-03", "CH-04",
    "E-03", "E-04", "E-05", "E-07", "E-08",
    "EV-01", "EV-02", "EV-03",
    "G-01", "G-02", "G-03", "G-04", "G-05",
    "H-01",
    "L-02", "L-03", "L-04", "L-05", "L-06",
    "MCP-01", "MCP-02", "MCP-03",
    "OQ-01", "OQ-02", "OQ-03", "OQ-04",
    "P-01", "P-02", "P-03", "P-04", "P-05",
    "R-01", "R-02", "R-03", "R-04",
    "S-02", "S-07",
    "SCH-01", "SCH-02", "SCH-03", "SCH-04", "SCH-05",
})

# Every table a #2337 family's `Signal:` names, and the section that names it.
# A predicate over a table that does not exist is prose with a SELECT in it.
NEW_FAMILY_TABLES = {
    "## 16. Skills": {"agent_skills", "skill_sources", "agent_ownership"},
    "## 17. Repo-bound deployment": {"agent_git_config", "agent_sync_state",
                                     "agent_ownership", "schedule_executions"},
    "## 19. Inter-agent calls": {"schedule_executions", "agent_permissions"},
}

# Self-test floor: if a refactor blinds the registry reader, the equality
# tests below would compare three EMPTY sets and pass.
_MIN_LIVE = 16


@pytest.fixture(scope="module")
def lib():
    return _lib()


@pytest.fixture(scope="module")
def text(lib):
    return lib.read(CATALOG)


# --------------------------------------------------------------------------
# The definitions.
# --------------------------------------------------------------------------
def test_every_family_is_declared_at_its_pinned_count(lib, text):
    ids = lib.catalog_ids(text)
    found = collections.Counter(lib.family(i) for i in ids)
    assert dict(found) == DECLARED_FAMILIES, (
        f"catalog families {dict(sorted(found.items()))} != pinned "
        f"{dict(sorted(DECLARED_FAMILIES.items()))}. Adding or removing an "
        f"invariant means bumping DECLARED_FAMILIES in the same commit."
    )
    assert len(ids) == DECLARED_TOTAL == sum(DECLARED_FAMILIES.values())


def test_no_id_is_defined_twice(lib, text):
    ids = lib.catalog_ids(text)
    dups = sorted(i for i, n in collections.Counter(ids).items() if n > 1)
    assert not dups, (
        f"{dups} are defined more than once — a second definition is a second "
        f"namespace. Fold the entries or give the second one its own id."
    )


def test_every_other_entry_carries_a_signal(lib, text):
    """The ratchet, in both directions: an entry outside the legacy set
    without a `Signal:` line fails (new families must be runnable — AC #2), and
    a legacy id that has gained its line must leave the set (the allowlist may
    only shrink, and must not sit above reality — the #2605 ratchet rule)."""
    ids = set(lib.catalog_ids(text))
    with_signal = lib.ids_with_signal(text)
    missing = sorted(ids - with_signal - LEGACY_NO_SIGNAL_LINE)
    assert not missing, (
        f"{missing} carry no `Signal:` line and are not in the legacy set. "
        f"Every entry added since #2337 states its predicate as a runnable "
        f"`Signal:` (SQL / Redis / HTTP / file) — that is AC #2."
    )
    paid_down = sorted(LEGACY_NO_SIGNAL_LINE & with_signal)
    assert not paid_down, (
        f"{paid_down} now carry a `Signal:` line — remove them from "
        f"LEGACY_NO_SIGNAL_LINE so the ratchet keeps biting."
    )
    vanished = sorted(LEGACY_NO_SIGNAL_LINE - ids)
    assert not vanished, f"{vanished} are in the legacy set but no longer defined"


@pytest.mark.parametrize("section,tables", sorted(NEW_FAMILY_TABLES.items()))
def test_new_family_signals_name_tables_that_exist(text, section, tables):
    schema = SCHEMA.read_text(encoding="utf-8")
    assert section in text, (
        f"{section!r} heading not found — renamed? Update NEW_FAMILY_TABLES in "
        f"the same commit; a silently unmatched section is an unguarded family."
    )
    start = text.index(section)
    end = text.index("\n## ", start + 1)
    body = text[start:end]
    for table in sorted(tables):
        assert f"CREATE TABLE IF NOT EXISTS {table} (" in schema, (
            f"{section} predicates over `{table}`, which db/schema.py does not "
            f"create — a SELECT over a missing table is prose"
        )
        assert f"`{table}" in body, f"{section} should name `{table}` in its text"


# --------------------------------------------------------------------------
# The parser, proved on synthetic markdown — negatives first.
# --------------------------------------------------------------------------
_ENTRY = "**Q-01** A real entry *(Tier A, 🔴)*\nProse.\nSignal: `SELECT 1` → 0.\n"


def test_a_well_formed_entry_is_a_definition(lib):
    assert lib.catalog_ids(_ENTRY) == ["Q-01"]
    assert lib.ids_with_signal(_ENTRY) == {"Q-01"}


@pytest.mark.parametrize("fragment,why", [
    ("Compare with **Q-02** above, which shares the row shape.\n",
     "a bold prose mention inside another entry"),
    ("**Q-03 †** No completed-but-not-reported — see the drift note.\n",
     "the old dagger footnote form"),
    ("| Q-04 | Some title | #129 |\n", "a summary-table row"),
    ("**Q-05** Title without a tier clause\n", "a bold id with no `*(Tier` — not the entry shape"),
    ("**Q-6** Typo id *(Tier A, 🟡)*\n", "a one-digit id"),
])
def test_things_that_are_not_definitions(lib, fragment, why):
    text = _ENTRY + fragment
    assert lib.catalog_ids(text) == ["Q-01"], f"{why} was read as a definition"


def test_a_signal_belongs_to_its_own_entry_only(lib):
    two = _ENTRY + "**Q-02** Prose only *(Tier B ≤ 5 min, 🟢)*\nJust words.\n"
    assert lib.ids_with_signal(two) == {"Q-01"}


@pytest.mark.parametrize("boundary", ["---\n", "## 99. Next section\n"])
def test_an_entry_ends_at_a_section_boundary(lib, boundary):
    """The LAST entry of a section must not absorb what follows the rule or the
    next heading — a `Signal:` line in a section preamble would otherwise be
    credited to the entry above it."""
    prose = "**Q-02** Prose only *(Tier B ≤ 5 min, 🟢)*\nJust words.\n\n"
    stray = boundary + "\nSignal: `SELECT 1` → 0 (belongs to nobody).\n"
    assert lib.ids_with_signal(_ENTRY + prose + stray) == {"Q-01"}


def test_duplicate_definitions_are_reported_not_collapsed(lib):
    assert lib.catalog_ids(_ENTRY + _ENTRY) == ["Q-01", "Q-01"]


# --------------------------------------------------------------------------
# The live set, three ways, and the mapping table that joins it to the catalog.
# --------------------------------------------------------------------------
def test_registry_dict_modules_and_mapping_table_agree(lib, text):
    in_dict = lib.registry_dict_ids(REGISTRY_INIT)
    in_modules = lib.module_ids(REGISTRY_DIR)
    rows = lib.mapping_rows(text)
    in_table = {r.registry_id for r in rows}

    assert len(in_dict) >= _MIN_LIVE, "registry reader is blind — INVARIANTS not found"
    assert in_dict == set(in_modules.values()), (
        f"INVARIANTS keys {sorted(in_dict)} != module INVARIANT_IDs "
        f"{sorted(in_modules.values())}: a key registered without a module "
        f"constant, or a module never registered, is not a live invariant"
    )
    assert in_dict == in_table, (
        f"INVARIANTS {sorted(in_dict)} != Canary mapping table "
        f"{sorted(in_table)} — every live invariant has exactly one row"
    )
    assert len(rows) == len(in_table), "a registry id appears in two mapping rows"
    for r in rows:
        assert (REGISTRY_DIR / r.module).exists(), f"mapping names {r.module}, which does not exist"
        assert in_modules.get(r.module) == r.registry_id, (
            f"{r.module} declares INVARIANT_ID={in_modules.get(r.module)!r}, "
            f"the mapping row says {r.registry_id}"
        )


def test_every_mapping_row_resolves_and_no_row_drifts(lib, text):
    defined = set(lib.catalog_ids(text))
    rows = lib.mapping_rows(text)
    unresolved = [r.catalog_id for r in rows if r.catalog_id not in defined]
    assert not unresolved, f"mapping rows cite {unresolved}, which no `**ID**` entry defines"
    drift = [(r.registry_id, r.catalog_id) for r in rows if r.registry_id != r.catalog_id]
    assert not drift, (
        f"registry/catalog ids differ on {drift}. One id, one meaning: re-home "
        f"the catalog entry (as #2337 did for E-06 → E-09) rather than "
        f"documenting a collision — a documented collision is still two namespaces."
    )


def test_the_mapping_table_never_starts_a_row_with_an_id(text):
    """The pre-#2337 resolver read `| X-NN |` rows as definitions. The
    mapping table starts every row with the MODULE so that regex — or its
    successor — can never mistake the join for a second definition site."""
    start = text.index("## Canary mapping")
    end = text.index("\n## ", start + 1)
    rows = re.findall(r"^\|\s*([A-Z]+-[0-9]+)\s*\|", text[start:end], re.MULTILINE)
    assert not rows, f"mapping rows starting with an id: {rows}"
