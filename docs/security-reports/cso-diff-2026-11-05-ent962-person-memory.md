# /cso --diff — trinity-enterprise#962 the owner's view of a person's memory

**Date:** 2026-11-05. **Mode:** diff, daily gate (8/10). **Scope:** both stacked PRs against `origin/dev` `07bb2166a`.
- **Routes:** `routers/person_memory.py`, 4 routes.
- **Logic:** `services/person_memory_service.py`; `db/public_links.py` (summary history with a compare-and-set, the row lock, undo per section, the people union); `client_portal/agent_page.py` + `router.py` (the person's view, paged, notes-only).
- **Migration:** `public_user_memory_writes.section`, SQLite + Alembic `0120`.
- **Summarizer:** the one line in `platform_prompt_service.py`; the `pull_pilot.PULL_UNPULLED_TRIGGERS` entry.
- **Frontend:** Agent Settings → Person memory, the person modal, Workspace diff rows.

## Architecture delta

The diff adds four owner-tier HTTP routes and nothing else: no WebSocket channel, Docker permission, MCP tool or webhook.
- **Gate:** every route takes `OwnedAgentByName` (owner-or-admin via `can_user_share_agent`, the uniform 404) plus `dependencies=[Depends(require_person)]`. So an agent, system or connector key gets 403 on the principal alone, before any lookup of the agent (#186).
- **Read surface:** another person's memory text (notes, summary, write history), the channel identity an access request captured (ent#737), and, through the existing MEM-002 owner routes, that person's facts.
- **Write surface:** undo of one history row, through the same primitive the person uses; `undone_by` records the operator.
- **The person's view:** it learns only that someone else undid a write (`undone_by_operator`), never who.

## Findings

None at the confidence gate. 0 SUPPORTED.

## Controls challenged

| Control | Evidence |
|---|---|
| Readers rule (ent#638): owner and admin, nobody else | `test_ent962_person_memory_owner_view.py::TestReadersRule`, through the real dependency chain: owner and admin get 200; a shared user and a provider-named approver get 404 on all four routes; agent, system and connector keys get 403 for this agent AND a nonexistent one (no existence differential) |
| Human-only before ownership (#2996, ent#629) | `require_person` is a decorator dependency, so it runs before `OwnedAgentByName`. Dropping it from one route turns `test_machine_principals_are_refused_before_any_lookup` red (mutation M1). The census and runtime census pass: 996 guard tests across 293 / 1310 / 186 / ent629 / 2094 / human-only / 2996 / ent926 / ent435 |
| Cross-person and cross-agent undo | The primitive matches on `(agent_name, user_email, id)`, and the agent comes from the path dependency. Another person's write id is `not_found`, as is a person with no memory (`test_refusals_are_named_on_the_owner_route`). The portal undo is pinned to `section='notes'`, so a summary write id is a 404 there (mutation M4) |
| Operator identity never reaches the person | `agent_page.memory` projects a bool and drops `undone_by` (`test_the_person_sees_an_operator_undo_without_who` asserts the key is absent). The person's line is one string for owner and admin |
| Lost update on the shared blob | `_read_blob_for_update`: `FOR UPDATE` on PostgreSQL, the RESERVED-lock no-op write on SQLite (the `db/skill_sets.py` precedent), on the notes write, the summary write and undo |
| An undo cannot be overwritten by a stale summary | The summary write is a compare-and-set on `expected_previous`. `test_an_undo_during_the_model_call_survives_the_summarizer` drives the real `summarize_user_memory_background` with the undo performed inside the stubbed model call; reverting the one line turns it red (M2) |
| Reads never create a row (learnings 2026-08-05) | `get_public_user_memory` is a pure SELECT; `test_an_owner_read_never_creates_a_memory_row` |
| SQL | SQLAlchemy Core with bound values throughout, including the `UNION ALL` people query, the keyset cursor compare and the `notin_` on the empty blob; no string-built SQL |
| Input bounds | `user_email`: 1–320 chars and membership checked (404, not a regex 422); cursor ≤200 chars, decoded to `(written_at, id)` (422 `invalid_cursor`); `limit`/`offset` bounded with `Query(ge, le)`; undo rate-limited per operator (30/60 s) |
| Attacker-authored text shown to a human (ASI09, LLM05) | No `v-html` in the diff (0 added). Notes, summary, facts, journal and diff lines are interpolated text. `personMemory.mount.spec.js` asserts a `<b>` in notes renders as text (mutation F3). Channel name and handle are labelled "as the channel reported it" |
| Destructive verbs confirmed | Operator Undo and Forget both sit behind `ConfirmDialog`, Cancel focused (mutations F2, F5) |
| Audit | `person_memory_undo` records agent, person and write id, no text; refusals are logged with the code only |
| Census of execution triggers | `triggered_by='summarizer'` labels a history row, never an execution. It is listed in `PULL_UNPULLED_TRIGGERS` with that reason, and `test_4105_pull_trigger_census` is green |
| Public-repo disclosure (ent#45) | The docs-guard pattern over the 133 added public doc lines: 0 hits |
| Secrets and concealed Unicode | 0 credential-shaped strings and 0 tag-block / zero-width / bidi characters in the added lines |

## Hypotheses

- **A user-scoped key reads everyone.** It is the owner's own MCP key, often handed to their tooling, and it passes `require_person`, so it can read every person's memory text on the owner's agents and undo writes. This is deliberate parity with the MEM-002 facts routes, which admit the same principal; it is pinned by `test_the_owners_user_scoped_key_reads_as_on_the_facts_routes`. Tightening to `require_interactive` would be a product decision covering both route families.
- **History is kept indefinitely.** Every summary change is now a history row holding the summary text before and after, and rows live until the agent is deleted. This is data minimisation, not a boundary. The retention bound is a filed follow-up.
- **An undone summary can come back.** The summarizer rebuilds from the latest 20 messages, so an undone poisoned summary can be re-derived while that conversation is recent. This is a stated limit in MEM-001, and the modal says so.

## Coverage gaps

- **No live PostgreSQL here.** The `FOR UPDATE` branch of `_read_blob_for_update` runs on no test; every test is SQLite, and `TEST_POSTGRES_URL` is unset. The CI `pg-migrations` job runs the migration only.
- **Phases with no diff overlap were skipped:**
  - 3: no dependency changes (`package.json`, lockfiles and the image Dockerfiles are untouched);
  - 4: no workflow changes;
  - 5: no Docker or compose changes;
  - 6: no webhook or integration code;
  - 8: beyond the Unicode scan, no shipped skill, template or hook changed.
- **Evidence base:** unit islands (24 backend tests plus the 1,596-test neighbourhood; 101 frontend specs), `/verify-local --skip-agent --skip-unit` (image build, `import main`, boot on a fresh DB, test-preflight, integration 74 passed), and the operator's eyeball on the local instance. No agent was exercised (no agent-runtime change).
