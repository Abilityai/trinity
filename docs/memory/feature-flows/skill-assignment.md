# Feature: Skill Assignment to Agents

> **Created**: 2026-01-25 - Complete vertical slice from SkillsPanel.vue through routers/skills.py to db/skills.py

## Revision History

| Date | Changes |
|------|---------|
| 2026-08-12 | **ent#384** — the Library page gained a fleet-wide **read** of who holds each skill (`GET /api/skills/assignments`, access-scoped + human-only; see [library-page.md](library-page.md)). Assignment itself is unchanged and still lives here: this per-agent tab and the REST/MCP surfaces remain the only WRITERS (ent#182 — one skill model). The Library's assign/unassign controls were split to ent#386, so if you are adding them, reuse `POST`/`DELETE /api/agents/{name}/skills/{skill}` rather than adding a skill-keyed writer — a second write path is a second place for the owner gate to drift. Note the two surfaces have DIFFERENT scopes: the ent#384 read is owned ∪ shared, while these writes are owner-or-admin, so a shared agent can appear as a holder that the same caller may not modify. |
| 2026-01-25 | Initial documentation of skill assignment feature flow |

## Overview

Skill assignment allows agent owners to select methodology guides (skills) from the platform library and assign them to their agents. Assigned skills are stored in the database and can be injected into running agent containers.

**Requirement**: Skills Management System
**Implemented**: 2026-01-25
**Last Updated**: 2026-01-25

## User Story

As an agent owner, I want to assign platform skills to my agents so that they follow standardized methodologies like TDD, systematic debugging, and code review best practices.

## Entry Points

### User Configuration (UI)
- **Component**: `src/frontend/src/components/SkillsPanel.vue:27-36` - "Save" button for bulk assignment
- **Tab Access**: Agent Detail page -> Skills tab (shown for agent owners)

### Backend API
- `GET /api/agents/{name}/skills` - List assigned skills
- `PUT /api/agents/{name}/skills` - Bulk update assignments (replace all)
- `POST /api/agents/{name}/skills/{skill}` - Add single skill
- `DELETE /api/agents/{name}/skills/{skill}` - Remove single skill
- `POST /api/agents/{name}/skills/inject` - Push assigned skills to running agent

---

## Frontend Layer

### Components

#### SkillsPanel.vue - Skill Assignment Interface
**File**: `src/frontend/src/components/SkillsPanel.vue` (325 lines)

> **ent#263**: the panel's skill-package fact chips (automation, invocability, file count/size) now render via the shared contract seam `components/skills/{SkillContractChips.vue, contract.js}` — one rendering consumed by both this per-agent tab and the Library page's fleet browse ([library-page.md](library-page.md)); the panel's former local `SkillMeta`/`formatBytes`/`deps` helpers were extracted there.

The SkillsPanel provides a checkbox-based interface for selecting and assigning skills:

| Section | Lines | Description |
|---------|-------|-------------|
| Header with buttons | 6-38 | "Inject to Agent" (running only) and "Save" buttons |
| Loading state | 41-43 | Spinner while fetching data |
| Library not configured | 46-58 | Warning when skills library URL not set |
| Skills grid | 61-105 | Searchable checkbox list of available skills |
| Library status footer | 123-137 | Shows library URL, commit SHA, last sync time |
| Toast messages | 141-148 | Success/error notifications |

#### Key UI Elements

```vue
<!-- SkillsPanel.vue:27-36 - Save button -->
<button
  @click="saveAssignments"
  :disabled="saving || !hasChanges"
  class="inline-flex items-center px-3 py-1.5 border border-transparent text-sm font-medium rounded-md text-white bg-green-600 hover:bg-green-700 disabled:opacity-50 disabled:cursor-not-allowed"
>
  {{ saving ? 'Saving...' : 'Save' }}
</button>
```

```vue
<!-- SkillsPanel.vue:76-104 - Skill checkbox cards -->
<div
  v-for="skill in filteredSkills"
  :key="skill.name"
  class="relative flex items-start p-3 border rounded-lg cursor-pointer transition-colors"
  :class="[
    selectedSkills.has(skill.name)
      ? 'border-indigo-500 bg-indigo-50 dark:bg-indigo-900/30'
      : 'border-gray-200 dark:border-gray-700 hover:border-gray-300'
  ]"
  @click="toggleSkill(skill.name)"
>
  <input type="checkbox" :checked="selectedSkills.has(skill.name)" />
  <div class="ml-3 flex-1">
    <label class="font-medium text-sm text-gray-900 dark:text-white">{{ skill.name }}</label>
    <p v-if="skill.description" class="text-xs text-gray-500">{{ skill.description }}</p>
  </div>
</div>
```

### State Management

**File**: `src/frontend/src/components/SkillsPanel.vue:152-314`

The component manages its own state using Vue 3 Composition API:

```javascript
// SkillsPanel.vue:171-190 - State variables
const loading = ref(true)
const saving = ref(false)
const injecting = ref(false)
const searchQuery = ref('')
const successMessage = ref('')
const errorMessage = ref('')

// Data
const availableSkills = ref([])      // All skills in library
const assignedSkills = ref([])        // Skills assigned to this agent
const selectedSkills = ref(new Set()) // Current UI selection
const originalSelection = ref(new Set()) // For dirty checking
const libraryStatus = ref({...})
```

#### hasChanges Computed Property

```javascript
// SkillsPanel.vue:202-208 - Dirty state detection
const hasChanges = computed(() => {
  if (selectedSkills.value.size !== originalSelection.value.size) return true
  for (const skill of selectedSkills.value) {
    if (!originalSelection.value.has(skill)) return true
  }
  return false
})
```

### API Calls

#### loadData() - Initial Load
**File**: `src/frontend/src/components/SkillsPanel.vue:211-235`

```javascript
async function loadData() {
  loading.value = true
  try {
    // Load library status, available skills, and assigned skills in parallel
    const [statusRes, skillsRes, assignedRes] = await Promise.all([
      axios.get('/api/skills/library/status', { headers: authStore.authHeader }),
      axios.get('/api/skills/library', { headers: authStore.authHeader }).catch(() => ({ data: [] })),
      axios.get(`/api/agents/${props.agentName}/skills`, { headers: authStore.authHeader })
    ])

    libraryStatus.value = statusRes.data
    availableSkills.value = skillsRes.data
    assignedSkills.value = assignedRes.data

    // Initialize selection from assigned skills
    selectedSkills.value = new Set(assignedSkills.value.map(s => s.skill_name))
    originalSelection.value = new Set(selectedSkills.value)
  } catch (e) {
    errorMessage.value = 'Failed to load skills data'
  } finally {
    loading.value = false
  }
}
```

#### saveAssignments() - Bulk Save
**File**: `src/frontend/src/components/SkillsPanel.vue:247-266`

```javascript
async function saveAssignments() {
  saving.value = true
  try {
    await axios.put(
      `/api/agents/${props.agentName}/skills`,
      { skills: Array.from(selectedSkills.value) },
      { headers: authStore.authHeader }
    )

    originalSelection.value = new Set(selectedSkills.value)
    successMessage.value = 'Skills saved successfully'
    setTimeout(() => { successMessage.value = '' }, 3000)
  } catch (e) {
    errorMessage.value = e.response?.data?.detail || 'Failed to save skills'
    setTimeout(() => { errorMessage.value = '' }, 3000)
  } finally {
    saving.value = false
  }
}
```

#### injectSkills() - Push to Running Agent
**File**: `src/frontend/src/components/SkillsPanel.vue:268-291`

```javascript
async function injectSkills() {
  injecting.value = true
  try {
    const response = await axios.post(
      `/api/agents/${props.agentName}/skills/inject`,
      {},
      { headers: authStore.authHeader }
    )

    const result = response.data
    if (result.success) {
      successMessage.value = `Injected ${result.skills_injected} skills`
    } else {
      successMessage.value = `Injected ${result.skills_injected} skills, ${result.skills_failed} failed`
    }
  } catch (e) {
    errorMessage.value = e.response?.data?.detail || 'Failed to inject skills'
  } finally {
    injecting.value = false
  }
}
```

---

## Backend Layer

### Router: Skills Assignment Endpoints
**File**: `src/backend/routers/skills.py` (200 lines)

The skills router handles both library management and agent skill assignments:

| Endpoint | Lines | Method | Description |
|----------|-------|--------|-------------|
| `/api/agents/{name}/skills` | 93-104 | GET | List assigned skills |
| `/api/agents/{name}/skills` | 107-129 | PUT | Bulk update assignments |
| `/api/agents/{name}/skills/inject` | 134-150 | POST | Inject to running agent |
| `/api/agents/{name}/skills/{skill}` | 153-182 | POST | Assign single skill |
| `/api/agents/{name}/skills/{skill}` | 185-199 | DELETE | Unassign single skill |

### Endpoint: GET /api/agents/{name}/skills
**File**: `src/backend/routers/skills.py:93-104`

```python
@router.get("/agents/{agent_name}/skills", response_model=List[AgentSkill])
async def get_agent_skills(
    agent_name: str,
    current_user: User = Depends(get_current_user)
):
    """
    Get skills assigned to an agent.

    Returns list of AgentSkill objects with assignment metadata.
    """
    # TODO: Add access control for shared agents
    return db.get_agent_skills(agent_name)
```

**Response Model**: `List[AgentSkill]`

```python
# db_models.py:418-425
class AgentSkill(BaseModel):
    id: int
    agent_name: str
    skill_name: str
    assigned_by: str  # Username of who assigned
    assigned_at: datetime
```

### Endpoint: PUT /api/agents/{name}/skills
**File**: `src/backend/routers/skills.py:107-129`

```python
@router.put("/agents/{agent_name}/skills")
async def update_agent_skills(
    agent_name: str,
    update: AgentSkillsUpdate,
    current_user: User = Depends(get_current_user)
):
    """
    Bulk update skills assigned to an agent.

    Replaces all existing skill assignments with the provided list.
    """
    # TODO: Add access control (owner only)
    count = db.set_agent_skills(
        agent_name=agent_name,
        skill_names=update.skills,
        assigned_by=current_user.username
    )
    return {
        "success": True,
        "agent_name": agent_name,
        "skills_assigned": count,
        "skills": update.skills
    }
```

**Request Model**: `AgentSkillsUpdate`

```python
# db_models.py:435-437
class AgentSkillsUpdate(BaseModel):
    skills: List[str]  # List of skill names to assign
```

### Endpoint: POST /api/agents/{name}/skills/{skill}
**File**: `src/backend/routers/skills.py:153-182`

```python
@router.post("/agents/{agent_name}/skills/{skill_name}")
async def assign_skill(
    agent_name: str,
    skill_name: str,
    current_user: User = Depends(get_current_user)
):
    """
    Assign a single skill to an agent.
    """
    # Verify skill exists in library
    skill = skill_service.get_skill(skill_name)
    if not skill:
        raise HTTPException(
            status_code=404,
            detail=f"Skill '{skill_name}' not found in library"
        )

    result = db.assign_skill(agent_name, skill_name, current_user.username)
    if result is None:
        return {
            "success": True,
            "message": "Skill already assigned",
            "skill_name": skill_name
        }

    return {
        "success": True,
        "message": "Skill assigned",
        "skill": result
    }
```

### Endpoint: DELETE /api/agents/{name}/skills/{skill}
**File**: `src/backend/routers/skills.py:185-199`

```python
@router.delete("/agents/{agent_name}/skills/{skill_name}")
async def unassign_skill(
    agent_name: str,
    skill_name: str,
    current_user: User = Depends(get_current_user)
):
    """
    Remove a skill assignment from an agent.
    """
    removed = db.unassign_skill(agent_name, skill_name)
    return {
        "success": True,
        "removed": removed,
        "skill_name": skill_name
    }
```

### Endpoint: POST /api/agents/{name}/skills/inject
**File**: `src/backend/routers/skills.py:134-150`

```python
@router.post("/agents/{agent_name}/skills/inject")
async def inject_skills(
    agent_name: str,
    current_user: User = Depends(get_current_user)
):
    """
    Inject assigned skills into a running agent.

    Copies all assigned skills to the agent's .claude/skills/ directory.
    Agent must be running.
    """
    result = await skill_service.inject_skills(agent_name)
    if not result.get("success") and result.get("skills_failed", 0) > 0:
        # Partial success or full failure
        return result

    return result
```

---

## Data Layer

### Database Table: agent_skills
**File**: `src/backend/database.py:658-666`

```sql
CREATE TABLE IF NOT EXISTS agent_skills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name TEXT NOT NULL,
    skill_name TEXT NOT NULL,
    assigned_by TEXT NOT NULL,
    assigned_at TEXT NOT NULL,
    UNIQUE(agent_name, skill_name)
)
```

**Indexes** (`src/backend/database.py:720-721`):
```sql
CREATE INDEX IF NOT EXISTS idx_agent_skills_agent ON agent_skills(agent_name)
CREATE INDEX IF NOT EXISTS idx_agent_skills_skill ON agent_skills(skill_name)
```

### Database Operations
**File**: `src/backend/db/skills.py` (232 lines)

#### SkillsOperations Class

| Method | Lines | Description |
|--------|-------|-------------|
| `get_agent_skills(agent_name)` | 35-53 | Returns List[AgentSkill] for an agent |
| `get_agent_skill_names(agent_name)` | 55-73 | Returns List[str] of skill names only |
| `assign_skill(agent_name, skill_name, assigned_by)` | 75-112 | Add single skill (idempotent) |
| `unassign_skill(agent_name, skill_name)` | 114-132 | Remove single skill |
| `set_agent_skills(agent_name, skill_names, assigned_by)` | 134-174 | Full replacement (delete + insert) |
| `delete_agent_skills(agent_name)` | 176-192 | Cleanup when agent deleted |
| `is_skill_assigned(agent_name, skill_name)` | 194-211 | Boolean check |
| `get_agents_with_skill(skill_name)` | 213-231 | Find all agents with a specific skill |

#### get_agent_skills()
**File**: `src/backend/db/skills.py:35-53`

```python
def get_agent_skills(self, agent_name: str) -> List[AgentSkill]:
    """
    Get all skills assigned to an agent.

    Args:
        agent_name: Name of the agent

    Returns:
        List of AgentSkill objects
    """
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, agent_name, skill_name, assigned_by, assigned_at
            FROM agent_skills
            WHERE agent_name = ?
            ORDER BY skill_name
        """, (agent_name,))
        return [self._row_to_skill(row) for row in cursor.fetchall()]
```

#### assign_skill()
**File**: `src/backend/db/skills.py:75-112`

```python
def assign_skill(
    self,
    agent_name: str,
    skill_name: str,
    assigned_by: str
) -> Optional[AgentSkill]:
    """
    Assign a skill to an agent.

    Returns:
        AgentSkill object if created, None if already exists
    """
    now = datetime.utcnow().isoformat()

    with get_db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("""
                INSERT INTO agent_skills (agent_name, skill_name, assigned_by, assigned_at)
                VALUES (?, ?, ?, ?)
            """, (agent_name, skill_name, assigned_by, now))
            conn.commit()

            return AgentSkill(
                id=cursor.lastrowid,
                agent_name=agent_name,
                skill_name=skill_name,
                assigned_by=assigned_by,
                assigned_at=datetime.fromisoformat(now)
            )
        except sqlite3.IntegrityError:
            # Skill already assigned (UNIQUE constraint)
            return None
```

#### set_agent_skills()
**File**: `src/backend/db/skills.py:134-174`

```python
def set_agent_skills(
    self,
    agent_name: str,
    skill_names: List[str],
    assigned_by: str
) -> int:
    """
    Set skills for an agent (full replacement).

    Removes all existing skills and assigns the new list.

    Returns:
        Number of skills assigned
    """
    now = datetime.utcnow().isoformat()

    with get_db_connection() as conn:
        cursor = conn.cursor()

        # Remove all existing skills for this agent
        cursor.execute("""
            DELETE FROM agent_skills WHERE agent_name = ?
        """, (agent_name,))

        # Add new skills
        for skill_name in skill_names:
            try:
                cursor.execute("""
                    INSERT INTO agent_skills (agent_name, skill_name, assigned_by, assigned_at)
                    VALUES (?, ?, ?, ?)
                """, (agent_name, skill_name, assigned_by, now))
            except sqlite3.IntegrityError:
                pass  # Skip duplicates

        conn.commit()
        return len(skill_names)
```

### Database Manager Delegation
**File**: `src/backend/database.py:1302-1318`

```python
def get_agent_skills(self, agent_name: str):
    return self._skills_ops.get_agent_skills(agent_name)

def assign_skill(self, agent_name: str, skill_name: str, assigned_by: str):
    return self._skills_ops.assign_skill(agent_name, skill_name, assigned_by)

def unassign_skill(self, agent_name: str, skill_name: str):
    return self._skills_ops.unassign_skill(agent_name, skill_name)

def set_agent_skills(self, agent_name: str, skill_names: list, assigned_by: str):
    return self._skills_ops.set_agent_skills(agent_name, skill_names, assigned_by)

def delete_agent_skills(self, agent_name: str):
    return self._skills_ops.delete_agent_skills(agent_name)
```

---

## Agent Lifecycle Integration

### Agent Deletion Cleanup
**File**: `src/backend/routers/agents.py:284`

When an agent is deleted, its skill assignments are also cleaned up:

```python
# routers/agents.py:284 (in delete_agent function)
db.delete_agent_skills(agent_name)
```

---

## Side Effects

### Skill Injection to Running Agents
When "Inject to Agent" is clicked, the assigned skills are written to the agent container's `.claude/skills/` directory. See [skill-injection.md](skill-injection.md) for details.

### No WebSocket Broadcasts
Skill assignment changes are persisted to the database but do not trigger WebSocket broadcasts. The UI updates locally after successful API calls.

### No Audit Logging
Skill assignments are not currently logged to the audit service.

---

## Error Handling

| Error Case | HTTP Status | Message |
|------------|-------------|---------|
| Skill not found in library | 404 | "Skill '{name}' not found in library" |
| Database constraint violation | 200 | Returns `{message: "Skill already assigned"}` |
| Invalid agent name | 404 | "Agent not found" (from agent lookup) |
| Unauthenticated | 401 | "Not authenticated" |

---

## Security Considerations

1. **Authentication Required**: All endpoints require `get_current_user` dependency
2. **Authorization TODO**: Access control for owner-only operations not yet implemented (noted in code)
3. **Input Validation**: Skill names validated against library before assignment
4. **SQL Injection Prevention**: All queries use parameterized statements

---

## Data Flow Diagram

```
User Action                Frontend                     Backend API              Database
-----------                --------                     -----------              --------

1. Open Skills tab    -->  loadData()
                           |
                           +--> GET /api/skills/library/status
                           +--> GET /api/skills/library
                           +--> GET /api/agents/{name}/skills  -->  db.get_agent_skills()  -->  SELECT
                                                                                                   |
                           <-- [library_status, available_skills, assigned_skills] <--------------+

2. Toggle checkboxes  -->  toggleSkill()
                           |
                           v
                           selectedSkills.add/delete()
                           hasChanges = true

3. Click Save         -->  saveAssignments()
                           |
                           +--> PUT /api/agents/{name}/skills  -->  db.set_agent_skills()  -->  DELETE + INSERT
                                { skills: [...] }                                                    |
                           <-- { success: true, skills_assigned: N } <-----------------------------+

4. Success toast      <--  successMessage = 'Skills saved'
```

---

## Testing

### Prerequisites
- Trinity platform running (`./scripts/deploy/start.sh`)
- Skills library configured in Settings (GitHub URL)
- At least one agent created

### Test Steps

1. **Load Skills Panel**
   - Action: Navigate to Agent Detail -> Skills tab
   - Expected: Grid of available skills with checkboxes
   - Verify: Skills show name and description, library status in footer

2. **Assign Skills**
   - Action: Check 2-3 skills, click Save
   - Expected: "Skills saved successfully" toast, Save button disabled
   - Verify: Refresh page, skills remain checked

3. **Unassign Skills**
   - Action: Uncheck a skill, click Save
   - Expected: "Skills saved successfully" toast
   - Verify: Database shows correct assignments (`sqlite3 ~/trinity-data/trinity.db "SELECT * FROM agent_skills"`)

4. **Search Skills**
   - Action: Type in search box
   - Expected: Grid filters to matching skills
   - Verify: Clear search shows all skills

5. **Inject to Running Agent**
   - Action: Start agent, click "Inject to Agent"
   - Expected: "Injected N skills" toast
   - Verify: SSH to agent, check `~/.claude/skills/` contains SKILL.md files

### Edge Cases
- Assign to non-existent agent (should fail gracefully)
- Assign skill not in library via direct API call (should return 404)
- Empty skills list (should clear all assignments)
- Duplicate assignment attempts (idempotent)

### Status: Untested (New Documentation)

---

## Related Flows

| Flow | Relationship |
|------|--------------|
| [skill-injection.md](skill-injection.md) | **Downstream** - Injects assigned skills to agent containers |
| [skills-crud.md](skills-crud.md) | **Upstream** - Admin management of skill library |
| [mcp-skill-tools.md](mcp-skill-tools.md) | **Related** - Programmatic skill assignment via MCP |
| [agent-lifecycle.md](agent-lifecycle.md) | **Lifecycle** - Skills deleted on agent deletion |
