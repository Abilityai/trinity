"""
System deployment routes for the Trinity backend.

Provides endpoints for deploying multi-agent systems from YAML manifests.
"""
import json
import logging
from typing import Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from models import (
    User,
    AgentConfig,
    SystemDeployFailure,
    SystemDeployRequest,
    SystemDeployResponse,
)
from database import db
from dependencies import get_current_user, require_role
from utils.credential_sanitizer import redact_url_userinfo, sanitize_text
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

# trinity-enterprise#125: cap on a single agent-create failure reason in the
# deploy report (the full text is still in the backend log).
_REASON_MAX_LEN = 500


def _failure_reason(exc: Exception) -> Tuple[str, Optional[int]]:
    """Normalize an agent-create exception into a (reason, status_code) pair.

    HTTPException details may be dicts (e.g. QUOTA_EXCEEDED) — prefer their
    'error' field. Reasons are credential-sanitized and URL-userinfo-redacted
    at this exit point because git/GitHub errors can embed PAT-bearing remote
    URLs (learnings 2026-07-14) and the deploy report is a durable response
    surface (trinity-enterprise#125).
    """
    status_code: Optional[int] = None
    if isinstance(exc, HTTPException):
        status_code = exc.status_code
        detail = exc.detail
        if isinstance(detail, dict):
            reason = detail.get("error")
            if not isinstance(reason, str) or not reason:
                reason = json.dumps(detail, default=str)
        else:
            reason = str(detail)
    else:
        reason = str(exc)
    reason = sanitize_text(redact_url_userinfo(reason))
    return reason[:_REASON_MAX_LEN], status_code


@router.post("/deploy", response_model=SystemDeployResponse)
async def deploy_system(
    body: SystemDeployRequest,
    request: Request,
    current_user: User = Depends(require_role("creator"))
):
    """
    Deploy a multi-agent system from YAML manifest.

    This is a "recipe" deployment - agents become independent after creation.

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

        # 5. Create all agents — best-effort by default (trinity-enterprise#125):
        # a per-agent failure is collected and the remaining agents still
        # deploy. `strict=True` restores abort-on-first-error, preserving the
        # failing agent's original status code. Each failed create self-cleans
        # via create_agent_internal's own rollback (#1484 _RollbackHandles).
        created_agents = []
        failed: list[SystemDeployFailure] = []
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

            except Exception as e:
                reason, status_code = _failure_reason(e)
                logger.error(f"Failed to create agent '{final_name}': {reason}")

                if body.strict:
                    raise HTTPException(
                        status_code=status_code or 500,
                        detail={
                            "error": "Deployment failed",
                            "failed_at": final_name,
                            "created": created_agents,
                            "reason": reason
                        }
                    )

                failed.append(SystemDeployFailure(
                    name=final_name,
                    short_name=short_name,
                    template=config.template,
                    reason=reason,
                    status_code=status_code
                ))

        # 6. Total failure: nothing was created — return the full report with a
        # non-2xx status so code-only callers (curl -f) don't read it as success.
        if not created_agents:
            logger.error(
                f"System '{manifest.name}' deploy failed: 0/{len(agent_names)} agents created"
            )
            response = SystemDeployResponse(
                status="failed",
                system_name=manifest.name,
                agents_created=[],
                prompt_updated=False,
                warnings=all_warnings,
                failed=failed
            )
            return JSONResponse(status_code=500, content=response.model_dump())

        # 6b. Update trinity_prompt (if provided) — moved after the create loop
        # (trinity-enterprise#125) so a totally-failed deploy never mutates the
        # platform-wide prompt; injection happens at agent start (step 12).
        prompt_updated = False
        if manifest.prompt:
            db.set_setting("trinity_prompt", manifest.prompt)
            prompt_updated = True
            logger.info(f"Updated trinity_prompt for system '{manifest.name}'")

        # 6c. Scope post-create configuration to the agents that were actually
        # created (the config functions skip short_names missing from the map).
        created_set = set(created_agents)
        created_map = {s: f for s, f in agent_names.items() if f in created_set}

        if failed:
            failed_names = [f.name for f in failed]
            all_warnings.append(
                f"Partial deploy: {len(failed)} agent(s) failed to create: {failed_names}. "
                "Re-deploying this manifest will create suffixed duplicates of the "
                "already-created agents — fix the cause and create the missing agents "
                "individually (converge support: trinity-enterprise#124)."
            )
            # An orchestrator-workers system without its orchestrator is a
            # headless fleet: survivors keep zero inter-agent permissions.
            if (
                manifest.permissions
                and manifest.permissions.preset == "orchestrator-workers"
                and "orchestrator" in agent_names
                and agent_names["orchestrator"] not in created_set
            ):
                all_warnings.append(
                    "Orchestrator agent failed to create; the deployed workers have "
                    "no inter-agent permissions — the system may be non-functional."
                )

        # 7–11. Post-create configuration — each phase is best-effort
        # (trinity-enterprise#125): once agents exist, a config failure must
        # degrade to a warning, never abort the report the caller was promised.

        # 7. Configure shared folders (Phase 2)
        folders_configured = 0
        try:
            folders_configured = configure_folders(
                agent_names=created_map,
                agents_config=manifest.agents
            )
            logger.info(f"Configured {folders_configured} folder configs for system '{manifest.name}'")
        except Exception as e:
            reason, _ = _failure_reason(e)
            logger.exception(f"Folder configuration failed for system '{manifest.name}'")
            all_warnings.append(f"Failed to configure shared folders: {reason}")

        # 8. Configure permissions (Phase 2)
        permissions_count = 0
        if manifest.permissions:
            try:
                permissions_count = await configure_permissions(
                    agent_names=created_map,
                    permissions=manifest.permissions,
                    created_by=current_user.username
                )
                logger.info(f"Configured {permissions_count} permissions for system '{manifest.name}'")
            except Exception as e:
                reason, _ = _failure_reason(e)
                logger.exception(f"Permission configuration failed for system '{manifest.name}'")
                all_warnings.append(f"Failed to configure permissions: {reason}")

        # 9. Create schedules (Phase 2)
        schedules_count = 0
        try:
            schedules_count = create_schedules(
                agent_names=created_map,
                agents_config=manifest.agents,
                owner_username=current_user.username
            )
            logger.info(f"Created {schedules_count} schedules for system '{manifest.name}'")
        except Exception as e:
            reason, _ = _failure_reason(e)
            logger.exception(f"Schedule creation failed for system '{manifest.name}'")
            all_warnings.append(f"Failed to create schedules: {reason}")

        # 10. Configure tags (ORG-001 Phase 4)
        tags_count = 0
        try:
            tags_count = configure_tags(
                system_name=manifest.name,
                agent_names=created_map,
                agents_config=manifest.agents,
                default_tags=manifest.default_tags
            )
            logger.info(f"Configured {tags_count} tags for system '{manifest.name}'")
        except Exception as e:
            reason, _ = _failure_reason(e)
            logger.exception(f"Tag configuration failed for system '{manifest.name}'")
            all_warnings.append(f"Failed to configure tags: {reason}")

        # 11. Create System View (ORG-001 Phase 4, optional)
        # create_system_view self-guards (returns None on error).
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
            status="partial" if failed else "deployed",
            system_name=manifest.name,
            agents_created=created_agents,
            prompt_updated=prompt_updated,
            permissions_configured=permissions_count,
            schedules_created=schedules_count,
            tags_configured=tags_count,
            system_view_created=system_view_id,
            warnings=all_warnings,
            failed=failed
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
