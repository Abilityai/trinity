"""Regression tests for #592: every agent-creation endpoint must require
the ``creator`` role.

Pentest finding AISEC-H1 flagged ``POST /api/agents/deploy-local`` as
bypassing the role gate. The direct gate has been in place since #150
(2026-04-05), but auditing per AC #2 turned up a real bypass on
``POST /api/systems/deploy`` — the system manifest deployment also
calls ``create_agent_internal`` and was using ``Depends(get_current_user)``
without a role check, so any authenticated ``user``-role account could
spawn a fleet of agents through that path.

These tests walk the source AST of the router files and assert that
every route which ultimately creates agents wires
``Depends(require_role("creator"))``. AST-level so the check is fast,
stable across formatting changes, and doesn't require a live backend or
role tokens — and crucially, it fires the moment someone removes the
dependency.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"


def _route_decorator_path(decorator: ast.AST) -> str | None:
    """Extract the path arg from `@router.post("/foo")` style decorators."""
    if not isinstance(decorator, ast.Call):
        return None
    func = decorator.func
    # @router.post("/foo")
    if not (isinstance(func, ast.Attribute) and func.attr in {"post", "put"}):
        return None
    if not decorator.args:
        return None
    arg = decorator.args[0]
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    return None


def _has_require_role_creator_dep(func_def: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True if the function has a parameter defaulting to
    ``Depends(require_role("creator"))``.

    Matches both keyword-only and regular argument styles.
    """
    defaults = list(func_def.args.defaults) + list(func_def.args.kw_defaults)
    for default in defaults:
        if default is None:
            continue
        # We're looking for: Depends(require_role("creator"))
        if not isinstance(default, ast.Call):
            continue
        outer = default.func
        if not (isinstance(outer, ast.Name) and outer.id == "Depends"):
            continue
        if not default.args:
            continue
        inner = default.args[0]
        if not isinstance(inner, ast.Call):
            continue
        if not (isinstance(inner.func, ast.Name) and inner.func.id == "require_role"):
            continue
        if not inner.args:
            continue
        role_arg = inner.args[0]
        if isinstance(role_arg, ast.Constant) and role_arg.value == "creator":
            return True
    return False


def _find_route(source_path: Path, route_path: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    """Locate the FastAPI handler whose decorator matches ``route_path``."""
    tree = ast.parse(source_path.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if _route_decorator_path(dec) == route_path:
                return node
    raise AssertionError(f"Route {route_path!r} not found in {source_path}")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestAgentCreationRoleGates:
    """Every entry point that ultimately calls ``create_agent_internal``
    must require the ``creator`` role."""

    def test_post_agents_requires_creator(self):
        """POST /api/agents — the canonical agent-creation endpoint."""
        agents_py = _BACKEND / "routers" / "agents.py"
        handler = _find_route(agents_py, "")  # mounted at /api/agents (prefix), route ""
        assert _has_require_role_creator_dep(handler), (
            "POST /api/agents must use Depends(require_role(\"creator\")) "
            "(see Architectural Invariant #8)"
        )

    def test_post_agents_deploy_local_requires_creator(self):
        """POST /api/agents/deploy-local — the AISEC-H1 finding's named target."""
        agents_py = _BACKEND / "routers" / "agents.py"
        handler = _find_route(agents_py, "/deploy-local")
        assert _has_require_role_creator_dep(handler), (
            "POST /api/agents/deploy-local must use "
            "Depends(require_role(\"creator\")) — bypass found in AISEC-H1"
        )

    def test_post_systems_deploy_requires_creator(self):
        """POST /api/systems/deploy — uncovered during the AC #2 audit.

        This route calls ``create_agent_internal`` to spawn one or more
        agents from a YAML manifest, so it is at least as privileged as
        single-agent creation and must enforce the same gate.
        """
        systems_py = _BACKEND / "routers" / "systems.py"
        handler = _find_route(systems_py, "/deploy")
        assert _has_require_role_creator_dep(handler), (
            "POST /api/systems/deploy must use "
            "Depends(require_role(\"creator\")) — fleet-spawning bypass"
        )
