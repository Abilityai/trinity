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
├─ <PortalRoom>          server `working` ∩ feed live rows → <PortalWorkCard show-agent …/>
└─ <PortalRail> #tab-work → <PortalWork>  Waiting on you (PortalAsks over store.asks) · Now · Earlier
stores/portalWork.js ──► GET /api/enterprise/client-portal/work?agents=a,b&chat_id=…
utils/websocket.js: agent_activity (started + terminal) / loop_* for a participant → portalWork (debounced 2 s)
                    pipeline_state_changed for a participant → portalWork (debounced 500 ms, ent#533)

agent_server/pipeline_state_watch.py   1 s scandir of ~/.trinity/pipeline-state, (mtime_ns,size)
      └─► POST /api/agents/{name}/pipeline-state/changed   own agent-scoped MCP key (heartbeat auth)
             └─► routers/agent_pipeline_state.py → work/pipeline_state.notify_changed
                    coalesce 20/10 s · bump the Redis cache generation · thin /ws trigger

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

### Three steps states, not two

Ruling 2 said "visible, not silent": an agent that publishes no pipeline says
so. The review added the third: a stopped agent, an unreachable one, an
unreadable file, or **two runs on one agent** (an agent-written, clock-skewed
`updated_at` cannot say which run an instance belongs to) all read `unknown`,
with their own sentence — "could not be read right now". Telling a user a
stopped agent "doesn't report steps" would be the distrust-training misrender
the ruling exists to prevent, in new clothes.

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

### Stage advances are pushed; the poll stays (ent#533)

Steps used to move only when the 12 s poll happened to run, so a stage advance
could lag a full poll behind the file the agent had already written. The file
is the agent's, in the agent's container — **only the agent's filesystem knows
when it changed**, so the honest notifier is agent-side; the backend can only
ask, and asking faster is the load the 10 s cache exists to bound. The watcher
is therefore a 1 s `os.scandir` in the agent server keyed on
`(mtime_ns, size)`, which costs no network at all while nothing changes (the
canonical `pipeline-tick` writer runs every 15 minutes, so a POST is an event,
not a stream).

**Why the agent's own key, and why `/api/agents/`.** `/api/internal/*`'s
blanket router is gated on `X-Internal-Secret`, which is deliberately never
injected into an agent; the one agent-key predicate on that prefix
(`_pull_authorized`) additionally requires `is_pull_pilot_agent`, so a route
placed there would be silently dead for virtually the whole fleet. The two
existing always-on agent→backend self-reports — `POST /{name}/heartbeat`
(#307) and `POST /{name}/executions/{id}/result` (#1083) — both live under
`/api/agents/` with exactly this auth, which is also Invariant #15's shape for
a fact about one named agent. So: `authorize_heartbeat` on the agent's own
agent-scoped MCP key; a user, system, connector or *other agent's* key is 403,
and the route carries `# mcp: none` because nothing an operator or an MCP
client should ever call belongs on the tool surface.

**Why a Redis generation and not just dropping the local entry.** `_cache` is
a per-process dict and production runs `uvicorn --workers 2`. Invalidating in
memory would refresh the worker that received the notice and leave the other
serving a stale card for up to 10 s — and dev, with one `--reload` worker,
would pass green. A notice instead `SETEX`s a generation the cache compares
alongside its TTL, so every worker misses once. It fails **open**: Redis down
means generation `None` on both sides, i.e. exactly today's TTL-only cache.
Putting the JSON itself in Redis was rejected for the opposite failure
direction — Redis down would then mean *no* cache and a fresh agent read per
participant per poll.

**Why the poll stays.** The notice is best-effort by construction: it is
swallowed on the agent side, coalesced on the server side, and absent
entirely on an old image or an agent without the env gate. If steps depended
on it, every one of those became a stuck card. With the poll kept, a lost
notice costs latency and nothing else — which is why this change has no
user-visible state, string or default of its own.

**Why the client debounce has both an earlier-deadline rule and a floor.**
The store already debounced push refetches by 2 s for `agent_activity`. Left
alone, a 2 s push landing 400 ms after a 500 ms stage push would postpone the
stage refetch to 2.4 s — the store would defeat this feature's own reason to
exist — so the earlier deadline now wins. That alone turns the debounce into
a `delay`-length throttle, and the Work read is limited to 120/60 s per
viewer with a 429 that renders as visible error text; a 1500 ms floor between
push-driven refreshes keeps a pathological writer from ever reaching it. The
floor never binds on the common path (an idle card, one stage advance).

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

## Tests

`tests/unit/test_ent525_portal_work.py` — the door (a portal token never
reaches the service), roster narrowing with no oracle, the `work_kind` /
`work_outcome` / `can_stop` tables, staleness, sanitizing and masking, the read
(children merged by id, a foreign chat id ignored, the bounded page and the
window total, steps only for one running row per agent, a ledger failure is a
503), and the hardened pipeline read (traversal ids, the listing-size cap, the
streamed cap, malformed YAML, an instance older than the run, the cache).
`src/frontend/tests/unit/portalWork.spec.js` — the pure rules, the store under
Pinia (one request, stale response dropped, failed-never-empty, the poll only
while live and not for a stale row, push filtered and debounced, Stop through
the terminate route), the owner feeding the Work store off the door gate and
re-scoping on a thread switch, the merged signal, and source guards on the
shell, both conversations, the tab body, the card and the WebSocket consumer.

`tests/unit/test_ent533_pipeline_state_broadcast.py` — the notice route's auth
(no Bearer / a user key / a system key / another agent's key are all the same
403), the thin trigger's exact key set and its ent#467 visibility by
construction, traversal ids rejected before anything is published, `stage`
bounded and normalised rather than rejected, coalescing, and the Redis
generation making a *second* worker's cache miss inside the TTL (with the
Redis-down path still TTL-only).
`tests/unit/test_ent533_agent_pipeline_state_watch.py` — the watcher's
`snapshot` / `changed` / `read_stage` rules and the loop itself: a silent
baseline, one POST per changed file with the Bearer, the per-tick cap, a
failing POST swallowed, and no scheduler at all without both env vars.

## Residuals (stated)

- The stage-advance notice (ent#533) is **best-effort**: swallowed on the
  agent side, coalesced server-side, and absent on an old image. It lands in
  ~1–1.6 s, not sub-second — one polling tick is the honest floor without an
  inotify dependency — and the 12 s poll remains the fallback for every case
  where it does not arrive.
- The roster scope is today's; ent#367's profile scope inherits when it lands.
- A step-level restart is a platform capability (#919 territory), ruled out of
  this surface; the card's lesser control is the honest one.
