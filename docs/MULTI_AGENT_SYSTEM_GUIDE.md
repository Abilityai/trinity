# Multi-Agent System Guide for Trinity

> **Comprehensive guide** for designing, building, and deploying multi-agent systems on the Trinity Deep Agent Orchestration Platform.
>
> This document covers architecture patterns, communication strategies, deployment workflows, and best practices for systems where multiple agents collaborate to achieve complex goals.

---

## Table of Contents

1. [Overview](#overview)
2. [Trinity Platform Capabilities](#trinity-platform-capabilities)
3. [When to Use Multi-Agent Systems](#when-to-use-multi-agent-systems)
4. [Architecture Patterns](#architecture-patterns)
5. [System Design Process](#system-design-process)
6. [Agent Boundaries & Responsibilities](#agent-boundaries--responsibilities)
7. [Communication Strategies](#communication-strategies)
8. [Shared Folder Architecture](#shared-folder-architecture)
9. [Scheduling & Coordination](#scheduling--coordination)
10. [Permissions & Access Control](#permissions--access-control)
11. [State Management](#state-management)
12. [Repository Structure](#repository-structure)
13. [Deployment Workflow](#deployment-workflow)
14. [Observability & Monitoring](#observability--monitoring)
15. [Testing Multi-Agent Systems](#testing-multi-agent-systems)
16. [Best Practices](#best-practices)
17. [System Definition Template](#system-definition-template)
18. [Example: Content Management System](#example-content-management-system)
19. [Troubleshooting](#troubleshooting)

---

## Overview

A **multi-agent system** on Trinity is a coordinated group of specialized agents that work together to accomplish complex workflows that would be difficult for a single agent to handle effectively.

### What Makes It Different from Single Agents?

| Aspect | Single Agent | Multi-Agent System |
|--------|--------------|-------------------|
| **Scope** | One domain, one focus | Multiple domains, distributed work |
| **Communication** | Self-contained | Agents talk to each other |
| **State** | Own memory only | Shared state via folders/messages |
| **Scheduling** | Independent | Coordinated timing |
| **Failure** | Isolated impact | Cascading potential |
| **Complexity** | Linear | Exponential (interactions) |

### Key Trinity Features for Multi-Agent Systems

Trinity provides the infrastructure for multi-agent orchestration:

| Feature | Purpose in Multi-Agent Context |
|---------|-------------------------------|
| **Shared Folders** | File-based state sharing between agents |
| **Agent Permissions** | Control who can talk to whom |
| **MCP Communication** | Real-time agent-to-agent messaging |
| **Coordinated Scheduling** | Timed workflows across agents |
| **Collaboration Dashboard** | Visual monitoring of interactions |
| **Activity Stream** | Unified audit trail |

---

## Trinity Platform Capabilities

Before designing your multi-agent system, understand what Trinity provides **automatically to every agent**. These capabilities are injected at runtime and available without configuration.

### Per-Agent Infrastructure

#### 1. Vector Database (Chroma)

Every agent has a dedicated **Chroma vector database** for semantic memory storage.

| Aspect | Details |
|--------|---------|
| **Location** | `/home/developer/vector-store/` |
| **Embedding Model** | `all-MiniLM-L6-v2` (384 dimensions, pre-loaded) |
| **Persistence** | Survives agent restarts |
| **Isolation** | Each agent has its own database |

**Access Methods:**
- **Python API** (direct):
  ```python
  import chromadb
  from chromadb.utils import embedding_functions

  client = chromadb.PersistentClient(path="/home/developer/vector-store")
  ef = embedding_functions.SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")
  collection = client.get_or_create_collection("memory", embedding_function=ef)

  # Store
  collection.add(documents=["Important context"], ids=["doc1"], metadatas=[{"type": "context"}])

  # Query
  results = collection.query(query_texts=["What do I know about X?"], n_results=5)
  ```

- **MCP Tools** (auto-injected):
  ```
  mcp__chroma__list_collections()
  mcp__chroma__create_collection(name, metadata)
  mcp__chroma__add_documents(collection, documents, ids, metadatas)
  mcp__chroma__query_collection(collection, query_texts, n_results)
  mcp__chroma__get_documents(collection, ids)
  mcp__chroma__delete_documents(collection, ids)
  mcp__chroma__update_documents(collection, ids, documents, metadatas)
  mcp__chroma__peek_collection(collection, limit)
  mcp__chroma__count_collection(collection)
  mcp__chroma__delete_collection(name)
  mcp__chroma__get_collection_info(name)
  mcp__chroma__modify_collection(name, metadata)
  ```

**Multi-Agent Use Cases:**
- Each agent builds domain-specific memory
- Agents can query their own history ("Have I solved this before?")
- Long-term learning across sessions
- Semantic search over past interactions

#### 2. Scheduling System (Autonomy)

Trinity provides **platform-managed scheduling** for autonomous agent execution.

| Aspect | Details |
|--------|---------|
| **Engine** | APScheduler with AsyncIO |
| **Format** | 5-field cron expressions |
| **Timezone** | Configurable per schedule (UTC, America/Los_Angeles, etc.) |
| **Management** | UI (Schedules tab) or API |

**Capabilities:**
- Create multiple schedules per agent
- Enable/disable individual schedules
- Manual trigger for testing
- Execution history with logs
- WebSocket notifications on execution

**API Endpoints:**
```bash
# Create schedule
POST /api/agents/{name}/schedules
{
  "name": "Hourly Check",
  "cron_expression": "0 * * * *",
  "message": "Run hourly coordination task",
  "timezone": "America/Los_Angeles",
  "enabled": true
}

# List schedules
GET /api/agents/{name}/schedules

# Manual trigger
POST /api/agents/{name}/schedules/{id}/trigger

# View executions
GET /api/agents/{name}/schedules/{id}/executions
```

**Multi-Agent Coordination Pattern:**
```
Orchestrator: "0 * * * *"      (hourly at :00)
Worker A:     "5,35 * * * *"   (at :05 and :35)
Worker B:     "10,40 * * * *"  (at :10 and :40)
Worker C:     "15,45 * * * *"  (at :15 and :45)
```

#### 3. Workplan System (Task DAGs)

Trinity injects a **planning system** for persistent task tracking outside the context window.

| Aspect | Details |
|--------|---------|
| **Storage** | YAML files in `plans/active/` and `plans/archive/` |
| **Commands** | `/trinity-plan-create`, `/trinity-plan-status`, `/trinity-plan-update`, `/trinity-plan-list` |
| **States** | pending → active → completed/failed, blocked (for dependencies) |
| **Visibility** | Plans tab in UI, aggregate API |

**Why It Matters for Multi-Agent Systems:**
- Plans persist across context resets
- Multiple agents can reference the same plan
- Human oversight of agent reasoning
- Recovery from failures with plan state intact
- Cross-agent task coordination

**Plan File Format:**
```yaml
id: "plan-abc123"
name: "Multi-agent research project"
status: "active"
goal: "Research and synthesize information on topic X"

tasks:
  - id: "task-001"
    name: "Gather sources"
    status: "completed"
    assigned_to: "researcher-agent"

  - id: "task-002"
    name: "Analyze findings"
    status: "active"
    assigned_to: "analyst-agent"
    depends_on: ["task-001"]

  - id: "task-003"
    name: "Write synthesis"
    status: "blocked"
    assigned_to: "writer-agent"
    depends_on: ["task-002"]
```

#### 4. Trinity MCP Tools (Agent-to-Agent)

Every agent gets **Trinity MCP tools** auto-injected for inter-agent communication.

| Tool | Purpose |
|------|---------|
| `mcp__trinity__list_agents` | Discover available agents |
| `mcp__trinity__get_agent` | Get agent status and details |
| `mcp__trinity__chat_with_agent` | Send message and get response |
| `mcp__trinity__get_chat_history` | Retrieve conversation history |
| `mcp__trinity__start_agent` | Start a stopped agent |
| `mcp__trinity__stop_agent` | Stop a running agent |
| `mcp__trinity__create_agent` | Spawn new agent from template |
| `mcp__trinity__delete_agent` | Remove an agent |
| `mcp__trinity__reload_credentials` | Hot-reload credentials |
| `mcp__trinity__get_credential_status` | Check credential files |
| `mcp__trinity__get_agent_logs` | View container logs |
| `mcp__trinity__list_templates` | List available templates |

**Example - Agent Delegation:**
```python
# Orchestrator delegates to content agent
response = mcp__trinity__chat_with_agent(
    agent_name="ruby-content",
    message="Scan for new content and update the inventory. Report findings."
)
```

#### 5. Shared Folders

File-based collaboration via **Docker volumes**.

| Path | Purpose |
|------|---------|
| `/home/developer/shared-out/` | Your agent's shared folder (others read) |
| `/home/developer/shared-in/{agent}/` | Other agents' folders (you read) |

**Configuration:**
- Enable via UI (Shared Folders tab) or API
- Requires agent restart to apply
- Permission-gated (only permitted agents can mount)

#### 6. Credential Hot-Reload

Update secrets **without restarting** the agent.

```bash
# Via API
POST /api/agents/{name}/credentials/hot-reload
Content-Type: text/plain

API_KEY=new-value
ANOTHER_KEY=another-value
```

**Multi-Agent Benefit:** Update all agents' credentials centrally without disrupting running workflows.

### Platform-Level Features

#### 7. Collaboration Dashboard

Real-time visualization at `/` showing:
- All agents as draggable nodes
- Animated connections during communication
- Context usage bars per agent
- Activity state (Active/Idle/Offline)
- Replay mode for historical analysis

#### 8. Activity Stream

Unified audit trail across all agents:
- Tool calls with timing
- Agent-to-agent communications
- Schedule executions
- Chat sessions

**API:**
```bash
GET /api/activities/timeline?activity_types=agent_collaboration&limit=100
```

#### 9. Context Tracking

Monitor context window usage per agent:
- Visual progress bars in dashboard
- Color-coded warnings (green → yellow → orange → red)
- API endpoint: `GET /api/agents/context-stats`

#### 10. Custom Metrics

Define agent-specific KPIs in `template.yaml`:

```yaml
metrics:
  - name: posts_published
    type: counter
    label: "Posts Published"
  - name: engagement_rate
    type: percentage
    label: "Engagement Rate"
  - name: agent_status
    type: status
    label: "Status"
    values:
      - value: "active"
        color: "green"
      - value: "idle"
        color: "gray"
```

Agent writes to `workspace/metrics.json`, Trinity displays in UI.

#### 11. Git Sync

Bidirectional GitHub synchronization for agents from GitHub templates:
- Push workspace changes to working branch
- Pull updates from template
- Track sync status in UI

### Capability Summary Table

| Capability | Scope | Access | Multi-Agent Benefit |
|------------|-------|--------|---------------------|
| **Vector DB** | Per-agent | Python API or MCP | Each agent has semantic memory |
| **Scheduling** | Per-agent | UI or API | Coordinated autonomous execution |
| **Workplans** | Per-agent | Slash commands | Persistent task tracking across context resets |
| **Trinity MCP** | Cross-agent | MCP tools | Agent delegation and collaboration |
| **Shared Folders** | Cross-agent | File system | Large data exchange, state sharing |
| **Credentials** | Per-agent | Hot-reload API | Update secrets without restart |
| **Dashboard** | System-wide | Web UI | Visual monitoring of all interactions |
| **Activity Stream** | System-wide | API | Unified audit trail |
| **Context Tracking** | Per-agent | Dashboard/API | Monitor resource usage |
| **Custom Metrics** | Per-agent | template.yaml | Domain-specific KPIs |
| **Git Sync** | Per-agent | UI/API | Version control of agent state |

### Design Implications

When designing your multi-agent system, consider:

1. **Use Vector Memory for Learning** — Each agent can build up domain knowledge that persists
2. **Use Scheduling for Autonomy** — Agents can run without human triggers
3. **Use Workplans for Complex Tasks** — Don't rely on context window for multi-step work
4. **Use Shared Folders for Data** — Large files, state, inventories
5. **Use MCP Chat for Urgency** — Time-sensitive coordination, questions
6. **Use Custom Metrics for Visibility** — Track domain-specific KPIs
7. **Use Context Tracking** — Know when agents need session resets

---

## When to Use Multi-Agent Systems

### Good Use Cases

| Pattern | Example | Why Multi-Agent? |
|---------|---------|------------------|
| **Pipeline** | Content creation → Review → Publishing | Each stage needs different expertise |
| **Orchestrator-Workers** | Manager + specialized executors | Coordination overhead is worth the specialization |
| **Parallel Processing** | Multiple researchers feeding into synthesis | Speed through parallelism |
| **Domain Separation** | Sales data + Marketing data + Finance data | Different data access, compliance needs |
| **Human-in-the-Loop** | Draft → Review → Approve | Clear handoff points |

### When NOT to Use Multi-Agent

| Situation | Better Approach |
|-----------|-----------------|
| Simple linear tasks | Single agent with good instructions |
| Small scope | One agent with multiple tools |
| Tight latency requirements | Single agent (no coordination overhead) |
| Learning/experimentation | Start with one agent, split later |

**Rule of Thumb**: Start with a single agent. Split into multiple only when you have clear evidence of:
- Distinct domains requiring different expertise
- Parallelization opportunities
- Access control requirements
- Complexity exceeding single-agent manageability

---

## Architecture Patterns

### Pattern 1: Orchestrator-Workers (Hub-and-Spoke)

```
                    ┌─────────────────────┐
                    │    ORCHESTRATOR     │
                    │  (Coordination)     │
                    └──────────┬──────────┘
                               │
              ┌────────────────┼────────────────┐
              │                │                │
              ▼                ▼                ▼
┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐
│    WORKER A     │  │    WORKER B     │  │    WORKER C     │
│  (Specialty 1)  │  │  (Specialty 2)  │  │  (Specialty 3)  │
└─────────────────┘  └─────────────────┘  └─────────────────┘
```

**Characteristics:**
- Central coordinator owns the schedule and state
- Workers are specialized and stateless (regarding coordination)
- Orchestrator reads from all workers, workers read from orchestrator
- Clear hierarchy, simple to reason about

**Best For:**
- Content management systems
- Multi-step workflows with clear stages
- Systems needing central monitoring/control

**Example:** Ruby Content System
- `ruby-orchestrator`: Owns schedule, health monitoring
- `ruby-content`: Content discovery and publishing
- `ruby-engagement`: Social monitoring and replies

---

### Pattern 2: Pipeline (Sequential)

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│  INGESTER   │────►│  PROCESSOR  │────►│  REVIEWER   │────►│  PUBLISHER  │
└─────────────┘     └─────────────┘     └─────────────┘     └─────────────┘
```

**Characteristics:**
- Linear flow of data/work
- Each stage transforms and passes forward
- Output of one is input to next
- Each agent only talks to adjacent agents

**Best For:**
- Data processing pipelines
- Content transformation workflows
- Approval workflows

**Shared Folder Flow:**
```
ingester/shared-out/queue.json
    → processor reads, processes
    → processor/shared-out/processed.json
        → reviewer reads, reviews
        → reviewer/shared-out/approved.json
            → publisher reads, publishes
```

---

### Pattern 3: Mesh (Peer-to-Peer)

```
┌─────────────┐◄────────────►┌─────────────┐
│   AGENT A   │              │   AGENT B   │
└──────┬──────┘              └──────┬──────┘
       │                            │
       │      ┌─────────────┐       │
       └─────►│   AGENT C   │◄──────┘
              └─────────────┘
```

**Characteristics:**
- All agents can talk to all others
- No central coordinator
- Emergent coordination through protocols
- More complex, harder to debug

**Best For:**
- Peer collaboration systems
- Research teams (agents debate/synthesize)
- Systems where any agent might initiate work

**Warning:** Mesh topologies can become chaotic. Define clear protocols for when/why agents contact each other.

---

### Pattern 4: Hierarchical (Multi-Level)

```
                    ┌─────────────────────┐
                    │   MASTER ORCH       │
                    └──────────┬──────────┘
                               │
              ┌────────────────┴────────────────┐
              ▼                                 ▼
┌─────────────────────────┐       ┌─────────────────────────┐
│    SUB-ORCHESTRATOR A   │       │    SUB-ORCHESTRATOR B   │
└────────────┬────────────┘       └────────────┬────────────┘
             │                                  │
    ┌────────┼────────┐               ┌────────┼────────┐
    ▼        ▼        ▼               ▼        ▼        ▼
┌───────┐┌───────┐┌───────┐     ┌───────┐┌───────┐┌───────┐
│WORKER1││WORKER2││WORKER3│     │WORKER4││WORKER5││WORKER6│
└───────┘└───────┘└───────┘     └───────┘└───────┘└───────┘
```

**Characteristics:**
- Multiple levels of coordination
- Sub-orchestrators manage worker groups
- Master orchestrator coordinates sub-orchestrators
- Good for large systems with distinct sub-domains

**Best For:**
- Large enterprise systems
- Multi-department workflows
- Systems with >5-6 agents

---

## System Design Process

### Step 1: Define the Goal

Start with a clear statement of what the system should accomplish:

```markdown
## System Goal
Automate content creation, scheduling, and publishing across social media
platforms while monitoring engagement and optimizing based on performance.
```

### Step 2: Identify Domains

Break down the goal into distinct domains of expertise:

| Domain | Responsibilities | Expertise Required |
|--------|------------------|-------------------|
| **Coordination** | Scheduling, health monitoring, conflict resolution | Project management |
| **Content** | Discovery, creation, formatting | Content expertise |
| **Publishing** | Platform APIs, timing, formatting | Platform knowledge |
| **Engagement** | Replies, comments, community | Social skills |
| **Analytics** | Performance tracking, insights | Data analysis |

### Step 3: Map to Agents

Decide how many agents and what each owns:

```markdown
## Agent Constellation

| Agent | Domains Covered | Rationale |
|-------|-----------------|-----------|
| orchestrator | Coordination | Single source of truth for schedule |
| content | Content + Publishing | Tight coupling, same platform APIs |
| engagement | Engagement + Analytics | Related social functions |
```

**Consolidation Rule:** Merge domains that:
- Share credentials/APIs
- Have high communication frequency
- Don't benefit from independent scaling

### Step 4: Define Interfaces

For each agent pair, define how they communicate:

```markdown
## Communication Matrix

| From → To | Method | Data Exchanged |
|-----------|--------|----------------|
| orchestrator → content | Shared folder | schedule.json, directives.json |
| orchestrator → engagement | Shared folder | schedule.json, targets.json |
| content → orchestrator | Shared folder | content_inventory.json, publish_status.json |
| engagement → orchestrator | Shared folder | engagement_stats.json |
| content ↔ engagement | Shared folder | Cross-read for coordination |
| Any → Any (urgent) | MCP chat | Real-time requests |
```

### Step 5: Design State Flow

Document how state moves through the system:

```markdown
## State Flow

1. **Initialization**
   - Orchestrator creates weekly_plan.json
   - Workers read plan on startup

2. **Content Discovery Cycle**
   - Content scans sources → updates content_inventory.json
   - Orchestrator reads inventory → updates schedule.json

3. **Publishing Cycle**
   - Content reads schedule → publishes due items → updates publish_status.json
   - Orchestrator reads status → confirms completion

4. **Engagement Cycle**
   - Engagement monitors platforms → updates engagement_stats.json
   - Orchestrator reads stats → adjusts future scheduling
```

---

## Agent Boundaries & Responsibilities

### Principle: Single Responsibility per Agent

Each agent should have ONE primary job. If you're struggling to describe what an agent does in one sentence, it might be doing too much.

**Good:**
- "Discovers and classifies new content"
- "Publishes scheduled posts to platforms"
- "Monitors and responds to engagement"

**Bad:**
- "Handles all content stuff" (too vague)
- "Manages the database and also does analytics and sometimes publishes" (too many things)

### Boundary Checklist

For each agent, you should be able to answer:

- [ ] **What is this agent's ONE primary job?**
- [ ] **What data does it OWN (write)?**
- [ ] **What data does it READ (from others)?**
- [ ] **What triggers it to act?** (Schedule, message, file change)
- [ ] **What does it produce?** (Files, messages, external actions)
- [ ] **What does it NOT do?** (Explicit boundaries)

### Example: Ruby Content Agent

```markdown
## Ruby Content Agent

### Primary Job
Content discovery, production, and publishing

### Owns (Writes)
- content/inventory.json
- content/new_arrivals.json
- publishing/post_log.json
- publishing/failed_posts.json

### Reads (From Others)
- orchestrator: schedule.json, weekly_plan.json
- engagement: engagement_stats.json (for content strategy)

### Triggers
- Schedule: Content scan every 15 minutes
- Schedule: Publishing check every 15 minutes

### Produces
- Updated content inventory
- Executed posts (logged)
- Status updates for orchestrator

### Does NOT Do
- Manage the schedule (orchestrator's job)
- Respond to comments (engagement's job)
- Monitor system health (orchestrator's job)
```

---

## Communication Strategies

Trinity provides two primary communication mechanisms. Use the right one for each situation.

### Strategy 1: Shared Folders (Recommended for Most Cases)

**Best For:**
- State sharing (inventories, schedules, status)
- Batch data handoffs
- Asynchronous coordination
- Audit trails (files are persistent)

**Advantages:**
- Persistent (survives restarts)
- Inspectable (browse via UI)
- No timing dependencies
- Natural checkpointing

**How It Works:**
```
Agent A writes to: /home/developer/shared-out/data.json
Agent B reads from: /home/developer/shared-in/agent-a/data.json
```

**Pattern: Status Files**
```json
// orchestrator/shared-out/schedule.json
{
  "updated_at": "2025-12-14T10:00:00Z",
  "items": [
    {"id": "post-1", "due": "2025-12-14T12:00:00Z", "status": "pending"},
    {"id": "post-2", "due": "2025-12-14T14:00:00Z", "status": "pending"}
  ]
}
```

```json
// content/shared-out/publish_status.json
{
  "updated_at": "2025-12-14T12:05:00Z",
  "completed": ["post-1"],
  "pending": ["post-2"],
  "failed": []
}
```

### Strategy 2: MCP Chat (For Real-Time Requests)

**Best For:**
- Urgent requests
- Interactive coordination
- One-off questions
- Triggering immediate action

**Advantages:**
- Real-time response
- Conversational context
- Good for complex requests

**Disadvantages:**
- Ephemeral (not persisted by default)
- Requires both agents running
- Higher latency than file read

**Example:**
```python
# Orchestrator urgently needs content agent to prioritize a post
mcp__trinity__chat_with_agent(
    agent_name="ruby-content",
    message="URGENT: Prioritize publishing post-123 immediately. Breaking news context."
)
```

### Choosing Between Them

| Scenario | Use |
|----------|-----|
| Regular status updates | Shared Folder |
| Schedule distribution | Shared Folder |
| Inventory sharing | Shared Folder |
| Urgent override | MCP Chat |
| Complex question | MCP Chat |
| Notification (fire-and-forget) | MCP Chat |
| Data that needs persistence | Shared Folder |

### Hybrid Pattern

Many systems use both:

1. **Shared folders** for state and data exchange
2. **MCP chat** for notifications and urgent requests

```python
# Orchestrator detects schedule conflict
# 1. Update shared folder with resolution
with open('/home/developer/shared-out/directives/urgent.json', 'w') as f:
    json.dump({"action": "delay_post", "post_id": "post-123", "reason": "conflict"}, f)

# 2. Notify content agent to read it immediately
mcp__trinity__chat_with_agent(
    agent_name="ruby-content",
    message="Check directives/urgent.json for schedule update."
)
```

---

## Shared Folder Architecture

### Folder Layout

Each agent in a multi-agent system should have a well-defined shared folder structure:

```
/home/developer/
├── shared-out/                    # THIS AGENT'S OUTPUT (others read)
│   ├── status.json                # Quick health status
│   ├── domain/                    # Domain-specific outputs
│   │   ├── primary_data.json
│   │   └── secondary_data.json
│   └── notifications/             # Events for other agents
│       └── events.json
│
├── shared-in/                     # OTHER AGENTS' OUTPUT (this agent reads)
│   ├── agent-a/                   # Agent A's shared-out mounted here
│   │   └── ...
│   └── agent-b/                   # Agent B's shared-out mounted here
│       └── ...
│
└── workspace/                     # Agent's private workspace
    └── ...
```

### File Contracts

Define explicit contracts for shared files:

```markdown
## File: schedule.json

**Owner:** orchestrator
**Readers:** content, engagement
**Update Frequency:** Every 10 minutes or on change
**Format:**

```json
{
  "version": "1.0",
  "updated_at": "ISO8601 timestamp",
  "week_start": "2025-12-09",
  "items": [
    {
      "id": "string (unique)",
      "type": "post|reply|newsletter",
      "due_at": "ISO8601 timestamp",
      "assigned_to": "agent-name",
      "status": "pending|in_progress|completed|failed",
      "priority": 1-5,
      "metadata": {}
    }
  ]
}
```

**Contract:**
- Orchestrator is the ONLY writer
- Items are never deleted, only status changes
- Readers should handle missing fields gracefully
- Maximum 1000 items (archive older)
```

### Recommended File Structure by Agent Type

**Orchestrator Agent:**
```
shared-out/
├── schedule.json           # Master schedule (all items)
├── weekly_plan.json        # Strategy for the week
├── directives/             # Commands to workers
│   ├── priorities.json     # Current priorities
│   └── urgent.json         # Urgent overrides
├── monitoring/
│   ├── health_status.json  # System health
│   └── alerts.json         # Active alerts
└── config/
    └── settings.json       # System-wide settings
```

**Worker Agent:**
```
shared-out/
├── status.json             # Agent's own status
├── domain/                 # Domain-specific data
│   ├── inventory.json      # Items this agent manages
│   ├── queue.json          # Work queue
│   └── results.json        # Completed work
└── logs/
    └── activity_log.json   # Audit trail
```

---

## Scheduling & Coordination

### Avoiding Collisions

When multiple agents run scheduled tasks, coordinate to avoid:
- **Resource contention** (hitting same API simultaneously)
- **Race conditions** (reading file another agent is writing)
- **Thundering herd** (all agents waking at :00)

### Scheduling Patterns

**Pattern 1: Staggered Schedules**
```
:00  → orchestrator (coordination)
:05  → content (scan)
:10  → engagement (monitor)
:15  → content (publish)
:20  → orchestrator (health check)
:25  → engagement (reply hunt)
...
```

**Pattern 2: Leader-Follower**
```
1. Orchestrator runs at :00, writes directive
2. Workers run at :05, read directive, execute
3. Workers complete, update status files
4. Orchestrator runs at :10, reads status, plans next cycle
```

**Pattern 3: Event-Driven (via MCP)**
```
1. Content discovers new item, writes to shared folder
2. Content notifies orchestrator via MCP
3. Orchestrator evaluates, updates schedule
4. Orchestrator notifies content of new schedule
```

### Schedule Configuration Example

```yaml
# From ruby-content-system-definition.md
schedules:
  orchestrator:
    - name: "Hourly Coordination"
      cron: "0 * * * *"           # Every hour at :00
      purpose: "Full system coordination"
    - name: "Quick Status Check"
      cron: "*/10 * * * *"        # Every 10 minutes
      purpose: "Health monitoring"

  content:
    - name: "Content Scan"
      cron: "5,20,35,50 * * * *"  # 4x/hour, offset from orchestrator
      purpose: "Discover new content"
    - name: "Publishing Check"
      cron: "*/15 * * * *"        # Every 15 minutes
      purpose: "Check for posts due"

  engagement:
    - name: "Engagement Monitor"
      cron: "*/5 * * * *"         # Every 5 minutes
      purpose: "Monitor engagement"
    - name: "Reply Opportunities"
      cron: "10,40 * * * *"       # 2x/hour
      purpose: "Find viral content"
```

### Coordination Best Practices

1. **Offset schedules** - Don't have all agents wake at :00
2. **Leader writes, followers read** - Clear ownership of files
3. **Idempotent operations** - Safe to run multiple times
4. **Timestamp everything** - Know when files were last updated
5. **Check before acting** - Read latest state before writing

---

## Permissions & Access Control

### Setting Up Permissions

For a multi-agent system, grant bidirectional permissions at creation:

```bash
# After creating all agents, grant permissions
# Using Trinity API:

# Orchestrator can call content and engagement
POST /api/agents/orchestrator/permissions
{"target_agent": "content"}

POST /api/agents/orchestrator/permissions
{"target_agent": "engagement"}

# Content can call orchestrator and engagement
POST /api/agents/content/permissions
{"target_agent": "orchestrator"}

POST /api/agents/content/permissions
{"target_agent": "engagement"}

# Engagement can call orchestrator and content
POST /api/agents/engagement/permissions
{"target_agent": "orchestrator"}

POST /api/agents/engagement/permissions
{"target_agent": "content"}
```

### Permission Topologies

**Full Mesh (All can talk to all):**
```
orchestrator ↔ content
orchestrator ↔ engagement
content ↔ engagement
```

**Hub-and-Spoke (Only through orchestrator):**
```
orchestrator ↔ content
orchestrator ↔ engagement
(no direct content ↔ engagement)
```

**Pipeline (Sequential only):**
```
ingester → processor → reviewer → publisher
```

### Permission Matrix Template

| Source ↓ / Target → | orchestrator | content | engagement |
|---------------------|--------------|---------|------------|
| **orchestrator** | — | ✅ | ✅ |
| **content** | ✅ | — | ✅ |
| **engagement** | ✅ | ✅ | — |

---

## State Management

### Principles

1. **Single Source of Truth**: Each piece of state has ONE owner
2. **Explicit Ownership**: Document who writes what
3. **Versioning**: Include timestamps and version numbers
4. **Eventual Consistency**: Accept that reads may be slightly stale
5. **Conflict Resolution**: Define who wins when there's disagreement

### State Ownership Table

| State | Owner | Location | Update Frequency |
|-------|-------|----------|------------------|
| Master schedule | orchestrator | schedule.json | On change |
| Content inventory | content | content_inventory.json | Every 15 min |
| Publish status | content | publish_status.json | After each publish |
| Engagement stats | engagement | engagement_stats.json | Every 5 min |
| System health | orchestrator | health_status.json | Every 10 min |

### Handling Conflicts

**Scenario:** Two agents try to claim the same task

**Resolution Strategy:**
```yaml
# In orchestrator/shared-out/schedule.json
items:
  - id: "post-123"
    status: "pending"
    claimed_by: null
    claimed_at: null

# Content agent wants to claim:
1. Read schedule.json
2. Check if claimed_by is null
3. If null, write claim to own status file
4. Notify orchestrator
5. Orchestrator validates and updates schedule.json with claimed_by

# If two agents claim simultaneously:
# Orchestrator resolves: first claim timestamp wins
# Loser agent sees updated schedule on next read
```

### Checkpointing

For long-running workflows, checkpoint progress:

```json
// content/shared-out/checkpoint.json
{
  "workflow": "weekly_content_creation",
  "started_at": "2025-12-14T00:00:00Z",
  "current_step": 3,
  "steps_completed": ["discover", "classify", "draft"],
  "steps_remaining": ["review", "publish"],
  "last_checkpoint": "2025-12-14T10:30:00Z",
  "can_resume_from": "draft"
}
```

---

## Repository Structure

### One Repository Per Agent

Each agent in the system should have its own GitHub repository:

```
github.com/YourOrg/
├── system-orchestrator/     # Orchestrator agent
├── system-content/          # Content agent
├── system-engagement/       # Engagement agent
└── system-docs/             # (Optional) System documentation
```

### Repository Contents

Each repository follows the [Trinity Compatible Agent Guide](TRINITY_COMPATIBLE_AGENT_GUIDE.md):

```
my-agent/
├── template.yaml            # Trinity metadata
├── CLAUDE.md                # Agent instructions
├── .mcp.json.template       # MCP config with placeholders
├── .env.example             # Required credentials
├── .gitignore               # Excludes secrets + platform dirs
├── memory/                  # Agent's persistent state
├── scripts/                 # Helper scripts
└── README.md                # Human documentation
```

### System Documentation Repository (Optional)

For complex systems, maintain a separate documentation repository:

```
system-docs/
├── README.md                # System overview
├── architecture.md          # Architecture diagrams
├── system-definition.md     # Complete system definition
├── runbooks/
│   ├── deployment.md        # How to deploy
│   ├── troubleshooting.md   # Common issues
│   └── recovery.md          # Disaster recovery
└── schemas/
    ├── schedule.schema.json # JSON schemas for contracts
    └── status.schema.json
```

---

## Deployment Workflow

### Step 1: Create Agents in Order

For orchestrator-worker patterns, create the orchestrator first:

```bash
# 1. Create orchestrator (owns shared folders others will read)
POST /api/agents
{
  "name": "ruby-orchestrator",
  "template": "github:YourOrg/ruby-orchestrator"
}

# 2. Wait for orchestrator to start and expose shared folder
# Verify: GET /api/agents/ruby-orchestrator shows status: running

# 3. Create workers
POST /api/agents
{
  "name": "ruby-content",
  "template": "github:YourOrg/ruby-content"
}

POST /api/agents
{
  "name": "ruby-engagement",
  "template": "github:YourOrg/ruby-engagement"
}
```

### Step 2: Configure Shared Folders

Enable shared folders for each agent:

```bash
# Orchestrator exposes, doesn't consume (it's the leader)
PUT /api/agents/ruby-orchestrator/folders
{"expose_enabled": true, "consume_enabled": false}

# Workers both expose and consume
PUT /api/agents/ruby-content/folders
{"expose_enabled": true, "consume_enabled": true}

PUT /api/agents/ruby-engagement/folders
{"expose_enabled": true, "consume_enabled": true}
```

### Step 3: Grant Permissions

```bash
# Full mesh permissions for the Ruby system
# (Do this for all pairs that need to communicate)

# Orchestrator permissions
POST /api/agents/ruby-orchestrator/permissions
{"target_agent": "ruby-content"}
POST /api/agents/ruby-orchestrator/permissions
{"target_agent": "ruby-engagement"}

# Content permissions
POST /api/agents/ruby-content/permissions
{"target_agent": "ruby-orchestrator"}
POST /api/agents/ruby-content/permissions
{"target_agent": "ruby-engagement"}

# Engagement permissions
POST /api/agents/ruby-engagement/permissions
{"target_agent": "ruby-orchestrator"}
POST /api/agents/ruby-engagement/permissions
{"target_agent": "ruby-content"}
```

### Step 4: Restart Agents (Apply Folder Mounts)

```bash
# Shared folder changes require restart
POST /api/agents/ruby-orchestrator/stop
POST /api/agents/ruby-orchestrator/start

POST /api/agents/ruby-content/stop
POST /api/agents/ruby-content/start

POST /api/agents/ruby-engagement/stop
POST /api/agents/ruby-engagement/start
```

### Step 5: Configure Schedules

```bash
# Orchestrator schedules
POST /api/agents/ruby-orchestrator/schedules
{
  "name": "Hourly Coordination",
  "cron_expression": "0 * * * *",
  "message": "Run hourly coordination: check all agent status, update schedule, resolve conflicts",
  "timezone": "America/Los_Angeles",
  "enabled": true
}

# Content schedules
POST /api/agents/ruby-content/schedules
{
  "name": "Content Scan",
  "cron_expression": "5,20,35,50 * * * *",
  "message": "Scan for new content and update inventory",
  "timezone": "America/Los_Angeles",
  "enabled": true
}

# ... etc for all schedules
```

### Step 6: Verify System

```bash
# Check all agents running
GET /api/agents
# Should show all 3 agents with status: running

# Check shared folders accessible
docker exec agent-ruby-content ls /home/developer/shared-in/
# Should show: ruby-orchestrator  ruby-engagement

# Check permissions
GET /api/agents/ruby-content/permissions
# Should show permissions to orchestrator and engagement
```

### Deployment Script Template

```bash
#!/bin/bash
# deploy-multi-agent-system.sh

AGENTS=("ruby-orchestrator" "ruby-content" "ruby-engagement")
TEMPLATES=("github:YourOrg/ruby-orchestrator" "github:YourOrg/ruby-content" "github:YourOrg/ruby-engagement")
API_URL="http://localhost:8000"
TOKEN="your-jwt-token"

# Create agents
for i in "${!AGENTS[@]}"; do
  echo "Creating ${AGENTS[$i]}..."
  curl -X POST "$API_URL/api/agents" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d "{\"name\": \"${AGENTS[$i]}\", \"template\": \"${TEMPLATES[$i]}\"}"
  sleep 10  # Wait for agent to start
done

# Configure shared folders
for agent in "${AGENTS[@]}"; do
  echo "Configuring shared folders for $agent..."
  curl -X PUT "$API_URL/api/agents/$agent/folders" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"expose_enabled": true, "consume_enabled": true}'
done

# Grant full mesh permissions
for source in "${AGENTS[@]}"; do
  for target in "${AGENTS[@]}"; do
    if [ "$source" != "$target" ]; then
      echo "Granting $source -> $target permission..."
      curl -X POST "$API_URL/api/agents/$source/permissions" \
        -H "Authorization: Bearer $TOKEN" \
        -H "Content-Type: application/json" \
        -d "{\"target_agent\": \"$target\"}"
    fi
  done
done

# Restart all agents to apply folder mounts
for agent in "${AGENTS[@]}"; do
  echo "Restarting $agent..."
  curl -X POST "$API_URL/api/agents/$agent/stop" -H "Authorization: Bearer $TOKEN"
  sleep 5
  curl -X POST "$API_URL/api/agents/$agent/start" -H "Authorization: Bearer $TOKEN"
  sleep 10
done

echo "System deployed!"
```

---

## Observability & Monitoring

### Trinity Dashboard

Use the Collaboration Dashboard (`/`) to visualize:
- Agent status (running/stopped)
- Active collaborations (animated connections)
- Context usage per agent
- Real-time activity

### Monitoring Checklist

| Check | How | Frequency |
|-------|-----|-----------|
| All agents running | Dashboard or `GET /api/agents` | Continuous |
| Schedules executing | Agent → Schedules tab | After each scheduled run |
| Shared folders updating | Agent → Files tab | Every 15 min |
| No error patterns | Agent → Logs tab | Hourly |
| Context not exhausted | Dashboard context bars | Continuous |

### Health Check Pattern

Orchestrator should implement a health check routine:

```python
# Orchestrator health check (pseudo-code)
def run_health_check():
    health = {"timestamp": now(), "agents": {}}

    for agent in ["ruby-content", "ruby-engagement"]:
        # Check shared folder freshness
        status_file = f"/home/developer/shared-in/{agent}/status.json"
        if exists(status_file):
            mtime = get_mtime(status_file)
            age_minutes = (now() - mtime).minutes
            health["agents"][agent] = {
                "status": "healthy" if age_minutes < 30 else "stale",
                "last_update_minutes_ago": age_minutes
            }
        else:
            health["agents"][agent] = {"status": "no_status_file"}

    write_json("/home/developer/shared-out/health_status.json", health)

    # Alert if issues
    for agent, info in health["agents"].items():
        if info["status"] != "healthy":
            log_alert(f"Agent {agent} health issue: {info}")
```

### Alerting Patterns

```json
// orchestrator/shared-out/monitoring/alert_log.json
{
  "alerts": [
    {
      "id": "alert-001",
      "timestamp": "2025-12-14T10:30:00Z",
      "severity": "warning",
      "agent": "ruby-content",
      "message": "Status file not updated in 45 minutes",
      "resolved": false,
      "resolved_at": null
    }
  ]
}
```

---

## Testing Multi-Agent Systems

### Test Levels

1. **Unit Testing**: Each agent in isolation
2. **Integration Testing**: Two agents communicating
3. **System Testing**: Full system end-to-end

### Unit Testing (Per Agent)

Test each agent works correctly with mocked shared folders:

```bash
# 1. Create agent locally
# 2. Create mock shared-in folders
mkdir -p shared-in/mock-orchestrator
echo '{"items": []}' > shared-in/mock-orchestrator/schedule.json

# 3. Run agent tasks
# 4. Verify shared-out contents
```

### Integration Testing

Test pairs of agents:

```markdown
## Test: Orchestrator → Content Communication

**Setup:**
1. Deploy orchestrator and content agents
2. Configure shared folders and permissions

**Steps:**
1. Orchestrator writes schedule with one item
2. Wait 5 minutes
3. Content should read schedule and update publish_status.json

**Verify:**
- [ ] content/shared-out/publish_status.json exists
- [ ] Status reflects the scheduled item
- [ ] No errors in either agent's logs
```

### System Testing

Test the full workflow end-to-end:

```markdown
## Test: Full Content Pipeline

**Setup:**
1. Deploy all agents
2. Configure all permissions and folders
3. Enable all schedules

**Steps:**
1. Place test content in content agent's workspace
2. Trigger content scan
3. Wait for orchestrator coordination
4. Trigger publish check
5. Check engagement monitoring

**Verify:**
- [ ] Content discovered and inventoried
- [ ] Orchestrator updated schedule
- [ ] Content published (simulated)
- [ ] Engagement monitoring captured
- [ ] All shared files have recent timestamps
```

### Test Mode

Implement test mode in your agents to avoid external side effects:

```yaml
# In agent's template.yaml or config
test_mode: true

# Agent behavior in test mode:
# - Log what WOULD happen instead of doing it
# - Use mock data for external APIs
# - Still write to shared folders (for verification)
```

---

## Best Practices

### Design Principles

1. **Start Simple**: Begin with 2-3 agents, add more only when needed
2. **Clear Ownership**: Every file, every piece of state has ONE owner
3. **Loose Coupling**: Agents should work even if others are temporarily down
4. **Fail Gracefully**: Handle missing files, stale data, unreachable agents
5. **Audit Everything**: Log all cross-agent interactions

### Communication Best Practices

1. **Prefer Shared Folders**: More reliable than MCP for state
2. **Use MCP for Urgency**: Only for time-sensitive coordination
3. **Timestamp Everything**: Know when data was written
4. **Version Your Schemas**: Include version in JSON files
5. **Document Contracts**: Explicit schemas for shared files

### Operational Best Practices

1. **Stagger Schedules**: Avoid thundering herd
2. **Health Checks**: Orchestrator monitors all agents
3. **Graceful Degradation**: System works at reduced capacity if one agent fails
4. **Recovery Procedures**: Document how to recover from failures
5. **Test Regularly**: Run integration tests periodically

### Security Best Practices

1. **Minimal Permissions**: Only grant necessary agent-to-agent permissions
2. **Credential Isolation**: Each agent has only its own credentials
3. **Audit Inter-Agent Calls**: Trinity logs all MCP communications
4. **Review Shared Data**: Don't put secrets in shared folders

---

## System Definition Template

Use this template for documenting your multi-agent system:

```markdown
# [System Name] - System Definition

> **Version**: X.Y.Z
> **Platform**: Trinity Deep Agent Orchestration Platform
> **Status**: Development | Testing | Production
> **Last Updated**: YYYY-MM-DD

---

## Executive Summary

[2-3 sentence description of what this system does]

---

## System Architecture

### Agent Constellation

```
[ASCII diagram of agents and their relationships]
```

### Agent Repositories

| Agent | GitHub Repository | Purpose |
|-------|-------------------|---------|
| agent-name | github.com/Org/repo | Description |

---

## Agent Roles & Responsibilities

### [Agent 1 Name]

**Role**: [One sentence]

**Responsibilities**:
- Responsibility 1
- Responsibility 2

**Owns (Writes)**:
- file1.json
- file2.json

**Reads (From Others)**:
- agent-x: file.json
- agent-y: file.json

---

## Communication Architecture

### Shared Folder Structure

[Document folder structure for each agent]

### MCP Communication

[Document when/why MCP is used]

### File Contracts

[Document schema for each shared file]

---

## Scheduling Configuration

| Agent | Schedule Name | Cron | Purpose |
|-------|---------------|------|---------|
| agent | name | cron | purpose |

---

## Deployment Configuration

### Prerequisites
- [ ] Prerequisite 1
- [ ] Prerequisite 2

### Deployment Steps
1. Step 1
2. Step 2

### Permissions Matrix

| Source ↓ / Target → | agent-a | agent-b | agent-c |
|---------------------|---------|---------|---------|
| **agent-a** | — | ✅ | ✅ |

---

## Operations Guide

### Starting the System
[Instructions]

### Monitoring
[What to monitor, how]

### Troubleshooting
[Common issues and solutions]

---

## Version History

| Version | Date | Changes |
|---------|------|---------|
| 1.0.0 | YYYY-MM-DD | Initial release |
```

---

## Example: Content Management System

See the Ruby Content Management System for a complete example:

**System Goal**: Automate content creation, scheduling, and publishing with engagement monitoring.

**Agents**:
- `ruby-orchestrator`: Coordination, scheduling, health monitoring
- `ruby-content`: Content discovery, production, publishing
- `ruby-engagement`: Engagement monitoring, replies, analytics

**Key Patterns Used**:
- Orchestrator-Workers architecture
- Shared folders for state
- Staggered scheduling
- Full mesh permissions
- Test mode for safe development

**Reference**: See `/Users/eugene/Dropbox/trinity/agents/Ruby/ruby-content-system-definition.md` for the complete system definition.

---

## Troubleshooting

### Agent Can't Read Shared Folder

**Symptoms**: `shared-in/{agent}` is empty or doesn't exist

**Causes & Solutions**:
1. **Permissions not granted**: Check `GET /api/agents/{name}/permissions`
2. **Consume not enabled**: Check `GET /api/agents/{name}/folders`
3. **Source agent not exposing**: Check source agent's folders config
4. **Needs restart**: Folder config changes require agent restart

### Agents Not Communicating

**Symptoms**: MCP chat fails or returns permission denied

**Causes & Solutions**:
1. **Permission not granted**: Grant via Permissions tab
2. **Target agent not running**: Start the target agent
3. **Agent name wrong**: Names are case-sensitive, use exact name

### Stale Data in Shared Folders

**Symptoms**: Files have old timestamps, not updating

**Causes & Solutions**:
1. **Source agent not running**: Check agent status
2. **Schedule not enabled**: Check Schedules tab
3. **Schedule failing**: Check execution history for errors
4. **Agent stuck**: Restart the source agent

### Schedule Conflicts

**Symptoms**: Agents interfering with each other, race conditions

**Causes & Solutions**:
1. **Simultaneous schedules**: Stagger cron expressions
2. **No locking**: Implement claim/lock pattern
3. **Reading while writing**: Add `_writing` flag pattern:
   ```json
   {"_writing": true, "data": ...}
   // Writer sets _writing: true, writes data, sets _writing: false
   // Reader skips if _writing: true
   ```

### System Too Slow

**Symptoms**: End-to-end workflows take too long

**Causes & Solutions**:
1. **Schedule intervals too long**: Reduce intervals
2. **Too many agents**: Consolidate if possible
3. **Inefficient coordination**: Use MCP for time-sensitive items
4. **Context exhaustion**: New session or memory folding

---

## Revision History

| Date | Changes |
|------|---------|
| 2025-12-14 | Initial version |

---

*This document provides comprehensive guidance for building multi-agent systems on Trinity. For single-agent templates, see [TRINITY_COMPATIBLE_AGENT_GUIDE.md](TRINITY_COMPATIBLE_AGENT_GUIDE.md).*
