# CSO diff audit — #3932 pull claims open the dispatch activity

- Mode: `--diff`, daily (8/10 gate). Base `origin/dev`. 6 files changed.
- Result: **0 findings** (0 SUPPORTED, 1 hypothesis).

## Change under audit

`GET /api/internal/next-task` now awaits `pull_coordination_service.open_claim_activity(execution_id)` after a successful claim. It opens the CHAT_START activity through `task_execution_service.start_dispatch_activity`, the same opener push dispatch uses. The lease reaper reads the dead attempt's activity id before re-queueing and closes it by id.

## Checks

- **Auth:** no new route. `/next-task` keeps `_pull_authorized` (internal secret, or the pilot agent's own agent-scoped key bound to `agent_name` via `authorize_heartbeat`). The opened activity belongs to the execution the claim returned, so a key cannot open an activity on a sibling agent's run.
- **Input:** `execution_id` comes from the claimed row, never from the request. Activity fields are read from the row (`message`, `triggered_by`, `source_agent_name`, `source_user_id`) and the parent from `backlog_metadata.collaboration_activity_id`, which the backend wrote at enqueue. `backlog_metadata` parse errors fall back to `{}`.
- **SQL:** only existing parameterized helpers (`get_execution`, `get_open_activity_id_for_execution`, `create_activity`).
- **Failure direction:** the open is fail-open, stated in the docstring (a claim must never fail on the timeline); failures log the exception type only. The reaper's id read is fail-open and falls back to the previous lookup close.
- **Credentials:** none touched; no credential prefixes or invisible Unicode in added lines.
- **Agent context:** the activity's `message_preview` (first 100 chars) is not fed into any agent prompt.

## Hypothesis (not counted)

Pull claim bars broadcast `agent_activity` with `message_preview` on `/ws`, the same event push dispatch sends for every run. The diff restores parity and adds no field or channel.

## Coverage gaps

- Docker pass not run: no Dockerfile, dependency or compose change.
- No independent verifier: no findings to verify.
