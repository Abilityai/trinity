# CSO diff audit — abilityai/trinity#2705 (`feature/2705-e2e-nightly-red`)

**Date**: 2026-09-11 · **Mode**: `--diff` (daily gate 8/10) · **Base**: merge-base `682fce30` on `dev` · **Diff**: 8 public files (+447 / −17), staged pre-commit: one dev-server config change (`src/frontend/vite.config.js`), one new dev script (`src/frontend/scripts/scan-dynamic-imports.mjs`), one new vitest guard, one rewritten Playwright spec, four docs. No backend, scheduler, MCP-server, Docker, compose, workflow, dependency or lockfile change.

## Verdict
**No findings at the gate.** The diff adds no endpoint, no query, no credential path, no prompt surface and no network reach. Six candidates were examined and refuted or filed to the appendix.

## Attack surface introduced by the diff
- **Endpoints / DB / Redis / prompts**: none.
- **Dev server**: `optimizeDeps.include: ['mermaid', 'qrcode']` — a pre-bundling instruction for the Vite dev server only. The CSP block in the same file is untouched (no `Content-Security-Policy` line in the hunk; `tests/unit/test_1400_csp_blob_preview.py` parity guard: 3 passed). No effect on `npm run build` or the production nginx image.
- **Dev script**: `scan-dynamic-imports.mjs` is a read-only file walk over `src/` (no `child_process`, no network, no writes); it runs under vitest and from the CLI in a developer or CI shell.
- **Tests**: the guard spec resolves the Vite config through Vite's public `resolveConfig`; the e2e spec reads geometry inside one `page.evaluate` against mocked routes.

## Findings
| # | Sev | Conf | Status | Category | Finding | Phase | File:Line |
|---|-----|------|--------|----------|---------|-------|-----------|
| — | — | — | — | — | none at the gate | — | — |

## Verification performed
- **Secrets / enterprise disclosure (P2)**: known-prefix scan (AWS, OpenAI/Anthropic, GitHub, Slack, Trinity MCP keys, password/secret/token assignments) over every added line — none. No `.env` tracked. The enterprise-docs-guard pattern (run through Python's regex engine, since the macOS grep has no `-P`) matches no added line; the only URL-shaped string is the `http://x` placeholder inside a code comment.
- **Supply chain (P3)**: no dependency, lockfile or install-script change; the two pre-bundled packages were already direct runtime dependencies at unchanged versions.
- **CI/CD (P4)**: no workflow change; the guard runs inside the existing `frontend-build` job, which already executes `vite.config.js` via `npm run build`.
- **Infrastructure (P5)**: no Dockerfile, compose, network or Redis change; the dev container consumes the config through its existing bind mount and writes the pre-bundle into its anonymous `node_modules` volume as before.
- **Webhooks / integrations (P6)**: none in the diff.
- **LLM / AI (P7)**: `mermaid` pre-bundling changes when the module is bundled, not how it is invoked — `CanvasDiagram.vue`'s `securityLevel: 'strict'` initialisation and the DOMPurify `sanitizeSvg` pass are outside the diff and unchanged.
- **Skills (P8)**: no `.claude` change is part of this PR (the working-tree gitlink drift is left unstaged by design).
- **OWASP (P9)**: A03 — no command, SQL or template construction; the script builds no shell commands and the spec's `evaluate` closure takes element handles, not strings. A05 — CSP unchanged (above). Nothing else applies to a config-and-tests diff.
- **Standing guards on this tree**: `tests/unit/test_1400_csp_blob_preview.py` (3 passed); frontend `npm run test:unit` 2705 passed with the new guard included (one file fails only for the known host-only missing `@vue/compiler-sfc`).

## Appendix (≤4/10, non-blocking)
1. `scan-dynamic-imports.mjs` follows symlinks while walking `src/` — repository-controlled input in a dev/CI shell (hard exclusion 5).
2. Lazy-quantifier regexes in the scanner run on repository source only (hard exclusion 16).
3. Pre-bundling `mermaid`/`qrcode` has no production effect and does not change their call sites.
4. The guard executes `vite.config.js` in CI, where the same job already executes it.
5. Playwright handle passing and reduced-motion emulation are test-only (hard exclusion 8).
6. Added docs name OSS components and public issue numbers only.

*AI-assisted scan, not a professional audit.*
