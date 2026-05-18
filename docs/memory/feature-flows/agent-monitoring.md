# Feature: Agent Monitoring Service (MON-001)

> **SUB-002 Update (2026-03-03):** Credential file monitoring removed. SUB-002 injects tokens as container env vars (`CLAUDE_CODE_OAUTH_TOKEN`) instead of `.credentials.json` files. Env vars persist across restarts and do not require file-presence monitoring or auto-remediation. The `credential_status` field on `BusinessHealthCheck` is deprecated (always `None`). The `alert_subscription_credentials_missing()` alert function has been removed.

## Overview

Multi-layer health monitoring system for agent fleet. Performs periodic Docker container, network, and business logic health checks. Stores results in database, sends alerts on status changes, and broadcasts real-time updates via WebSocket.

## User Story

As a Trinity platform admin, I want to monitor the health of all agents in real-time so that I can identify and resolve issues before they impact operations, with automated alerts when agents become unhealthy.

---

## Key Concepts

### Health Status Levels

| Status | Description | Priority |
|--------|-------------|----------|
| **healthy** | All checks passing | 4 (lowest) |
| **degraded** | Performance issues (high CPU, memory, latency) | 3 |
| **unhealthy** | Critical failures (network unreachable, runtime down) | 2 |
| **critical** | Container failures (stopped, OOM killed) | 1 (highest) |
| **unknown** | No health check data available | 3 |

### Three-Layer Health Checks

```
Layer 1: Docker          Layer 2: Network          Layer 3: Business
+------------------+     +------------------+       +------------------+
| Container status |     | HTTP reachability|       | Runtime status   |
| CPU/Memory usage |     | /health endpoint |       | Claude available |
| Restart count    |     | Response latency |       | Context usage %  |
| OOM killed       |     | Status code      |       | Active executions|
+------------------+     +------------------+       | Error rate       |
                                                    +------------------+
                              |
                              v
                    +------------------+
                    | Aggregate Status |
                    | (worst of three) |
                    +------------------+
```

### Status Aggregation Priority

```
Docker > Network > Business

CRITICAL: Container not found / stopped / OOM killed
UNHEALTHY: Network unreachable / Runtime not available
DEGRADED: High CPU/Memory / High latency / High context usage / Stuck executions
         (credential_status == "missing" no longer triggers DEGRADED — removed in SUB-002)
HEALTHY: All checks passing
```

---

## Entry Points

| UI Location | API Endpoint | Purpose |
|-------------|--------------|---------|
| **NavBar "Health" link** | - | Navigate to `/monitoring` |
| **Monitoring Page** | `GET /api/monitoring/status` | View fleet-wide health |
| **Agent Row Click** | - | Navigate to agent detail |
| **"Check All" button** | `POST /api/monitoring/check-all` | Trigger fleet-wide check (admin) |
| **Agent refresh button** | `POST /api/monitoring/agents/{name}/check` | Trigger single agent check (admin) |
| MCP: `get_fleet_health` | `GET /api/monitoring/status` | Fleet health via MCP |
| MCP: `get_agent_health` | `GET /api/monitoring/agents/{name}` | Agent health via MCP |
| MCP: `trigger_health_check` | `POST /api/monitoring/agents/{name}/check` | Trigger check via MCP |

---

## Database Schema

### Table: agent_health_checks (`src/backend/db/schema.py:422-445`)

```sql
CREATE TABLE IF NOT EXISTS agent_health_checks (
    id TEXT PRIMARY KEY,
    agent_name TEXT NOT NULL,
    check_type TEXT NOT NULL,           -- docker, network, business, aggregate
    status TEXT NOT NULL,               -- healthy, degraded, unhealthy, critical
    -- Docker metrics
    container_status TEXT,
    cpu_percent REAL,
    memory_percent REAL,
    memory_mb REAL,
    restart_count INTEGER,
    oom_killed INTEGER,
    -- Network metrics
    reachable INTEGER,
    latency_ms REAL,
    -- Business metrics
    runtime_available INTEGER,
    claude_available INTEGER,
    context_percent REAL,
    active_executions INTEGER,
    error_rate REAL,
    -- Common
    error_message TEXT,
    checked_at TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
```

**Indexes** (`src/backend/db/schema.py:551-556`):
```sql
CREATE INDEX IF NOT EXISTS idx_health_agent_time ON agent_health_checks(agent_name, checked_at DESC);
CREATE INDEX IF NOT EXISTS idx_health_status ON agent_health_checks(status);
CREATE INDEX IF NOT EXISTS idx_health_type ON agent_health_checks(check_type);
CREATE INDEX IF NOT EXISTS idx_health_checked_at ON agent_health_checks(checked_at);
```

### Table: monitoring_alert_cooldowns (`src/backend/db/schema.py:447-455`)

```sql
CREATE TABLE IF NOT EXISTS monitoring_alert_cooldowns (
    id TEXT PRIMARY KEY,
    agent_name TEXT NOT NULL,
    condition TEXT NOT NULL,            -- e.g., "status:critical", "container_stopped"
    last_alert_at TEXT NOT NULL,
    UNIQUE(agent_name, condition)
);
```

---

## Pydantic Models (`src/backend/db_models.py:653-808`)

### Health Check Models

```python
class AgentHealthStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    CRITICAL = "critical"
    UNKNOWN = "unknown"

class DockerHealthCheck(BaseModel):
    agent_name: str
    container_status: Optional[str]     # running, stopped, paused, restarting
    exit_code: Optional[int]
    restart_count: int = 0
    oom_killed: bool = False
    cpu_percent: Optional[float]
    memory_percent: Optional[float]
    memory_mb: Optional[float]
    checked_at: str

class NetworkHealthCheck(BaseModel):
    agent_name: str
    reachable: bool
    status_code: Optional[int]
    latency_ms: Optional[float]
    error: Optional[str]
    checked_at: str

class BusinessHealthCheck(BaseModel):
    agent_name: str
    status: str = "healthy"             # healthy, degraded, unhealthy
    runtime_available: Optional[bool]
    claude_available: Optional[bool]
    context_percent: Optional[float]
    active_execution_count: int = 0
    stuck_execution_count: int = 0
    recent_error_rate: float = 0.0
    checked_at: str
```

### Response Models

```python
class AgentHealthDetail(BaseModel):
    agent_name: str
    aggregate_status: str
    last_check_at: Optional[str]
    docker: Optional[DockerHealthCheck]
    network: Optional[NetworkHealthCheck]
    business: Optional[BusinessHealthCheck]
    issues: List[str] = []
    recent_alerts: List[dict] = []
    uptime_percent_24h: Optional[float]
    avg_latency_24h_ms: Optional[float]

class FleetHealthStatus(BaseModel):
    enabled: bool = True
    last_check_at: Optional[str]
    summary: FleetHealthSummary
    agents: List[AgentHealthSummary] = []

class MonitoringConfig(BaseModel):
    enabled: bool = True
    docker_check_interval: int = 30     # seconds
    network_check_interval: int = 30
    business_check_interval: int = 60
    http_timeout: float = 10.0
    cpu_warning_percent: float = 80.0
    cpu_critical_percent: float = 95.0
    memory_warning_percent: float = 85.0
    memory_critical_percent: float = 95.0
    latency_warning_ms: float = 2000.0
    latency_critical_ms: float = 5000.0
    context_warning_percent: float = 85.0
    context_critical_percent: float = 95.0
    critical_cooldown: int = 300        # 5 min
    unhealthy_cooldown: int = 600       # 10 min
    degraded_cooldown: int = 1800       # 30 min
```

---

## Frontend Layer

### Route Definition (`src/frontend/src/router/index.js:36-40`)

```javascript
{
  path: '/monitoring',
  name: 'Monitoring',
  component: () => import('../views/Monitoring.vue'),
  meta: { requiresAuth: true, requiresAdmin: true }
}
```

### Navigation (`src/frontend/src/components/NavBar.vue:25-32`)

Admin-only visibility via `v-if="isAdmin"`:

```html
<router-link v-if="isAdmin" to="/monitoring" ...>
  Health
</router-link>
```

### Monitoring Page (`src/frontend/src/views/Monitoring.vue`)

**Template Structure** (lines 1-227):
- Header with monitoring status badge and controls
- Summary cards (total, healthy, degraded, unhealthy, critical counts)
- Active alerts section (collapsible, shows recent alerts)
- Agent health grid with status indicators and issues

**Key State Variables** (lines 253-259):
```javascript
const monitoringStore = useMonitoringStore()
const isAdmin = ref(false)
const statusFilter = ref('')
const triggeringCheck = ref(false)
const checkingAgent = ref(null)
const autoRefreshEnabled = ref(true)
const refreshCountdown = ref(30)
```

**Key Methods**:

| Method | Line | Description |
|--------|------|-------------|
| `refreshAll()` | 294-300 | Fetch status + alerts |
| `triggerFleetCheck()` | 336-347 | POST to /check-all |
| `triggerAgentCheck(name)` | 349-358 | POST to /agents/{name}/check |
| `viewAgentDetail(name)` | 360-362 | Navigate to agent page |
| `startAutoRefresh()` | 302-316 | 30s polling interval |

### Monitoring Store (`src/frontend/src/stores/monitoring.js`)

**State** (lines 16-48):
```javascript
state: () => ({
  enabled: true,
  loading: false,
  error: null,
  lastCheck: null,
  summary: { total_agents: 0, healthy: 0, degraded: 0, unhealthy: 0, critical: 0, unknown: 0 },
  agents: [],
  alerts: [],
  config: null,
  agentDetailCache: {}
})
```

**Actions**:

| Action | Line | API Call |
|--------|------|----------|
| `fetchStatus()` | 120-139 | `GET /api/monitoring/status` |
| `fetchAgentHealth(name)` | 144-157 | `GET /api/monitoring/agents/{name}` |
| `fetchAgentHistory(name, hours, type)` | 162-173 | `GET /api/monitoring/agents/{name}/history` |
| `triggerCheck(name)` | 178-197 | `POST /api/monitoring/agents/{name}/check` |
| `fetchAlerts(status)` | 202-217 | `GET /api/monitoring/alerts` |
| `fetchConfig()` | 222-236 | `GET /api/monitoring/config` |
| `updateConfig(config)` | 241-253 | `PUT /api/monitoring/config` |
| `enableMonitoring()` | 258-268 | `POST /api/monitoring/enable` |
| `disableMonitoring()` | 273-283 | `POST /api/monitoring/disable` |
| `triggerFleetCheck()` | 288-298 | `POST /api/monitoring/check-all` |

**WebSocket Handler** (lines 332-363):
```javascript
handleHealthEvent(event) {
  if (event.type === 'agent_health_changed') {
    // Update agent in list
    // Recalculate summary
    // Invalidate cache
  } else if (event.type === 'monitoring_alert') {
    // Add to alerts list
  }
}
```

---

## Backend Layer

### Router Registration (`src/backend/main.py:64, 182-183, 325`)

```python
from routers.monitoring import (
    router as monitoring_router,
    set_websocket_manager as set_monitoring_ws_manager,
    set_filtered_websocket_manager as set_monitoring_filtered_ws_manager
)

# Line 182-183: WebSocket manager injection
set_monitoring_ws_manager(manager)
set_monitoring_filtered_ws_manager(filtered_manager)

# Line 325: Router registration
app.include_router(monitoring_router)  # Agent Monitoring (MON-001)
```

### API Router (`src/backend/routers/monitoring.py`)

| Endpoint | Line | Method | Auth | Description |
|----------|------|--------|------|-------------|
| `GET /api/monitoring/status` | 87-160 | `get_fleet_status()` | User | Fleet health summary |
| `GET /api/monitoring/agents/{name}` | 167-249 | `get_agent_health()` | Owner | Agent health detail |
| `GET /api/monitoring/agents/{name}/history` | 252-273 | `get_agent_health_history()` | Owner | Historical checks |
| `POST /api/monitoring/agents/{name}/check` | 276-303 | `trigger_health_check()` | Admin | Force health check |
| `GET /api/monitoring/alerts` | 310-331 | `get_active_alerts()` | Admin | List health alerts |
| `GET /api/monitoring/config` | 338-358 | `get_monitoring_config()` | Admin | Get config |
| `PUT /api/monitoring/config` | 360-380 | `update_monitoring_config()` | Admin | Update config |
| `POST /api/monitoring/enable` | 383-400 | `enable_monitoring()` | Admin | Start service |
| `POST /api/monitoring/disable` | 403-420 | `disable_monitoring()` | Admin | Stop service |
| `POST /api/monitoring/check-all` | 427-455 | `trigger_fleet_health_check()` | Admin | Check all agents |
| `DELETE /api/monitoring/history` | 458-473 | `cleanup_health_history()` | Admin | Delete old records |

### Fleet Status Endpoint (`src/backend/routers/monitoring.py:87-160`)

```python
@router.get("/status", response_model=FleetHealthStatus)
async def get_fleet_status(current_user: User = Depends(get_current_user)):
    # 1. List all agents via Docker
    all_agents = list_all_agents_fast()

    # 2. Filter to accessible agents (admin sees all)
    if current_user.role != "admin":
        accessible = get_accessible_agents(current_user.email, all_agent_names)
        agent_names = [n for n in all_agent_names if n in accessible_names]

    # 3. Get latest health checks from DB
    latest_checks = db.get_all_latest_health_checks(agent_names, "aggregate")
    summary = db.get_health_summary(agent_names)

    # 4. Build agent summaries
    agents = [AgentHealthSummary(name=name, status=check["status"], ...) for ...]

    # 5. Sort by severity (critical first)
    agents.sort(key=lambda a: status_order.get(a.status, 3))

    return FleetHealthStatus(enabled=..., summary=..., agents=agents)
```

### Monitoring Service (`src/backend/services/monitoring_service.py`)

**Health Check Functions**:

| Function | Line | Description |
|----------|------|-------------|
| `check_docker_health(agent_name)` | 56-131 | Docker container status, CPU, memory |
| `check_network_health(agent_name, timeout)` | 134-182 | HTTP /health endpoint, latency |
| `check_business_health(agent_name, timeout)` | 185-277 | Runtime, context, executions |
| `aggregate_health(docker, network, business, config)` | 280-353 | Combine into single status |
| `perform_health_check(agent_name, config, store_results)` | 360-522 | Run all checks, store, alert (credential auto-remediation removed in SUB-002) |
| `perform_fleet_health_check(agent_names, config, store_results)` | 525-595 | Parallel checks with semaphore |

**Docker Health Check** (lines 56-131):
```python
def check_docker_health(agent_name: str) -> DockerHealthCheck:
    container = docker_client.containers.get(f"agent-{agent_name}")
    state = container.attrs.get("State", {})

    # Get one-shot stats (CPU, memory)
    stats = container.stats(stream=False)
    cpu_percent = calculate_cpu_percent(stats)
    memory_percent = calculate_memory_percent(stats)

    return DockerHealthCheck(
        container_status=container.status,
        exit_code=state.get("ExitCode"),
        restart_count=container.attrs.get("RestartCount", 0),
        oom_killed=state.get("OOMKilled", False),
        cpu_percent=cpu_percent,
        memory_percent=memory_percent,
        memory_mb=memory_mb
    )
```

**Network Health Check** (lines 134-182):
```python
async def check_network_health(agent_name: str, timeout: float = 10.0) -> NetworkHealthCheck:
    url = f"http://agent-{agent_name}:8000/health"
    start = time.monotonic()

    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(url)
        latency_ms = (time.monotonic() - start) * 1000

    return NetworkHealthCheck(
        reachable=True,
        status_code=response.status_code,
        latency_ms=latency_ms
    )
```

**Business Health Check** (lines 185-277):
```python
async def check_business_health(agent_name: str, timeout: float = 10.0) -> BusinessHealthCheck:
    # Check /health for runtime status
    health_response = await client.get(f"http://agent-{agent_name}:8000/health")
    runtime_available = health_response.json().get("runtime_available", True)

    # Check /api/chat/session for context usage
    session_response = await client.get(f"http://agent-{agent_name}:8000/api/chat/session")
    context_percent = (context_used / context_max) * 100

    # Check /api/executions/running for stuck executions
    exec_response = await client.get(f"http://agent-{agent_name}:8000/api/executions/running")
    stuck_execution_count = count_stuck_executions(executions)

    # NOTE: credential_status is deprecated (always None) since SUB-002.
    # Credential file checks (/api/credentials/status) and degradation logic removed.
    # Tokens are now injected as container env vars, not files.

    return BusinessHealthCheck(
        status=determine_status(),
        runtime_available=runtime_available,
        context_percent=context_percent,
        active_execution_count=len(executions),
        stuck_execution_count=stuck_execution_count,
        credential_status=None  # deprecated (SUB-002)
    )
```

**Background Service** (lines 602-701):
```python
class MonitoringService:
    def __init__(self, config: MonitoringConfig = DEFAULT_CONFIG):
        self.config = config
        self._running = False
        self._task = None

    async def start(self):
        self._running = True
        self._task = asyncio.create_task(self._run_loop())

    async def _run_loop(self):
        while self._running:
            await self._run_check_cycle()
            await asyncio.sleep(self.config.docker_check_interval)

    async def _run_check_cycle(self):
        agents = list_all_agents_fast()
        running_agents = [a.name for a in agents if a.status == "running"]
        await perform_fleet_health_check(running_agents, self.config, store_results=True)
```

### Alert Service (`src/backend/services/monitoring_alerts.py`)

**Cooldown Configuration** (lines 26-38):
```python
DEFAULT_COOLDOWNS = {
    AgentHealthStatus.CRITICAL: 300,    # 5 min
    AgentHealthStatus.UNHEALTHY: 600,   # 10 min
    AgentHealthStatus.DEGRADED: 1800,   # 30 min
}
```

**Alert Evaluation** (lines 72-121):
```python
async def evaluate_and_alert(
    self,
    agent_name: str,
    previous_status: str,
    current_status: str,
    issues: List[str],
    details: Dict = None
) -> Optional[str]:
    # Determine if degradation or recovery
    is_degradation = self._is_degradation(prev, curr)
    is_recovery = self._is_recovery(prev, curr)

    if is_degradation:
        return await self._send_degradation_alert(...)
    elif is_recovery:
        return await self._send_recovery_alert(...)
```

**Degradation Alert** (lines 151-202):
```python
async def _send_degradation_alert(self, ...):
    # Check cooldown
    if db.is_in_alert_cooldown(agent_name, condition, cooldown_seconds):
        return None

    # Create notification via NOTIF-001
    notification = db.create_notification(
        agent_name=agent_name,
        data=NotificationCreate(
            notification_type="alert",
            title=f"Agent {agent_name} is {curr.value}",
            message="; ".join(issues),
            priority=STATUS_PRIORITIES.get(curr),
            category="health"
        )
    )

    # Set cooldown
    db.set_alert_cooldown(agent_name, condition)

    # Broadcast via WebSocket
    await self._broadcast_alert(notification)
```

Degradation notifications include `metadata.resolution_state='active_failure'`.
When the aggregate health check later recovers to `healthy`, pending health
alert notifications for that agent are dismissed by `system:recovered` and a
new pending info notification is created with
`metadata.resolution_state='recovered_unacknowledged'` plus
`recovery_evidence` (`verified_status`, `verified_at`,
`recovered_from_status`, and the recovered alert IDs). This keeps active
failures distinct from recovered-but-unacknowledged alerts without deleting
history.

**Specific Alert Types** (lines 272-410):
- `alert_container_stopped()` - OOM kill, crash, unexpected stop
- `alert_high_restart_count()` - Container restarting frequently
- `alert_stuck_execution()` - Execution running > 30 min
- `alert_resource_critical()` - High CPU/memory usage
- ~~`alert_subscription_credentials_missing()`~~ - **Removed** (SUB-002): credential file monitoring no longer needed

### Database Operations (`src/backend/db/monitoring.py`)

**Health Check CRUD** (lines 24-249):

| Method | Line | Description |
|--------|------|-------------|
| `create_health_check(...)` | 24-97 | Insert new health check record |
| `get_latest_health_check(agent, type)` | 99-116 | Most recent check |
| `get_agent_health_history(agent, type, hours, limit)` | 118-137 | Historical records |
| `get_all_latest_health_checks(agents, type)` | 139-181 | Latest for multiple agents |
| `get_health_summary(agents)` | 183-203 | Count by status |
| `calculate_uptime_percent(agent, hours)` | 205-216 | 24h uptime % |
| `calculate_avg_latency(agent, hours)` | 218-229 | 24h avg latency |
| `cleanup_old_records(days)` | 231-249 | Delete old records |

**Alert Cooldowns** (lines 255-341):

| Method | Line | Description |
|--------|------|-------------|
| `get_cooldown(agent, condition)` | 255-268 | Get last alert timestamp |
| `set_cooldown(agent, condition)` | 270-286 | Set/update cooldown |
| `clear_cooldown(agent, condition)` | 288-302 | Clear cooldown |
| `is_in_cooldown(agent, condition, seconds)` | 304-321 | Check if in cooldown |
| `cleanup_cooldowns(agent)` | 323-341 | Clear all cooldowns |

---

## WebSocket Integration

### Broadcast Function (`src/backend/routers/monitoring.py:57-81`)

```python
async def _broadcast_health_change(
    agent_name: str,
    previous_status: str,
    current_status: str,
    issues: List[str]
):
    event = {
        "type": "agent_health_changed",
        "agent_name": agent_name,
        "previous_status": previous_status,
        "current_status": current_status,
        "issues": issues,
        "timestamp": utc_now_iso()
    }

    if _websocket_manager:
        await _websocket_manager.broadcast(json.dumps(event))

    if _filtered_websocket_manager:
        await _filtered_websocket_manager.broadcast_filtered(event)
```

### Alert Broadcast (`src/backend/services/monitoring_alerts.py:246-266`)

```python
async def _broadcast_alert(self, notification):
    event = {
        "type": "monitoring_alert",
        "notification_id": notification.id,
        "agent_name": notification.agent_name,
        "alert_type": notification.notification_type,
        "priority": notification.priority,
        "title": notification.title,
        "timestamp": notification.created_at
    }

    if _websocket_manager:
        await _websocket_manager.broadcast(json.dumps(event))
```

### Frontend Handler (`src/frontend/src/stores/monitoring.js:332-363`)

```javascript
handleHealthEvent(event) {
  if (event.type === 'agent_health_changed') {
    const index = this.agents.findIndex(a => a.name === event.agent_name)
    if (index >= 0) {
      this.agents[index] = {
        ...this.agents[index],
        status: event.current_status,
        issues: event.issues,
        last_check_at: event.timestamp
      }
      this._recalculateSummary()
    }
    delete this.agentDetailCache[event.agent_name]
  } else if (event.type === 'monitoring_alert') {
    this.alerts.unshift({
      id: event.notification_id,
      agent_name: event.agent_name,
      title: event.title,
      priority: event.priority,
      status: 'pending',
      created_at: event.timestamp
    })
  }
}
```

---

## MCP Tools

### Tool Registration (`src/mcp-server/src/server.ts:244-251`)

```typescript
// Register monitoring tools (3 tools) - MON-001
const monitoringTools = createMonitoringTools(client, requireApiKey);
server.addTool(monitoringTools.getFleetHealth);
server.addTool(monitoringTools.getAgentHealth);
server.addTool(monitoringTools.triggerHealthCheck);
```

### Tool Definitions (`src/mcp-server/src/tools/monitoring.ts`)

| Tool | Line | Parameters | Description |
|------|------|------------|-------------|
| `get_fleet_health` | 41-91 | (none) | Fleet-wide health summary |
| `get_agent_health` | 96-158 | `agent_name` | Detailed agent health |
| `trigger_health_check` | 163-203 | `agent_name` | Force immediate check (admin) |

### Client Methods (`src/mcp-server/src/client.ts:829-912`)

```typescript
async getFleetHealth(): Promise<FleetHealthStatus> {
  return this.request("GET", "/api/monitoring/status");
}

async getAgentHealth(agentName: string): Promise<AgentHealthDetail> {
  return this.request("GET", `/api/monitoring/agents/${encodeURIComponent(agentName)}`);
}

async triggerAgentHealthCheck(agentName: string): Promise<AgentHealthDetail> {
  return this.request("POST", `/api/monitoring/agents/${encodeURIComponent(agentName)}/check`);
}
```

---

## Data Flow Diagrams

### Flow 1: View Fleet Health

```
User                  Frontend                   Backend                   Database
  |                      |                          |                          |
  | Visit /monitoring    |                          |                          |
  |------------------->  |                          |                          |
  |                      | GET /api/monitoring/     |                          |
  |                      | status                   |                          |
  |                      |------------------------>|                          |
  |                      |                          | list_all_agents_fast()   |
  |                      |                          | (Docker API)             |
  |                      |                          |                          |
  |                      |                          | get_all_latest_health_   |
  |                      |                          | checks(agents, "aggregate")
  |                      |                          |------------------------>|
  |                      |                          |<------------------------|
  |                      |                          |                          |
  |                      |                          | get_health_summary()     |
  |                      |                          |------------------------>|
  |                      |                          |<------------------------|
  |                      |<------------------------|                          |
  |                      |                          |                          |
  |                      | Store: agents, summary   |                          |
  |<---------------------| Render grid              |                          |
```

### Flow 2: Background Health Check Cycle

```
MonitoringService          monitoring_service.py          Database
      |                              |                        |
      | _run_check_cycle()           |                        |
      |----------------------------->|                        |
      |                              | list_all_agents_fast() |
      |                              | (get running agents)   |
      |                              |                        |
      |                              | perform_fleet_health_  |
      |                              | check()                |
      |                              |                        |
      |       For each agent (parallel with semaphore=10):    |
      |       +-----------------------------------------------+
      |       | check_docker_health()   (Docker API)          |
      |       | check_network_health()  (HTTP to agent:8000)  |
      |       | check_business_health() (HTTP to agent:8000)  |
      |       | aggregate_health()                            |
      |       +-----------------------------------------------+
      |                              |                        |
      |                              | create_health_check()  |
      |                              | (4 records: docker,    |
      |                              |  network, business,    |
      |                              |  aggregate)            |
      |                              |----------------------->|
      |                              |                        |
      |                              | If status changed:     |
      |                              | evaluate_and_alert()   |
      |                              |                        |
      |                              | WebSocket broadcast    |
      |<-----------------------------|                        |
```

### Flow 3: Alert with Cooldown

```
monitoring_service.py          monitoring_alerts.py          Database             WebSocket
       |                              |                          |                     |
       | Status changed:              |                          |                     |
       | healthy -> unhealthy         |                          |                     |
       |----------------------------->|                          |                     |
       |                              | is_in_alert_cooldown()?  |                     |
       |                              |------------------------->|                     |
       |                              |<-------------------------|                     |
       |                              | (not in cooldown)        |                     |
       |                              |                          |                     |
       |                              | create_notification()    |                     |
       |                              |------------------------->|                     |
       |                              |<-------------------------|                     |
       |                              |                          |                     |
       |                              | set_alert_cooldown()     |                     |
       |                              |------------------------->|                     |
       |                              |                          |                     |
       |                              | _broadcast_alert()       |                     |
       |                              |--------------------------------------->|
       |                              |                          |                     |
       |<-----------------------------|                          |             Frontend
```

---

## Configuration

### Default Thresholds (`src/backend/db_models.py:752-784`)

| Metric | Warning | Critical |
|--------|---------|----------|
| CPU | 80% | 95% |
| Memory | 85% | 95% |
| Latency | 2000ms | 5000ms |
| Context | 85% | 95% |
| Error Rate | 30% | 50% |

### Cooldowns

| Status | Duration |
|--------|----------|
| Critical | 5 minutes |
| Unhealthy | 10 minutes |
| Degraded | 30 minutes |

### Check Intervals

| Layer | Default Interval |
|-------|------------------|
| Docker | 30 seconds |
| Network | 30 seconds |
| Business | 60 seconds |

---

## Error Handling

| Error Case | HTTP Status | Message |
|------------|-------------|---------|
| Agent not found | 404 | "Agent not found" |
| Access denied (not owner) | 403 | "Access denied to this agent" |
| Not admin (trigger check) | 403 | "Admin access required" |
| Docker unavailable | 200 | Status returned as "unknown" |
| Agent unreachable | 200 | Network check reachable=false |

---

## Security Considerations

1. **Authorization**: Fleet status filtered by agent access (owner or shared)
2. **Admin-only operations**: Trigger checks, enable/disable service, view alerts
3. **No credential exposure**: Health checks don't include sensitive data
4. **WebSocket filtering**: Filtered manager respects agent permissions
5. **Cooldown protection**: Prevents alert storms during flapping

---

## Observability

### Metrics Available

- **Uptime percentage** (24h rolling)
- **Average latency** (24h rolling)
- **Health check history** (configurable retention, default 7 days)
- **Alert history** (via notifications table)

### Log Output

```
Monitoring service started
[Monitoring] Checking health for 5 running agents
[Monitoring] Agent my-agent status changed: healthy -> degraded
Failed to send monitoring alert: [error details]
Monitoring service stopped
```

---

## Related Flows

- **Agent Lifecycle** (`agent-lifecycle.md`) - Monitoring state when agents start/stop
- **Notification System** (`notifications.md`) - Alert delivery via NOTIF-001
- **WebSocket Broadcasting** (`websocket-events.md`) - Real-time event delivery
- **MCP Orchestration** (`mcp-orchestration.md`) - Monitoring tools via MCP

---

## Revision History

| Date | Changes |
|------|---------|
| 2026-03-03 | **SUB-002 credential monitoring removal**: Removed credential file checks from `check_business_health()`, `aggregate_health()`, and `perform_health_check()`. Removed `alert_subscription_credentials_missing()` from `monitoring_alerts.py`. Removed auto-remediation via `inject_subscription_on_start()`. `credential_status` field deprecated (always `None`). Tokens now injected as container env vars. |
| 2026-02-23 | **Admin-only access restriction**: NavBar "Health" link now requires admin (`v-if="isAdmin"` at NavBar.vue:26), route meta updated to `requiresAdmin: true` (router/index.js:39). Frontend Layer section already documented this correctly. |
| 2026-02-23 | Initial documentation for MON-001 implementation |
