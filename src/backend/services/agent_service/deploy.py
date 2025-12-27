"""
Agent Service Deploy - Local agent deployment logic.

Contains the business logic for deploying local agents via MCP.
"""
import base64
import tarfile
import tempfile
import shutil
import asyncio
import logging
from pathlib import Path
from io import BytesIO

from fastapi import HTTPException, Request

from models import (
    AgentConfig,
    AgentStatus,
    User,
    DeployLocalRequest,
    DeployLocalResponse,
    VersioningInfo,
    CredentialImportResult,
)
from services.template_service import is_trinity_compatible
from services.audit_service import log_audit_event
from services.docker_service import get_agent_container
from utils.helpers import sanitize_agent_name
from .helpers import get_agents_by_prefix, get_next_version_name, get_latest_version

logger = logging.getLogger(__name__)

# Size limits for local deployment
MAX_ARCHIVE_SIZE = 50 * 1024 * 1024  # 50 MB
MAX_CREDENTIALS = 100
MAX_FILES = 1000


async def deploy_local_agent_logic(
    body: DeployLocalRequest,
    current_user: User,
    request: Request,
    create_agent_fn,
    credential_manager
) -> DeployLocalResponse:
    """
    Deploy a Trinity-compatible local agent.

    This receives a base64-encoded tar.gz archive of a local agent
    directory, validates it's Trinity-compatible (has template.yaml), handles
    versioning if an agent with the same name exists, imports credentials,
    and creates the agent.

    Args:
        body: Deploy request with archive and credentials
        current_user: Authenticated user
        request: FastAPI request object
        create_agent_fn: Function to create agent (create_agent_internal)
        credential_manager: Credential manager instance

    Returns:
        DeployLocalResponse with deployment details
    """
    import httpx

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

        # 2. Validate credentials count
        if body.credentials and len(body.credentials) > MAX_CREDENTIALS:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": f"Credentials exceed maximum count of {MAX_CREDENTIALS}",
                    "code": "TOO_MANY_CREDENTIALS"
                }
            )

        # 3. Extract archive to temp directory
        temp_dir = Path(tempfile.mkdtemp(prefix="trinity-deploy-"))
        try:
            with tarfile.open(fileobj=BytesIO(archive_bytes), mode='r:gz') as tar:
                # Security: Check for path traversal
                for member in tar.getmembers():
                    if member.name.startswith('/') or '..' in member.name:
                        raise HTTPException(
                            status_code=400,
                            detail={
                                "error": "Invalid archive: contains path traversal",
                                "code": "INVALID_ARCHIVE"
                            }
                        )

                # Check file count
                if len(tar.getmembers()) > MAX_FILES:
                    raise HTTPException(
                        status_code=400,
                        detail={
                            "error": f"Archive exceeds maximum file count of {MAX_FILES}",
                            "code": "TOO_MANY_FILES"
                        }
                    )

                tar.extractall(temp_dir)
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

        # 7. Version handling
        version_name = get_next_version_name(base_name)
        previous_version = get_latest_version(base_name)
        previous_stopped = False

        if previous_version and previous_version.status == "running":
            # Stop the previous version
            try:
                container = get_agent_container(previous_version.name)
                if container:
                    container.stop()
                    previous_stopped = True
                    logger.info(f"Stopped previous version: {previous_version.name}")
            except Exception as e:
                logger.warning(f"Failed to stop previous version {previous_version.name}: {e}")

        # 8. Import credentials
        cred_results = {}
        if body.credentials:
            for key, value in body.credentials.items():
                result = credential_manager.import_credential_with_conflict_resolution(
                    key, value, current_user.username
                )
                cred_results[key] = CredentialImportResult(
                    status=result["status"],
                    name=result["name"],
                    original=result.get("original")
                )

        # 9. Copy to templates directory
        # Try /agent-configs/templates first, but check if writable (not just if exists)
        # The read-only mount makes this path exist but not writable
        templates_dir = Path("/agent-configs/templates")

        # Check if writable by attempting to create a test file
        try:
            test_file = templates_dir / ".write_test"
            test_file.touch()
            test_file.unlink()
        except (OSError, PermissionError):
            # Fall back to local config path
            templates_dir = Path("./config/agent-templates")

        if not templates_dir.exists():
            templates_dir.mkdir(parents=True, exist_ok=True)

        dest_path = templates_dir / version_name
        if dest_path.exists():
            shutil.rmtree(dest_path)

        shutil.copytree(extract_root, dest_path)
        logger.info(f"Copied agent template to: {dest_path}")

        # 10. Create agent
        agent_config = AgentConfig(
            name=version_name,
            template=f"local:{version_name}",
            type=template_data.get("type", "business-assistant"),
            resources=template_data.get("resources", {"cpu": "2", "memory": "4g"})
        )

        agent_status = await create_agent_fn(
            agent_config,
            current_user,
            request,
            skip_name_sanitization=True
        )

        # 11. Hot-reload credentials if any were imported
        if body.credentials:
            try:
                # Wait a moment for the agent to start
                await asyncio.sleep(2)

                async with httpx.AsyncClient(timeout=30.0) as client:
                    await client.post(
                        f"http://agent-{version_name}:8000/api/credentials/update",
                        json={
                            "credentials": {k: v for k, v in body.credentials.items()},
                            "mcp_config": None
                        }
                    )
                    logger.info(f"Hot-reloaded {len(body.credentials)} credentials into {version_name}")
            except Exception as e:
                logger.warning(f"Failed to hot-reload credentials: {e}")

        # 12. Audit log
        await log_audit_event(
            event_type="agent_management",
            action="deploy_local",
            user_id=current_user.username,
            agent_name=version_name,
            ip_address=request.client.host if request.client else None,
            result="success",
            details={
                "base_name": base_name,
                "previous_version": previous_version.name if previous_version else None,
                "previous_stopped": previous_stopped,
                "credentials_count": len(cred_results),
                "archive_size": len(archive_bytes)
            }
        )

        # 13. Return response
        return DeployLocalResponse(
            status="success",
            agent=agent_status,
            versioning=VersioningInfo(
                base_name=base_name,
                previous_version=previous_version.name if previous_version else None,
                previous_version_stopped=previous_stopped,
                new_version=version_name
            ),
            credentials_imported={k: v.dict() for k, v in cred_results.items()},
            credentials_injected=len(cred_results)
        )

    except HTTPException:
        raise
    except Exception as e:
        await log_audit_event(
            event_type="agent_management",
            action="deploy_local",
            user_id=current_user.username,
            ip_address=request.client.host if request.client else None,
            result="failed",
            severity="error",
            details={"error": str(e)}
        )
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
