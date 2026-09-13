# Product Telemetry & Fleet Sharing

Trinity has a two-tier telemetry model. Tier 1 records anonymous product events **locally** — on by default, nothing leaves your server. Tier 2 is an **admin opt-in** that shares coarse anonymized aggregates with a hosted benchmark service in exchange for fleet benchmarks — off by default and reversible.

## Concepts

| Tier | What it is | Default | Egress |
|------|-----------|---------|--------|
| **Tier 1 — Local product events** | A small fixed allow-list of onboarding/setup step events, recorded in Trinity's own database | On (no toggle) | None — stays on your server |
| **Tier 2 — Fleet sharing** | A periodic share of coarse anonymized aggregates to a hosted benchmark service, for reciprocal fleet benchmarks | Off (admin opt-in) | Only when consented **and** enabled |

The two tiers are independent: Tier 1 never sends anything, and turning Tier 2 off (or leaving it off) leaves Tier 1 exactly as it was.

- **Share id** — a random id minted when you turn sharing on and discarded when you turn it off. It keys every share. It is never your install id and is never sent beside it.
- **Install id** — a separate, local identifier that identifies the instance to the security & product updates contact form. Opting in there (**Settings → General → Security & product updates**) is the one way an operator mints it; merely looking at a page never creates one.

## How It Works

### Tier 1 — Local product events (nothing to do)

Trinity records anonymous local product events — a small fixed allow-list of onboarding/setup step events — in its own database. There is no toggle and no action to take: it is default-on, private, and produces zero network egress. Nothing leaves your server.

### Tier 2 — Fleet sharing (admin opt-in)

Tier 2 has two surfaces, both admin-only:

- **The ask.** After login, a **Finish setup** card on the Dashboard carries a **Help improve Trinity** section with an **Off by default** badge. It offers three choices: **Share anonymous usage** (turns sharing on, with the last 30 days of history), **Not now** (hides the ask in this browser for 14 days; nothing is sent to the server), and **Don't ask again** (a once-per-install marker that stops the ask on every browser and device; audit-logged). During a snooze the ask returns **once per browser** — headed **Your first scheduled run just completed** — after the instance's first successful scheduled or webhook run. Consenting, **Don't ask again**, or a config hard-disable all silence it permanently.
- **The panel.** **Settings → General → Usage sharing** holds the reversible **Share anonymous usage** toggle, the backfill choice ("On consent, also share the last **7 days / 30 days / 90 days / no history** of local history"), the share id and its rule, an **Exactly what would be shared (inspect before you consent)** preview of the exact payload, and **Recent sends**.

Both surfaces let you expand the exact payload before consenting, so nothing is shared sight-unseen.

Turning it on:

- Mints a fresh share id and fires a one-shot **backfill** of recent aggregates over the chosen window.
- Is **audit-logged** — see [Audit Trail](audit-trail.md). The audit row records only *whether* a new share id was minted, never the id.
- Is **reversible** — opting out flips the consent setting, deletes the share id locally, and the next heartbeat stops sending. Re-consenting mints a different id. Anything already sent stays with the receiver; on request it can be deleted there by the id shown on the panel.

### The two-gate model

Egress requires **two independent gates**, both on:

1. **Stored consent** — the admin opt-in (`telemetry_sharing_enabled`), default off.
2. **Config switch** — `TELEMETRY_SHARING_ENABLED`, which also honors the cross-tool `DO_NOT_TRACK` environment variable.

If **either** gate is off, nothing leaves the box. When the config switch is off the panel says so ("Sharing is disabled by configuration") and the toggle stays off; a consent request is refused with 409. An air-gapped or blocked send never affects the platform — every share is best-effort.

### Cadence

With both gates on, a background heartbeat wakes every 10–20 minutes and checks whether a share is **due**: a share is due when the last delivered one is older than the sharing interval (`TELEMETRY_SHARING_INTERVAL_HOURS`, default 24). The decision is made from the persisted last-delivered stamp, so a **backend restart never resets the cadence** — an install that restarts daily still shares once a day. Only one worker sends per interval.

The consent-time backfill is retried on every wake until the receiver first acknowledges it, so history disclosed at consent is not lost if the receiver is unreachable at the time. After that, each share covers everything since the last delivered one.

### Delivery and the receiver

Every send — success or failure — is recorded in **Recent sends** (the last five attempts) with its time, whether it was a backfill or a heartbeat, the window it covered, **the receiver it went to**, and the HTTP status or the error class. Expand an entry to see the exact payload it carried; an attempt that failed before a payload was built says so.

The panel's status line is decided from that record, never from the address configured right now:

- **Last delivered <date> to <receiver>** names the receiver that acknowledged the last delivery.
- **The receiving service at <receiver> acknowledged the last send.** — normal operation.
- **The receiving service answered 404 at the default address. The send is recorded here and retried automatically.** — the hosted receiver did not accept the share.
- **The receiver at <receiver> answered 404. Check `TELEMETRY_SHARING_URL`.** — a receiver at a non-default address is not accepting shares.
- **The last send to <receiver> failed; it is recorded below and retried automatically.**
- If the newest send went to a different address than the one now configured, the panel says so: *"That send went to X; sharing is now configured for Y, which has not seen it. The next scheduled send goes there."* (or, with sharing off, that nothing further leaves the box). An entry recorded before receivers were tracked reads "to an unknown receiver" and never claims a mismatch.

A dead receiver or an air gap is not a loop: after five consecutive failures, attempts drop to at most one per half-interval, and recover within half an interval once the receiver answers.

### Fleet benchmarks

With sharing on, exactly one other request leaves the box under the same two gates: the **Activation** tab in Settings (**requires an entitlement**) asks the hosted service for this instance's standing and renders it in a **Fleet benchmarks** card. The request carries only the share id. The card shows one of five outcomes — not sharing, pending, not enough data yet, ready, or unavailable — with a plain-language message, and when ready a three-row table (**Execution success rate**, **Executions per day**, **Agents**) with your value, the fleet median, and your percentile, plus how many instances it was compared against and over what window. Changing the funnel window does not re-fire the request; a slow receiver never hides the funnel.

The same tab prints the install id in its footer only once one exists; opening the tab never mints one.

## What Is / Isn't Shared

**Shared (coarse, anonymized aggregates only):**

- Release version (never a commit hash)
- Platform type and edition
- How the instance was installed (the install lane, or `unknown`)
- List of entitled features
- Counts — agents, executions, and activation-funnel steps
- An outcome mix — how runs ended, by trigger type and by status, plus counts of provider rate-limit and auth refusals
- The random **share id** — never your install id

The payload is validated against its documented schema (version 2) before every send; anything outside it is refused, logged, and recorded in Recent sends as a schema error — never sent. The annotated schema is in [`PRODUCT_EVENTS.md`](../../PRODUCT_EVENTS.md).

**Never shared:**

- PII
- Message content or prompts
- Emails
- Agent names

## For Agents / Admins

Tier 2 consent is an **admin, human-only** decision — agent-scoped keys cannot toggle it, dismiss the ask, or read the status.

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/api/settings/telemetry-sharing` | GET | Admin, human-only | Sharing status — consent, share id, backfill window, interval, the scrubbed share URL, `last_shared_at` / `last_shared_host`, `recent_sends`, `receiver_host` / `configured_host` / `receiver_mismatch` — plus a `payload_preview` of the exact aggregates that would be sent (`?preview=0` skips the preview) |
| `/api/settings/telemetry-sharing` | PUT | Admin, human-only | Set consent — body `{enabled, backfill_days}`; enabling mints a share id, fires a one-shot backfill, and is audit-logged; 409 when hard-disabled by config |
| `/api/settings/telemetry-sharing/ask/dismiss` | POST | Admin, human-only | "Don't ask again" — stamps the once-per-install marker; idempotent, audit-logged |

`GET /api/settings/feature-flags` (any authenticated user) carries four read-only booleans the ask decides from: `telemetry_sharing_enabled`, `telemetry_sharing_hard_disabled`, `telemetry_sharing_dismissed`, `telemetry_sharing_first_value`.

The generic `PUT /api/settings/{key}` refuses every `telemetry_sharing_*` key — the routes above are the only writers. The generic admin `DELETE /api/settings/{key}` stays open for them as the reset path: deleting `telemetry_sharing_dismissed_at` makes the ask reappear; deleting `telemetry_sharing_id` while sharing is on re-mints a fresh id at the next send; deleting `telemetry_sharing_last_shared_at` costs one re-share within 10–20 minutes (consent still gating).

**Configuration (environment):**

| Variable | Purpose |
|----------|---------|
| `TELEMETRY_SHARING_ENABLED` | Hard config switch — the second gate; honors `DO_NOT_TRACK`. Default on, but no egress without stored consent |
| `TELEMETRY_SHARING_URL` | Hosted benchmark intake endpoint |
| `TELEMETRY_SHARING_INTERVAL_HOURS` | How often aggregates are shared once opted in (default 24) |
| `TELEMETRY_SHARING_BACKFILL_DEFAULT_DAYS` | Default backfill window when consent is granted (default 30) |

Full API reference: http://localhost:8000/docs

## Limitations

- Tier 1 local events are default-on with no toggle — by design, they never leave your server.
- Tier 2 sends **aggregates only**; it is not a stream of individual events and cannot be used to reconstruct activity.
- The hosted funnel/benchmark analytics that consume these aggregates are a separate surface behind an entitlement; this page covers only the opt-in, what is shared, and what the benchmark card renders.
- Sharing runs on the configured interval — it is not real-time.
- Changing `TELEMETRY_SHARING_URL` does not start a new delivery episode: the last-delivered stamp and the backfill marker still count an acknowledgement from the previous address. The panel flags the mismatch, but the new receiver only sees shares from the next heartbeat on.

## See Also

- [Audit Trail](audit-trail.md) — Where the consent change is recorded
- [Monitoring](monitoring.md) — Fleet health checks and heartbeats
