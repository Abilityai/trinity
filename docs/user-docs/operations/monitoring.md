# Monitoring

Multi-layer health monitoring for the agent fleet with real-time alerts, automatic cleanup of stuck resources, and a fleet-wide health view on the Operations page.

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

**Heartbeat Liveness:** In addition to the periodic health-check loop, each running agent pushes a lightweight heartbeat to the backend every 5 seconds. See [Agent Heartbeats](#agent-heartbeats) below.

**Alert Cooldowns:** Repeated alerts for the same condition are throttled to prevent notification spam.

## How It Works

### Health Tab (Operations Page)

Fleet health lives on the **Health** tab of the [Operations page](operating-room.md) (`/operations?tab=health`). The tab is admin-only — non-admin users do not see it, and deep links to `?tab=health` fall back to the default tab. The legacy `/monitoring` route redirects there.

The tab shows summary cards (Total Agents, Healthy, Degraded, Unhealthy, Critical), active alerts, and a per-agent health list with a status filter. Admins can trigger a fleet-wide check with **Check All** or a single-agent check from each row.

- Real-time WebSocket updates push health state changes as they occur.
- Individual agent health is visible in both the agent header and the Agents listing page.

### Enabling Monitoring

The periodic health-check loop is **disabled by default**. A status badge at the top of the Health tab shows the current state: "Monitoring Active" or "Monitoring Disabled", with an **Enable monitoring** / **Disable monitoring** button next to it (admin only) — no API call needed.

The same control is available via the API (admin only):

```
POST /api/monitoring/enable
POST /api/monitoring/disable
```

The choice is persisted, so it survives backend restarts — if monitoring was enabled, the loop resumes automatically on boot. The check interval and other options are configured via `GET`/`PUT /api/monitoring/config`, which also persists and applies the `enabled` flag.

### Agent Heartbeats

Independently of the health-check loop, every running agent (on a current image) POSTs a small heartbeat to the backend every ~5 seconds, authenticated with its own agent-scoped MCP key. A backend watch loop acts on missed beats:

- After **3 consecutive missed beats**, a soft, high-priority operator alert fires — exactly once per loss episode.
- When beats resume after a loss, a recovery notification fires.
- A missed beat never hard-marks an agent as down by itself — the alert is advisory, because a missed beat can also mean a transient network issue.

Each agent resolves to one of three heartbeat states:

| State | Meaning |
|-------|---------|
| `alive` | Beating normally |
| `stale` | Was beating, then stopped |
| `unsupported` | Agent runs an older image that never sent a beat — never treated as dead |

Heartbeat fields (`heartbeat_state`, `heartbeat_alive`, `last_heartbeat_age_s`, `heartbeat_memory_mb`, `heartbeat_active_executions`) surface on `GET /api/monitoring/status` for each agent. Heartbeats work even when the periodic monitoring loop is disabled.

### Cleanup Service

A background service that automatically recovers stuck resources:

- **Stale executions** -- Any execution with `status='running'` past its per-slot timeout is marked `failed`.
- **Stale activities** -- Any activity with `activity_state='started'` past the configured threshold is marked `failed`.
- **Stale Redis slots** -- Orphaned slot reservations are released.
- **Run frequency** -- Every 5 minutes, plus a one-shot sweep on backend restart.
- **Startup recovery** -- Orphaned executions (container down, not in process registry) are marked `failed` immediately and their slots are released.

### Retention Sweeps

The same cleanup service runs retention sweeps to keep the database lean. Setting any window to `0` disables that sweep.

| Sweep | Default | Setting |
|-------|---------|---------|
| `schedule_executions.execution_log` nulled past | 30 days | `execution_log_retention_days` |
| Terminal `schedule_executions` rows deleted past | 90 days | `execution_row_retention_days` |
| `agent_health_checks` rows deleted past | 7 days | `health_check_retention_days` |
| Agent soft-delete purged past | 180 days | `agent_soft_delete_retention_days` |
| Schedule soft-delete purged past | 30 days | `schedule_soft_delete_retention_days` |
| Agent reports deleted past | 90 days | `agent_reports_retention_days` |
| Terminal operator-queue rows deleted past | 90 days | `operator_queue_retention_days` |
| `audit_log` rows deleted past | 365 days | `AUDIT_LOG_RETENTION_DAYS` (floor 365, exempt) |

The **agent soft-delete** purge is special: it destroys the agent's data volumes, so it is a recovery window, not a log window — it is **exempt** from the community floor below.

The 5,000-row figure some tooling reports bounds each **transaction**, not each sweep. Most prunes drain the whole candidate set in one sweep; the real bound on destruction is the blast-radius guard below, not chunking. A daily VACUUM at 04:30 UTC reclaims freed pages.

#### Community floor (fresh installs)

Fresh community installs are **seeded** with a 5-day minimum retention on the log windows. This applies to **new installs only** — it is never a retroactive change to an existing install, and the agent soft-delete window is exempt in every edition. Any admin can widen a window at any time.

#### Blast-radius guard & admin approval

A sweep that would delete more than a fixed safety threshold (**1,000 rows**) of a single table **refuses** to run, logs an error, and raises an operator-queue alarm instead of deleting. An admin must then approve it in **Settings → the retention panel**, which shows a pending-acknowledgements banner.

The approval is:

- **Admin- and human-only** — agent-scoped keys cannot approve.
- **Bound to the exact window in force** — approving names the deletion it authorizes; a mismatched window is rejected.
- **Single-use** — the guard re-arms after the prune runs, so each over-threshold sweep needs its own approval.

Agent-purge sweeps always require an acknowledgement because every one destroys data volumes.

#### Changing a window

Retention windows have exactly **one** write path: `PUT /api/settings/ops/config`, reached from **Settings → Retention**. It type- and range-validates every value all-or-nothing (one bad value rejects the whole request with a 422) and writes an audit entry naming which windows moved. The generic settings endpoint refuses these keys and points you here.

Two things worth internalising, because the risk is counter-intuitive:

- Garbage always failed **safe** — an unparseable value became `0`, which *disables* the sweep and retains forever.
- **A small valid number is the dangerous input.** `1` is well-formed and in range, and no range check can distinguish it from an operator who genuinely wants a one-day window. Validation buys you a loud failure instead of a silent coercion; what actually stops an accidental mass deletion is the blast-radius guard above and the admin-plus-human gate on approving it.

**Reset to defaults** (`POST /api/settings/ops/reset`) deliberately **skips** retention windows — resetting other operator settings must never silently change how much of your data is kept. The reset is itself audit-logged.

Endpoints:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/settings/retention` | GET | Effective windows (per-window value + source), edition, community-floor days, the read-only guard threshold, and pending acknowledgements (admin) |
| `/api/settings/ops/config` | PUT | The single validated, audited write path for retention windows (admin) |
| `/api/settings/retention/acknowledge` | POST | Approve one over-threshold prune — body `{key, window_days}` (admin, human-only) |

## Real-Time Event Reliability

Trinity uses a Redis Streams-backed event bus for all WebSocket delivery. This is invisible during normal operation but has operator-visible behaviour in a few edge cases.

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
| `/api/monitoring/status` | GET | Fleet health summary (includes `heartbeat_*` fields) |
| `/api/monitoring/agents/{name}` | GET | Single-agent health detail |
| `/api/monitoring/agents/{name}/check` | POST | Force immediate health check |
| `/api/monitoring/enable` | POST | Start the health-check loop; persisted (admin) |
| `/api/monitoring/disable` | POST | Stop the health-check loop; persisted (admin) |
| `/api/monitoring/config` | GET/PUT | Monitoring configuration, including the enabled flag (admin) |
| `/api/monitoring/check-all` | POST | Trigger a fleet-wide health check (admin) |
| `/api/monitoring/cleanup-status` | GET | Cleanup service status (admin) |
| `/api/monitoring/cleanup-trigger` | POST | Force a cleanup run (admin) |
| `/api/agents/{name}/heartbeat` | POST | Agent liveness heartbeat (sent by the agent itself every ~5s) |

Full API reference: http://localhost:8000/docs

## See Also

- [Dashboard](dashboard.md) -- Main dashboard overview
- [Operations Page](operating-room.md) -- Operator queue, notifications, and the other Operations tabs
- [Executions](executions.md) -- Fleet execution list (Executions tab)
