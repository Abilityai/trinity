# Feature: Git Sync Health Observability

## Overview

Trinity's GitHub-sync stack (`github-sync.md`) lets agents pull from and
push to GitHub, but every sync was operator-initiated and the dashboard
had no aggregate health signal. Fleets would silently drift for weeks —
documented in `ability-trinity-git-improvements-proposal.md` as problems
**P1** (silent desync accumulation) and **P6** (working-branch divergence
hidden by ahead/behind-against-main).

This flow adds:

- **Per-agent sync state** persisted in a new `agent_sync_state` table.
- A 15-minute **auto-sync heartbeat** that runs inside each GitHub-template
  agent container (non-source-mode), gated by `GIT_SYNC_AUTO=true`.
- A **sync-health service** on the backend that polls every git-enabled
  agent every 60 s, upserts the state row, and emits a `sync_failing`
  operator-queue entry when the consecutive-failures counter crosses 3.
- **Dual ahead/behind** response fields so external writes to the working
  branch become visible in the UI (fixes P6).
- A **dashboard sync-health dot** (green / yellow / red / gray) on the
  agents list.
- A fleet-level **sync-audit** endpoint with a `duplicate_binding` flag
  that catches the §P5 silent-clobber setup (two non-source-mode agents
  sharing the same `(repo, working_branch)` pair).

Per-agent opt-outs are available via API for both the auto-sync
heartbeat and schedule-freeze behaviour.

## User Stories

- **Operator**: "Show me which agents haven't synced successfully in the
  last week, without clicking each one open."
- **Operator**: "Warn me when an agent's working branch has been written
  to by someone else (peer-clobber on a shared branch)."
- **Operator**: "Automatically nudge agent containers to push their
  in-container state to GitHub so I don't have to remember."
- **Fleet admin**: "Tell me if two agents are bound to the same working
  branch — that setup causes silent data loss on force-push."

## Entry Points

| Type | Location | Description |
|------|----------|-------------|
| **UI** | Agents list view | Colored dot next to each agent (green / yellow / red / gray) with tooltip |
| **Agent loop** | `docker/base-image/agent_server/auto_sync.py` | Background heartbeat, 15-min interval, gated by `GIT_SYNC_AUTO=true` |
| **API** | `GET /api/agents/sync-health` | Batch per-agent sync-health summary for the dashboard |
| **API** | `GET /api/agents/{name}/git/sync-state` | Persisted sync-state row for one agent |
| **API** | `GET/PUT /api/agents/{name}/git/auto-sync` | Toggle the per-agent auto-sync flag |
| **API** | `GET/PUT /api/agents/{name}/git/freeze-schedules-if-failing` | Toggle the freeze-schedules-on-sync-failure flag |
| **API** | `GET /api/fleet/sync-audit` | Fleet-wide audit including `duplicate_binding` flag (admins see all; non-admins filtered) |
| **API** | `GET /api/internal/agents/{name}/sync-health-status` | Internal endpoint for the scheduler to check freeze-on-failure |
| **Operator Queue** | type=`sync_failing` | Inserted by `SyncHealthService` when `consecutive_failures` crosses 3 |

## Data Model

**New table — `agent_sync_state`** (`src/backend/db/schema.py`):

```
agent_name TEXT PRIMARY KEY
last_sync_at TEXT
last_sync_status TEXT              -- 'success' | 'failed' | 'never'
consecutive_failures INTEGER DEFAULT 0
last_error_summary TEXT
last_remote_sha_main TEXT
last_remote_sha_working TEXT
ahead_main INTEGER DEFAULT 0
behind_main INTEGER DEFAULT 0
ahead_working INTEGER DEFAULT 0
behind_working INTEGER DEFAULT 0
last_check_at TEXT
updated_at TEXT NOT NULL
FOREIGN KEY (agent_name) REFERENCES agent_ownership(agent_name)
```

Index: `idx_sync_state_status` on `(last_sync_status, consecutive_failures)`.

**New columns on `agent_git_config`**:

```
auto_sync_enabled INTEGER DEFAULT 0
freeze_schedules_if_sync_failing INTEGER DEFAULT 0
```

Migration: `sync_health` in `src/backend/db/migrations.py` (idempotent,
`PRAGMA table_info` + table-existence guards).

**Persistent file inside each agent container**:

- `.trinity/sync-state.json` — written by the agent's auto-sync loop after
  every cycle. Fields: `last_sync_status`, `last_sync_at`,
  `last_error_summary`, `consecutive_failures`. Read/merged into
  `GET /api/git/status` so the backend poller picks it up.

## Execution Flow

### 1. Auto-sync heartbeat (agent container)

```
┌──────────────────────────────┐
│ agent_server.main.py startup │
└────────────┬─────────────────┘
             │ GIT_SYNC_AUTO=true ?
             ▼
┌──────────────────────────────┐
│ auto_sync.run_auto_sync_loop │  sleeps GIT_SYNC_INTERVAL_SECONDS (900)
└────────────┬─────────────────┘
             │
             ▼
┌──────────────────────────────┐
│ routers/git._run_auto_sync_  │  git add -A
│ once(home_dir)               │  git commit (if dirty)
│                              │  git push origin HEAD
└────────────┬─────────────────┘
             │
             ▼
┌──────────────────────────────┐
│ _write_sync_state_file(…)    │  .trinity/sync-state.json
└──────────────────────────────┘
```

- Loop is guarded by `should_run_auto_sync()` (`GIT_SYNC_AUTO=true` env).
- `containers_run` env-var wiring in
  `services/agent_service/crud.py` sets `GIT_SYNC_AUTO=true` only for
  non-source-mode GitHub-template agents (auto-pushing to `main` would
  clobber protected branches).
- Loop swallows every exception so a single bad tick can't kill the
  heartbeat.

### 2. Backend poller

```
SyncHealthService._poll_loop (60 s)
    ├── for each git-enabled agent:
    │     ├── GET http://agent:8000/api/git/status (via AgentClient)
    │     │     response contains sync_state + dual ahead/behind
    │     ├── db.upsert_sync_state(...)
    │     └── if consecutive_failures crossed 3:
    │           └── db.create_operator_queue_item(
    │                 type='sync_failing', priority='high', …)
```

Emission is **edge-triggered** — a new entry appears only on the
transition from `N-1 < 3` to `N >= 3`. A fresh failure series after a
`success` reset produces a distinct entry (the ID embeds the emission
timestamp).

### 3. Dual ahead/behind (P6 fix)

`docker/base-image/agent_server/routers/git.py::_dual_ahead_behind_payload`
computes BOTH tuples:

- `ahead_main` / `behind_main` — `HEAD` vs `origin/main`
  (template-improvements signal)
- `ahead_working` / `behind_working` — `HEAD` vs `origin/<current_branch>`
  (peer-divergence signal — the P6 case)

Legacy `ahead` / `behind` in the response alias the main tuple so older
clients keep working.

### 4. Fleet sync-audit (S6)

```
GET /api/fleet/sync-audit
    ├── build_fleet_sync_audit(agent_names=...)
    │     ├── db.find_duplicate_bindings()  -- §P5 SQL
    │     ├── db.list_git_enabled_agents()
    │     └── db.list_sync_states()
    └── assembled { agents: [...], summary: {...} }
```

`find_duplicate_bindings()` implements the spec's §P5 query verbatim:

```sql
SELECT agent_name FROM agent_git_config
WHERE source_mode = 0
  AND (github_repo, working_branch) IN (
      SELECT github_repo, working_branch
      FROM agent_git_config
      WHERE source_mode = 0
      GROUP BY github_repo, working_branch
      HAVING COUNT(*) > 1
  )
```

Source-mode rows are excluded — legacy-mode siblings all tracking `main`
is legitimate; two non-source-mode agents sharing `trinity/<x>/<id>` is
the data-loss setup.

### 5. Dashboard dot

- `src/frontend/src/utils/syncHealth.js::classifySyncHealth(entry)` →
  `'green' | 'yellow' | 'red' | 'gray'`.
- Rules:
  - **gray**: `last_sync_status === 'never'` or no entry.
  - **red**: `behind_working > 0`, OR `last_sync_status === 'failed'`, OR
    last sync ≥ 7 days ago.
  - **yellow**: 24 h ≤ last sync < 7 d (status success).
  - **green**: last sync < 24 h AND status success AND
    `behind_working === 0`.
- `stores/agents.js::fetchSyncHealth()` calls `/api/agents/sync-health`
  on mount; `views/Agents.vue` renders the dot next to each agent.

## Files Touched

### Backend

| File | Purpose |
|------|---------|
| `db/schema.py` | `agent_sync_state` CREATE TABLE; new `agent_git_config` columns; index |
| `db/migrations.py` | `_migrate_sync_health` function (appended to `MIGRATIONS`) |
| `db/sync_state.py` | `SyncStateOperations` — upsert/get/list/delete, counter logic |
| `db/schedules.py` | `set_git_auto_sync_enabled`, `set_freeze_schedules_if_sync_failing`, `find_duplicate_bindings` |
| `db_models.py` | Two new fields on `AgentGitConfig` |
| `database.py` | Delegation to `SyncStateOperations` + the two new flags + duplicate query |
| `services/sync_health_service.py` | Background poller + operator-queue emitter |
| `services/fleet_audit_service.py` | `build_fleet_sync_audit()` aggregation |
| `services/agent_service/crud.py` | Sets `GIT_SYNC_AUTO` env + `auto_sync_enabled=1` for non-source-mode agents |
| `routers/git.py` | `/git/auto-sync`, `/git/freeze-schedules-if-failing`, `/git/sync-state` |
| `routers/agents.py` | `GET /api/agents/sync-health` (batch) |
| `routers/fleet.py` | `GET /api/fleet/sync-audit` (new router) |
| `routers/internal.py` | `GET /api/internal/agents/{name}/sync-health-status` |
| `main.py` | Starts `SyncHealthService` (staggered +5 s, PERF-269); registers `fleet_router` |

### Agent server

| File | Purpose |
|------|---------|
| `agent_server/auto_sync.py` | `run_auto_sync_loop`, env-gate helpers, FastAPI startup hook |
| `agent_server/main.py` | Calls `schedule_auto_sync_if_enabled(app)` |
| `agent_server/routers/git.py` | `_compute_ahead_behind`, `_dual_ahead_behind_payload`, `_run_auto_sync_once`, `_read_sync_state_file`, `_write_sync_state_file`. `get_git_status()` now returns both tuples + merges the persisted sync-state. |

### Frontend

| File | Purpose |
|------|---------|
| `stores/agents.js` | `syncHealth` state + `fetchSyncHealth()` action |
| `utils/syncHealth.js` | `classifySyncHealth`, `syncHealthColor`, `syncHealthLabel` |
| `views/Agents.vue` | Renders the dot + imports helpers + fetches on mount |

## Testing

Pure unit tests cover the feature end-to-end (no Docker, no live
backend):

- `tests/unit/test_sync_state_db.py` — CRUD + counter semantics +
  schema/migration assertions.
- `tests/unit/test_git_status_dual_ahead_behind.py` — real throwaway
  git repos; peer-clobber scenario exercised.
- `tests/unit/test_agent_server_auto_sync.py` — sync-state file,
  `_run_auto_sync_once` (success + push-failure), env-gate helpers.
- `tests/unit/test_sync_health_service.py` — persistence, threshold
  emission (edge-triggered + idempotent), behind-working red-flag.
- `tests/unit/test_fleet_sync_audit.py` — `find_duplicate_bindings`
  (source-mode exclusion + mixed-mode), `build_fleet_sync_audit`
  (clean agent, duplicate flagged, ahead_working, filter).

Baseline: 75 passing tests added across the two PRs.

## Operator Controls

| Control | How | Default |
|---------|-----|---------|
| Auto-sync on/off per agent | `PUT /api/agents/{name}/git/auto-sync` body `{enabled: bool}` | `true` for non-source-mode GitHub-template agents |
| Interval override | `GIT_SYNC_INTERVAL_SECONDS` env var in the agent container | 900 s (15 min) |
| Fleet kill-switch | `GIT_SYNC_AUTO` env var (if missing/false the loop never starts) | `true` only if backend set it at creation |
| Freeze schedules when sync failing | `PUT /api/agents/{name}/git/freeze-schedules-if-failing` | `false` (opt-in) |
| Alert threshold | Hardcoded in `SyncHealthService.ALERT_THRESHOLD` | 3 consecutive failures |

## Known Limitations

- **`dirty_tree` in `/api/fleet/sync-audit` is always `false`** today.
  Populating it requires a live agent call per row; can land as a
  follow-up with `asyncio.gather`.
- **`freeze_schedules_if_sync_failing` is read-only from the scheduler
  side**. The config flag, API, and internal lookup endpoint are wired
  but actual enforcement belongs in the dedicated `trinity-scheduler`
  container's pre-execution check — separate follow-up.
- **Auto-sync disabled for source-mode agents** by design. Source-mode
  tracks `main`, and auto-pushing to `main` would clobber protected
  branches. Source-mode agents still get sync-state tracking via the
  backend poller, they just don't run the heartbeat.

## Related Flows

- [github-sync.md](github-sync.md) — the pull/push infrastructure this
  feature builds on
- [github-repo-initialization.md](github-repo-initialization.md) —
  where instance IDs and working branches come from
- [operating-room.md](operating-room.md) — the operator queue where
  `sync_failing` entries surface

## References

- Upstream epic: [abilityai/trinity#381](https://github.com/abilityai/trinity/issues/381)
- Sub-issues: #389 (S1), #390 (S6); coordinates with #382 (S7)
- Spec: `ability-trinity-git-improvements-proposal.md` (§P1, §P5, §P6,
  §S1, §S1a, §S6)
