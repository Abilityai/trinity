# Feature: Agent Push-Heartbeat Liveness Layer (RELIABILITY-004 / #307)

## Overview

A fast, **push-based** liveness layer that flips agent-crash detection from
backend *polling* (the 30s `monitoring_service.py` loop) to agent *push*. Each
running agent POSTs a lightweight heartbeat to the backend every ~5s; the
backend stores it in Redis with a 15s TTL and a backend-side watch loop fires a
soft operator alert after 3 consecutive missed beats.

This is **additive**, not a replacement. The 30s docker/network/business
monitoring stays the authoritative aggregate-status signal
([agent-monitoring.md](agent-monitoring.md)). The heartbeat layer narrows the
detection window for a hard container crash from "up to 30s" to "≤15s" and
annotates fleet status with fast-path liveness — it never changes `status`
itself.

## User Story

As a Trinity platform operator, I want to know within seconds when an agent
container crashes or wedges so that I can react before the slower 30s health
loop notices — without being paged for a transient network blip between the
agent and the backend.

---

## Key Concepts

### Why push, and why "soft"

A missed heartbeat is ambiguous: it can mean the agent crashed, *or* that the
agent→backend POST failed while backend→agent still works. So the watch loop
never marks an agent hard-`critical` on a miss. It fires a **soft, high-priority
notification** after an **N-consecutive-miss guard** (default 3), and only on
the **alive→stale transition** — exactly one alert per loss episode. A recovery
notification fires when beats resume (only if a downgrade had happened). This
keeps a false positive cheap and self-correcting.

### Heartbeat states

`heartbeat_status()` resolves each agent to one of three states, hinged on a
**persistent `seen` marker** (the backward-compatibility primitive):

| State | Condition | Meaning |
|-------|-----------|---------|
| **`alive`** | `seen` marker present **and** 15s TTL key present | Beating normally |
| **`stale`** | `seen` marker present **but** TTL key expired | Was beating, stopped (≥3 missed beats) |
| **`unsupported`** | `seen` marker **absent** | Old-image agent that never heartbeated — **never marked dead**, watch loop ignores it |

`heartbeat_alive` is `True` for alive, `False` for stale, and `None` for
unsupported. The `unsupported` resolution is what makes the feature
zero-impact on a fleet of pre-#307 agents.

### Option B authentication (least-privilege)

The agent authenticates its own beat with its injected
`TRINITY_MCP_API_KEY` — **not** a user JWT and **not** the master internal
secret (no master secret is ever injected into agents). An agent may only
heartbeat *itself*: the key must be agent-scoped and its bound `agent_name`
must equal the path. User/system/null-scoped keys → 403. Key validation runs
with `track_usage=False` so a 5s beat doesn't amplify `usage_count` or write
to SQLite ~12×/min/agent.

---

## Entry Points

| Source | API / mechanism | Purpose |
|--------|-----------------|---------|
| **Agent container** (5s loop) | `POST /api/agents/{name}/heartbeat` | Push liveness payload |
| **Backend watch loop** (5s, staggered +10s) | internal `process_watch_tick()` | Detect stale agents, fire alerts |
| **Monitoring page / MCP `get_fleet_health`** | `GET /api/monitoring/status` | Reads back `heartbeat_*` annotations |
| **Agent delete** | `clear_heartbeat()` | Tear down Redis keys |
| **Agent rename** | `clear_heartbeat(old_name)` | Avoid orphaning the no-TTL `seen` key |

---

## Redis Keys

All keys follow the `agent:*` naming convention and live within the backend
Redis ACL (`-@dangerous`). No SQLite table — heartbeat state is intentionally
ephemeral.

```
agent:heartbeat:{name}        STRING, 15s TTL (SETEX). JSON {ts, memory_mb, active_executions, uptime_s}
agent:heartbeat:seen:{name}   STRING "1", NO TTL. Backward-compat hinge (absent ⇒ unsupported)
agent:heartbeat:misses:{name} STRING(int), ~60s TTL. Consecutive-miss counter (watch loop INCR/EXPIRE/DEL)
```

- `ts` is a **backend-side** receive timestamp stamped in `record_heartbeat()`,
  used to compute `last_heartbeat_age_s` on read.
- The `seen` marker is set with `nx=True` (idempotent — a no-op after the first
  beat) and has **no TTL**, so it must be explicitly deleted on agent
  delete/rename, else it leaks one permanent key per agent ever created.
- `misses` is per-tick watch-loop state; it self-expires and is never persisted.

---

## Pydantic Models

### Inbound payload (`src/backend/routers/agents.py:713`)

```python
class HeartbeatPayload(BaseModel):
    """Lightweight liveness payload POSTed by the agent every ~5s."""
    memory_mb: Optional[float] = None
    active_executions: Optional[int] = None
    uptime_s: Optional[float] = None
```

### Status annotation (`src/backend/db_models.py:872+`)

Five additive fields on `AgentHealthSummary` (all default `None` so old payloads
and old-image agents stay non-breaking):

```python
heartbeat_alive: Optional[bool] = None              # None for `unsupported`
last_heartbeat_age_s: Optional[float] = None
heartbeat_active_executions: Optional[int] = None
heartbeat_memory_mb: Optional[float] = None
heartbeat_state: Optional[str] = None               # "alive" | "stale" | "unsupported"
```

---

## Agent-Side Layer (`docker/base-image/agent_server/heartbeat.py`)

The 5s loop, mirroring the `auto_sync.py` background-loop pattern: sleeps-first,
infinite loop, **swallows every exception** so a backend blip can never kill the
loop. A failed beat is silent by design — the backend's miss guard is what acts
on the absence.

| Function | Line | Description |
|----------|------|-------------|
| `_parse_vmrss(status_text)` | 43 | Extract resident MB from `/proc/self/status` `VmRSS:` |
| `_read_memory_mb()` | 58 | Resident memory via `/proc/self/status` (no psutil dep) |
| `_count_active_executions()` | 73 | `len(process_registry.list_running())`, never crashes |
| `_build_payload()` | 81 | `{memory_mb, active_executions, uptime_s}` (monotonic uptime) |
| `_post_heartbeat_once(client, ...)` | 93 | One POST on the shared keep-alive client; logs ≥400 at debug |
| `run_heartbeat_loop(interval=5)` | 114 | Sleep-first loop; reuses one warm `AsyncClient` across beats |
| `schedule_heartbeat(app)` | 145 | Attach startup/shutdown handlers, gated on URL + key |

**Gating:** the loop only runs when **both** `TRINITY_BACKEND_URL` and
`TRINITY_MCP_API_KEY` are present (checked in both `schedule_heartbeat()` and
`run_heartbeat_loop()`). Mis-provisioned / old-image agents simply never beat.

**Wiring** (`docker/base-image/agent_server/main.py:69`):
```python
from .heartbeat import schedule_heartbeat
...
schedule_heartbeat(app)   # gated on TRINITY_BACKEND_URL + TRINITY_MCP_API_KEY
```

**Env injection** (`src/backend/services/agent_service/crud.py:527`): when an
agent gets its MCP key, the backend also injects
`TRINITY_BACKEND_URL` (default `http://backend:8000`) so the agent knows where
to POST.

---

## Backend Layer

### Heartbeat Service (`src/backend/services/heartbeat_service.py`)

Owns **all** Redis heartbeat access (Invariant #1: services hold the logic).
Reuses `routers.auth.get_redis_client()` via a lazy import (avoids a circular
import and keeps Redis off the unit-test import path). Every Redis path fails
**soft** — Redis-`None` resolves to `unsupported`/`False`, never an exception
into the request path.

| Function | Line | Description |
|----------|------|-------------|
| `authorize_heartbeat(validation_result, agent_name)` | 80 | Pure predicate: True iff key is agent-scoped **and** bound to this agent |
| `record_heartbeat(agent_name, payload)` | 99 | SETEX 15s + `seen` marker (nx); stamps `ts`; returns `stored` bool |
| `clear_heartbeat(agent_name)` | 121 | DELETE all three keys (delete/rename cleanup); best-effort |
| `read_heartbeat(agent_name)` | 144 | Last payload or None |
| `heartbeat_status(agent_name)` | 205 | Single-agent state compute |
| `heartbeat_status_bulk(agent_names)` | 219 | **One pipelined round-trip** for the whole fleet (D4) |
| `_compute_status(data, seen, now)` | 167 | The state machine (alive/stale/unsupported) |
| `process_watch_tick()` | 293 | One watch iteration; returns `(name, kind)` transitions, sync + testable |
| `run_heartbeat_watch_loop(interval=5)` | 358 | Sleep-first background loop; turns transitions into alerts |
| `schedule_heartbeat_watch()` | 375 | Spawn the watch loop as a background task |

**Constants:** `HEARTBEAT_TTL_SECONDS=15`, `HEARTBEAT_MISS_THRESHOLD=3`,
`HEARTBEAT_WATCH_INTERVAL_SECONDS=5`, `_MISS_COUNTER_TTL_SECONDS=60`.

**`process_watch_tick()` logic** (the heart of D2):
- Bounds itself to **live agents** (`docker_service.list_all_agents_fast()`) —
  Docker is the source of truth, and bounding keeps it canary-safe (L-03
  forbids referencing a missing agent).
- `state == "stale"` → `INCR` the miss counter (+ refresh 60s TTL). Emit
  `degraded` **exactly once** when the counter first hits the threshold (atomic
  INCR makes exactly one caller observe `== THRESHOLD`, so the transition is
  single-fire even with multiple workers).
- `state == "alive"` → if a miss counter exists, `DEL` it; emit `recovered`
  only if the prior count had reached the threshold (i.e. we'd actually
  downgraded). Fresh-and-healthy agents that were never counting do nothing.
- `state == "unsupported"` (or unknown) → ignored entirely.

### Heartbeat Endpoint (`src/backend/routers/agents.py:720`)

```python
@router.post("/{agent_name}/heartbeat")
async def agent_heartbeat(agent_name: str, payload: HeartbeatPayload, request: Request):
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=403, detail="Heartbeat requires the agent's own MCP key")
    token = auth_header[7:]
    res = db.validate_mcp_api_key(token, track_usage=False)   # <- no usage amplification
    if not heartbeat_service.authorize_heartbeat(res, agent_name):
        raise HTTPException(status_code=403, detail="Heartbeat requires the agent's own MCP key")
    stored = heartbeat_service.record_heartbeat(agent_name, payload.model_dump(exclude_none=True))
    return {"ok": True, "stored": stored}
```

A Redis blip returns `{"ok": True, "stored": false}` rather than 5xx — the beat
is an additive signal.

### MCP-key `track_usage` flag (`src/backend/db/mcp_keys.py:191`, `database.py:672`)

`validate_mcp_api_key(api_key, *, track_usage=True)` — the heartbeat path passes
`track_usage=False`, which skips the `UPDATE mcp_api_keys SET last_used_at,
usage_count` write so a 12×/min/agent beat doesn't bloat the counter or thrash
SQLite. Default `True` preserves existing behavior for every other caller.

### Fleet-status annotation (`src/backend/routers/monitoring.py:178`)

`GET /api/monitoring/status` merges the heartbeat layer in **a single batched
Redis round-trip** (`heartbeat_status_bulk`) inside the existing try/except, so
a Redis blip degrades rather than 500s. This is the **passive (pull)**
annotation — it sets the five `heartbeat_*` fields on each summary but does
**not** change `status`. Heartbeat loss is surfaced **actively (push)** by the
watch loop, not here.

### Watch-loop wiring (`src/backend/main.py:475`)

Started from the lifespan, **staggered +10s** so just-started agents aren't
penalized, mirroring the other maintenance loops:

```python
async def _start_heartbeat_watch_delayed():
    await asyncio.sleep(10)
    from services.heartbeat_service import schedule_heartbeat_watch
    schedule_heartbeat_watch()
asyncio.create_task(_start_heartbeat_watch_delayed())
```

### Alerts (`src/backend/services/monitoring_alerts.py:412+`)

Two new methods on `MonitoringAlertService`, reusing the existing
notification + cooldown infrastructure (no new alert plumbing):

| Method | Severity | Cooldown | Fires when |
|--------|----------|----------|-----------|
| `alert_heartbeat_lost(agent_name, missed_beats)` | `high` | `degraded_cooldown` (30 min) | alive→stale transition, after the 3-miss guard |
| `alert_heartbeat_recovered(agent_name)` | `normal` | none (rare, always worth surfacing) | beats resume after a prior downgrade |

Notification `category="health"`; condition keys `heartbeat_lost` /
`heartbeat_recovered`. Dispatched via the existing `_broadcast_alert()`
WebSocket path, so the operator UI receives them like any other monitoring
alert. `_emit_heartbeat_alert()` in the service swallows every exception — an
alert failure must never kill the watch loop.

### Cleanup wiring

| Site | Line | Reason |
|------|------|--------|
| Agent delete (`routers/agents.py:478`) | best-effort `clear_heartbeat()` | The no-TTL `seen` key would leak past delete |
| Agent rename (`routers/agent_rename.py:139`) | best-effort `clear_heartbeat(old_name)` | `seen` is keyed by name — a rename would orphan the old name's key forever; the renamed container re-sets `seen` under the new name on its next beat |

---

## Data Flow Diagrams

### Flow 1: Normal heartbeat (happy path)

```
Agent container          Backend                       Redis
     |                      |                             |
     | (sleep 5s)           |                             |
     | POST /api/agents/    |                             |
     |  {name}/heartbeat    |                             |
     | Bearer <own MCP key> |                             |
     |--------------------->|                             |
     |                      | validate_mcp_api_key(       |
     |                      |   track_usage=False)        |
     |                      | authorize_heartbeat()       |
     |                      |   scope==agent &&           |
     |                      |   agent_name==path          |
     |                      | record_heartbeat():         |
     |                      |   SETEX hb:{name} 15s       |
     |                      |-------------------------->  |
     |                      |   SET seen:{name} nx        |
     |                      |-------------------------->  |
     |  {ok:true,stored:true}                             |
     |<---------------------|                             |
```

### Flow 2: Crash detection + alert (the value path)

```
Watch loop (5s)        heartbeat_service        Redis              monitoring_alerts
     |                      |                      |                      |
     | process_watch_tick() |                      |                      |
     |--------------------->| list_all_agents_fast()                     |
     |                      | heartbeat_status_bulk (1 pipeline)          |
     |                      |--------------------->|                      |
     |   agent X: hb key expired, seen present -> "stale"                 |
     |                      | INCR misses:X        |                      |
     |                      |--------------------->| (=1, =2, =3)         |
     |   misses == 3 (threshold) -> transition ("X","degraded")          |
     |                      |                      |                      |
     | _emit_heartbeat_alert("X","degraded")       |                      |
     |------------------------------------------------------------------>| alert_heartbeat_lost()
     |                      |                      |  cooldown check + create_notification + WS broadcast
     |                      |                      |                      |
     |   ... later, agent X resumes beating: state -> "alive"            |
     | prior misses >= 3 -> DEL misses:X, transition ("X","recovered")   |
     |------------------------------------------------------------------>| alert_heartbeat_recovered()
```

---

## Failure Modes & Guarantees

| Scenario | Behavior |
|----------|----------|
| Backend down / network blip (agent side) | Beat raises, loop swallows it, retries next tick. Silent by design. |
| Redis down (backend side) | `record_heartbeat` → `stored=false` (200, not 5xx); `heartbeat_status*` → `unsupported`; `process_watch_tick` returns no transitions. Fail-soft. |
| Old-image agent (no `seen` marker) | Resolves to `unsupported` — never alerted on, never marked dead. |
| Mis-provisioned MCP key (wrong agent / wrong scope) | 403; agent loop logs at debug, keeps trying. No alert. |
| Flapping agent | Cooldown (`degraded_cooldown`, 30 min) + transition-only emit = at most one lost alert per cooldown window. |
| Agent deleted / renamed | `clear_heartbeat()` removes all three keys; no leaked `seen` key, no orphaned old name. |
| Multiple backend workers | Atomic `INCR` still makes exactly one caller observe `== THRESHOLD`, so the degraded transition stays single-fire (threshold reached in fewer wall-clock ticks). |

---

## Testing

| Test file | Lines | Covers |
|-----------|-------|--------|
| `tests/test_307_heartbeat_endpoint.py` | 136 | Endpoint auth (Option B), `track_usage=False`, `stored` semantics |
| `tests/unit/test_agent_heartbeat.py` | 272 | Agent-side loop: `VmRSS` parse, payload build, gating, exception swallowing |
| `tests/unit/test_heartbeat_service.py` | 521 | State machine, bulk pipeline, `process_watch_tick` miss-counting + transitions, clear |
| `tests/unit/test_heartbeat_alerts.py` | 104 | `alert_heartbeat_lost` / `alert_heartbeat_recovered` cooldown + dispatch |
| `tests/unit/test_mcp_key_track_usage.py` | 195 | `validate_mcp_api_key(track_usage=False)` skips the usage UPDATE |

---

## Security Considerations

1. **Least-privilege auth (Option B)**: agents heartbeat with their own
   agent-scoped MCP key; no master internal secret is injected into agents.
2. **Self-only**: the key's bound `agent_name` must equal the path —
   one agent cannot heartbeat (or impersonate the liveness of) another.
3. **No usage amplification**: `track_usage=False` keeps the 12×/min beat from
   inflating `usage_count` or thrashing SQLite.
4. **Redis ACL**: all ops (`SETEX`/`SET`/`GET`/`INCR`/`EXPIRE`/`DEL`/pipeline)
   are within the backend `-@dangerous` ACL and follow the `agent:*` convention;
   agents physically cannot reach Redis (network topology, #589).
5. **No sensitive data**: the payload is `{memory_mb, active_executions,
   uptime_s}` only.

---

## Related Flows

- **Agent Monitoring (Health)** ([agent-monitoring.md](agent-monitoring.md)) —
  the authoritative 30s aggregate-status loop this layer is additive to; it
  consumes the `heartbeat_*` annotations on `GET /api/monitoring/status`.
- **Agent Notifications** ([agent-notifications.md](agent-notifications.md)) —
  the NOTIF-001 path that delivers heartbeat-lost/recovered alerts.
- **WebSocket Event Bus** ([websocket-event-bus.md](websocket-event-bus.md)) —
  alert broadcast transport.
- **Agent Lifecycle** ([agent-lifecycle.md](agent-lifecycle.md)) /
  **Agent Rename** ([agent-rename.md](agent-rename.md)) — the delete/rename
  sites that call `clear_heartbeat()`.

---

## Revision History

| Date | Changes |
|------|---------|
| 2026-05-30 | Initial documentation for RELIABILITY-004 / #307 — agent push-heartbeat liveness layer (agent-side 5s loop, `heartbeat_service.py`, `POST /api/agents/{name}/heartbeat`, watch loop with 3-miss guard + soft alerts, Redis key family, Option-B auth, `track_usage=False`). |
