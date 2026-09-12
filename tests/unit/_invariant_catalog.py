"""One parser for the invariant namespace (#2337, Rail R3).

Three files describe the same set of ids and used to be read by three
different regexes:

* `docs/testing/orchestration-invariant-catalog.md` — the DEFINITIONS. Every
  `**X-NN** Title *(Tier A|B …)*` entry is the single declaration of that id.
* `src/backend/canary/invariants/` — the LIVE SET: `INVARIANTS` in
  `__init__.py` plus one `INVARIANT_ID` per module.
* the catalog's *Canary mapping* table — the documented join between the two.

`tests/unit/test_2338_journey_catalog.py` (journey records cite ids that
resolve) and `tests/unit/test_2337_invariant_namespace.py` (the three sources
agree) both read through this module, so there is exactly one answer to "what
is an invariant id". Before #2337 the validator resolved ids from markdown
TABLE rows only — the 12-row "Recommended starting subset" — and 60 of the 71
defined invariants were uncitable (`learnings.md` 2026-07-16: a mirrored list
with no owner). Body entries are the source; a table is never one.

Pure stdlib, imports nothing from `src/` (the #762 stub-leak class cannot
reach it). Read source, never import it: `learnings.md` 2026-07-16 lesson (1).

Underscore-prefixed so pytest never collects it as a test module.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Dict, List, NamedTuple, Set

ID = r"[A-Z]+-[0-9]{2,}"

# The FULL entry shape, not just a bold id. A bare `**X-NN**` also matches a
# prose mention inside another entry's footnote (`**SCH-03**` in the old E-06
# dagger note read as a 72nd invariant); requiring the title and the tier
# clause keeps definitions and mentions apart. `{2,}` refuses a typo like
# `E-1` the right to mint an id. Verified against every existing entry when
# it was introduced: 71 matched, 0 lost.
ENTRY_RE = re.compile(rf"^\*\*({ID})\*\*\s+\S.*?\*\(Tier [AB]", re.MULTILINE)

# An entry's body runs to the next entry, the next section, or a rule.
_BOUNDARY_RE = re.compile(rf"^(?:\*\*{ID}\*\*\s+\S.*?\*\(Tier [AB]|## |---\s*$)", re.MULTILINE)

SIGNAL_RE = re.compile(r"^Signal:", re.MULTILINE)

# `| \`<module>.py\` | <registry id> | <catalog id> | …`. The first cell is a
# filename ON PURPOSE: a table whose first cell is an id is exactly the shape
# the old resolver read as a second definition site.
MAPPING_ROW_RE = re.compile(
    rf"^\|\s*`([a-z0-9_]+\.py)`\s*\|\s*({ID})\s*\|\s*({ID})\s*\|", re.MULTILINE
)


def read(path: Path) -> str:
    # The catalog carries `†`, `≤`, `→` and emoji; a runner whose default
    # encoding is not UTF-8 would otherwise raise before any assertion runs.
    return Path(path).read_text(encoding="utf-8")


def catalog_ids(text: str) -> List[str]:
    """Every id DEFINED in the catalog body, in file order.

    Duplicates are returned, not collapsed — a second definition of the same
    id is a finding for the caller to name, not something to hide in a set.
    """
    return ENTRY_RE.findall(text)


def entries(text: str) -> Dict[str, str]:
    """id → the entry's text (header line through the line before the next
    boundary). Used to ask per-entry questions such as "does it carry a
    `Signal:`?". First definition wins when an id is duplicated."""
    found: Dict[str, str] = {}
    starts = [(m.start(), m.group(1)) for m in ENTRY_RE.finditer(text)]
    for start, inv_id in starts:
        nxt = _BOUNDARY_RE.search(text, start + 1)
        body = text[start: nxt.start() if nxt else len(text)]
        found.setdefault(inv_id, body)
    return found


def ids_with_signal(text: str) -> Set[str]:
    """Ids whose entry carries a runnable `Signal:` line (AC #2)."""
    return {i for i, body in entries(text).items() if SIGNAL_RE.search(body)}


def family(inv_id: str) -> str:
    return inv_id.split("-", 1)[0]


class MappingRow(NamedTuple):
    module: str
    registry_id: str
    catalog_id: str


def mapping_rows(text: str) -> List[MappingRow]:
    """Rows of the *Canary mapping* table."""
    return [MappingRow(*m) for m in MAPPING_ROW_RE.findall(text)]


# --------------------------------------------------------------------------
# The live set — read via `ast`, never imported.
# --------------------------------------------------------------------------
def _dict_keys(tree: ast.AST, name: str) -> Set[str]:
    """String keys of the dict assigned to `name`, anywhere in `tree`.

    Both `Assign` and `AnnAssign`: `INVARIANTS` is annotated
    (`INVARIANTS: Dict[str, Callable[...]] = {...}`), and an `Assign`-only walk
    finds nothing there — `learnings.md` 2026-07-16 lesson (1) verbatim,
    already paid for by `test_1880_canary_alert_parity.py`.
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


def registry_dict_ids(init_py: Path) -> Set[str]:
    """Keys of `INVARIANTS` in `canary/invariants/__init__.py` — what
    `run_cycle` can actually evaluate."""
    return _dict_keys(ast.parse(read(init_py)), "INVARIANTS")


def module_ids(registry_dir: Path) -> Dict[str, str]:
    """`<module>.py` → its `INVARIANT_ID` constant, for every module that
    declares one. A module without the constant is not an invariant module
    (`__init__.py`) and is skipped, never guessed at from its filename."""
    found: Dict[str, str] = {}
    for path in sorted(Path(registry_dir).glob("*.py")):
        tree = ast.parse(read(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            if not any(getattr(t, "id", None) == "INVARIANT_ID" for t in node.targets):
                continue
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                found[path.name] = node.value.value
    return found
