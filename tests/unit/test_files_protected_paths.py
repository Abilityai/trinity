"""
Unit tests for the backend file-route deny list (#590, abilityai/trinity-enterprise#792).

Verifies the AISEC-C2 RCE-by-config bypass is closed at the platform boundary:
authenticated owners cannot overwrite .mcp.json (or other runtime/credential
config) via the file-write endpoint to inject attacker-controlled MCP tool
definitions.

The agent-server still re-validates server-side via EDIT_PROTECTED_PATHS —
defense in depth — but the backend is the authoritative gate for user-facing
writes.

ent#792: these tests used to run against an inline COPY of the deny logic, so
they could not see a bug in the shipped module — and it had one. A path with
exactly two leading slashes kept them through `posixpath.normpath`, so no
path-anchored pattern (`.ssh/*`, `.claude/settings.json`) nor the `skills.manage`
fence matched it, while the agent server's `Path.resolve()` collapsed `//` to `/`
and wrote the real file. Everything below imports the shipped functions. The
same issue extended the deny list to DELETE, where deleting a directory that
holds a protected path is refused too.

Module: src/backend/services/agent_service/files.py
Issue:  https://github.com/abilityai/trinity/issues/590
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from hypothesis import example, given, settings, strategies as st

from services.agent_service import files
from services.agent_service.files import _is_user_writable_path, _normalize_user_path


# ---- AISEC-C2 exact reproduction ----

class TestAisecC2Reproduction:
    """The exact attack chain from the AISEC scan (2026-04-28, scan 3aad5469).

    Sequence: PUT /files?path=.mcp.json with attacker JSON → restart → MCP
    tool runs as agent process → reads /proc/1/environ → exfil OAuth token.
    Closing the .mcp.json write breaks the chain at step 1.
    """

    def test_mcp_json_relative(self):
        """The pentest's literal request — relative path."""
        assert _is_user_writable_path(".mcp.json") is False

    def test_mcp_json_absolute(self):
        """Same target, absolute form — same outcome."""
        assert _is_user_writable_path("/home/developer/.mcp.json") is False

    def test_mcp_json_template(self):
        """envsubst-only template = same RCE, different file."""
        assert _is_user_writable_path(".mcp.json.template") is False

    def test_credentials_enc(self):
        """Overwriting .credentials.enc lets attacker swap encrypted backups
        before next import → write attacker creds on next startup."""
        assert _is_user_writable_path(".credentials.enc") is False


# ---- Other protected paths ----

class TestProtectedPaths:
    """Other paths in the deny list — credential / SSH / runtime config."""

    def test_env_file(self):
        assert _is_user_writable_path(".env") is False

    def test_env_local(self):
        assert _is_user_writable_path(".env.local") is False

    def test_env_production(self):
        assert _is_user_writable_path(".env.production") is False

    def test_ssh_authorized_keys(self):
        assert _is_user_writable_path(".ssh/authorized_keys") is False

    def test_ssh_private_key(self):
        assert _is_user_writable_path(".ssh/id_rsa") is False

    def test_aws_credentials(self):
        assert _is_user_writable_path(".aws/credentials") is False

    def test_gcp_credentials(self):
        assert _is_user_writable_path(".gcp/service-account.json") is False

    def test_claude_settings(self):
        assert _is_user_writable_path(".claude/settings.json") is False

    def test_claude_settings_local(self):
        assert _is_user_writable_path(".claude/settings.local.json") is False

    def test_trinity_dir(self):
        assert _is_user_writable_path(".trinity/persistent-state.yaml") is False

    def test_git_config(self):
        assert _is_user_writable_path(".git/config") is False

    def test_gitignore(self):
        assert _is_user_writable_path(".gitignore") is False

    def test_opt_trinity(self):
        assert _is_user_writable_path("/opt/trinity/hooks/file-guardrail.py") is False

    def test_etc_claude_code(self):
        assert _is_user_writable_path("/etc/claude-code/managed-settings.json") is False


# ---- Path traversal ----

class TestPathTraversal:
    """Lexical normalization defeats `..` traversal attempts that try to
    smuggle a denied path past basename matching."""

    def test_traversal_to_etc(self):
        # `../../etc/passwd` resolved from /home/developer → /etc/passwd
        assert _is_user_writable_path("../../etc/passwd") is False

    def test_traversal_to_proc(self):
        assert _is_user_writable_path("../../proc/1/environ") is False

    def test_traversal_to_mcp_json(self):
        # Traversal back to .mcp.json should still be caught
        assert _is_user_writable_path("content/../.mcp.json") is False

    def test_dot_segments_to_env(self):
        assert _is_user_writable_path("./.env") is False

    def test_double_dot_to_credentials_enc(self):
        assert _is_user_writable_path("subdir/../.credentials.enc") is False


# ---- Allowed paths (regression — legit writes must keep working) ----

class TestAllowedPaths:
    """Paths the user CAN write — these must not be blocked."""

    def test_content_directory(self):
        assert _is_user_writable_path("content/notes.md") is True

    def test_content_subdirectory(self):
        assert _is_user_writable_path("content/reports/q1.txt") is True

    def test_workspace_file(self):
        assert _is_user_writable_path("workspace/script.py") is True

    def test_claude_md(self):
        """Owners DO edit CLAUDE.md — agent instructions are user-managed."""
        assert _is_user_writable_path("CLAUDE.md") is True

    def test_template_yaml(self):
        """template.yaml is metadata, not runtime config — editable."""
        assert _is_user_writable_path("template.yaml") is True

    def test_arbitrary_user_file(self):
        assert _is_user_writable_path("my-data.json") is True

    def test_nested_user_file(self):
        assert _is_user_writable_path("projects/foo/bar.txt") is True


# ---- Edge cases ----

class TestEdgeCases:
    """Boundary conditions in the normalizer."""

    def test_empty_path(self):
        # Empty path normalizes to "" → can't match → not writable (safe default)
        assert _is_user_writable_path("") is False

    def test_root_directory(self):
        # / normalizes to / — basename is empty, doesn't match any pattern,
        # but writing to / is nonsensical. The agent-server's "must be under
        # /home/developer" check catches this layer; the deny list lets it
        # through here.
        assert _is_user_writable_path("/") is True

    def test_home_developer_root(self):
        # The base directory itself
        assert _is_user_writable_path("/home/developer") is True

    def test_case_sensitivity_env(self):
        """.ENV is NOT .env — case-sensitive match (matches agent-server)."""
        assert _is_user_writable_path(".ENV") is True

    def test_subdir_env(self):
        """`subdir/.env` IS protected — basename matches."""
        assert _is_user_writable_path("subdir/.env") is False


# ---- ent#792: leading slashes --------------------------------------------------

class TestLeadingSlashes:
    """POSIX lets `normpath` keep exactly two leading slashes; Linux, and the
    agent server's `Path.resolve()`, treat them as one. The deny verdict must
    not depend on how many there are."""

    def test_two_slashes_normalize_to_one(self):
        assert _normalize_user_path("//home/developer/x") == "/home/developer/x"

    @pytest.mark.parametrize("slashes", ["/", "//", "///", "////"])
    @pytest.mark.parametrize("rest", [
        "home/developer/.ssh/authorized_keys",
        "home/developer/.claude/settings.json",
        "home/developer/.claude/settings.local.json",
        "home/developer/.aws/credentials",
        "home/developer/.mcp.json",
        "opt/trinity/hooks/file-guardrail.py",
        "etc/claude-code/managed-settings.json",
    ])
    def test_any_number_of_leading_slashes_is_refused(self, slashes, rest):
        assert _is_user_writable_path(slashes + rest) is False

    def test_dot_segment_after_two_slashes(self):
        # normpath('//./home/developer/.ssh/x') keeps the `//` too
        assert _is_user_writable_path("//./home/developer/.ssh/x") is False


# ---- ent#792: DELETE -------------------------------------------------------------

# Anchors of the path-anchored deny patterns, as derived in files.py. Pinned as a
# literal so a new pattern is reviewed for what it means on DELETE, not just
# picked up silently.
_EXPECTED_ANCHORS = (
    "/home/developer/.ssh",
    "/home/developer/.aws",
    "/home/developer/.gcp",
    "/home/developer/.claude/settings.json",
    "/home/developer/.claude/settings.local.json",
    "/home/developer/.git/config",          # ent#819
    "/home/developer/.claude/.credentials.json",   # ent#823
    "/home/developer/.gemini/settings.json",       # ent#823
    "/home/developer/.tmp/codex",                  # ent#823: deleting .tmp is refused
    "/home/developer/.trinity",
    "/home/developer/.git",
    "/opt/trinity",
    "/etc/claude-code",
    "/etc",
    "/sys",
    "/proc",
    "/dev",                                 # ent#819: /dev/fd/N reaches an open file
)


def test_delete_anchors_are_the_reviewed_set():
    assert files._DENY_ANCHORS == _EXPECTED_ANCHORS


@pytest.mark.parametrize("pattern,anchor", [
    (".ssh/*", "/home/developer/.ssh"),
    (".claude/settings.json", "/home/developer/.claude/settings.json"),   # no glob: itself
    ("/opt/trinity/*", "/opt/trinity"),
    # A glob inside a segment anchors at the directory ABOVE it: cutting at the
    # glob would give `.aws`, which is not an ancestor of `.aws-prod/`.
    (".aws*/credentials", "/home/developer"),
    ("/etc/claude-*/x", "/etc"),
    ("/*", "/"),
])
def test_an_anchor_is_cut_back_to_the_last_slash_before_the_first_glob(pattern, anchor):
    assert files._deny_anchor(pattern) == anchor


class TestDeletablePaths:
    """Deleting a directory removes everything under it, so DELETE refuses a
    protected path AND any directory that holds one."""

    @pytest.mark.parametrize("path", [
        ".ssh", ".ssh/", ".ssh/id_rsa", "./.ssh", ".//.ssh",
        ".aws", ".gcp", ".trinity", ".git",
        ".claude/settings.json", ".claude/settings.local.json",
        ".claude",                               # holds settings.json
        "/home/developer", "/home/developer/", "/", "", ".", "..",
        "//home/developer/.ssh", "///home/developer/.claude",
        ".env", ".env.example", "frontend/.env.production", ".credentials.enc",
        ".mcp.json", "sub/.mcp.json",
        "/opt", "/opt/trinity", "/etc",
        # /proc symlinks resolve into the home dir in the agent container
        "/proc/self/root/home/developer/.claude/skills",
        "/proc/self/cwd/.ssh",
        "/proc/thread-self/root/home/developer/.ssh",
    ])
    def test_refused(self, path):
        assert files._is_user_deletable_path(path) is False

    @pytest.mark.parametrize("path", [
        "notes.md", "docs", "docs/old", "content/reports/q1.txt",
        ".claude/skills/x", ".claude/skills", ".claude/agents", ".claude/projects",
        ".sshX", ".claude/settings.jsonX", "x/.ssh", "/etcX",
        # a string prefix of an anchor is not a directory above it
        "/home/developer/.ss", "/et", "/home/dev", ".claude/settings",
        "CLAUDE.md",          # the agent server's own by-name block decides this one
    ])
    def test_allowed(self, path):
        assert files._is_user_deletable_path(path) is True


# ---- ent#792: properties ----------------------------------------------------------

_SEGMENTS = st.sampled_from([
    "", ".", "..", "home", "developer", ".ssh", ".claude", "settings.json",
    "skills", "etc", "opt", "trinity", "proc", "x",
    "dev", ".ss", "settings", "et",            # string prefixes of real segments
])
_PATHS = st.builds(
    lambda lead, segs: "/" * lead + "/".join(segs),
    st.integers(min_value=0, max_value=4),
    st.lists(_SEGMENTS, min_size=1, max_size=6),
).filter(bool)
_PROPERTY = settings(max_examples=300, deadline=None, derandomize=True, database=None)


def _lexical(path: str) -> str:
    """Symlink-free lexical resolution, segment by segment: what the agent
    server's `Path.resolve()` computes when no component is a link. Written
    without `posixpath` so it cannot share its quirks."""
    stack = [] if path.startswith("/") else ["home", "developer"]
    for seg in path.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if stack:
                stack.pop()
            continue
        stack.append(seg)
    return "/" + "/".join(stack)


def _holds_an_anchor(normalized: str) -> bool:
    """True when `normalized` is an anchor or a directory above one, decided on
    whole path segments, written apart from the shipped string comparison."""
    mine = [p for p in normalized.split("/") if p]
    return any([p for p in a.split("/") if p][:len(mine)] == mine for a in _EXPECTED_ANCHORS)


@_PROPERTY
@given(_PATHS)
@example("//home/developer/.ssh/x")
@example("//")
def test_normalized_form_matches_the_agent_servers_resolution(path):
    normalized = _normalize_user_path(path)
    assert normalized == _lexical(path)
    assert not normalized.startswith("//")


@_PROPERTY
@given(_PATHS, st.integers(min_value=2, max_value=4))
@example("/home/developer/.ssh/x", 2)
@example("/home/developer/.claude/skills/x", 2)
def test_extra_leading_slashes_never_change_a_verdict(path, extra):
    one = "/" + path.lstrip("/")
    many = "/" * extra + path.lstrip("/")
    assert _is_user_writable_path(many) == _is_user_writable_path(one)
    assert files._is_user_deletable_path(many) == files._is_user_deletable_path(one)
    for ancestors in (False, True):
        assert (files._touches_skills_dir(many, include_ancestors=ancestors)
                == files._touches_skills_dir(one, include_ancestors=ancestors))


@_PROPERTY
@given(_PATHS)
@example(".claude")
@example("/")
def test_delete_is_refused_exactly_when_writing_is_or_the_path_holds_an_anchor(path):
    expected = _is_user_writable_path(path) and not _holds_an_anchor(_lexical(path))
    assert files._is_user_deletable_path(path) is expected


# ---- ent#792: through the route logic ---------------------------------------------

AGENT = "sibling-agent"


def _human():
    # mcp_scope=None is the JWT principal: it passes the skills fence by scope,
    # so a refusal below can only come from the deny list.
    return SimpleNamespace(id=1, username="owner", email="o@example.com", role="admin",
                           mcp_scope=None, agent_name=None, connector_agent=None,
                           mcp_key_id=None)


def _request():
    return SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), method="DELETE",
                           scope={"path": f"/api/agents/{AGENT}/files"},
                           state=SimpleNamespace(request_id="r1"))


def _call(fn, path):
    logic = getattr(files, fn)
    if fn == "update_agent_file_logic":
        return asyncio.run(logic(AGENT, path, "body", _human(), _request()))
    return asyncio.run(logic(AGENT, path, _human(), _request()))


@pytest.fixture
def routes(monkeypatch):
    monkeypatch.setattr(files.db, "can_user_access_agent", lambda u, a: True)
    # Past every gate, the next thing each logic function does is look up the
    # container; "Agent not found" is the observable proof nothing refused it.
    monkeypatch.setattr(files, "get_agent_container", lambda name: None)
    return files


_REFUSAL = {
    "update_agent_file_logic": "Cannot edit protected path: {}",
    "create_agent_folder_logic": "Cannot create folder in protected path: {}",
    "delete_agent_file_logic": "Cannot delete protected path: {}",
}


@pytest.mark.parametrize("fn", list(_REFUSAL))
@pytest.mark.parametrize("path", [
    "//home/developer/.ssh/authorized_keys",
    "//home/developer/.claude/settings.json",
    "///home/developer/.aws/credentials",
])
def test_a_double_slash_path_is_refused_on_every_write_route(routes, fn, path):
    with pytest.raises(HTTPException) as exc:
        _call(fn, path)
    assert exc.value.status_code == 403
    assert exc.value.detail == _REFUSAL[fn].format(path)


@pytest.mark.parametrize("method,url,request_kwargs,refusal", [
    ("PUT", f"/api/agents/{AGENT}/files",
     lambda p: {"params": {"path": p}, "json": {"content": "x"}}, "Cannot edit protected path: "),
    # mkdir takes the path in the JSON body (CreateFolderRequest), not the query
    ("POST", f"/api/agents/{AGENT}/files/mkdir",
     lambda p: {"json": {"path": p}}, "Cannot create folder in protected path: "),
    ("DELETE", f"/api/agents/{AGENT}/files",
     lambda p: {"params": {"path": p}}, "Cannot delete protected path: "),
])
def test_the_real_routes_refuse_a_double_slash_path(routes, method, url, request_kwargs, refusal):
    """Through the FastAPI router as a client calls it, request shape included."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers import agent_files

    app = FastAPI()
    app.include_router(agent_files.router)
    # the dependency object the route captured at import, not a fresh import
    app.dependency_overrides[agent_files.get_current_user] = _human
    path = "//home/developer/.ssh/authorized_keys"
    r = TestClient(app).request(method, url, **request_kwargs(path))
    assert r.status_code == 403, (method, r.status_code, r.text)
    assert r.json()["detail"] == refusal + path


@pytest.mark.parametrize("path", [
    ".ssh", ".claude/settings.json", ".claude", ".credentials.enc", ".env.local",
    "/proc/self/root/home/developer/.claude/skills", "/proc/self/cwd/.ssh",
])
def test_delete_refuses_protected_paths_for_the_owner(routes, path):
    with pytest.raises(HTTPException) as exc:
        _call("delete_agent_file_logic", path)
    assert exc.value.status_code == 403
    assert exc.value.detail == f"Cannot delete protected path: {path}"


@pytest.mark.parametrize("path", ["notes.md", "docs/old", ".claude/skills/x", ".claude/agents"])
def test_delete_of_an_ordinary_path_reaches_the_container_lookup(routes, path):
    with pytest.raises(HTTPException) as exc:
        _call("delete_agent_file_logic", path)
    assert exc.value.status_code == 404
    assert exc.value.detail == "Agent not found"


@pytest.mark.parametrize("fn,path,forwarded", [
    ("update_agent_file_logic", "content/../notes.md", "/home/developer/notes.md"),
    ("update_agent_file_logic", "//home/developer/notes.md", "/home/developer/notes.md"),
    ("create_agent_folder_logic", "docs/new", "/home/developer/docs/new"),
    ("delete_agent_file_logic", "//home/developer/docs/old", "/home/developer/docs/old"),
])
def test_the_agent_receives_the_path_that_was_checked(monkeypatch, fn, path, forwarded):
    """The check and the action read one string: the agent is sent the
    normalised path the deny list approved, never the raw input."""
    monkeypatch.setattr(files.db, "can_user_access_agent", lambda u, a: True)
    monkeypatch.setattr(files, "get_agent_container", lambda name: SimpleNamespace(status="running"))
    monkeypatch.setattr(files, "container_reload", AsyncMock())
    sent = AsyncMock(return_value=SimpleNamespace(status_code=200, json=lambda: {"success": True}))
    monkeypatch.setattr(files, "agent_http_request", sent)

    assert _call(fn, path) == {"success": True}
    assert sent.await_args.kwargs["params"] == {"path": forwarded}


# ---- ent#819: the owner-tier read set and the write list -----------------------

# The only owner-tier read patterns that are NOT write-denied. Pinned as a
# literal: a new read-only entry is a reviewed decision, not a side effect.
_EXPECTED_READ_ONLY = (".kube/config", ".config/gcloud/*", "*.key", "*.pem", "*.p12", "*.pfx")


def test_the_read_only_owner_tier_patterns_are_the_reviewed_set():
    assert files._OWNER_TIER_READ_ONLY_PATTERNS == _EXPECTED_READ_ONLY


@pytest.mark.parametrize("pattern", [
    p for p in files._OWNER_TIER_READ_PATTERNS if p not in _EXPECTED_READ_ONLY
])
def test_every_other_owner_tier_read_pattern_is_write_denied(pattern):
    instance = pattern.replace("*", "x")
    assert files._is_owner_tier_read_path(instance) is True
    assert _is_user_writable_path(instance) is False


@_PROPERTY
@given(_PATHS)
@example("/home/developer/.git/config")
@example("/dev/fd/7")
def test_an_owner_tier_path_outside_the_read_only_set_is_never_writable(path):
    if files._matches_any(path, files._OWNER_TIER_READ_PATTERNS) and not files._matches_any(
        path, _EXPECTED_READ_ONLY
    ):
        assert _is_user_writable_path(path) is False


@pytest.mark.parametrize("path", [
    ".trinity/pipelines/x.yaml", ".trinity/pipeline-state/p/i.json",
    "CLAUDE.md", "template.yaml", ".git/HEAD", "certs/ca.crt",
])
def test_the_platforms_shared_reads_are_not_owner_tier(path):
    assert files._is_owner_tier_read_path(path) is False


# ---- ent#823: the runtime config files (Claude Code, Gemini, Codex) --------------

_RUNTIME_CONFIG_SPELLINGS = [
    ".claude.json", "/home/developer/.claude.json", "//home/developer/.claude.json",
    ".claude/.credentials.json", "//home/developer/.claude/.credentials.json",
    ".gemini/settings.json", "/home/developer/.gemini/settings.json",
    ".tmp/codex/auth.json", ".tmp/codex/config.toml", "//home/developer/.tmp/codex/x",
]


@pytest.mark.parametrize("path", _RUNTIME_CONFIG_SPELLINGS)
def test_runtime_config_files_are_not_writable_nor_deletable(path):
    assert _is_user_writable_path(path) is False
    assert files._is_user_deletable_path(path) is False


@pytest.mark.parametrize("path", _RUNTIME_CONFIG_SPELLINGS)
def test_runtime_config_files_are_owner_tier_to_read(path):
    assert files._is_owner_tier_read_path(path) is True


@pytest.mark.parametrize("path", [".tmp", ".tmp/", ".tmp/codex", "/home/developer/.tmp", ".gemini", ".claude"])
def test_deleting_a_directory_that_holds_runtime_config_is_refused(path):
    assert files._is_user_deletable_path(path) is False


@pytest.mark.parametrize("path", [".tmp/other.txt", ".tmp/scratch/x", ".claude/agents/a.md", "notes/.claude.jsonx"])
def test_neighbours_of_runtime_config_stay_writable(path):
    assert _is_user_writable_path(path) is True
    assert files._is_user_deletable_path(path) is True


@pytest.mark.parametrize("fn", list(_REFUSAL))
@pytest.mark.parametrize("path", [
    ".claude.json", "/home/developer/.claude.json", ".claude/.credentials.json",
    "//home/developer/.claude/.credentials.json", ".gemini/settings.json",
    ".tmp/codex/auth.json", ".tmp/codex/config.toml",
])
def test_every_write_route_refuses_runtime_config(routes, fn, path):
    with pytest.raises(HTTPException) as exc:
        _call(fn, path)
    assert exc.value.status_code == 403
    assert exc.value.detail == _REFUSAL[fn].format(path)


@pytest.mark.parametrize("method,url,request_kwargs,refusal", [
    ("PUT", f"/api/agents/{AGENT}/files",
     lambda p: {"params": {"path": p}, "json": {"content": "x"}}, "Cannot edit protected path: "),
    ("POST", f"/api/agents/{AGENT}/files/mkdir",
     lambda p: {"json": {"path": p}}, "Cannot create folder in protected path: "),
    ("DELETE", f"/api/agents/{AGENT}/files",
     lambda p: {"params": {"path": p}}, "Cannot delete protected path: "),
])
@pytest.mark.parametrize("path", [".claude.json", "//home/developer/.claude/.credentials.json"])
def test_the_real_routes_refuse_the_claude_code_login_files(routes, method, url, request_kwargs, refusal, path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers import agent_files

    app = FastAPI()
    app.include_router(agent_files.router)
    app.dependency_overrides[agent_files.get_current_user] = _human
    r = TestClient(app).request(method, url, **request_kwargs(path))
    assert r.status_code == 403, (method, r.status_code, r.text)
    assert r.json()["detail"] == refusal + path


@pytest.mark.parametrize("path", [".tmp", ".tmp/codex"])
def test_delete_of_tmp_is_refused_through_the_route_logic(routes, path):
    with pytest.raises(HTTPException) as exc:
        _call("delete_agent_file_logic", path)
    assert exc.value.status_code == 403
