# Schedule → Workspace delivery — a brief lands in Main (trinity-enterprise#498)

> **Status**: ✅ Implemented (2026-09-07) · **Requirement**: `requirements/scheduling.md` §10.18
> **Journey**: J11 — *a companion's brief reaches me where I already work, without me asking*
> (`tests/journeys/catalog.yaml`, harness abilityai/trinity#2565)

## Overview

A schedule can name one Workspace user. When it fires, the execution's output lands as a
new turn in that person's **Main** chat with the agent (ent#523) — not in the executions
list, which is the operator's surface and the only place a scheduled run has ever been
visible.

## User Story

As the primary human a role companion works for, I want its daily brief to appear in the
conversation I already have with it, so I read it where I work instead of being asked to
go and look at an operations page.

## Entry Points

| Surface | How |
|---|---|
| API | `POST/PUT /api/agents/{name}/schedules[/{id}]` → `deliver_to_workspace_email` |
| MCP | `create_agent_schedule` / `update_agent_schedule`, same field (`null` clears it) |
| UI | **None this cut** — the schedule form is untouched, by decision |

## What already existed

Almost all of it. `channel_completion_report` has resolved, persisted and effect-guarded a
portal-bound completion since ent#457, and `report_completion` is **trigger-agnostic**:
`schedule` is deliberately *not* in `INLINE_CHANNEL_TRIGGERS`, because a scheduled run has
no surface that already answered. The single missing fact was that a scheduled execution
row never carried `source_channel='portal'`.

So this feature is **a stamp**, and deliberately nothing else: no new delivery path, no
second applier, no change to any terminal writer.

## Flow

```
scheduler (separate process)
  ├─ list_all_enabled_schedules  ──► Schedule.deliver_to_workspace_email   (SELECT *)
  ├─ create_execution                (the row — no channel columns)
  └─ POST /api/internal/execute-task { …, deliver_to_workspace_email }
        │
backend  ▼  routers/internal.py::execute_task_internal
  └─ services/schedule_workspace_delivery.resolve_and_stamp()
        ├─ is_client_blocked?            ── blocked      ─┐
        ├─ agent_on_roster(…, owned=True)── unreachable  ─┤ WorkspaceDeliveryRefused
        ├─ ensure_main_session()         ── unavailable  ─┤   → _fail_execution_row (FAILED)
        └─ db.stamp_execution_channel_context()           │   → 422, claim released
              WHERE source_channel IS NULL  ── taken ────┘
        │  (row now carries portal + session id + client email)
        ▼
     execute_task(...)  →  terminal  →  spawn_completion_report
        │
        ▼  channel_completion_report._resolve_portal   (UNCHANGED)
           wait, bounded, on the in-flight marker      (C9)
           add_portal_message(role="assistant") + touch_portal_session
```

## Why the resolution is backend-side

Two independent reasons, either of which is sufficient:

1. **The scheduler cannot resolve a session.** It is a separate process and cannot import
   the portal package.
2. **The kwargs would be inert.** The scheduler creates the execution row itself and always
   sends `execution_id`, so `execute_task`'s channel-persisting branch (`if not
   execution_id:`) never runs for a cron fire — passing the columns through would have
   looked right and done nothing. That is the #2426 class, and it is documented at the
   branch itself.

So the scheduler carries the **address** and nothing more.

## The stamp

`db.stamp_execution_channel_context` is the first UPDATE of these columns — every other
writer sets them at INSERT, which is why no updater existed. It is guarded on
`source_channel IS NULL` and returns whether it landed: it only ever **adds** a
destination. A row that already has one belongs to an inbound channel turn whose adapter is
waiting on that reply, and repointing it would deliver the answer to the wrong place.

Registered in `_EXPECTED_UPDATE_SITES` (`test_schedule_status_observability`) as a
non-status writer.

## Who may address whom

`_enforce_delivery_target_authority` (`routers/schedules.py`), on **both** create
and update: address yourself freely; address anyone else only as the agent's owner
or an admin. Schedule creation is `assert_agent_access`, so without this a merely
shared user could put a recurring message of their choosing into a colleague's Main
chat as an ordinary turn from the agent. Create-only would be a formality — create
without the field, PUT it a second later. Fail-closed on an unreadable ownership
read, and clearing the target is never privileged.

## Refusals are visible (AC 5)

| reason | when |
|---|---|
| `workspace_delivery_no_target` | the address is blank after normalisation |
| `workspace_delivery_target_blocked` | `is_client_blocked` |
| `workspace_delivery_target_unreachable` | not on the agent's Workspace roster (shared ∪ owned) |
| `workspace_delivery_target_unverifiable` | the block or roster read raised — **fail closed** |
| `workspace_delivery_session_unavailable` | `ensure_main_session` raised |
| `workspace_delivery_row_not_stampable` | the row already has a destination, or is gone |

A refusal **fails the pre-created row** and releases the idempotency claim, then answers
422. Running the turn anyway would spend the tokens and put the answer where nobody can
read it, so refusing before the spend is both cheaper and louder.

`_fail_execution_row` is an **admission-path** terminal — the refusal happens before
`execute_task`, so no dispatch activity exists to close. Allowlisted in
`test_1804_terminal_activity_parity` with that justification, beside the other admission
terminals.

## Access is checked against where the message will land

`agent_on_roster(agent, email, include_owned=True)` — the Workspace's own roster.
`email_has_agent_access` was rejected: it admits any admin, and an admin who neither owns
the agent nor is shared it **cannot open that thread**, so a brief delivered there would be
invisible. Pinned by an AST assertion that the rejected function is not called.

## The destination is Main

Through `client_portal.service.ensure_main_session` — the same landing rule an
agent-initiated message and an ask raised outside a chat already use, so a brief is not a
fourth thing that decides where to land. `_resolve_session_id`'s docstring named ent#498 as
a caller before this shipped.

## At-most-once per fire

Inherited, not rebuilt. `report_completion`'s `effect_guard` is keyed on the execution id
and a fire is one execution, so a re-delivered fire posts once by construction.

## C9 — never interleaved with an in-flight turn

`PortalConversation` detects a reply by an assistant-row count delta and renders the LAST
assistant row, so a report landing mid-turn can be read as that turn's answer (the
ambiguity ent#457 documented). The portal leg now waits, bounded
(`_INFLIGHT_WAIT_SECONDS`), on the ent#286 in-flight marker, then **writes anyway**.

The bound is the point: a wait that could refuse would trade a cosmetic misread for a lost
brief. It cannot hang either — `mark_turn_inflight` sets a TTL and `get_turn_inflight`
returns `None` when Redis is unreachable. It applies to **every** portal report, not only
scheduled ones: the misread is identical for the ent#457 delegation case. The complete fix
needs a per-row discriminator `enterprise_portal_messages` does not carry.

## Honest limits

- **Durable, not live-pushed.** The Workspace does not poll thread history, so the brief
  appears on the next load or thread switch. Acceptable at daily cadence; stated rather
  than implied.
- **No UI toggle** this cut.
- **Rooms are not a destination** — that is trinity-enterprise#442, along with
  agent-initiated `post_to_room`.
- **Rateable** like any agent message (#366) by construction: it is an ordinary `assistant`
  row in that session.

## Schema

`agent_schedules.deliver_to_workspace_email TEXT` — nullable, no backfill, no index (it is
read only through a row already loaded by id). Both tracks per Invariant #3: SQLite
`schedule_workspace_delivery`, Alembic `0056_schedule_workspace_delivery` off
`0055_portal_session_main_chat`.

## Testing

`tests/unit/test_ent498_workspace_delivery.py` — both migration tracks and the single-head
rule; the address normaliser driven over **both** models so the update path cannot be laxer
than the create path; the stamp's add-only guard; every refusal reason; the ordering that
puts the refusal ahead of both dispatch branches; the bounded wait; and that the scheduler
resolves no session.

`tests/journeys/test_j11_brief_delivery_journey.py` — the J11 skeleton (`strict=True`
xfail).

## Related Flows

- [workspace-agents-at-the-centre.md](workspace-agents-at-the-centre.md) — Main, the destination
- [channel-completion-report.md](channel-completion-report.md) — the delivery leg this reuses
- [scheduler-service.md](scheduler-service.md) — the fire path
