# CSO Audit — `--diff` — #1927 background polls re-flash loaded content

- **Skill**: cso v1.1 (daily mode, 8/10 confidence gate)
- **Date**: 2026-08-21
- **Scope**: `--diff` — branch `feature/1927-poll-reflash` vs `75cf605d`
- **Phases run**: 0, 1, 2, 3, 5, 7, 9, 10, 11, 12, 13, 14 (4, 6, 8 no-op — nothing in the diff touches CI, webhooks, or skills; the `.claude` pointer move is one `DEBT_INBOX.md` append)
- **Result**: **0 findings.** Frontend-only render-gate change; no new endpoint, input path, credential path, or dependency.

## Phase 0 — Mental model (diff-scoped)

The diff changes *when* four Vue surfaces render their loading / failed / empty
branches, and moves one expansion rule into the operator-queue store. Every
network call it touches is an **existing, authenticated** call to an existing
endpoint with the same headers; what changes is which template branch the
response drives and how a failed refresh is presented (a sibling `InlineError`
banner instead of a spinner). The only new module is a pure decision helper
(`utils/loadingState.js`) with no I/O. Trust boundaries are unchanged.

| | Before | After |
|---|---|---|
| Background poll response | swaps data for a spinner | swaps values in place |
| Failed refresh with data on screen | spinner (or silent) | stale banner, data kept, `detail` = `apiErrorMessage` text (the existing #1926 surface) |
| Failed first fetch | empty copy ("No agents found" / "No executions yet") | `LoadFailed` with Retry |
| `/m` operator-queue + execution-stats responses | parsed as arrays → TypeError every poll | shape-normalized (`listFrom`) |

## Phase 1 — Attack surface census (diff)

| Category | Count | Delta |
|---|---|---|
| Public/unauthenticated endpoints | 0 | 0 |
| Authenticated endpoints | 0 added / 0 changed (same `axios` calls to `/api/operator-queue`, `/api/notifications`, `/api/ops/fleet/status`, `/api/agents/autonomy-status`, `/api/agents/execution-stats`, `/api/agents/{name}/schedules/{id}/executions`, `/api/agents/{name}/info`) | 0 |
| Admin endpoints | 0 | 0 |
| Auth dependencies added/removed | 0 | — |
| File upload points / WebSocket channels / background jobs | 0 | — |
| CI workflows / Docker / compose | 0 changed | — |
| Dependencies | 0 added (`package.json` + lockfile untouched; `@playwright/test` / `vitest` already present) | — |
| New files | `utils/loadingState.js` (pure), `scripts/scan-loading-gates.mjs` (repo-local CLI, reads `.vue` files, writes a baseline only with `--baseline`), `loading-gate-baseline.json` (file paths + counts), 3 vitest specs, 1 Playwright spec | — |

## Phase 2 — Secrets archaeology (diff)

- `git diff 75cf605d` scanned for AWS / OpenAI-Anthropic / GitHub / Slack / Resend prefixes and PEM headers: **0 matches**.
- Added lines scanned for `password|secret|api_key|token = "…"` assignments: **0 matches**.
- The Playwright spec reads `ADMIN_PASSWORD` from the environment via the shared `auth.setup.js` / `tokenFromStorageState` flow (the house pattern); it logs no credential, and `e2e/.auth/` is gitignored.
- No `.env` / config files in the diff.

## Phase 3 — Dependency supply chain (diff)

No dependency or lockfile change. The host `pnpm install` used to run Playwright locally wrote a transient `pnpm-lock.yaml` that was deleted and is not in the diff.

## Phase 5 — Infrastructure shadow surface (diff)

No Dockerfile, compose, Redis, network or base-image change. Vendored-copy parity (Invariant #5) is untouched — no file under `docker/base-image/` or `src/backend/` is in the diff.

## Phase 7 — LLM & AI security (diff)

- **Unsanitized output**: no `v-html` / `innerHTML` / `eval` in the diff. Every new string reaches the DOM through Vue text interpolation: `staleBannerMessage(...)` (subject + a locale time), `detail` (`apiErrorMessage(...)` — server error text, the pre-existing #1926 surface), the `LoadFailed` titles/messages (static).
- No prompt, tool-schema, MCP-description or agent-container change.

## Phase 9 — OWASP Top 10 (diff)

- **A01** — no new route, no ownership change; the same authenticated calls with the same headers. `QueueCard` gains `data-testid` / `aria-expanded` / an `aria-label` only.
- **A03** — no SQL, no subprocess, no `v-html`.
- **A05/A07** — no config, session or token handling touched.
- **A08** — the ratchet baseline is data (paths + integers) read by a test; the scanner CLI only writes when explicitly asked with `--baseline`.
- **A09** — no logging change beyond `console.error` on failed fetches (already present) — error *text* only, never request bodies or credentials.

## Phase 10 — STRIDE (diff-scoped)

| Component | S | T | R | I | D | E |
|---|---|---|---|---|---|---|
| Frontend (four surfaces) | — | — | — | no new data exposed; error text already shown pre-diff | a failed poll no longer blanks the surface (reduced user-facing DoS) | — |
| `stores/operatorQueue.js` | — | client-state only (`autoExpandArmed`) | — | — | — | — |
| Tests / scripts | — | — | — | `tokenFromStorageState` reads the gitignored storage state (existing helper) | — | — |

## Phase 11 — Data classification (diff)

- **INTERNAL**: `lastLoadedAt` timestamps, `hasLoaded` / `fetchError` flags, the ratchet baseline (file paths + counts).
- **CONFIDENTIAL (pre-existing surface, unchanged class)**: `detail` = server error text in the stale banner — the same text #1926 already shows in `LoadFailed` / `InlineError`.
- No RESTRICTED data (credentials, tokens) is introduced, read, stored or logged.

## Phase 12 — FP filtering + verification

Zero candidate findings reached the 8/10 gate; no verifier subagents were needed. Variant analysis n/a.

## Phase 13 — Findings

```
SECURITY FINDINGS
═════════════════
(none)
```

## Phase 14

This report. No JSON written (diff run, zero findings — matches the prior `cso-diff-*` reports).
