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

**File**: `.env.example` (lines 80-94)

```bash
# Enable OpenTelemetry metrics export from Claude Code agents
# Set to 1 to enable, 0 to disable (default: disabled)
OTEL_ENABLED=0

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
| `OTEL_ENABLED` | `CLAUDE_CODE_ENABLE_TELEMETRY` | `0` (disabled) |
| `OTEL_METRICS_EXPORTER` | `OTEL_METRICS_EXPORTER` | `otlp` |
| `OTEL_LOGS_EXPORTER` | `OTEL_LOGS_EXPORTER` | `otlp` |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `OTEL_EXPORTER_OTLP_PROTOCOL` | `grpc` |
| `OTEL_COLLECTOR_ENDPOINT` | `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://trinity-otel-collector:4317` |
| `OTEL_METRIC_EXPORT_INTERVAL` | `OTEL_METRIC_EXPORT_INTERVAL` | `60000` |

---

## Backend Layer

### Agent Creation - Environment Injection

**File**: `src/backend/routers/agents.py` (lines 514-522)

```python
# OpenTelemetry Configuration (opt-in via OTEL_ENABLED)
# Claude Code has built-in OTel support - these vars enable metrics export
if os.getenv('OTEL_ENABLED', '0') == '1':
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

**File**: `docker-compose.yml` (lines 132-151)

```yaml
# OpenTelemetry Collector - receives metrics from Claude Code agents
otel-collector:
  image: otel/opentelemetry-collector:0.91.0
  container_name: trinity-otel-collector
  restart: unless-stopped
  ports:
    - "4317:4317"   # gRPC receiver (OTLP)
    - "4318:4318"   # HTTP receiver (OTLP)
    - "8889:8889"   # Prometheus metrics exporter
  volumes:
    - ./config/otel-collector.yaml:/etc/otelcol/config.yaml:ro
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

```yaml
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: "0.0.0.0:4317"
      http:
        endpoint: "0.0.0.0:4318"

processors:
  # Batch metrics for efficient export
  batch:
    timeout: 10s
    send_batch_size: 1024

  # Add Trinity metadata to all metrics
  resource:
    attributes:
      - key: platform
        value: trinity
        action: upsert

exporters:
  # Prometheus exporter - scrape at :8889/metrics
  prometheus:
    endpoint: "0.0.0.0:8889"
    namespace: trinity
    const_labels:
      platform: trinity

  # Debug logging
  debug:
    verbosity: basic
    sampling_initial: 5
    sampling_thereafter: 200

service:
  pipelines:
    metrics:
      receivers: [otlp]
      processors: [batch, resource]
      exporters: [prometheus, debug]
    logs:
      receivers: [otlp]
      processors: [batch]
      exporters: [debug]
    traces:
      receivers: [otlp]
      processors: [batch]
      exporters: [debug]
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
2. Remove from `docker-compose.yml` (lines 132-151)
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
| `src/backend/routers/agents.py` | Added OTel env var injection (lines 514-522) |
| `src/backend/routers/observability.py` | New file - metrics API endpoints (257 lines) |
| `src/backend/main.py` | Added observability router import (line 41) and registration (line 209) |
| `docker-compose.yml` | Added otel-collector service (lines 132-151) |
| `config/otel-collector.yaml` | New file - collector configuration |
| `.env.example` | Added OTel environment variables (lines 80-94) |
| `docs/DEPLOYMENT.md` | Added OpenTelemetry section |
| `src/frontend/src/stores/observability.js` | New file - Pinia store for observability state (269 lines) |
| `src/frontend/src/components/ObservabilityPanel.vue` | New file - collapsible metrics panel (186 lines) |
| `src/frontend/src/views/Dashboard.vue` | Added OTel imports (lines 437, 440, 449), header stats (lines 39-61), panel (line 422), lifecycle (line 508) |

---

## Frontend Layer (Phase 2.5: UI Integration)

### Backend API Endpoints

**File**: `src/backend/routers/observability.py`

#### GET /api/observability/metrics (lines 140-218)

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

**Business Logic** (lines 23-137):
1. `parse_prometheus_metrics()` - Parses Prometheus text format into structured dict
2. `calculate_totals()` - Aggregates metrics across all models

**Error Handling**:
- Returns `{enabled: false}` when `OTEL_ENABLED=0`
- Returns `{available: false, error: "..."}` on connection/timeout errors
- Non-blocking - dashboard continues to work if collector unavailable

#### GET /api/observability/status (lines 221-256)

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
- Registration: line 209

```python
from routers.observability import router as observability_router
# ...
app.include_router(observability_router)
```

---

### Frontend State Management

**File**: `src/frontend/src/stores/observability.js` (269 lines)

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

#### Getters (lines 41-149)

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

#### Actions (lines 151-266)

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

**File**: `src/frontend/src/views/Dashboard.vue`

#### Imports (lines 437, 440, 449)

```javascript
import { useObservabilityStore } from '@/stores/observability'
import ObservabilityPanel from '@/components/ObservabilityPanel.vue'

const observabilityStore = useObservabilityStore()
```

#### Header Stats (lines 39-61)

OTel metrics display in the compact header alongside agent counts:

```html
<!-- OTel Stats (when operational and has data) -->
<template v-if="observabilityStore.isOperational && observabilityStore.hasData">
  <span class="text-gray-300">·</span>
  <span class="flex items-center space-x-1">
    <svg class="w-3 h-3 text-emerald-500"><!-- dollar icon --></svg>
    <span class="font-medium text-emerald-600">{{ observabilityStore.formattedTotalCost }}</span>
  </span>
  <span class="text-gray-300">·</span>
  <span class="flex items-center space-x-1">
    <span class="font-medium text-cyan-600">{{ observabilityStore.formattedTotalTokens }}</span>
    <span>tokens</span>
  </span>
</template>

<!-- Warning indicator (when enabled but collector unavailable) -->
<span v-if="observabilityStore.enabled && !observabilityStore.available"
      class="flex items-center space-x-1 text-yellow-600"
      title="OTel Collector unavailable">
  <svg class="w-3 h-3"><!-- warning icon --></svg>
  <span>OTel</span>
</span>
```

#### Panel Integration (line 422)

```html
<!-- Observability Panel (bottom-left, only when OTel enabled) -->
<ObservabilityPanel v-if="observabilityStore.enabled" />
```

#### Lifecycle (lines 507-508)

```javascript
onMounted(async () => {
  // ... other initializations ...
  // Initialize observability (checks if OTel is enabled)
  await observabilityStore.fetchStatus()
})
```

---

### Observability Panel Component

**File**: `src/frontend/src/components/ObservabilityPanel.vue` (186 lines)

#### Template Structure (lines 1-161)

Collapsible panel positioned at bottom-left of Dashboard:

| State | Display |
|-------|---------|
| **Collapsed** | Status indicator + Cost + Tokens summary |
| **Expanded** | Full breakdown with 4 sections |
| **Not enabled** | "OTel not enabled. Set OTEL_ENABLED=1." |
| **Error** | Yellow warning with error message |
| **No data** | "No metrics data yet. Start chatting..." |
| **Loading** | Centered spinner overlay |

#### Expanded Sections (lines 56-146)

1. **Cost by Model** (lines 57-74)
   - List of models with costs
   - Total at bottom with border

2. **Tokens by Type** (lines 76-93)
   - input, output, cacheCreation, cacheRead
   - Formatted with K/M suffix

3. **Productivity Grid** (lines 95-116)
   - 2x2 grid with: Sessions, Active Time, Commits, PRs
   - Styled cards with dark mode support

4. **Lines of Code** (lines 118-140)
   - Green for added, red for removed
   - Only shown if data exists

5. **Last Updated** (line 143-145)
   - "just now", "2m ago", or timestamp

#### Script (lines 163-185)

```javascript
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useObservabilityStore } from '@/stores/observability'

const observabilityStore = useObservabilityStore()
const isExpanded = ref(false)

const formatLastUpdated = computed(() => {
  // "just now" | "Xm ago" | time
})

onMounted(() => {
  observabilityStore.startPolling()  // Start 60s polling
})

onUnmounted(() => {
  observabilityStore.stopPolling()   // Cleanup interval
})
```

#### Styling

- Positioned: `absolute bottom-4 left-4`
- Width: Collapsed `w-48`, Expanded `w-96`
- Dark mode: Full support via `dark:` classes
- Transitions: `transition-all duration-300`

---

## UI Testing

### Test 1: OTel Disabled State

**Action**: Start with `OTEL_ENABLED=0` (default)

**Expected**:
- No OTel stats in Dashboard header
- ObservabilityPanel not rendered
- No API calls to `/api/observability/*`

**Verify**: Check browser DevTools Network tab - no observability requests

### Test 2: OTel Enabled, Collector Down

**Action**: Set `OTEL_ENABLED=1`, stop OTel collector

**Expected**:
- Yellow warning icon + "OTel" text in Dashboard header
- ObservabilityPanel shows: `<error message>`
- No crash or UI freeze

**Verify**: Inspect element shows yellow warning styling

### Test 3: OTel Fully Operational

**Action**: Set `OTEL_ENABLED=1`, start OTel collector, generate agent activity

**Expected**:
- Green cost + tokens in Dashboard header after 60s
- ObservabilityPanel shows collapsed summary
- Click expand to see full breakdown

**Verify**:
```bash
# Check metrics are being collected
curl http://localhost:8889/metrics | grep trinity_claude_code
```

### Test 4: Auto-Refresh

**Action**: Keep Dashboard open, generate more agent activity

**Expected**:
- Metrics update every 60 seconds automatically
- "Updated just now" / "Updated Xm ago" shows correctly
- No manual refresh needed

**Verify**: Watch Network tab for `/api/observability/metrics` calls every 60s

### Test 5: Dark Mode

**Action**: Toggle dark mode in browser/system

**Expected**:
- ObservabilityPanel respects dark mode
- All text and backgrounds adjust appropriately
- Status indicator colors remain visible

### Test 6: Panel Expand/Collapse

**Action**: Click the chevron button on ObservabilityPanel

**Expected**:
- Panel smoothly animates between collapsed (w-48) and expanded (w-96)
- All sections visible when expanded
- Summary visible when collapsed

---

## Implementation Timeline

| Phase | Effort | Status |
|-------|--------|--------|
| Phase 1: Environment Variables | 30 min | Completed 2025-12-20 |
| Phase 2: OTEL Collector | 2 hours | Completed 2025-12-20 |
| Phase 2.5: UI Integration | 2 hours | Completed 2025-12-20 |
| Phase 3: Prometheus/Grafana | 4 hours | Not started |
| Phase 4: Hooks Integration | 1-2 days | Not started |
