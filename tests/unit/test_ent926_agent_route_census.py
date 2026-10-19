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
    the helper named in ``via``; a row declared ``accessor`` must NOT;
  * the accessor rows tagged ``legacy`` are the debt list - a shrink-only
    ratchet. A new accessor row needs a real reason, and the legacy count may
    only fall (lower ``LEGACY_ACCESSOR_BASELINE`` when you tighten a route).

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
_TABLE = Path(__file__).with_name("agent_route_tiers.json")

HTTP = {"get", "post", "put", "delete", "patch", "websocket"}
TIERS = {"owner", "accessor", "self", "public", "internal", "resource"}
LEGACY = "legacy"
# Lower this number when a legacy accessor row is tightened or removed. It may
# never be raised: a new accessor route carries a real reason instead.
LEGACY_ACCESSOR_BASELINE = 116

OWNER_GATE = re.compile(
    r"\b(OwnedAgentByName|OwnedAgent|assert_agent_owner|get_owned_agent\w*|assert_admin|"
    r"require_admin\w*|assert_owns_or_admin|assert_owns|assert_not_share_viewer|"
    r"can_user_share_agent|can_user_delete_agent|can_user_rename_agent)\b")
ACCESSOR_GATE = re.compile(
    r"\b(AuthorizedAgentByName|AuthorizedAgent|assert_agent_access|"
    r"get_authorized_agent\w*|can_user_access_agent|get_accessible_agents|"
    r"accessible_agent_names|_assert_agent_accessible)\b")

_HOW_TO = (
    "Declare it in tests/unit/agent_route_tiers.json: tier 'owner' (default - use an "
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
            seg = "\n".join(ast.get_source_segment(src, x) or "" for x in n.decorator_list)
            seg += "\n" + (ast.get_source_segment(src, n) or "")
            found[f"{fname}::{n.name}"] = {"method": d.func.attr.upper(), "path": full,
                                           "segment": seg}
    return found


def live_routes() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for f in sorted(_ROUTERS.rglob("*.py")):
        out.update(scan_source(f.read_text(), str(f.relative_to(_ROUTERS))))
    return out


_DEF_CACHE: dict[str, list[str]] | None = None


def _bodies(name: str) -> list[str]:
    """Source of every function called `name` in the backend (private submodule excluded)."""
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
                    _DEF_CACHE.setdefault(n.name, []).append(ast.get_source_segment(src, n) or "")
    return _DEF_CACHE.get(name, [])


def _gate_text(live: dict, row: dict) -> str:
    text = live["segment"]
    via = row.get("via", "")
    if via:
        bodies = _bodies(via)
        assert bodies, f"'via' names {via!r}, which is not a function in the backend"
        text += "\n" + "\n".join(bodies)
    return text


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
    row, text = TABLE[key], _gate_text(LIVE[key], TABLE[key])
    owner, accessor = bool(OWNER_GATE.search(text)), bool(ACCESSOR_GATE.search(text))
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
    assert ACCESSOR_GATE.search(found["synth.py::thing"]["segment"])
    assert not OWNER_GATE.search(found["synth.py::thing"]["segment"])
    assert not (ACCESSOR_GATE.search(found["synth.py::danger"]["segment"])
                or OWNER_GATE.search(found["synth.py::danger"]["segment"]))


def test_an_undeclared_synthetic_route_would_be_reported():
    undeclared = set(scan_source(_SYNTH, "synth.py")) - set(TABLE)
    assert undeclared == {"synth.py::thing", "synth.py::danger"}
