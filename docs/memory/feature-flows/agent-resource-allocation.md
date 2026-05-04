# Feature: Agent Resource Allocation

## Overview
Allows users to configure memory and CPU limits for Docker agent containers. Resources are stored in the database and applied when containers are created or recreated.

## User Story
As an agent owner, I want to configure memory and CPU allocation for my agents so that I can balance performance and resource usage across my fleet.

## Revision History
| Date | Change |
|------|--------|
| 2026-04-30 | **RES-001**: Added system-wide admin defaults (`GET/PUT /api/settings/agent-defaults/resources`). Container creation now uses 3-tier fallback: per-agent DB limit → system default → hardcoded. Fixed `4Gi`→`4g` bug in crud.py. |
| 2026-02-18 | **UI-001 Redesign**: AgentHeader restructured into 3 rows. Gear button now at line 150-161 in Row 2 (stats row). Stats display inline with CPU/MEM sparklines. Fixed widths prevent layout jumping. |
| 2026-01-23 | Updated line numbers for all components; AgentHeader gear icons at 224 and 247; ResourceModal extracted to separate component; useAgentSettings composable at 78-110 |
| 2026-01-02 | Initial documentation |

---

## Entry Points

- **UI**: `src/frontend/src/components/AgentHeader.vue:150-161` - Gear icon button in Row 2 (stats row), shown when agent.can_share
- **API GET**: `GET /api/agents/{name}/resources` - Fetch per-agent limits
- **API PUT**: `PUT /api/agents/{name}/resources` - Update per-agent limits
- **Admin API GET**: `GET /api/settings/agent-defaults/resources` - Fetch system-wide defaults
- **Admin API PUT**: `PUT /api/settings/agent-defaults/resources` - Set system-wide defaults (admin-only)

---

## Frontend Layer

### Components

**AgentHeader.vue** (`src/frontend/src/components/AgentHeader.vue`)

**Layout**: Header now structured into 3 rows (UI-001 redesign):
- **Row 1 (lines 4-57)**: Identity (name, badges) + Primary Action (toggle, delete)
- **Row 2 (lines 59-163)**: Settings (toggles, tags) on left + Stats/Resources on right
- **Row 3 (lines 165-250)**: Git controls (conditional)

| Line | Element | Description |
|------|---------|-------------|
| 150-161 | Gear button | `@click="$emit('open-resource-modal')"` - in Row 2, after stats/resources |
| 111-114 | CPU display (running) | Shows `agentStats.cpu_percent` with sparkline, fixed width `w-10` |
| 127-129 | Memory display (running) | Shows formatted bytes with sparkline, fixed width `w-14` |
| 132-134 | Uptime display | Shows `formatUptime(agentStats.uptime_seconds)`, fixed width `w-16` |
| 144-148 | Stopped state resources | Shows "X CPU" and "Xg" from `resourceLimits` |
| 297-300 | resourceLimits prop | Receives limits from AgentDetail |
| 350 | emit definition | `'open-resource-modal'` |

**AgentDetail.vue** (`src/frontend/src/views/AgentDetail.vue`)

| Line | Element | Description |
|------|---------|-------------|
| 49 | `@open-resource-modal` | Event handler from AgentHeader |
| 176-185 | ResourceModal | Modal component usage |
| 234 | Import | `import ResourceModal from '../components/ResourceModal.vue'` |
| 261 | `showResourceModal` | Ref for modal visibility |
| 378-382 | composable destructure | Gets `resourceLimits`, `loadResourceLimits`, `updateResourceLimits` |
| 385-404 | `saveResourceLimits()` | Handler that saves and auto-restarts |

**ResourceModal.vue** (`src/frontend/src/components/ResourceModal.vue`)

| Line | Element | Description |
|------|---------|-------------|
| 26-35 | Warning banner | "If the agent is running, it will be automatically restarted" |
| 41-55 | Memory dropdown | Select with options: Default, 1g-64g |
| 59-71 | CPU dropdown | Select with options: Default, 1-16 cores |
| 77-84 | Save button | `@click="$emit('save')"` |
| 99-106 | Props | `show`, `resourceLimits`, `loading` |
| 108 | Emits | `update:show`, `update:memory`, `update:cpu`, `save` |

### Composable

**useAgentSettings.js** (`src/frontend/src/composables/useAgentSettings.js:78-110`)

```javascript
// State (lines 17-23)
const resourceLimits = ref({
  memory: null,      // Selected value (null = use default)
  cpu: null,         // Selected value (null = use default)
  current_memory: '4g',  // Current container limit
  current_cpu: '2',      // Current container limit
  restart_needed: false  // Set by backend response
})

// Methods
const loadResourceLimits = async () => {  // lines 78-92
  const result = await agentsStore.getResourceLimits(agentRef.value.name)
  resourceLimits.value = { ... }
}

const updateResourceLimits = async () => {  // lines 94-110
  const result = await agentsStore.setResourceLimits(
    agentRef.value.name,
    resourceLimits.value.memory,
    resourceLimits.value.cpu
  )
}
```

### Store Actions

**agents.js** (`src/frontend/src/stores/agents.js:693-710`)

```javascript
// GET /api/agents/{name}/resources (lines 693-699)
async getResourceLimits(name) {
  const response = await axios.get(`/api/agents/${name}/resources`, {
    headers: authStore.authHeader
  })
  return response.data
}

// PUT /api/agents/{name}/resources (lines 701-710)
async setResourceLimits(name, memory, cpu) {
  const response = await axios.put(`/api/agents/${name}/resources`, {
    memory: memory,
    cpu: cpu
  }, { headers: authStore.authHeader })
  return response.data
}
```

### Auto-Restart Logic

**AgentDetail.vue:385-404** - `saveResourceLimits()`:

```javascript
async function saveResourceLimits() {
  // Check if values actually changed
  const newMemory = resourceLimits.value.memory || resourceLimits.value.current_memory
  const newCpu = resourceLimits.value.cpu || resourceLimits.value.current_cpu
  const oldMemory = resourceLimits.value.current_memory
  const oldCpu = resourceLimits.value.current_cpu
  const valuesChanged = newMemory !== oldMemory || newCpu !== oldCpu

  await updateResourceLimits()
  showResourceModal.value = false

  // If values changed and agent is running, restart it
  if (valuesChanged && agent.value?.status === 'running') {
    showNotification('Restarting agent to apply new resource limits...', 'info')
    await stopAgent()
    await new Promise(resolve => setTimeout(resolve, 1000))
    await startAgent()
  }
}
```

---

## Backend Layer

### System-Wide Defaults (RES-001)

**settings.py** (`src/backend/routers/settings.py`) — Admin-only

#### GET /api/settings/agent-defaults/resources

Returns current system defaults with valid value enumerations:
```json
{
  "cpu": "2",
  "memory": "4g",
  "cpu_default": "2",
  "memory_default": "4g",
  "valid_cpu_values": ["1", "2", "4", "8", "16"],
  "valid_memory_values": ["1g", "2g", "4g", "8g", "16g", "32g"],
  "note": "Changes apply to new agent containers only."
}
```

#### PUT /api/settings/agent-defaults/resources

Body: `{ "cpu": "4", "memory": "8g" }` (fields optional — only provided fields updated)

- CPU must be a whole number of processors from the valid list (no fractional values)
- Returns `restart_required: true` — existing containers are unaffected until next start
- Stored in the `system_settings` table via `db.set_setting("agent_default_cpu", ...)` / `db.set_setting("agent_default_memory", ...)`

**settings_service.py** (`src/backend/services/settings_service.py`)

```python
AGENT_DEFAULT_CPU_KEY = "agent_default_cpu"
AGENT_DEFAULT_MEMORY_KEY = "agent_default_memory"

def get_agent_default_resources() -> dict:
    """Returns {"cpu": "2", "memory": "4g"} — system defaults with safe fallback."""
```

### Container Creation Fallback Chain

When creating or recreating a container, resources are resolved in this order:

1. **Per-agent DB limit** — `db.get_resource_limits(agent_name)` from `agent_ownership.cpu_limit / memory_limit`
2. **System default** — `get_agent_default_resources()` from `system_settings` table
3. **Hardcoded safe value** — `cpu=2`, `memory=4g`

**crud.py** (`src/backend/services/agent_service/crud.py`):
```python
mem_limit=config.resources.get('memory') or _get_default_resource('memory'),
cpu_count=int(config.resources.get('cpu') or _get_default_resource('cpu'))
```

**lifecycle.py** (`src/backend/services/agent_service/lifecycle.py`):
```python
system_defaults = get_agent_default_resources()
if db_limits:
    cpu = db_limits.get("cpu") or labels.get("trinity.cpu") or system_defaults["cpu"]
    memory = db_limits.get("memory") or labels.get("trinity.memory") or system_defaults["memory"]
else:
    cpu = labels.get("trinity.cpu") or system_defaults["cpu"]
    memory = labels.get("trinity.memory") or system_defaults["memory"]
```

### Per-Agent Endpoints

**agent_config.py** (`src/backend/routers/agent_config.py`)

#### GET /api/agents/{name}/resources (Lines 803-838)

```python
@router.get("/{agent_name}/resources")
async def get_agent_resources(
    agent_name: str,
    current_user: User = Depends(get_current_user)
):
    # Check access
    if not db.can_user_access_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="Access denied")

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Get DB limits (may be None)
    db_limits = db.get_resource_limits(agent_name)

    # Get current container limits from labels
    labels = container.attrs.get("Config", {}).get("Labels", {})
    current_memory = labels.get("trinity.memory", "4g")
    current_cpu = labels.get("trinity.cpu", "2")

    return {
        "memory": db_limits.get("memory") if db_limits else None,
        "cpu": db_limits.get("cpu") if db_limits else None,
        "current_memory": current_memory,
        "current_cpu": current_cpu
    }
```

#### PUT /api/agents/{name}/resources (Lines 841-904)

```python
@router.put("/{agent_name}/resources")
async def set_agent_resources(
    agent_name: str,
    body: dict,
    current_user: User = Depends(get_current_user)
):
    # Only owners can change resources
    if not db.can_user_share_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="Only owners can change resource limits")

    # Validate memory format
    valid_memory = ["1g", "2g", "4g", "8g", "16g", "32g", "64g"]
    if memory and memory not in valid_memory:
        raise HTTPException(status_code=400, detail=f"Invalid memory value")

    # Validate CPU format
    valid_cpu = ["1", "2", "4", "8", "16"]
    if cpu and cpu not in valid_cpu:
        raise HTTPException(status_code=400, detail=f"Invalid CPU value")

    # Update database
    db.set_resource_limits(agent_name, memory=memory, cpu=cpu)

    # Check if restart is needed
    labels = container.attrs.get("Config", {}).get("Labels", {})
    current_memory = labels.get("trinity.memory", "4g")
    current_cpu = labels.get("trinity.cpu", "2")
    restart_needed = (memory and memory != current_memory) or (cpu and cpu != current_cpu)

    return {
        "message": "Resource limits updated",
        "memory": memory,
        "cpu": cpu,
        "restart_needed": restart_needed
    }
```

### Mismatch Check at Start

**helpers.py** (`src/backend/services/agent_service/helpers.py:333-363`)

```python
def check_resource_limits_match(container, agent_name: str) -> bool:
    """
    Check if container's resource limits match the current database settings.
    Returns True if resources match, False if recreation needed.
    """
    # Get DB settings (may be None if using template defaults)
    db_limits = db.get_resource_limits(agent_name)

    # Get current container limits from labels
    labels = container.attrs.get("Config", {}).get("Labels", {})
    current_memory = labels.get("trinity.memory", "4g")
    current_cpu = labels.get("trinity.cpu", "2")

    if db_limits is None:
        # No custom limits set - container uses template defaults
        return True

    # Compare DB limits with current container limits
    expected_memory = db_limits.get("memory") or current_memory
    expected_cpu = db_limits.get("cpu") or current_cpu

    if expected_memory != current_memory:
        logger.info(f"Resource mismatch for {agent_name}: memory {current_memory} -> {expected_memory}")
        return False

    if expected_cpu != current_cpu:
        logger.info(f"Resource mismatch for {agent_name}: cpu {current_cpu} -> {expected_cpu}")
        return False

    return True
```

### Container Recreation

**lifecycle.py** (`src/backend/services/agent_service/lifecycle.py:173-224`)

```python
async def start_agent_internal(agent_name: str) -> dict:
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Check if container needs recreation (line 195-200)
    container.reload()
    needs_recreation = (
        not check_shared_folder_mounts_match(container, agent_name) or
        not check_api_key_env_matches(container, agent_name) or
        not check_resource_limits_match(container, agent_name) or  # <-- Resource check at line 198
        not check_full_capabilities_match(container, agent_name)
    )

    if needs_recreation:
        await recreate_container_with_updated_config(agent_name, container, "system")
        container = get_agent_container(agent_name)

    container.start()
    # ... trinity prompt injection
```

**lifecycle.py:227-371** - `recreate_container_with_updated_config()`:

```python
async def recreate_container_with_updated_config(agent_name, old_container, owner_username):
    # ... extract old config ...

    # Get resource limits - check DB for overrides, fallback to labels/defaults (lines 254-261)
    db_limits = db.get_resource_limits(agent_name)
    if db_limits:
        cpu = db_limits.get("cpu") or labels.get("trinity.cpu", "2")
        memory = db_limits.get("memory") or labels.get("trinity.memory", "4g")
    else:
        cpu = labels.get("trinity.cpu", "2")
        memory = labels.get("trinity.memory", "4g")

    # Update labels with new resource limits for future reference (lines 263-265)
    labels["trinity.cpu"] = cpu
    labels["trinity.memory"] = memory

    # ... stop and remove old container ...

    # Create new container with new limits (lines 348-368)
    new_container = docker_client.containers.run(
        image,
        # ... other config ...
        mem_limit=memory,    # e.g., "8g"
        cpu_count=int(cpu)   # e.g., 4
    )
```

---

## Data Layer

### Database Schema

**agent_ownership table** - Columns added via migration:

```sql
-- Columns in agent_ownership table
memory_limit TEXT,  -- e.g., "4g", "8g", "16g"
cpu_limit TEXT      -- e.g., "2", "4", "8"
```

### CRUD Operations

**db/agents.py** (`src/backend/db/agents.py:368-414`)

```python
def get_resource_limits(self, agent_name: str) -> Optional[Dict[str, str]]:
    """Get per-agent resource limits (memory and CPU)."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT memory_limit, cpu_limit
            FROM agent_ownership WHERE agent_name = ?
        """, (agent_name,))
        row = cursor.fetchone()
        if row:
            memory = row["memory_limit"]
            cpu = row["cpu_limit"]
            if memory is None and cpu is None:
                return None
            return {"memory": memory, "cpu": cpu}
        return None

def set_resource_limits(self, agent_name: str, memory: Optional[str] = None, cpu: Optional[str] = None) -> bool:
    """Set per-agent resource limits."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE agent_ownership SET memory_limit = ?, cpu_limit = ?
            WHERE agent_name = ?
        """, (memory, cpu, agent_name))
        conn.commit()
        return cursor.rowcount > 0
```

### Container Labels

Resource limits are stored in Docker container labels for reference:
- `trinity.memory`: Current memory limit (e.g., "8g")
- `trinity.cpu`: Current CPU limit (e.g., "4")

These labels are read to determine current container resources and compared against DB values to detect mismatches.

---

## Complete Flow

```
1. User clicks gear icon in AgentHeader
   ├─ AgentHeader.vue:150-161 (Row 2) → $emit('open-resource-modal')
   └─ AgentDetail.vue:52 → showResourceModal = true

2. Modal opens, shows current limits from composable
   ├─ loadResourceLimits() called on component mount (useAgentSettings.js:78)
   └─ GET /api/agents/{name}/resources (agents.py:803)
       ├─ Check user access (can_user_access_agent)
       ├─ Get DB limits (may be null)
       ├─ Get container labels (trinity.memory, trinity.cpu)
       └─ Return { memory, cpu, current_memory, current_cpu }

3. User selects new values and clicks Save
   ├─ saveResourceLimits() in AgentDetail.vue:385
   ├─ Calculates if values actually changed
   └─ Calls updateResourceLimits() (useAgentSettings.js:94)
       └─ PUT /api/agents/{name}/resources { memory, cpu } (agents.py:841)
           ├─ Validate ownership (can_user_share_agent)
           ├─ Validate memory in [1g, 2g, 4g, 8g, 16g, 32g, 64g]
           ├─ Validate CPU in [1, 2, 4, 8, 16]
           ├─ UPDATE agent_ownership SET memory_limit, cpu_limit
           └─ Return { message, memory, cpu, restart_needed }

4. Frontend auto-restarts if needed (AgentDetail.vue:396-403)
   ├─ If valuesChanged && agent.status === 'running':
   │   ├─ showNotification("Restarting agent...")
   │   ├─ await stopAgent()
   │   ├─ wait 1 second
   │   └─ await startAgent()

5. On agent start, backend checks for mismatches (lifecycle.py:195-200)
   ├─ start_agent_internal() in lifecycle.py:173
   ├─ check_resource_limits_match() returns False if DB != container (helpers.py:333)
   └─ If mismatch:
       └─ recreate_container_with_updated_config() (lifecycle.py:227)
           ├─ Stop and remove old container
           ├─ Read new limits from DB (line 255)
           ├─ Update trinity.* labels (line 264)
           └─ Create new container with mem_limit and cpu_count (line 366-367)
```

---

## Error Handling

| Error Case | HTTP Status | Message |
|------------|-------------|---------|
| Agent not found | 404 | "Agent not found" |
| Access denied (not accessible) | 403 | "Access denied" |
| Not owner (trying to change) | 403 | "Only owners can change resource limits" |
| Invalid memory value | 400 | "Invalid memory value. Must be one of: 1g, 2g, 4g, 8g, 16g, 32g, 64g" |
| Invalid CPU value | 400 | "Invalid CPU value. Must be one of: 1, 2, 4, 8, 16" |

---

## Security Considerations

1. **Ownership Check**: Only agent owners can modify resource limits (`can_user_share_agent`)
2. **Access Check**: Reading limits requires agent access (`can_user_access_agent`)
3. **Input Validation**: Strict validation of memory and CPU values against allowlists
4. **Docker Security**: Recreated containers maintain security options (cap_drop=['ALL'], cap_add based on mode, apparmor:docker-default)

---

## Related Flows

- **Upstream**: [agent-lifecycle.md](agent-lifecycle.md) - Container start/stop/recreate logic
- **Downstream**: None (terminal operation)
- **Similar**: [agent-shared-folders.md](agent-shared-folders.md) - Also triggers container recreation on start

---

## Testing

### Prerequisites
- Backend running at localhost:8000
- Frontend running at localhost:3000
- At least one agent created and owned by test user

### Test Steps

1. **View Resource Settings (Running Agent)**
   - Action: Navigate to agent detail page for a running agent
   - Expected: Row 2 shows CPU/MEM sparklines with live stats; gear icon visible at end of row
   - Verify: Click opens modal with current values; stats have fixed widths (no layout jumping)

2. **View Resource Settings (Stopped Agent)**
   - Action: Navigate to agent detail page for a stopped agent
   - Expected: Row 2 shows "Created X ago | X CPU | Xg" format with gear icon
   - Verify: Click opens modal with current values

3. **Change Memory Limit**
   - Action: Select "8 GB" from memory dropdown, click Save
   - Expected: Success notification, modal closes
   - Verify: Reopen modal - "8g" should be selected
   - If running: Agent should restart automatically

4. **Change CPU Limit**
   - Action: Select "4 Cores" from CPU dropdown, click Save
   - Expected: Success notification, agent restarts if running
   - Verify: Check container with `docker inspect` - `CpuCount: 4`

5. **Reset to Default**
   - Action: Select "Default" for both fields, click Save
   - Expected: Success, values stored as null in DB
   - Verify: Container uses template defaults on next start

6. **Non-Owner Access**
   - Action: Share agent with another user, log in as that user
   - Expected: Gear icon should NOT be visible
   - Verify: API returns 403 if called directly

### Edge Cases
- Invalid memory value via API: 400 error with validation message
- Invalid CPU value via API: 400 error with validation message
- Agent deleted while modal open: Error on save
- Rapid clicking Save: Loading state prevents double-submit

### Status
:construction: Not Tested
