"""#1028 — the extracted handler services: ops fleet verbs, ops costs, public chat.

Two kinds of pin:

  * **No unresolved module-scope name.** A function moved between modules
    carries its body but not its import block, and a missing name only
    explodes on the exact runtime path that reads it — `PublicChatResponse`
    was unresolved in `public_chat_service` while 623 tests passed, because
    nothing drove the sync-success return, and `utc_now_iso` the same in the
    costs service. `py_compile` cannot catch this class; this walk does.
  * **The routers stayed thin and the gates stayed put.** The #2389
    fence-vs-gate scans read live handler source in `routers/ops.py`, so the
    `assert_admin` calls must remain in the router even though the bodies
    moved.
"""
from __future__ import annotations

import ast
import builtins
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"

_MOVED = (
    "services/fleet_ops_service.py",
    "services/ops_costs_service.py",
    "services/public_chat_service.py",
)


def _defined_names(tree: ast.AST) -> set:
    defined = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(n.name)
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                defined.add((a.asname or a.name).split(".")[0])
        if isinstance(n, ast.Assign):
            for tg in n.targets:
                for x in ast.walk(tg):
                    if isinstance(x, ast.Name):
                        defined.add(x.id)
        if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            defined.add(n.target.id)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            args = n.args
            for a in args.args + args.kwonlyargs + args.posonlyargs:
                defined.add(a.arg)
            if args.vararg: defined.add(args.vararg.arg)
            if args.kwarg: defined.add(args.kwarg.arg)
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            defined.add(n.id)
        if isinstance(n, ast.ExceptHandler) and n.name:
            defined.add(n.name)
        if isinstance(n, ast.comprehension):
            for x in ast.walk(n.target):
                if isinstance(x, ast.Name):
                    defined.add(x.id)
        if isinstance(n, (ast.With, ast.AsyncWith)):
            for item in n.items:
                if item.optional_vars:
                    for x in ast.walk(item.optional_vars):
                        if isinstance(x, ast.Name):
                            defined.add(x.id)
        if isinstance(n, (ast.For, ast.AsyncFor)):
            for x in ast.walk(n.target):
                if isinstance(x, ast.Name):
                    defined.add(x.id)
        if isinstance(n, (ast.Global, ast.Nonlocal)):
            defined.update(n.names)
    return defined


@pytest.mark.parametrize("relpath", _MOVED)
def test_no_unresolved_module_scope_names(relpath):
    tree = ast.parse((_BACKEND / relpath).read_text())
    defined = _defined_names(tree)
    unresolved = sorted({
        n.id for n in ast.walk(tree)
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
        and n.id not in defined and not hasattr(builtins, n.id)
    })
    assert unresolved == [], (
        f"{relpath} reads names it never defines or imports — the moved body "
        f"left its imports behind: {unresolved}"
    )


def test_the_services_are_under_the_critical_threshold():
    oversized = {p: n for p in _MOVED
                 if (n := len((_BACKEND / p).read_text().splitlines())) > 800}
    assert oversized == {}, f"over the 800-line threshold: {oversized}"


def test_ops_routes_keep_their_gates_in_the_router():
    """The #2389 scans read live handler source here; a gate that moved into
    the service would satisfy auth but blind those scans."""
    src = (_BACKEND / "routers" / "ops.py").read_text()
    tree = ast.parse(src)
    for name in ("get_fleet_health", "restart_fleet", "stop_fleet",
                 "emergency_stop", "get_ops_costs"):
        fn = next(n for n in tree.body
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
        body_src = ast.get_source_segment(src, fn)
        assert "assert_admin(current_user" in body_src, f"{name} lost its router-side gate"
        assert "_impl(" in body_src, f"{name} no longer delegates to a service impl"


def test_public_chat_router_is_thin():
    src = (_BACKEND / "routers" / "public.py").read_text()
    tree = ast.parse(src)
    fn = next(n for n in tree.body
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "public_chat")
    assert fn.end_lineno - fn.lineno + 1 < 40, (
        "public_chat grew back into the router — orchestration belongs in "
        "services/public_chat_service.py (#1028; it was 289 lines)"
    )
