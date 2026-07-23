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

---

# Tier-2 — Opt-in Fleet Sharing (ent#12)

Everything above (Tier-1) is **local only**. Tier-2 is a **separate, opt-in,
default-off** channel that — *only after you explicitly turn it on* — shares
**anonymized aggregates** with the Ability-operated hosted intake, so you get
fleet benchmarks in return ("how does my setup compare?").

> **Nothing is shared until you opt in, and opting out stops it immediately.**
> Two independent gates must both be on for any egress: your stored consent
> (Settings → General → **Usage sharing**, default-off) **and** the config switch
> `TELEMETRY_SHARING_ENABLED` (which honors the cross-tool `DO_NOT_TRACK=1`).

## What is shared (anonymized aggregates only)

A single JSON document on a periodic heartbeat (default every 24h), keyed by the
anonymous `installation_id`:

```jsonc
{
  "installation_id": "…",          // anonymous, random, per-install
  "schema_version": 1,
  "shared_at": "…Z",
  "window_days": 30,               // period the counts cover
  "backfill": true,                // true on the consent-time history send
  "instance": {
    "trinity_version": "…",        // short git commit
    "edition": "community" | "enterprise",
    "platform": "Linux",
    "python_version": "3.13.x"
  },
  "enterprise_features": ["…"],    // coarse module ids (already in /api/version)
  "counts": {                      // COUNTS ONLY
    "agents": 3,
    "executions_total": 22,
    "executions_success": 21,
    "executions_failed": 1
  },
  "activation_funnel": {           // Tier-1 wizard-step counts
    "setup_started": 1, "setup_step_create": 1,
    "setup_step_credential": 0, "setup_completed": 0, "setup_dismissed": 0
  }
}
```

**Never shared:** no message/chat content, no prompts, no agent outputs, no
credentials or tokens, no emails, no user identities, **no agent names** — only
coarse counts and enums. The exact payload for your instance is **inspectable
before you consent** in the Settings → Usage sharing panel (expand "Exactly what
would be shared").

## Retroactive backfill at consent

Because Tier-1 records locally from t=0, turning sharing on offers to include the
last N days of local history (default 30, your choice) in the first send, so your
benchmarks are accurate even though consent arrived later. This is disclosed at
the moment of consent.

## Reversibility

Turn **Usage sharing** off (Settings → General) and egress stops at the next
heartbeat — no further data leaves. For a hard, air-gapped guarantee set
`TELEMETRY_SHARING_ENABLED=false` (or `DO_NOT_TRACK=1`): the toggle then can't
enable egress at all.

The reciprocity benchmark view (Settings → Activation → "Fleet benchmarks") is an
entitlement-gated enterprise surface; the consent + egress mechanism above is
OSS-core and available in every edition.
