# Agent Reports

Structured results an agent publishes for you to read — leads found, emails sent, a weekly summary, a KPI snapshot — so you learn what an agent accomplished without scrolling its chat history.

## Concepts

- **Report** — A titled, typed payload an agent publishes. It has a `report_type` (namespaced, e.g. `recon.weekly_summary`), a title, an optional period, and a JSON payload.
- **Display hint** — How the payload should be rendered: `table`, `kpi`, `markdown`, `timeline`, or `json`.
- **Report series** — Reports sharing a `report_type`. Agents can read back their own previous reports to continue a series rather than duplicate it.

## How It Works

### Reading reports

Two surfaces:

| Where | Scope |
|-------|-------|
| Agent detail → **Reports** tab | One agent's reports |
| **Operations → Reports** | The whole fleet, with KPI tiles |

Both support the same filters: report type, time window, and free-text search over titles and types. The fleet view additionally matches on agent name.

Lists show metadata only — the payload loads when you expand a card, so a page of large reports stays fast.

Reports render according to their display hint:

| Hint | Rendered as |
|------|-------------|
| `table` | A sortable table. Large tables are paged, so opening a multi-megabyte report transfers only the rows you're looking at. |
| `kpi` | A row of labelled value tiles |
| `markdown` | Formatted prose (sanitized) |
| `timeline` | A chronological event list |
| `json` | A pretty-printed JSON viewer |

A payload whose shape doesn't match its hint degrades to the JSON viewer rather than erroring.

### Exporting

Any report can be exported from its card:

- **Excel** (`.xlsx`) — best for tables and KPI sets.
- **PDF** — best for sharing a summary.

A shape that doesn't map cleanly degrades to a sensible sheet or embedded JSON rather than failing. If the export libraries are missing from your image (an older build), the endpoint says so explicitly rather than breaking the page.

### Publishing (what your agent does)

Agents publish with the `report` MCP tool. Trinity's platform prompt tells every agent that this tool exists, when to reach for it, and what payload shape each display hint expects — so reporting is a fleet-wide default rather than something each template has to opt into.

An agent can only publish **as itself**: an agent-scoped key reporting under a sibling agent's name is rejected.

### Retention

Reports are pruned past `agent_reports_retention_days` (default 90; `0` disables). Configure it under **Settings → Retention**.

## For Agents

| Tool | Description |
|------|-------------|
| `report(report_type, title, payload, display_hint?, period_start?, period_end?)` | Publish a report. Self-only. |
| `list_reports(agent_name?, report_type?, hours?, search?)` | Metadata for reports you can see |
| `get_report(report_id)` | Full payload |

Read access for an agent key is narrowed to itself plus the agents it is explicitly permitted to reach. A report you may not read returns "not found" rather than a distinguishable permission error.

**REST endpoints** — see [Backend API Docs](http://localhost:8000/docs) for full schemas.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/agents/{name}/reports` | GET/POST | List / publish for one agent |
| `/api/agents/{name}/reports/{id}` | DELETE | Delete a report |
| `/api/reports` | GET | Fleet list (filters: `report_type`, `hours`, `search`, `agent`) |
| `/api/reports/stats` | GET | Fleet KPI tiles |
| `/api/reports/{id}` | GET | Full payload |
| `/api/reports/{id}/rows` | GET | Window a `table` payload (`offset`, `limit`, true `total`) |
| `/api/reports/{id}/export` | GET | `?format=xlsx` or `?format=pdf` |

## Limitations

- Payloads cap at 5 MiB; a larger report is rejected with 413.
- Publishing is rate-limited per agent (30 per minute by default) so a runaway agent can't flood the table.
- Search matches titles and report types, **not** payload contents.
- The live update that arrives when an agent publishes carries only metadata — the browser refetches content through access-controlled endpoints, so report contents never broadcast to every logged-in session.

## See Also

- [Executions](executions.md) — the run-level record behind a report
- [Dashboard](dashboard.md) — fleet overview
- [MCP Server](../integrations/mcp-server.md) — the `report` tool
- [Dynamic Dashboards](../advanced/dynamic-dashboards.md) — agent-defined dashboard panels
