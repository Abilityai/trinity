# Feature: Testing Agents Suite

> **Status**: Implemented and Tested
> **Created**: 2025-12-07
> **Last Updated**: 2025-12-19
> **Last Tested**: 2025-12-17 (179/179 tests passing)
> **Purpose**: Systematic verification of Trinity platform functionality using predictable test agents and automated pytest suite

---

## Overview

Trinity includes a comprehensive test suite with **179 automated pytest tests** covering backend APIs, agent-server endpoints, and platform functionality. Additionally, there are **8 testing agent repositories** (local only) designed for manual integration testing with predictable, deterministic behavior.

### Automated Test Suite (Primary)
- **Location**: `tests/` directory
- **Framework**: pytest with async support
- **Coverage**: Backend API, agent-server, schedules, credentials, templates, permissions
- **Status**: 179/179 passing (as of 2025-12-17)

### Manual Test Agents (Secondary)
- **Location**: `repositories/test-agent-*` (local repos, not in GitHub)
- **Purpose**: Manual integration testing with real containers
- **Note**: Test agent templates removed from `config.py` - use local repos for testing

## User Story

As a Trinity developer, I want predictable test agents so that I can systematically verify platform functionality without complex AI behavior interfering with test results.

---

## Test Agent Suite

| Agent | Repository | Tests | Priority | Status |
|-------|------------|-------|----------|--------|
| **test-echo** | `github:abilityai/test-agent-echo` | Basic chat, streaming | HIGH | PASSED |
| **test-counter** | `github:abilityai/test-agent-counter` | File persistence, state | MEDIUM | PASSED |
| **test-delegator** | `github:abilityai/test-agent-delegator` | Agent-to-agent (Pillar II) | HIGH | PASSED |
| **test-scheduler** | `github:abilityai/test-agent-scheduler` | Cron execution | HIGH | Configured |
| **test-queue** | `github:abilityai/test-agent-queue` | Execution queue, 429 | HIGH | Configured |
| **test-files** | `github:abilityai/test-agent-files` | File browser API | MEDIUM | Configured |
| **test-error** | `github:abilityai/test-agent-error` | Error handling | MEDIUM | Configured |

---

## Test Results (2025-12-08)

### Full System Test Session (14:00 UTC)

**Environment**: Local development (localhost:3000 + localhost:8000)
**Auth**: Dev mode with admin/trinity2024

| Step | Action | Result |
|------|--------|--------|
| 1 | Clean up existing test agents | ✅ Removed 4 containers |
| 2 | Verify Templates page | ✅ All 8 test agents displayed |
| 3 | Create test-echo via API | ✅ Agent created and running |
| 4 | Create test-counter via API | ✅ Agent created and running |
| 5 | Create test-delegator via API | ✅ Agent created and running |
| 6 | Dashboard visualization | ✅ 3 agents, all showing "Idle" |
| 7 | test-echo chat test | ✅ PASSED |
| 8 | test-counter persistence test | ✅ PASSED |
| 9 | test-delegator collaboration test | ✅ PASSED |
| 10 | Dashboard collaboration edges | ✅ "6x" edge visible |

### Verified Working

| Agent | Test | Result | Notes |
|-------|------|--------|-------|
| **test-echo** | Basic chat | ✅ PASSED | "Hello World" → "Echo: Hello World\nWords: 2\nCharacters: 11" |
| **test-counter** | State persistence | ✅ PASSED | Reset → 0, Increment → 1 |
| **test-counter** | File persistence | ✅ PASSED | counter.txt maintained, Read/Write tool calls visible |
| **test-delegator** | Trinity MCP | ✅ PASSED | Connected to Trinity MCP server |
| **test-delegator** | list_agents | ✅ PASSED | Listed all 3 test agents |
| **test-delegator** | chat_with_agent | ✅ PASSED | Delegated to test-echo, 2.2s response time |
| **Dashboard** | Agent network | ✅ PASSED | Vue Flow nodes with status indicators |
| **Dashboard** | Collaboration edges | ✅ PASSED | Edge with "6x" message count label |

### Key Findings

1. **test-echo**: Fully functional. Returns predictable echo with word/character count.

2. **test-counter**: Fully functional. State persists in `counter.txt` file. Activity panel shows Read/Write tool calls.

3. **test-delegator**: Trinity MCP integration works correctly. The agent successfully:
   - Connected to Trinity MCP server
   - Listed available agents via `mcp__trinity__list_agents`
   - Delegated messages to test-echo via `mcp__trinity__chat_with_agent`
   - Response time: 2.2 seconds for full delegation round-trip

4. **Dashboard Visualization**: Agent network shows collaboration edges with message counts. "6x" label indicates 6 messages exchanged between test-delegator and test-echo.

5. **Known Issue - Template Pre-selection**: CreateAgentModal shows "Blank Agent" when opened from Templates page "Use Template" button. The `initial-template` prop isn't being applied. **Workaround**: Create agents via API.

6. **Known Issue - Session Expiration**: After backend activity (agent creation), frontend sessions may expire. **Workaround**: Re-login with admin/trinity2024.

---

## Entry Points

### UI
- **Templates Page** (`/templates`): Test agents appear in GitHub templates section
- **Create Agent Modal**: Select any test agent template
- **Agent Detail Page**: Test via chat, verify features

### API
- `GET /api/templates` - Returns all templates including test agents
- `POST /api/agents` - Create agent from test template
- Template ID format: `github:abilityai/test-agent-{name}`

---

## Configuration

### Backend Configuration

**Current State (2025-12-19)**: Test agent templates have been removed from `config.py`. Only production templates are defined in `GITHUB_TEMPLATES`.

**Key Files** (modularized backend):

| File | Line | Purpose |
|------|------|---------|
| `src/backend/config.py` | 64 | `GITHUB_TEMPLATES` - Production templates only |
| `src/backend/config.py` | 137 | `ALL_GITHUB_TEMPLATES = GITHUB_TEMPLATES` |
| `src/backend/routers/templates.py` | 9 | Imports `ALL_GITHUB_TEMPLATES` |
| `src/backend/services/template_service.py` | 11 | Imports `ALL_GITHUB_TEMPLATES` |
| `src/backend/services/template_service.py` | 14-19 | `get_github_template()` function |

### Automated Test Suite Structure

```
tests/
  conftest.py                    # Pytest fixtures, test client setup
  test_agent_lifecycle.py        # Create, start, stop, delete agents
  test_agent_chat.py             # Chat with agents, streaming
  test_agent_files.py            # File browser API
  test_agent_git.py              # Git sync operations
  test_agent_sharing.py          # Agent sharing/permissions
  test_agent_permissions.py      # Agent-to-agent permissions
  test_auth.py                   # Authentication flows
  test_credentials.py            # Credential management
  test_execution_queue.py        # Serial execution, 429 handling
  test_mcp_keys.py               # MCP API key authentication
  test_schedules.py              # Cron scheduling
  test_settings.py               # System settings
  test_systems.py                # System manifest deployment
  test_templates.py              # Template API
  utils/
    assertions.py                # Test assertion helpers
    cleanup.py                   # Test cleanup utilities
  agent_server/                  # Direct agent-server tests
    conftest.py                  # Agent-server fixtures
    test_agent_info.py           # Info endpoint tests
    test_agent_chat_direct.py    # Direct chat tests
    test_agent_files_direct.py   # Direct file API tests
```

### Local Repository Structure (Manual Testing Only)

The 7 test agent repositories exist locally in `repositories/` for manual integration testing:

```
repositories/
  test-agent-echo/        # Basic chat, streaming
  test-agent-counter/     # File persistence, state
  test-agent-delegator/   # Agent-to-agent (Pillar II)
  test-agent-scheduler/   # Cron execution (not in GitHub)
  test-agent-queue/       # Execution queue (not in GitHub)
  test-agent-files/       # File browser API (not in GitHub)
  test-agent-error/       # Error handling (not in GitHub)
```

Each repository contains:
```
test-agent-{name}/
  template.yaml      # Trinity metadata
  CLAUDE.md          # Agent instructions
  README.md          # Human documentation
  .gitignore         # Ignore .env, .mcp.json
```

> **Note**: These are local-only repositories. They were removed from `config.py` templates. For automated testing, use the pytest suite in `tests/`.

---

## Agent Specifications

### 1. test-echo - Basic Chat Testing

**Commands**: Any text
**Response Format**:
```
Echo: [original message]
Words: [word count]
Characters: [character count]
```

**Tests**:
- Agent creation/start/stop
- Basic chat API
- Response streaming
- WebSocket status updates

### 2. test-counter - State Persistence

**Commands**: `get`, `increment`, `decrement`, `add N`, `subtract N`, `reset`
**Response Format**: `Counter: [value] (previous: [old_value])`

**Tests**:
- File read/write operations
- State persistence across messages
- Context window growth
- File browser API

### 3. test-delegator - Agent-to-Agent Communication

**Commands**: `list agents`, `delegate to [agent]: [message]`, `ping [agent]`, `chain [a1] [a2]: [msg]`, `broadcast: [msg]`

**Tests**:
- Trinity MCP injection
- Agent-scoped API keys
- `chat_with_agent` tool
- Access control (same owner)
- Collaboration events
- 429 handling for busy agents

### 5. test-scheduler - Scheduling System

**Commands**: `get log`, `clear log`, `log message: [text]`, `status`
**Log Format**: `[timestamp] [SCHEDULED|MANUAL] [message]`

**Tests**:
- Schedule creation
- Cron execution
- Execution history
- Manual trigger
- Source differentiation

### 6. test-queue - Execution Queue

**Commands**: `delay [N]`, `quick`, `status`, `busy [N]`

**Tests**:
- Serial execution
- Queue position tracking
- 429 when queue full
- Queue status API
- Defense-in-depth

### 7. test-files - File Browser

**Commands**: `create [file]: [content]`, `read [file]`, `list`, `create-tree`, `delete [file]`, `large [N]`, `hidden`

**Tests**:
- File listing API
- File download API
- Directory traversal
- Hidden file filtering
- File size limits (100MB)

### 8. test-error - Error Handling

**Commands**: `normal`, `fail`, `exception`, `slow [N]`, `empty`, `large`, `malformed`

**Tests**:
- Failed execution tracking
- Activity state (failed)
- Response truncation
- Timeout handling
- Error recovery

---

## Multi-Agent Test Scenarios

### Scenario 1: Basic Collaboration (VERIFIED 2025-12-08)
```
Setup: test-delegator + test-echo

1. Start both agents
2. Send to delegator: "delegate to test-echo: Hello World"
3. Verify: Echo response received, Dashboard shows collaboration event
```

### Scenario 2: Queue Under Load
```
Setup: test-queue

1. Send 5 simultaneous "delay 30" requests
2. Expected:
   - 1 request runs immediately
   - 3 requests queue (queue size = 3)
   - 1 request returns 429
3. Verify via GET /api/agents/test-queue/queue
```

### Scenario 3: Scheduled Delegation
```
Setup: test-delegator + test-counter

1. Reset counter in test-counter
2. Create schedule: "delegate to test-counter: increment" every minute
3. Wait 3 minutes
4. Verify counter = 3, collaboration events logged
```

---

## Testing

### Prerequisites
- [x] Backend running at http://localhost:8000
- [x] Frontend running at http://localhost:3000
- [x] Docker daemon running
- [x] GitHub PAT configured for private repos

### Test: Template Discovery

1. Navigate to Templates page (`/templates`)
2. **Expected**: 8 test agents appear in GitHub templates section
3. **Verify**: Each shows "Test:" prefix in display name

### Test: Create Test Agent

1. Click "Use Template" on test-echo
2. Enter name: "my-test-echo"
3. Click "Create"
4. **Expected**: Agent created and starts automatically
5. **Verify**: Agent appears in agents list with "running" status

### Test: Echo Response (PASSED 2025-12-08)

1. Navigate to my-test-echo detail page
2. Send message: "Hello World"
3. **Expected Response**:
   ```
   Echo: Hello World
   Words: 2
   Characters: 11
   ```

### Test: Counter Persistence (PASSED 2025-12-08)

1. Create test-counter agent
2. Send: "reset" -> Counter: 0
3. Send: "increment" -> Counter: 1
4. Send: "add 10" -> Counter: 11
5. **Verify**: File browser shows counter.txt

### Test: Agent Collaboration (PASSED 2025-12-08)

1. Create test-delegator and test-echo
2. Ensure both running
3. Send to delegator: "delegate to test-echo: ping"
4. **Verify**:
   - Response includes echo output
   - Dashboard shows collaboration edge animation

### Edge Cases

- [ ] Create agent when GitHub repo unavailable
- [ ] Delegate to stopped agent (should fail gracefully)
- [ ] Queue full (send 5+ "delay 60" to test-queue)
- [ ] Access denied (delegate to agent owned by different user)

### Cleanup

```bash
# Delete all test agents
docker rm -f $(docker ps -a --filter "name=test-" --format "{{.Names}}")
```

---

## Repository Structure

Each test agent repository:
```
test-agent-{name}/
  template.yaml      # Trinity metadata
  CLAUDE.md          # Agent instructions
  README.md          # Human documentation
  .gitignore         # Ignore .env, .mcp.json
```

### GitHub Repositories

| Agent | URL | Status |
|-------|-----|--------|
| test-echo | https://github.com/Abilityai/test-agent-echo | Local only |
| test-counter | https://github.com/Abilityai/test-agent-counter | Local only |
| test-worker | https://github.com/Abilityai/test-agent-worker | Local only |
| test-delegator | https://github.com/Abilityai/test-agent-delegator | Local only |
| test-scheduler | https://github.com/Abilityai/test-agent-scheduler | Local only |
| test-queue | https://github.com/Abilityai/test-agent-queue | Local only |
| test-files | https://github.com/Abilityai/test-agent-files | Local only |
| test-error | https://github.com/Abilityai/test-agent-error | Local only |

> **Action Required**: Push local repositories to GitHub to enable GitHub-based template cloning.

---

## Related Documentation

- **Testing Guide**: `docs/TESTING_GUIDE.md`
- **Agent Lifecycle**: `docs/memory/feature-flows/agent-lifecycle.md`
- **Agent Network**: `docs/memory/feature-flows/agent-network.md`
- **Execution Queue**: `docs/memory/feature-flows/execution-queue.md`

### Key Source Files

| Component | File Path |
|-----------|-----------|
| Backend Config | `src/backend/config.py` |
| Templates Router | `src/backend/routers/templates.py` |
| Template Service | `src/backend/services/template_service.py` |
| Agent-Server Main | `docker/base-image/agent_server/main.py` |
| Agent-Server Chat | `docker/base-image/agent_server/routers/chat.py` |
| Agent-Server Files | `docker/base-image/agent_server/routers/files.py` |
| Agent-Server Plans | `docker/base-image/agent_server/routers/plans.py` |
| Test Fixtures | `tests/conftest.py` |

---

## Revision History

| Date | Changes |
|------|---------|
| 2025-12-07 | Initial implementation - 8 test agents designed and 4 configured |
| 2025-12-08 | All 8 test agents configured in `config.py` (added test-scheduler, test-queue, test-files, test-error) |
| 2025-12-08 | Test results: test-echo PASSED, test-counter PASSED, test-worker PARTIAL, test-delegator PASSED |
| 2025-12-08 | Verified Trinity MCP integration with test-delegator successfully delegating to test-echo |
| 2025-12-08 | All 8 local repositories created in `repositories/` folder |
| 2025-12-08 | Full system test via UI - test-echo, test-counter, test-delegator all PASSED |
| 2025-12-08 | Dashboard collaboration edges verified - "6x" message count label visible |
| 2025-12-08 | Documented known issues: Template pre-selection bug, session expiration |
| 2025-12-17 | Automated pytest suite: 179/179 tests passing |
| 2025-12-19 | Updated documentation: Test agent templates removed from config.py, backend modularized (routers/), agent-server refactored to modular package (docker/base-image/agent_server/) |
