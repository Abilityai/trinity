"""The AST scans behind test_3785_operator_queue_package.py (#3785).

Pure stdlib: reads source, never imports it (the #762 stub-leak class cannot
reach an AST walk). Underscore-prefixed so pytest never collects it. The
policy tables and every assertion stay in the test file; this module only
answers "what does the tree say".

A target is "" (the package), "<sub>" (a submodule), or an intermediate:
"@services" (the `services` package a bare `import services.x` binds),
"@routers" / "@router" (`routers` / `routers.operator_queue`, whose
`operator_queue_service` attribute is the package: test_ent815's `route.`).
"""
from __future__ import annotations

import ast
import builtins
import functools
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "src" / "backend"
TESTS = REPO / "tests"
PKG = BACKEND / "services" / "operator_queue_service"
NAME = "services.operator_queue_service"


# ---------------------------------------------------------------------------
# Tree helpers
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=None)
def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def modules() -> dict:
    """{'' for `__init__`, else the dotted submodule name: path}."""
    out = {}
    for path in sorted(PKG.rglob("*.py")):
        parts = [p for p in path.relative_to(PKG).with_suffix("").parts if p != "__init__"]
        out[".".join(parts)] = path
    return out


def submodules() -> set:
    return {m for m in modules() if m}


def import_time_statements(tree: ast.Module):
    """Module-level statements in source order, descending into if/try/with
    blocks but never into a def or class body."""
    stack = list(tree.body)
    while stack:
        node = stack.pop(0)
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        children = list(getattr(node, "body", None) or [])
        for handler in getattr(node, "handlers", None) or []:
            children.extend(handler.body)
        children += list(getattr(node, "orelse", None) or []) + list(getattr(node, "finalbody", None) or [])
        stack[0:0] = [c for c in children if isinstance(c, ast.stmt)]


def bindings(tree: ast.Module):
    """({name: line} defined at top level, {name: line} bound by a top-level import)."""
    defs, imports = {}, {}
    for node in import_time_statements(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                if a.name != "*":
                    name = a.asname or (a.name.split(".")[0] if isinstance(node, ast.Import) else a.name)
                    imports.setdefault(name, node.lineno)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defs.setdefault(node.name, node.lineno)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.For, ast.AsyncFor)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                for x in ast.walk(target):
                    if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Store):
                        defs.setdefault(x.id, node.lineno)
    return defs, imports


def reexports() -> dict:
    """{name: owner submodule} for every `from .sub import name` in `__init__`."""
    out = {}
    for node in import_time_statements(parse(modules()[""])):
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
            for a in node.names:
                out[a.asname or a.name] = node.module
    return out


_MODULE_DUNDERS = {"__name__", "__file__", "__doc__", "__package__", "__spec__", "__loader__", "__path__"}


def unresolved_names(tree: ast.Module) -> set:
    """Names read where no binding is visible — its own scope, an enclosing
    function's, the module's, or builtins. A local import in one function does
    not resolve a read in another (the test_1028_extracted_services walk was
    file-wide, which a moved function's lost import can hide behind)."""
    def bound_in(scope) -> set:
        names = set()
        if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            a = scope.args
            names.update(x.arg for x in a.args + a.kwonlyargs + a.posonlyargs + [a.vararg, a.kwarg] if x)
        for node in scope_nodes(scope):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names.update((a.asname or a.name).split(".")[0] for a in node.names)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                names.add(node.id)
            elif isinstance(node, SCOPES) and not isinstance(node, ast.Lambda):
                names.add(node.name)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                names.add(node.name)
            elif isinstance(node, (ast.Global, ast.Nonlocal)):
                names.update(node.names)
            elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
                names.add(node.name)
        return names

    module = bound_in(tree) | _MODULE_DUNDERS | {g for n in ast.walk(tree) if isinstance(n, ast.Global) for g in n.names}
    out = set()

    def visit(scope, enclosing):
        here = bound_in(scope)
        for node in scope_nodes(scope):
            if (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
                    and node.id not in here | enclosing | module and not hasattr(builtins, node.id)):
                out.add(node.id)
            if isinstance(node, SCOPES):
                visit(node, enclosing if isinstance(scope, (ast.ClassDef, ast.Module)) else enclosing | here)

    visit(tree, set())
    return out


def is_backend_module(dotted: str) -> bool:
    path = BACKEND.joinpath(*dotted.split("."))
    return path.with_suffix(".py").is_file() or (path / "__init__.py").is_file()


def external_imports(tree: ast.Module, before=None) -> set:
    out = set()
    for node in import_time_statements(tree):
        if before is not None and node.lineno >= before:
            break
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            out.add(node.module)
            out.update(f"{node.module}.{a.name}" for a in node.names)
    return out


# ---------------------------------------------------------------------------
# Package-alias resolution, per scope
# ---------------------------------------------------------------------------

SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def alias_targets(node, inside: bool = False) -> dict:
    out = {}
    if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module is None and inside:
        out.update({a.asname or a.name: a.name for a in node.names if a.name in submodules()})
    elif isinstance(node, ast.ImportFrom) and node.level == 0:
        for a in node.names:
            local = a.asname or a.name
            if node.module == "services" and a.name == "operator_queue_service":
                out[local] = ""
            elif node.module == NAME and a.name in submodules():
                out[local] = a.name
            elif node.module == "routers" and a.name == "operator_queue":
                out[local] = "@router"
    elif isinstance(node, ast.Import):
        for a in node.names:
            root = a.name.split(".")[0]
            if a.asname and a.name == NAME:
                out[a.asname] = ""
            elif a.asname and a.name.startswith(NAME + "."):
                out[a.asname] = a.name[len(NAME) + 1:]
            elif a.asname and a.name == "routers.operator_queue":
                out[a.asname] = "@router"
            elif not a.asname and root in ("services", "routers"):
                out[root] = "@" + root
    return out


def resolve(expr, aliases):
    """The package target an expression denotes, or None."""
    if isinstance(expr, ast.Name):
        return aliases.get(expr.id)
    if isinstance(expr, ast.Attribute):
        base = resolve(expr.value, aliases)
        if base is None:
            return None
        if (base, expr.attr) in (("@services", "operator_queue_service"), ("@router", "operator_queue_service")):
            return ""
        if (base, expr.attr) == ("@routers", "operator_queue"):
            return "@router"
        if not base.startswith("@"):
            sub = f"{base}.{expr.attr}" if base else expr.attr
            if sub in submodules():
                return sub
    return None


def scope_nodes(scope):
    """The nodes evaluated in one scope; a nested def's decorators and defaults
    and a nested class's bases run here, its body does not."""
    stack = list(scope.body) if not isinstance(scope, ast.Lambda) else [scope.body]
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, SCOPES):
            stack.extend(getattr(node, "decorator_list", []))
            if isinstance(node, ast.ClassDef):
                stack.extend(node.bases)
                stack.extend(k.value for k in node.keywords)
            else:
                a = node.args
                stack.extend(a.defaults)
                stack.extend(d for d in a.kw_defaults if d is not None)
                stack.extend(x.annotation for x in a.args + a.kwonlyargs + a.posonlyargs + [a.vararg, a.kwarg]
                             if x is not None and x.annotation is not None)
                if getattr(node, "returns", None) is not None:
                    stack.append(node.returns)
            continue
        stack.extend(ast.iter_child_nodes(node))


@functools.lru_cache(maxsize=None)
def scope_of(path: Path) -> dict:
    """{id(node): the def/class/module whose scope evaluates it}."""
    out = {}

    def visit(scope):
        for node in scope_nodes(scope):
            out[id(node)] = scope
            if isinstance(node, SCOPES):
                visit(node)

    visit(parse(path))
    return out


@functools.lru_cache(maxsize=None)
def scoped(path: Path):
    """[(node, {alias: target})] for every node of a file. Flow-insensitive per
    scope: a name a function binds both to the package and to anything else is
    not an alias there (test_3313's `svc` is an instance in its fixture).

    In production code a PARAMETER named like the file's package alias is the
    package passed in (ask_service's `_rate_allowed(agent_name, oqs)` reads
    `oqs.rate_limiter`); in a test a parameter is a fixture, so never."""
    inside = PKG in path.parents
    passed_in = set() if TESTS in path.parents else {
        k for n in ast.walk(parse(path)) if isinstance(n, (ast.Import, ast.ImportFrom))
        for k, v in alias_targets(n, inside).items() if v == ""}
    out = []

    def visit(scope, inherited):
        nodes = list(scope_nodes(scope))
        aliased, other = {}, set()
        if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            a = scope.args
            for x in a.args + a.kwonlyargs + a.posonlyargs + [a.vararg, a.kwarg]:
                if x is not None and x.arg in passed_in:
                    aliased[x.arg] = ""
                elif x is not None:
                    other.add(x.arg)
        for node in nodes:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                targets = alias_targets(node, inside)
                aliased.update(targets)
                other.update(n for n in ((a.asname or a.name.split(".")[0]) for a in node.names)
                             if n not in targets)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                other.add(node.id)
            elif isinstance(node, SCOPES) and not isinstance(node, ast.Lambda):
                other.add(node.name)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                other.add(node.name)
        here = {k: v for k, v in inherited.items() if k not in other and k not in aliased}
        here.update({k: v for k, v in aliased.items() if k not in other})
        for node in nodes:
            out.append((node, here))
            if isinstance(node, SCOPES):
                visit(node, inherited if isinstance(scope, ast.ClassDef) else here)

    visit(parse(path), {})
    return out


def literal_strings(path: Path, name: str, scope=None):
    """The strings a loop/comprehension variable `name` takes in `scope` (the
    def or module that reads it — never a sibling test's loop) when it iterates
    a literal tuple/list, or a module dict / dict comprehension built from one,
    as test_ent734's `_REAL`; None when that cannot be shown."""
    tree = parse(path)
    module_values = {n.targets[0].id: n.value for n in tree.body
                     if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)}

    def strings(expr):
        if isinstance(expr, (ast.Tuple, ast.List, ast.Set)):
            vals = [e.value for e in expr.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
            return set(vals) if len(vals) == len(expr.elts) else None
        if isinstance(expr, ast.Dict):
            return strings(ast.Tuple(elts=list(expr.keys)))
        if isinstance(expr, ast.DictComp) and len(expr.generators) == 1:
            gen = expr.generators[0]
            if isinstance(gen.target, ast.Name) and isinstance(expr.key, ast.Name) and expr.key.id == gen.target.id:
                return strings(gen.iter)
        if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute) and expr.func.attr in ("items", "keys"):
            return strings(expr.func.value)
        if isinstance(expr, ast.Name) and expr.id in module_values:
            return strings(module_values[expr.id])
        return None

    found = set()
    for node in (n for stmt in scope_nodes(scope or tree) for n in ast.walk(stmt)):
        if isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            target = node.target
            first = target.elts[0] if isinstance(target, ast.Tuple) and target.elts else target
            if isinstance(first, ast.Name) and first.id == name:
                vals = strings(node.iter)
                if vals is None:
                    return None
                found |= vals
    return found or None


# ---------------------------------------------------------------------------
# Who reads the package
# ---------------------------------------------------------------------------

def files_naming_the_package(root: Path):
    for path in sorted(root.rglob("*.py")):
        if PKG not in path.parents and "operator_queue" in path.read_text(encoding="utf-8"):
            yield path


@functools.lru_cache(maxsize=None)
def reads_through_package(path: Path) -> frozenset:
    """{(target, name)} that a file reads through the package or a submodule."""
    reads = set()
    for node, aliases in scoped(path):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module and (
                node.module == NAME or node.module.startswith(NAME + ".")):
            target = node.module[len(NAME) + 1:]
            reads.update((target, a.name) for a in node.names if a.name != "*")
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            target = resolve(node.value, aliases)
            if target is not None and not target.startswith("@"):
                reads.add((target, node.attr))
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr"
              and len(node.args) >= 2):
            target = resolve(node.args[0], aliases)
            if target is None or target.startswith("@"):
                continue
            arg = node.args[1]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                reads.add((target, arg.value))
            elif isinstance(arg, ast.Name):
                reads.update((target, s) for s in (literal_strings(path, arg.id, scope_of(path)[id(node)]) or ()))
    return frozenset(reads)


def imports_the_package(path: Path) -> bool:
    return bool(reads_through_package(path)) or any(
        "" in alias_targets(n).values() for n in ast.walk(parse(path)) if isinstance(n, (ast.Import, ast.ImportFrom)))


def production_readers() -> set:
    """{(target, name)} that src/backend outside the package reads."""
    return {k for path in files_naming_the_package(BACKEND) for k in reads_through_package(path)}


def inside_readers(name: str, owner: str) -> set:
    """Package modules that read `name` at call time: bare in its owner, `owner.name` elsewhere."""
    readers = set()
    for mod, path in modules().items():
        for node, aliases in scoped(path):
            if (mod == owner and isinstance(node, ast.Name) and node.id == name
                    and isinstance(node.ctx, ast.Load)):
                readers.add(mod)
            elif (mod != owner and isinstance(node, ast.Attribute) and node.attr == name
                  and resolve(node.value, aliases) == owner):
                readers.add(mod or "__init__")
    return readers


# ---------------------------------------------------------------------------
# Import time, sentinels and patch sites
# ---------------------------------------------------------------------------

def import_time_exprs(tree: ast.Module):
    """(statement line, expression) evaluated when the module is imported:
    module statements, class bodies and bases, decorators, defaults, and
    annotations unless `from __future__ import annotations` defers them."""
    lazy_annotations = any(isinstance(n, ast.ImportFrom) and n.module == "__future__"
                           and any(a.name == "annotations" for a in n.names) for n in tree.body)

    def of_stmt(stmt):
        if isinstance(stmt, ast.ClassDef):
            for e in stmt.decorator_list + stmt.bases + [k.value for k in stmt.keywords]:
                yield stmt.lineno, e
            for inner in stmt.body:
                yield from of_stmt(inner)
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            a = stmt.args
            for e in stmt.decorator_list + a.defaults + [d for d in a.kw_defaults if d is not None]:
                yield stmt.lineno, e
            if not lazy_annotations:
                for x in a.args + a.kwonlyargs + a.posonlyargs + [a.vararg, a.kwarg]:
                    if x is not None and x.annotation is not None:
                        yield stmt.lineno, x.annotation
                if stmt.returns is not None:
                    yield stmt.lineno, stmt.returns
        elif not isinstance(stmt, (ast.Import, ast.ImportFrom)):
            for child in ast.iter_child_nodes(stmt):
                if not isinstance(child, (ast.stmt, ast.excepthandler)):
                    yield stmt.lineno, child

    for stmt in import_time_statements(tree):
        yield from of_stmt(stmt)


def sentinel_defaults():
    """(module, qualname, param, sentinel) for every parameter whose default is a
    module-level `X = object()` sentinel of the package."""
    sentinels = set()
    for path in modules().values():
        for node in parse(path).body:
            if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) and not node.value.args
                    and isinstance(node.value.func, ast.Name) and node.value.func.id == "object"):
                sentinels.update(t.id for t in node.targets if isinstance(t, ast.Name))
    found = []

    def visit(mod, body, qual):
        for node in body:
            if isinstance(node, ast.ClassDef):
                visit(mod, node.body, qual + [node.name])
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                a = node.args
                positional = a.posonlyargs + a.args
                pairs = list(zip(positional[len(positional) - len(a.defaults):], a.defaults))
                pairs += [(x, d) for x, d in zip(a.kwonlyargs, a.kw_defaults) if d is not None]
                for arg, default in pairs:
                    name = getattr(default, "id", None) or getattr(default, "attr", None)
                    if name in sentinels:
                        found.append((mod, ".".join(qual + [node.name]), arg.arg, name))

    for mod, path in modules().items():
        visit(mod, parse(path).body, [])
    return found


def patch_sites(path: Path):
    """(line, target, name) for every monkeypatch/mock patch of a package
    attribute, positional or keyword, `patch` under any local name included;
    `name` is None when it cannot be resolved."""
    patch_names = {"patch"} | {a.asname for n in ast.walk(parse(path)) if isinstance(n, ast.ImportFrom)
                               and n.module in ("unittest.mock", "mock") for a in n.names
                               if a.name == "patch" and a.asname}
    for node, aliases in scoped(path):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        attr = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
        if isinstance(f, ast.Name) and f.id in patch_names:
            attr = "patch"
        on_object = attr == "object" and isinstance(f, ast.Attribute) and (
            getattr(f.value, "attr", None) == "patch" or getattr(f.value, "id", None) in patch_names)
        kw = {k.arg: k.value for k in node.keywords if k.arg}
        first = node.args[0] if node.args else kw.get("target")
        second = node.args[1] if len(node.args) > 1 else kw.get("name", kw.get("attribute"))
        dotted = first.value if isinstance(first, ast.Constant) and isinstance(first.value, str) else None
        if (attr in ("setattr", "delattr") or on_object) and dotted is None:
            target = resolve(first, aliases) if first is not None else None
            if target is None or target.startswith("@") or second is None:
                continue
            arg = second
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                yield node.lineno, target, arg.value
            elif isinstance(arg, ast.Name) and (names := literal_strings(path, arg.id, scope_of(path)[id(node)])):
                yield from ((node.lineno, target, n) for n in sorted(names))
            else:
                yield node.lineno, target, None
        elif attr in ("setattr", "delattr", "patch") and dotted and dotted.startswith(NAME + "."):
            rest, target = dotted[len(NAME) + 1:].split("."), ""
            while len(rest) > 1 and ".".join(filter(None, [target, rest[0]])) in submodules():
                target = ".".join(filter(None, [target, rest.pop(0)]))
            yield node.lineno, target, ".".join(rest)
