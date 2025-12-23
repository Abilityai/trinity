# System YAML Specification (DRAFT)

> **Status**: Draft proposal for discussion
> **Created**: 2025-12-15
> **Priority**: TBD (Phase 11 or 12 candidate)

---

## Overview

A `system.yaml` file provides declarative specification for multi-agent systems, enabling one-command deployment of entire agent constellations with pre-configured permissions, schedules, and shared folders.

**Analogy**: `system.yaml` is to multi-agent systems what `template.yaml` is to individual agents.

## Location

The `system.yaml` file lives in the **orchestrator agent's repository**. This makes the orchestrator the "entry point" for the entire system.

```
github.com/YourOrg/ruby-orchestrator/
├── template.yaml          # Agent-level config
├── system.yaml            # System-level config (NEW)
├── CLAUDE.md
├── .mcp.json.template
└── ...
```

## Schema

```yaml
# system.yaml - Multi-Agent System Definition
apiVersion: trinity/v1
kind: AgentSystem

metadata:
  name: ruby-content-system           # Unique system identifier
  description: Automated content management with orchestration
  version: 1.0.0
  author: Your Team

# Agent definitions
agents:
  - name: ruby-orchestrator
    template: self                    # "self" = this repo (the orchestrator)
    role: orchestrator                # orchestrator | worker | pipeline-stage
    resources:                        # Optional resource overrides
      cpu: "2"
      memory: "4g"
    folders:
      expose: true                    # Create shared-out volume
      consume: false                  # Don't mount others' folders
    schedules:
      - name: Hourly Coordination
        cron: "0 * * * *"
        message: "Run hourly coordination cycle"
        timezone: America/Los_Angeles
        enabled: true
      - name: Health Check
        cron: "*/10 * * * *"
        message: "Quick health check on all agents"
        enabled: true

  - name: ruby-content
    template: github:YourOrg/ruby-content
    role: worker
    folders:
      expose: true
      consume: true                   # Will mount orchestrator's folder
    schedules:
      - name: Content Scan
        cron: "5,20,35,50 * * * *"
        message: "Scan for new content"
      - name: Publishing Check
        cron: "*/15 * * * *"
        message: "Check for posts due to publish"

  - name: ruby-engagement
    template: github:YourOrg/ruby-engagement
    role: worker
    folders:
      expose: true
      consume: true
    schedules:
      - name: Engagement Monitor
        cron: "*/5 * * * *"
        message: "Monitor engagement metrics"

# Permission topology
permissions:
  # Predefined topologies
  topology: mesh                      # mesh | hub-spoke | pipeline | custom

  # OR explicit custom permissions (used when topology: custom)
  # custom:
  #   - source: ruby-orchestrator
  #     targets: [ruby-content, ruby-engagement]
  #   - source: ruby-content
  #     targets: [ruby-orchestrator, ruby-engagement]
  #   - source: ruby-engagement
  #     targets: [ruby-orchestrator, ruby-content]

# Deployment configuration
deployment:
  order:                              # Creation order (for dependencies)
    - ruby-orchestrator               # Created first
    - ruby-content
    - ruby-engagement
  startup_delay: 10                   # Seconds between agent starts
  wait_for_healthy: true              # Wait for each agent to be running before next
```

## Credential Handling

**Credentials are NOT specified in system.yaml.**

Each agent's credential requirements are defined in their individual `template.yaml` files. The system deployment process:

1. Reads each agent's `template.yaml` to discover credential requirements
2. Validates that required credentials exist in Redis for the deploying user
3. Fails deployment if any required credentials are missing
4. Proceeds with agent creation (credentials injected per-agent as normal)

This keeps credential management at the agent level and avoids duplication.

## Permission Topologies

| Topology | Description | Generated Permissions |
|----------|-------------|----------------------|
| `mesh` | All agents can talk to all others | N×(N-1) bidirectional permissions |
| `hub-spoke` | Only orchestrator can initiate | Orchestrator → all workers (one-way) |
| `pipeline` | Sequential based on deployment order | Agent[n] → Agent[n+1] only |
| `custom` | Explicit list | As specified in `custom` section |

## API Endpoints (Proposed)

### Deploy System
```
POST /api/systems
Content-Type: application/json

{
  "template": "github:YourOrg/ruby-orchestrator",  // Repo containing system.yaml
  "name_prefix": "prod"                            // Optional: prefix all agent names
}
```

**Response:**
```json
{
  "system_name": "ruby-content-system",
  "agents_created": ["ruby-orchestrator", "ruby-content", "ruby-engagement"],
  "schedules_created": 5,
  "permissions_granted": 6
}
```

### Get System Status
```
GET /api/systems/{system_name}
```

**Response:**
```json
{
  "name": "ruby-content-system",
  "status": "healthy",  // healthy | degraded | unhealthy
  "agents": [
    {"name": "ruby-orchestrator", "status": "running", "healthy": true},
    {"name": "ruby-content", "status": "running", "healthy": true},
    {"name": "ruby-engagement", "status": "stopped", "healthy": false}
  ],
  "created_at": "2025-12-15T...",
  "deployed_by": "user@example.com"
}
```

### List Systems
```
GET /api/systems
```

### Delete System
```
DELETE /api/systems/{system_name}
```

Deletes all agents in the system.

## Database Schema (Proposed)

```sql
-- Track deployed systems
CREATE TABLE agent_systems (
    name TEXT PRIMARY KEY,
    description TEXT,
    version TEXT,
    source_template TEXT,           -- github:Org/repo
    deployed_by TEXT NOT NULL,
    deployed_at TEXT NOT NULL,
    config TEXT,                    -- Full system.yaml as JSON
    FOREIGN KEY (deployed_by) REFERENCES users(id)
);

-- Link agents to systems
CREATE TABLE agent_system_membership (
    agent_name TEXT NOT NULL,
    system_name TEXT NOT NULL,
    role TEXT,                      -- orchestrator | worker | pipeline-stage
    PRIMARY KEY (agent_name, system_name),
    FOREIGN KEY (agent_name) REFERENCES agent_ownership(agent_name),
    FOREIGN KEY (system_name) REFERENCES agent_systems(name)
);
```

## Open Questions

### 1. Update Semantics
What happens when `system.yaml` changes and is redeployed?

**Options:**
- **A) Additive only**: New agents added, existing unchanged, removals ignored
- **B) Full sync**: Add new, update existing, delete removed (dangerous)
- **C) Explicit action**: `POST /api/systems/{name}/sync` with dry-run option
- **D) Immutable**: Delete and recreate entire system

**Current thinking**: Option C with dry-run seems safest. User can preview changes before applying.

### 2. Partial Failures
What if agent 2 of 3 fails to create?

**Options:**
- **A) Rollback**: Delete agent 1, fail entire deployment
- **B) Continue**: Create what we can, report failures
- **C) Pause**: Stop deployment, let user decide

**Current thinking**: Option B with clear status reporting.

### 3. Name Prefixing
Should system deployment support name prefixes for environment isolation?

```
POST /api/systems
{"template": "...", "name_prefix": "staging"}

# Creates: staging-ruby-orchestrator, staging-ruby-content, etc.
```

**Current thinking**: Yes, useful for staging/prod separation.

### 4. System-Level Dashboard
Should there be a system-specific view in the Collaboration Dashboard?

- Filter to show only agents in a system
- System health indicator
- System-level metrics aggregation

### 5. Versioning and Rollback
Should we track system deployment versions for rollback?

```
POST /api/systems/{name}/rollback?version=1.0.0
```

## Implementation Phases

### Phase 1: Basic Deployment
- Parse system.yaml from orchestrator repo
- Create agents in order
- Apply permissions based on topology
- Create schedules

### Phase 2: System Management
- System status API
- System deletion
- System membership tracking in database

### Phase 3: Updates and Sync
- Detect changes in system.yaml
- Dry-run sync preview
- Apply changes safely

### Phase 4: Dashboard Integration
- System-level view in Collaboration Dashboard
- System health indicators
- Aggregated metrics

## Example: Ruby Content System

```yaml
apiVersion: trinity/v1
kind: AgentSystem

metadata:
  name: ruby-content-system
  description: |
    Automated Ruby community content management system.
    Discovers content, publishes to social platforms, monitors engagement.
  version: 1.0.0

agents:
  - name: ruby-orchestrator
    template: self
    role: orchestrator
    folders:
      expose: true
      consume: false
    schedules:
      - name: Hourly Coordination
        cron: "0 * * * *"
        message: |
          Run hourly coordination:
          1. Check all agent health via shared folders
          2. Review schedule for next hour
          3. Resolve any conflicts
          4. Update weekly_plan.json if needed
        timezone: America/Los_Angeles

  - name: ruby-content
    template: github:YourOrg/ruby-content
    role: worker
    folders:
      expose: true
      consume: true
    schedules:
      - name: Content Scan
        cron: "5,35 * * * *"
        message: "Scan Ruby Weekly, dev.to, and GitHub trending for new content"
      - name: Publishing Check
        cron: "15,45 * * * *"
        message: "Check schedule.json and publish any due posts"

  - name: ruby-engagement
    template: github:YourOrg/ruby-engagement
    role: worker
    folders:
      expose: true
      consume: true
    schedules:
      - name: Engagement Scan
        cron: "*/10 * * * *"
        message: "Monitor mentions, replies, and engagement metrics"

permissions:
  topology: mesh

deployment:
  order: [ruby-orchestrator, ruby-content, ruby-engagement]
  startup_delay: 15
  wait_for_healthy: true
```

## References

- [TRINITY_COMPATIBLE_AGENT_GUIDE.md](../TRINITY_COMPATIBLE_AGENT_GUIDE.md) - Individual agent template spec
- [MULTI_AGENT_SYSTEM_GUIDE.md](../MULTI_AGENT_SYSTEM_GUIDE.md) - Multi-agent design patterns
- [template.yaml schema](../TRINITY_COMPATIBLE_AGENT_GUIDE.md#templateyaml-schema) - Agent-level configuration

---

*This is a draft specification. Implementation details may change based on feedback and technical constraints.*
