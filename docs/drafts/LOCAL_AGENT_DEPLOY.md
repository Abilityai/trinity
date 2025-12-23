# Local Agent Deployment via MCP

> **Status**: Draft Requirements (Revised v2)
> **Created**: 2025-12-21
> **Author**: Claude (requirements analysis)

## Overview

Enable developers to deploy local Claude Code agents to a remote Trinity server using Trinity MCP. The local Claude Code packages the agent, sends it via MCP, and Trinity handles deployment. After deployment, the agent is immediately accessible through the same MCP connection.

## User Story

As a developer working on a local Claude Code agent, I want to deploy it to my Trinity server with a single command so that it runs in the cloud and I can interact with it via MCP alongside my other Trinity agents.

## End-to-End Scenario

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         Developer's Local Machine                            │
│                                                                              │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │                     Local Claude Code Session                         │  │
│  │                                                                       │  │
│  │  Developer: "Deploy this agent to Trinity"                           │  │
│  │                                                                       │  │
│  │  Claude Code:                                                         │  │
│  │    1. Validates template.yaml exists (Trinity-compatible check)      │  │
│  │    2. Reads local .env file for credentials                          │  │
│  │    3. Creates tar.gz archive of agent directory                      │  │
│  │    4. Base64 encodes the archive                                     │  │
│  │    5. Calls deploy_agent MCP tool with archive payload               │  │
│  │                                                                       │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│                                    │                                        │
│                                    │ MCP Protocol (SSE/stdio)              │
│                                    │ Connected to remote Trinity            │
└────────────────────────────────────┼────────────────────────────────────────┘
                                     │
                                     ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         Remote Trinity Server                                │
│                                                                              │
│  ┌────────────────────────────────────────────────────────────────────┐    │
│  │                     Trinity MCP Server (:8080)                      │    │
│  │                                                                     │    │
│  │  Receives: deploy_agent({archive, credentials, name})              │    │
│  │  Forwards to backend API                                           │    │
│  │                                                                     │    │
│  └────────────────────────────────────────────────────────────────────┘    │
│                                    │                                        │
│                                    │ POST /api/agents/deploy-archive       │
│                                    ▼                                        │
│  ┌────────────────────────────────────────────────────────────────────┐    │
│  │                     Trinity Backend (:8000)                         │    │
│  │                                                                     │    │
│  │  6. Decode and extract archive to temp directory                   │    │
│  │  7. Validate template.yaml (fail if missing/invalid)               │    │
│  │  8. Version handling (my-agent → my-agent-2 if exists)             │    │
│  │  9. Import credentials to Redis                                    │    │
│  │  10. Create agent container from extracted files                   │    │
│  │  11. Start agent and inject Trinity meta-prompt                    │    │
│  │  12. Return success with agent info                                │    │
│  │                                                                     │    │
│  └────────────────────────────────────────────────────────────────────┘    │
│                                    │                                        │
│                                    ▼                                        │
│  ┌────────────────────────────────────────────────────────────────────┐    │
│  │              Agent Container (agent-my-agent)                       │    │
│  │                                                                     │    │
│  │  - Running Claude Code with deployed agent code                    │    │
│  │  - Credentials injected from Redis                                 │    │
│  │  - Accessible via MCP tools (chat_with_agent, etc.)               │    │
│  │                                                                     │    │
│  └────────────────────────────────────────────────────────────────────┘    │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘

                    ═══════════════════════════════════════
                         AGENT NOW AVAILABLE VIA MCP
                    ═══════════════════════════════════════

Back on local machine, same Claude Code session can now:
  • chat_with_agent({agent_name: "my-agent", message: "Hello!"})
  • get_agent({name: "my-agent"})
  • get_agent_logs({agent_name: "my-agent"})
  • list_agents() → includes "my-agent"
```

---

## Key Architecture Insight

**The MCP server runs on the Trinity server, not locally.** This means:

- MCP tool cannot read the developer's local filesystem
- The archive must be passed **as a parameter** to the MCP tool
- Local Claude Code must package the agent **before** calling MCP

This is the critical difference from typical local tool use.

---

## Design Decisions

1. **Archive as parameter** - MCP tool receives base64 archive, not a path
2. **Client-side packaging** - Local Claude Code creates the tar.gz
3. **Trinity-compatible only** - Agent must have valid `template.yaml`
4. **Versioned deployment** - Same name creates new version, old one stopped
5. **Credential auto-import** - Extract from archive's `.env`, import to Redis
6. **Immediate availability** - Agent accessible via MCP right after deployment

---

## MCP Tool Design

### deploy_agent

```typescript
{
  name: "deploy_agent",
  description: "Deploy a Trinity-compatible agent from a base64-encoded archive. " +
    "The archive should be a tar.gz of the agent directory. " +
    "If agent name exists, creates new version (my-agent-2) and stops old one. " +
    "After deployment, agent is accessible via chat_with_agent and other tools.",
  parameters: z.object({
    archive: z.string().describe(
      "Base64-encoded tar.gz archive of the agent directory. " +
      "Must contain template.yaml at root level."
    ),
    credentials: z.record(z.string()).optional().describe(
      "Key-value pairs of credentials to import (e.g., from .env file)"
    ),
    name: z.string().optional().describe(
      "Agent name. Defaults to name from template.yaml"
    ),
  }),
  execute: async (args, context) => {
    // Forward to backend - MCP server just proxies the request
    const response = await trinityClient.deployArchive({
      archive: args.archive,
      credentials: args.credentials,
      name: args.name,
    });
    return response;
  }
}
```

**Note:** The MCP tool is thin - it just forwards the archive to the backend. All validation and processing happens server-side.

---

## What Local Claude Code Does

When the user says "Deploy this agent to Trinity", the local Claude Code should:

### Step 1: Validate Trinity Compatibility

```bash
# Check template.yaml exists
ls -la template.yaml

# Verify required fields
cat template.yaml | grep -E "^name:|^resources:"
```

If missing, inform user: "This agent is not Trinity-compatible. It needs a template.yaml file."

### Step 2: Extract Credentials

```bash
# Read .env if it exists
cat .env
```

Parse into key-value pairs for the credentials parameter.

### Step 3: Create Archive

```bash
# Create tar.gz excluding common large/unnecessary directories
tar -czf /tmp/agent-deploy.tar.gz \
  --exclude='.git' \
  --exclude='node_modules' \
  --exclude='__pycache__' \
  --exclude='.venv' \
  --exclude='venv' \
  --exclude='.env' \
  --exclude='*.pyc' \
  .

# Base64 encode
base64 -i /tmp/agent-deploy.tar.gz > /tmp/agent-deploy.b64
```

### Step 4: Call MCP Tool

```javascript
// Claude Code calls the MCP tool
deploy_agent({
  archive: "<base64 content from /tmp/agent-deploy.b64>",
  credentials: {
    "API_KEY": "sk-xxx",
    "SECRET_TOKEN": "abc123"
  },
  name: "my-agent"  // Optional, defaults to template.yaml name
})
```

### Step 5: Report Result

```
Agent deployed successfully!

Name: my-agent
Status: running
Version: my-agent-2 (previous version my-agent stopped)
Credentials imported: 2

You can now interact with it:
  • "Chat with my-agent about X"
  • "Check my-agent's status"
  • "View my-agent's logs"
```

---

## Backend API

### POST /api/agents/deploy-archive

**Request:**
```json
{
  "archive": "H4sIAAAAAAAAA+3OMQ7CMBCE0d6n...",
  "credentials": {
    "API_KEY": "sk-xxx",
    "SECRET": "my-secret"
  },
  "name": "my-agent"
}
```

**Response (Success):**
```json
{
  "status": "success",
  "agent": {
    "name": "my-agent-2",
    "status": "running",
    "port": 2290,
    "template": "uploaded:my-agent-2",
    "container_id": "abc123..."
  },
  "versioning": {
    "base_name": "my-agent",
    "previous_version": "my-agent",
    "previous_version_stopped": true,
    "new_version": "my-agent-2"
  },
  "credentials_imported": {
    "API_KEY": {"status": "created", "name": "API_KEY"},
    "SECRET": {"status": "reused", "name": "SECRET"}
  }
}
```

**Response (Error - Not Trinity Compatible):**
```json
{
  "status": "error",
  "error": "Agent is not Trinity-compatible: missing template.yaml",
  "code": "NOT_TRINITY_COMPATIBLE"
}
```

### Error Codes

| Code | HTTP Status | Description |
|------|-------------|-------------|
| `NOT_TRINITY_COMPATIBLE` | 400 | Missing or invalid template.yaml |
| `ARCHIVE_TOO_LARGE` | 413 | Archive exceeds 100MB limit |
| `INVALID_ARCHIVE` | 400 | Not a valid tar.gz or corrupt |
| `EXTRACTION_FAILED` | 500 | Failed to extract archive |

---

## Versioned Deployment

When deploying an agent with an existing name:

1. **Find existing agents** with same base name
2. **Stop the current version** (don't delete - preserve for rollback)
3. **Generate new version name**: `{base-name}-{n}`
4. **Create new agent** with versioned name
5. **Response includes** version info for user awareness

```python
def get_next_version_name(base_name: str) -> str:
    """Get next available version name for agent."""
    existing = get_agents_by_prefix(base_name)
    if not existing:
        return base_name

    # Find highest version number
    max_version = 1
    for agent in existing:
        if agent.name == base_name:
            max_version = max(max_version, 1)
        elif agent.name.startswith(f"{base_name}-"):
            try:
                v = int(agent.name.split("-")[-1])
                max_version = max(max_version, v)
            except ValueError:
                pass

    return f"{base_name}-{max_version + 1}"
```

---

## Credential Auto-Import

### Conflict Resolution

When importing credentials that may already exist:

| Scenario | Action |
|----------|--------|
| New credential | Create in Redis |
| Same name, same value | Reuse existing (no action) |
| Same name, different value | Create with suffix (e.g., `API_KEY_2`) |

```python
def import_credential(key: str, value: str, user_id: str) -> dict:
    """Import credential with conflict resolution."""
    existing = credential_manager.get_by_name(key, user_id)

    if existing:
        existing_value = credential_manager.get_secret(existing.id, user_id)
        if existing_value == value:
            return {"status": "reused", "name": key}
        else:
            new_key = find_unique_name(key, user_id)
            credential_manager.create(new_key, value, user_id)
            return {"status": "renamed", "original": key, "name": new_key}
    else:
        credential_manager.create(key, value, user_id)
        return {"status": "created", "name": key}
```

---

## Size Limits

| Limit | Value | Rationale |
|-------|-------|-----------|
| Archive size | 100 MB | Large enough for most agents |
| Credential count | 100 | Reasonable upper bound |
| File count in archive | 5000 | Prevent abuse |

---

## Security Considerations

1. **Archive Validation**
   - Size limit (100MB) prevents memory exhaustion
   - Path traversal prevention (no `../` in archive paths)
   - Temp directory cleanup after processing

2. **Credential Handling**
   - Credentials sent over HTTPS (MCP protocol)
   - Stored in Redis per existing credential_manager pattern
   - Never logged (only credential names logged, not values)

3. **Ownership**
   - Agent registered to authenticated user (via MCP API key)
   - Credentials stored under user's namespace

4. **Audit Logging**
   - Event type: `agent_management:deploy_archive`
   - Logs: user, agent_name, version, file_count, credential_count

---

## Implementation Plan

### Files to Modify

| File | Changes |
|------|---------|
| `src/backend/models.py` | Add `DeployArchiveRequest`, `DeployArchiveResponse` |
| `src/backend/routers/agents.py` | Add `POST /deploy-archive` endpoint |
| `src/backend/services/deploy_service.py` | New file: archive extraction, validation |
| `src/backend/credentials.py` | Add `import_with_conflict_resolution()` |
| `src/mcp-server/src/tools/agents.ts` | Add `deploy_agent` tool |
| `src/mcp-server/src/client.ts` | Add `deployAgent()` method |

### Backend Endpoint Pseudocode

```python
@router.post("/deploy-archive")
async def deploy_archive(body: DeployArchiveRequest, current_user: User):
    # 1. Decode base64 archive
    archive_bytes = base64.b64decode(body.archive)
    if len(archive_bytes) > MAX_ARCHIVE_SIZE:
        raise HTTPException(413, "ARCHIVE_TOO_LARGE")

    # 2. Extract to temp directory
    temp_dir = extract_tar_gz(archive_bytes)

    try:
        # 3. Validate Trinity-compatible
        template = validate_template(temp_dir)
        if not template:
            raise HTTPException(400, "NOT_TRINITY_COMPATIBLE")

        # 4. Determine version name
        base_name = body.name or template.get("name")
        version_name = get_next_version_name(base_name)

        # 5. Stop previous version if exists
        previous = find_previous_version(base_name)
        if previous and previous.status == "running":
            await stop_agent(previous.name)

        # 6. Import credentials to Redis
        cred_results = {}
        for key, value in (body.credentials or {}).items():
            result = import_credential(key, value, current_user.id)
            cred_results[key] = result

        # 7. Copy to uploaded templates dir
        template_path = f"config/uploaded-agents/{version_name}"
        shutil.copytree(temp_dir, template_path)

        # 8. Create agent via existing flow
        agent = await create_agent_internal(
            AgentConfig(name=version_name, template=f"uploaded:{version_name}"),
            current_user
        )

        # 9. Inject credentials to running agent
        await hot_reload_credentials(version_name)

        return DeployArchiveResponse(
            status="success",
            agent=agent,
            versioning={
                "base_name": base_name,
                "previous_version": previous.name if previous else None,
                "previous_version_stopped": previous is not None,
                "new_version": version_name
            },
            credentials_imported=cred_results
        )
    finally:
        # 10. Cleanup temp directory
        shutil.rmtree(temp_dir, ignore_errors=True)
```

### MCP Tool Implementation

```typescript
// src/mcp-server/src/tools/agents.ts

export const deployAgent = {
  name: "deploy_agent",
  description: `Deploy a Trinity-compatible agent from a base64-encoded tar.gz archive.
The archive must contain a template.yaml file at the root level.
If an agent with the same name exists, a new version is created and the old one is stopped.
After deployment, the agent is immediately accessible via chat_with_agent and other MCP tools.`,

  inputSchema: {
    type: "object",
    properties: {
      archive: {
        type: "string",
        description: "Base64-encoded tar.gz archive of the agent directory"
      },
      credentials: {
        type: "object",
        additionalProperties: { type: "string" },
        description: "Optional key-value pairs of credentials to import"
      },
      name: {
        type: "string",
        description: "Optional agent name (defaults to template.yaml name)"
      }
    },
    required: ["archive"]
  },

  async execute(args: DeployAgentArgs, context: ToolContext) {
    const response = await context.client.post("/api/agents/deploy-archive", {
      archive: args.archive,
      credentials: args.credentials,
      name: args.name
    });

    if (response.status === "success") {
      return {
        content: [{
          type: "text",
          text: formatDeploymentSuccess(response)
        }]
      };
    } else {
      throw new Error(response.error);
    }
  }
};

function formatDeploymentSuccess(response: DeployResponse): string {
  let msg = `Agent deployed successfully!\n\n`;
  msg += `Name: ${response.agent.name}\n`;
  msg += `Status: ${response.agent.status}\n`;

  if (response.versioning.previous_version) {
    msg += `Version: ${response.versioning.new_version} `;
    msg += `(previous ${response.versioning.previous_version} stopped)\n`;
  }

  const credCount = Object.keys(response.credentials_imported).length;
  if (credCount > 0) {
    msg += `Credentials imported: ${credCount}\n`;
  }

  msg += `\nThe agent is now accessible via MCP tools.`;
  return msg;
}
```

---

## Testing Plan

### Integration Tests

1. **Deploy new agent** - First deployment, no existing version
2. **Deploy with same name** - Creates version-2, stops original
3. **Deploy again** - Creates version-3, stops version-2
4. **Missing template.yaml** - Returns NOT_TRINITY_COMPATIBLE
5. **Invalid archive** - Returns INVALID_ARCHIVE
6. **Large archive** - Returns ARCHIVE_TOO_LARGE
7. **Credential import - new** - Creates in Redis
8. **Credential import - same value** - Reuses existing
9. **Credential import - different value** - Creates with suffix
10. **Post-deploy MCP access** - chat_with_agent works immediately

### Manual Testing

```bash
# 1. Create a Trinity-compatible test agent locally
mkdir /tmp/test-agent
cat > /tmp/test-agent/template.yaml << 'EOF'
name: test-deploy
display_name: Test Deploy Agent
description: Agent for testing deployment
resources:
  cpu: "2"
  memory: "4g"
EOF

cat > /tmp/test-agent/CLAUDE.md << 'EOF'
# Test Deploy Agent

This is a test agent deployed via MCP.
EOF

echo "API_KEY=test-key-123" > /tmp/test-agent/.env

# 2. In a local Claude Code session connected to Trinity MCP:
#    "Deploy the agent at /tmp/test-agent to Trinity"

# 3. Claude Code should:
#    - Create tar.gz of /tmp/test-agent
#    - Base64 encode it
#    - Call deploy_agent MCP tool
#    - Report success

# 4. Verify agent is accessible:
#    "Chat with test-deploy and ask it to introduce itself"
```

---

## Post-Deployment Experience

After successful deployment, the same local Claude Code session can immediately:

| Command | MCP Tool Used |
|---------|---------------|
| "Chat with my-agent about X" | `chat_with_agent` |
| "Check my-agent's status" | `get_agent` |
| "View my-agent's logs" | `get_agent_logs` |
| "List all my agents" | `list_agents` |
| "Stop my-agent" | `stop_agent` |
| "Delete my-agent" | `delete_agent` |

This creates a seamless developer experience where local development and remote deployment are unified through the same MCP interface.

---

## Non-Goals (Out of Scope)

1. **Auto-transformation** - Agent must already be Trinity-compatible
2. **Bidirectional sync** - One-way only (local → Trinity)
3. **Live reload** - Must redeploy for changes
4. **Multi-agent deployment** - Use System Manifest for that
5. **Dependency installation** - Agent must be self-contained in archive

---

## Related Documentation

- [Agent Lifecycle](../memory/feature-flows/agent-lifecycle.md) - How agents are created
- [Credential Injection](../memory/feature-flows/credential-injection.md) - How credentials work
- [Trinity Compatible Agent Guide](TRINITY_COMPATIBLE_AGENT_GUIDE.md) - Required structure
- [MCP Orchestration](../memory/feature-flows/mcp-orchestration.md) - Available MCP tools

---

## Revision History

| Date | Changes |
|------|---------|
| 2025-12-21 | Initial requirements draft |
| 2025-12-21 | v2: Fixed architecture - archive as MCP parameter, not path. Added end-to-end scenario, post-deployment experience. |
