# CLAUDE.md

This file provides guidance to Claude Code when working with this repository.

**Repository**: https://github.com/abilityai/trinity (PUBLIC)

> **AI agent orienting in this repo?** [AGENTS.md](AGENTS.md) is the authoritative agent entry point — a task router with key facts, exact commands, and a "done when" check per task. This file is the **contributor working agreement** (Rules of Engagement, SDLC, architectural invariants), auto-loaded by Claude Code when you work on the codebase. Read it when your task is "contribute code"; start at `AGENTS.md` for deploy / operate / evaluate.

---

## Current Product Focus

**Primary theme**: Reliability (`theme-reliability`) · **Secondary theme**: UI/UX (`theme-ui-ux`)

When picking tickets, prefer items carrying the focus `theme-*` label. Theme focus is a **tiebreaker filter**, not a sort key — among equivalent-priority work, `theme-reliability` is picked first, then `theme-ui-ux`, then everything else. Items in other themes are not deprioritized — they're picked once focus-theme work at that priority is exhausted.

---

## ⚠️ PUBLIC OPEN SOURCE REPOSITORY

**This is a PUBLIC open-source repository visible to the entire world.**

### What NEVER to Commit
- ❌ API keys, tokens, PATs, or any credentials (even in comments or docs)
- ❌ Internal company URLs, production domains, IP addresses
- ❌ Real user emails, personal information, or PII
- ❌ Database dumps, backups, or data exports
- ❌ `.env` files or deployment configs with real values
- ❌ Auth0 client secrets, OAuth credentials, or service account keys
- ❌ Private repository references or internal tooling details
- ❌ Customer names, company-specific configurations, or business data
- ❌ **Enterprise/paid-feature designs** — the catalog of which capabilities are gated behind the paid tier, their private schema (`enterprise_*` tables), per-module implementation, or the open-core monetization/gating rationale. Public docs describe the **generic open-core seam only** (the entitlement/registry mechanism and how OSS code conditionally registers private modules). Specific enterprise module designs live ONLY in the private `trinity-enterprise` repo (`docs/memory/ENTERPRISE_DOCS.md` there). See the standing rule below; a CI grep-guard (`.github/workflows/enterprise-docs-guard.yml`) flags regressions. (trinity-enterprise#45)

### Open Source Best Practices
✅ **Use placeholders**: `your-domain.com`, `your-api-key`, `user@example.com`
✅ **Example files**: Commit `.example` templates (e.g., `.env.example`)
✅ **Environment variables**: Reference `${VAR_NAME}` instead of hardcoded values
✅ **Local examples**: Use `localhost` or `127.0.0.1` in documentation
✅ **Review diffs**: Always check `git diff` before committing to catch accidental secrets
✅ **Public-first mindset**: Assume every commit will be visible forever and indexed by search engines

### Git Safety Checklist
Before every commit:
1. Run `git diff` and review all changes line by line
2. Search for patterns: API keys (often start with `sk-`, `pk-`, `ghp_`), emails (`@`), IPs (`192.168.`, `10.0.`)
3. Verify no `.env` or config files with real credentials are staged
4. Check that examples use placeholder values
5. Confirm commit message doesn't reference internal systems

### Standing Rule: Enterprise Docs Are Private (trinity-enterprise#45)

Enterprise **feature designs, paid-module schema, and the open-core gating/monetization strategy** live ONLY in the private `trinity-enterprise` repo. Public docs (`docs/`, this file) describe the **generic open-core seam only** — that an entitlement/registry mechanism exists and how OSS code conditionally registers private modules — with **no catalog of specific paid features, no `enterprise_*` table DDL, and no per-module implementation detail**.

- New enterprise design → write it in `trinity-enterprise/docs/`, not here.
- Touching the seam in public docs → describe the mechanism, never enumerate the modules behind it.
- `.github/workflows/enterprise-docs-guard.yml` greps live public docs for paid-feature/private-schema tokens and fails the build on a hit; keep historical point-in-time docs (`docs/archive/`, `docs/releases/`, `docs/security-reports/`) out of scope (covered by the separate git-history-scrub follow-up).

---

## Project Overview

**Trinity** is an **autonomous agent orchestration and infrastructure platform** — sovereign infrastructure for deploying, orchestrating, and governing fleets of autonomous AI agents on your own hardware.

Each agent runs as an isolated Docker container with standardized interfaces for credentials, tools, and MCP server integrations.

**Local**: http://localhost
**Backend API**: http://localhost:8000/docs

---

## Remote Agent

This repository has a remote counterpart running on Trinity (`trinity` agent) for autonomous development. Use `/trinity:sync` to synchronize local changes with the remote instance. The remote agent can run scheduled tasks, process backlog issues, and operate autonomously when needed.

---

## Development Skills (`.claude` submodule)

Skills, agents, and methodology guides live in the `.claude/` directory, which is a **git submodule** pointing to [abilityai/trinity-dev](https://github.com/Abilityai/trinity-dev) (private). This is where `/sprint`, `/cso`, `/autoplan`, `/implement`, `/review`, `/validate-pr`, etc. come from.

### One-time setup after cloning
```bash
git submodule update --init --recursive
git config submodule.recurse true  # auto-syncs .claude when switching branches
```

Without `submodule.recurse true`, switching branches will leave `.claude` stale and skills will disappear. The `fetchRecurseSubmodules = true` in `.gitmodules` handles `git pull` automatically, but branch switching requires the local config above.

---

## SDLC

All work follows a 4-stage lifecycle tracked via **GitHub Issues** (labels + open/closed state — no project board):

```
 Todo → In Progress → In Dev → Done
```

- **Todo**: Issue created, triaged with priority (P0-P3), type, and theme labels, acceptance criteria defined
- **In Progress**: Developer assigned, feature branch created (`feature/<issue>-<slug>`), `status-in-progress` label
- **In Dev**: PR squash-merged to `dev` — `status-in-dev` label, awaiting the next release cut (dev → main)
- **Done**: Release PR merged to `main`, issue auto-closed via `Closes #N`

**Two trackers (open-core).** Issues route by type: `type-bug`/`type-refactor`/`type-docs` → public `abilityai/trinity`; `type-feature`/`type-epic` → private `abilityai/trinity-enterprise`. Tracker ≠ code repo — core code still lands as a public-repo PR. Query/picking skills union both trackers.

**Full details**: `.claude/DEVELOPMENT_WORKFLOW.md` (→ Repository Routing)

---

## Rules of Engagement

### 1. Requirements-Driven Development
- Update `docs/memory/requirements.md` **BEFORE** implementing new features
- All features must trace back to documented requirements
- Never add features without requirements update first

### 2. Minimal Necessary Changes
- Only change what's required for the task
- No unsolicited refactoring or reorganization
- No cosmetic formatting changes to unrelated code
- No creating documentation files unless explicitly requested

### 3. Follow the Roadmap
- Check **GitHub Issues** for current priorities (`/roadmap` or `gh issue list`) — labels are the single source of truth. `/roadmap` unions both trackers (public bugs + private features/epics); a raw `gh issue list` sees only one repo — pass `--repo abilityai/trinity-enterprise` for feature/epic work
- Work P0 issues first, then P1 (`type-bug` before `type-feature`, then newest issue number first), then P2/P3
- Assign yourself and update `status-*` labels as you progress (see SDLC above)
- Close issues when complete

### 4. Tiered Documentation Updates
Documentation requirements scale with change type (change history is tracked via git commits):
- **Bug fix**: Descriptive commit message only
- **Feature / API change**: `architecture.md` or `feature-flows/` as needed
- **New capability**: `requirements.md` + `feature-flows/`

### 5. Security First (PUBLIC REPO)
- **This is a public repository** - assume all commits are visible worldwide
- Never expose credentials, API keys, or tokens in code or logs
- Never commit internal URLs, IP addresses, or email addresses
- Use environment variables for all secrets
- All credential operations logged via structured logging (values masked, captured by Vector)
- Use placeholder values in example configs (e.g., `your-domain.com`, `your-api-key`)
- Review diffs before committing for accidental sensitive data

### 6. Development Skills
Follow methodology guides in `.claude/skills/`:

| Skill | Key Rule |
|-------|----------|
| `verification` | No "done" claims without evidence (run command, show output) |
| `systematic-debugging` | Find root cause BEFORE attempting fixes |
| `tdd` | Write failing test first, then minimal code to pass |
| `code-review` | Verify feedback technically before implementing |

### 7. Architectural Invariants
Before adding endpoints, services, DB tables, or frontend views, review the Architectural Invariants section in @docs/memory/architecture.md. Violations of these patterns will break the system. Run `/validate-architecture` weekly to catch drift. For decisions about new capabilities or significant design choices, also consult `docs/planning/TARGET_ARCHITECTURE.md` — prefer changes that move toward the target, reject changes that move away from it.

### 8. Agent-Defined Pipelines (Trinity ≠ DAG engine)
Long-running multi-stage work inside agents (perception → synthesis → publish → measure, etc.) is **owned by the agent**, not by Trinity. The agent runs a heartbeat skill that advances stages, retries failures, and escalates via the operator queue. Trinity's only contribution is a standardized **read surface** — agents publish `~/.trinity/pipelines/<id>.yaml` (definition) and `~/.trinity/pipeline-state/<id>/<instance>.json` (state); Trinity exposes these via thin MCP tools (`list_agent_pipelines`, `get_agent_pipeline_state`) that wrap the existing `agent_files` router. **Do not** add a DAG executor, pipeline state tables, or backend transition logic — those belong in the agent. See requirements §34 and issue #919.

### 9. Dual-Track DB Migrations (SQLite + PostgreSQL) — #1183
Trinity supports **both** SQLite (default) and PostgreSQL, on **separate migration systems**. **Every schema change requires TWO migrations:**

1. **SQLite** → add a versioned entry to `src/backend/db/migrations.py` (bespoke runner: PRAGMA + `INSERT OR IGNORE`, tracked in `schema_migrations`).
2. **PostgreSQL** → add a new **Alembic** revision under `src/backend/migrations/versions/` (`init_database()`'s non-SQLite branch runs `db/alembic_runner.upgrade_to_head()`).

Also update the table DDL in `src/backend/db/schema.py` / `db/tables.py` so fresh builds stay correct. **Do not** drop or skip the SQLite track — SQLite stays supported (PostgreSQL-only is an eventual goal, not near-term). The single-source-of-truth consolidation (`tables.py` MetaData → autogenerated revisions, retiring `migrations.py`) is deferred to #746. Enterprise tables migrate through their **own** separate runner (`enterprise/backend/_migrations.py`, `enterprise_schema_migrations`) — see invariant #3 in architecture.md.

---

## Memory Files

| File | Purpose |
|------|---------|
| `docs/memory/requirements.md` | **SINGLE SOURCE OF TRUTH** - All features |
| @docs/memory/architecture.md | **Current system design** — describes what is built today (~1000 lines max) |
| `docs/planning/TARGET_ARCHITECTURE.md` | **Target system design + active orchestration direction** — pull / work-stealing coordination (Epic #1045, umbrella #1081). Use when evaluating tradeoffs and prioritizing work; consult before touching `task_execution_service`, `capacity_manager`, `slot_service`, `backlog_service`, `dispatch_breaker`, or `cleanup_service`. |
| `docs/memory/feature-flows.md` | Index of vertical slice docs |
| `docs/archive/plans/ORCHESTRATION_RELIABILITY_2026-04.md` | **Archived (historical)** — completed Sprint A–D′ execution-reliability plan (all shipped). Superseded 2026-06-05 by the pull-coordination direction in `TARGET_ARCHITECTURE.md`. Read for background on the slot/backlog/cleanup machinery. |
| GitHub Issues | Prioritized task queue — labels are authoritative: priority (P0-P3), type, `theme-*`, `complexity-*`; status via `status-*` labels + open/closed; epics are `type-epic` issues with native sub-issues. No project board. **Two trackers:** bugs/refactor/docs in public `abilityai/trinity`, features/epics in private `abilityai/trinity-enterprise` (see `.claude/DEVELOPMENT_WORKFLOW.md` → Repository Routing). |

---

## Development Commands

```bash
# Start all services
./scripts/deploy/start.sh

# Stop all services
./scripts/deploy/stop.sh

# Build base agent image
./scripts/deploy/build-base-image.sh

# Rebuild services
docker-compose build

# View logs
docker-compose logs -f backend
```

### Local URLs
- **Web UI**: http://localhost
- **Backend API**: http://localhost:8000/docs
- **MCP Server**: http://localhost:8080/mcp
- **Vector (logs)**: http://localhost:8686/health

---

## Project Structure

```
project_trinity/
├── src/
│   ├── backend/          # FastAPI backend (main.py, database.py)
│   ├── frontend/         # Vue.js 3 + Tailwind CSS
│   └── mcp-server/       # Trinity MCP server (62 tools)
├── docker/
│   ├── base-image/       # Universal agent base (agent-server.py)
│   ├── backend/          # Backend Dockerfile
│   └── frontend/         # Frontend Dockerfile
├── config/
│   ├── agent-templates/  # Pre-configured templates
│   └── vector.yaml       # Vector log aggregation config
├── .claude/
│   ├── memory/           # Persistent project memory
│   ├── commands/         # Slash commands
│   └── agents/           # Sub-agents
└── docs/                 # Additional documentation
```

---

## Key Files

| Category | File | Description |
|----------|------|-------------|
| Backend | `src/backend/main.py` | FastAPI app, 300+ endpoints across 40+ routers |
| Backend | `src/backend/database.py` | SQLite persistence |
| Backend | `src/backend/routers/credentials.py` | Credential injection (CRED-002) |
| Frontend | `src/frontend/src/views/AgentDetail.vue` | Agent detail page |
| Frontend | `src/frontend/src/stores/agents.js` | Agent state management |
| Agent | `docker/base-image/agent-server.py` | Agent internal server |

---

## Important Notes for Claude Code

1. **Credential security**: Never log credentials. Credential values are masked in all logs.

2. **Docker socket access**: Backend has read-only Docker socket access. Be cautious with Docker API calls.

3. **Port conflicts**: Agents use incrementing SSH ports (2222+). Check for conflicts.

4. **Data persistence**: SQLite at `~/trinity-data/trinity.db` (bind mount). Redis for secrets (Docker volume). Run `scripts/deploy/backup-database.sh` before major changes.

5. **Logging via Vector**: All container logs are captured by Vector and written to JSON files. Query logs with `jq` or grep.

6. **Frontend dev mode**: Vite with hot reload. Changes to `.vue` files reflect immediately.

7. **Base image rebuilds**: After modifying `docker/base-image/Dockerfile`, run `./scripts/deploy/build-base-image.sh`.

8. **Re-login after restart**: When the backend restarts, users need to re-login (JWT tokens are invalidated).

9. **MCP reconnection**: After backend restart, MCP clients (Claude Code, etc.) need to be manually reconnected (run `/mcp` or restart the client).

10. **Keep working directory clean**: Delete temporary files (screenshots, test outputs, cache directories) after use. Never leave PNG files, test artifacts, or debug outputs in the project root.

---

## Authentication

- **Email Login**: Primary method - users enter email, receive 6-digit code, login
- **Admin Login**: Password-based login for admin user (username fixed as 'admin')
- **Email Whitelist**: Manage allowed emails in Settings → Email Whitelist

### API Authentication Pattern

All authenticated API calls require a JWT Bearer token. To get one:

```bash
# 1. Login (form-encoded, NOT JSON)
curl -s -X POST http://localhost:8000/api/token \
  -d 'username=admin&password=${ADMIN_PASSWORD}'
# Returns: {"access_token": "eyJ...", "token_type": "bearer"}

# 2. Use token in Authorization header
curl -s -H "Authorization: Bearer <token>" http://localhost:8000/api/agents
```

**Key facts:**
- Login endpoint: `POST /api/token` (OAuth2 form-encoded: `username=...&password=...`)
- Admin password: Set via `ADMIN_PASSWORD` env var in `.env` (see `CLAUDE.local.md` for actual value)
- Token lifetime: 7 days, invalidated on backend restart
- MCP API keys (`trinity_mcp_*`) also work as Bearer tokens
- Unauthenticated endpoints: `/api/auth/mode`, `/api/setup/status`, `/api/token`

---

## Quick Reference

### Creating an Agent
```bash
# Via API
curl -X POST http://localhost:8000/api/agents \
  -H "Content-Type: application/json" \
  -d '{"name": "my-agent", "template": "github:Org/repo"}'

# Via UI
# Visit http://localhost → Create Agent
```

### Agent Container Labels
- `trinity.platform=agent` - Identifies Trinity agents
- `trinity.agent-name` - Agent name
- `trinity.agent-type` - Type (business-assistant, etc.)
- `trinity.template` - Template used

### Credential Pattern
```
.env                    # Source of truth (KEY=VALUE)
.mcp.json.template      # Template with ${VAR} placeholders
.mcp.json               # Generated at runtime
```

---

## Related Repositories

| Repository | Description |
|------------|-------------|
| [abilityai/trinity](https://github.com/abilityai/trinity) | This repository - Autonomous Agent Orchestration Platform |
| [abilityai/trinity-ops-public](https://github.com/abilityai/trinity-ops-public) | **Claude Code ops agent** — manage any Trinity instance (health, updates, logs, rollback, provisioning) |
| [abilityai/abilities](https://github.com/abilityai/abilities) | **Canonical agent development toolkit** — plugins for the full autonomous agent lifecycle (scaffolding, onboarding, deployment, scheduling, ops) |

### Abilities (agent development toolkit)

The **[abilities](https://github.com/abilityai/abilities)** repo is the canonical development workflow for building and managing autonomous agents with Claude Code. It provides 5 focused plugins covering the full agent lifecycle:

| Plugin | What it does |
|--------|-------------|
| **create-agent** | 12 wizards for agent scaffolding (create, prospector, chief-of-staff, webmaster, recon, receptionist, ghostwriter, kb-agent, website, custom, clone, adjust) |
| **agent-dev** | 15 skills: add skills, memory systems, git-sync hooks, GitHub backlog workflow, grooming, sprints, autonomous work loops |
| **trinity** | 5 skills: connect, onboard, deploy, sync, create-dashboard |
| **dev-methodology** | 24 skills: implementation, testing, security (CSO audit), PR validation, release, architecture/schema/config validation, feature flows, user doc generation |
| **utilities** | 7 skills: incident investigation, safe deployment, Docker ops, batch processing, conversation export, bug reports, ops knowledge sync |

**Installation:**
```bash
/plugin marketplace add abilityai/abilities
```

**Onboarding an agent to Trinity:**
```bash
/plugin install trinity@abilityai
/trinity:onboard
```

---

## See Also

- **SDLC & Development Workflow**: `.claude/DEVELOPMENT_WORKFLOW.md` ← Start here for dev process
- **Orchestration Reliability Plan (archived)**: `docs/archive/plans/ORCHESTRATION_RELIABILITY_2026-04.md` ← Sprint A–D′ historical record; superseded by `docs/planning/TARGET_ARCHITECTURE.md` (pull coordination) as the active execution-stack direction
- **Full Architecture**: @docs/memory/architecture.md
- **All Requirements**: `.claude/memory/requirements.md`
- **Current Roadmap**: https://github.com/abilityai/trinity/issues (public bugs) + private `abilityai/trinity-enterprise` (features/epics) — use `/roadmap` to see both
- **Recent Changes**: `git log --oneline --since="2 weeks ago"`
- **Agent Guide**: `docs/TRINITY_COMPATIBLE_AGENT_GUIDE.md`
- **Agent Network Demo**: `docs/AGENT_NETWORK_DEMO.md`
- **Agent Development Toolkit**: https://github.com/abilityai/abilities
- **Docs Q&A Bot**: `./scripts/ask-trinity.sh "your question"` or [public endpoint](https://us-central1-mcp-server-project-455215.cloudfunctions.net/ask-trinity)
