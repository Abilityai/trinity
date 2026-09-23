# Workspace — the seat-level decision record (trinity-enterprise#638, R25)

## Overview

Every week a role companion and its human approve, defer or kill things. The judgment
used to live nowhere — it evaporated with the chat that held it — so the seat's
judgment never compounded and nothing could say later *why*. Operator ruling R25
(2026-09-15) formalises it into the platform: a **seat** (agent × person, the ent#637
memory scope) gets a small, lintable **decision record**, recorded by the companion
over MCP or the person in Agent details, expiring unless reconfirmed, corrected by
supersession, and read back into every turn so the criterion is reused. It is the
evidence base for the autonomy dial (tandem-06 §2.2, #641): work is promoted to
unprompted execution when its records show the same criterion applied repeatedly
with no reversals — not because it is frequent.

## Flow

```
companion (execution e1, seat run or user-facing turn)
  │  record_decision {execution_id, outcome, decided, alternatives[], criterion, reversal,
  │                   review_by, scope?, notes?, ask_class?, cites?, request_id?}
  │  Idempotency-Key = mcp:sha256(record_decision agent execution_id decided)
  ▼
POST /api/agents/{name}/decisions            routers/seat_decisions.py  (# mcp: decisions.ts)
  │  assert_agent_access · rate limit · seat = _seat_for(execution)   ← the MEM-001 rule:
  │      schedule_seat_memory.seat_for_execution (ent#498 stamp) | source_user_email (public/slack/…) | 422 no_seat
  │  idempotency_service.begin(scope="seat_decision:{agent}:{seat}") → replay → snapshot
  │  decided_by_role = body.decided_by_role | assignment_provider.resolve_assignment().role_id | None
  ▼
services/seat_decision_service.record
  │  validate_record  → 422 decision_prose_only {receipt.fields}  |  422 decision_is_a_note
  │  _validate_cites  → 422 unknown_citation (same seat, any status)
  │  scope=direction  → status=routed (+ CANON_PROPOSAL_HINT)  |  else status=active
  ▼
db.insert_seat_decision  (seat_decisions; emails lower-cased; JSON lists in TEXT)
  ▼
{success, decision: to_agent(row) — decided_by.person ∈ {seat, owner}, NO email, hint}

person (Workspace principal)
  │  PortalAgentDecisions.vue → GET /api/enterprise/client-portal/agents/{name}/decisions
  ▼
client_portal/seat_decisions.page      _require_roster (uniform 404)
  │  readable_seats: own seat (writable) ∪ owner → every seat (writable)
  │                  ∪ provider.kinds_for(agent, me) ∈ READER_KINDS → every seat (read-only)
  │  rows per seat → to_human(row, writable)  (status = effective_status: expired computed)
  │  stats(own seat rows): recorded, reused, reuse_rate, reversed, ask_classes[{criteria, reversals, expired, stable}]
  ▼
PortalSeatDecisions {my_seat, seats, decisions[], stats, can_record}

person ──► Record (form) ──► POST …/decisions {fields, seat?}        own seat | owner on a named seat (403 seat_not_yours)
person ──► Correct        ──► POST …/decisions/{id}/actions {action: supersede, fields}   CAS: old→superseded + new row
person ──► Reconfirm      ──► {action: reconfirm, review_by}                              CAS on active
person ──► Reverse        ──► {action: reverse, reason}   (reason required — the evidence) CAS on active
person ──► Close (confirm)──► {action: close}                                             CAS on active
                              non-active → 409 decision_not_active · foreign seat → 404

every turn ──► platform_prompt_service.format_user_memory_block(record)
                 └ _seat_decisions_block → seat_decision_service.prompt_block(db, agent, seat)
                     "## This seat's standing decisions" — [id] outcome: decided — because criterion …
                     (active only, PROMPT_BLOCK_MAX / ~1.5 KB, fail-open, no emails)
```

## Why it is shaped this way

- **A note is not a decision.** tandem-07 §7a: a record with no alternatives is a note.
  The grammar refuses it (`decision_is_a_note`) rather than storing a weaker row —
  the seat's memory already holds notes (ent#637).
- **Receipts, not silence.** Prose-only entries are refused with `receipt.fields`
  naming every failing field and its rule (§7a "lintable, not prose", [R C2]); the
  Workspace lands each message on the field it names; the MCP tool returns it in the
  `success:false` envelope so the companion can repair the call.
- **Expiry is a state, not a purge.** `expired` = `review_by < today (UTC)`, computed
  on read; `reconfirm` moves the date. Nothing is deleted; §7a "expires rather than
  accretes" is honoured without a sweep.
- **Correction supersedes.** A new row with `supersedes_id`; the old flips to
  `superseded` in the same CAS transaction, so a double submit cannot fork the chain.
  "Superseded records stay as history" (AC 4) by construction.
- **Direction is routed, never dropped.** R C3 says a direction decision is a canon
  proposal; the platform cannot classify pricing from text, so the caller declares
  `scope`. A `direction` record is kept as `routed` (visible, expiring, outside the
  evidence) with the hint — refusing and storing nothing would be the evaporation
  R25 closes.
- **The seat is never named by the caller.** The companion's seat comes from
  `execution_id`, exactly as `write_user_memory`; the companion never receives an email
  back (`to_agent` labels the decider `seat` / `owner`) — an execution can be an
  anonymous public turn, the audience the assignment provider must never expose staff
  identities to.
- **Readers per assignment kind, safely.** The assignment record is private; the seam
  gains one optional method (`kinds_for`), read through `getattr`, allowlisted,
  failure → `None`. A core build reads own-seat only; the owner (creator / infra owner,
  `role_card._is_owner`) reads every seat. Per-agent in v1 — DEBT_INBOX 2026-09-22.
- **Conversion, not volume.** `reused` counts records a LATER record cited; a
  superseding record does not implicitly cite its predecessor. Per ask class the raw
  evidence (criteria, reversals, expired) plus `stable` (≥3, one criterion, no reversal)
  ships as #641's input, not its verdict.
- **Read into context.** Without a read path "reused" would measure a loop that does
  not exist; the block rides the SAME composer every caller already uses
  (`format_user_memory_block`), keyed off the record's own seat, so no caller can forget it.
- **No container read on the write path.** `decided_by_role` comes from the provider
  (sync, in-memory) or the caller; `x-role` is agent-writable anyway, so an HTTP read
  into a running agent would buy latency and a failure mode, not integrity.

## Files

| Layer | File | Role |
|---|---|---|
| DB | `db/seat_decisions.py` · `db/schema.py` · `db/tables.py` · `db/migrations.py` (`seat_decisions_table`) · `migrations/versions/0071_seat_decisions.py` · `db/agent_cleanup.py` (`AgentRef`, CASCADE) · `database.py` facade | rows, both tracks |
| Service | `services/seat_decision_service.py` | grammar + receipts, lifecycle, evidence, readers, the two shapes, the prompt block |
| Seam | `services/assignment_provider.py` (`kinds_for`, optional) | reader kinds from the private record |
| Agent-facing | `routers/seat_decisions.py` · `models.RecordDecisionRequest` · `src/mcp-server/src/tools/decisions.ts` · `access.ts` rows · `client.ts` | record / list over MCP |
| Workspace | `client_portal/seat_decisions.py` · `client_portal/router.py` · `client_portal/models.py` · `PortalAgentDecisions.vue` · `stores/clientPortal.js` | read / record / correct / close |
| Prompt | `services/platform_prompt_service.py::_seat_decisions_block` | read-into-context |
| Tests | `tests/unit/test_ent638_seat_decisions.py` (44) · `src/frontend/tests/unit/portalAgentDecisions.spec.js` (8, mounted) · `src/mcp-server/src/tools/decisions.test.ts` (5) | |

## Deferred (DEBT_INBOX 2026-09-22)

Readers per role rather than per agent; auto-record from an answered decision REQUEST
(#611); the canon-folder export (the role pack's `record-decision`, #510, reads the
MCP list — the field names already match).
