# CSO Diff Audit — #3402 a held gated request is never replayed to the agent

- **Date**: 2026-10-20 · **Mode**: `--diff` (daily gate 8/10) · **Branch**: `feature/3402-held-turn-history` vs `origin/dev` (merge-base `d1c2447c4`)
- **Scope**: `client_portal/{service,db,voice,role_card}.py`, `client_portal/asks/service.py`, `services/{public_chat_service,skill_gate_service,skill_gate_errors}.py`, `adapters/message_router.py`, `shared_sessions/{service,db}.py`, `routers/public.py`, `db/{public_chat,schema,tables,migrations,agent_cleanup}.py`, `db_models.py`, `database.py`, Alembic `0101_held_gate_turns`, three `tests/unit/test_3402_*` files, docs

## Verdict: 0 findings.

The diff only REMOVES text from what reaches an agent: a request the skill gate held or refused, and its notice, are left out of every later prompt on the Workspace, public links, Telegram groups and rooms. No new route, input, dependency, workflow or Docker change.

## Checks run (diff mode; phases 0, 1, 2, 6, 7, 9, 12–14)

| Check | Result |
|---|---|
| Attack-surface delta | No new endpoint. `GET /api/rooms/{id}` NARROWS for an agent principal (MCP `read_room`): rows its own gate stopped are left out; people see everything. The agent identity comes from the auth context (`_caller` → `current_user.agent_name`), never the request |
| ASI01/ASI06 (context poisoning) — the reported path | Closed on every store that keeps held text. Workspace `enterprise_portal_messages.gate_hold` + one-time backfill from the gate record; public/group `public_chat_messages.gate_hold` (marked at write for senders who can never self-approve, and by the verdict); rooms `room_gate_holds` per agent. Every prompt read excludes by DEFAULT; only person-facing reads opt in (`include_held=True`). Channel DMs never store a held message |
| "Shown == scanned" (2026-10-02 learning) | Holds. The room transcript and the gate's `request_text` are both built from one filtered `shown` list (`shared_sessions/service.py::_wake_agent_locked`). The other surfaces filter only history the gate never scans by design, so the gate's input is unchanged |
| Bypass by hiding text from the gate | Not possible: nothing is removed from a scan without also being removed from the prompt. A failed holds read in a room fails OPEN — the post is scanned and held again (over-gating, stated in the docstring) |
| Person-facing reads | `get_history` (thread + reply poll), the retry dedupe, the role card, the ask card and the public visitor history pass `include_held=True`. The Workspace history route rejects agent principals (`portal_auth.py`), so an agent cannot read a held row there |
| Suppression abuse | A mark hides only the marker's own row (public/group), or, in a room, a post whose own text invokes that agent's gated skill — from that agent only. A participant cannot hide another's ordinary post |
| SQL | Parameterised throughout. f-strings interpolate only constant table identifiers (`_not_held`, the two-table migration loops) |
| Migration safety | Additive nullable columns + one table. The backfill sets the new column only, matched exactly through `skill_gate_requests.origin_execution_id` (never text). Both tracks share the SQL (`db.migrations.HELD_PORTAL_ROWS_BACKFILL`) |
| A10 failure direction | Stated in each docstring: room filter fails open (re-hold), `read_room` filter fails closed (503), write-time mark fails open on an unreadable gate map (review I1: the verdict path marks every held or refused turn; the gap is a message invoking a gated skill stored during the blip and never dispatched — chatter, or a turn that fails before dispatch), mark failures log ERROR. An exception without a `code` is not a standing refusal → re-delivered (the safe direction) |
| Secrets in diff | None. A test's fake API key was changed from an `sk-`-shaped string to a neutral placeholder during the audit |
| Concealed Unicode in added lines | None |
| Enterprise-docs guard | Clean. `room_gate_holds` deliberately carries no `enterprise_` prefix, and the docs avoid naming one |
| Tests | 39 new tests over the real schema and the real gate backstop; 20 mutations of the fix each turn tests red |

## Residual limits (documented, not findings)

The ones this diff does not close are stated in `requirements/security.md` §26.13 and the skill-gate flow: prose that names no gated invocation (Codex and Gemini have no hook); rows from before the fix that carry nothing identifying them; MEM-001 summaries already written; and two races with the gate's own check, filed as #3846 (a Workspace message sent while an earlier one in its thread is inside the check; two simultaneous wakes of one push agent in a room).

## Hypotheses

None (daily mode).

## Coverage gaps

Diff mode: full-repo phases (secrets archaeology over history, dependency scan, CI/CD, image internals) not run — out of scope for a logic diff with no dependency, workflow or Docker change. The PostgreSQL track (`0101_held_gate_turns`) was not executed locally; CI's `pg-migrations` runs it.

## Trend

Narrows the open note carried since `cso-diff-2026-10-02-ent751-gated-skills` ("history is not re-read"): a request the gate stopped no longer reaches a later prompt. This audit adds no fingerprints.
