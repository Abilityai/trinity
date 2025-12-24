# System Manifest - Recipe-Based Deployment

> **Status**: Design Finalized
> **Approach**: Recipe (one-shot deployment, not declarative)
> **Principle**: YAML is a script to create agents, not a system of record

---

## Core Concept

A **system manifest** is a recipe that creates and configures multiple agents in one operation. It is NOT binding after deployment - agents become independent entities that can be modified via UI/API.

### Key Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Agent naming | `{system}-{agent}` | Prevents conflicts, groups agents visually |
| System prompt | Reuse global `trinity_prompt` | Simpler, one place to manage |
| Re-deploy behavior | Create new agents with `_N` suffix | `ruby` → `ruby_2` → `ruby_3` |
| Storage | No `systems` table | Agents are just agents with naming convention |

---

## system.yaml Format

```yaml
name: content-production
description: Autonomous content creation pipeline

# Updates global trinity_prompt setting (overwrites existing)
# All agents receive this on start/restart
prompt: |
  You are part of the Content Production system.
  Follow these guidelines:
  - Always save outputs to shared-out folder
  - Use structured JSON for inter-agent communication
  - Log progress to workspace/logs/

# Agents to create and configure
agents:
  orchestrator:
    template: github:YourOrg/content-orchestrator
    # OR template: local:business-assistant

    resources:
      cpu: "2"
      memory: "4g"

    # Folder sharing (maps to PUT /api/agents/{name}/folders)
    folders:
      expose: true    # Creates /home/developer/shared-out volume
      consume: true   # Mounts permitted agents' folders at /home/developer/shared-in/

    # Schedules (maps to POST /api/agents/{name}/schedules)
    schedules:
      - name: daily-planning
        cron: "0 9 * * *"
        message: "Plan today's content production tasks"
        timezone: America/New_York
        enabled: true

  ruby:
    template: github:YourOrg/ruby-content

    folders:
      expose: true
      consume: true

    schedules:
      - name: check-jobs
        cron: "*/30 * * * *"
        message: "Check shared-in/ for new work from orchestrator"

  cornelius:
    template: github:YourOrg/cornelius-research

    folders:
      expose: true
      consume: true

# Permission configuration
permissions:
  # Preset: "full-mesh" | "orchestrator-workers" | "none"
  preset: full-mesh

  # OR explicit list per agent (only if preset not specified)
  # explicit:
  #   orchestrator: [ruby, cornelius]
  #   ruby: [cornelius]
  #   cornelius: []
```

---

## Naming Convention

### Standard Format
```
{system-name}-{agent-name}
```

Examples:
- `content-production-orchestrator`
- `content-production-ruby`
- `content-production-cornelius`

### Re-deployment (Incremental Suffix)

When deploying a system where agents already exist:

```
First deploy:   content-production-ruby
Second deploy:  content-production-ruby_2
Third deploy:   content-production-ruby_3
```

Detection logic:
```python
def get_next_agent_name(base_name: str) -> str:
    """Find next available name with _N suffix."""
    if not agent_exists(base_name):
        return base_name

    n = 2
    while agent_exists(f"{base_name}_{n}"):
        n += 1
    return f"{base_name}_{n}"
```

---

## API Design

### POST /api/systems/deploy

Deploy a system from YAML manifest.

**Request:**
```json
{
  "manifest": "name: content-production\n...",  // YAML string
  "dry_run": false  // If true, validate only
}
```

**Response:**
```json
{
  "status": "deployed",
  "system_name": "content-production",
  "agents_created": [
    "content-production-orchestrator",
    "content-production-ruby",
    "content-production-cornelius"
  ],
  "prompt_updated": true,
  "permissions_configured": 6,
  "schedules_created": 2,
  "warnings": []
}
```

**Dry Run Response:**
```json
{
  "status": "valid",
  "system_name": "content-production",
  "agents_to_create": [
    {"name": "content-production-orchestrator", "template": "github:YourOrg/content-orchestrator"},
    {"name": "content-production-ruby_2", "template": "github:YourOrg/ruby-content"}  // _2 because ruby exists
  ],
  "prompt_will_update": true,
  "current_prompt_length": 150,
  "new_prompt_length": 450,
  "warnings": ["Agent 'content-production-ruby' exists, will create 'content-production-ruby_2'"]
}
```

---

## Execution Flow

```python
async def deploy_system(manifest_yaml: str, user: User, dry_run: bool = False):
    """Deploy a system from YAML manifest."""

    # 1. Parse and validate YAML
    system = parse_and_validate(manifest_yaml)

    # 2. Resolve agent names (handle conflicts)
    agent_names = {}
    warnings = []
    for short_name in system.agents.keys():
        base_name = f"{system.name}-{short_name}"
        final_name = get_next_agent_name(base_name)
        agent_names[short_name] = final_name
        if final_name != base_name:
            warnings.append(f"Agent '{base_name}' exists, will create '{final_name}'")

    if dry_run:
        return {"status": "valid", "agents_to_create": agent_names, "warnings": warnings}

    # 3. Update global trinity_prompt
    if system.prompt:
        db.set_setting("trinity_prompt", system.prompt)

    # 4. Create all agents
    created_agents = []
    for short_name, config in system.agents.items():
        agent_name = agent_names[short_name]
        await create_agent(
            name=agent_name,
            template=config.template,
            resources=config.resources,
            owner=user
        )
        created_agents.append(agent_name)

    # 5. Configure permissions (after all agents exist)
    permissions_count = 0
    if system.permissions.preset == "full-mesh":
        for a in created_agents:
            targets = [b for b in created_agents if b != a]
            db.set_agent_permissions(a, targets, user.username)
            permissions_count += len(targets)

    elif system.permissions.preset == "orchestrator-workers":
        orchestrator = agent_names.get("orchestrator")
        if orchestrator:
            workers = [a for a in created_agents if a != orchestrator]
            db.set_agent_permissions(orchestrator, workers, user.username)
            permissions_count = len(workers)

    elif system.permissions.explicit:
        for source, targets in system.permissions.explicit.items():
            source_name = agent_names[source]
            target_names = [agent_names[t] for t in targets]
            db.set_agent_permissions(source_name, target_names, user.username)
            permissions_count += len(target_names)

    # 6. Configure folders
    for short_name, config in system.agents.items():
        if config.folders:
            agent_name = agent_names[short_name]
            db.upsert_shared_folder_config(
                agent_name,
                expose_enabled=config.folders.get("expose", False),
                consume_enabled=config.folders.get("consume", False)
            )

    # 7. Create schedules
    schedules_count = 0
    for short_name, config in system.agents.items():
        agent_name = agent_names[short_name]
        for schedule in config.schedules or []:
            db.create_schedule(
                agent_name=agent_name,
                name=schedule["name"],
                cron_expression=schedule["cron"],
                message=schedule["message"],
                timezone=schedule.get("timezone", "UTC"),
                enabled=schedule.get("enabled", True),
                owner_id=user.id
            )
            schedules_count += 1

    # 8. Start all agents (triggers Trinity injection with updated prompt)
    for agent_name in created_agents:
        await start_agent(agent_name)

    return {
        "status": "deployed",
        "system_name": system.name,
        "agents_created": created_agents,
        "prompt_updated": bool(system.prompt),
        "permissions_configured": permissions_count,
        "schedules_created": schedules_count,
        "warnings": warnings
    }
```

---

## Permission Presets

### `full-mesh`
Every agent can communicate with every other agent.

```
orchestrator ↔ ruby ↔ cornelius
     ↑_______________↑
```

### `orchestrator-workers`
Only the agent named `orchestrator` can initiate communication.

```
orchestrator → ruby
orchestrator → cornelius
(workers cannot call orchestrator or each other)
```

### `none`
No permissions granted. Configure manually after deployment.

### `explicit`
Specify exact permissions per agent:

```yaml
permissions:
  explicit:
    orchestrator: [ruby, cornelius]
    ruby: [cornelius]
    cornelius: []
```

---

## Maps to Existing APIs

| YAML Field | API Endpoint | Method |
|------------|--------------|--------|
| `prompt` | `/api/settings/trinity_prompt` | PUT |
| `agents.{name}` | `/api/agents` | POST |
| `agents.{name}.folders` | `/api/agents/{name}/folders` | PUT |
| `agents.{name}.schedules[]` | `/api/agents/{name}/schedules` | POST |
| `permissions` | `/api/agents/{name}/permissions` | PUT |

---

## Validation Rules

| Field | Rule |
|-------|------|
| `name` | Required, alphanumeric + hyphens, 3-50 chars |
| `agents` | Required, at least 1 agent |
| `agents.*.template` | Required, valid template reference |
| `agents.*.resources.cpu` | Optional, string like "1", "2", "0.5" |
| `agents.*.resources.memory` | Optional, string like "1g", "512m" |
| `agents.*.schedules[].cron` | Valid 5-field cron expression |
| `agents.*.schedules[].message` | Required, non-empty |
| `permissions.preset` | One of: full-mesh, orchestrator-workers, none |
| `permissions.explicit` | Map of agent names to target lists |

---

## Error Handling

| Error | HTTP Status | Response |
|-------|-------------|----------|
| Invalid YAML syntax | 400 | `{"error": "YAML parse error", "details": "..."}` |
| Unknown template | 400 | `{"error": "Template not found", "agent": "ruby", "template": "..."}` |
| Invalid cron | 400 | `{"error": "Invalid cron expression", "schedule": "...", "cron": "..."}` |
| Permission target not in system | 400 | `{"error": "Unknown agent in permissions", "agent": "unknown"}` |
| Partial failure | 500 | `{"error": "Deployment failed", "created": [...], "failed_at": "ruby", "reason": "..."}` |

On partial failure, created agents are NOT rolled back (user can clean up manually or retry).

---

## Future Extensions

These fields are reserved for future use:

```yaml
# Not implemented yet
governance:
  repo: github:YourOrg/governance
  mount_path: /home/developer/governance

# NOTE: MCP servers must be included in agent templates, not system manifests
# Chroma example removed - templates should define their own vector memory if needed

hooks:
  on_deploy: "./scripts/post-deploy.sh"
  on_destroy: "./scripts/cleanup.sh"
```

---

## Implementation Phases

### Phase 1: Core Deployment
- [ ] YAML parsing and validation (pydantic models)
- [ ] `POST /api/systems/deploy` endpoint
- [ ] Agent creation with `{system}-{agent}` naming
- [ ] Incremental suffix for conflicts (`_2`, `_3`)
- [ ] Global trinity_prompt update
- [ ] Dry run mode

### Phase 2: Configuration
- [ ] Permission preset application (full-mesh, orchestrator-workers)
- [ ] Explicit permission matrix
- [ ] Shared folder configuration
- [ ] Schedule creation

### Phase 3: Polish
- [ ] Better error messages
- [ ] Partial rollback option
- [ ] UI for system deployment (upload YAML)
- [ ] System list view (agents grouped by prefix)

---

## References

- Existing features used: scheduling.md, agent-permissions.md, agent-shared-folders.md, system-wide-trinity-prompt.md
- Agent creation: agent-lifecycle.md
- Multi-agent patterns: MULTI_AGENT_SYSTEM_GUIDE.md
