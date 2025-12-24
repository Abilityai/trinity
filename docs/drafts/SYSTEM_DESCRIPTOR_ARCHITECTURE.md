# System Descriptor Architecture for Multi-Agent Systems

> **Status**: Design Draft v2.1
> **Created**: 2025-12-15
> **Updated**: 2025-12-16
> **Authors**: Eugene + Claude
> **Based On**: `/Users/eugene/Dropbox/Agents/process-architecture.md`
> **Aligned With**: `docs/TRINITY_COMPATIBLE_AGENT_GUIDE.md`

---

## Executive Summary

This document proposes a **system-centric** approach for deploying coordinated multi-agent systems on Trinity. The core shift from v1:

**v1 (Agent-Centric)**: Separate repos per agent, system.yaml references external templates
**v2 (System-Centric)**: One repository per agentic system containing all agents, policies, and processes

Key principles:
1. **One Repo = One System** — All agents defined within the system repository
2. **Policies & Processes Govern** — Agents follow system-level rules, don't define their own
3. **Standardized Approval Pipeline** — Job folders, headless execution, interchangeable reviewers
4. **Orchestrator Owns Governance** — Can modify policies/processes; workers cannot

---

## The Three-Layer Model

| Layer | What It Contains | Who Can Modify | Location |
|-------|------------------|----------------|----------|
| **Policies** | Rules, constraints, standards | Orchestrator only | `system/policies/` |
| **Processes** | Workflows, steps, handoffs | Orchestrator only | `system/processes/` |
| **Procedures** | How to execute steps | Agent-specific | `agents/{name}/.claude/agents/` |

Workers read policies and processes. Only orchestrators can modify them.

---

## Repository Structure

### One Repository Per Agentic System

Each agent within the system follows the **same structure as standalone Trinity-compatible agents** (see `TRINITY_COMPATIBLE_AGENT_GUIDE.md`), with system-level additions.

```
content-production-system/
├── system.yaml                       # THE SYSTEM DEFINITION (replaces per-agent template.yaml)
├── .gitignore                        # Excludes secrets, platform-managed dirs
│
├── system/                           # SYSTEM-LEVEL GOVERNANCE
│   ├── policies/
│   │   ├── CHANGELOG.md
│   │   ├── content-standards.md
│   │   ├── approval-workflows.md
│   │   └── data-handling.md
│   └── processes/
│       ├── content-production/
│       │   └── PROCESS.md
│       └── weekly-review/
│           └── PROCESS.md
│
├── agents/                           # AGENT DEFINITIONS
│   ├── orchestrator/                 # Each agent follows standard structure
│   │   ├── CLAUDE.md                 # Agent instructions
│   │   ├── .mcp.json.template        # MCP config with ${VAR} placeholders
│   │   ├── .env.example              # Documents required credentials
│   │   ├── .claude/
│   │   │   ├── agents/               # Sub-agents (procedures)
│   │   │   ├── commands/             # Agent-specific commands
│   │   │   └── settings.local.json
│   │   ├── memory/                   # Agent's persistent state
│   │   │   └── context.md
│   │   ├── plans/                    # Task DAGs (platform-managed)
│   │   │   ├── active/
│   │   │   └── archive/
│   │   └── outputs/                  # Generated content
│   │
│   ├── ruby/
│   │   ├── CLAUDE.md
│   │   ├── .mcp.json.template
│   │   ├── .env.example
│   │   ├── .claude/
│   │   │   ├── agents/
│   │   │   └── commands/
│   │   ├── memory/
│   │   ├── plans/
│   │   └── outputs/
│   │
│   └── cornelius/
│       ├── CLAUDE.md
│       ├── .mcp.json.template
│       ├── .env.example
│       ├── .claude/
│       │   ├── agents/
│       │   └── commands/
│       ├── memory/
│       ├── plans/
│       └── outputs/
│
├── jobs/                             # SHARED APPROVAL PIPELINE
│   └── .gitkeep
│
└── README.md
```

### What's Different from Standalone Agents

| Standalone Agent | System Agent |
|-----------------|--------------|
| `template.yaml` at repo root | `system.yaml` at repo root (shared) |
| Agent is the whole repo | Agent is in `agents/{name}/` |
| Policies in agent's CLAUDE.md | Policies in `system/policies/` (shared) |
| No cross-agent coordination | `jobs/` folder for handoffs |

### What's the Same

- Each agent has `CLAUDE.md`, `.mcp.json.template`, `.env.example`
- Each agent has `.claude/agents/`, `.claude/commands/`
- Each agent has `memory/`, `plans/`, `outputs/`
- Platform injects `.trinity/` and `.claude/commands/trinity/` per agent
- Credential handling via `${VAR}` placeholders

---

## system.yaml Schema

The `system.yaml` replaces individual `template.yaml` files and adds system-level configuration.

```yaml
# === SYSTEM METADATA ===
name: content-production-system
version: "1.0"
description: Autonomous content creation pipeline
author: "Your Name"

# === SYSTEM-LEVEL GOVERNANCE ===
governance:
  policies_path: system/policies/
  processes_path: system/processes/

# === RESOURCE DEFAULTS ===
# Can be overridden per-agent
resources:
  cpu: "2"
  memory: "4g"

# === AGENT DEFINITIONS ===
agents:
  orchestrator:
    path: agents/orchestrator/
    display_name: "Content Orchestrator"
    role: Orchestration, review, and governance
    type: orchestrator                    # orchestrator | worker | sub-orchestrator
    execution_mode: interactive           # interactive | headless

    # Capabilities (enforced by platform)
    capabilities:
      can_modify_policies: true
      can_modify_processes: true
      can_trigger_agents: [ruby, cornelius]
      can_review_jobs: true

    # Resource overrides (optional)
    resources:
      cpu: "4"
      memory: "8g"

    # Credentials for this agent's MCP servers
    # Same schema as template.yaml credentials
    credentials:
      mcp_servers:
        github:
          env_vars: [GITHUB_TOKEN]
      env_file:
        - OPENAI_API_KEY

  ruby:
    path: agents/ruby/
    display_name: "Ruby - Content Creator"
    role: Content creation and publishing
    type: worker
    execution_mode: headless              # Triggered by orchestrator

    capabilities:
      can_modify_policies: false
      can_modify_processes: false
      writes_to_jobs: true

    credentials:
      mcp_servers:
        twitter:
          env_vars: [TWITTER_API_KEY, TWITTER_API_SECRET]

  cornelius:
    path: agents/cornelius/
    display_name: "Cornelius - Knowledge Synthesizer"
    role: Knowledge synthesis and insight extraction
    type: worker
    execution_mode: headless

    capabilities:
      can_modify_policies: false
      can_modify_processes: false
      writes_to_jobs: true

# === APPROVAL PIPELINE ===
approval:
  job_folder: jobs/
  reviewers:
    - type: human
      inbox: true                         # Human can review via UI
    - type: agent
      agent: orchestrator                 # Orchestrator can auto-review

# === SHARED FOLDERS ===
# File-based collaboration between agents in the system
shared_folders:
  # Orchestrator exposes files to workers
  orchestrator:
    expose: true
    consume: true
  # Workers expose their outputs
  ruby:
    expose: true
    consume: false
  cornelius:
    expose: true
    consume: false

# === OPTIONAL: SCHEDULED PROCESSES ===
schedules:
  weekly-content:
    process: content-production
    cron: "0 9 * * 1"
    entry_agent: orchestrator
```

---

## The Approval Pipeline

### How It Works

```
1. Orchestrator triggers worker (Claude Code headless mode)
2. Worker executes, writes output to jobs/{job-id}/
3. Job status: pending_review
4. Reviewer (human OR orchestrator) checks job folder
5. Approved → continues to next step
6. Rejected → orchestrator can:
   - Retry with modified instructions
   - Modify the sub-agent/process
   - Escalate to human
```

### Job Folder Convention

Similar pattern to existing `plans/` and `outputs/` directories:

```
jobs/{job-id}/
├── request.json          # What was asked
│   {
│     "id": "job-20251216-001",
│     "process": "content-production",
│     "step": "draft-creation",
│     "assigned_to": "ruby",
│     "triggered_by": "orchestrator",
│     "instructions": "Create blog post about X",
│     "created_at": "2025-12-16T10:00:00Z"
│   }
├── output/               # The deliverable(s)
│   └── draft.md
├── status.json           # Current state
│   {
│     "status": "pending_review",
│     "updated_at": "2025-12-16T10:15:00Z",
│     "reviewed_by": null
│   }
└── feedback.md           # Review feedback (if any)
```

**Status values**: `pending` | `in_progress` | `pending_review` | `approved` | `rejected` | `revision_requested`

### Interchangeable Reviewers

The job folder pattern allows:
- **Human review**: Check folder in UI, approve/reject
- **Orchestrator review**: Agent reads folder, evaluates quality, decides
- **Future**: Dedicated reviewer agent, automated quality checks

Same interface, different reviewers.

---

## Agent CLAUDE.md Pattern

### System Awareness Section

All agents in a system should include system awareness in their CLAUDE.md:

```markdown
# Agent Name

## System Membership
You are part of the **content-production-system**.
- System policies: `../../system/policies/`
- System processes: `../../system/processes/`
- Job handoffs: `../../jobs/`

Follow all applicable policies. Reference processes when executing workflows.
```

### Orchestrator Example

```markdown
# Orchestrator

## System Membership
You are the orchestrator of the **content-production-system**.
- System policies: `../../system/policies/` (you can modify)
- System processes: `../../system/processes/` (you can modify)
- Job handoffs: `../../jobs/`

## Your Role
You coordinate the content production system. You:
- Trigger worker agents to execute tasks
- Review their outputs for quality
- Approve, reject, or request revisions
- Modify policies and processes when needed

## System Governance
You have write access to policies and processes.
Always commit changes with clear changelog entries.

## Triggering Workers
Use Claude Code headless mode to trigger workers:
```bash
claude --headless --cwd ../ruby --message "Execute job {job-id}: {instructions}"
```

## Reviewing Jobs
Check `../../jobs/` for pending reviews:
1. Read `request.json` to understand the task
2. Review `output/` against policies
3. Update `status.json` with decision
4. Add `feedback.md` if rejecting

## Policies You Enforce
@../../system/policies/content-standards.md
@../../system/policies/approval-workflows.md
```

### Worker Example (Ruby)

```markdown
# Ruby - Content Creator

## System Membership
You are a worker in the **content-production-system**.
- System policies: `../../system/policies/` (read-only)
- System processes: `../../system/processes/` (read-only)
- Job handoffs: `../../jobs/`

## Your Role
You create content when triggered by the orchestrator.
You do NOT modify policies or processes — you follow them.

## Execution Mode
You run in headless mode, triggered by orchestrator.
Your job is to complete the assigned task and write output.

## Output Convention
Always write your work to the job folder:
1. Read your assignment from `../../jobs/{job-id}/request.json`
2. Create deliverables in `../../jobs/{job-id}/output/`
3. Update `../../jobs/{job-id}/status.json` to `pending_review`

## Policies You Follow
@../../system/policies/content-standards.md
@../../system/policies/data-handling.md

## Your Procedures
Your sub-agents define HOW you do things:
- `.claude/agents/draft-writer.md`
- `.claude/agents/editor.md`
```

---

## How Trinity Deploys a System

### Deployment Flow

```
1. Clone system repository
2. Read system.yaml
3. For each agent in system.yaml:
   a. Create container from base image
   b. Mount system repo to /home/developer/workspace/
   c. Set WORKDIR to agent's path (/home/developer/workspace/agents/{name}/)
   d. Inject platform files:
      - .trinity/prompt.md (Trinity Meta-Prompt)
   e. Inject credentials:
      - Generate .env from credential store
      - Generate .mcp.json from .mcp.json.template
   f. Inject Trinity MCP into .mcp.json
   g. Configure capabilities per system.yaml
   h. Set up shared folder mounts per system.yaml
4. Start orchestrator in interactive mode
5. Workers start in headless mode (triggered on demand)
```

### Container Layout

Follows existing Trinity conventions (see `TRINITY_COMPATIBLE_AGENT_GUIDE.md`):

```
/home/developer/
├── workspace/                          # System repo mounted here
│   ├── system.yaml
│   ├── system/
│   │   ├── policies/                   # Read-only for workers (enforced)
│   │   └── processes/                  # Read-only for workers (enforced)
│   ├── agents/
│   │   ├── orchestrator/               # Orchestrator's workdir
│   │   ├── ruby/                       # Ruby's workdir
│   │   └── cornelius/                  # Cornelius's workdir
│   └── jobs/                           # Shared approval pipeline
│
├── shared-out/                         # This agent's shared files (if expose=true)
├── shared-in/                          # Other agents' shared files (if consume=true)
│   ├── ruby/
│   └── cornelius/
│
└── .trinity/                           # Platform-injected (per agent)
    ├── prompt.md
    └── version.json
```

### Agent Working Directory

Each agent container's working directory is set to its path within the system repo:

- **Orchestrator**: `/home/developer/workspace/agents/orchestrator/`
- **Ruby**: `/home/developer/workspace/agents/ruby/`
- **Cornelius**: `/home/developer/workspace/agents/cornelius/`

This means:
- Agent's `CLAUDE.md` is found automatically
- Agent's `.claude/` is the active Claude Code config
- Relative paths to `../../system/` access policies/processes
- Relative paths to `../../jobs/` access the approval pipeline

---

## Credential Management

### Per-Agent Credentials

Each agent defines its MCP servers in `.mcp.json.template`:

```json
// agents/ruby/.mcp.json.template
{
  "mcpServers": {
    "twitter": {
      "command": "npx",
      "args": ["-y", "@anthropic/mcp-twitter"],
      "env": {
        "TWITTER_API_KEY": "${TWITTER_API_KEY}",
        "TWITTER_API_SECRET": "${TWITTER_API_SECRET}"
      }
    }
  }
}
```

### Credential Schema in system.yaml

The `system.yaml` documents which credentials each agent needs:

```yaml
agents:
  ruby:
    credentials:
      mcp_servers:
        twitter:
          env_vars: [TWITTER_API_KEY, TWITTER_API_SECRET]
```

### Injection Flow

Same as standalone agents (see `TRINITY_COMPATIBLE_AGENT_GUIDE.md`):

1. Trinity reads `system.yaml` → gets credential schema per agent
2. Parses each agent's `.mcp.json.template` → finds `${VAR}` placeholders
3. Fetches credentials from Trinity credential store
4. Generates `.env` and `.mcp.json` per agent
5. Injects Trinity MCP and Chroma MCP into each `.mcp.json`

---

## Implementation in Trinity

### Database Changes

```sql
-- Systems table
CREATE TABLE systems (
    id TEXT PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    repo_url TEXT NOT NULL,
    version TEXT,
    description TEXT,
    owner_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (owner_id) REFERENCES users(id)
);

-- Agents within systems (extends existing agent concept)
CREATE TABLE system_agents (
    id TEXT PRIMARY KEY,
    system_id TEXT NOT NULL,
    agent_name TEXT NOT NULL,
    agent_type TEXT NOT NULL,             -- orchestrator | worker | sub-orchestrator
    execution_mode TEXT NOT NULL,         -- interactive | headless
    container_id TEXT,
    status TEXT DEFAULT 'stopped',
    UNIQUE(system_id, agent_name),
    FOREIGN KEY (system_id) REFERENCES systems(id)
);

-- Jobs for approval pipeline
CREATE TABLE jobs (
    id TEXT PRIMARY KEY,
    system_id TEXT NOT NULL,
    process_name TEXT,
    step_name TEXT,
    assigned_to TEXT NOT NULL,            -- agent name
    triggered_by TEXT NOT NULL,           -- agent name or 'human'
    status TEXT DEFAULT 'pending',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (system_id) REFERENCES systems(id)
);
```

### New API Endpoints

```
# System Management
POST   /api/systems                       # Deploy system from repo
GET    /api/systems                       # List all systems
GET    /api/systems/{name}                # Get system details + agents
DELETE /api/systems/{name}                # Delete system and all agents
POST   /api/systems/{name}/pull           # Pull latest from repo

# Agent Control (within system)
POST   /api/systems/{name}/agents/{agent}/start
POST   /api/systems/{name}/agents/{agent}/stop
POST   /api/systems/{name}/agents/{agent}/trigger    # Headless execution

# Job/Approval Pipeline
GET    /api/systems/{name}/jobs                      # List jobs
GET    /api/systems/{name}/jobs/{id}                 # Get job details
POST   /api/systems/{name}/jobs/{id}/approve
POST   /api/systems/{name}/jobs/{id}/reject
```

---

## UI Considerations

### Systems Page (New)
- List all deployed systems
- Show: name, version, agent count, status
- Actions: Start All, Stop All, Pull Updates, Delete

### System Detail Page
- **Overview**: description, version, repo link
- **Agents**: list with status, type, execution mode
- **Policies**: browse policies (read-only view)
- **Processes**: browse process definitions
- **Jobs**: approval inbox with pending items

### Job Review Interface
- Show request details
- Preview output files
- Approve/Reject buttons
- Feedback text field for rejections

---

## Compatibility with Standalone Agents

Systems and standalone agents coexist:

| Feature | Standalone Agent | System Agent |
|---------|-----------------|--------------|
| Deployment | `POST /api/agents` with `template:` | `POST /api/systems` with `repo_url:` |
| Credentials | Per-agent in Trinity UI | Per-system, per-agent in system.yaml |
| Policies | In agent's CLAUDE.md | In `system/policies/` (shared) |
| Inter-agent chat | Via permissions + MCP | Same, plus job folders |
| Planning | `/trinity-plan-*` commands | Same |

A system is essentially a collection of agents with:
- Shared governance (policies, processes)
- Built-in coordination (job folders)
- Centralized definition (system.yaml)

---

## Migration Path

### Phase 1: System Repository Support
- Parse system.yaml with inline agent definitions
- Deploy all agents from single repo
- Mount system repo to all containers
- Basic system CRUD in UI

### Phase 2: Approval Pipeline
- Job folder convention
- Job tracking in database
- UI for job review
- `/trinity-job-*` commands (platform-injected)

### Phase 3: Headless Orchestration
- Orchestrator can trigger workers via headless mode
- Orchestrator can review and approve/reject jobs
- Full autonomous operation with human oversight option

### Phase 4: Advanced Features
- Sub-orchestrators (hierarchical systems)
- Cross-system job handoffs
- LATS-style retry with backtracking (see: `LATS_APPROVAL_IDEA.md`)

---

## Open Questions

1. **How does orchestrator trigger headless workers?**
   - Via Trinity MCP `trigger_agent` tool?
   - Direct `claude --headless` call?
   - Need to define the exact mechanism

2. **Job folder location**
   - Currently: `jobs/` at system root (shared)
   - Alternative: Per-agent `jobs/` with cross-mounting?
   - Shared seems simpler

3. **Read-only enforcement for workers**
   - How to enforce workers can't modify `system/policies/`?
   - File permissions? Platform validation? Trust-based?

4. **Multiple orchestrators?**
   - Hierarchical systems with sub-orchestrators
   - How do they coordinate?

---

## References

- **Trinity Compatible Agent Guide**: `docs/TRINITY_COMPATIBLE_AGENT_GUIDE.md`
- **LATS Idea**: `docs/drafts/LATS_APPROVAL_IDEA.md`
- **Process-First Architecture**: `/Users/eugene/Dropbox/Agents/process-architecture.md`
- **Trinity Multi-Agent Guide**: `docs/MULTI_AGENT_SYSTEM_GUIDE.md`
- **Shared Folders Feature**: `docs/memory/feature-flows/agent-shared-folders.md`

---

*Document version: 2.1 | Last updated: 2025-12-16*
