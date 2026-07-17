# Feature: SSH Access

## Overview
Generate ephemeral SSH credentials for direct terminal access to agent containers. **Key-based (ED25519) auth only** — the client supplies its own public key and the private key never leaves the client. Configurable TTL (default 4 hours, max 24 hours). Controlled by system-wide `ssh_access_enabled` ops setting. **Admin-only access.**

## User Story
As a platform admin, I want to generate temporary SSH credentials for agent containers so that I can access them directly via SSH from my local terminal for debugging or maintenance (especially useful with Tailscale/VPN setups).

## Revision History
| Date | Change |
|------|--------|
| 2026-07-17 | **Enforce key TTL on the filesystem + injection-safe inject/remove (#1616)**. The expired-key security gap: an ephemeral key's TTL was enforced ONLY on its Redis metadata — `SshService.cleanup_expired_credentials()` (which removes the line from the `authorized_keys` file sshd reads) had **zero callers** (`SSH_ACCESS_CLEANUP_INTERVAL` was unused), so on a preserved (never-recreated) volume an **expired** key lingered in the file and still granted login. Wired it into the 5-min `cleanup_service` loop (`_sweep_expired_ssh_credentials`, report field `ssh_credentials_expired`; fail-open). A **live probe caught** that the sweep's keyspace scan used `redis_client.keys()`, which the backend's `-@dangerous` Redis ACL user **blocks** (`NoPermissionError`) — the fail-open handler would have swallowed it to 0-cleaned every cycle, leaving the fix inert; switched all three `ssh_service` scans (`cleanup_expired_credentials`, `list_active_keys`, `cleanup_agent_credentials`) to `scan_iter`. `inject_ssh_key` now passes the public key via the exec **environment** (never string-interpolated) in one atomic `sh -c` and gained `skip_if_present=True` (append-if-absent via `grep -qxF`), killing both the repeat-inject double-add and the docker-py `shlex.split`→`sh -c` double-unquoting injection class. `remove_ssh_key` (now load-bearing for cleanup) matches the comment as an exact awk `$NF` last-field via `ENVIRON` (no regex/shell metachar escaping, no substring over-delete). **Scope note:** #1616's reported *recreate-wipe* symptom is a SEPARATE, unidentified mechanism — a normal never-renamed agent is volume-safe on recreate (`/home/developer` is a named volume, forwarded by the config-recreate path), the #1664/#1665/#1667 volume-identity family (fixed by #1666) postdates the reporter's build, and no start-time re-injection hook was added (it is near-inert and would mask data loss). |
| 2026-07-16 | **Removed password auth (#1615)**: the option was broken end-to-end — host-side hashing imported the stdlib `crypt` module (removed in Python 3.13, so every request 500'd), and the agent sshd runs `PasswordAuthentication no`, so a password login could never succeed even with a hash set. `auth_method` now accepts only `"key"`; `"password"` returns 400. Removed `generate_password()` and `set_container_password()`. `clear_container_password()` is **retained** as a cleanup-only path (it locks a password left by a pre-#1615 backend on Python <3.13); nothing sets passwords any more. |
| 2026-04-18 | **SEC: Admin-only access**: Changed from owner/admin to admin-only. Uses `require_admin` dependency instead of `can_user_delete_agent` check. |
| 2026-03-26 | **SEC: Removed server-side keypair generation (#175)**: Key auth now requires client-supplied `public_key`. Private keys never leave the client. Removed `generate_ssh_keypair()` and `cryptography` dependency. |
| 2026-02-24 | **Async Docker Operations**: All SshService methods now async (DOCKER-001). Uses `container_exec_run` wrapper to prevent event loop blocking. |
| 2026-02-13 | **Fixed localhost bug**: Added FRONTEND_URL domain extraction as priority #2 in host detection. Production deployments now correctly return domain instead of localhost. |
| 2026-01-23 | Updated line numbers: ssh_service.py, agents.py, agents.ts, client.ts, types.ts |
| 2026-01-02 | Fixed password setting (sed → usermod), improved host detection |
| 2026-01-02 | Added UI toggle in Settings.vue for ssh_access_enabled |
| 2026-01-02 | Initial documentation |

---

## Entry Points

- **MCP Tool**: `get_agent_ssh_access` - Primary entry point for external clients (Claude Code, etc.)
- **API**: `POST /api/agents/{agent_name}/ssh-access` - Backend REST endpoint

---

## MCP Layer

### Tool Definition

**agents.ts** (`src/mcp-server/src/tools/agents.ts`, `getAgentSshAccess`)

```typescript
getAgentSshAccess: {
  name: "get_agent_ssh_access",
  description:
    "Generate ephemeral, key-based SSH credentials for direct terminal access to an agent container. " +
    "Generate a keypair locally (ssh-keygen -t ed25519) and provide the PUBLIC key; the server injects " +
    "it into the container and it expires automatically (default: 4 hours). Agent must be running. " +
    "The server never generates or handles private keys. Admin only. " +
    "(Password auth was removed — it never worked; key auth is the only method.)",
  parameters: z.object({
    agent_name: z.string().describe("Name of the agent to access"),
    ttl_hours: z
      .number()
      .optional()
      .default(4)
      .describe("How long the SSH key should be valid (0.1-24 hours, default: 4)"),
    public_key: z
      .string()
      .describe("Your SSH public key (required). Generate with: ssh-keygen -t ed25519. Provide the contents of ~/.ssh/id_ed25519.pub"),
  }),
  execute: async ({ agent_name, ttl_hours = 4, public_key }, context?) => {
    const apiClient = getClient(context?.session);
    const response = await apiClient.createSshAccess(agent_name, ttl_hours, public_key);
    return JSON.stringify(response, null, 2);
  },
}
```

`auth_method` is gone from the tool surface (#1615) — there is nothing to choose.

### Client Method

**client.ts** (`src/mcp-server/src/client.ts`, `createSshAccess`)

```typescript
async createSshAccess(
  name: string,
  ttlHours: number = 4,
  publicKey?: string
): Promise<SshAccessResponse> {
  // #1615: key-based auth only (password auth removed).
  const body: Record<string, unknown> = { ttl_hours: ttlHours, auth_method: "key" };
  if (publicKey) body.public_key = publicKey;
  return this.request<SshAccessResponse>(
    "POST",
    `/api/agents/${encodeURIComponent(name)}/ssh-access`,
    body
  );
}
```

The client still sends `auth_method: "key"` explicitly so an older backend (which
defaults the field) keeps working.

### Type Definitions

**types.ts** (`src/mcp-server/src/types.ts:116-135`)

```typescript
export interface SshConnectionInfo {
  command: string;      // Full SSH command to connect
  host: string;         // SSH host (tailscale IP, SSH_HOST env, or localhost)
  port: number;         // SSH port (from container label trinity.ssh-port)
  user: string;         // Always "developer"
}

export interface SshAccessResponse {
  status: string;           // "success"
  agent: string;            // Agent name
  auth_method: "key";       // #1615: password auth removed
  connection: SshConnectionInfo;
  expires_at: string;       // ISO timestamp
  expires_in_hours: number; // TTL value used
  instructions: string[];   // Step-by-step usage instructions
  // NOTE: private_key field removed in #175 — server never generates or returns private keys
}
```

---

## Backend Layer

### Endpoint

**agent_ssh.py** (`src/backend/routers/agent_ssh.py`)

#### Request Model

```python
class SshAccessRequest(BaseModel):
    """Request body for SSH access (key-based only; #1615 removed password auth)."""
    ttl_hours: float = 4.0
    auth_method: str = "key"  # only "key" is supported (password auth removed, #1615)
    public_key: Optional[str] = None  # Required — client-supplied OpenSSH public key
```

#### POST /{agent_name}/ssh-access

```python
@router.post("/{agent_name}/ssh-access")
async def create_ssh_access(
    agent_name: str,
    body: SshAccessRequest = SshAccessRequest(),
    current_user: User = Depends(require_admin)  # Admin-only access
):
    # 1. Check if SSH access is enabled system-wide
    if not get_ops_setting("ssh_access_enabled", as_type=bool):
        raise HTTPException(status_code=403, detail="SSH access is disabled. ...")

    # 2. Verify agent exists and is running
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")
    if container.status != "running":
        raise HTTPException(status_code=400, detail="Agent must be running")

    # 3. Validate auth method — key-based only (#1615: password auth removed).
    #    An explicit "password" gets a 400 that says what to do instead, rather
    #    than a 500 (the old crypt ImportError) or a silent no-op.
    if body.auth_method != "key":
        raise HTTPException(status_code=400, detail="Password SSH auth is no longer supported. ...")

    # 4. public_key is required — no server-side keypair generation (#175)
    if not body.public_key:
        raise HTTPException(status_code=400, detail="public_key is required ...")

    # 5. Validate and clamp TTL (0.1 - 24 hours)
    ttl_hours = max(0.1, min(body.ttl_hours, SSH_ACCESS_MAX_TTL_HOURS))

    # 6. Get SSH port and host
    labels = container.attrs.get("Config", {}).get("Labels", {})
    ssh_port = int(labels.get("trinity.ssh-port", "2222"))
    host = get_ssh_host()  # Tailscale IP, SSH_HOST env, or localhost

    # 7. Inject the client-supplied key and record metadata
    await ssh_service.inject_ssh_key(agent_name, public_key_with_comment)
    await ssh_service.store_key_metadata(...)
    return { key response — no private_key field }
```

There is one flow now. The old `if auth_method == "password"` branch is gone
along with the service functions it called.

### SSH Service

**ssh_service.py** (`src/backend/services/ssh_service.py`)

#### Configuration (Lines 30-35)

```python
SSH_ACCESS_DEFAULT_TTL_HOURS = int(os.getenv("SSH_ACCESS_DEFAULT_TTL_HOURS", "4"))
SSH_ACCESS_MAX_TTL_HOURS = int(os.getenv("SSH_ACCESS_MAX_TTL_HOURS", "24"))
SSH_ACCESS_CLEANUP_INTERVAL = int(os.getenv("SSH_ACCESS_CLEANUP_INTERVAL", "900"))  # 15 min
SSH_ACCESS_PREFIX = "ssh_access:"  # Redis key prefix
```

#### Key Injection - SshService.inject_ssh_key()

> **Note (SEC #175)**: `generate_ssh_keypair()` has been removed. The server no longer generates keypairs.
> Clients generate their own keypairs locally and supply only the public key.


```python
async def inject_ssh_key(self, agent_name: str, public_key: str) -> bool:
    """Inject SSH public key into agent's authorized_keys file."""
    container = get_agent_container(agent_name)

    # Ensure .ssh directory exists with correct permissions (async to avoid blocking)
    await container_exec_run(
        container,
        'sh -c "mkdir -p /home/developer/.ssh && chmod 700 /home/developer/.ssh"',
        user="developer"
    )

    # Append public key to authorized_keys
    escaped_key = public_key.replace("'", "'\"'\"'")
    await container_exec_run(
        container,
        f"sh -c 'printf \"%s\\n\" '\"'{escaped_key}'\"' >> /home/developer/.ssh/authorized_keys'",
        user="developer"
    )

    # Set correct permissions
    await container_exec_run(container, 'chmod 600 /home/developer/.ssh/authorized_keys', user="developer")
    return True
```

> **Note**: As of DOCKER-001, all SshService methods use `container_exec_run` from `services/docker_utils.py` to avoid blocking the FastAPI event loop.

#### Redis Metadata Storage (Lines 273-318) - SshService.store_credential_metadata()

```python
def store_credential_metadata(
    self,
    agent_name: str,
    credential_id: str,
    auth_type: Literal["key", "password"],
    created_by: str,
    ttl_hours: float,
    public_key: Optional[str] = None
) -> None:
    """Store SSH credential metadata in Redis with TTL."""
    now = datetime.utcnow()
    expires_at = now + timedelta(hours=ttl_hours)

    redis_key = f"{SSH_ACCESS_PREFIX}{agent_name}:{credential_id}"

    metadata = {
        "agent_name": agent_name,
        "credential_id": credential_id,
        "auth_type": auth_type,
        "created_at": now.isoformat() + "Z",
        "expires_at": expires_at.isoformat() + "Z",
        "created_by": created_by
    }
    if public_key:
        metadata["public_key"] = public_key

    # Store with TTL - Redis auto-expires
    ttl_seconds = int(ttl_hours * 3600)
    self.redis_client.setex(
        redis_key,
        ttl_seconds,
        json.dumps(metadata)
    )
    logger.info(f"Stored SSH {auth_type} metadata: {redis_key} (TTL: {ttl_hours}h)")
```

#### Host Detection (Lines 479-567) - get_ssh_host()

```python
def get_ssh_host() -> str:
    """
    Get the host IP/domain for SSH connections.

    Priority:
    1. SSH_HOST environment variable (explicit configuration)
    2. FRONTEND_URL domain extraction (production deployment)
    3. Tailscale IP detection
    4. host.docker.internal (Docker Desktop for Mac/Windows)
    5. Default gateway IP (often the Docker host on Linux)
    6. Fallback to localhost

    Note: This runs inside the backend container, so we need special
    handling to get the actual Docker host IP, not the container's IP.
    """
    # Option 1: Explicit environment variable (most reliable)
    ssh_host = os.getenv("SSH_HOST")
    if ssh_host:
        return ssh_host

    # Option 2: Extract host from FRONTEND_URL (production deployment)
    # FRONTEND_URL is set to production domain like "https://trinity.abilityai.dev"
    frontend_url = os.getenv("FRONTEND_URL", "")
    if frontend_url:
        parsed = urlparse(frontend_url)
        host = parsed.hostname or parsed.netloc
        if host and host not in ("localhost", "127.0.0.1", ""):
            return host

    # Option 3: Try to detect Tailscale IP
    try:
        result = subprocess.run(["tailscale", "ip", "-4"], ...)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Option 4: Try host.docker.internal (Docker Desktop Mac/Windows)
    try:
        ip = socket.gethostbyname("host.docker.internal")
        if ip and not ip.startswith("172.") and not ip.startswith("127."):
            return ip
    except socket.gaierror:
        pass

    # Option 5: Get default gateway IP (Linux Docker host)
    try:
        result = subprocess.run(["ip", "route", "show", "default"], ...)
        # Parse "default via 192.168.1.1 dev eth0" → 192.168.1.1
        if gateway_ip and not gateway_ip.startswith("172."):
            return gateway_ip
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Option 6: Fallback to localhost with warning
    return "localhost"
```

---

## Credential Cleanup

> **Why the cleanup paths still mention passwords (#1615).** Nothing writes
> `auth_type="password"` any more, but a backend on Python <3.13 could set a
> password successfully, so a pre-upgrade Redis row may still be in flight when
> the removal ships. Those rows carry a TTL of at most `SSH_ACCESS_MAX_TTL_HOURS`
> (24h), so the `auth_type == "password"` branches below — and
> `clear_container_password()` — are dead within a day of deploy. They exist so
> the last legacy credentials get locked rather than left set, and are the only
> surviving password code.

### Automatic Expiry (Redis TTL)

Redis handles metadata expiry automatically via `setex()`. When TTL expires, the key is deleted.

**But the Redis TTL alone is not enough.** The key's line in the container's
`authorized_keys` is what `sshd` actually reads — and Redis expiring the metadata
does nothing to that file. The file-side removal is `cleanup_expired_credentials()`
below, wired into the 5-min `cleanup_service` loop (#1616).

### Proactive Container Cleanup (#1616) - SshService.cleanup_expired_credentials()

**Wiring (#1616).** This method existed since DOCKER-001 but had **zero callers**
(`SSH_ACCESS_CLEANUP_INTERVAL` was defined and never read), so an expired key's
line lingered in `authorized_keys` and still granted login on a preserved
(never-recreated) volume — the enforceable half of #1616's "silent divergence."
It is now registered as a self-contained sweep in `cleanup_service._run_cleanup_inner`
(`_sweep_expired_ssh_credentials`, report field `ssh_credentials_expired`), running
every 5 minutes. Fail-open: the sweep owns its try/except, the per-key loop below
skips a Redis/exec error, and `remove_ssh_key` tolerates a missing container/file
— so a stopped or deleted agent is a no-op. The short Redis TTL is still the
primary guarantee; this makes it real on the filesystem.

**SCAN, not KEYS (#1616).** The keyspace iteration uses `redis_client.scan_iter`,
not `keys()`. The backend Redis ACL user is `-@dangerous` (see Network Topology),
which blocks `KEYS` — a live probe of the wired sweep raised
`NoPermissionError: User backend has no permissions to run the 'keys' command`,
which the fail-open handler would have swallowed to 0-cleaned every cycle, leaving
the fix **inert**. `SCAN` is allowed and is the production-safe incremental scan
anyway. The same fix applies to `list_active_keys()` and
`cleanup_agent_credentials()` (agent stop/delete), which were silently broken by
the identical ACL cause. A mocked unit test can't see this (it stubs the Redis
client), so `test_1616_ssh.py` carries a static guard that `redis_client.keys(`
never reappears in `ssh_service.py`.

```python
async def cleanup_expired_credentials(self) -> int:
    """
    Clean up expired SSH credentials from containers.
    Called every 5 min by cleanup_service (#1616).
    Redis TTL handles metadata cleanup automatically,
    but we need to remove credentials from containers.
    """
    cleaned = 0
    pattern = f"{SSH_ACCESS_PREFIX}*"

    # SCAN, not KEYS — the backend Redis ACL user is -@dangerous (#1616).
    # Find credentials about to expire (within 60 seconds)
    for redis_key in self.redis_client.scan_iter(match=pattern):
        ttl = self.redis_client.ttl(redis_key)

        # If TTL is very low or negative, credential is about to expire
        if ttl is not None and 0 <= ttl <= 60:
            try:
                data = self.redis_client.get(redis_key)
                if data:
                    metadata = json.loads(data)
                    agent_name = metadata.get("agent_name")
                    auth_type = metadata.get("auth_type", "key")
                    credential_id = metadata.get("credential_id") or metadata.get("comment")

                    if agent_name and credential_id:
                        if auth_type == "password":
                            await self.clear_container_password(agent_name)
                        else:
                            await self.remove_ssh_key(agent_name, credential_id)
                        cleaned += 1
            except Exception as e:
                logger.warning(f"Error during credential cleanup for {redis_key}: {e}")

    if cleaned > 0:
        logger.info(f"Cleaned up {cleaned} expired SSH credentials")
    return cleaned
```

### Key Removal (#1616) - SshService.remove_ssh_key()

Now load-bearing for the cleanup sweep, so it must never over-delete or be
injectable. The comment travels via the exec **environment** (raw, no shell
parsing) and is matched by awk as the exact **last field** (`$NF`) read from
`ENVIRON` — no regex/shell metacharacter escaping, and a crafted comment can
neither match a neighbouring key (the old substring `sed -i '/comment/d'` could)
nor run a shell. Writes to a temp file and swaps only on awk success, then
restores 600 perms (sshd StrictModes).

```python
async def remove_ssh_key(self, agent_name: str, comment: str) -> bool:
    """Remove SSH key by comment (exact last field) from authorized_keys."""
    container = get_agent_container(agent_name)
    if not container:
        return True  # Container may have been deleted

    # Static script — comment flows in via the exec environment only.
    script = (
        "f=/home/developer/.ssh/authorized_keys\n"
        '[ -f "$f" ] || exit 0\n'
        't="$f.trinity-remove.$$"\n'
        'if awk \'BEGIN { c = ENVIRON["TRINITY_SSH_COMMENT"] } $NF != c\' '
        '"$f" > "$t"; then\n'
        '  mv "$t" "$f" && chmod 600 "$f"\n'
        'else\n  rm -f "$t"\n  exit 1\nfi\n'
    )
    await container_exec_run(
        container, ["sh", "-c", script], user="developer",
        environment={"TRINITY_SSH_COMMENT": comment},
    )
    return True
```

### Key Injection (#1616) - SshService.inject_ssh_key()

One atomic `sh -c` script; the public key flows in via the exec **environment**
(never interpolated — the old `sh -c 'printf ... '<key>' ...'` form was run
through docker-py's `shlex.split` AND the container's `sh -c`, so a single quote
in the key raised `ValueError` and a `$(...)`/backtick opened an in-container
command substitution). `skip_if_present=True` (default) appends only if the exact
line is absent (`grep -qxF`), so a repeat/retried inject never duplicates a line.

```python
async def inject_ssh_key(self, agent_name, public_key, skip_if_present=True) -> bool:
    ...
    script = (
        "set -e\nd=/home/developer/.ssh\nf=\"$d/authorized_keys\"\n"
        'mkdir -p "$d"\nchmod 700 "$d"\ntouch "$f"\n'
        # append-if-absent when skip_if_present
        'if ! grep -qxF -- "$TRINITY_SSH_KEY" "$f"; then\n'
        "  printf '%s\\n' \"$TRINITY_SSH_KEY\" >> \"$f\"\nfi\n"
        'chmod 600 "$f"\n'
    )
    result = await container_exec_run(
        container, ["sh", "-c", script], user="developer",
        environment={"TRINITY_SSH_KEY": public_key},
    )
    return result.exit_code == 0
```

### Agent Stop/Delete Cleanup (Lines 441-484) - SshService.cleanup_agent_credentials()

```python
async def cleanup_agent_credentials(self, agent_name: str) -> int:
    """Clean up all SSH credentials for an agent (called on agent stop/delete)."""
    pattern = f"{SSH_ACCESS_PREFIX}{agent_name}:*"
    redis_keys = self.redis_client.keys(pattern)

    has_password_creds = False
    for key in redis_keys:
        try:
            data = self.redis_client.get(key)
            if data:
                metadata = json.loads(data)
                auth_type = metadata.get("auth_type", "key")
                credential_id = metadata.get("credential_id") or metadata.get("comment")

                if auth_type == "password":
                    has_password_creds = True
                elif credential_id:
                    await self.remove_ssh_key(agent_name, credential_id)
        except Exception as e:
            logger.warning(f"Error cleaning up credential {key}: {e}")

        self.redis_client.delete(key)

    # Clear password once if any password credentials existed
    if has_password_creds:
        await self.clear_container_password(agent_name)

    if redis_keys:
        logger.info(f"Cleaned up {len(redis_keys)} SSH credentials for agent {agent_name}")

    return len(redis_keys)
```

---

## Settings Layer

### Ops Setting Definition

**settings_service.py** (`src/backend/services/settings_service.py:32, 45`)

```python
OPS_SETTINGS_DEFAULTS = {
    # ... other settings ...
    "ssh_access_enabled": "false",  # Enable SSH access via MCP tool
}

OPS_SETTINGS_DESCRIPTIONS = {
    # ... other settings ...
    "ssh_access_enabled": "Enable ephemeral SSH access to agent containers via MCP tool (default: false)",
}
```

### Admin Configuration

SSH access must be explicitly enabled in the UI:
**Settings page → SSH Access section → Enable SSH Access toggle**

Or via API:
```bash
curl -X PUT /api/settings/ops/config \
  -H "Authorization: Bearer TOKEN" \
  -d '{"ssh_access_enabled": "true"}'
```

---

## Container Security Requirements

### Linux Capabilities

**lifecycle.py** (`src/backend/services/agent_service/lifecycle.py:31-37`)

```python
# Restricted mode capabilities - minimum for agent operation (default)
RESTRICTED_CAPABILITIES = [
    'NET_BIND_SERVICE',  # Bind to ports < 1024
    'SETGID', 'SETUID',  # Change user/group (for su/sudo)
    'CHOWN',             # Change file ownership
    'SYS_CHROOT',        # Use chroot
    'AUDIT_WRITE',       # Write to audit log
]
```

| Capability | Purpose |
|------------|---------|
| `SETGID` | SSH privilege separation (sshd changes GID) |
| `SETUID` | SSH privilege separation (sshd changes UID) |
| `CHOWN` | Ownership changes for SSH files |
| `SYS_CHROOT` | SSH ChrootDirectory support |
| `AUDIT_WRITE` | PAM session logging |
| `NET_BIND_SERVICE` | Bind to privileged ports (SSH) |

### Security Options

```python
security_opt=['apparmor:docker-default'],  # no-new-privileges removed for SSH support
```

**Important**: `no-new-privileges` is NOT set because SSH privilege separation requires setuid/setgid transitions.

---

## Complete Flow

### Key-Based Authentication (the only flow)

```
1. Client generates keypair locally: ssh-keygen -t ed25519
   |
2. MCP Client calls get_agent_ssh_access(agent_name, ttl_hours=4, auth_method="key", public_key="ssh-ed25519 AAAA...")
   |
3. MCP Tool (agents.ts) -> apiClient.createSshAccess()
   |
4. POST /api/agents/{name}/ssh-access (agent_ssh.py)
   |-- Requires admin role (via require_admin dependency)
   |   └── 403 if not admin
   |-- Check ssh_access_enabled ops setting
   |   └── 403 if disabled
   |-- Get container, verify running
   |   └── 404 if not found, 400 if not running
   |-- Validate TTL (0.1-24 hours)
   |-- Validate public_key format (must start with ssh-ed25519, ssh-rsa, etc.)
   |   └── 400 if missing or invalid
   |-- Get SSH port from container labels
   |-- Get host (SSH_HOST env or Tailscale or localhost)
   |
5. Key Injection (ssh_service.py) — #1616: one atomic `sh -c`; key via exec env, never interpolated
   |-- Append tracking comment: trinity-ephemeral-{agent}-{timestamp}
   |-- docker exec sh -c: mkdir -p ~/.ssh; chmod 700; touch authorized_keys
   |-- ...append-if-absent (grep -qxF) so a repeat inject can't duplicate a line
   |-- ...chmod 600 authorized_keys
   |
6. Store Metadata in Redis (ssh_service.py)
   |-- Key: ssh_access:{agent_name}:{comment}
   |-- TTL: ttl_hours * 3600 seconds
   |-- Value: { agent_name, credential_id, auth_type, created_at, expires_at, created_by, public_key }
   |
7. Return Response to Client (NO private key)
   {
     "status": "success",
     "agent": "my-agent",
     "auth_method": "key",
     "connection": {
       "command": "ssh -p 2222 developer@100.x.x.x",
       "host": "100.x.x.x",
       "port": 2222,
       "user": "developer"
     },
     "expires_at": "2026-01-02T20:00:00Z",
     "expires_in_hours": 4,
     "instructions": [
       "Connect: ssh -p 2222 developer@100.x.x.x",
       "Key expires in 4 hours"
     ]
   }
```

## Error Handling

| Error Case | HTTP Status | Message |
|------------|-------------|---------|
| User is not admin | 403 | "Admin role required" (from require_admin dependency) |
| SSH access disabled globally | 403 | "SSH access is disabled. Enable it in Settings -> Ops Settings -> ssh_access_enabled" |
| Agent container not found | 404 | "Agent not found" |
| Agent not running | 400 | "Agent must be running to generate SSH access. Start the agent first." |
| auth_method other than "key" | 400 | "Password SSH auth is no longer supported. Use key-based auth: ..." (#1615) |
| Missing public_key for key auth | 400 | "public_key is required for key-based authentication..." |
| Invalid public_key format | 400 | "Invalid public key format. Must be an OpenSSH public key..." |
| Key injection failed | 500 | "Failed to inject SSH key into agent container" |

---

## Security Considerations

1. **System-Level Control**: SSH access is disabled by default (`ssh_access_enabled = false`). Admin must explicitly enable.

2. **Admin-Only Access**: Endpoint requires admin role (`require_admin` dependency). Agent owners and shared users cannot generate SSH credentials — SSH grants shell access to injected credentials inside containers, so it's restricted to platform admins only.

3. **Ephemeral Credentials**: All credentials auto-expire via Redis TTL. Maximum TTL is 24 hours.

4. **No Server-Side Key Generation**: Private keys are never generated, transmitted, or stored server-side. Clients generate their own keypairs locally and supply only the public key (SEC #175).

5. **Container Isolation**: Each agent has its own container with isolated SSH configuration.

6. **No Persistent Keys**: Ephemeral keys are appended to authorized_keys with unique comments, allowing targeted removal.

7. **Container Capabilities**: Minimal capabilities granted - only those required for SSH privilege separation.

9. **Tailscale Priority**: Prefers Tailscale IP over localhost for secure network access.

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SSH_ACCESS_DEFAULT_TTL_HOURS` | 4 | Default credential lifetime |
| `SSH_ACCESS_MAX_TTL_HOURS` | 24 | Maximum allowed TTL |
| `SSH_ACCESS_CLEANUP_INTERVAL` | 900 | **Unused** (legacy). The expired-key sweep runs on `cleanup_service`'s own 5-min cycle (`CLEANUP_INTERVAL_SECONDS`), not this value (#1616). |
| `SSH_HOST` | (auto-detect) | Override host for SSH commands (highest priority) |
| `FRONTEND_URL` | `http://localhost` | Used to auto-detect SSH host in production (e.g., `https://trinity.abilityai.dev` → `trinity.abilityai.dev`) |

---

## Related Flows

- **Upstream**: [agent-lifecycle.md](agent-lifecycle.md) - Container creation with SSH capabilities
- **Related**: [agent-terminal.md](agent-terminal.md) - Alternative browser-based terminal access
- **Related**: [mcp-orchestration.md](mcp-orchestration.md) - MCP tool registration
- **Related**: [async-docker-operations.md](async-docker-operations.md) - Async wrappers for all Docker exec calls (DOCKER-001)

---

## Testing

### Prerequisites
- Backend running at localhost:8000
- MCP server running at localhost:8080
- At least one agent running
- `ssh_access_enabled` set to `true` in Ops Settings

### Test Steps

1. **Enable SSH Access**
   - Action: Navigate to Settings -> Ops Settings -> ssh_access_enabled = true
   - Verify: Setting saved successfully

2. **Generate Key-Based Credentials via MCP**
   - Action: `ssh-keygen -t ed25519` locally, then call `get_agent_ssh_access` with your PUBLIC key
   - Expected: `connection.command` returned; **no** `private_key` field (removed in #175 — the server never handles private keys)
   - Verify: Run the SSH command with your own private key, verify connection

3. **Password auth is refused**
   - Action: `POST /api/agents/{agent}/ssh-access` with `{"auth_method": "password"}`
   - Expected: **400** naming key auth as the alternative — not a 500 (the pre-#1615 `crypt` ImportError) and not a silent success
   - Verify: `pytest tests/unit/test_ssh_service.py`

4. **TTL Validation**
   - Action: Call with `ttl_hours: 0.01` (too low) and `ttl_hours: 100` (too high)
   - Expected: TTL clamped to 0.1 and 24 respectively
   - Verify: Check expires_at in response

5. **Stopped Agent Rejection**
   - Action: Stop agent, then call `get_agent_ssh_access`
   - Expected: 400 error "Agent must be running"

6. **Disabled Setting Rejection**
   - Action: Set `ssh_access_enabled = false`, call `get_agent_ssh_access`
   - Expected: 403 error with enable instructions

7. **Credential Expiry (#1616)**
   - Action: Generate credential with short TTL (0.1 hours = 6 min)
   - Expected: After expiry AND one cleanup cycle (≤5 min), SSH connection fails
   - Verify: Redis key auto-deleted by TTL; the key's line is removed from
     `authorized_keys` by the `cleanup_service` expired-SSH sweep (previously
     the line lingered and still granted login — #1616)

### Edge Cases
- Multiple concurrent SSH sessions (should work)
- Key generation for agent with existing keys (appends to authorized_keys; a repeat inject of the same key is a no-op — #1616 append-if-absent)
- `docker restart` (same container) preserves keys; a **recreate** rebuilds the container. A normal, never-renamed agent is volume-safe — `/home/developer` is a named volume (`agent-{name}-workspace`) that the config-recreate path forwards, so `.ssh` survives. Genuinely volume-less agents (ephemeral "ghost" agents) are overlay-only by design and lose `.ssh` on recreate. The recreate-wipe symptom in #1616's report is an unidentified mechanism on the reporter's build (not the #1664/#1665/#1667 family, which postdates it); no start-time re-injection hook was added.

### Status
Not Tested
