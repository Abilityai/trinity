# Remove Chroma Vector Store Platform Injection

**Status**: ✅ Implemented (2025-12-24)
**Created**: 2025-12-24
**Reason**: Development workflow parity - agents should be self-contained. Platform-injected capabilities create mismatch between local development and Trinity production.

## Decision

Remove all platform-level Chroma vector store injection. Agents that need vector memory should include it in their templates. The platform provides infrastructure (containers, networking, credentials), not agent capabilities (memory, specific tools).

## Guiding Principle

**Local dev == Production**

Whatever an agent has locally should be exactly what it has on Trinity. Templates are the complete definition of an agent.

---

## Changes Required

### 1. Base Image (`docker/base-image/Dockerfile`)

**Action**: Remove Chroma-related packages from pip install

**Location**: Lines ~64-76 (pip install block)

**Remove**:
```dockerfile
chromadb \
sentence-transformers \
chroma-mcp
```

**Also Remove**: Model pre-download step (~lines 78-79)
```dockerfile
RUN python3 -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"
```

**Impact**: Base image size reduced by ~800MB

---

### 2. Agent Server Config (`docker/base-image/agent_server/config.py`)

**Action**: Remove VECTOR_STORE_DIR constant

**Location**: Line ~25

**Remove**:
```python
VECTOR_STORE_DIR = WORKSPACE_DIR / "vector-store"
```

---

### 3. Agent Server Models (`docker/base-image/agent_server/models.py`)

**Action**: Remove vector_memory from TrinityStatusResponse

**Location**: Lines ~193-200

**Remove**:
```python
vector_memory: Dict[str, bool] = {}  # Vector memory status
```

---

### 4. Agent Server Trinity Router (`docker/base-image/agent_server/routers/trinity.py`)

**Action**: Remove all vector memory injection logic

**Locations**:

a) **Status check** (lines ~46-60): Remove vector_memory status block
```python
# Remove entire vector memory status section
vector_memory = {
    "vector-store": ...
    ...
}
vector_memory["chroma_mcp_configured"] = chroma_mcp_configured
```

b) **Injection - directory creation** (lines ~150-154): Remove vector-store directory creation
```python
# Remove:
vector_store_path = workspace / "vector-store"
vector_store_path.mkdir(parents=True, exist_ok=True)
directories_created.append("vector-store")
```

c) **Injection - docs copy** (lines ~156-162): Remove vector memory docs copy
```python
# Remove:
vector_docs_src = TRINITY_META_PROMPT_DIR / "vector-memory.md"
vector_docs_dst = trinity_dir / "vector-memory.md"
if vector_docs_src.exists():
    shutil.copy2(vector_docs_src, vector_docs_dst)
    files_created.append(".trinity/vector-memory.md")
```

d) **Injection - MCP config** (lines ~164-188): Remove chroma MCP injection
```python
# Remove entire chroma MCP config injection block
if "chroma" not in mcp_config["mcpServers"]:
    mcp_config["mcpServers"]["chroma"] = CHROMA_MCP_CONFIG
    ...
```

e) **CLAUDE.md section** (lines ~228-232): Remove vector memory section from trinity_section string
```markdown
# Remove from trinity_section:
### Vector Memory

You have a Chroma MCP server configured for semantic memory storage.
Use `mcp__chroma__*` tools to store and query by similarity.
Data persists at `/home/developer/vector-store/`.
```

f) **CHROMA_MCP_CONFIG constant**: Remove the constant definition if it exists

---

### 5. Trinity Meta-Prompt (`config/trinity-meta-prompt/`)

**Action**: Delete vector-memory.md file

**File**: `config/trinity-meta-prompt/vector-memory.md`

**Action**: DELETE entire file

---

### 6. Requirements Documentation (`docs/memory/requirements.md`)

**Action**: Update sections 10.4 and 10.5 to REMOVED status

**Location**: Section 10.4 Agent Vector Memory (~line 825)

**Change to**:
```markdown
#### 10.4 Agent Vector Memory (Chroma)
- **Status**: ❌ REMOVED (2025-12-24)
- **Reason**: Development workflow parity - agents should be self-contained. Templates that need vector memory should include it themselves.
- **What was removed**:
  - chromadb, sentence-transformers, chroma-mcp from base image
  - Vector store directory creation during Trinity injection
  - Chroma MCP config injection into .mcp.json
  - vector-memory.md documentation
  - Vector memory section in injected CLAUDE.md
- **Alternative**: Templates can include vector memory dependencies and configuration. See reference templates for examples.
```

**Location**: Section 10.5 Chroma MCP Server Integration (~line 853)

**Change to**:
```markdown
#### 10.5 Chroma MCP Server Integration
- **Status**: ❌ REMOVED (2025-12-24)
- **Reason**: Removed along with 10.4 - platform should not inject agent capabilities
```

---

### 7. Feature Flows Index (`docs/memory/feature-flows.md`)

**Action**: Update vector-memory.md entry to show removed

**Location**: Line with vector-memory.md entry

**Change**:
```markdown
| ~~Agent Vector Memory~~ | ~~Medium~~ | ~~vector-memory.md~~ | ❌ REMOVED (2025-12-24) - Templates should define their own memory |
```

---

### 8. Feature Flow Document (`docs/memory/feature-flows/vector-memory.md`)

**Action**: Mark as removed (keep for historical reference)

**Add at top of file**:
```markdown
> **STATUS: ❌ REMOVED (2025-12-24)**
>
> This feature has been removed from the platform. Agents that need vector memory should include it in their templates.
>
> **Reason**: Development workflow parity - local dev should equal production. Platform-injected capabilities create mismatches.
>
> **Alternative**: Templates can add chromadb/lancedb as dependencies and configure their own .mcp.json.
>
> This document is kept for historical reference only.

---

# (Original content below, for reference)
```

---

### 9. Architecture Documentation (`docs/memory/architecture.md`)

**Action**: Check for and remove any vector memory references

**Search for**: "vector", "chroma", "embedding"

**Expected locations**:
- Agent container file structure diagram
- Any mentions of vector-store directory

---

### 10. Roadmap (`docs/memory/roadmap.md`)

**Action**: Update Phase 10 items

**Location**: Phase 10 section

**Update**:
```markdown
| ✅ | ~~Agent Vector Memory (Chroma)~~ | ❌ REMOVED - Templates define own memory | ~~MEDIUM~~ |
| ✅ | ~~Chroma MCP Server~~ | ❌ REMOVED - Templates define own memory | ~~HIGH~~ |
```

---

### 11. Main CLAUDE.md (`CLAUDE.md`)

**Action**: Check for any vector memory references

**Search for**: "vector", "chroma"

**Remove if found**: Any references to platform-provided vector memory

---

### 12. Tests

**Action**: Find and remove/update any vector memory tests

**Search locations**:
- `tests/` directory
- `docker/base-image/agent_server/` for any test files

**Commands to find**:
```bash
grep -r "vector" tests/
grep -r "chroma" tests/
grep -r "vector_memory" tests/
```

---

### 13. Changelog (`docs/memory/changelog.md`)

**Action**: Add removal entry

**Add at top**:
```markdown
### 2025-12-24 HH:MM:SS
❌ **Removed Platform Chroma Vector Store Injection**

Removed all platform-level vector memory injection for development workflow parity.

**Reason**: Local dev should equal production. Platform-injected capabilities create mismatches between local Claude Code development and Trinity deployment.

**What was removed**:
- `chromadb`, `sentence-transformers`, `chroma-mcp` from base image (~800MB savings)
- Vector store directory creation during Trinity injection
- Chroma MCP config injection into agent .mcp.json
- `vector-memory.md` documentation file
- Vector memory section in injected CLAUDE.md
- `vector_memory` status field from Trinity status API

**Alternative**: Templates that need vector memory should include dependencies and configuration themselves. This ensures local development matches production exactly.

**Files changed**:
- `docker/base-image/Dockerfile` - Removed packages and model download
- `docker/base-image/agent_server/config.py` - Removed VECTOR_STORE_DIR
- `docker/base-image/agent_server/models.py` - Removed vector_memory field
- `docker/base-image/agent_server/routers/trinity.py` - Removed injection logic
- `config/trinity-meta-prompt/vector-memory.md` - Deleted
- `docs/memory/requirements.md` - Updated 10.4, 10.5 to REMOVED
- `docs/memory/feature-flows/vector-memory.md` - Marked as removed
- `docs/memory/roadmap.md` - Updated Phase 10
```

---

## Verification Checklist

After all changes:

- [ ] Base image rebuilds successfully: `./scripts/deploy/build-base-image.sh`
- [ ] Base image size reduced (~800MB less)
- [ ] Agent creation works without errors
- [ ] Trinity injection completes without vector memory references
- [ ] `GET /api/agents/{name}/trinity/status` returns without `vector_memory` field
- [ ] No "chroma" in injected `.mcp.json`
- [ ] No "vector-memory.md" in `.trinity/` directory
- [ ] No "Vector Memory" section in injected CLAUDE.md
- [ ] All tests pass
- [ ] Documentation is consistent

---

### 14. README.md

**Action**: Check for and remove any vector memory references

**Search for**: "vector", "chroma"

---

### 15. System Agent Service (`src/backend/services/system_agent_service.py`)

**Action**: Check for any vector memory references

**Search for**: "vector", "chroma"

---

### 16. Trinity Meta-Prompt (`config/trinity-meta-prompt/prompt.md`)

**Action**: Remove any vector memory references from the main prompt

**Search for**: "vector", "chroma", "memory"

---

### 17. Multi-Agent System Guide (`docs/MULTI_AGENT_SYSTEM_GUIDE.md`)

**Action**: Remove or update vector memory references

**Search for**: "vector", "chroma"

---

### 18. Trinity Compatible Agent Guide (`docs/TRINITY_COMPATIBLE_AGENT_GUIDE.md`)

**Action**: Remove or update vector memory references

**Search for**: "vector", "chroma"

---

### 19. Chroma MCP Server Requirements (`docs/requirements/CHROMA_MCP_SERVER.md`)

**Action**: DELETE entire file (obsolete requirements doc)

---

### 20. Draft Documents (`docs/drafts/`)

**Action**: Check and update these files:
- `SYSTEM_MANIFEST_SIMPLIFIED.md`
- `SYSTEM_DESCRIPTOR_IMPLEMENTATION.md`
- `SYSTEM_DESCRIPTOR_ARCHITECTURE.md`

**Search for**: "vector", "chroma"

---

### 21. Testing Phase Document (`docs/testing/phases/PHASE_13_SETTINGS.md`)

**Action**: Remove any vector memory test steps

**Search for**: "vector", "chroma"

---

### 22. Autonomous Agent Demos (`docs/memory/autonomous-agent-demos.md`)

**Action**: Check for vector memory references

**Search for**: "vector", "chroma"

---

## Post-Removal: Reference Template (Optional)

Consider creating a reference template showing how agents CAN add vector memory:

`config/agent-templates/with-vector-memory/` containing:
- `template.yaml` with chromadb/lancedb dependency
- `.mcp.json.template` with chroma server config
- `.claude/memory-guide.md` with usage docs

This demonstrates the "template defines capabilities" pattern without platform injection.
