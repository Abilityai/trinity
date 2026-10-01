# /cso --diff — #3127 /chat on the durable queue for pull pilots (2026-10-01)

**Scope:** `feature/3127-pull-ui-chat` vs `feature/3114-pull-route-interactive`. Daily gate: 8/10.
**Result:** 0 findings (0 critical, 0 high, 0 medium, 0 low).

## Attack surface added
- **No new endpoint or auth path.** `POST /api/agents/{name}/chat` keeps its admission order (auth, chain-depth guard, idempotency begin, breaker read) and on a pull pilot enqueues the turn instead of acquiring a slot. `GET`/`DELETE /chat/history` keep their dependencies (`get_authorized_agent` / `get_owned_agent`).
- **New column `chat_sessions.cached_claude_session_id`** (SQLite migration + Alembic `0086`), written only by the pulled `/chat` path after a UUID check, read by the session reaper's keep set.

## Phases
- **Secrets:** no key patterns in the diff. The pulled path scrubs staged secrets from the response before the `chat_messages` write (ent#279 parity).
- **Isolation:** on a pilot `/chat` memory is per `chat_sessions` row, i.e. per (agent, user). One user's turns cannot resume another user's Claude session; `GET /chat/history` returns the caller's own session only. Off a pilot the agent-wide shared session is unchanged.
- **Injection:** no raw SQL; the conversation key is built server-side (`session:chat:<chat_sessions.id>`).
- **Dual-track schema:** both tracks present; one Alembic head.

## Appendix (below the gate)
- `DELETE /chat/history` (owner-gated) closes every user's `/chat` session on the agent. That matches the agent-wide reset it performs off a pilot.
