"""
System deployment routes for the Trinity backend.

Provides endpoints for deploying multi-agent systems from YAML manifests.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from models import (
    User,
    SystemDeployRequest,
    SystemDeployResponse,
)
from database import db
from dependencies import get_current_user, require_role
from services.system_service import deploy_manifest
from services.docker_utils import container_stop

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/systems", tags=["systems"])


@router.post("/deploy", response_model=SystemDeployResponse)
async def deploy_system(
    body: SystemDeployRequest,
    request: Request,
    current_user: User = Depends(require_role("creator"))
):
    """
    Deploy a multi-agent system from YAML manifest.

    Thin HTTP wrapper over `services.system_service.deploy_manifest`
    (orchestration moved there in trinity-enterprise#124 so the first-run
    seeder can reuse it without importing a router — Invariant #1).

    Best-effort by default (trinity-enterprise#125): a per-agent create failure
    is reported in `failed[]` and the remaining agents still deploy; callers
    must check `status`, not just the HTTP code. `status` is "deployed" (all
    created), "partial" (some failed, HTTP 200), "failed" (none created,
    HTTP 500 with the full report as the body), or "valid" (dry_run).

    Args:
        body.manifest: YAML string defining the system
        body.dry_run: If true, validate only without creating agents
        body.strict: If true, abort on the first agent-create failure
            (legacy behavior), preserving the failure's original status code

    Returns:
        SystemDeployResponse with created agents, per-agent failures, and
        configuration summary
    """
    result = await deploy_manifest(
        body.manifest,
        current_user,
        request,
        dry_run=body.dry_run,
        strict=body.strict,
    )

    # Total failure: nothing was created — non-2xx so code-only callers
    # (curl -f) don't read it as success (trinity-enterprise#125).
    if result.status == "failed":
        return JSONResponse(status_code=500, content=result.model_dump())

    return result


@router.get("")
async def list_systems(current_user: User = Depends(get_current_user)):
    """
    List all systems (agents grouped by prefix).

    Groups agents by system prefix (before first '-').
    Returns system summaries with agent counts and details.
    """
    try:
        # Import here to avoid circular dependency
        from routers.agents import get_accessible_agents

        # Get all agents user can access
        agents = get_accessible_agents(current_user)

        # Group by system prefix
        systems_dict: dict = {}
        for agent in agents:
            # Extract system prefix (everything except last component after final '-')
            # Example: "my-system-abc-worker1" -> "my-system-abc"
            if '-' in agent['name']:
                parts = agent['name'].split('-')
                prefix = '-'.join(parts[:-1])  # All parts except the last (short name)
                if prefix not in systems_dict:
                    systems_dict[prefix] = {
                        "name": prefix,
                        "agents": [],
                        "agent_count": 0,
                        "created_at": agent.get('created_at')
                    }
                systems_dict[prefix]["agents"].append({
                    "name": agent['name'],
                    "status": agent.get('status', 'unknown'),
                    "template": agent.get('template')
                })
                systems_dict[prefix]["agent_count"] += 1

        # Sort by created_at (newest first)
        systems = list(systems_dict.values())
        systems.sort(key=lambda s: s.get('created_at') or '', reverse=True)

        return {"systems": systems}

    except Exception as e:
        logger.exception(f"Failed to list systems: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to list systems: {str(e)}")


@router.get("/{system_name}")
async def get_system(
    system_name: str,
    current_user: User = Depends(get_current_user)
):
    """
    Get system details with all agents.

    Returns detailed information about a system including all its agents,
    permissions, folders, and schedules.
    """
    try:
        from routers.agents import get_accessible_agents

        # Get all agents user can access
        agents = get_accessible_agents(current_user)

        # Filter agents by system prefix
        system_agents = [
            agent for agent in agents
            if agent['name'].startswith(f"{system_name}-")
        ]

        if not system_agents:
            raise HTTPException(
                status_code=404,
                detail=f"System '{system_name}' not found or no accessible agents"
            )

        # Get detailed info for each agent
        detailed_agents = []
        for agent in system_agents:
            agent_detail = {
                "name": agent['name'],
                "status": agent.get('status', 'unknown'),
                "template": agent.get('template'),
                "created_at": agent.get('created_at')
            }

            # Try to get additional details (permissions, folders, schedules)
            try:
                # Get permissions
                perms = db.get_agent_permissions(agent['name'])
                agent_detail["permissions"] = [p["target_agent"] for p in perms]

                # Get folders config
                folder_config = db.get_agent_folder_config(agent['name'])
                if folder_config:
                    agent_detail["folders"] = {
                        "expose": folder_config["expose_enabled"],
                        "consume": folder_config["consume_enabled"]
                    }

                # Get schedules
                schedules = db.get_agent_schedules(agent['name'])
                agent_detail["schedules"] = schedules

            except Exception as e:
                logger.warning(f"Failed to get details for agent {agent['name']}: {e}")

            detailed_agents.append(agent_detail)

        return {
            "name": system_name,
            "agent_count": len(detailed_agents),
            "agents": detailed_agents
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to get system '{system_name}': {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get system: {str(e)}")


@router.post("/{system_name}/restart")
async def restart_system(
    system_name: str,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Restart all agents in a system.

    Finds all agents with the given system prefix and stops then starts them.
    Useful after configuration changes.
    """
    try:
        from routers.agents import get_accessible_agents, start_agent_internal
        from services.docker_service import get_agent_container

        # Get all agents user can access
        agents = get_accessible_agents(current_user)

        # Filter agents by system prefix
        system_agents = [
            agent for agent in agents
            if agent['name'].startswith(f"{system_name}-")
        ]

        if not system_agents:
            raise HTTPException(
                status_code=404,
                detail=f"System '{system_name}' not found or no accessible agents"
            )

        restarted = []
        failed = []

        for agent in system_agents:
            agent_name = agent['name']
            try:
                # Stop agent
                if agent.get('status') == 'running':
                    container = get_agent_container(agent_name)
                    if container:
                        await container_stop(container)
                        logger.info(f"Stopped agent '{agent_name}' for system restart")

                # Start agent (with Trinity injection)
                await start_agent_internal(agent_name)
                restarted.append(agent_name)
                logger.info(f"Restarted agent '{agent_name}' for system '{system_name}'")

            except Exception as e:
                logger.error(f"Failed to restart agent '{agent_name}': {e}")
                failed.append(agent_name)

        return {
            "restarted": restarted,
            "failed": failed
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to restart system '{system_name}': {e}")
        raise HTTPException(status_code=500, detail=f"Failed to restart system: {str(e)}")


@router.get("/{system_name}/manifest", response_class=PlainTextResponse)
async def get_system_manifest(
    system_name: str,
    current_user: User = Depends(get_current_user)
):
    """
    Export system as YAML manifest.

    Generates a YAML manifest from the current system configuration.
    Useful for backup, documentation, or replicating systems.
    """
    try:
        from routers.agents import get_accessible_agents
        from services.system_service import export_manifest

        # Get all agents user can access
        agents = get_accessible_agents(current_user)

        # Filter agents by system prefix
        system_agents = [
            agent for agent in agents
            if agent['name'].startswith(f"{system_name}-")
        ]

        if not system_agents:
            raise HTTPException(
                status_code=404,
                detail=f"System '{system_name}' not found or no accessible agents"
            )

        # Export manifest
        yaml_content = export_manifest(system_name, system_agents)

        return yaml_content

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to export manifest for system '{system_name}': {e}")
        raise HTTPException(status_code=500, detail=f"Failed to export manifest: {str(e)}")
