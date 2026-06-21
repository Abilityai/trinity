# Feature: Container Capabilities Configuration

## Overview

Controls whether agent containers run with full Docker capabilities (allowing package installation via apt-get) or restricted capabilities (secure default). This is implemented as both a **system-wide setting** and a **per-agent API endpoint** for fine-grained control.

## User Story

**CFG-004**: As a platform operator, I want to enable full capabilities mode for agent containers so that agents can install packages with apt-get and have full control of their container environment.

## Entry Points

- **System-Wide Setting**: `agent_full_capabilities` setting in SQLite via Settings service
- **Per-Agent API**: `GET/PUT /api/agents/{name}/capabilities` (router endpoint, per-agent override stored in `agent_ownership` table)
- **UI**: Currently no dedicated UI toggle - managed via Settings API or direct API calls

## What "Full Capabilities" Means

Both modes apply the same baseline security (`cap_drop=['ALL']`, AppArmor, noexec tmpfs) and differ only in which caps are added back. Constants live in `src/backend/services/agent_service/capabilities.py` and are re-exported from `lifecycle.py`.

### Full Capabilities Mode (`full_capabilities=true`)
- `cap_drop=['ALL']` (baseline — always)
- `cap_add=FULL_CAPABILITIES` (9 caps: restricted set + `DAC_OVERRIDE`, `FOWNER`, `KILL`)
- `security_opt=['apparmor:docker-default']`
- `tmpfs={'/tmp': 'noexec,nosuid,size=<AGENT_TMP_SIZE>'}` (default `512m`; size operator-configurable via the `AGENT_TMP_SIZE` env var, `noexec,nosuid` always fixed — see [Agent `/tmp` tmpfs size](#agent-tmp-tmpfs-size-1231--tmpdir-scratch-redirect-1098))
- **Allows**: `sudo apt-get install` and similar package-installation flows
- **Still prevents** (Issue #602 / Phase 3c, 2026-05-13): `SYS_PTRACE` (heap-read escalation), `MKNOD` (device-node escape), `NET_RAW` (raw-packet crafting), `FSETID` (setuid-preserve on chmod)

### Restricted Mode (`full_capabilities=false`, secure default)
- `cap_drop=['ALL']` (baseline — always)
- `cap_add=RESTRICTED_CAPABILITIES` (6 caps: `NET_BIND_SERVICE`, `SETGID`, `SETUID`, `CHOWN`, `SYS_CHROOT`, `AUDIT_WRITE`)
- `security_opt=['apparmor:docker-default']`
- `tmpfs={'/tmp': 'noexec,nosuid,size=<AGENT_TMP_SIZE>'}` (default `512m`; see [Agent `/tmp` tmpfs size](#agent-tmp-tmpfs-size-1231--tmpdir-scratch-redirect-1098))
- **Prevents**: Package installation, most privileged operations

## Backend Layer

### System-Wide Setting (Settings Service)

**File**: `src/backend/services/settings_service.py:128-138`

```python
def get_agent_full_capabilities() -> bool:
    """
    Get system-wide agent full capabilities setting.
    Default: True (agents have full control of their container environment)
    """
    value = settings_service.get_setting('agent_full_capabilities', 'true')
    return str(value).lower() in ('true', '1', 'yes')
```

### Per-Agent API Endpoints

**File**: `src/backend/routers/agent_config.py`

#### GET /api/agents/{name}/capabilities

```python
@router.get("/{agent_name}/capabilities")
async def get_agent_capabilities(
    agent_name: str,
    current_user: User = Depends(get_current_user)
):
    """
    Returns:
    - full_capabilities: Database setting for this agent
    - current_full_capabilities: Current container label value
    """
```

**Response Example**:
```json
{
  "full_capabilities": true,
  "current_full_capabilities": false
}
```

#### PUT /api/agents/{name}/capabilities

```python
@router.put("/{agent_name}/capabilities")
async def set_agent_capabilities(
    agent_name: str,
    body: dict,
    current_user: User = Depends(get_current_user)
):
    """
    Body: {"full_capabilities": true|false}
    Note: Requires agent restart for changes to take effect.
    """
```

**Response Example**:
```json
{
  "message": "Capabilities updated",
  "full_capabilities": true,
  "restart_needed": true
}
```

### Database Layer

**File**: `src/backend/db/agents.py:420-461`

| Method | Line | Description |
|--------|------|-------------|
| `get_full_capabilities(agent_name)` | 420-440 | Get per-agent setting from `agent_ownership.full_capabilities` |
| `set_full_capabilities(agent_name, enabled)` | 442-461 | Update per-agent setting |

**Schema** (Migration in `src/backend/database.py:248-255`):
```sql
ALTER TABLE agent_ownership ADD COLUMN full_capabilities INTEGER DEFAULT 0
```

### Container Creation

**File**: `src/backend/services/agent_service/crud.py` — uses `AGENT_TMPFS_MOUNT` / `AGENT_DEFAULT_TMPDIR` imported from `capabilities.py` (import at `crud.py:38`; `TMPDIR` env at `crud.py:521`; `tmpfs=AGENT_TMPFS_MOUNT` at `crud.py:782`).

When creating a new agent:
1. Reads **system-wide** setting via `get_agent_full_capabilities()`
2. Sets container label `trinity.full-capabilities` to reflect current value
3. Configures Docker security options based on setting
4. Mounts `/tmp` as the hardened tmpfs via the shared `AGENT_TMPFS_MOUNT` constant (same mount both modes — `noexec,nosuid` always applied; size from `AGENT_TMP_SIZE`)

```python
full_capabilities = get_agent_full_capabilities()

container = docker_client.containers.run(
    ...
    labels={
        'trinity.full-capabilities': str(full_capabilities).lower(),
        ...
    },
    environment={..., 'TMPDIR': AGENT_DEFAULT_TMPDIR},  # '/home/developer/.tmp' (#1098)
    security_opt=['apparmor:docker-default'] if not full_capabilities else [],
    cap_drop=[] if full_capabilities else ['ALL'],
    cap_add=[] if full_capabilities else ['NET_BIND_SERVICE', 'SETGID', 'SETUID', 'CHOWN', 'SYS_CHROOT', 'AUDIT_WRITE'],
    tmpfs=AGENT_TMPFS_MOUNT,  # {'/tmp': 'noexec,nosuid,size=<AGENT_TMP_SIZE>'}, default 512m
    ...
)
```

The `tmpfs` mount is the shared `AGENT_TMPFS_MOUNT` constant from `capabilities.py` in **both** modes — full vs restricted differs only in `cap_add`/`security_opt`, not in the /tmp hardening. The agent's `TMPDIR` is also set to `AGENT_DEFAULT_TMPDIR` (`/home/developer/.tmp`) in the env block (`crud.py:522`). See [/tmp tmpfs size](#tmp-tmpfs-size-agent_tmp_size-1231-1098).

### Container Recreation on Start

**File**: `src/backend/services/agent_service/lifecycle.py:150-162`

When starting an agent, the system checks if container needs recreation:

```python
needs_recreation = (
    not check_shared_folder_mounts_match(container, agent_name) or
    not check_api_key_env_matches(container, agent_name) or
    not check_resource_limits_match(container, agent_name) or
    not check_full_capabilities_match(container, agent_name)  # <-- Capabilities check
)

if needs_recreation:
    await recreate_container_with_updated_config(agent_name, container, "system")
```

### Capabilities Mismatch Check

**File**: `src/backend/services/agent_service/helpers.py:366-392`

```python
def check_full_capabilities_match(container, agent_name: str) -> bool:
    """
    Check if container's full_capabilities setting matches the current system-wide setting.
    Returns True if capabilities match, False if recreation needed.

    Note: This currently uses the SYSTEM-WIDE setting, not per-agent.
    """
    system_full_caps = get_agent_full_capabilities()

    labels = container.attrs.get("Config", {}).get("Labels", {})
    current_full_caps = labels.get("trinity.full-capabilities", "false").lower() == "true"

    if system_full_caps != current_full_caps:
        logger.info(f"Capabilities mismatch for {agent_name}: container={current_full_caps} -> system={system_full_caps}")
        return False

    return True
```

### Container Recreation with Capabilities

**File**: `src/backend/services/agent_service/lifecycle.py` — imports `AGENT_TMPFS_MOUNT` / `AGENT_DEFAULT_TMPDIR` from `capabilities.py` (`lifecycle.py:97-98`; `TMPDIR` setdefault at `lifecycle.py:361`; `tmpfs=AGENT_TMPFS_MOUNT` at `lifecycle.py:578`).

When recreating a container:
1. Gets current system-wide setting
2. Updates container label
3. Creates new container with appropriate security options (including the same shared `AGENT_TMPFS_MOUNT` mount the create path uses — so size/posture can't drift between create and recreate)

```python
# Get full_capabilities from system-wide setting (not per-agent)
full_capabilities = get_agent_full_capabilities()

# Update label to reflect current setting
labels["trinity.full-capabilities"] = str(full_capabilities).lower()

# Create new container with security settings
new_container = docker_client.containers.run(
    ...
    security_opt=['apparmor:docker-default'] if not full_capabilities else [],
    cap_drop=[] if full_capabilities else ['ALL'],
    cap_add=[] if full_capabilities else ['NET_BIND_SERVICE', 'SETGID', 'SETUID', 'CHOWN', 'SYS_CHROOT', 'AUDIT_WRITE'],
    tmpfs=AGENT_TMPFS_MOUNT,  # {'/tmp': 'noexec,nosuid,size=<AGENT_TMP_SIZE>'}, default 512m
    ...
)
```

The recreate path imports the same `AGENT_TMPFS_MOUNT` / `AGENT_DEFAULT_TMPDIR` constants (`lifecycle.py:98-99`) and sets `TMPDIR` via `env_vars.setdefault('TMPDIR', AGENT_DEFAULT_TMPDIR)` (`lifecycle.py:363`), so create and recreate can't drift. See [/tmp tmpfs size](#tmp-tmpfs-size-agent_tmp_size-1231-1098).

## /tmp tmpfs Size (`AGENT_TMP_SIZE`, #1231 / #1098)

Agent `/tmp` is a RAM-backed tmpfs, hardened `noexec,nosuid` so a compromised agent can't stage/execute payloads there. Only the **size** is configurable — the `noexec,nosuid` flags are a security boundary and stay hardcoded.

**Single source of truth**: `src/backend/services/agent_service/capabilities.py:72-116`. The mount spec is built **once** as the module constant `AGENT_TMPFS_MOUNT` (`capabilities.py:107-109`) and imported by both the create path (`crud.py:39,791`) and the recreate path (`lifecycle.py:98,587`), so the two can't drift.

| Aspect | Detail | Reference |
|--------|--------|-----------|
| Env var | `AGENT_TMP_SIZE` on the **backend** service (e.g. `512m`, `2g`) | `.env.example:329`, `docker-compose.yml:52`, `docker-compose.prod.yml:96` (`${AGENT_TMP_SIZE:-512m}`) |
| Default | `_AGENT_TMP_SIZE_DEFAULT = "512m"` | `capabilities.py:92` |
| Validation | `_AGENT_TMP_SIZE_RE = re.compile(r"^\d+[mg]$")` — `<int>m`/`<int>g` (case-insensitive); empty / bare number / Kubernetes-style suffix → falls back to default (never a broken or unbounded mount) | `capabilities.py:93,96-104` |
| Resolution | `_resolve_agent_tmp_size()` reads the env var, lower-cases, returns it if it matches the regex else the default | `capabilities.py:96-104` |
| Memory accounting | The size counts against the agent's memory cgroup — kept bounded by design | `capabilities.py:84-86` |
| Apply semantics | Mount specs are creation-time, so a changed `AGENT_TMP_SIZE` is picked up on **recreate**, not on a plain restart | `capabilities.py:87-88` |

### TMPDIR Scratch Redirect (#1098)

`/tmp`'s `noexec` + size cap breaks heavy scratch (pip/npm installs, compiling C extensions, ML wheels like torch/transformers — "No space left on device" at the cap, "Permission denied" on `noexec`). The default `TMPDIR` is therefore set to `AGENT_DEFAULT_TMPDIR = '/home/developer/.tmp'` (`capabilities.py:116`) — the disk-backed, exec-capable agent home volume — which dodges both the size cap and `noexec` while keeping /tmp's hardened posture. `TMPDIR` is injected at container create (`crud.py:522`) and recreate (`lifecycle.py:363`); the directory itself is created writable by UID 1000 at container start by `docker/base-image/startup.sh`, so existing agents pick it up on restart. Tools that hardcode `/tmp` (e.g. the `gh` CLI) ignore `TMPDIR` and still exhaust the cap — the larger default `512m` (#1231) is the mitigation for those.

## Frontend Layer

**Current State**: No dedicated UI component for capabilities toggle.

The per-agent API endpoints exist (`GET/PUT /api/agents/{name}/capabilities`) but are not exposed in the AgentDetail.vue UI. Management is currently done via:
1. System-wide `agent_full_capabilities` setting (no UI either)
2. Direct API calls

**Potential UI Location**: Could be added to the agent settings modal alongside resource limits (gear button in `AgentDetail.vue` header, line 225).

## Data Flow

### System-Wide Default Flow
```
Settings DB (agent_full_capabilities=true/false)
    ↓
get_agent_full_capabilities() in settings_service.py
    ↓
Container creation/recreation in crud.py / lifecycle.py
    ↓
Docker container with appropriate cap_drop/cap_add
```

### Per-Agent Override Flow (API-only)
```
PUT /api/agents/{name}/capabilities {"full_capabilities": true}
    ↓
db.set_full_capabilities(agent_name, enabled)
    ↓
agent_ownership.full_capabilities column updated
    ↓
On next start: check_full_capabilities_match() detects mismatch
    ↓
Container recreated with new settings
```

**Note**: Currently, the system-wide setting takes precedence. Per-agent DB values exist but `check_full_capabilities_match()` only compares against the system-wide setting.

## Error Handling

| Error Case | HTTP Status | Message |
|------------|-------------|---------|
| Agent not found | 404 | Agent not found |
| Not owner | 403 | Only owners can change capabilities |
| Missing field | 400 | full_capabilities is required |
| Invalid type | 400 | full_capabilities must be a boolean |

## Security Considerations

### Why Restricted Mode is More Secure
1. **Capability Dropping**: `cap_drop=['ALL']` removes all Linux capabilities
2. **Minimal Capability Add-back**: Only essential capabilities for agent operation:
   - `NET_BIND_SERVICE`: Bind to low ports (if needed)
   - `SETGID/SETUID`: Change group/user (for user switching)
   - `CHOWN`: Change file ownership
   - `SYS_CHROOT`: Change root directory
   - `AUDIT_WRITE`: Write audit logs
3. **AppArmor Profile**: Additional confinement via `apparmor:docker-default`
4. **Noexec tmpfs**: Prevents execution from /tmp (`noexec,nosuid` — always fixed; only the size is operator-configurable, see below)

### Agent `/tmp` tmpfs size (#1231) + TMPDIR scratch redirect (#1098)

**File**: `src/backend/services/agent_service/capabilities.py:72-116` (single source of truth — imported by both the create path `crud.py` and the recreate path `lifecycle.py`, plus `system_agent_service.py`, so create/recreate/system-agent can't drift).

- `/tmp` is a RAM-backed tmpfs mounted `noexec,nosuid` (hardened — a compromised agent can't stage/execute payloads there). The mount spec is `AGENT_TMPFS_MOUNT` = `{'/tmp': f'noexec,nosuid,size={...}'}` (`capabilities.py:107-109`).
- **Size is operator-configurable** via the `AGENT_TMP_SIZE` env var on the backend service. Default **`512m`** (`_AGENT_TMP_SIZE_DEFAULT = "512m"`, `capabilities.py:92`). Accepts `<int>m` / `<int>g` (case-insensitive) validated by `_AGENT_TMP_SIZE_RE = ^\d+[mg]$` (`capabilities.py:93`); anything else — empty, a bare number, a Kubernetes-style suffix — **falls back to the default** so a typo can't yield a broken or unbounded mount (`_resolve_agent_tmp_size()`, `capabilities.py:96-104`).
- `noexec,nosuid` are **always fixed** — only the size is tunable. The tmpfs **counts against the agent's memory cgroup**, so the value stays bounded.
- **Creation-time**: mount specs are applied at container create, so existing agents pick up an `AGENT_TMP_SIZE` change on **recreate, not restart**.
- **Wiring**: `AGENT_TMP_SIZE=${AGENT_TMP_SIZE:-512m}` is passed to the backend service in `docker-compose.yml:52` and `docker-compose.prod.yml:92`; documented in `.env.example:317-323`.
- **Heavy-scratch redirect (#1098)**: pip/npm installs, C-extension builds, and ML wheels (torch/transformers) must NOT land on the small noexec `/tmp` (hits "No space left on device" at the cap and "Permission denied" on noexec). A default `TMPDIR=/home/developer/.tmp` (`AGENT_DEFAULT_TMPDIR`, `capabilities.py:116`) redirects `$TMPDIR`-honoring tooling onto the disk-backed, exec-capable home volume. Set on the create path (`crud.py:521`) and `setdefault` on recreate (`lifecycle.py:361`); the directory is created (writable by UID 1000) at container start by `docker/base-image/startup.sh`, so existing agents pick it up on restart.

### When Full Capabilities is Needed
- Agent templates requiring `apt-get install`
- Agents that need to modify system configurations
- Development/testing environments
- Agents running privileged operations

### Authorization
- Only agent **owners** can change per-agent capabilities
- System-wide setting requires **admin** access to Settings

## Testing

### Prerequisites
- Trinity platform running locally
- At least one agent created
- Admin access for system-wide setting changes

### Test Steps

1. **Check Current Capabilities**
   - **Action**: `GET /api/agents/{agent_name}/capabilities`
   - **Expected**: Returns current and database capability values
   - **Verify**: Response contains `full_capabilities` and `current_full_capabilities`

2. **Set Per-Agent Capabilities**
   - **Action**: `PUT /api/agents/{agent_name}/capabilities` with `{"full_capabilities": true}`
   - **Expected**: Returns success with `restart_needed: true`
   - **Verify**: Database updated (check `agent_ownership` table)

3. **Verify Container Recreation on Start**
   - **Action**: Start agent after changing capabilities
   - **Expected**: Container is recreated with new security settings
   - **Verify**: Check container labels: `docker inspect agent-{name} | grep full-capabilities`

4. **Test Package Installation (Full Capabilities)**
   - **Action**: With `full_capabilities=true`, run `apt-get update && apt-get install -y cowsay` in agent terminal
   - **Expected**: Package installs successfully
   - **Verify**: `cowsay "Hello"` works

5. **Test Package Installation (Restricted)**
   - **Action**: With `full_capabilities=false`, attempt `apt-get install`
   - **Expected**: Operation fails with permission errors
   - **Verify**: Error message about insufficient permissions

### Edge Cases
- Changing setting while agent is running (should set `restart_needed: true`)
- Orphaned containers without DB record (uses container label as source of truth)
- System-wide vs per-agent setting conflict (currently system-wide takes precedence)

### Cleanup
- Reset capabilities to default: `PUT /api/agents/{name}/capabilities {"full_capabilities": false}`

### Status
- API endpoints: Working
- Container security: Working
- Frontend UI toggle: Not implemented

## Related Flows

- **Upstream**: [Agent Lifecycle](agent-lifecycle.md) - Container creation and recreation
- **Related**: [Agent Resource Allocation](agent-resource-allocation.md) - Similar per-agent config pattern
- **Related**: [SSH Access](ssh-access.md) - Requires specific capabilities for privilege separation

## Architecture Notes

### Current Implementation Gap
The per-agent API (`/api/agents/{name}/capabilities`) stores values in `agent_ownership.full_capabilities`, but `check_full_capabilities_match()` only uses the **system-wide** setting. This means:
- Per-agent settings are stored but not used for container recreation decisions
- All agents follow the system-wide `agent_full_capabilities` setting

### Potential Enhancement
To enable true per-agent capability control:
1. Modify `check_full_capabilities_match()` to read from `db.get_full_capabilities(agent_name)` instead of `get_agent_full_capabilities()`
2. Fall back to system-wide setting if per-agent value is not set

## Revision History

| Date | Change |
|------|--------|
| 2026-06-21 | Agent `/tmp` tmpfs size configurable via `AGENT_TMP_SIZE` (#1231, default `512m`, `noexec,nosuid` fixed, counts against memory cgroup, creation-time); heavy scratch redirected to `TMPDIR=/home/developer/.tmp` on the home volume (#1098). Stale hardcoded `size=100m` references corrected; mount spec + TMPDIR now sourced from `AGENT_TMPFS_MOUNT` / `AGENT_DEFAULT_TMPDIR` in `capabilities.py`. |
| 2026-01-14 | **Security Consistency (HIGH)**: Added `RESTRICTED_CAPABILITIES` and `FULL_CAPABILITIES` constants in `lifecycle.py:31-49`. All container creation paths now ALWAYS apply baseline security (`cap_drop=['ALL']`, AppArmor, noexec tmpfs) before adding back needed capabilities. Previously some paths had inconsistent security settings. See [agent-lifecycle.md](agent-lifecycle.md) for full security constant documentation. |
| 2026-05-13 | **Cap tightening (Issue #602 Phase 3c, PR #830)**: Dropped `SYS_PTRACE` / `MKNOD` / `NET_RAW` / `FSETID` from `FULL_CAPABILITIES` — each was a documented escalation primitive with no defensible agent use case (SYS_PTRACE closes the AISEC-C2 heap-read OAuth-exfil path). FULL set is now 9 caps (was 13). Constants extracted into `services/agent_service/capabilities.py` so `tests/unit/test_capability_set.py` can pin them stdlib-only; `lifecycle.py` re-exports for runtime callers. Existing containers keep old caps until restart. |
| 2026-01-13 | Initial documentation - CFG-004 feature flow |
