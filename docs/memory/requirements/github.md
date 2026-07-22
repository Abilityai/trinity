# Requirements — GitHub Integration

> Part of Trinity's requirements set. Index & write-path rule: [requirements.md](../requirements.md).

---

## 11. GitHub Integration

### 11.1 GitHub Sync
- **Status**: ✅ Implemented (2025-11-29, Updated 2026-02-28)
- **Description**: Two sync modes - Source (pull-only, default) and Working Branch (bidirectional)
- **Key Features**: Pull button, sync button, content folder gitignored, branch selection via URL syntax or parameter
- **Branch Selection** (GIT-002): URL syntax `github:owner/repo@branch` or explicit `source_branch` parameter in MCP create_agent tool
- **Flow**: `docs/memory/feature-flows/github-sync.md`

### 11.2 GitHub Repository Initialization
- **Status**: ✅ Implemented
- **Description**: Initialize GitHub sync for existing agents
- **Flow**: `docs/memory/feature-flows/github-repo-initialization.md`

### 11.3 Operator-Readable Conflict Diagnosis (S5)
- **Status**: ✅ Implemented (2026-04-19)
- **Description**: Replace the raw git stderr that previously leaked into `GitConflictModal` with structured classification and per-class operator copy so non-developers can understand what failed and what to do.
- **Key Features**:
  - `ConflictClass` enum (7 members: `AHEAD_ONLY`, `BEHIND_ONLY`, `PARALLEL_HISTORY`, `UNCOMMITTED_LOCAL`, `AUTH_FAILURE`, `WORKING_BRANCH_EXTERNAL_WRITE`, `UNKNOWN`) + pure `classify_conflict()` in both backend (`git_service.py`) and agent-server (`utils/git_conflict.py`) — the two runtimes don't share code.
  - 409 responses now carry `conflict_class` in body and an `X-Conflict-Class` header alongside the legacy `X-Conflict-Type`.
  - Frontend `COPY` lookup in `GitConflictModal.vue` renders per-class title/body/recommendation; raw git stderr lives inside an expandable `<details>` element.
  - Pre-S5 fallback preserved for older agent images that don't emit `conflict_class`.
- **GitHub Issue**: #386 (Epic #381)

### 11.4 Parallel-History Detection (S2)
- **Status**: ✅ Implemented (2026-04-19)
- **Description**: When the agent's working branch and the upstream pull-branch share no recent ancestor and both have diverging commits, render a different conflict modal that offers an "Adopt latest upstream (preserve my state)" recovery instead of the (always-wrong) Pull-First / Force-Push pair.
- **Key Features**:
  - `/api/git/status` returns `common_ancestor_sha`, `common_ancestor_age_days`, and `pull_branch` (label-agnostic copy).
  - Frontend `isParallelHistory` predicate: `(no common ancestor OR ancestor age ≥ 30 days) AND behind > 0`.
  - New sibling modal variant in `GitConflictModal.vue`; existing pull/push variants untouched.
  - Primary recovery button calls the S3 endpoint owned by #384 (deferred dependency).
- **GitHub Issue**: #385 (Epic #381)
- **Flow**: `docs/memory/feature-flows/github-sync.md`

### 11.5 Branch Ownership Enforcement (S7)
- **Status**: ✅ Implemented (2026-04-19)
- **Description**: Prevent silent data loss from two agents binding to the same `(github_repo, working_branch)` pair (2026-04-17 alpaca incident).
- **Key Features**:
  - Layer 0/1: single `reserve_and_generate_instance_id` helper atomically generates a UUID, probes `git ls-remote`, and inserts under the partial UNIQUE; retries 5x on collision.
  - Layer 2: partial UNIQUE `(github_repo, working_branch) WHERE source_mode = 0` on `agent_git_config`; migration refuses to install on existing duplicates so operators rebind first.
  - Layer 3: agent-server pushes with `git push --force-with-lease=<branch>:<expected-sha>`; lease rejection returns 409 + `X-Conflict-Type: branch_ownership_collision` and emits a structured alert into `~/.trinity/operator-queue.json` so the Operating Room surfaces the collision.
- **GitHub Issue**: #382 (Epic #381)
- **Flows**: `docs/memory/feature-flows/github-repo-initialization.md`, `docs/memory/feature-flows/github-sync.md`

### 11.6 Persistent-State Allowlist (S4)
- **Status**: ✅ Implemented (2026-04-19)
- **GitHub Issue**: #383 (primitive); consumer #384 (S3 reset-preserve-state, pending)
- **Description**: Named allowlist of workspace paths that must survive a template-level reset. Materialized to `.trinity/persistent-state.yaml` at agent creation so runtime sync/reset paths don't depend on the 10-minute `template.yaml` cache. Operator-editable per-agent.
- **Key Features**: Default five-pattern list (`workspace/**`, `.trinity/**`, `.mcp.json`, `.claude.json`, `.claude/.credentials.json`); per-template override via `persistent_state:` key; readers on backend and agent-server with default fallback
- **Scope**: Primitive only — the reset-preserve-state operation that consumes it lands in #384
- **Flow**: `docs/memory/feature-flows/persistent-state-allowlist.md`

### 11.7 Reset-to-Main-Preserve-State (S3, #384)
- **Status**: ✅ Implemented (2026-04-18)
- **Description**: First-class UI-accessible recovery path for the parallel-history deadlock — hard-reset the agent's working branch to `origin/main` while preserving files listed in the persistent-state allowlist (#383 / S4)
- **Key Features**: Pre-destructive backup to `.trinity/backup/<iso-ts>/`, `git push --force-with-lease`, three 409 guardrails (`agent_busy`, `no_git_config`, `no_remote_main`), owner-only auth, integration with S4's `_read_persistent_state()` reader
- **Endpoint**: `POST /api/agents/{name}/git/reset-to-main-preserve-state`
- **Flow**: `docs/memory/feature-flows/github-sync.md` (Recovery section)

### 11.8 Git Sync Health Observability (#389, #390)
- **Status**: ✅ Implemented (2026-04-19)
- **Description**: Per-agent sync-state tracking, 15-min auto-sync heartbeat, dashboard health dot, operator-queue alerts on consecutive failures, and a fleet-wide audit endpoint with duplicate-binding detection. Fixes P1 (silent desync) and P6 (working-branch divergence hidden) from the git-improvements proposal.
- **Key Features**:
  - `agent_sync_state` table + `auto_sync_enabled` / `freeze_schedules_if_sync_failing` flags on `agent_git_config`
  - 15-min `GIT_SYNC_AUTO` heartbeat loop in the agent container (default-on for non-source-mode GitHub-template agents)
  - Dual `ahead_main`/`ahead_working` tuples in `GET /api/git/status` (P6 fix)
  - `SyncHealthService` emits `sync_failing` operator-queue entries at `consecutive_failures ≥ 3`
  - `GET /api/agents/sync-health` (batch) + dashboard dot
  - `GET /api/fleet/sync-audit` with `duplicate_binding` flag (§P5 query)
- **Flow**: `docs/memory/feature-flows/git-sync-health.md`
- **Upstream**: Epic #381 — sub-issues #389 (S1), #390 (S6)

### 11.9 Survivable Git Maintenance — platform-owned repo upkeep (#1595, extends #1596)
- **Status**: ✅ Implemented (2026-07-14)
- **Description**: Git's own auto-gc can never complete inside an agent container (a detached `gc --auto` reparents to PID 1 and the #817 orphan sweep SIGKILLs it — silent unbounded `.git` bloat; 44 GB / 97%-garbage repos observed in production). The platform disables agent-side auto-gc and owns maintenance via a survivable, guarded pass in the auto-sync loop.
- **Maintenance-ownership contract**: the base image ships `gc.auto=0`, `gc.autoDetach=false`, `maintenance.auto=false`, `maintenance.autoDetach=false` (`/etc/gitconfig`; `git_service` setup mirrors them into `~/.gitconfig`); the auto-sync loop's maintenance pass is the **single owner** of `/home/developer/.git` upkeep. Named residuals: sub-repos cloned into the workspace get no maintenance (blind spot, same as pre-fix); maintenance bounds *garbage*, not *history* — unbounded history growth stays with the deferred squash / geometric-repack follow-up.
- **Key Features**:
  - `agent_server/utils/registered_run.py` — the one seam for agent-server child subprocesses: orphan-sweep registration (`add_transient_pid`, call-time TTL) + process-group timeout kill (`start_new_session` + `killpg`)
  - Auto-sync cycle off the event loop (`asyncio.to_thread`) with a non-blocking repo lock; mutating git endpoints 409 `agent_busy` under contention
  - Trigger: packs ≥ `GIT_MAINTENANCE_PACK_THRESHOLD` (20) OR loose ≥ `GIT_MAINTENANCE_LOOSE_THRESHOLD` (6700); guards: free-disk preflight, exponential failure backoff (1h→24h), env-tunable budget (`GIT_MAINTENANCE_TIMEOUT_SECONDS`)
  - Concurrent-writer safety: `repack -A -d -l --unpack-unreachable=1.hour.ago` + `gc --prune=1.hour.ago` (never `--prune=now`); `pack.threads=1` + `pack.windowMemory=128m` RSS bound
  - Stale-lock hygiene: startup reap (`index.lock`, `gc.pid`, `maintenance.lock`, ref/reflog locks) + per-cycle age-gated reap incl. abandoned `tmp_pack_*`
  - Signal: `pack_count`/`loose_objects`/`maintenance_failures` in sync-state → `agent_sync_state` (agent-supplied ints coerced at the boundary) → `GET /api/agents/sync-health`; edge-triggered `git_bloat` operator alerts (`GIT_DIR_ALERT_BYTES` default 10 GiB; 3 consecutive maintenance failures)
- **Rollout**: base-image rebuild + agent recreate required; recovery of pre-existing bloated fleets is ops-side (trinity-ops-agent#127) using the tunable budget + memory bounds
- **Flow**: `docs/memory/feature-flows/git-sync-health.md`
- **Related**: #1505 (general sweep-subtree seam — evidence cross-filed), #1501 (transient-pid seam), #1596 (threshold repack, superseded parameters)

### 11.10 Per-User GitHub PAT (ent#162)
- **Status**: ✅ Implemented (v0.8.5 payload) — OSS-core half of a private feature.
- **Description**: A non-admin user can store **one personal GitHub token** in
  their own settings, so agents they create are no longer confined to the admin's
  global-PAT repo scope. It is a per-user, self-service credential — set/cleared
  only by its owner — that feeds the agent-**creation** git identity. Storage:
  a new credential-bearing column `users.github_pat_encrypted`.
- **FR-1 — Who can set it (self-service, owner-only)**: three endpoints on the
  caller's **own** account (`Depends(get_current_user)`; never another user's):
  - `GET /api/users/me/github-pat` → `{configured: bool, has_global: bool}` —
    **status only, the token is never returned** (see FR-5).
  - `PUT /api/users/me/github-pat` → validates the token against GitHub before
    storing (**honest validation**: GitHub-rejected → 400; GitHub-unreachable →
    503 — never "your token is bad" when we never got an answer), then encrypts
    at rest. Returns the resolved `github_username`, never the token.
  - `DELETE /api/users/me/github-pat` → clears it; agents already created under
    it keep their own persisted per-agent copy (#347) and are unaffected — only
    **future** creations fall back to the global PAT.
- **FR-2 — Three-tier resolution ladder (agent CREATE path)**:
  `settings_service.resolve_github_pat(agent_name, owner_id) -> (pat, tier)`
  resolves in strict order and returns the tier so the create path can key its
  persist decision:
  1. **`per_agent`** — the agent already has its own PAT (#347 explicit override).
  2. **`per_user`** — the **agent owner's** personal PAT, read **live** by
     `owner_id`. Resolution keys on **ownership only**, never on a calling/sharing
     user, so a sharee can never inject their PAT as the agent's git identity.
  3. **`global`** — the admin-set / env global PAT.
  4. **`none`** — nothing configured (`pat == ""`).
- **FR-3 — Persist carve-out (which tiers become the per-agent PAT)**: at
  creation the resolved value is persisted as the agent's #347 per-agent PAT for
  **`per_agent`/`per_user`** — **but NEVER for `global`**. A global-fallback agent
  deliberately keeps `github_pat_encrypted` **NULL** so
  `github_pat_propagation_service` continues to reach it when an admin rotates the
  global PAT (ent#162 Decision 2). Persisting the global value there would sever
  that propagation and freeze the agent on a stale token.
- **FR-4 — Recreate/restart ladder is 2-tier, never re-derives per-user**: the
  env-rebuild path on recreate/restart uses
  `settings_service.get_github_pat_for_agent(agent_name)` = **per-agent → global
  only**. It deliberately does **not** consult the per-user tier: re-deriving a
  live per-user PAT there would make `check_github_pat_env_matches` reactive, so
  **adding or rotating a personal token in Settings would force-recreate the
  owner's running agents and kill in-flight work.** The per-user tier is a
  **create-time input only**; adding a personal token never disturbs a running
  agent.
- **FR-5 — Never echoed on read (requirement, not just current behavior)**: no
  read path returns the stored token. `GET /me/github-pat` returns a `configured`
  flag; `PUT` returns the derived `github_username`. This is a standing
  requirement — a future field/endpoint MUST NOT surface the plaintext PAT.
- **FR-6 — Encryption at rest (Invariant #12)**: `users.github_pat_encrypted` is
  an **AES-256-GCM JSON envelope** via `services/credential_encryption.py`;
  plaintext persistence is forbidden. The column is listed among the tables under
  Invariant #12 in `architecture.md` (channel/subscription/PAT credential tables).
  Resolved live by the owner at agent creation; see §Security cross-reference in
  `security.md` §20.9.
- **Source of truth**: resolver `services/settings_service.py`
  (`resolve_github_pat` create-path, `get_github_pat_for_agent` recreate-path);
  column + envelope `db/schema.py` / `db/tables.py` /
  `services/credential_encryption.py`; endpoints `routers/users.py`. Prose it
  supersedes: the `users` table block in `architecture.md`.

---
