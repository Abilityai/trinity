# Feature Flow — Bind an Agent to a Repo You Own (trinity-enterprise#109)

> **What it is**: point a **live** agent at a GitHub repository the user owns —
> creating it if needed — from the agent's **current workspace**, then re-bake
> the container so the rebind survives a restart.
>
> **Requirements**: `docs/memory/requirements/github.md` §11.12 · amends §11.11 FR-5
> **Related flows**: [github-repo-initialization.md](github-repo-initialization.md)
> (the create-time sibling) · [github-sync.md](github-sync.md) (the push paths whose
> `no_write_credentials` refusal now points here) · [mcp-git-tools.md](mcp-git-tools.md)
> **Epic**: ent#122 (fresh-install fleet provisioning). Supersedes ent#230.

---

## 1. Why this exists

ent#123 made it possible to create an agent from a **public** GitHub template with
no PAT anywhere. Those agents work — they clone, they run, they learn — but they
are pull-only: `startup.sh` blackholes their push remote, and every backend push
path refuses with `no_write_credentials`. The default Cornelius second-brain agent
is exactly this shape.

So the fresh-install flow the epic promised —

> fresh install → seeded public fleet runs (no PAT) → configure credentials →
> **own the keepers**

— had no last step. The only documented escape was the message the product itself
printed: *"create a new agent with fork-to-own and import your data."* That
discards the agent's identity, its container, its 180-day name reservation, and
its history, to keep a directory of files.

This flow is the missing step, done in place.

**It is a rebind, not a fork verb.** The operation is *"point this agent at a
GitHub repo you own."* An agent that already has a writable repo is therefore an
ordinary rebind — a typo'd destination, the wrong PAT account, an org migration,
or a retry after a partial failure — rather than a refusal. That framing is what
makes ent#109 AC #3 ("works for any agent") literally true, and it is also *less*
code than special-casing the already-writable state.

---

## 2. Surfaces

| Layer | Path |
|---|---|
| UI | `src/frontend/src/components/BindRepoPanel.vue`, mounted by `GitPanel.vue` |
| Store | `src/frontend/src/stores/agents.js` → `bindAgentToOwnRepo` / `getBindToOwnRepoStatus` |
| Router | `src/backend/routers/git.py` → `POST /{name}/git/bind-to-own-repo`, `GET .../status`, `_bind_locks` |
| Service | `src/backend/services/agent_service/repo_binding.py` (orchestration) |
| Shared | `src/backend/services/agent_service/fork_to_own.py` → `inspect_or_create_destination_repo`, `validate_destination_pat` |
| Container | `src/backend/services/git_service.py` → `rebind_origin_and_push`, `inspect_container_git` |
| DB | `src/backend/db/schedules/git_config.py` → `rebind_git_config` (CAS), `restore_git_config_binding` |
| Models | `src/backend/models.py` → `BindAgentRepoRequest` / `BindAgentRepoResponse` |
| Redis | `agent:bind_dest:{sha256}`, `agent:bind_op:{name}` (registered in `agent_runtime_state.EXEMPT_KEYSPACES`) |

**No MCP tool.** A binding needs the user's PAT in the request body; exposing it
as a tool would push a user credential through the MCP layer for an agent to
handle. Human-only by construction.

---

## 3. End-to-end flow

```
UI (GitPanel → BindRepoPanel)
  destination_repo · github_pat (password) · private (default true)
        │  POST /api/agents/{name}/git/bind-to-own-repo      [axios, 300s]
        ▼
routers/git.py                     ← thin HTTP mapper ONLY
  OwnedAgentByName (uniform 404)  +  reject_agent_principal (human-only)
  idempotency_service.begin(scope="agent:{name}", key="bind_to_own_repo:{hdr}")
  async with _bind_locks(agent, destination):        ← FAIL CLOSED (503)
        │      agent:bind_dest:{sha256(lower(dest))}   ← serializes the REAL race
        │      agent:bind_op:{name}                    ← double-submit guard
        ▼
services/agent_service/repo_binding.bind_agent_to_own_repo()
  1  _classify()                      read-only; every refusal leaves state intact
       no row              → 400 BIND_NO_GIT_CONFIG
       source_mode == 0    → 409 BIND_WORKING_BRANCH_MODE_UNSUPPORTED
       not running         → 400 BIND_AGENT_NOT_RUNNING
       origin ≠ row / none → 409 BIND_STATE_UNCLASSIFIED   (reports both values)
  2  destination already bound to another agent?  → 409 BIND_DESTINATION_IN_USE
  3  validate_destination_pat()  →  inspect_or_create_destination_repo()
       created | empty → push        branches → 409 BIND_DESTINATION_EXISTS
  4  ══ COMMIT POINT ═════════════════════════════════════════════════════
     db.rebind_git_config(new=dest, expected=old, source_branch=branch)
       rowcount 0 → 409 BIND_CONCURRENT_MODIFICATION   (nothing partial)
     belt: re-check destination →  lost?  restore previous values, 409
  5  git_service.rebind_origin_and_push()        push → set-url → read back
       → 502 BIND_PUSH_FAILED / BIND_REWIRE_FAILED   (partial: true)
  6  db.set_agent_github_pat()          ← LAST, and before the recreate
       → 502 BIND_PAT_PERSIST_FAILED    (recreate deliberately NOT attempted)
  7  recreate_container_with_updated_config()     re-bakes GITHUB_REPO from DB
       → 502 BIND_RECREATE_FAILED       (partial: true)
  8  audit GIT_OPERATION / bind_to_own_repo   ← on EVERY exit path (#905)
```

---

## 4. The five decisions that carry this design

### 4.1 Classification partitions on `source_mode`, not on credentials

The intuitive gate is *"does this agent already have write credentials?"* It is
the wrong column. `idx_git_config_repo_branch_unique` is
`UNIQUE(github_repo, working_branch) WHERE source_mode = 0`; the credential
predicate (`_agent_has_write_credentials`) is orthogonal to it. A
`source_mode = 0` row that happens to lack credentials passes a credentials gate,
routes into this engine, and gets rebound **inside** the unique index — without a
branch re-reservation. So the partition is on `source_mode` explicitly, and every
other shape is refused by name rather than mis-routed.

`source_mode` also stays at **1** after a rebind. It does not mean "read-only" —
ent#93's own fork-to-own agents are source-mode and auto-push. It means "track the
source branch rather than carving `trinity/<agent>/<id>`", which is exactly what a
user who now owns the repo wants: their captures land on their default branch.

### 4.2 The destination lock is the one that matters

The race this feature can create is **two different agents binding one destination
repo**. A per-agent lock never serializes that, and ent#93's post-write re-check
has a real TOCTOU window (if `z` inserts and re-checks before `a` inserts, both
survive the lexicographic-minimum test). So the primary lock is keyed on
`sha256(lower(destination))` — lower-cased first, because GitHub slugs are
case-insensitive and `Alice/Brain` must take the same lock as `alice/brain`.

Both locks **fail closed** with 503 + `Retry-After`. `_agent_data_op_lock`'s
fail-open is right for a tar round-trip, where a lost lock costs a duplicated
read. Here it would cost two repo creations, two CAS writes, and two concurrent
recreates of one container.

### 4.3 The CAS is the commit point, and the loser is restored — never deleted

`db.rebind_git_config` is a single statement whose `WHERE` names the value read
before any GitHub state existed:

```sql
UPDATE agent_git_config
   SET github_repo = :new, source_branch = :default, auto_sync_enabled = 1
 WHERE agent_name = :agent AND github_repo = :expected_old
```

rowcount 0 means the row moved under us → 409, and because this one statement is
the entire commit, **nothing is partially written**.

The post-commit belt's loser path is `restore_git_config_binding`, a compensating
UPDATE putting the captured previous values back. It is deliberately **not**
`delete_git_config`, which is what ent#93's create path does. That is sound there
only because the row was INSERTed microseconds earlier and the whole agent
creation aborts with it. Here the row **pre-exists a live agent**: deleting it
strips the binding, so the agent's next recreate finds no row, drops
`GITHUB_REPO`, and the agent comes back with no repository at all — the
silently-empty-agent class (#843/#1439).

Note the two repo comparisons in this flow answer **different questions** and are
therefore deliberately inconsistent about case:

| Comparison | Question | Case |
|---|---|---|
| container `origin` vs the row (classification) | "do these name the same repo?" | insensitive |
| CAS `expected_old` | "did the row change under me?" | **sensitive** — any write, even a re-casing, is a lost race |

### 4.4 The PAT is persisted last

Writing the credential at the commit point is what made the *documented retry
path unreachable* in the pre-review design: `_agent_has_write_credentials` reads
the per-agent PAT row, so an early write makes the agent look already-writable and
the retry returns a refusal. The same fact also breaks the "a mid-window manual
Push fails closed" claim — it would succeed, against the **old** repo, with the
**new** token.

The in-container push uses the request's PAT directly and never needs the
persisted row, so the credential is written only once the container genuinely owns
the new repo. A failure before that leaves the agent exactly as not-writable as it
was, and the retry is clean.

It is written **before** the recreate for a symmetric reason: the config-drift
recreate resolves the PAT with `pat_gate="per_agent_only"`
(`lifecycle._apply_git_env_from_db`), so with no row it would bake a repo-bound
container carrying no token — and `startup.sh`'s `configure_push_remote` would
then blackhole its push remote.

### 4.5 A recreate is mandatory, and it is not a re-provision

`startup.sh`'s "repository already exists" branch runs
`git remote set-url origin "${CLONE_URL}"` **unconditionally**, with `CLONE_URL`
built from the baked `GITHUB_REPO`. The workspace-`.env` fallback covers
`GITHUB_PAT` only — there is no `GITHUB_REPO` fallback. A DB-only rebind is
therefore silently reverted by the next plain restart, which is precisely the
silent origin mismatch AC #5 forbids.

`recreate_container_with_updated_config` re-bakes the git env from the DB via
`_apply_git_env_from_db` (PR 1). It reuses the same volumes through the
`volume_base_name` pin (#1664) and touches neither `agent_ownership` nor the
180-day name reservation, so **AC #7 holds and AC #2's S4 persistent-state
allowlist is satisfied by construction** — the workspace volume is never detached,
so there is nothing to snapshot and overlay back.

---

## 5. Error registry

| Code | Status | When | Partial? |
|---|---|---|---|
| `BIND_NO_GIT_CONFIG` | 400 | no `agent_git_config` row — also `local:` agents and `trinity-system` | no |
| `BIND_WORKING_BRANCH_MODE_UNSUPPORTED` | 409 | `source_mode = 0`; needs a branch re-reservation | no |
| `BIND_AGENT_NOT_RUNNING` | 400 | the engine needs `docker exec` on a live workspace | no |
| `BIND_STATE_UNCLASSIFIED` | 409 | no readable `.git`/`origin`, detached HEAD, or origin ≠ row **and the row does not already name this destination** (see Resumption) | no |
| `FORK_PAT_INVALID` | 400 | shared primitive | no |
| `FORK_DESTINATION_FORBIDDEN` | 400 | shared primitive — PAT login ≠ destination owner | no |
| `FORK_DESTINATION_UNREACHABLE` | 502 | shared primitive — GitHub unreadable while inspecting the destination | no |
| `FORK_REPO_CREATE_FAILED` | 400 | shared primitive | no |
| `FORK_REPO_NOT_VISIBLE` | 502 | shared primitive — retryable, reuses the repo | no |
| `BIND_DESTINATION_EXISTS` | 409 | destination holds branches **and this is not a resumption** (on a resume they are the agent's own pushed history) | no |
| `BIND_DESTINATION_IN_USE` | 409 | another agent's git config binds the destination | no |
| `BIND_CONCURRENT_MODIFICATION` | 409 | CAS rowcount 0 | no |
| `BIND_PUSH_FAILED` | 502 | push to the destination failed | **yes** |
| `BIND_REWIRE_FAILED` | 502 | origin set-url failed, or read-back disagreed | **yes** |
| `BIND_PAT_PERSIST_FAILED` | 502 | credential not stored; recreate deliberately skipped | **yes** |
| `BIND_RECREATE_FAILED` | 502 | env not re-baked | **yes** |
| `BIND_OP_IN_PROGRESS` | 503 | lock contention or lock layer unavailable (+ `Retry-After`) | no |
| `BIND_DESTINATION_UNREACHABLE` | 502 | the destination-binding pre-check could not read the DB, or the shared primitive raised a non-structured error. **Fail-closed** — an unreadable guard must not open the gate | no |
| `BIND_UNEXPECTED_ERROR` | 500 | router catch-all; audited, and the idempotency claim released so a retry can proceed | unknown |

`partial: true` is surfaced in the response body and rendered distinctly in the UI
(warning, "Partly applied — action needed"). A post-commit failure means the DB
binding **is** saved; reporting it as a flat failure would send the operator
looking in the wrong place.

**Failure honesty.** Every partial message names what is saved and what is not,
and says retrying is safe. It does **not** claim self-healing.
`BIND_RECREATE_FAILED` additionally warns *against* a plain container restart,
which re-runs `startup.sh` and would undo the rebind.

**Resumption is an explicit branch, not a property of idempotence.** The first
draft assumed the retry converged "by construction" because the destination is
reused and the push and rewire are idempotent. It did not: after the CAS the row
names the destination while the container's `origin` still names the old repo,
and *both* pre-flight gates read that skew as a refusal — `_classify`
(origin ≠ row → `BIND_STATE_UNCLASSIFIED`) and the destination policy (the
branches now in the destination are the agent's own just-pushed history →
`BIND_DESTINATION_EXISTS`). All four post-commit messages told the user to do
the one thing that returned 409.

So the row naming **this** destination is treated as a resumption and relaxes
both gates:

* `_classify` lets `origin` lag. Safe because `origin` never selects what gets
  pushed — step 4 pushes `refs/heads/<branch>` from the workspace by *explicit
  URL* and writes `origin` afterwards — and it cannot be tightened anyway: a
  committed CAS has overwritten the old repo name, so "still the old repo" and
  "something else" are indistinguishable from the row, and treating the
  ambiguity as fatal strands the agent with no recovery at all.
* The destination policy accepts existing branches. Bounded by git, not by
  trust: the push carries no `--force` and no `+` refspec, so unrelated history
  is rejected non-fast-forward and an unrelated branch is untouched. The gate is
  a UX guard against tangling an agent into an occupied repo; integrity sits one
  layer down.
* `rebind_origin_and_push` receives `previous_repo=None` on a resume, so
  `upstream` is left alone rather than being repointed at the destination
  itself — which would erase the provenance the rebind exists to preserve.

A mismatch against any *other* repo is still `BIND_STATE_UNCLASSIFIED`.

**The container recreate clears name-keyed breaker state first.** Both circuit
breakers are keyed by agent name with no TTL, so the replacement container would
otherwise inherit its predecessor's verdict and come up fast-failed without ever
being contacted (#1560). `start_agent_internal` clears them immediately before
its own recreate call; this is that helper's **second** production call site and
carries the same `clear_agent_breakers`, before the recreate rather than after
(clearing afterwards would reset a breaker the fresh container had legitimately
tripped). Slots are deliberately untouched — `force_clear_slots` would drop
capacity accounting for an in-flight execution.

> **Why there is no drift predicate.** Decision #17 recommended adding
> `check_github_repo_env_matches` to `needs_recreation`'s eager predicates so a
> failed step 7 converged on the next start. It was cut. The only drift-proof
> implementation calls `_apply_git_env_from_db` against a throwaway dict — and
> PR 1's AST guard (`tests/unit/test_ent109_git_env_seam.py`) asserts that
> helper has **exactly two** callers, because "git env had two writers and one
> was wrong" is the bug PR 1 fixed. Re-implementing the derivation independently
> is the writer/matcher feedback loop `lifecycle.py` warns about (infinite
> recreate). The resumption branch above supplies the convergence instead — as an
> explicit, tested code path rather than as an assumed property — so the honest
> move was to drop the promise, not weaken the guard.

---

## 6. Sharing with ent#93 (AC #4)

`fork_to_own.py` gained two exported pieces:

* **`inspect_or_create_destination_repo()`** → `created | empty | branches`.
  It *reports*; it never decides. Both callers then apply their own policy:
  the create path treats "exactly the template tip" as an idempotent no-op;
  the rebind, having no template to compare against, refuses any existing branch.
* **`validate_destination_pat()`** — a **sibling**, not folded in, so each caller
  controls when the token is validated relative to its own preconditions. The
  create path validates before resolving the template tip, so "bad PAT +
  unreachable template" still reports `FORK_PAT_INVALID`.

This is **partial reuse, and the seam is the design decision**:
`fork_template_to_own_repo` copies a *GitHub template*; the rebind's content
source is the accumulated KB in the **workspace volume**. Cloning the template
would hand the user a pristine repo and silently drop everything the agent
learned. One machinery, one error registry, two content sources.

---

## 7. Security

* **A user PAT crosses the wire post-creation.** `SecretStr` in the model,
  unwrapped exactly once at the router boundary, never logged, and persisted
  AES-256-GCM (`agent_git_config.github_pat_encrypted`, Invariant #12).
* **Dual scrub, at the boundary.** Every message built from foreign text passes
  `scrub_secret(text, user_pat)` **and** `redact_url_userinfo`. The second is not
  redundant: git stderr embeds whatever userinfo is in the remote URL, which on a
  rebind is frequently a *stale baked* token that is **not** the request's PAT.
  `repo_binding._scrub` applies both where the PAT is in scope, rather than
  trusting the producer — the docker and GitHub exception paths arrive through
  libraries that never saw the token.
* **Owner-only AND human-only.** `OwnedAgentByName` gives the uniform 404
  (Invariant #8, no enumeration oracle); `reject_agent_principal` is the
  additional gate, because an agent-scoped key resolves to its owner *carrying the
  owner's role* — so on a default admin-owned install any agent's injected
  `TRINITY_MCP_API_KEY` satisfies a role gate (trinity-ops-agent#232;
  #1644/#1816 precedent). Blast radius is operator-scale: external GitHub state,
  a persisted credential, a container replacement.
* **In-container the PAT rides the remote URL** (`shlex.quote`d via
  `_remote_seturl_subcommand`) — the established, reviewed precedent shared with
  `update_remote_pat`, `initialize_git_in_container` and `startup.sh`, not a new
  boundary crossing. The UI states it plainly: *the agent can read its own git
  credential, so prefer the narrow token.*
* **Committed history only.** The push never runs `git add`. Nothing is lost —
  working-tree files live on the volume and the next Push (which now works)
  commits them — and staging blind would walk into the `git add .` credential
  hazard (`.claude/.credentials.json` is not matched by `_GITIGNORE_PATTERNS`'
  `credentials.json` entry) that must be solved before `local:` agents are
  supported.
* **Audit** `GIT_OPERATION` / `bind_to_own_repo` on **every** exit path (#905),
  carrying `github_repo`, `previous_repo`, `branch`, `private`, `created_repo`,
  `reused_existing`, `recreated` — and, on failures, the code and whether it was
  partial.

---

## 8. Tests

| Module | Covers |
|---|---|
| `tests/unit/test_ent109_repo_binding.py` | classification incl. the already-writable rebind and the `trinity-system` refusal; the CAS against a **real SQLite engine** (two racers, one winner); the restore-not-delete loser path; PAT-last ordering by recorded call order; fail-at-push → retry → success; secret hygiene incl. a stale baked token |
| `tests/unit/test_ent109_fork_to_own_extraction.py` | the primitive's three states + error registry; the seam property (reports `branches`, does not refuse); create-path validate-before-template ordering |
| `tests/unit/test_ent109_no_write_credentials_message.py` | both `no_write_credentials` surfaces point here; source guard over `git.ts` with an **asserted** anchor |
| `tests/unit/test_fork_to_own.py` (pre-existing, unchanged) | the real behaviour-preservation baseline for the extraction — 40 tests |
| `tests/unit/test_1560_agent_redis_key_parity.py` | both bind keyspaces registered |

---

## 9. Known limits

* **`local:` template agents are not supported yet** (400 `BIND_NO_GIT_CONFIG`).
  On a fresh install that population is three bundled demo agents plus
  `trinity-system`. When picked up, prefer extending `initialize_github_sync` over
  a second engine; two traps are recorded in requirements §11.12 (pass
  `working_branch=None` for source mode; fix `_GITIGNORE_PATTERNS` before its
  `git add .` runs).
* **`GIT_UPSTREAM_REPO` is not re-baked for a rebound agent.** The `upstream`
  remote is written in-container and lives on the persisted volume, so it survives
  every recreate and restart — but it is not *self-healed* if manually removed,
  and not restored after total volume loss (at which point the agent re-clones
  from the user's own repo and nothing else is lost). Pre-existing ent#93 gap.
* **Working-tree changes are not committed** by the bind (see §7).
* **No status polling.** `GET .../status` is a pull, not a progress stream; a
  client that times out reloads rather than watches.

---

| Date | Change |
|---|---|
| 2026-08-02 | Initial flow — post-creation repo binding (ent#109), superseding ent#230 |
