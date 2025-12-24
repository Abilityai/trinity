# Trinity Compatible Agent Guide

> **Comprehensive guide** for creating, deploying, and managing Trinity-compatible agents.
>
> This document covers everything from basic template structure to advanced features like task planning, inter-agent collaboration, and persistent memory.

---

## Table of Contents

1. [Overview](#overview)
2. [The Four Pillars of Deep Agency](#the-four-pillars-of-deep-agency)
3. [Required Files](#required-files)
4. [Directory Structure](#directory-structure)
5. [template.yaml Schema](#templateyaml-schema)
6. [CLAUDE.md Requirements](#claudemd-requirements)
7. [Credential Management](#credential-management)
8. [Platform Injection](#platform-injection)
9. [Task Planning System](#task-planning-system)
10. [Inter-Agent Collaboration](#inter-agent-collaboration)
11. [Shared Folders](#shared-folders)
12. [Custom Metrics](#custom-metrics)
13. [Memory Management](#memory-management)
14. [Testing Locally](#testing-locally)
15. [Compatibility Checklist](#compatibility-checklist)
16. [Migration Guide](#migration-guide)
17. [Troubleshooting](#troubleshooting)
18. [Best Practices](#best-practices)

---

## Overview

Trinity deploys agents from GitHub repositories or local directories. The platform reads template metadata, extracts credential requirements, injects secrets and platform capabilities at runtime, and starts the agent container.

**What makes an agent "Trinity-compatible"?**

- Follows the required file structure (`template.yaml`, `CLAUDE.md`, etc.)
- Uses placeholder syntax for credentials (`${VAR}` in `.mcp.json.template`)
- Keeps domain-specific logic in agent, lets platform handle orchestration
- Never commits secrets to the repository

---

## The Four Pillars of Deep Agency

Trinity implements infrastructure for **System 2 AI** — Deep Agents that plan, reason, and execute autonomously.

| Pillar | Description | Agent Responsibility | Platform Responsibility |
|--------|-------------|---------------------|------------------------|
| **I. Explicit Planning** | Task DAGs persisting outside context window | Execute plans following injected instructions | **Inject planning prompt**, store DAGs, visualize |
| **II. Hierarchical Delegation** | Orchestrator-Worker with context quarantine | Call other agents via MCP | Route messages, enforce access control |
| **III. Persistent Memory** | Virtual filesystems, memory management | Manage own memory files | GitHub sync, file browser, storage |
| **IV. Extreme Context Engineering** | High-Order Prompts defining reasoning | Domain-specific CLAUDE.md | **Inject Trinity Meta-Prompt**, credential injection |

**Key Insight**: Agents focus on **domain expertise**. Trinity handles **orchestration infrastructure**.

---

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
```

See [template.yaml Schema](#templateyaml-schema) for complete field reference.

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

See [CLAUDE.md Requirements](#claudemd-requirements) for guidelines.

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

### 5. `.gitignore` (Required)

Must exclude secrets and platform-managed directories:

```gitignore
# Credentials - NEVER COMMIT
.mcp.json
.env
*.pem
*.key
credentials.json

# Platform-managed directories - DO NOT COMMIT
.trinity/
.claude/commands/trinity/

# Claude Code session data
.claude/projects/
.claude/statsig/

# Temporary files
*.log
*.tmp
.DS_Store

# Local overrides
*.local.md
*.local.json
!.env.example
!.mcp.json.template
```

---

## Directory Structure

Every Trinity-compatible agent follows this structure:

```
my-agent/
├── .git/
├── .gitignore                     # CRITICAL: excludes secrets
│
├── CLAUDE.md                      # Agent instructions (Trinity prompt prepended at runtime)
├── README.md                      # Human documentation
├── template.yaml                  # Trinity metadata + credential schema
│
├── .trinity/                      # PLATFORM-MANAGED (injected at startup)
│   ├── prompt.md                  # Trinity Meta-Prompt (injected)
│   └── version.json               # Injection version tracking
│
├── .claude/
│   ├── agents/                    # Agent's own sub-agents
│   ├── commands/
│   │   ├── trinity/               # PLATFORM-INJECTED commands
│   │   │   ├── trinity-plan-create.md
│   │   │   ├── trinity-plan-status.md
│   │   │   ├── trinity-plan-update.md
│   │   │   └── trinity-plan-list.md
│   │   └── [agent's own commands] # Agent-specific commands (optional)
│   ├── skills/                    # Skills
│   └── settings.local.json        # Claude Code settings
│
├── .mcp.json.template             # MCP config with ${VAR} placeholders
├── .env.example                   # Documents required credentials
│
├── memory/                        # Agent's persistent state (COMMITTED)
│   ├── context.md                 # Learned context
│   ├── preferences.json           # User preferences
│   └── session_notes/             # Per-session notes
│
├── plans/                         # Task DAGs (managed via platform commands)
│   ├── active/                    # Currently executing plans
│   │   └── {plan-id}.yaml         # Active plan with task states
│   └── archive/                   # Finished plans
│
├── outputs/                       # Generated content (COMMITTED)
│   ├── reports/
│   ├── content/
│   └── exports/
│
├── scripts/                       # Helper scripts
└── resources/                     # Static resources
```

**Key Distinction:**
- `.trinity/` and `.claude/commands/trinity/` = **Platform-managed** (injected, updated by Trinity)
- Everything else = **Agent-managed** (versioned in agent's repo)

---

## template.yaml Schema

Complete schema with all available fields:

```yaml
# === REQUIRED FIELDS ===
name: my-agent                    # Unique identifier (lowercase, hyphens ok)
display_name: "My Agent"          # Human-readable name for UI
description: |
  Multi-line description of what this agent does.

# Resource limits (required)
resources:
  cpu: "2"                        # CPU cores (string)
  memory: "4g"                    # Memory limit (e.g., "2g", "4g", "8g")

# === CREDENTIAL SCHEMA ===
# Trinity uses this to inject secrets
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

# === OPTIONAL METADATA ===
version: "1.0"
author: "Your Name"
updated: "2025-01-15"
tagline: "Short one-liner for dashboard cards"

# Example prompts (shown in Info tab as "What You Can Ask")
use_cases:
  - "Do something useful"
  - "Ask about [topic]"

# === CAPABILITIES & FEATURES ===

# Capabilities (shown as chips in UI)
capabilities:
  - feature_one
  - feature_two

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

# Supported platforms (if applicable)
platforms:
  - trinity
  - local

# === GIT CONFIGURATION ===
git:
  push_enabled: true
  commit_paths:
    - memory/
    - plans/
    - outputs/
    - CLAUDE.md
  ignore_paths:
    - .mcp.json
    - .env
    - "*.log"

# === TASK PLANNING ===
planning:
  enabled: true
  max_active_plans: 5
  default_task_timeout_minutes: 30
  auto_checkpoint: true           # Auto-commit on plan completion

# === SHARED FOLDERS ===
# File-based collaboration between agents
# These are DEFAULT values when agent is created from template
shared_folders:
  expose: false                   # Expose /home/developer/shared-out
  consume: false                  # Mount shared folders from permitted agents

# === CUSTOM METRICS ===
# Agent-specific KPIs displayed in Trinity UI
metrics:
  - name: metric_name             # Required: internal identifier (snake_case)
    type: counter                 # Required: counter|gauge|percentage|status|duration|bytes
    label: "Display Label"        # Required: shown in UI
    description: "What this tracks"  # Optional: tooltip
    icon: "chart"                 # Optional: heroicons name
    unit: "items"                 # Optional: unit label
    warning_threshold: 80         # Optional (percentage): yellow if below
    critical_threshold: 50        # Optional (percentage): red if below
    values:                       # Required for status type only
      - value: "active"
        color: "green"
        label: "Active"
      - value: "idle"
        color: "gray"
        label: "Idle"
```

---

## CLAUDE.md Requirements

Every Trinity-compatible agent MUST have a CLAUDE.md with **domain-specific** content only. Planning and platform instructions are injected by Trinity.

### Recommended Structure

```markdown
# Agent Name

## Identity
Who this agent is and what it does.
Your role, expertise, and personality.

## Domain Expertise
What you specialize in. Your knowledge areas.

## Available Tools
What MCP servers and integrations you have access to.
(Trinity MCP is injected automatically - don't list it here)

## Workflows
Domain-specific processes you follow.
How you approach tasks in your specialty.

## Constraints
- Domain-specific rules
- Safety constraints for your area
- Things you should NOT do

## Memory Structure
How you organize your memory/ directory.
What goes in session_notes/, context.md, etc.
```

### What NOT to Include

These are injected by the platform - don't add them to your CLAUDE.md:

- Planning instructions (`/trinity-plan-*` commands)
- Collaboration protocols
- Git/commit instructions
- Platform constraints
- Trinity MCP documentation

---

## Credential Management

### Credential Injection Flow

When Trinity deploys your agent:

1. **Reads `template.yaml`** - gets credential schema
2. **Parses `.mcp.json.template`** - finds `${VAR}` placeholders
3. **Fetches credentials** - from Trinity's secure credential store
4. **Generates `.env`** - writes all credentials as KEY=VALUE pairs
5. **Generates `.mcp.json`** - replaces placeholders with real values
6. **Starts container** - with your repo as the working directory

### Hot-Reload

Credentials can be updated without restarting the agent:
- Via UI: Credentials tab → paste new values
- Via MCP: `reload_credentials` tool

### Your Agent Should

- Read MCP credentials from `.mcp.json` (Claude Code does this automatically)
- Read script credentials from `.env` or environment variables
- Never store credentials in committed files

---

## Platform Injection

Trinity controls agent behavior through **runtime injection**. This ensures:

- Consistent planning behavior across all agents
- Platform can update logic without touching agent repos
- Agents focus on domain expertise, platform handles orchestration

### What Gets Injected

| Component | Location | Purpose |
|-----------|----------|---------|
| **Trinity Meta-Prompt** | `.trinity/prompt.md` + CLAUDE.md section | Collaboration protocols |
| **Trinity MCP Config** | `.mcp.json` (merged) | Agent-to-agent collaboration tools |
| **Credentials** | `.env`, `.mcp.json` | API keys, tokens |

### Injection Timing

**On Agent Start:**
1. Trinity Meta-Prompt injected/updated in `.trinity/` and CLAUDE.md
2. Trinity MCP server injected into `.mcp.json`
3. Credentials injected into `.env` and `.mcp.json`

### CLAUDE.md After Injection

```markdown
<!-- TRINITY PLATFORM - AUTO-INJECTED -->
## Trinity Planning System

You are running on the **Trinity Deep Agent Platform**...

[Planning instructions, commands reference, constraints]

<!-- END TRINITY PLATFORM -->

## Custom Instructions
[Admin-configured system-wide instructions, if any]

# Agent Name
[Your agent's own content starts here...]
```

---

## Task Planning System

Deep Agents need plans that persist **outside the context window**:

- Context resets don't lose progress
- Multiple agents can collaborate on same plan
- Human oversight of agent reasoning
- Recovery from failures with plan state intact

### Plan File Format

Plans are stored as YAML in `plans/active/{plan-id}.yaml`:

```yaml
id: "plan-abc123"
name: "Research and write blog post"
created_at: "2025-12-05T10:00:00Z"
created_by: "agent-ruby"
status: "active"  # active | completed | failed | paused

goal: |
  Research the topic of AI agents and produce a 1500-word blog post.

tasks:
  - id: "task-001"
    name: "Research topic"
    status: "completed"
    started_at: "2025-12-05T10:01:00Z"
    completed_at: "2025-12-05T10:15:00Z"
    result_summary: "Found 12 relevant sources"
    depends_on: []

  - id: "task-002"
    name: "Create outline"
    status: "completed"
    depends_on: ["task-001"]

  - id: "task-003"
    name: "Write draft"
    status: "active"
    depends_on: ["task-002"]
    assigned_to: "agent-ruby"

  - id: "task-004"
    name: "Generate social snippets"
    status: "pending"
    depends_on: ["task-003"]

  - id: "task-005"
    name: "Review and publish"
    status: "blocked"
    depends_on: ["task-003", "task-004"]
    blocked_reason: "Waiting for draft completion"

metadata:
  total_tasks: 5
  completed_tasks: 2
```

### Task State Machine

```
                    ┌──────────────────────────────────────┐
                    │                                      │
                    ▼                                      │
┌─────────┐    ┌─────────┐    ┌───────────┐    ┌─────────┴─┐
│ pending │───►│ active  │───►│ completed │    │  failed   │
└─────────┘    └─────────┘    └───────────┘    └───────────┘
     │              │                               ▲
     │              │                               │
     ▼              └───────────────────────────────┘
┌─────────┐
│ blocked │  (waiting on dependencies)
└─────────┘
```

### Platform-Injected Commands

These commands are automatically available after Trinity injection:

| Command | Purpose |
|---------|---------|
| `/trinity-plan-create` | Create a new task plan |
| `/trinity-plan-status` | View all active plans |
| `/trinity-plan-update` | Update task status |
| `/trinity-plan-list` | List all plans |

---

## Inter-Agent Collaboration

### MCP Tools for Collaboration

Agents use Trinity MCP for collaboration:

```
mcp__trinity__chat_with_agent(
    agent_name="cornelius",
    message="Research: {topic}"
)
```

### Access Control

Permission checks before any agent-to-agent communication:

- **Same owner**: Allowed
- **Explicit permission granted**: Allowed (via Permissions tab)
- **Admin**: Allowed (bypass)
- **Otherwise**: Denied with error

### Delegation Pattern

When an agent needs help from another agent, it can create a delegation task:

```yaml
tasks:
  - id: "task-003"
    name: "Get research from Cornelius"
    status: "active"
    type: "delegation"
    delegate_to: "agent-cornelius"
    delegate_message: "Research AI agent architectures"
```

---

## Shared Folders

Trinity enables file-based collaboration between agents via shared Docker volumes.

### Folder Paths

| Path | Purpose |
|------|---------|
| `/home/developer/shared-out` | **Your agent's shared folder** - accessible to permitted agents |
| `/home/developer/shared-in/{agent-name}` | **Other agents' folders** - read/write access |

### How It Works

1. **Expose**: When enabled, creates a Docker volume (`agent-{name}-shared`) mounted at `/home/developer/shared-out`. Other permitted agents can mount this.

2. **Consume**: When enabled, mounts shared folders of all agents you have permission to call at `/home/developer/shared-in/{agent-name}`.

3. **Permissions**: Access follows the Agent Permissions system. Agent B can only mount Agent A's folder if:
   - Agent A has expose enabled
   - Agent B has consume enabled
   - Agent B has permission to call Agent A

### Template Configuration

Set defaults in `template.yaml`:

```yaml
shared_folders:
  expose: true    # Expose /home/developer/shared-out
  consume: true   # Mount shared folders from permitted agents
```

### Example Usage

**Agent A** (exposing):
```bash
echo "Data from Agent A" > /home/developer/shared-out/report.txt
```

**Agent B** (consuming, with permission to call Agent A):
```bash
cat /home/developer/shared-in/agent-a/report.txt
```

**Note**: Changes to shared folder config require an agent restart.

---

## Custom Metrics

Agents can define custom metrics displayed in the Trinity UI.

### Metric Types

| Type | Description | Example |
|------|-------------|---------|
| `counter` | Monotonically increasing | Posts published: 42 |
| `gauge` | Value that can go up/down | Active tasks: 3 |
| `percentage` | 0-100 with progress bar | Goal progress: 75% |
| `status` | Enum/state value | Status: Active |
| `duration` | Time in seconds | Uptime: 2h 15m |
| `bytes` | Size in bytes | Memory: 1.2 MB |

### How It Works

1. Define metrics in `template.yaml` under `metrics:`
2. Agent writes values to `workspace/metrics.json`
3. Trinity UI displays metrics in the Metrics tab

### metrics.json Format

```json
{
  "posts_published": 42,
  "goal_progress": 75,
  "current_status": "active"
}
```

---

## Memory Management

Agents manage their own memory. Trinity provides storage, not strategy.

### Recommended Structure

```
memory/
├── context.md           # Long-term learned context
├── preferences.json     # User preferences
├── session_notes/       # Per-session working notes
│   └── 2025-12-05.md
└── summaries/           # Compressed old context
    └── 2025-11.md       # Monthly summary
```

### Memory Folding (Agent-Level)

Agents should periodically compress old context:

1. At session end, review `session_notes/`
2. Extract key learnings → append to `context.md`
3. Archive verbose notes to `summaries/`
4. Commit via Git sync

---

## Testing Locally

You can test your template locally before deploying to Trinity:

```bash
# 1. Create .env with real credentials
cp .env.example .env
# Edit .env with actual values

# 2. Generate .mcp.json from template
cat .mcp.json.template | envsubst > .mcp.json

# 3. Run Claude Code
claude
```

### Local Init Script

Create an `init.sh` for local development setup:

```bash
#!/bin/bash
# init.sh - Local development setup

# Check for required env vars
if [ ! -f .env ]; then
    echo "Please create .env from .env.example"
    exit 1
fi

# Generate MCP config
cat .mcp.json.template | envsubst > .mcp.json

# Create directories
mkdir -p memory plans/active plans/archive outputs

echo "Agent ready for local development"
```

---

## Compatibility Checklist

An agent is Trinity-compatible if:

### Required Files
- [ ] Has `template.yaml` with required fields (name, display_name, description, version, resources)
- [ ] Has `CLAUDE.md` with identity and domain-specific instructions
- [ ] Has `.mcp.json.template` with `${VAR}` placeholders (if using MCP servers)
- [ ] Has `.env.example` documenting required credentials
- [ ] Has `.gitignore` excluding secrets AND platform-managed directories

### Directory Structure
- [ ] Has `memory/` directory for persistent state
- [ ] Has `plans/` directory (created by platform if missing)
- [ ] Does NOT have `.trinity/` in repo (platform creates this)
- [ ] Does NOT have `.claude/commands/trinity/` in repo (platform injects this)

### Security
- [ ] No credentials stored in repository
- [ ] `.mcp.json` and `.env` are gitignored
- [ ] Sensitive files excluded from git sync paths

### Behavior
- [ ] Agent CLAUDE.md does NOT include planning instructions (platform injects them)
- [ ] Agent does NOT define `/trinity-*` commands (platform injects them)
- [ ] Can run both locally (with init.sh) and on Trinity platform

---

## Migration Guide

To convert an existing agent to Trinity-compatible:

1. **Create repository structure** matching the directory layout above
2. **Extract CLAUDE.md** from current instructions (domain-specific only)
3. **Create template.yaml** with metadata and credentials
4. **Create .mcp.json.template** from current .mcp.json (replace values with ${VAR})
5. **Create .env.example** listing all required variables
6. **Move memory files** to `memory/` directory
7. **Add .gitignore** excluding secrets and platform directories
8. **Test locally** with `init.sh`
9. **Deploy to Trinity** and verify

---

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

### Planning commands not available
- Check that `.trinity/` and `.claude/commands/trinity/` exist in the container
- Verify Trinity injection completed (check agent logs)
- Try restarting the agent

### Agent-to-agent chat fails
- Verify permissions are granted (Permissions tab)
- Check both agents are running
- Verify agent names are correct (lowercase, hyphens)

---

## Best Practices

### Security
- Never commit secrets to the repository
- Use `.env.example` with placeholder values, not real credentials
- Add `.env` and `.mcp.json` to `.gitignore`
- Review git diffs before committing

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
- [ ] Agent works locally with `init.sh`

### Domain Focus
- Keep CLAUDE.md focused on your agent's specialty
- Let Trinity handle planning, collaboration, and infrastructure
- Use memory/ for domain-specific persistent state
- Use outputs/ for generated content

---

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

Trinity needs a GitHub PAT with read access to clone private repos.

### Local Templates
Place directory in `config/agent-templates/` on the Trinity server. It will auto-appear in the template list.

---

## Multi-Agent Systems

This guide covers **single-agent template development**. If you're building a **multi-agent system** where multiple agents collaborate:

### Deployment Options

**Option 1: System Manifest (Recommended)**

Deploy multiple coordinated agents from a single YAML manifest:

```yaml
name: content-production
description: Autonomous content pipeline

agents:
  orchestrator:
    template: github:YourOrg/content-orchestrator
    folders:
      expose: true
      consume: true
    schedules:
      - name: daily-planning
        cron: "0 9 * * *"
        message: "Plan today's tasks"

  ruby:
    template: github:YourOrg/ruby-content
    folders:
      expose: true
      consume: true

permissions:
  preset: full-mesh  # or orchestrator-workers, none, explicit
```

Deploy via API or MCP:
```bash
POST /api/systems/deploy
{"manifest": "...YAML content..."}
```

See **[Multi-Agent System Guide](MULTI_AGENT_SYSTEM_GUIDE.md)** for complete System Manifest documentation.

**Option 2: Manual Deployment**

Create agents individually, then configure permissions, folders, and schedules via API. See [Multi-Agent System Guide - Manual Deployment](MULTI_AGENT_SYSTEM_GUIDE.md#manual-deployment-workflow-alternative) for step-by-step instructions.

### Design Considerations for Multi-Agent Systems

When building agents intended for multi-agent systems:

1. **Design for Coordination**
   - Plan how your agent will communicate with others (MCP chat, shared folders)
   - Document expected file formats in shared-out/ directory
   - Define clear responsibilities (what this agent does vs. what others do)

2. **Shared Folder Conventions**
   - Use structured file formats (JSON, YAML)
   - Include timestamps in shared files
   - Document file contracts in README.md

3. **System-Wide Prompts**
   - If deploying via manifest with `prompt:` field, all agents receive the same global instructions
   - Keep agent-specific CLAUDE.md focused on the agent's domain expertise
   - Global prompts are useful for system-wide conventions (file formats, communication protocols)

4. **Template Defaults**
   - Set sensible defaults in `template.yaml` for `shared_folders:` if your agent is designed for collaboration
   - Don't enable by default if the agent works standalone

See **[Multi-Agent System Guide](MULTI_AGENT_SYSTEM_GUIDE.md)** for comprehensive multi-agent architecture patterns, communication strategies, and deployment workflows.

---

## Revision History

| Date | Changes |
|------|---------|
| 2025-12-24 | Removed Chroma MCP integration - templates should include their own vector memory if needed |
| 2025-12-18 | Added Multi-Agent Systems section with System Manifest deployment reference |
| 2025-12-14 | Consolidated from AGENT_TEMPLATE_SPEC.md and trinity-compatible-agent.md |
| 2025-12-13 | Added shared folders |
| 2025-12-10 | Added custom metrics specification |
| 2025-12-05 | Added Task DAG system (Pillar I) |

---

*This document is the single source of truth for Trinity-compatible agent development.*
