# Feature: WebSocket Event Bus (Redis Streams)

## Overview

Real-time WebSocket delivery backed by a Redis Stream. Replaces the legacy
in-process `ConnectionManager.broadcast(...)` + `except: pass` pattern with a
durable event log so:

- A momentary WebSocket disconnect no longer drops events — reconnecting with
  `?last-event-id=<stream_id>` replays missed events from the stream.
- Failed sends to a dead socket are logged and the client is evicted after
  3 consecutive failures instead of silently accumulating zombie connections.
- The stream has bounded memory (`XADD MAXLEN ~10000`, env-tunable) and
  provides the substrate for later work: agent-push completion (#428/#429) and
  heartbeat push (#307) will reuse the same stream primitive.

Issue: [#306](https://github.com/abilityai/trinity/issues/306) — RELIABILITY-003.
Positioned as the keystone of Tier 2.5 simplification in
[`docs/archive/plans/ORCHESTRATION_RELIABILITY_2026-04.md`](../../archive/plans/ORCHESTRATION_RELIABILITY_2026-04.md).

## User Story

As a Trinity operator watching the Collaboration Dashboard or a Trinity Connect
listener, I want real-time events to be reliable across short disconnects so
my UI doesn't end up with timeline bars stuck on "started" or missed
collaboration edges after a laptop sleep.

## Entry Points

- **Publisher shim (legacy API preserved)**: `src/backend/main.py`
  - `ConnectionManager.broadcast(message)` — `/ws` broadcast. Accepts a
    JSON-encoded string (legacy) or a dict. Internally calls
    `event_bus.publish(message, scope=SCOPE_ALL)`.
  - `FilteredWebSocketManager.broadcast_filtered(event)` — `/ws/events`
    broadcast with per-user `accessible_agents` filter. Internally calls
    `event_bus.publish(event, scope=SCOPE_SCOPED)`.
- **Publisher core**: `src/backend/services/event_bus.py:EventBus.publish`
- **WebSocket endpoints**:
  - `GET /ws?ticket=<opaque>&last-event-id=<stream_id>` — `@app.websocket("/ws")` handler in `src/backend/main.py` (ticket minted via `POST /api/ws/ticket`; see C-002 / #550)
  - `GET /ws/events?token=trinity_mcp_...&last-event-id=<stream_id>` —
    `src/backend/main.py:697+`
- **Frontend WebSocket clients**:
  - Main tab: `src/frontend/src/utils/websocket.js`
  - Collaboration dashboard: `src/frontend/src/stores/network.js` (separate WS
    connection)

---

## Architecture

### Design Decisions

**1. Two layers: producer (EventBus) + consumer (StreamDispatcher)**
- `EventBus.publish()` is fire-and-forget: events land in a bounded
  `asyncio.Queue(10_000)` and a background writer drains to Redis. Broadcast
  call sites never block on Redis latency.
- `StreamDispatcher` runs a single `XREAD BLOCK` coroutine per backend process
  and fans out in-memory to registered WebSocket clients.
- Rationale: 50+ per-connection `XREAD` calls would waste pool connections;
  one reader with in-memory fan-out is both cheaper and simpler.

**2. Single stream with `scope` field, not two streams**
- Stream key: `trinity:events`
- Every XADD carries `scope: "all" | "scoped" | "control"` and optional `agent_name`
- `scope=control` (`SCOPE_CONTROL`, trinity-enterprise#863) is a message
  between workers, never a client event: `_fanout` hands it to the injected
  control handler and returns before any roster read or delivery, and
  `_event_is_visible` has no branch for it, so no `/ws` or `/ws/events` slot
  (admins included) receives it, live or on replay
- `_event_is_visible(slot, scope, agent_name, agent_names, event_kind)` enforces:
  - `/ws` clients (`scope=SCOPE_SCOPED` events never reach them) see a
    `scope=all` event only when **every** agent the payload names is one they
    may access — admins short-circuit, an event naming no agent is
    fleet-visible (ent#467)
  - a `/ws` slot with `allowed_types` (a Workspace-only account,
    trinity-enterprise#837) additionally needs the event's kind in that set —
    `event_kind_of`: `type`, else the lifecycle `event` key; a kind-less
    payload is withheld. Checked first, so it only narrows
  - `/ws/events` clients (`scope=SCOPE_SCOPED`) see `scope=scoped` events,
    filtered by the single envelope `agent_name` (admins see all) — unchanged
- Rationale: Keeps the auth boundary in one place; avoids the 8 dual-broadcast
  call sites having to `XADD` twice.

**2a. `/ws` is agent-scoped too (ent#467)**

`/ws` shipped `SCOPE_ALL` and *unfiltered*, so every `agent_activity`,
`agent_created`, `operator_queue_*` and `agent_shared` event reached every
authenticated client — fleet-wide agent names, execution ids and activity
types, plus another user's email on the two sharing events. Any `role=user`
with one shared agent could mint a ticket (`POST /api/ws/ticket` is plain
`get_current_user`) and read it. `/ws/events` had scoped since #306.

- **Identity at connect.** `services/ws_identity_service.resolve_ws_identity`
  turns the ticket's `sub` into `{email, is_admin, accessible_agents,
  allowed_types}`.
  Fails **closed** — an unknown username, a suspended account (#995) or a
  raising lookup closes the socket with 4001. A non-admin row with *no email*
  is a resolved identity with an **empty roster**, not a refusal: ownership
  joins `users.email` and `agent_sharing` is keyed on it, so the empty set is
  the exact answer, and the frontend never retries a 4001.
- **Kinds for a Workspace-only account (trinity-enterprise#837).**
  `allowed_types` is `None` on the operator rungs and
  `WORKSPACE_WS_EVENT_TYPES` for a `user` (or a role outside the ladder):
  `agent_activity`, `loop_run_completed`, `loop_completed`,
  `agent_skills_changed`, `resync_required` — what the Workspace stores read.
  The operator queue, notifications, reports, room triggers, the agent
  lifecycle and the sharing events never reach that socket, on fan-out or
  replay. Each event it does get is cut to `allowed_fields`
  (`WORKSPACE_WS_EVENT_FIELDS`: `type`, `event`, `agent_name`,
  `activity_state`) plus `_eid` — an activity's prompt and reply previews,
  cost and session ids stay off it. `ConnectionManager.connect` defaults both
  arguments to the empty set, so a caller that forgets them gets a dark socket
  rather than an unfiltered one.
- **Identity is derived from the payload, once per event.** The 36 live
  `manager.broadcast` sites disagree about where the agent name lives
  (`agent_name` top-level, `data.name`, `data.agent_name`, two keys at once
  for `agent_collaboration`), so `agent_names_in_payload()` reads a fixed key
  vocabulary in one place. `details` is read **narrowly** (the two
  collaboration keys only) because activity `details` is free-form and a
  `details["name"]` holding a tool name would hide the event from the agent's
  own owner.
- **Both delivery paths filter.** `_catchup` re-reads history straight out of
  Redis on a `last-event-id` reconnect; a filter wired only into `_fanout`
  would hand the whole unfiltered backlog to any client that reconnects. Pinned
  structurally by `tests/unit/test_ent467_ws_agent_scope.py`.
- **Admins are never filtered.** Their roster would be a connect-time
  snapshot, so filtering on it would blind the operator to every agent created
  after the page loaded. `resolve_ws_identity` therefore resolves *no* roster
  for an admin.
- **A live roster is re-read on an access change and once a minute**
  (trinity-enterprise#863). `_maybe_invalidate_rosters` watches for
  `agent_created` / `agent_deleted` / `agent_shared` / `agent_unshared` /
  `agent_renamed` and re-resolves each non-admin slot in a worker thread: a
  `/ws` slot by email (`set_accessible_resolver`, injected from `main.py` so the
  delivery layer never imports `database`), a non-admin `/ws/events` slot
  through its own `roster_resolver`. Because every worker reads the same stream,
  one publish reaches the rosters on all of them with no call-site edits.
  `_reread_rosters_periodically` re-reads every non-admin roster on the worker
  every `ROSTER_REREAD_INTERVAL_S` (60 s): started and stopped by the dispatcher
  itself, per worker with no lease, one read at a time, and nothing read while
  no non-admin roster is held. A live roster therefore follows the database
  within about a minute while the database is readable.
- **A change during a read gets one more read.** A read asked for while one is
  already in flight for that slot (`refreshing`) marks it `refresh_again`, and
  the slot is read once more when the in-flight read ends — unless it has
  disconnected or the dispatcher is stopping — and the in-flight read's result
  is not applied to it, since it may predate the change. The periodic cycle
  claims each slot just before its own read, so a slot it has not reached yet
  is read at once on an access change. `update_accessible_agents` (the
  `/ws/events` `refresh` command) marks it the same way. A failed read keeps the
  previous roster until the next read replaces it.
- **An event-stream key's roster is its owner's, read now.** Every non-admin
  `/ws/events` key — user keys as well as agent keys — gets
  `ws_identity_service.event_stream_roster_reader(key_info)`, which re-reads the
  key's owner on every call: an owner that is gone, deactivated or
  Workspace-only reads as an empty list, and the owner's current email is the
  one used. The `refresh` command reads through the same function. Admin keys
  are never filtered and get no automatic re-read.
- **No Redis format change**: the envelope still carries a single
  `agent_name`; the set is derived from the payload at delivery, so entries
  written by a pre-ent#467 worker during a rolling deploy filter correctly.
- **The guard, not the extractor, is the safety net.** A payload naming no
  agent stays fleet-visible (fail-open), so `test_ent467_ws_agent_scope.py`
  discovers **every** `/ws` broadcast payload in the OSS tree by AST and fails
  unless each is either agent-keyed or listed in `FLEET_LEVEL_ALLOWLIST` with
  a reason. Known allowlist entries: the bulk `operator_queue_cleared`
  (reports a count, names no agent) and the ent#170 room triggers (ids only;
  `room_participant_state.identity` *is* an agent name but the column is
  polymorphic, so keying on it would drop the event for the room's own human
  participants — room-membership scoping is a tracked follow-up).

**3. Per-client bounded queue, never await send from fan-out**
- Each client slot has `asyncio.Queue(maxsize=256)`
- `_fanout` does `put_nowait`; on `QueueFull`, flags the client for
  `resync_required` and drops the event
- A dedicated `_client_consumer` coroutine drains the queue and does the
  actual `websocket.send_*`
- Rationale: A slow client can't block fan-out for others (head-of-line
  blocking prevention).

**4. Reconnect replay with trim-race detection**
- Client stores `lastEventId` in-memory (not localStorage — page reload wipes
  stores anyway, so only sub-session reconnect matters)
- On reconnect the client sends `?last-event-id=<id>`
- Dispatcher snapshots `_last_stream_id` at registration time, then runs
  `XRANGE (<id> max=<snapshot>` — the cap prevents catchup from overlapping
  with live fan-out, which would otherwise double-deliver (#306 review C1)
- Trimmed cursor → `{type: "resync_required", reason: "trimmed"}`; frontend
  clears cursor and refetches authoritative state via REST

**5. 3-failure eviction**
- `EVICT_AFTER_FAILURES=3` consecutive send exceptions → close socket,
  remove from dispatcher
- Replaces the legacy `except: pass` silent-drop

**6. Graceful degradation when Redis is unavailable**
- `EventBus._xadd` catches errors, closes and reconnects the client on next
  call
- In-memory fallback buffer (`_FALLBACK_BUFFER_MAX=1024`) holds events for a
  brief Redis outage; drops silently when full
- Trade-off: no live updates when Redis is down; rest of Trinity is already
  broken in that case (Redis is a hard dependency for credentials,
  rate-limiting, sessions)

---

## Flow

### 1. Publish path
```
broadcast site
   │ manager.broadcast(str|dict)  or  filtered_manager.broadcast_filtered(dict)
   ▼
EventBus.publish(event, scope)
   │ wraps into envelope: {payload, scope, agent_name}
   │ put_nowait onto self._outbound (asyncio.Queue, cap 10_000)
   ▼
EventBus._writer_loop (background task)
   │ drains outbound queue
   ▼
redis.xadd(STREAM_KEY, fields, maxlen=10000, approximate=True)
```

### 2. Consume path (live)
```
StreamDispatcher._reader_loop (single background task per process)
   │ redis.xread({STREAM_KEY: self._last_stream_id}, block=5000, count=100)
   │ updates self._last_stream_id
   ▼
StreamDispatcher._fanout(entry_id, fields)
   │ deserialize
   │ scope == "control" → control handler (live_connections.handle_control), return
   │ inject _eid into payload
   ▼
for each slot in self._clients:
   │ if _event_is_visible(slot, scope, agent_name):
   │     slot.queue.put_nowait((entry_id, payload))
   │     # QueueFull → mark resync_pending, enqueue resync_required marker
   ▼
_client_consumer (one per WS connection)
   │ await queue.get()
   │ await slot.send_func(payload)   # websocket.send_text / send_json
   │ on Exception: failure_count++; evict at EVICT_AFTER_FAILURES
```

### 3. Reconnect replay
```
browser reconnects with ?last-event-id=<id>
   │
   ▼ validate_last_event_id(raw)   # regex: ^\d+-\d+$ (security gate)
   │
   ▼ manager.connect(ws, last_event_id=validated_id)
   │ snapshot catchup_max = dispatcher._last_stream_id  (before adding to _clients)
   │ register slot → fan-out starts delivering events > snapshot
   ▼
_catchup(client_id, slot, last_event_id, catchup_max)
   │ if gap > REPLAY_GAP_LIMIT (5000) → queue resync_required("gap_too_large")
   │ else XRANGE(STREAM_KEY, min="(<id>", max=catchup_max, count=5001)
   │ check for trim race: if oldest_id > last_event_id → resync_required("trimmed")
   ▼ for each entry matching scope/access → put_nowait onto slot.queue
```

### 4. Frontend lastEventId contract
- **Set**: on every incoming message with `_eid`, store in module-scoped var
- **Send**: on (re)connect, append `&last-event-id=<id>` to WS URL
- **Clear**: on `{type: "resync_required"}` message, null out and call
  authoritative REST refetchers (`fetchAgents()`, `fetchHistoricalCollaborations()`,
  `fetchPendingCount()`)

---

## Security

- **Live connections follow the account** (trinity-enterprise#863,
  `services/live_connections.py`). Each `/ws` and `/ws/events` handler
  registers with the worker's `LiveConnectionRegistry` (`admit`) before it
  reads the account, records the `AccessSnapshot` of the row it read
  (`activate`), and marks the connection live once it is connected
  (`go_live`). A re-check compares the recorded snapshot with the account's
  current row: gone, deactivated or key inactive → closer with `ENDED` (4001);
  role, email or password changed → `CHANGED` (4000 `scope changed`). The tick
  runs every `RECHECK_INTERVAL_S` in every worker; `notify_account_changed` /
  `notify_key_changed` run the same comparison at once for one account or key
  (the role route and a password change call the first after their write; the
  MCP key revoke and delete routes call the second, so only the streams opened
  with that key close). Each `notify_*` also publishes one `scope=control`
  event (`{"recheck": "account", "username": …}` or `{"recheck": "key",
  "key_id": …}`) through the publisher `main.py` injects
  (`_publish_live_connection_control` → `event_bus.publish`); every worker's
  dispatcher hands it to `live_connections.handle_control`, which runs the same
  targeted re-check against that worker's own read and publishes nothing, so
  an event is never republished. The publishing worker reads its own event
  too; a connection that already matches the account (one that reconnected at
  the new scope) stays open. The closer unregisters the
  dispatcher slot before it closes the socket, so the slot's consumer never
  sends after the close frame; the handler returns through its `finally`.
- **Authentication**: `/ws` requires a single-use opaque ticket minted via
  `POST /api/ws/ticket` (C-002 / #550 — see `architecture.md` "WebSocket
  Security"). The 32-byte ticket has a 30s TTL and is consumed atomically
  via Redis `GETDEL`; JWT-in-URL was removed to close pentest finding 3.2.1.
  `/ws` consumes with `scope="user"` (trinity-enterprise#595): every ticket
  consumer names the one scope it accepts, so a voice or VoIP ticket is refused
  here by its scope after being spent, not by the shape of its subject.
  `/ws/events` still accepts a `trinity_mcp_*` API key via `?token=` (kept
  for the documented `wscat`/`websocat` surface). Consumer tasks inherit
  connection-level auth.
- **Input validation**: `?last-event-id=` is regex-gated
  (`EID_PATTERN = ^\d+-\d+$`) in `validate_last_event_id()` before reaching
  `XRANGE`. Malformed input → `None` → no catchup.
- **Authorization**: `_event_is_visible` is applied on both live fan-out AND
  catchup replay. `accessible_agents` is read at event-delivery time (not
  cached at connect), so changes via `update_accessible_agents` take effect
  immediately.
- **Replay DoS ceiling**: `REPLAY_GAP_LIMIT=5000`. Larger gap → reject with
  `resync_required` instead of an unbounded XRANGE.
- **Stream memory**: `REDIS_STREAM_MAXLEN` env var, default 10_000.

See `docs/security-reports/cso-2026-04-21-diff-306.md` (if saved) for the
`/cso --diff` audit.

---

## Observability

Log events (via stdlib `logging`, captured by Vector):
- `event_bus: writer error: <err>; backoff=<s>` — Redis XADD failure
- `event_bus: Redis unavailable (<err>); publish will degrade` — on startup
- `event_bus: outbound queue full, dropping event` — publisher side saturation
- `stream_dispatcher: reader crashed: <err>; restart in <s>` — supervised
  reader restart
- `stream_dispatcher: client <id-prefix> queue full, marking resync` — slow
  consumer
- `stream_dispatcher: send failed for <id-prefix> (N/3): <err>` — per-attempt
- `stream_dispatcher: evicting client <id-prefix> after 3 failures`

### Soak counters (`GET /api/debug/event-bus-stats`, admin-only)

In-memory counters for the #306 soak gates. Monotonic, reset on backend
restart — compare deltas. Response shape:

- `publisher` — `events_published`, `publish_failures`, `outbound_overflow`,
  `outbound_queue_depth`, `fallback_buffer_depth`, `redis_ready`, `uptime_seconds`
- `dispatcher` — `events_delivered`, `send_failures`, `drops_queue_full`,
  `clients_evicted`, `resyncs_sent`, `client_count`, `last_stream_id`, `uptime_seconds`
- `watchdog` — `cumulative_orphaned`, `cumulative_auto_terminated`, `last_run_at`
  (running totals from `CleanupService`)

Gate checks (see orchestration reliability plan, Tier 2.5):
- Delivery ≥99.9%: `(drops_queue_full + clients_evicted + resyncs_sent) / events_published < 0.001`
- Zero watchdog recoveries: `watchdog.cumulative_orphaned == 0`

---

## Key Files

| Layer | File | Role |
|-------|------|------|
| Service | `src/backend/services/event_bus.py` | `EventBus`, `StreamDispatcher`, scope helpers, `validate_last_event_id`, soak-counter `stats()` helpers |
| Router | `src/backend/routers/debug.py` | Admin-only `GET /api/debug/event-bus-stats` for soak dashboard |
| Entry | `src/backend/main.py:112-200` | `ConnectionManager` / `FilteredWebSocketManager` shims over the bus |
| Entry | `src/backend/main.py:634+`, `main.py:697+` | `/ws` and `/ws/events` endpoints with `?last-event-id=` support |
| Entry | `src/backend/main.py:285-294` | Lifespan `event_bus.start()` + `stream_dispatcher.start()` |
| Entry | `src/backend/main.py:538-547` | Lifespan 2s graceful drain on shutdown |
| Client | `src/frontend/src/utils/websocket.js` | Main WebSocket client; `_eid` capture, reconnect replay, `resync_required` → REST refetch. For a Workspace-only `user` the operator stores (agents, notifications, operator queue, executions) do not follow the stream — their refetches would 403 `workspace_only`; the Workspace stores still do (trinity-enterprise#837) |
| Client | `src/frontend/src/stores/network.js:535+` | Collaboration dashboard WS client; same contract, separate `lastEventId` |
| Service | `src/backend/services/ws_identity_service.py` | ent#467 `/ws` identity + agent scope (`resolve_ws_identity`, `accessible_agents_for`); `/ws/events` scope (`event_stream_identity`) and the owner re-read behind every non-admin event-stream roster (`event_stream_roster_reader`, trinity-enterprise#863) |
| Tests | `tests/test_event_bus.py` | 23 unit tests — envelope, scope visibility, eviction, slow-consumer resync, monotonic cursor guard, catchup trim detection |
| Tests | `tests/unit/test_ent467_ws_agent_scope.py` | 36 tests — payload agent-identity vocabulary, `/ws` visibility rule, both delivery paths, roster refresh, fail-closed identity, AST discovery guard over every `/ws` broadcast |
| Service | `src/backend/services/live_connections.py` | trinity-enterprise#863 — per-worker registry of open `/ws` and `/ws/events` connections, the one snapshot comparison, the 15 s re-check loop, the thread-safe `notify_*` (which also publish a control event) and `handle_control` (the receiving side, local only) |
| Tests | `tests/unit/test_ent863_live_connections.py` | trinity-enterprise#863 — the comparison (role/email/password → 4000; deactivated, gone or key inactive → 4001), the role route's immediate re-check, two workers, failures (tick read keeps open, immediate read closes, a raising closer), the plain-thread notify, register-before-read ordering, and the real `/ws` and `/ws/events` handlers closing with their codes and unregistering their slot |
| Tests | `tests/unit/test_ent863_control_events.py` | trinity-enterprise#863 — the control event: published once per change, handed to every worker's registry, re-checked locally and never republished; reaches no client live or on replay, admins included; the publisher's own echo closes nothing that matches; two workers over one stream; failing publish or handler contained; the real key revoke/delete routes close only that key's streams on every worker; a password change closes the account's connections; `main.py` wiring |
| Tests | `tests/unit/test_ent863_event_stream_roster.py` | trinity-enterprise#863 — the real `/ws/events` handler wires the owner re-read for every non-admin key and `refresh` reads the owner's current row; the reader's owner checks and current email; a change during an in-flight read; the periodic re-read (interval, one read at a time, no read when idle, survives a failing read, stops with the dispatcher) |

## Constants

| Name | Value | Purpose |
|------|-------|---------|
| `STREAM_KEY` | `"trinity:events"` | Redis stream name |
| `STREAM_MAXLEN` | env `REDIS_STREAM_MAXLEN`, default 10000 | Approximate trim target |
| `CLIENT_QUEUE_MAXSIZE` | 256 | Per-client buffer; overflow triggers resync |
| `EVICT_AFTER_FAILURES` | 3 | Consecutive send failures before eviction |
| `REPLAY_GAP_LIMIT` | 5000 | Max replay size before forced resync |
| `_FALLBACK_BUFFER_MAX` | 1024 | In-process buffer during Redis outage |
| `RECHECK_INTERVAL_S` | 15 | How often each worker re-checks every open `/ws` and `/ws/events` connection against the account's current row (trinity-enterprise#863); a code constant, no env setting |
| `ROSTER_REREAD_INTERVAL_S` | 60 | How often each worker re-reads every non-admin roster it holds (trinity-enterprise#863); a code constant, no env setting |

---

## Scope Discipline (#306 vs #428/#429/#307)

This flow covers **WebSocket delivery only**. The following are intentionally
deferred (see the orchestration reliability plan):

- **#428 (CAPACITY-CONSOLIDATE)** — replaces `ExecutionQueue`/`SlotService`/
  `BacklogService` with a single `CapacityManager`. Will consume agent
  completion events from a dedicated stream.
- **#429 (CLEANUP-COLLAPSE)** — retires the 9-path cleanup pyramid once
  agent-push completion is authoritative. Gated on ≥2 weeks of push in
  production with zero observed orphans.
- **#307 (RELIABILITY-004)** — flips agent heartbeat from 30s polling to 5s
  push. Reuses the stream primitive established here.
- **#408** — dissolves once agent-push completion retires the 1h blocking
  HTTP call in `TaskExecutionService`.

The additive-first migration rule: new paths ship alongside old ones, old
code is deleted only after proof. The legacy `manager.broadcast(...)` call
signature is preserved across all 33 broadcast sites, so no call-site change
was needed for this issue.
