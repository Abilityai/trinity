# Workspace — the autonomy dial (trinity-enterprise#641, P12)

## Overview

Autonomy in Trinity was one boolean per agent: scheduled runs on or off. The
operating model (canon `tandem-06-operations.md` §2.3) needs two more scopes. Above
the agent sits one **instance level** — `L0` Continuity, `L1` Companion, `L2`
Delegated classes, `L3` Load-bearing judgment — the ceiling on the whole fleet.
Below it sits a state per **(seat, ask class)**: `on_request` or `graduated`.
A graduated class means the companion acts and reports; everything else it asks
first, and it is told which is which, in the same words the person reads.

Promotion is **earned from #638's decision record** — the same criterion applied
repeatedly with no reversals — never granted by clicking. There is no promote
control anywhere in the product. A person can move a class DOWN (hold) and let it
back up (release); the level is an admin grant that can only cap.

## Flow

```
── the ceiling ──────────────────────────────────────────────────────────────────
admin (interactive principal ONLY — an agent's own MCP key can read, never raise)
  │  Settings → Retention → AutonomyDialPanel.vue
  │  PUT /api/settings/autonomy-dial {level: L0|L1|L2|L3}
  ▼
routers/settings/autonomy_dial.py     require_admin + reject_non_interactive_principal
  │  validate ∈ LEVELS → 422 invalid_autonomy_level        audit: autonomy_dial_change
  ▼
system_settings['autonomy_dial_level:instance']      ← one validated key, not a table
     ▲ generic PUT /api/settings/{key} REFUSES this key (the ent#297 catch-all door)

── the earned half ──────────────────────────────────────────────────────────────
a real event ──┬── seat_decision_service.record()            (a decision recorded)
               ├── seat_decision_service.act()               (supersede | reverse | close)
               ├── client_portal.service.submit_rating()     (the seat rates a turn)
               └── client_portal.autonomy.act()/set_guard()  (hold | release | guard)
  ▼
services/autonomy_dial_service.evaluate_seat(db, agent, seat, persist=True)
  │  per ask_class of the seat's rows:
  │     class_evidence(rows, today)      ← NON-EXPIRED rows only = "the window"
  │        reversed row → counted ONLY if it has not lapsed (effective_status returns
  │        `reversed` unconditionally, so a reversed row never becomes `expired`;
  │        counted over all history, one reversal ever blocks the class forever)
  │        → records · distinct normalized criteria · reversals · earliest review_by
  │     evaluate_class: ≥3 records · exactly 1 criterion · 0 reversals
  │                     · no negative rating in the last 30 days
  │                     · guard_metric != capped   · not held
  │        → state ∈ {on_request, graduated} + blocked_by[] (every reason NAMED)
  ▼
db.upsert_seat_ask_class_state   CAS on evidence_hash (previous_hash)
     unchanged verdict → no write, no event · a real change → agent_event
                                                (autonomy_class_graduated | _demoted)

── what anyone reads ────────────────────────────────────────────────────────────
autonomy_dial_service.live_verdict(stored, level=…, autonomy_enabled=…, today=…)
  │  stored earned-state  AND  level ≥ L2  AND  agent.autonomy_enabled
  │                       AND  today <= evidence_expires_at
  │  → unprompted: bool  ·  blocked_by: [named …]        READS PERSIST NOTHING
  ▼
companion ──► get_autonomy {execution_id}          src/mcp-server/src/tools/decisions.ts
                 → GET /api/agents/{name}/seat-autonomy   routers/seat_decisions.py
                    seat from execution (_seat_for, the MEM-001 rule) · no email out
person    ──► PortalAgentAutonomy.vue → GET /api/enterprise/client-portal/agents/{n}/autonomy
                 own seat, or every seat for the agent owner (_require_roster → 404)
              ──► POST …/autonomy/actions {action: hold|release, ask_class, seat?}
                    hold    = a refusal   → any reader, own seat (owner: any seat)
                    release = a grant     → the agent OWNER only (403 release_not_yours)
              ──► POST …/autonomy/guard   {ask_class, guard_metric} → owner only
every turn ──► platform_prompt_service memory block → autonomy_dial_service.prompt_lines
                 "You may act unprompted on: vendor-renewal."
                 "Ask first on: hiring-scope — the instance autonomy level is below L2."
```

## Why it is shaped this way

- **The verdict is a stored earned-state ANDed with three read-time conjuncts**, not a
  materialised boolean. Each conjunct fails silently if written instead: a class whose
  records all lapse at UTC midnight fires **no event**, so a stored verdict outlives
  its own evidence with nobody watching; and re-evaluating the fleet inside a level
  change is an unbounded fan-out with no fleet-wide seat query to drive it. As
  conjuncts, dropping the level needs **zero** writes and raising it back restores
  exactly what each class had earned.
- **Reversals are counted over the window.** `effective_status` returns `reversed`
  unconditionally — a reversed record never becomes `expired`. Counted over all
  history, one reversal ever would block a class permanently and `on_request` would be
  the only reachable steady state. `_has_lapsed` retires a reversed record at its own
  `review_by`, like every other row. The dial is a dial, not a ratchet.
- **The rating window has three deliberate shapes**, each a defect first. It matches
  `operator:<email>` as well as `workspace:<email>` — on a single-operator install the
  seat person IS the platform principal, so their thumbs-down lands under the other
  prefix and the class could never demote. It reads `COALESCE(updated_at, created_at)`
  — flipping a rating up→down touches only `updated_at`, and that flip is precisely a
  demotion. And it is a FIXED 30 days, not "since the oldest surviving record", which
  would let a blocking rating fall out as records expire — promotion by the clock,
  which R25 forbids.
- **Promotion is earned; only demotion is a control.** There is no promote button and
  no API that sets `graduated`. Holding is a **refusal** — anyone may make it for their
  own seat. Releasing is a **grant** — the agent owner only. The grant-vs-use line, one
  level down from the admin gate.
- **The level is a grant, so it is admin AND interactive.** Raising the ceiling is what
  lets anything run unprompted at all; an agent's injected MCP key may read the dial and
  can never raise it (`reject_non_interactive_principal`, the ent#293/#297 shape). It
  is also blocklisted on the generic `PUT /api/settings/{key}` catch-all — the one door
  that can address any key.
- **Every block is named.** `level_below_l2`, `agent_autonomy_off`, `evidence_expired`,
  `too_few_records`, `criterion_not_stable`, `reversal_in_window`,
  `negative_rating_in_window`, `guard_metric_capped`, `held_by_operator` — each with a sentence. A bare
  "not autonomous yet" teaches nobody what to do next, and the companion needs the
  reason to say why it is asking rather than inventing one.
- **The ceiling needs a control, not only an endpoint.** The level is the one
  thing an admin sets, and a setting reachable only by `curl` is a setting the
  product does not have. The panel also has to argue for itself: an operator who
  believes lowering the level throws away what every seat earned will never move
  it, so the panel says outright that raising promotes nothing and lowering
  destroys nothing — which is true precisely because the earned half is stored
  and the level is ANDed at read time.
- **The seat read needs its own noun.** `/api/agents/{name}/autonomy` is the
  agent-level `autonomy_enabled` toggle (`agent_config`), registered first in
  `main.py`; a second declaration on that path is matched by neither error nor
  warning — FastAPI simply serves the first one and the new route is dead. Caught
  by calling it live, not by a test or a diff read.
- **The companion reads the same verdict as the person.** `prompt_lines` rides the seat's
  existing memory block (the #638 composer), so no caller can forget it, and the model
  cannot believe it is more autonomous than the panel says.
- **Writes only on real events, CAS'd on an evidence hash.** A read never persists; an
  unchanged verdict writes no row and emits no event, so the event stream carries
  transitions, not heartbeats.
- **#638's contract is unchanged.** It shipped `stats().reversals` as this issue's INPUT
  and deferred the verdict; `autonomy_dial_service.class_evidence` is the verdict and
  reads #638's rows without altering them.

## Files

| Layer | File | Role |
|---|---|---|
| DB | `db/seat_ask_class_state.py` · `db/schema.py` · `db/tables.py` · `db/migrations.py` (`seat_ask_class_state_table`) · `migrations/versions/0073_seat_ask_class_state.py` · `db/agent_cleanup.py` (CASCADE) · `db/evaluations.py` (`latest_negative_seat_rating`) · `database.py` facade | rows, both tracks |
| Service | `services/autonomy_dial_service.py` | the rule leaf: levels, evidence, the named blocks, `live_verdict`, `evaluate_seat`, `prompt_lines` |
| Hooks | `services/seat_decision_service.py` (`_reevaluate_autonomy` on `record` + the three terminal `act` branches) · `client_portal/service.py` (`submit_rating`) | re-evaluate on a real event |
| Level | `components/settings/AutonomyDialPanel.vue` · `views/Settings.vue` · `routers/settings/autonomy_dial.py` · `routers/settings/__init__.py` (before `generic.router`) · `routers/settings/generic.py` (blocklist) | the instance ceiling |
| Agent-facing | `routers/seat_decisions.py` (`GET /{agent_name}/seat-autonomy`) · `src/mcp-server/src/tools/decisions.ts` (`get_autonomy`) · `access.ts` · `client.ts` | the companion's read |
| Workspace | `client_portal/autonomy.py` · `client_portal/router.py` · `client_portal/models.py` · `PortalAgentAutonomy.vue` · `PortalAgentDetails.vue` · `stores/clientPortal.js` | read / hold / release / guard |
| Prompt | `services/platform_prompt_service.py` (via `seat_decision_service.prompt_block`) | read-into-context |
| Tests | `tests/unit/test_ent641_autonomy_dial.py` (44) · `src/frontend/tests/unit/portalAgentAutonomy.spec.js` (10, mounted) · `autonomyDialPanel.spec.js` (5, mounted) · `src/mcp-server/src/tools/decisions.test.ts` (7) | |

## Deferred (DEBT_INBOX 2026-09-23)

Per-agent or per-seat levels (the instance level is the only ceiling in v1);
auto-feeding `guard_metric` from the §49 declared-metric registry (the owner sets it
explicitly for now); an audit trail of hold/release beyond the row's `held_by`/`held_at`.
