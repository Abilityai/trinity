# Trinity CLI

Command-line interface for Trinity, the operating system for the AI-native company. Manage agents from your terminal with shell commands — a subset of what the [MCP server](../integrations/mcp-server.md) offers (see Limitations).

> 📺 **Watch:** [From Zero to Deployed AI Agent](https://youtu.be/-TSZyekDS6o) *(Apr 2026)* · [all videos](../videos.md)

## Installation

```bash
# PyPI (recommended; Python 3.10+)
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

The CLI stores credentials in `~/.trinity/config.json` (mode 0600) and auto-provisions an MCP API key. Options: `--profile <name>` saves the instance under a named profile, and `--admin` logs in with the admin password instead of an email code.

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
trinity login                      # email code; --admin for the admin password
trinity login --instance https://trinity.example.com --profile staging   # add or repoint a profile
trinity status                     # which profile and instance you are on
trinity logout                     # clear the current profile's stored credentials
```

## Commands

### Agent Management

```bash
# List all agents
trinity agents list

# Get agent details
trinity agents get my-agent

# Create from GitHub template (created as an agent: a working branch with
# auto-sync when your own token can push to your own repo, else pull-only)
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

A directory deploy packages the folder (in a git repo, tracked files plus untracked files `.gitignore` does not exclude; always leaving out `.git`, `node_modules`, virtualenvs, and `.env`, `.env.local`, `.env.production`), adds an integrity manifest, and uploads it — the archive must stay under 50 MB. The agent name comes from `--name`, then `template.yaml`'s `name`, then the directory name. The first deploy writes `.trinity-remote.yaml`, so later deploys from the same directory update the same agent; if that file points at a different instance than your current profile, the CLI asks before deploying. An agent deployed from local files has no repository behind it and no auto-sync.

`--repo` creates the agent from `github:user/repo` as a **deployment** — pull-only, tracking the branch, never pushing back. To give an agent a working branch on your own repo, use `trinity agents create … --template github:user/repo` instead. A private repo needs a GitHub token on the instance that can read it.

### Chat and Logs

```bash
# Send a message
trinity chat my-agent "Hello, what can you do?"

# View chat history
trinity history my-agent

# View container logs (default: last 50 lines)
trinity logs my-agent --tail 200
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

`--format` is an option on each listing or read command, not a global flag.

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

- Covered today: agents, deploy, chat and history, logs, health, skills, schedules (list and trigger), tags, profiles.
- Not yet in the CLI: credentials, events, executions, systems, subscriptions — use the [MCP server](../integrations/mcp-server.md) or the REST API for those.

## See Also

- [MCP Server](../integrations/mcp-server.md) — Programmatic access via MCP tools
- [Authentication](../api-reference/authentication.md) — API authentication patterns
