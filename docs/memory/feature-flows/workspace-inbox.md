# Workspace Inbox — what needs you and what came back, across your agents (trinity-enterprise#610)

## Overview

The Workspace opens on an **Inbox**. Three tabs answer two questions across every agent
on the viewer's roster: **Action** (asks addressed to you, waiting), **Unread** (chats
where something came back since you last read them — replies, completed runs delivered
to you, deliverables addressed to you) and **All** (the last 30 days, read or not, plus
asks that ended in the last 7). A reading pane opens an item without leaving the Inbox:
an ask is answered in place; a chat shows its new arrivals and deliverables, and is
marked read.

It is four windows over rows the Workspace already owns — no table, no store, no router.

Requirement: `docs/memory/requirements/core-agent.md` §5.40. Asks honesty (PR A0):
`requirements/security.md` §26.8. Journeys: J05, J11.

## Flow

```
Sign-in / reload on bare /workspace
  Portal.vue bootstrap()
    landing = inboxLandingTarget({path, params, query})   read BEFORE the first await
    try: if (landing) await router.replace(landing)       the FIRST await, inside the try
         … fetchRoster, refreshThreads, asks poll …
    finally: bootstrapResolved = true                     → no conversation frame on bare /workspace
  Explicit targets never redirect: /c/:id, /r/:id, /a/:agent, ?agent=, ?new=1, ?voice=1

/workspace/inbox  (route WorkspaceInbox, the same Portal.vue shell)
  stage branch: isInboxRoute && stage.state === 'ready'   (after rooms, before the conversation)
    failed / empty roster → falls through to the existing bare-stage copy
  PortalInbox  (props: threads=sidebarThreads, previews, threadsLoaded, threadsFailed, labels, isPlatform;
                asks/openAsks/asksLoaded/asksFailed/asksAbsent read from the clientPortal store)
    OverflowTabs  Action (needs) · Unread (came) · All          ?tab=&item= via router.replace
      desktop: the tab's first row is auto-selected — NOT a read; phone: nothing selected
    PortalInboxList   bounded, stated total, rows are <button>s; outcome pill done / failed (icon + label)
    PortalInboxPane
      ask   → <PortalAsks :ask-ids="[id]" testid-prefix="inbox-ask"> → store.answerAsk → asks/router
      chat  → store.fetchHistory(agent, id, {limit: 50})  render from first_unread_message_id
              store.fetchSessionDeliverablesStrict → ReportRenderer (+ ReportSummary fallback)
                                               a failed read is LoadFailed, never "no deliverables"
              "N earlier arrivals — Open in chat" when the first unread is outside the 50
              markRead('thread', id)           explicit open only (existing #557 cursor; row stays
                                               selected, drawn read in place)
              Open in chat → /workspace/c/:id?anchor=m:<first_unread> | d:<report>
                             → PortalConversation + composables/useConversationAnchor.js
                               (data-message-id / data-report-id, useStickToBottom.detach(),
                                not found → bottom + "That message is further up"; key stripped)
              Reply in chat → the same, then focusConversationComposer() (A2: no composer in the pane)
              Open canvas  → NOT built (deferred: no cheap per-agent "has a visible canvas" fact)
    Mark all read (ghost) → Promise.allSettled(markChatReadStrict) → InlineError "N failed"
  inboxSelection (computed from ?item=) → activeAgent (never activeAgentName) → the rail follows it

Counts (one projection)
  Needs me  = store.askCount                    (= openAsks.length)
  Came back = totalUnread(sidebarThreads)       (= the sidebar's own sum = the tab title's)
  pinned PortalInboxRow in PortalSidebar carries both (sidebar-ask-count, sidebar-unread-count),
  white on a 700 ground, tabular-nums

GET /api/enterprise/client-portal/chat-state?previews=true      (only while the Inbox is mounted)
  chat_previews.get_chat_state_with_previews(email, is_platform)
    db.unread_arrivals_with_latest(email)       ONE statement over _UNREAD_ARRIVALS
      arm (i)  assistant messages in my chats, after the cursor / baseline
      arm (ii) agent_reports addressed_to_email = me, stamped to a session I own, after the cursor
      → {sid: (n, latest, first_unread_message_id)}
    service.get_chat_state(email, unread={sid: n})   same counts, same instant
    attach latest{kind,id,at,excerpt,outcome,title?,display_hint?} for roster agents, ≤100
      excerpt: credential-sanitised, markdown-stripped, ≤160; a deliverable's excerpt = its title
      response_model_exclude_none → no preview = keys absent; roster read failure → raises (fail loud)

Where arrivals come from
  a turn's reply / an agent-started message         → enterprise_portal_messages (role=assistant)
  a delivered run (portal turn, delivery schedule)  → channel_completion_report → source="completion:done|failed"
  an addressed report                               → report_service.resolve_report_session
                                                       addressee's in-flight chat, else their Main (+ touch, added=0)
```

## Why it is shaped this way

- **One unread model.** The unit of Unread is a chat and "read" is the existing #557
  cursor. Per-item read rows would pile up against `MAX_CHAT_STATE_ROWS` (where
  `mark_chat_read` silently no-ops) and would be a second cursor the agent row must
  also count.
- **Runs need no source of their own.** Every terminal of a portal-stamped background run
  already arrives as an assistant message ("**Finished**" / "**Didn't finish**"). The
  Inbox reads no executions. Only runs **addressed into the Workspace** arrive (A8).
- **The outcome pill is a platform marker.** `source="completion:*"` is written by the
  completion writer from the same status that picks its wording. Parsing the body would
  make the pill agent-writable.
- **Counts and previews from one statement**, so the number on a row and its excerpt can
  never disagree mid-poll.
- **Sidebar parity by construction.** The Inbox reads `sidebarThreads`, the projection the
  sidebar sums, and the report stamp touches Main so no thread with an arrival is hidden
  by that projection.
- **Honest states.** Asks and threads each carry loaded/failed verdicts; the empty copy is
  shown only after a successful read, a failed first read shows a retry, and a failed
  refresh keeps the list with a stale banner.

## Known properties

- A viewer who has never read anything has no baseline, so nothing counts for them (#557).
- An owner is never the addressee of an ask or a report (`include_owned=False`), so their
  Action is empty and its empty state links to Operations (A9).
- Archived chats stay in Unread.
- Rooms are not in Unread until PR C.

## Files

| Layer | File | Role |
|---|---|---|
| Backend | `src/backend/client_portal/db.py` | `_UNREAD_ARRIVALS` (two arms), `count_unread_by_session`, `unread_arrivals_with_latest` |
| Backend | `src/backend/client_portal/chat_previews.py` | `get_chat_state_with_previews`, `_arrival_excerpt`, `_outcome`; `MAX_PREVIEWS = 100` |
| Backend | `src/backend/client_portal/service.py` | `get_chat_state(email, unread=None)` — the optional precomputed map only |
| Backend | `src/backend/client_portal/router.py` | `GET /chat-state?previews=` (`response_model_exclude_none`) |
| Backend | `src/backend/client_portal/models.py` | `PortalChatArrival` (no `cost`), `PortalChatStateEntry.latest` / `first_unread_message_id` |
| Backend | `src/backend/services/channel_completion_report.py` | `source="completion:done" \| "completion:failed"` on the portal message |
| Backend | `src/backend/services/report_service.py`, `src/backend/routers/reports.py` | `resolve_report_session` — the addressee's in-flight chat, else their Main (+ touch) |
| Frontend | `src/frontend/src/router/index.js` | route `WorkspaceInbox` `/workspace/inbox` |
| Frontend | `src/frontend/src/components/portal/portalInbox.js` | every pure rule: landing, builders, counts, keys, `sidebarThreadsOf`, selection/hold |
| Frontend | `src/frontend/src/components/portal/PortalInbox.vue`, `PortalInboxList.vue`, `PortalInboxPane.vue`, `PortalInboxRow.vue` | container, list, pane, pinned sidebar row |
| Frontend | `src/frontend/src/views/Portal.vue` | stage branch, landing replace, `threadsLoaded`, `chatPreviews`, `replyInChat` |
| Frontend | `src/frontend/src/components/portal/PortalSidebar.vue`, `portalUtils.js` | pinned row mount, `WORKSPACE_INBOX`, "new" wording (`unreadBadgeTitle`) |
| Frontend | `src/frontend/src/components/portal/PortalAsks.vue` | `askIds`, `testidPrefix` |
| Frontend | `src/frontend/src/components/portal/PortalConversation.vue`, `PortalDeliverables.vue`, `composables/useConversationAnchor.js`, `composables/useStickToBottom.js` | anchors (`data-message-id`, `data-report-id`, `loaded`, `detach()`) |
| Frontend | `src/frontend/src/stores/clientPortal.js` | `fetchChatState({previews})`, `markChatReadStrict`, `fetchSessionDeliverablesStrict`, `asksAbsent` |

## Tests

| Test | Covers |
|---|---|
| `tests/unit/test_ent610_inbox.py` | D3 deliverable arm, D4 report stamp, completion `source` marker, previews (property n == count, latest iff n > 0, no `cost`, off-roster, excerpt redaction, outcome only from the marker), the router flag |
| `tests/unit/test_ent557_unread_never_opened_chat.py`, `test_ent359_portal_chat_state.py`, `test_ent365_report_audience.py`, `test_ent457_portal_completion_report.py` | updated fixtures / retargets for the arm and the stamp |
| `src/frontend/tests/unit/portalInbox.spec.js` | pure rules + the seeded count-parity property |
| `src/frontend/tests/unit/portalInboxStore.spec.js` | `fetchChatState({previews})`, `markChatReadStrict` |
| `src/frontend/tests/unit/portalInbox.mount.spec.js` | honest states, mark-read once, answer via store, Mark all read failures, phone Back + focus |
| `src/frontend/tests/unit/portalInboxShell.mount.spec.js` | the stage chain (roster error, empty roster, no `PortalConversation`), selection never writes `activeAgentName` |
| `src/frontend/tests/unit/portalSidebarInboxRow.spec.js` | the pinned row's counts and link |
| `src/frontend/tests/unit/portalAsksTestidPrefix.mount.spec.js` | default ids unchanged, prefix over every id, disjoint id sets |
| `src/frontend/tests/unit/portalConversationAnchor.mount.spec.js`, `stickToBottom.spec.js` | `?anchor=` found / missing / one-shot, `detach()` |
| `src/frontend/e2e/workspace-inbox.spec.js` (`@smoke`) | landing, the pinned row, explicit targets still win |
| `src/frontend/e2e/workspace-rail-reserved.spec.js`, `e2e/contrast-ratchet.spec.js` | retargeted to `?agent=` / `?new=1`; `/workspace/inbox` held at zero contrast failures |
