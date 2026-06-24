# Trinity CLI

Command-line interface for the Trinity Autonomous Agent Orchestration Platform.

## Installation

```bash
pip install trinity-cli                 # from PyPI (recommended)
# or: brew install abilityai/tap/trinity-cli
```

From a source checkout (development / unreleased changes), install editable instead:

```bash
pip install -e src/cli/
```

## Quick Start

```bash
# Set up and authenticate (first time)
trinity init

# Or configure manually
trinity login --instance https://your-instance.example.com

# Check status
trinity status

# List your agents
trinity agents list --format table

# Chat with an agent
trinity chat my-agent "What is the status of the project?"
```

## Authentication

The CLI supports two auth methods:

### Email Login (interactive)

```bash
# Full onboarding flow (request access + login)
trinity init

# Login to a configured instance
trinity login
```

The `init` command:
1. Prompts for instance URL
2. Prompts for email
3. Auto-registers you on the instance
4. Sends a verification code to your email
5. Stores the JWT token in `~/.trinity/config.json`

### API Key (non-interactive)

```bash
# Via environment variables
export TRINITY_URL=https://your-instance.example.com
export TRINITY_API_KEY=trinity_mcp_...
trinity agents list

# Or inline
TRINITY_API_KEY=trinity_mcp_... trinity agents list
```

Environment variables take precedence over the config file.

## Commands

### Auth & Config

| Command | Description |
|---------|-------------|
| `trinity init` | Configure instance, request access, and log in |
| `trinity login` | Log in with email verification |
| `trinity logout` | Clear stored credentials |
| `trinity status` | Show instance, user, and connection status |

### Deploy

| Command | Description |
|---------|-------------|
| `trinity deploy .` | Deploy current directory as an agent |
| `trinity deploy ./path` | Deploy a specific directory |
| `trinity deploy . --name bot` | Override agent name |
| `trinity deploy --repo user/repo` | Deploy from a GitHub repo |

On first deploy, writes `.trinity-remote.yaml` for tracking. Subsequent deploys update the same agent.

### Agents

| Command | Description |
|---------|-------------|
| `trinity agents list` | List all agents |
| `trinity agents get <name>` | Get agent details |
| `trinity agents create <name>` | Create a new agent |
| `trinity agents delete <name>` | Delete an agent (with confirmation) |
| `trinity agents start <name>` | Start an agent container |
| `trinity agents stop <name>` | Stop an agent container |
| `trinity agents rename <old> <new>` | Rename an agent |

### Chat & Logs

| Command | Description |
|---------|-------------|
| `trinity chat <agent> "<message>"` | Send a message to an agent |
| `trinity history <agent>` | Get chat history |
| `trinity logs <agent> [--tail N]` | View container logs |

### Health

| Command | Description |
|---------|-------------|
| `trinity health fleet` | Fleet-wide health status |
| `trinity health agent <name>` | Health status for one agent |

### Skills

| Command | Description |
|---------|-------------|
| `trinity skills list` | List available skills |
| `trinity skills get <name>` | Get skill details |

### Schedules

| Command | Description |
|---------|-------------|
| `trinity schedules list <agent>` | List agent schedules |
| `trinity schedules trigger <agent> <id>` | Trigger a schedule now |

### Tags

| Command | Description |
|---------|-------------|
| `trinity tags list` | List all tags |
| `trinity tags get <agent>` | Get tags for an agent |

## Output Formats

All commands support `--format table` (default) and `--format json`:

```bash
# Table (default) — human-readable
trinity agents list

# JSON — for piping and scripts
trinity agents list --format json
trinity agents list --format json | jq '.[].name'
```

## Configuration

Config is stored in `~/.trinity/config.json`:

```json
{
  "instance_url": "https://your-instance.example.com",
  "token": "eyJ...",
  "user": {
    "email": "you@example.com",
    "role": "user"
  }
}
```

The file is created with `0600` permissions (owner read/write only).

## Extending

The CLI is designed to be extended. Each command group is a separate module in `src/cli/trinity_cli/commands/`. To add a new command group:

1. Create `src/cli/trinity_cli/commands/mygroup.py`
2. Define a `@click.group()` with subcommands
3. Register it in `src/cli/trinity_cli/main.py`
