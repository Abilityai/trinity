# CSO Diff Audit — trinity#2214 Workspace turn bound = the agent's own timeout

- **Date**: 2026-08-16 · **Mode**: `--diff` (daily, ≥8/10 confidence gate) · **Branch**: `vybe/issue-2214` (6 commits vs merge-base `f5fe15f3`)
- **Scope**: 12 files — backend `client_portal/service.py` + `models.py`, `services/session_turn_service.py`; frontend `PortalConversation.vue`, `portalUtils.js`, `stores/clientPortal.js`; 3 test files; 3 docs
- **Verification**: independent fresh-context adversarial pass over the seven load-bearing claims below; every clean verdict carries a refutation cite

## Findings

**None at the ≥8 confidence gate.**

## Clean categories (what was checked)

| Axis | Verdict | Basis |
|---|---|---|
| Attack surface delta | CLEAN | no new endpoint, no auth-dependency change; ONE optional response field (`PortalHistory.in_flight_wait_budget_seconds`, `Optional[int] = None`) on the existing roster-scoped, session-ownership-gated `GET .../history`; one internal kwarg (`portal_chat(turn_timeout_seconds=)`) with two callers, neither caller-controlled (router sync path passes nothing → resolved server-side; `start_portal_turn` passes its own resolution) |
| Cross-client leak via the new field | CLEAN | `get_history` runs `agent_on_roster` → `db.get_portal_session(session_id, agent_name, email)` (uniform 404) BEFORE any marker read; the TTL key is `portal_inflight:{session_id}` derived only from the validated/caller-owned session id; a caller-supplied `session_id` that is not theirs never reaches the Redis read |
| Fail-open direction | CLEAN | `resolve_turn_timeout` → 3600 (platform DEFAULT, not the cap) on any raise; read-side clamp [60, 7200]; history TTL read: any exception → budget `None` + today's `in_flight_execution_id` behaviour preserved (`get_turn_inflight` still fails to `None`); `-2` → both fields `None`; `-1` → full per-agent budget (over-wait; a `lost` verdict never offers Retry) |
| Redis | CLEAN | one extra O(1) `TTL` per history fetch, only while a marker exists; keys server-minted; no SCAN, no user-shaped key material |
| Info disclosure | CLEAN | the budget arithmetic (and the 504 copy) let a client recover the agent's configured timeout — the bound governing that client's own turns, carried on the 202 since #2133; no operator jargon, no internals in the 504 (`turn_timeout` is always an `int` at that point) |
| Cost / abuse | ACCEPTED (design) | a portal client may now hold a turn up to the operator's configured `execution_timeout_seconds` (≤7200s) instead of 300s — this IS the operator's expressed intent (TIMEOUT-001); concurrency is still bounded by the per-thread resume lock (`ResumeLockBusy` → 429), the router's per-(email, agent) burst + hourly rate limits, and `max_parallel_tasks` via the capacity manager; no new amplification (the number of concurrent turns a client can mint is unchanged, only their length) |
| Frontend | CLEAN | `resolveWaitBudgetMs` coerces with `Number(...) > 0` — absent/0/NaN/negative/string → frozen fallback; the `lost` verdict path (`markFailed(..., {retryable:false})`) is unchanged, so no server value can re-enable Retry on a lost turn; no template/`v-html` change |
| Secrets in diff | CLEAN | `git diff` over the branch range: no key-shaped strings; the only literals are timeouts and test session ids |
| Enterprise-docs-guard (ent#45) | CLEAN | the guard's own pattern grepped over every added doc line: 0 hits; touched docs describe the OSS-core Workspace (ent#356) turn bound only |
| Vendored parity / channels / CI / Docker | N/A | untouched by this diff |

## Below the gate (recorded, not findings)

| Item | Confidence | Disposition |
|---|---|---|
| Resume-lock TTL is not resized with the turn: `run_resumable_turn` still takes `session_lock:{agent}:{uuid}` at `resolve_lock_ttl` = `min(t+30, 7230)` while one attempt can now reach `t+310` (the #678 reader-race retry) and a cold retry `2×(t+310)+60`; on the lapse a second sender on the same thread (second tab / headless `POST .../chat`) can acquire the lock and `--resume` the same JSONL concurrently. Pre-existing on the Session surface; this PR moves the exposed population onto the mainline surface. Reproducible only with cold-retry + long turn + concurrent send on one thread. | 3/10 | **Deferred by plan decision** (plan §6 risk #2, audit #11): engine-owned — sizing the lock from the attempt ceiling belongs in `session_turn_service`, not as a portal-only `lock_ttl=` that would leave the two engine callers on different lock policies. Ship-checklist item: file as **priority** follow-up. |
| Orphaned in-flight marker after a HARD backend kill now lives up to `portal_max_turn_seconds(7200)` = 15,080s (~4.2h) and holds one thread's composer disabled for its remaining TTL. | n/a | **Accepted + documented** (plan D3; `service.py` `portal_max_turn_seconds` docstring; requirements §5.9); operator escape `DEL portal_inflight:{session}`. |
| The 504 copy now names the agent's timeout to sync `POST .../chat` API clients (ent#83) that never saw the 202 budget. | n/a | Operator-set, non-secret, the bound governing that caller's own turn — cosmetic. |

## Verdict

A bound-widening change whose only new wire surface is one optional integer on an already-gated read. Every fallback fails toward the platform default or toward the client waiting *longer* without a Retry — never toward a re-bill or a cross-client read. Ship-clean.
