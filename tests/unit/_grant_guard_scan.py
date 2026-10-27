"""No grant route is guarded by an agent-only check alone (trinity-enterprise#719).

An agent-only check refuses a principal because it carries an agent name
(`reject_agent_principal`, `if current_user.agent_name: raise`, an early
`return` for every non-agent before a refusal). The `system` key and every
scope that is neither an agent key nor a person's carry no agent name, so the
check admits them. On a route that grants something, an agent-only check is
therefore never enough on its own: the route also needs an allowlist (a person
or an interactive rule) or the admin gate.

The route set and each route's policy class come from the census
(`_route_census.py`); the tables below list only reviewed exceptions, each with
its reason. Each route is followed into the code it runs, across modules — its
handler, the dependencies in its signature and decorator (an `Annotated` alias
or an inline `Annotated` parameter included, and a dependency's own
dependencies), and every module-level function those call — so a check that
lives in a shared helper or a service counts for each route that reaches it.
`dependencies.py` is read as the gate module: its gates are recognised by name
and not followed further, and any other function in it that holds an
agent-only check counts as a site where it is called.

Three checks, one per shape the rule takes:

A. Agent-only sites. A route that reaches an agent-only check passes when its
   census class is `person`, `interactive` or `person_or_grant` (the allowlist
   runs in the decorator, before the handler); when it is admin-gated (census
   class `admin_tier` / `admin_widened`, or an admin gate the handler calls as
   a top-level statement before any `return`), which is not "alone" and is the
   admin tier's own question; or when the function holding the check refuses
   a non-person the same way (an allowlist gate, or `if not <person
   predicate>: <refuse>` refusing on every path, as a top-level statement
   before any `return`). A reviewed exemption names the check AND the routes it
   covers: `NOT_GRANT_GUARDS` (a check that guards no grant),
   `REVIEWED_CREDITS` (a person check the reading cannot credit) and `DEFERRED`
   (a grant this check does not settle, printed with the follow-up that rules
   it). Anything else is a finding, a listed check reached from a route its
   entry does not name included. A check counts as agent-only when its refusal
   is keyed on the agent name: the name itself, a test that it is set
   (`is not None`, `!= None`), a test that the scope is the agent
   scope, or an `and` / `or` with one of those as a term (`agent_name and
   reserved` admits every principal that carries no agent name). Every
   agent-only check must be reached by some census route or be listed: a check
   the scan cannot reach is a problem, not a pass.
B. The Workspace write list. `get_portal_principal` refuses agent keys and
   admits the `system` key on a surface #2198 ruled a use. Every census `portal`
   route with a write method must either refuse a non-person as its first
   statement (`if not principal.is_person: <refuse on every path>`, or a call
   to a helper whose first statement is that) or be listed in `WORKSPACE_USES`
   with its reason (or in `DEFERRED`, when one of its arms is a grant ruled
   elsewhere; once that arm refuses a non-person the entry is stale). Reads
   stay out: a machine's reads of rows about a person are
   trinity-enterprise#948's ruling. Outside `portal`-class routes the door's own
   agent-only check is a check A site, exempt only where a listed
   `DOOR_FALLBACKS` function is the route's only way to it.
C. The room moderator door. `_require_moderator` admits a moderator on more
   than one branch. Every way it can admit (each `return` that is not a
   refusal, and falling off its end) must sit inside an `if` whose test carries
   a person term as one of its `and` terms, or follow a top-level statement
   that refuses every non-person. Every route that reaches the door is
   reported for each exit that does not.

A person term is positive only: `is_person_principal`, a local bound from it
and from nothing else, an `and` with it, or a helper whose every truthy return
is one. `not` and `or` do not make one, and a helper that answers a truthy constant is not one unless
`PERSON_BRANCHES` lists that exact branch for it (a caller who is a person by
construction there). Only the function's own scope is read (a nested
function's imports, bindings and returns are not its). A gate factory
(`require_person_or_capability(...)`, `require_admin_allowing(...)`) checks no
one when it is called, so a call to one in a body is never credited; on a
route, `Depends(<factory>(...))` is credited through the census class.

Stated limits (what a green run does not prove): a route with no machine
refusal at all is the census baseline's, not this scan's; a credited allowlist
or admin call runs before any `return`, but is not shown to run before the
effect it guards; a call through an object attribute (`db.x`, `self.x`), a
method or a module-level alias of a gate is not followed (a check reached only
that way fails as unreached; one also reached another way is read only on
that way); a refusal through a raising helper (`if agent_name: _forbid()`), a
principal parameter named outside the census's list, or a value derived from
the principal by another function (a participant kind) is not read as a site;
a name resolves from imports (in source order) and module-level definitions,
not from a local assignment or the branch an import sits in; the value a
`PERSON_BRANCHES` condition reads (`WORKSPACE_KIND`) is not checked;
WebSocket routes are outside the census route table; and the private tree
(`enterprise/`) is excluded, as the census excludes it.

Pure stdlib; it reads source and never imports `src/`. Underscore-prefixed so
pytest does not collect it. By hand: `python tests/unit/_grant_guard_scan.py`
prints the report and exits 1 on any finding or table problem.
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Set, Tuple, Union

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _route_census as rc  # noqa: E402

# --------------------------------------------------------------------------- #
# What the gate module's names mean
# --------------------------------------------------------------------------- #

GATE_MODULE = "dependencies"
DENYLIST_GATES = frozenset({"reject_agent_principal", "human_only"})
ALLOWLIST_GATES = frozenset(
    {
        "assert_person",
        "reject_non_person_principal",
        "reject_non_interactive_principal",
        "require_person",
        "require_interactive",
        "require_person_or_capability",
    }
)
PERSON_PREDICATES = frozenset({"is_person_principal", "is_interactive_principal"})
ADMIN_GATES = frozenset({"assert_admin", "require_admin", "require_admin_allowing"})
# Calling one of these builds a dependency and checks no one; only
# `Depends(<factory>(...))` runs the check.
GATE_FACTORIES = frozenset({"require_person_or_capability", "require_admin_allowing"})
_GATE_NAMES = DENYLIST_GATES | ALLOWLIST_GATES | PERSON_PREDICATES | ADMIN_GATES

CLOSED_CLASSES = frozenset({"person", "interactive", "person_or_grant"})
ADMIN_CLASSES = frozenset({"admin_tier", "admin_widened"})
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# The shared doors the scan follows by name (the census already names the
# Workspace door as its `portal` gate).
WORKSPACE_DOOR = "client_portal/portal_auth.py::get_portal_principal"
ROOM_MODERATOR_DOOR = "shared_sessions/service.py::_require_moderator"

# --------------------------------------------------------------------------- #
# The reviewed tables
# --------------------------------------------------------------------------- #

# Each reviewed exemption below names the routes it covers, not only the check:
# a new route that reaches a listed check is a finding until a reviewer adds it,
# and a named route that no longer rests on the check alone (gone, person-gated,
# or credited another way) fails as stale. Values: (reason, routes).

# Agent-only checks that guard no grant, keyed by the function that holds the
# check; a key the scan no longer finds as an agent-only site fails as stale.
NOT_GRANT_GUARDS: Dict[str, Tuple[str, FrozenSet[str]]] = {
    "dependencies.py::enforce_agent_spawn_scope": (
        "lifecycle fence (trinity-enterprise#69 Part 2): it bounds which agents "
        "an agent key may start, stop or delete to the ones it spawned; it guards "
        "no grant",
        frozenset(
            {
                "routers/agents.py::delete_agent_endpoint",
                "routers/agents.py::start_agent_endpoint",
                "routers/agents.py::stop_agent_endpoint",
            }
        ),
    ),
    "routers/skills.py::get_skill": (
        "a read of the shared skills library: no credential, no person field, "
        "nothing granted",
        frozenset({"routers/skills.py::get_skill"}),
    ),
    "routers/skills.py::get_skill_assignments": (
        "a read of which agents carry which library skills: nothing granted",
        frozenset({"routers/skills.py::get_skill_assignments"}),
    ),
    "routers/systems.py::restart_system": (
        "a lifecycle use the system agent makes (fleet restart); it changes "
        "nothing about what the agents may do",
        frozenset({"routers/systems.py::restart_system"}),
    ),
    "routers/mcp_keys.py::get_mcp_api_key_endpoint": (
        "a read of one key record of the caller's own account, never its "
        "secret; the check narrows an agent key to its own key "
        "(trinity-enterprise#872); nothing granted",
        frozenset({"routers/mcp_keys.py::get_mcp_api_key_endpoint"}),
    ),
}

# Agent-only sites whose own function refuses a non-person on every path a
# platform credential takes, which the reading cannot show (a `return` comes
# before the person check). Keyed and pinned like NOT_GRANT_GUARDS; a named
# route the scan credits without the entry fails as redundant.
REVIEWED_CREDITS: Dict[str, Tuple[str, FrozenSet[str]]] = {
    "services/person_mention_service.py::tagger_for": (
        "its one `return` before `is_person_principal` is the Workspace "
        "principal's branch (`is_portal`), and on these routes a Workspace "
        "principal is never a platform one: the room principal resolves a "
        "platform credential to its account first and falls back to the "
        "Workspace door only for a credential the platform rejects (ent#362), so "
        "every platform key reaches the person check",
        frozenset(
            {
                "shared_sessions/router.py::create_room",
                "shared_sessions/router.py::get_room",
                "shared_sessions/router.py::post_message",
                "shared_sessions/router.py::room_people",
            }
        ),
    ),
}

# Grants this scan does not settle: each passes, is printed in the report, and
# names the follow-up that rules it. Keyed like the table it stands in for (an
# agent-only site for check A, a Workspace write for check B, whose routes are
# the write itself). An entry that no longer covers an open route fails as
# stale, so it leaves when the ruling lands; a Workspace write whose arm comes
# to refuse a non-person fails as stale too, since its other arm is then a use
# to list in WORKSPACE_USES.
_FOLLOW_UP = "ruled in a follow-up linked from trinity-enterprise#719"
_DELETE_DOCUMENT = "client_portal/router.py::portal_delete_document"
DEFERRED: Dict[str, Tuple[str, FrozenSet[str]]] = {
    _DELETE_DOCUMENT: (
        "`scope=me` is the viewer's own dismissal, a use; `scope=everyone` is the "
        "owner's revoke of the share for every viewer, the same act as "
        "`DELETE /api/agents/{agent_name}/shared-files/{file_id}`, which is still in "
        f"the census baseline; the two are {_FOLLOW_UP}",
        frozenset({_DELETE_DOCUMENT}),
    ),
}

# Person helpers that answer a constant `True` on one branch: the helper, the
# exact condition of that branch (its `if` tests joined by `and`), and why the
# caller is a person by construction there. Anywhere else a truthy constant
# disqualifies the helper. An entry for a helper the tree defines without that
# branch fails as stale; one for a helper the tree does not define is printed,
# not failed, so the table can name a helper before it lands.
PERSON_BRANCHES: Dict[str, Tuple[str, str]] = {
    "shared_sessions/service.py::_is_room_person": (
        "kind == WORKSPACE_KIND",
        "a Workspace client's participant kind: a verified email, a person by "
        "construction",
    ),
}

# Functions through which a non-Workspace route reaches the Workspace door only
# as a fallback a platform credential never takes. Outside `portal`-class
# routes the door's agent-only check is exempt only when the route stops
# reaching the door once these are taken out of its reach. An entry the tree
# does not define, or that no route uses, fails as stale.
DOOR_FALLBACKS: Dict[str, str] = {
    "shared_sessions/router.py::get_room_principal": (
        "the room principal resolves a platform credential to its account "
        "first; only a credential the platform rejects falls back to the "
        "Workspace door (ent#362)"
    ),
}

# Workspace writes that are uses (#2198 ruled the Workspace a use surface, on
# which the `system` key keeps its breadth). A write missing from here must
# refuse a non-person first; an entry for a write that already does fails as
# redundant, and an entry for a route that is no longer a portal write fails as
# stale.
_WS_USE = "a use on the Workspace use surface (#2198)"
_WS_SESSION = f"the viewer's own chat sessions: a per-viewer {_WS_USE}"
_WS_TURN = f"a chat turn, or its speech conversion, for the viewer: {_WS_USE}"
_WS_FILES = f"the viewer's own uploads and documents: {_WS_USE}"
_WS_CANVAS = f"removing a canvas from the agent's Workspace: ruled {_WS_USE}"
WORKSPACE_USES: Dict[str, str] = {
    "client_portal/router.py::portal_archive_session": _WS_SESSION,
    "client_portal/router.py::portal_unarchive_session": _WS_SESSION,
    "client_portal/router.py::portal_rename_session": _WS_SESSION,
    "client_portal/router.py::portal_create_session": _WS_SESSION,
    "client_portal/router.py::portal_reset_main_session": _WS_SESSION,
    "client_portal/router.py::portal_star_chat": _WS_SESSION,
    "client_portal/router.py::portal_unstar_chat": _WS_SESSION,
    "client_portal/router.py::portal_mark_chat_read": _WS_SESSION,
    "client_portal/router.py::portal_chat": _WS_TURN,
    "client_portal/router.py::portal_chat_stream": _WS_TURN,
    "client_portal/router.py::portal_voice_start": _WS_TURN,
    "client_portal/router.py::portal_tts": _WS_TURN,
    "client_portal/router.py::portal_stt": _WS_TURN,
    "client_portal/router.py::portal_upload": _WS_FILES,
    "client_portal/router.py::portal_delete_upload": _WS_FILES,
    "client_portal/router.py::portal_delete_canvas": _WS_CANVAS,
    "client_portal/router.py::portal_bulk_delete_canvases": _WS_CANVAS,
    "client_portal/router.py::portal_submit_rating": (
        "the viewer's rating of a reply: a use on the Workspace use surface (#2198)"
    ),
    "client_portal/router.py::portal_terminate_execution": (
        "stopping the viewer's running turn: a use on the Workspace use surface (#2198)"
    ),
    "client_portal/router.py::portal_agent_memory_undo": (
        f"undoing a memory write from the viewer's own chat: {_WS_USE}"
    ),
    "client_portal/suggestions/router.py::post_feedback": (
        f"the viewer's feedback on a suggestion: {_WS_USE}"
    ),
}

# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #

AGENT_ONLY_SITE = "agent-only-site"
WORKSPACE_WRITE = "workspace-write"
ROOM_MODERATOR = "room-moderator"


@dataclass(frozen=True)
class Site:
    """An agent-only check: the function that holds it, its line, its shape."""

    key: str
    line: int
    shape: str


@dataclass(frozen=True)
class Finding:
    check: str
    route: str
    methods: Tuple[str, ...]
    site: str
    line: int
    detail: str


@dataclass
class Report:
    findings: List[Finding] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)
    not_alone: List[Tuple[str, str, str]] = field(
        default_factory=list
    )  # (route, site, why)
    deferred: List[Tuple[str, str]] = field(default_factory=list)  # (route, key)
    routes: int = 0
    sites: int = 0
    workspace_writes: int = 0
    door_routes: List[str] = field(default_factory=list)
    absent_person_branches: List[str] = field(default_factory=list)
    baseline: int = 0
    literal: int = 0

    @property
    def routes_found(self) -> List[str]:
        return sorted({f.route for f in self.findings})


# --------------------------------------------------------------------------- #
# The function index
# --------------------------------------------------------------------------- #


FnNode = Union[ast.FunctionDef, ast.AsyncFunctionDef]
Ref = Tuple[str, str]  # (dotted module, name)


@dataclass
class _Fn:
    key: str
    rel: str
    module: str
    node: FnNode
    idx: rc._FileIndex
    names: Dict[str, Ref]  # function-local `from x import y`, nested scopes included
    modules: Dict[str, str]  # function-local `import x`, nested scopes included
    own_names: Dict[str, Ref]  # the same, from the function's own scope only
    own_modules: Dict[str, str]


def _local_imports(
    rel: str, node: FnNode, walk=ast.walk
) -> Tuple[Dict[str, Ref], Dict[str, str]]:
    """Imports inside a function. With `ast.walk` a nested scope's imports
    count too; with `_walk_local` (what a credit reads) they do not, since a
    nested import never binds the outer name. Finding a site and following a
    call read both (`_Index.refs`)."""
    names: Dict[str, Ref] = {}
    modules: Dict[str, str] = {}
    for n in walk(node):
        if isinstance(n, ast.ImportFrom):
            mod = rc._resolve_relative(rel, n.module, n.level)
            for alias in n.names:
                local = alias.asname or alias.name
                names[local] = (mod, alias.name)
                modules[local] = f"{mod}.{alias.name}" if mod else alias.name
        elif isinstance(n, ast.Import):
            for alias in n.names:
                if alias.asname:
                    modules[alias.asname] = alias.name
                else:
                    top = alias.name.split(".")[0]
                    modules[top] = top
    return names, modules


def _resolve(
    expr: ast.AST,
    idx: rc._FileIndex,
    module: str,
    names: Dict[str, Ref],
    modules: Dict[str, str],
) -> Optional[Ref]:
    """(module, name) an expression refers to, import aliases followed."""
    if isinstance(expr, ast.Name):
        if expr.id in names:
            return names[expr.id]
        if expr.id in idx.local_defs:
            return module, expr.id
        return idx.names.get(expr.id)
    if isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name):
        owner = expr.value.id
        mod = modules.get(owner)
        if mod is None and owner not in idx.local_defs:
            mod = idx.modules.get(owner)
        return (mod, expr.attr) if mod else None
    return None


class _Index:
    def __init__(self, root: Path):
        self.root = root
        self.files: Dict[str, rc._FileIndex] = {}
        # rel -> top-level function name -> node
        self.top: Dict[str, Dict[str, FnNode]] = {}
        # rel -> `Annotated[..., Depends(f)]` alias name -> `f`
        self.aliases: Dict[str, Dict[str, ast.AST]] = {}
        # rel -> runtime-style qualname -> node
        self.defs: Dict[str, Dict[str, FnNode]] = {}
        self._fns: Dict[str, _Fn] = {}
        # PERSON_BRANCHES (or a planted tree's), set by `scan`.
        self.person_branches: Dict[str, Tuple[str, str]] = {}
        for p in rc.backend_files(root):
            rel = p.relative_to(root).as_posix()
            tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
            self.files[rel] = rc._FileIndex(rel, tree)
            self.top[rel] = {
                n.name: n
                for n in tree.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            self.defs[rel] = {
                q: n
                for q, n in rc._walk_defs(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            self.aliases[rel] = {}
            for n in tree.body:
                if (
                    isinstance(n, ast.Assign)
                    and len(n.targets) == 1
                    and isinstance(n.targets[0], ast.Name)
                ):
                    target = _annotated_depends(n.value)
                    if target is not None:
                        self.aliases[rel][n.targets[0].id] = target

    def fn(self, rel: str, qualname: str) -> Optional[_Fn]:
        key = f"{rel}::{qualname}"
        if key in self._fns:
            return self._fns[key]
        node = self.defs.get(rel, {}).get(qualname)
        if node is None:
            return None
        names, modules = _local_imports(rel, node)
        own_names, own_modules = _local_imports(rel, node, _walk_local)
        made = _Fn(
            key, rel, rc._module_of(rel), node, self.files[rel],
            names, modules, own_names, own_modules,
        )
        self._fns[key] = made
        return made

    def ref(self, fn: _Fn, expr: ast.AST) -> Optional[Ref]:
        """What `expr` refers to inside `fn` (its own imports first)."""
        return _resolve(expr, fn.idx, fn.module, fn.names, fn.modules)

    def own_ref(self, fn: _Fn, expr: ast.AST) -> Optional[Ref]:
        """`ref`, with only the imports of `fn`'s own scope: what a credit reads."""
        return _resolve(expr, fn.idx, fn.module, fn.own_names, fn.own_modules)

    def refs(self, fn: _Fn, expr: ast.AST) -> List[Ref]:
        """Both readings (`ref` and `own_ref`), distinct: what finding a site
        and following a call read, so a rebinding in either cannot hide one."""
        out: List[Ref] = []
        for r in (self.ref(fn, expr), self.own_ref(fn, expr)):
            if r is not None and r not in out:
                out.append(r)
        return out

    def module_ref(self, rel: str, expr: ast.AST) -> Optional[Ref]:
        """What `expr` refers to at the top level of the module `rel`."""
        return _resolve(expr, self.files[rel], rc._module_of(rel), {}, {})

    def target(self, ref: Ref, depth: int = 0) -> Optional[_Fn]:
        """The top-level function a (module, name) resolves to, through package
        re-exports, or None (a class, an object, a third-party name)."""
        mod, name = ref
        rel = rc._module_to_relpath(mod, self.root)
        if rel is None or rel not in self.files:
            return None
        if name in self.top[rel]:
            return self.fn(rel, name)
        if depth < 3 and name in self.files[rel].names:
            return self.target(self.files[rel].names[name], depth + 1)
        return None


def _annotated_depends(value: ast.AST) -> Optional[ast.AST]:
    """`Annotated[T, Depends(f)]` -> `f`."""
    if not isinstance(value, ast.Subscript):
        return None
    head = value.value
    name = head.id if isinstance(head, ast.Name) else getattr(head, "attr", None)
    if name != "Annotated" or not isinstance(value.slice, ast.Tuple):
        return None
    for e in value.slice.elts[1:]:
        if isinstance(e, ast.Call) and rc._is_depends(e):
            return e.args[0]
    return None


def _is_gate(ref: Optional[Ref], names: FrozenSet[str]) -> bool:
    return ref is not None and ref[0] == GATE_MODULE and ref[1] in names


# --------------------------------------------------------------------------- #
# Reading one function
# --------------------------------------------------------------------------- #


def _agent_aliases(fn: ast.AST, principals: Set[str]) -> Set[str]:
    return {
        n.targets[0].id
        for n in ast.walk(fn)
        if isinstance(n, ast.Assign)
        and len(n.targets) == 1
        and isinstance(n.targets[0], ast.Name)
        and rc._agent_name_of(n.value, principals, set())
    }


def _keyed_on_agent(test: ast.AST, principals: Set[str], aliases: Set[str]) -> bool:
    """Is `test` keyed on the principal's agent name: the name itself, a test
    that it is set (`is not None`, `!= None`), a test that the scope
    is the agent scope (`mcp_scope == "agent"`, or `in` a literal naming it),
    or an `and` / `or` with one of those as a term? Under an `and` the refusal
    admits every principal that carries no agent name; under an `or` it refuses
    every agent. Either way it is a denylist on the agent name, not a person
    rule."""
    if isinstance(test, ast.BoolOp):
        return any(_keyed_on_agent(v, principals, aliases) for v in test.values)
    # The census helper below reads `and` / `or` too; it never sees one here,
    # so the branch above is the one rule for them (a self-test pins it).
    if isinstance(test, (ast.BoolOp, ast.IfExp)):
        return False
    if isinstance(test, ast.Compare) and len(test.ops) == 1:
        return _set_test(test, principals, aliases) or _agent_scope_test(
            test, principals
        )
    return rc._agent_name_of(test, principals, aliases)


def _set_test(test: ast.Compare, principals: Set[str], aliases: Set[str]) -> bool:
    """`<agent name> is not None` or `!= None`, either operand order."""
    if not isinstance(test.ops[0], (ast.IsNot, ast.NotEq)):
        return False
    pair = (test.left, test.comparators[0])
    for name, other in (pair, pair[::-1]):
        if (
            isinstance(other, ast.Constant)
            and other.value is None
            and rc._agent_name_of(name, principals, aliases)
        ):
            return True
    return False


def _scope_of(expr: ast.AST, principals: Set[str]) -> bool:
    """`<principal>.mcp_scope` or `getattr(<principal>, "mcp_scope", ...)`."""
    if isinstance(expr, ast.Attribute):
        return (
            expr.attr == "mcp_scope"
            and isinstance(expr.value, ast.Name)
            and expr.value.id in principals
        )
    if (
        isinstance(expr, ast.Call)
        and isinstance(expr.func, ast.Name)
        and expr.func.id == "getattr"
        and len(expr.args) >= 2
    ):
        owner, key = expr.args[0], expr.args[1]
        return (
            isinstance(owner, ast.Name)
            and owner.id in principals
            and isinstance(key, ast.Constant)
            and key.value == "mcp_scope"
        )
    return False


def _agent_scope_test(test: ast.Compare, principals: Set[str]) -> bool:
    """`<scope> == "agent"` (either order), or `<scope> in` a literal tuple,
    list or set that names "agent"."""
    op, left, right = test.ops[0], test.left, test.comparators[0]
    if isinstance(op, ast.Eq):
        for scope, other in ((left, right), (right, left)):
            if (
                _scope_of(scope, principals)
                and isinstance(other, ast.Constant)
                and other.value == "agent"
            ):
                return True
        return False
    if isinstance(op, ast.In) and isinstance(right, (ast.Tuple, ast.List, ast.Set)):
        return _scope_of(left, principals) and any(
            isinstance(e, ast.Constant) and e.value == "agent" for e in right.elts
        )
    return False


def _no_agent(test: ast.AST, principals: Set[str], aliases: Set[str]) -> bool:
    """`not <the principal's agent name>`."""
    return (
        isinstance(test, ast.UnaryOp)
        and isinstance(test.op, ast.Not)
        and _keyed_on_agent(test.operand, principals, aliases)
    )


def _refuses_all(body: List[ast.stmt]) -> bool:
    """The branch refuses every principal that takes it: a top-level refusal
    with no `return` before it. A branch that first returns for some callers
    (`if identity: if header == identity: return ...; raise`) checks which
    agent, not whether it is one."""
    for stmt in body:
        if isinstance(stmt, ast.Raise):
            return True
        if isinstance(stmt, ast.Return):
            return rc._refuses([stmt])
        if any(isinstance(n, ast.Return) for n in ast.walk(stmt)):
            return False
    return False


def _sites_in(index: _Index, fn: _Fn) -> List[Site]:
    node = fn.node
    out: List[Site] = []
    for n in ast.walk(node):
        if not isinstance(n, ast.Call):
            continue
        for ref in index.refs(fn, n.func):
            if _is_gate(ref, DENYLIST_GATES):
                out.append(Site(fn.key, n.lineno, f"{ref[1]}(...)"))
                break
    principals = rc._principal_like_params(node)
    if principals:
        aliases = _agent_aliases(node, principals)
        for n in ast.walk(node):
            if (
                isinstance(n, ast.If)
                and _keyed_on_agent(n.test, principals, aliases)
                and _refuses_all(n.body)
            ):
                out.append(
                    Site(fn.key, n.lineno, f"if {ast.unparse(n.test)}: <refuse>")
                )
        body = list(node.body)
        for i, stmt in enumerate(body):
            if (
                isinstance(stmt, ast.If)
                and _no_agent(stmt.test, principals, aliases)
                and stmt.body
                and isinstance(stmt.body[0], ast.Return)
                and not stmt.orelse
                and rc._refuses(body[i + 1 :])
            ):
                out.append(
                    Site(
                        fn.key,
                        stmt.lineno,
                        f"if {ast.unparse(stmt.test)}: return; <refuse>",
                    )
                )
    return out


_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _walk_local(node: ast.AST):
    """`ast.walk` over one function's own scope: a nested function, lambda or
    class is not entered, since what it binds or returns is not this
    function's."""
    todo = list(reversed(list(ast.iter_child_nodes(node))))
    while todo:  # depth-first in source order, so a later import wins
        n = todo.pop()
        yield n
        if not isinstance(n, _SCOPES):
            todo.extend(reversed(list(ast.iter_child_nodes(n))))


def _guard_text(guards: List[ast.expr]) -> str:
    return " and ".join(ast.unparse(t) for t in guards)


def _truthy_constant(v: Optional[ast.AST]) -> bool:
    return isinstance(v, ast.Constant) and bool(v.value)


def _person_call(index: _Index, fn: _Fn, call: ast.AST, depth: int = 0) -> bool:
    """A call to a person predicate, or to a helper whose every truthy return
    is a positive person term, at least one of them. A falsy return denies, so
    it is allowed; a truthy constant admits without asking, so it disqualifies
    the helper unless it sits on the one branch `PERSON_BRANCHES` lists for it
    (the exact `if` tests that hold it)."""
    if not isinstance(call, ast.Call):
        return False
    ref = index.own_ref(fn, call.func)
    if _is_gate(ref, PERSON_PREDICATES):
        return True
    if ref is None or depth >= 3:
        return False
    callee = index.target(ref)
    if callee is None:
        return False
    bound = _person_bound(index, callee, depth + 1)
    listed = index.person_branches.get(callee.key)
    person = False
    for ret, guards in _returns(callee.node):
        v = ret.value
        if v is None or (isinstance(v, ast.Constant) and not v.value):
            continue
        if _person_term(index, callee, v, bound, depth + 1):
            person = True
        elif not (
            _truthy_constant(v)
            and listed is not None
            and _guard_text(guards) == listed[0]
        ):
            return False
    return person


def _person_term(
    index: _Index, fn: _Fn, expr: ast.AST, bound: Set[str], depth: int = 0
) -> bool:
    """A positive person term: a person call, a local bound from one, or an
    `and` with one as a term. `not <it>` and an `or` with it are not."""
    if isinstance(expr, ast.BoolOp) and isinstance(expr.op, ast.And):
        return any(_person_term(index, fn, v, bound, depth) for v in expr.values)
    if isinstance(expr, ast.Name):
        return expr.id in bound
    return _person_call(index, fn, expr, depth)


def _person_bound(index: _Index, fn: _Fn, depth: int = 0) -> Set[str]:
    """Locals bound (`x = ...`) from a positive person term, in `fn`'s own
    scope, and never bound any other way there: a later `x = True`, an
    augmented assignment, a loop or `with` target, a walrus or an unpacking
    leaves `x` no person term."""
    person_targets: Set[int] = set()
    for n in _walk_local(fn.node):
        if isinstance(n, (ast.Assign, ast.AnnAssign)):
            targets = n.targets if isinstance(n, ast.Assign) else [n.target]
            if (
                len(targets) == 1
                and isinstance(targets[0], ast.Name)
                and n.value is not None
                and _person_term(index, fn, n.value, set(), depth)
            ):
                person_targets.add(id(targets[0]))
    good: Set[str] = set()
    other: Set[str] = set()
    for n in _walk_local(fn.node):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            (good if id(n) in person_targets else other).add(n.id)
    return good - other


def _admits(node: ast.AST) -> bool:
    """`node` holds a `return` that is not a refusal, in its own scope."""
    return any(
        isinstance(n, ast.Return) and not rc._refuses([n])
        for n in [node, *_walk_local(node)]
    )


def _before_any_return(fn: _Fn) -> List[ast.stmt]:
    """The function's top-level statements up to the first that holds a
    `return` which is not a refusal: the only ones that run on every path that
    gets past them. Only these credit the function with a check."""
    out: List[ast.stmt] = []
    for st in fn.node.body:
        if _admits(st):
            break
        out.append(st)
    return out


def _stmt_call(st: ast.stmt) -> Optional[ast.Call]:
    """The call a statement is (`f(...)` or `await f(...)`), else None."""
    value = st.value if isinstance(st, ast.Expr) else None
    if isinstance(value, ast.Await):
        value = value.value
    return value if isinstance(value, ast.Call) else None


def _always_refuses(body: List[ast.stmt]) -> bool:
    """Every path through `body` refuses: it always ends (`_terminates`) and
    holds no `return` that is not a refusal."""
    return _terminates(body) and not any(_admits(st) for st in body)


def _gate_called(index: _Index, fn: _Fn, names: FrozenSet[str]) -> bool:
    """`fn` calls one of the gates `names` as a top-level statement before any
    `return`. A gate factory never counts here: calling one builds a
    dependency, and whether FastAPI resolves it depends on where it is
    declared, which the census class records for a route (`person_or_grant`,
    `admin_widened`)."""
    for st in _before_any_return(fn):
        call = _stmt_call(st)
        if call is None:
            continue
        ref = index.own_ref(fn, call.func)
        if _is_gate(ref, names) and ref is not None and ref[1] not in GATE_FACTORIES:
            return True
    return False


def _refuses_non_person(index: _Index, fn: _Fn) -> bool:
    """The function itself refuses on an allowlist, as a top-level statement
    before any `return`: an allowlist gate called, or `if not <person
    predicate>: <refuse>` (a direct call or a local bound from one) whose body
    refuses on every path."""
    if _gate_called(index, fn, ALLOWLIST_GATES):
        return True
    bound = _person_bound(index, fn)
    for st in _before_any_return(fn):
        if (
            isinstance(st, ast.If)
            and isinstance(st.test, ast.UnaryOp)
            and isinstance(st.test.op, ast.Not)
            and _always_refuses(st.body)
        ):
            inner = st.test.operand
            if _person_call(index, fn, inner) or (
                isinstance(inner, ast.Name) and inner.id in bound
            ):
                return True
    return False


def _calls_admin_gate(index: _Index, fn: _Fn) -> bool:
    return _gate_called(index, fn, ADMIN_GATES)


def _skip_preamble(body: List[ast.stmt]) -> List[ast.stmt]:
    """Drop a docstring and leading local imports."""
    out = list(body)
    if (
        out
        and isinstance(out[0], ast.Expr)
        and isinstance(getattr(out[0], "value", None), ast.Constant)
    ):
        out = out[1:]
    while out and isinstance(out[0], (ast.Import, ast.ImportFrom)):
        out = out[1:]
    return out


def _not_is_person(t: ast.AST, params: Set[str]) -> bool:
    """`not <param>.is_person`."""
    return (
        isinstance(t, ast.UnaryOp)
        and isinstance(t.op, ast.Not)
        and isinstance(t.operand, ast.Attribute)
        and t.operand.attr == "is_person"
        and isinstance(t.operand.value, ast.Name)
        and t.operand.value.id in params
    )


def _is_person_refusal(stmt: ast.stmt, params: Set[str]) -> bool:
    """`if not <param>.is_person: <refuse>`, refusing on every path."""
    return (
        isinstance(stmt, ast.If)
        and _always_refuses(stmt.body)
        and _not_is_person(stmt.test, params)
    )


def _person_helper_call(index: _Index, fn: _Fn, stmt: ast.stmt) -> bool:
    """`stmt` calls a helper whose own first statement refuses a non-person."""
    call = _stmt_call(stmt)
    if call is None:
        return False
    ref = index.own_ref(fn, call.func)
    helper = index.target(ref) if ref else None
    if helper is None:
        return False
    hbody = _skip_preamble(helper.node.body)
    return bool(hbody) and _is_person_refusal(hbody[0], rc._param_names(helper.node))


def _person_first(index: _Index, fn: _Fn) -> bool:
    """The handler refuses a non-person as its first statement, inline or via a
    helper whose own first statement is that refusal."""
    body = _skip_preamble(fn.node.body)
    if not body:
        return False
    first = body[0]
    return _is_person_refusal(first, rc._param_names(fn.node)) or _person_helper_call(
        index, fn, first
    )


def _person_on_an_arm(index: _Index, fn: _Fn) -> bool:
    """Somewhere in the handler's own scope, an arm refuses a non-person: an
    `if` whose test is `not <param>.is_person` or an `and` with it as a term,
    refusing on every path, or a call to a helper whose first statement is
    that refusal."""
    params = rc._param_names(fn.node)
    for n in _walk_local(fn.node):
        if isinstance(n, ast.If) and _always_refuses(n.body):
            terms = (
                n.test.values
                if isinstance(n.test, ast.BoolOp) and isinstance(n.test.op, ast.And)
                else [n.test]
            )
            if any(_not_is_person(t, params) for t in terms):
                return True
        if isinstance(n, ast.stmt) and _person_helper_call(index, fn, n):
            return True
    return False


def _returns(node: FnNode) -> List[Tuple[ast.Return, List[ast.expr]]]:
    """Every `return` in the function's own scope, each with the tests of the
    `if`s whose body holds it (an `else` branch or a `match` case contributes no
    test). Nested functions and classes are skipped."""
    out: List[Tuple[ast.Return, List[ast.expr]]] = []

    def visit(stmts: List[ast.stmt], guards: List[ast.expr]) -> None:
        for st in stmts:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(st, ast.Return):
                out.append((st, list(guards)))
                continue
            if isinstance(st, ast.If):
                visit(st.body, guards + [st.test])
                visit(st.orelse, guards)
                continue
            if isinstance(st, ast.Match):
                for case in st.cases:
                    visit(case.body, guards)
                continue
            for name in ("body", "orelse", "finalbody", "handlers"):
                inner = getattr(st, name, None)
                if not inner:
                    continue
                for part in inner:
                    if isinstance(part, ast.ExceptHandler):
                        visit(part.body, guards)
                    elif isinstance(part, ast.stmt):
                        visit([part], guards)

    visit(list(node.body), [])
    return out


def _admitting_exits(node: FnNode) -> List[Tuple[int, List[ast.expr]]]:
    """Every way the function can admit: each `return` that is not a refusal,
    and falling off its end. Each comes with the tests of the `if`s whose body
    holds it (an `else` branch contributes no test)."""
    out = [(r.lineno, guards) for r, guards in _returns(node) if not rc._refuses([r])]
    if not _terminates(list(node.body)):
        last = node.body[-1] if node.body else node
        out.append((getattr(last, "end_lineno", node.lineno) or node.lineno, []))
    return out


def _person_floor(index: _Index, fn: _Fn, bound: Set[str]) -> Optional[int]:
    """The last line of the first top-level statement that refuses every
    non-person (`if not <person term>: <refuse>`, or an allowlist gate called):
    every exit after it asks for a person. None when there is no such
    statement."""
    for st in fn.node.body:
        if (
            isinstance(st, ast.If)
            and isinstance(st.test, ast.UnaryOp)
            and isinstance(st.test.op, ast.Not)
            and _always_refuses(st.body)
            and _person_term(index, fn, st.test.operand, bound)
        ):
            return st.end_lineno or st.lineno
        call = _stmt_call(st)
        if call is not None:
            ref = index.own_ref(fn, call.func)
            if _is_gate(ref, ALLOWLIST_GATES) and ref is not None and ref[1] not in GATE_FACTORIES:
                return st.end_lineno or st.lineno
    return None


def _terminates(stmts: List[ast.stmt]) -> bool:
    """Does every path through `stmts` end in a `return` or a `raise`? An
    `if` needs both arms; a `try` needs its body (or `else`) and every handler,
    or a `finally` that does. Anything else is read as able to fall through: a
    loop, a `match`, and a `with`, whose context manager may suppress the
    refusal it ends on (`contextlib.suppress`)."""
    if not stmts:
        return False
    last = stmts[-1]
    if isinstance(last, (ast.Return, ast.Raise)):
        return True
    if isinstance(last, ast.If):
        return _terminates(last.body) and _terminates(last.orelse)
    if isinstance(last, ast.Try) or type(last).__name__ == "TryStar":
        if _terminates(getattr(last, "finalbody", [])):
            return True
        main = _terminates(last.body) or _terminates(last.orelse)
        return main and all(_terminates(h.body) for h in last.handlers)
    return False


# --------------------------------------------------------------------------- #
# Following a route
# --------------------------------------------------------------------------- #


def _entry_points(
    index: _Index, route: rc.Route, handler: _Fn
) -> List[Tuple[str, str]]:
    """The refs a route starts from besides its handler: its signature and
    decorator dependencies, and the `Annotated` aliases its parameters use."""
    node = handler.node
    exprs: List[ast.AST] = [d.args[0] for d in rc._signature_depends(node)]
    for dec, _methods, _ws in rc._route_decorators(node):
        for k in dec.keywords:
            if k.arg == "dependencies" and isinstance(k.value, (ast.List, ast.Tuple)):
                exprs.extend(
                    e.args[0]
                    for e in k.value.elts
                    if isinstance(e, ast.Call) and rc._is_depends(e)
                )
    refs: List[Tuple[str, str]] = []
    for e in exprs:
        refs.extend(index.refs(handler, e.func if isinstance(e, ast.Call) else e))
    args = node.args
    for a in args.posonlyargs + args.args + args.kwonlyargs:
        ann = a.annotation
        if ann is None:
            continue
        # An annotation is evaluated in the enclosing scope, so the handler's
        # own imports never rebind it; every reading is followed (union).
        readings = index.refs(handler, ann)
        at_module = index.module_ref(handler.rel, ann)
        if at_module is not None and at_module not in readings:
            readings.append(at_module)
        for r in readings:
            rel = rc._module_to_relpath(r[0], index.root)
            target = index.aliases.get(rel or "", {}).get(r[1])
            if target is not None and rel is not None:
                dep = target.func if isinstance(target, ast.Call) else target
                tr = index.module_ref(rel, dep)
                if tr:
                    refs.append(tr)
    return refs


class _Scanner:
    def __init__(self, root: Path):
        self.index = _Index(root)
        self._sites: Dict[str, List[Site]] = {}
        self._callees: Dict[str, List[Tuple[Tuple[str, str], bool]]] = {}

    def sites(self, fn: _Fn) -> List[Site]:
        if fn.key not in self._sites:
            if fn.module == GATE_MODULE and fn.node.name in _GATE_NAMES:
                self._sites[fn.key] = []
            else:
                self._sites[fn.key] = _sites_in(self.index, fn)
        return self._sites[fn.key]

    def callees(self, fn: _Fn) -> List[Tuple[Tuple[str, str], bool]]:
        """(ref, via_depends) for every call the function makes and every
        `Depends(x)` it declares, in its signature or body (FastAPI resolves a
        dependency's own dependencies too)."""
        if fn.key not in self._callees:
            refs: List[Tuple[Tuple[str, str], bool]] = []
            for n in ast.walk(fn.node):
                if not isinstance(n, ast.Call):
                    continue
                if rc._is_depends(n):
                    dep = n.args[0]
                    target = dep.func if isinstance(dep, ast.Call) else dep
                    refs.extend((r, True) for r in self.index.refs(fn, target))
                    continue
                refs.extend((r, False) for r in self.index.refs(fn, n.func))
            self._callees[fn.key] = refs
        return self._callees[fn.key]

    def gate_line(self, name: str) -> int:
        node = self.index.top.get(f"{GATE_MODULE}.py", {}).get(name)
        return getattr(node, "lineno", 0)

    def reach(
        self, route: rc.Route, handler: _Fn, blocked: FrozenSet[str] = frozenset()
    ) -> Tuple[List[_Fn], Set[str]]:
        """Every function the route runs (a gate-module function is entered but
        not followed further; a `blocked` one is not entered), and the denylist
        gates it declares as a dependency."""
        seen: Dict[str, _Fn] = {handler.key: handler}
        depends_denylist: Set[str] = set()
        queue: List[_Fn] = [handler]
        pending: List[Tuple[Tuple[str, str], bool]] = [
            (r, True) for r in _entry_points(self.index, route, handler)
        ]
        while queue or pending:
            if not pending:
                pending.extend(self.callees(queue.pop()))
                continue
            ref, via_depends = pending.pop()
            if ref[0] == GATE_MODULE and ref[1] in _GATE_NAMES:
                if via_depends and ref[1] in DENYLIST_GATES:
                    depends_denylist.add(ref[1])
                continue
            callee = self.index.target(ref)
            if callee is None or callee.key in seen or callee.key in blocked:
                continue
            seen[callee.key] = callee
            if callee.module != GATE_MODULE:
                queue.append(callee)
        return list(seen.values()), depends_denylist


def scan(
    root: Path = rc.BACKEND,
    baseline: Optional[Dict[str, str]] = None,
    *,
    tables: Optional[Dict[str, Dict[str, object]]] = None,
    not_grant_guards: Optional[Dict[str, Tuple[str, FrozenSet[str]]]] = None,
    reviewed_credits: Optional[Dict[str, Tuple[str, FrozenSet[str]]]] = None,
    workspace_uses: Optional[Dict[str, str]] = None,
    deferred: Optional[Dict[str, Tuple[str, FrozenSet[str]]]] = None,
    person_branches: Optional[Dict[str, Tuple[str, str]]] = None,
    door_fallbacks: Optional[Dict[str, str]] = None,
    workspace_door: str = WORKSPACE_DOOR,
    moderator_door: Optional[str] = ROOM_MODERATOR_DOOR,
) -> Report:
    """Run checks A, B and C over the tree at `root` (the real backend by
    default). `tables` passes census tables through to `rc.classify`, for
    planted trees; `moderator_door=None` skips check C (a tree with no rooms)."""
    not_grant_guards = (
        NOT_GRANT_GUARDS if not_grant_guards is None else not_grant_guards
    )
    reviewed_credits = (
        REVIEWED_CREDITS if reviewed_credits is None else reviewed_credits
    )
    workspace_uses = WORKSPACE_USES if workspace_uses is None else workspace_uses
    deferred = DEFERRED if deferred is None else deferred
    person_branches = PERSON_BRANCHES if person_branches is None else person_branches
    door_fallbacks = DOOR_FALLBACKS if door_fallbacks is None else door_fallbacks
    real_baseline = rc.load_baseline() if baseline is None else baseline
    census = rc.walk(root=root)
    classes, census_failures = rc.classify(census, real_baseline, **(tables or {}))
    report = Report(
        routes=len(census.routes),
        baseline=len(real_baseline),
        literal=rc.FROZEN_BASELINE_COUNT,
    )
    for tag, msg in census_failures:
        report.problems.append(f"census {tag}: {msg}")

    scanner = _Scanner(root)
    index = scanner.index
    index.person_branches = dict(person_branches)

    # Every agent-only site in the tree, for the count and the stale checks.
    all_sites: Dict[str, List[Site]] = {}
    for rel, quals in index.defs.items():
        for qual in quals:
            fn = index.fn(rel, qual)
            if fn is not None and scanner.sites(fn):
                all_sites[fn.key] = scanner.sites(fn)
    report.sites = sum(len(v) for v in all_sites.values())
    for key in sorted(not_grant_guards):
        if key not in all_sites:
            report.problems.append(
                f"stale NOT_GRANT_GUARDS entry {key}: no agent-only check there "
                "now — remove it"
            )

    # The Workspace writes (check B) and the tables that pin routes to a site.
    writes = {
        key
        for key, route in census.routes.items()
        if classes.get(key) == "portal" and set(route.methods) & WRITE_METHODS
    }
    pinned = (
        ("NOT_GRANT_GUARDS", not_grant_guards),
        ("REVIEWED_CREDITS", reviewed_credits),
        ("DEFERRED", {k: v for k, v in deferred.items() if k not in writes}),
    )
    pinned_used: Set[Tuple[str, str, str]] = set()  # (table, site, route)
    fallbacks_used: Set[str] = set()
    reached_sites: Set[str] = set()

    # A. Agent-only sites.
    door_routes = report.door_routes
    for key, route in sorted(census.routes.items()):
        handler = index.fn(route.relpath, route.qualname)
        if handler is None:
            report.problems.append(
                f"{key}: the census route has no definition the scan can read"
            )
            continue
        reached, depends_denylist = scanner.reach(route, handler)
        reached_sites.update(f.key for f in reached if scanner.sites(f))
        cls = classes.get(key)
        if moderator_door is not None and any(f.key == moderator_door for f in reached):
            door_routes.append(key)
        if cls in CLOSED_CLASSES:
            continue
        admin = cls in ADMIN_CLASSES or _calls_admin_gate(index, handler)
        candidates: List[Tuple[Site, Optional[_Fn]]] = [
            (site, fn) for fn in reached for site in scanner.sites(fn)
        ]
        for name in sorted(depends_denylist):
            gate_key = f"{GATE_MODULE}.py::{name}"
            gate = Site(gate_key, scanner.gate_line(name), f"Depends({name})")
            candidates.append((gate, None))
        for site, holder in candidates:
            if site.key == workspace_door:
                if cls == "portal":
                    continue  # check B reads the Workspace writes
                if door_fallbacks:
                    without, _ = scanner.reach(
                        route, handler, blocked=frozenset(door_fallbacks)
                    )
                    if all(f.key != workspace_door for f in without):
                        reached_keys = {f.key for f in reached}
                        fallbacks_used.update(set(door_fallbacks) & reached_keys)
                        continue
            if holder is not None and _refuses_non_person(index, holder):
                why = "the same function refuses a non-person"
                report.not_alone.append((key, site.key, why))
                continue
            if admin:
                report.not_alone.append((key, site.key, "admin-gated"))
                continue
            listed = [(t, e) for t, e in pinned if site.key in e]
            if listed and any(key in e[site.key][1] for _t, e in listed):
                for table, entries in listed:
                    if key not in entries[site.key][1]:
                        continue
                    pinned_used.add((table, site.key, key))
                    if table == "DEFERRED":
                        report.deferred.append((key, site.key))
                    elif table == "REVIEWED_CREDITS":
                        report.not_alone.append((key, site.key, "reviewed credit"))
                continue
            detail = f"{site.shape} is the only machine refusal on this route"
            if listed:
                detail += (
                    f"; {'/'.join(t for t, _e in listed)} lists this check "
                    "for other routes, not this one"
                )
            report.findings.append(
                Finding(AGENT_ONLY_SITE, key, route.methods, site.key, site.line, detail)
            )

    for table, entries in pinned:
        for site_key, (_reason, routes) in sorted(entries.items()):
            for route_key in sorted(routes):
                if (table, site_key, route_key) not in pinned_used:
                    report.problems.append(
                        f"stale {table} route {route_key} for {site_key}: it no "
                        "longer rests on that check alone (gone, person-gated or "
                        "credited another way) — remove it"
                    )
    listed_sites = {k for _t, e in pinned for k in e}
    for site_key in sorted(set(all_sites) - reached_sites - listed_sites):
        line = all_sites[site_key][0].line
        report.problems.append(
            f"{site_key}:{line}: an agent-only check no census route reaches — the "
            "scan cannot follow the call that runs it (a method, an object "
            "attribute), or only a WebSocket route or nothing calls it; make the "
            "call one the scan follows, or list it with its reason"
        )
    for fb in sorted(door_fallbacks):
        rel, _, qual = fb.partition("::")
        if index.fn(rel, qual) is None or fb not in fallbacks_used:
            report.problems.append(
                f"stale DOOR_FALLBACKS entry {fb}: no route reaches the Workspace "
                "door through it alone now — remove it"
            )
    for key, (cond, _why) in sorted(person_branches.items()):
        rel, _, qual = key.partition("::")
        helper = index.fn(rel, qual)
        if helper is None:
            report.absent_person_branches.append(key)
            continue
        if not any(
            _truthy_constant(r.value) and _guard_text(g) == cond
            for r, g in _returns(helper.node)
        ):
            report.problems.append(
                f"stale PERSON_BRANCHES entry {key}: it no longer answers a constant "
                f"under `{cond}` — remove or correct it"
            )

    # B. The Workspace write list.
    report.workspace_writes = len(writes)
    for key in sorted(writes):
        route = census.routes[key]
        handler = index.fn(route.relpath, route.qualname)
        person = handler is not None and _person_first(index, handler)
        for table, entries in (("WORKSPACE_USES", workspace_uses), ("DEFERRED", deferred)):
            if person and key in entries:
                report.problems.append(
                    f"redundant {table} entry {key}: it already refuses a "
                    "non-person — remove it"
                )
        if not person and key in deferred:
            if handler is not None and _person_on_an_arm(index, handler):
                report.problems.append(
                    f"stale DEFERRED entry {key}: an arm of it refuses a non-person "
                    "now — list the route in WORKSPACE_USES with the reason its "
                    "other arm is a use, and remove the entry"
                )
            else:
                report.deferred.append((key, key))
        elif not person and key not in workspace_uses:
            report.findings.append(
                Finding(
                    WORKSPACE_WRITE,
                    key,
                    route.methods,
                    key,
                    route.lineno,
                    f"behind {workspace_door.partition('::')[2]}, which admits a "
                    "non-person: it neither refuses one first nor is a listed use",
                )
            )
    for key in sorted(set(workspace_uses) - writes):
        report.problems.append(
            f"stale WORKSPACE_USES entry {key}: not a Workspace write — remove it"
        )

    # C. The room moderator door.
    if moderator_door is None:
        return report
    rel, _, qual = moderator_door.partition("::")
    door = index.fn(rel, qual)
    if door is None:
        report.problems.append(
            f"the room moderator door {moderator_door} is gone — point the scan "
            "at its successor"
        )
    else:
        exits = _admitting_exits(door.node)
        if not exits:
            report.problems.append(f"{moderator_door}: no admission branch found")
        if not door_routes:
            report.problems.append(
                f"{moderator_door}: no route reaches it — the scan would be vacuous"
            )
        bound = _person_bound(index, door)
        floor = _person_floor(index, door, bound)
        for line, guards in exits:
            if floor is not None and line > floor:
                continue
            if any(
                _person_term(index, door, t, bound)
                for test in guards
                for t in (
                    test.values
                    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And)
                    else [test]
                )
            ):
                continue
            shown = " and ".join(f"({ast.unparse(t)})" for t in guards) or "no condition"
            for key in door_routes:
                if classes.get(key) in CLOSED_CLASSES:
                    continue
                report.findings.append(
                    Finding(
                        ROOM_MODERATOR,
                        key,
                        census.routes[key].methods,
                        moderator_door,
                        line,
                        f"an admission under {shown} does not ask for a person",
                    )
                )
    return report


def format_report(report: Report) -> str:
    lines = [
        f"routes walked: {report.routes}; agent-only sites: {report.sites}",
        f"baseline entries: {report.baseline}; literal: {report.literal}",
        f"not alone (an allowlist or admin gate beside it): {len(report.not_alone)}",
        f"deferred (a grant ruled elsewhere, see DEFERRED): {len(report.deferred)}",
        f"Workspace writes read: {report.workspace_writes}; "
        f"routes behind the moderator door: {len(report.door_routes)}",
        "grant routes guarded by an agent-only check alone: "
        f"{len(report.routes_found)}",
    ]
    for check in (AGENT_ONLY_SITE, WORKSPACE_WRITE, ROOM_MODERATOR):
        rows = [f for f in report.findings if f.check == check]
        routes = sorted({f.route for f in rows})
        lines.append(f"  [{check}] {len(routes)} route(s)")
        for f in sorted(rows, key=lambda f: (f.route, f.site, f.line)):
            verbs = ",".join(f.methods)
            lines.append(f"    {verbs:6} {f.route}  <- {f.site}:{f.line}  {f.detail}")
    for route, key in sorted(report.deferred):
        lines.append(f"  [deferred] {route}  <- {key}")
    for key in report.absent_person_branches:
        lines.append(f"  [PERSON_BRANCHES, not in this tree] {key}")
    lines.append(f"table problems: {len(report.problems)}")
    lines.extend(f"  {p}" for p in report.problems)
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover - run by hand
    result = scan()
    print(format_report(result))
    sys.exit(1 if result.findings or result.problems else 0)
