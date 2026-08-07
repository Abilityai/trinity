# Trinity Onboard Plugin - Requirements Specification

**Version**: 1.1
**Created**: 2025-02-05
**Updated**: 2026-02-09
**Status**: **Historical** — records the original spec, not current behaviour (marked 2026-08-07, #2055)

> ## ⚠️ This document is historical
>
> It captures the plugin as originally specified. The plugin has since been
> restructured, and **several sections below describe things that no longer
> exist**. Do not implement against it, and do not treat it as the onboarding
> contract.
>
> **The live contract is the skill source itself:**
> [`abilityai/abilities` → `plugins/trinity/`](https://github.com/abilityai/abilities/tree/main/plugins/trinity).
> Each skill's `SKILL.md` carries its own version and changelog; read those.
>
> See [What changed since this spec](#what-changed-since-this-spec) for the
> drifts verified when this banner was added.

---

## What changed since this spec

Verified against `abilityai/abilities` → `plugins/trinity/` on 2026-08-07. This
list is what was checked, not an exhaustive audit — assume anything below is
stale unless the skill source confirms it.

**`.mcp.json` is written by `/trinity:connect`, not by onboard** (#2055). This
is the drift that prompted the banner. [Phase 7](#phase-7-configure-local-connection)
below says onboard updates `.mcp.json`; onboard `v4.12` deleted that writer
outright, along with the `.mcp.json.template` it produced. Its Step 4 now only
*verifies* the connection, and the skill states the rule directly:

> Authentication and MCP configuration are owned by **`/trinity:connect`** — the
> single source of truth. onboard does **not** resolve credentials or write
> `.mcp.json` itself (duplicating that is what drifted before).

`onboard`, `sync` and `loop` all delegate to `connect`. A duplicated writer is
exactly what drifted before and caused the refactor, so §3.2's `npx mcp-remote`
config and every "creates `.mcp.json.template`" claim below are not just stale —
re-implementing them would reintroduce the bug. Trinity connection credentials
now live in `connect`'s `~/.trinity/config.json` + `.mcp.json`; the agent's
`.env` is for the agent's *own* integration secrets only.

**The skill set is different.** §4's table and §5's per-skill sections describe
skills that are gone or renamed:

| This doc describes | Actual `plugins/trinity/skills/` |
|---|---|
| `onboard`, `sync`, `connect` | still present (contents changed) |
| `dashboard` | renamed `create-dashboard` |
| `remote`, `schedules` | no longer exist as skills |
| §5's `trinity-adopt`, `trinity-compatibility`, `trinity-remote`, `trinity-sync`, `trinity-schedules` | **none of these directories exist** |
| — | new: `deploy-new-instance`, `loop`, `start-here` |

**`plugins/trinity/templates/` does not exist.** §3 and §6 describe a templates
directory (`template.yaml.example`, `gitignore.example`, `env.example`,
`mcp-json.template.example`); the path returns 404.

What has held up: the two-command install flow, `template.yaml` as the agent's
manifest, and `abilityai/abilities` as the system of record.

---

## Executive Summary

A Claude Code plugin that enables any Claude Code agent to onboard to the Trinity platform with minimal friction. The goal is two commands to full deployment:

```bash
/plugin marketplace add abilityai/abilities
/plugin install trinity@abilityai
/trinity:onboard
```

---

## Table of Contents

1. [Goals and Non-Goals](#1-goals-and-non-goals)
2. [Architecture Overview](#2-architecture-overview)
3. [Plugin Structure](#3-plugin-structure)
4. [Skill Library](#4-skill-library)
5. [Source Skills Reference](#5-source-skills-reference)
6. [Template Files](#6-template-files)
7. [Onboarding Workflow](#7-onboarding-workflow)
8. [MCP Integration](#8-mcp-integration)
9. [Implementation Phases](#9-implementation-phases)
10. [Testing Checklist](#10-testing-checklist)

---

## 1. Goals and Non-Goals

### Goals

- **Zero-friction onboarding**: Any Claude Code agent can deploy to Trinity in under 5 minutes
- **Methodology transfer**: Install Trinity skills that enable ongoing remote operations
- **Self-contained**: Plugin includes all templates and skills needed
- **Idempotent**: Running onboarding multiple times won't break anything

### Non-Goals

- Building a new MCP server (use existing Trinity MCP)
- Custom UI or dashboard (use existing Trinity web UI)
- Agent migration from other platforms

---

## 2. Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│           abilityai/abilities Repository (System of Record)      │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  plugins/trinity/                                                │
│  ├── skills/                                                     │
│  │   ├── onboard/              # Main onboarding workflow        │
│  │   ├── sync/                 # Git synchronization             │
│  │   ├── remote/               # Remote exec, run, notify        │
│  │   ├── schedules/            # Scheduled task management       │
│  │   ├── connect/              # MCP connection setup            │
│  │   └── dashboard/            # Dashboard configuration         │
│  ├── templates/                # File templates                  │
│  └── .mcp.json                 # Trinity MCP config              │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Target Agent                                  │
│              (after onboarding)                                  │
├─────────────────────────────────────────────────────────────────┤
│  .claude/skills/                                                 │
│  ├── trinity-adopt/                                              │
│  ├── trinity-compatibility/                                      │
│  ├── trinity-remote/                                             │
│  ├── trinity-sync/                                               │
│  └── trinity-schedules/                                          │
│  template.yaml                                                   │
│  .mcp.json.template                                              │
│  .env.example                                                    │
│  .gitignore (updated)                                            │
└─────────────────────────────────────────────────────────────────┘
```

---

## 3. Plugin Structure

Location: `github.com/abilityai/abilities/plugins/trinity/`

```
plugins/trinity/
├── .claude-plugin/
│   └── plugin.json              # Plugin manifest (required)
├── skills/
│   ├── onboard/                 # Main onboarding skill
│   ├── sync/                    # Git synchronization
│   ├── remote/                  # Remote operations
│   ├── schedules/               # Schedule management
│   ├── connect/                 # MCP connection setup
│   └── dashboard/               # Dashboard configuration
├── templates/
│   ├── template.yaml.example    # Agent metadata template
│   ├── gitignore.example        # Required .gitignore entries
│   ├── env.example              # Environment variables template
│   └── mcp-json.template.example # MCP config with placeholders
├── .mcp.json                    # Trinity MCP server config
└── README.md                    # Installation instructions
```

### 3.1 Plugin Manifest

**File**: `.claude-plugin/plugin.json`

```json
{
  "name": "trinity",
  "description": "Deploy to Trinity platform: connect, onboard, sync",
  "version": "1.0.0",
  "author": {
    "name": "Ability.ai",
    "email": "support@ability.ai"
  },
  "homepage": "https://trinity.ability.ai",
  "repository": "https://github.com/abilityai/trinity",
  "license": "MIT",
  "keywords": ["trinity", "deployment", "cloud", "agent", "automation"]
}
```

### 3.2 MCP Configuration

> ⛔ **Superseded (#2055).** This `npx mcp-remote` shape is the writer onboard
> deleted in `v4.12`. `/trinity:connect` owns `.mcp.json` now; do not
> reimplement this from here.

**File**: `.mcp.json`

```json
{
  "mcpServers": {
    "trinity": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://trinity.abilityai.dev/mcp"],
      "env": {
        "TRINITY_API_KEY": "${TRINITY_API_KEY}"
      }
    }
  }
}
```

---

## 4. Skill Library

The Trinity skills are bundled directly inside the `trinity` plugin in the abilities repo.

**Location**: `github.com/abilityai/abilities/plugins/trinity/skills/`

```
plugins/trinity/skills/
├── onboard/                 # Main onboarding workflow
├── sync/                    # Git synchronization
├── remote/                  # Remote operations
├── schedules/               # Schedule management
├── connect/                 # MCP connection setup
└── dashboard/               # Dashboard configuration
```

### 4.1 Skill Overview

| Skill | Commands | Purpose |
|-------|----------|---------|
| `onboard` | `/trinity:onboard` | Deploy agent to Trinity platform |
| `sync` | `/trinity:sync push`, `/trinity:sync pull`, `/trinity:sync status` | Git synchronization |
| `remote` | `/trinity:remote exec`, `/trinity:remote run` | Remote agent operations |
| `schedules` | `/trinity:schedules status`, `/trinity:schedules create`, `/trinity:schedules trigger` | Scheduled task management |
| `connect` | `/trinity:connect` | Set up MCP connection to Trinity |
| `dashboard` | `/trinity:dashboard` | Configure agent dashboard |

---

## 5. Skills Reference

> **System of Record**: All skills are in `github.com/abilityai/abilities/plugins/trinity/skills/`
>
> ⛔ **Superseded (#2055).** **None of the five skill directories described in
> this section still exist** (`trinity-adopt`, `trinity-compatibility`,
> `trinity-remote`, `trinity-sync`, `trinity-schedules`). The actual set is
> `connect`, `create-dashboard`, `deploy-new-instance`, `loop`, `onboard`,
> `start-here`, `sync`. Read their `SKILL.md` files instead — every "required
> files" list below, including the `.mcp.json.template` entries, is stale.

### 5.1 trinity-adopt

**Source**: `github.com/abilityai/abilities/plugins/trinity/skills/trinity-adopt/SKILL.md`

**Purpose**: Convert any Claude Code agent to Trinity-compatible format following best practices.

**Key Features**:
- Analyze mode (read-only audit)
- Adopt mode (full conversion)
- Creates required directories: `outputs/`, `scripts/`, `.claude/skills/`, `.claude/agents/`, `memory/`
- Creates required files: `template.yaml`, `.env.example`, `.gitignore`, `.mcp.json.template`
- Creates initial policy: `policy-agent-purpose`

**Metadata**:
```yaml
name: trinity-adopt
description: Convert any Claude Code agent to Trinity-compatible format
argument-hint: "[analyze|adopt]"
disable-model-invocation: true
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
version: "3.0"
```

### 5.2 trinity-compatibility

**Source**: `github.com/abilityai/abilities/plugins/trinity/skills/trinity-compatibility/SKILL.md`

**Purpose**: Analyze current agent directory against Trinity requirements and produce a detailed compatibility report.

**Key Features**:
- Checks 5 required files: `template.yaml`, `CLAUDE.md`, `.mcp.json.template`, `.env.example`, `.gitignore`
- Verifies directory structure
- Security audit (hardcoded credentials, excluded files)
- Produces actionable remediation steps

**Metadata**:
```yaml
name: trinity-compatibility
description: Analyze current agent for Trinity platform compatibility
disable-model-invocation: true
allowed-tools: Read, Glob, Grep, Bash(ls *), Bash(cat *)
```

### 5.3 trinity-remote

**Source**: `github.com/abilityai/abilities/plugins/trinity/skills/trinity-remote/SKILL.md`

**Purpose**: Remote agent operations - execute prompts, deploy-run workflows, and manage notifications.

**Commands**:
| Command | Description |
|---------|-------------|
| `/trinity-remote` | Show status of remote agent |
| `/trinity-remote exec <prompt>` | Execute prompt on remote (no sync) |
| `/trinity-remote run <prompt>` | Sync local changes, then execute (deploy-run) |
| `/trinity-remote notify [config]` | Configure notifications |

**Metadata**:
```yaml
name: trinity-remote
description: Remote agent operations - execute prompts, deploy-run workflows, and manage notifications
argument-hint: "[exec <prompt>|run <prompt>|notify <config>|status]"
disable-model-invocation: true
user-invocable: true
allowed-tools: Read, Write, Edit, Bash, Glob, Grep, mcp__trinity__list_agents, mcp__trinity__chat_with_agent, mcp__trinity__get_agent, mcp__trinity__start_agent
version: "1.0"
```

### 5.4 trinity-sync

**Source**: `github.com/abilityai/abilities/plugins/trinity/skills/trinity-sync/SKILL.md`

**Purpose**: Synchronize local agent with remote counterpart via GitHub. Supports branch-based versioning.

**Commands**:
| Command | Description |
|---------|-------------|
| `/trinity-sync status` | Check sync status between local and remote |
| `/trinity-sync pull` | Pull changes from remote |
| `/trinity-sync push` | Push local changes to remote on current branch |
| `/trinity-sync push <branch>` | Push and deploy a specific branch |
| `/trinity-sync deploy <branch>` | Deploy an existing branch/tag to remote |
| `/trinity-sync branches` | List available branches |

**Key Concepts**:

**What Syncs (Agent Value)**:
- `.claude/skills/` - Agent capabilities
- `.claude/agents/` - Sub-agent definitions
- `.claude/commands/` - Command definitions
- `memory/` - Schedules, persistent state
- `scripts/` - Python/bash scripts
- `CLAUDE.md` - Main agent instructions
- `template.yaml` - Trinity metadata

**What's Discardable (Runtime)**:
- `.claude/debug/`, `.claude/projects/`, `.claude/statsig/`, `.claude/todos/`
- `session-files/`, `content/`

**Metadata**:
```yaml
name: trinity-sync
description: Synchronize this agent with remote Trinity via GitHub
argument-hint: [push|pull|status|deploy <branch>|branches]
disable-model-invocation: true
user-invocable: true
allowed-tools: Bash, Read, Grep, mcp__trinity__list_agents, mcp__trinity__chat_with_agent
```

### 5.5 trinity-schedules

**Source**: `github.com/abilityai/abilities/plugins/trinity/skills/trinity-schedules/`

**Files**:
- `SKILL.md` - Main skill definition (303 lines)
- `registry-template.json` - Empty registry template
- `scripts/registry.py` - Python helper for registry management (466 lines)
- `examples.md` - Usage examples and documentation (251 lines)

**Purpose**: Manage scheduled tasks on remote Trinity platform via MCP.

**Commands**:
| Command | Description |
|---------|-------------|
| `/trinity-schedules status` | Show current state of all schedules |
| `/trinity-schedules sync` | Synchronize local registry with remote |
| `/trinity-schedules list` | Quick list of all schedules |
| `/trinity-schedules schedule <skill> [cron] [name]` | Create/update a schedule |
| `/trinity-schedules trigger <skill-or-id>` | Manually trigger execution |
| `/trinity-schedules history [skill] [--limit N]` | Show execution history |
| `/trinity-schedules pause <skill-or-id>` | Disable a schedule |
| `/trinity-schedules resume <skill-or-id>` | Enable a paused schedule |
| `/trinity-schedules link <id> <skill>` | Associate untracked schedule |
| `/trinity-schedules unlink <skill-or-id>` | Remove association |
| `/trinity-schedules delete <skill-or-id>` | Permanently delete schedule |

**Registry File**: `~/.schedule-registry.json`

**Metadata**:
```yaml
name: trinity-schedules
description: Manage scheduled tasks on remote Trinity platform via MCP
argument-hint: "[status|sync|list|schedule|trigger|history] [skill-name]"
disable-model-invocation: false
```

---

## 6. Template Files

> ⛔ **Superseded (#2055).** `plugins/trinity/templates/` **does not exist** —
> the path 404s. §6.4's `mcp-json.template.example` is doubly gone: onboard
> stopped emitting `.mcp.json.template` in `v4.12`.

### 6.1 template.yaml.example

```yaml
name: agent-name
display_name: Agent Name
description: |
  Describe what this agent does.
  Include key capabilities and use cases.
resources:
  cpu: "2"
  memory: "4g"
credentials:
  mcp_servers: {}
  env_file: []
```

### 6.2 gitignore.example

```gitignore
# Credentials - NEVER commit
.mcp.json
.env
*.pem
*.key
*credentials*
*secret*

# Claude Code internals
.claude/projects/
.claude/statsig/
.claude/todos/
.claude/debug/

# Runtime/generated content
content/
session-files/
*.log

# OS files
.DS_Store
Thumbs.db

# Dependencies
node_modules/
.venv/
__pycache__/
```

### 6.3 env.example

```bash
# Environment variables for this agent
# Copy to .env and fill in actual values

# Trinity API (provided after deployment)
# TRINITY_API_KEY=

# Add your agent-specific variables below:
```

### 6.4 mcp-json.template.example

```json
{
  "mcpServers": {
    "example-server": {
      "command": "npx",
      "args": ["-y", "example-mcp-server"],
      "env": {
        "API_KEY": "${EXAMPLE_API_KEY}"
      }
    }
  }
}
```

---

## 7. Onboarding Workflow

The `skills/onboard/SKILL.md` implements an 8-phase workflow:

### Phase 1: Discovery

1. Identify agent name from directory or `template.yaml`
2. Check for existing Trinity files
3. Read `.gitignore` contents
4. Check if git repo exists

### Phase 2: Compatibility Analysis

Generate report showing status of:
- CLAUDE.md
- template.yaml
- .gitignore
- .mcp.json.template
- .env.example
- outputs/
- scripts/

**Present report and ask user to confirm before proceeding.**

### Phase 3: Create Required Files

For each missing component, use templates from `templates/` directory:

1. **template.yaml** - Customize with agent name and description
2. **.gitignore** - Merge with existing
3. **.env.example** - Create from template
4. **.mcp.json.template** - Convert .mcp.json if exists
5. **Directories** - Create outputs/, scripts/, .claude/skills/, .claude/agents/

### Phase 4: Git Setup

1. Initialize git if not a repo
2. Check remote, prompt if missing
3. Commit Trinity compatibility files
4. Push to origin

### Phase 5: Deploy to Trinity

1. Call Trinity API via MCP: `mcp__trinity__deploy_agent`
2. Wait for deployment, poll status
3. Get agent info

### Phase 6: Install Trinity Skills

Copy Trinity skills from the plugin to target agent:

```bash
# Clone abilities repo (skills are bundled in the plugin)
git clone --depth 1 --filter=blob:none --sparse \
  https://github.com/abilityai/abilities.git /tmp/abilities
cd /tmp/abilities
git sparse-checkout set plugins/trinity/skills

# Copy to target agent
cp -r plugins/trinity/skills/trinity-* .claude/skills/
```

Or the onboard skill can copy from its own plugin directory.

### Phase 7: Configure Local Connection

> ⛔ **Superseded (#2055).** Step 1 is wrong and must not be reimplemented:
> onboard has no `.mcp.json` writer. `/trinity:connect` is the **single writer**
> — onboard hands off to it, then only verifies (step 2 still holds). See
> [What changed since this spec](#what-changed-since-this-spec).

1. ~~Update local `.mcp.json` to add Trinity server~~ → run `/trinity:connect`,
   which authenticates and writes `.mcp.json` (or refreshes it from a stored
   profile)
2. Verify connection: `mcp__trinity__list_agents()`

### Phase 8: Completion Report

```
## Trinity Onboarding Complete

### Agent Deployed
- Name: <agent-name>
- Status: running
- URL: https://trinity.abilityai.dev/agents/<agent-name>

### Files Created
- [x] template.yaml
- [x] .gitignore (updated)
- [x] .env.example
- [x] .mcp.json.template

### Skills Installed
- [x] /trinity-adopt - Convert agents to Trinity format
- [x] /trinity-compatibility - Check agent compatibility
- [x] /trinity-remote - Remote execution (exec, run, notify)
- [x] /trinity-sync - Git synchronization (push, pull, status)
- [x] /trinity-schedules - Scheduled task management

### Next Steps
1. Start the remote agent: `/trinity-remote`
2. Send a message: `/trinity-remote exec hello`
3. Set up schedules: `/trinity-schedules schedule <skill-name>`
```

---

## 8. MCP Integration

### 8.1 Required Trinity MCP Tools

The onboarding skill uses these Trinity MCP tools:

| Tool | Purpose |
|------|---------|
| `mcp__trinity__list_agents` | List all agents |
| `mcp__trinity__get_agent` | Get agent details |
| `mcp__trinity__deploy_agent` | Deploy agent from GitHub |
| `mcp__trinity__start_agent` | Start a stopped agent |
| `mcp__trinity__chat_with_agent` | Send message to agent |

### 8.2 Schedule Management Tools

Used by `trinity-schedules` skill:

| Tool | Purpose |
|------|---------|
| `list_agent_schedules` | List all schedules for agent |
| `get_agent_schedule` | Get schedule details |
| `create_agent_schedule` | Create new schedule |
| `update_agent_schedule` | Modify schedule |
| `delete_agent_schedule` | Remove schedule |
| `toggle_agent_schedule` | Enable/disable |
| `trigger_agent_schedule` | Manual trigger |
| `get_schedule_executions` | Execution history |

---

## 9. Implementation Status

> **Status**: ✅ Implemented - All phases complete

### Location

All Trinity skills and the onboard plugin are in: `github.com/abilityai/abilities/plugins/trinity/`

### What's Included

- `skills/onboard/` - Main onboarding workflow
- `skills/trinity-adopt/` - Convert to Trinity format
- `skills/trinity-compatibility/` - Audit agent structure
- `skills/trinity-remote/` - Remote operations
- `skills/trinity-sync/` - Git synchronization
- `skills/trinity-schedules/` - Schedule management
- `skills/credential-sync/` - Encryption helpers
- `skills/create-heartbeat/` - Heartbeat setup
- `templates/` - File templates for new agents
- `.mcp.json` - Trinity MCP connection config

### Installation

```bash
/plugin marketplace add abilityai/abilities
```

### Phase 5: Documentation

1. Update Trinity docs with plugin installation
2. Add troubleshooting guide
3. Create video walkthrough (optional)

---

## 10. Testing Checklist

### Plugin Installation

- [ ] Plugin installs from marketplace: `/plugin marketplace add abilityai/trinity`
- [ ] Plugin shows in list: `/plugin list`
- [ ] Skill appears: `/trinity:onboard`

### Onboarding Flow

- [ ] Discovery phase identifies agent correctly
- [ ] Compatibility report is accurate
- [ ] User confirmation works
- [ ] Required files created correctly
- [ ] Git operations succeed (init, commit, push)
- [ ] Deployment to Trinity succeeds
- [ ] Skills copied to `.claude/skills/`

### Post-Onboarding

- [ ] `/trinity-remote` shows agent status
- [ ] `/trinity-remote exec hello` works
- [ ] `/trinity-sync status` works
- [ ] `/trinity-schedules status` works
- [ ] Local MCP connection works

### Edge Cases

- [ ] Agent already Trinity-compatible (no changes needed)
- [ ] No git repo (initializes correctly)
- [ ] No GitHub remote (prompts user)
- [ ] Existing .mcp.json (converts to template)
- [ ] Idempotent (running twice doesn't break)

### Error Handling

- [ ] No CLAUDE.md → creates minimal one
- [ ] Deployment fails → helpful error message
- [ ] MCP connection fails → troubleshooting steps
- [ ] Git push fails → explains permissions issue

---

## Appendix A: Skill Reference

All skills are in the abilities repo (`github.com/abilityai/abilities`):

| Skill | Source Path |
|-------|-------------|
| trinity-adopt | `github.com/abilityai/abilities/plugins/trinity/skills/trinity-adopt/SKILL.md` |
| trinity-compatibility | `github.com/abilityai/abilities/plugins/trinity/skills/trinity-compatibility/SKILL.md` |
| trinity-remote | `github.com/abilityai/abilities/plugins/trinity/skills/trinity-remote/SKILL.md` |
| trinity-sync | `github.com/abilityai/abilities/plugins/trinity/skills/trinity-sync/SKILL.md` |
| trinity-schedules | `github.com/abilityai/abilities/plugins/trinity/skills/trinity-schedules/` (directory) |

**trinity-schedules additional files**:
- `registry-template.json` - Empty registry template
- `scripts/registry.py` - Python helper (466 lines)
- `examples.md` - Usage examples (251 lines)

---

## Appendix B: Claude Code Plugin Reference

Based on official Claude Code documentation:

### Plugin Directory Structure

```
plugin-name/
├── .claude-plugin/
│   └── plugin.json       # Manifest (required)
├── skills/               # Agent Skills with SKILL.md
├── commands/             # User commands (deprecated, use skills)
├── agents/               # Custom agent definitions
├── hooks/                # Event handlers
├── .mcp.json             # MCP server configurations
└── .lsp.json             # LSP server configurations
```

### Skill Naming

- Plugin skills are namespaced: `/plugin-name:skill-name`
- Example: `/trinity:onboard`

### Testing

```bash
# Load plugin for testing
claude --plugin-dir ./plugins/trinity

# Multiple plugins
claude --plugin-dir ./plugin-one --plugin-dir ./plugin-two
```

### Marketplace

Marketplace JSON at repo root enables plugin discovery:

```json
{
  "name": "abilityai-trinity",
  "owner": {
    "name": "Ability.ai",
    "email": "support@ability.ai"
  },
  "plugins": [
    {
      "name": "trinity",
      "source": "./plugins/trinity",
      "description": "Onboard any Claude Code agent to Trinity platform"
    }
  ]
}
```

---

## Revision History

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 1.1 | 2026-02-09 | Claude | Updated: abilities repo is now system of record. Skills bundled in plugin. Removed skill-library from trinity repo. |
| 1.0 | 2025-02-05 | Eugene | Initial draft |
