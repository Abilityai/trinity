# Feature: Web Terminal for Agents

## Overview

Browser-based interactive terminal for any agent using xterm.js with full Claude Code TUI support. The terminal replaces the chat tab on the Agent Detail page and provides direct terminal access to agent containers via WebSocket-based PTY forwarding.

## User Story

As a **user**, I want to access an interactive terminal for my agents directly from my browser so that I can run Claude Code or bash commands without SSH port exposure.

## Entry Points

- **UI**: `src/frontend/src/views/AgentDetail.vue:354-425` - Terminal tab (auto-connects when agent running)
- **API**: `WS /api/agents/{agent_name}/terminal?mode=claude|bash`

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
When agent is stopped, the Terminal tab shows an authentication toggle:
- **Use Platform API Key** (default): Agent uses Trinity's configured Anthropic API key
- **Authenticate in Terminal**: No API key injected; user runs `claude login` in terminal

```vue
<div class="space-y-2">
  <label>
    <input type="radio" :checked="apiKeySetting.use_platform_api_key" @change="updateApiKeySetting(true)" />
    Use Platform API Key
  </label>
  <label>
    <input type="radio" :checked="!apiKeySetting.use_platform_api_key" @change="updateApiKeySetting(false)" />
    Authenticate in Terminal
  </label>
</div>
```

Changing this setting requires agent restart. Container is recreated with/without `ANTHROPIC_API_KEY` env var.

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

### API Key Setting Endpoints

- **File**: `src/backend/routers/agents.py:2536-2617`

#### GET `/api/agents/{agent_name}/api-key-setting`
Returns current API key authentication setting for an agent.

```python
@router.get("/{agent_name}/api-key-setting")
async def get_agent_api_key_setting(agent_name: str, ...) -> dict:
    return {
        "agent_name": agent_name,
        "use_platform_api_key": True,  # or False
        "restart_required": False,     # True if setting changed but not applied
        "status": "running"            # Container status
    }
```

#### PUT `/api/agents/{agent_name}/api-key-setting`
Updates the API key setting. Only agent owner can modify.

```python
@router.put("/{agent_name}/api-key-setting")
async def update_agent_api_key_setting(agent_name: str, body: dict, ...) -> dict:
    # body: {"use_platform_api_key": true|false}
    db.set_use_platform_api_key(agent_name, body["use_platform_api_key"])
    return {
        "status": "updated",
        "use_platform_api_key": body["use_platform_api_key"],
        "restart_required": True,
        "message": "Setting updated. Restart the agent to apply changes."
    }
```

### WebSocket Endpoint

- **File**: `src/backend/routers/agents.py:2624-2900`
- **Endpoint**: `@router.websocket("/{agent_name}/terminal")`
- **Query Params**: `mode: str` (default: "claude") - Either "claude" or "bash"

### Authentication Flow

```python
# Step 1: Wait for auth message (10 second timeout)
auth_msg = await asyncio.wait_for(websocket.receive_text(), timeout=10.0)
auth_data = json.loads(auth_msg)

if auth_data.get("type") != "auth":
    # Send error, close connection
    return

token = auth_data.get("token")

# Step 2: Decode and validate JWT
user = decode_token(token)  # From dependencies.py

# Step 3: Check access to agent
if not db.can_user_access_agent(user_email, agent_name) and user_role != "admin":
    # Send error with code 4003
    return
```

### Session Limiting

```python
# Track active sessions (1 per user per agent, 5-minute stale cleanup)
_active_terminal_sessions: dict = {}  # (user_id, agent_name) -> session_info
_terminal_lock = threading.Lock()
SESSION_TIMEOUT_SECONDS = 300

with _terminal_lock:
    session_key = (user_email, agent_name)
    if session_key in _active_terminal_sessions:
        session_info = _active_terminal_sessions[session_key]
        session_age = (datetime.utcnow() - session_info["started_at"]).total_seconds()
        if session_age < SESSION_TIMEOUT_SECONDS:
            # Reject: existing active session
            return
        else:
            # Clean up stale session
            logger.warning(f"Cleaning up stale session for {user_email}@{agent_name}")
    _active_terminal_sessions[session_key] = {"started_at": datetime.utcnow()}
```

### Docker Exec Creation

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

---

## Side Effects

### Audit Logging

Session start:
```python
await log_audit_event(
    event_type="terminal_session",
    action="start",
    user_id=user_email,
    agent_name=agent_name,
    details={"mode": mode}
)
```

Session end:
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

---

## Error Handling

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

### Edge Cases

- **No Access**: User without agent access sees 403 error on terminal connect
- **Network Interruption**: Terminal shows disconnection, allows reconnect via Connect button
- **Token Expiry**: Auth error displayed, prompt to refresh page
- **Agent Deleted**: Container not found error when agent is deleted mid-session

### Cleanup
No cleanup required - sessions terminate automatically on disconnect.

### Status
**Testing Status**: Not Tested

---

## Changelog

| Date | Change |
|------|--------|
| 2025-12-25 | Initial implementation - replaced Chat tab with Terminal tab |
| 2025-12-25 | Added fullscreen toggle with ESC key to exit |
