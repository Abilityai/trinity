"""
Issue #1267 — lifespan transport startup referenced a bare `db` (only `_db` is
in scope) → NameError + misleading "Error starting Telegram/WhatsApp transport"
on every boot.

`lifespan()` binds the DB singleton as ``from database import db as _db``, but
the Telegram and WhatsApp transport-startup blocks fetched their bindings via a
bare ``db.get_all_*_bindings()``. With only ``_db`` in scope, each raised
``NameError: name 'db' is not defined``, swallowed by the surrounding
``try/except`` and surfaced as a misleading ``ERROR main: Error starting
<Telegram|WhatsApp> transport``. The Telegram NameError additionally skipped the
per-binding webhook reconciliation loop (it sits *after* the failing line), so
webhooks were not re-registered on startup.

Fix: use the in-scope ``_db`` alias in both blocks.

These are AST checks on ``src/backend/main.py`` — unit tests never import
``main`` (too heavy; see tests/unit/test_858_dockerfile_unbuffered.py for the
precedent).

True unit test — no Docker, no backend.

Issue: https://github.com/Abilityai/trinity/issues/1267
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

# tests/unit/ lives two levels under the repo root.
REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_MAIN = REPO_ROOT / "src" / "backend" / "main.py"


def _lifespan_node() -> ast.AsyncFunctionDef:
    """Return main.py's ``lifespan`` async handler node."""
    tree = ast.parse(BACKEND_MAIN.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan":
            return node
    pytest.fail("async def lifespan(...) not found in src/backend/main.py")


def _bound_names(fn: ast.AST) -> set[str]:
    """Names bound anywhere in the function (assignments, imports, params).

    Per Python scoping, a name assigned anywhere in a function body is local to
    that function; this is the set a bare ``Load`` of the name resolves against.
    """
    bound: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)
        elif isinstance(node, ast.alias):  # `import x` / `from m import x as y`
            bound.add((node.asname or node.name).split(".")[0])
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
    return bound


def _attr_receiver_name(call: ast.Call) -> str | None:
    """For ``obj.method(...)``, return ``obj`` when it is a bare Name; else None."""
    func = call.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return func.value.id
    return None


def _binding_fetches(method: str) -> list[ast.Call]:
    """All ``<recv>.<method>(...)`` calls inside lifespan."""
    return [
        node
        for node in ast.walk(_lifespan_node())
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == method
    ]


@pytest.mark.unit
def test_lifespan_imports_db_alias() -> None:
    """lifespan must bind the DB singleton as ``_db`` — the alias the fix relies on."""
    has_alias = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "database"
        and any(a.name == "db" and a.asname == "_db" for a in node.names)
        for node in ast.walk(_lifespan_node())
    )
    assert has_alias, (
        "lifespan must `from database import db as _db` — the in-scope DB alias "
        "the Telegram/WhatsApp transport blocks reference (#1267)."
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "method", ["get_all_telegram_bindings", "get_all_whatsapp_bindings"]
)
def test_lifespan_transport_binding_fetch_uses_db_alias(method: str) -> None:
    """The Telegram/WhatsApp startup blocks must fetch bindings via ``_db``, not a
    bare ``db`` (unbound in lifespan → NameError on every boot, the #1267 bug)."""
    calls = _binding_fetches(method)
    assert calls, (
        f"expected a `_db.{method}(...)` call in lifespan transport startup; "
        f"none found — did the block move or get removed? (#1267)"
    )
    bad = [c for c in calls if _attr_receiver_name(c) == "db"]
    assert not bad, (
        f"lifespan calls bare `db.{method}()` at line(s) {[c.lineno for c in bad]} "
        f"— only `_db` is in scope, so this is a NameError on every boot (#1267). "
        f"Use `_db.{method}()`."
    )
    assert all(_attr_receiver_name(c) == "_db" for c in calls), (
        f"`{method}` must be called on the `_db` alias in lifespan (#1267)."
    )


@pytest.mark.unit
def test_lifespan_has_no_unbound_bare_db() -> None:
    """General guard for the #1267 bug class: lifespan must not ``Load`` a bare
    ``db`` name that is never bound in its scope (only ``_db`` is imported)."""
    node = _lifespan_node()
    bound = _bound_names(node)
    unbound = [
        n.lineno
        for n in ast.walk(node)
        if isinstance(n, ast.Name)
        and n.id == "db"
        and isinstance(n.ctx, ast.Load)
        and "db" not in bound
    ]
    assert not unbound, (
        f"lifespan references unbound bare `db` at line(s) {unbound} — only `_db` "
        f"is in scope, so this raises NameError at startup (#1267)."
    )
