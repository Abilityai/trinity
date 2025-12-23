# System Manifest - Phase 2: Configuration & Startup

> **Goal**: Apply permissions, shared folders, schedules, and start all agents
> **Prerequisite**: Phase 1 complete and tested
> **Depends On**: Phase 1 creates agents; Phase 2 configures and starts them

---

## Overview

This phase completes the deployment by:
1. Applying permission presets (full-mesh, orchestrator-workers, none, explicit)
2. Configuring shared folders per agent
3. Creating schedules per agent
4. Starting all agents (triggers Trinity injection with updated prompt)

---

## Files to Modify

### 1. `src/backend/services/system_service.py` - Add Configuration Functions

Add these functions to the existing file:

```python
from db_models import ScheduleCreate


async def configure_permissions(
    agent_names: Dict[str, str],  # {short_name: final_name}
    permissions: Optional[SystemPermissions],
    created_by: str
) -> int:
    """
    Apply permission configuration based on preset or explicit rules.

    Args:
        agent_names: Mapping of short names to final agent names
        permissions: Permission configuration from manifest
        created_by: Username for audit trail

    Returns:
        Number of permissions configured
    """
    if not permissions:
        return 0

    final_names = list(agent_names.values())
    permissions_count = 0

    if permissions.preset == "full-mesh":
        # Every agent can communicate with every other agent
        for source in final_names:
            targets = [t for t in final_names if t != source]
            if targets:
                db.set_agent_permissions(source, targets, created_by)
                permissions_count += len(targets)

    elif permissions.preset == "orchestrator-workers":
        # Only orchestrator can call workers
        orchestrator_short = "orchestrator"
        if orchestrator_short in agent_names:
            orchestrator = agent_names[orchestrator_short]
            workers = [n for n in final_names if n != orchestrator]
            if workers:
                db.set_agent_permissions(orchestrator, workers, created_by)
                permissions_count = len(workers)

    elif permissions.preset == "none":
        # No permissions - nothing to do
        pass

    elif permissions.explicit:
        # Apply explicit permission matrix
        for source_short, target_shorts in permissions.explicit.items():
            source = agent_names.get(source_short)
            if not source:
                continue
            targets = [agent_names[t] for t in target_shorts if t in agent_names]
            if targets:
                db.set_agent_permissions(source, targets, created_by)
                permissions_count += len(targets)

    return permissions_count


async def configure_folders(
    agent_names: Dict[str, str],  # {short_name: final_name}
    agents_config: Dict[str, SystemAgentConfig]
) -> None:
    """
    Configure shared folder settings for all agents.

    Args:
        agent_names: Mapping of short names to final agent names
        agents_config: Agent configurations from manifest
    """
    for short_name, config in agents_config.items():
        final_name = agent_names.get(short_name)
        if not final_name:
            continue

        if config.folders:
            expose = config.folders.get("expose", False)
            consume = config.folders.get("consume", False)

            db.upsert_shared_folder_config(
                agent_name=final_name,
                expose_enabled=expose,
                consume_enabled=consume
            )


async def create_schedules(
    agent_names: Dict[str, str],  # {short_name: final_name}
    agents_config: Dict[str, SystemAgentConfig],
    owner_id: int
) -> int:
    """
    Create schedules for all agents.

    Args:
        agent_names: Mapping of short names to final agent names
        agents_config: Agent configurations from manifest
        owner_id: User ID for schedule ownership

    Returns:
        Number of schedules created
    """
    schedules_count = 0

    for short_name, config in agents_config.items():
        final_name = agent_names.get(short_name)
        if not final_name or not config.schedules:
            continue

        for schedule_data in config.schedules:
            schedule_create = ScheduleCreate(
                name=schedule_data["name"],
                cron_expression=schedule_data["cron"],
                message=schedule_data["message"],
                enabled=schedule_data.get("enabled", True),
                timezone=schedule_data.get("timezone", "UTC"),
                description=schedule_data.get("description")
            )

            # Use username for create_schedule (it expects username, not ID)
            # The db.create_schedule function handles the lookup
            db.create_schedule(
                agent_name=final_name,
                username=str(owner_id),  # Will be looked up in create_schedule
                schedule_data=schedule_create
            )
            schedules_count += 1

    return schedules_count


async def start_all_agents(agent_names: List[str]) -> None:
    """
    Start all created agents.

    This triggers Trinity meta-prompt injection with the updated trinity_prompt.

    Args:
        agent_names: List of agent names to start
    """
    from routers.agents import start_agent_internal

    for agent_name in agent_names:
        try:
            await start_agent_internal(agent_name)
        except Exception as e:
            logger.warning(f"Failed to start agent '{agent_name}': {e}")
            # Continue starting other agents even if one fails
```

---

### 2. `src/backend/routers/systems.py` - Complete Deploy Flow

Update the `deploy_system` endpoint to add configuration steps after agent creation:

```python
# After the "Create all agents" section (step 6), add:

        # 7. Configure permissions
        permissions_count = 0
        if manifest.permissions:
            permissions_count = await configure_permissions(
                agent_names=agent_names,
                permissions=manifest.permissions,
                created_by=current_user.username
            )
            logger.info(f"Configured {permissions_count} permissions for system '{manifest.name}'")

        # 8. Configure folders
        await configure_folders(
            agent_names=agent_names,
            agents_config=manifest.agents
        )
        logger.info(f"Configured folders for system '{manifest.name}'")

        # 9. Create schedules
        schedules_count = await create_schedules(
            agent_names=agent_names,
            agents_config=manifest.agents,
            owner_id=current_user.id
        )
        logger.info(f"Created {schedules_count} schedules for system '{manifest.name}'")

        # 10. Start all agents (triggers Trinity injection with updated prompt)
        await start_all_agents(created_agents)
        logger.info(f"Started all agents for system '{manifest.name}'")

# Update the final response to include the counts:
        return SystemDeployResponse(
            status="deployed",
            system_name=manifest.name,
            agents_created=created_agents,
            prompt_updated=prompt_updated,
            permissions_configured=permissions_count,
            schedules_created=schedules_count,
            warnings=all_warnings
        )
```

Add imports at the top:
```python
from services.system_service import (
    parse_manifest,
    validate_manifest,
    resolve_agent_names,
    configure_permissions,
    configure_folders,
    create_schedules,
    start_all_agents
)
```

---

### 3. `src/backend/routers/agents.py` - Extract Start Internal

Extract `start_agent_internal()` similar to `create_agent_internal()`:

```python
async def start_agent_internal(agent_name: str) -> None:
    """
    Internal function to start an agent.

    Used by both the API endpoint and system deployment.
    Triggers Trinity meta-prompt injection.
    """
    # ... existing start logic from the endpoint ...
    # Including inject_trinity_meta_prompt call


@router.post("/{agent_name}/start")
async def start_agent_endpoint(
    agent_name: str,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Start a stopped agent container."""
    # Auth check
    if not db.can_user_access_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="Access denied")

    await start_agent_internal(agent_name)
    # ... rest of endpoint ...
```

---

## Testing Phase 2

### Test 1: Full-Mesh Permissions

```yaml
# full-mesh-test.yaml
name: mesh-test
agents:
  alpha:
    template: local:business-assistant
  beta:
    template: local:business-assistant
  gamma:
    template: local:business-assistant
permissions:
  preset: full-mesh
```

```bash
curl -X POST http://localhost:8000/api/systems/deploy \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"manifest": "<yaml above>"}'

# Verify permissions:
curl http://localhost:8000/api/agents/mesh-test-alpha/permissions \
  -H "Authorization: Bearer $TOKEN"
# Expected: permitted_agents includes beta and gamma

curl http://localhost:8000/api/agents/mesh-test-beta/permissions \
  -H "Authorization: Bearer $TOKEN"
# Expected: permitted_agents includes alpha and gamma
```

### Test 2: Orchestrator-Workers Permissions

```yaml
# orchestrator-test.yaml
name: orch-test
agents:
  orchestrator:
    template: local:business-assistant
  worker1:
    template: local:business-assistant
  worker2:
    template: local:business-assistant
permissions:
  preset: orchestrator-workers
```

```bash
# Deploy and verify:
curl http://localhost:8000/api/agents/orch-test-orchestrator/permissions \
  -H "Authorization: Bearer $TOKEN"
# Expected: permitted_agents includes worker1 and worker2

curl http://localhost:8000/api/agents/orch-test-worker1/permissions \
  -H "Authorization: Bearer $TOKEN"
# Expected: permitted_agents is empty (workers can't call others)
```

### Test 3: Explicit Permissions

```yaml
# explicit-test.yaml
name: explicit-test
agents:
  manager:
    template: local:business-assistant
  analyst:
    template: local:business-assistant
  writer:
    template: local:business-assistant
permissions:
  explicit:
    manager: [analyst, writer]
    analyst: [writer]
    writer: []
```

```bash
# Verify:
# manager can call analyst and writer
# analyst can call writer only
# writer can call no one
```

### Test 4: Shared Folders

```yaml
# folders-test.yaml
name: folders-test
agents:
  producer:
    template: local:business-assistant
    folders:
      expose: true
      consume: false
  consumer:
    template: local:business-assistant
    folders:
      expose: false
      consume: true
permissions:
  preset: full-mesh
```

```bash
curl http://localhost:8000/api/agents/folders-test-producer/folders \
  -H "Authorization: Bearer $TOKEN"
# Expected: expose_enabled=true, consume_enabled=false

curl http://localhost:8000/api/agents/folders-test-consumer/folders \
  -H "Authorization: Bearer $TOKEN"
# Expected: expose_enabled=false, consume_enabled=true
```

### Test 5: Schedules

```yaml
# schedules-test.yaml
name: sched-test
agents:
  worker:
    template: local:business-assistant
    schedules:
      - name: hourly-check
        cron: "0 * * * *"
        message: "Run hourly health check"
        enabled: false
        timezone: America/New_York
      - name: daily-report
        cron: "0 9 * * *"
        message: "Generate daily report"
        enabled: false
```

```bash
curl http://localhost:8000/api/agents/sched-test-worker/schedules \
  -H "Authorization: Bearer $TOKEN"
# Expected: 2 schedules listed (both disabled)
```

### Test 6: Complete System Deployment

```yaml
# complete-system.yaml
name: content-production
description: Full content pipeline

prompt: |
  You are part of the Content Production system.
  Always save outputs to shared-out folder.
  Use JSON for inter-agent communication.

agents:
  orchestrator:
    template: local:business-assistant
    resources:
      cpu: "2"
      memory: "4g"
    folders:
      expose: true
      consume: true
    schedules:
      - name: morning-planning
        cron: "0 9 * * *"
        message: "Plan today's content tasks"
        enabled: false

  writer:
    template: local:business-assistant
    folders:
      expose: true
      consume: true

  reviewer:
    template: local:business-assistant
    folders:
      expose: true
      consume: true

permissions:
  preset: full-mesh
```

```bash
curl -X POST http://localhost:8000/api/systems/deploy \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"manifest": "<yaml above>"}'

# Expected response:
{
  "status": "deployed",
  "system_name": "content-production",
  "agents_created": [
    "content-production-orchestrator",
    "content-production-writer",
    "content-production-reviewer"
  ],
  "prompt_updated": true,
  "permissions_configured": 6,  # 3 agents × 2 targets each
  "schedules_created": 1,
  "warnings": []
}

# Verify all agents running:
curl http://localhost:8000/api/agents \
  -H "Authorization: Bearer $TOKEN"
# All three should be status: "running"

# Verify trinity_prompt updated:
curl http://localhost:8000/api/settings/trinity_prompt \
  -H "Authorization: Bearer $TOKEN"
```

---

## Completion Checklist

- [ ] Configuration functions added to `system_service.py`
- [ ] Deploy endpoint updated with configuration steps
- [ ] `start_agent_internal()` extracted in `agents.py`
- [ ] Test 1 (full-mesh) passing
- [ ] Test 2 (orchestrator-workers) passing
- [ ] Test 3 (explicit) passing
- [ ] Test 4 (folders) passing
- [ ] Test 5 (schedules) passing
- [ ] Test 6 (complete system) passing
- [ ] Update `docs/memory/changelog.md`
- [ ] Update `docs/memory/requirements.md` (10.7 status to ✅)

---

## Post-Implementation

After both phases complete:
1. Create feature flow: `docs/memory/feature-flows/system-manifest.md`
2. Update roadmap: Move 10.7 to completed
3. Consider Phase 3 (UI) if needed
