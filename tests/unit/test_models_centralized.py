"""Static guard: Pydantic request/response models live in models.py (#654).

Architectural Invariant #14 — "Pydantic Models Centralized in ``models.py``" —
governs the **router** layer: a ``class X(BaseModel)`` must not be defined under
``src/backend/routers/``. The API contract stays in one place, so a reviewer can
read every request/response shape without spelunking 32 routers.

Scope (Codex #5): INV-14 as fingerprinted covers router models only. Two model
homes are **intentionally separate** and out of scope here:
  - ``db_models.py``       — DB-row / persistence models (a distinct layer).
  - ``adapters/base.py``   — the ChannelAdapter ABC's normalized message models.

Allowlist: exactly one router model is exempt — ``canary.py::RunCycleRequest`` —
because it evaluates ``INVARIANTS`` (from the ``canary`` library) in a
``Field(description=...)`` at *class-definition time*, and the ``canary`` library
imports ``TaskExecutionStatus`` back from ``models`` (``canary/snapshot.py``).
Relocating it would force ``models.py`` to ``from canary import INVARIANTS`` —
inverting the dependency direction of a module that is meant to be a low-level
leaf everything else imports *from*. Keeping it in the router is the correct
home; this single, documented exception is enforced (not a free pass).

Lives in tests/unit/ (not tests/) so it runs as a pure static check without the
backend-connection autouse fixtures the integration suite carries.
"""
import ast
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
_ROUTERS = _BACKEND / "routers"

# (filename, classname) pairs allowed to remain a BaseModel under routers/.
# Keep this MINIMAL and documented — every entry needs a verified reason above.
_ALLOWLIST: set[tuple[str, str]] = {
    ("canary.py", "RunCycleRequest"),
}


def _is_basemodel(node: ast.ClassDef) -> bool:
    for base in node.bases:
        if isinstance(base, ast.Name) and base.id == "BaseModel":
            return True
        if isinstance(base, ast.Attribute) and base.attr == "BaseModel":
            return True
    return False


def _router_basemodels() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for path in sorted(_ROUTERS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and _is_basemodel(node):
                found.add((path.name, node.name))
    return found


def test_no_basemodels_outside_allowlist_in_routers():
    """Routers define no Pydantic models except the documented allowlist."""
    offenders = sorted(_router_basemodels() - _ALLOWLIST)
    assert not offenders, (
        "These BaseModel subclasses are defined under src/backend/routers/ but "
        "belong in src/backend/models.py (Architectural Invariant #14, #654). "
        "Move each class VERBATIM into its domain section in models.py and import "
        "it back where the router still references it:\n  "
        + "\n  ".join(f"{f}::{c}" for f, c in offenders)
    )


def test_allowlist_entries_still_exist():
    """Guard against a stale allowlist — every exemption must still be real."""
    present = _router_basemodels()
    stale = sorted(_ALLOWLIST - present)
    assert not stale, (
        "These allowlisted router models no longer exist — drop them from "
        "_ALLOWLIST so the guard stays honest:\n  "
        + "\n  ".join(f"{f}::{c}" for f, c in stale)
    )


def test_models_py_present():
    """models.py must exist as a flat module (conftest preload + Dockerfile
    COPY *.py both depend on it; a package conversion would break both)."""
    assert (_BACKEND / "models.py").is_file()


def test_guard_detects_planted_router_model(tmp_path):
    """The guard must FAIL on a new BaseModel planted in a routers/ file."""
    fake_routers = tmp_path / "routers"
    fake_routers.mkdir()
    (fake_routers / "planted.py").write_text(
        "from pydantic import BaseModel\n\n\nclass PlantedRequest(BaseModel):\n"
        "    field: str\n",
        encoding="utf-8",
    )
    found = set()
    for path in fake_routers.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and _is_basemodel(node):
                found.add((path.name, node.name))
    assert ("planted.py", "PlantedRequest") in found
