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
         - payload <= 5 MiB else 413; ReportCreate strict validation
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
  (full), `FleetReportStats`. `REPORT_PAYLOAD_MAX_BYTES = 5 MiB` (#1537).

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


## Agent read-back (#1538)

The write path was one-way: an agent could publish a report and never see it again, so a
recurring report had no way to continue a series — it could only re-derive, duplicate, or
contradict what it filed last period.

Two MCP tools close the loop over the **existing** access-controlled REST endpoints
(`GET /api/reports`, `GET /api/agents/{name}/reports`, `GET /api/reports/{id}`) — no new
endpoint, no new tenant-boundary logic:

- `list_reports` — metadata only (id, type, title, period, created_at), filters on
  `agent_name` / `report_type` / `hours` / `search`, paged. Same list-vs-detail split as REST,
  so a broad listing cannot dump every payload.
- `get_report(report_id)` — one report including its payload.

**The gate the backend cannot apply.** An agent-scoped key resolves to its *owner*, so the
backend scopes a read to everything the owner can see — wider than the calling agent's
permits. The tool narrows a broad listing to `{self} ∪ permitted` (the #1104 rule that
`list_operator_queue` established) and re-checks the owning agent on `get_report`.

**A denial looks like a miss, not a refusal.** `GET /api/reports/{id}` deliberately answers
404 rather than 403 so an id cannot be probed for existence. The MCP re-check returns the same
`Report not found` shape — returning "exists but forbidden" for agent keys would undo that
choice at the tool layer.

Write is unchanged and stays self-gated: reading another agent's reports never widens what you
can write.


## Search & filter (#1539)

The fleet view shipped with `report_type` / `hours` / `search`; the per-agent route never
got them, so the Agent Detail Reports tab was a flat list and the #1538 `list_reports` tool
silently dropped both filters whenever a caller scoped to one agent.

Both routes now build their WHERE clause from the same `_fleet_conditions`, with a single
parameterized difference:

| | fleet list | per-agent list |
|---|---|---|
| `search` matches | title, report_type, **agent_name** | title, report_type |

Including `agent_name` on a single-agent list would be actively misleading: every row
carries that name, so searching `recon` inside agent `recon-bot` would return the agent's
entire history — indistinguishable from search being ignored.

**Payload is not searched.** `LIKE` over a multi-MiB TEXT column with no index gets slower
exactly as reporting succeeds. An FTS/extracted-column answer belongs with the #1537
storage rework rather than being smuggled in behind a filter box.

**Facade note.** `database.py` forwards these by keyword. It previously delegated
positionally, so adding two parameters to the ops signature rebound `limit`→`hours` and
every request 500'd — the pitfall a wholesale-mocked test cannot see
(`test_1539_report_filters.py::test_facade_forwards_the_new_filters` pins it).


## Large payloads (#1537)

**Measured before designing.** On a live fleet: 4 reports, average 201 bytes, largest 683 —
four orders of magnitude under the 256 KiB cap. So the cap was never a limit agents were
hitting; it was the wall the *first* real tabular report would hit. That is why this raises
the ceiling and windows the read rather than migrating to off-row row storage: with no
payload anywhere near the cap, a rows table would be a schema commitment made against a
hypothetical.

| | before | after |
|---|---|---|
| ceiling | 256 KiB | 5 MiB |
| detail fetch (table) | whole blob | `GET /reports/{id}/rows` — columns + a window + `total` |
| storage | one TEXT blob | unchanged, no migration |

Verified end to end: a 12,000-row / 1.16 MB report (4.5× the old cap) creates successfully,
the row reader answers `total=12000` with 100 rows, and expanding the card in the UI
transfers **8,699 bytes** instead of 1.16 MB.

`display_hint` decides the fetch shape, and it is already on the summary — no extra request
is needed to know whether to page. Non-tabular payloads answer 400 on the rows route rather
than being given an invented row axis, and no-access answers 404 exactly like
`GET /reports/{id}`, so the sibling route cannot be used to probe an id.

**Honest residual.** The slice happens in Python after the whole blob is read from the
column, so it bounds the RESPONSE, not the read. Moving the slice into SQL needs the rows
off-row; the trigger for that work should be a measured payload distribution approaching the
new ceiling, not this issue's premise.


## Export (#1536)

`GET /api/reports/{id}/export?format=xlsx|pdf`. Builders are pure
`(payload, display_hint, title) -> bytes` in `services/report_export.py`; the router owns
access, format validation and headers.

| payload | .xlsx | .pdf |
|---|---|---|
| `table` | header row + typed cells | table, header repeated per page |
| `kpi` | `label / value / unit` | same, as a table |
| `timeline` | `ts / label / detail` | same |
| `markdown` | one line per row | flowed paragraphs |
| anything else | pretty JSON in one cell | preformatted block |

Nothing in that table is an error path. A `kpi` report asked for as a spreadsheet is a
two-column sheet, not a 400 — a stakeholder holding a slightly plain file is better served
than one holding a stack trace.

**Verified against real data**, not just unit-tested: the 12,000-row / 1.16 MB report from
#1537 exports to a 362 KB .xlsx whose sheet reads back with all 12,000 rows and typed
values, and to a 153 KB PDF; a markdown report exports to a 1.7 KB PDF.

Three decisions worth knowing:

- **Lazy imports.** `openpyxl`/`reportlab` are pinned in the backend image, but `start.sh`
  does not rebuild on an in-place upgrade (#1814). A module-level import would take the
  whole reports router down on such an instance; lazily importing turns it into one
  endpoint answering **503 — "rebuild the backend image"**. Confirmed live: on an
  un-rebuilt container the endpoint returns exactly that.
- **404, not 403**, matching `GET /reports/{id}` — an export URL must not become the
  existence oracle the detail route deliberately refuses to be.
- **The PDF is capped at 2000 rows with a visible note** pointing at the spreadsheet.
  Silent truncation of an export is a data-integrity trap; a 12,000-row PDF is not a
  document anyone reads.
