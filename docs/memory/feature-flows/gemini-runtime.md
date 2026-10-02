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

## 5. CLI Argument Surface and Chat Session Continuity (#2971)

### Status: ✅ Implemented (2026-10-01)

**Problem**: the base image installed `@google/gemini-cli` unpinned. The release it took has a strict argument parser with no `--system-prompt` and no `--max-turns`, so every headless run (the backend always sends a composed system prompt) exited during argument parsing, and every turn hit the trusted-folder check (exit 55, `--yolo` silently downgraded to `default`). Chat turns also used a bare `--resume`, which takes the newest session in a store headless runs write to as well — the #2958 class, fixed for Claude only.

**Files**:
- `docker/base-image/Dockerfile` — `npm install -g @google/gemini-cli@0.62.0` (pinned), and after the agent server is copied in: `RUN python3 /app/agent_server/services/gemini_cli_args.py --smoke`
- `docker/base-image/agent_server/services/gemini_cli_args.py` — stdlib-only; builds every argv the runtime spawns (`build_chat_argv`, `build_headless_argv`), `compose_prompt`, `GEMINI_CLI_VERSION`, and the build-time `smoke()`
- `docker/base-image/agent_server/services/gemini_runtime.py` — `execute()` / `_execute_chat_once()` / `execute_headless()` consume the builders

**Argument contract** (verified against gemini-cli 0.62.0):

| Concern | Before | Now |
|---------|--------|-----|
| System prompt | `--system-prompt <text>` (rejected) | Prepended to the turn input via `compose_prompt(system_prompt, prompt)` — `"{system}\n\n---\n\n{prompt}"`, the Codex pattern. `GEMINI_SYSTEM_MD` was rejected: it *replaces* the CLI's built-in tool-use/safety prompt. |
| Turn cap | `--max-turns N` (rejected) | Logged, not enforced — gemini-cli has no per-run cap (`model.maxSessionTurns` is a settings-file value shared by every concurrent run). The wall-clock `timeout_seconds` bounds the run. |
| Workspace trust | none (exit 55) | `--skip-trust` on every spawn, scoped to the spawn's cwd (the agent home). A flag rather than `GEMINI_CLI_TRUST_WORKSPACE` so a rename fails the smoke instead of silently reverting to untrusted. |
| Chat continuity | bare `--resume` (= newest session) | `--resume <agent_state.chat_session_id>` only |
| Headless | `--allowed-tools <t>` per tool | unchanged; never resumes |

**Chat session rules** (mirror the Claude path, #2958): the id is captured from the stream's `init.session_id` **after a successful turn** and only when `agent_state.chat_session_generation` is unchanged (a `reset_session()` during the turn drops it); a model change (`chat_session_model != model`) starts fresh; a failed turn does not move the id; a resumed session the CLI cannot find (exit 42, stderr `Error resuming session`, no response parts) raises `_ResumeMissing` and gets **one** cold retry with the same `execution_id` — re-registered as pending first so a cancel landing in the gap is honoured and the retry is skipped. Gemini sessions live under `~/.gemini/tmp/`, outside the JSONL reaper, so no chat-session marker file is written.

**Transcript hygiene**: stream-json echoes the whole stdin as the turn's `{"type":"message","role":"user"}` event. The chat reader rewrites that event's `content` back to the caller's `prompt` before appending to `raw_messages`, so the execution log the backend persists never carries the composed platform prompt (collaborators, stakeholders, custom instructions) as the user's turn. The model still receives the full input.

**Build-time smoke**: `gemini_cli_args.py --smoke` runs each argv shape (`chat-cold`, `chat-resume`, `headless`) with `--list-sessions` appended under a throwaway `HOME` and a dummy `GEMINI_API_KEY`, so the CLI parses the full argv and exits without contacting the API — exit 0 expected, or exit 42 for the `--resume <uuid>` shape against an empty store (parse succeeded, session missing). Any other exit fails the image build with the offending argv and the CLI's stderr. Bumping the pin means changing the Dockerfile line **and** `GEMINI_CLI_VERSION` together (`tests/unit/test_2971_gemini_runtime.py` asserts they match).

**Tests**: `tests/unit/test_2971_gemini_runtime.py` (22) — real `execute`/`execute_headless` over a captured `Popen`; the smoke harness against a fake `gemini` emulating 0.62.0's strict parser; Dockerfile pin and smoke placement.

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
| **Gemini CLI argv** | `docker/base-image/agent_server/services/gemini_cli_args.py` | Every argv the runtime spawns + the build-time `--smoke` (#2971) |
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
| 2026-10-01 | #2971: `@google/gemini-cli` pinned to 0.62.0 with a build-time argv smoke; argv built only in `gemini_cli_args.py` (no `--system-prompt`/`--max-turns`, `--skip-trust` always); system prompt prepended to the turn input; chat resumes only its own session id with the #2958 rules; echoed stdin rewritten to the caller's prompt in the execution log. See §5. |
| 2026-05-18 | Pipe-drop reclassification (#474/#873): `BrokenPipeError`/`ConnectionResetError` in `execute_headless` now raise HTTP 502 instead of 500, preventing false circuit-breaker trips when the Gemini child process exits early. |

---

## Related Documentation

- [Gemini Support Guide](../../GEMINI_SUPPORT.md) - User-facing setup guide
- [Trinity Compatible Agent Guide](../../TRINITY_COMPATIBLE_AGENT_GUIDE.md) - Template configuration
- [Multi-Runtime Architecture](../requirements.md#12-multi-runtime-support) - Requirements
- [Delegation Best Practices](../../MULTI_AGENT_SYSTEM_GUIDE.md#delegation-best-practices) - MCP vs runtime sub-agents

