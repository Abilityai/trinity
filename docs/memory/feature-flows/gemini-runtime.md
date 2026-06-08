# Gemini CLI Runtime Integration

**Status**: ✅ Implemented
**Date**: 2025-12-28
**Priority**: High

---

## Problem Statement

Trinity was originally built for Claude Code only. To support cost optimization and provider flexibility, we needed to add Gemini CLI as an alternative runtime while maintaining feature parity.

---

## Implementation Summary

| Priority | Item | Status | Date |
|----------|------|--------|------|
| 1 | MCP injection for Gemini | ✅ Done | 2025-12-28 |
| 2 | Complete `configure_mcp` | ✅ Done | 2025-12-28 |
| 3 | Instruction file docs | ✅ Done | 2025-12-28 |
| 4 | Tool name mapping | ⏸️ Deferred | N/A |

---

## 1. Instruction File Name

### Decision: Keep `CLAUDE.md` (Option C)

Both Claude Code and Gemini CLI read agent instructions from `CLAUDE.md`.

**Rationale**:
- Backward compatibility with existing agents
- Both runtimes understand markdown instruction files
- Renaming would break existing templates without benefit

Documented in [GEMINI_SUPPORT.md](../../GEMINI_SUPPORT.md).

---

## 2. MCP Injection - Runtime Aware

### Status: ✅ Implemented

**Files Updated**:
- `docker/base-image/agent_server/services/trinity_mcp.py`

**Implementation**:
```python
def inject_trinity_mcp_if_configured() -> bool:
    """Inject Trinity MCP server - runtime aware."""
    runtime = os.getenv("AGENT_RUNTIME", "claude-code")

    if runtime == "gemini-cli":
        return _inject_gemini_mcp(url, key)  # gemini mcp add
    else:
        return _inject_claude_mcp(url, key)  # .mcp.json
```

**New functions**:
- `_inject_claude_mcp()` - writes to `.mcp.json`
- `_inject_gemini_mcp()` - uses `gemini mcp add` command
- `configure_mcp_servers()` - shared runtime-aware MCP config
- `_configure_claude_mcp_servers()` - Claude-specific
- `_configure_gemini_mcp_servers()` - Gemini-specific

---

## 3. Gemini MCP Configuration

### Status: ✅ Implemented

**Files Updated**:
- `docker/base-image/agent_server/services/gemini_runtime.py`

`GeminiRuntime.configure_mcp()` now delegates to shared `_configure_gemini_mcp_servers()` function for consistency.

---

## 4. Tool Name Mapping

### Status: ⏸️ Deferred

No action needed - both runtimes have equivalent built-in tools:

| Generic Name | Claude Code | Gemini CLI |
|--------------|-------------|------------|
| filesystem | Read, Write, Edit | read_file, write_file, replace |
| shell | Bash | run_shell_command |
| web_search | WebSearch | google_web_search |
| memory | Task | save_memory |

The `tools` array in templates is informational only.

---

## Testing Checklist

- [x] Gemini agent can use Trinity MCP (via `gemini mcp add`)
- [ ] Gemini agent can delegate to other agents (needs testing)
- [x] Custom MCP servers work with Gemini agents
- [x] Template MCP configurations apply correctly
- [ ] Vector memory (Chroma MCP) works with Gemini (needs testing)

---

## Key Implementation Files

| Layer | File | Purpose |
|-------|------|---------|
| **Runtime Adapter** | `docker/base-image/agent_server/services/runtime_adapter.py` | Abstract interface |
| **Gemini Runtime** | `docker/base-image/agent_server/services/gemini_runtime.py` | Gemini CLI execution |
| **Claude Runtime** | `docker/base-image/agent_server/services/claude_code.py` | Claude Code execution |
| **MCP Injection** | `docker/base-image/agent_server/services/trinity_mcp.py` | Runtime-aware MCP config |
| **Agent Config** | `src/backend/models.py` | `runtime` field in AgentConfig |
| **Agent Creation** | `src/backend/routers/agents.py` | Injects AGENT_RUNTIME env var |

---

## Error Handling

- **Pipe-drop (`BrokenPipeError` / `ConnectionResetError`) in `execute_headless`**: raised as HTTP 502, not 500. This keeps pipe-drops out of the `agent_client.py` circuit-breaker failure counter — 4xx/5xx/502/503/504 are treated as application errors and skip the failure increment (#474/#873).

---

## Revision History

| Date | Change |
|------|--------|
| 2026-05-18 | Pipe-drop reclassification (#474/#873): `BrokenPipeError`/`ConnectionResetError` in `execute_headless` now raise HTTP 502 instead of 500, preventing false circuit-breaker trips when the Gemini child process exits early. |

---

## Related Documentation

- [Gemini Support Guide](../../GEMINI_SUPPORT.md) - User-facing setup guide
- [Trinity Compatible Agent Guide](../../TRINITY_COMPATIBLE_AGENT_GUIDE.md) - Template configuration
- [Multi-Runtime Architecture](../requirements.md#12-multi-runtime-support) - Requirements
- [Delegation Best Practices](../../MULTI_AGENT_SYSTEM_GUIDE.md#delegation-best-practices) - MCP vs runtime sub-agents

