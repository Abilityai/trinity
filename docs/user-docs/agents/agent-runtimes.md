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

On the Agent Detail page, a runtime badge shows which runtime the agent is using. Chat works the same across all runtimes, with full conversation continuity. Codex cannot resume a session, so in the [Workspace](../sharing-and-access/workspace.md) a Codex agent's turns replay the visible history as text instead of carrying working memory forward (see Limitations).

### Runtime Comparison

| | Claude Code (default) | Gemini CLI | OpenAI Codex |
|---|---|---|---|
| Auth model | Claude subscription / OAuth, or platform API key | Gemini API key | `OPENAI_API_KEY` in `.env` (or a ChatGPT-plan login; Codex skips Claude-subscription auto-assign) |
| System-prompt file | `CLAUDE.md` | `CLAUDE.md` | `AGENTS.md` |
| Chat continuity | Yes | Yes | Yes |
| Working memory across turns | Yes | Yes | No (history replayed as text) |
| MCP support | Yes | Yes | Yes |
| Cost reporting | Actual | Actual | Estimated |

Safety controls apply across all runtimes: read-only mode and credential redaction work the same regardless of runtime. Codex enforces read-only through its own sandbox (`--sandbox read-only`) rather than the Claude tool-use hook.

### Codex authentication

A Codex agent authenticates in one of two ways, and Trinity handles the file the CLI reads its credential from:

- **API key** — inject `OPENAI_API_KEY` (or `CODEX_API_KEY`) as a credential. Trinity logs the CLI in with that key before the agent's first turn, so an API-key Codex agent works out of the box. If you rotate the key in `.env`, the next turn re-logs in with the new one.
- **ChatGPT plan** — run `codex login` yourself from the agent's terminal. Trinity never overwrites a plan login with an API key.

### Headless runs on Claude Code

A task, schedule, loop, or MCP call runs the agent as a one-shot turn: nothing the agent starts survives the end of that turn. Claude Code's built-in tools that promise a *later* event — a scheduled wake-up, a cron entry, a workflow or task-output notification, a message to another local session, a push notification, a remote trigger — would let the agent plan around an event that will never arrive, so Trinity withholds that tool family from headless runs (`ScheduleWakeup`, `Workflow`, `Monitor`, `TaskOutput`, `CronCreate`/`CronList`/`CronDelete`, `SendMessage`, `ListAgents`, `PushNotification`, `RemoteTrigger`). The agent is told the same thing in its platform prompt and pointed at the platform's own mechanisms instead: `run_agent_loop` for repetition, `set_reminder` for a deferred self-trigger, and `chat_with_agent` for talking to another agent — see [Agent Loops](../automation/agent-loops.md) and [Agent Reminders](../automation/agent-reminders.md). Subagents are unaffected: the turn waits for them. A background shell command that is still running when the turn ends is killed, and the execution records that it was, instead of reporting a clean success.

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
- **The headless tool denial reaches an agent on its next container recreate** after the base image is rebuilt; the prompt guidance reaches every agent as soon as the platform is updated.
- **No runtime switch after creation.** Recreate the agent from a different template to change runtimes — a post-creation runtime-switch endpoint is planned.
- **Codex cost is estimated**, not metered exactly.
- **Codex vision/image input and SSE streaming are out of scope** for the current release (planned).

## See Also

- [Creating Agents](creating-agents.md) -- Selecting a runtime via the template
- [Continuous Conversations](agent-session.md) -- What resuming preserves (Claude/Gemini only)
- [Agent Guardrails](agent-guardrails.md) -- Your own `disallowed_tools`, merged with the platform's headless denials
- [Chat](agent-chat.md) -- Standard chat, available on all runtimes
- [Subscription Credentials](../credentials/subscription-credentials.md) -- Claude-subscription auto-assignment (skipped for Codex)
- [MCP Server](../integrations/mcp-server.md) -- Tools available to all runtimes
