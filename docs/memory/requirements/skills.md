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
- **Status**: ✅ Implemented (partial — see Not Built)
- **Description**: Platform syncs skills from a GitHub repository
- **Key Features**:
  - Configure library URL + branch in Settings (admin); URL locked to github.com (SSRF guard, SEC-179)
  - `git clone/pull` to local `/data/skills-library/` (`skill_service.py`)
  - On-demand sync: Settings "Sync Library" button / `POST /api/skills/library/sync` (admin-only)
  - Uses existing GitHub PAT for private repos
- **Not Built**: scheduled auto-sync (hourly/daily) — library sync is manual/on-demand only; injection (not sync) runs on agent start

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
