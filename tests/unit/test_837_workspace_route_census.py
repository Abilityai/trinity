"""Which operator-side routes a Workspace-only member may reach (trinity-enterprise#837).

Two halves of one list, checked against each other:

* the backend: every route marked `@workspace_route` is in a frozen set, has a
  reason, is marked BELOW its route decorator (so the function FastAPI
  registers carries the mark), and authenticates through `get_current_user`
  (a mark anywhere else is dead);
* the frontend: every `/api/` call in the Workspace's own files reaches a
  Workspace door, a sign-in / public / own-auth route, or a marked route, and
  those files import no operator store.

No CI account holds the `user` role, so without this a Workspace feature that
calls an operator route would 403 for every seat holder while CI stays green.
Growing the marked set is a reviewed edit here; the direction is the other way
(the Workspace's remaining operator-side calls move behind its own doors).

Known limit: shared components outside the Workspace's own files
(`components/canvas/**`) are not scanned. A canvas path image fetches
`/api/agents/{name}/files/preview`, which is not part of the Workspace set,
and falls back.
"""
from __future__ import annotations

import ast
import importlib
import pathlib
import re

import pytest

pytestmark = pytest.mark.unit

REPO = pathlib.Path(__file__).resolve().parents[2]
BACKEND = REPO / "src" / "backend"
FRONTEND = REPO / "src" / "frontend" / "src"

MARKED = frozenset({
    "main.py::get_current_user_info",
    "routers/auth.py::logout",
    "routers/ws_tickets.py::create_ws_ticket",
    "routers/users.py::get_my_preferences",
    "routers/users.py::put_my_preference",
    "routers/users.py::delete_my_preference",
    "routers/loops.py::list_loops",
    "routers/loops.py::start_loop",
    "routers/loops.py::stop_loop",
    "routers/voice.py::get_voice_panel",
    "routers/public.py::get_shared_canvas",
})

ROUTE_VERBS = {"get", "post", "put", "patch", "delete", "api_route"}


def _is_mark(dec) -> bool:
    if not isinstance(dec, ast.Call):
        return False
    f = dec.func
    return (isinstance(f, ast.Name) and f.id == "workspace_route") or (
        isinstance(f, ast.Attribute) and f.attr == "workspace_route"
    )


def _marked_functions():
    out = {}
    for path in sorted(BACKEND.rglob("*.py")):
        rel = path.relative_to(BACKEND)
        if rel.parts[0] in {"enterprise", "tests"} or "__pycache__" in rel.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                _is_mark(d) for d in node.decorator_list
            ):
                out[f"{rel.as_posix()}::{node.name}"] = node
    return out


def _route_decorator_index(fn) -> int:
    for i, dec in enumerate(fn.decorator_list):
        if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and dec.func.attr in ROUTE_VERBS:
            return i
    return -1


class TestTheMarkedSet:
    def test_the_marked_set_is_exactly_the_reviewed_one(self):
        assert set(_marked_functions()) == MARKED

    def test_nothing_marks_a_route_any_other_way(self):
        """The census sees decorators. Every OTHER reference to the marker or its
        attribute — `workspace_route("x")(fn)`, `setattr(fn, WORKSPACE_ROUTE_ATTR,
        ...)`, an alias from `import ... as`, an `add_api_route` of a marked
        handler under a second path — would mark a route this set never lists.
        The only references allowed are the decorators above and the two in
        `dependencies.py` that define and read the mark."""
        names = {"workspace_route", "WORKSPACE_ROUTE_ATTR"}
        marked_names = {key.split("::")[1] for key in MARKED}
        stray = []
        for path in sorted(BACKEND.rglob("*.py")):
            rel = path.relative_to(BACKEND).as_posix()
            if rel.split("/")[0] in {"enterprise", "tests"} or "__pycache__" in rel:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            decorator_calls = {
                id(d.func) for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                for d in n.decorator_list if _is_mark(d)
            }
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    stray += [f"{rel}: import {a.name} as {a.asname}" for a in node.names
                              if a.name in names and a.asname]
                    continue
                ident = node.id if isinstance(node, ast.Name) else (
                    node.attr if isinstance(node, ast.Attribute) else None)
                if ident in names and id(node) not in decorator_calls and rel != "dependencies.py":
                    stray.append(f"{rel}:{node.lineno}: {ident}")
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "add_api_route"):
                    refs = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
                    stray += [f"{rel}:{node.lineno}: add_api_route({r})" for r in refs & marked_names]
        assert not stray, stray
        src = (BACKEND / "dependencies.py").read_text(encoding="utf-8")
        uses = [n for n in ast.walk(ast.parse(src))
                if isinstance(n, ast.Name) and n.id in names]
        # `WORKSPACE_ROUTE_ATTR` is defined, set by the decorator and read by the floor.
        assert sorted(n.id for n in uses) == ["WORKSPACE_ROUTE_ATTR", "WORKSPACE_ROUTE_ATTR", "WORKSPACE_ROUTE_ATTR"], [
            (n.id, n.lineno) for n in uses]

    def test_every_mark_has_a_reason_and_sits_below_its_route_decorator(self):
        for key, fn in _marked_functions().items():
            idx_mark = next(i for i, d in enumerate(fn.decorator_list) if _is_mark(d))
            mark = fn.decorator_list[idx_mark]
            assert mark.args and isinstance(mark.args[0], ast.Constant) and str(mark.args[0].value).strip(), key
            idx_route = _route_decorator_index(fn)
            assert 0 <= idx_route < idx_mark, f"{key}: @workspace_route must sit BELOW the route decorator"

    def test_every_marked_route_authenticates_through_get_current_user(self):
        """The floor lives in `get_current_user`; a mark on a route that never
        reaches it would admit nothing and mislead the next reader."""

        # `get_optional_user` / `get_user_or_anonymous` call `get_current_user`
        # in their bodies, so the floor runs for them too.
        doors = {"get_current_user", "get_optional_user", "get_user_or_anonymous"}

        def reaches(dependant) -> bool:
            for dep in dependant.dependencies:
                call = dep.call
                if getattr(call, "__name__", "") in doors and getattr(call, "__module__", "") == "dependencies":
                    return True
                if reaches(dep):
                    return True
            return False

        from fastapi.routing import APIRoute, APIRouter

        for key in sorted(MARKED):
            rel, name = key.split("::")
            if rel == "main.py":  # main is not imported in the unit island; its signature says it
                fn = _marked_functions()[key]
                sig = ast.unparse(fn.args)
                assert "Depends(get_current_user)" in sig, key
                continue
            module = importlib.import_module(rel[:-3].replace("/", "."))
            routes = [
                r for obj in vars(module).values() if isinstance(obj, APIRouter)
                for r in obj.routes if isinstance(r, APIRoute) and r.endpoint.__name__ == name
            ]
            assert routes, f"{key}: no registered route found"
            for route in routes:
                assert reaches(route.dependant), f"{key}: does not authenticate through get_current_user"


# --------------------------------------------------------------------------- #
# The frontend half
# --------------------------------------------------------------------------- #

def _workspace_files():
    files = [FRONTEND / "views" / "Portal.vue"]
    files += sorted((FRONTEND / "components" / "portal").rglob("*.vue"))
    files += sorted((FRONTEND / "components" / "portal").rglob("*.js"))
    files += [FRONTEND / "stores" / "clientPortal.js", FRONTEND / "stores" / "projects.js"]
    files += sorted((FRONTEND / "stores").glob("portal*.js"))
    return files


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    return re.sub(r"(?m)(^|[^:\"'`])//.*$", r"\1", text)


_API = re.compile(r"""[`'"](/api/[^`'"\s]*)""")
_DOORS = (
    "/api/enterprise/client-portal/",  # get_portal_principal
    "/api/enterprise/projects",        # Workspace projects, portal door
    "/api/rooms",                      # get_room_principal
)
_OTHER_AUTH = (
    "/api/files/",                     # token-gated downloads
    "/api/public/",                    # public links
    "/api/token", "/api/auth/mode", "/api/auth/email/",
)
# Calls the Workspace makes only for a principal the floor never applies to.
_KNOWN = {
    ("stores/clientPortal.js", "/api/settings/portal-session-policy"):
        "title-generation health, fetched only for an admin (shouldFetchTitleHealth); the route is assert_admin",
}
_ALLOWED_STORES = {
    "clientPortal", "projects", "portalDrafts", "portalWork", "portalRailFeeds", "portalLoops",
    "auth", "userPreferences", "theme",
}


def _normalise(path: str) -> str:
    path = path.split("?", 1)[0]
    path = re.sub(r"\$\{[^}]*\}", "{}", path)
    return re.sub(r"\{[^}/]*\}", "{}", path).rstrip("/")


def _marked_paths():
    """Normalised paths of the marked routes, read from their route decorators."""
    from fastapi.routing import APIRoute, APIRouter

    paths = set()
    for key in MARKED:
        rel, name = key.split("::")
        if rel == "main.py":
            fn = _marked_functions()[key]
            dec = fn.decorator_list[_route_decorator_index(fn)]
            paths.add(_normalise(dec.args[0].value))
            continue
        module = importlib.import_module(rel[:-3].replace("/", "."))
        for obj in vars(module).values():
            if isinstance(obj, APIRouter):
                for r in obj.routes:
                    if isinstance(r, APIRoute) and r.endpoint.__name__ == name:
                        paths.add(_normalise(r.path))
    return paths


class TestTheWorkspaceCalls:
    def test_the_scan_is_not_vacuous(self):
        files = _workspace_files()
        assert len(files) > 50
        calls = [m.group(1) for f in files for m in _API.finditer(_strip_comments(f.read_text()))]
        assert len(calls) > 40

    def test_every_workspace_call_reaches_a_door_or_a_marked_route(self):
        marked = _marked_paths()
        assert len(marked) >= len(MARKED) - 2  # a route registered on two verbs shares a path
        offenders = []
        for f in _workspace_files():
            rel = f.relative_to(FRONTEND).as_posix()
            for m in _API.finditer(_strip_comments(f.read_text())):
                raw = m.group(1)
                if raw in ("/api/", "/api"):
                    continue
                if raw.startswith(_DOORS) or raw.startswith(_OTHER_AUTH):
                    continue
                if (rel, raw.split("?", 1)[0]) in _KNOWN:
                    continue
                if _normalise(raw) in marked:
                    continue
                offenders.append(f"{rel}: {raw}")
        assert not offenders, (
            "The Workspace calls an operator route a Workspace-only member is refused on "
            "(trinity-enterprise#837). Serve it from a Workspace door, or mark the route "
            "@workspace_route with a reason and add it to MARKED:\n  " + "\n  ".join(offenders)
        )

    def test_workspace_files_import_no_operator_store(self):
        imp = re.compile(r"""import\s+[^;]*?from\s+['"](?:@/stores/|\.\.?/(?:\.\./)*stores/|\./)([A-Za-z]+)(?:\.js)?['"]""")
        offenders = []
        for f in _workspace_files():
            rel = f.relative_to(FRONTEND).as_posix()
            text = f.read_text()
            for m in imp.finditer(text):
                spec = m.group(0)
                if "stores/" not in spec and not rel.startswith("stores/"):
                    continue  # a relative component import, not a store
                name = m.group(1)
                if rel.startswith("stores/") and "stores/" not in spec and not (FRONTEND / "stores" / f"{name}.js").exists():
                    continue
                if name not in _ALLOWED_STORES:
                    offenders.append(f"{rel}: {name}")
        assert not offenders, (
            "A Workspace file imports an operator store; its calls 403 for a Workspace-only "
            "member (trinity-enterprise#837):\n  " + "\n  ".join(offenders)
        )
