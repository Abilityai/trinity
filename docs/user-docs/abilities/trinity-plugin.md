# trinity Plugin

Connect, deploy, and sync agents to the Trinity platform. Three skills covering the complete deployment workflow.

## Installation

```bash
/plugin install trinity@abilityai
```

## Skills

| Skill | Description |
|-------|-------------|
| `/trinity:connect` | One-time: authenticate and configure the MCP connection |
| `/trinity:onboard` | Per-agent: compatibility check, file creation, deploy |
| `/trinity:sync` | Ongoing: sync changes between local and the running remote agent |

## How It Works

### Step 1: Connect (One-Time)

```bash
/trinity:connect
```

This:

1. Prompts for your Trinity instance URL
2. Authenticates via email verification
3. Provisions an MCP API key
4. Configures `.mcp.json` for Trinity MCP tools

After connecting, Trinity MCP tools become available:

- `mcp__trinity__list_agents`
- `mcp__trinity__chat_with_agent`
- `mcp__trinity__deploy_local_agent`

### Step 2: Onboard (Per Agent)

```bash
/trinity:onboard
```

For each agent you want to deploy:

1. **Compatibility check** — Verifies required files exist
2. **File creation** — Generates missing Trinity files
3. **Deploy** — Pushes the agent to the Trinity platform

Required files (created if missing):

- `template.yaml` — Agent metadata
- `.env.example` — Environment variable documentation
- `.mcp.json.template` — MCP server configuration

### Step 3: Sync (Ongoing)

```bash
/trinity:sync
```

After making local changes:

- Detects modified files
- Pushes updates to the remote agent
- Optionally restarts the agent

## Alternative: Trinity CLI

You can also deploy via the command line:

```bash
# Install CLI
pip install trinity-cli

# Initialize (one-time)
trinity init

# Deploy agent
trinity deploy .
```

See [Trinity CLI](../cli/trinity-cli.md) for details.

## Compatibility Requirements

Agents must have:

| File | Purpose |
|------|---------|
| `CLAUDE.md` | Agent identity and instructions |
| `template.yaml` | Trinity metadata (name, description, type) |
| `.env.example` | Documents required environment variables |

Optional but recommended:

- `dashboard.yaml` — Custom metrics dashboard
- `.mcp.json.template` — MCP server configuration

## See Also

- [Trinity CLI](../cli/trinity-cli.md) — Command-line deployment
- [Creating Agents](../agents/creating-agents.md) — Agent creation in Trinity
- [Abilities Overview](overview.md) — Full toolkit overview
