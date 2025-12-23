# System Manifest - Phase 3: MCP Tools & Minimal UI

> **Goal**: MCP-first deployment workflow with minimal UI additions
> **Prerequisite**: Phase 1 & 2 complete (agent creation, configuration, startup)
> **Focus**: Enable Claude Code to deploy systems via MCP, UI just for visibility

---

## Overview

Phase 3 adds MCP tools for system deployment and a minimal UI to:
1. Upload and deploy `system.yaml` via MCP
2. View deployed systems (grouped by prefix)
3. Reload/restart system agents

**Design Philosophy**: MCP is the primary interface, UI is read-only + basic actions.

---

## MCP Tools (Priority)

### 1. `deploy_system` - Deploy Multi-Agent System

**Purpose**: Deploy a system from YAML manifest via MCP

```python
@mcp_server.tool()
async def deploy_system(
    manifest: str,
    dry_run: bool = False,
    context: McpAuthContext
) -> dict:
    """
    Deploy a multi-agent system from YAML manifest.

    Args:
        manifest: YAML content as string
        dry_run: If True, validate without deploying
        context: MCP authentication context

    Returns:
        {
            "status": "deployed" | "dry_run_passed",
            "system_name": "content-production",
            "agents_created": ["agent-1", "agent-2"],
            "prompt_updated": true,
            "permissions_configured": 6,
            "schedules_created": 2,
            "warnings": []
        }
    """
    # Call backend /api/systems/deploy with user context
    # Return deployment result
```

**Usage from Claude Code**:
```bash
# Read manifest file
cat system.yaml | mcp__trinity__deploy_system

# Or direct string
mcp__trinity__deploy_system manifest="name: my-system\nagents:\n  worker:\n    template: local:business-assistant"
```

### 2. `list_systems` - List Deployed Systems

**Purpose**: Show all systems (agents grouped by prefix)

```python
@mcp_server.tool()
async def list_systems(context: McpAuthContext) -> dict:
    """
    List all deployed systems with their agents.

    Returns:
        {
            "systems": [
                {
                    "name": "content-production",
                    "agent_count": 3,
                    "agents": [
                        {
                            "name": "content-production-orchestrator",
                            "status": "running",
                            "template": "github:Org/repo"
                        },
                        {
                            "name": "content-production-writer",
                            "status": "running",
                            "template": "local:business-assistant"
                        }
                    ],
                    "created_at": "2025-12-18T10:00:00Z"
                }
            ]
        }
    """
    # Get all agents user can access
    # Group by system prefix (before first '-')
    # Return system summaries
```

**Usage**:
```bash
mcp__trinity__list_systems
# Shows all systems with their agents
```

### 3. `restart_system` - Restart All System Agents

**Purpose**: Restart all agents in a system (useful after config changes)

```python
@mcp_server.tool()
async def restart_system(
    system_name: str,
    context: McpAuthContext
) -> dict:
    """
    Restart all agents belonging to a system.

    Args:
        system_name: System prefix (e.g., "content-production")

    Returns:
        {
            "restarted": ["agent-1", "agent-2"],
            "failed": []
        }
    """
    # Find all agents with prefix {system_name}-
    # Stop and start each agent
    # Return results
```

**Usage**:
```bash
mcp__trinity__restart_system system_name="content-production"
```

### 4. `get_system_manifest` - Export Current System Config

**Purpose**: Generate YAML manifest from deployed system (for backup/documentation)

```python
@mcp_server.tool()
async def get_system_manifest(
    system_name: str,
    context: McpAuthContext
) -> str:
    """
    Generate YAML manifest for a deployed system.

    Args:
        system_name: System prefix

    Returns:
        YAML string representing current system configuration
    """
    # Query agents, permissions, folders, schedules
    # Reconstruct YAML manifest
    # Return as string
```

**Usage**:
```bash
mcp__trinity__get_system_manifest system_name="content-production" > backup.yaml
```

---

## Backend API (Supporting MCP)

### New Endpoints

#### `GET /api/systems`
List all systems (agents grouped by prefix)

**Response**:
```json
{
  "systems": [
    {
      "name": "content-production",
      "agent_count": 3,
      "agents": ["content-production-orchestrator", "content-production-writer"],
      "created_at": "2025-12-18T10:00:00Z"
    }
  ]
}
```

**Implementation**:
```python
@router.get("/systems")
async def list_systems(current_user: User = Depends(get_current_user)):
    """List all systems (agents grouped by prefix)."""
    # Get all agents user can access
    agents = list_agents_for_user(current_user.username)

    # Group by system prefix
    systems = {}
    for agent in agents:
        # Extract prefix (before first '-')
        if '-' in agent['name']:
            prefix = agent['name'].split('-')[0]
            if prefix not in systems:
                systems[prefix] = {
                    "name": prefix,
                    "agents": [],
                    "created_at": None
                }
            systems[prefix]["agents"].append(agent)

    # Sort by created_at
    # Return list
```

#### `GET /api/systems/{name}`
Get system details with all agents

**Response**:
```json
{
  "name": "content-production",
  "agents": [
    {
      "name": "content-production-orchestrator",
      "status": "running",
      "template": "github:Org/repo",
      "permissions": ["content-production-writer", "content-production-reviewer"],
      "folders": {"expose": true, "consume": true},
      "schedules": [...]
    }
  ]
}
```

#### `POST /api/systems/{name}/restart`
Restart all agents in system

**Request**: Empty body
**Response**:
```json
{
  "restarted": ["agent-1", "agent-2"],
  "failed": []
}
```

#### `GET /api/systems/{name}/manifest`
Export system as YAML manifest

**Response**: YAML string (Content-Type: text/yaml)

---

## Minimal UI Changes

### 1. Systems View (Optional - Can Skip)

**Location**: `/systems` route (new page)

**UI Components**:
- System cards grouped by prefix
- Show agent count, status summary
- "Restart System" button
- "View Agents" button (navigates to filtered Agents page)

**OR**: Just extend existing Agents page with grouping toggle

### 2. Agents Page Enhancement (Minimal)

**Current**: List of all agents
**Add**: Optional grouping by system prefix

```vue
<template>
  <div>
    <!-- Existing search/filter -->

    <!-- NEW: Group toggle -->
    <div class="mb-4">
      <button @click="groupBySystem = !groupBySystem">
        {{ groupBySystem ? 'Show All' : 'Group by System' }}
      </button>
    </div>

    <!-- Agent list (grouped or flat) -->
    <div v-if="groupBySystem">
      <div v-for="system in systemGroups" :key="system.name">
        <h3>{{ system.name }}</h3>
        <AgentCard v-for="agent in system.agents" :key="agent.name" :agent="agent" />
      </div>
    </div>
    <div v-else>
      <AgentCard v-for="agent in agents" :key="agent.name" :agent="agent" />
    </div>
  </div>
</template>
```

**Changes**: ~20 lines of code

### 3. Upload Manifest Dialog (Optional)

**Trigger**: Button on Agents page: "Deploy System"

**Dialog**:
- Textarea for YAML paste
- File upload button
- "Validate" button (dry_run=true)
- "Deploy" button

**OR**: Skip this entirely - use MCP only for deployment

---

## Implementation Plan

### Step 1: MCP Tools (HIGH PRIORITY)
```
1. Add 4 MCP tools to src/mcp-server/main.py:
   - deploy_system
   - list_systems
   - restart_system
   - get_system_manifest

2. Implement helper in mcp-server:
   - System grouping logic (extract prefix from agent names)
   - Call backend APIs with user auth context
```

### Step 2: Backend API Endpoints (MEDIUM PRIORITY)
```
1. Create src/backend/routers/systems.py endpoints:
   - GET /api/systems (list with grouping)
   - GET /api/systems/{name} (system details)
   - POST /api/systems/{name}/restart (bulk restart)
   - GET /api/systems/{name}/manifest (export YAML)

2. Add to main.py router mounting
```

### Step 3: UI (LOW PRIORITY - Optional)
```
Option A: Skip UI entirely, MCP-only workflow

Option B: Minimal changes to Agents.vue:
  - Add "Group by System" toggle
  - Show system headers when grouped
  - Add "Deploy System" button → Textarea dialog
```

---

## MCP-First Workflow

### Deploy a System

```bash
# 1. Create system.yaml
cat > system.yaml << 'EOF'
name: content-pipeline
description: Automated content workflow

prompt: |
  You are part of the content-pipeline system.
  Always save outputs to shared-out folder.

agents:
  orchestrator:
    template: local:business-assistant
    folders:
      expose: true
      consume: true
    schedules:
      - name: morning-planning
        cron: "0 9 * * *"
        message: "Plan today's tasks"
        enabled: false

  writer:
    template: local:business-assistant
    folders:
      expose: true
      consume: true

  reviewer:
    template: local:business-assistant
    folders:
      consume: true

permissions:
  preset: full-mesh
EOF

# 2. Deploy via MCP (dry run first)
cat system.yaml | mcp__trinity__deploy_system dry_run=true

# 3. If validation passes, deploy for real
cat system.yaml | mcp__trinity__deploy_system

# Output:
# {
#   "status": "deployed",
#   "system_name": "content-pipeline",
#   "agents_created": [
#     "content-pipeline-orchestrator",
#     "content-pipeline-writer",
#     "content-pipeline-reviewer"
#   ],
#   "permissions_configured": 6,
#   "schedules_created": 1
# }
```

### View Systems

```bash
# List all systems
mcp__trinity__list_systems

# Output:
# {
#   "systems": [
#     {
#       "name": "content-pipeline",
#       "agent_count": 3,
#       "agents": [...]
#     }
#   ]
# }
```

### Restart System

```bash
# Restart all agents in system
mcp__trinity__restart_system system_name="content-pipeline"

# Output:
# {
#   "restarted": ["content-pipeline-orchestrator", "content-pipeline-writer", "content-pipeline-reviewer"],
#   "failed": []
# }
```

### Backup System

```bash
# Export current config
mcp__trinity__get_system_manifest system_name="content-pipeline" > backup.yaml
```

---

## Testing Phase 3

### Test 1: MCP Deploy (Dry Run)

```bash
# Create test manifest
cat > test-system.yaml << 'EOF'
name: test-system
agents:
  worker:
    template: local:business-assistant
permissions:
  preset: none
EOF

# Dry run
cat test-system.yaml | mcp__trinity__deploy_system dry_run=true

# Expected: validation pass, no agents created
mcp__trinity__list_agents
# Should not include test-system-worker
```

### Test 2: MCP Deploy (Actual)

```bash
# Deploy
cat test-system.yaml | mcp__trinity__deploy_system

# Verify agents created
mcp__trinity__list_agents
# Should include test-system-worker with status "running"
```

### Test 3: List Systems

```bash
mcp__trinity__list_systems

# Expected output:
# {
#   "systems": [
#     {
#       "name": "test-system",
#       "agent_count": 1,
#       "agents": ["test-system-worker"]
#     }
#   ]
# }
```

### Test 4: Restart System

```bash
# Stop the agent manually
mcp__trinity__stop_agent agent_name="test-system-worker"

# Restart system
mcp__trinity__restart_system system_name="test-system"

# Verify agent running
mcp__trinity__get_agent agent_name="test-system-worker"
# status should be "running"
```

### Test 5: Export Manifest

```bash
# Export
mcp__trinity__get_system_manifest system_name="test-system" > exported.yaml

# Verify content matches original (minus dynamic fields)
diff test-system.yaml exported.yaml
```

### Test 6: UI Grouping (if implemented)

```bash
# Navigate to http://localhost:3000
# Click "Group by System" toggle
# Verify agents grouped under "test-system" header
```

---

## Files to Create/Modify

### MCP Server
```
src/mcp-server/main.py
  - Add 4 new tools: deploy_system, list_systems, restart_system, get_system_manifest
  - Add helper: group_agents_by_system()
```

### Backend
```
src/backend/routers/systems.py (extend existing)
  - GET /api/systems (new)
  - GET /api/systems/{name} (new)
  - POST /api/systems/{name}/restart (new)
  - GET /api/systems/{name}/manifest (new)

src/backend/services/system_service.py (extend)
  - export_manifest() - reconstruct YAML from deployed agents
```

### Frontend (Optional)
```
src/frontend/src/views/Agents.vue
  - Add groupBySystem toggle
  - Add grouping logic
  - Add "Deploy System" button (optional)
```

---

## Decision: UI or No UI?

**Option A: MCP-Only (Recommended)**
- Zero UI changes
- All deployment via MCP tools
- Existing Agents page shows all agents (no grouping)
- Fast to implement (~2-3 hours)

**Option B: Minimal UI**
- Add grouping toggle to Agents page
- Add "Deploy System" button with textarea
- ~4-5 hours implementation

**Recommendation**: Start with Option A (MCP-only). Add UI later if users request it.

---

## Completion Checklist

### MCP Tools
- [ ] `deploy_system` tool implemented
- [ ] `list_systems` tool implemented
- [ ] `restart_system` tool implemented
- [ ] `get_system_manifest` tool implemented
- [ ] MCP tools tested via Claude Code

### Backend
- [ ] `GET /api/systems` endpoint
- [ ] `GET /api/systems/{name}` endpoint
- [ ] `POST /api/systems/{name}/restart` endpoint
- [ ] `GET /api/systems/{name}/manifest` endpoint
- [ ] System grouping logic tested

### Testing
- [ ] Test 1: Dry run validation
- [ ] Test 2: Actual deployment
- [ ] Test 3: List systems
- [ ] Test 4: Restart system
- [ ] Test 5: Export manifest
- [ ] All tests passing

### Documentation
- [ ] Update `docs/memory/changelog.md`
- [ ] Update `docs/memory/requirements.md` (10.7 → ✅ Complete)
- [ ] Create feature flow: `docs/memory/feature-flows/system-manifest.md`
- [ ] Update `docs/MULTI_AGENT_SYSTEM_GUIDE.md` with MCP deployment examples

### UI (Optional)
- [ ] Agents.vue grouping toggle
- [ ] Deploy System dialog
- [ ] System cards view

---

## Post-Implementation

### Next Steps
1. User testing with real multi-agent systems
2. Consider Phase 4 (advanced features):
   - System templates (pre-configured multi-agent recipes)
   - System health dashboard
   - System-level schedules (trigger orchestrator)
   - System versioning (git-tracked manifests)

### Future Enhancements
- System templates library (e.g., "content-pipeline-template", "research-team-template")
- System-level metrics (aggregate across agents)
- System dependency graphs (orchestrator → workers visualization)
- Manifest validation against template schemas
