# Feature: Agent-Reported Structured Reports (#918)

## Overview
A generic **agent report** primitive: agents publish typed-but-flexible structured reports
(telemetry, domain results — leads found, emails sent, KPI snapshots, weekly summaries) via an
MCP tool. Reports are persisted and surfaced on the Agent Detail "Reports" tab and a
fleet-wide Operations → Reports view, so users see what each agent produces without reading
chat transcripts.

Three-surface feature (backend router + MCP tool + frontend). **No agent-server endpoint** —
reports flow agent → MCP → backend. Structural clone of `agent_activities`.

## User Story
As an operator running a fleet of autonomous agents, I want each agent to publish structured
results I can browse on a dashboard, so I don't have to scrape chat history to learn what an
agent accomplished this week.

## Entry Points
- **MCP Tool**: `report` in `src/mcp-server/src/tools/reports.ts`
- **API (create)**: `POST /api/agents/{name}/reports`
- **API (read)**: `GET /api/agents/{name}/reports`, `GET /api/reports`, `GET /api/reports/stats`,
  `GET /api/reports/{id}`, `DELETE /api/agents/{name}/reports/{id}`

## Data Flow
```
agent --report(...)--> MCP reports.ts (resolves agent from auth context; agent-scoped key only)
   --> client.createReport(agentName, data) --> POST /api/agents/{name}/reports
       routers/reports.py:
         - AuthorizedAgent (owner-access to path agent)
         - self-gate: agent-scoped key => current_user.agent_name == name  (no sibling spoof)
         - rate_limiter.enforce(report:{name}, 30/60s) => 429 (fail-open, runaway guard)
         - payload <= 256 KB else 413; ReportCreate strict validation
       --> report_service.create_report
             --> db.create_report (agent_reports row; SQLite/PG via SQLAlchemy Core)
             --> THIN WS trigger {type:agent_report, agent_name, report_id, report_type, created_at}
                 |  (broadcast = /ws SCOPE_ALL ; broadcast_filtered = /ws/events SCOPE_SCOPED)
                 v
         frontend stores/reports.js handleWebSocketEvent --> REFETCH via REST (access-controlled)
```

## Why the WS event is thin (security A1)
`/ws` registers with `scope=SCOPE_ALL` and `_event_is_visible` returns true for **every**
SCOPE_ALL event with no access filter (`services/event_bus.py:112-113`). A SCOPE_SCOPED-only
broadcast would never reach the main UI, and a SCOPE_ALL broadcast reaches every logged-in
browser. So the `agent_report` event carries only trigger metadata — never `title`/`payload`
(which can hold sensitive domain data). The store refetches the actual content through the
access-gated REST endpoints (the `notifications` pattern). Guarded by
`tests/unit/test_918_report_broadcast.py`.

## Self-gated create (security, Codex #1)
`AuthorizedAgent` only checks that the key owner can access the path agent — it does **not**
stop an agent-scoped key from reporting as a *sibling* agent the owner also shares
(`dependencies.py:385`). The create endpoint additionally requires
`current_user.agent_name == name` for agent-scoped callers (mirrors
`heartbeat_service.authorize_heartbeat`). The MCP tool also resolves the agent purely from the
auth context and rejects user-scoped keys, so a report can only ever be attributed to the
calling agent.

## Backend Layers
- **Router** `routers/reports.py` — collection routes before parameterized (`/api/reports/stats`
  before `/api/reports/{id}`, Invariant #4). Create is rate-limited per agent via the shared
  sliding-window limiter (`rate_limiter.enforce`, `REPORT_RATE_LIMIT`/30 per 60s, fail-open →
  429) so a runaway agent can't flood the table between retention sweeps. Fleet endpoints use
  `accessible_agent_names` + `_narrow_to_agent` (imported from `routers/executions.py`);
  admin = no filter.
- **Service** `services/report_service.py` — persist + thin broadcast only (module-level WS
  managers injected from `main.py`, `notifications` pattern).
- **DB** `db/reports.py::ReportOperations` — `create_report`, `get_report` (full),
  `get_reports_for_agent` / `get_fleet_reports` (metadata only), `get_fleet_report_stats`
  (total / by_type / agents), `delete_report(agent_name, id)` (scoped, Codex #2),
  `prune_agent_reports` (chunked, `iso_cutoff`, `idx_agent_reports_created`). Delegated through
  `database.py`.
- **Models** `models.py` — `ReportCreate` (regex `report_type`, `Literal` `display_hint`,
  ranged `schema_version`, ISO + ordered periods), `ReportSummary` (no payload), `Report`
  (full), `FleetReportStats`. `REPORT_PAYLOAD_MAX_BYTES = 256 KiB`.

## Schema & Migration
`agent_reports` table (see architecture.md → Database Schema). Dual-track: SQLite
`_migrate_agent_reports_table` (`db/migrations.py`, registered `agent_reports_table`) + Alembic
`migrations/versions/0006_agent_reports.py`. Three indexes: `(agent_name, created_at DESC)`,
`(report_type, created_at DESC)`, `(created_at)` for the retention scan.

## Frontend
- **Stores** `stores/reports.js` — two stores (Codex #7): `useReportsStore` (agent-scoped,
  mirrors `loops.js`: `setAgent`/`fetchReports`/`loadPayload`/`deleteReport`/`clearAgent`) and
  `useFleetReportsStore` (fleet, mirrors `executions.js`: `filters`/`refresh`/`setFilter`,
  `setActive` gate so a WS trigger only refetches while the panel is mounted). Wired into
  `utils/websocket.js` `agent_report` dispatch.
- **Renderers** `components/reports/` — `ReportRenderer.vue` picks by `display_hint` →
  `report_type` prefix → JSON, validating payload shape and falling back to `ReportJson` on
  mismatch (Codex #10). Typed renderers: `ReportTable`, `ReportKpiTiles`, `ReportMarkdown`
  (DOMPurify via `utils/markdown.js`), `ReportTimeline`, `ReportJson`.
- **Panels** — `ReportsPanel.vue` (Agent Detail "Reports" tab) and `ReportsPanelFleet.vue`
  (Operations → Reports tab; agent/type/time/search filters + KPI tiles from
  `GET /api/reports/stats`). Lists show metadata; full payload lazy-loads on expand.

### Renderer payload contracts
| hint | expected payload shape |
|------|------------------------|
| `table` | `{ columns: string[], rows: Array<object\|array> }` |
| `kpi` | `{ tiles: Array<{label, value, unit?}> }` |
| `markdown` | `{ markdown: string }` |
| `timeline` | `{ events: Array<{ts?, label, detail?}> }` |
| `json` (or anything malformed) | rendered as a pretty-printed JSON viewer |

## Retention
`cleanup_service._sweep_retention_772` prunes `agent_reports` older than
`agent_reports_retention_days` (ops setting, default 90, `0` disables) via
`db.prune_agent_reports`, chunked at `RETENTION_CHUNK_SIZE_PER_CYCLE`.

## Tests
- `tests/unit/test_918_agent_reports_db.py` — CRUD, metadata-only lists, fleet access filter,
  search, stats, scoped delete, retention prune (cutoff + disabled).
- `tests/unit/test_918_report_endpoint.py` — endpoint gating: self-gate blocks sibling-spoof
  (403), payload over cap rejected (413), self-report reaches the service.
- `tests/unit/test_918_report_broadcast.py` — A1 leak regression (event carries no
  title/payload).
- `tests/unit/test_cleanup_inner_sweeps.py` — updated for the new retention sweep.

## Deferred (NOT in scope)
- Effect-guard dedup on `report()` for at-least-once pull-mode re-delivery (#1084 / Epic #1045).
- Audit-log entry on report write (issue: low priority — "reports are the audit").
- Per-report sharing distinct from the agent's access model.
