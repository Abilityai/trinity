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
  response with ``status_code=e.status_code``. The carrier list is derived
  from the code, so a new carrier is covered the first time it is raised with
  a status;
- an ``HTTPException`` subclass defined in the trees. One whose ``__init__``
  fixes the status answers at that status (500 when it cannot be read), with
  every argument as text;
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
  that handler, in **any** arm, broad or narrow. Narrow arms are not
  exempt because ``except OSError as e`` and ``except GitHubError as e`` carry
  paths and echoed credentials as readily as ``except Exception``; the narrow
  arms whose text is deliberate are listed below with their reason. Also
  ``traceback.format_exc()`` and its siblings, ``sys.exc_info()`` /
  ``sys.exception()`` (also when imported by name, ``from traceback import
  format_exc``), and the exception parameter of a registered exception
  handler;
- **upstream body**: ``<x>.text``, ``<x>.content``, ``<x>.json()``,
  ``await <x>.text()``, the streaming iterators (``iter_text``, ``aiter_raw``,
  ...), and ``read()`` / ``aread()`` on a receiver named like a response
  (``r``, ``resp``, ``response``, ``*_response``), i.e. a relayed answer. An
  agent on an older base image still answers with its own exception text, and
  the backend's relay is the only thing between that text and the caller;
- **a parameter**: a sink that renders one of its own function's parameters
  is judged at each caller, by what the caller passes (the parameter is
  replaced by the caller's argument, so ``exc.detail`` is ``e.detail`` with
  the caller's ``e``). A status that is a parameter is read at the caller the
  same way, so ``def _fail(code, msg): raise HTTPException(code, msg)`` called
  as ``_fail(400, str(e))`` is a 4xx and as ``_fail(500, str(e))`` a finding.
  Callers are found by name: anywhere in the trees when the name is defined
  once, in the same module otherwise. Exception text is followed up to three
  calls back, an upstream body one call back.

Text is followed through every spelling the code uses: f-strings (``!r`` and
``!s`` included), ``str()`` / ``repr()`` / ``format()`` / ``.format()``, ``%``,
``+``, ``.join``, slicing, method calls on the text (``str(e).lower()``),
dicts / lists / tuples, comprehensions, conditional expressions, ``or``, and
any call that is handed the text (``_scrub(str(e), pat)``), so a scrubber is
not an exemption. Locals are followed within the function: an
assignment, ``+=``, ``:=``, a ``for`` target, ``x.append(...)`` and friends,
and ``d[k] = ...`` all carry the text into the name.

What is not text: ``type(e).__name__``, ``e.__class__.__name__`` (any
``.__name__`` / ``.__qualname__``), ``e.status_code``, ``e.status``,
``e.code``, ``e.errno``, ``isinstance`` / ``len`` / ``bool`` / ``type`` /
``int`` results, and comparisons. ``e.detail`` is not text only when ``e``
was caught as an ``HTTPException`` (or a subclass the trees define), whose
details this guard checks where they are raised; on any other class it is
that class's text. ``getattr(e, "<attr>", default)`` follows the same rule
for the attribute, and its default is text.

Exception text at a **dynamic** status is a finding only when the exception
was caught as a broad or foreign class (``Exception``, ``httpx.HTTPError``).
A platform-defined class (any class defined in the two trees) is a carrier
whose text this guard checks where the carrier is built, so its forwarders
(``HTTPException(status_code=e.status_code, detail=e.detail)``, 80-odd
routes) are not findings. An upstream body at a dynamic status is always a
finding.

Out of the rule: a 2xx payload that carries exception text, a 4xx
(deliberate domain errors such as ``ValueError`` -> 400 stay), log lines, and
text that crosses a function boundary through a return value or an object's
attribute (a relay assembled in a service and raised in a router, an
exception stored on ``self``). A nested function does not see the enclosing
handler's exception name.

The allowlist
-------------
``_ALLOWED`` is permanent and every entry carries its reason. An entry names
the file, the enclosing function, the sink and the exact **source fragments**
that reach it (``e.msg``, ``e.strerror``, ``resp.json()``). So a site cannot
degrade while listed: adding ``str(e)`` next to an allowed ``e.msg`` adds the
fragment ``e``, which no entry allows. An entry that matches no site fails
(``test_allowlist_has_no_stale_entries``), so the list cannot rot. An entry
that holds a site for a while carries the date it ends
(``test_held_entries_have_not_expired``).

A reason that rests on a class is checked at every construction of it in
both trees, with the class's subclasses, and with any platform class whose
text a construction uses (that class is checked the same way):
- ``platform_text_only``: every argument is platform text. A parameter is
  followed to each caller, and a parameter no caller can be seen filling
  counts as foreign, so building the class in a helper from caller-supplied
  text does not pass (``test_listed_carriers_are_raised_only_with_platform_text``);
- ``no_exception_text``: the weaker claim, for a class that deliberately
  carries a peer's or a caller's words: no argument is exception text, at the
  construction or at a caller that can be seen
  (``test_listed_carriers_carry_no_exception_text``). A construction that
  does carry it is pinned by name with its reason.

``_CARRIED`` lists classes whose text reaches a 5xx by a return value or an
attribute, which the scan does not follow, so no entry sits at their sink;
each is given the ``no_exception_text`` check with its reason.
"""

from __future__ import annotations

import ast
import copy
import datetime
import functools
import re
from dataclasses import dataclass, field
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
# Methods whose result is a relayed body: httpx's ``json()`` and aiohttp's
# ``await r.text()`` on anything, ``read()`` / ``aread()`` on a receiver named
# like a response (a file or an upload is read the same way), and the
# streaming iterators.
_RELAY_METHODS = frozenset({"json", "text"})
_RELAY_READS = frozenset({"read", "aread"})
_RESPONSE_NAME = re.compile(r"(r|res|resp|response|reply|upstream)\Z|.*_(resp|response)\Z")
_RELAY_ITERATORS = frozenset(
    {"iter_text", "iter_bytes", "iter_lines", "iter_raw",
     "aiter_text", "aiter_bytes", "aiter_lines", "aiter_raw"}
)
# The names an ``HTTPException``'s ``detail`` is platform text under: the
# FastAPI and Starlette classes, plus every subclass the trees define.
_HTTP_EXCEPTIONS = frozenset({"HTTPException", "StarletteHTTPException"})
# How far a parameter is followed back through its callers.
_MAX_CALL_DEPTH = 3
_FAMILIES = ("exception", "upstream body", "parameter")
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
    # Where a parameter in the answer was handed the text, for a finding
    # that crosses a call ("path:line function"); empty otherwise.
    via: str = field(default="", compare=False)

    def render(self) -> str:
        via = f" (handed it at {self.via})" if self.via else ""
        return (f"{self.path}:{self.line} {self.function} -> {self.sink}"
                f"(status={self.status}) renders {self.kind}: "
                f"{', '.join(sorted(self.fragments))}{via}")


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
# it (name, caught-as-broad-or-foreign, the classes caught), and the status
# expressions an enclosing branch bounds below 500.
_Exc = Tuple[str, bool, Tuple[str, ...]]
_Ctx = Tuple[Tuple[_Exc, ...], FrozenSet[str]]


class _Scope:
    def __init__(self, fn: ast.AST, qualname: str, own_classes: FrozenSet[str],
                 exc_param: Optional[_Exc] = None, *,
                 http_classes: FrozenSet[str] = _HTTP_EXCEPTIONS,
                 tb_names: FrozenSet[str] = frozenset(),
                 is_method: bool = False) -> None:
        self.fn = fn
        self.qualname = qualname
        self.own = own_classes
        self.exc_param = exc_param
        self.http = http_classes
        # Local names a module imported from ``traceback`` / ``sys`` that
        # render the exception in flight (``from traceback import format_exc``).
        self.tb_names = tb_names
        self.params: Tuple[str, ...] = ()
        self.param_star: FrozenSet[str] = frozenset()
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            a = fn.args
            names = [x.arg for x in a.posonlyargs + a.args]
            if is_method and names:
                names = names[1:]
            self.params = tuple(names + [x.arg for x in a.kwonlyargs])
            self.param_star = frozenset(x.arg for x in (a.vararg, a.kwarg) if x is not None)
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
                inner = (excs + ((h.name, self._foreign(h), tuple(_exc_type_names(h.type))),)
                         if h.name else excs)
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
        for block in ("body", "orelse", "finalbody", "handlers", "cases"):
            seq = getattr(node, block, None)
            if isinstance(seq, list) and seq and isinstance(seq[0], (ast.stmt, ast.match_case)):
                if block == "cases":
                    for case in seq:
                        self._visit(case.pattern, ctx)
                        if case.guard is not None:
                            self._visit(case.guard, ctx)
                        self._block(case.body, ctx)
                else:
                    self._block(seq, ctx)


class _Taint:
    """Fragment computation for one scope and one source family.

    Families: ``exception`` (the exception in flight), ``upstream body`` (a
    relayed answer) and ``parameter`` (the enclosing function's own
    parameters, which the callers fill in; see ``_through_callers``).
    """

    def __init__(self, scope: _Scope, family: str) -> None:
        self.scope = scope
        self.family = family
        self.locals: Dict[str, FrozenSet[Frag]] = {}
        # fragment text -> the access expression it came from (parameter
        # family) or the classes caught (exception family).
        self.nodes: Dict[str, ast.expr] = {}
        self.classes: Dict[str, Tuple[str, ...]] = {}
        self._fixpoint()

    def _exc_entry(self, name: str, ctx: _Ctx) -> Optional[Tuple[bool, Tuple[str, ...]]]:
        if self.family != "exception":
            return None
        for n, foreign, classes in reversed(ctx[0]):
            if n == name:
                return foreign, classes
        if self.scope.exc_param and self.scope.exc_param[0] == name:
            return self.scope.exc_param[1], self.scope.exc_param[2]
        return None

    def _is_param(self, name: str, ctx: _Ctx) -> bool:
        return (self.family == "parameter" and name in self.scope.params
                and not any(n == name for n, _f, _c in ctx[0]))

    @staticmethod
    def _root_name(node: ast.expr) -> Optional[str]:
        while True:
            if isinstance(node, (ast.Attribute, ast.Subscript)):
                node = node.value
            elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                  and node.func.id == "getattr" and node.args):
                node = node.args[0]
            else:
                return node.id if isinstance(node, ast.Name) else None

    def _source(self, e: ast.expr, root: Optional[str], ctx: _Ctx) -> FrozenSet[Frag]:
        """The fragment for an access rooted at an exception name or a
        parameter, or nothing when ``root`` is neither."""
        if root is None:
            return frozenset()
        entry = self._exc_entry(root, ctx)
        if entry is not None:
            text = ast.unparse(e)
            self.classes[text] = entry[1]
            return frozenset({(text, entry[0])})
        if self._is_param(root, ctx):
            text = ast.unparse(e)
            self.nodes[text] = e
            return frozenset({(text, True)})
        return frozenset()

    def _detail_is_platform(self, root: Optional[str], ctx: _Ctx) -> bool:
        """``.detail`` is platform text only on an ``HTTPException``: its
        details are checked where it is raised. On any other class it is
        that class's text."""
        if root is None:
            return False
        entry = self._exc_entry(root, ctx)
        return entry is not None and bool(entry[1]) and all(c in self.scope.http for c in entry[1])

    def _clean_attr(self, attr: str, root: Optional[str], ctx: _Ctx) -> bool:
        if attr not in _CLEAN_ATTRS or self.family == "upstream body":
            return False
        if attr != "detail":
            return True
        # A parameter's ``.detail`` is decided at each caller, where the
        # argument's class is known.
        return self.family == "exception" and self._detail_is_platform(root, ctx)

    def frags(self, e: Optional[ast.AST], ctx: _Ctx) -> FrozenSet[Frag]:
        if e is None or isinstance(e, (ast.Constant, ast.Compare, ast.Lambda)):
            return frozenset()
        if isinstance(e, ast.Name):
            found = self._source(e, e.id, ctx)
            if found:
                return found
            return self.locals.get(e.id, frozenset())
        if isinstance(e, ast.Attribute):
            root = self._root_name(e)
            if self._clean_attr(e.attr, root, ctx):
                return frozenset()
            if self.family == "upstream body" and e.attr in _RELAY_ATTRS:
                return frozenset({(ast.unparse(e), True)})
            found = self._source(e, root, ctx)
            if found:
                return found
            return self.frags(e.value, ctx)
        if isinstance(e, ast.Call):
            f = e.func
            if isinstance(f, ast.Name) and f.id in _NON_TEXT_CALLS:
                return frozenset()
            if (isinstance(f, ast.Name) and f.id == "getattr" and len(e.args) >= 2
                    and isinstance(e.args[1], ast.Constant)):
                attr = e.args[1].value
                root = self._root_name(e.args[0])
                if (self.family == "parameter" and isinstance(e.args[0], ast.Name)
                        and self._is_param(e.args[0].id, ctx)):
                    if isinstance(attr, str) and attr in _CLEAN_ATTRS and attr != "detail":
                        return self._union(e.args[2:], ctx)
                    return self._source(e, root, ctx) | self._union(e.args[2:], ctx)
                if isinstance(attr, str) and self._clean_attr(attr, root, ctx):
                    # The attribute is not text, but the default is rendered
                    # whenever the attribute is missing.
                    return self._union(e.args[2:], ctx)
                if self.family == "upstream body" and attr in _RELAY_ATTRS:
                    return frozenset({(ast.unparse(e), True)})
            if (self.family == "exception" and isinstance(f, ast.Attribute)
                    and isinstance(f.value, ast.Name)
                    and (f.value.id, f.attr) in _TRACEBACK_SOURCES):
                return frozenset({(ast.unparse(e), True)})
            if (self.family == "exception" and isinstance(f, ast.Name)
                    and f.id in self.scope.tb_names):
                return frozenset({(ast.unparse(e), True)})
            if (self.family == "upstream body" and isinstance(f, ast.Attribute)
                    and ((f.attr in _RELAY_METHODS and not e.args)
                         or f.attr in _RELAY_ITERATORS
                         or (f.attr in _RELAY_READS and _RESPONSE_NAME.match(
                             self._root_name(f.value) or "")))):
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
            found = self._source(e, self._root_name(e), ctx)
            if found and self.family == "parameter":
                return found
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
    bases: Dict[str, Set[str]] = field(default_factory=dict)   # class -> base names
    class_defs: Dict[str, ast.ClassDef] = field(default_factory=dict)
    tb_names: Set[str] = field(default_factory=set)


def _module_facts(mod: _Module) -> _Facts:
    """Everything the tree-wide passes need, from ONE walk of the module."""
    facts = _Facts(set(), {}, [], [], [])
    aliases: Dict[str, str] = {}
    for n in ast.walk(mod.tree):
        if isinstance(n, ast.ClassDef):
            facts.classes.add(n.name)
            facts.bases.setdefault(n.name, set()).update(
                b for b in (_callee(x) for x in n.bases) if b)
            facts.class_defs.setdefault(n.name, n)
        elif isinstance(n, ast.ImportFrom):
            for a in n.names:
                if a.asname:
                    aliases[a.asname] = a.name
                if (n.module, a.name) in _TRACEBACK_SOURCES:
                    facts.tb_names.add(a.asname or a.name)
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


@dataclass
class _Tree:
    per_module: List[_Facts]
    own: FrozenSet[str]
    carriers: Dict[str, Set[str]]
    handlers: Dict[str, Tuple[bool, str]]          # function -> (foreign, class)
    http: FrozenSet[str]                           # HTTPException and its subclasses
    # An HTTPException subclass whose __init__ fixes the status: the status it
    # fixes (None when it cannot be read, which is treated as 5xx).
    http_fixed: Dict[str, Optional[ast.expr]]


def _fixed_http_status(cls: ast.ClassDef) -> Tuple[bool, Optional[ast.expr]]:
    """(fixes the status, the status) for an HTTPException subclass: True when
    its own ``__init__`` passes a status to ``super().__init__`` that is not
    one of its parameters."""
    for item in cls.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == "__init__":
            params = {a.arg for a in item.args.posonlyargs + item.args.args + item.args.kwonlyargs}
            for n in ast.walk(item):
                if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                        and n.func.attr == "__init__"):
                    kws = {k.arg: k.value for k in n.keywords if k.arg}
                    status = kws.get("status_code", n.args[0] if n.args else None)
                    if isinstance(status, ast.Name) and status.id in params:
                        return False, None
                    return True, status
            return True, None
    return False, None


def _tree(mods: List[_Module]) -> _Tree:
    per_module = [_module_facts(m) for m in mods]
    classes: Set[str] = set().union(*(f.classes for f in per_module))
    bases: Dict[str, Set[str]] = {}
    defs: Dict[str, ast.ClassDef] = {}
    for f in per_module:
        for name, b in f.bases.items():
            bases.setdefault(name, set()).update(b)
        for name, d in f.class_defs.items():
            defs.setdefault(name, d)
    http = set(_HTTP_EXCEPTIONS)
    changed = True
    while changed:
        changed = False
        for name, b in bases.items():
            if name not in http and b & http:
                http.add(name)
                changed = True
    # HTTPException's detail and headers are written where it is raised, and
    # those raises are checked here, so a caught HTTPException is a carrier.
    own = frozenset(classes | _HTTP_EXCEPTIONS)
    carriers: Dict[str, Set[str]] = {"HTTPException": {"kw:status_code", "pos:0"}}
    for name in _RESPONSE_CLASSES:
        carriers[name] = {"kw:status_code", "pos:1"}
    http_fixed: Dict[str, Optional[ast.expr]] = {}
    for name in http - _HTTP_EXCEPTIONS:
        fixes, status = _fixed_http_status(defs[name]) if name in defs else (False, None)
        if fixes:
            http_fixed[name] = status
            carriers[name] = set()
        else:
            carriers[name] = {"kw:status_code", "pos:0"}
    handlers: Dict[str, Tuple[bool, str]] = {}
    for f in per_module:
        for name, positions in f.carriers.items():
            if name not in http_fixed:
                carriers.setdefault(name, set()).update(positions)
        for cls, fn in f.handlers:
            if fn:
                handlers[fn] = (cls in _BROAD or cls not in own, cls or "<expr>")
    return _Tree(per_module, own, carriers, handlers, frozenset(http), http_fixed)


def _functions(mod: _Module) -> List[Tuple[ast.AST, str, bool]]:
    """Every function in the module: (node, dotted qualname, is a method that
    takes self or cls)."""
    out: List[Tuple[ast.AST, str, bool]] = []

    def walk(node: ast.AST, prefix: str, in_class: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                static = any(_callee(d) == "staticmethod" for d in child.decorator_list)
                out.append((child, f"{prefix}{child.name}", in_class and not static))
                walk(child, f"{prefix}{child.name}.", False)
            elif isinstance(child, ast.ClassDef):
                walk(child, f"{prefix}{child.name}.", True)
            elif isinstance(child, ast.stmt):
                walk(child, prefix, in_class)

    walk(mod.tree, "", False)
    return out


class _Index:
    """Scopes and call sites across the trees, built on first use."""

    def __init__(self, mods: List[_Module], tree: _Tree) -> None:
        self.mods = mods
        self.tree = tree
        self._fns = [_functions(m) for m in mods]
        self._scopes: Dict[int, _Scope] = {}
        self._defs: Optional[Dict[str, int]] = None
        self._calls: Optional[Dict[str, List[Tuple[int, ast.Call]]]] = None
        self._taints: Dict[int, Dict[str, "_Taint"]] = {}
        self._ctx: Dict[int, Dict[int, _Ctx]] = {}
        # Platform classes whose text the scan let through at a dynamic status;
        # ``test_classes_answered_at_a_dynamic_status_carry_no_exception_text``
        # checks each one where it is built.
        self.dynamic_exempt: Set[str] = set()

    def exempt(self, taint: "_Taint", frs: FrozenSet[Frag], ctx: _Ctx) -> FrozenSet[Frag]:
        """Drop the platform-class fragments from exception text at a dynamic
        status, remembering the classes they came from."""
        for text, foreign in frs:
            if foreign:
                continue
            root = _Taint._root_name(ast.parse(text, mode="eval").body)
            entry = taint._exc_entry(root, ctx) if root else None
            if entry is not None:
                self.dynamic_exempt.update(c for c in entry[1] if c not in self.tree.http)
        return frozenset(f for f in frs if f[1])

    def scope(self, mi: int, fn: ast.AST, qual: str, is_method: bool) -> _Scope:
        key = id(fn)
        if key not in self._scopes:
            facts = self.tree.per_module[mi]
            param = None
            name = getattr(fn, "name", None)
            if name in self.tree.handlers and isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = fn.args.posonlyargs + fn.args.args
                if len(args) >= 2:
                    foreign, cls = self.tree.handlers[name]
                    param = (args[1].arg, foreign, (cls,))
            self._scopes[key] = _Scope(
                fn, qual, self.tree.own, param, http_classes=self.tree.http,
                tb_names=frozenset(facts.tb_names), is_method=is_method)
        return self._scopes[key]

    def module_scope(self, mi: int) -> _Scope:
        return self.scope(mi, self.mods[mi].tree, "<module>", False)

    def scopes_for_lines(self, mi: int, lines: Set[int]) -> List[_Scope]:
        """The module body and each function whose span holds a candidate line."""
        out = [self.module_scope(mi)]
        for fn, qual, is_method in self._fns[mi]:
            span = range(fn.lineno, (fn.end_lineno or fn.lineno) + 1)  # type: ignore[attr-defined]
            if any(line in span for line in lines):
                out.append(self.scope(mi, fn, qual, is_method))
        return out

    def enclosing(self, mi: int, node: ast.AST) -> _Scope:
        """The innermost function scope that holds ``node``."""
        best = None
        line = getattr(node, "lineno", 0)
        for fn, qual, is_method in self._fns[mi]:
            if fn.lineno <= line <= (fn.end_lineno or fn.lineno):  # type: ignore[attr-defined]
                if best is None or fn.lineno >= best[0].lineno:  # type: ignore[attr-defined]
                    best = (fn, qual, is_method)
        if best is None:
            return self.module_scope(mi)
        return self.scope(mi, *best)

    def _build(self) -> None:
        defs: Dict[str, int] = {}
        calls: Dict[str, List[Tuple[int, ast.Call]]] = {}
        for mi, m in enumerate(self.mods):
            for fn, _q, _m in self._fns[mi]:
                defs[fn.name] = defs.get(fn.name, 0) + 1  # type: ignore[attr-defined]
            for n in ast.walk(m.tree):
                if isinstance(n, ast.Call):
                    name = _callee(n.func)
                    if name:
                        calls.setdefault(name, []).append((mi, n))
        self._defs, self._calls = defs, calls

    def callers(self, mi: int, scope: _Scope) -> List[Tuple[int, ast.Call]]:
        """Call sites of ``scope``'s function, by name: anywhere in the trees
        when the name is defined once, else in its own module only."""
        if self._calls is None:
            self._build()
        assert self._calls is not None and self._defs is not None
        name = getattr(scope.fn, "name", None)
        if not name:
            return []
        sites = self._calls.get(name, [])
        if self._defs.get(name, 0) != 1:
            sites = [(i, c) for i, c in sites if i == mi]
        return sites

    def calls_to(self, names: Set[str]) -> List[Tuple[int, ast.Call]]:
        if self._calls is None:
            self._build()
        assert self._calls is not None
        return [site for n in sorted(names) for site in self._calls.get(n, [])]

    def taints(self, scope: _Scope) -> Dict[str, "_Taint"]:
        key = id(scope)
        if key not in self._taints:
            self._taints[key] = {fam: _Taint(scope, fam) for fam in _FAMILIES}
        return self._taints[key]

    def ctx_at(self, scope: _Scope, node: ast.AST) -> Optional[_Ctx]:
        key = id(scope)
        if key not in self._ctx:
            self._ctx[key] = {id(n): c for n, c in scope.nodes}
        return self._ctx[key].get(id(node))


def _argument_for(scope: _Scope, param: str, call: ast.Call) -> Tuple[str, Optional[ast.expr]]:
    """What ``call`` passes for ``param``: ("arg", expr), ("default", expr),
    ("missing", None) or ("unknown", None) when a star argument hides it."""
    fn = scope.fn
    assert isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
    for k in call.keywords:
        if k.arg == param:
            return "arg", k.value
    if any(k.arg is None for k in call.keywords) or any(isinstance(a, ast.Starred) for a in call.args):
        return "unknown", None
    a = fn.args
    positional = [x.arg for x in a.posonlyargs + a.args]
    offset = 0
    if positional and positional[0] in ("self", "cls") and scope.params[:1] != tuple(positional[:1]):
        offset = 1 if isinstance(call.func, ast.Attribute) else 0
    if param in positional:
        i = positional.index(param) - offset
        if 0 <= i < len(call.args):
            return "arg", call.args[i]
        defaults = a.defaults
        j = positional.index(param) - (len(positional) - len(defaults))
        if 0 <= j < len(defaults):
            return "default", defaults[j]
        return "missing", None
    for x, d in zip(a.kwonlyargs, a.kw_defaults):
        if x.arg == param:
            return ("default", d) if d is not None else ("missing", None)
    return "missing", None


class _Swap(ast.NodeTransformer):
    def __init__(self, name: str, value: ast.expr) -> None:
        self.name, self.value = name, value

    def visit_Name(self, node: ast.Name) -> ast.AST:
        return copy.deepcopy(self.value) if node.id == self.name else node


def _at_caller(access: ast.expr, param: str, arg: ast.expr) -> ast.expr:
    """``access`` (``exc.detail``) with the parameter replaced by what the
    caller passed (``e``), so the caller's own rules decide what it is."""
    return _Swap(param, arg).visit(copy.deepcopy(access))


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


def _status_mode(status: ast.expr, names: Tuple[Set[str], Set[str], Set[str]],
                 bounds: FrozenSet[str]) -> Optional[str]:
    """``"5xx"``, ``"dynamic"``, or None when the status is known to be below 500."""
    status5_names, static_names, dyn_names = names
    if _has_5xx(status) or (isinstance(status, ast.Name) and status.id in status5_names):
        return "5xx"
    if _is_static_below_500(status):
        return None
    if (isinstance(status, ast.Name) and status.id in static_names
            and status.id not in dyn_names):
        return None  # a local only ever assigned constants below 500
    if ast.dump(status) in bounds:
        return None
    return "dynamic"


def _status_names(scope: _Scope) -> Tuple[Set[str], Set[str], Set[str], bool]:
    """A status local is static-below-500 only when every assignment to it in
    the function is a constant below 500. Unpacked, looped, augmented and
    never-assigned names (globals, closures, parameters) are dynamic."""
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
    return status5_names, static_names, dyn_names, sets_5xx_status


def _scan(mods: List[_Module], dynamic_exempt: Optional[Set[str]] = None) -> List[Finding]:
    tree = _tree(mods)
    index = _Index(mods, tree)
    if dynamic_exempt is not None:
        index.dynamic_exempt = dynamic_exempt
    findings: List[Finding] = []
    for mi, (mod, facts) in enumerate(zip(mods, tree.per_module)):
        lines = {line for line, name in facts.calls if name in tree.carriers}
        lines |= set(facts.status_assign_lines)
        if not lines:
            continue
        for scope in index.scopes_for_lines(mi, lines):
            findings.extend(_scan_scope(index, mi, scope))
    return sorted(set(findings), key=lambda f: (f.path, f.line, f.sink, f.kind))


def _scan_scope(index: _Index, mi: int, scope: _Scope) -> List[Finding]:
    rel = index.mods[mi].rel
    carriers, fixed = index.tree.carriers, index.tree.http_fixed
    candidates: List[Tuple[ast.AST, _Ctx, ast.expr, List[ast.AST], str]] = []
    status5_names, static_names, dyn_names, sets_5xx_status = _status_names(scope)
    for node, ctx in scope.nodes:
        if isinstance(node, ast.Return) and sets_5xx_status and node.value is not None:
            candidates.append((node, ctx, ast.Constant(value=500), [node.value], "return"))
            continue
        if not isinstance(node, ast.Call):
            continue
        name = _callee(node.func)
        if not name or name not in carriers:
            continue
        if name in fixed:
            # An HTTPException subclass that sets its own status: every
            # argument is text, at the status it fixes (500 when unreadable).
            status = fixed[name] if fixed[name] is not None else ast.Constant(value=500)
            texts: List[ast.AST] = list(node.args) + [k.value for k in node.keywords]
            candidates.append((node, ctx, status, texts, name))
            continue
        status_expr, kw, pos = _status_of(node, carriers[name])
        if status_expr is None:
            continue
        texts = [a for i, a in enumerate(node.args) if i != pos]
        texts += [k.value for k in node.keywords if k.arg != kw]
        candidates.append((node, ctx, status_expr, texts, name))
    if not candidates:
        return []
    taints = index.taints(scope)
    out: List[Finding] = []
    for node, ctx, status, texts, sink in candidates:
        mode = _status_mode(status, (status5_names, static_names, dyn_names), ctx[1])
        if mode is None:
            continue
        line = getattr(node, "lineno", 0)
        for fam in ("exception", "upstream body"):
            frs = taints[fam]._union(texts, ctx)
            if mode == "dynamic" and fam == "exception":
                frs = index.exempt(taints[fam], frs, ctx)
            if frs:
                out.append(Finding(rel, line, scope.qualname, sink, fam,
                                   ast.unparse(status), frozenset(f[0] for f in frs)))
        pfr = taints["parameter"]._union(texts, ctx)
        if not pfr:
            continue
        status_param = (status.id if mode == "dynamic" and isinstance(status, ast.Name)
                        and status.id in scope.params else None)
        crossed: Dict[str, Tuple[Set[str], Set[str]]] = {}
        for fam, via, text in _through_callers(index, mi, scope, pfr, status_param, mode, 1):
            texts_seen, vias = crossed.setdefault(fam, (set(), set()))
            texts_seen.add(text)
            vias.add(via)
        for fam, (texts_seen, vias) in crossed.items():
            out.append(Finding(rel, line, scope.qualname, sink, fam, ast.unparse(status),
                               frozenset(texts_seen), via="; ".join(sorted(vias))))
    return out


def _through_callers(index: _Index, mi: int, scope: _Scope, frags: FrozenSet[Frag],
                     status_param: Optional[str], mode: str,
                     depth: int) -> List[Tuple[str, str, str]]:
    """Follow parameters in an answer back to what each caller hands them.

    Returns (family, caller, the parameter fragment at the answer) for every
    caller that hands exception text or an upstream body. A status that is a
    parameter is read at each caller the same way, so a helper answering
    ``HTTPException(code, msg)`` is judged by the ``code`` each caller passes.
    """
    ptaint = index.taints(scope)["parameter"]
    out: List[Tuple[str, str, str]] = []
    for cmi, call in index.callers(mi, scope):
        cscope = index.enclosing(cmi, call)
        cctx = index.ctx_at(cscope, call)
        if cctx is None:
            continue
        cmode = mode
        if status_param:
            how, sarg = _argument_for(scope, status_param, call)
            if how in ("arg", "default") and sarg is not None:
                names = _status_names(cscope)[:3]
                cmode = _status_mode(sarg, names, cctx[1])  # type: ignore[assignment]
                if cmode is None:
                    continue
        ctaints = index.taints(cscope)
        via = f"{index.mods[cmi].rel}:{call.lineno} {cscope.qualname}"
        for text, _f in frags:
            access = ptaint.nodes.get(text)
            param = ptaint._root_name(access) if access is not None else None
            if access is None or param is None:
                continue
            how, arg = _argument_for(scope, param, call)
            if arg is None:
                continue
            expr = _at_caller(access, param, arg)
            # A relayed body is followed one call deep: past that, an
            # unrelated ``.text`` (a chat message's) is likelier than a relay.
            for fam in ("exception", "upstream body") if depth == 1 else ("exception",):
                frs = ctaints[fam].frags(expr, cctx)
                if cmode == "dynamic" and fam == "exception":
                    frs = index.exempt(ctaints[fam], frs, cctx)
                if frs:
                    out.append((fam, via, text))
            if depth < _MAX_CALL_DEPTH:
                deeper = ctaints["parameter"].frags(expr, cctx)
                if deeper:
                    for fam, cvia, _t in _through_callers(index, cmi, cscope, deeper, None,
                                                          cmode or mode, depth + 1):
                        out.append((fam, cvia, text))
    return out


def scan_sources(sources: Dict[str, str]) -> List[Finding]:
    """Scan in-memory modules (path -> source). Used by the rule's own tests."""
    return _scan(_iter_modules(sources))


@functools.lru_cache(maxsize=1)
def _real_scan() -> Tuple[Tuple[Finding, ...], FrozenSet[str]]:
    exempt: Set[str] = set()
    findings = _scan(list(_real_modules()), exempt)
    return tuple(findings), frozenset(exempt)


def _real_findings() -> Tuple[Finding, ...]:
    return _real_scan()[0]


# ---------------------------------------------------------------------------
# carriers listed as "only ever raised with platform text"
# ---------------------------------------------------------------------------

# A construction of a listed class that is allowed to carry another
# exception's text: (file, enclosing function, class built, fragments). Each
# names its reason where it is used; ``_PLATFORM_TEXT_PINS`` below.
_Pin = Tuple[str, str, str, FrozenSet[str]]


def _constructions_with_foreign_text(mods: List[_Module], classes: Set[str],
                                     pins: FrozenSet[_Pin] = frozenset(), *,
                                     strict: bool = True,
                                     answers_only: bool = False,
                                     reviewed: FrozenSet[Tuple[str, str, str]] = frozenset(),
                                     ) -> List[str]:
    """Every call that builds one of ``classes`` (or a subclass defined in the
    trees) from anything but platform text.

    Platform text is a constant, a name that is not an exception or a
    parameter, or the text of another platform class that is itself only
    built from platform text (that class joins the checked set). A parameter
    is followed to every caller, up to ``_MAX_CALL_DEPTH`` deep; a parameter
    whose callers cannot be seen, or that a star argument hides, counts as
    foreign, so wrapping the construction in a helper changes nothing.

    ``strict=False`` is the weaker "no exception text" check: an upstream
    body and a parameter whose callers cannot be seen are allowed, and only
    exception text (followed through the callers that can be seen) counts.

    ``answers_only`` skips a carrier built at a status fixed below 500, which
    no forwarder can answer at 5xx, and the (file, function, class) sites in
    ``reviewed``: constructions the scan itself finds at 5xx, which an
    ``_ALLOWED`` entry holds by their exact fragments.
    """
    tree = _tree(mods)
    index = _Index(mods, tree)
    names = set(classes)
    out: Set[str] = set()
    seen: Set[int] = set()
    while True:
        changed = True
        while changed:
            changed = False
            for name, bases in ((n, b) for f in tree.per_module for n, b in f.bases.items()):
                if name not in names and bases & names:
                    names.add(name)
                    changed = True
        before = len(names)
        for mi, call in index.calls_to(names):
            if id(call) in seen:
                continue
            seen.add(id(call))
            scope = index.enclosing(mi, call)
            ctx = index.ctx_at(scope, call)
            if ctx is None:
                continue
            built = _callee(call.func) or "?"
            if answers_only:
                if (mods[mi].rel, scope.qualname, built) in reviewed:
                    continue
                if built in tree.carriers:
                    status, _kw, _pos = _status_of(call, tree.carriers[built])
                    if status is not None and _is_static_below_500(status):
                        continue
            args: List[ast.AST] = list(call.args) + [k.value for k in call.keywords]
            bad = _foreign_in(index, mi, scope, ctx, args, names, 1, strict)
            if not bad:
                continue
            key = (mods[mi].rel, scope.qualname, built, frozenset(t for t, _v in bad))
            if key in pins:
                continue
            via = "; ".join(sorted({v for _t, v in bad if v}))
            out.add(f"{mods[mi].rel}:{call.lineno} {scope.qualname} builds {built} from "
                    f"{', '.join(sorted({t for t, _v in bad}))}" + (f" (via {via})" if via else ""))
        if len(names) == before:
            return sorted(out)


def _foreign_in(index: _Index, mi: int, scope: _Scope, ctx: _Ctx, args: List[ast.AST],
                names: Set[str], depth: int, strict: bool) -> List[Tuple[str, str]]:
    """(fragment, caller) pairs in ``args`` that are not platform text. Adds
    any platform class whose text is used to ``names``."""
    taints = index.taints(scope)
    bad: List[Tuple[str, str]] = []
    for text, foreign in taints["exception"]._union(args, ctx):
        caught = taints["exception"].classes.get(text, ())
        # An HTTPException's own text is "<status>: <detail>", and a 4xx
        # detail may carry a validation error's words, so it is never
        # platform text here.
        if foreign or not caught or any(c in index.tree.http for c in caught):
            bad.append((text, ""))
        else:
            names.update(caught)
    if strict:
        for text, _f in taints["upstream body"]._union(args, ctx):
            bad.append((text, ""))
    pfr = taints["parameter"]._union(args, ctx)
    if not pfr:
        return bad
    ptaint = taints["parameter"]
    callers = index.callers(mi, scope)
    for text, _f in pfr:
        access = ptaint.nodes.get(text)
        param = ptaint._root_name(access) if access is not None else None
        if access is None or param is None or not callers or depth > _MAX_CALL_DEPTH:
            if strict:
                bad.append((text, "no caller seen" if not callers else ""))
            continue
        for cmi, call in callers:
            cscope = index.enclosing(cmi, call)
            cctx = index.ctx_at(cscope, call)
            how, arg = _argument_for(scope, param, call)
            where = f"{index.mods[cmi].rel}:{call.lineno}"
            if cctx is None or how in ("unknown", "missing") or arg is None:
                if strict:
                    bad.append((text, f"{where} hides it"))
                continue
            if _foreign_in(index, cmi, cscope, cctx, [_at_caller(access, param, arg)],
                           names, depth + 1, strict):
                bad.append((text, where))
    return bad


# ---------------------------------------------------------------------------
# classes whose text crosses a function before a 5xx
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _Carried:
    cls: str
    reason: str


_CARRIED: Tuple[_Carried, ...] = (
    _Carried(
        "EgressError",
        "AAuth egress (services/aauth/egress.py). ps_client and person_token hand "
        "its `detail` to a returned refusal that routers/aauth_person.py answers at "
        "502 (`person_server_unreachable`), and resource_client to one that "
        "routers/aauth_resource.py answers at 502 by default. A transport failure "
        "names httpx's class only; the A2A validator's words it re-raises are "
        "A2ACallError's, checked with that class's pins.",
    ),
)


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
    # Classes whose every construction must carry no exception text (peer or
    # caller-supplied words are allowed): the weaker check, for a reason that
    # says only that.
    no_exception_text: Tuple[str, ...] = ()
    # Constructions of those classes that do carry exception text, each named
    # by the reason (``_Pin``).
    pins: Tuple[_Pin, ...] = ()
    # The last day the entry may stand: a hold, not a decision.
    expires: Optional[datetime.date] = None

    def matches(self, f: Finding) -> bool:
        return (f.path, f.function, f.sink, f.fragments) == (
            self.path, self.function, self.sink, self.fragments)


_AS = "docker/base-image/agent_server/"
_BE = "src/backend/"

# The system_service entries' shared reason. `_failure_reason` RETURNS the
# text, which the scan does not follow, so the guard checks only the classes.
_FAILURE_REASON = (
    "`reason` is `_failure_reason(e)`. It keeps an HTTPException's detail (each "
    "raise is checked by this guard), any ValueError's text (the validators' words; "
    "a library ValueError's text too, sanitized, redacted and capped), and the "
    "words of the classes in `_platform_worded`, whose constructions this entry "
    "checks: no exception text. That any other exception answers its class name "
    "only crosses as a return value, so test_4284_exception_text_sites.py and "
    "test_ent125_resilient_system_deploy.py hold it, not this guard. Sanitized and "
    "capped (ent#125)."
)
_FAILURE_REASON_CLASSES = (
    "_Refused", "AgentAuthSecretMissing", "GitInstanceReservationError",
    "SeatWriterUnavailable", "SkillSetUnresolved",
)

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
        "ChannelDeliveryError is raised only with platform sentences: constants, and "
        "`recipient_blocked`'s channel name, which every caller passes as a literal.",
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
    # --- backend: relays whose 5xx text is a deliberate decision --------------
    _Allowed(
        _BE + "services/agent_skills_listing.py", "fetch_live", "SkillsListUnavailable",
        frozenset({"response.text"}),
        "The agent's own error body, at the agent's status, on three surfaces: the "
        "owner's agent page (routers/agent_files.py), the owner's connector route "
        "(routers/connector.py, any user the agent is shared with) and the inline-auth "
        "MCP route (services/mcp_auth_service.py, a verified-email collaborator), as "
        "`Agent error: <body>` through connector_service.fetch_live_playbooks. Both "
        "are deliberate decisions pinned by test_ent754_skills_list_cache.py; the "
        "public link answers a fixed sentence (routers/public.py). The agent's skills "
        "route has no handler that renders an exception, in its current code or its "
        "history: an unforeseen error there is FastAPI's bare 500.",
    ),
    _Allowed(
        _BE + "services/chat_execution_service.py", "_proxy_terminate_and_finalize",
        "ChatDispatchError", frozenset({"response.json()"}),
        "The body passes `_terminate_refusal_detail`: at an agent 5xx only the "
        "agent's own sentences go on, `termination_unconfirmed: ...` (#3408, which "
        "tells the caller the termination can be retried) and the registry's "
        "`Termination failed: <class>`; anything else, such as an older agent's raw "
        "exception text, answers a fixed sentence with the agent's status. A 4xx is "
        "the agent's refusal. This entry pins only the fragment: the filter is a "
        "return value the scan does not follow, so test_4284_exception_text_sites.py "
        "and test_terminate_async_executor.py hold it, not this guard.",
    ),
    _Allowed(
        _BE + "services/chat_execution_service.py", "_apply_sub003_autoswitch",
        "ChatDispatchError", frozenset({"agent_status_code", "error_msg"}),
        "The sync chat failure: `error_msg` is `_parse_agent_http_error`'s, the "
        "agent's own error detail from its answer (credential-scrubbed, ent#279, and "
        "capped) or `HTTP error: <class>`, never httpx's text. It is the turn's "
        "failure the caller acts on, and this module's classifiers and the 503's "
        "consumers read it (#2889). The agent server's chat and task 5xx are "
        "platform text, which this guard checks in that tree; an agent still on a "
        "base image from before #4293 answers `Execution error: <exception text>` "
        "here until it is recreated (named in #4284's PR).",
        count=5,
    ),
    _Allowed(
        _BE + "services/chat_execution_service.py", "_finalize_budget_exhausted",
        "ChatDispatchError", frozenset({"budget_exc"}),
        "`str(budget_exc)` of BackendAgentCallBudgetExhausted, built from the agent "
        "name and the platform's own caps and wait (checked: no exception text).",
        no_exception_text=("BackendAgentCallBudgetExhausted",),
    ),
    # --- backend: A2A outbound ------------------------------------------------
    _Allowed(
        _BE + "routers/a2a.py", "_map_call_error", "HTTPException",
        frozenset({"exc.detail", "exc.payment", "exc.reason", "exc.remote_code",
                   "exc.remote_status", "exc.task_id"}),
        "A2ACallError is built from platform sentences and reason codes, the peer's "
        "status and JSON-RPC code, and the peer's own bounded, credential-scrubbed "
        "answer (its JSON-RPC error message, the 402 payment block, #3185): what the "
        "calling agent acts on. A transport failure names httpx's class only "
        "(`_read_capped`). Checked: no exception text, except a URL ValueError's "
        "words under `endpoint_invalid`, which this route answers at 400 "
        "(test_a2a_pinned_constructions_answer_below_500).",
        no_exception_text=("A2ACallError",),
        pins=(
            (_BE + "services/a2a_client.py", "validate_endpoint", "A2ACallError",
             frozenset({"exc"})),
            (_BE + "utils/url_validation.py", "validate_a2a_endpoint_url",
             "A2AEndpointUrlError", frozenset({"exc"})),
        ),
    ),
    _Allowed(
        _BE + "services/credential_sink.py", "_remote_failure", "_destination_failure",
        frozenset({"exc.reason"}),
        "The A2A client's reason code (A2ACallError, checked with the A2A route's "
        "entry) in the 503's `data.reason`; `exc.detail` is deliberately never "
        "forwarded.",
        count=3,
    ),
    _Allowed(
        _BE + "services/credential_sink.py", "_remote_failure", "_destination_failure",
        frozenset({"getattr(exc, 'remote_status', None)"}),
        "The peer's HTTP status (an int the A2A client read off the answer) in the "
        "503's `data.status`.",
    ),
    _Allowed(
        _BE + "services/credential_sink.py", "_destination_failure", "CredentialSinkError",
        frozenset({"data", "message"}),
        "`_remote_failure` fills these with its own sentences around the host, and "
        "`data` with the A2A reason code and the peer's status: codes and platform "
        "text, not exception text.",
    ),
    # --- backend: a duck-typed HTTPException ----------------------------------
    _Allowed(
        _BE + "services/agent_service/repo_binding.py", "_translate_destination_error",
        "BindError", frozenset({"getattr(exc, 'detail', None)"}),
        "Called only from bind_agent_to_own_repo's handler around "
        "inspect_or_create_destination_repo, and only for an exception that has both "
        "`status_code` and `detail`: that primitive's structured HTTPException "
        "(FORK_* codes), whose every raise this guard checks. repo_binding stays "
        "HTTP-free, so it does not name the class.",
        count=2,
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
        "Strict-mode system deploy, at the failure's own status or 500. "
        + _FAILURE_REASON + " The non-strict report carries the same reason (ent#125 parity).",
        no_exception_text=_FAILURE_REASON_CLASSES,
    ),
    _Allowed(
        _BE + "services/system_service.py", "deploy_manifest", "SystemDeployFailure",
        frozenset({"e"}),
        "A per-member report row, the body of the deploy's 500 when every member "
        "fails (routers/systems.py). " + _FAILURE_REASON,
        count=2,
        no_exception_text=_FAILURE_REASON_CLASSES,
    ),
    _Allowed(
        _BE + "services/system_service.py", "_preflight_template", "SystemDeployFailure",
        frozenset({"e"}),
        "The preview's per-member row, also apply's `template_unresolvable` blocker "
        "(a 409/503 body). " + _FAILURE_REASON,
        no_exception_text=_FAILURE_REASON_CLASSES,
    ),
    # --- held unedited --------------------------------------------------------
    _Allowed(
        _BE + "routers/slack.py", "connect_slack", "HTTPException", frozenset({"e"}),
        "routers/slack.py stays unchanged until after 2026-11-19, so the "
        "code-scanning dismissals reviewed against its current source stay valid "
        "until then. Fix it the #1917 way after that date and drop this entry "
        "(abilityai/trinity#4339).",
        expires=datetime.date(2026, 11, 19),
    ),
)

# Constructions the A2A entry pins: each must carry this reason code, and the
# A2A route must answer the code below 500.
_A2A_PINNED_REASON = "endpoint_invalid"


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
        "exception text, an upstream body or a parameter whose callers do:\n  "
        + "\n  ".join(bad)
    )


def test_listed_carriers_carry_no_exception_text():
    classes = {c for a in _ALLOWED for c in a.no_exception_text} | {c.cls for c in _CARRIED}
    pins = frozenset(p for a in _ALLOWED for p in a.pins)
    bad = _constructions_with_foreign_text(list(_real_modules()), classes, pins, strict=False)
    assert not bad, (
        "A class an _ALLOWED reason says carries no exception text is built from "
        "it (directly, or through a parameter a caller fills with it):\n  "
        + "\n  ".join(bad)
    )


def test_classes_answered_at_a_dynamic_status_carry_no_exception_text():
    """What the dynamic-status exemption rests on: every platform class whose
    text the trees answer at a status computed at run time (``e.status_code``,
    a status parameter) is checked where it is built, carriers and non-carriers
    alike."""
    classes = set(_real_scan()[1])
    assert classes, "the exemption let no class through: the collection is broken"
    pins = frozenset(p for a in _ALLOWED for p in a.pins)
    reviewed = frozenset((a.path, a.function, a.sink) for a in _ALLOWED)
    bad = _constructions_with_foreign_text(list(_real_modules()), classes, pins, strict=False,
                                           answers_only=True, reviewed=reviewed)
    assert not bad, (
        "A platform class whose text a route answers at a dynamic status (exempt "
        "there, as a carrier checked where it is built) is built from exception "
        "text:\n  " + "\n  ".join(bad)
    )


def test_failure_reason_classes_are_the_ones_it_keeps():
    """The system_service entries check the classes `_platform_worded` keeps
    the words of; a class kept there and not checked here would pass unseen."""
    tree = ast.parse((_REPO / _BE / "services/system_service.py").read_text())
    [fn] = [n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "_platform_worded"]
    [ret] = [n for n in ast.walk(fn) if isinstance(n, ast.Return)]
    assert isinstance(ret.value, ast.Tuple), ast.unparse(ret)
    kept = {ast.unparse(e).rsplit(".", 1)[-1] for e in ret.value.elts}
    assert kept == set(_FAILURE_REASON_CLASSES)


def test_carried_classes_are_defined_and_reasoned():
    """A renamed class would leave its check matching nothing."""
    defined = {n.name for m in _real_modules() for n in ast.walk(m.tree)
               if isinstance(n, ast.ClassDef)}
    for c in _CARRIED:
        assert c.cls in defined, f"_CARRIED names {c.cls}, which no module defines"
        assert len(c.reason) > 80, c.cls


def test_pins_are_live():
    """Every pinned construction still exists, so a pin cannot outlive its site."""
    mods = list(_real_modules())
    for a in _ALLOWED:
        if not a.pins:
            continue
        found = _constructions_with_foreign_text(mods, set(a.no_exception_text), strict=False)
        for path, function, cls, frags in a.pins:
            want = f"{path}:"
            assert any(b.startswith(want) and f" {function} builds {cls} from "
                       f"{', '.join(sorted(frags))}" in b for b in found), (path, function, cls)


def _a2a_status_table() -> Dict[str, int]:
    tree = ast.parse((_REPO / _BE / "routers/a2a.py").read_text())
    for n in ast.walk(tree):
        if (isinstance(n, ast.Assign) and any(isinstance(t, ast.Name)
                                              and t.id == "_A2A_CLIENT_ERROR_STATUS"
                                              for t in n.targets)):
            return ast.literal_eval(n.value)
    raise AssertionError("routers/a2a.py: _A2A_CLIENT_ERROR_STATUS not found")


def test_a2a_pinned_constructions_answer_below_500():
    """The A2A entry's pins carry a URL ValueError's words, which is right only
    because they are built under a reason the route answers below 500."""
    assert _a2a_status_table()[_A2A_PINNED_REASON] < 500
    [entry] = [a for a in _ALLOWED if a.function == "_map_call_error"]
    for path, function, cls, _frags in entry.pins:
        tree = ast.parse((_REPO / path).read_text())
        [fn] = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name == function]
        builds = [n for n in ast.walk(fn) if isinstance(n, ast.Call) and _callee(n.func) == cls
                  and n.args and not isinstance(n.args[0], ast.Constant)]
        pinned = [n for n in ast.walk(fn) if isinstance(n, ast.Call) and _callee(n.func) == cls
                  and n.args and isinstance(n.args[0], ast.Constant)
                  and n.args[0].value == _A2A_PINNED_REASON]
        assert pinned, (path, function)
        for n in builds:
            # The one non-literal reason is the URL error's own, whose every
            # value is an `endpoint_*` code the route answers at 400.
            assert ast.unparse(n.args[0]).endswith(".reason") or "_A2A_REASON_BY_KIND" in ast.unparse(n.args[0]), ast.unparse(n)
    assert all(code < 500 for name, code in _a2a_status_table().items()
               if name.startswith("endpoint_"))


def _expired_holds(today: datetime.date) -> List[str]:
    return [f"{a.path} {a.function}: held until {a.expires}" for a in _ALLOWED
            if a.expires is not None and today > a.expires]


def test_held_entries_have_not_expired():
    expired = _expired_holds(datetime.date.today())
    assert not expired, "Fix the site and drop the entry:\n  " + "\n  ".join(expired)


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
    "local alias": _wrap('msg = f"Failed: {e}"\nraise HTTPException(status_code=500, detail=msg)'),
    "scrubbed alias": _wrap('safe = _scrub(str(e), pat)\nraise HTTPException(500, detail=f"x: {safe}")'),
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
    "narrow arm": _wrap('raise HTTPException(500, f"Failed: {e}")', catch="OSError as e"),
    "bare re-bound": _wrap('raise HTTPException(500, f"Failed: {e}")', catch="(ValueError, KeyError) as e"),
    "traceback.format_exc": _wrap("raise HTTPException(500, traceback.format_exc())", catch="Exception"),
    "sys.exc_info": _wrap("raise HTTPException(500, str(sys.exc_info()[1]))", catch="Exception"),
    "carrier positional": _wrap('raise BindError(502, "BIND_X", f"Could not bind: {e}")'),
    "carrier keyword": _wrap('return Outcome(status_code=500, payload={"error": str(e)})'),
    "foreign exception at a dynamic status": _wrap(
        "raise HTTPException(status_code=e.response.status_code, detail=str(e))",
        catch="httpx.HTTPStatusError as e"),
    "broad exception at a dynamic status": _wrap(
        "raise HTTPException(status_code=pat, detail=str(e))"),
    "relay .text": _wrap("raise HTTPException(status_code=r.status_code, detail=r.text)"),
    "relay .json()": _wrap(
        'raise HTTPException(status_code=r.status_code, detail=r.json().get("detail"))'),
    "relay at 502": _wrap('raise HTTPException(502, f"Agent failed: {r.text[:500]}")'),
    "relay into a carrier": _wrap('raise BindError(r.status_code, "X", r.content)'),
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
    ".detail of a platform class": _wrap(
        'raise HTTPException(502, detail={"message": e.detail})', catch="BindError as e"),
    ".detail of a broad catch": _wrap('raise HTTPException(500, e.detail)'),
    "getattr default": _wrap('raise HTTPException(503, getattr(e, "detail", str(e)))',
                             catch="HTTPException as e"),
    "getattr .detail of a broad catch": _wrap('raise HTTPException(503, getattr(e, "detail", ""))'),
    "format_exc imported by name": (
        "from traceback import format_exc\n" + _wrap("raise HTTPException(500, format_exc())")),
    "format_exc imported under an alias": (
        "from traceback import format_exc as fx\n" + _wrap("raise HTTPException(500, fx())")),
    "relay .read().decode()": _wrap(
        'raise HTTPException(502, f"Agent failed: {response.read().decode()}")'),
    "relay await .aread()": _wrap('raise HTTPException(502, (await resp.aread()).decode())'),
    "relay aiohttp await .text()": _wrap('raise HTTPException(502, await r.text())'),
    "relay iterator": _wrap('raise HTTPException(502, "".join(r.iter_text()))'),
    "HTTPException subclass, status fixed in __init__": _HEAD + (
        "class GitFailed(HTTPException):\n"
        "    def __init__(self, detail):\n"
        "        super().__init__(status_code=500, detail=detail)\n"
        "def handler():\n"
        "    try:\n"
        "        work()\n"
        "    except Exception as e:\n"
        "        raise GitFailed(str(e))\n"
    ),
    "HTTPException subclass, status passed": _HEAD + (
        "class Refused(HTTPException):\n"
        "    pass\n"
        "def handler():\n"
        "    try:\n"
        "        work()\n"
        "    except Exception as e:\n"
        "        raise Refused(502, str(e))\n"
    ),
    "helper handed the exception": _HEAD + (
        "def _fail_git(what, e):\n"
        '    raise HTTPException(500, f"{what}: {e}")\n'
        "def handler():\n"
        "    try:\n"
        "        work()\n"
        "    except Exception as e:\n"
        '        _fail_git("Git sync error", e)\n'
    ),
    "helper handed the text, two calls back": _HEAD + (
        "def _answer(msg):\n"
        "    raise HTTPException(503, msg)\n"
        "def _wrap_up(msg):\n"
        '    _answer(f"Failed: {msg}")\n'
        "def handler():\n"
        "    try:\n"
        "        work()\n"
        "    except Exception as e:\n"
        "        _wrap_up(str(e))\n"
    ),
    "helper status is a parameter, a caller passes 5xx": _HEAD + (
        "def _fail(code, msg):\n"
        "    raise HTTPException(code, msg)\n"
        "def handler():\n"
        "    try:\n"
        "        work()\n"
        "    except Exception as e:\n"
        "        _fail(500, str(e))\n"
    ),
    "mapper handed a platform exception (.detail)": _HEAD + (
        "class CallError(Exception):\n"
        "    def __init__(self, reason, detail):\n"
        "        self.reason, self.detail = reason, detail\n"
        "def _map(exc):\n"
        '    return HTTPException(MAP.get(exc.reason, 502), {"message": exc.detail})\n'
        "def handler():\n"
        "    try:\n"
        "        work()\n"
        "    except CallError as exc:\n"
        "        raise _map(exc)\n"
    ),
    "method helper handed the exception": _HEAD + (
        "class Svc:\n"
        "    def _fail(self, e):\n"
        '        raise HTTPException(500, f"failed: {e}")\n'
        "    def run(self):\n"
        "        try:\n"
        "            work()\n"
        "        except Exception as e:\n"
        "            self._fail(e)\n"
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
    "getattr .detail of an HTTPException": _wrap(
        'raise HTTPException(502, getattr(e, "detail", "Failed"))', catch="HTTPException as e"),
    "an upload read is not a relay": _wrap(
        "raise HTTPException(500, f\"Bad upload: {len(await upload.read())} bytes\")"),
    "a file read is not a relay": _wrap('raise HTTPException(500, fh.read())'),
    "HTTPException subclass fixing a 4xx": _HEAD + (
        "class Gone(HTTPException):\n"
        "    def __init__(self, detail):\n"
        "        super().__init__(status_code=410, detail=detail)\n"
        "def handler():\n"
        "    try:\n"
        "        work()\n"
        "    except ValueError as e:\n"
        "        raise Gone(str(e))\n"
    ),
    "helper status is a parameter, every caller passes 4xx": _HEAD + (
        "def _fail(code, msg):\n"
        "    raise HTTPException(code, msg)\n"
        "def handler():\n"
        "    try:\n"
        "        work()\n"
        "    except ValueError as e:\n"
        "        _fail(400, str(e))\n"
    ),
    "helper handed only platform words": _HEAD + (
        "def _fail_git(what, e):\n"
        '    raise HTTPException(500, f"{what}: {type(e).__name__}")\n'
        "def handler():\n"
        "    try:\n"
        "        work()\n"
        "    except Exception as e:\n"
        '        _fail_git("Git sync error", e)\n'
    ),
    "mapper handed an HTTPException's detail": _HEAD + (
        "def _map(exc):\n"
        "    return HTTPException(502, exc.detail)\n"
        "def handler():\n"
        "    try:\n"
        "        work()\n"
        "    except HTTPException as exc:\n"
        "        raise _map(exc)\n"
    ),
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


# ---------------------------------------------------------------------------
# the class checks behind an allowlist reason
# ---------------------------------------------------------------------------

_CARRIER_HEAD = (
    "import httpx\n"
    "class Unavailable(Exception):\n"
    "    pass\n"
    "class Upstream(Exception):\n"
    "    pass\n"
)


def _built_from(src: str, *, strict: bool = True) -> List[str]:
    mods = _iter_modules({"src/backend/services/sample.py": _CARRIER_HEAD + src})
    return _constructions_with_foreign_text(mods, {"Unavailable"}, strict=strict)


_BUILT_FOREIGN = {
    "exception text": (
        "def f():\n"
        "    try:\n"
        "        work()\n"
        "    except Exception as e:\n"
        "        raise Unavailable(f'x: {e}')\n"),
    "a helper handed the exception text": (
        "def _refuse(why):\n"
        "    raise Unavailable('template_unreadable', why)\n"
        "def f():\n"
        "    try:\n"
        "        work()\n"
        "    except Exception as e:\n"
        "        _refuse(str(e))\n"),
    "a method helper handed it": (
        "class Svc:\n"
        "    def _refuse(self, why):\n"
        "        raise Unavailable(why)\n"
        "    def f(self):\n"
        "        try:\n"
        "            work()\n"
        "        except OSError as e:\n"
        "            self._refuse(f'{e}')\n"),
    "another platform class built from exception text": (
        "def g():\n"
        "    try:\n"
        "        work()\n"
        "    except Exception as e:\n"
        "        raise Upstream(str(e))\n"
        "def f():\n"
        "    try:\n"
        "        g()\n"
        "    except Upstream as u:\n"
        "        raise Unavailable(str(u))\n"),
    "a subclass built from exception text": (
        "class Narrower(Unavailable):\n"
        "    pass\n"
        "def f():\n"
        "    try:\n"
        "        work()\n"
        "    except Exception as e:\n"
        "        raise Narrower(repr(e))\n"),
}

_BUILT_STRICT_ONLY = {
    "a parameter no caller fills in sight": (
        "def _refuse(why):\n"
        "    raise Unavailable(why)\n"),
    "a star argument hides the parameter": (
        "def _refuse(why):\n"
        "    raise Unavailable(why)\n"
        "def f(*args):\n"
        "    _refuse(*args)\n"),
    "an upstream body": (
        "def f(r):\n"
        "    raise Unavailable(r.text)\n"),
}

_BUILT_CLEAN = {
    "constants": "def f():\n    raise Unavailable('not_running', 'Agent is not running.')\n",
    "a helper given literals only": (
        "def _refuse(why, channel):\n"
        "    raise Unavailable(f'{why}: blocked on the {channel} channel')\n"
        "def f():\n"
        "    _refuse('recipient_blocked', 'telegram')\n"
        "def g():\n"
        "    _refuse(why='recipient_blocked', channel='slack')\n"),
    "another platform class built from constants": (
        "def g():\n"
        "    raise Upstream('the store is unreadable')\n"
        "def f():\n"
        "    try:\n"
        "        g()\n"
        "    except Upstream as u:\n"
        "        raise Unavailable(str(u))\n"),
    "the class name of the exception": (
        "def f():\n"
        "    try:\n"
        "        work()\n"
        "    except Exception as e:\n"
        "        raise Unavailable(f'parse failed ({type(e).__name__})')\n"),
}


@pytest.mark.parametrize("name", sorted(_BUILT_FOREIGN))
def test_class_check_flags_exception_text_both_ways(name):
    assert _built_from(_BUILT_FOREIGN[name]), name
    assert _built_from(_BUILT_FOREIGN[name], strict=False), name


@pytest.mark.parametrize("name", sorted(_BUILT_STRICT_ONLY))
def test_platform_text_only_fails_closed_where_no_exception_text_does_not(name):
    assert _built_from(_BUILT_STRICT_ONLY[name]), name
    assert not _built_from(_BUILT_STRICT_ONLY[name], strict=False), name


@pytest.mark.parametrize("name", sorted(_BUILT_CLEAN))
def test_class_check_passes_platform_text(name):
    assert not _built_from(_BUILT_CLEAN[name]), _built_from(_BUILT_CLEAN[name])


def test_a_pin_covers_only_its_construction():
    src = _BUILT_FOREIGN["exception text"]
    pin = ("src/backend/services/sample.py", "f", "Unavailable", frozenset({"e"}))
    mods = _iter_modules({"src/backend/services/sample.py": _CARRIER_HEAD + src})
    assert not _constructions_with_foreign_text(mods, {"Unavailable"}, frozenset({pin}),
                                                strict=False)
    other = ("src/backend/services/sample.py", "f", "Unavailable", frozenset({"e.args"}))
    assert _constructions_with_foreign_text(mods, {"Unavailable"}, frozenset({other}),
                                            strict=False)


def test_the_slack_hold_ends_after_2026_11_19():
    assert _expired_holds(datetime.date(2026, 11, 19)) == []
    assert _expired_holds(datetime.date(2026, 11, 20)) == [
        "src/backend/routers/slack.py connect_slack: held until 2026-11-19"]


# ---------------------------------------------------------------------------
# the dynamic-status exemption, checked where the exempted class is built
# ---------------------------------------------------------------------------

_FORWARDED = (
    "class Upstream(Exception):\n"
    "    def __init__(self, message):\n"
    "        super().__init__(message)\n"
    "        self.status = 502\n"
    "def service():\n"
    "    try:\n"
    "        work()\n"
    "    except Exception as e:\n"
    "        {build}\n"
    "def route():\n"
    "    try:\n"
    "        service()\n"
    "    except {caught} as e:\n"
    "        raise HTTPException(status_code=e.{status}, detail=e.{text})\n"
)


def _exempted(build: str, caught: str, status: str = "status", text: str = "args",
              **kw: bool) -> Tuple[Set[str], List[str]]:
    src = _HEAD + _FORWARDED.format(build=build, caught=caught, status=status, text=text)
    mods = _iter_modules({"src/backend/routers/sample.py": src})
    exempt: Set[str] = set()
    assert not _scan(mods, exempt)
    return exempt, _constructions_with_foreign_text(mods, exempt, strict=False,
                                                    answers_only=kw.get("answers_only", True))


def test_a_class_forwarded_at_its_own_status_is_checked_where_it_is_built():
    exempt, bad = _exempted("raise Upstream(f'upstream: {e}')", "Upstream")
    assert exempt == {"Upstream"}
    assert bad and "builds Upstream from e" in bad[0], bad


def test_a_class_forwarded_at_its_own_status_passes_with_platform_text():
    exempt, bad = _exempted("raise Upstream('the upstream refused')", "Upstream")
    assert exempt == {"Upstream"} and not bad, bad


def test_a_carrier_built_below_500_is_not_an_answer_at_5xx():
    """A carrier built at a fixed 4xx reaches no 5xx through its forwarder, so
    the answers-only check leaves it; the plain check does not."""
    build = "raise BindError(409, 'BIND_CONFLICT', f'conflict: {e}')"
    exempt, bad = _exempted(build, "BindError", status="status_code", text="message")
    assert exempt == {"BindError"} and not bad, bad
    _exempt, plain = _exempted(build, "BindError", status="status_code", text="message",
                               answers_only=False)
    assert plain, "the fixed-4xx skip is the only reason the construction passes"
