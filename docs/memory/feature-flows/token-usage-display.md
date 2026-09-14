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

### DB Method (`src/backend/db/schedules/stats.py`)

`ScheduleOperations.get_agent_token_stats(agent_name: str) -> Dict`

Two SQL queries:
1. Single-pass aggregation for lifetime, 24h, and 7d windows using `CASE WHEN started_at > ?` with `iso_cutoff()` helpers
2. `GROUP BY substr(started_at, 1, 10)` for the per-day breakdown (last 7
   days) — a SUBSTRING, never a date function, spelled exactly as
   `get_agent_analytics` spells it (see the time-window invariant below)

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

**Time-window invariant** (Architectural Invariant #16): `started_at` is an ISO-Z
TEXT column, so BOTH halves of the query treat it as text — the cutoff comes from
`iso_cutoff(hours)` in `utils/helpers.py`, never `datetime('now', ...)`, and the
day bucket is a `substr`, never a SQL date function.

The bucket half was learned the hard way (#2193). It shipped as
`GROUP BY DATE(started_at)`, which is not dialect-agnostic: SQLite returns TEXT,
while on PostgreSQL `date(x)` is the type-name-as-function cast and psycopg hands
back a `datetime.date`. The gap-fill below keys on `strftime("%Y-%m-%d")`
**strings**, and a `str`/`date` dict lookup does not raise — it MISSES. Every
bucket read as a legitimate zero, so the sparkline was permanently flat on every
PostgreSQL install while `cost_24h` / `cost_7d` / `lifetime_*` (query 1, no date
function) stayed correct. Nothing errored on either backend.

Pre-#1474 scheduler rows (`YYYY-MM-DD HH:MM:SS`, space separator, no `Z`) need
no special handling here — the separator sits at position 11, outside a 10-char
slice, so both shapes yield the same day. ent#326's **hour** bucket does wrap the
column in `replace(..., ' ', 'T')`, because its 13-char slice spans the
separator; do not copy that expression into a day bucket on the strength of the
similar name. `_day_bucket_key` normalizes the join key on the Python side, and
`tests/unit/test_2193_token_stats_day_bucket.py` AST-guards the SQL literals —
the behavioural cases cannot catch a reintroduction in CI, which runs SQLite-only
unless `TEST_POSTGRES_URL` is set.

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

## #471 Tier 0 (2026-08-19) — API-equivalent relabel for subscription-funded agents

A bare `$` on a subscription-funded agent reads as a bill; for Claude Code, `cost` is the runtime's native `total_cost_usd` — token counts at API prices. The TOKEN USAGE ROW now branches on the `authStatus` prop AgentHeader already receives for its auth chip (zero new backend):

- `auth_mode === "subscription"` → an `≈ API-equiv` badge (tooltip names the funding subscription) and `≈` prefixes on Today/Lifetime cost. API-key agents keep plain metered semantics.
- **Zeros-fork** (the #471 Step-0 design fork): if lifetime cost is 0 across real runs (cost structurally unreported under this auth), render an honest `—` with tooltip instead of `≈$0.00`. A zero *day* on a cost-reporting agent renders normally (lifetime > 0 proves the channel works). Note: Step 0 was ANSWERED live on 2026-08-19 — cost IS populated under subscription auth ($0.0687 for a one-sentence turn) — so the dash is a degradation path, not the expected state.
- Fleet surfaces (AgentTile Zone-4 cost, AgentListPanel tablet cost) prefix `≈` when the subscription-pressure batch payload reports the agent subscription-funded (`utils/subscriptionPressure.js::isSubscriptionFunded`). Per-execution cost cells elsewhere stay plain `$` — records of individual runs; a mixed-mode fleet makes a global formatter relabel wrong (`useFormatters.js` untouched).
