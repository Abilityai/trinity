# Workspace — suggestions: what you can do with this agent, and what is waiting (trinity-enterprise#465)

## Overview

A platform user who opens an agent in the Workspace gets a short list computed for
**them and that agent**: questions the agent is waiting on them for, decisions on their
seat past review, the health of the agent's schedules (owner/admin only), how long since
they last talked to it, and playbooks it exposes that they have never run. Each item
shows its evidence (`signal`) and has two verbs: **Accept** — prefill the composer, open
a section, focus the chat, or deep-link to the operator schedules tab — and **Dismiss**.
Nothing is sent on the person's behalf, and an agent with nothing to say says so.

Requirement: `docs/memory/requirements/core-agent.md` §5.39. Journey: J13 (skeleton).

## Flow

```
Portal.vue (shell)
  watch(active 1:1 agent, platform session) → store.loadAgentSuggestions(name)   [60 s reuse]
  #empty slot → PortalSuggestions compact limit=3 ─┐
  Info tab   → PortalAgentDetails → PortalSuggestions (full) ─┤ both read one store slice
  railSignals.info = {updated, note: "N suggestions"} ─────────┘ → Info's rail dot

GET /api/enterprise/client-portal/agents/{name}/suggestions
  suggestions/router._gate:  not is_platform → 404 · agent_on_roster(…, True) → 404 · rate limit 60/min/viewer
  suggestions/service.get_suggestions
    can_configure = principal.is_admin OR portal_owns_agent(email, name, True)
    asyncio.gather(
      to_thread(_gather)      → list_asks · list_seat_decisions+effective_status · list_schedules
                                · last_user_message_at · slash_texts_by_viewer · dismissed_fingerprints
                                · [owner/admin] autonomy · recent_runs_by_schedule (ROW_NUMBER ≤10) · overdue reminders
      _playbooks(name)        → _availability_map → _bounded_briefing (only if it can answer) → playbooks | None
    )
    build(signals, now)       → ranked (Suggestion, fingerprint) pairs          [PURE]
    shape(pairs, dismissed)   → hide where fingerprint == dismissed one; cap 5; total; capabilities; basis

POST …/suggestions/feedback {key, action: accept|dismiss}
  same gate → key class allowlisted (else 422) → recompute → key not emitted → 404 (nothing written)
  → db.record_feedback: dismissed_at + dismissed_fingerprint | accepted_at + accept_count+1
```

## Why it is shaped this way

- **Two kinds, per the engine-bay ruling (decision 15, option 1).** `invoke` items act
  inside the Workspace; `configure` items only deep-link to the operator page. The
  Workspace still never configures (§5.11).
- **Prefill, never send.** The ent#138 rule every hint follows. `open_chat` focuses the
  composer; `link` opens a new tab and only for an `/agents/…` path.
- **One `autonomy_held` item, not one per schedule.** Autonomy is off by default, so a
  per-schedule item would fill every slot, and nothing records when autonomy was turned
  off — "held N days" would be invented. It cites the last real run instead, and
  suppresses "never fired" (a held schedule cannot fire).
- **"Haven't run /name", not "never used".** Usage is explicit slash invocations in the
  viewer's own Workspace messages and executions attributed to them; plain-language
  requests are undetectable. A playbook an enabled schedule already runs is not offered.
- **Fingerprints are state identities, never counts.** Otherwise a failing schedule
  returns with every new failure — nagging, not "the signal materially changed".
- **Bounded, unforgeable writes.** The key travels in the body (it can carry an
  agent-authored playbook name); the server recomputes and writes only for an item it
  would show this viewer now, with its own fingerprint.
- **Info's dot, not a number.** The rail has two signal shapes in one hue (principle
  24); Info borrows `updated` and its words ("Info · 2 suggestions") come from `note`.
  The shell loads the slice itself so the dot works with the rail collapsed.
- **No LLM.** A handful of indexed queries, one windowed runs query, and at most one
  bounded briefing call (cached 60 s, skipped for a stopped agent).

## Files

| Layer | Files |
|---|---|
| Router | `src/backend/client_portal/suggestions/router.py` (registered in `main.py`) |
| Service | `src/backend/client_portal/suggestions/service.py` (`build`, `shape`, thresholds) |
| DB | `src/backend/client_portal/suggestions/db.py`; table `workspace_suggestion_feedback` in `db/schema.py`, `db/tables.py`, `db/migrations.py`, Alembic `0073_workspace_suggestion_feedback`; CASCADE in `db/agent_cleanup.py` |
| Auth | `client_portal/portal_auth.py` — `PortalPrincipal.is_admin`, `_is_admin_principal` |
| Frontend | `components/portal/PortalSuggestions.vue`, `PortalAgentDetails.vue`, `views/Portal.vue`, `stores/clientPortal.js` (suggestions slice), `components/portal/portalRail.js` (Info signal + `note`) |
| Tests | `tests/unit/test_ent465_suggestions.py`, `src/frontend/tests/unit/portalSuggestions.spec.js`, `portalRail.spec.js`, `tests/journeys/test_j13_suggestions_journey.py` |

## Deferred

- A suggestion that consults another agent (2026-09-21 amendment → ent#698) and
  suggestions derived from the current conversation (2026-09-22 → ent#699).
- Role, project and objective-gap inputs (#500, #661, #477–#479); the ent#178 curated
  skill set replaces the briefing ladder as the capability source when it lands.
- The Inbox placement (#610).
