"""Every route that names an agent declares the tier it enforces (ent#926).

The accessor check (`can_user_access_agent`: admin | owner | shared) means "may
chat with this agent". A route that names an agent and carries no declared tier
inherits it by accident. This census flips the default: a route is OWNER tier
unless its row in ``agent_route_tiers.json`` says otherwise, and a route with
no row fails CI.

The table is read against the code, not trusted:
  * every live agent-naming route has a row, every row has a live route (a
    renamed handler or a changed path must be re-declared);
  * a row declared ``owner`` must show an owner/admin gate in the handler or in
    the helper named in ``via``; a row declared ``accessor`` must NOT. A gate is
    read from the code's identifiers (names and attributes on the AST), so a
    gate name in a comment, docstring or message string does not count;
  * the other four tiers are held to the code too (trinity-enterprise#978): a
    ``self`` row shows a self gate and no owner gate, in the handler or one
    level into a helper of the same file that the handler names (a gate run on
    a worker thread sits in such a helper); an ``internal`` row sits on a router
    that requires the internal secret; a ``resource`` row shows its resource
    predicate, or calls a DB writer whose own body carries the creator check;
    and a ``public`` row shows no user or agent gate and is on the reviewed
    list ``PUBLIC_ROUTES``. The runtime fence leaves ``self``,
    ``public`` and ``internal`` rows open to an agent's key in a restricted
    turn, so a row that claims one of them without enforcing it is a hole;
  * the accessor rows tagged ``legacy`` are the debt list - a shrink-only
    ratchet. A new accessor row needs a real reason, and the legacy count may
    only fall (lower ``LEGACY_ACCESSOR_BASELINE`` when you tighten a route).

The table also drives the auth entry point at runtime
(``services/route_tier_service.py``): an agent's key in a restricted turn is
refused every ``owner`` row and every ``standing_work`` row. ``restricted_turn``
on an owner row lets that key through, and must say why.

Static (AST, stdlib only) on purpose: importing the assembled app in the unit
island is skipped in whole-directory runs, which would make this guard decorative.
"""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
_ROUTERS = _BACKEND / "routers"
_TABLE = _BACKEND / "services" / "agent_route_tiers.json"

HTTP = {"get", "post", "put", "delete", "patch", "websocket"}
TIERS = {"owner", "accessor", "self", "public", "internal", "resource"}
LEGACY = "legacy"
# Lower this number when a legacy accessor row is tightened or removed. It may
# never be raised: a new accessor route carries a real reason instead.
LEGACY_ACCESSOR_BASELINE = 113

OWNER_GATE = re.compile(
    r"\b(OwnedAgentByName|OwnedAgent|assert_agent_owner|get_owned_agent\w*|assert_admin|"
    r"require_admin\w*|assert_owns_or_admin|assert_owns|assert_not_share_viewer|"
    r"can_user_share_agent|can_user_delete_agent|can_user_rename_agent)\b")
ACCESSOR_GATE = re.compile(
    r"\b(AuthorizedAgentByName|AuthorizedAgent|assert_agent_access|"
    r"get_authorized_agent\w*|can_user_access_agent|get_accessible_agents|"
    r"accessible_agent_names|_assert_agent_accessible)\b")

# The gate each of the other tiers shows (trinity-enterprise#978).
# These refuse everyone but the agent itself...
SELF_ONLY_GATE = re.compile(r"\b(get_self_acting_agent|get_self_agent|authorize_heartbeat)\b")
# ...while this one holds an agent's key to its own agent and is a no-op for a
# person, so a route that self-gates with it must check the person's access too.
OWN_AGENT_GATE = re.compile(r"\b(assert_own_agent)\b")
INTERNAL_GATE = re.compile(r"\b(verify_internal_secret)\b")
# A resource row shows its predicate in the handler...
RESOURCE_PREDICATE = re.compile(r"\b(_assert_can_manage_webhook|_can_manage_webhook)\b")
# ...or leaves it to the DB writer it calls. Then calling the writer proves
# nothing by itself (every handler does its job): the WRITER's body is read for
# the creator check (`shows_creator_check`).
RESOURCE_WRITERS = frozenset({"update_schedule", "delete_schedule", "set_allow_proactive"})
# Anything that resolves a signed-in principal: a public route shows none.
USER_AUTH_GATE = re.compile(
    r"\b(get_current_user\w*|CurrentUser|get_optional_user|get_user_or_anonymous|"
    r"require_\w+|decode_token|get_portal_principal\w*)\b")
# Every route declared `public`, reviewed one by one. An agent's key in a
# restricted turn may call these, and "shows no gate" cannot tell an open route
# from one gated by a helper this file has never heard of, so a new public
# route is added HERE as well as to the table.
PUBLIC_ROUTES = frozenset({
    "a2a.py::a2a_well_known_card",
    "avatar.py::get_avatar",
    "avatar.py::get_avatar_emotion",
    "avatar.py::get_avatar_emotions",
    "avatar.py::get_avatar_reference",
    "paid.py::get_paid_agent_info",
    "paid.py::paid_chat",
})

_HOW_TO = (
    "Declare it in src/backend/services/agent_route_tiers.json: tier 'owner' (default - use an "
    "OwnedAgent dependency or assert_agent_owner), or another tier with a reason a "
    "reviewer can read. Accessor tier means a person the agent is merely shared with "
    "may call it; say why that is intended.")


def _prefixes(tree: ast.AST) -> dict[str, str]:
    out: dict[str, str] = {}
    for n in ast.walk(tree):
        if (isinstance(n, ast.Assign) and isinstance(n.value, ast.Call)
                and getattr(n.value.func, "id", "") == "APIRouter"):
            pre = ""
            for kw in n.value.keywords:
                if kw.arg == "prefix" and isinstance(kw.value, ast.Constant):
                    pre = kw.value.value
            for t in n.targets:
                if isinstance(t, ast.Name):
                    out[t.id] = pre
    return out


def _router_gates(tree: ast.AST) -> dict[str, frozenset[str]]:
    """router variable -> the identifiers in its `APIRouter(dependencies=...)`:
    a gate declared once for every route on the router (`routers/internal.py`)."""
    out: dict[str, frozenset[str]] = {}
    for n in ast.walk(tree):
        if (isinstance(n, ast.Assign) and isinstance(n.value, ast.Call)
                and getattr(n.value.func, "id", "") == "APIRouter"):
            names: frozenset[str] = frozenset()
            for kw in n.value.keywords:
                if kw.arg == "dependencies":
                    names = identifiers(kw.value)
            for t in n.targets:
                if isinstance(t, ast.Name):
                    out[t.id] = names
    return out


def _names_an_agent(full: str) -> bool:
    return ("{agent_name}" in full or "/agents/{" in full
            or full.startswith("/api/agent-dashboard/{name}"))


def identifiers(node: ast.AST) -> frozenset[str]:
    """Every name and attribute the code under `node` uses (decorators included).
    Comments are not on the AST and strings are not identifiers, so neither can
    stand in for a gate."""
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Attribute):
            out.add(n.attr)
    return frozenset(out)


def shows(gate: re.Pattern, names) -> bool:
    return any(gate.fullmatch(name) for name in names)


def scan_source(src: str, fname: str) -> dict[str, dict]:
    """key -> {method, path, segment} for every agent-naming route in one file."""
    tree = ast.parse(src)
    pre = _prefixes(tree)
    gates = _router_gates(tree)
    # Module-level functions of this file: a handler that names one is read one
    # level into it (never further), so a gate behind `asyncio.to_thread(helper,
    # ...)` or a plain call is seen, and a helper cannot hide an owner gate.
    local = {f.name: identifiers(f) for f in tree.body
             if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))}
    found: dict[str, dict] = {}
    for n in ast.walk(tree):
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for d in n.decorator_list:
            if not (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                    and d.func.attr in HTTP and d.args and isinstance(d.args[0], ast.Constant)):
                continue
            router = getattr(d.func.value, "id", "")
            full = pre.get(router, "") + d.args[0].value
            if not _names_an_agent(full):
                continue
            names = identifiers(n)
            helper_names = frozenset().union(
                *(local[h] for h in names if h in local and h != n.name))
            found[f"{fname}::{n.name}"] = {"method": d.func.attr.upper(), "path": full,
                                           "names": names, "helper_names": helper_names,
                                           "router_names": gates.get(router, frozenset())}
    return found


def live_routes() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for f in sorted(_ROUTERS.rglob("*.py")):
        out.update(scan_source(f.read_text(), str(f.relative_to(_ROUTERS))))
    return out


_DEF_CACHE: dict[str, list[frozenset]] | None = None


def _bodies(name: str) -> list[frozenset]:
    """Identifiers of every function called `name` in the backend (private submodule excluded)."""
    global _DEF_CACHE
    if _DEF_CACHE is None:
        _DEF_CACHE = {}
        for f in _BACKEND.rglob("*.py"):
            if "enterprise" in f.parts or "tests" in f.parts:
                continue
            try:
                src = f.read_text()
                tree = ast.parse(src)
            except (SyntaxError, UnicodeDecodeError):
                continue
            for n in ast.walk(tree):
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    _DEF_CACHE.setdefault(n.name, []).append(identifiers(n))
    return _DEF_CACHE.get(name, [])


def _gate_names(live: dict, row: dict) -> frozenset:
    names = set(live["names"])
    via = row.get("via", "")
    if via:
        bodies = _bodies(via)
        assert bodies, f"'via' names {via!r}, which is not a function in the backend"
        for body in bodies:
            names |= body
    return frozenset(names)


TABLE: dict[str, dict] = json.loads(_TABLE.read_text())
LIVE = live_routes()


def test_the_table_is_nonempty_and_well_formed():
    assert len(TABLE) > 250
    for key, row in TABLE.items():
        assert row["tier"] in TIERS, key
        assert row["method"] and row["path"], key
        if row["tier"] != "owner":
            assert row.get("reason"), f"{key}: a non-owner tier needs a reason"


def test_every_agent_route_is_declared():
    missing = sorted(set(LIVE) - set(TABLE))
    assert not missing, (
        "Agent-naming routes with no declared tier (default is owner, and CI fails "
        "undeclared): " + ", ".join(f"{k} [{LIVE[k]['method']} {LIVE[k]['path']}]" for k in missing)
        + ". " + _HOW_TO)


def test_no_row_outlives_its_route():
    stale = sorted(set(TABLE) - set(LIVE))
    assert not stale, ("Rows with no live route (renamed, moved or deleted - re-declare "
                       "or remove): " + ", ".join(stale))


@pytest.mark.parametrize("key", sorted(set(TABLE) & set(LIVE)))
def test_the_row_describes_the_route(key):
    assert (TABLE[key]["method"], TABLE[key]["path"]) == (LIVE[key]["method"], LIVE[key]["path"])


def _owner_rule(key, row, names, live):
    if not shows(OWNER_GATE, names):
        return "is declared owner but shows no owner/admin gate (add one, or re-declare)"


def _accessor_rule(key, row, names, live):
    if not shows(ACCESSOR_GATE, names):
        return "is declared accessor but shows no accessor gate"
    # `mentions_owner` marks a read that only COMPUTES an owner flag
    # (e.g. `can_share`) for the response; it does not gate on it.
    if shows(OWNER_GATE, names) and not row.get("mentions_owner"):
        return ("is declared accessor but now shows an owner/admin gate - re-declare "
                "it owner and lower LEGACY_ACCESSOR_BASELINE")


def _self_rule(key, row, names, live):
    # Read from the handler alone: a route that also has a person path behind
    # an owner gate is an owner route, and stays fenced in a restricted turn.
    if row.get("via"):
        return "is declared self with a `via`: a self gate is read from the handler itself"
    seen = live["names"] | live.get("helper_names", frozenset())
    if shows(OWNER_GATE, seen):
        return "is declared self but shows an owner/admin gate - re-declare it owner"
    if not shows(SELF_ONLY_GATE, seen):
        if not shows(OWN_AGENT_GATE, seen):
            return ("is declared self but shows no self gate (assert_own_agent, "
                    "get_self_acting_agent, authorize_heartbeat)")
        if not shows(ACCESSOR_GATE, seen):
            return ("is declared self but gates with assert_own_agent alone, which lets every "
                    "signed-in person through: check the person's access too")


def _internal_rule(key, row, names, live):
    if not shows(INTERNAL_GATE, live["router_names"]):
        return ("is declared internal but its router does not require the internal secret "
                "(`APIRouter(dependencies=[Depends(verify_internal_secret)])`)")


def _public_rule(key, row, names, live):
    if key not in PUBLIC_ROUTES:
        return ("is declared public but is not on the reviewed list PUBLIC_ROUTES in "
                "tests/unit/test_ent926_agent_route_census.py")
    seen = names | live["router_names"]
    for label, gate in (("owner/admin", OWNER_GATE), ("accessor", ACCESSOR_GATE),
                        ("signed-in user", USER_AUTH_GATE), ("internal-secret", INTERNAL_GATE)):
        if shows(gate, seen):
            return f"is declared public but shows a {label} gate - re-declare it"


_DB_FUNCTIONS: dict[str, list] | None = None


def _db_functions(name: str) -> list:
    """Every function called `name` under src/backend/db (the AST nodes)."""
    global _DB_FUNCTIONS
    if _DB_FUNCTIONS is None:
        _DB_FUNCTIONS = {}
        for f in sorted((_BACKEND / "db").rglob("*.py")):
            for n in ast.walk(ast.parse(f.read_text())):
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    _DB_FUNCTIONS.setdefault(n.name, []).append(n)
    return _DB_FUNCTIONS.get(name, [])


def shows_creator_check(fn: ast.AST) -> bool:
    """Does this DB writer refuse a caller who is not the row's creator (or the
    agent's owner)? A COMPARISON on `owner_id`, or a call to the owner predicate.
    A column name in a string or an assignment to `owner_id` is not a check."""
    for n in ast.walk(fn):
        if isinstance(n, ast.Compare):
            for side in (n.left, *n.comparators):
                if isinstance(side, ast.Attribute) and side.attr == "owner_id":
                    return True
                if (isinstance(side, ast.Subscript) and isinstance(side.slice, ast.Constant)
                        and side.slice.value == "owner_id"):
                    return True
        if isinstance(n, ast.Call) and getattr(
                n.func, "attr", getattr(n.func, "id", None)) == "can_user_share_agent":
            return True
    return False


def _resource_rule(key, row, names, live):
    if shows(RESOURCE_PREDICATE, names):
        return None
    writers = sorted(RESOURCE_WRITERS & set(names))
    if not writers:
        return ("is declared resource but shows none of the resource predicates "
                "(webhook manager, or a DB writer that checks the creator)")
    for writer in writers:
        bodies = _db_functions(writer)
        if not bodies or not all(shows_creator_check(fn) for fn in bodies):
            return f"is declared resource, but its DB writer {writer} shows no creator check"


TIER_RULES = {"owner": _owner_rule, "accessor": _accessor_rule, "self": _self_rule,
              "internal": _internal_rule, "public": _public_rule, "resource": _resource_rule}


def tier_gate_problem(key: str, row: dict, live: dict) -> str | None:
    """What is wrong with `row`'s declared tier given the route's code, or None.
    The one function the live table and the synthetic cases below both go through."""
    rule = TIER_RULES.get(row.get("tier"))
    if rule is None:
        return f"{key} declares tier {row.get('tier')!r}, which has no gate rule"
    problem = rule(key, row, _gate_names(live, row), live)
    return f"{key} {problem}" if problem else None


@pytest.mark.parametrize("key", sorted(set(TABLE) & set(LIVE)))
def test_a_declared_tier_matches_the_gate_in_the_code(key):
    assert tier_gate_problem(key, TABLE[key], LIVE[key]) is None


def test_every_tier_has_a_gate_rule():
    """A seventh tier cannot arrive unchecked."""
    assert set(TIER_RULES) == TIERS
    assert "no gate rule" in tier_gate_problem("x.py::y", {"tier": "brand_new"}, {"names": frozenset()})


def test_the_public_list_is_exactly_the_public_rows():
    assert PUBLIC_ROUTES == {k for k, r in TABLE.items() if r["tier"] == "public"}


def test_legacy_accessor_debt_only_shrinks():
    legacy = [k for k, r in TABLE.items() if r["tier"] == "accessor" and r["reason"] == LEGACY]
    assert len(legacy) <= LEGACY_ACCESSOR_BASELINE, (
        f"{len(legacy)} legacy accessor rows > baseline {LEGACY_ACCESSOR_BASELINE}: a new "
        "accessor route needs a real reason, not 'legacy'")
    assert len(legacy) >= LEGACY_ACCESSOR_BASELINE, (
        f"only {len(legacy)} legacy accessor rows: lower LEGACY_ACCESSOR_BASELINE to "
        f"{len(legacy)} so the ratchet keeps biting")


def test_a_non_legacy_accessor_row_gives_a_real_reason():
    for key, row in TABLE.items():
        if row["tier"] == "accessor" and row["reason"] != LEGACY:
            assert len(row["reason"]) >= 20, f"{key}: say why a shared person may call this"


# ---- the scanner itself ----------------------------------------------------------

_SYNTH = '''
from fastapi import APIRouter
router = APIRouter(prefix="/api/agents")

@router.get("/{agent_name}/thing")
async def thing(agent_name: AuthorizedAgentByName):
    return 1

@router.post("/{agent_name}/danger")
async def danger(agent_name: str, current_user=Depends(get_current_user)):
    return 2

@router.get("/templates/{name}")
async def not_an_agent(name: str):
    return 3
'''


def test_the_scanner_finds_agent_routes_and_only_those():
    found = scan_source(_SYNTH, "synth.py")
    assert set(found) == {"synth.py::thing", "synth.py::danger"}
    assert found["synth.py::thing"]["path"] == "/api/agents/{agent_name}/thing"


def test_the_scanner_reads_gates_from_the_handler():
    found = scan_source(_SYNTH, "synth.py")
    assert shows(ACCESSOR_GATE, found["synth.py::thing"]["names"])
    assert not shows(OWNER_GATE, found["synth.py::thing"]["names"])
    assert not (shows(ACCESSOR_GATE, found["synth.py::danger"]["names"])
                or shows(OWNER_GATE, found["synth.py::danger"]["names"]))


_SYNTH_COMMENTED = '''
from fastapi import APIRouter
router = APIRouter(prefix="/api/agents")

@router.post("/{agent_name}/danger")
async def danger(agent_name: str, current_user=Depends(get_current_user)):
    """Owner only: callers must pass assert_agent_owner."""
    # OwnedAgentByName would be the right dependency here
    raise HTTPException(403, "assert_agent_owner failed")
'''


def test_a_gate_named_only_in_a_comment_docstring_or_string_does_not_count():
    """PR #3778 review: the check was a regex over source text."""
    names = scan_source(_SYNTH_COMMENTED, "synth.py")["synth.py::danger"]["names"]
    assert not shows(OWNER_GATE, names)


# ---- the runtime fence reads this table (PR #3778 review) -------------------------------

def test_runtime_flags_are_well_formed():
    for key, row in TABLE.items():
        assert row.get("standing_work") in (None, True), key
        if "restricted_turn" in row:
            assert row["tier"] == "owner", f"{key}: restricted_turn only loosens an owner row"
            assert row["restricted_turn"].startswith("allow: ") and len(row["restricted_turn"]) >= 30, (
                f"{key}: say why an agent's key may use it in a shared person's turn")


def test_standing_work_routes_that_name_no_agent_are_live():
    """The two standing-work routes the census has no row for are pinned by method and
    template, so a renamed route fails here instead of dropping out of the fence."""
    import sys
    sys.path.insert(0, str(_BACKEND))
    try:
        from services.route_tier_service import STANDING_WORK_UNNAMED, names_an_agent
    finally:
        sys.path.remove(str(_BACKEND))
    live = set()
    for f in sorted(_ROUTERS.rglob("*.py")):
        src = f.read_text()
        tree = ast.parse(src)
        pre = _prefixes(tree)
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for d in n.decorator_list:
                    if (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                            and d.func.attr in HTTP and d.args and isinstance(d.args[0], ast.Constant)):
                        live.add((d.func.attr.upper(),
                                  pre.get(getattr(d.func.value, "id", ""), "") + d.args[0].value))
    for method, template in STANDING_WORK_UNNAMED:
        assert (method, template) in live, f"{method} {template} is not a live route"
        assert not names_an_agent(template)


# Creator-gated routes that name no agent and are deliberately open in a restricted
# turn, with the reason. Everything else of that shape must be in OWNER_TIER_UNNAMED.
CREATOR_UNNAMED_OPEN = {
    ("GET", "/api/systems/manifests"): "a read of the manifest list; creates and touches nothing",
    ("GET", "/api/systems/manifests/{manifest_id}"): "a read of one manifest; creates and touches nothing",
}


def _creator_gated_unnamed_routes():
    out = set()
    for f in sorted(_ROUTERS.rglob("*.py")):
        src = f.read_text()
        tree = ast.parse(src)
        pre = _prefixes(tree)
        for n in ast.walk(tree):
            if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            # The gate may sit on a handler argument or in the decorator's
            # `dependencies=[...]` (PR #3778 review); scan both.
            scopes = [n.args] + [
                kw.value for d in n.decorator_list if isinstance(d, ast.Call)
                for kw in d.keywords if kw.arg == "dependencies"
            ]
            gated = any(
                isinstance(c, ast.Call) and getattr(c.func, "id", getattr(c.func, "attr", None)) == "require_role"
                and c.args and isinstance(c.args[0], ast.Constant) and c.args[0].value == "creator"
                for scope in scopes for c in ast.walk(scope)
            )
            if not gated:
                continue
            for d in n.decorator_list:
                if (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                        and d.func.attr in HTTP and d.args and isinstance(d.args[0], ast.Constant)):
                    path = pre.get(getattr(d.func.value, "id", ""), "") + d.args[0].value
                    if not _names_an_agent(path):
                        out.add((d.func.attr.upper(), path))
    return out


def test_every_creator_gated_route_that_names_no_agent_is_fenced_or_declared_open():
    """PR #3778 review: `POST /api/agents` named no agent, so it had no census row
    and was open in a restricted turn — while the agent key carries the owner's role.
    A new create/deploy route of that shape fails here until it is classified."""
    import sys
    sys.path.insert(0, str(_BACKEND))
    try:
        from services.route_tier_service import OWNER_TIER_UNNAMED
    finally:
        sys.path.remove(str(_BACKEND))
    found = _creator_gated_unnamed_routes()
    assert found, "the scan found no creator-gated route; it has gone blind"
    unclassified = found - set(OWNER_TIER_UNNAMED) - set(CREATOR_UNNAMED_OPEN)
    assert not unclassified, f"classify in OWNER_TIER_UNNAMED or CREATOR_UNNAMED_OPEN: {sorted(unclassified)}"
    stale = (set(OWNER_TIER_UNNAMED) | set(CREATOR_UNNAMED_OPEN)) - found
    assert not stale, f"no longer a live creator-gated route: {sorted(stale)}"


def test_the_runtime_reads_the_same_agent_naming_test_as_the_census():
    import sys
    sys.path.insert(0, str(_BACKEND))
    try:
        from services.route_tier_service import names_an_agent
    finally:
        sys.path.remove(str(_BACKEND))
    for path in [r["path"] for r in TABLE.values()] + ["/api/agents", "/api/templates/{name}"]:
        assert names_an_agent(path) == _names_an_agent(path), path


def test_an_undeclared_synthetic_route_would_be_reported():
    undeclared = set(scan_source(_SYNTH, "synth.py")) - set(TABLE)
    assert undeclared == {"synth.py::thing", "synth.py::danger"}


# ---- the gate rules of the other four tiers (trinity-enterprise#978) ----------------------

_SYNTH_TIERS = '''
from fastapi import APIRouter, Depends
router = APIRouter(prefix="/api/internal", dependencies=[Depends(verify_internal_secret)])
pull_router = APIRouter(prefix="/api/internal")
open_router = APIRouter(prefix="/api/agents")

@router.post("/agents/{name}/sealed")
async def sealed(name: str):
    return 1

@pull_router.post("/agents/{name}/claim")
async def claim(name: str):
    return 2

@open_router.post("/{agent_name}/own")
async def own(agent_name: str, current_user=Depends(get_current_user)):
    assert_own_agent(current_user, agent_name)

@open_router.post("/{agent_name}/own-and-access")
async def own_and_access(agent_name: str, current_user=Depends(get_current_user)):
    assert_own_agent(current_user, agent_name)
    assert_agent_access(current_user, agent_name)

@open_router.get("/{agent_name}/mine")
async def mine(agent: str = Depends(get_self_acting_agent)):
    return 5

def _require_own_key(request, agent_name):
    if not heartbeat_service.authorize_heartbeat(request, agent_name):
        raise HTTPException(403)

def _reads_the_body(request):
    return request

def _own_then_owner(current_user, agent_name):
    assert_own_agent(current_user, agent_name)
    assert_agent_owner(current_user, agent_name)

def _two_levels(request, agent_name):
    _require_own_key(request, agent_name)

@open_router.post("/{agent_name}/threaded")
async def threaded(agent_name: str, request: Request):
    await asyncio.to_thread(_require_own_key, request, agent_name)

@open_router.post("/{agent_name}/helper-without-a-gate")
async def helper_without_a_gate(agent_name: str, request: Request):
    _reads_the_body(request)

@open_router.post("/{agent_name}/helper-hides-owner")
async def helper_hides_owner(agent_name: str, current_user=Depends(get_current_user)):
    _own_then_owner(current_user, agent_name)

@open_router.post("/{agent_name}/two-levels")
async def two_levels(agent_name: str, request: Request):
    _two_levels(request, agent_name)

@open_router.put("/{agent_name}/renames")
async def renames(agent_name: str, current_user=Depends(get_current_user)):
    return db.rename_schedule(agent_name, current_user.username)

@open_router.put("/{agent_name}/updates")
async def updates(agent_name: str, current_user=Depends(get_current_user)):
    return db.update_schedule(agent_name, current_user.username, {})

@open_router.post("/{agent_name}/own-or-owner")
async def own_or_owner(agent_name: str, current_user=Depends(get_current_user)):
    assert_own_agent(current_user, agent_name)
    assert_agent_owner(current_user, agent_name)

@open_router.get("/{agent_name}/card")
async def card(agent_name: str):
    return 3

@open_router.get("/{agent_name}/secret-card")
async def secret_card(agent_name: str, current_user=Depends(get_current_user)):
    return 4

@open_router.put("/{agent_name}/hook")
async def hook(agent_name: str, current_user=Depends(get_current_user)):
    _assert_can_manage_webhook(current_user, agent_name)
'''


def _synth_problem(handler, tier, **row):
    live = scan_source(_SYNTH_TIERS, "synth.py")[f"synth.py::{handler}"]
    return tier_gate_problem(f"synth.py::{handler}", {"tier": tier, "reason": "x" * 20, **row}, live)


@pytest.mark.parametrize("handler,tier", [
    ("sealed", "internal"), ("own_and_access", "self"), ("mine", "self"), ("hook", "resource"),
    ("threaded", "self"),       # the gate sits in a helper of the same file, run on a thread
    ("updates", "resource"),    # the creator check sits in the DB writer, and is read there
])
def test_a_route_that_shows_its_tiers_gate_passes(handler, tier):
    assert _synth_problem(handler, tier) is None


@pytest.mark.parametrize("handler,tier,says", [
    ("claim", "internal", "does not require the internal secret"),   # on the router with no blanket secret
    ("card", "internal", "does not require the internal secret"),
    ("card", "self", "shows no self gate"),
    ("secret_card", "self", "shows no self gate"),                   # signed in is not a self gate
    ("own", "self", "lets every signed-in person through"),          # a no-op for a person, and no access check
    ("own_or_owner", "self", "shows an owner/admin gate"),           # a person path makes it owner tier
    ("card", "resource", "none of the resource predicates"),
    ("own", "resource", "none of the resource predicates"),
    ("card", "public", "not on the reviewed list"),                  # open, but nobody reviewed it
    ("helper_without_a_gate", "self", "shows no self gate"),         # a helper, but it gates nothing
    ("helper_hides_owner", "self", "shows an owner/admin gate"),     # a helper cannot hide a person path
    ("two_levels", "self", "shows no self gate"),                    # one level of helper, no further
    ("renames", "resource", "none of the resource predicates"),      # a DB call that is not a known writer
])
def test_a_route_that_does_not_show_its_tiers_gate_is_reported(handler, tier, says):
    assert says in (_synth_problem(handler, tier) or "")


def test_a_self_row_cannot_borrow_its_gate_through_via():
    assert "a self gate is read from the handler itself" in _synth_problem("card", "self", via="assert_own_agent")


@pytest.mark.parametrize("handler,says", [
    ("card", None),
    ("secret_card", "signed-in user gate"),
    ("own_or_owner", "owner/admin gate"),
    ("sealed", "internal-secret gate"),
])
def test_a_reviewed_public_route_must_still_show_no_gate(monkeypatch, handler, says):
    """On the list AND gate-free: the list alone would keep blessing a route
    that later grew a gate and should have been re-declared."""
    monkeypatch.setattr(sys.modules[__name__], "PUBLIC_ROUTES", PUBLIC_ROUTES | {f"synth.py::{handler}"})
    problem = _synth_problem(handler, "public")
    assert problem is None if says is None else says in problem



# ---- a resource row whose check lives in the DB writer (PR #4760 review) ------------------

@pytest.mark.parametrize("writer", sorted(RESOURCE_WRITERS))
def test_every_resource_writer_carries_its_creator_check(writer):
    """The handler calling the writer proves nothing: it is the writer that must
    refuse a caller who is not the creator, the owner or an admin."""
    bodies = _db_functions(writer)
    assert bodies, f"{writer} is not a function under src/backend/db"
    assert all(shows_creator_check(fn) for fn in bodies), f"db {writer} shows no creator check"


@pytest.mark.parametrize("writer_source", [
    "def update_schedule(self, schedule_id, username, updates):\n    return self._write(schedule_id, updates)\n",
    None,   # no such function under db/
])
def test_calling_a_writer_that_checks_nothing_does_not_make_a_resource_row(monkeypatch, writer_source):
    bodies = [ast.parse(writer_source).body[0]] if writer_source else []
    monkeypatch.setattr(sys.modules[__name__], "_db_functions", lambda name: bodies)
    assert "its DB writer update_schedule shows no creator check" in _synth_problem("updates", "resource")


@pytest.mark.parametrize("body,expected", [
    ("if user['role'] != 'admin' and schedule.owner_id != user['id']:\n        return None", True),
    ("if row['owner_id'] != user['id']:\n        return False", True),
    ("where.append(agent_schedules.c.owner_id == user['id'])", True),
    ("if not self.can_user_share_agent(username, agent_name):\n        return False", True),
    ("schedule.owner_id = user['id']", False),               # an assignment is not a check
    ("fields = ['owner_id', 'updated_at']", False),           # a column name in a string is not a check
    ("return self._write(schedule_id, updates)", False),
])
def test_what_counts_as_a_creator_check(body, expected):
    fn = ast.parse(f"def writer(self, schedule_id, username, updates):\n    {body}\n").body[0]
    assert shows_creator_check(fn) is expected
