# CSO diff audit — trinity-enterprise#1001 (2026-10-29)

`cso v1.2.3` · `--diff` daily (8/10 gate) · branch `feature/1001-protect-endpoint-env` vs merge-base `af193c822` (uncommitted working tree) · diff run, not a full run.

## Result

**0 findings** (critical 0, high 0, medium 0, low 0).

One defect found by `/review` before this audit was fixed in the same diff. `strip_protected_env_lines` failed open: if the rewrite of `$CODEX_HOME/.env` failed, for example because the agent had made its own `$CODEX_HOME` read-only, the turn still ran and the CLI loaded the protected line. It now raises, and the Codex turn answers 503 without reaching the CLI. A test pins that. The 503 detail names the file, never a value.

## Checks run

| Check | Result |
|---|---|
| Secret prefixes in the diff and new files | none |
| Concealed Unicode in changed and new files | one literal U+FEFF in a test string (a BOM fixture), replaced with its `﻿` escape; none in shipped files |
| Root exec at boot (`sudo -E /usr/local/bin/python3 -I -S …/write-routing-env.py`) | constant root-owned script; `-I` drops `PYTHON*`, sudo drops `LD_*`; env values reach the drop-in only as JSON strings under fixed key names, so no value can add a key |
| `ANTHROPIC_BASE_URL` / proxy / TLS trust from `~/.env` | refused on every spawn and in the process mirror; container-baseline values still apply; 19 mutations of the fix lines each turn a test red |
| Same names from Claude Code settings `env` (user + workspace `settings.json`) | pinned by root-owned 0444 policy drop-in `60-routing-env.json`; read from the pinned CLI (policy env applied last, `""` falls back to the default) |
| Codex `config.toml` provider / base URL | `-c model_provider="openai" -c openai_base_url="" -c chatgpt_base_url=<default>` on every `codex exec`; read from 0.147.0 source (`-c` above every agent-writable layer; built-in `openai` not redefinable) |
| Codex `$CODEX_HOME/.env` (loaded over the process env) | protected lines removed before each turn; fail closed on a failed rewrite; temp file via `mkstemp`, original mode kept |
| Gemini settings / own `.env` loader | settings carry no endpoint/proxy/env key in 0.62.0; the loader fills only unset names, so every pinnable name is handed to the spawn already set |
| Runtime flip via a mirrored `.env` `AGENT_RUNTIME` | closed: `AGENT_RUNTIME` is protected; scoping reads the container baseline |
| Credential in logs | WARN lines carry names only (tested); the 503 detail carries the path and errno, never a value |
| Invariant #17 | unchanged (new build step runs as root before `USER developer`, like the other `/etc/claude-code` steps) |
| Failure direction (A10) | stated: `.env` refusal is unconditional; the drop-in keeps the image's empty pins if the boot rewrite fails (logged `GUARDRAILS: ERROR`); the Codex strip fails closed on write, and returns `[]` on an unreadable file, which the CLI (same user) cannot read either |

## Considered and dismissed

- Proxy URLs that carry userinfo are written to a world-readable 0444 drop-in. The same values are in the container environment, which every process in the container already reads, so the file adds no reader.
- An agent using passwordless `sudo` can rewrite the drop-in or rerun the writer with a chosen env (the bash guardrail denies `sudo` commands naming `/opt/trinity` and `/etc/claude-code`). This is the stated GUARD-002 limit, not a boundary.
- `$CODEX_HOME/.env` rewritten by a concurrent turn between the strip and the CLI start: a race with no demonstrated exploit (exclusion 6).
- Claude Code provider switches (`CLAUDE_CODE_USE_*`) stay settable by decision. With every base URL pinned, they reach only the provider's official endpoint, using the agent's own cloud credentials.
- `runtime_family()` returns `None` for an unknown runtime, so no runtime-specific names are refused. `get_runtime()` refuses to start an unknown runtime, so no CLI runs with that set.

## Hypotheses (not counted)

- **Gemini's own `.env` loader also refills the execution-class names** (`GIT_SSH_COMMAND`, `NODE_OPTIONS`, …) that `build_execution_env` refuses. This holds when the workspace is trusted, and the agent can make it trusted through its own `~/.gemini/settings.json`. The loader predates this diff. The effect reaches only the Gemini process's own children (tool calls, stdio MCP servers), which the agent can already shape. Confidence 5. A follow-up, because pinning those names empty changes their meaning (`GIT_SSH_COMMAND=""` makes git fail).

## Coverage gaps

- No image was built, and no agent was run. The three CLI behaviours this relies on were read from the pinned versions' source and bundle, not exercised: Claude Code applying the policy `env` over user settings, Codex `-c` precedence, and Gemini's fill-only loader. `/verify-local`'s agent stage and the manual check after the base-image rebuild exercise them.
- Phase 3 scanners: no dependency changed in the diff (not applicable).
- Independent finding verifier: not run, as there were no findings to verify.

## Trend

Extends the closure in `cso-diff-2026-10-06-ent787-guardrail-exec-form`, which used the same two routes (`PROTECTED_KEYS` + policy `env` pin) for one name. The Phase 0 quicklist row "Claude Code in the base image" names `ANTHROPIC_BASE_URL`. That route is now closed for the agent-writable files as well as for template content.
