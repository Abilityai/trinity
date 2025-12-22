# Feature: Agent Chat

## Overview
Real-time chat interface allowing users to communicate with Claude Code agents running in isolated Docker containers. Messages are proxied through the Trinity backend to agent containers, which execute Claude Code CLI commands and stream responses back.

**Important**: All chat requests are serialized through the [Execution Queue System](execution-queue.md) to prevent parallel execution and conversation state corruption.

## User Story
As a Trinity platform user, I want to chat with my Claude Code agents so that I can interact with AI assistants that have access to configured tools and MCP servers.

## Entry Points
- **UI**: `src/frontend/src/views/AgentDetail.vue:360-494` - Chat tab content
- **API**: `POST /api/agents/{name}/chat`

---

## Frontend Layer

### Components

**AgentDetail.vue** (`src/frontend/src/views/AgentDetail.vue`)
| Line | Element | Purpose |
|------|---------|---------|
| 476-493 | Chat input + Send button | Message entry point |
| 480 | `@keypress.enter="sendChatMessage"` | Enter key handler |
| 412-473 | Chat messages container | Displays conversation history |
| 465-471 | Loading indicator | Shows `currentToolDisplay` during execution |
| 1484-1535 | `sendChatMessage()` | Main message submission handler |

**UnifiedActivityPanel.vue** (`src/frontend/src/components/UnifiedActivityPanel.vue`)
| Line | Element | Purpose |
|------|---------|---------|
| 1-206 | Full component | Real-time tool activity display |
| 9-25 | Status indicator | Shows active tool or "Idle" |
| 58-67 | Tool chips | Sorted tool counts display |
| 70-134 | Expanded timeline | Detailed tool execution history |

### State Management (`src/frontend/src/stores/agents.js`)

| Line | Action | Purpose |
|------|--------|---------|
| 161-168 | `sendChatMessage(name, message)` | POST to `/api/agents/{name}/chat` |
| 170-176 | `getChatHistory(name)` | GET chat history from agent |
| 178-184 | `getSessionInfo(name)` | GET session info (tokens, cost) |
| 186-192 | `clearSession(name)` | DELETE `/api/agents/{name}/chat/history` |

### API Calls
```javascript
// Send message
const response = await axios.post(`/api/agents/${name}/chat`,
  { message },
  { headers: authStore.authHeader }
)
return response.data
```

---

## Backend Layer

### Endpoints (`src/backend/routers/chat.py`)

| Line | Endpoint | Method | Purpose |
|------|----------|--------|---------|
| 50-294 | `/api/agents/{name}/chat` | POST | Send chat message |
| 296-324 | `/api/agents/{name}/chat/history` | GET | Get chat history |
| 366-394 | `/api/agents/{name}/chat/session` | GET | Get session info |
| 327-363 | `/api/agents/{name}/chat/history` | DELETE | Clear/reset session |
| 399-441 | `/api/agents/{name}/activity` | GET | Get activity summary |

### Chat Endpoint Flow (`routers/chat.py:50-294`)
```python
@router.post("/{name}/chat")
async def chat_with_agent(
    name: str,
    request: ChatMessageRequest,
    current_user: User = Depends(get_current_user),
    x_source_agent: Optional[str] = Header(None)  # Agent-to-agent detection
):
    # 1. Lookup container from Docker
    container = get_agent_container(name)

    # 2. Validate container is running
    if container.status != "running":
        raise HTTPException(status_code=503, detail="Agent is not running")

    # 2.5. EXECUTION QUEUE - Prevent parallel execution
    source = ExecutionSource.AGENT if x_source_agent else ExecutionSource.USER
    queue = get_execution_queue()
    execution = queue.create_execution(
        agent_name=name,
        message=request.message,
        source=source,
        source_agent=x_source_agent,
        source_user_id=str(current_user.id),
        source_user_email=current_user.email or current_user.username
    )

    try:
        queue_result, execution = await queue.submit(execution, wait_if_busy=True)
    except QueueFullError as e:
        raise HTTPException(status_code=429, detail={
            "error": "Agent queue is full",
            "queue_length": e.queue_length,
            "retry_after": 30
        })

    # 3. AGENT COLLABORATION DETECTION (Lines 107-130)
    collaboration_activity_id = None
    if x_source_agent:
        # Broadcast real-time collaboration event
        await broadcast_collaboration_event(
            source_agent=x_source_agent,
            target_agent=name,
            action="chat"
        )

        # Track collaboration activity in database
        collaboration_activity_id = await activity_service.track_activity(
            agent_name=x_source_agent,  # Activity belongs to source agent
            activity_type=ActivityType.AGENT_COLLABORATION,
            user_id=current_user.id,
            triggered_by="agent",
            details={
                "source_agent": x_source_agent,
                "target_agent": name,
                "action": "chat",
                "message_preview": request.message[:100]
            }
        )

    # 4. Get or create chat session (persistent tracking)
    session = db.get_or_create_chat_session(
        agent_name=name,
        user_id=current_user.id,
        user_email=current_user.email or current_user.username
    )

    # 5. Track chat start activity (unified activity stream)
    chat_activity_id = await activity_service.track_activity(
        agent_name=name,
        activity_type=ActivityType.CHAT_START,
        user_id=current_user.id,
        triggered_by="agent" if x_source_agent else "user",
        parent_activity_id=collaboration_activity_id,  # Link to collaboration
        details={
            "message_preview": request.message[:100],
            "source_agent": x_source_agent
        }
    )

    # 6. Log user message to database
    user_message = db.add_chat_message(
        session_id=session.id,
        agent_name=name,
        user_id=current_user.id,
        user_email=current_user.email or current_user.username,
        role="user",
        content=request.message
    )

    # 7. Proxy to agent's internal server via Docker network
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"http://agent-{name}:8000/api/chat",
            json={"message": request.message, "stream": False},
            timeout=300.0  # 5 minute timeout
        )

    # 8. Log assistant response with observability data
    assistant_message = db.add_chat_message(
        session_id=session.id,
        agent_name=name,
        user_id=current_user.id,
        user_email=current_user.email or current_user.username,
        role="assistant",
        content=response_data.get("response", ""),
        cost=metadata.get("cost_usd"),
        context_used=session_data.get("context_tokens"),
        context_max=session_data.get("context_window"),
        tool_calls=json.dumps(execution_log) if execution_log else None,
        execution_time_ms=execution_time_ms
    )

    # 9. Track individual tool calls (granular activity tracking)
    for tool_call in execution_log:
        await activity_service.track_activity(
            agent_name=name,
            activity_type=ActivityType.TOOL_CALL,
            user_id=current_user.id,
            parent_activity_id=chat_activity_id,  # Link to parent
            related_chat_message_id=assistant_message.id,
            details={
                "tool_name": tool_call.get("tool", "unknown"),
                "duration_ms": tool_call.get("duration_ms"),
                "success": tool_call.get("success", True)
            }
        )

    # 10. Complete chat activity
    await activity_service.complete_activity(
        activity_id=chat_activity_id,
        status="completed",
        details={
            "context_used": session_data.get("context_tokens"),
            "cost_usd": metadata.get("cost_usd"),
            "tool_count": len(execution_log)
        }
    )

    # 11. Log audit event
    await log_audit_event(
        event_type="agent_interaction",
        action="chat",
        user_id=current_user.username,
        agent_name=name
    )

    return response.json()
```

---

## Agent Layer

> **Architecture Change (2025-12-06)**: The agent-server has been refactored from a monolithic file into a modular package structure at `docker/base-image/agent_server/`.

### Modular Structure

| Module | Purpose |
|--------|---------|
| `agent_server/routers/chat.py` | Chat endpoints (POST /api/chat, GET /api/chat/history, etc.) |
| `agent_server/services/claude_code.py` | Claude Code CLI execution with streaming |
| `agent_server/state.py` | AgentState class for session management |
| `agent_server/models.py` | Pydantic models (ChatMessage, ExecutionLogEntry, etc.) |

### Endpoints (`agent_server/routers/chat.py`)

| Line | Endpoint | Purpose |
|------|----------|---------|
| 18-80 | `POST /api/chat` | Claude Code execution with lock |
| 83-86 | `GET /api/chat/history` | Get conversation history |
| 89-102 | `GET /api/chat/session` | Get session info (tokens, cost) |
| 138-151 | `DELETE /api/chat/history` | Clear/reset session |

### Claude Code Execution (`agent_server/services/claude_code.py:312-450`)
```python
async def execute_claude_code(prompt: str, stream: bool = False, model: Optional[str] = None):
    # Build command
    cmd = ["claude", "--print", "--output-format", "stream-json",
           "--verbose", "--dangerously-skip-permissions"]

    # Add MCP config if .mcp.json exists
    mcp_config_path = Path.home() / ".mcp.json"
    if mcp_config_path.exists():
        cmd.extend(["--mcp-config", str(mcp_config_path)])

    # Add model selection if set
    if agent_state.current_model:
        cmd.extend(["--model", agent_state.current_model])

    # Use --continue flag for subsequent messages
    if agent_state.session_started:
        cmd.append("--continue")

    # Use Popen for real-time streaming
    process = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, ...)

    # Read output in thread pool (allows activity polling during execution)
    loop = asyncio.get_event_loop()
    stderr_output, return_code = await loop.run_in_executor(_executor, read_subprocess_output)
```

### Stream-JSON Parsing (`agent_server/services/claude_code.py:33-173`)

Claude Code outputs one JSON object per line:
```json
{"type": "init", "session_id": "abc123"}
{"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "...", "name": "Read", "input": {...}}]}}
{"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "...", "content": [...]}]}}
{"type": "result", "total_cost_usd": 0.003, "duration_ms": 1234, "usage": {...}}
```

### Session State (`agent_server/state.py:15-91`)
```python
class AgentState:
    def __init__(self):
        self.conversation_history: List[ChatMessage] = []
        self.session_started = False
        self.session_total_cost: float = 0.0
        self.session_context_tokens: int = 0
        self.session_context_window: int = 200000
        self.current_model: Optional[str] = os.getenv("CLAUDE_MODEL", None)
        self.session_activity = {...}  # Real-time tool tracking
```

### Execution Lock (`agent_server/services/claude_code.py:23-30`)
```python
# Thread pool for running blocking subprocess operations
# max_workers=1 ensures only one execution at a time within this container
_executor = ThreadPoolExecutor(max_workers=1)

# Asyncio lock for execution serialization (safety net for parallel request prevention)
_execution_lock = asyncio.Lock()
```

---

## Data Flow Diagram

```
+-----------------------------------------------------------------------+
|                        FRONTEND (Vue.js)                               |
|  AgentDetail.vue  ->  agents.js store  ->  axios.post(/api/.../chat)  |
+----------------------------------+------------------------------------+
                                   |
                                   v
+-----------------------------------------------------------------------+
|                       BACKEND (FastAPI)                                |
|  routers/chat.py:50  ->  httpx proxy  ->  http://agent-{name}:8000/api/chat |
+----------------------------------+------------------------------------+
                                   | Docker Internal Network
                                   v
+-----------------------------------------------------------------------+
|                    AGENT CONTAINER                                     |
|  agent_server/routers/chat.py  ->  services/claude_code.py            |
|  ThreadPoolExecutor reads stdout, parses stream-json, updates state   |
+-----------------------------------------------------------------------+
```

---

## Context Window Tracking

### Frontend Display (`AgentDetail.vue:370-401`)
```vue
<span class="font-mono">
  {{ formatTokenCount(sessionInfo.context_tokens) }} /
  {{ formatTokenCount(sessionInfo.context_window) }}
  ({{ sessionInfo.context_percent || 0 }}%)
</span>
```

### Token Tracking in Agent (`agent_server/routers/chat.py:46-63`)
```python
agent_state.session_total_cost += metadata.cost_usd
agent_state.session_total_output_tokens += metadata.output_tokens
# Context window usage: metadata.input_tokens should contain the complete total
# (from modelUsage.inputTokens which includes all turns and cached tokens)
# However, with --continue flag, Claude Code may sometimes report only new tokens
# Fix: Context should monotonically increase during a session, so keep the max
if metadata.input_tokens > agent_state.session_context_tokens:
    agent_state.session_context_tokens = metadata.input_tokens
elif metadata.input_tokens > 0 and metadata.input_tokens < agent_state.session_context_tokens:
    # Claude reported fewer tokens than before - likely only new input, not cumulative
    # Keep the previous (higher) value as context should only grow
    logger.warning(...)
agent_state.session_context_window = metadata.context_window
```

**Bug Fix (2025-12-11)**: Previously, context tracking incorrectly summed `input_tokens + cache_creation_tokens + cache_read_tokens`, causing context percentages >100% (e.g., 130%, 289%). The issue: `cache_creation_tokens` and `cache_read_tokens` are billing **breakdowns** (subsets) of `input_tokens`, not additional tokens. The fix uses `metadata.input_tokens` directly, which already contains the authoritative total from Claude's `modelUsage.inputTokens` (includes all conversation turns).

**Bug Fix (2025-12-19)**: Context was resetting to ~4 tokens on subsequent messages when using `--continue` flag. Root cause: Claude Code may report only new input tokens (not cumulative) for continued conversations. Fix: Context tokens now use monotonic growth - only update if new value is greater than previous. This ensures context displays consistently across a session.

---

## Session Reset

### Frontend Trigger (`AgentDetail.vue:1707-1758`)
```javascript
const startNewSession = () => {
  confirmDialog.onConfirm = async () => {
    await agentsStore.clearSession(agent.value.name)
    await agentsStore.clearSessionActivity(agent.value.name)
    chatMessages.value = []
    sessionInfo.value = { context_tokens: 0, ... }
  }
}
```

### Agent Reset (`agent_server/state.py:76-87`)
```python
def reset_session(self):
    self.conversation_history = []
    self.session_started = False
    self.session_total_cost = 0.0
    self.session_total_output_tokens = 0
    self.session_context_tokens = 0
    # Note: current_model is NOT reset - it persists until explicitly changed
    self.session_activity = self._create_empty_activity()
    self.tool_outputs = {}
```

---

## Side Effects

### 1. Persistent Chat Tracking (Database)
**Chat Sessions**: `chat_sessions` table
- Created/updated on every chat
- Tracks total cost, context usage, message count

**Chat Messages**: `chat_messages` table
- User message stored immediately
- Assistant response stored with observability data (cost, context, tool_calls)
- See: `persistent-chat-tracking.md`

### 2. Unified Activity Stream (Database + WebSocket)
**Activity Tracking**: `agent_activities` table
- `chat_start` activity created at message receive
- `tool_call` activities created for each tool (linked via parent_activity_id)
- Activities completed with duration and metadata
- See: `activity-stream.md`

**WebSocket Events**:
```json
{
  "type": "agent_activity",
  "agent_name": "my-agent",
  "activity_id": "uuid-123",
  "activity_type": "chat_start",
  "activity_state": "started",
  "action": "Processing: Hello...",
  "timestamp": "2025-12-02T10:30:00.000Z",
  "details": {"message_preview": "Hello..."}
}
```

### 3. Audit Logging (HTTP)
```python
await log_audit_event(
    event_type="agent_interaction",
    action="chat",
    user_id=current_user.username,
    agent_name=name
)
```

---

## Error Handling

| Error Case | HTTP Status | Message |
|------------|-------------|---------|
| Agent not found | 404 | "Agent not found" |
| Agent not running | 503 | "Agent is not running" |
| Queue full | 429 | "Agent queue is full" (retry_after: 30) |
| Communication failure | 503 | "Failed to communicate with agent" |
| No API key | 500 | "ANTHROPIC_API_KEY not configured" |
| Execution error | 500 | "Claude Code execution failed" |

---

## Security Considerations

1. **Internal Network Only**: Agent server not exposed externally
2. **Authentication**: All endpoints require JWT
3. **Audit Logging**: Every chat interaction logged
4. **300s Timeout**: Long-running executions limited (increased from 120s for queued execution)
5. **--dangerously-skip-permissions**: Claude runs in trusted environment

---

## Testing

**Prerequisites**:
- [ ] Agent running (from agent-lifecycle test)
- [ ] Frontend at http://localhost:3000
- [ ] ANTHROPIC_API_KEY configured
- [ ] Agent has valid credentials

**Test Steps**:

### 1. Send Chat Message
**Action**:
- Navigate to agent detail page
- Click "Chat" tab
- Type: "Hello, list the files in your workspace"
- Press Enter or click Send

**Expected**:
- Message appears in chat history
- Activity panel shows "Current Tool: Read" (or similar)
- Response streams in
- Tool chips show counts (e.g., "Read x3")

**Verify**:
- [ ] Message appears in UI immediately
- [ ] Activity panel updates in real-time
- [ ] Response received within 30s
- [ ] Context window shows tokens used (e.g., "2.5K / 200K")
- [ ] Session cost displayed (e.g., "$0.05")

### 2. Check Chat History
**Action**:
- Send another message: "What files did you find?"
- Refresh the page

**Expected**:
- Both messages and responses persist
- Context window accumulates
- Cost accumulates

**Verify**:
- [ ] All messages visible after refresh
- [ ] Token count increased
- [ ] Cost increased
- [ ] API: `GET /api/agents/{name}/chat/history` returns all messages

### 3. Clear Session
**Action**: Click "New" button, confirm

**Expected**:
- Chat cleared
- Context resets to 0
- Cost resets to $0

**Verify**:
- [ ] UI shows empty chat
- [ ] Context window: "0 / 200K"
- [ ] Session cost: "$0.00"
- [ ] Next message starts fresh conversation

### 4. Tool Execution Tracking
**Action**:
- Send: "Read the README file and summarize it"
- Watch activity panel during execution

**Expected**:
- Activity shows current tool with pulsing indicator
- Tool chips update with counts
- Timeline shows execution history

**Verify**:
- [ ] "Current Tool: Read" appears during execution
- [ ] Tool chips show counts in order
- [ ] Click tool chip -> timeline expands
- [ ] Click timeline entry -> modal shows full details

**Edge Cases**:
- [ ] Chat with stopped agent: Stop agent, try to chat (should show error)
- [ ] Long-running task: Send complex request, verify 300s timeout handling
- [ ] Context overflow: Send many messages until approaching 200K limit
- [ ] Concurrent chat: Open two browser windows, chat simultaneously

**Cleanup**:
- [ ] Clear session if needed
- [ ] Note final token/cost usage

**Last Tested**: 2025-12-20
**Tested By**: Feature flow verification
**Status**: Verified
**Issues**: None

---

## Related Flows

- **Upstream**: Agent Lifecycle (agent must be running)
- **Integrates With**:
  - Execution Queue (`execution-queue.md`) - **Critical**: All chat requests go through queue to prevent parallel execution (Added 2025-12-06)
- **Alternative Mode**:
  - Parallel Headless Execution (`parallel-headless-execution.md`) - For stateless parallel tasks, use `POST /api/agents/{name}/task` instead of `/chat`. This endpoint bypasses the execution queue and runs without `--continue` flag. (Added 2025-12-22)
- **Downstream**:
  - Activity Monitoring (`activity-monitoring.md`) - Real-time in-memory tool tracking
  - Unified Activity Stream (`activity-stream.md`) - Persistent database tracking (Req 9.7)
  - Persistent Chat Tracking (`persistent-chat-tracking.md`) - Chat history persistence
  - Activity Stream Collaboration Tracking (`activity-stream-collaboration-tracking.md`) - Agent-to-agent collaboration detection (2025-12-02)
  - Collaboration Dashboard (`collaboration-dashboard.md`) - Visualizes collaboration events
- **Related**:
  - Credential Injection (MCP tools need credentials)
  - Agent-to-Agent Collaboration (`agent-to-agent-collaboration.md`) - MCP tool triggers X-Source-Agent header

---

## Revision History

| Date | Changes |
|------|---------|
| 2025-12-20 | Verified and updated all line numbers for frontend (AgentDetail.vue), backend (routers/chat.py), and agent server (chat.py, claude_code.py, state.py). Updated timeout from 120s to 300s. Verified execution queue integration. |
| 2025-12-22 | Added reference to Parallel Headless Execution as alternative mode for stateless tasks |
| 2025-12-19 | **Bug Fix**: Fixed context resetting to ~4 tokens on subsequent messages with `--continue` flag. Implemented monotonic growth - context only increases within a session. |
| 2025-12-11 | **Critical Bug Fix**: Fixed context percentage calculation showing >100% (e.g., 130%, 289%). Changed from incorrectly summing `input_tokens + cache_creation_tokens + cache_read_tokens` to using just `metadata.input_tokens`, which already contains the authoritative total from Claude's `modelUsage.inputTokens`. Cache tokens are billing breakdowns (subsets), not additional tokens. |
| 2025-12-06 | Updated agent-server references to new modular structure (`agent_server/` package) |
| 2025-12-06 | Updated line numbers for `routers/chat.py`, `services/claude_code.py`, `state.py` |
| 2025-11-30 | Initial documentation |
