# Workspace Inbox — what needs you and what came back, across your agents (trinity-enterprise#610)

## Overview

The Workspace opens on an **Inbox**. Three tabs answer two questions across every agent
on the viewer's roster: **Action** (asks addressed to you, waiting), **Unread** (chats
where something came back since you last read them — replies, completed runs delivered
to you, deliverables addressed to you) and **All** (every chat of any age, read or not,
plus pending asks and asks that ended in the last 7 days — §3g D-4). A reading pane opens an item without leaving the Inbox:
an ask is answered in place; a chat shows its new arrivals and deliverables, and is
marked read.

It is four windows over rows the Workspace already owns — no table, no store, no router.

Requirement: `docs/memory/requirements/core-agent.md` §5.40. Asks honesty (PR A0):
`requirements/security.md` §26.8. Journeys: J05, J11.

## User Story

As a Workspace user (an external client with a portal token, or a platform user), I want
one landing view that shows what my agents need from me and what came back since I last
looked, across every agent, so I can act on it without opening each agent's chat.

## Entry Points

- **UI:** sign-in or reload on bare `/workspace` → `/workspace/inbox` (route `WorkspaceInbox`,
  `src/frontend/src/router/index.js:263`); the brand mark and the pinned sidebar row
  (`PortalInboxRow.vue`) link there.
- **API:** `GET /api/enterprise/client-portal/chat-state?previews=true`
  (`src/backend/client_portal/router.py:687`); the existing `GET /client-portal/asks`,
  `POST /client-portal/asks/{id}/answer`, `POST /chat-state/thread/{id}/read`,
  `GET …/agents/{name}/history` and the deliverables reads. No new endpoint.

## Frontend Layer

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
                  tablist-label="Inbox" (role=tablist, manual activation — §3g B6a); each
                  counter caps at 99+ (utils/tabTitle.js::capCount, the one cap every
                  Workspace counter uses) and the tab is NAMED with the full number
                  (badgeLabel from askBadgeTitle / unreadBadgeTitle — §3g A8c)
                  counters solid: needs = white on status-urgent-700, new = white on
                  action-primary-700 — the same two on the pinned row, the agent pills
                  (agent-ask-count / agent-unread-count), the chat rows (§3g A3b)
      layout (§3g A4): inboxLayout({width: useContainerWidth(root), allowance: Portal's
               inboxRailAllowance — the rail width its column has not grown into yet
               (target − measured; the rail enters from 0) —; an open rail with nothing
               selected is PortalRailPlaceholder (collapse control, open width),
               phoneViewport, prev}) → split ≥ 720 (16px hysteresis; w-96 list ≥ 1100) |
               stacked (list → pane + Back; no preview); data-layout on the root; a flip
               keeps an opened item and focuses its heading (stacked) / row (split)
      phone (§3g A5 / F4): an sm:hidden 44px Menu button (inbox-menu) emits open-menu →
               Portal's mobileNav = true; watch(route.fullPath) closes the drawer on ANY
               navigation (the pinned Inbox row is a router-link and left it open)
      history (§3g A6): stacked, an open PUSHES (the hardware Back returns to the list, focus on
               the row, via a routeItem watcher); split, it REPLACES. The pane's Back pops the
               entry our open pushed, else replaces the item away (a deep link never walks out)
      split: the tab's first row is a local PREVIEW — not ?item=, NOT a read, emitted as
               update:preview (§3g S5, T2); stacked: nothing selected
    stableRows(fresh, visit)   rows keep their place for one TAB VISIT (§3g S1): a row that leaves
                               stays as a ghost (chat drawn read, ask drawn ended); a poll never
                               re-sorts; a new row goes in before its nearest fresh neighbour; a
                               deleted chat drops. New visit = tab change, re-click, bulk read.
    selectedItem = the row, else resolveItem(key, threads/asks/previews) — never "Pick something"
                   for an old chat; on a phone a key that resolves to nothing goes Back
    PortalInboxList   bounded; the head counts LIVE rows in units — listHeadLabel: "21 asks",
                      "15 chats · 70 new", "3 chats · 2 asks", "All caught up" (§3g A8 / D-1);
                      rows are <button>s; outcome pill done / failed (icon + label)
                      every tab pages (§3g SM / C4, pageWindow): 50 rows, then "Showing 50 of
                      212" (tabular-nums) + a secondary "Show more" (+50, focus to the first new
                      row); the limit resets on a tab change, never on a poll; a selected row past
                      the window widens it (a ?item= at row 72 shows 72). Not virtualisation.
                      All's footer (§3g D-4b, allFooterNotes): "Answered, expired and cancelled
                      asks drop off after 7 days."; "Showing your 200 most recent asks." when the
                      asks read hit its cap (T16; server total is #3059); "Rooms aren't in the
                      Inbox yet…" only to a viewer with rooms. Empty All: "No chats or asks yet"
                      + a New chat link (/workspace?new=1)
                      ask rows (§3g A10, portalAskUrgency.js): kind by SHAPE in gray
                      (shield-check / question-mark-circle / bell, kind sr-only); no
                      "Waiting on you"; Critical (danger) / High (urgent) only; "Expires
                      in 18m" (warning, < 1h) / "Expires in 5h" (neutral, 1–24h), absolute
                      + zone on hover; ≤ 2 badges; a 30 s clock only while such a row exists
    PortalInboxPane
      ask   → <PortalAsks :ask-ids="[id]" testid-prefix="inbox-ask"> → store.answerAsk → asks/router
      chat  → store.fetchHistory(agent, id, {limit: 50})  render from first_unread_message_id
              store.fetchSessionDeliverablesStrict → ReportRenderer (+ ReportSummary fallback)
                                               a failed read is LoadFailed, never "no deliverables"
              grouped by paneRuns (§3g A13): one 12.5px header per run of one sender (a
              system line, or a gap > 10 min, starts a new run), relative time with the
              absolute on hover; the chat's own bubbles (user accent bubble; PortalAvatar +
              PortalAgentBubble); system lines in meta ink; body capped at --ws-message-max
              at most PANE_TAIL (5) messages from there (§3g S2); the hidden ARRIVALS are said:
              "N earlier arrivals — Open in chat" (also when the first unread is outside the 50)
              markRead('thread', id)           a function prop; called when readIntent (a click, or
                                               the initial ?item=) === the pane's rendered(key):
                                               history + deliverables + every payload settled OK
                                               (§3g S5). A failed payload leaves it unread; a false
                                               result is inbox-pane-read-error; "Mark read" (n>0)
                                               reads on demand. The row stays, drawn read, for
                                               the tab visit
              header (§3g L5), never wrapping: split [title flex-1 truncate] … [Mark read]
                [Reply][Open in chat] — volatile actions leftmost; stacked [Back][title] …
                [Open in chat][More ▾ → Mark read · Reply] (a disclosure: Esc returns focus),
                and the Inbox's title, subtitle and tabs are hidden over the pane
              Open in chat → /workspace/c/:id?anchor=m:<first_unread> | d:<report>
                             → PortalConversation + composables/useConversationAnchor.js
                               (data-message-id / data-report-id, useStickToBottom.detach(),
                                not found → bottom + "That message is further up"; key stripped)
              Reply in chat → the same, then focusConversationComposer() (A2: no composer in the pane)
              Open canvas  → shown when inboxCanvasCount({tabs: railTabs, canvases: the rail feed
                             store, agent}) > 0 — on chats AND asks (T6); split: leftmost of
                             the actions; stacked: in More → open-canvas → openRailOn('canvas')
                             (§3g C10)
    Mark N chats read (ghost; Unread + All only — §3g A9) → k > 1: ConfirmDialog (variant info,
      confirm-variant primary, Cancel focused, "M new messages across N chats will be marked read. You can't undo
      this.") | k = 1: direct → Promise.allSettled(props.markRead per chat — S4 rollback each)
      → all true: toast "Marked N chats read" + a new tab visit | any false: InlineError "N failed"
  inboxSelection (?item= || the Inbox's preview) → activeAgent (never activeAgentName) → the rail follows it

Counts (one projection)
  Needs me  = store.askCount                    (= openAsks.length)
  Came back = totalUnread(sidebarThreads)       (= the sidebar's own sum = the tab title's)
  pinned PortalInboxRow in PortalSidebar carries both (sidebar-ask-count, sidebar-unread-count),
  white on a 700 ground, tabular-nums, capped by capCount; the row is NAMED once —
  inboxRowLabel({needs, came}) → "Inbox, 2 asks are waiting on your answer, 5 new you haven't
  read" — and both badges are aria-hidden (§3g B6b)
```

Key sites: `Portal.vue:2224` (landing target read before the first await),
`Portal.vue:929` (`inboxBranchVisible`), `portalInbox.js:52` (`inboxLandingTarget`),
`portalInbox.js:169` (`inboxCounts`), `stores/clientPortal.js:1468` (`fetchChatState`),
`stores/clientPortal.js:1511` (`markChatReadStrict`).

## Backend Layer

```
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

Key sites: `client_portal/db.py:1131` (`_UNREAD_ARRIVALS`), `client_portal/db.py:1224`
(`unread_arrivals_with_latest`), `client_portal/chat_previews.py:114`
(`get_chat_state_with_previews`), `chat_previews.py:62` (`_arrival_excerpt`),
`services/report_service.py:83` (`resolve_report_session`), `report_service.py:147`
(`touch_report_session`, called after the insert at `routers/reports.py:255`).

Database operations: reads only on the Inbox path (`enterprise_portal_messages`,
`agent_reports`, `enterprise_portal_sessions`, `enterprise_portal_chat_state`). No new
table, no migration. The report publish may mint the addressee's Main
(`ensure_main_session`, race-safe by the partial unique index) and touch it with `added=0`.

## Side Effects

- Opening a chat row advances the existing #557 read cursor (`POST /chat-state/thread/{id}/read`);
  Mark all read does the same per unread chat.
- Answering an ask in the pane goes through the one end sink (`asks/router` → `ask_service`),
  which wakes the parked agent exactly as an answer anywhere else does.
- The agent's own addressed report publish may mint and touch the addressee's Main, so the
  sidebar, the tab title and the Inbox count it. No WebSocket event is added.

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
  make the pill agent-writable. The marker also takes the row out of the typed window, so
  the agent's next turn is told `[Background task report: …]` (resumed or cold), and the
  row is never the resumed-turn cursor.
- **Counts and previews from one statement**, so the number on a row and its excerpt can
  never disagree mid-poll.
- **Sidebar parity by construction.** The Inbox reads `sidebarThreads`, the projection the
  sidebar sums, and the report stamp touches Main so no thread with an arrival is hidden
  by that projection.
- **Honest states.** Asks and threads each carry loaded/failed verdicts; the empty copy is
  shown only after a successful read, a failed first read shows a retry, and a failed
  refresh keeps the list with a stale banner.

## Error Handling

| Case | Behaviour |
|---|---|
| Asks read 5xx / network, no data yet | Action shows `LoadFailed` with a retry |
| Asks read fails after a good load | list kept, stale banner (`staleBannerMessage`) |
| Asks read fails, on All (§3g S3 / A11) | All waits on the chats only: its chats render, and `inbox-asks-stale` sits above them — "Couldn't load your asks — the chats below are current." with no ask data yet, the stale-refresh line otherwise. Never `LoadFailed` over chats that loaded |
| Asks 404/403 (not served) | `asksAbsent` counts as a verdict, so Action resolves to its empty state instead of a skeleton forever |
| Sessions or previews read fails, no data yet | Unread/All `LoadFailed` |
| Sessions or previews read fails after a good load | list kept, stale banner |
| Roster unreadable during `?previews=true` | 5xx (fail loud), never a 200 with previews dropped |
| Deliverables read fails in the pane | `LoadFailed` in the pane, never "no deliverables" |
| Mark all read partly fails | `InlineError` naming the failed count; those rows keep "N new" |
| A single read write fails (any of `Portal.vue::markRead`'s 7 callers) | the optimistic zero is rolled back — only while the entry is still the one that call wrote (`portalUtils.optimisticRead` / `rollbackRead`, compared via `toRaw`) — and the call resolves `false`; it never rejects (§3g S4) |
| First unread message outside the 50-message window | "N earlier arrivals — Open in chat" |
| `?anchor=` target not found | bottom of the chat + "That message is further up"; key stripped |
| Roster error / empty roster on `/workspace/inbox` | the existing bare-stage copy, not the Inbox |

## Security Considerations

- **Scoping.** Both arms read only the caller's own rows (`m.client_email = :email`;
  `s.client_email = :email AND r.addressed_to_email = :email`); previews attach only for
  agents on `roster_agent_names(email, include_owned=is_platform)`.
- **No cost, no execution id** in any new projection (`PortalChatArrival` has neither;
  pinned by a test that walks the payload). The pane renders neither.
- **Excerpts** are credential-sanitised before markdown stripping and rendered by text
  interpolation; pane messages go through `PortalMarkdown` (DOMPurify).
- **Report stamp.** Only the agent's own publish (`current_user.agent_name == name`) may
  be placed at all (the addressee's in-flight chat, else their Main); a human sharer's
  publish is `NULL`, even when it quotes the addressee's live turn, so a sharer cannot push a badge into another person's Inbox. The
  addressee is roster-validated (`include_owned=False`) and the report route is rate-limited.
- **URL params.** `?tab=` is allowlisted; `?item=` resolves only against the viewer's loaded
  items; `?anchor=` compares `dataset` values in JS, never builds a selector.
- Asks keep the uniform 404 of `answer_ask` (Invariant #8); no new per-item endpoint.

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
| Backend | `src/backend/services/report_service.py`, `src/backend/routers/reports.py` | `resolve_report_session` — the addressee's in-flight chat, else (agent's own publish only) their Main; `touch_report_session` after the insert |
| Frontend | `src/frontend/src/router/index.js` | route `WorkspaceInbox` `/workspace/inbox` |
| Frontend | `src/frontend/src/components/portal/portalInbox.js` | every pure rule: landing, builders, counts, keys, `sidebarThreadsOf`, selection/hold |
| Frontend | `src/frontend/src/components/portal/PortalInbox.vue`, `PortalInboxList.vue`, `PortalInboxPane.vue`, `PortalInboxRow.vue` | container, list, pane, pinned sidebar row |
| Frontend | `src/frontend/src/views/Portal.vue` | stage branch, landing replace, `threadsLoaded`, `chatPreviews`, `replyInChat` |
| Frontend | `src/frontend/src/components/portal/PortalSidebar.vue`, `portalUtils.js` | pinned row mount, `WORKSPACE_INBOX`, "new" wording (`unreadBadgeTitle`) |
| Frontend | `src/frontend/src/components/portal/PortalAsks.vue` | `askIds`, `testidPrefix` |
| Frontend | `src/frontend/src/components/portal/PortalConversation.vue`, `PortalDeliverables.vue`, `composables/useConversationAnchor.js`, `composables/useStickToBottom.js` | anchors (`data-message-id`, `data-report-id`, `loaded`, `detach()`) |
| Frontend | `src/frontend/src/stores/clientPortal.js` | `fetchChatState({previews})`, `markChatReadStrict`, `fetchSessionDeliverablesStrict`, `asksAbsent` |

## Testing

**Prerequisites:** backend unit env (real SQLite); `src/frontend` with `npm ci`; for e2e a
live stack with an admin whose email is on at least one agent's roster.

**Test steps (manual):** sign in → lands on `/workspace/inbox`; the pinned row's two counts
equal the agent rows; answer an ask in the pane → it stays in place drawn ended until you leave the
tab, then shows in All (where it does not move); open an Unread chat → the pane shows the arrivals, the badge clears in the sidebar
too; a deliverable renders through `ReportRenderer`; `?agent=X` and `?new=1` still win;
375 px and both themes.

**Edge cases:** first-ever viewer (no baseline, nothing counts); archived chat with arrivals;
an ask that ends while selected; a deep-linked `?item=`; previews read failing mid-session.

**Status:** ✅ unit + mount + property tests green; verified in a real browser (both themes,
1280/375) and on a live stack without an agent container. ⚠️ No real agent turn exercised
(no LLM key in verification).

| Test | Covers |
|---|---|
| `tests/unit/test_ent610_inbox.py` | D3 deliverable arm, D4 report stamp, completion `source` marker, previews (property n == count, latest iff n > 0, no `cost`, off-roster, excerpt redaction, outcome only from the marker), the router flag |
| `tests/unit/test_ent557_unread_never_opened_chat.py`, `test_ent359_portal_chat_state.py`, `test_ent365_report_audience.py`, `test_ent457_portal_completion_report.py` | updated fixtures / retargets for the arm and the stamp |
| `src/frontend/tests/unit/portalInbox.spec.js` | pure rules + the seeded count-parity property |
| `src/frontend/tests/unit/portalInboxStore.spec.js` | `fetchChatState({previews})`, `markChatReadStrict` |
| `src/frontend/tests/unit/portalInbox.mount.spec.js` | honest states, mark-read once, answer via store, Mark all read (Unread/All only, confirm for k>1 with Cancel focused, k=1 direct, success toast + new visit, partial failure), phone Back + focus; §3g S1 rows keep their place (A1 click-then-read, A7 answer on All, A12 poll ghosts + "All caught up"), a deleted open chat goes Back on phone; §3g S5 read after render (payloads awaited, a failed payload stays unread + Mark read, preview not in the URL, deep link reads, a false write shows the error) |
| `src/frontend/tests/unit/portalInboxShell.mount.spec.js` | the stage chain (roster error, empty roster, no `PortalConversation`), selection never writes `activeAgentName` |
| `src/frontend/tests/unit/portalSidebarInboxRow.spec.js` | the pinned row's counts and link |
| `src/frontend/tests/unit/portalAsksTestidPrefix.mount.spec.js` | default ids unchanged, prefix over every id, disjoint id sets |
| `src/frontend/tests/unit/portalConversationAnchor.mount.spec.js`, `stickToBottom.spec.js` | `?anchor=` found / missing / one-shot, `detach()` |
| `src/frontend/e2e/workspace-inbox.spec.js` (`@smoke`) | landing, the pinned row, explicit targets still win |
| `src/frontend/e2e/workspace-rail-reserved.spec.js`, `e2e/contrast-ratchet.spec.js` | retargeted to `?agent=` / `?new=1`; `/workspace/inbox` held at zero contrast failures |

## Related Flows

- Upstream: [workspace-deliverables.md](workspace-deliverables.md) (addressed reports and the
  Main stamp), [operating-room.md](operating-room.md) (the asks table the Action tab reads).
- Siblings: [workspace-sidebar-ia.md](workspace-sidebar-ia.md) (the pinned row and the
  "new" counts), [workspace-agents-at-the-centre.md](workspace-agents-at-the-centre.md)
  (the landing rule; explicit targets win).
