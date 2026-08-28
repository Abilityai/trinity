"""An unresolvable or INVALID `local:` template must fail loudly (#1759).

`_resolve_local_template` had no `else`: a `local:<name>` whose directory (or
`template.yaml`) did not exist under either root returned `template_data == {}`
and the agent was created — HTTP 200, blank container, no warning. Only
*malformed* names failed (`INVALID_LOCAL_TEMPLATE_NAME`). This is the sibling
hole #843 explicitly left open for unprefixed names.

The ABSENT half was closed on `dev` by #1793 (PR #1803) with **404
`UNKNOWN_LOCAL_TEMPLATE`**; #1759 was built in parallel against the same line
and conceded that contract at merge. What #1793 did NOT close, and what this
file now primarily covers, is the **present-but-invalid** half — an empty,
non-mapping or unparseable `template.yaml` still reached the identical blank
agent at HTTP 200 through the broad `except Exception`. That is **400
`LOCAL_TEMPLATE_INVALID`**.

The absent-template cases are kept here rather than deleted: unlike
`test_1793_unknown_local_template.py` (which calls `_resolve_local_template`
directly) they drive the full `create_agent_internal` and assert
`_assert_no_side_effects` — no container, volume, MCP key, ownership row or
ephemeral slot. That pre-side-effect proof exists nowhere else.

Every test here monkeypatches `crud._LOCAL_TEMPLATE_ROOTS` to REAL `tmp_path`
directories (the `test_1484:853` precedent). That is the anti-trap: the gate is
exercised against a real filesystem and never touches
`services.template_service`, which this harness MagicMocks — a gate that called
`template_service._local_templates_dir()` would be satisfied by a truthy mock
and the 15 `test_1484` characterization tests would stay green on the OLD
behaviour, silently.

Template names are deliberately unambiguous non-catalog names (`nope-1759`):
`_LOCAL_TEMPLATE_NAME_RE` allows uppercase and macOS is case-insensitive, so
`local:SCOUT` would resolve on a dev box and 404 on Linux CI.

Harness: copied + trimmed from `test_1484_create_agent_characterization.py`
(D6 — deliberately NOT shared cross-file; sharing reintroduces a documented
`sys.modules` / `services.*` leak).

Target: src/backend/services/agent_service/crud.py
Issue:  abilityai/trinity#1759
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Env prerequisites before any backend import (repo test convention).
os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")
os.environ.setdefault("AGENT_AUTH_SECRET", "0" * 64)
_TMP_DB = Path(tempfile.gettempdir()) / "trinity_test_1759_local_template.db"
os.environ.setdefault("TRINITY_DB_PATH", str(_TMP_DB))

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = str(_PROJECT_ROOT / "src" / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from fastapi import HTTPException  # noqa: E402
from models import AgentConfig  # noqa: E402

pytestmark = pytest.mark.unit


class _NotFound(Exception):
    """Stand-in for docker.errors.NotFound (the mocked docker module)."""


# ---------------------------------------------------------------------------
# Harness — import crud with every heavy dep mocked.
# ---------------------------------------------------------------------------


def _load_crud(monkeypatch):
    docker_mod = MagicMock()
    docker_mod.errors.NotFound = _NotFound

    docker_service = MagicMock()
    docker_service.docker_client = MagicMock()
    docker_service.get_agent_by_name = MagicMock(return_value=None)
    docker_service.get_next_available_port = MagicMock(return_value=2222)
    docker_service.get_agent_status_from_container = MagicMock(return_value=MagicMock())

    docker_utils = MagicMock()
    docker_utils.volume_get = AsyncMock(side_effect=_NotFound("no such volume"))
    docker_utils.volume_create = AsyncMock()
    docker_utils.containers_run = AsyncMock(return_value=MagicMock())

    template_service = MagicMock()
    template_service.generate_credential_files = MagicMock(return_value={})
    template_service.get_github_template = MagicMock(return_value=None)
    # ent#128: `_resolve_local_template` reads the template's declared MCP servers
    # through this tolerant accessor instead of reaching through `credentials:`
    # raw (a null/list/string block raised AttributeError, and that read sits
    # FIRST in a run of `config` mutations under one broad `except` — so it cost
    # the agent its `runtime:` and `shared_folders:` too). This harness MagicMocks
    # the whole module, so an unstubbed call returns a truthy Mock that lands in
    # `config.mcp_servers` and later blows up in a `yaml.dump`. Stubbed with a
    # faithful mirror rather than a fixed `[]` so a fixture that DOES declare
    # credentials cannot be silently masked.
    def _mcp_server_names(block):
        servers = block.get("mcp_servers") if isinstance(block, dict) else None
        return [str(n) for n in servers] if isinstance(servers, dict) else []

    template_service.credential_mcp_server_names = MagicMock(
        side_effect=_mcp_server_names
    )

    git_service = MagicMock()
    git_service.DEFAULT_PERSISTENT_STATE = ["memory/"]
    git_service.DEFAULT_DATA_PATHS = []
    git_service.reserve_and_generate_instance_id = AsyncMock(
        return_value=("iid-1", "main")
    )
    git_service.materialize_persistent_state = AsyncMock()
    git_service.materialize_data_paths = AsyncMock()

    settings_service = MagicMock()
    settings_service.get_anthropic_api_key = MagicMock(return_value="sk-ant-key")
    settings_service.resolve_github_pat = MagicMock(
        return_value=("platform-pat", "global")
    )
    settings_service.get_agent_full_capabilities = MagicMock(return_value=False)
    settings_service.get_agent_quota_for_role = MagicMock(return_value=0)
    settings_service.get_agent_default_resources = MagicMock(
        return_value={"cpu": "2", "memory": "4g"}
    )
    settings_service.get_agent_default_require_email = MagicMock(return_value=False)
    settings_service.get_ephemeral_agent_quota = MagicMock(return_value=5)
    settings_service.get_ephemeral_ttl_ceiling_seconds = MagicMock(return_value=86400)

    entitlement_mod = MagicMock()
    entitlement_mod.entitlement_service.is_entitled = MagicMock(return_value=False)

    rate_limiter_mod = MagicMock()
    rate_limiter_mod.enforce = MagicMock()

    ephemeral_mod = MagicMock()
    ephemeral_mod.try_reserve_ephemeral_slot = MagicMock(return_value=True)
    ephemeral_mod.release_ephemeral_slot = MagicMock()

    pull_mode_mod = MagicMock()
    pull_mode_mod.pull_mode_env_vars = MagicMock(return_value={})

    runtime_state_mod = MagicMock()
    runtime_state_mod.clear_agent_breakers = MagicMock()

    helpers_mod = MagicMock()
    helpers_mod.validate_base_image = MagicMock()
    helpers_mod.is_claude_runtime = MagicMock(return_value=False)
    helpers_mod.validate_runtime = MagicMock()

    lifecycle_mod = MagicMock()
    lifecycle_mod.RESTRICTED_CAPABILITIES = []
    lifecycle_mod.FULL_CAPABILITIES = []

    capabilities_mod = MagicMock()
    capabilities_mod.AGENT_TMPFS_MOUNT = {"/tmp": "size=512m"}
    capabilities_mod.AGENT_DEFAULT_TMPDIR = "/home/developer/.tmp"
    capabilities_mod.normalize_cpu = MagicMock(side_effect=lambda v, d: v or d)
    capabilities_mod.normalize_memory = MagicMock(side_effect=lambda v, d: v or d)

    agent_auth = MagicMock()
    agent_auth.derive_agent_token = MagicMock(return_value="agent-auth-token")

    database_mod = MagicMock()
    db = database_mod.db
    db.get_agent_owner.return_value = None
    db.is_agent_name_reserved.return_value = False
    db.is_volume_base_reserved.return_value = False
    db.get_agents_by_owner.return_value = []
    db.get_guardrails_config.return_value = None
    db.create_agent_mcp_api_key.return_value = MagicMock(
        api_key="trinity_mcp_test", key_prefix="trinity_mcp_te"
    )
    db.get_git_config_agent_names_for_repo.return_value = []
    db.set_agent_github_pat.return_value = True
    db.get_shared_folder_config.return_value = None
    db.get_file_sharing_enabled.return_value = False
    db.grant_default_permissions.return_value = 0
    db.register_agent_owner.return_value = None
    db.get_agent_ephemeral_info.return_value = None
    db.get_user_by_username.return_value = {"id": 7}
    db.get_agent_mcp_api_key.return_value = None
    db.list_assignable_subscriptions.return_value = []
    db.get_subscription_token.return_value = None
    db.add_agent_permission.return_value = None
    db.assign_subscription_to_agent.return_value = None
    db.set_default_avatar.return_value = None
    db.set_git_auto_sync_enabled.return_value = None
    db.delete_git_config.return_value = None
    db.delete_agent_mcp_api_key.return_value = None
    db.upsert_shared_folder_config.return_value = None

    pkg = "services.agent_service"
    sibling_mocks = {}
    for sib in [
        "api_key",
        "autonomy",
        "dashboard",
        "deploy",
        "file_sharing",
        "files",
        "folders",
        "helpers",
        "lifecycle",
        "mcp_tool_names",
        "metrics",
        "permissions",
        "queue",
        "read_only",
        "stats",
        "terminal",
        "capabilities",
        "ephemeral",
        "pull_mode",
    ]:
        mod = {
            "helpers": helpers_mod,
            "lifecycle": lifecycle_mod,
            "capabilities": capabilities_mod,
            "ephemeral": ephemeral_mod,
            "pull_mode": pull_mode_mod,
        }.get(sib, MagicMock())
        sibling_mocks[f"{pkg}.{sib}"] = mod

    mocks = {
        "docker": docker_mod,
        "docker.errors": docker_mod.errors,
        "redis": MagicMock(),
        "redis.asyncio": MagicMock(),
        "database": database_mod,
        "services.docker_service": docker_service,
        "services.docker_utils": docker_utils,
        "services.template_service": template_service,
        "services.git_service": git_service,
        "services.settings_service": settings_service,
        "services.entitlement_service": entitlement_mod,
        "services.rate_limiter": rate_limiter_mod,
        "services.agent_runtime_state": runtime_state_mod,
        "services.agent_auth": agent_auth,
        **sibling_mocks,
    }

    patcher = patch.dict("sys.modules", mocks)
    patcher.start()
    # Purge every real `services*` module not explicitly mocked so crud's
    # `from services import ...` resolves the sys.modules mock. `monkeypatch.delitem`
    # (not a bare `del`) per tests/lint_sys_modules.py.
    for key in list(sys.modules.keys()):
        if (key == "services" or key.startswith("services.")) and key not in mocks:
            monkeypatch.delitem(sys.modules, key, raising=False)
    import services.agent_service.crud as crud

    return crud, {
        "patcher": patcher,
        "db": db,
        "docker_service": docker_service,
        "docker_utils": docker_utils,
        "template_service": template_service,
        "git_service": git_service,
        "settings_service": settings_service,
        "ephemeral": ephemeral_mod,
        "rate_limiter": rate_limiter_mod,
        "runtime_state": runtime_state_mod,
    }


@pytest.fixture
def crud_env(monkeypatch):
    crud, ctx = _load_crud(monkeypatch)
    try:
        yield crud, ctx
    finally:
        ctx["patcher"].stop()


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _user(uid=7, username="eugene", role="creator"):
    u = MagicMock()
    u.id = uid
    u.username = username
    u.role = role
    u.agent_name = None
    return u


def _roots(monkeypatch, crud, tmp_path):
    """Point BOTH roots at real, empty tmp dirs and return `(curated, deployed)`.

    Deliberately real directories — never a MagicMock — so the gate is proven
    against actual `pathlib` calls (see the module docstring).
    """
    curated = tmp_path / "curated"
    curated.mkdir()
    deployed = tmp_path / "deployed"
    deployed.mkdir()
    monkeypatch.setattr(crud, "_LOCAL_TEMPLATE_ROOTS", (curated, deployed))
    return curated, deployed


def _write_template(root: Path, name: str, body: str = "type: business-assistant\n"):
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "template.yaml").write_text(body)
    return d


#: An absolute path is a `/` that does NOT continue a relative path — i.e. one
#: not preceded by a path character. The negative lookbehind (rather than a
#: `(?:^|\s)` prefix) is load-bearing: a leading-whitespace rule is blind to a
#: path wrapped in ANY punctuation, and `!r` — the quoting style this module's
#: own messages already use for `config.template` — wraps in single quotes. A
#: future `f"...{template_path!r}..."` would emit `'/data/deployed-templates/x'`
#: and sail straight through a whitespace-anchored guard, shipping the leak with
#: a green test. `config/agent-templates/` stays clean because its `/` follows
#: `g`; `/api/...` is an HTTP route, not a filesystem path, and is the message's
#: whole remedy, so it is the one allowed `/`-rooted form.
#:
#: POSIX-only by construction, deliberately: every path that could reach one of
#: these messages is a `pathlib.Path` built inside the Linux backend container
#: (`/agent-configs/templates`, `/data/deployed-templates`), so a Windows-style
#: `C:\...` form is unreachable and a branch for it would be dead regex.
_ABS_PATH_RE = re.compile(r"(?<![A-Za-z0-9_.\-/])/[A-Za-z0-9_.\-/]+")

#: Naming EITHER root is banned outright, with or without a leading slash: the
#: plan's binding rule is that one identical message is returned whichever root
#: missed, because deploy-local templates (#950) are named after AGENT names, so
#: "which root missed" is itself the #186 enumeration oracle. A relative
#: `data/deployed-templates/victim` discloses exactly as much as the absolute
#: form and is invisible to any leading-slash rule. `agent-templates` is NOT
#: listed — `config/agent-templates/` is the public, intentional remedy.
_SENSITIVE_ROOT_TOKENS = ("deployed-templates", "agent-configs")


def _leaked_paths(msg: str) -> list[str]:
    """Filesystem disclosure in an error message (API routes excluded).

    MUST always be empty — see `test_t1b` / `test_t6b`.
    """
    hits = [p for p in _ABS_PATH_RE.findall(msg) if not p.startswith("/api/")]
    hits += [t for t in _SENSITIVE_ROOT_TOKENS if t in msg]
    return hits


def _agent_run_kwargs(ctx):
    """kwargs of the containers_run call that created the AGENT container."""
    for call in ctx["docker_utils"].containers_run.call_args_list:
        if call.kwargs.get("detach"):
            return call.kwargs
    raise AssertionError("agent container was never created")


def _assert_no_side_effects(ctx):
    """AC#1's real content: the 400 fires BEFORE anything is allocated.

    Asserting only "it raised" would pass even if the gate landed after the
    MCP-key mint / slot reservation / container run.
    """
    ctx["docker_utils"].containers_run.assert_not_awaited()
    ctx["docker_utils"].volume_create.assert_not_awaited()
    ctx["db"].create_agent_mcp_api_key.assert_not_called()
    ctx["db"].register_agent_owner.assert_not_called()
    ctx["ephemeral"].try_reserve_ephemeral_slot.assert_not_called()
    ctx["git_service"].reserve_and_generate_instance_id.assert_not_awaited()


# ===========================================================================
# T1 — absent in BOTH roots → 404 UNKNOWN_LOCAL_TEMPLATE (#1793), no side effects
# ===========================================================================


@pytest.mark.asyncio
async def test_t1_absent_in_both_roots_404_and_no_side_effects(
    crud_env, monkeypatch, tmp_path
):
    """The absent-template contract is #1793's 404. What this adds over
    `test_1793_unknown_local_template.py` is the pre-side-effect proof: the
    raise happens before any container, volume, MCP key or ownership row."""
    crud, ctx = crud_env
    _roots(monkeypatch, crud, tmp_path)

    with pytest.raises(HTTPException) as exc:
        await crud.create_agent_internal(
            AgentConfig(name="nf-1759", template="local:nope-1759"), _user(), None
        )

    assert exc.value.status_code == 404
    assert exc.value.detail["code"] == "UNKNOWN_LOCAL_TEMPLATE"
    # #1793 interpolates the STRIPPED name (`raw_name`), not the `local:` id.
    assert "nope-1759" in exc.value.detail["error"]
    _assert_no_side_effects(ctx)


@pytest.mark.asyncio
async def test_t1b_error_message_leaks_no_filesystem_path(
    crud_env, monkeypatch, tmp_path
):
    """The whole security surface of this fix.

    ONE identical message regardless of which root missed: deploy-local
    templates under /data/deployed-templates are named after AGENT names, which
    #186 does protect — a root-distinguishing (or path-echoing) message would
    let a creator-role caller probe whether another user's deploy-local agent
    exists.
    """
    crud, ctx = crud_env
    curated, deployed = _roots(monkeypatch, crud, tmp_path)

    with pytest.raises(HTTPException) as exc:
        await crud.create_agent_internal(
            AgentConfig(name="nf-leak", template="local:nope-1759"), _user(), None
        )

    msg = exc.value.detail["error"]
    assert str(curated) not in msg and str(deployed) not in msg
    assert str(tmp_path) not in msg
    assert "/agent-configs/templates" not in msg
    assert "/data/deployed-templates" not in msg
    # No absolute path of any shape, and no leading-slash root hint.
    assert _leaked_paths(msg) == []
    assert len(msg) < 500
    # Actionable remedy, abstract only.
    assert "GET /api/templates" in msg


@pytest.mark.asyncio
async def test_t1c_same_message_whichever_root_missed(crud_env, monkeypatch, tmp_path):
    """Curated-present / deployed-absent and both-absent must be
    indistinguishable to the caller."""
    crud, ctx = crud_env
    curated, deployed = _roots(monkeypatch, crud, tmp_path)
    # A sibling template exists in the curated root; the requested one does not.
    _write_template(curated, "other-1759")

    with pytest.raises(HTTPException) as exc_a:
        await crud.create_agent_internal(
            AgentConfig(name="nf-a", template="local:nope-1759"), _user(), None
        )

    # Now make the curated root itself vanish entirely.
    monkeypatch.setattr(
        crud, "_LOCAL_TEMPLATE_ROOTS", (tmp_path / "gone-a", tmp_path / "gone-b")
    )
    with pytest.raises(HTTPException) as exc_b:
        await crud.create_agent_internal(
            AgentConfig(name="nf-b", template="local:nope-1759"), _user(), None
        )

    assert exc_a.value.detail == exc_b.value.detail


# ===========================================================================
# T2 — present in root[0] (curated) → still creates
# ===========================================================================


@pytest.mark.asyncio
async def test_t2_present_in_curated_root_still_creates(
    crud_env, monkeypatch, tmp_path
):
    crud, ctx = crud_env
    curated, _ = _roots(monkeypatch, crud, tmp_path)
    _write_template(curated, "tpl-1759")

    await crud.create_agent_internal(
        AgentConfig(name="ok-curated", template="local:tpl-1759"), _user(), None
    )

    ctx["docker_utils"].containers_run.assert_awaited()
    ctx["db"].register_agent_owner.assert_called_once()


# ===========================================================================
# T3 — present ONLY in root[1] (deploy-local store) → still creates
# ===========================================================================


@pytest.mark.asyncio
async def test_t3_present_only_in_deployed_root_still_creates(
    crud_env, monkeypatch, tmp_path
):
    """#950 deploy-local: deploy.py copytree's the template into
    /data/deployed-templates and THEN calls create with
    `local:<version_name>`. A gate written against the curated root alone
    would 400 every deploy-local agent."""
    crud, ctx = crud_env
    _, deployed = _roots(monkeypatch, crud, tmp_path)
    _write_template(deployed, "deployed-1759")

    await crud.create_agent_internal(
        AgentConfig(name="ok-deployed", template="local:deployed-1759"), _user(), None
    )

    ctx["docker_utils"].containers_run.assert_awaited()
    ctx["db"].register_agent_owner.assert_called_once()
    # Deploy-local templates deliberately get NO /template bind (#950: the
    # workspace volume was already pre-populated via put_archive).
    volumes = _agent_run_kwargs(ctx)["volumes"]
    assert "/template" not in {v["bind"] for v in volumes.values()}


# ===========================================================================
# T4 — directory exists but no template.yaml (AC#1's "or template.yaml")
# ===========================================================================


@pytest.mark.asyncio
async def test_t4_directory_without_template_yaml_404(crud_env, monkeypatch, tmp_path):
    crud, ctx = crud_env
    curated, _ = _roots(monkeypatch, crud, tmp_path)
    (curated / "bare-1759").mkdir()  # dir exists, no template.yaml

    with pytest.raises(HTTPException) as exc:
        await crud.create_agent_internal(
            AgentConfig(name="nf-bare", template="local:bare-1759"), _user(), None
        )

    assert exc.value.status_code == 404
    assert exc.value.detail["code"] == "UNKNOWN_LOCAL_TEMPLATE"
    _assert_no_side_effects(ctx)


@pytest.mark.asyncio
async def test_t4b_name_resolving_to_a_regular_file_404(
    crud_env, monkeypatch, tmp_path
):
    crud, ctx = crud_env
    curated, _ = _roots(monkeypatch, crud, tmp_path)
    (curated / "afile-1759").write_text("not a directory")

    with pytest.raises(HTTPException) as exc:
        await crud.create_agent_internal(
            AgentConfig(name="nf-file", template="local:afile-1759"), _user(), None
        )

    assert exc.value.status_code == 404
    assert exc.value.detail["code"] == "UNKNOWN_LOCAL_TEMPLATE"
    _assert_no_side_effects(ctx)


# ===========================================================================
# T5 — empty / non-dict template.yaml → 400 LOCAL_TEMPLATE_INVALID (D5)
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body, label",
    [
        ("", "empty file"),
        ("   \n\n", "whitespace only"),
        ("# just a comment\n", "comments only"),
        ("just-a-scalar\n", "scalar document"),
        ("- a\n- b\n", "list document"),
    ],
)
async def test_t5_non_dict_template_yaml_400(
    crud_env, monkeypatch, tmp_path, body, label
):
    """`yaml.safe_load("")` returns None — identical observable outcome to an
    absent template (blank agent, HTTP 200) through a different line. The
    listing path already rejects non-dicts (`_build_local_template`), so the
    create path was strictly LESS strict than the listing path."""
    crud, ctx = crud_env
    curated, _ = _roots(monkeypatch, crud, tmp_path)
    _write_template(curated, "invalid-1759", body=body)

    with pytest.raises(HTTPException) as exc:
        await crud.create_agent_internal(
            AgentConfig(name="nf-invalid", template="local:invalid-1759"), _user(), None
        )

    assert exc.value.status_code == 400, label
    assert exc.value.detail["code"] == "LOCAL_TEMPLATE_INVALID", label
    assert "local:invalid-1759" in exc.value.detail["error"]
    _assert_no_side_effects(ctx)


# ===========================================================================
# T6 — unparseable YAML → 400, not a swallowed logger.warning (D5)
# ===========================================================================


@pytest.mark.asyncio
async def test_t6_unparseable_yaml_400(crud_env, monkeypatch, tmp_path):
    crud, ctx = crud_env
    curated, _ = _roots(monkeypatch, crud, tmp_path)
    _write_template(curated, "broken-1759", body="foo: [1, 2\nbar: }{\n")

    with pytest.raises(HTTPException) as exc:
        await crud.create_agent_internal(
            AgentConfig(name="nf-broken", template="local:broken-1759"), _user(), None
        )

    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "LOCAL_TEMPLATE_INVALID"
    _assert_no_side_effects(ctx)


@pytest.mark.asyncio
async def test_t6b_invalid_message_leaks_no_filesystem_path(
    crud_env, monkeypatch, tmp_path
):
    """Same disclosure rule as the NOT_FOUND message — and additionally the
    raw parser error (which quotes the file path and file contents) must not
    be echoed to the caller."""
    crud, ctx = crud_env
    curated, _ = _roots(monkeypatch, crud, tmp_path)
    _write_template(curated, "broken-1759", body="foo: [1, 2\nbar: }{\n")

    with pytest.raises(HTTPException) as exc:
        await crud.create_agent_internal(
            AgentConfig(name="nf-broken2", template="local:broken-1759"), _user(), None
        )

    msg = exc.value.detail["error"]
    assert str(tmp_path) not in msg
    assert _leaked_paths(msg) == []
    assert len(msg) < 500


# ===========================================================================
# T7 — template "" / None never enter the local: branch (Blank Agent)
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize("template", ["", None])
async def test_t7_blank_agent_unaffected(crud_env, monkeypatch, tmp_path, template):
    """The UI's "Blank Agent" sends `template: ''` (CreateAgentModal.vue). The
    gate lives inside `_resolve_local_template`, which `if config.template:`
    never reaches for these — this pins that it stays that way."""
    crud, ctx = crud_env
    _roots(monkeypatch, crud, tmp_path)  # both roots empty

    await crud.create_agent_internal(
        AgentConfig(
            name=f"blank-{'empty' if template == '' else 'none'}", template=template
        ),
        _user(),
        None,
    )

    ctx["docker_utils"].containers_run.assert_awaited()
    ctx["db"].register_agent_owner.assert_called_once()


# ===========================================================================
# T8 — malformed name still fails FIRST with INVALID_LOCAL_TEMPLATE_NAME
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw", ["../etc", "..", "a/b", "a\\b", ".hidden", "", "has space"]
)
async def test_t8_malformed_name_wins_first(crud_env, monkeypatch, tmp_path, raw):
    """The traversal barrier (#950) must keep precedence over the new
    existence gate — otherwise a hostile name would be reported as
    "not found", which is a weaker (and differently-shaped) rejection.
    Zero coverage before #1759."""
    crud, ctx = crud_env
    _roots(monkeypatch, crud, tmp_path)

    with pytest.raises(HTTPException) as exc:
        await crud.create_agent_internal(
            AgentConfig(name="nf-malformed", template=f"local:{raw}"), _user(), None
        )

    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "INVALID_LOCAL_TEMPLATE_NAME"
    _assert_no_side_effects(ctx)


# ===========================================================================
# T10 — the /template bind source (seam 2, crud.py:_stage_config_files)
# ===========================================================================


@pytest.mark.asyncio
async def test_t10_bind_source_honours_host_templates_path(
    crud_env, monkeypatch, tmp_path
):
    """Byte-identical to today's behaviour when HOST_TEMPLATES_PATH is set —
    which compose always does (docker-compose.prod.yml:138), i.e. the verified
    container path is unchanged by the seam-2 widening."""
    crud, ctx = crud_env
    curated, _ = _roots(monkeypatch, crud, tmp_path)
    _write_template(curated, "bindtpl-1759")
    monkeypatch.setenv("HOST_TEMPLATES_PATH", "/host/templates")

    await crud.create_agent_internal(
        AgentConfig(name="bind-set", template="local:bindtpl-1759"), _user(), None
    )

    volumes = _agent_run_kwargs(ctx)["volumes"]
    assert volumes["/host/templates/bindtpl-1759"] == {
        "bind": "/template",
        "mode": "ro",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("env_value", [None, ""])
async def test_t10b_bind_source_absolute_when_host_path_unset_or_empty(
    crud_env, monkeypatch, tmp_path, env_value
):
    """F8: `HOST_TEMPLATES_PATH` defaulted to the RELATIVE
    "./config/agent-templates" (Docker rejects a relative bind source), and an
    EMPTY value made `Path("") / name` → a bare name → an empty NAMED VOLUME
    mounted at /template — a silently blank template, the exact bug class this
    issue kills, one seam over. `os.getenv(k) or default` closes both."""
    crud, ctx = crud_env
    curated, _ = _roots(monkeypatch, crud, tmp_path)
    _write_template(curated, "bindtpl-1759")
    if env_value is None:
        monkeypatch.delenv("HOST_TEMPLATES_PATH", raising=False)
    else:
        monkeypatch.setenv("HOST_TEMPLATES_PATH", env_value)

    await crud.create_agent_internal(
        AgentConfig(
            name=f"bind-{'unset' if env_value is None else 'empty'}",
            template="local:bindtpl-1759",
        ),
        _user(),
        None,
    )

    volumes = _agent_run_kwargs(ctx)["volumes"]
    sources = [s for s, v in volumes.items() if v["bind"] == "/template"]
    assert len(sources) == 1
    source = sources[0]
    # Never a bare name (that is a named volume, not a bind) and never relative.
    assert source != "bindtpl-1759"
    assert Path(source).is_absolute(), source
    assert source == str(crud._repo_local_templates_dir() / "bindtpl-1759")


# ===========================================================================
# Field-level malformation stays a DEGRADE, not a 400 and not a 500
# ===========================================================================


@pytest.mark.asyncio
async def test_malformed_field_still_creates_and_names_the_template(
    crud_env, monkeypatch, tmp_path, caplog
):
    """#1759 narrowed the PARSE, not the field mutation.

    A `template.yaml` that parses to a dict but carries a malformed FIELD
    (`shared_folders: "a string"` → `.get(...)` raises `AttributeError`) is
    still swallowed into a warning, on purpose: the file is valid YAML, the
    agent gets its template files, and only some `config` mutations are
    skipped. Tightening that to a 400 would reject templates that deploy today
    and is outside this issue's ACs.

    NOTE (ent#128): the trigger field was `credentials: "a string"` until
    `_resolve_local_template` started reading that block through the tolerant
    `credential_mcp_server_names()`. `credentials:` is no longer a trigger *by
    design* — it used to raise FIRST in this run of mutations, so one malformed
    key also cost the agent its `runtime:` and `shared_folders:` config. That is
    now covered by
    `test_ent128b2_credential_setup.py::test_malformed_credentials_does_not_cost_runtime_and_shared_folders`.
    `shared_folders:` is the surviving trigger and keeps this degrade path — and
    the two identifiers in its warning — under test.

    This pins two things the swallow must never lose:

    1. It stays a DEGRADE — the handler runs inside an `except`, so a bug in
       the handler itself (a missing attribute in the log call) would convert a
       graceful degrade into a 500 on the create path.
    2. The warning names the template AND the agent. Without them an operator
       has no way to find which agent came out subtly wrong.
    """
    crud, ctx = crud_env
    curated, _ = _roots(monkeypatch, crud, tmp_path)
    _write_template(
        curated,
        "badfield-1759",
        "type: business-assistant\nshared_folders: not-a-mapping\n",
    )

    with caplog.at_level("WARNING"):
        await crud.create_agent_internal(
            AgentConfig(name="degrade-1759", template="local:badfield-1759"),
            _user(),
            None,
        )

    # 1. Still created — no raise, container really ran.
    _agent_run_kwargs(ctx)

    # 2. The warning is greppable by template and by agent.
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    hits = [w for w in warnings if "badfield-1759" in w and "degrade-1759" in w]
    assert hits, (
        f"no warning named both the template and the agent; got: {warnings}"
    )
