# Trinity — Target Architecture

**Created:** 2026-05-05
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
3. **Async-first communication.** No component blocks waiting for another to respond. Sync semantics are thin edge adapters over async internals — not a core design choice.
4. **Proven primitives.** Use PostgreSQL for relational state, Redis for ephemeral/event state, Docker for isolation. Resist building custom solutions for problems these tools already solve.
5. **Actor model as the coordination shape.** Each agent is an independent actor with a mailbox, a processor, and a journal. The platform delivers messages and projects journals — it does not own workflow state.
6. **Sovereign infrastructure.** Trinity runs on hardware the operator controls. Design decisions must work on a single commodity server, not require cloud dependencies or managed services.
7. **Data exchange over conversation chains.** Agents composing via structured files, queues, and typed outputs is more reliable and testable than chaining conversations. Async data handoffs — shared folders, repo commits, scheduled queue tasks — are the default composition pattern. Direct agent-to-agent conversation is an edge adapter for cases where no data-exchange pattern fits.

---

## System Topology

```
┌─────────────────────────────────────────────────────────────────────────────────────┐
│                              Trinity Target Architecture                              │
├─────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                       │
│   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐   ┌───────────────────┐   │
│   │   Frontend   │   │   Backend    │   │  MCP Server  │   │    Scheduler      │   │
│   │   (Vue.js)   │   │  (FastAPI)   │   │  (FastMCP)   │   │   (Celery Beat)   │   │
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
│       │ PgBouncer  │  └────────────┘          │                                     │
│       │  :6432     │                          │                                     │
│       └────────────┘     ┌───────────────────┬┴──────────────────────┐              │
│                          │                   │                       │              │
│                    ┌─────┴───┐          ┌────┴────┐            ┌─────┴────┐         │
│                    │ Agent 1 │          │ Agent 2 │            │ Agent N  │         │
│                    │ :8000   │          │ :8000   │            │ :8000    │         │
│                    └─────────┘          └─────────┘            └──────────┘         │
│                                                                                       │
│   Platform Network (172.29.0.0/16): postgres, pgbouncer, redis, scheduler, vector    │
│   Agent Network (172.28.0.0/16):    agents, backend, mcp-server                     │
│                                                                                       │
│   Observability Plane (all services emit):                                            │
│   OTEL Collector → Prometheus → Grafana                                               │
│   Vector → structured log files                                                       │
└─────────────────────────────────────────────────────────────────────────────────────┘
```

---

## Data Layer

### PostgreSQL (Primary Relational Store)

Replaces SQLite as the authoritative store for all durable relational state.

**What lives here:**
- All current SQLite tables (users, agents, schedules, executions, chat history, audit log, activities, subscriptions, skills, tags, operator queue, sync state)
- Partitioned tables for high-volume, time-series data: `audit_log`, `agent_activities`, `chat_messages`, `agent_session_messages` — partitioned by month, retained per configured window
- Schema migrations via Alembic (versioned, reversible, automated on startup)

**Access pattern:**
- All backend processes connect through **PgBouncer** in transaction pooling mode — this is the single connection management point
- Read-heavy endpoints (activity timeline, execution history, audit log queries) use a read replica
- No direct PostgreSQL connections from agent containers — agents communicate only through the backend API

**Why PostgreSQL over SQLite:**
SQLite's single-writer constraint is acceptable for embedded use; it is not acceptable when the backend, scheduler, and future services all write execution state concurrently under load. PostgreSQL provides row-level locking, concurrent writes, replication, and point-in-time recovery — without introducing a second store or split-brain risk. It is the proven, boring answer for this class of problem.

### Redis (Ephemeral and Event State)

Redis owns state that is either transient or derivable from the primary store.

**What lives here:**
- **Event bus**: Redis Streams (`trinity:events`) — all real-time delivery, WebSocket fan-out
- **Execution slots**: per-agent capacity counters (ZSET) — authoritative only until execution completes; final state written to PostgreSQL
- **Agent mailboxes**: per-agent task queue (STREAM) — the actor model inbox; drained by the processor, checkpointed to PostgreSQL on completion
- **Session tickets**: WebSocket auth (30s TTL, single-use GETDEL)
- **Rate limiting**: sliding window counters per endpoint
- **Distributed locks**: Redis SET NX EX for critical sections (session resume serialization, credential writes)

**What does not live here:**
- Chat history, execution records, user data, audit log — these are PostgreSQL
- Redis is never the source of truth for anything a user would expect to retrieve after a restart

### Clean Separation Rule

| Concern | Store |
|---------|-------|
| Durable entity state | PostgreSQL |
| Time-series / append-only events | PostgreSQL (partitioned) |
| Real-time delivery | Redis Streams |
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

## Coordination Model (Actor Model)

This is the central architectural shift. The full rationale is in `ORCHESTRATION_RELIABILITY_2026-04.md` — this document states the destination shape.

### The Model

Every agent is an **actor** with three components:

**1. Mailbox** — a durable per-agent inbox (Redis STREAM, checkpointed to PostgreSQL). All messages destined for the agent arrive here: scheduled tasks, agent-to-agent calls, webhook triggers, human chat turns. There is no separate dispatch path — the mailbox is the only path in.

**2. Processor** — pulls from the mailbox, executes one message at a time per slot (configurable parallelism via `max_parallel_tasks`), emits reply messages or completion events, appends to the journal. The processor is the agent-server's execution loop.

**3. Journal** — append-only record of everything the agent processed, stored in the agent's git repository (`journal.ndjson`). Replayable. Source of truth for "what happened to this agent." The platform projects the journal into PostgreSQL for observability and querying — it does not own the authoritative workflow state.

### Message Envelope

Every inter-agent message carries:
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

The envelope is the unit of delivery, retry, and deduplication.

### Async-First Communication

`chat_with_agent` (MCP tool) never blocks. It:
1. Drops a message in the target agent's mailbox
2. Returns immediately: `{execution_id, status: "queued"}`
3. Caller subscribes to `agent.task.completed` / `agent.task.failed` events via the event bus
4. Or polls `get_execution_result` — the existing polling path remains valid

Human-facing chat is the edge adapter: the WebSocket holds open, drops a message in the mailbox, and forwards the reply when the completion event arrives. The user experience is synchronous; the internals are not.

### Fan-Out Without Deadlock

`fan_out` creates N messages in N mailboxes and returns a `fan_out_id` immediately. No orchestrator slot is held. The orchestrator subscribes to a synthetic `fan_out.all_complete` event that the platform emits when all `N` branches emit their completion events. The join point is event-driven, not blocking.

This eliminates the fork-join topology instability identified in the scalability analysis — mathematically, there is no deadlock possible when the coordinator holds no resources while waiting.

### Circuit Breaker Per Agent Pair

If calls from Agent A to Agent B accumulate consecutive failures above a threshold, the circuit opens. Agent A receives immediate failure responses without enqueuing — preventing cascade backlog buildup. Auto-resets after Agent B's health check passes. Configured per agent pair, defaulting to 3 consecutive failures.

### Saga Pattern for Multi-Step Workflows

Long-running workflows spanning multiple agents define a compensation action for each step. If step N fails, steps N-1 through 1 execute their compensations in reverse order. This prevents partial state corruption from stranded mid-workflow executions. Implemented at the orchestration layer — individual agents remain unaware.

### Replica Groups for Horizontal Scale

When a single container's `max_parallel_tasks` ceiling — bounded by container CPU, memory, and Claude Code concurrency — is below the throughput a capability needs, the agent is deployed as a **replica group**: N container instances backed by one logical agent identity.

**Topology:**
- `agent_ownership.replica_count` (default 1) controls the desired instance count; `replica_count = 1` preserves today's behavior exactly
- One mailbox (Redis STREAM) per agent name — unchanged
- Replicas join a Redis Streams consumer group `agent:{name}:processors` — provides at-most-once delivery across replicas, pending-entry tracking, and claim-on-consumer-death without inventing new platform machinery
- Callers address the agent by name; the consumer group distributes messages. No caller-side dispatch logic. Schedules fire once into the mailbox per tick and exactly one replica drains each trigger.

**Shared-state discipline (required before `replica_count > 1`):**
- **Git repo writes**: single-writer election via Redis lock `agent:{name}:git_writer`; only the leader pushes, followers stay read-only
- **Journal**: serialized through the platform projection path, not written from inside the container — N container writers never race on `journal.ndjson`
- **Credentials and template files**: read-only after injection — safe to share by image across replicas
- **Replica-safety**: declared in `template.yaml`. Agents that mutate `~/.trinity/` mid-turn, hold long-running in-memory state, or persist pipeline state in the container filesystem cannot opt into `replica_count > 1` without explicit design work.

**Distinct from agent cloning:** cloning produces two siblings with divergent state and forces every caller to choose between them. Replica groups produce one logical agent with N processors and zero caller-side routing — one `agent_ownership` row, one credential set, one schedule list, one row in the fleet view.

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

**Streaming responses**: agent-server supports SSE (Server-Sent Events) on `/api/chat` for real-time token streaming to the frontend. Eliminates the current pattern of waiting for full response completion before delivery.

**Richer health signal**: `/health` returns not just `{status: ok}` but `{status, active_tasks, mailbox_depth, last_task_at, consecutive_failures}`. The platform uses this for circuit breaker decisions and fleet health scoring.

**Journal writes**: on every task completion, the agent-server appends a structured entry to `~/.trinity/journal.ndjson` in the agent's workspace. The backend projects this into PostgreSQL. The journal is the agent's authoritative history; the database is a queryable projection.

**Post-execution hooks**: companion to the existing pre-check hook. `~/.trinity/post-check` runs after every task completion (language-agnostic, shebang-selected). Enables custom alerting, output validation, or state transitions defined by the agent template.

---

## Scheduling

**Celery Beat** replaces APScheduler as the distributed task scheduler. Celery workers consume from a Redis-backed task queue. This eliminates the single-process APScheduler limitation — multiple scheduler instances can run for redundancy, with Redis providing distributed leader election.

The scheduling data model (agent_schedules, schedule_executions) is unchanged. The interface — cron expressions, manual triggers, webhook triggers — is unchanged. The implementation switches from in-process APScheduler to an independent Celery process that publishes messages to agent mailboxes.

**Why Celery over APScheduler at scale**: APScheduler is excellent for single-process scheduling; it cannot distribute work across processes or provide fault-tolerant redundancy. Celery with Redis backend is the standard distributed task queue for Python systems at this scale.

---

## Multi-Channel Integration

### What Stays the Same

- Channel Adapter ABC (`adapters/base.py`) — correct abstraction, keep
- Slack, Telegram, WhatsApp adapters — keep
- `NormalizedMessage` / `ChannelResponse` models — keep
- Per-channel DB tables — keep

### What Changes

**Message queue between adapters and agents**: currently adapters call agents synchronously through the message router. In the target state, adapters enqueue to the agent mailbox and return an acknowledgment to the channel immediately. This decouples channel availability from agent availability — a slow agent doesn't stall Slack responses.

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

**Emit level**: every service (backend, scheduler, MCP server, agent-server) emits OTEL metrics and traces. The OTEL Collector forwards metrics to Prometheus and traces to a configured backend (Jaeger or Tempo).

**Fleet-level metrics** (not available today):
- `trinity_fleet_capacity_utilization` — % of total agent slots in use across the fleet
- `trinity_agent_task_duration_seconds` — P50/P95/P99 by agent name
- `trinity_agent_task_error_rate` — error ratio by agent and error type
- `trinity_fanout_join_latency_seconds` — time from first branch to last branch completion
- `trinity_mailbox_depth` — per-agent queued message count (auto-scaling signal)
- `trinity_circuit_breaker_state` — open/closed/half-open per agent pair

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
  mailbox_depth_vs_capacity,
  circuit_breaker_state,
  last_successful_output_at
)
```

The `GuardAgent` (below) contributes output quality to this score. An agent that executes successfully but produces garbage outputs is not healthy.

---

## Security and Trust

### Zero-Trust Agent Network

**Current state**: agent_permissions table enforces who can call whom at the MCP layer. This is correct as the default-deny model.

**Target addition — workflow-scoped capability tokens**: when Agent A is granted permission to call Agent B for a specific workflow, Agent B receives an ephemeral token scoped to that `correlation_id` with a TTL matching the workflow deadline. After the workflow completes, the token expires. This prevents credential reuse across workflow boundaries and bounds the blast radius of a compromised agent to its active workflows, not its permanent permission set.

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
| `scheduler` | upgraded: Celery worker + Celery Beat (replaces APScheduler inside backend) |
| `postgres` | **NEW** — replaces SQLite bind mount |
| `pgbouncer` | **NEW** — connection pooler in front of postgres |
| `redis` | unchanged |
| `vector` | unchanged |
| `otel-collector` | already present |
| `prometheus` | **NEW** — scrapes OTEL Collector metrics |
| `grafana` | **NEW** — dashboards for fleet operators |

`~/trinity-data/` bind mount expands to cover the PostgreSQL data directory. SQLite is deprecated and removed once migration is complete.

### Redis ACL

The existing two-user ACL model (`default` admin + `backend` restricted) extends to include a `scheduler` user and a `worker` user. Each process has the minimum permissions it needs. No process has `@dangerous` access except `default`.

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
| `trinity-platform-network` | postgres, pgbouncer, redis, scheduler, vector, prometheus, grafana |
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

1. **Journal format** (issue #945): What does `journal.ndjson` contain per entry? The envelope fields are defined; the payload schema for each `kind` is not. The one-page postcard (envelope + journal format) is required before the Phase 2 actor-model experiment (#946) can be scheduled. See `ACTOR_MODEL_TASK_DEMOTION_MAP.md` for the pre-postcard work — `ParallelTaskRequest` has 15 fields today, and the postcard cannot fit honestly until those are demoted to session/agent state or quarantined.

2. **PostgreSQL migration strategy** (issue #300): What is the zero-downtime migration path from SQLite to PostgreSQL for operators running live instances? Likely: parallel-write period, verification query, cutover. #300 covers the SQLAlchemy Core abstraction step; a detailed cutover plan is still required before the migration ticket is opened.

3. **GuardAgent evaluation** (issue #947): How does the GuardAgent evaluate output quality? Rule-based (regexes, schema validation) is implementable today. LLM-based evaluation (semantic quality scoring) is more powerful but adds latency and cost. The boundary between them needs a design decision.

4. **Celery vs. APScheduler+PG** (issue #949): Celery adds operational surface (worker processes, task routing, retry configuration). Is the distributed redundancy benefit worth it for operators running single-node deployments? The alternative is APScheduler backed by a PostgreSQL job store, which gives persistence without the full Celery stack. Decision should be made before the scheduler migration is planned.

5. **Replica-group coordination** (issue #927): the single-writer election for git pushes, the journal-projection serialization path, and the `template.yaml` schema for declaring replica-safety all need design before `replica_count > 1` is exposed. Container autoscaling (vs. operator-set replica counts) is explicitly out of scope until real load patterns justify it.

6. **Workflow-scoped capability tokens** (issue #948): the §Security and Trust addition — ephemeral tokens scoped to a `correlation_id` so a compromised agent's blast radius is bounded to its active workflows rather than its permanent permission set. Layered on top of `agent_permissions`, not a replacement. Sequencing depends on the #946 decision gate.

## Tracking Issues

Critical-path work toward this architecture is tracked in GitHub:

| Surface | Issues |
|---------|--------|
| Cleanup pyramid collapse | #429 |
| Idempotency keys at trigger boundaries | #525 |
| Per-agent dispatch circuit breaker | #526 |
| Agent heartbeat push (5s) | #307 |
| Actor-model postcard (envelope + journal) | #945 |
| Phase 2 actor-model experiment (MCP boundary) | #946 |
| GuardAgent design + rule-based prototype | #947 |
| Workflow-scoped capability tokens | #948 |
| Celery vs APScheduler+PG decision | #949 |
| Replica groups | #927 |
| PostgreSQL migration | #300 |

See `ORCHESTRATION_RELIABILITY_2026-04.md` for the sprint sequencing and gating constraints between these.

---

## Review Schedule

| Trigger | Action |
|---------|--------|
| Quarterly | Review all sections for drift from current implementation |
| Before any major new capability | Check that the proposed design aligns with this document |
| After each completed sprint | Update "What Does Not Change" if a decision was revisited |
| When a scaling milestone is hit (50 agents, 100 agents) | Validate that the architecture holds at the new scale |
