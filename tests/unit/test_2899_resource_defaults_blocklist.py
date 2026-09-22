"""The fleet-wide agent resource defaults are not writable through the generic
settings catch-all (#2899).

`PUT /api/settings/agent-defaults/resources` validates `cpu`/`memory` against
`VALID_CPU`/`VALID_MEMORY` — the same sets the container-create path enforces.
The generic `PUT /api/settings/{key}` takes `Dict[str, str]` and validates
nothing, so the same two keys were writable by a second, unvalidated door: the
#506 / #1609 / ent#435 shape, one more time.

This is not a cosmetic setting. `crud._get_default_resource` reads it at create
time as the fallback for every agent whose template and caller declare no
`resources` — which, since #2899 re-graded T-004/T-005, is the SUPPORTED shape
and what all three bundled starters use. A junk value there makes
`normalize_cpu` raise and every such creation 400, fleet-wide, until someone
finds the row.

Read the SOURCE rather than importing it (learnings.md): `routers/settings/`
pulls `routers/__init__.py`, which imports every router in the app.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = REPO_ROOT / "src" / "backend"
_GENERIC = _BACKEND / "routers" / "settings" / "generic.py"

# The symbols the guard must name. Matched as SYMBOLS, not as the literal
# strings they hold: the router imports them from `settings_service`, which is
# the right choice (no mirror to drift), and a literal grep would fail on
# correct code.
_GUARDED_SYMBOLS = {"AGENT_DEFAULT_CPU_KEY", "AGENT_DEFAULT_MEMORY_KEY"}


def _update_setting_handler() -> ast.AST:
    tree = ast.parse(_GENERIC.read_text(encoding="utf-8"))
    handler = next(
        (
            n
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name == "update_setting"
        ),
        None,
    )
    assert handler is not None, (
        "the generic PUT /api/settings/{key} handler was renamed — re-point this "
        "test, do not delete it (#2899)"
    )
    return handler


def test_catch_all_refuses_the_agent_resource_default_keys():
    handler = _update_setting_handler()

    named = {
        node.id
        for node in ast.walk(handler)
        if isinstance(node, ast.Name) and node.id in _GUARDED_SYMBOLS
    }
    missing = sorted(_GUARDED_SYMBOLS - named)
    assert not missing, (
        f"PUT /api/settings/{{key}} no longer mentions {missing}. Those defaults "
        "must go through PUT /api/settings/agent-defaults/resources, which "
        "validates them; written unvalidated here, a junk value stops every "
        "agent creation that relies on the fleet default (#2899)."
    )

    # The mention has to be a refusal, not a log line: the guarded comparison
    # must sit in an `if` whose body raises.
    refusing = [
        node
        for node in ast.walk(handler)
        if isinstance(node, ast.If)
        and {
            n.id
            for n in ast.walk(node.test)
            if isinstance(n, ast.Name) and n.id in _GUARDED_SYMBOLS
        } == _GUARDED_SYMBOLS
        and any(isinstance(stmt, ast.Raise) for stmt in ast.walk(node))
    ]
    assert refusing, (
        "the agent resource-default keys are named in the catch-all but not in a "
        "branch that raises — they are still writable there (#2899)"
    )


def test_dedicated_route_validates_against_the_create_path_value_sets():
    """The blocklist above is only worth having if the door it points at is the
    validated one. `agent_defaults.py` must derive its accepted values from
    `capabilities.VALID_CPU` / `VALID_MEMORY` rather than re-listing them —
    otherwise Settings and container creation drift, which is the same defect
    T-004/T-005 had on the template side (#2899)."""
    source = (_BACKEND / "routers" / "settings" / "agent_defaults.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and (node.module or "").endswith("agent_service.capabilities")
        for alias in node.names
    }
    assert {"VALID_CPU", "VALID_MEMORY"} <= imported, (
        "routers/settings/agent_defaults.py no longer imports VALID_CPU/VALID_MEMORY "
        "from services.agent_service.capabilities — the admin default can now name a "
        "value container creation rejects (#2899)"
    )
