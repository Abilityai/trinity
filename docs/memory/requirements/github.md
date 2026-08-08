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

### 11.11 PAT-Free Clone of Public Templates (trinity-enterprise#123)
- **Status**: ✅ Implemented (2026-07-23) — gating prerequisite for the ent#122
  fresh-install fleet provisioning epic (first-run manifest seed ent#124, UI
  manifest install ent#126).
- **Description**: Creating an agent from a **public** `github:owner/repo`
  template with **no** GitHub PAT anywhere (no per-agent, per-user, or global
  token) succeeds via an **anonymous, read-only, source-mode clone**. Previously
  the create path hard-failed 400 without a token and `startup.sh` skipped the
  clone entirely when `GITHUB_PAT` was empty.
- **FR-1 — Tokenless create is source-mode only**: a tokenless request with
  `source_mode` falsy (explicit `False` **or** `None`) fails with a named 400
  ("bidirectional git sync requires write credentials") — working-branch mode
  pushes at boot and is impossible anonymously. Fork-to-own requests are
  unaffected (they carry the user's PAT).
- **FR-2 — Visibility probe rides the git transport, not the REST API**:
  the tokenless path validates reachability via a credential-less
  `git ls-remote <url> HEAD` (`git_service.probe_anonymous_repo_access`) — the
  same transport the container clone uses, immune to the anonymous REST 60/hr
  cap. Outcomes: reachable → proceed; definitive auth-challenge/not-found →
  named 400 *"not found or private — add a GitHub token"* (anonymous GitHub
  cannot distinguish the two; no new existence oracle); transient failure →
  **fail-closed 502** (same transport ⇒ the clone would fail too; avoids a
  silently-empty agent while monitoring is default-off). PAT-ful validation
  is unchanged (REST probe, GitHubError → 502).
- **FR-3 — Container env carries no token vars**: `GITHUB_PAT`/`GH_TOKEN`/
  `GITHUB_TOKEN` are set only when a PAT exists; `GITHUB_REPO` +
  `GIT_SYNC_ENABLED` + `GIT_SOURCE_MODE` are set for every github-template
  agent. Applies to creation (`_apply_github_env`) AND the rebuild-recovery
  seam (`lifecycle._apply_persisted_auth_env`) so a tokenless agent rebuilt
  after container loss still clones (silent-empty-agent class, #843/#1439).
- **FR-4 — startup.sh clones anonymously**: clone gate is `GITHUB_REPO`-only;
  `CLONE_URL` embeds `oauth2:<PAT>@` only when a PAT is present (else the
  credential-less form, mirroring the fork-to-own `UPSTREAM_URL`); tokenless
  git network ops run with `GIT_TERMINAL_PROMPT=0` (deterministic fail-fast);
  on restart the baked-env PAT falls back to the workspace `.env` value
  before the origin URL is rewritten (preserves a live-injected per-agent PAT
  across ops-path raw restarts, #1264/#1089). Tokenless agents get a
  **blackholed push remote** (self-describing invalid push URL) so any
  in-container `git push` fails legibly.
- **FR-5 — Push surfaces fail honestly**: backend push paths (`sync_to_github`,
  `reset_to_main_preserve_state`) pre-check write credentials — baked env
  `GITHUB_PAT` **or** the per-agent PAT row (never the global tier, which
  cannot reach a tokenless container) — and return conflict_type
  `no_write_credentials` with the actionable message. MCP `git_sync` suppresses
  the "resolve via chat" hint for this conflict type.
  **Amended by §11.12 (ent#109):** the message no longer teaches the manual
  workaround ("create a new agent with fork-to-own + data import"). It now points
  at **Bind to your own repo** in *this* agent's Git tab — the in-place retrofit
  that keeps the accumulated workspace — or at adding a GitHub token. Both
  surfaces change together (Invariant #13): `git_service.NO_WRITE_CREDENTIALS_MESSAGE`
  and the MCP `git.ts` 409 hint.
- **FR-6 — Private repos with no token fail with a named error**, never a raw
  500 (FR-2's combined 400 at create; auth-classified clone failure +
  `.git-clone-status` marker in-container if a repo goes private later).
- **Mixed-fleet caveat (release note)**: an un-rebuilt base image still has the
  old `GITHUB_PAT`-gated clone block — tokenless creation on an old image
  yields an empty agent with no failure marker. Base-image rebuild is a hard
  ordering requirement when upgrading.
- **Source of truth**: `services/agent_service/crud.py`
  (`_resolve_github_repo_and_pat`, `_validate_github_access`,
  `_apply_github_env`), `services/agent_service/lifecycle.py`
  (`_apply_persisted_auth_env`), `services/git_service.py`
  (`probe_anonymous_repo_access`, write-credential guard),
  `docker/base-image/startup.sh`.
- **GitHub Issue**: trinity-enterprise#123 (Epic ent#122)

---

### 11.12 Post-Creation Repo Binding — "Bind to your own repo" (trinity-enterprise#109)

- **Status**: ✅ Implemented (2026-08-02) — the last gate in the ent#122
  fresh-install fleet-provisioning flow; supersedes ent#230.
- **Description**: Point a **live** agent at a GitHub repo the **user** owns,
  creating the repo if needed, from the agent's *current workspace* — not from
  the template. Closes the ownership retrofit that §11.11 opened: a tokenless
  public-template agent (ent#123, e.g. the default Cornelius) accumulates a
  knowledge base it cannot push anywhere, and the only previously documented
  escape was "create a new agent with fork-to-own and import your data", which
  discards the agent's identity, name reservation, and history.
- **Framing**: this is a **rebind**, not a fork verb. The operation is *"point
  this agent at a GitHub repo you own."* An agent that already has a writable
  repo is therefore an ordinary rebind (typo'd destination, wrong PAT account,
  org migration, partial-failure retry), not a refusal — which is what makes
  ent#109 AC #3 ("works for any agent") literally true.
- **Endpoint**: `POST /api/agents/{name}/git/bind-to-own-repo` (+
  `GET /api/agents/{name}/git/bind-to-own-repo/status` so a client that eats a
  proxy timeout can resolve the outcome). Orchestration lives in
  `services/agent_service/repo_binding.py` (Invariant #1); the router is a thin
  HTTP mapper over domain errors.

- **FR-1 — Supported row states are an explicit table; the unclassified is
  refused by name, never guessed.** Partitioned by `source_mode`, which is the
  column `idx_git_config_repo_branch_unique` actually keys on
  (`WHERE source_mode = 0`) — *not* by write-credential state, which is an
  orthogonal column and would mis-route:

  | Row state | Handling |
  |---|---|
  | No `agent_git_config` row (`local:` template agent) | 400 `BIND_NO_GIT_CONFIG`, pointing at Initialize GitHub Sync. Deferred; see the State-A note below. |
  | Row, `source_mode = 1`, any credential state | **Supported — the main path.** Includes tokenless (Cornelius) *and* already-writable ent#93 fork agents. |
  | Row, `source_mode = 0` (carved `trinity/<agent>/<id>` working branch) | 409 `BIND_WORKING_BRANCH_MODE_UNSUPPORTED` — rebinding moves the row *within* the partial unique index and needs a branch re-reservation. Out of scope explicitly. |
  | Row present, container has no `.git`, or the live `origin` disagrees with the row | 409 `BIND_STATE_UNCLASSIFIED`, reporting **both** observed values. |
  | `is_system` agent (`trinity-system`) | Refused by the existing `BIND_NO_GIT_CONFIG` path — it is provisioned from a bundled template and has no git-config row, so it never reaches the container recreate. Asserted by test, not inferred. |

  Every refusal is **structural** ("this shape needs machinery this feature does
  not build"), not product gating, and each names the state and the next action.

  **Resumption exception.** When the row *already names the requested
  destination*, the operation is a retry of a partially-applied bind and the
  last two refusals do not apply: `origin` is allowed to lag the row, and
  branches already in the destination are the agent's own pushed history. Both
  are safe — `origin` never selects what is pushed (step 4 pushes by explicit
  URL and writes `origin` afterwards), and the push carries no `--force`/`+`
  refspec so unrelated history is rejected non-fast-forward. Without this,
  every post-commit failure message promises a retry that returns 409.

- **FR-8 — A GitHub PAT must be header-safe before it leaves the model.**
  A PAT is sent as `Authorization: Bearer <pat>` and embedded in a git remote
  URL; h11 rejects an illegal header value by **echoing** it, so a token
  carrying `\r`/`\n` — what a paste from a terminal or clipboard routinely
  produces — puts the raw credential in an error response and the
  Vector-captured platform log. `models._validate_pat_secret` strips surrounding
  whitespace (the common case must keep working) and rejects anything outside
  printable ASCII, for **both** `BindAgentRepoRequest` and `ForkToOwnRequest`.
  Because Pydantic v2 records the rejected value in `errors()["input"]` and
  FastAPI returns `exc.errors()` verbatim, `error_handlers.validation_error_without_input`
  strips `input` from every 422 entry — otherwise the guard would relocate the
  leak rather than close it.
  No refusal is reachable for the flagship tokenless agent or for a user
  re-running the operation after a typo or a partial failure.

- **FR-2 — `source_mode` is preserved at `1`; no branch reservation.** A rebind
  does **not** flip to working-branch mode. `source_mode` means *"track the
  source branch instead of carving `trinity/<agent>/<id>`"* — **not** read-only:
  ent#93's own fork-to-own agents are source-mode and still auto-push. Keeping
  it at `1` means the user's default branch holds their captures, `working_branch`
  is untouched, and no `reserve_and_generate_instance_id` call is needed. That the
  row also stays outside the partial unique index is a *fact about the index*,
  not the safety argument — FR-3 supplies the actual safety.

- **FR-3 — Concurrency: destination-scoped lock + a real CAS + a compensating
  restore.** The collision this feature can produce is *two different agents
  binding one destination repo*, so:
  - **`agent:bind_dest:{sha256(lower(destination))}`** — the lock that actually
    serializes the collision. SETNX + TTL, and **fail-closed (503 +
    `Retry-After`)**: a lost lock here means two repo creates, two DB writes,
    and two concurrent recreates of one container, so the export/import lock's
    fail-open calibration does not transfer.
  - **`agent:bind_op:{name}`** — agent-scoped, guards double-submit on one agent.
  - **CAS**: `db.rebind_git_config()` is a single
    `UPDATE agent_git_config SET github_repo=:new, source_branch=:default,
    auto_sync_enabled=1 WHERE agent_name=:a AND github_repo=:expected_old`.
    rowcount 0 ⇒ 409 `BIND_CONCURRENT_MODIFICATION` with **nothing partial
    written**. The predicate is named in the function's docstring.
  - **Loser path is a compensating `UPDATE` restoring the captured previous
    values — never `delete_git_config`.** On a *pre-existing* row a delete is
    destruction, not rollback: it strips a live agent's binding, so the next
    recreate drops `GITHUB_REPO` (the silently-empty-agent class, #843/#1439).
  - The ent#93 post-write re-check against
    `get_git_config_agent_names_for_repo` is kept as a **belt** (409
    `BIND_DESTINATION_IN_USE`), not as the mechanism.

- **FR-4 — The PAT is persisted LAST, after the container owns the new repo.**
  Ordering: classify → create/inspect destination → capture previous values →
  **CAS commit point** → in-container push + origin rewire → `set_agent_github_pat`
  → recreate → verify + audit. The in-container push uses the *request's* PAT
  directly and never needs the persisted row, so writing the credential early
  buys nothing and costs correctness: `_agent_has_write_credentials` reads the
  per-agent PAT row, so an early write makes the agent look already-writable on
  a retry and makes a mid-window manual push succeed against the **old** repo
  with the **new** token. A failure before the PAT write leaves the agent exactly
  as not-writable as it was, and the retry is clean.

- **FR-5 — A container recreate is mandatory; a DB-only rebind is silently
  reverted.** `docker/base-image/startup.sh`'s "repository already exists"
  branch rewrites `git remote set-url origin` **unconditionally** from the baked
  `GITHUB_REPO`, and the workspace-`.env` fallback covers `GITHUB_PAT` only —
  there is no `GITHUB_REPO` fallback. So the operation ends in
  `recreate_container_with_updated_config()`, which re-bakes the git env from
  the DB via `_apply_git_env_from_db` (`pat_gate="per_agent_only"`; this is why
  FR-4's PAT write must precede the recreate — the gate reads the per-agent PAT
  row). **A recreate is not a re-provision**: the same volumes are reused via the
  `volume_base_name` pin (#1664), and `agent_ownership` plus the 180-day name
  reservation are untouched (ent#109 AC #7). Consequently the S4 persistent-state
  allowlist (AC #2) is preserved **by construction** — the workspace volume is
  never detached and no snapshot/overlay step is required.

- **FR-6 — Owner-only *and* human-only, with explicit PAT disclosure.**
  `OwnedAgentByName` (uniform 404 for unknown *and* inaccessible, Invariant #8)
  **plus** `reject_agent_principal`. A role gate alone is insufficient: an
  agent-scoped key resolves to its owner *carrying the owner's role*, so on a
  default admin-owned install any agent's injected `TRINITY_MCP_API_KEY` would
  satisfy it (the trinity-ops-agent#232 trap; #1644/#1816 precedent). Blast
  radius is operator-scale — it creates external GitHub state, persists a
  credential, and replaces the container. The PAT is `SecretStr`, unwrapped once
  at the service boundary, persisted AES-256-GCM (Invariant #12), and **never**
  logged: every message crosses `scrub_secret(text, user_pat)` **and**
  `redact_url_userinfo` (git stderr can embed a *stale baked* token that is not
  `user_pat`). The UI carries the create-path disclosure verbatim — *the agent
  can read its own git credential, so prefer the narrow token*. **No MCP tool**:
  it would push a user PAT through the MCP layer.

- **FR-7 — The `no_write_credentials` surfaces point at this action.** Both
  surfaces change together (Invariant #13): `git_service.NO_WRITE_CREDENTIALS_MESSAGE`
  (consumed by `sync_to_github` and `reset_to_main_preserve_state`, mapped 409 in
  `routers/git.py`) and the MCP `git.ts` 409 hint. Neither teaches
  create-a-new-agent-and-import any more. The in-container blackhole sentinel in
  `startup.sh` is a third, agent-side surface that already reads correctly and is
  deliberately left unchanged.

- **Failure honesty**: a failure *after* the CAS commit point returns a
  structured 502 naming precisely what is saved (the row binding) and what is not
  (origin, PAT, env), and states that retrying converges — the destination
  inspect/create reuses the repo, and the push and rewire are idempotent. It does
  **not** claim self-healing: nothing re-drives the operation on its own.
  `BIND_RECREATE_FAILED` specifically instructs the operator to **start the agent
  through Trinity** to finish, because a *plain* container restart re-runs the
  `startup.sh` origin rewrite and reverts the rebind.

- **State-A note (`local:` template agents)**: deferred. On a fresh install that
  population is the three bundled demo agents plus the `is_system`-protected
  `trinity-system`, and the ent#128 "Trinity-installable agent" work may convert
  bundled templates to `github:` and shrink it further. When it is picked up,
  prefer extending `initialize_github_sync` (which already gates on a running
  container, creates the repo with visibility, pre-reserves the branch, writes
  the row, rolls back on failure, and audits) over a second engine — the gap is
  only the PAT source. Two traps are recorded in the implementation plan:
  `initialize_git_in_container` must be passed `working_branch=None` for
  source-mode, and its `git add .` over `/home/developer` needs
  `.claude/.credentials.json` added to `_GITIGNORE_PATTERNS` first.

- **Source of truth**: `services/agent_service/repo_binding.py` (orchestration),
  `services/agent_service/fork_to_own.py`
  (`inspect_or_create_destination_repo` — the shared destination primitive;
  reuse/refuse **policy** stays in each caller because the create path's reuse
  branch *is* its template-tip SHA comparison),
  `services/git_service.py` (`rebind_origin_and_push`,
  `NO_WRITE_CREDENTIALS_MESSAGE`), `db/schedules/git_config.py`
  (`rebind_git_config` — the CAS), `routers/git.py` (endpoint + status + locks +
  audit), `services/agent_runtime_state.py` (`agent:bind_op:` /
  `agent:bind_dest:` exemptions), `models.py` (`BindAgentRepoRequest` /
  `BindAgentRepoResponse`), `src/frontend/src/components/GitPanel.vue`,
  `src/mcp-server/src/tools/git.ts`.
- **Flow**: `docs/memory/feature-flows/agent-repo-binding.md`
- **GitHub Issue**: trinity-enterprise#109 (Epic ent#122); supersedes ent#230

---

### 11.13 GitHub-Repo Import Intents — fork / copy / clone (trinity-enterprise#15)

- **Status**: 🔨 In development (2026-08-06)
- **Description**: Creating an agent from a `github:owner/repo[@branch]` template
  accepts an explicit **`import_intent`** — `fork` | `copy` | `clone` — so the three
  user intents ("start from someone else's agent and own my changes" / "give me a
  point-in-time snapshot, no upstream tie" / "this is my agent's repo") stop sharing
  one one-size clone flow. Absent intent → today's behavior exactly (clone semantics;
  fork when a `fork_to_own` block is present) — fully additive.
- **FR-1 — Intent mapping (parameterizes existing machinery, no new clone engine)**:
  - `fork` → the ent#93 fork-to-own path unchanged; requires the `fork_to_own` block
    (400 `FORK_PARAMS_REQUIRED` without it).
  - `clone` → today's default `github:` path unchanged (`source_mode` true/false as
    before); a stray `fork_to_own` block with explicit `clone`/`copy` intent is a
    400 `INTENT_FORK_BLOCK_CONFLICT` (presence-triggered forking would silently
    create a GitHub repo against stated intent).
  - `copy` → **backend-materialized snapshot** (new): staging clone
    (`--depth 1 --single-branch --branch <source_branch or repo default>` — `@branch`
    is honored) via the fork-to-own git machinery (disk-backed `/data` staging, PAT
    via `GIT_CONFIG_*` env, output scrubbed), `.git` stripped, then the workspace
    volume is pre-populated via the deploy-local primitive
    (`_prepopulate_workspace_from_template`, `.trinity-initialized` included) BEFORE
    the container exists. The container carries **no GitHub env and no PAT**; no
    `agent_git_config` row is written (not sync-polled, git endpoints refuse as for
    `local:` agents); the PAT is used server-side only and persisted nowhere.
- **FR-2 — Copy-mode guards**: empty source (zero files staged) → 400
  `COPY_SOURCE_EMPTY`, never a green blank agent. Private-no-PAT → the ent#123-style
  combined named 400; transient staging failure → 502, fail-fast pre-side-effect.
  Copy does NOT run the ent#123 tokenless source-mode gate (that 400 protects
  boot-time push; copy never pushes — tokenless public copy is legal for any
  `source_mode`). Symlinks in the staged tree are preserved only when their target
  resolves inside the tree; escapers are dropped with a warning; never followed.
  Copy + `ephemeral` → 400 `COPY_EPHEMERAL_UNSUPPORTED` (ghosts are volume-less by
  the ent#69 invariant; the snapshot lives on the workspace volume).
- **FR-3 — Provenance**: `import_intent` + source repo + cloned SHA are recorded in
  the create audit entry's details; the container carries a `trinity.import-intent`
  label. No schema column in v1 (volume-loss rebuild of a copy agent yields an empty
  workspace by design — a snapshot must not silently re-clone CURRENT upstream; the
  documented mitigations are #1169 data export + the advisory compat check going red).
  Own-it-later path: **Initialize GitHub Sync** (copy agents are the same class as
  `local:` agents; `bind-to-own-repo` refuses `BIND_NO_GIT_CONFIG` by design).
- **FR-4 — Inline compatibility check (reuses #668 wholesale)**: the Create Agent
  flow ends with a post-create validation step that polls the existing
  `GET /api/agents/{name}/compatibility` (STATIC only; AI stays on-demand), gated on
  agent-server readiness — a REAL `/info` response, discriminated as **200 without a
  `message` key** (the backend proxy fail-opens to 200 + a fallback body carrying
  `message` while the container is mid-clone, so a bare 200 is NOT readiness; Docker
  "running" races the clone for fork/clone intents and false HARD failures would
  persist via the results upsert; old agent images without the endpoint honestly land
  in the timeout state) — with an honest "agent failed to start" branch on a
  non-running container (checked periodically). Non-blocking — the agent exists
  regardless.
- **FR-5 — Trigger-boundary idempotency (Invariant #18)**: `POST /api/agents` accepts
  `Idempotency-Key` (scope `agent_create:{user_id}` — the scope folds the caller so
  another user's identical key can never replay a foreign create response); replay
  returns the original response + `X-Idempotent-Replay: true`; in-flight duplicate
  409 is named distinctly from name-taken 409. MCP `create_agent` derives a
  deterministic key from call args (name included) so long fork creates survive MCP
  client retries.
- **FR-6 — Surfaces**: UI Create Agent modal gains a 3-way intent selector on the
  free-form GitHub path (default `clone`; fork reveals the existing destination/PAT/
  visibility fieldset; copy explains the no-upstream contract + own-it-later); MCP
  `create_agent` gains `import_intent: "copy" | "clone"` only — fork stays UI-only
  (standing decision: MCP tool args are audit-logged, a PAT arg would persist in
  plaintext). Fork-scope failures name the alternative intents (never silent
  auto-degrade). `fork_to_own: required` catalog templates reject non-fork intents.
- **Source of truth**: `services/agent_service/snapshot_import.py` (copy staging),
  `services/agent_service/crud.py` (intent gates + wiring),
  `services/agent_service/deploy.py::_prepopulate_workspace_from_template` (reused),
  `models.py::AgentConfig.import_intent`, `routers/agents.py` (idempotency),
  `src/mcp-server/src/tools/agents.ts`,
  `src/frontend/src/components/CreateAgentModal.vue` + `ImportValidationStep.vue`.
- **GitHub Issue**: trinity-enterprise#15 (Epic ent#122)

---
