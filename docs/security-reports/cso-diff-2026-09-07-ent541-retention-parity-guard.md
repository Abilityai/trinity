# CSO diff audit — abilityai/trinity-enterprise#541 (`feature/541-retention-parity-guard`, private submodule)

**Date**: 2026-09-07 · **Mode**: `--diff` (daily gate 8/10) · **Base**: merge-base `2019035` on enterprise `main` (public backend at `dev` `bd2d2451`) · **Diff**: 3 files in the private submodule, +54 / −17 (working tree, pre-commit) — a test helper, one doc line, one CI trigger. No OSS code.

## Verdict
**No findings.** The diff introduces no route, tool, schema, credential, container, or network change. Two observations recorded below the gate, neither a vulnerability.

## Attack surface introduced by the diff
- **None.** `tests/test_retention.py::_oss_ops_settings_defaults` now parses `src/backend/config.py` instead of `services/settings_service.py` for the `OPS_SETTINGS_DEFAULTS` literal (the definition moved in #2432 and left a re-export). Still `ast.parse` + `ast.literal_eval` over a fixed repo-relative path — no import, no code execution, no caller-controlled input.
- `.github/workflows/enterprise-tests.yml` gains a nightly `schedule:` trigger. Same job, same `permissions: contents: read`, no `pull_request_target`, no `${{ github.event.* }}` interpolation into `run:`, no new secret; the per-run `CREDENTIAL_ENCRYPTION_KEY` remains generated in-job and never echoed.
- `docs/memory/feature-flows/retention.md` (private): one citation repointed by symbol.

## Findings
| # | Sev | Conf | Status | Category | Finding | Phase | File:Line |
|---|-----|------|--------|----------|---------|-------|-----------|
| — | — | — | — | — | none at the gate | — | — |

## Verification performed
- Secrets: known-prefix scan (AWS, OpenAI/Anthropic, GitHub token/PAT, Slack, `trinity_mcp_`), local-path and email patterns over every added line of the merge-base diff — 0 matches. No `.env`, Dockerfile or compose change.
- Enterprise-disclosure guard: the diff lives in the private repo; the only public-repo artefacts of this work are this report and a ledger update, both kept at the generic "a module in a separate repo mirrors an OSS constant" altitude already used by `docs/memory/learnings.md` (2026-07-16).
- CI/CD (P4, in-diff): trigger-only change; no action `uses:` line changed, so the pre-existing unpinned first-party `actions/*` references are out of diff scope (see O1).
- Code (P7/P9): `literal_eval` on a trusted repo file cannot execute code; a computed value now fails as a named `AssertionError`; `read_bytes()` makes the parse locale-independent. Probe against temp files: re-export-only → "not found … must be repointed (#541)"; computed value → "no longer a literal dict"; bare annotation then definition → returned; `AnnAssign` with value → returned; non-ASCII bytes → returned; missing file → names the path. All six as designed.
- Suite: enterprise tests under the CI environment, `--randomly-seed=106` and `12345` → 317 passed, 5 skipped, 0 failed (baseline before the fix: 316 / 1).
- Independent verification: no finding survived the gate, so no verifier subagent was needed.

## Observations (below the gate)
| # | Sev | Conf | Observation |
|---|-----|------|-------------|
| O1 | INFO | 5/10 | `actions/checkout@v7` and `actions/setup-python@v7` are tag-pinned, not SHA-pinned — first-party, pre-existing, unchanged by this diff (FP rule: first-party unpinned is MEDIUM at most, and only in a full audit). |
| O2 | INFO | 6/10 | A scheduled run's failure notification goes to the user who last modified the cron line, not to a team channel; the nightly detector therefore needs that person, or a later notification hook, to be read. Operational, not a vulnerability. |

## Trend
Prior report `cso-diff-2026-09-07-ent536-canvas-vocabulary`: different surface (canvas rendering); nothing resolved, persistent, or new on this diff.
