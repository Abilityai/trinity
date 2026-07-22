# Local Product Events — Activation Funnel (Tier-1)

Trinity records a small set of **local, anonymous product events** so you — the
operator — can see how your own first-run users move through setup and reach
their first value. This is **Tier-1** of a two-tier telemetry model.

> **This data never leaves your instance.** There is no network egress from this
> layer. Events are written to your local database and read back only by you, on
> your own admin surface. Sharing anything externally is a separate, explicitly
> opt-in feature (Tier-2, not enabled by default and not part of this layer).

## What is recorded

**Onboarding-wizard step transitions** (emitted by the first-run wizard):

| Event | Meaning |
|-------|---------|
| `setup_started` | The first-run wizard was opened |
| `setup_step_create` | An intent was picked; advanced to the create form |
| `setup_step_credential` | The first agent was created; reached the credential step |
| `setup_completed` | The wizard was finished (opened chat / went to credentials) |
| `setup_dismissed` | The wizard was closed before creating an agent |

**First-value events** — `first_agent_created`, `first_chat`,
`first_schedule_created`, `first_channel_connected`. These are **not** captured
by a separate beacon; they are **derived on read** from data Trinity already
records (the audit log and agent-activity timeline), so they cost no extra write
path and survive restarts by construction.

Each event carries:

- a stable, random **installation id** (the same anonymous per-install id used by
  the operator-intake correlation key — not tied to any user account),
- a **UTC timestamp**, and
- optionally a tiny, non-sensitive context blob (e.g. which starter intent was
  picked). No message contents, credentials, emails, or PII are recorded.

The emit endpoint accepts only the fixed allow-list of event types above; it
cannot be used to store arbitrary data.

## What is NOT recorded

- No chat/message contents, no agent outputs.
- No credentials, tokens, API keys, or emails.
- No IP addresses or user identities beyond the anonymous install id.

## Turning it off

Capture is on by default and is intentionally lightweight (a handful of rows per
install). Because it never phones home, there is no privacy reason to disable it.
If you want zero local capture, you can drop the `product_events` rows at any
time — nothing else depends on them:

```sql
DELETE FROM product_events;
```

## Viewing the funnel

The operator-facing **Activation funnel** view (Settings → Activation) shows
step-by-step activation counts, drop-off between steps, and the first-value
tiles, with an honest empty state before any data exists. The funnel view is an
entitlement-gated enterprise surface; the **capture** described above runs in
every edition.
