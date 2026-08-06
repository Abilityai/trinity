"""#1898 — a test module must not leave `sys.modules` stubs behind.

`test_ent183_skill_packages.py` wrote fake modules straight into `sys.modules`
at import time and left them there. That is collection-time code, and pytest
imports every test module before running a single test — so every file
collected afterwards resolved `services.settings_service` (and three siblings)
to a four-function fake, captured names from it at module scope, and kept those
bindings for the rest of the session.

Its own autouse `sys.modules` restore fixture could not help: by the time a
fixture first runs, the damage is a *binding* in another module's namespace,
which restoring the module table does not reach.

Two shapes of symptom, both looking like unrelated bugs:

  * `ImportError: cannot import name X from 'services.settings_service'
    (unknown location)` at COLLECTION, for anything importing a real name the
    stub does not define (#1855);
  * silent wrong behaviour, when the stub happens to define the name — 7
    failures in `test_1081_physical_meter.py` (#1898's own reproduction).

The fix scopes the stubs to the single import that needs them. This file guards
both halves of that: the mechanism (statically, so a rewrite cannot quietly
reintroduce a bare assignment) and the outcome (by actually running a victim
after the offender in one process).
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_TESTS = _REPO / "tests"
_OFFENDER = _TESTS / "unit" / "test_ent183_skill_packages.py"

pytestmark = pytest.mark.unit


def _module_level_sys_modules_writes(path: Path) -> list:
    """Module-level `sys.modules[...] = ...`, ignoring anything inside a
    function, a class, or a `with` block.

    The `with` exclusion is the point: installing stubs for the duration of one
    import is the fix, and a guard that banned every assignment would ban the
    fix along with the bug.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []

    def walk(nodes, *, inside_with=False):
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue  # runtime code, restored by the autouse fixture
            if isinstance(node, ast.With):
                walk(node.body, inside_with=True)
                continue
            if isinstance(node, (ast.For, ast.If, ast.Try)):
                walk(node.body, inside_with=inside_with)
                walk(getattr(node, "orelse", []), inside_with=inside_with)
                walk(getattr(node, "finalbody", []), inside_with=inside_with)
                continue
            if inside_with:
                continue
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if (
                        isinstance(target, ast.Subscript)
                        and isinstance(target.value, ast.Attribute)
                        and target.value.attr == "modules"
                    ):
                        found.append(node.lineno)

    walk(tree.body)
    return found


class TestTheMechanism:

    def test_no_stub_is_installed_at_module_scope(self):
        """The bug in one assertion: a bare `sys.modules[x] = fake` at module
        scope runs during collection and outlives the file."""
        leaks = _module_level_sys_modules_writes(_OFFENDER)
        assert not leaks, (
            f"{_OFFENDER.name} writes sys.modules at module scope "
            f"(line(s) {leaks}) — that runs during COLLECTION and poisons "
            "every file imported after it (#1898)"
        )

    def test_the_stubs_are_scoped_to_a_patch_context(self):
        """Guards the fix's shape, not just the bug's absence: the stubs must
        be installed by something that restores them, and `patch.dict` is what
        makes the `with` meaningful."""
        tree = ast.parse(_OFFENDER.read_text(encoding="utf-8"))
        withs = [n for n in ast.walk(tree) if isinstance(n, ast.With)]
        patch_dicts = [
            w for w in withs
            if any(
                isinstance(item.context_expr, ast.Call)
                and "patch" in ast.dump(item.context_expr)
                and "dict" in ast.dump(item.context_expr)
                for item in w.items
            )
        ]
        assert patch_dicts, (
            "the stubs are no longer installed inside a patch.dict context — "
            "whatever replaced it must restore sys.modules on exit (#1898)"
        )


class TestTheOutcome:
    """Statically-correct is not the claim; the claim is that a victim runs
    clean after the offender in one process."""

    @pytest.mark.parametrize("victim", [
        # Fails at COLLECTION pre-fix: imports a real `settings_service` name
        # the stub does not define (#1855's reported pair). Chosen because it
        # is the cheapest reproduction — ~2s.
        "test_ent125_resilient_system_deploy.py",
    ])
    def test_a_victim_passes_when_collected_after_the_offender(self, victim):
        result = subprocess.run(
            [sys.executable, "-m", "pytest",
             f"unit/{_OFFENDER.name}", f"unit/{victim}",
             "-q", "-p", "no:randomly", "-p", "no:warnings", "--timeout=120"],
            cwd=_TESTS,
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert result.returncode == 0, (
            f"{victim} does not survive being collected after "
            f"{_OFFENDER.name} (#1898)\n{result.stdout[-3000:]}"
        )
