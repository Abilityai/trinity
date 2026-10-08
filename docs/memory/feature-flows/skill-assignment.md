# Feature: Skill Assignment to Agents

> **Created**: 2026-01-25 - Complete vertical slice from SkillsPanel.vue through routers/skills.py to db/skills.py

## Revision History

| Date | Changes |
|------|---------|
| 2026-10-08 | **trinity-enterprise#754** — the assignment UI is now the **Shared skills** section of the merged Skills tab ([skills-tab.md](skills-tab.md)), visible to everyone with access; the controls stay owner/admin-only. `SkillsPanel.vue` is deleted: assigned rows became cards, the library picker moved into `SkillAssignModal.vue` ("Assign skills"), the #2914 conflict / delivery warnings / superseded text open in `SkillDetailsModal.vue`, and sets show as chips with `AgentSkillSets.vue` in a "Manage sets" dialog. Routes and store unchanged. |
| 2026-10-07 | **trinity-enterprise#753** — every route here that writes `agent_skills` (PUT, POST, DELETE, the inject Sync, POST/DELETE set) now also keeps the agent's skill gate map in line (`_sync_gates` → `skill_gate_map_service`), and answers with a `gates` block (`gate_defaults` applied/removed, `gates_removed`, `gates_kept`, or null). A library skill whose metadata says `approval: recommended` is gated BEFORE its package is delivered; a gate on an unassigned skill goes only once its package removal completed, and an explicit gate only when a person unassigned it. See [skill-gate.md](skill-gate.md) §6. |
| 2026-09-30 | **ent#672** — a deprecated library skill is marked and its assignment says so. `SkillsPanel.vue` shows a `deprecated` badge + "Superseded by …" line on the assigned rows (an agent already holding one shows it on load) and on the picker rows (before the tick); `POST` / `PUT` still succeed and their `delivery.skills[name].warnings` carries `deprecated[:<successor>]`, rendered as a warning line under the save note; after a manual Sync the per-skill warning list says it in words. See "Deprecated skills" below and [skill-injection.md](skill-injection.md). |
| 2026-09-24 | **ent#530** — skill **sets**. `agent_skill_sets` + `agent_skills.individual`; `POST`/`DELETE /api/agents/{name}/skill-sets/{set}` (ent#596 fence), `GET …/skill-sets` (honest status), `GET /api/skills/library/sets`. The PUT adds `set:` entries / replaces with `sets` (absent = untouched) and resolves held sets inside its transaction so set-derived rows survive a replace; a single unassign of a set-named skill answers `retained_via_sets`. The Skills tab renders `AgentSkillSets.vue`, badges `via <set>`, locks set-only members, and its draft is the INDIVIDUAL list. See *Skill sets* below. |
| 2026-09-11 | **#2703** — assignment now DELIVERS. `POST` (both branches) and `PUT` (added names) call `skill_service.deliver_assigned` and return a `delivery` block beside the existing keys; `PUT` is symmetric (added injected, dropped removed). Every listing change — assign, unassign, replace, manual Sync, background completion, fleet sweep — fires the thin `agent_skills_changed` WS trigger, and the Skills tab's Save note / the Library control say what happened instead of "Saved. Sync now, or…". Full flow in [skill-injection.md](skill-injection.md) → *Delivery on assign*. |
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
- **Component**: `src/frontend/src/components/skills/SkillAssignModal.vue:75-77` - "Save assignments" (bulk PUT), opened by **Assign skills** in the Skills tab's Shared section (`SkillsTab.vue:133`)
- **Tab Access**: Agent Detail page -> Skills tab, visible to everyone with access since trinity-enterprise#754; the assignment controls (Assign skills, Manage sets, Sync now, Unassign) are for the owner or an admin, never on the system agent. See [skills-tab.md](skills-tab.md)

### Backend API
- `GET /api/agents/{name}/skills` - List assigned skills
- `PUT /api/agents/{name}/skills` - Bulk update assignments (replace all)
- `POST /api/agents/{name}/skills/{skill}` - Add single skill
- `DELETE /api/agents/{name}/skills/{skill}` - Remove single skill
- `POST /api/agents/{name}/skills/inject` - Push assigned skills to running agent

---

## Frontend Layer

### Components

Since trinity-enterprise#754 the assignment surface is the **Shared skills** section of the agent's Skills tab; the tab itself (cards, Run, approval, the own list) is documented once in [skills-tab.md](skills-tab.md). `SkillsPanel.vue` is deleted. The writers:

| Control | File | Call |
|---|---|---|
| **Assign skills** → tick → **Save assignments** | `components/skills/SkillAssignModal.vue:134-146` | `store.saveAssignments(draft)` → `PUT /api/agents/{name}/skills` |
| **Unassign** on a Shared card | `components/skills/SkillsTab.vue:541-559`, after its confirm (`:561-573`) | `store.saveAssignments(individualNames − name)` → the same PUT |
| **Unassign library skill** (#2914 conflict) | `components/skills/SkillDetailsModal.vue:31-41` → `SkillsTab.onUnassign` | the same PUT |
| **Manage sets** | `components/skills/AgentSkillSets.vue` in a `BaseModal` (`SkillsTab.vue:266-271`) | `store.assignSet` / `unassignSet` → `POST` / `DELETE /api/agents/{name}/skill-sets/{set}` |
| **Sync now** | `SkillsTab.vue:135-144,581-589` | `store.inject()` → `POST /api/agents/{name}/skills/inject` |

The picker keeps its ent#235 / ent#530 / ent#672 / #2914 behaviour: the draft is the INDIVIDUAL list, a set-only member is ticked and locked, a deprecated skill is marked before it is ticked, and a draft survives a sync that re-reads the same assignment set. Package fact chips render through the shared seam `components/skills/{SkillContractChips.vue, contract.js}` (ent#263, shared with the Library page).

### State Management

**File**: `src/frontend/src/stores/skills.js` (agent-scoped; all HTTP through `api`, Invariant #7)

- `library`, `libraryStatus`, `assigned` (the rows, with `individual` / `via_sets` / `delivery_status`), `sets`, `librarySets`; `injectionResults` + `lastInjectionAt` (the last Sync, per skill); `lastDelivery` (the last save's #2703 report).
- Derived: `individualNames` (the draft's source — what the bulk PUT replaces), `setOnlyNames` (locked rows), `conflictNames` (#2914), `emptyReason` (`library_unconfigured` / `library_empty` / `none_assigned`).
- The picker's dirty check compares the sorted draft with `individualNames` (`SkillAssignModal.vue:120-124`); the draft resets only when that set changes (`:132`).

### API Calls

**`load(name)`** (`stores/skills.js:250-290`): `GET /api/skills/library/status` + `GET /api/agents/{name}/skills` together; only when the library is configured, `GET /api/skills/library` and `loadSets()` (`GET /api/agents/{name}/skill-sets?probe=true`, `GET /api/skills/library/sets`). A failed read is an error, never a confident empty.

**`saveAssignments(names)`** (`stores/skills.js:369-391`):

```javascript
const { data: saved } = await api.put(
  `/api/agents/${agentName.value}/skills`, { skills: names }, { timeout: ASSIGN_TIMEOUT_MS },  // 45 s: the PUT delivers (#2703)
)
lastDelivery.value = saved?.delivery ?? null
const { data } = await api.get(`/api/agents/${agentName.value}/skills`)   // re-read the rows
```

The dialog then emits `saved` with `deliveryText(lastDelivery, {saved: true})` and `deprecationText(lastDelivery)` (`SkillAssignModal.vue:134-146`); the tab shows it on the Shared line and arms the Sync emphasis only when delivery did not land.

**`inject()`** (`stores/skills.js:403-432`): `POST /api/agents/{name}/skills/inject` (a repair: `force` server-side). Results are stored per skill (`injectionResults`), then the rows are re-read so a #2914 verdict that was set or cleared shows at once. A 409 reads "A skill sync is already running for this agent. Try again in a moment."

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
When **Sync now** is clicked, the assigned skills are written to the agent container's `.claude/skills/` directory. See [skill-injection.md](skill-injection.md) for details.

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

1. Open Skills tab    -->  stores/skills.js load()
                           |
                           +--> GET /api/skills/library/status
                           +--> GET /api/skills/library
                           +--> GET /api/agents/{name}/skills  -->  db.get_agent_skills()  -->  SELECT
                                                                                                   |
                           <-- [library_status, available_skills, assigned_skills] <--------------+

2. Assign skills,     -->  SkillAssignModal.vue draft (checkbox v-model)
   tick boxes              |
                           v
                           dirty = draft != individualNames

3. Save assignments   -->  store.saveAssignments(draft)
                           |
                           +--> PUT /api/agents/{name}/skills  -->  db.set_agent_skills()  -->  DELETE + INSERT
                                { skills: [...] }                                                    |
                           <-- { success: true, skills_assigned: N } <-----------------------------+

4. Delivery note      <--  deliveryText(lastDelivery), via the dialog's `saved` event (dialog + Shared line)
```

---

## Testing

### Prerequisites
- Trinity platform running (`./scripts/deploy/start.sh`)
- Skills library configured in Settings (GitHub URL)
- At least one agent created

### Test Steps

1. **Open the Shared section**
   - Action: Navigate to Agent Detail -> Skills tab
   - Expected: The Shared skills section lists the assignments as cards (or a named empty state); the owner sees **Assign skills**, **Manage sets** and **Sync now** in its head
   - Verify: A shared (non-owner) user sees the cards without those controls

2. **Assign Skills**
   - Action: **Assign skills**, tick 2-3 skills, **Save assignments**
   - Expected: The delivery note ("Saved and delivered — available now" on a running agent); Save is disabled again
   - Verify: Refresh page, the skills are Shared cards and ticked in the dialog

3. **Unassign Skills**
   - Action: **Unassign** on a Shared card (or untick in the dialog and save)
   - Expected: "Unassigned <name>." on the Shared line
   - Verify: Database shows correct assignments (`sqlite3 ~/trinity-data/trinity.db "SELECT * FROM agent_skills"`)

4. **Filter Skills**
   - Action: Type in the tab's filter box
   - Expected: Both sections narrow to matching skills
   - Verify: Clearing it shows all skills

5. **Sync to Running Agent**
   - Action: Start agent, click **Sync now**
   - Expected: Each Shared card's badge shows the result (`synced` / `up to date` / `partial` / `failed`); "Last sync <time>" on the Shared line
   - Verify: In the agent, `~/.claude/skills/` contains the SKILL.md files

### Edge Cases
- Assign to non-existent agent (should fail gracefully)
- Assign skill not in library via direct API call (should return 404)
- Empty skills list (should clear all assignments)
- Duplicate assignment attempts (idempotent)

### Status: Untested (New Documentation)

---

## Deprecated skills (ent#672)

```
library SKILL.md frontmatter (deprecated, superseded-by)
  → skill_packaging.extract_contract → _parse_skill_info → GET /api/skills/library (SkillInfo)
  → stores/skills.js library / assignedSkills (the library entries joined to the rows)
  → utils/skillCards.js: badge + "Superseded by …" note on the Shared card   (ent#754)
  → SkillAssignModal.vue: badge + supersededLine() on the picker row

Save (PUT) / assign (POST)  — unchanged: the row is written and delivered
  → skill_service.deliver_assigned: report.skills[name].warnings = ["deprecated[:<successor>]"]
  → SkillAssignModal.onSave: emit('saved', {…, deprecation: deprecationText(store.lastDelivery)})
  → SkillsTab.onSaved: notice = that verdict (the dialog and the Shared line render it)
```

- **Nothing is refused or hidden.** Assigning a deprecated skill is allowed; the marker is on the picker before the tick and the note is on the save after it. No confirm dialog, no 409.
- **An agent that already holds one** shows the badge on its Shared card because the card is the library entry joined to the assignment — nothing about the assignment, the package or the agent's CLAUDE.md changes.
- **The note belongs to its save**: it rides the tab's `notice` object, written from the dialog's `saved` event and replaced by the next verb, and is rendered only beside that save's note — never a computed over `store.lastDelivery` (which outlives the note and is also written by a set assign).
- **After a manual Sync** the injection result carries `deprecated[:<successor>]`, and the card's warning list leaves it out (`utils/skillCards.js::runWarnings`): that list says what went wrong with the delivery, and the badge already says the skill is retired.
- **Skill sets** are not marked: a deprecated member gets the delivery warning on the wire, but the set notes do not render it.

## Skill sets (ent#530)

```
AgentSkillSets.vue (the Skills tab's "Manage sets" dialog, ent#754) / LibrarySkillSets.vue
  → stores/skills.js assignSet / unassignSet   (stores/skillsLibrary.js assignSet)
  → POST|DELETE /api/agents/{a}/skill-sets/{set}   [get_skill_managed_agent_by_name]
  → skill_set_service.assign|unassign  (require_set: 404 unknown_set / 422 set_member_missing)
  → db.assign_skill_set|unassign_skill_set → db/skill_sets._apply  (one txn, plan_member_rows)
  → _deliver_assigned_skills(added) | _remove_unassigned_skills(removed)
```

- **Rows**: members are `agent_skills` rows with `individual = 0`; a member also assigned on its own keeps `1`. `_apply` re-reads the rows and assigned sets inside the transaction and re-plans — add members with no row, drop `individual = 0` rows no assigned set names — and removes nothing while any assigned set is unresolvable (fail-closed).
- **Overlap**: each assignment stands on its own. `POST /skills/{s}` on a set-derived row promotes it to `individual = 1`; `DELETE /skills/{s}` on a set-named skill demotes it to `0` and answers `{removed: false, retained_via_sets}`.
- **Bulk PUT**: `skills` is the individual list; `set:` entries ADD sets (all-or-nothing), the `sets` field replaces them, neither leaves them. `db.set_agent_skills(set_resolver=…)` resolves the held sets inside its own transaction under the agent lock and keeps every row a set still names — listed rows keep their flag, unlisted ones are demoted to `individual = 0` — so a legacy read-then-write client can neither drop nor promote them.
- **Reads**: `GET /skills` carries `individual` + `via_sets`; `GET /skill-sets` the per-set status (`skill_set_service.agent_set_status`).

## Related Flows

| Flow | Relationship |
|------|--------------|
| [skills-tab.md](skills-tab.md) | **UI** - The Skills tab whose Shared section is this flow's surface (trinity-enterprise#754) |
| [skill-injection.md](skill-injection.md) | **Downstream** - Injects assigned skills to agent containers |
| [skills-crud.md](skills-crud.md) | **Upstream** - Admin management of skill library |
| [mcp-skill-tools.md](mcp-skill-tools.md) | **Related** - Programmatic skill assignment via MCP |
| [agent-lifecycle.md](agent-lifecycle.md) | **Lifecycle** - Skills deleted on agent deletion |
