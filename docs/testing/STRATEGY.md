# Trinity Testing Strategy

> **One document.** How Trinity is tested: the method, the CI lanes as they are
> built, and the bar a harness must meet. Everything else in `docs/testing/` is a
> pointer target — the [invariant catalog](orchestration-invariant-catalog.md), the
> generated [journey list](JOURNEYS.md), the click-through
> [scenario phases](phases/INDEX.md) and the `ui-sweep/` reports they produce.
> Retired material lives under [`docs/archive/testing/`](../archive/README.md).
> The structural claims in this file are pinned by
> `tests/unit/test_2339_testing_docs_consolidated.py` (#2339, Rail R5 of the
> journey-coverage deck, epic #1850).

## Glossary — four words that all say "tier"

| Word | Where it is used | Meaning |
|---|---|---|
| **Tier A / Tier B** | the invariant catalog | *when* an invariant must hold: at every observable instant (A) or within a reconciliation window (B) |
| **journey tier** — `journey-smoke`, `live-stack`, `soak` | `tests/journeys/catalog.yaml` | the *cost class* of a journey, which decides what it gates: every PR, the release cut, or a long run |
| **runner tier** — `unit`, `integration`, `git_sync`, `security`, `scheduler_tests`, `agent_server`, `journeys`, plus the `api`, `standalone` and `postgres` invocations | `tests/run-full.sh` | one pytest invocation with its own verdict, timeout and skip audit |
| **lane** | this document | a CI workflow: what triggers it, what it runs, where those tests live |

## Method

Three kinds of test, and the suite only earns trust when all three are present.

1. **Promise tests — journeys.** A journey is a sentence a person can say about
   Trinity ("I can create an agent with no configuration and get a useful
   answer"), asserted end to end through the public API against a live stack.
   The list of promises is `tests/journeys/catalog.yaml`, the harnesses are
   `tests/journeys/`, and the generated `JOURNEYS.md` shows which promises have
   a harness and which are honestly `built: no`.
2. **Invariant tests — catalog and canary.** An invariant is a property of
   system *state* — no `running` execution older than its timeout plus the
   cleanup window (E-01), a harness that can prove its own collector is not
   blind (H-01) — written as a predicate a query can evaluate. Definitions live
   exactly once, in the catalog's `**X-NN**` entries; the canary in
   `src/backend/canary/` evaluates the live subset continuously, and journeys
   *cite* ids rather than restating them. The catalog's own
   [Framework](orchestration-invariant-catalog.md#framework) names five layers.
   What exists today is the catalog itself and the black-box canary;
   property-based testing covers pure functions (#1771, partial); the chaos
   and load layers are not built — the `soak` journey tier (J04) is where they
   would land.
3. **Regression tests.** The bulk of `tests/unit/` is ticket-named
   (`test_<issue>_*.py`): each defends a fix that already shipped. They are
   necessary, and they are the one kind that cannot say whether a promise still
   holds — which is why the first two exist.

Two rules keep the three honest:

- **Intent lives in git, state lives in CI.** The journey catalog never carries
  a pass/fail field; `JOURNEYS.md` is produced by
  `scripts/ci/generate_journeys_md.py` and byte-compared in CI; coverage is
  computed from JUnit artifacts at report time, never committed.
- **A skip is not a pass.** `tests/run-full.sh` fails on any skip whose reason
  is not allowlisted, and a journey may not skip at all (see the acceptance
  bar). Verification means running the command and showing the output — a
  "done" without evidence is not done (CLAUDE.md, Rules of Engagement).

Feature flows carry a `## Testing` section — a manual runbook for that flow
(most flows have one), created with the add-testing skill (`/add-testing`,
dev-methodology plugin). Read it as a runbook, not as coverage: its
`Last Tested` stamps are not maintained.

## Lanes — what runs, when, and from where

| Lane | Trigger | What it runs | Tests live in |
|---|---|---|---|
| `backend-unit-test.yml` | every PR to `dev`/`main`; pushes to both | `cd tests && pytest unit/` under three pytest-randomly seeds on the base tip and on the merge commit; the **regression diff** of failing ids is the signal, and a run that yields no JUnit fails closed | `tests/unit/` — the per-PR island (`pytest.ini` sets `norecursedirs = ..`) |
| `backend-unit-nightly.yml` | 06:00 UTC | the same three-seed diff over every open PR's merge commit; a sticky comment on a PR that introduces failures | `tests/unit/` |
| `journey-smoke.yml` | every PR to `dev` — deliberately no path filter, so a docs-only PR pays the boot too | boots the stack from the PR's own tree and runs the credential-free lifecycle journey; 30-minute budget, and a timeout is a failure, never a pass | `tests/journeys/` |
| `journey-impact.yml` | every PR to `dev`/`main` (open, edit, sync, reopen) | validates the `Journey Impact:` line on the PR and on any epic it references; `new:` without a skeleton in the diff fails, and so does a bare `none` | PR and epic templates |
| `integration-nightly.yml` | 06:30 UTC | a live-instance sweep, per open PR, of everything under `tests/` except `unit/` and `process_engine/` (`-m "not slow"`): the root-level API files and the `integration`, `git_sync`, `security`, `scheduler_tests`, `agent_server` and `journeys` directories | `tests/` root and its sibling directories |
| `frontend-build.yml` | PRs and pushes touching `src/frontend/**` | `npm run check:tokens`, `npm run test:unit` (Vitest, including the raw-colour and loading-gate ratchets), `npm run build` | `src/frontend/tests/unit/` |
| `frontend-e2e.yml` | nightly 07:00 UTC; PRs touching `src/frontend/**`; the `ui` label; dispatch | Playwright `@smoke` specs against a booted stack; `@visual` and `@interactive` never run in CI | `src/frontend/e2e/` |
| `schema-parity.yml` | every PR (self-skips when no schema file changed) | SQLite DDL ↔ migration parity, plus the cross-track Alembic and single-head guards | `tests/unit/test_schema_parity.py`, `scripts/ci/` |
| `backend-image-smoke.yml` | pushes and PRs touching the backend image inputs | builds the production backend image and boots it | — |
| canary loop | continuously inside the backend, one cycle per fleet | the live invariant subset (`POST /api/canary/run-cycle` evaluates one on demand); a green→red transition pages with that invariant's runbook | `src/backend/canary/`, joined to the catalog in its *Canary mapping* table |
| `/ui-sweep` — core-team skill, daily on the dev agent | Mon–Fri 04:00 UTC | one click-through lane per run — a scenario phase from `phases/`, an exploratory sweep of one UI area, edge probing, or a phase refresh — driven through a real browser; dated reports and the rotation cursor land in `ui-sweep/`, and reproduced defects become public bugs | `docs/testing/phases/`, `docs/testing/ui-sweep/` |

Locally, `tests/run-full.sh` runs every runner tier as its own invocation with a
verdict, a timeout and a skip audit. Mechanics, wall times and the rule for
where a new test goes are in [tests/README.md](../../tests/README.md#tiers);
the frontend commands and the fixture contract are in
[src/frontend/e2e/README.md](../../src/frontend/e2e/README.md).

**Which checks are required to merge is a branch-protection setting, not a
commit.** Read it rather than trusting a document —
`gh api repos/abilityai/trinity/rules/branches/dev` (rulesets), or the
branch-protection endpoint with admin rights. The intent on record: #2336 made
the journey smoke a required check and absorbed #1958, whose finding was that
`dev`'s required contexts ran no tests at all.

## Acceptance bar for a harness

A journey harness is accepted when each of these holds. The parenthesis names
what enforces it; "convention" means a reviewer does.

1. **It fails; it never skips.** A stack it cannot reach, an agent that never
   reaches `running`, a turn that never answers — each *is* the finding, and it
   is reported as a failure that names the URL or agent tried
   (`tests/journeys/conftest.py::journey_client`, `journey_agent`).
2. **Preconditions are checked once, loudly,** before any test runs, so a
   broken stack is one failure with a cause rather than forty confusing ones
   (the same fixture).
3. **It touches nothing it did not create.** Every agent is named
   `pytest-ephemeral-journey-<random>` and torn down by that name; teardown is
   idempotent, so a crashed run leaves the tier re-runnable
   (`journey_agent`, `delete_agent_idempotent`).
4. **Synchronisation is a deadline poll, never a sleep** —
   `poll_until(predicate, deadline_s=…, describe=…)`; a `sleep` used as
   synchronisation fails review.
5. **It has a runtime budget, and a timeout is a failure**
   (`run_tier journeys 900` in `tests/run-full.sh`; `timeout-minutes: 30` in
   `journey-smoke.yml`).
6. **It is declared before it is built.** The promise is a record in
   `tests/journeys/catalog.yaml` — `built: false` is legal and visible — and
   `harness:` may only name a file that exists
   (`tests/unit/test_2338_journey_catalog.py`).
7. **It cites invariant ids and never restates them;** every id must resolve to
   a catalog entry through the shared parser
   (`tests/unit/_invariant_catalog.py`, checked by test_2338 and test_2337).
8. **The PR that adds or extends a promise says so** on its `Journey Impact:`
   line; `new:` obliges a skeleton in the same PR, and a `strict=True` xfail
   asserting the journey's invariants is enough
   (`journey-impact.yml`, `.github/PULL_REQUEST_TEMPLATE.md`).
9. **Its tier is chosen by cost, not importance.** A journey that needs a fresh
   host is `live-stack` and gates the release cut; one a job can stand up is
   `journey-smoke` and gates every PR; a long run is `soak` (the catalog's
   `tier:` field, values validated by test_2338).
10. **Failure output names the broken promise** — "agent `x` was created but
    never reached `running` within 90 s" — never a bare status comparison
    (convention, from #2336).

## Pointers — what lives where

| You are looking for | It lives in |
|---|---|
| the invariant definitions and the live canary set | [orchestration-invariant-catalog.md](orchestration-invariant-catalog.md) — definitions are its `**X-NN**` entries; the *Canary mapping* table is the join to `src/backend/canary/` |
| the promise list, and which promises have a harness | [tests/journeys/catalog.yaml](../../tests/journeys/catalog.yaml) → generated [JOURNEYS.md](JOURNEYS.md) |
| how to run the backend suites: env vars, tier wall times, friction recovery | [tests/README.md](../../tests/README.md) |
| how to run and tag the Playwright specs, and the fixture-agent contract | [src/frontend/e2e/README.md](../../src/frontend/e2e/README.md) |
| what each test file covers | [tests/registry.json](../../tests/registry.json) — it indexes tests; the journey catalog indexes promises, and the two are linked, never merged |
| the click-through scenarios and their run reports | [phases/INDEX.md](phases/INDEX.md); `ui-sweep/` (created by the first sweep) |
| a feature's own test record | beside that feature's docs — e.g. [PULL_MIGRATION_TESTING.md](../planning/PULL_MIGRATION_TESTING.md) next to the pull-migration status |
| the manual runbook for one feature | that feature flow's `## Testing` section under `docs/memory/feature-flows/` |
| the charters | epic #1850 (CI health and journey coverage) and #1259 (execution correctness) |
| retired plans, reports and guides | [docs/archive/testing/](../archive/README.md), indexed with what superseded each |

## Where a new testing document goes

- **Guidance about how we test** goes *here*. If this file cannot hold it as a
  section, it is probably a feature flow or a README, not strategy.
- **A promise** goes in `tests/journeys/catalog.yaml`; **an invariant** goes in
  the catalog as a `**X-NN**` entry with a `Signal:`; **a test** goes where
  [tests/README.md](../../tests/README.md#where-does-a-new-test-go-1895) says.
- **A feature's test plan or record** sits beside that feature's documentation,
  not in `docs/testing/`.
- **A click-through scenario** is a phase under `phases/`; **a sweep report** is
  written by `/ui-sweep` into `ui-sweep/`.
- **Anything dated or superseded** moves to `docs/archive/testing/` with a row
  in [the archive index](../archive/README.md). Testing records are archived,
  never deleted (#2339); a planning document may be deleted once a successor
  states everything it did (#2657).

The guard (`tests/unit/test_2339_testing_docs_consolidated.py`) fails CI when
a new file appears at the top of `docs/testing/`, when a subdirectory other
than `phases/` or `ui-sweep/` appears, when a link here stops resolving, when
this file copies a run of lines from a README or the catalog, when it defines
an invariant, when a runner tier or a test-running workflow exists that this
file does not name, or when a live file still points at a retired path.

## Known gaps

- The sibling directories (`security`, `scheduler_tests`, `git_sync`,
  `agent_server`) run in the nightly sweep and locally, not per PR; the
  placement rule that keeps new unit-safe tests out of them is in
  [tests/README.md](../../tests/README.md#where-does-a-new-test-go-1895).
- Property-based coverage is partial (#1771); the chaos and load layers are
  unbuilt.
- How many promises have a green harness is a report-time number: read
  [JOURNEYS.md](JOURNEYS.md), never this file.
