# Feature Flow: Per-Agent Dispatch Circuit Breaker (RELIABILITY-007, #526)

> **Status**: Shipped 2026-05-30. Default-OFF behind a global master switch +
> per-agent opt-in (true opt-in canary).

## Problem

Trinity's dispatch path had a backlog **depth** cap but no **health** gate. When
an agent wedges (expired Anthropic key, OOM loop, broken push stream) it answers
HTTP 503, every execution fails after the detect window while the slot is held,
and the persistent backlog fills with up to `max_backlog_depth` doomed tasks. A
user could wait ~25 min for their *first* failure, and producers had no signal
to back off.

## Goal

A per-agent **producer-side** breaker at the dispatch layer that fast-fails new
executions with **HTTP 503** when an agent is auth-dead (instead of poisoning
the backlog), fails the doomed backlog immediately, and self-heals via a
half-open probe.

## Two breakers, separate jobs

| Breaker | Module | Redis key | Counts | Owns |
|---------|--------|-----------|--------|------|
| **Transport** (#631) | `services/agent_client.py` (`CircuitState`) | `agent:circuit:{name}` | `ConnectError`/`ConnectTimeout` only (#474) | *unreachable* |
| **Dispatch** (#526) | `services/dispatch_breaker.py` (`DispatchBreaker`) | `agent:dispatch:{name}` | `error_code == AUTH` only (D10) | *auth-dead* |
| *(seam)* heartbeat (#307) | calls `DispatchBreaker.record_failure("missed_heartbeat")` | — | missed heartbeat | *wedged* |

Separate namespaces + separate Lua → neither contaminates the other's counter
(`AgentClient.record_success()` HTTP-200 never touches the dispatch breaker).
Both share the Redis plumbing in the **top-level** `redis_breaker_util.py`
(fail-open client, Lua `ScriptCache`, decode helpers, `fail_open` wrapper).

**Why `redis_breaker_util.py` is top-level (not `services/`)**: `agent_client.py`
is loaded *standalone* via `importlib` in both its unit and integration test
suites to bypass the heavy `services/__init__.py`. A `from services.X import`
there would re-trigger that package init and break both suites (IRON RULE R1). A
top-level leaf module resolves against `src/backend` on `sys.path` in every
context — prod, unit, integration — exactly like the `config`/`database` imports
`agent_client` already uses.

## Failure signal — AUTH only (D10)

The dispatch breaker counts only execution outcomes whose `error_code == AUTH`
(set at `task_execution_service.py` on an agent HTTP 503). It does **NOT** count
TIMEOUT (long legit tasks / bad prompts) or AGENT_ERROR (user-caused, and never
actually assigned in code). The consecutive threshold (default 3) gives SUB-003
subscription auto-switch two attempts to self-repair before the breaker trips.
Consecutive (not rolling-window) because a key-dead agent fails back-to-back and
the existing `CircuitState` Lua already solves half-open + probe-lock (D9).

## End-to-end flow

```
caller reads {max_parallel_tasks, circuit_breaker_enabled}
          │
routers/chat.py
   /chat (~160)   : PURE-READ gate — DispatchBreaker(agent).to_dict()["state"]=="open"
                    ─► 503 immediately (does NOT consume a probe; /chat never feeds
                    the breaker, so it must not drain its recovery probe — #526 F1).
                    Nothing dispatched ─► fail(idem) releases the idempotency claim
                    so the same key can retry once the breaker recovers (#1051).
                    acquire() runs WITHOUT breaker_enabled.
   /task (async ~1127, sync ~1241): acquire(..., breaker_enabled=cb_enabled)
          │
CapacityManager.acquire (services/capacity_manager.py)
   0. breaker_enabled=0 OR global DISPATCH_BREAKER_ENABLED off ─► skip gate (zero cost)
   1. DispatchBreaker(agent).allow_dispatch()?  ── deny ─► raise CircuitOpen(retry_after)
   2. if breaker open (we hold the half-open PROBE): admit ONLY into a free slot;
      slots full ─► raise CircuitOpen (NEVER enqueue). Guarantees the probe leads to
      a recorded dispatch instead of a verdict-less backlog row that stalls backoff
      (#526 F1). No-enqueue invariant now spans the half-open window too.
   3. closed breaker: acquire_slot() (unchanged) ─► overflow policy
          │                                   (CircuitOpen raised BEFORE overflow → NO enqueue)
   routers/chat.py  : except CircuitOpen ─► mark pre-created /task row FAILED(circuit_open)
                                            + HTTP 503 + X-Circuit-Open + Retry-After
   task_exec_service: except CircuitOpen ─► TaskExecutionResult(CIRCUIT_OPEN) + FAILED row
                      AUTH terminal  ─► DispatchBreaker.record_outcome(AUTH)  ┐
                      success terminal ─► record_outcome(None)                │
                                                                              ▼ on →open (caller backgrounds)
                      _spawn_bg(_fail_backlog_and_audit(agent)) =   # strong ref held → no GC mid-flight
                          db.fail_queued_for_agent(agent,"circuit_open")  → status FAILED
                          + CapacityManager.clear_in_memory_queue
                          + platform_audit_service.log(circuit_breaker_open)
                      (if that task is still lost → 60s breaker-aware run_maintenance
                       sweep re-fails any still-open breaker's backlog: ~60s, not 24h)
```

### Probe double-consumption guard

`allow_dispatch()` consumes the half-open probe (SET-NX-EX lock). The
`task_execution_service` 3b pre-dispatch check therefore uses a **non-probe
state read** (`to_dict()["state"] == "open"`) and only on the backlog-drain
path (`slot_already_held and not dispatch_gate_checked`). Router pre-acquire
sites pass `dispatch_gate_checked=True`; the not-`slot_already_held` path already
gated at `acquire()`. This guarantees the 3b check never blocks a probe an
upstream `acquire()` gate already admitted, so router-only agents still self-heal.

## Surfaces

- **DB**: `agent_ownership.circuit_breaker_enabled INTEGER DEFAULT 0`
  (migration `agent_ownership_circuit_breaker`, mixin getters/setters in
  `db/agent_settings/resources.py`).
- **Config**: `DISPATCH_BREAKER_ENABLED` env (default `false`) — global master switch.
- **Operator API**: `GET/PUT /api/agents/{name}/circuit-breaker` (state / owner-gated toggle);
  `POST /api/agents/{name}/circuit-breaker/reset` resets BOTH breakers (admin).
- **Health**: `circuit_breaker` block on `GET /api/monitoring/agents/{name}`.
- **Dashboard**: `circuit_breakers` field on `GET /api/agents/slots` (pipelined
  HGETALL over the known agent list — no keyspace SCAN; only OPEN breakers).
- **Frontend**: distinct ⚡ "circuit open" danger badge in `AgentNode.vue`
  (dashboard graph, fed by `/slots`) and `AgentHeader.vue` (detail page, fed by
  the per-agent `/circuit-breaker` fetch in `stores/agents.js`).

## Testing

- `tests/unit/test_dispatch_breaker.py` — Transition value object, `record_outcome`
  routing (AUTH→failure, None→success, all-else→ignored), fail-open (fakeredis has
  no EVALSHA → total fail-open), and the CapacityManager gate (CircuitOpen + **no
  enqueue**, R3 acquire-unchanged-when-off). `TestHalfOpenProbeNoEnqueue` pins the
  #526 F1 fix — a half-open probe is admitted only into a free slot and fast-fails
  (never enqueues) when slots are full. Plus the maintenance backstop
  (`_backstop_open_breaker_backlog` fails only open-breaker agents, no-op when the
  global flag is off, never raises) and the drain-on-open wiring
  (`_fail_backlog_and_audit` drains+audits; `_record_dispatch_terminal` spawns the
  drain on →open via the strong-ref `_spawn_bg`, audits recovery on →closed, no-op
  when disabled).
- `tests/integration/test_dispatch_breaker.py` — the Lua state machine against
  real Redis: threshold open, success reset, ignored outcomes never move state
  (R2), retry_after, half-open probe close, probe-fail backoff growth, heartbeat
  seam, `get_all_dispatch_states`/`get_dispatch_states_for`/`reset_dispatch`, and
  the `record_success` no-op guard (skips the HASH write when already closed+0,
  still resets accumulated sub-threshold failures).
- `tests/integration/test_fail_queued_for_agent.py` — sets FAILED (not CANCELLED),
  leaves running + other-agent rows untouched.
- **Regression guards**: R1 — `tests/{unit,integration}/test_circuit_breaker.py`
  stay green after the `redis_breaker_util` extraction; R2 — non-AUTH outcomes
  don't trip; R3 — `acquire()` unchanged when `breaker_enabled=False`.
- **E2E**: `src/frontend/e2e/circuit-breaker-badge.spec.js` — badge renders
  distinctly (route-mocked `/circuit-breaker`).
- **Staging end-to-end (manual)**: on a sibling stack (`-p trinity-526-test`,
  remapped ports) with `DISPATCH_BREAKER_ENABLED=1` and a per-agent opt-in:
  expire the agent's key → repeated `/task` → ~3 consecutive AUTH → breaker opens
  → 503 + `X-Circuit-Open`/`Retry-After` → backlog rows FAILED(circuit_open) → fix
  key → half-open probe → closed. (Mechanism is covered piecewise by the
  Redis-level integration + capacity-gate + fail_queued automated tests.)

## Known limitations (by design)

- **Fail-open disables the breaker during a Redis outage (F3).** Every breaker
  op is fail-open: with Redis unreachable, `allow_dispatch()` returns `allow`,
  `to_dict()` reports `closed`, and the 60s `run_maintenance` backstop no-ops.
  So during a *simultaneous* Redis-down **and** auth-dead-agent incident, new
  work is NOT fast-failed and the doomed backlog is NOT drained — the protection
  is silently off for the duration. This is deliberate: failing **closed** would
  let a transient Redis blip halt all dispatch fleet-wide, a worse failure mode
  than the one the breaker guards. The transport breaker (#631) shares the same
  fail-open posture. Mitigation if ever needed: a local in-process fallback
  counter, explicitly out of scope here.

- **Consecutive-failure model misses *intermittent* auth death (F5).** The
  state machine (D9) resets the counter on ANY success, so an agent that flaps
  between healthy and auth-dead — e.g. one good subscription interleaved with a
  dead key, or a key that intermittently 503s — can keep the consecutive AUTH
  count below the threshold and never trip. This is acceptable for the target
  case (a *fully* auth-dead agent fails back-to-back and trips reliably within
  `threshold` dispatches) but a partially-degraded agent is not protected. The
  fix path is the rolling-window / per-cause counter already listed under NOT in
  scope.

## NOT in scope (deferred — file follow-up GitHub issues)

- **#307 heartbeat producer** — owned by #307; #526 ships only the
  `record_failure("missed_heartbeat")` seam.
- **TIMEOUT / AGENT_ERROR as health signals** — excluded (D10) to avoid false
  trips; revisit only with a taxonomy that separates agent-health from
  user-caused failures.
- **Transport-breaker 503 unification** — transport-open still returns
  200 + error post-slot.
- **Rolling-window / per-agent numeric thresholds** — consecutive + global
  tunables only (D9).
- **Per-agent breaker history/metrics UI** — only live state is surfaced.
