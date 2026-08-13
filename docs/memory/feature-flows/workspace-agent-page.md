# Feature: the Workspace agent page

> **Status**: ✅ Implemented (2026-08-13)
> **Issue**: abilityai/trinity-enterprise#360
> **Requirement**: `docs/memory/requirements/core-agent.md` §5.11
> **Related**: [workspace-sidebar-ia.md](workspace-sidebar-ia.md) (the roster row that opens it), [workspace-absorbs-session.md](workspace-absorbs-session.md)

## Overview

An agent had no home. A roster row emitted `new-chat-with-agent`, so there was
nowhere to see what an agent had been doing, nowhere for it to ask you something
while no chat was open, and nowhere to show what it can do. Clicking an agent now
opens its page; starting a chat is an explicit button there.

## The two constraints, and why both are subtractive

**It reports; it does not configure.** No schedules, no skill editing, no logs,
no costs. Model and plan are not shown at all — the AC permits them
"informational and visibility-gated", and the cheapest way to satisfy a gate is
to not open the door. Building agents stays operator-side.

**The viewer may be an external client.** The same page serves a portal-token
client and a platform user. That is what makes this a security surface rather
than a layout exercise, and it decides *where* the exclusions live.

## Exclusion by projection, not by template

Every field the page must not show is dropped in `client_portal/agent_page.py`,
before the payload exists. Nothing is filtered in the Vue component.

The reason is failure mode, not taste: a template filter is correct until
somebody adds a column to a list view, and nothing fails when they do. A field
that never leaves the service cannot be surfaced by a later edit.

| Surface | Underlying accessor also returns | Why it must not ship |
|---|---|---|
| `recent_work` | `message`, `cost`, `model_used`, `source_user_email` | `message` is another user's prompt; `cost` and `model_used` are excluded by AC #7 |
| `asks` | `context`, and `alert`-type items | `context` is free-form agent JSON and a known credential-leak surface (canary G-04 exists because secrets turn up there). `alert` items are platform-generated ops telemetry — sync-failing, git-bloat, breaker dormancy — not an agent asking a person anything |
| report detail | any report in the install | Report ids are global. The roster gate proves only that the caller may reach **this** agent, so without an ownership check the page becomes a reader for every report on the instance. A foreign id returns the same 404 as a missing one, so it is not an existence oracle either (invariant #8) |

## Flow

```
GET /api/enterprise/client-portal/agents/{name}/page?window=7d
  ├─ _require_roster ............ uniform 404 for an agent off the roster
  ├─ get_roster ................. identity + "what it can do" (briefing, #138/ent#380)
  └─ agent_page.build_page
       ├─ _health ............... last persisted health check
       ├─ _stats ................ db.get_agent_analytics (#1107) + first_try_stats
       ├─ _asks ................. operator queue, filtered + projected
       └─ _recent_work .......... executions, projected to shape only
```

One call rather than five, because the page is one screen: fetching header,
stats, asks and work separately renders it in pieces, and a stats call that
outruns the header shows numbers above a nameless card. Reports and Files are
separate — most visits never open those tabs, so they fetch on first open.

`GET /api/agents/{name}/analytics` is reused as the Technical Notes ask, but
through the **DB accessor**, not over HTTP: the platform endpoint is JWT-gated
and a portal-token client cannot call it.

## Degradation — AC #6

Everything is DB-sourced, so a stopped agent renders degraded rather than empty,
and a failing data source degrades **only its own section** (a page that 500s
because the operator queue is unhappy is worse than one without its asks).

Health reports `unknown`, never `unhealthy`, when nothing has ever checked the
agent. Monitoring is default-OFF (#1121), so on many installs that is every
agent, and "unhealthy" there would be a lie about the whole fleet.

## The two AC #3 metrics

**First-try rate** is real: successes with `retry_count` 0 over the window
(`client_portal/db.py::first_try_stats`). It is deliberately distinct from the
success rate the analytics accessor reports, which counts a retried-then-
succeeded execution as a success — the right answer to "does it get there in the
end", the wrong answer to "does it get there first time". `retry_count` is NULL
on rows predating #678, read as zero, since such a success genuinely had no
retry. No terminal executions ⇒ `rate: null`, not `0.0`: a fresh agent has no
first-try rate, and 0% reads as "it fails every time".

**Rating tally is not shipped.** There is no rating, thumbs or feedback
mechanism anywhere in Trinity — no table, no column, no endpoint. It has no data
source, so it was omitted rather than invented. A number a user reads as "how
well is this agent doing" has to come from something real.

## What this supersedes

ent#359 made a roster row with unread open the *unread chat*, because a badge
reading "2 replies" beside a control that opened a **blank** chat was a
contradiction. The page resolves that properly: the row opens the page, the
badge still shows on the row, and the page's Overview lists the chats this agent
belongs to with their unread counts. Nothing is lost, and "an agent is a
destination" is finally true.

## Files

| Layer | File | Change |
|---|---|---|
| Service | `client_portal/agent_page.py` | **new** — assembly + every projection |
| DB | `client_portal/db.py` | `first_try_stats` |
| Router | `client_portal/router.py` | 3 endpoints, roster-gated |
| Models | `client_portal/models.py` | page / ask / work / stats / report models |
| UI | `components/portal/PortalAgentPage.vue` | **new** — header, stats strip, 5 tabs |
| UI | `components/portal/PortalSidebar.vue` | roster row emits `open-agent` |
| UI | `views/Portal.vue`, `router/index.js` | `/workspace/a/:agentName` |
| Store | `stores/clientPortal.js` | `fetchAgentPage`, `fetchAgentReports`, `fetchAgentReport` |

## Tests

`tests/unit/test_ent360_workspace_agent_page.py` — the projections (no
message/cost/model; alerts excluded; `context` never present, asserted against
the rendered repr too), cross-agent report isolation and its 404 uniformity,
degradation (health `unknown`, per-section failure, capabilities from the
briefing), and the first-try arithmetic including the NULL-`retry_count` and
no-terminal-rows cases.

Verified live against the running instance: 37 executions, 89% completed, 33/37
first try, and `recent_work` carrying exactly the six safe keys.

## Known Limitations

| Limitation | Detail |
|---|---|
| **Asks are read-only** | Answering writes to the operator queue, an operator surface with its own auth. Rather than render a control that 403s for a client, the card offers "Reply in chat →". |
| **No rating tally** | See above — no data source exists. |
| **Files tab is a list, not the panel** | It lists documents and uploads; the upload flow stays in the existing files panel. |
| **"What it can do" is the briefing** | A projection of the roster briefing (#138/ent#380). ent#178 (unified exposable-skills config) is the mechanism this becomes a view of; it deliberately does not build a competing one. |
