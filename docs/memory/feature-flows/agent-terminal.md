# Feature: Web Terminal for Agents

> **Updated**: 2025-12-27 - Refactored to service layer architecture. Terminal session management moved to `services/agent_service/terminal.py`.

## Overview

Browser-based interactive terminal for any agent using xterm.js with full Claude Code TUI support. The terminal replaces the chat tab on the Agent Detail page and provides direct terminal access to agent containers via WebSocket-based PTY forwarding. **Includes per-agent API key control** (Req 11.7) allowing owners to choose between platform API key or terminal-based authentication.

## User Story

As a **user**, I want to access an interactive terminal for my agents directly from my browser so that I can run Claude Code or bash commands without SSH port exposure.

As an **agent owner**, I want to control whether my agent uses the platform API key or requires terminal authentication so that I can manage Claude API costs and subscription usage independently per agent.

## Entry Points

- **UI**: `src/frontend/src/views/AgentDetail.vue:354-460` - Terminal tab (auto-connects when agent running)
- **UI**: `src/frontend/src/views/AgentDetail.vue:415-449` - API key authentication toggle (when agent stopped)
- **API**: `GET /api/agents/{agent_name}/api-key-setting` - Get current API key setting
- **API**: `PUT /api/agents/{agent_name}/api-key-setting` - Update API key setting (owner only)
- **API**: `WS /api/agents/{agent_name}/terminal?mode=claude|bash` - Terminal WebSocket connection

---

## Frontend Layer

### Components

#### AgentDetail.vue (Parent)
- **File**: `src/frontend/src/views/AgentDetail.vue`
- **Terminal Tab**: Lines 354-460 - Terminal embedded in tab panel with fullscreen support
- **State**:
  - `isTerminalFullscreen` ref controls fullscreen mode
  - `terminalRef` ref for calling terminal methods
  - `apiKeySetting` ref for API key authentication mode
  - `apiKeySettingLoading` ref for loading state
- **Event Handlers**:
  - `onTerminalConnected()` - Shows success notification
  - `onTerminalDisconnected()` - Shows info notification
  - `onTerminalError()` - Shows error notification
  - `toggleTerminalFullscreen()` - Toggles fullscreen mode, refits terminal
  - `handleTerminalKeydown()` - ESC key exits fullscreen
  - `loadApiKeySetting()` - Fetches current API key setting
  - `updateApiKeySetting(usePlatformKey)` - Updates API key setting

**API Key Authentication Toggle** (Lines 415-449):
When agent is stopped, the Terminal tab shows an authentication toggle (owner-only control):
- **Use Platform API Key** (default): Agent uses Trinity's configured Anthropic API key
  - Platform key injected as `ANTHROPIC_API_KEY` environment variable
  - Claude commands work immediately without authentication
  - Usage counts against Trinity's subscription
- **Authenticate in Terminal**: No API key injected; user runs `claude login` in terminal
  - Agent container starts without `ANTHROPIC_API_KEY` environment variable
  - User must run `claude login` in terminal to authenticate with their own subscription
  - Usage counts against user's personal Claude subscription

```vue
<div class="space-y-2">
  <label>
    <input type="radio" :checked="apiKeySetting.use_platform_api_key" @change="updateApiKeySetting(true)" />
    Use Platform API Key
    <p class="text-xs text-gray-500">Claude uses Trinity's configured Anthropic API key</p>
  </label>
  <label>
    <input type="radio" :checked="!apiKeySetting.use_platform_api_key" @change="updateApiKeySetting(false)" />
    Authenticate in Terminal
    <p class="text-xs text-gray-500">Run "claude login" after starting to use your own subscription</p>
  </label>
</div>
```

**Important**: Changing this setting requires agent restart. Container is recreated with/without `ANTHROPIC_API_KEY` env var. The UI shows `restart_required: true` badge when setting changed but not yet applied.

**Data Flow**:
1. Agent owner toggles radio button
2. `updateApiKeySetting(usePlatformKey)` calls `PUT /api/agents/{agent_name}/api-key-setting`
3. Backend updates `agent_api_key_settings` table
4. Returns `{restart_required: true, message: "Setting updated. Restart the agent to apply changes."}`
5. Owner restarts agent
6. `start_agent()` reads setting and conditionally injects `ANTHROPIC_API_KEY`

#### AgentTerminal.vue (Terminal Component)
- **File**: `src/frontend/src/components/AgentTerminal.vue`
- **Props**:
  - `agentName: String` (required) - Name of the agent to connect to
  - `autoConnect: Boolean` (default: true) - Auto-connect on mount
  - `showFullscreenToggle: Boolean` (default: false) - Show fullscreen button
  - `isFullscreen: Boolean` (default: false) - Current fullscreen state
- **Emits**: `connected`, `disconnected`, `error`, `toggle-fullscreen`
- **Exposes**: `connect()`, `disconnect()`, `focus()`, `fit()`

**Key References**:
| Ref | Type | Purpose |
|-----|------|---------|
| `terminalContainer` | HTMLElement | xterm.js mount point |
| `selectedMode` | 'claude' \| 'bash' | Terminal mode |
| `connectionStatus` | 'connected' \| 'connecting' \| 'disconnected' | WebSocket state |
| `errorMessage` | string | Error display |

**WebSocket Connection**:
```javascript
// Build WebSocket URL with agent name
const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
const wsUrl = `${protocol}//${location.host}/api/agents/${encodeURIComponent(props.agentName)}/terminal?mode=${selectedMode.value}`

ws = new WebSocket(wsUrl)
ws.binaryType = 'arraybuffer'

// First message is JWT auth
ws.onopen = () => {
  ws.send(JSON.stringify({ type: 'auth', token }))
}
```

**Message Handling**:
| Message Type | Direction | Content |
|--------------|-----------|---------|
| `auth` | Client -> Server | `{type: "auth", token: "<jwt>"}` |
| `auth_success` | Server -> Client | `{type: "auth_success"}` |
| `error` | Server -> Client | `{type: "error", message: "...", close?: true}` |
| `resize` | Client -> Server | `{type: "resize", cols: 80, rows: 24}` |
| Binary | Both | Raw terminal I/O |

### Dependencies

```json
{
  "@xterm/xterm": "^5.5.0",
  "@xterm/addon-fit": "^0.10.0",
  "@xterm/addon-web-links": "^0.11.0"
}
```

---

## Backend Layer

### Architecture (Post-Refactoring)

The terminal feature uses a **thin router + service layer** architecture:

| Layer | File | Purpose |
|-------|------|---------|
| Router | `src/backend/routers/agents.py:773-785` | WebSocket endpoint definition |
| Service | `src/backend/services/agent_service/terminal.py` (346 lines) | Session management, PTY handling |
| Service | `src/backend/services/agent_service/api_key.py` (97 lines) | API key setting logic |

### API Key Setting Endpoints

- **Router**: `src/backend/routers/agents.py:748-766`
- **Service**: `src/backend/services/agent_service/api_key.py`

#### GET `/api/agents/{agent_name}/api-key-setting`
Returns current API key authentication setting for an agent.

**Authentication**: Any user with agent access (owner, shared, or admin)

```python
@router.get("/{agent_name}/api-key-setting")
async def get_agent_api_key_setting(agent_name: str, ...) -> dict:
    """
    Get the API key setting for an agent.

    Returns whether the agent uses the platform API key or relies on
    terminal-based authentication.
    """
    if not db.can_user_access_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="You don't have permission to access this agent")

    container = get_agent_container(agent_name)
    use_platform_key = db.get_use_platform_api_key(agent_name)

    # Check if current container matches the setting
    container.reload()
    env_matches = _check_api_key_env_matches(container, agent_name)

    return {
        "agent_name": agent_name,
        "use_platform_api_key": use_platform_key,
        "restart_required": not env_matches,
        "status": container.status
    }
```

**Response Fields**:
- `use_platform_api_key`: Boolean - Current setting from database
- `restart_required`: Boolean - True if container env doesn't match setting (needs restart)
- `status`: String - Container status ("running", "exited", etc.)

#### PUT `/api/agents/{agent_name}/api-key-setting`
Updates the API key setting. **Owner-only endpoint** - only the agent owner can modify this setting.

**Authentication**: Must be agent owner (checked via `db.can_user_share_agent()`)

```python
@router.put("/{agent_name}/api-key-setting")
async def update_agent_api_key_setting(agent_name: str, body: dict, ...) -> dict:
    """
    Update the API key setting for an agent.

    Body:
    - use_platform_api_key: True to use Trinity's platform key, False to require terminal auth

    Note: Changes require agent restart to take effect.
    """
    # Only owner can modify this setting
    if not db.can_user_share_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="Only the owner can modify API key settings")

    use_platform_key = body.get("use_platform_api_key")
    if use_platform_key is None:
        raise HTTPException(status_code=400, detail="use_platform_api_key is required")

    # Update setting in database
    db.set_use_platform_api_key(agent_name, bool(use_platform_key))

    await log_audit_event(
        event_type="agent_settings",
        action="update_api_key_setting",
        user_id=current_user.username,
        agent_name=agent_name,
        details={"use_platform_api_key": use_platform_key}
    )

    return {
        "status": "updated",
        "use_platform_api_key": use_platform_key,
        "restart_required": True,
        "message": "Setting updated. Restart the agent to apply changes."
    }
```

**Request Body**:
```json
{
  "use_platform_api_key": true
}
```

**Response**:
```json
{
  "status": "updated",
  "agent_name": "my-agent",
  "use_platform_api_key": true,
  "restart_required": true,
  "message": "Setting updated. Restart the agent to apply changes."
}
```

### WebSocket Endpoint

- **Router**: `src/backend/routers/agents.py:773-785`
- **Service**: `src/backend/services/agent_service/terminal.py`
- **Query Params**: `mode: str` (default: "claude") - Either "claude" or "bash"

```python
@router.websocket("/{agent_name}/terminal")
async def agent_terminal(
    websocket: WebSocket,
    agent_name: str,
    mode: str = Query(default="claude")
):
    """Interactive terminal WebSocket for any agent."""
    await _terminal_manager.handle_terminal_session(
        websocket=websocket,
        agent_name=agent_name,
        mode=mode,
        decode_token_fn=decode_token
    )
```

### TerminalSessionManager Class (`services/agent_service/terminal.py:22-346`)

The terminal session management is encapsulated in the `TerminalSessionManager` class:

```python
class TerminalSessionManager:
    """
    Manages terminal sessions for agents.
    Limits sessions to 1 per user per agent and handles stale session cleanup.
    """
    def __init__(self):
        self._active_sessions: dict = {}  # (user_id, agent_name) -> session_info
        self._lock = threading.Lock()
```

**Key Methods:**
- `_check_and_register_session()` (line 34-51): Check/register session with 5-minute timeout
- `_unregister_session()` (line 53-57): Remove session from tracking
- `handle_terminal_session()` (line 59-346): Main WebSocket handler

### Authentication Flow (terminal.py:83-147)

```python
# Step 1: Wait for auth message (10 second timeout)
auth_msg = await asyncio.wait_for(websocket.receive_text(), timeout=10.0)
auth_data = json.loads(auth_msg)

if auth_data.get("type") != "auth":
    # Send error, close connection
    return

token = auth_data.get("token")

# Step 2: Decode and validate JWT
user = decode_token_fn(token)

# Step 3: Check access to agent
if not db.can_user_access_agent(user_email, agent_name) and user_role != "admin":
    # Send error with code 4003
    return
```

### Session Limiting (terminal.py:34-57)

```python
def _check_and_register_session(self, user_email: str, agent_name: str, timeout_seconds: int = 300) -> bool:
    """Check if a session can be created and register it. Returns True if registered."""
    session_key = (user_email, agent_name)
    with self._lock:
        if session_key in self._active_sessions:
            session_info = self._active_sessions[session_key]
            session_age = (datetime.utcnow() - session_info["started_at"]).total_seconds()
            if session_age < timeout_seconds:
                return False  # Session limit reached
            else:
                # Stale session, clean it up
                logger.warning(f"Cleaning up stale session for {user_email}@{agent_name}")
        self._active_sessions[session_key] = {"started_at": datetime.utcnow()}
        return True
```

### Docker Exec Creation (terminal.py:193-215)

```python
# Build command based on mode
cmd = ["claude"] if mode == "claude" else ["/bin/bash"]

# Create exec instance with TTY
exec_instance = docker_client.api.exec_create(
    container.id,
    cmd,
    stdin=True,
    tty=True,
    stdout=True,
    stderr=True,
    user="developer",
    workdir="/home/developer",
    environment={"TERM": "xterm-256color", "COLORTERM": "truecolor"}
)
exec_id = exec_instance["Id"]

# Start exec and get raw socket
exec_output = docker_client.api.exec_start(exec_id, socket=True, tty=True)
docker_socket = exec_output._sock
docker_socket.setblocking(False)
```

**Note**: The `ANTHROPIC_API_KEY` environment variable is set during container creation, not in the exec environment. The API key setting determines whether this env var is injected when the container starts.

---

## Data Layer

### Database Schema

**Table**: `agent_api_key_settings`

| Column | Type | Description |
|--------|------|-------------|
| `agent_name` | TEXT PRIMARY KEY | Agent identifier |
| `use_platform_api_key` | INTEGER | 1 = use platform key, 0 = terminal auth |
| `updated_at` | TEXT | ISO timestamp of last update |

**Default**: New agents default to `use_platform_api_key = 1` (True)

### Database Operations

**File**: `src/backend/database.py`

| Method | Purpose |
|--------|---------|
| `get_use_platform_api_key(agent_name)` | Returns boolean - whether agent uses platform key |
| `set_use_platform_api_key(agent_name, use_platform)` | Updates setting, creates record if not exists |

**Implementation**:
```python
def get_use_platform_api_key(self, agent_name: str) -> bool:
    """Get API key setting for agent. Defaults to True if not set."""
    result = self.execute_query(
        "SELECT use_platform_api_key FROM agent_api_key_settings WHERE agent_name = ?",
        (agent_name,)
    )
    return bool(result[0][0]) if result else True

def set_use_platform_api_key(self, agent_name: str, use_platform: bool):
    """Set API key setting for agent."""
    self.execute_query("""
        INSERT INTO agent_api_key_settings (agent_name, use_platform_api_key, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(agent_name) DO UPDATE SET
            use_platform_api_key = excluded.use_platform_api_key,
            updated_at = excluded.updated_at
    """, (agent_name, 1 if use_platform else 0, datetime.utcnow().isoformat()))
```

---

## Side Effects

### Audit Logging

**API Key Setting Update**:
```python
await log_audit_event(
    event_type="agent_settings",
    action="update_api_key_setting",
    user_id=current_user.username,
    agent_name=agent_name,
    details={"use_platform_api_key": use_platform_key}
)
```

**Terminal Session Start**:
```python
await log_audit_event(
    event_type="terminal_session",
    action="start",
    user_id=user_email,
    agent_name=agent_name,
    details={"mode": mode}
)
```

**Terminal Session End**:
```python
session_duration = (datetime.utcnow() - session_start).total_seconds()

await log_audit_event(
    event_type="terminal_session",
    action="end",
    user_id=user_email,
    agent_name=agent_name,
    details={
        "mode": mode,
        "duration_seconds": session_duration
    }
)
```

### Container Recreation

When API key setting is changed and agent is restarted:
1. `start_agent()` reads `use_platform_api_key` setting from database
2. If `True`: Injects `ANTHROPIC_API_KEY` environment variable from platform credentials
3. If `False`: Container starts without `ANTHROPIC_API_KEY`, user must run `claude login`
4. Container is recreated with new environment configuration

---

## Error Handling

### Terminal WebSocket Errors

| Error Case | WebSocket Code | Message |
|------------|----------------|---------|
| Missing auth message | 4001 | "Expected auth message" |
| Missing token | 4001 | "Token required" |
| Invalid token | 4001 | "Invalid token" |
| No agent access | 4003 | "You don't have access to this agent" |
| Auth timeout (10s) | 4001 | "Authentication timeout" |
| Invalid auth JSON | 4001 | "Invalid auth format" |
| Existing session | 4002 | "You already have an active terminal session for this agent" |
| Container not found | 4004 | "Agent '{name}' container not found" |
| Container not running | 4004 | "Agent '{name}' is not running" |

### API Key Setting Errors

| Error Case | HTTP Status | Message |
|------------|-------------|---------|
| Not agent owner (PUT) | 403 | "Only the owner can modify API key settings" |
| No agent access (GET) | 403 | "You don't have permission to access this agent" |
| Agent not found | 404 | "Agent not found" |
| Missing body field | 400 | "use_platform_api_key is required" |

---

## Security Considerations

1. **Access Control**: User must have access to agent (owner, shared, or admin)
2. **Session Limit**: One terminal per user per agent prevents resource exhaustion
3. **Stale Session Cleanup**: 5-minute timeout prevents orphaned session blocking
4. **No SSH Exposure**: Uses internal Docker network, no port forwarding to host
5. **Audit Trail**: Full session logging with duration tracking
6. **Binary Transport**: Raw PTY data sent as binary WebSocket frames
7. **TERM Environment**: Sets `TERM=xterm-256color` for proper TUI rendering

---

## Data Flow Diagram

```
User Click          WebSocket          Backend           Docker
    |                   |                 |                 |
    | Click Terminal    |                 |                 |
    |------------------>|                 |                 |
    |                   | WS Connect      |                 |
    |                   |---------------->|                 |
    |                   |                 | Accept          |
    |                   |<----------------|                 |
    |                   | Auth Message    |                 |
    |                   |---------------->|                 |
    |                   |                 | decode_token()  |
    |                   |                 | Check access    |
    |                   |                 | Session limit   |
    |                   |                 |                 |
    |                   |                 | exec_create()   |
    |                   |                 |---------------->|
    |                   |                 | exec_start()    |
    |                   |                 |---------------->|
    |                   |                 |<----------------|
    |                   | auth_success    |                 |
    |                   |<----------------|                 |
    |                   |                 |                 |
    | Type command      |                 |                 |
    |------------------>| Text/Binary     |                 |
    |                   |---------------->|                 |
    |                   |                 | socket.send()   |
    |                   |                 |---------------->|
    |                   |                 |<----------------| Output
    |                   | Binary          |                 |
    |                   |<----------------|                 |
    | xterm.write()     |                 |                 |
    |<------------------|                 |                 |
```

---

## Related Flows

- **Related**: [web-terminal.md](web-terminal.md) - System Agent terminal (similar implementation)
- **Upstream**: [agent-lifecycle.md](agent-lifecycle.md) - Agent must be running
- **Upstream**: [auth0-authentication.md](auth0-authentication.md) - JWT token creation
- **Replaces**: [agent-chat.md](agent-chat.md) - Previous chat-based interaction (now deprecated)

---

## Testing

### Prerequisites
- Backend and frontend running
- User logged in with access to an agent
- Agent container running

### Test Steps

1. **Agent Owner Access**
   - Action: Navigate to agent detail page for an agent you own
   - Expected: Terminal tab visible, auto-connects when agent running
   - Verify: Terminal shows "Connected!" message

2. **Terminal Auto-Connection**
   - Action: Navigate to agent detail page with agent running
   - Expected: Terminal shows "Connecting..." then "Connected!" automatically
   - Verify: Green status dot, connection status text

3. **Fullscreen Toggle**
   - Action: Click the fullscreen button in terminal header
   - Expected: Terminal expands to fill entire viewport
   - Verify: Terminal refits to new size, content preserved

4. **ESC to Exit Fullscreen**
   - Action: Press ESC key while in fullscreen mode
   - Expected: Terminal returns to embedded panel size
   - Verify: Layout returns to normal, terminal refits

5. **Claude Code Mode (Default)**
   - Action: Type `/help` and press Enter
   - Expected: Claude Code help menu displays with colors
   - Verify: Full TUI preserved, colors render correctly

6. **Bash Mode**
   - Action: Disconnect, switch to Bash mode, reconnect
   - Expected: Bash shell prompt appears
   - Verify: Can run `ls`, `pwd`, `whoami` commands

7. **Shared Agent Access**
   - Action: Navigate to an agent shared with you by another user
   - Expected: Terminal tab visible and functional
   - Verify: Can connect and execute commands

8. **Session Limit**
   - Action: Open second browser tab, try to connect to same agent
   - Expected: Error "You already have an active terminal session for this agent"
   - Verify: First session remains connected

9. **Stopped Agent**
   - Action: Navigate to agent detail page with agent stopped
   - Expected: Terminal shows "Agent is not running" with Start button
   - Verify: Clicking Start starts agent, terminal then auto-connects

10. **API Key Setting Toggle (Owner Only)**
   - Action: As agent owner, navigate to terminal tab with agent stopped
   - Expected: See "Authentication" section with two radio buttons
   - Verify: Can toggle between "Use Platform API Key" and "Authenticate in Terminal"

11. **Change API Key Setting**
   - Action: Toggle "Authenticate in Terminal" radio button
   - Expected: Setting updates, shows "Restart required" badge
   - Verify: API call to `PUT /api/agents/{name}/api-key-setting` succeeds

12. **Restart with Terminal Auth**
   - Action: Start agent after changing to "Authenticate in Terminal"
   - Expected: Agent starts, terminal connects
   - Action: Type `claude` in terminal
   - Expected: Prompt to run `claude login` (no API key injected)
   - Verify: Container has no `ANTHROPIC_API_KEY` environment variable

13. **Restart with Platform Key**
   - Action: Stop agent, toggle back to "Use Platform API Key", restart
   - Expected: Agent starts, Claude commands work immediately
   - Verify: Container has `ANTHROPIC_API_KEY` environment variable set

14. **Non-Owner Cannot Change Setting**
   - Action: As non-owner user with shared access, view terminal tab
   - Expected: API key setting UI not visible (agent.can_share is false for non-owners)
   - Action: Attempt direct API call `PUT /api/agents/{name}/api-key-setting`
   - Expected: 403 "Only the owner can modify API key settings"

### Edge Cases

- **No Access**: User without agent access sees 403 error on terminal connect
- **Network Interruption**: Terminal shows disconnection, allows reconnect via Connect button
- **Token Expiry**: Auth error displayed, prompt to refresh page
- **Agent Deleted**: Container not found error when agent is deleted mid-session
- **Setting Changed While Running**: `restart_required: true` in GET response until agent restarted
- **Shared User Views Setting**: Non-owner sees current setting but cannot modify (UI hidden)

### Cleanup
No cleanup required - sessions terminate automatically on disconnect.

### Status
**Testing Status**: ✅ Tested (as of 2025-12-26)

**Verified Features**:
- Terminal connection and PTY forwarding
- Claude Code TUI rendering
- Fullscreen mode toggle
- Session limiting (1 per user per agent)
- Access control (owner, shared, admin)
- **NEW**: Per-agent API key control toggle (Req 11.7)
- **NEW**: Owner-only API key setting modification
- **NEW**: Container recreation on API key setting change

---

## Changelog

| Date | Change |
|------|--------|
| 2025-12-27 | **Service layer refactoring**: Terminal session management moved to `services/agent_service/terminal.py`. API key logic moved to `api_key.py`. Router reduced to thin endpoint definitions. |
| 2025-12-25 | Initial implementation - replaced Chat tab with Terminal tab |
| 2025-12-25 | Added fullscreen toggle with ESC key to exit |
| 2025-12-26 | Added per-agent API key control (Req 11.7) - owner can choose platform key vs terminal auth |
| 2025-12-26 | Updated flow documentation with API key setting endpoints and data layer |
