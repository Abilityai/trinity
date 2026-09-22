# Agent Custom Metrics - Feature Flow

> **Status (ent#476 epic, complete as of ent#479)**: a declared metric now has
> exactly one declaration path, one write path and one read path.
> `template.yaml metrics:` is parsed into a **per-agent registry** (ent#477),
> points are recorded through **`record_metrics`** (ent#478), and
> `GET /api/agents/{name}/metrics` **reads them back from the point store** with
> one staleness rule (ent#479). The `metrics.json` file is **retired as a
> source**: it is no longer read by anything, is never served as a current
> number, and an agent still writing one gets the **D-010** compatibility
> finding. `MetricsPanel.vue` was deleted as unreferenced in #2492; the renderer
> is now `DeclaredMetricsTiles.vue` (ent#479).

> **Updated**: 2026-01-23 - Verified line numbers and added Dashboard Widget system documentation (dashboard.yaml).

**Feature ID**: 9.9
**Status**: Implemented
**Date**: 2025-12-10
**Last Updated**: 2026-09-22 (ent#479 — the read contract and freshness)

## The declared metric registry (ent#477)

The block an author writes is now read by the **backend**, validated, and
persisted per agent. That is what gives ent#478 a schema to validate recorded
points against and ent#479 definitions to render with.

```
template.yaml `metrics:`
  │
  ├─ create (github: / local: / snapshot import)
  │     crud.py resolver → tr.declared_metrics = metric_registry
  │                          .declared_metrics_from_template(...)
  │     crud._materialize_agent_files → reconcile_declared_metrics(source="create")
  │                                     (non-fatal, ghost-skipped, inside the
  │                                      destructive rollback fence)
  │
  ├─ git pull ✓ / reset-to-main ✓ / sync strategy=pull_first ✓
  │     routers/git.py::_refresh_metric_registry  (non-fatal; the summary goes
  │       to the log + the existing _audit_git details, NOT the response body)
  │
  ├─ container start  (T1)
  │     lifecycle.start_agent_internal → metric_registry
  │       .spawn_refresh_from_running_agent(source="start")   fire-and-forget
  │
  └─ POST /api/agents/{name}/metrics/definitions/refresh
        routers/agent_files.py  (AuthorizedAgentByName, running agent only)

  every live path ───► metric_registry.refresh_from_running_agent
                          docker exec `timeout N head -c 256K template.yaml`
                          → utils.safe_yaml.load_template_yaml
                          → services/template_metrics.normalize_declared_metrics
                          → db.metric_definitions.reconcile()
                                insert · update · revive · retire
                                (UNIQUE(agent_name, name), one transaction)
                  ▲
  GET /api/agents/{name}/metrics/definitions ── AuthorizedAgentByName ──┘
  services/compatibility/static_checks.c_d009 ── metric_shape_errors (no DB)
```

**Key properties**

| Property | Why |
|---|---|
| The reader (`services/template_metrics.py`) is a stdlib-only leaf and **never raises** | The creation path sits inside the destructive rollback fence; `template_service` imports the sibling leaves, so an import back would close a cycle |
| An entry with **any** error is dropped and named | The registry must never hold a half-valid definition — ent#478 validates points against these rows |
| An **unreadable** template changes nothing | A failed exec is absence of evidence, not "the author removed the block" (#2196). Only a template that *parsed* and carries no `metrics:` retires rows |
| A **`type` change is refused** | Points are stored by name; a shape flip makes prior points uninterpretable. `type_conflict` records the refusal and the definitions read surfaces it |
| Rows are retired, never deleted | Points recorded under a name still need a definition to interpret them |
| The live read uses `execute_command_in_container`, never the volume path | The stopped-agent read spawns a throwaway container, and no request-triggered route may create one as a side effect of a read (409 instead) |

**Files**

| Layer | File |
|---|---|
| Reader (leaf) | `src/backend/services/template_metrics.py` |
| Store | `src/backend/db/metric_definitions.py` (+ `schema.py`, `tables.py`, `migrations.py`, `migrations/versions/0066_metric_definitions.py`, `agent_cleanup.py`, `database.py` facade) |
| Service | `src/backend/services/metric_registry.py` |
| Hooks | `services/agent_service/crud.py`, `routers/git.py`, `services/agent_service/lifecycle.py` |
| Routes | `src/backend/routers/agent_files.py` |
| Compatibility | `services/compatibility/{spec,static_checks}.py` — `D-009` |

**Known gap**: an agent that runs `git pull` itself without restarting is
covered by no hook. The remedy is the refresh route, reachable by the agent as
the `refresh_metric_definitions` MCP tool (ent#478) — which is what the
undeclared-metric 422's hint names.

Requirement: `docs/memory/requirements/lifecycle-observability.md` §47.

---

## Recording points against those declarations (ent#478)

The registry says which metrics exist; this is how their values get in. There
is exactly one write path — the `record_metrics` MCP tool — and no second one.

```
agent turn (execution_id is in the Execution Context block)
  │ record_metrics(points[], idempotency_key?, execution_id?)
  ▼
mcp-server/src/tools/metrics.ts      agent-scoped key only; never throws
  │ client.recordMetrics(agent, body)
  ▼
POST /api/agents/{name}/metrics/points        routers/metric_points.py
  ├─ AuthorizedAgent + self-gate       an agent key records only as itself
  ├─ rate limit (agent_metrics:{name}) · size guard (2 MiB, post-parse)
  ├─ execution provenance              backend-confirmed, else NULL
  ├─ idempotency begin(scope, key)     key BOUND to sha256(canonical points)
  ├─ db.list_metric_definitions(name, include_retired=True)     ← the registry
  ├─ metric_points_service.validate_batch(defs, points, now)    ← pure leaf
  │     → rows[] | errors[]   422 all-or-nothing, one reason code per point
  ├─ daily write cap → 429 daily_point_cap_exceeded + Retry-After
  ├─ db.insert_metric_points(name, rows)   ON CONFLICT DO NOTHING on identity
  └─ 201 {recorded, deduplicated, replayed, points[{index, ts, key}]}

cleanup cycle (300 s)  →  _sweep_metric_points  →  guarded ts-range prune
```

**What the agent has to understand, and where it learns it.** The tool
description carries four rules, because each is one an author can get wrong in
a way the platform cannot detect afterwards: declare the metric first (the
`metric_undeclared` hint names `refresh_metric_definitions`, which ships in the
same module so the remedy is reachable from where the error is read); values
are not coerced; identity is `(metric, ts, dims)` and **excludes the value**, so
a correction is a new `ts` rather than a new number at the same one; and
passing `execution_id` is what makes a re-delivered turn replay instead of
recording twice.

**Two idempotency layers, for two different failures.** The row key
(`sha256(metric \0 ts \0 canonical_dims)`, which is also the tail of the primary
key) holds with no client key, no Redis and no execution id — it is the
invariant. The batch key gives Invariant #18's "returns the first result" for a
re-delivered turn; where no client key is supplied but `execution_id` resolves
to the calling agent, it is derived from that execution, which is what dedups a
batch of `ts`-less points whose timestamps would otherwise be freshly assigned
on the retry. With neither, a retry is a new observation — stated in the tool
description rather than papered over with a body hash, because the same numbers
an hour later are usually a genuine new observation.

**The batch key is bound to the body.** `idempotency_keys` stores no request
fingerprint, so a claim on the client key alone would make the key identify the
*caller* rather than the batch: an agent stamping a constant `idempotency_key`
on every turn gets the first batch's snapshot for 24 hours, with `replayed:
true` and no 4xx — silent metric loss, where the equivalent on `/chat` is only
a stale answer. Both branches therefore fold the canonical points payload into
the claim, so the same batch replays and a different one is recorded.

**The size guard bounds storage, not parse cost.** Starlette has buffered and
Pydantic has validated the body before the handler runs; the 2 MiB cap is
honest about being a storage bound, and the per-field
`METRIC_VALUE_TEXT_MAX_LEN` (1024) is what stops one text `value` from being
the whole batch.

**Failure is classified, not blanket.** A store outage is a 503 the tool
reports as `retryable`; a batch the database rejects on its content is a 500
the tool reports as explicitly not retryable, because an agent told to retry
that would retry forever. A refusal never ends the turn.

Requirement: `docs/memory/requirements/lifecycle-observability.md` §48.

---

## Overview

An agent declares its domain KPIs in `template.yaml`, records observations
against those declarations with `record_metrics`, and Trinity reads them back
through one route. That is the whole loop, and each arrow has exactly one
implementation — the point of the ent#476 epic.

## Flow Diagram

```
┌──────────────────────┐   ┌──────────────────────┐   ┌──────────────────────┐
│  template.yaml       │   │  the agent calls     │   │  an operator opens   │
│  metrics:            │   │  record_metrics(...) │   │  the Dashboard tab   │
│  name/type/label/    │   │  one or many points  │   │                      │
│  cadence/direction   │   │                      │   │                      │
└──────────┬───────────┘   └──────────┬───────────┘   └──────────┬───────────┘
           │ reconcile (ent#477)      │ validate (ent#478)       │
           ▼                          ▼                          ▼
   metric_definitions  ◀── join ──▶  metric_points     GET /api/agents/{n}/metrics
   (the declaration)                 (the values)      services/metric_read_service
                                                                 │
                                       ┌─────────────────────────┼───────────────┐
                                       ▼                         ▼               ▼
                              DeclaredMetricsTiles      MCP get_metrics    get_agent_health
                              (+ bound dashboard.yaml   (agent-scoped)     .metrics block
                                 widgets)                                  (informational)
```

**No container is contacted on the read.** The store answers, so a *stopped*
agent returns exactly what a running one would — the legacy proxy replied
"Agent must be running to read metrics", which made every number vanish at the
moment an operator most wanted to know what it had been.

## Metric Types

| Type | Description | Display | Example |
|------|-------------|---------|---------|
| `counter` | Monotonically increasing | Large number | "42 Messages" |
| `gauge` | Current value (up/down) | Number + unit | "12.5 Avg Words" |
| `percentage` | 0-100 value | Progress bar | "75% Success Rate" |
| `status` | Enum/state | Colored badge | "ACTIVE", "IDLE" |
| `duration` | Time in seconds | Formatted | "2h 15m" |
| `bytes` | Size in bytes | Formatted | "1.2 MB" |

## Template Schema

```yaml
# template.yaml
metrics:
  - name: messages_processed     # Internal identifier (snake_case, the join key)
    type: counter                # counter|gauge|percentage|status|duration|bytes
    label: "Messages"            # Display label
    description: "Total messages"  # Tooltip text
    cadence: 1h                  # How often you intend to record it — this is
                                 # what makes staleness answerable (ent#479)
    direction: up_good           # up_good|down_good|neutral — whose trend is good
    aggregation: last            # last|sum|avg — how dimension series fold

  - name: success_rate
    type: percentage
    label: "Success Rate"
    warning_threshold: 80        # Yellow if below (read in the declared direction)
    critical_threshold: 50       # Red if below

  - name: current_state
    type: status
    label: "State"
    values:                      # Required for status type; `color` is reused
      - value: "active"          # by a bound dashboard widget
        color: "green"
        label: "Active"
      - value: "error"
        color: "red"
        label: "Error"
```

## Recording values

There is **one** write path, `record_metrics` (ent#478) — see
[Recording points against those declarations](#recording-points-against-those-declarations-ent478)
above.

### `metrics.json` is retired (ent#479)

Writing `~/metrics.json` no longer does anything. The backend does not read it,
the read route does not fall back to it when the store is empty, and the agent
server's own `GET /api/metrics` no longer parses it either — that route is
retired in place, answering `410` with
`{has_metrics: false, superseded_by: "record_metrics / GET /api/agents/{name}/metrics", finding: "D-010"}`
rather than being deleted (§49.7). Two sources for one number is the condition this epic exists to remove,
and "serve the file when we have nothing better" is precisely how two sources
drift apart unnoticed.

Instead, the file is a **named finding**. Compatibility check **D-010** (SOFT,
static) reports it, lists its keys, and separately lists the keys that have no
`template.yaml metrics:` entry — "you still write this file" is advice, "these
numbers are declared nowhere" is a fix. The read route echoes the persisted
finding into `findings[]`, with `findings_evaluated_at` so an empty list before
the first compatibility run reads as *not evaluated* rather than *clean*.

To migrate: declare each key in `template.yaml metrics:`, call
`refresh_metric_definitions`, replace the file write with `record_metrics`, and
delete the file.

## Reading them back (ent#479)

### `GET /api/agents/{name}/metrics`

| Aspect | Contract |
|---|---|
| Gate | `AuthorizedAgentByName` (uniform 404 — Invariant #8/#186), **then** the agent self-gate: an agent-scoped key reads only its own numbers (403). Cross-agent reads are ent#80's grant. |
| Rate limit | 240/min per agent — clears N tabs at a 30 s poll, stops a loop |
| Window | `auto` (default) · `24h` · `7d` · `30d` · `90d`, or `since`/`until`. `auto` = `max(24h, 12 × cadence)` capped at 90 d, because a cadence ranges 60 s–1 y and a fixed 24 h shows a weekly metric four points |
| Filters | `metric=<declared name>` · `include_retired` · `series_limit` (≤ 2000, single-metric path) |
| Errors | 422 `window_invalid` · 422 `metric_undeclared` (retired names get "retired at T — pass `include_retired=true`") · 503 `metric_store_unavailable` + `Retry-After: 30` |
| Series | Bucketed (≤ 120/series) by default; raw `points` only on the `metric=` path, truncation keeps the **newest**. A bucket covers the **window**: a point outside `[since, until]` is dropped before indexing, never clamped into bucket 0 |
| Dimensions | Grouped by `canonical_dims`, folded by the declared `aggregation`, with `latest_by_series[]` carrying each series' own freshness |
| Chart | `chart` is the one bucket list the sparkline, `stats` and a bound widget's `history` all read, so they describe the same thing as `latest.value`: `basis: "folded"` (the cross-series fold, for `sum`/`avg`) or `basis: "series"` + `dims` (for `last`, where a cross-series fold is undefined — the UI labels it) |

### The one staleness rule

```
stale  ⟺  cadence declared  AND  now − last_point_at > 2 × cadence
```

`metric_read_service.freshness()` is pure, exported and the ONLY implementation.
The tiles, the bound widgets, the health block and the role card all import it;
a second copy is a defect whether or not it currently agrees.

| `freshness` | `stale` | Meaning |
|---|---|---|
| `fresh` | `false` | a point arrived within 2× cadence |
| `stale` | `true` | it did not; `stale_after` says when it tipped |
| `no_cadence` | `null` | no `cadence:` declared — **never stale**; `null`, not `false`, because "not stale" and "unanswerable" are different claims |
| `no_points` | `false` | declared, never recorded — not *late*, not started |

Exactly `2 × cadence` is not stale (strict `>`), and a point in the future
(≤ 300 s of tolerated clock skew) clamps to fresh rather than going negative.

### Consumers

| Consumer | What it gets |
|---|---|
| `DeclaredMetricsTiles.vue` | The default agent dashboard — tiles with **no `dashboard.yaml` needed**, rendering for a **stopped** agent. Every tile shows its point time; a stale tile keeps its value under a warning chip and is never rendered as current |
| `dashboard.yaml` widgets | A `metric`/`status`/`progress` widget carrying `metric: <name>` is filled from the registry — see [agent-dashboard.md](agent-dashboard.md) |
| MCP `get_metrics` | Agent-scoped, never throws, maps `metric_undeclared` to a hint naming `refresh_metric_definitions` |
| `get_agent_health` | An **informational** `metrics` block. It never touches `aggregate_status` or `issues`: a business metric going stale is a fact about the agent's work, not its health |
| **The objective join (ent#666)** | `latest_by_metric` — the same folded tile value, with no series work — joined to the targets the agent's objective files set. See [the section below](#objectives-the-join-ent666) |

### Empty states name the next action

Zero declarations → "no metrics: block in template.yaml — declare one and pull,
restart the agent, or POST .../metrics/definitions/refresh". A declared metric
with no points → "declared, no points yet — record points with `record_metrics`
(or schedule `/update-dashboard` if the agent has that playbook)". The **route**
composes the sentence, so no surface can offer an action the agent cannot take;
it names both actions rather than branching, because a store-only read cannot
learn which playbooks a container holds (the catalog is a container probe, and
`agent_skills` knows only *library* assignments while the bundled templates
carry `/update-dashboard` in `.claude/commands/`). The earlier conditional took
a flag no caller could compute, which made half the sentence unreachable.

## Objectives: the join (ent#666)

A metric with no objective is fine — nobody set a target for it. A metric WITH
one is the question this section answers: **target vs actual, with freshness,
computed in exactly one place.**

Full contract: [requirements §50](../requirements/lifecycle-observability.md#50-objective--metric-join--one-read-of-target-vs-actual-with-freshness-trinity-enterprise666).

```
objectives/<id>.yaml  ──┐                    (Tandem §3.4, in the agent's canon)
  metrics: [{name,      │   agent door
            direction,  │   (AgentClient, <=2 in flight, abort on transport death)
            target, by}]│
                        ▼
      services/objective_join_service.py  ── the ONE join
                        │
        ┌───────────────┼────────────────────────────┐
        ▼               ▼                            ▼
  db.list_metric_   metric_read_service        metric_read_service
  definitions       .latest_by_metric          .freshness   (§49.1)
  (identity,        (actual — the SAME              (stale / fresh /
   direction)        fold as the tile)               no_cadence / no_points)
                        │
        ┌───────────────┴───────────────────────────────────────┐
        ▼                        ▼                              ▼
 GET /api/agents/          MCP get_objectives          role card (ent#527) /
   {name}/objectives         (agent-scoped)            hub (ent#661) /
                                                       proactivity (ent#605)
                                                       — in process, no second join
```

| Aspect | Contract |
|---|---|
| Gate | `AuthorizedAgentByName` (uniform 404) → agent self-gate (403) → limiter on the **validated** name — the `/metrics` order, verbatim |
| Rate limit | `OBJECTIVES_READ_RATE_LIMIT`, default **60**/min per agent — its own knob, a quarter of `/metrics`, because this read touches the **container** |
| Store-only? | **No.** Files are truth and they live in the container (E7/E13), so a stopped agent answers `unavailable: agent_stopped` with copy naming the fix — never a cached number |
| `actual` | the tile's folded latest via `latest_by_metric` — one number on every surface, parity-tested on a dimensioned `sum` metric |
| `gap.status` | `behind` · `on_target` · `ahead` · `off_target` (the `hold` arm) · `not_computable` — **position, never pace**. `by` and `horizon` ride the row so a consumer can judge pace itself |
| Stale | orthogonal: a stale metric keeps its gap and carries `stale: true`. The join reports; the consumer decides |
| Direction | registry first; `neutral` (the column default — no bundled template declares one) falls through to the objective's `up`/`down`/`hold` with `direction_source: objective`. The wire value stays inside the **registry's** three (`up_good`/`down_good`/`neutral`), so a declared `hold` is `neutral` and a direction-aware formatter needs no fourth case; the author's word rides along as `objective_direction`. Two *declared* directions that disagree are a `direction_mismatch` finding |
| Never a blank | an undeclared metric is a `metric_undeclared` finding with the fix; a **supporting-only** agent gets `metric_not_declared_here` instead, because "declare it and refresh" is advice it cannot take |
| Bounds | scan 100 files → filter → cap the **output** at 20; 12 metrics/objective; ≤ 2 reads in flight; 5 s per read and a 30 s budget for the whole fan-out (a *slow* agent raises no typed error, so the abort alone does not bound it) |
| Findings | belong to the objectives returned — another role's or a finished objective's parse defect never lands on this agent's read, since a shared fleet canon would otherwise put every role's mistakes on every card. File-level (`objective_invalid`, `objective_unreadable`, `objective_file_skipped`, `objectives_read_timeout`) are unconditional: nothing there says whose they are |
| Errors | 503 `metric_store_unavailable` + `Retry-After: 30`. Everything below transport is a **named field on a 200** |

### The rebase note for PR #2927 (role card, ent#527)

The role card shipped the first version of this join — and a second staleness
rule with it (a 30-day bound over `metrics.json`'s `last_updated`). ent#666
retires that half. On rebase onto this branch, `client_portal/role_card.py`
drops `_read_metrics`, `metric_row`, `objective_concerns`, `canon_root`, the
objectives loop and `MAX_OBJECTIVES` / `MAX_METRICS_PER_OBJECTIVE`, and keeps
`is_stale` / `STALE_AFTER_DAYS` **only** for the role file's `review_by`
(framework §3.5 governs files, not metrics). After the role read it calls
`objective_join_service.read_objective_join(agent_name, template=template,
client=client)` — function-locally, so no portal suite drags the metrics stack
in — and copies `objectives` / `findings` / `summary` onto the card.

Two things ride that rebase rather than this branch:

* **A slim portal projection** (TD-4). The card exposes
  `name, target, actual, last_point_at, stale, freshness, gap.status,
  finding.code` with client-safe copy per code — **not** `ObjectiveMetricRead`
  whole. The findings here are operator-facing remediation ("call
  `refresh_metric_definitions`") and `owner: role:<id>` names a canon an
  external client does not own (#78 auth-path invariant).
* **The portal route gains the limiter.** `GET …/client-portal/agents/{name}/role`
  has none today and reaches the same container fan-out; it takes the same
  `agent_objectives_read:{name}` key, so one key bounds both doors. The limiter
  stays in the routers — it is transport (Invariant #1).

**Alembic: two chains fork at `0065`, and whichever lands second re-chains.**
This branch adds no revision, but ent#527/#2927 carries
`0066_public_user_memory_writes` + `0067_agent_role_readiness` (both currently
`down_revision = "0065…"`), while the metrics epic carries
`0066_metric_definitions` → `0067_metric_points`. Two revisions sharing a
`down_revision` are **two heads**, and `alembic upgrade head` resolves its
target before applying anything, so such a graph applies **zero** revisions
while git reports no conflict (Invariant #3, #2068). Both orders are written
out here so the rebaser needs nothing but this file:

**Order A — the metrics chain is already on `dev`** (rename #2927's files and
re-chain them onto `0067_metric_points`):

| file | `revision` | `down_revision` |
|---|---|---|
| `0068_public_user_memory_writes.py` | `0068_public_user_memory_writes` | `0067_metric_points` |
| `0069_agent_role_readiness.py` | `0069_agent_role_readiness` | `0068_public_user_memory_writes` |

**Order B — #2924/#2927 lands first** (re-chain the metrics revisions onto
`0066_public_user_memory_writes` / `0067_agent_role_readiness`):

| file | `revision` | `down_revision` |
|---|---|---|
| `0067_metric_definitions.py` | `0067_metric_definitions` | `0066_public_user_memory_writes` |
| `0068_metric_points.py` | `0068_metric_points` | `0067_metric_definitions` |
| `0069_agent_role_readiness.py` | `0069_agent_role_readiness` | `0068_metric_points` |

Either way: the SQLite entries in `db/migrations.py` are appended in **landing
order** (that runner keys by name, not by number, so they do not have to agree
with the Alembic numbering); `rm -rf src/backend/migrations/versions/__pycache__`
afterwards, or a stale `.pyc` keeps serving the old `down_revision`; then run
`scripts/ci/check_alembic_heads.py` and `scripts/ci/check_alembic_parity.py`
locally before pushing — the first must report exactly **one** head.

## Key Files

| Component | File | Purpose |
|-----------|------|---------|
| Registry | `src/backend/services/metric_registry.py`, `db/metric_definitions.py` | Declarations (ent#477) |
| Write | `src/backend/services/metric_points_service.py`, `db/metric_points.py` | `record_metrics` validation + store (ent#478) |
| **Read** | `src/backend/services/metric_read_service.py` | `freshness`, `read_agent_metrics`, `latest_by_metric`, `freshness_summary`, `bind_dashboard_widgets` (ent#479, ent#666) |
| **Join** | `src/backend/services/objective_join_service.py` | `gap`, `join_objectives`, `read_objective_files`, `read_objective_join` (ent#666) — the one objective ↔ metric join |
| Route | `src/backend/routers/agent_files.py` | `GET/POST .../metrics*`, `GET .../objectives` |
| Health | `src/backend/routers/monitoring.py`, `db_models.AgentHealthDetail` | The informational block |
| Compat | `src/backend/services/compatibility/static_checks.py` | D-009 (shape), D-010 (`metrics.json` superseded) |
| MCP | `src/mcp-server/src/tools/metrics.ts` | `record_metrics`, `get_metrics`, `get_objectives` |
| Frontend | `src/frontend/src/components/DeclaredMetricsTiles.vue` | The tiles |
| Frontend | `src/frontend/src/components/BoundMetricMark.vue` | A bound widget's point time / stale mark / binding error |
| Frontend | `src/frontend/src/utils/metricFormat.js` | Type- and direction-aware formatting, shared by both surfaces |
| Frontend | `src/frontend/src/utils/agentTabs.js` | `buildTabs({hasDashboardFlag, hasDeclaredMetrics})` |
| Store | `src/frontend/src/stores/agents.js` | `getAgentMetrics`, the two-flag `checkDashboardExists` |

---

## Dashboard Widget System (dashboard.yaml)

In addition to template-defined metrics, agents can create a `dashboard.yaml` file for richer, widget-based dashboards.

### Dashboard Flow

```
┌─────────────────────┐     ┌─────────────────────┐
│   Agent writes      │     │   User opens        │
│   dashboard.yaml    │     │   Dashboard tab     │
│   with widgets      │     │                     │
└─────────────────────┘     └─────────────────────┘
          │                           │
          ▼                           ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                       Agent Server (/api/dashboard)                          │
│   1. Read dashboard.yaml                                                     │
│   2. Validate widget types and required fields                               │
│   3. Return { has_dashboard, config, last_modified, error }                  │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                   Backend (/api/agent-dashboard/{name})                      │
│   1. Access control check                                                    │
│   2. Check agent is running                                                  │
│   3. Proxy to agent server                                                   │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                       Frontend (DashboardPanel.vue)                          │
│   1. Load dashboard when tab activated                                       │
│   2. Render sections with layout (grid/list)                                 │
│   3. Render widget types: metric, status, progress, text, markdown,          │
│      table, list, link, image, divider, spacer                               │
│   4. Auto-refresh based on config.refresh (default 30s, min 5s)              │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Widget Types

| Type | Required Fields | Description |
|------|-----------------|-------------|
| `metric` | label, value | Single numeric value with optional trend/unit |
| `status` | label, value, color | Colored status badge |
| `progress` | label, value | Progress bar (0-100) |
| `text` | content | Simple text with optional size/color/align |
| `markdown` | content | Rich text with markdown rendering |
| `table` | columns, rows | Tabular data |
| `list` | items | Bullet or numbered list |
| `link` | label, url | Clickable link or button |
| `image` | src, alt | Image display |
| `divider` | (none) | Horizontal separator |
| `spacer` | (none) | Vertical space (sm/md/lg) |

### Dashboard Schema

```yaml
# dashboard.yaml
title: "Agent Dashboard"
description: "Real-time status overview"
refresh: 30  # Auto-refresh interval in seconds (min 5)

sections:
  - title: "Key Metrics"
    layout: grid  # grid or list
    columns: 3    # 1-4 columns (for grid layout)
    widgets:
      - type: metric
        label: "Messages"
        value: 42
        unit: "total"
        trend: "up"
        trend_value: "+5"

      - type: status
        label: "Status"
        value: "Running"
        color: green

      - type: progress
        label: "Completion"
        value: 75
        color: blue

  - title: "Details"
    layout: list
    widgets:
      - type: markdown
        content: |
          ## Notes
          - Item 1
          - Item 2

      - type: table
        title: "Recent Activity"
        columns:
          - key: time
            label: "Time"
          - key: event
            label: "Event"
        rows:
          - time: "10:30"
            event: "Started"
          - time: "10:35"
            event: "Completed"
```

### Dashboard Key Files

| Component | File | Purpose |
|-----------|------|---------|
| Agent Server | `docker/base-image/agent_server/routers/dashboard.py:150-229` | GET /api/dashboard endpoint |
| Validation | `docker/base-image/agent_server/routers/dashboard.py:23-119` | Widget validation logic |
| Router | `src/backend/routers/agent_dashboard.py:19-43` | GET /api/agent-dashboard/{name} |
| Service | `src/backend/services/agent_service/dashboard.py` (107 lines) | Dashboard proxy logic |
| Frontend | `src/frontend/src/components/DashboardPanel.vue` (510 lines) | Dashboard display component |
| Frontend | `src/frontend/src/views/AgentDetail.vue:88-91` | Dashboard tab integration |
| Store | `src/frontend/src/stores/agents.js:516-522` | getAgentDashboard action |

---

## Test Agents with Metrics

All test agents have metrics defined:

1. **test-echo**: messages_echoed, total_words, total_characters, avg_message_length
2. **test-counter**: counter_value, increment_count, decrement_count, reset_count, total_operations
3. **test-delegator**: delegations_sent, delegations_succeeded, delegations_failed, success_rate, unique_agents_contacted
4. **test-scheduler**: scheduled_executions, manual_executions, last_execution_status, total_log_entries, uptime_seconds
5. **test-queue**: requests_processed, total_delay_seconds, avg_delay, queue_depth, quick_requests
6. **test-files**: files_created, files_deleted, total_bytes_written, current_file_count, directories_created
7. **test-error**: normal_responses, intentional_failures, timeouts, error_rate, last_error_type

## Future Enhancements

Shipped since this list was written: time-series history (ent#478's
`metric_points` + ent#479's bucketed series), showing declared metrics as the
default dashboard (ent#479), and the objective join (ent#666 — see above).
Still open:

1. **Pace, as opposed to position** — `gap.status` is where the number sits
   relative to the target; judging whether the agent is *late* against `by` is
   ent#605's ramp maths, on top of the `by` / `horizon` this read already carries
2. **Cross-agent and fleet reads** — the read is self-scoped by design; lifting
   that is a deliberate grant (ent#80, ent#94)
3. **A `metrics_updated` WebSocket trigger** — the refetch route now exists, so
   a thin coalesced trigger is possible (ent#538)
4. **Alerting** on a breached threshold or a stale metric
5. **Export**: Prometheus/OpenTelemetry

## Related Documents

- [Agent Template Spec](../../docs/AGENT_TEMPLATE_SPEC.md)
- [Agent Custom Metrics Spec](../../docs/AGENT_CUSTOM_METRICS_SPEC.md)
- [Requirements 9.9](requirements.md#99-agent-custom-metrics)

---

## Revision History

| Date | Changes |
|------|---------|
| 2025-12-10 | Initial documentation |
| 2025-12-30 | Verified file paths, service layer refactor |
| 2026-01-23 | Updated line numbers (info.py:148-208, agents.py:688-695, agents.js:507-522), added Dashboard Widget system documentation (dashboard.yaml), added DashboardPanel.vue (510 lines), added revision history |
| 2026-09-22 | Added the write path (ent#478): `record_metrics`, the `metric_points` store, the two Settings knobs and the retention sweep |
| 2026-09-22 | Added the objective join (ent#666): `GET .../objectives`, MCP `get_objectives`, `latest_by_metric`, the gap semantics (position not pace, the `hold` arm) and the #2927 rebase note |
| 2026-09-22 | ent#666 review fixes: findings scoped to the objectives returned, a declared `hold` on the wire as the registry's `neutral`, a 30 s fan-out budget + 5 s per-read timeout, `objectives_skipped` / `objective_id_invalid` findings, and both Alembic rebase orders written out above |
| 2026-09-22 | Rewrote the READ half (ent#479): the re-backed route, the one `2 x cadence` staleness rule, the declared-metric tiles, MCP `get_metrics`, the health block — and retired `metrics.json` as a source, replacing it with the D-010 finding |
