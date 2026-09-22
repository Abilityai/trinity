# Agent Custom Metrics - Feature Flow

> **Status (ent#477)**: `template.yaml metrics:` now has a **backend reader and
> a per-agent registry**. The declaration half of this flow is no longer
> "parsed in the container on every read, validated nowhere" — see
> [The declared metric registry](#the-declared-metric-registry-ent477) below,
> which is the current shape. The `metrics.json` VALUE path documented further
> down is **superseded** by `record_metrics` (ent#478) and re-backed by ent#479;
> it is unchanged in code and still the only source of values today.

> **Status (#2492)**: the frontend half is GONE — `MetricsPanel.vue` was deleted as unreferenced (it had zero importers; the metrics API below remains live and MCP/agent-side declarations still work). Re-adding a renderer is a feature decision, not a revert. ent#479 owns the definitions/values renderer.

> **Updated**: 2026-01-23 - Verified line numbers and added Dashboard Widget system documentation (dashboard.yaml).

**Feature ID**: 9.9
**Status**: Implemented
**Date**: 2025-12-10
**Last Updated**: 2026-09-21 (ent#477 — declared metric registry)

## The declared metric registry (ent#477)

The block an author writes is now read by the **backend**, validated, and
persisted per agent. That is what gives ent#478 a schema to validate recorded
points against and ent#479 definitions to render with.

```
template.yaml `metrics:`
  │
  ├─ create (github: / local: / snapshot import)
  │     crud.py resolver → tr.declared_metrics = metric_registry
  │                          .declared_metrics_from_template(...)
  │     crud._materialize_agent_files → reconcile_declared_metrics(source="create")
  │                                     (non-fatal, ghost-skipped, inside the
  │                                      destructive rollback fence)
  │
  ├─ git pull ✓ / reset-to-main ✓ / sync strategy=pull_first ✓
  │     routers/git.py::_refresh_metric_registry  (non-fatal; the summary goes
  │       to the log + the existing _audit_git details, NOT the response body)
  │
  ├─ container start  (T1)
  │     lifecycle.start_agent_internal → metric_registry
  │       .spawn_refresh_from_running_agent(source="start")   fire-and-forget
  │
  └─ POST /api/agents/{name}/metrics/definitions/refresh
        routers/agent_files.py  (AuthorizedAgentByName, running agent only)

  every live path ───► metric_registry.refresh_from_running_agent
                          docker exec `timeout N head -c 256K template.yaml`
                          → utils.safe_yaml.load_template_yaml
                          → services/template_metrics.normalize_declared_metrics
                          → db.metric_definitions.reconcile()
                                insert · update · revive · retire
                                (UNIQUE(agent_name, name), one transaction)
                  ▲
  GET /api/agents/{name}/metrics/definitions ── AuthorizedAgentByName ──┘
  services/compatibility/static_checks.c_d009 ── metric_shape_errors (no DB)
```

**Key properties**

| Property | Why |
|---|---|
| The reader (`services/template_metrics.py`) is a stdlib-only leaf and **never raises** | The creation path sits inside the destructive rollback fence; `template_service` imports the sibling leaves, so an import back would close a cycle |
| An entry with **any** error is dropped and named | The registry must never hold a half-valid definition — ent#478 validates points against these rows |
| An **unreadable** template changes nothing | A failed exec is absence of evidence, not "the author removed the block" (#2196). Only a template that *parsed* and carries no `metrics:` retires rows |
| A **`type` change is refused** | Points are stored by name; a shape flip makes prior points uninterpretable. `type_conflict` records the refusal and the definitions read surfaces it |
| Rows are retired, never deleted | Points recorded under a name still need a definition to interpret them |
| The live read uses `execute_command_in_container`, never the volume path | The stopped-agent read spawns a throwaway container, and no request-triggered route may create one as a side effect of a read (409 instead) |

**Files**

| Layer | File |
|---|---|
| Reader (leaf) | `src/backend/services/template_metrics.py` |
| Store | `src/backend/db/metric_definitions.py` (+ `schema.py`, `tables.py`, `migrations.py`, `migrations/versions/0069_metric_definitions.py`, `agent_cleanup.py`, `database.py` facade) |
| Service | `src/backend/services/metric_registry.py` |
| Hooks | `services/agent_service/crud.py`, `routers/git.py`, `services/agent_service/lifecycle.py` |
| Routes | `src/backend/routers/agent_files.py` |
| Compatibility | `services/compatibility/{spec,static_checks}.py` — `D-009` |

**Known gap**: an agent that runs `git pull` itself without restarting is
covered by no hook. The remedy is the refresh route, reachable by the agent as
the `refresh_metric_definitions` MCP tool (ent#478) — which is what the
undeclared-metric 422's hint names.

Requirement: `docs/memory/requirements/lifecycle-observability.md` §47.

---

## Recording points against those declarations (ent#478)

The registry says which metrics exist; this is how their values get in. There
is exactly one write path — the `record_metrics` MCP tool — and no second one.

```
agent turn (execution_id is in the Execution Context block)
  │ record_metrics(points[], idempotency_key?, execution_id?)
  ▼
mcp-server/src/tools/metrics.ts      agent-scoped key only; never throws
  │ client.recordMetrics(agent, body)
  ▼
POST /api/agents/{name}/metrics/points        routers/metric_points.py
  ├─ AuthorizedAgent + self-gate       an agent key records only as itself
  ├─ rate limit (agent_metrics:{name}) · size guard (2 MiB, post-parse)
  ├─ execution provenance              backend-confirmed, else NULL
  ├─ idempotency begin(scope, key)     key BOUND to sha256(canonical points)
  ├─ db.list_metric_definitions(name, include_retired=True)     ← the registry
  ├─ metric_points_service.validate_batch(defs, points, now)    ← pure leaf
  │     → rows[] | errors[]   422 all-or-nothing, one reason code per point
  ├─ daily write cap → 429 daily_point_cap_exceeded + Retry-After
  ├─ db.insert_metric_points(name, rows)   ON CONFLICT DO NOTHING on identity
  └─ 201 {recorded, deduplicated, replayed, points[{index, ts, key}]}

cleanup cycle (300 s)  →  _sweep_metric_points  →  guarded ts-range prune
```

**What the agent has to understand, and where it learns it.** The tool
description carries four rules, because each is one an author can get wrong in
a way the platform cannot detect afterwards: declare the metric first (the
`metric_undeclared` hint names `refresh_metric_definitions`, which ships in the
same module so the remedy is reachable from where the error is read); values
are not coerced; identity is `(metric, ts, dims)` and **excludes the value**, so
a correction is a new `ts` rather than a new number at the same one; and
passing `execution_id` is what makes a re-delivered turn replay instead of
recording twice.

**Two idempotency layers, for two different failures.** The row key
(`sha256(metric \0 ts \0 canonical_dims)`, which is also the tail of the primary
key) holds with no client key, no Redis and no execution id — it is the
invariant. The batch key gives Invariant #18's "returns the first result" for a
re-delivered turn; where no client key is supplied but `execution_id` resolves
to the calling agent, it is derived from that execution, which is what dedups a
batch of `ts`-less points whose timestamps would otherwise be freshly assigned
on the retry. With neither, a retry is a new observation — stated in the tool
description rather than papered over with a body hash, because the same numbers
an hour later are usually a genuine new observation.

**The batch key is bound to the body.** `idempotency_keys` stores no request
fingerprint, so a claim on the client key alone would make the key identify the
*caller* rather than the batch: an agent stamping a constant `idempotency_key`
on every turn gets the first batch's snapshot for 24 hours, with `replayed:
true` and no 4xx — silent metric loss, where the equivalent on `/chat` is only
a stale answer. Both branches therefore fold the canonical points payload into
the claim, so the same batch replays and a different one is recorded.

**The size guard bounds storage, not parse cost.** Starlette has buffered and
Pydantic has validated the body before the handler runs; the 2 MiB cap is
honest about being a storage bound, and the per-field
`METRIC_VALUE_TEXT_MAX_LEN` (1024) is what stops one text `value` from being
the whole batch.

**Failure is classified, not blanket.** A store outage is a 503 the tool
reports as `retryable`; a batch the database rejects on its content is a 500
the tool reports as explicitly not retryable, because an agent told to retry
that would retry forever. A refusal never ends the turn.

Requirement: `docs/memory/requirements/lifecycle-observability.md` §48.

---

## Overview

Agent Custom Metrics allows agents to define domain-specific KPIs in their `template.yaml` that Trinity displays in the UI. This enables per-agent observability beyond generic tool call counts.

Additionally, agents can create a `dashboard.yaml` file for richer widget-based dashboards with tables, lists, markdown, and more.

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
| Agent Server | `docker/base-image/agent_server/routers/info.py:148-208` | GET /api/metrics endpoint |
| Router | `src/backend/routers/agent_files.py` | GET /api/agents/{name}/metrics endpoint |
| Service | `src/backend/services/agent_service/metrics.py` (93 lines) | Metrics proxy logic |
| Frontend | `src/frontend/src/components/MetricsPanel.vue` (365 lines) | Metrics display component |
| Frontend | `src/frontend/src/views/AgentDetail.vue:88-91` | Dashboard tab content integration |
| Store | `src/frontend/src/stores/agents.js:507-513` | getAgentMetrics action |

### Backend Architecture

```python
# Router (agents.py:688-695)
@router.get("/{agent_name}/metrics")
async def get_agent_metrics(agent_name: str, request: Request, current_user: User = Depends(get_current_user)):
    """Get agent custom metrics."""
    return await get_agent_metrics_logic(agent_name, current_user)
```

```python
# Service (metrics.py:18-93)
async def get_agent_metrics_logic(agent_name: str, current_user: User) -> dict:
    """Get agent custom metrics from agent's internal API."""
    if not db.can_user_access_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, ...)
    # ... proxy to agent-server
```

---

## Dashboard Widget System (dashboard.yaml)

In addition to template-defined metrics, agents can create a `dashboard.yaml` file for richer, widget-based dashboards.

### Dashboard Flow

```
┌─────────────────────┐     ┌─────────────────────┐
│   Agent writes      │     │   User opens        │
│   dashboard.yaml    │     │   Dashboard tab     │
│   with widgets      │     │                     │
└─────────────────────┘     └─────────────────────┘
          │                           │
          ▼                           ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                       Agent Server (/api/dashboard)                          │
│   1. Read dashboard.yaml                                                     │
│   2. Validate widget types and required fields                               │
│   3. Return { has_dashboard, config, last_modified, error }                  │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                   Backend (/api/agent-dashboard/{name})                      │
│   1. Access control check                                                    │
│   2. Check agent is running                                                  │
│   3. Proxy to agent server                                                   │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                       Frontend (DashboardPanel.vue)                          │
│   1. Load dashboard when tab activated                                       │
│   2. Render sections with layout (grid/list)                                 │
│   3. Render widget types: metric, status, progress, text, markdown,          │
│      table, list, link, image, divider, spacer                               │
│   4. Auto-refresh based on config.refresh (default 30s, min 5s)              │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Widget Types

| Type | Required Fields | Description |
|------|-----------------|-------------|
| `metric` | label, value | Single numeric value with optional trend/unit |
| `status` | label, value, color | Colored status badge |
| `progress` | label, value | Progress bar (0-100) |
| `text` | content | Simple text with optional size/color/align |
| `markdown` | content | Rich text with markdown rendering |
| `table` | columns, rows | Tabular data |
| `list` | items | Bullet or numbered list |
| `link` | label, url | Clickable link or button |
| `image` | src, alt | Image display |
| `divider` | (none) | Horizontal separator |
| `spacer` | (none) | Vertical space (sm/md/lg) |

### Dashboard Schema

```yaml
# dashboard.yaml
title: "Agent Dashboard"
description: "Real-time status overview"
refresh: 30  # Auto-refresh interval in seconds (min 5)

sections:
  - title: "Key Metrics"
    layout: grid  # grid or list
    columns: 3    # 1-4 columns (for grid layout)
    widgets:
      - type: metric
        label: "Messages"
        value: 42
        unit: "total"
        trend: "up"
        trend_value: "+5"

      - type: status
        label: "Status"
        value: "Running"
        color: green

      - type: progress
        label: "Completion"
        value: 75
        color: blue

  - title: "Details"
    layout: list
    widgets:
      - type: markdown
        content: |
          ## Notes
          - Item 1
          - Item 2

      - type: table
        title: "Recent Activity"
        columns:
          - key: time
            label: "Time"
          - key: event
            label: "Event"
        rows:
          - time: "10:30"
            event: "Started"
          - time: "10:35"
            event: "Completed"
```

### Dashboard Key Files

| Component | File | Purpose |
|-----------|------|---------|
| Agent Server | `docker/base-image/agent_server/routers/dashboard.py:150-229` | GET /api/dashboard endpoint |
| Validation | `docker/base-image/agent_server/routers/dashboard.py:23-119` | Widget validation logic |
| Router | `src/backend/routers/agent_dashboard.py:19-43` | GET /api/agent-dashboard/{name} |
| Service | `src/backend/services/agent_service/dashboard.py` (107 lines) | Dashboard proxy logic |
| Frontend | `src/frontend/src/components/DashboardPanel.vue` (510 lines) | Dashboard display component |
| Frontend | `src/frontend/src/views/AgentDetail.vue:88-91` | Dashboard tab integration |
| Store | `src/frontend/src/stores/agents.js:516-522` | getAgentDashboard action |

---

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

---

## Revision History

| Date | Changes |
|------|---------|
| 2025-12-10 | Initial documentation |
| 2025-12-30 | Verified file paths, service layer refactor |
| 2026-01-23 | Updated line numbers (info.py:148-208, agents.py:688-695, agents.js:507-522), added Dashboard Widget system documentation (dashboard.yaml), added DashboardPanel.vue (510 lines), added revision history |
| 2026-09-22 | Added the write path (ent#478): `record_metrics`, the `metric_points` store, the two Settings knobs and the retention sweep |
