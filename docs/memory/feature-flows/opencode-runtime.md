# Feature: OpenCode Runtime (#2981)

## Overview

A Trinity **harness IS an `AgentRuntime`**. **OpenCode** is the fourth runtime,
beside Claude Code (default), Gemini CLI and OpenAI Codex. It is model-agnostic:
an agent runs on any provider OpenCode knows from models.dev (OpenRouter by
default for open-weight models) or on a self-hosted OpenAI-compatible endpoint
declared in the template. An agent runs on OpenCode when its template declares
`runtime: { type: opencode, model: openrouter/qwen/qwen3-coder }`; the backend
creates the container with `AGENT_RUNTIME=opencode`, and `opencode_runtime.py`
implements the ABC.

As with Codex, the hard part is parity with Trinity's control layer (platform
prompt, read-only, guardrails, credential handling), plus one thing Codex never
had to solve: OpenCode exits 0 on turns that did not answer, so the parser
decides whether a turn succeeded. Ported from the lab proof of concept (2026-09)
and adopted to the current seam. Requirements: [runtimes.md §42](../requirements/runtimes.md).

## Flow: UI → API → Runtime → Side Effects

1. **Create.** Template `runtime.type: opencode` → `crud.py` lowercases the
   runtime, then `helpers.validate_runtime_model` (a `provider/model` shape, named
   400) and `helpers.validate_runtime_provider` (the optional `runtime.provider`
   block; a block under any other runtime is a 400). `_build_base_env` sets
   `AGENT_RUNTIME=opencode`, `AGENT_RUNTIME_MODEL`, and `AGENT_RUNTIME_PROVIDER`
   (compact JSON from `render_runtime_provider_env`, `''` when absent). OpenCode
   agents skip Claude-subscription auto-assign (`is_claude_runtime` is false).
   `lifecycle.recreate_container_with_updated_config` re-serializes the block from
   the volume's `template.yaml` with the same serializer and does not re-validate.
2. **Startup** (`startup.sh`). Creates the OpenCode root under `$AGENT_TMPDIR`
   (0700) and the shared config (`share: disabled`, `autoupdate: false`); the XDG
   variables are not exported globally. No `AGENTS.md` mirror — OpenCode reads
   `CLAUDE.md` natively. The guardrail path deny covers `.tmp/opencode/*`.
3. **Chat / Task.** `POST /api/chat` → `execute()`; `POST /api/task` →
   `execute_headless()`. Both go through `_execute_with_stall_retry` →
   `_execute_opencode`, which validates the model, runs the key-presence check
   (`opencode_provider.missing_key_env` → 503 naming the variable), builds the
   permission block, discovers playbook commands, calls `_prepare_turn_config`,
   and spawns `opencode run --format json --auto [-m …] [--session …] [--dir …] [--command …]`
   with `XDG_CONFIG_HOME` pointed at the turn dir.
4. **Commands.** `opencode_commands.discover_playbook_commands` reads the agent's
   skills through the Playbooks tab's reader and renders one `command:` entry each
   (template: follow the skill at its absolute SKILL.md path with `$ARGUMENTS`).
   `split_playbook_prefix` maps a first line `/<known-skill> [args]` to
   `--command <skill>`; an unknown name, or arguments carrying the `` !`cmd` ``
   shell shape, stay prose.
5. **MCP + prompt.** `opencode_mcp.apply_mcp_config` translates `~/.mcp.json`
   onto the turn config's `mcp:` key, with `${VAR}` → `{env:VAR}`, reverse lookup
   of literal secrets to their variable name, and unmatched secret-shaped literals
   dropped. The composed platform prompt is written to the turn dir and listed
   first in `instructions:`; the backend has already rewritten Trinity tool names
   to `trinity_<tool>` and prepended the OpenCode orientation block
   (`platform_prompt_service._adapt_instructions_for_runtime`).
6. **Controls.** `build_permission_block` maps read-only (`edit`/`bash` deny,
   `webfetch` allow), operator `disallowed_tools` ∪ platform denials, and a
   headless `allowed_tools` onto `permission:`; `merge_permission_block` keeps deny
   sticky over the operator's own block. Each unmapped control becomes a
   `trinity-control` execution-log entry.
7. **Parse + result.** Part-nested JSONL with top-level fallback → tool activity,
   tokens, cost, session id. `finalize_response` takes the last step's text and
   fails the turn on an `error` event, a `length` cut, a dangling `tool-calls`
   step, a reasoning-only or empty answer, or a text-shaped tool call. The cost
   policy sets `cost_usd=None` on a custom provider with no declared cost. Returns
   `(response_text, execution_log, ExecutionMetadata, …)` — the same shape as the
   other runtimes, so the backend ledger and result callback are unchanged. The
   turn dir is removed in `finally`.

## Key files

| Layer | File |
|-------|------|
| Identity leaf | `docker/base-image/agent_server/runtime_names.py` |
| Runtime | `agent_server/services/opencode_runtime.py` (engine, parser, permission block, stall retry, cost policy) |
| Provider | `agent_server/services/opencode_provider.py` (block read/validate/render, key presence) |
| MCP | `agent_server/services/opencode_mcp.py` (`.mcp.json` → `mcp:`, `{env:}` rewrite) |
| Playbooks | `agent_server/services/opencode_commands.py`, `routers/skills.py` (shared reader) |
| Contract | `runtime_adapter.py` (factory), `state.py`, `models.py` (`stall_retries`), `routers/chat.py`, `routers/info.py`, `result_callback.py`, `plugins_reinstall.py` |
| Base image | `docker/base-image/Dockerfile` (OpenCode 1.18.32 per arch + in-layer version assertion, pre-seeded provider packages), `startup.sh`, `hooks/guardrails-baseline.json` |
| Backend | `services/agent_service/{helpers,crud,lifecycle,terminal}.py`, `services/platform_prompt_service.py`, `services/session_turn_service.py`, `models.py` (`runtime_provider`), `main.py` (version payload) |
| Frontend | `components/RuntimeBadge.vue` (`brand-opencode`), `utils/agentRuntime.js`, `views/AgentDetail.vue`, `components/AgentTerminal.vue`, `tailwind.config.js` |
| Template | `config/agent-templates/test-opencode/` |

## Tests

| File | Pins |
|------|------|
| `tests/unit/test_opencode_runtime.py` | Parser (part-nested + fallback), failed-turn outcomes, error → HTTP, stall retry, command construction |
| `tests/unit/test_opencode_provider.py` | Provider block validation and render, `{env:}` key reference, key-presence 503 |
| `tests/unit/test_opencode_mcp_prompt.py` | `.mcp.json` → `mcp:` translation, no literal secret in the rendered config |
| `tests/unit/test_opencode_controls.py` | Permission block precedence, read-only, unmapped controls in the execution log, ledger fields |
| `tests/unit/test_opencode_backend_inertness.py` | The backend stays runtime-agnostic for OpenCode turns |
| `tests/unit/test_runtime_factory_opencode.py` | Factory returns the OpenCode singleton; unknown runtimes still fail |
| `tests/unit/test_2981_runtime_seam_hygiene.py` | `runtime_names.py` is the one identity leaf |
| `tests/unit/test_2981_playbook_dispatch.py` | `/name args` → `--command`, unknown name = prose, shell-shape guard |
| `tests/unit/test_2981_base_image_arch.py` | Per-arch OpenCode + Go install, in-layer version assertion |
| `tests/unit/test_2981_backend_registration.py` | Known runtime, model/provider validation, serializer round-trip, regex parity, Session-tab gate |
| `tests/unit/test_2981_terminal_opencode_mode.py` | Backend terminal `opencode` mode argv |
| `tests/unit/test_2981_docs_and_template.py` | `test-opencode` fixture validates; its commented provider example passes the real validator; docs indexed |
| `src/frontend/tests/unit/runtimeBadgeOpenCode.mount.spec.js` | Badge label and token family (mounted) |
| `src/frontend/tests/unit/agentRuntimeDefaultModel.spec.js` | `defaultModelForRuntime` per runtime |
| `src/frontend/tests/unit/agentTerminalOpenCodeMode.mount.spec.js` | Terminal requests the `opencode` mode (mounted) |

Existing suites extended: `test_validate_runtime.py`, `test_session_tab_gate_codex.py`,
`test_platform_prompt_runtime.py`, `test_runtime_capabilities_matrix.py`,
`test_ent945_environment_block.py`, `test_files_protected_paths.py`,
`test_ent819_files_read_tier.py`, `test_926_version_endpoint.py`,
`test_local_templates_listing.py`.

## Design decisions

- **T1** — The Go tarball is resolved per build arch in the same Dockerfile change (included in F).
- **T2** — Playbooks are per-turn `command:` config entries dispatched with `--command`, not a workspace shim directory.
- **T3** — Config is delivered through a per-turn `XDG_CONFIG_HOME`, scoped to the subprocess env.
- **T4** — An unset model runs on `anthropic/claude-sonnet-5`; `test-opencode` pins an OpenRouter open-weight model; a missing key is a 503 naming the variable, never a silent fallback.
- **T5** — The custom provider is `runtime.provider: {id, npm, base_url, api_key_env, models{cost?}}`, serialized to `AGENT_RUNTIME_PROVIDER` at create and rendered per turn.
- **T6** — A custom provider with no declared cost records `cost_usd=None` plus one warning, never $0.
- **T7** — The `crud.py` hotspot paydown (#3033) is deferred.
- **T8** — The `AGENTS.md` mirror (a copy, refreshed at every start) stays Codex-only; OpenCode reads `CLAUDE.md`.
- **G1** — A `runtime.provider` block under any runtime other than `opencode` is a named 400 at create.
- **G2** — The OpenCode orientation is prepended at render, like Codex's, and is not a `PLATFORM_INSTRUCTIONS` section.
- **G3** — Recreate serializes the provider block without re-validating it; the harness re-validates every turn.
- **H1** — The badge uses its own `brand-opencode` token family, no raw palette classes.
- **H2** — `defaultModel` moved to `utils/agentRuntime.js`; for OpenCode it returns the harness default (the agent payload carries no template model).
- **H3** — The terminal has an `opencode` mode; the TUI takes `--model` (`tui.ts:81-85` at v1.18.32).

## Verification status

**Unit-verified** (no binary, no image build):

- The parser runs against a **synthetic** `run --format json` stream,
  `tests/fixtures/opencode/run-format-json.synthetic.jsonl`, not a capture.
- OpenCode schema facts the code relies on were read from the v1.18.32 source and
  are cited where they are used:
  - `--command`: `run.ts:143-146`, dispatch at `run.ts:845-851`
    (`opencode_commands.py:15-20`).
  - `command:` entry schema: `core/src/v1/config/command.ts:5-12`
    (`opencode_commands.py:21-26`).
  - Shell expansion in command templates: `prompt.ts:1397-1407`,
    `config/markdown.ts:6` (`opencode_commands.py:37-38`).
  - `{env:VAR}` substitution with no default form: `config/variable.ts`
    (`opencode_mcp.py:53`, `opencode_provider.py:35`).
  - `permission:` schema and deny-before-ask: `config/permission.ts`,
    `config/config.ts:567-578`, `permission/index.ts:75-79`, `agent/agent.ts:119-135`
    (`opencode_runtime.py:549-569`).
  - TUI `--model`: `tui.ts:81-85` (`terminal.py:27`).

**Still live-only** — the plan's Before-merge list:

- Build the base image (`./scripts/deploy/build-base-image.sh`) — `opencode --version` layer assertion on x86_64 and arm64.
- Capture a real `opencode run --format json` stream on the pinned binary; replace the synthetic fixture; confirm `step_finish` shape (P1) and `--command` flag name (P7).
- Real `/verify-local` on the pinned binary.
- Cornelius (unmodified public template) on `openrouter/<open-weight>`: chat turn + scheduled headless turn; cost/tokens/model_used populated in the ledger.
- Read-only enforcement: a write is blocked (P4); denied tool surfaced in the execution log.
- `/playbook args` from a schedule and a dispatch runs the playbook deterministically.
- Trinity MCP `report` lands from inside the OpenCode session; template MCP servers carried; no literal secret on disk (grep the turn dir).
- Custom-provider block e2e on an OpenAI-compatible endpoint (Ollama-shaped); Replicate: record "no OpenAI-compatible endpoint" finding in the PR (no shim).
- Fidelity ablation: N scheduled runs of one deterministic Cornelius playbook, OpenCode vs Claude Code (≥90 % equivalence / 0 silent failures) → findings doc.
- Comment on #3033 recording the deferred crud.py paydown.

**Findings carried, not built:**

- **Replicate.** The proof of concept measured no OpenAI-compatible chat/tools
  endpoint for Replicate, so it cannot be a `runtime.provider` block. This is a
  report-back finding; no shim was built.
- **`auth.json` does not replace the key check (T4).** The turn dir links the
  shared `auth.json`, but the key-presence check runs first: an `anthropic/*`
  turn needs `ANTHROPIC_API_KEY` in the environment even when that linked
  `auth.json` holds an OpenCode OAuth credential. Otherwise it fails 503 naming
  the variable.
- **Stall retry is mitigation.** The watchdog retries a launch that never
  starts. What causes the stall is unknown, and how often it happens depends on
  the host.
