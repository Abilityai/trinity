# Feature Flow: Subscription Auto-Switch (SUB-003)

> **Requirement**: `docs/requirements/SUB-003-subscription-auto-switch.md`
> **Issue**: #153
> **Status**: Implemented (2026-03-21)

## Overview

Automatically switches an agent to a different subscription when it encounters 2+ consecutive rate-limit (429) errors from the Claude API. Opt-in via system setting.

When Anthropic's 429 response body includes a reset time (e.g. `"resets 8pm (America/New_York)"`), that timestamp is parsed and persisted as `rate_limited_until` on the subscription row. Subsequent calls to `is_subscription_rate_limited()` check this authoritative timestamp first, blocking the subscription until it actually resets rather than relying on a 2-hour rolling event window (which is shorter than Anthropic's real 5–8 hour reset cycle).

## Flow

```
Agent container detects rate limit → returns 429 to backend
    ↓
Backend catches 429 in:
  - TaskExecutionService.execute_task() [schedules, MCP, agent-to-agent]
  - chat_with_agent() [interactive chat]
  - background task handler [async tasks]
    ↓
subscription_auto_switch.handle_rate_limit_error(agent_name)
    ↓
Check: setting enabled? → No → return None
    ↓ Yes
Check: agent has subscription? → No → return None
    ↓ Yes
Record rate-limit event, get consecutive count
Parse reset time from 429 body → persist rate_limited_until on subscription row (if parseable)
    ↓
Count < 2? → return None (wait for more)
    ↓ ≥ 2
Find best alternative subscription (fewest agents, not rate-limited)
    ↓
No alternative? → return None (log warning)
    ↓ Found
Switch: DB update + container restart + log activity + send notification
    ↓
Return switch result to caller → 429 response includes auto_switch info
```

## Files

| Layer | File | Purpose |
|-------|------|---------|
| DB | `src/backend/db/subscriptions.py` | Rate-limit event CRUD, best-alternative selection |
| DB | `src/backend/db/migrations.py` | `subscription_rate_limit_events` table |
| DB | `src/backend/database.py` | Delegation methods |
| Service | `src/backend/services/subscription_auto_switch.py` | Orchestration: detect, switch, log, notify |
| Router | `src/backend/routers/subscriptions.py` | Setting GET/PUT endpoints |
| Service | `src/backend/services/task_execution_service.py` | 429 interception for all execution paths (schedules, MCP, agent-to-agent) |
| Router | `src/backend/routers/chat.py` | 429 interception in chat proxy + background tasks |
| Frontend | `src/frontend/src/views/Settings.vue` | Toggle in Subscriptions section |
| Tests | `tests/test_subscription_auto_switch.py` | Smoke tests |
| Tests | `tests/unit/test_subscription_auto_switch_pingpong.py` | Unit regression for #444 ping-pong prevention |
| Spec | `docs/requirements/SUB-003-subscription-auto-switch.md` | Full requirements |

## Database

### subscription_credentials (relevant columns)

| Column | Type | Description |
|--------|------|-------------|
| rate_limited_until | TEXT | ISO 8601 UTC timestamp — authoritative reset time parsed from 429 body. NULL when not rate-limited or after expiry. Checked by `is_subscription_rate_limited()` before the event-count fallback. |

### subscription_rate_limit_events

| Column | Type | Description |
|--------|------|-------------|
| id | TEXT PK | UUID |
| agent_name | TEXT | Agent that hit the limit |
| subscription_id | TEXT FK | Subscription that was rate-limited |
| error_message | TEXT | Error details |
| occurred_at | TEXT | ISO timestamp |

### System Setting

| Key | Default | Description |
|-----|---------|-------------|
| `auto_switch_subscriptions` | `"false"` | Enable/disable auto-switch |

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/subscriptions/settings/auto-switch` | Get setting state |
| PUT | `/api/subscriptions/settings/auto-switch?enabled=true` | Toggle setting |

## Selection Strategy

1. Exclude current subscription
2. Order by agent_count ascending (load-balance)
3. Skip any subscription where `is_subscription_rate_limited()` returns True:
   - **Primary check**: `rate_limited_until` is set and still in the future (authoritative)
   - **Fallback**: 2+ rate-limit events in the last 2 hours (heuristic for cases where reset time wasn't parseable)
4. Return first viable candidate, or None

## Edge Cases

- **All subscriptions exhausted**: No switch, error surfaces as normal 429. `_perform_auto_switch` does **not** clear rate-limit events for the old subscription — those events are the signal that keeps `is_subscription_rate_limited()` truthful, so the just-drained sub is not offered as a candidate on the next cycle (issue #444).
- **Slow ping-pong** (#476): The 2h event window was shorter than Anthropic's 5–8h reset window, causing repeated failed switches every 2 hours. Fixed by persisting `rate_limited_until` from the 429 body — the subscription is blocked until Anthropic's actual reset time.
- **API key agents**: Auto-switch only applies to subscription-based agents
- **Flip-flopping**: 2-consecutive-error requirement prevents immediate re-switch
- **Concurrent switches**: SQLite serialization prevents races
- **Cleanup**: `rate_limited_until` is cleared in-place by `is_subscription_rate_limited()` once it expires. Rate-limit event records older than 24h are eligible for cleanup; the event-count heuristic operates independently as a fallback when reset time was not parseable.
