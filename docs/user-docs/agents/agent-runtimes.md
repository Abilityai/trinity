# Agent Runtimes

Trinity is model-agnostic: each agent runs on a pluggable runtime — the CLI harness that executes the agent inside its container. Trinity supports three runtimes: Claude Code (the default), Gemini CLI, and OpenAI Codex.

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

On the Agent Detail page, a runtime badge shows which runtime the agent is using. Chat works on all three runtimes, and each continues its **own** chat session from turn to turn. Only Claude Code resumes a session in the [Workspace](../sharing-and-access/workspace.md): a Codex agent's Workspace turns replay the visible history as text instead, and Gemini's headless turns never resume (see Limitations).

### Runtime Comparison

| | Claude Code (default) | Gemini CLI | OpenAI Codex |
|---|---|---|---|
| Auth model | Claude subscription / OAuth, or platform API key | The platform Gemini key (Settings, or `GEMINI_API_KEY` / `GOOGLE_API_KEY` on the backend) | `OPENAI_API_KEY` in `.env` (or a ChatGPT-plan login; Codex skips Claude-subscription auto-assign) |
| System-prompt file | `CLAUDE.md` | `CLAUDE.md` | `AGENTS.md` |
| Chat continuity (own session) | Yes | Yes | Yes |
| Workspace session resume | Yes | No (headless turns never resume) | No (history replayed as text) |
| MCP support | Yes | Yes | Yes |
| Cost reporting | Reported by the CLI | Estimated from tokens | Estimated from tokens |
| CLI version | Pinned in the base image | Pinned in the base image | Pinned in the base image |

Safety controls are not the same on every runtime. Platform controls (which credentials the agent holds, which agents and platform calls its key may reach) apply to all three, and so does credential redaction of stored results: the backend scrubs execution output whichever CLI produced it. The in-container controls differ:

| In-container control | Claude Code | OpenAI Codex | Gemini CLI |
|---|---|---|---|
| [Guardrail](agent-guardrails.md) hooks (Bash deny-list, credential-file protection, leak scan) | Yes | No | No |
| Read-only mode | Yes, through a hook | Yes, through Codex's own read-only sandbox (`--sandbox read-only`) | No |
| Credential redaction of output | Yes, inside the container | Yes, inside the container | No; only the platform's pattern-based scrub when results are stored |
| Turn limit | Yes | No; the execution timeout bounds the run | No; the execution timeout bounds the run |
| Per-agent disallowed tools | Yes | No | No |

See [How Trinity Keeps Agents in Bounds](../guides/keeping-agents-in-bounds.md#coverage-by-runtime) for where the boundary sits.

### Codex authentication

A Codex agent authenticates in one of two ways, and Trinity handles the file the CLI reads its credential from:

- **API key** — inject `OPENAI_API_KEY` (or `CODEX_API_KEY`) as a credential. Trinity logs the CLI in with that key before the agent's first turn, so an API-key Codex agent works out of the box. If you rotate the key in `.env`, the next turn re-logs in with the new one.
- **ChatGPT plan** — run `codex login` yourself from the agent's terminal. Trinity never overwrites a plan login with an API key.

### Gemini authentication

A Gemini agent uses the platform's Gemini key. Trinity reads the key saved in **Settings** first, then `GEMINI_API_KEY` or `GOOGLE_API_KEY` on the backend, and passes it to the container as `GEMINI_API_KEY` when the container is created. Settings accepts Google AI Studio keys that start with `AIza` or `AQ.` — see [Platform Keys](../credentials/platform-keys.md). With no key configured, the agent is still created, but its turns fail with "GEMINI_API_KEY not configured".

### Gemini CLI turns

Trinity pins `gemini-cli` in the base image, and the image build checks that the pinned CLI accepts every argument Trinity passes. A new CLI release that drops a flag therefore fails the image build instead of every turn. Gemini CLI has no system-prompt flag, so Trinity puts the platform instructions in front of each turn's input. The execution log still shows only your message as the user turn.

A chat turn resumes only the agent's own chat session, never the newest session on disk, so a scheduled or headless run never leaks into the chat. Changing the model or resetting the chat history starts a fresh session. If the saved session is gone, the turn retries once from a fresh session.

### How failures are reported

- **Claude Code chat:** a turn that ends with an error is recorded as **failed**, even when it produced some text first. The one exception is when the session transcript on disk proves the turn finished. The answer is then kept and marked as recovered.
- **Codex:** a failure counts as a rate limit only when the error says so ("rate limit", "quota", "too many requests") or reports an HTTP `429` status. A bare `429` inside a timestamp, port or process id is not misread as a rate limit.

### Headless runs on Claude Code

A task, schedule, loop, or MCP call runs the agent as a one-shot turn: nothing the agent starts survives the end of that turn. Claude Code's built-in tools that promise a *later* event — a scheduled wake-up, a cron entry, a workflow or task-output notification, a message to another local session, a push notification, a remote trigger — would let the agent plan around an event that will never arrive, so Trinity withholds that tool family from headless runs (`ScheduleWakeup`, `Workflow`, `Monitor`, `TaskOutput`, `CronCreate`/`CronList`/`CronDelete`, `SendMessage`, `ListAgents`, `PushNotification`, `RemoteTrigger`). The agent is told the same thing in its platform prompt and pointed at the platform's own mechanisms instead: `run_agent_loop` for repetition, `set_reminder` for a deferred self-trigger, and `chat_with_agent` for talking to another agent — see [Agent Loops](../automation/agent-loops.md) and [Agent Reminders](../automation/agent-reminders.md). Subagents are unaffected: the turn waits for them. A background shell command that is still running when the turn ends is killed, and the execution records that it was, instead of reporting a clean success.

## For Agents

Set the runtime in the template's `template.yaml`:

| Field | Values | Default | Notes |
|-------|--------|---------|-------|
| `runtime.type` | `claude-code`, `gemini-cli`, `codex` | `claude-code` | Selects the harness |
| `runtime.model` | runtime-specific model id (e.g. `gpt-5.6-sol`) | runtime default | Optional override — pin it, so recorded cost is attributable |

A Codex agent reads its identity and instructions from `AGENTS.md`. At startup Trinity copies the template's `CLAUDE.md` to `AGENTS.md` when the workspace has none, so a single instruction file works across runtimes; an `AGENTS.md` you ship yourself is left as it is.

Trinity's MCP tools are available to Codex agents. Codex references tools by their bare name (no `mcp__trinity__` prefix); Trinity adjusts the platform prompt automatically so the agent calls them correctly.

There is no API or MCP endpoint to switch an agent's runtime after creation. See the full API reference at `http://localhost:8000/docs`.

## Limitations

- **Codex cannot resume a session.** In the Workspace, a Codex agent's turns replay the visible history as text instead of carrying its working memory forward — the conversation stays coherent, but tool results and mid-task state do not survive between turns.
- **Gemini headless turns never resume a session.** Tasks, schedules and Workspace turns on a Gemini agent run without the previous turn's working memory. Only the Chat path resumes the agent's own Gemini session.
- **No turn cap on Gemini or Codex.** Neither CLI has a per-run turn limit, so the guardrail `max_turns` settings are logged but not enforced there. The execution timeout bounds the run.
- **The headless tool denial reaches an agent on its next container recreate** after the base image is rebuilt; the prompt guidance reaches every agent as soon as the platform is updated.
- **No runtime switch after creation.** Recreate the agent from a different template to change runtimes — a post-creation runtime-switch endpoint is planned.
- **Codex cost is estimated**, not metered exactly.
- **Each CLI is pinned in the base image.** A model newer than the pinned Claude Code version is refused, and the run fails with `model_unsupported` rather than an auth error (see [Model Selection](agent-configuration.md#model-selection)). Rebuild the base image after upgrading Trinity so agents pick up the newer CLIs.
- **Codex vision/image input and SSE streaming are out of scope** for the current release (planned).

## See Also

- [Creating Agents](creating-agents.md) -- Selecting a runtime via the template
- [Continuous Conversations](agent-session.md) -- What resuming preserves (Claude Code only)
- [Platform Keys](../credentials/platform-keys.md) -- The Gemini key a Gemini agent uses
- [Agent Guardrails](agent-guardrails.md) -- Your own `disallowed_tools`, merged with the platform's headless denials
- [Chat](agent-chat.md) -- Standard chat, available on all runtimes
- [Subscription Credentials](../credentials/subscription-credentials.md) -- Claude-subscription auto-assignment (skipped for Codex)
- [MCP Server](../integrations/mcp-server.md) -- Tools available to all runtimes
