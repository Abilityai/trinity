"""
Agent Service Files - File browser operations.

Handles file listing, download, preview, and delete for agent workspaces.
"""
import fnmatch
import logging
import posixpath

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import PlainTextResponse, StreamingResponse

from models import User
from database import db
from services.docker_service import execute_command_in_container, get_agent_container
from services.docker_utils import container_reload
from .helpers import agent_http_request

logger = logging.getLogger(__name__)


# AISEC-C2 / #590 — backend-side deny list for PUT /api/agents/{name}/files.
# Mirrors the `path_deny` list in docker/base-image/hooks/guardrails-baseline.json
# so an authenticated owner cannot bypass the agent-server's EDIT_PROTECTED_PATHS
# check by exploiting any future router/proxy gap. Defense in depth — the
# agent-server still re-validates server-side. KEEP IN SYNC with both:
#   - docker/base-image/hooks/guardrails-baseline.json::path_deny
#   - docker/base-image/agent_server/routers/files.py::EDIT_PROTECTED_PATHS
#
# Credential paths (trinity-enterprise#819): never writable through these
# routes, and owner-tier to read. To add a path: (1) here; (2)
# hooks/guardrails-baseline.json::path_deny plus a guard002-smoke row; (3)
# agent_server PROTECTED_PATHS / EDIT_PROTECTED_PATHS by file or parent name,
# unless the name is too generic for a by-name match (say why here); (4) tests:
# test_files_protected_paths (+ _EXPECTED_ANCHORS), the hook test, the
# agent-server list test.
#
# Runtime config files (trinity-enterprise#823): each runtime's login and MCP
# config. Gemini's `settings.json` and Codex's `auth.json` / `config.toml` are
# too generic for the agent-server by-name lists; the agent server matches all
# four on the resolved path instead (`_RUNTIME_CONFIG_PATHS`). Codex keeps them
# in CODEX_HOME = $TMPDIR/codex = ~/.tmp/codex, so deleting `.tmp` is refused.
_RUNTIME_CONFIG_PATTERNS = (
    ".claude.json",
    ".claude/.credentials.json",
    ".gemini/settings.json",
    ".tmp/codex/*",
)
_CREDENTIAL_PATH_PATTERNS = (
    ".env",
    ".env.*",
    ".mcp.json",
    ".mcp.json.template",
    ".credentials.enc",
    ".ssh/*",
    ".aws/*",
    ".gcp/*",
    ".claude/settings.json",
    ".claude/settings.local.json",
    ".git/config",
) + _RUNTIME_CONFIG_PATTERNS
# Other spellings of a home-dir path: the agent server follows
# /proc/self/{cwd,root}/... and /dev/fd/N back into the home dir.
_PATH_ALIAS_PATTERNS = (
    "/proc/*",
    "/dev/*",
)
_FILE_WRITE_DENY_PATTERNS = _CREDENTIAL_PATH_PATTERNS + (
    ".trinity/*",
    ".git/*",
    ".gitignore",
    "/opt/trinity/*",
    "/etc/claude-code/*",
    "/etc/*",
    "/sys/*",
) + _PATH_ALIAS_PATTERNS
# Trinity-managed copies that may hold credentials; write-denied already by `.trinity/*`.
_TRINITY_CREDENTIAL_COPY_PATTERNS = (
    ".trinity/git-credential",
    ".trinity/backup/*",
)
# Secret file classes: owner-tier to read, deliberately NOT write-denied here
# (the per-verb tier follows separately).
_SECRET_FILE_CLASS_PATTERNS = (
    ".kube/config",
    ".config/gcloud/*",
    "*.key",
    "*.pem",
    "*.p12",
    "*.pfx",
)
# The only owner-tier read patterns that are not also write-denied (pinned by
# test_files_protected_paths).
_OWNER_TIER_READ_ONLY_PATTERNS = _SECRET_FILE_CLASS_PATTERNS
_OWNER_TIER_READ_PATTERNS = (
    _CREDENTIAL_PATH_PATTERNS
    + _PATH_ALIAS_PATTERNS
    + _TRINITY_CREDENTIAL_COPY_PATTERNS
    + _OWNER_TIER_READ_ONLY_PATTERNS
)


def _normalize_user_path(raw: str) -> str:
    """Reduce a user-supplied path to a stable absolute form for deny matching.

    Resolves `..`/`.` segments lexically (no FS access — mirrors the agent-server
    Path.resolve() guard). Non-absolute paths are anchored at /home/developer to
    match how the agent-server interprets them (see agent_server/routers/files.py).
    """
    if not raw:
        return ""
    # posixpath.normpath collapses `..` and `.` lexically; fine for matching.
    # It also keeps exactly two leading slashes (POSIX leaves `//` implementation-
    # defined), while Linux and the agent server's resolve() read them as one —
    # left alone, `//home/developer/.ssh/x` matched no path pattern and no fence
    # (trinity-enterprise#792).
    if raw.startswith("/"):
        return posixpath.normpath("/" + raw.lstrip("/"))
    return posixpath.normpath(posixpath.join("/home/developer", raw))


def _matches_any(path: str, patterns) -> bool:
    """True when the normalised `path` matches any of `patterns`.

    Match strategy (mirrors docker/base-image/hooks/file-guardrail.py):
    - basename match against any pattern (handles `.env`, `.mcp.json` etc.)
    - full-path glob match (handles `.ssh/*`, `/opt/trinity/*` etc.)
    - relative-form glob match against /home/developer-relative path
    fnmatch's `*` crosses `/`, so `.trinity/backup/*` covers any depth.
    """
    normalized = _normalize_user_path(path)
    if not normalized:
        return False
    basename = posixpath.basename(normalized)
    rel_to_home = ""
    if normalized.startswith("/home/developer/"):
        rel_to_home = normalized[len("/home/developer/"):]
    for pattern in patterns:
        if fnmatch.fnmatch(basename, pattern):
            return True
        if fnmatch.fnmatch(normalized, pattern):
            return True
        if rel_to_home and fnmatch.fnmatch(rel_to_home, pattern):
            return True
    return False


def _is_user_writable_path(path: str) -> bool:
    """Reject writes to credential / runtime-config / Trinity-managed paths."""
    return bool(_normalize_user_path(path)) and not _matches_any(path, _FILE_WRITE_DENY_PATTERNS)


def _is_owner_tier_read_path(path: str) -> bool:
    """True when reading `path` takes the owner tier (trinity-enterprise#819).
    An empty path is owner-tier: it fails closed."""
    return not _normalize_user_path(path) or _matches_any(path, _OWNER_TIER_READ_PATTERNS)


def _deny_anchor(pattern: str) -> str:
    """The fixed directory (or file) a path-anchored deny pattern lives under:
    its literal prefix, cut back to the last `/` before the first glob char.
    Relative patterns are anchored at /home/developer, as the matcher reads them."""
    glob_at = min((i for i, c in enumerate(pattern) if c in "*?["), default=None)
    head = pattern if glob_at is None else pattern[:pattern.rfind("/", 0, glob_at) + 1]
    if not head.startswith("/"):
        head = posixpath.join("/home/developer", head)
    return head.rstrip("/") or "/"


# trinity-enterprise#792: DELETE removes everything under a directory, so
# deleting `.ssh` (or `.claude`, or the home dir) is the same act as deleting
# `.ssh/authorized_keys`. The `/proc` anchor is load-bearing here:
# `/proc/self/root/...` and `/proc/self/cwd/...` resolve into the home dir in
# the agent container.
_DENY_ANCHORS = tuple(_deny_anchor(p) for p in _FILE_WRITE_DENY_PATTERNS if "/" in p)


def _is_user_deletable_path(path: str) -> bool:
    """Reject deleting a protected path or any directory that holds one.

    Only the path-anchored patterns can be checked that way. A basename pattern
    (`.env`, `.credentials.enc`) can sit in any directory, so deleting a
    directory that merely contains one is not refused here; the agent server's
    own by-name `PROTECTED_PATHS` block applies on its side. Fails closed: an
    empty path is refused (via the write check), and `/` holds every anchor.
    """
    if not _is_user_writable_path(path):
        return False
    normalized = _normalize_user_path(path)
    head = normalized.rstrip("/")
    return not any(a == normalized or a.startswith(head + "/") for a in _DENY_ANCHORS)


# trinity-enterprise#819: reading a credential path takes the owner tier. The
# message says what a teammate on a shared agent can still do and whom to ask.
_OWNER_TIER_MESSAGE = (
    "Credential files can be opened only by the agent's owner or an admin. "
    "You can still chat with this agent, and it keeps using its credentials. "
    "To view or change them, ask the agent's owner or an admin."
)
_INVALID_PATH_DETAIL = {"code": "invalid_path", "message": "Invalid path"}


def _owner_tier_detail(path: str) -> dict:
    return {
        "code": "owner_tier_path",
        "message": _OWNER_TIER_MESSAGE,
        "path": _normalize_user_path(path),
    }


def _refuse_invalid_path(path: str) -> None:
    """A NUL byte names no file; refused for every caller before the agent is called."""
    if "\x00" in path:
        raise HTTPException(status_code=400, detail=dict(_INVALID_PATH_DETAIL))


def _owner_tier_rule(path: str) -> str:
    """Which part of the owner-tier set `path` falls in, for the audit row."""
    if not _normalize_user_path(path):
        return "empty"
    for rule, patterns in (
        ("alias", _PATH_ALIAS_PATTERNS),
        ("trinity_copy", _TRINITY_CREDENTIAL_COPY_PATTERNS),
        ("secret_class", _SECRET_FILE_CLASS_PATTERNS),
        ("runtime", _RUNTIME_CONFIG_PATTERNS),
    ):
        if _matches_any(path, patterns):
            return rule
    return "credential"


def _audit_actor(current_user) -> dict:
    """Who the row is filed against. The audit resolver ranks `actor_user`
    first and would record the OWNER for a key that resolves to them, so only a
    person is filed as `actor_user`; an agent key is filed as its agent, and any
    other key by its scope and key, the owner riding as `actor_email`."""
    from dependencies import is_person_principal
    if is_person_principal(current_user):
        return {"actor_user": current_user}
    keyed = {
        "actor_email": getattr(current_user, "email", None),
        "mcp_key_id": getattr(current_user, "mcp_key_id", None),
        "mcp_key_name": getattr(current_user, "mcp_key_name", None),
        "mcp_scope": getattr(current_user, "mcp_scope", None),
    }
    agent = getattr(current_user, "agent_name", None)
    if agent:
        keyed["actor_agent_name"] = agent
    return keyed


async def _audit_read(event_action, request, current_user, agent_name, path, status, rule) -> None:
    """One AUTHORIZATION row per owner-tier read decision. Best-effort: an audit
    failure never turns a refusal into a 500 nor blocks an allowed read."""
    try:
        from services.platform_audit_service import platform_audit_service, AuditEventType
        await platform_audit_service.log(
            event_type=AuditEventType.AUTHORIZATION,
            event_action=event_action,
            source="api",
            **_audit_actor(current_user),
            actor_ip=request.client.host if request.client else None,
            target_type="agent",
            target_id=agent_name,
            # the routed path, never request.url.path (#3108)
            endpoint=request.scope["path"],
            request_id=getattr(request.state, "request_id", None),
            details={
                "path": _normalize_user_path(path)[:512],
                "status": status,
                "method": request.method,
                "tier": "owner",
                "rule": rule,
            },
        )
    except Exception:  # noqa: BLE001
        logger.warning("File read audit failed: action=%s agent=%s", event_action, agent_name)


def _is_owners_interactive_session(current_user, agent_name: str) -> bool:
    """The agent's owner in a signed-in session (not a key). Decided on the
    owner row, never `can_user_share_agent` (true for every admin). A failed
    lookup reads as "not the owner", so the read is audited."""
    if getattr(current_user, "mcp_scope", "__missing__") is not None:
        return False
    try:
        owner = db.get_agent_owner(agent_name)
    except Exception:  # noqa: BLE001
        return False
    return bool(owner) and owner.get("owner_username") == current_user.username


# trinity-enterprise#819: the agent server refuses a download/preview whose path
# is a link or passes through one, for every caller. Keep in sync with
# docker/base-image/agent_server/routers/files.py::_LINK_REFUSAL.
_LINK_REFUSAL_CODE = "resolved_path_mismatch"
_LINK_REFUSAL_MESSAGE = (
    "This path is a link. Links are not opened by the file routes; open the file it points to."
)


def _is_link_refusal(response) -> bool:
    """True for the agent server's structured link refusal; any other answer
    (a string detail, a non-JSON body) is not one."""
    if response.status_code != 403:
        return False
    try:
        detail = response.json().get("detail")
    except Exception:  # noqa: BLE001 - a non-JSON body is not the refusal
        return False
    return isinstance(detail, dict) and detail.get("code") == _LINK_REFUSAL_CODE


async def _raise_if_link_refusal(response, request, current_user, agent_name, path) -> None:
    """Turn the agent's link refusal into an audited, structured 403."""
    if not _is_link_refusal(response):
        return
    logger.warning(
        "File read refused, path is a link: agent=%s path=%r user=%s",
        agent_name, _normalize_user_path(path), current_user.username,
    )
    await _audit_read("file_read_refused", request, current_user, agent_name, path, 403,
                      _LINK_REFUSAL_CODE)
    raise HTTPException(status_code=403, detail={
        "code": _LINK_REFUSAL_CODE,
        "message": _LINK_REFUSAL_MESSAGE,
        "path": _normalize_user_path(path),
    })


# trinity-enterprise#819: an agent still on an older base image follows links on
# download and preview. Probed once per container and image (ent#708-style: grep
# the agent server's source for the read function); until it is recreated on the
# current image, reads below the owner tier are refused. Fails SAFE: a missing
# target, an unreadable answer or an exec error all read as "not verified"; only
# grep's own quiet answer (exit 0, or 1/2 with no output) is cached.
_READ_POLICY_PROBE_PATH = "/app/agent_server/routers/files.py"
_READ_POLICY_PROBE_TOKEN = "_open_for_read"
_READ_POLICY_PROBE_TIMEOUT = 10
_READ_POLICY_PROBE_CACHE: dict = {}
_UNVERIFIED_IMAGE_REFUSALS = 0
_RESTART_REQUIRED_MESSAGE = (
    "This agent needs a restart to apply an update. "
    "Ask the agent's owner or an admin to stop and start it in Trinity."
)


async def _agent_reads_without_links(container, agent_name: str) -> bool:
    """Does this container's agent server open reads without following links?
    Cached per (container id, image id) only when the answer is conclusive:
    exit 0 (verified), or exit 1/2 with empty output (token or file absent;
    `grep -qs` prints nothing). The exec helper reports a Docker fault as
    exit 1 with text in `output`, so any output, a timeout or another code is
    "not verified", logged and not cached."""
    image = (getattr(container, "attrs", None) or {}).get("Image")
    key = (getattr(container, "id", None), image)
    cacheable = all(key)
    if cacheable and key in _READ_POLICY_PROBE_CACHE:
        return _READ_POLICY_PROBE_CACHE[key]
    try:
        result = await execute_command_in_container(
            f"agent-{agent_name}",
            ["grep", "-qsF", "--", _READ_POLICY_PROBE_TOKEN, _READ_POLICY_PROBE_PATH],
            timeout=_READ_POLICY_PROBE_TIMEOUT,
        )
    except Exception as e:  # noqa: BLE001 - inconclusive: fail safe, re-probe next time
        logger.warning("Read-policy probe failed: agent=%s error=%s", agent_name, e)
        return False
    code = result.get("exit_code")
    quiet = (result.get("output") or "").strip() == ""
    if result.get("timed_out") or code not in (0, 1, 2) or (code != 0 and not quiet):
        logger.warning("Read-policy probe inconclusive: agent=%s exit=%s", agent_name, code)
        return False
    verdict = code == 0   # 1: token absent, 2: file absent; both a property of the image
    if cacheable:
        _READ_POLICY_PROBE_CACHE[key] = verdict
    return verdict


def _passes_owner_tier(current_user, agent_name: str) -> bool:
    """The credential owner tier: a person who passes the owner gate (the
    agent's owner, or an admin). An agent, system, connector or ops key is
    below it, as in `_enforce_owner_tier_read`."""
    from dependencies import assert_agent_owner, assert_person
    try:
        assert_person(current_user)
        assert_agent_owner(current_user, agent_name)
    except HTTPException:
        return False
    return True


async def _refuse_below_owner_on_unverified_image(container, current_user, agent_name, path) -> None:
    """Below the owner tier, read only from an agent whose image is verified.
    Only a person who is the owner or an admin skips the check; agent keys are
    refused like any caller below the owner tier until the restart."""
    global _UNVERIFIED_IMAGE_REFUSALS
    if _passes_owner_tier(current_user, agent_name):
        return
    if await _agent_reads_without_links(container, agent_name):
        return
    _UNVERIFIED_IMAGE_REFUSALS += 1
    logger.warning(
        "File read refused until the agent restarts on the current image: "
        "agent=%s path=%r user=%s refusals=%d",
        agent_name, _normalize_user_path(path), current_user.username, _UNVERIFIED_IMAGE_REFUSALS,
    )
    raise HTTPException(status_code=403, detail={
        "code": "agent_restart_required",
        "message": _RESTART_REQUIRED_MESSAGE,
        "path": _normalize_user_path(path),
    })


async def _enforce_owner_tier_read(path, current_user, request, agent_name) -> None:
    """Refuse an owner-tier read unless the caller is a person who passes the
    owner tier (the agent's owner, or an admin). The PERSON gate runs first, so
    an agent, system, connector or ops key gets `person_required`. Refusals are
    audited, and so are allowed reads by anyone but the owner's own session."""
    if not _is_owner_tier_read_path(path):
        return
    from dependencies import assert_agent_owner, assert_person
    rule = _owner_tier_rule(path)
    try:
        assert_person(current_user)
        try:
            assert_agent_owner(current_user, agent_name)
        except HTTPException as e:
            raise HTTPException(status_code=e.status_code, detail=_owner_tier_detail(path)) from e
    except HTTPException as refusal:
        logger.warning(
            "Owner-tier read refused: agent=%s path=%r user=%s",
            agent_name, _normalize_user_path(path), current_user.username,
        )
        await _audit_read("file_read_refused", request, current_user, agent_name, path,
                          refusal.status_code, rule)
        raise
    if not _is_owners_interactive_session(current_user, agent_name):
        await _audit_read("file_read_allowed", request, current_user, agent_name, path, 200, rule)


# trinity-enterprise#596: the skills directory is where a library skill lands
# (`skill_service.inject_skills`). Writing it through the generic file routes is
# the same act as assigning a skill — an executable package in the agent's head —
# so it takes the same capability. Without this the skill fence on
# `routers/skills.py` is decorative: an agent key with no grant would write
# `.claude/skills/x/SKILL.md` + `scripts/` into a sibling through `PUT /files`,
# a route that checks only ACCESS. Humans and the system agent pass the
# capability by scope, so the Files tab is unchanged for them.
_SKILLS_DIR = "/home/developer/.claude/skills"


def _touches_skills_dir(path: str, *, include_ancestors: bool = False) -> bool:
    """True for the skills dir or anything under it; with `include_ancestors`,
    also for any directory ABOVE it — deleting `.claude` (or the home dir)
    removes every skill as surely as deleting the skills dir does."""
    normalized = _normalize_user_path(path)
    if not normalized:
        return False
    if normalized == _SKILLS_DIR or normalized.startswith(_SKILLS_DIR + "/"):
        return True
    if include_ancestors:
        return _SKILLS_DIR.startswith(normalized.rstrip("/") + "/")
    return False


async def _require_skill_capability(path, current_user, request, agent_name, *, include_ancestors=False):
    if _touches_skills_dir(path, include_ancestors=include_ancestors):
        from dependencies import enforce_agent_capability
        from db.capability_grants import CAPABILITY_SKILLS_MANAGE
        await enforce_agent_capability(
            request, current_user, CAPABILITY_SKILLS_MANAGE, target=agent_name
        )


async def list_agent_files_logic(
    agent_name: str,
    path: str,
    current_user: User,
    request: Request,
    show_hidden: bool = False
) -> dict:
    """
    List files in the agent's workspace directory.
    Returns a flat list of files with metadata (name, size, modified date).

    Args:
        agent_name: Name of the agent
        path: Directory path to list
        current_user: Current authenticated user
        request: HTTP request object
        show_hidden: If True, include hidden files (starting with .)
    """
    if not db.can_user_access_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="You don't have permission to access this agent")

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    await container_reload(container)
    if container.status != "running":
        raise HTTPException(status_code=400, detail="Agent must be running to browse files")

    try:
        # Call agent's internal file listing API with retry
        response = await agent_http_request(
            agent_name,
            "GET",
            "/api/files",
            params={"path": path, "show_hidden": str(show_hidden).lower()},
            max_retries=3,
            retry_delay=1.0,
            timeout=30.0
        )
        if response.status_code == 200:
            return response.json()
        else:
            raise HTTPException(
                status_code=response.status_code,
                detail=f"Failed to list files: {response.text}"
            )
    except httpx.ConnectError:
        # Agent server not ready - return 503 so tests can skip
        raise HTTPException(
            status_code=503,
            detail="Agent server not ready. The agent may still be starting up."
        )
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="File listing timed out")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list files: {str(e)}")


async def download_agent_file_logic(
    agent_name: str,
    path: str,
    current_user: User,
    request: Request
) -> PlainTextResponse:
    """
    Download a file from the agent's workspace.
    Returns the file content as plain text.
    """
    if not db.can_user_access_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="You don't have permission to access this agent")

    # ent#819: a NUL byte names no file; credential paths take the owner tier.
    _refuse_invalid_path(path)
    await _enforce_owner_tier_read(path, current_user, request, agent_name)

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    await container_reload(container)
    if container.status != "running":
        raise HTTPException(status_code=400, detail="Agent must be running to download files")

    # ent#819: below the owner tier, only an agent on the current image is read.
    await _refuse_below_owner_on_unverified_image(container, current_user, agent_name, path)

    try:
        # Call agent's internal file download API with retry
        response = await agent_http_request(
            agent_name,
            "GET",
            "/api/files/download",
            # ent#819: send the path the checks above approved, not the raw input
            params={"path": _normalize_user_path(path)},
            max_retries=3,
            retry_delay=1.0,
            timeout=60.0
        )
        if response.status_code == 200:
            return PlainTextResponse(content=response.text)
        else:
            await _raise_if_link_refusal(response, request, current_user, agent_name, path)
            raise HTTPException(
                status_code=response.status_code,
                detail=f"Failed to download file: {response.text}"
            )
    except httpx.ConnectError:
        # Agent server not ready - return 503 so tests can skip
        raise HTTPException(
            status_code=503,
            detail="Agent server not ready. The agent may still be starting up."
        )
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="File download timed out")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to download file: {str(e)}")


async def delete_agent_file_logic(
    agent_name: str,
    path: str,
    current_user: User,
    request: Request
) -> dict:
    """
    Delete a file or directory from the agent's workspace.
    """
    if not db.can_user_access_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="You don't have permission to access this agent")

    # ent#596: a write into the skills dir needs the skill-management capability.
    await _require_skill_capability(path, current_user, request, agent_name, include_ancestors=True)

    # trinity-enterprise#792: the write deny list applies to DELETE too, and to
    # any directory that holds a protected path.
    if not _is_user_deletable_path(path):
        logger.warning(
            "File delete blocked at backend deny-list: agent=%s path=%s user=%s",
            agent_name, path, current_user.username,
        )
        raise HTTPException(
            status_code=403,
            detail=f"Cannot delete protected path: {path}"
        )

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    await container_reload(container)
    if container.status != "running":
        raise HTTPException(status_code=400, detail="Agent must be running to delete files")

    try:
        # Call agent's internal file delete API with retry
        response = await agent_http_request(
            agent_name,
            "DELETE",
            "/api/files",
            # ent#792: send the path the checks above approved, not the raw input
            params={"path": _normalize_user_path(path)},
            max_retries=3,
            retry_delay=1.0,
            timeout=30.0
        )
        if response.status_code == 200:
            result = response.json()
            return result
        else:
            raise HTTPException(
                status_code=response.status_code,
                detail=response.json().get("detail", f"Failed to delete: {response.text}")
            )
    except httpx.ConnectError:
        # Agent server not ready - return 503 so tests can skip
        raise HTTPException(
            status_code=503,
            detail="Agent server not ready. The agent may still be starting up."
        )
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="File deletion timed out")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete file: {str(e)}")


async def preview_agent_file_logic(
    agent_name: str,
    path: str,
    current_user: User,
    request: Request
) -> StreamingResponse:
    """
    Get file with proper MIME type for preview.
    Streams the response from the agent container.
    """
    if not db.can_user_access_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="You don't have permission to access this agent")

    # ent#819: a NUL byte names no file; credential paths take the owner tier.
    _refuse_invalid_path(path)
    await _enforce_owner_tier_read(path, current_user, request, agent_name)

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    await container_reload(container)
    if container.status != "running":
        raise HTTPException(status_code=400, detail="Agent must be running to preview files")

    # ent#819: below the owner tier, only an agent on the current image is read.
    await _refuse_below_owner_on_unverified_image(container, current_user, agent_name, path)

    try:
        # Call agent's internal file preview API with retry
        response = await agent_http_request(
            agent_name,
            "GET",
            "/api/files/preview",
            # ent#819: send the path the checks above approved, not the raw input
            params={"path": _normalize_user_path(path)},
            max_retries=3,
            retry_delay=1.0,
            timeout=30.0
        )
        if response.status_code != 200:
            await _raise_if_link_refusal(response, request, current_user, agent_name, path)
            raise HTTPException(
                status_code=response.status_code,
                detail=response.json().get("detail", f"Failed to preview: {response.text}")
            )

        content_type = response.headers.get("content-type", "application/octet-stream")
        content_disposition = response.headers.get("content-disposition")

        # For small files, return directly
        return StreamingResponse(
            iter([response.content]),
            media_type=content_type,
            headers={"Content-Disposition": content_disposition} if content_disposition else {}
        )

    except httpx.ConnectError:
        # Agent server not ready - return 503 so tests can skip
        raise HTTPException(
            status_code=503,
            detail="Agent server not ready. The agent may still be starting up."
        )
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="File preview timed out")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to preview file: {str(e)}")


async def update_agent_file_logic(
    agent_name: str,
    path: str,
    content: str,
    current_user: User,
    request: Request
) -> dict:
    """
    Update a file's content in the agent's workspace.

    Args:
        agent_name: Name of the agent
        path: File path to update
        content: New file content
        current_user: Current authenticated user
        request: HTTP request object
    """
    if not db.can_user_access_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="You don't have permission to access this agent")

    # ent#596: a write into the skills dir needs the skill-management capability.
    await _require_skill_capability(path, current_user, request, agent_name)

    # AISEC-C2 / #590: backend-side deny check before proxying to the agent.
    # Stops the .mcp.json RCE escalation at the platform boundary; the
    # agent-server still re-validates as defense in depth.
    if not _is_user_writable_path(path):
        logger.warning(
            "File write blocked at backend deny-list: agent=%s path=%s user=%s",
            agent_name, path, current_user.username,
        )
        raise HTTPException(
            status_code=403,
            detail=f"Cannot edit protected path: {path}"
        )

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    await container_reload(container)
    if container.status != "running":
        raise HTTPException(status_code=400, detail="Agent must be running to update files")

    try:
        # Call agent's internal file update API with retry
        response = await agent_http_request(
            agent_name,
            "PUT",
            "/api/files",
            # ent#792: send the path the checks above approved, not the raw input
            params={"path": _normalize_user_path(path)},
            json={"content": content},
            max_retries=3,
            retry_delay=1.0,
            timeout=60.0
        )
        if response.status_code == 200:
            result = response.json()
            return result
        else:
            raise HTTPException(
                status_code=response.status_code,
                detail=response.json().get("detail", f"Failed to update: {response.text}")
            )
    except httpx.ConnectError:
        # Agent server not ready - return 503 so tests can skip
        raise HTTPException(
            status_code=503,
            detail="Agent server not ready. The agent may still be starting up."
        )
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="File update timed out")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update file: {str(e)}")


async def create_agent_folder_logic(
    agent_name: str,
    path: str,
    current_user: User,
    request: Request
) -> dict:
    """
    Create a new directory in the agent's workspace.

    Args:
        agent_name: Name of the agent
        path: Directory path to create
        current_user: Current authenticated user
        request: HTTP request object
    """
    if not db.can_user_access_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="You don't have permission to access this agent")

    # ent#596: a write into the skills dir needs the skill-management capability.
    await _require_skill_capability(path, current_user, request, agent_name)

    # AISEC-C2 / #590: backend-side deny check before proxying to the agent.
    # Same deny-list used for file writes — a folder under a credential /
    # Trinity-managed path is still a write into that path. The agent-server
    # re-validates as defense in depth.
    if not _is_user_writable_path(path):
        logger.warning(
            "Folder create blocked at backend deny-list: agent=%s path=%s user=%s",
            agent_name, path, current_user.username,
        )
        raise HTTPException(
            status_code=403,
            detail=f"Cannot create folder in protected path: {path}"
        )

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    await container_reload(container)
    if container.status != "running":
        raise HTTPException(status_code=400, detail="Agent must be running to create folders")

    try:
        # Call agent's internal mkdir API with retry
        response = await agent_http_request(
            agent_name,
            "POST",
            "/api/files/mkdir",
            # ent#792: send the path the checks above approved, not the raw input
            params={"path": _normalize_user_path(path)},
            max_retries=3,
            retry_delay=1.0,
            timeout=30.0
        )
        if response.status_code == 200:
            return response.json()
        else:
            raise HTTPException(
                status_code=response.status_code,
                detail=response.json().get("detail", f"Failed to create folder: {response.text}")
            )
    except httpx.ConnectError:
        # Agent server not ready - return 503 so tests can skip
        raise HTTPException(
            status_code=503,
            detail="Agent server not ready. The agent may still be starting up."
        )
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Folder creation timed out")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create folder: {str(e)}")
