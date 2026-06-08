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
- Every XADD carries `scope: "all" | "scoped"` and optional `agent_name`
- `_event_is_visible(slot, scope, agent_name)` enforces:
  - `/ws` clients (`scope=SCOPE_ALL`) see only `scope=all` events
  - `/ws/events` clients (`scope=SCOPE_SCOPED`) see `scope=scoped` events,
    filtered by `accessible_agents` (admins see all)
- Rationale: Keeps the auth boundary in one place; avoids the 8 dual-broadcast
  call sites having to `XADD` twice.

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
   │ deserialize + inject _eid into payload
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

- **Authentication**: `/ws` requires a single-use opaque ticket minted via
  `POST /api/ws/ticket` (C-002 / #550 — see `architecture.md` "WebSocket
  Security"). The 32-byte ticket has a 30s TTL and is consumed atomically
  via Redis `GETDEL`; JWT-in-URL was removed to close pentest finding 3.2.1.
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
| Client | `src/frontend/src/utils/websocket.js` | Main WebSocket client; `_eid` capture, reconnect replay, `resync_required` → REST refetch |
| Client | `src/frontend/src/stores/network.js:535+` | Collaboration dashboard WS client; same contract, separate `lastEventId` |
| Tests | `tests/test_event_bus.py` | 23 unit tests — envelope, scope visibility, eviction, slow-consumer resync, monotonic cursor guard, catchup trim detection |

## Constants

| Name | Value | Purpose |
|------|-------|---------|
| `STREAM_KEY` | `"trinity:events"` | Redis stream name |
| `STREAM_MAXLEN` | env `REDIS_STREAM_MAXLEN`, default 10000 | Approximate trim target |
| `CLIENT_QUEUE_MAXSIZE` | 256 | Per-client buffer; overflow triggers resync |
| `EVICT_AFTER_FAILURES` | 3 | Consecutive send failures before eviction |
| `REPLAY_GAP_LIMIT` | 5000 | Max replay size before forced resync |
| `_FALLBACK_BUFFER_MAX` | 1024 | In-process buffer during Redis outage |

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
