"""#2373 — the four post-deploy system endpoints, plus two preview hardenings.

`GET /systems`, `GET /systems/{name}`, `POST /systems/{name}/restart` and
`GET /systems/{name}/manifest` were essentially untouched since 2025 while every
commit since ent#124 hardened the DEPLOY half. Each defect below is one the
existing suite could not see: `tests/test_systems.py` is live-backend tier and
never asserted on the affected keys.
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
_BACKEND = str(_REPO / "src" / "backend")
while _BACKEND in sys.path:
    sys.path.remove(_BACKEND)
sys.path.insert(0, _BACKEND)


def _code_only(src: str) -> str:
    """Source with comment lines removed.

    These assertions are about what the CODE does, and the comments here quote
    the old expressions on purpose so the next reader knows what was wrong — a
    naive substring check matches its own explanation and fails on a correct
    file. (Same trap the ent#155 spec hit with `voiceConvLive`.)
    """
    out = []
    for line in src.split("\n"):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        out.append(line.split("  # ")[0] if "  # " in line else line)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# D1 — the nonexistent DB method
# ---------------------------------------------------------------------------

def test_the_schedules_accessor_the_router_calls_actually_exists():
    """`db.get_agent_schedules` does not exist. `database.py` deliberately has
    no `__getattr__` fallback, and the AttributeError was swallowed by the
    surrounding `except Exception` — so every response omitted `schedules` for
    every agent and logged one warning each, silently, for the life of the
    endpoint."""
    from database import DatabaseManager
    from routers import systems

    src = _code_only(inspect.getsource(systems.get_system))
    assert "db.get_agent_schedules(" not in src, (
        "get_agent_schedules does not exist on the db facade"
    )
    assert "db.list_agent_schedules(" in src
    assert hasattr(DatabaseManager, "list_agent_schedules")
    assert not hasattr(DatabaseManager, "get_agent_schedules")


def test_the_facade_has_no_getattr_fallback_so_a_typo_cannot_be_silent():
    """The property that makes D1 possible, pinned: if a fallback is ever added,
    a mistyped accessor stops raising and starts returning a Mock."""
    from database import DatabaseManager
    assert "__getattr__" not in vars(DatabaseManager)


# ---------------------------------------------------------------------------
# D2 — the ungated mutating verb
# ---------------------------------------------------------------------------

def test_restart_is_creator_gated_like_deploy():
    """It was bare `get_current_user` — below `POST /deploy` and below even the
    read-only bundled-catalog routes — so any authenticated principal could stop
    and start every container in a system whose agents it could see."""
    from routers import systems
    src = _code_only(inspect.getsource(systems.restart_system))
    assert 'require_role("creator")' in src
    assert "Depends(get_current_user)" not in src


def test_restart_is_not_reachable_by_an_agent_principal():
    """`require_role` rejects agent principals since #1890, which matters here
    because an agent-scoped MCP key resolves to its owner CARRYING the owner's
    role — on a default admin-owned install that is every agent."""
    import dependencies
    src = inspect.getsource(dependencies.require_role)
    assert "reject_agent_principal" in src or "agent_name" in src


# ---------------------------------------------------------------------------
# D4 — membership
# ---------------------------------------------------------------------------

def _members(system, names, tags):
    from services import system_service
    from database import db

    real = db.get_tags_for_agents
    db.get_tags_for_agents = lambda ns: {n: tags.get(n, []) for n in ns}
    try:
        return system_service.system_member_names(system, names)
    finally:
        db.get_tags_for_agents = real


def test_an_operation_on_acme_never_touches_an_acme_extra_agent():
    """The AC. `startswith("acme-")` captured `acme-extra-worker`, and
    `restart_system` STOPS AND STARTS what it captures."""
    names = ["acme-web", "acme-db", "acme-extra-worker"]
    tags = {"acme-web": ["acme"], "acme-db": ["acme"],
            "acme-extra-worker": ["acme-extra"]}
    assert sorted(_members("acme", names, tags)) == ["acme-db", "acme-web"]
    assert _members("acme-extra", names, tags) == ["acme-extra-worker"]


def test_the_prefix_fallback_still_excludes_another_systems_tagged_agents():
    """Pre-tag systems fall back to the prefix, but an agent claimed by its own
    system's tag is still excluded — otherwise the fallback re-opens the exact
    collision."""
    names = ["acme-web", "acme-extra-worker"]
    tags = {"acme-extra-worker": ["acme-extra"]}   # `acme` itself predates tags
    assert _members("acme", names, tags) == ["acme-web"]


def test_tags_win_over_the_prefix_so_a_renamed_member_is_still_a_member():
    names = ["acme-web", "totally-different"]
    tags = {"acme-web": ["acme"], "totally-different": ["acme"]}
    assert sorted(_members("acme", names, tags)) == ["acme-web", "totally-different"]


def test_a_tag_read_failure_degrades_to_the_prefix_rather_than_500ing():
    from services import system_service
    from database import db
    real = db.get_tags_for_agents

    def _boom(_names):
        raise RuntimeError("tags table unavailable")

    db.get_tags_for_agents = _boom
    try:
        assert system_service.system_member_names("acme", ["acme-web", "other"]) == ["acme-web"]
    finally:
        db.get_tags_for_agents = real


def test_all_three_endpoints_use_the_one_predicate():
    """Three copies of a wrong rule is what made this a bug in three places, and
    the same predicate is the prerequisite for the teardown verb."""
    from routers import systems
    src = _code_only(inspect.getsource(systems))
    assert 'startswith(f"{system_name}-")' not in src
    assert src.count("system_member_names(") == 3


# ---------------------------------------------------------------------------
# D3 — export round-trip
# ---------------------------------------------------------------------------

def test_export_does_not_embed_the_instance_global_prompt():
    """Deploying an exported manifest on another instance overwrote THAT
    instance's platform-wide prompt. Nothing records whether the source system
    ever set one, so the only honest export of an unknown is to omit it."""
    from services import system_service
    src = _code_only(inspect.getsource(system_service.export_manifest))
    assert 'manifest_dict["prompt"]' not in src
    assert 'get_setting_value("trinity_prompt")' not in src


def test_export_scopes_permission_edges_to_members_in_both_branches():
    """The non-full-mesh branch had no membership filter at all, so an edge
    pointing outside the system was blind-sliced into a garbage short name that
    then failed `validate_manifest` on re-deploy — the export broke its own
    round trip."""
    from services import system_service
    src = _code_only(inspect.getsource(system_service.export_manifest))
    slices = src.count('p["target_agent"][len(system_name) + 1:]')
    guarded = src.count('if p["target_agent"] in')
    assert slices == guarded == 2, (
        "every target slice must be guarded by membership"
    )
    assert 'startswith(f"{system_name}-")' not in src


# ---------------------------------------------------------------------------
# H5 / H6 — the preview hardenings
# ---------------------------------------------------------------------------

def test_unknown_per_agent_keys_surface_as_warnings():
    """`credentials:`, `skills:`, `display_label:` are the fields people try
    first and they vanished in silence, while a top-level typo already warned."""
    from services.system_service import parse_manifest, validate_manifest

    manifest = parse_manifest(
        "name: acme\n"
        "agents:\n"
        "  web:\n"
        "    template: local:starter\n"
        "    credentials: [OPENAI_API_KEY]\n"
        "    display_label: Web\n"
    )
    assert manifest.unknown_agent_keys == {"web": ["credentials", "display_label"]}

    warnings = validate_manifest(manifest)
    joined = " ".join(warnings)
    assert "web" in joined and "credentials" in joined and "display_label" in joined


def test_a_fully_recognised_manifest_warns_about_nothing_per_agent():
    from services.system_service import parse_manifest
    manifest = parse_manifest(
        "name: acme\nagents:\n  web:\n    template: local:starter\n    tags: [x]\n"
    )
    assert manifest.unknown_agent_keys == {}


def test_preview_and_deploy_resolve_the_same_resource_default():
    """`_preflight_template` validated against the admin-configurable default
    while deploy hardcoded `{"cpu": "2", "memory": "4g"}` — so they disagreed the
    moment an admin moved the fleet default."""
    from services import system_service
    deploy_src = _code_only(inspect.getsource(system_service.deploy_manifest))
    assert '{"cpu": "2", "memory": "4g"}' not in deploy_src
    assert "_manifest_default_resources()" in deploy_src

    from services.agent_service import crud
    assert "get_agent_default_resources" in inspect.getsource(crud._get_default_resource)
