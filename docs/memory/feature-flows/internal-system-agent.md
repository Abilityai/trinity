# Feature Flow: Internal System Agent

> **Phase 11.1 & 11.2** - Platform operations manager with privileged access
>
> **Updated 2025-12-21**: Added Cost Monitoring data flow, 5-step reinitialize process, OTel access fix documentation

## Overview

The Internal System Agent (`trinity-system`) is an auto-deployed, deletion-protected agent that serves as the **platform operations manager**. It focuses exclusively on infrastructure concerns: agent health, lifecycle management, resource governance, and schedule control.

> **Guiding Principle**: "The system agent manages the orchestra, not the music."

## Operational Scope

### In Scope (Operations)

| Area | Responsibilities |
|------|------------------|
| **Health Monitoring** | Detect stuck agents, high context usage, container failures |
| **Lifecycle Management** | Start, stop, restart agents based on health |
| **Resource Governance** | Monitor cost, context thresholds, memory bounds |
| **Schedule Control** | Enable/disable schedules, trigger manual runs, pause automation |
| **Validation** | Pre-flight checks before agent operations |
| **Alerting** | Notify on anomalies, failures, threshold breaches |
| **Cleanup** | Reset stuck sessions, clear stale context |
| **Reporting** | Fleet status, cost summaries, health reports |

### Out of Scope (Not Ops)

| NOT Responsible For | Why |
|---------------------|-----|
| Task orchestration | Agents coordinate themselves via MCP |
| Content/output review | Domain-specific, not infrastructure |
| Workflow design | User/developer responsibility |
| Agent-to-agent messaging | Agents handle directly |

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                     Trinity Platform Startup                        │
├─────────────────────────────────────────────────────────────────────┤
│  1. Backend starts                                                  │
│  2. Database migrations run (is_system column)                      │
│  3. system_agent_service.ensure_deployed() called                   │
│  4. If trinity-system doesn't exist → create from template          │
│  5. If exists but stopped → start it                                │
│  6. If running → no action                                          │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│                    System Agent Container                            │
├─────────────────────────────────────────────────────────────────────┤
│  Name: agent-trinity-system                                         │
│  Template: local:trinity-system                                      │
│  Resources: 4 CPU, 8GB RAM                                          │
│  MCP Key: scope="system" (bypasses all permissions)                 │
│  Labels: trinity.is-system=true                                      │
│                                                                      │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  CLAUDE.md                                                    │   │
│  │  - Operations-only scope                                      │   │
│  │  - Available MCP tools                                        │   │
│  │  - Health monitoring guidelines                               │   │
│  │  - Alerting and reporting                                     │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                                                                      │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  Slash Commands (/ops/*)                                      │   │
│  │  - /ops/status - Fleet status report                         │   │
│  │  - /ops/health - Health check all agents                     │   │
│  │  - /ops/restart <agent> - Restart specific agent             │   │
│  │  - /ops/restart-all - Restart entire fleet                   │   │
│  │  - /ops/stop <agent> - Stop specific agent                   │   │
│  │  - /ops/schedules - Schedule overview                        │   │
│  │  - /ops/costs - Cost report from OTel                        │   │
│  └──────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
```

## Key Files

| File | Purpose |
|------|---------|
| `config/agent-templates/trinity-system/template.yaml` | Template configuration |
| `config/agent-templates/trinity-system/CLAUDE.md` | Operations instructions |
| `config/agent-templates/trinity-system/.claude/commands/ops/*.md` | Slash commands |
| `src/backend/services/system_agent_service.py` | Auto-deployment logic |
| `src/backend/routers/system_agent.py` | Admin management endpoints |
| `src/backend/routers/ops.py` | Fleet operations endpoints |
| `src/backend/routers/settings.py` | Ops settings (OPS_SETTINGS_DEFAULTS) |
| `src/backend/db/agents.py` | SYSTEM_AGENT_NAME constant, is_system checks |
| `src/frontend/src/views/SystemAgent.vue` | **System Agent UI** - dedicated ops interface |
| `src/frontend/src/components/NavBar.vue` | System link in navbar (admin-only) |
| `src/frontend/src/router/index.js` | `/system-agent` route |

## Frontend UI

### Route
- **Path**: `/system-agent`
- **Access**: Admin-only (requires `role: "admin"`)
- **NavBar**: Purple "System" link with CPU icon (visible to admins)

### Components

The `SystemAgent.vue` view provides a dedicated, ops-focused interface:

```
┌─────────────────────────────────────────────────────────────────────┐
│                    System Agent Header                               │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │  [CPU Icon]  System Agent                    [running] [Restart]│  │
│  │              Platform Operations Manager                        │  │
│  └───────────────────────────────────────────────────────────────┘  │
│                                                                      │
│  ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐                       │
│  │ Total  │ │Running │ │Stopped │ │ Issues │  <- Fleet Overview    │
│  │   12   │ │   8    │ │   4    │ │   1    │                       │
│  └────────┘ └────────┘ └────────┘ └────────┘                       │
│                                                                      │
│  ┌──────────────────┐  ┌────────────────────────────────────────┐  │
│  │  Quick Actions   │  │     Operations Console                  │  │
│  │                  │  │  ┌──────────────────────────────────┐   │  │
│  │ [Emergency Stop] │  │  │ [/ops/status] [/ops/health] [...] │   │  │
│  │ [Restart All]    │  │  └──────────────────────────────────┘   │  │
│  │ [Pause Scheds]   │  │                                         │  │
│  │ [Resume Scheds]  │  │  Chat messages appear here...           │  │
│  │ [Refresh Status] │  │                                         │  │
│  │                  │  │  [Enter command...              ] [Send]│  │
│  └──────────────────┘  └────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
```

### Features

| Feature | Description |
|---------|-------------|
| **Clean Header** | White/gray header with SYSTEM badge, consistent with rest of platform |
| **Fleet Overview Cards** | Total, Running, Stopped, Issues counts |
| **Quick Actions** | Emergency Stop, Restart All, Pause/Resume Schedules (muted styling) |
| **Operations Console** | Chat interface for ops commands |
| **Quick Command Buttons** | One-click `/ops/status`, `/ops/health`, `/ops/schedules` |
| **Auto-refresh** | Polls status every 10 seconds |

### Design Decisions

1. **Simplified Layout**: Unlike regular `AgentDetail.vue` with many tabs, this is a single-page ops dashboard
2. **Consistent Styling**: Uses same muted color palette as rest of platform (no aggressive colors)
3. **Quick Actions**: One-click buttons for common operations (no need to type commands)
4. **Fleet Focus**: Shows aggregate stats, not individual agent details
5. **Admin-Only**: Protected route - only admins can access
6. **Blue Theme**: Chat bubbles and send button use blue (matching system color scheme)

## API Endpoints

### System Agent Management

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/system-agent/status` | GET | Get system agent status |
| `/api/system-agent/restart` | POST | Restart system agent (admin) |
| `/api/system-agent/reinitialize` | POST | Reset to clean state (admin) |

### Fleet Operations

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/ops/fleet/status` | GET | All agents with status, context, activity |
| `/api/ops/fleet/health` | GET | Health summary with critical/warning issues |
| `/api/ops/fleet/restart` | POST | Restart all/filtered agents |
| `/api/ops/fleet/stop` | POST | Stop all/filtered agents |

### Schedule Control

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/ops/schedules/pause` | POST | Pause all schedules |
| `/api/ops/schedules/resume` | POST | Resume all schedules |
| `/api/ops/emergency-stop` | POST | Halt all executions immediately |

### Cost & Observability (OTel Integration)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/ops/costs` | GET | Cost report with OTel metrics, alerts, and thresholds |

The `/api/ops/costs` endpoint provides an ops-focused view of OpenTelemetry metrics:

**Response Structure**:
```json
{
  "enabled": true,
  "available": true,
  "timestamp": "2025-12-20T...",
  "summary": {
    "total_cost": 0.0234,
    "total_tokens": 48093,
    "daily_limit": 50.0,
    "cost_percent_of_limit": 0.05
  },
  "alerts": [
    {
      "severity": "warning",
      "type": "cost_limit_approaching",
      "message": "...",
      "recommendation": "..."
    }
  ],
  "cost_by_model": [...],
  "tokens_by_type": {...},
  "productivity": {
    "sessions": 5,
    "active_time_formatted": "1h",
    "commits": 3,
    "pull_requests": 1,
    "lines_added": 42,
    "lines_removed": 15
  }
}
```

**Alert Types**:
- `cost_limit_exceeded` (critical) - Daily cost >= limit
- `cost_limit_approaching` (warning) - Daily cost >= 80% of limit

**Integration with OTel**:
- Fetches raw metrics from OTel Collector's Prometheus endpoint (`:8889/metrics`)
- Reuses parsing from `observability.py` (decoupled architecture)
- Adds ops-specific analysis: threshold checks, alerts, formatted output
- System agent interprets the data via `/ops/costs` slash command

### Ops Settings

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/settings/ops/config` | GET | Get all ops settings with defaults |
| `/api/settings/ops/config` | PUT | Update multiple ops settings |
| `/api/settings/ops/reset` | POST | Reset all ops settings to defaults |

## Ops Settings

Stored in `system_settings` table with these defaults:

| Key | Default | Description |
|-----|---------|-------------|
| `ops_context_warning_threshold` | 75 | Context % to trigger warning |
| `ops_context_critical_threshold` | 90 | Context % to trigger critical |
| `ops_idle_timeout_minutes` | 30 | Minutes before stuck detection |
| `ops_cost_limit_daily_usd` | 50.0 | Daily cost limit (0 = unlimited) |
| `ops_max_execution_minutes` | 10 | Max chat execution time |
| `ops_alert_suppression_minutes` | 15 | Suppress duplicate alerts |
| `ops_log_retention_days` | 7 | Days to keep container logs |
| `ops_health_check_interval` | 60 | Seconds between health checks |

## MCP Scope Types

| Scope | Description | Permission Check |
|-------|-------------|------------------|
| `user` | Human user via Claude Code client | Owner/admin/shared |
| `agent` | Regular agent | Explicit permission list |
| `system` | System agent | **Bypasses all checks** |

## Slash Commands

Commands are located in `config/agent-templates/trinity-system/.claude/commands/ops/`:

| Command | File | Description |
|---------|------|-------------|
| `/ops/status` | `status.md` | Fleet status with context usage |
| `/ops/health` | `health.md` | Health check with issues and recommendations |
| `/ops/restart <agent>` | `restart.md` | Restart specific agent |
| `/ops/restart-all` | `restart-all.md` | Restart entire fleet |
| `/ops/stop <agent>` | `stop.md` | Stop specific agent |
| `/ops/schedules` | `schedules.md` | Schedule overview with next runs |
| `/ops/costs` | `costs.md` | Cost report from OTel metrics |

## Data Flow

### 1. Auto-Deployment on Startup

```
Backend Startup
       │
       ▼
┌──────────────────┐
│ lifespan()       │
│ handler          │
└────────┬─────────┘
         │
         ▼
┌──────────────────────────────────┐
│ system_agent_service.            │
│ ensure_deployed()                │
└────────┬─────────────────────────┘
         │
         ▼
┌──────────────────┐    Yes    ┌──────────────────┐
│ Container        ├──────────►│ Already running? │
│ exists?          │           │ → No action      │
└────────┬─────────┘           └──────────────────┘
         │ No
         ▼
┌──────────────────────────────────┐
│ _create_system_agent()           │
│ 1. Load template                 │
│ 2. Create MCP key (scope=system) │
│ 3. Start container with mounts:  │
│    - /template (ro)              │
│    - /trinity-meta-prompt (ro)   │
│ 4. Register ownership (is_system)│
└──────────────────────────────────┘
```

### 2. Fleet Health Check Flow

```
User: /ops/health
       │
       ▼
┌──────────────────────────────────┐
│ System Agent receives command    │
│ Reads health.md instructions     │
└────────┬─────────────────────────┘
         │
         ▼
┌──────────────────────────────────┐
│ mcp__trinity__list_agents        │
│ Get all agents                   │
└────────┬─────────────────────────┘
         │
         ▼
┌──────────────────────────────────┐
│ For each running agent:          │
│ - Check container status         │
│ - Check context usage            │
│ - Check last activity            │
└────────┬─────────────────────────┘
         │
         ▼
┌──────────────────────────────────┐
│ Classify health:                 │
│ - Critical: >90% context, down   │
│ - Warning: >75% context, idle    │
│ - Healthy: Normal operation      │
└────────┬─────────────────────────┘
         │
         ▼
┌──────────────────────────────────┐
│ Generate health report           │
│ with recommendations             │
└──────────────────────────────────┘
```

### 3. Emergency Stop Flow

```
POST /api/ops/emergency-stop
       │
       ▼
┌──────────────────────────────────┐
│ 1. Pause all enabled schedules   │
│    - Set enabled=false in DB     │
│    - Remove from APScheduler     │
└────────┬─────────────────────────┘
         │
         ▼
┌──────────────────────────────────┐
│ 2. Stop all non-system agents    │
│    - Skip trinity-system         │
│    - container.stop(timeout=10)  │
└────────┬─────────────────────────┘
         │
         ▼
┌──────────────────────────────────┐
│ 3. Audit log with results        │
│    - schedules_paused: N         │
│    - agents_stopped: N           │
│    - errors: [...]               │
└──────────────────────────────────┘
```

### 4. Cost Monitoring Flow (OTel Access)

The system agent accesses OpenTelemetry metrics via the REST API using its MCP API key:

```
User: /ops/costs (or "show me platform costs")
       │
       ▼
┌──────────────────────────────────┐
│ System Agent reads costs.md      │
│ (slash command instructions)     │
└────────┬─────────────────────────┘
         │
         ▼
┌──────────────────────────────────────────────────────────┐
│ Execute Bash curl command:                               │
│ curl -s http://backend:8000/api/ops/costs \              │
│   -H "Authorization: Bearer $TRINITY_MCP_API_KEY"        │
└────────┬─────────────────────────────────────────────────┘
         │
         ▼
┌──────────────────────────────────┐
│ Backend /api/ops/costs endpoint  │
│ 1. Validate MCP API key (system) │
│ 2. Check OTEL_ENABLED env var    │
│ 3. Fetch from OTel Collector     │
│    (http://collector:8889/metrics)│
│ 4. Parse Prometheus format       │
│ 5. Add alerts & thresholds       │
│ 6. Return JSON response          │
└────────┬─────────────────────────┘
         │
         ▼
┌──────────────────────────────────┐
│ System Agent parses response:    │
│ - enabled: true/false            │
│ - available: true/false          │
│ - summary: cost, tokens, limit   │
│ - alerts: warnings/critical      │
│ - cost_by_model: breakdown       │
│ - productivity: commits, PRs     │
└────────┬─────────────────────────┘
         │
         ▼
┌──────────────────────────────────┐
│ Generate formatted report        │
│ based on response state          │
└──────────────────────────────────┘
```

**Key Implementation Details (2025-12-21 Fix):**

| Issue | Root Cause | Fix |
|-------|------------|-----|
| Agent couldn't access OTel | Slash command used `$TRINITY_API_KEY` but agent has `$TRINITY_MCP_API_KEY` | Updated `costs.md` to use correct env var |
| Agent said "I don't have access" | No explicit documentation in CLAUDE.md | Added Cost Monitoring section with curl example |
| MCP key works for REST API | `dependencies.py` already supports MCP key auth | No code change needed (discovery) |

**Environment Variable:**
- `$TRINITY_MCP_API_KEY` - Auto-injected when system agent is created
- Used for both MCP tool calls AND REST API authentication
- System scope bypasses permission checks

**Files Modified:**
- `config/agent-templates/trinity-system/.claude/commands/ops/costs.md:11` - Fixed env var
- `config/agent-templates/trinity-system/CLAUDE.md:128-147` - Added Cost Monitoring section

### 5. Reinitialize Flow (5-Step Process)

The reinitialize endpoint resets the system agent to a clean state without losing database records:

```
POST /api/system-agent/reinitialize (admin-only)
       │
       ▼
┌──────────────────────────────────┐
│ Step 1: STOPPED                  │
│ - container.stop(timeout=30)     │
│ - Wait for clean shutdown        │
└────────┬─────────────────────────┘
         │
         ▼
┌──────────────────────────────────────────────────────────┐
│ Step 2: WORKSPACE_CLEARED                                │
│ - container.start()                                      │
│ - exec: rm -rf /home/developer/workspace/*               │
│         /home/developer/.claude /home/developer/.trinity │
└────────┬─────────────────────────────────────────────────┘
         │
         ▼
┌──────────────────────────────────┐
│ Step 3: STARTED                  │
│ - Container already running      │
│   from step 2                    │
└────────┬─────────────────────────┘
         │
         ▼
┌──────────────────────────────────────────────────────────┐
│ Step 4: TEMPLATE_COPIED (2025-12-21 Fix)                 │
│ - exec: cp -r /template/.claude /home/developer/         │
│ - exec: cp /template/CLAUDE.md /home/developer/          │
│ - Restores slash commands deleted in step 2              │
└────────┬─────────────────────────────────────────────────┘
         │
         ▼
┌──────────────────────────────────┐
│ Step 5: TRINITY_INJECTED         │
│ - inject_trinity_meta_prompt()   │
│ - Copies /trinity-meta-prompt/*  │
│   to agent workspace             │
└────────┬─────────────────────────┘
         │
         ▼
┌──────────────────────────────────┐
│ Return result:                   │
│ - success: true                  │
│ - steps_completed: [...]         │
│ - warnings: [any errors]         │
└──────────────────────────────────┘
```

**Why Step 4 Was Added (2025-12-21):**

The previous reinitialize flow had 4 steps but was missing template re-copy:
1. Step 2 deleted `/home/developer/.claude` (including `/ops/` slash commands)
2. Step 5 only injected Trinity meta-prompt (not template content)
3. Result: System agent lost its `/ops/` slash commands after reinitialize

Fix: Added explicit template copy step between workspace clear and Trinity injection.

**File:** `src/backend/routers/system_agent.py:163-174`

```python
# Step 4: Re-copy template files (.claude and CLAUDE.md)
copy_result = container.exec_run(
    "bash -c 'cp -r /template/.claude /home/developer/ 2>/dev/null; "
    "cp /template/CLAUDE.md /home/developer/ 2>/dev/null; true'",
    user="developer"
)
```

## Testing

### 1. Auto-Deployment
```bash
# Restart backend
docker-compose restart backend

# Check logs for system agent deployment
docker-compose logs backend | grep "System agent"
# Expected: "System agent: created - System agent created and started"
```

### 2. Deletion Protection
```bash
# Try to delete via API
curl -X DELETE http://localhost:8000/api/agents/trinity-system \
  -H "Authorization: Bearer $TOKEN"

# Expected: 403 with message about system agents
```

### 3. Fleet Status API
```bash
curl http://localhost:8000/api/ops/fleet/status \
  -H "Authorization: Bearer $TOKEN"

# Expected: List of all agents with status, context, activity
```

### 4. Health Check API
```bash
curl http://localhost:8000/api/ops/fleet/health \
  -H "Authorization: Bearer $TOKEN"

# Expected: Health summary with critical_issues, warnings, healthy_count
```

### 5. Emergency Stop
```bash
curl -X POST http://localhost:8000/api/ops/emergency-stop \
  -H "Authorization: Bearer $TOKEN"

# Expected: All schedules paused, all non-system agents stopped
```

### 6. Ops Commands (via Chat)
```bash
# Connect to system agent and run:
# /ops/status
# /ops/health
# /ops/restart <agent-name>
```

### 7. Ops Settings
```bash
# Get current settings
curl http://localhost:8000/api/settings/ops/config \
  -H "Authorization: Bearer $TOKEN"

# Update settings
curl -X PUT http://localhost:8000/api/settings/ops/config \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"settings": {"ops_context_warning_threshold": "80"}}'
```

## Troubleshooting

### System Agent Not Deploying
1. Check admin user exists in database
2. Verify template exists at `config/agent-templates/trinity-system/`
3. Check backend logs for deployment errors
4. Verify Docker is accessible

### System Agent Can't Access Other Agents
1. Verify MCP key was created with `scope: "system"`
2. Check MCP server logs for authentication
3. Verify key is passed correctly to agent container

### Re-initialization Fails
1. Verify user is admin
2. Check container is accessible
3. Review workspace cleanup errors in response

### Ops Commands Not Working
1. Verify slash commands exist at `config/agent-templates/trinity-system/.claude/commands/ops/`
2. Check agent has been re-initialized after template update
3. Verify agent can access MCP tools

### System Agent Can't Access OTel Metrics (2025-12-21 Fix)

**Symptom**: Agent responds "I don't have direct access to view OpenTelemetry metrics" when asked about costs.

**Root Causes and Fixes**:

1. **Wrong environment variable in slash command**
   - Problem: `costs.md` used `$TRINITY_API_KEY`
   - Fix: Change to `$TRINITY_MCP_API_KEY` (what the agent actually has)
   - File: `config/agent-templates/trinity-system/.claude/commands/ops/costs.md`

2. **Missing /trinity-meta-prompt mount on creation**
   - Problem: System agent created without Trinity meta-prompt mount
   - Fix: Added mount in `_create_system_agent()` (line 216-220)
   - File: `src/backend/services/system_agent_service.py`

3. **Reinitialize deleted slash commands without restoring them**
   - Problem: Step 2 deleted `.claude/` dir, step 5 only injected Trinity prompt
   - Fix: Added step 4 to re-copy template files before Trinity injection
   - File: `src/backend/routers/system_agent.py`

4. **No explicit documentation in CLAUDE.md**
   - Problem: Agent didn't know it could call REST API with its MCP key
   - Fix: Added "Cost Monitoring" section with curl example
   - File: `config/agent-templates/trinity-system/CLAUDE.md`

**Verification**:
```bash
# SSH into system agent and verify environment
echo $TRINITY_MCP_API_KEY  # Should show the key

# Test the API call directly
curl -s http://backend:8000/api/ops/costs \
  -H "Authorization: Bearer $TRINITY_MCP_API_KEY"
```

## Related Documents

- `docs/drafts/SYSTEM_AGENT_OPS_REQUIREMENTS.md` - Full ops requirements
- `docs/memory/requirements.md` - Requirements 11.1 and 11.2
- `docs/memory/feature-flows/agent-lifecycle.md` - Agent lifecycle operations
