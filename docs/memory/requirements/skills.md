# Requirements — Skills & Playbooks

> Part of Trinity's requirements set. Index & write-path rule: [requirements.md](../requirements.md).

---

## 21. Skills Management (GitHub-Based)

> **Simplified Design**: GitHub repository as the single source of truth for skills.
> No custom version control, no Docker volumes, no complex infrastructure.
> Spec: `docs/requirements/SKILLS_MANAGEMENT.md`
> Flows: `docs/memory/feature-flows/skills-library-sync.md`, `skill-assignment.md`, `skill-injection.md`, `skills-on-agent-start.md`
> **Strategy**: skills are being unified into one model / one lifecycle (distribute → place → expose) — strategy epic trinity-enterprise#182. Phase 1 (trinity-enterprise#183: full-directory skill packages + frontmatter contract, §21.4/§21.6) shipped 2026-07-19; Phase 2 is placement/skill-runner (trinity-enterprise#139), Phase 3 exposure (trinity-enterprise#178).

### 21.1 GitHub Skills Library
- **Status**: ✅ Implemented
- **Description**: Platform syncs skills from a GitHub repository
- **Key Features**:
  - Configure library URL + branch in Settings (admin); URL locked to github.com (SSRF guard, SEC-179)
  - `git clone/pull` to local `/data/skills-library/` (`skill_service.py`)
  - On-demand sync: Settings "Sync Library" button / `POST /api/skills/library/sync` (admin-only)
  - Scheduled auto-sync (trinity-enterprise#236) — see §21.7
  - Uses existing GitHub PAT for private repos
  - **Durable sync status** (ent#236): outcome persisted to `system_settings`
    (`skills_library_last_sync`, `..._last_status`, `..._last_error`, `..._last_commit`)
    on **both** success and failure. Previously in-process only, so under
    `--workers 2` the worker answering `/status` was usually not the worker that
    synced — the panel showed a stale timestamp and could never show an error at all

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
- **Not Built**: fleet assignment read ("assigned to N agents" per skill — needs a `GET /api/skills/assignments` aggregate); per-source display (post-#1901)

---
