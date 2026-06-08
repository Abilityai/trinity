# Feature: Read-Only Mode (CFG-007)

## Overview

Read-only mode prevents agents from modifying source code, instructions, or configuration files while allowing output to designated directories. Uses Claude Code's PreToolUse hooks to intercept Write/Edit/NotebookEdit operations.

## User Story

As an agent owner, I want to enable read-only mode so that the agent cannot modify critical files (code, configs, CLAUDE.md) while still allowing it to write reports and output files.

## Entry Points

- **UI (Agent Detail)**: `src/frontend/src/components/AgentHeader.vue:128-136` - ReadOnlyToggle component
- **UI (Agents List)**: `src/frontend/src/views/Agents.vue:248-255` - ReadOnlyToggle in card toggles row (between Running and Autonomy)
- **API**: `GET/PUT /api/agents/{name}/read-only`

---

## Frontend Layer

### Components

#### ReadOnlyToggle.vue (172 lines)
`src/frontend/src/components/ReadOnlyToggle.vue`

Reusable toggle component with rose/red color scheme for read-only state indication.

| Line Range | Element | Description |
|------------|---------|-------------|
| 1-68 | Template | Toggle button with lock icon, loading spinner, size variants |
| 70-119 | Props | `modelValue`, `disabled`, `loading`, `showLabel`, `size` |
| 122-162 | Computed | Size classes for sm/md/lg variants |
| 164-170 | toggle() | Emits `update:modelValue` and `toggle` events |

**Features:**
- Rose background when enabled (read-only ON)
- Lock icon inside toggle when enabled
- Loading spinner during API call
- ARIA support with descriptive labels
- Three sizes: sm, md, lg

#### AgentHeader.vue
`src/frontend/src/components/AgentHeader.vue:128-136`

Contains the ReadOnlyToggle in the header actions row.

```vue
<!-- Read-Only Mode Toggle (not for system agents) -->
<template v-if="!agent.is_system && agent.can_share">
  <div class="h-4 w-px bg-gray-300 dark:bg-gray-600 mx-1"></div>
  <ReadOnlyToggle
    :model-value="agent.read_only_enabled"
    :loading="readOnlyLoading"
    size="md"
    @toggle="$emit('toggle-read-only')"
  />
</template>
```

**Props passed from parent:**
- `readOnlyLoading`: Loading state during API call

**Events emitted:**
- `toggle-read-only`: Triggered when user clicks toggle

### View Handler

#### AgentDetail.vue
`src/frontend/src/views/AgentDetail.vue:373-411`

```javascript
// Read-only mode state
const readOnlyLoading = ref(false)

async function toggleReadOnly() {
  if (!agent.value || readOnlyLoading.value) return

  readOnlyLoading.value = true
  const newState = !agent.value.read_only_enabled

  try {
    const response = await fetch(`/api/agents/${agent.value.name}/read-only`, {
      method: 'PUT',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${localStorage.getItem('token')}`
      },
      body: JSON.stringify({ enabled: newState })
    })

    if (!response.ok) {
      const error = await response.json()
      throw new Error(error.detail || 'Failed to update read-only mode')
    }

    const result = await response.json()

    // Update local state
    agent.value.read_only_enabled = newState

    showNotification(
      newState
        ? `Read-only mode enabled. Agent cannot modify source files.${result.hooks_injected ? '' : ' Hooks will be applied on next agent start.'}`
        : 'Read-only mode disabled. Agent can modify all files.',
      'success'
    )
  } catch (error) {
    console.error('Failed to toggle read-only mode:', error)
    showNotification(error.message || 'Failed to update read-only mode', 'error')
  } finally {
    readOnlyLoading.value = false
  }
}
```

### State Management

Agent state includes `read_only_enabled` from backend response.

| Location | Description |
|----------|-------------|
| `AgentDetail.vue:196-197` | `read_only_enabled` extracted in `get_agent_endpoint()` response |
| `AgentDetail.vue:331` | `readOnlyLoading` ref for UI loading state |

### API Calls

```javascript
// Get read-only status
GET /api/agents/{name}/read-only
Authorization: Bearer {token}

// Set read-only status
PUT /api/agents/{name}/read-only
Authorization: Bearer {token}
Content-Type: application/json

{
  "enabled": true,
  "config": {  // Optional - uses defaults if not provided
    "blocked_patterns": ["*.py", "*.js", "CLAUDE.md", ...],
    "allowed_patterns": ["content/*", "output/*", "*.log", ...]
  }
}
```

---

## Backend Layer

### Endpoints

#### routers/agents.py
`src/backend/routers/agents.py:814-848`

```python
@router.get("/{agent_name}/read-only")
async def get_agent_read_only_status(
    agent_name: str,
    current_user: User = Depends(get_current_user)
):
    """Get the read-only mode status for an agent."""
    from services.agent_service.read_only import get_read_only_status_logic
    return await get_read_only_status_logic(agent_name, current_user)


@router.put("/{agent_name}/read-only")
async def set_agent_read_only_status(
    agent_name: str,
    body: dict,
    current_user: User = Depends(get_current_user)
):
    """Set the read-only mode status for an agent."""
    from services.agent_service.read_only import set_read_only_status_logic
    return await set_read_only_status_logic(agent_name, body, current_user)
```

### Service Layer

#### read_only.py
`src/backend/services/agent_service/read_only.py` (~253 lines)

**Functions:**

| Function | Description |
|----------|-------------|
| `get_default_config()` | Returns default blocked/allowed patterns |
| `get_read_only_status_logic()` | GET endpoint handler - returns status and config |
| `set_read_only_status_logic()` | PUT endpoint handler - validates, saves, syncs config file |
| `inject_read_only_hooks()` | Writes `{"enabled": true, ...config}` to `~/.trinity/read-only-config.json` only |
| `remove_read_only_hooks()` | Writes `{"enabled": false}` to config; calls `_remove_legacy_settings_hook()` |
| `_remove_legacy_settings_hook()` | Migration helper: strips old `"Write\|Edit\|NotebookEdit"` entry from `settings.local.json` |

**Key invariant**: `inject_read_only_hooks()` writes **one file only** — the config JSON. The guard script lives at `/opt/trinity/hooks/read-only-guard.py` (root-owned in base image) and its hook registration lives in `~/.claude/settings.json` (base image `claude-settings.json`). Neither is touched at runtime.

**Default Blocked Patterns:**
```python
DEFAULT_BLOCKED_PATTERNS = [
    "*.py", "*.js", "*.ts", "*.jsx", "*.tsx", "*.vue", "*.svelte",
    "*.go", "*.rs", "*.rb", "*.java", "*.c", "*.cpp", "*.h",
    "*.sh", "*.bash", "Makefile", "Dockerfile",
    "CLAUDE.md", "README.md", ".claude/*", ".env", ".env.*",
    "template.yaml", "*.yaml", "*.yml", "*.json", "*.toml"
]
```

**Default Allowed Patterns:**
```python
DEFAULT_ALLOWED_PATTERNS = [
    "content/*", "output/*", "reports/*", "exports/*",
    "*.log", "*.txt",
    ".trinity/operator-queue.json"  # agent must be able to write queue items
]
```

#### lifecycle.py
`src/backend/services/agent_service/lifecycle.py`

Config is **always synced** on every agent start (both enable and disable paths):

```python
# Sync read-only config file on every start so the baked-in guard always
# reflects the current DB state — prevents stale enabled:true config from
# persisting on the volume after the user disables read-only mode (#887).
read_only_data = db.get_read_only_mode(agent_name)
try:
    if read_only_data.get("enabled"):
        result = await inject_read_only_hooks(agent_name, read_only_data.get("config"))
    else:
        result = await remove_read_only_hooks(agent_name)
    read_only_result = {"status": "success" if result.get("success") else "failed", **result}
except Exception as e:
    logger.warning(f"Failed to sync read-only config for agent {agent_name}: {e}")
    read_only_result = {"status": "failed", "error": str(e)}
```

**Why always sync**: The agent workspace volume persists across container restarts. If a user disables read-only mode while the agent is stopped and then starts it, the stale `enabled:true` config file would otherwise remain on the volume, keeping the guard active despite the DB saying disabled.

### Database Operations

#### db/agents.py
`src/backend/db/agents.py:463-516`

```python
def get_read_only_mode(self, agent_name: str) -> dict:
    """
    Get read-only mode status and configuration for an agent.

    Returns:
        dict with 'enabled' (bool) and 'config' (dict or None)
    """
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT COALESCE(read_only_mode, 0) as read_only_mode, read_only_config
            FROM agent_ownership WHERE agent_name = ?
        """, (agent_name,))
        row = cursor.fetchone()
        if row:
            import json
            config = None
            if row["read_only_config"]:
                try:
                    config = json.loads(row["read_only_config"])
                except json.JSONDecodeError:
                    config = None
            return {
                "enabled": bool(row["read_only_mode"]),
                "config": config
            }
        return {"enabled": False, "config": None}


def set_read_only_mode(self, agent_name: str, enabled: bool, config: dict = None) -> bool:
    """
    Set read-only mode status and configuration for an agent.
    """
    with get_db_connection() as conn:
        cursor = conn.cursor()
        import json
        config_json = json.dumps(config) if config else None
        cursor.execute("""
            UPDATE agent_ownership SET read_only_mode = ?, read_only_config = ?
            WHERE agent_name = ?
        """, (1 if enabled else 0, config_json, agent_name))
        conn.commit()
        return cursor.rowcount > 0
```

#### database.py Migration
`src/backend/database.py:260-276`

```python
def _migrate_agent_ownership_read_only_mode(cursor, conn):
    """Add read_only_mode and read_only_config columns to agent_ownership table."""
    cursor.execute("PRAGMA table_info(agent_ownership)")
    columns = {row[1] for row in cursor.fetchall()}

    new_columns = [
        ("read_only_mode", "INTEGER DEFAULT 0"),  # 0 = disabled, 1 = enabled
        ("read_only_config", "TEXT")  # JSON config for blocked/allowed patterns
    ]

    for col_name, col_def in new_columns:
        if col_name not in columns:
            print(f"Adding {col_name} column to agent_ownership for read-only mode...")
            cursor.execute(f"ALTER TABLE agent_ownership ADD COLUMN {col_name} {col_def}")

    conn.commit()
```

---

## Agent Layer

### Guard Script

#### read-only-guard.py
`docker/base-image/hooks/read-only-guard.py` (~80 lines)

PreToolUse hook script baked into the base image at `/opt/trinity/hooks/read-only-guard.py` (root-owned 0555, cannot be overwritten by the agent).

**Protocol (Claude Code hooks):**
- Input: JSON via stdin with `tool_input`
- Exit 0: Allow operation
- Exit 2 + stderr message: Block operation (feedback to Claude)
- Wrapped by `run_hook(main)` from `lib.py` — **fail-closed**: any uncaught exception exits 2

**Logic Flow:**
1. Read JSON from stdin via `read_stdin_json()` (lib.py)
2. Load `~/.trinity/read-only-config.json` — if missing or `enabled: false`, `allow()` immediately
3. For `MultiEdit`: iterate `tool_input["edits"]`, call `_check_path()` on each entry's `file_path`
4. For all other tools: check `tool_input.get("file_path")` or `tool_input.get("notebook_path")`
5. `_check_path()`: allowed patterns first (take precedence), then blocked patterns → `deny()`
6. Default: `allow()` (anything not blocked)

**Key fix vs pre-#887**: Handles `MultiEdit` (`edits[]` array) which has no top-level `file_path` — the old guard exited 0 for all MultiEdit calls, allowing bulk writes to blocked files.

```python
def main():
    data = read_stdin_json()
    tool_input = data.get("tool_input") or {}
    cfg = _load_read_only_config()
    if cfg is None:
        allow()  # disabled

    # MultiEdit: edits[] array — no top-level file_path
    for edit in (tool_input.get("edits") or []):
        if isinstance(edit, dict):
            _check_path(edit.get("file_path") or "", cfg)

    # Single file tools
    path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    if path:
        _check_path(path, cfg)
    allow()  # default

if __name__ == "__main__":
    run_hook(main)  # fail-closed wrapper
```

### Files Written to Agent Container

When read-only mode is enabled, **one file** is written:

| Path | Purpose |
|------|---------|
| `~/.trinity/read-only-config.json` | Config with `enabled: true` + blocked/allowed patterns |

When read-only mode is disabled, the same file is overwritten with `{"enabled": false}`.

**The guard script and hook registration are NOT written at runtime** — they are baked into the base image.

### Hook Registration

The hook is registered in `~/.claude/settings.json` (base image `claude-settings.json`, developer-readable 0644, not overwritten at runtime):

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Edit|Write|NotebookEdit|MultiEdit",
        "hooks": [
          {"type": "command", "command": "/usr/bin/python3 /opt/trinity/hooks/file-guardrail.py"},
          {"type": "command", "command": "/usr/bin/python3 /opt/trinity/hooks/read-only-guard.py"}
        ]
      }
    ]
  }
}
```

The guard runs on **every** agent regardless of read-only mode status — it exits 0 immediately when `~/.trinity/read-only-config.json` is absent or has `enabled: false`. The config file is the on/off switch.

### Config File Layout

`~/.trinity/read-only-config.json` (written by platform, protected by `path_deny` and `bash_deny`):

```json
{
  "enabled": true,
  "blocked_patterns": ["*.py", "*.js", "CLAUDE.md", "..."],
  "allowed_patterns": ["content/*", "output/*", "*.log", "..."]
}
```

### Guardrail Protections (docker/base-image/hooks/guardrails-baseline.json)

Two protections prevent the agent from disabling read-only mode via the config file:

**`path_deny`** — blocks Write/Edit/NotebookEdit/MultiEdit tools from directly writing the file:
```json
"/home/developer/.trinity/read-only-config.json"
```

**`bash_deny`** — blocks shell redirects/pipes targeting the config file:
```json
{
  "pattern": "(\\.trinity/read-only-config\\.json).*[>|]|[>|].*(\\.trinity/read-only-config\\.json)",
  "reason": "modifying read-only mode configuration"
}
```

Both are enforced by `file-guardrail.py` and `bash-guardrail.py` respectively, which always run independently of read-only mode state (GUARD-001/002).

---

## Data Flow Diagram

```
User clicks ReadOnlyToggle
        │
        ▼
AgentDetail.vue:toggleReadOnly()
        │
        ▼
PUT /api/agents/{name}/read-only
        │
        ▼
routers/agents.py:set_agent_read_only_status()
        │
        ▼
services/agent_service/read_only.py:set_read_only_status_logic()
        │
        ├─► db.set_read_only_mode() - Save to SQLite
        │
        └─► If running:
              ├─► enabled: inject_read_only_hooks() → write ~/.trinity/read-only-config.json (1 file)
              └─► disabled: remove_read_only_hooks() → write {"enabled": false} + cleanup legacy settings
```

**On Agent Start (always syncs both paths):**

```
lifecycle.py:start_agent_internal()
        │
        ▼
db.get_read_only_mode()
        │
        ├─► enabled → inject_read_only_hooks() → write ~/.trinity/read-only-config.json
        └─► disabled → remove_read_only_hooks() → write {"enabled": false}
                                                        + _remove_legacy_settings_hook()
```

**During Claude Code Operation:**

```
Claude Code: Write/Edit/NotebookEdit/MultiEdit
        │
        ▼
PreToolUse hook triggered (registered in ~/.claude/settings.json, always active)
        │
        ├─► file-guardrail.py (runs first — blocks path_deny including config file)
        │
        └─► read-only-guard.py
                │
                ▼
        Load ~/.trinity/read-only-config.json
                │
                ├─► Missing or enabled:false → Exit 0 (allow)
                │
                └─► Enabled:
                        │
                        ├─► MultiEdit: check each edits[].file_path
                        └─► Single file: check file_path / notebook_path
                                │
                                ├─► Allowed pattern match → Exit 0 (allow)
                                ├─► Blocked pattern match → Exit 2 + stderr (block)
                                └─► No match → Exit 0 (allow)
```

---

## Error Handling

| Error Case | HTTP Status | Message |
|------------|-------------|---------|
| Agent not found | 404 | Agent not found |
| Not owner | 403 | Only the owner can modify read-only settings |
| System agent | 403 | Cannot modify read-only mode for system agent |
| Missing enabled field | 400 | enabled is required |
| Invalid config type | 400 | config must be an object |
| Invalid patterns | 400 | blocked_patterns/allowed_patterns must be a list |
| Hook injection failed | 200 | Returns `hooks_injected: false` with message |

---

## Security Considerations

1. **Owner-only access**: Only the agent owner (checked via `can_user_share_agent`) can modify read-only settings
2. **System agent protection**: Read-only mode cannot be enabled for the system agent (`trinity-system`)
3. **Fail-closed guard**: Guard script wrapped by `run_hook(main)` from `lib.py` — any uncaught exception exits 2 (deny). Pre-#887, exceptions exited 0 (allow).
4. **Allowed takes precedence**: Even if a file matches blocked patterns, allowed patterns override
5. **Normalized paths**: Guard uses `fnmatch` against the basename and the absolute path; relative paths are resolved before pattern matching
6. **Guard script tamperproof** (GUARD-001/002): Script lives at `/opt/trinity/hooks/read-only-guard.py` (root-owned 0555). Agent cannot overwrite it via Write tool — path is in `path_deny` (`/opt/trinity/*`).
7. **Config file protected**: `path_deny` blocks direct writes to `/home/developer/.trinity/read-only-config.json`; `bash_deny` blocks shell redirect/pipe patterns targeting the same file (GUARD-001). Both enforced independently of read-only mode state.
8. **Hook always registered**: Hook registered in base image `~/.claude/settings.json`. Agent cannot remove it because `~/.claude/settings.json` is in `path_deny` in `guardrails-baseline.json`.
9. **MultiEdit covered**: Guard iterates `edits[]` array — bulk writes to blocked files are denied. Pre-#887, MultiEdit was a bypass vector because the old guard only checked top-level `file_path`.

---

## API Response Examples

### GET /api/agents/{name}/read-only

```json
{
  "agent_name": "my-agent",
  "enabled": true,
  "config": {
    "blocked_patterns": ["*.py", "*.js", "CLAUDE.md", ...],
    "allowed_patterns": ["content/*", "output/*", "*.log", ...]
  }
}
```

### PUT /api/agents/{name}/read-only

**Request:**
```json
{
  "enabled": true,
  "config": null  // Use defaults
}
```

**Response:**
```json
{
  "status": "updated",
  "agent_name": "my-agent",
  "enabled": true,
  "config": {
    "blocked_patterns": [...],
    "allowed_patterns": [...]
  },
  "hooks_injected": true,
  "message": "Read-only mode enabled."
}
```

---

## Testing

### Prerequisites
- Trinity backend running
- At least one agent created and owned by test user
- Agent running (for immediate hook injection test)

### Unit Tests

`tests/unit/test_read_only_guard.py` — 18 tests, all passing.

| Class | Tests |
|-------|-------|
| `TestGuardDisabledConfig` | No config file allows; `enabled:false` allows; missing `enabled` field allows |
| `TestGuardBlockedPaths` | `.py` denied; `.js` denied; `CLAUDE.md` denied; `.claude/*` denied; relative path blocked; `notebook_path` blocked |
| `TestGuardAllowedPaths` | Allowed pattern overrides blocked; unblocked path allowed; empty path allowed; missing `file_path` key allowed |
| `TestGuardMultiEdit` | All allowed passes; one blocked edit denied; all blocked denied; empty edits allowed; edit missing `file_path` key skipped |

### Integration Test Steps

1. **Enable via UI**
   - Navigate to agent detail page
   - Click ReadOnlyToggle (should show "Editable")
   - Toggle should turn rose/red with lock icon
   - Notification: "Read-only mode enabled"

2. **Verify Config Written (Running Agent)**
   - SSH into agent container
   - Check `~/.trinity/read-only-config.json` exists with `"enabled": true`
   - Confirm `~/.trinity/hooks/` does NOT have `read-only-guard.py` (it's in `/opt/trinity/hooks/`)
   - Confirm `~/.claude/settings.json` has the `read-only-guard.py` hook entry (base image)

3. **Test File Protection**
   - In agent terminal, ask agent to create a Python file
   - Agent should receive: "read-only mode: blocked: …" stderr message

4. **Test MultiEdit Protection**
   - Ask agent to make edits across multiple `.py` files in one operation
   - Should be denied (MultiEdit now covered)

5. **Test Allowed Patterns**
   - In agent terminal, create file in `content/` directory
   - Should succeed (allowed pattern)

6. **Disable via UI**
   - Click ReadOnlyToggle again
   - Should return to gray "Editable" state
   - `~/.trinity/read-only-config.json` now contains `{"enabled": false}`
   - Notification: "Read-only mode disabled"

7. **Restart Sync**
   - Enable read-only, stop agent, disable while stopped, start agent
   - Verify `~/.trinity/read-only-config.json` is written with `enabled: false` on start (stale-config fix)

### Edge Cases
- Enable on stopped agent: config written on next start
- System agent: toggle should not appear
- Non-owner user: toggle should not appear (requires `can_share`)
- Legacy agent (pre-#887): `_remove_legacy_settings_hook()` strips old `settings.local.json` entry on next disable or start

---

## Related Flows

- **Upstream**: [agent-lifecycle.md](agent-lifecycle.md) - Hook injection on agent start
- **Related**: [container-capabilities.md](container-capabilities.md) - Similar per-agent security setting
- **Related**: [autonomy-mode.md](autonomy-mode.md) - Similar toggle pattern in AgentHeader

---

## Agents Page Integration

### Entry Point

`src/frontend/src/views/Agents.vue:248-255` - ReadOnlyToggle between Running and Autonomy toggles

### Template Structure

```vue
<!-- Running, Read-Only, and Autonomy toggles (same row) -->
<div class="flex items-center justify-between mb-2">
  <RunningStateToggle ... />
  <ReadOnlyToggle
    v-if="!agent.is_system && !agent.is_shared"
    :model-value="getAgentReadOnlyState(agent.name)"
    :loading="readOnlyLoading === agent.name"
    size="sm"
    @toggle="handleReadOnlyToggle(agent)"
  />
  <AutonomyToggle ... />
</div>
```

**Note (2026-02-18)**: ReadOnlyToggle now shows labels (`:show-label` removed) for consistency with other toggles. All toggles on this row use `size="sm"`.

### State Management

| Line | Element | Description |
|------|---------|-------------|
| 377 | `readOnlyLoading` | Ref tracking which agent's toggle is loading |
| 378 | `agentReadOnlyStates` | Map of agent_name -> boolean read-only state |
| 444 | onMounted | Calls `fetchAllReadOnlyStates()` |

### Functions

| Line | Function | Description |
|------|----------|-------------|
| 540-542 | `getAgentReadOnlyState(agentName)` | Returns boolean from `agentReadOnlyStates` map |
| 544-563 | `fetchAllReadOnlyStates()` | Parallel fetch of read-only status for all owned agents |
| 565-594 | `handleReadOnlyToggle(agent)` | Toggles read-only mode via PUT API, updates local state |

### Visibility Conditions

ReadOnlyToggle only shown when:
- `!agent.is_system` - Not the system agent
- `!agent.is_shared` - User owns the agent (not viewing someone else's shared agent)

### Data Flow

```
User clicks ReadOnlyToggle on agent card
        |
        v
handleReadOnlyToggle(agent) [line 565]
        |
        +-- Set readOnlyLoading = agent.name
        |
        v
PUT /api/agents/{name}/read-only
        |
        +-- Success: Update agentReadOnlyStates[agent.name]
        |            Show notification
        |
        +-- Error: Show error notification
        |
        v
Finally: readOnlyLoading = null
```

---

## Revision History

| Date | Change |
|------|--------|
| 2026-05-18 | **#887 — Guard moved to base image**: Guard script baked into base image at `/opt/trinity/hooks/read-only-guard.py` (root-owned 0555). Hook registered permanently in `~/.claude/settings.json` via `claude-settings.json`. `inject_read_only_hooks()` now writes one file only (config JSON). `remove_read_only_hooks()` writes `{"enabled": false}` + migration cleanup via `_remove_legacy_settings_hook()`. `lifecycle.py` always syncs config on every start (fixes stale-config-on-volume bug). Added `MultiEdit` coverage. Added `path_deny` and `bash_deny` protections in `guardrails-baseline.json`. Added 18 unit tests in `tests/unit/test_read_only_guard.py`. |
| 2026-02-18 17:50 | **Toggle Consistency Fix**: Removed `:show-label="false"` from ReadOnlyToggle in Agents.vue - it now shows labels like the other toggles. All toggles (Running, ReadOnly, Autonomy) now use consistent `size="sm"` across both Agents.vue and AgentHeader.vue. |
| 2026-02-18 | **Agents Page Integration**: Added ReadOnlyToggle to Agents.vue card tiles (lines 248-255). Shows for owned agents (not system, not shared). Added `agentReadOnlyStates` state (line 378), `readOnlyLoading` (line 377), `fetchAllReadOnlyStates()` (lines 544-563), `handleReadOnlyToggle()` (lines 565-594). Toggle positioned between Running and Autonomy toggles in same row. |
| 2026-02-17 | Initial documentation (CFG-007) |
