"""An agent dependency must match its route's path-param spelling (#2094).

`dependencies.py` ships two spellings of each agent gate, differing ONLY in the
`Path(...)` name they read:

    OwnedAgent           -> get_owned_agent(name: str = Path(...))
    OwnedAgentByName     -> get_owned_agent_by_name(agent_name: str = Path(...))
    AuthorizedAgent      -> get_authorized_agent(name: str = Path(...))
    AuthorizedAgentByName-> get_authorized_agent_by_name(agent_name: str = Path(...))

Pair the wrong one with a route and FastAPI can never satisfy the dependency, so
it rejects the request at validation time:

    {"detail":[{"type":"missing","loc":["path","agent_name"],"msg":"Field required"}]}

#2081 (CSO M1) moved schedule enable/disable/trigger to owner-tier — correct
intent — but picked `OwnedAgentByName` on routes declared `/{name}/...`. All
three returned **422 to every caller**, owner included, breaking the REST API,
the frontend Schedules tab, and the MCP `toggle_agent_schedule` /
`trigger_agent_schedule` tools.

WHY THIS NEEDS A GUARD AND NOT JUST A FIX

The swap is invisible at every layer that normally catches things:

* It reads as a *tightening* — the diff swaps one owner-tier gate for another,
  which is exactly what the security review asked for.
* Python is happy: both names are imported and valid.
* The failure is a 422, not a 500 or a traceback, so it looks like a client
  problem rather than a server one.
* It is uniform. There is no "works for the owner, fails for a shared user"
  asymmetry to notice — it fails for *everyone*, which perversely makes it
  easier to mistake for an unrelated request-shape issue.
* And the required checks on `dev` do not run the integration tests that caught
  it, so it merged green.

There are 146 uses of these four aliases across the routers. Any future
owner/accessor tier change touches one of them, and the same swap ships the same
way.

STATIC BY DESIGN

This parses the router sources rather than importing the assembled app.
`tests/unit/test_1483_route_order.py` documents what importing `main` costs: it
skipped ALWAYS — standalone included — for months behind a message blaming the
wrong cause. A guard that can silently skip is not a guard. AST analysis has no
import side effects, no env prerequisites, and cannot be shadowed by whatever a
sibling test left in `sys.modules`.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROUTERS = Path(__file__).resolve().parents[2] / "src" / "backend" / "routers"

#: alias -> the path parameter its underlying dependency reads.
DEPENDENCY_PARAM = {
    "OwnedAgent": "name",
    "AuthorizedAgent": "name",
    "OwnedAgentByName": "agent_name",
    "AuthorizedAgentByName": "agent_name",
}

#: The SAME gates attached the other way: `param: str = Depends(get_owned_agent)`.
#: Both forms are in live use (146 alias annotations, 40 Depends defaults), and a
#: wrong-variant swap is exactly as invisible in either. Scanning only the aliases
#: would have left 40 routes — every credential endpoint among them — unguarded
#: while the guard reported success, which is the failure mode this file exists to
#: prevent, one level up.
FUNCTION_PARAM = {
    "get_owned_agent": "name",
    "get_authorized_agent": "name",
    "get_owned_agent_by_name": "agent_name",
    "get_authorized_agent_by_name": "agent_name",
}

_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


def _route_paths(func: ast.AST) -> list[str]:
    """Literal paths from `@router.<method>("...")` decorators on `func`."""
    paths = []
    for dec in getattr(func, "decorator_list", []):
        if not isinstance(dec, ast.Call):
            continue
        f = dec.func
        if not (isinstance(f, ast.Attribute) and f.attr in _HTTP_METHODS):
            continue
        if dec.args and isinstance(dec.args[0], ast.Constant) and isinstance(dec.args[0].value, str):
            paths.append(dec.args[0].value)
    return paths


def _agent_dependencies(func: ast.AST) -> list[tuple[str, str, str]]:
    """`(param_name, label, required_path_param)` for every agent gate on `func`.

    Covers BOTH attachment forms — the Annotated alias (`name: OwnedAgent`) and
    the Depends default (`name: str = Depends(get_owned_agent)`).
    """
    args = func.args
    params = [*args.posonlyargs, *args.args, *args.kwonlyargs]
    # `args.defaults` aligns to the END of posonly+args; kwonly have their own.
    positional = [*args.posonlyargs, *args.args]
    pos_defaults = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)
    defaults = dict(zip(positional, pos_defaults))
    defaults.update(zip(args.kwonlyargs, args.kw_defaults))

    found = []
    for arg in params:
        ann = arg.annotation
        if isinstance(ann, ast.Name) and ann.id in DEPENDENCY_PARAM:
            found.append((arg.arg, ann.id, DEPENDENCY_PARAM[ann.id]))
            continue
        dv = defaults.get(arg)
        if (
            isinstance(dv, ast.Call)
            and getattr(dv.func, "id", None) == "Depends"
            and dv.args
            and getattr(dv.args[0], "id", None) in FUNCTION_PARAM
        ):
            fn = dv.args[0].id
            found.append((arg.arg, f"Depends({fn})", FUNCTION_PARAM[fn]))
    return found


def _collect() -> list[dict]:
    """Every (route, agent-dependency) pairing declared under `routers/`."""
    out = []
    for path in sorted(_ROUTERS.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            deps = _agent_dependencies(node)
            if not deps:
                continue
            for route in _route_paths(node):
                for param_name, label, needed in deps:
                    out.append({
                        "file": path.name,
                        "line": node.lineno,
                        "handler": node.name,
                        "route": route,
                        "param": param_name,
                        "alias": label,
                        "needed": needed,
                    })
    return out


PAIRINGS = _collect()


def test_the_scan_actually_found_the_routes():
    """A guard that silently matches nothing certifies nothing.

    If a refactor moves these dependencies behind a helper, or the routers move
    directory, this fails loudly instead of passing vacuously.
    """
    assert len(PAIRINGS) > 100, (
        f"only {len(PAIRINGS)} route/dependency pairings found under {_ROUTERS} — "
        "the scan is no longer seeing the routers"
    )
    labels = {p["alias"] for p in PAIRINGS}

    # BOTH attachment forms must be represented. If the Depends() family stops
    # appearing, the collector has quietly gone back to seeing only aliases —
    # which is the 40-route blind spot this guard was extended to close, and it
    # would otherwise look like a pass.
    missing_alias = set(DEPENDENCY_PARAM) - labels
    assert not missing_alias, (
        f"alias(es) no longer found in any route: {sorted(missing_alias)} — if one "
        "is genuinely retired, drop it from DEPENDENCY_PARAM deliberately"
    )
    missing_fn = {f"Depends({f})" for f in FUNCTION_PARAM} - labels
    assert not missing_fn, (
        f"Depends() form(s) no longer found: {sorted(missing_fn)} — either they were "
        "retired, or the collector stopped scanning that form (a silent 40-route gap)"
    )


def test_every_agent_dependency_matches_its_route_path_param():
    """The bug, stated as a rule.

    `OwnedAgentByName`/`AuthorizedAgentByName` require `{agent_name}` in the
    route; `OwnedAgent`/`AuthorizedAgent` require `{name}`.
    """
    offenders = []
    for p in PAIRINGS:
        needed = p["needed"]
        if "{%s}" % needed not in p["route"]:
            present = [seg for seg in ("{name}", "{agent_name}") if seg in p["route"]]
            offenders.append(
                f"{p['file']}:{p['line']} {p['handler']}() — route {p['route']!r} "
                f"uses {p['alias']} (reads path param {needed!r}) but declares "
                f"{present or 'no agent path param'}; every request 422s"
            )

    assert not offenders, (
        "agent dependency does not match the route's path param — FastAPI cannot "
        "satisfy it, so the route returns 422 to every caller:\n  "
        + "\n  ".join(offenders)
    )


def test_the_parameter_name_matches_the_dependency_too():
    """The handler's own parameter should be spelled like the path param.

    Not a 422 on its own — FastAPI binds the dependency by annotation, not by
    name — but a `name: OwnedAgentByName` reads as if it were fine and is how
    the mismatch stays invisible while someone is looking straight at it.
    """
    offenders = [
        f"{p['file']}:{p['line']} {p['handler']}() — parameter {p['param']!r} "
        f"annotated {p['alias']} (which reads {p['needed']!r})"
        for p in PAIRINGS
        if p["param"] != p["needed"]
    ]
    assert not offenders, (
        "parameter name disagrees with the dependency's path param:\n  "
        + "\n  ".join(offenders)
    )


def test_the_three_regressed_schedule_routes_specifically():
    """Named-and-shamed check for the exact routes #2081 broke.

    The general rule above already covers these. This is here so the failure
    names the regression rather than appearing as one line in a list, and so
    that deleting the general check does not silently drop coverage of the P0.
    """
    wanted = {
        "/{name}/schedules/{schedule_id}/enable",
        "/{name}/schedules/{schedule_id}/disable",
        "/{name}/schedules/{schedule_id}/trigger",
    }
    seen = {p["route"]: p for p in PAIRINGS if p["route"] in wanted}
    assert set(seen) == wanted, f"routes missing from the scan: {sorted(wanted - set(seen))}"

    for route, p in sorted(seen.items()):
        assert p["alias"] == "OwnedAgent", (
            f"{route} uses {p['alias']}; it declares {{name}}, so the owner-tier "
            "gate that can actually bind is OwnedAgent. #2081's owner-tier intent "
            "is preserved — OwnedAgent is the same owner/admin check, just the "
            "variant that reads the path param this route has."
        )
