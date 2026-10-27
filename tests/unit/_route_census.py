"""The route census walker and its reviewed tables (#2996, Invariant #8).

Every HTTP route in the OSS backend — every method, GETs included — must resolve
to exactly one policy class. This module is the one place that answers "which
class is this route in"; `test_2996_human_only_routes.py` (the AST side) and
`test_2996_route_census_runtime.py` (the live route table) both read through it.

Pure stdlib, imports nothing from `src/` (read source, never import it — the
#762 stub-leak class cannot reach an AST walk). Underscore-prefixed so pytest
never collects it.

Identity of a route is `"<definition relpath>::<qualname>"`, relative to
`src/backend/`: stable under prefix and line drift. `add_api_route` targets are
resolved to the file that DEFINES the handler, so the key matches the runtime
`endpoint.__module__` + `__qualname__`.

Policy classes
--------------
Recognised from code (a gate name counts only when imported, unaliased, from
`dependencies` — or `get_portal_principal` from `client_portal.portal_auth` —
and not redefined in the file):

* ``interactive`` / ``person`` — ``Depends(require_interactive|require_person)``
  in the signature or the decorator's ``dependencies=[...]``; or
  ``reject_non_interactive_principal`` / ``assert_person`` /
  ``reject_non_person_principal`` as the handler's FIRST statement (after the
  docstring), called on its principal parameter (one whose default is
  ``Depends(...)``). A call nested in ``if``/``try``, placed after another
  statement, or made on another name does not count.
* ``admin_tier`` — the same recognition for ``require_admin`` and for
  ``assert_admin`` without ``allow_scopes=``. Counted and printed, not listed:
  the admin tier's own policy (which admits `trinity-system` by #2323 design) is
  guarded by test_293 / test_2323.
* ``person_or_grant`` — ``Depends(require_person_or_capability(cap))``
  (trinity-enterprise#164): a person, or an agent holding the named
  self-change grant; every other machine principal is refused as by
  ``require_person``. The grant itself is admin-and-interactive only.
* ``admin_widened`` — ``Depends(require_admin_allowing(...))`` or
  ``assert_admin(..., allow_scopes=...)``. Each needs an ``ADMIN_WIDENED`` entry:
  a widening grants a scope and is reviewed per route.
* ``portal`` — ``Depends(get_portal_principal)``; the portal principal's own
  policy (#2198) owns it.

A route carrying several recognised gates takes the strictest
(``interactive > person > person_or_grant > admin_widened > admin_tier > portal``).

Listed here, per route, with the registered ``"METHOD /full/path"`` pinned and
checked against the live app by the runtime test:

* ``agent_callable_self`` — an agent key may call it, for its OWN agent only
  (trinity-enterprise#840). Recognised AND listed, like ``admin_widened``: the
  handler's first statement (after the docstring) is
  ``assert_own_agent(<principal>, <agent>, ...)`` — imported unaliased from
  ``dependencies``, positional, ``<principal>`` a ``Depends(get_current_user)`` /
  ``CurrentUser`` parameter, ``<agent>`` the ``name`` / ``agent_name`` parameter
  named in the path of every route decorator — or the signature takes
  ``Depends(get_self_acting_agent)``. Each entry says why the route is a USE,
  not a grant: a self-gated route with no entry fails, and an entry whose route
  carries no recognised gate fails. That forces the review; it does not do it.
  A self-scoped grant that someone lists passes every check, so the reviewed
  reason is the guard, as it was for the old ``AGENT_CALLABLE`` table.
* ``agent_callable_any`` — a reviewed use an agent key may make WITHOUT a
  recognised self-gate, with a reason AND a written answer to "why may an agent
  other than the one in the path reach this".
* ``own_auth`` — authenticates itself (internal secret, webhook token, payment,
  inline MCP auth) or is unauthenticated by design.
* ``delegated`` — the check lives in ``services/``; names the behavioural test
  that proves it, and the census asserts that test exists.

The private submodule reuses ``walk`` / ``classify`` / ``count_failures`` /
``hand_copied_self_gates`` over its own tree with its own tables: keep their
signatures stable, or move both sides together.

Everything else must sit in the frozen baseline
(``fixtures/human_only_route_baseline.json``) — ``unclassified_at_freeze``. The
baseline is written ONCE by hand (``python tests/unit/_route_census.py
--freeze``) and only ever shrinks; the test never regenerates it.

Router-level ``APIRouter(dependencies=...)`` / ``include_router(dependencies=...)``
are NOT recognised (none exist today): such routes stay unclassified, which fails
closed. WebSocket routes are outside the HTTP census but pinned as a literal set.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "src" / "backend"
BASELINE_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "human_only_route_baseline.json"
)

# Top-level directories under src/backend/ the census does not walk.
# `enterprise/` is the private submodule and carries its own census (#1677).
EXCLUDED_TOP = frozenset({"enterprise", "tests", "__pycache__"})

HTTP_DECORATORS = {
    "get": ("GET",),
    "post": ("POST",),
    "put": ("PUT",),
    "patch": ("PATCH",),
    "delete": ("DELETE",),
    "head": ("HEAD",),
    "options": ("OPTIONS",),
    "api_route": None,  # methods from the `methods=` keyword
}

CODE_CLASSES = (
    "interactive",
    "person",
    "person_or_grant",
    "admin_widened",
    "admin_tier",
    "portal",
)  # strictest first
LISTED_CLASSES = (
    "agent_callable_self",
    "agent_callable_any",
    "own_auth",
    "delegated",
    "unclassified_at_freeze",
)

# Gate names and the module each must be imported from.
_DEPENDS_GATES = {
    ("dependencies", "require_interactive"): "interactive",
    ("dependencies", "require_person"): "person",
    ("dependencies", "require_admin"): "admin_tier",
    ("client_portal.portal_auth", "get_portal_principal"): "portal",
}
_DEPENDS_FACTORIES = {
    ("dependencies", "require_admin_allowing"): "admin_widened",
    ("dependencies", "require_person_or_capability"): "person_or_grant",
}
_IMPERATIVE_GATES = {
    ("dependencies", "reject_non_interactive_principal"): "interactive",
    ("dependencies", "assert_person"): "person",
    ("dependencies", "reject_non_person_principal"): "person",
    (
        "dependencies",
        "assert_admin",
    ): "admin_tier",  # admin_widened when allow_scopes= is passed
}

# The agent-key self-gate (trinity-enterprise#840). The first-statement call,
# the dependency that fences the path `{name}` to the key itself, the principal
# the call must be made on, and the path parameters that name the agent
# (`dependencies._path_agent` reads the same two).
_SELF_GATE_CALL = ("dependencies", "assert_own_agent")
_SELF_GATE_DEPENDS = ("dependencies", "get_self_acting_agent")
_PRINCIPAL_DEPENDS = ("dependencies", "get_current_user")
_PRINCIPAL_ALIAS = ("dependencies", "CurrentUser")
_AGENT_PARAMS = ("name", "agent_name")


@dataclass(frozen=True)
class SelfGate:
    """A recognised self-gate. `kind` is `assert_own_agent` (first statement:
    `principal` and `agent` are its two arguments) or `get_self_acting_agent`
    (a dependency: `agent` is the parameter it fills, `principal` is None).
    `refusal` is True when the call passes its own exception factory."""

    kind: str
    principal: Optional[str]
    agent: str
    refusal: bool = False


@dataclass
class Route:
    key: str
    relpath: str
    qualname: str
    lineno: int
    methods: Tuple[str, ...]
    gates: Set[str] = field(default_factory=set)
    problems: List[str] = field(default_factory=list)
    self_gate: Optional[SelfGate] = None

    @property
    def code_class(self) -> Optional[str]:
        for cls in CODE_CLASSES:
            if cls in self.gates:
                return cls
        return None


@dataclass
class Census:
    routes: Dict[str, Route]
    websockets: Set[str]
    problems: List[str]  # walker-level problems (duplicates, unresolvable targets)


# --------------------------------------------------------------------------- #
# Source walking
# --------------------------------------------------------------------------- #


def backend_files(root: Path = BACKEND) -> List[Path]:
    """Every `*.py` under `root` via rglob (a new package is walked with no
    edit here — the Invariant #1 `glob` blindness), minus EXCLUDED_TOP."""
    out = []
    for p in sorted(root.rglob("*.py")):
        rel = p.relative_to(root)
        if rel.parts[0] in EXCLUDED_TOP or "__pycache__" in rel.parts:
            continue
        out.append(p)
    return out


def _module_of(relpath: str) -> str:
    parts = relpath[:-3].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _package_of(relpath: str) -> str:
    mod = _module_of(relpath)
    if relpath.endswith("__init__.py"):
        return mod
    return mod.rpartition(".")[0]


def _resolve_relative(relpath: str, module: Optional[str], level: int) -> str:
    if level == 0:
        return module or ""
    pkg = _package_of(relpath).split(".") if _package_of(relpath) else []
    if level > 1:
        pkg = pkg[: len(pkg) - (level - 1)]
    base = ".".join(pkg)
    if module:
        return f"{base}.{module}" if base else module
    return base


class _FileIndex:
    """Module-level imports and definitions of one source file."""

    def __init__(self, relpath: str, tree: ast.Module):
        self.relpath = relpath
        self.tree = tree
        self.names: Dict[str, Tuple[str, str]] = {}  # local -> (module, original)
        self.modules: Dict[str, str] = {}  # local -> dotted module
        self.local_defs: Set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.ImportFrom):
                mod = _resolve_relative(relpath, node.module, node.level)
                for alias in node.names:
                    local = alias.asname or alias.name
                    # `from pkg import sub` may name a submodule; record both.
                    self.names[local] = (mod, alias.name)
                    self.modules[local] = f"{mod}.{alias.name}" if mod else alias.name
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    self.modules[alias.asname or alias.name.split(".")[0]] = (
                        alias.name if alias.asname else alias.name.split(".")[0]
                    )
            elif isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                self.local_defs.add(node.name)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                for t in targets:
                    if isinstance(t, ast.Name):
                        self.local_defs.add(t.id)

    def resolve(self, expr: ast.AST) -> Optional[Tuple[str, str]]:
        """(module, name) an expression refers to, if it is an unaliased,
        un-shadowed imported name or an attribute of an imported module."""
        if isinstance(expr, ast.Name):
            if expr.id in self.local_defs or expr.id not in self.names:
                return None
            mod, orig = self.names[expr.id]
            if orig != expr.id:  # aliased (`import x as require_person`)
                return None
            return mod, orig
        if isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name):
            mod = self.modules.get(expr.value.id)
            if mod and expr.value.id not in self.local_defs:
                return mod, expr.attr
        return None


def _is_depends(call: ast.AST) -> bool:
    if not isinstance(call, ast.Call) or not call.args:
        return False
    f = call.func
    name = (
        f.id
        if isinstance(f, ast.Name)
        else (f.attr if isinstance(f, ast.Attribute) else None)
    )
    return name in ("Depends", "Security")


def _depends_gate(idx: _FileIndex, call: ast.Call) -> Optional[str]:
    target = call.args[0]
    if isinstance(target, ast.Call):
        ref = idx.resolve(target.func)
        return _DEPENDS_FACTORIES.get(ref) if ref else None
    ref = idx.resolve(target)
    return _DEPENDS_GATES.get(ref) if ref else None


def _principal_params(fn: ast.AST) -> Set[str]:
    args = fn.args
    positional = args.posonlyargs + args.args
    out = set()
    for a, d in zip(positional[len(positional) - len(args.defaults) :], args.defaults):
        if _is_depends(d):
            out.add(a.arg)
    for a, d in zip(args.kwonlyargs, args.kw_defaults):
        if d is not None and _is_depends(d):
            out.add(a.arg)
    return out


def _signature_depends(fn: ast.AST) -> Iterable[ast.Call]:
    args = fn.args
    for d in list(args.defaults) + [d for d in args.kw_defaults if d is not None]:
        if _is_depends(d):
            yield d


def _first_statement(fn: ast.AST) -> Optional[ast.stmt]:
    body = list(fn.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(getattr(body[0], "value", None), ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return body[0] if body else None


def _imperative_gate(idx: _FileIndex, fn: ast.AST) -> Optional[str]:
    stmt = _first_statement(fn)
    if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)):
        return None
    call = stmt.value
    ref = idx.resolve(call.func)
    if ref not in _IMPERATIVE_GATES:
        return None
    principals = _principal_params(fn)
    if not (
        call.args
        and isinstance(call.args[0], ast.Name)
        and call.args[0].id in principals
    ):
        return None
    cls = _IMPERATIVE_GATES[ref]
    if cls == "admin_tier" and any(k.arg == "allow_scopes" for k in call.keywords):
        return "admin_widened"
    return cls


_PATH_PARAM = re.compile(r"\{(\w+)(?::[^}]*)?\}")


def _literal_path(dec: ast.Call) -> Optional[str]:
    """The path a route decorator registers, when it is a string literal."""
    arg = dec.args[0] if dec.args else next(
        (k.value for k in dec.keywords if k.arg == "path"), None
    )
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    return None


def _defaulted_params(fn: ast.AST) -> List[Tuple[ast.arg, ast.AST]]:
    args = fn.args
    positional = args.posonlyargs + args.args
    pairs = list(zip(positional[len(positional) - len(args.defaults) :], args.defaults))
    pairs += [(a, d) for a, d in zip(args.kwonlyargs, args.kw_defaults) if d is not None]
    return pairs


def _param_names(fn: ast.AST) -> Set[str]:
    args = fn.args
    return {a.arg for a in args.posonlyargs + args.args + args.kwonlyargs}


def _current_user_params(idx: _FileIndex, fn: ast.AST) -> Set[str]:
    """Parameters that receive the `get_current_user` principal: defaulted
    `Depends(get_current_user)`, or annotated with the `CurrentUser` alias —
    both resolved unaliased from `dependencies`. Any other `Depends` parameter
    is not a principal: `assert_own_agent(name, name)` on a
    `Depends(get_self_acting_agent)` value would read no agent and pass."""
    out = {
        a.arg
        for a, d in _defaulted_params(fn)
        if _is_depends(d) and idx.resolve(d.args[0]) == _PRINCIPAL_DEPENDS
    }
    args = fn.args
    for a in args.posonlyargs + args.args + args.kwonlyargs:
        if a.annotation is not None and idx.resolve(a.annotation) == _PRINCIPAL_ALIAS:
            out.add(a.arg)
    return out


def _self_gate(idx: _FileIndex, fn: ast.AST, decorators: List[ast.Call]) -> Optional[SelfGate]:
    """The recognised agent-key self-gate of a route handler, or None.

    Fails closed: a route with no literal decorator path (an `add_api_route`
    target, a computed path) is never recognised, because the path is what
    proves the gate compares the agent the request names; nor is a path that
    names BOTH `{name}` and `{agent_name}`, where the gate could fence one agent
    while the handler acts on the other (`dependencies._path_agent`, which the
    capability fences read, prefers `agent_name`)."""
    paths = [_literal_path(d) for d in decorators]
    if not paths or any(p is None for p in paths):
        return None
    if any(set(_AGENT_PARAMS) <= set(_PATH_PARAM.findall(p)) for p in paths):
        return None

    def in_every_path(param: str) -> bool:
        return all(param in _PATH_PARAM.findall(p) for p in paths)

    # `name: str = Depends(get_self_acting_agent)` — it reads the path `{name}`.
    for a, d in _defaulted_params(fn):
        if _is_depends(d) and idx.resolve(d.args[0]) == _SELF_GATE_DEPENDS and in_every_path("name"):
            return SelfGate("get_self_acting_agent", None, a.arg)

    stmt = _first_statement(fn)
    if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)):
        return None
    call = stmt.value
    if idx.resolve(call.func) != _SELF_GATE_CALL or len(call.args) < 2:
        return None
    principal, agent = call.args[0], call.args[1]
    if not (isinstance(principal, ast.Name) and principal.id in _current_user_params(idx, fn)):
        return None
    if not (
        isinstance(agent, ast.Name)
        and agent.id in _AGENT_PARAMS
        and agent.id in _param_names(fn)
        and in_every_path(agent.id)
    ):
        return None
    return SelfGate(
        "assert_own_agent",
        principal.id,
        agent.id,
        refusal=any(k.arg == "refusal" for k in call.keywords),
    )


# --------------------------------------------------------------------------- #
# Hand-copied self-gates (the ratchet behind AGENT_CALLABLE_SELF)
# --------------------------------------------------------------------------- #


_PRINCIPAL_NAMES = ("current_user", "user", "principal", "caller")
_REFUSAL_STATUS = {401, 403, 404}
_REFUSAL_STATUS_NAMES = {"HTTP_401_UNAUTHORIZED", "HTTP_403_FORBIDDEN", "HTTP_404_NOT_FOUND"}


def _refuses(body: List[ast.stmt]) -> bool:
    """A branch that refuses: any `raise` (an `HTTPException`, a factory's
    result, a prebuilt exception), or a `return` of a call carrying a
    401/403/404 `status_code` (a hand-built `JSONResponse`)."""
    for stmt in body:
        for n in ast.walk(stmt):
            if isinstance(n, ast.Raise):
                return True
            if isinstance(n, ast.Return) and isinstance(n.value, ast.Call):
                for k in n.value.keywords:
                    v = k.value
                    if k.arg == "status_code" and (
                        (isinstance(v, ast.Constant) and v.value in _REFUSAL_STATUS)
                        or (isinstance(v, ast.Attribute) and v.attr in _REFUSAL_STATUS_NAMES)
                    ):
                        return True
    return False


def _annotation_names(ann: Optional[ast.AST]) -> Set[str]:
    """Every name an annotation mentions — through `Optional[...]`,
    `Annotated[...]`, a dotted path or a string annotation."""
    if ann is None:
        return set()
    if isinstance(ann, ast.Constant) and isinstance(ann.value, str):
        try:
            ann = ast.parse(ann.value, mode="eval").body
        except SyntaxError:
            return set()
    return {n.id for n in ast.walk(ann) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(ann) if isinstance(n, ast.Attribute)
    }


def _principal_like_params(fn: ast.AST) -> Set[str]:
    """Parameters that hold a principal: one whose annotation mentions `User`
    or `CurrentUser` (bare, dotted, `Optional`, `Annotated`, a string), one
    defaulted `Depends(...)`, or an unannotated one named like a principal
    (`current_user`, `user`, `principal`, `caller`). A helper such as
    `_self_gate(current_user, name)` is caught as well as a handler."""
    out = {a.arg for a, d in _defaulted_params(fn) if _is_depends(d)}
    args = fn.args
    for a in args.posonlyargs + args.args + args.kwonlyargs:
        if _annotation_names(a.annotation) & {"User", "CurrentUser"}:
            out.add(a.arg)
        elif a.annotation is None and a.arg in _PRINCIPAL_NAMES:
            out.add(a.arg)
    return out


def _agent_name_of(expr: ast.AST, principals: Set[str], aliases: Set[str]) -> bool:
    """Is `expr` a principal's agent name, in any of the spellings the hand
    copies used: `<p>.agent_name`, `getattr(<p>, "agent_name", ...)`,
    `agent_principal_name(<p>)`, or a local bound from one of those — also as
    an operand of `or` / `and` or a branch of `… if … else …`, which can
    evaluate to it (`(current_user.agent_name or name) != name`)."""
    if isinstance(expr, ast.Name):
        return expr.id in aliases
    if isinstance(expr, ast.BoolOp):
        return any(_agent_name_of(v, principals, aliases) for v in expr.values)
    if isinstance(expr, ast.IfExp):
        return any(_agent_name_of(v, principals, aliases) for v in (expr.body, expr.orelse))
    if isinstance(expr, ast.Attribute):
        return (
            expr.attr == "agent_name"
            and isinstance(expr.value, ast.Name)
            and expr.value.id in principals
        )
    if isinstance(expr, ast.Call) and expr.args:
        f = expr.func
        fname = f.id if isinstance(f, ast.Name) else getattr(f, "attr", None)
        first = expr.args[0]
        on_principal = isinstance(first, ast.Name) and first.id in principals
        if fname == "getattr" and on_principal and len(expr.args) >= 2:
            key = expr.args[1]
            return isinstance(key, ast.Constant) and key.value == "agent_name"
        if fname == "agent_principal_name" and on_principal:
            return True
    return False


_DIFFERS = (ast.NotEq, ast.IsNot, ast.NotIn)
_EQUALS = (ast.Eq, ast.Is, ast.In)


def _compares_principal_to_agent(test: ast.AST, principals, aliases, agent_params, *, differs: bool) -> bool:
    """Does `test` say the principal's agent name differs from (`differs`) —
    or equals — the path agent? Reads `!=` / `is not` / `not in (…)`, their
    negations, and either operand order."""
    negated = {
        id(n.operand)
        for n in ast.walk(test)
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.Not) and isinstance(n.operand, ast.Compare)
    }
    for n in ast.walk(test):
        if not isinstance(n, ast.Compare):
            continue
        direct, flipped = (_DIFFERS, _EQUALS) if differs else (_EQUALS, _DIFFERS)
        if not (
            any(isinstance(op, direct) for op in n.ops)
            or (id(n) in negated and any(isinstance(op, flipped) for op in n.ops))
        ):
            continue
        parts = [n.left]
        for c in n.comparators:
            parts.extend(c.elts if isinstance(c, (ast.Tuple, ast.List, ast.Set)) else [c])
        if any(_agent_name_of(p, principals, aliases) for p in parts) and any(
            isinstance(p, ast.Name) and p.id in agent_params for p in parts
        ):
            return True
    return False


def hand_copied_self_gates(tree: ast.Module) -> List[Tuple[str, int]]:
    """`(qualname, lineno)` of every hand-written agent-key self-gate in a
    module: an `if` that refuses (any `raise`, or a returned 401/403/404
    response) when a principal's agent name differs from the function's
    `name` / `agent_name` parameter — or a local bound from it — or whose
    `else` refuses when the two are equal. `dependencies.assert_own_agent` is
    the one spelling the census recognises; every other one is a copy the next
    author forgets.

    Stated limit: the comparison must be against a parameter named `name` or
    `agent_name` (the `dependencies._path_agent` convention, which every route
    path here follows) or a local alias of one. A gate written against a
    differently-named parameter is not matched; widening that would flag the
    legitimate header checks (`x_source_agent != current_user.agent_name`)."""
    out: List[Tuple[str, int]] = []
    for qual, fn in _walk_defs(tree):
        principals = _principal_like_params(fn)
        agent_params = _param_names(fn) & set(_AGENT_PARAMS)
        if not principals or not agent_params:
            continue
        assigns = [
            node for node in ast.walk(fn)
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
        ]
        aliases = {a.targets[0].id for a in assigns if _agent_name_of(a.value, principals, set())}
        agent_params = agent_params | {
            a.targets[0].id for a in assigns
            if isinstance(a.value, ast.Name) and a.value.id in agent_params
        }
        for node in ast.walk(fn):
            if not isinstance(node, ast.If):
                continue
            if (_refuses(node.body) and _compares_principal_to_agent(
                    node.test, principals, aliases, agent_params, differs=True)) or (
                    node.orelse and _refuses(node.orelse) and _compares_principal_to_agent(
                        node.test, principals, aliases, agent_params, differs=False)):
                out.append((qual, node.lineno))
    return out


def _route_decorators(
    fn: ast.AST,
) -> List[Tuple[ast.Call, Optional[Tuple[str, ...]], bool]]:
    """(decorator call, methods, is_websocket) for each route decorator."""
    out = []
    for dec in fn.decorator_list:
        if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)):
            continue
        attr = dec.func.attr
        if attr == "websocket":
            out.append((dec, None, True))
        elif attr in HTTP_DECORATORS:
            methods = HTTP_DECORATORS[attr]
            if methods is None:
                methods = tuple(sorted(_methods_kw(dec) or ("GET",)))
            out.append((dec, methods, False))
    return out


def _methods_kw(call: ast.Call) -> Optional[Tuple[str, ...]]:
    for k in call.keywords:
        if k.arg == "methods" and isinstance(k.value, (ast.List, ast.Tuple, ast.Set)):
            return tuple(
                e.value.upper()
                for e in k.value.elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str)
            )
    return None


def _gates_for(
    idx: _FileIndex, fn: ast.AST, decorators: Iterable[ast.Call]
) -> Set[str]:
    gates: Set[str] = set()
    for dep in _signature_depends(fn):
        g = _depends_gate(idx, dep)
        if g:
            gates.add(g)
    for dec in decorators:
        for k in dec.keywords:
            if k.arg == "dependencies" and isinstance(k.value, (ast.List, ast.Tuple)):
                for e in k.value.elts:
                    if _is_depends(e):
                        g = _depends_gate(idx, e)
                        if g:
                            gates.add(g)
    imp = _imperative_gate(idx, fn)
    if imp:
        gates.add(imp)
    return gates


def _walk_defs(tree: ast.Module):
    """Yield (qualname, function node) for every function, with runtime-style
    qualnames (`outer.<locals>.inner`, `Class.method`)."""

    def visit(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                q = f"{prefix}{child.name}"
                yield q, child
                yield from visit(child, f"{q}.<locals>.")
            elif isinstance(child, ast.ClassDef):
                yield from visit(child, f"{prefix}{child.name}.")
            else:
                yield from visit(child, prefix)

    yield from visit(tree, "")


def _module_to_relpath(module: str, root: Path) -> Optional[str]:
    base = module.replace(".", "/")
    for cand in (f"{base}.py", f"{base}/__init__.py"):
        if (root / cand).is_file():
            return cand
    return None


def walk(root: Path = BACKEND, files: Optional[List[Path]] = None) -> Census:
    files = files if files is not None else backend_files(root)
    indexes: Dict[str, _FileIndex] = {}
    ordered: Dict[str, List[Tuple[str, ast.AST]]] = {}
    defs: Dict[str, Dict[str, ast.AST]] = {}
    for p in files:
        rel = p.relative_to(root).as_posix()
        tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
        indexes[rel] = _FileIndex(rel, tree)
        ordered[rel] = list(_walk_defs(tree))  # a redefinition stays visible
        defs[rel] = dict(ordered[rel])

    routes: Dict[str, Route] = {}
    websockets: Set[str] = set()
    problems: List[str] = []

    def add(rel: str, qual: str, fn: ast.AST, methods: Tuple[str, ...], decorators):
        key = f"{rel}::{qual}"
        if key in routes:
            existing = routes[key]
            if existing.lineno != fn.lineno or existing.relpath != rel:
                problems.append(f"duplicate census key {key}")
            existing.methods = tuple(sorted(set(existing.methods) | set(methods)))
            # A second registration (`add_api_route` of a decorated handler)
            # serves a path the self-gate was never checked against.
            existing.self_gate = None
            return
        routes[key] = Route(
            key=key,
            relpath=rel,
            qualname=qual,
            lineno=fn.lineno,
            methods=tuple(sorted(methods)),
            gates=_gates_for(indexes[rel], fn, decorators),
            self_gate=_self_gate(indexes[rel], fn, list(decorators)),
        )

    for rel, idx in indexes.items():
        for qual, fn in ordered[rel]:
            decs = _route_decorators(fn)
            http = [(d, m) for d, m, ws in decs if not ws]
            if any(ws for _, _, ws in decs):
                websockets.add(f"{rel}::{qual}")
            if http:
                add(
                    rel,
                    qual,
                    fn,
                    tuple(sorted({m for _, ms in http for m in ms})),
                    [d for d, _ in http],
                )

        # `<obj>.add_api_route(path, endpoint, methods=[...])`
        for node in ast.walk(idx.tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_api_route"
            ):
                continue
            target = (
                node.args[1]
                if len(node.args) > 1
                else next((k.value for k in node.keywords if k.arg == "endpoint"), None)
            )
            methods = _methods_kw(node) or ("GET",)
            resolved = _resolve_endpoint(idx, target, root)
            if resolved is None:
                problems.append(
                    f"{rel}:{node.lineno}: add_api_route target cannot be resolved to its definition"
                )
                continue
            def_rel, name = resolved
            fn = defs.get(def_rel, {}).get(name)
            if fn is None:
                problems.append(
                    f"{rel}:{node.lineno}: add_api_route target {def_rel}::{name} not found"
                )
                continue
            add(def_rel, name, fn, tuple(sorted(methods)), [])

    return Census(routes=routes, websockets=websockets, problems=problems)


def _resolve_endpoint(
    idx: _FileIndex, target: Optional[ast.AST], root: Path
) -> Optional[Tuple[str, str]]:
    if isinstance(target, ast.Name):
        if target.id in idx.local_defs:
            return idx.relpath, target.id
        if target.id in idx.names:
            mod, orig = idx.names[target.id]
            rel = _module_to_relpath(mod, root)
            return (rel, orig) if rel else None
        return None
    if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
        mod = idx.modules.get(target.value.id)
        rel = _module_to_relpath(mod, root) if mod else None
        return (rel, target.attr) if rel else None
    return None


# --------------------------------------------------------------------------- #
# The reviewed tables
# --------------------------------------------------------------------------- #

# Widenings of the admin gate to another scope (#2323). Grants, reviewed per route.
# key -> ("METHOD /full/path", scopes, reason)
ADMIN_WIDENED: Dict[str, Tuple[str, Tuple[str, ...], str]] = {
    "routers/ops.py::get_fleet_status": ("GET /api/ops/fleet/status", ("ops",), "#2323 ops read fence: an ops key reads fleet state; role == admin still enforced"),
    "routers/ops.py::get_fleet_health": ("GET /api/ops/fleet/health", ("ops",), "#2323 ops read fence: an ops key reads fleet state; role == admin still enforced"),
    "routers/ops.py::list_all_schedules": ("GET /api/ops/schedules", ("ops",), "#2323 ops read fence: an ops key reads fleet state; role == admin still enforced"),
    "routers/ops.py::list_alerts": ("GET /api/ops/alerts", ("ops",), "#2323 ops read fence: an ops key reads fleet state; role == admin still enforced"),
    "routers/ops.py::get_ops_costs": ("GET /api/ops/costs", ("ops",), "#2323 ops read fence: an ops key reads fleet state; role == admin still enforced"),
    "routers/ops.py::get_auth_report": ("GET /api/ops/auth-report", ("ops",), "#2323 ops read fence: an ops key reads fleet state; role == admin still enforced"),
    "routers/subscriptions.py::get_subscription_usage": (
        "GET /api/subscriptions/{subscription_id}/usage", ("ops",), "#2323 ops read fence: an ops key reads fleet state; role == admin still enforced (subscription pressure, #2389)"),
}

# Agent-callable routes fenced to the calling agent (trinity-enterprise#840).
# Recognised AND listed: the census checks each entry's route carries the
# self-gate (`assert_own_agent` first, or `Depends(get_self_acting_agent)`), and
# fails a self-gated route with no entry. The reason says why the route is a
# USE an agent makes on itself, not a GRANT (Invariant #8) — a route that
# changes what the agent itself may do is person-only however it is fenced.
# key -> ("METHOD /full/path", reason)
AGENT_CALLABLE_SELF: Dict[str, Tuple[str, str]] = {
    "routers/agent_files.py::get_agent_canon_roles": (
        "GET /api/agents/{agent_name}/canon/roles",
        "a READ of the agent's own canon seats (trinity-enterprise#817), the same rule and budget as its objectives"),
    "routers/agent_files.py::get_agent_objectives": (
        "GET /api/agents/{agent_name}/objectives",
        "a READ of what the agent is supposed to move, joined from its own canon (trinity-enterprise#666)"),
    "routers/agent_files.py::share_agent_file": (
        "POST /api/agents/{agent_name}/shared-files",
        "MCP share_file: a download link for a file in the agent's OWN publish dir (FILES-001); the owner gate "
        "follows. Listed as today's behaviour, not a ruling: whether minting a public link is a grant (person-only, "
        "like the canvas share, trinity-enterprise#554) is open in trinity-enterprise#719's sharing cluster"),
    "routers/reports.py::create_report": (
        "POST /api/agents/{name}/reports", "MCP report: structured output an agent publishes about itself (#918)"),
    "routers/metric_points.py::record_metric_points": (
        "POST /api/agents/{name}/metrics/points",
        "MCP record_metrics: points for metrics the agent declared itself (trinity-enterprise#478)"),
    "routers/reminders.py::create_reminder_endpoint": (
        "POST /api/agents/{name}/reminders",
        "MCP set_reminder: a one-shot reminder the agent sets for itself; whether it fires stays bounded by autonomy (#1296, #1806)"),
    "routers/reminders.py::list_reminders_endpoint": (
        "GET /api/agents/{name}/reminders", "MCP list_reminders: the agent reads its own reminders (#1296)"),
    "routers/reminders.py::cancel_reminder_endpoint": (
        "POST /api/agents/{name}/reminders/{reminder_id}/cancel",
        "MCP cancel_reminder: the agent cancels its own pending reminder (#1296)"),
    "routers/seat_decisions.py::record_seat_decision": (
        "POST /api/agents/{agent_name}/decisions",
        "MCP record_decision: a decision for the seat the run serves; the seat comes from the execution, never the caller (trinity-enterprise#638, #839)"),
    "routers/seat_decisions.py::list_seat_decisions": (
        "GET /api/agents/{agent_name}/decisions",
        "MCP list_seat_decisions: the seat's standing decisions, read to reuse a criterion (trinity-enterprise#638)"),
    "routers/public_memory.py::write_user_memory": (
        "POST /api/agents/{agent_name}/user-memory",
        "MCP write_user_memory: notes about the person the run serves, resolved from the execution (MEM-001); every write recorded and undoable (trinity-enterprise#637)"),
    "routers/canvas.py::write_canvas": (
        "PUT /api/agents/{name}/canvas/{canvas_id}",
        "MCP set_canvas: the agent renders its own canvas; who outside the platform may read it (share) stays human-only (trinity-enterprise#554)"),
    "routers/canvas.py::patch_canvas": (
        "PATCH /api/agents/{name}/canvas/{canvas_id}",
        "MCP patch_canvas: replaces named blocks of the agent's own canvas (trinity-enterprise#536)"),
    "routers/canvas.py::clear_canvas": (
        "DELETE /api/agents/{name}/canvas/{canvas_id}",
        "MCP clear_canvas: an agent tidying up its own canvas; a person must own the agent (trinity-enterprise#553)"),
    "routers/canvas.py::bulk_delete_canvases": (
        "POST /api/agents/{name}/canvas/bulk-delete",
        "no MCP tool; the same rule as clear_canvas, so an agent key may remove only its own canvases (trinity-enterprise#553)"),
    "routers/agents.py::send_voice_reply_endpoint": (
        "POST /api/agents/{agent_name}/voice-reply",
        "MCP send_voice_reply: the agent speaks one reply of its own current channel turn (trinity-enterprise#117)"),
    "routers/a2a.py::call_a2a_agent": (
        "POST /api/agents/{agent_name}/a2a/call",
        "MCP a2a_call: spends an endpoint an admin registered, a USE of that grant (#736); the self-gate keeps the rate limit, audit and activity on the agent that spent it"),
    "routers/a2a.py::get_a2a_task": (
        "POST /api/agents/{agent_name}/a2a/task",
        "MCP a2a_call follow-up: polls a remote task one of the agent's own calls started (#736)"),
    "routers/nevermined_delegation.py::get_agent_delegation": (
        "GET /api/nevermined/agents/{agent_name}/delegation",
        "MCP get_agent_spending: a READ of the agent's own spending budget, live from Nevermined "
        "(trinity-enterprise#966); a sibling gets the router's uniform 404, the same as a missing agent "
        "(refusal=), and bind and revoke are person-only"),
    # The agent's own asks (ent#611): get_self_acting_agent admits only the agent's own
    # key (or trinity-system as itself) and refuses every person.
    "routers/operator_queue.py::raise_my_ask": (
        "POST /api/agents/{name}/operator-queue", "MCP ask_operator; the agent raises its own ask, self-only (get_self_acting_agent)"),
    # trinity-enterprise#901: the agent ends its OWN pending ask with a reason; the same
    # self-only gate, the row must carry raised_by='agent' (a gate's approval is a 404).
    "routers/operator_queue.py::withdraw_my_ask": (
        "POST /api/agents/{name}/operator-queue/{request_id}/withdraw", "MCP withdraw_ask; the agent withdraws its own pending ask, self-only (get_self_acting_agent)"),
    "routers/operator_queue.py::get_my_ask": (
        "GET /api/agents/{name}/operator-queue/{request_id}",
        "MCP ask readback: how one of the agent's own asks stands (trinity-enterprise#611), a redacted projection; self-only (get_self_acting_agent)"),
}

# Answers to "why may an agent OTHER than the one in the path reach this".
_SIB_OWN_KEY = (
    "a sibling cannot reach it: the handler authenticates the agent's OWN key itself "
    "(heartbeat_service.authorize_heartbeat: the key's agent must be the path agent)")
_SIB_AAUTH = (
    "a sibling cannot reach it, nor can a person: aauth_person.assert_own_key admits only the "
    "agent's OWN key (403 aauth_agent_only), stricter than assert_own_agent")
_SIB_FROM_KEY = "no sibling to name: the agent comes from the key, never from the request"
_SIB_NO_AGENT = "names no agent"
_SIB_SCHEDULES = (
    "a sibling needs the admin-granted schedules.manage capability and reaches only agents its "
    "owner owns (capability_fence own_agent_exempt, trinity-enterprise#164)")
_SIB_SKILL_SETS = (
    "a sibling needs the skills.manage capability, then the owner fence "
    "(get_skill_managed_agent_by_name, trinity-enterprise#596)")
_SIB_SKILL_GATE_WRITE = (
    "a sibling needs skills.manage on an agent its owner owns, and never on itself "
    "(require_person_or_capability, self_person_only, trinity-enterprise#753)")
_SIB_READ = (
    "a non-sensitive read a sibling may make only inside its reach: an edge target or an agent "
    "it spawned (agent_may_reach, trinity-enterprise#629)")

# Reviewed uses an agent key may make WITHOUT a recognised self-gate.
# key -> ("METHOD /full/path", reason, sibling)
AGENT_CALLABLE_ANY: Dict[str, Tuple[str, str, str]] = {
    # The agent's own runtime surface (#2996 §3.4 regression set).
    "routers/agents.py::agent_heartbeat": (
        "POST /api/agents/{agent_name}/heartbeat",
        "liveness; authenticates the agent's own agent-scoped key outside get_current_user (#307)",
        _SIB_OWN_KEY),
    "routers/agents.py::agent_execution_result": (
        "POST /api/agents/{agent_name}/executions/{execution_id}/result",
        "terminal result delivery; authenticates the agent's own key and path self-match (#1083)",
        _SIB_OWN_KEY),
    "routers/aauth_person.py::request_person_token": (
        "POST /api/agents/{agent_name}/aauth/person-token",
        "a USE of the agent's AAuth identity (trinity-enterprise#878): the handler refuses every principal "
        "but the agent's OWN key (403 aauth_agent_only) — a human may not make the backend sign as the agent",
        _SIB_AAUTH),
    "routers/aauth_resource.py::call_aauth_resource": (
        "POST /api/agents/{agent_name}/aauth/resource-call",
        "a USE of the agent's AAuth identity (trinity-enterprise#879): the same own-key gate as the person-token "
        "route — every principal but the agent's OWN key is refused (403 aauth_agent_only) before the allow-list "
        "is consulted, so a human may neither make the backend sign as the agent nor probe the allow-list",
        _SIB_AAUTH),
    "routers/notifications.py::create_notification": (
        "POST /api/notifications", "an agent raises a notification; agent name comes from its key (NOTIF-001)",
        _SIB_FROM_KEY),
    # MCP schedule tools — the agent's own use; autonomy (person-only) bounds cron firing.
    "routers/schedules.py::create_schedule": (
        "POST /api/agents/{name}/schedules", "MCP create_schedule; unattended firing is gated by autonomy",
        _SIB_SCHEDULES),
    "routers/schedules.py::update_schedule": (
        "PUT /api/agents/{name}/schedules/{schedule_id}", "MCP update_schedule", _SIB_SCHEDULES),
    "routers/schedules.py::delete_schedule": (
        "DELETE /api/agents/{name}/schedules/{schedule_id}", "MCP delete_schedule", _SIB_SCHEDULES),
    "routers/schedules.py::enable_schedule": (
        "POST /api/agents/{name}/schedules/{schedule_id}/enable", "MCP enable_schedule; cron still gated by autonomy",
        _SIB_SCHEDULES),
    "routers/schedules.py::disable_schedule": (
        "POST /api/agents/{name}/schedules/{schedule_id}/disable", "MCP disable_schedule", _SIB_SCHEDULES),
    "routers/schedules.py::trigger_schedule": (
        "POST /api/agents/{name}/schedules/{schedule_id}/trigger", "MCP trigger_schedule (a manual run)",
        "no capability fence ('trigger now' is exempt from schedules.manage, trinity-enterprise#164): a sibling "
        "triggers another agent's schedule only as an agent it spawned (OwnedAgent, the ent#629 manage reach)"),
    # Reads of the settings whose writes #2996 made person-only.
    "routers/agent_config.py::get_agent_api_key_setting": ("GET /api/agents/{agent_name}/api-key-setting", "a non-sensitive read of this agent's own setting (access-level)", _SIB_READ),
    "routers/agent_config.py::get_agent_autonomy_status": ("GET /api/agents/{agent_name}/autonomy", "a non-sensitive read of this agent's own setting (access-level)", _SIB_READ),
    "routers/agent_config.py::get_agent_read_only_status": ("GET /api/agents/{agent_name}/read-only", "a non-sensitive read of this agent's own setting (access-level)", _SIB_READ),
    "routers/agent_config.py::get_agent_resources": ("GET /api/agents/{agent_name}/resources", "a non-sensitive read of this agent's own setting (access-level)", _SIB_READ),
    "routers/agent_config.py::get_agent_capabilities": ("GET /api/agents/{agent_name}/capabilities", "a non-sensitive read of this agent's own setting (access-level)", _SIB_READ),
    "routers/agent_config.py::get_agent_capacity": ("GET /api/agents/{agent_name}/capacity", "a non-sensitive read of this agent's own setting (access-level)", _SIB_READ),
    "routers/agent_config.py::get_agent_timeout": ("GET /api/agents/{agent_name}/timeout", "a non-sensitive read of this agent's own setting (access-level)", _SIB_READ),
    "routers/agent_config.py::get_public_channel_model": ("GET /api/agents/{agent_name}/public-channel-model", "a non-sensitive read of this agent's own setting (access-level)", _SIB_READ),
    "routers/agent_config.py::get_agent_guardrails": ("GET /api/agents/{agent_name}/guardrails", "a non-sensitive read of this agent's own setting (access-level)", _SIB_READ),
    "routers/agent_config.py::list_agent_capability_grants": (
        "GET /api/agents/{agent_name}/capability-grants",
        "trinity-enterprise#164: an agent reads which self-change permissions it holds, so it knows when to raise a "
        "permission-request (owner-level; granting is interactive-admin)",
        "owner tier (OwnedAgentByName): a sibling reads it only for an agent it spawned, never through an edge "
        "(the ent#629 manage reach)"),
    "routers/agent_files.py::get_agent_peer_skills": (
        "GET /api/agents/{agent_name}/peer-skills",
        "abilityai/trinity-enterprise#662: a READ of what this agent's own edges expose — the library skills on its "
        "permitted targets and whether it may run each — so an agent can reason about a refusal before it is refused; "
        "targets filtered to what the viewer may open AND (for an agent key) reach; the grant itself is admin + interactive",
        _SIB_READ),
    "routers/agent_files.py::get_agent_reach": (
        "GET /api/agents/{agent_name}/reach/{target}",
        "trinity-enterprise#629: the MCP agent-to-agent gate asks, with the CALLING agent's own key, whether it may "
        "call a target — the edge or the spawn record, a boolean; access as for GET .../permissions",
        "a sibling may ask only about an agent inside its own reach (access + agent_may_reach); the answer is a boolean"),
    "routers/users.py::get_my_github_pat_status": (
        "GET /api/users/me/github-pat", "configured flags only, never the token (ent#162)",
        _SIB_NO_AGENT + ": the key owner's own flags"),
    # Skill sets (ent#530). The writes are the USE of the skills-manage capability, fenced like
    # the single-skill writes by get_skill_managed_agent_by_name (capability, then owner); the
    # GRANT is PUT /agents/{name}/skill-manager, admin + interactive (ent#596). Not a setting.
    "routers/skills.py::list_skill_sets": (
        "GET /api/skills/library/sets", "library set metadata (names, members, schedules), open like the skills listing",
        _SIB_NO_AGENT + " (the library listing)"),
    "routers/skills.py::get_agent_skill_sets": (
        "GET /api/agents/{agent_name}/skill-sets", "a read of this agent's sets; ?probe= honoured only under the ent#596 fence (#3052)",
        _SIB_READ),
    "routers/skills.py::assign_skill_set": (
        "POST /api/agents/{agent_name}/skill-sets/{set_name}", "ent#596 skills-manage USE: capability holder + owner fence; grant is admin-interactive",
        _SIB_SKILL_SETS),
    "routers/skills.py::unassign_skill_set": (
        "DELETE /api/agents/{agent_name}/skill-sets/{set_name}", "ent#596 skills-manage USE: capability holder + owner fence; grant is admin-interactive",
        _SIB_SKILL_SETS),
    # trinity-enterprise#752: the in-container skill-gate hook reads its own gates and its own
    # run's clearance; the agent comes from the key (get_self_agent), every person is refused.
    "routers/skill_gate.py::check_skill_invocation": (
        "POST /api/skill-gate/check", "the in-container hook's check: own gates and own run only, agent from the key (get_self_agent)",
        _SIB_FROM_KEY + " (get_self_agent)"),
    # trinity-enterprise#753: the per-agent skill gate map. The writes take the #3236 shape
    # (require_person_or_capability("skills.manage", self_person_only=True)): a person, or a
    # skills.manage holder on an agent its owner owns — never itself; then the owner fence.
    # The read: an agent key reads its own gates (the hook pulls), a holder an agent its owner owns.
    "routers/skill_gate.py::list_agent_skill_gates": (
        "GET /api/agents/{agent_name}/skill-gates", "ent#753 read: anyone with access; an agent key its own gates, a skills.manage holder an agent its owner owns",
        "a sibling reads another agent's gates only holding skills.manage on an agent its owner owns "
        "(get_skill_gate_readable_agent_by_name, trinity-enterprise#753)"),
    "routers/skill_gate.py::set_agent_skill_gate": (
        "PUT /api/agents/{agent_name}/skill-gates/{skill_name}", "ent#753 skills.manage USE on an owner-owned agent, never itself; persons pass assert_person + owner fence",
        _SIB_SKILL_GATE_WRITE),
    "routers/skill_gate.py::clear_agent_skill_gate": (
        "DELETE /api/agents/{agent_name}/skill-gates/{skill_name}", "ent#753 skills.manage USE on an owner-owned agent, never itself; persons pass assert_person + owner fence",
        _SIB_SKILL_GATE_WRITE),
    # #3295: the MCP server arms a sequential /chat row's completion report with the
    # CALLER's key when it hands back a receipt; the service admits only the row's own
    # dispatcher and inherits the destination through the ent#265 provenance guard.
    "routers/chat.py::arm_execution_report_back": (
        "POST /api/agents/{name}/executions/{execution_id}/report-back",
        "#3295 MCP report-back arm: the dispatcher of a /chat row (agent key = source_agent_name) asks for the consent-gated completion report; inheritance stays behind _inherited_channel_context",
        "a sibling may arm only a row it dispatched itself (chat_execution_service checks source_agent_name), "
        "on an agent inside its reach (get_authorized_agent)"),
    # ent#703: the agent's pull loop reads its own switch every cycle with its own key.
    # The write (PUT .../git/pull-sync) is a setting, so it is person-only.
    "routers/git.py::get_pull_sync_config": (
        "GET /api/agents/{agent_name}/git/pull-sync", "ent#703 the agent's pull loop reads its own switch live each cycle",
        _SIB_READ + " — one boolean"),
}

# Authenticates itself, or unauthenticated by design. key -> ("METHOD /full/path", reason)
_SECRET = "internal shared secret (router-level verify_internal_secret, C-003)"
_PUBLIC_LINK = "public-link surface; the link token in the path is the credential"
_PULL = "pull seam: internal secret OR the agent's own scoped key (#1081)"
_INLINE = "internal secret + inline-auth flag (#848)"

OWN_AUTH: Dict[str, Tuple[str, str]] = {
    "routers/internal.py::execute_task_internal": ("POST /api/internal/execute-task", _SECRET),
    "routers/internal.py::internal_agent_brief_readiness": ("GET /api/internal/agents/{agent_name}/brief-readiness", _SECRET),
    "routers/internal.py::internal_agent_pre_check": ("POST /api/internal/agents/{agent_name}/pre-check", _SECRET),
    "routers/internal.py::internal_next_task": ("GET /api/internal/next-task", _PULL),
    "routers/internal.py::internal_task_result": ("POST /api/internal/tasks/{execution_id}/result", _PULL),
    "routers/internal.py::log_audit_entry": ("POST /api/internal/audit", _SECRET),
    "routers/internal.py::mcp_exposed_agents": ("GET /api/internal/mcp-exposed-agents", _SECRET),
    "routers/internal.py::validate_execution": ("POST /api/internal/validate-execution", _SECRET),
    "routers/setup.py::get_setup_status": ("GET /api/setup/status", "unauthenticated by design (first-run status)"),
    "routers/setup.py::set_admin_password": ("POST /api/setup/admin-password", "unauthenticated by design until setup completes, then refused"),
    "routers/webhooks.py::trigger_webhook": ("POST /api/webhooks/{webhook_token}", "the webhook token in the path is the credential"),
    "routers/paid.py::get_paid_agent_info": ("GET /api/paid/{agent_name}/info", "public paid-agent listing (x402)"),
    "routers/paid.py::paid_chat": ("POST /api/paid/{agent_name}/chat", "x402 payment is the credential"),
    "routers/mcp_auth.py::inline_chat": ("POST /api/internal/mcp-auth/chat", _INLINE),
    "routers/mcp_auth.py::inline_playbooks": ("POST /api/internal/mcp-auth/playbooks", _INLINE),
    "routers/mcp_auth.py::request_inline_login": ("POST /api/internal/mcp-auth/request", _INLINE),
    "routers/mcp_auth.py::verify_inline_login": ("POST /api/internal/mcp-auth/verify", _INLINE),
    "routers/auth.py::get_auth_mode": ("GET /api/auth/mode", "unauthenticated by design (login page)"),
    "routers/auth.py::login": ("POST /token", "password sign-in"),
    "routers/auth.py::login_api": ("POST /api/token", "password sign-in"),
    "routers/auth.py::request_access": ("POST /api/access/request", "unauthenticated by design (access request)"),
    "routers/auth.py::request_email_login_code": ("POST /api/auth/email/request", "email sign-in, step 1"),
    "routers/auth.py::verify_email_login_code": ("POST /api/auth/email/verify", "email sign-in, step 2"),
    "routers/auth.py::validate_token": ("GET /api/auth/validate", "validates the presented token itself"),
    "routers/public.py::clear_public_session": ("DELETE /api/public/session/{token}", _PUBLIC_LINK),
    "routers/public.py::confirm_verification_code": ("POST /api/public/verify/confirm", "public-link surface; the link token in the path is the credential (email verification)"),
    "routers/public.py::get_agent_intro": ("GET /api/public/intro/{token}", _PUBLIC_LINK),
    "routers/public.py::get_public_chat_history": ("GET /api/public/history/{token}", _PUBLIC_LINK),
    "routers/public.py::get_public_link_info": ("GET /api/public/link/{token}", _PUBLIC_LINK),
    "routers/public.py::get_public_playbooks": ("GET /api/public/playbooks/{token}", _PUBLIC_LINK),
    "routers/public.py::public_chat": ("POST /api/public/chat/{token}", _PUBLIC_LINK),
    "routers/public.py::public_execution_status": ("GET /api/public/executions/{token}/{execution_id}/status", _PUBLIC_LINK),
    "routers/public.py::public_stream_execution": ("GET /api/public/executions/{token}/{execution_id}/stream", _PUBLIC_LINK),
    "routers/public.py::public_terminate_execution": ("POST /api/public/executions/{token}/{execution_id}/terminate", _PUBLIC_LINK),
    "routers/public.py::request_verification_code": ("POST /api/public/verify/request", "public-link surface; the link token in the path is the credential (email verification)"),
    "routers/public.py::tls_allowed": ("GET /api/public/tls-allowed", "unauthenticated by design (reverse-proxy on-demand TLS probe)"),
    "routers/a2a.py::a2a_well_known_card": ("GET /a2a/{agent_name}/.well-known/agent-card.json", "unauthenticated by design (A2A discovery card)"),
    "routers/a2a.py::a2a_jsonrpc": ("POST /a2a/{agent_name}", "ent#679: a Trinity MCP key OR an x402 payment token; decided in-handler (get_user_or_anonymous → 401 unless exposed AND priced)"),
    # trinity-enterprise#623: AAuth discovery documents. Anonymous by design —
    # a peer fetches them from the issuer origin before it holds any credential;
    # they carry only public material (issuer, JWKS public keys, signature
    # requirements) and answer a uniform 404 while the prototype flag is off.
    "routers/aauth.py::aauth_agent_metadata": ("GET /.well-known/aauth-agent.json", "unauthenticated by design (AAuth agent-provider metadata; 404 unless the prototype flag is on)"),
    "routers/aauth.py::aauth_jwks": ("GET /.well-known/aauth-jwks.json", "unauthenticated by design (AAuth JWKS, public keys only; 404 unless the prototype flag is on)"),
    "routers/aauth.py::aauth_resource_metadata": ("GET /.well-known/aauth-resource.json", "unauthenticated by design (AAuth resource metadata; 404 unless the prototype flag is on)"),
    "routers/mcp_keys.py::validate_mcp_api_key_http_endpoint": ("POST /api/mcp/validate", "validates the presented MCP key itself (MCP server auth)"),
    "main.py::health_check": ("GET /health", "unauthenticated by design (health probe)"),
    "routers/slack.py::handle_slack_event": ("POST /api/public/slack/events", "Slack request signature"),
    "routers/slack.py::slack_oauth_callback": ("GET /api/public/slack/oauth/callback", "OAuth state parameter"),
    "routers/telegram.py::handle_telegram_webhook": ("POST /api/telegram/webhook/{webhook_secret}", "per-binding webhook secret"),
    "routers/whatsapp.py::handle_twilio_webhook": ("POST /api/whatsapp/webhook/{webhook_secret}", "per-binding webhook secret + Twilio signature"),
    "client_portal/router.py::portal_auth_request": ("POST /api/enterprise/client-portal/auth/request", "portal sign-in, step 1"),
    "client_portal/router.py::portal_auth_verify": ("POST /api/enterprise/client-portal/auth/verify", "portal sign-in, step 2"),
    "routers/files.py::download_shared_file": ("GET /api/files/{file_id}", "the share signature is the credential (FILES-001)"),
    "routers/files.py::head_shared_file": ("HEAD /api/files/{file_id}", "the share signature is the credential (FILES-001)"),
    "routers/avatar.py::get_avatar": ("GET /api/agents/{agent_name}/avatar", "unauthenticated by design (avatars are public assets)"),
    "routers/avatar.py::get_avatar_emotion": ("GET /api/agents/{agent_name}/avatar/emotion/{emotion}", "unauthenticated by design (public asset)"),
    "routers/avatar.py::get_avatar_emotions": ("GET /api/agents/{agent_name}/avatar/emotions", "unauthenticated by design (public asset)"),
    "routers/avatar.py::get_avatar_reference": ("GET /api/agents/{agent_name}/avatar/reference", "unauthenticated by design (public asset)"),
}

# Check lives in services/. key -> ("METHOD /full/path", service function, behavioural test id)
DELEGATED: Dict[str, Tuple[str, str, str]] = {
    # trinity-enterprise#719: a room's moderator acts. Same principal as the
    # picker below (`get_room_principal`), so no dependency gate fits. The check
    # is `_require_moderator`: membership first (the uniform 404 for a
    # non-member; the admin account sees every room), then a caller acting as
    # a platform account (kind `user`) must be a PERSON on both of its branches
    # (the admin bypass and the `user` moderator row), else 403 `not_a_person`;
    # a Workspace client keeps its own moderator row; an agent key is never a
    # moderator (403 `not_moderator`).
    "shared_sessions/router.py::close_room": (
        "POST /api/rooms/{room_id}/close",
        "shared_sessions.service.close_room -> _require_moderator",
        "tests/unit/test_ent719_room_moderator_person.py::test_the_system_key_cannot_moderate_a_room_its_admin_owner_created"),
    "shared_sessions/router.py::add_participant": (
        "POST /api/rooms/{room_id}/participants",
        "shared_sessions.service.add_participant -> _require_moderator",
        "tests/unit/test_ent719_room_moderator_person.py::test_the_system_key_cannot_moderate_a_room_its_admin_owner_created"),
    "shared_sessions/router.py::remove_participant": (
        "DELETE /api/rooms/{room_id}/participants/{agent_name}",
        "shared_sessions.service.remove_participant -> _require_moderator",
        "tests/unit/test_ent719_room_moderator_person.py::test_the_system_key_cannot_moderate_a_room_its_admin_owner_created"),
    # trinity-enterprise#719: a room post. Its principal is `get_room_principal`
    # (a platform user, an agent key or a Workspace client), so no dependency
    # gate fits. The router checks membership before the idempotency claim, so
    # a non-member gets the uniform 404 on every answer to a well-formed post, a
    # stored replay and the in-flight 409 included (a malformed body gets the
    # same 422 from request validation, member or not).
    "shared_sessions/router.py::post_message": (
        "POST /api/rooms/{room_id}/messages",
        "shared_sessions.service._require_membership, before idempotency_service.begin",
        "tests/unit/test_ent719_room_post_membership_first.py::test_a_non_member_never_gets_a_stored_replay"),
    # trinity-enterprise#631: the people picker of a room. Its principal is
    # `get_room_principal` (a platform user OR a Workspace client), so no
    # dependency gate fits; the refusal of agents, system keys and external
    # clients is `person_mention_service.tagger_for`, reached first.
    "shared_sessions/router.py::room_people": (
        "GET /api/rooms/{room_id}/people",
        "shared_sessions.service.room_people -> person_mention_service.tagger_for",
        "tests/unit/test_ent631_person_mention.py::test_an_agent_key_cannot_list_people_to_tag"),
}

# WebSocket routes — out of the HTTP census, pinned so a new one is a visible edit.
WEBSOCKET_ROUTES = frozenset({
    "main.py::websocket_endpoint",
    "main.py::websocket_events_endpoint",
    "routers/agents.py::agent_terminal",
    "routers/system_agent.py::system_agent_terminal",
    "routers/voice.py::voice_websocket",
    "routers/voip.py::voip_media_stream",
})

# The exact size of the frozen baseline. Lower it in the same change that
# removes an entry; it never goes up.
FROZEN_BASELINE_COUNT = 326


def load_baseline(path: Path = BASELINE_PATH) -> Dict[str, str]:
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #


# Failure tags — one per ratchet rule, so each rule is its own test.
UNCLASSIFIED = "rule1-unclassified"
STALE = "rule2-stale-entry"
GATED_IN_BASELINE = "rule3-gated-baseline-entry"
COUNT = "rule4-baseline-count"
TWO_LISTS = "rule5-key-in-two-lists"
DUPLICATE = "rule6-duplicate-key"
PATH_MISMATCH = "rule7-runtime-path-mismatch"
WIDENED_UNLISTED = "admin-widened-unlisted"
DELEGATED_TEST = "delegated-test-missing"
SELF_UNGATED = "rule8-self-entry-without-gate"


def classify(
    census: Census,
    baseline: Dict[str, str],
    *,
    admin_widened=None,
    agent_callable_self=None,
    agent_callable_any=None,
    own_auth=None,
    delegated=None,
) -> Tuple[Dict[str, str], List[Tuple[str, str]]]:
    """Return ({key: class}, [(rule tag, message)])."""
    admin_widened = ADMIN_WIDENED if admin_widened is None else admin_widened
    agent_callable_self = AGENT_CALLABLE_SELF if agent_callable_self is None else agent_callable_self
    agent_callable_any = AGENT_CALLABLE_ANY if agent_callable_any is None else agent_callable_any
    own_auth = OWN_AUTH if own_auth is None else own_auth
    delegated = DELEGATED if delegated is None else delegated
    lists = {
        "agent_callable_self": agent_callable_self,
        "agent_callable_any": agent_callable_any,
        "own_auth": own_auth,
        "delegated": delegated,
        "unclassified_at_freeze": baseline,
    }
    out: Dict[str, str] = {}
    failures: List[Tuple[str, str]] = [(DUPLICATE, p) for p in census.problems if p.startswith("duplicate")]
    failures += [(UNCLASSIFIED, p) for p in census.problems if not p.startswith("duplicate")]

    for name, table in list(lists.items()) + [("admin_widened", admin_widened)]:
        for key in table:
            if key not in census.routes:
                failures.append((STALE, f"stale {name} entry {key}: no such route — remove it"))

    for key, route in sorted(census.routes.items()):
        code = route.code_class
        member = [n for n, t in lists.items() if key in t]
        if code is not None:
            if key in baseline:
                failures.append((GATED_IN_BASELINE, (
                    f"{key} is now gated ({code}) — remove it from the baseline and lower "
                    f"FROZEN_BASELINE_COUNT (the baseline only shrinks)")))
            for n in member:
                if n != "unclassified_at_freeze":
                    failures.append((STALE, f"{key} is gated ({code}) — its {n} entry is stale, remove it"))
            if code == "admin_widened" and key not in admin_widened:
                failures.append((WIDENED_UNLISTED, (
                    f"{key} widens the admin gate — add an ADMIN_WIDENED entry with its scopes and reason")))
            out[key] = code
            continue
        if key in admin_widened:
            failures.append((STALE, f"{key} is listed in ADMIN_WIDENED but no longer widens the admin gate — remove it"))
        gate = route.self_gate
        if gate is not None:
            for n in member:
                if n == "unclassified_at_freeze":
                    failures.append((GATED_IN_BASELINE, (
                        f"{key} is now self-gated ({gate.kind}) — remove it from the baseline, lower "
                        f"FROZEN_BASELINE_COUNT, and list it in AGENT_CALLABLE_SELF")))
                elif n != "agent_callable_self":
                    failures.append((STALE, (
                        f"{key} carries a recognised self-gate ({gate.kind}) — move its {n} entry to "
                        f"AGENT_CALLABLE_SELF")))
            if "agent_callable_self" not in member:
                failures.append((UNCLASSIFIED, (
                    f"{key} ({','.join(route.methods)}, line {route.lineno}) is self-gated but not "
                    f"listed. Add an AGENT_CALLABLE_SELF entry saying why it is a USE an agent makes "
                    f"on itself; a route that changes what the agent itself may do is a GRANT — gate "
                    f"it Depends(require_person|require_interactive) instead.")))
                continue
            out[key] = "agent_callable_self"
            continue
        if "agent_callable_self" in member:
            failures.append((SELF_UNGATED, (
                f"{key} is listed in AGENT_CALLABLE_SELF but carries no recognised self-gate: its "
                f"first statement must be assert_own_agent(<get_current_user principal>, <the path's "
                f"name|agent_name>, ...) imported from dependencies, or its signature must take "
                f"Depends(get_self_acting_agent)")))
            continue
        if len(member) > 1:
            failures.append((TWO_LISTS, f"{key} is in more than one list: {member}"))
        if not member:
            failures.append((UNCLASSIFIED, (
                f"{key} ({','.join(route.methods)}, line {route.lineno}) is unclassified. Gate it "
                f"(Depends(require_person|require_interactive|require_admin), or the imperative "
                f"call as the handler's first statement on its principal); fence it to the calling "
                f"agent (assert_own_agent as its first statement) and list it in "
                f"AGENT_CALLABLE_SELF; or list it in AGENT_CALLABLE_ANY / OWN_AUTH / DELEGATED in "
                f"tests/unit/_route_census.py with a reason. Never add it to the baseline.")))
            continue
        out[key] = member[0]

    for key, (_mp, _fn, test_id) in delegated.items():
        if not _test_exists(test_id):
            failures.append((DELEGATED_TEST, f"DELEGATED {key} names a behavioural test that does not exist: {test_id}"))
    return out, failures


def count_failures(baseline: Dict[str, str], frozen: int) -> List[Tuple[str, str]]:
    """Rule 4: the baseline is exactly FROZEN_BASELINE_COUNT entries."""
    if len(baseline) == frozen:
        return []
    return [(COUNT, (
        f"baseline has {len(baseline)} entries but FROZEN_BASELINE_COUNT is {frozen}. Gating a "
        f"route removes its entry AND lowers the literal in the same change; the baseline never grows")),
    ]


def path_failures(
    universe: Iterable[str],
    paths: Dict[str, List[str]],
    stored: Dict[str, str],
) -> List[Tuple[str, str]]:
    """Rule 7 plus the registration cross-check: every live route maps to a
    census key the AST found, and every stored "METHOD /path" is what the live
    app registers for that key (so an exemption cannot silently follow a
    function onto another path or method)."""
    universe = set(universe)
    failures: List[Tuple[str, str]] = []
    for key in sorted(paths):
        if key not in universe:
            failures.append((UNCLASSIFIED, (
                f"live route {key} ({stored_value(paths[key])}) is not in the AST census — a "
                f"registration shape the walker does not see; teach the walker")))
    for key, value in sorted(stored.items()):
        live = stored_value(paths.get(key, []))
        if value != live:
            failures.append((PATH_MISMATCH, f"{key}: stored {value!r} but the app registers {live!r}"))
    return failures


def _test_exists(test_id: str) -> bool:
    path_s, _, name = test_id.partition("::")
    name = name.split("::")[-1].split("[")[0]
    path = REPO / path_s
    if not path.is_file() or not name:
        return False
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return any(
        isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
        for n in ast.walk(tree)
    )


# --------------------------------------------------------------------------- #
# Runtime dump (run in a subprocess by the runtime test and by --freeze)
# --------------------------------------------------------------------------- #

RUNTIME_DUMP = r"""
import json, os, sys, tempfile
from pathlib import Path
backend = sys.argv[1]
td = tempfile.mkdtemp(prefix="trinity-route-census-")
for k, v in {
    "REDIS_URL": "redis://test:test@redis:6379", "REDIS_PASSWORD": "test",
    "REDIS_BACKEND_PASSWORD": "test", "AGENT_AUTH_SECRET": "0" * 64,
    "SECRET_KEY": "x" * 32, "INTERNAL_API_SECRET": "y" * 32,
}.items():
    os.environ.setdefault(k, v)
os.environ["TRINITY_DB_PATH"] = str(Path(td) / "census.db")
os.environ["LOG_ARCHIVE_PATH"] = str(Path(td) / "logs")
sys.path.insert(0, backend)
import main
from fastapi.routing import APIRoute
flat = []
for entry in main.app.routes:
    if type(entry).__name__ == "_IncludedRouter":
        flat.extend(r for r in entry.original_router.routes if isinstance(r, APIRoute))
    elif isinstance(entry, APIRoute):
        flat.append(entry)
rows = [
    {"module": r.endpoint.__module__, "qualname": r.endpoint.__qualname__,
     "methods": sorted(r.methods or []), "path": r.path}
    for r in flat
]
sys.stdout.write("\n__CENSUS__" + json.dumps(rows))
"""


def runtime_routes(
    python: str = sys.executable, backend: Path = BACKEND, timeout: int = 300
):
    """Import the real app in a SUBPROCESS and return its routes. Raises
    (never skips) on a failed import, unparseable output or zero routes."""
    import os
    import subprocess

    # `main` must resolve from `backend` alone: an inherited PYTHONPATH (the
    # verify-local unit stage sets one) would import some other tree's app.
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run(
        [python, "-c", RUNTIME_DUMP, str(backend)],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(REPO),
        env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"`import main` failed in the census subprocess (exit {proc.returncode}):\n{proc.stderr[-4000:]}"
        )
    marker = proc.stdout.rfind("__CENSUS__")
    if marker < 0:
        raise RuntimeError(
            f"census subprocess printed no route dump:\n{proc.stdout[-2000:]}"
        )
    rows = json.loads(proc.stdout[marker + len("__CENSUS__") :])
    if not rows:
        raise RuntimeError("census subprocess found zero routes")
    return rows


def runtime_key(module: str, qualname: str, root: Path = BACKEND) -> Optional[str]:
    rel = _module_to_relpath(module, root)
    return f"{rel}::{qualname}" if rel else None


def is_enterprise_module(module: str) -> bool:
    return module == "enterprise" or module.startswith("enterprise.")


def runtime_paths(rows) -> Dict[str, List[str]]:
    """{census key: sorted ["METHOD /path", ...]} over the non-enterprise routes."""
    out: Dict[str, Set[str]] = {}
    for r in rows:
        if is_enterprise_module(r["module"]):
            continue
        key = (
            runtime_key(r["module"], r["qualname"])
            or f"<unresolved {r['module']}>::{r['qualname']}"
        )
        for m in r["methods"]:
            out.setdefault(key, set()).add(f"{m} {r['path']}")
    return {k: sorted(v) for k, v in out.items()}


NOT_REGISTERED = "(not registered)"


def stored_value(paths: List[str]) -> str:
    """The baseline / table value for a key: its registered "METHOD /path"
    entries, or NOT_REGISTERED for a handler no app includes."""
    return " | ".join(paths) if paths else NOT_REGISTERED


def freeze() -> None:  # pragma: no cover - run by hand, once
    census = walk()
    rows = runtime_routes()
    paths = runtime_paths(rows)
    if census.problems:
        raise SystemExit(f"fix the walker problems first: {census.problems}")
    listed = set(AGENT_CALLABLE_SELF) | set(AGENT_CALLABLE_ANY) | set(OWN_AUTH) | set(DELEGATED)
    baseline = {
        key: stored_value(paths.get(key, []))
        for key, route in sorted(census.routes.items())
        if route.code_class is None and key not in listed
    }
    BASELINE_PATH.write_text(
        json.dumps(baseline, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"wrote {len(baseline)} entries to {BASELINE_PATH}")


if __name__ == "__main__":  # pragma: no cover
    if sys.argv[1:] == ["--freeze"]:
        freeze()
    else:
        print("usage: python tests/unit/_route_census.py --freeze")
