# CSO Diff Audit — 2026-09-18 — abilityai/trinity#2889 (503 from an exhausted credit balance laundered into a skip)

**Mode**: diff (all phases) · **Confidence gate**: 8/10 · **Branch**: `fix/2889-503-skip-laundering` → `dev` · **Skill**: cso v1.1

## Architecture (Phase 0)
Two halves. Backend: `chat_execution_service` gains one additive response header, `X-Trinity-Error-Code`, on the sync `/chat` and `/task` failure raises — the `TaskExecutionErrorCode` the immediate `/task` path already computed (`TaskExecutionResult.error_code`) and the `/chat` path derives from `agent_status_code` with the same producer-side rule `task_execution_service` applies. Body shapes are byte-identical to before. Test tree: `tests/testkit/readiness.py` (one classifier; header first, transport-vocabulary body fallback, unknown ⇒ FAIL), a `requires_model` marker, a session `model_provider_preflight` fixture and a `pytest_runtest_setup` fast-fail, and 53 call sites converted from a bare `if status == 503: pytest.skip`. Trust boundary under audit: **backend failure response → an already-authorized caller of `/chat`/`/task`**.

## Attack surface (Phase 1, diff-scoped)
- New endpoints: 0 · New tools: 0 · WebSocket: 0 · Docker/compose: unchanged · Dependencies: unchanged · CI: unchanged · Migrations: 0
- Changed response surface: one new header on 4xx/5xx from `POST /api/agents/{name}/chat` and `POST …/task` (both `AuthorizedAgent`-gated; unchanged)
- Header value domain: closed enum (`auth|billing|network|agent_error|capacity|timeout`) — never agent-authored or caller-authored text
- Test-side outbound: one real `/task` probe per session on the runner-provisioned harness agent, only when `TEST_AGENT_NAME` is set
- Docs changed: `architecture/execution.md`, `tests/README.md`, `learnings.md` — enterprise-docs guard replayed (PCRE): 0 hits

## Findings (Phases 2–12)
**None introduced.** 7 candidates evaluated; all refuted or excluded by rule.

Key clearances: the header discloses a coarser class than the prose body the same authorized caller already receives (`Failed to communicate with agent: <agent error text>`), so it widens nothing (`_apply_sub003_autoswitch`, `_map_task_failure`); the value is an enum member's `.value` or nothing — `_error_code_headers` sends no header for any non-string, so a stub or garbage cannot inject a header; the dispatch-breaker 503 keeps its own `X-Circuit-Open` (untouched); evidence in a pytest reason is the agent error text already credential-scrubbed upstream (`scrub_text` at `_parse_agent_http_error`, `sanitize_text` agent-side), bounded to 300 chars, and lands only in test output; the preflight probe is once-per-session, test-env-only, on an agent the runner owns (hard exclusion #1 / #8); `_detail_text`'s regex is linear over a bounded 503 body in test code (hard exclusion #16); secret patterns 0 over the diff; no tracked `.env`; no TLS bypass; no dependency or CI delta.

## STRIDE (sync dispatch failure path, diff-scoped)
- **Spoofing**: n/a — no auth surface touched. **Tampering**: the header is computed server-side from the agent's HTTP status / the result's code; a caller cannot influence it beyond what the body already reflects. **Repudiation**: unchanged (FAILED row + audit as before). **Information disclosure**: an enum class to a caller who already sees the prose — no widening. **DoS**: none (hard exclusion #1). **Elevation**: none.

## Data classification (diff-scoped)
- Agent error text in 503 bodies — INTERNAL (already scrubbed of staged credentials upstream) — unchanged surface; additionally quoted, bounded, in pytest failure reasons (test logs)
- `X-Trinity-Error-Code` — INTERNAL — closed enum on an authorized response

## Trend
Prior: `cso-diff-2026-09-17-2419-utilization-fraction` (0 findings). This diff: **0 findings** — eleventh consecutive clean diff audit. Direction: stable.

> Not a substitute for a professional security audit — an AI-assisted first pass between professional audits.
