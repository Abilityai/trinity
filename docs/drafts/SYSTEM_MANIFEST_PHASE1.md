# System Manifest - Phase 1: Core Deployment Engine

> **Goal**: Parse YAML, create agents with proper naming, handle conflicts, update trinity_prompt
> **Prerequisite**: Read `docs/drafts/SYSTEM_MANIFEST_SIMPLIFIED.md` for full design context
> **Access Control**: Any authenticated user (not admin-only)

---

## Overview

This phase implements the core deployment endpoint that:
1. Parses and validates YAML manifests
2. Creates agents with `{system}-{agent}` naming convention
3. Handles naming conflicts with `_N` suffix
4. Updates the global `trinity_prompt` setting
5. Supports dry_run mode for validation

---

## Files to Create

### 1. `src/backend/models.py` - Add System Manifest Models

Add these models at the end of the file:

```python
# ============================================================================
# System Manifest Models (Recipe-based Multi-Agent Deployment)
# ============================================================================

class SystemAgentConfig(BaseModel):
    """Configuration for a single agent in a system manifest."""
    template: str  # e.g., "github:Org/repo" or "local:business-assistant"
    resources: Optional[dict] = None  # {"cpu": "2", "memory": "4g"}
    folders: Optional[dict] = None  # {"expose": bool, "consume": bool}
    schedules: Optional[List[dict]] = None  # [{name, cron, message, ...}]


class SystemPermissions(BaseModel):
    """Permission configuration for system agents."""
    preset: Optional[str] = None  # "full-mesh", "orchestrator-workers", "none"
    explicit: Optional[Dict[str, List[str]]] = None  # {"orchestrator": ["worker1", "worker2"]}


class SystemManifest(BaseModel):
    """Parsed system manifest from YAML."""
    name: str
    description: Optional[str] = None
    prompt: Optional[str] = None
    agents: Dict[str, SystemAgentConfig]
    permissions: Optional[SystemPermissions] = None


class SystemDeployRequest(BaseModel):
    """Request to deploy a system from YAML manifest."""
    manifest: str  # Raw YAML string
    dry_run: bool = False


class SystemDeployResponse(BaseModel):
    """Response from system deployment."""
    status: str  # "deployed" or "valid" (for dry_run)
    system_name: str
    agents_created: List[str]  # Final agent names created
    agents_to_create: Optional[List[dict]] = None  # For dry_run: [{name, template}]
    prompt_updated: bool
    permissions_configured: int = 0
    schedules_created: int = 0
    warnings: List[str] = []
```

---

### 2. `src/backend/services/system_service.py` - Business Logic

Create new file:

```python
"""
System manifest parsing and deployment service.

Handles YAML parsing, validation, agent naming, and deployment orchestration.
"""
import yaml
import re
from typing import Dict, List, Tuple, Optional
from models import SystemManifest, SystemAgentConfig, SystemPermissions
from database import db


def parse_manifest(yaml_str: str) -> SystemManifest:
    """
    Parse YAML string into SystemManifest.

    Raises:
        ValueError: If YAML is invalid or missing required fields
    """
    try:
        data = yaml.safe_load(yaml_str)
    except yaml.YAMLError as e:
        raise ValueError(f"YAML parse error: {str(e)}")

    if not data:
        raise ValueError("Empty manifest")

    if not isinstance(data, dict):
        raise ValueError("Manifest must be a YAML object")

    # Validate required fields
    if "name" not in data:
        raise ValueError("Missing required field: name")
    if "agents" not in data or not data["agents"]:
        raise ValueError("Missing required field: agents (must have at least 1)")

    # Parse agents
    agents = {}
    for agent_name, agent_config in data["agents"].items():
        if not isinstance(agent_config, dict):
            raise ValueError(f"Agent '{agent_name}' config must be an object")
        if "template" not in agent_config:
            raise ValueError(f"Agent '{agent_name}' missing required field: template")

        agents[agent_name] = SystemAgentConfig(
            template=agent_config["template"],
            resources=agent_config.get("resources"),
            folders=agent_config.get("folders"),
            schedules=agent_config.get("schedules")
        )

    # Parse permissions
    permissions = None
    if "permissions" in data:
        perm_data = data["permissions"]
        permissions = SystemPermissions(
            preset=perm_data.get("preset"),
            explicit=perm_data.get("explicit")
        )

    return SystemManifest(
        name=data["name"],
        description=data.get("description"),
        prompt=data.get("prompt"),
        agents=agents,
        permissions=permissions
    )


def validate_manifest(manifest: SystemManifest) -> List[str]:
    """
    Validate manifest and return warnings.

    Returns:
        List of warning messages (empty if all valid)

    Raises:
        ValueError: If validation fails
    """
    warnings = []

    # Validate system name
    if not re.match(r'^[a-z0-9][a-z0-9-]{1,48}[a-z0-9]$', manifest.name):
        raise ValueError(
            f"Invalid system name '{manifest.name}': must be 3-50 chars, "
            "lowercase alphanumeric and hyphens, start/end with alphanumeric"
        )

    # Validate agent names
    for agent_name in manifest.agents.keys():
        if not re.match(r'^[a-z0-9][a-z0-9-]*[a-z0-9]$|^[a-z0-9]$', agent_name):
            raise ValueError(
                f"Invalid agent name '{agent_name}': must be lowercase alphanumeric and hyphens"
            )

    # Validate template references
    for agent_name, config in manifest.agents.items():
        template = config.template
        if not (template.startswith("github:") or template.startswith("local:")):
            raise ValueError(
                f"Agent '{agent_name}': template must start with 'github:' or 'local:'"
            )

    # Validate permissions
    if manifest.permissions:
        if manifest.permissions.preset and manifest.permissions.explicit:
            raise ValueError("Cannot specify both preset and explicit permissions")

        if manifest.permissions.preset:
            valid_presets = ["full-mesh", "orchestrator-workers", "none"]
            if manifest.permissions.preset not in valid_presets:
                raise ValueError(
                    f"Invalid permission preset '{manifest.permissions.preset}': "
                    f"must be one of {valid_presets}"
                )

            # Warn if orchestrator-workers but no orchestrator agent
            if manifest.permissions.preset == "orchestrator-workers":
                if "orchestrator" not in manifest.agents:
                    warnings.append(
                        "Permission preset 'orchestrator-workers' specified but no "
                        "'orchestrator' agent defined. No permissions will be granted."
                    )

        if manifest.permissions.explicit:
            agent_names = set(manifest.agents.keys())
            for source, targets in manifest.permissions.explicit.items():
                if source not in agent_names:
                    raise ValueError(f"Unknown agent in permissions: {source}")
                for target in targets:
                    if target not in agent_names:
                        raise ValueError(f"Unknown agent in permissions: {target}")

    # Validate schedules (if any)
    for agent_name, config in manifest.agents.items():
        if config.schedules:
            for i, schedule in enumerate(config.schedules):
                if "name" not in schedule:
                    raise ValueError(f"Agent '{agent_name}' schedule {i}: missing 'name'")
                if "cron" not in schedule:
                    raise ValueError(f"Agent '{agent_name}' schedule {i}: missing 'cron'")
                if "message" not in schedule:
                    raise ValueError(f"Agent '{agent_name}' schedule {i}: missing 'message'")

    return warnings


def agent_exists(name: str) -> bool:
    """Check if an agent with this name already exists."""
    # Check database for ownership record
    owner = db.get_agent_owner(name)
    return owner is not None


def get_next_agent_name(base_name: str) -> str:
    """
    Find next available name with _N suffix if base name exists.

    Examples:
        "my-agent" -> "my-agent" (if doesn't exist)
        "my-agent" -> "my-agent_2" (if my-agent exists)
        "my-agent" -> "my-agent_3" (if my-agent and my-agent_2 exist)
    """
    if not agent_exists(base_name):
        return base_name

    n = 2
    while agent_exists(f"{base_name}_{n}"):
        n += 1
    return f"{base_name}_{n}"


def resolve_agent_names(
    system_name: str,
    agents: Dict[str, SystemAgentConfig]
) -> Tuple[Dict[str, str], List[str]]:
    """
    Resolve short agent names to final names, handling conflicts.

    Args:
        system_name: The system name prefix
        agents: Dict of short_name -> agent config

    Returns:
        Tuple of (name_mapping, warnings)
        - name_mapping: {short_name: final_name}
        - warnings: List of conflict warnings
    """
    name_mapping = {}
    warnings = []

    for short_name in agents.keys():
        base_name = f"{system_name}-{short_name}"
        final_name = get_next_agent_name(base_name)
        name_mapping[short_name] = final_name

        if final_name != base_name:
            warnings.append(
                f"Agent '{base_name}' already exists, will create '{final_name}'"
            )

    return name_mapping, warnings
```

---

### 3. `src/backend/routers/systems.py` - API Endpoint

Create new file:

```python
"""
System deployment routes for the Trinity backend.

Provides endpoints for deploying multi-agent systems from YAML manifests.
"""
from typing import List
from fastapi import APIRouter, Depends, HTTPException, Request
import logging

from models import User, SystemDeployRequest, SystemDeployResponse, AgentConfig
from database import db
from dependencies import get_current_user
from services.audit_service import log_audit_event
from services.system_service import (
    parse_manifest,
    validate_manifest,
    resolve_agent_names
)

# Import for agent creation (reuse existing logic)
from routers.agents import create_agent_internal

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/systems", tags=["systems"])


@router.post("/deploy", response_model=SystemDeployResponse)
async def deploy_system(
    body: SystemDeployRequest,
    request: Request,
    current_user: User = Depends(get_current_user)
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
                # Note: This creates but does NOT start the agent yet
                await create_agent_internal(
                    config=agent_config,
                    current_user=current_user,
                    request=request,
                    start_after_create=False  # We'll start all at once in Phase 2
                )

                created_agents.append(final_name)
                logger.info(f"Created agent '{final_name}' for system '{manifest.name}'")

            except Exception as e:
                # Log partial failure
                logger.error(f"Failed to create agent '{final_name}': {e}")
                await log_audit_event(
                    event_type="system_deployment",
                    action="partial_failure",
                    user_id=current_user.username,
                    ip_address=request.client.host if request.client else None,
                    result="failed",
                    severity="error",
                    details={
                        "system_name": manifest.name,
                        "failed_agent": final_name,
                        "created_agents": created_agents,
                        "error": str(e)
                    }
                )
                raise HTTPException(
                    status_code=500,
                    detail={
                        "error": "Deployment failed",
                        "failed_at": final_name,
                        "created": created_agents,
                        "reason": str(e)
                    }
                )

        # 7. Audit log success
        await log_audit_event(
            event_type="system_deployment",
            action="deploy",
            user_id=current_user.username,
            ip_address=request.client.host if request.client else None,
            result="success",
            details={
                "system_name": manifest.name,
                "agents_created": created_agents,
                "prompt_updated": prompt_updated
            }
        )

        return SystemDeployResponse(
            status="deployed",
            system_name=manifest.name,
            agents_created=created_agents,
            prompt_updated=prompt_updated,
            permissions_configured=0,  # Phase 2
            schedules_created=0,  # Phase 2
            warnings=all_warnings
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"System deployment failed: {e}")
        raise HTTPException(status_code=500, detail=f"Deployment failed: {str(e)}")
```

---

## Files to Modify

### 1. `src/backend/main.py` - Register Router

Add import and registration:

```python
# Near other router imports
from routers.systems import router as systems_router

# Near other router registrations (around line 205)
app.include_router(systems_router)
```

---

### 2. `src/backend/routers/agents.py` - Extract Internal Function

We need to extract `create_agent_internal()` from the existing endpoint so we can reuse it.

Find the `create_agent_endpoint` function and refactor to:

```python
async def create_agent_internal(
    config: AgentConfig,
    current_user: User,
    request: Request,
    start_after_create: bool = True
) -> AgentStatus:
    """
    Internal function to create an agent.

    Used by both the API endpoint and system deployment.
    """
    # ... existing creation logic ...
    # At the end, conditionally start based on start_after_create


@router.post("")
async def create_agent_endpoint(
    config: AgentConfig,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Create a new agent container."""
    return await create_agent_internal(config, current_user, request, start_after_create=True)
```

---

## Testing Phase 1

### Test 1: Dry Run Validation

```bash
# Valid manifest
curl -X POST http://localhost:8000/api/systems/deploy \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "manifest": "name: test-system\nagents:\n  worker:\n    template: local:business-assistant",
    "dry_run": true
  }'

# Expected response:
{
  "status": "valid",
  "system_name": "test-system",
  "agents_created": [],
  "agents_to_create": [{"name": "test-system-worker", "short_name": "worker", "template": "local:business-assistant"}],
  "prompt_updated": false,
  "warnings": []
}
```

### Test 2: Invalid YAML

```bash
curl -X POST http://localhost:8000/api/systems/deploy \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"manifest": "invalid: yaml: content:", "dry_run": true}'

# Expected: 400 with YAML parse error
```

### Test 3: Missing Required Fields

```bash
curl -X POST http://localhost:8000/api/systems/deploy \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"manifest": "name: test", "dry_run": true}'

# Expected: 400 "Missing required field: agents"
```

### Test 4: Actual Deployment

```bash
curl -X POST http://localhost:8000/api/systems/deploy \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "manifest": "name: test-system\nagents:\n  worker:\n    template: local:business-assistant"
  }'

# Expected: Agent "test-system-worker" created
# Verify: GET /api/agents shows the new agent
```

### Test 5: Conflict Resolution

```bash
# Deploy again with same name
curl -X POST http://localhost:8000/api/systems/deploy \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "manifest": "name: test-system\nagents:\n  worker:\n    template: local:business-assistant"
  }'

# Expected: Creates "test-system-worker_2"
# warnings: ["Agent 'test-system-worker' already exists, will create 'test-system-worker_2'"]
```

### Test 6: Trinity Prompt Update

```bash
curl -X POST http://localhost:8000/api/systems/deploy \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "manifest": "name: prompt-test\nprompt: |\n  You are part of a test system.\nagents:\n  bot:\n    template: local:business-assistant"
  }'

# Verify: GET /api/settings/trinity_prompt returns the new prompt
```

---

## Completion Checklist

- [ ] Models added to `src/backend/models.py`
- [ ] `src/backend/services/system_service.py` created
- [ ] `src/backend/routers/systems.py` created
- [ ] Router registered in `src/backend/main.py`
- [ ] `create_agent_internal()` extracted in `agents.py`
- [ ] All 6 tests passing
- [ ] Update `docs/memory/changelog.md`
- [ ] Update `docs/memory/requirements.md` (10.7 status to 🚧)
