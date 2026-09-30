# /cso --diff — #2842 claim priority + #2843 one turn per conversation (2026-09-30)

**Scope:** `feature/2842-interactive-priority` vs `dev`. Daily gate: 8/10.
**Result:** 0 findings (0 critical, 0 high, 0 medium, 0 low).

## Attack surface added
- **No new endpoint, route or auth path.** `GET /api/internal/next-task` keeps its dual auth (`_pull_authorized`); only the order of the rows it can claim changes.
- **New column `schedule_executions.conversation_key`** plus the partial unique index `idx_executions_one_running_turn`. Written only by `backlog_service.enqueue` on pull-pilot agents.
- **Canary snapshot** reads `conversation_key` for running and queued rows. It is not added to any `observed_state`, so it never reaches Slack or `canary_violations`.

## Phases
- **Secrets:** no key patterns, emails or private IPs in the diff.
- **Enterprise docs guard:** 0 hits across the changed docs.
- **Dependencies, CI and Docker:** unchanged.
- **Injection:** all new SQL is SQLAlchemy Core with bound parameters; the trigger set is bound through `in_()`. Both migrations are static strings. No interpolation added.
- **Access control:** the claim is per-agent (`agent_name` bound from the authorized request), and the conversation guard is scoped by `agent_name`, so rows of one agent can never block or reorder another agent's queue.
- **Dual-track schema:** the column and index are in `db/schema.py`, `db/tables.py`, the SQLite migration and Alembic `0083`; verified on a fresh PostgreSQL via `alembic upgrade head`.

## Mutations
- Unique index dropped before the concurrent Postgres claim test: red.
- Interactive ordering disabled: red. Conversation skip disabled: red.

## Appendix (below the gate)
- **Caller-supplied key (DoS, excluded by rule).** `conversation_key` is taken from `chat_session_id` / `resume_session_id` in the request body. A caller with access to the same agent who knows another conversation's id can make that conversation's queued turn wait behind theirs. Delay only; resuming another session's id predates this diff. Derive the key server-side when interactive producers are routed onto the queue.
- **Priority flood (DoS, excluded by rule, accepted by decision).** Interactive rows have strict precedence with no anti-starvation rule, so sustained interactive load delays autonomous work until the operator raises the worker count.
