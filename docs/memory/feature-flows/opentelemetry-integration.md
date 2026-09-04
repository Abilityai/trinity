# Feature: OpenTelemetry Integration

## Overview

Enables Claude Code agents to export metrics to an OpenTelemetry Collector for external observability tools like Prometheus and Grafana. Leverages Claude Code's built-in OTel SDK to capture cost, token usage, and productivity metrics.

## User Story

As a platform operator, I want agents to export standardized metrics so that I can monitor cost, token usage, and productivity across all agents using industry-standard observability tools.

## Entry Points

- **Configuration**: `.env` file with `OTEL_ENABLED=1`
- **API**: Agent creation at `POST /api/agents` injects OTel environment variables
- **Metrics**: Prometheus endpoint at `http://localhost:8889/metrics`

---

## Architecture

```
+------------------+     +------------------+     +------------------+
|   Agent 1        |     |   Agent 2        |     |   Agent N        |
|  Claude Code     |     |  Claude Code     |     |  Claude Code     |
|  + OTel SDK      |     |  + OTel SDK      |     |  + OTel SDK      |
+--------+---------+     +--------+---------+     +--------+---------+
         |                        |                        |
         +------------------------+------------------------+
                                  | OTLP (gRPC :4317)
                                  v
                    +----------------------------+
                    |   OTEL Collector           |
                    |   trinity-otel-collector   |
                    |   :4317 (gRPC in)          |
                    |   :4318 (HTTP in)          |
                    |   :8889 (Prometheus out)   |
                    +-------------+--------------+
                                  | Prometheus format
                                  v
                    +----------------------------+
                    |   Prometheus / Grafana     |
                    |   (External - Phase 3)     |
                    +----------------------------+
```

---

## Configuration Layer

### Environment Variables

**File**: `.env.example` (lines 95-112)

```bash
# Enable OpenTelemetry metrics export from Claude Code agents
# Set to 1 to enable, 0 to disable (default: enabled)
OTEL_ENABLED=1

# OTEL Collector endpoint (only used when OTEL_ENABLED=1)
# Default points to Docker service name for in-network access
OTEL_COLLECTOR_ENDPOINT=http://trinity-otel-collector:4317

# Exporter configuration (usually no need to change)
OTEL_METRICS_EXPORTER=otlp
OTEL_LOGS_EXPORTER=otlp
OTEL_EXPORTER_OTLP_PROTOCOL=grpc

# Metrics export interval in milliseconds (default: 60 seconds)
OTEL_METRIC_EXPORT_INTERVAL=60000
```

### Variable Mapping

| Backend Env Var | Agent Container Env Var | Default Value |
|-----------------|------------------------|---------------|
| `OTEL_ENABLED` | `CLAUDE_CODE_ENABLE_TELEMETRY` | `1` (enabled) |
| `OTEL_METRICS_EXPORTER` | `OTEL_METRICS_EXPORTER` | `otlp` |
| `OTEL_LOGS_EXPORTER` | `OTEL_LOGS_EXPORTER` | `otlp` |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `OTEL_EXPORTER_OTLP_PROTOCOL` | `grpc` |
| `OTEL_COLLECTOR_ENDPOINT` | `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://trinity-otel-collector:4317` |
| `OTEL_METRIC_EXPORT_INTERVAL` | `OTEL_METRIC_EXPORT_INTERVAL` | `60000` |

---

## Backend Layer

### Agent Creation - Environment Injection

**File**: `src/backend/services/agent_service/crud.py` (lines 308-316)

```python
# OpenTelemetry Configuration (enabled by default)
# Claude Code has built-in OTel support - these vars enable metrics export
if os.getenv('OTEL_ENABLED', '1') == '1':
    env_vars['CLAUDE_CODE_ENABLE_TELEMETRY'] = '1'
    env_vars['OTEL_METRICS_EXPORTER'] = os.getenv('OTEL_METRICS_EXPORTER', 'otlp')
    env_vars['OTEL_LOGS_EXPORTER'] = os.getenv('OTEL_LOGS_EXPORTER', 'otlp')
    env_vars['OTEL_EXPORTER_OTLP_PROTOCOL'] = os.getenv('OTEL_EXPORTER_OTLP_PROTOCOL', 'grpc')
    env_vars['OTEL_EXPORTER_OTLP_ENDPOINT'] = os.getenv('OTEL_COLLECTOR_ENDPOINT', 'http://trinity-otel-collector:4317')
    env_vars['OTEL_METRIC_EXPORT_INTERVAL'] = os.getenv('OTEL_METRIC_EXPORT_INTERVAL', '60000')
```

### Business Logic

1. Check if `OTEL_ENABLED=1` in backend environment
2. If enabled, inject Claude Code telemetry environment variables into agent container
3. Agent container's Claude Code process reads these variables at startup
4. Claude Code SDK exports metrics to specified OTLP endpoint

---

## Infrastructure Layer

### OTEL Collector Service

**File**: `docker-compose.yml` (lines 207-228)

**CRITICAL**: Must use `contrib` image version 0.120.0+ for `deltatocumulative` processor. Claude Code exports delta metrics, but Prometheus requires cumulative.

```yaml
# OpenTelemetry Collector - receives metrics from Claude Code agents
# Using contrib image for deltatocumulative processor (required for Prometheus export)
otel-collector:
  image: otel/opentelemetry-collector-contrib:0.120.0
  container_name: trinity-otel-collector
  restart: unless-stopped
  ports:
    - "4317:4317"   # gRPC receiver (OTLP)
    - "4318:4318"   # HTTP receiver (OTLP)
    - "8889:8889"   # Prometheus metrics exporter
    - "13133:13133" # Health check endpoint
  volumes:
    - ./config/otel-collector.yaml:/etc/otelcol-contrib/config.yaml:ro  # Note: contrib path
  networks:
    - trinity-network
  labels:
    - "trinity.platform=infrastructure"
    - "trinity.service=otel-collector"
  security_opt:
    - no-new-privileges:true
  cap_drop:
    - ALL
```

### Collector Configuration

**File**: `config/otel-collector.yaml`

**Key Processors**:
- `batch` - Batches metrics for efficient export
- `resource` - Adds `platform: trinity` metadata
- `deltatocumulative` - **CRITICAL** - Converts delta to cumulative temporality (required for Prometheus)

```yaml
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: "0.0.0.0:4317"
      http:
        endpoint: "0.0.0.0:4318"

processors:
  batch:
    timeout: 10s
    send_batch_size: 1024

  resource:
    attributes:
      - key: platform
        value: trinity
        action: upsert

  # CRITICAL: Convert delta to cumulative for Prometheus compatibility
  deltatocumulative:
    max_stale: 10m
    max_streams: 10000

exporters:
  prometheus:
    endpoint: "0.0.0.0:8889"
    namespace: trinity
    enable_open_metrics: true
    resource_to_telemetry_conversion:
      enabled: true
    # NOTE: Don't use const_labels with resource_to_telemetry_conversion
    # to avoid duplicate label conflicts

  debug:
    verbosity: basic
    sampling_initial: 5
    sampling_thereafter: 200

service:
  extensions: [health_check]
  pipelines:
    metrics:
      receivers: [otlp]
      processors: [batch, resource, deltatocumulative]  # deltatocumulative REQUIRED
      exporters: [prometheus, debug]
    logs:
      receivers: [otlp]
      processors: [batch]
      exporters: [debug]
    traces:
      receivers: [otlp]
      processors: [batch]
      exporters: [debug]
  telemetry:
    logs:
      level: info
```

---

## Metrics Reference

### Claude Code Metrics (Counter type)

| Metric Name | Description | Labels |
|-------------|-------------|--------|
| `trinity_claude_code_cost_usage_USD_total` | Cost per API call in USD | `model` |
| `trinity_claude_code_token_usage_tokens_total` | Token consumption | `type` (input, output, cacheCreation, cacheRead), `model` |
| `trinity_claude_code_lines_of_code_count_total` | Lines added/removed | `type` (added, removed) |
| `trinity_claude_code_session_count_total` | Sessions started | `session_id`, `terminal_type`, `app_version` |
| `trinity_claude_code_active_time_total_seconds_total` | Active usage duration | - |
| `trinity_claude_code_pull_request_count_total` | PRs created | - |
| `trinity_claude_code_commit_count_total` | Commits created | - |

### Sample Prometheus Output

```
# HELP trinity_claude_code_cost_usage_USD_total Cost per model
# TYPE trinity_claude_code_cost_usage_USD_total counter
trinity_claude_code_cost_usage_USD_total{model="claude-sonnet-4-20250514",platform="trinity"} 0.0234

# HELP trinity_claude_code_token_usage_tokens_total Token usage by type
# TYPE trinity_claude_code_token_usage_tokens_total counter
trinity_claude_code_token_usage_tokens_total{model="claude-sonnet-4-20250514",platform="trinity",type="input"} 1523
trinity_claude_code_token_usage_tokens_total{model="claude-sonnet-4-20250514",platform="trinity",type="output"} 892
trinity_claude_code_token_usage_tokens_total{model="claude-sonnet-4-20250514",platform="trinity",type="cacheRead"} 45678

# HELP trinity_claude_code_lines_of_code_count_total Lines of code
# TYPE trinity_claude_code_lines_of_code_count_total counter
trinity_claude_code_lines_of_code_count_total{platform="trinity",type="added"} 42
trinity_claude_code_lines_of_code_count_total{platform="trinity",type="removed"} 15
```

---

## Network Configuration

Agents connect to the collector via Docker's internal network:

- **Container name**: `trinity-otel-collector`
- **Internal gRPC endpoint**: `http://trinity-otel-collector:4317`
- **Internal HTTP endpoint**: `http://trinity-otel-collector:4318`
- **External Prometheus**: `http://localhost:8889/metrics`

The `trinity-network` bridge network allows all agent containers to reach the collector by service name.

---

## Error Handling

| Error Case | Behavior | Recovery |
|------------|----------|----------|
| Collector not running | Claude Code logs warning, continues working | Metrics lost until collector restarts |
| Network unreachable | Export fails silently | Agent continues, metrics buffered briefly |
| Invalid endpoint | No metrics exported | Check `OTEL_EXPORTER_OTLP_ENDPOINT` value |
| OTEL disabled | No env vars injected | Enable with `OTEL_ENABLED=1` |

Claude Code's OTel integration is non-blocking - agent operations continue even if metric export fails.

---

## Security Considerations

1. **No credentials in metrics**: Only counts, durations, and model names exported
2. **Internal network only**: Collector runs on Docker bridge network
3. **No authentication**: Prometheus endpoint at :8889 has no auth (Phase 3 would add auth proxy)
4. **Minimal privileges**: Collector runs with `no-new-privileges` and dropped capabilities

---

## Testing

### Prerequisites

1. Trinity platform running (`./scripts/deploy/start.sh`)
2. `OTEL_ENABLED=1` in `.env` file
3. Backend restarted after enabling OTel

### Test 1: Verify Environment Variables

**Action**: Create a new agent and check its environment

```bash
# Create test agent
curl -X POST http://localhost:8000/api/agents \
  -H "Content-Type: application/json" \
  -d '{"name": "otel-test", "type": "test"}'

# Wait for container to start, then check env vars
docker exec agent-otel-test env | grep -E "(OTEL|CLAUDE_CODE_ENABLE)"
```

**Expected Output**:
```
CLAUDE_CODE_ENABLE_TELEMETRY=1
OTEL_METRICS_EXPORTER=otlp
OTEL_LOGS_EXPORTER=otlp
OTEL_EXPORTER_OTLP_PROTOCOL=grpc
OTEL_EXPORTER_OTLP_ENDPOINT=http://trinity-otel-collector:4317
OTEL_METRIC_EXPORT_INTERVAL=60000
```

**Verify**: All 6 environment variables present with expected values

### Test 2: Collector Health Check

**Action**: Verify collector is receiving data

```bash
# Check collector is running
docker ps | grep otel-collector

# Check collector logs
docker logs trinity-otel-collector 2>&1 | tail -20
```

**Expected**: Container running, logs show "Everything is ready"

### Test 3: Prometheus Endpoint

**Action**: Query the Prometheus metrics endpoint

```bash
curl -s http://localhost:8889/metrics | grep trinity_claude_code
```

**Expected**: Metric lines starting with `trinity_claude_code_*` (may be empty if no agent activity yet)

### Test 4: End-to-End Metric Export

**Action**: Generate agent activity and verify metrics appear

1. Open Trinity UI at http://localhost:3000
2. Start an agent with OTel enabled
3. Send a chat message that triggers tool use (e.g., "List the files in the current directory")
4. Wait 60 seconds (default export interval)
5. Query Prometheus endpoint:

```bash
curl -s http://localhost:8889/metrics | grep -E "cost_usage|token_usage|lines_of_code"
```

**Expected**: Non-zero counters for cost and token usage:
```
trinity_claude_code_cost_usage_USD_total{model="claude-sonnet-4-20250514",platform="trinity"} 0.0123
trinity_claude_code_token_usage_tokens_total{model="...",type="input"} 1234
```

### Test 5: Multi-Agent Collaboration Metrics

**Action**: Verify metrics from agent-to-agent collaboration

1. Start two agents (e.g., `orchestrator` and `worker`)
2. From orchestrator, send a message that triggers collaboration (uses Trinity MCP)
3. Wait 60 seconds
4. Check that metrics from both agents appear in Prometheus

```bash
curl -s http://localhost:8889/metrics | grep trinity_claude_code | wc -l
```

**Expected**: Multiple metric lines (both agents contributing)

### Cleanup

```bash
# Stop and remove test agent
curl -X POST http://localhost:8000/api/agents/otel-test/stop
curl -X DELETE http://localhost:8000/api/agents/otel-test
```

### Status

| Test | Status |
|------|--------|
| Environment Variables | Verified 2025-12-20 |
| Collector Health | Verified 2025-12-20 |
| Prometheus Endpoint | Verified 2025-12-20 |
| End-to-End Export | Verified 2025-12-20 |
| Multi-Agent Metrics | Verified 2025-12-20 |

---

## Rollback

### Disable OTel

1. Set `OTEL_ENABLED=0` in `.env`
2. Restart backend: `docker-compose restart backend`
3. New agents will not have OTel environment variables

Existing agents keep their OTel config until restarted.

### Remove Collector

1. Stop collector: `docker-compose stop otel-collector`
2. Remove from `docker-compose.yml` (lines 207-228)
3. Agents will log export warnings but continue working

---

## Future Phases

### Phase 3: Prometheus/Grafana Stack

- Add `docker-compose.monitoring.yml` with Prometheus and Grafana services
- Pre-configured dashboards for cost, tokens, productivity
- See `docs/drafts/OTEL_INTEGRATION.md` sections 3.1-3.3

### Phase 4: Claude Code Hooks

- Inject `hooks.json` for custom validation gates
- Quality checks before task completion
- See `docs/drafts/OTEL_INTEGRATION.md` sections 4.1-4.2

---

## Related Flows

| Flow | Relationship |
|------|--------------|
| [agent-lifecycle.md](agent-lifecycle.md) | Upstream - OTel vars injected during container creation |
| [agent-chat.md](agent-chat.md) | Parallel - Chat generates metrics that OTel exports |
| [activity-stream.md](activity-stream.md) | Parallel - Existing real-time tracking (OTel is additive) |
| [persistent-chat-tracking.md](persistent-chat-tracking.md) | Parallel - Database stores similar metrics locally |

---

## Files Modified

| File | Change |
|------|--------|
| `src/backend/services/agent_service/crud.py` | OTel env var injection (lines 308-316) |
| `src/backend/services/system_agent_service.py` | OTel env var injection for system agents (lines 191-198) |
| `src/backend/routers/observability.py` | Metrics API endpoints (257 lines) |
| `src/backend/routers/ops.py` | Cost endpoint powered by OTel (lines 728-869) |
| `src/backend/main.py` | Added observability router import (line 41) and registration (line 289) |
| `docker-compose.yml` | Added otel-collector service (lines 207-228) |
| `config/otel-collector.yaml` | Collector configuration with health_check extension (73 lines) |
| `.env.example` | OTel environment variables (lines 95-112) |
| `docs/DEPLOYMENT.md` | Added OpenTelemetry section |
| `src/frontend/src/stores/observability.js` | Pinia store for observability state (268 lines) — **unused after Dashboard removal** |
| `src/frontend/src/components/ObservabilityPanel.vue` | Collapsible metrics panel — **removed from Dashboard 2026-04-18, file deleted in #2492** (unreferenced) |

---

## Frontend Layer (Phase 2.5: UI Integration)

### Backend API Endpoints

**File**: `src/backend/routers/observability.py`

#### GET /api/observability/metrics (lines 140-218)

**File**: `src/backend/routers/observability.py` (257 lines total)

Fetches and parses Prometheus metrics from the OTel Collector.

```python
@router.get("/metrics")
async def get_observability_metrics(
    current_user: User = Depends(get_current_user)
):
    """
    Get OpenTelemetry metrics from the OTEL Collector.
    Returns structured metrics data including:
    - Cost breakdown by model
    - Token usage by model and type
    - Productivity metrics (lines, sessions, commits, PRs)
    Returns {enabled: false} when OTel is not configured.
    """
```

**Business Logic** (lines 23-137 in `src/backend/routers/observability.py`):
1. `parse_prometheus_metrics()` - Parses Prometheus text format into structured dict
2. `calculate_totals()` - Aggregates metrics across all models

**Error Handling**:
- Returns `{enabled: false}` when `OTEL_ENABLED=0`
- Returns `{available: false, error: "..."}` on connection/timeout errors
- Non-blocking - dashboard continues to work if collector unavailable

#### GET /api/observability/status (lines 221-257)

Quick status check without full metrics parsing.

```python
@router.get("/status")
async def get_observability_status(
    current_user: User = Depends(get_current_user)
):
    """Quick check without full metrics parsing."""
    # Returns: enabled, collector_configured, collector_reachable
```

#### Response Format

```json
{
  "enabled": true,
  "available": true,
  "metrics": {
    "cost_by_model": {"claude-sonnet": 0.0234, "claude-haiku": 0.0012},
    "tokens_by_model": {
      "claude-sonnet": {"input": 1523, "output": 892, "cacheRead": 45678}
    },
    "lines_of_code": {"added": 42, "removed": 15},
    "sessions": 5,
    "active_time_seconds": 3600,
    "commits": 3,
    "pull_requests": 1
  },
  "totals": {
    "total_cost": 0.0246,
    "total_tokens": 48093,
    "tokens_by_type": {"input": 1523, "output": 892, "cacheRead": 45678},
    "total_lines": 57,
    "sessions": 5,
    "active_time_seconds": 3600,
    "commits": 3,
    "pull_requests": 1
  }
}
```

#### Router Registration

**File**: `src/backend/main.py`
- Import: line 41
- Registration: line 289

```python
from routers.observability import router as observability_router
# ...
app.include_router(observability_router)
```

---

### Frontend State Management

**File**: `src/frontend/src/stores/observability.js` (268 lines)

#### State (lines 5-39)

```javascript
export const useObservabilityStore = defineStore('observability', {
  state: () => ({
    enabled: false,        // OTEL_ENABLED in backend
    available: false,      // Collector reachable
    error: null,           // Error message if any
    loading: false,        // Fetch in progress
    lastUpdated: null,     // Timestamp of last fetch
    metrics: {
      cost_by_model: {},
      tokens_by_model: {},
      lines_of_code: {},
      sessions: 0,
      active_time_seconds: 0,
      commits: 0,
      pull_requests: 0
    },
    totals: { /* aggregated values */ },
    pollInterval: null,
    pollIntervalMs: 60000  // 60 seconds
  })
})
```

#### Getters (lines 41-148)

| Getter | Purpose |
|--------|---------|
| `isOperational` | `enabled && available` - OTel fully working |
| `formattedTotalCost` | Currency format: `$0.0234` |
| `formattedTotalTokens` | K/M suffix: `1.5M`, `234K` |
| `formattedActiveTime` | Duration: `1h 30m` |
| `costBreakdown` | Sorted array for cost-by-model display |
| `tokensByType` | Aggregated by type (input, output, cache) |
| `linesBreakdown` | Added/removed breakdown |
| `hasData` | `total_cost > 0 || total_tokens > 0 || sessions > 0` |

#### Actions (lines 151-267)

| Action | Purpose |
|--------|---------|
| `fetchMetrics()` | GET /api/observability/metrics |
| `fetchStatus()` | GET /api/observability/status (lightweight) |
| `startPolling()` | Begin 60-second polling interval |
| `stopPolling()` | Clear interval on unmount |
| `setPollingInterval(ms)` | Change polling frequency |
| `formatModelName(model)` | "claude-haiku-4-5-20251001" -> "Claude Haiku" |
| `formatNumber(num)` | Number -> K/M suffix string |

---

### Dashboard Integration

> **REMOVED 2026-04-18**: OTel UI integration was removed from Dashboard.vue. The Dashboard no longer displays cost/token stats or the ObservabilityPanel. OTel metrics are still collected by the backend and exported to Prometheus — this only affects the Dashboard UI display.
>
> A dedicated Observability page will be added in a future phase.

**Previous implementation** (for reference):
- Header stats showed total cost and token count
- ObservabilityPanel displayed detailed breakdown
- observabilityStore fetched metrics on mount

---

### Observability Panel Component

> **REMOVED 2026-04-18**: ObservabilityPanel was removed from Dashboard. The component file still exists but is no longer used.

---

## UI Testing

> **NOTE**: Dashboard UI tests below are obsolete as of 2026-04-18. OTel backend functionality can still be tested via the Prometheus endpoint.

### Test: Prometheus Metrics Export (Still Valid)

**Action**: Set `OTEL_ENABLED=1`, start collector, generate agent activity

**Verify**:
```bash
# Check metrics are being collected
curl http://localhost:8889/metrics | grep trinity_claude_code
```

---

## Implementation Timeline

| Phase | Effort | Status |
|-------|--------|--------|
| Phase 1: Environment Variables | 30 min | Completed 2025-12-20 |
| Phase 2: OTEL Collector | 2 hours | Completed 2025-12-20 |
| Phase 2.5: UI Integration | 2 hours | ~~Completed 2025-12-20~~ **Removed 2026-04-18** |
| Phase 3: Prometheus/Grafana | 4 hours | Not started |
| Phase 4: Hooks Integration | 1-2 days | Not started |
| Phase 5: Dedicated Observability Page | TBD | Planned |

---

## Distributed Tracing (RELIABILITY-002)

### Overview

Backend auto-instrumentation for distributed tracing across multi-agent request flows. Enables end-to-end request correlation: when Agent A calls the backend which calls Agent B, all operations share a single trace ID.

### Architecture

```
Request (User)                    Agent A                    Agent B
     │                               │                          │
     │ ──traceparent──►              │                          │
     │                               │                          │
     ▼                               ▼                          ▼
┌─────────────────────────────────────────────────────────────────────┐
│                         Trinity Backend                              │
│  FastAPI (auto-instrumented) → httpx (auto-instrumented) → Agent    │
│                    │                                                 │
│                    │ traceparent header propagated                  │
│                    ▼                                                 │
│              OTLP/gRPC                                               │
└────────────────────┬────────────────────────────────────────────────┘
                     │
                     ▼
          ┌──────────────────┐
          │  OTel Collector  │
          │  :4317 (gRPC)    │
          └──────────────────┘
```

### Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `OTEL_ENABLED` | `0` | Set to `1` to enable tracing |
| `OTEL_SAMPLE_RATE` | `0.1` | Fraction of requests to sample (10% default) |
| `OTEL_COLLECTOR_ENDPOINT` | `http://trinity-otel-collector:4317` | Collector gRPC endpoint |

### Backend Implementation

**File**: `src/backend/main.py` (lines 247-289)

```python
def setup_opentelemetry(app: FastAPI) -> bool:
    """Initialize OTel distributed tracing."""
    if os.getenv("OTEL_ENABLED", "0") != "1":
        return False

    # Configure sampling (10% default)
    sample_rate = float(os.getenv("OTEL_SAMPLE_RATE", "0.1"))
    sampler = TraceIdRatioBased(sample_rate)

    # Create resource with service metadata
    resource = Resource.create({
        "service.name": "trinity-backend",
        "service.version": "1.0.0",
        "deployment.environment": os.getenv("ENVIRONMENT", "development"),
    })

    # Set up tracer provider
    provider = TracerProvider(resource=resource, sampler=sampler)
    trace.set_tracer_provider(provider)

    # Configure OTLP exporter
    endpoint = os.getenv("OTEL_COLLECTOR_ENDPOINT", "http://trinity-otel-collector:4317")
    exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True)
    provider.add_span_processor(BatchSpanProcessor(exporter))

    # Auto-instrument frameworks
    FastAPIInstrumentor.instrument_app(app)
    HTTPXClientInstrumentor().instrument()
    RedisInstrumentor().instrument()

    return True
```

**Called at**: App creation time, before middleware/routers (line 542)

### Log-Trace Correlation

**File**: `src/backend/logging_config.py` (lines 24-29)

```python
# Add OpenTelemetry trace context for log-trace correlation
span = trace.get_current_span()
span_context = span.get_span_context()
if span_context.is_valid:
    log_entry["trace_id"] = format(span_context.trace_id, "032x")
    log_entry["span_id"] = format(span_context.span_id, "016x")
```

**Result**: All JSON log entries during a request include `trace_id` and `span_id`:

```json
{
  "timestamp": "2026-04-14T12:19:35.623530Z",
  "level": "INFO",
  "logger": "services.execution_queue",
  "message": "[Queue] Agent 'trinity-system' execution started",
  "trace_id": "48b5fef080ff82fb283c308f2f3d408a",
  "span_id": "23dbd607a019b726"
}
```

### Dependencies

**File**: `docker/backend/Dockerfile` (lines 33-38)

```dockerfile
opentelemetry-api==1.29.0 \
opentelemetry-sdk==1.29.0 \
opentelemetry-exporter-otlp==1.29.0 \
opentelemetry-instrumentation-fastapi==0.50b0 \
opentelemetry-instrumentation-httpx==0.50b0 \
opentelemetry-instrumentation-redis==0.50b0
```

### Testing

```bash
# Enable tracing
export OTEL_ENABLED=1

# Restart backend
docker compose up -d backend

# Check startup log
docker compose logs backend | grep "OpenTelemetry tracing"
# Expected: "OpenTelemetry tracing enabled (sample rate: 10%)"

# Make a request that triggers logging
curl -X POST http://localhost:8000/api/agents/trinity-system/chat \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message": "hello"}'

# Check logs for trace_id
docker compose logs backend --since 10s | grep trace_id

# Check collector is receiving traces
docker compose logs otel-collector --since 30s | grep Traces
# Expected: "Traces" with "spans": N
```

### Error Handling

| Error | Behavior |
|-------|----------|
| Collector unreachable | Traces buffered, dropped after timeout; app continues |
| Invalid config | Logs warning, tracing disabled; app starts normally |
| OTEL_ENABLED=0 | No initialization, no overhead |

---

**Last Updated**: 2026-04-14 (Added Distributed Tracing section for RELIABILITY-002)

---

## Revision History

| Date | Changes |
|------|---------|
| 2026-04-18 | **REMOVED Dashboard UI**: OTel header stats (cost, tokens, warning) and ObservabilityPanel removed from Dashboard.vue. Backend metrics collection unchanged. A dedicated Observability page planned for future. |
| 2026-04-14 | Added Distributed Tracing section (RELIABILITY-002): backend auto-instrumentation for FastAPI/httpx/Redis, trace_id in logs, 10% sampling |
| 2026-01-23 | Updated line numbers for crud.py (308-316), docker-compose.yml (207-228), observability.py, main.py (289), Dashboard.vue (357, 371, 35-56, 340, 430-431), ObservabilityPanel.vue (185 lines). Added system_agent_service.py and ops.py to files modified. Added health_check endpoint port 13133 to docker-compose. Updated otel-collector.yaml to show health_check extension and telemetry config. |
| 2025-12-30 | Line numbers verified |
| 2025-12-20 | Initial Phase 1, 2, and 2.5 implementation complete |
