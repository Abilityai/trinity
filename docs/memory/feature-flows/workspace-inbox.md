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
    capture {path, params, query} BEFORE the first await
    … roster, threads, asks …
    inboxLandingTarget(captured) === '/workspace/inbox'  → await router.replace(...)   [inside try]
    finally: bootstrapResolved = true                     → no conversation frame on bare /workspace
  Explicit targets never redirect: /c/:id, /r/:id, /a/:agent, ?agent=, ?new=1, ?voice=1

/workspace/inbox  (route WorkspaceInbox, the same Portal.vue shell)
  stage branch: isInboxRoute && stage.state === 'ready'   (after rooms, before the conversation)
    failed / empty roster → falls through to the existing bare-stage copy
  PortalInbox  (props: sidebarThreads, previews, asks, openAsks, honesty flags)
    OverflowTabs  Action (needs) · Unread (came) · All          ?tab=&item= via router.replace
    PortalInboxList   bounded, stated total, rows are <button>s
    PortalInboxPane
      ask   → <PortalAsks :ask-ids="[id]" testid-prefix="inbox-ask"> → store.answerAsk → asks/router
      chat  → store.fetchHistory(agent, id, {limit: 50})  render from first_unread_message_id
              store.fetchSessionDeliverables → ReportRenderer (+ ReportSummary fallback)
              markRead('thread', id)           (existing #557 cursor; row stays selected, drawn read)
              Open in chat → /workspace/c/:id?anchor=m:<first_unread> | d:<report>
              Reply in chat → the same, composer focused (A2: no composer in the pane)
    Mark all read (ghost) → Promise.allSettled(markChatReadStrict) → InlineError "N failed"
  inboxSelection ref → activeAgent (never activeAgentName) → the rail follows the selection

Counts (one projection)
  Needs me  = store.askCount                    (= openAsks.length)
  Came back = totalUnread(sidebarThreads)       (= the sidebar's own sum = the tab title's)
  pinned "Inbox" row in PortalSidebar carries both (sidebar-ask-count, sidebar-unread-count)

GET /api/enterprise/client-portal/chat-state?previews=true      (only while the Inbox is mounted)
  chat_previews.get_chat_state_with_previews(email, is_platform)
    db.unread_arrivals_with_latest(email)       ONE statement over _UNREAD_ARRIVALS
      arm (i)  assistant messages in my chats, after the cursor / baseline
      arm (ii) agent_reports addressed_to_email = me, stamped to a session I own, after the cursor
      → {sid: (n, latest, first_unread_message_id)}
    service.get_chat_state(email, unread={sid: n})   same counts, same instant
    attach latest{kind,id,at,excerpt,outcome,title?,display_hint?} for roster agents, ≤100

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

Filled at implementation (see the commit series on `AndriiPasternak31/ent610`).

## Tests

Filled at implementation.
