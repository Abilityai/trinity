# Feature: Web Terminal for Agents

## Overview

Browser-based interactive terminal for Trinity agents using xterm.js with full Claude Code TUI support. The terminal is embedded on agent detail pages with fullscreen capability. Provides WebSocket-based PTY forwarding to agent containers.

## User Story

As an **agent user**, I want to access an interactive terminal for my agents directly from my browser so that I can run Claude Code or bash commands without SSH port exposure.

## Entry Points

- **UI (Regular Agents)**: `src/frontend/src/views/AgentDetail.vue:93-121` - Terminal tab with fullscreen support
- **API (Regular Agents)**: `WS /api/agents/{agent_name}/terminal?mode=claude|gemini|bash&model=<model>`
- **UI (System Agent)**: System Agent page - Embedded terminal panel (admin-only)
- **API (System Agent)**: `WS /api/system-agent/terminal?mode=claude|gemini|bash`

---

## Frontend Layer

### Components

#### AgentDetail.vue (Parent)
- **File**: `src/frontend/src/views/AgentDetail.vue`
- **Terminal Tab**: Lines 93-121 - Embedded terminal with fullscreen support
- **State**:
  - `isTerminalFullscreen` ref controls fullscreen mode
  - `terminalRef` ref for calling terminal methods
- **Event Handlers**:
  - `onTerminalConnected()` - Shows success notification
  - `onTerminalDisconnected()` - Shows info notification
  - `onTerminalError()` - Shows error notification
  - `toggleTerminalFullscreen()` - Toggles fullscreen mode, refits terminal
  - `handleTerminalKeydown()` - ESC key exits fullscreen

```vue
<!-- Terminal Tab Content (lines 93-121) -->
<div
  v-show="activeTab === 'terminal'"
  :class="[
    'transition-all duration-300',
    isTerminalFullscreen ? 'fixed inset-0 z-50 bg-gray-900' : ''
  ]"
  @keydown="handleTerminalKeydown"
>
  <TerminalPanelContent
    ref="terminalRef"
    :agent-name="agent.name"
    :agent-status="agent.status"
    :runtime="agent.runtime || 'claude-code'"
    :model="currentModel"
    :is-fullscreen="isTerminalFullscreen"
    ...
  />
</div>
```

#### TerminalPanelContent.vue (Wrapper)
- **File**: `src/frontend/src/components/TerminalPanelContent.vue`
- **Props**:
  - `agentName: String` (required)
  - `agentStatus: String` (default: 'stopped')
  - `runtime: String` (default: 'claude-code')
  - `model: String` (default: 'sonnet')
  - `isFullscreen: Boolean` (default: false)
  - `canShare: Boolean` (default: false)
  - `apiKeySetting: Object` (API key configuration)
- **Features**:
  - Shows "Agent is not running" placeholder when stopped
  - API key setting toggle (Platform key vs authenticate in terminal)
  - Forwards events to AgentTerminal child component

#### AgentTerminal.vue (Terminal Component)
- **File**: `src/frontend/src/components/AgentTerminal.vue`
- **Props**:
  - `agentName: String` (required)
  - `autoConnect: Boolean` (default: true) - Auto-connect on mount
  - `showFullscreenToggle: Boolean` (default: false)
  - `isFullscreen: Boolean` (default: false)
  - `runtime: String` (default: 'claude-code') - 'claude-code' or 'gemini-cli'
  - `model: String` (default: '') - Model to use
- **Emits**: `connected`, `disconnected`, `error`, `toggle-fullscreen`
- **Exposes**: `connect()`, `disconnect()`, `focus()`, `fit()`

**Key References**:
| Ref | Type | Purpose |
|-----|------|---------|
| `terminalContainer` | HTMLElement | xterm.js mount point |
| `selectedMode` | 'cli' \| 'bash' | Terminal mode |
| `connectionStatus` | 'connected' \| 'connecting' \| 'disconnected' | WebSocket state |
| `errorMessage` | string | Error display |

**Terminal Initialization** (lines 216-297):
```javascript
terminal = new Terminal({
  cursorBlink: true,
  fontSize: 14,
  fontFamily: 'Menlo, Monaco, "Courier New", monospace',
  theme: { /* VS Code dark theme colors */ },
  allowProposedApi: true,
  scrollback: 10000,
  // Performance optimizations
  fastScrollModifier: 'alt',
  fastScrollSensitivity: 5,
  smoothScrollDuration: 0
})

fitAddon = new FitAddon()
terminal.loadAddon(fitAddon)
terminal.loadAddon(new WebLinksAddon())

// Load WebGL/Canvas renderer for GPU acceleration
loadRenderer() // WebGL with canvas fallback
```

**WebSocket Connection** (lines 299-390):
```javascript
// Build WebSocket URL
const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
const terminalMode = selectedMode.value === 'cli' ? (isGemini.value ? 'gemini' : 'claude') : 'bash'
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

**Resize Handling** (lines 165-182):
```javascript
function debouncedResize() {
  if (resizeTimeout) clearTimeout(resizeTimeout)
  resizeTimeout = setTimeout(() => {
    if (fitAddon && terminal) {
      fitAddon.fit()
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({
          type: 'resize',
          cols: terminal.cols,
          rows: terminal.rows
        }))
      }
    }
  }, 150)
}
```

### Composable: useAgentTerminal
- **File**: `src/frontend/src/composables/useAgentTerminal.js`
- **Functions**:
  - `toggleTerminalFullscreen()` - Toggle fullscreen and refit
  - `handleTerminalKeydown()` - ESC exits fullscreen
  - `onTerminalConnected/Disconnected/Error()` - Event handlers

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

### WebSocket Endpoints

There are two terminal implementations:
1. **Regular Agents**: `src/backend/routers/agents.py:1173-1187` - Access-controlled per agent
2. **System Agent**: `src/backend/routers/system_agent.py:262-528` - Admin-only access

#### Regular Agents Endpoint

- **File**: `src/backend/routers/agents.py:1173-1187`
- **Endpoint**: `@router.websocket("/{agent_name}/terminal")`
- **Query Params**:
  - `mode: str` (default: "claude") - One of "claude", "gemini", or "bash"
  - `model: str` (default: None) - Model override

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

### Terminal Session Manager

- **File**: `src/backend/services/agent_service/terminal.py:20-320`
- **Class**: `TerminalSessionManager`

#### Session Management (lines 20-56)

```python
class TerminalSessionManager:
    def __init__(self):
        self._active_sessions: dict = {}  # (user_id, agent_name) -> session_info
        self._lock = threading.Lock()

    def _check_and_register_session(self, user_email: str, agent_name: str, timeout_seconds: int = 300) -> bool:
        """Returns True if session was registered, False if limit reached."""
        session_key = (user_email, agent_name)
        with self._lock:
            if session_key in self._active_sessions:
                session_info = self._active_sessions[session_key]
                session_age = (datetime.utcnow() - session_info["started_at"]).total_seconds()
                if session_age < timeout_seconds:
                    return False  # Session limit reached
                # Stale session, clean it up
            self._active_sessions[session_key] = {"started_at": datetime.utcnow()}
            return True
```

#### Authentication Flow (lines 83-147)

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

# Step 3: Check access to agent (via database)
if not db.can_user_access_agent(user_email, agent_name) and user_role != "admin":
    # Send error with code 4003
    return
```

### Token Decoding Helper

- **File**: `src/backend/dependencies.py:71-101`
- **Function**: `decode_token(token: str) -> Optional[dict]`

```python
def decode_token(token: str) -> Optional[dict]:
    """
    Decode a JWT token without FastAPI dependency.
    Useful for WebSocket authentication where Depends() doesn't work.

    Returns:
        dict with keys: sub, email, role, exp, mode (if valid)
        None if token is invalid or expired
    """
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")
        if not username:
            return None

        user = db.get_user_by_username(username)
        if not user:
            return None

        return {
            "sub": username,
            "email": user.get("email"),
            "role": user.get("role"),
            "exp": payload.get("exp"),
            "mode": payload.get("mode")
        }
    except JWTError:
        return None
```

### Docker Exec Creation (lines 186-220)

```python
# Build command based on mode - supports multiple terminal modes
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

# Create exec instance with TTY (async to avoid blocking event loop - DOCKER-001)
from services.docker_utils import api_exec_create, api_exec_start

exec_instance = await api_exec_create(
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

# Start exec and get raw socket (async to avoid blocking - DOCKER-001)
exec_output = await api_exec_start(exec_id, socket=True, tty=True)
docker_socket = exec_output._sock
docker_socket.setblocking(False)
```

> **Note**: As of DOCKER-001, exec creation and start use async wrappers from `services/docker_utils.py` to prevent event loop blocking during the Docker API calls.

### Bidirectional Forwarding (lines 227-286)

The terminal uses **asyncio socket coroutines** (`loop.sock_recv()` and `loop.sock_sendall()`) for proper async I/O without thread pool overhead.

```python
loop = asyncio.get_event_loop()

async def read_from_docker():
    """Read from Docker socket using asyncio sock_recv (no thread pool)."""
    try:
        while True:
            data = await loop.sock_recv(docker_socket, 16384)
            if not data:
                break
            await websocket.send_bytes(data)
    except Exception as e:
        logger.debug(f"Docker read error for {agent_name}: {e}")

async def read_from_websocket():
    """Read from WebSocket, send to Docker socket."""
    try:
        while True:
            message = await websocket.receive()

            if message["type"] == "websocket.disconnect":
                break

            if "text" in message:
                try:
                    ctrl = json.loads(message["text"])
                    if ctrl.get("type") == "resize":
                        docker_client.api.exec_resize(
                            exec_id,
                            height=ctrl.get("rows", 24),
                            width=ctrl.get("cols", 80)
                        )
                        continue
                except json.JSONDecodeError:
                    pass
                await loop.sock_sendall(docker_socket, message["text"].encode())
            elif "bytes" in message:
                await loop.sock_sendall(docker_socket, message["bytes"])
    except WebSocketDisconnect:
        pass

# Run both tasks concurrently
await asyncio.gather(read_from_docker(), read_from_websocket(), return_exceptions=True)
```

---

## System Agent Terminal

The System Agent has a separate implementation with admin-only access.

### WebSocket Endpoint

- **File**: `src/backend/routers/system_agent.py:262-528`
- **Endpoint**: `@router.websocket("/terminal")`
- **Admin Check**: Lines 325-333

```python
# Check admin access
if user.get("role") != "admin":
    await websocket.send_text(json.dumps({
        "type": "error",
        "message": "Admin access required",
        "close": True
    }))
    await websocket.close(code=4003, reason="Admin access required")
    return
```

Key differences from regular agent terminal:
- Admin role required (line 326)
- Session tracking per user only (not per user+agent)
- No `--dangerously-skip-permissions` flag for Claude

---

## Side Effects

### Session Cleanup

On disconnect (lines 299-319 in terminal.py):
```python
finally:
    # Cleanup
    if docker_socket:
        try:
            docker_socket.close()
        except:
            pass

    # Remove from active sessions
    if user_email and agent_name:
        self._unregister_session(user_email, agent_name)

        # Calculate session duration for logging
        session_duration = None
        if session_start:
            session_duration = (datetime.utcnow() - session_start).total_seconds()

        if session_duration:
            logger.info(f"Terminal session ended for {user_email}@{agent_name} (duration: {session_duration:.1f}s)")
```

### Access changes (abilityai/trinity-enterprise#863)

Both terminal doors follow the account they were opened for, through the worker's live-connection registry (`services/live_connections.py`, mechanism in `architecture/integrations.md` → Real-time Delivery). The shared helper is `TerminalAccess` in `services/agent_service/terminal.py`; `routers/system_agent.py` imports it.

1. **Register.** Right after the access checks pass (decode, floor, owner/admin), in the same synchronous step as the account read, the door creates `TerminalAccess(websocket, door, user)`. Its snapshot takes the role and email from the decoded identity the door authorized on, and the rest from a read of the row just after. Doors: `agent_terminal`, `system_agent_terminal`.
2. **Start.** The shell starts with `TRINITY_TERMINAL_SESSION=<random id>` in its environment (`shell_env`). After `api_exec_start` returns, the door calls `go_live()`. If a re-check decided to close while the start was running, the door closes the socket it got and sends the access-close frame instead of `auth_success`. The start is never cancelled: it runs in a Docker executor thread that a cancel would not stop.
3. **Serve.** The two forwarders run as tasks the session owns (`forward`). The browser forwarder stops at the next message once the session is revoked.
4. **Close.** The closer sends `{"type": "error", "message": ..., "close": true}`, closes with `4003`, and cancels the two forwarder tasks. It never cancels the handler's task, so the handler returns through its `finally`: the container socket is closed, the session slot is freed, and the entry is released (`finish`).
5. **End the shell.** A session closed for an access change (and only such a session) also has its shell ended: `finish` runs one exec as `developer` (`END_SHELL_TIMEOUT_S` = 10 s) that ends, HUP first and then KILL, the processes started from that terminal session (found by its marker; `_END_SHELL_SCRIPT`). Best effort: a failure is logged as a warning. An ordinary close by the browser ends nothing, as before.

The re-check runs at once on the worker that served a role change, and on every worker every 15 s.

---

## Error Handling

| Error Case | WebSocket Code | Message |
|------------|----------------|---------|
| Missing auth message | 4001 | "Expected auth message first" |
| Missing token | 4001 | "Token required" |
| Invalid token, or the account is missing or deactivated (`decode_token` returns `None`; abilityai/trinity-enterprise#863). Refused before any container lookup | 4001 | "Invalid token" |
| Access denied | 4003 | "You don't have access to this agent" |
| Admin required (System Agent) | 4003 | "Admin access required" |
| Auth timeout (10s) | 4001 | "Authentication timeout" |
| Invalid auth JSON | 4001 | "Invalid JSON in auth message" |
| Existing session | 4002 | "You already have an active terminal session for this agent" |
| Container not found | 4004 | "Agent '{name}' container not found" |
| Container not running | 4004 | "Agent '{name}' is not running. Please start it first." |
| Access changed while open or opening (#863) | 4003 | "Your access changed. Open the terminal again to continue." |
| Access ended while open or opening (#863) | 4003 | "Your access to this terminal has ended." |

Frontend error display (AgentTerminal.vue lines 84-89):
```vue
<div
  v-if="errorMessage"
  class="px-4 py-2 bg-red-900/50 border-t border-red-800 text-red-300 text-sm"
>
  {{ errorMessage }}
</div>
```

---

## Security Considerations

1. **Access Control**: User must have access to agent via `db.can_user_access_agent()` (or be admin)
2. **Admin-Only System Agent**: Role check happens after JWT validation
3. **Session Limit**: One terminal per user per agent prevents resource exhaustion
4. **Stale Session Cleanup**: 5-minute timeout prevents orphaned session blocking
5. **No SSH Exposure**: Uses internal Docker network, no port forwarding to host
6. **Binary Transport**: Raw PTY data sent as binary WebSocket frames
7. **TERM Environment**: Sets `TERM=xterm-256color` for proper TUI rendering
8. **Resource Efficiency**: Asyncio socket coroutines provide proper async I/O with zero thread pool calls
9. **GPU Rendering**: WebGL with canvas fallback for terminal performance

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
    |                   |                 |                 |
    | Resize window     |                 |                 |
    |------------------>| resize JSON     |                 |
    |                   |---------------->|                 |
    |                   |                 | exec_resize()   |
    |                   |                 |---------------->|
```

---

## Related Flows

- **Upstream**: [agent-lifecycle.md](agent-lifecycle.md) - Agent deployment and status
- **Upstream**: [auth0-authentication.md](auth0-authentication.md) - JWT token creation
- **Related**: [agent-chat.md](agent-chat.md) - Alternative chat interface via HTTP API
- **Related**: [internal-system-agent.md](internal-system-agent.md) - System agent management
- **Related**: [async-docker-operations.md](async-docker-operations.md) - Async wrappers for Docker exec calls (DOCKER-001)

---

## Testing

### Prerequisites
- Backend and frontend running
- User logged in with access to an agent
- Agent container running

### Test Steps

1. **Terminal Auto-Connection**
   - Action: Navigate to agent detail, click Terminal tab
   - Expected: Terminal shows "Connecting..." then "Connected!" automatically
   - Verify: Green status dot, connection status text

2. **Claude Code Mode (Default)**
   - Action: Type `/help` and press Enter
   - Expected: Claude Code help menu displays with colors
   - Verify: Full TUI preserved, colors render correctly

3. **Bash Mode**
   - Action: Disconnect, switch to Bash mode, reconnect
   - Expected: Bash shell prompt appears
   - Verify: Can run `ls`, `pwd`, `whoami` commands

4. **Gemini CLI Mode**
   - Action: On agent with gemini-cli runtime, verify CLI mode label
   - Expected: Mode toggle shows "Gemini CLI" instead of "Claude Code"
   - Verify: Gemini CLI starts when connected

5. **Fullscreen Toggle**
   - Action: Click the fullscreen button in terminal header
   - Expected: Terminal expands to fill entire viewport
   - Verify: Terminal refits to new size, content preserved

6. **ESC to Exit Fullscreen**
   - Action: Press ESC key while in fullscreen mode
   - Expected: Terminal returns to tab panel size
   - Verify: Layout returns to normal, terminal refits

7. **Terminal Resize**
   - Action: Resize browser window while connected
   - Expected: Terminal content reflows to fit
   - Verify: No content truncation, prompt intact

8. **Session Limit**
   - Action: Open second browser tab, try to connect to same agent
   - Expected: Error "You already have an active terminal session for this agent"
   - Verify: First session remains connected

9. **Access Denied**
   - Action: Try to connect to agent user doesn't have access to
   - Expected: Error "You don't have access to this agent"
   - Verify: Connection rejected with code 4003

10. **Disconnect/Cleanup**
    - Action: Click Disconnect button
    - Expected: Session terminates cleanly
    - Verify: Can reconnect immediately after

### Edge Cases

- **Non-running Agent**: Terminal panel shows "Agent is not running" with Start button
- **Network Interruption**: Terminal shows disconnection, allows reconnect via Connect button
- **Token Expiry**: Auth error displayed, prompt to refresh page
- **WebGL Unavailable**: Falls back to Canvas renderer automatically

### Cleanup
No cleanup required - sessions terminate automatically on disconnect.

### Status
**Testing Status**: Verified (2026-01-23)

---

## Changelog

| Date | Change |
|------|--------|
| 2026-11-08 | **Terminals follow the account** (abilityai/trinity-enterprise#863): both doors register with the live-connection registry, close with an error frame and `4003` when the account's access changes, and end the session's shell. See Side Effects → Access changes. |
| 2026-02-24 | **Async Docker Operations**: Uses `api_exec_create` and `api_exec_start` from docker_utils.py to prevent event loop blocking (DOCKER-001). |
| 2026-01-23 | Updated documentation with current line numbers, added model parameter support, documented Gemini CLI mode |
| 2025-12-28 | Second fix: Replaced broken `add_reader` callback approach with proper asyncio socket coroutines (`sock_recv`/`sock_sendall`) |
| 2025-12-28 | First fix: Refactored bidirectional forwarding from thread pool polling to native asyncio I/O (add_reader) - had issues |
| 2025-12-25 | Embedded terminal directly on page, removed modal - Terminal now auto-connects when agent running |
| 2025-12-25 | Added fullscreen toggle with ESC key to exit |
| 2025-12-25 | Initial implementation (Req 11.5) |

---

## Revision History

| Date | Reviewer | Changes |
|------|----------|---------|
| 2026-02-24 | Claude | Updated Docker Exec Creation section to use async wrappers (`api_exec_create`, `api_exec_start`) from docker_utils.py per DOCKER-001. Added Related Flows link to async-docker-operations.md. |
| 2026-01-23 | Claude | Updated line numbers for AgentDetail.vue (93-121), AgentTerminal.vue (165-505), terminal.py (20-320), agents.py (1173-1187), system_agent.py (262-528). Added model parameter documentation, Gemini CLI runtime support, WebGL/Canvas rendering, TerminalPanelContent wrapper component. Verified access control flow uses db.can_user_access_agent(). |
| 2025-12-25 | Initial | Initial feature flow documentation |
