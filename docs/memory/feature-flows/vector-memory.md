# Feature: Agent Vector Memory (Chroma + MCP)

> **STATUS: ❌ REMOVED (2025-12-24)**
>
> This feature has been removed from the platform. Agents that need vector memory should include it in their templates.
>
> **Reason**: Development workflow parity - local dev should equal production. Platform-injected capabilities create mismatches between local Claude Code development and Trinity deployment.
>
> **Alternative**: Templates can add chromadb/sentence-transformers as dependencies and configure their own .mcp.json with a chroma server.
>
> This document is kept for historical reference only.

---

## Overview (HISTORICAL)

Per-agent Chroma vector database with auto-configured MCP server for semantic memory storage and retrieval. Agents can use `mcp__chroma__*` tools directly without writing Python code.

**Requirements**: 10.4 Agent Vector Memory + 10.5 Chroma MCP Server
**Status**: ❌ REMOVED (2025-12-24)
**Priority**: Medium/High
**Last Updated**: 2025-12-24

## User Story

As an agent developer, I want my agents to have semantic memory so that they can store knowledge and retrieve relevant information by similarity search.

## Architecture

```
Agent Container
├── /home/developer/
│   ├── vector-store/                   # Chroma data directory
│   │   └── chroma.sqlite3              # Persistence file
│   ├── .mcp.json                       # MCP config with chroma server
│   ├── .trinity/
│   │   ├── prompt.md                   # Trinity system prompt
│   │   └── vector-memory.md            # Usage documentation
│   └── CLAUDE.md                       # Includes vector memory section
│
├── /trinity-meta-prompt/               # Read-only mount from host
│   ├── prompt.md
│   ├── commands/
│   └── vector-memory.md                # Source documentation
```

**Components**:
- **chroma-mcp**: MCP server exposing 12 vector operation tools
- **chromadb**: Vector database with persistent storage
- **sentence-transformers**: Embedding model library (auto-configured by chroma-mcp)

## Entry Points

### Injection API
- **API**: `POST /api/trinity/inject` (agent-server)
- **Trigger**: Backend calls after agent container starts

### Status Check
- **API**: `GET /api/trinity/status` (agent-server)
- **Returns**: Includes `vector_memory` object with directory and docs status

## Base Image Layer

### Dockerfile
**File**: `docker/base-image/Dockerfile`

```dockerfile
# Python packages (lines 64-76)
RUN python3 -m pip install --user \
    ...
    chromadb \
    sentence-transformers \
    chroma-mcp

# Pre-download model (lines 78-79)
RUN python3 -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"
```

**Impact**:
- Image size: +800MB (PyTorch CPU + model)
- Build time: +5-10 minutes (model download)

## Agent Server Layer

### Configuration
**File**: `docker/base-image/agent_server/config.py:25`

```python
VECTOR_STORE_DIR = WORKSPACE_DIR / "vector-store"
```

### Models
**File**: `docker/base-image/agent_server/models.py:193-200`

```python
class TrinityStatusResponse(BaseModel):
    ...
    vector_memory: Dict[str, bool] = {}  # Vector memory status (line 199)
```

### Injection Logic
**File**: `docker/base-image/agent_server/routers/trinity.py`

**Status Check** (lines 46-60):
```python
# Vector memory status
vector_memory = {
    "vector-store": (workspace / "vector-store").exists(),
    ".trinity/vector-memory.md": (workspace / ".trinity" / "vector-memory.md").exists(),
}

# Check if chroma MCP is configured in .mcp.json
mcp_json_path = workspace / ".mcp.json"
chroma_mcp_configured = False
if mcp_json_path.exists():
    mcp_config = json.loads(mcp_json_path.read_text())
    chroma_mcp_configured = "chroma" in mcp_config.get("mcpServers", {})
vector_memory["chroma_mcp_configured"] = chroma_mcp_configured
```

**Injection** (lines 150-162):
```python
# 4. Create vector-store directory for Chroma persistence
vector_store_path = workspace / "vector-store"
vector_store_path.mkdir(parents=True, exist_ok=True)
directories_created.append("vector-store")

# 5. Copy vector memory documentation
vector_docs_src = TRINITY_META_PROMPT_DIR / "vector-memory.md"
vector_docs_dst = trinity_dir / "vector-memory.md"
if vector_docs_src.exists():
    shutil.copy2(vector_docs_src, vector_docs_dst)
    files_created.append(".trinity/vector-memory.md")
```

**Chroma MCP Config Injection** (lines 164-188):
```python
# 6. Inject chroma MCP server into .mcp.json
mcp_json_path = workspace / ".mcp.json"
if mcp_json_path.exists():
    mcp_config = json.loads(mcp_json_path.read_text())
else:
    mcp_config = {"mcpServers": {}}

if "chroma" not in mcp_config["mcpServers"]:
    mcp_config["mcpServers"]["chroma"] = CHROMA_MCP_CONFIG
    mcp_json_path.write_text(json.dumps(mcp_config, indent=2))
```

**CLAUDE.md Update** (lines 228-232 within trinity_section):
```markdown
### Vector Memory

You have a Chroma MCP server configured for semantic memory storage.
Use `mcp__chroma__*` tools to store and query by similarity.
Data persists at `/home/developer/vector-store/`.
```

## Meta-Prompt Configuration

### Vector Memory Documentation
**File**: `config/trinity-meta-prompt/vector-memory.md`

Comprehensive documentation covering:
- Quick start with code examples
- Store, query, update, delete operations
- Metadata filtering
- Best practices
- EpisodicMemory class example
- Troubleshooting

## Data Layer

### Storage
- **Path**: `/home/developer/vector-store/`
- **Format**: SQLite (Chroma's persistence format)
- **Persistence**: Survives container restarts (Docker volume)
- **Git Sync**: Syncs to GitHub if git sync enabled

### Isolation
- Each agent has its own vector store
- No cross-agent access by default
- Could be shared via shared folders feature

## Usage Examples

### MCP Tools (Recommended)

Agents can use chroma MCP tools directly:

```
# Store a memory
mcp__chroma__chroma_add_documents(
    collection_name="memory",
    documents=["The user prefers Python"],
    metadatas=[{"type": "preference"}],
    ids=["pref-001"]
)

# Query by similarity
mcp__chroma__chroma_query_documents(
    collection_name="memory",
    query_texts=["programming languages"],
    n_results=5
)

# List collections
mcp__chroma__chroma_list_collections()
```

### Python API (Still Available)

For advanced use cases, Python API remains available:

```python
import chromadb
from chromadb.utils import embedding_functions

client = chromadb.PersistentClient(path="/home/developer/vector-store")
ef = embedding_functions.SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")
collection = client.get_or_create_collection("memory", embedding_function=ef)
```

## Testing

### Prerequisites
- [ ] Base image rebuilt with chroma-mcp package
- [ ] Agent created and running
- [ ] Trinity injection completed

### Test Steps

#### 1. Verify chroma-mcp Installation
**Action**: Check chroma-mcp is installed
```bash
docker exec agent-{name} python3 -m chroma_mcp --help
```
**Expected**: Shows chroma-mcp usage help

#### 2. Verify MCP Config Injection
**Action**: Check .mcp.json has chroma server
```bash
docker exec agent-{name} cat /home/developer/.mcp.json | jq '.mcpServers.chroma'
```
**Expected**: Shows chroma MCP server config with persistent client

#### 3. Verify Status API
**Action**: Check trinity status includes chroma_mcp_configured
```bash
curl http://localhost:8000/api/agents/{name}/trinity/status
```
**Expected**: `vector_memory.chroma_mcp_configured: true`

#### 4. Test via Agent Chat
**Action**: Ask agent to use vector memory
```
Store the fact that "the sky is blue" in your vector memory, then query for "color of sky"
```
**Expected**: Agent uses `chroma_add_documents` and `chroma_query_documents` tools

#### 5. Test Persistence
**Action**: Stop and start agent, verify data persists
```bash
curl -X POST http://localhost:8000/api/agents/{name}/stop
curl -X POST http://localhost:8000/api/agents/{name}/start
```
Then ask agent to query for previously stored data.

### Edge Cases
- [ ] First use may be slower (embedding model initialization)
- [ ] Empty collection returns empty results
- [ ] MCP tools handle errors gracefully

### Cleanup
```bash
docker exec agent-{name} rm -rf /home/developer/vector-store/*
```

**Last Tested**: 2025-12-13
**Tested By**: claude
**Status**: 🚧 Ready for testing (requires base image rebuild)

## Error Handling

| Error | Cause | Resolution |
|-------|-------|------------|
| `ModuleNotFoundError: chromadb` | Base image not rebuilt | Rebuild with `build-base-image.sh` |
| `Connection refused` | Vector store path wrong | Use `/home/developer/vector-store` |
| Slow first query | Model loading | Normal - subsequent queries fast |
| Out of disk | Vector store growth | Clean old data or expand volume |

## Security Considerations

- **Isolation**: Each agent has separate vector store
- **Persistence**: Data in Docker volume (not encrypted)
- **Access**: No cross-agent access by default
- **Credentials**: No sensitive data in embeddings

## Performance

| Metric | Value |
|--------|-------|
| Model size | ~80MB (cached in image) |
| Memory usage | ~300MB when loaded |
| First query | 2-5 seconds (model load) |
| Subsequent queries | 10-50ms |
| Embedding speed | ~100 docs/second |

## Related Flows

- **Upstream**: Trinity Injection (`trinity.py`)
- **Downstream**: Could feed into Memory Folding (Phase 10)
- **Related**: Shared Folders for cross-agent memory

## Files Reference

| File | Purpose |
|------|---------|
| `docker/base-image/Dockerfile` | Package installation (chromadb, sentence-transformers, chroma-mcp) |
| `docker/base-image/agent_server/config.py` | VECTOR_STORE_DIR constant |
| `docker/base-image/agent_server/routers/trinity.py` | Injection logic + chroma MCP config |
| `config/trinity-meta-prompt/vector-memory.md` | Usage documentation |
| `docs/memory/requirements.md` | Requirements 10.4 + 10.5 |
