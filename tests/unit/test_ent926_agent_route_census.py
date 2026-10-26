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
LEGACY_ACCESSOR_BASELINE = 115

OWNER_GATE = re.compile(
    r"\b(OwnedAgentByName|OwnedAgent|assert_agent_owner|get_owned_agent\w*|assert_admin|"
    r"require_admin\w*|assert_owns_or_admin|assert_owns|assert_not_share_viewer|"
    r"can_user_share_agent|can_user_delete_agent|can_user_rename_agent)\b")
ACCESSOR_GATE = re.compile(
    r"\b(AuthorizedAgentByName|AuthorizedAgent|assert_agent_access|"
    r"get_authorized_agent\w*|can_user_access_agent|get_accessible_agents|"
    r"accessible_agent_names|_assert_agent_accessible)\b")

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
    found: dict[str, dict] = {}
    for n in ast.walk(tree):
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for d in n.decorator_list:
            if not (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                    and d.func.attr in HTTP and d.args and isinstance(d.args[0], ast.Constant)):
                continue
            full = pre.get(getattr(d.func.value, "id", ""), "") + d.args[0].value
            if not _names_an_agent(full):
                continue
            found[f"{fname}::{n.name}"] = {"method": d.func.attr.upper(), "path": full,
                                           "names": identifiers(n)}
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


@pytest.mark.parametrize("key", sorted(k for k in set(TABLE) & set(LIVE)
                                        if TABLE[k]["tier"] in ("owner", "accessor")))
def test_a_declared_tier_matches_the_gate_in_the_code(key):
    row, names = TABLE[key], _gate_names(LIVE[key], TABLE[key])
    owner, accessor = shows(OWNER_GATE, names), shows(ACCESSOR_GATE, names)
    if row["tier"] == "owner":
        assert owner, f"{key} is declared owner but shows no owner/admin gate (add one, or re-declare)"
    else:
        assert accessor, f"{key} is declared accessor but shows no accessor gate"
        # `mentions_owner` marks a read that only COMPUTES an owner flag
        # (e.g. `can_share`) for the response; it does not gate on it.
        assert row.get("mentions_owner") or not owner, (
            f"{key} is declared accessor but now shows an owner/admin gate - re-declare "
            "it owner and lower LEGACY_ACCESSOR_BASELINE")


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
            gated = any(
                isinstance(c, ast.Call) and getattr(c.func, "id", getattr(c.func, "attr", None)) == "require_role"
                and c.args and isinstance(c.args[0], ast.Constant) and c.args[0].value == "creator"
                for c in ast.walk(n.args)
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
