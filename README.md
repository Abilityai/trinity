# Trinity

**Deep Agent Orchestration Platform** — Sovereign infrastructure for deploying, orchestrating, and governing autonomous AI systems that plan, reason, and execute independently.

Unlike reactive chatbots ("System 1" AI), Deep Agents operate with deliberative reasoning ("System 2" AI): they decompose goals into task graphs, persist memory across sessions, delegate to specialized sub-agents, and recover from failures autonomously.

## The Four Pillars of Deep Agency

Trinity implements four foundational capabilities that transform simple AI assistants into autonomous agents:

1. **Explicit Planning** — Workplans that persist outside the context window
2. **Hierarchical Delegation** — Orchestrator-Worker pattern with context quarantine
3. **Persistent Memory** — Virtual filesystems, memory folding, episodic memory
4. **Extreme Context Engineering** — High-Order Prompts defining reasoning processes

## Features

- **Isolated Agent Containers** — Each agent runs in its own Docker container with dedicated resources
- **Template-Based Deployment** — Create agents from pre-configured templates or custom configurations
- **Real-Time Monitoring** — WebSocket-based activity streaming, telemetry, and context tracking
- **MCP Integration** — 12 tools for external agent orchestration via Model Context Protocol
- **Agent-to-Agent Communication** — Hierarchical delegation with permission controls
- **Workplan System** — Visual task graphs with dependencies and progress tracking
- **Credential Management** — Redis-backed secrets with hot-reload capability
- **Scheduling** — Cron-based automation for recurring agent tasks
- **File Browser** — Browse and download agent workspace files via web UI

## Quick Start

### Prerequisites

- Docker and Docker Compose v2+
- Anthropic API key (for Claude-powered agents)

### Installation

```bash
# 1. Clone the repository
git clone https://github.com/abilityai/trinity.git
cd trinity

# 2. Configure environment
cp .env.example .env
# Edit .env - at minimum set:
#   SECRET_KEY (generate with: openssl rand -hex 32)
#   ADMIN_PASSWORD
#   ANTHROPIC_API_KEY

# 3. Build the base agent image
./scripts/deploy/build-base-image.sh

# 4. Start all services
./scripts/deploy/start.sh
```

### Access

- **Web UI**: http://localhost:3000
- **API Docs**: http://localhost:8000/docs
- **MCP Server**: http://localhost:8080/mcp

Default login (dev mode): `admin` / your configured `ADMIN_PASSWORD`

### Create Your First Agent

1. Open http://localhost:3000
2. Click **Create Agent**
3. Enter a name and select a template (or leave blank for a basic agent)
4. Click **Create**

Your agent will start automatically. Use the Chat tab to interact with it.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      Trinity Platform                        │
├─────────────────────────────────────────────────────────────┤
│  Frontend (Vue.js)  │  Backend (FastAPI)  │  MCP Server     │
│     Port 3000       │     Port 8000       │    Port 8080    │
├─────────────────────────────────────────────────────────────┤
│  Redis (secrets)    │  SQLite (data)      │  Audit Logger   │
│   Internal only     │   /data volume      │    Port 8001    │
├─────────────────────────────────────────────────────────────┤
│                    Agent Containers                          │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐                     │
│  │ Agent 1 │  │ Agent 2 │  │ Agent N │  ...                │
│  └─────────┘  └─────────┘  └─────────┘                     │
└─────────────────────────────────────────────────────────────┘
```

## Project Structure

```
trinity/
├── src/
│   ├── backend/          # FastAPI backend API
│   ├── frontend/         # Vue.js 3 + Tailwind CSS web UI
│   ├── mcp-server/       # Trinity MCP server (12 tools)
│   └── audit-logger/     # Security audit service
├── docker/
│   ├── base-image/       # Universal agent base image
│   ├── backend/          # Backend Dockerfile
│   └── frontend/         # Frontend Dockerfile
├── config/
│   └── agent-templates/  # Pre-configured agent templates
├── scripts/
│   └── deploy/           # Deployment and management scripts
└── docs/                 # Documentation
```

## Agent Templates

Trinity deploys agents from templates. Templates define agent behavior, resources, and credential requirements.

### Template Structure

```
my-template/
├── template.yaml              # Metadata, resources, credentials
├── CLAUDE.md                  # Agent instructions
├── .claude/                   # Claude Code configuration
│   ├── agents/               # Sub-agents (optional)
│   ├── commands/             # Slash commands (optional)
│   └── skills/               # Custom skills (optional)
├── .mcp.json.template        # MCP config with ${VAR} placeholders
└── .env.example              # Documents required env vars
```

### Design Guides

| Guide | Use Case |
|-------|----------|
| [Trinity Compatible Agent Guide](docs/TRINITY_COMPATIBLE_AGENT_GUIDE.md) | **Single agents** — Template structure, CLAUDE.md, credentials, platform injection |
| [Multi-Agent System Guide](docs/MULTI_AGENT_SYSTEM_GUIDE.md) | **Multi-agent systems** — Architecture patterns, shared folders, coordination, deployment |

The Multi-Agent System Guide covers Trinity's platform capabilities that enable autonomous operation:
- **Vector Database (Chroma)** — Per-agent semantic memory with MCP tools
- **Scheduling System** — Cron-based autonomous execution
- **Workplan System** — Persistent task DAGs outside context window
- **Shared Folders** — File-based state sharing between agents
- **Agent-to-Agent MCP** — Real-time delegation and collaboration

### Public Agent Templates

Trinity includes three reference agent implementations that demonstrate real-world agent patterns. These repositories are **public and available for use as templates** for your own agents:

| Agent | Repository | Purpose |
|-------|------------|---------|
| **Cornelius** | [github.com/abilityai/agent-cornelius](https://github.com/abilityai/agent-cornelius) | Knowledge Base Manager — Obsidian vault management, insight synthesis, research coordination |
| **Corbin** | [github.com/abilityai/agent-corbin](https://github.com/abilityai/agent-corbin) | Business Assistant — Google Workspace integration, task coordination, team management |
| **Ruby** | [github.com/abilityai/agent-ruby](https://github.com/abilityai/agent-ruby) | Content Creator — Multi-platform publishing, social media distribution, content strategy |

These agents demonstrate:
- Production-ready template structure with `template.yaml`, `CLAUDE.md`, and `.claude/` configuration
- Agent-to-agent collaboration patterns via Trinity MCP
- Custom metrics definitions for specialized tracking
- Credential management for external API integrations
- Real-world slash commands and workflow automation

**Usage**: Create agents from these templates via the Trinity UI:
```bash
# Via UI: Create Agent → Select "github:abilityai/agent-cornelius"
# Via MCP: trinity_create_agent(name="my-agent", template="github:abilityai/agent-cornelius")
```

**Note**: You'll need to configure a `GITHUB_PAT` environment variable in `.env` to use GitHub templates.

## MCP Integration

Trinity includes an MCP server for external orchestration of agents:

```json
{
  "mcpServers": {
    "trinity": {
      "type": "http",
      "url": "http://localhost:8080/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_API_KEY"
      }
    }
  }
}
```

### Available Tools

| Tool | Description |
|------|-------------|
| `list_agents` | List all agents with status |
| `get_agent` | Get detailed agent information |
| `create_agent` | Create a new agent |
| `start_agent` | Start a stopped agent |
| `stop_agent` | Stop a running agent |
| `delete_agent` | Delete an agent |
| `chat_with_agent` | Send a message and get response |
| `get_chat_history` | Retrieve conversation history |
| `list_templates` | List available templates |
| `reload_credentials` | Hot-reload agent credentials |
| `get_credential_status` | Check credential files |
| `get_agent_logs` | View container logs |

## Configuration

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `SECRET_KEY` | Yes | JWT signing key |
| `ADMIN_PASSWORD` | Yes | Admin user password |
| `ANTHROPIC_API_KEY` | Yes | For Claude-powered agents |
| `DEV_MODE_ENABLED` | No | Enable local login (default: true) |
| `AUTH0_DOMAIN` | No | Auth0 tenant for OAuth |
| `EXTRA_CORS_ORIGINS` | No | Additional CORS origins |

See [.env.example](.env.example) for the complete list.

### Authentication

**Development Mode** (default): Username/password login
```bash
DEV_MODE_ENABLED=true
```

**Production Mode**: Auth0 OAuth
```bash
DEV_MODE_ENABLED=false
AUTH0_DOMAIN=your-tenant.us.auth0.com
```

## Documentation

- [Deployment Guide](docs/DEPLOYMENT.md) — Production deployment instructions
- [Trinity Compatible Agent Guide](docs/TRINITY_COMPATIBLE_AGENT_GUIDE.md) — Creating Trinity-compatible agents
- [Multi-Agent System Guide](docs/MULTI_AGENT_SYSTEM_GUIDE.md) — Building multi-agent systems with coordinated workflows
- [Testing Guide](docs/TESTING_GUIDE.md) — Testing approach and standards
- [Known Issues](docs/KNOWN_ISSUES.md) — Current limitations and workarounds

## Development

```bash
# Start in development mode (hot reload)
./scripts/deploy/start.sh

# View logs
docker compose logs -f backend
docker compose logs -f frontend

# Rebuild after changes
docker compose build backend
docker compose up -d backend
```

## License

This project is licensed under the [Polyform Noncommercial License 1.0.0](LICENSE).

**Free for**:
- Personal use
- Research and education
- Non-profit organizations
- Hobby projects

**Commercial use** requires a separate license. Contact [hello@ability.ai](mailto:hello@ability.ai) for commercial licensing.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## Support

- **Issues**: [GitHub Issues](https://github.com/abilityai/trinity/issues)
- **Commercial inquiries**: [hello@ability.ai](mailto:hello@ability.ai)

---

Built by [Ability AI](https://ability.ai)
