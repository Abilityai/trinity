# Feature: OpenCode Runtime (#2981)

## Overview

**OpenCode** is the fourth `AgentRuntime`, alongside Claude Code (default),
Gemini CLI and Codex. An agent runs on OpenCode when its template declares
`runtime: { type: opencode, model: "<provider>/<model>" }`; the backend creates
the container with `AGENT_RUNTIME=opencode`, and
`agent_server/services/opencode_runtime.py` implements the ABC over
`opencode run --format json --auto`. The CLI is pinned to **v1.18.32**.

What OpenCode adds over the other three is the **model source**: one runtime
reaches any models.dev provider (OpenRouter, Anthropic, OpenAI), a local Ollama,
or any OpenAI-compatible endpoint, chosen by the `provider/` prefix of the model
id. Requirements: [runtimes.md §14.4 and RT-OPENCODE](../requirements/runtimes.md).
Architecture: [agent-runtime.md](../architecture/agent-runtime.md#agent-runtimes--multi-runtime--harness--runtime-1187).

## Flow: UI → API → Runtime → Side Effects

1. **Create.** Template `runtime.type: opencode` → `crud.py` sets
   `AGENT_RUNTIME=opencode` + the `trinity.agent-runtime` label, and
   `settings_service.apply_opencode_provider_env` injects the platform provider
   tier **into `runtime == opencode` agents only**: `OPENROUTER_API_KEY` from the
   `openrouter_api_key` setting and `OPENCODE_DEFAULT_PROVIDER_BASE_URL` from
   `opencode_default_provider_base_url`. OpenCode agents skip Claude-subscription
   auto-assign (`is_claude_runtime()` gate); `lifecycle.py` mirrors it on recreate.
   `runtime.model` is shape-checked by `helpers.validate_runtime_model` — not
   `provider/model` is a named 400.
2. **Startup** (`startup.sh`). `CLAUDE.md` is copied to `AGENTS.md` (Abilityai/lilu#627
   — a copy, refreshed per turn by `refresh_agents_md()`), the pre-seeded provider
   `node_modules` tree is copied from `/opt/trinity/opencode` into `OPENCODE_CONFIG_DIR`,
   and `OPENCODE_DISABLE_AUTOUPDATE` and the other OpenCode switches are exported.
3. **Dispatch** (backend). A turn with no explicit model gets
   `task_execution_service._default_model_for()`: for OpenCode that is
   `platform_default_model_opencode` (a `provider/model` id), never the fleet
   Claude default, which OpenCode refuses as "missing a provider prefix". Unset →
   `None`, and the agent's own `AGENT_RUNTIME_MODEL` / CLI default applies. The
   `/api/chat` and `/api/model` paths validate `provider/model` the same way
   (Abilityai/lilu#652).
4. **Turn** (`opencode_runtime.py`). Per turn, `_prepare_turn_config` writes a
   0600 `opencode.json` under a per-turn `XDG_CONFIG_HOME`:
   - the platform prompt in `instructions:` (there is no system-prompt flag);
   - `.mcp.json` translated onto the `mcp:` key (`opencode_mcp.py`);
   - the env-declared provider blocks (`opencode_provider.apply_provider_config`);
   - `.claude/commands/*` and `.claude/skills/*/SKILL.md` as `command.<name>`;
   - the tool policy: guardrail `disallowed_tools` and read-only denies.
   The child env then gets `OPENCODE_DISABLE_SHARE=1` and, in read-only mode,
   `OPENCODE_PERMISSION` (see **B1** below). The message is written to **stdin**
   and stdin closed (see **B2**). `/name args` runs as `--command name -- args`.
5. **Parse.** JSON lines; the response is the last step's concatenated `text`
   parts. Cost is native (`step_finish.cost`); `input_tokens` is the last step's
   (each step re-sends the session). An empty final text is a failed turn.
6. **Side effects.** Session id recorded for chat continuity (`--session`), the
   Session tab resumes via `resume_session_id`, and the #1083 async 202 path is
   open (`async_dispatch=True`).

## Model sources

| Prefix | Configured by | Notes |
|---|---|---|
| `openrouter/…` | `OPENROUTER_API_KEY` in the agent `.env`, else the platform `openrouter_api_key` setting | Built-in provider, no block needed. No key anywhere → refused before the subprocess |
| `ollama/…` | `OPENCODE_PROVIDER_OLLAMA_BASE_URL` (alias `OPENCODE_OLLAMA_BASE_URL`) > platform `opencode_default_provider_base_url` > the container's default gateway | `/v1` appended; the turn's model is always declared |
| `<id>/…` (any) | `OPENCODE_PROVIDER_<ID>_BASE_URL`, `_API_KEY`, `_MODELS`, `_NPM` | `<ID>` is lower-cased into the provider id; only the pre-seeded `@ai-sdk/openai-compatible` driver is accepted |
| `anthropic/…`, `openai/…` | the provider's own key in `.env` | models.dev providers |

The provider block is computed **per turn** from the spawn env (the agent `.env`
over the container baseline, #1999), so a changed `.env` applies on the next turn
with no restart. An operator's own `provider.<id>` in `opencode.json` wins on
`npm`/`options`; model lists are unioned.

**Images** (Abilityai/lilu#660). The pinned binary accepts an image only on a
model whose capabilities include image input. Config-only providers (Ollama, any
URL-declared endpoint) therefore default to **no** images; models.dev providers
default to yes. `OPENCODE_PROVIDER_<ID>_IMAGES=true|false` overrides either, and
`capabilities().image_input` reports the configured model's provider.

## Controls delivered through env (review blockers B1, B2)

- **B1 — workspace config cannot re-enable share or edit/bash.** The per-turn
  global config is OpenCode's **lowest**-precedence layer: a workspace
  `opencode.json` or `.opencode/opencode.json` merges over it. So two controls are
  also delivered through env, which OpenCode applies after every file:
  `OPENCODE_DISABLE_SHARE=1` on every spawn, and in read-only mode
  `OPENCODE_PERMISSION={"edit":"deny","bash":"deny"}` (popped otherwise).
  Guardrail `disallowed_tools` are still delivered only in the per-turn config.
- **B2 — the prompt never travels in argv.** The message is written to stdin and
  stdin closed (the #3838 class on dev); `--` is kept, so a >128 KiB message no
  longer makes `Popen` raise `E2BIG`. A failed handoff (`BrokenPipeError`, child
  died at startup) ends the child and unregisters it.

## Error → HTTP mapping

| Condition | HTTP |
|---|---|
| CLI missing in the container | 503 |
| `-m` not `provider/model`, or a provider config / missing key (`OpenCodeProviderConfigError`, `OpenCodeProviderCredentialError`) | 400, named |
| Provider auth failure | 503 |
| Model refused by the provider | 400 |
| Rate limit | 429 |
| More steps than `max_turns` | 422 `Task exceeded turn limit:` |
| Timeout, or a silent turn after `OPENCODE_STALL_RETRIES` relaunches | 504 |
| Pipe dropped | 502 |
| Empty response (`reasoning_only`, `empty_response`), generic | 500 |

## Key files

| File | Role |
|---|---|
| `docker/base-image/agent_server/services/opencode_runtime.py` | The runtime: per-turn config, spawn, parse, errors |
| `docker/base-image/agent_server/services/opencode_provider.py` | Env → `provider:` blocks, OpenRouter key check, images |
| `docker/base-image/agent_server/services/opencode_mcp.py` | `.mcp.json` → `mcp:` translation |
| `docker/base-image/agent_server/services/_runtime_config.py` | Images defaults, disallowed-tool mapping |
| `docker/base-image/startup.sh` | `AGENTS.md`, provider tree, OpenCode env switches |
| `src/backend/services/settings_service.py` | `openrouter_api_key`, `opencode_default_provider_base_url`, `platform_default_model_opencode` |
| `src/backend/routers/settings/provider_keys.py` | `GET /api/settings/opencode-provider`, `PUT/DELETE /api/settings/api-keys/openrouter`, `PUT /api/settings/opencode-provider/base-url` |
| `src/backend/services/task_execution_service.py` | `_default_model_for()` |
| `src/backend/services/agent_service/helpers.py` | `validate_runtime_model` |

## Tests

- `tests/unit/test_lilu257_opencode_runtime.py` — parse, errors, stdin delivery, env-delivered controls
- `tests/unit/test_lilu257_opencode_provider.py` — provider grammar, OpenRouter refusal
- `tests/unit/test_lilu257_opencode_mcp_prompt.py` — MCP translation, platform prompt
- `tests/unit/test_lilu257_opencode_backend_registration.py`, `test_lilu257_runtime_seam_hygiene.py`
- `tests/unit/test_lilu659_opencode_part_and_images.py` — images capability
- `tests/unit/test_lilu652_opencode_platform_default.py` — `_default_model_for` and the settings resolution
- `tests/unit/test_runtime_capabilities_matrix.py`, `test_2796_dispatch_model_validation.py`

## Out of scope / open

- The base image with the pinned binary is not yet built and published; agents need a base-image rebuild + recreate.
- A UI for the OpenCode settings (they are API-only).
- Output and logs pass the in-container credential sanitizer, but Claude Code's guardrail hooks (Bash deny-list, credential-file protection) do not run on OpenCode.
