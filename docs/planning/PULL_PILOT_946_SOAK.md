# #946 Pull Pilot — Soak Harness & Phase 3 Decision Record

> **What this gates.** #946 routes **agent→agent** (`scope='agent'`, non-self)
> sequential `chat_with_agent` through the durable async `/task` path instead of
> the synchronous held `/chat`. It is a **Phase 2 proof-of-concept** for pull /
> work-stealing (Epic #1045, umbrella #1081). The deliverable is the **go/no-go
> decision record** in §4 — not a shipped feature.
>
> **Default OFF, reversible.** Flag `MCP_AGENT_CHAT_PULL_ENABLED` (MCP server +
> backend `config.py`). Rollback = flip OFF + restart the MCP server (and revert
> the one MCP routing block). No backend execution-path change, no new endpoint.
>
> **Contract for the pilot (verified, not assumed):**
> - **Polling-only.** `schedule_execution_completed` is named in a docstring
>   (`main.py` `/ws/events`) but **never emitted** — confirmed against the
>   current backend. The caller learns the result by **polling
>   `get_execution_result`** (`queued → running → success/failed`, incl. the
>   #914 `queued_timeout` contract). The pilot does **not** subscribe to a
>   non-emitted event.
> - **Agent→agent only.** `scope='user'` (human chat), self-tasks, and
>   `parallel=true` are unchanged. No irreversible external side effect rides
>   this path, so #1084 (effect-scoped idempotency) is not a prerequisite.
> - **Idempotency claim released on deny.** The `/task` dispatch-breaker deny
>   path now releases the idempotency claim (#946 T5 fix), so a breaker-open
>   reject doesn't silently block same-key retries for 24h.
> - **Stateless turn — continuity delta (watch this).** Sequential `/chat` runs
>   inside the target's persistent chat session; the pull-routed `/task` runs a
>   stateless headless turn with **no** cross-call conversation memory. For a
>   one-shot delegation ("do X, report back") this is invisible; for a *multi-turn*
>   agent→agent exchange the target no longer remembers prior turns. This is the
>   pull model's intended semantic (per the postcard's `continue_session`
>   retirement), **not** a regression — but it is the behavioral change most
>   likely to surface a real divergence in the soak. Watch treatment-window
>   delegations for callers that assumed the target retained context.

---

## 1. Setup (one-time)

Two agents on the dev instance, a permission grant, and a synthetic caller:

1. **`pilot-caller`** and **`pilot-target`** — any minimal template (the
   target just needs to answer a trivial prompt).
2. Grant `pilot-caller → pilot-target` in the Permissions tab (or
   `agent_permissions` row). Required for agent→agent `chat_with_agent`.
3. **Synthetic traffic** — a schedule on `pilot-caller` that fires every N
   minutes and runs the agent-side equivalent of:
   `chat_with_agent(agent_name="pilot-target", message="ping <run-id>")`
   (sequential — `parallel=false`). This is the **same A→B→poll→success path**
   the unit tests stub; the soak is its live E2E. Reuse the `config/canary-fleet.yaml`
   load-generator pattern, or a `run_agent_loop` against `pilot-caller`.

Confirm the active mode at any time:
```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/api/settings/feature-flags | jq .mcp_agent_chat_pull_enabled
# and the MCP server startup log: "Agent→agent chat pull routing (#946): ON|OFF"
```

## 2. Toggle matrix (alternating windows — not anecdotal)

| Window | `MCP_AGENT_CHAT_PULL_ENABLED` | Arm | Routes to |
|--------|-------------------------------|-----|-----------|
| W1 | `false` | **control** | sync `/chat` |
| W2 | `true`  | **treatment** | async `/task` |
| W3 | `false` | control | sync `/chat` |
| W4 | `true`  | treatment | async `/task` |

- **Same agent pair, same message cadence, same window length** across all
  windows. Alternate (not one long ON block) so a drift in load or Claude-API
  latency hits both arms.
- Procedure per flip: set the env var on the MCP server (and backend, for the
  observability flag) → restart the MCP server → confirm via the feature-flags
  read above → run for the fixed window → snapshot the §3 metrics with the
  window's `[start,end)` bounds.
- Suggested: ≥4 windows, ≥1h each, ≥30 executions/window so p95 is meaningful.

## 3. Metric collection points (per window)

All from `trinity.db` (`schedule_executions`, scoped to the synthetic pair and
the window). Use the agent name(s) and an ISO `started_at` window to bound each
arm. Examples (sqlite3):

**a. E2E latency (p50/p95) — execution time + queue wait.**
```sql
-- duration_ms = execution time; queue wait = started_at - queued_at (ms).
SELECT
  COUNT(*)                                              AS n,
  AVG(duration_ms)                                      AS dur_avg_ms,
  -- p50/p95 computed client-side over the ordered duration_ms list:
  GROUP_CONCAT(duration_ms)                             AS dur_samples,
  AVG((julianday(started_at) - julianday(queued_at)) * 86400000.0) AS queue_wait_avg_ms
FROM schedule_executions
WHERE agent_name = 'pilot-target'
  AND triggered_by = 'agent'
  AND started_at >= :win_start AND started_at < :win_end
  AND status IN ('success','failed');
```
Compute p50/p95 from `dur_samples` (sort, index). Report **receipt→terminal**
wall-clock too (the caller measures it: time from the `{accepted|queued}`
receipt to the polled terminal) — that is the number the orchestrator feels.

**b. Duplicate-execution rate (idempotency replays).** A replay means the same
`Idempotency-Key` was seen twice — the dedup working, but a *rising* rate vs
control signals retry churn (e.g. callers re-issuing on a slow receipt).
```sql
SELECT COUNT(*) AS idempotent_replays
FROM audit_log
WHERE event_action = 'idempotent_replay'
  AND target_id = 'pilot-target'
  AND timestamp >= :win_start AND timestamp < :win_end;
```
Rate = `idempotent_replays / total dispatches` in the window.

**c. Agent-side error rate (terminal `error_code`).** Watch for a **new error
class** appearing only under treatment.
```sql
SELECT COALESCE(error, '(none)') AS terminal_error, COUNT(*) AS n
FROM schedule_executions
WHERE agent_name = 'pilot-target'
  AND triggered_by = 'agent'
  AND started_at >= :win_start AND started_at < :win_end
  AND status = 'failed'
GROUP BY terminal_error
ORDER BY n DESC;
```
`circuit_open` / `lease_expired` / `Agent at capacity…` are the deny/recovery
terminals to expect; anything novel is a finding.

**d. Sanity: did treatment actually route async?** Under treatment the rows are
created via the `/task` path. Cross-check the MCP server logs for
`[Agent Chat Pull #946] Routing pilot-caller -> pilot-target via async /task`
during W2/W4 and its absence during W1/W3.

## 4. Rollback criteria + Phase 3 decision record

**Flip OFF immediately (rollback) if, in any treatment window:**
1. **Duplicate-execution rate > control** (treatment replays/dispatch exceeds
   the control arm's — beyond noise), or
2. **p95 e2e latency regresses beyond the agreed bound** (set the bound before
   W1; suggested: treatment p95 ≤ 1.25× control p95 for receipt→terminal), or
3. **Any new terminal-error class appears** under treatment that is absent in
   control (§3c).

**Go/no-go rule (the decision):**
> **GO** (recommend advancing the pull direction, #1081) iff across all
> treatment windows: duplicate rate ≤ control, p95 within the agreed bound, and
> zero new terminal-error classes — AND the receipt-then-poll contract held
> (no caller had to fall back to a duplicate dispatch).
> **NO-GO** otherwise: keep the flag OFF, record which criterion failed, and
> feed it back into #1081's design.

### Decision-record scaffold (fill in at Phase 3)

```
# #946 Pull Pilot — Decision Record (Phase 3)

Date:                <yyyy-mm-dd>
Windows run:         <W1..Wn, start/end, executions per window>
Agreed p95 bound:    <set before W1>

## Results (control vs treatment)
| Metric                       | Control | Treatment | Δ | Within bound? |
|------------------------------|---------|-----------|---|---------------|
| n executions                 |         |           |   |               |
| e2e p50 (receipt→terminal)   |         |           |   |               |
| e2e p95 (receipt→terminal)   |         |           |   |               |
| queue wait avg (ms)          |         |           |   |               |
| duplicate-execution rate     |         |           |   |               |
| agent error rate             |         |           |   |               |
| new terminal-error classes   |   n/a   |           |   |               |

## Envelope finding (Codex caveat)
Did the pilot's reliance on the existing backlog_metadata / ParallelTaskRequest
reconstruction shape reveal anything about whether the #945 envelope can cleanly
REPLACE that shape as a wire format? <notes — input to the 6 demotion PRs>

## Verdict:  GO | NO-GO
Rationale:   <which criteria passed/failed>
Next step:   <advance #1081 phase | keep OFF + redesign>
Ruled out:   <what this pilot explicitly did NOT test — capacity-physical model,
             chat-semantics-preserving drain, effect-scoped idempotency (#1084)>
```

---

## See also
- [`ACTOR_MODEL_POSTCARD.md`](ACTOR_MODEL_POSTCARD.md) — the #945 envelope + result-reporting contract this pilot rides
- [`TARGET_ARCHITECTURE.md`](TARGET_ARCHITECTURE.md) §Coordination Model, §Async-First Communication
- **#946** pilot · **#1081** umbrella · **#1082**/**#1083** bankable wins · **#1084** effect-scoped idempotency (Direction A; reframed to retry-with-trace + tool-side gates in `TARGET_ARCHITECTURE.md` v2 — #1401/#1402, no longer *the* gate)
