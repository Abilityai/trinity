# Agent Evaluations — the referee surface (ent#206)

**Status**: shipped (foundation) + the Tier-0 deterministic evaluator (ent#935, below). Later children of the eval epic build on this surface.

> A score is only trustworthy if the graded agent cannot write it.

That single rule is what this feature is. Everything below is the structural
enforcement of it.

## Why a new surface and not `agent_reports`

`agent_reports` (#918) already stores agent-published structured data and would
have been the cheap answer. It was rejected on the auth boundary: its create is
**self-gated** — an agent-scoped key POSTs to its own agent's reports, by design,
because a report is the agent's own output. That is exactly backwards for a
grade. A surface where the graded party writes the score is not a referee.

So the axes are kept apart:

| Axis | Where | Who writes | Means |
|------|-------|-----------|-------|
| `completion` | `schedule_executions.status` (mirrored into `agent_evaluations.completion`) | the platform | the run exited cleanly |
| `quality` | `agent_evaluations.quality` | an admin today / a future judge — **never the graded agent** (Tier-0 leaves it NULL) | the answer was good |

`status='success'` had been rendered to users as "Success rate", which reads as
correctness when it only ever meant a clean process exit. That relabel is the
other half of this PR (below).

## Flow

```
                                          admin / operator
                                                │ POST /api/agents/{name}/evaluations
                                                ▼
   ┌──────────────────────────────────────────────────┐
   │ routers/evaluations.py  — write: require_admin   │
   │                            + reject_agent_principal│
   └──────────────────────┬───────────────────────────┘
                          ▼
 Tier-0 sweep ──────▶ db/evaluations.py  (EvaluationOperations)
 (no route; see below)    ▼
                  table `agent_evaluations`
                          │
        ┌─────────────────┴──────────────────┐
        ▼                                     ▼
  agent reads its own              admin reads the fleet
  GET /api/agents/{name}/evaluations   GET /api/evaluations
```

## The write fence

Two independent gates on the write route, and the second is the load-bearing one:

- `require_admin` — role check.
- `reject_agent_principal` — an agent-scoped key resolves to its **owner** and
  therefore inherits the owner's role. On a default install the owner is admin,
  so `require_admin` **alone would let a graded agent write its own grade**
  (the trinity-ops-agent#232 trap). This is why the fence is two gates, not one.

Read is deliberately *not* fenced the same way: `AuthorizedAgentByName` lets an
agent read its own evaluations. Feedback is the point — an agent that can see it
scored badly can act on it. Read ≠ write.

There is no agent-writable route at all: the surface has exactly one write
endpoint and it is human-admin-only. A test asserts that no route allows an
agent principal to write, so a future endpoint can't quietly open one.

## Data layer

`agent_evaluations` (dual-track per Invariant #3: `db/schema.py` +
`db/migrations.py` for SQLite, Alembic `0033_agent_evaluations` for PostgreSQL;
DDL in `db/tables.py` for fresh builds):

| column | meaning |
|--------|---------|
| `id` | `eval_<hex>` |
| `agent_name` | graded agent (registered in `AGENT_REFS` → rename re-keys, purge cascades) |
| `execution_id` | the run graded, nullable — an evaluation may be about the agent, not one run |
| `archetype` | what "good" means here; per-archetype rubric (strategy §2) |
| `completion` | mirror of the clean-exit axis, nullable |
| `quality` | the graded axis, nullable — **null is normal**: a run can be evaluated for completion before any grader has scored it |
| `checks_json` | Tier-0 deterministic check results |
| `judge_json` | Tier-1 judge output (enterprise layer, child 5) |
| `evaluator` | who produced it (`platform:tier0`, `admin:<user>`, `workspace:<email>`, a judge id) |

`quality` being nullable is a contract, not laziness: the two axes are
independent, and a UI must render "not yet graded" rather than assuming 0.

## Completion relabel

Three surfaces rendered `success_rate` as "Success rate": Overview (#1107), the
schedules rollup (#1115), fleet stats (EXEC-022). All three now read
**"Completion"** with a tooltip — *finished without erroring, not answer
quality*.

**Additive by design**: the `success_rate` API field is unchanged, so every
existing client keeps working. Only the label moves. Renaming the field would
have been a breaking change for an honesty fix that needs no API surface.

## Open-core

OSS, per the strategy gate (trinity-enterprise#206 §10, merged): the table, the
write-fence auth boundary, the Tier-0 deterministic runner, the agent-owned case
runner, and this relabel are **edition-agnostic primitives** — the load-bearing
rule has to be enforceable in every edition, and a deterministic check makes no
external call. The managed grading experience (judge panels, calibration, rubric
management UI) is the paid layer, mirroring #668 exactly: the deterministic tier
is free, the AI/managed tier is not.

## Tier-0 deterministic evaluator (ent#935)

Every finished `schedule`- or `agent`-triggered run — and every `fan_out`,
`loop` or `reminder` run another agent sent (ent#940, below) — is graded against
the platform's own records — zero LLM calls, OSS core, always on. Contract:
[requirements §46](../requirements/lifecycle-observability.md) FR-7..FR-10a.

```
 main.py lifespan ── tier0_sweep_service  (every 60 s, every worker;
                     Redis lease `tier0:leader`, fail-OPEN)
        │ leader only
        ▼
 tier0_evaluator.sweep(limit=25)
   terminal runs, triggered_by ∈ {schedule, agent}, or ∈ {fan_out, loop,
   reminder} with source_agent_name set (tier0_eligible, ent#940),
   completed 30 s – 24 h ago, no platform:tier0 row yet  (oldest first)
        │ to_thread, ≤4 at once, 10 s bound each (timeout → all-skip row)
        ▼
 evaluate_execution(execution_id)      ← takes an id only: no principal, no route
   gate checks:   execution_exists · credential_echo · validate_001 ·
                  deliverable_landed · playbook_outputs
   signal checks: budget_vs_baseline   (listed, never counted)
   headline = {passed, gate_pass, gate_fail, gate_skip}   ← gate checks only (TD-7)
        ▼
 db.upsert_platform_evaluation   evaluator='platform:tier0',
   target_kind='execution', target_id=<execution_id>  → one row per run
   (ent#366 partial unique index), completion = status=='success', quality NULL
        ▼
 GET /api/agents/{name}/executions/{execution_id}/evaluation
   get_authorized_agent_by_name · 404 `not_evaluated`
        ▼
 ExecutionDetail.vue → ExecutionChecks.vue  "Checks" card
```

**What the page shows.** One headline from the gate checks: *Checks passed*
(no gate failed), *Checks failed* (any gate failed — listed by id, reason and
evidence), *No gate check applied* (every gate skipped). Signals sit under
"recorded, not counted" and are never worded or coloured as pass/fail — a
baseline drift is an observation, not a verdict. A run with no row reads
*Not evaluated* (manual and chat runs are not graded), never an error. The
card's explanatory copy names scheduled runs, agent-triggered runs and runs
another agent delegated (the ent#940 sourced `fan_out`/`loop`/`reminder` runs).

**Why a sweep, not a terminal hook.** The scheduler owns no terminal for
`agent` runs, and the backend's terminal fan-out misses the FAILED terminals the
scheduler writes itself; a leased sweep is the one shape that sees every run
and adds zero lines to any terminal writer.

**The fence, extended.** The evaluator and the sweep import no FastAPI or auth
type; no route can create, update or delete a `platform:tier0` row (the admin
write files under `admin:<user>`; a Workspace rating cannot address an
`execution` target). Tier-0 rows never enter a rating read.

**The rollup rate.** `db/schedules/analytics.py` joins each window's runs to
their `platform:tier0` row (inner join, so an ungraded run is *not evaluated*,
never failed) and adds two fields beside `success_rate` to all three analytics
payloads — `get_schedule_analytics`, every `get_agent_schedules_summary` row,
and `get_agent_analytics`:

```
 checks_passed_rate = passed / evaluated_count     (null when evaluated_count == 0)
   evaluated_count  = runs in window with a platform:tier0 row that has a
                      gate check graded (gate_pass + gate_fail > 0)
                      (a skip-only row is on neither side; workspace:/
                      operator: rows never match)
   passed           = graded rows whose headline passed (gate_fail == 0)
```

`ScheduleAnalyticsCard.vue` renders a "Checks passed" tile beside Completion.
`OverviewPanel.vue` renders the agent's rate beside the completion headline and
a "checks" column on each "Schedules performance" row. Each reads "—" at zero
evaluated, never 0%. Tests: `tests/unit/test_935_checks_passed_rate.py`,
`src/frontend/tests/unit/checksPassedTile.spec.js`.

**The baseline series.** `budget_vs_baseline` compares a run with the same
series' last ≤20 successful runs (`recent_series_metrics`): a schedule run with
its schedule; a run another agent sent (any trigger but `schedule`, `agent`
included) with `(agent_name, triggered_by, source_agent_name, playbook)` — so a
`chat_with_agent` baseline is per caller (ent#940); an unsourced run with the
agent plus the playbook (ent#935).

**Not built yet.** Re-grading a replayed run (D-3, ent#937); an MCP read tool
(ent#937).

## Read API — aggregates, failures, trend, chain (ent#937)

Five GET routes, declared before `/evaluations/{eval_id}` (Invariant #4), all over
`agent_evaluations` INNER JOINed to `schedule_executions`:

| Route | Answers |
|-------|---------|
| `GET /api/evaluations/aggregate?by=agent\|schedule\|pair` | Tier-0-graded runs completed in the window, folded per group: `evaluated`, `completed`, `completion_rate`, `graded`, `passed`, `checks_passed_rate` (passed ÷ graded), `per_check_fail`, `judged`, plus `quality_mean` / `judge_human_agreement` / `agreement_n` for a person |
| `GET /api/evaluations/failed` | Runs that did not complete or failed a gate check, newest first, paged (`limit` ≤ 200, `offset`) — check ids and reason codes only, never evidence text |
| `GET /api/evaluations/trend?metric=eval.*` | The stored daily `eval.*` series for one agent (`agent=`), one pair (`caller=` + `callee=`), or — naming neither — the fleet (`dims={"scope":"fleet"}`, served to an admin person and the system key only; every other reader gets `[]`, ent#938), ≤ 92 days — read from the points, never recomputed. A half-named pair, or `agent=` beside a pair, is 422 `invalid_series` |
| `GET /api/evaluations/chain/{execution_id}` | Ancestors (nearest first) and descendants (breadth first), bounded by `inter_agent_max_chain_depth` and 200 nodes; a walk stops at the first node outside the reader's scope and says `access`, never naming it |
| `GET /api/agents/{name}/evaluations/summary` | One agent's fold plus up to 50 failed runs — what the MCP tool serves |

`period ∈ {day, week, custom}`; `custom` needs `since` + `until`, at most 92 days
apart (422 `invalid_period`). The window fetch is capped at 5,000 rows with an honest
`truncated` flag. Rates are `null`, not 0, when nothing was counted. `caller=` /
`callee=` also filter the two existing list reads.

**Who reads what** (`evaluation_read_service.scope_for`, one helper for every route):
a person reads every agent they can access; an **agent key is narrowed to itself** —
its own rows and the pairs it is caller or callee of — never its owner's other agents,
on the new routes *and* the existing `GET /evaluations`, `GET /evaluations/{id}`,
`GET /agents/{name}/evaluations` and the execution read. An out-of-scope agent reads as
zeros (the same body as "no runs"), so there is no existence oracle. A **machine
principal never receives a judge's score**: `quality_mean`, `judge_human_agreement`
and `agreement_n` are absent from its copy (absent by rule, not null-as-ungraded), and
the trend refuses `eval.quality_mean` / `eval.judge_human_agreement` for it. The
evaluator column is projected through an allowlist (`platform:tier0`,
`platform:tier1:*` pass; `kind:rest` → `kind`; no colon → `unknown`, ent#781). A
`role=user` key gets 403 `workspace_only` (no route is `@workspace_route`).

**Scorecard points.** `services/eval_scorecard_service.py` writes daily points under
the reserved owner `platform:eval`, graded agent (or caller/callee pair) in `dims` —
four rates (`eval.checks_passed_rate`, `eval.completion_rate`, `eval.quality_mean`,
`eval.judge_human_agreement`) and five counts (`eval.evaluated`, `eval.completed`,
`eval.graded`, `eval.passed`, `eval.judged`) so a multi-day window rebuilds every
rate exactly. Rolled up on each Tier-0 row and nightly (first sweep tick after
00:10 UTC, same lease); identity `(metric, ts, dims)` makes a second write a
correction. `platform:eval` is invisible to every agent `get_metrics` read, and
`record_metrics` refuses the `eval.` prefix for every principal
(`eval_namespace_reserved`) — a graded agent cannot write its own scorecard.

**Retention.** Aggregates age out with execution rows by construction (the INNER
JOIN drops an evaluation whose execution row is gone; orphan eval rows are not pruned
yet — deferred). Scorecard points persist for the metric-point retention window
(`metrics_retention_days`, default 365 d).

**MCP.** `get_evaluation_summary` (`src/mcp-server/src/tools/evaluations.ts`) proxies
the summary route; `agent_name` defaults to the calling agent, the access row is
`enforce` on `agent_name`, and the projection is the backend's — the proxy never
decides what an agent may see.

## Operations › Evaluations tab (ent#938)

**Component tree.** `views/Operations.vue` adds tab id `evaluations`. Gated tabs are
data — `TAB_GATES` maps tab id → predicate (`evaluations: () => atOperatorFloor`,
ent#837's floor: operator, creator, admin); the strip, the render slot, `resolveTab`
(a gated deep link bounces to `needs-response`) and a late-role watch (re-selects the
tab once `/api/users/me` lands, bounces on a demotion) all read it. The slot renders
`components/EvaluationsPanel.vue` → `components/evaluations/{EvalKpiRow, EvalAgentTable,
EvalFailed, EvalPairs, EvalTrend}.vue` + `format.js`, over one domain store,
`stores/evaluations.js` (Invariants #6/#7: one store, the single API client).

**Request set, as data** (`EVALUATION_URLS`, pinned by the store spec):

| Read | Request |
|------|---------|
| KPIs + per-agent table | `GET /api/evaluations/aggregate?by=agent` (+ `agent=` when one is picked), and the previous window of the same length for the deltas (a failed previous read drops the deltas to "—", never the numbers) |
| Delegated pairs | `GET /api/evaluations/aggregate?by=pair` (narrowed client-side to the picked agent) |
| Failed runs | `GET /api/evaluations/failed`, `limit=50`, offset paging ("Load more" hidden at `total`); `by=pair&caller&callee` when a pair is picked |
| Trend | `GET /api/evaluations/trend?metric=eval.checks_passed_rate&days=N` — no `agent=` reads the fleet series. `N` runs back from now to the window's first day (≤ 92), so a custom window that ended in the past plots its own days; a window opening before the 92-day reach says so |

Period mapping: 7 d = `period=week`; 30 d and custom = `period=custom&since&until`
(ISO-Z; a custom range is inclusive of both picked calendar days). The API validates
the window (≤ 92 d, 422 `invalid_period` — the data stays and the API's message shows).
Every loader stamps its request and drops an answer a later request overtook, so two
quick picker changes never leave the earlier window under the later picker.

**The guard is the API's.** `get_current_user` refuses a Workspace-only person (403
`workspace_only`) on every read the tab issues; an agent key reads its own rows; a
machine principal gets no judge keys. The UI gate only mirrors it.
`tests/unit/test_938_tab_guard.py` drives the store's request set through the router
under admin, operator, `user` and agent-key principals.

**Fleet series.** Written only by the nightly pass's unscoped read, and only for
completed UTC days — never today, which is still filling — never by the per-row restate
or the over-cap per-agent retry (each sees one agent; an over-cap day is a gap). Dims
`{"scope":"fleet"}`. Read by an admin person and the system key (counts only); every
other reader — an operator or creator scoped to fewer agents, an agent key, a connector
or any other machine key, even one owned by an admin — gets `[]`. The empty state says
which: an admin is told no fleet point is written yet (first nightly pass pending); an
operator or creator is told the fleet series is admin-only and to pick an agent. The
caption states the one-day lag and the day the series starts. Pinned by
`tests/unit/test_938_fleet_series.py`.

**Honest states.** The panel runs the `viewState` triad (loading = no data yet; a
failed refresh keeps the data with a stale banner; a failed first load is
`LoadFailed`); the failed, pairs and trend blocks each carry their own error state, so
one failed read never blanks the KPIs. The trend has one slot per UTC day of the
window and a day with no point is a gap, never 0 %; switching agents drops the previous
agent's line rather than showing it as this one's. `?agent=` deep-links both ways
(other query keys kept). The empty state links to the Executions tab.

**Deliberately absent** (waiting on the placement ruling): the C4/C5 blocks, a
judged-quality column, and the agreement tile with its T4 floor — the store reads no
`quality_mean` / `judge_human_agreement` / `agreement_n`.

## Pair evaluation — the caller→callee edge (ent#940)

Tier-0 grades a run; the pair evaluator grades the **delegation** that produced
it — one more row per delegated callee run, deterministic, zero LLM calls.
Contract: [requirements §46](../requirements/lifecycle-observability.md)
addendum C, FR-18..FR-21. Layer 1 only: the sampled pair judge (Layer 2, three
dimensions on the Tier-1 row) waits on ent#936 and is **not built**; nor is the
enterprise gitlink that lands it.

```
 tier0_sweep_service.run_cycle        (leader only — same tick, lease, bounds)
   phase 1  tier0_evaluator.sweep      → platform:tier0 rows
   phase 2  tier0_pair_evaluator.sweep(limit=25)
        ▼
 db.unevaluated_pair_execution_ids()
   Tier-0 rows naming a caller, no pair row yet (≤24 h) — plus pair rows left
   `skip: caller_running` whose caller has since ended, or `skip: caller_unreadable`
   (re-graded in place); fresh candidates sort before these re-grades
        │ to_thread, ≤4 at once, 10 s bound (timeout → all-skip row, insert-only)
        ▼
 evaluate_pair(callee_execution_id)        ← id only: no principal, no route
   ctx: callee · caller (parent_execution_id) · siblings (same parent, ≤200) ·
        caller agent_collaboration rows incl. action='read_result' ·
        callee Tier-0 headline · receipts · peer-grant refusals · rework series
   gate:   dispatch_exists · one_sided_record · relay_unverified ·
           relayed_failed_work · duplicate_dispatch · resend_after_receipt ·
           wrong_route
   signal: failed_with_side_effects · rework_bounds
   metrics: latency {dispatch→start, start→end, end→first read} · cost · verified
        ▼
 db.upsert_platform_evaluation(evaluator='platform:tier0:pair',
   target_kind='execution', target_id=<callee run>)   one row, beside Tier-0
        ▼
 evaluation_aggregates.window_rows   LEFT JOIN pair row → pair_checks
 evaluation_read_service.fold        Tier-0 counters unchanged + `pair` block
   (by=pair only) · edge failures in `failed` on by=pair only · agent
   summary: its edge failures as caller (`pair_failed`), never in the callee's `failed`
 eval_scorecard_service              eval.pair_{graded,passed,checks_passed_rate}
                                     on pair series only
```

**Where the evidence comes from.** Structured records first: the caller's own
`agent_collaboration` rows (written by `chat_with_agent`) and the callee rows
whose `parent_execution_id` is the caller run. The caller's reply is a second
source — an execution id it names that exists nowhere fails `dispatch_exists`;
a fan-out id never fails, as there is no per-target lookup. Fan-out, loop and
reminder children carry their record on the row (`fan_out_id`, `loop_id`), so
`one_sided_record` skips them as `no_activity_channel`.

**The read receipt (TD-1).** `relay_unverified` needs to know the caller read
the result before relaying it, so a read now leaves a record.
`activity_service.record_first_reads` runs on `GET
/api/agents/{name}/executions/{id}` (`routers/schedules.py`, MCP
`get_execution_result`) and `GET /api/agents/{name}/fan-out/{id}`
(`routers/fan_out.py`, MCP `get_fan_out_result`, one per finished child):

```
 reader = current_user.agent_name   (set for scope=agent keys only; never a header)
 reader is None or reader == target agent  → nothing
 run not terminal (success/failed/cancelled) → nothing — a poll of a running
                                               run writes no receipt
 else db.insert_read_receipt_if_absent(reader, target, run)
   agent_activities(agent_name=reader, type=agent_collaboration,
     related_execution_id=run, details.action="read_result")
   id = uuid5(reader, run), INSERT … ON CONFLICT DO NOTHING
     → first read only; repeat and racing reads leave one row
 any exception → swallowed (fail-open); response status and body unchanged;
 no broadcast
```

A caller run that ended before the install held any receipt reads `skip:
no_readback_record`, so history from before the receipt existed is never failed.

**Delegated triggers (TD-2).** A fan-out child had no Tier-0 row, so it could
have no pair row. `db.evaluations.tier0_eligible` — the one rule the candidate
query and `evaluate_execution` share — now admits `fan_out`, `loop` and
`reminder` when `source_agent_name` is set; unsourced ones and `user` stay
ungraded. Their `budget_vs_baseline` series is keyed per sender (above).

**Duplicates (TD-3).** Same playbook series key **and** token-set Jaccard ≥
`DUPLICATE_JACCARD` (0.9, a module constant — a threshold an operator could move
would be a Goodhart knob). Exact text match is recorded as `method: exact`.
Loop iterations and distinct fan-out tasks are excluded by construction; a
retry chain (`retry_of_execution_id`) is one dispatch.

**Who sees what.** By `by=pair` a person reads a pair only when both sides are
visible; an agent key reads the pairs it is caller or callee of. A machine
principal gets the pair block's counts only (`PAIR_MACHINE_KEYS`) — check ids
and reason codes, never evidence, latency or cost, never quality. `by=agent`
carries no pair rates (TD-6): an agent sees what to fix, not a pair score — and
only its own: a callee's `failed` list never carries the caller's edge failure.

**Tests.** `tests/unit/test_940_pair_checks.py`, `test_940_pair_checks_part2.py`,
`test_940_pair_metrics.py`, `test_940_pair_sweep.py`, `test_940_pair_read.py`,
`test_940_read_receipt.py`, `test_940_trigger_widening.py`,
`test_940_facade.py`; the writer fence in `test_935_write_fence.py`.

## What this does NOT include

- **The thing that populates `quality`.** Tier-0 grades what the run left
  behind and leaves `quality` NULL; it is written only by an admin until a judge
  lands.
- Agent-owned case runner (child 4), Tier-1 judge (child 5, enterprise),
  replay/shadow (child 7 — blocked on #1084 fail-closed + #1408).

## Files

| Layer | File |
|-------|------|
| Router | `src/backend/routers/evaluations.py` (incl. the Tier-0 read route) |
| Tier-0 | `src/backend/services/tier0_evaluator.py`, `src/backend/services/tier0_sweep_service.py` |
| Pair (ent#940) | `src/backend/services/tier0_pair_evaluator.py`; read receipt in `src/backend/services/activity_service.py` (`record_first_reads`), hooked in `routers/schedules.py` + `routers/fan_out.py`; `db/activities.py`, `db/schedules/executions.py` (`sibling_executions`, `pair_rework_series`) |
| DB | `src/backend/db/evaluations.py` (`EvaluationOperations`), facade in `database.py` |
| Read (ent#937) | `src/backend/db/evaluation_aggregates.py`, `src/backend/services/evaluation_read_service.py`, `src/backend/services/eval_scorecard_service.py` |
| MCP | `src/mcp-server/src/tools/evaluations.ts` (`get_evaluation_summary`) |
| Schema | `db/schema.py`, `db/tables.py`, `db/migrations.py`, `migrations/versions/0033_agent_evaluations.py` |
| Models | `models.py` (`EvaluationCreate`, `EvaluationResponse`) |
| Cascade | `db/agent_cleanup.py` (`AGENT_REFS`) |
| Frontend | `ExecutionsPanel.vue`, `OverviewPanel.vue`, `ScheduleAnalyticsCard.vue` (relabel only); `views/ExecutionDetail.vue` + `components/ExecutionChecks.vue` (Tier-0 card) |
| Frontend (ent#938) | `views/Operations.vue` (tab + `TAB_GATES`), `components/EvaluationsPanel.vue`, `components/evaluations/*`, `stores/evaluations.js`, `utils/tokenColor.js` |
| Tests | `tests/unit/test_938_tab_guard.py`, `tests/unit/test_938_fleet_series.py`; `src/frontend/tests/unit/{evaluationsStore.spec.js, evaluationsPanel.mount.spec.js, operationsEvaluationsTab.mount.spec.js}`; `tests/unit/test_206_agent_evaluations.py`; `tests/unit/test_935_*.py`, `tests/unit/test_ent935_*.py`; `tests/unit/test_937_*.py`; `tests/unit/test_940_*.py`; `src/mcp-server/src/tools/evaluations.test.ts`; `src/frontend/tests/unit/executionChecks.mount.spec.js` |
