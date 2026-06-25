# Feature Flow: Agent Detail Overview Dashboard (#1107)

> The default landing tab on Agent Detail — a deterministic, glanceable view
> of the agent's **trends over the last few days** (execution volume by type,
> success rate, duration, context), plus a compact attention badge, health
> panel, recent-activity drill-in, and footprint chips. Backed by a new
> agent-scoped analytics endpoint that generalises the #868 per-schedule query.

## Design split (non-duplication)

The persistent `AgentHeader` (above the tabs) owns **"now + cost"** — live
CPU/MEM sparklines, cost cards, git controls, autonomy/read-only/auth chips,
circuit badge. The Overview owns **"trend over the window"**. The Overview
deliberately does **not** re-render any header element; where it references one
(e.g. circuit open) it links up to the header control.

## UI → API → Database

```
/agents/{name}?tab=overview  (AgentDetail.vue → OverviewPanel.vue)
  ├── mount → loadAnalytics()  → executionsStore.fetchAgentAnalytics(name, window)
  │                              → GET /api/agents/{name}/analytics?window=7d|14d|30d
  │                              (cached per `${name}:${window}`; never polled)
  ├── mount → loadSidecars()   → Promise.allSettled([
  │      GET /api/executions/stats?agent=name          (live running/queued)
  │      GET /api/agents/{name}/notifications/count     ┐
  │      GET /api/operator-queue/agents/{name}?status=pending  ├ attention badge
  │      GET /api/agents/{name}/git/sync-state          ┘ (count + link only)
  │      GET /api/monitoring/agents/{name}              (current health indicators)
  │      GET /api/monitoring/agents/{name}/history?check_type=network (uptime/latency trend)
  │      GET /api/agents/{name}/schedules               ┐ footprint chips
  │      GET /api/agents/{name}/skills                  ┘
  │      GET /api/executions?agent=name&limit=5         (recent-activity mini-list)
  │      GET /api/agents/{name}/info ])                 (About lead)
  ├── window selector 7/14/30d → watch(window) → loadAnalytics()  (per-window cache)
  ├── "New task" / "Full details →" → emit navigate-tab(tab)  → AgentDetail.activeTab
  └── recent row click → emit open-task(execId) → activeTab='tasks' + ?execution=
```

## Frontend Layer

### Components
- `components/OverviewPanel.vue` — the tab. Sections: About lead (+ deep-links),
  attention badge (count + link to `/operating-room`, hidden at zero), trend
  charts, Health & reliability, Recent activity, Footprint. Emits `navigate-tab`
  and `open-task` to `AgentDetail.vue`.
- `components/StackedBarChart.vue` — executions-by-type, **CSS/flexbox** stacked
  bars (NOT uPlot bars — chosen for correct-by-construction per-segment
  tooltips, theme-aware colors, no cumulative-stacking math). One column/day,
  ≤9 buckets, hover shows the per-bucket breakdown, legend with window totals.
  Buckets missing from the `colors` prop render slate (`#94a3b8`) rather than
  invisible — covers a stale cached bundle against a newer backend (#1150).
- `components/TrendLineChart.vue` — uPlot line/area with axes + cursor. Dark-mode
  aware (axis/grid strokes re-resolved on theme toggle). uPlot's built-in legend
  is **disabled** (it reflows the layout on hover → labels jump); replaced by a
  custom absolutely-positioned cursor tooltip appended to `u.root` (zero layout
  impact). `null` points render as gaps. Used for success-rate %, duration avg,
  context, uptime %, latency.

Palette: an **analogous cool ramp** (indigo `action-primary` → violet → blue →
sky → cyan → teal → emerald, + slate `Other`) — design-system aligned, no warm
hues (avoids a "traffic light" read).

### State Management
- `stores/executions.js` — `fetchAgentAnalytics(name, window, {force})`. Caches
  the response in `analyticsCache[`${name}:${window}`]`; returns the cached
  payload unless `force` or uncached. **Never** wired into the stats poll — the
  charts are historical and refetch only on window change / forced refresh.

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
`success_rate`, `duration_ms{avg,p95}`, `context_avg`, `by_type[]` (per-bucket
window totals), `buckets[]` (legend/stack order), `timeline[]` (gap-filled UTC
days, each with `by_type`, `success_rate`, `duration_avg_ms`, `context_avg`),
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

## Health panel sourcing (7-day cap)

Current indicators (status, reachable, restart count, OOM, 24h uptime/latency)
come from `GET /api/monitoring/agents/{name}`. The uptime/latency **trend lines**
are derived client-side from `…/history?check_type=network` and are inherently
**≤7 days** — `agent_health_checks` is purged at `health_check_retention_days`
(default 7) and the history endpoint caps `hours ≤ 168`. The panel labels them
"last 7 days" even on a 14/30d window. Reliability-over-time for the full window
is the success-rate chart (from `schedule_executions`, retained ~90d) — a
deliberately separate source so the execution charts aren't clipped.

When there is no `agent_health_checks` history at all (e.g. the Monitoring
Service was never enabled), the trend block's `v-else` renders a one-line empty
state — "No health data available — the monitoring service may be off." —
instead of a silently empty section (fix 6df68c96, 2026-06-10).

## Info tab redesign

`components/InfoPanel.vue` leads with the **About** narrative (display name,
tagline, description) + "What You Can Ask" above the fold; the exhaustive
`template.yaml` metadata (resources, sub-agents, commands, MCP servers, skills,
capabilities, platforms, tools) is tucked behind a native collapsible
`<details>` **"Technical details"** disclosure (gated on `hasTechnicalDetails`).

## AgentDetail wiring
- `activeTab` defaults to `'overview'`; `{id:'overview',label:'Overview'}` is the
  first entry in `visibleTabs`.
- The three tab-validity sites were deduped into one `DEEP_LINK_TABS` constant
  (includes `'overview'`); the invalid-tab fallback resets to `'overview'`.

## Schedules performance section (#1115)
Below the trend charts, a **Schedules performance** section lists the agent's
non-deleted schedules — one compact scorecard each (command/name + cron,
terminal success rate, avg duration, runs-in-window, tool calls), honoring the
same 7/14/30d window selector. Each row deep-links to the Schedules tab (the
#868 per-schedule deep view stays the drill-in target). Hidden when the agent
has no schedules. The Schedules tab itself (`SchedulesPanel.vue`) renders the
**same** rollups as inline mini-stats per row. Both consume one
`executions.js` `fetchSchedulesSummary(name, window)` call
(`GET /api/agents/{name}/schedules/analytics-summary`, DB
`get_agent_schedules_summary`) — cached per `${name}:${window}`, no
N per-schedule round-trips.

## Testing
- `tests/unit/test_agent_analytics.py` — bucketing (incl. `Other` fallback),
  terminal-based success rate, **full-set avg vs sampled p95** correctness,
  NULL-skipping context avg, window boundary, empty agent, gap-filled timeline.
  12 tests; mirrors the #868 `test_schedule_analytics.py` fixture machinery.
- `tests/unit/test_1115_schedules_summary.py` — per-schedule rollups:
  terminal success rate (incl. `error`, non-terminal excluded), NULL-skipping
  avg duration, tool-call total (malformed JSON skipped), zero-run schedule
  still appears (rate `None`), soft-deleted excluded, out-of-window excluded.
  6 tests.

## Related Flows
- [executions-dashboard.md](executions-dashboard.md) — fleet-level sibling; shares
  the `executions.js` store and `get_fleet_executions` query family.
- [scheduling.md](scheduling.md) — `schedule_executions` is the data source;
  `get_schedule_analytics` (#868) is the per-schedule analytics this generalises.
- [agent-info-display.md](agent-info-display.md) — the Info tab redesigned here.
