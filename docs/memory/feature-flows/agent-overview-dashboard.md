# Feature Flow: Agent Detail Overview (#1107, rebuilt in trinity-enterprise#1317)

> The default landing tab on Agent Detail — a page about the agent: what it is
> doing now, what it has done, how well, what it reports about its own job, and
> whether it is healthy. Every section keeps one footprint across loading,
> empty, failed and ready, so an agent with no runs, no schedules, no declared
> metrics and no health checks still gets the whole page.

## Design split (non-duplication)

The persistent `AgentHeader` (above the tabs) owns identity, live CPU/MEM,
**cost**, git controls, autonomy/read-only/auth chips and the circuit badge.
The Overview re-renders none of it — there is no cost tile, and a circuit that
is open is one badge in the Health card pointing at the header.

The deployment compatibility report (#668) is **not** on this tab (operator
ruling 2026-11-20): `CompatibilityPanel.vue` is mounted in Settings › General
only, and the Overview neither renders nor reads it. Consequence, accepted: the
stored report refreshes on deploy or when that pane is opened.

## Sections

| # | Section | Component | Source(s) |
|---|---------|-----------|-----------|
| 1 | **Now** — state, next scheduled run, "N items need attention", the one primary action | `overview/OverviewNow.vue` | `recent`, `live`, `schedules`, `notifications` + `asks` + `sync` |
| 2 | **Activity** — runs per day stacked by trigger bucket, failure rail, 7d/14d/30d | `overview/OverviewActivity.vue` | `analytics` |
| 2 | **Recent work** — last five runs | `overview/OverviewRecent.vue` | `recent` |
| 3 | **Finished · Time per run · Context used** | `overview/OverviewMeasureTile.vue` ×3 | `analytics` |
| 4 | **Reported by this agent** — declared metrics as plain tiles | `overview/OverviewReported.vue` | `metrics` |
| 5 | **Schedules** — next run, outcome over the window, avg duration | `overview/OverviewSchedules.vue` | `summary` + `schedules` |
| 5 | **Health** — verdict, 24h reachability, strip, latency, restarts, sync line | `overview/OverviewHealth.vue` | `health`, `history`, `sync` |

## UI → API

```
/agents/{name}?tab=overview  (AgentDetail.vue → OverviewPanel.vue — layout and wiring only)
  └── stores/agentOverview.js  — one SOURCE per read, each { data, hasLoaded, error }
        analytics      GET /api/agents/{name}/analytics?window=7d|14d|30d
        summary        GET /api/agents/{name}/schedules/analytics-summary?window=…
        schedules      GET /api/agents/{name}/schedules            (next_run_at, enabled)
        live           GET /api/executions/stats?agent={name}
        recent         GET /api/executions?agent={name}&limit=5
        notifications  GET /api/agents/{name}/notifications/count
        asks           GET /api/operator-queue?agent_name={name}&status=pending&limit=1
                         (#3302: reads `total`; an unverified total with more rows is
                          followed by ONE pending read through utils/operatorQueueRead)
        sync           GET /api/agents/{name}/git/sync-state
        health         GET /api/monitoring/agents/{name}
        history        GET /api/monitoring/agents/{name}/history?check_type=network&hours=168&limit=1000
        metrics        agentsStore.getAgentMetrics(name)  → GET /api/agents/{name}/metrics
```

Reads go through `api.js` (in-flight GET de-duplication), except `metrics`,
which reuses the agents store's own de-duplicated reader. `load(name)` reads
every source; `setWindow(w)` re-reads `analytics` + `summary` only;
`refreshLive()` re-reads `live`, `recent`, `notifications`, `asks` every 30 s
while the tab is on screen and the document is visible. AgentDetail is
KeepAlive'd, so the timers stop in `onDeactivated` and restart (with a refresh)
in `onActivated`; the panel itself is mounted by `v-if`, so mount starts them.

## Frontend Layer

### State rules (`stores/agentOverview.js`)

- **A different agent blanks every source first** — in `load()`, and in
  `OverviewPanel`'s setup before the first render, because the store outlives
  the component. A read that fails for the new agent leaves that section
  failed, never showing the previous agent's figure (#3815).
- **A response is written only if its agent is still on screen** — each write
  re-checks `agent` after its await.
- **Only the newest read of a source may write** — each read takes a sequence
  number, so 30d-then-7d cannot end with 30d's numbers under a 7d label. The
  two windowed sources record the window their data is for; labels read that,
  and a window switch whose read fails puts the control back.
- **A refresh keeps data in place**, and keeps it if the refresh fails (`error`
  set, `hasLoaded` still true → the section stays up; Activity says it is stale).

Each section's state comes from `utils/loadingState.js::viewState` over its
source: `loading` (no data yet) · `failed` (`LoadFailed` + retry) · `empty`
(the read succeeded and returned nothing) · `ready`.

### Shaping (`utils/overviewShape.js`, pure)

`activityColumns` / `activityReadout` / `ghostColumns` · `linePaths` (a `null`
day breaks the line into subpaths — a gap, never a point at zero) ·
`measureTiles` · `nowState` / `nextRun` · `recentRows` · `scheduleRows` ·
`healthBlocks` · `attentionCount`. Three rules live here: a value never
measured is `—` (never `0` or `0%`); a rate carries its denominator
("18 of 20 runs", trinity-enterprise#227); the schedules row states its outcome
over the window ("9 of 10 finished in 14d") because the API has no per-run
history.

### Empty and loading faces

| Surface | No data yet | Succeeded, nothing there |
|---------|-------------|--------------------------|
| Activity chart | `ScanlineReveal` track | dashed ghost bars + "Nothing has run yet…"; a zero-run day inside an active window is a baseline stub |
| Measure tile chart | `ScanlineReveal` track, value `—` | dashed ghost curve, value `—`, "after the first run" |
| Health strip | `ScanlineReveal` track | 24 dashed placeholder blocks |
| Recent · Schedules · Reported | `SkeletonLoader` | what will appear + one next action |

`ScanlineReveal` adopters are allowlisted in
`tests/unit/portalLoadingTreatment.spec.js`; the branch lives inside the slot
and nothing renders in it while loading.

### Activity readout

There is no floating tooltip. One reserved line under the title shows the
window summary at rest and one day's breakdown while that day is hovered or
reached with ←/→/Home/End on the focused plot (Esc clears). Nothing is appended
outside the panel, so nothing can outlive the page (#3591).

### Colours

Series colours come from `utils/executionBuckets.js::BUCKET_COLORS` through
style bindings (now with entries for Reminders, Rooms and Operator queue,
#3597). Everything else is semantic Tailwind tokens; the new files carry zero
raw non-gray palette classes.

## Backend Layer

### Router: `routers/analytics.py`
**`GET /api/agents/{name}/analytics`** (`AuthorizedAgent`, read-only)
- `?window=` mapped via `_WINDOW_HOURS = {"7d":168, "14d":336, "30d":720}`;
  anything else → 422.
- Returns `AgentAnalyticsResponse` from `db.get_agent_analytics(name, hours)`.
- All values DB-sourced → renders even when the agent is stopped.
- Mounted in `main.py` after `executions_router`. (`/{name}/analytics` differs in
  path depth from the `/{name}` catch-all, so registration order is not load-bearing.)

### Models: `models.py`
`AgentAnalyticsResponse` (+ `DurationStats`, `AgentTypeTotal`,
`AgentAnalyticsTimelinePoint`). Key fields: `total_executions`, terminal
`success_rate`, `duration_ms{avg,p95}`, `context_avg`, `context_pct_avg`, `by_type[]` (per-bucket
window totals), `buckets[]` (legend/stack order), `timeline[]` (gap-filled UTC
days, each with `by_type`, `success_rate`, `duration_avg_ms`, `context_avg`, `context_pct_avg`),
`sampled`/`sample_size`.

## Data Layer

### `db/schedules.py:ScheduleOperations.get_agent_analytics(agent_name, hours)`
Generalises `get_schedule_analytics` (#868) to agent scope with a `triggered_by`
breakdown. Delegated through `database.py:DatabaseManager.get_agent_analytics`.

Four queries over `schedule_executions` (all indexed by
`idx_executions_agent_started(agent_name, started_at DESC)`):
1. **counts + per-day type stacks** — `GROUP BY day, status, triggered_by`.
2. **per-day duration AVG (success only) + context AVG (NULL-skipped)** —
   `AVG(CASE WHEN status='success' THEN duration_ms END)`, `AVG(context_used)`,
   `GROUP BY day`.
3. **overall duration AVG + context AVG** — single row, full set.
4. **capped success-duration pool** — newest `_PERCENTILE_ROWSET_CAP` (5000) for
   the headline **p95 only** (`statistics.quantiles(n=100, method='inclusive')`).

**Locked correctness rules (from the /autoplan review):**
- Headline `avg` / `context_avg` come from the **full set** (queries 2/3), never
  the capped pool — an average over a sampled subset is silently wrong on
  high-traffic agents. Only `p95` is sampled.
- `success_rate` is **terminal-based**: `success / (success + failed)` where
  `failed = status in ('failed','error')`. Days with zero terminal rows report
  `success_rate=null` (chart renders a gap, not a false 0%).
- `context_avg` uses **NULL-skipping** `AVG` (unmeasured rows don't read as 0).
- `context_pct_avg` (trinity-enterprise#1317) is `AVG(context_used * 100.0 / context_max)` taken
  per row in queries 2/3, under a `CASE` that skips rows with no known window (NULL or 0) —
  the share of each run's OWN window, averaged, rounded to an int. The UI clamps it at 100.
- Day buckets are UTC; the timeline is **Python gap-filled** so zero-days render.

### Trigger bucketing (`_TRIGGER_BUCKETS` + `_bucket_for_trigger`)
Raw `triggered_by` → user-facing buckets, in Python (not a SQL `CASE`):
| Bucket | Raw triggers |
|--------|--------------|
| Chat/Tasks | chat, manual, user, session, self_chat |
| MCP | mcp |
| Channels | telegram, slack, whatsapp |
| Public | public, paid |
| Scheduled | schedule, webhook |
| Loops | loop (#1150 — distinct fuchsia accent so loop bursts don't read as cron load) |
| Agent-to-agent | agent, fan_out, self_task |
| Voice | voip, voice |
| **Other** | anything unmapped (catch-all — a new trigger type never vanishes) |

## Health card sourcing

- **Headline + facts**: `GET /api/monitoring/agents/{name}` (`AgentHealthDetail`)
  — `aggregate_status`, `uptime_percent_24h`, `avg_latency_24h_ms`,
  `docker.restart_count` / `oom_killed`, `circuit_breaker.open`.
- **Strip**: the history read returns the newest 1000 network checks, which at
  the default 30 s cadence is about 8 hours — so `healthBlocks` buckets the span
  the checks actually cover into 24 blocks and labels its left edge with the
  real age of the oldest check. It never claims 7 days. With at most 24 checks
  (monitoring just started) each check is its own block, packed against "now".
- **Nothing recorded**: only when the detail has no 24h figures *and* the
  history is empty does the card say so; an admin gets the link to
  Operations › Health, anyone else is told an admin can turn it on (#1409).
- A history read that failed says the history could not be loaded; it does not
  claim there are no checks.

## Info tab redesign (historical — the Info tab became Settings › General in trinity-enterprise#887)

`components/InfoPanel.vue` leads with the **About** narrative (display name,
tagline, description) + "What You Can Ask" above the fold; the exhaustive
`template.yaml` metadata (resources, sub-agents, commands, MCP servers, skills,
template tags — `capabilities:`, shown raw since trinity-enterprise#584 —
platforms, tools) is tucked behind a native collapsible
`<details>` **"Technical details"** disclosure (gated on `hasTechnicalDetails`).

## AgentDetail wiring
- `activeTab` defaults to `'overview'`; `{id:'overview',label:'Overview'}` is the
  first entry in `visibleTabs`.
- The three tab-validity sites were deduped into one `DEEP_LINK_TABS` constant
  (includes `'overview'`); the invalid-tab fallback resets to `'overview'`.

## Schedules card (#1115)

Rows come from `GET /api/agents/{name}/schedules/analytics-summary` (one rollup
per schedule for the window) joined client-side to `GET /api/agents/{name}/schedules`
for `next_run_at`. A disabled schedule reads "paused"; a schedule with no
finished run shows a dashed track and "no finished runs yet". Each row opens
the Schedules tab. The list scrolls inside the card past ~5 rows.

## Testing

- `tests/unit/test_agent_analytics.py` — the analytics query, including
  `context_pct_avg` (share of each run's own window; runs with no known window
  skipped; `None` when nothing measured) and the locked per-point key set.
- `src/frontend/tests/unit/overviewShape.spec.js` — the shaping rules.
- `src/frontend/tests/unit/overviewPanel.mount.spec.js` — mounted: the new agent
  with nothing to show (every section present, ghosts, dashes, empty copy and
  its actions), the active agent, hover and keyboard readout, window switch
  without re-entering loading, failed reads, retry, and agent-switch isolation
  (#3815). Also asserts no compatibility read is made.
- `src/frontend/tests/unit/overviewPanelSync.spec.js`,
  `overviewPanelQueueCount3302.spec.js` — the sync line and the attention count,
  ported unchanged in what they assert.

## Related Flows
- [executions-dashboard.md](executions-dashboard.md) — fleet-level sibling over the same
  `schedule_executions` data (the Overview has its own store, `agentOverview.js`).
- [scheduling.md](scheduling.md) — `schedule_executions` is the data source;
  `get_schedule_analytics` (#868) is the per-schedule analytics this generalises.
- [agent-info-display.md](agent-info-display.md) — the Info tab redesigned here.
