# /cso --diff — 2026-11-26 — trinity-enterprise#978 items 2–5 (typed refusals, owner-tier names)

**Mode:** diff, daily confidence gate (8/10). **Skill:** cso v1.2.3. **Base:** `dev` at `7ce8eb1d2`.
**Verdict:** one HIGH finding (verified, fixed in this PR), one hypothesis closed defensively in this PR, two coverage gaps.

## What the change does

- A turn whose tier the platform could not tell is answered 502 typed `turn_check_unavailable` by Claude Code, Gemini and Codex; no caller reads it as an auth failure, switches subscriptions or counts it on the AUTH breaker. An older image's 503 is recognised by its sentence.
- `TURN_REFUSED` comes only from a typed `turn_refused` body, never from a bare 403 (push, `/chat`, `/task` and, in this review, the agent's pull worker).
- `visitor_shell` is off, and logged, when the guardrails setting cannot be read (read raises, not JSON, not an object, not a boolean). Missing keeps the default.
- An instruction file name is the instructions class at any depth (`file_path_policy.INSTRUCTION_FILE_NAMES`, three byte-identical copies). `template.yaml` stays top-level only.

## Stack CVE quicklist (diff scope)

| Component | Verdict |
|---|---|
| Claude Code, Codex CLI, Gemini CLI in the base image | pins unchanged by this diff |
| Starlette, Vite, Vitest, axios, Redis, runc, Node CLIs | not in the diff (no dependency, image, compose or workflow change) |

## Attack-surface delta

- **Endpoints:** none added or removed. Enumeration uniformity and the admin-gate allowlist pass.
- **Files API (share tier, agent key):** the instructions class widens to nested names; `.codex/**` joins the files that run code (this review).
- **Agent → backend:** none new. **Backend → agent:** none new.
- **Agent-context ingress:** none new. The refusal and retry texts are platform-authored fixed words (#4284 rule).

## Findings

| # | Sev | Conf | Status | Evidence | Category | Finding | Phase | File:Line |
|---|---|---|---|---|---|---|---|---|
| 1 | HIGH | 8/10 | VERIFIED, FIXED | SUPPORTED | instruction-file-coverage | Codex's `AGENTS.override.md` was not an instruction file name | P7 / P9 A01 | `src/backend/services/file_path_policy.py:126` |

**1. Codex's override file.** OWASP 2025 A01; ASI01 / ASI06.
- **Description.** The pinned Codex CLI (0.147.0) reads `AGENTS.override.md` ahead of `AGENTS.md` in the same folder ("project-document precedence (`AGENTS.override.md`, `AGENTS.md`, then configured fallback filenames)", `codex-rs/prompts/templates/review/rubric.md` at that tag). The name list held `CLAUDE.md`, `CLAUDE.local.md`, `AGENTS.md`, `GEMINI.md`, so `decide(SHARE, WRITE, "AGENTS.override.md")` allowed the write. No later layer refused it: the backend Files path (`_is_user_writable_path`, `_enforce_tier`) and the agent server's floor (`AGENT_SERVER_EDIT_PROTECTED_NAMES`, runtime config) both pass it. The top-level file predates this diff; the diff's any-depth rule made the list the place to fix it.
- **Exploit scenario.** On a Codex agent, a person the agent is shared with writes `/home/developer/AGENTS.override.md` through the Files tab. Their own Codex turns are refused (`turn_scope.require_owner_tier`), but the owner's next turn and the owner's schedules run Codex with `-C /home/developer`, which loads the override instead of the owner's `AGENTS.md` (Trinity's mirror of `CLAUDE.md`).
- **Impact.** The share holder's text becomes the standing instructions of owner-tier turns, which run commands.
- **Fix (this PR).** `AGENTS.override.md` joins `INSTRUCTION_FILE_NAMES` in all three copies, so it is owner-tier to write or delete at any depth and an agent key needs `instructions.manage`. Tests: `test_ent978_typed_refusals_and_owner_names.py` (nested and top level), `test_ent164_self_change_grants.py` rows. Verifier: an independent fresh-context agent tried to refute it and confirmed (8/10).

## Hypotheses (closed defensively, not counted)

- **H1. A project `.codex/config.toml` in the agent home.** Codex 0.147.0's config loader has a `Project` layer (`.codex/config.toml`) above the user config, and that file can declare MCP servers Codex starts as commands. The share tier could write `~/.codex/config.toml`. Not proved: whether Codex applies the project layer to an untrusted folder in `codex exec` was not established from its documentation. Trinity keeps Codex's own state in `CODEX_HOME` (`~/.tmp/codex`, already runtime config), and the endpoint is pinned with `-c`, so `~/.codex` has no platform use. **Closed either way in this PR:** `.codex` joins `RUNS_CODE_DIRS` (owner-tier write, never an agent key's). Tests: `test_ent978…` (`.codex/config.toml`, `.codex/AGENTS.override.md`, folder delete), `test_ent820_files_tier_census.py` row.

## Reviewer notes checked (PR review, informational)

- **I1.** A `CLAUDE.md` inside a skill folder is instructions as well as skills. Kept (the any-depth ruling; Claude Code loads it when a turn works there) and pinned by a `test_ent164` row; a mutation moving the name test inside the skills exclusion turns that row red.
- **I2.** The agent's pull worker mapped a bare 403 to `turn_refused`. Fixed: no 403 entry in `_PULL_STATUS_MAP`; a test pins bare, untyped-dict and typed 403s.

## Checks by phase

- **P2 Secrets:** added lines scanned for known secret shapes, emails, private IPs: none (decorator false positives only). No `.env`, no credential in argv or logs.
- **P3 Supply chain, P4 CI/CD, P5 Docker/compose:** not in the diff.
- **P6 Integrations:** no webhook or channel change.
- **P7 Agentic:** ASI01/ASI06 finding 1 above. ASI03: no route, scope or grant change. ASI09: user-facing texts are fixed platform words.
- **P8 Agent-shipped content:** no invisible or bidi Unicode in any added line.
- **P9 A10 failure directions:** `visitor_shell_allowed` fails closed on an unreadable setting and says so in its docstring; `get_guardrails_config(strict=True)` raises only for the caller that asks. The typed 502 never reads as auth.

## Guards run

`test_ent978_typed_refusals_and_owner_names`, `test_ent164_self_change_grants`, `test_ent820_files_tier_census`, `test_ent820_path_policy_parity`, `test_4284_no_exception_text_in_5xx`, `test_186_enumeration_uniformity`, `test_293_admin_gate_rejects_agent_keys`, the pull-worker suites (`test_1081`, `test_4257`, `test_3012`, `test_3487`), `test_ent926_runtime_turn_scope`, `test_ent926_public_turn_refused`, and every unit file that names the path policy (20 files): pass, except `test_ent926_turn_tier_hook.py`, whose 45 local reds are the known macOS `realpath` mapping of `/home/developer`; re-run with a lexical `resolve`, 172 of 173 pass and the one left is the symlink case that needs `realpath`.

## Coverage gaps

- No Docker pass: image-internal checks were not run, and the agent-side 502 was not exercised in a built image (CI's `prod-image-smoke` covers the build).
- Codex's project-layer trust rule (H1) was not established; the fix does not depend on it.

## Trend

Against `cso-diff-2026-11-23-1176-managed-hooks`: no persistent finding. New: finding 1 (fixed in the same PR).
