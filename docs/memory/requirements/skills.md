# Requirements — Skills & Playbooks

> Part of Trinity's requirements set. Index & write-path rule: [requirements.md](../requirements.md).

---

## 21. Skills Management (GitHub-Based)

> **Simplified Design**: GitHub repository as the single source of truth for skills.
> No custom version control, no Docker volumes, no complex infrastructure.
> Spec: `docs/requirements/SKILLS_MANAGEMENT.md`
> Flows: `docs/memory/feature-flows/skills-library-sync.md`, `skill-assignment.md`, `skill-injection.md`, `skills-on-agent-start.md`
> **Strategy**: skills are being unified into one model / one lifecycle (distribute → place → expose) — strategy epic trinity-enterprise#182. Phase 1 (trinity-enterprise#183: full-directory skill packages + frontmatter contract, §21.4/§21.6) shipped 2026-07-19; Phase 2 is placement/skill-runner (trinity-enterprise#139), Phase 3 exposure (trinity-enterprise#178).

### 21.1 GitHub Skills Library (Multi-Source)
- **Status**: ✅ Implemented (trinity-enterprise#237 — multi-source; OSS-core by decision, see below)
- **Description**: Platform syncs skills from **one or more** GitHub repositories: a bundled public community catalog plus any number of admin-added custom repos
- **Key Features**:
  - Sources live in the `skill_sources` table (id, name, url, ref, ref_type, is_default, enabled, priority, per-source sync status) — **replaces** the single `skills_library_url` system setting
  - Per-source clone at `/data/skills-library/<source_id>/`; `SkillSourceClone` owns one checkout's git lifecycle, `skill_service` orchestrates N of them
  - Manage via `GET/POST/PUT/DELETE /api/skills/sources` + `POST /api/skills/sources/{id}/sync`; URLs locked to github.com (SSRF guard, SEC-179) validated **on write**
  - **An edit must reach disk.** Repointing `url` re-clones (the checkout's `origin` is compared against the configured remote, credentials stripped so a PAT-bearing `origin` is not a false repoint; discard-and-re-clone rather than `remote set-url`, which would leave the old repo's tag refs behind); changing `url`/`ref`/`ref_type` clears the sync bookkeeping so a tag **bump** is not refused as a tag **move**; deleting a source reclaims its checkout, with a fail-closed orphan sweep on full syncs as the backstop. Non-identity edits (name/enabled/priority) keep the recorded SHA — clearing it on disable would let a tag moved during the disabled window be adopted silently on re-enable
  - **Both sync routes are human-only** (`reject_agent_principal` on top of `require_admin`): a sync clones executable material and, when the commit moves with auto-reinject on, spawns the fleet-wide re-inject. Grant-vs-use is a claim about *effect*, and fleet-wide executable delivery is not "use"
  - One source failing never blinds the others: per-source outcomes reported individually, aggregate succeeds if any source synced. A source whose first clone failed has no directory, and every git read tolerates that (a raising read would 500 the whole merged listing over one unreachable repo)
  - Uses existing GitHub PAT for private repos
  - **Durable sync status** (ent#236): the outcome of the last sweep is persisted to `system_settings` (`skills_library_last_sync`, `..._last_status`, `..._last_error`, `..._last_commit`) on **both** success and failure — previously in-process only, so under `--workers 2` the worker answering `/status` was usually not the worker that synced. These stay **library-wide** under multi-source (the Settings header summarises the whole sweep); per-source truth lives on each `skill_sources` row and is what the source list renders
  - **One lock covers the whole sweep** (ent#236's `SETNX`, kept library-wide by ent#237): per-source locking would let two workers interleave and each publish a merged listing built from a half-updated set of checkouts. A contended sync returns `busy`, never a failure, so a concurrent click cannot overwrite the panel with "Last sync failed" when nothing failed
  - **`commit_changed` is computed per source and OR'd** — ent#236 fires the fleet-wide re-inject only when the library actually moved, and with several sources ANY source moving changes what agents receive. Each source compares against its own **durable** `last_commit_sha`, not an in-memory field, so a fresh process cannot read "changed" and sweep the whole fleet on every backend restart
  - **Gating: all OSS-core** (AC#7, decided by vybe 2026-07-30). One seam per area — distribution is free, *execution* is paid via the `skill_runner` entitlement; a second paywall on distribution would double-monetize the same feature area
- **Per-source skills root** (ent#332) — the source layout is no longer a fixed `.claude/skills/` convention; see §21.1.4
- **Not Built**: per-source automation schedules: auto-sync (§21.7, ent#236) is one library-wide timer over every enabled source, not a cadence per source

#### 21.1.1 Name Resolution — Custom-Wins Precedence (ent#237 AC#4)
- **Rule**: two sources may ship the same skill name. Resolution is `priority` ASC then `created_at` ASC; custom sources default to 100, the bundled community source to 1000 — so **custom wins**, and a source added later needs no reordering
- **Names stay bare** (`pdf-export`, never `community/pdf-export`). The agent-side identity IS the directory `.claude/skills/<name>/`, and both the ent#139 skill-runner and the ent#178 A2A card resolve by bare name — prefixing would change every agent's `/skill-name` invocation string and require a fleet-wide re-inject
- **Never a silent overwrite**: the winning entry carries `shadowed_by` (the lower-precedence sources shipping that name, which are unreachable). Surfaced in `GET /api/skills/library`, on the source status, and as a `shadowed_source:<name>` warning at **inject** time — so "I assigned Community's copy and got Acme's" cannot happen quietly. Shadowed copies are deliberately NOT separate list entries: they are unreachable, and offering them would imply a choice the flat namespace cannot honour
- `agent_skills.source_id` records which source an assignment resolved from (recorded, not keyed — the UNIQUE stays `(agent_name, skill_name)`, since two sources' copies cannot coexist on disk). NULL = assigned pre-ent#237 or the source row is gone; resolves by precedence like any other bare name
- Deleting a source does **not** cascade to assignments: the skill keeps resolving through whatever source still provides it, and cascading would silently strip capabilities that are still available

#### 21.1.2 Supply-Chain Posture — Tag Pinning (ent#237 AC#5)
- **Why**: skills carry executable `scripts/` (§21.4) that the ent#139 runner executes, and ent#236 automates sync + fleet re-inject. A source tracking a branch head therefore puts every merged upstream commit on every install **with no human in the loop** — and the community catalog accepts PRs from strangers
- **Rule**: the bundled community source pins to a **tag we bump** (`ref_type='tag'`); custom sources, whose write access the operator controls, track a branch
- **A pinned tag must not move.** Git does not enforce tag immutability, so a tag resolving to a different commit than the last sync is **refused** (`moved_tag`) rather than adopted. Moving to new content = pointing the source at a new tag NAME, an explicit admin action
- **The refusal covers both materialization paths**, which is the whole of the control. On the **update** path (a checkout exists): the fetch omits `--force`, so git refuses to clobber a moved tag ref, plus an explicit comparison against the recorded SHA — the tag resolved **peeled** (`refs/tags/<ref>^{commit}`), because the recorded SHA is the commit and a bare rev-parse of an **annotated** tag yields the tag object instead, so an unmoved tag read as moved on every sync after the first (#2550 — every `trinity-skills` release tag is annotated, so the bundled source went red on its second sync; the clone path was unaffected because it resolves `HEAD`). On the **clone** path (no checkout): neither of those applies — there is no local tag ref to conflict and `_update_tag` is never reached — so `_refuse_moved_pin_after_clone` re-checks the resolved HEAD against the recorded SHA and **deletes the checkout** on refusal, because `list_skills` and injection read the working tree and a failed sync that left it behind would still serve the moved tag's content. Enforcing only the update path leaves the pin bypassed in exactly the case that matters: the checkout was lost (the non-repo quarantine rename, a restored `/data` backup, a recreated volume) while upstream moved the tag — and since sync would then report success with a changed commit, ent#236's fleet re-inject would push the payload to every running agent unattended. Caught by `/cso` on the ent#237 branch and closed before merge; a first-ever sync has no recorded SHA and is untouched
- **Revocation**: cut a new tag without the offending skill. The community repo + its review bar, CI contract validation, and release-tag process are trinity-enterprise#296
- **Gate on effect, not on verb.** Every mutating `/api/skills/sources` route carries `reject_agent_principal` **in addition to** `require_admin`, because `require_admin` answers "what role", never "is this a human" — an agent-scoped MCP key resolves to its owner *carrying the owner's role* (ent#293). Registering a source is the obvious case (it decides which repo the fleet executes code from). The *grant-vs-use* framing then mis-classified two routes and both had to be corrected: the LIST read, gated because "read" says nothing about the private repo URLs it returns; and **both sync routes**, originally role-gated as "use" until the effect was stated plainly — a sync clones executable material and, when the commit moves with auto-reinject on, spawns `run_fleet_reinject`, pushing skill `scripts/` to every running agent. The rule that survived: gate on what the route *does*

#### 21.1.3 Migration & Fresh Installs
- **Existing single-repo installs migrate losslessly** (AC#6): a configured `skills_library_url` is adopted as a regular **custom** source (custom, not default — precedence must keep preferring the repo the operator actually chose). The DB row is written before the clone moves, so a crash between them leaves a source that simply re-clones; the reverse order would strand a checkout no row points at. Idempotent and fail-soft
- **Fresh installs are pre-configured** (AC#3): the bundled community source is seeded as a **row** during the #1638 fresh-install window (`users` empty, before `_ensure_admin_user`), both dialects. A row, not a read-time code default — a default would resurrect a source the admin deleted and would silently hand an existing install a source it never configured. `TRINITY_DEFAULT_SKILL_SOURCE=""` disables the seed. Never raises: `init_database` runs at import, so a raising seed would crash-loop boot

#### 21.1.4 Vendor-Neutral Layout — Per-Source Skills Root (ent#332)
- **Why**: the SKILL.md format is harness-portable — only the *discovery path* is Claude-specific. The public community catalog (`abilityai/trinity-skills`, ent#296) is legible on its GitHub front page and consumable by non-Claude runtimes: top-level `skills/<name>/` plus a root `catalog.yaml`. The platform must consume that layout without breaking any existing `.claude/skills/` source
- **Resolution order, per source** (`SkillSourceClone.skills_rel_root()`, lazily cached per instance — instances are per-operation, so no staleness across syncs): (1) a root `catalog.yaml` `skills_root:` declaration; (2) a `skills/` directory carrying ≥1 real `<dir>/SKILL.md` (evidence-gated probe); (3) the legacy `.claude/skills/` fallback — existing sources keep working with zero config
- **Every invalid tier falls through to the next** (never blanks a source a lower tier can still read): an unusable catalog (unparseable, alias-bearing, non-mapping, unknown `schema_version` — tolerating int `1` and string `"1"` —, oversized >64 KB, symlinked, invalid `skills_root` value) warns and degrades to the probe; a symlinked or clone-escaping declared root falls to the probe; a symlinked `skills/` is lstat-refused by the probe. Only the FINAL tier escaping containment yields an empty listing (None/empty-propagated — never a raise through `list_skills`)
- **Dual-layout guard**: SKILL.md evidence under BOTH `skills/` and `.claude/skills/` with no catalog to decide keeps `.claude/skills/` and flags `layout_conflict` in status — a pre-existing dual-layout source must never silently flip *which executable content* the ent#236 auto-sync injects fleet-wide; switching requires the explicit declaration
- **`skills_root` validation is segment-wise**: strip one trailing `/`, split on `/`, reject any segment that is empty, `.`, or `..`, per-segment charset `[A-Za-z0-9._-]`, no leading `/` or `-`, ≤200 chars — a whole-string charset regex admits `.`, `./skills` and `skills//x`, each of which breaks archive-prefix math into per-skill "empty package" failures. The value is author-controlled repo content (ent#314 hardened loader, alias-REJECT, bounded `read(cap+1)`), reaches git argv only behind `--` separators, and is realpath-contained inside the clone
- **The agent-side destination is fixed (today)**: packages always land at `~/.claude/skills/<name>/`. `filter_skill_archive(source_root=…)` is the ONE point where source layout becomes destination — it rewrites tar arcnames from `<root>/<name>/…` to `.claude/skills/<name>/…` (identity for the legacy layout), so manifests, prune confinement, chmod/finalize paths, restore accounting, the legacy-fallback lookup and the whole ent#236 removal machinery stay destination-canonical with **zero migration**. This rewrite point is also the future seam if a non-Claude runtime ever needs a different in-agent discovery path — treat it as the plug-in location, not the destination as an eternal invariant
- **Versions are layout-independent**: the per-skill version is the skill *directory's* git tree SHA, so a repo restructure with unchanged content keeps versions stable — no spurious fleet re-inject
- **Schema commitment**: `skills_root` v1 means a **single flat root, one level deep**. Nesting / repo-root skills (`skills_root: "."` is rejected) arrive only via a `schema_version` bump, which current platforms refuse (degrading to the probe) rather than misread. Platform parses ONLY `schema_version` + `skills_root`; `categories`/`providers` are catalog metadata for UIs (#235)
- **Status surface**: each source's entry in `GET /api/skills/library/status` carries the resolved `skills_root` (null until cloned) and `layout_conflict`

### 21.2 Skill Types (by Convention)
- **Status**: ⏳ Not Started
- **Description**: Three skill types via naming convention (`policy-*`, `procedure-*`, no prefix)
- **Note**: direction superseded — the skill-package frontmatter contract (trinity-enterprise#183) carries automation level / invocability explicitly instead of name-prefix conventions

### 21.3 Agent Skill Assignment
- **Status**: ✅ Implemented
- **Description**: Assign specific skills to individual agents
- **Key Features**:
  - Database stores assignments only (`agent_skills` table)
  - Bulk save via PUT `/api/agents/{name}/skills`; single assign/unassign via POST/DELETE `/api/agents/{name}/skills/{skill}` (owner-only)
- **Note**: assignment surfaces — the Agent Detail **Skills tab** (visible since trinity-enterprise#235 / PR #1877, 2026-07-29; §22.2) plus REST/MCP. The Library page's skills section is browse-only, never a second assignment path (§22.3)

### 21.4 Skill Injection (Full Directory Packages)
- **Status**: ✅ Implemented (trinity-enterprise#183, 2026-07-19; OSS-core by decision)
- **Description**: Inject each assigned skill as its **entire directory package** (`SKILL.md` + `scripts/` + resources) into the agent's `~/.claude/skills/`
- **Key Features**:
  - Tar sourced from `git archive HEAD` of the library clone (atomic vs concurrent re-sync); members vetted backend-side (regular files only — symlinks never ship; VCS litter and protected basenames like `.env`/`CLAUDE.md` dropped with named warnings); delivered via the existing agent-server restore primitive (`POST /api/agent-server/restore`, #384) — no new endpoint
  - Size caps: per-skill `SKILL_MAX_BYTES` (10 MiB) and per-injection `SKILLS_TOTAL_MAX_BYTES` (50 MiB), env-tunable; over-cap → named per-skill error, injection continues
  - **Versioning/idempotency**: per-skill version = git tree SHA, recorded in a generated `.claude/skills/<name>/.trinity-skill.json` (version, commit, file manifest, injected_at; appended LAST to the tar). Agent start (`force=False`) skips version-unchanged skills; manual sync (`POST /api/agents/{name}/skills/inject` / MCP `sync_agent_skills`, `force=True`) is an unconditional repair
  - **Manifest-based prune**: on re-inject, files the *previous injection* wrote that the new package no longer carries are deleted (previous-manifest − new-manifest, capped 200/skill) — runtime artifacts and agent-authored files are structurally untouched; a same-named unmanaged dir (no meta) is overwritten but never pruned
  - **Honest per-skill results**: `{success, status: injected|unchanged|fallback|failed, files_written, error?, warnings[]}` with named warning codes (`missing_binary:*`, `missing_env:*`, `packages_not_checked`, `symlink_skipped:*`, `restore_skipped:*`, `stale_delete_failed:*`, `multi_file_dropped_old_image`, `frontmatter_invalid`, …); never silent partial success
  - **Declaration-only dep check (v1)**: declared binaries probed via `shutil.which`, env keys by NAME against process env ∪ `.env` key names (values never read) — one injected-python exec, fail-open; runs for `unchanged` skills too. Dep *provisioning* belongs to placement (trinity-enterprise#139)
  - Exec bits restored post-injection from git member modes (injected-python chmod)
  - **Git-sync guard**: injected skill dirs are appended per-name to the agent's own `.gitignore` and untracked (`git rm --cached`) so the 15-min auto-sync never commits platform-injected packages into agent repos (#1595/#1596 bloat class); agent-authored Playbooks keep committing
  - CLAUDE.md "Platform Skills" section rebuilt from **all** assigned skills present on the agent (unchanged included) with missing-dep annotations
  - Old agent image (no restore endpoint) → graceful fallback to single-file `SKILL.md` write + `multi_file_dropped_old_image` warning
  - Concurrency: per-agent Redis lock `skill_inject:{name}` (SETNX+TTL, fail-open; manual inject → 409, start path → skip)
- **Fleet re-inject + removal-on-unassign**: trinity-enterprise#236 — see §21.7

### 21.6 Skill Package Contract (Frontmatter)
- **Status**: ✅ Implemented (trinity-enterprise#183, 2026-07-19)
- **Description**: SKILL.md frontmatter is parsed platform-side into a skill **contract** surfaced via `GET /api/skills/library`(+`/{name}`, `/status`) and the MCP skill tools
- **Fields** (flat keys — the existing catalog convention; a `trinity:` mapping takes precedence when present): `description`, `automation` (surfaced, not enforced — feeds abilityai/trinity#518 later), `user_invocable` (default true), `allowed-tools` (verbatim), `requires: {packages, binaries, env}` — plus derived package metadata (`multi_file`, `file_count`, `size_bytes`, `version` = git tree SHA)
- **Hardening**: 64 KiB frontmatter cap; alias-refusing SafeLoader (billion-laughs guard, #919 convention); every field isinstance-guarded (unknown keys/garbage tolerated — skills are authored for Claude Code first); dep names regex-gated before any in-container probe (`binaries: ^[A-Za-z0-9._+-]+$`, `env: ^[A-Z][A-Z0-9_]*$`) so library-derived strings never reach a shell
- **Parse failure**: named `frontmatter_invalid` warning + first-paragraph description fallback — never a 500, never a dropped skill

### 21.5 MCP Tools (Simplified)
- **Status**: ✅ Implemented
- **Description**: Library + assignment tools in `src/mcp-server/src/tools/skills.ts`
- **Tools**: `list_skills`, `get_skill`, `get_skills_library_status`, `assign_skill_to_agent`, `set_agent_skills`, `sync_agent_skills`, `get_agent_skills`
- **Contract surface (ent#183)**: `list_skills`/`get_skill` carry the §21.6 contract fields (`automation`, `user_invocable`, `requires`, `multi_file`, `file_count`, `size_bytes`, `version`); `sync_agent_skills` surfaces per-skill warnings even on success (honest results)
- **Provenance surface (ent#237)**: `list_skills` carries each skill's `source` (name only — never a URL, since the tool is reachable by agent-scoped keys) and `shadowed_by`; `get_skills_library_status` carries the per-source array. Source *management* is deliberately REST-only, not an MCP tool — it is the grant action of §21.1.2
- **Removed from original design**: create/update/delete (use GitHub), execute_procedure (use scheduling)

### 21.7 Library Lifecycle Automation
- **Status**: ✅ Implemented (trinity-enterprise#236; OSS-core by decision, consistent with §21.4)
- **Description**: The automation layer over the ent#183 primitives — a library
  update reaches the fleet, and an unassignment actually removes the package.
  All three behaviors default **OFF/no-op**, so zero-config behavior is
  byte-identical to before.

**Scheduled auto-sync**
- Backend background service `services/skills_sync_service.py` (the
  `sync_health_service` shape), cross-worker **leader-locked** on
  `skills:sync:leader` (fail-open, mirrors `monitoring:leader` #1464) so
  `--workers 2` doesn't double-clone
- Backend-hosted, not the standalone scheduler: the sweep must reach agent
  containers and the scheduler sits on `trinity-platform` only (Network Topology)
- Admin-configurable via `GET/PUT /api/settings/skills-library` (dedicated
  range-validated route — the generic `PUT /{key}` is unvalidated `Dict[str,str]`,
  the #1644 trap; the three keys are blocklisted there). Interval default 3600s,
  floor 300s, ceiling 86400s. Enabled flag default **false**
- Config re-read every cycle → an interval/flag change applies without a restart
- Failures never silent: persisted status + last-error surface in the Settings panel (§21.1)

**Fleet-wide re-inject**
- Opt-in `skills_library_auto_reinject_enabled` (default false), applies to
  **both** the scheduled sync and the manual "Sync Library" button
- Fires only when the library **commit SHA actually changed** — a no-op pull must
  not sweep the fleet
- Scope: **running** agents (stopped agents self-heal on next start via the
  existing `force=False` start path); ephemeral ghosts excluded (§69 fleet-hygiene
  precedent); `trinity-system` included
- `force=False`, so the ent#183 tree-SHA skip makes unchanged skills free
- Bounded concurrency `SKILLS_FLEET_INJECT_CONCURRENCY` (default 5); per-agent
  `skill_inject:{name}` lock respected — contention is **skip-and-report**, never a wait
- Honest aggregate report persisted to `skills_fleet_reinject_last_run` and rendered
  in Settings; an operator-queue alarm fires **only** when ≥1 agent failed, hosted on
  the reserved sentinel `_skills-sync` (uncreatable — `sanitize_agent_name` strips the
  leading `_`, the #1644 pattern)

**Removal-on-unassign**
- Unassigning removes the injected package using the **existing manifest prune
  primitive**: only paths a previous injection wrote are deleted, plus the generated
  `.trinity-skill.json`. Agent-authored files and runtime artifacts are structurally
  untouched; directories the removal empties are reaped (`os.rmdir`, #1842 climb), a
  directory holding anything survives
- Triggered by **both** the single `DELETE /skills/{name}` **and** a bulk
  `PUT /skills` that drops names (the primary UI/MCP path — `set_agent_skills`)
- The DB unassign is authoritative and always succeeds; removal is attempted inline
  and reported in the response body. A stopped agent, a busy inject lock, or a
  transport failure degrades to a named `removal_deferred:*` — never a failed unassign
- The skill's `.gitignore` line is stripped on removal (else the file accumulates a
  line per skill ever injected)
- **Truncation**: a manifest longer than `PRUNE_CAP_PER_SKILL` (200) removes the
  first 200 paths, emits `removal_truncated`, and **keeps** the meta so the package
  stays platform-managed rather than becoming unmanaged orphan files

**Start-path reconciliation** (how a stopped agent learns about an unassign)
- The assignment row is gone by the time the agent starts, so removal is
  **reconciled, not replayed**: `reconcile_agent_skills` enumerates the agent's
  `~/.claude/skills/*/.trinity-skill.json` and removes every platform-managed skill
  not in the current assigned set. No tombstone table, no migration, and it
  self-heals every removal route (single DELETE, bulk-PUT shrink, direct DB edit)
- Runs on the start path **after** injection, and **also when zero skills are
  assigned** — that is exactly the "unassigned the last skill" case
- **Blast-radius guard**: a reconcile proposing more than
  `SKILLS_RECONCILE_MAX_REMOVALS` (default 10) removals for one agent **refuses
  wholesale**, logs ERROR, and raises an operator-queue alarm. A wiped/reset
  `agent_skills` table is indistinguishable from a legitimate mass-unassign, and the
  fail-safe direction is to keep files (#1638/#1644 discipline: the guard counts, it
  never samples row content)

**Audit**: scheduled sync, fleet re-inject, and every removal write `audit_log`
entries (`actor_type=system` for the automated paths).

---

## 22. Playbooks Tab (Agent Local Skills)

> **Design**: Browse and invoke agent's local skills directly from UI.
> Spec: `docs/requirements/PLAYBOOKS_TAB.md`
> Flow: `docs/memory/feature-flows/playbooks-tab.md`

### 22.1 Playbooks Tab
- **Status**: ✅ Implemented (2026-02-27)
- **Requirement ID**: PLAYBOOK-001
- **Description**: UI tab to view and invoke agent's local `.claude/skills/` directory
- **Key Features**:
  - Grid display of skills parsed from SKILL.md YAML frontmatter
  - One-click run (sends `/{skill-name}` to `/task` endpoint)
  - Run with instructions (prefills Tasks tab input)
  - Search/filter by name or description
  - Automation badge (autonomous/gated/manual)
- **Agent Endpoint**: `GET /api/skills` - Lists skills from `.claude/skills/`
- **Backend Proxy**: `GET /api/agents/{name}/playbooks`
- **Frontend**: `PlaybooksPanel.vue` component

### 22.2 Skills Tab (Platform Library)
- **Status**: ✅ Implemented (visible — unhidden & rebuilt, trinity-enterprise#235 / PR #1877, 2026-07-29)
- **Description**: Per-agent skill assignment from the platform library, on Agent Detail
- **Key Features**: assigned-skills list with honest per-skill injection status (§21.4 results incl. warnings), assign/unassign against the library list, manual re-inject; agent-scoped store `stores/skills.js` (its `emptyReason` discriminator is agent-scoped — do not reuse it on fleet surfaces)
- **History**: originally shipped hidden from `visibleTabs`; ent#235 rebuilt it and made it visible

### 22.3 Library Page — Fleet Skills Browse (trinity-enterprise#263)
- **Status**: ✅ Implemented (2026-07-31)
- **Description**: The Library page (`/library`, core-agent.md §4.5) carries a fleet-level **browse** section over the shared skills library — read-only discovery, NOT a second assignment path (strategy epic trinity-enterprise#182: one skill model, no parallel mechanisms). Assignment stays on each agent's Skills tab (§22.2); cards link there via the agents list.
- **Key Features**:
  - Reads ONLY the existing surfaces: `GET /api/skills/library/status` + `GET /api/skills/library` (list fetched only when `configured` — the no-swallow rule: a fetch error renders as an error with retry, never a confident wrong "empty"); zero new endpoints
  - Own Pinia store `stores/skillsLibrary.js`, deliberately separate from the agent-scoped `stores/skills.js`: `App.vue` KeepAlives AgentDetail, so `SkillsPanel`'s unmount-clear never fires on nav-away — shared refs would render the Library page's state (including fetch errors) inside the cached per-agent Skills tab
  - Fleet-scoped 4-state empty discriminator: `unconfigured` (admin → Settings CTA; non-admin → "ask your admin"), `not_cloned` (configured, never synced → Sync CTA), `empty` (cloned, zero skills → "add a skill directory, then Sync"), plus error-carried-separately
  - Sync-state header leads with disk-derived truth (`commit_sha` short + `skill_count` + branch); `last_sync` rendered only when truthy (it is per-worker in-memory state and reads null on the other uvicorn worker / after restart); admin-only **Sync now** with a 180s client timeout and ECONNABORTED → status re-fetch (client timeout ≠ server failure on a long first clone)
  - Repo URL shown admin-only, **userinfo-stripped** (the clone path accepts and stores `https://user:token@host/...` verbatim), labeled "Primary source", and hidden when `status.sources` reports >1 source (ent#237 / PR #1901 forward-compat); dormant `source_name`/`shadowed_by` render slots light up when #1901 lands
  - Per-skill cards render the §21.6 contract via the shared chips seam `components/skills/{SkillContractChips.vue, contract.js}` (extracted from `SkillsPanel.vue` so both surfaces render package facts from ONE seam); interpolation only — no `v-html`, no `:href` bound to library-derived strings (skills come from a synced repo — semi-trusted)
- **Not Built**: per-source display (post-#1901)

### 22.4 Library Page — Tabs + Fleet Assignment Read (trinity-enterprise#384)
- **Status**: ✅ Implemented (2026-08-12; **OSS-core by decision** — see architecture.md)
- **Description**: Two changes to §22.3's page, taken together as one deliberate restructure. (1) The three stacked sections become **tabs** — Agent Templates · Systems · Skills — reversing ent#263's stacked-sections choice for *all* sections rather than special-casing Skills. (2) Each skill block lists **which agents already hold it**, closing §22.3's named "Not Built" gap. Assignment itself stays a **write on §22.2's per-agent Skills tab**; the Library gained a read, not a second write path (ent#182: one skill model). The assign/unassign *writes* on this page were scoped out to ent#386 during planning; delivery semantics remain ent#385.
- **Key Features**:
  - **One batched read**, `GET /api/skills/assignments` → `{assignments: {skill: [{name, display_label}]}, scope}` — the aggregate §22.3 named. Never N per-card fetches (the ent#260 List-view rule: N+1 mount loops are deleted, not migrated). `db.get_all_skill_assignments()` is SQLAlchemy Core (the module has been 8/8 Core since #300), INNER JOINs `agent_ownership` on `deleted_at IS NULL` so a soft-deleted agent's rows — preserved up to 180 days by #834 — never render as a current holder on the unfiltered admin path (the ent#335 class), and **excludes ephemeral ghosts**: a ghost is hard-discarded at budget, so its chip would link to a 404 within minutes and a fan-out burst would inflate every count (heartbeat + fleet health exclude ghosts by the same reasoning)
  - **Access boundary**: admin unfiltered; everyone else owned ∪ shared. Unscoped, this is a fleet-wide agent-name enumeration oracle for any `role=user` — the Invariant #8 disclosure class already called out for `GET /api/subscriptions`. The accessible set is derived from **`db.get_all_agent_metadata()` (pure DB), NOT `accessible_agent_names`**: that helper resolves through `list_all_agents_fast()`, which returns `[]` whenever the Docker client is unavailable, so a denied socket or a daemon restart would report *"no agent holds any skill"* to every non-admin behind nothing but a throttled WARNING — a confident wrong zero arriving through the access helper, where a store-level degrade rule structurally cannot catch it. Access semantics are identical; only the source changes (the DB set additionally includes the caller's own container-less agents, which #1747 documents as routine, and never someone else's)
  - **`reject_agent_principal`** on top of `get_current_user`: an agent-scoped key resolves to its owner *carrying the owner's role*, so on a default admin-owned install it would receive the unfiltered fleet capability map. There is no agent consumer (deliberately no MCP tool), which makes the gate free and matches this router's own `/skills/library/{name}` (ent#139) and `/skills/sources` (ent#293) gates. Ghost + connector keys are already blocked by their allowlist fences
  - **`response_model`** (`SkillAssignmentsResponse`, `models.py` per Invariant #14) carrying `name` + `display_label` only — the ent#334 rule, from this same router: the model is a security boundary, so a future sensitive column on `agent_ownership` is fail-closed. `display_label` widens nothing (`GET /api/agents` already serves it for exactly this set)
  - **`scope: "all" | "accessible"`** so an empty result is worded honestly rather than asserting a global zero: under `all` (admin) the card reads "no agents yet", under `accessible` it reads "none of your agents" — true whether the caller has zero agents or zero assignments among them, so the endpoint needs to send no count of the accessible set. Without it, a `role=user` who cannot see a skill's forty holders would be told it has none
  - **Orphaned assignments surfaced**: rows whose skill is no longer in the library are rendered in their own bounded list. ent#237's revocation model is "cut a new tag without the offending skill", after which the operator's first question is *who still has it* — and a page keyed by the library listing answers "nothing" forever
  - **Tabs**: `OverflowTabs` (the design-system contract's mandated primitive — `Operations.vue`'s hand-rolled strip is not the precedent), `?tab=` URL state, a `route.query.tab` watch so the render follows the URL for deep links, external links and history entries (tab CLICKS use `router.replace`, so switching pushes no history entry and Back leaves the page rather than walking back through tabs), legacy `#agent-templates`/`#systems`/`#skills` anchors migrated to the matching tab then cleared, `?tab=` winning over a hash. The Systems tab stays creator-gated with **both** arms of a late-role watch (`stores/auth.js` reports `user` until `/api/users/me` lands, so a creator hard-loading `?tab=systems` would otherwise land on Templates and never recover)
  - **Lazy-mount-once panels** (`v-if` visited + `v-show` active): a plain `v-if` would refetch status+library+assignments on every tab switch (there is no store-owned poll to tear down, so the #1109 rationale does not apply here), and a plain `v-show` would mount Skills and Systems for someone who only wants Templates. Also preserves `SystemInstallPanel`'s editor state across switches
  - Loading uses the `ScanlineReveal` standard as **one persistent instance** with branching inside its slot (sibling `v-if` branches re-init from `loading=false` and never play), and `loading` means *no data yet* — the store splits `fetching` from `loading` so a revisit does not replay the beam over rendered data

---
