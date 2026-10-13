# Feature: Persistent Chat Session Tracking

> **Note (2025-12-25)**: The Chat tab UI has been replaced by Web Terminal ([agent-terminal.md](agent-terminal.md)).
> This persistence system still applies to:
> - Scheduled executions (cron jobs)
> - MCP `chat_with_agent` tool (agent-to-agent communication)
> - Backend `/task` endpoint executions
>
> Terminal sessions are NOT persisted to this system (they use direct PTY access).
>
> **Update (2026-02-20)**: The new authenticated Chat tab (`ChatPanel.vue`) uses the `/task` endpoint with
> `save_to_session=true` to persist messages to these same `chat_sessions` and `chat_messages` tables.
> See [authenticated-chat-tab.md](authenticated-chat-tab.md) and [parallel-headless-execution.md](parallel-headless-execution.md)
> for the Chat Session Persistence section.

> **Pull pilots (#3127)**: on an agent in `PULL_MODE_PILOT_AGENTS`, `POST /chat` (UI, MCP and agent-to-agent)
> runs through `chat_execution_service.run_pulled_chat_turn`. The turn is queued and claimed by the agent's worker;
> the Claude conversation is resumed per `chat_sessions` row from `chat_sessions.cached_claude_session_id`
> (alembic `0091_chat_session_claude_id`). `GET /chat/history` returns the caller's active session from the database,
> and `DELETE /chat/history` clears every cached id of the agent. The caller persists the assistant `chat_messages` row
> after the terminal; a 504 receipt or a backend restart loses that write.

## Overview
Database-backed chat session persistence that survives agent restarts, container deletions, and provides long-term observability. Every user message and assistant response is logged to SQLite with full metadata including costs, context usage, tool calls, and execution time. Sessions are automatically created per user+agent combination and track cumulative statistics.

## User Story
As a Trinity platform user, I want all my chat conversations with agents to be permanently stored in the database so that I can review conversation history, audit costs, analyze agent performance, and resume conversations even after restarting or recreating agents.

## Entry Points
- **UI**: ~~Chat tab~~ (DEPRECATED - replaced by Terminal tab, see [agent-terminal.md](agent-terminal.md))
- **API**: `POST /api/agents/{name}/chat` (still active for scheduled/MCP executions)
- **New APIs**:
  - `GET /api/agents/{name}/chat/history/persistent` - Retrieve persisted history
  - `GET /api/agents/{name}/chat/sessions` - List all sessions
  - `GET /api/agents/{name}/chat/sessions/{session_id}` - Session details
  - `POST /api/agents/{name}/chat/sessions/{session_id}/close` - Close session

---

## Frontend Layer

### Components

**AgentDetail.vue** (`src/frontend/src/views/AgentDetail.vue`)

> **DEPRECATED (2025-12-25)**: Chat tab has been replaced by Terminal tab. The table below is historical reference.

| Line | Element | Purpose |
|------|---------|---------|
| ~~345-498~~ | ~~Chat tab content~~ | Replaced by Terminal tab |
| ~~1484-1535~~ | ~~`sendChatMessage()`~~ | Removed from UI, API still works |
| ~~1474-1482~~ | ~~`loadChatHistory()`~~ | No longer called from UI |

**Note**: Persistent chat tracking still works for backend-initiated requests (schedules, MCP, `/task` endpoint). Terminal sessions use direct PTY and are not persisted through this system.

### State Management (`src/frontend/src/stores/agents.js`)

| Line | Action | Purpose |
|------|--------|---------|
| 161-168 | `sendChatMessage(name, message)` | POST to `/api/agents/{name}/chat` (now persists to DB) |
| 170-176 | `getChatHistory(name)` | GET in-memory history from container |

**Future Enhancement**: Add store actions for persistent history:
```javascript
async getPersistentChatHistory(name, limit = 100, userFilter = false)
async getChatSessions(name, status = null)
async getChatSessionDetail(name, sessionId, limit = 100)
async closeChatSession(name, sessionId)
```

### API Calls
```javascript
// Send message (automatically persisted)
const response = await axios.post(`/api/agents/${name}/chat`,
  { message },
  { headers: authStore.authHeader }
)

// Get persistent history (new endpoint)
const response = await axios.get(`/api/agents/${name}/chat/history/persistent`,
  {
    params: { limit: 100, user_filter: false },
    headers: authStore.authHeader
  }
)

// List sessions (new endpoint)
const response = await axios.get(`/api/agents/${name}/chat/sessions`,
  {
    params: { status: 'active' },
    headers: authStore.authHeader
  }
)
```

---

## Backend Layer

### Endpoints (`src/backend/routers/chat.py`)

| Line | Endpoint | Method | Purpose |
|------|----------|--------|---------|
| 106-356 | `/api/agents/{name}/chat` | POST | Send message + persist to DB |
| 876-915 | `/api/agents/{name}/chat/history/persistent` | GET | Get persistent history |
| 918-950 | `/api/agents/{name}/chat/sessions` | GET | List sessions for agent |
| 953-988 | `/api/agents/{name}/chat/sessions/{session_id}` | GET | Get session details |
| 991-1027 | `/api/agents/{name}/chat/sessions/{session_id}/close` | POST | Close session |

### Modified Chat Endpoint (`routers/chat.py:106-356`)

**Key Changes**:
1. Resolve agent subscription ID via `db.get_agent_subscription_id(name)`
2. Get or create chat session, passing `subscription_id`
3. Log user message to database, passing `subscription_id`
4. Proxy to agent container (unchanged)
5. Log assistant response with metadata, passing `subscription_id`

```python
@router.post("/{name}/chat")
async def chat_with_agent(
    name: str,
    request: ChatMessageRequest,
    current_user: User = Depends(get_current_user),
    x_source_agent: Optional[str] = Header(None)
):
    container = get_agent_container(name)
    if container.status != "running":
        raise HTTPException(status_code=503, detail="Agent is not running")

    # 1. Resolve subscription ID for this agent (Nevermined)
    subscription_id = db.get_agent_subscription_id(name)

    # 2. Get or create chat session for this user+agent
    session = db.get_or_create_chat_session(
        agent_name=name,
        user_id=current_user.id,
        user_email=current_user.email or current_user.username,
        subscription_id=subscription_id
    )

    # 3. Log user message to database
    user_message = db.add_chat_message(
        session_id=session.id,
        agent_name=name,
        user_id=current_user.id,
        user_email=current_user.email or current_user.username,
        role="user",
        content=request.message,
        subscription_id=subscription_id
    )

    # 4. Proxy to agent's internal server (unchanged)
    start_time = datetime.utcnow()
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"http://agent-{name}:8000/api/chat",
            json={"message": request.message, "stream": False},
            timeout=300.0
        )
        response_data = response.json()

    # 5. Extract metadata for persistence
    execution_time_ms = int((datetime.utcnow() - start_time).total_seconds() * 1000)
    metadata = response_data.get("metadata", {})
    session_data = response_data.get("session", {})
    execution_log = response_data.get("execution_log", [])
    tool_calls_json = json.dumps(execution_log) if execution_log else None

    # 6. Log assistant response with observability data
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
        tool_calls=tool_calls_json,
        execution_time_ms=execution_time_ms,
        subscription_id=subscription_id
    )

    # 7. Audit log (unchanged)
    await log_audit_event(...)

    return response_data
```

### New Persistent History Endpoint (`routers/chat.py:876-915`)

```python
@router.get("/{name}/chat/history/persistent")
async def get_persistent_chat_history(
    name: str,
    limit: int = 100,
    user_filter: bool = False,
    current_user: User = Depends(get_current_user)
):
    """
    Get persistent chat history from database.

    - Returns messages across all sessions
    - Persists through container restarts
    - Includes full observability metadata

    Parameters:
    - limit: Maximum messages to return (default 100)
    - user_filter: If true, only show current user's messages
    """
    container = get_agent_container(name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Access control: admins see all, users see own messages
    user_id_filter = None
    if user_filter or current_user.role != "admin":
        user_id_filter = current_user.id

    messages = db.get_agent_chat_history(
        agent_name=name,
        user_id=user_id_filter,
        limit=limit
    )

    return {
        "agent_name": name,
        "message_count": len(messages),
        "messages": [msg.model_dump() for msg in messages]
    }
```

### Sessions List Endpoint (`routers/chat.py:918-950`)

```python
@router.get("/{name}/chat/sessions")
async def get_agent_chat_sessions(
    name: str,
    status: str = None,  # 'active' or 'closed'
    current_user: User = Depends(get_current_user)
):
    """
    Get all chat sessions for an agent.

    Returns session metadata including:
    - Message counts
    - Total costs
    - Context usage
    - Timestamps

    Non-admin users only see their own sessions.
    """
    # Access control
    user_id_filter = None if current_user.role == "admin" else current_user.id

    sessions = db.get_agent_chat_sessions(
        agent_name=name,
        user_id=user_id_filter,
        status=status
    )

    return {
        "agent_name": name,
        "session_count": len(sessions),
        "sessions": [session.model_dump() for session in sessions]
    }
```

### Session Detail Endpoint (`routers/chat.py:953-988`)

```python
@router.get("/{name}/chat/sessions/{session_id}")
async def get_chat_session_detail(
    name: str,
    session_id: str,
    limit: int = 100,
    current_user: User = Depends(get_current_user)
):
    """Get detailed session information including all messages."""
    session = db.get_chat_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Chat session not found")

    # Verify session belongs to this agent
    if session.agent_name != name:
        raise HTTPException(status_code=403, detail="Session does not belong to this agent")

    # Access control: non-admins can only see their own sessions
    if current_user.role != "admin" and session.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="You don't have access to this session")

    messages = db.get_chat_messages(session_id, limit=limit)

    return {
        "session": session.model_dump(),
        "message_count": len(messages),
        "messages": [msg.model_dump() for msg in messages]
    }
```

### Close Session Endpoint (`routers/chat.py:991-1027`)

```python
@router.post("/{name}/chat/sessions/{session_id}/close")
async def close_chat_session(
    name: str,
    session_id: str,
    current_user: User = Depends(get_current_user)
):
    """Close a chat session (marks as closed but keeps history)."""
    session = db.get_chat_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Chat session not found")

    # Verify session belongs to this agent and user
    if session.agent_name != name:
        raise HTTPException(status_code=403, detail="Session does not belong to this agent")

    if current_user.role != "admin" and session.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="You don't have access to this session")

    success = db.close_chat_session(session_id)

    if success:
        await log_audit_event(
            event_type="agent_interaction",
            action="close_chat_session",
            user_id=current_user.username,
            agent_name=name,
            resource=f"session-{session_id}",
            result="success"
        )
        return {"status": "closed", "session_id": session_id}
    else:
        raise HTTPException(status_code=500, detail="Failed to close session")
```

---

## Database Layer

### Schema (`src/backend/database.py`)

#### Chat Sessions Table (`src/backend/db/schema.py`)

```sql
CREATE TABLE IF NOT EXISTS chat_sessions (
    id TEXT PRIMARY KEY,                  -- Unique session ID (urlsafe token)
    agent_name TEXT NOT NULL,             -- Agent name
    user_id INTEGER NOT NULL,             -- User ID (FK to users table)
    user_email TEXT NOT NULL,             -- User email for quick lookup
    started_at TEXT NOT NULL,             -- ISO timestamp of first message
    last_message_at TEXT NOT NULL,        -- ISO timestamp of most recent message
    message_count INTEGER DEFAULT 0,      -- Total messages (user + assistant)
    total_cost REAL DEFAULT 0.0,          -- Cumulative cost in USD
    total_context_used INTEGER DEFAULT 0, -- Latest context tokens used
    total_context_max INTEGER DEFAULT 200000, -- Latest context window size
    status TEXT DEFAULT 'active',         -- 'active' or 'closed'
    subscription_id TEXT,                 -- Nevermined subscription ID (nullable)
    FOREIGN KEY (user_id) REFERENCES users(id)
)

-- Indexes
CREATE INDEX idx_chat_sessions_agent ON chat_sessions(agent_name)
CREATE INDEX idx_chat_sessions_user ON chat_sessions(user_id)
CREATE INDEX idx_chat_sessions_status ON chat_sessions(status)
```

#### Chat Messages Table (`src/backend/db/schema.py`)

```sql
CREATE TABLE IF NOT EXISTS chat_messages (
    id TEXT PRIMARY KEY,                  -- Unique message ID (urlsafe token)
    session_id TEXT NOT NULL,             -- FK to chat_sessions
    agent_name TEXT NOT NULL,             -- Agent name (denormalized for queries)
    user_id INTEGER NOT NULL,             -- User ID (denormalized)
    user_email TEXT NOT NULL,             -- User email (denormalized)
    role TEXT NOT NULL,                   -- 'user' or 'assistant'
    content TEXT NOT NULL,                -- Message content
    timestamp TEXT NOT NULL,              -- ISO timestamp
    cost REAL,                            -- Cost for assistant messages (NULL for user)
    context_used INTEGER,                 -- Tokens used (assistant only)
    context_max INTEGER,                  -- Context window size (assistant only)
    tool_calls TEXT,                      -- JSON array of tool executions (assistant only)
    execution_time_ms INTEGER,            -- Execution duration (assistant only)
    subscription_id TEXT,                 -- Nevermined subscription ID (nullable)
    output_tokens INTEGER,                -- Output token count (nullable)
    FOREIGN KEY (session_id) REFERENCES chat_sessions(id),
    FOREIGN KEY (user_id) REFERENCES users(id)
)

-- Indexes
CREATE INDEX idx_chat_messages_session ON chat_messages(session_id)
CREATE INDEX idx_chat_messages_agent ON chat_messages(agent_name)
CREATE INDEX idx_chat_messages_user ON chat_messages(user_id)
CREATE INDEX idx_chat_messages_timestamp ON chat_messages(timestamp)
```

### Models (`src/backend/db_models.py`)

```python
class ChatSession(BaseModel):
    """Persistent chat session for an agent."""
    id: str
    agent_name: str
    user_id: int
    user_email: str
    started_at: datetime
    last_message_at: datetime
    message_count: int = 0
    total_cost: float = 0.0
    total_context_used: int = 0
    total_context_max: int = 200000
    status: str = "active"  # "active" or "closed"
    subscription_id: Optional[str] = None  # Nevermined subscription ID


class ChatMessage(BaseModel):
    """A single message in a chat session."""
    id: str
    session_id: str
    agent_name: str
    user_id: int
    user_email: str
    role: str  # "user" or "assistant"
    content: str
    timestamp: datetime
    # Observability (only for assistant messages)
    cost: Optional[float] = None
    context_used: Optional[int] = None
    context_max: Optional[int] = None
    tool_calls: Optional[str] = None  # JSON array
    execution_time_ms: Optional[int] = None
    subscription_id: Optional[str] = None  # Nevermined subscription ID
    output_tokens: Optional[int] = None    # Output token count
```

### Database Operations (`src/backend/db/chat.py`)

#### ChatOperations Class (`db/chat.py:15-246`)

```python
class ChatOperations:
    """Chat session and message database operations."""

    def get_or_create_chat_session(
        self, agent_name: str, user_id: int, user_email: str,
        subscription_id: Optional[str] = None
    ) -> ChatSession:
        """
        Get the active chat session for a user+agent, or create a new one.
        Returns the most recent active session if it exists.
        subscription_id is stored on the session row when provided.
        """
        # Try to find an active session for this user+agent
        # If none exists, create a new session with secrets.token_urlsafe(16)

    def add_chat_message(
        self,
        session_id: str,
        agent_name: str,
        user_id: int,
        user_email: str,
        role: str,
        content: str,
        fields: Optional[ChatMessageFields] = None,  # #1482: cost/context/tool_calls/timing/source/subscription_id/output_tokens
    ) -> ChatMessage:
        """Add a message to a chat session and update session stats."""

    def get_chat_session(self, session_id: str) -> Optional[ChatSession]:
        """Get a specific chat session by ID."""

    def get_chat_messages(self, session_id: str, limit: int = 100) -> List[ChatMessage]:
        """Get messages for a chat session (newest first)."""

    def get_agent_chat_history(
        self, agent_name: str, user_id: Optional[int] = None, limit: int = 100
    ) -> List[ChatMessage]:
        """Get chat history for an agent, newest first."""

    def get_agent_chat_sessions(
        self, agent_name: str, user_id: Optional[int] = None, status: Optional[str] = None
    ) -> List[ChatSession]:
        """Get all chat sessions for an agent."""

    def close_chat_session(self, session_id: str) -> bool:
        """Mark a chat session as closed."""

    def delete_chat_session(self, session_id: str) -> bool:
        """Delete a chat session and all its messages."""
```

---

## Data Flow Diagram

```
+----------------------------------------------------------------------+
|                         FRONTEND (Vue.js)                            |
|   User types message -> sendChatMessage() -> POST /api/agents/{}/chat  |
+--------------------------------+-------------------------------------+
                                 |
                                 v
+----------------------------------------------------------------------+
|                        BACKEND (FastAPI)                              |
|  routers/chat.py:50                                                   |
|    +- 1. Get or create session (DB)                                  |
|    +- 2. Log user message (DB)                                       |
|    +- 3. Proxy to agent container (httpx)                            |
|    +- 4. Log assistant response with metadata (DB)                   |
+--------------------------------+-------------------------------------+
                                 |
                                 v
+----------------------------------------------------------------------+
|                         DATABASE (SQLite)                             |
|  chat_sessions table:                                                 |
|    - Session metadata (costs, context, message count)                |
|  chat_messages table:                                                 |
|    - User message (content, timestamp)                               |
|    - Assistant message (content, cost, context, tool_calls, timing)  |
+----------------------------------------------------------------------+
```

---

## Message Flow Example

### 1. User Sends First Message
```
POST /api/agents/my-agent/chat
Body: {"message": "Hello, list my files"}

Backend Flow:
1. db.get_or_create_chat_session("my-agent", user_id=5, user_email="user@example.com")
   -> Returns: ChatSession(id="abc123", started_at="2025-12-01T10:00:00", message_count=0)

2. db.add_chat_message(session_id="abc123", role="user", content="Hello, list my files")
   -> Inserts user message, updates session.message_count=1

3. Proxy to agent container -> Gets response with metadata:
   {
     "response": "Here are your files:\n- file1.txt\n- file2.py",
     "execution_log": [{"type": "tool_use", "tool": "Bash", "command": "ls"}],
     "metadata": {"cost_usd": 0.003, "tool_count": 1},
     "session": {"context_tokens": 2500, "context_window": 200000}
   }

4. db.add_chat_message(
     session_id="abc123",
     role="assistant",
     content="Here are your files...",
     cost=0.003,
     context_used=2500,
     context_max=200000,
     tool_calls='[{"type":"tool_use","tool":"Bash","command":"ls"}]',
     execution_time_ms=1234
   )
   -> Inserts assistant message, updates session:
     - message_count=2
     - total_cost=0.003
     - total_context_used=2500
     - last_message_at="2025-12-01T10:00:05"
```

### 2. User Sends Follow-up Message
```
POST /api/agents/my-agent/chat
Body: {"message": "Read file1.txt"}

Backend Flow:
1. db.get_or_create_chat_session("my-agent", user_id=5, ...)
   -> Finds existing session "abc123" (same user+agent, status='active')

2. db.add_chat_message(session_id="abc123", role="user", content="Read file1.txt")
   -> message_count=3

3. Proxy to agent -> response with metadata

4. db.add_chat_message(session_id="abc123", role="assistant", ...)
   -> message_count=4, total_cost=0.006, total_context_used=4200
```

### 3. Query Persistent History
```
GET /api/agents/my-agent/chat/history/persistent?limit=100

Backend Flow:
1. db.get_agent_chat_history("my-agent", user_id=5, limit=100)
   -> Returns all messages for this user across all sessions
   -> Ordered by timestamp DESC (newest first)

Response:
{
  "agent_name": "my-agent",
  "message_count": 4,
  "messages": [
    {
      "id": "msg4",
      "session_id": "abc123",
      "role": "assistant",
      "content": "The file contains...",
      "timestamp": "2025-12-01T10:00:10",
      "cost": 0.003,
      "context_used": 4200,
      "context_max": 200000,
      "tool_calls": "[{\"type\":\"tool_use\",\"tool\":\"Read\"}]",
      "execution_time_ms": 890
    },
    {
      "id": "msg3",
      "role": "user",
      "content": "Read file1.txt",
      "timestamp": "2025-12-01T10:00:09",
      "cost": null
    },
    // ... earlier messages
  ]
}
```

---

## Access Control

### User Roles

| Role | Session Access | Message Access |
|------|----------------|----------------|
| **User** | Own sessions only | Own messages only |
| **Admin** | All sessions | All messages |
| **Agent Owner** | Currently same as User (can be enhanced) | Currently same as User |

### Endpoint-Level Access Control

```python
# Non-admins only see their own sessions
user_id_filter = None if current_user.role == "admin" else current_user.id

sessions = db.get_agent_chat_sessions(
    agent_name=name,
    user_id=user_id_filter,
    status=status
)
```

---

## Cost and Context Tracking

### Per-Message Tracking
- **User messages**: No cost/context data (NULL)
- **Assistant messages**: Full observability metadata
  - `cost`: USD cost of that specific response
  - `context_used`: Tokens used at that point in conversation
  - `context_max`: Context window size for that model
  - `tool_calls`: JSON array of all tool executions
  - `execution_time_ms`: Total response time

### Session-Level Aggregation
- `total_cost`: Sum of all assistant message costs in session
- `total_context_used`: Latest context tokens from most recent message
- `total_context_max`: Latest context window size
- `message_count`: Total user + assistant messages

### Example Query: Total Cost Per Agent
```sql
SELECT
    agent_name,
    COUNT(DISTINCT session_id) as session_count,
    SUM(total_cost) as total_cost,
    SUM(message_count) as total_messages
FROM chat_sessions
GROUP BY agent_name
ORDER BY total_cost DESC;
```

### Example Query: Most Active Users
```sql
SELECT
    user_email,
    COUNT(DISTINCT session_id) as session_count,
    SUM(message_count) as total_messages,
    SUM(total_cost) as total_cost
FROM chat_sessions
GROUP BY user_email
ORDER BY total_messages DESC
LIMIT 10;
```

---

## Side Effects

### 1. Audit Logging
Every chat interaction logged to audit service:
```python
await log_audit_event(
    event_type="agent_interaction",
    action="chat",
    user_id=current_user.username,
    agent_name=name,
    resource=f"agent-{name}",
    result="success"
)
```

### 2. Session Auto-Creation
Sessions automatically created on first message between user+agent pair. No explicit "start session" endpoint needed.

### 3. Database Updates on Every Message
- `chat_messages` table: 2 inserts per exchange (user + assistant)
- `chat_sessions` table: 2 updates per exchange (message_count, costs, timestamps)

### 4. No Impact on Container-Level History
Agent container maintains separate in-memory history for current session. Database persistence is transparent layer.

---

## Error Handling

| Error Case | HTTP Status | Message |
|------------|-------------|---------|
| Agent not found | 404 | "Agent not found" |
| Agent not running | 503 | "Agent is not running" |
| Session not found | 404 | "Chat session not found" |
| Session access denied | 403 | "You don't have access to this session" |
| Session/agent mismatch | 403 | "Session does not belong to this agent" |
| Database write failure | 500 | "Failed to persist message" |

---

## Security Considerations

1. **User Isolation**: Non-admin users only see their own messages
2. **Email-Based Tracking**: User email stored for quick filtering (even if username changes)
3. **Session Ownership**: Sessions tied to user_id with FK constraint
4. **Access Validation**: Every session access checks ownership or admin role
5. **Audit Logging**: All chat interactions logged for compliance
6. **No Credential Leakage**: Tool calls stored as JSON but credentials not included

---

## Testing

**Prerequisites**:
- [ ] Database initialized (`trinity.db` exists)
- [ ] Agent running (from agent-lifecycle test)
- [ ] User authenticated
- [ ] ANTHROPIC_API_KEY configured

**Test Steps**:

### 1. Send First Message (Session Creation)
**Action**:
```bash
# Via UI: Navigate to agent, go to Chat tab, send message
# Or via API:
curl -X POST http://localhost:8000/api/agents/my-agent/chat \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message": "Hello, list my files"}'
```

**Expected**:
- Response contains agent reply
- Database creates new session
- Two messages inserted (user + assistant)

**Verify**:
```sql
-- Check session created
SELECT * FROM chat_sessions WHERE agent_name = 'my-agent';
-- Should show: message_count=2, total_cost>0, status='active'

-- Check messages
SELECT role, content, cost, context_used
FROM chat_messages
WHERE agent_name = 'my-agent'
ORDER BY timestamp DESC;
-- Should show 2 rows: assistant (with cost/context), user (nulls)
```

### 2. Send Follow-up Message (Session Reuse)
**Action**:
```bash
curl -X POST http://localhost:8000/api/agents/my-agent/chat \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message": "What files did you find?"}'
```

**Expected**:
- Uses same session (not new)
- message_count increments
- total_cost accumulates

**Verify**:
```sql
SELECT id, message_count, total_cost, total_context_used
FROM chat_sessions
WHERE agent_name = 'my-agent';
-- Should show: message_count=4, costs accumulated
```

### 3. Get Persistent History
**Action**:
```bash
curl http://localhost:8000/api/agents/my-agent/chat/history/persistent?limit=10 \
  -H "Authorization: Bearer $TOKEN"
```

**Expected**:
- Returns all messages for current user
- Includes metadata (costs, context, tool_calls)
- Ordered newest first

**Verify**:
- [ ] Response contains all 4 messages
- [ ] Assistant messages have cost/context data
- [ ] User messages have cost=null
- [ ] tool_calls is valid JSON array

### 4. List Sessions
**Action**:
```bash
curl http://localhost:8000/api/agents/my-agent/chat/sessions \
  -H "Authorization: Bearer $TOKEN"
```

**Expected**:
- Returns session metadata
- Shows message_count, total_cost, timestamps

**Verify**:
- [ ] session_count = 1
- [ ] Session has correct message_count and total_cost
- [ ] Status is "active"

### 5. Get Session Details
**Action**:
```bash
SESSION_ID=$(sqlite3 ~/trinity-data/trinity.db \
  "SELECT id FROM chat_sessions WHERE agent_name='my-agent' LIMIT 1")

curl http://localhost:8000/api/agents/my-agent/chat/sessions/$SESSION_ID \
  -H "Authorization: Bearer $TOKEN"
```

**Expected**:
- Returns session object + all messages
- Message list is complete with metadata

**Verify**:
- [ ] Session metadata matches
- [ ] All 4 messages returned
- [ ] Messages have full observability data

### 6. Close Session
**Action**:
```bash
curl -X POST http://localhost:8000/api/agents/my-agent/chat/sessions/$SESSION_ID/close \
  -H "Authorization: Bearer $TOKEN"
```

**Expected**:
- Session status changes to "closed"
- History remains accessible
- Next message creates new session

**Verify**:
```sql
SELECT status FROM chat_sessions WHERE id = '$SESSION_ID';
-- Should show: status='closed'
```

### 7. Restart Agent and Verify Persistence
**Action**:
```bash
# Stop and remove agent container
docker stop agent-my-agent
docker rm agent-my-agent

# Recreate and start agent
curl -X POST http://localhost:8000/api/agents/my-agent/start \
  -H "Authorization: Bearer $TOKEN"

# Query persistent history
curl http://localhost:8000/api/agents/my-agent/chat/history/persistent \
  -H "Authorization: Bearer $TOKEN"
```

**Expected**:
- All previous messages still in database
- Container has fresh in-memory history
- Persistent history survives restart

**Verify**:
- [ ] GET /chat/history/persistent returns all messages
- [ ] Container's GET /chat/history is empty (fresh)
- [ ] Next message starts new in-memory session but appends to DB

### 8. Multi-User Isolation
**Action**:
```bash
# User 1 sends message
curl -X POST http://localhost:8000/api/agents/my-agent/chat \
  -H "Authorization: Bearer $USER1_TOKEN" \
  -d '{"message": "User 1 message"}'

# User 2 sends message
curl -X POST http://localhost:8000/api/agents/my-agent/chat \
  -H "Authorization: Bearer $USER2_TOKEN" \
  -d '{"message": "User 2 message"}'

# User 1 queries history
curl http://localhost:8000/api/agents/my-agent/chat/history/persistent \
  -H "Authorization: Bearer $USER1_TOKEN"
```

**Expected**:
- User 1 only sees own messages
- User 2 only sees own messages
- Admin sees all messages

**Verify**:
```sql
SELECT user_email, COUNT(*) FROM chat_messages
WHERE agent_name='my-agent'
GROUP BY user_email;
-- Should show 2 users with separate message counts
```

**Edge Cases**:
- [ ] Send message with stopped agent -> 503 error, no DB write
- [ ] Query history for non-existent agent -> 404
- [ ] Non-admin tries to access another user's session -> 403
- [ ] Close already-closed session -> succeeds (idempotent)
- [ ] Very long conversation (>100 messages) -> pagination works
- [ ] Agent deleted -> sessions remain (orphaned but queryable)

**Cleanup**:
```sql
-- Clean up test data
DELETE FROM chat_messages WHERE agent_name = 'my-agent';
DELETE FROM chat_sessions WHERE agent_name = 'my-agent';
```

**Last Tested**: 2025-12-20
**Tested By**: Production deployment
**Status**: Working (deployed and tested)
**Issues**: None

---

## Performance Considerations

### Database Writes
- **Per chat exchange**: 2 message inserts + 2 session updates
- **Volume**: Low (human conversation pace, not high-frequency)
- **Indexes**: Optimized for common queries (agent_name, user_id, timestamp)

### Query Optimization
```sql
-- Efficient queries thanks to indexes
SELECT * FROM chat_messages
WHERE agent_name = ? AND user_id = ?
ORDER BY timestamp DESC
LIMIT 100;
-- Uses: idx_chat_messages_agent, idx_chat_messages_user, idx_chat_messages_timestamp
```

### Future Enhancements
1. **Pagination**: Offset-based pagination for large histories
2. **Date Range Filters**: Query messages by date range
3. **Search**: Full-text search across message content
4. **Archival**: Move old sessions to separate table/file
5. **Analytics**: Pre-aggregated cost/usage tables

---

## Related Flows

- **Upstream**:
  - Agent Lifecycle (agent must exist and be running)
  - Email Authentication (user must be authenticated)
- **Downstream**:
  - Activity Monitoring (tool calls stored in messages)
  - Cost Auditing (session costs tracked)
- **Related**:
  - [authenticated-chat-tab.md](authenticated-chat-tab.md) - Chat tab uses `/task` endpoint with `save_to_session=true` to persist to these tables
  - [parallel-headless-execution.md](parallel-headless-execution.md) - Documents `save_to_session`, `user_message`, `create_new_session` parameters
  - Agent Chat (in-memory session, non-persistent)
  - Scheduling (scheduled executions also tracked separately)

---

## Session Management

This section documents the session management capabilities for organizing and reviewing past conversations.

### User Stories

| ID | Story | Status |
|----|-------|--------|
| EXEC-019 | As a user, I want to list all chat sessions so I can review past conversations | Backend API Complete |
| EXEC-020 | As a user, I want to view specific session messages so I can see full conversation history | Backend API Complete |
| EXEC-021 | As a user, I want to close a session so I can organize my conversation history | Backend API Complete |

### Entry Points

- **API Only** (No frontend UI currently):
  - `GET /api/agents/{name}/chat/sessions` - List all sessions
  - `GET /api/agents/{name}/chat/sessions/{session_id}` - View session details
  - `POST /api/agents/{name}/chat/sessions/{session_id}/close` - Close session

### Backend Layer

#### List Sessions Endpoint (`src/backend/routers/chat.py:967-999`)

Lists all chat sessions for an agent with filtering options.

```python
@router.get("/{name}/chat/sessions")
async def get_agent_chat_sessions(
    name: str,
    status: str = None,  # 'active' or 'closed'
    current_user: User = Depends(get_current_user)
):
```

**Request**: `GET /api/agents/{name}/chat/sessions?status=active`

**Response**:
```json
{
  "agent_name": "my-agent",
  "session_count": 2,
  "sessions": [
    {
      "id": "abc123...",
      "agent_name": "my-agent",
      "user_id": 5,
      "user_email": "user@example.com",
      "started_at": "2025-12-01T10:00:00",
      "last_message_at": "2025-12-01T11:30:00",
      "message_count": 12,
      "total_cost": 0.045,
      "total_context_used": 15000,
      "total_context_max": 200000,
      "status": "active"
    }
  ]
}
```

#### View Session Details Endpoint (`src/backend/routers/chat.py:1002-1037`)

Retrieves detailed session information including all messages.

```python
@router.get("/{name}/chat/sessions/{session_id}")
async def get_chat_session_detail(
    name: str,
    session_id: str,
    limit: int = 100,
    current_user: User = Depends(get_current_user)
):
```

**Request**: `GET /api/agents/{name}/chat/sessions/{session_id}?limit=50`

**Response**:
```json
{
  "session": {
    "id": "abc123...",
    "agent_name": "my-agent",
    "user_id": 5,
    "user_email": "user@example.com",
    "started_at": "2025-12-01T10:00:00",
    "last_message_at": "2025-12-01T11:30:00",
    "message_count": 12,
    "total_cost": 0.045,
    "status": "active"
  },
  "message_count": 12,
  "messages": [
    {
      "id": "msg1...",
      "session_id": "abc123...",
      "role": "assistant",
      "content": "Response content...",
      "timestamp": "2025-12-01T11:30:00",
      "cost": 0.003,
      "context_used": 15000,
      "tool_calls": "[{\"tool\": \"Read\", ...}]",
      "execution_time_ms": 1234
    },
    {
      "id": "msg2...",
      "session_id": "abc123...",
      "role": "user",
      "content": "User message...",
      "timestamp": "2025-12-01T11:29:55",
      "cost": null
    }
  ]
}
```

#### Close Session Endpoint (`src/backend/routers/chat.py:1040-1067`)

Marks a session as closed (history preserved, new messages go to new session).

```python
@router.post("/{name}/chat/sessions/{session_id}/close")
async def close_chat_session(
    name: str,
    session_id: str,
    current_user: User = Depends(get_current_user)
):
```

**Request**: `POST /api/agents/{name}/chat/sessions/{session_id}/close`

**Response**:
```json
{
  "status": "closed",
  "session_id": "abc123..."
}
```

### Database Layer (`src/backend/db/chat.py`)

#### ChatOperations Methods

| Method | Line | Purpose |
|--------|------|---------|
| `get_agent_chat_sessions()` | 194-221 | List sessions with optional user/status filter |
| `get_chat_session()` | 145-151 | Get single session by ID |
| `get_chat_messages()` | 153-163 | Get messages for a session |
| `close_chat_session()` | 223-231 | Set session status to 'closed' |
| `delete_chat_session()` | 233-245 | Delete session and all messages (not exposed via API) |

### Access Control

| Role | List Sessions | View Session | Close Session |
|------|--------------|--------------|---------------|
| **User** | Own sessions only | Own sessions only | Own sessions only |
| **Admin** | All sessions | All sessions | All sessions |

Access control is enforced at the endpoint level:
```python
# Non-admins only see their own sessions
user_id_filter = None if current_user.role == "admin" else current_user.id
```

### Frontend Layer (Not Yet Implemented)

**Current Status**: Backend APIs are complete, no frontend UI exists.

**Planned Store Actions** (`src/frontend/src/stores/agents.js`):
```javascript
// Future implementation
async getChatSessions(name, status = null) {
  const authStore = useAuthStore()
  const response = await axios.get(`/api/agents/${name}/chat/sessions`, {
    params: { status },
    headers: authStore.authHeader
  })
  return response.data
}

async getChatSessionDetail(name, sessionId, limit = 100) {
  const authStore = useAuthStore()
  const response = await axios.get(
    `/api/agents/${name}/chat/sessions/${sessionId}`,
    {
      params: { limit },
      headers: authStore.authHeader
    }
  )
  return response.data
}

async closeChatSession(name, sessionId) {
  const authStore = useAuthStore()
  const response = await axios.post(
    `/api/agents/${name}/chat/sessions/${sessionId}/close`,
    {},
    { headers: authStore.authHeader }
  )
  return response.data
}
```

**Planned UI Components**:
1. Sessions List Panel in agent detail view
2. Session detail modal with message history
3. Close session button with confirmation
4. Filter by status (active/closed)

### Testing: Session Management

**Prerequisites**:
- Agent exists and has been used for chat
- User authenticated with valid token
- Database has existing chat sessions

#### Test 1: List All Sessions

**Action**:
```bash
# List all sessions for an agent
curl http://localhost:8000/api/agents/my-agent/chat/sessions \
  -H "Authorization: Bearer $TOKEN"
```

**Expected**:
- Returns array of session objects
- Non-admins see only their own sessions
- Sessions ordered by last_message_at DESC

**Verify**:
- [ ] Response has `session_count` and `sessions` array
- [ ] Each session has id, message_count, total_cost, status
- [ ] Timestamps are valid ISO format

#### Test 2: Filter Sessions by Status

**Action**:
```bash
# List only active sessions
curl "http://localhost:8000/api/agents/my-agent/chat/sessions?status=active" \
  -H "Authorization: Bearer $TOKEN"

# List only closed sessions
curl "http://localhost:8000/api/agents/my-agent/chat/sessions?status=closed" \
  -H "Authorization: Bearer $TOKEN"
```

**Expected**:
- Only sessions matching the status filter returned
- Empty array if no matching sessions

**Verify**:
- [ ] All returned sessions have correct status
- [ ] Both filters work independently

#### Test 3: View Session Details

**Action**:
```bash
# Get session ID from list endpoint first
SESSION_ID=$(curl -s http://localhost:8000/api/agents/my-agent/chat/sessions \
  -H "Authorization: Bearer $TOKEN" | jq -r '.sessions[0].id')

# Get session details with messages
curl "http://localhost:8000/api/agents/my-agent/chat/sessions/$SESSION_ID" \
  -H "Authorization: Bearer $TOKEN"
```

**Expected**:
- Returns session metadata and messages array
- Messages include full observability data
- Messages ordered by timestamp DESC

**Verify**:
- [ ] Session object matches list response
- [ ] Messages have role, content, timestamp
- [ ] Assistant messages have cost, context_used, tool_calls
- [ ] User messages have cost=null

#### Test 4: Close Session

**Action**:
```bash
# Close a session
curl -X POST "http://localhost:8000/api/agents/my-agent/chat/sessions/$SESSION_ID/close" \
  -H "Authorization: Bearer $TOKEN"
```

**Expected**:
- Returns `{"status": "closed", "session_id": "..."}`
- Session status in database changes to "closed"
- Next chat message creates a new session

**Verify**:
```sql
-- Check session is closed
SELECT id, status FROM chat_sessions WHERE id = '$SESSION_ID';
-- Should show: status='closed'
```

#### Test 5: Access Control

**Action**:
```bash
# User 1 creates a session
curl -X POST http://localhost:8000/api/agents/my-agent/chat \
  -H "Authorization: Bearer $USER1_TOKEN" \
  -d '{"message": "Hello"}'

# User 2 tries to access User 1's session
USER1_SESSION=$(sqlite3 ~/trinity-data/trinity.db \
  "SELECT id FROM chat_sessions WHERE user_id=1 ORDER BY started_at DESC LIMIT 1")

curl "http://localhost:8000/api/agents/my-agent/chat/sessions/$USER1_SESSION" \
  -H "Authorization: Bearer $USER2_TOKEN"
```

**Expected**:
- User 2 gets 403 Forbidden
- Admin can access any session

**Verify**:
- [ ] 403 error for non-owner access
- [ ] Admin (role='admin') can view any session

#### Test 6: Session After Close

**Action**:
```bash
# Close current session
curl -X POST "http://localhost:8000/api/agents/my-agent/chat/sessions/$SESSION_ID/close" \
  -H "Authorization: Bearer $TOKEN"

# Send new message
curl -X POST http://localhost:8000/api/agents/my-agent/chat \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"message": "Hello again"}'

# List sessions
curl http://localhost:8000/api/agents/my-agent/chat/sessions \
  -H "Authorization: Bearer $TOKEN"
```

**Expected**:
- New session created automatically
- Old session remains closed with its messages
- session_count increases by 1

**Verify**:
```sql
SELECT id, status, message_count FROM chat_sessions
WHERE agent_name = 'my-agent' ORDER BY started_at DESC;
-- Should show: 2 sessions (1 active, 1 closed)
```

#### Edge Cases

- [ ] Close already-closed session -> succeeds (idempotent)
- [ ] View session for non-existent agent -> 404
- [ ] View non-existent session -> 404
- [ ] Close session belonging to different agent -> 403
- [ ] List sessions with both status and user filter -> combines filters correctly

---

## Migration Notes

### Database Migration
Handled automatically by `init_database()` in `database.py` (tables created in init block):
1. Creates `chat_sessions` table if not exists
2. Creates `chat_messages` table if not exists
3. Creates indexes

### Backward Compatibility
- Existing in-memory chat still works (container-level)
- Persistent layer is additive, not breaking
- Frontend doesn't need changes (persistence is transparent)

### Future UI Integration
To show persistent history in UI:
1. Add store action: `getPersistentChatHistory()`
2. Add UI tab: "History" next to "Chat"
3. Display sessions list with costs/timestamps
4. Click session -> view all messages
5. Filter by date range, user (admin only)

---

## Changelog

- **2026-02-20**: Added cross-references to authenticated-chat-tab.md and parallel-headless-execution.md. The `/task` endpoint now supports `save_to_session=true` parameter to persist to these same tables. New `db.create_new_chat_session()` method added (`db/chat.py:233-263`).
- **2026-01-13**: Added Session Management section documenting EXEC-019, EXEC-020, EXEC-021 user stories. Documented list/view/close session APIs, database operations, access control, and testing procedures. Frontend UI not yet implemented (backend-only).
- **2025-12-30**: Updated line numbers to reflect current codebase after execution queue integration. Chat endpoint now at lines 106-356 (was 50-294). Persistent history endpoints now at lines 876-1027 (was 586-736). Database schema lines updated.
- **2025-12-01**: Initial implementation of persistent chat session tracking
