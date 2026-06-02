# Feature: Local Agent Deployment via MCP

> **Updated**: 2026-05-29 - #950 deferred hardening: require non-empty CLAUDE.md, advisory MCP credential-gap `warnings[]`. The API/CLI accept an optional `credentials` map (the MCP tool forwards archive + name only).

## Overview

Deploy Trinity-compatible local Claude Code agents to a remote Trinity platform with a single MCP command. The **local agent** (Claude Code on your machine) packages the directory into a tar.gz archive and sends it to the remote Trinity backend for deployment.

**Key Architecture Point**: The MCP server runs remotely and cannot access your local filesystem. Therefore, the **calling agent** must package the archive locally before invoking the MCP tool.

## User Story

As a developer working with a Trinity-compatible local agent, I want to deploy it to a remote Trinity instance with one command so I can run it on the platform without manual file transfer.

## Entry Points

- **CLI**: `trinity deploy .` — packages and uploads local directory (`src/cli/trinity_cli/commands/deploy.py`)
- **MCP Tool**: `deploy_local_agent` via Trinity MCP server
- **API**: `POST /api/agents/deploy-local`

---

## Architecture

```
+-------------------------------------+                     +-----------------------------+
|  Your Local Machine                 |                     |  Remote Trinity Server      |
|                                     |                     |                             |
|  Claude Code (local agent)          |     HTTP POST       |  MCP Server                 |
|  1. tar -czf archive.tar.gz ...     |  ---------------->  |  deploy_local_agent tool    |
|  2. base64 archive.tar.gz           |   archive           |         |                   |
|  3. Call deploy_local_agent         |                     |         v                   |
|                                     |                     |  Backend API                |
|                                     |                     |  /api/agents/deploy-local   |
|  /home/you/my-agent/                |                     |         |                   |
|  |-- template.yaml                  |                     |         v                   |
|  |-- CLAUDE.md                      |                     |  Extract, validate, deploy  |
|  +-- .env                           |                     |  Agent container created    |
+-------------------------------------+                     +-----------------------------+
```

---

## MCP Tool Layer

### Tool: `deploy_local_agent`

**Location**: `src/mcp-server/src/tools/agents.ts:556-643`

**Parameters**:
```typescript
{
  archive: string,                    // Base64-encoded tar.gz archive (REQUIRED)
  name?: string                       // Override agent name (optional)
}
```

The archive should include all files needed by the agent — `.env`, `.mcp.json`, `CLAUDE.md`, etc. **The MCP tool exposes only `archive` and `name`** and forwards just those two fields to the backend (no credential parameter on this path). The underlying API and `trinity deploy` CLI additionally accept an optional `credentials` map that is merged into the deployed `.env` (see Request Model + step 9 below) — that path is what surfaces the MCP credential-gap `warnings[]`.

**Validation** (lines 588-600):
- Checks archive is provided and non-empty
- Validates base64 format with regex: `/^[A-Za-z0-9+/=]+$/`

**Description**: The tool receives a pre-packaged archive from the calling agent and forwards it to the backend. It does NOT access the local filesystem - that's the calling agent's responsibility.

---

## Calling Agent Workflow

The local Claude Code agent must perform these steps before calling `deploy_local_agent`:

### Step 1: Create tar.gz Archive

```bash
# Package the agent directory, including .env and all credential files
tar -czf /tmp/agent-deploy.tar.gz \
  --exclude='.git' \
  --exclude='node_modules' \
  --exclude='__pycache__' \
  --exclude='.venv' \
  --exclude='venv' \
  --exclude='*.pyc' \
  --exclude='.DS_Store' \
  -C /path/to/parent agent-directory-name
```

### Step 2: Base64 Encode

```bash
# macOS
base64 -i /tmp/agent-deploy.tar.gz > /tmp/agent-deploy.b64

# Linux
base64 /tmp/agent-deploy.tar.gz > /tmp/agent-deploy.b64
```

### Step 3: Call MCP Tool

The agent then calls `deploy_local_agent` with:
- `archive`: Contents of the base64 file
- `name`: Optional name override

---

## Backend Layer

### Architecture (Service Layer)

The local agent deployment uses a **thin router + service layer** architecture:

| Layer | File | Purpose |
|-------|------|---------|
| Router | `src/backend/routers/agents.py` | Endpoint definition |
| Service | `src/backend/services/agent_service/deploy.py` | Deployment business logic |

### Endpoint: POST /api/agents/deploy-local

**Router**: `src/backend/routers/agents.py:418-430`

```python
@router.post("/deploy-local")
async def deploy_local_agent(
    body: DeployLocalRequest,
    request: Request,
    current_user: User = Depends(require_role("creator"))
):
    """Deploy a Trinity-compatible local agent. Requires creator role or above."""
    return await deploy_local_agent_logic(
        body=body,
        current_user=current_user,
        request=request,
        create_agent_fn=create_agent_internal
    )
```

Deploy requires the **creator** role or above (`require_role("creator")`),
consistent with `create_agent` — see [role-model.md](role-model.md).

**Request Model** (`src/backend/models.py`):
```python
class DeployLocalRequest(BaseModel):
    """Request to deploy a local agent."""
    archive: str  # Base64-encoded tar.gz
    name: Optional[str] = None  # Override name from template.yaml
    credentials: Optional[Dict[str, str]] = None  # Optional {KEY: value} merged into .env

# Maximum credentials allowed per deploy-local request
MAX_DEPLOY_CREDENTIALS = 100
```

`credentials` is capped at `MAX_DEPLOY_CREDENTIALS` (100); exceeding it returns
HTTP 400. The MCP `deploy_local_agent` tool does not pass this field — it is
used by the API and `trinity deploy` CLI to fold operator-supplied secrets into
the archive's `.env` at deploy time.

**Response Model** (`src/backend/models.py`):
```python
class DeployLocalResponse(BaseModel):
    """Response from local agent deployment."""
    status: str  # "success" or "error"
    agent: Optional[AgentStatus] = None
    versioning: Optional[VersioningInfo] = None
    credentials_imported: Optional[Dict[str, str]] = None
    credentials_injected: Optional[int] = None
    warnings: List[str] = []  # Advisory deploy-time warnings (e.g. MCP credential gaps)
    error: Optional[str] = None
    code: Optional[str] = None  # Error code for machine-readable errors
```

`warnings` carries non-fatal advisories — currently MCP servers whose
`${VAR}` references have no matching credential after the request `credentials`
are merged into `.env` (see step 9 below). The MCP `deploy_local_agent` tool
`JSON.stringify`s the whole response, so warnings reach `/trinity:onboard`
automatically.

### Deployment Flow (`deploy.py`)

1. **Decode & Validate Archive**
   - Decode base64 archive
   - Check size limit (50MB max)
   - Reject `body.credentials` exceeding `MAX_DEPLOY_CREDENTIALS` (100) → HTTP 400

2. **Extract Archive**
   - Extract to temp directory using `_safe_extract_tar()`
   - Security: Full path validation via `_validate_tar_member()`

3. **Find Root Directory**
   - Handle nested extraction (single directory case)

4. **Trinity-Compatible Validation**
   - `is_trinity_compatible()` in `services/template_service.py`
   - Requires template.yaml with `name` and `resources` fields
   - Requires a non-empty, UTF-8-readable `CLAUDE.md` — missing / empty /
     whitespace-only / non-UTF-8 → HTTP 400 `NOT_TRINITY_COMPATIBLE` (#950).
     **Behavior change**: agents that previously deployed without a CLAUDE.md
     (a warning, not an error) are now rejected at deploy time. A redeploy of
     a CLAUDE.md-less local agent will 400 until a CLAUDE.md is added.

5. **Determine Agent Name**
   - Use body.name override or template.yaml name
   - Sanitize with `sanitize_agent_name()`

6. **Agent Quota Enforcement** (added in #259)
   - Check if existing versions are owned by current user (`get_agents_by_prefix` + `get_agents_by_owner`)
   - Skip quota for redeploys of user-owned agents
   - For new agents: enforce `max_agents_per_user` setting (default: 3)
   - System agents excluded from count
   - Returns HTTP 429 with `QUOTA_EXCEEDED` code on limit

7. **Version Handling**
   - `get_next_version_name()` finds next available version
   - Pattern: `my-agent` -> `my-agent-2` -> `my-agent-3`
   - Stops previous version if running

8. **Persist Template** (#950)
   - Write the validated archive contents to `/data/deployed-templates/{version_name}/` (`dest_path`) for inspection and future `template.yaml` lookups. On write failure: HTTP 500 with `code=DEPLOYED_TEMPLATES_DIR_UNWRITABLE` (fail-fast, no silent fallback).
   - Curated catalog at `/agent-configs/templates` stays read-only (operators' source of truth).

9. **Merge Credentials + MCP Credential-Gap Warnings** (#950)
   - If `body.credentials` is provided, merge the `{KEY: value}` pairs into the persisted `.env` at `dest_path/.env` (returned `credentials_injected` count; `credentials_imported` records `.env`/`.mcp.json` provenance: `from_archive` / `merged` / `created`).
   - `collect_mcp_credential_warnings(dest_path)` then scans `.mcp.json.template` (falling back to `.mcp.json`) for `${VAR}` references whose key is neither present in the **post-merge** `.env` nor platform-injected, and returns them as advisory `warnings[]` — non-fatal. The platform-injected allowlist (`_PLATFORM_INJECTED_EXACT` + `TRINITY_`/`GIT_`/`OTEL_`/`CLAUDE_CODE_` prefixes) is a deliberate static mirror of the env vars Trinity sets at create time (`crud.py`), so those don't produce false-positive gaps. The MCP server name (an arbitrary, operator-supplied JSON key) is passed through `_sanitize_for_warning()` before interpolation — non-printable characters (ANSI escapes, newlines, C0/C1 controls) are stripped and the length is bounded — so a hostile template can't smuggle terminal-escape sequences into the operator-facing warning rendered by `/trinity:onboard` (CSO L1).

10. **Workspace Volume Pre-population** (#950)
    - **Pre-populate the agent's workspace volume directly** via `put_archive` into an ephemeral `alpine:3.20` container that mounts `agent-{version_name}-workspace`. Includes a `.trinity-initialized` marker so the agent's `startup.sh` skips its `/template` → `/home/developer` copy on boot. On failure: HTTP 500 with `code=WORKSPACE_PREPOP_FAILED`.
    - **Why no bind-mount transport for deploy-local**: dev compose uses a docker-managed named volume for `/data` while prod uses a host bind. Any host-path math in `crud.py` was right on prod and wrong on dev, producing empty agents on dev. Pre-populating the workspace volume directly is uniform across both.

11. **Agent Creation**
    - Extract runtime config from template
    - Call `create_agent_fn()` (injected `create_agent_internal`) with local template
    - Agent container starts with all files from the archive (including `.env`)

12. **Return Response**
    - Return DeployLocalResponse with agent status, versioning info, `credentials_injected`/`credentials_imported`, and any advisory `warnings`

### Safe Tar Extraction (`deploy.py:39-181`)

The extraction uses comprehensive security validation:

**Path Validation** (`_is_path_within()`, lines 43-60):
- Uses `Path.resolve()` to normalize paths
- Checks target stays within base directory

**Member Validation** (`_validate_tar_member()`, lines 63-135):
- Rejects absolute paths
- Rejects path traversal (`..` in paths)
- Validates destination stays within base_dir
- Rejects device files (chr, blk) and FIFOs
- Validates symlink targets stay within base_dir
- Validates hardlink targets stay within base_dir

**Safe Extraction** (`_safe_extract_tar()`, lines 138-180):
- Checks file count (1000 max)
- Validates all members before extraction
- Only extracts validated members

---

## Template Validation

**Location**: `src/backend/services/template_service.py:608-728` (`is_trinity_compatible` + `collect_mcp_credential_warnings`)

```python
def is_trinity_compatible(path: Path) -> Tuple[bool, Optional[str], Optional[dict]]:
    """
    Check if a directory contains a Trinity-compatible agent.

    A Trinity-compatible agent must have:
    1. template.yaml file
    2. name field in template.yaml
    3. resources field in template.yaml
    4. a non-empty CLAUDE.md (agent instructions)
    """
```

**Validation Checks**:
1. `template.yaml` exists
2. File is valid YAML
3. File is not empty
4. `name` field present
5. `resources` field present and is a dictionary
6. `CLAUDE.md` present, readable as UTF-8, and non-empty after `.strip()`
   (blocking — #950; a binary/non-UTF-8 CLAUDE.md is rejected with a clean
   400 rather than crashing the generic handler with a 500)

---

## Error Codes

| Code | HTTP | Description |
|------|------|-------------|
| `NOT_TRINITY_COMPATIBLE` | 400 | Missing/invalid template.yaml, or missing/empty/non-UTF-8 CLAUDE.md (#950) |
| `ARCHIVE_TOO_LARGE` | 400 | Exceeds 50MB limit |
| `INVALID_ARCHIVE` | 400 | Not valid tar.gz, bad base64, or path traversal |
| `TOO_MANY_FILES` | 400 | Exceeds 1000 file limit |
| `MISSING_NAME` | 400 | No name specified and template.yaml has no name |
| `DEPLOYED_TEMPLATES_DIR_UNWRITABLE` | 500 | `/data/deployed-templates` could not be created — fail-fast, no silent fallback (#950/#971) |
| `WORKSPACE_PREPOP_FAILED` | 500 | Workspace volume pre-population (`put_archive`/chown) failed (#950) |

---

## Size Limits

| Limit | Value | Constant Location |
|-------|-------|-------------------|
| Archive size | 50 MB | `deploy.py` MAX_ARCHIVE_SIZE |
| File count | 1000 | `deploy.py` MAX_FILES |

---

## Security Considerations

1. **Path Traversal Prevention**: Archive paths validated via `_validate_tar_member()`:
   - No `..` in paths
   - No absolute paths
   - Symlinks/hardlinks validated to stay within extraction dir
   - Device files and FIFOs rejected

2. **Temp Cleanup**: Temp directory always cleaned up in finally block (lines 430-436)

3. **Self-Contained Archives**: Credentials (`.env`) travel inside the archive. The optional `credentials` map is an additive merge into that `.env` at deploy time (step 9), not an out-of-band injection into a running container.

4. **Auth Required**: JWT authentication plus the **creator** role gate (`require_role("creator")`, which wraps `get_current_user`)

5. **Write Permission Check**: Templates directory write-tested before use

---

## Testing

### Prerequisites
- Trinity backend running (local or remote)
- MCP server running and accessible
- Valid MCP API key configured in Claude Code
- Local agent directory with valid template.yaml

### Test Steps

#### 1. Create Test Agent Directory
```bash
mkdir -p /tmp/test-deploy-agent
cat > /tmp/test-deploy-agent/template.yaml << 'EOF'
name: test-deploy
display_name: Test Deploy Agent
description: Testing local agent deployment
resources:
  cpu: "2"
  memory: "4g"
EOF

echo "# Test Deploy Agent" > /tmp/test-deploy-agent/CLAUDE.md
echo "TEST_API_KEY=test-value-123" > /tmp/test-deploy-agent/.env
```

#### 2. Package and Deploy via Claude Code

In Claude Code with Trinity MCP configured, ask:

```
Package and deploy my local agent at /tmp/test-deploy-agent to Trinity.
```

**Expected**: Claude Code will:
1. Run `tar` command to create archive (including .env)
2. Run `base64` to encode it
3. Call the MCP tool with the archive

**Verify**:
- Agent "test-deploy" created in Trinity
- Agent has .env file from archive

#### 3. Deploy Again (Versioning Test)
```
Deploy my local agent at /tmp/test-deploy-agent to Trinity again
```

**Expected**:
- New agent "test-deploy-2" created
- Previous "test-deploy" stopped

#### 4. Test Invalid Archive
```
Call deploy_local_agent with archive="not-valid-base64!"
```

**Expected**: Error "Invalid archive format"

#### 5. Test Missing Template
```bash
rm /tmp/test-deploy-agent/template.yaml
# Then try to deploy
```

**Expected**: Error "NOT_TRINITY_COMPATIBLE"

### Edge Cases
- [ ] Archive larger than 50MB -> ARCHIVE_TOO_LARGE
- [ ] More than 1000 files -> TOO_MANY_FILES
- [ ] Path traversal in archive -> INVALID_ARCHIVE

### Cleanup
```bash
rm -rf /tmp/test-deploy-agent
rm -f /tmp/agent-deploy.tar.gz /tmp/agent-deploy.b64
```

---

## Example: Full Deployment Script

For reference, here's a complete bash script a local agent might execute:

```bash
#!/bin/bash
# deploy-to-trinity.sh - Package and prepare for MCP deployment

AGENT_DIR="$1"
if [ -z "$AGENT_DIR" ]; then
  echo "Usage: deploy-to-trinity.sh /path/to/agent"
  exit 1
fi

# Validate template.yaml exists
if [ ! -f "$AGENT_DIR/template.yaml" ]; then
  echo "Error: Not Trinity-compatible - missing template.yaml"
  exit 1
fi

# Create archive (includes .env and all credential files)
ARCHIVE="/tmp/trinity-deploy-$$.tar.gz"
tar -czf "$ARCHIVE" \
  --exclude='.git' \
  --exclude='node_modules' \
  --exclude='__pycache__' \
  --exclude='.venv' \
  -C "$(dirname "$AGENT_DIR")" "$(basename "$AGENT_DIR")"

# Base64 encode
ARCHIVE_B64=$(base64 -i "$ARCHIVE" 2>/dev/null || base64 "$ARCHIVE")

echo "Archive size: $(wc -c < "$ARCHIVE") bytes"
echo "Ready for deploy_local_agent MCP call"

# Cleanup
rm -f "$ARCHIVE"
```

---

## Related Documentation

- [TRINITY_COMPATIBLE_AGENT_GUIDE.md](../../TRINITY_COMPATIBLE_AGENT_GUIDE.md) - Required template.yaml structure
- [credential-injection.md](credential-injection.md) - Credential management
- [agent-lifecycle.md](agent-lifecycle.md) - Agent creation flow

---

## Revision History

| Date | Changes |
|------|---------|
| 2026-05-29 | **#950 (deferred hardening)**: `is_trinity_compatible()` now requires a non-empty, UTF-8 `CLAUDE.md` (blocking 400, was a non-fatal warning). New `collect_mcp_credential_warnings()` surfaces MCP servers with unsatisfied `${VAR}` refs as advisory `DeployLocalResponse.warnings[]` (also added to the MCP tool response type). Documented the `credentials` request field (reinstated after #251) + `MAX_DEPLOY_CREDENTIALS`, the credential-merge step, and the `DEPLOYED_TEMPLATES_DIR_UNWRITABLE`/`WORKSPACE_PREPOP_FAILED` error codes. Refreshed router snippet (`require_role("creator")`, `agents.py:418-430`). |
| 2026-04-03 | **#251**: Removed `credentials` parameter from the deploy flow. Archive is self-contained — `.env` and credential files included in tar.gz. Removed credential injection block that caused hangs. *(Note: a `credentials` map was later reinstated as an optional API/CLI field — see 2026-05-29.)* |
| 2026-02-05 | **CRED-002**: Removed `credential_manager` parameter from deploy flow. |
| 2026-01-23 | Verified all line numbers. Updated deploy.py references (now 437 lines). Added safe tar extraction details. Updated router line numbers (212-225). Added template validation location. |
| 2025-12-30 | Verified line numbers |
| 2025-12-27 | Service layer refactoring: Deploy logic moved to `services/agent_service/deploy.py` |
| 2025-12-24 | Changed from local path to archive-based deployment |
| 2025-12-21 | Initial implementation |

**Status**: Working
