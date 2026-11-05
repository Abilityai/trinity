# Debt inbox

Deferred items noted during implementation, for the next grooming pass. One
line per item: what, why it was deferred, and where to pick it up.

## #4257 — queue stall

- **debt: queue-stall #4257 — the `get_execution_result` read surface does not
  carry the receipt fields.** `GET /api/executions/{id}` (`routers/executions.py`,
  `models.py`) and the MCP `src/mcp-server/src/tools/executions.ts` mapping were
  plan Step 7 and are not done. The three fields (`queued_at`, `queue_position`,
  `wait_reason`) reach MCP callers on the **dispatch receipt** only, because
  `chat.ts asyncReceipt` spreads `...response`. The MCP tool description for
  `get_execution_result` was therefore deliberately NOT updated in the fix step:
  documenting fields the endpoint does not return would be the wrong half of
  the pair. Do both together, or neither.
- **debt: queue-stall #4257 — the periodic resend tick is never executed under
  test.** `tests/unit/test_4257_pull_worker_hardening.py` asserts two function
  names and a constant; nothing runs `_resend_pull_periodically` even one
  iteration, so swapping `asyncio.sleep` for a blocking call, or dropping the
  `CancelledError` re-raise, stays green. Fix: `_RESEND_TICK_SECONDS=0`,
  `resend_pending_pull_results` as an `AsyncMock`, run under `asyncio.wait_for`
  for ~2 ticks, cancel, assert ≥1 call and that `CancelledError` propagates.
- **debt: queue-stall #4257 — the settings PUT 400 and the generic-PUT 422 are
  substring checks, not executed branches.** `tests/unit/test_4257_queue_stall_setting.py`
  asserts the error text appears in the source of `routers/settings/agent_defaults.py`
  / `generic.py`. A `<` vs `<=` typo in the 5–1440 comparison ships green; the
  #506 ceiling precedent it mirrors has executing route tests. Fix: call both
  handlers directly with a stub admin `User` and `value ∈ {4, 5, 1440, 1441}`.
- **debt: queue-stall #4257 — `oldest_queued_head_per_agent` scans every queued
  row fleet-wide, per 60 s tick, per uvicorn worker.** `db/schedules/queue.py`
  selects all `status='queued'` rows with no LIMIT and folds them in Python for
  one head + count per agent. The platform already has a `queue_flood` alert,
  i.e. queues of thousands are a known state. Fix or accept-with-a-note:
  `GROUP BY agent_name` with `MIN(queued_at)` + `COUNT(*)`, then one indexed
  read per agent for the head's id / conversation key.
- **debt: queue-stall #4257 — the resend tick can pre-empt an in-loop #3487
  credential retry.** The tick's sweep sends the same envelope with
  `auth_override_capable: False`, and `pull_coordination_service` answers that
  flag with a terminal FAILED instead of a retry instruction, so a refused
  credential loses its walk if the tick wins the race during the 90 s in-loop
  window. No double-apply (the `claim_token` CAS holds); window is small (the
  sweep jitters 0–60 s first). Fix: skip files younger than
  `_DELIVERY_IN_LOOP_SECONDS`, or keep an in-process "delivering" set.
- **debt: queue-stall #4257 — the stall alert is filed at priority `high` for
  every reason, including the now-correct `capacity`.** With the slot meter read
  (fixed), a push agent at capacity is classified honestly, but it still files
  at `high`. Decide whether an expected-but-long wait deserves a lower priority
  or no row at all; a kind that fires on healthy agents is one operators learn
  to ignore.
