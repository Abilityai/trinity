"""abilityai/trinity#4284 — no exception text in a 5xx answer, in either tree.

A handler written as ``raise HTTPException(500, detail=f"...: {e}")`` hands the
caller whatever the exception carried: a file path, an internal host, SQL
bound parameters, library internals, and on the git and GitHub paths a PAT.
CodeQL's ``py/stack-trace-exposure`` does not flag the ``HTTPException`` form,
so nothing but this guard keeps the class from coming back. The fix pattern
(#1917) is fixed words plus the exception's class name in the answer, and the
full text in the log (masked through ``utils/log_masking.masked_traceback``
where a credential is held).

The rule
--------
Both trees are walked with ``rglob``: ``src/backend/`` (except the private
``enterprise`` submodule, which owns its own guards, the #1677 convention) and
``docker/base-image/agent_server/``. A finding is an **answer** whose status may
be 5xx and whose text renders a **source**.

Answers (sinks):
- ``HTTPException(...)``, status at ``status_code=`` or position 0;
- ``JSONResponse`` / ``Response`` / ``PlainTextResponse`` / ``HTMLResponse``
  / ``ORJSONResponse``, status at ``status_code=`` or position 1;
- any **carrier**: a callable the two trees call, somewhere, with a literal
  HTTP error status (400-599) in a status position (``status_code=``,
  ``status=`` or a leading positional int). These are the platform's domain
  errors and outcome objects (``BindError``, ``ClientPortalError``,
  ``PaidTurnOutcome``, ``_http_error``, ...) that a router later turns into a
  response with ``status_code=e.status_code`` (G2). The carrier list is
  derived from the code, so a new carrier is covered the first time it is
  raised with a status;
- ``return <x>`` in a function that sets ``<obj>.status_code = <5xx>``.

Every argument other than the status is text: ``detail``, ``content``,
``payload``, ``headers``, a message.

A status "may be 5xx" when it contains a 5xx literal or ``status.HTTP_5xx_*``
(so ``status_code or 500`` and ``_MAP.get(code, 502)`` count), or is a local
assigned one. A status that is neither constant nor such a local is
**dynamic** (``response.status_code``, ``e.status_code``) unless an enclosing
branch bounds it below 500 (``if 400 <= r.status_code < 500:``, the ``else`` of
``if r.status_code >= 500``, or the statements after an ``if ... >= 500:``
that always raises or returns).

Sources:
- **exception text**: a name bound by ``except ... as e``, referenced inside
  that handler, in **any** arm, broad or narrow (G4). Narrow arms are not
  exempt because ``except OSError as e`` and ``except GitHubError as e`` carry
  paths and echoed credentials as readily as ``except Exception``; the narrow
  arms whose text is deliberate are listed below with their reason. Also
  ``traceback.format_exc()`` and its siblings, ``sys.exc_info()`` /
  ``sys.exception()``, and the exception parameter of a registered exception
  handler;
- **upstream body** (G3, G6): ``<x>.text``, ``<x>.content`` or ``<x>.json()``,
  i.e. a relayed response. An agent on an older base image still answers with
  its own exception text, and the backend's relay is the only thing between
  that text and the caller.

Text is followed through every spelling the code uses: f-strings (``!r`` and
``!s`` included), ``str()`` / ``repr()`` / ``format()`` / ``.format()``, ``%``,
``+``, ``.join``, slicing, method calls on the text (``str(e).lower()``),
dicts / lists / tuples, comprehensions, conditional expressions, ``or``, and
any call that is handed the text (``_scrub(str(e), pat)``), so a scrubber is
not an exemption. Locals are followed within the function (G1): an
assignment, ``+=``, ``:=``, a ``for`` target, ``x.append(...)`` and friends,
and ``d[k] = ...`` all carry the text into the name.

What is not text: ``type(e).__name__``, ``e.__class__.__name__`` (any
``.__name__`` / ``.__qualname__``), ``e.status_code``, ``e.status``,
``e.code``, ``e.errno``, ``e.detail`` (an ``HTTPException``'s own platform
detail), ``isinstance`` / ``len`` / ``bool`` / ``type`` / ``int`` results, and
comparisons.

Exception text at a **dynamic** status is a finding only when the exception
was caught as a broad or foreign class (``Exception``, ``httpx.HTTPError``).
A platform-defined class (any class defined in the two trees) is a carrier
whose text this guard checks where the carrier is built, so its forwarders
(``HTTPException(status_code=e.status_code, detail=e.detail)``, 80-odd
routes) are not findings. An upstream body at a dynamic status is always a
finding.

Out of the rule: a 2xx payload that carries exception text (G5, for example
the per-member ``failed[].reason`` of a system deploy), a 4xx (deliberate
domain errors such as ``ValueError`` -> 400 stay), log lines, and text that
crosses a function boundary through a return value (a relay assembled in a
service and raised in a router). A nested function does not see the
enclosing handler's exception name.

The allowlist
-------------
``_ALLOWED`` is permanent and every entry carries its reason. An entry names
the file, the enclosing function, the sink and the exact **source fragments**
that reach it (``e.msg``, ``e.strerror``, ``resp.json()``). So a site cannot
degrade while listed: adding ``str(e)`` next to an allowed ``e.msg`` adds the
fragment ``e``, which no entry allows. An entry that matches no site fails
(``test_allowlist_has_no_stale_entries``), so the list cannot rot. An entry
whose reason is "this class is only ever raised with platform text" names the
class, and ``test_listed_carriers_are_raised_only_with_platform_text`` checks
every construction of it in both trees.
"""

from __future__ import annotations

import ast
import functools
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, FrozenSet, Iterable, List, Optional, Set, Tuple

import pytest

_REPO = Path(__file__).resolve().parents[2]
_TREES = (_REPO / "src" / "backend", _REPO / "docker" / "base-image" / "agent_server")
_EXCLUDED_PREFIXES = (("src", "backend", "enterprise"),)

_CLEAN_ATTRS = frozenset(
    {"__name__", "__qualname__", "status_code", "status", "code", "errno", "detail"}
)
_NON_TEXT_CALLS = frozenset(
    {"type", "isinstance", "issubclass", "len", "bool", "id", "hash", "hasattr",
     "callable", "int", "float"}
)
_TRACEBACK_SOURCES = frozenset(
    {("traceback", "format_exc"), ("traceback", "format_exception"),
     ("traceback", "format_exception_only"), ("traceback", "format_tb"),
     ("traceback", "format_stack"), ("sys", "exc_info"), ("sys", "exception")}
)
_RELAY_ATTRS = frozenset({"text", "content"})
_CONTAINER_MUTATORS = frozenset(
    {"append", "extend", "insert", "add", "update", "setdefault", "appendleft"}
)
_LOG_METHODS = frozenset(
    {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
)
_RESPONSE_CLASSES = frozenset(
    {"JSONResponse", "Response", "PlainTextResponse", "HTMLResponse",
     "ORJSONResponse", "UJSONResponse"}
)
_BROAD = frozenset({"Exception", "BaseException"})
_NOT_CARRIERS = _LOG_METHODS | {"get", "pop", "setdefault", "range", "sleep",
                                "min", "max", "sum", "round"}

# A fragment is (source text, caught-as-broad-or-foreign). Relay fragments are
# always "foreign": the guard never trusts an upstream body.
Frag = Tuple[str, bool]


@dataclass(frozen=True)
class Finding:
    path: str        # repo-relative, posix
    line: int
    function: str    # dotted qualname of the enclosing function, or <module>
    sink: str        # callee name, or "return"
    kind: str        # "exception" | "upstream body"
    status: str      # the status expression as written
    fragments: FrozenSet[str]

    def render(self) -> str:
        return (f"{self.path}:{self.line} {self.function} -> {self.sink}"
                f"(status={self.status}) renders {self.kind}: "
                f"{', '.join(sorted(self.fragments))}")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _callee(func: ast.expr) -> Optional[str]:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _http_status_int(node: ast.expr, lo: int, hi: int) -> bool:
    if isinstance(node, ast.Constant) and type(node.value) is int:
        return lo <= node.value <= hi
    if isinstance(node, ast.Attribute) and node.attr.startswith("HTTP_"):
        try:
            code = int(node.attr.split("_")[1])
        except (IndexError, ValueError):
            return False
        return lo <= code <= hi
    return False


def _has_5xx(node: ast.expr) -> bool:
    return any(_http_status_int(n, 500, 599) for n in ast.walk(node)
               if isinstance(n, (ast.Constant, ast.Attribute)))


def _is_static_below_500(node: ast.expr) -> bool:
    """Constant, HTTP_* attribute, or a conditional/boolean of only those."""
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.Attribute) and node.attr.startswith("HTTP_"):
        return not _has_5xx(node)
    if isinstance(node, ast.IfExp):
        return _is_static_below_500(node.body) and _is_static_below_500(node.orelse)
    if isinstance(node, ast.BoolOp):
        return all(_is_static_below_500(v) for v in node.values)
    return False


def _exc_type_names(node: Optional[ast.expr]) -> List[str]:
    if node is None:
        return ["BaseException"]
    if isinstance(node, ast.Tuple):
        out: List[str] = []
        for elt in node.elts:
            out.extend(_exc_type_names(elt))
        return out
    name = _callee(node)
    return [name] if name else ["<expr>"]


# ---- branch bounds: which status expressions are known to be < 500 ---------

def _const_int(node: ast.expr) -> Optional[int]:
    if isinstance(node, ast.Constant) and type(node.value) is int:
        return node.value
    return None


def _below_500_when_true(test: ast.expr) -> Set[str]:
    out: Set[str] = set()
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And):
        for v in test.values:
            out |= _below_500_when_true(v)
        return out
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return _below_500_when_false(test.operand)
    if isinstance(test, ast.Compare):
        operands = [test.left] + list(test.comparators)
        for op, left, right in zip(test.ops, operands, operands[1:]):
            rv, lv = _const_int(right), _const_int(left)
            if rv is not None and lv is None:
                if (isinstance(op, ast.Lt) and rv <= 500) or (isinstance(op, ast.LtE) and rv < 500):
                    out.add(ast.dump(left))
            elif lv is not None and rv is None:
                if (isinstance(op, ast.Gt) and lv <= 500) or (isinstance(op, ast.GtE) and lv < 500):
                    out.add(ast.dump(right))
            if isinstance(op, ast.In) and isinstance(right, (ast.Tuple, ast.List, ast.Set)):
                vals = [_const_int(e) for e in right.elts]
                if vals and all(v is not None and v < 500 for v in vals):
                    out.add(ast.dump(left))
    return out


def _below_500_when_false(test: ast.expr) -> Set[str]:
    out: Set[str] = set()
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.Or):
        for v in test.values:
            out |= _below_500_when_false(v)
        return out
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return _below_500_when_true(test.operand)
    if isinstance(test, ast.Compare) and len(test.ops) == 1:
        left, op, right = test.left, test.ops[0], test.comparators[0]
        rv, lv = _const_int(right), _const_int(left)
        if rv is not None and lv is None:
            if (isinstance(op, ast.GtE) and rv <= 500) or (isinstance(op, ast.Gt) and rv < 500):
                out.add(ast.dump(left))
        elif lv is not None and rv is None:
            if (isinstance(op, ast.LtE) and lv <= 500) or (isinstance(op, ast.Lt) and lv < 500):
                out.add(ast.dump(right))
    return out


def _always_exits(stmts: List[ast.stmt]) -> bool:
    return bool(stmts) and isinstance(stmts[-1], (ast.Raise, ast.Return, ast.Continue, ast.Break))


# ---------------------------------------------------------------------------
# per-function scope
# ---------------------------------------------------------------------------

# Context in force at a node: the exception names bound by the handlers around
# it (name -> caught-as-broad-or-foreign), and the status expressions an
# enclosing branch bounds below 500.
_Ctx = Tuple[Tuple[Tuple[str, bool], ...], FrozenSet[str]]


class _Scope:
    def __init__(self, fn: ast.AST, qualname: str, own_classes: FrozenSet[str],
                 exc_param: Optional[Tuple[str, bool]] = None) -> None:
        self.fn = fn
        self.qualname = qualname
        self.own = own_classes
        self.exc_param = exc_param
        self.nodes: List[Tuple[ast.AST, _Ctx]] = []
        self._block(getattr(fn, "body", []), ((), frozenset()))

    def _foreign(self, handler: ast.ExceptHandler) -> bool:
        names = _exc_type_names(handler.type)
        return any(n in _BROAD or n not in self.own for n in names)

    def _block(self, stmts: Iterable[ast.stmt], ctx: _Ctx) -> None:
        excs, bounds = ctx
        for stmt in stmts:
            self._visit(stmt, (excs, bounds))
            if (isinstance(stmt, ast.If) and not stmt.orelse
                    and _always_exits(stmt.body)):
                bounds = bounds | frozenset(_below_500_when_false(stmt.test))

    def _visit(self, node: ast.AST, ctx: _Ctx) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            for d in getattr(node, "decorator_list", []):
                self._visit(d, ctx)
            return
        self.nodes.append((node, ctx))
        excs, bounds = ctx
        if isinstance(node, ast.Try) or type(node).__name__ == "TryStar":
            self._block(node.body, ctx)  # type: ignore[attr-defined]
            self._block(node.orelse, ctx)  # type: ignore[attr-defined]
            self._block(node.finalbody, ctx)  # type: ignore[attr-defined]
            for h in node.handlers:  # type: ignore[attr-defined]
                self.nodes.append((h, ctx))
                if h.type is not None:
                    self._visit(h.type, ctx)
                inner = excs + ((h.name, self._foreign(h)),) if h.name else excs
                self._block(h.body, (inner, bounds))
            return
        if isinstance(node, ast.If):
            self._visit(node.test, ctx)
            self._block(node.body, (excs, bounds | frozenset(_below_500_when_true(node.test))))
            self._block(node.orelse, (excs, bounds | frozenset(_below_500_when_false(node.test))))
            return
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.stmt, ast.match_case)):
                continue
            self._visit(child, ctx)
        for field in ("body", "orelse", "finalbody", "handlers", "cases"):
            seq = getattr(node, field, None)
            if isinstance(seq, list) and seq and isinstance(seq[0], (ast.stmt, ast.match_case)):
                if field == "cases":
                    for case in seq:
                        self._visit(case.pattern, ctx)
                        if case.guard is not None:
                            self._visit(case.guard, ctx)
                        self._block(case.body, ctx)
                else:
                    self._block(seq, ctx)


class _Taint:
    """Fragment computation for one scope and one source family."""

    def __init__(self, scope: _Scope, family: str) -> None:
        self.scope = scope
        self.family = family  # "exception" | "upstream body"
        self.locals: Dict[str, FrozenSet[Frag]] = {}
        self._fixpoint()

    def _exc_name(self, name: str, ctx: _Ctx) -> Optional[bool]:
        if self.family != "exception":
            return None
        for n, foreign in reversed(ctx[0]):
            if n == name:
                return foreign
        if self.scope.exc_param and self.scope.exc_param[0] == name:
            return self.scope.exc_param[1]
        return None

    def _root_exc(self, node: ast.expr, ctx: _Ctx) -> Optional[bool]:
        while isinstance(node, (ast.Attribute, ast.Subscript)):
            node = node.value
        if isinstance(node, ast.Name):
            return self._exc_name(node.id, ctx)
        return None

    def frags(self, e: Optional[ast.AST], ctx: _Ctx) -> FrozenSet[Frag]:
        if e is None or isinstance(e, (ast.Constant, ast.Compare, ast.Lambda)):
            return frozenset()
        if isinstance(e, ast.Name):
            foreign = self._exc_name(e.id, ctx)
            if foreign is not None:
                return frozenset({(e.id, foreign)})
            return self.locals.get(e.id, frozenset())
        if isinstance(e, ast.Attribute):
            if self.family == "exception" and e.attr in _CLEAN_ATTRS:
                return frozenset()
            if self.family == "upstream body" and e.attr in _RELAY_ATTRS:
                return frozenset({(ast.unparse(e), True)})
            foreign = self._root_exc(e, ctx)
            if foreign is not None:
                return frozenset({(ast.unparse(e), foreign)})
            return self.frags(e.value, ctx)
        if isinstance(e, ast.Call):
            f = e.func
            if isinstance(f, ast.Name) and f.id in _NON_TEXT_CALLS:
                return frozenset()
            if (isinstance(f, ast.Name) and f.id == "getattr" and len(e.args) >= 2
                    and isinstance(e.args[1], ast.Constant)):
                attr = e.args[1].value
                if self.family == "exception" and attr in _CLEAN_ATTRS:
                    return frozenset()
                if self.family == "upstream body" and attr in _RELAY_ATTRS:
                    return frozenset({(ast.unparse(e), True)})
            if (self.family == "exception" and isinstance(f, ast.Attribute)
                    and isinstance(f.value, ast.Name)
                    and (f.value.id, f.attr) in _TRACEBACK_SOURCES):
                return frozenset({(ast.unparse(e), True)})
            if (self.family == "upstream body" and isinstance(f, ast.Attribute)
                    and f.attr == "json" and not e.args):
                return frozenset({(ast.unparse(e), True)})
            parts: List[ast.AST] = list(e.args) + [k.value for k in e.keywords]
            if isinstance(f, ast.Attribute):
                parts.append(f.value)
            return self._union(parts, ctx)
        if isinstance(e, ast.UnaryOp):
            return frozenset() if isinstance(e.op, ast.Not) else self.frags(e.operand, ctx)
        if isinstance(e, ast.IfExp):
            return self.frags(e.body, ctx) | self.frags(e.orelse, ctx)
        if isinstance(e, ast.Dict):
            return self._union([k for k in e.keys if k is not None] + list(e.values), ctx)
        if isinstance(e, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
            return self.frags(e.elt, ctx) | self._union([g.iter for g in e.generators], ctx)
        if isinstance(e, ast.DictComp):
            return (self.frags(e.key, ctx) | self.frags(e.value, ctx)
                    | self._union([g.iter for g in e.generators], ctx))
        if isinstance(e, ast.Subscript):
            return self.frags(e.value, ctx)
        if isinstance(e, ast.FormattedValue):
            return self.frags(e.value, ctx)
        return self._union([c for c in ast.iter_child_nodes(e) if isinstance(c, ast.expr)], ctx)

    def _union(self, parts: Iterable[Optional[ast.AST]], ctx: _Ctx) -> FrozenSet[Frag]:
        out: FrozenSet[Frag] = frozenset()
        for p in parts:
            out |= self.frags(p, ctx)
        return out

    def _flows(self) -> List[Tuple[List[ast.expr], ast.AST, _Ctx]]:
        flows: List[Tuple[List[ast.expr], ast.AST, _Ctx]] = []
        for node, ctx in self.scope.nodes:
            if isinstance(node, ast.Assign):
                flows.append((list(node.targets), node.value, ctx))
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and node.value is not None:
                flows.append(([node.target], node.value, ctx))
            elif isinstance(node, ast.NamedExpr):
                flows.append(([node.target], node.value, ctx))
            elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
                flows.append(([node.target], node.iter, ctx))
            elif isinstance(node, ast.withitem) and node.optional_vars is not None:
                flows.append(([node.optional_vars], node.context_expr, ctx))
            elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                  and node.func.attr in _CONTAINER_MUTATORS):
                flows.append(([node.func.value],
                              ast.Tuple(elts=list(node.args) + [k.value for k in node.keywords]),
                              ctx))
        return flows

    @staticmethod
    def _target_names(target: ast.expr) -> List[str]:
        if isinstance(target, ast.Name):
            return [target.id]
        if isinstance(target, (ast.Tuple, ast.List)):
            out: List[str] = []
            for elt in target.elts:
                out.extend(_Taint._target_names(elt))
            return out
        if isinstance(target, ast.Starred):
            return _Taint._target_names(target.value)
        if isinstance(target, (ast.Subscript, ast.Attribute)):
            base = target.value
            while isinstance(base, (ast.Subscript, ast.Attribute)):
                base = base.value
            return [base.id] if isinstance(base, ast.Name) else []
        return []

    def _fixpoint(self) -> None:
        flows = self._flows()
        changed = True
        while changed:
            changed = False
            for targets, value, ctx in flows:
                fr = self.frags(value, ctx)
                if not fr:
                    continue
                for t in targets:
                    for name in self._target_names(t):
                        old = self.locals.get(name, frozenset())
                        if not fr <= old:
                            self.locals[name] = old | fr
                            changed = True


# ---------------------------------------------------------------------------
# tree-wide passes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _Module:
    rel: str
    tree: ast.Module


def _iter_modules(sources: Optional[Dict[str, str]] = None) -> List[_Module]:
    if sources is not None:
        return [_Module(rel, ast.parse(src)) for rel, src in sorted(sources.items())]
    return list(_real_modules())


@functools.lru_cache(maxsize=1)
def _real_modules() -> Tuple[_Module, ...]:
    out: List[_Module] = []
    for tree_root in _TREES:
        for path in sorted(tree_root.rglob("*.py")):
            rel_parts = path.relative_to(_REPO).parts
            if any(rel_parts[:len(p)] == p for p in _EXCLUDED_PREFIXES):
                continue
            out.append(_Module(path.relative_to(_REPO).as_posix(), ast.parse(path.read_text())))
    return tuple(out)


@dataclass
class _Facts:
    classes: Set[str]
    carriers: Dict[str, Set[str]]
    handlers: List[Tuple[Optional[str], Optional[str]]]  # (exception class, function)
    calls: List[Tuple[int, str]]                         # (line, callee)
    status_assign_lines: List[int]                       # `<x>.status_code = <5xx>`


def _module_facts(mod: _Module) -> _Facts:
    """Everything the tree-wide passes need, from ONE walk of the module."""
    facts = _Facts(set(), {}, [], [], [])
    aliases: Dict[str, str] = {}
    for n in ast.walk(mod.tree):
        if isinstance(n, ast.ClassDef):
            facts.classes.add(n.name)
        elif isinstance(n, ast.ImportFrom):
            for a in n.names:
                if a.asname:
                    aliases[a.asname] = a.name
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for d in n.decorator_list:
                if isinstance(d, ast.Call) and _callee(d.func) == "exception_handler" and d.args:
                    facts.handlers.append((_callee(d.args[0]), n.name))
        elif isinstance(n, ast.Assign):
            if any(isinstance(t, ast.Attribute) and t.attr == "status_code" for t in n.targets) \
                    and _has_5xx(n.value):
                facts.status_assign_lines.append(n.lineno)
        elif isinstance(n, ast.Call):
            name = _callee(n.func)
            if not name:
                continue
            facts.calls.append((n.lineno, name))
            if name == "add_exception_handler" and len(n.args) >= 2:
                facts.handlers.append((_callee(n.args[0]), _callee(n.args[1])))
            if name in _NOT_CARRIERS:
                continue
            for k in n.keywords:
                if k.arg in ("status_code", "status") and _http_status_int(k.value, 400, 599):
                    facts.carriers.setdefault(name, set()).add(f"kw:{k.arg}")
            for i, a in enumerate(n.args[:2]):
                if _http_status_int(a, 400, 599):
                    facts.carriers.setdefault(name, set()).add(f"pos:{i}")
                    break
    facts.handlers = [(aliases.get(c or "", c), aliases.get(f or "", f))
                      for c, f in facts.handlers]
    return facts


def _tree_facts(mods: List[_Module]) -> Tuple[List[_Facts], FrozenSet[str], Dict[str, Set[str]], Dict[str, bool]]:
    per_module = [_module_facts(m) for m in mods]
    # HTTPException's detail and headers are written where it is raised, and
    # those raises are checked here, so a caught HTTPException is a carrier.
    own = frozenset(set().union(*(f.classes for f in per_module)) | {"HTTPException"})
    carriers: Dict[str, Set[str]] = {"HTTPException": {"kw:status_code", "pos:0"}}
    for name in _RESPONSE_CLASSES:
        carriers[name] = {"kw:status_code", "pos:1"}
    handlers: Dict[str, bool] = {}
    for f in per_module:
        for name, positions in f.carriers.items():
            carriers.setdefault(name, set()).update(positions)
        for cls, fn in f.handlers:
            if fn:
                handlers[fn] = cls in _BROAD or cls not in own
    return per_module, own, carriers, handlers


def _scopes(mod: _Module, own: FrozenSet[str], handlers: Dict[str, bool],
            lines: Set[int]) -> List[_Scope]:
    """Scopes for the module body and each function whose span holds a candidate line."""
    scopes: List[_Scope] = [_Scope(mod.tree, "<module>", own)]

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qual = f"{prefix}{child.name}"
                span = range(child.lineno, (child.end_lineno or child.lineno) + 1)
                if any(line in span for line in lines):
                    param = None
                    if child.name in handlers:
                        args = child.args.posonlyargs + child.args.args
                        if len(args) >= 2:
                            param = (args[1].arg, handlers[child.name])
                    scopes.append(_Scope(child, qual, own, param))
                    walk(child, qual + ".")
            elif isinstance(child, ast.ClassDef):
                walk(child, f"{prefix}{child.name}.")
            elif isinstance(child, ast.stmt):
                walk(child, prefix)

    walk(mod.tree, "")
    return scopes


def _status_of(call: ast.Call, positions: Set[str]) -> Tuple[Optional[ast.expr], Optional[str], Optional[int]]:
    kws = {k.arg: k.value for k in call.keywords if k.arg}
    for key in ("status_code", "status"):
        if f"kw:{key}" in positions and key in kws:
            return kws[key], key, None
    for pos in sorted(p for p in positions if p.startswith("pos:")):
        i = int(pos.split(":")[1])
        if len(call.args) > i:
            return call.args[i], None, i
    return None, None, None


def _scan(mods: List[_Module]) -> List[Finding]:
    per_module, own, carriers, handlers = _tree_facts(mods)
    findings: List[Finding] = []
    for mod, facts in zip(mods, per_module):
        lines = {line for line, name in facts.calls if name in carriers}
        lines |= set(facts.status_assign_lines)
        if not lines:
            continue
        for scope in _scopes(mod, own, handlers, lines):
            findings.extend(_scan_scope(mod.rel, scope, carriers))
    return sorted(set(findings), key=lambda f: (f.path, f.line, f.sink, f.kind))


def _scan_scope(rel: str, scope: _Scope, carriers: Dict[str, Set[str]]) -> List[Finding]:
    candidates: List[Tuple[ast.AST, _Ctx, ast.expr, List[ast.AST], str]] = []
    # A status local is static-below-500 only when every assignment to it in
    # the function is a constant below 500. Unpacked, looped, augmented and
    # never-assigned names (globals, closures) are dynamic.
    status5_names: Set[str] = set()
    static_names: Set[str] = set()
    dyn_names: Set[str] = set()
    sets_5xx_status = False
    for node, _ctx in scope.nodes:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    if _has_5xx(node.value):
                        status5_names.add(t.id)
                    elif _is_static_below_500(node.value):
                        static_names.add(t.id)
                    else:
                        dyn_names.add(t.id)
                else:
                    dyn_names.update(_Taint._target_names(t))
                if (isinstance(t, ast.Attribute) and t.attr == "status_code"
                        and _has_5xx(node.value)):
                    sets_5xx_status = True
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
            target, value = node.target, node.value
            if isinstance(target, ast.Name) and value is not None:
                if _has_5xx(value):
                    status5_names.add(target.id)
                elif isinstance(node, ast.AnnAssign) and _is_static_below_500(value):
                    static_names.add(target.id)
                else:
                    dyn_names.add(target.id)
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            dyn_names.update(_Taint._target_names(node.target))
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            dyn_names.update(_Taint._target_names(node.optional_vars))
    for node, ctx in scope.nodes:
        if isinstance(node, ast.Return) and sets_5xx_status and node.value is not None:
            candidates.append((node, ctx, ast.Constant(value=500), [node.value], "return"))
            continue
        if not isinstance(node, ast.Call):
            continue
        name = _callee(node.func)
        if not name or name not in carriers:
            continue
        status, kw, pos = _status_of(node, carriers[name])
        if status is None:
            continue
        texts: List[ast.AST] = [a for i, a in enumerate(node.args) if i != pos]
        texts += [k.value for k in node.keywords if k.arg != kw]
        candidates.append((node, ctx, status, texts, name))
    if not candidates:
        return []
    taints = {fam: _Taint(scope, fam) for fam in ("exception", "upstream body")}
    out: List[Finding] = []
    for node, ctx, status, texts, sink in candidates:
        bounds = ctx[1]
        if _has_5xx(status) or (isinstance(status, ast.Name) and status.id in status5_names):
            mode = "5xx"
        elif _is_static_below_500(status):
            continue
        elif (isinstance(status, ast.Name) and status.id in static_names
              and status.id not in dyn_names):
            continue  # a local only ever assigned constants below 500
        elif ast.dump(status) in bounds:
            continue
        else:
            mode = "dynamic"
        for fam, taint in taints.items():
            frs = taint._union(texts, ctx)
            if mode == "dynamic" and fam == "exception":
                frs = frozenset(f for f in frs if f[1])
            if frs:
                out.append(Finding(rel, getattr(node, "lineno", 0), scope.qualname, sink, fam,
                                   ast.unparse(status), frozenset(f[0] for f in frs)))
    return out


def scan_sources(sources: Dict[str, str]) -> List[Finding]:
    """Scan in-memory modules (path -> source). Used by the rule's own tests."""
    return _scan(_iter_modules(sources))


@functools.lru_cache(maxsize=1)
def _real_findings() -> Tuple[Finding, ...]:
    return tuple(_scan(list(_real_modules())))


# ---------------------------------------------------------------------------
# carriers listed as "only ever raised with platform text"
# ---------------------------------------------------------------------------

def _constructions_with_foreign_text(mods: List[_Module], classes: Set[str]) -> List[str]:
    """Every call that builds one of ``classes`` (or a subclass defined in the
    trees) with exception text or an upstream body in its arguments."""
    per_module, own, _carriers, handlers = _tree_facts(mods)
    names = set(classes)
    changed = True
    while changed:
        changed = False
        for m in mods:
            for n in ast.walk(m.tree):
                if (isinstance(n, ast.ClassDef) and n.name not in names
                        and any(_callee(b) in names for b in n.bases)):
                    names.add(n.name)
                    changed = True
    out: List[str] = []
    for mod, facts in zip(mods, per_module):
        lines = {line for line, name in facts.calls if name in names}
        if not lines:
            continue
        for scope in _scopes(mod, own, handlers, lines):
            calls = [(n, ctx) for n, ctx in scope.nodes
                     if isinstance(n, ast.Call) and _callee(n.func) in names]
            if not calls:
                continue
            taints = [_Taint(scope, fam) for fam in ("exception", "upstream body")]
            for call, ctx in calls:
                args: List[ast.AST] = list(call.args) + [k.value for k in call.keywords]
                for taint in taints:
                    frs = taint._union(args, ctx)
                    if frs:
                        out.append(f"{mod.rel}:{call.lineno} {scope.qualname} builds "
                                   f"{_callee(call.func)} from "
                                   f"{', '.join(sorted(f[0] for f in frs))}")
    return sorted(out)


# ---------------------------------------------------------------------------
# the allowlist
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _Allowed:
    path: str
    function: str
    sink: str
    fragments: FrozenSet[str]
    reason: str
    count: int = 1
    # Classes whose every construction must carry platform text only: the
    # reason depends on it, so the guard checks it.
    platform_text_only: Tuple[str, ...] = ()

    def matches(self, f: Finding) -> bool:
        return (f.path, f.function, f.sink, f.fragments) == (
            self.path, self.function, self.sink, self.fragments)


_AS = "docker/base-image/agent_server/"
_BE = "src/backend/"

_ALLOWED: Tuple[_Allowed, ...] = (
    # --- agent server --------------------------------------------------------
    _Allowed(
        _AS + "routers/files.py", "_open_for_read", "HTTPException", frozenset({"e"}),
        "Narrow OSError from the no-follow open, after the ENOENT, link, ENOTDIR and "
        "ELOOP arms. Its text is the errno message and the path component the caller "
        "sent: nothing the caller did not already know. file-browser.md documents "
        "it as the OS-error row.",
    ),
    _Allowed(
        _AS + "routers/files.py", "_walk_dir_nofollow", "HTTPException",
        frozenset({"e.strerror"}),
        "Narrow OSError from mkdir and from the no-follow open of one component: "
        "`e.strerror` is the C library's errno string, next to the component the "
        "caller sent.",
        count=2,
    ),
    _Allowed(
        _AS + "services/codex_runtime.py", "CodexRuntime._execute_codex", "HTTPException",
        frozenset({"exc"}),
        "Narrow OSError from rewriting the agent's own $CODEX_HOME/.env "
        "(trinity-enterprise#1001): an operator-remediation 503 about a file in "
        "the agent's home, which the caller owns.",
    ),
    _Allowed(
        _AS + "services/gemini_runtime.py", "GeminiRuntime.execute_headless",
        "HTTPException", frozenset({"e"}),
        "Narrow TimeoutError whose only raisers are the platform's own "
        "`exceeded {n}s timeout` / `Task execution timed out after {n} seconds`, "
        "and the backend's failure classifiers key on 'timed out'.",
    ),
    _Allowed(
        _AS + "services/headless_executor.py", "execute_headless_task", "HTTPException",
        frozenset({"e"}),
        "Narrow RuntimeError filtered on its text; its only source is the platform's "
        "`Permission bypass failed: permissionMode=...`, which "
        "src/backend/services/failure_classifier.py and "
        "src/scheduler/failure_classifier.py match (SUB-003). Changing it breaks "
        "that classification.",
    ),
    # --- backend: platform text, checked at construction ----------------------
    _Allowed(
        _BE + "routers/agent_files.py", "refresh_agent_metric_definitions",
        "HTTPException", frozenset({"e.message", "e.reason"}),
        "RefreshUnavailable carries a reason code and a platform sentence (the "
        "parse error is named by class only); the 503 names why the registry was "
        "not refreshed.",
        platform_text_only=("RefreshUnavailable",),
    ),
    _Allowed(
        _BE + "routers/messages.py", "send_proactive_message", "HTTPException",
        frozenset({"e"}),
        "ChannelDeliveryError is raised only with constant sentences.",
        platform_text_only=("ChannelDeliveryError",),
    ),
    _Allowed(
        _BE + "routers/reports.py", "export_report", "HTTPException", frozenset({"e"}),
        "ExportUnavailable is raised only with constant text; the ImportError "
        "behind it is only its __cause__.",
        platform_text_only=("ExportUnavailable",),
    ),
    # --- backend: provider reasons the caller acts on --------------------------
    _Allowed(
        _BE + "services/voip_service.py", "VoipService._place_call_inner",
        "HTTPException", frozenset({"e.msg"}),
        "TwilioRestException.msg is Twilio's API reason (an unverified number, a "
        "missing geo permission) that the caller fixes in their Twilio account; "
        "every other exception answers the fixed 'Twilio rejected the call'.",
    ),
    _Allowed(
        _BE + "routers/telegram.py", "send_telegram_group_message", "HTTPException",
        frozenset({"response.json()"}),
        "The Telegram Bot API's own `description` field (chat not found, bot was "
        "blocked by the user), the reason the caller acts on. It is a provider "
        "reason, not exception text, and the bot token travels in the request URL, "
        "never in that body.",
    ),
    _Allowed(
        _BE + "client_portal/service.py", "transcribe_portal_audio", "ClientPortalError",
        frozenset({"resp.text"}),
        "The provider body only selects a category in "
        "stt_capability_service.record_live_failure (#2696); the answer is that "
        "category's fixed client sentence and status, never the body.",
    ),
    # --- backend: relays whose 5xx text is a reviewed decision ----------------
    _Allowed(
        _BE + "services/agent_skills_listing.py", "fetch_live", "SkillsListUnavailable",
        frozenset({"response.text"}),
        "The owner's agent page shows the agent's own error body, a reviewed "
        "decision pinned by test_ent754_skills_list_cache.py; the public link "
        "answers a fixed sentence (routers/public.py). The agent's skills route "
        "has no handler that renders an exception: an unforeseen error there is "
        "FastAPI's bare 500.",
    ),
    _Allowed(
        _BE + "services/chat_execution_service.py", "_proxy_terminate_and_finalize",
        "ChatDispatchError", frozenset({"response.json()"}),
        "The agent's terminate 500s are its own sentences: `termination_unconfirmed: "
        "...` (#3408), which tells the caller the termination can be retried, and "
        "`Termination failed: <class>` from services/process_registry.py.",
    ),
    # --- backend: deliberate operator-remediation text -------------------------
    _Allowed(
        _BE + "services/agent_service/deploy.py", "deploy_local_agent_logic",
        "HTTPException", frozenset({"e"}),
        "DEPLOYED_TEMPLATES_DIR_UNWRITABLE: a documented operator-remediation 500 "
        "(local-agent-deploy.md) about the platform's own deploy-store path.",
    ),
    _Allowed(
        _BE + "services/agent_service/deploy.py", "deploy_local_agent_logic",
        "HTTPException", frozenset({"e", "e.args"}),
        "TEMPLATE_COPY_FAILED (#2060): names the first five source -> destination "
        "pairs that failed to copy, documented in core-agent.md; the paths are the "
        "caller's own archive entries and the deploy store.",
    ),
    _Allowed(
        _BE + "services/system_service.py", "deploy_manifest", "HTTPException",
        frozenset({"e"}),
        "Strict-mode system deploy: `reason` is _failure_reason(e), the one "
        "sanitized, length-capped exit point that the non-strict response also "
        "puts in failed[].reason (ent#125 parity).",
    ),
    _Allowed(
        _BE + "services/system_service.py", "deploy_manifest", "SystemDeployFailure",
        frozenset({"e"}),
        "A per-member report row returned in a 2xx deploy response; its reason is "
        "_failure_reason(e), sanitized and capped (ent#125).",
        count=2,
    ),
    _Allowed(
        _BE + "services/system_service.py", "_preflight_template", "SystemDeployFailure",
        frozenset({"e"}),
        "The dry-run preview's per-member row, built by the same _failure_reason "
        "as the deploy loop (ent#125).",
    ),
    # --- held unedited --------------------------------------------------------
    _Allowed(
        _BE + "routers/slack.py", "connect_slack", "HTTPException", frozenset({"e"}),
        "routers/slack.py stays unchanged until after 2026-11-19, so the "
        "code-scanning dismissals reviewed against its current source stay valid "
        "until then. Fix it the #1917 way after that date and drop this entry.",
    ),
)


def _unlisted(findings: Iterable[Finding]) -> List[Finding]:
    return [f for f in findings if not any(a.matches(f) for a in _ALLOWED)]


# ---------------------------------------------------------------------------
# the tree
# ---------------------------------------------------------------------------

def test_no_5xx_answer_renders_exception_text():
    bad = _unlisted(_real_findings())
    assert not bad, (
        "A 5xx answer renders exception text or a relayed upstream body "
        "(abilityai/trinity#4284). Answer with fixed words plus the class name, "
        "e.g. f\"Failed to X ({type(e).__name__} — details in backend logs)\", and "
        "log the full exception (utils/log_masking.masked_traceback where a "
        "credential is held). For a relay, answer a fixed sentence when the "
        "upstream status is >= 500. Only a deliberate, reasoned case goes in "
        "_ALLOWED.\n  " + "\n  ".join(f.render() for f in bad)
    )


def test_allowlist_has_no_stale_entries():
    findings = _real_findings()
    stale = []
    for a in _ALLOWED:
        n = sum(1 for f in findings if a.matches(f))
        if n != a.count:
            stale.append(f"{a.path} {a.function} {a.sink} {sorted(a.fragments)}: "
                         f"expected {a.count} site(s), found {n}")
    assert not stale, (
        "An _ALLOWED entry no longer matches the code. Remove it if the site was "
        "fixed, or update it if the site moved:\n  " + "\n  ".join(stale)
    )


def test_listed_carriers_are_raised_only_with_platform_text():
    classes = {c for a in _ALLOWED for c in a.platform_text_only}
    bad = _constructions_with_foreign_text(list(_real_modules()), classes)
    assert not bad, (
        "A class an _ALLOWED reason calls 'platform text only' is built from "
        "exception text or an upstream body:\n  " + "\n  ".join(bad)
    )


def test_allowlist_entries_are_reasoned_and_distinct():
    keys = [(a.path, a.function, a.sink, a.fragments) for a in _ALLOWED]
    assert len(keys) == len(set(keys))
    for a in _ALLOWED:
        assert len(a.reason) >= 60, a
        assert a.count >= 1 and a.fragments, a
        assert (_REPO / a.path).is_file(), a.path


def test_both_trees_are_walked_recursively():
    rels = {m.rel for m in _real_modules()}
    # Nested packages on both sides: a one-level glob would miss these.
    for nested in ("src/backend/routers/settings/credentials.py",
                   "src/backend/services/agent_service/files.py",
                   "src/backend/client_portal/service.py",
                   "docker/base-image/agent_server/routers/files.py",
                   "docker/base-image/agent_server/services/headless_executor.py"):
        assert nested in rels, nested
    assert not any(r.startswith("src/backend/enterprise/") for r in rels)
    assert sum(r.startswith("docker/base-image/agent_server/") for r in rels) >= 20
    assert sum(r.startswith("src/backend/") for r in rels) >= 500


# ---------------------------------------------------------------------------
# the rule, one spelling at a time
# ---------------------------------------------------------------------------

_HEAD = (
    "import sys, traceback, httpx\n"
    "from fastapi import HTTPException, status\n"
    "from fastapi.responses import JSONResponse, PlainTextResponse, Response\n"
    "class BindError(Exception):\n"
    "    def __init__(self, status_code, code, message):\n"
    "        self.status_code, self.code, self.message = status_code, code, message\n"
    "class DomainRefused(Exception):\n"
    "    pass\n"
    "def _scrub(text, secret):\n"
    "    return text\n"
    "def _known_status():\n"
    "    # A carrier is recognised by a literal-status raise somewhere in the tree.\n"
    "    raise BindError(409, 'BIND_CONFLICT', 'already bound')\n"
)


def _wrap(body: str, catch: str = "Exception as e") -> str:
    lines = "\n".join("        " + ln for ln in body.strip("\n").splitlines())
    return _HEAD + (
        "async def handler(pat, r):\n"
        "    try:\n"
        "        work()\n"
        f"    except {catch}:\n"
        f"{lines}\n"
    )


def _findings(src: str) -> List[Finding]:
    return scan_sources({"src/backend/routers/sample.py": src})


# One per spelling family. Each is red against the guard; the mutation table in
# the PR re-introduces the same families into real files.
_FLAGGED = {
    "f-string": _wrap('raise HTTPException(status_code=500, detail=f"Failed: {e}")'),
    "f-string !r": _wrap('raise HTTPException(500, f"Failed: {e!r}")'),
    "str()": _wrap("raise HTTPException(status_code=500, detail=str(e))"),
    "repr()": _wrap("raise HTTPException(status_code=502, detail=repr(e))"),
    "percent": _wrap('raise HTTPException(status_code=500, detail="Failed: %s" % e)'),
    ".format": _wrap('raise HTTPException(status_code=500, detail="Failed: {}".format(e))'),
    "format()": _wrap('raise HTTPException(status_code=500, detail=format(e))'),
    "concatenation": _wrap('raise HTTPException(status_code=500, detail="Failed: " + str(e))'),
    "join": _wrap('raise HTTPException(status_code=500, detail="; ".join([str(e)]))'),
    "slice": _wrap("raise HTTPException(status_code=500, detail=str(e)[:200])"),
    "method on text": _wrap("raise HTTPException(status_code=500, detail=str(e).splitlines()[0])"),
    "e.args": _wrap("raise HTTPException(status_code=500, detail=e.args[0])"),
    "attribute": _wrap('raise HTTPException(status_code=500, detail=f"git: {e.stderr}")'),
    "getattr": _wrap('raise HTTPException(status_code=500, detail=getattr(e, "message", ""))'),
    "local alias (G1)": _wrap('msg = f"Failed: {e}"\nraise HTTPException(status_code=500, detail=msg)'),
    "scrubbed alias (G1)": _wrap('safe = _scrub(str(e), pat)\nraise HTTPException(500, detail=f"x: {safe}")'),
    "augmented": _wrap('msg = "Failed"\nmsg += str(e)\nraise HTTPException(500, msg)'),
    "walrus": _wrap('raise HTTPException(500, (m := str(e)))'),
    "list append": _wrap('errs = []\nerrs.append(str(e))\nraise HTTPException(500, ", ".join(errs))'),
    "subscript assign": _wrap('body = {}\nbody["error"] = str(e)\nraise HTTPException(500, body)'),
    "comprehension": _wrap('raise HTTPException(500, [x for x in str(e).split()])'),
    "conditional": _wrap('raise HTTPException(500, str(e) if pat else "Failed")'),
    "or": _wrap('raise HTTPException(500, str(e) or "Failed")'),
    "dict detail": _wrap('raise HTTPException(500, detail={"error": str(e), "code": "X"})'),
    "headers": _wrap('raise HTTPException(500, "Failed", headers={"X-Error": str(e)})'),
    "JSONResponse content": _wrap('return JSONResponse(status_code=500, content={"error": str(e)})'),
    "JSONResponse positional": _wrap('return JSONResponse({"detail": f"{e}"}, 503)'),
    "PlainTextResponse": _wrap('return PlainTextResponse(str(e), status_code=500)'),
    "status.HTTP_5xx": _wrap('raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e))'),
    "status local": _wrap('code = 502\nraise HTTPException(status_code=code, detail=str(e))'),
    "status or 500": _wrap('raise HTTPException(status_code=pat or 500, detail=str(e))'),
    "status map default": _wrap('raise HTTPException(status_code=MAP.get(pat, 502), detail=str(e))'),
    "conditional status": _wrap('raise HTTPException(409 if pat else 503, str(e))'),
    "narrow arm (G4)": _wrap('raise HTTPException(500, f"Failed: {e}")', catch="OSError as e"),
    "bare re-bound": _wrap('raise HTTPException(500, f"Failed: {e}")', catch="(ValueError, KeyError) as e"),
    "traceback.format_exc": _wrap("raise HTTPException(500, traceback.format_exc())", catch="Exception"),
    "sys.exc_info": _wrap("raise HTTPException(500, str(sys.exc_info()[1]))", catch="Exception"),
    "carrier positional (G2)": _wrap('raise BindError(502, "BIND_X", f"Could not bind: {e}")'),
    "carrier keyword (G2)": _wrap('return Outcome(status_code=500, payload={"error": str(e)})'),
    "foreign exception at a dynamic status": _wrap(
        "raise HTTPException(status_code=e.response.status_code, detail=str(e))",
        catch="httpx.HTTPStatusError as e"),
    "broad exception at a dynamic status": _wrap(
        "raise HTTPException(status_code=pat, detail=str(e))"),
    "relay .text (G3)": _wrap("raise HTTPException(status_code=r.status_code, detail=r.text)"),
    "relay .json() (G3)": _wrap(
        'raise HTTPException(status_code=r.status_code, detail=r.json().get("detail"))'),
    "relay at 502 (G3)": _wrap('raise HTTPException(502, f"Agent failed: {r.text[:500]}")'),
    "relay into a carrier (G3)": _wrap('raise BindError(r.status_code, "X", r.content)'),
    "response.status_code = 500": _HEAD + (
        "async def handler(response, r):\n"
        "    try:\n"
        "        work()\n"
        "    except Exception as e:\n"
        "        response.status_code = 500\n"
        '        return {"error": str(e)}\n'
    ),
    "exception handler parameter": _HEAD + (
        "async def on_error(request, exc):\n"
        '    return JSONResponse(status_code=500, content={"detail": str(exc)})\n'
        "def register(app):\n"
        "    app.add_exception_handler(Exception, on_error)\n"
    ),
}

_CLEAN = {
    "class name": _wrap(
        'raise HTTPException(500, f"Failed ({type(e).__name__} — details in backend logs)")'),
    "__class__.__name__": _wrap('raise HTTPException(500, f"Failed: {e.__class__.__name__}")'),
    "HTTPException detail": _wrap('raise HTTPException(500, e.detail)', catch="HTTPException as e"),
    "status code only": _wrap('raise HTTPException(502, f"Agent answered {e.status_code}")'),
    "a 4xx": _wrap('raise HTTPException(status_code=400, detail=str(e))', catch="ValueError as e"),
    "a 4xx local": _wrap('code = 409\nraise HTTPException(status_code=code, detail=str(e))',
                         catch="ValueError as e"),
    "a 4xx conditional": _wrap('raise HTTPException(409 if pat else 400, str(e))',
                               catch="ValueError as e"),
    "own carrier forwarded": _wrap(
        "raise HTTPException(status_code=e.status_code, detail=e.message)",
        catch="BindError as e"),
    "own carrier str forwarded": _wrap(
        "raise HTTPException(status_code=e.status_code, detail=str(e))",
        catch="DomainRefused as e"),
    "text only in the log": _wrap(
        'logger.error("failed: %s", e)\nraise HTTPException(500, "Failed")'),
    "text only in a condition": _wrap(
        'if "timed out" in str(e):\n    raise HTTPException(504, "Timed out")\n'
        'raise HTTPException(500, "Failed")'),
    "isinstance": _wrap('raise HTTPException(500, "Failed", headers={"X": str(isinstance(e, OSError))})'),
    "relay below 500 in the branch": _wrap(
        "if 400 <= r.status_code < 500:\n"
        "    raise HTTPException(status_code=r.status_code, detail=r.text)\n"
        'raise HTTPException(502, "Agent failed")'),
    "relay after a 5xx early exit": _wrap(
        "if r.status_code >= 500:\n"
        '    raise HTTPException(502, f"Agent failed (HTTP {r.status_code})")\n'
        "raise HTTPException(status_code=r.status_code, detail=r.text)"),
    "relay in the else of a 5xx test": _wrap(
        "if r.status_code >= 500:\n"
        '    detail = "Agent failed"\n'
        "else:\n"
        "    raise HTTPException(status_code=r.status_code, detail=r.text)"),
    "relay at a 4xx": _wrap("raise HTTPException(404, r.text)"),
}


@pytest.mark.parametrize("name", sorted(_FLAGGED))
def test_rule_flags_each_spelling(name):
    assert _findings(_FLAGGED[name]), f"{name!r} was not flagged"


@pytest.mark.parametrize("name", sorted(_CLEAN))
def test_rule_leaves_safe_forms_alone(name):
    assert not _findings(_CLEAN[name]), [f.render() for f in _findings(_CLEAN[name])]


def test_exception_name_outside_its_handler_is_not_text():
    src = _HEAD + (
        "async def handler(items):\n"
        "    try:\n"
        "        work()\n"
        "    except Exception as e:\n"
        "        pass\n"
        "    for e in items:\n"
        "        raise HTTPException(500, f'bad item {e}')\n"
    )
    assert not _findings(src)


def test_an_allowed_site_cannot_degrade_while_listed():
    """Adding another spelling to an allowed sink changes its fragments, so the
    entry stops covering it and the site fails again."""
    path = _REPO / "src/backend/services/voip_service.py"
    src = path.read_text()
    needle = 'detail = f"Twilio error: {e.msg}"'
    assert needle in src
    mutated = src.replace(needle, 'detail = f"Twilio error: {e.msg} ({e})"')
    found = scan_sources({"src/backend/services/voip_service.py": mutated})
    voip = [f for f in found if f.function == "VoipService._place_call_inner"]
    assert voip and _unlisted(voip), [f.render() for f in voip]
