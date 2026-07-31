# CSO Diff Audit — #1860 fleet-restart adoption (2026-07-31)

**Mode**: `--diff` (daily gate, 8/10) · **Branch**: `feature/1860-fleet-restart-adoption` vs `dev@5b289996`
**Scope**: `src/backend/routers/ops.py`, `src/backend/services/agent_service/lifecycle.py`,
`tests/unit/test_1860_fleet_restart_adoption.py`, 3 doc files, `tests/registry.json`

## Architecture delta (Phase 0)

`POST /api/ops/fleet/restart` stops routing each agent through raw `container_stop`+`container_start`
and instead delegates to the new `lifecycle.restart_agent_internal` (explicit stop → full
`start_agent_internal` path). No new endpoints, no new input paths (same two query params), no new
network flows. One new Redis key (`ops:fleet_restart`, backend-side, SETNX single-flight lock).
Trust-boundary movement is **inward**: the endpoint's effective privilege escalated from
restart-in-place to container replacement, and the diff re-prices principals accordingly.

## Attack-surface census (Phase 1, diff)

| Category | Delta |
|---|---|
| Endpoints | 0 new; 1 hardened (`/api/ops/fleet/restart`) |
| Auth gates | +`reject_agent_principal` (ops.py:260, beside `assert_admin` ops.py:256) |
| Concurrency | +single-flight SETNX lock, 409 `fleet_restart_in_progress` (ops.py:270-273) |
| Redis keys | +`ops:fleet_restart` via `get_breaker_redis` (backend ACL user, data-ops only) |
| New inputs / uploads / WS / integrations | none |

## Findings

**None at the 8/10 gate.** (Zero findings also below the gate worth an appendix.)

## What was checked (clean categories, with evidence)

- **Secrets archaeology (P2, diff)**: pattern scan of the working-tree diff + new test file —
  clean. No enterprise paid-feature disclosure in the 3 doc edits (mechanism-level #1860 text only).
- **A01 access control**: gate is *strengthened* — `assert_admin(current_user)` (ops.py:256) +
  `reject_agent_principal(current_user)` (ops.py:260), the Invariant #8 escalation rule for
  endpoints that replace containers (#1816 precedent; trinity-ops-agent#232 class). Live-verified:
  agent-scoped key → 403 `"This operation is human-only"`. System-scoped principals unaffected
  (`User.agent_name` set only for scope="agent") — the documented system-agent use keeps working.
  No enumeration surface (no agent-name path param; fleet op, admin-only).
- **Agent-key self-boundaries (P6)**: n/a in reverse — the endpoint now rejects ALL agent keys.
- **A03 injection**: no SQL, no subprocess, no template rendering in the diff. Ephemeral check uses
  the existing parameterized accessor (`db.get_agent_ephemeral_info`).
- **Concurrency/lock correctness (P5)**: SETNX+TTL with own-lease refresh; release is
  compare-and-delete (ops.py:427) so a second runner's lock can never be deleted by the first;
  worst-case anomaly of the unconditional per-iteration `expire` is extending an *active* lock's
  TTL — protection-extending, not protection-breaking. Fail-open on Redis down matches the
  platform's uniform breaker/limiter convention (fail-closed would make a Redis outage block all
  fleet ops). Live-verified: concurrent second call → 409.
- **Credential exposure (P2/P7)**: per-agent results carry injection *status strings* only
  (`"skipped"`/`"success"`...), never the service result dicts; audit `details` carries counts,
  agent names, and platform-enum recreate reasons (`recreated_map` ops.py:286/380,
  `failed_agents` ops.py:287) — no free-form agent-authored text, no secrets. Exception text in
  per-agent `error` fields is served only to the admin caller (same practice as the single-agent
  stop endpoint's 500 detail).
- **A09 logging/monitoring**: improved — restores the `fleet_restart` audit entry dropped in
  `0ec3a7fc`, now partial-completion-safe with per-agent recreate traceability.
- **LLM/AI surface (P7)**: none touched — no prompts, no MCP tool changes, no advertised
  descriptions, no agent-reachable surface (lock key lives on the platform-side Redis the agent
  network cannot route to, #589).
- **CI/CD, Docker, dependencies**: not in diff; no workflow/compose/requirements changes.

## Residual risks (accepted, documented — not findings)

1. **Lock fail-open when Redis is down** — overlapping fleet restarts become possible again in
   that degraded state; consistent with every other Trinity breaker/limiter and documented in code.
2. **Sequential loop duration** — an operator-experience concern (504 with server-side
   completion), mitigated by the audit-as-durable-record + docstring + chunking params; DoS-class
   concerns are per skill policy out of scope.

## Verdict

**PASS** — the diff is a net security improvement: one endpoint's principal surface narrowed
(agent keys excluded), a concurrency wedge closed (single-flight lock), and an audit gap restored.
No findings to remediate.
