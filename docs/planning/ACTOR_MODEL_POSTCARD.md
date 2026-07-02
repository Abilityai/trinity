# The Actor-Model Postcard (#945)

> **Status**: Decision record. Unblocks the pull pilot (#946).
>
> **What this is.** The #945 "postcard" gate from the archived
> [`ORCHESTRATION_RELIABILITY_2026-04.md`](../archive/plans/ORCHESTRATION_RELIABILITY_2026-04.md)
> §"actor model" Phase 2: if the **message envelope** and the **result-reporting
> contract** both fit cleanly on a postcard, the coordination model is sound and
> the pilot proceeds. They do. This is that postcard.
>
> **What changed since the original gate.** The 2026-06-05 pivot to pull /
> work-stealing **retired "journal-as-source-of-truth."** Section 2 below is
> therefore *result reporting* (backend row authoritative for execution state),
> **not** "journal format." The envelope (Section 1) is unchanged by the pivot —
> it is still the unit of enqueue, re-delivery, and deduplication.
>
> **Honest scope caveat (carried from the #946 plan).** For the pilot the
> envelope below is a **documented contract, not a physically enforced wire
> format**. The pilot rides the existing `backlog_metadata` /
> `ParallelTaskRequest` reconstruction shape; whether the envelope can cleanly
> *replace* that shape is itself a finding for the Phase 3 decision record.
> Physically enforcing the envelope is the six demotion PRs in
> [`ACTOR_MODEL_TASK_DEMOTION_MAP.md`](ACTOR_MODEL_TASK_DEMOTION_MAP.md) — out of
> scope for #946.

---

## 1. Message Envelope

Every queued task — and every agent-to-agent message — carries this envelope,
stored in the queue row's payload column. It is the single unit of enqueue,
re-delivery, and dedup.

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

| Field | Type | Req? | Meaning |
|-------|------|------|---------|
| `id` | uuid | **required** | Unique message id. Reused verbatim on lease-expiry re-delivery (same unit of work, never a half-finished turn resumed). |
| `kind` | enum (`chat`/`task`/`event`/`reply`) | **required** | Selects the `payload` schema below. |
| `from` | string | **required** | Origin: agent name, user id, or `system`. |
| `to` | string (agent_name) | **required** | Target agent whose queue this lands in. (An `event` is fanned out by the projector into one envelope **per subscriber**, so each queued copy has a concrete `to`.) |
| `correlation_id` | uuid | **required** | Groups all messages of one workflow. For a root message, `correlation_id == id`. |
| `causation_id` | parent message id | optional | The message that caused this one; `null` for a root. Carries `inject_result` semantics for free (see demotion map #13): if it points at a chat-session message, the completion projector writes the result back into that session. |
| `idempotency_key` | opaque string | **required** | Derived from call args (RELIABILITY-006 / [#525](https://github.com/abilityai/trinity/issues/525)). Reused verbatim on re-delivery, so a duplicate result POST is absorbed by the compare-and-set guard. Dedups the **trigger**, not the agent's downstream side effects (see §2 and #1084). |
| `deadline` | iso8601 | **required** | Computed from `agent.execution_timeout_seconds` (#665) or `schedule.timeout_seconds` (#913). Drives the lease (`lease_expires_at = deadline + grace`). |
| `payload` | object | **required** | Per-`kind` schema below. |

### Per-`kind` payloads

```
chat   → { message, session_id, file_ids? }
task   → { message, session_id?, file_ids?, task_overrides? }
event  → { event_type, data }
reply  → { in_reply_to, content, <typed terminal-reason — see below> }
```

- `session_id` — Claude Code session UUID; resolved **server-side** from
  `agent_sessions.cached_claude_session_id` (the SESSION_TAB pattern). Unifies
  the old `save_to_session` / `chat_session_id` / `resume_session_id` /
  `create_new_session` / `user_message` task fields (demotion map #8–#12).
  (#13 `inject_result` is folded into `causation_id`, above — not `session_id`.)
- `file_ids` — out-of-band references into the FILES-001 shared-files volume;
  replaces inline file bytes (demotion map #3).
- `task_overrides` — explicit quarantine sub-object for the few genuinely
  per-task knobs that survive demotion (#5, #6).

### Load-bearing part: typed terminal-reason on `reply`

A `reply` envelope's `payload` MUST carry a **typed terminal outcome the agent
produces** — not a reason the backend infers from exit codes or stderr
substrings:

```json
{
  "in_reply_to": "<request_message_id>",
  "content": "<result text>",
  "status": "success | failed",
  "error_code": "AUTH | TIMEOUT | OOM | MAX_TURNS | AGENT_ERROR | NETWORK | …",
  "cost": 0.0,
  "tokens": 0,
  "session_id": "<uuid>"
}
```

This is the structural cure for the **MISCLASSIFIED_FAILURE** class: the
auth-substring classifier was re-patched 5+ times across three hand-synced
container copies, because every new kill/OOM shape that happened to carry an
auth substring re-triggered a false subscription switch. The platform reads a
**typed field** instead of guessing. (The concrete backend enum today is
`TaskExecutionErrorCode` =
`{TIMEOUT, CAPACITY, AUTH, BILLING, AGENT_ERROR, NETWORK, CIRCUIT_OPEN,
RECONCILED, LEASE_EXPIRED}`; this taxonomy is the contract it must satisfy. The
contract additionally pins `OOM` and `MAX_TURNS` — distinct failure shapes the
agent should report rather than collapse into `AGENT_ERROR` — so the enum grows
to meet the contract, not the other way round.)

Pinning this taxonomy is a **coordination-contract requirement**, not an agent
implementation detail — it is the highest-value half of #945.

---

## 2. Result Reporting (pull-reconciled)

> This section replaces the original gate's "journal format." Under pull,
> execution **state** is owned by the backend, not by an agent-authoritative
> journal — the deliberate departure from the earlier actor-model design, which
> made the agent's journal authoritative and thereby reintroduced cross-store
> reconciliation.

**Two authorities, cleanly split:**

1. **Execution state → backend `schedule_executions` row.** Applied from the
   worker's result POST under a **compare-and-set guard** (`#1082`
   status-as-projection). `queued → claimed/running → success/failed` is the
   backend's to own. The agent does **not** keep a parallel authoritative
   history. Inbox depth is `COUNT(*) WHERE status='queued'` (a single backend
   read), never a reconcilable agent-side copy.

2. **Result *payload* → an agent-owned, out-of-band, durable record.** The
   result *content* the worker uploads (cost / tokens / turns / session-id /
   the `{"type":"result"}` line) must come from a durable agent-written record
   (the recovered JSONL / a result file), **not** from parsed `stdout`. Today
   that terminal line rides the same `stdout` pipe Claude's grandchild
   processes inherit (fd 1), so it can be lost or truncated before the worker
   POSTs (#548 / #333) — and lease re-delivery would re-run a turn that
   null-everythings the same way. The out-of-band record is authoritative **for
   the uploaded payload**, even though the backend row stays authoritative **for
   execution state**. This is the one deliberate exception to "state is the
   backend's."

**`journal.ndjson` is optional debug-only.** An agent may keep a local
`~/.trinity/journal.ndjson` as an audit/debug aid. The platform does **not**
depend on projecting it and does **not** treat it as a source of truth. (#946's
original AC "agent writes a journal entry per processed message" is **dropped**
as an artifact of the pre-pivot design.)

**Completion signalling for the pilot is polling, not events.** Verified against
the current backend: `schedule_execution_completed` IS emitted — but only by the
**scheduler** (`src/scheduler/service.py`, several `_publish_event` sites) for
*schedule-triggered* runs, and **no agent-facing path consumes it** (no
`mcp-server`/`frontend` subscriber; `main.py` `/ws/events` only names it in a
docstring). Crucially it is **not** emitted on the agent→agent MCP path the #946
pilot uses. The caller therefore learns a result by **polling
`get_execution_result`** (the existing `queued → running → success/failed` path,
including the #914 `queued_timeout` contract). The pull pilot makes **polling the
contract**; it does not subscribe to an event that doesn't reach it. Hardening a
completion event into a first-class agent-consumable channel is deferred (out of
scope for #946).

**The contract, stated honestly.** The platform guarantees **at-least-once
delivery with an idempotent coordination boundary**. It cannot make a third
party's email/payment API exactly-once: lease re-delivery re-runs a whole turn,
re-emitting any irreversible external effect the first attempt performed. The
fix is agent-side recovery, not universal sink-keying — **`TARGET_ARCHITECTURE.md`
v2** reframes #1084 to **retry-with-prior-trace** (#1401) + **deterministic
tool-side gates** on capability-confined irreversible rails + an **async operator
human-gate** (#1402), gating **per-effect not per-agent**. (The earlier plan — an
`{execution_id}:{effect_ordinal}` key threaded through every outbound sink, #1084
as *the* gate — is superseded; the envelope/result contract in this postcard is
unchanged.) Agent→agent `chat_with_agent` (the #946
pilot's only routed call) has **no irreversible external effect**, so #1084 is
not needed for this pilot.

---

## See also

- [`TARGET_ARCHITECTURE.md`](TARGET_ARCHITECTURE.md) §Coordination Model,
  §Message Envelope, §Key Open Questions #1
- [`ACTOR_MODEL_TASK_DEMOTION_MAP.md`](ACTOR_MODEL_TASK_DEMOTION_MAP.md) — the
  `ParallelTaskRequest` → envelope demotion (6 PRs, the physical-enforcement
  work)
- [`ORCHESTRATION_RELIABILITY_2026-04.md`](../archive/plans/ORCHESTRATION_RELIABILITY_2026-04.md)
  §"actor model" — the historical gate this postcard satisfies
- **#946** — the pull pilot this unblocks · **#1081** umbrella · **Epic #1045**
