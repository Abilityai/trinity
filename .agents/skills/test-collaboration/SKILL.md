---
name: test-collaboration
description: Run a complete test cycle of inter-agent collaboration for a given user. Creates test agents, runs collaboration patterns, and verifies dashboard data.
allowed-tools: [Bash, Read]
user-invocable: true
argument-hint: "[agent_count] [user_email] [--no-cleanup]"
automation: manual
---

# Test Inter-Agent Collaboration

Run a complete test cycle of inter-agent collaboration for a given user.

## State Dependencies

| Source | Location | Read | Write | Description |
|--------|----------|------|-------|-------------|
| Trinity Platform | `http://localhost:8000/api` | ✅ | ✅ | Agent CRUD |
| Database | `~/trinity-data/trinity.db` | ✅ | | Activity verification |
| Docker | Docker socket | ✅ | ✅ | Container management |
| Local Config | `CLAUDE.local.md` | ✅ | | User email default |

## What This Does

1. Creates N test agents owned by the specified user (default: 3, max: 50)
2. Runs a series of collaboration patterns between them
3. Verifies data appears in the collaboration dashboard
4. Optionally cleans up by deleting all test agents

## Usage

```bash
/test-collaboration [agent_count] [user_email] [--no-cleanup]
```

**Parameters**:
- `agent_count` (optional): Number of agents to create (default: 3, max: 50)
- `user_email` (optional): Email of user to own the test agents. Defaults to your email from `CLAUDE.local.md`
- `--no-cleanup` (optional): Skip cleanup step, leave agents running for manual testing

**Examples**:
```bash
/test-collaboration                    # 3 agents, default user, cleanup
/test-collaboration 20                 # 20 agents, default user, cleanup
/test-collaboration 10 user@example.com # 10 agents, specific user
/test-collaboration 20 --no-cleanup    # 20 agents, keep them running
```

## Process

### Step 0: Parse Parameters

Parse the input arguments:
- First numeric argument = `agent_count` (default: 3, clamp to 1-50)
- Email pattern argument = `user_email` (default: from CLAUDE.local.md or user@example.com)
- `--no-cleanup` flag = skip cleanup step

Generate a unique batch ID: `batch_{unix_timestamp}` (e.g., `batch_1701648000`)

### Step 1: Create Test Agents

Create `{agent_count}` test agents using Trinity MCP `mcp__trinity__create_agent`:

**Naming Convention**: `collab-{batch_id}-{index}` where index is zero-padded (01, 02, ... 20)

**For each agent (index 1 to agent_count)**:
```
mcp__trinity__create_agent:
- name: "collab-{batch_id}-{padded_index}"  # e.g., collab-batch_1701648000-01
- type: "business-assistant"
- template: null (default)
```

**Parallelization**: Create agents in batches of 5 to avoid overwhelming Docker:
- Batch 1: agents 01-05
- Batch 2: agents 06-10
- Batch 3: agents 11-15
- Batch 4: agents 16-20
- (Continue as needed)

Wait 5 seconds between batches.

**Track created agents** in a list for later steps.

### Step 2: Wait for Agents to Start

Wait for all agents to be in "running" state:
- Poll `mcp__trinity__list_agents` every 5 seconds
- Filter for agents matching `collab-{batch_id}-*`
- Continue when all show `status: "running"`
- Timeout after 120 seconds (fail if not all running)

### Step 3: Run Collaboration Patterns

Execute collaboration patterns scaled to agent count. Use `mcp__trinity__chat_with_agent` for all interactions.

**Pattern A: Ring Chain** (1 → 2 → 3 → ... → N → 1)
- Each agent sends a message to the next agent in sequence
- Last agent sends back to first, completing the ring
- Message: "Ring chain message from agent {current} to agent {next}, step {step}/{total}"
- Total interactions: `agent_count`

**Pattern B: Hub-Spoke** (Agent-01 as coordinator)
- Agent-01 sends a task to each other agent
- Each agent responds back to Agent-01
- Message (out): "Task assignment from coordinator to agent {target}"
- Message (back): "Task complete, reporting to coordinator"
- Total interactions: `(agent_count - 1) * 2`

**Pattern C: Random Mesh** (if agent_count >= 5)
- Generate `min(agent_count, 10)` random agent pairs (no self-messaging)
- Each pair exchanges one message
- Message: "Random mesh collaboration: {source} connecting with {target}"
- Total interactions: `min(agent_count, 10)`

**Pattern D: Team Clusters** (if agent_count >= 8)
- Divide agents into teams of 4 (or 3 for remainder)
- Within each team, agent[0] coordinates with agent[1,2,3]
- Message: "Team {team_id} coordination: {role} to {target_role}"
- Total interactions: `3 * number_of_teams`

**Execution Strategy**:
- Run patterns sequentially (A, then B, then C, then D)
- Within each pattern, execute interactions with 2-second delays
- Log each interaction: `[Pattern X] Agent {source} → Agent {target}: {status}`

### Step 4: Verify Dashboard Data

Check that collaboration data is persisted:

```bash
# Query the agent_activities table for this batch
sqlite3 ~/trinity-data/trinity.db "
SELECT
  agent_name,
  activity_type,
  json_extract(details, '$.target_agent') as target,
  started_at
FROM agent_activities
WHERE activity_type = 'agent_collaboration'
  AND agent_name LIKE 'collab-{batch_id}-%'
ORDER BY started_at DESC
LIMIT 50;
"

# Count total collaborations for this batch
sqlite3 ~/trinity-data/trinity.db "
SELECT COUNT(*) as total_collaborations
FROM agent_activities
WHERE activity_type = 'agent_collaboration'
  AND agent_name LIKE 'collab-{batch_id}-%';
"
```

**Expected counts by agent_count**:
| Agents | Pattern A | Pattern B | Pattern C | Pattern D | Total |
|--------|-----------|-----------|-----------|-----------|-------|
| 3      | 3         | 4         | 0         | 0         | 7     |
| 5      | 5         | 8         | 5         | 0         | 18    |
| 10     | 10        | 18        | 10        | 6         | 44    |
| 20     | 20        | 38        | 10        | 15        | 83    |

### Step 5: Test Replay Feature

Instruct the user to:
1. Open browser to `http://localhost:3000/collaboration`
2. Observe {agent_count} agent nodes on the canvas
3. Click "Replay" mode toggle
4. Verify timeline shows expected collaboration events (see table above)
5. Set speed to 10x or 20x for large agent counts
6. Click "Play" to replay the sequence
7. Observe edges animating in chronological order
8. For 20 agents, expect replay to show complex interconnection patterns

### Step 6: Cleanup Test Agents

**Skip this step if `--no-cleanup` flag was provided.**

Delete all test agents using Trinity MCP `mcp__trinity__delete_agent`:

**Deletion Strategy** (parallel batches of 5):
```
For each agent in collab-{batch_id}-* list:
  mcp__trinity__delete_agent(name=agent_name)

Delete in reverse order (highest index first) to avoid dependency issues.
Batch deletions in groups of 5, wait 3 seconds between batches.
```

**Verify cleanup**:
```bash
# Should return 0 containers
docker ps -a --filter "name=collab-{batch_id}" --format "{{.Names}}" | wc -l
```

### Step 7: Report Results

Provide a summary report with:
- Batch ID and parameters
- Agent creation results
- Collaboration pattern results
- Database verification
- Performance metrics
- Cleanup status

## Prerequisites

- Trinity platform running (`./scripts/deploy/start.sh`)
- User exists in database with specified email
- Trinity MCP configured and accessible
- **Memory requirements by agent count**:
  | Agents | Min RAM | Recommended |
  |--------|---------|-------------|
  | 3      | 2 GB    | 4 GB        |
  | 10     | 6 GB    | 10 GB       |
  | 20     | 12 GB   | 20 GB       |
  | 50     | 30 GB   | 50 GB       |

## Troubleshooting

**Agent creation fails**:
- Check MCP authentication (API key valid?)
- Check Docker resources: `docker system df` and `free -h`
- Check user exists: `sqlite3 ~/trinity-data/trinity.db "SELECT * FROM users WHERE email='{user_email}'"`
- For large counts, increase Docker memory limit in Docker Desktop settings

**Collaboration not triggering**:
- Verify agents are running: `mcp__trinity__list_agents`
- Check agent MCP configuration (Trinity MCP in .mcp.json?)
- Check backend logs: `docker-compose logs -f backend | grep collaboration`
- For high load, check for rate limiting or timeouts

**No events in replay**:
- Check database: Query agent_activities table (Step 4)
- Verify WebSocket connected (green dot in dashboard)
- Check time range filter (default 24h)
- For many events, increase timeline limit

**Performance issues with many agents**:
- Monitor CPU: `docker stats`
- Check for Docker socket bottleneck
- Consider running on production server for 20+ agents
- Reduce collaboration frequency (increase delays)

## Expected Duration

| Agents | Creation | Collaboration | Cleanup | Total |
|--------|----------|---------------|---------|-------|
| 3      | ~30s     | ~1 min        | ~15s    | ~2 min |
| 10     | ~1.5 min | ~4 min        | ~30s    | ~6 min |
| 20     | ~3 min   | ~10 min       | ~1 min  | ~15 min |
| 50     | ~8 min   | ~30 min       | ~2 min  | ~40 min |

## Completion Checklist

- [ ] Parameters parsed
- [ ] Batch ID generated
- [ ] Test agents created
- [ ] All agents running
- [ ] Pattern A (Ring Chain) executed
- [ ] Pattern B (Hub-Spoke) executed
- [ ] Pattern C (Random Mesh) executed (if applicable)
- [ ] Pattern D (Team Clusters) executed (if applicable)
- [ ] Database verified
- [ ] Replay instructions provided
- [ ] Cleanup completed (unless --no-cleanup)
- [ ] Results report generated
