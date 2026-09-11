"""#1028 — `services/agent_client` is a package; same contract as its siblings.

See test_1028_git_service_package.py for the reasoning behind each property —
the two packages carry the same discipline, pinned per package because a guard
that walks only one tree is not a guard (Invariant #5's lesson, one layer up).
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_PKG = Path(__file__).resolve().parents[2] / "src" / "backend" / "services" / "agent_client"


def test_every_module_is_under_the_critical_threshold():
    oversized = {p.name: len(p.read_text().splitlines())
                 for p in _PKG.glob("*.py") if len(p.read_text().splitlines()) > 800}
    assert oversized == {}, f"over the 800-line threshold: {oversized}"


def test_the_import_surface_still_resolves():
    import services.agent_client as a

    for name in ("AgentClient", "AgentClientError", "AgentNotReachableError",
                 "AgentRequestError", "AgentCircuitOpenError",
                 "AgentConnectionDroppedError", "CircuitState",
                 "get_agent_client", "get_all_circuit_states", "reset_circuit",
                 "force_circuit_dormant", "is_circuit_failure",
                 "close_all_clients", "CIRCUIT_FAILURE_THRESHOLD"):
        assert hasattr(a, name), f"services.agent_client.{name} no longer resolves"


def test_no_function_from_import_between_package_modules():
    """`from . import circuit` is the sanctioned cross-module form; importing a
    FUNCTION freezes the binding and silently detaches monkeypatches on the
    owning module (the git_service rule)."""
    offenders = []
    for path in sorted(_PKG.glob("*.py")):
        if path.name == "__init__.py":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and node.level >= 1 and node.module:
                offenders.append(f"{path.name}: from .{node.module} import "
                                 + ", ".join(a.name for a in node.names))
    assert offenders == [], "; ".join(offenders)
