# CSO diff audit — abilityai/trinity#2694 (`feature/2694-voice-call-thread-order`)

**Date**: 2026-09-10 · **Mode**: `--diff` (daily gate 8/10) · **Base**: merge-base `3edd0dc6` on `dev` · **Diff**: 25 public files (+~1600 / −115) incl. 2 new test files (working tree, pre-commit). Surface: one endpoint gains a bounded query parameter, two new scoped DB reads, one new Redis marker, one prompt surface widened (not added).

## Verdict
**No findings at the gate.** Every candidate was traced end-to-end and refuted. One appendix item (a self-inflicted stranded marker on a REST `/stop` that lands before the socket closes) was fixed in the same branch before commit rather than deferred.

## Attack surface introduced by the diff
- **Endpoints**: none added. `GET /api/enterprise/client-portal/agents/{name}/history` gains `?limit` (`Query(None, ge=1, le=50)`) — a *narrower* read for the reply poll; the window path takes only module constants, so nothing on the route can widen it.
- **DB reads**: `db.get_portal_thread_window` (two `text()` statements) and `db.get_platform_rows_since_last_reply` (one statement with a correlated `MAX`). Both bind `agent_name`/`client_email`/`session_id` and are reached only after the roster gate and the thread-ownership uniform 404.
- **Redis**: `portal_voice_active:{portal_session_id}` — SET once in `start_workspace_voice` after the ownership 404 and after the provider session exists; DEL in the WS bridge's `finally` (owner/admin-gated socket) and in the owner/admin-gated REST `/stop`. The id is server-side, never a caller value.
- **Prompt surface**: a resumed turn's user message is prefixed with the spoken rows since the agent's last typed reply. Spoken rows are producible only by platform principals (the start route is `is_platform`-gated twice); the same rows already reached the cold replay since ent#534, so this widens a budget rather than adding a producer.
- **409s**: two, both after the uniform 404 — no existence oracle.

## Findings
| # | Sev | Conf | Status | Category | Finding | Phase | File:Line |
|---|-----|------|--------|----------|---------|-------|-----------|
| — | — | — | — | — | none at the gate | — | — |

## Verification performed
- **Secrets / enterprise disclosure (P2)**: known-prefix, internal-host and email scan over every added line — none (only `alice@example.com` fixtures). The enterprise-docs guard pattern matches no added line; the only `enterprise_*` tokens are the two portal tables already public in these files.
- **Access control (P6/P9-A01)**: both new reads sit behind `agent_on_roster` → `get_portal_session(session_id, agent, email)` (404) in `get_history`, and behind the roster gate → `_resolve_session_id` (404) in `portal_chat` / `start_portal_turn`; the 409 gates come after those 404s in all three entries (pinned by `test_the_inflight_gate_runs_after_the_uniform_404_not_before`). `limit` selects the narrow row read and is bounded 1–50 (AST-pinned). Marker writers: one ownership-gated SET, two owner/admin-gated DELs; thread ids are `uuid4().hex` primary keys.
- **Injection (P9-A03)**: every new `text()` interpolates only module constants (`_MESSAGE_COLUMNS`, `_TYPED`, a fixed `base` clause, a fixed tie-break fragment); all caller/row values are bound (`:ts`, `:tid`, `:off`, `:lim`); `typed_limit`/`ceiling` are `int()`-coerced.
- **LLM (P7)**: spoken `user` rows replay in the user-message position (FP rule); `You (voice):` rows are the platform's own voice-model output, the class ent#534 already replayed. `_one_line` collapses whitespace so no row can open a forged `You:` / `[Client Portal]` / header line (pinned). Bracketed markers come only from `role='system'` rows the platform writes with fixed strings. Content is secret-scrubbed at write; the consumer is the same agent.
- **A04/A05**: `truncated` is a bool about the caller's own thread; new WARNING lines carry ids and the exception, never content; the poll now reads 8 rows instead of the full window at the same cadence.
- **Standing guards on this tree**: `test_186_enumeration_uniformity`, `test_agent_auth_header_guard`, `test_2094_dependency_path_param_pairing` — 38 passed.
- **Supply chain (P3), CI/CD (P4), infrastructure (P5), skills (P8)**: no dependency, lockfile, workflow, Dockerfile, compose, network or skill file in the diff.

## Appendix (≤4/10, non-blocking)
1. **Stranded marker on an early REST `/stop`** (availability, self-inflicted, conf 4) — `voice_stop` ended the session without clearing the marker, so the bridge's later `end_session` returned None and skipped the DEL; the owner's own thread would refuse typed turns until the TTL. Owner/admin-gated, and the Workspace client never sends the REST stop (`restStop: false`). **Fixed on this branch**: `voice_stop` now clears the marker for a portal-bound session.
2. Thread ids / emails in WARNING logs — follows the file's existing pattern; not a new class.
3. The delta is re-prefixed on every resumed turn until a typed reply lands — bounded (≤24k chars), owner-only, platform-principal-only producer.
4. Mid-line `You:` text inside a spoken row stays under the `Client (voice):` label — same property as typed rows today; no exploit path beyond the pre-existing one.

*AI-assisted scan, not a professional audit.*
