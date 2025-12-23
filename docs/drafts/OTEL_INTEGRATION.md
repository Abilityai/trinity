# OpenTelemetry Integration for Trinity Agents

> **Status**: Phase 1 & 2 Complete
> **Created**: 2025-12-20
> **Priority**: Medium
> **Effort**: Low (Phase 1) to Medium (Full)
>
> **Phase 1 Completed**: 2025-12-20 - Environment variable injection implemented and tested
> **Phase 2 Completed**: 2025-12-20 - OTEL Collector service with Prometheus export verified

## Overview

Integrate Claude Code's built-in OpenTelemetry (OTel) telemetry into Trinity agents to enable standardized metrics export, external observability tools (Prometheus/Grafana), and additional metrics not currently captured.

## Motivation

### Current State

Trinity already captures ~80% of what Claude Code OTel provides:

| OTel Capability | Trinity Equivalent | Location |
|-----------------|-------------------|----------|
| `claude_code.cost.usage` | `cost` field | `chat_messages.cost` |
| `claude_code.token.usage` | `context_used/max` | `chat_messages.context_used/max` |
| `claude_code.tool_result` events | Tool call tracking | `agent_activities` table |
| `claude_code.api_request` latency | `execution_time_ms` | `chat_messages.execution_time_ms` |
| Session tracking | `chat_sessions` | Full session lifecycle |
| Real-time events | WebSocket broadcasts | `activity_service.py` |

### What OTel Adds

1. **Standard OTLP format** - Export to Prometheus/Grafana/Datadog without custom integration
2. **Additional metrics**:
   - `claude_code.lines_of_code.count` - Productivity tracking (added/removed)
   - `claude_code.code_edit_tool.decision` - Accept/reject rates by language
   - `claude_code.active_time.total` - Active usage duration
   - `claude_code.session.count` - Session lifecycle with terminal type
3. **Hooks system** - Quality gates and custom validation (future)

---

## Requirements

### Phase 1: Enable OTel Environment Variables ✅ COMPLETE

**Priority**: High | **Effort**: 30 minutes | **Risk**: Very Low | **Completed**: 2025-12-20

#### 1.1 Add OTel Environment Variables to Agent Creation

**File**: `src/backend/routers/agents.py:503-512`

Add OTel configuration to the `env_vars` dict during agent container creation:

```python
env_vars = {
    # ... existing vars ...

    # OpenTelemetry Configuration (opt-in via OTEL_ENABLED)
    'CLAUDE_CODE_ENABLE_TELEMETRY': os.getenv('OTEL_ENABLED', '0'),
    'OTEL_METRICS_EXPORTER': os.getenv('OTEL_METRICS_EXPORTER', 'otlp'),
    'OTEL_LOGS_EXPORTER': os.getenv('OTEL_LOGS_EXPORTER', 'otlp'),
    'OTEL_EXPORTER_OTLP_PROTOCOL': os.getenv('OTEL_EXPORTER_OTLP_PROTOCOL', 'grpc'),
    'OTEL_EXPORTER_OTLP_ENDPOINT': os.getenv('OTEL_COLLECTOR_ENDPOINT', ''),
    'OTEL_METRIC_EXPORT_INTERVAL': os.getenv('OTEL_METRIC_EXPORT_INTERVAL', '60000'),
}
```

**Acceptance Criteria**:
- [x] OTel env vars injected into agent containers at creation
- [x] Default OFF (`OTEL_ENABLED=0`) - zero impact on existing agents
- [x] Configurable via backend environment variables
- [x] Existing agents continue to work unchanged

#### 1.2 Update Environment Configuration

**File**: `.env.example`

Add new environment variables:

```bash
# OpenTelemetry Configuration (Optional)
# Set OTEL_ENABLED=1 to enable Claude Code telemetry export
OTEL_ENABLED=0
OTEL_COLLECTOR_ENDPOINT=http://otel-collector:4317
OTEL_METRICS_EXPORTER=otlp
OTEL_LOGS_EXPORTER=otlp
OTEL_EXPORTER_OTLP_PROTOCOL=grpc
OTEL_METRIC_EXPORT_INTERVAL=60000
```

**Acceptance Criteria**:
- [x] `.env.example` documents all OTel options
- [x] Comments explain each variable

#### 1.3 Documentation

**File**: `docs/DEPLOYMENT.md`

Add section on OTel configuration:

```markdown
### OpenTelemetry Metrics (Optional)

Trinity agents can export metrics to an OpenTelemetry collector for
external observability tools like Prometheus and Grafana.

To enable:
1. Set `OTEL_ENABLED=1` in your `.env`
2. Configure `OTEL_COLLECTOR_ENDPOINT` to your collector
3. Restart the backend

See Phase 2 for adding an OTEL collector service.
```

---

### Phase 2: OTEL Collector Service ✅ COMPLETE

**Priority**: Medium | **Effort**: 2 hours | **Risk**: Low | **Completed**: 2025-12-20

#### 2.1 Add OTEL Collector to Docker Compose

**File**: `docker-compose.yml`

```yaml
  otel-collector:
    image: otel/opentelemetry-collector:0.91.0
    container_name: trinity-otel-collector
    restart: unless-stopped
    ports:
      - "4317:4317"   # gRPC receiver
      - "4318:4318"   # HTTP receiver
      - "8889:8889"   # Prometheus exporter
    volumes:
      - ./config/otel-collector.yaml:/etc/otelcol/config.yaml:ro
    networks:
      - trinity-network
    labels:
      - "trinity.platform=infrastructure"
      - "trinity.service=otel-collector"
```

**Acceptance Criteria**:
- [x] Collector service starts with `docker-compose up`
- [x] Accessible from agent containers via Docker network
- [x] Prometheus metrics exposed on port 8889
- [x] Graceful restart on failure

#### 2.2 Create Collector Configuration

**File**: `config/otel-collector.yaml` (new)

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

exporters:
  # Prometheus for metrics (scrape at :8889/metrics)
  prometheus:
    endpoint: "0.0.0.0:8889"
    namespace: trinity
    const_labels:
      platform: trinity

  # Debug logging (optional - disable in production)
  logging:
    loglevel: info
    sampling_initial: 5
    sampling_thereafter: 200

service:
  pipelines:
    metrics:
      receivers: [otlp]
      processors: [batch]
      exporters: [prometheus, logging]
    logs:
      receivers: [otlp]
      processors: [batch]
      exporters: [logging]
```

**Acceptance Criteria**:
- [x] Config file created in `config/` directory
- [x] Receives OTLP over gRPC and HTTP
- [x] Exports to Prometheus format
- [x] Batches for efficiency

#### 2.3 Update Agent Network Configuration

Ensure agents can reach collector:

**File**: `src/backend/routers/agents.py`

Update `OTEL_EXPORTER_OTLP_ENDPOINT` default:

```python
'OTEL_EXPORTER_OTLP_ENDPOINT': os.getenv(
    'OTEL_COLLECTOR_ENDPOINT',
    'http://trinity-otel-collector:4317'  # Default to Docker service name
),
```

---

### Phase 3: Prometheus/Grafana Dashboard (Optional)

**Priority**: Low | **Effort**: 4 hours | **Risk**: Very Low

#### 3.1 Add Prometheus Service

**File**: `docker-compose.monitoring.yml` (new optional file)

```yaml
version: '3.8'

services:
  prometheus:
    image: prom/prometheus:v2.47.0
    container_name: trinity-prometheus
    ports:
      - "9090:9090"
    volumes:
      - ./config/prometheus.yml:/etc/prometheus/prometheus.yml:ro
      - prometheus-data:/prometheus
    networks:
      - trinity-network
    command:
      - '--config.file=/etc/prometheus/prometheus.yml'
      - '--storage.tsdb.path=/prometheus'
      - '--web.enable-lifecycle'

  grafana:
    image: grafana/grafana:10.2.0
    container_name: trinity-grafana
    ports:
      - "3001:3000"
    volumes:
      - grafana-data:/var/lib/grafana
      - ./config/grafana/provisioning:/etc/grafana/provisioning:ro
    networks:
      - trinity-network
    environment:
      - GF_SECURITY_ADMIN_PASSWORD=${GRAFANA_PASSWORD:-admin}
      - GF_USERS_ALLOW_SIGN_UP=false

volumes:
  prometheus-data:
  grafana-data:
```

#### 3.2 Prometheus Configuration

**File**: `config/prometheus.yml` (new)

```yaml
global:
  scrape_interval: 15s
  evaluation_interval: 15s

scrape_configs:
  - job_name: 'otel-collector'
    static_configs:
      - targets: ['trinity-otel-collector:8889']

  - job_name: 'trinity-backend'
    static_configs:
      - targets: ['backend:8000']
    metrics_path: /metrics
```

#### 3.3 Grafana Dashboard

**File**: `config/grafana/provisioning/dashboards/trinity-agents.json`

Pre-configured dashboard showing:
- Agent session counts over time
- Cost per agent (USD)
- Token usage (input/output/cache)
- Tool acceptance rates
- Lines of code generated

---

### Phase 4: Claude Code Hooks Integration (Future)

**Priority**: Low | **Effort**: 1-2 days | **Risk**: Medium

#### 4.1 Hooks Configuration Injection

Inject hooks configuration during Trinity meta-prompt injection:

**File**: `docker/base-image/agent_server/routers/trinity.py`

Add hooks.json generation:

```python
# During inject endpoint
hooks_config = {
    "hooks": {
        "PostToolUse": [{
            "matcher": "*",
            "hooks": [{
                "type": "command",
                "command": "python /home/developer/.trinity/hooks/log_tool.py"
            }]
        }],
        "Stop": [{
            "hooks": [{
                "type": "prompt",
                "prompt": "Evaluate if the task is complete. Respond with JSON: {\"decision\": \"approve\" or \"block\", \"reason\": \"explanation\"}"
            }]
        }]
    }
}
```

#### 4.2 Hook Scripts

Create hook scripts in Trinity meta-prompt:

**File**: `config/trinity-meta-prompt/hooks/log_tool.py`

```python
#!/usr/bin/env python3
import json
import sys
import requests

data = json.load(sys.stdin)
# Log to Trinity backend
requests.post(
    f"http://backend:8000/api/hooks/tool-result",
    json=data,
    timeout=5
)
```

---

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Trinity Platform                              │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  ┌──────────────┐     ┌──────────────┐     ┌──────────────┐        │
│  │   Agent 1    │     │   Agent 2    │     │   Agent N    │        │
│  │              │     │              │     │              │        │
│  │ Claude Code  │     │ Claude Code  │     │ Claude Code  │        │
│  │ + OTel SDK   │     │ + OTel SDK   │     │ + OTel SDK   │        │
│  └──────┬───────┘     └──────┬───────┘     └──────┬───────┘        │
│         │                    │                    │                 │
│         └────────────────────┼────────────────────┘                 │
│                              │ OTLP (gRPC/HTTP)                     │
│                              ▼                                      │
│                    ┌──────────────────┐                             │
│                    │  OTEL Collector  │                             │
│                    │    :4317/:4318   │                             │
│                    └────────┬─────────┘                             │
│                             │                                       │
│              ┌──────────────┼──────────────┐                       │
│              ▼              ▼              ▼                       │
│      ┌────────────┐  ┌────────────┐  ┌────────────┐               │
│      │ Prometheus │  │  Logging   │  │  (Future)  │               │
│      │   :8889    │  │            │  │  Backend   │               │
│      └─────┬──────┘  └────────────┘  └────────────┘               │
│            │                                                        │
│            ▼                                                        │
│      ┌────────────┐                                                │
│      │  Grafana   │  ◄── Dashboards                                │
│      │   :3001    │                                                │
│      └────────────┘                                                │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

---

## OTel Metrics Reference

### Metrics Captured by Claude Code

| Metric | Type | Description | Attributes |
|--------|------|-------------|------------|
| `claude_code.session.count` | Counter | Sessions started | session_id, terminal_type, app_version |
| `claude_code.cost.usage` | Counter | Cost in USD | model |
| `claude_code.token.usage` | Counter | Token consumption | type (input/output/cache), model |
| `claude_code.lines_of_code.count` | Counter | Code added/removed | type (added/removed) |
| `claude_code.code_edit_tool.decision` | Counter | Edit accept/reject | tool, decision, language |
| `claude_code.active_time.total` | Counter | Active usage seconds | - |
| `claude_code.pull_request.count` | Counter | PRs created | - |
| `claude_code.commit.count` | Counter | Commits created | - |

### Events Captured by Claude Code

| Event | Description | Key Attributes |
|-------|-------------|----------------|
| `claude_code.user_prompt` | User input | prompt_length |
| `claude_code.tool_result` | Tool execution | tool_name, success, duration_ms |
| `claude_code.api_request` | API call | model, cost_usd, tokens |
| `claude_code.api_error` | API error | error, status_code |
| `claude_code.tool_decision` | Accept/reject | tool_name, decision |

---

## Testing Plan

### Phase 1 Testing

1. **Unit Test**: Verify env vars are passed to container
   ```bash
   docker exec agent-test-agent env | grep OTEL
   ```

2. **Integration Test**: Enable OTel, verify no errors in agent logs
   ```bash
   # Set OTEL_ENABLED=1, create agent
   docker logs agent-test-agent 2>&1 | grep -i otel
   ```

### Phase 2 Testing

1. **Collector Health**:
   ```bash
   curl http://localhost:8889/metrics | grep claude_code
   ```

2. **Agent Export**:
   - Chat with agent
   - Verify metrics appear in Prometheus
   - Check `trinity_claude_code_cost_usage_total`

---

## Rollback Plan

### Phase 1 Rollback
- Set `OTEL_ENABLED=0` in `.env`
- Restart backend
- New agents won't have OTel enabled

### Phase 2 Rollback
- Stop otel-collector service
- Remove from docker-compose.yml
- Agents will log warnings but continue working

---

## Timeline

| Phase | Effort | Dependencies |
|-------|--------|--------------|
| Phase 1 | 30 min | None |
| Phase 2 | 2 hours | Phase 1 |
| Phase 3 | 4 hours | Phase 2 |
| Phase 4 | 1-2 days | Phase 1 |

---

## Related Documents

- `docs/memory/feature-flows/activity-stream.md` - Existing activity tracking
- `docs/memory/feature-flows/agent-chat.md` - Chat metrics extraction
- `docs/memory/feature-flows/agent-lifecycle.md` - Container creation flow
- Claude Code Docs: `/Users/eugene/Dropbox/Agents/claude-code-docs/docs/monitoring-usage.md`

---

## Decision Log

| Date | Decision | Rationale |
|------|----------|-----------|
| 2025-12-20 | Start with env vars only (Phase 1) | Minimal risk, enables future integration |
| 2025-12-20 | Use OTEL Collector (not direct export) | Flexibility, can add multiple backends |
| 2025-12-20 | Keep existing activity stream | OTel is additive, not replacement |
