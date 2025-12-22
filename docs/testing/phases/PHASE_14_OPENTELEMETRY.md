# Phase 14: OpenTelemetry Integration

> **Purpose**: Validate OpenTelemetry metrics collection and UI display
> **Duration**: ~15 minutes
> **Assumes**: Phase 1 PASSED (authentication), at least one running agent
> **Output**: OTel metrics visible in Dashboard header and Observability panel

---

## Prerequisites

- Phase 1 PASSED (logged in as admin)
- Backend healthy at http://localhost:8000
- Frontend accessible at http://localhost:3000
- At least one running agent (for generating metrics)
- **OTEL_ENABLED=1** in backend .env (or verify with `/api/observability/status`)

---

## Test Steps

### Step 1: Verify OTel Backend Configuration

**Action**: Check if OpenTelemetry is enabled in the backend

```bash
# Via API
curl http://localhost:8000/api/observability/status \
  -H "Authorization: Bearer $TOKEN"
```

OR check via browser DevTools Console:
```javascript
fetch('/api/observability/status', {
  headers: { 'Authorization': `Bearer ${localStorage.getItem('token')}` }
}).then(r => r.json()).then(console.log)
```

**Expected**:
- [ ] Returns JSON with `enabled: true` (OTel is on)
- [ ] `collector_configured: true` (endpoint is set)
- [ ] `collector_reachable: true` (collector responding)

**If `enabled: false`**:
- OTel is not enabled in backend
- Set `OTEL_ENABLED=1` in `.env` and restart backend
- Or document as "OTel disabled" and skip remaining steps

---

### Step 2: Verify OTel Collector is Running

**Action**: Check the OTel Collector container

```bash
# Check container status
docker ps | grep otel-collector

# Check collector logs
docker logs trinity-otel-collector 2>&1 | tail -20

# Test Prometheus endpoint
curl -s http://localhost:8889/metrics | head -20
```

**Expected**:
- [ ] Container `trinity-otel-collector` is running
- [ ] Logs show "Everything is ready" or similar
- [ ] Prometheus endpoint returns metrics (even if empty initially)

---

### Step 3: Verify Dashboard Header Shows OTel Stats

**Action**:
- Navigate to Dashboard (http://localhost:3000)
- Look at the header stats bar (next to agent counts)

**Expected** (when OTel operational with data):
- [ ] Cost displayed: `$X.XX` in green
- [ ] Tokens displayed: `XXK tokens` or `X.XM tokens`
- [ ] Both update when page refreshes

**Expected** (when OTel enabled but no data):
- [ ] No cost/token stats shown (hidden when no data)
- [ ] No error messages

**Expected** (when collector unreachable):
- [ ] Yellow warning icon with "OTel" label
- [ ] Tooltip explains collector is unavailable

---

### Step 4: Generate Agent Activity for Metrics

**Action**:
- Navigate to any running agent
- Send a chat message that triggers tool use, e.g.:
  - "List the files in the current directory"
  - "What is 2 + 2?"
  - "Hello, how are you?"

**Expected**:
- [ ] Agent responds successfully
- [ ] Response includes cost and token info in the UI

**Note**: Metrics export happens every 60 seconds by default. Wait 60-90 seconds after activity before checking metrics.

---

### Step 5: Verify Observability Panel

**Action**:
- After waiting 60+ seconds for metrics export
- Look for the Observability Panel in Dashboard (bottom-left area)
- Click to expand if collapsed

**Expected** (panel visible when OTel enabled):
- [ ] Panel shows in collapsed state with summary
- [ ] Clicking expands to show full breakdown
- [ ] Shows "Cost by Model" section
- [ ] Shows "Tokens by Type" section (input, output, cacheRead)
- [ ] Shows "Productivity" section (sessions, active time)
- [ ] Shows "Lines of Code" if any were modified
- [ ] Shows "Last Updated" timestamp

**Expected** (no data yet):
- [ ] Panel shows "No metrics data yet"
- [ ] Message suggests generating activity

---

### Step 6: Verify Prometheus Metrics Format

**Action**: Query the raw Prometheus metrics

```bash
curl -s http://localhost:8889/metrics | grep trinity_claude_code
```

**Expected**:
- [ ] `trinity_claude_code_cost_usage_USD_total` lines present
- [ ] `trinity_claude_code_token_usage_tokens_total` lines present
- [ ] Labels include `model`, `type`, `platform`
- [ ] Values are numeric (not NaN or empty)

**Sample Output**:
```
trinity_claude_code_cost_usage_USD_total{model="claude-sonnet-4-20250514",platform="trinity"} 0.0234
trinity_claude_code_token_usage_tokens_total{model="claude-sonnet-4-20250514",platform="trinity",type="input"} 1523
```

---

### Step 7: Verify Metrics API Response Structure

**Action**: Call the full metrics API

```bash
curl http://localhost:8000/api/observability/metrics \
  -H "Authorization: Bearer $TOKEN" | jq
```

**Expected JSON structure**:
```json
{
  "enabled": true,
  "available": true,
  "metrics": {
    "cost_by_model": { "claude-sonnet": 0.0234 },
    "tokens_by_model": {
      "claude-sonnet": { "input": 1523, "output": 892, "cacheRead": 45678 }
    },
    "lines_of_code": { "added": 42, "removed": 15 },
    "sessions": 5,
    "active_time_seconds": 3600,
    "commits": 3,
    "pull_requests": 1
  },
  "totals": {
    "total_cost": 0.0234,
    "total_tokens": 48093,
    "tokens_by_type": { "input": 1523, "output": 892, "cacheRead": 45678 }
  }
}
```

**Verify**:
- [ ] `enabled: true`
- [ ] `available: true` (or false with error message)
- [ ] `metrics` object contains expected fields
- [ ] `totals` aggregates all models

---

### Step 8: Verify Auto-Refresh (Optional)

**Action**:
- Keep Dashboard open with Observability panel expanded
- Generate more agent activity (send another chat message)
- Wait 60 seconds for next polling cycle
- Watch for values to update

**Expected**:
- [ ] Metrics update automatically without page refresh
- [ ] "Last Updated" timestamp changes
- [ ] New activity reflected in token/cost counts

**Verify** (browser DevTools Network tab):
- [ ] `/api/observability/metrics` called every 60 seconds
- [ ] Response returns 200 with updated values

---

### Step 9: Verify Agent OTel Environment Variables

**Action**: Check that running agents have OTel env vars injected

```bash
# Pick any running agent
docker exec agent-YOUR_AGENT_NAME env | grep -E "(OTEL|CLAUDE_CODE_ENABLE)"
```

**Expected**:
- [ ] `CLAUDE_CODE_ENABLE_TELEMETRY=1`
- [ ] `OTEL_METRICS_EXPORTER=otlp`
- [ ] `OTEL_LOGS_EXPORTER=otlp`
- [ ] `OTEL_EXPORTER_OTLP_PROTOCOL=grpc`
- [ ] `OTEL_EXPORTER_OTLP_ENDPOINT=http://trinity-otel-collector:4317`
- [ ] `OTEL_METRIC_EXPORT_INTERVAL=60000`

**Note**: If env vars missing, agent was created before OTel was enabled. Restart agent to inject vars.

---

### Step 10: Test Dark Mode Support

**Action**:
- Toggle dark mode (if available in UI, or via browser dev tools)
- Check Observability panel appearance

**Expected**:
- [ ] Panel background adjusts to dark mode
- [ ] Text colors remain readable
- [ ] Status indicators visible
- [ ] No broken layouts

---

## Critical Validations

### OTel Must Not Block Agent Operations

**Validation**: If collector is down, agents should still work

```bash
# Stop collector
docker stop trinity-otel-collector

# Send message to agent
curl -X POST http://localhost:8000/api/agents/YOUR_AGENT/chat \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message": "Hello"}'

# Agent should still respond (metrics just won't export)
```

**Verify**:
- [ ] Chat works even with collector stopped
- [ ] Dashboard shows yellow warning for OTel
- [ ] No crash or error in agent logs

**Cleanup**: Restart collector
```bash
docker start trinity-otel-collector
```

---

### Metrics Namespace

**Validation**: All metrics use `trinity_` prefix

```bash
curl -s http://localhost:8889/metrics | grep -E "^trinity_" | head -10
```

**Expected**:
- [ ] All metric names start with `trinity_`
- [ ] No raw `claude_code_` metrics exposed

---

## Success Criteria

Phase 14 is **PASSED** when:
- OTel status API returns `enabled: true`
- OTel Collector container is running
- Dashboard header shows cost/token stats (when data exists)
- Observability panel displays metric breakdown
- Prometheus endpoint returns properly formatted metrics
- Agent env vars include OTel configuration
- System is resilient to collector downtime

---

## Troubleshooting

**OTel not enabled**:
- Check `.env` has `OTEL_ENABLED=1`
- Restart backend: `docker-compose restart backend`
- Re-check `/api/observability/status`

**Collector not running**:
- Check docker-compose.yml includes otel-collector service
- Start: `docker-compose up -d otel-collector`
- Check logs: `docker logs trinity-otel-collector`

**No metrics appearing**:
- Wait 60+ seconds after agent activity (export interval)
- Check collector logs for received data
- Verify agent has OTel env vars (restart if needed)

**Dashboard not showing stats**:
- Check browser console for errors
- Verify observability store is loaded
- Force refresh: `observabilityStore.fetchMetrics()`

**Yellow warning in header**:
- Collector is configured but unreachable
- Check collector container is running
- Check network connectivity on `trinity-network`

---

## API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/observability/status` | Quick status check (enabled, available) |
| GET | `/api/observability/metrics` | Full metrics with breakdown |

---

## Cleanup

After testing:
- [ ] Restart collector if stopped: `docker start trinity-otel-collector`
- [ ] No special cleanup needed (metrics persist in collector)

---

## Related Documentation

- Feature Flow: `docs/memory/feature-flows/opentelemetry-integration.md`
- Requirements: `requirements.md` section 10.8
- Collector Config: `config/otel-collector.yaml`
- Observability Store: `src/frontend/src/stores/observability.js`
- Observability Panel: `src/frontend/src/components/ObservabilityPanel.vue`

---

## Next Phase

Proceed to **Phase 15: System Agent & Ops** after PASSED.

---

**Status**: Ready for testing
**Last Updated**: 2025-12-21
