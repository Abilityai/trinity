# Feature: Workspace deliverables — reports with an audience

> **Status**: ✅ Implemented (2026-08-24)
> **Issue**: abilityai/trinity-enterprise#365
> **Requirement**: `docs/memory/requirements/core-agent.md` §5.14
> **Related**: [agent-reports.md](agent-reports.md) (#918 — the output this gives an audience), [workspace-agent-page.md](workspace-agent-page.md) (ent#360 — the page the list lives on), `docs/memory/requirements/core-agent.md` §5.11 (ent#364/#428 — the ask surface whose scoping bug this is the twin of)

## Overview

Agents publish structured reports (#918) and the person they were produced for
had no way to see them. Reports were operator-scoped: the Agent Detail tab, the
fleet view, and — since ent#360 — a Workspace Reports tab that listed *the
agent's entire history to every client of that agent*.

Two things change. A report can be **addressed** to a Workspace user, and the
Workspace read is **scoped to whoever is asking**. A deliverable then appears in
two places: on the producing agent's page for that person, and as a card in the
chat that produced it.

## The bug this fixes on the way past

`agent_page.reports()` called `db.get_reports_for_agent(agent_name)` — the
OPERATOR question, "everything this agent ever published". The Workspace asks a
different one. So a client of a shared agent saw every report it had produced
for every other client, rendered from free-form agent JSON on a client-facing
surface.

ent#428 fixed exactly this shape on the sibling surface — `_asks` was scoped by
`agent_name` alone, so co-shared clients read each other's pending asks. This is
the same defect over a bigger blob, and the same remedy: the read takes the
reader's identity, and `client_email` is a **required** keyword on the detail
path so no caller can inherit the unscoped behaviour by omission.

## Where the audience comes from

```
agent (MCP)                     backend                          Workspace
──────────                      ───────                          ─────────
report(audience_email=…,   ──►  roster check                     agent page
       execution_id=…)          db.email_has_agent_access        (my deliverables)
                                  ├─ not a client → 400 (named)
                                  └─ unreadable   → 503                │
                                                                      │
                                execution → session                   ▼
                                resolve_and_validate_execution   chat card
                                + ent#286 in-flight marker       (this session)
                                        │
                                        ▼
                                agent_reports.addressed_to_email
                                agent_reports.portal_session_id
```

Two rules make the columns trustworthy:

- **The address is checked against the publishing agent's own roster**, using
  the same predicate the #848 inline-auth path gates on. An agent may hand a
  report to someone it already talks to and to nobody else. It is a validated
  column rather than a key in `payload`, for the ent#364 reason: `payload` is
  agent-authored, so an audience buried there would let a prompt-injected agent
  choose whose Workspace its output lands in.
- **The chat is resolved server-side.** The agent passes an `execution_id`; the
  backend confirms that execution belongs to it (`resolve_and_validate_execution`
  — the MEM-001 rule) and reads the session from the ent#286 reverse marker,
  which maps execution → portal session for the duration of a turn. The agent
  never names a conversation, so it cannot post a card into one it was not part
  of. No marker (a scheduled run, an expired turn) ⇒ NULL, and the deliverable
  simply lists on the agent page with no card.

## What a client sees

| Surface | Shows |
|---|---|
| Agent page → Reports | Deliverables **addressed to this reader** by this agent |
| Chat | Deliverables produced **in this chat** (`?session_id=`), as cards at the end of the thread |
| Operator surfaces | Unchanged — Agent Detail and the fleet view still show everything |

Cards render through the shared `components/reports/` dispatch — the issue's
Technical Notes say not to build a second rendering layer — with the #2162
client rule: `:fallback-component="ReportSummary"`, so an unknown payload shape
degrades to a bounded, humanised summary and never a raw JSON dump at a client.

The list re-reads after a **turn**, not on a timer: a turn is the only thing
that can produce a deliverable in a chat, so a conversation nobody is talking in
costs nothing.

## Deliberate behaviour change

Unaddressed reports (`addressed_to_email IS NULL`) no longer appear in the
Workspace at all — AC #1's "unaddressed output stays operator-only". An install
whose agents have not adopted `audience_email` will show an **empty** Workspace
Reports tab where it previously showed the agent's full history. That history
was the leak; the empty state is the correct answer until an agent addresses
something.

**Adoption is a mechanism, not a hope** (caught in review on PR #2383). The
column is nullable and every agent defaults to NULL, so the surface shipped
inert until the fleet-wide platform prompt taught the argument:
`services/platform_prompt_service.py`'s "Publishing Reports" block documents
`audience_email` and when to use it, which is the same reason #1535 put
reporting itself in the platform prompt rather than in each template — a
fleet-wide default instead of a per-template opt-in. The block is CI-pinned
(`tests/unit/test_1535_report_prompt_guidance.py`), including a test asserting
the argument stays documented AND that the name matches what the MCP tool
accepts; the block's character budget was raised 2000 -> 2400 as an explicit
decision recorded there.

Who a report was for is answerable on the operator surfaces too: `addressed_to`
rides the access-controlled REST list and detail responses — and deliberately
NOT the `/ws` broadcast, which is SCOPE_ALL and unfiltered (the #918 rule). A
badge rendering it on the Agent Detail Reports tab is a follow-up; the data is
there.

## Files

| File | Role |
|------|------|
| `db/schema.py`, `db/tables.py`, `db/migrations.py`, `migrations/versions/0046_report_audience.py` | The two nullable columns + the two indexes, on both tracks |
| `db/reports.py` | `get_reports_for_client` / `get_report_for_client` — the Workspace question, kept apart from the operator one |
| `routers/reports.py` | Roster check on the audience, `_resolve_portal_session` for the chat |
| `client_portal/agent_page.py`, `client_portal/router.py` | The scoped list + detail, and the `?session_id=` narrowing |
| `client_portal/service.py` | `get_inflight_session_for_execution` — the ent#286 marker read for its value |
| `src/mcp-server/src/tools/reports.ts` | `audience_email` + `execution_id` on the `report` tool |
| `components/portal/PortalDeliverables.vue` | The cards, mounted at the end of the thread |
| `tests/unit/test_ent365_report_audience.py`, `src/frontend/tests/unit/portalDeliverables.spec.js` | The rules |

## Known gaps, stated

- **Ratings (AC #6)** — trinity-enterprise#366 is not built. The card is the
  surface it will attach to; nothing here pre-empts its shape.
- **Files (AC #1's other half)** — file scoping is unchanged, as the issue
  directs: the per-agent inbox boundary stays where it is, because that boundary
  is where the last two portal security bugs lived. Shared files are therefore
  not yet addressable, and remain listed per agent.
- The chat card list is per session; a deliverable published after its turn's
  marker expires lands on the agent page only.
