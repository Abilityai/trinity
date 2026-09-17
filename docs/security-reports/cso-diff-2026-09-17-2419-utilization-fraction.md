# CSO Diff Audit — 2026-09-17 — abilityai/trinity#2419 (utilization header read as a fraction)

**Mode**: diff (all phases) · **Confidence gate**: 8/10 · **Branch**: `feature/2419-utilization-fraction` → `dev` · **Skill**: cso v1.1

## Architecture (Phase 0)
The diff lives in the backend's subscription headroom service (`src/backend/services/subscription_headroom_service.py`), the module that probes the provider with the operator's own subscription token and reads the `anthropic-ratelimit-unified-*` response headers into a Redis snapshot, a history row, the ent#434 weekly alert, the #2409 auto-switch ranker and two UI surfaces. Trust boundary under audit: **provider response → parser → every consumer**. The provider is api.anthropic.com over TLS at a pinned URL; no user-controlled input reaches the parser. The change is a unit fix (fraction × 100, always), a finiteness/range guard on the scaled value, and one INFO log line when a window reads past its cap.

## Attack surface (Phase 1, diff-scoped)
- New endpoints: 0 · New tools: 0 · WebSocket: 0 · Docker/compose: unchanged · Dependencies: unchanged · CI: unchanged
- Changed input path: one pre-existing header, one caller (`parse_unified_headers`), one ingestion point (`_probe`)
- New log site: `_log_past_cap` — subscription UUID + five rate-limit **response** headers; the request's bearer token is never passed (`_probe` hands it `resp.headers`)
- Docs changed: `feature-flows/subscription-usage-tracking.md`, `learnings.md` — enterprise-docs guard replayed (PCRE via python `re`): 0 hits

## Findings (Phases 2–12)
**None introduced.** 12 candidates evaluated; all refuted, excluded by rule, or pre-existing (see `filter_stats.detail` in the JSON).

Key clearances: the log line cannot carry the token (response headers only, by construction at `service.py:271`); `float()` is not an eval and every non-finite/negative result is dropped **after** scaling, so the diff strictly narrows what reaches Redis, SQLite/PG and the operator queue (the old code could persist `NaN`/`Infinity`); every consumer of a value past 100 is monotone or clamps geometry only; secret patterns 0 over the diff; no tracked `.env`; no TLS bypass; no dependency or CI delta.

## Appendix — pre-existing, tracked
| # | Sev | Conf | Item | Tracking |
|---|-----|------|------|----------|
| A1 | LOW | 8 | `overage_status`/`unified_status` are parsed and persisted but read by no predicate; a subscription serving on overage may carry `unified-status: rejected` + `overage-status: allowed` — a truthfulness gap on the badge/alert, not a boundary crossing | follow-up issue, filed after the PR (plan-gate ruling 2026-09-17) |

## STRIDE (headroom service, diff-scoped)
- **Spoofing**: n/a — the probe authenticates with the operator's own token, unchanged. **Tampering**: the parser now rejects malformed numbers instead of storing them. **Repudiation**: a past-cap reading is now logged with its raw inputs. **Information disclosure**: the new log line names a subscription UUID and rate-limit figures — INTERNAL data, no credential. **DoS**: none (hard exclusion #1; a probe is ≥60 s apart per subscription). **Elevation**: none — no auth surface touched.

## Data classification (diff-scoped)
- Subscription token — RESTRICTED — read in `_post_probe` only, never logged (unchanged)
- Utilization / window status / reset instants — INTERNAL — Redis snapshot (7-day TTL), `subscription_headroom_history` (30-day retention), operator-queue alert bodies, admin-only UI
- Subscription UUID / name — INTERNAL — admin-only surfaces and backend logs (already present in the #471 warning lines)

## Trend
Prior: `cso-diff-2026-09-15-628-loop-permission-gate` (0 findings). This diff: **0 findings** — tenth consecutive clean diff audit. Direction: stable.

> Not a substitute for a professional security audit — an AI-assisted first pass between professional audits.
