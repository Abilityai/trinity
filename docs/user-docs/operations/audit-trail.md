# Audit Trail

Append-only log of administrative actions across the platform. Records who did what for compliance, incident investigation, and security monitoring.

## Concepts

| Term | Description |
|------|-------------|
| **Event type** | Category of action (agent_lifecycle, authentication, etc.) |
| **Actor** | Who performed the action (user, agent, mcp_client, system) |
| **Target** | What was affected (agent, user, schedule, etc.) |
| **Source** | Where the action originated (api, mcp, scheduler) |
| **Hash chain** | Optional SHA-256 chain over consecutive entries — proves the log wasn't tampered with between two checkpoints |

## What Gets Logged

| Event Type | Actions |
|------------|---------|
| `agent_lifecycle` | create, start, stop, delete, rename, recover |
| `authentication` | login_success, login_failure, logout |
| `authorization` | permission_granted, permission_denied |
| `configuration` | settings_changed, quota_updated |
| `credentials` | injected, exported, imported |
| `mcp_operation` | tool_called — every MCP tool call, captured via a transparent wrapper |
| `git_operation` | sync, pull, push |
| `system` | startup, shutdown, migration |

All entry-emitting code paths now write to the audit log; the earlier "Phase 1 coverage" caveat no longer applies.

## How It Works

### Dashboard

Admins get a searchable dashboard at **Enterprise → Audit Log** in the UI. The dashboard is part of the OSS bundle but is gated by an `audit` entitlement on the route — instances without the entitlement bounce to the dashboard catalogue.

The dashboard surfaces:

- **Stats tiles** — total events in window, distinct event types, distinct actors.
- **Time presets** — Last 1h, 24h, 7d, 30d, All time. Manual edits flip the preset to "custom".
- **Filters** — event type, actor type, actor id, target type, target id, source. Drop-down options come from live `/distinct/event-types` and `/distinct/actor-types` endpoints, so the UI never goes stale.
- **Heatmaps** (collapsible card with two tabs):
  - **Weekly pattern** — day-of-week × hour-of-day grid showing when activity happens.
  - **Calendar** — GitHub-style per-day grid showing *when in calendar time*.
- **Paginated table** with inline drill-down — click any cell to apply that value as a filter.
- **Hash-chain verify badge** — runs a SHA-256 verification over the currently visible window and reports whether the chain is intact.
- **Export** — CSV or JSON download of the current filter window.

### Retention

Audit entries are kept for **365 days** by default (`AUDIT_LOG_RETENTION_DAYS`). A daily background job at 04:15 UTC prunes entries older than the retention window. The retention floor is 365 days — the database trigger refuses DELETE on younger rows.

## For Agents

**API Endpoints**: see [Backend API Docs](http://localhost:8000/docs) for full schemas.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/audit-log` | GET | List entries (filterable, paginated) |
| `/api/audit-log/{event_id}` | GET | Single entry by UUID |
| `/api/audit-log/stats` | GET | Aggregate counts by event_type and actor_type |
| `/api/audit-log/heatmap` | GET | Day-of-week × hour-of-day grid |
| `/api/audit-log/calendar` | GET | Per-day activity grid |
| `/api/audit-log/distinct/event-types` | GET | Sorted unique event types (for filter dropdowns) |
| `/api/audit-log/distinct/actor-types` | GET | Sorted unique actor types |
| `/api/audit-log/export` | GET | CSV or JSON export of a time window |
| `/api/audit-log/verify` | POST | Verify SHA-256 hash chain over an id range |
| `/api/audit-log/hash-chain/enable` | POST | Toggle hash chain computation for new entries |

All endpoints are admin-only.

**Common query parameters** (apply across list, stats, heatmap, calendar, export):

- `event_type`, `actor_type`, `actor_id`, `target_type`, `target_id`, `source`
- `start_time`, `end_time` (ISO 8601)
- `limit` (default 100, max 1000), `offset`

### Entry Format

```json
{
  "event_id": "550e8400-e29b-41d4-a716-446655440000",
  "event_type": "agent_lifecycle",
  "event_action": "create",
  "actor_type": "user",
  "actor_id": "42",
  "actor_email": "admin@example.com",
  "target_type": "agent",
  "target_id": "my-agent",
  "timestamp": "2026-05-14T10:30:00Z",
  "source": "api",
  "endpoint": "/api/agents",
  "request_id": "9a7c…",
  "details": { "template": "github:Org/repo" }
}
```

## Data Integrity

The audit log is append-only at the database level:

- **No updates** — entries cannot be modified after creation. Enforced by an unconditional SQLite trigger.
- **No deletes within the retention window** — the SQLite trigger refuses DELETE on rows newer than 365 days.
- **Hash chain** — when enabled, each entry stores `previous_hash` and `entry_hash`; the verify endpoint walks the chain and reports the first broken link.

## See Also

- [Monitoring](monitoring.md) — Health checks and system metrics
- [Executions](executions.md) — Task execution history
- [Authentication](../api-reference/authentication.md) — Auth flow details
