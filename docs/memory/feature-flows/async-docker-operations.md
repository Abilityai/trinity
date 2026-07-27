# Feature: Async Docker Operations (DOCKER-001)

> **Issue**: [#42](https://github.com/abilityai/trinity/issues/42) - Blocking Docker calls in async lifecycle functions freeze entire backend
> **Priority**: P1 (Critical Path)
> **Status**: Implemented
> **Created**: 2026-02-24
> **Updated**: 2026-02-24 - Added exec wrappers, expanded to SSH/Git/Terminal services

## Overview

Async wrappers for blocking Docker SDK operations using ThreadPoolExecutor. Prevents FastAPI event loop from freezing during Docker operations (container start/stop, volume management, container creation).

## Problem Statement

Docker SDK operations (`container.stop()`, `container.start()`, `docker_client.containers.run()`, etc.) are synchronous blocking calls that take 1-25+ seconds. When called directly in `async` FastAPI endpoints, they block the entire event loop, causing:

- HTTP 499 client timeout errors
- WebSocket connection drops
- UI unresponsiveness for 10-30+ seconds
- Docker health checks marking backend as "unhealthy"

## Solution

A centralized utility module that wraps all blocking Docker SDK calls in `asyncio.run_in_executor()` with a shared `ThreadPoolExecutor`.

---

## Entry Points

All callers use the same import pattern:

```python
from services.docker_utils import (
    container_stop, container_remove, container_start, container_reload,
    container_stats, container_get,
    volume_get, volume_create, volume_remove,
    containers_run
)
```

---

## Architecture

### Core Module: `src/backend/services/docker_utils.py`

**Lines**: 287
**Pattern**: ThreadPoolExecutor with `asyncio.run_in_executor()`

```python
# Shared executor - limited to 4 workers to avoid overwhelming Docker daemon
_docker_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="docker-")

async def container_stop(container, timeout: int = 10) -> None:
    """Stop a container without blocking the event loop."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(_docker_executor, lambda: container.stop(timeout=timeout))
```

### Wrapper Functions

| Function | Operation | Duration (blocking) | Duration (async) |
|----------|-----------|---------------------|------------------|
| `container_stop(container, timeout=10)` | Stop container | 1-5s | 0s event loop block |
| `container_remove(container, force=False)` | Remove container | 0.5-1s | 0s event loop block |
| `container_start(container)` | Start container | 2-5s | 0s event loop block |
| `container_reload(container)` | Refresh container attrs | 100-500ms | 0s event loop block |
| `container_stats(container, stream=False)` | Get container stats | 1-2s | 0s event loop block |
| `container_get(container_id)` | Get container by ID/name | 50-200ms | 0s event loop block |
| `image_get(name)` | Get image by tag/ID (#1809 drift check) | 50-200ms | 0s event loop block |
| `volume_get(name)` | Get volume by name | 100-300ms | 0s event loop block |
| `volume_create(name, labels=None)` | Create volume | 200-500ms | 0s event loop block |
| `volume_remove(volume)` | Remove volume | 100-300ms | 0s event loop block |
| `containers_run(image, command=None, **kwargs)` | Create and start container | 3-8s | 0s event loop block |
| `container_exec_run(container, cmd, user, workdir, environment)` | Execute command in container | 100ms-30s | 0s event loop block |
| `api_exec_create(container_id, cmd, stdin, tty, ...)` | Create exec instance | 50-200ms | 0s event loop block |
| `api_exec_start(exec_id, socket, tty)` | Start exec instance | 50-200ms | 0s event loop block |

---

## Integration Points

### Agent Lifecycle (`src/backend/services/agent_service/lifecycle.py`)

**Import (line 17-20)**:
```python
from services.docker_utils import (
    container_stop, container_remove, container_start, container_reload,
    volume_get, volume_create, containers_run
)
```

**Usage in `start_agent_internal()` (lines 214-233)**:
```python
# Check for container recreation
await container_reload(container)
needs_recreation = (
    not check_shared_folder_mounts_match(container, agent_name) or
    not check_api_key_env_matches(container, agent_name) or ...
)

if needs_recreation:
    await recreate_container_with_updated_config(agent_name, container, "system")
    container = get_agent_container(agent_name)

await container_start(container)
```

**Usage in `recreate_container_with_updated_config()` (lines 281-426)**:
```python
# Stop and remove old container
try:
    await container_stop(old_container)
except Exception:
    pass
await container_remove(old_container)

# Create shared volume if needed
try:
    await volume_get(shared_volume_name)
except docker.errors.NotFound:
    await volume_create(
        name=shared_volume_name,
        labels={'trinity.platform': 'agent-shared', 'trinity.agent-name': agent_name}
    )

# Fix volume ownership with temp container
await containers_run(
    'alpine',
    command='chown 1000:1000 /shared',
    volumes={shared_volume_name: {'bind': '/shared', 'mode': 'rw'}},
    remove=True
)

# Create new container
new_container = await containers_run(
    image,
    detach=True,
    name=f"agent-{agent_name}",
    ...
)
```

### Agent Router (`src/backend/routers/agents.py`)

**Import (lines 23-26)**:
```python
from services.docker_utils import (
    container_stop, container_remove, container_reload,
    volume_get, volume_remove
)
```

**Delete endpoint (lines 291-376)**:
```python
await container_stop(container)
await container_remove(container)

# Delete workspace volume
volume = await volume_get(agent_volume_name)
await volume_remove(volume)

# Delete shared volume
shared_volume = await volume_get(shared_volume_name)
await volume_remove(shared_volume)
```

**Stop endpoint (lines 415-440)**:
```python
await container_stop(container)
```

**Info endpoint (line 522)** and **SSH access endpoint (line 1166)**:
```python
await container_reload(container)
```

### Fleet Operations (`src/backend/routers/ops.py`)

**Import (line 24)**:
```python
from services.docker_utils import container_stop, container_start
```

**Fleet restart (lines 227-329)**:
```python
await container_stop(container, timeout=30)
await container_start(container)
```

**Fleet stop (lines 332-422)**:
```python
await container_stop(container, timeout=30)
```

### System Agent Service (`src/backend/services/system_agent_service.py`)

**Import (line 23)**:
```python
from services.docker_utils import container_reload, container_start, containers_run
```

**ensure_deployed() (lines 43-49, 77-78, 91-92)**:
```python
await container_reload(container)
if container.status == "running":
    return result

await container_start(container)
```

**_create_system_agent() (lines 240-260)**:
```python
container = await containers_run(
    'trinity-agent-base:latest',
    name=f"agent-{SYSTEM_AGENT_NAME}",
    detach=True,
    ...
)
```

### SSH Service (`src/backend/services/ssh_service.py`)

**Import (line 26)**:
```python
from services.docker_utils import container_exec_run
```

**Async credential injection methods**:
- `inject_ssh_key()` (line 85) - Creates `.ssh` directory and appends public key
- `set_container_password()` (line 150) - Sets user password via `usermod` or `chpasswd`
- `clear_container_password()` (line 213) - Locks account password via `passwd -l`
- `remove_ssh_key()` (line 243) - Removes key from `authorized_keys` via `sed`
- `cleanup_expired_credentials()` (line 368) - Periodic cleanup of expired credentials
- `cleanup_agent_credentials()` (line 441) - Cleanup all credentials on agent stop/delete
- `revoke_key()` (line 420) - Immediate key revocation

All methods use `await container_exec_run()` for container operations.

### Terminal Session (`src/backend/services/agent_service/terminal.py`)

**Import (line 16)**:
```python
from services.docker_utils import container_reload, api_exec_create, api_exec_start
```

**WebSocket terminal handling (lines 173, 203, 217)**:
```python
await container_reload(container)
# ...
exec_instance = await api_exec_create(
    container.id, cmd, stdin=True, tty=True, ...
)
exec_output = await api_exec_start(exec_id, socket=True, tty=True)
```

### System Agent Router (`src/backend/routers/system_agent.py`)

**Import (lines 24-27)**:
```python
from services.docker_utils import (
    container_reload, container_stop, container_start,
    container_exec_run, api_exec_create, api_exec_start
)
```

**reinitialize endpoint (lines 144, 161)**:
```python
cleanup_result = await container_exec_run(
    container,
    "bash -c 'rm -rf /home/developer/.claude ...'",
    user="developer"
)
```

**terminal WebSocket (lines 419, 433)**:
```python
exec_instance = await api_exec_create(container.id, cmd, ...)
exec_output = await api_exec_start(exec_id, socket=True, tty=True)
```

### Agent Service Helpers (`src/backend/services/agent_service/helpers.py`)

**Import (line 20)**:
```python
from services.docker_utils import volume_get
```

**check_shared_folder_mounts_match() (line 270)**:
```python
async def check_shared_folder_mounts_match(container, agent_name: str) -> bool:
    # ...
    await volume_get(source_volume)
```

### Docker Service (`src/backend/services/docker_service.py`)

**execute_command_in_container() (lines 208-243)**:
```python
async def execute_command_in_container(container_name: str, command: str, timeout: int = 60) -> dict:
    from services.docker_utils import container_exec_run, container_get

    container = await container_get(container_name)
    result = await container_exec_run(container, command, user="developer")
```

### Git Service (`src/backend/services/git_service.py`)

Uses `execute_command_in_container()` which is now async:
- `initialize_git_in_container()` (lines 295-402) - 6 await calls
- `check_git_initialized()` (lines 406-435) - 1 await call

### Git Router (`src/backend/routers/git.py`)

**initialize_github_sync() (line 288)**:
```python
git_dir = await git_service.check_git_initialized(agent_name)
```

### SSH Router (`src/backend/routers/agent_ssh.py`)

**SSH access endpoint** — key injection is the only awaited Docker call on this
path since #1615 removed password auth (`set_container_password`, the other
caller here, is deleted):
```python
if not await ssh_service.inject_ssh_key(agent_name, public_key_with_comment):
```

---

## Reference Implementation

The correct async pattern for Docker operations was established in `src/backend/routers/telemetry.py` (lines 18-147):

```python
# Module-level executor for Docker operations (blocking calls)
# Limited to 4 workers to avoid overwhelming Docker daemon
_docker_executor = ThreadPoolExecutor(max_workers=4)

def _get_single_container_stats_sync(agent_name: str) -> Dict[str, Any]:
    """Synchronous helper runs in thread pool."""
    container = docker_client.containers.get(f"agent-{agent_name}")
    stats = container.stats(stream=False)  # Blocking call
    return process_stats(stats)

@router.get("/containers")
async def get_container_stats():
    # Fetch all container stats in PARALLEL using thread pool
    loop = asyncio.get_event_loop()
    tasks = [
        loop.run_in_executor(_docker_executor, _get_single_container_stats_sync, agent.name)
        for agent in running_agents
    ]
    containers_stats = await asyncio.gather(*tasks, return_exceptions=True)
```

---

## Performance Impact

| Scenario | Before (blocking) | After (async) |
|----------|-------------------|---------------|
| Container recreation | 10-25s event loop block | 0s block |
| Agent start | 2-5s event loop block | 0s block |
| Agent stop | 1-5s event loop block | 0s block |
| Agent delete | 3-8s event loop block | 0s block |
| Concurrent HTTP requests | Queued/timeout | Processed normally |
| WebSocket connections | Dropped during Docker ops | Stable |

---

## Thread Pool Configuration

**Workers**: 4 (matches telemetry.py reference)
**Thread prefix**: `docker-` (for debugging)

**Rationale**:
- 4 workers balances parallelism with Docker daemon capacity
- Higher worker counts can overwhelm the Docker socket
- Thread prefix aids in debugging/profiling

---

## Error Handling

All wrapper functions preserve Docker SDK exceptions:

```python
async def container_get(container_id: str) -> Any:
    """
    Raises:
        docker.errors.NotFound: If container doesn't exist
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        _docker_executor,
        docker_client.containers.get,
        container_id
    )
```

Callers handle exceptions normally:

```python
try:
    await container_stop(old_container)
except Exception:
    pass  # Container might already be stopped
```

---

## Files Changed

| File | Changes |
|------|---------|
| `src/backend/services/docker_utils.py` | **NEW** - Core utility module (287 lines, 13 wrapper functions) |
| `src/backend/services/agent_service/lifecycle.py` | Added import, wrapped all Docker calls |
| `src/backend/services/agent_service/helpers.py` | Made `check_shared_folder_mounts_match()` async, uses `volume_get` |
| `src/backend/services/agent_service/terminal.py` | Uses `api_exec_create`, `api_exec_start` for PTY creation |
| `src/backend/services/ssh_service.py` | All 7 credential methods now async, use `container_exec_run` |
| `src/backend/services/docker_service.py` | Made `execute_command_in_container()` async |
| `src/backend/services/git_service.py` | 7 calls now await `execute_command_in_container()` |
| `src/backend/services/system_agent_service.py` | Added import, wrapped all Docker calls |
| `src/backend/routers/agents.py` | Wrapped delete/stop/reload calls, SSH access awaits ssh_service |
| `src/backend/routers/ops.py` | Added import, wrapped fleet restart/stop calls |
| `src/backend/routers/system_agent.py` | Uses `container_exec_run`, `api_exec_create`, `api_exec_start` |
| `src/backend/routers/git.py` | Awaits `check_git_initialized()` |

---

## What NOT to Wrap

Per `docker_utils.py:281-287` comment:

```python
# Note: list_all_agents() and list_all_agents_fast() are already efficient
# and don't require async wrappers as they complete quickly (<50ms).
# Only wrap if profiling shows they become a bottleneck.
```

Container listing operations in `docker_service.py` are fast (<50ms) and do not require wrapping.

**Exec resize** (`docker_client.api.exec_resize()`) is intentionally NOT wrapped. It's a quick metadata call (<10ms) and is called from within the bidirectional forwarding loop where adding async overhead would complicate the flow.

---

## Testing Checklist

- [ ] Create agent - completes without blocking
- [ ] Start agent - UI remains responsive
- [ ] Stop agent - UI remains responsive
- [ ] Delete agent - UI remains responsive
- [ ] Container recreation (API key toggle) - no 499 errors
- [ ] Shared folder config change - no blocking
- [ ] Bulk operations (fleet restart) - parallel execution
- [ ] WebSocket stability during Docker ops

---

## Related Documentation

- **Requirements**: `docs/requirements/ASYNC_DOCKER_OPERATIONS.md` - Full implementation spec
- **Agent Lifecycle**: `docs/memory/feature-flows/agent-lifecycle.md` - Uses these utilities
- **Host Telemetry**: `docs/memory/feature-flows/host-telemetry.md` - Reference implementation pattern

---

## Revision History

| Date | Changes |
|------|---------|
| 2026-02-24 | Initial implementation per DOCKER-001 requirements |
| 2026-02-24 | Added exec wrappers (`container_exec_run`, `api_exec_create`, `api_exec_start`), expanded to SSH/Git/Terminal services (12 files total) |
