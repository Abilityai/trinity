# Harness Authoring Guide — adding an agent runtime

> A Trinity **harness IS an `AgentRuntime`**: the pluggable execution engine that
> runs inside the agent container. This guide is the checklist for adding a
> fourth runtime, using the OpenAI **Codex** runtime (#1187) as the worked
> example. Claude Code is the reference implementation; Gemini and Codex are the
> two ports.

The work spans **four surfaces** (Invariant #13 — keep them in sync): the agent
server (the runtime + parity), the backend (credentials + Session-tab gate), the
MCP config, and the frontend. Do them in this order — the runtime + capability
contract first, then everything that depends on it.

---

## 1. Install the CLI in the base image

`docker/base-image/Dockerfile` — `RUN npm install -g <cli>@<pinned-version>`
**before** the `USER developer` switch (global installs need root). **Pin** the
version (not `@latest`) so base-image rebuilds are reproducible. Verify
`<cli> --version` in the booted image during `/verify-local`.

## 2. Implement the `AgentRuntime` ABC

New `docker/base-image/agent_server/services/<name>_runtime.py`. Implement all
abstract methods: `execute` (chat, with continuity), `execute_headless`
(stateless task), `configure_mcp`, `is_available`, `get_default_model`,
`get_context_window`. Build on the **existing per-runtime primitives** — do NOT
extract a shared helper as part of this PR, and do NOT copy Gemini's blanket
`kill_cgroup_orphans()`:

| Concern | Reuse |
|---------|-------|
| Cancellation / process tracking | `process_registry` (`register`/`unregister`/`active_execution_pids`) |
| Live SSE logs | `registry.publish_log_entry(execution_id, entry)` |
| Activity timeline | `activity_tracking.start_tool_execution` / `complete_tool_execution(id, success, output)` |
| Orphan tag | `utils/subprocess_pgroup.EXECUTION_TAG_NAME` on the subprocess env |
| **Concurrency-safe** orphan cleanup | `subprocess_lifecycle._capture_pgid` + `_terminate_process_group(pgid=…)` + `_drain_bounded(...)` — the latter runs `kill_cgroup_orphans(extra_pids=active_execution_pids())`, preserving sibling executions |
| Credential redaction | `utils/credential_sanitizer.sanitize_text` / `sanitize_dict` / `sanitize_subprocess_line` |

Spawn with `start_new_session=True` and `env={**os.environ, EXECUTION_TAG_NAME: execution_id}`.
Parse the CLI's machine output into `ExecutionMetadata` + `ExecutionLogEntry`.
Map errors to HTTP status: **auth → 503**, **rate-limit → 429**,
**runtime-unavailable → 500** (NOT 503 — 503 is the backend's AUTH signal and the
dispatch breaker counts AUTH only), **early pipe drop → 502** (the SUB-003 guard).
Add a singleton `get_<name>_runtime()`.

## 3. Override `capabilities()`

`RuntimeCapabilities` (in `runtime_adapter.py`) is how callers gate on a feature
instead of branching on the runtime name. Declare honest values:

```python
@classmethod
def capabilities(cls) -> RuntimeCapabilities:
    return RuntimeCapabilities(
        chat_continuity=True,        # the runtime can resume a conversation
        session_tab_resume=False,    # NOT the Session tab's cached-UUID --resume model
        mcp_support=True,
        cost_reporting="estimated",  # "native" only if the CLI reports real cost
    )
```

The ABC default is conservative, so forgetting this leaves the runtime
least-capable (safe) rather than over-claiming.

## 4. Register + validate in the factory

`runtime_adapter.get_runtime()` — add the branch and the value to
`KNOWN_RUNTIMES`. The factory **validates** `AGENT_RUNTIME` and **raises** on an
unknown value (it must never silently fall back to Claude). Test: factory
selects your runtime; an unknown value raises.

## 5. Wire the parity layer (BLOCKING — do not ship without it)

A naive port silently bypasses Trinity's Claude-specific safety layer. Every
runtime MUST wire all four:

1. **System prompt / identity** — the backend always sends an effective
   `system_prompt` (`task_execution_service.py`). If your CLI has no
   `--append-system-prompt` equivalent, **prepend** it to the prompt. Map the
   agent's persistent identity file if the CLI reads a different one (Codex reads
   `AGENTS.md`, so `startup.sh` mirrors `CLAUDE.md` → `AGENTS.md`).
2. **Sandbox / read-only mode** — the read-only signal is
   `~/.trinity/read-only-config.json` (`enabled`), the same file Claude's
   PreToolUse hook reads. Your runtime can't run Claude hooks, so read the file
   and translate. **Caution:** if your CLI ships an *internal* sandbox that needs
   user namespaces (Codex's bubblewrap), it will fail inside the hardened agent
   container (`bwrap: No permissions to create a new namespace`) and block every
   tool. The Trinity container is already the boundary (cap_drop ALL + AppArmor +
   no-new-privileges), so disable the inner sandbox for normal mode (Codex →
   `--sandbox danger-full-access`) and reserve the sandboxed setting for read-only
   (Codex → `--sandbox read-only`). Read-only *enforcement* for a CLI without
   hooks is an open question — surface it for a platform decision rather than
   assuming.
3. **Guardrails** — `_runtime_config._load_guardrails()` yields `disallowed_tools`
   + turn caps. Honor what maps to your CLI's controls; **surface (log) what
   doesn't** — never silently drop a guardrail.
4. **Credential sanitization** — run the sanitizer over the response text AND the
   raw log/stderr, exactly as the Claude/headless paths do.

## 6. Credentials + subscription (backend)

If the runtime authenticates with its own key (not a Claude subscription):
- The key arrives in the agent's `.env` (CRED-002), which is copied to disk but
  NOT exported into the agent-server process — so the runtime must read it from
  the process env OR parse `/home/developer/.env`, then inject it into the
  subprocess env.
- **Check whether the env var is actually enough.** A CLI may authenticate from
  its own state file rather than the environment, in which case the key must be
  *materialised to disk* before the first spawn — Codex authenticates its
  websocket transport from `$CODEX_HOME/auth.json` and 401s on every turn with
  only `OPENAI_API_KEY` set (#2208). Write it through the CLI's own login verb
  (key on **stdin**, never argv), make it idempotent, never overwrite a
  subscription credential or one you cannot parse, and re-write it when the
  resolved key changes.
- Skip the Claude-subscription auto-assign in `crud.py` and the OAuth juggle in
  `lifecycle.py` via `agent_service/helpers.is_claude_runtime(...)`, so the
  runtime gets no `CLAUDE_CODE_OAUTH_TOKEN` and no persisted `subscription_id`.
- Relocate any CLI home/state dir off the git-tracked workspace (Codex:
  `CODEX_HOME` under `$TMPDIR`, gitignored) and read-then-delete transient result
  files in a `finally`.

## 7. MCP (`trinity_mcp.py`)

Add a branch in both `inject_trinity_mcp_if_configured` (the Trinity HTTP MCP +
bearer token) and `configure_mcp_servers` (template MCP servers). **Do not fall
through to the Claude path.** Write the runtime's native config; reference the
bearer token by env var, never persist the literal secret. (Codex writes
`$CODEX_HOME/config.toml` with `bearer_token_env_var`.)

**Tool-name naming in the platform prompt** — registering the MCP server is only
half the job. `PLATFORM_INSTRUCTIONS` documents the tools with Claude's
`mcp__trinity__<tool>` prefix; a CLI that names MCP tools differently (Codex
auto-discovers them by bare name) will emit `unknown MCP server` if it copies the
prefixed call. Make the prompt runtime-aware in
`services/platform_prompt_service.py` (`get_platform_system_prompt(runtime=…)` /
`compose_system_prompt(runtime=…)`), thread the runtime from `routers/chat.py` +
`services/task_execution_service.py` via the `trinity.agent-runtime` label
(`docker_service.get_agent_runtime`, resolved lazily + guarded), and add your
runtime to the non-Claude branch.

## 8. Session-tab gate (backend, if `session_tab_resume=False`)

Add the runtime to the single constant `RUNTIMES_WITHOUT_SESSION_TAB_RESUME` in
`routers/sessions.py`. The turn endpoint then runs a plain stateless turn (drops
the cached UUID) for it. Verify the #678 reader-race retry stays inert for your
runtime's 502 shape and a generic failure never sets `error_code=AUTH`.

## 9. Frontend

- `components/RuntimeBadge.vue` — add the runtime's `is<Name>Runtime` computed,
  label, tooltip, color, and icon.
- `views/AgentDetail.vue` — `defaultModel` branch; hide the Session tab if
  `session_tab_resume` is false (`agent.runtime !== '<name>'`).
- `components/AgentTerminal.vue` — the `cli` terminal-mode mapping; add the
  matching backend mode in `services/agent_service/terminal.py`.

## 10. Template, tests, docs

- A `config/agent-templates/<name>/` template (`runtime: { type: <name>, model }`)
  declaring required credentials under `credentials.env_file` so the inject UI
  prompts for them. Mirror `test-codex/` / `test-gemini/`.
- Unit tests: parser → metadata, cost (no double-count), error→status mapping,
  capabilities, factory + unknown-runtime validation, MCP config (+ no dup on
  restart), subscription skip, Session-tab gate + backend inertness. **Parity
  tests:** read-only blocks writes, guardrails honored/surfaced, secret redacted,
  system prompt reaches the runtime, 2-turn chat continuity.
- E2E (`/verify-local`): a real `AGENT_RUNTIME=<name>` agent, one `/api/chat` +
  one `/api/task` turn → non-empty response, cost/tokens populated, cold-start
  auth works.
- Update `requirements.md`, `architecture.md` (the [Agent Runtimes](architecture/agent-runtime.md#agent-runtimes--multi-runtime--harness--runtime-1187)
  subsystem block), and a `feature-flows/` entry.
