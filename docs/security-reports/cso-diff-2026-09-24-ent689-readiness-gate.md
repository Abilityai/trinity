# /cso --diff — ent#689 readiness gate (2026-09-24)

**Scope:** `feature/ent689-readiness-gate` vs `dev`. Daily gate: 8/10.
**Result:** 0 findings (0 critical, 0 high, 0 medium, 0 low).

## Attack surface added
- `GET /api/internal/agents/{name}/brief-readiness`. It is on the internal router, so it inherits the router-level `verify_internal_secret`, and it returns only `{agent_name, fire, reason, basis}`.
- A scheduler→backend call that sends `X-Internal-Secret`, is bounded at 5 s, and fails open.
- Role-card payload:
  - `readiness.source` gains the value `rollout`, with `changed_by` set to `None`.
  - `brief_held` is computed for platform viewers only.
- A data-only rollout seed: static SQL with bound parameters, on both tracks.

## Phases
- **Secrets:** no key patterns in the diff.
- **Enterprise docs guard:** 0 hits.
- **Dependencies, CI and Docker:** unchanged.
- **Injection:** no string-built SQL and no `v-html`.
- **Template parsing:** `template.yaml` goes through the ent#314 safe loader.

## Fixed during the audit
- `brief_held` is now computed for platform viewers only. The claim that it disclosed schedule-derived state to external clients was refuted by an independent verifier: the ent#78 platform-door text covers the executions ledger, not the role card. I applied it anyway as hardening, because a client can neither act on a held brief nor see its schedules.

## Appendix (below the gate)
- **TENTATIVE (6/10):** an *unstamped* companion can exempt itself by removing `x-role`, or by stalling its template read past 3 s, which fails open. An explicit owner stamp is never bypassed. This is governance rather than a security boundary.
- **PRE-EXISTING, out of scope:** the role card sends `readiness.changed_by` (the owner's email for an owner stamp) to external clients. Raised separately.

## Mutations
- The gate letting unstamped companions fire turns 2 tests red.
- The seed overwriting stamps turns 1 test red.
- The scheduler never asking turns 1 test red.
