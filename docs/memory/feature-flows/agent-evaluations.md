# Agent Evaluations — the referee surface (ent#206)

**Status**: shipped (foundation). Children 3–7 of the eval epic build on this surface.

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
| `quality` | `agent_evaluations.quality` | the platform / a future evaluator — **never the graded agent** | the answer was good |

`status='success'` had been rendered to users as "Success rate", which reads as
correctness when it only ever meant a clean process exit. That relabel is the
other half of this PR (below).

## Flow

```
Tier-0 evaluator (child 3, next)          admin / operator
        │ db.create_evaluation()                │ POST /api/agents/{name}/evaluations
        ▼                                        ▼
   ┌──────────────────────────────────────────────────┐
   │ routers/evaluations.py  — write: require_admin   │
   │                            + reject_agent_principal│
   └──────────────────────┬───────────────────────────┘
                          ▼
                 db/evaluations.py  (EvaluationOperations)
                          ▼
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

**Stored vs. read (ent#781).** The row keeps the full `evaluator`, person
included; what a reader is *served* depends on who is reading. All three GET
routes (`/api/agents/{name}/evaluations`, `/api/evaluations`,
`/api/evaluations/{id}`) and the POST echo project every row through
`routers/evaluations.py::_to_response`. A human owner or admin of the row's
agent (JWT or their own `user`-scoped key) reads the stored value. Every machine
principal and every human the agent is merely shared with reads
`services/evaluation_read_service.redact_evaluator(...)`: the exact names
`platform:tier0` / `platform:tier0:pair` unchanged, `platform:tier1:<model>` as
`platform:tier1`, any other `kind:rest` as `kind` (so `admin:<email>` reads
`admin`) when the kind is an identity-free lowercase token, and a value with no
kind or a malformed kind (an address before the colon, uppercase, whitespace) as
`unknown`. The canonical rule is
lifecycle-observability §46 FR-3. It covers the `evaluator` field only; `judge`
and `checks` are returned as submitted.

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

## What this does NOT include

- **The thing that populates `quality`.** Child 3 (Tier-0 deterministic
  evaluator) writes to this surface; until it lands, `quality` is written only by
  an admin. This PR is the surface, not the grader.
- Agent-owned case runner (child 4), Tier-1 judge (child 5, enterprise),
  replay/shadow (child 7 — blocked on #1084 fail-closed + #1408).

## Files

| Layer | File |
|-------|------|
| Router | `src/backend/routers/evaluations.py` |
| DB | `src/backend/db/evaluations.py` (`EvaluationOperations`), facade in `database.py` |
| Schema | `db/schema.py`, `db/tables.py`, `db/migrations.py`, `migrations/versions/0033_agent_evaluations.py` |
| Models | `models.py` (`EvaluationCreate`, `EvaluationResponse`) |
| Cascade | `db/agent_cleanup.py` (`AGENT_REFS`) |
| Frontend | `ExecutionsPanel.vue`, `OverviewPanel.vue`, `ScheduleAnalyticsCard.vue` (relabel only) |
| Tests | `tests/unit/test_206_agent_evaluations.py` |

## Testing

Automated (unit island, no live stack):

- `tests/unit/test_206_agent_evaluations.py` — the write fence, the table and the
  read scoping.
- `tests/unit/test_937_projection.py` — grader identity (ent#781): the issue's
  failing test adopted verbatim, driving the real admin POST route as an
  email-login admin and projecting the stored row for the graded agent's key, the
  system scope and a shared non-operator (each reads `admin`, never the address),
  plus the operator controls and the `redact_evaluator` table.
- `tests/unit/test_937_ent781_census.py` — an AST census of every statically named
  call to `create_agent_evaluation` / `upsert_workspace_rating` /
  `create_evaluation`; each site is driven through its real writer and read back
  by four machine readers. It covers those three APIs only, not a writer added
  under a new name, which the read projection still covers.
- `tests/unit/test_ent781_read_routes.py` — the evaluator output contract, a
  malformed kind (an address before the colon, uppercase, whitespace) reading
  `unknown`, human operators vs. machines carrying operator membership, a failed
  operator lookup, and all three GET routes over HTTP on the real router.

On the pre-fix router the ent#781 files fail 56 of 85 behavioural cases (each on
the disclosed address), and the malformed-kind cases fail 16 of 20 without the
kind-grammar check; with both, all 115 tests pass.
