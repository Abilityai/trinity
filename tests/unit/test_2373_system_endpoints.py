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


def test_require_role_does_NOT_reject_agent_principals():
    """The false premise this test used to rest on, pinned as its opposite.

    The earlier version asserted `"reject_agent_principal" in
    inspect.getsource(require_role)` — and passed, because `getsource` returns
    the DOCSTRING, which contains the sentence *"Deliberately does NOT call
    `reject_agent_principal`"*. The assertion matched its own refutation. Same
    trap `_code_only()` above exists for, one file over.

    `require_role` rejects CONNECTOR principals only, and that omission is
    deliberate: `require_role("creator")` on `POST /api/agents` is what makes
    ent#69 Part 2 agent-spawned creation work. Do not "fix" it.
    """
    import dependencies
    body = _code_only(inspect.getsource(dependencies.require_role))
    assert "_reject_connector_principal(current_user)" in body
    assert "reject_agent_principal(current_user)" not in body, (
        "require_role must not reject agent principals — ent#69 Part 2 "
        "agent-spawned creation goes through require_role('creator')"
    )


def test_restart_refuses_an_agent_principal_before_touching_anything():
    """Behavioural, not textual (#2373 review).

    The gate is `reject_agent_principal` AT THE ENDPOINT, because
    `require_role` does not do it and an agent key resolves to its owner
    carrying the owner's role — so `require_role("creator")` alone admits every
    agent on a default admin-owned install.

    It is a BYPASS being closed, not just a wider gate: `POST /agents/{name}/`
    `start`/`stop`/`delete` each call `enforce_agent_spawn_scope`, so an agent
    may only start or stop what it SPAWNED, while this route loops every member
    calling `container_stop` + `start_agent_internal` with no per-member check.

    Driven for real, and asserted to raise BEFORE any work: the stubs below
    would blow up loudly if the refusal came late.
    """
    import asyncio
    from fastapi import HTTPException
    from models import User
    from routers import systems

    agent_caller = User(
        id=1, username="owner", role="admin",
        mcp_scope="agent", agent_name="scribe",
    )

    def _explode(*a, **k):  # pragma: no cover - must not run
        raise AssertionError("restart_system did work before refusing an agent key")

    import routers.agents as agents_mod
    real_accessible = agents_mod.get_accessible_agents
    agents_mod.get_accessible_agents = _explode
    try:
        with pytest.raises(HTTPException) as e:
            asyncio.run(systems.restart_system(
                system_name="acme", request=None, current_user=agent_caller,
            ))
        assert e.value.status_code == 403
    finally:
        agents_mod.get_accessible_agents = real_accessible


def test_restart_still_admits_a_human_creator():
    """The refusal must be about the PRINCIPAL KIND, not about the role — a
    human creator is exactly who this route is for, and a guard that refused
    them too would pass the test above while breaking the feature."""
    from models import User
    from routers import systems

    human = User(id=1, username="op", role="creator", mcp_scope=None)
    from dependencies import reject_agent_principal
    reject_agent_principal(human)   # must not raise

    body = _code_only(inspect.getsource(systems.restart_system))
    assert "reject_agent_principal(current_user)" in body
    assert 'require_role("creator")' in body


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
    # Review pass 3: this was a SOURCE COUNT (`slices == guarded == 2`) and it
    # passed while the bug was live — the guard it counted filters membership,
    # which since this change ADMITS tag-only members whose names carry no
    # prefix at all. Replaced with an assertion about the property: no blind
    # length-slice survives anywhere in the exporter.
    from services import system_service
    src = _code_only(inspect.getsource(system_service.export_manifest))
    assert "[len(system_name) + 1:]" not in src, (
        "export_manifest slices a name by prefix length without checking the "
        "prefix is present — `helper` under system `acme` becomes `r`, and two "
        "tag-only members under `content-production` both become '' and collide "
        "on one manifest key (C1)"
    )
    assert src.count("_member_short_name(") >= 4, (
        "both permission loops must resolve BOTH sides of each edge through the "
        "prefix-safe helper"
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

# ---------------------------------------------------------------------------
# Review blocker: the schedules fix must not leak the webhook credential
# ---------------------------------------------------------------------------
def test_the_system_detail_never_ships_a_webhook_token():
    """`webhook_token` is the bearer for the UNAUTHENTICATED `POST /api/webhooks/{token}`.

    Fixing the `schedules` key turned a three-year no-op into a disclosure: the
    accessor returns whole `db_models.Schedule` rows and the route declares no
    `response_model`, so FastAPI serialized every column to any chat-level
    `role: user` the system's agents are shared with. `db_models.py` says
    outright that neither webhook field is ever surfaced in a response model,
    and `ScheduleResponse` omits all four — which is why the projection is to
    THAT model rather than a hand-picked dict that can drift from it.
    """
    import inspect
    from routers import systems

    body = _code_only(inspect.getsource(systems.get_system))
    assert "list_agent_schedules" in body
    assert "ScheduleResponse" in body, (
        "project the rows; a bare accessor result carries webhook_token"
    )


def test_the_projection_drops_every_webhook_field():
    """Asserted against the MODEL, not a list I maintain here — a fifth webhook
    field added to `Schedule` tomorrow is covered without anyone remembering."""
    from db_models import Schedule
    from models import ScheduleResponse

    leaky = {f for f in Schedule.model_fields if "webhook" in f}
    assert leaky, "premise check: Schedule is supposed to carry webhook fields"
    exposed = set(ScheduleResponse.model_fields)
    assert not (leaky & exposed), f"ScheduleResponse exposes {sorted(leaky & exposed)}"

# ---------------------------------------------------------------------------
# Review blockers 2 & 3 — membership must not drop or over-capture
# ---------------------------------------------------------------------------
def test_partial_tagging_does_not_drop_the_untagged_members(monkeypatch):
    """`if tagged: return tagged` made one tag hide every other member.

    Reachable three ways, none exotic: `PUT /api/agents/{n}/tags` is a FULL-SET
    replacement, an agent added after deploy is untagged, and `configure_tags`
    sits in a try/except so a mid-loop raise still reports "deployed".
    `restart_system` then restarts a subset and reports success, and
    `export_manifest` writes an incomplete backup and calls it one.
    """
    from services import system_service as svc

    monkeypatch.setattr(svc.db, "get_tags_for_agents",
                        lambda names: {"acme-worker": ["acme"]}, raising=False)
    out = svc.system_member_names("acme", ["acme-web", "acme-db", "acme-worker"])
    assert sorted(out) == ["acme-db", "acme-web", "acme-worker"]


def test_a_tag_still_wins_for_a_member_without_the_prefix(monkeypatch):
    """The union must not cost the property the tag half exists for."""
    from services import system_service as svc

    monkeypatch.setattr(svc.db, "get_tags_for_agents",
                        lambda names: {"helper": ["acme"]}, raising=False)
    out = svc.system_member_names("acme", ["acme-web", "helper", "other"])
    assert sorted(out) == ["acme-web", "helper"]


def test_a_tag_read_failure_never_returns_FEWER_members_than_the_prefix(monkeypatch):
    """The invariant that stops a third narrowing rule being invented here.

    #2373 is about over-capture on the fallback path, and two attempts to fix
    that by narrowing both produced UNDER-capture on a healthy system — dropping
    11 of 11 `dd-*` members, dropping a sibling-prefixed member, and in the
    sharpest case returning `[]` because one agent was named after the system.

    Both errors are real, but they are not symmetric. `restart_system` and
    `export_manifest` share this predicate: over-capture restarts one agent too
    many and says so in a WARNING, while under-capture restarts a SUBSET and
    reports success, or writes a short backup and calls it complete — the silent
    partial success the union rule exists to prevent.

    So the fallback is pinned as a SUPERSET of the raw prefix, property-style
    rather than case-by-case. Any future rule that narrows here fails this test
    by construction, whatever shape it narrows on. The real narrowing lives on
    the tag-READABLE path, where the evidence exists — see
    `test_the_prefix_fallback_still_excludes_another_systems_tagged_agents`.
    """
    from services import system_service as svc

    def _boom(names):
        raise RuntimeError("tags table unavailable")

    monkeypatch.setattr(svc.db, "get_tags_for_agents", _boom, raising=False)

    rosters = [
        ["acme-web", "acme-extra", "acme-extra-worker"],
        ["acme-api", "acme-api-worker", "acme-web"],
        ["acme", "acme-web", "acme-db"],
        ["acme-web-1", "acme-db", "other"],
        [f"acme-dd-{x}" for x in ("lead", "tech", "market")],
    ]
    for roster in rosters:
        raw = [n for n in roster if n.startswith("acme-")]
        got = svc.system_member_names("acme", roster)
        assert got == raw, (
            f"the unreadable-tag fallback is not the raw prefix for {roster}: "
            f"got {got}, prefix gives {raw}"
        )


# ---------------------------------------------------------------------------
# Review blocker 4 — the export name slice on a tag-only member
# ---------------------------------------------------------------------------
def test_a_member_without_the_prefix_is_skipped_from_the_export_not_mangled(monkeypatch):
    """`full_name[len(system_name)+1:]` assumes every member carries the prefix.

    Tag-first membership broke that assumption, and this PR's own
    `test_tags_win_over_the_prefix_...` asserts such members ARE members. So
    `content-production` + `bot` and `assistant` both sliced to `''`, which
    COLLIDES as a dict key — one agent silently overwrites the other — and `''`
    fails `validate_manifest`'s name regex, so the export cannot re-deploy. The
    same "export broke its own round trip" class this PR exists to fix.

    Review finding: the first version of this test was a source grep that
    matched a leftover variable declaration and survived deleting the skip. It
    drives the real function now.
    """
    import yaml
    from services import system_service as svc

    monkeypatch.setattr(svc.db, "get_tags_for_agents",
                        lambda names: {"bot": ["content-production"],
                                       "assistant": ["content-production"]},
                        raising=False)
    monkeypatch.setattr(svc.db, "get_agent_permissions", lambda n: [], raising=False)
    monkeypatch.setattr(svc.db, "list_agent_schedules", lambda n: [], raising=False)

    agents = [
        {"name": "content-production-writer", "template": "local:scribe"},
        {"name": "bot", "template": "local:scout"},
        {"name": "assistant", "template": "local:sage"},
    ]
    out = yaml.safe_load(svc.export_manifest("content-production", agents))
    keys = set((out.get("agents") or {}).keys())

    assert "writer" in keys, "the prefixed member must still export"
    assert "" not in keys, "an empty agent key collides and fails validate_manifest"
    assert len(keys) == 1, (
        f"tag-only members must be omitted, not mangled into colliding keys; got {keys}"
    )


# ---------------------------------------------------------------------------
# Review pass 3 — C1 and C2, both driven through the real functions
# ---------------------------------------------------------------------------

class _RaisingDb:
    """Every attribute is a callable that raises — forces the degraded path."""

    def __getattr__(self, name):
        def _raise(*a, **k):
            raise RuntimeError("tag read failed")
        return _raise


def test_a_tag_read_failure_keeps_a_hyphenated_short_name(monkeypatch):
    """C2 — the degraded path must degrade to the PREFIX, not below it.

    The previous rule excluded any member whose short name contained a hyphen.
    The bundled flagship manifest is entirely inside that gap:
    `config/manifests/vc-due-diligence.yaml` names all eleven agents `dd-*`, so
    every deployed name is `vc-due-diligence-dd-<x>` and every remainder has a
    hyphen. A transient tag-read error therefore turned a healthy, correctly
    tagged fleet into `404 System not found` on `GET /api/systems/{name}` and an
    EMPTY export — strictly worse than the raw prefix it claimed to fall back to.
    """
    import services.system_service as svc

    monkeypatch.setattr(svc, "db", _RaisingDb(), raising=False)
    roster = [
        f"vc-due-diligence-dd-{x}" for x in
        ("lead", "intake", "tech", "market", "legal", "finance",
         "refs", "memo", "risk", "ops", "synth")
    ]
    got = svc.system_member_names("vc-due-diligence", roster)
    assert len(got) == 11, (
        f"a tag-read failure dropped {11 - len(got)} of 11 members of the bundled "
        f"vc-due-diligence system: {got}"
    )


def test_the_degraded_path_never_drops_a_member_of_its_OWN_system(monkeypatch):
    """Review pass 4 — the roster-evidence rule was the same bug, one door along.

    Two narrowing rules were tried on the unreadable-tag path and BOTH lost
    members of a healthy system:

      1. `"-" in short_name` dropped 11 of 11 `vc-due-diligence-dd-*` agents.
      2. roster evidence (`any(other != name and name.startswith(other + "-"))`)
         drops `acme-api-worker` — an ordinary member of `acme` whose manifest
         key is `api-worker`, sitting beside key `api` — because a SIBLING
         MEMBER happens to be a name-prefix of it.

    Both directions matter here because `restart_system` shares this predicate:
    a dropped member means a subset restarted and reported as success, the
    silent partial success the union rule exists to prevent.
    """
    import services.system_service as svc

    monkeypatch.setattr(svc, "db", _RaisingDb(), raising=False)

    got = svc.system_member_names("acme", ["acme-api", "acme-api-worker", "acme-web"])
    assert got == ["acme-api", "acme-api-worker", "acme-web"], (
        f"a sibling member acting as a name-prefix dropped a real member: {got}"
    )

    got = svc.system_member_names(
        "vc-dd", ["vc-dd-dd", "vc-dd-dd-lead", "vc-dd-dd-tech"])
    assert len(got) == 3, f"2 of 3 members dropped: {got}"


def test_an_agent_named_after_the_system_does_not_erase_the_system(monkeypatch):
    """The sharpest form of the same rule: `acme` is a prefix of EVERY member.

    An agent named exactly `acme` anywhere on the accessible roster made the
    roster-evidence rule exclude every `acme-*` agent, so `system_member_names`
    returned `[]` — `404 System not found` on `GET /api/systems/acme` and an
    empty export, for a healthy system, from one unrelated agent's name.
    """
    import services.system_service as svc

    monkeypatch.setattr(svc, "db", _RaisingDb(), raising=False)
    got = svc.system_member_names(
        "acme", ["acme", "acme-web", "acme-db", "acme-worker"])
    assert "acme-web" in got and "acme-db" in got and "acme-worker" in got, got


def test_the_documented_residual_is_the_raw_prefix(monkeypatch):
    """And here is the cost, stated rather than hidden.

    With tags unreadable, `acme` DOES capture `acme-extra-worker`. Nothing in
    the names can separate `worker` of `acme-extra` from `extra-worker` of
    `acme`; only a tag can, and the tag read is what just failed. This is the
    pre-#2373 behaviour on an error path, and it ends when the read recovers —
    deliberately preferred over the two rules above, each of which lost members
    of a healthy system.

    While the tags ARE readable the narrowing still applies — pinned by
    `test_membership_excludes_a_longer_sibling_system` above, which is the path
    that actually runs.
    """
    import services.system_service as svc

    monkeypatch.setattr(svc, "db", _RaisingDb(), raising=False)
    got = svc.system_member_names("acme", ["acme-web", "acme-extra", "acme-extra-worker"])
    assert got == ["acme-web", "acme-extra", "acme-extra-worker"], got


def test_no_evidence_means_the_raw_prefix(monkeypatch):
    """With no sibling on the roster the fallback is the documented behaviour."""
    import services.system_service as svc

    monkeypatch.setattr(svc, "db", _RaisingDb(), raising=False)
    got = svc.system_member_names("acme", ["acme-web-1", "acme-db"])
    assert got == ["acme-web-1", "acme-db"], got


@pytest.mark.parametrize("full,system,expected", [
    ("acme-web", "acme", "web"),
    ("content-production-bot", "content-production", "bot"),
    ("helper", "acme", None),                    # tag-only member: no key
    ("bot", "content-production", None),          # would slice to 'ntent-production-bot'[?]
    ("acme", "acme", None),                       # would slice to ''
])
def test_a_member_short_name_is_prefix_safe(full, system, expected):
    """C1 — a manifest key exists only for a member carrying the prefix.

    Membership now admits TAG-ONLY members, whose names carry no prefix at all,
    so the blind `name[len(system)+1:]` slice in both permission loops produced
    garbage: `helper` under `acme` sliced to `r`, and two tag-only members under
    `content-production` both sliced to `''` — one colliding dict key. Either
    way the export failed its own `validate_manifest` on re-deploy: the round
    trip broken by the export, not the import.
    """
    from services.system_service import _member_short_name

    assert _member_short_name(full, system) == expected


def test_neither_permission_loop_blind_slices():
    """C1 — the fix must be in BOTH branches, which is how it was missed once.

    The `agent_configs` loop got the prefix skip; the two permission loops did
    not. Asserted structurally because both branches sit behind a template
    lookup that a unit test cannot easily reach, and a source count is what
    previously passed while the bug was live — so this asserts the ABSENCE of
    the blind slice rather than a count of guards.
    """
    import inspect
    import services.system_service as svc

    src = inspect.getsource(svc.export_manifest)
    assert "[len(system_name) + 1:]" not in src, (
        "export_manifest still slices a name by prefix length without checking "
        "the prefix is there — use _member_short_name (C1)."
    )
    assert src.count("_member_short_name(") >= 4, (
        "both permission loops must use the prefix-safe helper on BOTH sides of "
        "each edge (source and target)"
    )
