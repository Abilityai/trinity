"""#2314 — the first carve out of `task_execution_service`, pinned.

`task_execution_service.py` is the #2 code-health hotspot and the only top-3 one
that had no covering refactor issue. The issue is explicit that a big-bang split
mid-#1081 is the wrong shape, so this is carve 1 of N: the six PURE readers of
an agent's response, plus the two constants they own.

These tests pin the three properties that make it a REFACTOR rather than a
rewrite — and one of them is here because the sibling extraction in #1028
shipped broken while three separate verifications said it was fine.
"""
from __future__ import annotations

import ast
import builtins
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_SVC = _ROOT / "src" / "backend" / "services" / "task_execution_service.py"
_NEW = _ROOT / "src" / "backend" / "services" / "execution_classification.py"

MOVED = [
    "_compute_context_used",
    "_is_reader_race_signature",
    "detect_unresolved_slash_command",
    "_extract_agent_error",
    "classify_switch_failure",
    "_salvage_attempt_cost",
]


def _functions(src: str) -> dict[str, str]:
    return {
        n.name: ast.unparse(n)
        for n in ast.parse(src).body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def test_every_moved_body_is_byte_identical_to_the_original():
    """A refactor claim is only worth what proves it.

    #1028 moved `lifespan`'s blocks 'verbatim', proved 518 == 518 identical
    lines, and still shipped a branch where three phases raised `NameError` —
    so 'I moved it carefully' is not evidence. This compares the parsed body of
    every moved function against the same function on `origin/dev`.
    """
    old = subprocess.run(
        ["git", "show", "origin/dev:src/backend/services/task_execution_service.py"],
        capture_output=True, text=True, cwd=_ROOT,
    ).stdout
    if not old:
        pytest.skip("origin/dev not available in this checkout")

    before, after = _functions(old), _functions(_NEW.read_text())
    for name in MOVED:
        assert name in after, f"{name} did not arrive in the new module"
        assert before[name] == after[name], f"{name} changed while being moved"


def test_no_other_function_in_the_service_was_touched():
    """The carve must not be a place where something else got 'tidied'."""
    old = subprocess.run(
        ["git", "show", "origin/dev:src/backend/services/task_execution_service.py"],
        capture_output=True, text=True, cwd=_ROOT,
    ).stdout
    if not old:
        pytest.skip("origin/dev not available in this checkout")

    before, after = _functions(old), _functions(_SVC.read_text())
    changed = [k for k in after if k in before and before[k] != after[k]]
    assert changed == [], f"unrelated edits rode along: {changed}"


def test_the_new_module_has_no_unresolved_names():
    """THE #1028 GUARD, applied to a module rather than a function.

    A moved chunk loses whatever its old module scope gave it, and the failure
    is invisible in a diff: #1028's helpers referenced `_db` and
    `message_router` — function-local imports shared through the enclosing
    scope — and every use site sat inside `try/except`, so the boot succeeded
    and two integrations were simply never wired.

    This carve hit the same class immediately: the dependency analysis walked
    the moved FUNCTIONS and missed that a moved CONSTANT
    (`_UNRESOLVED_COMMAND_RE = re.compile(...)`) needed `re`. Caught by the
    suite, not by reading. So the check covers every top-level statement, not
    just the callables.
    """
    tree = ast.parse(_NEW.read_text())

    bound: set[str] = set()
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(n.name)
        elif isinstance(n, ast.Assign):
            bound |= {t.id for t in n.targets if isinstance(t, ast.Name)}
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            bound |= {(a.asname or a.name).split(".")[0] for a in n.names}

    def locally_bound(node) -> set[str]:
        out = {a.arg for a in ast.walk(node) if isinstance(a, ast.arg)}
        for x in ast.walk(node):
            if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Store):
                out.add(x.id)
            elif isinstance(x, (ast.Import, ast.ImportFrom)):
                # function-local imports are deliberate here — one keeps a test
                # patch target live — and they DO bind the name.
                out |= {(a.asname or a.name).split(".")[0] for a in x.names}
            elif isinstance(x, ast.comprehension):
                for t in ast.walk(x.target):
                    if isinstance(t, ast.Name):
                        out.add(t.id)
        return out

    known = bound | set(dir(builtins))
    missing: set[str] = set()
    for n in tree.body:
        scope = locally_bound(n) if isinstance(
            n, (ast.FunctionDef, ast.AsyncFunctionDef)) else set()
        for x in ast.walk(n):
            if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load):
                if x.id not in known and x.id not in scope:
                    missing.add(x.id)
    assert not missing, (
        f"names the new module uses but never binds: {sorted(missing)} — "
        f"the #1028 class: a moved chunk lost something its old scope supplied"
    )


def test_the_service_still_re_exports_every_moved_name():
    """Pure refactor means no CALL SITE moves. `chat_execution_service` and six
    test modules import these from `task_execution_service` today."""
    src = _SVC.read_text()
    for name in MOVED + ["_AUTO_RETRY_MAX_TURNS", "_UNRESOLVED_COMMAND_RE"]:
        assert name in src, f"{name} is no longer reachable from the old module"
    assert "from .execution_classification import" in src


def test_the_new_module_is_a_leaf():
    """AC: the code-health dashboard reports 0 circular imports — keep it 0.

    Asserted structurally rather than by running an import-graph tool, so it
    fails in the PR that would create the cycle.
    """
    for node in ast.walk(ast.parse(_NEW.read_text())):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            target = getattr(node, "module", None) or ""
            names = ",".join(a.name for a in node.names)
            assert "task_execution_service" not in f"{target} {names}", (
                "the carve must not import back into the module it came from"
            )


def test_the_carve_actually_shrank_the_hotspot():
    """A decomposition that does not reduce the file is theatre."""
    old = subprocess.run(
        ["git", "show", "origin/dev:src/backend/services/task_execution_service.py"],
        capture_output=True, text=True, cwd=_ROOT,
    ).stdout
    if not old:
        pytest.skip("origin/dev not available in this checkout")
    assert len(_SVC.read_text().split("\n")) < len(old.split("\n"))


def test_the_effectful_sibling_stayed_behind():
    """`_alert_skill_not_found` reads like a sibling of the slash-command
    detector and is deliberately NOT here: it writes to the operator queue
    through the #1677 bounded-alert path. Pulling it in would import the queue
    into a leaf module and make this module two things instead of one."""
    # Checked against DEFINITIONS, not file text: this module's own docstring
    # explains why the function stayed behind, so a substring scan fails on its
    # own documentation — the same trap that bit the #2429 CAS guard and the
    # #2449 single-source guard earlier in this chain.
    def _defines(path):
        return {
            n.name for n in ast.parse(path.read_text()).body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
    assert "_alert_skill_not_found" not in _defines(_NEW)
    assert "_alert_skill_not_found" in _defines(_SVC)
