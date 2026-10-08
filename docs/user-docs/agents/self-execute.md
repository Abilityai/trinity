# Self-Execute (Background Tasks)

Allow an agent to run a background task on itself while keeping the chat responsive. The agent tells the user "I'm working on that in the background" and the result appears when ready.

## How It Works

1. Agent calls `chat_with_agent` targeting itself with `parallel=true`.
2. Trinity creates a background execution tracked as a `SELF_TASK` activity.
3. The chat session stays responsive.
4. When complete, the result optionally injects into the chat.

```
Agent (in chat) → "Let me research that in the background"
       │
       ▼
MCP chat_with_agent(self, inject_result=true)
       │
       ▼
Background execution starts → Activity panel shows progress
       │
       ▼
Execution completes → Result appears in chat (collapsed)
```

## For Agents

### MCP Tool Usage

```typescript
mcp__trinity__chat_with_agent({
  agent_name: "my-agent",      // Same as calling agent
  message: "Research the top 5 competitors",
  parallel: true,              // Required for background execution
  async: true,                 // Returns immediately
  inject_result: true,         // Insert result into chat when done
  chat_session_id: "session-abc-123"  // Current session ID
})
```

The call returns immediately with an `execution_id`. The result appears in chat when the task completes.

That `execution_id` is a receipt: the task arrived and is queued, running or done. Never send the same task again because a call timed out or delivery could not be confirmed — a reworded repeat can run the work twice. To check on it, call `get_execution_result(agent_name, execution_id)`; `running` does not mean stuck. Every agent is taught this rule in its platform instructions and in the `chat_with_agent` tool description. See [Agent Network](../collaboration/agent-network.md).

### REST API

```bash
POST /api/agents/my-agent/task
```

**Request:**
```json
{
  "message": "Research competitor pricing",
  "async_mode": true,
  "inject_result": true,
  "chat_session_id": "session-abc-123"
}
```

## Activity Tracking

Self-tasks appear in the activity panel with:
- Activity type: `self_task`
- Triggered by: `self_task`
- Agent name: The agent running the background work

### WebSocket Events

**Task started:**
```json
{
  "type": "agent_activity",
  "activity_type": "self_task",
  "activity_state": "started",
  "action": "Background task: Research competitor...",
  "details": {
    "execution_id": "exec-123",
    "inject_result": true
  }
}
```

**Task completed:**
```json
{
  "type": "agent_activity",
  "activity_type": "self_task",
  "activity_state": "completed",
  "details": {
    "cost_usd": 0.05,
    "execution_time_ms": 45000,
    "result_injected": true
  }
}
```

## Chat Display

Self-task results appear in chat with distinct styling:
- Purple background
- "Background Task Result" header
- Collapsed by default (click to expand)
- Shows preview when collapsed

## Security

- The `X-Source-Agent` header must match the MCP key's agent scope.
- Result injection validates session ownership before inserting.

## Use Cases

- **Research tasks**: "Research this topic while I continue chatting"
- **File processing**: "Process these files in the background"
- **Code generation**: "Generate that component, I'll check it when done"
- **Batch operations**: "Run those 10 queries, show me results when ready"

## Limitations

- No cancellation UI yet — running self-tasks cannot be stopped from chat.
- No real-time progress streaming — only start/complete events.
- Session must be owned by the same user for result injection.

## See Also

- [Agent Chat](agent-chat.md) — Chat interface basics
- [Executions](../operations/executions.md) — Viewing execution history
- [Fan-Out](../automation/fan-out.md) — Parallel task dispatch
