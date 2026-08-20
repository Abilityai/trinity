# Subscription Headroom History (ent#433)

> The durable half of #471's live headroom. One row per probe, so "how close did
> we run to the 5h wall this week" is answerable — a question the live snapshot
> structurally cannot answer, because it keeps exactly one reading per
> subscription and overwrites it on every probe.

**Issue**: abilityai/trinity-enterprise#433 (P2, `theme-monetization`, epic ent#94)
**Extends**: [subscription-usage-tracking.md](subscription-usage-tracking.md) (#471)
**Consumer sibling**: ent#259 (grid pressure tile — merged point-in-time only, PR #2327)
**OSS-core by explicit decision** — the #471 gate ruling carries over: visibility is
ungated, the paid layer is governance. Recorded so it is never inferred backwards from
the mere fact that it merged (the ent#326 discipline).

---

## Flow

```
  click ── POST /api/subscriptions/{id}/usage/refresh ─┐
                                                       ├─► get_headroom(force|ambient)
  poll ─── GET /api/agents/subscription-pressure ──────┘            │
                                                                    ▼
                                                             _locked_probe        (SingleFlightLock)
                                                                    │
                                                                    ▼
                                                            _probe_and_store
                                                             │       │       │
                        _probe() ────────────────────────────┘       │       └──► _record_history()   ◄── NEW
                     (httpx → api.anthropic.com, headers)             │             asyncio.to_thread
                                                                      ▼             fail-open
                                                              _store_snapshot              │
                                                              (Redis, 7d TTL)              ▼
                                                                              db.insert_headroom_history
                                                                          → subscription_headroom_history
                                                                                           │
   READ                                                                                    │
   GET /api/subscriptions/{id}/headroom/history?window= ──► get_history() ──► db.get_headroom_history
                                                            (pure DB, never probes)        │
   RETENTION                                                                               │
   cleanup_service._sweep_headroom_history ──► _guard_allows ──► db.prune_headroom_history ┘
   cleanup_service._sweep_rate_limit_events ─► _guard_allows ──► db.cleanup_old_rate_limit_events
   db.delete_subscription ──────────────────► in-transaction DELETE ─────────────────────────┘
```

---

## Why the write sits in `_probe_and_store`

It is the single chokepoint every probe passes through — `get_headroom`'s `force`
branch and both ambient branches funnel through `_locked_probe` into it. Placing the
hook there means it **inherits #471's entire rate-bounding envelope for free**: the
60s per-subscription floor, the cross-worker single-flight, and the fail-CLOSED
ambient gate that stops the 60s dashboard poll from becoming a write storm.

It therefore adds **no probe**. That property is load-bearing: the issue forbids this
work from creating pressure to probe more often, because probes spend the operator's
own subscription quota.

## Three properties of the write, each load-bearing

1. **Off the event loop.** `db.*` is synchronous SQLAlchemy, the platform DB runs
   DELETE journal mode with a 30s busy timeout, and this coroutine runs inside a
   fire-and-forget task on the dashboard-poll path. A sync write landing during the
   03:30 backup (which holds a read transaction for its whole duration) or the 04:30
   VACUUM (exclusive, minutes) would block the **entire** loop — `/health`, the WS
   dispatcher, every in-flight request — for up to 30 seconds. `try/except` handles
   *errors*, not *blocking*; `asyncio.to_thread` handles both.
2. **After the Redis write, never before.** The snapshot is what every live surface
   reads, so a slow or contended DB write must never delay it. Pinned by test —
   a refactor could otherwise invert it silently.
3. **`except Exception`, never `BaseException`.** Shutdown cancels the await, and
   `CancelledError` is not an `Exception`; it must keep propagating.

## What is persisted, and what deliberately is not

Every probe that **ran** is persisted, failures included as status-only rows. Skipping
failures would make a three-day dead token byte-identical to nobody-watching.

`no_windows` is a history-local classification of the one genuinely ambiguous case:
`parse_unified_headers` returns a result when only the bare top-level `status` header
arrives, so `status='ok'` with neither window reported is reachable, and persisting it
verbatim yields an all-NULL row indistinguishable from a botched write — permanently,
once it is in the table. The live #471 snapshot keeps its own status vocabulary
untouched; both of its readers already handle that case by inspecting the windows.

A probe that **never ran** records nothing. A subscription with no usable token returns
before any HTTP call, and persisting that would emit one row every 15 minutes forever
for a pure configuration state — the highest-volume, lowest-information row available.
**So a gap has three causes** — nobody watched, no usable token, auto-refresh off — and
no consumer may present a gap as any one of them.

---

## The series is `last`-per-bucket, never `max`

Three independent arguments, any one of which is sufficient:

1. **Observer effect.** Probes are demand-driven — they fire only on an HTTP request —
   so samples-per-bucket is proportional to operator attention. `E[max of n]` rises
   monotonically with `n`, so an hour watched during an incident out-reads an identical
   unwatched hour. The bias runs the wrong way twice: the unattended overnight burn,
   the thing most worth seeing, gets the fewest samples and therefore the lowest max.
2. **Two-peak ambiguity.** 5h and 7d are independent metrics that peak at different
   instants inside one bucket, so "the peak sample's timestamp" is undefined for a
   two-column response. `last` yields ONE correlated snapshot of both windows.
3. **Invisible 429s.** A 429 legitimately carries `status='rate_limited'` with
   `utilization_pct = NULL`. Under a `MAX(utilization)` read the single most important
   sample in the series vanishes and the chart flatlines through an outage.

The headline question is still answerable: the consumer takes max **across** buckets,
which is a far less biased estimator than max **within** bucket.

**Selection is `ROW_NUMBER() OVER (PARTITION BY bucket ORDER BY fetched_at DESC)`**,
never a bare non-aggregated column beside an aggregate — that is a SQLite-only
extension and raises `GroupingError` on PostgreSQL. Because PostgreSQL only runs in
the test matrix when `TEST_POSTGRES_URL` is set, the dialect is pinned by **compiling**
the query for the PG dialect, which runs everywhere.

## Honest gaps need TWO timestamps

The AC asks that consumers can detect gaps from "real timestamps, no synthetic fill".
Real timestamps **alone are insufficient**, and this was the plan's original error:
sample jitter and a true gap are indistinguishable from timestamp deltas. With hourly
buckets, a sample at 10:05 followed by one at 11:55 is 1h50m apart with **no** gap;
10:55 followed by 12:05 is 1h10m apart **with** one.

So each bucket carries the logical `bucket_start` **and** the real `fetched_at`.
Buckets with no sample are simply absent — nothing is interpolated or zero-filled.
`coverage_pct` (buckets observed ÷ buckets elapsed) lets a thin series report itself as
thin instead of rendering as a confident flat line.

---

## Retention — two windows

| Key | Default | What it sweeps |
|-----|---------|----------------|
| `subscription_headroom_retention_days` | 30 | the new probe-history table |
| `subscription_failure_event_retention_days` | 30 | `subscription_rate_limit_events` |

The second is a **conversion, not an addition**. That table held the platform's only
durable record of *real agent work* hitting a provider rate limit — timestamped and
attributed to the causing agent — and destroyed it at a **hardcoded 24 hours** with no
operator-visible window, no #1644 blast-radius guard, and no `GET /api/settings/retention`
entry, while every sibling table had all three. It was doubly capped: the pressure call
site also hardcodes `hours=24`.

Widening 1 day → 30 is the #1638-**safe** direction: no install loses data, and it
cannot change any existing answer because every consumer already filters by time itself
(`hours=24` at the call site, a 2h predicate for `rate_limited_now`).

Both windows: registered in `RETENTION_OPS_KEYS`, range-validated in
`OPS_SETTINGS_VALIDATION`, exactly one `_guard_allows` site each (enforced by the
`test_1771a` AST guard), surfaced automatically on `GET /api/settings/retention`, and
logged at boot. Each guard's candidate count **shares the prune's predicate by
construction** (`_headroom_history_prune_predicate` / `_rate_limit_event_prune_predicate`)
— a guard that counts a different row set than the prune removes protects nothing.

Neither is in `COMMUNITY_FRESH_INSTALL_SEED`: the 5-day floor would silently truncate
the 7-day default read window while the UI labelled it 7 days.

**No first-sweep blast radius.** This is a new table, so nothing is older than 30 days
for the first 30 days. Steady state is a trickle — #471's floors bound probing to ≤1
ambient probe per 15 min per subscription — so a 5-minute cycle's candidate set is
single digits, three orders of magnitude under the guard threshold. The guard only
fires if an operator NARROWS the window, which is exactly the #1644 case it exists for.

---

## Cascade, and the race that is deliberately accepted

`delete_subscription` deletes history rows **in its own transaction**. The DDL's
`ON DELETE CASCADE` is decorative: `PRAGMA foreign_keys` is off platform-wide, and the
PG DDL path regex-strips every FK clause, so the platform has **zero enforced FKs**.
The Alembic revision therefore declares **no** constraint — writing the natural
`ForeignKeyConstraint` would create the platform's first enforced FK and make the
backends diverge on exactly the race below (silent orphan on SQLite,
`ForeignKeyViolation` on PostgreSQL).

Accepted residual: `get_headroom(wait=False)` spawns a background probe that can still
be in flight and land its INSERT after the delete commits. That orphan is reaped by the
retention sweep. It is not worth a pre-INSERT existence check, which would add a read to
every probe to close a window measured in seconds.

---

## Read surface

`GET /api/subscriptions/{id}/headroom/history?window=24h|7d|30d`

- `assert_admin` (which also rejects agent principals, #1890), mirroring `/usage`.
- Resolves by **id OR name** then 404 — parity with `/usage`, so an operator who just
  used a name there does not find it rejected here, and a typo 404s rather than
  returning an empty series that reads as "no data yet".
- Unknown `window` → **422 with a named reason**, deliberately a hard reject rather than
  a silent fall back to the default: this parameter is the chart's *axis*, and quietly
  redrawing a window the caller never asked for is the wrong kind of forgiving.
- Granularity: hour for 24h/7d, day for 30d — bounding the response by construction.
- **Read-only. Never probes.** Viewing a trend costs no subscription quota.

## Consumer

This ships the **backend only**. The realistic ent#259 consumer is a compact in-tile
**sparkline**, not a labelled trend chart: FleetGrid v1 renders every tile in exactly
one cell (`cells` is declared and deliberately ignored), the tile body is
`overflow: hidden` with no scroll, and its row list silently drops anything past a
hardcoded cap of 4. `SubscriptionsPanel.vue` is the roomier second consumer.

## Storage shape — why not `product_events`

That table has no `subscription_id`, binds these rows to the activation-funnel's own
retention window, and **egresses**: `telemetry_sharing_service.build_aggregate_payload`
counts it by type and POSTs it on Tier-2 opt-in. Per-subscription quota telemetry does
not belong there.

(Discovered while evaluating it: `prune_product_events` has **zero callers**, so
`product_events` grows unswept today. Registered as debt, out of scope here.)

---

## Files

| Layer | File |
|-------|------|
| Schema | `db/schema.py`, `db/tables.py` |
| Migrations | `db/migrations.py` (`subscription_headroom_history_table`), `migrations/versions/0041_subscription_headroom_history.py` |
| DB | `db/subscriptions.py` (insert / bucketed read / two candidate counts / two chunked prunes / two shared predicates / cascade) |
| Facade | `database.py` (5 hand-written delegations — no `__getattr__`) |
| Service | `services/subscription_headroom_service.py` (`_history_row`, `_record_history`, `HISTORY_WINDOWS`, `get_history`) |
| Retention | `config.py`, `services/settings_service.py`, `services/cleanup_service.py` (`_read_retention_setting`, two guarded sweeps) |
| API | `routers/subscriptions.py`, `models.py` |
| Tests | `tests/unit/test_433_headroom_history.py` (41) |
