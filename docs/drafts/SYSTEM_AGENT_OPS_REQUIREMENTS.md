# System Agent: Operational Scope Requirements

> **Status**: Draft
> **Created**: 2025-12-20
> **Requirement**: 11.1 Enhancement

## Overview

The Trinity System Agent (`trinity-system`) is a **platform operations agent** focused exclusively on infrastructure management. It does NOT participate in agent workflows or business logic - it manages the health, lifecycle, and operational aspects of the agent fleet.

## Guiding Principle

> **The system agent manages the orchestra, not the music.**

It ensures agents are running, healthy, and within resource bounds. It does not care what the agents are doing - only that they can do it effectively.

---

## Scope Definition

### In Scope (Operational Concerns)

| Area | Responsibilities |
|------|------------------|
| **Health Monitoring** | Detect stuck agents, high context usage, container failures |
| **Lifecycle Management** | Start, stop, restart agents based on health or schedule |
| **Resource Governance** | Enforce cost limits, context thresholds, memory bounds |
| **Schedule Control** | Enable/disable schedules, trigger manual runs, pause automation |
| **Validation** | Pre-flight checks before agent operations, config validation |
| **Alerting** | Notify on anomalies, failures, threshold breaches |
| **Cleanup** | Reset stuck sessions, archive old plans, prune logs |
| **Reporting** | Fleet status, cost summaries, health dashboards |

### Out of Scope (Business/Domain Concerns)

| NOT Responsible For | Why |
|---------------------|-----|
| Task orchestration | Agents coordinate themselves via MCP |
| Content/output review | Domain-specific, not infrastructure |
| Workflow design | User/developer responsibility |
| Agent-to-agent messaging | Agents handle directly |
| Business logic validation | Template/agent responsibility |
| Data processing | Agent workload, not ops |

---

## Functional Requirements

### 1. Health Monitoring

#### 1.1 Context Exhaustion Detection
- **Trigger**: Agent context usage > 90%
- **Action**: Log warning, optionally notify user
- **Configurable**: Threshold percentage (default 90%)

#### 1.2 Stuck Agent Detection
- **Trigger**: Agent running but no activity for > N minutes
- **Action**: Log warning, optionally restart
- **Configurable**: Idle timeout (default 30 minutes)

#### 1.3 Container Health
- **Trigger**: Container unhealthy or OOM killed
- **Action**: Log error, attempt restart, alert if repeated
- **Check interval**: Every 60 seconds

#### 1.4 Failed Task Detection
- **Trigger**: Workplan task in `failed` state for > N minutes
- **Action**: Log, notify, optionally pause agent
- **Configurable**: Failure timeout (default 10 minutes)

### 2. Lifecycle Management

#### 2.1 Scheduled Restart
- **Capability**: Restart agents on schedule (e.g., daily at 3am)
- **Use case**: Clear context, refresh state
- **Configurable**: Per-agent or fleet-wide schedules

#### 2.2 Auto-Recovery
- **Trigger**: Agent container exits unexpectedly
- **Action**: Restart with backoff (1s, 5s, 30s, 5m)
- **Max retries**: 5 before alerting

#### 2.3 Graceful Shutdown
- **Capability**: Stop agents in order for maintenance
- **Behavior**: Wait for active tasks, sync to GitHub, then stop

#### 2.4 Fleet Operations
- **Commands**: Start all, stop all, restart all
- **Filtering**: By system prefix, by status, by owner

### 3. Resource Governance

#### 3.1 Cost Limits
- **Capability**: Pause agent when daily cost exceeds threshold
- **Configurable**: Per-agent or global limit in USD
- **Action**: Stop agent, notify user, resume next day

#### 3.2 Context Budget
- **Capability**: Reset session when context exceeds threshold
- **Configurable**: Threshold percentage (default 95%)
- **Action**: Trigger new session, preserve chat history

#### 3.3 Execution Time Limits
- **Capability**: Kill long-running chat executions
- **Configurable**: Max execution time (default 10 minutes)
- **Action**: Terminate, log, notify

### 4. Schedule Control

#### 4.1 Schedule Pause/Resume
- **Capability**: Pause all schedules for an agent or fleet
- **Use case**: Maintenance windows, incident response
- **API**: `POST /api/system-agent/schedules/pause`

#### 4.2 Schedule Validation
- **Capability**: Validate cron expressions before saving
- **Check**: No schedules running more than N times per hour
- **Configurable**: Max frequency (default 12/hour)

#### 4.3 Schedule Conflict Detection
- **Capability**: Warn when multiple agents scheduled simultaneously
- **Threshold**: > 3 agents at same minute
- **Action**: Log warning, suggest staggering

#### 4.4 Emergency Stop
- **Capability**: Immediately halt all scheduled executions
- **Use case**: Runaway costs, system issues
- **API**: `POST /api/system-agent/emergency-stop`

### 5. Validation

#### 5.1 Pre-Start Validation
- **Check before starting agent**:
  - Template exists and valid
  - Required credentials configured
  - MCP servers reachable
  - Sufficient disk space
- **Action**: Block start with clear error if validation fails

#### 5.2 Config Validation
- **Validate on change**:
  - Credential format (KEY=VALUE)
  - Schedule cron syntax
  - Resource limits within bounds
- **Action**: Reject invalid config with error

#### 5.3 Permission Validation
- **Periodic check**:
  - Permission graph has no orphans
  - Shared folder mounts are valid
  - MCP keys not expired
- **Action**: Log warnings, cleanup orphans

### 6. Alerting

#### 6.1 Alert Channels
- **Immediate**: WebSocket broadcast to UI
- **Logged**: Database record for history
- **Future**: Email, Slack, webhook integration

#### 6.2 Alert Types
| Type | Severity | Example |
|------|----------|---------|
| `agent_unhealthy` | Warning | Context > 90% |
| `agent_stuck` | Warning | No activity 30m |
| `agent_failed` | Error | Container exited |
| `cost_exceeded` | Error | Daily limit hit |
| `schedule_failed` | Warning | Execution error |
| `fleet_degraded` | Critical | >50% agents unhealthy |

#### 6.3 Alert Suppression
- **Configurable**: Suppress repeated alerts for N minutes
- **Default**: 15 minutes for same agent + alert type

### 7. Cleanup Operations

#### 7.1 Session Reset
- **Capability**: Clear agent session, start fresh
- **Preserves**: Chat history in database, workplans
- **Clears**: Container context, temp files

#### 7.2 Log Rotation
- **Capability**: Archive/delete old container logs
- **Retention**: Configurable (default 7 days)
- **Schedule**: Daily at 4am

#### 7.3 Plan Archival
- **Capability**: Move completed plans to archive
- **Trigger**: Plan completed > 24 hours ago
- **Location**: `plans/archive/` in agent workspace

### 8. Reporting

#### 8.1 Fleet Status Report
- **Content**: All agents with status, context, last activity
- **Format**: Markdown table
- **Command**: `/ops/status`

#### 8.2 Cost Report
- **Content**: Cost by agent, by day, totals
- **Source**: OTel metrics
- **Command**: `/ops/costs [period]`

#### 8.3 Health Report
- **Content**: Unhealthy agents, recent failures, warnings
- **Format**: Severity-grouped list
- **Command**: `/ops/health`

#### 8.4 Schedule Report
- **Content**: All schedules, next run times, recent executions
- **Format**: Timeline view
- **Command**: `/ops/schedules`

---

## Non-Functional Requirements

### Performance
- Health checks complete in < 5 seconds
- Fleet status report in < 10 seconds for 50 agents
- Alert latency < 2 seconds from detection

### Reliability
- System agent auto-recovers if it crashes
- Operations are idempotent (safe to retry)
- State persists across restarts

### Security
- System agent cannot be deleted
- Only admins can trigger emergency stop
- All operations logged to audit trail

### Observability
- System agent's own health visible in UI
- Operations logged with timestamps
- Metrics exposed for external monitoring

---

## Configuration

### System Settings (stored in `system_settings` table)

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `ops_context_warning_threshold` | int | 90 | Context % to trigger warning |
| `ops_context_critical_threshold` | int | 95 | Context % to trigger reset |
| `ops_idle_timeout_minutes` | int | 30 | Minutes before stuck detection |
| `ops_cost_limit_daily_usd` | float | 50.0 | Daily cost limit (0 = unlimited) |
| `ops_max_execution_minutes` | int | 10 | Max chat execution time |
| `ops_alert_suppression_minutes` | int | 15 | Suppress duplicate alerts |
| `ops_log_retention_days` | int | 7 | Days to keep container logs |
| `ops_health_check_interval` | int | 60 | Seconds between health checks |

### Per-Agent Overrides (stored in agent labels or database)

| Key | Description |
|-----|-------------|
| `ops.cost_limit` | Override daily cost limit |
| `ops.idle_timeout` | Override idle timeout |
| `ops.auto_restart` | Enable/disable auto-restart |
| `ops.health_check` | Enable/disable health monitoring |

---

## API Endpoints

### System Agent Operations

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/system-agent/status` | System agent health |
| POST | `/api/system-agent/restart` | Restart system agent |
| POST | `/api/system-agent/reinitialize` | Reset to clean state |

### Fleet Operations (via System Agent)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/ops/fleet/status` | All agents status |
| GET | `/api/ops/fleet/health` | Health summary |
| POST | `/api/ops/fleet/restart` | Restart all agents |
| POST | `/api/ops/fleet/stop` | Stop all agents |

### Schedule Control

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/ops/schedules/pause` | Pause all schedules |
| POST | `/api/ops/schedules/resume` | Resume all schedules |
| POST | `/api/ops/emergency-stop` | Halt all executions |

### Alerting

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/ops/alerts` | List recent alerts |
| POST | `/api/ops/alerts/{id}/acknowledge` | Acknowledge alert |

---

## Slash Commands (System Agent)

| Command | Description |
|---------|-------------|
| `/ops/status` | Fleet status report |
| `/ops/health` | Health check all agents |
| `/ops/costs [period]` | Cost report (day/week/month) |
| `/ops/schedules` | Schedule overview |
| `/ops/restart <agent>` | Restart specific agent |
| `/ops/restart-all` | Restart entire fleet |
| `/ops/stop <agent>` | Stop specific agent |
| `/ops/pause-schedules` | Pause all schedules |
| `/ops/resume-schedules` | Resume all schedules |
| `/ops/reset <agent>` | Reset agent session |
| `/ops/validate <agent>` | Run validation checks |
| `/ops/cleanup` | Run cleanup operations |

---

## Implementation Phases

### Phase 1: Core Operations (Priority: HIGH)
- [ ] Enhanced CLAUDE.md with ops-only scope
- [ ] `/ops/status` command
- [ ] `/ops/health` command
- [ ] `/ops/restart` command
- [ ] Basic health check loop

### Phase 2: Monitoring (Priority: HIGH)
- [ ] Context exhaustion detection
- [ ] Stuck agent detection
- [ ] Alert WebSocket broadcasting
- [ ] Health check interval configuration

### Phase 3: Schedule Control (Priority: MEDIUM)
- [ ] Pause/resume schedules API
- [ ] Emergency stop functionality
- [ ] Schedule validation
- [ ] Conflict detection

### Phase 4: Governance (Priority: MEDIUM)
- [ ] Cost limit enforcement
- [ ] Execution time limits
- [ ] Per-agent overrides
- [ ] Settings UI

### Phase 5: Cleanup & Reporting (Priority: LOW)
- [ ] Session reset capability
- [ ] Log rotation
- [ ] Plan archival
- [ ] Cost/schedule reports

---

## Success Criteria

1. **Health Visibility**: Can see fleet health status within 10 seconds
2. **Quick Recovery**: Stuck agents detected and restarted within 5 minutes
3. **Cost Control**: No agent exceeds configured daily limit
4. **Schedule Safety**: Can pause all automation with single command
5. **Self-Healing**: System agent recovers automatically from failures

---

## Related Documents

- `docs/memory/feature-flows/internal-system-agent.md` - Current implementation
- `docs/memory/requirements.md` - Requirement 11.1
- `docs/drafts/INTERNAL_SYSTEM_AGENT.md` - Original design doc
