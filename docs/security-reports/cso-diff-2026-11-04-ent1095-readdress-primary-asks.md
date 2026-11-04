# /cso --diff — trinity-enterprise#1095 re-address pending asks on a primary change

**Date:** 2026-11-04. **Mode:** diff, daily gate (8/10). **Scope:** `services/ask_service.py` (`readdress_item`, `readdress_role`), `db/operator_queue.py` (`read_address`, `list_pending_role_addressed`, `readdress`), `database.py` facade, the private assignments router trigger and projects proposals pass, tests, docs.

## Architecture delta

No new endpoint, WebSocket channel, Docker permission or MCP tool. The only callers of the new primitives are the admin-and-interactive assignment writes in the private module (`require_admin` + `reject_non_interactive_principal`), so an agent key, a connector key or `trinity-system` cannot reach them. An agent still cannot change who answers its own ask.

## Findings

None at the confidence gate. 0 SUPPORTED.

## Controls challenged

| Control | Evidence |
|---|---|
| Trigger is a grant, human-only | `assignments/router.py` writes keep `require_assignment_admin`; the new call sits after the write, inside the handler; `test_883`'s positive table and `test_1095` (19 tests) pass |
| Agent cannot re-address its own ask | `readdress_role` / `readdress_item` have no route and are not MCP-exposed; an agent raise still passes through `_address` once |
| Seat fence (ent#1053) not loosened | `_fresh_audience` returns None on a refused `approver`/`viewer`; `test_an_unfilled_approver_leaves_the_seat_fenced_row_as_it_was` asserts `may_end(stranger)` stays False |
| One reader for a credential ask (ent#1066) | credential rows move only to a single person; test covers 0, 1 and 2 fresh readers |
| Named-addressee rows never move | DB allowlist (`raised_by='agent'` or `gate-` + 40 hex approval); `gate-note-*` and `gate-project-agent-*` rows untouched in the test |
| CAS against a concurrent answer / cancel / drop | `readdress` compares `status='pending'` and each rewritten column to the raw text read; `test_a_row_answered_between_the_read_and_the_write_is_not_overwritten` |
| Thin `/ws` trigger (#918, ent#467) | both payload literals carry `id` + `agent_name` only and are built through `_broadcast_payload`; `test_ent467_ws_agent_scope.py` passed in the run |
| No address in an audit row or a log line (ent#715) | audit `details` = reason, to_role, agent_name; log lines carry ids, role names and refusal codes; grep of the diff for email patterns outside `example.com`: none |
| Public-repo disclosure (ent#45) | docs-guard pattern over the added public doc lines: no hit |
| SQL | SQLAlchemy Core with bound values throughout; the one `f"..."` in the private `projects/db.py` interpolates the module's own column-list constant |
| Fail direction stated (A10:2025) | the private trigger is fail-SOFT by design and says so: the grant stands; the OSS primitive re-raises only programming errors (`ValueError` on an unknown role or reason) |
| Concealed Unicode | zero hits over both diffs, both test files and the learnings fragment |

## Hypotheses

- A requester who is later made primary can approve their own gated request after the re-address (R8). Only an admin can create that state, and `enforce` already self-approves a requester who is primary at raise time, so this is the existing rule, not a new path. Stated in `skill-gate.md`.
- `readdress_item` attaches the new single addressee's Main before the CAS; a lost CAS leaves a Main created for the pair. Main is the pair's standing chat and idempotent, so nothing is disclosed.

## Coverage gaps

- Phases with no diff overlap were skipped: 3 (no dependency changes), 4 (no workflow changes), 5 (no Docker or compose changes), 6 (no webhook or integration code), 8 beyond the Unicode scan.
- No image was rebuilt and no live instance was driven; the evidence is the unit islands (26 OSS + 19 enterprise tests, plus the 804-test ask-sink neighbourhood).
- The enterprise suite's 16 failures are all `tests/test_5_two_factor.py` on a locally missing `pyotp`, which CI installs; not in scope of this diff.
