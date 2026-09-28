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

**trinity-enterprise#706 changed what "healthy" means.** Health used to
measure whether the last push *succeeded*; an agent 15 commits ahead with
auto-sync off never ran a heartbeat, never failed, and looked fine. It now
measures whether the agent and its repository *agree*: the poller keeps a
divergence clock (`diverged_since`), a dirt clock (`dirty_files` /
`dirty_since`) and `last_successful_push_at`, one backend policy module
(`services/sync_freeze_policy.py`) turns them into `state` / `reason` /
`recommendation`, and the schedule freeze now also fires for a **work agent**
diverged from origin for more than 24 h. See §2b and §5.

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
| **API** | `GET /api/internal/agents/{name}/sync-health-status` | The backend's read of the freeze decision (`should_freeze`, `freeze_reason`, `divergence_age_s`, `work_agent` — ent#706). The scheduler reads the DB directly with the vendored policy, not this endpoint |
| **Operator Queue** | type=`sync_failing` | Inserted by `SyncHealthService` when `consecutive_failures` crosses 3 |
| **Operator Queue** | type=`sync_diverged` | ent#706: one per divergence episode, only while a divergence freeze is in force (id `sync-diverged-{agent}-{diverged_since}`) |
| **Scheduler** | `SchedulerDatabase.sync_freeze_reason` | ent#706: the cron gate's reason, written into the skipped row as `Git sync frozen: <reason>` |

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
git_dir_bytes BIGINT                     -- #1596: .git on-disk size (BIGINT since #2800: int4 on PG overflowed at 2 GiB)
pack_count INTEGER                       -- #1595: packs (count-objects -v)
loose_objects INTEGER                    -- #1595: loose objects
maintenance_failures INTEGER DEFAULT 0   -- #1595: failed maintenance streak
diverged_since TEXT                      -- ent#706: divergence episode start (set once, cleared at 0/0)
dirty_files INTEGER                      -- ent#706: porcelain change count (changes_count)
dirty_since TEXT                         -- ent#706: dirt episode start (cleared at 0)
last_successful_push_at TEXT             -- ent#706: last push that landed
last_check_at TEXT
updated_at TEXT NOT NULL
FOREIGN KEY (agent_name) REFERENCES agent_ownership(agent_name)
```

The four ent#706 columns come from the SQLite migration
`agent_sync_state_divergence` and Alembic `0081_agent_sync_state_divergence`.
They are nullable with **no backfill**: the clocks cannot be known
retroactively, so they start at the first poll after upgrade — which is also
the 24 h soak before any divergence freeze can fire. The upsert takes a `KEEP`
sentinel for the two clocks (`db/sync_state.py`), because `_merged()` maps
None to "unchanged" and a clock needs "clear" too.

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
  `last_error_summary`, `consecutive_failures`, and (#3011)
  `last_successful_push_at` + `behind_after_fetch`. Read/merged into
  `GET /api/git/status` so the backend poller picks it up.

## Execution Flow

### 0. Creation-time canonical `.gitignore` seed (#2069)

The auto-sync loop (§1) is on **from birth** for the `GIT_SYNC_AUTO` set, but the
fleet-wide `_GITIGNORE_PATTERNS` was applied only on Push/init — never at creation.
So the first cycle staged `.trinity/` runtime state and the root-level `.env`/`.mcp.json`
into a **user-owned** repo before any Push could migrate the list. #2069 lands the
merged list *after* startup.sh's full git setup and *before* the first auto-sync cycle:

```
crud.py::_materialize_agent_files  (last step, gated: _git_auto_sync_baked(...))
    │  and start_agent_internal (T1: gated on DB auto_sync_enabled)
    ▼
git_service.spawn_gitignore_merge_after_clone(name)   fire-and-forget, Semaphore-capped
    ▼
merge_gitignore_after_clone(name)   monotonic deadline _MERGE_READY_TIMEOUT_SECONDS (1800)
    ├── poll: DIRECT agent /health probe (agent_httpx_client, NOT the masking proxy)
    │        server launches once at startup.sh:517 — AFTER clone→tar→checkout→remote-config,
    │        so a 200 proves no further `git checkout` can revert the merge (Codex #1 race)
    ├── once ready: [ -d /home/developer/.git ]  → absent ⇒ failed clone, skip (Push backstop)
    ├── _git_toplevel(name)  → None ⇒ skip
    └── _build_gitignore_merge_command(git_dir)   MERGE-ONLY (no rm-cached; PREVENT)
```

- **Reuses `_build_gitignore_merge_command`** (single source of truth; no fourth pattern
  list — one backend `docker exec`, nothing in the agent-server / Invariant #5).
- **Merge-only = PREVENT**: the generated `.env`/`.mcp.json` are untracked post-clone,
  so a merge-installed `.gitignore` stops `git add -A` from ever staging them. A template
  that *committed* a credential file (unusual subclass) stays tracked → Push migration
  remediates → #1703 retires the layer.
- **ENV-predicate gate** (`_git_auto_sync_baked`, the single owner shared with
  `_apply_github_env`): covers exactly the auto-committing population, **ephemeral
  non-source `github:`+PAT ghosts included** (the DB-flag block excludes them, but they
  still bake `GIT_SYNC_AUTO` and are never Pushed). Source-mode excluded (the uncommitted
  `.gitignore` would block a pull-only agent's next `git pull`).
- **Two managed regions, not one block (#2529)**: the merge is a normalize-and-rebuild —
  `[defaults block][the agent's own rules, original order][protected floor]`. The defaults
  block sits ABOVE the agent's rules so an agent negation (`!.env.example`,
  `!.claude/settings.json`) wins without having to be the file's last line; the floor —
  the seven credential patterns (incl. `.ssh/`) with `!.env.example`/`!.mcp.json.template`, plus `.trinity/*`
  and its 8 derived `!` re-includes — sits BELOW them and cannot be overridden. One block
  provably cannot carry both: hoisting would turn every currently-inert `!.env` in the fleet
  live in one unattended Push, and would let a user `*.sh` beat `!.trinity/setup.sh`
  (ent#76 / #1704, failing quietly). `_GITIGNORE_PROTECTED` is a filter over
  `_GITIGNORE_PATTERNS`, so the floor cannot drift from the list.
- **Idempotent (#953, #2529)**: idempotent by CONTENT — a second run computes byte-identical
  output and `cmp` leaves the file (and its mtime) untouched, so an already-compliant
  template shows no `M .gitignore` drift and the 15-min auto-sync loop has nothing to
  re-commit. The 14 bundled templates are regenerated as this merge's own **fixed point**
  (#1908 byte-identity), because a flat canonical list is no longer one. A stale wholesale
  `.trinity/` line gets a legitimate supersede (#2070).
- **Fleet remediation (T1)**: `start_agent_internal` fires the same spawn (gated on the DB
  `auto_sync_enabled` flag — ghosts never recreate), so existing leakers converge on their
  next base-image-drift recreate/restart. No behaviour change on Push (`sync_to_github`).
- **Bounded & non-fatal**: monotonic deadline, module-level `asyncio.Semaphore` cap on
  pollers, every exec/HTTP `asyncio.wait_for`-wrapped (frees the task, not the pinned
  4-thread Docker pool thread). Known hole: a backend restart in the readiness window
  loses the in-memory `create_task` — Push migration remediates; #1703 is the structural fix.

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
│ routers/git._run_auto_sync_  │  refuse: source-mode on default branch (#3011)
│ once(home_dir)               │  git add -A
│                              │  git commit (if dirty)
│                              │  git fetch origin <branch>
│                              │  behind > 0 → git rebase --autostash
│                              │     conflict → rebase --abort, record diverged
│                              │     clean    → push --force-with-lease=<fetched>
│                              │  else git push origin HEAD
└────────────┬─────────────────┘
             │
             ▼
┌──────────────────────────────┐
│ _write_sync_state_file(…)    │  .trinity/sync-state.json
└──────────────────────────────┘
```

- **The toggle is authoritative and live (#3010).** The loop starts whenever
  the agent can ask the platform (`TRINITY_BACKEND_URL` + its own
  `TRINITY_MCP_API_KEY`) or was baked with `GIT_SYNC_AUTO=true`, and **every
  cycle** reads the owner's flag through `GET /api/agents/{name}/git/auto-sync`
  (`auto_sync.resolve_auto_sync_enabled`). OFF skips that cycle, ON runs it —
  a `PUT .../git/auto-sync` lands within one interval, no recreate. 404 "Git
  not configured" → off; the platform unreachable, a 5xx, an auth refusal or a
  uniform 404 → the `GIT_SYNC_AUTO` env, which is the last value the platform
  handed the container. The agent-side `GET /api/git/status` reports the value
  the loop is running with as `auto_sync_enabled`.
- `auto_sync_enabled` in `agent_git_config` is the **one writer**. Creation
  (`crud.py::_materialize_agent_files`) sets it from the same
  `_git_auto_sync_baked` predicate that bakes `GIT_SYNC_AUTO` — ghosts
  included — and on every container rebuild (ent#109)
  `lifecycle.py::_apply_git_env_from_db` derives `GIT_SYNC_AUTO` from the
  flag **alone**. The old `DB flag OR baked env` meant an owner's OFF never
  stuck (creation set both; the PUT cleared only the DB; the OR re-armed it
  on every recreate). Still derive-only, never written back, so the recreate
  trigger (`POST .../start`, `AuthorizedAgentByName`) cannot flip the
  owner-only flag (`PUT .../auto-sync`, `OwnedAgentByName`).
- **One-shot backfill** (`auto_sync_enabled_backfill` + Alembic `0075`):
  live non-source-mode ghosts — the env-true/DB-0 slice the DB can
  identify — get the flag set so they keep auto-pushing. Agents bound later
  through `POST /git/initialize` are also `source_mode = 0` but never baked
  the env, so a wider backfill would arm pushes they never had; a non-ghost
  whose flag is 0 now stays off (an owner's earlier OFF finally takes effect).
- Settings → **Git sync** (`GitSyncSettingsPanel.vue`) carries both toggles
  (auto-sync and pause-schedules-while-failing).
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
  froze three production agents for ~12 days. **#2742 makes it speak.** It
  was `rm -f`: silent whether or not it removed anything, so the one moment
  the platform reliably heals a wedge produced no evidence that it had. It
  now tests-then-removes, echoes a line per lock actually cleared (Vector
  captures it), and drops `~/.trinity/lock-recovery.json`, which the agent
  server folds into `sync_state.last_lock_recovery` → the `lock_recovery`
  field on `GET /api/git/status` → a one-shot backend WARNING. Container
  start is also the ONLY context where "no process holds this lock" is
  provable for free (the PID namespace is empty), which is why recovery
  lives here and not in the running container. It also reaps `index.lock`
  under `<gitdir>/modules/*` and `<gitdir>/worktrees/*` — covered by nothing
  before, so a submodule or linked-worktree wedge survived every restart.
- **Runtime stuck-lock report (#2742) — observe, never delete.** The status
  read reports a currently-present `index.lock` as `index_lock_stuck`
  (`{path, age_seconds, stable_for_seconds, sightings, size_bytes}` — the
  backend keeps only the ints and drops the agent-composed path) and a backend
  WARNING; it does **not**
  unlink it. Three measurements decide this:
  - `st_size == 0` is the signature of a **live** writer, not an abandoned
    one — git creates the lock with `O_EXCL` *before* walking the worktree
    and writes the new index into it only at the very end. A healthy
    `git add -A` held a 0-byte lock for 100 % of its life: 3.9 s on 450 MB,
    **29 s at 60 000 files**, **155 s** with a `clean` filter configured.
  - `st_mtime` is stamped at create and never advances, so **age measures
    the in-flight operation**, not abandonment — and a forward NTP step or
    a live-migration ages every existing lock at once, forging staleness in
    the one direction that makes a delete *more* likely.
  - A wrong unlink is worse than the wedge it repairs and is **permanent**:
    git renames by *path*, so after the unlink a second git owns the path
    and the first git's closing `rename(index.lock, index)` promotes the
    second's in-flight file onto `.git/index` — the corrupting process
    exits rc=0 with empty stderr, and the resulting 0-byte `.git/index`
    is cleared by neither `startup.sh`, nor `_reap_stale_git_litter`, nor
    `git reset`.

  Detection is therefore a **two-point inode-stability** observation, not a
  one-tick age gate: the same `(st_ino, st_mtime_ns, st_size)` must be seen
  unchanged across ≥3 ticks spanning ≥15 min before anything is reported.
  The observer resolves the real gitdir (`.git` is a **file** for a linked
  worktree or a submodule) and covers `<gitdir>/index.lock`,
  `<gitdir>/modules/*/index.lock` and `<gitdir>/worktrees/*/index.lock`;
  uses `lstat` + `S_ISREG` and skips a symlinked `.git`; takes **no** repo
  lock (an `lstat` needs none, and taking one would make a status poll a
  new source of 409 `agent_busy`); and is wrapped in its own
  `except OSError` — it lives inside the `try` whose tail is
  `HTTPException(500)`, and a 500 makes the poller write nothing at all, so
  an observability path that can darken the feed it feeds is worse than no
  observability path.
- **Rollout:** everything ships in the base image — existing fleets need a
  base-image rebuild + agent recreate; pre-existing bloat recovery is
  ops-side (trinity-ops-agent#127) using `GIT_MAINTENANCE_TIMEOUT_SECONDS`.

### 1b. Reconcile before push (#3011)

The cycle used to be `add -A → commit → push origin HEAD` with no fetch, so
the first foreign push to the agent's branch failed every later cycle
non-fast-forward, forever — the `sync_failing` alert fired but the divergence
was never repaired and the agent's commits piled up on the container disk. The
heartbeat is now the durability mechanism for agents whose humans also push,
so it reconciles (all under `_REPO_LOCK`, every child via `run_registered`):

- **Refusal first.** `GIT_SOURCE_MODE=true` (pull-only by contract) on the
  repo's default branch (`origin/HEAD`, else `main`/`master`) refuses before
  anything is committed: `failed`, `refused: source-mode on <branch>`. The
  clone stays a clean mirror and three refusals raise `sync_failing`, naming
  the contradictory config. Fork-to-own agents own their fork's `main` and are
  exempt — recognised by `GIT_UPSTREAM_REPO` or, since that env is not
  re-derived on recreate, the `upstream` remote on the persistent volume.
- **Fetch** `origin <branch>` (the one new network call). A branch missing on
  the remote (a fresh working branch) is not an error — the push creates it.
- **Behind → rebase** `--autostash` onto `origin/<branch>`. A clean rebase
  pushes with `--force-with-lease=refs/heads/<branch>:<fetched sha>`, so a
  push landing between fetch and push is rejected (recorded, retried next
  cycle), never overwritten; never the bare forced form. Not behind → the
  plain `git push origin HEAD` as before.
- **Conflict → abort.** `git rebase --abort` (also on a timeout-killed
  rebase) leaves the repo exactly as it was — the agent's commit intact, the
  remote untouched, nothing reset, nothing resolved automatically — and
  records `diverged: rebase conflict on <branch>`; the existing three-strike
  `sync_failing` path raises it. Resolution stays with the operator
  `sync_to_github` endpoint, which is unchanged.
- **Recorded:** `behind_after_fetch` (the commits the remote had that we
  lacked, before the rebase) and `last_successful_push_at` (stamped on success)
  in `sync-state.json`, for the divergence-age work (trinity-enterprise#706).

### 2. Backend poller

```
SyncHealthService._poll_loop (SYNC_HEALTH_POLL_INTERVAL_SECONDS, default 60 s)
    ├── acquire synchealth:leader (SET NX, fail-open) — non-leaders return  [#2742]
    ├── for each git-enabled agent:
    │     ├── GET http://agent:8000/api/git/status (via AgentClient)
    │     │     response contains sync_state + dual ahead/behind
    │     ├── coerce agent-supplied ints (#1595): git_dir_bytes /
    │     │     pack_count / loose_objects / maintenance_failures →
    │     │     _coerce_nonneg_int (sync-state.json is agent-writable;
    │     │     reject strings/bools/objects/out-of-range at the boundary)
    │     ├── ent#706 episode clocks from the RAW payload values
    │     │     (_episode_clock): diverged_since set once on ahead/behind > 0,
    │     │     cleared at ahead == 0 with behind 0 or None (no upstream),
    │     │     KEPT when ahead is None; dirty_files = changes_count with
    │     │     dirty_since the same way; last_successful_push_at = max(the
    │     │     #3011 heartbeat field, agent_git_config.last_sync_at, stored)
    │     │     — agent timestamps need an offset and must not be future
    │     ├── db.upsert_sync_state(...)
    │     ├── if consecutive_failures crossed 3:
    │     │     └── db.create_operator_queue_item(
    │     │           type='sync_failing', priority='high', …)
    │     │         #2107: when last_error_summary is a refused push
    │     │         (git_service.is_push_denied) the item is titled
    │     │         "Git token can't push", says it will not recover on
    │     │         its own, and carries context.cause='push_denied' +
    │     │         context.remediation (grant Contents: write / `repo`)
    │     ├── ent#706: sync_view(updated, config) → if the freeze cause is
    │     │     divergence: db.create_operator_queue_item(type='sync_diverged',
    │     │     id='sync-diverged-{agent}-{diverged_since}') — the conflict
    │     │     target makes it ONE row per episode across polls, restarts
    │     │     and a fail-open double leader; counts + recommendation only
    │     ├── #1595 git_bloat alerts (same edge-trigger pattern):
    │     │     ├── git_dir_bytes crossed GIT_DIR_ALERT_BYTES (10 GiB)
    │     │     └── maintenance_failures crossed 3
    │     └── #2742 WARNING on a newly observed lock_recovery / index_lock_stuck
    │           (log line only — no operator-queue item, no DB column)
```

### 2b. The policy and the freeze (trinity-enterprise#706)

`services/sync_freeze_policy.py::classify(row, cfg, now, push_denied=)` is the
one rule. Stdlib only and never raises (the scheduler calls it on the fire
path over agent-written numbers); vendored **byte-identically** to
`src/scheduler/sync_freeze_policy.py` and pinned by
`test_ent706_sync_policy_parity.py`. `services/sync_health_view.py::sync_view`
is the backend's single call site; it resolves `git_service.is_push_denied`
so the leaf stays pure.

```
work agent = source_mode = 0 OR auto_sync_enabled = 1     (fork-to-own keeps
                                                            source_mode = 1)
unknown  no row / never observed
red      last sync failed                                  (any binding)
         work agent diverged > 24 h
         work agent dirty > 24 h
         auto-sync on, no heartbeat for 7 d                 (fresh observation)
yellow   work agent diverged <= 24 h
         deployment diverged (any age) or dirty > 24 h      (never red on age)
green    otherwise

freeze = freeze_schedules_if_sync_failing AND (
             sync_failing                                  (#1808, any binding)
             OR (work agent AND diverged > 24 h AND last_check_at <= 15 min old))
```

The freshness clause fails open: the poller writes nothing for an unreachable
agent (or a status call that 504s), so a stale `diverged_since` must never keep
an agent frozen that may already have pushed. The row stays red and the reason
says `(last observed 3h ago)`. `behind_main` (a push to `main` under a
working branch) is information in the reason, never divergence.

Enforcement, both over the one rule:

```
scheduler  _execute_schedule_with_lock (cron only)
    └── db.sync_freeze_reason(agent)   # direct DB read, fail-OPEN on any error
          ├── None  → fire
          └── "diverged 0 behind / 7 ahead for 26h"
                → _record_skipped_agent_schedule(skip_reason="Git sync frozen: <reason>")
                  (one row per cron tick while frozen; retention prunes)
                → _advance_next_run_only; un-freezes on the first tick after it clears
backend    GET /api/internal/agents/{name}/sync-health-status
                → pre-#706 keys unchanged + freeze_reason / divergence_age_s / work_agent
```

`sync_freeze_reason` returns a string, never a `(bool, reason)` tuple — a tuple
is truthy, and a caller left on the old `if db.should_freeze_...` shape would
freeze the whole fleet. `should_freeze_schedules` is its bool view.

### 2a. Agent status handler (#2742)

```
GET /api/git/status  (poller 10 s · UI git panel 60 s · MCP get_git_status)
    └── get_git_status()                     <- async, thin, ON THE LOOP
          ├── .git missing -> {"git_enabled": false}
          ├── _STATUS_INFLIGHT live?  --yes--> await wait_for(shield(fut), 35 s)
          └── no -> ensure_future(to_thread(_compute_git_status, home_dir))
                      ^^^ exactly ONE default-pool thread per in-flight run
                          (the #2433 starvation class: that pool also carries
                           ctx.terminate, auto-sync and pipe-close)
                └── _compute_git_status()                  (worker thread)
                      rev-parse / log / merge-base / remote -> run_registered
                      git --no-optional-locks status --porcelain -z -> NO index.lock
                      git fetch origin (30 s)               -> run_registered
                      lstat index.lock -> index_lock_stuck  (REPORT, never unlink)
                      + computed_at, lock_recovery
```

Two bounds, deliberately distinct. `_STATUS_FOLLOWER_WAIT_SECONDS = 35` is a
**caller** bound, set at or just past the point every real client has already
given up (poller 10 s, backend `git_service` 30 s) — a follower waiting longer
can only produce work nobody awaits, and times out as `504`. The leader carries
its own **computation** bound (`_STATUS_LEADER_DEADLINE_SECONDS = 90`) because
the child timeouts sum to ~130–150 s nominal (10 `rev-parse` + 10 `status` + 10
`log` + **30 `fetch`** + 10 `merge-base` + 10 `log` + 10 `remote get-url`, plus
10 `_persist_last_remote_sha` + 10 `_get_pull_branch` + 10‥30
`_dual_ahead_behind_payload` (#2105), before `run_registered`'s post-`killpg` drain), and
a wedged leader would otherwise hold the in-flight slot for all of it.

Coalescing **is** bounded staleness and the doc says so rather than denying it:
a follower arriving at t=29 s of a 30 s leader run is served a 29-second-old
snapshot, which an operator can see as "1 ahead" straight after a successful
push. `computed_at` (ISO-Z, stamped inside the computation) makes that legible.
Serving late followers a fresh run was explicitly rejected — it reintroduces the
overlapping `git fetch` this change exists to remove.

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

The working tuple uses `origin/<current_branch>` whatever the branch is named
(#2105). It used to do that only for `trinity/*` branches. Every other branch got
the `origin/main` counts under the working label, and the fleet audit reads
`ahead_working` as unpushed commits. When there is no upstream (the branch was
never pushed, or HEAD is detached), `ahead_working` counts the commits that no
remote holds, and `behind_working` is `null`. A count that can't be computed is
`null`, never 0: for example, the main tuple on a repo with no `main`. The
backend stores `null` as 0 (`sync_health_service._coerce_counter`).

**The working tuple is the divergence basis (ent#706).** `diverged_since` is
decided from the RAW `ahead_working` / `behind_working` payload values, before
that 0-coercion, so an uncomputable count never starts or clears the clock.
When the clock is kept that way, the stored `ahead_working` / `behind_working`
are kept too rather than coerced to 0, so a kept episode never reads
"diverged 0 behind / 0 ahead" in its reason or its operator-queue item.

Legacy `ahead` / `behind` in the response alias the main tuple so older
clients keep working.

### 4. Fleet sync-audit (S6)

```
GET /api/fleet/sync-audit
    ├── build_fleet_sync_audit(agent_names=...)
    │     ├── db.find_duplicate_bindings()  -- §P5 SQL
    │     ├── db.list_git_enabled_agents()
    │     ├── db.list_sync_states()
    │     └── sync_health_view.sync_block(row, cfg)   -- ent#707, per agent
    └── assembled { agents: [...], summary: {...} }
```

**ent#707:** every pre-#707 key keeps its meaning. Each entry adds `ahead`,
`behind`, `dirty_files`, `diverged_since`, `divergence_age_s`,
`last_successful_push_at`, `state`, `reason`, `recommendation`, `binding`,
`auto_sync_enabled`, `frozen` from the same `sync_view` call every other
surface makes; `dirty_tree` is now `dirty_files > 0` (it was hard-coded
`false`), so `in_sync` / `dirty` in the summary are real; the summary adds
`diverged`, `frozen`, `auto_sync_off`, `red`. Exposed over MCP as
`get_fleet_sync_audit` (`tools/monitoring.ts`; `routers/fleet.py`'s `# mcp:`
header names it) — the backend scopes rows through `accessible_agent_names`,
so an agent-scoped key sees its owner's set (decision D12). No surface's
`reason` carries the agent-written git error: `sync_view` computes it with
`last_error_summary` withheld (the recommendation still reads the error). The
same block, per agent, rides `GET /api/monitoring/status` as `agent.sync` —
see [agent-monitoring.md](agent-monitoring.md).

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
- **The backend owns the state (ent#706).** `GET /api/agents/sync-health`
  serves each entry's `state` / `reason` / `recommendation` / `binding` /
  `freeze` from `sync_view` (plus `dirty_files`, `divergence_age_s`,
  `last_successful_push_at`; every pre-#706 key is unchanged). The helper maps
  `state` to a colour (`unknown` or missing → gray) and `syncHealthLabel`
  shows `reason — recommendation` on hover. It holds **no threshold** — the
  24 h / 7 d / behind-is-red rules it used to compute are the backend's (§2b),
  so the dot cannot disagree with the freeze.
- Visible change: an agent ahead of origin with auto-sync off used to render
  **gray** (it had never synced); it now renders the backend's yellow or red.
- The batch reads through `db.list_sync_health_rows` — one query over
  `agent_git_config` ⋈ live `agent_ownership` ⟕ `agent_sync_state`, the same
  set the poller polls. An agent whose git sync is disabled (`sync_enabled = 0`)
  now shows `unknown` rather than its last stale row.
- `stores/agents.js::fetchSyncHealth()` calls `/api/agents/sync-health`
  on mount; `components/AgentListPanel.vue` (the Dashboard List mode —
  ent#260 retired the Agents page into it) renders the dot next to each
  agent, with a 60s visibility-aware refresh while the mode is active.
- **The agent card (ent#707).** The Fleet tile (`AgentTile.vue`) reads the
  same batch entry and shows one sync chip — `↑7 ↓0 · 12 dirty · pushed 3h
  ago`, kind from `state` (red crit, yellow warn, green calm; none for
  unknown), reason — recommendation and the absolute push time on hover, plus
  `Scheduled runs are paused until it syncs` when a freeze is in force. This
  closes #3035 /review I2: the old tile chip needed `last_sync_status ==
  'failed'`, so a divergence-frozen agent (which never fails a push) showed
  nothing in grid mode while the list-mode dot was red. Agent
  Detail → Overview reads `/git/sync-state` and renders the same line in the
  state colour. The formatter is `utils/syncSummary.js` (display only; this
  file keeps no clock and no counts).

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
| `services/sync_health_service.py` | Background poller + operator-queue emitter. #2742: `synchealth:leader` lease (fail-open, compare-and-delete release), the `SYNC_HEALTH_POLL_INTERVAL_SECONDS` knob, and `_coerce_lock_recovery` / `_coerce_lock_stuck` + the one-shot recovery WARNING |
| `services/fleet_audit_service.py` | `build_fleet_sync_audit()` aggregation |
| `services/agent_service/crud.py` | Sets `GIT_SYNC_AUTO` env + `auto_sync_enabled=1` for non-source-mode agents; `_apply_github_env` gates on `git_service._git_auto_sync_baked` (#2069, single owner of the bake predicate); `_materialize_agent_files` fires `spawn_gitignore_merge_after_clone` on the same predicate (#2069 creation seed) |
| `services/agent_service/lifecycle.py` | `_apply_git_env_from_db` re-derives `GIT_SYNC_AUTO` on every container rebuild from `auto_sync_enabled` alone (#3010) — derive-only, never writing the column back (ent#109); `start_agent_internal` fires `spawn_gitignore_merge_after_clone` on the DB `auto_sync_enabled` flag (#2069 T1 fleet remediation) |
| `services/git_service.py` | `merge_gitignore_after_clone` (readiness-gated poll-then-merge, reusing `_build_gitignore_merge_command`), `spawn_gitignore_merge_after_clone` (fire-and-forget, Semaphore-capped), `_git_auto_sync_baked` (the `GIT_SYNC_AUTO`-bake predicate) — #2069 creation-time seed |
| `services/git_service.py` | `_GITIGNORE_PROTECTED` + the four `_GITIGNORE_BLOCK_*`/`_GITIGNORE_FLOOR_*` markers, the rebuilt `_build_gitignore_merge_command`, the reporting probes on `_build_rm_cached_ignored_command`, `GitignoreSweep`/`_parse_gitignore_sweep`/`_shadowed_negations`/`_coerce_sweep`/`_with_sweep`, `_emit_gitignore_untracked_alert`, `_augment_commit_message` — #2529 precedence + honest sweep reporting |
| `db_models.py`, `routers/git.py`, `src/mcp-server/src/tools/git.ts`, `src/frontend/src/composables/useGitSync.js` | the three sweep fields on `GitSyncResult` and their five surfaces (#2529) |
| `routers/git.py` | `/git/auto-sync`, `/git/freeze-schedules-if-failing`, `/git/sync-state` |
| `routers/agents.py` | `GET /api/agents/sync-health` (batch) |
| `routers/fleet.py` | `GET /api/fleet/sync-audit` (new router) |
| `routers/internal.py` | `GET /api/internal/agents/{name}/sync-health-status` (ent#706: via `sync_view`) |
| `services/sync_freeze_policy.py` | ent#706: the one rule — `classify`, the constants, `format_age`; stdlib leaf |
| `services/sync_health_view.py` | ent#706: `sync_view(row, config)` — the backend's single call into the policy |
| `db/migrations.py`, `migrations/versions/0081_agent_sync_state_divergence.py`, `db/schema.py`, `db/tables.py` | ent#706: the four columns on both tracks |
| `db/sync_state.py` | ent#706: `KEEP` sentinel, the new upsert kwargs, `list_health_rows` (the shared reader) |
| `services/operator_queue_service.py` | ent#706: `sync-diverged-` in `_RESERVED_ID_PREFIXES` |
| `src/scheduler/sync_freeze_policy.py` | ent#706: byte-identical mirror of the policy |
| `src/scheduler/database.py` | ent#706: `sync_freeze_reason` (+ `should_freeze_schedules` as its bool view); the threshold is imported from the mirror |
| `src/scheduler/service.py` | ent#706: the gate writes `Git sync frozen: <reason>` into the skipped row |
| `main.py` | Starts `SyncHealthService` (staggered +5 s, PERF-269); registers `fleet_router` |

### Agent server

| File | Purpose |
|------|---------|
| `agent_server/auto_sync.py` | `run_auto_sync_loop`, env-gate helpers, FastAPI startup hook |
| `agent_server/main.py` | Calls `schedule_auto_sync_if_enabled(app)` |
| `agent_server/routers/git.py` | `_compute_ahead_behind`, `_dual_ahead_behind_payload`, `_run_auto_sync_once`, `_read_sync_state_file`, `_write_sync_state_file`. `get_git_status()` now returns both tuples + merges the persisted sync-state. #2742: `get_git_status()` is a thin loop-level single-flight over `_compute_git_status()` (one `to_thread` worker per in-flight run); `--no-optional-locks` on the status read; the seven status children plus `_compute_ahead_behind` / `_get_pull_branch` / `_persist_last_remote_sha` route through `run_registered`; `_record_lock_recovery` + `_index_lock_stuck` (observe-only); `remote_url` unconditionally `redact_url_userinfo`-d; `_read_sync_state_file` size-gated at 64 KiB. |
| `docker/base-image/startup.sh` | #2742: the #1595 boot lock reap becomes test-then-remove, echoes each lock it cleared, and writes `~/.trinity/lock-recovery.json`. |

### Frontend

| File | Purpose |
|------|---------|
| `stores/agents.js` | `syncHealth` state + `fetchSyncHealth()` action |
| `utils/syncHealth.js` | `classifySyncHealth`, `syncHealthColor`, `syncHealthLabel` — render the backend's `state` / `reason` (ent#706), no thresholds |
| `utils/syncSummary.js` | `formatSyncSummary` / `syncChip` / `syncTextClass` — the card's numbers (`↑7 ↓0 · 12 dirty · pushed 3h ago`); display only, `now` passed in, kind and colour from the backend `state` (ent#707) |
| `components/AgentTile.vue` | One sync chip (replaces `sync failing ×N` + `git ✓`): kind from state, the numbers as text, reason — recommendation + absolute push time on hover (ent#707) |
| `components/OverviewPanel.vue` | Footprint `Sync:` line from the whole `/git/sync-state` payload, state colour one tier up on chrome, `—` with no observation; the attention count still counts failed syncs (ent#707, D13) |
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
- `tests/unit/test_2742_git_status_lock_free.py` — #2742 agent-server:
  the flagged argv on the status path and the plain argv absent; the real
  route never rewrites `.git/index` and is never observed holding
  `.git/index.lock` while the legacy argv is (the control asserts the
  **sighting**, never a rewrite — git's racily-clean rule makes a rewrite
  assertion fail once fixture setup crosses ~1 s, which is ordinary under
  CI's `-n auto`); every status child is sweep-registered, including on the
  locked `sync`/`pull` paths; five concurrent callers ⇒ one `git fetch`, one
  `to_thread`, five identical payloads; `shield` keeps the leader alive
  through follower cancellation; leader exception fans out and clears the
  slot; the stuck-lock observer reports without deleting, never reports a
  *changing* candidate, resolves a `gitdir:` file, skips a symlinked `.git`,
  takes no repo lock, and cannot 500 the read; a tokenized non-`github.com`
  origin comes back with no userinfo. **AC4** ships in three phases,
  strongest first: a deterministic-and-concurrent SIGSTOP freeze of a real
  `git status` child holding the lock (the gate), a lock-sighting sampler,
  and a threaded witness whose control arm must reproduce a failure in-run
  or `pytest.skip` — it must never pass without having demonstrated it can
  fail.
- `tests/unit/test_2742_sync_health_leader_lock.py` — #2742 backend:
  distinct worker ids, one leader of two, own-lease refresh, Redis-down and
  Redis-error fail **open**, release hands off and only deletes its own
  lease, TTL expiry lets a sibling take over, `_leader_ttl()` floor, the
  poll cycle lists agents iff leader — **plus the alert-timing test the
  lease owes**: with one leader a failing agent crosses `ALERT_THRESHOLD`
  after 3 cycles, i.e. ~180 s where two unleased workers took ~90 s.

Baseline: 75 passing tests added across the two PRs.

trinity-enterprise#706 (divergence age and the freeze):

- `tests/unit/test_ent706_sync_policy.py` — `classify`, one case per rule, the
  24 h boundary at ± 1 s, fork-to-own vs deployment, the stale-observation
  fail-open, aware / naive / offset timestamps agreeing, the recommendations.
- `tests/unit/test_ent706_sync_policy_parity.py` — backend ↔ scheduler byte
  parity; the policy is a stdlib leaf; the scheduler imports the threshold.
- `tests/unit/test_ent706_sync_state_columns.py` — both migration tracks, the
  SQLite migration run twice, and the upsert's set / keep / clear.
- `tests/unit/test_ent706_divergence_tracking.py` — the real `_poll_cycle`:
  the clocks, `last_successful_push_at`, and the one-item-per-episode alert
  (two pollers → one row; a new episode → a second; **control arms**: freeze
  flag off or a deployment → zero items).
- `tests/unit/test_1808_sync_freeze_enforcement.py` — the scheduler gate on a
  real SQLite file: divergence freezes at 24 h + 1 s, not at 24 h − 1 s;
  **control arm**: the same row with `diverged_since` NULL fires; the skipped
  row carries the reason.
- `tests/unit/test_ent706_internal_sync_health.py`,
  `tests/unit/test_ent706_sync_health_surfaces.py` — the three read surfaces
  keep their old keys and add the verdict.
- `src/frontend/tests/unit/syncHealth.spec.js` — the dot renders the backend
  state and reason and holds no threshold.
- `tests/unit/test_ent707_fleet_health_sync.py` — `/api/monitoring/status`:
  the per-agent `sync` block, `sync: null` without a binding, the fleet totals,
  the red-sync issue string, status untouched, accessible-only, no raw error
  text anywhere (seeded marker), a sync-reader fault degrades to null.
- `tests/unit/test_fleet_sync_audit.py` (ent#707 class) — the audit's new keys,
  `dirty_tree` from `dirty_files`, the summary counts, no raw error text.
- `src/mcp-server/src/tools/monitoring.test.ts` — `get_fleet_health` passes
  `sync` / `sync_summary` / `issues` through; `get_fleet_sync_audit` returns the
  audit unchanged; its policy row.
- `src/frontend/tests/unit/syncSummary.spec.js`, `agentTileSyncChip.spec.js`,
  `overviewPanelSync.spec.js` — the formatter, and MOUNTED AgentTile /
  OverviewPanel: chip kind per state, text, hover without the raw error, the old
  chips gone, `—` with no observation, the attention count unchanged (D13).

The `.gitignore` half of §0 has its own real-git suites (no Docker either — they
run the SHIPPED builder commands against throwaway repositories, because the
defects live in git's own last-match-wins and dir-descent semantics):

- `tests/unit/test_2069_gitignore_at_creation.py` — the creation-time seed:
  readiness gate, merge-only (no rm-cached), the ENV predicate, and the #953
  no-drift contract (now: *already in block shape ⇒ no drift*).
- `tests/unit/test_2069_gitignore_merge_caller_guard.py` — the AST writer-SET
  guard: exactly three callers of `_build_gitignore_merge_command`, and
  `_GITIGNORE_PATTERNS` read by that builder alone.
- `tests/unit/test_2070_trinity_authored_paths.py` — the authored-vs-runtime
  `.trinity/` split survives a Push sweep.
- `tests/unit/test_2529_gitignore_precedence.py` — precedence over two
  consecutive Pushes, the protected floor, merge-command hardening (CRLF, NUL
  byte, unreadable file, no line-gluing), and the three report fields, including
  AC-1 as the universal `before − after == set(removed_paths)`.
- `tests/unit/test_1908_bundled_template_gitignore.py` — the 14 bundled
  templates are byte-identical to the merge's fixed point.
- `src/frontend/tests/unit/gitSyncSweepToast.spec.js` and
  `src/mcp-server/src/tools/git.test.ts` — the sweep report on the UI and MCP
  surfaces (Invariant #13).

## Operator Controls

| Control | How | Default |
|---------|-----|---------|
| Auto-sync on/off per agent | `PUT /api/agents/{name}/git/auto-sync` body `{enabled: bool}` | `true` for non-source-mode GitHub-template agents |
| Interval override | `GIT_SYNC_INTERVAL_SECONDS` env var in the agent container | 900 s (15 min) |
| Fleet kill-switch | `GIT_SYNC_AUTO` env var (if missing/false the loop never starts) | `true` if the backend set it at creation **or** `auto_sync_enabled = 1` — re-derived as the OR of both on every container rebuild (ent#109) |
| Freeze schedules when sync failing | `PUT /api/agents/{name}/git/freeze-schedules-if-failing` — since ent#706 it also freezes a **work agent** diverged from origin for more than 24 h (fresh observation). It is the only switch: there is no fleet kill switch and no env knob; the 24 h clock that starts at the first post-upgrade poll is the soak | `false` (opt-in) |
| Divergence / dirt / no-heartbeat / freshness thresholds (ent#706) | Constants in `services/sync_freeze_policy.py` (and its scheduler mirror) — deliberately not env knobs, so the dashboard and the freeze cannot be configured apart | 24 h / 24 h / 7 d / 15 min |
| Alert threshold | Hardcoded in `SyncHealthService.ALERT_THRESHOLD` | 3 consecutive failures |
| Sync-health poll cadence (#2742) | `SYNC_HEALTH_POLL_INTERVAL_SECONDS` env var on the backend — read at **call** time (a property, not an import-time copy), and wired into `docker-compose.yml`, `docker-compose.prod.yml` and `.env.example` as `${VAR:-60}`. Prod compose launches standalone (no base merge, no `env_file:`), so the explicit `environment:` list is the only route in; `/validate-pr` caught all three missing on this branch (the #1056 packaging class), and `test_2742_sync_health_leader_lock.py::TestPollIntervalReachesTheContainer` now pins the form so unset and empty both land on the default | 60 s (**unchanged** — the knob ships, the default does not move) |
| Stuck-lock report sensitivity (#2742) | `_STUCK_LOCK_MIN_SIGHTINGS` / `_STUCK_LOCK_MIN_AGE_SECONDS` module constants in `agent_server/routers/git.py` — deliberately **not** an env var: the tunable is the number of stable sightings, not a wall-clock age, because age measures the in-flight operation | 3 sightings / 900 s |

## Known Limitations

- **The scheduler's skip row still excerpts the git error.** `sync_view`
  withholds `last_error_summary` from every backend surface's `reason`
  (ent#707), but the scheduler calls the vendored policy directly and its
  `skip_reason` for a `sync_failing` freeze carries up to 120 characters of it,
  as before. It is shown only on that agent's own execution rows.
- ~~**`dirty_tree` in `/api/fleet/sync-audit` is always `false`**~~ — real
  since ent#707 (`dirty_files > 0`, the poller's persisted porcelain count; no
  live agent call).
- ~~`freeze_schedules_if_sync_failing` is read-only from the scheduler
  side~~ — enforced since #1808 (`SchedulerDatabase`), and since ent#706 it
  also covers divergence.
- **A failed `git status` inside the agent reads as a clean tree (ent#706).**
  The agent's status handler returns `changes = []` when `git status` fails,
  so `dirty_files` goes to 0 and `dirty_since` clears. Rare (the read is
  lock-free since #2742); fixing it needs a base-image change.
- **A fork-to-own or bound agent whose owner turned auto-sync off reads as a
  deployment (ent#706)** and never divergence-freezes. `source_mode` stays 1
  for those agents and the binding kind is not persisted; closing the gap
  needs a persisted binding (overlaps trinity-enterprise#704).
- **A starved pull cycles the freeze (ent#706).** An agent with auto-sync off
  whose pull (trinity-enterprise#703) skips while executions run can sit
  behind for > 24 h → freeze → cron stops → the pull lands → divergence clears
  → the next tick fires: at most one short freeze window and one
  `sync_diverged` item a day. It self-heals by design.
- **Raising `SYNC_HEALTH_POLL_INTERVAL_SECONDS` above 15 minutes disables the
  divergence freeze (ent#706)**: every observation is then stale, and the
  freshness guard fails open. `sync_failing` still freezes.
- **The agent writes every number the policy reads** (ahead / behind /
  changes / push time), so a compromised agent can misreport them — the
  existing #1595 trust model; the backend coerces types and ranges only.
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
- **Ref-lock litter is covered by no reaper at all — not even at startup
  (#2742 residual).** `startup.sh`'s `find` is scoped to `.git/refs` and
  `.git/logs`, so `.git/FETCH_HEAD.lock` and `.git/packed-refs.lock` sit
  directly in `.git/` and are removed by nothing. `run_registered` on the
  status children makes a sweep-killed *poll* stop producing them, but a
  sweep-killed **operator** op still can. AC1 measures `index.lock` only —
  the class is narrowed, not closed. Adjacent to #1505; do not read the
  flow as claiming otherwise.
- **A genuinely wedged lock is now *reported* and still needs a restart or
  one `rm` (#2742).** The report is the half of the acceptance criterion
  that was missing — the operator complaint was silence, not the wedge. The
  race-free automatic recovery is a backend-initiated container restart
  (after which the boot reap runs with the PID namespace empty); it is
  deliberately **not** built yet, because it would be new automation firing
  on a signal never once observed in production. Let one release of the
  report say how often it fires first.
- **Neither reaper covers `<gitdir>/modules/*` or `<gitdir>/worktrees/*`
  (#2742).** The new *observer* does, but `startup.sh` and
  `_reap_stale_git_litter` do not — so a submodule lock wedge is permanent
  and survives every restart. Filed separately; not absorbed here.
- **Coalescing is bounded staleness (#2742).** A follower can be served a
  snapshot up to one leader-run old; `computed_at` makes it legible rather
  than denied. TTL caching was rejected — it would reintroduce the
  overlapping `git fetch`.
- **`--no-optional-locks` is not free, and the cost is governed by content
  bytes, not file count (#2742).** The index writeback the flag suppresses
  is also what caches the stat results, so a stat-dirty tree re-hashes on
  every poll instead of once. Re-measured inside a real agent container
  (ext4 workspace volume, git 2.39.5), each tree made stat-dirty then run
  eight times back-to-back:

  | tree | plain, steady | flagged | ratio |
  |---|---|---|---|
  | 42 491 files / ~1 MB content | 0.08 s | 0.08 s | **~1×** |
  | 1 500 files / 294 MB content | 0.001 s | 0.389 s | **~390×** |

  So the penalty is driven by how many **bytes** git must re-hash, not by
  index size: a big-but-light tree pays essentially nothing, while a
  byte-heavy one pays ~0.39 s of CPU on **every** poll where plain would
  settle to ~0.001 s. The absolute number is the one to reason about —
  ~0.39 s per agent per 60 s poll, worst for exactly the auto-sync-off
  agents whose index nothing else ever refreshes. (An earlier single "29×"
  figure, taken outside the fleet, sat between these two regimes and
  described neither.) The settling is visible in the raw series: plain runs
  0.395 / 0.390 / 0.392 s and only then drops to 0.001 s — git's
  racily-clean rule, which is also why the obvious mitigation
  (`git update-index --refresh` on a low cadence) did **not** restore the
  fast path in measurement, so it is deliberately *not* in the code. AC1 is
  not negotiable and the flag is the only thing that satisfies it; the cost
  is open, quantified and owned, not silently accepted.
- **Older base images keep the old handler until recreate (#2742).** The
  agent-server half ships in the base image, so the fleet converges via
  `build-base-image.sh` + agent recreate — which is also what clears every
  already-orphaned lock in the installed base, through the boot reap. The
  backend lease alone halves exposure immediately.

## Related Flows

- [github-sync.md](github-sync.md) — the pull/push infrastructure this
  feature builds on
- [github-repo-initialization.md](github-repo-initialization.md) —
  where instance IDs and working branches come from
- [operating-room.md](operating-room.md) — the operator queue where
  `sync_failing` and (ent#706) `sync_diverged` entries surface

## References

- Upstream epic: [abilityai/trinity#381](https://github.com/abilityai/trinity/issues/381)
- Sub-issues: #389 (S1), #390 (S6); coordinates with #382 (S7)
- Spec: `ability-trinity-git-improvements-proposal.md` (§P1, §P5, §P6,
  §S1, §S1a, §S6)
