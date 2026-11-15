# Feature: Subscription Credential Health Monitoring

> **SUPERSEDED by SUB-002** (2026-03-03): This feature flow is no longer applicable.
> SUB-002 replaced `.credentials.json` file injection with `CLAUDE_CODE_OAUTH_TOKEN` env var
> injection. Credential file monitoring, auto-remediation, and the related alert have been removed.
> See `docs/requirements/SUBSCRIPTION_LONG_LIVED_TOKENS.md` for the new design.

## Overview (Historical — SUB-001)

Extends the monitoring service (MON-001) and subscription system (SUB-001) to detect when subscription credentials are missing from agent containers, auto-remediate by re-injecting them, fire alerts when remediation fails, and improve error surfacing in both the subscription assignment endpoint and the scheduler.

## User Story

As a Trinity platform admin, I want the monitoring system to detect when subscription credentials have disappeared from agent containers so that they can be automatically restored, and I am alerted when automatic recovery fails.

---

## Key Concepts

### Problem Statement

Subscription credentials (`.claude/.credentials.json`) can go missing from agent containers due to:
- Container recreation without re-injection
- Filesystem issues inside ephemeral Docker layers
- Failed injection during agent start
- Manual deletion inside the container

Previously, credential loss was silent -- agents would fail tasks with cryptic authentication errors and no monitoring coverage existed for this case.

**Related fix (Issue #57)**: A separate but related issue was that Claude Code prioritizes `ANTHROPIC_API_KEY` env var over OAuth credentials. Even when `.credentials.json` was present, if the container also had `ANTHROPIC_API_KEY` set, Claude Code would use the (potentially invalid) API key and never fall back to OAuth. This root cause is now prevented at the container level -- `check_api_key_env_matches()` and `recreate_container_with_updated_config()` ensure the API key is absent when a subscription is assigned. See [subscription-management.md](subscription-management.md) for details.

### Solution Architecture

```
MonitoringService._run_check_cycle()
     |
     v
check_business_health(agent)
     |
     v
GET /api/credentials/status  <-- Agent container
     |
     v
credential_status = "ok" | "missing" | null
     |
     v  [if missing]
perform_health_check()
     |
     +---> inject_subscription_on_start(agent)   <-- Auto-remediation
     |        |
     |        +--> SUCCESS: credential_status = "ok", status = "healthy"
     |        |
     |        +--> FAILURE: credential_status stays "missing"
     |                |
     |                v
     |     alert_subscription_credentials_missing()
     |        |
     |        +--> Creates NOTIF-001 notification (priority: "high")
     |        +--> WebSocket broadcast: {type: "monitoring_alert", ...}
     |        +--> Sets cooldown: "subscription:credentials_missing" (10 min)
     v
aggregate_health()
     |
     +--> credential_status == "missing" --> DEGRADED
```

---

## Entry Points

This feature has no direct UI entry point. It is triggered automatically by the background monitoring service loop.

| Trigger | Location | Frequency |
|---------|----------|-----------|
| Background monitoring loop | `MonitoringService._run_check_cycle()` | Every 30 seconds (configurable) |
| Manual fleet check | `POST /api/monitoring/check-all` | On-demand (admin) |
| Manual agent check | `POST /api/monitoring/agents/{name}/check` | On-demand (admin) |

---

## Data Flow

### Flow 1: Credential Health Check (Business Layer)

**File**: `src/backend/services/monitoring_service.py:258-306`

During the business health check, the monitoring service probes the agent container for subscription credential presence.

```
monitoring_service.py                    Database                      Agent Container
      |                                      |                              |
      | db.get_agent_subscription_id(name)   |                              |
      |------------------------------------->|                              |
      |<-------------------------------------|                              |
      | subscription_id (or None)            |                              |
      |                                      |                              |
      | [if subscription_id exists]          |                              |
      |                                      |                              |
      | GET http://agent-{name}:8000/        |                              |
      |   api/credentials/status             |                              |
      |--------------------------------------------------------------------->|
      |<---------------------------------------------------------------------|
      | response.files[".claude/.credentials.json"].exists                   |
      |                                      |                              |
      | credential_status = "ok" or "missing"|                              |
      |                                      |                              |
      | [if missing] status = "degraded"     |                              |
      | issues.append("Subscription          |                              |
      |   credentials missing")              |                              |
      |                                      |                              |
      | return BusinessHealthCheck(          |                              |
      |   credential_status=...,             |                              |
      |   status=...)                        |                              |
```

**Key code** (`monitoring_service.py:258-306`):

```python
# Check subscription credential presence (SUB-001/MON-001)
credential_status = None
subscription_id = db.get_agent_subscription_id(agent_name)
if subscription_id:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            cred_response = await client.get(
                f"http://agent-{agent_name}:8000/api/credentials/status"
            )
            if cred_response.status_code == 200:
                cred_data = cred_response.json()
                files = cred_data.get("files", {})
                has_cred_file = files.get(
                    ".claude/.credentials.json", {}
                ).get("exists", False)
                credential_status = "ok" if has_cred_file else "missing"
            else:
                credential_status = "missing"
    except Exception:
        pass  # Agent unreachable -- can't verify credentials

# Credential missing degrades health
if credential_status == "missing":
    if status == "healthy":
        status = "degraded"
    issues.append("Subscription credentials missing")
```

**Important note**: The agent-side `/api/credentials/status` endpoint (`docker/base-image/agent_server/routers/credentials.py:135-176`) only checks a hardcoded list of files: `.env`, `.mcp.json`, `.mcp.json.template`, `.credentials.enc`. It does NOT include `.claude/.credentials.json` in its file list. This means `files.get(".claude/.credentials.json", {})` always returns `{}`, causing the check to always report "missing" for subscription-enabled agents. The auto-remediation mechanism compensates by re-injecting credentials each cycle.

---

### Flow 2: Auto-Remediation

**File**: `src/backend/services/monitoring_service.py:423-439`

When `check_business_health()` reports `credential_status == "missing"`, the composite health check attempts to re-inject credentials before aggregating results.

```
perform_health_check()                subscription_service.py              Agent Container
      |                                      |                              |
      | business_check.credential_status     |                              |
      | == "missing"                         |                              |
      |                                      |                              |
      | inject_subscription_on_start(name)   |                              |
      |------------------------------------->|                              |
      |                                      | db.get_agent_subscription_id |
      |                                      | db.get_subscription()        |
      |                                      |                              |
      |                                      | inject_subscription_to_agent |
      |                                      |------------------------------>|
      |                                      | POST /api/credentials/inject |
      |                                      | {".claude/.credentials.json":|
      |                                      |   <decrypted JSON>}          |
      |                                      |<------------------------------|
      |<-------------------------------------|                              |
      |                                      |                              |
      | [if success]                         |                              |
      |   credential_status = "ok"           |                              |
      |   business_check.status = "healthy"  |                              |
      |                                      |                              |
      | [if failure]                         |                              |
      |   credential_status stays "missing"  |                              |
      |   --> triggers alert (Flow 3)        |                              |
```

**Key code** (`monitoring_service.py:423-439`):

```python
# Auto-remediate missing subscription credentials (SUB-001/MON-001)
if business_check.credential_status == "missing":
    try:
        from services.subscription_service import inject_subscription_on_start
        reinject_result = await inject_subscription_on_start(agent_name)
        if reinject_result.get("status") == "success":
            business_check.credential_status = "ok"
            business_check.status = "healthy"
            print(f"Auto-remediated missing credentials for agent {agent_name}")
        else:
            print(
                f"Auto-remediation failed for agent {agent_name}: "
                f"{reinject_result.get('error', reinject_result.get('reason', 'unknown'))}"
            )
    except Exception as e:
        print(f"Auto-remediation error for agent {agent_name}: {e}")
```

**Injection retry behavior** (`src/backend/services/subscription_service.py:24-111`):

The `inject_subscription_to_agent()` function retries up to **5 times** (increased from 3) with a 2-second delay between attempts:

```python
async def inject_subscription_to_agent(
    agent_name: str,
    subscription_id: str,
    max_retries: int = 5,    # <-- Increased from 3
    retry_delay: float = 2.0
) -> dict:
```

---

### Flow 3: Alert on Failed Remediation

**File**: `src/backend/services/monitoring_service.py:558-563`
**File**: `src/backend/services/monitoring_alerts.py:379-413`

If auto-remediation fails and `credential_status` is still "missing" after the remediation attempt, an alert is fired.

```
perform_health_check()          monitoring_alerts.py           Database         WebSocket
      |                                |                          |                |
      | credential_status == "missing" |                          |                |
      | (after remediation failed)     |                          |                |
      |                                |                          |                |
      | db.get_agent_subscription()    |                          |                |
      |---------------------------------------------->|                          |
      |<----------------------------------------------|                          |
      | subscription.name                             |                          |
      |                                |                          |                |
      | alert_subscription_credentials |                          |                |
      |   _missing(name, sub_name)     |                          |                |
      |------------------------------->|                          |                |
      |                                | is_in_alert_cooldown(    |                |
      |                                |   name,                  |                |
      |                                |   "subscription:         |                |
      |                                |    credentials_missing", |                |
      |                                |   600)  <-- 10 min      |                |
      |                                |------------------------->|                |
      |                                |<-------------------------|                |
      |                                |                          |                |
      |                                | [if NOT in cooldown]     |                |
      |                                |                          |                |
      |                                | db.create_notification() |                |
      |                                |------------------------->|                |
      |                                |                          |                |
      |                                | db.set_alert_cooldown()  |                |
      |                                |------------------------->|                |
      |                                |                          |                |
      |                                | _broadcast_alert()       |                |
      |                                |------------------------------------------>|
      |                                |                          |                |
```

**Alert notification created** (`monitoring_alerts.py:379-413`):

```python
async def alert_subscription_credentials_missing(
    self,
    agent_name: str,
    subscription_name: str
) -> Optional[str]:
    condition = "subscription:credentials_missing"

    if db.is_in_alert_cooldown(agent_name, condition, self.config.unhealthy_cooldown):
        return None

    notification = db.create_notification(
        agent_name=agent_name,
        data=NotificationCreate(
            notification_type="alert",
            title=f"Agent {agent_name} missing subscription credentials",
            message=(
                f"Subscription '{subscription_name}' is assigned but "
                f".credentials.json is missing from the agent container. "
                f"Auto-remediation failed."
            ),
            priority="high",
            category="health",
            metadata={
                "agent_name": agent_name,
                "subscription_name": subscription_name,
                "condition": condition,
                "timestamp": utc_now_iso()
            }
        )
    )

    db.set_alert_cooldown(agent_name, condition)
    await self._broadcast_alert(notification)
    return notification.id
```

**WebSocket event broadcast**:

```json
{
  "type": "monitoring_alert",
  "notification_id": "<uuid>",
  "agent_name": "<agent-name>",
  "alert_type": "alert",
  "priority": "high",
  "title": "Agent <name> missing subscription credentials",
  "timestamp": "<iso-timestamp>"
}
```

---

### Flow 4: Aggregate Health Status Degradation

**File**: `src/backend/services/monitoring_service.py:310-387`

The `aggregate_health()` function includes credential status in its evaluation. If credentials are missing (after remediation attempt), the agent is classified as `DEGRADED`.

```python
# In aggregate_health() at line 373-375:
if business.credential_status == "missing":
    issues.append("Subscription credentials missing")
    return AgentHealthStatus.DEGRADED, issues
```

This check runs after all critical/unhealthy checks, so credential issues appear as degradation rather than a higher-severity status. The priority order is:

1. **CRITICAL**: Container not found, stopped, OOM killed
2. **UNHEALTHY**: Network unreachable, runtime not available
3. **DEGRADED**: High CPU/memory, high latency, context usage, stuck executions, **missing credentials**
4. **HEALTHY**: All checks passing

---

### Flow 5: Subscription Assignment Error Surfacing

**File**: `src/backend/routers/subscriptions.py:203-227`

When assigning a subscription to a running agent, the endpoint now properly reports injection failures instead of silently swallowing them.

```python
# Try to inject credentials if agent is running
from services.subscription_service import inject_subscription_to_agent
injection_result = await inject_subscription_to_agent(agent_name, subscription.id)

if injection_result.get("status") == "failed":
    logger.error(
        f"Subscription injection failed for agent '{agent_name}': "
        f"{injection_result.get('error', 'unknown')}"
    )

return {
    "success": True,
    "message": f"Subscription '{subscription_name}' assigned to agent '{agent_name}'",
    "agent_name": agent_name,
    "subscription_name": subscription_name,
    "injection_result": injection_result  # <-- Always returned, includes failure details
}
```

The response always includes `injection_result` so callers (UI, MCP tools) can see whether hot-injection succeeded or failed. Previously, injection errors were caught silently.

---

### Flow 6: Scheduler Auth Error Detection

**File**: `src/scheduler/service.py:673-717`

The scheduler's `_execute_schedule_with_lock()` method now detects authentication-related failures in scheduled task responses and labels them distinctly.

```python
except Exception as e:
    error_msg = str(e)

    # Detect auth-specific failures for better error surfacing
    auth_indicators = [
        "credit balance", "unauthorized", "authentication",
        "credentials", "forbidden", "401", "403",
        "oauth", "token expired", "not authenticated"
    ]
    error_lower = error_msg.lower()
    is_auth_failure = any(ind in error_lower for ind in auth_indicators)

    if is_auth_failure:
        error_msg = f"[AUTH_ERROR] {error_msg}"
        logger.error(
            f"Schedule {schedule.name} execution failed due to authentication error: {error_msg}"
        )
    else:
        logger.error(f"Schedule {schedule.name} execution failed: {error_msg}")
```

The `[AUTH_ERROR]` prefix is stored in the execution record's `error` field, making auth failures searchable and distinguishable in the execution history. The activity completion also receives a `failure_category: "auth"` metadata field:

```python
await self._complete_activity(
    activity_id=activity_id,
    status="failed",
    error=error_msg,
    details={
        "related_execution_id": execution.id,
        **({"failure_category": "auth"} if is_auth_failure else {})
    }
)
```

---

## Data Model Changes

### BusinessHealthCheck (`db_models.py:710`)

New field added:

```python
class BusinessHealthCheck(BaseModel):
    # ... existing fields ...
    credential_status: Optional[str] = None  # null, "ok", "missing" (SUB-001/MON-001)
```

This field is also persisted in the `agent_health_checks` table as part of business metrics JSON (`monitoring_service.py:484`):

```python
db.create_health_check(
    agent_name=agent_name,
    check_type="business",
    status=business_check.status,
    business_metrics={
        # ... other metrics ...
        "credential_status": business_check.credential_status,
    }
)
```

---

## Side Effects

| Side Effect | When | Data |
|------------|------|------|
| **WebSocket broadcast** | Alert fired after failed remediation | `{type: "monitoring_alert", priority: "high", title: "Agent X missing subscription credentials"}` |
| **Notification created** | Alert fired after failed remediation | NOTIF-001 record: type="alert", category="health", priority="high" |
| **Alert cooldown set** | After first alert for an agent | condition="subscription:credentials_missing", duration=600s (10 min) |
| **Health check records** | Every monitoring cycle | 4 records per agent (docker, network, business, aggregate) with credential_status in business metrics |
| **Credential re-injection** | Auto-remediation attempt | POST to agent's /api/credentials/inject with decrypted subscription JSON |
| **Activity metadata** | Scheduler auth failure | `failure_category: "auth"` in activity details |
| **Execution error prefix** | Scheduler auth failure | Error message prefixed with `[AUTH_ERROR]` |

---

## Error Handling

| Error Case | Behavior | Recovery |
|------------|----------|----------|
| Agent container unreachable | `credential_status` stays null (not checked) | Next monitoring cycle retries |
| Subscription assigned but credentials decryption fails | `inject_subscription_to_agent()` returns `{status: "failed"}` | Alert fires, admin must re-register subscription |
| Agent's `/api/credentials/status` returns non-200 | `credential_status = "missing"` | Auto-remediation attempted |
| Auto-remediation injection fails (all 5 retries) | Alert fired via `alert_subscription_credentials_missing()` | Admin manually re-injects or restarts agent |
| Alert service import fails | Exception caught, monitoring continues | Next cycle retries |
| Scheduler detects auth error pattern | Error prefixed with `[AUTH_ERROR]` | Admin checks subscription validity |

---

## Known Limitations

1. **Agent `/api/credentials/status` gap**: The agent-side endpoint (`docker/base-image/agent_server/routers/credentials.py:143-148`) does not include `.claude/.credentials.json` in its hardcoded file list. The monitoring service checks for this path in the response, which means the credential check always reports "missing" for subscription-enabled agents. Auto-remediation re-injects credentials every monitoring cycle as a compensating mechanism.

2. **Cooldown prevents repeated alerts**: The 10-minute cooldown on `subscription:credentials_missing` means only one alert per agent per 10 minutes, even if remediation fails every 30-second cycle.

3. **No frontend-specific credential health UI**: Credential status is surfaced through the existing monitoring page as a "degraded" status with the issue "Subscription credentials missing". There is no dedicated credential health indicator in the UI.

4. **API key conflict now prevented at container level**: As of Issue #57, `ANTHROPIC_API_KEY` is removed from container env when a subscription is assigned. This eliminates the scenario where Claude Code ignores OAuth credentials in favor of an API key. The credential health monitoring still detects missing `.credentials.json` files, but the API key conflict case is no longer a failure mode.

---

## Testing

### Prerequisites
- Trinity platform running with monitoring enabled
- Admin user logged in
- At least one agent with a subscription assigned and running

### Test Steps

#### Test 1: Credential Health Check Detection

1. **Action**: Assign a subscription to a running agent via `PUT /api/subscriptions/agents/{name}?subscription_name=test`
2. **Expected**: Agent shows `credential_status: "ok"` or auto-remediation triggers
3. **Verify**: `GET /api/monitoring/agents/{name}` shows business check with `credential_status` field

#### Test 2: Auto-Remediation

1. **Action**: Exec into agent container and delete `~/.claude/.credentials.json`
2. **Expected**: Next monitoring cycle detects missing credentials and re-injects
3. **Verify**: Container file is restored, agent logs show "Auto-remediated missing credentials"

#### Test 3: Alert on Failed Remediation

1. **Action**: Delete the subscription from the database while agent is running (simulates decryption failure)
2. **Expected**: Monitoring detects missing credentials, remediation fails, alert fires
3. **Verify**: Notification appears in `/events` page with title "Agent X missing subscription credentials"

#### Test 4: Scheduler Auth Error Detection

1. **Action**: Run a scheduled task on an agent with invalid/expired subscription credentials
2. **Expected**: Execution fails with `[AUTH_ERROR]` prefix in error message
3. **Verify**: Execution record in database has error starting with `[AUTH_ERROR]`, activity has `failure_category: "auth"`

#### Test 5: Subscription Assignment Error Reporting

1. **Action**: Assign subscription to a running agent whose internal API is temporarily unreachable
2. **Expected**: Response includes `injection_result.status: "failed"` and `injection_result.error: "Agent restart failed (<ExceptionClass> — details in backend logs)"`; the exception text is in the backend log, not the response
3. **Verify**: API response JSON contains full `injection_result` object

---

## Files Changed

| File | Lines | Change |
|------|-------|--------|
| `src/backend/db_models.py` | 710 | Added `credential_status` field to `BusinessHealthCheck` |
| `src/backend/services/monitoring_service.py` | 258-306 | Credential check in `check_business_health()` |
| `src/backend/services/monitoring_service.py` | 373-375 | `aggregate_health()` treats missing credentials as DEGRADED |
| `src/backend/services/monitoring_service.py` | 423-439 | Auto-remediation in `perform_health_check()` |
| `src/backend/services/monitoring_service.py` | 558-563 | Alert trigger after failed remediation |
| `src/backend/services/monitoring_alerts.py` | 379-413 | New `alert_subscription_credentials_missing()` method |
| `src/backend/routers/subscriptions.py` | 215-220 | Error logging for injection failures on assignment |
| `src/backend/services/subscription_service.py` | 27 | `max_retries` increased from 3 to 5 |
| `src/scheduler/service.py` | 677-717 | Auth error detection and `[AUTH_ERROR]` prefix |

---

## Related Flows

- [subscription-management.md](subscription-management.md) - Core subscription CRUD and injection (SUB-001)
- [agent-monitoring.md](agent-monitoring.md) - Health monitoring infrastructure (MON-001)
- [credential-injection.md](credential-injection.md) - General credential injection system (CRED-002)
- [agent-lifecycle.md](agent-lifecycle.md) - Subscription injection on agent start
- [agent-notifications.md](agent-notifications.md) - Notification system used for alerts (NOTIF-001)
- [scheduler-service.md](scheduler-service.md) - Scheduler service with auth error detection

---

## Revision History

| Date | Changes |
|------|---------|
| 2026-03-02 | Updated for credential priority fix (Issue #57): Added note about API key conflict prevention at container level. Added limitation #4 about the resolved failure mode. |
| 2026-03-02 | Initial documentation for subscription credential health monitoring (Issue #57) |
