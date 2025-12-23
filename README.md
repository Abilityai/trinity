# Trinity

**Deep Agent Orchestration Platform** — Sovereign infrastructure for deploying, orchestrating, and governing autonomous AI systems that plan, reason, and execute independently.

Unlike reactive chatbots ("System 1" AI), Deep Agents operate with deliberative reasoning ("System 2" AI): they decompose goals into task graphs, persist memory across sessions, delegate to specialized sub-agents, and recover from failures autonomously.

## The Four Pillars of Deep Agency

Trinity implements four foundational capabilities that transform simple AI assistants into autonomous agents:

1. **Hierarchical Delegation** — Orchestrator-Worker pattern with context quarantine
2. **Persistent Memory** — Virtual filesystems, vector databases, episodic memory
3. **Extreme Context Engineering** — High-Order Prompts defining reasoning processes
4. **Autonomous Operations** — Scheduling, monitoring, and self-healing capabilities

## Features

### Core Platform
- **Isolated Agent Containers** — Each agent runs in its own Docker container with dedicated resources
- **Template-Based Deployment** — Create agents from pre-configured templates or custom configurations
- **Real-Time Monitoring** — WebSocket-based activity streaming, telemetry, and context tracking
- **First-Time Setup Wizard** — Guided setup for admin password and API key configuration

### Agent Capabilities
- **MCP Integration** — 16 tools for external agent orchestration via Model Context Protocol
- **Agent-to-Agent Communication** — Hierarchical delegation with fine-grained permission controls
- **Vector Memory (Chroma)** — Per-agent semantic memory with MCP tools for retrieval
- **Shared Folders** — File-based state sharing between agents via Docker volumes
- **Parallel Task Execution** — Stateless parallel tasks for orchestrator-worker patterns

### Operations
- **System Manifest Deployment** — Deploy multi-agent systems from YAML configuration
- **Internal System Agent** — Platform orchestrator for fleet health monitoring and operations
- **Credential Management** — Redis-backed secrets with hot-reload capability
- **Scheduling** — Cron-based automation for recurring agent tasks
- **OpenTelemetry Metrics** — Cost, token usage, and productivity tracking
- **Public Agent Links** — Shareable links for unauthenticated agent access
- **File Browser** — Browse and download agent workspace files via web UI

## Quick Start

### Prerequisites

- Docker and Docker Compose v2+
- Anthropic API key (for Claude-powered agents)

### One-Line Install

```bash
curl -fsSL https://raw.githubusercontent.com/abilityai/trinity/main/install.sh | bash
```

This will clone the repository, configure environment, build the base image, and start all services.

### Manual Installation

```bash
# 1. Clone the repository
git clone https://github.com/abilityai/trinity.git
cd trinity

# 2. Configure environment
cp .env.example .env
# Edit .env - at minimum set:
#   SECRET_KEY (generate with: openssl rand -hex 32)

# 3. Build the base agent image
./scripts/deploy/build-base-image.sh

# 4. Start all services
./scripts/deploy/start.sh
```

### First-Time Setup

On first launch, Trinity will guide you through initial setup:

1. Open http://localhost:3000 — you'll be redirected to the setup wizard
2. Set your **admin password** (minimum 8 characters)
3. Log in with username `admin` and your new password
4. Go to **Settings** → **API Keys** to configure your Anthropic API key

### Access

- **Web UI**: http://localhost:3000
- **API Docs**: http://localhost:8000/docs
- **MCP Server**: http://localhost:8080/mcp

### Create Your First Agent

1. Open http://localhost:3000
2. Click **Create Agent**
3. Enter a name and select a template (or leave blank for a basic agent)
4. Click **Create**

Your agent will start automatically. Use the Chat tab to interact with it.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                       Trinity Platform                           │
├─────────────────────────────────────────────────────────────────┤
│  Frontend (Vue.js)  │  Backend (FastAPI)  │  MCP Server         │
│     Port 3000       │     Port 8000       │    Port 8080        │
├─────────────────────────────────────────────────────────────────┤
│  Redis (secrets)    │  SQLite (data)      │  Audit Logger       │
│   Internal only     │   /data volume      │    Port 8001        │
├─────────────────────────────────────────────────────────────────┤
│                    Agent Containers                              │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌────────────────┐    │
│  │ Agent 1 │  │ Agent 2 │  │ Agent N │  │ trinity-system │    │
│  └─────────┘  └─────────┘  └─────────┘  └────────────────┘    │
├─────────────────────────────────────────────────────────────────┤
│  (Optional) OTel Collector - Port 4317/8889 for metrics export  │
└─────────────────────────────────────────────────────────────────┘
```

## Project Structure

```
trinity/
├── src/
│   ├── backend/          # FastAPI backend API
│   ├── frontend/         # Vue.js 3 + Tailwind CSS web UI
│   ├── mcp-server/       # Trinity MCP server (16 tools)
│   └── audit-logger/     # Security audit service
├── docker/
│   ├── base-image/       # Universal agent base image
│   ├── backend/          # Backend Dockerfile
│   └── frontend/         # Frontend Dockerfile
├── config/
│   ├── agent-templates/  # Pre-configured agent templates
│   ├── otel-collector.yaml # OpenTelemetry collector config
│   └── trinity-meta-prompt/ # Platform injection templates
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

#### Agent Management
| Tool | Description |
|------|-------------|
| `list_agents` | List all agents with status |
| `get_agent` | Get detailed agent information |
| `create_agent` | Create a new agent from template |
| `start_agent` | Start a stopped agent |
| `stop_agent` | Stop a running agent |
| `delete_agent` | Delete an agent |

#### Communication
| Tool | Description |
|------|-------------|
| `chat_with_agent` | Send a message and get response (supports parallel mode) |
| `get_chat_history` | Retrieve conversation history |
| `get_agent_logs` | View container logs |

#### System Management
| Tool | Description |
|------|-------------|
| `deploy_system` | Deploy multi-agent system from YAML manifest |
| `list_systems` | List deployed systems with agent counts |
| `restart_system` | Restart all agents in a system |
| `get_system_manifest` | Export system configuration as YAML |

#### Configuration
| Tool | Description |
|------|-------------|
| `list_templates` | List available templates |
| `reload_credentials` | Hot-reload agent credentials |
| `get_credential_status` | Check credential files |

## Multi-Agent Systems

Deploy coordinated multi-agent systems from a single YAML manifest:

```yaml
name: content-production
description: Autonomous content pipeline

agents:
  orchestrator:
    template: github:abilityai/agent-corbin
    resources: {cpu: "2", memory: "4g"}
    folders: {expose: true, consume: true}
    schedules:
      - name: daily-review
        cron: "0 9 * * *"
        message: "Review today's content pipeline"

  writer:
    template: github:abilityai/agent-ruby
    folders: {expose: true, consume: true}

permissions:
  preset: full-mesh  # All agents can communicate
```

Deploy via MCP or API:
```bash
# Via MCP tool
mcp__trinity__deploy_system(manifest="...")

# Via REST API
curl -X POST http://localhost:8000/api/systems/deploy \
  -H "Content-Type: application/json" \
  -d '{"manifest": "...", "dry_run": false}'
```

See the [Multi-Agent System Guide](docs/MULTI_AGENT_SYSTEM_GUIDE.md) for architecture patterns and best practices.

## Configuration

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `SECRET_KEY` | Yes | JWT signing key (generate with `openssl rand -hex 32`) |
| `ANTHROPIC_API_KEY` | No | For Claude-powered agents (can also be set via Settings UI) |
| `DEV_MODE_ENABLED` | No | Enable local login (default: true) |
| `AUTH0_DOMAIN` | No | Auth0 tenant for production OAuth |
| `GITHUB_PAT` | No | GitHub PAT for cloning private template repos |
| `OTEL_ENABLED` | No | Enable OpenTelemetry metrics export (default: false) |
| `EMAIL_PROVIDER` | No | Email provider for public links: console, smtp, sendgrid |
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

- [Development Workflow](docs/DEVELOPMENT_WORKFLOW.md) — How to develop Trinity (context loading, testing, documentation)
- [Deployment Guide](docs/DEPLOYMENT.md) — Production deployment instructions
- [Trinity Compatible Agent Guide](docs/TRINITY_COMPATIBLE_AGENT_GUIDE.md) — Creating Trinity-compatible agents
- [Multi-Agent System Guide](docs/MULTI_AGENT_SYSTEM_GUIDE.md) — Building multi-agent systems with coordinated workflows
- [Testing Guide](docs/TESTING_GUIDE.md) — Testing approach and standards
- [Contributing Guide](CONTRIBUTING.md) — How to contribute (PRs, code standards)
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
