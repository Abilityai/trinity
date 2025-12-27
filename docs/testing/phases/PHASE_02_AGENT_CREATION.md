# Phase 2: Agent Creation (GitHub Templates)

> **Purpose**: Validate agent creation from GitHub templates for all 8 test agents
> **Duration**: ~30 minutes (agents need time to initialize)
> **Assumes**: Phase 1 PASSED (logged in, clean slate)
> **Output**: 8 agents created from GitHub, all running

---

## Prerequisites

- ✅ Phase 1 PASSED
- ✅ Logged in as admin
- ✅ Dashboard shows "No agents"
- ✅ All 8 GitHub templates available

---

## Overview

You will create 8 agents, one per template:
1. test-echo - Basic terminal commands
2. test-counter - State persistence
3. test-worker - Task delegation target
4. test-delegator - Agent-to-agent communication
5. test-scheduler - Scheduling
6. test-queue - Execution queue
7. test-files - File browser
8. test-error - Error handling

**Timeline**: Each agent takes 30-60 seconds to create and ~10-15 seconds to initialize.

---

## Agent Creation Procedure (Repeat 8 times)

### For Each Agent:

#### Step 1: Via API (Recommended)
**Action**: Create agent via API endpoint

```bash
TOKEN="<from Phase 1>"
AGENT_NAME="test-echo"  # change for each agent
TEMPLATE="github:abilityai/test-agent-echo"  # change for each agent

curl -X POST http://localhost:8000/api/agents \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{
    \"name\": \"$AGENT_NAME\",
    \"template\": \"$TEMPLATE\"
  }"
```

**Expected Response**:
```json
{
  "name": "test-echo",
  "status": "starting",
  "type": "business-assistant",
  "template": "github:abilityai/test-agent-echo",
  "port": 2290
}
```

**Verify**:
- [ ] HTTP 201 (Created) status
- [ ] `name` matches request
- [ ] `template` = `github:abilityai/test-agent-*` (GitHub, not local)
- [ ] `status` = "starting"
- [ ] `port` assigned (2290+)

---

#### Step 2: Verify in UI
**Action**: Navigate to http://localhost:3000/agents

**Expected**:
- [ ] Agent card appears in list
- [ ] Status shows "Starting" (yellow badge)
- [ ] Agent name displayed: "test-echo"
- [ ] "No agents" message gone

**Wait 10-15 seconds** for agent to initialize

**After Wait**:
- [ ] Status transitions to "Running" (green badge)
- [ ] Telemetry appears: CPU, MEM, NET, Uptime

---

#### Step 3: Verify Docker Container
**Action**: Check container created
```bash
docker ps --filter "label=trinity.agent-name=test-echo"
```

**Expected**:
```
CONTAINER ID   IMAGE                    STATUS         PORTS
abc123...      trinity-agent-base:...   Up 20 seconds  0.0.0.0:2290->22/tcp
```

**Verify**:
- [ ] Container exists
- [ ] Status = "Up X seconds"
- [ ] Port mapping correct (2290->22)
- [ ] Image = trinity-agent-base:latest

---

#### Step 4: Verify GitHub Template (CRITICAL)
**Action**: Check agent has git repository

```bash
docker exec agent-test-echo ls -la /home/developer/workspace/.git
```

**Expected**: Directory listing showing `.git/` contents

**Verify**:
- [ ] `.git/` directory exists
- [ ] `HEAD`, `refs/`, `objects/` subdirectories present

**CRITICAL FAILURE**: If no `.git/`, agent was created from local template, not GitHub. **Test FAILS**.

---

#### Step 5: Verify Template in Container Labels
**Action**: Check Docker labels

```bash
docker inspect agent-test-echo --format='{{json .Config.Labels}}' | jq .
```

**Expected Output** (partial):
```json
{
  "trinity.template": "github:abilityai/test-agent-echo",
  "trinity.agent-name": "test-echo",
  "trinity.platform": "agent"
}
```

**Verify**:
- [ ] `trinity.template` = `github:abilityai/test-agent-echo`
- [ ] NOT `local:test-echo` (would be wrong)

**If Shows `local:`**: **Test FAILS** - Must use GitHub templates

---

#### Step 6: Verify Trinity Injection
**Action**: Check meta-prompt was injected

```bash
docker exec agent-test-echo cat /home/developer/.trinity/prompt.md | head -5
```

**Expected**: First few lines of Trinity meta-prompt

```
# Trinity Planning System
...
```

**Verify**:
- [ ] `.trinity/prompt.md` exists
- [ ] File contains planning prompt text

---

#### Step 7: Verify API Metadata
**Action**: Get agent details

```bash
curl http://localhost:8000/api/agents/test-echo \
  -H "Authorization: Bearer $TOKEN"
```

**Expected Response**:
```json
{
  "name": "test-echo",
  "status": "running",
  "type": "business-assistant",
  "template": "github:abilityai/test-agent-echo",
  "owner": "admin",
  "created_at": "2025-12-09T...",
  "port": 2290,
  ...
}
```

**Verify**:
- [ ] `status` = "running"
- [ ] `template` = "github:abilityai/test-agent-echo"
- [ ] `owner` = "admin"

---

#### Step 8: Verify Default Permissions Granted (Req 9.10)
**Action**: Check agent received default permissions for same-owner agents

```bash
curl http://localhost:8000/api/agents/test-echo/permissions \
  -H "Authorization: Bearer $TOKEN" | jq .
```

**Expected Response** (after first agent - minimal):
```json
{
  "source_agent": "test-echo",
  "permitted_agents": [],
  "available_agents": []
}
```

**Expected Response** (after multiple agents created):
```json
{
  "source_agent": "test-echo",
  "permitted_agents": [
    {"name": "test-counter", "status": "running", "permitted": true},
    {"name": "test-delegator", "status": "running", "permitted": true}
  ],
  "available_agents": [...]
}
```

**Verify**:
- [ ] HTTP 200 response
- [ ] `source_agent` matches agent name
- [ ] Other same-owner agents appear in `permitted_agents`
- [ ] Permissions are bidirectional (if test-echo can see test-counter, test-counter can see test-echo)

**Note**: As each new agent is created, it automatically receives bidirectional permissions with all existing same-owner agents.

---

### Repeat Steps 1-8 for Each Agent

Create agents in this order:

| Agent | Template | Notes |
|-------|----------|-------|
| test-echo | github:abilityai/test-agent-echo | Basic chat, no special setup |
| test-counter | github:abilityai/test-agent-counter | Stateful, file I/O |
| test-delegator | github:abilityai/test-agent-delegator | Requires Trinity MCP injection |
| test-worker | github:abilityai/test-agent-worker | Task delegation target |
| test-scheduler | github:abilityai/test-agent-scheduler | Scheduling system |
| test-queue | github:abilityai/test-agent-queue | Concurrency queue |
| test-files | github:abilityai/test-agent-files | File browser |
| test-error | github:abilityai/test-agent-error | Error handling |

---

## Dashboard Verification

Once all 8 agents created:

**Action**: Navigate to http://localhost:3000/ (Dashboard)

**Expected**:
- [ ] 8 agent nodes visible in graph
- [ ] All nodes show green status (running)
- [ ] All show "Idle" state
- [ ] Context shows 0% for each
- [ ] Tasks show "—" for each
- [ ] Stats bar shows: "8 agents · 8 running · 0 messages"
- [ ] No overlapping nodes
- [ ] Mini-map visible in corner

---

## Agents Page Verification

**Action**: Navigate to http://localhost:3000/agents

**Expected**:
- [ ] 8 agent cards in list
- [ ] All show "running" status (green)
- [ ] Each shows correct port (2290-2297)
- [ ] Each shows "Context 0%"
- [ ] Each shows template: "Test: Echo", "Test: Counter", etc.
- [ ] Newest first sorting shows: test-error, ..., test-echo

---

## Critical Validations

### GitHub Templates Only
**Validation**: EVERY agent must have GitHub template

For each agent:
```bash
curl http://localhost:8000/api/agents/$AGENT_NAME \
  -H "Authorization: Bearer $TOKEN" \
  | jq .template
```

**Expected**:
```
"github:abilityai/test-agent-echo"  ✅
"github:abilityai/test-agent-counter"  ✅
...
```

**NEVER**:
```
"local:test-echo"  ❌ FAILS TEST
"local:test-counter"  ❌ FAILS TEST
```

**If any agent shows `local:`**: **PHASE 2 FAILS**

### Docker Labels Verification
```bash
for agent in test-echo test-counter test-delegator test-worker test-scheduler test-queue test-files test-error; do
  echo "=== $agent ==="
  docker inspect agent-$agent --format='{{index .Config.Labels "trinity.template"}}'
done
```

**Expected**: All show `github:abilityai/test-agent-*`

---

## Success Criteria

Phase 2 is **PASSED** when:
- ✅ 8 agents created successfully
- ✅ **ALL agents use GitHub templates** (github:abilityai/test-agent-*)
- ✅ **NO agents use local templates** (would auto-fail)
- ✅ All agents show "running" status
- ✅ All have `.git/` directory
- ✅ All have Trinity meta-prompt injected
- ✅ Dashboard shows all 8 nodes
- ✅ All initialized (green status, CPU/MEM visible)
- ✅ **Default permissions granted** - Each agent has bidirectional permissions with other same-owner agents (Req 9.10)

---

## Troubleshooting

**Agent stuck on "Starting"**:
- Wait 20-30 seconds
- Check logs: `docker logs agent-test-echo | tail -20`
- If persistent, delete and recreate

**Wrong template (local: instead of github:)**:
- Verify template in API response
- If wrong, delete agent and recreate
- Check config.py TEST_AGENT_TEMPLATES

**No .git/ directory**:
- Agent created from wrong template
- Delete and recreate from GitHub template
- Verify GITHUB_CREDENTIAL_ID in config.py

**GitHub clone timeout**:
- Check backend logs: `docker logs trinity-backend | grep -i clone`
- May need GitHub PAT credential configured
- Retry creation

**Port conflict**:
- Verify no other containers using 2290-2297
- Docker automatically assigns next available

---

## Next Phase

Once Phase 2 is **PASSED**, proceed to:
- **Phase 3**: Basic Chat & Context Validation (test-echo)
- **Phase 4**: State Persistence (test-counter)
- ... (other phases in order)

---

**Status**: 🟢 8 GitHub agents created and running
