# Agent Custom Metrics - Feature Flow

> **Updated**: 2025-12-27 - Refactored to service layer architecture. Metrics logic moved to `services/agent_service/metrics.py`.

**Feature ID**: 9.9
**Status**: Implemented
**Date**: 2025-12-10
**Last Updated**: 2025-12-27

## Overview

Agent Custom Metrics allows agents to define domain-specific KPIs in their `template.yaml` that Trinity displays in the UI. This enables per-agent observability beyond generic tool call counts.

## Flow Diagram

```
┌─────────────────────┐     ┌─────────────────────┐     ┌─────────────────────┐
│   template.yaml     │     │   Agent writes      │     │   User opens        │
│   defines metrics:  │     │   metrics.json      │     │   Metrics tab       │
│   - name            │     │   with values       │     │                     │
│   - type            │     │                     │     │                     │
│   - label           │     │                     │     │                     │
└─────────────────────┘     └─────────────────────┘     └─────────────────────┘
          │                           │                           │
          ▼                           ▼                           ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         Agent Server (/api/metrics)                          │
│   1. Read template.yaml → get metric definitions                             │
│   2. Read metrics.json → get current values                                  │
│   3. Return { has_metrics, definitions, values, last_updated }               │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                    Backend (/api/agents/{name}/metrics)                      │
│   1. Access control check (owner/shared/admin)                               │
│   2. Check agent is running                                                  │
│   3. Proxy to agent server                                                   │
│   4. Add agent_name and status to response                                   │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         Frontend (MetricsPanel.vue)                          │
│   1. Load metrics when tab activated                                         │
│   2. Render type-specific components:                                        │
│      - counter: Large number with label                                      │
│      - gauge: Number with optional unit                                      │
│      - percentage: Progress bar with thresholds                              │
│      - status: Colored badge                                                 │
│      - duration: Formatted time (e.g., "2h 15m")                             │
│      - bytes: Formatted size (e.g., "1.2 MB")                                │
│   3. Auto-refresh every 30 seconds                                           │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Metric Types

| Type | Description | Display | Example |
|------|-------------|---------|---------|
| `counter` | Monotonically increasing | Large number | "42 Messages" |
| `gauge` | Current value (up/down) | Number + unit | "12.5 Avg Words" |
| `percentage` | 0-100 value | Progress bar | "75% Success Rate" |
| `status` | Enum/state | Colored badge | "ACTIVE", "IDLE" |
| `duration` | Time in seconds | Formatted | "2h 15m" |
| `bytes` | Size in bytes | Formatted | "1.2 MB" |

## Template Schema

```yaml
# template.yaml
metrics:
  - name: messages_processed     # Internal identifier (snake_case)
    type: counter                # counter|gauge|percentage|status|duration|bytes
    label: "Messages"            # Display label
    description: "Total messages"  # Tooltip text

  - name: success_rate
    type: percentage
    label: "Success Rate"
    warning_threshold: 80        # Yellow if below
    critical_threshold: 50       # Red if below

  - name: current_state
    type: status
    label: "State"
    values:                      # Required for status type
      - value: "active"
        color: "green"
        label: "Active"
      - value: "error"
        color: "red"
        label: "Error"
```

## Metrics Data File

Agents write `metrics.json` in workspace:

```json
{
  "messages_processed": 42,
  "success_rate": 87.5,
  "current_state": "active",
  "last_updated": "2025-12-10T10:30:00Z"
}
```

## API Endpoints

### Agent Server: GET /api/metrics

```json
{
  "has_metrics": true,
  "definitions": [...],
  "values": {...},
  "last_updated": "2025-12-10T10:30:00Z"
}
```

### Backend: GET /api/agents/{name}/metrics

Same as above, plus:
- `agent_name`: Agent identifier
- `status`: "running" or "stopped"
- Access control enforced

## Key Files

| Component | File | Purpose |
|-----------|------|---------|
| Agent Server | `docker/base-image/agent_server/routers/info.py:161` | GET /api/metrics endpoint |
| Router | `src/backend/routers/agents.py:689-696` | GET /api/agents/{name}/metrics endpoint |
| Service | `src/backend/services/agent_service/metrics.py` (92 lines) | Metrics proxy logic |
| Frontend | `src/frontend/src/components/MetricsPanel.vue` | Metrics display component (350 lines) |
| Frontend | `src/frontend/src/views/AgentDetail.vue:342` | Metrics tab content integration |
| Store | `src/frontend/src/stores/agents.js:439` | getAgentMetrics action |

### Backend Architecture

```python
# Router (agents.py:689-696)
@router.get("/{agent_name}/metrics")
async def get_agent_metrics(agent_name: str, request: Request, current_user: User = Depends(get_current_user)):
    """Get agent custom metrics."""
    return await get_agent_metrics_logic(agent_name, current_user)
```

```python
# Service (metrics.py:18-92)
async def get_agent_metrics_logic(agent_name: str, current_user: User) -> dict:
    """Get agent custom metrics from agent's internal API."""
    if not db.can_user_access_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, ...)
    # ... proxy to agent-server
```

## Test Agents with Metrics

All test agents have metrics defined:

1. **test-echo**: messages_echoed, total_words, total_characters, avg_message_length
2. **test-counter**: counter_value, increment_count, decrement_count, reset_count, total_operations
3. **test-delegator**: delegations_sent, delegations_succeeded, delegations_failed, success_rate, unique_agents_contacted
4. **test-scheduler**: scheduled_executions, manual_executions, last_execution_status, total_log_entries, uptime_seconds
5. **test-queue**: requests_processed, total_delay_seconds, avg_delay, queue_depth, quick_requests
6. **test-files**: files_created, files_deleted, total_bytes_written, current_file_count, directories_created
7. **test-error**: normal_responses, intentional_failures, timeouts, error_rate, last_error_type

## Future Enhancements

1. **Metrics History**: Store time-series data for graphs
2. **Alerting**: Trigger alerts when thresholds breached
3. **Aggregation**: Platform-wide metrics dashboard
4. **Export**: Prometheus/OpenTelemetry export
5. **Dashboard Display**: Show key metrics on agent cards

## Related Documents

- [Agent Template Spec](../../docs/AGENT_TEMPLATE_SPEC.md)
- [Agent Custom Metrics Spec](../../docs/AGENT_CUSTOM_METRICS_SPEC.md)
- [Requirements 9.9](requirements.md#99-agent-custom-metrics)
