"""
Agent Service Deploy - Local agent deployment logic.

Contains the business logic for deploying local agents via MCP.
"""
import base64
import os
import tarfile
import tempfile
import shutil
import logging
from pathlib import Path
from io import BytesIO

import docker
from fastapi import HTTPException, Request

from models import (
    AgentConfig,
    AgentStatus,
    User,
    DeployLocalRequest,
    DeployLocalResponse,
    VersioningInfo,
    MAX_DEPLOY_CREDENTIALS,
)
from database import db
from services.template_service import (
    is_trinity_compatible,
    collect_mcp_credential_warnings,
)
from services.docker_service import get_agent_container
from services.docker_utils import container_stop
from utils.helpers import sanitize_agent_name
from services.settings_service import get_agent_quota_for_role
from .helpers import get_agents_by_prefix, get_next_version_name, get_latest_version

logger = logging.getLogger(__name__)

# Size limits for local deployment
MAX_ARCHIVE_SIZE = 50 * 1024 * 1024  # 50 MB
MAX_FILES = 1000

# Container-side path for deployed-local templates (#950). Sits under
# /data which is host-bound to TRINITY_DATA_PATH (default ./trinity-data),
# writable, and owned by UID 1000 — separate from the curated catalog at
# /agent-configs/templates which is intentionally read-only.
DEPLOYED_TEMPLATES_DIR_IN_BACKEND = "/data/deployed-templates"

# Image used by the workspace pre-population transient container (#950).
# Pinned to a specific tag so a Docker Hub `latest` swap can't silently
# change deploy behavior.
_PREPOP_IMAGE = "alpine:3.20"


def _prepopulate_workspace_from_template(version_name: str, template_dir: Path) -> None:
    """Pre-populate `agent-{version_name}-workspace` with the template files (#950).

    Creates (or reuses) the docker named volume that the agent container
    will mount at `/home/developer`, then copies the extracted template
    contents into it via an ephemeral alpine container (`put_archive`).
    A `.trinity-initialized` marker is included in the same tar so the
    agent's `startup.sh` skips its `/template`→`/home/developer` copy on
    boot. This bypasses the host-path bind-mount transport that was
    inconsistent between dev (named volume `/data`) and prod (host bind
    `/data`) compose files.

    Failures raise HTTPException(500) — partial pre-population would
    leave the deploy in an inconsistent state.
    """
    workspace_vol = f"agent-{version_name}-workspace"
    client = docker.from_env()

    try:
        client.volumes.get(workspace_vol)
    except docker.errors.NotFound:
        client.volumes.create(
            name=workspace_vol,
            labels={
                "trinity.platform": "agent-workspace",
                "trinity.agent-name": version_name,
            },
        )

    # Stream template + .trinity-initialized marker into the volume.
    # Both files captured under uid=1000/gid=1000 so the agent container
    # (running as `developer`, UID 1000 per #874) can read & write them.
    tar_buf = BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w") as tar:
        def _set_owner(info: tarfile.TarInfo) -> tarfile.TarInfo:
            info.uid = 1000
            info.gid = 1000
            info.uname = "developer"
            info.gname = "developer"
            return info

        tar.add(str(template_dir), arcname=".", filter=_set_owner)

        marker = tarfile.TarInfo(name=".trinity-initialized")
        marker.size = 0
        marker.uid = 1000
        marker.gid = 1000
        marker.uname = "developer"
        marker.gname = "developer"
        tar.addfile(marker, BytesIO(b""))

    tar_buf.seek(0)
    tar_bytes = tar_buf.read()

    transient = None
    try:
        # Auto-pull the image if it isn't already present on the daemon
        # (docker SDK's `containers.create` doesn't pull, unlike `run`).
        try:
            client.images.get(_PREPOP_IMAGE)
        except docker.errors.ImageNotFound:
            logger.info(f"Pulling {_PREPOP_IMAGE} for workspace pre-pop")
            client.images.pull(_PREPOP_IMAGE)
        transient = client.containers.create(
            _PREPOP_IMAGE,
            # Chown the volume root after put_archive — Docker creates new
            # volumes root-owned, and put_archive only sets ownership on
            # the entries inside, not on the mount point itself. Without
            # this, the agent (UID 1000) can't write to /home/developer.
            command=["sh", "-c", "chown 1000:1000 /dest && chmod 755 /dest"],
            volumes={workspace_vol: {"bind": "/dest", "mode": "rw"}},
        )
        ok = transient.put_archive("/dest", tar_bytes)
        if not ok:
            raise RuntimeError("put_archive returned False")
        transient.start()
        result = transient.wait(timeout=30)
        if result.get("StatusCode", 1) != 0:
            log_tail = transient.logs(tail=20).decode(errors="replace")
            raise RuntimeError(
                f"chown step failed (exit {result.get('StatusCode')}): {log_tail}"
            )
        logger.info(
            f"Pre-populated workspace volume {workspace_vol} from {template_dir}"
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={
                "error": f"Failed to pre-populate workspace for {version_name}: {e}",
                "code": "WORKSPACE_PREPOP_FAILED",
            },
        )
    finally:
        if transient is not None:
            try:
                transient.remove(force=True)
            except Exception:
                pass


# =============================================================================
# Safe Tar Extraction Utilities
# =============================================================================

def _is_path_within(base: Path, target: Path) -> bool:
    """
    Check if target path is within base directory.
    
    Uses Path.resolve() to handle symlinks and normalize paths.
    Returns True if target is inside base (or is base itself).
    """
    try:
        # resolve() normalizes the path and resolves symlinks
        # We use strict=False because target may not exist yet during extraction
        base_resolved = base.resolve()
        target_resolved = target.resolve()
        
        # Check if target starts with base path
        return str(target_resolved).startswith(str(base_resolved) + "/") or \
               target_resolved == base_resolved
    except (OSError, ValueError):
        return False


def _validate_tar_member(member: tarfile.TarInfo, base_dir: Path) -> tuple[bool, str]:
    """
    Validate a tar archive member for safe extraction.
    
    Checks:
    - Destination path stays within base_dir
    - No absolute paths
    - Symlinks/hardlinks only point within base_dir
    - No special file types (devices, FIFOs)
    
    Args:
        member: The tar archive member to validate
        base_dir: The base directory for extraction
        
    Returns:
        Tuple of (is_valid, error_message). If valid, error_message is empty.
    """
    member_name = member.name
    
    # Reject absolute paths
    if member_name.startswith('/'):
        return False, f"Absolute path not allowed: {member_name}"
    
    # Reject path traversal in member name
    if '..' in member_name.split('/'):
        return False, f"Path traversal not allowed: {member_name}"
    
    # Calculate the destination path
    dest_path = base_dir / member_name
    
    # Verify destination stays within base_dir
    if not _is_path_within(base_dir, dest_path):
        return False, f"Path escapes extraction directory: {member_name}"
    
    # Reject special file types (devices, FIFOs)
    if member.ischr() or member.isblk():
        return False, f"Device files not allowed: {member_name}"
    if member.isfifo():
        return False, f"FIFO files not allowed: {member_name}"
    
    # Validate symlinks
    if member.issym():
        linkname = member.linkname
        
        # Reject absolute symlink targets
        if linkname.startswith('/'):
            return False, f"Absolute symlink target not allowed: {member_name} -> {linkname}"
        
        # Calculate where the symlink would point
        # Symlink is relative to the directory containing it
        link_dir = dest_path.parent
        link_target = (link_dir / linkname).resolve()
        
        # Verify symlink target stays within base_dir
        if not _is_path_within(base_dir, link_target):
            return False, f"Symlink escapes extraction directory: {member_name} -> {linkname}"
    
    # Validate hardlinks
    if member.islnk():
        linkname = member.linkname
        
        # Reject absolute hardlink targets
        if linkname.startswith('/'):
            return False, f"Absolute hardlink target not allowed: {member_name} -> {linkname}"
        
        # Hardlink target is relative to archive root (base_dir)
        link_target = base_dir / linkname
        
        # Verify hardlink target stays within base_dir
        if not _is_path_within(base_dir, link_target):
            return False, f"Hardlink escapes extraction directory: {member_name} -> {linkname}"
    
    return True, ""


def _safe_extract_tar(tar: tarfile.TarFile, dest_dir: Path, max_files: int) -> None:
    """
    Safely extract a tar archive with full validation.
    
    Validates all members before extraction and raises HTTPException
    if any member fails validation.
    
    Args:
        tar: Open tarfile object
        dest_dir: Destination directory for extraction
        max_files: Maximum number of files allowed
        
    Raises:
        HTTPException: If archive validation fails
    """
    members = tar.getmembers()
    
    # Check file count
    if len(members) > max_files:
        raise HTTPException(
            status_code=400,
            detail={
                "error": f"Archive exceeds maximum file count of {max_files}",
                "code": "TOO_MANY_FILES"
            }
        )
    
    # Validate all members before extraction
    safe_members = []
    for member in members:
        is_valid, error_msg = _validate_tar_member(member, dest_dir)
        if not is_valid:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": f"Invalid archive: {error_msg}",
                    "code": "INVALID_ARCHIVE"
                }
            )
        safe_members.append(member)
    
    # Extract validated members
    tar.extractall(dest_dir, members=safe_members)


async def deploy_local_agent_logic(
    body: DeployLocalRequest,
    current_user: User,
    request: Request,
    create_agent_fn
) -> DeployLocalResponse:
    """
    Deploy a Trinity-compatible local agent.

    This receives a base64-encoded tar.gz archive of a local agent
    directory, validates it's Trinity-compatible (has template.yaml), handles
    versioning if an agent with the same name exists, and creates the agent.

    Credentials should be included in the archive (.env file) — no
    separate credential injection step.

    Args:
        body: Deploy request with archive
        current_user: Authenticated user
        request: FastAPI request object
        create_agent_fn: Function to create agent (create_agent_internal)

    Returns:
        DeployLocalResponse with deployment details
    """
    temp_dir = None

    try:
        # 1. Validate archive size
        try:
            archive_bytes = base64.b64decode(body.archive)
        except Exception as e:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": f"Invalid base64 encoding: {e}",
                    "code": "INVALID_ARCHIVE"
                }
            )

        if len(archive_bytes) > MAX_ARCHIVE_SIZE:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": f"Archive exceeds maximum size of {MAX_ARCHIVE_SIZE // (1024*1024)}MB",
                    "code": "ARCHIVE_TOO_LARGE"
                }
            )

        # 1b. Validate credential count limit
        if body.credentials and len(body.credentials) > MAX_DEPLOY_CREDENTIALS:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": f"Too many credentials: {len(body.credentials)} exceeds limit of {MAX_DEPLOY_CREDENTIALS}",
                    "code": "TOO_MANY_CREDENTIALS"
                }
            )

        # 2. Extract archive to temp directory
        temp_dir = Path(tempfile.mkdtemp(prefix="trinity-deploy-"))
        try:
            with tarfile.open(fileobj=BytesIO(archive_bytes), mode='r:gz') as tar:
                # Security: Safe extraction with full validation
                # - Validates paths stay within temp_dir
                # - Blocks symlinks/hardlinks pointing outside
                # - Rejects device files and FIFOs
                _safe_extract_tar(tar, temp_dir, MAX_FILES)
        except tarfile.TarError as e:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": f"Invalid tar.gz archive: {e}",
                    "code": "INVALID_ARCHIVE"
                }
            )

        # 4. Find the root directory (handle nested extraction)
        contents = list(temp_dir.iterdir())
        if len(contents) == 1 and contents[0].is_dir():
            extract_root = contents[0]
        else:
            extract_root = temp_dir

        # 5. Validate Trinity-compatible
        is_compatible, error_msg, template_data = is_trinity_compatible(extract_root)
        if not is_compatible:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": f"Agent is not Trinity-compatible: {error_msg}",
                    "code": "NOT_TRINITY_COMPATIBLE"
                }
            )

        # 6. Determine agent name
        base_name = body.name or template_data.get("name")
        if not base_name:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "No agent name specified and template.yaml has no name field",
                    "code": "MISSING_NAME"
                }
            )

        base_name = sanitize_agent_name(base_name)

        # 6b. Agent quota enforcement: per-role limits (QUOTA-001)
        # Skip for redeploys of existing agents owned by this user
        existing_versions = get_agents_by_prefix(base_name)
        owned = db.get_agents_by_owner(current_user.username)
        is_redeploy = any(v.name in owned for v in existing_versions)
        if not is_redeploy:
            max_agents = get_agent_quota_for_role(current_user.role)
            if max_agents > 0:
                non_system = [a for a in owned if not (db.get_agent_owner(a) or {}).get("is_system")]
                if len(non_system) >= max_agents:
                    raise HTTPException(
                        status_code=429,
                        detail={
                            "error": f"Agent quota exceeded. You have {len(non_system)}/{max_agents} agents. "
                                     f"Delete an agent to create a new one.",
                            "code": "QUOTA_EXCEEDED",
                            "current": len(non_system),
                            "limit": max_agents
                        }
                    )

        # 7. Version handling
        version_name = get_next_version_name(base_name)
        previous_version = get_latest_version(base_name)
        previous_stopped = False

        if previous_version and previous_version.status == "running":
            # Stop the previous version
            try:
                container = get_agent_container(previous_version.name)
                if container:
                    await container_stop(container)
                    previous_stopped = True
                    logger.info(f"Stopped previous version: {previous_version.name}")
            except Exception as e:
                logger.warning(f"Failed to stop previous version {previous_version.name}: {e}")

        # 8. Copy to deployed-templates directory (#950).
        # The historical /agent-configs/templates mount is intentionally read-only
        # in compose to protect the curated catalog; the prior writability probe
        # always failed and silently fell back to ./config/agent-templates which
        # resolved INSIDE the backend container, leaving the new agent's bind
        # mount pointing at a host path that didn't exist → empty agents.
        # /data is host-mapped (TRINITY_DATA_PATH), writable, owned by UID 1000.
        templates_dir = Path(DEPLOYED_TEMPLATES_DIR_IN_BACKEND)
        try:
            templates_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise HTTPException(
                status_code=500,
                detail={
                    "error": (
                        f"Deployed-templates directory {templates_dir} is not writable: {e}. "
                        f"Check that {DEPLOYED_TEMPLATES_DIR_IN_BACKEND}'s host bind "
                        f"(TRINITY_DATA_PATH default './trinity-data') exists and is owned "
                        f"by UID 1000 (see docs/migrations/NON_ROOT_CONTAINERS_2026-05.md)."
                    ),
                    "code": "DEPLOYED_TEMPLATES_DIR_UNWRITABLE",
                }
            )

        dest_path = templates_dir / version_name

        # Path-containment guard (#950). version_name is already a single
        # sanitized slug (sanitize_agent_name strips path separators), but
        # normalize + verify containment so the value reaching every downstream
        # file access provably stays under templates_dir. This is
        # defense-in-depth AND the CodeQL-recognized path-injection barrier:
        # normalize, inline startswith prefix-check, and use the normalized
        # value downstream.
        _templates_base = os.path.normpath(str(templates_dir))
        _normalized_dest = os.path.normpath(str(dest_path))
        if _normalized_dest != _templates_base and not _normalized_dest.startswith(
            _templates_base + os.sep
        ):
            raise HTTPException(
                status_code=400,
                detail={
                    "error": (
                        f"Resolved template path escapes deployed-templates "
                        f"directory: {version_name}"
                    ),
                    "code": "TEMPLATE_PATH_ESCAPE",
                },
            )
        dest_path = Path(_normalized_dest)

        if dest_path.exists():
            shutil.rmtree(dest_path)

        shutil.copytree(extract_root, dest_path)
        logger.info(f"Copied agent template to: {dest_path}")

        # 10. Create agent
        # Extract runtime config from template
        runtime_config = template_data.get("runtime", {})
        runtime_type = None
        runtime_model = None
        if isinstance(runtime_config, dict):
            runtime_type = runtime_config.get("type")
            runtime_model = runtime_config.get("model")
        elif isinstance(runtime_config, str):
            runtime_type = runtime_config

        agent_config = AgentConfig(
            name=version_name,
            template=f"local:{version_name}",
            type=template_data.get("type", "business-assistant"),
            resources=template_data.get("resources", {"cpu": "2", "memory": "4g"}),
            runtime=runtime_type,
            runtime_model=runtime_model
        )

        # 9b. Process credentials before agent creation
        credentials_imported = {}
        credentials_injected = 0

        # Check for credential files in archive
        env_file = dest_path / ".env"
        if env_file.exists():
            credentials_imported[".env"] = "from_archive"

        mcp_file = dest_path / ".mcp.json"
        if mcp_file.exists():
            credentials_imported[".mcp.json"] = "from_archive"

        # Write credentials from request to template directory
        # These will be copied to the agent workspace during creation
        if body.credentials:
            env_content = "\n".join(f"{k}={v}" for k, v in body.credentials.items())
            # Append to existing .env or create new one
            if env_file.exists():
                existing = env_file.read_text()
                # Append with newline separator
                env_file.write_text(existing.rstrip() + "\n" + env_content + "\n")
                credentials_imported[".env"] = "merged"
            else:
                env_file.write_text(env_content + "\n")
                credentials_imported[".env"] = "created"
            credentials_injected = len(body.credentials)
            logger.info(f"Wrote {credentials_injected} credentials to template for agent {version_name}")

        # 9b-advisory. Warn about MCP servers whose ${VAR} references have no
        # matching credential in the post-merge .env (#950 deferred hardening).
        # Read dest_path/.env — that's where body.credentials were merged just
        # above; extract_root still holds the un-merged archive copy.
        warnings = collect_mcp_credential_warnings(dest_path)
        if warnings:
            logger.info(
                f"Deploy {version_name}: {len(warnings)} MCP credential warning(s)"
            )

        # 9c. Pre-populate the agent's workspace volume from the extracted
        # template (#950). Sidesteps the bind-mount transport entirely:
        # dev compose uses a docker-managed named volume for /data while
        # prod uses a host bind, so any host-path math in crud.py would be
        # right on prod and wrong on dev. By writing into the workspace
        # volume directly here, both environments behave identically.
        # The `.trinity-initialized` marker tells the agent's startup.sh
        # to skip its `/template` -> `/home/developer` copy (which won't
        # run anyway since no /template bind is set up — see crud.py).
        _prepopulate_workspace_from_template(version_name, dest_path)

        agent_status = await create_agent_fn(
            agent_config,
            current_user,
            request,
            skip_name_sanitization=True
        )

        # 11. Return response
        return DeployLocalResponse(
            status="success",
            agent=agent_status,
            versioning=VersioningInfo(
                base_name=base_name,
                previous_version=previous_version.name if previous_version else None,
                previous_version_stopped=previous_stopped,
                new_version=version_name
            ),
            credentials_imported=credentials_imported,
            credentials_injected=credentials_injected,
            warnings=warnings,
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to deploy local agent: {str(e)}"
        )
    finally:
        # Cleanup temp directory
        if temp_dir and temp_dir.exists():
            try:
                shutil.rmtree(temp_dir)
            except Exception:
                pass
