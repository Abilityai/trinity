# Feature: Local Agent Deployment via MCP

> **Updated**: 2025-12-27 - Refactored to service layer architecture. Deploy logic moved to `services/agent_service/deploy.py`.

## Overview

Deploy Trinity-compatible local Claude Code agents to a remote Trinity platform with a single MCP command. The **local agent** (Claude Code on your machine) packages the directory into a tar.gz archive and sends it to the remote Trinity backend for deployment.

**Key Architecture Point**: The MCP server runs remotely and cannot access your local filesystem. Therefore, the **calling agent** must package the archive locally before invoking the MCP tool.

## User Story

As a developer working with a Trinity-compatible local agent, I want to deploy it to a remote Trinity instance with one command so I can run it on the platform without manual file transfer.

## Entry Points

- **MCP Tool**: `deploy_local_agent` via Trinity MCP server
- **API**: `POST /api/agents/deploy-local`

---

## Architecture

```
┌─────────────────────────────────────┐                     ┌─────────────────────────────┐
│  Your Local Machine                 │                     │  Remote Trinity Server      │
│                                     │                     │                             │
│  Claude Code (local agent)          │     HTTP POST       │  MCP Server                 │
│  1. tar -czf archive.tar.gz ...     │  ──────────────►    │  deploy_local_agent tool    │
│  2. base64 archive.tar.gz           │   archive +         │         │                   │
│  3. Read .env for credentials       │   credentials       │         ▼                   │
│  4. Call deploy_local_agent         │                     │  Backend API                │
│                                     │                     │  /api/agents/deploy-local   │
│  /home/you/my-agent/                │                     │         │                   │
│  ├── template.yaml                  │                     │         ▼                   │
│  ├── CLAUDE.md                      │                     │  Extract, validate, deploy  │
│  └── .env                           │                     │  Agent container created    │
└─────────────────────────────────────┘                     └─────────────────────────────┘
```

---

## MCP Tool Layer

### Tool: `deploy_local_agent`

**Location**: `src/mcp-server/src/tools/agents.ts`

**Parameters**:
```typescript
{
  archive: string,                    // Base64-encoded tar.gz archive (REQUIRED)
  credentials?: Record<string, string>, // Key-value pairs from .env (optional)
  name?: string                       // Override agent name (optional)
}
```

**Description**: The tool receives a pre-packaged archive from the calling agent and forwards it to the backend. It does NOT access the local filesystem - that's the calling agent's responsibility.

---

## Calling Agent Workflow

The local Claude Code agent must perform these steps before calling `deploy_local_agent`:

### Step 1: Create tar.gz Archive

```bash
# Package the agent directory, excluding unnecessary files
tar -czf /tmp/agent-deploy.tar.gz \
  --exclude='.git' \
  --exclude='node_modules' \
  --exclude='__pycache__' \
  --exclude='.venv' \
  --exclude='venv' \
  --exclude='.env' \
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

### Step 3: Read Credentials (Optional)

```bash
# Read .env file and parse KEY=VALUE pairs
cat /path/to/agent/.env
```

### Step 4: Call MCP Tool

The agent then calls `deploy_local_agent` with:
- `archive`: Contents of the base64 file
- `credentials`: Parsed key-value pairs from .env
- `name`: Optional name override

---

## Backend Layer

### Architecture (Post-Refactoring)

The local agent deployment uses a **thin router + service layer** architecture:

| Layer | File | Purpose |
|-------|------|---------|
| Router | `src/backend/routers/agents.py:191-204` | Endpoint definition |
| Service | `src/backend/services/agent_service/deploy.py` (306 lines) | Deployment business logic |

### Endpoint: POST /api/agents/deploy-local

**Router**: `src/backend/routers/agents.py:191-204`
**Service**: `src/backend/services/agent_service/deploy.py:40-307`

```python
# Router
@router.post("/deploy-local")
async def deploy_local_agent(body: DeployLocalRequest, request: Request, current_user: User = Depends(get_current_user)):
    """Deploy a Trinity-compatible local agent."""
    return await deploy_local_agent_logic(
        body=body,
        current_user=current_user,
        request=request,
        create_agent_fn=create_agent_internal,
        credential_manager=credential_manager
    )
```

**Request Model** (`src/backend/models.py`):
```python
class DeployLocalRequest(BaseModel):
    archive: str                              # Base64-encoded tar.gz
    credentials: Optional[Dict[str, str]]     # KEY=VALUE pairs
    name: Optional[str]                       # Override name
```

**Response Model**:
```python
class DeployLocalResponse(BaseModel):
    status: str                               # "success" or "error"
    agent: Optional[AgentStatus]              # Created agent info
    versioning: Optional[VersioningInfo]      # Version tracking
    credentials_imported: Dict[str, CredentialImportResult]
    credentials_injected: int
    error: Optional[str]
    code: Optional[str]                       # Error code
```

### Deployment Flow (`deploy.py:40-307`)

1. **Decode & Validate** (lines 70-99)
   - Decode base64 archive
   - Check size limit (50MB max)
   - Check credentials count (100 max)

2. **Extract Archive** (lines 101-134)
   - Extract to temp directory
   - Security: Check for path traversal (`..` in paths)
   - Check file count (1000 max)

3. **Trinity-Compatible Validation** (lines 143-152)
   - `is_trinity_compatible()` in `services/template_service.py`
   - Requires template.yaml with `name` and `resources` fields

4. **Version Handling** (lines 166-181)
   - `get_next_version_name()` in `helpers.py` finds next available version
   - Pattern: `my-agent` -> `my-agent-2` -> `my-agent-3`
   - Stops previous version if running

5. **Credential Import** (lines 183-194)
   - `import_credential_with_conflict_resolution()` in `credentials.py`
   - Same name + same value = reuse
   - Same name + different value = rename with suffix (`_2`, `_3`)
   - New name = create

6. **Template Copy** (lines 196-218)
   - Copy to `config/agent-templates/{version_name}/`

7. **Agent Creation** (lines 220-233)
   - Call `create_agent_fn()` (injected `create_agent_internal`) with local template

8. **Credential Hot-Reload** (lines 235-251)
   - POST credentials to agent's internal API
   - Agent writes `.env` and regenerates `.mcp.json`

---

## Error Codes

| Code | HTTP | Description |
|------|------|-------------|
| `NOT_TRINITY_COMPATIBLE` | 400 | Missing or invalid template.yaml |
| `ARCHIVE_TOO_LARGE` | 400 | Exceeds 50MB limit |
| `INVALID_ARCHIVE` | 400 | Not valid tar.gz or path traversal |
| `TOO_MANY_FILES` | 400 | Exceeds 1000 file limit |
| `TOO_MANY_CREDENTIALS` | 400 | Exceeds 100 credential limit |
| `MISSING_NAME` | 400 | No name specified and template.yaml has no name |

---

## Size Limits

| Limit | Value | Rationale |
|-------|-------|-----------|
| Archive size | 50 MB | Prevents memory issues with base64 |
| Credential count | 100 | Reasonable upper bound |
| File count | 1000 | Prevents abuse |

---

## Security Considerations

1. **Path Traversal**: Archive paths checked for `..` and absolute paths
2. **Temp Cleanup**: Temp directory always cleaned up in finally block
3. **Credential Handling**: Credentials sent separately from archive, not stored in archive
4. **Auth Required**: Uses standard MCP API key authentication
5. **Audit Logging**: Deploy events logged with user, agent name, archive size

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

Steps:
1. Create a tar.gz archive of the directory (exclude .git, node_modules, .env)
2. Base64 encode the archive
3. Read the .env file for credentials
4. Call deploy_local_agent with the archive and credentials
```

**Expected**: Claude Code will:
1. Run `tar` command to create archive
2. Run `base64` to encode it
3. Parse .env file
4. Call the MCP tool with the data

**Verify**:
- Agent "test-deploy" created and running in Trinity
- Credential TEST_API_KEY imported

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
- [ ] Archive larger than 50MB → ARCHIVE_TOO_LARGE
- [ ] More than 1000 files → TOO_MANY_FILES
- [ ] Path traversal in archive → INVALID_ARCHIVE
- [ ] Same credential with different value → renamed with suffix

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

# Create archive
ARCHIVE="/tmp/trinity-deploy-$$.tar.gz"
tar -czf "$ARCHIVE" \
  --exclude='.git' \
  --exclude='node_modules' \
  --exclude='__pycache__' \
  --exclude='.venv' \
  --exclude='.env' \
  -C "$(dirname "$AGENT_DIR")" "$(basename "$AGENT_DIR")"

# Base64 encode
ARCHIVE_B64=$(base64 -i "$ARCHIVE" 2>/dev/null || base64 "$ARCHIVE")

# Read credentials from .env
CREDENTIALS="{}"
if [ -f "$AGENT_DIR/.env" ]; then
  CREDENTIALS=$(grep -v '^#' "$AGENT_DIR/.env" | grep '=' | \
    awk -F'=' '{gsub(/^[ \t]+|[ \t]+$/, "", $1); gsub(/^[ \t]+|[ \t]+$/, "", $2);
                printf "\"%s\": \"%s\", ", $1, $2}' | \
    sed 's/, $//' | sed 's/^/{/' | sed 's/$/}/')
fi

echo "Archive size: $(wc -c < "$ARCHIVE") bytes"
echo "Credentials: $CREDENTIALS"
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

**Implemented**: 2025-12-21
**Updated**: 2025-12-27 - Service layer refactoring: Deploy logic moved to `services/agent_service/deploy.py`
**Updated**: 2025-12-24 - Changed from local path to archive-based deployment
**Status**: Working
