# Common Workflows

This guide covers the day-to-day operations you'll perform when working with Trinity. These are the tasks you'll do regularly as you manage your agents and systems.

---

## Table of Contents

1. [Daily Operations](#daily-operations)
2. [Agent Management](#agent-management)
3. [Credential Management](#credential-management)
4. [Schedule Management](#schedule-management)
5. [Monitoring & Debugging](#monitoring--debugging)
6. [Multi-Agent Coordination](#multi-agent-coordination)
7. [Backup & Recovery](#backup--recovery)
8. [Performance Optimization](#performance-optimization)

---

## Daily Operations

### Morning Routine: Check Agent Health

**Time**: 2-3 minutes

1. **Open Dashboard**
   ```
   http://localhost:3000
   ```

2. **Visual Health Check**
   - All agents showing green (running)?
   - Any red indicators (stopped/error)?
   - Context bars not at 100%?

3. **Check Recent Activity**
   - Click **Activity** in navigation
   - Filter by last 24 hours
   - Look for error patterns

4. **Review Scheduled Tasks**
   - Did scheduled tasks run successfully?
   - Check execution history for any failures

**Red Flags to Watch For**:
- 🔴 Agent stuck in "starting" state
- 🔴 Schedule showing repeated failures
- 🔴 Context bar at 95%+ (nearing limit)
- 🔴 No activity from an agent that should be active

### Evening Routine: Review Results

**Time**: 5-10 minutes

1. **Check Agent Outputs**
   - Navigate to each agent → Files tab
   - Look for new files in expected locations
   - Review shared-out folders for coordination data

2. **Read Agent Summaries**
   - Many agents create daily summaries
   - Check `workspace/reports/` or similar directories

3. **Plan Tomorrow**
   - Any schedule adjustments needed?
   - Any credentials expiring soon?
   - Any agents that should be paused?

---

## Agent Management

### Creating a New Agent

**Scenario**: You need a new specialized agent.

#### Via Web UI

1. Click **Agents** → **+ Create Agent**

2. Fill in details:
   - **Name**: `my-new-agent` (lowercase, hyphens only)
   - **Display Name**: `My New Agent` (optional)
   - **Template**: Choose from:
     - `local:scout` — Market research analyst (one of the shipped starters)
     - `github:owner/repo` — From GitHub
     - `local:custom-template` — Your custom template

3. Click **Create**

4. Wait for status to show **Running** (~30-60 seconds)

#### Via API

```bash
curl -X POST http://localhost:8000/api/agents \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "my-new-agent",
    "template": "local:default",
    "display_name": "My New Agent"
  }'
```

#### Via MCP (from another agent)

```python
# Inside an agent with Trinity MCP access
mcp__trinity__create_agent(
    name="my-new-agent",
    template="local:default"
)
```

### Editing Agent Instructions

**Scenario**: You need to change how an agent behaves.

1. Go to agent → **Files** tab
2. Navigate to `CLAUDE.md`
3. Click **Edit** (if supported in UI) or:

```bash
# SSH into container
docker exec -it agent-my-agent bash

# Edit CLAUDE.md
nano CLAUDE.md

# Restart agent to apply changes
exit
```

Or via Trinity UI:
- Stop the agent
- Edit configuration (some platforms support live edit)
- Start the agent

**Pro Tip**: Keep agent templates in Git. When you need to update, commit changes and redeploy from template.

### Stopping & Starting Agents

**When to Stop**:
- Agent misbehaving or stuck
- Need to conserve resources
- Applying configuration changes
- Debugging issues

**Via UI**:
1. Go to agent detail page
2. Click **Stop Agent** button
3. Wait for status to change to "Stopped"
4. Click **Start Agent** to restart

**Via API**:
```bash
# Stop
curl -X POST http://localhost:8000/api/agents/my-agent/stop \
  -H "Authorization: Bearer $TOKEN"

# Start
curl -X POST http://localhost:8000/api/agents/my-agent/start \
  -H "Authorization: Bearer $TOKEN"
```

**Via Command Line**:
```bash
# Stop
docker stop agent-my-agent

# Start
docker start agent-my-agent
```

### Deleting an Agent

**Scenario**: Agent no longer needed or needs complete reset.

⚠️ **Warning**: This permanently deletes:
- The Docker container
- All agent files (workspace, memory, etc.)
- Configuration and credentials
- **Cannot be undone**

**Before Deleting**:
1. Backup important files from agent's workspace
2. Remove agent from any schedules or dependencies
3. Update other agents that might reference this one

**Via UI**:
1. Go to agent detail page
2. Click **Delete Agent**
3. Confirm deletion

**Via API**:
```bash
curl -X DELETE http://localhost:8000/api/agents/my-agent \
  -H "Authorization: Bearer $TOKEN"
```

---

## Credential Management

### Adding Credentials to an Agent

**Scenario**: Your agent needs API keys for external services.

#### Step 1: Store Credentials Centrally

1. Go to **Settings** → **Credentials**
2. Click **+ Add Credential**
3. Fill in:
   - **Name**: `TWITTER_API_KEY`
   - **Service**: `twitter`
   - **Type**: `api_key`
   - **Value**: `your-actual-key`
4. Click **Save**

Repeat for all required credentials.

#### Step 2: Link to Agent

Trinity automatically injects credentials that match placeholders in your agent's `.mcp.json.template`.

If agent already exists:
1. Go to agent → **Credentials** tab
2. Click **Reload Credentials**
3. Trinity fetches from central store and updates agent

### Hot-Reloading Credentials

**Scenario**: API key changed, need to update without restarting agent.

#### Option 1: From Central Store

1. Update credential in **Settings** → **Credentials**
2. Go to agent → **Credentials** tab
3. Click **Reload from Store**

#### Option 2: Direct Paste

1. Go to agent → **Credentials** tab
2. Click **Hot-Reload**
3. Paste credentials in `KEY=VALUE` format:
   ```
   TWITTER_API_KEY=new-key-value
   TWITTER_API_SECRET=new-secret-value
   ```
4. Click **Apply**

Agent receives updated credentials **without restarting**.

### Checking Credential Status

**Scenario**: Agent not working, might be credentials issue.

1. Go to agent → **Credentials** tab
2. See which credentials are loaded
3. Check for:
   - ❌ Missing credentials (red warning)
   - ⚠️ Expired credentials
   - ✅ Valid credentials

**Via API**:
```bash
curl http://localhost:8000/api/agents/my-agent/credentials/status \
  -H "Authorization: Bearer $TOKEN"
```

---

## Schedule Management

### Creating a Schedule

**Scenario**: You want an agent to run a task automatically.

1. Go to agent → **Schedules** tab
2. Click **+ Create Schedule**
3. Configure:
   - **Name**: Descriptive name (e.g., "Daily Report")
   - **Cron Expression**: When to run (e.g., `0 9 * * *`)
   - **Message**: What to tell the agent
   - **Timezone**: Your timezone
   - **Enabled**: ✅

4. Click **Create**

#### Cron Expression Examples

```bash
# Every hour at :00
0 * * * *

# Every day at 9 AM
0 9 * * *

# Every Monday at 9 AM
0 9 * * 1

# Every 15 minutes
*/15 * * * *

# Weekdays at 9 AM and 5 PM
0 9,17 * * 1-5

# First day of month at midnight
0 0 1 * *
```

**Pro Tip**: Use [crontab.guru](https://crontab.guru/) to build and validate cron expressions.

### Testing a Schedule

**Before enabling**, test manually:

1. Create schedule but leave **Enabled** unchecked
2. Click **Trigger Now** button
3. Watch agent → **Activity** tab
4. Verify expected behavior
5. If good, enable the schedule

### Disabling a Schedule

**Scenario**: Temporarily pause automation without deleting schedule.

1. Go to agent → **Schedules** tab
2. Find the schedule
3. Toggle **Enabled** to off
4. Schedule won't run but configuration is saved

Re-enable anytime by toggling back on.

### Viewing Execution History

**Scenario**: Check if scheduled tasks ran successfully.

1. Go to agent → **Schedules** tab
2. Click on a schedule
3. View **Execution History**:
   - Timestamp of each run
   - Success/failure status
   - Duration
   - Logs/errors

### Modifying Schedule Timing

**Scenario**: Need to change when a schedule runs.

1. Go to agent → **Schedules** tab
2. Click **Edit** on the schedule
3. Update cron expression
4. Save changes

New timing takes effect immediately.

---

## Monitoring & Debugging

### Viewing Agent Logs

**Scenario**: Agent not behaving as expected, need to see what's happening.

#### Via Web UI

1. Go to agent → **Logs** tab
2. See real-time container logs
3. Use filters:
   - Error only
   - Last N lines
   - Search for keywords

#### Via Command Line

```bash
# View logs (live tail)
docker logs -f agent-my-agent

# Last 100 lines
docker logs --tail 100 agent-my-agent

# Since specific time
docker logs --since 30m agent-my-agent

# Search logs
docker logs agent-my-agent 2>&1 | grep ERROR
```

### Checking Agent Activity

**Scenario**: See what tools an agent is using and when.

1. Go to agent → **Activity** tab
2. See chronological list of:
   - Tool calls (MCP, functions, etc.)
   - Chat messages
   - File operations
   - Schedule executions

3. Filter by:
   - Time range
   - Activity type
   - Success/error status

### Monitoring Context Usage

**Scenario**: Agent getting close to context limit.

1. **Dashboard View**:
   - See context bars below each agent
   - Colors: Green → Yellow → Orange → Red
   - Percentage shown

2. **Agent Detail View**:
   - Go to agent detail page
   - Top bar shows context usage
   - "Reset Context" button if needed

**When to Reset Context**:
- Context > 90% (agent might lose track)
- Agent giving confused/contradictory responses
- After completing a major workflow

**How to Reset**:
```bash
# Via Trinity API
curl -X POST http://localhost:8000/api/agents/my-agent/reset-context \
  -H "Authorization: Bearer $TOKEN"
```

### Checking File System

**Scenario**: Verify agent created expected files.

1. Go to agent → **Files** tab
2. Browse directory structure
3. Click files to view content
4. Download files if needed

**Via Command Line**:
```bash
# List files
docker exec agent-my-agent ls -la /home/developer/workspace

# Read file
docker exec agent-my-agent cat /home/developer/workspace/report.txt

# Copy file out
docker cp agent-my-agent:/home/developer/workspace/report.txt ./local-report.txt
```

### Debugging Agent Communication

**Scenario**: Multi-agent system, agents not communicating correctly.

1. **Check Permissions**:
   - Go to agent → **Permissions** tab
   - Verify target agent is listed
   - Grant permission if missing

2. **Check Shared Folders**:
   - Go to agent → **Shared Folders** tab
   - Verify "expose" and "consume" are enabled
   - Check which agents are mounted

3. **Verify Folder Contents**:
   ```bash
   # Check what agent is sharing
   docker exec agent-source ls /home/developer/shared-out

   # Check what agent can see
   docker exec agent-consumer ls /home/developer/shared-in/source-agent
   ```

4. **Check Activity Timeline**:
   - Dashboard → Activity
   - Filter: "Agent Collaboration"
   - See all inter-agent messages

---

## Multi-Agent Coordination

### Setting Up Agent Permissions

**Scenario**: Two agents need to communicate.

1. Go to source agent → **Permissions** tab
2. Click **+ Grant Permission**
3. Select target agent
4. Click **Grant**

Repeat in reverse if bidirectional communication needed.

**Via API (full mesh for system)**:
```bash
# Grant all agents permission to talk to each other
AGENTS=("agent-a" "agent-b" "agent-c")

for source in "${AGENTS[@]}"; do
  for target in "${AGENTS[@]}"; do
    if [ "$source" != "$target" ]; then
      curl -X POST "http://localhost:8000/api/agents/$source/permissions" \
        -H "Authorization: Bearer $TOKEN" \
        -d "{\"target_agent\": \"$target\"}"
    fi
  done
done
```

### Configuring Shared Folders

**Scenario**: Agents need to share files.

1. Go to agent → **Shared Folders** tab
2. Enable options:
   - **Expose** ✅: Other agents can read your files
   - **Consume** ✅: You can read other agents' files
3. Click **Save**
4. **Restart agent** (required for folder mounts to apply)

**Folder Layout**:
```
/home/developer/
├── shared-out/           # This agent's output (others read)
└── shared-in/            # Other agents' output (you read)
    ├── agent-a/          # Agent A's shared-out
    └── agent-b/          # Agent B's shared-out
```

### Coordinating Schedule Timing

**Scenario**: Multiple agents need to run in sequence.

**Pattern: Leader-Follower**
```
Orchestrator: 0 * * * *      # :00 - Plan work
Worker A:     5 * * * *      # :05 - Execute work
Worker B:     10 * * * *     # :10 - Process results
Orchestrator: 15 * * * *     # :15 - Verify completion
```

**Pattern: Staggered Workers**
```
Worker A:     0,15,30,45 * * * *    # Every 15 min starting :00
Worker B:     5,20,35,50 * * * *    # Every 15 min starting :05
Worker C:     10,25,40,55 * * * *   # Every 15 min starting :10
```

---

## Backup & Recovery

### Backing Up Agent Workspace

**Scenario**: Want to save agent's work before major change.

```bash
# Backup entire workspace
docker exec agent-my-agent tar czf /tmp/backup.tar.gz /home/developer/workspace
docker cp agent-my-agent:/tmp/backup.tar.gz ./backups/agent-my-agent-$(date +%Y%m%d).tar.gz

# Backup specific files
docker cp agent-my-agent:/home/developer/workspace/important-data.json ./backups/
```

### Backing Up Vector Memory

**Scenario**: Preserve agent's learned knowledge.

```bash
# Backup Chroma database
docker exec agent-my-agent tar czf /tmp/vector-backup.tar.gz /home/developer/vector-store
docker cp agent-my-agent:/tmp/vector-backup.tar.gz ./backups/vector-my-agent-$(date +%Y%m%d).tar.gz
```

### Restoring from Backup

**Scenario**: Need to restore agent to previous state.

```bash
# Copy backup into container
docker cp ./backups/agent-my-agent-20251223.tar.gz agent-my-agent:/tmp/

# Extract
docker exec agent-my-agent tar xzf /tmp/agent-my-agent-20251223.tar.gz -C /
```

### Exporting Agent Configuration

**Scenario**: Want to recreate agent or move to another Trinity instance.

1. Export agent template files from GitHub repo
2. Export system manifest (if part of multi-agent system):
   ```bash
   curl http://localhost:8000/api/systems/my-system/manifest \
     -H "Authorization: Bearer $TOKEN" > my-system.yaml
   ```
3. Document credentials needed (don't export actual secrets!)

---

## Performance Optimization

### Checking Resource Usage

**Scenario**: System feels slow, check what's consuming resources.

```bash
# See all containers and resource usage
docker stats

# See specific agent
docker stats agent-my-agent
```

Look for:
- High CPU% (agent is working hard or stuck)
- High MEM% (close to limit, might OOM)
- High NET I/O (lots of API calls)

### Adjusting Agent Resources

**Scenario**: Agent needs more memory or CPU.

Currently requires recreating agent with new resource limits:

1. Note current agent configuration
2. Delete agent
3. Create new agent with higher resources in `template.yaml`:
   ```yaml
   resources:
     cpu: "4"      # Was "2"
     memory: "8g"  # Was "4g"
   ```

### Reducing Context Usage

**Strategies to keep context low**:

1. **Use Vector Memory**: Store long-term knowledge outside context
   ```python
   # Instead of keeping in context
   # Store in Chroma
   mcp__chroma__add_documents(
       collection="knowledge",
       documents=["Important info"],
       ids=["doc1"]
   )
   ```

2. **Use Files**: Store data in files, not in chat
   ```python
   # Write to file
   with open('workspace/data.json', 'w') as f:
       json.dump(large_data, f)
   # Reference file instead of keeping in context
   ```

3. **Reset Context Periodically**: After major workflows
   - Manually via UI
   - Agent can request reset
   - Schedule context resets

4. **Use Planning System**: Persist tasks outside context
   ```bash
   /trinity-plan-create
   ```

### Reducing API Costs

**Strategies to minimize token usage**:

1. **Batch Operations**: Process multiple items in one message
2. **Use Smaller Models**: For simple tasks, use faster/cheaper models
3. **Cache Results**: Store frequent queries in vector memory
4. **Filter Input**: Only process what's necessary
5. **Monitor Usage**: Check OpenTelemetry metrics

---

## Quick Reference

### Most Common Commands

```bash
# View all agents
docker ps | grep agent-

# View agent logs
docker logs -f agent-NAME

# Restart agent
docker restart agent-NAME

# Stop Trinity
docker compose down

# Start Trinity
docker compose up -d

# View Trinity logs
docker compose logs -f backend
docker compose logs -f frontend

# Check disk usage
docker system df
```

### Most Common Issues & Fixes

| Issue | Quick Fix |
|-------|-----------|
| Agent stuck starting | Check logs: `docker logs agent-NAME` |
| Agent not responding | Restart: `docker restart agent-NAME` |
| Credentials not working | Reload: Agent → Credentials → Reload |
| Shared folder empty | Check: Permissions granted & agent restarted |
| Schedule not running | Check: Enabled ✅ & cron syntax valid |
| Context at 100% | Reset: Agent → Reset Context button |
| High memory usage | Restart agent, check for memory leaks |

---

## Next Steps

- **Need Help?** Check [Troubleshooting Guide](04-troubleshooting.md)
- **Building Systems?** Read [Multi-Agent System Guide](../MULTI_AGENT_SYSTEM_GUIDE.md)
- **Creating Templates?** See [Trinity Compatible Agent Guide](../TRINITY_COMPATIBLE_AGENT_GUIDE.md)

---

**These workflows will become second nature quickly. Happy building!** 🚀





