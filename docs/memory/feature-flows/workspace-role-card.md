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
                                          ▼
                                    client_portal/role_card.build_role_card
                                          │  docker state  ── not running → {unavailable, readiness(stamp)}
                                          │  agent door (get_agent_client):
                                          │    read_file template.yaml   → x-role {role, status, seat}, x-canon.clone_path
                                          │    read_file <canon>/roles/<id>.yaml
                                          │    GET /api/files?path=<canon>/objectives → *.yaml → read each
                                          │    GET /api/metrics → values + last_updated
                                          │  db.get_agent_role_readiness (the owner's stamp)
                                          ▼
                                    PortalRoleCard {role, seat, objectives[metrics], readiness,
                                                    walkthrough, relationship, can_flip_readiness}

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
  a file read, so both are validated (`_ID_RE`, one plain segment chain, no `..`) before
  any path is built; text fields are capped; objectives ≤ 20, metrics ≤ 12 each.
- **Freshness is the framework's own bound — interim.** A metric is stale when its value is
  missing, when `metrics.json` has no `last_updated`, or when that stamp is older than
  30 days (§3.5). Stale renders beside the last value and its age, never as current.
  *Interim rule (operator ruling 2026-09-21, recorded on ent#476):* the platform's one
  read path and stale rule for business metrics is `GET /api/agents/{name}/metrics`
  re-backed by `metric_points` (ent#479, stale = no point within 2× the declared
  cadence). When that lands, this card's `last_updated` + 30-day check and the
  objective ↔ metric join in `role_card.py` move to that shared read; the swap is
  named in ent#479's acceptance.
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
  → _apply_pre_check_gate (#454)            → skipped? stop
  → _apply_readiness_gate                    (cron + seat only)
      GET /api/internal/agents/{name}/brief-readiness
        services/role_readiness_gate.brief_readiness
          stamp? → ready fires / else held
          no stamp → container running? template.yaml (≤3 s) has x-role? → held : fires
          any ambiguity → fires (logged)
      fire:false → _record_gate_skip: skipped row + reason, run times advanced, skipped event
  → dispatch
```

- **Why the scheduler, not `internal.execute_task`:** the #454 pre-check already owns a
  cron-only, fail-open skip with a reason and a *skipped* event. Gating later would have
  created-then-failed a row, published a failure, and opened the seat's Main chat for a
  brief that never runs.
- **Why the template's `status` is never read:** it is agent-writable; the stamp is the
  owner's act (#663). The template answers only "is this a companion".
- **Rollout:** a one-time, DB-only seed (both tracks) stamps `ready` —
  `changed_by = rollout:ent#689` — for every agent whose seat brief fired at deploy,
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
ent#689: `tests/unit/test_ent689_readiness_gate.py` (the verdict, every fail-open case, the
endpoint, the rollout source, `brief_held`, the seed on both tracks against real SQLite) and
`tests/scheduler_tests/test_ent689_readiness_gate.py` (held → skipped row + event, never
dispatched; manual / webhook / non-seat not asked; every error fires).

## Related Flows

- [workspace-agents-at-the-centre.md](workspace-agents-at-the-centre.md) — the Info rail
- [schedule-workspace-delivery.md](schedule-workspace-delivery.md) — the brief, and the seat's memory (ent#637)
