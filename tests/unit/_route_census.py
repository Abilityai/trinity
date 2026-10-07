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
* ``admin_widened`` — ``Depends(require_admin_allowing(...))`` or
  ``assert_admin(..., allow_scopes=...)``. Each needs an ``ADMIN_WIDENED`` entry:
  a widening grants a scope and is reviewed per route.
* ``portal`` — ``Depends(get_portal_principal)``; the portal principal's own
  policy (#2198) owns it.

A route carrying several recognised gates takes the strictest
(``interactive > person > admin_widened > admin_tier > portal``).

Listed here, per route, with the registered ``"METHOD /full/path"`` pinned and
checked against the live app by the runtime test:

* ``agent_callable`` — a reviewed use an agent key may make, with a reason.
* ``own_auth`` — authenticates itself (internal secret, webhook token, payment,
  inline MCP auth) or is unauthenticated by design.
* ``delegated`` — the check lives in ``services/``; names the behavioural test
  that proves it, and the census asserts that test exists.

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
    "admin_widened",
    "admin_tier",
    "portal",
)  # strictest first
LISTED_CLASSES = ("agent_callable", "own_auth", "delegated", "unclassified_at_freeze")

# Gate names and the module each must be imported from.
_DEPENDS_GATES = {
    ("dependencies", "require_interactive"): "interactive",
    ("dependencies", "require_person"): "person",
    ("dependencies", "require_admin"): "admin_tier",
    ("client_portal.portal_auth", "get_portal_principal"): "portal",
}
_DEPENDS_FACTORIES = {("dependencies", "require_admin_allowing"): "admin_widened"}
_IMPERATIVE_GATES = {
    ("dependencies", "reject_non_interactive_principal"): "interactive",
    ("dependencies", "assert_person"): "person",
    ("dependencies", "reject_non_person_principal"): "person",
    (
        "dependencies",
        "assert_admin",
    ): "admin_tier",  # admin_widened when allow_scopes= is passed
}


@dataclass
class Route:
    key: str
    relpath: str
    qualname: str
    lineno: int
    methods: Tuple[str, ...]
    gates: Set[str] = field(default_factory=set)
    problems: List[str] = field(default_factory=list)

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
            return
        routes[key] = Route(
            key=key,
            relpath=rel,
            qualname=qual,
            lineno=fn.lineno,
            methods=tuple(sorted(methods)),
            gates=_gates_for(indexes[rel], fn, decorators),
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

# Reviewed uses an agent key may make. key -> ("METHOD /full/path", reason)
AGENT_CALLABLE: Dict[str, Tuple[str, str]] = {
    # The agent's own runtime surface (#2996 §3.4 regression set).
    "routers/agents.py::agent_heartbeat": (
        "POST /api/agents/{agent_name}/heartbeat",
        "liveness; authenticates the agent's own agent-scoped key outside get_current_user (#307)"),
    "routers/agents.py::agent_execution_result": (
        "POST /api/agents/{agent_name}/executions/{execution_id}/result",
        "terminal result delivery; authenticates the agent's own key and path self-match (#1083)"),
    "routers/reports.py::create_report": (
        "POST /api/agents/{name}/reports", "structured output an agent publishes about itself (self-check)"),
    "routers/notifications.py::create_notification": (
        "POST /api/notifications", "an agent raises a notification; agent name comes from its key (NOTIF-001)"),
    # MCP schedule tools — the agent's own use; autonomy (person-only) bounds cron firing.
    "routers/schedules.py::create_schedule": (
        "POST /api/agents/{name}/schedules", "MCP create_schedule; unattended firing is gated by autonomy"),
    "routers/schedules.py::update_schedule": (
        "PUT /api/agents/{name}/schedules/{schedule_id}", "MCP update_schedule"),
    "routers/schedules.py::delete_schedule": (
        "DELETE /api/agents/{name}/schedules/{schedule_id}", "MCP delete_schedule"),
    "routers/schedules.py::enable_schedule": (
        "POST /api/agents/{name}/schedules/{schedule_id}/enable", "MCP enable_schedule; cron still gated by autonomy"),
    "routers/schedules.py::disable_schedule": (
        "POST /api/agents/{name}/schedules/{schedule_id}/disable", "MCP disable_schedule"),
    "routers/schedules.py::trigger_schedule": (
        "POST /api/agents/{name}/schedules/{schedule_id}/trigger", "MCP trigger_schedule (a manual run)"),
    # Reads of the settings whose writes #2996 made person-only.
    "routers/agent_config.py::get_agent_api_key_setting": ("GET /api/agents/{agent_name}/api-key-setting", "a non-sensitive read of this agent's own setting (access-level)"),
    "routers/agent_config.py::get_agent_autonomy_status": ("GET /api/agents/{agent_name}/autonomy", "a non-sensitive read of this agent's own setting (access-level)"),
    "routers/agent_config.py::get_agent_read_only_status": ("GET /api/agents/{agent_name}/read-only", "a non-sensitive read of this agent's own setting (access-level)"),
    "routers/agent_config.py::get_agent_resources": ("GET /api/agents/{agent_name}/resources", "a non-sensitive read of this agent's own setting (access-level)"),
    "routers/agent_config.py::get_agent_capabilities": ("GET /api/agents/{agent_name}/capabilities", "a non-sensitive read of this agent's own setting (access-level)"),
    "routers/agent_config.py::get_agent_capacity": ("GET /api/agents/{agent_name}/capacity", "a non-sensitive read of this agent's own setting (access-level)"),
    "routers/agent_config.py::get_agent_timeout": ("GET /api/agents/{agent_name}/timeout", "a non-sensitive read of this agent's own setting (access-level)"),
    "routers/agent_config.py::get_public_channel_model": ("GET /api/agents/{agent_name}/public-channel-model", "a non-sensitive read of this agent's own setting (access-level)"),
    "routers/agent_config.py::get_agent_guardrails": ("GET /api/agents/{agent_name}/guardrails", "a non-sensitive read of this agent's own setting (access-level)"),
    "routers/users.py::get_my_github_pat_status": (
        "GET /api/users/me/github-pat", "configured flags only, never the token (ent#162)"),
    # Skill sets (ent#530). The writes are the USE of the skills-manage capability, fenced like
    # the single-skill writes by get_skill_managed_agent_by_name (capability, then owner); the
    # GRANT is PUT /agents/{name}/skill-manager, admin + interactive (ent#596). Not a setting.
    "routers/skills.py::list_skill_sets": (
        "GET /api/skills/library/sets", "library set metadata (names, members, schedules), open like the skills listing"),
    "routers/skills.py::get_agent_skill_sets": (
        "GET /api/agents/{agent_name}/skill-sets", "a read of this agent's sets; ?probe= honoured only under the ent#596 fence (#3052)"),
    "routers/skills.py::assign_skill_set": (
        "POST /api/agents/{agent_name}/skill-sets/{set_name}", "ent#596 skills-manage USE: capability holder + owner fence; grant is admin-interactive"),
    "routers/skills.py::unassign_skill_set": (
        "DELETE /api/agents/{agent_name}/skill-sets/{set_name}", "ent#596 skills-manage USE: capability holder + owner fence; grant is admin-interactive"),
    # The agent's own ask (ent#611): what MCP ask_operator calls; get_self_acting_agent admits
    # only the agent's own key (or trinity-system as itself) and refuses every person.
    "routers/operator_queue.py::raise_my_ask": (
        "POST /api/agents/{name}/operator-queue", "MCP ask_operator; the agent raises its own ask, self-only (get_self_acting_agent)"),
    # trinity-enterprise#752: the in-container skill-gate hook reads its own gates and its own
    # run's clearance; the agent comes from the key (get_self_agent), every person is refused.
    "routers/skill_gate.py::check_skill_invocation": (
        "POST /api/skill-gate/check", "the in-container hook's check: own gates and own run only, agent from the key (get_self_agent)"),
    # trinity-enterprise#753: the per-agent skill gate map. The writes take the #3236 shape
    # (require_person_or_capability("skills.manage", self_person_only=True)): a person, or a
    # skills.manage holder on an agent its owner owns — never itself; then the owner fence.
    # The read: an agent key reads its own gates (the hook pulls), a holder an agent its owner owns.
    "routers/skill_gate.py::list_agent_skill_gates": (
        "GET /api/agents/{agent_name}/skill-gates", "ent#753 read: anyone with access; an agent key its own gates, a skills.manage holder an agent its owner owns"),
    "routers/skill_gate.py::set_agent_skill_gate": (
        "PUT /api/agents/{agent_name}/skill-gates/{skill_name}", "ent#753 skills.manage USE on an owner-owned agent, never itself; persons pass assert_person + owner fence"),
    "routers/skill_gate.py::clear_agent_skill_gate": (
        "DELETE /api/agents/{agent_name}/skill-gates/{skill_name}", "ent#753 skills.manage USE on an owner-owned agent, never itself; persons pass assert_person + owner fence"),
    # ent#703: the agent's pull loop reads its own switch every cycle with its own key.
    # The write (PUT .../git/pull-sync) is a setting, so it is person-only.
    "routers/git.py::get_pull_sync_config": (
        "GET /api/agents/{agent_name}/git/pull-sync", "ent#703 the agent's pull loop reads its own switch live each cycle"),
}

# Authenticates itself, or unauthenticated by design. key -> ("METHOD /full/path", reason)
_SECRET = "internal shared secret (router-level verify_internal_secret, C-003)"
_PUBLIC_LINK = "public-link surface; the link token in the path is the credential"
_PULL = "pull seam: internal secret OR the agent's own scoped key (#1081)"
_INLINE = "internal secret + inline-auth flag (#848)"

OWN_AUTH: Dict[str, Tuple[str, str]] = {
    "routers/internal.py::agent_files_share": ("POST /api/internal/agent-files/share", _SECRET),
    "routers/internal.py::complete_activity": ("POST /api/internal/activities/{activity_id}/complete", _SECRET),
    "routers/internal.py::execute_task_internal": ("POST /api/internal/execute-task", _SECRET),
    "routers/internal.py::internal_agent_brief_readiness": ("GET /api/internal/agents/{agent_name}/brief-readiness", _SECRET),
    "routers/internal.py::internal_agent_pre_check": ("POST /api/internal/agents/{agent_name}/pre-check", _SECRET),
    "routers/internal.py::internal_agent_sync_health": ("GET /api/internal/agents/{agent_name}/sync-health-status", _SECRET),
    "routers/internal.py::internal_health": ("GET /api/internal/health", _SECRET),
    "routers/internal.py::internal_next_task": ("GET /api/internal/next-task", _PULL),
    "routers/internal.py::internal_task_result": ("POST /api/internal/tasks/{execution_id}/result", _PULL),
    "routers/internal.py::log_audit_entry": ("POST /api/internal/audit", _SECRET),
    "routers/internal.py::mcp_exposed_agents": ("GET /api/internal/mcp-exposed-agents", _SECRET),
    "routers/internal.py::track_activity": ("POST /api/internal/activities/track", _SECRET),
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
DELEGATED: Dict[str, Tuple[str, str, str]] = {}

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
FROZEN_BASELINE_COUNT = 359


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


def classify(
    census: Census,
    baseline: Dict[str, str],
    *,
    admin_widened=None,
    agent_callable=None,
    own_auth=None,
    delegated=None,
) -> Tuple[Dict[str, str], List[Tuple[str, str]]]:
    """Return ({key: class}, [(rule tag, message)])."""
    admin_widened = ADMIN_WIDENED if admin_widened is None else admin_widened
    agent_callable = AGENT_CALLABLE if agent_callable is None else agent_callable
    own_auth = OWN_AUTH if own_auth is None else own_auth
    delegated = DELEGATED if delegated is None else delegated
    lists = {
        "agent_callable": agent_callable,
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
        if len(member) > 1:
            failures.append((TWO_LISTS, f"{key} is in more than one list: {member}"))
        if not member:
            failures.append((UNCLASSIFIED, (
                f"{key} ({','.join(route.methods)}, line {route.lineno}) is unclassified. Gate it "
                f"(Depends(require_person|require_interactive|require_admin), or the imperative "
                f"call as the handler's first statement on its principal), or list it in "
                f"AGENT_CALLABLE / OWN_AUTH / DELEGATED in tests/unit/_route_census.py with a "
                f"reason. Never add it to the baseline.")))
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
    listed = set(AGENT_CALLABLE) | set(OWN_AUTH) | set(DELEGATED)
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
