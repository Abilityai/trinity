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

Every finished `schedule`- or `agent`-triggered run is graded against the
platform's own records — zero LLM calls, OSS core, always on. Contract:
[requirements §46](../requirements/lifecycle-observability.md) FR-7..FR-10a.

```
 main.py lifespan ── tier0_sweep_service  (every 60 s, every worker;
                     Redis lease `tier0:leader`, fail-OPEN)
        │ leader only
        ▼
 tier0_evaluator.sweep(limit=25)
   terminal runs, triggered_by ∈ {schedule, agent},
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
*Not evaluated* (manual and chat runs are not graded), never an error.

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

**Not built yet.** Re-grading a replayed run (D-3, ent#937); an MCP read tool
(ent#937).

## Tier-1 judge seam (ent#936, checkpoint A)

The sampled, model-backed judge is an enterprise module; OSS carries the seam
it writes through, in `db/evaluations.py` (contract: [requirements §46
addendum B](../requirements/lifecycle-observability.md)):

```
 tier1_candidates(limit, lookback_hours)   Tier-0 rows whose run has no Tier-1 row
        ▼
 claim_tier1_row(execution_id, model)      INSERT platform:tier1:<model> in state
   compare-and-set on the one-row index      `claimed`; loser → None (no call, no spend)
        ▼  (the judge's provider call — enterprise, not here)
 finish_tier1_row(eval_id, judge)          validate_judge_json → verdict or skip,
   only onto a row still `claimed`           final per run; Tier-0 row untouched
 reclaim_stale_tier1(stale_seconds)        releases a claim a dead worker left
```

A second row, never the Tier-0 row (a Tier-0 re-grade would clobber `quality`
in place). Reached from the `database.py` facade only; the write fence holds
(`tests/unit/test_935_write_fence.py`, ent#936 section). The agent-facing
read of a judge row is ent#937's. Tests: `tests/unit/test_936_tier1_writer.py`.

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
| DB | `src/backend/db/evaluations.py` (`EvaluationOperations`), facade in `database.py` |
| Tier-1 seam | `src/backend/db/evaluations.py` (`claim_tier1_row` / `finish_tier1_row` / `tier1_candidates` / `reclaim_stale_tier1` / `validate_judge_json`), ent#936 |
| Schema | `db/schema.py`, `db/tables.py`, `db/migrations.py`, `migrations/versions/0033_agent_evaluations.py` |
| Models | `models.py` (`EvaluationCreate`, `EvaluationResponse`) |
| Cascade | `db/agent_cleanup.py` (`AGENT_REFS`) |
| Frontend | `ExecutionsPanel.vue`, `OverviewPanel.vue`, `ScheduleAnalyticsCard.vue` (relabel only); `views/ExecutionDetail.vue` + `components/ExecutionChecks.vue` (Tier-0 card) |
| Tests | `tests/unit/test_206_agent_evaluations.py`; `tests/unit/test_935_*.py`, `tests/unit/test_ent935_*.py`; `src/frontend/tests/unit/executionChecks.mount.spec.js` |
