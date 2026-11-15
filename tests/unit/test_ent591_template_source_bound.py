"""trinity-enterprise#591 — the template-source bound for agent-created agents.

One instance-wide allowlist (`system_settings['agent_creation_allowlist']`).
Empty = unbounded. While it has entries, a machine principal outside
{JWT, user key, system key} may create an agent only from a listed source:
an `agents.manage` holder creating a durable agent and any agent spawning a
ghost alike. Uploads (deploy-local) and blank creates are refused while the list
is set. Every check runs before any side effect of the create it guards.

Rulings (2026-11-06): one list on Settings → Agents; entries `local:<name>`
(bundled only), `github:<owner>/*`, `github:<owner>/<repo>[@<ref>]`; a system
deploy with a refused member refuses as a whole and its dry run predicts it;
agents read the bound on `GET /api/templates` and in the refusal; no new MCP tool.

Each call-site test reaches the line that applies the bound: the faked
collaborator behind it must stay uncalled on a refusal and be called on a pass.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

pytestmark = pytest.mark.unit

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _tsp():
    """The leaf, as currently registered (the crud harness rebuilds `services.*`)."""
    return importlib.import_module("services.template_source_policy")


def _principal(scope="agent", agent="orchestrator", **kw):
    base = dict(id=1, username="owner", email="owner@example.com", role="admin",
                mcp_scope=scope, agent_name=agent if scope == "agent" else None,
                connector_agent=None, mcp_key_id="key-1" if scope else None,
                mcp_key_name="orchestrator-key" if scope else None)
    base.update(kw)
    return SimpleNamespace(**base)


PERSON = dict(scope=None, agent=None)


def _request(path="/api/agents", method="POST"):
    return SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), method=method,
                           scope={"path": path}, state=SimpleNamespace(request_id="r1"),
                           path_params={})


def _config(template="local:scout", name="new-one", **kw):
    base = dict(template=template, name=name, ephemeral=None, source_branch="main",
                fork_to_own=None, import_intent=None)
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def bundled(monkeypatch, tmp_path):
    """A bundled catalog holding `scout` and `writer`, and `quiet`, which is
    `hidden: true` (creatable by id, absent from the template list)."""
    for name in ("scout", "writer"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "template.yaml").write_text("name: x\n")
    (tmp_path / "quiet").mkdir()
    (tmp_path / "quiet" / "template.yaml").write_text("name: quiet\nhidden: true\n")
    monkeypatch.setattr(_tsp(), "_bundled_root", lambda: tmp_path)
    return tmp_path


@pytest.fixture
def allowlist(monkeypatch):
    """Answer the setting row from a value the test owns (None = no row)."""
    from database import db
    state = {"raw": None}
    real = db.get_setting_value

    def fake(key, default=None):
        if key == "agent_creation_allowlist":
            return state["raw"] if state["raw"] is not None else default
        return real(key, default)

    monkeypatch.setattr(db, "get_setting_value", fake)

    def set_(value):
        state["raw"] = value if (value is None or isinstance(value, str)) else json.dumps(value)
    return set_


@pytest.fixture
def audit(monkeypatch):
    mod = importlib.import_module("services.platform_audit_service")
    log = AsyncMock(return_value="evt")
    monkeypatch.setattr(mod.platform_audit_service, "log", log)
    return log


def _refusal_rows(audit):
    return [c for c in audit.await_args_list if c.kwargs.get("event_action") == "template_source_refused"]


# ===========================================================================
# 1. The entry grammar
# ===========================================================================

@pytest.mark.parametrize("raw,canonical", [
    ("local:scout", "local:scout"),
    ("  local:Scout.v2  ", "local:Scout.v2"),           # trimmed; local names stay exact
    ("github:Acme/Tools", "github:acme/tools"),         # owner/repo lowercased
    ("github:ACME/*", "github:acme/*"),
    ("github:acme/tools@v2", "github:acme/tools@v2"),
    ("github:acme/tools@Release/1.0", "github:acme/tools@Release/1.0"),  # refs exact
])
def test_an_accepted_entry_is_normalised(raw, canonical):
    entry, reason = _tsp().parse_entry(raw)
    assert reason is None and entry.text == canonical


@pytest.mark.parametrize("raw,needle", [
    ("", "Empty entry"),
    ("   ", "Empty entry"),
    ("*", "bare *"),
    ("local:*", "local:* is not allowed"),
    ("local:../etc", "local:<template>"),
    ("local:.hidden", "local:<template>"),
    ("github:acme", "github:<owner>/<repo>"),
    ("github:acme/a/b", "github:<owner>/<repo>"),
    ("github:*", "github:<owner>/<repo>"),
    ("github:acme/*@v2", "takes no @ref"),
    ("github:acme/tools@", "Nothing after '@'"),
    ("github:acme/tools@-bad", "not a valid branch or tag"),
    ("github:ac me/tools", "owner may use"),
    ("gitlab:acme/tools", "Start with local: or github:"),
    ("local:" + "a" * 200, "Longer than 200"),
    (5, "must be text"),
])
def test_a_refused_entry_names_the_fix(raw, needle):
    entry, reason = _tsp().parse_entry(raw)
    assert entry is None and needle in reason


def test_duplicates_are_dropped_and_errors_carry_their_index():
    entries, errors = _tsp().parse_entries(["github:acme/*", "GitHub:x", "github:ACME/*", "local:scout"])
    assert [e.text for e in entries] == ["github:acme/*", "local:scout"]
    assert errors == [{"index": 1, "entry": "GitHub:x", "reason": errors[0]["reason"]}]


def test_more_than_the_cap_is_refused_whole():
    tsp = _tsp()
    entries, errors = tsp.parse_entries([f"local:t{i}" for i in range(tsp.MAX_ENTRIES + 1)])
    assert entries == [] and errors[0]["index"] is None and "At most" in errors[0]["reason"]


# ===========================================================================
# 2. Matching a create's source
# ===========================================================================

def _allowed(entries, template, **kw):
    tsp = _tsp()
    parsed, errors = tsp.parse_entries(entries)
    assert not errors
    source = tsp.source_of(template)
    return tsp.allows(parsed, source, tsp.effective_ref(source, **kw))


@pytest.mark.parametrize("entries,template,kw,expected", [
    (["local:scout"], "local:scout", {}, True),
    (["local:scout"], "local:Scout", {}, False),                       # local names exact
    (["local:ghost-tpl"], "local:ghost-tpl", {}, False),               # listed, not bundled
    (["github:acme/tools"], "github:Acme/Tools", {}, True),            # case-insensitive
    (["github:acme/tools"], "github:acme/tools@any-branch", {}, True),  # unpinned: every ref
    (["github:acme/*"], "github:ACME/whatever@x", {}, True),
    (["github:acme/*"], "github:acme-evil/tools", {}, False),
    (["github:acme/tools@v2"], "github:acme/tools@v2", {}, True),
    (["github:acme/tools@v2"], "github:acme/tools@trinity/me/abc", {}, False),
    (["github:acme/tools@v2"], "github:acme/tools", {}, False),        # effective ref = main
    (["github:acme/tools@v2"], "github:acme/tools", {"source_branch": "v2"}, True),
    (["github:acme/tools@v2"], "github:acme/tools", {"source_branch": "evil"}, False),
    (["github:acme/tools@main"], "github:acme/tools", {"fork": True}, False),   # default branch unknown
    (["github:acme/tools"], "github:acme/tools", {"fork": True}, True),
    (["github:acme/tools@main"], "github:acme/tools", {"copy": True}, False),
    (["github:acme/tools@main"], "github:acme/tools@main", {"copy": True}, False),  # copy of main = default branch
    (["github:acme/tools@v2"], "github:acme/tools@v2", {"copy": True}, True),       # copy of a named ref = that ref
    (["github:acme/tools@v2"], "github:acme/tools@v2", {"source_mode": False}, False),  # working branch = default branch
    (["github:acme/tools"], "github:acme/tools@v2", {"source_mode": False}, True),     # unpinned still admits it
    (["github:acme/tools", "local:scout"], None, {}, False),           # blank
    (["github:acme/tools"], "github:acme", {}, False),                 # unrecognised
    (["github:acme/tools"], "github:acme/tools\n", {}, False),         # fullmatch, never match
    (["local:scout"], "scout", {}, False),
])
def test_the_matcher(bundled, entries, template, kw, expected):
    assert _allowed(entries, template, **kw) is expected


TRICKY = [
    "github:Acme/Tools", "github:acme/tools@feature/x", "github:acme/tools@",
    "github:acme/tools@a@b", "github:acme/tools@..", "github:acme/to ols",
    "github:acme/tools/extra", "github:a.b/c_d-e", "github:acme/tools@v1.2",
]


@pytest.mark.parametrize("template", TRICKY)
def test_a_recognised_source_parses_to_crud_s_repo_and_ref(template):
    """One-way parity with crud's `_parse_github_ref`: whatever the leaf
    recognises, crud parses to the same repo and ref. (Where crud is laxer — a
    slashless catalog id — the leaf refuses: fail closed.) Parsing only: which
    ref the build then CLONES depends on the mode, pinned by the
    working-branch, copy and pin-forcing tests below."""
    from models import AgentConfig
    from services.agent_service import crud
    source = _tsp().source_of(template)
    if source.kind != "github":
        return
    config = AgentConfig(name="x", template=template)
    _, repo_path, url_branch = crud._parse_github_ref(config)
    assert (source.owner, source.repo) == tuple(repo_path.lower().split("/"))
    assert source.ref == url_branch


def test_the_mirrored_grammar_matches_crud_and_the_scope_allowlist():
    from services.agent_service import crud
    import dependencies
    tsp = _tsp()
    assert crud._LOCAL_TEMPLATE_NAME_RE.pattern == f"^{tsp._LOCAL_NAME_RE.pattern}\\Z"
    part = tsp._GH_PART_RE.pattern
    assert crud._GITHUB_REPO_PATH_RE.pattern == f"^{part}/{part}$"
    assert crud._GITHUB_REF_RE.pattern == f"^{tsp._GH_REF_RE.pattern}$"
    assert tsp.UNBOUNDED_SCOPES == dependencies.ADMIN_GATE_SCOPES


# ===========================================================================
# 3. Who is bounded, and the stored row
# ===========================================================================

class _NoScope:
    id, username, email, role, agent_name = 1, "owner", "o@example.com", "admin", None


@pytest.mark.parametrize("principal,bounded", [
    (_principal("agent"), True),
    (_principal(None), False),           # a person's JWT
    (_principal("user"), False),         # a person's own key
    (_principal("system"), False),       # trinity-system
    (_principal("connector"), True),
    (_principal("ops"), True),
    (_principal("portal_delegate"), True),
    (_principal("a-scope-no-one-has-shipped"), True),
    (_NoScope(), True),                  # absent attribute fails closed (#2323)
])
def test_only_people_and_the_system_key_are_never_bounded(bundled, allowlist, principal, bounded):
    allowlist(["local:writer"])
    refusal = _tsp().refusal_for(principal, _config("local:scout"))
    assert (refusal is not None) is bounded


def test_an_empty_or_missing_list_is_unbounded(bundled, allowlist):
    tsp = _tsp()
    allowlist(None)
    assert tsp.refusal_for(_principal(), _config("github:anyone/anything")) is None
    allowlist("[]")
    assert tsp.refusal_for(_principal(), _config(None)) is None
    assert tsp.refusal_for(_principal(), None, upload=True) is None


@pytest.mark.parametrize("raw,bounded,invalid,entries", [
    (None, False, False, []),
    ("[]", False, False, []),
    ("not json", True, True, []),
    ('{"local:scout": 1}', True, True, []),
    ('["local:scout", "bad"]', True, True, ["local:scout"]),
    ('["bad"]', True, True, []),
    ('["local:scout"]', True, False, ["local:scout"]),
])
def test_a_damaged_row_fails_closed_and_says_so(allowlist, raw, bounded, invalid, entries):
    allowlist(raw)
    stored = _tsp().read_stored()
    assert (stored.bounded, stored.invalid, stored.texts) == (bounded, invalid, entries)


def test_the_refusal_names_the_canonical_source_and_the_list(bundled, allowlist):
    allowlist(["local:scout", "github:abilityai/*"])
    tsp = _tsp()
    r = tsp.refusal_for(_principal(), _config("github:Acme/Tools@trinity/me/x"))
    detail = r.detail()
    assert detail["code"] == "template_source_not_allowed"
    assert detail["template"] == "github:acme/tools@trinity/me/x"
    assert detail["allowed_sources"] == ["local:scout", "github:abilityai/*"]
    assert detail["message"] == (
        "This instance limits which templates an agent may create agents from, and "
        "github:acme/tools@trinity/me/x is not on the list. Create the agent from one of "
        "allowed_sources instead, or ask a person to create it: people are not limited by "
        "this list. Only an instance admin can change the list (Settings → Agents → Template "
        "sources for agent-created agents).")
    blank = tsp.refusal_for(_principal(), _config(None)).detail()
    assert blank["template"] is None and "a blank agent (no template) is not allowed" in blank["message"]
    garbage = tsp.refusal_for(_principal(), _config("github:x\nINJECT")).detail()
    assert garbage["template"] is None and "INJECT" not in garbage["message"]
    up = tsp.refusal_for(_principal(), None, upload=True).detail()
    assert up["code"] == "template_upload_not_allowed" and "template" not in up
    assert up["message"] == (
        "This instance limits which templates an agent may create agents from, so an agent "
        "cannot deploy an uploaded archive: an upload is never on the list. Create the agent "
        "from one of allowed_sources, or ask a person to deploy it.")


# ===========================================================================
# 4. POST /api/agents — before the idempotency claim and the create
# ===========================================================================

@pytest.fixture
def holder(monkeypatch):
    """The orchestrator holds `agents.manage` (ent#164), so only the bound decides."""
    from database import db
    monkeypatch.setattr(db, "agent_has_capability", lambda agent, cap: agent == "orchestrator")
    monkeypatch.setattr(db, "get_agent_ephemeral_info", lambda agent: None)


@pytest.mark.parametrize("ephemeral", [None, SimpleNamespace(ttl_seconds=60, max_executions=None)])
def test_the_create_endpoint_refuses_before_the_claim_and_the_create(
        bundled, allowlist, holder, audit, monkeypatch, ephemeral):
    from routers import agents as agents_router
    created, claimed = [], []

    async def fake_create(*a, **k):
        created.append(a)
        return {"name": "new-one"}
    monkeypatch.setattr(agents_router, "create_agent_internal", fake_create)
    monkeypatch.setattr(agents_router.idempotency_service, "begin",
                        lambda *a, **k: claimed.append(a) or SimpleNamespace(replay=False))
    monkeypatch.setattr(agents_router.idempotency_service, "complete", lambda *a, **k: None)
    allowlist(["local:writer"])
    req = _request()

    with pytest.raises(HTTPException) as e:
        asyncio.run(agents_router.create_agent_endpoint(
            _config("local:scout", ephemeral=ephemeral), req, _principal(), "idem-1"))
    assert e.value.status_code == 403
    assert e.value.detail["code"] == "template_source_not_allowed"
    assert created == [] and claimed == []
    assert len(_refusal_rows(audit)) == 1

    asyncio.run(agents_router.create_agent_endpoint(
        _config("local:writer", ephemeral=ephemeral), req, _principal(), "idem-2"))
    assert len(created) == 1 and len(claimed) == 1


def test_a_person_is_never_bounded_at_the_endpoint(bundled, allowlist, audit, monkeypatch):
    from routers import agents as agents_router
    created = []

    async def fake_create(*a, **k):
        created.append(a)
        return {"name": "new-one"}
    monkeypatch.setattr(agents_router, "create_agent_internal", fake_create)
    allowlist(["local:writer"])
    asyncio.run(agents_router.create_agent_endpoint(
        _config("github:anyone/anything"), _request(), _principal(**PERSON), None))
    assert len(created) == 1 and _refusal_rows(audit) == []


# ===========================================================================
# 5. create_agent_internal — the choke point (fork, copy, deploy members, ...)
# ===========================================================================

@pytest.fixture
def crud_env(monkeypatch):
    import test_1484_create_agent_characterization as h
    crud, ctx = h._load_crud(monkeypatch)
    try:
        yield crud, ctx
    finally:
        ctx["patcher"].stop()


def _set_crud_list(ctx, entries):
    ctx["db"].get_setting_value.side_effect = (
        lambda key, default=None: json.dumps(entries) if key == "agent_creation_allowlist" else default)


def _agent_user(**kw):
    return _principal("agent", "orchestrator", role="creator", id=7, username="eugene", **kw)


def _ghost_entitled(crud):
    crud.entitlement_service.is_entitled = MagicMock(return_value=True)


@pytest.mark.asyncio
async def test_the_choke_point_refuses_a_ghost_before_its_spawn_token(crud_env):
    from models import AgentConfig, EphemeralConfig
    crud, ctx = crud_env
    _ghost_entitled(crud)
    _set_crud_list(ctx, ["local:other"])
    config = AgentConfig(name="helper", template="local:scout", ephemeral=EphemeralConfig(ttl_seconds=60))
    with pytest.raises(HTTPException) as e:
        await crud.create_agent_internal(config, _agent_user(), request=None)
    assert e.value.detail["code"] == "template_source_not_allowed"
    ctx["rate_limiter"].enforce.assert_not_called()
    ctx["docker_utils"].containers_run.assert_not_awaited()
    ctx["db"].create_agent_mcp_api_key.assert_not_called()


@pytest.mark.asyncio
async def test_the_choke_point_admits_a_listed_bundled_template(crud_env):
    from models import AgentConfig
    crud, ctx = crud_env
    _set_crud_list(ctx, ["local:scout"])  # `scout` is bundled in the #1484 root
    await crud.create_agent_internal(AgentConfig(name="ok-agent", template="local:scout"),
                                     _agent_user(), request=None)
    ctx["docker_utils"].containers_run.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [
    {"fork_to_own": {"destination_repo": "eugene/fork"}},
    {"import_intent": "copy"},
    {"import_intent": "clone"},
])
async def test_fork_copy_and_clone_meet_the_bound_before_any_github_work(crud_env, extra):
    from models import AgentConfig
    crud, ctx = crud_env
    _set_crud_list(ctx, ["github:abilityai/*"])
    config = AgentConfig(name="imp", template="github:evil/tools", **extra)
    with pytest.raises(HTTPException) as e:
        await crud.create_agent_internal(config, _agent_user(), request=None)
    assert e.value.detail["code"] == "template_source_not_allowed"
    ctx["template_service"].fetch_template_metadata_result_for_create.assert_not_called()
    ctx["git_service"].reserve_and_generate_instance_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_platform_caller_with_no_request_is_never_bounded(crud_env):
    """The Cornelius seeder passes an admin `User` (no agent key) and request=None."""
    from models import AgentConfig
    crud, ctx = crud_env
    _set_crud_list(ctx, ["local:other"])
    await crud.create_agent_internal(AgentConfig(name="seeded", template="local:scout"),
                                     _principal(**PERSON, role="admin"), request=None)
    ctx["docker_utils"].containers_run.assert_awaited_once()


# ===========================================================================
# 6. deploy-local — an upload is never on the list
# ===========================================================================

def _deploy_local(agents_router, principal):
    from models import DeployLocalRequest
    return asyncio.run(agents_router.deploy_local_agent(
        DeployLocalRequest(archive="eA=="), _request("/api/agents/deploy-local"), principal, "idem-d"))


def test_deploy_local_is_refused_for_an_agent_key_while_bounded(allowlist, audit, monkeypatch):
    from routers import agents as agents_router
    logic, claimed = AsyncMock(return_value=SimpleNamespace(versioning=None)), []
    monkeypatch.setattr(agents_router, "deploy_local_agent_logic", logic)
    monkeypatch.setattr(agents_router.idempotency_service, "begin",
                        lambda *a, **k: claimed.append(a) or SimpleNamespace(replay=False))
    monkeypatch.setattr(agents_router.idempotency_service, "complete", lambda *a, **k: None)
    allowlist(["local:scout"])
    with pytest.raises(HTTPException) as e:
        _deploy_local(agents_router, _principal())
    assert e.value.detail["code"] == "template_upload_not_allowed"
    logic.assert_not_awaited()
    assert claimed == [] and len(_refusal_rows(audit)) == 1

    _deploy_local(agents_router, _principal(**PERSON))      # a person: unchanged
    allowlist(None)
    _deploy_local(agents_router, _principal())              # empty list: unchanged
    assert logic.await_count == 2


# ===========================================================================
# 7. System deploy — whole refusal, predicted by the dry run
# ===========================================================================

MANIFEST = """
name: fleet
agents:
  scout:
    template: local:scout
  rogue:
    template: github:evil/tools
"""


@pytest.fixture
def deploy_env(monkeypatch):
    import services.agent_service.crud as crud
    import services.system_service as system_service
    monkeypatch.setattr(crud, "_resolve_local_template", lambda config: ({}, None))
    monkeypatch.setattr(system_service, "resolve_agent_names",
                        lambda system_name, agents, naming="prefixed": (
                            {s: f"{system_name}-{s}" for s in agents}, []))
    for name in ("configure_folders", "create_schedules", "configure_tags", "create_system_view"):
        monkeypatch.setattr(system_service, name, MagicMock(return_value=0))
    monkeypatch.setattr(system_service, "configure_permissions", AsyncMock(return_value=0))
    monkeypatch.setattr(system_service, "start_all_agents",
                        AsyncMock(side_effect=lambda names: {n: "started" for n in names}))
    return system_service


def test_the_dry_run_predicts_exactly_the_members_the_write_refuses(
        bundled, allowlist, audit, deploy_env):
    allowlist(["local:scout"])
    create = AsyncMock(return_value={"status": "created"})
    preview = asyncio.run(deploy_env.deploy_manifest(
        MANIFEST, _principal(), _request("/api/systems/deploy"), dry_run=True, create_agent_fn=create))
    assert preview.status == "invalid"
    refused_preview = {(f.short_name, f.status_code) for f in preview.failed}
    assert refused_preview == {("rogue", 403)}

    with pytest.raises(HTTPException) as e:
        asyncio.run(deploy_env.deploy_manifest(
            MANIFEST, _principal(), _request("/api/systems/deploy"), create_agent_fn=create))
    assert e.value.status_code == 403
    assert e.value.detail["code"] == "template_source_not_allowed"
    assert {(m["short_name"], 403) for m in e.value.detail["members"]} == refused_preview
    assert e.value.detail["members"][0]["template"] == "github:evil/tools"
    create.assert_not_awaited()
    assert len(_refusal_rows(audit)) == 1


def test_a_person_and_an_all_listed_manifest_deploy(bundled, allowlist, audit, deploy_env):
    allowlist(["local:scout", "github:evil/tools"])
    create = AsyncMock(return_value={"status": "created"})
    out = asyncio.run(deploy_env.deploy_manifest(
        MANIFEST, _principal(), _request("/api/systems/deploy"), create_agent_fn=create))
    assert out.status == "deployed" and create.await_count == 2
    allowlist(["local:scout"])
    out = asyncio.run(deploy_env.deploy_manifest(
        MANIFEST, _principal(**PERSON), _request("/api/systems/deploy"), create_agent_fn=create))
    assert out.status == "deployed" and create.await_count == 4
    assert _refusal_rows(audit) == []


def test_apply_stays_person_only():
    """Agent keys never reach apply, so it carries no bound. If apply is ever
    opened to agent keys, add the template-source blocker
    (`template_source_policy.manifest_refusals`) to its plan first."""
    from routers import systems
    route = next(r for r in systems.router.routes if r.path.endswith("/apply"))
    names = {getattr(d.call, "__name__", "") for d in route.dependant.dependencies}
    assert "require_person" in names


# ===========================================================================
# 8. The agent read: GET /api/templates
# ===========================================================================

def test_the_template_list_says_what_an_agent_may_use(bundled, allowlist, monkeypatch):
    from routers import templates as templates_router
    catalog = lambda: [{"id": "local:scout"}, {"id": "github:acme/tools"}, {"id": "local:writer"}]
    monkeypatch.setattr(templates_router, "get_all_templates", catalog)
    allowlist(["local:scout", "github:acme/tools@v2"])

    got = asyncio.run(templates_router.list_templates(current_user=_principal()))
    assert {t["id"]: t["allowed_for_agents"] for t in got} == {
        "local:scout": True, "github:acme/tools": False, "local:writer": False}

    person = asyncio.run(templates_router.list_templates(current_user=_principal(**PERSON)))
    assert all("allowed_for_agents" not in t for t in person)
    allowlist(None)
    unbounded = asyncio.run(templates_router.list_templates(current_user=_principal()))
    assert all("allowed_for_agents" not in t for t in unbounded)


# ===========================================================================
# 9. The admin surface: GET/PUT /api/settings/agent-creation-allowlist
# ===========================================================================

URL = "/api/settings/agent-creation-allowlist"


@pytest.fixture
def settings_client(monkeypatch, bundled):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import routers.settings as rs
    import routers.settings.templates as rs_templates
    from database import db

    db.delete_setting("agent_creation_allowlist")
    ds = importlib.import_module("services.docker_service")
    monkeypatch.setattr(ds, "list_all_agents_fast", lambda: [
        SimpleNamespace(name="helper-1", template="github:evil/tools"),
        SimpleNamespace(name="helper-2", template="local:scout"),
        SimpleNamespace(name="by-a-person", template="github:evil/tools"),
    ])
    monkeypatch.setattr(ds, "agent_container_states", lambda: {
        "helper-1": "running", "helper-2": "running", "by-a-person": "stopped"})
    monkeypatch.setattr(db, "list_agent_created_agent_names", lambda: ["helper-1", "helper-2"])
    who = {"user": None}
    app = FastAPI()
    app.include_router(rs.router)
    app.dependency_overrides[rs_templates.get_current_user] = lambda: who["user"]
    client = TestClient(app, raise_server_exceptions=False)
    yield SimpleNamespace(client=client, who=who, db=db)
    db.delete_setting("agent_creation_allowlist")


def _admin(scope=None, **kw):
    from models import User
    base = dict(id=1, username="admin", email="admin@example.com", role="admin", mcp_scope=scope)
    base.update(kw)
    return User(**base)


def test_an_admin_sets_reads_and_clears_the_list(settings_client, monkeypatch):
    import routers.settings.templates as rs_templates
    # The instance the router bound at import (conftest pops the module between
    # tests, so a fresh import would be a different object).
    audit = AsyncMock(return_value="evt")
    monkeypatch.setattr(rs_templates.platform_audit_service, "log", audit)
    c = settings_client
    c.who["user"] = _admin()
    empty = c.client.get(URL).json()
    assert empty["entries"] == [] and empty["bounded"] is False
    assert {r["source"]: r["allowed"] for r in empty["in_use"]} == {
        "github:evil/tools": True, "local:scout": True}

    r = c.client.put(URL, json={"entries": [" github:Abilityai/* ", "local:scout", "local:not-bundled"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["entries"] == ["github:abilityai/*", "local:scout", "local:not-bundled"]
    assert body["bounded"] is True and body["not_bundled"] == ["local:not-bundled"]
    assert {r["source"]: r["allowed"] for r in body["in_use"]} == {
        "github:evil/tools": False, "local:scout": True}
    assert json.loads(c.db.get_setting_value("agent_creation_allowlist")) == body["entries"]
    changes = [a for a in audit.await_args_list
               if a.kwargs.get("event_action") == "agent_creation_allowlist_change"]
    assert len(changes) == 1 and changes[0].kwargs["details"]["new"] == body["entries"]

    cleared = c.client.put(URL, json={"entries": []}).json()
    assert cleared["bounded"] is False and c.db.get_setting_value("agent_creation_allowlist") is None


def test_an_invalid_save_names_each_bad_entry_and_stores_nothing(settings_client):
    c = settings_client
    c.who["user"] = _admin()
    r = c.client.put(URL, json={"entries": ["local:scout", "github:acme", "*"]})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["code"] == "invalid_template_sources"
    assert [(e["index"], e["entry"]) for e in detail["errors"]] == [(1, "github:acme"), (2, "*")]
    assert c.db.get_setting_value("agent_creation_allowlist") is None


@pytest.mark.parametrize("principal", [
    _admin("user"),                                          # an admin's own MCP key
    _admin("agent", agent_name="orchestrator"),              # an agent key carrying admin
    _admin("system"),
    _admin(None, role="creator"),                            # signed in, not admin
])
def test_only_a_signed_in_admin_writes_the_list(settings_client, principal):
    c = settings_client
    c.who["user"] = principal
    r = c.client.put(URL, json={"entries": ["local:scout"]})
    assert r.status_code == 403
    assert c.db.get_setting_value("agent_creation_allowlist") is None


def test_an_agent_key_cannot_read_the_admin_view(settings_client):
    settings_client.who["user"] = _admin("agent", agent_name="orchestrator")
    assert settings_client.client.get(URL).status_code == 403


def test_the_generic_catch_all_refuses_the_key(settings_client):
    c = settings_client
    c.who["user"] = _admin()
    put = c.client.put("/api/settings/agent_creation_allowlist", json={"value": "[\"*\"]"})
    assert put.status_code == 422 and "agent-creation-allowlist" in put.text
    assert c.client.delete("/api/settings/agent_creation_allowlist").status_code == 422
    assert c.db.get_setting_value("agent_creation_allowlist") is None


def test_a_damaged_row_shows_as_bounded_and_invalid(settings_client):
    c = settings_client
    c.who["user"] = _admin()
    c.db.set_setting("agent_creation_allowlist", "not json")
    body = c.client.get(URL).json()
    assert body["bounded"] is True and body["stored_invalid"] is True and body["entries"] == []


def test_the_impact_read_degrades_to_unknown_never_to_none(settings_client, monkeypatch):
    """`list_all_agents_fast` answers `[]` when Docker cannot be read (#2196),
    never raises, so the impact read asks the tri-state reader first: Docker
    unreadable is `None` ("couldn't read"), a fleet with no containers is `[]`."""
    c = settings_client
    c.who["user"] = _admin()
    ds = importlib.import_module("services.docker_service")
    monkeypatch.setattr(ds, "list_all_agents_fast", lambda: [])
    monkeypatch.setattr(ds, "agent_container_states", lambda: None)
    assert c.client.get(URL).json()["in_use"] is None
    monkeypatch.setattr(ds, "agent_container_states", lambda: {})
    assert c.client.get(URL).json()["in_use"] == []


# ===========================================================================
# 10. The pieces behind the reads and the audit row
# ===========================================================================

def test_the_impact_query_lists_live_agent_created_agents_only():
    """The real query (the settings tests stub it): spawned and live, durable or
    ghost; a person's agent and a soft-deleted child are not listed."""
    from database import db
    from db_models import UserCreate
    owner = "ent591-owner"
    if not db.get_user_by_username(owner):
        db.create_user(UserCreate(username=owner, role="user", email="ent591@example.com"))
    names = ["ent591-child", "ent591-mine", "ent591-gone"]
    try:
        db.register_agent_owner("ent591-child", owner, spawned_by_agent="ent591-parent")
        db.register_agent_owner("ent591-mine", owner)
        db.register_agent_owner("ent591-gone", owner, spawned_by_agent="ent591-parent")
        db.delete_agent_ownership("ent591-gone")
        listed = set(db.list_agent_created_agent_names())
        assert "ent591-child" in listed
        assert not {"ent591-mine", "ent591-gone"} & listed
    finally:
        for n in names:
            db.purge_agent_ownership(n)


def test_the_refusal_row_names_the_agent_and_a_sanitised_target(bundled, allowlist, audit):
    allowlist(["local:writer"])
    with pytest.raises(HTTPException):
        asyncio.run(_tsp().enforce(_request(), _principal(), _config("local:scout", name="New One\n<x>")))
    (row,) = _refusal_rows(audit)
    kw = row.kwargs
    assert kw["actor_agent_name"] == "orchestrator" and "actor_user" not in kw
    assert kw["mcp_key_id"] == "key-1" and kw["actor_email"] == "owner@example.com"
    assert kw["target_id"] == "new-one-x"
    assert kw["details"]["template"] == "local:scout" and kw["details"]["code"] == "template_source_not_allowed"


def test_the_template_annotation_never_mutates_the_catalog(bundled, allowlist):
    allowlist(["local:scout"])
    catalog = [{"id": "local:scout"}]
    out = _tsp().annotate_templates(_principal(), catalog)
    assert out[0]["allowed_for_agents"] is True and "allowed_for_agents" not in catalog[0]


# ===========================================================================
# 11. Eyeball findings (2026-11-06)
# ===========================================================================

def test_a_hidden_bundled_entry_is_named_in_the_admin_view(settings_client):
    """`local:default` is `hidden: true`: agents may create from it, but the
    template list never shows it, so no entry reads `allowed_for_agents: true`.
    The admin view names such entries, beside the not-bundled ones."""
    c = settings_client
    c.who["user"] = _admin()
    body = c.client.put(URL, json={"entries": ["local:scout", "local:quiet", "local:nope"]}).json()
    assert body["hidden"] == ["local:quiet"]
    assert body["not_bundled"] == ["local:nope"]
    assert c.client.get(URL).json()["hidden"] == ["local:quiet"]


def test_a_hidden_bundled_entry_still_admits_the_create(bundled, allowlist):
    allowlist(["local:quiet"])
    assert _tsp().refusal_for(_principal(), _config("local:quiet")) is None


@pytest.mark.parametrize("raw,allowed,limited", [
    ("not json", [], "may not create agents from any template"),
    ('["local:scout", "bad"]', ["local:scout"], "limited to allowed_sources"),
])
def test_an_unreadable_list_says_so_in_the_refusal(bundled, allowlist, raw, allowed, limited):
    """A damaged row must not read as "X is not on the list": the agent is told
    the saved list could not be read and stays limited until an admin saves it."""
    allowlist(raw)
    detail = _tsp().refusal_for(_principal(), _config("local:writer")).detail()
    assert detail["code"] == "template_source_not_allowed"
    assert detail["allowed_sources"] == allowed
    assert detail["list_unreadable"] is True
    message = detail["message"]
    assert "is not on the list" not in message
    assert "the saved list could not be read" in message
    assert limited in message
    assert "until an instance admin saves it again" in message


def test_a_readable_list_carries_no_unreadable_flag(bundled, allowlist):
    allowlist(["local:scout"])
    assert "list_unreadable" not in _tsp().refusal_for(_principal(), _config("local:writer")).detail()


def test_a_system_deploy_against_an_unreadable_list_says_so(bundled, allowlist, audit, deploy_env):
    allowlist("not json")
    create = AsyncMock(return_value={"status": "created"})
    with pytest.raises(HTTPException) as e:
        asyncio.run(deploy_env.deploy_manifest(
            MANIFEST, _principal(), _request("/api/systems/deploy"), create_agent_fn=create))
    assert "the saved list could not be read" in e.value.detail["message"]
    assert "not on the list" not in e.value.detail["message"]
    assert e.value.detail["list_unreadable"] is True
    create.assert_not_awaited()


# ===========================================================================
# 12. The pin is judged on what the build clones (review of PR #4311)
# ===========================================================================

def test_an_explicit_working_branch_never_matches_a_pin(bundled, allowlist):
    """A working-branch agent clones the repo's DEFAULT branch (startup.sh runs
    `git clone` without `-b` unless GIT_SOURCE_MODE), so `source_mode: false`
    can never satisfy a pinned entry; an unpinned entry still admits it."""
    tsp = _tsp()
    allowlist(["github:acme/tools@v2"])
    assert tsp.refusal_for(_principal(), _config("github:acme/tools@v2", source_mode=False)) is not None
    assert tsp.refusal_for(_principal(), _config("github:acme/tools@v2", source_mode=True)) is None
    allowlist(["github:acme/tools"])
    assert tsp.refusal_for(_principal(), _config("github:acme/tools@v2", source_mode=False)) is None


def test_a_pinned_match_makes_the_create_track_the_pin(bundled, allowlist, audit):
    """Ruled 2026-11-06: a create admitted only by a pinned entry, that left
    the mode unset, is made a source-mode create, so ent#705 (which honours an
    explicit `source_mode`) cannot turn it into a working branch built from the
    default branch. An unpinned match leaves the create untouched."""
    from models import AgentConfig
    tsp = _tsp()
    allowlist(["github:acme/tools@v2"])
    pinned = AgentConfig(name="x", template="github:acme/tools@v2")
    asyncio.run(tsp.enforce(_request(), _principal(), pinned))
    assert pinned.source_mode is True and "source_mode" in pinned.model_fields_set

    allowlist(["github:acme/tools"])
    free = AgentConfig(name="y", template="github:acme/tools@v2")
    asyncio.run(tsp.enforce(_request(), _principal(), free))
    assert "source_mode" not in free.model_fields_set
    # Both a pin and a looser entry: the looser one admits it, nothing is forced.
    allowlist(["github:acme/tools@v2", "github:acme/tools"])
    both = AgentConfig(name="b", template="github:acme/tools@v2")
    asyncio.run(tsp.enforce(_request(), _principal(), both))
    assert "source_mode" not in both.model_fields_set

    allowlist(["github:acme/tools@v2"])
    with pytest.raises(HTTPException):
        asyncio.run(tsp.enforce(_request(), _principal(),
                                AgentConfig(name="z", template="github:acme/tools@v2", source_mode=False)))
    person = AgentConfig(name="p", template="github:acme/tools@v2")
    asyncio.run(tsp.enforce(_request(), _principal(**PERSON), person))
    assert "source_mode" not in person.model_fields_set      # people are never touched


def test_a_manifest_member_on_a_pin_previews_as_allowed(bundled, allowlist):
    """The deploy preview judges a member as the create will: no `source_mode`
    in a manifest, so a pinned member is admitted and the real create is made
    source mode by the choke point — the dry run stays exact."""
    allowlist(["github:acme/tools@v2"])
    refused, _ = _tsp().manifest_refusals(_principal(), [("f-a", "a", "github:acme/tools@v2")])
    assert refused == []


@pytest.mark.asyncio
async def test_a_pin_beats_the_working_branch_default(crud_env, monkeypatch):
    """End to end through the real create: the creator's own token owns the repo
    and can push, which is exactly when ent#705 grants a working branch. With a
    pinned entry the agent is built in source mode on the pinned ref instead."""
    import test_1484_create_agent_characterization as h
    crud, ctx = crud_env
    h._own_token(monkeypatch, crud)
    ctx["template_service"].get_github_template.return_value = {
        "github_repo": "alice/brain", "resources": {"cpu": "2", "memory": "4g"},
        "mcp_servers": [], "fork_to_own": None,
    }
    ctx["template_service"].is_catalog_github_template.return_value = False
    h._patch_repo_validation(monkeypatch, crud, login=("valid", "alice"))

    _set_crud_list(ctx, ["github:alice/brain@v2"])
    from models import AgentConfig
    pinned = await crud.create_agent_internal(
        AgentConfig(name="pinned", template="github:alice/brain@v2"), _agent_user(), request=None)
    env = h._agent_run_kwargs(ctx)["environment"]
    assert env.get("GIT_SOURCE_MODE") == "true" and env.get("GIT_SOURCE_BRANCH") == "v2"
    assert "GIT_WORKING_BRANCH" not in env
    assert pinned.git_mode["source_mode"] is True

    # Control: the same create under an unpinned entry keeps ent#705's grant.
    ctx["docker_utils"].containers_run.reset_mock()
    _set_crud_list(ctx, ["github:alice/brain"])
    free = await crud.create_agent_internal(
        AgentConfig(name="free", template="github:alice/brain@v2"), _agent_user(), request=None)
    assert free.git_mode["source_mode"] is False
    assert "GIT_WORKING_BRANCH" in h._agent_run_kwargs(ctx)["environment"]
