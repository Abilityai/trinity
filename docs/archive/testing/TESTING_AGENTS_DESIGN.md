# Trinity Testing Agents - Design Document

> **Status**: Design Phase
> **Created**: 2025-12-07
> **Purpose**: Define a suite of testing agents for systematic verification of Trinity platform functionality

---

## Overview

This document defines a suite of **testing agents** designed to systematically verify all core functionality of the Trinity autonomous agent platform. These agents are:

1. **Simple & Predictable** - Each agent does ONE thing well with deterministic behavior
2. **Fast Response** - Minimal AI reasoning, mostly echo/transform operations
3. **Isolated Testing** - Can test individual features without dependencies
4. **Combinable** - Can be used together for integration/multi-agent testing
5. **Observable** - Clear success/failure criteria with logging

---

## Functionality Coverage Matrix

Based on Trinity requirements and architecture, here's what needs to be tested:

| Category | Feature | Test Agent | Priority |
|----------|---------|------------|----------|
| **Core Agent** | Create/Start/Stop/Delete | Any agent | HIGH |
| **Chat** | Send message, receive response | test-echo | HIGH |
| **Chat** | Streaming responses | test-echo | HIGH |
| **Chat** | Context window tracking | test-counter | MEDIUM |
| **Chat** | Session persistence | test-counter | MEDIUM |
| **Workplan** | Create plan with tasks | test-worker | HIGH |
| **Workplan** | Task dependencies (blocked→pending) | test-worker | HIGH |
| **Workplan** | Plan completion/archiving | test-worker | HIGH |
| **Workplan** | Visibility in Dashboard | test-worker | HIGH |
| **Execution Queue** | Serial execution | test-queue | HIGH |
| **Execution Queue** | 429 on queue full | test-queue | HIGH |
| **Execution Queue** | Queue status API | test-queue | MEDIUM |
| **Scheduling** | Cron execution | test-scheduler | HIGH |
| **Scheduling** | Execution history | test-scheduler | MEDIUM |
| **Agent-to-Agent** | MCP chat_with_agent | test-delegator | HIGH |
| **Agent-to-Agent** | Access control (same owner) | test-delegator | HIGH |
| **Agent-to-Agent** | Collaboration events | test-delegator | HIGH |
| **File Browser** | List workspace files | test-files | MEDIUM |
| **File Browser** | Download files | test-files | MEDIUM |
| **Credentials** | .env injection | test-credential | MEDIUM |
| **Credentials** | Hot-reload | test-credential | LOW |
| **Error Handling** | Failed execution | test-error | MEDIUM |
| **Error Handling** | Timeout handling | test-error | LOW |

---

## Testing Agents

### 1. test-echo - Basic Chat Testing

**Purpose**: Verify basic chat functionality with predictable responses.

**Behavior**:
- Echoes any message with prefix `Echo: `
- Returns word count and character count
- No external dependencies, fastest possible response

**Repository Structure**:
```
test-echo/
├── template.yaml
├── CLAUDE.md
├── README.md
└── .gitignore
```

**template.yaml**:
```yaml
name: test-echo
display_name: "Test: Echo"
description: "Simple echo agent for testing basic chat functionality"
version: "1.0"
author: "Trinity Test Suite"

resources:
  cpu: "1"
  memory: "2g"

capabilities:
  - testing
  - echo
  - basic-chat

use_cases:
  - "Hello world"
  - "Test message 123"
```

**CLAUDE.md**:
```markdown
# Echo Agent

You are a simple echo agent for testing. Your ONLY job is to echo back messages.

## Instructions

1. For ANY message, respond with EXACTLY this format:
   ```
   Echo: [original message]
   Words: [word count]
   Characters: [character count]
   ```

2. Do NOT engage in conversation. Do NOT try to be helpful. Just echo.

3. Examples:
   - Input: "Hello world"
   - Output: "Echo: Hello world\nWords: 2\nCharacters: 11"

4. NEVER deviate from this format. NEVER add explanations.
```

**Tests Enabled**:
- Agent creation
- Agent start/stop
- Basic chat API
- Response parsing
- Streaming verification
- WebSocket status updates

---

### 2. test-counter - State Persistence Testing

**Purpose**: Verify stateful behavior, file persistence, and context growth.

**Behavior**:
- Maintains a counter in `counter.txt`
- Commands: `increment`, `decrement`, `get`, `reset`, `add N`, `subtract N`
- Shows context usage growing over conversation

**template.yaml**:
```yaml
name: test-counter
display_name: "Test: Counter"
description: "Stateful counter agent for testing file persistence"
version: "1.0"
author: "Trinity Test Suite"

resources:
  cpu: "1"
  memory: "2g"

capabilities:
  - testing
  - state-persistence
  - file-operations

use_cases:
  - "get"
  - "increment"
  - "add 10"
  - "reset"
```

**CLAUDE.md**:
```markdown
# Counter Agent

You maintain a persistent counter stored in `counter.txt`.

## Commands

| Command | Description |
|---------|-------------|
| `get` | Read and return current counter value |
| `increment` | Add 1 to counter |
| `decrement` | Subtract 1 from counter |
| `add N` | Add N to counter (e.g., "add 5") |
| `subtract N` | Subtract N from counter |
| `reset` | Reset counter to 0 |

## Behavior

1. ALWAYS read counter.txt first (create with 0 if doesn't exist)
2. Perform the operation
3. Write new value to counter.txt
4. Respond with: "Counter: [value] (previous: [old_value])"

## File Format

counter.txt contains a single integer value.

## Examples

User: "increment"
You: *read counter.txt* → 5
You: *write 6 to counter.txt*
Response: "Counter: 6 (previous: 5)"

User: "add 10"
You: *read counter.txt* → 6
You: *write 16 to counter.txt*
Response: "Counter: 16 (previous: 6)"

## Error Handling

If counter.txt doesn't exist, create it with value 0.
If command is unrecognized, respond: "Unknown command. Use: get, increment, decrement, add N, subtract N, reset"
```

**Tests Enabled**:
- File read/write operations
- State persistence across messages
- Context window growth tracking
- File browser API (can see counter.txt)
- Session cost accumulation

---

### 3. test-worker - Workplan System Testing

**Purpose**: Verify Pillar I (Explicit Planning) - workplan creation, task execution, dependencies.

**Behavior**:
- Accepts work items and creates workplans
- Reports task completion
- Exercises full plan lifecycle

**template.yaml**:
```yaml
name: test-worker
display_name: "Test: Worker"
description: "Task execution agent for testing the Workplan system"
version: "1.0"
author: "Trinity Test Suite"

resources:
  cpu: "1"
  memory: "2g"

capabilities:
  - testing
  - workplan
  - task-execution

commands:
  - name: workplan-create
    description: "Create a new workplan"
  - name: workplan-status
    description: "Check current plan status"
  - name: workplan-update
    description: "Update task status"
  - name: workplan-list
    description: "List all plans"

use_cases:
  - "create plan: Test workflow"
  - "complete task: task-1"
  - "plan status"
```

**CLAUDE.md**:
```markdown
# Worker Agent

You are a task execution agent that uses the Trinity Workplan system.

## Available Commands

- `/workplan-create` - Create a new workplan
- `/workplan-status` - Check current plan status
- `/workplan-update` - Update task status
- `/workplan-list` - List all plans

## Behavior

### On receiving "create plan: [description]"

1. Use `/workplan-create` to create a plan with 3 tasks:
   - task-1: "Initialize" (no dependencies)
   - task-2: "Process" (depends on task-1)
   - task-3: "Finalize" (depends on task-2)
2. Report: "Created plan [id] with 3 tasks. task-1 is pending, task-2 and task-3 are blocked."

### On receiving "complete task: [task-id]"

1. Use `/workplan-update` to mark task as completed
2. Report the update and any tasks that became unblocked
3. Example: "Completed task-1. task-2 is now pending."

### On receiving "plan status"

1. Use `/workplan-status` to get current plan state
2. Report all tasks and their statuses in a table format

### On receiving "fail task: [task-id]"

1. Use `/workplan-update` to mark task as failed
2. Report: "task-[id] marked as failed. Plan status: [status]"

### On receiving "complete plan"

1. Mark all pending tasks as completed in order
2. Report final plan status

## Response Format

Always include:
- Plan ID (if applicable)
- Task statuses (pending, active, completed, failed, blocked)
- Dependency information
```

**Tests Enabled**:
- Workplan creation API
- Task dependencies (blocked→pending transitions)
- Plan completion and archiving
- Plan visibility in Dashboard
- Cross-agent plan aggregation
- Trinity command injection

---

### 4. test-delegator - Agent-to-Agent Communication

**Purpose**: Verify Pillar II (Hierarchical Delegation) - inter-agent communication via MCP.

**Behavior**:
- Delegates tasks to other agents using Trinity MCP
- Tests access control rules
- Generates collaboration events

**template.yaml**:
```yaml
name: test-delegator
display_name: "Test: Delegator"
description: "Orchestration agent for testing agent-to-agent communication"
version: "1.0"
author: "Trinity Test Suite"

resources:
  cpu: "1"
  memory: "2g"

capabilities:
  - testing
  - delegation
  - orchestration
  - agent-to-agent

mcp_servers:
  - name: trinity
    description: "Trinity platform orchestration"

use_cases:
  - "list agents"
  - "delegate to test-echo: Hello"
  - "ping test-counter"
```

**CLAUDE.md**:
```markdown
# Delegator Agent

You orchestrate work by delegating to other agents via Trinity MCP.

## Available Trinity MCP Tools

- `mcp__trinity__list_agents` - Discover available agents
- `mcp__trinity__chat_with_agent` - Send message to another agent
- `mcp__trinity__get_chat_history` - Get conversation history

## Commands

### "list agents"

Call `mcp__trinity__list_agents()` and report:
- Total agent count
- Running vs stopped
- Agent names and their status

### "delegate to [agent]: [message]"

1. Call `mcp__trinity__chat_with_agent(agent_name="[agent]", message="[message]")`
2. Report: "Sent to [agent]: [message]\nReceived: [response]"

### "ping [agent]"

1. Send "ping" to the named agent
2. If response received: "Agent [agent] is responsive"
3. If error: "Agent [agent] is not responsive: [error]"

### "chain [agent1] [agent2]: [message]"

1. Send message to agent1
2. Send agent1's response to agent2
3. Report both exchanges and final response

### "broadcast: [message]"

1. Get list of all running agents
2. Send message to each
3. Report all responses

## Error Handling

| Error | Response |
|-------|----------|
| Access denied | "Access denied to [agent]: [reason]" |
| Agent busy | "Agent [agent] busy, queue full. Retry in [N] seconds." |
| Agent not found | "Agent [agent] not found" |
| Agent stopped | "Agent [agent] is not running" |

## Response Format

Always include:
- Source agent (this agent)
- Target agent
- Message sent
- Response received (or error)
- Timestamp
```

**Tests Enabled**:
- Trinity MCP injection
- Agent-scoped API keys
- `chat_with_agent` tool
- Access control (same owner)
- Collaboration events in Dashboard
- WebSocket `agent_collaboration` broadcasts
- 429 handling for busy agents

---

### 5. test-scheduler - Scheduling System Testing

**Purpose**: Verify scheduled autonomous execution.

**Behavior**:
- Logs execution timestamps to file
- Differentiates scheduled vs manual execution
- Allows testing cron functionality

**template.yaml**:
```yaml
name: test-scheduler
display_name: "Test: Scheduler"
description: "Scheduling test agent for cron execution verification"
version: "1.0"
author: "Trinity Test Suite"

resources:
  cpu: "1"
  memory: "2g"

capabilities:
  - testing
  - scheduling
  - autonomous

use_cases:
  - "get log"
  - "clear log"
  - "log message: Test entry"
```

**CLAUDE.md**:
```markdown
# Scheduler Test Agent

You execute scheduled tasks and log them for verification.

## Scheduled Execution Behavior

When executed via schedule (you'll see this in the execution context):
1. Get current UTC timestamp
2. Append to `schedule-log.txt`: "[timestamp] [SCHEDULED] Execution triggered"
3. Respond: "[SCHEDULED] Execution logged at [timestamp]"

## Manual Commands

### "get log"

Read and return contents of schedule-log.txt.
If file doesn't exist: "Log is empty"

### "clear log"

Delete schedule-log.txt and respond: "Log cleared"

### "log message: [text]"

Append "[timestamp] [MANUAL] [text]" to schedule-log.txt
Respond: "Logged: [text]"

### "status"

Report:
- Log file exists: yes/no
- Line count: N
- Last entry: [entry] or "none"

## Log File Format

Each line in schedule-log.txt:
```
2025-12-07T10:30:00Z [SCHEDULED] Execution triggered
2025-12-07T10:31:00Z [MANUAL] User test message
```

## Response Format

Always prefix responses with:
- `[SCHEDULED]` for cron-triggered execution
- `[MANUAL]` for user-triggered execution

This allows test verification of execution source.
```

**Tests Enabled**:
- Schedule creation via API
- Cron-style execution
- Execution history tracking
- Manual trigger
- Schedule enable/disable
- Execution source differentiation

---

### 6. test-queue - Execution Queue Testing

**Purpose**: Verify serial execution and queue behavior.

**Behavior**:
- Configurable delay before responding
- Reports execution metadata
- Used to test concurrent request handling

**template.yaml**:
```yaml
name: test-queue
display_name: "Test: Queue"
description: "Queue test agent with configurable delays"
version: "1.0"
author: "Trinity Test Suite"

resources:
  cpu: "1"
  memory: "2g"

capabilities:
  - testing
  - queue
  - delay

use_cases:
  - "delay 30"
  - "quick"
  - "status"
```

**CLAUDE.md**:
```markdown
# Queue Test Agent

You help test the execution queue by introducing configurable delays.

## Commands

### "delay [N]" (default: 10)

1. Record start timestamp
2. Wait N seconds (use bash sleep or similar)
3. Record end timestamp
4. Respond: "Completed after [N] second delay\nStarted: [start]\nEnded: [end]"

### "quick"

Respond immediately with: "Quick response at [timestamp]"

### "status"

Report execution context if available:
- execution_id
- queue_status (running/queued)
- source (user/schedule/agent)
- Current timestamp

### "busy [N]"

Simulate CPU-intensive work for N seconds.
Use a loop that consumes time.

## Timing Accuracy

For delay commands, measure actual elapsed time and report:
- Requested delay: [N]s
- Actual delay: [X.XX]s

## Testing Instructions

To test queue behavior:
1. Send "delay 30" to this agent
2. While still running, send another message
3. Second message should queue (or return 429 if queue full)
4. Check queue status via API: GET /api/agents/test-queue/queue

## Queue Full Test

Send 4+ rapid "delay 60" requests to trigger 429 response.
First request runs, next 3 queue, 4th+ should return 429.
```

**Tests Enabled**:
- Single execution at a time
- Queue position tracking
- 429 when queue full
- Queue status API
- Queue clear/release operations
- Defense-in-depth (container lock)

---

### 7. test-files - File Browser Testing

**Purpose**: Verify workspace file operations and file browser API.

**Behavior**:
- Creates, reads, and manages files in workspace
- Generates test directory structures

**template.yaml**:
```yaml
name: test-files
display_name: "Test: Files"
description: "File operations agent for testing file browser"
version: "1.0"
author: "Trinity Test Suite"

resources:
  cpu: "1"
  memory: "2g"

capabilities:
  - testing
  - file-operations
  - file-browser

use_cases:
  - "create test.txt: Hello World"
  - "list"
  - "create-tree"
```

**CLAUDE.md**:
```markdown
# File Test Agent

You manage files in the workspace for testing the file browser feature.

## Commands

### "create [filename]: [content]"

Create a file with given content.
Respond: "Created [filename] ([size] bytes)"

### "read [filename]"

Read and return file contents.
If not found: "File not found: [filename]"

### "list"

List all files in workspace (excluding hidden files).
Format:
```
Files in workspace:
- file1.txt (123 bytes)
- file2.json (456 bytes)
- subdir/ (directory)
```

### "create-tree"

Create a test directory structure:
```
test-dir/
├── file1.txt (contains "content 1")
├── file2.json (contains {"test": true})
└── subdir/
    └── nested.md (contains "# Nested")
```

Respond: "Created test directory tree with 3 files"

### "delete [filename]"

Delete the specified file.
Respond: "Deleted [filename]" or "File not found: [filename]"

### "large [N]"

Create a file `large-test.txt` with N KB of content (repeated "x" characters).
Respond: "Created large-test.txt ([N] KB)"

### "hidden"

Create hidden files for testing filtering:
- .hidden-file
- .secret-data

Respond: "Created hidden test files"

## File Browser API Testing

After creating files, test via:
1. GET /api/agents/test-files/files - Should list non-hidden files
2. GET /api/agents/test-files/files/download?path=test.txt - Download specific file
```

**Tests Enabled**:
- File listing API
- File download API
- Directory traversal
- File metadata (size, modified date)
- Hidden file filtering
- File size limits

---

### 8. test-error - Error Handling Testing

**Purpose**: Verify platform handles failures gracefully.

**Behavior**:
- Fails on command in controlled ways
- Tests various error scenarios

**template.yaml**:
```yaml
name: test-error
display_name: "Test: Error"
description: "Error handling test agent for failure scenarios"
version: "1.0"
author: "Trinity Test Suite"

resources:
  cpu: "1"
  memory: "2g"

capabilities:
  - testing
  - error-handling
  - failure-scenarios

use_cases:
  - "normal"
  - "fail"
  - "timeout"
```

**CLAUDE.md**:
```markdown
# Error Test Agent

You help test error handling by failing in controlled ways.

## Commands

### "normal"

Respond normally: "OK - normal response at [timestamp]"

### "fail"

1. Print "INTENTIONAL_FAILURE" to stdout
2. Exit the current operation with an error
3. The platform should record this as a failed execution

### "exception"

Attempt to execute invalid code to raise an exception.

### "slow [N]" (default: 300)

Sleep for N seconds. Default is 5 minutes.
Use this to test timeout handling.
Respond after: "Completed slow operation after [N] seconds"

### "empty"

Return a completely empty response (no text at all).

### "large"

Return a response with 100KB of repeated text.
Used to test response truncation.
Format: "START" + 100KB of "x" + "END"

### "malformed"

Return malformed/corrupted output.

## Error Testing Instructions

1. **Test fail**: Send "fail", verify activity shows as failed
2. **Test timeout**: Send "slow 400", verify timeout handling (default 600s)
3. **Test recovery**: After "fail", send "normal" to verify agent recovers

## Expected Platform Behavior

| Command | Expected |
|---------|----------|
| fail | Activity state: FAILED |
| slow 400 | Either completes or times out |
| empty | Should handle gracefully |
| large | Response truncated |
```

**Tests Enabled**:
- Failed execution tracking
- Activity state (failed)
- Response truncation
- Timeout handling
- Error recovery

---

## Multi-Agent Test Scenarios

### Scenario 1: Basic Multi-Agent Collaboration

**Setup**: Create `test-delegator` and `test-echo`

**Test Steps**:
1. Start both agents
2. Send to delegator: "delegate to test-echo: Hello World"
3. Verify delegator receives echo response
4. Check Dashboard for collaboration event

**Expected**:
- Echo response: "Echo: Hello World\nWords: 2\nCharacters: 11"
- WebSocket broadcasts `agent_collaboration` event
- Dashboard shows connection between agents

---

### Scenario 2: Queue Under Load

**Setup**: Create `test-queue`

**Test Steps**:
1. Send 5 simultaneous "delay 30" requests
2. Check queue status immediately
3. Wait for all to complete

**Expected**:
- 1 request running
- 3 requests queued (queue size = 3)
- 1 request returns 429 (queue full)
- All complete sequentially

---

### Scenario 3: Workplan Lifecycle

**Setup**: Create `test-worker`

**Test Steps**:
1. Send: "create plan: Integration Test"
2. Check Dashboard for plan visibility
3. Send: "complete task: task-1"
4. Verify task-2 unblocks
5. Complete remaining tasks
6. Verify plan moves to archive

**Expected**:
- Plan visible in Dashboard
- Dependency transitions work correctly
- Plan status changes to completed
- Plan file moves to plans/archive/

---

### Scenario 4: Scheduled Delegation

**Setup**: Create `test-delegator` and `test-counter`

**Test Steps**:
1. Reset counter: Send "reset" to test-counter
2. Create schedule on test-delegator: "delegate to test-counter: increment" every minute
3. Wait 3 minutes
4. Check counter value

**Expected**:
- Counter should be 3 (incremented 3 times)
- Schedule executions recorded
- Collaboration events logged

---

### Scenario 5: Chain of Agents

**Setup**: Create `test-echo`, `test-counter`, `test-delegator`

**Test Steps**:
1. Reset counter
2. Send to delegator: "chain test-echo test-counter: increment"
3. Echo receives "increment", responds with echo
4. Counter receives echo response (may not understand it)

**Expected**:
- Multi-hop communication works
- All collaboration events logged
- Dashboard shows all connections

---

### Scenario 6: Access Control Verification

**Setup**:
- Create `test-delegator-a` owned by user A
- Create `test-echo-b` owned by user B (not shared)

**Test Steps**:
1. From delegator-a: "delegate to test-echo-b: Hello"
2. Verify access denied

**Expected**:
- Access denied response
- Audit log shows denied access
- No collaboration event (blocked)

---

## Template Repository Structure

Each testing agent will be a GitHub repository with this structure:

```
test-{name}/
├── template.yaml          # Trinity metadata
├── CLAUDE.md              # Agent instructions
├── README.md              # Human documentation
└── .gitignore             # Ignore .env, .mcp.json
```

### Standard .gitignore

```
.env
.mcp.json
*.log
```

### Standard README.md Template

```markdown
# Test Agent: {Name}

## Purpose
{Description of what this agent tests}

## Commands
{List of available commands}

## Testing
{How to use this agent for testing Trinity}

## Related Features
{Links to Trinity features this agent tests}
```

---

## Implementation Plan

### Phase 1: Core Testing Agents

1. **test-echo** - Basic chat verification
2. **test-counter** - State persistence
3. **test-worker** - Workplan system

### Phase 2: Infrastructure Testing Agents

4. **test-queue** - Execution queue
5. **test-scheduler** - Scheduling system
6. **test-files** - File browser

### Phase 3: Integration Testing Agents

7. **test-delegator** - Agent-to-agent communication
8. **test-error** - Error handling

### Phase 4: Test Automation

- Create test scripts that exercise all agents
- Document test procedures for each scenario
- Consider GitHub Actions for automated testing

---

## GitHub Repository Locations

Proposed repository locations (to be created):

| Agent | Repository |
|-------|------------|
| test-echo | github:AbilityAI/trinity-test-echo |
| test-counter | github:AbilityAI/trinity-test-counter |
| test-worker | github:AbilityAI/trinity-test-worker |
| test-delegator | github:AbilityAI/trinity-test-delegator |
| test-scheduler | github:AbilityAI/trinity-test-scheduler |
| test-queue | github:AbilityAI/trinity-test-queue |
| test-files | github:AbilityAI/trinity-test-files |
| test-error | github:AbilityAI/trinity-test-error |

---

## Next Steps

1. [ ] Review and approve this design
2. [ ] Create GitHub repositories for each testing agent
3. [ ] Implement agent templates
4. [ ] Create test procedure documentation
5. [ ] Run initial test suite
6. [ ] Document results and refine agents

---

## Revision History

| Date | Changes |
|------|---------|
| 2025-12-07 | Initial design document |
