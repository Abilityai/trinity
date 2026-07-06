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
UNHEALTHY: Network unreachable / /health 5xx response / Runtime not available
DEGRADED: High CPU/Memory / High latency / High context usage / Stuck executions
         (credential_status == "missing" no longer triggers DEGRADED — removed in SUB-002)
HEALTHY: All checks passing
```

**Note (#474)**: A `/health` probe that completes with any HTTP response (200..599) records circuit-breaker success — the agent is TCP-reachable. To prevent a wedged-but-listening agent from being silently HEALTHY, `aggregate_health()` has an explicit `status_code >= 500 → UNHEALTHY` branch (`monitoring_service.py:419`). 5xx is **not** a circuit signal; only `ConnectError`/`ConnectTimeout` count toward the per-agent circuit threshold.

**Note (#1463)**: A `/health` **read timeout** normally counts as a liveness failure (a fast endpoint that doesn't answer ⇒ wedged event loop). The exception: when the agent has an execution in flight (`_agent_has_active_executions()` → CapacityManager `is_busy`), the timeout is treated as *busy, not wedged* and stays circuit-neutral — a long CPU-bound run starves the event loop but completes successfully, so counting it opened the breaker and fast-failed every other trigger. The probe still reports `reachable=False`; only the circuit signal is suppressed. Fail-open (lookup error ⇒ treat as idle ⇒ record failure).

---

## Richer Agent `/health` Signal (#1020)

> **#1020 (2026-06-02):** The agent-server `/health` endpoint was promoted from `{status}` + ad-hoc #333 diagnostics to a **named, contractual signal** the platform acts on — an incremental step toward `TARGET_ARCHITECTURE.md` §Agent Runtime. This is orthogonal to the #474/#798 circuit-breaker classification above: that work is about *whether the probe reached the agent*; this work is about *what the agent reports once reached*.

### New `/health` fields

The agent-server `/health` handler (`docker/base-image/agent_server/routers/info.py:98-123`, `health_check()`) now returns these named fields beyond the pre-existing `status` / `runtime_available` / `claude_available` / `message_count`:

| Field | Type | Meaning |
|-------|------|---------|
| `active_tasks` | int | Concurrent executions across `/api/chat` **and** `/api/task` |
| `last_task_at` | ISO string \| null | Timestamp of the most recent task start/finish |
| `consecutive_failures` | int | Reset to 0 on success, incremented on failure |
| `diagnostics` | object | Pre-existing #333 runtime gauges (`thread_count`, `asyncio_task_count`, `running_executions`, `conversation_history_size`, `conversation_history_limit`) |

**`mailbox_depth` is intentionally NOT emitted.** There is no agent-side mailbox until the actor model lands (#945); the backend derives queue depth from `CapacityManager`. (Documented inline at `info.py:114-118` and `state.py:64-67`.)

Back-compat: existing `/health` keys are unchanged and the new keys are purely additive.

### Counters in `agent_state` (`docker/base-image/agent_server/state.py`)

The three new fields are backed by counters on the global `AgentState` instance, initialized at `state.py:68-71` under a `threading.Lock` (`_health_lock`) because `/api/task` runs concurrently:

```python
self._health_lock = threading.Lock()
self.active_task_count: int = 0
self.last_task_at: Optional[str] = None
self.consecutive_failures: int = 0
```

| Method | Line | Behavior |
|--------|------|----------|
| `record_task_start()` | `state.py:73-77` | `active_task_count += 1`, refresh `last_task_at` |
| `record_task_finish(success)` | `state.py:79-90` | `active_task_count -= 1` (floored at 0), refresh `last_task_at`; on `success` reset `consecutive_failures = 0`, else `consecutive_failures += 1` |

### Wiring at the execution chokepoints (`docker/base-image/agent_server/routers/chat.py`)

Both execution paths bracket the runtime call with start/finish, using `except BaseException` so a cancelled/failed run still records a failure:

- `/api/chat` (`chat.py:24-25`, `chat()`): `record_task_start()` at `chat.py:47`; `record_task_finish(success=False)` in the `except` at `chat.py:58`; `record_task_finish(success=True)` at `chat.py:60`.
- `/api/task` (`chat.py:106-107`, `execute_task()`): `record_task_start()` at `chat.py:133`; `record_task_finish(success=False)` at `chat.py:148`; `record_task_finish(success=True)` at `chat.py:150`.

### Backend consumption (`src/backend/services/monitoring_service.py`)

`check_business_health()` (`monitoring_service.py:386`) reads the two persisted signals off the `/health` JSON and threads them into the business-layer model:

- Locals default to `None` (`monitoring_service.py:408-409`) — this is the **graceful default for pre-#1020 agent images** that don't emit the keys, so older agents never break fleet health.
- `consecutive_failures = health_data.get("consecutive_failures")` and `last_task_at = health_data.get("last_task_at")` (`monitoring_service.py:422-423`).
- Passed to `BusinessHealthCheck(...)` at `monitoring_service.py:486-487`.

`consecutive_failures` is the signal the **dispatch circuit breaker (#526)** consumes; both fields feed **fleet-health scoring / heartbeat push (#307)**. `active_tasks` is reported on `/health` but is not currently persisted into `BusinessHealthCheck` (the backend tracks active executions via `/api/executions/running`, see `active_execution_count` below).

### Model field (`src/backend/db_models.py`)

`BusinessHealthCheck` carries the two persisted fields (`db_models.py:840-856`), both `Optional` with a `None` default for the pre-#1020 case:

```python
consecutive_failures: Optional[int] = None   # db_models.py:854
last_task_at: Optional[str] = None            # db_models.py:855
```

---

## Entry Points

> **UI surface moved 2026-06-09 (#1109):** the fleet-monitoring UI is no longer a standalone `/monitoring` page. Its content was extracted into `src/frontend/src/components/MonitoringPanel.vue` and is now the **admin-gated "Health" tab** of the consolidated **Operations** view (`/operations?tab=health`); `/monitoring` redirects there. The tab is gated on `authStore.role === 'admin'` (button `v-if` + panel `v-if`), and non-admin `?tab=health` deep links are coerced to the default tab. `views/Monitoring.vue` was deleted. The **backend monitoring service, endpoints, and models below are unchanged.** See [operating-room.md](operating-room.md) (now "Operations").

| UI Location | API Endpoint | Purpose |
|-------------|--------------|---------|
| **NavBar "Operations" link** | - | Navigate to `/operations`; the **Health** tab (admin-only) shows fleet monitoring |
| **Operations → Health tab** (`MonitoringPanel.vue`) | `GET /api/monitoring/status` | View fleet-wide health |
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

## Frontend Layer (#1109: Health tab of Operations)

### Route (`src/frontend/src/router/index.js:39-41`)

The standalone page is gone — `/monitoring` is now a query-preserving redirect:

```javascript
{
  path: '/monitoring',
  redirect: { path: '/operations', query: { tab: 'health' } }
}
```

### Navigation (`src/frontend/src/components/NavBar.vue`)

No dedicated Health link. The single **Operations** link (with the unified
operator-queue + notifications badge) leads to `/operations`; the Health tab
button inside `Operations.vue` is admin-gated (`authStore.role === 'admin'`),
and non-admin `?tab=health` deep links are coerced to the default tab.

### Monitoring Panel (`src/frontend/src/components/MonitoringPanel.vue`, 406 lines)

Tab-embeddable content extracted from the deleted `views/Monitoring.vue`
(#1109); rendered by `Operations.vue` via `v-if="activeTab === 'health' && isAdmin"`
so its 30s auto-refresh polling tears down on tab-leave.

**Template Structure**:
- Header strip with monitoring status badge ("Monitoring Active/Disabled"), auto-refresh toggle + countdown, Refresh and "Check All" buttons
- Summary cards (total, healthy, degraded, unhealthy, critical counts)
- Active alerts section (collapsible, shows recent alerts)
- Agent health grid with status indicators and issues

**Key State Variables** (same shape as the old view): `monitoringStore`,
`statusFilter`, `triggeringCheck`, `checkingAgent`, `autoRefreshEnabled`,
`refreshCountdown`.

**Key Methods**:

| Method | Description |
|--------|-------------|
| `refreshAll()` | Fetch status + alerts |
| `triggerFleetCheck()` | POST to /check-all |
| `triggerAgentCheck(name)` | POST to /agents/{name}/check |
| `viewAgentDetail(name)` | Navigate to agent page |
| `startAutoRefresh()` | 30s polling interval (torn down on unmount/tab-leave) |

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
| `check_network_health(agent_name, timeout)` | 170-269 | HTTP /health endpoint, latency; classifies transport exceptions for circuit-breaker (#474 Layer 2) |
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

**Network Health Check** (`monitoring_service.py:175-281`, refined by #474):

Mirrors `AgentClient._request()`'s failure classification — single source of truth lives in `services/agent_client.py` (`CIRCUIT_FAILURE_EXCEPTIONS`, `TRANSIENT_TRANSPORT_EXCEPTIONS`). The probe records circuit-breaker success on any HTTP response, fails the circuit only on `ConnectError`/`ConnectTimeout`, and surfaces transient transport errors as `reachable=False` without touching the circuit.

```python
async def check_network_health(agent_name: str, timeout: float = 10.0) -> NetworkHealthCheck:
    from services.agent_client import (
        CircuitState, CIRCUIT_FAILURE_EXCEPTIONS, TRANSIENT_TRANSPORT_EXCEPTIONS,
    )
    circuit = CircuitState(agent_name)
    url = f"http://agent-{agent_name}:8000/health"
    start = time.monotonic()

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url)
            # Any 100..599 response proves TCP/HTTP reachability — circuit
            # records success unconditionally (symmetric with _request()).
            circuit.record_success()
            return NetworkHealthCheck(
                reachable=True, status_code=response.status_code,
                latency_ms=(time.monotonic() - start) * 1000,
            )

    except asyncio.CancelledError:
        raise  # Don't swallow shutdown cancellation.

    except CIRCUIT_FAILURE_EXCEPTIONS as e:
        # ConnectError / ConnectTimeout — real unreachability.
        circuit.record_failure()
        return NetworkHealthCheck(reachable=False, error=str(e)[:200])

    except TRANSIENT_TRANSPORT_EXCEPTIONS as e:
        # Read/write timeout, pool exhaustion, mid-write broken pipe / reset,
        # garbled framing. NOT a circuit signal.
        return NetworkHealthCheck(reachable=False, error=str(e)[:200])
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

- **Agent Heartbeat Liveness** (`agent-heartbeat-liveness.md`) - Additive push-based 5s liveness layer (RELIABILITY-004 / #307); this 30s loop stays authoritative, the heartbeat layer annotates `GET /api/monitoring/status` and alerts on missed beats
- **Agent Lifecycle** (`agent-lifecycle.md`) - Monitoring state when agents start/stop
- **Notification System** (`notifications.md`) - Alert delivery via NOTIF-001
- **WebSocket Broadcasting** (`websocket-events.md`) - Real-time event delivery
- **MCP Orchestration** (`mcp-orchestration.md`) - Monitoring tools via MCP

---

## Revision History

| Date | Changes |
|------|---------|
| 2026-07-06 | **#1463 — `/health` timeout is circuit-neutral while the agent is busy** (`services/monitoring_service.py:check_network_health()`). A long CPU-bound execution starves the agent-server event loop enough that `/health` can't answer within the 10s probe timeout, while the run itself completes SUCCESS — "busy", not "wedged". The `httpx.TimeoutException` handler now consults `_agent_has_active_executions()` (CapacityManager `get_status().is_busy`, a pure backend/Redis lookup that never touches the agent) and **skips `circuit.record_failure()` when a slot is active**, still returning `reachable=False` with `error="HTTP timeout (agent busy)"` for the aggregate rollup. Fail-open: a slot-lookup error treats the agent as idle (preserves the pre-#1463 liveness contract; the breaker is itself fail-open on Redis-down). Fixes the breaker opening on every long scheduled run and fast-failing every other trigger until the ~1h dormant probe recovered (surfaced by #1121 monitoring auto-resume). Only the timeout leg changed — ConnectError/ReadError/etc. classification is untouched. Regression test: `tests/unit/test_monitoring_health_check_classification.py`. Companion per-worker duplication bug filed separately. |
| 2026-06-02 | **#1020 — richer agent `/health` signal** (commit 122d07ed). Agent-server `/health` (`docker/base-image/agent_server/routers/info.py:98-123`) now emits named, contractual fields beyond `{status}`: `active_tasks` (concurrent executions across `/api/chat` + `/api/task`), `last_task_at` (ISO), `consecutive_failures` (reset on success, incremented on failure). Counters live on `AgentState` (`state.py:68-90`, `record_task_start`/`record_task_finish`, lock-guarded) and are wired at both execution chokepoints in `routers/chat.py` (47/58/60 for `/api/chat`, 133/148/150 for `/api/task`). `mailbox_depth` is **deliberately absent** — there is no agent-side mailbox until the actor model (#945); the backend derives queue depth from `CapacityManager`. Backend `check_business_health()` reads `consecutive_failures`/`last_task_at` into `BusinessHealthCheck` (`monitoring_service.py:408-409, 422-423, 486-487`; `db_models.py:854-855`) with a `None` **graceful default for pre-#1020 agent images** so older agents don't break fleet health. `consecutive_failures` feeds the dispatch circuit breaker (#526) and fleet-health (#307). Additive only — existing keys unchanged. New "Richer Agent `/health` Signal (#1020)" section added above; #474/#798 circuit-breaker classification untouched. |
| 2026-05-30 | **RELIABILITY-004 / #307 — additive push-heartbeat liveness layer** (`heartbeat_service.py`, new). `get_fleet_status()` (`routers/monitoring.py:178`) now merges five `heartbeat_*` fields onto each `AgentHealthSummary` via one batched `heartbeat_status_bulk()` Redis round-trip, inside the existing try/except so a Redis blip degrades rather than 500s. This is a **passive annotation** — it does **not** change `status`; this 30s loop stays authoritative for aggregate health. Heartbeat loss is surfaced actively by the separate watch loop via `monitoring_alerts.alert_heartbeat_lost`/`alert_heartbeat_recovered`. Full flow: [agent-heartbeat-liveness.md](agent-heartbeat-liveness.md). |
| 2026-05-18 | **#873 — pipe-drop responses classified as HTTP 502 on agent server** (`docker/base-image/agent_server/services/headless_executor.py:856-874`). `BrokenPipeError`/`ConnectionResetError` in `execute_headless` now raise 502 instead of 500, avoiding collision with the 503 auto-switch path in `task_execution_service.py`. From the monitoring side, 502 and 500 are equivalent: both are HTTP responses that call `circuit.record_success()` in `check_network_health()` (agent is TCP-reachable) and are then flagged `UNHEALTHY` by the `status_code >= 500` branch in `aggregate_health()`. No change to `monitoring_service.py` was required. |
| 2026-05-13 | **#474 Layer 2 — `/health` probe exception classification split** (`services/monitoring_service.py:check_network_health()`, commit d53a2d6b). Layered on top of #798 (below). Two new exception handlers inserted BEFORE the shared `TRANSIENT_TRANSPORT_EXCEPTIONS` handler so they win Python's first-match: (1) `BrokenPipeError` / `ConnectionResetError` → client-side transport drop (upstream MCP-sync cancellation cascading into a pooled keepalive socket); returns `reachable=False` with `error="Connection dropped: <ExceptionName>"` but does **NOT** call `circuit.record_failure()` — the agent's health was never observed, so it must not trip the breaker on healthy agents. (2) `httpx.ReadError` / `httpx.WriteError` / `httpx.RemoteProtocolError` → genuine agent liveness signals on a `/health` probe (partial write then socket drop = event-loop wedge, OOM mid-write, segfault); calls `circuit.record_failure()` and returns `reachable=False` with `error="HTTP transport error on /health: <ExceptionName>"`. This is the documented `/health`-specific divergence from `AgentClient._request()` — on `/api/*` paths #798's tuple-based handler keeps the same exceptions circuit-neutral because the cause space is broader. Regression test: `tests/unit/test_monitoring_health_check_classification.py`. |
| 2026-05-12 | **Circuit-breaker classification mirrored on the /health probe (#474)**: `check_network_health()` now lazy-imports `CIRCUIT_FAILURE_EXCEPTIONS` / `TRANSIENT_TRANSPORT_EXCEPTIONS` from `services/agent_client.py` and applies the same rule as inline `/api/*` requests. Any HTTP response (200..599) records circuit success so stale failure counters clear as soon as the agent answers. Only `ConnectError`/`ConnectTimeout` increment the failure counter; read-timeouts, pool exhaustion, mid-write broken-pipe/reset, and garbled framing surface as `reachable=False` but don't poison the circuit. `aggregate_health()` adds an explicit `network.status_code >= 500 → UNHEALTHY` branch (`monitoring_service.py:419`) so a wedged-but-listening agent isn't silently HEALTHY under the new rule. |
| 2026-03-03 | **SUB-002 credential monitoring removal**: Removed credential file checks from `check_business_health()`, `aggregate_health()`, and `perform_health_check()`. Removed `alert_subscription_credentials_missing()` from `monitoring_alerts.py`. Removed auto-remediation via `inject_subscription_on_start()`. `credential_status` field deprecated (always `None`). Tokens now injected as container env vars. |
| 2026-02-23 | **Admin-only access restriction**: NavBar "Health" link now requires admin (`v-if="isAdmin"` at NavBar.vue:26), route meta updated to `requiresAdmin: true` (router/index.js:39). Frontend Layer section already documented this correctly. |
| 2026-02-23 | Initial documentation for MON-001 implementation |
