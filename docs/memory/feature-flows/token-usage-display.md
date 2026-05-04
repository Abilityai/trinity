# Feature: Token Usage Display (Issue #250)

> **Created**: 2026-05-03 — Per-agent cost and token consumption sourced from the database, displayed in AgentHeader with 7-day sparkline and trend vs average.

## Overview

Displays accumulated LLM cost and token usage per agent in the Agent Detail page header. Data is sourced exclusively from the database (`schedule_executions` table) so it persists across agent restarts. Shows today's cost, a 7-day sparkline, trend vs daily average, and lifetime totals.

---

## User Story

As an operator, I want to see how much each agent has cost over time — today vs the 7-day average — so I can identify unexpectedly expensive agents and monitor cost trends.

---

## Entry Points

- **UI**: `src/frontend/src/views/AgentDetail.vue` — loads token stats on mount
- **Rendered in**: `src/frontend/src/components/AgentHeader.vue` — TOKEN USAGE ROW (between stats row and git row)
- **API**: `GET /api/agents/{name}/token-stats`

---

## Data Flow

```
schedule_executions table
  ├─ cost REAL         — cost per execution in USD
  ├─ context_used INT  — context tokens at end of session
  └─ started_at TEXT   — ISO-Z timestamp

DB method: ScheduleOperations.get_agent_token_stats(agent_name)
  src/backend/db/schedules.py

DB facade: database.py → get_agent_token_stats()

Router: GET /api/agents/{name}/token-stats
  src/backend/routers/agents.py
  auth: AuthorizedAgentByName + CurrentUser

Store action: agentsStore.getAgentTokenStats(name)
  src/frontend/src/stores/agents.js

View: AgentDetail.vue onMounted (Promise.allSettled, non-critical)
  → tokenStats ref → :token-stats prop on AgentHeader

Component: AgentHeader.vue TOKEN USAGE ROW
  src/frontend/src/components/AgentHeader.vue
```

---

## Backend Layer

### DB Method (`src/backend/db/schedules.py`)

`ScheduleOperations.get_agent_token_stats(agent_name: str) -> Dict`

Two SQL queries:
1. Single-pass aggregation for lifetime, 24h, and 7d windows using `CASE WHEN started_at > ?` with `iso_cutoff()` helpers
2. `GROUP BY DATE(started_at)` for per-day breakdown (last 7 days)

Gap-filling: iterates days 6..0 (oldest→today), zero-fills missing dates so sparkline always has exactly 7 data points.

**Returns:**
```python
{
  "lifetime_cost": float,
  "lifetime_context_tokens": int,
  "lifetime_executions": int,
  "cost_24h": float,
  "context_tokens_24h": int,
  "executions_24h": int,
  "cost_7d": float,
  "context_tokens_7d": int,
  "executions_7d": int,
  "avg_daily_cost": float,       # cost_7d / 7.0
  "trend_cost_pct": float,       # (cost_24h - avg_daily_cost) / avg_daily_cost * 100
  "daily_breakdown": [           # 7 items, oldest first
    {"date": "YYYY-MM-DD", "cost": float, "context_tokens": int, "executions": int},
    ...
  ]
}
```

**Note**: Only `schedule_executions` is queried. `chat_sessions` (interactive chat) is not included.

**Time-window invariant**: Uses `iso_cutoff(hours)` from `utils/helpers.py` (ISO-Z format), never `datetime('now', ...)` (see Architectural Invariant #16).

### Router (`src/backend/routers/agents.py`)

```python
@router.get("/{agent_name}/token-stats")
async def get_agent_token_stats(
    agent_name: AuthorizedAgentByName,
    current_user: CurrentUser,
):
    return db.get_agent_token_stats(agent_name)
```

Inserted after `GET /{agent_name}/stats` to maintain route ordering.

---

## Frontend Layer

### Store (`src/frontend/src/stores/agents.js`)

```javascript
async getAgentTokenStats(name) {
  const response = await axios.get(`/api/agents/${name}/token-stats`, {
    headers: authStore.authHeader
  })
  return response.data
}
```

### View (`src/frontend/src/views/AgentDetail.vue`)

- `const tokenStats = ref(null)` — loaded in `onMounted` as part of `Promise.allSettled` (failure is non-critical, row simply stays hidden)
- Reset to `null` and reloaded in the route watcher when navigating between agents
- Passed as `:token-stats="tokenStats"` prop to `<AgentHeader>`

### Component (`src/frontend/src/components/AgentHeader.vue`)

TOKEN USAGE ROW renders between the stats row and the git row.

**Visibility guard**: `v-if="tokenStats && tokenStats.lifetime_executions > 0"` — hidden for new agents with no runs.

**Layout:**
- Left: `SparklineChart` (amber `#f59e0b`, 56×16 px, 7 data points from `daily_breakdown`) + "Today $X.XX" label
- Center: Trend indicator (SVG arrow icon + percentage), color-coded:
  - `>5%` → warning amber (cost rising)
  - `<-5%` → success green (cost falling)
  - else → gray (flat)
  - Hidden entirely when `avg_daily_cost < 0.0001` (prevents noise for agents with near-zero cost)
- Right: "Lifetime $X.XX · N runs"

**Computed properties:**
```javascript
tokenCostSparkline    // daily_breakdown.map(d => d.cost)
tokenCostSparklineMax // Math.max(...values, 0.0001) — prevents uPlot scale collapse
trendClass            // Tailwind text color class based on trend_cost_pct
```

**Helper functions:** `formatCost(val)` (2 decimal places USD), `formatTrendPct(pct)` (1 decimal place with sign)

---

## Trend Math

```
avg_daily_cost = cost_7d / 7.0
trend_cost_pct = (cost_24h - avg_daily_cost) / avg_daily_cost × 100
```

Note: `cost_7d` includes today's `cost_24h`, so the denominator slightly dampens the numerator. This is acceptable self-dampening behavior.

---

## Related Features

- `scheduling.md` — source of the `schedule_executions` rows this feature reads
- `agent-lifecycle.md` — AgentHeader.vue component context
- Context window monitoring (live session polling) is a separate, orthogonal feature using `GET /api/agents/context-stats`
