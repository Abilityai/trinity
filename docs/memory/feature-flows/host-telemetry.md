# Feature: Host Telemetry

## Overview

Real-time infrastructure monitoring displaying host CPU, memory, and disk usage directly in the Dashboard header, with optional aggregate container statistics for running agents.

## User Stories

### OBS-011: View Host CPU/Memory/Disk
**As a** platform operator
**I want to** see host CPU, memory, and disk usage
**So that** I can monitor infrastructure health at a glance

### OBS-012: View Aggregate Container Stats
**As a** platform operator
**I want to** see aggregate container CPU and memory usage
**So that** I can understand resource distribution across running agents

## Entry Points

- **UI**: `src/frontend/src/components/HostTelemetry.vue` - Inline component in Dashboard header
- **Dashboard Integration**: `src/frontend/src/views/Dashboard.vue:59` - `<HostTelemetry />` component
- **API - Host Stats**: `GET /api/telemetry/host`
- **API - Container Stats**: `GET /api/telemetry/containers`

---

## Frontend Layer

### Components

#### HostTelemetry.vue (194 lines)
`src/frontend/src/components/HostTelemetry.vue`

| Section | Lines | Description |
|---------|-------|-------------|
| State Setup | 11-18 | `cpuHistory`, `memHistory` arrays, `hostStats` ref |
| History Init | 22-25 | Initialize 60-point rolling arrays with nulls |
| Fetch Stats | 27-56 | Poll `/api/telemetry/host`, update history arrays |
| Format Helpers | 58-73 | `formatPercent()`, `formatMemory()`, `getColorClass()` |
| Polling Setup | 75-83 | `onMounted`: init history, start 5s interval |
| Template | 86-136 | CPU/Mem sparklines + Disk progress bar |

**Visual Components:**
- CPU: Blue sparkline chart + percentage
- Memory: Purple sparkline chart + used/total GB
- Disk: Green progress bar + percentage

**Color Thresholds:**
```javascript
// getColorClass(percent) at line 67-73
< 50%  -> green
50-75% -> yellow
75-90% -> orange
>= 90% -> red
```

#### SparklineChart.vue (148 lines)
`src/frontend/src/components/SparklineChart.vue`

Uses uPlot library for lightweight SVG charts:
- Props: `data` (array), `color` (CSS), `yMax` (100), `width` (60px), `height` (20px)
- Updates reactively on data changes
- No axes, legend, or cursor (minimal footprint)

### State Management

No Pinia store - component manages its own state:
- `hostStats`: Current metrics snapshot
- `cpuHistory`: Rolling 60-point array (5 min at 5s intervals)
- `memHistory`: Rolling 60-point array

### API Calls

```javascript
// HostTelemetry.vue:30-38
const token = localStorage.getItem('token')
const headers = { Authorization: `Bearer ${token}` }
const hostRes = await fetch(`${API_BASE}/api/telemetry/host`, {
  headers,
  signal: AbortSignal.timeout(3000)
})
```

**Polling:** Every 5 seconds (`setInterval` at line 78)

---

## Backend Layer

### Router
`src/backend/routers/telemetry.py`

Registered in main.py:
```python
# main.py:47
from routers.telemetry import router as telemetry_router

# main.py:263
app.include_router(telemetry_router)
```

### Endpoints

#### GET /api/telemetry/host
`src/backend/routers/telemetry.py:29-66`

Returns host system statistics via psutil.

**Requires authentication** (Bearer token, SEC-180).

**Response Schema:**
```json
{
  "cpu": {
    "percent": 45.2,
    "count": 8
  },
  "memory": {
    "percent": 62.3,
    "used_gb": 12.5,
    "total_gb": 20.0
  },
  "disk": {
    "percent": 54.1,
    "used_gb": 108.2,
    "total_gb": 200.0
  },
  "timestamp": "2026-01-13T12:00:00.000000Z"
}
```

**Implementation Details:**
```python
# Line 26: Prime CPU counter on module load
psutil.cpu_percent(interval=None)

# Line 39: Non-blocking CPU read
cpu_percent = psutil.cpu_percent(interval=None)

# Line 43-46: Memory and disk via psutil
mem = psutil.virtual_memory()
disk = psutil.disk_usage('/')
```

#### GET /api/telemetry/containers
`src/backend/routers/telemetry.py` — `get_container_stats`

Returns aggregate statistics across all running agent containers.

**Requires authentication** (Bearer token, SEC-180).

**Non-blocking — stale-while-revalidate cache (#1096):** the request path
NEVER awaits the Docker daemon. `container.stats(stream=False)` costs ~1-2s
per container, so the original synchronous endpoint took
`ceil(N/pool) * ~1.5s` (~6s at 11 agents), pinning a uvicorn worker for its
whole duration and starving the rest of the UI on low-worker prod (2 workers).
The handler now reads a short-TTL, background-refreshed cache and returns
immediately; the expensive Docker work runs out-of-band.

**Response Schema:**
```json
{
  "running_count": 3,
  "total_cpu_percent": 15.2,
  "total_memory_mb": 1024.5,
  "containers": [
    {"name": "agent-a", "cpu": 8.1, "memory_mb": 512.2},
    {"name": "agent-b", "cpu": 4.5, "memory_mb": 256.1},
    {"name": "agent-c", "cpu": 2.6, "memory_mb": 256.2}
  ],
  "timestamp": "2026-01-13T12:00:00.000000Z",
  "cached": true,
  "stale": false,
  "cache_age_seconds": 3.2
}
```

The original 5 fields are unchanged; `cached`, `stale`, and
`cache_age_seconds` (float | null) are **additive** (the endpoint has no
`response_model`, so existing consumers ignore the extra keys).

**Caching behaviour:**

| Cache state | Response | Refresh |
|-------------|----------|---------|
| Fresh (`age < TTL`) | live cached payload, `stale:false` | none |
| Stale (`age ≥ TTL`) | last payload, `stale:true` | scheduled in background |
| Cold (no data yet) | empty payload (`running_count:0`, `[]`), `cached:false, stale:true` | scheduled in background |

Cold state only occurs on a worker's first poll after (re)start and
self-heals within one TTL once the background refresh completes.

**Performance Optimization:**
```python
# Pool sized to the fleet (default 16, env TELEMETRY_DOCKER_POOL_SIZE,
# clamped 1..64) — the refresh now runs off the request path, so a cold
# refresh is ~one Docker-sample window instead of ceil(N/4) windows.
_docker_executor = ThreadPoolExecutor(max_workers=_POOL_SIZE, ...)

# TTL of a "fresh" payload (default 10s, env TELEMETRY_CONTAINER_STATS_TTL).
_CACHE_TTL = ...

# Single-flight: one background refresh per process at a time, holding a
# strong task ref so it can't be GC'd mid-run (_schedule_refresh).
# _compute_container_stats() offloads the agent listing AND each container's
# stats to the executor and aggregates via asyncio.gather(return_exceptions=True).
```

Cache state is per-process (each uvicorn worker keeps its own); a failed
refresh leaves the previous payload intact and never surfaces as a request
error. Config env vars are defensively parsed — a bad value falls back to the
default and can never crash the router at import.

**Single Container Stats Helper:**
```python
# Line 69-109: _get_single_container_stats_sync(agent_name)
# - Gets container by name
# - Reads stats (one-shot, ~1-2s per container)
# - Calculates CPU % from deltas
# - Calculates memory (usage - cache)
```

### Business Logic

1. **CPU Calculation** (lines 82-89):
   ```python
   cpu_delta = stats['cpu_stats']['cpu_usage']['total_usage'] - \
              stats['precpu_stats']['cpu_usage']['total_usage']
   system_delta = stats['cpu_stats']['system_cpu_usage'] - \
                 stats['precpu_stats']['system_cpu_usage']
   cpu_percent = (cpu_delta / system_delta) * num_cpus * 100.0
   ```

2. **Memory Calculation** (lines 92-95):
   ```python
   memory_usage = stats['memory_stats'].get('usage', 0)
   memory_cache = stats['memory_stats'].get('stats', {}).get('cache', 0)
   memory_used = memory_usage - memory_cache  # Exclude cache for accuracy
   ```

### Docker Integration

Uses `services/docker_service.py`:
```python
# Line 16: Import
from services.docker_service import docker_client, list_all_agents_fast
```

**list_all_agents_fast()** (docker_service.py:101-159):
- Extracts data from container labels only
- Avoids slow operations: `container.attrs`, `container.image`, `container.stats()`
- Performance: ~50ms for 10 agents vs ~2-3s with full metadata

---

## Data Flow

```
Dashboard.vue
    |
    +-- <HostTelemetry /> (line 59)
           |
           +-- onMounted() -> initHistory() + fetchStats()
           |
           +-- setInterval(fetchStats, 5000)
                   |
                   +-- GET /api/telemetry/host
                           |
                           +-- psutil.cpu_percent()
                           +-- psutil.virtual_memory()
                           +-- psutil.disk_usage('/')
                           |
                           +-- Return JSON response
                   |
                   +-- Update cpuHistory[], memHistory[]
                   |
                   +-- SparklineChart re-renders
```

---

## Side Effects

- **No WebSocket**: Pure polling model
- **No Audit Logging**: Telemetry endpoints are read-only, high-frequency
- **No Database**: Metrics are computed fresh each request
- **Authentication Required**: Bearer token required (SEC-180, pentest finding 3.2.3)

---

## Error Handling

| Error Case | HTTP Status | Handling |
|------------|-------------|----------|
| psutil error (`/host`) | 500 | `Error getting host stats: {message}` |
| Docker unavailable (`docker_client` falsy) | 503 | `Docker not available` (`/containers`) |
| Container stats error (per container) | - | Returns `{error: message}` for that container, continues |
| Background refresh fails (`/containers`, #1096) | 200 | Logged at WARNING; previous (or empty) cached payload served — no 500 |
| Fetch timeout | - | Frontend: 3s timeout, silently fails |

**Frontend Error Handling:**
```javascript
// HostTelemetry.vue:32
signal: AbortSignal.timeout(3000)

// Lines 52-55
} catch (e) {
  loading.value = false
  error.value = e.message
}
```

---

## Security Considerations

- **Authentication Required**: Bearer token (JWT or MCP API key) required since SEC-180
- **Read-Only**: No mutation endpoints
- **Rate Limiting**: Not implemented (polling interval is client-controlled)
- **No PII**: Only system metrics exposed

---

## Related Flows

- **Upstream**: Dashboard page load triggers HostTelemetry mount
- **Downstream**: None (display-only)
- **Related**: [opentelemetry-integration.md](opentelemetry-integration.md) - OTel metrics from agents
- **Related**: [agent-logs-telemetry.md](agent-logs-telemetry.md) - Per-agent container logs

---

## Testing

### Prerequisites
- Trinity backend running (`./scripts/deploy/start.sh`)
- At least one agent created (for container stats)

### Test Steps

#### Test 1: Host Stats Display
| Step | Action | Expected | Verify |
|------|--------|----------|--------|
| 1 | Navigate to Dashboard (`/`) | Page loads with header | Header visible |
| 2 | Look at right side of stats bar | See "CPU", "Mem", "Disk" stats | Sparklines visible |
| 3 | Wait 5 seconds | Values update | Numbers change |
| 4 | Check color coding | Values <50% are green | Color matches threshold |

#### Test 2: API Direct Access (Requires Auth)
| Step | Action | Expected | Verify |
|------|--------|----------|--------|
| 1 | `curl http://localhost:8000/api/telemetry/host` (no auth) | 401 Unauthorized | Authentication enforced |
| 2 | `curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/telemetry/host` | JSON response | Contains cpu, memory, disk |
| 3 | `curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/telemetry/containers` | JSON response | Contains running_count, containers array |

#### Test 3: Container Stats (OBS-012)
| Step | Action | Expected | Verify |
|------|--------|----------|--------|
| 1 | Start 2+ agents | Containers running | `docker ps` shows agents |
| 2 | Call `/api/telemetry/containers` | Aggregate stats | `running_count >= 2` |
| 3 | Check containers array | Per-agent breakdown | Each has cpu, memory_mb |

### Edge Cases

| Scenario | Expected Behavior |
|----------|-------------------|
| No running agents | `running_count: 0`, empty `containers` array |
| Docker unavailable | 503 error on `/containers`, host stats still work |
| Cold cache (first poll after restart) | Instant empty payload, `stale:true`; real data on the next poll (#1096) |
| Background refresh fails | Previous payload served (`stale:true`); warning logged; no 500 (#1096) |
| High CPU load (>90%) | Red color class applied |
| Disk nearly full (>90%) | Red progress bar |

### Cleanup
No cleanup required - read-only endpoints.

### Status
- Host Telemetry Display: Working
- Container Aggregate Stats: Working (API only, no UI integration); non-blocking SWR cache since #1096

---

## Revision History

| Date | Change |
|------|--------|
| 2026-06-08 | #1096: `/containers` made non-blocking via short-TTL background-refreshed cache (stale-while-revalidate); added `cached`/`stale`/`cache_age_seconds` fields |
| 2026-03-27 | SEC-180: Authentication added to all telemetry endpoints |
| 2026-01-13 | Initial documentation |
