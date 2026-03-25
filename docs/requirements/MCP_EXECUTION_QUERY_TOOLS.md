# MCP Execution Query Tools

> **Requirement ID**: MCP-007
> **Priority**: HIGH
> **Status**: ✅ Implemented (2026-03-25)
> **Created**: 2026-02-05
> **Source**: `docs/planning/WORKFLOW_PRIORITIES_2026-02.md`

---

## Overview

Add MCP tools that allow agents to query execution history and status across the fleet. This enables orchestrator agents to monitor worker agents, track task completion, and build awareness of "what happened" programmatically.

## Business Context

**Problem**: Orchestrator agents and monitoring agents cannot programmatically answer:
- "What tasks ran in the last hour?"
- "Did the scheduled report complete successfully?"
- "What was the output of that execution?"
- "Which agents are currently busy?"

**Current State**: MCP tools exist for triggering tasks (`chat_with_agent`) but not for querying results. Agents must rely on shared files or manual inspection.

**Solution**: Add execution query tools to the MCP server:
- `list_recent_executions` - Query what happened across fleet
- `get_execution_result` - Fetch specific execution output
- `get_agent_activity_summary` - High-level status for monitoring

**Strategic Value**: Enables autonomous monitoring, orchestration awareness, and self-healing workflows.

---

## Current State Analysis

### Existing MCP Tools (36 total)

**Schedule-related tools** (`src/mcp-server/src/tools/schedules.ts`):
- `get_schedule_executions` - Gets execution history for a specific schedule
- Limited to schedule-triggered executions, not manual/MCP-triggered

**Chat tools** (`src/mcp-server/src/tools/chat.ts`):
- `chat_with_agent` - Trigger task (supports async mode with execution_id return)
- No way to query the result after async execution

### Execution Data Available

**Database fields** (`schedule_executions` table):
```sql
id, schedule_id, agent_name, status, started_at, completed_at,
duration_ms, message, response, error, triggered_by,
context_used, context_max, cost, tool_calls, execution_log
```

**Backend endpoints available**:
- `GET /api/agents/{name}/executions` - List agent executions
- `GET /api/agents/{name}/executions/{id}` - Get execution detail
- `GET /api/agents/{name}/executions/{id}/log` - Get execution transcript
- `GET /api/executions` - Unified cross-agent list (new, from EXEC-022)

### MCP Tool Pattern

Tools follow consistent pattern (`src/mcp-server/src/tools/*.ts`):
```typescript
export function createXxxTools(client: TrinityClient, requireApiKey: boolean) {
  const getClient = (authContext?: McpAuthContext) => { ... };

  return {
    toolName: {
      name: "tool_name",
      description: "...",
      parameters: z.object({ ... }),
      execute: async (args, context) => {
        const apiClient = getClient(context?.session);
        // Call backend API
        return JSON.stringify(result, null, 2);
      }
    }
  };
}
```

---

## Requirements

### R1: list_recent_executions Tool

**R1.1**: Tool definition
```typescript
listRecentExecutions: {
  name: "list_recent_executions",
  description: "List recent task executions across agents. Use to monitor what happened, check task completion status, or find specific executions.",
  parameters: z.object({
    agent_name: z.string().optional()
      .describe("Filter by specific agent name. Omit for all accessible agents."),
    status: z.enum(["pending", "running", "success", "failed"]).optional()
      .describe("Filter by execution status"),
    triggered_by: z.enum(["manual", "schedule", "agent", "mcp"]).optional()
      .describe("Filter by trigger source"),
    hours: z.number().optional().default(24)
      .describe("Look back period in hours (default 24, max 168)"),
    limit: z.number().optional().default(20)
      .describe("Maximum results to return (default 20, max 100)")
  }),
  execute: async (args, context) => {
    // Returns list of execution summaries
  }
}
```

**R1.2**: Response format
```json
{
  "executions": [
    {
      "id": "abc123def456",
      "agent_name": "ruby-agent",
      "status": "success",
      "triggered_by": "schedule",
      "message": "Process daily invoices",
      "started_at": "2026-02-05T10:00:00Z",
      "completed_at": "2026-02-05T10:00:45Z",
      "duration_seconds": 45,
      "cost_usd": 0.02,
      "has_error": false
    }
  ],
  "total_count": 42,
  "filters_applied": {
    "hours": 24,
    "status": null,
    "agent_name": null
  }
}
```

**R1.3**: Access control
- User-scoped keys: Returns executions for owned + shared agents
- Agent-scoped keys: Returns executions for self + permitted agents
- System-scoped keys: Returns all executions

### R2: get_execution_result Tool

**R2.1**: Tool definition
```typescript
getExecutionResult: {
  name: "get_execution_result",
  description: "Get the full result of a specific execution including output, cost, and optionally the execution transcript. Use after triggering async tasks or to inspect historical execution details.",
  parameters: z.object({
    agent_name: z.string()
      .describe("Name of the agent that ran the execution"),
    execution_id: z.string()
      .describe("Execution ID (returned from async chat_with_agent or list_recent_executions)"),
    include_transcript: z.boolean().optional().default(false)
      .describe("Include full execution transcript (can be large)")
  }),
  execute: async (args, context) => {
    // Returns full execution details
  }
}
```

**R2.2**: Response format
```json
{
  "execution": {
    "id": "abc123def456",
    "agent_name": "ruby-agent",
    "status": "success",
    "message": "Process daily invoices",
    "response": "Successfully processed 15 invoices totaling $4,250.00...",
    "error": null,
    "started_at": "2026-02-05T10:00:00Z",
    "completed_at": "2026-02-05T10:00:45Z",
    "duration_seconds": 45,
    "cost_usd": 0.02,
    "context_used": 12500,
    "context_max": 200000,
    "tool_calls": ["Read", "Write", "Bash"],
    "transcript": [...]  // Only if include_transcript=true
  }
}
```

**R2.3**: Access control
- Must have access to the agent that ran the execution
- Agent-scoped keys can only access executions they triggered or self

### R3: get_agent_activity_summary Tool

**R3.1**: Tool definition
```typescript
getAgentActivitySummary: {
  name: "get_agent_activity_summary",
  description: "Get a high-level summary of agent activity and health. Use for monitoring dashboards, health checks, or understanding agent workload.",
  parameters: z.object({
    agent_name: z.string().optional()
      .describe("Specific agent to summarize. Omit for fleet-wide summary."),
    hours: z.number().optional().default(24)
      .describe("Look back period in hours (default 24)")
  }),
  execute: async (args, context) => {
    // Returns activity summary
  }
}
```

**R3.2**: Response format (single agent)
```json
{
  "agent_name": "ruby-agent",
  "summary": {
    "total_executions": 42,
    "successful": 38,
    "failed": 4,
    "running": 0,
    "success_rate": 90.5,
    "total_cost_usd": 1.23,
    "avg_duration_seconds": 45,
    "last_execution_at": "2026-02-05T10:00:00Z",
    "last_execution_status": "success",
    "is_busy": false,
    "queue_length": 0
  },
  "recent_failures": [
    {
      "id": "xyz789",
      "message": "Check portfolio",
      "error": "Rate limited by external API",
      "failed_at": "2026-02-05T09:30:00Z"
    }
  ]
}
```

**R3.3**: Response format (fleet-wide)
```json
{
  "fleet_summary": {
    "total_agents": 5,
    "agents_with_activity": 3,
    "total_executions": 156,
    "successful": 142,
    "failed": 14,
    "running": 2,
    "success_rate": 91.0,
    "total_cost_usd": 8.45
  },
  "by_agent": [
    {
      "agent_name": "ruby-agent",
      "executions": 42,
      "success_rate": 90.5,
      "cost_usd": 1.23,
      "status": "idle"
    },
    {
      "agent_name": "researcher",
      "executions": 28,
      "success_rate": 100.0,
      "cost_usd": 2.10,
      "status": "running"
    }
  ],
  "recent_failures": [...]
}
```

### R4: Backend API Support

**R4.1**: New endpoint for fleet summary
```
GET /api/executions/summary
Query Parameters:
  - agent_name: string (optional)
  - hours: number (default 24)
Auth: Scoped to accessible agents
Returns: ActivitySummaryResponse
```

**R4.2**: Extend execution detail endpoint
```
GET /api/agents/{name}/executions/{id}
Query Parameters:
  - include_log: boolean (default false)
Returns: ExecutionDetailResponse (with optional transcript)
```

---

## Implementation Plan

### Phase 1: Backend Endpoints
1. Add `GET /api/executions/summary` endpoint
2. Extend execution detail to optionally include transcript
3. Add appropriate indexes for summary queries

### Phase 2: TrinityClient Methods
1. Add `listRecentExecutions()` method to client.ts
2. Add `getExecutionResult()` method to client.ts
3. Add `getActivitySummary()` method to client.ts

### Phase 3: MCP Tools
1. Create `src/mcp-server/src/tools/executions.ts`
2. Implement three tools with Zod schemas
3. Add access control checks
4. Register tools in server.ts

### Phase 4: Testing
1. Test with user-scoped key
2. Test with agent-scoped key (permission boundaries)
3. Test with system-scoped key
4. Test async workflow: chat_with_agent(async) → get_execution_result

---

## API Changes

### Backend Endpoints

```python
# src/backend/routers/unified_executions.py

@router.get("/api/executions/summary")
async def get_activity_summary(
    agent_name: Optional[str] = None,
    hours: int = Query(default=24, ge=1, le=168),
    current_user: User = Depends(get_current_user),
    db: DatabaseManager = Depends(get_db)
) -> ActivitySummaryResponse:
    """Get activity summary for agent or fleet"""
```

### MCP Client Methods

```typescript
// src/mcp-server/src/client.ts

async listRecentExecutions(params: {
  agent_name?: string;
  status?: string;
  triggered_by?: string;
  hours?: number;
  limit?: number;
}): Promise<ExecutionListResponse> {
  const query = new URLSearchParams();
  if (params.agent_name) query.append("agent_name", params.agent_name);
  // ... build query
  return this.request("GET", `/api/executions?${query.toString()}`);
}

async getExecutionResult(
  agentName: string,
  executionId: string,
  includeTranscript: boolean = false
): Promise<ExecutionDetailResponse> {
  const query = includeTranscript ? "?include_log=true" : "";
  return this.request("GET", `/api/agents/${agentName}/executions/${executionId}${query}`);
}

async getActivitySummary(params: {
  agent_name?: string;
  hours?: number;
}): Promise<ActivitySummaryResponse> {
  const query = new URLSearchParams();
  if (params.agent_name) query.append("agent_name", params.agent_name);
  if (params.hours) query.append("hours", String(params.hours));
  return this.request("GET", `/api/executions/summary?${query.toString()}`);
}
```

### MCP Tools File

```typescript
// src/mcp-server/src/tools/executions.ts

import { z } from "zod";
import { TrinityClient } from "../client.js";
import type { McpAuthContext } from "../types.js";

export function createExecutionTools(
  client: TrinityClient,
  requireApiKey: boolean
) {
  const getClient = (authContext?: McpAuthContext): TrinityClient => {
    if (requireApiKey) {
      if (!authContext?.mcpApiKey) {
        throw new Error("MCP API key authentication required");
      }
      const userClient = new TrinityClient(client.getBaseUrl());
      userClient.setToken(authContext.mcpApiKey);
      return userClient;
    }
    return client;
  };

  return {
    listRecentExecutions: { /* ... */ },
    getExecutionResult: { /* ... */ },
    getAgentActivitySummary: { /* ... */ },
  };
}
```

---

## File Changes

### New Files

| File | Purpose |
|------|---------|
| `src/mcp-server/src/tools/executions.ts` | Execution query tools |

### Modified Files

| File | Change |
|------|--------|
| `src/mcp-server/src/client.ts` | Add execution query methods |
| `src/mcp-server/src/server.ts` | Register execution tools |
| `src/backend/routers/unified_executions.py` | Add summary endpoint |
| `docs/memory/feature-flows/mcp-orchestration.md` | Document new tools |

---

## Use Cases

### UC1: Async Task Completion Check
```
Orchestrator: "Send research task to researcher agent and wait for result"

1. chat_with_agent(agent_name="researcher", message="...", parallel=true, async=true)
   → Returns: { execution_id: "abc123" }

2. (poll or wait)

3. get_execution_result(agent_name="researcher", execution_id="abc123")
   → Returns: { status: "success", response: "Research findings..." }
```

### UC2: Daily Health Check
```
System Agent: "Check fleet health every morning"

1. get_agent_activity_summary(hours=24)
   → Returns fleet summary with success rates, failures, costs

2. If failure_rate > 10%:
   → list_recent_executions(status="failed", hours=24)
   → Analyze patterns, alert human
```

### UC3: Cost Monitoring
```
Finance Agent: "Track daily AI spending"

1. get_agent_activity_summary(hours=24)
   → Returns total_cost_usd by agent

2. If total > threshold:
   → Alert, optionally pause schedules
```

### UC4: Orchestrator Coordination
```
Orchestrator: "Wait for all worker tasks to complete"

1. For each worker:
   chat_with_agent(parallel=true, async=true) → execution_ids[]

2. Poll until all complete:
   for id in execution_ids:
     get_execution_result(id) → check status

3. Collect results, proceed to next phase
```

---

## Security Considerations

1. **Access control**: Tools respect agent permission boundaries
2. **Data exposure**: Don't include full transcript by default (can be large/sensitive)
3. **Rate limiting**: Prevent abuse of list/summary endpoints
4. **Audit logging**: Log execution queries for compliance

---

## Testing Checklist

- [ ] `list_recent_executions` returns correct results with user key
- [ ] `list_recent_executions` respects agent permissions with agent key
- [ ] `list_recent_executions` filters work correctly (status, trigger, hours)
- [ ] `get_execution_result` returns full execution details
- [ ] `get_execution_result` includes transcript when requested
- [ ] `get_execution_result` blocks access to unauthorized agents
- [ ] `get_agent_activity_summary` returns correct stats for single agent
- [ ] `get_agent_activity_summary` returns correct fleet summary
- [ ] `get_agent_activity_summary` includes recent failures
- [ ] Async workflow: chat_with_agent(async) → poll → get_execution_result works
- [ ] System-scoped key can query all executions
- [ ] Tools registered and visible in MCP tool list

---

## Related Documents

- `docs/memory/feature-flows/mcp-orchestration.md` - MCP tool documentation
- `docs/memory/feature-flows/parallel-headless-execution.md` - Async execution mode
- `docs/requirements/UNIFIED_EXECUTIONS_DASHBOARD.md` - Related UI feature
- `docs/planning/WORKFLOW_PRIORITIES_2026-02.md` - Strategic context

---

## Success Criteria

1. Orchestrator agents can poll for async task completion
2. Monitoring agents can query fleet health programmatically
3. Cost tracking agents can aggregate spending data
4. Access control prevents unauthorized execution data access
5. MCP tool count increases from 36 to 39
