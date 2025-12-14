# Agent Template Specification

This document defines the requirements for a GitHub repository to be deployable as a Trinity agent.

## Overview

Trinity deploys agents from GitHub repositories (or local directories). The platform reads template metadata, extracts credential requirements, injects secrets at runtime, and starts the agent container. Your repository must follow this specification for Trinity to deploy it correctly.

## Required Files

### 1. `template.yaml` (Required)

Metadata file that Trinity reads to understand your agent.

```yaml
# Required fields
name: my-agent                    # Unique identifier (lowercase, hyphens ok)
display_name: "My Agent"          # Human-readable name for UI
description: "What this agent does"

# Resource limits (required)
resources:
  cpu: "2"                        # CPU cores (string)
  memory: "4g"                    # Memory limit (e.g., "2g", "4g", "8g")

# Credential schema (required if agent needs credentials)
credentials:
  # MCP server credentials - extracted from .mcp.json.template
  mcp_servers:
    server-name:                  # Must match server name in .mcp.json.template
      env_vars:
        - API_KEY
        - API_SECRET

  # Environment variables for scripts/tools
  env_file:
    - OTHER_VAR
    - ANOTHER_VAR

# Optional metadata fields
version: "1.0"
author: "Your Name"
updated: "2025-01-15"
tagline: "Short one-liner for dashboard cards"

# Example prompts (shown in Info tab as "What You Can Ask")
use_cases:
  - "Do something useful"
  - "Ask about [topic]"

# Sub-agents with descriptions (displayed in Info tab)
sub_agents:
  - name: helper-agent
    description: "What this sub-agent does"
  - name: another-agent
    description: "Another sub-agent description"

# Slash commands with descriptions
commands:
  - name: my-command
    description: "What this command does"
  - name: another-command
    description: "Another command description"

# MCP servers with descriptions (for Info tab display)
mcp_servers:
  - name: server-name
    description: "What this MCP server provides"

# Skills with descriptions
skills:
  - name: my-skill
    description: "What this skill enables"

# Capabilities (shown as chips)
capabilities:
  - feature_one
  - feature_two

# Supported platforms (if applicable)
platforms:
  - platform1
  - platform2

# Shared folders - file-based collaboration between agents (Req 9.11)
# These are DEFAULT values when agent is created from template
# Can be changed at runtime via UI or API
shared_folders:
  expose: false     # Expose /home/developer/shared-out to permitted agents
  consume: false    # Mount shared folders from agents this one has permission to call

# Custom metrics - agent-specific KPIs displayed in Trinity UI
# See docs/AGENT_CUSTOM_METRICS_SPEC.md for full specification
metrics:
  - name: metric_name            # Required: internal identifier (snake_case)
    type: counter                # Required: counter|gauge|percentage|status|duration|bytes
    label: "Display Label"       # Required: shown in UI
    description: "What this metric tracks"  # Optional: tooltip
    icon: "chart"               # Optional: heroicons name
    unit: "items"               # Optional: unit label (e.g., "sec", "MB", "items")
    warning_threshold: 80       # Optional (percentage only): yellow if below
    critical_threshold: 50      # Optional (percentage only): red if below
    values:                     # Required for status type only
      - value: "active"
        color: "green"
        label: "Active"
      - value: "idle"
        color: "gray"
        label: "Idle"
```

### 2. `CLAUDE.md` (Required)

Agent instructions that Claude Code reads. This is your agent's "brain" - it defines behavior, workflows, available tools, and constraints.

```markdown
# Agent Name

## Purpose
What this agent does...

## Available Tools
- Tool 1: description
- Tool 2: description

## Workflows
How the agent should approach tasks...

## Constraints
What the agent should NOT do...
```

### 3. `.mcp.json.template` (Required if using MCP servers)

MCP server configuration with credential placeholders. Trinity replaces `${VAR}` with actual values from the credential store.

```json
{
  "mcpServers": {
    "server-name": {
      "command": "npx",
      "args": ["-y", "@org/mcp-server"],
      "env": {
        "API_KEY": "${API_KEY}",
        "API_SECRET": "${API_SECRET}"
      }
    }
  }
}
```

**Important:**
- Use `${VAR_NAME}` syntax for credential placeholders
- Never commit actual secrets
- Server names must match `credentials.mcp_servers` keys in `template.yaml`

### 4. `.env.example` (Recommended)

Documents all required environment variables. Helps users understand what credentials are needed.

```bash
# MCP Server Credentials
API_KEY=your-api-key-here
API_SECRET=your-api-secret-here

# Script/Tool Credentials
OTHER_VAR=value
```

## Optional Structure

### `.claude/` Directory

Claude Code configuration for sub-agents, commands, and skills.

```
.claude/
├── agents/                 # Sub-agent definitions
│   └── helper-agent.md
├── commands/               # Slash commands
│   └── my-command.md
├── skills/                 # Custom skills
│   └── my-skill/
│       ├── skill.md
│       └── scripts/
└── settings.local.json     # Claude Code settings
```

### `scripts/` Directory

Helper scripts your agent uses. These can reference environment variables from `.env`.

```
scripts/
├── setup.sh
└── helpers/
    └── process-data.py
```

### `memory/` Directory

Persistent state that survives agent restarts. Committed to git.

```
memory/
├── state.json
└── history.md
```

### `outputs/` Directory

Generated content. Can be committed or gitignored depending on your needs.

## Complete Example Structure

```
my-agent/
├── template.yaml              # Required: Trinity metadata
├── CLAUDE.md                  # Required: Agent instructions
├── .mcp.json.template         # Required if using MCP servers
├── .env.example               # Recommended: Documents credentials
├── .claude/
│   ├── agents/
│   │   └── sub-agent.md
│   ├── commands/
│   │   └── do-thing.md
│   └── settings.local.json
├── scripts/
│   └── helper.sh
├── memory/
│   └── state.json
├── outputs/                   # gitignored or committed
└── README.md                  # For humans browsing GitHub
```

## Shared Folders

Trinity enables file-based collaboration between agents via shared Docker volumes. This allows agents to exchange documents, data files, and artifacts without going through the chat interface.

### Folder Paths

| Path | Purpose |
|------|---------|
| `/home/developer/shared-out` | **Your agent's shared folder** - files here are accessible to permitted agents |
| `/home/developer/shared-in/{agent-name}` | **Other agents' shared folders** - read/write access to permitted agents' folders |

### How It Works

1. **Expose**: When enabled, your agent gets a `/home/developer/shared-out` folder backed by a Docker volume (`agent-{name}-shared`). Other agents that have permission to call your agent can mount this folder.

2. **Consume**: When enabled, your agent mounts the shared folders of all agents it has permission to call at `/home/developer/shared-in/{agent-name}`.

3. **Permissions**: Shared folder access follows the Agent Permissions system. Agent B can only mount Agent A's folder if:
   - Agent A has expose enabled
   - Agent B has consume enabled
   - Agent B has permission to call Agent A (granted via Permissions tab)

### Template Configuration

Set defaults in `template.yaml`:

```yaml
shared_folders:
  expose: true    # Expose /home/developer/shared-out to permitted agents
  consume: true   # Mount shared folders from agents this one has permission to call
```

These are **default values** when the agent is created. Users can change them at runtime via the UI (Shared Folders tab) or API.

### Example Usage

**Agent A** (exposing):
```bash
# Write a file for other agents
echo "Data from Agent A" > /home/developer/shared-out/report.txt
```

**Agent B** (consuming, with permission to call Agent A):
```bash
# Read Agent A's shared file
cat /home/developer/shared-in/agent-a/report.txt

# Write back (shared folders are read-write)
echo "Processed by Agent B" >> /home/developer/shared-in/agent-a/report.txt
```

### Important Notes

- **Restart required**: Changes to shared folder config require an agent restart to take effect
- **Read-write access**: All mounts are currently read-write; consumers can modify files
- **Persistence**: Shared volumes persist across agent restarts
- **Volume naming**: Volumes are named `agent-{name}-shared` and labeled with `trinity.platform=agent-shared`

---

## Credential Injection Flow

When Trinity deploys your agent:

1. **Reads `template.yaml`** - gets credential schema
2. **Parses `.mcp.json.template`** - finds `${VAR}` placeholders
3. **Fetches credentials** - from Trinity's secure credential store
4. **Generates `.env`** - writes all credentials as KEY=VALUE pairs
5. **Generates `.mcp.json`** - replaces placeholders with real values
6. **Starts container** - with your repo as the working directory

Your agent code should:
- Read MCP credentials from `.mcp.json` (Claude Code does this automatically)
- Read script credentials from `.env` or environment variables

## Best Practices

### Security
- Never commit secrets to the repository
- Use `.env.example` with placeholder values, not real credentials
- Add `.env` and `.mcp.json` to `.gitignore`

### Credential Naming
Use descriptive names that indicate the service:
```
TWITTER_API_KEY          # Good
CLOUDINARY_API_SECRET    # Good
API_KEY                  # Bad - too generic
KEY1                     # Bad - meaningless
```

### Template Validation
Before publishing, verify:
- [ ] `template.yaml` has all required fields
- [ ] All `${VAR}` placeholders in `.mcp.json.template` are listed in `credentials`
- [ ] `.env.example` documents all variables
- [ ] No secrets are committed anywhere

### Testing Locally
You can test your template locally before deploying to Trinity:

```bash
# Create .env with real credentials
cp .env.example .env
# Edit .env with actual values

# Generate .mcp.json from template
cat .mcp.json.template | envsubst > .mcp.json

# Run Claude Code
claude
```

## Registering with Trinity

### GitHub Templates
Use format `github:Org/repo-name` when creating an agent:

```bash
POST /api/agents
{
  "name": "my-agent",
  "template": "github:YourOrg/your-agent-repo"
}
```

Trinity needs a GitHub PAT with read access to clone private repos. Store it as a credential with service `github`.

### Local Templates
Place directory in `config/agent-templates/` on the Trinity server. It will auto-appear in the template list.

## Updating Deployed Agents

When you push changes to your GitHub repo:
1. Stop the agent in Trinity
2. Delete the agent
3. Create a new agent from the same template

Future: Trinity will support `git pull` to update running agents.

## Troubleshooting

### "Missing credentials" error
- Check that all `${VAR}` placeholders in `.mcp.json.template` are defined in `template.yaml`
- Verify credentials are configured in Trinity's credential store

### MCP server not starting
- Check `.mcp.json` was generated correctly (inspect container logs)
- Verify the MCP server package exists and is spelled correctly

### Agent can't find credentials
- Scripts should read from `.env` or environment variables
- MCP servers read from `.mcp.json` automatically
