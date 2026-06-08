# Monitoring

Multi-layer health monitoring for the agent fleet with real-time alerts, automatic cleanup of stuck resources, and a fleet-wide health dashboard.

## Concepts

**Health Levels** (ordered by severity):

| Level | Meaning |
|-------|---------|
| healthy | All checks passing |
| degraded | Minor issues detected |
| unhealthy | Significant problems |
| critical | Immediate attention required |
| unknown | Unable to determine status |

**Three Monitoring Layers:**

1. **Docker layer** -- Container status, CPU/memory usage, restart count, OOM detection.
2. **Network layer** -- Agent HTTP reachability with latency tracking.
3. **Business layer** -- Runtime availability, context usage, error rates.

**Alert Cooldowns:** Repeated alerts for the same condition are throttled to prevent notification spam.

## How It Works

### Fleet Health Dashboard

The fleet health dashboard is an admin-only view that summarizes the health of all agents in the system. It is accessible from the admin area of the UI.

- Real-time WebSocket updates push health state changes as they occur.
- Individual agent health is visible in both the agent header and the Agents listing page.

### Cleanup Service

A background service that automatically recovers stuck resources:

- **Stale executions** -- Any execution with `status='running'` past its per-slot timeout is marked `failed`.
- **Stale activities** -- Any activity with `activity_state='started'` past the configured threshold is marked `failed`.
- **Stale Redis slots** -- Orphaned slot reservations are released.
- **Run frequency** -- Every 5 minutes, plus a one-shot sweep on backend restart.
- **Startup recovery** -- Orphaned executions (container down, not in process registry) are marked `failed` immediately and their slots are released.

### Retention Sweeps

The same cleanup service runs daily retention sweeps to keep the database lean:

| Sweep | Default | Setting |
|-------|---------|---------|
| `schedule_executions.execution_log` nulled past | 30 days | `execution_log_retention_days` |
| Terminal `schedule_executions` rows deleted past | 90 days | `execution_row_retention_days` |
| `agent_health_checks` rows deleted past | 7 days | `health_check_retention_days` |
| `audit_log` rows deleted past | 365 days | `AUDIT_LOG_RETENTION_DAYS` (floor 365) |

Each sweep is capped at 5,000 rows per cycle, so the first post-deploy backfill spans hours, not minutes. Setting any retention value to `0` disables that sweep. A daily VACUUM at 04:30 UTC reclaims freed pages.

## Real-Time Event Reliability

Trinity uses a Redis Streams-backed event bus for all WebSocket delivery (RELIABILITY-003). This is invisible during normal operation but has operator-visible behaviour in a few edge cases.

### Reconnect Replay

When a browser tab reconnects after a brief disconnect (e.g., laptop sleep, flaky network), it automatically requests missed events using a `?last-event-id=` cursor tracked in memory. Events are replayed from the Redis stream, so the collaboration dashboard, activity timeline, and operator queue resume without stale state.

### `resync_required` Events

If the cursor is too far behind (>5 000 events missed) or the stream has been trimmed past the stored cursor, the server sends a `{"type": "resync_required"}` message. The frontend clears the cursor and refetches authoritative state via REST. Users see a brief refresh but no data loss.

The stream retains approximately the last 10 000 events (configurable via `REDIS_STREAM_MAXLEN` in `.env`).

### Admin Stats Endpoint

For soak monitoring and diagnosing delivery issues:

```
GET /api/debug/event-bus-stats    (admin-only)
```

Returns counters since last backend restart:

| Field | What to check |
|-------|---------------|
| `publisher.events_published` | Total events emitted |
| `dispatcher.drops_queue_full` | Events dropped due to slow clients |
| `dispatcher.clients_evicted` | Connections closed after 3 consecutive send failures |
| `dispatcher.resyncs_sent` | Forced full-state refreshes sent to clients |
| `watchdog.cumulative_orphaned` | Orphaned executions recovered by cleanup service |

Healthy baseline: `drops_queue_full + clients_evicted + resyncs_sent` should be < 0.1% of `events_published`. Non-zero `cumulative_orphaned` warrants investigation.

## For Agents

Agents can query monitoring data through these MCP tools:

| Tool | Description |
|------|-------------|
| `get_fleet_health()` | Fleet-wide health summary |
| `get_agent_health(name)` | Individual agent health |
| `trigger_health_check()` | Force an immediate health check |

### API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/monitoring/status` | GET | Fleet health summary |
| `/api/monitoring/agents/{name}` | GET | Single-agent health detail |
| `/api/monitoring/agents/{name}/check` | POST | Force immediate health check |
| `/api/monitoring/cleanup-status` | GET | Cleanup service status (admin) |
| `/api/monitoring/cleanup-trigger` | POST | Force a cleanup run (admin) |

## See Also

- [Dashboard](dashboard.md) -- Admin dashboard overview
- [Operating Room](operating-room.md) -- Real-time operations view
