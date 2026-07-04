# Feature: Authenticated Chat Tab (CHAT-001)

## Overview

A dedicated **Chat** tab in the Agent Detail page that provides a simple, clean chat interface for authenticated users. This complements the Terminal tab (which provides full Claude Code TUI access) with a simpler chat experience that tracks all activity in the Dashboard timeline.

**Spec**: `docs/requirements/AUTHENTICATED_CHAT_TAB.md`

## User Story

As an authenticated user, I want a simple chat interface with my agents that:
- Has a clean, modern UI (like PublicChat)
- Supports multi-turn conversations with session persistence
- Lets me switch between past sessions
- Shows all activity in the Dashboard (unlike Terminal which is TUI-only)
- Works consistently with how public links work
- Shows dynamic status labels while the agent is working (THINK-001)

## Entry Points

- **UI**: `src/frontend/src/views/AgentDetail.vue:498` - Chat tab in tab list (`{ id: 'chat', label: 'Chat' }`)
- **UI**: `src/frontend/src/views/AgentDetail.vue:95-96` - Chat tab content rendering
- **Component**: `src/frontend/src/components/ChatPanel.vue` (755 lines) - Main authenticated chat panel

## Frontend Layer

### Components

| Component | Location | Purpose |
|-----------|----------|---------|
| `ChatPanel.vue` | `components/ChatPanel.vue` (~755 lines) | Main authenticated chat panel with session selector, model selector + SSE streaming |
| `ModelSelector.vue` | `components/ModelSelector.vue` (172 lines) | Reusable model dropdown with presets and free-text input |
| `ChatMessages.vue` | `components/chat/ChatMessages.vue` (87 lines) | Shared message list with bottom-aligned layout |
| `ChatInput.vue` | `components/chat/ChatInput.vue` (83 lines) | Shared input with auto-resize textarea |
| `ChatBubble.vue` | `components/chat/ChatBubble.vue` (61 lines) | Shared message bubble with markdown rendering + timestamp display |
| `ChatLoadingIndicator.vue` | `components/chat/ChatLoadingIndicator.vue` (51 lines) | Bouncing dots with dynamic status text + fade animation |
| `execution-status.js` | `utils/execution-status.js` (72 lines) | Maps stream-json events to human-readable status labels |

### ChatPanel Features

1. **Session Selector** (lines 7-55)
   - Dropdown showing current session date/time
   - List of past sessions with message counts
   - Click to switch sessions and load history

2. **New Chat Button** (lines 64-74)
   - Creates fresh session
   - Clears messages
   - Closes any active SSE connection

3. **Agent Not Running State** (lines 78-91)
   - Yellow warning icon
   - Message: "Start the agent to begin chatting."

4. **Message Display** (lines 124-144)
   - Uses shared `ChatMessages` component
   - User messages: indigo bubbles (right-aligned)
   - Assistant messages: white/gray with markdown (left-aligned)
   - Bottom-aligned (iMessage style)
   - Custom empty slot for welcome message
   - Dynamic loading text via `loadingText` prop (THINK-001)

5. **Model Selector** (lines 60-62)
   - `ModelSelector` component (compact mode) above chat input
   - Persisted to `localStorage` as `trinity_chat_model`
   - Passed as `model` parameter in `/task` payload
   - Empty value = agent default model

6. **Input Area** (lines 159-171)
   - Uses shared `ChatInput` component
   - Auto-resize textarea
   - Send on Enter or button click

### State Management

```javascript
// ChatPanel.vue state (lines 256-267)
const message = ref('')              // Current input
const messages = ref([])             // Current conversation
const loading = ref(false)           // Send in progress
const loadingText = ref('Thinking...') // Dynamic status label (THINK-001)
const error = ref(null)              // Error message
const isRateLimitError = computed()  // Detects rate/usage limit errors for amber styling

// SSE state (THINK-001) - lines 279-282
let heartbeatTimer = null            // 10s timeout fallback to "Working..."
let labelTimer = null                // Min 500ms display time scheduler
let lastLabelTime = 0                // Timestamp of last label change

// Sessions
const selectedModel = ref(localStorage.getItem('trinity_chat_model') || '') // Persisted model choice

// Sessions
const sessions = ref([])             // List of user's sessions
const sessionsLoading = ref(false)   // Loading sessions
const currentSessionId = ref(null)   // Active session
const showSessionDropdown = ref(false)

// Resume mode state (EXEC-023) - lines 291-296
const resumeSessionIdLocal = ref(null)     // Claude session ID for --resume
const resumeExecutionIdLocal = ref(null)   // Execution ID for banner display
const resumeBannerDismissed = ref(false)   // Banner-only dismissal flag
const isResumeMode = computed(() =>        // Show banner when in resume mode AND not dismissed
  !!resumeSessionIdLocal.value && !resumeBannerDismissed.value)
```

### ChatPanel.vue Methods

| Method | Line | Description |
|--------|------|-------------|
| `formatSessionDate()` | 309 | Format timestamp as relative time ("2h ago") |
| `loadSessions()` | 332 | Fetch user's chat sessions for this agent |
| `selectSession()` | 359 | Select a session and load its messages (includes timestamps) |
| `startNewChat()` | 398 | Clear current session, start fresh, close SSE |
| `buildContextPrompt()` | 421 | Build conversation context from last 20 messages |
| `updateLoadingText()` | 441 | Update status label with 500ms min display time (THINK-001) |
| `resetHeartbeat()` | 464 | Reset 10s heartbeat timer for "Working..." fallback (THINK-001) |
| `closeSSE()` | 476 | Close SSE stream reader + cleanup timers (THINK-001) |
| `subscribeToStream()` | 488 | Subscribe to execution SSE stream via fetch ReadableStream (THINK-001) |
| `pollExecution()` | 562 | Poll execution status every 5s until complete (THINK-001) |
| `sendMessage()` | 589 | Send message via async `/task` endpoint with `chat_session_id` (THINK-001 refactor) |

### API Calls

```javascript
// List sessions (line 335)
await axios.get(`/api/agents/${agentName}/chat/sessions`, { headers: authStore.authHeader })

// Get session details with messages (line 377)
await axios.get(`/api/agents/${agentName}/chat/sessions/${sessionId}`, { headers: authStore.authHeader })

// Send message - async mode (THINK-001) (line 632)
await axios.post(`/api/agents/${agentName}/task`, {
  message: contextPrompt,          // Full message with conversation context
  save_to_session: true,           // Persist to chat_sessions table
  user_message: userMessage,       // Original message (for session display)
  create_new_session: !sessionId,  // Create new session if none active
  chat_session_id: currentSessionId.value || undefined,  // Explicit session targeting
  async_mode: true,                // Return immediately with execution_id (THINK-001)
  model: selectedModel.value || undefined  // User-selected model override
}, { headers: authStore.authHeader })

// SSE stream subscription (line 494) - via fetch, not EventSource (custom auth headers)
fetch(`/api/agents/${agentName}/executions/${executionId}/stream`, {
  headers: { 'Authorization': `Bearer ${token}`, 'Accept': 'text/event-stream' }
})

// Poll execution status (line 571)
await axios.get(`/api/agents/${agentName}/executions/${executionId}`, { headers: authStore.authHeader })
```

## Backend Layer

### Endpoints Used

| Endpoint | Purpose |
|----------|---------|
| `GET /api/agents/{name}/chat/sessions` | List user's chat sessions |
| `GET /api/agents/{name}/chat/sessions/{id}` | Get session with messages |
| `POST /api/agents/{name}/task` | Send message (async headless execution) |
| `GET /api/agents/{name}/executions/{id}/stream` | SSE stream proxy to agent (THINK-001) |
| `GET /api/agents/{name}/executions/{id}` | Poll execution status (THINK-001) |

### Why `/task` not `/chat`?

- `/task` uses headless execution - activities tracked in Dashboard timeline
- `/chat` uses `--continue` flag but doesn't track well in Dashboard
- For Chat tab, we want visibility in Dashboard like Tasks tab

### Async Mode Flow (THINK-001)

When `async_mode: true` is set on the `/task` endpoint:

1. Backend creates execution record, acquires capacity slot
2. Sets execution status to `"running"` in database
3. Spawns `_run_async_task_with_persistence()` via `asyncio.create_task()`
4. Returns immediately with `{ status: "accepted", execution_id: "..." }`
5. Background task runs headless Claude Code and updates execution record on completion
6. Background task persists to `chat_sessions` when `save_to_session=true` (passes `user_id` + `user_email`)
7. Background task broadcasts `chat_response_ready` WebSocket event with `chat_session_id`

**Key backend code** (`src/backend/routers/chat.py:735-808`):
```python
if request.async_mode:
    db.update_execution_status(execution_id=execution_id, status="running")
    asyncio.create_task(
        _run_async_task_with_persistence(
            agent_name=name,
            request=request,
            execution_id=execution_id,
            ...
            release_slot=True,
            user_id=current_user.id,       # THINK-001
            user_email=current_user.email   # THINK-001
        )
    )
    return { "status": "accepted", "execution_id": execution_id, ... }
```

**Session persistence in background** (`_persist_chat_session`, shared by the async wrapper `_run_async_task_with_persistence` and the sync `/task` branch; guarded on a **SUCCESS** terminal — FAILED/CANCELLED turns write no session):
```python
if request.save_to_session and user_id and user_email:  # gate
  if result.status == SUCCESS:                            # only persist a completed turn
    if request.create_new_session:
        session = db.create_new_chat_session(...)
    elif request.chat_session_id:
        session = db.get_chat_session(request.chat_session_id)
        # #1444 (Security F2): a caller-supplied id must belong to this user —
        # otherwise fall through to the caller's own session (IDOR fix).
        if not session or session.user_id != user_id:
            session = db.get_or_create_chat_session(...)
    else:
        session = db.get_or_create_chat_session(...)
    db.add_chat_message(session_id=session.id, role="user", content=original_user_message)
    db.add_chat_message(session_id=session.id, role="assistant", content=sanitized_resp, ...)
    # Broadcast chat_session_id via WebSocket
```
**Fail-loud (#1444):** the persistence body is wrapped so a DB error is **not** silently swallowed — it logs at ERROR with a stack trace (message carries agent + execution_id + exception-type only, never user content — Security F3) and returns None without re-raising past the completed, billed turn. The stack trace is param-safe because the SQLAlchemy engine sets `hide_parameters=True` (`db/engine.py`) — otherwise a `DBAPIError` from the `add_chat_message` INSERT would append `[parameters: [{'content': …}]]` (the user message / assistant reply) to `str(e)`, which `exc_info=True` would then write to the log. Regression-guarded by `test_dbapi_error_params_not_leaked_by_fail_loud_log`. The sync branch then surfaces a `chat_persist_failed` marker on the response so a dropped write is visible; the async wrapper's `_on_task_done` callback surfaces unhandled errors. The in-process path is the **only** persister: the #1083 fire-and-forget callback path (which does not persist) is structurally unreachable by a manual `/task` (`ASYNC_DISPATCH_ELIGIBLE_TRIGGERS` = `{schedule, webhook}`), so callback-path persistence is deferred to the pull-mode epic (#1045/#1081). Fast CI guard: `tests/unit/test_1444_chat_session_persistence.py`.

### SSE Stream Proxy (`src/backend/routers/chat.py:1496-1563`)

The backend proxies SSE streams from agent containers to the authenticated frontend:

1. Validates user auth via `get_current_user`
2. Opens `httpx.AsyncClient.stream("GET", agent_url)` to agent container
3. Yields chunks directly to client as `StreamingResponse`
4. Handles connection errors by sending `{type: "error"}` + `{type: "stream_end"}`

### Context Building

Since `/task` is stateless, ChatPanel builds conversation context:

```javascript
const buildContextPrompt = (userMessage) => {
  if (messages.value.length === 0) return userMessage

  let context = '### Previous conversation:\n\n'
  for (const msg of messages.value.slice(-20)) {
    const role = msg.role === 'user' ? 'User' : 'Assistant'
    context += `${role}: ${msg.content}\n\n`
  }
  context += `### Current message:\n\nUser: ${userMessage}`
  return context
}
```

## Dynamic Thinking Status (THINK-001)

### Overview

Instead of showing a static "Thinking..." indicator while waiting for the agent, the Chat tab now shows dynamic status labels that reflect what the agent is actually doing: "Reading file...", "Searching code...", "Editing code...", etc. This is powered by SSE streaming of Claude Code's `stream-json` output events.

### Architecture

```
ChatPanel.sendMessage()
    |
    +--> POST /api/agents/{name}/task  {async_mode: true}
    |       Returns: { execution_id }
    |
    +--> subscribeToStream(executionId)
    |       |
    |       +--> fetch /api/agents/{name}/executions/{id}/stream  (SSE)
    |               |
    |               +--> Backend proxy --> Agent container SSE endpoint
    |                       |
    |                       +--> ProcessRegistry.subscribe_logs(id) --> asyncio.Queue
    |                               |
    |                               +--> Claude Code subprocess (stream-json output)
    |
    +--> pollExecution(executionId)  (5s intervals, up to 30 min)
            |
            +--> GET /api/agents/{name}/executions/{id}
            |       Returns execution status + response when complete
            |
            +--> On completion: closeSSE(), add response to messages
```

### Status Label Mapping (`src/frontend/src/utils/execution-status.js`)

The `getStatusFromStreamEvent()` function maps Claude Code stream-json events to labels:

| Event Content Block | Status Label |
|---------------------|-------------|
| `type: "init"` | "Starting session..." |
| `type: "thinking"` | "Thinking..." |
| `type: "text"` | "Responding..." |
| `type: "tool_result"` | "Processing results..." |
| `tool_use` name = `Read` | "Reading file..." |
| `tool_use` name = `Grep` | "Searching code..." |
| `tool_use` name = `Glob` | "Finding files..." |
| `tool_use` name = `Bash` | "Running command..." |
| `tool_use` name = `Edit` | "Editing code..." |
| `tool_use` name = `Write` | "Writing file..." |
| `tool_use` name = `WebSearch` | "Searching web..." |
| `tool_use` name = `WebFetch` | "Fetching page..." |
| `tool_use` name = `Task` | "Delegating to agent..." |
| `tool_use` name = `NotebookEdit` | "Editing notebook..." |
| `tool_use` name starts with `mcp__` | "Using {server}..." |
| Unknown `tool_use` | "Working..." |

### Anti-Flicker Mechanism

Labels change rapidly during fast tool sequences. Two mechanisms prevent flickering:

1. **Minimum display time** (`MIN_LABEL_DISPLAY_MS = 500`):
   - Each label must display for at least 500ms before being replaced
   - If a new label arrives before 500ms elapsed, it is scheduled via `setTimeout`
   - `labelTimer` tracks the pending scheduled update
   - `lastLabelTime` tracks when the current label was set

   ```javascript
   // ChatPanel.vue:441-461
   const updateLoadingText = (newText) => {
     const elapsed = Date.now() - lastLabelTime
     if (elapsed < MIN_LABEL_DISPLAY_MS) {
       clearTimeout(labelTimer)
       labelTimer = setTimeout(() => {
         loadingText.value = newText
         lastLabelTime = Date.now()
       }, MIN_LABEL_DISPLAY_MS - elapsed)
     } else {
       loadingText.value = newText
       lastLabelTime = Date.now()
     }
     resetHeartbeat()
   }
   ```

2. **Heartbeat timeout** (`HEARTBEAT_TIMEOUT_MS = 10000`):
   - If no SSE events arrive for 10 seconds, falls back to "Working..."
   - Prevents a stale specific label from lingering during long operations
   - Timer is reset on every new event via `resetHeartbeat()`

   ```javascript
   // ChatPanel.vue:464-471
   const resetHeartbeat = () => {
     clearTimeout(heartbeatTimer)
     heartbeatTimer = setTimeout(() => {
       if (loading.value) loadingText.value = 'Working...'
     }, HEARTBEAT_TIMEOUT_MS)
   }
   ```

### SSE Stream Processing (`ChatPanel.vue:488-558`)

The SSE connection uses `fetch` with `ReadableStream` (not `EventSource`) because `EventSource` does not support custom authorization headers.

```javascript
const subscribeToStream = (executionId) => {
  closeSSE()
  lastLabelTime = 0
  resetHeartbeat()

  fetch(url, {
    headers: { 'Authorization': `Bearer ${token}`, 'Accept': 'text/event-stream' }
  }).then(response => {
    const reader = response.body.getReader()
    streamReader = reader
    const decoder = new TextDecoder()
    let buffer = ''

    function processStream() {
      reader.read().then(({ done, value }) => {
        if (done) { closeSSE(); return }
        buffer += decoder.decode(value, { stream: true })

        const lines = buffer.split('\n')
        buffer = lines.pop() || ''

        for (const line of lines) {
          if (line.startsWith('data: ')) {
            const data = JSON.parse(line.slice(6))
            if (data.type === 'stream_end') { closeSSE(); return }
            const status = getStatusFromStreamEvent(data)
            if (status) updateLoadingText(status)
          }
        }
        processStream()
      })
    }
    processStream()
  })
}
```

### Execution Polling (`ChatPanel.vue:562-586`)

Parallel to the SSE subscription, the frontend polls for execution completion:

- **Interval**: 5 seconds
- **Max duration**: 30 minutes (360 attempts)
- **Terminal statuses**: `success`, `failed`, `cancelled`
- **On success**: adds assistant response to `messages[]`
- **On failure/cancel**: sets `error` ref

```javascript
const pollExecution = async (executionId) => {
  const maxAttempts = 360
  while (attempts < maxAttempts && loading.value) {
    await new Promise(resolve => setTimeout(resolve, 5000))
    const response = await axios.get(`/api/agents/${name}/executions/${executionId}`)
    if (['success', 'failed', 'cancelled'].includes(response.data.status)) {
      return response.data
    }
  }
}
```

### ChatLoadingIndicator Animation (`src/frontend/src/components/chat/ChatLoadingIndicator.vue`)

The indicator shows three bouncing dots plus a dynamic text label. The `:key="text"` binding on the `<span>` triggers Vue's re-render on text change, which activates the CSS `fadeIn` animation:

```html
<span class="text-sm text-gray-500 status-text" :key="text">{{ text }}</span>
```

```css
.status-text {
  animation: fadeIn 0.3s ease-in-out;
}

@keyframes fadeIn {
  from { opacity: 0; transform: translateY(2px); }
  to   { opacity: 1; transform: translateY(0);   }
}
```

The `:key` binding is critical -- it forces Vue to destroy and recreate the `<span>` element whenever `text` changes, retriggering the CSS animation each time.

## Data Flow

```
User types message
    |
    v
ChatPanel pushes { role, content, timestamp: new Date().toISOString() } to messages[]
    |
    v
ChatPanel prepends session history to message (buildContextPrompt)
    |
    v
POST /api/agents/{name}/task (async_mode=true, save_to_session=true, chat_session_id=...)
    |
    +---> Backend creates execution record (status: "running")
    +---> Backend acquires capacity slot (CAPACITY-001)
    +---> Backend spawns _run_async_task_with_persistence()
    +---> Returns immediately: { status: "accepted", execution_id }
    |
    v
Frontend receives execution_id
    |
    +---> subscribeToStream(executionId)
    |       |
    |       +---> SSE: GET /api/agents/{name}/executions/{id}/stream
    |       |       Backend proxies to agent container
    |       |       Agent streams Claude Code stream-json events
    |       |
    |       +---> Events arrive --> getStatusFromStreamEvent()
    |       |       Maps to: "Reading file...", "Searching code...", etc.
    |       |
    |       +---> updateLoadingText() (500ms min display, 10s heartbeat)
    |               Updates loadingText ref --> ChatMessages --> ChatLoadingIndicator
    |
    +---> pollExecution(executionId) (every 5s)
            |
            +---> Terminal status detected (success/failed/cancelled)
            |
            v
    closeSSE()
    |
    +---> On success: add assistant message to messages[]
    +---> On failure: set error message
    |
    v
Background task completes:
    +---> schedule_executions table updated (status, response)
    +---> chat_sessions table (session created/updated)
    +---> chat_messages table (user + assistant messages persisted)
    +---> agent_activities table (Dashboard tracking)
    +---> WebSocket broadcast: { type: "chat_response_ready", execution_id, chat_session_id }
    +---> Capacity slot released (CAPACITY-001)
    |
    v
Frontend refreshes session list (loadSessions)
    +---> Session appears in dropdown (survives page refresh)
    +---> Dashboard shows activity in timeline
```

### Session Persistence Parameters

| Parameter | Type | Purpose |
|-----------|------|---------|
| `model` | string | User-selected model override (e.g., `claude-opus-4-6`). Empty = agent default. |
| `async_mode` | bool | When true, return immediately with `execution_id` for polling (THINK-001) |
| `save_to_session` | bool | When true, persist to `chat_sessions` + `chat_messages` tables |
| `user_message` | string | Original user message (without context prefix) for clean display |
| `create_new_session` | bool | When true, close existing active sessions and create new one |
| `chat_session_id` | string | Explicit chat session ID to save messages to (for continuing existing sessions) |
| `resume_session_id` | string | Claude Code session ID for `--resume` flag (EXEC-023) |

## Shared Components

The following components are shared between ChatPanel and PublicChat:

```
src/frontend/src/components/chat/
├── index.js                  # Export barrel (5 lines)
├── ChatBubble.vue            # Message bubble with markdown + timestamps (61 lines)
├── ChatInput.vue             # Auto-resize textarea + send button (83 lines)
├── ChatMessages.vue          # Message list with auto-scroll (87 lines)
└── ChatLoadingIndicator.vue  # Dynamic status indicator with fade animation (51 lines)
```

### Component Details

**ChatBubble.vue**
- User messages: indigo background, white text, right-aligned (`bg-indigo-600 text-white ml-auto`)
- Assistant messages: white/gray background, markdown rendered via `marked` library
- Links open in new tab with `target="_blank"`
- Optional `timestamp` prop (string, ISO 8601). When provided, a formatted time is shown below the bubble:
  - Today's messages: time only (e.g., "3:42 PM")
  - Older messages: date + time (e.g., "Mar 14, 3:42 PM")
  - User bubbles: right-aligned timestamp; assistant bubbles: left-aligned

**ChatMessages.vue**
- Bottom-aligned layout using `min-h-full flex flex-col justify-end` (lines 2-4)
- Auto-scroll on new messages via `watch` on `messages.length`
- Passes `loadingText` prop down to `ChatLoadingIndicator` (line 28)
- Passes `msg.timestamp` to `ChatBubble` via `:timestamp="msg.timestamp"` (line 24)
- Exposes `scrollToBottom()` method for parent components
- Named slot `#empty` for custom empty state

**ChatInput.vue**
- Auto-resize textarea with max height of 150px
- Enter to submit, supports v-model
- Send button disabled when empty or loading
- Exposes `focus()` method

**ChatLoadingIndicator.vue**
- Three bouncing dots with staggered animation delays (0ms, 150ms, 300ms)
- Dynamic text via `text` prop (default: "Thinking...")
- CSS `fadeIn` animation triggered by `:key="text"` on the span element
- Fade animation: 0.3s ease-in-out, opacity 0->1 + translateY(2px)->0

## Tab Position

Chat tab appears after Tasks in the tab navigation:

```
Tasks | Chat | Dashboard | Schedules | Credentials | Skills | ...
```

## Differences from Terminal Tab

| Aspect | Chat Tab | Terminal Tab |
|--------|----------|--------------|
| Interface | Simple bubbles | Full TUI (xterm.js) |
| Execution | Headless (`/task`) async | Interactive (PTY) |
| Dashboard | Shows in timeline | Not tracked |
| Session | Switchable, persistent | Single stream |
| Model | User-selectable via ModelSelector | Per-session |
| Power | Basic chat | Full Claude Code |
| Status | Dynamic labels (THINK-001) | TUI shows directly |

## Differences from Public Chat

| Aspect | Chat Tab | Public Chat |
|--------|----------|-------------|
| Auth | JWT token | Public link token |
| API | `/api/agents/...` | `/api/public/...` |
| Sessions | Full session list | Single session per link |
| Features | Session switching | Basic only |
| Components | **SHARED** | **SHARED** |
| Execution | Async + SSE streaming | Synchronous |

## Testing

### Prerequisites
- Backend running at http://localhost:8000
- Frontend running at http://localhost
- Agent running

### Test Cases

1. **Basic Chat**
   - Navigate to agent detail, click Chat tab
   - Send a message
   - Verify response appears
   - Verify activity in Dashboard timeline

2. **Dynamic Status Labels (THINK-001)**
   - Send a message that triggers tool use (e.g., "Read the README.md file")
   - Verify loading indicator shows dynamic labels: "Thinking..." -> "Reading file..." -> "Responding..."
   - Verify labels transition with fade animation (not instant swap)
   - Verify labels persist for at least 500ms each (no flicker)
   - Verify fallback to "Working..." after 10s of no events

3. **Session Management**
   - Send multiple messages
   - Click session dropdown
   - Verify session appears in list
   - Click New Chat
   - Verify messages cleared

4. **Session Switching**
   - Create messages in one session
   - Start new chat, send messages
   - Switch back to first session
   - Verify history loads correctly

5. **Agent Not Running**
   - Stop agent
   - Navigate to Chat tab
   - Verify "Agent Not Running" message displayed

6. **Component Sharing**
   - Test public chat link
   - Verify same styling as Chat tab

## Files

### Created
- `src/frontend/src/components/chat/ChatBubble.vue`
- `src/frontend/src/components/chat/ChatInput.vue`
- `src/frontend/src/components/chat/ChatMessages.vue`
- `src/frontend/src/components/chat/ChatLoadingIndicator.vue`
- `src/frontend/src/components/chat/index.js`
- `src/frontend/src/components/ChatPanel.vue`
- `src/frontend/src/utils/execution-status.js` (THINK-001)

### Modified
- `src/frontend/src/views/AgentDetail.vue` - Added Chat tab
- `src/frontend/src/views/PublicChat.vue` - Refactored to use shared components
- `src/backend/routers/chat.py` - `_run_async_task_with_persistence` now supports `save_to_session` + `user_id`/`user_email` args; SSE stream proxy endpoint; both async and sync paths use explicit `chat_session_id` when provided
- `src/backend/models.py` - `ParallelTaskRequest.async_mode` and `chat_session_id` fields
- `src/backend/db/chat.py` - `get_chat_messages()` uses subquery for correct ASC ordering

## Related Flows

- **[parallel-headless-execution.md](parallel-headless-execution.md)** - Core `/task` endpoint with `save_to_session` and `async_mode` parameter documentation
- **[tasks-tab.md](tasks-tab.md)** - Similar headless execution pattern (same `/task` endpoint)
- **[public-agent-links.md](public-agent-links.md)** - Public chat (shares components)
- **[persistent-chat-tracking.md](persistent-chat-tracking.md)** - Session API documentation (`chat_sessions`, `chat_messages` tables)
- **[execution-queue.md](execution-queue.md)** - Task execution flow
- **[continue-execution-as-chat.md](continue-execution-as-chat.md)** - Resume executions as chat via `resume_session_id` (EXEC-023)
- **[parallel-capacity.md](parallel-capacity.md)** - Slot management used by async mode (CAPACITY-001)

## Resume Mode (EXEC-023 Integration)

ChatPanel supports resuming executions as chat via the "Continue as Chat" feature. When navigating with `resumeSessionId` query parameter:

1. **Watch handler** (lines 715-725) enters resume mode:
   - Clears messages
   - Sets `resumeSessionIdLocal`
   - Sets `resumeBannerDismissed = false`
   - Displays resume banner

2. **Session auto-select prevention** (line 345):
   ```javascript
   // Fix: Don't auto-select session when in resume mode
   if (autoSelect && sessions.value.length > 0 && !currentSessionId.value &&
       messages.value.length === 0 && !isResumeMode.value) {
   ```

3. **ALL messages** include `resume_session_id` in payload (lines 625-629):
   ```javascript
   // EXEC-023: Include resume_session_id for ALL messages in resume mode
   // The /task endpoint is stateless - it doesn't use --continue.
   // We must keep passing resume_session_id so Claude Code uses --resume for every message.
   if (resumeSessionIdLocal.value) {
     payload.resume_session_id = resumeSessionIdLocal.value
     // Note: We intentionally do NOT clear resumeSessionIdLocal here.
   }
   ```

4. **Resume mode state** (lines 291-296):
   ```javascript
   const resumeSessionIdLocal = ref(null)
   const resumeExecutionIdLocal = ref(null)
   const resumeBannerDismissed = ref(false)  // Separate flag for banner visibility
   const isResumeMode = computed(() => !!resumeSessionIdLocal.value && !resumeBannerDismissed.value)
   ```

   **Key Design**: `resumeSessionIdLocal` persists for the entire session. Only the banner can be dismissed (via `resumeBannerDismissed`). This ensures every message uses `--resume` for context continuity.

5. **Resume mode ends** when:
   - User clicks "New Chat" (clears `resumeSessionIdLocal`)
   - User selects a different session from dropdown (clears `resumeSessionIdLocal`)
   - User navigates away from Chat tab

**Bug Fixed (2026-02-21)**: Previously, `resumeSessionIdLocal` was cleared after the first message. Since `/task` is stateless and doesn't use `--continue`, subsequent messages lost context. Fix: Keep `resumeSessionIdLocal` for all messages.

See [continue-execution-as-chat.md](continue-execution-as-chat.md) for complete flow.

## Side Effects

- **WebSocket**: `{ type: "chat_response_ready", execution_id, agent_name, chat_session_id, timestamp }` - broadcast when async task completes with `save_to_session=true`
- **Activity**: `CHAT_START` activity tracked in `agent_activities` table via `activity_service`
- **Capacity Slot**: Acquired on task submit, released on completion via `slot_service` (CAPACITY-001)

## Error Handling

| Error Case | HTTP Status | Message |
|------------|-------------|---------|
| Agent not found | 404 | Agent not found |
| Agent not running | 503 | Agent is not running |
| At capacity | 429 | Agent at capacity (N parallel tasks) |
| No execution_id returned | N/A | Frontend error: "No execution_id returned from async task submission" |
| Poll timeout (30 min) | N/A | Frontend error: "Request timed out. Please try again." |
| SSE stream error | N/A | Logged, polling handles completion fallback |
| Session load failed | N/A | Frontend error: "Failed to load conversation history" |
| Rate/usage limit hit | N/A | Amber-styled error with "Subscription Usage Limit" header (see below) |

### Rate Limit Error Styling

Rate limit and subscription usage errors are visually distinguished from generic errors. A computed property `isRateLimitError` (`src/frontend/src/components/ChatPanel.vue:261-265`) detects these by checking if `error.value` contains any of: `"usage limit"`, `"rate limit"`, `"out of extra usage"`, or `"out of usage"` (case-insensitive).

When detected (lines 147-150):
- Error box uses **amber** styling (`bg-amber-100`, `border-amber-200`, `text-amber-600`) instead of the default **red** styling
- A bold header **"Subscription Usage Limit"** is displayed above the error message text
- This helps users understand the error is a billing/quota issue, not a system failure

## Revision History

| Date | Change |
|------|--------|
| 2026-07-04 | **#1444: fail-loud + owner-gated `/task` session persistence**. `_persist_chat_session` (async wrapper + sync branch) no longer swallows a DB error with a `logger.warning; return None` — it logs at ERROR with a stack trace (message = agent + execution_id + exc-type only; no user content, Security F3), never re-raises past a billed turn, and the sync branch surfaces a `chat_persist_failed` marker on the response. The `chat_session_id` branch is now owner-checked (`session.user_id == caller`) before appending — a forged/leaked id falls through to the caller's own session (IDOR fix). Added a fast unit regression guard (`tests/unit/test_1444_chat_session_persistence.py`); the slow `requires_agent` integration tests (`test_dynamic_thinking_status.py::TestAsyncModeSessionPersistence`) now assert `success` before demanding a session. Triage confirmed the in-process path is correct on stock config and the #1083 callback path is unreachable by a manual `/task` (`ASYNC_DISPATCH_ELIGIBLE_TRIGGERS`={schedule,webhook}); callback-path persistence deferred to the pull-mode epic. |
| 2026-03-14 | **Model selector in Chat tab**. Added `ModelSelector` component (compact mode) above the chat input area in `ChatPanel.vue`. Users can now select a model (e.g., Opus, Sonnet, Haiku) before sending messages. Selection is persisted to `localStorage` (`trinity_chat_model`) and passed as the `model` parameter in the `/task` payload. Empty selection uses the agent's default model. Reuses the existing `ModelSelector.vue` component already used by Tasks and Schedules panels. |
| 2026-03-14 | **Message timestamps**. ChatBubble now accepts optional `timestamp` prop (line 41-44) with `formattedTime` computed (line 51-59). Today's messages show time only ("3:42 PM"), older messages show date + time ("Mar 14, 3:42 PM"). ChatMessages passes `msg.timestamp` to ChatBubble (line 24). ChatPanel includes `timestamp: new Date().toISOString()` when pushing local user/assistant messages (lines 515, 570). Backend-loaded messages include `timestamp` from `msg.timestamp` in `selectSession()` (line 306). |
| 2026-03-14 | **Bug Fix: Messages saved to wrong session**. Frontend was not passing `currentSessionId` to backend. When continuing in an existing (possibly closed) session, backend's `get_or_create_chat_session()` found a different active session or created a new one. Fix: Added `chat_session_id` field to `ParallelTaskRequest` (models.py:95). Frontend now sends `chat_session_id: currentSessionId.value` in payload (ChatPanel.vue:534). Both async (`_run_async_task_with_persistence`, chat.py:462-471) and sync (chat.py:803-810) backend paths use explicit session ID via `db.get_chat_session()`, falling back to `get_or_create_chat_session()` if not found. |
| 2026-03-14 | **Bug Fix: Message ordering wrong after switching sessions**. `get_chat_messages()` in `db/chat.py` returned `ORDER BY timestamp DESC` but frontend displayed messages as-is. New messages pushed locally appeared at the end, but after switching sessions and back, loaded messages were reversed. Fix: Changed SQL to subquery `SELECT * FROM (... ORDER BY timestamp DESC LIMIT ?) sub ORDER BY timestamp ASC` (db/chat.py:157-164) so the most recent N messages are returned in chronological order. |
| 2026-03-07 | **Rate limit error styling**. Added `isRateLimitError` computed property (line 193) that detects subscription/rate limit errors by keyword matching. Error display (lines 140-143) now uses amber styling (`bg-amber-100`, `text-amber-700`) with a "Subscription Usage Limit" header for these errors, distinguishing them from red generic errors. |
| 2026-03-03 | **THINK-001: Dynamic Thinking Status**. Refactored `sendMessage()` from synchronous POST to async mode (`async_mode=true`). Added SSE stream subscription via `fetch` + `ReadableStream` for real-time status labels. New utility `execution-status.js` maps Claude Code `stream-json` events to human-readable labels ("Reading file...", "Searching code...", etc.). Added 500ms minimum display time per label to prevent flicker. Added 10s heartbeat timeout fallback to "Working...". Added polling loop (5s intervals) for execution completion. Updated `ChatLoadingIndicator.vue` with CSS fade transition animation (`:key` binding for re-render). Backend `_run_async_task_with_persistence` now accepts `user_id`/`user_email` for session persistence in async mode. |
| 2026-02-27 | **Bug Fix (CHAT-002)**: Fixed chat message ordering issue. Replaced fragile flex spacer technique (`<div class="flex-1">` pushing content down) with `min-h-full flex flex-col justify-end` pattern in `ChatMessages.vue`. This provides reliable bottom-alignment without race conditions between spacer resizing and message rendering. |
| 2026-02-21 | **Bug Fix (EXEC-023)**: Fixed resume mode context lost after first message. Since `/task` is stateless (no `--continue`), clearing `resumeSessionIdLocal` after first message caused subsequent messages to lose context. Fix: (1) Added `resumeBannerDismissed` flag for banner-only dismissal, (2) Keep `resumeSessionIdLocal` for ALL messages, (3) `dismissResumeMode()` only hides banner (session ID persists), (4) Clear resume mode only on "New Chat" or session switch. See lines 291-296, 416-418, 625-629. |
| 2026-02-21 | **Bug Fix (EXEC-023)**: Fixed session auto-select overriding resume mode. Added `!isResumeMode.value` condition at line 345 in `loadSessions()`. Without this fix, `onMounted` -> `loadSessions()` would select an existing session even when ChatPanel was entered via "Continue as Chat" button, breaking the resume flow. |
| 2026-02-20 | Fixed session persistence - `/task` now saves to `chat_sessions` when `save_to_session=true`. Added `create_new_session` for "New Chat" button. |
| 2026-02-19 | Initial implementation (CHAT-001) |
