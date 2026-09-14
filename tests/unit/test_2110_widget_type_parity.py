"""#2110 — the dashboard widget-type allowlist lives in three code sites and
five contract documents, and until this file nothing pinned any two together.

The set behind `D-002` ("all widget types are supported") is hand-copied:

| Tier | Where | Role |
|---|---|---|
| A | `services/compatibility/static_checks.py::_WIDGET_TYPES` | the D-002 allowlist |
| A | `docker/base-image/agent_server/routers/dashboard.py::validate_widget.valid_types` | the agent-side gate that STRIPS unknown widgets before the UI sees them |
| A | `src/frontend/src/components/DashboardPanel.vue` | the `widget.type === '…'` render chain |
| B | `docs/agent-validation-spec.md` `Allowed types:` | canonical check contract |
| B | `docs/TRINITY_COMPATIBLE_AGENT_GUIDE.md` `### Widget Types` table | served verbatim by MCP `get_agent_requirements` |
| B | `services/agent_service/dashboard.py` docstring | lands in the OpenAPI description |
| B | `docs/user-docs/advanced/dynamic-dashboards.md` `**Widget Types**` bullet | Vertex docs Q&A index |
| B | `docs/user-docs/faq/advanced-features.md` "widget types are supported" | Vertex docs Q&A index |

#2110 is what that looks like when it drifts: two user-docs pages advertised
`chart`, `badge`, `countdown` (none ever existed) and omitted `markdown`,
`divider`, `spacer`; both pages feed the docs Q&A index, a fleet generator
learned `type: chart` from somewhere, and every agent it scaffolded carried a
permanently red D-002. The agent-server list is a *semantic* twin of the backend
tuple, not one of the four Invariant #5 byte mirrors, so the byte-parity tests
never covered it.

Design:

* **Stdlib only, read-never-import.** `agent_server/routers/dashboard.py` has
  relative imports, so `spec_from_file_location` does not apply; `ast.parse` +
  a walk for the defining `Assign`/`AnnAssign` (the agent-server list is
  function-local) reads the constant without executing anything. The element
  list is taken in SOURCE order, so the Tier-A order assertion is deterministic
  whatever container the literal uses.
* **Exactly one binding, never mutated.** A second assignment, an `x += [...]`
  or an `x.append(...)` on the allowlist name fails the extractor, so a later
  `valid_types += ['chart']` cannot slip past a guard that only read the first
  binding.
* **Extractors are `(text) -> set[str]` functions.** The meta-tests feed them
  planted strings, so the guard is proven to bite on every run — not only on the
  commit where the docs were still wrong.
* **Tier C is a regression-signature check, not a fuzzy sweep.** A line that
  says "widget type(s)" in a reader-facing doc must not present a backticked
  `chart`, `badge` or `countdown`. A subset sweep was rejected: the guide's
  Revision History truthfully backticks `text`/`values`/`href` as anti-examples
  and would have fired on day one, so that section is sliced off.

**Authoring rule this imposes:** on any line that says "widget type(s)" in
`docs/user-docs/**`, the agent guide (outside Revision History) or the spec,
never backtick `chart`, `badge` or `countdown` — bare or as `type: chart`.
Name them in plain prose. Quoting D-002's own output in a longer code span is
fine (the backticks are not adjacent to the token).

CI: `tests/unit/` is the only directory the per-PR job collects
(`backend-unit-test.yml` → `pytest unit/`), which is why the guard lives here.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]

_BACKEND_CHECKS = _ROOT / "src" / "backend" / "services" / "compatibility" / "static_checks.py"
_AGENT_DASHBOARD = _ROOT / "docker" / "base-image" / "agent_server" / "routers" / "dashboard.py"
_VUE_PANEL = _ROOT / "src" / "frontend" / "src" / "components" / "DashboardPanel.vue"
_PROXY_MODULE = _ROOT / "src" / "backend" / "services" / "agent_service" / "dashboard.py"
_SPEC = _ROOT / "docs" / "agent-validation-spec.md"
_GUIDE = _ROOT / "docs" / "TRINITY_COMPATIBLE_AGENT_GUIDE.md"
_USER_DOC_DASHBOARDS = _ROOT / "docs" / "user-docs" / "advanced" / "dynamic-dashboards.md"
_USER_DOC_FAQ = _ROOT / "docs" / "user-docs" / "faq" / "advanced-features.md"
_USER_DOCS_DIR = _ROOT / "docs" / "user-docs"

_FICTIONAL = ("chart", "badge", "countdown")
_ANCHOR_HELP = "anchor not found — update the extractor; this line is a contract surface"


class _AnchorMissing(AssertionError):
    """A Tier-B extractor could not find the line it pins (raised, never skipped)."""


class _AllowlistShape(AssertionError):
    """The allowlist binding is missing, duplicated, non-literal, or mutated."""


# ---------------------------------------------------------------------------
# Tier A extractors — code, read by AST / regex
# ---------------------------------------------------------------------------

_MUTATORS = ("append", "extend", "insert", "add", "update", "remove", "discard", "pop", "clear")


def _literal_assign(source: str, name: str) -> list:
    """Return the elements of the ONE literal sequence bound to `name`, in source order.

    Walks the whole module (the agent-server list is local to `validate_widget`).
    Fails when there are zero or several bindings, when the value is not a
    tuple/list/set literal of constants, or when anything mutates the name after
    definition (`AugAssign`, or a `.append/.extend/...` call on it).
    """
    tree = ast.parse(source)
    bindings = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
                bindings.append(node.value)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == name and node.value is not None:
                bindings.append(node.value)
        elif isinstance(node, ast.AugAssign):
            if isinstance(node.target, ast.Name) and node.target.id == name:
                raise _AllowlistShape(f"{name} is mutated after definition (augmented assignment)")
        elif isinstance(node, ast.Call):
            fn = node.func
            if (isinstance(fn, ast.Attribute) and fn.attr in _MUTATORS
                    and isinstance(fn.value, ast.Name) and fn.value.id == name):
                raise _AllowlistShape(f"{name} is mutated after definition (.{fn.attr}())")
    if len(bindings) != 1:
        raise _AllowlistShape(f"expected exactly one binding of {name}, found {len(bindings)}")
    value = bindings[0]
    if not isinstance(value, (ast.Tuple, ast.List, ast.Set)):
        raise _AllowlistShape(f"{name} is not a tuple/list/set literal ({type(value).__name__})")
    elts = [ast.literal_eval(e) for e in value.elts]
    if not all(isinstance(e, str) for e in elts):
        raise _AllowlistShape(f"{name} holds non-string elements")
    return elts


_VUE_BRANCH = re.compile(r"""widget\.type\s*===\s*['"]([a-z]+)['"]""")


def _vue_render_branches(text: str) -> set:
    """The `widget.type === '…'` branches of the render chain.

    A refactor to a lookup map yields zero hits and fails loudly — by design:
    the extractor must then be pointed at the new shape, not left silent.
    """
    found = set(_VUE_BRANCH.findall(text))
    if not found:
        raise _AnchorMissing(f"DashboardPanel.vue: no `widget.type === '…'` branch — {_ANCHOR_HELP}")
    return found


# ---------------------------------------------------------------------------
# Tier B extractors — contract docs, `(text) -> set[str]`
# ---------------------------------------------------------------------------

_BACKTICKED = re.compile(r"`([a-z]+)`")


def _spec_allowed_types(text: str) -> set:
    m = re.search(r"^Allowed types: (.+?)\.", text, re.M)
    if not m:
        raise _AnchorMissing(f"agent-validation-spec.md `Allowed types:` — {_ANCHOR_HELP}")
    return set(_BACKTICKED.findall(m.group(1)))


def _guide_widget_table(text: str) -> set:
    """First column of the `### Widget Types` table only — prose in the section is never scanned."""
    m = re.search(r"^### Widget Types\n(.*?)(?=^#{2,3} |\Z)", text, re.M | re.S)
    if not m:
        raise _AnchorMissing(f"TRINITY_COMPATIBLE_AGENT_GUIDE.md `### Widget Types` — {_ANCHOR_HELP}")
    rows = re.findall(r"^\|\s*`([a-z]+)`\s*\|", m.group(1), re.M)
    if not rows:
        raise _AnchorMissing(f"TRINITY_COMPATIBLE_AGENT_GUIDE.md Widget Types table rows — {_ANCHOR_HELP}")
    return set(rows)


def _proxy_docstring_types(text: str) -> set:
    m = re.search(r"Widget types supported:\n((?:[ \t]*- [a-z]+:.*\n)+)", text)
    if not m:
        raise _AnchorMissing(f"agent_service/dashboard.py `Widget types supported:` — {_ANCHOR_HELP}")
    return set(re.findall(r"^[ \t]*- ([a-z]+):", m.group(1), re.M))


def _user_doc_widget_bullet(text: str) -> set:
    for line in text.splitlines():
        if "**Widget Types**" in line:
            return set(_BACKTICKED.findall(line))
    raise _AnchorMissing(f"dynamic-dashboards.md `**Widget Types**` bullet — {_ANCHOR_HELP}")


def _faq_widget_sentence(text: str) -> set:
    """Backticks inside the parentheses that follow "widget types are supported"."""
    m = re.search(r"widget types are supported \(([^)]*)\)", text)
    if not m:
        raise _AnchorMissing(f"advanced-features.md `widget types are supported (…)` — {_ANCHOR_HELP}")
    return set(_BACKTICKED.findall(m.group(1)))


# ---------------------------------------------------------------------------
# Tier C — regression signature
# ---------------------------------------------------------------------------

_WIDGET_TYPE_LINE = re.compile(r"widget types?", re.I)
# A bare token (`chart`) or its YAML-key spelling (`type: chart`) — the two ways a
# doc presents a name AS a widget type. A longer code span that merely contains
# the word (the spec quoting D-002's own `… 'chart' ×5 …` output) is truthful and
# must not fire; the meta-test below pins both sides of that line.
_FICTIONAL_TOKEN = re.compile(r"`(?:type:\s*)?(" + "|".join(_FICTIONAL) + r")`")


def _signature_violations(text: str, label: str) -> list:
    """`label:line \\`token\\`` for every "widget type(s)" line that backticks a fictional type."""
    out = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if not _WIDGET_TYPE_LINE.search(line):
            continue
        for tok in _FICTIONAL_TOKEN.findall(line):
            out.append(f"{label}:{lineno} `{tok}`")
    return out


def _guide_without_revision_history(text: str) -> str:
    """The Revision History is out of contract by definition (it quotes anti-examples)."""
    head, sep, _tail = text.partition("\n## Revision History")
    return head if sep else text


# ---------------------------------------------------------------------------
# Fixtures (read once per session; these are repository files, not stacks)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def backend_types() -> list:
    assert _BACKEND_CHECKS.exists(), f"missing {_BACKEND_CHECKS}"
    return _literal_assign(_BACKEND_CHECKS.read_text(encoding="utf-8"), "_WIDGET_TYPES")


@pytest.fixture(scope="module")
def agent_types() -> list:
    assert _AGENT_DASHBOARD.exists(), (
        f"missing {_AGENT_DASHBOARD} — the agent-side widget gate moved; update this path"
    )
    return _literal_assign(_AGENT_DASHBOARD.read_text(encoding="utf-8"), "valid_types")


# ---------------------------------------------------------------------------
# Tier A — code
# ---------------------------------------------------------------------------

def test_backend_allowlist_and_agent_server_gate_have_the_same_members(backend_types, agent_types):
    """The D-002 allowlist and the gate that strips widgets must agree on membership.

    A type in one but not the other means D-002 either blesses a widget the
    agent server will strip, or flags one it renders fine.
    """
    assert len(backend_types) == len(set(backend_types)), f"duplicate in _WIDGET_TYPES: {backend_types}"
    assert len(agent_types) == len(set(agent_types)), f"duplicate in valid_types: {agent_types}"
    assert set(backend_types) == set(agent_types), (
        "backend _WIDGET_TYPES ≠ agent-server valid_types — "
        f"backend-only={sorted(set(backend_types) - set(agent_types))}, "
        f"agent-only={sorted(set(agent_types) - set(backend_types))}. "
        "Change both together (docker/base-image is a base-image rebuild)."
    )


def test_backend_allowlist_and_agent_server_gate_agree_on_order(backend_types, agent_types):
    """Order too: D-002's `supported: …` list and the agent server's warning banner
    both print the sequence, and an operator reading both should see one list."""
    assert list(backend_types) == list(agent_types), (
        f"same members, different order — backend={backend_types}, agent={agent_types}"
    )


def test_render_chain_covers_exactly_the_allowlist(backend_types):
    """Every allowed type has a render branch, and no branch renders a type the
    allowlist (and therefore the agent-server gate) would strip first."""
    branches = _vue_render_branches(_VUE_PANEL.read_text(encoding="utf-8"))
    assert branches == set(backend_types), (
        f"DashboardPanel.vue branches ≠ _WIDGET_TYPES — "
        f"unrendered={sorted(set(backend_types) - branches)}, "
        f"unreachable={sorted(branches - set(backend_types))}"
    )


# ---------------------------------------------------------------------------
# Tier B — contract docs (equality with the backend allowlist)
# ---------------------------------------------------------------------------

_CONTRACT_DOCS = [
    pytest.param(_SPEC, _spec_allowed_types, id="agent-validation-spec"),
    pytest.param(_GUIDE, _guide_widget_table, id="agent-guide-table"),
    pytest.param(_PROXY_MODULE, _proxy_docstring_types, id="proxy-docstring"),
    pytest.param(_USER_DOC_DASHBOARDS, _user_doc_widget_bullet, id="user-docs-dynamic-dashboards"),
    pytest.param(_USER_DOC_FAQ, _faq_widget_sentence, id="user-docs-faq"),
]


@pytest.mark.parametrize("path, extract", _CONTRACT_DOCS)
def test_contract_doc_lists_exactly_the_allowlist(path, extract, backend_types):
    assert path.exists(), f"missing {path}"
    found = extract(path.read_text(encoding="utf-8"))
    expected = set(backend_types)
    assert found == expected, (
        f"{path.relative_to(_ROOT)} lists a different widget-type set than _WIDGET_TYPES — "
        f"invented={sorted(found - expected)}, omitted={sorted(expected - found)}"
    )


# ---------------------------------------------------------------------------
# Tier C — no reader-facing doc presents a fictional type as a widget type
# ---------------------------------------------------------------------------

def test_no_reader_facing_doc_presents_a_fictional_widget_type():
    violations = []
    for md in sorted(_USER_DOCS_DIR.rglob("*.md")):
        violations += _signature_violations(md.read_text(encoding="utf-8"), str(md.relative_to(_ROOT)))
    violations += _signature_violations(
        _guide_without_revision_history(_GUIDE.read_text(encoding="utf-8")),
        str(_GUIDE.relative_to(_ROOT)),
    )
    violations += _signature_violations(_SPEC.read_text(encoding="utf-8"), str(_SPEC.relative_to(_ROOT)))
    assert not violations, (
        "a doc presents a fictional widget type (#2110 regression signature) — "
        "name it in plain prose, never backticked on a 'widget type' line:\n  "
        + "\n  ".join(violations)
    )


# ---------------------------------------------------------------------------
# Meta — the checkers bite on planted violations (no reliance on a one-time red)
# ---------------------------------------------------------------------------

_CANON = ["metric", "status", "progress", "text", "markdown", "table",
          "list", "link", "image", "divider", "spacer"]

_ORIGINAL_LYING_LINE = (
    "- **Widget Types** -- There are 11 supported types: `metric`, `status`, `progress`, "
    "`table`, `list`, `chart`, `text`, `badge`, `countdown`, `link`, `image`."
)
_TRUTHFUL_ANTI_EXAMPLE_ROW = (
    "| 2026-01-13 | **Dashboard widget examples**: Added complete examples for ALL 11 widget "
    "types with required field names highlighted; Added warning box about common field name "
    "mistakes (`content` not `text`, `items` not `values`, `url` not `href`) |"
)


def test_meta_signature_check_flags_the_original_lying_line_and_spares_a_truthful_one():
    flagged = _signature_violations(_ORIGINAL_LYING_LINE, "planted.md")
    assert flagged == ["planted.md:1 `chart`", "planted.md:1 `badge`", "planted.md:1 `countdown`"]
    # the reason a subset sweep was rejected: this line is true and must not fire
    assert _signature_violations(_TRUTHFUL_ANTI_EXAMPLE_ROW, "planted.md") == []
    # plain prose on a widget-type line is the sanctioned way to mention them
    assert _signature_violations(
        "Widget types: there is no chart, badge, or countdown widget.", "planted.md") == []
    # the YAML-key spelling presents the name as a type just as much as a bare token does
    assert _signature_violations(
        "Widget types: use `type: chart` for trends.", "planted.md") == ["planted.md:1 `chart`"]
    # …but a longer code span that quotes D-002's own output is truthful and must not fire
    assert _signature_violations(
        "Widget types: D-002 reads `unsupported dashboard widget type(s): 'chart' ×5 — not rendered`.",
        "planted.md") == []


def test_meta_literal_assign_rejects_missing_duplicate_and_mutated_bindings():
    with pytest.raises(_AllowlistShape, match="found 0"):
        _literal_assign("x = 1\n", "valid_types")
    with pytest.raises(_AllowlistShape, match="found 2"):
        _literal_assign("valid_types = ['a']\ndef f():\n    valid_types = ['b']\n", "valid_types")
    with pytest.raises(_AllowlistShape, match="augmented"):
        _literal_assign("valid_types = ['a']\nvalid_types += ['chart']\n", "valid_types")
    with pytest.raises(_AllowlistShape, match=r"\.append"):
        _literal_assign("def f():\n    valid_types = ['a']\n    valid_types.append('chart')\n", "valid_types")
    with pytest.raises(_AllowlistShape, match="not a tuple"):
        _literal_assign("valid_types = list(other)\n", "valid_types")
    # clean shapes: function-local, annotated, and a set literal — all in SOURCE order
    assert _literal_assign("def f():\n    valid_types = ['b', 'a']\n", "valid_types") == ["b", "a"]
    assert _literal_assign("T: tuple = ('b', 'a')\n", "T") == ["b", "a"]
    assert _literal_assign("T = {'b', 'a'}\n", "T") == ["b", "a"]


_PLANTED = {
    "agent-validation-spec": (
        _spec_allowed_types,
        "Allowed types: " + ", ".join(f"`{t}`" for t in _CANON) + ". Anything else is stripped.\n",
    ),
    "agent-guide-table": (
        _guide_widget_table,
        "### Widget Types\n\n| Type | Required Fields | Description |\n|---|---|---|\n"
        + "".join(f"| `{t}` | - | … |\n" for t in _CANON)
        + "\nThis paragraph names a `chart` widget and must never be scanned.\n\n### Next\n",
    ),
    "proxy-docstring": (
        _proxy_docstring_types,
        "    Widget types supported:\n" + "".join(f"    - {t}: …\n" for t in _CANON) + "\n    Args:\n",
    ),
    "user-docs-dynamic-dashboards": (
        _user_doc_widget_bullet,
        "- **dashboard.yaml** -- `dashboard.yaml` file.\n- **Widget Types** -- There are 11 supported types: "
        + ", ".join(f"`{t}`" for t in _CANON) + ".\n",
    ),
    "user-docs-faq": (
        _faq_widget_sentence,
        "Yes. Write a `dashboard.yaml` file. Eleven widget types are supported ("
        + ", ".join(f"`{t}`" for t in _CANON) + ") — see the docs.\n",
    ),
}


@pytest.mark.parametrize("extract, planted", list(_PLANTED.values()), ids=list(_PLANTED))
def test_meta_tier_b_extractors_read_a_planted_line_and_fail_without_their_anchor(extract, planted):
    assert extract(planted) == set(_CANON)
    with pytest.raises(_AnchorMissing, match="anchor not found"):
        extract("# A document that says nothing about widgets\n\nSome prose.\n")
