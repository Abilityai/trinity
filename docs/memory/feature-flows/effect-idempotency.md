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
     `payment:{agent_request_id}` (Nevermined — an observability id, **not** a
     provider exactly-once token; this local guard enforces at-most-once per id,
     residual at-least-once retry tracked by #1408).
   - Key: `{effect_type}:sha256(execution_id ∥ effect_type ∥
     resolved_identifying_args ∥ dedup_label)`. `resolved_identifying_args` is the
     **resolved, immutable** identity only — recipient + channel (+ provider
     account); **never the LLM-generated message body** (non-deterministic across
     a re-run → would defeat dedup). Per-sink args: share_file → filename +
     content sha256 + audience (the addressee — the agent's `audience_email`
     override, else the one the platform resolved from the turn; empty for an
     owner-only share); voip → resolved E.164 dial target + Twilio account;
     nevermined → `agent_request_id` (covers amount/asset/payer/settlement-phase).
   - **Why the addressee is in the share_file key** (trinity-enterprise#549): the
     addressee is part of WHAT the effect is. Re-addressing the same file to a
     second person in one turn must be a second share, not a replay — a key over
     `{filename, content}` alone replays the first snapshot for the second call,
     reporting success while the second person's row never exists. The args are
     hashed into the key, and the replay snapshot is stored WITHOUT the
     `addressed_to` echo, so `idempotency_keys` never holds an address: a replay
     re-derives the echo from its own call, whose `audience_email` is part of the
     key and therefore the same address by construction. The scope is unchanged (`effect:{execution_id}`): same
     turn, same file, same addressee still replays the original signed URL. The
     rule that decides the addressee lives in
     [file-sharing-outbound.md](file-sharing-outbound.md) → *Who a file is for*.
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

4. **The platform supplies `execution_id` (#2392).** The agent server sets
   `TRINITY_EXECUTION_ID` in every spawned runtime process; the agent's Trinity
   MCP entry sends it as `X-Trinity-Execution-Id` (Claude Code:
   `${TRINITY_EXECUTION_ID:-manual}`; Codex `env_http_headers`; Gemini env
   expansion). The MCP server reads it per request and it wins over the
   agent-supplied tool arg, which stays as the fallback for older images.
   `manual` marks a person's terminal/SSH session: no execution to re-deliver.
   When no usable id arrives, a **pull-mode** agent's effect is **refused**
   (`EffectUnguardedError` → 422 `effect_unguarded`, operator alarm
   `effect_unguarded`); any other agent's effect is sent and logged as
   `effect_guard.degraded` (see Pull-mode section).

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
| Share file | `agent_shared_files_service.create_share` | `effect:{exec}` | `{filename, content sha256, audience}` — audience is the addressee (override or resolved; empty for owner-only) |
| Nevermined settle | `nevermined_payment_service.settle_payment_once` | `payment:{agent_request_id}` | `{phase, plan_id}` |

Each service entry is reached from its router (`routers/messages.py`,
`routers/voip.py`, `routers/agent_files.py`, `routers/paid.py`), and the
message/voip/share entries are exposed as MCP tool params on `messages.ts` /
`voip.ts` / `files.ts` → body fields in `client.ts` (Invariant #13).

## Backend Layer (Invariant #1)

- **Primitive** — `services/idempotency_service.py`: `make_effect_scope`,
  `make_payment_scope`, `derive_effect_key`, `resolve_and_validate_execution`
  (generalizes the MEM-001 server-side resolution in `routers/public_memory.py`
  — the backend confirms the id belongs to the calling agent), `EffectInProgressError`,
  `EffectUnguardedError`, `effect_dedup_required` (today: the pull pilots; Phase 5
  flips it), and `effect_guard`. Reuses the
  existing `begin`/`complete`/`fail` over `db/idempotency.py` — **no schema or
  migration change**.
- **Sinks** wrap their actual emission in `effect_guard`; a success records the
  sanitized snapshot, an exception releases the claim for retry.
- **Routers** surface a concurrent in-flight duplicate as **409**
  (`messages.py`, `voip.py`, `agent_files.py`, `a2a.py`) and a refused
  unguarded effect as **422** `{"reason": "effect_unguarded"}` (same routers plus
  `internal.py`'s `/agent-files/share`, which carries no id and is therefore
  refused for a pull-mode agent); `paid.py` returns a retryable
  "settlement already in progress" result. The VoIP router keeps its boundary
  `Idempotency-Key` gate as the OUTER layer.

## MCP Layer (Invariant #13)

`server.ts` `authenticate` parses `X-Trinity-Execution-Id` into
`authContext.executionId` on every POST (fastmcp re-runs it per request).
`send_message`, `call_user`, `send_voice_reply`, `share_file` and
`call_a2a_agent` send `resolveExecutionId(authContext, params.execution_id)`
(`tools/execution_id.ts`): the header value, else the optional tool arg (older
agent images). `manual` is honoured only from the header; typed as the tool arg it is dropped. `client.ts` threads it and `dedup_label` into the request body. `chat_with_agent` already derives an
`Idempotency-Key` (#525) — unchanged, regression-asserted in `messages.test.ts`.

## Pull-mode: fail-closed without a usable id (#2392)

Pull re-delivers the same `execution_id` after a lease expiry, so on a pull-mode
agent (`effect_dedup_required`) the guard refuses an effect it cannot de-duplicate:

| id reaching the guard | pull-mode agent | other agent |
|---|---|---|
| resolves to the caller's own execution | dedup | dedup |
| `manual` (terminal/SSH session; Claude Code and Gemini) | send, `effect_guard.degraded` | same |
| absent (old image, raw API call, internal share route) | **refuse** + alarm | send, degraded log |
| unknown / another agent's execution | **refuse** + alarm | send, degraded log |
| lookup error (DB blip) | send, degraded log | same |

Codex omits the header when `TRINITY_EXECUTION_ID` is unset, so a person running
Codex in a pull-mode agent's terminal reaches `absent` and is refused; so is a
user-scoped key acting for a pull-mode agent from outside a turn.

The alarm goes through `create_bounded_alert` (type `effect_unguarded`, id prefix
`effect-unguarded-` reserved); at the budget cap the effect is still refused.
The header is not a trust boundary: an agent owns its `.mcp.json` and can cite
any of its own executions. The guard stops omission, not a hostile agent.

> **Reframed (v2, 2026-07-01).** `TARGET_ARCHITECTURE.md` reframes the pull-mode side-effect rollout from a **per-agent** gate to **per-effect**: read/analysis-only + reversible + capability-confined-irreversible effects default on; only irreversible-**un-confineable** effects wait, via the **async operator queue** (#1402). `effect_guard` (this doc) is the reversible/backend-sink slice; general recovery is **retry-with-prior-trace** (#1401). The (a)/(b) trusted-injection requirement above still applies to the *confined-irreversible* tool-side gate.

## Cross-execution intent keys (trinity-enterprise#665)

`effect_guard` dedupes within ONE execution. A recurring agent's runs are separate
executions, so it could not say "I already told this person X; do not say it again
for N seconds". `send_message`, `call_user` and `send_group_message` take an optional
caller-declared `idempotency_key` + `idempotency_ttl` (60–86400 s, default 86400).

- **Guard**: `idempotency_service.intent_guard(effect_type, agent_name, target,
  idempotency_key, ttl_seconds, execution_id)`. Scope `intent:{agent}`; key
  `{effect_type}:sha256(effect_type, target, idempotency_key)`. Target = resolved
  recipient email / E.164 number / `telegram:{chat_id}` / `slack:{team}:{channel}`.
  The channel is not part of a DM key: the same news on Telegram then Slack is one
  interruption. **No text argument** — content-derived suppression is the #1422 failure.
- **Store**: the same `idempotency_keys` table. `claim(ttl_seconds=, in_flight_lease_seconds=)`
  expires only a COMPLETED row by `created_at` (the checking call's TTL decides), and
  reclaims an `in_flight` row only after a 300 s lease on `updated_at`. Callers that
  pass neither keep the 24 h rule. TTL ceiling = the cleanup sweep's 24 h purge.
- **Outcomes**: completed claim → suppressed (`sent:false, suppressed_by:"idempotency_key",
  first_sent_at, first_execution_id`); in-flight claim → `IntentInProgressError`
  (subclass of `EffectInProgressError`, 409, retryable — never "suppressed", which would
  lie if that send dies); failed send → claim released. Keyless → nothing claimed, the
  response is byte-identical (`response_model_exclude_unset` on the messages route).
- **Composition** (`send_message`, `call_user`): `effect_guard` (outer, per execution) → `intent_guard` (inner) → send. The group routes have no `effect_guard`; they run `intent_guard` alone and map an in-flight claim to 409 themselves.
  The intent key joins `effect_guard`'s identifying args, so one turn's suppressed send
  under key A never replays for key B. A re-delivery of the suppressing run replays
  `sent:false`.
- **Order inside a sink**: consent → intent key → rate limit → deliver. A revoked
  recipient gets 403; a suppressed send costs no rate-limit budget.
- **Visibility**: audit `suppressed` (DM), `group_message_suppressed`,
  `voip_call_suppressed`; and a `system`-role row labelled `Trinity` in the first send's
  conversation session (`channel_history.persist_suppressed_note`, `sender_email=None` so
  it stays out of MEM-001). Calls have no conversation session → audit only.

## Failure Modes

| Codepath | Realistic failure | Handling | User-visible |
|---|---|---|---|
| `effect_guard` claim | Redis/DB hiccup on claim | fail-open (`begin` returns disabled) → send proceeds | no |
| pre-call crash | crash after claim, before provider call | `in_flight` blocks re-send for the TTL window (at-most-once-with-possible-loss) | **silent loss — documented tradeoff, not a silent bug** |
| `in_flight` replay | duplicate worker mid-flight | raise `EffectInProgressError` → 409 | clear retryable error |
| intent key `in_flight` | another run mid-send on the same key | `IntentInProgressError` → 409; stale after 300 s lease | retryable error naming the key |
| intent key, ambiguous send | timeout / cancel after the provider may have delivered | claim released → next run sends again (at-least-once on ambiguity) | possible duplicate — documented |
| intent key, send stalls > 300 s | lease reclaimed while the first send is alive | second send goes out; first `complete()` overwrites the row (no owner token) | possible duplicate — documented ceiling |
| chunked message crash | crash after chunk 3/5 | whole message re-sent on retry | duplicate chunks (at-least-once, documented) |
| Nevermined settle | settle on terminal turn | terminal-turn guard preserved (no settle on failed execution) | no double-charge |
| no usable execution_id, pull-mode agent | old image / raw API call / foreign or unknown id | `EffectUnguardedError` → 422 `effect_unguarded` + operator alarm; nothing sent | agent sees a non-retryable refusal; operator sees the alarm |
| no usable execution_id, other agent | same | send proceeds, `effect_guard.degraded` warning | log only |

## NOT in Scope (deferred)

- Pull-mode re-delivery itself (#1045/#1081) — this only makes it *safe* to enable.
- A per-execution credential that would make the id unforgeable by the agent itself.
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
  content → new share; the same file re-addressed to a second person in the
  same turn → a second share, while same turn + file + addressee still replays
  (`tests/unit/test_ent549_file_audience.py`).
- MCP: `send_message` forwards `execution_id` + `dedup_label` (undefined when
  omitted); `chat_with_agent` still sets a non-empty `mcp:` Idempotency-Key.
- #2392 (`tests/unit/test_2392_effect_guard_fail_closed.py`): the table above
  for pull-mode and other agents, the alarm payload and budget-cap refusal, and a
  re-delivered execution emitting each of message / voip_call / share_file /
  a2a_call once. `tests/unit/test_2392_execution_id_header.py`: the MCP config
  writers and validator. MCP: `tools/execution_id.test.ts`,
  `execution-id-transport.test.ts` (two requests on one session carry distinct ids).

### Integration (sibling stack `-p trinity-1084-test`, remapped ports)
- Re-deliver the same `execution_id` through proactive send → exactly one
  adapter call (mock/audit); fresh `execution_id` → not deduped.

## Related Flows

- [nevermined-payments.md](nevermined-payments.md) — the paid x402 boundary now
  carries the #525 trigger-idempotency layer too (`derive_payment_key`, `(token+body)`,
  #1018), composing with the `payment:{agent_request_id}` settle effect guard here:
  trigger-dedup stops the LLM re-run; the effect guard stops a *concurrent same-id*
  double-settle (Nevermined's `agent_request_id` is an observability id, not a provider
  exactly-once token, so a fresh-id retry's double-settle residual is tracked by #1408).
- [idempotency-keys.md](idempotency-keys.md) — the #525 trigger-boundary layer
  this extends (shared `idempotency_keys` table + `begin`/`complete`/`fail`).
- #1083 fire-and-forget dispatch (architecture.md → Fire-and-Forget Dispatch);
  `dispatch_async_eligible` in `task_execution_service.py` carries the pull-mode
  gate comment.

## Change History

- 2026-09-21 — trinity-enterprise#549: the share_file sink's identifying args
  gained the addressee (`{filename, content sha256, audience}`), so re-addressing
  a file within one turn is a second share rather than a replay. Scope unchanged
  (`effect:{execution_id}`); no change to `idempotency_keys`.
- 2026-07-04 — #1018: the paid x402 boundary (`routers/paid.py`) gained the #525
  trigger-idempotency layer (`derive_payment_key`, `upgrade_snapshot`), composing
  with the `payment:{agent_request_id}` settle effect guard. No schema change.
- 2026-06-22 — #1084: effect-scoped idempotency for outbound side effects
  (messages, voip, share_file, Nevermined settle); `effect_guard` primitive +
  MCP `execution_id`/`dedup_label` params. No schema change.
