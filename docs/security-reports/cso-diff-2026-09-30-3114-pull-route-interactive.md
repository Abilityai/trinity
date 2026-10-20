# /cso --diff — #3114 interactive producers on the pull queue (2026-09-30)

**Scope:** `feature/3114-pull-route-interactive` vs `dev`. Daily gate: 8/10.
**Result:** 0 findings (0 critical, 0 high, 0 medium, 0 low).

## Attack surface added
- **No new endpoint or auth path.** On pull-pilot agents the existing interactive entry points (Session tab, Workspace portal, public link, Slack/Telegram/WhatsApp, rooms, paid, MCP inline auth, validation, run-now) enqueue a row and wait for its terminal instead of pushing. Each entry point keeps its own auth, which runs before dispatch.
- **Claim envelope** now carries `persist_session`, `images`, and the push-equivalent prompt context (`source_user_email`, `source_agent_name`, `source_mcp_key_name`, schedule context, attempt). It is served only to the agent's own scoped key or the internal secret (`_pull_authorized`), so an agent reads only its own queue.
- **Stream proxies** (`routers/chat.py`, `routers/public.py`, `client_portal/router.py`) hold the SSE connection while the row is queued. Each validates its caller and binds the execution to the agent before the hold starts.

## Phases
- **Secrets:** no key patterns, emails or private IPs in the diff.
- **Enterprise docs guard:** 0 hits across the changed docs.
- **Dependencies, CI, Docker:** unchanged. `docker/base-image/agent_server/services/pull_worker.py` changes; vendored policy mirrors (Invariant #5) untouched.
- **Injection:** no raw SQL added. `conversation_key` is built server-side at every interactive call site (`session:`, `public:`, `channel:`, `room:`, `paid:`); no request model exposes it.
- **Plaintext persistence (G-04 class):** images ride `backlog_metadata` as base64. G-04 skips only each image's `data` field; every other field is scanned. The #1449 sweep clears the column once the row is success/cancelled/skipped; failed rows keep it until the 90-day prune, as for the message text.
- **Paid:** a caller that disconnects while its turn is queued cancels the row, and a turn not claimed within one agent timeout is cancelled before any settlement.

## Appendix (below the gate)
- **Shared backlog cap (DoS, excluded by rule).** On a pilot, anonymous public-link turns now count toward the agent's `max_backlog_depth` (default 50), which scheduled work also uses. A flood that fills it makes the next scheduled fire fail with `persistent_full` until the queue drains; strict interactive precedence also delays autonomous rows. Before this change public turns on a pilot were pushed against the Redis counter. Follow-up: a separate cap for public-link rows.
- **Stream hold polling.** Each held stream reads the row once per second for up to one agent timeout.
