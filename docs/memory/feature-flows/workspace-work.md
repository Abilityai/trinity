# Workspace work — the live execution card and the Work tab (trinity-enterprise#525)

> The visual half of ent#457. When a message starts a long-running job, the
> Workspace shows it happening: a **live card** under the message (status,
> elapsed, current step, the steps of a pipeline with the agent holding each,
> Stop, Open in Work) and the rail's **Work** tab (Waiting on you · Now ·
> Earlier). The report-back contract itself — every terminal posts back into
> the chat that started it — is abilityai/trinity#2386 and lives in
> `channel-completion-report.md`. The user-facing noun is **work**.
>
> Rulings this is built to (ent#457 thread, 2026-09-02 / 2026-09-06): the
> artboards as posted; an honest **Ask about it** instead of a fake restart; a
> visible **"this agent doesn't report steps"**; today's roster scope (inherit
> ent#367 later); the Work tab is the first docked tab of the ent#474 rail.

## The shape

```
Portal.vue (shell)
├─ railChatId  = the open 1:1 thread (null in a room)
├─ rail = usePortalRailFeeds({ …, chatId: railChatId, workEmit: workSignal })   ← the ONE owner
│    ├─ wants.work = feedsFor(visibleTabs)                    platform door → the feed exists at all
│    ├─ watch([visible, participantsKey, wantsKey])            work.setScope(names, chat) + refresh()
│    ├─ watch(chatKey)                                          re-scopes ONLY the Work feed
│    └─ signals.work = workSignalFromItems(work.now, { emit })  ONE set, merged by execution id
├─ onWorkState: live 0→1 → work.scheduleRefresh(1500)  (the row exists now; its children soon)
│               live 1→0 → rail.refresh()               (a turn ended)
├─ <PortalConversation>  sending → <PortalWorkCard :item="liveCardItem" …/>   @open-work → openRailOn('work')
│                        terminalCardItem → <PortalWorkCard> (from the durable verdict)  @ask-about-it → prefill
├─ <PortalRoom>          feed live rows with `chat_id` == the room (∩ server `working`) → <PortalWorkCard show-agent …/>
│                        no row for the room yet → the server-derived "X is thinking…" line (#2792)
└─ <PortalRail> #tab-work → <PortalWork>  Waiting on you (PortalAsks over store.asks) · Now · Earlier
stores/portalWork.js ──► GET /api/enterprise/client-portal/work?agents=a,b&chat_id=…
utils/websocket.js: agent_activity (started + terminal) / loop_* for a participant → portalWork (debounced 2 s)

client_portal/work/router.py   is_platform? else 404 · agents cap 422 · per-viewer limiter
client_portal/work/service.py  roster ∩ names · get_fleet_executions(running|queued|recent) · stats(30d)
                               · get_running_for_chat(chat_id) [owned by caller] · project · mask · steps
client_portal/work/pipeline_state.py   the #919 read, hardened like pipelines.ts, three-state, cached 10 s
```

## Design decisions

### The read is under the portal roster, not the fleet ACL

The obvious reuse is `GET /api/executions?agents=` — its DB query IS what this
route runs (`get_fleet_executions` / `get_fleet_execution_stats`, which gained
`source_channel`, `source_channel_chat_id` and `loop_id`). The route itself was
not reusable: it narrows through `accessible_agent_names` →
`get_accessible_agents` → `list_all_agents_fast()`, a **Docker read** that
answers `[]` on any daemon fault. The Workspace's own rule (#2196: membership
is a DB fact, container state a projection) forbids exactly that — one Docker
restart would empty every user's Work tab. `roster_agent_names` is the DB
predicate; it is also the set the Loops tab enforces for a platform user
(owned ∪ shared-by-email), so the two tabs agree on who is in the rail.

### A delegated child is found by the chat, never by the agent

A ↔ you; A asks B. B's row says `agent_name = B`, so "the participants'
executions" can never return it in a 1:1 — the one case where the participant
list is the chat's own agent. What the child DOES carry is the chat it was
started from: `source_channel_chat_id` is copied from the parent at creation
(ent#265 D0, #2386). The route takes `chat_id`, honours it only when it names
a thread **this caller** holds with a requested agent (`get_portal_session`,
the scoped read), and selects `status IN (running, queued) AND
source_channel_chat_id = ?` — `idx_executions_status` drives it, a handful of
rows on any install, so no migration. A child on an agent outside the roster
is still a step — rendered "held by another agent" — but never a name (the
ent#467 disclosure class: every name that leaves the module is roster-masked).

### A room's cards are its own, joined by the chat id (#2792)

The feed is agent-scoped in a room on purpose — `railChatId` is null there, so
the rail's Work tab shows everything the participants are doing. The room
itself used to pick its cards from that feed by **agent name** (server
`working` ∩ feed rows), which is every live execution of a working participant:
a schedule run, a loop turn, a 1:1 thread, another room, an MCP task, all
rendered as this room's work. It could not do better, because the room turn
stamped no chat: `_wake_agent` passed `triggered_by="room"` and nothing else.

Now the room turn stamps `source_channel_chat_id = room_id` — the 1:1 thread's
shape — under the room's **own** channel value, `config.ROOM_SOURCE_CHANNEL =
"room"`, and `PortalRoom.vue` selects `liveItemsForRoom(feed, roomId, working)`:
`chat_id === roomId`, still ∩ the server's `working` list (the room polls at
3 s and the feed at 12 s, so a row the server has already retired must not sit
under the posted reply), a masked agent excluded (no name to draw). With no row
for the room yet — a reload mid-turn, feed lag — the fallback is the
server-derived "thinking…" line, never unrelated cards.

Why `room` and not `portal` (the plan review's finding): every reader of
`portal` branches on "this is a 1:1 thread". Reusing it would have routed every
room terminal into `_resolve_portal` (a session lookup that can only fail), and
the voice-reply route would have told a room's **delegated child** — an `mcp`
trigger carrying the inherited stamp — that the client "hears your reply when
they switch on the speaker control", which a room does not have (the #2157
false-claim class). `room` is in neither map, so parent and child are suppressed
by construction; the projection is the one reader that accepts it (`chat_id`,
and `work_kind` classifies a room's child `delegated`, so the Work tab groups it
under its parent through `childrenForChat`).

Stated asymmetry: a room's delegated child on an agent that is **not** a
participant is outside the feed's scope (`_chat_is_callers` has no room arm),
so it draws no card in the room. A room-membership arm is a follow-up.

### Three steps states, not two

Ruling 2 said "visible, not silent": an agent that publishes no pipeline says
so. The review added the third: a stopped agent, an unreachable one, an
unreadable file, or **two runs on one agent** (an agent-written, clock-skewed
`updated_at` cannot say which run an instance belongs to) all read `unknown`,
with their own sentence — "could not be read right now". Telling a user a
stopped agent "doesn't report steps" would be the distrust-training misrender
the ruling exists to prevent, in new clothes. In the chat's live card,
`pending` (the feed has not read the turn yet) holds the sentence's one-line
footprint, blank and aria-hidden, and the sentence is held to one line there
(only the agent's name truncates), so `none` / `unknown` swap in place. The
row takes the card's width and never sets it (`w-0 min-w-full`), so a long
agent name cannot widen the card when the sentence lands. The Work tab and the
room render as before (#2964).

### The #919 read is hardened like the MCP tool

`pipeline_state.py` restates `pipelines.ts`'s rules because the two cannot
share code: ids grammar-checked (`^[A-Za-z0-9._-]+$`, no `..`) **before** any
download path is built (the agent-server route has only a `/home/developer`
prefix check); `size` from the listing checked **before** the download, and the
download itself streamed under a 256 KiB budget (a cap after `response.text` is
a cap on memory already spent); YAML through `load_hardened_yaml`; **no
retries** (a stopped agent must not turn one read into a gateway timeout), 2 s
per call inside a 3 s wall budget, a 10 s per-agent cache so the 12 s poll never
reads twice. Every failure is a verdict, never an exception into the read.

### A ghost row is not live

After a hard restart the `finally` that clears an execution is skipped and the
row sits `running` until the 120-minute sweep. A card counting up from its
`started_at` — and a poll it keeps alive — would be the stuck "running" AC 1
forbids, arriving through data instead of transport. The server marks a
running row `stale` past 1.5× the agent's own turn bound (floor 30 min): not
live, no clock, no signal, no poll, and the card says "No longer tracked".

### One signal, merged by id

The conversation still emits its in-flight flag (a synchronous turn has a row
before the feed has read it), and the feed has the same turn a moment later.
Summing the two would show "Work · 2 running" for the whole window both see
it — the common case, not the edge. The emit carries the execution id, and
`workSignalFromItems` joins it into the feed's live set **by id**; a room's
server `working` list joins by name the same way. `live` is derived once.

### The terminal card is the durable verdict, rendered

A card that only lived in the tab's memory would lie after F5. The thread
already carries #2320's outcome record (`lastTurnOutcome`), applied on load and
on reattach by `markLastUserTurnFailed`; the card renders from the same
record, remembered beside that function (`rememberVerdict`) rather than inside
it — #2320's spec evaluates that function in isolation. A cancel the person
asked for is recorded at the act (`cancelTurn`), so "Stopped by you" is a card,
never a red message. A reply that still lands clears it: the reply is the
outcome. **Ask about it** pre-fills the composer with a question that names the
job and how it ended; it never sends.

### Stop after a reload

`reattach` never set `activeExecutionId`, so `canCancelTurn` stayed false for
every reattached turn — Stop was dead after any reload (review E3). It sets it
now and clears it with the turn, pinned by a source guard.

## Doors and disclosure

- Platform-authenticated only, twice: the route 404s a portal token before any
  read; `visibleTabs` never renders the tab for a client, so its body — the
  only thing that fetches — never mounts.
- `agents` is set-membership tested against the roster; an unknown or
  off-roster name is dropped, never answered (Invariant #8). More than 50 names
  is a named 422 before the limiter.
- `title`/`error` pass `sanitize_text` and are bounded; `response`,
  `execution_log`, `tool_calls` are never on the payload; only a `portal` stamp
  becomes a `chat_id` (a Telegram destination is not the client's business).
- `can_stop` is computed once, server-side, as exactly what
  `POST …/executions/{id}/terminate` will accept (roster + started by this
  caller + in flight + a turn or delegated child), so the button is never a lie.

## The activity line — what the agent is doing right now (trinity-enterprise#620)

The card's "current step" slot was fed by the ent#286 stream through a handler
that matched `evt.type === 'tool_use'` — a shape the raw stream-json frames
never carry — so it said nothing; and no run but the chat's own turn had a
live path at all. Now:

- **One vocabulary** — `utils/workActivity.js::activityLine({tool, summary})`
  composes "Reading `…/x.py`" / "Running `pytest …`" / "Searching for …" /
  "Fetching …" / "Using <server>" / "Delegating to <agent>: …" / "Thinking".
  `execution-status.js` (Chat tab) and `useSessionActivity` (Agent Detail)
  compose from the same function.
- **Two feeds, same facts.** The chat's own turn reads the SSE frames
  (`activityFromStreamEvent` parses `message.content[].type === 'tool_use'`
  and summarises the input with a port of the agent's `get_input_summary`;
  parity fixture `tests/fixtures/tool_input_summary.json`). Every other run —
  delegated, scheduled, room — reads the agent's **heartbeat**: the agent
  server keys its active tool per execution (`session_activity.by_execution`)
  and the 5 s beat carries `executions: [{execution_id, tool, summary, since}]`
  for the registry's running set. `heartbeat_service.read_execution_activity`
  keys it by id; `work/service.clean_activity` sanitises (same `sanitize_text`
  + bound as titles), masks an off-roster delegation target, drops a line
  older than 30 s, and `get_work` folds it onto a live, non-stale row of a
  rostered agent as `WorkItem.activity`. `GET …/work/activity?agents=` is the
  cheap sibling (Redis only, same gates) the store polls every 2.5 s while a
  card is live; the full read stays at 12 s. `store.activityFor(item)` prefers
  the fresher map.
- **The row.** `PortalWorkCard` reserves `h-4 overflow-hidden` for the whole
  live life; a keyed `<Transition name="portal-activity">` slides a new line up
  (a swap under reduced motion); `createActivityLineQueue` holds each line
  ≥700 ms, collapses a burst to its latest member, never re-keys an identical
  line, keeps the last line on a quiet run, and is cleared at terminal. The
  chat's card (`reserveLiveRows`) also reserves the steps-sentence row and
  Stop's slot from first paint: Stop stays invisible, disabled, aria-hidden
  and out of the tab order until the 202's execution id arrives (ent#155),
  then the same button turns usable, so Open in Work never moves. In that card
  only the agent's name truncates, never "doesn't report steps." Both of the
  chat's synthetic cards (live and terminal) title themselves with
  `previewTitle`, a mirror of `service.clean_title` pinned row-for-row by
  `tests/fixtures/portal-work-titles.json`, so the feed's row lands without
  re-wrapping the title; only secret masking is not mirrored, and the
  server's sanitiser masks some plain words too ("Basic question"), so those
  titles still change when the row lands. Known and out
  of scope: `stages ↔ unknown`, and a row going stale mid-turn (#2964).
- **Scroll.** `submitUserText` already pins before `deliver`; the card mounts
  after, so a `watch(sending)` re-pins once, on `nextTick`, guarded by
  `following` — the person's own send only (#2624 holds for arrivals).
- **Honest silence.** No beat (old image), a stale row, an off-roster agent, or
  an aged line → the row is empty. The three-state steps rule is untouched.

## Tests

`tests/unit/test_ent525_portal_work.py` — the door (a portal token never
reaches the service), roster narrowing with no oracle, the `work_kind` /
`work_outcome` / `can_stop` tables, staleness, sanitizing and masking, the read
(children merged by id, a foreign chat id ignored, the bounded page and the
window total, steps only for one running row per agent, a ledger failure is a
503), and the hardened pipeline read (traversal ids, the listing-size cap, the
streamed cap, malformed YAML, an instance older than the run, the cache).
`tests/unit/test_ent620_work_activity.py` / `test_ent620_agent_activity.py` /
`test_ent620_summary_parity.py` and `src/frontend/tests/unit/workActivity.spec.js`
— the activity line (ent#620): the heartbeat's bounds, the per-execution slot,
the fold's gates, sanitiser + mask, the age ceiling, the route's gates, the
vocabulary, the real frame shape, the summariser parity, the queue rules.
`src/frontend/tests/unit/portalWork.spec.js` — the pure rules, the store under
Pinia (one request, stale response dropped, failed-never-empty, the poll only
while live and not for a stale row, push filtered and debounced, Stop through
the terminate route), the owner feeding the Work store off the door gate and
re-scoping on a thread switch, the merged signal, and source guards on the
shell, both conversations, the tab body, the card and the WebSocket consumer.
`src/frontend/tests/unit/portalWorkCardLiveShape.spec.js` — the chat card's
reserved rows (#2964): the steps row and Stop's slot exist from first paint and
are patched in place, and every other host keeps the default markup.
`tests/fixtures/portal-work-titles.json`, asserted by
`tests/unit/test_2964_portal_work_title_fixture.py` (against `clean_title`) and
`src/frontend/tests/unit/portalWorkTitle.spec.js` (against `previewTitle`) —
the pre-read title mirror, row for row.

## Residuals (stated)

- Steps refresh on the 12 s poll while a run is live; there is no backend
  broadcast for a pipeline-state write (registered in the debt inbox).
- The roster scope is today's; ent#367's profile scope inherits when it lands.
- A step-level restart is a platform capability (#919 territory), ruled out of
  this surface; the card's lesser control is the honest one.
- A room's delegated child on a non-participant agent is not in the room's feed
  scope, so no card there (#2792; the Work tab of a 1:1 with that agent has it).
