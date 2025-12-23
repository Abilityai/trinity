# CLAUDE.md

This file provides guidance to Claude Code when working with this repository.

**Repository**: https://github.com/abilityai/trinity (PUBLIC)

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

### Open Source Best Practices
✅ **Use placeholders**: `your-domain.com`, `your-api-key`, `user@example.com`
✅ **Example files**: Commit `.example` templates (e.g., `deploy.config.example`)
✅ **Environment variables**: Reference `${VAR_NAME}` instead of hardcoded values
✅ **Local examples**: Use `localhost:3000` or `127.0.0.1` in documentation
✅ **Review diffs**: Always check `git diff` before committing to catch accidental secrets
✅ **Public-first mindset**: Assume every commit will be visible forever and indexed by search engines

### Git Safety Checklist
Before every commit:
1. Run `git diff` and review all changes line by line
2. Search for patterns: API keys (often start with `sk-`, `pk-`, `ghp_`), emails (`@`), IPs (`192.168.`, `10.0.`)
3. Verify no `.env` or config files with real credentials are staged
4. Check that examples use placeholder values
5. Confirm commit message doesn't reference internal systems

---

## Project Overview

**Trinity** is a **Deep Agent Orchestration Platform** — sovereign infrastructure for deploying, orchestrating, and governing autonomous AI systems that plan, reason, and execute independently.

Unlike reactive chatbots ("System 1" AI), Deep Agents operate with deliberative reasoning ("System 2" AI): they decompose goals into task graphs, persist memory across sessions, delegate to specialized sub-agents, and recover from failures autonomously.

Trinity implements the **Four Pillars of Deep Agency**:
1. **Hierarchical Delegation** — Orchestrator-Worker pattern with context quarantine
2. **Persistent Memory** — Virtual filesystems, vector databases, episodic memory
3. **Extreme Context Engineering** — High-Order Prompts defining reasoning processes
4. **Autonomous Operations** — Scheduling, monitoring, and self-healing capabilities

Each agent runs as an isolated Docker container with standardized interfaces for credentials, tools, and MCP server integrations.

**Local**: http://localhost:3000
**Backend API**: http://localhost:8000/docs

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
- Check `.claude/memory/roadmap.md` for current priorities
- Work topmost incomplete items in the queue
- Mark items complete with timestamp: `[x] 2025-11-28 HH:MM:SS`

### 4. Mandatory Documentation Updates
After **EVERY** change, update:
- `changelog.md` - Add timestamped entry with emoji prefix
- `architecture.md` - If changing APIs, schema, or integrations
- `feature-flows/` - If modifying feature behavior
- `requirements.md` - If changing feature scope

### 5. Security First (PUBLIC REPO)
- **This is a public repository** - assume all commits are visible worldwide
- Never expose credentials, API keys, or tokens in code or logs
- Never commit internal URLs, IP addresses, or email addresses
- Use environment variables for all secrets
- All credential operations logged via audit logger (values masked)
- Use placeholder values in example configs (e.g., `your-domain.com`, `your-api-key`)
- Review diffs before committing for accidental sensitive data

---

## Memory Files

| File | Purpose |
|------|---------|
| `.claude/memory/requirements.md` | **SINGLE SOURCE OF TRUTH** - All features |
| `.claude/memory/architecture.md` | Current system design (~1000 lines max) |
| `.claude/memory/roadmap.md` | Prioritized task queue |
| `.claude/memory/changelog.md` | Timestamped history (~500 lines) |
| `.claude/memory/feature-flows.md` | Index of vertical slice docs |
| `.claude/memory/project_index.json` | Machine-readable project state |

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
- **Web UI**: http://localhost:3000
- **Backend API**: http://localhost:8000/docs
- **MCP Server**: http://localhost:8080/mcp
- **Audit Logger**: http://localhost:8001/docs

### Production Deployment (GCP)

Deployment uses a config-based approach. Settings are stored in `deploy.config` (gitignored).

```bash
# Set up deployment (one-time)
cp deploy.config.example deploy.config
# Edit deploy.config with your GCP project, zone, instance, domain

# Deploy to GCP
./scripts/deploy/gcp-deploy.sh

# Other commands
./scripts/deploy/gcp-firewall.sh      # Set up firewall rules
./scripts/deploy/backup-database.sh   # Backup database
./scripts/deploy/restore-database.sh  # Restore database
```

**Key deployment files:**
| File | Purpose |
|------|---------|
| `deploy.config.example` | Template (committed) |
| `deploy.config` | Your settings (gitignored, never commit) |
| `scripts/deploy/gcp-deploy.sh` | Main deployment script |

---

## Project Structure

```
project_trinity/
├── src/
│   ├── backend/          # FastAPI backend (main.py, database.py, credentials.py)
│   ├── frontend/         # Vue.js 3 + Tailwind CSS
│   ├── mcp-server/       # Trinity MCP server (12 tools)
│   └── audit-logger/     # Audit service
├── docker/
│   ├── base-image/       # Universal agent base (agent-server.py)
│   ├── backend/          # Backend Dockerfile
│   └── frontend/         # Frontend Dockerfile
├── config/
│   └── agent-templates/  # Pre-configured templates
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
| Backend | `src/backend/main.py` | FastAPI app, 35+ endpoints |
| Backend | `src/backend/database.py` | SQLite persistence |
| Backend | `src/backend/credentials.py` | Redis credential manager |
| Frontend | `src/frontend/src/views/AgentDetail.vue` | Agent detail page |
| Frontend | `src/frontend/src/stores/agents.js` | Agent state management |
| Agent | `docker/base-image/agent-server.py` | Agent internal server |

---

## Important Notes for Claude Code

1. **Don't break existing environments**: The `environments/` directory contains working production setups. Don't modify these.

2. **Credential security**: Never log credentials. Use audit logger for tracking access, not values.

3. **Docker socket access**: Backend has read-only Docker socket access. Be cautious with Docker API calls.

4. **Port conflicts**: Agents use incrementing SSH ports (2222+). Check for conflicts.

5. **Data persistence**: SQLite at `~/trinity-data/trinity.db` (bind mount). Redis for secrets (Docker volume). Run `scripts/deploy/backup-database.sh` before major changes.

6. **Audit logging is async**: Uses 2-second timeout. Operations continue if audit logger is down.

7. **Frontend dev mode**: Vite with hot reload. Changes to `.vue` files reflect immediately.

8. **Base image rebuilds**: After modifying `docker/base-image/Dockerfile`, run `./scripts/deploy/build-base-image.sh`.

9. **Re-login after restart**: When the app is redeployed or restarted, users need to re-login to see the changes (JWT tokens are invalidated).

10. **MCP reconnection**: After backend redeployment, MCP clients (Claude Code, etc.) need to be manually reconnected by the user (run `/mcp` or restart the client).

---

## Authentication

- **Production**: Auth0 + Google OAuth (configure allowed domains in Auth0)
- **Development**: Set `DEV_MODE_ENABLED=true` in backend environment (docker-compose.yml)
- **Mode Detection**: Runtime via `GET /api/auth/mode` - no frontend rebuild needed

---

## Quick Reference

### Creating an Agent
```bash
# Via API
curl -X POST http://localhost:8000/api/agents \
  -H "Content-Type: application/json" \
  -d '{"name": "my-agent", "template": "github:Org/repo"}'

# Via UI
# Visit http://localhost:3000 → Create Agent
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

## See Also

- **Development Workflow**: `docs/DEVELOPMENT_WORKFLOW.md` ← Start here for dev process
- **Full Architecture**: `.claude/memory/architecture.md`
- **All Requirements**: `.claude/memory/requirements.md`
- **Current Roadmap**: `.claude/memory/roadmap.md`
- **Recent Changes**: `.claude/memory/changelog.md`
- **Template Spec**: `docs/AGENT_TEMPLATE_SPEC.md`
- **Deployment Guide**: `docs/DEPLOYMENT.md`
- **Agent Network Demo**: `docs/AGENT_NETWORK_DEMO.md`
