# Feature: Agent Lifecycle

> **Updated**: 2025-12-27 - Refactored to service layer architecture. Business logic moved from `routers/agents.py` (2928 lines) to modular services in `services/agent_service/` (12 modules, ~2200 lines total).

## Overview
Complete lifecycle management for Trinity agents: create, start, stop, and delete Docker containers with credential injection, network isolation, Trinity meta-prompt injection, and WebSocket broadcasts.

## User Story
As a Trinity platform user, I want to create, start, stop, and delete agents so that I can manage isolated Claude Code execution environments with custom configurations, credentials, and Trinity planning capabilities.

---

## Entry Points

### Create Agent
- **UI**: `src/frontend/src/views/Agents.vue:34-39` - "Create Agent" button
- **API**: `POST /api/agents`

### Start Agent
- **UI**: `src/frontend/src/views/AgentDetail.vue:44-55` - Start button (when stopped)
- **API**: `POST /api/agents/{agent_name}/start`

### Stop Agent
- **UI**: `src/frontend/src/views/AgentDetail.vue:56-67` - Stop button (when running)
- **API**: `POST /api/agents/{agent_name}/stop`

### Delete Agent
- **UI**: `src/frontend/src/views/AgentDetail.vue:110-119` - Delete button (trash icon)
- **API**: `DELETE /api/agents/{agent_name}`

---

## Frontend Layer

### Components

**Agents List View** - `src/frontend/src/views/Agents.vue`
- Line 34-39: Create Agent button opens modal
- Line 113-124: Start agent inline button with spinner
- Line 125-136: Stop agent inline button with spinner
- Line 248-258: `startAgent()` method
- Line 261-272: `stopAgent()` method

**Agent Detail View** - `src/frontend/src/views/AgentDetail.vue`
- Line 44-55: Start button (conditional on `agent.status === 'stopped'`)
- Line 56-67: Stop button (conditional on `agent.status === 'running'`)
- Line 110-119: Delete button (conditional on `agent.can_delete`)
- Line 1430-1442: `startAgent()` method
- Line 1444-1456: `stopAgent()` method
- Line 1458-1472: `deleteAgent()` method with confirmation dialog

**Create Agent Modal** - `src/frontend/src/components/CreateAgentModal.vue`
- Line 9: Form submit calls `createAgent()`
- Line 15-22: Agent name input
- Line 26-151: Template selection (blank, GitHub, local)
- Lines 191-196: `initialTemplate` prop - Pre-selects template when modal opens
- Line 198: `emit('created', agent)` - Emits created agent for navigation
- Lines 207-210: Watch for `initialTemplate` prop changes
- Lines 263-285: `createAgent()` method - emits `created` event on success

### State Management (`src/frontend/src/stores/agents.js`)

```javascript
// Line 86-102: Create agent
async createAgent(config) {
  const response = await axios.post('/api/agents', config, {
    headers: authStore.authHeader
  })
  this.agents.push(response.data)
  return response.data
}

// Line 104-119: Delete agent
async deleteAgent(name) {
  await axios.delete(`/api/agents/${name}`, {
    headers: authStore.authHeader
  })
  this.agents = this.agents.filter(agent => agent.name !== name)
}

// Line 121-135: Start agent
async startAgent(name) {
  const response = await axios.post(`/api/agents/${name}/start`, {}, {
    headers: authStore.authHeader
  })
  const agent = this.agents.find(a => a.name === name)
  if (agent) agent.status = 'running'
  return { success: true, message: response.data?.message }
}

// Line 137-151: Stop agent
async stopAgent(name) {
  const response = await axios.post(`/api/agents/${name}/stop`, {}, {
    headers: authStore.authHeader
  })
  const agent = this.agents.find(a => a.name === name)
  if (agent) agent.status = 'stopped'
  return { success: true, message: response.data?.message }
}
```

---

## Backend Layer

### Architecture (Post-Refactoring)

The agent router uses a **thin router + service layer** architecture:

| Layer | File | Purpose |
|-------|------|---------|
| Router | `src/backend/routers/agents.py` (786 lines) | Endpoint definitions, dependency injection |
| Services | `src/backend/services/agent_service/` | Business logic modules |

**Service Modules:**

| Module | Lines | Key Functions |
|--------|-------|---------------|
| `helpers.py` | 233 | `get_accessible_agents()`, `get_next_version_name()`, `check_shared_folder_mounts_match()` |
| `lifecycle.py` | 251 | `inject_trinity_meta_prompt()`, `start_agent_internal()`, `recreate_container_with_updated_config()` |
| `crud.py` | 447 | `create_agent_internal()` |

### Pydantic Models (`src/backend/models.py:10-40`)

```python
class AgentConfig(BaseModel):
    name: str
    type: Optional[str] = "business-assistant"
    base_image: str = "trinity-agent-base:latest"
    resources: Optional[dict] = {"cpu": "2", "memory": "4g"}
    tools: Optional[List[str]] = ["filesystem", "web_search"]
    mcp_servers: Optional[List[str]] = []
    custom_instructions: Optional[str] = None
    port: Optional[int] = None  # SSH port (auto-assigned)
    template: Optional[str] = None
    github_repo: Optional[str] = None  # GitHub-native agent support
    github_credential_id: Optional[str] = None

class AgentStatus(BaseModel):
    name: str
    type: str
    status: str  # "running" | "stopped"
    port: int    # SSH port only
    created: datetime
    resources: dict
    container_id: Optional[str] = None
    template: Optional[str] = None
```

### Endpoints

#### Create Agent (`src/backend/routers/agents.py:185-188`)
```python
@router.post("")
async def create_agent_endpoint(config: AgentConfig, request: Request, current_user: User = Depends(get_current_user)):
    """Create a new agent."""
    return await create_agent_internal(config, current_user, request, skip_name_sanitization=False)
```

**Service Function** (`src/backend/services/agent_service/crud.py:36-447`):

**Business Logic:**
1. **Sanitize name** (line 64-68): Lowercase, replace special chars with hyphens via `sanitize_agent_name()`
2. **Check existence** (line 70-71): Query Docker for existing container via `get_agent_by_name()`
3. **Load template** (line 81-132): GitHub or local template processing
4. **Auto-assign port** (line 134-135): Find next available SSH port (2289+) via `get_next_available_port()`
5. **Get credentials** (line 137): `credential_manager.get_agent_credentials()`
6. **Generate credential files** (line 147-152): Process template placeholders via `generate_credential_files()`
7. **Create MCP API key** (line 201-214): Generate agent-scoped Trinity MCP access key
8. **Build env vars** (line 216-261): ANTHROPIC_API_KEY, MCP server credentials, GitHub repo/PAT
9. **Create persistent volume** (line 265-277): Per-agent workspace volume for Pillar III compliance
10. **Mount Trinity meta-prompt** (line 292-297): Mount `/trinity-meta-prompt` volume for planning commands
11. **Create container** (line 346-371): Docker SDK `containers.run()` with security options
12. **Register ownership** (line 389): `db.register_agent_owner(current_user.username)`
13. **Grant default permissions** (line 391-397): Same-owner agent permissions (Phase 9.10)
14. **Create git config** (line 399-409): For GitHub-native agents (Phase 7)
15. **Broadcast WebSocket** (line 375-387): `agent_created` event
16. **Audit log** (line 411-426): `event_type="agent_management", action="create"`

#### Delete Agent (`src/backend/routers/agents.py:207-309`)
```python
@router.delete("/{agent_name}")
async def delete_agent_endpoint(agent_name: str, request: Request, current_user: User = Depends(get_current_user)):
    # System agent protection check (line 211-225)
    if db.is_system_agent(agent_name):
        raise HTTPException(403, "System agents cannot be deleted")

    # Authorization check: owner or admin (line 227-237)
    if not db.can_user_delete_agent(current_user.username, agent_name):
        raise HTTPException(403, "Permission denied")

    container = get_agent_container(agent_name)
    container.stop()
    container.remove()

    # Delete persistent volume (line 249-257)
    volume = docker_client.volumes.get(f"agent-{agent_name}-workspace")
    volume.remove()

    # Delete schedules (line 259-263)
    # Delete git config (line 266)
    # Delete MCP API key (line 268-272)
    # Delete agent permissions (line 274-278)
    # Delete shared folder config (line 280-290)
    # Delete ownership (line 292) - cascades to shares
    # Broadcast WebSocket (line 294-298), audit log (line 300-307)
```

#### Start Agent (`src/backend/routers/agents.py:316-353`)
```python
@router.post("/{agent_name}/start")
async def start_agent_endpoint(agent_name: str, request: Request, current_user: User = Depends(get_current_user)):
    # Use service function for core start logic (line 320)
    result = await start_agent_internal(agent_name)
    trinity_status = result.get("trinity_injection", "unknown")

    # Broadcast WebSocket with injection status (line 323-327)
    # Audit log with injection status (line 329-337)
```

**Service Function** (`src/backend/services/agent_service/lifecycle.py:84-127`):
```python
async def start_agent_internal(agent_name: str) -> dict:
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Check if container needs recreation for shared folders or API key settings
    container.reload()
    needs_recreation = (
        not check_shared_folder_mounts_match(container, agent_name) or
        not check_api_key_env_matches(container, agent_name)
    )

    if needs_recreation:
        # Recreate container with updated config (shared folders, API key)
        await recreate_container_with_updated_config(agent_name, container, "system")
        container = get_agent_container(agent_name)

    container.start()

    # Inject Trinity meta-prompt
    trinity_result = await inject_trinity_meta_prompt(agent_name)
    return {
        "message": f"Agent {agent_name} started",
        "trinity_injection": trinity_result.get("status", "unknown")
    }
```

**Container Recreation Triggers:**
- **Shared folder changes**: Mounts added/removed based on `shared_folder_config`
- **API key setting changes**: `ANTHROPIC_API_KEY` added/removed based on `use_platform_api_key`

**Trinity Meta-Prompt Injection** (`src/backend/services/agent_service/lifecycle.py:23-81`)
```python
async def inject_trinity_meta_prompt(agent_name: str, max_retries: int = 5, retry_delay: float = 2.0) -> dict:
    """
    Inject Trinity meta-prompt into an agent via its internal API.
    Called after agent startup to inject planning commands.

    - Retries up to 5 times with 2s delay (agent server startup)
    - Calls agent's POST /api/trinity/inject endpoint
    - Returns {"status": "success|error", ...}
    """
    agent_url = f"http://agent-{agent_name}:8000"
    # ... retry logic with httpx.AsyncClient
    response = await client.post(f"{agent_url}/api/trinity/inject")
```

**Container Recreation** (`src/backend/services/agent_service/lifecycle.py:130-251`):
Handles recreating containers with updated volume mounts and environment variables.

#### Stop Agent (`src/backend/routers/agents.py:356-393`)
```python
@router.post("/{agent_name}/stop")
async def stop_agent_endpoint(agent_name: str, request: Request, current_user: User = Depends(get_current_user)):
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    container.stop()

    # Broadcast WebSocket (line 366-370), audit log (line 372-379)
```

### Docker Service (`src/backend/services/docker_service.py`)

**Key Functions:**

| Function | Line | Purpose |
|----------|------|---------|
| `get_agent_container()` | 18-28 | Get container by name from Docker API |
| `get_agent_status_from_container()` | 31-58 | Convert Docker container to AgentStatus model |
| `list_all_agents()` | 61-73 | List all containers with `trinity.platform=agent` label |
| `get_agent_by_name()` | 76-81 | Get specific agent status |
| `get_next_available_port()` | 84-88 | Find next SSH port (2289+) |

**Status Normalization (line 38-44):**
```python
# Docker statuses: created, running, paused, restarting, removing, exited, dead
docker_status = container.status
if docker_status in ("exited", "dead", "created"):
    normalized_status = "stopped"
elif docker_status == "running":
    normalized_status = "running"
else:
    normalized_status = docker_status  # paused, restarting, etc.
```

---

## Database Layer (`src/backend/db/agents.py`)

### Tables

**agent_ownership**
```sql
CREATE TABLE agent_ownership (
    id INTEGER PRIMARY KEY,
    agent_name TEXT UNIQUE,
    owner_id INTEGER REFERENCES users(id),
    created_at TEXT
)
```

**agent_schedules** (for scheduled tasks)
```sql
CREATE TABLE agent_schedules (
    id TEXT PRIMARY KEY,
    agent_name TEXT NOT NULL,
    schedule_type TEXT NOT NULL,
    cron_expression TEXT,
    ...
)
```

**agent_git_configs** (Phase 7 - GitHub-native agents)
```sql
CREATE TABLE agent_git_configs (
    agent_name TEXT PRIMARY KEY,
    github_repo TEXT NOT NULL,
    working_branch TEXT NOT NULL,
    instance_id TEXT NOT NULL,
    ...
)
```

**agent_mcp_api_keys** (Agent-to-Agent collaboration)
```sql
CREATE TABLE agent_mcp_api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name TEXT UNIQUE NOT NULL,
    api_key TEXT UNIQUE NOT NULL,
    ...
)
```

### Operations (`src/backend/db/agents.py`)

| Method | Line | Purpose |
|--------|------|---------|
| `register_agent_owner()` | 38-55 | Record owner on creation |
| `get_agent_owner()` | 57-70 | Get owner info |
| `delete_agent_ownership()` | 85-94 | Delete ownership + cascade shares |
| `can_user_access_agent()` | 96-115 | Check if user can view agent |
| `can_user_delete_agent()` | 117-132 | Authorization check (owner or admin) |

---

## Docker Configuration

### Container Labels (`src/backend/services/agent_service/crud.py:353-362`)
| Label | Purpose |
|-------|---------|
| `trinity.platform=agent` | Identifies Trinity agents |
| `trinity.agent-name` | Agent name |
| `trinity.agent-type` | Type (business-assistant, etc.) |
| `trinity.ssh-port` | SSH port number |
| `trinity.cpu` | CPU allocation |
| `trinity.memory` | Memory limit |
| `trinity.created` | Creation timestamp (ISO format) |
| `trinity.template` | Template used (empty string if none) |

### Security Options (line 640-644)
```python
security_opt=['no-new-privileges:true', 'apparmor:docker-default'],
cap_drop=['ALL'],
cap_add=['NET_BIND_SERVICE'],
read_only=False,
tmpfs={'/tmp': 'noexec,nosuid,size=100m'}
```

### Network Isolation (line 645)
- Network: `trinity-agent-network` (Docker network)
- Only SSH port (22) mapped externally via `ports={'22/tcp': config.port}`
- UI port (8000) NOT exposed - accessed via backend proxy at `/api/agents/{name}/ui/`

### Resource Limits (line 646-647)
```python
mem_limit=config.resources.get('memory', '4Gi'),
cpu_count=int(config.resources.get('cpu', '2'))
```

### Persistent Volume (line 542-561)
```python
# Create per-agent persistent volume for /home/developer (Pillar III: Persistent Memory)
agent_volume_name = f"agent-{config.name}-workspace"
volumes = {
    ...
    agent_volume_name: {'bind': '/home/developer', 'mode': 'rw'}  # Persistent workspace
}
```

### Trinity Meta-Prompt Volume (line 569-574)
```python
# Mount Trinity meta-prompt for agent collaboration guidance
container_meta_prompt_path = Path("/trinity-meta-prompt")
host_meta_prompt_path = os.getenv("HOST_META_PROMPT_PATH", "./config/trinity-meta-prompt")
if container_meta_prompt_path.exists():
    volumes[host_meta_prompt_path] = {'bind': '/trinity-meta-prompt', 'mode': 'ro'}
```

---

## Side Effects

### WebSocket Broadcasts
| Event | Payload | Trigger |
|-------|---------|---------|
| `agent_created` | `{name, type, status, port, created, resources, container_id}` | After container.run() (line 652-664) |
| `agent_started` | `{name, trinity_injection}` | After container.start() + Trinity injection (line 986-990) |
| `agent_stopped` | `{name}` | After container.stop() (line 1029-1033) |
| `agent_deleted` | `{name}` | After container.remove() (line 803-807) |

### Audit Logging
```python
await log_audit_event(
    event_type="agent_management",
    action="create|start|stop|delete",
    user_id=current_user.username,
    agent_name=config.name,
    resource=f"agent-{config.name}",
    ip_address=request.client.host if request.client else None,
    result="success|failed|unauthorized",
    details={...}
)
```

### Cascading Deletes (on agent deletion)
1. **Persistent Volume**: Agent workspace volume deleted
2. **Schedules**: All scheduled tasks removed from scheduler and database
3. **Git Config**: GitHub sync configuration deleted
4. **MCP API Key**: Agent's Trinity MCP access key revoked
5. **Permissions**: Agent-to-agent permissions deleted (source and target)
6. **Shared Folders**: Shared folder config and shared volume deleted
7. **Ownership**: Ownership record deleted
8. **Shares**: All shares cascade deleted via foreign key constraint

---

## Error Handling

| Error Case | HTTP Status | Message |
|------------|-------------|---------|
| Invalid agent name | 400 | "Invalid agent name - must contain at least one alphanumeric character" |
| Agent already exists | 400 | "Agent already exists" |
| Agent not found | 404 | "Agent not found" |
| Permission denied (delete) | 403 | "You don't have permission to delete this agent" |
| Docker error | 500 | "Failed to create/start/stop agent: {error}" |
| Docker unavailable | 503 | "Docker not available - cannot create agents in demo mode" |

---

## Security Considerations

1. **Authentication**: All endpoints require `Depends(get_current_user)`
2. **Authorization**:
   - Delete requires `can_user_delete_agent()` (owner or admin)
   - Access requires `can_user_access_agent()` (owner, shared user, or admin)
3. **Container Security**: CAP_DROP ALL, no-new-privileges, AppArmor
4. **Network Isolation**: Agent UI not exposed externally, accessed via backend proxy
5. **Credential Protection**: Never logged, injected at runtime via environment variables
6. **Agent-scoped MCP Keys**: Each agent gets unique API key for Trinity MCP access

---

## Testing

**Prerequisites**:
- [ ] Backend running at http://localhost:8000
- [ ] Frontend running at http://localhost:3000
- [ ] Docker daemon running
- [ ] Logged in as test@example.com

**Test Steps**:

### 1. Create Agent
**Action**:
- Navigate to http://localhost:3000
- Click "Create Agent" button
- Enter name: "test-lifecycle"
- Select template: "local:default"
- Click "Create"

**Expected**:
- Agent appears in agent list
- Status shows "running"
- SSH port assigned (2290+)
- WebSocket broadcast received

**Verify**:
- [ ] UI shows agent card with name "test-lifecycle"
- [ ] API: `curl http://localhost:8000/api/agents` includes agent
- [ ] Docker: `docker ps | grep test-lifecycle` shows container
- [ ] Database: Query agent_ownership for record
- [ ] Container has correct labels: `docker inspect agent-test-lifecycle | grep trinity`

### 2. Start Agent
**Action**: Click "Start" button on stopped agent

**Expected**:
- Button shows loading spinner
- Status changes to "running"
- Toast notification appears
- WebSocket broadcast received with `trinity_injection` status
- Trinity meta-prompt injected into agent

**Verify**:
- [ ] UI shows "running" badge
- [ ] Docker: `docker inspect agent-test-lifecycle | grep '"Running": true'`
- [ ] Container accessible on internal network
- [ ] Audit log has `agent_management:start` event with `trinity_injection` in details
- [ ] Trinity injection: Agent has `/home/developer/.trinity/` directory structure
- [ ] Trinity injection: Agent has planning commands available (check via chat)

### 3. Stop Agent
**Action**: Click "Stop" button

**Expected**:
- Status changes to "stopped"
- Container stops but remains
- WebSocket broadcast received

**Verify**:
- [ ] UI shows "stopped" status
- [ ] Docker: Container exists but not running
- [ ] Can start again without recreating
- [ ] Audit log has `agent_management:stop` event

### 4. Delete Agent
**Action**: Click trash icon, confirm deletion

**Expected**:
- Agent removed
- Container deleted
- All associated resources cleaned up
- Redirected to dashboard

**Verify**:
- [ ] UI: Agent not in list
- [ ] Docker: `docker ps -a | grep test-lifecycle` returns nothing
- [ ] Database: No ownership record
- [ ] Sharing records cascade deleted
- [ ] Schedules deleted
- [ ] MCP API key deleted
- [ ] Audit log has `agent_management:delete` event

**Edge Cases**:
- [ ] Duplicate name: Try creating "test-lifecycle" twice (should fail with 400)
- [ ] Unauthorized delete: Login as different user, try to delete (should fail with 403)
- [ ] Start running agent: Already running agent start should be idempotent
- [ ] Invalid template: Create with "github:invalid/repo" (should fail gracefully)
- [ ] Name sanitization: Create agent with name "Test Agent!" (should become "test-agent")

**Cleanup**:
- [ ] Delete any remaining test agents
- [ ] `docker ps -a | grep test-` - verify no orphans

---

**Last Updated**: 2025-12-27
**Status**: Working (all CRUD operations functional with Trinity injection)
**Issues**: None - agent lifecycle fully operational with service layer architecture and Trinity meta-prompt injection

---

## Revision History

| Date | Changes |
|------|---------|
| 2025-12-27 | **Service layer refactoring**: Updated all references to new modular architecture. Business logic moved from `routers/agents.py` to `services/agent_service/` modules (lifecycle.py, crud.py, helpers.py). Router reduced from 2928 to 786 lines. |
| 2025-12-19 | **Line number updates**: Updated all line number references to match current codebase. Added Phase 9.10 (agent permissions) and Phase 9.11 (shared folders) cleanup in delete flow. Updated frontend component references. |
| 2025-12-09 | **Critical Bug Fix - File Persistence**: Added checks in `startup.sh` to skip re-cloning if repo already exists. Git-sync agents check for `.git` directory; non-git-sync agents check for `.trinity-initialized` marker. Files now persist across container restarts (Pillar III compliance). |
| 2025-12-07 | **CreateAgentModal enhancements**: Added `initialTemplate` prop for pre-selection, `created` event for navigation after success. Used by Templates.vue to open modal with template pre-selected and navigate to new agent's detail page. |

---

## Agent Container Startup Flow

### Container Initialization (`docker/base-image/startup.sh`)

When an agent container starts, it follows this initialization flow:

1. **GitHub Repository Handling** (if `GITHUB_REPO` and `GITHUB_PAT` are set):
   - **Git-Sync Enabled** (`GIT_SYNC_ENABLED=true`):
     - Checks if `/home/developer/.git` directory exists (persistent volume)
     - If exists: Skips cloning, runs `git fetch origin` to sync with remote
     - If not exists: Clones full repo, creates working branch, configures git user
     - Restores infrastructure files from base image backup
   - **Git-Sync Disabled**:
     - Checks if `/home/developer/.trinity-initialized` marker exists
     - If exists: Skips cloning, preserves user files on persistent volume
     - If not exists: Shallow clones repo, copies files, creates `.trinity-initialized` marker

2. **Local Template Handling** (if `TEMPLATE_NAME` and `/template` exists):
   - Copies `.claude/`, `CLAUDE.md`, `README.md`, `resources/`, `scripts/`, `memory/`

3. **Credential File Injection** (from `/generated-creds` volume):
   - Copies `.mcp.json`, `.env`, and other generated config files

4. **Service Startup**:
   - SSH server (if `ENABLE_SSH=true`)
   - Agent Web Server on port 8000 (if `ENABLE_AGENT_UI=true`)

### File Persistence (Bug Fix 2025-12-09)

**Problem**: Files created by agents were lost on container restart because `startup.sh` unconditionally re-cloned repositories, overwriting all user-created files.

**Solution**: Added persistence checks before cloning:

```bash
# For git-sync agents: Check for existing .git directory
if [ -d "/home/developer/.git" ]; then
    echo "Repository already exists on persistent volume - skipping clone"
    # Just fetch from remote, don't re-clone
fi

# For non-git-sync agents: Check for initialization marker
if [ -f "/home/developer/.trinity-initialized" ]; then
    echo "Agent workspace already initialized - preserving user files"
    # Skip cloning entirely
fi
```

**Key Files**:
- `docker/base-image/startup.sh:14-124` - Repository initialization with persistence checks
- `.trinity-initialized` - Marker file created after first-time initialization
- Per-agent Docker volume `agent-{name}-workspace` mounted to `/home/developer`

**Pillar III Compliance**: Agent workspace now survives restarts as required by Deep Agency spec (Persistent Memory pillar).

---

## Related Flows

- **Upstream**: Authentication Flow (JWT required via `get_current_user`)
- **Downstream**: Agent Terminal, Credential Injection, Activity Monitoring, Trinity Injection
- **Related**: Agent Sharing (ownership and access control)
- **Related**: Git Sync (GitHub-native agents)
- **Related**: Agent Scheduling (scheduled task management)
