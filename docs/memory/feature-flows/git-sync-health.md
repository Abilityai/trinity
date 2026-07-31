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
git_dir_bytes INTEGER                    -- #1596: .git on-disk size
pack_count INTEGER                       -- #1595: packs (count-objects -v)
loose_objects INTEGER                    -- #1595: loose objects
maintenance_failures INTEGER DEFAULT 0   -- #1595: failed maintenance streak
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
- **#1595:** the cycle runs in a worker thread (`asyncio.to_thread`) so a
  long maintenance pass can't starve `/health`, the 5s liveness heartbeat,
  or chat. A non-blocking module-level repo lock (`_REPO_LOCK`) replaces
  the serialization the blocking loop used to provide by accident: the
  cycle skips (`{"status": "skipped", "reason": "repo_busy"}`) when an
  operator git op is in flight, and the mutating endpoints
  (`/api/git/sync`, `/api/git/pull`, reset) return `409 X-Conflict-Type:
  agent_busy` while a cycle/maintenance runs (`_with_repo_lock`).

### 1a. Survivable maintenance pass (#1596, hardened by #1595)

```
_run_auto_sync_once (worker thread, repo lock held)
    ├── _reap_stale_git_litter          gc.pid / index.lock >1h;
    │                                   tmp_pack_* > repack budget + 300s
    ├── _collect_git_object_stats       one `git count-objects -v`
    ├── _git_dir_bytes                  `du -sb .git`
    ├── add / status / commit / push    ALL via run_registered(...)
    ├── _maybe_run_git_maintenance(stats)
    │     ├── trigger: packs ≥ 20 OR loose ≥ 6700
    │     ├── guards: backoff gate → disk preflight (free < 1.1× pack bytes)
    │     ├── git -c pack.threads=1 -c pack.windowMemory=128m \
    │     │     repack -A -d -l -q --unpack-unreachable=1.hour.ago
    │     └── git gc --quiet --prune=1.hour.ago
    └── _write_sync_state_file          atomic (tmp + os.replace); metrics on
                                        EVERY terminal path; maintenance
                                        backoff bookkeeping (1h→24h)
```

- **Why registered:** any bare agent-server child is indistinguishable from
  a leaked orphan — the sweep's hard-protect walk goes UP to PID 1, never
  down (#1501). `utils/registered_run.py` wraps Popen with
  `ProcessRegistry.add_transient_pid` (TTL derived from the call-time
  timeout) and kills the **process group** on timeout (`start_new_session`
  + `killpg`): killing only `git repack` orphans its `pack-objects` child
  holding the stderr pipe, wedging `communicate()` and recreating the
  tmp_pack litter.
- **Why auto-gc is off:** a detached `gc --auto` reparents to PID 1 and is
  SIGKILLed by the sweep within one tick — it never once completed in
  production (#1595: 44 GB / 97%-garbage repos, stale `gc.pid`, no
  `gc.log`). Base image bakes `gc.auto=0`, `gc.autoDetach=false`,
  `maintenance.auto=false`, `maintenance.autoDetach=false` into
  `/etc/gitconfig`; `git_service.setup_git_in_container` mirrors them into
  `~/.gitconfig` for older images. With auto-gc off, garbage accumulates
  as LOOSE objects — hence the loose-count trigger condition.
- **Prune grace:** Claude executions run git concurrently by design;
  `--prune=now` would delete their just-written, not-yet-referenced
  objects (repo corruption). 1h grace > any single git op and still
  reclaims the aged garbage that matters.
- **Startup reap:** `startup.sh` removes `index.lock`, `gc.pid`,
  `objects/maintenance.lock`, and ref/reflog `*.lock` at container start
  (provably no live git process) — a stale `index.lock` from a killed op
  froze three production agents for ~12 days.
- **Rollout:** everything ships in the base image — existing fleets need a
  base-image rebuild + agent recreate; pre-existing bloat recovery is
  ops-side (trinity-ops-agent#127) using `GIT_MAINTENANCE_TIMEOUT_SECONDS`.

### 2. Backend poller

```
SyncHealthService._poll_loop (60 s)
    ├── for each git-enabled agent:
    │     ├── GET http://agent:8000/api/git/status (via AgentClient)
    │     │     response contains sync_state + dual ahead/behind
    │     ├── coerce agent-supplied ints (#1595): git_dir_bytes /
    │     │     pack_count / loose_objects / maintenance_failures →
    │     │     _coerce_nonneg_int (sync-state.json is agent-writable;
    │     │     reject strings/bools/objects/out-of-range at the boundary)
    │     ├── db.upsert_sync_state(...)
    │     ├── if consecutive_failures crossed 3:
    │     │     └── db.create_operator_queue_item(
    │     │           type='sync_failing', priority='high', …)
    │     └── #1595 git_bloat alerts (same edge-trigger pattern):
    │           ├── git_dir_bytes crossed GIT_DIR_ALERT_BYTES (10 GiB)
    │           └── maintenance_failures crossed 3
```

Emission is **edge-triggered** — a new entry appears only on the
transition from `N-1 < 3` to `N >= 3` (or below-ceiling → above-ceiling
for `git_bloat`). A fresh failure series after a `success` reset produces
a distinct entry (the ID embeds the emission timestamp).

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
  on mount; `components/AgentListPanel.vue` (the Dashboard List mode —
  ent#260 retired the Agents page into it) renders the dot next to each
  agent, with a 60s visibility-aware refresh while the mode is active.

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
| `components/AgentListPanel.vue` | Renders the dot + imports helpers + fetches on mount + 60s visibility-aware refresh (ent#260 — replaces the retired `views/Agents.vue`) |

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
- **Sub-repos get no maintenance (#1595)**. `gc.auto=0` is system-wide but
  the maintenance pass targets only `/home/developer/.git`; a project repo
  cloned into a subdirectory has auto-gc off with no replacement. Not a
  regression (its detached auto-gc was always sweep-killed too), but the
  blind spot is now by design — revisit if sub-repo bloat surfaces.
- **Agents with auto-sync OFF get no maintenance (#1595)**. Deliberate:
  they previously had zero *effective* maintenance anyway (auto-gc never
  completed), and `gc.auto=0` at least stops their tmp_pack garbage
  ratchet.
- **Maintenance bounds garbage, not history (#1595)**. Every repack still
  rewrites full history, whose size grows without bound under auto-commit
  churn — the opt-in history-squash policy and/or geometric repack
  (`--geometric=2` + midx) remain the deferred follow-up that fixes the
  long-run cost curve.
- **`/api/git/status` still blocks the agent event loop** (~30s worst case
  per 60s poll on a bloated repo — `git fetch` + ~8 subprocesses on the
  loop thread). Pre-existing; evidence filed to #1505 rather than threading
  it here (a threaded status needs a busy-path design vs the repo lock).

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
