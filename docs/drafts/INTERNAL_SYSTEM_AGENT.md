# Internal System Agent - Requirements & Design

> **Status**: Draft
> **Created**: 2025-12-20
> **Author**: Claude Code
> **Requirement ID**: 11.1

---

## Overview

The **Internal System Agent** (codename: `trinity-system`) is a privileged, auto-deployed agent that operates at the platform level. It serves as the primary executor for system-level user requests, orchestrating other agents through MCP tools and providing a unified interface for complex multi-agent operations.

### Vision

Users interact with Trinity through this system agent, which can:
- Delegate tasks to specialized agents
- Coordinate multi-agent workflows
- Execute system-level operations (deploy systems, manage agents)
- Provide platform-wide observability and control

---

## User Stories

1. **As a platform user**, I want a system agent that can orchestrate my other agents so that I don't need to manually coordinate complex workflows.

2. **As a platform operator**, I want the system agent to be always available and protected from accidental deletion.

3. **As an administrator**, I want to be able to re-initialize the system agent without losing the platform's orchestration capabilities.

---

## Functional Requirements

### 11.1.1 Auto-Deployment on Platform Startup

**Priority**: Critical

The system agent must be automatically deployed when the Trinity platform starts.

**Acceptance Criteria**:
- [ ] System agent is created on first platform startup if not exists
- [ ] System agent is started automatically if stopped during platform restart
- [ ] Uses local template from `config/agent-templates/trinity-system/`
- [ ] Owned by a special system user (not a regular user)
- [ ] Appears in agent list with special "System" badge
- [ ] Backend initialization includes system agent health check

**Implementation Notes**:
- Add initialization in `src/backend/main.py` `lifespan` function
- Create system user in database if not exists (e.g., `system@trinity.local`)
- Check if `trinity-system` container exists; create if not
- Start container if stopped

### 11.1.2 Deletion Protection

**Priority**: Critical

The system agent must be protected from deletion through all interfaces.

**Acceptance Criteria**:
- [ ] Cannot be deleted via REST API (`DELETE /api/agents/trinity-system` returns 403)
- [ ] Cannot be deleted via MCP tool (`delete_agent("trinity-system")` returns error)
- [ ] Cannot be deleted via UI (delete button hidden/disabled)
- [ ] Error message clearly states: "System agent cannot be deleted"
- [ ] Admins can force re-initialization but not permanent deletion

**Implementation Notes**:
- Add `is_system_agent(agent_name)` check in `src/backend/db/agents.py`
- Modify `can_user_delete_agent()` to return False for system agents
- Add check in MCP `delete_agent` tool in `src/mcp-server/src/tools/agents.ts`
- Frontend: Hide delete button when `agent.is_system === true`

### 11.1.3 Re-Initialization Capability

**Priority**: High

Administrators must be able to re-initialize the system agent to restore it to a clean state.

**Acceptance Criteria**:
- [ ] `POST /api/agents/trinity-system/reinitialize` endpoint (admin-only)
- [ ] MCP tool: `reinitialize_system_agent()` (admin-only)
- [ ] Re-initialization stops agent, clears workspace, re-clones template, restarts
- [ ] Preserves agent identity (same name, same MCP key)
- [ ] Audit log entry for re-initialization events
- [ ] WebSocket broadcast for re-initialization status

**Implementation Notes**:
- Similar to delete + create but without removing database records
- Clear `/home/developer/workspace/*` but keep volume
- Re-run Trinity meta-prompt injection
- Reset chat session (optional: archive previous sessions)

### 11.1.4 Local Template Structure

**Priority**: High

The system agent uses a dedicated local template with platform-specific capabilities.

**Template Location**: `config/agent-templates/trinity-system/`

**Template Structure**:
```
config/agent-templates/trinity-system/
├── template.yaml           # Template metadata
├── CLAUDE.md               # System agent instructions
├── .mcp.json.template      # Trinity MCP pre-configured
└── resources/
    └── system-prompts/     # System-level prompt library
```

**Acceptance Criteria**:
- [ ] `template.yaml` defines system agent metadata
- [ ] `CLAUDE.md` contains system-level instructions and capabilities
- [ ] Trinity MCP server pre-configured in `.mcp.json.template`
- [ ] Higher resource allocation (CPU: 4, Memory: 8g) for orchestration workloads
- [ ] Custom metrics defined for system health monitoring

**CLAUDE.md Content Should Include**:
- Platform overview and system agent role
- Available MCP tools and their usage patterns
- Multi-agent orchestration guidelines
- System manifest deployment instructions
- Error handling and escalation procedures

### 11.1.5 MCP Integration

**Priority**: High

The system agent must have full access to Trinity MCP tools for orchestration.

**Acceptance Criteria**:
- [ ] Pre-configured Trinity MCP server in `.mcp.json`
- [ ] System agent MCP key with elevated permissions
- [ ] Can call all 12+ Trinity MCP tools
- [ ] Can communicate with all agents (no permission restrictions)
- [ ] Access to system-level tools (deploy_system, list_systems, etc.)

**Special Permissions**:
- System agent bypasses normal agent-to-agent permission checks
- Can deploy and manage multi-agent systems
- Can access all agents regardless of ownership
- Can trigger agent re-initialization

**Implementation Notes**:
- Add `scope: "system"` to system agent's MCP API key
- Modify permission checks to allow system agent access
- Consider rate limiting for system agent operations

### 11.1.6 UI Visibility

**Priority**: Medium

The system agent must be clearly identifiable in the UI.

**Acceptance Criteria**:
- [ ] System badge/indicator on agent card in Agents list
- [ ] Special icon or color scheme for system agent
- [ ] "System Agent" label in agent detail header
- [ ] Delete button hidden (not just disabled)
- [ ] Re-initialize button visible for admins only
- [ ] Info tab explains system agent role and capabilities

**UI Indicators**:
- Badge: "System" with distinct color (e.g., purple)
- Icon: Shield or gear icon
- Tooltip: "Trinity System Agent - Platform orchestrator"

### 11.1.7 Collaboration Dashboard Integration

**Priority**: Medium

The system agent should appear prominently in the Collaboration Dashboard.

**Acceptance Criteria**:
- [ ] System agent node has distinct visual styling
- [ ] Positioned centrally or at top of graph (optional)
- [ ] Shows active orchestration connections
- [ ] Activity state reflects current operations
- [ ] Click-through to system agent detail works

**Visual Styling**:
- Larger node size than regular agents
- Distinct border color (gold/purple)
- "System" label always visible
- Special icon overlay

---

## Non-Functional Requirements

### Performance
- System agent should start within 30 seconds of platform startup
- Re-initialization should complete within 60 seconds
- No performance impact on other agents

### Reliability
- System agent must auto-recover from crashes
- Health check endpoint for monitoring
- Automatic restart on container failure (Docker restart policy)

### Security
- System agent credentials stored securely in Redis
- Audit logging for all system agent operations
- Rate limiting for system-level operations
- No external network access unless explicitly configured

### Observability
- System agent logs accessible via UI
- Custom metrics for orchestration operations
- Activity stream integration for all operations
- Cost tracking for system agent usage

---

## Database Schema Changes

### Option A: Flag in agent_ownership

```sql
ALTER TABLE agent_ownership ADD COLUMN is_system INTEGER DEFAULT 0;
```

### Option B: Separate system_agents table

```sql
CREATE TABLE system_agents (
    agent_name TEXT PRIMARY KEY,
    agent_type TEXT NOT NULL DEFAULT 'orchestrator',
    created_at TEXT NOT NULL,
    last_initialized_at TEXT,
    config TEXT,  -- JSON configuration
    FOREIGN KEY (agent_name) REFERENCES agent_ownership(agent_name)
);
```

**Recommended**: Option A (simpler, leverages existing infrastructure)

---

## API Changes

### New Endpoints

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| POST | `/api/system-agent/reinitialize` | Re-initialize system agent | Admin |
| GET | `/api/system-agent/status` | Get system agent health | Any |
| POST | `/api/system-agent/restart` | Restart system agent | Admin |

### Modified Endpoints

| Method | Path | Change |
|--------|------|--------|
| DELETE | `/api/agents/{name}` | Return 403 for system agent |
| GET | `/api/agents/{name}` | Add `is_system` field to response |
| GET | `/api/agents` | Add `is_system` field to each agent |

### New MCP Tools

| Tool | Description | Auth |
|------|-------------|------|
| `reinitialize_system_agent` | Re-initialize system agent | Admin |
| `get_system_agent_status` | Get system agent health status | Any |

### Modified MCP Tools

| Tool | Change |
|------|--------|
| `delete_agent` | Return error for system agent |
| `list_agents` | Include `is_system` field |

---

## Implementation Phases

### Phase 1: Core Infrastructure (Priority: Critical)
1. Create `trinity-system` local template
2. Add `is_system` flag to database schema
3. Implement deletion protection in backend
4. Add auto-deployment on startup

### Phase 2: MCP Integration (Priority: High)
1. Create system-scoped MCP API key
2. Configure Trinity MCP in template
3. Implement permission bypass for system agent
4. Add MCP tool modifications

### Phase 3: Re-Initialization (Priority: High)
1. Implement reinitialize endpoint
2. Add MCP reinitialize tool
3. Add audit logging
4. Implement WebSocket broadcasts

### Phase 4: UI Integration (Priority: Medium)
1. Add system badge to agent cards
2. Hide delete button for system agent
3. Add reinitialize button for admins
4. Update Collaboration Dashboard styling

### Phase 5: Observability (Priority: Medium)
1. Add custom metrics to template
2. Implement health check endpoint
3. Add system agent activity tracking
4. Dashboard integration

---

## Testing

### Prerequisites
- [ ] Backend running with database initialized
- [ ] Docker daemon running
- [ ] Admin user available for testing

### Test Cases

#### 1. Auto-Deployment
- [ ] Fresh platform start creates system agent
- [ ] Platform restart doesn't duplicate system agent
- [ ] System agent starts automatically after platform restart

#### 2. Deletion Protection
- [ ] API DELETE returns 403 with clear error message
- [ ] MCP delete_agent returns error
- [ ] UI doesn't show delete button
- [ ] Admin also cannot delete (only reinitialize)

#### 3. Re-Initialization
- [ ] Admin can reinitialize via API
- [ ] Admin can reinitialize via MCP
- [ ] Workspace is cleared but agent identity preserved
- [ ] New chat session started
- [ ] Audit log entry created

#### 4. MCP Operations
- [ ] System agent can list all agents
- [ ] System agent can chat with any agent
- [ ] System agent can deploy systems
- [ ] System agent bypasses permission checks

#### 5. UI Visibility
- [ ] System badge visible on agent card
- [ ] System label in agent detail
- [ ] Reinitialize button visible for admins
- [ ] Delete button hidden

---

## Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| System agent consumes excessive resources | Platform degradation | Resource limits in template, monitoring |
| System agent infinite loops with other agents | Cost explosion | Rate limiting, thinking budget |
| System agent credentials compromised | Full platform access | Rotate keys, audit logging, alerts |
| Auto-deployment fails on startup | No orchestration available | Health checks, manual fallback, alerts |

---

## Future Enhancements

1. **Multiple System Agents** - Specialized system agents for different domains
2. **System Agent Plugins** - Extensible capabilities via plugins
3. **Cross-Platform Orchestration** - System agent coordinates with external systems
4. **Natural Language Interface** - Direct user chat with system agent via UI
5. **Autonomous Operations** - System agent performs scheduled maintenance

---

## Related Documents

- [Agent Lifecycle](../memory/feature-flows/agent-lifecycle.md)
- [MCP Orchestration](../memory/feature-flows/mcp-orchestration.md)
- [Agent Permissions](../memory/feature-flows/agent-permissions.md)
- [System Manifest](../memory/feature-flows/system-manifest.md)
- [Template Processing](../memory/feature-flows/template-processing.md)

---

## Appendix: Template Files

### template.yaml

```yaml
name: trinity-system
display_name: "Trinity System Agent"
description: "Platform-level orchestrator for multi-agent coordination"
version: "1.0.0"
author: "Trinity Platform"

type: system-orchestrator

resources:
  cpu: "4"
  memory: "8g"

capabilities:
  - agent-orchestration
  - system-deployment
  - multi-agent-coordination
  - platform-monitoring

metrics:
  - name: orchestrations_completed
    type: counter
    label: "Orchestrations"
    description: "Number of multi-agent orchestrations completed"
  - name: active_workflows
    type: gauge
    label: "Active Workflows"
    description: "Currently running workflow count"
  - name: system_health
    type: status
    label: "Health"
    description: "Overall system health status"
    values: ["healthy", "degraded", "critical"]
```

### CLAUDE.md (Excerpt)

```markdown
# Trinity System Agent

You are the **Trinity System Agent**, the platform-level orchestrator for the Trinity Deep Agent Orchestration Platform.

## Your Role

You coordinate and orchestrate other agents in the Trinity platform. You have access to Trinity MCP tools that allow you to:
- List, create, start, stop agents
- Send messages to any agent
- Deploy multi-agent systems from YAML manifests
- Monitor agent health and activity

## Available Tools

You have access to the Trinity MCP server with these tools:
- `mcp__trinity__list_agents` - List all agents
- `mcp__trinity__chat_with_agent` - Send message to an agent
- `mcp__trinity__deploy_system` - Deploy multi-agent system
- `mcp__trinity__list_systems` - List deployed systems
- ... (all 12+ tools)

## Guidelines

1. **Delegate appropriately** - Use specialized agents for domain-specific tasks
2. **Monitor progress** - Check agent responses and handle failures
3. **Coordinate workflows** - Break complex tasks into agent-specific subtasks
4. **Report status** - Keep users informed of orchestration progress
```
