"""
System deployment routes for the Trinity backend.

Provides endpoints for deploying multi-agent systems from YAML manifests.
"""
import logging
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse

from models import User, AgentConfig, SystemDeployRequest, SystemDeployResponse
from database import db
from dependencies import get_current_user, require_role
from services.system_service import (
    parse_manifest,
    validate_manifest,
    resolve_agent_names,
    configure_permissions,
    configure_folders,
    create_schedules,
    configure_tags,
    create_system_view,
    start_all_agents
)
from services.docker_utils import container_stop

# Import for agent creation (reuse existing logic)
from routers.agents import create_agent_internal

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

    This is a "recipe" deployment - agents become independent after creation.

    Args:
        body.manifest: YAML string defining the system
        body.dry_run: If true, validate only without creating agents

    Returns:
        SystemDeployResponse with created agents and configuration summary
    """
    try:
        # 1. Parse YAML
        try:
            manifest = parse_manifest(body.manifest)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        # 2. Validate manifest
        try:
            validation_warnings = validate_manifest(manifest)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        # 3. Resolve agent names (handle conflicts)
        agent_names, name_warnings = resolve_agent_names(
            manifest.name,
            manifest.agents
        )
        all_warnings = validation_warnings + name_warnings

        # 4. If dry_run, return preview
        if body.dry_run:
            agents_to_create = [
                {
                    "name": final_name,
                    "short_name": short_name,
                    "template": manifest.agents[short_name].template
                }
                for short_name, final_name in agent_names.items()
            ]

            return SystemDeployResponse(
                status="valid",
                system_name=manifest.name,
                agents_created=[],
                agents_to_create=agents_to_create,
                prompt_updated=bool(manifest.prompt),
                warnings=all_warnings
            )

        # 5. Update trinity_prompt (if provided)
        prompt_updated = False
        if manifest.prompt:
            db.set_setting("trinity_prompt", manifest.prompt)
            prompt_updated = True
            logger.info(f"Updated trinity_prompt for system '{manifest.name}'")

        # 6. Create all agents
        created_agents = []
        for short_name, config in manifest.agents.items():
            final_name = agent_names[short_name]

            try:
                # Build AgentConfig for existing create_agent logic
                agent_config = AgentConfig(
                    name=final_name,
                    template=config.template,
                    resources=config.resources or {"cpu": "2", "memory": "4g"}
                )

                # Create agent using existing internal function
                await create_agent_internal(
                    config=agent_config,
                    current_user=current_user,
                    request=request,
                    skip_name_sanitization=True  # Name already validated
                )

                created_agents.append(final_name)
                logger.info(f"Created agent '{final_name}' for system '{manifest.name}'")

            except HTTPException as e:
                # Re-raise HTTP exceptions (they have proper status codes)
                logger.error(f"Failed to create agent '{final_name}': {e.detail}")
                raise HTTPException(
                    status_code=500,
                    detail={
                        "error": "Deployment failed",
                        "failed_at": final_name,
                        "created": created_agents,
                        "reason": e.detail
                    }
                )

            except Exception as e:
                # Log partial failure
                logger.error(f"Failed to create agent '{final_name}': {e}")
                raise HTTPException(
                    status_code=500,
                    detail={
                        "error": "Deployment failed",
                        "failed_at": final_name,
                        "created": created_agents,
                        "reason": str(e)
                    }
                )

        # 7. Configure shared folders (Phase 2)
        folders_configured = configure_folders(
            agent_names=agent_names,
            agents_config=manifest.agents
        )
        logger.info(f"Configured {folders_configured} folder configs for system '{manifest.name}'")

        # 8. Configure permissions (Phase 2)
        permissions_count = 0
        if manifest.permissions:
            permissions_count = await configure_permissions(
                agent_names=agent_names,
                permissions=manifest.permissions,
                created_by=current_user.username
            )
            logger.info(f"Configured {permissions_count} permissions for system '{manifest.name}'")

        # 9. Create schedules (Phase 2)
        schedules_count = create_schedules(
            agent_names=agent_names,
            agents_config=manifest.agents,
            owner_username=current_user.username
        )
        logger.info(f"Created {schedules_count} schedules for system '{manifest.name}'")

        # 10. Configure tags (ORG-001 Phase 4)
        tags_count = configure_tags(
            system_name=manifest.name,
            agent_names=agent_names,
            agents_config=manifest.agents,
            default_tags=manifest.default_tags
        )
        logger.info(f"Configured {tags_count} tags for system '{manifest.name}'")

        # 11. Create System View (ORG-001 Phase 4, optional)
        system_view_id = None
        if manifest.system_view:
            system_view_id = create_system_view(
                system_name=manifest.name,
                system_view=manifest.system_view,
                default_tags=manifest.default_tags,
                owner_id=str(current_user.id)
            )
            if system_view_id:
                logger.info(f"Created System View '{manifest.system_view.name}' (ID: {system_view_id}) for system '{manifest.name}'")

        # 12. Start all agents (triggers Trinity injection with updated prompt)
        start_results = await start_all_agents(created_agents)
        agents_started = sum(1 for status in start_results.values() if status == "started")
        agents_failed = len(created_agents) - agents_started

        if agents_failed > 0:
            failed_agents = [name for name, status in start_results.items() if status != "started"]
            all_warnings.append(f"Failed to start {agents_failed} agents: {failed_agents}")

        logger.info(f"Started {agents_started}/{len(created_agents)} agents for system '{manifest.name}'")

        return SystemDeployResponse(
            status="deployed",
            system_name=manifest.name,
            agents_created=created_agents,
            prompt_updated=prompt_updated,
            permissions_configured=permissions_count,
            schedules_created=schedules_count,
            tags_configured=tags_count,
            system_view_created=system_view_id,
            warnings=all_warnings
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"System deployment failed: {e}")
        raise HTTPException(status_code=500, detail=f"Deployment failed: {str(e)}")


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
