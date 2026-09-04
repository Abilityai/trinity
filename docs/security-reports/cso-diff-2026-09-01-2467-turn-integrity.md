# CSO Audit — /cso --diff — 2026-09-01 — #2467 turn-integrity derivation

**Mode**: diff (branch `feature/2467-bg-kill-visibility` vs merge-base `a1f3a6e4` on `dev`)
**Skill**: cso v1.1 · **Confidence gate**: 8/10 (daily)
**Result**: **0 findings.** No new attack surface; one new agent-influenced data path, contained by design and verified.

## Scope (the diff)

Backend-only change for abilityai/trinity#2467: `services/execution_integrity.py` (new pure
leaf) derives structured kill records + a response notice from the transcript's task
lifecycle events inside `apply_result`; new nullable `schedule_executions.turn_integrity`
column (dual-track migration SQLite `execution_turn_integrity` + Alembic
`0049_execution_turn_integrity`); surfaced on `ExecutionSummary` / `ExecutionResponse` /
`FleetExecutionSummary` + both explicit list SELECTs. Zero changes under
`docker/base-image/`, `docker/`, `.github/workflows/`, `src/frontend/`, `src/mcp-server/`,
dependencies, or compose.

## Attack-surface delta

- **Endpoints**: 0 new, 0 modified auth paths. The new field rides existing access-scoped
  reads (`accessible_agent_names` on the fleet list; `AuthorizedAgent` tiers on per-agent
  routes) — readable by exactly the principals who can already read the same row's
  `execution_log`, which contains strictly more.
- **New data path (the one thing to audit)**: CLI/agent-authored stream events →
  backend-derived JSON column + notice text → stored response / #1578 event summary /
  channel completion report / future UI.

## Phase highlights (diff-constrained)

- **P2 Secrets**: no secret-prefix patterns, no host paths (`/Users/…`), no real emails in
  additions (tracked diff + all 4 untracked files). The `/home/developer/...output_file`
  string appears only inside test fixture data, and a dedicated privacy test asserts it is
  never persisted. Enterprise-docs guard: no paid-feature/module tokens in the doc changes.
- **P3/P4/P5/P6/P8**: no dependency, workflow, docker/infra, webhook/integration, or skill
  changes in the diff — nothing to assess. Vendored-copy parity (Invariant #5) untouched
  and re-verified green (`test_credential_paths_parity`, `test_model_context_parity`).
- **P7 LLM/AI — the substantive phase**:
  - *Injection containment at the sink*: persisted entries are whitelist-shaped —
    `task_id`/`task_type` charset-validated (`^[A-Za-z0-9._:-]{1,128}$`, failures replaced
    with `invalid`/`unknown`, never dropped), `was_backgrounded_by`/`final_status` closed
    enums, `end_time` int-coerced, list capped at 20; `description`/`summary`/
    `output_file`/`command` are structurally unreachable (the #2127 privacy rule, the
    canary G-04 class). The serialized JSON additionally passes `sanitize_execution_log`
    (same boundary treatment as `execution_log_json`/`tool_calls_json`).
  - *Notice text*: static template + count + validated tokens only; task ids never enter
    prose; unknown stream statuses are logged with `%r` (repr escapes newlines — no log
    forging) and truncated.
  - *Forged-stream analysis (#640: an stdio MCP child shares the pipe)*: a compromised
    agent can forge kill events and flag its own runs — but it already controls the entire
    response text flowing to the same surfaces (stored response, event summary, channel
    report), so forgery adds zero capability. No cross-agent or cross-tenant reach: the
    scan runs per-execution over that execution's own transcript. Equal trust, no
    escalation — not a finding.
  - *Cost*: pure O(n) scan, no LLM calls, no new spend surface.
- **P9 OWASP (scoped)**: A01 — no new routes, enumeration-uniformity guard green; A03 —
  the fleet SELECT gains a literal column name (named binds unchanged), migrations are
  static DDL, the SQLAlchemy write uses a values dict; A08 — derive only serializes
  (json.dumps), no new deserialization of untrusted input; A09 — belt logs carry exception
  text only, no user content; A10 — no outbound requests added.
- **P10 STRIDE delta**: Tampering — an agent can tamper only with its own flag (equal
  trust, above); Repudiation — improved: the kill now leaves a durable structured record;
  Information disclosure — none (structural fields, no PII/content, same principal scope);
  EoP — none (no new writer authority; the column rides the existing CAS'd terminal write,
  conditional kwarg protects the FAILED→SUCCESS resurrect path).
- **P11 Data classification**: `turn_integrity` = INTERNAL (structural execution metadata;
  ids/types/enums/timestamps only).

## Verification evidence

- 37 feature tests green, incl. forged-id containment, privacy (description/output_file
  never persisted), cap, malformed-shape degradation, facade signature parity.
- Always-run guards green: `test_186_enumeration_uniformity`,
  `test_agent_auth_header_guard`, `test_2094_dependency_path_param_pairing`, vendored
  parity ×2 (42 tests).
- Live E2E on the dev instance: flagged record verified end-to-end (fleet list +
  notice + proof-file absent); healthy runs byte-identical.

## Residual risks (accepted, documented — not findings)

1. A compromised agent can false-flag its own executions (integrity noise, no privilege
   gain — same trust level as authoring its own response).
2. The sync `/task` HTTP passthrough body shows the agent's raw response without the
   backend notice; every stored/downstream surface carries it (documented in the flow doc).
3. Detection keys on CLI 2.1.235 stream vocabulary; a future CLI rename degrades to
   today's silence (fail-safe direction), with unknown statuses logged once for
   vocabulary learning.
