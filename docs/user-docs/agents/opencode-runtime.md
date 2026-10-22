# OpenCode Runtime

OpenCode is one of the four runtimes an agent can run on. It works with many model providers, so you can run an agent on an open-weight model through OpenRouter or on a model you host yourself. You choose it in the agent's template when the agent is created.

## When to Use It

- You want an agent on an open-weight model (Qwen, Llama, DeepSeek and others) instead of Claude, Gemini or OpenAI models.
- You run your own model server that speaks the OpenAI-compatible API, such as a local inference server on the host machine.
- You want one template that can switch model providers by changing a single line.

For the agent's own work — tools, MCP calls, playbooks, schedules — Trinity treats an OpenCode agent like any other agent. See [Agent Runtimes](agent-runtimes.md) for how the four runtimes compare.

## Template Block

Declare the runtime and the key it needs in `template.yaml`:

```yaml
runtime:
  type: opencode
  model: openrouter/qwen/qwen3-coder

credentials:
  env_file:
    - OPENROUTER_API_KEY
```

Then inject `OPENROUTER_API_KEY` as a credential (create a key at https://openrouter.ai/keys). The runtime is fixed when the agent is created.

## Choosing a Model

The model is always `provider/model`. For OpenRouter, write `openrouter/` followed by the OpenRouter model id — for example `openrouter/qwen/qwen3-coder` or `openrouter/meta-llama/llama-4-maverick`. Trinity rejects a model without a provider prefix when the agent is created and tells you why.

Pick a model that supports tool calling. A model that answers with a tool call written out as text, instead of running the tool, fails the turn — Trinity does not treat it as an answer.

If you leave `model` out, the agent runs on `anthropic/claude-sonnet-5` and needs `ANTHROPIC_API_KEY` instead. Pin the model so the agent runs on the provider you set up.

## Custom OpenAI-Compatible Endpoint

To use a model server you host, add a `provider` block under `runtime` and point `model` at it:

```yaml
runtime:
  type: opencode
  model: local/qwen3-coder:30b
  provider:
    id: local
    npm: "@ai-sdk/openai-compatible"
    base_url: "http://host.docker.internal:11434/v1"
    api_key_env: LOCAL_LLM_API_KEY
    models:
      qwen3-coder:30b: {}

credentials:
  env_file:
    - LOCAL_LLM_API_KEY
```

- `id` is the provider half of `model` (`local/...` above). Lowercase letters, digits, `-` and `_`.
- `npm` must be `@ai-sdk/openai-compatible`, the only driver the agent image ships.
- `base_url` is the server's OpenAI-compatible URL, `http://` or `https://`. `host.docker.internal` is how an agent container reaches a server running on the Docker host itself; use a normal hostname for a server elsewhere.
- `api_key_env` is the **name** of the variable holding the key. Put the key in the agent's credentials, never in the template. If your server needs no key, inject any placeholder value.
- `models` lists the model ids the server offers. To have cost recorded, give a model a price, for example `qwen3-coder:30b: {cost: {input: 0.1, output: 0.3}}`. Without one, the cost of each run is recorded as unknown, not as free.

Trinity checks the block when the agent is created and again on every turn. A mistake is reported with the name of the field at fault. A `provider` block on any other runtime is rejected.

If you also edit the agent's shared `opencode.json` by hand, a `provider.<id>.options.apiKey` written there is copied into each turn's config as written, so use the `{env:VAR}` reference form there too (for example `{env:LOCAL_LLM_API_KEY}`), never the key itself.

## Playbooks

Start a message with `/<playbook> <arguments>` and an OpenCode agent runs that playbook, the same way Claude Code runs a slash command. A playbook prompt is dispatched the same way from chat, schedules, loops and agent messages. Only the first line can name a playbook, and only one the agent actually has (as listed in its Skills tab); anything else is sent as an ordinary message.

## What the Agent Sees

- **Instructions** — OpenCode reads the agent's `CLAUDE.md` directly, plus the Trinity platform instructions for each turn.
- **Trinity tools** — the Trinity MCP tools appear as `trinity_<tool>` (for example `trinity_list_agents`, `trinity_report`). The platform instructions use these names, so the agent calls them correctly.
- **Other MCP servers** — servers in the agent's `.mcp.json` are available too. Their keys are passed by variable name, not written into OpenCode's configuration.

## Safety Controls

- **Read-only mode** denies file edits and shell commands.
- **Disallowed tools** from the agent's guardrails, and the tools Trinity withholds from headless runs, are denied where OpenCode has an equivalent (`Write`, `Edit`, `Bash`, `WebFetch`). A tool name with no equivalent is listed in the run's execution log as not enforced, so you can see the gap.
- **Allowed tools** on a task restrict the run the same way.
- Agent output is scrubbed for credentials before it is stored.

The Claude Code guardrail hooks do not run on OpenCode. See [Agent Runtimes](agent-runtimes.md#runtime-comparison) for the full comparison.

## Limitations

- **No session resume.** The Workspace replays the visible conversation as text; tool results and mid-task state do not carry over between turns. The Chat tab continues its own session.
- **No subscription login.** OpenCode runs on provider API keys only.
- **Cost on a custom endpoint is unknown** unless you declare a price for the model.
- **No image input.**
- **No turn limit** — the execution timeout bounds the run.
- **The runtime cannot be changed after creation.** Recreate the agent from a template with a different runtime.
- **Not every provider works.** A custom endpoint must offer an OpenAI-compatible chat API with tool calling.

## See Also

- [Agent Runtimes](agent-runtimes.md) -- How the four runtimes compare
- [Creating Agents](creating-agents.md) -- Creating an agent from a template
- [Agent Guardrails](agent-guardrails.md) -- Disallowed tools and limits
- [MCP Server](../integrations/mcp-server.md) -- The Trinity tools available to every runtime
