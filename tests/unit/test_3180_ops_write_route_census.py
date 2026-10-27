"""#3180 — which routes an `ops_write` key reaches, proven against the LIVE app.

Three halves of one decision, checked against each other:

* the MARK (`@ops_write_route`): the marked set is a frozen literal; each mark
  is imported unaliased, has a reason and sits BELOW its route decorator; nothing
  references the mark or its attribute any other way; every marked handler
  resolves its caller through `get_current_user` (the fence lives there, so a
  mark anywhere else would be dead);
* the PATH LIST (`_OPS_WRITE_ALLOWED_ROUTES`): every entry resolves to exactly
  one live route, that route carries the mark, and every marked handler has an
  entry. The mark is the independent declaration that lets this catch an
  overbroad regex at all — a sweep driven by the regex itself cannot;
* the SWEEP (`_ops_write_sweep.py`): the real `main.app`, real minted keys, the
  real resolver, every registered OSS route, classified by what its credential
  dependency does. Unlisted routes refuse; listed routes are not refused by the
  fence; with the path list holed the mark still refuses every unmarked handler;
  with every handler marked the path list still refuses; a demoted owner's key
  is refused everywhere, a suspended owner's key gets 401 everywhere; and with
  the fence call removed the named grant families show which of them have a
  gate of their own and which have only the fence.

OSS routes only: the enterprise submodule is not mounted in this tree, so its
routes are neither swept nor claimed.
"""
from __future__ import annotations

import ast
import importlib
import inspect
import pathlib
import re
import sys

import pytest

pytestmark = pytest.mark.unit

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
BACKEND = REPO / "src" / "backend"
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import _ops_write_sweep as sweep  # noqa: E402

MARKED = frozenset({
    "routers/agents.py::reset_circuit_breaker_endpoint",
    "routers/agents.py::start_agent_endpoint",
    "routers/agents.py::stop_agent_endpoint",
    "routers/chat.py::terminate_agent_execution",
})

# The live (METHOD, template) of each marked handler, pinned so a move is a
# reviewed edit here and in `_OPS_WRITE_ALLOWED_ROUTES` together.
LISTED = {
    ("POST", "/api/agents/{agent_name}/circuit-breaker/reset"): "routers/agents.py::reset_circuit_breaker_endpoint",
    ("POST", "/api/agents/{agent_name}/start"): "routers/agents.py::start_agent_endpoint",
    ("POST", "/api/agents/{agent_name}/stop"): "routers/agents.py::stop_agent_endpoint",
    ("POST", "/api/agents/{name}/executions/{execution_id}/terminate"): "routers/chat.py::terminate_agent_execution",
}

ROUTE_VERBS = {"get", "post", "put", "patch", "delete", "api_route"}
NAMES = {"ops_write_route", "OPS_WRITE_ROUTE_ATTR"}


def _deps():
    import dependencies
    return dependencies


# --------------------------------------------------------------------------- #
# The mark
# --------------------------------------------------------------------------- #

def _is_mark(dec) -> bool:
    if not isinstance(dec, ast.Call):
        return False
    f = dec.func
    return (isinstance(f, ast.Name) and f.id == "ops_write_route") or (
        isinstance(f, ast.Attribute) and f.attr == "ops_write_route")


def _backend_files():
    for path in sorted(BACKEND.rglob("*.py")):
        rel = path.relative_to(BACKEND)
        if rel.parts[0] in {"enterprise", "tests"} or "__pycache__" in rel.parts:
            continue
        yield rel.as_posix(), ast.parse(path.read_text(encoding="utf-8"))


def _marked_functions():
    out = {}
    for rel, tree in _backend_files():
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                    _is_mark(d) for d in node.decorator_list):
                out[f"{rel}::{node.name}"] = node
    return out


class TestTheMark:
    def test_the_marked_set_is_exactly_the_reviewed_one(self):
        assert set(_marked_functions()) == MARKED

    def test_every_mark_has_a_reason_and_sits_below_its_route_decorator(self):
        for key, fn in _marked_functions().items():
            idx_mark = next(i for i, d in enumerate(fn.decorator_list) if _is_mark(d))
            mark = fn.decorator_list[idx_mark]
            reason = mark.args[0] if mark.args else None
            text = None
            if isinstance(reason, ast.Constant):
                text = reason.value
            elif isinstance(reason, ast.JoinedStr) or isinstance(reason, ast.BinOp):
                text = ast.unparse(reason)
            assert text and str(text).strip(), f"{key}: @ops_write_route needs a reason"
            idx_route = next((i for i, d in enumerate(fn.decorator_list)
                              if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                              and d.func.attr in ROUTE_VERBS), -1)
            assert 0 <= idx_route < idx_mark, f"{key}: @ops_write_route must sit BELOW the route decorator"

    def test_nothing_marks_a_handler_any_other_way(self):
        """Every reference to the mark or its attribute outside the decorators —
        `ops_write_route("x")(fn)`, `setattr(fn, OPS_WRITE_ROUTE_ATTR, ...)`, an
        aliased import, an `add_api_route` of a marked handler under a second
        path — would admit a route this census never lists."""
        marked_names = {k.split("::")[1] for k in MARKED}
        stray = []
        for rel, tree in _backend_files():
            decorator_calls = {
                id(d.func) for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                for d in n.decorator_list if _is_mark(d)
            }
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    stray += [f"{rel}: import {a.name} as {a.asname}" for a in node.names
                              if a.name in NAMES and a.asname]
                    continue
                ident = node.id if isinstance(node, ast.Name) else (
                    node.attr if isinstance(node, ast.Attribute) else None)
                if ident in NAMES and id(node) not in decorator_calls and rel != "dependencies.py":
                    stray.append(f"{rel}:{node.lineno}: {ident}")
                if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "add_api_route":
                    refs = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
                    stray += [f"{rel}:{node.lineno}: add_api_route({r})" for r in refs & marked_names]
                # The attribute's literal value set by name, bypassing the constant.
                if isinstance(node, ast.Constant) and node.value == "__trinity_ops_write_route__" \
                        and rel != "dependencies.py":
                    stray.append(f"{rel}:{node.lineno}: literal attribute name")
        assert not stray, stray
        tree = ast.parse((BACKEND / "dependencies.py").read_text(encoding="utf-8"))
        uses = sorted((n.id, n.lineno) for n in ast.walk(tree)
                      if isinstance(n, ast.Name) and n.id == "OPS_WRITE_ROUTE_ATTR")
        # Defined once, set once by the decorator, read once by the fence.
        assert len(uses) == 3, uses
        literal = [n.lineno for n in ast.walk(tree)
                   if isinstance(n, ast.Constant) and n.value == "__trinity_ops_write_route__"]
        assert len(literal) == 1, literal

    def test_the_mark_imports_are_unaliased_and_from_dependencies(self):
        for key in MARKED:
            rel = key.split("::")[0]
            tree = ast.parse((BACKEND / rel).read_text(encoding="utf-8"))
            imports = [a for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module == "dependencies"
                       for a in n.names if a.name == "ops_write_route"]
            assert len(imports) == 1 and imports[0].asname is None, (key, imports)

    def test_the_mark_refuses_an_empty_reason(self):
        d = _deps()
        for bad in ("", "   ", None, 3):
            with pytest.raises(ValueError):
                d.ops_write_route(bad)

    def test_every_marked_handler_resolves_its_caller_through_get_current_user(self):
        """The fence lives in the resolver behind `get_current_user`; a mark on a
        route that never reaches it admits nothing and misleads the reader."""
        from fastapi.routing import APIRoute, APIRouter

        def reaches(dependant) -> bool:
            for dep in dependant.dependencies:
                call = dep.call
                if getattr(call, "__name__", "") == "get_current_user" and \
                        getattr(call, "__module__", "") == "dependencies":
                    return True
                if reaches(dep):
                    return True
            return False

        for key in sorted(MARKED):
            rel, name = key.split("::")
            module = importlib.import_module(rel[:-3].replace("/", "."))
            routes = [r for obj in vars(module).values() if isinstance(obj, APIRouter)
                      for r in obj.routes if isinstance(r, APIRoute) and r.endpoint.__name__ == name]
            assert len(routes) == 1, f"{key}: expected one registered route, found {len(routes)}"
            assert reaches(routes[0].dependant), f"{key}: does not authenticate through get_current_user"
            d = _deps()
            assert getattr(routes[0].endpoint, d.OPS_WRITE_ROUTE_ATTR, None), f"{key}: registered handler unmarked"


# --------------------------------------------------------------------------- #
# The path list
# --------------------------------------------------------------------------- #

class TestThePathList:
    def test_every_entry_is_an_anchored_write(self):
        d = _deps()
        assert len(d._OPS_WRITE_ALLOWED_ROUTES) == len(LISTED)
        for method, pattern in d._OPS_WRITE_ALLOWED_ROUTES:
            assert method in {"POST", "PUT", "PATCH", "DELETE"}, (method, pattern.pattern)
            assert pattern.pattern.startswith("^") and pattern.pattern.endswith("$"), pattern.pattern
            assert ".*" not in pattern.pattern and ".+" not in pattern.pattern, pattern.pattern

    def test_the_write_list_is_disjoint_from_the_read_list(self):
        """D2: `ops_write` does NOT carry the `ops` reads, and the `ops` list is
        untouched — its every-entry-is-a-GET guard keeps its meaning."""
        d = _deps()
        assert all(m == "GET" for m, _ in d._OPS_ALLOWED_ROUTES)
        assert len(d._OPS_ALLOWED_ROUTES) == 19
        assert d._OPS_ALLOWED_ROUTES is not d._OPS_WRITE_ALLOWED_ROUTES
        assert not ({p.pattern for _, p in d._OPS_ALLOWED_ROUTES}
                    & {p.pattern for _, p in d._OPS_WRITE_ALLOWED_ROUTES})


# --------------------------------------------------------------------------- #
# The fence and the gate must agree (the test_2323 pair, mirrored)
# --------------------------------------------------------------------------- #

_OPS_WRITE_OPT_IN = re.compile(r"""['"]ops_write['"]""")
_ASSERT_ADMIN_CALL = re.compile(r"assert_admin\(([^)]*)\)", re.S)
_ALLOWING_CALL = re.compile(r"require_admin_allowing\(([^)]*)\)", re.S)
_BARE_REQUIRE_ADMIN = re.compile(r"\brequire_admin\b(?!_allowing)")


def _code_only(src: str) -> str:
    """The handler source with comments and docstrings removed, via the AST —
    the reset handler's own comment explains `require_admin` in prose."""
    tree = ast.parse(inspect.cleandoc("\n" + src) if src.startswith(" ") else src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.body and \
                isinstance(node.body[0], ast.Expr) and isinstance(getattr(node.body[0], "value", None), ast.Constant):
            node.body = node.body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def _ops_write_gate_offences(src: str) -> list:
    offences = []
    for m in _ASSERT_ADMIN_CALL.finditer(src):
        if not _OPS_WRITE_OPT_IN.search(m.group(1)):
            offences.append("assert_admin() without allow_scopes={'ops_write'}")
    for m in _ALLOWING_CALL.finditer(src):
        if not _OPS_WRITE_OPT_IN.search(m.group(1)):
            offences.append("require_admin_allowing() without 'ops_write'")
    if _BARE_REQUIRE_ADMIN.search(src):
        offences.append("Depends(require_admin): no opt-in exists; use require_admin_allowing('ops_write')")
    return offences


def _admitted_handler_sources():
    from fastapi.routing import APIRoute, APIRouter

    d = _deps()
    out = []
    for module_name in ("routers.agents", "routers.chat"):
        module = importlib.import_module(module_name)
        for obj in vars(module).values():
            if not isinstance(obj, APIRouter):
                continue
            for r in obj.routes:
                if not isinstance(r, APIRoute):
                    continue
                concrete = re.sub(r"\{[^}]+\}", "x", r.path)
                if any(m in (r.methods or set()) and p.fullmatch(concrete) for m, p in d._OPS_WRITE_ALLOWED_ROUTES):
                    out.append((r.path, _code_only(inspect.getsource(r.endpoint))))
    return out


class TestTheFenceAndTheGateAgree:
    def test_every_admin_gated_admitted_route_opts_ops_write_in(self):
        pairs = _admitted_handler_sources()
        assert len(pairs) == len(LISTED), pairs
        offenders = {p: _ops_write_gate_offences(s) for p, s in pairs if _ops_write_gate_offences(s)}
        assert not offenders, offenders
        # And the one admin-gated route really is gated, opted in, by the
        # Depends form — so the check above is not satisfied by having no gate.
        reset = next(s for p, s in pairs if p.endswith("/circuit-breaker/reset"))
        assert "require_admin_allowing('ops_write')" in reset

    def test_the_gate_guard_detects_every_spelling(self):
        assert _ops_write_gate_offences("assert_admin(current_user)")
        assert _ops_write_gate_offences('assert_admin(current_user, allow_scopes={"ops"})')
        assert _ops_write_gate_offences("user: User = Depends(require_admin)")
        assert _ops_write_gate_offences('Depends(require_admin_allowing("ops"))')
        assert not _ops_write_gate_offences('assert_admin(current_user, allow_scopes={"ops_write"})')
        assert not _ops_write_gate_offences("Depends(require_admin_allowing('ops_write'))")
        assert not _ops_write_gate_offences("user: User = Depends(get_current_user)")

    def test_the_opt_in_is_per_route(self):
        """`require_admin` (bare) and `allow_scopes={"ops"}` still refuse the
        write scope, with a real principal (never a stand-in)."""
        from fastapi import HTTPException
        from models import User

        d = _deps()
        p = User(id=7, username="ow-gate", email="ow-gate@example.com", role="admin",
                 mcp_scope="ops_write", mcp_key_id="k-gate", mcp_key_name="gate")
        with pytest.raises(HTTPException):
            d.require_admin(p)
        with pytest.raises(HTTPException):
            d.assert_admin(p, allow_scopes=frozenset({"ops"}))
        d.assert_admin(p, allow_scopes=frozenset({"ops_write"}))
        with pytest.raises(HTTPException):
            d.assert_admin(User(id=7, username="ow-gate", email="ow-gate@example.com", role="creator",
                                mcp_scope="ops_write"), allow_scopes=frozenset({"ops_write"}))


# --------------------------------------------------------------------------- #
# The sweep over the live app
# --------------------------------------------------------------------------- #

# Routes with no Trinity-credential dependency, each by name with what they use
# instead. A new route that reads no Trinity credential fails until it is listed
# here with its reason; a route that gains one fails as stale.
UNAUTHENTICATED = "public by design: no credential"
OTHER_CREDENTIAL = "authenticates with something other than a Trinity key"
OWN_VALIDATOR = "validates a Trinity key itself; asserted in test_doors_that_validate_a_key_themselves"
NO_TRINITY_CREDENTIAL = {
    "routers/auth.py::get_auth_mode": UNAUTHENTICATED,
    "routers/auth.py::login": OTHER_CREDENTIAL + " (password grant)",
    "routers/auth.py::login_api": OTHER_CREDENTIAL + " (password grant)",
    "routers/auth.py::validate_token": OTHER_CREDENTIAL + " (a JWT only; an MCP key fails jwt.decode)",
    "routers/auth.py::request_email_login_code": UNAUTHENTICATED,
    "routers/auth.py::verify_email_login_code": OTHER_CREDENTIAL + " (emailed code)",
    "routers/auth.py::request_access": UNAUTHENTICATED,
    "routers/agents.py::agent_heartbeat": OWN_VALIDATOR,
    "routers/agents.py::agent_execution_result": OWN_VALIDATOR,
    "routers/mcp_keys.py::validate_mcp_api_key_http_endpoint": OWN_VALIDATOR + " (introspection: grants nothing)",
    "routers/public.py::tls_allowed": UNAUTHENTICATED,
    "routers/public.py::get_public_link_info": OTHER_CREDENTIAL + " (public link token)",
    "routers/public.py::get_public_playbooks": OTHER_CREDENTIAL + " (public link token)",
    "routers/public.py::request_verification_code": UNAUTHENTICATED,
    "routers/public.py::confirm_verification_code": OTHER_CREDENTIAL + " (emailed code)",
    "routers/public.py::public_chat": OTHER_CREDENTIAL + " (public link token)",
    "routers/public.py::get_agent_intro": OTHER_CREDENTIAL + " (public link token)",
    "routers/public.py::get_public_chat_history": OTHER_CREDENTIAL + " (public link token)",
    "routers/public.py::clear_public_session": OTHER_CREDENTIAL + " (public link token)",
    "routers/public.py::public_stream_execution": OTHER_CREDENTIAL + " (public link token)",
    "routers/public.py::public_execution_status": OTHER_CREDENTIAL + " (public link token)",
    "routers/public.py::public_terminate_execution": OTHER_CREDENTIAL + " (public link token)",
    "routers/files.py::download_shared_file": OTHER_CREDENTIAL + " (signed file id)",
    "routers/files.py::head_shared_file": OTHER_CREDENTIAL + " (signed file id)",
    "routers/setup.py::get_setup_status": UNAUTHENTICATED,
    "routers/setup.py::set_admin_password": UNAUTHENTICATED + " (first-run only)",
    "routers/internal.py::internal_agent_pre_check": OTHER_CREDENTIAL + " (X-Internal-Secret)",
    "routers/internal.py::internal_agent_brief_readiness": OTHER_CREDENTIAL + " (X-Internal-Secret)",
    "routers/internal.py::execute_task_internal": OTHER_CREDENTIAL + " (X-Internal-Secret)",
    "routers/internal.py::validate_execution": OTHER_CREDENTIAL + " (X-Internal-Secret)",
    "routers/internal.py::log_audit_entry": OTHER_CREDENTIAL + " (X-Internal-Secret)",
    "routers/internal.py::mcp_exposed_agents": OTHER_CREDENTIAL + " (X-Internal-Secret)",
    "routers/internal.py::internal_next_task": OWN_VALIDATOR + " (pull seam: internal secret or the agent's own key)",
    "routers/internal.py::internal_task_result": OWN_VALIDATOR + " (pull seam: internal secret or the agent's own key)",
    "routers/mcp_auth.py::request_inline_login": OTHER_CREDENTIAL + " (X-Internal-Secret)",
    "routers/mcp_auth.py::verify_inline_login": OTHER_CREDENTIAL + " (X-Internal-Secret)",
    "routers/mcp_auth.py::inline_playbooks": OTHER_CREDENTIAL + " (X-Internal-Secret)",
    "routers/mcp_auth.py::inline_chat": OTHER_CREDENTIAL + " (X-Internal-Secret)",
    "routers/slack.py::handle_slack_event": OTHER_CREDENTIAL + " (Slack request signature)",
    "routers/slack.py::slack_oauth_callback": OTHER_CREDENTIAL + " (OAuth state)",
    "routers/telegram.py::handle_telegram_webhook": OTHER_CREDENTIAL + " (webhook secret)",
    "routers/whatsapp.py::handle_twilio_webhook": OTHER_CREDENTIAL + " (webhook secret)",
    "routers/paid.py::get_paid_agent_info": UNAUTHENTICATED + " (price card)",
    "routers/paid.py::paid_chat": OTHER_CREDENTIAL + " (x402 payment)",
    "routers/avatar.py::get_avatar": UNAUTHENTICATED + " (public asset)",
    "routers/avatar.py::get_avatar_reference": UNAUTHENTICATED + " (public asset)",
    "routers/avatar.py::get_avatar_emotions": UNAUTHENTICATED + " (public asset)",
    "routers/avatar.py::get_avatar_emotion": UNAUTHENTICATED + " (public asset)",
    "routers/a2a.py::a2a_well_known_card": UNAUTHENTICATED + " (published agent card)",
    "routers/aauth.py::aauth_agent_metadata": UNAUTHENTICATED + " (well-known metadata)",
    "routers/aauth.py::aauth_jwks": UNAUTHENTICATED + " (well-known metadata)",
    "routers/aauth.py::aauth_resource_metadata": UNAUTHENTICATED + " (well-known metadata)",
    "routers/webhooks.py::trigger_webhook": OTHER_CREDENTIAL + " (webhook token)",
    "client_portal/router.py::portal_auth_request": UNAUTHENTICATED,
    "client_portal/router.py::portal_auth_verify": OTHER_CREDENTIAL + " (emailed code)",
    "main.py::health_check": UNAUTHENTICATED,
}

# Never-grantable families, through their real routes with a real agent and
# real key ids. `own_gate` names the gate that still refuses with the fence call
# removed; None means the fence is the only layer there (plan "not delivered").
FAMILIES = [
    {"key": "routers/mcp_keys.py::create_mcp_api_key_endpoint", "method": "POST",
     "json": {"name": "x", "scope": "ops_write"}, "own_gate": "interactive"},
    {"key": "routers/mcp_keys.py::list_mcp_api_keys_endpoint", "method": "GET", "own_gate": "interactive"},
    {"key": "routers/mcp_keys.py::ensure_default_mcp_api_key", "method": "POST", "own_gate": "interactive"},
    {"key": "routers/mcp_keys.py::revoke_mcp_api_key_endpoint", "method": "POST", "sacrifice": "revoke",
     "own_gate": None},
    {"key": "routers/mcp_keys.py::delete_mcp_api_key_endpoint", "method": "DELETE", "sacrifice": "delete",
     "own_gate": None},
    {"key": "routers/users.py::update_user_role", "method": "PUT", "json": {"role": "admin"}, "own_gate": "admin"},
    {"key": "routers/settings/whitelist.py::add_email_to_whitelist", "method": "POST",
     "json": {"email": "someone@example.com"}, "own_gate": "interactive"},
    {"key": "routers/settings/whitelist.py::remove_email_from_whitelist", "method": "DELETE", "own_gate": "admin"},
    {"key": "routers/credentials.py::inject_credentials", "method": "POST",
     "json": {"files": {".env": "A=1\n"}}, "own_gate": None},
    {"key": "routers/credentials.py::export_credentials", "method": "POST", "own_gate": None},
    {"key": "routers/credentials.py::import_credentials", "method": "POST", "own_gate": None},
    {"key": "routers/subscriptions.py::register_subscription", "method": "POST",
     "json": {"name": "sweep-sub", "token": "sk-ant-oat01-sweep"}, "own_gate": "admin"},
    {"key": "routers/subscriptions.py::assign_subscription_to_agent", "method": "PUT",
     "query": "subscription_name=sweep-sub", "own_gate": None},
    {"key": "routers/subscriptions.py::delete_subscription", "method": "DELETE", "own_gate": "admin"},
    {"key": "routers/agent_config.py::set_agent_capability_grant", "method": "PUT",
     "json": {"granted": True}, "own_gate": "admin"},
    {"key": "routers/skills.py::set_skill_manager", "method": "PUT", "json": {"enabled": True}, "own_gate": "admin"},
    {"key": "routers/agent_config.py::set_agent_autonomy_status", "method": "PUT",
     "json": {"enabled": True}, "own_gate": "person"},
    {"key": "routers/users.py::change_my_password", "method": "PUT",
     "json": {"current_password": "a", "new_password": "b"}, "own_gate": "interactive"},
    {"key": "routers/users.py::set_my_github_pat", "method": "PUT", "json": {"pat": "x"}, "own_gate": "interactive"},
    {"key": "routers/ws_tickets.py::create_ws_ticket", "method": "POST", "own_gate": "person"},
]

GATE_DETAILS = {
    "interactive": "This operation requires an interactive session; MCP API keys cannot perform it",
    "admin": "This key scope cannot satisfy an admin gate",
}

# Candidates the plan excluded (D3), and the `ops` reads (D2), by live route.
EXCLUDED_CANDIDATES = [
    ("POST", "/api/ops/fleet/restart"), ("POST", "/api/ops/fleet/stop"), ("POST", "/api/ops/emergency-stop"),
    ("POST", "/api/ops/schedules/pause"), ("POST", "/api/ops/schedules/resume"),
    ("POST", "/api/ops/alerts/{alert_id}/acknowledge"), ("POST", "/api/settings/retention/acknowledge"),
    ("POST", "/api/system-agent/restart"),
    ("POST", "/api/agents/{name}/schedules/{schedule_id}/enable"),
    ("POST", "/api/agents/{name}/schedules/{schedule_id}/disable"),
    ("PUT", "/api/agents/{name}/schedules/{schedule_id}"),
    ("POST", "/api/agents/{name}/schedules/{schedule_id}/trigger"),
    ("POST", "/api/notifications/{notification_id}/acknowledge"),
    ("POST", "/api/notifications/{notification_id}/dismiss"),
    ("POST", "/api/monitoring/agents/{agent_name}/check"), ("POST", "/api/monitoring/enable"),
    ("POST", "/api/monitoring/disable"), ("POST", "/api/monitoring/cleanup-trigger"),
    ("POST", "/api/agents/{agent_name}/queue/clear"), ("POST", "/api/agents/{agent_name}/queue/release"),
    ("PUT", "/api/agents/{agent_name}/circuit-breaker"),
]


@pytest.fixture(scope="module")
def live():
    return sweep.run(FAMILIES)


def _rows(live, cls=None):
    return [r for r in live["routes"] if cls is None or r["cls"] == cls]


def _k(row):
    return f"{row['method']} {row['template']}"


def _fenced(live):
    return [r for r in live["routes"] if r["cls"] in ("resolver", "or_anonymous")]


def _listed(row):
    return (row["method"], row["template"]) in LISTED


class TestTheLiveApp:
    def test_the_sweep_is_not_vacuous(self, live):
        assert len(live["routes"]) > 600
        fenced = _fenced(live)
        assert len(fenced) > 600
        assert {(r["method"], r["template"]) for r in fenced} >= set(LISTED)
        assert any(r["cls"] == "or_anonymous" for r in live["routes"]), "the A2A door vanished"
        assert any(r["cls"] == "swallowing" for r in live["routes"]), "the get_optional_user door vanished"

    def test_every_fence_entry_resolves_to_exactly_one_live_marked_route(self, live):
        d = _deps()
        rows = live["routes"]
        for method, pattern in d._OPS_WRITE_ALLOWED_ROUTES:
            hits = [r for r in rows if r["method"] == method
                    and pattern.fullmatch(re.sub(r"\{[^}]+\}", "x", r["template"]))]
            assert len(hits) == 1, (pattern.pattern, [_k(h) for h in hits])
            assert hits[0]["key"] in MARKED, hits[0]["key"]
            assert LISTED[(hits[0]["method"], hits[0]["template"])] == hits[0]["key"]
        assert set(live["marked_after_restore"]) == MARKED, "the live app's marked handlers drifted"

    def test_routes_with_no_trinity_credential_are_exactly_the_listed_ones(self, live):
        none = {r["key"] for r in _rows(live, "none")}
        assert none - set(NO_TRINITY_CREDENTIAL) == set(), (
            "a route reads no Trinity credential and is not listed — list it with its reason, or gate it: "
            f"{sorted(none - set(NO_TRINITY_CREDENTIAL))}")
        assert set(NO_TRINITY_CREDENTIAL) - none == set(), (
            f"stale: these now read a Trinity credential: {sorted(set(NO_TRINITY_CREDENTIAL) - none)}")

    def test_every_unlisted_route_refuses_and_the_four_do_not(self, live):
        bad = []
        for row in _fenced(live):
            got = live["normal"][_k(row)]
            if _listed(row):
                # Reached the route's own access check (the swept agent does not
                # exist), so the fence admitted it; the pass paths run elsewhere.
                if got["code"] or got["status"] != 404:
                    bad.append((_k(row), got["status"], got["code"]))
            elif not (got["status"] == 403 and got["code"] == "ops_write_route_not_listed"):
                bad.append((_k(row), got["status"], got["code"], str(got["detail"])[:120]))
        assert not bad, bad

    def test_a2a_answers_403_not_its_anonymous_path(self, live):
        rows = _rows(live, "or_anonymous")
        assert [r["key"] for r in rows] == ["routers/a2a.py::a2a_jsonrpc"]
        got = live["normal"][_k(rows[0])]
        assert got["status"] == 403 and got["code"] == "ops_write_route_not_listed", got

    def test_a_swallowing_door_treats_the_key_as_an_unrecognised_one(self, live):
        rows = _rows(live, "swallowing")
        assert rows
        for row in rows:
            got = live["swallowing"][_k(row)]
            assert (got["status"], got["body"]) == (got["bogus"]["status"], got["bogus"]["body"]), (_k(row), got)

    def test_the_excluded_candidates_and_the_ops_reads_refuse(self, live):
        d = _deps()
        normal = live["normal"]
        for method, template in EXCLUDED_CANDIDATES:
            got = normal[f"{method} {template}"]
            assert got["status"] == 403 and got["code"] == "ops_write_route_not_listed", (method, template, got)
        reads = [r for r in _fenced(live) if r["method"] == "GET" and any(
            p.fullmatch(re.sub(r"\{[^}]+\}", "x", r["template"])) for _, p in d._OPS_ALLOWED_ROUTES)]
        assert len(reads) >= 15, [_k(r) for r in reads]
        for row in reads:
            got = normal[_k(row)]
            assert got["code"] == "ops_write_route_not_listed", (_k(row), got)

    def test_with_the_path_list_holed_every_unmarked_handler_still_refuses(self, live):
        bad = []
        for row in _fenced(live):
            got = live["hole_path"][_k(row)]
            if _listed(row):
                if got["code"]:
                    bad.append((_k(row), got["code"]))
            elif got["code"] != "ops_write_route_not_marked":
                bad.append((_k(row), got["status"], got["code"]))
        assert not bad, bad

    def test_with_every_handler_marked_the_path_list_still_refuses(self, live):
        bad = []
        for row in _fenced(live):
            got = live["stray_mark"][_k(row)]
            if _listed(row):
                if got["code"]:
                    bad.append((_k(row), got["code"]))
            elif got["code"] != "ops_write_route_not_listed":
                bad.append((_k(row), got["status"], got["code"]))
        assert not bad, bad

    @pytest.mark.parametrize("role", ["creator", "operator"])
    def test_a_demoted_owners_key_is_refused_on_every_route(self, live, role):
        res = live[f"demoted_{role}"]
        bad = [(_k(r), res[_k(r)]["status"], res[_k(r)]["code"]) for r in _fenced(live)
               if res[_k(r)]["code"] != "ops_write_owner_not_admin"]
        assert not bad, bad

    def test_a_suspended_owners_key_is_refused_on_every_route(self, live):
        res = live["suspended"]
        bad = [(_k(r), res[_k(r)]["status"]) for r in _fenced(live) if res[_k(r)]["status"] != 401]
        assert not bad, bad

    def test_the_read_only_ops_key_still_reaches_no_write(self, live):
        res = live["ops_key_writes"]
        assert len(res) > 300
        assert {_k(r) for r in _fenced(live) if _listed(r)} <= set(res)
        bad = {k: v["status"] for k, v in res.items()
               if not (v["status"] == 403 and isinstance(v["detail"], str) and v["detail"].startswith("Ops keys"))}
        assert not bad, bad


class TestNeverGrantableFamilies:
    def test_every_family_resolved_to_a_live_route(self, live):
        missing = [k for k, v in live["families"].items() if isinstance(v, dict) and v.get("missing")]
        assert not missing, missing
        assert len([k for k in live["families"] if not k.startswith("_")]) == len(FAMILIES)

    def test_every_family_is_refused_by_the_fence(self, live):
        bad = {k: (v["status"], v["code"]) for k, v in live["families"].items()
               if not k.startswith("_") and not (v["status"] == 403 and v["code"] == "ops_write_route_not_listed")}
        assert not bad, bad
        assert live["families"]["_sacrifice_active"] == {"revoke": True, "delete": True}

    def test_with_the_fence_removed_each_family_shows_its_own_layer(self, live):
        """Layer masking, the third direction: the fence call itself removed.
        Admin, PERSON and INTERACTIVE families still refuse with their own gate.
        The owner-tier families get exactly what an admin's own `user` key gets:
        the fence is the only layer there, which the PR states as not delivered."""
        no_fence, user = live["families_no_fence"], live["families_user_key"]
        fence_only = set()
        for fam in FAMILIES:
            k = f"{fam['key']} {fam['method']}"
            got = no_fence[k]
            assert not got["code"] or not got["code"].startswith("ops_write"), (k, got)
            if fam["own_gate"] == "person":
                assert got["status"] == 403 and got["code"] == "person_required", (k, got)
            elif fam["own_gate"] in GATE_DETAILS:
                assert got["status"] == 403 and got["detail"] == GATE_DETAILS[fam["own_gate"]], (k, got)
            else:
                assert (got["status"], got["body"]) == (user[k]["status"], user[k]["body"]), (k, got, user[k])
                assert got["status"] not in (401, 403), (k, got)
                fence_only.add(fam["key"])
        assert fence_only == {
            "routers/mcp_keys.py::revoke_mcp_api_key_endpoint",
            "routers/mcp_keys.py::delete_mcp_api_key_endpoint",
            "routers/credentials.py::inject_credentials",
            "routers/credentials.py::export_credentials",
            "routers/credentials.py::import_credentials",
            "routers/subscriptions.py::assign_subscription_to_agent",
        }
        # The reach is real, not a status code: the sacrificial keys are gone.
        assert no_fence["_sacrifice_active"] == {"revoke": False, "delete": False}

    def test_doors_that_validate_a_key_themselves(self, live):
        """Each door is driven with the `ops_write` key AND with the agent's own
        key (the pull seams with the agent made a pilot, else they refuse before
        reading any key). The agent's key getting past the key check is what
        makes the `ops_write` 403 the key check's answer, not a gate in front."""
        ow, agent = live["doors"]["ops_write"], live["doors"]["agent_key"]
        own_key = {
            "heartbeat": "Heartbeat requires the agent's own MCP key",
            "result_callback": "Result callback requires the agent's own MCP key",
            "pull_next_task": "Pull seam requires a valid X-Internal-Secret or the agent's own MCP key",
            "pull_task_result": "Pull seam requires a valid X-Internal-Secret or the agent's own MCP key",
        }
        for door, detail in own_key.items():
            assert ow[door]["status"] == 403 and ow[door]["detail"] == detail, (door, ow[door])
        assert agent["heartbeat"]["status"] == 200, agent["heartbeat"]
        assert agent["pull_next_task"]["status"] == 200, agent["pull_next_task"]
        # Past the key check, refused only because the execution does not exist.
        for door in ("result_callback", "pull_task_result"):
            assert agent[door]["status"] == 404 and agent[door]["detail"] == "Execution not found", (door, agent[door])
        # The one exception, stated: introspection answers "valid" and grants nothing.
        import json as _json
        validate = live["doors"]["mcp_validate"]
        body = _json.loads(validate["body"])
        assert validate["status"] == 200 and body["valid"] is True and body["scope"] == "ops_write"

    def test_the_optional_auth_door_treats_the_key_as_anonymous_on_a_live_share(self, live):
        """A live `authorized` canvas share: a viewer who can reach the agent gets
        the canvas, anyone else is asked to sign in. The `ops_write` key, whose
        owner CAN reach the agent, must get exactly the anonymous answer — and
        does only because of the fence: with the fence call removed it gets the
        canvas."""
        canvas = live["canvas"]
        assert canvas["owner_user_key"]["status"] == 200 and canvas["owner_user_key"]["share_status"] == "ok"
        assert canvas["owner_session"]["status"] == 200 and canvas["owner_session"]["share_status"] == "ok"
        anon = canvas["anonymous"]
        assert anon["status"] == 401 and anon["share_status"] == "sign_in_required", anon
        for who in ("ops_write", "bogus_key"):
            assert (canvas[who]["status"], canvas[who]["body"]) == (anon["status"], anon["body"]), (who, canvas[who])
        assert live["canvas_no_fence"]["status"] == 200, live["canvas_no_fence"]

    def test_neither_event_socket_opens(self, live):
        assert live["ws_events"] == {"closed": 4003}, live["ws_events"]
        assert live["ws_main"].get("closed") in (4001, 4003, 1008), live["ws_main"]
