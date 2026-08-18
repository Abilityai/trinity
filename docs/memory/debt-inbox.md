# Debt Inbox

> Deferred-debt capture point. When a review gate defers a real finding instead of fixing it,
> deferral and registration are **one act**: append an entry here. Producers: `/review`
> (Step 7b), `/autoplan` (approved deferred items), `/release-plan` (DEFER rows). Consumer:
> `/groom`'s weekly **Debt Health & Inbox Triage** step, which converts each entry to a
> `type-refactor` issue (deduped mechanically by the `debt:<id>` key), records it as an
> accepted-risk residual in `architecture.md`, or drops it — and marks a `Disposition:` line.
> Entries carrying a `Disposition:` are pruned by the next capture append (their durable
> record lives in the filed issue or `architecture.md`). This file is an *inbox*, not a
> registry — GitHub Issues remain the single source of truth for actionable debt.

## Entry format

```markdown
## debt:YYYY-MM-DD-<slug>
**What**: the shortcut / issue being deferred
**Why deferred**: why it isn't being fixed in this PR
**Context**: enough that someone picking this up in 3 months understands the state and where to start
**Interest**: what it costs while unpaid (recurring friction, risk, blocked work)
**Effort (AI)**: rough Claude-Code execution cost (S/M/L)
**Source**: PR / branch / issue ref
```

---

<!-- entries below, newest first -->
