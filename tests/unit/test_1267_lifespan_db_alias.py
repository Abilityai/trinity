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


def _lifespan_surface() -> list[ast.AsyncFunctionDef]:
    """`lifespan` plus the phase helpers it awaits, in call order.

    #1028 split the former 580-line `lifespan` into ordered phase helpers, so
    the transport-startup blocks this guard is about no longer live inside
    `lifespan` itself. Following the calls keeps the guard pointed at the code
    rather than at a function name — and that matters more than usual here,
    because the split RE-INTRODUCED this very bug in a new form: `_db` was bound
    in phase 1 and used in three later helpers where it was no longer in scope,
    swallowed by the same `try/except` that made #1267 invisible.
    """
    tree = ast.parse(BACKEND_MAIN.read_text(encoding="utf-8"))
    fns = {
        n.name: n for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    lifespan = fns.get("lifespan")
    if lifespan is None:
        pytest.fail("async def lifespan(...) not found in src/backend/main.py")

    surface = [lifespan]
    for stmt in lifespan.body:
        if not isinstance(stmt, ast.Expr) or not isinstance(stmt.value, ast.Await):
            continue
        call = stmt.value.value
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name):
            helper = fns.get(call.func.id)
            if helper is not None:
                surface.append(helper)
    return surface


def _lifespan_node() -> ast.AsyncFunctionDef:
    """The `lifespan` handler itself (kept for the callers that mean only it)."""
    return _lifespan_surface()[0]


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
        for fn in _lifespan_surface()
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == method
    ]


@pytest.mark.unit
def test_lifespan_imports_db_alias() -> None:
    """lifespan must bind the DB singleton as ``_db`` — the alias the fix relies on."""
    def _binds_alias(fn: ast.AST) -> bool:
        return any(
            isinstance(node, ast.ImportFrom)
            and node.module == "database"
            and any(a.name == "db" and a.asname == "_db" for a in node.names)
            for node in ast.walk(fn)
        )

    surface = _lifespan_surface()
    assert any(_binds_alias(fn) for fn in surface), (
        "no function on the lifespan surface does `from database import db as _db` "
        "— the in-scope DB alias the Telegram/WhatsApp transport blocks reference "
        "(#1267)."
    )

    # #1028 strengthens this: after the phase split each helper is its own scope,
    # so binding the alias SOMEWHERE is no longer sufficient. Every function that
    # loads `_db` must bind it itself — the exact defect the split shipped, where
    # `_db` was imported in phase 1 and read in three later helpers.
    offenders = [
        fn.name for fn in surface
        if any(
            isinstance(n, ast.Name) and n.id == "_db" and isinstance(n.ctx, ast.Load)
            for n in ast.walk(fn)
        )
        and not _binds_alias(fn)
    ]
    assert not offenders, (
        f"{offenders} read `_db` without importing it — after the #1028 phase "
        f"split each helper is its own scope, so this is a NameError, swallowed "
        f"by the surrounding try/except exactly as in #1267."
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
    unbound = [
        n.lineno
        for fn in _lifespan_surface()
        for n in ast.walk(fn)
        if isinstance(n, ast.Name)
        and n.id == "db"
        and isinstance(n.ctx, ast.Load)
        and "db" not in _bound_names(fn)
    ]
    assert not unbound, (
        f"lifespan references unbound bare `db` at line(s) {unbound} — only `_db` "
        f"is in scope, so this raises NameError at startup (#1267)."
    )
