"""The agent Files surface: a ruled tier per verb (abilityai/trinity-enterprise#820).

Ruling (operator, 2026-10-15): a share is one flat grant — chat, see your own
conversations, browse and edit the agent's files EXCEPT credentials, runtime
config and its instructions. Instruction files are owner tier through the Files
tab (agent keys keep the ent#164 grant path). Below the owner tier the list is
projected (restricted nodes flagged, hidden files withheld). One audit row per
write, delete or mkdir.

Three parts:
1. The census: every `/files*` route in `routers/agent_files.py` is in ROUTE_TIERS
   with its verb and the gate its logic function runs. A new route without a
   row fails here — no route inherits the accessor tier by default.
2. The table: `file_path_policy.decide()` row by row, for each tier.
3. The routes: the real logic functions with real tier resolution, driven
   with an owner, an admin, a share holder (by JWT and by their own user-scope
   key) and an agent key — refused AND still allowed, both directions.
"""
import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from services import file_path_policy as policy
from services.agent_service import files

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
_ROUTER = _REPO / "src/backend/routers/agent_files.py"
_LOGIC = _REPO / "src/backend/services/agent_service/files.py"

AGENT = "shared-agent"
OWNER, ADMIN, SHARED = "owner", "admin-user", "teammate"


# ---- 1. the census ---------------------------------------------------------------

# route (method, path) -> (logic function, verb, the gate that function must run,
# reason). Reads keep the ent#819 owner-tier read gate; every other verb runs
# the ent#820 tier gate.
ROUTE_TIERS = {
    ("GET", "/{agent_name}/files"): (
        "list_agent_files_logic", policy.LIST, "_enforce_tier",
        "a share holder lists non-hidden files; hidden and credential dirs are owner tier"),
    ("GET", "/{agent_name}/files/download"): (
        "download_agent_file_logic", policy.READ, "_enforce_owner_tier_read",
        "credential, runtime, secret-class reads are owner tier (ent#819)"),
    ("GET", "/{agent_name}/files/preview"): (
        "preview_agent_file_logic", policy.READ, "_enforce_owner_tier_read",
        "same as download (ent#819)"),
    ("PUT", "/{agent_name}/files"): (
        "update_agent_file_logic", policy.WRITE, "_enforce_tier",
        "instructions, skills and files that run code are owner tier"),
    ("POST", "/{agent_name}/files/mkdir"): (
        "create_agent_folder_logic", policy.WRITE, "_enforce_tier",
        "a folder is a write"),
    ("DELETE", "/{agent_name}/files"): (
        "delete_agent_file_logic", policy.DELETE, "_enforce_tier",
        "as write, and a directory above an owner-tier location counts as it"),
}


def _file_routes():
    tree = ast.parse(_ROUTER.read_text())
    found = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for dec in node.decorator_list:
            if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                    and dec.func.attr in ("get", "put", "post", "delete", "patch")
                    and dec.args and isinstance(dec.args[0], ast.Constant)
                    and "/files" in dec.args[0].value):
                found[(dec.func.attr.upper(), dec.args[0].value)] = node
    return found


def _calls_in(function_source_node):
    return {n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", None)
            for n in ast.walk(function_source_node) if isinstance(n, ast.Call)}


def test_every_files_route_has_a_ruled_tier():
    routes = _file_routes()
    assert routes, "the census found no /files routes — the scanner is blind"
    assert set(routes) == set(ROUTE_TIERS), (
        "a /files route was added or removed without a ruled tier: "
        f"{set(routes) ^ set(ROUTE_TIERS)}"
    )


@pytest.mark.parametrize("route", list(ROUTE_TIERS))
def test_each_route_calls_its_logic_and_the_logic_runs_its_gate(route):
    logic_name, _verb, gate, _reason = ROUTE_TIERS[route]
    assert logic_name in _calls_in(_file_routes()[route])
    logic = {n.name: n for n in ast.walk(ast.parse(_LOGIC.read_text()))
             if isinstance(n, ast.AsyncFunctionDef)}[logic_name]
    assert gate in _calls_in(logic), f"{logic_name} no longer runs {gate}"


# ---- 2. the table ---------------------------------------------------------------------

ROWS = [
    # (path, verb, owner, share, key) — None allowed, else the refusal code
    ("workspace/notes.md", policy.WRITE, None, None, None),
    ("workspace/notes.md", policy.DELETE, None, None, None),
    ("CLAUDE.md", policy.WRITE, None, policy.OWNER_TIER, None),
    ("CLAUDE.md", policy.DELETE, policy.PROTECTED, policy.PROTECTED, policy.PROTECTED),
    ("CLAUDE.local.md", policy.WRITE, None, policy.OWNER_TIER, None),
    ("AGENTS.md", policy.WRITE, None, policy.OWNER_TIER, None),
    ("GEMINI.md", policy.WRITE, None, policy.OWNER_TIER, None),
    (".gemini/GEMINI.md", policy.WRITE, None, policy.OWNER_TIER, None),
    (".claude/agents/a.md", policy.WRITE, None, policy.OWNER_TIER, None),
    (".claude/skills/x/SKILL.md", policy.WRITE, None, policy.OWNER_TIER, None),
    (".claude", policy.DELETE, policy.PROTECTED, policy.PROTECTED, policy.PROTECTED),
    ("docs/CLAUDE.md", policy.WRITE, None, None, None),
    (".bashrc", policy.WRITE, None, policy.OWNER_TIER, policy.PROTECTED),
    (".gitconfig", policy.WRITE, None, policy.OWNER_TIER, policy.PROTECTED),
    (".config/git/config", policy.WRITE, None, policy.OWNER_TIER, policy.PROTECTED),
    (".local/bin/git", policy.WRITE, None, policy.OWNER_TIER, policy.PROTECTED),
    # the Python user-site runs as the agent at the next `python3` (review of #3776)
    (".local/lib/python3.13/site-packages/zz.pth", policy.WRITE, None, policy.OWNER_TIER, policy.PROTECTED),
    (".local/lib/python3.13/site-packages/usercustomize.py", policy.WRITE, None, policy.OWNER_TIER, policy.PROTECTED),
    (".zshenv", policy.WRITE, None, policy.OWNER_TIER, policy.PROTECTED),
    (".zprofile", policy.WRITE, None, policy.OWNER_TIER, policy.PROTECTED),
    (".zlogin", policy.WRITE, None, policy.OWNER_TIER, policy.PROTECTED),
    (".bash_login", policy.WRITE, None, policy.OWNER_TIER, policy.PROTECTED),
    (".bash_logout", policy.WRITE, None, policy.OWNER_TIER, policy.PROTECTED),
    (".local", policy.DELETE, None, policy.OWNER_TIER, policy.PROTECTED),
    (".env", policy.WRITE, policy.PROTECTED, policy.PROTECTED, policy.PROTECTED),
    (".credentials.enc", policy.DELETE, policy.PROTECTED, policy.PROTECTED, policy.PROTECTED),
    (".ssh/id_rsa", policy.DELETE, policy.PROTECTED, policy.PROTECTED, policy.PROTECTED),
    (".claude/settings.json", policy.DELETE, policy.PROTECTED, policy.PROTECTED, policy.PROTECTED),
    (".claude.json", policy.DELETE, policy.PROTECTED, policy.PROTECTED, policy.PROTECTED),
    ("workspace/r/.git/config", policy.WRITE, policy.PROTECTED, policy.PROTECTED, policy.PROTECTED),
    ("workspace/r/.git/hooks/pre-commit", policy.WRITE, policy.PROTECTED, policy.PROTECTED, policy.PROTECTED),
    ("workspace/r.git/hooks/post-update", policy.WRITE, policy.PROTECTED, policy.PROTECTED, policy.PROTECTED),
    ("", policy.WRITE, policy.PROTECTED, policy.PROTECTED, policy.PROTECTED),
    (".env", policy.READ, None, policy.OWNER_TIER, policy.PERSON_REQUIRED),
    (".netrc", policy.READ, None, policy.OWNER_TIER, policy.PERSON_REQUIRED),
    (".git-credentials", policy.READ, None, policy.OWNER_TIER, policy.PERSON_REQUIRED),
    (".docker/config.json", policy.READ, None, policy.OWNER_TIER, policy.PERSON_REQUIRED),
    (".config/gh/hosts.yml", policy.READ, None, policy.OWNER_TIER, policy.PERSON_REQUIRED),
    (".gemini/oauth_creds.json", policy.READ, None, policy.OWNER_TIER, policy.PERSON_REQUIRED),
    ("CLAUDE.md", policy.READ, None, None, None),
    ("/home/developer", policy.LIST, None, None, None),
    ("workspace", policy.LIST, None, None, None),
    (".ssh", policy.LIST, None, policy.OWNER_TIER, None),
    (".claude", policy.LIST, None, policy.OWNER_TIER, None),
    ("workspace/.cache", policy.LIST, None, policy.OWNER_TIER, None),
    # the MCP pipeline tools list these for anyone with access (CLAUDE.md rule 8)
    (".trinity/pipelines", policy.LIST, None, None, None),
    (".trinity/pipeline-state/p1", policy.LIST, None, None, None),
    (".trinity", policy.LIST, None, policy.OWNER_TIER, None),
    (".trinity/pipelines-x", policy.LIST, None, policy.OWNER_TIER, None),
]


@pytest.mark.parametrize("path,verb,owner,share,key", ROWS)
def test_the_table(path, verb, owner, share, key):
    for tier, expected in ((policy.OWNER, owner), (policy.SHARE, share), (policy.KEY, key)):
        assert policy.decide(tier, verb, path).code == expected, (tier, verb, path)


def test_the_policy_read_floor_is_the_one_the_read_gate_uses():
    for path in ("x.pem", ".env", ".ssh/k", ".kube/config", "/proc/self/root/x", "a.md", "CLAUDE.md"):
        assert policy.owner_tier_read(path) == files._is_owner_tier_read_path(path)
        assert policy.decide(policy.SHARE, policy.READ, path).allowed == (
            not files._is_owner_tier_read_path(path))


def test_unknown_tiers_and_verbs_raise():
    with pytest.raises(ValueError):
        policy.decide("guest", policy.READ, "a")
    with pytest.raises(ValueError):
        policy.decide(policy.OWNER, "chmod", "a")


# ---- 3. the routes ------------------------------------------------------------------------

def _user(username, role="user", mcp_scope=None, agent_name=None):
    return SimpleNamespace(id=hash(username) % 1000, username=username,
                           email=f"{username}@example.com", role=role, mcp_scope=mcp_scope,
                           agent_name=agent_name, connector_agent=None, mcp_key_id=None,
                           mcp_key_name=None)


PRINCIPALS = {
    "owner": lambda: _user(OWNER),
    "admin": lambda: _user(ADMIN, role="admin"),
    "share": lambda: _user(SHARED),
    "share_user_key": lambda: _user(SHARED, mcp_scope="user"),
    "agent_key": lambda: _user(OWNER, mcp_scope="agent", agent_name="sibling"),
}


def _request(method="PUT"):
    return SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), method=method,
                           scope={"path": f"/api/agents/{AGENT}/files"},
                           state=SimpleNamespace(request_id="req-1"))


@pytest.fixture
def agent(monkeypatch):
    import dependencies
    monkeypatch.setattr(files.db, "can_user_access_agent", lambda u, a: True)
    monkeypatch.setattr(files, "agent_may_reach", lambda u, a, **kw: True)
    for mod in (files.db, dependencies.db):
        monkeypatch.setattr(mod, "can_user_share_agent", lambda u, a: u in (OWNER, ADMIN))
        monkeypatch.setattr(mod, "get_agent_owner", lambda a: {"owner_username": OWNER})
    # capability gates (ent#164 / ent#596) pass for every principal here: the
    # tier is what is under test; capability refusals are pinned in their own files
    monkeypatch.setattr(files, "_require_skill_capability", AsyncMock())
    monkeypatch.setattr(files, "_may_reach_for_path", lambda *a, **k: True)
    container = SimpleNamespace(status="running", id="c-1", attrs={"Image": "sha256:img"})
    monkeypatch.setattr(files, "get_agent_container", lambda name: container)
    monkeypatch.setattr(files, "container_reload", AsyncMock())
    sent = AsyncMock(return_value=SimpleNamespace(
        status_code=200, json=lambda: {"success": True, "tree": [
            {"name": "CLAUDE.md", "path": "CLAUDE.md", "type": "file"},
            {"name": "notes.md", "path": "notes.md", "type": "file"},
            {"name": "x", "path": "x", "type": "link"},
            {"name": "workspace", "path": "workspace", "type": "directory", "children": [
                {"name": "k.pem", "path": "workspace/k.pem", "type": "file"}]},
        ]}, text="", headers={}))
    monkeypatch.setattr(files, "agent_http_request", sent)
    probe = AsyncMock(return_value={"exit_code": 0, "output": "", "timed_out": False})
    monkeypatch.setattr(files, "execute_command_in_container", probe)
    monkeypatch.setattr(files, "_READ_POLICY_PROBE_CACHE", {})
    monkeypatch.setattr(files, "_WRITE_POLICY_PROBE_CACHE", {})
    from services.platform_audit_service import platform_audit_service
    log = AsyncMock()
    monkeypatch.setattr(platform_audit_service, "log", log)
    return SimpleNamespace(sent=sent, probe=probe, log=log)


def _write(fn, path, who):
    user = PRINCIPALS[who]()
    if fn == "update_agent_file_logic":
        return asyncio.run(files.update_agent_file_logic(AGENT, path, "x", user, _request()))
    return asyncio.run(getattr(files, fn)(AGENT, path, user, _request()))


MUTATIONS = ("update_agent_file_logic", "create_agent_folder_logic", "delete_agent_file_logic")


_OWNER_TIER_PATHS = ["CLAUDE.md", ".claude/agents/a.md", ".claude/skills/x/SKILL.md",
                     ".bashrc", ".local/bin/tool", ".zshenv",
                     ".local/lib/python3.13/site-packages/zz.pth"]
# CLAUDE.md is deletable by no one (test_no_one_deletes_claude_md_through_the_routes)
_OWNER_TIER_CASES = [(fn, p) for fn in MUTATIONS for p in _OWNER_TIER_PATHS
                     if not (fn == "delete_agent_file_logic" and p == "CLAUDE.md")]


@pytest.mark.parametrize("fn,path", _OWNER_TIER_CASES)
@pytest.mark.parametrize("who", ["share", "share_user_key"])
def test_a_share_holder_cannot_change_instructions_skills_or_startup_files(agent, fn, path, who):
    with pytest.raises(HTTPException) as exc:
        _write(fn, path, who)
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == policy.OWNER_TIER
    agent.sent.assert_not_awaited()
    (call,) = agent.log.await_args_list
    assert call.kwargs["event_action"].endswith("_refused")
    assert call.kwargs["details"]["tier"] == policy.SHARE


@pytest.mark.parametrize("fn,path", [("update_agent_file_logic", "CLAUDE.md"),
                                     ("create_agent_folder_logic", ".claude/agents"),
                                     ("delete_agent_file_logic", ".claude/agents/a.md")])
@pytest.mark.parametrize("who", ["owner", "admin"])
def test_the_owner_and_an_admin_still_change_the_instructions(agent, fn, path, who):
    assert _write(fn, path, who)["success"] is True
    agent.sent.assert_awaited_once()


@pytest.mark.parametrize("who", list(PRINCIPALS))
def test_no_one_deletes_claude_md_through_the_routes(agent, who):
    """The agent server refuses it by name; the route says so first."""
    with pytest.raises(HTTPException) as exc:
        _write("delete_agent_file_logic", "CLAUDE.md", who)
    assert exc.value.status_code == 403


@pytest.mark.parametrize("fn", MUTATIONS)
def test_an_agent_key_keeps_its_capability_path_to_the_instructions(agent, fn):
    assert _write(fn, ".claude/agents/a.md", "agent_key")["success"] is True


def test_an_agent_key_cannot_write_a_file_that_runs_code(agent):
    with pytest.raises(HTTPException) as exc:
        _write("update_agent_file_logic", ".bashrc", "agent_key")
    assert exc.value.detail["code"] == policy.PROTECTED


@pytest.mark.parametrize("fn", MUTATIONS)
@pytest.mark.parametrize("who", list(PRINCIPALS))
def test_every_tier_still_changes_ordinary_files(agent, fn, who):
    assert _write(fn, "workspace/notes.md", who)["success"] is True


@pytest.mark.parametrize("fn,action", [("update_agent_file_logic", "file_write"),
                                       ("create_agent_folder_logic", "file_mkdir"),
                                       ("delete_agent_file_logic", "file_delete")])
@pytest.mark.parametrize("who", ["owner", "share", "agent_key"])
def test_one_audit_row_per_mutation_for_every_caller(agent, fn, action, who):
    _write(fn, "workspace/notes.md", who)
    (call,) = agent.log.await_args_list
    from services.platform_audit_service import AuditEventType
    assert call.kwargs["event_type"] == AuditEventType.AGENT_FILES
    assert call.kwargs["event_action"] == action
    assert call.kwargs["target_id"] == AGENT
    assert call.kwargs["details"]["path"] == "/home/developer/workspace/notes.md"
    assert call.kwargs["details"]["unverified_image"] is False


def test_an_audit_failure_does_not_fail_a_write_that_happened(agent):
    agent.log.side_effect = RuntimeError("audit down")
    assert _write("update_agent_file_logic", "workspace/notes.md", "share")["success"] is True


# -- unverified image (R6) --

@pytest.fixture
def old_image(agent):
    agent.probe.return_value = {"exit_code": 1, "output": "", "timed_out": False}
    return agent


def test_a_share_holder_cannot_write_to_an_agent_on_an_old_image(old_image):
    with pytest.raises(HTTPException) as exc:
        _write("update_agent_file_logic", "workspace/notes.md", "share")
    assert exc.value.detail["code"] == "agent_restart_required"
    old_image.sent.assert_not_awaited()


def test_an_agent_key_still_writes_to_an_old_image_and_the_row_says_so(old_image):
    assert _write("update_agent_file_logic", ".claude/agents/a.md", "agent_key")["success"] is True
    (call,) = old_image.log.await_args_list
    assert call.kwargs["details"]["unverified_image"] is True


def test_the_owner_writes_to_an_old_image_without_a_probe(old_image):
    assert _write("update_agent_file_logic", "CLAUDE.md", "owner")["success"] is True
    old_image.probe.assert_not_awaited()


# -- the list --

def _list(who, path="/home/developer", show_hidden=True):
    return asyncio.run(files.list_agent_files_logic(AGENT, path, PRINCIPALS[who](),
                                                    _request("GET"), show_hidden))


def test_a_share_holder_gets_no_hidden_files(agent):
    data = _list("share")
    assert agent.sent.await_args.kwargs["params"]["show_hidden"] == "false"
    assert data["hidden_withheld"] is True and data["tier"] == policy.SHARE


@pytest.mark.parametrize("who", ["owner", "admin", "agent_key"])
def test_others_keep_the_hidden_toggle(agent, who):
    data = _list(who)
    assert agent.sent.await_args.kwargs["params"]["show_hidden"] == "true"
    assert data["hidden_withheld"] is False


@pytest.mark.parametrize("path", [".ssh", "/home/developer/.claude", "workspace/.cache", ".env"])
def test_a_share_holder_cannot_list_a_hidden_or_credential_dir(agent, path):
    with pytest.raises(HTTPException) as exc:
        _list("share", path)
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == policy.OWNER_TIER
    agent.sent.assert_not_awaited()


def test_the_list_flags_what_this_caller_cannot_do(agent):
    nodes = {n["path"]: n for n in _list("share")["tree"]}
    assert nodes["CLAUDE.md"]["restricted"] == ["write", "delete"]
    assert nodes["notes.md"].get("restricted") is None
    assert "restricted" not in nodes["notes.md"]
    assert nodes["x"]["restricted"] == ["read", "write"]
    pem = nodes["workspace"]["children"][0]
    assert pem["restricted"] == ["read"]
    owner_nodes = {n["path"]: n for n in _list("owner")["tree"]}
    assert owner_nodes["CLAUDE.md"]["restricted"] == ["delete"]
    assert "restricted" not in owner_nodes["workspace"]["children"][0]


def test_a_share_holder_still_lists_the_published_pipeline_dirs(agent):
    """`list_agent_pipelines` (MCP) lists these with a share holder's own key."""
    for path in (".trinity/pipelines", ".trinity/pipeline-state/demo"):
        _list("share_user_key", path)
        assert agent.sent.await_args.kwargs["params"]["path"] == f"/home/developer/{path}"


def test_the_list_sends_the_normalised_path(agent):
    _list("owner", "//home/developer/workspace/../workspace")
    assert agent.sent.await_args.kwargs["params"]["path"] == "/home/developer/workspace"


# -- hard links (R9) --

def _read_response(nlink):
    return SimpleNamespace(status_code=200, text="BODY", content=b"BODY",
                           headers={"content-type": "text/plain", "X-File-Nlink": str(nlink)},
                           json=lambda: {})


@pytest.mark.parametrize("fn", ["download_agent_file_logic", "preview_agent_file_logic"])
def test_a_share_holder_cannot_read_a_file_with_a_second_name(agent, fn):
    agent.sent.return_value = _read_response(2)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(getattr(files, fn)(AGENT, "workspace/n.txt", PRINCIPALS["share"](), _request("GET")))
    assert exc.value.detail["code"] == "hardlinked_file"


@pytest.mark.parametrize("fn", ["download_agent_file_logic", "preview_agent_file_logic"])
def test_the_owner_reads_a_file_with_a_second_name(agent, fn):
    agent.sent.return_value = _read_response(2)
    asyncio.run(getattr(files, fn)(AGENT, "workspace/n.txt", PRINCIPALS["owner"](), _request("GET")))


def test_a_share_holder_reads_an_ordinary_file(agent):
    agent.sent.return_value = _read_response(1)
    r = asyncio.run(files.download_agent_file_logic(AGENT, "workspace/n.txt", PRINCIPALS["share"](),
                                                     _request("GET")))
    assert r.body == b"BODY"
