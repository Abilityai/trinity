# Feature: The open Workspace thread catches up on what the agent wrote later

> **Status**: ✅ Implemented (2026-10-26)
> **Issue**: abilityai/trinity#3439 (also closes the display half of #3709 and #2319)
> **Related**: [channel-completion-report.md](channel-completion-report.md) (the writer this was reported against), [workspace-stick-to-bottom.md](workspace-stick-to-bottom.md) (an arrival never moves a reader who is holding the transcript), [workspace-sidebar-ia.md](workspace-sidebar-ia.md) (the 20 s tick and the unread badge, ent#557), [operating-room.md](operating-room.md) (the ent#747 ask-result poll that now rides the same read)

## Overview

You ask an agent for something, it answers, and you leave the page. It keeps
working. Later it writes more into the same thread — a completion report, an
approval's outcome, a recovered lost turn, a background task's reply — and the
sidebar badges that chat. You open it, or you were sitting on it already, and
the transcript has not changed. It appears only after a reload or a switch to
another chat.

The reason was structural, not a race. `PortalConversation.vue` loaded history
on mount and on an agent/thread change, and nothing else: there was no history
poll, and the shell's one interval — the 20 s tick in `views/Portal.vue`
(`startAsksPoll`) — refreshed asks, tags and the **thread list** (every row's
`unread`, `last_message_at`, `message_count`) for the sidebar, never the open
transcript. So the badge on the chat you were reading was right and the chat
itself was stale, which is the contradiction the operator reported.

## The design

No new timer, no new transport, no new prop, no backend change.

The shell already hands the conversation its thread list (`:threads`), and the
tick re-stamps it. The conversation watches its **own row** in that list and,
when the row moves, reads the thread's newest rows and appends what it has not
seen. Four decisions carry it, each one a trap a reviewer found before it was
written:

1. **The watcher compares a string, not the row.** `decorate()` returns new
   objects every tick (stars, unread, drafts). A watcher over the object — or
   over a tuple, which Vue compares by identity — would read the tail every 20 s
   on every open chat. `tailSignature(row)` is `` `${message_count}|${last_message_at}` ``.
2. **The signal is `message_count` + `last_message_at`, not `unread`.** `unread`
   counts assistant rows past the read cursor (`count_unread_by_session`), so a
   system row (an ask's discussion notice, a Main-reset notice) never moves it,
   and a report that landed mid-turn is read past by the turn-end mark-read.
   Both DO move `last_message_at` (`touch_portal_session`, which every portal
   writer calls). `unread` still sizes the read: `limit = unread + 8`, capped at
   the server's 50 (`router.py`, `le=50` — a larger value is a 422).
3. **The person's own question is not shown twice.** `send()` pushes a user row
   with no id (`local: true`); `deliver()` emits `sessions-changed`; the shell
   refreshes the list; the row has moved. The first read after their own turn
   returns the server's copy of that question with an id. `adoptLocalIds` gives
   the id (and the server time) to the local row in place — the bubble keeps its
   identity — so `freshRows` leaves it alone.
4. **An arrival, never an intent.** The appended rows go through
   `onMessagesArrived(n)` (#2624): the transcript follows if the reader was at
   the bottom, otherwise "jump to latest" counts them. A bounded read that is
   entirely unseen (every one of 50 rows new) re-reads the **window** and merges
   it the same way — never `loadThread`, which empties and pins the viewport.

Then the conversation emits **`thread-caught-up`** with the thread id — a
distinct event from `sessions-changed`, because `onConversationTurnDone` also
clears and re-arms the #2579 title-settle cycle and re-reads the list, none of
which an agent-initiated row should drive. The shell answers with
`markRead('thread', sid)` **only if that thread is the one on screen**
(`shouldMarkTurnRead`, the same question a finished turn asks): the badge on
the chat you are sitting in clears, and a chat you have left keeps its badge.

**Skipped while a turn or a call is in flight, re-evaluated when it ends.**
`sending`, `voiceCallActive` and `loadingHistory` are watch sources beside the
signature, and the reconciled signature is recorded only when a read actually
ran — so a tick that landed mid-turn fires the moment `sending` flips, rather
than being skipped and never re-fired. A turn's own path delivers its reply; a
call's spoken rows are folded once at its end.

**A moved time with an unmoved count is a deliverable.** `report_service`
touches the session with `added=0` — a card, not a row. The read finds nothing
fresh; the conversation bumps `deliverableTick` instead, so the card list
re-reads on this cue the way it does after the person's own turn (ent#365).

**Coming back to the tab catches up at once.** The interval skips hidden tabs
(a row, not an event — nothing is lost). Since #3439 `startAsksPoll` also runs
the tick body on `visibilitychange` → visible, so the reported gesture — "I come
back to the page" — costs one round trip, not up to 20 s of a transcript the
badge already contradicts. Removed in `stopAsksPoll`.

**One fetch-dedupe-append.** `reconcileTail(sid, { limit })` is the function;
the ent#747 ask-result poll (5 s, 5 min, only after answering an ask) now calls
it too — a faster cadence over the same read, stopping once a `completion:` row
has landed — instead of keeping its own copy.

## Files

| File | Role |
|---|---|
| `src/frontend/src/components/portal/portalThreadTail.js` | The pure half: `tailSignature`, `tailLimit`, `adoptLocalIds`, `freshRows`, `tailOverflowed`, `TAIL_LIMIT_CAP` |
| `src/frontend/src/components/portal/PortalConversation.vue` | `historyRow` (the one mapper for a persisted row, shared by the load and the catch-up), `reconcileTail`, the signature watcher, `thread-caught-up`; `checkAskResult` rides `reconcileTail` |
| `src/frontend/src/views/Portal.vue` | `onThreadCaughtUp` (mark-read iff still open), `onTabVisible` on `visibilitychange` |
| `src/backend/services/channel_completion_report.py` | `_resolve_portal` docstring stops describing the gap |

## Tests

- `src/frontend/tests/unit/portalThreadTail.spec.js` — the pure helpers (signature stability across re-stamped objects, cap, id adoption, overflow).
- `src/frontend/tests/unit/portalOpenThreadRefresh.mount.spec.js` — the real component, mounted: a moved row reads `{limit: 9}` and appends once; a re-stamp with the same values does not read; nothing-new appends nothing and emits nothing; the local question takes the server id; an all-fresh read at the cap merges the window; another thread's row is ignored; a failed read is swallowed and is never `thread-missing`.
- `src/frontend/tests/unit/portalThreadCaughtUpShell.mount.spec.js` — the real shell, mounted: `thread-caught-up` marks the open thread read once and re-reads nothing else, leaves another thread unread; `visibilitychange` → visible runs the tick body, hidden does nothing, and the listener dies with the view.

Red before the change: the four behavioural assertions in the component spec failed on "`fetchHistory` never called with a limit" and "the local row has no id"; the shell spec failed on "`markChatReadStrict` not called" and "`fetchAllSessions` not re-called".

## Known limitations

- Appended rows are not re-paired by `pairRepliesWithQuestions` (#3166); a reload reorders two overlapping turns. Arrival order is honest for live rows.
- Rooms (`PortalRoom.vue`) are out of scope: they already poll on a `since` cursor every 3 s.
- The ask-only arm of `_UNREAD_ARRIVALS` (a report addressed to the viewer, no row) moves `unread` but neither `message_count` nor `last_message_at`; the badge is right and there is nothing to append, so the conversation does not read.
