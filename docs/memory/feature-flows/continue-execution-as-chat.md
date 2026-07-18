# Feature: Continue Execution as Chat (EXEC-023)

## Overview

Allows users to resume failed or completed executions as interactive chat conversations with full Claude Code context preservation using the `--resume {session_id}` flag.

**Spec**: `docs/requirements/CONTINUE_EXECUTION_AS_CHAT.md`

## User Story

As an agent operator, I want to click "Continue as Chat" on a failed or completed execution so I can:
- Ask the agent what went wrong
- Fix issues interactively with full context
- Continue partially completed work without re-explaining context

## Key Insight

Claude Code stores complete session history on disk at:
```
~/.claude/projects/{project-path}/{session_id}.jsonl
```

By storing the `session_id` from each execution and using `claude --resume {session_id}`, we can continue the exact session with full context (150K+ tokens) without copying or re-injection.

## Entry Points

| Entry Point | File | Line | Description |
|-------------|------|------|-------------|
| Continue as Chat button | `src/frontend/src/views/ExecutionDetail.vue` | 60-70 | Indigo button visible when `claude_session_id` exists and status is not "running" |

## Architecture

```
Phase 1: Store session_id on execution completion
┌───────────────────┐     ┌───────────────────┐     ┌──────────────────────┐
│ Task execution    │────▶│ Claude Code runs  │────▶│ Returns session_id   │
│ via /task         │     │ in agent container│     │ in response.session_id│
└───────────────────┘     └───────────────────┘     └──────────────────────┘
         │                                                     │
         ▼                                                     ▼
┌───────────────────┐                               ┌──────────────────────┐
│ Backend extracts  │◀──────────────────────────────│ Agent stores session │
│ session_id, saves │                               │ at ~/.claude/projects│
│ to schedule_      │                               └──────────────────────┘
│ executions table  │
└───────────────────┘

Phase 2: Resume session from Chat tab
┌───────────────────┐     ┌───────────────────┐     ┌──────────────────────┐
│ User clicks       │────▶│ Navigate to Chat  │────▶│ ChatPanel receives   │
│ "Continue as Chat"│     │ tab with query    │     │ resumeSessionId prop │
└───────────────────┘     │ params            │     └──────────────────────┘
                          └───────────────────┘                │
                                                               ▼
┌───────────────────┐     ┌───────────────────┐     ┌──────────────────────┐
│ claude --resume   │◀────│ Agent /api/task   │◀────│ Backend passes       │
│ {session_id} -p   │     │ uses --resume     │     │ resume_session_id    │
│ "user message"    │     │ flag              │     │ to agent             │
└───────────────────┘     └───────────────────┘     └──────────────────────┘
```

## Frontend Layer

### ExecutionDetail.vue (Button)

**File**: `src/frontend/src/views/ExecutionDetail.vue`

**Continue as Chat Button** (lines 59-70):
```vue
<!-- Continue as Chat button (EXEC-023) -->
<button
  v-if="execution?.claude_session_id && execution?.status !== 'running'"
  @click="continueAsChat"
  class="px-3 py-1.5 bg-indigo-600 hover:bg-indigo-700 text-white text-sm font-medium rounded-lg transition-colors flex items-center space-x-1"
  title="Continue this execution as an interactive chat"
>
  <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
  </svg>
  <span>Continue as Chat</span>
</button>
```

**Navigation Handler** (lines 674-686):
```javascript
// Continue execution as chat (EXEC-023)
function continueAsChat() {
  if (!execution.value?.claude_session_id) return

  router.push({
    name: 'AgentDetail',
    params: { name: agentName.value },
    query: {
      tab: 'chat',
      resumeSessionId: execution.value.claude_session_id,
      executionId: executionId.value
    }
  })
}
```

### AgentDetail.vue (Route Handler)

**File**: `src/frontend/src/views/AgentDetail.vue`

**Resume Mode State** (lines 291-293):
```javascript
// Resume mode state (EXEC-023)
const resumeSessionId = computed(() => route.query.resumeSessionId || null)
const resumeExecutionId = computed(() => route.query.executionId || null)
```

**Props to ChatPanel** (lines 97-100):
```vue
<ChatPanel
  :agent-name="agent.name"
  :agent-status="agent.status"
  :resume-session-id="resumeSessionId"
  :resume-execution-id="resumeExecutionId"
/>
```

### ChatPanel.vue (Resume Handling)

**File**: `src/frontend/src/components/ChatPanel.vue`

**Props** (lines 172-179):
```javascript
// Resume mode props (EXEC-023)
resumeSessionId: {
  type: String,
  default: null
},
resumeExecutionId: {
  type: String,
  default: null
}
```

**State** (lines 199-204):
```javascript
// Resume mode state (EXEC-023)
const resumeSessionIdLocal = ref(null)
const resumeExecutionIdLocal = ref(null)
const resumeBannerDismissed = ref(false)  // Separate flag for banner visibility
// Show banner only if in resume mode AND banner not dismissed
const isResumeMode = computed(() => !!resumeSessionIdLocal.value && !resumeBannerDismissed.value)
```

**Key Insight**: The `resumeSessionIdLocal` is kept for the entire session to pass `resume_session_id` with every message. The `resumeBannerDismissed` flag only controls banner visibility. This separation allows:
- Banner dismissal without losing resume context
- All messages to use `--resume` flag for full context continuity

**Resume Banner UI** (lines 88-114):
```vue
<!-- Resume mode banner (EXEC-023) -->
<div
  v-if="isResumeMode"
  class="mx-4 mt-2 px-4 py-3 bg-indigo-50 dark:bg-indigo-900/30 border border-indigo-200 dark:border-indigo-800 rounded-lg flex items-center justify-between"
>
  <div class="flex items-center space-x-2">
    <svg class="w-5 h-5 text-indigo-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
    </svg>
    <span class="text-sm text-indigo-700 dark:text-indigo-300">
      Continuing from execution
      <span class="font-mono text-xs bg-indigo-100 dark:bg-indigo-800 px-1.5 py-0.5 rounded">
        {{ resumeExecutionIdLocal?.substring(0, 8) }}...
      </span>
      - The agent has full context from that execution.
    </span>
  </div>
  <button @click="dismissResumeMode" ...>
    <!-- X icon -->
  </button>
</div>
```

**Watch for Resume Props** (lines 425-436):
```javascript
// Watch for resume mode props (EXEC-023)
watch(() => props.resumeSessionId, (newSessionId) => {
  if (newSessionId) {
    // Start fresh for resume - don't load existing session
    messages.value = []
    currentSessionId.value = null
    error.value = null
    resumeSessionIdLocal.value = newSessionId
    resumeExecutionIdLocal.value = props.resumeExecutionId
    resumeBannerDismissed.value = false  // Show banner when entering resume mode
  }
}, { immediate: true })
```

**Start New Chat** (lines 302-312):
```javascript
const startNewChat = () => {
  currentSessionId.value = null
  messages.value = []
  error.value = null
  showSessionDropdown.value = false
  // Clear resume mode when starting new chat
  resumeSessionIdLocal.value = null
  resumeExecutionIdLocal.value = null
  resumeBannerDismissed.value = false
}
```

**Send Message with Resume** (lines 370-377):
```javascript
// EXEC-023: Include resume_session_id for ALL messages in resume mode
// The /task endpoint is stateless - it doesn't use --continue.
// We must keep passing resume_session_id so Claude Code uses --resume for every message.
if (resumeSessionIdLocal.value) {
  payload.resume_session_id = resumeSessionIdLocal.value
  // Note: We intentionally do NOT clear resumeSessionIdLocal here.
  // All messages in a resumed session need --resume to maintain context.
}
```

**Dismiss Handler** (lines 314-319):
```javascript
// Dismiss resume mode banner (EXEC-023)
// Note: This only hides the banner, but keeps resumeSessionIdLocal so
// subsequent messages continue using --resume for context continuity
const dismissResumeMode = () => {
  resumeBannerDismissed.value = true
}
```

**Select Session Handler** (lines 274-278):
```javascript
// Clear resume mode when selecting a different session
// (user is explicitly choosing a different conversation context)
resumeSessionIdLocal.value = null
resumeExecutionIdLocal.value = null
resumeBannerDismissed.value = false
```

## Backend Layer

### Database Migration

**File**: `src/backend/database.py` (lines 361-374)

```python
def _migrate_execution_session_tracking(cursor, conn):
    """Add claude_session_id column to schedule_executions table (EXEC-023).

    Enables "Continue Execution as Chat" feature by storing Claude Code's
    session_id, which can be used with --resume to continue the session.
    """
    cursor.execute("PRAGMA table_info(schedule_executions)")
    columns = {row[1] for row in cursor.fetchall()}

    if "claude_session_id" not in columns:
        print("Adding claude_session_id column to schedule_executions for session resume...")
        cursor.execute("ALTER TABLE schedule_executions ADD COLUMN claude_session_id TEXT")

    conn.commit()
```

### Model Updates

**File**: `src/backend/db_models.py` (lines 162-163)
```python
# Session resume support (EXEC-023)
claude_session_id: Optional[str] = None    # Claude Code session ID for --resume
```

**File**: `src/backend/models.py` (line 95)
```python
resume_session_id: Optional[str] = None  # Claude Code session ID to resume (EXEC-023)
```

### Schedule Operations

**File**: `src/backend/db/schedules.py`

**Row to Model** (lines 121-123):
```python
# Session resume support (EXEC-023)
claude_session_id=row["claude_session_id"] if "claude_session_id" in row_keys else None,
```

**Update Execution Status** (lines 565-609):
```python
def update_execution_status(
    execution_id: str,
    status: str,
    response: str = None,
    error: str = None,
    context_used: int = None,
    context_max: int = None,
    cost: float = None,
    tool_calls: str = None,
    execution_log: str = None,
    claude_session_id: str = None  # NEW: EXEC-023
) -> bool:
    """Update execution status when completed.

    Args:
        claude_session_id: Claude Code session ID for --resume support (EXEC-023)
    """
    # SQL includes claude_session_id in UPDATE statement
```

### Chat Router

**File**: `src/backend/routers/chat.py`

**Pass resume_session_id to Agent** (lines 675-684):
```python
payload = {
    "message": request.message,
    "model": request.model,
    "allowed_tools": request.allowed_tools,
    "system_prompt": request.system_prompt,
    "timeout_seconds": request.timeout_seconds,
    "execution_id": execution_id,
    "resume_session_id": request.resume_session_id  # Claude Code session resume (EXEC-023)
}
```

**Extract and Store session_id** (lines 730-743):
```python
# Extract Claude Code session_id for --resume support (EXEC-023)
claude_session_id = response_data.get("session_id") or metadata.get("session_id")

db.update_execution_status(
    execution_id=execution_id,
    status="success",
    response=sanitized_resp,
    context_used=context_used if context_used > 0 else None,
    context_max=metadata.get("context_window") or 200000,
    cost=metadata.get("cost_usd"),
    tool_calls=tool_calls_json,
    execution_log=execution_log_json,
    claude_session_id=claude_session_id  # NEW
)
```

## Agent Server Layer

### Models

**File**: `docker/base-image/agent_server/models.py` (line 224)
```python
class ParallelTaskRequest(BaseModel):
    """Request for parallel task execution (stateless, no conversation context)"""
    message: str
    model: Optional[str] = None
    allowed_tools: Optional[List[str]] = None
    system_prompt: Optional[str] = None
    timeout_seconds: Optional[int] = 900
    max_turns: Optional[int] = None
    execution_id: Optional[str] = None
    resume_session_id: Optional[str] = None  # Claude Code session ID for --resume (EXEC-023)
```

### Chat Router

**File**: `docker/base-image/agent_server/routers/chat.py` (lines 96-142)

```python
@router.post("/api/task")
async def execute_task(request: ParallelTaskRequest):
    """
    Execute a stateless task in parallel mode (no conversation context).

    Use this for:
    - Agent delegation from orchestrators
    - Batch processing without context pollution
    - Parallel task execution
    - Resuming previous sessions with resume_session_id (EXEC-023)
    """
    if request.resume_session_id:
        logger.info(f"[Task] Resuming session {request.resume_session_id}: {request.message[:50]}...")

    runtime = get_runtime()
    response_text, raw_messages, metadata, session_id = await runtime.execute_headless(
        prompt=request.message,
        model=request.model,
        allowed_tools=request.allowed_tools,
        system_prompt=request.system_prompt,
        timeout_seconds=request.timeout_seconds or 900,
        max_turns=request.max_turns,
        execution_id=request.execution_id,
        resume_session_id=request.resume_session_id  # Resume previous session (EXEC-023)
    )
```

### Runtime Adapter

**File**: `docker/base-image/agent_server/services/runtime_adapter.py` (lines 100-131)

```python
@abstractmethod
async def execute_headless(
    self,
    prompt: str,
    model: Optional[str] = None,
    allowed_tools: Optional[List[str]] = None,
    system_prompt: Optional[str] = None,
    timeout_seconds: int = 900,
    max_turns: Optional[int] = None,
    execution_id: Optional[str] = None,
    resume_session_id: Optional[str] = None  # EXEC-023
) -> Tuple[str, List[ExecutionLogEntry], ExecutionMetadata, str]:
    """
    Execute a stateless task in headless mode.

    Args:
        resume_session_id: Optional Claude Code session ID to resume (EXEC-023)
    """
    pass
```

### Claude Code Service

**File**: `docker/base-image/agent_server/services/claude_code.py` (lines 571-622)

```python
async def execute_headless_task(
    prompt: str,
    model: Optional[str] = None,
    allowed_tools: Optional[List[str]] = None,
    system_prompt: Optional[str] = None,
    timeout_seconds: int = 900,
    max_turns: Optional[int] = None,
    execution_id: Optional[str] = None,
    resume_session_id: Optional[str] = None
) -> tuple[str, List[ExecutionLogEntry], ExecutionMetadata, str]:
    """
    Execute Claude Code in headless mode for parallel task execution.

    Can resume previous sessions via resume_session_id (EXEC-023)

    Args:
        resume_session_id: Optional Claude Code session ID to resume (EXEC-023)
    """

    # Build command - NO --continue flag (stateless) unless resuming
    cmd = ["claude", "--print", "--output-format", "stream-json", "--verbose", "--dangerously-skip-permissions"]

    # Add --resume if resuming a previous session (EXEC-023)
    if resume_session_id:
        cmd.extend(["--resume", resume_session_id])
        logger.info(f"[Headless Task] Resuming session: {resume_session_id}")

    # ... rest of command building and execution
```

### Gemini Runtime (Not Supported)

**File**: `docker/base-image/agent_server/services/gemini_runtime.py` (lines 498-511)

```python
async def execute_headless(
    self,
    prompt: str,
    # ... other params
    resume_session_id: Optional[str] = None
) -> Tuple[str, List[ExecutionLogEntry], ExecutionMetadata, str]:
    """
    Execute Gemini CLI in headless mode for parallel tasks.

    Note: resume_session_id is not supported by Gemini CLI (ignored).
    """
    # Note: resume_session_id is ignored - Gemini CLI doesn't support session resume
```

## Data Flow

```
1. User clicks "Continue as Chat" on ExecutionDetail page
   │
   ▼
2. Router navigates to AgentDetail with query params:
   /agents/{name}?tab=chat&resumeSessionId={sessionId}&executionId={execId}
   │
   ▼
3. AgentDetail.vue extracts query params, passes to ChatPanel as props
   │
   ▼
4. ChatPanel watches props, enters resume mode:
   - Clears messages
   - Shows resume banner
   - Stores resumeSessionIdLocal (persists for entire session)
   - Sets resumeBannerDismissed = false
   │
   ▼
5. User types first message and sends
   │
   ▼
6. ChatPanel.sendMessage() includes resume_session_id in payload:
   POST /api/agents/{name}/task { message, resume_session_id, ... }
   (Note: resume_session_id is NOT cleared after sending)
   │
   ▼
7. Backend passes to agent:
   POST http://agent:8000/api/task { message, resume_session_id }
   │
   ▼
8. Agent server builds command with --resume:
   claude --resume {session_id} --print --output-format stream-json ... -p "{message}"
   │
   ▼
9. Claude Code loads ~/.claude/projects/{path}/{session_id}.jsonl
   - Full context from original execution available
   - Agent can reference tool calls, errors, decisions from original session
   │
   ▼
10. Response returned to ChatPanel
    - Message displayed, user can continue chatting
    │
    ▼
11. User sends subsequent messages
    - SAME resume_session_id passed with EVERY message
    - Each message uses --resume flag for full context
    - Banner can be dismissed but session ID persists
    │
    ▼
12. Resume mode ends only when:
    - User clicks "New Chat" (clears resumeSessionIdLocal)
    - User selects different session from dropdown (clears resumeSessionIdLocal)
    - User navigates away from Chat tab
```

### Why Pass resume_session_id for Every Message?

The `/task` endpoint is **stateless** - it does NOT use `--continue` flag. Each call to `/task` is a fresh Claude Code invocation. Without `--resume`, Claude Code would start with no context.

**Previous Bug (Fixed 2026-02-21)**: Resume mode was cleared after the first message, causing subsequent messages to lose context. Users had to click "Continue as Chat" again for each message.

**Fix**: Keep `resumeSessionIdLocal` for the entire resumed session. All messages pass `resume_session_id` so every Claude Code invocation uses `--resume {session_id}` and has full context from the original execution.

## Session Storage

Claude Code stores sessions at:
```
~/.claude/projects/{project-path}/{session_id}.jsonl
```

Where `{project-path}` is the sanitized absolute path of the working directory (e.g., `/home/developer` becomes `home-developer`).

Session files persist across agent restarts since `~/.claude/` is inside `/home/developer/` which is a Docker bind mount.

## Error Handling

| Error Case | HTTP Status | Message | Recovery |
|------------|-------------|---------|----------|
| Session file deleted | 500 | Claude Code fails to resume | Start fresh session |
| Agent not running | N/A | "Agent Not Running" UI state | Start agent first |
| Invalid session_id | 500 | "Session not found" | Start fresh session |

### Session Expiration

Claude Code may clean up old session files. If resume fails:
1. Claude Code returns error
2. Agent server returns 500
3. Frontend shows error message
4. User can start fresh session

## Security Considerations

1. **Session Isolation**: Each agent has its own session files; no cross-agent access possible
2. **Resume ownership (IDOR guard, #1672)**: agent *access* is NOT sufficient to resume a
   session. Execution rows are agent-scoped (`accessible_agent_names`) and expose
   `claude_session_id`, so on a **shared** agent one operator could read a peer's session id
   and resume their private conversation. The `POST /api/agents/{name}/task` resume path now
   authorizes ownership via `db.resume_session_belongs_to_user(agent, session_id, user_id)` —
   the session id must belong to an execution **this user** triggered on **this agent** (admin
   bypasses, mirroring the Session tab). A foreign/unknown id returns **404** (enumeration-safe,
   never confirming the id exists); the `dispatched`/`dispatched_async` #1083 sentinels return
   **400**. The gate keys on the resume id's presence, **not** on the spoofable `X-Source-Agent`
   header. See `routers/chat.py` (gate) + `db/schedules.py::resume_session_belongs_to_user`.
3. **No Credential Exposure**: Session files may contain tool outputs but credentials are already sanitized by Trinity's credential sanitizer

## Related Flows

| Flow | Relationship |
|------|--------------|
| [authenticated-chat-tab.md](authenticated-chat-tab.md) | ChatPanel component reused for resume mode |
| [execution-detail-page.md](execution-detail-page.md) | Entry point for "Continue as Chat" button |
| [parallel-headless-execution.md](parallel-headless-execution.md) | `/task` endpoint used for resume messages |

## Revision History

| Date | Change |
|------|--------|
| 2026-07-17 | **Bug Fix (#1672) — reliability + security.** "Continue as Chat" was broken/unreliable across cases. Root cause: **`AgentDetail` is KeepAlive-cached**, so `routeForcedMode='legacy'` (set only in `onMounted`) never ran on the common nav path (open agent → execution → Continue as Chat re-activates the cached instance via `onActivated`); `SessionPanel` rendered instead of the legacy `ChatPanel` and the resume id was silently dropped — worked only after a hard reload. Fixes: (1) extracted `applyDeepLinkRouting()` called from **both** `onMounted` and `onActivated`, and it clears `routeForcedMode` on non-resume landings so legacy mode can't stick for the SPA session; (2) **IDOR** — the `/task` resume path was unvalidated/unauthorized (any operator on a shared agent could resume a peer's private session), now gated by `db.resume_session_belongs_to_user` + sentinel-reject, keyed on the resume id's presence **not** the spoofable `X-Source-Agent` header (see Security Considerations); (3) the button + `continueAsChat()` now hide/reject the `dispatched`/`dispatched_async` sentinels (`canContinueAsChat`); (4) `ChatPanel` auto-select guard changed `!isResumeMode` → `!resumeSessionIdLocal` so **dismissing the banner** no longer lets auto-select null the resume id and drop `--resume`. Codex resume preserved (no id-shape check — ownership is the guard; a Codex `thread_id` is not a UUID). Tests: `tests/unit/test_1672_continue_as_chat_resume_auth.py` (real accessor SQL + endpoint gate incl. the header-spoof case), `src/frontend/e2e/continue-as-chat.spec.js` (KeepAlive path, CI/`ui`-label). Follow-ups: no index on `claude_session_id` (rare-action full scan, LOW); paid `/api/paid/{name}/chat` resume has only the sentinel guard — full payer→session authorization needs a payer-identity binding that doesn't exist yet. |
| 2026-02-21 | **Bug Fix (EXEC-023)**: Fixed resume mode context lost after first message. The `/task` endpoint is stateless (doesn't use `--continue`), but `resumeSessionIdLocal` was cleared after the first message, causing subsequent messages to start fresh without context. **Fix**: (1) Added `resumeBannerDismissed` flag to separate banner visibility from session ID state, (2) Removed clearing of `resumeSessionIdLocal` after first message - now ALL messages pass `resume_session_id`, (3) `dismissResumeMode()` only sets banner flag (doesn't clear session ID), (4) `selectSession()` clears resume mode when switching sessions, (5) Reset `resumeBannerDismissed` in `startNewChat()` and resume mode watcher. See `ChatPanel.vue` lines 202-204, 314-319, 370-377, 274-278, 302-312, 425-436. |
| 2026-02-21 | **Bug Fix (EXEC-023)**: Fixed `ChatPanel.vue` auto-selecting old session in resume mode. The `loadSessions()` function auto-selected the most recent active session even when in resume mode because `messages.length === 0` was true after the watch handler cleared messages. Fix: Added `!isResumeMode.value` condition at line 253. |
| 2026-02-21 | **Bug Fix (EXEC-023)**: Fixed scheduled executions missing `claude_session_id`. The dedicated scheduler service had its own code path that didn't capture session_id from agent responses. Updated 4 files in `src/scheduler/`: models.py (added `session_id` field), agent_client.py (extract session_id), database.py (accept claude_session_id param), service.py (pass session_id to DB). |
| 2026-02-21 | **Bug Fix (EXEC-023)**: Fixed `DatabaseManager.update_execution_status()` wrapper in `src/backend/database.py:1295-1299` which was missing the `claude_session_id` parameter. The underlying `db/schedules.py:update_execution_status()` accepted the parameter, but the wrapper method did not forward it. This caused all manual task executions (via `/task` endpoint) to fail updating their database status with the session ID, breaking the "Continue as Chat" feature for those executions. |
| 2026-02-20 | Initial implementation (EXEC-023) |
