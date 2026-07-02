# Feature: Playbooks Tab (PLAYBOOK-001)

## Overview

The Playbooks tab displays an agent's local skills from `.claude/skills/` and allows one-click or instructed invocation.

## User Story

As a **Trinity user**, I want to **browse and invoke my agent's skills directly from the UI** so that **I can run automated tasks without typing slash commands**.

---

## Entry Points

| Type | Location | Description |
|------|----------|-------------|
| UI | `src/frontend/src/views/AgentDetail.vue:527` | "Playbooks" tab in tab navigation |
| API | `GET /api/agents/{name}/playbooks` | Fetch skills list from running agent |
| API | `POST /api/agents/{name}/task` | Execute skill (existing task endpoint) |

---

## Frontend Layer

### Component: PlaybooksPanel.vue

**File**: `src/frontend/src/components/PlaybooksPanel.vue`

**Props** (lines 173-182):
```javascript
const props = defineProps({
  agentName: {
    type: String,
    required: true
  },
  agentStatus: {
    type: String,
    default: 'stopped'
  }
})
```

**Emits** (line 184):
```javascript
const emit = defineEmits(['run-with-instructions'])
```

**State** (lines 188-198):
```javascript
const loading = ref(false)
const error = ref(null)
const searchQuery = ref('')
const running = ref(null)  // Currently running skill name
const successMessage = ref('')
const errorMessage = ref('')
const skills = ref([])
const skillPaths = ref([])
```

**Key Methods**:

1. **loadPlaybooks()** (lines 217-239) - Fetches skills from backend proxy
```javascript
async function loadPlaybooks() {
  if (props.agentStatus !== 'running') return
  loading.value = true
  try {
    const response = await axios.get(
      `/api/agents/${props.agentName}/playbooks`,
      { headers: authStore.authHeader }
    )
    skills.value = response.data.skills || []
    skillPaths.value = response.data.skill_paths || []
  } catch (e) {
    error.value = e.response?.data?.detail || 'Failed to load playbooks'
  } finally {
    loading.value = false
  }
}
```

2. **runSkill(skill)** (lines 241-273) - One-click execution
```javascript
async function runSkill(skill) {
  if (!skill.user_invocable || running.value) return
  running.value = skill.name
  try {
    const response = await axios.post(
      `/api/agents/${props.agentName}/task`,
      { message: `/${skill.name}`, parallel: false },
      { headers: authStore.authHeader }
    )
    const executionId = response.data.execution_id
    successMessage.value = `Playbook /${skill.name} started`
    // Navigate to Tasks tab with highlighted execution
    emit('run-with-instructions', `__NAVIGATE_TASKS__:${executionId}`)
  } catch (e) {
    errorMessage.value = e.response?.data?.detail || `Failed to run /${skill.name}`
  } finally {
    running.value = null
  }
}
```

3. **runWithInstructions(skill)** (lines 275-279) - Prefill and switch to Tasks
```javascript
function runWithInstructions(skill) {
  emit('run-with-instructions', `/${skill.name} `)
}
```

**Watchers** (lines 289-305):
- Watch `agentStatus`: Load playbooks when agent starts, clear when stops
- Watch `agentName`: Reload playbooks when switching agents

### Integration: AgentDetail.vue

**File**: `src/frontend/src/views/AgentDetail.vue`

**Tab Configuration** (line 527):
```javascript
tabs.push(
  { id: 'schedules', label: 'Schedules' },
  { id: 'playbooks', label: 'Playbooks' },  // <-- Here
  { id: 'credentials', label: 'Credentials' }
)
```

**Tab Content** (lines 168-175):
```vue
<div v-if="activeTab === 'playbooks'" class="p-6">
  <PlaybooksPanel
    :agent-name="agent.name"
    :agent-status="agent.status"
    @run-with-instructions="handlePlaybookRunWithInstructions"
  />
</div>
```

**Handler** (lines 829-849):
```javascript
const handlePlaybookRunWithInstructions = (prefillText) => {
  // Check if this is a navigation request (one-click run completed)
  if (prefillText.startsWith('__NAVIGATE_TASKS__:')) {
    const executionId = prefillText.replace('__NAVIGATE_TASKS__:', '')
    activeTab.value = 'tasks'
    router.replace({ query: { ...route.query, execution: executionId } })
    return
  }
  // Normal case: prefill the task input and switch to Tasks tab
  taskPrefillMessage.value = prefillText
  activeTab.value = 'tasks'
  nextTick(() => {
    setTimeout(() => { taskPrefillMessage.value = '' }, 100)
  })
}
```

---

## Backend Layer

### Endpoint: GET /api/agents/{name}/playbooks

**File**: `src/backend/routers/agents.py`
**Lines**: 510-552

```python
@router.get("/{agent_name}/playbooks")
async def get_agent_playbooks_endpoint(
    agent_name: AuthorizedAgentByName,
    request: Request
):
    """Get available skills (playbooks) from agent's .claude/skills/ directory."""
    import httpx

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    await container_reload(container)

    if container.status != "running":
        raise HTTPException(
            status_code=503,
            detail="Agent is not running. Start the agent to view playbooks."
        )

    try:
        agent_url = f"http://agent-{agent_name}:8000/api/skills"
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(agent_url)
            if response.status_code == 200:
                return response.json()
            else:
                raise HTTPException(
                    status_code=response.status_code,
                    detail=f"Agent returned error: {response.text}"
                )
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Agent is starting up, please try again")
    except httpx.ConnectError:
        raise HTTPException(status_code=503, detail="Could not connect to agent")
```

**Authorization**: `AuthorizedAgentByName` dependency (owner, shared user, or admin)

---

## Agent Layer

### Router Registration

**File**: `docker/base-image/agent_server/routers/__init__.py`
**Line 12**:
```python
from .skills import router as skills_router
```

**File**: `docker/base-image/agent_server/main.py`
**Line 59**:
```python
app.include_router(skills_router)  # Skills/playbooks listing endpoint
```

### Endpoint: GET /api/skills

**File**: `docker/base-image/agent_server/routers/skills.py`

**Response Model** (lines 20-36):
```python
class SkillInfo(BaseModel):
    name: str
    description: Optional[str] = None
    path: str
    user_invocable: bool = True
    automation: Optional[str] = None  # autonomous, gated, manual, null
    allowed_tools: Optional[List[str]] = None
    argument_hint: Optional[str] = None
    has_schedule: bool = False

class SkillsResponse(BaseModel):
    skills: List[SkillInfo]
    count: int
    skill_paths: List[str]
```

**Endpoint Handler** (lines 160-209):
```python
@router.get("/api/skills", response_model=SkillsResponse)
async def list_skills():
    """List all available skills from agent's skills directories."""
    home_dir = Path('/home/developer')

    skill_paths = [
        home_dir / '.claude' / 'skills',
        Path.home() / '.claude' / 'skills'  # Personal skills
    ]

    all_skills: List[SkillInfo] = []
    scanned_paths: List[str] = []

    for skills_dir in skill_paths:
        # ... path handling ...
        skills = scan_skills_directory(skills_dir)
        all_skills.extend(skills)

    # Remove duplicates (by name), keeping project skills over personal
    # Sort by name
    unique_skills.sort(key=lambda s: s.name.lower())

    return SkillsResponse(
        skills=unique_skills,
        count=len(unique_skills),
        skill_paths=scanned_paths
    )
```

**YAML Parsing** (lines 39-62):
```python
def parse_yaml_frontmatter(content: str) -> Dict[str, Any]:
    """Parse YAML frontmatter from SKILL.md file."""
    pattern = r'^---\s*\n(.*?)\n---'
    match = re.match(pattern, content, re.DOTALL)
    if not match:
        return {}
    try:
        import yaml
        frontmatter = yaml.safe_load(match.group(1))
        return frontmatter if isinstance(frontmatter, dict) else {}
    except Exception as e:
        logger.warning(f"Failed to parse YAML frontmatter: {e}")
        return {}
```

**Directory Scanning** (lines 97-157):
```python
def scan_skills_directory(skills_dir: Path) -> List[SkillInfo]:
    """Scan skills directory for subdirectories containing SKILL.md files."""
    skills = []
    if not skills_dir.exists():
        return skills

    for entry in skills_dir.iterdir():
        if not entry.is_dir():
            continue
        skill_md = entry / "SKILL.md"
        if not skill_md.exists():
            continue

        content = skill_md.read_text(encoding='utf-8')
        frontmatter = parse_yaml_frontmatter(content)

        name = frontmatter.get('name', entry.name)
        description = frontmatter.get('description') or extract_description_from_body(content)
        # ... build SkillInfo ...
        skills.append(skill)

    return skills
```

---

## Side Effects

| Type | Event | Location |
|------|-------|----------|
| WebSocket | None | Playbooks are read-only listing |
| Audit Log | None | Skill invocation logged via `/task` endpoint |
| Database | None | Skills are filesystem-based |

**Skill Execution Side Effects** (via POST /api/agents/{name}/task):
- Creates execution record in SQLite
- Broadcasts `task_created` WebSocket event
- Claude Code runs inside agent container
- Execution status updates streamed via SSE

---

## Error Handling

| Error Case | HTTP Status | Message | Location |
|------------|-------------|---------|----------|
| Agent not found | 404 | "Agent not found" | `agents.py:524` |
| Agent not running | 503 | "Agent is not running. Start the agent to view playbooks." | `agents.py:529` |
| Agent starting up | 504 | "Agent is starting up, please try again" | `agents.py:546` |
| Connection failed | 503 | "Could not connect to agent" | `agents.py:548` |
| Agent error | varies | "Agent returned error: {text}" | `agents.py:542` |
| Skill not invocable | N/A | Button disabled in UI | `PlaybooksPanel.vue:101` |

---

## Security Considerations

1. **Access Control**: `AuthorizedAgentByName` ensures only owner, shared users, or admins can access
2. **No Code Injection**: Skills must exist in `.claude/skills/` - cannot execute arbitrary commands
3. **Container Isolation**: Skills run inside Docker container with standard isolation
4. **Proxy Pattern**: Backend proxies to agent's internal endpoint (agent-server not exposed externally)

---

## Data Flow Diagram

```
┌────────────────────────────────────────────────────────────────────────────┐
│                          Playbooks Tab Data Flow                            │
├────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  User clicks "Playbooks" tab                                               │
│         │                                                                   │
│         ▼                                                                   │
│  ┌─────────────────────┐                                                   │
│  │  PlaybooksPanel.vue │                                                   │
│  │  (loadPlaybooks)    │                                                   │
│  └──────────┬──────────┘                                                   │
│             │                                                               │
│             │ GET /api/agents/{name}/playbooks                             │
│             ▼                                                               │
│  ┌─────────────────────┐                                                   │
│  │  agents.py:510      │  Backend checks:                                  │
│  │  (proxy endpoint)   │  - Agent exists (Docker container)               │
│  └──────────┬──────────┘  - Agent running (container.status)              │
│             │              - User authorized (AuthorizedAgentByName)       │
│             │                                                               │
│             │ GET http://agent-{name}:8000/api/skills                      │
│             ▼                                                               │
│  ┌─────────────────────┐                                                   │
│  │  skills.py:160      │  Agent server:                                    │
│  │  (list_skills)      │  - Scans .claude/skills/                         │
│  └──────────┬──────────┘  - Parses SKILL.md YAML frontmatter              │
│             │              - Deduplicates, sorts by name                   │
│             │                                                               │
│             │ SkillsResponse { skills, count, skill_paths }               │
│             ▼                                                               │
│  ┌─────────────────────┐                                                   │
│  │  PlaybooksPanel.vue │  Display:                                         │
│  │  (render skills)    │  - Grid of skill cards                           │
│  └──────────┬──────────┘  - Name, description, automation badge           │
│             │              - Run / Edit & Run buttons                      │
│             │                                                               │
│  ═══════════╪═══════════════════════════════════════════════════════════  │
│  One-Click  │  Run with Instructions                                       │
│  ═══════════╪═══════════════════════════════════════════════════════════  │
│             │                                                               │
│  Click "Run" button                    Click "Edit & Run" button           │
│         │                                      │                           │
│         ▼                                      ▼                           │
│  POST /api/agents/{name}/task          emit('run-with-instructions')      │
│  { message: "/{skill}" }                     │                             │
│         │                                      │                           │
│         ▼                                      ▼                           │
│  { execution_id }                      AgentDetail handler                 │
│         │                              taskPrefillMessage = "/{skill} "   │
│         ▼                              activeTab = 'tasks'                 │
│  emit('__NAVIGATE_TASKS__:{id}')              │                           │
│         │                                      ▼                           │
│         ▼                              TasksPanel shows prefilled input   │
│  Switch to Tasks tab                   User adds instructions             │
│  Highlight execution                   Presses Enter to run               │
│                                                                             │
└────────────────────────────────────────────────────────────────────────────┘
```

---

## Files Summary

### Created

| File | Lines | Description |
|------|-------|-------------|
| `docker/base-image/agent_server/routers/skills.py` | 210 | Agent-side skills listing endpoint with YAML parsing |
| `src/frontend/src/components/PlaybooksPanel.vue` | 316 | Playbooks tab Vue component with grid display |
| `docs/memory/feature-flows/playbooks-tab.md` | - | This documentation |

### Modified

| File | Lines Changed | Description |
|------|---------------|-------------|
| `docker/base-image/agent_server/routers/__init__.py` | 12, 23 | Export skills_router |
| `docker/base-image/agent_server/main.py` | 26, 59 | Import and mount skills_router |
| `src/backend/routers/agents.py` | 510-552 | Add `/api/agents/{name}/playbooks` proxy |
| `src/frontend/src/views/AgentDetail.vue` | 168-175, 269, 527, 829-849 | Add Playbooks tab and handler |

---

## Testing

**Test File**: `tests/test_playbooks.py`

| Test Class | Tests | Description |
|------------|-------|-------------|
| `TestPlaybooksAuthentication` | 1 | Verifies 401 without auth (smoke) |
| `TestPlaybooksErrorHandling` | 1 | Verifies 404 for nonexistent agent (smoke) |
| `TestPlaybooksAgentNotRunning` | 1 | Verifies 503 when agent stopped |
| `TestPlaybooksRetrieval` | 4 | SkillsResponse structure validation |
| `TestPlaybooksSkillStructure` | 3 | SkillInfo field types and sorting |
| `TestPlaybooksAccessControl` | 1 | Authorization check (smoke) |
| `TestPlaybooksTimeoutHandling` | 1 | Graceful 504 handling |
| `TestPlaybooksEmptyState` | 1 | Empty skills list handling |
| `TestPlaybooksConcurrentAccess` | 1 | Multiple request consistency |

**Run Tests**:
```bash
# Smoke tests only (fast, no agent required)
cd tests && ./run-smoke.sh test_playbooks.py

# All tests (requires running agent with rebuilt base image)
cd tests && source .venv/bin/activate && pytest test_playbooks.py -v
```

**Note**: Agent-requiring tests will skip gracefully if the agent base image hasn't been rebuilt with `skills.py` router.

---

## Related Flows

| Flow | Relationship |
|------|--------------|
| [tasks-tab.md](tasks-tab.md) | Skill execution via `/task` endpoint |
| [parallel-headless-execution.md](parallel-headless-execution.md) | Task execution mechanics |
| [scheduling.md](scheduling.md) | Future: Schedule skills |
| [agent-lifecycle.md](agent-lifecycle.md) | Agent must be running to list skills |

---

## Revision History

| Date | Changes |
|------|---------|
| 2026-02-27 | Added Testing section with test_playbooks.py coverage |
| 2026-02-27 | Initial implementation (PLAYBOOK-001) - Full vertical slice documentation |
