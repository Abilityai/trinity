# Effect Idempotency - Direction B: Retry With Prior-Trace Context

| Field | Value |
|---|---|
| **Status** | Discussion artifact - proposes replacing the primary strategy in `TARGET_ARCHITECTURE.md` §Re-Delivery and Side-Effect Idempotency (lines 210-220) and Open Q 2a (line 488). NOT a committed decision. |
| **Date** | 2026-07-01 |
| **Issue** | #1084 (side-effect idempotency) - reframed |
| **Relationship to the review note** | This is "Direction B", the alternative to the effect-key sink-wiring "Direction A" analysed in the #1084 review note. It does not amend that note's §7 edits - it replaces the approach they edit. |

---

## 1. The change in one paragraph

Stop trying to control the agent's external calls at the platform layer. The current design (`effect_guard` + a platform-injected idempotency key threaded through every outbound sink, deduplicated at the sink) tries to make re-delivery *safe* by intercepting effects. Direction B abandons that goal as structurally unachievable and replaces it with a different primitive: **re-delivery is not a blind re-run - it is a re-invocation that carries the prior failed execution's structured trace as context.** The retried turn *sees what the dead turn already did* and decides for itself whether to continue, stop, or verify. The platform stops policing effects; the agent handles its own recovery from hindsight. Universal sink-keying is deleted as the gate; a narrow provider-native key survives only on the short list of irreversible-and-verifiable sinks (payment), as the thing the agent's "verify" step can query.

## 2. Why - the argument that forces the switch

**Completeness is unattainable, so eliminating the problem is unattainable.** The set of channels an agent can act through is unbounded: guarded backend sinks, but also its own Resend/SMTP key, `gh pr create`, a raw `curl`, any new MCP server, any shell command. The effect-key strategy can only wrap sinks the platform *knows about and routes*. You will never enumerate them all, so you are always *managing* the duplicate-effect risk, never *closing* it.

**The current doc already concedes this - for the exception.** Line 219 admits that no-backend-sink effects "cannot be made idempotent at the platform layer - at all," and that the only safe behaviour is to "never auto-re-deliver the agent" for those. Direction B simply promotes that admission from the *exception* to the *rule*: since the uncovered surface is unbounded and permanent, do not build the platform-level controller at all. Make re-delivery itself intelligent instead.

**The knowledge lives at the agent, not the platform.** The platform only ever sees "task died." The agent is the sole place that knows the semantic intent ("I was two steps into a three-step booking"). Recovery decisions belong where the context is. Trying to enforce correctness at the platform layer is optimising the wrong layer.

## 3. The new model

**Recovery primitive (replaces "blind lease re-delivery"):**

1. A crash is detected exactly as today - lease expiry (§Recovery, line 200-208 unchanged).
2. On re-queue, the platform injects the **structured trace of the prior failed execution(s) of this schedule** into the retried turn's context. The agent already has tools to fetch prior execution details; this makes the most recent failed trace *present by default* rather than something the agent must think to look up.
3. The retried turn is still a **fresh turn, not a resumed process** (compatible with line 206's "never a half-finished turn resumed") - but it runs *with hindsight*.
4. The agent branches:
   - **Transient failure** (network blip, timeout, rate limit, an internal step that never reached an external system) -> pick up and continue. Safe because the failure is legibly *before* any external effect.
   - **Real failure** (precondition genuinely unmet, repeated breakage) -> fail gracefully, report, stop thrashing.
   - **Ambiguous external effect** (a request left the container but no confirmation was recorded) -> **verify, then decide** - never blindly redo. This is the only branch that can cause a duplicate, and it only matters for irreversible sinks.

**What the trace must record (the one thing you must build well):**

- **Three states, not two:** every side-effecting step is `done` / `not-done` / `unknown`. The classic bug is collapsing `unknown` into `failed`: an errored/timed-out call whose effect actually landed, re-run because the trace said "failed." For any irreversible effect, an errored or timed-out call must be recorded as **`unknown` (verify)**, never `failed` (safe to retry).
- **Write-ahead intent:** for irreversible effects, log "about to charge $X, target=Y" *before* the call, not after it returns. Then the trace can never show a silent hole - worst case it shows "attempted, unconfirmed," which routes the agent to *verify* instead of *redo*. (This is the outbox pattern, done at the agent level - internal, cheap, crash-safe.)
- **Structured, not stdout:** the injected record is a step/outcome trace the agent can *locate its position in*, not raw prose it has to re-derive intent from.

## 4. What gets deleted, kept, and narrowed

| Component | Fate under Direction B |
|---|---|
| `effect_guard` as **the gate** for default-on | **Deleted as the gate.** No longer the blocker for pull rollout. |
| "Wire every outbound sink with the effect key" workstream (reactive replies, git-sync, ...) | **Deleted as a goal.** Chasing coverage of an unbounded surface stops. |
| Platform-injected `{effect_type}:sha256(...)` key threaded through sinks | **Retired as the primary mechanism.** |
| Provider-native idempotency key on the **short irreversible list** (payment) | **Kept, narrowed.** Not to dedupe blindly - to give the agent's *verify* step a real question to ask ("did charge key=X go through?"). |
| Human gate (operator queue) for irreversible-**and**-unverifiable effects | **Kept.** Small, permanent, honest residual. |
| Structured execution trace + injection into the retried turn | **New - the one thing to build.** Bounded surface: one record, one injection path. |

The trade being made explicit: you swap an **unbounded** engineering surface (a key path for every channel, forever) for a **bounded** one (one trace record + one injection path + a native key on the money sink).

## 5. Rollout rule rewrite

**Current (line 220):** pull defaults on for read/analysis-only agents first; side-effect-bearing agents wait for the effect-key gate to close (full sink coverage + trusted injection + fail-closed); un-mediated-write agents are *never* auto-re-deliverable.

**Direction B:** pull can default on for **any** agent once (a) it receives prior-trace context injection on re-delivery, and (b) each of its *irreversible* effects (if any) has either a provider-native verify path or a human gate. Concretely:

- **Read/analysis-only** -> default-on (nothing to recover carefully).
- **Reversible side effects only** (Slack, apologisable email, notes, most tool calls) -> default-on. A duplicate is survivable; trace-aware retry makes even that rare. No sink-keying required.
- **Irreversible + verifiable** (payment via provider idempotency) -> default-on, because "verify before redo" has an answer.
- **Irreversible + unverifiable** (an irreversible action through a channel with no query/dedup) -> the *specific effect* is human-gated; the agent is still auto-re-deliverable for everything else.

The unit of gating moves from **the agent** (a coarse "this whole agent can/can't auto-recover") to **the effect** (only genuinely irreversible-and-unverifiable actions gate). This removes the "everyone waits for full sink coverage" bottleneck that blocks the pilot today.

## 6. What this does NOT solve (honesty)

Direction B does not eliminate the duplicate-effect problem - nothing can; it is the two-generals impossibility (line 214). It **relocates** the residual and **bounds the surface**:

- The crash-window still exists: an irreversible effect that lands but is never recorded, on a sink that offers no way to verify, will still double-fire. Direction B's answer for that exact intersection is the same as everyone's - a human gate. The difference is that this intersection is now a *named, narrow residual*, not a moving target hidden behind "we'll wire more sinks."
- The agent reading its trace and deciding is non-deterministic. That is fine for reversible effects (a wrong guess is survivable) and explicitly *not* relied upon for irreversible ones (those get a deterministic verify path or a human).

## 7. Exact edits to `TARGET_ARCHITECTURE.md`

1. **§ heading, line 210** - retitle "Re-Delivery and Side-Effect Idempotency (the hardest open problem)" -> "Re-Delivery and Side-Effect Recovery (retry-with-context)". It stops being framed as an unsolved gate.

2. **Line 212/214** - keep the impossibility statement (it is true) but change the conclusion. Current: "the single hardest unsolved problem... exactly-once external effects are unattainable at the platform layer." Proposed: "exactly-once is unattainable at the platform layer, so the platform does not attempt it. Re-delivery carries the prior failed execution's structured trace, and the agent recovers from hindsight (continue / fail gracefully / verify-before-redo)."

3. **Lines 216-219 (the contract)** - replace the effect-key contract with the trace contract:
   - Platform guarantees at-least-once delivery **plus prior-trace context on every re-delivery**.
   - Recovery is the agent's, informed by the injected trace, not the platform's, enforced at sinks.
   - The trace must be three-state (`done`/`not-done`/`unknown`), write-ahead for irreversible effects, and structured.
   - Retain the line-219 truth ("effects with no backend sink cannot be deduped at the platform") but reframe it as the *general* case that motivates the whole switch, not a carve-out.

4. **Line 220 (rollout rule)** - replace with §5 above (gate the effect, not the agent).

5. **Open Q 2a, line 488** - rewrite from "three things gate defaulting pull on (coverage / enforcement / scope limit)" to: "the effect-key coverage chase is abandoned; the open work is (1) the structured three-state trace record, (2) its injection into the retried turn, (3) provider-native verify keys on the short irreversible list, (4) the effect-level human-gate lever for irreversible-unverifiable actions."

6. **Tracking table, line 511** - #1084 is no longer "**the gate** - pull stays read-only-agents-first until this lands." Re-scope #1084 to "structured recovery trace + injection; provider-native verify keys on irreversible sinks." Pull rollout no longer blocks on it.

7. **Cross-references** - note that this is complementary to the Saga pattern (line 235): sagas handle *cross-agent, multi-step* compensation at the orchestration layer; trace-injection handles *single-agent, single-turn* recovery. And the #1085 herd controls (line 490) apply unchanged - re-delivery is still re-delivery.

## 8. Open questions for the reviewer

1. **Trace fidelity.** Can the execution trace reliably capture every side-effecting step at `done`/`not-done`/`unknown` granularity - including effects through the agent's *own* keys / `gh` / `curl` - or only through instrumented paths? If some effects are invisible to the trace, those specific effects fall back to the human gate exactly like an unverifiable irreversible - is that acceptable coverage?
2. **Injection cost.** Prior-trace injection adds context tokens to every re-delivered turn. For a large failed trace, is a summarised trace sufficient, or does verify-correctness need the full trace? Where is the summarisation boundary?
3. **The `unknown` discipline is now load-bearing.** The whole safety of the irreversible path rests on "errored/timed-out irreversible call -> `unknown`, not `failed`." Is that classifiable reliably at the point the trace is written, or does it itself require per-sink knowledge (re-importing a slice of the coverage problem)?
4. **Non-determinism of the recovery decision.** For reversible effects we accept the agent's judgment. Is there any reversible effect whose duplicate is *more* costly than assumed (e.g. a Slack post that triggers a downstream automation), such that it should be treated as irreversible-verifiable instead?
5. **Interaction with `MAX_REDELIVERY`.** The re-delivery cap (line 208, unbuilt) parks poison tasks in the operator queue. Under Direction B a task that keeps hitting `unknown` and failing gracefully should also cap out to a human - does the cap counter reset on partial progress, or count raw attempts?

---

*Discussion artifact, 2026-07-01. Proposes replacing the effect-key sink-wiring strategy (Direction A) with retry-with-prior-trace recovery (Direction B). Requires review and sign-off before any change lands in `TARGET_ARCHITECTURE.md`.*
