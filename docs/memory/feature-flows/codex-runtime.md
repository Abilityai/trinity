# Feature: OpenAI Codex Runtime (#1187)

## Overview

A Trinity **harness IS an `AgentRuntime`** — the pluggable execution engine inside
the agent container. **Codex** is the third runtime, alongside Claude Code
(default) and Gemini CLI. An agent runs on Codex when its template declares
`runtime: { type: codex, model: gpt-5.6-sol }`; the backend creates the
container with `AGENT_RUNTIME=codex`, and `codex_runtime.py` implements the ABC.

The hard part is **not** wiring a new CLI — it's achieving full parity with
Trinity's Claude-specific safety layer (system prompt, read-only mode,
guardrails, credential sanitization), which a naive port silently bypasses.

This is an MVP (follow-up to spike #854). For adding a *fourth* runtime, see the
[Harness Authoring Guide](../harness-authoring-guide.md).

## Flow: UI → API → Runtime → Side Effects

1. **Create.** Template `runtime.type: codex` → `crud.py` sets `AGENT_RUNTIME=codex`
   env + `trinity.agent-runtime=codex` label. Codex agents **skip** Claude-subscription
   auto-assign (`is_claude_runtime()` gate) — no `CLAUDE_CODE_OAUTH_TOKEN`, no
   persisted `subscription_id`. `lifecycle.py` mirrors the skip on recreate.
2. **Startup** (`startup.sh`). Mirrors `CLAUDE.md` → `AGENTS.md` (Codex reads
   `AGENTS.md`), creates `CODEX_HOME` under `$TMPDIR` (off the git-tracked repo),
   gitignores `.tmp/`. MCP config (`trinity_mcp.py`) writes `$CODEX_HOME/config.toml`.
3. **Chat / Task.** `POST /api/chat` → `runtime.execute()`; `POST /api/task` →
   `runtime.execute_headless()`. Both build `codex exec --json --skip-git-repo-check
   -C /home/developer --sandbox <mode> -o <CODEX_HOME>/<exec_id>-last.txt
   [-m model] [resume <thread_id>] -- <prompt>` and stream the JSONL.
   `<mode>` = `danger-full-access` normally (no inner bwrap sandbox — see Safety
   parity), `read-only` when the agent is read-only. Exec-level flags **precede**
   `resume` (the `resume` sub-subcommand rejects them — the turn-2+ continuity
   fix); `--` ends options so a `-`-leading prompt can't be reparsed as a flag.
4. **Parse.** `thread.started`→session id; `turn.completed.usage`→tokens (cost
   estimated; `reasoning_output_tokens` ⊂ `output_tokens`, never double-counted);
   `item.completed`→agent message / tool activity; `turn.failed`/`error`→error.
   The `-o` file is the **authoritative** response (read-then-delete in `finally`);
   JSONL `agent_message` is the fallback.
5. **Return.** `(response_text, execution_log, ExecutionMetadata, …)` — same shape
   as Claude/Gemini, so the backend treats Codex executions identically.

## Safety parity (Phase C — blocking)

| Control | Claude | Codex |
|---------|--------|-------|
| System prompt | `--append-system-prompt` | prepended to the prompt; `AGENTS.md` for identity; MCP-tool naming made runtime-aware (no `mcp__trinity__` prefix — see MCP) |
| Sandbox | none (container is the boundary) | normal → `--sandbox danger-full-access` (Codex's own bwrap sandbox can't create a user namespace in the hardened container — `bwrap: No permissions…` — which blocks every tool; drop it, the container stays the boundary, same as Claude) |
| Read-only | PreToolUse hook on `~/.trinity/read-only-config.json` | reads the same file → `--sandbox read-only` (Codex has no PreToolUse hook; a fail-closed enforcement story is a fast-follow) |
| Guardrails | `--disallowedTools` + turn caps | sandbox + network; unmappable tool-names **logged** (not dropped) |
| Credential redaction | sanitizer over response + logs | identical sanitizer calls |

## Authentication (#1971, #2208)

Two credential shapes, one file. The CLI authenticates its
`wss://api.openai.com/v1/responses` transport from **`$CODEX_HOME/auth.json`**,
NOT from the environment — and that transport is no longer optional
(`responses_websockets` / `..._v2` are listed as *removed* in
`codex features list`, so there is no flag back to the HTTP path that did accept
a bare env key).

| Agent | Credential | Who writes `auth.json` |
|---|---|---|
| Subscription (ChatGPT plan) | none — no API key by design (#1971) | the operator, via `codex login`; `auth_mode: chatgpt` |
| API key | `OPENAI_API_KEY` (or `CODEX_API_KEY`) in `.env` | **Trinity**, lazily on first turn; `auth_mode: apikey` |

`_materialize_api_key_auth` runs in `_execute_codex` **before** the spawn —
after would still 401. It shells out to `codex login --with-api-key` with the key
on **stdin, never argv** (a process listing is readable by the agent's own
turns). Before #2208 nothing ever ran it, so an API-key agent 401'd on every turn
and could not complete a single one; only subscription agents worked.

Three properties carry the design:

* **Subscription auth wins.** Only an `auth_mode: apikey` file is overwritten.
  A `chatgpt` file — or one that will not parse — is left exactly where it is.
  The classification is tri-state (`absent` / `unreadable` / `parsed`) precisely
  so "no file" and "a file I cannot read" never collapse into "safe to write".
* **Rotation propagates.** Since #1999 the execution env is rebuilt from `.env`
  per spawn, so the key can change under a live container; a stored key that no
  longer matches is re-logged-in. The steady state costs one small file read per
  turn, not a subprocess.
* **It never raises.** A login failure logs at ERROR and the turn proceeds to
  fail with the CLI's own auth error. Raising would convert an auth problem into
  a Trinity 503 on the subscription path too.

`_has_subscription_auth` is therefore keyed on **`auth_mode`, not existence**.
Once Trinity writes an `auth.json` of its own, an existence test reports every
API-key agent as a subscription agent — and the #1971 credential gate would then
let a container whose key was *removed* from `.env` keep running on the stale
file it left behind, i.e. silent failure of credential revocation (the #1999
class). An `auth.json` with no `auth_mode` (older CLI) or an unreadable one still
counts as a subscription credential: unknown provenance is not ours to discount,
and over-reporting there preserves the pre-#2208 behaviour exactly.

## Error → HTTP mapping

auth (missing/invalid key, 401) → **503**; rate-limit → **429**;
runtime-unavailable → **500** (NOT 503 — 503 is the backend's AUTH signal, and the
dispatch breaker counts AUTH only); early pipe drop → **502** (SUB-003 guard).
Generic failures staying at 500 keep the AUTH path and SUB-003 auto-switch inert
for Codex; the #678 reader-race retry never matches a Codex 502 (no
`recovery_attempted` marker).

## Capabilities & Session tab

`CodexRuntime.capabilities()` → `chat_continuity=True` (`codex exec resume`),
`session_tab_resume=False`, `mcp_support=True`, `cost_reporting="estimated"`.
Because `session_tab_resume=False`, the backend gates the Session-tab cached-UUID
`--resume` turn off (one constant `RUNTIMES_WITHOUT_SESSION_TAB_RESUME` in
`sessions.py` → stateless turn) and the frontend hides the Session tab. The Chat
tab (with continuity) stays.

## Cost estimation (#1187, corrected #2207)

`cost_reporting="estimated"` means the number `calculate_codex_cost()` computes
**is** the number Trinity records — Codex reports no cost of its own. It flows to
`schedule_executions.cost`, agent analytics, cost-threshold alerts, and the loop
budget `max_cost_usd` (#1155). Because a budget is a **spend control**, an
understated rate is a control bypass, not a cosmetic metric error: #2207 found
the table stranded on the gpt-5.1 generation, so every newer model prefix-matched
`gpt-5` at $1.25/$10 — 3.6x under for `gpt-5.6-sol`, **21x under for
`gpt-5.5-pro`**. Rates therefore fail toward **over**-reporting.

Three properties, each load-bearing:

- **Rates are per 1,000,000 tokens**, stated exactly as OpenAI publishes them, so
  `CODEX_PRICING` diffs against the rate card by eye. The bug being fixed was a
  stale transcription; per-1K forced arithmetic on every audit.
- **The prefix match is boundary-aware.** A prefix matches only when the
  remainder starts with `-` (a variant/date suffix: `gpt-5.2-codex` → `gpt-5.2`),
  never `.` (a new generation: `gpt-5.7-sol` → `default`). A bare `startswith`
  makes every entry a catch-all for future models and silently routes them to the
  oldest, cheapest rate — the same defect, pre-armed for the next release.
  Deliberately **asymmetric** with `model_context._FAMILY_PREFIX_WINDOWS`, whose
  bare `startswith` is correct because there the fall-through picks the *smallest
  window*, which over-reports usage. Same mechanism, opposite risk.
- **`default` is the flagship rate, not the cheapest.** An unpinned agent reaches
  it on every turn: `_CodexParseState.model` is the caller-supplied value and
  `thread.started` carries only a thread id, so Trinity never learns the model the
  CLI actually resolved. Since that default *is* `gpt-5.6-sol`, pricing the
  unknown case at sol's rate is the accurate answer, not a pessimistic one.

**Long-context tier**: prompts over `LONG_CONTEXT_THRESHOLD_TOKENS` (272K input)
bill the whole request at 2x input / 1.5x output, applied only for models that
publish such a tier. Note 272K is a *price break*, **not** a window — the 5.6
family's window is 1,050,000 (`CODEX_EXTENDED_CONTEXT_WINDOW`). Conflating the
two is what made the context gauge read ~3.9x too full on every 5.6 turn.

**Cache writes are priced**, because Codex reports them. A live gpt-5.6-sol turn
on codex-cli 0.147.0 emits `cache_write_input_tokens` alongside
`cached_input_tokens`, and it is a **subset of `input_tokens`** — the same trap
as `reasoning_output_tokens` ⊂ `output_tokens`. Input therefore splits three
ways, each at its own rate:

```
plain = input_tokens - cached_input_tokens - cache_write_input_tokens
```

Writes carry a 1.25x surcharge on gpt-5.4+. On the measured turn 11,395 of 11,398
input tokens were writes, so ignoring them under-reports that turn by ~20% (and
the pre-#2207 table under-reported it by **5.0x**). Both subset counters are
clamped before use: they arrive from an external process, and an oversubscribed
payload must never produce a negative plain remainder, which would silently
*reduce* the bill.

**Deliberately unpriced** (documented, not guessed): **fast mode** (~2x) is
genuinely unobservable — nothing in the event stream distinguishes it.
`gpt-5.3-codex` / `gpt-5.3-codex-spark` remain reachable with an API key but
OpenAI publishes no rate for them, so they resolve to `default` (over-reporting)
rather than carrying an unsourceable number. **Never add a rate that is not on
the card.**

## MCP

`_inject_codex_mcp` / `_configure_codex_mcp_servers` write `$CODEX_HOME/config.toml`
directly (merging, idempotent — same approach the Gemini path uses for its
settings.json). The Trinity HTTP MCP references the token via `bearer_token_env_var`
= `TRINITY_MCP_API_KEY` — **the literal secret is never persisted** to config.toml.

Registering the server is only half of MCP working. `PLATFORM_INSTRUCTIONS`
documents the tools with Claude's `mcp__trinity__<tool>` prefix; Codex
auto-discovers MCP tools by bare name and answers `unknown MCP server` to a
prefixed call. So the platform prompt is **runtime-aware**:
`platform_prompt_service.get_platform_system_prompt(runtime=…)` /
`compose_system_prompt(runtime=…)` strip the prefix and add a Codex orientation
note for `runtime="codex"` (Claude/Gemini/unknown unchanged). The runtime is
threaded from `routers/chat.py` + `services/task_execution_service.py`, resolved
best-effort + lazily from the `trinity.agent-runtime` label
(`docker_service.get_agent_runtime`).

## Key files

| Layer | File |
|-------|------|
| Base image | `docker/base-image/Dockerfile` (`@openai/codex@0.139.0`), `startup.sh` (AGENTS.md, CODEX_HOME) |
| Runtime | `docker/base-image/agent_server/services/codex_runtime.py` |
| Contract | `runtime_adapter.py` (`RuntimeCapabilities`, factory + validation), `models.py` (`ExecutionMetadata.status/error_code`) |
| MCP | `agent_server/services/trinity_mcp.py`, `services/platform_prompt_service.py` (runtime-aware tool naming) |
| Backend | `services/agent_service/{crud,lifecycle,helpers,terminal}.py`, `routers/sessions.py`, `routers/chat.py` + `services/task_execution_service.py` (thread runtime), `services/docker_service.py` (`get_agent_runtime`) |
| Frontend | `components/RuntimeBadge.vue`, `components/AgentTerminal.vue`, `views/AgentDetail.vue` |
| Template | `config/agent-templates/test-codex/` |

## Tests

Unit (`tests/unit/test_codex_*`, `test_runtime_*`, `test_session_tab_gate_codex`,
`test_platform_prompt_runtime`): JSONL parser → metadata, cost (no reasoning
double-count + cached pricing + default), error→status (pipe-drop 502, generic
500-not-503), capabilities matrix, factory + unknown-runtime validation,
subscription skip, MCP config (+ no dup on restart + token-not-persisted),
**sandbox resolution** (normal → `danger-full-access`, read-only stays, no dead
`network_access` flag), **runtime-aware prompt** (codex omits `mcp__trinity__` +
gets the orientation note, Claude/Gemini unchanged), resume arg-order guard,
Session-tab gate + backend inertness. E2E in `/verify-local`: a real
`AGENT_RUNTIME=codex` agent with an injected `OPENAI_API_KEY`, one `/api/chat` +
one `/api/task` turn (tools create+read a file; MCP `list_agents`).

## Out of scope (fast-follow)

Shared subprocess-helper DRY extraction; Session-tab cached-UUID resume for Codex;
backend reading `ExecutionMetadata.error_code` directly; Codex SSE streaming;
vision/images; a post-creation runtime-switch endpoint.
