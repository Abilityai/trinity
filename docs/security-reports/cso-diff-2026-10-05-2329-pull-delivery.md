# /cso --diff — #2329 pull sink post-turn delivery (2026-10-05)

Mode: diff, daily (8/10 gate). Scope: 7 backend files, 1 unit test, 1 learnings fragment, 1 report. No dependency, CI, Docker, frontend, template or skill changes.

## Stack CVE quicklist (diff-relevant rows)
No pin touched by the diff. All rows: not reachable from this change.

## Attack surface delta
- New endpoints: 0. New MCP tools: 0. New WS channels: 0.
- Changed behaviour on an existing route: `POST /api/internal/tasks/{execution_id}/result` (pull sink) now runs post-turn delivery.
- Auth on that route is unchanged: the caller's agent-scoped key must match `execution.agent_name`; unknown or foreign execution is a uniform 404 (`routers/internal.py` `internal_task_result`).

## Checks
| Area | Check | Result |
|---|---|---|
| A01 IDOR | Delivery identity (`user_id`, `user_email`, `chat_session_id`) comes from `backlog_metadata`, written by `backlog_service.enqueue` from the authenticated request. The worker's POST cannot set it. A foreign `chat_session_id` falls back to the caller's own session (`persist_chat_session` #1444 guard), covered on the pull path by `test_foreign_session_id_falls_back_to_callers_own`. | clean |
| A01 activity ids | `collaboration_activity_id` / `self_task_activity_id` are server-derived (not `ParallelTaskRequest` fields); closed by exact id. | clean |
| ASI03 / ASI10 | An agent can deliver only its own execution's result (route gate above). The tightened SUCCESS CAS stops a retried POST re-running delivery. | clean |
| ASI07 | `chat_response_ready` broadcast carries identifiers only (existing helper, unchanged). | clean |
| A04 | No new credential column or plaintext persistence. | clean |
| A09 / ent#292 | New log lines carry execution id and exception type, never content. `_close_queued_activities` failure logs `str(e)`, same as the existing `close_execution_activity` warning beside it. | clean |
| A10 failure direction | Sink delivery: fail-open, stated (terminal already committed; waiter signalled in `finally`). Queued-activity close: fail-open, stated, isolated from the dispatch close. SUCCESS CAS: fail-closed against duplicates. | clean |
| Secrets (P2, diff) | No key patterns in added lines. | clean |

## Findings
None.

## Hypotheses
None (daily mode).

## Coverage gaps
- Phase 3 / 4 / 5 / 8: no files in scope for the diff (no dependency, workflow, Docker or agent-shipped changes).
- Docker pass not run (diff mode; no image change).
