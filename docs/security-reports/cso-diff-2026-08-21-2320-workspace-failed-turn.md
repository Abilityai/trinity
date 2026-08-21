# CSO Diff Audit — trinity#2320 Workspace failed-turn visibility

- **Date**: 2026-08-21 · **Mode**: `--diff` (daily, ≥8/10 confidence gate) · **Branch**: `feature/2320-workspace-failed-turn-visibility` (1 commit vs merge-base `76615f26`)
- **Scope**: 8 files — backend `client_portal/service.py` + `models.py`; frontend `PortalConversation.vue`, `stores/clientPortal.js`; 2 test files; 2 docs
- **Phases**: 0–14. Phases 3 (deps), 4 (CI/CD), 5 (infra/Docker), 8 (skills) are **N/A** — the diff touches no requirements/lockfile, no `.github/workflows/`, no Dockerfile/compose, no `.claude/skills/`

## Findings

**None at the ≥8 confidence gate.**

No new endpoint, no auth-dependency change, no new SQL, no `subprocess`, no new file-path handling, no new dependency, no schema change.

## Clean categories (what was checked)

| Axis | Verdict | Basis |
|---|---|---|
| Attack surface delta | CLEAN | zero new routes. One optional response field (`PortalHistory.last_turn_outcome`) on the existing roster-scoped, session-ownership-gated `GET .../history`; two keyword-only params on `ClientPortalError` with defaults; three module-private Redis helpers. No caller-reachable new input path |
| Cross-client leak via the new field | CLEAN | `get_history` runs `agent_on_roster` → `db.get_portal_session(session_id, agent_name, email)` (uniform 404) **before** the outcome read (`service.py:2260`). A caller-supplied foreign `session_id` 404s and never reaches Redis. Same gate the #2214 marker read sits behind |
| Key-material injection | CLEAN | `_outcome_key` interpolates only a server-minted session id — DB-validated on the read path, closure-resolved on the write path. Never caller-shaped. Mirrors the `portal_inflight:` precedent |
| Raw error disclosure (issue AC 2) | CLEAN | the only two writers are `e.detail` (authored client copy at every raise site) and the fixed `INTERNAL_FAILURE_DETAIL`. The generic `except Exception` branch sends `f"{type(exc).__name__}: {exc}"` to the log and `schedule_executions.error` only — pinned by `test_2320_…` with a distinctive sentinel string asserted absent from the outcome |
| Prompt injection via the new record | CLEAN | the outcome is Redis-only and never persisted as a message. `_format_history_context` (`service.py:1120`) iterates portal **messages**, so no outcome text can re-enter an agent's context. This is why the error-message-row design was rejected: `who = "Client" if role == "user" else "You"` would have replayed a platform error to the agent as its own words |
| Frontend XSS | CLEAN | `m.error` is rendered `>{{ m.error }}</p>` (`:92`) — Vue interpolation, auto-escaped. The `v-html` at `:105` is the pre-existing assistant-content path through DOMPurify `render()`, untouched |
| Fail-open direction | CLEAN | all three helpers swallow and degrade: no client ⇒ return/None, unparseable JSON ⇒ None, non-dict ⇒ None. Every failure lands on the **pre-#2320** lost-track behaviour — never on a Retry being offered. `retryable` defaults `False` on the exception; the client's `outcome.retryable === true` is an explicit-true test, so a truthy-but-not-true value reads as not retryable |
| Retry / double-bill boundary | CLEAN | only `busy` (`ResumeLockBusy`, raised before `run_resumable_turn` reaches the agent) and `capacity` (admission refused) are retryable. The three post-run terminals (`timeout`, `agent_error`, `auth`) and both `agent_unavailable` sites are not. The client believes a verdict only when `outcome.execution_id === executionId`, so a stale record cannot re-enable Retry on a different turn |
| Cost / abuse amplification | CLEAN | Retry re-sends exactly one turn on the same rate-limited path as an ordinary send; concurrency still bounded by the per-thread resume lock, the router's per-(email, agent) limits, and `max_parallel_tasks`. The two retryable categories both mean **nothing ran**, so a retry loop costs no model tokens until the contended resource frees |
| Redis lifecycle | CLEAN | one `SET`/`DEL` per turn terminal, one `GET` per history fetch. Session-keyed with a 900s TTL, so it is self-reaping — no purge/rename cascade obligation, and not `agent:*`-prefixed, so the #1560 parity registry does not apply (same as `portal_inflight:`) |
| Secrets in diff | CLEAN | `git log -p <merge-base>..HEAD` over the branch: no key-shaped strings; the only literals are a TTL, category tokens, and copy |
| Enterprise-docs-guard (ent#45) | CLEAN | the two touched docs describe the OSS-core Workspace (ent#356) turn path only; no paid-module tokens, no `enterprise_*` DDL |
| Agent-key self-boundaries / backend→agent auth / vendored parity / channels / CI / Docker / deps | N/A | untouched by this diff |

## Below the gate (recorded, not findings)

| Item | Confidence | Disposition |
|---|---|---|
| `record_turn_outcome`'s `message` is not length-capped, while its sibling `_fail_unstarted_execution` caps at `[:500]` (`service.py:1902`). Unexploitable today — every producer is a fixed string or short formatted copy — so this is a hardening asymmetry, not a vulnerability. A future raise site with a long or foreign-derived `detail` would put unbounded text into Redis and onto a client. | 6/10 | **APPLIED in this branch** — same `[:500]` bound as the row writer, for symmetry. No behaviour change today; it exists so a future raise site cannot widen it silently. |
| The new `auth` category tells an external portal client that the agent *"has reached its usage limit"*. The issue explicitly rules remediation guidance operator-only, and this copy carries none — but it does newly reveal the operator's billing posture to an untrusted principal, where the previous behaviour was a generic *"couldn't respond"*. | n/a (product) | **Owner's call.** A neutral alternative — *"The agent is temporarily unavailable. Please try again later."* — conveys the same operational fact with no billing signal, at the cost of the honesty the issue asks for. Flagged, not changed. |
| The #2214 report's deferred item (resume-lock TTL not resized with the turn bound; a lapse lets two senders `--resume` one JSONL concurrently) is **untouched** by this diff. | — | Carried forward; still engine-owned (`session_turn_service`), still a priority follow-up. |

## Verification

No finding survived the confidence gate, so the independent adversarial pass was vacuous by construction — the load-bearing *clean* verdicts above (session-ownership gate ordering, prompt-injection reachability, XSS escaping, fail-open direction) were each traced to a quoted line rather than assumed.

## Trend

Third consecutive `--diff` audit on the Workspace turn path (#2214 → #2258 → #2320), third with zero gate-passing findings. All three widened what the client is told about a turn while leaving the auth gates untouched; each one's new field rides the same already-gated `GET .../history` read. The pattern is healthy but worth naming: this read is now carrying four pieces of turn state, and every future addition inherits `get_history`'s session-ownership check as its *only* boundary.

## Verdict

A visibility change on an already-gated read. Every fallback lands on the pre-existing message, and the one bit that could cost money — `retryable` — defaults to the unprivileged answer at both ends and is set true only where the server states nothing reached the agent. Ship-clean; the `[:500]` cap is applied.
