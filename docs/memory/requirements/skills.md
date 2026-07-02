# Requirements — Skills & Playbooks

> Part of Trinity's requirements set. Index & write-path rule: [requirements.md](../requirements.md).

---

## 21. Skills Management (GitHub-Based)

> **Simplified Design**: GitHub repository as the single source of truth for skills.
> No custom version control, no Docker volumes, no complex infrastructure.
> Spec: `docs/requirements/SKILLS_MANAGEMENT.md`

### 21.1 GitHub Skills Library
- **Status**: ⏳ Not Started
- **Description**: Platform syncs skills from a GitHub repository
- **Key Features**:
  - Configure library URL in Settings (admin)
  - `git clone/pull` to local `/data/skills-library/`
  - Auto-sync options: on-demand, on agent start, hourly, daily
  - Uses existing GitHub PAT for private repos

### 21.2 Skill Types (by Convention)
- **Status**: ⏳ Not Started
- **Description**: Three skill types via naming convention
- **Types**:
  - `policy-*` — Always active rules (e.g., `policy-code-review`)
  - `procedure-*` — Step-by-step instructions (e.g., `procedure-incident-response`)
  - (no prefix) — Methodologies/guidance (e.g., `verification`, `tdd`)

### 21.3 Agent Skill Assignment
- **Status**: ⏳ Not Started
- **Description**: Assign specific skills to individual agents
- **Key Features**:
  - Checkbox list in Agent Detail → Skills tab
  - Database stores assignments only (`agent_skills` table)
  - Bulk save via PUT `/api/agents/{name}/skills`

### 21.4 Skill Injection (Copy-Based)
- **Status**: ⏳ Not Started
- **Description**: Copy assigned skills to agent's `~/.claude/skills/` directory
- **Key Features**:
  - Copy on agent start (not symlinks)
  - Manual "Inject to Agent" button for running agents
  - "Sync All Agents" admin action after library update

### 21.5 MCP Tools (Simplified)
- **Status**: ⏳ Not Started
- **Description**: 4 essential MCP tools (reduced from 10)
- **Tools**:
  - `list_skills` — List available skills from library
  - `get_skill` — Get skill content
  - `assign_skill_to_agent` — Assign skill
  - `sync_agent_skills` — Re-inject skills to running agent
- **Removed**: create/update/delete (use GitHub), execute_procedure (use scheduling)

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
