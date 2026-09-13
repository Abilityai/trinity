# CSO diff audit — abilityai/trinity#2337 (`feature/2337-journey-invariants`)

`cso v1.1` · mode `--diff` · confidence gate 8/10 · 2026-09-12

## Verdict

**Clean.** The diff adds no attack surface: four documentation files, two new test modules, one existing test module, a journey record, a test-registry entry, and a **comment-only** hunk in `src/backend/services/canary_alerts.py`. Zero findings in scope.

## Attack surface introduced by the diff

| Category | Count |
|---|---|
| New / changed endpoints | 0 |
| New DB reads / Redis keys | 0 |
| New or widened prompt surfaces | 0 |
| Dependency / workflow / Docker / compose changes | 0 |
| New test files | 2 (`tests/unit/_invariant_catalog.py`, `tests/unit/test_2337_invariant_namespace.py`) — read repo-tracked files via `re`/`ast`, import nothing from `src/` |
| Doc files | 4 |

## Findings

None in scope.

## Verification performed

- **Secrets (P2):** 0 secret-shaped strings (`sk-`, `ghp_`, `xox*`, `AKIA`, `trinity_mcp_*`) in the additions; no commits on the branch yet, so the working-tree diff against merge-base `7a40408b4` was scanned.
- **Enterprise-disclosure guard (P2, trinity-enterprise#45):** the exact PCRE from `.github/workflows/enterprise-docs-guard.yml` run over every added line of the five changed docs/seam-adjacent files — **CLEAN**. The new catalog families name OSS-core tables only.
- **P3–P8:** not applicable — no dependency, CI, Dockerfile/compose, webhook, skill or LLM-surface files changed (stated, not skipped).
- **P7 note:** the new `IA-01` entry documents the `X-Source-Agent` trust boundary. That text discloses nothing new — the same fact is a public code comment (`routers/chat.py`) — and every new predicate reads counts/status only, never `.env` bytes or `env_drift`, carrying the E-04/G-04 "reason code + ids only" rule.
- **P9–P11:** tests and docs are hard-excluded (#8, #12); the sole code hunk is a comment. STRIDE over the changed surface: the guard reads tracked files; no tampering or disclosure path.
- **P12:** seven candidates screened; six cleared in-context; one survived and was handed to a fresh-context verifier instructed to refute it (see appendix).

## Appendix (non-blocking)

- **A1 — OUT OF `--diff` SCOPE, VERIFIED, MEDIUM (9/10).** Pre-existing code, surfaced by the diff's `IA-01` analysis: audit-log actor attribution on the chat / task / fan-out dispatch paths trusts a raw client header when the principal is not an agent-scoped key. Exploit specifics are deliberately withheld from this public file and routed privately per `DEVELOPMENT_WORKFLOW.md` → Repository Routing (`--private` for vulnerabilities before disclosure). Filed privately as abilityai/trinity-enterprise#614 (operator ruling 2026-09-12); deferred to its own PR — out of #2337's scope.
