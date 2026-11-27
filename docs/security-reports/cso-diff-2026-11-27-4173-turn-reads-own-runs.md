# /cso --diff — feature/4173-turn-own-runs-read (abilityai/trinity#4173)

Date 2026-11-27 · cso 1.2.3 · daily gate · diff against `dev` at `ea04de240` · not a full run.

Scope: the by-id execution read for the turn an agent's key acts in. Two functions in `src/backend/dependencies.py` (`acting_turn_reach`, `row_is_acting_turns`), their call at four readers (execution detail, `/log`, `/stream`, the fan-out batch read), one more arm in the fan-out SQL filter, and the MCP wording. No new route, no schema change, no dependency, image, compose or workflow change.

## What the change opens, by decision

These are the operator's ruling (2026-11-23, with the choices recorded on 2026-11-27), not findings:

- An agent's key acting in a running turn for a shared person or a visitor reads by id that turn's own row and the runs it started: result, full log and live stream. This includes a run on an agent the turn's person cannot open, so such a turn can read that run's transcript.
- Between two share-tier turns running on one agent, the turn id is the only bound. A turn's MCP calls carry its own id. Naming a sibling turn takes a raw request with the agent's key, which the shell gives (the owner's turn, or a visitor's while `visitor_shell` is on). Such a process can read the sibling turn's row and runs, and through that turn's running wake the runs of a sibling turn that has ended. Before this change a read made while two different people's turns ran answered no rows at all. The stricter rule (accept the id only when its person is the one the ledger narrows the key to) was considered and not taken: it brings the 404 back whenever two people's turns overlap.

## Findings

Nothing open. Three low findings came out of the independent pre-commit read and are fixed on the branch.

| # | Sev | Conf | Category | Finding | State |
|---|---|---|---|---|---|
| 1 | LOW | 6/10 | fail-closed-default-as-evidence | A continuation is rated `share` when the turn it continues cannot be read, and the reach accepted that rating. A wake of the owner's turn that failed closed at that moment would have opened its own row and the owner turn's runs (`src/backend/dependencies.py`) | fixed on branch |
| 2 | LOW | 9/10 | guard-untested | The check that a reach with no link never matches a row with no link (`None == None`) was not executed by any test (`src/backend/dependencies.py`) | fixed on branch |
| 3 | LOW | 8/10 | guard-untested | The fan-out SQL arm's check that a missing caller never renders `source_agent_name IS NULL` was not executed by any test (`src/backend/db/schedules/executions.py`) | fixed on branch |

Fix for 1: a continuation opens something only when the turn it names is read and itself rates `share`; one whose turn cannot be read opens nothing, not even its own row. Each fix has a test, and removing the fixed line turns that test red.

## Checked and clean

- **Who reaches the exception:** only an agent-scoped key. The turn id is recorded for agent keys alone; a person, a user, system, ops or connector key carries no agent name; an ephemeral agent key is fenced off these routes at the auth entry point.
- **What the id must be:** a row of the key's own agent, RUNNING, rated `share`. An owner turn's id, a finished, unknown or foreign id, `manual` and no header open nothing. The lookups that decide it are the ones the dispatch link is written by.
- **Which rows:** the acting turn's own, or a row carrying both the key's agent as dispatcher and the turn's link. Both columns are written by the platform from the authenticated principal; no request body or header value reaches them unvouched.
- **Enumeration (Invariant #8):** the reach is decided before the target row is read and every lookup in it keys on the header. A row outside the rule, a near-miss (the link without the dispatcher) and an id that never existed answer the same status, body, headers and query count on detail and `/log`; the stream reader answers the same 404 for a hidden row and a missing id.
- **The narrowed-read marker:** unchanged by a grant. A granted and a refused read of the same request shape carry the same code.
- **Readers that must not change:** the lists, the running list, capacity, queue status and `terminate` are not in the diff, and a test scan fails any caller of the two functions beyond the four readers (`dependencies.py` included).
- **Failure direction:** stated in the helper. Any lookup error returns no reach; the helper returns before any lookup for the full view and for a principal that is not an agent key, so it adds no 500 path.
- **Injection:** the fan-out arm is bound parameters; it is applied only to an already narrowed read and only with a non-empty caller and link.
- **Logging:** one info line per granted read (agent, turn id, row id, row's agent). No credential, no message text.
- **Secrets, hidden characters, enterprise-docs guard:** no hit in added lines or changed files.
- **Mutation proof:** 23 single-line breaks, each at a line the diff adds, each turned a named test red; files restored from copies.

## Coverage gaps

- Guards that import the assembled app (the runtime route census, the ops-write sweep, the human-only refusal audit) could not run on the audit host: a Python package is missing there, at the merge-base too. The diff adds no route. CI runs them.
- One MCP test file (`tool-visibility.test.ts`) fails on the audit host with or without this change (same counts with the two changed MCP files restored to the merge-base). The other MCP test files pass, including the 2,048-character description cap.
- An optional private module is not mounted in this checkout; any reader of executions it adds was not read.
- No dependency scanner ran, because no dependency changed.
