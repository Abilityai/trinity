# Trinity Testing Guide - Simple & Practical

> **Philosophy**: Feature flows include testing instructions. Follow them to verify everything works.
>
> **Last Updated**: 2025-12-08

---

## Approach

### 1. Every Feature Flow Has a Testing Section

Each feature flow document (`docs/memory/feature-flows/*.md`) includes:

```markdown
## Testing

### Prerequisites
- [ ] Services running (backend, frontend, Redis, etc.)
- [ ] Test user logged in
- [ ] Agent running (if testing agent features)

### Test Steps

#### 1. [Feature Action Name]
**Action**: Describe what to do
**Expected**: What should happen
**Verify**:
- [ ] UI shows correct state
- [ ] API returns expected response
- [ ] Database updated correctly

### Edge Cases
- [ ] What happens if...
- [ ] Test with invalid data...

**Last Tested**: YYYY-MM-DD
**Status**: ✅ Working / ⚠️ Issues Found / ❌ Broken
```

### 2. Testing Happens Before Marking Features Complete

When implementing or modifying a feature:
1. Read the feature flow document
2. Follow the Testing section step-by-step
3. Verify each step works as documented
4. Update "Last Tested" timestamp
5. Document any issues found

### 3. Manual Integration Testing > Automated Tests

**Manual testing** (following documented steps) catches:
- Integration issues
- UX problems
- Edge cases
- Real-world workflows

**Automated tests** only when:
- Feature breaks repeatedly
- Critical path that must never break
- Regression prevention needed

---

## Feature Flows Reference

All feature flows are indexed in `docs/memory/feature-flows.md`. Key flows:

### Core Features (High Priority)
| Flow | Document | Status |
|------|----------|--------|
| Agent Lifecycle | `agent-lifecycle.md` | ✅ Tested 2025-12-07 |
| Agent Terminal | `agent-terminal.md` | ✅ Working |
| Authentication | `email-authentication.md` | ✅ Working |
| Agent Network | `agent-network.md` | ✅ Tested 2025-12-07 |
| Execution Queue | `execution-queue.md` | ✅ Ready for testing |

### Supporting Features
| Flow | Document | Status |
|------|----------|--------|
| Credential Injection | `credential-injection.md` | ✅ Working |
| Agent Scheduling | `scheduling.md` | ✅ Working |
| File Browser | `file-browser.md` | ✅ Working |
| Agent Sharing | `agent-sharing.md` | ✅ Working |
| MCP Orchestration | `mcp-orchestration.md` | ✅ Working |
| Activity Stream | `activity-stream.md` | ✅ Working |

---

## Testing Section Template

Copy this into each feature flow:

```markdown
## Testing

### Prerequisites
- [ ] Backend running at http://localhost:8000
- [ ] Frontend running at http://localhost
- [ ] Docker daemon running
- [ ] Redis running (for queue/credential features)
- [ ] Logged in (via email auth or admin login)
- [ ] Test agent created and running

### Test Steps

#### 1. [Feature Action Name]
**Action**:
- Step-by-step description
- Include specific URLs, buttons, inputs

**Expected**:
- What should happen immediately
- Any WebSocket updates
- Toast notifications

**Verify**:
- [ ] UI: Check specific element states
- [ ] API: `curl` command or browser DevTools
- [ ] Database: SQL query or API response
- [ ] Docker: Container state if applicable

#### 2. [Next Action]
...

### Edge Cases
- [ ] Invalid input: What error message?
- [ ] Unauthorized access: 403 response?
- [ ] Concurrent operations: Race conditions?
- [ ] Network failure: Graceful degradation?

### Cleanup
- [ ] Delete test data
- [ ] Reset state
- [ ] Verify no orphaned resources

**Last Tested**: YYYY-MM-DD
**Tested By**: claude / human
**Status**: ✅ All tests passed
**Issues**: None (or list issues found)
```

---

## Example: Agent Lifecycle Testing

```markdown
## Testing

### Prerequisites
- [ ] Backend running at http://localhost:8000
- [ ] Frontend running at http://localhost
- [ ] Docker daemon running
- [ ] Logged in as user@example.com

### 1. Create Agent
**Action**:
- Navigate to http://localhost
- Click "Create Agent" button
- Enter name: "test-lifecycle"
- Select template: "local:default"
- Click "Create"

**Expected**:
- Agent appears in agent list
- Status shows "running"
- SSH port assigned (2290+)
- WebSocket broadcast received

**Verify**:
- [ ] UI shows agent card with correct name
- [ ] API: GET /api/agents includes agent
- [ ] Docker: `docker ps | grep test-lifecycle` shows container
- [ ] Database: agent_ownership record exists
- [ ] Container has correct labels

### 2. Start Agent
**Action**: Click "Start" button on stopped agent

**Expected**:
- Button shows loading spinner
- Status changes to "running"
- Trinity meta-prompt injected

**Verify**:
- [ ] UI shows "running" badge
- [ ] Docker: container status is "Up"
- [ ] Trinity injection: Agent has planning commands

### 3. Stop Agent
**Action**: Click "Stop" button

**Expected**:
- Status changes to "stopped"
- Container stops but isn't removed

### 4. Delete Agent
**Action**: Click trash icon, confirm deletion

**Expected**:
- Agent removed from list
- Container deleted
- All resources cleaned up

**Edge Cases**:
- [ ] Duplicate name (should fail with 400)
- [ ] Unauthorized delete (should fail with 403)
- [ ] Start already running agent (idempotent)

**Cleanup**:
- [ ] Delete test-lifecycle if exists
- [ ] `docker ps -a | grep test-` - verify no orphans

**Last Tested**: 2025-12-07
**Status**: ✅ All tests passed
```

---

## How Claude Code Uses This

### When Implementing a Feature

1. **Read feature flow**: Understand what to build
2. **Implement the feature**: Write the code
3. **Follow testing instructions**: Execute each test step
4. **Document results**: Update "Last Tested" and "Status"
5. **Report issues**: If anything fails, document in "Issues"

### When Modifying a Feature

1. **Update feature flow**: Document the changes
2. **Update testing section**: Add new test steps
3. **Run all tests**: Ensure nothing broke
4. **Update timestamp**: Mark as tested

### When Debugging

1. **Read testing section**: See how feature should work
2. **Reproduce issue**: Follow test steps
3. **Identify failure point**: Find where actual ≠ expected
4. **Fix and retest**: Follow all steps again

---

## Testing Checklist for Feature Completion

Before marking a feature as ✅ Implemented:

- [ ] Feature flow document exists
- [ ] Testing section completed with instructions
- [ ] All test steps executed successfully
- [ ] Edge cases tested
- [ ] "Last Tested" timestamp updated
- [ ] "Status" marked as ✅ Working
- [ ] Changelog entry added
- [ ] Requirements.md updated if new feature

---

## When to Add Automated Tests

Add automated tests only when:

1. **Feature broke in production** - Prevent regression
2. **Critical user path** - Must never break (auth, agent creation)
3. **Complex edge cases** - Hard to test manually every time
4. **API contract** - External integrations that need stability

**How to add**:
- Create `tests/integration/test_{feature}.py`
- Link to feature flow in header comment
- Focus on the specific scenario that needs automation
- Keep tests simple and focused

---

## API Testing Best Practices

### ✅ Recommended Methods

1. **Python `requests` library**
   ```python
   import requests

   token = 'eyJhbGc...'
   headers = {'Authorization': f'Bearer {token}'}
   response = requests.get('http://localhost:8000/api/agents', headers=headers)
   ```

2. **Browser DevTools** - Best for integration testing user flows
   - Network tab: Monitor API calls, check request/response
   - WebSocket frames: Verify real-time updates
   - Application tab: Check localStorage persistence

3. **Postman/Insomnia** - GUI tools for manual API testing

### ❌ Avoid

**curl with bash variables** - Tokens can be truncated due to shell escaping:
```bash
# DON'T DO THIS - token may be truncated
TOKEN='eyJhbGc...'
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents
```

**Why it fails:**
- Bash variable expansion with special characters
- JWT tokens contain `-`, `_`, `.` which can cause issues
- Use Python or save token to file instead

### Debugging Authentication

If you get `401 Unauthorized`:
1. ✅ Test with Python `requests` first (rules out client issues)
2. ✅ Check backend logs for actual error
3. ✅ Verify token expiration (`exp` claim)
4. ❌ Don't assume it's a backend bug - verify with multiple clients

---

## WebSocket Testing

Many Trinity features use WebSocket for real-time updates:

### Monitoring WebSocket Events

1. **Browser DevTools** → Network → WS
2. Click the WebSocket connection (`ws://localhost:8000/ws`)
3. View Messages tab for incoming events

### Expected Events

| Event | Trigger | Payload |
|-------|---------|---------|
| `agent_created` | Create agent | `{name, type, status}` |
| `agent_started` | Start agent | `{name, trinity_injection}` |
| `agent_stopped` | Stop agent | `{name}` |
| `agent_deleted` | Delete agent | `{name}` |
| `agent_collaboration` | Agent-to-agent chat | `{source_agent, target_agent}` |
| `agent_activity` | Tool calls, chat | `{agent_name, activity_type}` |

### Testing Real-Time Features

1. Open two browser tabs
2. Trigger action in Tab 1
3. Verify Tab 2 receives WebSocket update
4. Check DevTools for event payload

---

## Docker Testing

Many features interact with Docker containers:

### Useful Commands

```bash
# List Trinity agents
docker ps --filter "label=trinity.platform=agent"

# Check agent container
docker inspect agent-{name} | grep -E '"Status"|"Running"'

# View agent logs
docker logs agent-{name} --tail 50

# Check container labels
docker inspect agent-{name} --format '{{json .Config.Labels}}' | jq

# Execute command in agent
docker exec agent-{name} ls -la /home/developer/
```

### Verifying Container State

```bash
# Agent running?
docker ps | grep agent-{name}

# Agent has Trinity injection?
docker exec agent-{name} ls -la /home/developer/.trinity/

# Agent has planning commands?
docker exec agent-{name} ls -la /home/developer/.claude/commands/trinity/
```

---

## Database Testing

### SQLite (via Backend)

Most database state can be verified via API:

```bash
# Agent ownership
GET /api/agents/{name}  # Returns owner info

# Chat sessions
GET /api/agents/{name}/chat/sessions

# Activities
GET /api/activities/timeline?activity_types=agent_collaboration
```

### Direct SQLite Access (Development Only)

```bash
# Connect to database
sqlite3 ~/trinity-data/trinity.db

# Check agent ownership
SELECT * FROM agent_ownership WHERE agent_name = 'test-agent';

# Check activities
SELECT * FROM agent_activities ORDER BY created_at DESC LIMIT 10;

# Check chat sessions
SELECT * FROM chat_sessions WHERE agent_name = 'test-agent';
```

---

## Status Indicators

**✅ Working** - All tests pass, feature works as documented
**⚠️ Issues Found** - Feature mostly works, but has known issues
**❌ Broken** - Feature doesn't work, needs fixing
**🚧 Not Tested** - Feature implemented but not yet tested
**⏳ Pending** - Testing blocked by dependencies

---

## Quick Reference: Test Coverage by Feature

| Feature | Flow Doc | Last Tested | Status |
|---------|----------|-------------|--------|
| Agent Create/Start/Stop/Delete | agent-lifecycle.md | 2025-12-07 | ✅ |
| Agent Terminal | agent-terminal.md | 2025-12-25 | ✅ |
| Agent Network Dashboard | agent-network.md | 2025-12-07 | ✅ |
| Execution Queue | execution-queue.md | 2025-12-06 | ✅ |
| Activity Stream | activity-stream.md | 2025-12-02 | ✅ |
| Agent Sharing | agent-sharing.md | 2025-11-28 | ✅ |
| Scheduling | scheduling.md | 2025-11-28 | ✅ |
| File Browser | file-browser.md | 2025-12-01 | ✅ |
| Credential Injection | credential-injection.md | 2025-12-01 | ✅ |
| MCP Orchestration | mcp-orchestration.md | 2025-11-27 | ✅ |
| Email Authentication | email-authentication.md | 2025-12-26 | ✅ |
| GitHub Sync | github-sync.md | 2025-11-29 | ✅ |
| Agent Replay Mode | agent-network-replay-mode.md | 2025-12-02 | ✅ |
| Agents Page UI | agents-page-ui-improvements.md | 2025-12-07 | ✅ |
| System Settings | system-wide-trinity-prompt.md | 2025-12-14 | ✅ 19/19 |

---

## Next Steps

1. **Before implementing**: Read the feature flow document
2. **During development**: Use TodoWrite to track test steps
3. **After implementing**: Execute all tests, update timestamps
4. **On issues**: Document in flow, create bug fix task

---

## Automated API Test Suite

Located in `tests/` directory. Run with pytest:

```bash
cd tests
source .venv/bin/activate
python -m pytest -v --tb=short
```

**Latest Results (2025-12-09):**
- **142 tests** collected
- **110 passed** (77.5%)
- **25 skipped** (agent-server direct tests - require running agent)
- **0 failures** ✅

**Test Categories:**
| Category | Tests | Status |
|----------|-------|--------|
| Authentication | 12 | ✅ All pass |
| Agent Lifecycle | 21 | ✅ All pass |
| Agent Chat | 11 | ✅ All pass |
| Agent Files | 8 | ✅ All pass |
| Agent Sharing | 7 | ✅ All pass |
| Credentials | 11 | ✅ All pass |
| Execution Queue | 6 | ✅ All pass |
| MCP Keys | 8 | ✅ All pass |
| Schedules | 9 | ✅ All pass |
| Templates | 7 | ✅ All pass |
| Git Sync | 6 | ✅ All pass |

**Reports:**
- HTML Report: `tests/reports/test-report.html`
- Coverage Report: `tests/reports/coverage/index.html`

---

**Approach**: Manual testing via documented instructions > Automated tests
**Principle**: Load context first, test thoroughly, then mark complete
