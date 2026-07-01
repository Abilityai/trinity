# Feature: Platform Audit Trail (SEC-001 / Issue #20)

## Overview
Cross-cutting append-only audit log for the Trinity platform. Records WHO did
WHAT across agent lifecycle, authentication, authorization, configuration,
credentials, MCP operations, git operations, and system events. Distinct from
the Process Engine's `audit_entries` table which is workflow-specific.

**Status**: All phases implemented 2026-04-16. Issue #20 can be closed
after merge.

| Phase | Scope | Status |
|---|---|---|
| **Phase 1** | Schema, immutability triggers, `PlatformAuditService`, `db/audit.py`, admin query API, unit tests | ✅ Merged |
| **Phase 2a** | Agent lifecycle integration (create / start / stop / delete) as working smoke test | ✅ Merged |
| **Phase 2b** | Auth, sharing, credentials, settings, rename integrations + request_id middleware | ✅ This PR |
| **Phase 3** | MCP server integration — TypeScript audit logging for all tool calls | ✅ This PR |
| **Phase 4** | Hash chain verification, CSV/JSON export, enable/disable toggle | ✅ Merged |
| **Phase 5** | Admin dashboard UI (`/enterprise/audit`) + distinct-value endpoints for filter dropdowns | ✅ #941 |
| **Phase 5 v2** | Stats tiles, time-preset chips, drill-down filters, hash-chain verify badge, CSV/JSON export | ✅ #941 v2 |
| **Phase 5 v3** | Day-of-week × hour-of-day activity heatmap | ✅ #941 v3 |
| **Phase 5 v3.1** | GitHub-style per-day calendar heatmap with click-to-drill-down | ✅ #941 v3.1 |
| **Phase 6** | `request_id` correlation (MCP `mcp_operation` ⇄ backend `git_operation` join) + recover `target_id`/`request_id` on the MCP row | ✅ #905 |

## User Story
As a platform admin, I want a tamper-evident record of every administrative
action so that I can investigate incidents, respond to compliance requests,
and trace who modified what across the platform.

## Entry Points
- **Backend — query API** (admin-only): `src/backend/routers/audit_log.py` mounted at `/api/audit-log`
- **Backend — write API** (internal, used by Phase 2 integrations): `services/platform_audit_service.py` exports a global `platform_audit_service` instance with an `async log(...)` method
- **Database**: `audit_log` table in `~/trinity-data/trinity.db`

## Frontend Layer

Phase 5 (#941) adds an admin-facing dashboard at `/enterprise/audit`. The view
ships in the OSS bundle; the route is gated by `requiresEntitlement: 'audit'`
in `src/frontend/src/router/index.js`, so OSS-only deploys (no enterprise
submodule mounted) bounce to the dashboard catalogue or home. Backend
endpoints stay OSS — only the dashboard UI is enterprise-flagged.

| File | Role |
|---|---|
| `src/frontend/src/views/enterprise/Audit.vue` | Dashboard view — filter form + paginated table + side detail panel |
| `src/frontend/src/stores/auditLog.js` | Pinia store — entries, filters (default last 24h), pagination, distinct lists, selected entry |
| `src/frontend/src/views/enterprise/Index.vue` | Enterprise landing — audit card flipped from `soon: true` to Available |
| `src/frontend/src/router/index.js` | Route gate (`requiresEntitlement: 'audit'`) |
| `src/backend/enterprise/backend/__init__.py` | Submodule entitlement registration (`register_module("audit")`) — flips the UI route from hidden to visible |

### Distinct-value endpoints (#941)

Two cheap aggregate endpoints feed the dashboard's filter dropdowns so the
frontend doesn't hardcode the `event_type` / `actor_type` enums:

- `GET /api/audit-log/distinct/event-types` → sorted `list[str]`
- `GET /api/audit-log/distinct/actor-types` → sorted `list[str]`

Both admin-only (`Depends(require_admin)`). Indexed source columns, low
cardinality — sub-ms even on a million-row audit_log.

### Heatmap endpoint (#941 v3)

`GET /api/audit-log/heatmap?start_time=…&end_time=…&event_type=…&actor_type=…`
returns a sparse day-of-week × hour-of-day aggregation:

```json
{
  "cells": [{"dow": 1, "hour": 9, "count": 42}, …],
  "total": 1234,
  "max_count": 42
}
```

`dow` is the SQLite `strftime('%w', …)` index (Sunday=0…Saturday=6) so the
payload stays calendrically canonical; the frontend reorders rows to ISO
Mon…Sun for display. Honors the dashboard's current `start_time`/
`end_time`/`event_type`/`actor_type` filters so the heatmap and the table
view stay coherent.

### Calendar endpoint (#941 v3.1)

`GET /api/audit-log/calendar?start_time=…&end_time=…&event_type=…&actor_type=…`
returns a sparse per-day aggregation (GitHub-style calendar view):

```json
{
  "days": [{"date": "2026-05-25", "count": 234}, …],
  "total": 6753,
  "max_count": 740
}
```

Complements the `/heatmap` endpoint:

| Endpoint | Question it answers |
|---|---|
| `/heatmap` (7×24 dow×hour) | When in a *typical week* is activity heavy? |
| `/calendar` (per-day) | Which *calendar days* were heavy? |

`date` is the SQLite-formatted UTC date (`strftime('%Y-%m-%d', timestamp)`).
Quiet days are omitted — the frontend lays the sparse pairs onto a dense
week × day-of-week grid (Mon-top, GitHub style) and pads out-of-range
cells as transparent so the grid edges read as "outside window".

Clicking a cell calls `store.drilldownToDay(date)`, which narrows the
filter to `[dateT00:00:00Z, dateT23:59:59Z]`, demotes the preset chip to
`custom`, and reloads list + stats + heatmap + calendar together — the
dashboard pivots as one unit.

### Out of scope for Phase 5

These were deferred to follow-up issues — backend support already exists:
CSV/JSON export download button, hash-chain verify button, stats tiles
on the dashboard header, and an external log-export push (an enterprise capability tracked privately in `trinity-enterprise`).

Note: the legacy `/api/audit` router (Process Engine audit) was removed in
#430 (2026-04-24). The platform audit log at `/api/audit-log` is the only
audit surface now.

## Backend Layer

### Database Schema (`db/schema.py`, `db/migrations.py`)

```sql
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT UNIQUE NOT NULL,        -- UUID for deduplication
    event_type TEXT NOT NULL,             -- AuditEventType
    event_action TEXT NOT NULL,           -- "create" / "login_success" / etc.
    actor_type TEXT NOT NULL,             -- user / agent / mcp_client / system
    actor_id TEXT,                        -- user.id or agent_name or key_id
    actor_email TEXT,
    actor_ip TEXT,
    mcp_key_id TEXT,
    mcp_key_name TEXT,
    mcp_scope TEXT,
    target_type TEXT,                     -- agent / user / schedule / ...
    target_id TEXT,
    timestamp TEXT NOT NULL,              -- ISO 8601 UTC
    details TEXT,                         -- JSON payload
    request_id TEXT,                      -- correlation id
    source TEXT NOT NULL,                 -- api / mcp / scheduler / system
    endpoint TEXT,                        -- request path
    previous_hash TEXT,                   -- Phase 4
    entry_hash TEXT,                      -- Phase 4
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Indexes match the spec's documented query patterns
CREATE INDEX idx_audit_log_timestamp ON audit_log(timestamp DESC);
CREATE INDEX idx_audit_log_event_type ON audit_log(event_type, timestamp DESC);
CREATE INDEX idx_audit_log_actor ON audit_log(actor_type, actor_id, timestamp DESC);
CREATE INDEX idx_audit_log_target ON audit_log(target_type, target_id, timestamp DESC);
CREATE INDEX idx_audit_log_mcp_key ON audit_log(mcp_key_id, timestamp DESC);
CREATE INDEX idx_audit_log_request ON audit_log(request_id);

-- Append-only enforcement
CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'Audit log entries cannot be modified'); END;

CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log
WHEN OLD.timestamp > datetime('now', '-365 days')
BEGIN SELECT RAISE(ABORT, 'Audit log entries cannot be deleted within retention period'); END;
```

Migration `audit_log_table` (#31 in `db/migrations.py`) creates the table on
existing installs; fresh installs use `db/schema.py` directly.

### Database Operations (`db/audit.py`)

`PlatformAuditOperations` class with:
- `create_audit_entry(entry)` — insert
- `get_audit_entries(**filters, limit, offset)` — paginated list (newest first)
- `count_audit_entries(**filters)` — total for the same filter
- `get_audit_entry(event_id)` — single row by UUID
- `get_audit_entries_range(start_id, end_id)` — for hash-chain verification (Phase 4)
- `get_audit_stats(start_time, end_time)` — aggregate counts by event_type and actor_type

Composed into `DatabaseManager` as `db._audit_ops` with thin delegate methods
following the existing `db/permissions.py` → `database.py` pattern (invariant #2).

### Service Layer (`services/platform_audit_service.py`)

```python
from services.platform_audit_service import (
    platform_audit_service,
    AuditEventType,
)

await platform_audit_service.log(
    event_type=AuditEventType.AGENT_LIFECYCLE,
    event_action="create",
    source="api",
    actor_user=current_user,
    target_type="agent",
    target_id=agent_name,
    request_id=request.headers.get("X-Request-ID"),
    endpoint=str(request.url.path),
    details={"template": template_name},
)
```

The service:
- Resolves actor identity (user > agent > system > mcp_client) into the
  `actor_type` / `actor_id` / `actor_email` columns
- Generates a UUID `event_id` for deduplication
- JSON-encodes `details` before persisting
- Logs and swallows any DB error — **audit failures must never raise** so they
  cannot break the caller's primary operation
- Has dormant hash-chain code behind `_hash_chain_enabled = False` (Phase 4)

### API Endpoints (`routers/audit_log.py`)

Mounted at `/api/audit-log` to coexist with the existing `/api/audit` (Process
Engine audit). All endpoints require `Depends(require_admin)`.

| Method | Path | Description |
|---|---|---|
| GET | `/api/audit-log` | List audit entries with filters and pagination |
| GET | `/api/audit-log/stats` | Aggregate counts by event_type / actor_type |
| GET | `/api/audit-log/{event_id}` | Look up a single entry by UUID |

Filter parameters on the list endpoint: `event_type`, `actor_type`, `actor_id`,
`target_type`, `target_id`, `source`, `start_time`, `end_time`, `limit` (max 1000),
`offset`.

Route ordering: `/stats` is declared before `/{event_id}` in the router so it
isn't shadowed by the parameterized catch-all (invariant #4).

### Mounting (`main.py`)

```python
from routers.audit import router as audit_router            # Process Engine (existing)
from routers.audit_log import router as audit_log_router    # SEC-001 / #20

app.include_router(audit_router)       # /api/audit (Process Engine audit)
app.include_router(audit_log_router)   # /api/audit-log (Platform audit)
```

## Side Effects

- Inserts into `audit_log` are append-only — visible immediately to query API.
- No WebSocket events in Phase 1 (real-time audit feed is a Phase 4+ feature).
- Storage growth: ~500 bytes per entry. At 10K events/day that's ~180MB/year —
  manageable on a single SQLite file.
- Two read-only DB queries per `/api/audit-log` request (entries + count).

## Error Handling

| Failure | Behavior |
|---|---|
| DB write fails (disk full, locked, etc.) | `PlatformAuditService.log` catches, logs at ERROR with full traceback, returns `None`. **Caller is never affected.** |
| Duplicate `event_id` (UUID collision — vanishingly unlikely) | `IntegrityError` caught at the service layer; entry not written; logged. |
| UPDATE attempted on `audit_log` (anywhere in the codebase) | SQLite trigger raises `IntegrityError` with "Audit log entries cannot be modified". |
| DELETE within 365-day retention | Trigger raises with "Audit log entries cannot be deleted within retention period". |
| DELETE of >365-day entry | Allowed — supports future retention cleanup script. |
| Query returns no rows | `get_audit_entry` returns `None` → router returns 404; list endpoint returns `entries: []`. |
| Non-admin user calls any endpoint | `require_admin` raises 403 before the handler runs. |

**Invariant**: audit logging is best-effort metadata — building or writing it
can never fail the caller's primary operation. Phase 2 integration calls must
not propagate audit errors.

## Security Considerations

- **Admin-only access**: every endpoint depends on `require_admin`. Non-admin
  callers receive 403 with no information about whether the requested entry
  exists.
- **Append-only enforcement at the DB level**: SQLite triggers block UPDATE
  unconditionally and DELETE within the 365-day retention window. The triggers
  apply to direct SQL too — not just the ORM layer — so a future direct-DB
  attack still cannot tamper with history.
- **No PII in logs by design**: only metadata is stored — actor email, MCP key
  *name* (not value), user ID, agent name, endpoint path. No credential values,
  no token contents, no `.env` data, no chat message content.
- **Actor attribution always present**: `actor_type` is `NOT NULL` and the
  service falls back to `system`/`trinity-system` if no identifiable actor is
  passed, so every row has a clear "who".
- **Hash chain (Phase 4)**: dormant in Phase 1. When enabled via the toggle in
  `PlatformAuditService.__init__`, every entry's hash includes the previous
  entry's hash for tamper evidence. Verification endpoint planned for Phase 4.
- **Distinct from Process Engine audit**: the existing `/api/audit` (and its
  `audit_entries` table) is unchanged and continues to serve Process Engine
  workflow audit. The two systems coexist intentionally per the spec.

## Phase 2a — Agent Lifecycle Integration (shipped in this PR)

The first write-side integration lives in `routers/agents.py`. Four handlers
emit audit rows after a successful state transition:

| Handler | Event action | Details payload |
|---|---|---|
| `create_agent_endpoint` | `create` | `{template, base_image, agent_type}` |
| `start_agent_endpoint` | `start` | `{credentials_injection}` |
| `stop_agent_endpoint` | `stop` | `null` |
| `delete_agent_endpoint` | `delete` | `null` |

All four use the same kwargs shape:

```python
await platform_audit_service.log(
    event_type=AuditEventType.AGENT_LIFECYCLE,
    event_action="create",          # or start / stop / delete
    source="api",
    actor_user=current_user,
    actor_ip=request.client.host if request.client else None,
    target_type="agent",
    target_id=agent_name,
    endpoint=str(request.url.path),
    details={...},                  # action-specific
)
```

Log call lives in the **router**, not the service, so `current_user` stays in
scope without threading through service signatures. Placement is always
**after the state transition has committed** (container running, DB rows
deleted, etc.) — if the action fails partway, no audit row appears.

Audit failures are swallowed by the service layer and never fail the caller's
primary operation (verified by `test_service_never_raises_on_db_failure`).

## Phase 2b — Remaining Write Integrations (shipped in this PR)

### Request-ID Middleware (`main.py`)

HTTP middleware generates a UUID `X-Request-ID` for every request (or respects an
incoming header from nginx/proxy). Stored on `request.state.request_id` and
returned in the response header. All audit calls pass this for correlation.

### Authentication (`routers/auth.py`)

| Handler | Event action | Details payload |
|---|---|---|
| `login` (admin) — success | `login_success` | `{method: "admin"}` |
| `login` (admin) — failure | `login_failed` | `{method: "admin", username}` |
| `verify_email_login_code` — success | `login_success` | `{method: "email", email}` |
| `verify_email_login_code` — failure | `login_failed` | `{method: "email", email}` |

Failed logins have no `actor_user` — the service falls back to
`actor_type=system`. The `actor_ip` is always captured for forensic queries.

### Authorization (`routers/sharing.py`)

| Handler | Event action | Details payload |
|---|---|---|
| `share_agent_endpoint` | `share` | `{shared_with}` |
| `unshare_agent_endpoint` | `unshare` | `{removed_email}` |
| `decide_access_request_endpoint` (approve) | `access_request_approved` | `{email, access_request_id}` |
| `decide_access_request_endpoint` (reject) | `access_request_rejected` | `{email, access_request_id}` |

### Credentials (`routers/credentials.py`)

| Handler | Event action | Details payload |
|---|---|---|
| `inject_credentials` | `inject` | `{files: [".env", ...]}` |
| `export_credentials` | `export` | `{files_exported: N}` |
| `import_credentials` | `import` | `{files_imported: [".env", ...]}` |

Credential *values* are never logged — only file paths and counts (security invariant).

### Configuration (`routers/settings.py`)

| Handler | Event action | Details payload |
|---|---|---|
| `update_anthropic_key` | `settings_change` | `{setting: "anthropic_api_key", action: "update"}` |
| `delete_anthropic_key` | `settings_change` | `{setting: "anthropic_api_key", action: "delete"}` |
| `update_github_pat` | `settings_change` | `{setting: "github_pat", action: "update"}` |
| `delete_github_pat` | `settings_change` | `{setting: "github_pat", action: "delete"}` |
| `update_setting` (generic) | `settings_change` | `{setting: key, action: "update"}` |
| `delete_setting` (generic) | `settings_change` | `{setting: key, action: "delete"}` |

API key *values* are never logged — only the setting name and action.

### Agent Rename (`routers/agent_rename.py`)

| Handler | Event action | Details payload |
|---|---|---|
| `rename_agent_endpoint` | `rename` | `{old_name, new_name}` |

Uses `AGENT_LIFECYCLE` event type. Target ID is set to the *new* name.

## Phase 3 — MCP Server Tool Call Audit (shipped in this PR)

### Architecture

MCP tool calls are audited via a transparent wrapper — no individual tool
files need modification. The flow:

```
Tool execute() → withAudit() wrapper → logToolCall() → POST /api/internal/audit
```

### Files

| File | Purpose |
|---|---|
| `src/mcp-server/src/audit.ts` | `withAudit()` wrapper + `logToolCall()` — fire-and-forget POST to backend |
| `src/mcp-server/src/server.ts` | `addAllTools()` wraps every tool with `withAudit()` at registration |
| `src/backend/routers/internal.py` | `POST /api/internal/audit` — receives MCP audit entries (C-003 auth) |
| `docker-compose.yml` | `INTERNAL_API_SECRET` env var added to mcp-server service |

### Audit Wrapper (`withAudit`)

Wraps each tool's `execute` function:
1. Records start time
2. Calls original execute
3. Fires non-blocking `logToolCall()` with tool name, auth context, duration, success
4. Returns original result (or re-throws error)

Never blocks or delays tool execution. Never throws.

### Internal Audit Endpoint

`POST /api/internal/audit` accepts:
```json
{
  "event_type": "mcp_operation",
  "event_action": "tool_call",
  "source": "mcp",
  "mcp_key_id": "key-42",
  "mcp_key_name": "dev-key",
  "mcp_scope": "user",
  "actor_agent_name": null,
  "details": {"tool": "list_agents", "duration_ms": 150, "success": true}
}
```

Authenticated via `X-Internal-Secret` header (C-003), same as scheduler.

### Coverage

All 66+ MCP tools across 14 modules are wrapped automatically. Adding new
tools to any module requires zero audit code — `addAllTools()` wraps them.

## Phase 6 — Request-ID Correlation + Target Recovery (#905)

Closes two gaps in the MCP `mcp_operation` row, end-to-end. Full flow lives in
[mcp-git-tools.md](mcp-git-tools.md); the audit-side mechanics:

- **`target_id` + `request_id` recovered** — `withAudit()` previously dropped
  both ("we don't have params here"). `audit.ts` now resolves the target from
  params (`resolveTargetId()` → `agent_name ?? name`) and reads a per-call
  `requestId` off the shared `ToolCallContext`, passing both into
  `logToolCall()`. So every tool's row attributes the action to a `target_id`,
  and a tool that mints a correlation id records it.
- **Cross-surface join** — a git tool mints `requestId = randomUUID()`, stamps
  it on its `mcp_operation` row, AND forwards it as `X-Request-ID` on the
  proxied backend call. The backend's request-id middleware adopts the incoming
  header (rather than minting fresh), so the resulting `git_operation` row
  carries the **same** id. `GET /api/audit-log?request_id=<id>` returns both
  rows. (`db/audit.py` `_filter_conditions` gained the `request_id` filter;
  `routers/audit_log.py` exposes the `request_id` query param;
  `InternalAuditRequest.request_id` carries it on the MCP write path.)
- **Symmetric git auditing** — `routers/git.py` `_audit_git()` now logs the
  **failure** paths of mutating/destructive ops (`sync`/`pull`/
  `reset_to_main_preserve_state`) too — a 409 conflict / 400 / 500 is no longer
  silently un-audited. One row per handler exit, success or failure.

## Phase 4 — Hash Chain + Export (shipped in this PR)

### Hash Chain Verification

Opt-in via `POST /api/audit-log/hash-chain/enable?enabled=true` (admin-only).
When enabled:
- Each new entry gets `entry_hash` (SHA-256 of event_id, event_type, event_action,
  actor_id, target_id, timestamp, details, previous_hash)
- `previous_hash` links to the prior entry's hash

Verify with `POST /api/audit-log/verify?start_id=1&end_id=100`:
```json
{"valid": true, "checked": 100, "first_invalid_id": null}
```

Entries written before hash chain was enabled are skipped during verification.

### Export

`GET /api/audit-log/export?start_time=...&end_time=...&format=json|csv`

- **JSON**: Returns `{entries: [...], count: N, format: "json"}`
- **CSV**: Returns downloadable CSV file with `Content-Disposition: attachment`

### Endpoints Added

| Method | Path | Description |
|---|---|---|
| POST | `/api/audit-log/verify` | Verify hash chain integrity (admin) |
| POST | `/api/audit-log/hash-chain/enable` | Toggle hash chain (admin) |
| GET | `/api/audit-log/export` | Export entries as JSON or CSV (admin) |

## Out of Scope (future follow-ups)

- Retention automation (cron job to delete entries >365 days)
- Frontend admin UI for browsing audit log
- Unified query surface spanning `audit_log` + Process Engine `audit_entries`
- WebSocket events for real-time audit feed

## Testing

`tests/test_audit_log_unit.py` — 51 unit tests covering:

| Area | Tests |
|---|---|
| Insert / fetch | basic insert, JSON details serialization, unique event_id, missing-entry returns None |
| Append-only triggers | UPDATE blocked, DELETE within retention blocked, DELETE of >1yr-old entry allowed |
| Query filters | by event_type, actor_type, actor_id, target_type+target_id, time range |
| Ordering & pagination | newest-first, limit+offset, count independent of pagination |
| Range query | `get_audit_entries_range` for Phase 4 hash chain |
| Stats aggregation | total + by_event_type + by_actor_type |
| Service actor resolution | user, agent, mcp_client, system mcp_scope |
| Service serialization | JSON details encoding, request metadata (ip / request_id / endpoint) |
| Error contract | DB failure must return None and never raise; unique event_ids across rapid calls |
| Lifecycle integration | create / start / stop / delete / rename produce correctly-shaped rows |
| Auth integration | login_success (admin + email), login_failed (no actor fallback) |
| Sharing integration | share, unshare, access_request_approved, access_request_rejected |
| Credentials integration | inject, export, import with file lists |
| Configuration integration | settings_change for update and delete |
| Request-ID propagation | request_id stored and queryable |
| Cross-category query | mixed event types are independently queryable; stats aggregate all |
| MCP tool call audit | tool_call with user/agent scope, success/failure, details |
| Hash chain | entries get hashes when enabled; verify_chain validates integrity |
| Export | time-range query for export (DB layer) |

Run: `.venv/bin/python -m pytest tests/test_audit_log_unit.py -v`

### Phase 5 — Dashboard tests (#941)

`tests/unit/test_847_audit_dashboard.py` — covers the two new distinct
endpoints + the router-ordering invariant:

| Test | Asserts |
|---|---|
| `test_distinct_event_types_empty_table_returns_empty_list` | empty table → `[]`, no exception |
| `test_distinct_event_types_returns_sorted_unique_list` | duplicates collapsed, sorted ASCII |
| `test_distinct_actor_types_empty_table_returns_empty_list` | same, actor_types column |
| `test_distinct_actor_types_returns_sorted_unique_list` | same, actor_types column |
| `test_distinct_endpoints_admin_gated_and_before_catch_all` | `Depends(require_admin)` present; declared BEFORE `/{event_id}` (invariant #4) |
| `test_heatmap_endpoint_registered_before_catch_all` | `/heatmap` admin-gated and declared BEFORE `/{event_id}` (invariant #4) |
| `test_heatmap_buckets_by_dow_and_hour` | strftime buckets seeded rows into the correct (dow, hour) cells |
| `test_heatmap_honors_time_and_event_type_filter` | filters narrow aggregation; time-window scoping works |
| `test_heatmap_empty_window_returns_zero_total` | empty window → `total=0`, `max_count=0`, `cells=[]` (no exception) |
| `test_calendar_endpoint_registered_before_catch_all` | `/calendar` admin-gated and declared BEFORE `/{event_id}` (invariant #4) |
| `test_calendar_buckets_per_day` | same-date timestamps collapse into one cell; sorted ascending |
| `test_calendar_honors_time_and_event_type_filters` | filters narrow per-day aggregation in lockstep with `/heatmap` |
| `test_calendar_empty_window_returns_zero_total` | empty window → `total=0`, `max_count=0`, `days=[]` |
| `test_distinct_endpoints_do_not_apply_entitlement_gate` | static pin: backend stays OSS (no `requires_entitlement` on audit_log endpoints) |

`src/frontend/e2e/audit-dashboard.spec.js` — Playwright `@smoke`
suite, 4 cases: admin sees Enterprise nav + audit card available;
click card lands on `/enterprise/audit`; row click opens side panel
with hash chain disclosure; filter dropdown populated from
distinct endpoint.

Run pytest: `.venv/bin/python -m pytest tests/unit/test_847_audit_dashboard.py tests/unit/test_847_entitlement_seam.py -v`
Run e2e: `cd src/frontend && npm run test:e2e -- audit-dashboard.spec`

## Related Flows
- `docs/requirements/AUDIT_TRAIL_ARCHITECTURE.md` — full SEC-001 spec (840 lines, all four phases)
- `services/process_engine/services/audit.py` — the separate Process Engine audit service this coexists with
- `routers/audit.py` — existing `/api/audit` endpoint that exposes the Process Engine audit (unchanged)

## Architectural Compliance

- **Invariant #1** (Router → Service → DB): router holds only HTTP concerns, service holds only logic, db/audit.py holds only SQL
- **Invariant #2** (Class-per-domain DB ops with composition): `PlatformAuditOperations` composed into `DatabaseManager`
- **Invariant #3** (Schema in schema.py, migrations in migrations.py): both updated together
- **Invariant #4** (Router registration order): `/stats` before `/{event_id}` in router declaration
- **Invariant #8** (Auth pattern): every endpoint uses `Depends(require_admin)`
- **Invariant #14** (Pydantic models centralized): all audit response models live in `src/backend/models.py` (`AuditLogEntry`/`AuditLogListResponse`/`AuditLogStatsResponse` at `models.py:947-982`), centralized out of `routers/audit_log.py` per #654
