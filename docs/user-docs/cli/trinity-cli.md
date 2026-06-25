# Trinity CLI

Command-line interface for managing Trinity agents from your terminal. Provides the same capabilities as the MCP server but invoked via shell commands.

> 📺 **Watch:** [From Zero to Deployed AI Agent](https://youtu.be/-TSZyekDS6o) *(Apr 2026)* · [all videos](../videos.md)

## Installation

```bash
# PyPI (recommended)
pip install trinity-cli

# Homebrew (macOS/Linux)
brew install abilityai/tap/trinity-cli

# Verify installation
trinity --version
```

## How It Works

### First-Time Setup

Run `trinity init` to connect to a Trinity instance:

```bash
trinity init
```

You'll be prompted for:
1. **Instance URL** — Your Trinity server (e.g., `trinity.example.com`)
2. **Email** — Your login email
3. **Verification code** — 6-digit code sent to your email

The CLI stores credentials in `~/.trinity/config.json` and auto-provisions an MCP API key.

### Multi-Instance Profiles

Manage multiple Trinity instances (local, staging, production) with named profiles:

```bash
# List all profiles
trinity profile list

# Switch active profile
trinity profile use production

# Remove a profile
trinity profile remove staging
```

Profile resolution priority:
1. `TRINITY_URL` / `TRINITY_API_KEY` environment variables
2. `--profile <name>` flag
3. `TRINITY_PROFILE` environment variable
4. `current_profile` in config file

### Returning Users

Re-authenticate with an existing profile:

```bash
trinity login
```

## Commands

### Agent Management

```bash
# List all agents
trinity agents list

# Get agent details
trinity agents get my-agent

# Create from GitHub template
trinity agents create my-agent --template github:user/repo

# Start/stop agents
trinity agents start my-agent
trinity agents stop my-agent

# Rename an agent
trinity agents rename old-name new-name

# Delete an agent
trinity agents delete my-agent
```

### Deploy

Deploy a local agent directory to Trinity:

```bash
# Deploy current directory
trinity deploy .

# Deploy with custom name
trinity deploy . --name my-agent

# Deploy from GitHub
trinity deploy --repo user/repo
```

The CLI creates `.trinity-remote.yaml` to track deployments, enabling idempotent redeploys.

### Chat and Logs

```bash
# Send a message
trinity chat my-agent "Hello, what can you do?"

# View chat history
trinity history my-agent

# View container logs
trinity logs my-agent
```

### Health Monitoring

```bash
# Fleet-wide health status
trinity health fleet

# Single agent health
trinity health agent my-agent
```

### Skills and Schedules

```bash
# List available skills
trinity skills list

# Get skill details
trinity skills get skill-name

# List agent schedules
trinity schedules list my-agent

# Trigger a schedule manually
trinity schedules trigger my-agent schedule-id
```

### Tags

```bash
# List all tags
trinity tags list

# Get tags for an agent
trinity tags get my-agent
```

## Output Formats

```bash
# Table output (default)
trinity agents list

# JSON output (for scripting)
trinity agents list --format json
```

## Environment Variables

| Variable | Description |
|----------|-------------|
| `TRINITY_URL` | Override instance URL |
| `TRINITY_API_KEY` | Override authentication token |
| `TRINITY_PROFILE` | Set active profile |

## For Agents

Agents can use the CLI in their workflows for self-management or fleet operations:

```bash
# Deploy updates to self
trinity deploy .

# Trigger another agent's schedule
trinity schedules trigger other-agent daily-report
```

## Limitations

- Phase 2 coverage: core agent, chat, health, skills, schedules, tags
- Phase 3 (future): credentials, events, executions, systems, subscriptions

## See Also

- [MCP Server](../integrations/mcp-server.md) — Programmatic access via MCP tools
- [Authentication](../api-reference/authentication.md) — API authentication patterns
