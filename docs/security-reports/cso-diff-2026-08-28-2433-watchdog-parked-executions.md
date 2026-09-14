# CSO Diff Audit — trinity#2433 watchdog false-orphans parked executions

**Scope**: `feature/2433-watchdog-parked-executions` vs merge-base `135248e9` · mode `--diff` · daily gate (≥8/10) · phases 0–2, 5–7, 9–14 (3/4/8 N/A: no dependency, workflow or skill change)

## Findings

| # | Sev | Conf | Status | Category | Finding | Phase | File |
|---|-----|------|--------|----------|---------|-------|------|
| 1 | HIGH | 9/10 | VERIFIED → **FIXED in-branch** | Access control (A01) | Cross-agent false-cancel through the proxy terminate arm via caller-supplied `task_execution_id` | P9/P12 | `src/backend/services/chat_execution_service.py` |

**Finding 1 — pre-existing, surfaced by the Phase-12 independent verifier.** `terminate_execution` takes `execution_id` (path) and `task_execution_id` (query param, defaults to the path id). The proxy arm POSTs `execution_id` to agent A — whose 404 is what scoped the old path — but writes CANCELLED and closes the dispatch activity keyed on `task_execution_id`, which the agent never sees. The branch's review had added agent-scope belts to the queued and parked arms; the verifier found the proxy arm carried none.

Exploit: a caller authorised on agent A with a live turn `X` learns agent B's id `T` and calls `POST /api/agents/A/executions/X/terminate?task_execution_id=T`. The queued and parked arms refuse (scoped); the proxy arm asks A about `X`, A answers `terminated`, `release_if_matches(A, T)` is a no-op, and `db.update_execution_status(T, CANCELLED)` wins the CAS on B's RUNNING row. B keeps running, never contacted, but its later SUCCESS loses the CAS — B's deliverable is discarded and its row reads as a user cancellation. Impact: cross-tenant false-cancel + result loss; no slot release, no process kill, no RCE.

Fix (in this branch): one agent-scope gate at the **entry** of `terminate_execution`, for all three arms — the row behind `task_execution_id` must belong to `name`, refused with the proxy's own uniform 404 so a foreign id reads exactly like an unknown one; an unreadable row fails **closed** (503). Per-arm belts are kept as a second layer. Tests replay the exploit, the fail-closed path and the positive controls (`tests/unit/test_2433_dispatch_wiring.py`); the pre-existing terminate suites are unchanged and green.

## Clean categories (what was checked)

- **Secrets (P2)**: diff-scoped prefix scan clean; no tracked `.env`; `backlog_metadata` producers untouched; zero enterprise-catalog tokens added to docs or seam files.
- **Infrastructure (P5)**: compose diff is two env forwards with defaults; no network/port/volume/socket change; vendored-parity, auth-header-guard, enumeration-uniformity, dependency-pairing and admin-gate guards: 79 passed.
- **Integrations (P6)**: internal router, webhooks, heartbeat / result-callback / report handlers untouched; no new raw `agent-{name}:8000` caller.
- **LLM/AI (P7)**: no prompt or LLM-output rendering change; the Redis marker payload is `{agent, phase, since, pid}` — no user content.
- **Injection (A03)**: all new writes are SQLAlchemy Core with bound parameters; `restamp_execution_dispatch` is CAS-guarded on `status='running' AND lease_expires_at IS NULL`.
- **STRIDE / data (P10–11)**: no new trust boundary; new data is INTERNAL (execution ids, agent names, pid).

## Below the gate (recorded, not findings)

1. `/ws` (`SCOPE_ALL`) broadcasts `agent_activity` events carrying execution ids and agent names fleet-wide to any authenticated user — the disclosure that made Finding 1 practical. Pre-existing documented design outside this diff; with the gate in place the id alone is no longer a write capability. Recommended follow-up: scope `/ws` `agent_activity` by `accessible_agents`, as `/ws/events` already does.
2. `pending_ids` is agent-asserted; the watchdog unions it only against that agent's own rows, so the blast radius is "keep my own rows alive".
3. Path-supplied execution ids reach Redis as key material; keys are binary-safe and the cancel key is written only after the marker's agent matches the caller's authorised agent.

## Verification

- Independent fresh-context verifier (read-only) was given only the claim "no arm of `terminate_execution` can cross agents on any of its three routes" and instructed to refute it: **refuted on the proxy arm via the operator route**, confirmed on the other eight arm×route cells with cited lines. The refutation drove the fix above.
- Full unit suite under CI conditions (branch on a clean `origin/dev` worktree, no submodules): 12966 passed, 0 failed before the gate; targeted terminate suites 70 passed after it; a second CI-condition full run was started with the gate included.
