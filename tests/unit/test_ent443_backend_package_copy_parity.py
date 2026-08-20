"""Every backend PACKAGE main.py imports must ship in the prod image (#1033 class).

`docker/backend/Dockerfile` globs top-level *modules* (`COPY src/backend/*.py`)
— that glob was #1033's fix, after `redis_breaker_util.py` was added, never
listed, and crash-looped the backend on boot. **Packages are still enumerated
one COPY line at a time**, so the same trap is open for every new directory:
the image builds clean, and the container dies at import with
`ModuleNotFoundError`.

It is not hypothetical. It happened twice: `client_portal` (ent#356, whose COPY
line carries a comment saying so) and `shared_sessions` (ent#443, caught by
`prod-image-smoke` on the PR that added it).

`prod-image-smoke` DOES catch it — after building the whole production image,
minutes into CI, on a signal that reads like a runtime crash rather than a
missing line in a Dockerfile. This is the same fact, asserted in milliseconds
against the source, so the next package to be added fails with a message that
names the fix.

Deliberately NOT a replacement for `prod-image-smoke`: that job proves the image
actually boots, which no static check can. This only proves the COPY list has
not fallen behind the import list.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parent.parent.parent
_BACKEND = _ROOT / "src" / "backend"
_DOCKERFILE = _ROOT / "docker" / "backend" / "Dockerfile"

# Packages that are legitimately absent from the image. Keep this minimal and
# documented — an entry here is a claim that the app never imports it at boot.
_EXEMPT = {
    # The private submodule. Optional by design: `main.py` guards its import in
    # a try/except and an OSS build has no directory at all (#1443).
    "enterprise",
    # Test-only trees that never ship.
    "tests",
}


def _top_level_packages() -> set[str]:
    """Directories under src/backend that are importable packages."""
    return {
        d.name
        for d in _BACKEND.iterdir()
        if d.is_dir()
        and (d / "__init__.py").exists()
        and not d.name.startswith((".", "__"))
    }


def _packages_imported_by_main() -> set[str]:
    """Top-level package names `main.py` imports at module scope.

    Module-scope only: a function-local import inside a lifespan handler is a
    different (and much louder) failure than an import-time one, and several are
    deliberately function-local to keep the seam swappable in tests.
    """
    tree = ast.parse((_BACKEND / "main.py").read_text())
    packages = _top_level_packages()
    found: set[str] = set()
    for node in tree.body:  # module scope only
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            root = node.module.split(".")[0]
            if root in packages:
                found.add(root)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in packages:
                    found.add(root)
    return found


def _copied_packages() -> set[str]:
    """Package directories the Dockerfile copies into /app."""
    text = _DOCKERFILE.read_text()
    return set(
        re.findall(r"^COPY\s+\.\./\.\./src/backend/([A-Za-z_][A-Za-z0-9_]*)\s",
                   text, re.MULTILINE)
    )


def test_every_package_main_imports_is_copied_into_the_image():
    imported = _packages_imported_by_main() - _EXEMPT
    copied = _copied_packages()

    missing = sorted(imported - copied)
    assert not missing, (
        "these backend packages are imported by main.py but never COPY'd into "
        f"the production image: {missing}\n"
        "The image will build and then die at import with ModuleNotFoundError "
        "(#1033's class). Add a line to docker/backend/Dockerfile:\n"
        + "\n".join(f"  COPY ../../src/backend/{p} /app/{p}/" for p in missing)
    )


def test_every_top_level_package_is_copied_into_the_image():
    """The stronger form: `main.py`'s import list is not the whole risk.

    A package pulled in only TRANSITIVELY — imported by a router or a service
    that `main.py` imports — is invisible to the test above and dies at import
    exactly the same way. There is no reason to key on `main.py` at all while
    the two sets are equal, and they are: every non-exempt package under
    `src/backend/` is copied today, so this assertion is free.

    `_EXEMPT` is the escape hatch if a package ever legitimately must not ship;
    an entry there is a claim, and the test above still holds the line for
    anything `main.py` imports directly.
    """
    missing = sorted(_top_level_packages() - _EXEMPT - _copied_packages())
    assert not missing, (
        f"these backend packages never ship in the production image: {missing}\n"
        "Add a COPY line to docker/backend/Dockerfile, or — if the package "
        "genuinely must not ship — add it to _EXEMPT with a reason:\n"
        + "\n".join(f"  COPY ../../src/backend/{p} /app/{p}/" for p in missing)
    )


def test_the_copy_list_has_no_dead_entries():
    """Backward parity: a COPY naming a directory that no longer exists is a
    build failure waiting for the next image build, and reads as intentional."""
    dead = sorted(
        p for p in _copied_packages()
        if not (_BACKEND / p).is_dir() and not (_BACKEND / f"{p}.py").exists()
    )
    assert not dead, f"Dockerfile copies paths that no longer exist: {dead}"


def test_the_guard_can_see_the_two_packages_that_actually_broke():
    """Anchor: if the parsers silently stop matching, both assertions above pass
    vacuously. `client_portal` (ent#356) and `shared_sessions` (ent#443) are the
    two real incidents, so they are the fixture."""
    imported = _packages_imported_by_main()
    copied = _copied_packages()
    for package in ("client_portal", "shared_sessions"):
        assert package in imported, f"{package} not detected as imported by main.py"
        assert package in copied, f"{package} not detected in the Dockerfile COPY list"
