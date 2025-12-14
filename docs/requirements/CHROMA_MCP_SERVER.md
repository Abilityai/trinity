# Requirement: Chroma MCP Server Integration

## Overview

Integrate the official [Chroma MCP Server](https://github.com/chroma-core/chroma-mcp) into Trinity agent containers, enabling agents to use vector memory through standard MCP tools instead of writing Python code.

**Requirement ID**: 10.5
**Priority**: High
**Depends On**: 10.4 (Agent Vector Memory with Chroma) - Already implemented

## Current State

- Agents have Chroma + sentence-transformers installed (Req 10.4)
- Vector store directory at `/home/developer/vector-store/`
- Agents must write Python code to use Chroma
- No MCP tools for vector operations

## Proposed Solution

Install and auto-configure the official `chroma-mcp` server in agent containers, giving agents 12 MCP tools for vector memory operations.

### MCP Tools Available

**Collection Management (7 tools)**:
| Tool | Description |
|------|-------------|
| `chroma_list_collections` | List all collections in the database |
| `chroma_create_collection` | Create a new collection |
| `chroma_peek_collection` | Preview documents in a collection |
| `chroma_get_collection_info` | Get collection metadata |
| `chroma_get_collection_count` | Get document count |
| `chroma_modify_collection` | Update collection settings |
| `chroma_delete_collection` | Delete a collection |

**Document Operations (5 tools)**:
| Tool | Description |
|------|-------------|
| `chroma_add_documents` | Add documents with embeddings |
| `chroma_query_documents` | Semantic search by similarity |
| `chroma_get_documents` | Retrieve documents by ID or filter |
| `chroma_update_documents` | Update existing documents |
| `chroma_delete_documents` | Delete documents |

### Agent Usage Example

Instead of writing Python code, agents can use MCP tools directly:

```
# Store a memory
mcp__chroma__chroma_add_documents(
    collection_name="memory",
    documents=["User prefers Python over JavaScript"],
    ids=["pref-001"],
    metadatas=[{"type": "preference"}]
)

# Query by similarity
mcp__chroma__chroma_query_documents(
    collection_name="memory",
    query_texts=["programming language preferences"],
    n_results=5
)
```

## Implementation

### 1. Base Image Updates

**File**: `docker/base-image/Dockerfile`

```dockerfile
# Install chroma-mcp server (add after existing pip install)
RUN python3 -m pip install --user chroma-mcp
```

### 2. Auto-Configure MCP Server

**File**: `docker/base-image/agent_server/routers/trinity.py`

During Trinity injection, add chroma-mcp to the agent's `.mcp.json`:

```json
{
  "mcpServers": {
    "chroma": {
      "command": "python3",
      "args": ["-m", "chroma_mcp", "--client-type", "persistent", "--data-dir", "/home/developer/vector-store"],
      "env": {}
    }
  }
}
```

### 3. Update Trinity Injection

Modify `inject_trinity()` to:
1. Read existing `.mcp.json` (if any)
2. Add/update the `chroma` MCP server entry
3. Write back the merged config

### 4. Update Documentation

Update `.trinity/vector-memory.md` to show MCP tool usage instead of Python code.

## Configuration Options

### Embedding Function

The default embedding function uses sentence-transformers (already installed). For other providers:

| Provider | Env Var Required |
|----------|------------------|
| Default (sentence-transformers) | None |
| OpenAI | `OPENAI_API_KEY` |
| Cohere | `COHERE_API_KEY` |
| Jina | `JINA_API_KEY` |

### Data Persistence

- **Path**: `/home/developer/vector-store/`
- **Client Type**: `persistent`
- Survives container restarts
- Syncs to GitHub if git sync enabled

## Acceptance Criteria

- [ ] `chroma-mcp` package installed in base image
- [ ] MCP server auto-configured during Trinity injection
- [ ] Agents can use `mcp__chroma__*` tools without setup
- [ ] Data persists at `/home/developer/vector-store/`
- [ ] Works with pre-installed sentence-transformers embeddings
- [ ] Documentation updated with MCP tool examples
- [ ] Existing Python API still works (backward compatible)

## Testing

### 1. Verify Installation
```bash
docker exec agent-{name} python3 -m chroma_mcp --help
```

### 2. Verify MCP Config
```bash
docker exec agent-{name} cat /home/developer/.mcp.json | jq '.mcpServers.chroma'
```

### 3. Test via Agent Chat
Ask agent to:
```
Store the fact that "the sky is blue" in your vector memory, then query for "color of sky"
```

Expected: Agent uses `chroma_add_documents` and `chroma_query_documents` tools.

## File Changes

| File | Change |
|------|--------|
| `docker/base-image/Dockerfile` | Add `chroma-mcp` to pip install |
| `docker/base-image/agent_server/routers/trinity.py` | Auto-inject chroma MCP config |
| `config/trinity-meta-prompt/vector-memory.md` | Update with MCP tool examples |
| `.claude/memory/requirements.md` | Add requirement 10.5 |

## Rollout

1. Rebuild base image: `./scripts/deploy/build-base-image.sh`
2. Restart agents to pick up new packages
3. New/restarted agents automatically get chroma MCP configured

## Sources

- [Official Chroma MCP Server](https://github.com/chroma-core/chroma-mcp)
- [MCP Servers Registry](https://github.com/modelcontextprotocol/servers)
- [Chroma MCP on PulseMCP](https://www.pulsemcp.com/servers/chroma)
