# Feature: Automatic Skill Injection on Agent Start

> **Injection mechanics moved (ent#183, 2026-07-19)**: skills now ship as FULL
> directory packages (`SKILL.md` + `scripts/` + resources) with tree-SHA
> versioning — the start path passes `force=False`, so version-unchanged skills
> are skipped (`unchanged`) instead of rewritten. The canonical injection flow
> (packaging, transport, prune, dep-check warnings) is
> [skill-injection.md](skill-injection.md); this doc covers the START-path
> wiring only. Code excerpts below predate ent#183 where they show the old
> single-file `write_file` loop.

## Overview
When an agent starts, Trinity automatically injects all assigned skills into the agent container as full skill-directory packages under `~/.claude/skills/{name}/`. This happens after container start, Trinity prompt injection, and credential injection, and is skipped entirely for an already-running container that needed no recreation (#421).

## User Story
As a platform user, I want my agents to automatically receive their assigned skills when they start so that agents have consistent methodology guidance without manual intervention.

---

## Entry Points

| Trigger | API Endpoint | Purpose |
|---------|--------------|---------|
| Start button (UI) | `POST /api/agents/{agent_name}/start` | User clicks start in AgentDetail or Agents page |
| System deployment | Internal call to `start_agent_internal()` | System manifest deploys agent |
| Container restart | `POST /api/agents/{agent_name}/start` | Agent restart after config change |

---

## Complete Flow

```
User clicks Start
       |
       v
POST /api/agents/{name}/start
       |
       v
start_agent_endpoint() [routers/agents.py:321-345]
       |
       v
start_agent_internal() [lifecycle.py:215-272]
       |
       v
1. Check container recreation needed
2. container.start()
3. inject_trinity_meta_prompt()     --> Trinity planning commands
4. inject_assigned_credentials()    --> .env, .mcp.json files
5. inject_assigned_skills()         --> .claude/skills/{name}/ (full package,
       |                                   force=False: unchanged versions skipped)
       |                                   + CLAUDE.md "Platform Skills" section
       v
Return result with skills_injection status
```

---

## Frontend Layer

### Entry Points

**Agents List Page** - `src/frontend/src/views/Agents.vue:200`
```html
<button
  v-if="agent.status === 'stopped'"
  @click="startAgent(agent.name)"
  ...
>
```

**Agent Detail Page** - Uses composable for lifecycle
- `src/frontend/src/composables/useAgentLifecycle.js:19-31`

### Store Action (`src/frontend/src/stores/agents.js:151-165`)

```javascript
async startAgent(name) {
  try {
    const authStore = useAuthStore()
    const response = await axios.post(`/api/agents/${name}/start`, {}, {
      headers: authStore.authHeader
    })
    const agent = this.agents.find(a => a.name === name)
    if (agent) agent.status = 'running'
    return { success: true, message: response.data?.message || `Agent ${name} started` }
  } catch (error) {
    // error handling
  }
}
```

---

## Backend Layer

### API Endpoint (`src/backend/routers/agents.py:321-345`)

```python
@router.post("/{agent_name}/start")
async def start_agent_endpoint(agent_name: AuthorizedAgentByName, request: Request, current_user: CurrentUser):
    """Start an agent."""
    try:
        result = await start_agent_internal(agent_name)
        trinity_status = result.get("trinity_injection", "unknown")
        credentials_status = result.get("credentials_injection", "unknown")
        credentials_result = result.get("credentials_result", {})

        if manager:
            await manager.broadcast(json.dumps({
                "event": "agent_started",
                "data": {"name": agent_name, "trinity_injection": trinity_status, "credentials_injection": credentials_status}
            }))

        return {
            "message": f"Agent {agent_name} started",
            "trinity_injection": trinity_status,
            "credentials_injection": credentials_status,
            "credentials_result": credentials_result
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to start agent: {str(e)}")
```

### Start Internal Function (`src/backend/services/agent_service/lifecycle.py:215-272`)

```python
async def start_agent_internal(agent_name: str) -> dict:
    """
    Internal function to start an agent.
    Triggers Trinity meta-prompt injection, credential injection, and skill injection.
    """
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Check if container needs recreation for shared folders, API key, resource limits, or capabilities
    container.reload()
    needs_recreation = (
        not check_shared_folder_mounts_match(container, agent_name) or
        not check_api_key_env_matches(container, agent_name) or
        not check_resource_limits_match(container, agent_name) or
        not check_full_capabilities_match(container, agent_name)
    )

    if needs_recreation:
        await recreate_container_with_updated_config(agent_name, container, "system")
        container = get_agent_container(agent_name)

    container.start()

    # Step 1: Inject Trinity meta-prompt
    trinity_result = await inject_trinity_meta_prompt(agent_name)
    trinity_status = trinity_result.get("status", "unknown")

    # Step 2: Inject assigned credentials from the Credentials page
    credentials_result = await inject_assigned_credentials(agent_name)
    credentials_status = credentials_result.get("status", "unknown")

    # Step 3: Inject assigned skills from the Skills page
    skills_result = await inject_assigned_skills(agent_name)
    skills_status = skills_result.get("status", "unknown")

    return {
        "message": f"Agent {agent_name} started",
        "trinity_injection": trinity_status,
        "trinity_result": trinity_result,
        "credentials_injection": credentials_status,
        "credentials_result": credentials_result,
        "skills_injection": skills_status,
        "skills_result": skills_result
    }
```

### Skill Injection Function (`src/backend/services/agent_service/lifecycle.py:174-212`)

```python
async def inject_assigned_skills(agent_name: str) -> dict:
    """
    Inject assigned skills into a running agent.

    This is called after agent startup to push any skills that were
    assigned to this agent in the Skills tab.

    Args:
        agent_name: Name of the agent

    Returns:
        dict with injection status
    """
    from database import db

    # Get assigned skills from database
    skill_names = db.get_agent_skill_names(agent_name)

    if not skill_names:
        logger.debug(f"No assigned skills for agent {agent_name}")
        return {"status": "skipped", "reason": "no_skills"}

    logger.info(f"Injecting {len(skill_names)} skills into agent {agent_name}: {skill_names}")

    # Inject skills via skill_service
    result = await skill_service.inject_skills(agent_name, skill_names)

    if result.get("success"):
        return {
            "status": "success",
            "skills_injected": result.get("skills_injected", 0)
        }
    else:
        return {
            "status": "partial" if result.get("skills_injected", 0) > 0 else "failed",
            "skills_injected": result.get("skills_injected", 0),
            "skills_failed": result.get("skills_failed", 0),
            "results": result.get("results", {})
        }
```

---

## Service Layer

### SkillService.inject_skills() (`src/backend/services/skill_service.py:300-374`)

```python
async def inject_skills(
    self,
    agent_name: str,
    skill_names: Optional[List[str]] = None
) -> Dict[str, Any]:
    """
    Inject skills into a running agent.

    Copies SKILL.md files to .claude/skills/<name>/SKILL.md in the agent,
    then updates CLAUDE.md with a "Platform Skills" section.

    Args:
        agent_name: Name of the agent
        skill_names: List of skill names to inject, or None to use assigned skills

    Returns:
        Dict with injection results for each skill
    """
    if skill_names is None:
        skill_names = db.get_agent_skill_names(agent_name)

    if not skill_names:
        return {
            "success": True,
            "message": "No skills to inject",
            "skills_injected": 0
        }

    client = get_agent_client(agent_name)
    results = {}
    success_count = 0
    error_count = 0

    for skill_name in skill_names:
        skill = self.get_skill(skill_name)
        if not skill:
            results[skill_name] = {
                "success": False,
                "error": "Skill not found in library"
            }
            error_count += 1
            continue

        try:
            # Write skill to agent via AgentClient
            path = f".claude/skills/{skill_name}/SKILL.md"
            result = await client.write_file(path, skill["content"])

            if result.get("success"):
                results[skill_name] = {"success": True}
                success_count += 1
            else:
                results[skill_name] = {
                    "success": False,
                    "error": result.get("error", "Write failed")
                }
                error_count += 1

        except AgentClientError as e:
            results[skill_name] = {
                "success": False,
                "error": str(e)
            }
            error_count += 1

    # Update CLAUDE.md with skills section so the agent knows what skills it has
    if success_count > 0:
        injected_skills = [name for name, res in results.items() if res.get("success")]
        await self._update_claude_md_skills_section(client, injected_skills)

    return {
        "success": error_count == 0,
        "skills_injected": success_count,
        "skills_failed": error_count,
        "results": results
    }
```

### SkillService._update_claude_md_skills_section() (`src/backend/services/skill_service.py:376-435`)

After injecting skill files, updates the agent's `workspace/CLAUDE.md` with a "Platform Skills" section listing the injected skills. This enables the agent to answer questions like "what skills do you have?"

```python
async def _update_claude_md_skills_section(
    self,
    client,
    skill_names: List[str]
) -> None:
    """
    Update CLAUDE.md with a Platform Skills section.
    """
    try:
        # Read current CLAUDE.md using AgentClient.read_file()
        result = await client.read_file("workspace/CLAUDE.md")

        if not result.get("success"):
            logger.warning(f"Could not read CLAUDE.md: {result.get('error')}")
            return

        content = result.get("content") or ""

        # Build skills section
        skills_list = "\n".join([f"- `/{skill}` - Use with /{skill} command" for skill in sorted(skill_names)])
        skills_section = f"""

## Platform Skills

This agent has the following skills installed in `~/.claude/skills/`:

{skills_list}

Use these skills by invoking their slash commands (e.g., `/{skill_names[0] if skill_names else 'skill-name'}`).
"""

        # Remove existing Platform Skills section if present
        if "## Platform Skills" in content:
            # Find start and end of section
            start_idx = content.index("## Platform Skills")
            rest = content[start_idx + len("## Platform Skills"):]
            next_section = rest.find("\n## ")
            if next_section != -1:
                end_idx = start_idx + len("## Platform Skills") + next_section
                content = content[:start_idx].rstrip() + content[end_idx:]
            else:
                content = content[:start_idx].rstrip()

        # Append skills section
        content = content.rstrip() + skills_section

        # Write back using AgentClient.write_file()
        write_result = await client.write_file("workspace/CLAUDE.md", content)
        if write_result.get("success"):
            logger.info(f"Updated CLAUDE.md with {len(skill_names)} skills")
        else:
            logger.warning(f"Failed to update CLAUDE.md: {write_result.get('error')}")

    except Exception as e:
        logger.warning(f"Failed to update CLAUDE.md with skills: {e}")
```

**CLAUDE.md Section Example**:
```markdown
## Platform Skills

This agent has the following skills installed in `~/.claude/skills/`:

- `/clip-video` - Use with /clip-video command
- `/commit` - Use with /commit command
- `/verification` - Use with /verification command

Use these skills by invoking their slash commands (e.g., `/clip-video`).
```

### AgentClient.read_file() (`src/backend/services/agent_client.py:470-506`)

```python
async def read_file(
    self,
    path: str,
    timeout: float = 30.0
) -> dict:
    """
    Read content from a file in the agent's workspace.

    Args:
        path: File path within /home/developer
        timeout: Request timeout

    Returns:
        dict with success status and content
    """
    try:
        import urllib.parse
        encoded_path = urllib.parse.quote(path, safe='')

        response = await self.get(
            f"/api/files/download?path={encoded_path}",
            timeout=timeout
        )

        if response.status_code == 200:
            return {"success": True, "content": response.text}
        elif response.status_code == 404:
            return {"success": True, "content": None, "not_found": True}
        else:
            return {
                "success": False,
                "error": response.text,
                "status_code": response.status_code
            }

    except AgentClientError as e:
        return {"success": False, "error": str(e)}
```

### AgentClient.write_file() (`src/backend/services/agent_client.py:508-547`)

```python
async def write_file(
    self,
    path: str,
    content: str,
    timeout: float = 30.0
) -> dict:
    """
    Write content to a file in the agent's workspace.
    Creates parent directories if they don't exist.

    Args:
        path: File path within /home/developer
        content: File content to write
        timeout: Request timeout

    Returns:
        dict with success status and file info
    """
    try:
        import urllib.parse
        encoded_path = urllib.parse.quote(path, safe='')

        response = await self.put(
            f"/api/files?path={encoded_path}",
            json={"content": content},
            timeout=timeout
        )

        if response.status_code == 200:
            return {"success": True, **response.json()}
        else:
            return {
                "success": False,
                "error": response.text,
                "status_code": response.status_code
            }

    except AgentClientError as e:
        return {"success": False, "error": str(e)}
```

---

## Database Layer

### Get Agent Skill Names (`src/backend/db/skills.py:55-73`)

```python
def get_agent_skill_names(self, agent_name: str) -> List[str]:
    """
    Get skill names assigned to an agent.

    Args:
        agent_name: Name of the agent

    Returns:
        List of skill names
    """
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT skill_name
            FROM agent_skills
            WHERE agent_name = ?
            ORDER BY skill_name
        """, (agent_name,))
        return [row["skill_name"] for row in cursor.fetchall()]
```

### Database Schema (`src/backend/db/schema.py`)

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

---

## Agent Container Layer

### Files Download Endpoint (`docker/base-image/agent_server/routers/files.py:112-153`)

Used by `AgentClient.read_file()` to read CLAUDE.md content before updating.

```python
@router.get("/api/files/download")
async def download_file(path: str):
    """
    Download a file from the workspace.
    Only allows access to /home/developer for security.
    """
    allowed_base = Path("/home/developer")

    # Handle both absolute and relative paths
    if path.startswith('/'):
        requested_path = Path(path).resolve()
    else:
        requested_path = (allowed_base / path).resolve()

    # Security: Ensure path is within workspace
    if not str(requested_path).startswith(str(allowed_base)):
        raise HTTPException(status_code=403, detail="Access denied")

    if not requested_path.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {path}")

    if not requested_path.is_file():
        raise HTTPException(status_code=400, detail=f"Not a file: {path}")

    # Read and return content
    content = requested_path.read_text(encoding='utf-8', errors='replace')
    return PlainTextResponse(content=content)
```

### Files Update Endpoint (`docker/base-image/agent_server/routers/files.py:314-371`)

```python
@router.put("/api/files")
async def update_file(path: str, request: FileUpdateRequest):
    """
    Update or create a file's content in the workspace.
    Creates parent directories if they don't exist.
    """
    allowed_base = Path("/home/developer")

    # Handle both absolute and relative paths
    if path.startswith('/'):
        requested_path = Path(path).resolve()
    else:
        requested_path = (allowed_base / path).resolve()

    # Security: Ensure path is within workspace
    if not str(requested_path).startswith(str(allowed_base)):
        raise HTTPException(status_code=403, detail="Access denied")

    # Check protected paths
    if _is_edit_protected_path(requested_path):
        raise HTTPException(status_code=403, detail=f"Cannot edit protected path")

    try:
        # Create parent directories if needed
        requested_path.parent.mkdir(parents=True, exist_ok=True)

        # Write the content
        requested_path.write_text(request.content, encoding='utf-8')
        stat = requested_path.stat()

        return {
            "success": True,
            "path": path,
            "size": stat.st_size,
            "modified": iso_z_from_mtime(stat.st_mtime)  # ISO-Z UTC, TZ-independent (#1795)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update file: {str(e)}")
```

---

## Result in Agent Container

After successful skill injection, the agent container has:

```
/home/developer/
  workspace/
    CLAUDE.md             # Updated with "## Platform Skills" section
  .claude/
    skills/
      verification/
        SKILL.md          # Verification methodology
      systematic-debugging/
        SKILL.md          # Debugging methodology
      tdd/
        SKILL.md          # Test-driven development
      code-review/
        SKILL.md          # Code review methodology
```

**Two mechanisms ensure the agent knows about its skills:**

1. **SKILL.md Files**: Claude Code automatically reads these files as additional context when processing requests (via `.claude/skills/` convention).

2. **CLAUDE.md Section**: The "Platform Skills" section is appended to the agent's `workspace/CLAUDE.md`, enabling the agent to answer questions like "what skills do you have?" even without scanning the filesystem.

---

## Side Effects

### WebSocket Broadcast

The start endpoint broadcasts an `agent_started` event (but skills_injection status is NOT included in broadcast - only in HTTP response):

```json
{
  "event": "agent_started",
  "data": {
    "name": "my-agent",
    "trinity_injection": "success",
    "credentials_injection": "success"
  }
}
```

### Logging

```
INFO - Injecting 3 skills into agent my-agent: ['verification', 'tdd', 'code-review']
INFO - Updated file: /home/developer/.claude/skills/verification/SKILL.md
INFO - Updated file: /home/developer/.claude/skills/tdd/SKILL.md
INFO - Updated file: /home/developer/.claude/skills/code-review/SKILL.md
INFO - Updated CLAUDE.md with 3 skills
```

---

## Return Value Structure

The `start_agent_internal()` function returns:

```python
{
    "message": "Agent my-agent started",
    "trinity_injection": "success",           # Trinity prompt status
    "trinity_result": {...},
    "credentials_injection": "success",        # Credentials status
    "credentials_result": {...},
    "skills_injection": "success",            # Skills status
    "skills_result": {
        "status": "success",
        "skills_injected": 3
    }
}
```

Possible `skills_injection` values:
- `"success"` - All skills injected successfully
- `"partial"` - Some skills injected, some failed
- `"failed"` - All skill injections failed
- `"skipped"` - No skills assigned to agent

---

## Error Handling

| Error Case | Status | Result |
|------------|--------|--------|
| No skills assigned | `skipped` | `{"status": "skipped", "reason": "no_skills"}` |
| Skill not found in library | `partial`/`failed` | Individual skill marked as failed in results |
| Agent not reachable | `failed` | AgentClientError captured in results |
| File write failed | `partial`/`failed` | HTTP error from agent captured in results |

The skill injection does NOT fail the entire agent start - it reports status but allows agent to run even if skills fail to inject.

---

## Security Considerations

1. **Path Validation**: Agent container validates paths are within `/home/developer`
2. **Protected Paths**: Cannot write to `.git`, `.trinity`, `.env`, etc. (but `.claude/skills/` is allowed)
3. **No Credential Exposure**: Skills are methodology guides, not secrets
4. **Authorization**: Start endpoint uses `AuthorizedAgentByName` - only owner/shared users/admin can start

---

## Testing

### Prerequisites
- [ ] Backend running at http://localhost:8000
- [ ] Frontend running at http://localhost
- [ ] At least one skill defined in platform (Skills page)
- [ ] Test agent created with skills assigned

### Test Steps

1. **Assign Skills to Agent**
   - Navigate to Agent Detail -> Skills tab
   - Check skills to assign (e.g., "verification", "tdd")
   - Click "Save"

2. **Stop Agent** (if running)
   - Click Stop button
   - Wait for agent to stop

3. **Start Agent**
   - Click Start button
   - Watch for success notification

4. **Verify Skill Files**
   - Navigate to File Manager (`/files`) and select the agent
   - Browse to `.claude/skills/`
   - Verify each assigned skill has a `SKILL.md` file

5. **Verify API Response**
   - Check browser network tab for `/api/agents/{name}/start` response
   - Confirm `skills_injection: "success"` in response
   - Confirm `skills_result.skills_injected` matches assigned count

### Edge Cases
- [ ] Agent with no skills assigned -> `skills_injection: "skipped"`
- [ ] Skill deleted from platform after assignment -> Individual skill fails, others succeed
- [ ] Agent container not responding -> Skills fail but agent marked as running

---

## Related Flows

- **Upstream**: [agent-skill-assignment.md](agent-skill-assignment.md) - How skills get assigned to agents
- **Upstream**: [skills-crud.md](skills-crud.md) - How platform skills are created/managed
- **Related**: [agent-lifecycle.md](agent-lifecycle.md) - Full agent start/stop flow
- **Related**: [credential-injection.md](credential-injection.md) - Credential injection (happens before skills)
- **Downstream**: Agent uses skills via Claude Code's automatic SKILL.md loading

---

## Revision History

| Date | Changes |
|------|---------|
| 2026-01-25 | Added CLAUDE.md "Platform Skills" section update - `_update_claude_md_skills_section()` method, `AgentClient.read_file()` method documentation, updated flow diagram and result section |
| 2026-01-25 | Initial creation - documented automatic skill injection on agent start |

---

**Last Updated**: 2026-01-25
**Status**: Verified - All file paths and line numbers confirmed accurate
