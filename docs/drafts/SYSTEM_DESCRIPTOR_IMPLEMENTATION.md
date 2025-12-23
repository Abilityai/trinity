# System Descriptor Architecture - Implementation Design

> **Status**: Implementation Design v1.0
> **Based On**: SYSTEM_DESCRIPTOR_ARCHITECTURE.md (Design Draft v2.1)
> **Design Decisions**: Unified Model, File-based Jobs, trigger_job MCP tool, Hard Enforcement

---

## Design Decisions Summary

| Question | Decision | Rationale |
|----------|----------|-----------|
| Systems vs Agents | **Unified Model** | Standalone agent = system with one agent. Single codebase, simpler mental model |
| Jobs Storage | **File-based** | Jobs as folders in repo. Git-native, versioned, agents read/write directly |
| Worker Triggering | **New `trigger_job` MCP tool** | Uses `claude -p` (headless mode) with `--append-system-prompt` for job context. Synchronous execution, returns structured result |
| Governance | **Hard Enforcement** | Workers cannot write to `system/policies/`. Read-only bind mounts |

### Key Implementation Details

**Headless Execution**: Uses native Claude Code headless mode:
```bash
claude -p "message" --output-format json --append-system-prompt "job context"
```

**Multi-turn Revision**: Uses `--resume session_id` for follow-up on rejected jobs

**Job Lifecycle**: Platform creates folder → runs `claude -p` → captures result → orchestrator reviews

---

## Data Model

### Core Principle: Unified Model

A **standalone agent** is simply a **system with one agent**. This eliminates the need for parallel concepts.

```
┌─────────────────────────────────────────────────────────────────┐
│                         systems                                  │
│  (Every deployment is a system, even single-agent ones)         │
├─────────────────────────────────────────────────────────────────┤
│  id          │ system name (unique)                             │
│  repo_url    │ GitHub URL or local path                         │
│  repo_type   │ 'github' | 'local'                               │
│  version     │ From system.yaml                                 │
│  description │ From system.yaml                                 │
│  owner_id    │ FK to users                                      │
│  created_at  │ Timestamp                                        │
│  updated_at  │ Timestamp                                        │
└─────────────────────────────────────────────────────────────────┘
         │
         │ 1:N
         ▼
┌─────────────────────────────────────────────────────────────────┐
│                      system_agents                               │
│  (Replaces agent_ownership for system-deployed agents)          │
├─────────────────────────────────────────────────────────────────┤
│  id            │ Primary key                                    │
│  system_id     │ FK to systems                                  │
│  agent_key     │ Key in system.yaml (e.g., 'orchestrator')      │
│  container_name│ Docker container name (system-agent format)    │
│  display_name  │ From system.yaml                               │
│  agent_type    │ 'orchestrator' | 'worker' | 'sub-orchestrator' │
│  execution_mode│ 'interactive' | 'headless'                     │
│  agent_path    │ Path within repo (e.g., 'agents/ruby/')        │
│  status        │ 'running' | 'stopped' | 'error'                │
│  container_id  │ Docker container ID                            │
│  created_at    │ Timestamp                                      │
└─────────────────────────────────────────────────────────────────┘
         │
         │ 1:N (cached index, not source of truth)
         ▼
┌─────────────────────────────────────────────────────────────────┐
│                      system_jobs                                 │
│  (Index of jobs from file system - cache for quick queries)     │
├─────────────────────────────────────────────────────────────────┤
│  id            │ Job ID (from request.json)                     │
│  system_id     │ FK to systems                                  │
│  job_path      │ Relative path in repo (jobs/{id}/)             │
│  process_name  │ From request.json                              │
│  step_name     │ From request.json                              │
│  assigned_to   │ Agent key                                      │
│  triggered_by  │ Agent key or 'human'                           │
│  status        │ Cached from status.json                        │
│  created_at    │ From request.json                              │
│  updated_at    │ Last index refresh                             │
└─────────────────────────────────────────────────────────────────┘
```

### Backward Compatibility: Standalone Agents

Standalone agents (created via `POST /api/agents` with `template:`) become:
- A system with `id = agent_name`
- One system_agent with `agent_key = 'default'`
- No jobs folder (single agent doesn't need approval pipeline)

```sql
-- Standalone agent "ruby-social-media"
INSERT INTO systems (id, repo_url, repo_type, owner_id)
VALUES ('ruby-social-media', 'github:Org/ruby-social-media-agent', 'github', 'user-123');

INSERT INTO system_agents (system_id, agent_key, container_name, agent_type, execution_mode)
VALUES ('ruby-social-media', 'default', 'agent-ruby-social-media', 'worker', 'interactive');
```

---

## Container Architecture

### Mount Strategy

Each agent container mounts the **entire system repo**, with workdir set to agent path:

```
/home/developer/
├── workspace/                          # ENTIRE system repo mounted here
│   ├── system.yaml
│   ├── system/
│   │   ├── policies/                   # Read-only for workers (bind mount)
│   │   └── processes/                  # Read-only for workers (bind mount)
│   ├── agents/
│   │   ├── orchestrator/               # Orchestrator's workdir
│   │   ├── ruby/                       # Ruby's workdir (WORKDIR for this container)
│   │   └── cornelius/
│   └── jobs/                           # Shared, writable by all
│
├── shared-out/                         # This agent's shared folder
├── shared-in/                          # Other agents' shared folders
├── vector-store/                       # Chroma DB
└── .trinity/                           # Platform-injected
```

### Hard Enforcement via Mount Options

```python
# In docker_service.py - create_agent_container()

def get_volume_mounts(system_id: str, agent_key: str, agent_type: str) -> list:
    """Generate volume mounts with governance enforcement."""

    mounts = []
    workspace_path = f"/data/systems/{system_id}/repo"

    # Main workspace - read-write
    mounts.append({
        'source': workspace_path,
        'target': '/home/developer/workspace',
        'type': 'bind',
        'read_only': False
    })

    # Governance enforcement for workers
    if agent_type == 'worker':
        # Override policies and processes as read-only
        mounts.append({
            'source': f"{workspace_path}/system/policies",
            'target': '/home/developer/workspace/system/policies',
            'type': 'bind',
            'read_only': True  # HARD ENFORCEMENT
        })
        mounts.append({
            'source': f"{workspace_path}/system/processes",
            'target': '/home/developer/workspace/system/processes',
            'type': 'bind',
            'read_only': True  # HARD ENFORCEMENT
        })

    return mounts
```

### Container Naming Convention

```
# Standalone agents (backward compatible)
agent-{agent-name}

# System agents
{system-id}-{agent-key}

Examples:
- agent-ruby-social-media          # Standalone
- content-system-orchestrator      # System agent
- content-system-ruby              # System agent
- content-system-cornelius         # System agent
```

---

## API Design

### System Lifecycle Endpoints

```
# System Management
POST   /api/systems                              # Deploy system from repo
GET    /api/systems                              # List all systems
GET    /api/systems/{id}                         # Get system with all agents
DELETE /api/systems/{id}                         # Delete system and all agents
POST   /api/systems/{id}/pull                    # Git pull latest

# System Agent Control
POST   /api/systems/{id}/start                   # Start all agents
POST   /api/systems/{id}/stop                    # Stop all agents
POST   /api/systems/{id}/agents/{key}/start      # Start specific agent
POST   /api/systems/{id}/agents/{key}/stop       # Stop specific agent

# Jobs (reads from file system, caches in DB)
GET    /api/systems/{id}/jobs                    # List jobs (from cache)
GET    /api/systems/{id}/jobs/{job_id}           # Get job details (reads files)
POST   /api/systems/{id}/jobs/{job_id}/approve   # Write approval to status.json
POST   /api/systems/{id}/jobs/{job_id}/reject    # Write rejection + feedback.md
POST   /api/systems/{id}/jobs/refresh            # Re-scan jobs folder, update cache
```

### Backward Compatible Agent Endpoints

Existing endpoints continue to work:

```
# These still work for standalone agents
POST   /api/agents                    # Creates single-agent system
GET    /api/agents                    # Lists all agents (from all systems)
GET    /api/agents/{name}             # Gets agent (resolves system internally)
DELETE /api/agents/{name}             # Deletes agent (and system if single-agent)
```

### System Creation Flow

```python
# POST /api/systems
{
    "repo_url": "github:Org/content-production-system",
    "name": "content-production"  # Optional override
}

# Response
{
    "id": "content-production",
    "agents": [
        {"key": "orchestrator", "container_name": "content-production-orchestrator", "status": "running"},
        {"key": "ruby", "container_name": "content-production-ruby", "status": "stopped"},
        {"key": "cornelius", "container_name": "content-production-cornelius", "status": "stopped"}
    ],
    "jobs_count": 0
}
```

---

## Headless Worker Execution

### Claude Code Headless Mode

Claude Code supports headless execution via `claude -p` (print mode):

```bash
claude -p "message" \
  --output-format json \
  --append-system-prompt "Job context here" \
  --allowedTools "Read,Write,Bash" \
  --permission-mode acceptEdits
```

Key flags:
- `-p` / `--print`: Non-interactive mode
- `--output-format json`: Structured output with `session_id`, `cost_usd`, `result`
- `--append-system-prompt`: Inject job context
- `--resume session_id`: Multi-turn follow-up
- `--allowedTools`: Restrict tool access

### New MCP Tool: trigger_job

```python
@mcp.tool()
async def trigger_job(
    agent_key: str,
    message: str,
    job_id: str = None,          # Auto-generated if not provided
    process_name: str = None,    # Optional process reference
    step_name: str = None,       # Optional step reference
    timeout: int = 600           # Max execution time (seconds)
) -> dict:
    """
    Trigger a worker agent to execute a job in headless mode.

    Creates job folder, runs worker with `claude -p`, returns when complete.

    Returns:
        {
            "job_id": "job-20251216-001",
            "status": "pending_review",  # or "failed"
            "session_id": "abc123",      # For follow-up if needed
            "cost_usd": 0.05,
            "duration_ms": 45000,
            "output_files": ["output/draft.md"]
        }
    """
```

### Execution Flow

```
1. Orchestrator calls: trigger_job("ruby", "Create blog post about X")

2. Platform creates job folder:
   jobs/job-20251216-001/
   ├── request.json    # {id, message, process, step, triggered_by, created_at}
   ├── status.json     # {status: "in_progress"}
   └── output/         # Empty, worker writes here

3. Platform runs in worker container:
   docker exec content-system-ruby claude -p "Create blog post about X" \
     --output-format json \
     --append-system-prompt "$(cat job-context.md)" \
     --allowedTools "Read,Write,Bash,Glob,Grep" \
     --permission-mode acceptEdits

4. Worker executes:
   - Reads job context from system prompt
   - Follows policies in ../../system/policies/
   - Writes deliverables to ../../jobs/job-20251216-001/output/
   - Updates ../../jobs/job-20251216-001/status.json

5. Platform captures result:
   - Parses JSON output (cost, session_id, result)
   - Updates status.json with completion info
   - Returns job summary to orchestrator

6. Orchestrator reviews:
   - Reads output files
   - Approves/rejects by updating status.json
   - Can trigger revision with --resume session_id
```

### Job Context System Prompt

Injected via `--append-system-prompt`:

```markdown
# JOB EXECUTION CONTEXT

You are executing a job as part of a multi-agent system.

## Job Details
- **Job ID**: job-20251216-001
- **Process**: content-production
- **Step**: draft-creation
- **Triggered By**: orchestrator
- **System**: content-production-system

## Instructions
Create blog post about X

## Output Requirements
1. Write all deliverables to: `../../jobs/job-20251216-001/output/`
2. Update status when done: `../../jobs/job-20251216-001/status.json`
   Set `{"status": "pending_review", "updated_at": "ISO timestamp"}`

## Governance (ENFORCED)
- You are a WORKER agent in this system
- `../../system/policies/` is READ-ONLY - you cannot modify policies
- `../../system/processes/` is READ-ONLY - you cannot modify processes
- Follow all policies defined in `../../system/policies/`

## Available Policies
- content-standards.md: Quality and formatting requirements
- approval-workflows.md: How your work will be reviewed
- data-handling.md: Data privacy and security rules
```

### Multi-turn Job Revision

If orchestrator rejects and requests revision:

```python
# Orchestrator can trigger revision using session_id
result = await trigger_job(
    agent_key="ruby",
    message="Revise the draft: make it more concise, fix the intro",
    job_id="job-20251216-001",  # Same job
    resume_session="abc123"     # Continue conversation
)
```

Platform runs:
```bash
docker exec content-system-ruby claude -p "Revise the draft..." \
  --resume abc123 \
  --output-format json
```

### Synchronous vs Asynchronous

**Default: Synchronous** - `trigger_job` waits for completion and returns result.

For long-running jobs, orchestrator can:
1. Set longer timeout
2. Use scheduling system for truly async work
3. Poll job status via `get_job_status(job_id)`

```python
@mcp.tool()
async def get_job_status(job_id: str) -> dict:
    """Get current status of a job by reading its status.json."""
    return {
        "job_id": job_id,
        "status": "pending_review",
        "output_files": ["output/draft.md", "output/images/hero.png"],
        "updated_at": "2025-12-16T10:15:00Z"
    }
```

---

## File-based Jobs Implementation

### Job Folder Structure

```
jobs/
├── job-20251216-001/
│   ├── request.json         # Created by platform when job starts
│   ├── status.json          # Updated by worker, then reviewer
│   ├── output/              # Worker writes deliverables here
│   │   ├── draft.md
│   │   └── images/
│   └── feedback.md          # Written on rejection
│
├── job-20251216-002/
│   └── ...
│
└── .index.json              # Platform-maintained index (optional)
```

### Platform Job Indexing

Platform periodically scans jobs folder and updates cache:

```python
async def index_system_jobs(system_id: str) -> list[dict]:
    """Scan jobs folder and update system_jobs table."""

    jobs_path = f"/data/systems/{system_id}/repo/jobs"
    jobs = []

    for job_dir in os.listdir(jobs_path):
        if job_dir.startswith('.'):
            continue

        request_path = f"{jobs_path}/{job_dir}/request.json"
        status_path = f"{jobs_path}/{job_dir}/status.json"

        if os.path.exists(request_path):
            request = json.load(open(request_path))
            status = json.load(open(status_path)) if os.path.exists(status_path) else {"status": "pending"}

            jobs.append({
                "id": request["id"],
                "job_path": f"jobs/{job_dir}",
                "process_name": request.get("process"),
                "step_name": request.get("step"),
                "assigned_to": request.get("assigned_to"),
                "triggered_by": request.get("triggered_by"),
                "status": status.get("status"),
                "created_at": request.get("created_at")
            })

    # Update cache table
    await update_jobs_cache(system_id, jobs)
    return jobs
```

### Human Review via UI

```
UI Flow:
1. User navigates to Systems → content-production → Jobs
2. UI calls GET /api/systems/content-production/jobs
3. Shows list with status badges
4. User clicks job → sees request, output files
5. User clicks Approve → POST /api/systems/content-production/jobs/job-123/approve
6. Backend writes to status.json: {"status": "approved", "reviewed_by": "user@email", "reviewed_at": "..."}
```

---

## UI Design

### Systems Page (New)

```
/systems

┌─────────────────────────────────────────────────────────────────┐
│  Systems                                            [+ Deploy]  │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │ content-production-system                    3 agents   │   │
│  │ Autonomous content creation pipeline                    │   │
│  │ ● orchestrator (running)  ○ ruby  ○ cornelius          │   │
│  │ 2 jobs pending review                                   │   │
│  │                                    [Start All] [Stop]   │   │
│  └─────────────────────────────────────────────────────────┘   │
│                                                                 │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │ ruby-social-media                           1 agent     │   │
│  │ Social media content creator                            │   │
│  │ ● default (running)                                     │   │
│  │                                    [Start] [Stop]       │   │
│  └─────────────────────────────────────────────────────────┘   │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### System Detail Page

```
/systems/content-production

Tabs: [Overview] [Agents] [Jobs] [Policies] [Git]

┌─ Overview ──────────────────────────────────────────────────────┐
│  content-production-system v1.0                                 │
│  Autonomous content creation pipeline                           │
│                                                                 │
│  Repository: github:Org/content-production-system               │
│  Last Pull: 2025-12-16 10:00:00                                │
│                                                                 │
│  Agents: 3 (1 running, 2 stopped)                              │
│  Jobs: 5 total (2 pending review, 3 completed)                 │
└─────────────────────────────────────────────────────────────────┘

┌─ Agents ────────────────────────────────────────────────────────┐
│  orchestrator    Orchestrator    ● Running    [Stop] [Chat]    │
│  ruby            Worker          ○ Stopped    [Start]          │
│  cornelius       Worker          ○ Stopped    [Start]          │
└─────────────────────────────────────────────────────────────────┘

┌─ Jobs (Approval Inbox) ─────────────────────────────────────────┐
│  job-20251216-002  │ content-production │ ruby │ pending_review │
│  job-20251216-001  │ content-production │ ruby │ approved       │
└─────────────────────────────────────────────────────────────────┘
```

### Existing Agents Page

The existing `/agents` page shows all agents flattened:

```
/agents

┌─────────────────────────────────────────────────────────────────┐
│  All Agents                                                     │
├─────────────────────────────────────────────────────────────────┤
│  content-production-orchestrator  │ orchestrator │ ● Running   │
│  content-production-ruby          │ worker       │ ○ Stopped   │
│  content-production-cornelius     │ worker       │ ○ Stopped   │
│  ruby-social-media                │ standalone   │ ● Running   │
└─────────────────────────────────────────────────────────────────┘
```

---

## Implementation Phases

### Phase 1: Foundation (Week 1)

**Database & Models**
- [ ] Create `systems` table
- [ ] Create `system_agents` table
- [ ] Create `system_jobs` table (cache)
- [ ] Migration script for existing agents → single-agent systems

**System Deployment**
- [ ] `POST /api/systems` - Clone repo, parse system.yaml, create agents
- [ ] `GET /api/systems` - List systems
- [ ] `GET /api/systems/{id}` - System detail with agents
- [ ] `DELETE /api/systems/{id}` - Cleanup

**Container Changes**
- [ ] Mount entire repo with workdir per agent
- [ ] Implement read-only mounts for workers (governance enforcement)
- [ ] Container naming: `{system-id}-{agent-key}`

### Phase 2: Jobs Pipeline (Week 2)

**File-based Jobs**
- [ ] Job folder creation helper (request.json, status.json, output/)
- [ ] Job indexing service (scan jobs/, update cache)
- [ ] `GET /api/systems/{id}/jobs` - List from cache
- [ ] `GET /api/systems/{id}/jobs/{job_id}` - Read files
- [ ] `POST /api/systems/{id}/jobs/{job_id}/approve` - Write status.json
- [ ] `POST /api/systems/{id}/jobs/{job_id}/reject` - Write status.json + feedback.md

**New MCP Tool: trigger_job**
- [ ] `trigger_job(agent_key, message, job_id, process_name, step_name, timeout)` tool
- [ ] Job context generation (system prompt with job details, policies)
- [ ] Execute `docker exec ... claude -p` with `--output-format json`
- [ ] Parse result (session_id, cost, output)
- [ ] Update status.json with completion info
- [ ] `get_job_status(job_id)` helper tool
- [ ] Support `--resume session_id` for revisions

### Phase 3: UI (Week 3)

**Systems Page**
- [ ] Systems list view
- [ ] System detail view with tabs
- [ ] Deploy system modal (repo URL input)

**Jobs UI**
- [ ] Jobs tab in system detail
- [ ] Job detail modal (request, output, status)
- [ ] Approve/Reject buttons
- [ ] Feedback input for rejections

**Integration**
- [ ] Update Agents page to show system membership
- [ ] Update Agent detail to link to parent system
- [ ] WebSocket broadcasts for system events

### Phase 4: Polish (Week 4)

**Observability**
- [ ] System-level activity stream
- [ ] Job execution timeline
- [ ] Cross-agent collaboration visualization

**Git Integration**
- [ ] System-level git sync (all agents)
- [ ] Pull updates with agent restart

**Documentation**
- [ ] Update TRINITY_COMPATIBLE_AGENT_GUIDE.md
- [ ] Create SYSTEM_GUIDE.md
- [ ] API documentation

---

## Migration Strategy

### Existing Agents → Single-Agent Systems

```sql
-- Migration script
INSERT INTO systems (id, repo_url, repo_type, owner_id, created_at)
SELECT
    agent_name as id,
    -- Reconstruct repo_url from container labels
    COALESCE(template_url, 'local:default') as repo_url,
    CASE WHEN template_url LIKE 'github:%' THEN 'github' ELSE 'local' END as repo_type,
    owner_id,
    created_at
FROM agent_ownership;

INSERT INTO system_agents (system_id, agent_key, container_name, agent_type, execution_mode)
SELECT
    agent_name as system_id,
    'default' as agent_key,
    'agent-' || agent_name as container_name,
    'worker' as agent_type,  -- Standalone agents are workers by default
    'interactive' as execution_mode
FROM agent_ownership;
```

### Backward Compatibility

- `POST /api/agents` with `template:` creates single-agent system
- `GET /api/agents` returns flattened view of all system_agents
- `GET /api/agents/{name}` resolves to system_agent
- Existing integrations (MCP tools, chat, etc.) continue working

---

## Open Questions (Resolved)

| Question | Resolution |
|----------|------------|
| How do Systems relate to standalone Agents? | **Unified Model** - Standalone = single-agent system |
| Where are jobs stored? | **File-based** - jobs/ folder in repo, cached in DB |
| How do orchestrators trigger workers? | **Extended chat_with_agent** - Add headless param |
| How is governance enforced? | **Hard Enforcement** - Read-only mounts for workers |

---

## References

- Design Document: `docs/drafts/SYSTEM_DESCRIPTOR_ARCHITECTURE.md`
- Agent Guide: `docs/TRINITY_COMPATIBLE_AGENT_GUIDE.md`
- Multi-Agent Guide: `docs/MULTI_AGENT_SYSTEM_GUIDE.md`
- Shared Folders: `docs/memory/feature-flows/agent-shared-folders.md`
