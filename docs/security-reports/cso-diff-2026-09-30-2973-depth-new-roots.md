# CSO Diff Audit — 2026-09-30 — #2973 (chain depth through loops, schedule triggers and events)

**Mode**: diff (all phases) · **Confidence gate**: 8/10 · **Branch**: `feature/2973-chain-depth-laundering` → `dev` · **Audited**: working-tree diff against `origin/dev`.

## Architecture (Phase 0)
Extends the #2806 chain-depth guard to three agent-initiated paths that used to start a depth-0 root:
- **Loop start**: depth stored on `agent_loops.chain_depth` (nullable, SQLite + Alembic `0084`) and stamped on every iteration.
- **Manual schedule trigger**: `chain_depth` forwarded in the backend→scheduler body. The scheduler is on the platform network only.
- **Agent event emit**: depth carried as a `SECRET_KEY`-signed `chain_depth` claim in the EVT-001 loopback JWT.

It also adds a Redis hourly dispatch budget per source→subscriber agent pair in `event_dispatch_service.trigger_subscription`. Refusals from the new routes use an app-level handler with the #2806 403 body and header.

## Attack surface (Phase 1, diff-scoped)
- **New endpoints: 0.** New MCP tools: 0.
- **Changed responses**: 403 `inter_agent_depth_exceeded` on `POST /api/agents/{name}/sessions/{id}/message`, `POST /api/agents/{name}/loops`, `.../schedules/{id}/trigger`, `POST /api/events`, `POST /api/agents/{name}/emit-event`.
- **New token claim**: `chain_depth` on the event-loopback JWT, read only when `scope == event_loopback`, and the token is fenced to `POST /api/agents/{name}/task`.
- **New scheduler body field**: `chain_depth` (int, 1–1000; any other value is dropped).
- **New ops setting**: `event_dispatch_max_fires_per_hour` (1–10000, default 120). Written only through the existing admin-gated ops-config route.
- **Redis keys**: `trinity:evt_fires:{source}:{subscriber}`, plus an `:alerted` flag with the same 1h TTL.
- **Unchanged**: Docker, CI, dependencies.

## Findings (Phases 2–12)

**None open at or above the 8/10 gate.**

Two findings were raised during the audit and fixed in this diff:

| # | Sev | Conf | Status | Finding | Resolution |
|---|---|---|---|---|---|
| 1 | Low | 8 | FIXED | Budget keyed on subscriber only. A source agent the subscriber listens to could spend the subscriber's hourly budget and starve every other source into it for the window. | Key changed to the (source, subscriber) pair. Self-subscriptions share one pair key, so they still cannot multiply the cap. Test: `test_2973_one_noisy_source_does_not_starve_the_others`. |
| 2 | Info | 6 | FIXED | An unvouched loopback refusal was recorded with the subscriber as actor, plus a self-edge collaboration activity. | Caller stays `None`; the audit row is written and the collaboration activity skipped. Test: `test_2973_unvouched_loopback_refusal_names_no_caller`. |

## Checks that came back clean
- **Depth cannot be lowered**
  - The claim comes only from a verified, loopback-scoped JWT (`dependencies.py` `loopback_chain_depth=_loopback_chain_depth(payload) if loopback else None`), and `depth = max(depth, claimed)` means it can only raise the value.
  - The token is posted by the backend to itself and never reaches an agent.
  - No request body sets depth on the loop or emit paths.
  - The webhook forwards a fixed body.
  - Agents cannot reach the scheduler (`docker-compose.yml` platform-only network).
  - Forged signature → 401 (`test_2973_a_forged_claim_is_rejected_by_the_signature`).
- **Invariant #8**
  - The access dependencies (`get_authorized_agent`, `OwnedAgent`, `AuthorizedAgent`) and the schedule 404 resolve before the guard.
  - The subscriber named in an emit refusal is already listable by anyone with access to the source (`list_event_subscriptions`, `direction=source`).
- **Session turns**: the guard runs after `AuthorizedAgent` and the per-user `_session_or_404` (404 for a foreign session), and before file uploads and the user-message insert, so a refusal discloses nothing and leaves no row. `execute_task(chain_depth=)` only stamps a row it creates itself.
- **`_chain_caller` fallback**: one caller (`enforce_inter_agent_depth`). The other `vouched_source_agent` readers are unchanged. Non-str/non-int principal attributes are ignored.
- **Budget**
  - `INCR` and `EXPIRE NX` run in one `MULTI` transaction.
  - Agent names are `[a-zA-Z0-9_.-]`, so keys cannot collide.
  - The backend Redis ACL allows `@write @keyspace @transaction`.
  - Agents cannot force the fail-open path: they have no Redis reach.
- **Secrets**: new logs, notification metadata and audit details carry agent names and counts only. Diff scanned for known key prefixes: 0 real hits.
- **SQL / migrations**: the scheduler `INSERT` is parameterized (16/16). The column is nullable with no backfill on both tracks, and there is a single Alembic head.

## Accepted and stated (not findings)
- **Residual new roots**: webhook tokens, agent-created cron schedules and self-reminders. Tracked in **#3116**.
- **Model-cost exposure**: up to 120 dispatches per source→subscriber pair per hour. Chain depth still bounds how deep any one chain runs.
- **Sync Redis in async**: the budget uses the same sync client as the auth rate limiter, one round trip per dispatch.
