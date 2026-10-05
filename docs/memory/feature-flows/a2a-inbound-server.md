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
breaker, activity rows, and cost tracking like any other trigger.

**All three trigger constants are wired** (the class in `learnings.md`
2026-07-30 — the one that degrades to "no filter" fails silently and lies):

| Constant | File | Value | Why |
|---|---|---|---|
| `_TRIGGER_BUCKETS` | `db/schedules/analytics.py` | `"Agent-to-agent"` | an inbound A2A task IS agent-to-agent work; it shares the bucket with `agent`/`self_task` rather than landing in `Other` |
| `_VALID_TRIGGERS` | `routers/executions.py` | listed | an unlisted value degrades to **no filter**, so `?triggered_by=a2a` would return every execution on the install |
| `_AUTONOMOUS_TRIGGERS` | `services/task_execution_service.py` | listed | **the judgement call.** The set means "no human is watching the reply", so an unresolved slash-command run earns an Operating Room alert (#1410). A2A is dispatched by a remote *machine* caller — structurally the same as `agent`, which is already in the set — so nobody on this install sees the "Unknown command" text come back. The interactive-ish triggers deliberately left out (`manual`, `mcp`, `public`, `chat`, `session`) all have a person reading the reply. |

## Schema / migration

`agent_ownership.a2a_exposed INTEGER DEFAULT 0` — dual-track (Invariant #3):

| Track | Artifact |
|-------|----------|
| SQLite | `db/migrations.py` → `_migrate_agent_ownership_a2a_exposed` |
| PostgreSQL | `migrations/versions/0035_agent_ownership_a2a_exposed.py` (`down_revision = "0033_agent_evaluations"` — re-chained onto the dev head at the 2026-08-05 rebase; the branch must carry exactly ONE Alembic head or `alembic upgrade head` refuses the branched graph, which is a PostgreSQL boot failure. 0034 is skipped deliberately: PR #1901 holds `0034_skill_sources` unmerged off the same head, so whichever lands second re-chains — skipping the number keeps the filenames from colliding too) |
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

## Exposed-skills filter (ent#180)

The card's `skills[]` is derived from the agent's `template.yaml capabilities[]`
(§32.1). `services/a2a_gate.py` carries a second provider hook —
`exposed_skills(agent_name) -> Optional[List[str]]` — and
`routers/a2a.py::_card_with_exposed_skills` applies it to **both** card surfaces
(public well-known + authenticated per-agent), so they can never disagree and a
future third surface inherits the filter.

OSS registers no provider, so the filter is the identity function and cards are
unchanged. `None` from a provider means "no opinion" → advertise everything (the
unconfigured default); `[]` means advertise nothing. A provider error or a
malformed return advertises unfiltered and logs at WARNING — fail-open, matching
the seam's other hook.

**Disclosure only.** `message/send` dispatches free-form text via `execute_task`,
so an unadvertised skill is hidden, not unreachable. Anything that constrains
what an external caller can actually reach would be a different mechanism
(`allowed_tools`/guardrails) with its own threat model.

## Payment gate (ent#679)

The door authenticated a Trinity MCP key and nothing else, so a stranger —
including a remote Trinity holding a perfectly good x402 payment token — got
**401** and could never reach a 402, never pay, never be served. Requirement:
`requirements/mcp.md` §32.6.

**One branch, decided by the principal.** `Depends(get_current_user)` became
`Depends(get_user_or_anonymous)`, a sibling that returns `None` on a **401 only**
and re-raises a **403**. That asymmetry is the point: the connector and
ephemeral-key fences raise 403, and a credential Trinity recognised and then
fenced must not slide onto the payment path and buy the access it was refused.

```
principal is not None  → TODAY'S PATH, byte-identical: the three gates → dispatch.
                         No facilitator call, no payment row, no payment metadata.
principal is None      → per-IP + per-agent rate limit (before any DB/SDK work)
                         → not exposed, or not priced   → today's 401 bytes + WWW-Authenticate
                         → priced but SDK absent        → 501
                         → exposed AND priced           → payment path
```

The free path is unchanged for internal fleet traffic, owner/shared callers and
subscription tenants — by construction, not by a carve-out. The only agents
whose anonymous answer differs from before are **exposed AND priced**, which
their public well-known card already publishes.

**Token, metadata first.** `params.message.metadata["x402.payment.payload"]` is
re-encoded to the access token the facilitator consumes; the `payment-signature`
header is the deprecated fallback. Metadata wins when both are present (the
provider SDK's own precedence), so a client migrating between rails cannot have
a stale header decide what it pays with. Re-encoding is signature-safe: the
EIP-712 signature lives *inside* the payload, not over the base64 envelope.
Every malformed shape falls through to the 402 rather than raising — all of it
is caller-controlled input on a route reachable with no credential.

**No extension handshake.** Trinity speaks the x402 message vocabulary and does
not negotiate. Nothing reads or emits `X-A2A-Extensions`.

**402/403 come from the paid door's own builders.** One requirements builder, two
doors — a 402 built differently from the later verify is a rejection the caller
cannot act on. `resource.url` names **this** door (`/a2a/{name}`): an x402 token
signs the resource URL, so a token minted against `/api/paid/{name}/chat` cannot
authorize an A2A call.

**The money logic is not here.** `services/paid_turn_service.py` is shared with
`routers/paid.py`, so the #1018 settle/replay/`success_unsettled` branches exist
once. `services/a2a_payment_gate.py` is only the A2A-shaped adapter: what a token
looks like on this wire, what a refusal looks like, and how an outcome becomes a
Task. Settlement detail: [nevermined-payments.md](nevermined-payments.md).

**The allow-list seam flips direction here.** `a2a_gate`'s allow-list fails
**open** for an authenticated caller (a restriction layered on auth). For a payer
it is consulted after verify with identity `x402:{payer}` and fails **closed** —
on this path the payment *is* the authorization, so a seam failure must not
void a configured control.

**A payer can retrieve what it paid for.** `tasks/get` / `tasks/cancel` are
allowed when the token verifies **and** the wallet matches that execution's
payment-log rows. Every mismatch — including payer A polling payer B's existing
task — answers byte-identical `-32001`, so the binding is not an existence
oracle. The binding is written **when the turn's result exists, before the
settle**: `run_paid_turn` logs a `verify` row carrying the execution id as soon
as `execute()` returns, because when only the `settle` / `settle_failed` rows
carried the pair, a payer was locked out of a finished task while its settle was
still running, and for good when a concurrent settle wrote no row. It is **not**
written mid-turn — `execute()` returns at the terminal, so a poll during the
turn reads as not-found and a payer's `tasks/cancel` can only answer "already in
a terminal state". No schema change; those columns were already on the row.

**A refusal is classified once, rendered twice.** `routers/a2a.py`'s
`_classify_paid_refusal` is the single table, consumed by `message/send` (which
can answer with an HTTP status) and by `message/stream` (which cannot — the
status line is gone by the time the body generator runs). Two properties it
exists to hold: a verify that could not DECIDE — facilitator timeout, SDK error,
saturated concurrency gate — is **our** unavailability, so it answers a JSON-RPC
error with `data.retryable: true` rather than the 403 a remote Trinity reads as
"stop retrying and buy another token", and is logged as a `verify` attempt
rather than a `reject`; and the stream never answers `-32001` for a payment
condition, since that is A2A **TaskNotFound** and tells a client its task
vanished when the truth is "pay" / "not allowed" / "retry". `data.code`
(`payment_rejected` · `verify_unavailable` · `not_allowed` · `in_flight` ·
`execution_error`) is the discriminator those four instructions need. The paid
door keeps its own 403 bytes unchanged.

**Attribution, no schema change.** The payer wallet on the `settle` row, the
execution row (`triggered_by="a2a"` + principal fields) and the platform audit
row (IP + payer) already carry it. No column, no migration.

**`triggered_by="a2a"` joins `INTERACTIVE_TRIGGERS`.** A remote caller waits
in-line on both the free and the paid path, so both get the caller-went-away
cancel and the claim budget — this changes queue treatment for the **principal**
path too, deliberately, and keeps the `a2a` analytics bucket honest rather than
filing paid A2A calls as REST paid chats.

### The card states the price

`_card_with_exposed_skills` is still **the** single card producer (ent#180
FR-3), and it gains one step: `a2a_card_service.with_payment_extension`. A priced
agent's card declares a `urn:nevermined:payment` extension carrying `agentId`,
`planId`, `credits`, `paymentType` and `paymentInfoUrl`
(`GET /api/paid/{name}/info`, the public "where to buy" document), so an
x402-speaking client mints a token from the card alone and meets the paywall on
its **first** request.

- The card builder stays pure; the config read lives in the router, like the
  skills provider lookup, and uses the no-decrypt `get_nevermined_config` — the
  card publishes plan ids, never the API key.
- **An unpriced or disabled agent's card is returned by identity** — byte-identical
  to before. Every install that sells nothing is untouched.
- The **official** A2A x402 extension URI is deliberately **not** declared, even
  though the provider SDK's own card helper appends it. Declaring an extension
  advertises its activation handshake; a generic client would activate it and
  then wait for a negotiation that never comes.
- `credits: 0` is a Nevermined **duration** plan (charged by time), declared as
  `paymentType: "dynamic"` — `fixed`/0 reads as free and the SDK's own card
  validator rejects that shape for a paid plan.
- Fail-open: an unreadable payment config serves the card with no price block
  and logs at WARNING. A card route has never 5xx'd, and the gate re-reads the
  config and still answers 402, so the only cost is a priced agent briefly
  looking free.

**In an OSS-only build, a configured price block points at a door that 404s.**
Exposure is set only by the entitled provider, so the paid A2A door is not
reachable; the card says what the agent *costs*, not that the door is *open*.
That is the same open-core line the paid chat door has always had — the gate is
OSS mechanism with no new entitlement and no enterprise-submodule code.

### Payment-gate testing

`tests/unit/test_ent679_a2a_payment_gate.py` (the gate, over a TestClient),
`tests/unit/test_ent679_paid_turn_service.py` (the shared orchestrator at its own
layer, with the paid door's three existing test files unedited as the behaviour
net) and `tests/unit/test_ent679_a2a_priced_card.py` (the card, both surfaces).

The real facilitator (verify + settle) and what a duration-plan settle actually
burns are **not** provable from the SDK source and are a live sandbox run before
merge, not a unit test. Stated rather than hidden.
