# Trinity Compatible Agent Guide

> **Comprehensive guide** for creating and deploying Trinity-compatible agents.
>
> This document covers template structure, inter-agent collaboration, and best practices.

---

## Table of Contents

1. [Overview](#overview)
2. [Required Files](#required-files)
3. [Directory Structure](#directory-structure)
4. [template.yaml Schema](#templateyaml-schema)
5. [CLAUDE.md Requirements](#claudemd-requirements)
6. [Runtime Options](#runtime-options)
7. [Credential Management](#credential-management)
8. [Inter-Agent Collaboration](#inter-agent-collaboration)
9. [Shared Folders](#shared-folders)
10. [Platform Skills](#platform-skills)
11. [Custom Metrics](#custom-metrics)
12. [Agent Dashboard](#agent-dashboard)
13. [Memory Management](#memory-management)
14. [Content Folder Convention](#content-folder-convention)
15. [Package Persistence](#package-persistence)
16. [Compatibility Checklist](#compatibility-checklist)
17. [Migration Guide](#migration-guide)
18. [Best Practices](#best-practices)
19. [Autonomous Agent Design](#autonomous-agent-design)

---

## Overview

Trinity deploys agents from GitHub repositories or local directories. The platform reads template metadata, extracts credential requirements, injects secrets and platform capabilities at runtime, and starts the agent container.

**What makes an agent "Trinity-compatible"?**

- Follows the required file structure (`template.yaml`, `CLAUDE.md`, etc.)
- Uses placeholder syntax for credentials (`${VAR}` in `.mcp.json.template`)
- Keeps domain-specific logic in agent, lets platform handle orchestration
- Never commits secrets to the repository

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

Must exclude secrets, instance-specific files, and large content.

> **Source of truth**: this list mirrors `_GITIGNORE_PATTERNS` in
> `src/backend/services/git_service.py`. The Python constant is the
> source of truth — the platform appends every entry to the agent's
> `.gitignore` on init and again on every Push. A unit test
> (`tests/unit/test_github_init_gitignore.py::test_doc_and_constant_in_sync`)
> asserts the two stay in sync.

```gitignore
# Shell init / history (instance-specific)
.bash_logout
.bashrc
.profile
.bash_history
.sudo_as_admin_successful

# Credentials - NEVER COMMIT
.env
.env.*
.mcp.json
credentials.json
*.pem
*.key

# Instance-specific directories - DO NOT COMMIT
.cache/
.local/
.npm/
.ssh/
.trinity/
.tmp/

# Large generated content - DO NOT COMMIT
content/

# Claude Code - commit commands/skills/agents, exclude runtime data
.claude.json
.claude.json.backup
.claude/projects/
.claude/statsig/
.claude/todos/
.claude/debug/
.claude/sessions/
.claude/shell-snapshots/
# Keep: .claude/commands/, .claude/skills/, .claude/agents/, settings.local.json

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

**What to commit from `.claude/`:**
- ✅ `.claude/commands/` - Slash commands
- ✅ `.claude/skills/` - Skills (seeded to platform library on first deploy)
- ✅ `.claude/agents/` - Sub-agents
- ✅ `.claude/settings.local.json` - Claude Code settings

**What NOT to commit from `.claude/`:**
- ❌ `.claude/projects/` - Session data
- ❌ `.claude/statsig/` - Analytics
- ❌ `.claude/todos/` - Temporary todo lists
- ❌ `.claude/debug/` - Debug logs
- ❌ `.claude/sessions/` - Per-session state
- ❌ `.claude/shell-snapshots/` - Shell environment snapshots

**Note on Skills**: Skills in templates are seeded to the **Platform Skills Library** on first deployment, then managed centrally. See [Platform Skills](#platform-skills).

---

## Directory Structure

Every Trinity-compatible agent follows this structure:

```
my-agent/
├── .git/
├── .gitignore                     # CRITICAL: excludes secrets
│
├── CLAUDE.md                      # Agent instructions
├── README.md                      # Human documentation
├── template.yaml                  # Trinity metadata + credential schema
│
├── .claude/
│   ├── agents/                    # Agent's own sub-agents (optional)
│   ├── commands/                  # Slash commands (optional)
│   ├── skills/                    # Symlinks to assigned platform skills (auto-managed)
│   ├── skills-library/            # Read-only mount of all platform skills
│   └── settings.local.json        # Claude Code settings
│
├── .mcp.json.template             # MCP config with ${VAR} placeholders
├── .env.example                   # Documents required credentials
│
├── docs/                          # Agent documentation (recommended)
│   └── ...
│
├── outputs/                       # Generated content (COMMITTED)
│   ├── reports/
│   └── data/
│
├── content/                       # Large generated assets (NOT COMMITTED)
│   ├── videos/                    # Generated video files
│   ├── audio/                     # Generated audio files
│   ├── images/                    # Generated images
│   └── exports/                   # Data exports, large files
│
├── data/                          # Runtime data — SQLite DBs, datasets (NOT COMMITTED; see data_paths)
│
├── scripts/                       # Helper scripts (optional)
└── resources/                     # Static resources (optional)
```

---

## Runtime Data (`data_paths`) — #1169

If your agent accumulates **runtime data** — a SQLite database, a scraped dataset, an embeddings index — put it under **`data/`** (i.e. `/home/developer/data/...` inside the container) and declare it in `template.yaml`:

```yaml
data_paths:
  - data/**            # everything under data/
  # or be specific:
  # - data/app.sqlite
  # - data/datasets/**
```

Why this matters:

- **It's already durable.** `/home/developer` is a persistent Docker volume that survives container restarts, recreation, image upgrades, and template re-pulls (`git reset --hard`). You do **not** need a separate volume.
- **It stays out of git.** When you declare `data_paths`, Trinity appends `data/` to your agent's `.gitignore` at creation, so runtime data is never committed (no repo bloat, no accidental data leak). A template shipping its own `.gitignore` should pre-include `data/`.
- **It's portable.** The owner can export the data as a tar and import it into another agent/instance:
  - `POST /api/agents/{name}/data/export` — download a tar of `data/` (or `?format=base64` for small data inline).
  - `POST /api/agents/{name}/data/import` — restore a tar back into `data/` (only `data/**` entries are written; path traversal is rejected).
  - MCP tools `export_agent_data` / `import_agent_data` carry the tar as base64 for small datasets.

Keep `data_paths` entries **under `data/`** — entries that escape the data root (absolute paths, `../`) are never snapshotted. Don't overlap `.trinity/`, `.claude/`, `.env`, `.mcp.json`, `git.commit_paths`, or `persistent_state` — those are managed separately. (Validation checks `DP-001..DP-005` enforce this.)

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

# === RUNTIME CONFIGURATION (Optional) ===
# Defaults to Claude Code if not specified
runtime:
  type: claude-code               # "claude-code" or "gemini-cli"
  model: sonnet                   # Optional model override (e.g., "gemini-2.5-pro")

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

# Skills (for documentation/seeding - managed at platform level)
# Skills in .claude/skills/ are seeded to Platform Skills Library on first deploy
skills:
  - name: my-skill
    description: "What this skill enables"

# Supported platforms (if applicable)
platforms:
  - trinity
  - local

# === GIT CONFIGURATION ===
# Two sync modes are available:
#
# SOURCE MODE (default): Pull-only from GitHub
#   - Agent tracks the source branch (main) directly
#   - Changes are pulled from GitHub, never pushed back
#   - Use when developing locally and pushing to GitHub
#   - git.push_enabled is ignored in this mode
#
# WORKING BRANCH MODE (legacy): Bidirectional sync
#   - Agent creates unique branch: trinity/{agent}/{instance-id}
#   - Changes can be pushed back to GitHub
#   - Set source_mode=false when creating agent to enable
#
git:
  push_enabled: true              # Only applies to Working Branch Mode
  commit_paths:                   # Paths auto-committed on sync (Working Branch Mode only)
    - memory/
    - outputs/
    - CLAUDE.md
  ignore_paths:
    - .mcp.json
    - .env
    - "*.log"

# === SHARED FOLDERS ===
# File-based collaboration between agents
# These are DEFAULT values when agent is created from template
shared_folders:
  expose: false                   # Expose /home/developer/shared-out
  consume: false                  # Mount shared folders from permitted agents

# === CUSTOM METRICS ===
# Agent-specific KPIs displayed in Trinity UI
# See "Custom Metrics" section for complete documentation
metrics:
  - name: metric_name             # Required: internal identifier (snake_case)
    type: counter                 # Required: counter|gauge|percentage|status|duration|bytes
    label: "Display Label"        # Required: shown in UI
    description: "What this tracks"  # Optional: tooltip
    unit: "items"                 # Optional: unit label (gauge type)
    warning_threshold: 80         # Optional (percentage type): yellow if below
    critical_threshold: 50        # Optional (percentage type): red if below
    values:                       # Required for status type only
      - value: "active"           # Value written to metrics.json
        color: "green"            # green|red|yellow|gray|blue|orange
        label: "Active"           # Display label in UI
```

---

## CLAUDE.md Requirements

Every Trinity-compatible agent MUST have a CLAUDE.md with **domain-specific** content.

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

## Workflows
Domain-specific processes you follow.
How you approach tasks in your specialty.

## Constraints
- Domain-specific rules
- Safety constraints for your area
- Things you should NOT do
```

### Imports

CLAUDE.md files can import other files using `@path/to/file` syntax:

```markdown
See @README.md for project overview and @package.json for npm commands.

# Additional Instructions
- Git workflow: @docs/git-instructions.md
- Individual preferences: @~/.claude/my-project-instructions.md
```

Imports support relative paths, absolute paths, and `~` for home directory. Not evaluated inside code blocks.

### Best Practices

| ✅ Include | ❌ Exclude |
|-----------|-----------|
| Bash commands Claude can't guess | Anything Claude can infer from code |
| Code style rules differing from defaults | Standard language conventions |
| Testing instructions and runners | Detailed API docs (link instead) |
| Repository etiquette (branch naming, PRs) | Information that changes frequently |
| Architectural decisions | Long explanations or tutorials |
| Common gotchas | Self-evident practices ("write clean code") |

**If Claude ignores rules**, the file is probably too long. Use emphasis for critical rules: `IMPORTANT: Always run tests before committing`

---

## Runtime Options

Trinity supports multiple AI runtimes, allowing you to choose the best provider for each agent's use case.

### Available Runtimes

| Runtime | Provider | Context Window | Pricing | Best For |
|---------|----------|----------------|---------|----------|
| `claude-code` | Anthropic | 200K tokens | Pay-per-use | Complex reasoning, code quality |
| `gemini-cli` | Google | 1M tokens | Free tier | Large codebases, data processing |

### Configuring Runtime in template.yaml

```yaml
# Option 1: Simple runtime selection
runtime:
  type: gemini-cli

# Option 2: With model override
runtime:
  type: gemini-cli
  model: gemini-2.5-pro

# Option 3: Claude with specific model
runtime:
  type: claude-code
  model: opus  # or sonnet, haiku
```

### Default Behavior

If `runtime:` is not specified, agents default to `claude-code` for backward compatibility.

### Environment Requirements

| Runtime | Required Environment Variable |
|---------|------------------------------|
| `claude-code` | `ANTHROPIC_API_KEY` |
| `gemini-cli` | `GOOGLE_API_KEY` |

See [Gemini Support Guide](GEMINI_SUPPORT.md) for detailed setup instructions and cost comparisons.

---

## Long-Running & Background Processes (the orphan sweeper)

Each agent container runs a periodic **orphan sweeper** (`agent_server`,
issue #817). On a fixed interval it reads the container cgroup and
**SIGKILLs every process not on an allowlist** — this is how Trinity
recovers CPU/event-loop starvation from processes a finished execution
leaked behind. A process is **spared** when it is any of:

- the agent-server and its parent chain, and PID 1 (container init);
- an **in-flight platform execution** (chat / task / schedule / loop) and
  every subprocess it spawned — these carry a registered PID;
- an **operator session** — an SSH session **or** a live `docker exec`
  session (PPID 0) and its children (#1153);
- a base-image essential (the keep-alive, sshd wrapper, guardrail writer);
- a process whose argv matches a pattern in
  **`~/.trinity/persistent-processes.allow`**.

**What this means for templates:**

- **Run maintenance work as a platform execution, not a detached background
  job.** A FAISS index build, a data refresh, a migration — launch it as a
  scheduled task, a `run_agent_loop`, or an MCP `chat`/`task`. It then has a
  registered PID (and, under the pull model, a lease) and is never swept.
  A bare `nohup … &` or a process double-forked away from the execution has
  no registered PID and **will be reaped** mid-run.
- **Long-lived daemons started from a `SessionStart` hook** (e.g. a local
  MCP HTTP server) must be declared in
  `~/.trinity/persistent-processes.allow` — one `fnmatch` glob per line,
  `#` for comments:

  ```
  # local MCP server started by SessionStart hook
  *my-mcp-server*
  ```

- **Operator debugging is safe.** A long `docker exec agent-<name> …` or
  `trinity ssh` session survives sweeps (#1153). When the session exits,
  any children it left behind reparent to PID 1 and are reaped on the next
  sweep — as intended.

Each kill is logged at **WARNING** with the victim's `pid` and a
length-capped `cmd=` — `docker logs agent-<name> 2>&1 | grep OrphanSweep`
shows exactly what was reaped if a job disappears unexpectedly.

---

## Credential Management

> **Updated 2026-02-05 (CRED-002)**: Simplified credential system using direct file injection with encrypted git storage.

### Credential Flow

Credentials are managed as files in the agent workspace:

1. **`.env`** — Source of truth for KEY=VALUE credentials
2. **`.mcp.json`** — MCP server configuration (edit directly, no template substitution)
3. **`.credentials.enc`** — Encrypted backup safe for git commits

### Injecting Credentials

- **Via UI**: Agent Detail → Credentials tab → Quick Inject (paste KEY=VALUE text)
- **Via MCP**: `inject_credentials(agent_name, {".env": "KEY=value\n"})`
- **Via API**: `POST /api/agents/{name}/credentials/inject`

### Export/Import for Git

To persist credentials across deployments:

1. **Export**: Click "Export to Git" → encrypts `.env` and `.mcp.json` → writes `.credentials.enc`
2. **Commit**: The `.credentials.enc` file is safe to commit (AES-256-GCM encrypted)
3. **Auto-import**: On agent start, if `.credentials.enc` exists but `.env` doesn't, credentials are automatically decrypted and injected

### Your Agent Should

- Read MCP credentials from `.mcp.json` (Claude Code does this automatically)
- Read script credentials from `.env` or environment variables
- Never store plaintext credentials in committed files
- Use `.credentials.enc` for secure credential persistence in git

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

### 60-Second MCP Call Timeout

> **Design Limitation**: Claude Code enforces a hardcoded 60-second timeout on all MCP HTTP tool calls. Any `chat_with_agent` call that takes longer than 60 seconds will fail regardless of the `timeout_seconds` parameter.

When designing agents that collaborate with other agents, ensure that:
- Synchronous MCP calls complete within 60 seconds
- Complex tasks use the async pattern (`parallel=true, async=true`) and poll for results
- Large data exchanges use shared folders instead of MCP return values

See the [Multi-Agent System Guide](MULTI_AGENT_SYSTEM_GUIDE.md#design-limitation-60-second-mcp-call-timeout) for workaround patterns.

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

## Platform Skills

**Skills are the recommended way to encode reusable knowledge for agents.** A Skill is a markdown file that teaches Claude how to do something specific — like reviewing PRs using your team's standards or generating commit messages in your preferred format. When an agent encounters a task that matches a Skill's purpose, Claude automatically applies it.

Unlike slash commands (which require `/command` to invoke), **skills are model-invoked**: Claude decides which skills to use based on the task at hand. This makes skills ideal for organizational knowledge that should be consistently applied.

### How Trinity Manages Skills

1. **Platform stores skills** in a centralized library (synced from GitHub or created via UI)
2. **Admins manage skills** via the `/skills` page (create, edit, delete)
3. **Owners assign skills** to their agents via the Skills tab
4. **Skills are injected** into `~/.claude/skills/<name>/SKILL.md` on agent start
5. **CLAUDE.md is updated** with a "Platform Skills" section listing available skills

### Skill Types

Use naming conventions to indicate how a skill should be applied:

| Type | Naming Convention | When to Use | Example |
|------|-------------------|-------------|---------|
| `policy` | `policy-*` | Always-active rules that Claude follows implicitly | `policy-code-review`, `policy-security` |
| `procedure` | `procedure-*` | Step-by-step instructions for specific tasks | `procedure-incident-response`, `procedure-deploy` |
| `methodology` | (no prefix) | General guidance for approaches to problems | `verification`, `tdd`, `systematic-debugging` |

### Writing Effective Skills

Every skill needs a `SKILL.md` file with YAML frontmatter and markdown instructions:

```yaml
---
name: code-review
description: Reviews code for quality, security, and best practices. Use when reviewing pull requests, code changes, or asking "is this code good?"
---

# Code Review

## Instructions

When reviewing code, check for:
1. **Security issues** - SQL injection, XSS, exposed secrets
2. **Error handling** - Are all error cases handled?
3. **Performance** - Any obvious N+1 queries or inefficient loops?
4. **Readability** - Is the code self-documenting?

## Output Format

Provide feedback in three sections:
- **Critical**: Must fix before merge
- **Suggestions**: Would improve the code
- **Good**: Highlight what was done well
```

#### Frontmatter Fields

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Skill identifier (lowercase, hyphens, max 64 chars) |
| `description` | Yes | What the skill does and when to use it. **Claude uses this to decide when to apply the skill.** |
| `allowed-tools` | No | Restrict which tools Claude can use (e.g., `Read, Grep, Glob` for read-only) |
| `disable-model-invocation` | No | Set `true` to prevent Claude from auto-invoking. User must invoke with `/skill-name`. Use for workflows with side effects. |
| `user-invocable` | No | Set `false` to hide from `/` menu. Use for background knowledge Claude should apply automatically but users shouldn't invoke directly. |
| `argument-hint` | No | Autocomplete hint (e.g., `[issue-number]`, `[filename] [format]`) |
| `model` | No | Override model for this skill (`sonnet`, `opus`, `haiku`) |
| `context` | No | Set to `fork` to run in isolated subagent context |
| `agent` | No | Subagent type when `context: fork` (`Explore`, `Plan`, or custom agent name) |
| `hooks` | No | Lifecycle hooks scoped to this skill (`PreToolUse`, `PostToolUse`, `Stop`) |

#### Invocation Control

| Frontmatter | User can invoke | Claude can invoke | Context behavior |
|-------------|-----------------|-------------------|------------------|
| (default) | ✅ | ✅ | Description loaded, full skill on invoke |
| `disable-model-invocation: true` | ✅ | ❌ | Not in context until user invokes |
| `user-invocable: false` | ❌ | ✅ | Description loaded, auto-applied when relevant |

#### String Substitutions

Skills support variable substitution:

| Variable | Description |
|----------|-------------|
| `$ARGUMENTS` | All arguments passed when invoking (e.g., `/fix-issue 123` → `123`) |
| `$ARGUMENTS[N]` or `$N` | Specific argument by index (`$0` = first, `$1` = second) |
| `${CLAUDE_SESSION_ID}` | Current session ID (useful for logging, session-specific files) |

```yaml
---
name: fix-issue
description: Fix a GitHub issue
disable-model-invocation: true
---
Fix GitHub issue $ARGUMENTS following our coding standards.
# Or: Migrate the $0 component from $1 to $2.
```

#### Dynamic Context Injection

The `!`command`` syntax runs shell commands before skill content is sent to Claude:

```yaml
---
name: pr-summary
description: Summarize a pull request
context: fork
agent: Explore
---
## Context
- PR diff: !`gh pr diff`
- Changed files: !`gh pr diff --name-only`

Summarize this pull request...
```

Commands execute immediately; output replaces the placeholder. Claude only sees the final rendered content.

#### Description Best Practices

The description is critical — Claude uses it to decide whether to apply the skill. A good description:

```yaml
# ❌ Bad - too vague
description: Helps with code

# ✅ Good - specific actions and trigger terms
description: Reviews code for quality, security, and best practices. Use when reviewing pull requests, code changes, or asking "is this code good?"
```

Include:
1. **What it does**: Specific capabilities (reviews, generates, validates)
2. **When to use it**: Trigger phrases users would say ("review this PR", "is this secure?")

### Restricting Tool Access

Use `allowed-tools` to limit what Claude can do when a skill is active:

```yaml
---
name: read-only-analysis
description: Analyze code without making changes
allowed-tools: Read, Grep, Glob
---
```

This is useful for:
- Read-only skills that shouldn't modify files
- Security-sensitive workflows
- Analysis tasks that should never write

### Skill Size Guidelines

- **Keep `SKILL.md` under 500 lines.** Move detailed reference material to separate files.
- **Skill descriptions budget**: ~15,000 characters total across all skills. If you have many skills, some may be excluded from context.
- **Bundled scripts** run without consuming context tokens — only their output does.

### Multi-File Skills

For complex skills, use progressive disclosure — essential info in `SKILL.md`, details in supporting files:

```
my-skill/
├── SKILL.md           # Overview and navigation (<500 lines)
├── reference.md       # Detailed docs (loaded when needed)
├── examples.md        # Usage examples
└── scripts/
    └── validate.py    # Utility script (executed, not loaded)
```

In `SKILL.md`, reference the files:

```markdown
For detailed API reference, see [reference.md](reference.md).

To validate input, run:
```bash
python scripts/validate.py input.txt
```

### Agent Perspective

Agents see assigned skills in the standard Claude Code location:

```
~/.claude/skills/
├── verification/
│   └── SKILL.md
├── systematic-debugging/
│   └── SKILL.md
└── policy-code-review/
    └── SKILL.md
```

The agent's `CLAUDE.md` is also updated with a "Platform Skills" section:

```markdown
## Platform Skills

This agent has the following skills installed in `~/.claude/skills/`:

- `/verification` - Use with /verification command
- `/systematic-debugging` - Use with /systematic-debugging command
- `/policy-code-review` - Use with /policy-code-review command
```

This allows agents to answer "what skills do you have?" without scanning the filesystem.

### What This Means for Templates

**Templates should NOT include `.claude/skills/`** — skills are managed at the platform level and assigned per-agent. If your template includes skills in `.claude/skills/`, they will be seeded to the platform library on first deployment, then managed centrally.

### Syncing Skills

When skills are updated at the platform level, agents receive updates:
- **On next start**: Skills automatically injected
- **While running**: Use "Inject to Agent" button in Skills tab or MCP `sync_agent_skills` tool

### MCP Tools for Skills

Agents can interact with the skills system programmatically:

| Tool | Description |
|------|-------------|
| `list_skills` | List all platform skills |
| `get_skill` | Get skill details and content |
| `assign_skill_to_agent` | Assign a skill to an agent |
| `sync_agent_skills` | Re-inject skills to running agent |

### Skills vs. Slash Commands vs. CLAUDE.md

| Mechanism | Invoked By | Best For |
|-----------|------------|----------|
| **Skills** | Claude (automatic) | Reusable knowledge that applies across many situations |
| **Slash commands** | User (`/command`) | Specific actions the user explicitly requests |
| **CLAUDE.md** | Always loaded | Project-wide context and constraints |

**Use skills when**: The knowledge should apply automatically based on the task (e.g., always apply security review standards when reviewing code).

**Use slash commands when**: The user needs to explicitly trigger an action (e.g., `/deploy staging`).

---

## Custom Metrics

Agents can define custom KPIs displayed in the Trinity UI Metrics tab. This enables domain-specific observability beyond generic tool call counts.

### Metric Types

| Type | Description | Display | Example |
|------|-------------|---------|---------|
| `counter` | Monotonically increasing | Large number | "42 Messages" |
| `gauge` | Value that can go up/down | Number + optional unit | "12.5 Avg Words" |
| `percentage` | 0-100 with progress bar | Colored bar | "75% Success" |
| `status` | Enum/state value | Colored badge | "Active", "Idle" |
| `duration` | Time in seconds | Formatted time | "2h 15m" |
| `bytes` | Size in bytes | Formatted size | "1.2 MB" |

### How It Works

1. Define metrics in `template.yaml` under `metrics:`
2. Agent writes values to `metrics.json` in workspace
3. Trinity UI displays metrics in the Metrics tab (auto-refresh every 30 seconds)
4. **Agent must be running** for metrics to be visible

### File Locations

The agent server reads from the agent's working directory (`/home/developer/`):
- **Definitions**: `/home/developer/template.yaml`
- **Values**: `/home/developer/metrics.json`

### template.yaml Metric Definitions

```yaml
metrics:
  # Counter - monotonically increasing value
  - name: messages_processed        # Internal identifier (snake_case)
    type: counter
    label: "Messages"               # Display label
    description: "Total messages"   # Tooltip text

  # Gauge - value that goes up and down
  - name: avg_response_time
    type: gauge
    label: "Avg Response"
    unit: "ms"                      # Optional unit label

  # Percentage - with color thresholds
  - name: success_rate
    type: percentage
    label: "Success Rate"
    warning_threshold: 80           # Yellow if below 80%
    critical_threshold: 50          # Red if below 50%

  # Status - enum with colored badges
  - name: current_state
    type: status
    label: "State"
    values:                         # Required for status type
      - value: "active"             # The value in metrics.json
        color: "green"              # green, red, yellow, gray, blue, orange
        label: "Active"             # Display label
      - value: "idle"
        color: "gray"
        label: "Idle"
      - value: "error"
        color: "red"
        label: "Error"

  # Duration - time in seconds
  - name: last_cycle_duration
    type: duration
    label: "Last Cycle"
    description: "Duration of last processing cycle"

  # Bytes - size in bytes
  - name: cache_size
    type: bytes
    label: "Cache Size"
```

### metrics.json Format

Your agent writes current values to `metrics.json`:

```json
{
  "messages_processed": 42,
  "avg_response_time": 125.5,
  "success_rate": 87.5,
  "current_state": "active",
  "last_cycle_duration": 120,
  "cache_size": 1048576,
  "last_updated": "2025-12-10T10:30:00Z"
}
```

**Notes:**
- Keys must match the `name` field in template.yaml
- `last_updated` is optional but recommended (shown as "Updated X ago" in UI)
- Values are read when the Metrics tab is viewed or refreshed

### Complete Example

**template.yaml:**
```yaml
name: research-agent
display_name: Research Agent
description: Autonomous researcher

resources:
  cpu: "1"
  memory: "2g"

metrics:
  - name: research_cycles
    type: counter
    label: "Research Cycles"
    description: "Total research cycles completed"

  - name: findings_discovered
    type: counter
    label: "Findings"
    description: "Total findings discovered"

  - name: research_status
    type: status
    label: "Status"
    values:
      - value: "active"
        color: "green"
        label: "Researching"
      - value: "idle"
        color: "gray"
        label: "Idle"

  - name: last_cycle_duration
    type: duration
    label: "Last Cycle"
```

**Updating metrics in your agent:**
```bash
# In a script or via Claude Code
cat > /home/developer/metrics.json << 'EOF'
{
  "research_cycles": 5,
  "findings_discovered": 23,
  "research_status": "idle",
  "last_cycle_duration": 180,
  "last_updated": "2025-12-10T10:30:00Z"
}
EOF
```

**In CLAUDE.md instructions:**
```markdown
## Metrics Tracking

After each research cycle, update metrics.json:
- Increment `research_cycles`
- Update `findings_discovered` count
- Set `research_status` to "active" during work, "idle" when done
- Record `last_cycle_duration` in seconds
```

---

## Agent Dashboard

Agents can define a custom dashboard displayed in the Trinity UI Dashboard tab.

### File Location

Save the dashboard configuration to **`/home/developer/dashboard.yaml`** — the root of the agent's working directory.

If the file does not exist, no dashboard will be displayed.

### Basic Structure

```yaml
title: "My Agent Dashboard"
refresh: 30                    # Auto-refresh interval (seconds, min 5)

sections:
  - title: "Status"
    layout: grid               # 'grid' or 'list'
    columns: 3                 # 1-4 columns
    widgets:
      - type: metric
        label: "Total Tasks"
        value: 42
        trend: up

      - type: status
        label: "System"
        value: "Healthy"
        color: green           # green, red, yellow, gray, blue, orange

      - type: progress
        label: "Disk Usage"
        value: 75
        color: yellow
```

### Widget Types

| Type | Required Fields | Description |
|------|----------------|-------------|
| `metric` | label, value | Number with optional trend (up/down) |
| `status` | label, value, color | Colored badge |
| `progress` | label, value | Progress bar (0-100) |
| `text` | **content** | Plain text (NOT `text` or `value`) |
| `markdown` | **content** | Rendered markdown |
| `table` | columns, rows | Tabular data |
| `list` | **items** | Bullet/numbered list (NOT `values` or `list`) |
| `link` | label, **url** | Clickable link (NOT `href`) |
| `image` | src, alt | Image display |
| `divider` | - | Horizontal line |
| `spacer` | - | Vertical space |

### Widget Examples (All Types)

**IMPORTANT**: Use exact field names shown below. Common mistakes:
- `text` widget requires `content` (not `text`, `value`, or `label`)
- `list` widget requires `items` (not `values`, `list`, or `content`)
- `link` widget requires `url` (not `href` or `link`)

```yaml
widgets:
  # METRIC - numeric value with optional trend
  - type: metric
    label: "Total Tasks"        # Required
    value: 42                   # Required (number)
    trend: up                   # Optional: up, down
    trend_value: "+12%"         # Optional
    unit: "tasks"               # Optional
    description: "Since start"  # Optional

  # STATUS - colored badge
  - type: status
    label: "System Status"      # Required
    value: "Healthy"            # Required (string)
    color: green                # Required: green, red, yellow, gray, blue, orange, purple

  # PROGRESS - progress bar (0-100)
  - type: progress
    label: "Disk Usage"         # Required
    value: 75                   # Required (0-100)
    color: yellow               # Optional: green, red, yellow, blue

  # TEXT - plain text (NOT 'text' or 'value'!)
  - type: text
    content: "This is plain text"  # Required - MUST use 'content'
    size: md                       # Optional: xs, sm, md, lg
    color: gray                    # Optional
    align: center                  # Optional: left, center, right

  # MARKDOWN - rendered markdown
  - type: markdown
    content: "**Bold** and *italic* text"  # Required - MUST use 'content'

  # TABLE - tabular data
  - type: table
    title: "Recent Events"      # Optional
    columns:                    # Required
      - { key: date, label: Date }
      - { key: event, label: Event }
    rows:                       # Required
      - { date: "2024-01-01", event: "Started" }
      - { date: "2024-01-02", event: "Completed" }
    max_rows: 5                 # Optional

  # LIST - bullet or numbered list (NOT 'values'!)
  - type: list
    title: "Tasks"              # Optional
    items:                      # Required - MUST use 'items'
      - "Task 1"
      - "Task 2"
      - "Task 3"
    style: bullet               # Optional: bullet, number, none
    max_items: 10               # Optional

  # LINK - clickable link (NOT 'href'!)
  - type: link
    label: "Documentation"      # Required
    url: "https://example.com"  # Required - MUST use 'url'
    external: true              # Optional: opens in new tab
    style: button               # Optional: 'button' or omit for text link
    color: blue                 # Optional

  # IMAGE - image display
  - type: image
    src: "/files/chart.png"     # Required (or full URL)
    alt: "Chart description"    # Required
    caption: "Weekly metrics"   # Optional

  # DIVIDER - horizontal line
  - type: divider

  # SPACER - vertical space
  - type: spacer
    size: lg                    # Optional: sm (8px), md (16px), lg (32px)
```

### Updating Dashboard Data

Your agent updates the dashboard by rewriting `dashboard.yaml`. Use dynamic values:

```yaml
widgets:
  - type: metric
    label: "Processed"
    value: 127              # Update this value in your agent
    description: "Last run: 2 min ago"
```

**Note**: Agent must be running for dashboard to display. See `docs/memory/feature-flows/agent-dashboard.md` for complete schema.

---

## Agent-Defined Pipelines (#919)

If your agent runs a long-running, multi-stage pipeline (e.g. perception → incubation → synthesis → publish → measure), you can make its shape and live state **uniformly discoverable** by Trinity — **without** Trinity owning the DAG, the execution, or the recovery. You own all of that (via schedules, events, the operator queue, and the pre-check hook); Trinity only **reads** two files you publish.

### The file contract

Publish two kinds of file inside your container:

```
~/.trinity/pipelines/<pipeline_id>.yaml                  # definition (the DAG)
~/.trinity/pipeline-state/<pipeline_id>/<instance_id>.json  # live state per instance
```

- **`<pipeline_id>`** and **`<instance_id>`** must match `^[A-Za-z0-9._-]+$` (no `/`, no `..`). Trinity interpolates them into read paths and rejects anything else.
- **`<instance_id>` SHOULD be time-sortable.** Trinity selects the *latest* instance by the state file's mtime, tie-broken by `instance_id` lexically.
- Authoritative schemas (validate your files against these):
  - [`docs/schemas/agent-pipeline.schema.json`](schemas/agent-pipeline.schema.json) — the definition
  - [`docs/schemas/agent-pipeline-state.schema.json`](schemas/agent-pipeline-state.schema.json) — the state. **Required:** `instance_id`, `current_stage`, `health`, `updated_at` (ISO-8601), `escalations` (array). Trinity reads exactly these for its summary.

Minimal example:

```yaml
# ~/.trinity/pipelines/research.yaml
id: research
name: Daily Research Pipeline
stages:
  - id: collect
  - id: synthesize
  - id: publish
```

```json
// ~/.trinity/pipeline-state/research/2026-06-26T1200.json
{
  "instance_id": "2026-06-26T1200",
  "pipeline_id": "research",
  "current_stage": "synthesize",
  "health": "green",
  "updated_at": "2026-06-26T12:05:00Z",
  "escalations": []
}
```

### Reading it back (MCP tools)

Two thin, read-only MCP tools wrap the existing agent file-browser surface — no new backend endpoint, no Trinity-side DB:

- **`list_agent_pipelines(agent_name)`** — enumerates `pipelines/*.yaml`, each with a health summary from its latest instance (`current_stage`, `health`, `open_escalations`, `updated_at`). Returns `[]` when you publish no pipelines. A *stopped/unreachable* agent surfaces a real error, not an empty list (the files live in the container).
- **`get_agent_pipeline_state(agent_name, pipeline_id, instance_id?)`** — the full parsed state JSON for one instance; omit `instance_id` for the latest. A missing pipeline/instance returns a clean not-found (never a 500).

`open_escalations` counts `escalations[]` entries that are still open — an entry is open unless it carries `open: false`, `resolved: true`, or `status` in `{resolved, closed, done}`.

### Grouping escalations by pipeline (operator-queue convention)

When you escalate a stuck stage to the **operator queue**, put the pipeline coordinates in the queue item's free-form `context` JSON so escalations group by pipeline in the UI — **no Trinity schema change**:

```json
{ "pipeline_id": "research", "instance_id": "2026-06-26T1200", "stage": "synthesize" }
```

### Driving it (agent-side heartbeat — not Trinity)

Stage advancement, retry, and escalation are owned by your agent: run a single `pipeline-tick` skill on a cron schedule, gated by your `~/.trinity/pre-check` hook so it's near-free when nothing needs attention. That heartbeat ships with the **`agent-dev:add-pipeline`** plugin in [`abilityai/abilities`](https://github.com/abilityai/abilities), **not** with Trinity.

> **Adoption note:** these tools ship as MCP + docs only. Existing agents return `[]` until they adopt this file convention — that's by design, not a bug.

---

## Memory Management

Agents manage their own memory. Trinity provides storage, not strategy. Each agent can implement memory however it sees fit.

### Example Structure (Optional)

This is a suggested pattern, not a requirement:

```
memory/
├── context.md           # Long-term learned context
├── preferences.json     # User preferences
├── session_notes/       # Per-session working notes
│   └── 2025-12-05.md
└── summaries/           # Compressed old context
    └── 2025-11.md       # Monthly summary
```

---

## Content Folder Convention

For agents that generate large files (videos, audio, images, exports), Trinity provides a standard convention to prevent Git repository bloat.

### The Problem

Large generated files:
- Bloat Git repositories (video files can be 100s of MB)
- Slow down GitHub sync operations
- Risk accidental commits

### The Solution

Trinity automatically creates a `content/` directory structure:

```
/home/developer/content/
├── videos/      # Generated video files
├── audio/       # Generated audio files
├── images/      # Generated images
└── exports/     # Data exports, large files
```

**Key Properties:**
- ✅ **Persists** - Files survive container restarts (same Docker volume as workspace)
- ✅ **Excluded from Git** - Automatically added to `.gitignore`
- ✅ **Not synced** - Git sync ignores `content/` directory

### Usage in Your Agent

When generating large assets, save them to `content/`:

```python
# In your agent's scripts
output_path = "/home/developer/content/videos/my-video.mp4"
```

```markdown
# In your CLAUDE.md
When generating videos, save them to `content/videos/`.
When exporting data, save to `content/exports/`.
```

### outputs/ vs content/

| Directory | Synced to Git? | Use For |
|-----------|---------------|---------|
| `outputs/` | ✅ Yes | Small files you want versioned (reports, summaries) |
| `content/` | ❌ No | Large files that shouldn't be in Git (videos, audio) |

---

## Package Persistence

When agents install system packages (via `apt-get`, `npm install -g`, etc.), those packages are lost when the container is updated. Trinity provides a setup script convention to handle this.

### How It Works

1. When an agent installs a system package, it also appends the command to `~/.trinity/setup.sh`
2. On container start, Trinity runs this script automatically
3. Packages are reinstalled, surviving image updates

### Setup Script Location

```
/home/developer/.trinity/setup.sh
```

This file lives in the persistent workspace volume and survives container recreation.

### Usage Pattern

When installing packages, always add them to the setup script:

```bash
# Install the package
sudo apt-get install -y ffmpeg

# Remember it for future container starts
mkdir -p ~/.trinity
echo "sudo apt-get install -y ffmpeg" >> ~/.trinity/setup.sh
```

### Pre-configuring in Templates

Templates can ship with a pre-defined `setup.sh`:

```
my-agent/
├── .trinity/
│   └── setup.sh          # Pre-defined package installations
├── template.yaml
└── CLAUDE.md
```

Example `setup.sh` for a video processing agent:

```bash
#!/bin/bash
# Package persistence script - runs on every container start

# System packages
sudo apt-get update -qq
sudo apt-get install -y -qq ffmpeg imagemagick

# Global npm packages
npm install -g typescript ts-node

# Python packages (user-space)
pip install --user opencv-python moviepy
```

### What Goes Where

| Package Type | Persists Automatically? | Setup Script Needed? |
|--------------|------------------------|---------------------|
| `pip install --user` | ✅ Yes (in ~/.local) | No |
| `npm install` (local) | ✅ Yes (in node_modules/) | No |
| `go install` | ✅ Yes (in ~/go/) | No |
| `apt-get install` | ❌ No | Yes |
| `npm install -g` | ❌ No | Yes |
| System-level configs | ❌ No | Yes |

### Best Practices

1. **Prefer user-space installs**: `pip install --user`, local `npm install` when possible
2. **Keep setup.sh idempotent**: Use `-y` flags, check if already installed
3. **Minimize apt-get**: Each install adds startup time
4. **Document dependencies**: List required packages in README.md

---

## Compatibility Checklist

An agent is Trinity-compatible if:

### Required Files
- [ ] Has `template.yaml` with required fields (name, display_name, description, version, resources)
- [ ] Has `CLAUDE.md` with identity and domain-specific instructions
- [ ] Has `.mcp.json.template` with `${VAR}` placeholders (if using MCP servers)
- [ ] Has `.env.example` documenting required credentials
- [ ] Has `.gitignore` excluding secrets and large content

### Directory Structure
- [ ] (Optional) Has `docs/` directory for documentation

### Security
- [ ] No credentials stored in repository
- [ ] `.mcp.json` and `.env` are gitignored
- [ ] Sensitive files excluded from git sync paths

### Behavior
- [ ] Agent CLAUDE.md focuses on domain-specific instructions
- [ ] Can run both locally and on Trinity platform

---

## Migration Guide

To convert an existing agent to Trinity-compatible:

1. **Create repository structure** matching the directory layout above
2. **Extract CLAUDE.md** from current instructions (domain-specific only)
3. **Create template.yaml** with metadata and credentials
4. **Create .mcp.json.template** from current .mcp.json (replace values with ${VAR})
5. **Create .env.example** listing all required variables
6. **Add .gitignore** excluding secrets and platform directories
7. **Deploy to Trinity** and verify

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

### Domain Focus
- Keep CLAUDE.md focused on your agent's specialty
- Let Trinity handle collaboration and infrastructure
- Use outputs/ for generated content

### Documentation
- Keep documentation in a `docs/` folder
- Use README.md for quick-start and overview
- Document workflows, integrations, and constraints

---

## Autonomous Agent Design

Trinity agents achieve autonomy through a three-phase lifecycle:

```
DEVELOP → PACKAGE → SCHEDULE
```

1. **Develop** — Refine procedures interactively until they consistently produce good results
2. **Package** — Codify proven procedures as slash commands in `.claude/commands/`
3. **Schedule** — Run commands on cron via `schedules:` in template.yaml or the UI

### Autonomy Design Principles

| Principle | Description |
|-----------|-------------|
| **Self-contained** | No user input during execution |
| **Deterministic output** | Consistent format for parsing/alerts |
| **Graceful degradation** | Partial results better than failure |
| **Bounded scope** | Predictable runtime and cost |
| **Idempotent** | Safe to run multiple times |

### Quick Example

```yaml
# template.yaml
schedules:
  - name: Morning Health Check
    cron: "0 8 * * *"
    message: "/health-check"
    timezone: "UTC"
    enabled: true
```

```markdown
# .claude/commands/health-check.md
---
description: Automated fleet health check
allowed-tools: mcp__trinity__list_agents, mcp__trinity__get_agent
---

# Health Check
1. List all agents using `mcp__trinity__list_agents`
2. Evaluate context usage and last activity
3. Generate structured report
```

→ **Full guide**: [Autonomous Agent Design Guide](AUTONOMOUS_AGENT_DESIGN.md)

---

## Stop hook authoring — release inherited stdout

The Trinity platform reads the agent's `{"type":"result"}` JSON from Claude
Code's stdout pipe. Stop hooks inherit that pipe FD by default. If a hook
spawns a long-running subprocess that calls `setsid()` (`ssh` from
`git push` is the canonical case — see [#586](https://github.com/abilityai/trinity/issues/586) /
[#618](https://github.com/abilityai/trinity/pull/618)), the subprocess can
hold the pipe open during network I/O even after Claude exits. The
platform catches this via the `kill_cgroup_orphans()` sweep in
`drain_reader_threads` (SIGKILLs anything in the container cgroup outside
the allowlist) so your agent still completes, but the
slow path adds drain latency and shows up in `[METRIC] drain_outcome` log
lines with `outcome=natural` or `outcome=force_close`. Defensive hooks
avoid the slow path by releasing the inherited stdout FD before any
blocking I/O:

```bash
#!/bin/bash
exec 1>/dev/null    # release stdout pipe FD; keep stderr for diagnostics
set +e
git push origin HEAD
```

Closing stdout only (not stderr) preserves error messages from a failing
`git push`. Non-shell hook runtimes need the equivalent:

- **Python**: `sys.stdout = open(os.devnull, "w")` before spawning.
- **Node**: `process.stdout.destroy()` (or redirect with `stdio` when
  spawning child processes).

---

## Revision History

| Date | Changes |
|------|---------|
| 2026-02-05 | **Credential System Refactor (CRED-002)**: Updated Credential Management section for new simplified system; Direct file injection replaces Redis-based assignments; Export/Import with encrypted `.credentials.enc` for git storage; Auto-import on agent startup |
| 2026-01-27 | **Advanced Skills & CLAUDE.md**: Added 8 new skill frontmatter fields (`disable-model-invocation`, `user-invocable`, `argument-hint`, `model`, `context`, `agent`, `hooks`); Added invocation control table; Added string substitutions (`$ARGUMENTS`, `$N`, `${CLAUDE_SESSION_ID}`); Added dynamic context injection (`!`command``); Added skill size guidelines; Added CLAUDE.md imports (`@path` syntax) and best practices table |
| 2026-01-26 | **Platform Skills Best Practices**: Expanded Platform Skills section with comprehensive skill writing guidance; Added SKILL.md format, frontmatter fields, description best practices, `allowed-tools` for restricting tool access, multi-file skill patterns, Skills vs Commands vs CLAUDE.md comparison table; Emphasized skills as the recommended way to encode reusable knowledge |
| 2026-01-25 | **Platform Skills**: Added new section documenting centralized Skills Library; Skills managed at platform level, mounted read-only into agents; Three skill types (policy, procedure, methodology); Updated directory structure and .gitignore notes |
| 2026-01-13 | **Dashboard widget examples**: Added complete examples for ALL 11 widget types with required field names highlighted; Added warning box about common field name mistakes (`content` not `text`, `items` not `values`, `url` not `href`) |
| 2026-01-13 | Added Agent Dashboard section with YAML schema and widget types reference |
| 2026-01-12 | Expanded Custom Metrics section: added file locations, complete template.yaml examples for all 6 metric types (counter, gauge, percentage, status, duration, bytes), metrics.json format with last_updated field, complete working example, and CLAUDE.md integration guidance |
| 2026-01-12 | Updated .gitignore: added instance-specific files (.npm, .ssh, .trinity, .cache, .claude.json, .sudo_as_admin_successful); clarified what to commit vs exclude from .claude/ directory |
| 2026-01-12 | Added Package Persistence section with setup.sh convention for surviving container updates |
| 2026-01-12 | Simplified guide: removed Platform Injection, Testing Locally, Troubleshooting, Registering with Trinity, Multi-Agent Systems sections; Made memory/ optional; Added docs/ best practice |
| 2026-01-01 | Added Autonomous Agent Design section with lifecycle overview; Reference to detailed guide |
| 2025-12-30 | Documented Source Mode (default) vs Working Branch Mode (legacy) in Git Configuration; Removed Task DAG/workplan content (feature removed 2025-12-23) |
| 2025-12-27 | Added Content Folder Convention for large generated assets (videos, audio, images) |
| 2025-12-24 | Removed Chroma MCP integration - templates should include their own vector memory if needed |
| 2025-12-18 | Added Multi-Agent Systems section with System Manifest deployment reference |
| 2025-12-14 | Consolidated from AGENT_TEMPLATE_SPEC.md and trinity-compatible-agent.md |
| 2025-12-13 | Added shared folders |
| 2025-12-10 | Added custom metrics specification |

---

*This document is the single source of truth for Trinity-compatible agent development.*
