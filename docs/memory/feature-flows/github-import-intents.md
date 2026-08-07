# GitHub-Repo Import Intents — fork / copy / clone (trinity-enterprise#15)

> Epic ent#122 (fresh-install fleet provisioning). Requirements: `docs/memory/requirements/github.md` §11.13.

Creating an agent from `github:owner/repo[@branch]` accepts an explicit
`import_intent` — `fork` | `copy` | `clone` — parameterizing the existing
creation machinery instead of forking the clone logic. Absent intent → legacy
behavior exactly (clone semantics; fork when a `fork_to_own` block is present).

## Intent → mechanism map

| Intent | Mechanism | `agent_git_config` row | Container env |
|---|---|---|---|
| fork | ent#93 fork-to-own, unchanged (requires the `fork_to_own` block) | yes (source-mode) | repo=user's fork, PAT, `GIT_UPSTREAM_REPO`, sync=true |
| clone | legacy default path, unchanged | yes | repo, PAT if any, sync=true |
| copy | **backend-materialized snapshot** (new) | **no** | **no GitHub env, no PAT** |

## Copy flow (the new path)

```
POST /api/agents {template: "github:o/r[@b]", import_intent: "copy"}
  crud._resolve_template (PRE docker try-block → structured 4xx reach the UI)
    ├─ intent gates (see below)
    ├─ catalog check: fork_to_own:"required" template → 400 FORK_TO_OWN_REQUIRED
    ├─ resolve_github_pat ladder (ent#162) — used SERVER-SIDE only
    ├─ snapshot_import.stage_github_snapshot(repo, branch≠"main"?, pat)
    │    clone --depth 1 --single-branch [-b] via fork_to_own._run_git
    │    (disk-backed /data/agent-import-tmp staging; PAT via GIT_CONFIG_* env,
    │     never argv; output scrub_secret'd; 24h stale-sibling sweep)
    │    → rev-parse HEAD (provenance SHA) → strip .git
    │    → prune tree-ESCAPING symlinks (in-tree preserved, never followed)
    │    → 0 files → 400 COPY_SOURCE_EMPTY
    ├─ declared schedules read from the STAGED template.yaml (hardened loader,
    │    non-fatal) — not an API re-fetch, the snapshot is the truth
    └─ tr.github_repo_for_agent stays None  ⇒ no git row, no github env
  orchestrator (inside the docker try-block)
    ├─ deploy._prepopulate_workspace_from_template(name, staged_dir)
    │    (ephemeral-alpine put_archive: files + .trinity-initialized + chown;
    │     volume created BEFORE the container — startup.sh sees the marker and
    │     skips all clone logic on EVERY image generation: no base-image dep)
    ├─ staging dir cleaned; handles.copy_volume_name armed for rollback
    └─ container create with label trinity.import-intent=copy
  response: AgentStatus.import_snapshot {source_repo, source_branch, head_sha,
    file_count}; the create audit entry persists intent + snapshot provenance
    (the only durable record — see residual below).
```

**Failure honesty**: unreadable/private-no-PAT → 400 `COPY_SOURCE_UNREADABLE`
(ent#123 combined form — no enumeration oracle); transient → 502
`COPY_CLONE_FAILED`; all pre-side-effect. A mid-try failure rolls back via
`_cleanup_copy_artifacts` **after** the container reclaim (mount released), so
the pre-populated volume is removed and the #1667 leftover-volume guard cannot
409 the retry.

**Resource bounds (#2040 review)**: the clone streams caller-chosen bytes onto
`/data` (the bind mount that also holds `trinity.db`), so the path is bounded
three ways: a **free-space preflight** refuses to start a clone without
`3 × cap` free (`507 COPY_STAGING_NO_SPACE` — structured, never a disk-full
500); a **post-clone size cap** (`AGENT_IMPORT_MAX_BYTES`, default 1 GiB,
measured via `lstat` so a symlink never counts its target) answers a named
`400 COPY_SOURCE_TOO_LARGE`; and the shared volume pre-population primitive
now **spools the tar to disk** beside the template tree instead of
materializing it as one in-memory `BytesIO` (a large repo could otherwise OOM
the backend process, which serves every other agent — the primitive was
written for operator-bounded #950 deploy-local input). `CLONE_TIMEOUT_S` (120s)
plus the preflight bounds the clone itself.

## Intent gates (`crud._resolve_template`, pre-side-effect)

| Condition | Result |
|---|---|
| intent + non-`github:` template | 400 `INTENT_REQUIRES_GITHUB_TEMPLATE` |
| `fork` without `fork_to_own` block | 400 `FORK_PARAMS_REQUIRED` |
| `copy`/`clone` + `fork_to_own` block | 400 `INTENT_FORK_BLOCK_CONFLICT` (presence-triggered fork would silently create a GitHub repo) |
| `copy` on a `fork_to_own: required` catalog template | 400 `FORK_TO_OWN_REQUIRED` |
| `copy` + `ephemeral` | 400 `COPY_EPHEMERAL_UNSUPPORTED` (ghosts are volume-less, ent#69; the snapshot lives on the workspace volume) |

Copy deliberately does **not** run `_gate_tokenless_request`'s source-mode 400
(that guard protects boot-time push; copy never pushes — tokenless public copy
is legal for any `source_mode`).

## Why copy agents are safe across the lifecycle (verified seams)

- **Recreate/rebuild**: no `agent_git_config` row ⇒
  `lifecycle._apply_git_env_from_db`'s no-row branch pops the whole
  `_GIT_ENV_KEYS` block on BOTH recreate paths — sync can never be re-armed.
  The workspace volume + `.trinity-initialized` carry the files.
- **PAT drift**: no env PAT + no per-agent PAT row ⇒
  `check_github_pat_env_matches` is stably True (no recreate churn); the
  `crud` PAT-persist branch is skipped by construction
  (`tr.github_repo_for_agent is None`).
- **Sync surfaces**: `sync_health_service` selects via
  `db.list_git_enabled_agents()` (rows) — copy agents are never polled; git
  routers refuse as for `local:` agents.
- **Own-it-later**: `bind-to-own-repo` refuses `BIND_NO_GIT_CONFIG` by design;
  the documented path is **Initialize GitHub Sync** (`POST .../git/initialize`,
  built for git-less workspaces; env converges from the new row on the next
  recreate).
- **Documented residual (concurrency)**: two same-name creates racing inside a
  seconds window can interleave so the loser's volume cleanup removes the
  winner's pre-populated volume (review F3) — a narrow instance of the
  pre-existing check-then-act creation class (`_check_name_availability` runs
  pre-try, `_register_agent` late); mitigated by the UI in-flight guard and
  the idempotency in-flight 409, not eliminated. A per-name creation lock is
  the future fix if it ever bites.
- **Documented residual**: container + volume BOTH lost ⇒ rebuild yields an
  empty workspace with green health. Deliberate — re-cloning at rebuild would
  fetch CURRENT upstream (the #1809 divergent-rebuild class), wrong for a
  snapshot. Mitigations: #1169 data export; the audit entry's repo+SHA names
  what was lost; the advisory compat check goes red on the empty workspace.
  Schema-column provenance + exact-SHA re-fetch is a filed follow-up.

## Inline compatibility check (reuses #668 wholesale)

The Create Agent modal's post-create step (`ImportValidationStep.vue`) polls
`GET /api/agents/{name}/info` until a REAL agent-server response arrives —
discriminated as **200 without a `message` key**, because the backend proxy
fail-opens to 200 with a fallback body carrying `message` while the container
is still mid-clone (`routers/agent_files.py` catch-all), so a bare 200 is NOT
readiness (Docker "running" would race the clone for fork/clone intents and
false HARD failures would PERSIST via the results upsert; old agent images
without the endpoint keep the fallback shape and honestly reach the timeout
state). It then calls
`GET /api/agents/{name}/compatibility` (STATIC only; AI stays on-demand in
Overview). Honest branches: unavailable-when-stopped; "agent failed to start"
on a non-running container after bounded retries (never an infinite spinner).
Non-blocking — the agent exists regardless. MCP's `create_agent` response
points at `get_agent_compatibility_report`.

## Idempotency (Invariant #18)

`POST /api/agents` accepts `Idempotency-Key`; scope `agent_create:{user_id}`
(folds the principal — the 2026-07-20 learning: another user's identical key
must never replay a foreign create response). Replay → original response +
`X-Idempotent-Replay: true`; in-flight duplicate → 409 `CREATE_IN_FLIGHT`
(named distinctly from name-taken). Header-only server-side; MCP
`create_agent` derives a deterministic key from call args (name included, so
two agents from one repo never collide).

**Replay is liveness-gated (#2040 review F3)**: a completed replay is only
truthful while the agent it reports still exists. Because the MCP key is
deterministic over name+config, delete-then-identical-recreate inside the 24h
TTL would otherwise replay a 200 naming an agent that is gone — and create
nothing. The endpoint re-checks `db.is_agent_live(recorded_name)` before
replaying; a dead recorded agent **discards** the stored row
(`idempotency_service.discard_stale_replay` → the new
`db.idempotency_discard_completed`, which deletes ONLY completed rows) and
falls through to a genuinely fresh create — which then answers honestly:
name-reserved 409 for a soft-deleted agent, a real create for a hard-purged
one. Losing the re-claim race to a concurrent retry yields 409
`CREATE_IN_FLIGHT`.

## Surfaces

- **UI**: `CreateAgentModal.vue` — 3-way intent radio on the free-form GitHub
  path (default clone; fork reveals the existing destination/PAT/visibility
  fieldset; copy names the own-it-later path) + `ImportValidationStep.vue`.
- **MCP**: `create_agent` gains `import_intent: "copy" | "clone"` only — fork
  stays UI-only (tool args are audit-logged; a PAT arg would persist in
  plaintext). Fork-scope failures (`FORK_PAT_INVALID`,
  `FORK_REPO_CREATE_FAILED`) name the copy/clone alternatives — never silent
  auto-degrade.
- **Backend**: `services/agent_service/snapshot_import.py` (new),
  `crud.py` (gates + copy branch + rollback), `models.py`
  (`AgentConfig.import_intent`, `AgentStatus.import_snapshot`),
  `routers/agents.py` (idempotency + audit provenance),
  `fork_to_own.py` (degrade-message extension).

## Tests

`tests/unit/test_ent15_import_intents.py` — snapshot staging (branch args,
`.git` strip, symlink policy, empty guard, error mapping, cleanup), intent
gates (incl. `source_mode` None/False/True parametrization for the tokenless
bypass), copy-branch wiring (no row, no env, no reservation), idempotency
two-principal scope isolation + replay/in-flight.
