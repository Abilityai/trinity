# /cso --diff — #2392 platform-injected execution id (2026-09-29)

**Scope:** `feature/2392-inject-execution-id` vs `dev`. Daily gate: 8/10.
**Result:** 0 findings (0 critical, 0 high, 0 medium, 0 low).

## Attack surface added
- **No new endpoint.** One new inbound header on the MCP server, `X-Trinity-Execution-Id`, read in `authenticate` on the validated-API-key path only. It is parsed against `^[A-Za-z0-9_.:-]{1,128}$`; anything else is dropped, never an error. The value is forwarded as the existing `execution_id` body field of five effect tools.
- **Agent MCP config shape.** The Trinity entry gains one header whose value is the literal `${TRINITY_EXECUTION_ID:-manual}` (Codex: `env_http_headers` naming the env var). The value carries no secret.
- **Validator.** `_is_canonical_trinity_entry` accepts one additional header set: exactly `{Authorization, X-Trinity-Execution-Id}` with that exact literal. The Bearer regex still applies. Both vendored copies are byte-identical (`cmp`).
- **Refusal path.** `EffectUnguardedError` → 422 `effect_unguarded` in five routers, plus a budgeted operator alarm.

## Phases
- **Secrets:** no key patterns in the diff or the new files.
- **Enterprise docs guard:** 0 hits across changed docs.
- **Dependencies, CI, Docker:** unchanged.
- **Vendored parity (Invariant #5):** `mcp_validator.py` copies identical; parity test green.
- **Internal route:** `/api/internal/agent-files/share` stays on the `verify_internal_secret` router; the change only maps a new exception to 422.
- **Spoofing / A01:** the header cannot claim another agent's execution. `resolve_and_validate_execution` checks the execution's `agent_name` against the route's authenticated agent, and a mismatch resolves `foreign` (refused on a pull-mode agent, degraded log elsewhere). A user-scoped key that sends the header gains nothing the body field did not already allow.
- **Operator-visible text (G-04 class):** the alarm's `question` and `context` hold the agent name, a literal `effect_type` and a reason from a fixed set. No header or request text reaches it. The id prefix `effect-unguarded-` is reserved, so an agent cannot pre-create and suppress its own alarm.
- **Prompt surface:** tool descriptions changed wording only; no per-agent metadata added.

## Mutations
- Pull-mode predicate forced to `False`: 5 refusal tests in `test_2392_effect_guard_fail_closed.py` go red.

## Appendix (below the gate)
- **Accepted:** an agent holding its own key can call a sink directly with `execution_id: "manual"` and skip the refusal. The guard targets omission, not a hostile agent; closing this needs a per-execution credential (out of scope, recorded in the effect-idempotency flow).
- **Accepted:** Codex terminal sessions and user-scoped keys acting for a pull-mode agent carry no id and are refused. Fails closed; documented.
