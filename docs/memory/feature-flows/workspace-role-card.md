# Workspace — the role card in Agent details (trinity-enterprise#527, #663)

## Overview

When a companion has a role (Tandem, ent#497), the Info rail's Agent details show a
**Role** card: the role it fills, the objectives it owns or supports with each metric's
value / target / freshness, the viewer's relationship to it, and its **readiness**.
Framework §8's "organisation UI over the canon" shrunk to one agent — files are truth,
the card is a projection. This cut ships the Role + Readiness half; the relationship
line reads from ent#500 when it lands.

## Flow

```
Info rail ──► PortalAgentRole.vue ──► GET /api/enterprise/client-portal/agents/{name}/role
                                          │  _require_roster (uniform 404)
                                          │  admit_objectives() — built here, called by the builder:
                                          │    viewer cap  portal_role_objectives:{email}:{name}  limit/3 (20/min)
                                          │    AND shared  objectives_read_budget.admit(name)     60/min
                                          │    (never raises — a refused read is a 200 without objectives)
                                          ▼
                                    client_portal/role_card.build_role_card(admit_objectives=…)
                                          │  docker state  ── not running → {unavailable, readiness(stamp)}
                                          │  agent door (get_agent_client):
                                          │    read_file template.yaml   → x-role {role, status, seat}, x-canon.clone_path
                                          │    read_file <canon>/roles/<id>.yaml
                                          │  db.get_agent_role_readiness (the owner's stamp)
                                          │  admit_objectives() — only here, after the role file; any earlier
                                          │    return (no role, stopped, role-file error) spends nothing
                                          │  refused ── objectives_error: objectives_rate_limited, no fan-out
                                          ▼
                                    objective_join_service.read_objective_join(agent, template=, client=)
                                          │  the ONE join (ent#666): objectives listing + files through the
                                          │  same client, the metric registry, the point store, the 2× cadence rule
                                          ▼
                                    portal_objective / portal_metric / objectives_error / finding_codes
                                          ▼
                                    PortalRoleCard {role, seat, objectives[metrics], objectives_error,
                                                    finding_codes, readiness, walkthrough, relationship,
                                                    can_flip_readiness}

owner ──► Mark ready (ConfirmDialog) ──► POST …/role/readiness {status}
                                          │  role_card.flip_readiness: owner check (get_owned_roster) → 403 readiness_owner_only
                                          ▼
                                    db.set_agent_role_readiness (upsert) → re-read the card
```

## Why it is shaped this way

- **Never a second store.** Nothing about the role is cached or copied platform-side;
  every read goes through the agent's own server, so the card can never disagree with
  the file — and a failed read is *named* (`role.error`), never rendered as an empty
  role. No `x-role` → no card at all: the panel is byte-identical for every agent that
  has no role.
- **Author-controlled input is bounded.** `x-role.role` and `x-canon.clone_path` reach
  a file read, so both are validated before any path is built — the role id by `_ID_RE`,
  the canon path by the join's own `canon_root` (one validator, not a copy); text fields
  are capped. The objective bounds (scan 100 files, 20 objectives, 12 metrics each) are
  the join's.
- **The objectives are the one join, and the card is a projection of it (ent#676).** The
  card shipped with its own objective ↔ metric join and a 30-day staleness rule over
  `metrics.json`; both are gone. `build_role_card` calls
  `objective_join_service.read_objective_join` in process, handing it the template and the
  agent client it already holds (the template is read once), so the numbers are the point
  store's and a metric is stale exactly when the platform's one rule says so — a declared
  cadence and no point within 2× of it. `is_stale` / `STALE_AFTER_DAYS` survive for the
  role file's `review_by` only (framework §3.5 governs files, not metrics). The import is
  function-local, so no portal suite loads the metrics stack.
- **What a client sees is an allowlist (TD-4).** `portal_objective` and `portal_metric`
  PICK fields: `id, statement, horizon, status, owned` and `name, type, unit, target,
  actual, last_point_at, stale, freshness, gap.status, finding.code`. The operator's
  sentences ("call `refresh_metric_definitions`"), objective file paths and `owner: role:<id>` stay
  on the operator door (the #78 auth-path invariant); a finding crosses as its code and
  `PortalAgentRole.vue` holds the client sentence for each. Card-level findings cross as
  `finding_codes` — distinct codes, nothing else, because a file-level finding can name
  another role's file. A key the join grows later reaches no client until it is added to
  the picker and to `PortalRoleMetric`; the tests pin both key sets as literals.
- **No objectives is silent only when nothing went wrong reading them.** `objectives_error`
  names every other way of arriving at zero: `objectives_rate_limited` (the budget),
  `agent_unreachable` (the door died mid-read), `objectives_timeout`,
  `objectives_unreadable` (the directory would not list, an objective file failed to read
  and none of this agent's objectives joined, or the join raised — a store outage is
  caught, logged with its traceback, and named), `objectives_incomplete` (a file would not
  parse, was refused by name, or lay beyond the scan bound). File-level failures cannot be
  attributed to a role (whose file it was is unknowable when it never parsed), so in a
  shared canon another role's broken file can put the `objectives_incomplete` line on an
  agent with no objectives of its own. An agent whose objective files all read cleanly and
  none name it shows no objectives block, as before. A role file that fails to load stops
  the card before the objectives are read; the role-error line is what the viewer sees.
- **No number from another agent (ent#727).** The join can resolve a metric an objective
  names from an agent this one holds an `agent_permissions` grant on, but only through a
  `can_view` that says who is looking. The card passes none, so the join's fail-closed
  default holds on the Workspace: a viewer never sees a number served by an agent they may
  not be rostered on, and `served_by` is not projected either. Pinned by
  `test_the_card_never_resolves_another_agents_metric`.
- **A partial list says so.** When objectives joined but some objective files were not read
  (would not read or parse, refused by name, beyond the scan bound), `objectives_partial`
  is set and one line under the list says it may be incomplete. The same file-level codes
  cannot be attributed to a role, so the line says "may". Added at PR #3132's review; the
  claim-time ruling had covered only the zero-objectives case.
- **One budget, two doors, and they fail differently.** The card's objective read draws on
  the bucket `GET /api/agents/{name}/objectives` draws on
  (`services/objectives_read_budget.py` — key, limit and window spelled once). The operator
  door answers 429; the card never does. It also carries the role, the readiness stamp and
  the owner's flip, and the agent can empty the bucket by polling its own `get_objectives`,
  so refusing the whole card would let an agent hide its owner's control. A refused read
  leaves the objectives out, says so, and performs no fan-out. The check is spent where the
  fan-out starts — the builder calls it after the role file — so an Info tab for an agent
  with no role, a stopped agent or a broken role file costs nothing. The viewer's own cap
  (a third of the limit per agent, 20/min at the default, derived so it follows the limit) is
  checked first, so one viewer spends at most a third of the shared
  budget and a refused viewer spends none of it.
- **Readiness is the owner's stamp (#663).** `x-role.status` is agent-writable, so a
  file that says `ready` proves nothing. The effective state is the platform record if
  present, else `calibrating`; a template-claimed `ready` with no stamp is shown as
  calibrating with the note that no owner stamped it. The flip is gated on the platform's
  owner of the agent record (creator, not an assignment kind); a portal-token principal is
  never an owner; the agent has no route to it. The flip records — it does not switch
  the brief schedule on.
- **Walkthrough progress** is the viewer's own count (user turns in their Main, capped at
  ten; their own thumbs-down over the replies), labelled as such.

## Schema

`agent_role_readiness (agent_name PK, status, changed_at, changed_by)` — SQLite
`agent_role_readiness_table`, Alembic `0067_agent_role_readiness`; CASCADE in `AGENT_REFS`.

## The stamp gates the proactive brief (ent#689)

```
scheduler cron fire, schedule.deliver_to_workspace_email set
  → _apply_readiness_gate                    (cron + seat only; first, so a held brief never runs the hook)
      GET /api/internal/agents/{name}/brief-readiness
        services/role_readiness_gate.brief_readiness
          stamp? → ready fires / else held
          no stamp → container running? template.yaml (≤3 s) has x-role? → held : fires
          any ambiguity → fires (logged)
      fire:false → _record_gate_skip: skipped row + reason, run times advanced, skipped event
  → _apply_pre_check_gate (#454)            → skipped? stop
  → dispatch
```

- **Why the scheduler, not `internal.execute_task`:** the #454 pre-check already owns a
  cron-only, fail-open skip with a reason and a *skipped* event. Gating later would have
  created-then-failed a row, published a failure, and opened the seat's Main chat for a
  brief that never runs.
- **Why the template's `status` is never read:** it is agent-writable; the stamp is the
  owner's act (#663). The template answers only "is this a companion".
- **Rollout:** a one-time, DB-only seed (both tracks) stamps `ready` —
  `changed_by = rollout:ent#689` — for every agent whose seat brief fired at deploy
  (enabled schedule, autonomy on),
  insert-if-absent. The card renders it as "carried over when the readiness gate
  shipped" (`source: "rollout"`), and `brief_held` adds "its scheduled brief is paused
  until you mark it ready" (or "its owner marks") next to the flip.

## Testing

`tests/unit/test_ent527_role_card.py` — the card driven through a fake agent door: no
role → no card; missing / invalid / unreadable role file named; traversal-shaped ids and
paths never reach a read; objectives filtered by owner/supporting agent; stale vs fresh
metrics; the readiness rule; owner-only flip with named refusals; the stamp against a
real SQLite file; both migration tracks. `src/frontend/tests/unit/portalAgentRole.spec.js`
mounts the component: nothing for no role, error copy, stale badge, who/when, the
unstamped-ready call-out, the confirm-then-post flip, the non-owner hiding, the refusal.
ent#676: `tests/unit/test_ent676_role_card_join.py` — the projection's key sets as
literals and the leak check against the operator's own read of the same files; the card
through the REAL join (store numbers, the 2× cadence rule, the template read once); every
named way of showing no objectives with the role and readiness intact; and the two doors
on the real in-process limiter (the operator door empties the budget and the card still
answers; card reads spend the operator's budget; the viewer cap is spent first; off-roster
is a 404 before any key exists). `test_ent666_objective_join.py`'s one-stale-rule test now
covers `role_card.py` by AST. The spec mounts every gap status, freshness state, finding
code and `objectives_error` code.
ent#689: `tests/unit/test_ent689_readiness_gate.py` (the verdict, every fail-open case, the
endpoint, the rollout source, `brief_held`, the seed on both tracks against real SQLite) and
`tests/scheduler_tests/test_ent689_readiness_gate.py` (held → skipped row + event, never
dispatched; manual / webhook / non-seat not asked; every error fires).

## Related Flows

- [workspace-agents-at-the-centre.md](workspace-agents-at-the-centre.md) — the Info rail
- [agent-custom-metrics.md](agent-custom-metrics.md) — the objective ↔ metric join the card projects, and the one stale rule
- [schedule-workspace-delivery.md](schedule-workspace-delivery.md) — the brief, and the seat's memory (ent#637)
