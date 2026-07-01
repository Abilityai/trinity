# Effect Idempotency (#1084) — Review Note for a Second Opinion

| Field | Value |
|---|---|
| **Status** | Discussion note — NOT a decision. Written to gather a second opinion on proposed `TARGET_ARCHITECTURE.md` changes. |
| **Date** | 2026-07-01 |
| **Issue** | [#1084](https://github.com/Abilityai/trinity/issues/1084) — side-effect-scoped idempotency keys ("the gate" for pull default-on) |
| **Epic** | #1045 (Agent Infrastructure) · umbrella #1081 (pull migration) |
| **Purpose** | Capture the discussion (an external proposal → Cornelius's critique → proposed edits) in one place, so a second reviewer has full context without replaying the conversation. |
| **Author** | Drafted from a Claude Code working session with Eugene. |

> **How to read this.** §1–§2 is context a reviewer new to the project needs. §3 is the current doc state. §4 is the external review that kicked this off. §5 is Cornelius's take. §6 is the proposed changes (with diagrams). §7 is the exact doc edits. §8 is the open questions we want a second opinion on.

---

## 1. Context (for a reviewer new to Trinity)

Trinity runs a fleet of autonomous AI agents, each in its own Docker container. The target coordination model (see `TARGET_ARCHITECTURE.md` §Coordination Model) is **pull / work-stealing**: the platform writes tasks into one durable per-agent queue (a PostgreSQL `schedule_executions` row); an agent's worker pool **pulls** the next task only when it has free capacity, runs the Claude turn, and POSTs the result back.

The recovery primitive is **lease-expiry re-delivery**: if an agent dies mid-turn, its claimed row's lease expires and the task flips back to `queued` and **runs again** on another worker (same `execution_id`, same idempotency key).

- Re-running is **safe** for internal state (workspace files, DB rows).
- Re-running is **NOT safe** for external side effects the turn already performed: an email sent, a Slack post, a Nevermined payment charged, a git push. On re-delivery the turn re-runs and **repeats the effect**.

That is **#1084**. `TARGET_ARCHITECTURE.md:212` calls it *"the single hardest unsolved problem in the whole design."*

## 2. Why exactly-once is impossible (the root constraint)

Under Trinity's network invariant (#589) an agent can never touch the platform DB directly. So the agent's local "I finished / I sent it" write and the backend's "mark complete" write are **on two different machines and can never be one transaction** (`TARGET_ARCHITECTURE.md:214`). This is the classic two-generals / FLP shape: exactly-once external effects are **structurally unattainable** at the platform layer. The design must *route around* this, not defeat it.

## 3. Current doc state (what `TARGET_ARCHITECTURE.md` says today)

- **The contract** (lines 216–219): the platform guarantees **at-least-once delivery with an idempotent coordination boundary**; exactly-once external effects are the agent's responsibility; the platform helps by threading an **effect-scoped idempotency key `{execution_id}:{effect_ordinal}`** from the envelope through every sink (channel adapters, MCP outbound tools, payment path, git-sync), deduplicated at the sink.
- **The rollout rule** (line 219): *"until effect-scoped keys exist, pull mode defaults on only for agents with no irreversible external side effects (read/analysis-only). Channel- and payment-bound agents are migrated last."* — a single global switch.
- **Open Question 2a** (line 487): names it "the hardest open problem."

**Note — partially shipped already.** `architecture.md` §Idempotency "Effect-scoped extension (#1084)" and `feature-flows/effect-idempotency.md` describe a **shipped** `effect_guard(...)` wired into 4 sinks (`proactive_message_service`, `voip_service`, `agent_shared_files_service`, `nevermined_payment_service`). It is **fail-open when the key/`execution_id` is absent** (safe only because pull re-delivery is OFF today). Crucially, the shipped key is **NOT** the doc's `{execution_id}:{effect_ordinal}` — it keys on `{effect_type}:sha256(execution_id ‖ resolved_identifying_args ‖ dedup_label)`, i.e. **resolved immutable identity (recipient/channel/account) + an explicit `dedup_label`, never the LLM body and never a positional ordinal.** The target doc still shows the older, weaker `ordinal` form.

## 4. The external review that kicked this off (`Proposals (1).md`)

An external outside-voice review (*Proposals*, provided out-of-band — not committed to this repo) of Trinity's pull model refers to *"their"* one-source-of-truth principle and *"their"* roadmap #1082). Four workstreams:

- **A. Reliable result capture** — source the worker's result POST from an agent-owned artifact (Claude Code `Stop`/`PostToolUse` hooks writing `~/.trinity/results/<session>.json`), never from stdout; closes the #548/#333/#620 dropped-result class.
- **B. Portable & suspendable state** — declare non-git state in `template.yaml`, snapshot/restore, suspend idle agents to reclaim RAM, replica-safety.
- **C. Readers-writer concurrency** — read-only tasks run in parallel, modifying tasks exclusive, enforced inside the atomic `next-task` claim SQL.
- **D. Pull/steal + Saga** — transactional **outbox** for Postgres↔Redis dual-write, **event-source** the state machine (extends #1082), **sagas** as event-driven task-chaining over the pull queue.

**It shares the typed `error_code` taxonomy verbatim** with `TARGET_ARCHITECTURE.md:198` and `ACTOR_MODEL_POSTCARD.md:90` — evidence the author worked from Trinity's own docs.

**Key limitation for #1084:** Proposals D (outbox/sagas) is **internal-atomicity only**. It makes "I durably recorded that I *meant* to send" crash-safe *inside* the agent boundary — it does **not** and **cannot** certify "the external sink saw this effect exactly once." So Proposals is **context, not an answer** to #1084. It assumes the effect-key contract as given.

## 5. Cornelius's take (summarized)

1. **Reframe, don't lament.** Exactly-once impossible is the *start* of the design. This is the eventual-consistency escape: you never deliver exactly-once as a primitive; you **synthesize** it = at-least-once delivery + idempotent application keyed on a **platform-trusted token**. Line 214 should read "we chose the eventual-consistency escape," not "unsolved problem."
2. **Fail-open → fail-closed is a priced safety/liveness trade.** `effect_guard` fail-open = liveness-first (effect always proceeds, duplicate be damned). The gate (fail-closed injection before default-on) = unconditional safety / conditional liveness (never double-charge; if the trusted key is absent, block/defer). Not free: it converts a *silent duplicate* into a *loud refusal/queue* — right for irreversible sinks, wrong default for cheap ones.
3. **Don't ship one global default-on flag — sequence per-sink by cost-of-error.** Graduate a sink to default-on only when `(irreversibility tolerable) × (native or harness dedup key) × (trusted injection)`. Order: **payment last** (irreversible; only with provider-native idempotency key, else never unattended), **email/Slack middle** (reversible-by-apology), **git push first** (content-addressable; re-push of identical commit is a no-op). No-key sinks **park at a human gate** — a legitimate permanent residual, not a gap.
4. **The trust of `execution_id` is the whole ballgame.** "Make injection trustworthy" = "make the dedup key an external anchor the agent cannot forge or replay." If the turn authors/reads/replays its own key, fail-closed degrades to theater.
5. **Proposals = internal-atomicity leg only.** Outbox/sagas make the internal "intent-to-emit" durable; they move the problem *to* the boundary, they don't cross it. Don't let an outbox masquerade as closing #1084.

**Tradeoffs Cornelius flagged:** liveness vs safety per sink; coverage-completeness vs ship-velocity (honest "full coverage" = every default-on sink has both a trusted-key path and an idempotent-apply path; the rest stay human-gated rather than block the whole pilot); the residual can only be *relocated* (native sink dedup / platform key / human gate), never eliminated.

## 6. Assessment + proposed changes

**Verdict: ~90% agree.** Cornelius's two real contributions to adopt: (a) the **reframe**, (b) the **per-sink graduation gate**. Two sharpenings below.

### Change 1 — Reframe: chosen escape, not "unsolved problem"

```
   ┌──────────────────────────┐      ┌───────────────────────────┐
   │  AT-LEAST-ONCE delivery   │  +   │  IDEMPOTENT sink           │  = "effectively-once"
   │  (re-run until it lands)   │      │ ("seen this key? → skip")  │
   └──────────────────────────┘      └───────────────────────────┘
      ✅ platform already does this        ⬅ the real work (#1084), at the SINK
         (lease re-delivery)
```

### Change 2 — Fix the key so it survives a non-deterministic re-run (SHARPENING)

The doc's `{execution_id}:{effect_ordinal}` breaks because an AI turn is non-deterministic — a re-run may emit the same logical effect in a different order:

```
   Run 1 (then crashes):   effect #1 = charge $5     effect #2 = email Bob
   Run 2 (re-delivery):    effect #1 = email Bob     effect #2 = charge $5
                                       ▲ same email, ordinal changed 2→1
                            → key differs → dedup MISSES → Bob emailed twice

   ❌ key = execution_id : ordinal                          (drifts across re-runs)
   ✅ key = execution_id : resolved-recipient : dedup_label  (stable — already shipped)
```

This is **already** the shipped design (`architecture.md` §effect-idempotency). The edit is "make the target doc match the better design we already built" + state the requirement explicitly: **the key must be stable across a non-deterministic re-run.**

### Change 3 — Per-sink graduation gate replaces the global flag (ADOPT from Cornelius)

```
        ┌──────────────────────────────────────────────────────────┐
        │  Can this sink run unattended under re-delivery?          │
        │   1. Is a duplicate tolerable / reversible?              │
        │   2. Is there a dedup key? (provider-native OR our key)   │
        │   3. Is that key TRUSTED? (platform-injected, always      │
        │                            present — never agent-omitted)  │
        └──────────────────────────────────────────────────────────┘
              all 3 = yes → DEFAULT-ON
              any  = no   → HUMAN GATE (operator queue — already exists)

   git push    │ duplicate = usually a no-op (same commit)     │ graduates FIRST
   email/Slack │ duplicate = annoying, apologisable            │ middle
   payment     │ duplicate = real money, irreversible          │ LAST — provider-native
               │                                               │ key only, else never unattended
```

The no-key sinks **don't block the rollout**; they park at Trinity's existing operator queue. Permanent, legitimate residual.

### Change 4 — Narrow the "open work" (SHARPENING of Cornelius's point 4)

Cornelius frames the crux as "unforgeable / non-replayable anchor" (agent-as-adversarial-prover). **But Trinity's threat model is different:** the agent is not adversarial, it's *buggy / re-delivered*, and the backend **already resolves identity server-side from `execution_id`** (the MEM-001 pattern — `write_user_memory` resolves the user email server-side, never trusting an agent-supplied value). So forgery-resistance is largely already present. The genuinely-open work is narrower and more concrete:

- **Always-present** — fail-closed when the key/`execution_id` is absent (today it's fail-open). This is the actual gate before default-on.
- **Stable across re-run** — Change 2. A determinism problem, not (only) a checker-independence one.

Don't let "make it unforgeable" inflate scope; the ask is "make it always present + stable."

### Change 5 — Draw the boundary the outbox can't cross (from Cornelius point 5)

```
   INSIDE the agent container         │  BOUNDARY  │   OUTSIDE (3rd party)
   ──────────────────────────         │            │   ──────────────────
   "I durably recorded that I         │            │   Stripe / Gmail / Slack
    MEANT to send"  ◄── OUTBOX        │            │   actually performed it
    (internal, cheap, crash-safe)     │            │           ▲
                                      │            │   only the DEDUP KEY crosses
```

The outbox guarantees the internal leg only. Add a one-liner so it's not sold as closing #1084.

## 7. Proposed edits to `TARGET_ARCHITECTURE.md`

1. **Reframe** §"Re-Delivery and Side-Effect Idempotency" (heading + line 212/214): impossibility → *chosen* eventual-consistency escape (effectively-once = at-least-once + idempotent sink).
2. **Fix the key** (line 218 and 487/2a): `execution_id : resolved-identity : dedup_label`, not `:{effect_ordinal}`; add "must be stable across a non-deterministic re-run." Point at the shipped `architecture.md` §effect-idempotency as the reference.
3. **Per-sink graduation gate** replaces the single default-on rule (line 219): `default-on ⟺ (irreversibility tolerable) × (dedup key exists) × (key trusted/always-present)`; no-key sinks → operator-queue human gate; order irreversibility-first (git → channel → payment).
4. **Narrow the open work** (2a): from "make injection trustworthy/unforgeable" → **"always-present (fail-closed) + stable-across-re-run"**; note server-side resolution already blunts forgery; the threat is buggy/re-delivered, not adversarial.
5. **Boundary note**: outbox/saga (Proposals D) = internal-atomicity leg only; must not be presented as closing #1084.

**Net reframing:** #1084 stops being "an unsolved problem" and becomes **"a rollout discipline"** — graduate each sink when it has a stable, trusted, always-present key; park the rest at a human gate; never let an internal mechanism pretend it closed the external boundary.

## 8. Open questions for the second reviewer

1. **Is the reframe honest, or does it hand-wave a real gap?** Does "effectively-once = at-least-once + idempotent sink + trusted key" fully cover the failure modes, or are there sinks where even a stable trusted key isn't enough?
2. **Per-sink graduation gate** — is `(irreversibility × key-exists × key-trusted)` the right predicate? Is "park no-key sinks at the operator queue" operationally acceptable at 200-agent scale, or does it create an approval-queue bottleneck that defeats autonomy?
3. **Is the "threat is buggy-not-adversarial" claim safe?** MEM-001 resolves identity server-side, but is there any path where an agent-supplied field reaches a sink's dedup key un-resolved? (If yes, Cornelius's unforgeability concern re-enters.)
4. **`dedup_label` correctness** — the shipped design lets an agent intentionally repeat an effect to the same target by varying `dedup_label`. Under re-delivery, does a non-deterministic turn reliably reproduce the *same* `dedup_label` for the *same* logical effect? If not, Change 2 is incomplete.
5. **git push "natural idempotency"** — re-pushing an identical commit is a no-op, but a re-*created* branch/PR is a real duplicate. Does git-sync graduate first safely, or does it need a key too?

## 9. Referenced files (full paths)

**Primary (the open question):**
- `docs/planning/TARGET_ARCHITECTURE.md` — §Re-Delivery/Side-Effect Idempotency (210–219), Open Q 2a (487, 510)
- `docs/memory/feature-flows/effect-idempotency.md` — shipped mechanism + real key shape
- `docs/memory/architecture.md` — §Idempotency "Effect-scoped extension (#1084)"
- `docs/memory/feature-flows/idempotency-keys.md` — trigger-boundary base (#525)

**Coordination context:**
- `docs/planning/ACTOR_MODEL_POSTCARD.md` — envelope + typed result contract (#945)
- `docs/planning/ORCHESTRATION_BUG_META_ANALYSIS_2026-06.md` — MISSING_IDEMPOTENCY family
- `docs/memory/feature-flows/redelivery-governor.md` — #1085 herd controls
- `docs/planning/PULL_PILOT_946_SOAK.md` — the pilot the gate blocks

**Adjacent / external:**
- `docs/planning/TRANSACTIONAL_EXECUTIONS_2026-06.md` — #1095 workspace transactions (NO-GO), §10.3 defers external effects to #1084
- *Proposals* — external review, provided out-of-band (not committed to this repo); A/B/C/D workstreams, D = internal-atomicity leg only

---

*This note is a discussion artifact, not a committed decision. The proposed edits in §7 require review + sign-off before any change lands in `TARGET_ARCHITECTURE.md`.*
