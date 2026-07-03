# Trinity — Target Architecture

**Created:** 2026-05-05
**Updated 2026-07-01 (v2 — side-effect reframe):** v1 tried to make re-delivery *safe* by threading a platform-injected idempotency key through every outbound sink (effect-key **coverage**, "Direction A"). This version abandons *universal* sink coverage as structurally unachievable (the agent's own Resend key / `gh` / `curl` / any new MCP server emit effects the platform never routes) and replaces it with **retry-with-prior-trace recovery** + **deterministic tool-side gates on capability-confined irreversible rails** ("Direction B"). `#1084` is re-scoped from *"the gate that blocks pull"* to a **recovery discipline**; pull default-on now gates **per-effect, not per-agent**, so read/analysis-only + reversible + confined-irreversible agents can default on immediately. The reactive channel-reply and git-sink coverage work is **downgraded** (reversible / naturally-idempotent, not gate-blockers); the load-bearing problem **moves** to *trace fidelity* (the #548/#333 reader-race family) — it does not disappear. Tracked by new sub-issues of #1081: **#1401** (structured recovery trace + injection) and **#1402** (async operator-queue human-gate lever). The effect-key issue **#1084** (Direction A) is superseded in approach and **awaits re-scope**; the tool-side confined-rail gate is **not yet filed**. The prior v1 is archived at `docs/archive/plans/TARGET_ARCHITECTURE_v1_2026-06-06.md`. See §"Re-Delivery and Side-Effect Recovery" and Open Question 2a. All other sections carry forward from v1 unchanged.
**Updated:** 2026-06-05 — Coordination model revised from a **push actor model** (backend dispatches into a per-agent Redis-stream mailbox) to **pull / work-stealing** (backend owns one durable per-agent queue; agents pull when they have free capacity). The change was driven by an adversarial design review across four coordination architectures: pull scored highest on simplicity, operational complexity, and reachability while delivering the same goals (async-first, the two operator levers, single-source-of-truth, replica groups) with ~80% reuse of existing primitives. See "Coordination Model" below. Tracked in GitHub under Epic #1045: umbrella #1081 (pull migration), #1082 (status-as-projection), #1083 (fire-and-forget dispatch), #1084 (effect-scoped idempotency), #1085 (correlated-failure controls). The governing principles, data layer, observability, and security sections are otherwise unchanged. **Stack trimmed the same day** to match the lean coordination model: Celery/Celery Beat rejected for APScheduler on a PostgreSQL job store (pull made a second competing-consumers system redundant — #949); PgBouncer and a read replica demoted to deferred scaling levers; Prometheus/Grafana framed as an opt-in observability profile. PostgreSQL is the one non-negotiable new service. **Updated 2026-06-06** — folded in three result-contract tightenings surfaced by the execution-bug meta-analysis (`ORCHESTRATION_BUG_META_ANALYSIS_2026-06.md`): a typed terminal-reason on the reply envelope, an agent-owned out-of-band result record, and credential rotation via hot-reload. The coordination model is unchanged — these close the residual MISCLASSIFIED_FAILURE / READER_RACE / credential-recreate seams that pull alone does not address.
**Status:** Living document — review quarterly, update on major architectural decisions
**Purpose:** Describes the optimal steady-state design Trinity should converge toward. This is not a migration plan — it is the destination. Use it to evaluate tradeoffs, prioritize work, and reject changes that move away from it.

---

## What This Document Is and Is Not

**Is:** The architectural vision for a 200+ agent fleet running reliably on self-hosted hardware, assuming no resource constraints.

**Is not:** A rewrite proposal. The current platform stays load-bearing throughout the transition. Every component described here has a reachable path from what exists today.

---

## Governing Principles

These rules take precedence over all other considerations. When in doubt, measure a proposed change against them.

1. **Simplicity over cleverness.** A boring solution that works beats an elegant solution that requires understanding. Fewer moving parts means fewer failure modes.
2. **One source of truth per domain.** Never split the authoritative state for an entity across two stores. Pick one; the other is a projection or a cache.
3. **Async-first communication.** No component blocks waiting for another to respond. Sync semantics are thin edge adapters over async internals — not a core design choice. This includes the human: an effect or task that needs a person is parked into the operator queue asynchronously, never a blocking in-turn prompt (see Principle #8 and #1402).
4. **Proven primitives.** Use PostgreSQL for relational state, Redis for ephemeral/event state, Docker for isolation. Resist building custom solutions for problems these tools already solve.
5. **Pull-based work-stealing as the coordination shape.** The platform owns one durable per-agent work queue. Each agent pulls the next task only when it has free capacity, runs it, and reports the result back — the platform never pushes work into a busy or unhealthy agent. Capacity is a *physical* property of the agent (the size of its worker pool), not a distributed counter. The authoritative execution state ("what is queued" and "what is running") lives in exactly one store — the agent computes results but does not own a parallel copy of that state. This is the industry-standard competing-consumers pattern (Celery, Sidekiq, GitHub Actions runners, Temporal workers); it is chosen over a custom actor framework precisely because it is boring and proven (see Principle #1, #4).
6. **Sovereign infrastructure.** Trinity runs on hardware the operator controls. Design decisions must work on a single commodity server, not require cloud dependencies or managed services.
7. **Data exchange over conversation chains.** Agents composing via structured files, queues, and typed outputs is more reliable and testable than chaining conversations. Async data handoffs — shared folders, repo commits, scheduled queue tasks — are the default composition pattern. Direct agent-to-agent conversation is an edge adapter for cases where no data-exchange pattern fits.
8. **Recovery lives where the context is (v2).** The platform detects failure and re-delivers; it does not *police* what an agent does to the outside world. The semantic intent of a turn ("I was two steps into a three-step booking") exists only at the agent, so the agent owns its own recovery from hindsight — informed by a platform-injected trace of the prior failed attempt. The platform's contribution is (a) at-least-once delivery, (b) that trace, and (c) deterministic gates on the short list of irreversible rails it *solely* fronts. This is the same read-surface / agent-owns-the-work discipline as CLAUDE.md §8 (Trinity ≠ DAG engine), applied to failure recovery.

---

## System Topology

```
┌─────────────────────────────────────────────────────────────────────────────────────┐
│                              Trinity Target Architecture                              │
├─────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                       │
│   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐   ┌───────────────────┐   │
│   │   Frontend   │   │   Backend    │   │  MCP Server  │   │    Scheduler      │   │
│   │   (Vue.js)   │   │  (FastAPI)   │   │  (FastMCP)   │   │   (APScheduler)   │   │
│   │   :80        │   │   :8000      │   │   :8080      │   │   (internal)      │   │
│   └──────┬───────┘   └──────┬───────┘   └──────┬───────┘   └────────┬──────────┘   │
│          │                  │                   │                    │               │
│          └──────────────────┼───────────────────┼────────────────────┘               │
│                             │                   │                                     │
│              ┌──────────────┼───────────────────┤                                     │
│              │              │                   │                                     │
│       ┌──────┴─────┐  ┌─────┴──────┐   ┌───────┴──────┐                             │
│       │ PostgreSQL │  │   Redis    │   │   Docker     │                             │
│       │  (primary) │  │(event bus/ │   │   Engine     │                             │
│       │  :5432     │  │ ephemeral) │   │              │                             │
│       │            │  │  :6379     │   └──────┬───────┘                             │
│       │            │  └────────────┘          │                                     │
│       │            │                          │                                     │
│       └────────────┘     ┌───────────────────┬┴──────────────────────┐              │
│                          │                   │                       │              │
│                    ┌─────┴───┐          ┌────┴────┐            ┌─────┴────┐         │
│                    │ Agent 1 │          │ Agent 2 │            │ Agent N  │         │
│                    │ :8000   │          │ :8000   │            │ :8000    │         │
│                    └─────────┘          └─────────┘            └──────────┘         │
│                                                                                       │
│   Platform Network (172.29.0.0/16): postgres, redis, scheduler, vector    │
│   Agent Network (172.28.0.0/16):    agents, backend, mcp-server                     │
│                                                                                       │
│   Observability Plane (opt-in profile):                                            │
│   OTEL Collector → Prometheus → Grafana                                               │
│   Vector → structured log files                                                       │
└─────────────────────────────────────────────────────────────────────────────────────┘
```

---

## Data Layer

### PostgreSQL (Primary Relational Store)

Replaces SQLite as the authoritative store for all durable relational state.

> **SQLite end-of-support: September 1, 2026** (decision #1278). PostgreSQL is the
> forward path; SQLite stays the zero-config default until then. Operators migrate
> via `docs/migrations/SQLITE_TO_POSTGRES.md`. The dual-track migration system
> (#1183) and single-source schema consolidation (#746) carry the transition.

**What lives here:**
- All current SQLite tables (users, agents, schedules, executions, chat history, audit log, activities, subscriptions, skills, tags, operator queue, sync state)
- **The per-agent work queue and execution state machine** — `schedule_executions` carrying the lifecycle `queued → claimed → running → terminal`. This single table is the sole owner of both "what is waiting" (lever 1: inbox depth) and "what is running" (the fact the old slot-ZSET/SQL split forced the cleanup+canary machinery to reconcile). The atomic claim (`UPDATE … WHERE id = (SELECT … ORDER BY queued_at LIMIT 1) RETURNING`) is the competing-consumer primitive that lets N agent workers — and N replicas — pull without overbooking.
- Partitioned tables for high-volume, time-series data: `audit_log`, `agent_activities`, `chat_messages`, `agent_session_messages` — partitioned by month, retained per configured window
- Schema migrations via Alembic (versioned, reversible, automated on startup)

**Access pattern:**
- Backend and scheduler connect through **asyncpg's built-in connection pool**. **PgBouncer (transaction pooling) is a deferred scaling lever** — added only when process count (multi-replica backend, prefork workers) outgrows PostgreSQL's connection ceiling, not before.
- Read-heavy endpoints (activity timeline, execution history, audit log queries) run against the primary today; a **read replica is a deferred scaling lever** for when the platform becomes genuinely read-bound.
- No direct PostgreSQL connections from agent containers — agents communicate only through the backend API

**Why PostgreSQL over SQLite:**
SQLite's single-writer constraint is acceptable for embedded use; it is not acceptable when the backend, scheduler, and future services all write execution state concurrently under load. PostgreSQL provides row-level locking, concurrent writes, replication, and point-in-time recovery — without introducing a second store or split-brain risk. It is the proven, boring answer for this class of problem.

### Redis (Ephemeral and Event State)

Redis owns state that is either transient or derivable from the primary store.

**What lives here:**
- **Event bus**: Redis Streams (`trinity:events`) — all real-time delivery, WebSocket fan-out
- **Queue wake-up hints**: a lightweight notify-on-enqueue signal so a long-polling agent worker wakes immediately instead of waiting out its poll interval. This is a *hint*, not the queue — the authoritative queue is the PostgreSQL `schedule_executions` table. A lost hint costs latency (the worker picks the task up on its next poll), never correctness.
- **Session tickets**: WebSocket auth (30s TTL, single-use GETDEL)
- **Rate limiting**: sliding window counters per endpoint
- **Distributed locks**: Redis SET NX EX for critical sections (session resume serialization, credential writes)

**What does not live here:**
- Chat history, execution records, user data, audit log — these are PostgreSQL
- **The work queue and execution state** — these are PostgreSQL; Redis holds only the wake-up hint, never the authoritative queue or the "is X running" fact. (This is the deliberate departure from the old design, where a per-agent capacity ZSET and a mailbox STREAM split that authority across Redis + SQLite + agent RAM and forced continuous reconciliation.)
- Redis is never the source of truth for anything a user would expect to retrieve after a restart

### Clean Separation Rule

| Concern | Store |
|---------|-------|
| Durable entity state | PostgreSQL |
| **Work queue + execution state ("queued"/"running")** | **PostgreSQL** (`schedule_executions`) |
| Time-series / append-only events | PostgreSQL (partitioned) |
| Real-time delivery | Redis Streams |
| Queue wake-up hint (not the queue) | Redis |
| Ephemeral coordination | Redis |
| Secrets (transient) | Redis (encrypted values) |
| Credentials (durable) | Agent `.env` files (encrypted at rest) |

---

## Backend API

### What Stays the Same

- **FastAPI** as the framework — correct choice, keep
- **Three-layer invariant**: Router → Service → DB — never violated
- **Pydantic models centralized in `models.py`** — keep
- **`Depends(get_current_user)` auth pattern** — keep
- **Modular router structure** (`routers/`) — keep
- **Channel Adapter ABC** (`adapters/base.py`) — keep
- **OTEL auto-instrumentation** — keep and extend

### What Changes

**Database driver**: `asyncpg` + SQLAlchemy 2.0 async ORM replaces `aiosqlite` and direct SQLite calls. The DB layer interface (`db/` modules) remains the same from the router/service perspective — only the implementation changes.

**Schema migrations**: Alembic replaces the custom `migrations.py`. Migrations run automatically on startup, are versioned, and are reversible.

**Rate limiting**: Redis-backed sliding window replaces ad-hoc per-endpoint implementations. One middleware, one configuration, consistent behavior across all endpoints.

**Idempotency keys**: All state-mutating endpoints accept an `Idempotency-Key` header. The backend deduplicates within a 24h window using Redis. This makes retries safe — critical for webhook triggers, scheduled executions, and agent-initiated calls. (See also Sprint D′ #525.)

**API versioning**: `/api/v1/` prefix introduced for all endpoints. `/api/` aliases preserved for backward compatibility. New capabilities go on `/api/v1/` only.

---

## Coordination Model (Pull / Work-Stealing)

This is the central architectural shift. The full rationale is in `docs/archive/plans/ORCHESTRATION_RELIABILITY_2026-04.md` and the 2026-06-05 design review — this document states the destination shape.

**The one-line model:** the platform never pushes work at an agent. It writes every task as a durable row in one per-agent queue; the agent's own worker pool *pulls* the next task whenever it has a free worker, runs it, and reports the result back. A busy, overloaded, or dead agent simply stops pulling — so a task is never handed to an agent that isn't ready (the failure mode that caused most of today's instability).

### The Model

**1. Queue (backend-owned)** — one durable FIFO per agent, the `schedule_executions` rows with status `queued`, ordered by `queued_at`. Every producer — scheduler, webhook, human chat turn, agent-to-agent call — writes here and nothing else. There is one writer of "queued" (the backend) and one place it lives (PostgreSQL). This is the single source of truth the old mailbox-STREAM + slot-ZSET + agent-RAM split failed to provide.

**2. Pull endpoints (backend, internal)** — `GET /api/internal/next-task` (long-poll, woken by the Redis hint) hands the caller the head row via the atomic claim (`UPDATE … status='claimed', lease_expires_at=…, claimed_by_worker=… WHERE id=(SELECT … ORDER BY queued_at LIMIT 1) RETURNING`); `POST /api/internal/tasks/{id}/result` applies the terminal result under a compare-and-set guard. Both sit behind `X-Internal-Secret` — agents reach them over the backend API only, never touching Redis/PostgreSQL (Invariant #589 honored trivially: there is no agent-side store to own).

**3. Worker pool (agent-side)** — the agent-server runs `N = max_parallel_tasks` worker coroutines. A worker long-polls `next-task` *only when idle*, runs the (unchanged) Claude turn, then POSTs the result. **Capacity is therefore physical**: the agent literally cannot run more than N tasks because it has N workers — overbooking is structurally impossible, not policed by a counter. This is also why a hung turn (e.g. a wedged MCP call) consumes an *agent* worker, not a backend resource, and there is no dispatch breaker to trip and cascade.

### The Two Operator Levers

The model exposes exactly the two control surfaces an operator needs to run a fleet, each with a single authoritative owner:

- **Lever 1 — Inbox depth** = `COUNT(*) WHERE agent_name=? AND status='queued'`, a single indexed read owned by the backend. Rising depth is the signal to add capacity.
- **Lever 2 — Capacity** = `agent_ownership.max_parallel_tasks`, enforced physically by the agent's worker count. Changing it takes effect on the next pull (workers grow immediately; shrink lets in-flight finish then stops re-polling) — no container restart.

Depth is what's waiting; capacity is how fast it drains. They are mechanically coupled with zero distributed-counter glue.

### Message Envelope

Every queued task — and every agent-to-agent message — carries this envelope (stored in the row's payload column):
```json
{
  "id": "<uuid>",
  "kind": "chat | task | event | reply",
  "from": "<agent_name> | <user_id> | system",
  "to": "<agent_name>",
  "correlation_id": "<uuid>",
  "causation_id": "<parent_message_id>",
  "idempotency_key": "<opaque_string>",
  "deadline": "<iso8601>",
  "payload": {}
}
```

The envelope is the unit of enqueue, re-delivery, and deduplication.

**Typed terminal-reason on the `reply` payload.** A `reply` envelope's `payload` MUST carry a typed terminal outcome the agent produces — `{ "status": "success | failed", "error_code": "AUTH | TIMEOUT | OOM | MAX_TURNS | AGENT_ERROR | NETWORK | …", "cost", "tokens", "session_id" }` — **not** a reason the backend infers from exit codes or stderr substrings. This is the structural cure for the MISCLASSIFIED_FAILURE class (the auth-substring classifier re-patched 5+ times across three hand-synced container copies, where every new kill/OOM shape carrying an auth substring re-triggered a false subscription switch): the platform reads a typed field instead of guessing. Pinning this taxonomy is part of the #945 envelope-payload-schema work — it is a coordination-contract requirement, not an agent implementation detail.

### Recovery: Lease-Expiry Re-Delivery (the only recovery primitive)

"Let it fail" is implemented as exactly one move, with no defensive partial-work rerun, no blind timeout-retry, no reconciliation repair:

- Every claimed/running row carries `lease_expires_at` (= `execution_timeout_seconds` + a grace buffer). A heartbeat from the worker *renews* the lease, so a legitimately long turn is never reaped out from under itself.
- A single **lease-reaper** sweep flips any expired claimed/running row back to `queued`. Any idle worker — the restarted agent, or a replica — re-pulls it. This one sweep replaces the ~5 reconciliation sweeps and the slot-ZSET watchdog that exist today.
- Re-delivery reuses the **same `execution_id` and the same `idempotency_key`** (RELIABILITY-006 / #525), so a duplicate result POST is absorbed by the compare-and-set guard, and a re-pulled task is the same unit of work, never a half-finished turn resumed. **(v2)** A re-pulled task is not a *blind* re-run: the backend injects the prior failed attempt's **structured recovery trace** into it so the retried turn recovers from hindsight — see §"Re-Delivery and Side-Effect Recovery" (#1401).

Crash taxonomy, all recovered by that one move: agent/container/OOM death mid-turn → lease expires → re-queued; backend restart → in-flight long-polls drop, agents re-poll on reconnect, committed queue rows intact; container recreate (e.g. subscription auto-switch) → the queue lives in the backend, so at most the active turn is lost, recovered by lease expiry (and upgraded to a clean drain by a pre-recreate "stop pulling, finish in-flight" handshake). A re-delivery cap is *intended* to park a poison task as `FAILED(MAX_REDELIVERY)` into the operator queue so it cannot loop forever — but that cap, the lease-reaper it bounds, and the auto-park-to-operator + alert path are **all unbuilt today** (the existing `retry_count` column is #678's reader-race auto-retry, not a lease cap), so a maxed-out task raises no operator signal yet. **(v2)** This park is now *doubly* load-bearing: it is also the landing spot for the irreversible-and-un-confineable effect class (§"Re-Delivery and Side-Effect Recovery"), so building it is a prerequisite for defaulting any effect-bearing agent on — tracked as **#1402**.

### Re-Delivery and Side-Effect Recovery (retry-with-context)

Re-delivery is safe **at the coordination boundary** but is **not** automatically safe for the agent's external side effects: a turn that sends an email / posts to Slack / charges a payment / pushes a commit and *then* crashes before reporting its result will, on a **blind** re-run, repeat that effect. Under Invariant #589 the agent's local "I finished" write and the backend's "complete" write are on two different machines and can never be one transaction, so **exactly-once external effects are unattainable at the platform layer.** This is a fixed constraint — the two-generals impossibility — not a bug to close. v1 tried to route around it with a platform-injected idempotency key threaded through every outbound sink; v2 abandons *universal* sink coverage (the surface — the agent's own Resend key, `gh`, `curl`, any new MCP server — is unbounded and un-confineable) and makes two moves instead.

**1. Re-delivery carries prior-trace context (the general recovery mechanism, #1401).** A re-queued task is still a **fresh turn, never a resumed process**, but the backend injects the **structured trace of the prior failed execution** into it. The same record is also injected into the **next** execution (a new `execution_id`, e.g. the next cron tick), so a subsequent run has continuity — it does not redo finished work and can build forward. The retried/next turn runs *with hindsight* and recovers itself:
- **Continue** — the failure was legibly *before* any external effect (a network blip, a timeout, an internal step that never reached a sink). Safe to pick up where it left off.
- **Fail gracefully** — a precondition is genuinely unmet or the work keeps breaking. Report, stop thrashing.
- **Verify-before-redo** — a request left the container but no confirmation was recorded. Never blindly redo; this is the only branch that can cause a duplicate, and it only matters for irreversible effects.

Recovery decisions live at the agent because that is the only place the semantic intent exists (Principle #8). The trace it reads must be:
- **Three-state** — every side-effecting step is `done` / `not-done` / `unknown`. The classic bug is collapsing `unknown` into `failed`: an errored/timed-out call whose effect actually landed, re-run because the trace said "failed." For any irreversible effect, an errored or timed-out call is recorded as **`unknown` (verify)**, never `failed` (safe to retry).
- **Write-ahead for irreversible effects** — log "about to charge $X, target=Y" *before* the call, so the trace can never show a silent hole; worst case it shows "attempted, unconfirmed," which routes the turn to *verify* rather than *redo*. (This is the outbox pattern, done at the agent level — internal, cheap, crash-safe.)
- **Structured** — a step/outcome record the agent can locate its position in, not raw `stdout` prose it must re-derive intent from.

Building this trace reliably — **including effects the agent performs through its own channels** — is the load-bearing prerequisite for the whole model. It is the same agent-owned record surface as §Agent Runtime "Result reporting" and the `post-check` hook, and its fidelity gate is the #548/#333 reader-race family. See Open Question 2a.

**2. Irreversible effects are gated deterministically at the Trinity-owned MCP tool — not by agent judgment.** For the short list of irreversible rails Trinity *solely fronts* (Nevermined settle today; any future money rail), the deterministic guarantee lives **in the MCP tool that fronts the rail**, not scattered across sinks and not in the LLM's discretion:
- The tool applies `effect_guard` plus, where the provider offers one, a **native idempotency token** (e.g. Stripe's `Idempotency-Key`, Nevermined's `agent_request_id`) — so the **provider** enforces exactly-once *at the boundary*. Where no native token exists the tool does a mechanical check-then-act verify (a small window, still deterministic, still not the agent guessing).
- The agent never decides "verify vs redo" for money — the tool does, mechanically, keyed on **resolved, immutable identity** (recipient + account + channel + an explicit `dedup_label`), the LLM-generated body structurally **absent** so a re-run with reworded content still dedups.
- **This is sound only under capability confinement:** the rail must be reachable *only* through the gated tool, with the agent holding **no direct credential** for it. If the agent can reach the rail another way (its own key, a raw `curl`), the tool-side gate is theater — it double-charges around it. Confinement, not the gate, is the real boundary.

`effect_guard` (merged to dev under #1084, 4 sinks) is **kept here** — on Trinity-confined irreversible tools — and #1084's universal-coverage ambition is **retired**. Chasing a key path for every possible sink stops. *(The tool-side deterministic gate as its own workstream is **not yet a filed issue** — a known v2 gap; see Open Question 2a.)*

**Gate the effect, not the agent.** Auto-re-delivery is decided per *effect class*, not per agent:

| Effect class | Auto-re-delivery |
|---|---|
| **Read/analysis-only**, and **reversible effects** (Slack post, apologisable email, notes, most tool calls) | **Default-on.** Trace-aware retry makes a duplicate rare; a duplicate is survivable, so no platform keying is required. |
| **Irreversible + reachable only via a confined Trinity tool** (payment) | **Default-on.** The tool-side native-key gate makes "verify before redo" mechanical. |
| **Irreversible + un-confineable** (own email key / `gh` / `curl`, no gated tool in the path) | The *specific effect* is **human-gated** — parked into the **asynchronous operator queue (#1402)**, never a synchronous in-turn block; the agent stays auto-re-deliverable for everything else. |

The unit of gating is the **effect**, so an agent is no longer coarsely "auto-recoverable or not"; only its genuinely irreversible-and-un-confineable actions gate. This removes the "everyone waits for full sink coverage" bottleneck that blocked the pilot.

**No synchronous user gates (#1402).** The human gate is the **asynchronous operator queue** (OPS-001), not a blocking in-turn prompt: a turn that needs a human parks a question/approval and **ends** — it never holds a worker waiting on a person (that would pin a worker and violate Principle #3). Resolution re-enters asynchronously (a new execution, or the operator-queue respond → re-trigger path). **Agents must be authored to treat all user/operator communication as asynchronous — fire-and-park, not block-and-wait** — and this contract is surfaced in the platform system prompt so agent authors build for it explicitly.

**Honest residual.** An irreversible effect that *lands but is never recorded*, on an un-confineable channel the trace cannot see, will still double-fire — the platform cannot route to a human what nothing knows happened. This is the two-generals floor; it is relocated and bounded (to the sink's native idempotency, a confined tool, or a human gate), never eliminated. v2 does not pretend otherwise, and it does not let the agent-level outbox (which makes only the *internal* "I meant to send" leg crash-safe) masquerade as crossing that boundary.

### Async-First Communication

`chat_with_agent` (MCP tool) never blocks. It:
1. Enqueues a `queued` row in the target agent's queue (idempotency key derived from call args)
2. Returns immediately: `{execution_id, status: "queued"}`
3. Caller subscribes to `agent.task.completed` / `agent.task.failed` events via the event bus, or polls `get_execution_result` — the existing polling path remains valid (including the #914 `queued_timeout` contract).

Human-facing chat is the edge adapter: the WebSocket (or a `?wait=true` MCP call) holds open, enqueues, and forwards the reply when the completion event arrives. The user experience is synchronous; the internals are not. The held connection must time out so it never pins a worker. **The operator/human gate is likewise asynchronous:** an effect or task that needs a person is parked into the operator queue (OPS-001) and the turn ends — nothing blocks a worker waiting on a human (#1402).

### Fan-Out With an Explicit Join

`fan_out` enqueues N child tasks and returns a `fan_out_id` immediately — **the coordinator does not hold a worker while waiting** (this is what prevents the deadlock that a naive pull design would hit when a self-fan-out parent waits on children that the same agent's remaining workers must pull). The platform counts the N terminal acks and, when all N are reached, assembles a single **reply envelope** (carrying the `correlation_id`) into the coordinator's queue. The join is a small piece of explicit backend state — accepted honestly, with a canary for stuck joins (parent waiting, child count never reaches N) — not a blocking wait.

### Saga Pattern for Multi-Step Workflows

Long-running workflows spanning multiple agents define a compensation action for each step. If step N fails, steps N-1 through 1 execute their compensations in reverse order. This prevents partial state corruption from stranded mid-workflow executions. Implemented at the orchestration layer — individual agents remain unaware. (Orthogonal to push/pull; unchanged by the coordination revision.) **Complementary to retry-with-context (v2):** sagas handle **cross-agent, multi-step** compensation at the orchestration layer; trace-injection (#1401) handles **single-agent, single-turn** recovery. Different scopes, no overlap.

### Failure Isolation Without a Dispatch Breaker

Pull removes the need for a producer-side dispatch circuit breaker as a *gate*. A dead or wedged agent simply stops calling `next-task`; its queue depth rises (visible on Lever 1) and **zero compute is wasted** — the backend never blocks a thread on a multi-minute turn and never floods a sick agent. The transport breaker for the synchronous edge adapter stays; the per-agent dispatch breaker (#526) is repurposed from a gate into an operator **alert** (depth climbing + no successful results = unhealthy), never a mechanism that fails work pre-emptively. (Note: the `#307` missed-heartbeat → breaker seam is currently *unwired*; pull is the one model that does not depend on it.)

### Replica Groups for Horizontal Scale

When a single container's `max_parallel_tasks` ceiling — bounded by container CPU, memory, and Claude Code concurrency — is below the throughput a capability needs, the agent is deployed as a **replica group**: N container instances backed by one logical agent identity. **Pull makes this nearly free**: the atomic row-claim *is* the competing-consumer primitive, so multiple replica containers pulling the same queue distribute work correctly with no Redis consumer-group machinery, no caller-side routing, and no new platform concept.

**Topology:**
- `agent_ownership.replica_count` (default 1) controls the desired instance count; `replica_count = 1` preserves today's behavior exactly
- One backend queue per agent name — unchanged. All replicas pull from it; the atomic claim guarantees each task goes to exactly one worker on exactly one replica
- Callers address the agent by name. No caller-side dispatch logic. Schedules enqueue once per tick and exactly one replica's worker claims each trigger.

**Shared-state discipline (required before `replica_count > 1`):**
- **Git repo writes**: single-writer election via Redis lock `agent:{name}:git_writer`; only the leader pushes, followers stay read-only
- **Credentials and template files**: read-only after injection — safe to share by image across replicas
- **Replica-safety**: declared in `template.yaml`. Agents that mutate `~/.trinity/` mid-turn, hold long-running in-memory state, or persist pipeline state in the container filesystem cannot opt into `replica_count > 1` without explicit design work.

**Distinct from agent cloning:** cloning produces two siblings with divergent state and forces every caller to choose between them. Replica groups produce one logical agent with N worker pools and zero caller-side routing — one `agent_ownership` row, one credential set, one schedule list, one row in the fleet view.

---

## Agent Runtime

### What Stays the Same

- Docker container per agent — correct isolation model, keep
- `agent-server.py` (FastAPI on port 8000 inside container) — keep
- Claude Code as the execution runtime — keep
- `/api/chat`, `/health`, `/api/credentials/update`, `/api/files` endpoints — keep
- Pre-check hook (`~/.trinity/pre-check`) — keep
- Credential injection via `.env` + `.credentials.enc` — keep

### What Improves

**Pull worker pool**: the agent-server runs `N = max_parallel_tasks` worker coroutines that long-poll the backend's `next-task` endpoint when idle, run the (otherwise unchanged) Claude turn, and POST the result back. This is the agent-side half of the coordination model and is built on the existing in-container asyncio-loop precedent (`auto_sync.py`). Capacity is the worker count; there is no agent-side queue to own.

**Streaming responses**: agent-server supports SSE (Server-Sent Events) on `/api/chat` for real-time token streaming to the frontend. Eliminates the current pattern of waiting for full response completion before delivery.

**Richer health signal**: `/health` returns not just `{status: ok}` but `{status, active_tasks, last_task_at, consecutive_failures}` (#1020). The platform uses this for fleet health scoring and lease-staleness alerting. Note: there is deliberately **no** `mailbox_depth` field — under pull there is no agent-side queue to count; inbox depth (Lever 1) is a backend `COUNT` over the queue table, so the agent never carries a second, reconcilable copy of it.

**Result reporting, not journal-as-truth**: execution state is owned by the backend `schedule_executions` row, applied from the worker's result POST under a compare-and-set guard. The agent *computes* the result; it does not own a parallel authoritative history. An optional `~/.trinity/journal.ndjson` may be kept as a local audit/debug aid, but it is not the source of truth and the platform does not depend on projecting it (this is the deliberate departure from the earlier actor-model design, which made the agent's journal authoritative and thereby reintroduced cross-store reconciliation). **The result *content* the worker reports must come from an agent-owned out-of-band record, not from parsed `stdout`.** Today the authoritative terminal line (`{"type":"result"}`, carrying cost/tokens/turns/session-id) rides the same `stdout` pipe that Claude's grandchild processes inherit (fd 1), so it can be lost or truncated *before* the worker POSTs it (#548/#333) — and lease re-delivery would then re-run a turn that null-everythings the same way. The worker therefore reads its result POST from a durable agent-written record (the recovered JSONL / a result file), which is authoritative *for the result payload the worker uploads* even though the backend row stays authoritative *for execution state*. This is the one deliberate exception to "journal-as-truth": state is the backend's; the uploaded result payload must not depend on a lossy inherited pipe.

**Recovery-trace capture (v2, #1401)**: the *same* agent-owned record surface is where the **structured, three-state (`done`/`not-done`/`unknown`), write-ahead side-effect trace** lives — the record the backend injects into a re-delivered turn *and* the next execution (§"Re-Delivery and Side-Effect Recovery"). Capturing it reliably, **including effects the agent performs through its own channels** (its own email key, `gh`, `curl`), is the load-bearing prerequisite for gating irreversible effects; effects the trace cannot see fall back to the human gate exactly like an un-confineable irreversible one. The fidelity ceiling here is the #548/#333 stdout-inheritance family — the same seam that makes result reporting lossy makes trace capture lossy, so the two problems are solved together, not separately.

**Post-execution hooks**: companion to the existing pre-check hook. `~/.trinity/post-check` runs after every task completion (language-agnostic, shebang-selected). Enables custom alerting, output validation, or state transitions defined by the agent template — and is the natural home for finalizing the recovery trace above.

**Credential rotation via hot-reload, not recreate** ✅ *Implemented (#1089, 2026-06-13)*: rotating an agent's subscription token (auto-switch, manual sub→sub reassignment, key rollover) goes through a dedicated agent-server `POST /api/credentials/reload-token` endpoint and does **not** recreate the container. (A new surgical endpoint, not the existing `/api/credentials/update` — the latter destructively rewrites `.env`/`.mcp.json`; the token is an env-only credential.) "Rotate a credential" and "kill every in-flight turn" are no longer the same operation (#1037) — container recreate is reserved for image/template/auth-**mode** changes, where the pre-recreate "stop pulling, finish in-flight" handshake applies. A writable-layer durable override (`/var/lib/trinity/oauth-token`, read by `startup.sh`) keeps a rotation across a plain restart; recreate self-reconciles to the DB token (fresh layer wipes the override). This removes the credential↔execution collision class structurally instead of recovering from it after the fact. See architecture.md §"Subscription Token Rotation via Hot-Reload".

---

## Scheduling

**APScheduler, backed by a PostgreSQL job store**, remains the scheduler — it moves its job store from SQLite to PostgreSQL and otherwise stays the standalone `scheduler` process it is today. Redundancy, when wanted, comes from running a second instance behind a **Redis-lock leader election** (`SET NX EX`), not from adopting a distributed task framework.

The scheduling data model (agent_schedules, schedule_executions) is unchanged. The interface — cron expressions, manual triggers, webhook triggers — is unchanged. Under the pull model the scheduler is just another producer: on each tick it **enqueues a `queued` task row** (with its `Idempotency-Key: sched:{execution_id}`) and is done — the agent's worker pool drains it when it has capacity. The scheduler never dispatches to or blocks on an agent.

**Why not Celery**: Celery is itself a competing-consumers system — adopting it under pull would mean running *two* such systems for no benefit. Pull already moved task *execution* onto the agent worker pools, so the scheduler's entire remaining job is "INSERT a queued row on a cron tick." A broker + worker processes + result backend + routing config is the wrong weight for that. APScheduler on a PostgreSQL job store gives durable, restart-surviving schedules; a Redis lock gives multi-instance redundancy — without Celery's operational surface. (Resolves #949.)

---

## Multi-Channel Integration

### What Stays the Same

- Channel Adapter ABC (`adapters/base.py`) — correct abstraction, keep
- Slack, Telegram, WhatsApp adapters — keep
- `NormalizedMessage` / `ChannelResponse` models — keep
- Per-channel DB tables — keep

### What Changes

**Message queue between adapters and agents**: currently adapters call agents synchronously through the message router. In the target state, adapters **enqueue a `queued` task row** and return an acknowledgment to the channel immediately; the agent's worker pool pulls it when ready. This decouples channel availability from agent availability — a slow agent doesn't stall Slack responses. **(v2)** The *reactive* reply path (adapter `send_response` posting an agent's turn output back to Slack/Telegram/WhatsApp) is a **reversible** effect — a duplicate post is survivable — so under retry-with-context it is **not** on the irreversible short list and **does not gate default-on**. It no longer requires effect-key wiring; trace-aware retry makes a duplicate rare and a duplicate is tolerable. (This is a deliberate scope reduction from v1, which treated the reactive path as an unwired blocking sink.)

**Channel-level circuit breakers**: if a channel API (Slack, Telegram, Twilio) is rate-limiting or unreachable, the adapter backs off with exponential retry rather than propagating errors to the routing layer. The agent never sees channel transport failures.

**Unified channel health**: a single `/api/channels/health` endpoint returns status for all connected channel workspaces — connection state, last message received, error rate. Visible in the UI alongside fleet health.

---

## Frontend

### What Stays the Same

- Vue.js 3 + Composition API — keep
- Pinia stores — keep
- Vue Flow for collaboration graph — keep
- Single Axios instance (`api.js`) — keep
- WebSocket client with reconnect replay — keep

### What Improves

**Fleet dashboard as primary view**: the default landing page is a fleet-level view — total capacity utilization, active tasks, fan-out graphs in progress, circuit breaker states, recent failures. The per-agent view exists but the operator's primary context is the fleet.

**Execution DAG visualization**: for multi-agent workflows (fan-out + join), the collaboration dashboard renders the execution DAG — which branches are running, which completed, timing, which failed. This is an extension of the current node graph, not a replacement.

**Streaming chat**: chat panels display tokens as they arrive via SSE, not after the full response. The current "typing…" indicator becomes actual streamed output.

**Session tab as default**: `--resume`-based sessions are the default experience. Cold turns (new conversations) are the exception, not the norm. The UI makes the session state visible and navigable.

---

## Observability

### Metrics Stack: OTEL → Prometheus → Grafana

> Prometheus + Grafana are an **opt-in observability profile**, not baseline services. The OTEL Collector (already present) and Vector cover the baseline; operators enable the metrics/dashboard stack when they want fleet-level views.

**Emit level**: every service (backend, scheduler, MCP server, agent-server) emits OTEL metrics and traces. The OTEL Collector forwards metrics to Prometheus and traces to a configured backend (Jaeger or Tempo).

**Fleet-level metrics** (not available today):
- `trinity_fleet_capacity_utilization` — % of total agent slots in use across the fleet
- `trinity_agent_task_duration_seconds` — P50/P95/P99 by agent name
- `trinity_agent_task_error_rate` — error ratio by agent and error type
- `trinity_fanout_join_latency_seconds` — time from first branch to last branch completion
- `trinity_queue_depth` — per-agent `COUNT(status='queued')`, the Lever-1 backlog signal (auto-scaling input)
- `trinity_queue_oldest_age_seconds` — age of the oldest queued task per agent (starvation / stuck-drain detector)
- `trinity_lease_reaper_redeliveries_total` — re-deliveries fired by lease expiry (a rising rate flags crashing/wedged agents)
- **(v2)** `trinity_effect_verify_total` / `trinity_effect_human_gated_total` — irreversible effects that hit the tool-side verify path vs. that fell to the operator-queue human gate (the residual-surface signal)

**Grafana dashboards**:
- Fleet Operations: capacity, error rates, fan-out health, circuit breakers
- Agent Deep-Dive: per-agent task history, cost, session continuity
- Channel Health: per-channel message volume, error rate, adapter latency

**Vector stays**: log aggregation via Vector remains — it handles the unstructured agent container stdout that OTEL doesn't capture. The two streams complement each other.

### Semantic Health Score

Fleet health is not "all agents responding to /health." It is a derived signal:

```
agent_health_score = f(
  recent_task_success_rate,    // last 1h
  p95_task_duration_vs_baseline,
  queue_depth_vs_capacity,     // Lever 1 vs Lever 2
  queue_oldest_age,            // is the drain actually keeping up?
  lease_redelivery_rate,       // crashing/wedged signal
  last_successful_output_at
)
```

The `GuardAgent` (below) contributes output quality to this score. An agent that executes successfully but produces garbage outputs is not healthy.

---

## Security and Trust

### Zero-Trust Agent Network

**Current state**: agent_permissions table enforces who can call whom at the MCP layer. This is correct as the default-deny model.

**Target addition — workflow-scoped capability tokens**: when Agent A is granted permission to call Agent B for a specific workflow, Agent B receives an ephemeral token scoped to that `correlation_id` with a TTL matching the workflow deadline. After the workflow completes, the token expires. This prevents credential reuse across workflow boundaries and bounds the blast radius of a compromised agent to its active workflows, not its permanent permission set.

### Capability Confinement of Irreversible Rails (v2)

The tool-side deterministic gate (§"Re-Delivery and Side-Effect Recovery") is only as strong as the confinement behind it. An irreversible rail (a payment wallet, a settlement key) must be reachable **only** through the gated Trinity-owned MCP tool, with the agent holding **no direct credential** for it — Trinity holds the rail's credentials, the agent holds a permission to invoke the tool. This is a security property, not just a reliability one: it is what makes "the agent cannot double-charge around the gate" true by construction rather than by trust. Rails that cannot be confined this way (an agent that legitimately needs its own third-party key) are **not** eligible for the confined-irreversible default-on tier and stay human-gated for those effects.

### GuardAgent

An optional platform-level output monitor that sits between agent responses and their destinations (users, downstream agents, channels).

**Capabilities:**
- PII detection (email, phone, SSN patterns) before delivery to external channels
- Output schema validation for agents that declare an output contract in `template.yaml`
- Rate limiting on external API calls initiated from agent outputs (prevents runaway spend)
- Content policy enforcement (configurable per agent, per channel)

**Integration**: GuardAgent is a platform service, not an agent. It intercepts completion events before the event bus delivers them to subscribers. Opt-in per agent initially; opt-out for system agents.

### Audit Log Completeness

Every action that touches an external system, modifies platform state, or transits between agents is auditable. The hash chain (Phase 4, already implemented) covers the complete event history. Retention and archival are automated.

---

## Infrastructure

### Primary Deployment: Docker Compose

Docker Compose remains the primary deployment model — it matches Trinity's ICP (self-hosted, commodity hardware, operator-controlled). The compose file grows to include:

| Service | Change from today |
|---------|-------------------|
| `backend` | unchanged |
| `frontend` | unchanged |
| `mcp-server` | unchanged |
| `scheduler` | unchanged shape — still a standalone APScheduler process; only its job store moves from SQLite to PostgreSQL. Optional second instance behind a Redis-lock leader election for redundancy. |
| `postgres` | **NEW** — replaces SQLite bind mount. The one non-negotiable new service. |
| `redis` | unchanged |
| `vector` | unchanged |
| `otel-collector` | already present |

**Deferred scaling levers (not baseline):** `pgbouncer` (transaction-pool in front of postgres — add when process count outgrows the connection ceiling) and a postgres **read replica** (add when read-bound). Both are introduced only when measured load justifies them, never speculatively.

**Opt-in observability profile (not baseline):** `prometheus` (scrapes the already-present OTEL Collector) + `grafana` (fleet dashboards). Enabled by operators who want fleet-level dashboards; OTEL Collector and Vector cover the baseline.

`~/trinity-data/` bind mount expands to cover the PostgreSQL data directory. SQLite is deprecated and removed once migration is complete.

### Redis ACL

The existing ACL model (`default` admin + restricted `backend`/`scheduler` users, already in place today) is unchanged — no Celery `worker` user is introduced. Each process keeps the minimum permissions it needs; no process has `@dangerous` access except `default`.

### Kubernetes Compatibility

Services are designed to run in Kubernetes without modification, though Docker Compose remains primary. This means:
- No `localhost` assumptions between services (all references use service DNS names)
- Health endpoints on all services at `/health`
- Configuration entirely via environment variables
- No local filesystem assumptions for state (everything in bind-mounted volumes or external storage)
- Stateless service processes (no in-process caches that can't be reconstructed)

A Helm chart is a natural byproduct of this discipline, not a separate effort.

### Network Topology (Unchanged by Design)

Two networks remain:

| Network | Members |
|---------|---------|
| `trinity-platform-network` | postgres, redis, scheduler, vector (+ pgbouncer, prometheus, grafana when those deferred/opt-in profiles are enabled) |
| `trinity-agent-network` | agents, frontend |

Bridges (both networks): backend, mcp-server, otel-collector, cloudflared

**Invariant preserved**: agents are never on the platform network. They cannot reach PostgreSQL or Redis directly. All data access flows through the backend API.

---

## What Does Not Change

These decisions are already correct and should not be revisited without strong evidence:

| Decision | Rationale |
|----------|-----------|
| Docker container per agent | Correct isolation model. Each agent is a sovereign runtime. |
| FastAPI for the backend | Excellent async performance, well-typed, good OpenAPI generation. |
| Vue.js 3 + Pinia | Correct frontend choice for the complexity level. |
| Redis Streams as event bus | Already the right primitive. `event_bus.py` is in good shape. |
| Channel Adapter ABC | Correct abstraction boundary for external messaging. |
| Three-layer backend architecture | Router → Service → DB invariant prevents coupling. |
| Credential file injection (CRED-002) | Simpler and more auditable than the Redis credential store it replaced. |
| MCP server as the external API surface | Agents and external tools communicate through MCP, not raw REST. |
| Docker as source of truth for container state | No in-memory registry. `docker_service.py` is the single Docker interaction point. |
| Single Axios instance (`api.js`) | One auth interceptor, one base URL, no duplicate clients. |

---

## Key Open Questions

These are architectural decisions not yet resolved. They should be answered before the relevant components are built. Each has a tracking issue.

1. **Message-envelope payload schema** (issue #945) — *Resolved (postcard written): see [`ACTOR_MODEL_POSTCARD.md`](ACTOR_MODEL_POSTCARD.md).* The envelope fields are defined; the `payload` schema for each `kind` is now pinned in the postcard. The pull model **retires the journal-as-source-of-truth question** — execution state is the backend row, not an agent-owned `journal.ndjson` — but the envelope is still the unit of enqueue/re-delivery/dedup and its payload contract is pinned before the pull pilot (#946). In particular the `reply` payload pins the **typed terminal-reason taxonomy** (`status` + `error_code`) that retires the substring failure classifier — see §Message Envelope and the postcard. See `ACTOR_MODEL_TASK_DEMOTION_MAP.md` for the *physical-enforcement* pre-work: `ParallelTaskRequest` has 14 fields today, and the envelope cannot replace that shape as a wire format until those are demoted to session/agent state or quarantined (the postcard is the documented contract; the pilot rides the existing reconstruction shape).

2. **PostgreSQL migration strategy** (issue #300): What is the zero-downtime migration path from SQLite to PostgreSQL for operators running live instances? Likely: parallel-write period, verification query, cutover. **Sequencing constraint added by the pull model:** the queue, the atomic claim, the result-write, and the lease-renewal all converge on one DB; at 200 agents that is exactly where SQLite's single-writer lock becomes the ceiling — *before* agent count does. PostgreSQL must therefore land **before** the pull queue carries the full fleet at scale (or no later than the "capacity becomes physical" phase), not after. #300 covers the SQLAlchemy Core abstraction step (closed); a detailed cutover plan + a dedicated migration ticket are still required.

2a. **Side-effect recovery** (issues #1401, #1402, #1084, **reframed in v2**): exactly-once external effects are impossible (two-generals), so the platform does not attempt them — it synthesizes recovery = **at-least-once delivery + retry-with-prior-trace + deterministic tool-side gates on capability-confined irreversible rails.** The remaining work, now with tracking:
   1. **Structured recovery trace + injection into re-delivered *and* next executions** — three-state (`done`/`not-done`/`unknown`), write-ahead for irreversible steps, structured (not stdout). The new center of gravity. **→ #1401** (filed, `status-incubating`, sub of #1081).
   2. **Async operator-queue human-gate lever** — the `MAX_REDELIVERY` cap + operator-queue park that catches poison tasks *and* irreversible-un-confineable effects; **no synchronous user gates** (agents fire-and-park). **→ #1402** (filed, `status-incubating`, sub of #1081; bounds the lease-reaper #429).
   3. **Deterministic tool-side gate on capability-confined irreversible rails** — native-idempotency-token where the provider offers one, check-then-act otherwise, gated on capability confinement (§Security "Capability Confinement"). **→ NOT yet filed (known v2 gap).**
   4. **`effect_guard` (#1084)** — merged to dev, 4 sinks; its *universal-coverage* ambition is retired. #1084 **awaits re-scope** to "the reversible/backend-sink slice + the confined-tool gate," so the tracker stops describing Direction A.
   5. **Trace fidelity is the load-bearing dependency** — capturing effects through the agent's *own* channels (own key / `gh` / `curl`) sits on the OPEN **#548 / #333** stdout-inheritance family; effects the trace cannot see fall to the human gate. **The hard problem *moved* here — it did not disappear.**
   **#1084 no longer blocks pull default-on.** Gating the *effect* not the *agent* lets read/analysis-only + reversible + confined-irreversible agents default on immediately; only irreversible-and-un-confineable effects wait. *(Design notes: `EFFECT_IDEMPOTENCY_DIRECTION_B_RETRY_CONTEXT.md`, `EFFECT_IDEMPOTENCY_1084_REVIEW_NOTE.md`.)*

2b. **Correlated-failure / thundering-herd behavior** (issue #1085) — *Resolved / shipped 2026-06-28.* Built against the **live #1083 fire-and-forget callback path** (the present-day re-delivery primitive — a backend restart re-sends ~N persisted terminal envelopes + in-flight retries), as reusable primitives pull consumes unchanged: (a) **decorrelated jitter** on the agent callback backoff + a jittered startup resend sweep (+ a `Retry-After` floor) so a fleet desynchronises; (b) **per-agent + fleet-wide re-delivery rate caps** at the callback endpoint (`services/rate_limiter` keys `redelivery:fleet`/`redelivery:agent:{name}`) → **503 + Retry-After**, retryable so never dropped; (c) a **shared-cause pause** (`services/redelivery_governor.py`) that counts *distinct* agents posting AUTH/BILLING terminals in a rolling window and, past a threshold, sets an auto-expiring Redis pause flag read by the callback endpoint, the lease reaper, and the capacity drain. All fail-open; backend controls default-OFF behind `REDELIVERY_GOVERNOR_ENABLED`. The soak gate is an in-suite restart-storm concurrency simulation plus a documented manual induced-restart procedure. See [`feature-flows/redelivery-governor.md`](../memory/feature-flows/redelivery-governor.md). **Open for pull:** when pull's re-delivery replaces the callback path, point the same governor at its `/api/internal/tasks/{id}/result` sink (the API is already transport-agnostic).

3. **GuardAgent evaluation** (issue #947): How does the GuardAgent evaluate output quality? Rule-based (regexes, schema validation) is implementable today. LLM-based evaluation (semantic quality scoring) is more powerful but adds latency and cost. The boundary between them needs a design decision.

4. **Scheduler implementation** (issue #949) — *Resolved 2026-06-05*: **APScheduler on a PostgreSQL job store**, not Celery. Pull moved task execution onto the agent worker pools, leaving the scheduler with only "enqueue a row on a cron tick"; a second competing-consumers system (Celery) would add a broker, worker processes, and routing config for no benefit. Redundancy, when needed, is a Redis-lock leader election over a second APScheduler instance — not a distributed task framework. See §Scheduling. (Remaining sub-question, if any: the exact PostgreSQL job-store driver and the leader-election lock semantics — minor, settled at implementation time.)

5. **Replica-group coordination** (issue #927): pull **simplifies** this — the atomic row-claim is the competing-consumer primitive, so there is no Redis consumer-group machinery and no journal-projection serialization path to design (the journal is no longer authoritative). What remains: the single-writer election for git pushes (Redis lock `agent:{name}:git_writer`) and the `template.yaml` schema for declaring replica-safety, both needed before `replica_count > 1` is exposed. Container autoscaling (vs. operator-set replica counts) is explicitly out of scope until real load patterns justify it.

6. **Workflow-scoped capability tokens** (issue #948): the §Security and Trust addition — ephemeral tokens scoped to a `correlation_id` so a compromised agent's blast radius is bounded to its active workflows rather than its permanent permission set. Layered on top of `agent_permissions`, not a replacement. Sequencing depends on the #946 decision gate.

## Tracking Issues

Critical-path work toward this architecture is tracked in GitHub:

All pull-coordination work lives under **Epic #1045 (Agent Infrastructure)**.

| Surface | Issue(s) |
|---------|----------|
| **Pull / work-stealing migration — umbrella** (schema → dark pull endpoints → agent worker-pool → capacity-physical + lease-reaper → sync edge + fan-out join → default-on + delete) | **#1081** |
| ├─ Bankable win 1 — status-as-projection (CAS-guarded; retires canary S-01; ships independently) — **shipped** | #1082 |
| ├─ Bankable win 2 — fire-and-forget dispatch (a hung turn holds zero backend resource) — **shipped** | #1083 |
| ├─ **v2 — Structured recovery trace + injection** (retry **and** next execution; the Direction-B center of gravity) | **#1401** |
| ├─ **v2 — MAX_REDELIVERY cap + async operator-queue human-gate lever** (bounds the lease-reaper; no synchronous user gates) | **#1402** |
| ├─ Effect-scoped idempotency `effect_guard` (Direction A — reversible/backend-sink slice; universal coverage retired, **re-scope pending**) | #1084 |
| ├─ Deterministic tool-side gate on capability-confined irreversible rails — **not yet filed (v2 gap)** | — |
| ├─ Correlated-failure / herd controls (jitter + re-delivery caps + shared-cause pause) — **shipped**; default-OFF flag | #1085 |
| ├─ Pilot: route MCP `chat_with_agent` through the agent queue | #946 |
| ├─ Cleanup pyramid → single lease-reaper (bounded by #1402 `MAX_REDELIVERY`) | #429 |
| └─ PostgreSQL migration (SQLAlchemy Core abstraction shipped; cutover ticket still to open) | #300 |
| Message-envelope payload schema (gates the pilot #946) — postcard written (`ACTOR_MODEL_POSTCARD.md`) | #945 |
| Idempotency keys at trigger boundaries (shipped) | #525 |
| Per-agent dispatch circuit breaker — repurposed gate→alert under pull (shipped) | #526 |
| Agent heartbeat push — repurposed gate→alert under pull (shipped) | #307 |
| Replica groups — now row-claim competing-consumers, no Redis consumer groups | #927 |
| Scheduler: APScheduler+PG job store — Celery rejected (pull made it redundant) | #949 |
| GuardAgent design + rule-based prototype | #947 |
| Workflow-scoped capability tokens | #948 |
| Trace-fidelity dependency (blocks #1401) — stdout fd-inheritance reader-race family | #548, #333 |

See `docs/archive/plans/ORCHESTRATION_RELIABILITY_2026-04.md` for the sprint sequencing and gating constraints between these, and `docs/archive/plans/TARGET_ARCHITECTURE_v1_2026-06-06.md` for the superseded v1 side-effect approach (Direction A).

---

## Incubating Directions (Not Yet Decided)

Forward-looking directions captured for evaluation — **not** committed architecture and **not**
on the current critical path. They incubate in the private tracker until a build/defer/reject
decision is made.

- **Goal-directed control surface** — make Trinity an optional *environment* for goal-seeking
  agents: a first-class **Objective** + **policies**, a **roster** of recruitable agents, and
  **externally-measured evaluations** (Trinity scores the work; the agent does not grade itself),
  closed by an agent-owned optimization loop. Framed as the capstone over the Semantic Health
  Score, GuardAgent (#947), collaborator injection (#171), A2A Agent Cards (#737), and the
  `systems` grouping. Strictly bounded by CLAUDE.md §8 ("Trinity ≠ DAG engine"): Trinity provides
  the objective, the eval/scoreboard, the roster, and the levers; the *optimization loop and peer
  dispatch live in a manager agent*, never in backend transition logic. Naturally sequenced
  **after** the pull migration + PostgreSQL (#300). Incubating in `abilityai/trinity-enterprise#27`.

---

## Appendix: v1 → v2 Change Log (Side-Effect Handling)

For readers coming from v1 (archived at `docs/archive/plans/TARGET_ARCHITECTURE_v1_2026-06-06.md`), this is exactly what moved and why.

| Aspect | v1 (Direction A) | v2 (Direction B) |
|---|---|---|
| **Framing of #1084** | "The single hardest unsolved problem — the gate that blocks pull." | A **recovery discipline**, not a gate. Exactly-once is impossible; the platform synthesizes effectively-once from at-least-once + retry-with-trace + tool-side gates. |
| **Primary mechanism** | Platform-injected effect key threaded through **every** outbound sink, deduplicated at the sink. | **Retry with prior-trace context** (#1401) — the re-delivered (and next) turn sees what the dead turn did and recovers itself (continue / verify-before-redo / fail-gracefully). |
| **Irreversible effects** | Same effect key, fail-open, `execution_id` as an untrusted tool arg. | **Deterministic gate inside the Trinity-owned MCP tool that solely fronts the rail** — native-idempotency-token where available, gated on **capability confinement** (not yet filed). The agent never decides "verify vs redo" for money. |
| **`effect_guard` (4 sinks, shipped)** | The seed of universal coverage; expand to all sinks. | **Kept, narrowed** to confined irreversible tools; **universal-coverage ambition retired**. #1084 code stays; re-scope pending. |
| **Reactive channel replies + git-sink** | Unwired sinks that **block** default-on. | **Downgraded** — reversible / naturally-idempotent; not gate-blockers. |
| **Human gate** | Implied blocking behavior. | **Asynchronous operator queue (#1402)** — no synchronous user gates; agents fire-and-park, never block a worker on a person. |
| **Unit of gating** | The **agent** (coarse: whole agent can/can't auto-recover). | The **effect** (only irreversible-and-un-confineable actions gate; the rest of the agent is auto-re-deliverable). |
| **The hard problem** | Wire enough sinks + make the key trusted/fail-closed. | **Trace fidelity** (#548/#333) — can the trace see effects through the agent's own channels? The problem *moved*, it did not disappear. |
| **Rollout impact** | Side-effect agents wait for full sink coverage before pull default-on. | Read-only + reversible + confined-irreversible agents default on **now**; only irreversible-un-confineable effects wait. Pilot unblocked. |

Tracking: **#1401** (recovery trace + injection) and **#1402** (async human-gate lever) filed under #1081; **#1084** (Direction A) awaits re-scope; the **tool-side confined-rail gate is not yet filed**; trace fidelity depends on the open **#548/#333**. The two-generals residual (an irreversible effect that lands but is never recorded on an un-confineable channel) is unchanged between v1 and v2 — **neither closes it**; both relocate it to a human gate. v2 is more honest that the residual is permanent and names precisely where it lives.

---

## Review Schedule

| Trigger | Action |
|---------|--------|
| Quarterly | Review all sections for drift from current implementation |
| Before any major new capability | Check that the proposed design aligns with this document |
| After each completed sprint | Update "What Does Not Change" if a decision was revisited |
| When a scaling milestone is hit (50 agents, 100 agents) | Validate that the architecture holds at the new scale |
