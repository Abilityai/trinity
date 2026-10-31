"""
Agent Service Files - File browser operations.

Handles file listing, download, preview, and delete for agent workspaces.
"""
import logging

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import PlainTextResponse, StreamingResponse

from models import User
from database import db
from dependencies import agent_may_reach
from services import file_path_policy as _policy
from services.docker_service import execute_command_in_container, get_agent_container
from services.docker_utils import container_reload
from .helpers import agent_http_request

logger = logging.getLogger(__name__)


# trinity-enterprise#820: every path class and the tier x verb table live in ONE
# module, vendored byte-identical into the agent server (Invariant #5). The
# names below are this module's spelling of it; to add a path, change
# `services/file_path_policy.py`, copy it to
# docker/base-image/agent_server/file_path_policy.py, and add the
# hooks/guardrails-baseline.json::path_deny entry the parity test asks for.
_RUNTIME_CONFIG_PATTERNS = _policy.RUNTIME_CONFIG_PATTERNS
_CREDENTIAL_PATH_PATTERNS = _policy.CREDENTIAL_PATH_PATTERNS + _policy.RUNTIME_CONFIG_PATTERNS
_PATH_ALIAS_PATTERNS = _policy.PATH_ALIAS_PATTERNS
_FILE_WRITE_DENY_PATTERNS = _policy.FILE_WRITE_DENY_PATTERNS
_TRINITY_CREDENTIAL_COPY_PATTERNS = _policy.TRINITY_CREDENTIAL_COPY_PATTERNS
_SECRET_FILE_CLASS_PATTERNS = _policy.SECRET_FILE_CLASS_PATTERNS
# The only owner-tier read patterns that are not also write-denied (pinned by
# test_files_protected_paths).
_OWNER_TIER_READ_ONLY_PATTERNS = _SECRET_FILE_CLASS_PATTERNS
_OWNER_TIER_READ_PATTERNS = (
    _CREDENTIAL_PATH_PATTERNS
    + _PATH_ALIAS_PATTERNS
    + _TRINITY_CREDENTIAL_COPY_PATTERNS
    + _OWNER_TIER_READ_ONLY_PATTERNS
)
_deny_anchor = _policy.deny_anchor
_DENY_ANCHORS = _policy.DENY_ANCHORS


def _normalize_user_path(raw: str) -> str:
    """A user path reduced to a stable absolute form, lexically (no FS access);
    relative paths anchored at /home/developer, as the agent server reads them."""
    return _policy.normalize(raw)


def _matches_any(path: str, patterns) -> bool:
    """True when the normalised `path` matches any of `patterns`."""
    return _policy.matches_any(_normalize_user_path(path), patterns)


def _is_user_writable_path(path: str) -> bool:
    """Reject writes to credential / runtime-config / Trinity-managed / git
    metadata paths, for every caller (the route write floor)."""
    return not _policy.write_denied(path)


def _is_owner_tier_read_path(path: str) -> bool:
    """True when reading `path` takes the owner tier (trinity-enterprise#819).
    An empty path is owner-tier: it fails closed."""
    return _policy.owner_tier_read(path)


def _is_user_deletable_path(path: str) -> bool:
    """Reject deleting a protected path or any directory that holds one
    (trinity-enterprise#792). Fails closed on an empty path."""
    return not _policy.delete_denied(path)


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


# trinity-enterprise#820: the same probe for the write verbs. An agent on an
# older image follows a link on PUT, mkdir and DELETE; until it restarts on the
# current image, share holders' writes are refused (agent keys pass, audited).
_WRITE_POLICY_PROBE_TOKEN = "_walk_dir_nofollow"
_WRITE_POLICY_PROBE_CACHE: dict = {}


async def _agent_server_has(container, agent_name: str, token: str, cache: dict) -> bool:
    """Does this container's agent server source carry `token`?
    Cached per (container id, image id) only when the answer is conclusive:
    exit 0 (verified), or exit 1/2 with empty output (token or file absent;
    `grep -qs` prints nothing). The exec helper reports a Docker fault as
    exit 1 with text in `output`, so any output, a timeout or another code is
    "not verified", logged and not cached."""
    image = (getattr(container, "attrs", None) or {}).get("Image")
    key = (getattr(container, "id", None), image)
    cacheable = all(key)
    if cacheable and key in cache:
        return cache[key]
    try:
        result = await execute_command_in_container(
            f"agent-{agent_name}",
            ["grep", "-qsF", "--", token, _READ_POLICY_PROBE_PATH],
            timeout=_READ_POLICY_PROBE_TIMEOUT,
        )
    except Exception as e:  # noqa: BLE001 - inconclusive: fail safe, re-probe next time
        logger.warning("File-policy probe failed: agent=%s token=%s error=%s", agent_name, token, e)
        return False
    code = result.get("exit_code")
    quiet = (result.get("output") or "").strip() == ""
    if result.get("timed_out") or code not in (0, 1, 2) or (code != 0 and not quiet):
        logger.warning("File-policy probe inconclusive: agent=%s token=%s exit=%s", agent_name, token, code)
        return False
    verdict = code == 0   # 1: token absent, 2: file absent; both a property of the image
    if cacheable:
        cache[key] = verdict
    return verdict


async def _agent_reads_without_links(container, agent_name: str) -> bool:
    """Does this container's agent server open reads without following links?"""
    return await _agent_server_has(container, agent_name, _READ_POLICY_PROBE_TOKEN,
                                   _READ_POLICY_PROBE_CACHE)


async def _agent_writes_without_links(container, agent_name: str) -> bool:
    """Do this container's write verbs refuse a link that changes a path's class?"""
    return await _agent_server_has(container, agent_name, _WRITE_POLICY_PROBE_TOKEN,
                                   _WRITE_POLICY_PROBE_CACHE)


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


def _caller_tier(current_user, agent_name: str) -> str:
    """The Files tier (trinity-enterprise#820): OWNER is a person who passes the
    owner gate (owner or admin), SHARE any other person (a share holder, by JWT
    or by their own user-scope key), KEY every agent, system, connector or ops
    key."""
    from dependencies import is_person_principal, restricted_agent_turn
    if _passes_owner_tier(current_user, agent_name):
        return _policy.OWNER
    if is_person_principal(current_user):
        return _policy.SHARE
    # ent#926: an agent's key acting for a shared person (or a limited visitor)
    # is that person's tier, so its `instructions.manage` / `skills.manage`
    # grants do not rewrite instructions on their behalf.
    if restricted_agent_turn(current_user):
        return _policy.SHARE
    return _policy.KEY


# trinity-enterprise#820: what a share holder is told when a path takes the
# owner tier through these routes.
_SHARE_TIER_MESSAGE = (
    "Only the agent's owner or an admin can change this. A share covers the "
    "agent's files except credentials, runtime config and its instructions."
)


def _tier_refusal_detail(code: str, path: str) -> dict:
    if code == _policy.OWNER_TIER:
        return {"code": code, "message": _SHARE_TIER_MESSAGE, "path": _normalize_user_path(path)}
    return {"code": code, "message": f"Cannot change protected path: {path}",
            "path": _normalize_user_path(path)}


async def _audit_mutation(event_action, request, current_user, agent_name, path, status, tier,
                          **extra) -> None:
    """One row per write, delete or mkdir through these routes, and per tier
    refusal (trinity-enterprise#820). Best-effort: an audit failure never turns
    a refusal into a 500 nor fails a write that happened."""
    try:
        from services.platform_audit_service import platform_audit_service, AuditEventType
        await platform_audit_service.log(
            event_type=(AuditEventType.AUTHORIZATION if event_action.endswith("_refused")
                        else AuditEventType.AGENT_FILES),
            event_action=event_action,
            source="api",
            **_audit_actor(current_user),
            actor_ip=request.client.host if request.client else None,
            target_type="agent",
            target_id=agent_name,
            endpoint=request.scope["path"],
            request_id=getattr(request.state, "request_id", None),
            details={
                "path": _normalize_user_path(path)[:512],
                "status": status,
                "method": request.method,
                "tier": tier,
                **extra,
            },
        )
    except Exception:  # noqa: BLE001
        logger.warning("File mutation audit failed: action=%s agent=%s", event_action, agent_name)


async def _enforce_tier(verb, path, current_user, request, agent_name, action) -> str:
    """Refuse `verb` on `path` when the policy table says this caller's tier may
    not use it; audit the refusal. Returns the tier for the success row."""
    tier = _caller_tier(current_user, agent_name)
    verdict = _policy.decide(tier, verb, path)
    if not verdict.allowed:
        logger.warning(
            "File %s refused by tier: agent=%s path=%r user=%s tier=%s code=%s",
            verb, agent_name, _normalize_user_path(path), current_user.username, tier, verdict.code,
        )
        await _audit_mutation(f"{action}_refused", request, current_user, agent_name, path, 403,
                              tier, code=verdict.code)
        raise HTTPException(status_code=403, detail=_tier_refusal_detail(verdict.code, path))
    return tier


async def _refuse_share_write_on_unverified_image(container, current_user, agent_name, path, tier) -> bool:
    """Below the owner tier, a share holder writes only to an agent whose image
    refuses class-changing links. Agent keys pass (their ent#164 / ent#596
    grants keep working on old images); the success row records it. Returns
    True when the image is verified."""
    global _UNVERIFIED_IMAGE_REFUSALS
    if tier == _policy.OWNER:
        return True
    if await _agent_writes_without_links(container, agent_name):
        return True
    if tier == _policy.KEY:
        return False
    _UNVERIFIED_IMAGE_REFUSALS += 1
    logger.warning(
        "File write refused until the agent restarts on the current image: "
        "agent=%s path=%r user=%s refusals=%d",
        agent_name, _normalize_user_path(path), current_user.username, _UNVERIFIED_IMAGE_REFUSALS,
    )
    raise HTTPException(status_code=403, detail={
        "code": "agent_restart_required",
        "message": _RESTART_REQUIRED_MESSAGE,
        "path": _normalize_user_path(path),
    })


def _project_tree(nodes: list, tier: str) -> None:
    """Flag each listed node with the verbs this caller cannot use on it
    (trinity-enterprise#820), in place. The Files tab reads these flags; it
    keeps no path list of its own."""
    for node in nodes:
        is_dir = node.get("type") == "directory"
        verbs = _policy.restricted_verbs(tier, node.get("path") or "", is_dir=is_dir)
        if node.get("type") == "link":
            # Links are never opened or written through (only unlinked).
            verbs = ["read", "write"] + [v for v in verbs if v == "delete"]
        if verbs:
            node["restricted"] = verbs
        if is_dir:
            _project_tree(node.get("children") or [], tier)


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
def _touches_skills_dir(path: str, *, include_ancestors: bool = False) -> bool:
    """True for the skills dir or anything under it; with `include_ancestors`,
    also for any directory ABOVE it — deleting `.claude` (or the home dir)
    removes every skill as surely as deleting the skills dir does."""
    return _policy.SKILLS in _policy.classes(path, include_ancestors=include_ancestors)


def _may_reach_for_path(current_user, agent_name, path, *, include_ancestors=False) -> bool:
    """trinity-enterprise#629 reach, plus the capability exception: `skills.manage`
    (ent#596) and `instructions.manage` (ent#164) ARE the grants to change those
    files on the owner's other agents, so a holder writing such a path reaches
    the target without a permission edge. Every file class the path touches
    needs its capability, and the target must be one the key's owner OWNS — the
    bound the route-level capability fences apply (`owner_owns_agent`). The
    capability gate below still decides the write and audits a refusal."""
    if agent_may_reach(current_user, agent_name):
        return True
    from db.capability_grants import CAPABILITY_INSTRUCTIONS_MANAGE, CAPABILITY_SKILLS_MANAGE
    needed = []
    if _touches_skills_dir(path, include_ancestors=include_ancestors):
        needed.append(CAPABILITY_SKILLS_MANAGE)
    if _touches_instructions(path, include_ancestors=include_ancestors):
        needed.append(CAPABILITY_INSTRUCTIONS_MANAGE)
    if not needed:
        return False
    from dependencies import capability_refusal, owner_owns_agent
    if any(capability_refusal(current_user, cap) is not None for cap in needed):
        return False
    return owner_owns_agent(current_user, agent_name)


# trinity-enterprise#164: the agent's INSTRUCTIONS — its top-level instruction
# files (CLAUDE.md, CLAUDE.local.md, AGENTS.md, GEMINI.md, .gemini/GEMINI.md)
# and everything under `.claude/` except the skills dir (which is
# `skills.manage`, one permission per file class). A platform write there
# changes how the agent behaves, so it takes `instructions.manage`. Only the
# agent's own top-level files count: a `docs/CLAUDE.md` is an ordinary file.
def _touches_instructions(path: str, *, include_ancestors: bool = False) -> bool:
    """True for an instruction file or `.claude/**` outside the skills dir;
    with `include_ancestors`, also for a directory ABOVE them."""
    return _policy.INSTRUCTIONS in _policy.classes(path, include_ancestors=include_ancestors)


async def _require_skill_capability(path, current_user, request, agent_name, *, include_ancestors=False):
    from dependencies import enforce_agent_capability
    if _touches_skills_dir(path, include_ancestors=include_ancestors):
        from db.capability_grants import CAPABILITY_SKILLS_MANAGE
        await enforce_agent_capability(
            request, current_user, CAPABILITY_SKILLS_MANAGE, target=agent_name
        )
    if _touches_instructions(path, include_ancestors=include_ancestors):
        from db.capability_grants import CAPABILITY_INSTRUCTIONS_MANAGE
        await enforce_agent_capability(
            request, current_user, CAPABILITY_INSTRUCTIONS_MANAGE, target=agent_name
        )


# trinity-enterprise#820: a hard link to a credential file (made by the agent
# on request in chat) is a second name for it that no path pattern can see.
# The agent server reports the link count of the file it opened; below the
# owner tier a file with more than one name is not served. A pre-#820 image
# sends no count and is served as before until it restarts.
_NLINK_HEADER = "X-File-Nlink"
_HARDLINK_MESSAGE = (
    "This file has more than one name on disk, so it may be a credential under "
    "another name. Ask the agent's owner or an admin to open it."
)


async def _refuse_hardlinked_read(response, request, current_user, agent_name, path) -> None:
    try:
        nlink = int(response.headers.get(_NLINK_HEADER, "1"))
    except (TypeError, ValueError):
        nlink = 1
    if nlink <= 1 or _passes_owner_tier(current_user, agent_name):
        return
    await _audit_read("file_read_refused", request, current_user, agent_name, path, 403, "hardlink")
    raise HTTPException(status_code=403, detail={
        "code": "hardlinked_file", "message": _HARDLINK_MESSAGE, "path": _normalize_user_path(path),
    })


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
    if not (db.can_user_access_agent(current_user.username, agent_name) and agent_may_reach(current_user, agent_name)):
        raise HTTPException(status_code=403, detail="You don't have permission to access this agent")

    # ent#820: below the owner tier the list is projected — a share holder
    # gets no hidden files and cannot list a hidden or credential directory.
    _refuse_invalid_path(path)
    tier = await _enforce_tier(_policy.LIST, path, current_user, request, agent_name, "file_list")
    hidden_withheld = tier == _policy.SHARE
    if hidden_withheld:
        show_hidden = False

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
            # ent#820: send the path the checks above approved, not the raw input
            params={"path": _normalize_user_path(path), "show_hidden": str(show_hidden).lower()},
            max_retries=3,
            retry_delay=1.0,
            timeout=30.0
        )
        if response.status_code == 200:
            data = response.json()
            _project_tree(data.get("tree") or [], tier)
            data["tier"] = tier
            data["hidden_withheld"] = hidden_withheld
            return data
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
    if not (db.can_user_access_agent(current_user.username, agent_name) and agent_may_reach(current_user, agent_name)):
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
            await _refuse_hardlinked_read(response, request, current_user, agent_name, path)
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
    if not (db.can_user_access_agent(current_user.username, agent_name) and _may_reach_for_path(current_user, agent_name, path, include_ancestors=True)):
        raise HTTPException(status_code=403, detail="You don't have permission to access this agent")

    # ent#596: a write into the skills dir needs the skill-management capability.
    await _require_skill_capability(path, current_user, request, agent_name, include_ancestors=True)

    # trinity-enterprise#792: the write deny list applies to DELETE too, and to
    # any directory that holds a protected path.
    _refuse_invalid_path(path)
    if not _is_user_deletable_path(path):
        logger.warning(
            "File delete blocked at backend deny-list: agent=%s path=%s user=%s",
            agent_name, path, current_user.username,
        )
        raise HTTPException(
            status_code=403,
            detail=f"Cannot delete protected path: {path}"
        )
    # ent#820: instructions, skills and files that run code take the owner tier.
    tier = await _enforce_tier(_policy.DELETE, path, current_user, request, agent_name, "file_delete")

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    await container_reload(container)
    if container.status != "running":
        raise HTTPException(status_code=400, detail="Agent must be running to delete files")
    verified = await _refuse_share_write_on_unverified_image(container, current_user, agent_name, path, tier)

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
            await _audit_mutation("file_delete", request, current_user, agent_name, path, 200, tier,
                                  unverified_image=not verified)
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
    if not (db.can_user_access_agent(current_user.username, agent_name) and agent_may_reach(current_user, agent_name)):
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

        await _refuse_hardlinked_read(response, request, current_user, agent_name, path)
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
    if not (db.can_user_access_agent(current_user.username, agent_name) and _may_reach_for_path(current_user, agent_name, path)):
        raise HTTPException(status_code=403, detail="You don't have permission to access this agent")

    # ent#596: a write into the skills dir needs the skill-management capability.
    await _require_skill_capability(path, current_user, request, agent_name)

    # AISEC-C2 / #590: backend-side deny check before proxying to the agent.
    # Stops the .mcp.json RCE escalation at the platform boundary; the
    # agent-server still re-validates as defense in depth.
    _refuse_invalid_path(path)
    if not _is_user_writable_path(path):
        logger.warning(
            "File write blocked at backend deny-list: agent=%s path=%s user=%s",
            agent_name, path, current_user.username,
        )
        raise HTTPException(
            status_code=403,
            detail=f"Cannot edit protected path: {path}"
        )
    # ent#820: instructions, skills and files that run code take the owner tier.
    tier = await _enforce_tier(_policy.WRITE, path, current_user, request, agent_name, "file_write")

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    await container_reload(container)
    if container.status != "running":
        raise HTTPException(status_code=400, detail="Agent must be running to update files")
    verified = await _refuse_share_write_on_unverified_image(container, current_user, agent_name, path, tier)

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
            await _audit_mutation("file_write", request, current_user, agent_name, path, 200, tier,
                                  unverified_image=not verified)
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
    if not (db.can_user_access_agent(current_user.username, agent_name) and _may_reach_for_path(current_user, agent_name, path)):
        raise HTTPException(status_code=403, detail="You don't have permission to access this agent")

    # ent#596: a write into the skills dir needs the skill-management capability.
    await _require_skill_capability(path, current_user, request, agent_name)

    # AISEC-C2 / #590: backend-side deny check before proxying to the agent.
    # Same deny-list used for file writes — a folder under a credential /
    # Trinity-managed path is still a write into that path. The agent-server
    # re-validates as defense in depth.
    _refuse_invalid_path(path)
    if not _is_user_writable_path(path):
        logger.warning(
            "Folder create blocked at backend deny-list: agent=%s path=%s user=%s",
            agent_name, path, current_user.username,
        )
        raise HTTPException(
            status_code=403,
            detail=f"Cannot create folder in protected path: {path}"
        )
    # ent#820: a folder is a write; the same tier as PUT.
    tier = await _enforce_tier(_policy.WRITE, path, current_user, request, agent_name, "file_mkdir")

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    await container_reload(container)
    if container.status != "running":
        raise HTTPException(status_code=400, detail="Agent must be running to create folders")
    verified = await _refuse_share_write_on_unverified_image(container, current_user, agent_name, path, tier)

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
            result = response.json()
            await _audit_mutation("file_mkdir", request, current_user, agent_name, path, 200, tier,
                                  unverified_image=not verified)
            return result
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
