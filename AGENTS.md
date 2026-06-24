# Trinity — Guide for AI Agents

Trinity is an autonomous agent orchestration platform: every agent runs in its own Docker container with scheduling, observability, credential injection, channel integrations (Slack/Telegram/WhatsApp), and a tamper-evident audit trail — self-hosted on infrastructure the operator controls. Agents are plain Claude Code (or Gemini CLI) projects; Trinity is where they run in production.

> **Detailed documentation index: [docs/user-docs/README.md](docs/user-docs/README.md)** — guides, agent management, credentials, automation, operations, integrations, and the full API reference live there.

## Using this file

**This is the authoritative entry point for AI agents.** If you are an autonomous agent that just landed in this repository or connected to a Trinity instance, start here. The other root files serve different readers — [`README.md`](README.md) is for humans evaluating Trinity; [`CLAUDE.md`](CLAUDE.md) is the **contributor working agreement** (Rules of Engagement, SDLC, architectural invariants) that Claude Code auto-loads when you edit this codebase. Read `CLAUDE.md` only when your task is "contribute code"; for everything else, this file is the map.

**How to traverse it:** find your task in [Route by task](#route-by-task) → jump to that section → run the commands → stop at the **Done when** signal. Each task is *zero-to-value*: the shortest path from landing to a confirmed useful result, ending in one check you can run to prove you got there. If a step is ambiguous or a link is dead, that is a bug — report it on the [issue tracker](https://github.com/abilityai/trinity/issues).

## Route by task

| Your task | Go to | Done when |
|-----------|-------|-----------|
| Deploy yourself (or another agent) to a Trinity instance | [Deploy an agent](#deploy-an-agent-to-trinity) | `trinity agents list` (or `GET /api/agents`) shows the agent `running` |
| Stand up a new Trinity instance | [Stand up an instance](#stand-up-a-trinity-instance) | `curl /health` → `{"status": "healthy"}` |
| Operate an existing instance (chat, schedules, fleet ops) | [Operate over MCP](#operate-a-trinity-instance-over-mcp) | an MCP tool call (e.g. `list_agents`) returns results over your API key |
| Evaluate Trinity / summarize it for your operator | [README.md](README.md), then [docs/user-docs/README.md](docs/user-docs/README.md); system design: [docs/memory/architecture.md](docs/memory/architecture.md) | you can state what Trinity is, how to run it, and its license |
| Contribute to Trinity's codebase | [Work on this repository](#work-on-this-repository) — Claude Code also auto-loads [CLAUDE.md](CLAUDE.md) | tests pass and a PR is open against `dev` |

## Key facts

| | |
|---|---|
| Web UI | `http://localhost` (port 80) |
| Backend API | `http://localhost:8000` — OpenAPI at `/docs` |
| MCP server | `http://localhost:8080/mcp` (Streamable HTTP) |
| Health check | `GET http://localhost:8000/health` → `{"status": "healthy", ...}`; 503 + `"unhealthy"` while DB migrations are incomplete |
| Auth | `POST /api/token` with form-encoded `username` & `password` → JWT, sent as `Authorization: Bearer <token>`. MCP API keys (`trinity_mcp_*`) also work as Bearer tokens |
| Unauthenticated endpoints | `/health`, `/api/auth/mode`, `/api/setup/status`, `/api/token` |
| Agent SSH ports | 2222+ (incrementing per agent) |
| Required agent files | `CLAUDE.md` (agent instructions), `template.yaml` (metadata); optional `.env.example`, `.mcp.json.template` |
| Persistence | SQLite at `~/trinity-data/trinity.db` (host bind mount); Redis for transient state |
| License | Apache 2.0 — free for any use, commercial included |

## Stand up a Trinity instance

Prerequisites: Docker + Docker Compose v2; an Anthropic API key (Claude agents) or Google API key (Gemini agents).

```bash
curl -fsSL https://raw.githubusercontent.com/abilityai/trinity/main/install.sh | bash
```

Then open `http://localhost` → setup wizard → set the admin password → **Settings → API Keys** to add the model API key.

**Verify:**

```bash
curl -s http://localhost:8000/health
# → {"status": "healthy", "timestamp": "..."}
```

Manual install and production deployment: [docs/user-docs/guides/deploying-trinity.md](docs/user-docs/guides/deploying-trinity.md). From inside Claude Code, `/trinity:deploy-new-instance` (abilities marketplace) provisions Trinity on any SSH-reachable server and scaffolds an ops agent for it.

## Deploy an agent to Trinity

**Prerequisite:** a running Trinity instance to deploy to — if you don't have one, [Stand up an instance](#stand-up-a-trinity-instance) first (`trinity deploy` against no instance fails with a connection error).

An agent is a directory with `CLAUDE.md` + `template.yaml`. Minimal `template.yaml`:

```yaml
name: my-agent                  # unique identifier (lowercase, hyphens ok)
display_name: "My Agent"
description: "What this agent does"
resources:
  cpu: "2"
  memory: "4g"
```

Full schema (credentials, runtime selection, metrics, shared folders): [docs/TRINITY_COMPATIBLE_AGENT_GUIDE.md](docs/TRINITY_COMPATIBLE_AGENT_GUIDE.md). Rather than author one from scratch, start from a ready-made template — catalog: [config/agent-templates/README.md](config/agent-templates/README.md) (also `GET /api/templates` / `list_templates` over MCP).

**If you are running inside Claude Code** — use the abilities plugins:

```text
/plugin marketplace add abilityai/abilities      # one-time
/plugin install trinity@abilityai
/trinity:connect        # one-time per instance: URL + email code → MCP key + .mcp.json
/trinity:onboard        # per agent: compatibility check, Trinity files, deploy + start
/trinity:sync           # ongoing: push/pull changes between local and remote
```

Terminal equivalent for the installs: `claude plugin marketplace add abilityai/abilities && claude plugin install trinity@abilityai`.

**Any other runtime** — use the CLI (deterministic, scriptable):

```bash
pip install trinity-cli
trinity init                          # instance URL + email code → JWT + MCP key
cd my-agent/ && trinity deploy .      # package, upload, create + start the container
```

**Done when** `trinity agents list` shows the agent with status `running` (uses the key from `trinity init` — no extra token needed). Equivalent raw-API check, deriving a token first:

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/api/token \
  -d 'username=admin&password=YOUR_ADMIN_PASSWORD' | jq -r .access_token)
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents
```

## Operate a Trinity instance over MCP

Get an MCP API key (Settings → API Keys in the UI; `/trinity:connect` and `trinity init` auto-provision one), then configure:

```json
{
  "mcpServers": {
    "trinity": {
      "type": "http",
      "url": "http://localhost:8080/mcp",
      "headers": { "Authorization": "Bearer trinity_mcp_..." }
    }
  }
}
```

~80 tools: agent lifecycle, chat, schedules, executions, skills, tags, monitoring, loops, system deployment. Catalog: [docs/user-docs/integrations/mcp-server.md](docs/user-docs/integrations/mcp-server.md).

Caveats that matter to agents:

- Claude Code enforces a 60-second timeout on MCP HTTP tool calls. For longer tasks call `chat_with_agent` with `async=true, parallel=true` to get an `execution_id` immediately, then poll `get_execution_result`.
- Agent-scoped keys see only their permitted agents; user-scoped keys see the owner's agents. Details: [docs/user-docs/collaboration/agent-permissions.md](docs/user-docs/collaboration/agent-permissions.md).

## Work on this repository

**Claude Code auto-loads [CLAUDE.md](CLAUDE.md)** — full dev guidelines, SDLC, and architectural invariants live there. The short version for other agents:

- **This is a PUBLIC repository.** Never commit credentials, API keys, internal URLs, or PII. Use placeholders (`your-domain.com`, `user@example.com`). Review `git diff` before every commit.
- Run the stack: `./scripts/deploy/start.sh` (Docker must be running). Stop: `./scripts/deploy/stop.sh`. Rebuild the agent base image after `docker/base-image/` changes: `./scripts/deploy/build-base-image.sh`.
- Tests: `python -m pytest -v --tb=short` (markers: `unit` needs no backend, `requires_agent` needs a running agent).
- Layout: `src/backend` (FastAPI), `src/frontend` (Vue 3 + Pinia), `src/mcp-server` (TypeScript MCP proxy), `src/cli`, `docker/base-image` (agent runtime).
- Backend pattern: router → service → db (`src/backend/routers|services|db`); schema changes require a versioned migration in `src/backend/db/migrations.py`.
- Workflow: GitHub Issues with priority/type/theme labels; feature branches off `dev`; PRs target `dev` (releases merge `dev` → `main`). **Two-tracker open-core model:** bugs/refactor/docs live in public `abilityai/trinity`; features/epics in private `abilityai/trinity-enterprise` (see `.claude/DEVELOPMENT_WORKFLOW.md` → Repository Routing). See [CONTRIBUTING.md](CONTRIBUTING.md).

## Documentation map

| Resource | What's in it |
|----------|--------------|
| [docs/user-docs/README.md](docs/user-docs/README.md) | **The detailed docs index** — guides, agents, credentials, collaboration, automation, operations, sharing, integrations, CLI, abilities plugins, API reference |
| [docs/memory/architecture.md](docs/memory/architecture.md) | Current system design: components, cross-cutting subsystems, DB schema, invariants |
| [docs/TRINITY_COMPATIBLE_AGENT_GUIDE.md](docs/TRINITY_COMPATIBLE_AGENT_GUIDE.md) | Agent template structure in depth |
| [docs/MULTI_AGENT_SYSTEM_GUIDE.md](docs/MULTI_AGENT_SYSTEM_GUIDE.md) | Multi-agent YAML manifests and coordination patterns |
| [docs/CLI.md](docs/CLI.md) | Full `trinity` CLI reference and multi-instance profiles |
| [docs/KNOWN_ISSUES.md](docs/KNOWN_ISSUES.md) | Current limitations and workarounds |
| [abilityai/abilities](https://github.com/abilityai/abilities) | The plugin marketplace — agent lifecycle workflows (scaffold, develop, deploy, iterate) |

Programmatic docs Q&A: `./scripts/ask-trinity.sh "your question"` from a checkout, or the hosted endpoint linked in [docs/user-docs/getting-started/help.md](docs/user-docs/getting-started/help.md).
