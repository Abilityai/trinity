# What is Trinity

Trinity is an autonomous agent orchestration and infrastructure platform — sovereign infrastructure for deploying, orchestrating, and governing fleets of autonomous AI agents on your own hardware.

> 📺 **Watch:** [Trinity Platform Demo](https://youtu.be/ivljtZqsxeo) *(May 2026)* · [The Multi-Agent Platform I Run My Company On](https://youtu.be/8j6q-kABRqc) *(May 2026)* · [all videos](../videos.md)

## Concepts

**Autonomous Agent** -- An AI system that plans and executes tasks independently. Each agent runs as an isolated Docker container with pre-installed languages (Python 3.13, Node.js 20, Go 1.23) and a pluggable agent runtime: Claude Code, OpenAI Codex, or Gemini CLI (see [Agent Runtimes](../agents/agent-runtimes.md)). Agents persist memory across sessions, delegate to other agents, and run on schedules without human intervention.

**Agent Container** -- An isolated Docker container with standardized interfaces for credentials, tools, and MCP server integrations.

**Template** -- A GitHub repository or local directory that defines an agent's initial configuration, including CLAUDE.md, template.yaml, .mcp.json.template, and credential declarations.

**MCP (Model Context Protocol)** -- The protocol agents use to communicate with each other and with external tools. Trinity's MCP server exposes 130 tools for fleet management, credential injection, scheduling, file sharing, per-user memory, channel messaging, and more.

**System Agent** -- An auto-deployed platform orchestrator (`trinity-system`) that manages fleet operations such as health checks, scaling, and coordination.

**Autonomy Mode** -- A master toggle that enables or disables all scheduled operations for a given agent.

**Execution** -- A single run of a task on an agent. Executions can be triggered manually, by a cron schedule, by another agent, or via the API.

**Public Link** -- A shareable URL that allows unauthenticated users to chat with an agent directly.

**Workspace** -- The chat app. One continuous conversation per agent that keeps its memory across turns, with voice, file drops, loops, deliverables, and the agent's canvas in one place. Platform users open it from the top nav; external clients sign in to it directly. See [Workspace](../sharing-and-access/workspace.md).

**Canvas** -- A surface an agent keeps current — blocks of text, metrics, charts, images, and diagrams it updates as it works — shown beside the conversation. See [Agent Canvas](../agents/agent-canvas.md).

## How It Works

Trinity runs as a set of Docker containers on your local machine or server. After starting the platform, you interact with it through the web UI or the API.

1. **Start the platform** -- Run `./scripts/deploy/start.sh` to bring up all services. The web UI is available at `http://localhost` and the API at `http://localhost:8000/docs`.
2. **Create an agent** -- From the dashboard, click "Create Agent" and select a template (GitHub repo URL or local path). Trinity pulls the template, builds a container, and deploys the agent.
3. **Configure credentials** -- Add API keys and secrets through the agent's credential panel. Credentials are encrypted in Redis and injected into the container at runtime with hot-reload support.
4. **Chat with the agent** -- Open the **Workspace** for a continuous conversation, or use the stateless Chat tab on the agent detail page for one-off turns. The agent processes your request using its configured tools, MCP connections, and reasoning context.
5. **Schedule autonomous work** -- Set up cron-based schedules so the agent executes tasks on its own. Enable Autonomy Mode to let the agent's enabled schedules run.
6. **Monitor the fleet** -- Use the Dashboard (Timeline, Grid, or List) to view agent health and execution history; inter-agent calls show up in the Timeline replay.

## For Agents

All platform operations are available through the REST API and the MCP server.

### Architecture

| Component | Technology | Port | Purpose |
|-----------|-----------|------|---------|
| Frontend | Vue.js 3 + Tailwind CSS | 80 | Web dashboard and chat UI |
| Backend | FastAPI (Python) | 8000 | REST API, 500+ endpoints across 70+ routers |
| MCP Server | FastMCP, Streamable HTTP | 8080 | 130 tools for agent-to-agent and agent-to-platform communication |
| Vector | Log aggregation | 8686 | Structured logging from all containers |
| Redis | Secrets and cache | 6379 | Encrypted credential storage |
| Docker Engine | Container orchestration | -- | Agent lifecycle management |
| Agent Network | Isolated bridge | 172.28.0.0/16 | Container-to-container communication |

### Key API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/token` | Authenticate and receive a JWT token |
| GET | `/api/agents` | List all agents |
| POST | `/api/agents` | Create a new agent from a template |
| GET | `/api/agents/{name}` | Get agent details |
| DELETE | `/api/agents/{name}` | Delete an agent |
| POST | `/api/agents/{name}/start` | Start an agent container |
| POST | `/api/agents/{name}/stop` | Stop an agent container |
| POST | `/api/agents/{name}/chat` | Send a message to an agent |
| GET | `/api/agents/{name}/schedules` | List agent schedules |
| POST | `/api/agents/{name}/schedules` | Create a scheduled execution |

Authentication uses Bearer tokens. Obtain a token from `/api/token` using form-encoded credentials, then pass it in the `Authorization: Bearer <token>` header on all subsequent requests.

```bash
# Authenticate
TOKEN=$(curl -s --fail-with-body -X POST http://localhost:8000/api/token \
  -d 'username=admin&password=your-password' \
  | python3 -c "import json,sys; print(json.load(sys.stdin).get('access_token') or '')")
# 403 = correct password, second factor required (no session). Use an MCP API
# key for automation — see api-reference/authentication.md.
[ -n "$TOKEN" ] || { echo "login issued no session" >&2; exit 1; }

# List agents
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents
```

### Platform Capabilities

- Create agents from GitHub templates or local directories
- Multi-runtime support: Claude Code, OpenAI Codex, Gemini CLI (see [Agent Runtimes](../agents/agent-runtimes.md))
- Credential management with encryption and hot-reload
- Agent-to-agent collaboration via MCP tool calls
- Cron-based scheduling with execution history and per-schedule timeouts
- Real-time monitoring dashboard with Timeline, Grid, and List views
- Public chat links for external users
- Channel adapters: Slack, Telegram, WhatsApp (via Twilio)
- Outbound file sharing — agents publish files to signed download URLs
- A2A `0.3.0` in both directions — Agent Card discovery and opt-in inbound tasking, plus outbound calls to external A2A agents
- Voice calls with an agent inside the Workspace conversation (Gemini Live API)
- x402 payment protocol for agent monetization
- Prebuilt images for a pull-only install, and a DigitalOcean Marketplace 1-Click image
- Opt-in, anonymous usage sharing (off by default)

## See Also

- [Setup](setup.md) -- Set up Trinity on your machine
- [Quick Start](quick-start.md) -- Create your first agent in 5 minutes
- [Trinity-Compatible Agent Guide](../../TRINITY_COMPATIBLE_AGENT_GUIDE.md) -- How to make any agent work with Trinity
- [API Documentation](http://localhost:8000/docs) -- Full interactive API reference
