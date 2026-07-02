# Feature: Effect-Scoped Idempotency for Outbound Side Effects (#1084)

> P1 · theme-reliability · extends RELIABILITY-006 (#525, Invariant #18).
> The named *gate* on defaulting pull-mode ON for side-effect-bearing agents.

## Overview

Trigger-boundary idempotency (#525) dedups the **execution** — it stops a
re-POSTed `/chat`, webhook, or scheduler fire from creating a *second
execution*. It does **not** reach an agent's individual outbound tool calls. So
when a turn is re-delivered (the at-least-once semantics that pull-mode /
work-stealing, Epic #1045/#1081, will introduce), an agent that already sent a
message or charged a payment **re-emits the effect** on the re-run.

Per network invariant #589, the agent's in-container "done" write and the
backend's coordination-complete write are on different machines and can never be
one transaction. Exactly-once external effects must therefore be enforced **at
the sink, per action**. This feature adds that per-sink guarantee so pull-mode
can later be enabled safely.

## Honest scope of the guarantee

This delivers **local duplicate suppression** keyed on resolved effect identity
within a single `execution_id`. It is *true* exactly-once only where a provider
has native idempotency (Nevermined `agent_request_id`); for message providers
without native keys it is best-effort dedup with a **documented pre-call crash
window** (see Failure Modes). The "real" exactly-once for pull-mode also depends
on a dispatcher invariant — re-delivery preserves the same `execution_id` —
**owned by Epic #1045/#1081**, not this feature.

## User Story

As an operator enabling autonomous (pull-mode) agents, I want an agent that
re-runs a turn to **not** re-send the email / re-charge the payment / re-place
the call it already completed, so that at-least-once delivery is safe for
side-effect-bearing agents.

## Design (review-locked)

1. **Content-derived key on STABLE identity, scoped by execution_id.**
   - Scope: `effect:{execution_id}` (messages / voip / share_file);
     `payment:{agent_request_id}` (Nevermined — its natural unit + native token).
   - Key: `{effect_type}:sha256(execution_id ∥ effect_type ∥
     resolved_identifying_args ∥ dedup_label)`. `resolved_identifying_args` is the
     **resolved, immutable** identity only — recipient + channel (+ provider
     account); **never the LLM-generated message body** (non-deterministic across
     a re-run → would defeat dedup). Per-sink args: share_file → filename +
     content sha256; voip → resolved E.164 dial target + Twilio account;
     nevermined → `agent_request_id` (covers amount/asset/payer/settlement-phase).
   - `dedup_label` (agent-supplied, default ""): lets an agent intentionally send
     two distinct messages to the same recipient in one turn. Default empty →
     at-most-one effect per (recipient, channel, type) per turn.

2. **Long TTL (≥ lease window) for all effect rows.** A completed row must
   outlive a late lease-expiry re-delivery (`agent_timeout + SLOT_TTL_BUFFER`,
   ≤ ~2h). The existing **24h default** already exceeds that, so it is reused —
   no `ttl_minutes` param, no new config constant. The shared `claim()` deletion
   semantics are unchanged (#525 trigger-dedup depends on them).

3. **One shared `effect_guard` + `resolve_and_validate_execution`, in the
   existing `services/idempotency_service.py`** (no new module — avoids the
   untracked-file / Docker-COPY crash class, #1033).
   - `async with effect_guard(effect_type, identifying_args, *, execution_id,
     agent_name, dedup_label, payment_request_id) as g:` resolves + validates the
     execution, claims the key, yields `g.replay` / `g.snapshot`; on clean exit
     calls `complete(snapshot)`, on exception calls `fail()` (release for retry).
   - **`in_flight` ≠ `completed`**: a `completed` replay returns the stored
     snapshot; an `in_flight` replay raises a retryable `EffectInProgressError`
     (HTTP 409). The sink MUST NOT silently return None-as-success (that would
     skip the send AND report success).
   - Each sink stores a **sanitized, JSON-stable snapshot** (message_id /
     call_id+status / settle receipt / share URL), never a raw provider object.

4. **Agent passes `execution_id` as a tool arg** (MEM-001 template), **fail-open
   when absent** — for this PR. Safe today because pull-mode re-delivery is OFF,
   so fail-open cannot produce a duplicate. The MCP server can't currently see
   `TRINITY_EXECUTION_ID`; trusted runtime injection is deferred (see Gate).

5. **Guard at the service entry, keyed on resolved recipient+channel(+account).**
   A chunked message = one effect; a mid-chunk crash re-sends the whole message
   on retry (at-least-once for chunks — documented). Any sink that genuinely fans
   out to multiple providers/targets must instead guard at its resolved per-target
   call.

6. **git-sync: no synthetic key.** Git push is idempotent-by-construction:
   same-commit re-push is a no-op, `--force-with-lease` guards divergence, and
   the 15-min auto-sync loop has no `execution_id`. No guard added.

## Entry Points (wired sinks)

| Sink | Service entry | Scope | identifying_args |
|------|---------------|-------|------------------|
| Proactive message | `proactive_message_service.send_message` | `effect:{exec}` | `{recipient, channel}` |
| VoIP call | `voip_service.place_outbound_call` | `effect:{exec}` | `{to (E.164), account}` |
| Share file | `agent_shared_files_service.create_share` | `effect:{exec}` | `{filename, content sha256}` |
| Nevermined settle | `nevermined_payment_service.settle_payment_once` | `payment:{agent_request_id}` | `{phase, plan_id}` |

Each service entry is reached from its router (`routers/messages.py`,
`routers/voip.py`, `routers/agent_files.py`, `routers/paid.py`), and the
message/voip/share entries are exposed as MCP tool params on `messages.ts` /
`voip.ts` / `files.ts` → body fields in `client.ts` (Invariant #13).

## Backend Layer (Invariant #1)

- **Primitive** — `services/idempotency_service.py`: `make_effect_scope`,
  `make_payment_scope`, `derive_effect_key`, `resolve_and_validate_execution`
  (generalizes the MEM-001 server-side resolution in `routers/public_memory.py`
  — the agent supplies an execution_id, never its own identity; fail-open on
  missing/mismatch), `EffectInProgressError`, and `effect_guard`. Reuses the
  existing `begin`/`complete`/`fail` over `db/idempotency.py` — **no schema or
  migration change**.
- **Sinks** wrap their actual emission in `effect_guard`; a success records the
  sanitized snapshot, an exception releases the claim for retry.
- **Routers** surface a concurrent in-flight duplicate as **409**
  (`messages.py`, `voip.py`, `agent_files.py`); `paid.py` returns a retryable
  "settlement already in progress" result. The VoIP router keeps its boundary
  `Idempotency-Key` gate as the OUTER layer.

## MCP Layer (Invariant #13)

`messages.ts` / `voip.ts` / `files.ts` add optional `execution_id` +
`dedup_label` params (agent reads `execution_id` from the 'Execution Context'
block of its system prompt, same source as `write_user_memory`). `client.ts`
threads both into the request body. `chat_with_agent` already derives an
`Idempotency-Key` (#525) — unchanged, regression-asserted in `messages.test.ts`.

## Pull-mode default-ON gate (BLOCKING prerequisite)

`dispatch_async_eligible()` stays `DISPATCH_ASYNC AND triggered_by in
ASYNC_DISPATCH_ELIGIBLE_TRIGGERS` (DISPATCH_ASYNC default-OFF). Enabling pull-mode
default-ON for **any** side-effect-bearing agent REQUIRES, first:
(a) **trusted runtime injection** of `execution_id` (so an agent can't forge or
omit it), and (b) **fail-closed-when-absent** (no execution_id → refuse the
effect, not fail-open). Both are tracked as a **blocking dependency on Epic
#1045/#1081**, not doc-only-and-forget.

> **Reframed (v2, 2026-07-01).** `TARGET_ARCHITECTURE.md` reframes the pull-mode side-effect rollout from a **per-agent** gate to **per-effect**: read/analysis-only + reversible + capability-confined-irreversible effects default on; only irreversible-**un-confineable** effects wait, via the **async operator queue** (#1402). `effect_guard` (this doc) is the reversible/backend-sink slice; general recovery is **retry-with-prior-trace** (#1401). The (a)/(b) trusted-injection requirement above still applies to the *confined-irreversible* tool-side gate.

## Failure Modes

| Codepath | Realistic failure | Handling | User-visible |
|---|---|---|---|
| `effect_guard` claim | Redis/DB hiccup on claim | fail-open (`begin` returns disabled) → send proceeds | no |
| pre-call crash | crash after claim, before provider call | `in_flight` blocks re-send for the TTL window (at-most-once-with-possible-loss) | **silent loss — documented tradeoff, not a silent bug** |
| `in_flight` replay | duplicate worker mid-flight | raise `EffectInProgressError` → 409 | clear retryable error |
| chunked message crash | crash after chunk 3/5 | whole message re-sent on retry | duplicate chunks (at-least-once, documented) |
| Nevermined settle | settle on terminal turn | terminal-turn guard preserved (no settle on failed execution) | no double-charge |
| absent execution_id | old image / agent omits arg | fail-open → send proceeds, no 5xx | no |

## NOT in Scope (deferred)

- Pull-mode re-delivery itself (#1045/#1081) — this only makes it *safe* to enable.
- Trusted runtime/MCP injection of `execution_id` + fail-closed-when-absent — the
  enforced capability gate (BLOCKING prerequisite above).
- The "re-delivery preserves the same `execution_id`" dispatcher invariant + its
  integration test — owned by the pull-mode epic.
- Per-chunk / per-provider-call guarding — single-target sinks use the entry
  guard; only future multi-target tools guard per-target.

## Testing

### Unit (`tests/test_idempotency.py`, `src/mcp-server/src/messages.test.ts`)
- Same `execution_id` + recipient + channel, **different generated body** →
  exactly ONE send (the body is structurally absent from the key).
- Completed effect row still replays after aging 6h (long-TTL, ≪ 24h).
- Fresh `execution_id` (#271 scheduler retry) → distinct scope → NOT deduped.
- `derive_effect_key`: distinct `dedup_label` → distinct key; arg-order
  insensitive; different execution_id / recipient → different key.
- `effect_guard`: success → snapshot stored, re-enter → replay (no 2nd call);
  exception → release → retry sends; **in_flight replay → raises, NOT silent**.
- `resolve_and_validate_execution`: valid match ok; agent mismatch / missing /
  None → fail-open.
- Nevermined: retried settle same `agent_request_id` → ONE settle; failed settle
  → release + retry; distinct tokens → both; missing token → fail-open.
- share_file: re-run same execution_id+filename → same URL replay; changed
  content → new share.
- MCP: `send_message` forwards `execution_id` + `dedup_label` (undefined when
  omitted); `chat_with_agent` still sets a non-empty `mcp:` Idempotency-Key.

### Integration (sibling stack `-p trinity-1084-test`, remapped ports)
- Re-deliver the same `execution_id` through proactive send → exactly one
  adapter call (mock/audit); fresh `execution_id` → not deduped.

## Related Flows

- [idempotency-keys.md](idempotency-keys.md) — the #525 trigger-boundary layer
  this extends (shared `idempotency_keys` table + `begin`/`complete`/`fail`).
- #1083 fire-and-forget dispatch (architecture.md → Fire-and-Forget Dispatch);
  `dispatch_async_eligible` in `task_execution_service.py` carries the pull-mode
  gate comment.

## Change History

- 2026-06-22 — #1084: effect-scoped idempotency for outbound side effects
  (messages, voip, share_file, Nevermined settle); `effect_guard` primitive +
  MCP `execution_id`/`dedup_label` params. No schema change.
