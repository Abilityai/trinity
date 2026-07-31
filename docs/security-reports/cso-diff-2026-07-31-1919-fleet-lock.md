# CSO Diff Audit — #1919 fleet-restart lock hardening

**Date:** 2026-07-31 · **Mode:** daily (8/10 gate) · **Scope:** `--diff`, `feature/1919-fleet-lock-hardening` vs merge-base `a4eac445` (dev)
**Verdict:** ✅ **PASS — zero findings** (trend: flat vs this morning's #1860 diff audit, 0 → 0)

## Diff surface

7 files (+388/−18): `routers/ops.py` (ownership-checked lease gate, TTL 900→2100, acquire-inside-try, partial-run honesty fields), `redis_breaker_util.py` (+`lock_token_matches` helper), the #1860 unit-test file (+11 tests), 4 docs. One endpoint changed (`POST /api/ops/fleet/restart`), zero new endpoints, zero new inputs.

## What was checked (and the evidence)

- **Auth boundaries (A01):** `assert_admin` (`ops.py:256`) and `reject_agent_principal` (`ops.py:265`) are untouched context — **0 auth-gate lines among the 98 added lines** (verified by grepping the diff hunks). New `processed`/`stopped_early`/`lease_reacquired`/`error` fields reach only the admin-only response and the admin-only audit log.
- **Secrets (P2):** pattern grep over every added line (AWS/OpenAI/GitHub/Slack/whsec/private-key/password/secret assignments) — clean; the only matches are the deliberate test fixtures `intruder-token`/`intruder-secret-value` in the test file (FP rule 8). Enterprise-docs guard: no seam files touched, doc edits carry no paid-module tokens.
- **Exposure discipline (A09):** loss/re-acquire warnings log token *state* (`"foreign"`/`"absent"`) and counts, never values (`ops.py:343-350`) — regression-locked by `test_1919_loss_warning_names_state_not_token_values`; abnormal-exit audit stores `e.__class__.__name__` only (`ops.py:497-498`, the #1912 stack-trace-exposure rule); the HTTP response on abnormal exit is the framework 500 with no exception content.
- **Redis surface (P5):** new commands (GET / SET-NX / EXPIRE / DEL) run under the existing `backend` ACL user's data-ops grant; the key is a module constant — no user-controlled key material; agents cannot reach Redis (#589 two-network invariant, untouched).
- **Injection (A03):** no SQL, no subprocess, no user input in any new path.
- **No surface in diff:** dependencies, CI/CD, LLM/prompt, skills, webhooks, internal endpoints, agent-key self-boundaries, vendored parity files.

## Candidates considered and discarded (2)

1. `lock_token_matches` uses `==`, not a constant-time compare — **not a finding**: server-internal comparison of a server-minted uuid4 nonce guarding a 409; the attacker neither controls nor observes per-byte timing, and the token is not an authentication credential.
2. Forcing lease-loss aborts as a DoS primitive — hard exclusion 1, and it requires Redis write access (platform network + ACL password), which is already game-over.

*AI-assisted scan, not a substitute for a professional audit.*
