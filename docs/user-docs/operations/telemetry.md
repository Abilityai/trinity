# Product Telemetry & Fleet Sharing

Trinity has a two-tier telemetry model. Tier 1 records anonymous product events **locally** — on by default, nothing leaves your server. Tier 2 is an **admin opt-in** that shares coarse anonymized aggregates with a hosted benchmark service in exchange for fleet benchmarks — off by default and reversible.

## Concepts

| Tier | What it is | Default | Egress |
|------|-----------|---------|--------|
| **Tier 1 — Local product events** | A small fixed allow-list of onboarding/setup step events, recorded in Trinity's own database | On (no toggle) | None — stays on your server |
| **Tier 2 — Fleet sharing** | A periodic share of coarse anonymized aggregates to a hosted benchmark service, for reciprocal fleet benchmarks | Off (admin opt-in) | Only when consented **and** enabled |

The two tiers are independent: Tier 1 never sends anything, and turning Tier 2 off (or leaving it off) leaves Tier 1 exactly as it was.

## How It Works

### Tier 1 — Local product events (nothing to do)

Trinity records anonymous local product events — a small fixed allow-list of onboarding/setup step events — in its own database. There is no toggle and no action to take: it is default-on, private, and produces zero network egress. Nothing leaves your server.

### Tier 2 — Fleet sharing (admin opt-in)

An admin can opt in from **Settings** to share coarse anonymized aggregates with a hosted benchmark service and receive fleet benchmarks in return. Before consenting, the admin can **inspect the exact payload preview** in Settings, so nothing is shared sight-unseen.

Turning it on:

- Fires a one-shot **backfill** of recent aggregates (window set by `backfill_days`).
- Is **audit-logged** — see [Audit Trail](audit-trail.md).
- Is **reversible** — opting out flips the consent setting and the next cycle stops sending immediately.

### The two-gate model

Egress requires **two independent gates**, both on:

1. **Stored consent** — the admin opt-in (`telemetry_sharing_enabled`), default off.
2. **Config switch** — `TELEMETRY_SHARING_ENABLED`, which also honors the cross-tool `DO_NOT_TRACK` environment variable.

If **either** gate is off, nothing leaves the box. An air-gapped or blocked send never affects the platform — every share is best-effort.

## What Is / Isn't Shared

**Shared (coarse, anonymized aggregates only):**

- Release version (never a commit hash)
- Platform type and edition
- How the instance was installed (the install lane, or `unknown`)
- List of entitled features
- Counts — agents, executions, and activation-funnel steps
- An outcome mix — how runs ended, by trigger type and by status, plus counts of provider rate-limit and auth refusals
- A random **share id**, minted when you turn sharing on and discarded when you turn it off — never your install id

The payload is validated against its documented schema before every send; anything outside it is refused and recorded, never sent. The last five send attempts are kept locally and shown in Settings → Usage sharing → **Recent sends**.

**Never shared:**

- PII
- Message content or prompts
- Emails
- Agent names

## For Agents / Admins

Tier 2 consent is an **admin, human-only** decision — agent-scoped keys cannot toggle it.

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/api/settings/telemetry-sharing` | GET | Admin | Sharing status plus a `payload_preview` of the exact aggregates that would be sent |
| `/api/settings/telemetry-sharing` | PUT | Admin, human-only | Set consent — body `{enabled, backfill_days}`; enabling fires a one-shot backfill and is audit-logged |

Observability: `telemetry_sharing_enabled` appears in `GET /api/settings/feature-flags` (read-only status; the routing gate is the config switch above).

**Configuration (environment):**

| Variable | Purpose |
|----------|---------|
| `TELEMETRY_SHARING_ENABLED` | Hard config switch — the second gate; honors `DO_NOT_TRACK`. Default on, but no egress without stored consent |
| `TELEMETRY_SHARING_URL` | Hosted benchmark intake endpoint |
| `TELEMETRY_SHARING_INTERVAL_HOURS` | How often aggregates are shared once opted in |
| `TELEMETRY_SHARING_BACKFILL_DEFAULT_DAYS` | Default backfill window when consent is granted |

Full API reference: http://localhost:8000/docs

## Limitations

- Tier 1 local events are default-on with no toggle — by design, they never leave your server.
- Tier 2 sends **aggregates only**; it is not a stream of individual events and cannot be used to reconstruct activity.
- The hosted funnel/benchmark analytics that consume these aggregates are a separate enterprise surface; this page covers only the user-facing opt-in and what is shared.
- With both gates on, sharing runs on the configured interval — it is not real-time.

## See Also

- [Audit Trail](audit-trail.md) — Where the consent change is recorded
- [Monitoring](monitoring.md) — Fleet health checks and heartbeats
