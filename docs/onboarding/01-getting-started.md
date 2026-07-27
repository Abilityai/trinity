# Getting Started with Trinity

This guide will walk you through installing Trinity, creating your first agent, and understanding the core concepts. By the end, you'll have a working Trinity installation and a running agent.

---

## Prerequisites

Before you begin, make sure you have:

- [ ] **Docker & Docker Compose v2+** installed ([Install Docker](https://docs.docker.com/get-docker/))
- [ ] **At least one AI API Key**:
  - **Anthropic API Key** for Claude agents ([Get API Key](https://console.anthropic.com/))
  - **Google API Key** for Gemini agents ([Get API Key](https://aistudio.google.com/apikey)) — *Free tier available!*
- [ ] **10GB+ free disk space** for images and agent containers
- [ ] **Basic command line skills** (running commands, editing files)

### System Requirements

- **OS**: Linux, macOS, or Windows with WSL2
- **RAM**: 8GB minimum, 16GB recommended
- **CPU**: 4+ cores recommended

> 💡 **Tip**: You can start with just Gemini (free tier) to try Trinity without any costs!

---

## Installation

### Option 1: Quick Install (Recommended)

Run the one-line installer:

```bash
curl -fsSL https://raw.githubusercontent.com/abilityai/trinity/main/install.sh | bash
```

This will:
1. Clone the Trinity repository
2. Create `.env` file from template
3. Build the base agent image
4. Start all services

**Time**: ~10-15 minutes depending on your connection speed.

### Option 2: Manual Installation

If you prefer manual control:

```bash
# 1. Clone the repository
git clone https://github.com/abilityai/trinity.git
cd trinity

# 2. Copy environment template
cp .env.example .env

# 3. Generate a secure secret key
openssl rand -hex 32

# 4. Edit .env and set at minimum:
#    SECRET_KEY=<your-generated-key>
#    ADMIN_PASSWORD=<strong-password>
nano .env

# 5. Build the base agent image (one-time, takes ~5 minutes)
./scripts/deploy/build-base-image.sh

# 6. Start all services
./scripts/deploy/start.sh
```

### Verify Installation

Check that all services are running:

```bash
docker compose ps
```

You should see:
- `trinity-backend` (running)
- `trinity-frontend` (running)
- `trinity-redis` (running)
- `trinity-audit-logger` (running)
- `trinity-mcp-server` (running)
- `trinity-otel-collector` (running, optional)

---

## First-Time Setup

### Step 1: Access the Web UI

Open your browser and navigate to:

```
http://localhost
```

(or `http://localhost:<FRONTEND_PORT>` if you changed it in `.env`)

You'll be redirected to the **Setup Wizard** on first launch.

### Step 2: Create Admin Account

1. **Create the Admin Account**
   - Enter your **admin email** — this becomes your sign-in identity
   - Enter a strong password (minimum 12 characters, including upper- and lowercase, a digit, and a special character)
   - Confirm the password
   - Click **Create Account**

2. **Login**
   - Email: the admin email you just entered (the username `admin` also works)
   - Password: (the password you just created)
   - Click **Sign In**

### Step 3: Configure API Keys

After logging in, configure your API keys in the `.env` file:

```bash
# For Claude Code agents
ANTHROPIC_API_KEY=sk-ant-api03-...

# For Gemini CLI agents (free tier available!)
GOOGLE_API_KEY=AIza...
```

You need at least one of these configured. You can use both to mix Claude and Gemini agents.

> 💡 **Tip**: Gemini's free tier is great for experimentation. Claude is better for complex reasoning tasks.

---

## Create Your First Agent

Now let's create your first agent!

### Step 1: Navigate to Agent Creation

1. Click **Agents** in the navigation menu
2. Click the **+ Create Agent** button

### Step 2: Configure Agent

Fill in the agent details:

- **Name**: `my-first-agent` (lowercase, hyphens allowed)
- **Template**: Select from available templates:
  - `Blank Agent` — Empty Claude Code agent (default)
  - `Test Gemini Agent` — Gemini CLI agent for testing
  - Or any custom template from `config/agent-templates/`

Click **Create Agent**.

> 💡 **Runtime Selection**: Templates define which runtime (Claude or Gemini) the agent uses. Check the template's `runtime` field in its `template.yaml`.

### Step 3: Wait for Agent to Start

Trinity will:
1. Create a Docker container for your agent
2. Inject platform capabilities (vector memory, planning tools, etc.)
3. Start the Claude Code agent server
4. Show status updates in the UI

**Time**: ~30-60 seconds for first agent (downloads Claude image).

### Step 4: Chat with Your Agent

Once the agent shows **Status: Running**:

1. Click on your agent in the list
2. Go to the **Chat** tab
3. Type a message: `Hello! What can you do?`
4. Press Enter or click **Send**

Your agent should respond, explaining its capabilities!

---

## Understanding the UI

Let's explore the Trinity web interface.

### Dashboard (Home Page)

The **Collaboration Dashboard** at `/` shows:

- 🟢 **Agent Nodes** — Visual representation of all your agents
- 🔗 **Connections** — Animated lines show agent-to-agent communication
- 📊 **Context Bars** — See how much of the context window each agent is using
- 🔴 **Status Indicators** — Active/Idle/Offline states

**Try This**: Drag agents around to organize them visually. The layout persists.

### Agents Page

The **Agents** page lists all your agents with:

- Status (running/stopped/error)
- Resource usage
- Last activity timestamp
- Quick actions (start/stop/delete)

### Agent Detail View

Click on any agent to see:

- **Chat** — Talk with your agent
- **Activity** — See what tools the agent is using
- **Files** — Browse the agent's workspace
- **Logs** — View container logs for debugging
- **Schedules** — Set up cron-based automation
- **Permissions** — Control which agents can communicate
- **Shared Folders** — Configure file sharing
- **Plans** — View persistent task plans (if agent creates them)

---

## Core Concepts

### Agents

An **agent** is an AI runtime (Claude Code or Gemini CLI) running in a Docker container. Each agent:
- Has its own isolated filesystem
- Gets dedicated CPU and memory resources
- Can have custom instructions (CLAUDE.md)
- Can access different tools via MCP servers
- Has persistent storage for memory and files
- Uses a specific **runtime** (Claude or Gemini) defined by its template

**Runtime Comparison**:
| Feature | Claude Code | Gemini CLI |
|---------|-------------|------------|
| Context Window | 200K (up to 1M) | 1M tokens |
| Free Tier | No | Yes |
| Best For | Complex reasoning | Fast, cost-effective tasks |
| Model Selector | Sonnet, Opus, Haiku | Gemini 2.5/3 Pro/Flash |

### Templates

A **template** defines an agent's behavior and configuration. Templates include:
- `template.yaml` — Metadata (name, resources, credentials)
- `CLAUDE.md` — Instructions that define agent behavior
- `.mcp.json.template` — MCP server configurations
- `.env.example` — Documentation of required credentials

Trinity includes a `default` template, and you can use templates from GitHub repositories.

### Credentials

**Credentials** are API keys and secrets your agents need to access external services. Trinity:
- Stores credentials securely in Redis (encrypted)
- Injects them into agents at creation time
- Supports hot-reload (update credentials without restarting)
- Never exposes secrets in logs or UI

### Schedules

**Schedules** allow agents to run autonomously without human input. You define:
- Cron expression (e.g., `0 9 * * *` for 9 AM daily)
- Message to send to the agent
- Timezone
- Enabled/disabled state

The agent receives the scheduled message and executes the workflow.

### Permissions

**Permissions** control agent-to-agent communication. By default, agents cannot see or talk to each other. You explicitly grant permissions to enable collaboration.

### Shared Folders

**Shared folders** enable file-based communication between agents:
- Each agent has `/home/developer/shared-out/` (their output)
- Other agents can read this at `/home/developer/shared-in/{agent-name}/`
- Perfect for exchanging data, state, or coordination files

---

## Try These Tasks

Here are some hands-on exercises to get familiar with Trinity.

### Task 1: View Agent Activity

1. Chat with your agent: "Tell me a joke"
2. Go to the **Activity** tab
3. See the tool calls the agent made (token counting, chat history, etc.)

### Task 2: Explore the Filesystem

1. Go to the **Files** tab
2. Browse the agent's workspace
3. Look for `.trinity/` directory (platform-injected docs)
4. Open `CLAUDE.md` to see the agent's instructions

### Task 3: Check Logs

1. Go to the **Logs** tab
2. See the container startup logs
3. Try making the agent do something, then refresh logs
4. Use logs for debugging when things go wrong

### Task 4: Create a Schedule

1. Go to the **Schedules** tab
2. Click **+ Create Schedule**
3. Configure:
   - Name: `Daily Greeting`
   - Cron: `0 9 * * *` (9 AM daily)
   - Message: `Good morning! Summarize your tasks for today.`
   - Timezone: `America/Los_Angeles` (or your timezone)
   - Enabled: ✅
4. Click **Create**
5. Test it immediately with **Trigger Now** button

---

## What's Auto-Injected?

When your agent starts, Trinity automatically injects:

### 1. Vector Memory (Chroma)
- Database at `/home/developer/vector-store/`
- MCP tools for storing and querying semantic memory
- Documentation at `.trinity/vector-memory.md`

### 2. Planning System
- Slash commands: `/trinity-plan-create`, `/trinity-plan-update`, etc.
- Directories: `plans/active/`, `plans/archive/`
- Documentation at `.trinity/prompt.md`

### 3. Trinity MCP Tools
- Tools to list, chat with, and manage other agents
- Requires permissions to use
- Documented in appended section of `CLAUDE.md`

### 4. Platform Documentation
- `.trinity/prompt.md` — Planning instructions
- `.trinity/vector-memory.md` — Memory usage guide

**Important**: Your custom templates should NOT include this content. Trinity injects it automatically.

---

## Common First-Time Issues

### Issue: "Services failed to start"

**Solution**: Check Docker is running and you have enough disk space.

```bash
docker info
df -h
```

### Issue: "Agent stuck in 'starting' state"

**Solution**: Check logs for errors:

```bash
docker logs agent-my-first-agent
```

Common causes:
- Anthropic API key not set or invalid
- Network issues downloading Claude image
- Insufficient memory

### Issue: "Permission denied" errors

**Solution**: Make sure you have Docker permissions:

```bash
# Add yourself to docker group (Linux)
sudo usermod -aG docker $USER
newgrp docker
```

### Issue: "Agent responds very slowly"

**Solution**: This is normal for the first message. Subsequent messages are faster. If consistently slow:
- Check CPU usage: `docker stats`
- Check API key is valid
- Try stopping other agents to free resources

---

## Next Steps

Congratulations! You now have Trinity running and understand the basics.

### Ready to Build Something Real?

Continue to **[Use Case Scenarios](02-use-case-scenarios.md)** to see practical examples of what you can build.

### Want to Customize Your Agent?

Learn about agent templates in the **[Trinity Compatible Agent Guide](../TRINITY_COMPATIBLE_AGENT_GUIDE.md)**.

### Building Multi-Agent Systems?

Check out the **[Multi-Agent System Guide](../MULTI_AGENT_SYSTEM_GUIDE.md)** for advanced patterns.

### Need Help?

- **Troubleshooting**: [04-troubleshooting.md](04-troubleshooting.md)
- **API Reference**: http://localhost:8000/docs
- **GitHub Issues**: https://github.com/abilityai/trinity/issues

---

## Quick Reference

### Useful Commands

```bash
# Start Trinity
./scripts/deploy/start.sh

# Stop Trinity
./scripts/deploy/stop.sh

# View logs
docker compose logs -f backend
docker compose logs -f frontend
docker logs agent-my-first-agent

# Rebuild after changes
docker compose build backend
docker compose up -d backend

# Check agent status
docker ps | grep agent-
```

### Important URLs

- **Web UI**: http://localhost:3000
- **API**: http://localhost:8000
- **API Docs**: http://localhost:8000/docs
- **MCP Server**: http://localhost:8080/mcp

### Important Directories

- **Agent configs**: `./config/agent-templates/`
- **Trinity data**: Docker volume `trinity-data`
- **Backend code**: `./src/backend/`
- **Frontend code**: `./src/frontend/`

---

**You're ready to go! Head to the next guide to explore real use cases.** 🎉

