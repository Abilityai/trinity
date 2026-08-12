# CSO Diff Audit — trinity#2114 subscription-shadow fix

- **Date**: 2026-08-12 · **Mode**: `--diff` (daily, ≥8/10 confidence gate) · **Branch**: `vybe/issue-2114` (2 commits vs merge-base `a6cf1ad6`)
- **Scope**: 14 files — agent-server `execution_env.py`/`credentials.py`/`models.py`/`main.py`, backend `subscription_auto_switch.py`, 4 test files, registry, 4 docs
- **Verification**: independent fresh-context adversarial pass; every clean verdict carries a refutation cite

## Findings

**None at the ≥8 confidence gate.**

## Clean categories (what was checked)

| Axis | Verdict | Basis |
|---|---|---|
| Secrets in diff | CLEAN | all credential-shaped strings are test fixtures (`sk-ant-oat01-good` style placeholders); `git log -p` over the branch range |
| Agent-influenced control flow | CLEAN | the arm (`arm_subscription_auth_guard`) mutates only the agent's own in-process overrides and only ever force-unsets its own spawns' API-key auth — self-DoS, already within the agent's power via its own `.env`. `clear_runtime_override` has zero callers → the arm is not remotely disarmable; the only HTTP override-mutator (`reload-token`) sets the fixed 2-name allowlist |
| Backend log injection via `env_shadow` | CLEAN | structured JSON logging (`logging_config.py` `json.dumps(log_entry)`) escapes newlines/ANSI into the `message` field; honest servers emit only the 2-element literal constant; shape-validated (list, str items, capped) backend-side |
| Cross-tenant / sibling reach | CLEAN | all new state is per-container process globals; backend helper resolves runtime + token per agent; no shared mutable state |
| Fail-open direction | CLEAN | every added fallback fails toward MORE auth-stripping or LESS diagnostics — never toward re-enabling a shadow or touching a sibling (label-read failure → claude-code → `remove_api_key=True`; env_shadow parse failure → no warning, hot-reload verdict preserved) |
| Info disclosure | CLEAN | names only, never values, everywhere (tests assert value-absence in caplog + response text); reload response reaches only backend + the agent that already owns the `.env`; drift report stays on the owner-gated route |
| Enterprise-docs-guard (ent#45) | CLEAN | touched docs describe OSS SUB-002/003 + the #1999 seam; no `enterprise_*` DDL, catalog, or gating rationale |

## Verdict

Defense-oriented, tightly-scoped change: narrows the agent's ability to shadow its own subscription auth, adds names-only observability, and its one agent-influenced input (`INITIAL_ENV` via the agent-writable override file) affects only that agent's own spawn env. Ship-clean.
