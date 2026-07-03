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

---
