# Feature: Web Terminal for Agents

> **⚠️ DEPRECATED (2026-03-15)**: Terminal tab hidden from agent detail page for all users. Candidate for full removal. Backend WebSocket endpoint and SSH service remain intact for API-level access. Frontend components retained but not rendered.
>
> **Previous (2026-02-18)**: Terminal tab repositioned after Git tab in AgentDetail.vue.

## Overview

Browser-based interactive terminal for any agent using xterm.js with full Claude Code TUI support. **UI tab hidden as of 2026-03-15** — backend API remains available. Includes per-agent API key control (Req 11.7) allowing owners to choose between platform API key or terminal-based authentication.

## User Story

As a **user**, I want to access an interactive terminal for my agents directly from my browser so that I can run Claude Code or bash commands without SSH port exposure.

As an **agent owner**, I want to control whether my agent uses the platform API key or requires terminal authentication so that I can manage Claude API costs and subscription usage independently per agent.

## Entry Points

- **UI**: `src/frontend/src/views/AgentDetail.vue:93-121` - Terminal tab (uses v-show for persistent WebSocket)
- **Tab Order (2026-02-18)**: Terminal tab now positioned after Git tab in `visibleTabs` (line 520). Order: Tasks, Dashboard*, Schedules, Credentials, Skills, Sharing*, Permissions*, Git*, **Terminal**, Folders*, Public Links*, Info (* = conditional)
- **UI**: `src/frontend/src/components/TerminalPanelContent.vue:53-87` - API key authentication toggle (when agent stopped)
- **API**: `GET /api/agents/{agent_name}/api-key-setting` - Get current API key setting
- **API**: `PUT /api/agents/{agent_name}/api-key-setting` - Update API key setting (owner only)
- **API**: `WS /api/agents/{agent_name}/terminal?mode=claude|bash|gemini&model=<model>` - Terminal WebSocket connection

---

## Frontend Layer

### Components

#### AgentDetail.vue (Parent)
- **File**: `src/frontend/src/views/AgentDetail.vue`
- **Terminal Tab**: Lines 93-121 - Terminal embedded in tab panel with fullscreen support (uses `v-show` to keep WebSocket connected when switching tabs)
- **State** (via `useAgentTerminal` composable at line 287-296):
  - `isTerminalFullscreen` ref controls fullscreen mode
  - `terminalRef` ref for calling terminal methods
- **State** (via `useAgentSettings` composable at line 371-382):
  - `apiKeySetting` ref for API key authentication mode
  - `apiKeySettingLoading` ref for loading state
  - `currentModel` ref for selected model
- **Event Handlers** (from composables):
  - `onTerminalConnected()` - Shows success notification
  - `onTerminalDisconnected()` - Shows info notification
  - `onTerminalError()` - Shows error notification
  - `toggleTerminalFullscreen()` - Toggles fullscreen mode, refits terminal
  - `handleTerminalKeydown()` - ESC key exits fullscreen
  - `loadApiKeySetting()` - Fetches current API key setting
  - `updateApiKeySetting(usePlatformKey)` - Updates API key setting

#### TerminalPanelContent.vue (Container Component)
- **File**: `src/frontend/src/components/TerminalPanelContent.vue` (166 lines)
- **Purpose**: Wrapper that shows either AgentTerminal (when running) or stopped state UI with API key toggle
- **Props**:
  - `agentName: String` (required) - Agent identifier
  - `agentStatus: String` - "running" or "stopped"
  - `runtime: String` - "claude-code" or "gemini-cli"
  - `model: String` - Model to use (e.g., "sonnet", "gemini-2.5-flash")
  - `isFullscreen: Boolean` - Fullscreen state
  - `canShare: Boolean` - Whether user is owner (controls API key toggle visibility)
  - `apiKeySetting: Object` - Current API key setting
  - `apiKeySettingLoading: Boolean` - Loading state
  - `actionLoading: Boolean` - Start button loading state
- **Emits**: `toggle-fullscreen`, `connected`, `disconnected`, `error`, `start-agent`, `update-api-key-setting`
- **Lines 53-87**: API Key Authentication toggle UI (owner-only, when agent stopped)

**API Key Authentication Toggle** (Lines 53-87):
When agent is stopped, the Terminal tab shows an authentication toggle (owner-only control):
- **Use Platform API Key** (default): Agent uses Trinity's configured Anthropic API key
  - Platform key injected as `ANTHROPIC_API_KEY` environment variable
  - Claude commands work immediately without authentication
  - Usage counts against Trinity's API subscription
- **Authenticate in Terminal**: No API key injected; user runs `claude login` in terminal
  - Agent container starts without `ANTHROPIC_API_KEY` environment variable
  - User must run `claude login` in terminal to authenticate with their own subscription (Claude Pro/Max)
  - OAuth session stored in `~/.claude.json` inside the container
  - Usage counts against user's personal Claude subscription (Pro/Max)
  - **Headless calls also use subscription**: Once logged in, scheduled tasks, MCP-triggered executions, and `/api/task` calls all use the OAuth session instead of requiring API billing

**Important**: Changing this setting requires agent restart. Container is recreated with/without `ANTHROPIC_API_KEY` env var. The UI shows `restart_required: true` badge when setting changed but not yet applied.

**Claude Max Subscription Flow** (Added 2026-02-15):
1. Owner sets "Authenticate in Terminal" and restarts agent
2. User connects to web terminal and runs `claude login` or `/login` in Claude Code TUI
3. OAuth flow completes, session stored in `~/.claude.json`
4. All subsequent Claude Code executions (interactive and headless) use the subscription:
   - Interactive terminal sessions
   - Scheduled task executions (`/api/task` via scheduler)
   - MCP-triggered `chat_with_agent` calls
   - Direct `/api/task` API calls

#### AgentTerminal.vue (Terminal Component)
- **File**: `src/frontend/src/components/AgentTerminal.vue` (526 lines)
- **Props** (lines 102-127):
  - `agentName: String` (required) - Name of the agent to connect to
  - `autoConnect: Boolean` (default: true) - Auto-connect on mount
  - `showFullscreenToggle: Boolean` (default: false) - Show fullscreen button
  - `isFullscreen: Boolean` (default: false) - Current fullscreen state
  - `runtime: String` (default: 'claude-code') - Runtime type for mode label
  - `model: String` (default: '') - Model parameter for CLI
- **Emits**: `connected`, `disconnected`, `error`, `toggle-fullscreen`
- **Exposes** (lines 499-504): `connect()`, `disconnect()`, `focus()`, `fit()`

**Key References**:
| Ref | Type | Purpose |
|-----|------|---------|
| `terminalContainer` | HTMLElement | xterm.js mount point |
| `selectedMode` | 'cli' \| 'bash' | Terminal mode (cli maps to claude or gemini) |
| `connectionStatus` | 'connected' \| 'connecting' \| 'disconnected' | WebSocket state |
| `errorMessage` | string | Error display |

**Mode Toggle** (Lines 16-41):
- `cli` mode: Maps to `claude` or `gemini` based on `runtime` prop
- `bash` mode: Direct bash shell
- Mode can only be changed when disconnected

**WebSocket Connection** (Lines 300-390):
```javascript
// Build WebSocket URL with agent name
const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
// Map 'cli' mode to the appropriate runtime command (claude or gemini)
const terminalMode = selectedMode.value === 'cli' ? (isGemini.value ? 'gemini' : 'claude') : 'bash'
// Include model parameter if specified
const modelParam = props.model ? `&model=${encodeURIComponent(props.model)}` : ''
const wsUrl = `${protocol}//${location.host}/api/agents/${encodeURIComponent(props.agentName)}/terminal?mode=${terminalMode}${modelParam}`

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

**Rendering** (Lines 184-213):
- Uses WebGL renderer for GPU acceleration (primary)
- Falls back to Canvas renderer if WebGL unavailable
- DOM renderer as final fallback

### Composables

#### useAgentTerminal.js
- **File**: `src/frontend/src/composables/useAgentTerminal.js` (48 lines)
- **Purpose**: Terminal fullscreen and keyboard handling
- **Returns**: `isTerminalFullscreen`, `terminalRef`, `toggleTerminalFullscreen`, `handleTerminalKeydown`, `onTerminalConnected`, `onTerminalDisconnected`, `onTerminalError`

#### useAgentSettings.js
- **File**: `src/frontend/src/composables/useAgentSettings.js` (127 lines)
- **Purpose**: API key, model, and resource settings management
- **API Key Methods** (lines 26-50): `loadApiKeySetting()`, `updateApiKeySetting(usePlatformKey)`
- **Model Methods** (lines 52-76): `loadModelInfo()`, `changeModel()`
- **Resource Methods** (lines 78-110): `loadResourceLimits()`, `updateResourceLimits()`

### Dependencies

```json
{
  "@xterm/xterm": "^5.5.0",
  "@xterm/addon-fit": "^0.10.0",
  "@xterm/addon-web-links": "^0.11.0",
  "@xterm/addon-webgl": "^0.19.0",
  "@xterm/addon-canvas": "^0.7.0"
}
```

---

## Backend Layer

### Architecture

The terminal feature uses a **thin router + service layer** architecture:

| Layer | File | Purpose |
|-------|------|---------|
| Router | `src/backend/routers/agents.py:1173-1184` | WebSocket endpoint definition |
| Service | `src/backend/services/agent_service/terminal.py` (320 lines) | Session management, PTY handling |
| Service | `src/backend/services/agent_service/api_key.py` (85 lines) | API key setting logic |

### API Key Setting Endpoints

- **Router**: `src/backend/routers/agents.py:747-765`
- **Service**: `src/backend/services/agent_service/api_key.py`

#### GET `/api/agents/{agent_name}/api-key-setting`
Returns current API key authentication setting for an agent.

**Authentication**: Any user with agent access (owner, shared, or admin)

```python
@router.get("/{agent_name}/api-key-setting")
async def get_agent_api_key_setting(agent_name: str, ...) -> dict:
    """Get the API key setting for an agent."""
    return await get_agent_api_key_setting_logic(agent_name, current_user)
```

**Service Logic** (`api_key.py:18-46`):
```python
async def get_agent_api_key_setting_logic(agent_name: str, current_user: User) -> dict:
    if not db.can_user_access_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="You don't have permission to access this agent")

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    use_platform_key = db.get_use_platform_api_key(agent_name)

    # Check if current container matches the setting
    container.reload()
    env_matches = check_api_key_env_matches(container, agent_name)

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

**Person-only (#2996)**: the route takes `Depends(require_person)` — a JWT session or the owner's own `user`-scoped key. Agent- and system-scoped keys (and every other key scope) get 403 `person_required` before the owner check, whatever agent they address. See requirements `auth.md` §2.8.

**Service Logic** (`api_key.py:49-84`):
```python
async def update_agent_api_key_setting_logic(agent_name: str, body: dict, current_user: User, request: Request) -> dict:
    # Only owner can modify this setting
    if not db.can_user_share_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="Only the owner can modify API key settings")

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    use_platform_key = body.get("use_platform_api_key")
    if use_platform_key is None:
        raise HTTPException(status_code=400, detail="use_platform_api_key is required")

    # Update setting
    db.set_use_platform_api_key(agent_name, bool(use_platform_key))

    return {
        "status": "updated",
        "agent_name": agent_name,
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

- **Router**: `src/backend/routers/agents.py:1173-1184`
- **Service**: `src/backend/services/agent_service/terminal.py`
- **Query Params**:
  - `mode: str` (default: "claude") - "claude", "gemini", or "bash"
  - `model: str` (default: None) - Optional model override (e.g., "gemini-2.5-flash", "sonnet")

```python
@router.websocket("/{agent_name}/terminal")
async def agent_terminal(
    websocket: WebSocket,
    agent_name: str,
    mode: str = Query(default="claude"),
    model: str = Query(default=None)
):
    """Interactive terminal WebSocket for any agent."""
    await _terminal_manager.handle_terminal_session(
        websocket=websocket,
        agent_name=agent_name,
        mode=mode,
        decode_token_fn=decode_token,
        model=model
    )
```

### TerminalSessionManager Class (`services/agent_service/terminal.py:20-320`)

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
- `_check_and_register_session()` (line 32-49): Check/register session with 5-minute timeout
- `_unregister_session()` (line 51-55): Remove session from tracking
- `handle_terminal_session()` (line 57-320): Main WebSocket handler

### Authentication Flow (terminal.py:84-147)

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

### Session Limiting (terminal.py:32-55)

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

### Docker Exec Creation (terminal.py:186-220)

```python
# Build command based on mode (supports claude, gemini, bash)
if mode == "claude":
    cmd = ["claude", "--dangerously-skip-permissions"]
    if model:
        cmd.extend(["--model", model])
elif mode == "gemini":
    cmd = ["gemini"]
    if model:
        cmd.extend(["--model", model])
else:
    cmd = ["/bin/bash"]

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

**Note**: Claude mode includes `--dangerously-skip-permissions` flag to bypass interactive permission prompts in the TUI. The `ANTHROPIC_API_KEY` or `GEMINI_API_KEY` environment variable is set during container creation, not in the exec environment. The API key setting determines whether these env vars are injected when the container starts.

### PTY Forwarding (terminal.py:227-286)

Bidirectional forwarding uses asyncio socket coroutines for proper async I/O:

```python
loop = asyncio.get_event_loop()

async def read_from_docker():
    """Read from Docker socket using asyncio sock_recv (no thread pool)."""
    while True:
        data = await loop.sock_recv(docker_socket, 16384)
        if not data:
            break
        await websocket.send_bytes(data)

async def read_from_websocket():
    """Read from WebSocket, send to Docker socket."""
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            break

        if "text" in message:
            # Check for resize control message
            try:
                ctrl = json.loads(message["text"])
                if ctrl.get("type") == "resize":
                    docker_client.api.exec_resize(exec_id, height=ctrl["rows"], width=ctrl["cols"])
                    continue
            except json.JSONDecodeError:
                pass
            await loop.sock_sendall(docker_socket, message["text"].encode())
        elif "bytes" in message:
            await loop.sock_sendall(docker_socket, message["bytes"])

# Run both tasks concurrently
await asyncio.gather(read_from_docker(), read_from_websocket(), return_exceptions=True)
```

---

## Data Layer

### Database Schema

The API key setting is stored in the `agent_ownership` table (not a separate table):

**Table**: `agent_ownership`

| Column | Type | Description |
|--------|------|-------------|
| `agent_name` | TEXT PRIMARY KEY | Agent identifier |
| `owner_email` | TEXT | Owner's email |
| `use_platform_api_key` | INTEGER | 1 = use platform key (default), 0 = terminal auth |
| ... | ... | Other ownership fields |

**Default**: New agents default to `use_platform_api_key = 1` (True)

### Database Operations

**File**: `src/backend/db/agents.py`

| Method | Line | Purpose |
|--------|------|---------|
| `get_use_platform_api_key(agent_name)` | 302-313 | Returns boolean - whether agent uses platform key |
| `set_use_platform_api_key(agent_name, use_platform)` | 315-324 | Updates setting in agent_ownership table |

**Implementation** (`db/agents.py:302-324`):
```python
def get_use_platform_api_key(self, agent_name: str) -> bool:
    """Check if agent should use platform API key (default: True)."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT COALESCE(use_platform_api_key, 1) as use_platform_api_key
            FROM agent_ownership WHERE agent_name = ?
        """, (agent_name,))
        row = cursor.fetchone()
        if row:
            return bool(row["use_platform_api_key"])
        return True  # Default to using platform key

def set_use_platform_api_key(self, agent_name: str, use_platform_key: bool) -> bool:
    """Set whether agent should use platform API key."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE agent_ownership SET use_platform_api_key = ?
            WHERE agent_name = ?
        """, (1 if use_platform_key else 0, agent_name))
        conn.commit()
        return cursor.rowcount > 0
```

---

## Side Effects

### Session Logging

Terminal sessions are logged via Python logger (not audit events):

**Session Start** (`terminal.py:225`):
```python
logger.info(f"Terminal session started for {user_email}@{agent_name} (mode: {mode})")
```

**Session End** (`terminal.py:316-319`):
```python
if session_duration:
    logger.info(f"Terminal session ended for {user_email}@{agent_name} (duration: {session_duration:.1f}s)")
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
| Missing auth message | 4001 | "Expected auth message first" |
| Missing token | 4001 | "Token required" |
| Invalid token | 4001 | "Invalid token" |
| No agent access | 4003 | "You don't have access to this agent" |
| Auth timeout (10s) | 4001 | "Authentication timeout" |
| Invalid auth JSON | 4001 | "Invalid JSON in auth message" |
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
5. **Session Logging**: Full session logging with duration tracking (via Python logger)
6. **Binary Transport**: Raw PTY data sent as binary WebSocket frames
7. **TERM Environment**: Sets `TERM=xterm-256color` for proper TUI rendering
8. **Permission Skip**: Claude mode uses `--dangerously-skip-permissions` for non-interactive operation

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
- **Upstream**: [email-authentication.md](email-authentication.md) - JWT token creation
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

7. **Gemini CLI Mode** (for gemini-cli runtime agents)
   - Action: Create agent with gemini-cli runtime, connect terminal
   - Expected: CLI mode shows "Gemini CLI" label, connects to gemini command
   - Verify: Gemini CLI interface loads

8. **Shared Agent Access**
   - Action: Navigate to an agent shared with you by another user
   - Expected: Terminal tab visible and functional
   - Verify: Can connect and execute commands

9. **Session Limit**
   - Action: Open second browser tab, try to connect to same agent
   - Expected: Error "You already have an active terminal session for this agent"
   - Verify: First session remains connected

10. **Stopped Agent**
    - Action: Navigate to agent detail page with agent stopped
    - Expected: Terminal shows "Agent is not running" with Start button
    - Verify: Clicking Start starts agent, terminal then auto-connects

11. **API Key Setting Toggle (Owner Only)**
    - Action: As agent owner, navigate to terminal tab with agent stopped
    - Expected: See "Authentication" section with two radio buttons
    - Verify: Can toggle between "Use Platform API Key" and "Authenticate in Terminal"

12. **Change API Key Setting**
    - Action: Toggle "Authenticate in Terminal" radio button
    - Expected: Setting updates, shows "Restart required" badge
    - Verify: API call to `PUT /api/agents/{name}/api-key-setting` succeeds

13. **Restart with Terminal Auth**
    - Action: Start agent after changing to "Authenticate in Terminal"
    - Expected: Agent starts, terminal connects
    - Action: Type `claude` in terminal
    - Expected: Prompt to run `claude login` (no API key injected)
    - Verify: Container has no `ANTHROPIC_API_KEY` environment variable

14. **Restart with Platform Key**
    - Action: Stop agent, toggle back to "Use Platform API Key", restart
    - Expected: Agent starts, Claude commands work immediately
    - Verify: Container has `ANTHROPIC_API_KEY` environment variable set

15. **Non-Owner Cannot Change Setting**
    - Action: As non-owner user with shared access, view terminal tab
    - Expected: API key setting UI not visible (canShare is false for non-owners)
    - Action: Attempt direct API call `PUT /api/agents/{name}/api-key-setting`
    - Expected: 403 "Only the owner can modify API key settings"

16. **Tab Switching Preserves Connection**
    - Action: Connect to terminal, switch to another tab, switch back
    - Expected: Terminal still connected (uses v-show not v-if)
    - Verify: No reconnection needed, session preserved

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
**Testing Status**: Verified 2026-02-18

**Verified Features**:
- Terminal connection and PTY forwarding
- Claude Code TUI rendering with WebGL/Canvas acceleration
- Fullscreen mode toggle
- Session limiting (1 per user per agent)
- Access control (owner, shared, admin)
- Per-agent API key control toggle (Req 11.7)
- Owner-only API key setting modification
- Container recreation on API key setting change
- Mode toggle (Claude/Gemini/Bash)
- Model parameter support
- Tab switching preserves connection (v-show)

---

## Revision History

| Date | Changes |
|------|---------|
| 2026-03-15 | **Terminal tab hidden**: Tab removed from `visibleTabs`, template content commented out, all reconnection/fit logic disabled. Backend endpoints unchanged. Marked as deprecation candidate. |
| 2026-02-18 | **Terminal tab repositioned**: Tab now appears after Git tab in `visibleTabs` (line 520). Updated Entry Points section with new tab order. |
| 2026-01-23 | Verified all line numbers across layers. Database schema uses `agent_ownership` table. Added WebGL/Canvas renderer documentation. |
| 2025-12-28 | Added Gemini CLI runtime support, model selection feature. |
| 2025-12-26 | Added per-agent API key control (Req 11.7). |
| 2025-12-25 | Initial implementation. |

## Changelog

| Date | Change |
|------|--------|
| 2026-02-18 | **Terminal tab repositioned**: Terminal tab moved from middle of tab list to after Git tab. New tab order in visibleTabs (line 520): Tasks, Dashboard*, Schedules, Credentials, Skills, Sharing*, Permissions*, Git*, Terminal, Folders*, Public Links*, Info. Also: Logs tab and Files tab removed from AgentDetail.vue. |
| 2026-02-15 | **Claude Max subscription support**: Updated documentation to reflect that Claude Code now uses whatever authentication is available: (1) OAuth session from `/login` (Claude Pro/Max subscription) stored in `~/.claude.json`, or (2) `ANTHROPIC_API_KEY` environment variable. Users can log in once via web terminal with Claude Max subscription, then all subsequent headless executions use their subscription instead of API billing. Updated Test Cases 13-14 to note this authentication model. |
| 2026-01-23 | **Line number verification**: Updated all line numbers. Corrected database schema (uses `agent_ownership` table, not separate table). Added WebGL/Canvas renderer documentation. Clarified no audit logging for terminal sessions (uses Python logger). Added `--dangerously-skip-permissions` flag documentation. Added Gemini CLI test case. Added tab switching preservation note. |
| 2025-12-30 | Line number verification. Added Gemini runtime support to terminal modes. Updated WebSocket query params documentation. |
| 2025-12-27 | Service layer refactoring: Terminal session management moved to `services/agent_service/terminal.py`. API key logic moved to `api_key.py`. Router reduced to thin endpoint definitions. |
| 2025-12-25 | Initial implementation - replaced Chat tab with Terminal tab |
| 2025-12-25 | Added fullscreen toggle with ESC key to exit |
| 2025-12-26 | Added per-agent API key control (Req 11.7) - owner can choose platform key vs terminal auth |
| 2025-12-26 | Updated flow documentation with API key setting endpoints and data layer |
