# Feature: Agent Rename (RENAME-001)

## Overview
Rename agents via UI, MCP tool, or REST API. Updates all database references atomically, renames Docker container, and broadcasts WebSocket events for real-time UI updates. System agents cannot be renamed.

## User Story
As a Trinity platform user, I want to rename my agents so that I can keep agent names meaningful and organized as their purpose evolves.

---

## Entry Points

| Method | Location | Description |
|--------|----------|-------------|
| **UI** | `src/frontend/src/components/AgentHeader.vue:26-35` | Pencil icon next to agent name |
| **API** | `PUT /api/agents/{name}/rename` | REST endpoint with `{new_name}` body |
| **MCP** | `rename_agent` tool | MCP tool with `name` and `new_name` parameters |

---

## Frontend Layer

### AgentHeader.vue (`src/frontend/src/components/AgentHeader.vue`)

**Lines 8-37**: Editable agent name section

```vue
<!-- Inline editing mode -->
<template v-if="isEditingName">
  <input
    ref="nameInput"
    v-model="editedName"
    @keydown.enter="saveName"
    @keydown.escape="cancelEditName"
    @blur="saveName"
  />
</template>

<!-- Display mode with pencil icon -->
<template v-else>
  <h1>{{ agent.name }}</h1>
  <button
    v-if="agent.can_share && !agent.is_system"
    @click="startEditName"
    title="Rename agent"
  >
    <svg><!-- pencil icon --></svg>
  </button>
</template>
```

**Lines 283-294**: Emits list includes `rename` event
```javascript
const emit = defineEmits([
  'toggle', 'delete', 'toggle-autonomy', 'toggle-read-only',
  'open-resource-modal', 'git-pull', 'git-push', 'git-refresh',
  'update-tags', 'add-tag', 'remove-tag', 'rename'
])
```

**Lines 397-431**: Name editing functions
```javascript
function startEditName() {
  editedName.value = props.agent.name
  isEditingName.value = true
  nextTick(() => nameInput.value?.focus())
}

function saveName() {
  const trimmed = editedName.value.trim()
  if (!trimmed || trimmed === props.agent.name) {
    cancelEditName()
    return
  }
  emit('rename', trimmed)
  isEditingName.value = false
}
```

### AgentDetail.vue (`src/frontend/src/views/AgentDetail.vue`)

**Lines 457-494**: `renameAgent()` handler

```javascript
async function renameAgent(newName) {
  if (!agent.value || renameLoading.value) return
  renameLoading.value = true

  try {
    const response = await fetch(`/api/agents/${agent.value.name}/rename`, {
      method: 'PUT',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${localStorage.getItem('token')}`
      },
      body: JSON.stringify({ new_name: newName })
    })

    if (!response.ok) {
      const error = await response.json()
      throw new Error(error.detail || 'Failed to rename agent')
    }

    const result = await response.json()
    showNotification(`Agent renamed to '${result.new_name}'`, 'success')

    // Navigate to new URL
    router.replace({ name: 'AgentDetail', params: { name: result.new_name } })
  } catch (error) {
    showNotification(error.message, 'error')
  } finally {
    renameLoading.value = false
  }
}
```

**Line 59**: Event handler wiring in template
```vue
<AgentHeader @rename="renameAgent" ... />
```

---

## Backend Layer

### Endpoint (`src/backend/routers/agent_rename.py`)

```python
class RenameAgentRequest(BaseModel):
    new_name: str

@router.put("/{agent_name}/rename")
async def rename_agent_endpoint(
    agent_name: str,
    body: RenameAgentRequest,
    request: Request,
    current_user: User = Depends(get_current_user)
):
```

**Business Logic Flow**:

1. **Permission Check** (lines 1397-1407)
   ```python
   if not db.can_user_rename_agent(current_user.username, agent_name):
       if db.is_system_agent(agent_name):
           raise HTTPException(403, "System agents cannot be renamed")
       raise HTTPException(403, "Permission denied")
   ```

2. **Name Validation** (lines 1410-1425)
   ```python
   # Sanitize for Docker compatibility
   sanitized_name = re.sub(r'[^a-zA-Z0-9_-]', '-', new_name.lower())
   sanitized_name = re.sub(r'-+', '-', sanitized_name).strip('-')

   # Validate
   if len(sanitized_name) > 63:
       raise HTTPException(400, "Name too long")
   if sanitized_name == agent_name:
       raise HTTPException(400, "New name same as current")
   ```

3. **Check Name Availability** (lines 1428-1430)
   ```python
   existing = get_agent_container(sanitized_name)
   if existing:
       raise HTTPException(409, "Agent already exists")
   ```

4. **Stop Container if Running** (lines 1440-1443)
   ```python
   was_running = container.status == "running"
   if was_running:
       await container_stop(container)
   ```

5. **Rename Docker Container** (lines 1446-1448)
   ```python
   new_container_name = f"agent-{sanitized_name}"
   await container_rename(container, new_container_name)
   ```

6. **Update Database** (lines 1468-1474)
   ```python
   if not db.rename_agent(agent_name, sanitized_name):
       # Rollback container rename on failure
       await container_rename(container, f"agent-{agent_name}")
       raise HTTPException(500, "Database update failed")
   ```

7. **Broadcast WebSocket** (lines 1477-1489)
   ```python
   event = {
       "event": "agent_renamed",
       "type": "agent_renamed",
       "name": sanitized_name,
       "data": {"old_name": agent_name, "new_name": sanitized_name}
   }
   await manager.broadcast(json.dumps(event))
   await filtered_manager.broadcast_filtered(event)
   ```

8. **Return Response** (lines 1495-1501)
   ```python
   return {
       "message": f"Agent renamed from '{agent_name}' to '{sanitized_name}'",
       "old_name": agent_name,
       "new_name": sanitized_name,
       "was_running": was_running,
       "note": "Restart needed" if was_running else None
   }
   ```

### Async Docker Wrapper (`src/backend/services/docker_utils.py:82-90`)

```python
async def container_rename(container, new_name: str) -> None:
    """Rename a container without blocking the event loop."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        _docker_executor,
        lambda: container.rename(new_name)
    )
```

---

## Data Layer

### Database Operations (`src/backend/db/agent_settings/metadata.py` — `MetadataMixin`)

**`rename_agent()` Method**

One transaction, one registry. `rename_agent` re-keys the parent `agent_ownership` row itself
(pinning `volume_base_name` in the same statement, #1664) and then hands *every other*
agent-keyed table to `db/agent_cleanup.py::cascade_rename`. It carries **no list of tables**,
and nothing here should ever grow one back.

| Piece | Detail |
|-------|--------|
| Registry | `AGENT_REFS` in `src/backend/db/agent_cleanup.py` — **51 `AgentRef` entries over 47 distinct tables**. Four tables appear twice: `schedule_executions`, `agent_permissions` and `agent_event_subscriptions` register two agent-identifier columns each; `mcp_api_keys` registers one column under two `extra_filter` scopes (`scope='agent'`, `scope='connector'`) |
| Consumers | Exactly two: `cascade_delete()` (purge) and `cascade_rename()` (rename) — one list, read twice |
| Policy | `CASCADE` vs `KEEP` decides **delete** only. Rename touches every entry regardless of policy, because the row's `agent_name` is a foreign-key value — so the `KEEP` entries (`schedule_executions` ×2, `nevermined_payment_log`) are re-keyed like the rest |

```python
# db/agent_settings/metadata.py::rename_agent — all inside get_engine().begin()
conn.execute(update(agent_ownership)...)         # parent row + volume_base_name pin
cascade_rename(conn, old_name, new_name)         # AGENT_REFS + EXTRA_AGENT_REFS
rename_reports_to_refs(conn, old_name, new_name) # `reports-to-<name>` tag VALUES on OTHER agents
```

**Why there is no table list here (#1819).** Rename used to be ~19 hand-written `update()`
blocks while `cascade_delete` consumed the registry — two lists answering one question, so
every table added since only ever joined one of them. By the time it was filed the rename list
had fallen **23 tables** behind `AGENT_REFS`: the reported symptom was stranded Session-tab
history, but reminders, loops, notifications, operator queue, sync state, compatibility
results, per-user memory and the Telegram / WhatsApp / VoIP / Slack bindings were stranded too.
`cascade_rename` had been written for exactly this and had zero callers. Adding a table to
`AGENT_REFS` is now all a new feature has to do — do not reintroduce a hand-maintained list.

### Entitled-module tables (`EXTRA_AGENT_REFS`)

A private/entitled module that owns an agent-scoped table joins the same lifecycle at import
time via `register_agent_owned_table(table, column)` (`db/agent_cleanup.py`). The list is empty
in OSS-only builds. It is deliberately a **second** registry rather than more `AGENT_REFS`
entries: the OSS cascade resolves `Table` objects by name from the `db/tables.py` MetaData
(`_table()`), which cannot see a table declared in the private submodule — appending one would
`KeyError` and break **agent deletion**, not merely rename. So these are keyed by NAME through
bound-parameter raw `text()`, and absent tables are skipped (OSS-only and partial installs).

**Both paths sweep both lists** — `cascade_delete` and `cascade_rename` each iterate
`AGENT_REFS` and then `EXTRA_AGENT_REFS`. Until trinity-enterprise#500 only the delete path did,
while `register_agent_owned_table`'s docstring had always promised both.

The rename endpoint was nonetheless correct the whole time, because `rename_agent` carried its
OWN copy of the sweep (ent#46) immediately after the `cascade_rename` call. That is what made the
contradiction survivable — and what made it worth fixing rather than shrugging at. The behaviour
lived in **the caller**, so the shared function disagreed with its own contract: a cross-repo
module author reads `register_agent_owned_table`'s docstring, not this method, and any second
caller of `cascade_rename` would have silently dropped the sweep. Exactly the #1819 shape, one
level down.

What that would produce is a cross-wire rather than an orphan: registered rows keep the OLD name,
the renamed agent loses them, and a NEW agent taking the freed name **inherits** them. For a table
recording which human an agent serves, that is one agent inheriting another agent's people. The
codebase names this exact class one function away, at the `delete_reports_to_refs` call site in
`cascade_delete`: *"a dangling ref would silently re-attach to an unrelated agent that reuses the
name."*

| Guard | What it pins |
|-------|--------------|
| `tests/unit/test_agent_cleanup_parity.py` | schema ↔ registry — a new agent-referencing table in `db/schema.py` with no `AgentRef` entry fails CI |
| `tests/unit/test_1819_rename_cascade_parity.py` | registry ↔ rename behaviour — seeds one row per registered table, renames, asserts nothing is left behind; structurally forbids `rename_agent` regrowing a private list |
| `tests/unit/test_ent500_cascade_rename.py` | the `EXTRA_AGENT_REFS` half — verified RED on the pre-fix code. Its probe table is deliberately absent from the OSS MetaData, because that absence *is* the reason the second registry exists: a test written against an OSS table passes on the broken code |

`rename_agent`'s ent#46-era `EXTRA_AGENT_REFS` loop was **deleted** when `cascade_rename` grew
the sweep. Leaving it would have been a third copy of the same question, and a redundant pass is
how the two lists drifted apart in the first place. `test_ent500_cascade_rename.py` pins both
levels: the shared function re-keys, the production `rename_agent` path still re-keys after the
move, and `rename_agent` may not regrow a loop of its own.

**`can_user_rename_agent()` Method**

```python
def can_user_rename_agent(self, username: str, agent_name: str) -> bool:
    """Check if user can rename (owner or admin, NOT system agents)."""
    user = self._user_ops.get_user_by_username(username)
    if not user:
        return False

    owner = self.get_agent_owner(agent_name)
    if owner and owner.get("is_system", False):
        return False  # System agents cannot be renamed

    if user["role"] == "admin":
        return True
    if owner and owner["owner_username"] == username:
        return True

    return False
```

---

## MCP Layer

### Tool Definition (`src/mcp-server/src/tools/agents.ts:260-296`)

```typescript
renameAgent: {
  name: "rename_agent",
  description:
    "Rename an agent in the Trinity platform. " +
    "Changes the agent name across all references. " +
    "System agents cannot be renamed. Only owners or admins can rename.",
  parameters: z.object({
    name: z.string().describe("Current agent name"),
    new_name: z.string().describe("New agent name"),
  }),
  execute: async ({ name, new_name }, context) => {
    const authContext = context?.session;

    // Prevent system agent self-rename
    if (authContext?.scope === "system" && authContext?.agentName === name) {
      return JSON.stringify({
        error: "Cannot rename system agent",
        reason: "System agents cannot be renamed."
      });
    }

    const apiClient = getClient(authContext);
    const result = await apiClient.renameAgent(name, new_name);
    return JSON.stringify(result, null, 2);
  },
}
```

### Client Method (`src/mcp-server/src/client.ts:277-296`)

```typescript
async renameAgent(
  name: string,
  newName: string
): Promise<{
  message: string;
  old_name: string;
  new_name: string;
  was_running: boolean;
  note?: string;
}> {
  return this.request<{...}>(
    "PUT",
    `/api/agents/${encodeURIComponent(name)}/rename`,
    { new_name: newName }
  );
}
```

---

## Side Effects

### WebSocket Broadcasts

| Event | Payload | Recipients |
|-------|---------|------------|
| `agent_renamed` | `{old_name, new_name}` | All connected UI clients |

Both main WebSocket manager and filtered Trinity Connect manager receive the event.

### Docker Operations

| Operation | Effect |
|-----------|--------|
| `container_stop()` | Stops container if running |
| `container_rename()` | Changes container name from `agent-{old}` to `agent-{new}` |

Note: Container is NOT auto-restarted. Volume rename is deferred to next restart via `recreate_container_with_updated_config()`.

---

## Error Handling

| Error Case | HTTP Status | Message |
|------------|-------------|---------|
| Not authorized | 403 | "You don't have permission to rename this agent" |
| System agent | 403 | "System agents cannot be renamed" |
| Empty name | 400 | "New name cannot be empty" |
| Same name | 400 | "New name is the same as current name" |
| Name too long | 400 | "Agent name too long (max 63 characters)" |
| Invalid after sanitize | 400 | "Invalid agent name after sanitization" |
| Name taken | 409 | "Agent with name '{name}' already exists" |
| Agent not found | 404 | "Agent not found" |
| Database failure | 500 | "Failed to update database" |
| Docker failure | 500 | "Failed to rename agent: {error}" |

---

## Security Considerations

1. **Authorization**: Only agent owners and platform admins can rename agents
2. **System Agent Protection**: `trinity-system` and other `is_system=true` agents cannot be renamed
3. **Name Sanitization**: All names sanitized for Docker/DNS compatibility (lowercase, alphanumeric + hyphens)
4. **Atomic Updates**: Database changes wrapped in transaction with rollback on failure
5. **Container Rollback**: If database update fails, container name is restored

---

## Testing

### Prerequisites
- Backend running at http://localhost:8000
- Frontend running at http://localhost
- Logged in as agent owner or admin
- Agent exists and is not a system agent

### Test Steps

1. **UI Rename (Happy Path)**
   - Action: Click pencil icon, type new name, press Enter
   - Expected: Name updates, URL changes, notification shown
   - Verify: `docker ps` shows new container name

2. **Cancel Rename**
   - Action: Click pencil, type name, press Escape
   - Expected: Reverts to original name, no API call

3. **API Rename**
   ```bash
   curl -X PUT http://localhost:8000/api/agents/old-name/rename \
     -H "Authorization: Bearer $TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"new_name": "new-name"}'
   ```
   - Expected: `{"message": "Agent renamed...", "old_name": "...", "new_name": "..."}`

4. **MCP Tool Rename**
   - Action: Use `rename_agent` tool with name and new_name
   - Expected: Same response as API

5. **Error Cases**
   - [ ] Try renaming system agent -> 403
   - [ ] Try renaming as non-owner -> 403
   - [ ] Try duplicate name -> 409
   - [ ] Try empty name -> 400

---

## Related Flows

- **Parent**: [agent-lifecycle.md](agent-lifecycle.md) - Rename is part of agent lifecycle
- **Related**: [agent-sharing.md](agent-sharing.md) - Shares are updated atomically
- **Related**: [scheduling.md](scheduling.md) - Schedules are updated atomically
- **Related**: [role-assignments.md](role-assignments.md) - Its private agent-scoped table is what made the `EXTRA_AGENT_REFS` rename gap reachable
- **Related**: [mcp-orchestration.md](mcp-orchestration.md) - MCP tool interface

---

**Last Updated**: 2026-03-01
**Status**: Implemented
**Issues**: None - feature fully operational
