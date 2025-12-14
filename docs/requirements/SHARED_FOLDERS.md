# Requirement 9.11: Agent Shared Folders

> **Status**: Draft
> **Priority**: High
> **Author**: Trinity Platform
> **Created**: 2025-12-13
> **Traces To**: Pillar II (Hierarchical Delegation), Pillar III (Persistent Memory)

---

## Overview

Enable agents to share files with each other through a Unix-style shared filesystem. Each agent can optionally **expose** a folder for others to access, and optionally **consume** folders exposed by other agents it has permission to access.

This follows standard Unix filesystem semantics - no platform-level conflict resolution, last-write-wins, standard file locking via `flock` if agents choose to use it.

---

## User Stories

### US-1: Agent Exposes Shared Folder
**As an** agent owner
**I want to** configure my agent to expose a shared folder
**So that** other authorized agents can access files my agent produces

### US-2: Agent Consumes Shared Folders
**As an** agent owner
**I want to** configure my agent to consume shared folders from other agents
**So that** my agent can read/write files produced by collaborating agents

### US-3: Access Control via Permissions
**As an** agent owner
**I want** shared folder access to follow existing agent permissions
**So that** only authorized agents can see my agent's shared folder

---

## Architecture

### Conceptual Model

```
research-agent                         writer-agent
┌─────────────────────────┐           ┌─────────────────────────┐
│ /home/developer/        │           │ /home/developer/        │
│   workspace/  (own)     │           │   workspace/  (own)     │
│                         │           │                         │
│   shared-out/ ──────────┼──────────►│   shared/research-agent/│
│     (I expose this)     │  Docker   │     (mounted from peer) │
│                         │  Volume   │                         │
└─────────────────────────┘           └─────────────────────────┘
        │                                       │
        ▼                                       ▼
   Volume: trinity-shared-research-agent   (same volume)
```

### Volume Naming Convention

```
trinity-shared-{agent-name}
```

Example: Agent `research-agent` with expose enabled creates volume `trinity-shared-research-agent`.

### Mount Points

| Path | Purpose |
|------|---------|
| `/home/developer/shared-out/` | Folder this agent exposes (if `expose: true`) |
| `/home/developer/shared/{agent-name}/` | Folders consumed from other agents |

### Access Control

Shared folder access **follows existing agent permissions** (Req 9.10):
- If Agent B has permission to communicate with Agent A, Agent B can mount Agent A's shared folder
- No separate permission system needed - reuses `agent_permissions` table

---

## Technical Feasibility

### Docker Volume Sharing (Verified)

Docker named volumes can be mounted into multiple containers simultaneously:

```python
# Agent A (exposing)
volumes = {
    'trinity-shared-research-agent': {
        'bind': '/home/developer/shared-out',
        'mode': 'rw'
    }
}

# Agent B (consuming)
volumes = {
    'trinity-shared-research-agent': {
        'bind': '/home/developer/shared/research-agent',
        'mode': 'rw'  # or 'ro' for read-only
    }
}
```

### Current Implementation Reference

Volume mounting in Trinity happens at container creation:
- **File**: `src/backend/routers/agents.py` (lines 479-497)
- **Pattern**: Named volumes created on-demand, mounted via Docker SDK

### Limitation: Restart Required

Volume mounts are configured at container start. To add/remove shared folder access:
1. Agent must be stopped
2. Configuration updated
3. Agent restarted with new mounts

This is an acceptable tradeoff for simplicity.

---

## Data Model

### Option A: Database Configuration (Recommended)

New table for shared folder settings:

```sql
CREATE TABLE agent_shared_folder_config (
    agent_name TEXT PRIMARY KEY,
    expose_enabled INTEGER DEFAULT 0,       -- 0=false, 1=true
    consume_enabled INTEGER DEFAULT 0,      -- 0=false, 1=true
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Index for quick lookups
CREATE INDEX idx_shared_folder_expose ON agent_shared_folder_config(expose_enabled);
CREATE INDEX idx_shared_folder_consume ON agent_shared_folder_config(consume_enabled);
```

### Option B: Template Configuration

Extend `template.yaml` schema:

```yaml
# In template.yaml
sharing:
  expose: true      # This agent exposes /shared-out
  consume: true     # This agent mounts other agents' shared folders
```

### Recommendation

Use **both**:
- Template defines default (`sharing:` section)
- Database stores runtime override (user can toggle in UI)
- Database takes precedence over template default

---

## API Specification

### Get Shared Folder Config

```
GET /api/agents/{name}/sharing
```

**Response:**
```json
{
  "agent_name": "research-agent",
  "expose_enabled": true,
  "consume_enabled": true,
  "exposed_volume": "trinity-shared-research-agent",
  "exposed_path": "/home/developer/shared-out",
  "consumed_folders": [
    {
      "source_agent": "data-processor",
      "mount_path": "/home/developer/shared/data-processor",
      "access_mode": "rw"
    }
  ]
}
```

### Update Shared Folder Config

```
PUT /api/agents/{name}/sharing
```

**Request:**
```json
{
  "expose_enabled": true,
  "consume_enabled": true
}
```

**Response:** Same as GET

**Note:** Changes require agent restart to take effect. Response should include:
```json
{
  "restart_required": true,
  "message": "Agent must be restarted for changes to take effect"
}
```

### List Available Shared Folders

```
GET /api/agents/{name}/sharing/available
```

Returns list of shared folders this agent **could** consume (based on permissions):

```json
{
  "available_folders": [
    {
      "agent_name": "research-agent",
      "owner": "user@example.com",
      "volume_name": "trinity-shared-research-agent",
      "currently_mounted": true
    },
    {
      "agent_name": "data-processor",
      "owner": "user@example.com",
      "volume_name": "trinity-shared-data-processor",
      "currently_mounted": false
    }
  ]
}
```

---

## UI Specification

### Agent Detail Page - Sharing Tab

Add new tab "Sharing" (or extend existing "Settings" tab):

```
┌─────────────────────────────────────────────────────────────┐
│  Info   Chat   Files   Logs   Schedules   Sharing   ...    │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  EXPOSE SHARED FOLDER                                       │
│  ┌─────────────────────────────────────────────────────┐   │
│  │ [✓] Enable shared folder                            │   │
│  │                                                     │   │
│  │ Path: /home/developer/shared-out                    │   │
│  │ Volume: trinity-shared-research-agent               │   │
│  │                                                     │   │
│  │ ℹ️  Files in this folder are visible to agents     │   │
│  │     that have permission to access this agent.      │   │
│  └─────────────────────────────────────────────────────┘   │
│                                                             │
│  CONSUME SHARED FOLDERS                                     │
│  ┌─────────────────────────────────────────────────────┐   │
│  │ [✓] Enable consuming shared folders                 │   │
│  │                                                     │   │
│  │ Available folders (from permitted agents):          │   │
│  │                                                     │   │
│  │ ┌─────────────────────────────────────────────┐    │   │
│  │ │ [✓] research-agent                          │    │   │
│  │ │     Owner: user@example.com                 │    │   │
│  │ │     Mount: /home/developer/shared/research-agent │   │
│  │ └─────────────────────────────────────────────┘    │   │
│  │ ┌─────────────────────────────────────────────┐    │   │
│  │ │ [ ] data-processor                          │    │   │
│  │ │     Owner: user@example.com                 │    │   │
│  │ │     Mount: /home/developer/shared/data-processor │   │
│  │ └─────────────────────────────────────────────┘    │   │
│  └─────────────────────────────────────────────────────┘   │
│                                                             │
│  ⚠️  Changes require agent restart                         │
│                                                             │
│  [ Save Changes ]  [ Restart Agent ]                        │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### Agent Card Indicator (Optional)

Show sharing status on agent cards in dashboard:
- 📤 = Exposing shared folder
- 📥 = Consuming shared folders
- 🔗 = Both

---

## Implementation Plan

### Phase 1: Backend Infrastructure

1. **Database Schema**
   - Create `agent_shared_folder_config` table
   - Add migration script

2. **Volume Management**
   - Create shared volume when agent with `expose: true` starts
   - Delete shared volume when agent deleted (with confirmation)

3. **Mount Logic in Agent Creation**
   - If `expose_enabled`: mount `trinity-shared-{name}` at `/shared-out`
   - If `consume_enabled`: for each permitted agent with expose, mount their volume

4. **API Endpoints**
   - `GET/PUT /api/agents/{name}/sharing`
   - `GET /api/agents/{name}/sharing/available`

### Phase 2: Frontend UI

1. **Sharing Tab Component**
   - Toggle for expose/consume
   - List of available shared folders
   - Checkboxes to select which to mount

2. **Restart Warning**
   - Show warning when config changes
   - "Restart Required" badge
   - Inline restart button

### Phase 3: Agent Integration

1. **Update CLAUDE.md Injection**
   - Document shared folder paths in agent instructions
   - Add to Trinity meta-prompt section

2. **Template Schema**
   - Add `sharing:` section to template.yaml spec
   - Update AGENT_TEMPLATE_SPEC.md

---

## Acceptance Criteria

### AC-1: Expose Configuration
- [ ] Agent owner can enable/disable expose via UI
- [ ] Setting persists in database
- [ ] Volume created when agent with expose starts
- [ ] Volume mounted at `/home/developer/shared-out`

### AC-2: Consume Configuration
- [ ] Agent owner can enable/disable consume via UI
- [ ] UI shows list of available shared folders (from permitted agents)
- [ ] Owner can select which folders to mount
- [ ] Selected folders mounted at `/home/developer/shared/{agent-name}/`

### AC-3: Access Control
- [ ] Only agents with permission (Req 9.10) can mount shared folder
- [ ] Unauthorized mount attempts rejected
- [ ] Audit log entry for shared folder access

### AC-4: Unix Filesystem Semantics
- [ ] Multiple agents can read/write simultaneously
- [ ] No platform-level conflict resolution
- [ ] Standard Unix file permissions apply
- [ ] Files persist across agent restarts

### AC-5: Restart Behavior
- [ ] UI shows "restart required" when config changes
- [ ] New mounts only take effect after restart
- [ ] Existing files preserved on restart

### AC-6: Cleanup
- [ ] Shared volume deleted when owning agent deleted
- [ ] Confirmation prompt before deleting volume with data
- [ ] Consuming agents gracefully handle missing volumes

---

## Security Considerations

### Access Control
- Shared folder access follows agent permission system
- No additional authentication layer needed
- Audit logging for mount operations

### Data Isolation
- Each agent's shared folder is a separate volume
- No cross-contamination between unrelated agents
- Agent can only access folders it has explicit permission for

### Volume Lifecycle
- Volumes persist independently of containers
- Deleting agent prompts for volume deletion
- Option to preserve volume data on agent deletion

---

## Testing Strategy

### Unit Tests
- Database operations for shared folder config
- Volume name generation
- Permission checking logic

### Integration Tests
- Create agent with expose → volume created
- Start agent with consume → volumes mounted
- Permission denied → mount rejected

### Manual Testing
1. Create Agent A with expose enabled
2. Write file to `/shared-out/test.txt` in Agent A
3. Create Agent B with consume enabled + permission to A
4. Verify Agent B can read `/shared/agent-a/test.txt`
5. Modify file from Agent B
6. Verify Agent A sees changes

---

## Dependencies

| Dependency | Status | Notes |
|------------|--------|-------|
| Agent Permissions (Req 9.10) | ✅ Implemented | Access control foundation |
| Docker Volume Support | ✅ Available | Native Docker feature |
| Agent Restart Flow | ✅ Implemented | Required for mount changes |
| Template System | ✅ Implemented | For default configuration |

---

## Open Questions

1. **Read-only option?** Should consumers be able to mount as read-only?
   - Recommendation: Yes, add `access_mode` field (default: `rw`)

2. **Multiple exposed folders?** Should an agent expose multiple named folders?
   - Recommendation: Start with single `/shared-out`, extend later if needed

3. **Volume size limits?** Should we limit shared folder size?
   - Recommendation: No limits initially, monitor usage

4. **File browser integration?** Should Files tab show shared folders?
   - Recommendation: Yes, show `/shared-out` and `/shared/*` in file browser

---

## Revision History

| Date | Author | Changes |
|------|--------|---------|
| 2025-12-13 | Trinity Platform | Initial draft |
