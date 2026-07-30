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
  - One source failing never blinds the others: per-source outcomes reported individually, aggregate succeeds if any source synced. A source whose first clone failed has no directory, and every git read tolerates that (a raising read would 500 the whole merged listing over one unreachable repo)
  - Uses existing GitHub PAT for private repos
  - **Gating: all OSS-core** (AC#7, decided by vybe 2026-07-30). One seam per area — distribution is free, *execution* is paid via the `skill_runner` entitlement; a second paywall on distribution would double-monetize the same feature area
- **Not Built**: scheduled auto-sync (hourly/daily) — sync is manual/on-demand only (trinity-enterprise#236); injection (not sync) runs on agent start. Per-source `skills_path` — the `.claude/skills/` layout is a fixed convention for every source, so a repo shaped differently syncs to zero skills

#### 21.1.1 Name Resolution — Custom-Wins Precedence (ent#237 AC#4)
- **Rule**: two sources may ship the same skill name. Resolution is `priority` ASC then `created_at` ASC; custom sources default to 100, the bundled community source to 1000 — so **custom wins**, and a source added later needs no reordering
- **Names stay bare** (`pdf-export`, never `community/pdf-export`). The agent-side identity IS the directory `.claude/skills/<name>/`, and both the ent#139 skill-runner and the ent#178 A2A card resolve by bare name — prefixing would change every agent's `/skill-name` invocation string and require a fleet-wide re-inject
- **Never a silent overwrite**: the winning entry carries `shadowed_by` (the lower-precedence sources shipping that name, which are unreachable). Surfaced in `GET /api/skills/library`, on the source status, and as a `shadowed_source:<name>` warning at **inject** time — so "I assigned Community's copy and got Acme's" cannot happen quietly. Shadowed copies are deliberately NOT separate list entries: they are unreachable, and offering them would imply a choice the flat namespace cannot honour
- `agent_skills.source_id` records which source an assignment resolved from (recorded, not keyed — the UNIQUE stays `(agent_name, skill_name)`, since two sources' copies cannot coexist on disk). NULL = assigned pre-ent#237 or the source row is gone; resolves by precedence like any other bare name
- Deleting a source does **not** cascade to assignments: the skill keeps resolving through whatever source still provides it, and cascading would silently strip capabilities that are still available

#### 21.1.2 Supply-Chain Posture — Tag Pinning (ent#237 AC#5)
- **Why**: skills carry executable `scripts/` (§21.4) that the ent#139 runner executes, and ent#236 automates sync + fleet re-inject. A source tracking a branch head therefore puts every merged upstream commit on every install **with no human in the loop** — and the community catalog accepts PRs from strangers
- **Rule**: the bundled community source pins to a **tag we bump** (`ref_type='tag'`); custom sources, whose write access the operator controls, track a branch
- **A pinned tag must not move.** Git does not enforce tag immutability, so a tag resolving to a different commit than the last sync is **refused** (`moved_tag`) rather than adopted. Two independent mechanisms: the fetch omits `--force` (git refuses to clobber a moved tag ref) and an explicit SHA comparison catches the same condition on a fresh clone with no local ref to conflict. Moving to new content = pointing the source at a new tag NAME, an explicit admin action
- **Revocation**: cut a new tag without the offending skill. The community repo + its review bar, CI contract validation, and release-tag process are trinity-enterprise#296
- **The grant/use boundary**: registering a source decides which repo the fleet executes code from, so every mutating `/api/skills/sources` route carries `reject_agent_principal` **in addition to** `require_admin`. `require_admin` answers "what role", never "is this a human" — an agent-scoped MCP key resolves to its owner *carrying the owner's role* (ent#293). Reading and syncing an already-configured source is *use* and stays role-gated only

#### 21.1.3 Migration & Fresh Installs
- **Existing single-repo installs migrate losslessly** (AC#6): a configured `skills_library_url` is adopted as a regular **custom** source (custom, not default — precedence must keep preferring the repo the operator actually chose). The DB row is written before the clone moves, so a crash between them leaves a source that simply re-clones; the reverse order would strand a checkout no row points at. Idempotent and fail-soft
- **Fresh installs are pre-configured** (AC#3): the bundled community source is seeded as a **row** during the #1638 fresh-install window (`users` empty, before `_ensure_admin_user`), both dialects. A row, not a read-time code default — a default would resurrect a source the admin deleted and would silently hand an existing install a source it never configured. `TRINITY_DEFAULT_SKILL_SOURCE=""` disables the seed. Never raises: `init_database` runs at import, so a raising seed would crash-loop boot

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
- **Note**: the Skills tab is hidden from Agent Detail (§22.2) — assignment is via REST/MCP only

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
- **Not Built**: "Sync All Agents" fleet-wide re-inject after a library update; removal-on-unassign (unassigned skills stay on the agent until manually removed)

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

### 22.2 Skills Tab (Platform Library) - Hidden
- **Status**: ✅ Implemented (hidden)
- **Description**: Platform-level skill library assignment (existing feature)
- **Change**: Tab hidden from visibleTabs but component preserved for potential admin-only access

---
