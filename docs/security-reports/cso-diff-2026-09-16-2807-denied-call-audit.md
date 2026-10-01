# CSO Diff Audit — 2026-09-16 — abilityai/trinity#2807 (a denied tool call audited as success)

**Mode**: diff (all phases) · **Confidence gate**: 8/10 · **Branch**: `feature/2807-audit-denied-calls` → `dev` · **Skill**: cso v1.1

## Architecture (Phase 0)
The diff lives in the MCP server (`src/mcp-server/`), the TypeScript proxy in front of the FastAPI backend, plus the J10 journey and five docs. Trust boundary under audit: an MCP principal's tool call that the tool-surface gate REFUSES, and the admin-only audit row the server posts for it (`POST /api/internal/audit`, `X-Internal-Secret`). Every gate on the surface returns its denial as JSON (the contract agents parse), and the audit wrapper labelled a call by throw/no-throw — so a refusal was recorded as `success: true` (reproduced live before the fix). The fix is a stamp: `access.ts::accessDenied` writes `context.outcome = {kind: "denied", reason}` on the per-call context and `audit.ts::withAudit` reads it after `execute`; a thrown backend 403 is labelled `denied` too; `createServer` injects the audit URL + secret.

## Attack surface (Phase 1, diff-scoped)
- New endpoints: 0 · New tools: 0 · Docker/compose/CI/env: unchanged · Dependencies: unchanged (lockfile untouched)
- Changed: the audit wrapper, the access helper, 29 deny branches across 10 tool modules (all now serialise through one helper), one `createServer` option, the J10 xfail, docs.
- Unchanged on purpose: which calls are admitted or refused; every tool description; the caller-visible denial envelopes (byte-identical, asserted).

## Findings (Phases 2–12)
**None introduced.** 16 candidates evaluated; all refuted, excluded by rule, or pre-existing and tracked (see `filter_stats.detail` in the JSON).

Key clearances: a tool RESULT cannot set or clear the label (server-set stamp; pinned by test); the stamp lives on the per-call object FastMCP builds inline for every `execute` and never on the shared `session` object (both asserted, including deny→allow on one session over the real transport); the only reader of the row is `/api/audit-log`, admin-gated on every route including `list_audit_log`; reasons are server-composed and carry no credential; the hash chain normalises stored details at write, so additive keys leave prior rows verifiable; secret patterns 0 over the diff and the new test; enterprise-disclosure PCRE replayed with Python `re` over the five changed docs — 0 hits.

## Appendix — pre-existing, tracked
| # | Sev | Conf | Item | Tracking |
|---|-----|------|------|----------|
| A1 | HIGH | 9 | Backend REST routes are owner-equivalent for an agent key; 53 tools carry `baselined` rows | abilityai/trinity-enterprise#629 |
| A2 | MED | 9 | Returned denials audited as successful calls — **RESOLVED here**; residual: id-addressed rows carry no `target_id` | abilityai/trinity#2807 · DEBT_INBOX `2026-09-16-mcp-id-addressed-tools-leave-target-id-empty` |
| A3 | LOW | 8 | `none` shape check does not treat `name` as an agent target | documented in `access.ts` |
| A4 | MED | 9 | A tool that catches a backend error and RETURNS `{error}` is still audited `success: true` | DEBT_INBOX `2026-09-16-mcp-returned-failures-audited-as-success` |
| A5 | LOW | 9 | Refusals visible per row but not listable (`/api/audit-log` filters neither `details.*` nor `event_action`) | DEBT_INBOX `2026-09-16-audit-log-refusals-not-listable` |
| A6 | MED | 8 | No chain-depth guard on agent-to-agent chat chains | abilityai/trinity#2806 |

## STRIDE (MCP server, diff-scoped)
- **Spoofing**: unchanged (key auth); the label is set by server code, never by the caller or the tool result.
- **Tampering**: n/a (rows are written by the server with the shared secret; hash chain unaffected).
- **Repudiation**: IMPROVED — a refusal is now recorded as one (`success: false`, `denied: true`, the reason); a backend 403 surfaced as a throw is marked too. Residual A4 (returned failures) and A2's attribution half.
- **Information disclosure**: the caller's envelope is byte-identical; the internal reason for loop/report/queue denials reaches only the admin-only row.
- **DoS**: excluded by rule. **Elevation**: none — no gate decision changed.

## Data classification (diff-scoped)
RESTRICTED: `INTERNAL_API_SECRET` (now carried in the injected audit config; header-only, never logged) and MCP keys (unchanged). CONFIDENTIAL: audit rows (admin-only) — now also carry the refusal reason, which names agents and loop ids the operator may see. INTERNAL: agent names and permission edges — echoed to a caller only where it supplied the name. PUBLIC (to operator scopes): tool descriptions — unchanged.

## Trend
Prior: `cso-diff-2026-09-15-628-loop-permission-gate` · Resolved 1 (#2807 labelling) · Persistent 3 (ent#629, A3, #2806) · New 0 · Registered residuals 2 (A4, A5 — pre-existing) · **Direction: improving**

## Remediation
No introduced findings; nothing to decide here. A1 and A6 remain with their owners; A2's residual, A4 and A5 are in the debt inbox for `/groom`. Supply chain: the pre-existing moderate `hono` advisory rides dependabot PR #2673.

*AI-assisted diff scan; not a substitute for a professional audit.*
