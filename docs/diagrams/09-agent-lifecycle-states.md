# Diagram 09: Agent Lifecycle States

## What This Diagram Shows

This diagram illustrates the complete state machine for Trinity agent lifecycle management. It shows:

1. **All possible agent states** from creation to deletion
2. **State transitions** with triggers (user actions, API calls, scheduled events)
3. **Docker container status** corresponding to each agent state
4. **Side effects** that occur during each transition (credential injection, WebSocket broadcasts, volume management)
5. **Recovery paths** for interrupted or failed transitions

The lifecycle is the foundation of agent management - every agent operation (create, start, stop, delete) follows this state machine.

---

## State Machine Diagram

```
                                    ┌─────────────────────────────────────────────────────────────┐
                                    │                     AGENT LIFECYCLE                          │
                                    └─────────────────────────────────────────────────────────────┘

    ┌──────────┐                                                                              ┌──────────┐
    │          │                                                                              │          │
    │  (none)  │                                                                              │ DELETED  │
    │          │                                                                              │          │
    └────┬─────┘                                                                              └──────────┘
         │                                                                                          ▲
         │ CREATE                                                                                   │
         │ POST /api/agents                                                                         │
         │                                                                                          │
         ▼                                                                                          │
    ┌──────────┐          START              ┌──────────┐                                          │
    │          │   POST /api/agents/         │          │                                          │
    │ CREATED  │──────{name}/start──────────▶│ STARTING │                                          │
    │          │                             │          │                                          │
    └────┬─────┘                             └────┬─────┘                                          │
         │                                        │                                                │
         │                                        │ Container healthy                              │
         │                                        │ + Trinity injection                            │
         │                                        │ + Credential injection                         │
         │                                        │ + Skills injection                             │
         │                                        ▼                                                │
         │                                   ┌──────────┐                                          │
         │                                   │          │                                          │
         │                                   │ RUNNING  │◀───────────────────┐                     │
         │                                   │          │                    │                     │
         │                                   └────┬─────┘                    │                     │
         │                                        │                          │                     │
         │                                        │ STOP                     │ RESTART             │
         │                                        │ POST /api/agents/        │ (auto-recovery      │
         │                                        │ {name}/stop              │  or manual)         │
         │                                        ▼                          │                     │
         │                                   ┌──────────┐                    │                     │
         │                                   │          │                    │                     │
         │                                   │ STOPPING │                    │                     │
         │                                   │          │                    │                     │
         │                                   └────┬─────┘                    │                     │
         │                                        │                          │                     │
         │                                        │ Container stopped        │                     │
         │                                        ▼                          │                     │
         │                                   ┌──────────┐                    │                     │
         │                                   │          │ START              │                     │
         └──────────────────────────────────▶│ STOPPED  │────────────────────┘                     │
                                             │          │                                          │
                     DELETE                  └────┬─────┘                                          │
              DELETE /api/agents/{name}           │                                                │
              ────────────────────────────────────┴────────────────────────────────────────────────┘


                            ┌────────────────────────────────────────────────────┐
                            │              TRANSITIONAL STATES                    │
                            ├────────────────────────────────────────────────────┤
                            │  STARTING: Container started, waiting for health   │
                            │  STOPPING: Container stopping, cleanup in progress │
                            │  RECREATING: Config changed, rebuilding container  │
                            └────────────────────────────────────────────────────┘
```

---

## State Descriptions

| State | Description | Docker Status | Available Actions |
|-------|-------------|---------------|-------------------|
| **(none)** | Agent does not exist | No container | CREATE |
| **CREATED** | Container created but never started | `created` | START, DELETE |
| **STARTING** | Container starting, injections in progress | `running` (not healthy) | Wait |
| **RUNNING** | Agent fully operational, accepting requests | `running` | STOP, DELETE, RESTART |
| **STOPPING** | Container shutting down gracefully | `exited` (in progress) | Wait |
| **STOPPED** | Container stopped but preserved | `exited` or `dead` | START, DELETE |
| **DELETED** | Agent and all resources removed | No container | CREATE (new agent) |

### Docker Status Normalization

The backend normalizes Docker statuses for frontend display:

```python
# Source: src/backend/services/docker_service.py:38-44
docker_status = container.status
if docker_status in ("exited", "dead", "created"):
    normalized_status = "stopped"
elif docker_status == "running":
    normalized_status = "running"
else:
    normalized_status = docker_status  # paused, restarting, etc.
```

---

## Triggers and Side Effects

### CREATE Transition

**Trigger**: `POST /api/agents` (user action via UI or API)

**Source**: `src/backend/services/agent_service/crud.py:48-553`

| Step | Action | Source Line |
|------|--------|-------------|
| 1 | Sanitize agent name (lowercase, replace special chars) | `crud.py:76-77` |
| 2 | Check for existing agent | `crud.py:82-83` |
| 3 | Load template (GitHub or local) | `crud.py:95-181` |
| 4 | Auto-assign SSH port (2222+) | `crud.py:183-184` |
| 5 | Get assigned credentials from credential manager | `crud.py:187-197` |
| 6 | Generate credential files (.env, .mcp.json) | `crud.py:200-229` |
| 7 | Create agent MCP API key for Trinity access | `crud.py:274-283` |
| 8 | Build environment variables (ANTHROPIC_API_KEY, etc.) | `crud.py:285-356` |
| 9 | Create persistent workspace volume | `crud.py:362-372` |
| 10 | Mount Trinity meta-prompt volume | `crud.py:388-391` |
| 11 | Mount skills library volume | `crud.py:393-399` |
| 12 | Configure shared folder volumes (if enabled) | `crud.py:414-458` |
| 13 | Create container with security options | `crud.py:468-500` |
| 14 | Register agent ownership in database | `crud.py:518` |
| 15 | Grant default permissions (same-owner agents) | `crud.py:521-526` |
| 16 | Create git config (for GitHub-native agents) | `crud.py:529-542` |
| 17 | Broadcast WebSocket `agent_created` event | `crud.py:504-516` |

**Container Security Applied**:
```python
# Source: src/backend/services/agent_service/crud.py:489-496
security_opt=['apparmor:docker-default'],
cap_drop=['ALL'],
cap_add=FULL_CAPABILITIES if full_capabilities else RESTRICTED_CAPABILITIES,
tmpfs={'/tmp': 'noexec,nosuid,size=100m'},
network='trinity-agent-network'
```

**Volumes Created**:
- `agent-{name}-workspace` - Persistent workspace at `/home/developer`
- `trinity-skills-library` - Skills library at `/home/developer/.claude/skills-library`
- `agent-{name}-shared` - (if expose_enabled) Shared folder at `/home/developer/shared-out`

---

### START Transition

**Trigger**: `POST /api/agents/{name}/start` (user action via UI or API)

**Source**: `src/backend/services/agent_service/lifecycle.py:174-231`

| Step | Action | Source Line |
|------|--------|-------------|
| 1 | Get container from Docker | `lifecycle.py:190-192` |
| 2 | Check if container needs recreation (config changes) | `lifecycle.py:195-201` |
| 3 | Recreate container if needed (shared folders, API key, resources, capabilities) | `lifecycle.py:203-207` |
| 4 | Start container | `lifecycle.py:209` |
| 5 | Inject Trinity meta-prompt | `lifecycle.py:212` |
| 6 | Inject assigned credentials | `lifecycle.py:216` |
| 7 | Inject assigned skills | `lifecycle.py:220` |
| 8 | Broadcast WebSocket `agent_started` event | Router handles |

**Recreation Triggers** (checked before start):
```python
# Source: src/backend/services/agent_service/lifecycle.py:196-201
needs_recreation = (
    not check_shared_folder_mounts_match(container, agent_name) or
    not check_api_key_env_matches(container, agent_name) or
    not check_resource_limits_match(container, agent_name) or
    not check_full_capabilities_match(container, agent_name)
)
```

**Trinity Meta-Prompt Injection**:
```python
# Source: src/backend/services/agent_service/lifecycle.py:63-90
async def inject_trinity_meta_prompt(agent_name: str, max_retries: int = 5, retry_delay: float = 2.0):
    custom_prompt = db.get_setting_value("trinity_prompt", default=None)
    client = get_agent_client(agent_name)
    return await client.inject_trinity_prompt(
        custom_prompt=custom_prompt,
        force=False,
        max_retries=max_retries,
        retry_delay=retry_delay
    )
```

**Credential Injection**:
```python
# Source: src/backend/services/agent_service/lifecycle.py:93-171
async def inject_assigned_credentials(agent_name: str, max_retries: int = 3, retry_delay: float = 2.0):
    # Get owner, fetch assigned credentials, push to agent HTTP endpoint
    # Endpoint: http://agent-{name}:8000/api/credentials/inject (#2008 — this
    # diagram named /credentials/update, which had no callers and was removed)
```

---

### STOP Transition

**Trigger**: `POST /api/agents/{name}/stop` (user action via UI or API)

**Source**: `src/backend/routers/agents.py`

| Step | Action | Notes |
|------|--------|-------|
| 1 | Get container from Docker | `get_agent_container(agent_name)` |
| 2 | Stop container gracefully | `container.stop()` |
| 3 | Broadcast WebSocket `agent_stopped` event | Router handles |

**Important**: Container is PRESERVED (not removed). All data on the persistent volume remains intact. Agent can be restarted without data loss.

---

### DELETE Transition

**Trigger**: `DELETE /api/agents/{name}` (user action via UI or API)

**Source**: `src/backend/routers/agents.py`

| Step | Action | Notes |
|------|--------|-------|
| 1 | Check system agent protection | System agents cannot be deleted |
| 2 | Check authorization (owner or admin) | `db.can_user_delete_agent()` |
| 3 | Stop container (if running) | `container.stop()` |
| 4 | Remove container | `container.remove()` |
| 5 | Delete persistent workspace volume | `docker_client.volumes.get(f"agent-{name}-workspace").remove()` |
| 6 | Delete shared volume (if exists) | Shared folder volume cleanup |
| 7 | Delete schedules | Remove scheduled tasks |
| 8 | Delete git config | GitHub sync configuration |
| 9 | Delete MCP API key | Revoke Trinity MCP access |
| 10 | Delete agent permissions | Source and target permissions |
| 11 | Delete shared folder config | Shared folder settings |
| 12 | Delete ownership record | Cascades to shares via foreign key |
| 13 | Broadcast WebSocket `agent_deleted` event | Router handles |

---

## Container Recreation Flow

When configuration changes are detected at start time, the container is recreated:

**Source**: `src/backend/services/agent_service/lifecycle.py:234-386`

```
┌─────────────────────────────────────────────────────────────────────┐
│                    CONTAINER RECREATION FLOW                         │
└─────────────────────────────────────────────────────────────────────┘

    START requested
          │
          ▼
    ┌─────────────┐
    │ Check if    │
    │ recreation  │──── No changes ───▶ [Normal start flow]
    │ needed      │
    └──────┬──────┘
           │
           │ Changes detected:
           │ - Shared folder mounts
           │ - API key settings
           │ - Resource limits
           │ - Capability settings
           ▼
    ┌─────────────┐
    │ Extract old │
    │ container   │
    │ config      │
    └──────┬──────┘
           │
           ▼
    ┌─────────────┐
    │ Stop old    │
    │ container   │
    └──────┬──────┘
           │
           ▼
    ┌─────────────┐
    │ Remove old  │
    │ container   │
    └──────┬──────┘
           │
           ▼
    ┌─────────────┐
    │ Create new  │  (Same volumes, updated config)
    │ container   │
    └──────┬──────┘
           │
           ▼
    [Continue with normal start flow]
```

---

## Security Capability Sets

**Source**: `src/backend/services/agent_service/lifecycle.py:32-50`

### Restricted Capabilities (Default)

Minimum capabilities for agent operation:

```python
RESTRICTED_CAPABILITIES = [
    'NET_BIND_SERVICE',  # Bind to ports < 1024
    'SETGID', 'SETUID',  # Change user/group (for su/sudo)
    'CHOWN',             # Change file ownership
    'SYS_CHROOT',        # Use chroot
    'AUDIT_WRITE',       # Write to audit log
]
```

### Full Capabilities Mode

Additional capabilities for package installation (apt, pip):

```python
FULL_CAPABILITIES = RESTRICTED_CAPABILITIES + [
    'DAC_OVERRIDE',      # Bypass file permission checks (needed for apt)
    'FOWNER',            # Bypass permission checks on file owner
    'FSETID',            # Don't clear setuid/setgid bits
    'KILL',              # Send signals to processes
    'MKNOD',             # Create special files
    'NET_RAW',           # Use raw sockets (ping, etc.)
    'SYS_PTRACE',        # Trace processes (debugging)
]
```

### Prohibited Capabilities (Never Granted)

```python
PROHIBITED_CAPABILITIES = [
    'SYS_ADMIN',         # Mount filesystems, configure namespace
    'NET_ADMIN',         # Network administration
    'SYS_RAWIO',         # Raw I/O access
    'SYS_MODULE',        # Load kernel modules
    'SYS_BOOT',          # Reboot system
]
```

---

## WebSocket Events

| Event | Trigger | Payload |
|-------|---------|---------|
| `agent_created` | After CREATE completes | `{name, type, status, port, created, resources, container_id}` |
| `agent_started` | After START completes | `{name, trinity_injection, credentials_injection, skills_injection}` |
| `agent_stopped` | After STOP completes | `{name}` |
| `agent_deleted` | After DELETE completes | `{name}` |

---

## Error States and Recovery

| Error | Cause | Recovery |
|-------|-------|----------|
| Agent already exists (400) | Duplicate name | Choose different name |
| Agent not found (404) | Invalid agent name | Verify agent exists |
| Permission denied (403) | Not owner/admin | Contact owner or admin |
| Docker error (500) | Docker API failure | Retry, check Docker daemon |
| Docker unavailable (503) | Docker not running | Start Docker daemon |

---

## Sources

| File | Lines | Description |
|------|-------|-------------|
| `src/backend/services/agent_service/lifecycle.py` | 1-390 | Start, stop, injection, recreation logic |
| `src/backend/services/agent_service/crud.py` | 1-555 | Agent creation logic |
| `src/backend/services/docker_service.py` | 1-242 | Docker container operations |
| `src/backend/routers/agents.py` | — | API endpoints for lifecycle operations |
| `docs/memory/feature-flows/agent-lifecycle.md` | 1-840 | Feature flow documentation |

---

**Last Updated**: 2026-01-25
