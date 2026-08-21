# Message Envelope — Payload-per-Kind Schema (#945)

> **Status**: Contract spec. Companion to [`ACTOR_MODEL_POSTCARD.md`](ACTOR_MODEL_POSTCARD.md).
> Gates the pull pilot ([#946](https://github.com/abilityai/trinity/issues/946)).
>
> **What this is.** The postcard is the **decision record**: it pins the message
> envelope's outer frame and the **typed terminal-reason taxonomy** (`status` +
> `error_code`) that retires the substring failure classifier. This document is
> the **field-level reference** the postcard points to: for every message that
> crosses the pull coordination boundary, it fixes the concrete payload — field
> names, types, required/optional, and exactly how the pinned taxonomy is
> embedded.
>
> **Why a separate file (not a postcard section).** The postcard's value is that
> the model "fits on a postcard" — a terse decision record. A full field-by-field
> schema for four `kind`s plus the two transport messages would bury that. Keeping
> the reference here leaves the postcard as the gate and this as the contract it
> cites; the two are cross-linked. This doc **does not re-decide anything** in the
> postcard or `TARGET_ARCHITECTURE.md` — where those are silent it says so
> explicitly (see [§6 OPEN](#6-open--needs-decision-session)) rather than filling
> the gap.
>
> **Honest scope caveat (carried from the postcard).** For the pilot the envelope
> is a **documented contract, not a physically enforced wire format**. The pilot
> rides the existing `backlog_metadata` / `ParallelTaskRequest` reconstruction
> shape; whether the envelope can cleanly *replace* that shape is itself a Phase 3
> finding (postcard §"scope caveat"; [PULL_PILOT_946_SOAK.md](PULL_PILOT_946_SOAK.md)
> §4 "Envelope finding"). Physically enforcing it is the six demotion PRs in
> [`ACTOR_MODEL_TASK_DEMOTION_MAP.md`](ACTOR_MODEL_TASK_DEMOTION_MAP.md).

---

## Scope — the messages that cross the pull boundary

Under pull / work-stealing ([TARGET_ARCHITECTURE.md](TARGET_ARCHITECTURE.md)
§"Coordination Model"), the backend owns one durable per-agent queue; an idle
agent worker pulls the head row, runs the turn, and reports the terminal back.
Exactly three message shapes cross that boundary, plus a control message:

```
  producer (scheduler / webhook / chat / agent→agent)
        │  enqueue envelope  (kind = chat | task | event)
        ▼
  ┌───────────────────────── backend queue ─────────────────────────┐
  │  schedule_executions row: status queued → claimed → running …    │
  └──────────────────────────────────────────────────────────────────┘
        │  GET /api/internal/next-task   → §3.1  CLAIM RESPONSE
        ▼                                  (the claimed envelope + lease)
   agent worker  ── runs the Claude turn ──┐
        │  POST /api/internal/tasks/{id}/result   → §3.3  RESULT (kind = reply)
        ▼
  backend applies terminal under a compare-and-set guard (#1082)
```

Message kinds specified here:

| # | Message | Direction | Section |
|---|---------|-----------|---------|
| 1 | **Envelope frame** (common outer header) | both | [§1](#1-envelope-frame-common-header) |
| 2 | `chat` payload | enqueue | [§2.1](#21-kind-chat) |
| 3 | `task` payload | enqueue | [§2.2](#22-kind-task) |
| 4 | `event` payload | enqueue (fanned out per subscriber) | [§2.3](#23-kind-event) |
| 5 | `reply` payload (typed terminal-reason) | result / join | [§2.4](#24-kind-reply--the-typed-terminal-reason) |
| 6 | **Claim response** — `GET /api/internal/next-task` body | backend → worker | [§3.1](#31-claim-response-get-apiinternalnext-task) |
| 7 | **Result POST** — `POST /api/internal/tasks/{id}/result` body | worker → backend | [§3.3](#33-result-post-post-apiinternaltasksidresult) |
| 8 | **Lease renewal / heartbeat** (control) | worker → backend | [§3.5](#35-lease-renewal--heartbeat-control) — **OPEN** |

Side-effect idempotency (`effect_guard` / native provider tokens, #1084 →
v2 #1401/#1402) is **not** carried in the envelope. Per `TARGET_ARCHITECTURE.md`
v2 it is gated **per-effect at the Trinity-owned tool**, not threaded through the
message. The envelope carries only the coordination fields below.

---

## 1. Envelope frame (common header)

Every enqueued task and every agent-to-agent message carries this outer frame,
stored in the queue row's payload column. It is the single unit of enqueue,
re-delivery, and dedup. Verbatim from the postcard; types tightened here.

```json
{
  "id": "3f9c…",
  "kind": "chat | task | event | reply",
  "from": "agent-alpha | 42 | system",
  "to": "agent-beta",
  "correlation_id": "3f9c…",
  "causation_id": "1a2b…",
  "idempotency_key": "sched:exec-7|<sha256>",
  "deadline": "2026-07-02T14:03:00Z",
  "payload": {}
}
```

| Field | Type | Req? | Meaning |
|-------|------|------|---------|
| `id` | string (uuid) | **required** | Unique message id. Reused **verbatim** on lease-expiry re-delivery (same unit of work; never a half-finished turn resumed). |
| `kind` | enum `chat`\|`task`\|`event`\|`reply` | **required** | Selects the `payload` schema (§2). |
| `from` | string | **required** | Origin. One of: `<agent_name>`, a stringified `<user_id>` (`users.id`), or the literal `system`. |
| `to` | string (agent_name) | **required** | Target agent whose queue this lands in. An `event` is fanned out into one envelope **per subscriber**, so each queued copy has a concrete `to`. |
| `correlation_id` | string (uuid) | **required** | Groups all messages of one workflow. For a root message, `correlation_id == id`. |
| `causation_id` | string (parent message `id`) | optional | The message that caused this one; `null` for a root. Carries `inject_result` semantics (demotion map #13): if it points at a chat-session message, the completion projector writes the result back into that session. |
| `idempotency_key` | string (opaque) | **required** | Derived from call args (RELIABILITY-006 / [#525](https://github.com/abilityai/trinity/issues/525)). Reused verbatim on re-delivery, so a duplicate result POST is absorbed by the compare-and-set guard. Dedups the **trigger**, not the agent's downstream side effects (§2.4 note; #1084/v2). |
| `deadline` | string (ISO 8601 UTC, `…Z` — Invariant #16) | **required** | Computed by the backend from `agent.execution_timeout_seconds` (#665) or `schedule.timeout_seconds` (#913). Drives the lease: `lease_expires_at = deadline + grace`. Producer-supplied per-task timeout overrides are demoted (map #1). |
| `payload` | object | **required** | Per-`kind` schema — §2. |

`payload` is the only `kind`-dependent part. The platform (dispatch, projector,
audit, observability) routes on the frame **without reading inside `payload`** —
that is the property the demotion map (`ParallelTaskRequest` → envelope) exists
to guarantee.

---

## 2. Per-`kind` payloads

Postcard summary (authoritative shapes):

```
chat   → { message, session_id, file_ids? }
task   → { message, session_id?, file_ids?, task_overrides? }
event  → { event_type, data }
reply  → { in_reply_to, content, <typed terminal-reason> }
```

Shared payload fields (defined once):

| Field | Type | Meaning |
|-------|------|---------|
| `message` | string | The constructed turn text handed to the runtime. Context-prompt construction stays **server-side**; the original user text (old `user_message`, map #12) is journal metadata, not a peer field. |
| `session_id` | string (uuid) \| null | Claude Code session UUID. Resolved **server-side** from `agent_sessions.cached_claude_session_id` (SESSION_TAB pattern). Unifies old `save_to_session` / `chat_session_id` / `resume_session_id` / `create_new_session` (map #8–#11): **presence ⇒ persist/resume; absence ⇒ stateless cold turn.** |
| `file_ids` | array\<string\> \| null | Out-of-band references into the FILES-001 shared-files volume; replaces inline file bytes (map #3). Each id is an `agent_shared_files.id` / download token the agent resolves from storage. |

### 2.1 `kind: chat`

Human or agent conversational turn that runs inside the target's session.

```json
{ "message": "summarise the Q3 memo", "session_id": "b1e2…", "file_ids": ["f_9a…"] }
```

| Field | Type | Req? | Notes |
|-------|------|------|-------|
| `message` | string | **required** | See shared table. |
| `session_id` | string (uuid) \| null | **required** for `chat` | A chat turn is session-bound by definition; `null` ⇒ first (cold) turn that writes the JSONL for turn 2. |
| `file_ids` | array\<string\> \| null | optional | See shared table. |

### 2.2 `kind: task`

Headless / delegated turn (scheduler, webhook, agent→agent delegation). May be
stateless.

```json
{
  "message": "run the weekly recon and report",
  "session_id": null,
  "file_ids": null,
  "task_overrides": { "allowed_tools": ["mcp__trinity__report"], "system_prompt": "…" }
}
```

| Field | Type | Req? | Notes |
|-------|------|------|-------|
| `message` | string | **required** | See shared table. |
| `session_id` | string (uuid) \| null | optional | Omitted/`null` ⇒ stateless headless turn (the #946 pilot's mode — no cross-call memory; per postcard `continue_session` retirement). |
| `file_ids` | array\<string\> \| null | optional | See shared table. |
| `task_overrides` | object (`TaskOverrides`) \| null | optional | **Explicit quarantine** for the few genuinely per-task knobs that survive demotion (map #5, #6). All other former `ParallelTaskRequest` fields are demoted — nothing else rides the task payload. |

`TaskOverrides` sub-object:

| Field | Type | Req? | Notes |
|-------|------|------|-------|
| `allowed_tools` | array\<string\> \| null | optional | `--allowedTools` restriction (map #5) — fan-out branches that need per-call variance. |
| `system_prompt` | string \| null | optional | `--append-system-prompt` (map #6). |
| `max_turns` | int \| null | optional | `--max-turns` (map #7). **Pending deletion** — the demotion map slates this for removal if the `git grep` audit finds no consumer; kept in the schema only until that PR lands. Do **not** treat as a stable field. |

Model selection is **not** a task field: per-call `model` is demoted to a
session/schedule attribute (map #2, MODEL-001). `async_mode` disappears —
everything is async; sync is the `?wait=true` edge adapter (map #4).

> **Pilot reality (#2317).** The table above is the TARGET shape. Until the
> demotion PRs land, the producer of a queued row (`backlog_service.enqueue`)
> still records a per-task `model` and `timeout_seconds`, the push path enforces
> both, and the pull worker reads both off `task_overrides` — so the claim
> envelope built by `pull_coordination_service._build_claim_response` carries
> `model` and `timeout_seconds` alongside the three fields above. They leave the
> payload when the demotion that removes them from `ParallelTaskRequest` lands,
> not before; dropping them from the wire earlier is a silent correctness
> regression on the pull path (that was #2317).

### 2.3 `kind: event`

Agent event pub/sub (EVT-001). Emitted once by a source agent; the projector
fans it out into one envelope **per subscriber** (each with a concrete `to` and
`causation_id` = the emit).

```json
{ "event_type": "recon.competitor_change", "data": { "competitor": "acme", "delta": "pricing" } }
```

| Field | Type | Req? | Notes |
|-------|------|------|-------|
| `event_type` | string (namespaced) | **required** | e.g. `recon.competitor_change`. Matches the subscriber's `agent_event_subscriptions.event_type`. |
| `data` | object (JSON) | **required** | Event body. Rendered into the subscriber's `target_message` template (`{{payload.field}}`) when the fanned-out envelope is materialised. |

> Fan-out delivery detail: the subscriber's queued copy is a `task` (or `chat`)
> envelope whose `message` is the rendered `target_message`; `data` is the raw
> event body the template drew from. The `event` payload above is the **source**
> shape the projector consumes, not what the subscriber's worker claims.

### 2.4 `kind: reply` — the typed terminal-reason

**The load-bearing part of #945.** A `reply` payload carries a **typed terminal
outcome the agent produces** — never a reason the backend infers from exit codes
or stderr substrings. It is the result of a completed turn (posted back via
§3.3) and the assembled output of a fan-out join.

```json
{
  "in_reply_to": "3f9c…",
  "content": "Done. 4 competitors changed pricing; report published.",
  "status": "success",
  "error_code": null,
  "cost": 0.0123,
  "tokens": 8421,
  "session_id": "b1e2…"
}
```

| Field | Type | Req? | Notes |
|-------|------|------|-------|
| `in_reply_to` | string (request message `id`) | **required** | The `id` of the request envelope this replies to. On the pull result path the task id is **also** in the URL (`/tasks/{id}/result`) — relationship flagged [OPEN-3](#6-open--needs-decision-session). |
| `content` | string | **required** | The result text. (Live #1083 wire calls this `response` — [OPEN-2](#6-open--needs-decision-session).) |
| `status` | enum `success`\|`failed` | **required** | Agent-produced terminal outcome. **Postcard pins exactly `success \| failed`.** Whether a third `cancelled` value is contractual is [OPEN-1](#6-open--needs-decision-session). |
| `error_code` | enum (see §4) \| null | **required when `status="failed"`**; `null` on success | The typed failure class. **This is the field the platform reads instead of guessing.** Taxonomy pinned in §4. |
| `cost` | number (float, USD) | **required** | Turn cost. Must come from the **agent-owned out-of-band record**, not parsed `stdout` (postcard §2 / #548/#333). |
| `tokens` | int | **required** | Tokens consumed this turn (same durable-record provenance). |
| `session_id` | string (uuid) \| null | **required** | The Claude session id the turn actually ran under (audit; changes on resume-fallback/reset). |

**Why typed, not inferred.** This is the structural cure for the
**MISCLASSIFIED_FAILURE** class: the auth-substring classifier was re-patched 5+
times across three hand-synced container copies because every new kill/OOM shape
carrying an auth substring re-triggered a false subscription switch. The platform
reads a **typed field**; the agent classifies at the source.

---

## 3. Transport / boundary control messages

The two pull endpoints (`TARGET_ARCHITECTURE.md` §"Coordination Model" #2) sit
behind `X-Internal-Secret`; agents reach them over the backend API only. Both
are **target-state** shapes — they are unbuilt today (the pilot #946 rides the
sync `/chat` + existing `/task` reconstruction; see scope caveat).

### 3.1 Claim response (`GET /api/internal/next-task`)

Long-poll; woken by the Redis enqueue hint. Returns the head row via the atomic
claim (`UPDATE … status='claimed', lease_expires_at=…, claimed_by_worker=…
WHERE id=(SELECT … ORDER BY queued_at LIMIT 1) RETURNING`). **200** body = the
claimed **envelope frame** (§1) plus claim metadata:

```json
{
  "envelope": { "id": "3f9c…", "kind": "task", "from": "system", "to": "agent-beta",
                "correlation_id": "3f9c…", "causation_id": null,
                "idempotency_key": "sched:exec-7|…", "deadline": "2026-07-02T14:03:00Z",
                "payload": { "message": "…", "session_id": null } },
  "execution_id": "exec-7",
  "lease_expires_at": "2026-07-02T14:08:00Z",
  "claimed_by_worker": "agent-beta#w2",
  "redelivery_count": 0,
  "prior_trace": null
}
```

| Field | Type | Req? | Notes |
|-------|------|------|-------|
| `envelope` | object (§1) | **required** | The claimed message, frame + `payload`. |
| `execution_id` | string | **required** | `schedule_executions.id` the terminal POST addresses. Distinct from the message `id`; stable across re-delivery. |
| `lease_expires_at` | string (ISO 8601 UTC) | **required** | `deadline + grace`. A heartbeat (§3.5) renews it; the lease-reaper re-queues on expiry. |
| `claimed_by_worker` | string | **required** | Opaque worker identity (`{agent}#{worker}` or a replica-scoped id) for lease attribution across replica groups. |
| `redelivery_count` | int | **required** | 0 on first delivery; incremented per lease-expiry re-queue. Bounds the `MAX_REDELIVERY` poison-park (#1402). Distinct from the existing `retry_count` column (#678 reader-race). |
| `prior_trace` | object \| null | optional | **(v2 #1401)** The structured, three-state (`done`/`not-done`/`unknown`) recovery trace of the prior failed attempt, injected so the retried turn recovers from hindsight. `null` on first delivery. **Internal structure is owned by #1401 — [OPEN-6](#6-open--needs-decision-session); this schema reserves the field name and nullability only.** |

### 3.2 Empty claim (no work)

Long-poll timeout with an empty queue. **204 No Content** (no body), or **200**
with `{"envelope": null}` — the worker re-polls. (204-vs-empty-200 is an
implementation choice for #1081, not a contract decision; either is
unambiguous.)

### 3.3 Result POST (`POST /api/internal/tasks/{id}/result`)

The worker reports the terminal. Path `{id}` = `execution_id`. **Request body =
the `reply` payload (§2.4)** plus the durable-record fields the backend persists;
applied under the compare-and-set guard (#1082 status-as-projection).

```json
{
  "status": "success",
  "content": "Done. Report published.",
  "error_code": null,
  "cost": 0.0123,
  "tokens": 8421,
  "session_id": "b1e2…",
  "execution_log": [ /* recovered JSONL transcript, sanitised */ ],
  "metadata": { "context_window": 8421, "compact_events": 0 }
}
```

| Field | Type | Req? | Notes |
|-------|------|------|-------|
| `status` | enum `success`\|`failed` | **required** | Selects success-style (reconcile-on-lost-CAS) vs failure-style applier. See [OPEN-1](#6-open--needs-decision-session) re: `cancelled`. |
| `content` | string | **required** | Result text (§2.4). |
| `error_code` | enum §4 \| null | required on `failed` | The typed failure class fed to the reply/breaker logic (only `AUTH` feeds the dispatch breaker → alert, per pull). |
| `cost`, `tokens`, `session_id` | see §2.4 | **required** | From the agent-owned out-of-band record, **not** parsed `stdout` (postcard §2, #548/#333). |
| `execution_log` | array (JSON transcript) \| null | optional | Recovered JSONL, sanitised. Provenance = durable record. |
| `metadata` | object | optional | Extra observability (context window, compact events, cache tokens). Contractual status of the richer block is [OPEN-2](#6-open--needs-decision-session). |

**Authorities split** (postcard §2): execution **state** → the backend
`schedule_executions` row (CAS-guarded); result **payload** (cost/tokens/session
/content) → the agent-owned durable record uploaded here. Re-delivery reuses the
same `execution_id` + `idempotency_key`, so a duplicate result POST is absorbed
by the CAS.

### 3.4 Result POST response

Backend ack. Mirrors the live #1083 callback response semantics.

| Outcome | HTTP | Body | Meaning |
|---------|------|------|---------|
| Applied | 200 | `{ "applied": true }` | Terminal written (CAS won). |
| Idempotent replay | 200 | `{ "replayed": true }` | An **authoritative** terminal (SUCCESS/CANCELLED/SKIPPED) already exists; short-circuited. A prior FAILED still falls through so a late SUCCESS can overwrite a reaper `LEASE_EXPIRED`. |
| Marker / ownership mismatch | 409 / 404 | error | Not a claimable/RUNNING row under this worker, or agent mismatch. |
| Throttled (governor) | 503 + `Retry-After` | error | #1085 re-delivery governor paused/capped — **retryable, never a drop** (the worker keeps the persisted record and retries). |

### 3.5 Lease renewal / heartbeat (control)

A worker running a legitimately long turn **renews** its lease so the reaper does
not re-queue it (`TARGET_ARCHITECTURE.md` §"Recovery: Lease-Expiry
Re-Delivery": "A heartbeat from the worker *renews* the lease"). The message that
carries this renewal — its endpoint, cadence, and payload (worker id? progress
marker? new `lease_expires_at` echoed back?) — is **not specified** in the
postcard or `TARGET_ARCHITECTURE.md`. **[OPEN-4](#6-open--needs-decision-session).**
Reserved shape only:

```json
{ "claimed_by_worker": "agent-beta#w2" }   // → renews lease for exec {id}; fields OPEN
```

---

## 4. Typed terminal-reason taxonomy (the pinned contract)

Restated **verbatim** from the postcard (authoritative source). `status` and
`error_code` are the two fields the `reply`/result payload embeds; this section
fixes their value sets so the field-level schema above is self-contained.

**`status`** (agent-produced terminal outcome, in the `reply` payload):

```
success | failed
```

> Postcard pins exactly these two. The live #1083 wire additionally emits
> `cancelled`; whether that is contractual on the pull path is [OPEN-1](#6-open--needs-decision-session).
> This is **distinct** from the backend execution-state machine
> `TaskExecutionStatus` (`queued → claimed → running → success|failed|cancelled
> |skipped|pending_retry`), which is the row projection, not the payload field.
> (`claimed` is a target-state addition named in `TARGET_ARCHITECTURE.md`'s
> atomic-claim SQL; not in today's enum.)

**`error_code`** (typed failure class; `null` on success). The concrete backend
enum today is `TaskExecutionErrorCode`
(`src/backend/services/task_execution_service.py`):

```
TIMEOUT | CAPACITY | AUTH | BILLING | AGENT_ERROR | NETWORK | CIRCUIT_OPEN | RECONCILED | LEASE_EXPIRED
```

The contract **additionally pins** two failure shapes the agent should report
rather than collapse into `AGENT_ERROR`:

```
OOM | MAX_TURNS
```

> Per the postcard: "the enum grows to meet the contract, not the other way
> round." `OOM` and `MAX_TURNS` are contract-required additions to the code enum,
> not fields this doc invents. Pinning this taxonomy is a **coordination-contract
> requirement**, not an agent implementation detail — it is the highest-value
> half of #945.

Value semantics (from the code enum + postcard):

| `error_code` | Meaning |
|--------------|---------|
| `TIMEOUT` | Execution exceeded its deadline. |
| `CAPACITY` | All parallel slots in use (admit-time reject). |
| `AUTH` | No API key / subscription (agent answers 503). **Only value that feeds the dispatch breaker → alert.** |
| `BILLING` | Rate limit / credit / billing issue (agent 429). |
| `AGENT_ERROR` | Agent returned a non-zero exit with no more specific class. |
| `NETWORK` | HTTP/connection error to the agent container. |
| `CIRCUIT_OPEN` | Breaker open — agent known unhealthy (#767). |
| `RECONCILED` | Terminal write lost the CAS; row reflects another writer's terminal (#671/H4). |
| `LEASE_EXPIRED` | Lease expired — no result before the lease deadline (#1083). |
| `OOM` | Out-of-memory kill mid-turn (**contract addition**). |
| `MAX_TURNS` | `--max-turns` guardrail hit (**contract addition**). |

---

## 5. Contract vs the live #1083 wire shape — reconciliation findings

The closest existing wire format is the **fire-and-forget terminal envelope**
(`docker/base-image/agent_server/services/result_callback.py`), which the #1083
callback POSTs to `/api/agents/{name}/executions/{id}/result` today. It is the
pull result POST's ancestor and diverges from the postcard-pinned `reply` shape
in named ways. These divergences are exactly the "can the envelope *replace* the
reconstruction shape" question the postcard flags for **Phase 3** — recorded here
so the decision session has the concrete list, **not resolved**:

| Aspect | Postcard `reply` (pinned) | Live #1083 wire (`result_callback.py`) | Finding |
|--------|---------------------------|----------------------------------------|---------|
| Result text field | `content` | `response` | Field rename — [OPEN-2](#6-open--needs-decision-session). |
| Cost / tokens | top-level `cost`, `tokens` | nested under `metadata` (`cost_usd`, `tokens`) | Placement — [OPEN-2](#6-open--needs-decision-session). |
| `status` values | `success` \| `failed` | `success` \| `failed` \| `cancelled` | Extra `cancelled` — [OPEN-1](#6-open--needs-decision-session). |
| Finer reason | (none — `status` + `error_code` only) | `terminal_reason` (`completed`/`auth`/`max_duration`/`empty_result`/`rate_limit`/`max_turns`/`error`/`cancelled`) | Is `terminal_reason` contractual or impl-only? [OPEN-5](#6-open--needs-decision-session). |
| Correlation | `in_reply_to` (message id) | URL path `execution_id` only | Redundancy — [OPEN-3](#6-open--needs-decision-session). |
| `error_code` casing | UPPER (`AUTH`) | lower (`"auth"`, mapped from `_STATUS_MAP`) | Cosmetic; same values. Note for the wire freeze. |

The live shape is a **superset in some fields** (`terminal_reason`, `metadata`,
`execution_log`) and a **subset in others** (no `in_reply_to`, no top-level
`cost`/`tokens`). The reconciliation is a Phase-3 wire-freeze decision, gated on
whether the pilot shows the envelope can replace the reconstruction shape.

---

## 6. OPEN — needs decision session

Genuine gaps the postcard / `TARGET_ARCHITECTURE.md` do **not** answer. **Not
resolved here** — a separate human-interactive design session owns these.

- **OPEN-1 — `reply.status` third value.** Postcard pins `success | failed`; the
  live #1083 wire emits `cancelled` (user-cancel / SIGINT relabel). Does the pull
  `reply` payload include `cancelled`, or does a user-cancel map to `failed` with
  a distinct `error_code`? Backend `TaskExecutionStatus` has `CANCELLED`, so the
  row can hold it regardless — the question is the **payload** field's value set.

- **OPEN-2 — result payload field names / shape.** `content` vs `response`;
  top-level `cost`/`tokens` vs nested `metadata`. Needs a single canonical wire
  freeze. (This is the postcard's Phase-3 "envelope replaces reconstruction
  shape" finding, made concrete.)

- **OPEN-3 — `in_reply_to` vs path `execution_id`.** On the pull result path the
  terminal is POSTed to `/tasks/{execution_id}/result`, so the correlation is
  already in the URL. Is `in_reply_to` still required in the body (for the
  fan-out-join / event-bus reply case where there is no URL id), redundant, or
  the canonical correlation with `execution_id` demoted? The relationship between
  message `id`, `execution_id`, and `in_reply_to` on the pull path is unpinned.

- **OPEN-4 — lease-renewal / heartbeat message.** The renewal that keeps a long
  turn's lease alive (§3.5) has no specified endpoint, cadence, or payload. Only
  its existence is pinned (`TARGET_ARCHITECTURE.md` §"Recovery").

- **OPEN-5 — `terminal_reason` contractual status.** The live wire carries a
  finer-grained `terminal_reason` beyond `status` + `error_code`. Is it part of
  the envelope contract (and if so, its value set), or an agent-server
  implementation detail the backend maps away? The postcard pins only
  `status` + `error_code`.

- **OPEN-6 — `prior_trace` internal structure.** The claim response reserves a
  `prior_trace` field (§3.1) for the v2 recovery-trace injection (#1401). Its
  internal schema — the three-state (`done`/`not-done`/`unknown`) step records,
  write-ahead markers, and how the agent locates its position — is owned by
  **#1401** (`status-incubating`), not this doc. This schema reserves the field
  name and nullability only.

None of the above blocks the #946 pilot: the pilot rides the existing
reconstruction shape and is agent→agent-only (no irreversible side effect, so
#1084/v2 does not apply). They block the **wire freeze** for physical envelope
enforcement (the demotion PRs, #1081 Phase 3+).

---

## See also

- [`ACTOR_MODEL_POSTCARD.md`](ACTOR_MODEL_POSTCARD.md) — the decision record / gate
  (envelope frame + pinned taxonomy) this doc details.
- [`TARGET_ARCHITECTURE.md`](TARGET_ARCHITECTURE.md) §"Coordination Model",
  §"Recovery: Lease-Expiry Re-Delivery", §"Async-First Communication".
- [`ACTOR_MODEL_TASK_DEMOTION_MAP.md`](ACTOR_MODEL_TASK_DEMOTION_MAP.md) — the
  `ParallelTaskRequest` → envelope demotion (the physical-enforcement pre-work).
- [`PULL_PILOT_946_SOAK.md`](PULL_PILOT_946_SOAK.md) — the pilot this gates; §4
  "Envelope finding" is where the reconciliation (§5 above) is decided.
- Code precedent for the result shape:
  `docker/base-image/agent_server/services/result_callback.py` (#1083 terminal
  envelope), `src/backend/services/task_execution_service.py`
  (`TaskExecutionErrorCode`, `TerminalEnvelope`, `apply_result`).
- **#945** (this spec) · **#946** pilot · **#1081** umbrella · **Epic #1045**.
