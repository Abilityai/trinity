# Feature: Local Agent Deployment via MCP

## Overview

Deploy Trinity-compatible local Claude Code agents to Trinity platform with a single MCP command. The tool packages the agent directory, auto-imports credentials from `.env`, and deploys with versioning support.

## User Story

As a developer working with a Trinity-compatible local agent, I want to deploy it to Trinity with one command so I can run it on the remote platform without any manual setup.

## Entry Points

- **MCP Tool**: `deploy_local_agent` via Trinity MCP server
- **API**: `POST /api/agents/deploy-local`

---

## MCP Tool Layer

### Tool: `deploy_local_agent`

**Location**: `src/mcp-server/src/tools/agents.ts`

**Parameters**:
```typescript
{
  path: string,        // Absolute path to local agent directory
  name?: string,       // Override agent name (defaults to template.yaml name)
  include_env?: boolean // Include .env credentials (default: true)
}
```

**Flow**:
1. Validate directory exists
2. Check template.yaml exists (fail fast if not Trinity-compatible)
3. Read `.env` file if `include_env=true`
4. Create tar.gz archive (excludes .git, node_modules, __pycache__, .venv, .env)
5. Base64 encode archive
6. POST to `/api/agents/deploy-local`
7. Return deployment result

---

## Backend Layer

### Endpoint: POST /api/agents/deploy-local

**Location**: `src/backend/routers/agents.py:856`

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

### Deployment Flow

1. **Decode & Validate**
   - Decode base64 archive
   - Check size limit (50MB max)
   - Check credentials count (100 max)

2. **Extract Archive**
   - Extract to temp directory
   - Security: Check for path traversal (`..` in paths)
   - Check file count (1000 max)

3. **Trinity-Compatible Validation**
   - `is_trinity_compatible()` in `services/template_service.py`
   - Requires template.yaml with `name` and `resources` fields

4. **Version Handling**
   - `get_next_version_name()` finds next available version
   - Pattern: `my-agent` → `my-agent-2` → `my-agent-3`
   - Stops previous version if running

5. **Credential Import**
   - `import_credential_with_conflict_resolution()` in `credentials.py`
   - Same name + same value = reuse
   - Same name + different value = rename with suffix (`_2`, `_3`)
   - New name = create

6. **Template Copy**
   - Copy to `config/agent-templates/{version_name}/`

7. **Agent Creation**
   - Call `create_agent_internal()` with local template

8. **Credential Hot-Reload**
   - POST credentials to agent's internal API
   - Agent writes `.env` and regenerates `.mcp.json`

---

## Supporting Functions

### Template Validation

**Location**: `src/backend/services/template_service.py:309`

```python
def is_trinity_compatible(path: Path) -> Tuple[bool, Optional[str], Optional[dict]]:
    """
    Check if directory is Trinity-compatible.

    Requirements:
    - template.yaml exists
    - Has 'name' field
    - Has 'resources' field (dict)

    Returns: (is_valid, error_msg, template_data)
    """
```

### Credential Import

**Location**: `src/backend/credentials.py:367`

```python
def import_credential_with_conflict_resolution(
    self, key: str, value: str, user_id: str
) -> Dict[str, str]:
    """
    Import credential with conflict resolution.

    Returns:
    - {"status": "created", "name": "API_KEY"}
    - {"status": "reused", "name": "API_KEY"}
    - {"status": "renamed", "name": "API_KEY_2", "original": "API_KEY"}
    """
```

### Versioning Logic

**Location**: `src/backend/routers/agents.py:766`

```python
def get_agents_by_prefix(prefix: str) -> List[AgentStatus]:
    """Get all agents matching base name (my-agent, my-agent-2, etc.)."""

def get_next_version_name(base_name: str) -> str:
    """Get next available version: my-agent -> my-agent-2 -> my-agent-3."""

def get_latest_version(base_name: str) -> Optional[AgentStatus]:
    """Get most recent version of an agent."""
```

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
3. **Credential Handling**: Credentials not included in archive, sent separately
4. **Auth Required**: Uses standard MCP API key authentication
5. **Audit Logging**: Deploy events logged with user, agent name, archive size

---

## Testing

### Prerequisites
- Trinity backend running
- MCP server running
- Valid MCP API key configured

### Test Steps

#### 1. Create Test Agent
```bash
mkdir /tmp/test-agent
cat > /tmp/test-agent/template.yaml << EOF
name: test-agent
display_name: Test Agent
resources:
  cpu: "2"
  memory: "4g"
EOF
echo "# Test Agent" > /tmp/test-agent/CLAUDE.md
echo "TEST_API_KEY=test123" > /tmp/test-agent/.env
```

#### 2. Deploy via MCP
From Claude Code with Trinity MCP configured:
```
Deploy my local agent at /tmp/test-agent to Trinity
```

**Expected**:
- Agent "test-agent" created and running
- Credential TEST_API_KEY imported

#### 3. Deploy Again (Versioning)
```
Deploy my local agent at /tmp/test-agent to Trinity
```

**Expected**:
- New agent "test-agent-2" created
- Previous "test-agent" stopped

#### 4. Test Not Compatible
```bash
rm /tmp/test-agent/template.yaml
```
```
Deploy my local agent at /tmp/test-agent to Trinity
```

**Expected**: Error "Not Trinity-compatible: missing template.yaml"

### Edge Cases
- [ ] Archive larger than 50MB → ARCHIVE_TOO_LARGE
- [ ] More than 1000 files → TOO_MANY_FILES
- [ ] Path traversal in archive → INVALID_ARCHIVE
- [ ] Same credential with different value → renamed with suffix

---

## Related Documentation

- [TRINITY_COMPATIBLE_AGENT_GUIDE.md](../../TRINITY_COMPATIBLE_AGENT_GUIDE.md) - Required template.yaml structure
- [credential-injection.md](credential-injection.md) - Credential management
- [agent-lifecycle.md](agent-lifecycle.md) - Agent creation flow

---

**Implemented**: 2025-12-21
**Status**: ✅ Working
