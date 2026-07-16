# A2A Inbound Server (ent#157) + A2A Control over MCP (ent#160)

The serving half of A2A interop. The #737 Agent Card *describes* an agent; this
lets an external orchestrator (AWS Bedrock, Azure Copilot, Google ADK, any A2A
SDK) **task** it — discover a Trinity agent from a spec-shaped well-known URL,
then drive it over JSON-RPC 2.0 with no Trinity-specific client code.

Exposure is **opt-in per agent and default OFF** (`agent_ownership.a2a_exposed`).
Until an owner turns it on, nothing is publicly reachable and every public route
answers a uniform 404 — indistinguishable from an agent that doesn't exist.

Protocol target is **`0.3.0`** (the v0.3.x method set: `message/send`,
`message/stream`, `tasks/get`, `tasks/cancel`, lowerCamel casing). The card's
earlier `"1.0"` was a placeholder with no endpoint behind it; once the card
points at a real JSON-RPC server, the advertised version has to match what
answers.

## Layers

| Layer | File | Notes |
|-------|------|-------|
| Router (card) | `src/backend/routers/a2a.py` → `router` | `GET /api/agents/{name}/a2a/agent-card` — authenticated (`AuthorizedAgentByName`), any accessible agent (#737) |
| Router (inbound) | `src/backend/routers/a2a.py` → `a2a_server_router` | Prefix-less so external clients get spec-shaped paths: `GET /a2a/{name}/.well-known/agent-card.json` + `POST /a2a/{name}` (ent#157) |
| Card generator | `src/backend/services/a2a_card_service.py` | `generate_a2a_card` — pure; `template.yaml` → card, `capabilities[]`→`skills[]`, `use_cases[]` as examples |
| Allow-list seam | `src/backend/services/a2a_gate.py` | Open-core provider protocol; OSS registers none → allow. **Fails open** by design |
| DB | `src/backend/db/agent_settings/` + `db/schema.py` / `db/tables.py` | `a2a_exposed` getter/setter on the `AgentOperations` mixin (Invariant #2) |
| MCP tools | `src/mcp-server/src/tools/a2a.ts` (+ `a2a.test.ts`) | ent#160 control surface (Invariant #13) |
| UI | `components/A2aPanel.vue` | Sharing tab: exposure toggle, card URL, advertised skills, inbound allow-list, outbound endpoints |
| Front door | `src/frontend/nginx.conf` | `location /a2a/` → backend, `proxy_buffering off` for SSE, `X-Real-IP` set so the rate limiter sees the true client |

## Endpoints

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| GET | `/api/agents/{name}/a2a/agent-card` | `AuthorizedAgentByName` | The #737 card. Unaffected by `a2a_exposed` |
| GET | `/a2a/{name}/.well-known/agent-card.json` | **None** | Public discovery for an exposed agent. Per-IP rate limited. Uniform 404 otherwise |
| POST | `/a2a/{name}` | Bearer MCP API key | JSON-RPC 2.0 task endpoint. Fail-closed 401 before any envelope parse |

JSON-RPC methods: `message/send`, `message/stream` (SSE), `tasks/get`,
`tasks/cancel`. `tasks/resubscribe` returns an explicit unsupported error
(`-32004`) rather than a method-not-found, so a client can tell "phased" from
"typo".

Protocol errors ride the JSON-RPC envelope at **HTTP 200** (that's the spec's
shape). Auth is the deliberate exception — a 401 happens at the dependency,
before an envelope exists to carry an error.

## The three gates on an inbound task

`_authorize_inbound` runs all three before any work, in this order:

1. **Exposed?** `db.get_a2a_exposed(name)` — else 404.
2. **Accessible?** `db.can_user_access_agent(...)` — else 404, *byte-identical*
   to the previous branch. Exposure is not access: an exposed agent the caller
   can't reach must be indistinguishable from one that doesn't exist, or the
   difference is an enumeration oracle (Invariant #8).
3. **On the allow-list?** `a2a_gate.check_inbound_allowed(...)` — else 403.
   This one is a *restriction on an already-authenticated caller*, not a
   security boundary: it runs last, and it fails open.

## `messageId` dedup — why the scope carries the caller

Invariant #18 wants a re-delivered task to not double-execute, and A2A's
`messageId` is the obvious key. It is also **peer-controlled**: SDKs generate it
automatically and the spec only requires uniqueness *per client*. `"req-1"` is
the spec's own worked example.

So the scope is `a2a:{agent}:{principal}` (`_a2a_idem_scope`), never
`a2a:{agent}` alone. With an agent-only scope, two callers sharing an agent
collide on `"req-1"` and the second caller receives the **first caller's stored
snapshot** — which carries the agent's full response text — while their own task
silently never runs.

The principal prefers `mcp_key_id` over `username` because agent-scoped keys all
resolve to the same owner user; the key id is what distinguishes two agents
calling on one owner's behalf. Same caller + same `messageId` still dedups
normally — a collision can only ever replay the caller's *own* prior result.

This differs from the `make_agent_scope` house shape (`chat.py`, `fan_out.py`)
on purpose: there, `Idempotency-Key` is opt-in and deliberately chosen by the
caller; here the key is automatic and collision-prone.

## Rate limiting the public route

The well-known route is unauthenticated and its URL is **published by design**,
so the agent name is public. Each hit costs a DB read → a live uncached Docker
API call → an HTTP call into the agent container (5s timeout) — all inside an
`async def`, so an unthrottled flood stalls the event loop and hammers the fleet
from one URL.

`A2A_CARD_RATE_LIMIT = 60` per `A2A_CARD_RATE_WINDOW = 60`s per IP, via the
shared `services/rate_limiter` (Redis sliding window, fail-open), enforced
**before** any of that work — limiting after the expensive part would not fix
the flood. Client IP resolves through `routers.public._get_client_ip`, which
only trusts `X-Real-IP`/`X-Forwarded-For` from a trusted proxy, so headers can't
spoof past it. Mirrors `public.py` (`PUBLIC_LINK_LOOKUP_RATE_LIMIT`), `files.py`
(`_DOWNLOAD_RATE_LIMIT`), and `webhooks.py` (#1424).

The JSON-RPC body is capped at `_MAX_RPC_BODY_BYTES` (1 MB) **before** parsing —
nginx caps at 25m, but `:8000` may be reachable directly.

## Cancellation is honest

`tasks/cancel` reports what actually happened, because the alternative is a
caller who believes a task is dead while it drains, runs, bills, and performs
side effects:

| Row state | Action | Result |
|-----------|--------|--------|
| terminal (`success`/`failed`/`cancelled`) | none | `-32002` TaskNotCancelable |
| `queued` | `db.cancel_queued_execution` (backlog CAS) | `canceled`, agent registry never consulted |
| running | `terminate_execution_on_agent` | `canceled` on True; `-32002` on False |

A queued row has no container process to signal — a registry call 404s, and the
old code discarded that `bool` and reported success anyway. The CAS is what
makes a lost race honest (cf. #1082 status-as-projection).

`_a2a_state_for` maps `cancelled` → `canceled`, not `failed` — A2A treats them
as distinct terminal states.

## SSE (`message/stream`)

Emits a `working` status event, runs the turn, then a terminal task event.
Non-incremental (the agent turn is atomic) but spec-shaped, so a streaming
client attaches and receives the result.

A client disconnect raises `asyncio.CancelledError` — a `BaseException` since
3.8, so a plain `except Exception` never sees it. The generator catches it
explicitly, calls `idempotency_service.fail(decision)`, and re-raises; without
that the row stays `in_flight` and every retry with that `messageId` gets
"already in progress" for the full 24h TTL.

Replay of a completed `messageId` returns **SSE** for `message/stream`
(`_replay_stream`) and JSON for `message/send` — a streaming SDK has an
event-stream parser attached and breaks on a bare JSON body.

## Execution bridge

`_run_a2a_task` → `task_execution_service.execute_task(triggered_by="a2a")` —
the standard stack, so A2A tasks get slots, capacity admission, the dispatch
breaker, activity rows, and cost tracking like any other trigger. `triggered_by="a2a"`
buckets into the analytics `_TRIGGER_BUCKETS` catch-all until it earns its own
bucket.

## Schema / migration

`agent_ownership.a2a_exposed INTEGER DEFAULT 0` — dual-track (Invariant #3):

| Track | Artifact |
|-------|----------|
| SQLite | `db/migrations.py` → `_migrate_agent_ownership_a2a_exposed` |
| PostgreSQL | `migrations/versions/0024_agent_ownership_a2a_exposed.py` (`down_revision = "0023_agent_sync_state_gc_signals"`) |
| Fresh DDL | `db/schema.py` + `db/tables.py` |

## Open-core split

The public repo owns the whole serving mechanism: both routes, the exposure
column, the execution bridge, and the `a2a_gate` seam. A private module can
register an allow-list provider and the entitled exposure setter; OSS registers
neither, so in an OSS-only build every agent is non-exposed and the public routes
are inert — safe by default.

`services/a2a_gate.py` is a **seam file**: its comments describe the mechanism
only and must never name a private table or the paid catalog (the #1461 class).
It is listed in `SEAM_FILES` in `.github/workflows/enterprise-docs-guard.yml`,
which greps it on every build.

Merge ordering: this OSS surface lands **before** the companion enterprise module
that imports `a2a_gate` and `get_a2a_exposed` / `set_a2a_exposed` — none of which
exist in OSS until it does.

## Testing

`tests/unit/test_157_a2a_inbound_server.py` (44 tests). The harness patches the
names the router hoisted into its **own** module globals — the unit harness loads
a duplicate `services.*` module, so patching `services.task_execution_service`
directly does not reach the router.

Covered: uniform 404 (non-exposed, nonexistent, exposed-but-inaccessible, and
byte-identical bodies across branches); JSON-RPC envelope errors; the fail-closed
401; per-caller dedup scoping incl. an end-to-end proof that caller B's task
executes rather than replaying A's snapshot; the rate limit and its ordering
ahead of Docker/DB work; honest cancel across queued/terminal/failed-terminate;
state mapping; SSE working→final; allow-list allow/deny/fail-open.

Verified live against a running stack (backend bind-mounts `src/backend`, so the
checked-out branch is what serves): flooding the public card gave 57×200 then
18×429 with `Retry-After: 50`; unauthenticated JSON-RPC gave 401; a 1.1 MB body
gave `-32600 "Request body too large"`; a non-exposed agent and a nonexistent one
both gave 404; an exposed agent served a `0.3.0` card.

## Related Flows

- [mcp-agent-exposure.md](mcp-agent-exposure.md) — the sibling per-agent exposure
  flag (#846); same "publish a surface, never bypass the access gate" shape
- [mcp-connector.md](mcp-connector.md) — the other "external client reaches one
  agent" surface, via a scoped key rather than a public route
- [task-execution-service.md](task-execution-service.md) — what `execute_task`
  does with an A2A-triggered turn
- [idempotency-keys.md](idempotency-keys.md) — the trigger-boundary dedup layer
  `messageId` plugs into (Invariant #18)
