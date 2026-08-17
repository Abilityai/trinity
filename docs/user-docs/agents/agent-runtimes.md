# Agent Runtimes

Trinity agents run on a pluggable runtime — the CLI harness that executes the agent inside its container. Trinity supports Claude Code (default), Gemini CLI, and OpenAI Codex.

## Concepts

**Runtime** -- The execution engine inside the agent container. Each runtime is a different coding-agent CLI with its own auth model, system-prompt file, and capabilities. A template picks one; if none is declared, the agent uses Claude Code.

**Harness** -- Another name for the runtime. The harness reads the agent's system prompt, calls tools and MCP servers, and produces responses, which Trinity treats identically regardless of which runtime ran them.

## How It Works

A template selects the runtime in `template.yaml`:

```yaml
runtime:
  type: codex          # claude-code (default) | gemini-cli | codex
  model: gpt-5.6-sol   # optional model override
```

The runtime is fixed when the agent is created. To change it, recreate the agent from a template that declares a different runtime — there is no post-creation switch.

On the Agent Detail page, a runtime badge shows which runtime the agent is using. Chat works the same across all runtimes. For Codex agents, the **Session** tab is hidden (Codex does not support session resume); the **Chat** tab remains, with full conversation continuity.

### Runtime Comparison

| | Claude Code (default) | Gemini CLI | OpenAI Codex |
|---|---|---|---|
| Auth model | Claude subscription / OAuth, or platform API key | Gemini API key | `OPENAI_API_KEY` (Codex skips Claude-subscription auto-assign) |
| System-prompt file | `CLAUDE.md` | `CLAUDE.md` | `AGENTS.md` |
| Chat continuity | Yes | Yes | Yes |
| Working memory across turns | Yes | Yes | No (history replayed as text) |
| MCP support | Yes | Yes | Yes |
| Cost reporting | Actual | Actual | Estimated |

Safety controls apply across all runtimes: read-only mode and credential redaction work the same regardless of runtime. Codex enforces read-only through its own sandbox (`--sandbox read-only`) rather than the Claude tool-use hook.

## For Agents

Set the runtime in the template's `template.yaml`:

| Field | Values | Default | Notes |
|-------|--------|---------|-------|
| `runtime.type` | `claude-code`, `gemini-cli`, `codex` | `claude-code` | Selects the harness |
| `runtime.model` | runtime-specific model id (e.g. `gpt-5.6-sol`) | runtime default | Optional override — pin it, so recorded cost is attributable |

A Codex agent reads its identity and instructions from `AGENTS.md`. Trinity mirrors the template's `CLAUDE.md` into `AGENTS.md` at startup, so a single instruction file works across runtimes.

Trinity's MCP tools are available to Codex agents. Codex references tools by their bare name (no `mcp__trinity__` prefix); Trinity adjusts the platform prompt automatically so the agent calls them correctly.

There is no API or MCP endpoint to switch an agent's runtime after creation. See the full API reference at `http://localhost:8000/docs`.

## Limitations

- **Codex cannot resume a session.** In the Workspace, a Codex agent's turns replay the visible history as text instead of carrying its working memory forward — the conversation stays coherent, but tool results and mid-task state do not survive between turns.
- **No runtime switch after creation.** Recreate the agent from a different template to change runtimes — a post-creation runtime-switch endpoint is planned.
- **Codex cost is estimated**, not metered exactly.
- **Codex vision/image input and SSE streaming are out of scope** for the current release (planned).

## See Also

- [Creating Agents](creating-agents.md) -- Selecting a runtime via the template
- [Continuous Conversations](agent-session.md) -- What resuming preserves (Claude/Gemini only)
- [Chat](agent-chat.md) -- Standard chat, available on all runtimes
- [Subscription Credentials](../credentials/subscription-credentials.md) -- Claude-subscription auto-assignment (skipped for Codex)
- [MCP Server](../integrations/mcp-server.md) -- Tools available to all runtimes
