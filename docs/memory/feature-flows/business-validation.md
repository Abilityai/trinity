# Business Task Validation (VALIDATE-001)

Post-execution validation feature that runs a clean-context Claude session to verify business task completion.

## Overview

After a scheduled task completes successfully (technical status), an optional validation phase runs to verify that the business task was actually completed correctly. This separates technical success (Claude ran without errors) from business success (the intended work was done).

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        Validation Flow                                   │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  Scheduler Service              Backend                 Agent Container  │
│  ┌─────────────────┐           ┌──────────────────┐    ┌─────────────┐ │
│  │ _poll_and_      │           │ ValidationService│    │   Claude    │ │
│  │  finalize()     │ ──(1)──►  │ validate_        │    │    Code     │ │
│  │                 │   POST    │  execution()     │    │             │ │
│  │                 │ /internal │                  │    │             │ │
│  │                 │ /validate │                  │    │             │ │
│  │                 │           │   ┌──────────┐   │    │             │ │
│  │                 │           │   │ Build    │   │    │             │ │
│  │                 │           │   │ Auditor  │───┼────►             │ │
│  │                 │           │   │ Prompt   │   │(2) │             │ │
│  │                 │           │   └──────────┘   │    │             │ │
│  │                 │           │                  │    │             │ │
│  │                 │           │   ┌──────────┐   │    │             │ │
│  │                 │           │   │ Parse    │◄──┼────┤  JSON or    │ │
│  │                 │           │   │ Response │   │(3) │  Text       │ │
│  │                 │           │   └──────────┘   │    │  Response   │ │
│  │                 │           │                  │    │             │ │
│  │                 │           │   ┌──────────┐   │    │             │ │
│  │                 │  ◄──(4)── │   │ Update   │   │    │             │ │
│  │                 │   status  │   │ Business │   │    │             │ │
│  │                 │           │   │ Status   │   │    │             │ │
│  │                 │           │   └──────────┘   │    └─────────────┘ │
│  └─────────────────┘           └──────────────────┘                     │
│                                                                          │
└─────────────────────────────────────────────────────────────────────────┘
```

## Data Model

### Schedule Fields (validation config)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `validation_enabled` | bool | false | Enable post-execution validation |
| `validation_prompt` | text | null | Custom auditor prompt (uses default if null) |
| `validation_timeout_seconds` | int | 120 | Timeout for validation execution |

### Execution Fields (validation tracking)

| Field | Type | Description |
|-------|------|-------------|
| `business_status` | enum | pending_validation, validated, failed_validation, skipped |
| `validated_at` | timestamp | When validation completed |
| `validation_execution_id` | FK | Points to the validation execution record |
| `validates_execution_id` | FK | Set on validation execution, points back to original |

### BusinessStatus Enum

```python
class BusinessStatus(str, Enum):
    PENDING_VALIDATION = "pending_validation"  # Waiting for validation
    VALIDATED = "validated"                    # Validation passed
    FAILED_VALIDATION = "failed_validation"    # Validation failed
    SKIPPED = "skipped"                        # Validation not configured
```

## Key Files

| Layer | File | Purpose |
|-------|------|---------|
| Backend | `src/backend/services/validation_service.py` | Core validation logic |
| Backend | `src/backend/routers/internal.py` | `/validate-execution` endpoint |
| Backend | `src/backend/models.py` | Schedule/execution response models (centralized per Invariant #14, #654) |
| Backend | `src/backend/db/schedules.py` | DB operations for validation |
| Backend | `src/backend/models.py` | BusinessStatus enum |
| Scheduler | `src/scheduler/service.py` | Triggers validation after execution |
| Tests | `tests/test_validation.py` | Unit and integration tests |

## Validation Prompt

The default prompt uses explicit auditor framing:

```
You are an AUDITOR, not an executor. Your task is to VALIDATE whether
the previous execution successfully completed its intended work.

## Important Context
- This is a POST-EXECUTION validation session
- You are reviewing work that was ALREADY ATTEMPTED by this agent
- Your job is to VERIFY completion, not to DO the work

## Original Task
{original_message}

## Execution Output
{execution_response}

## Your Validation Task
1. Check if work was actually completed (not just claimed)
2. Verify artifacts/files/changes exist
3. Check for completeness
4. Look for errors, partial completions, or hallucinated success

## Response Format
{
  "status": "pass" | "fail" | "partial",
  "summary": "One sentence summary",
  "items": [{"check": "...", "result": "pass|fail", "evidence": "..."}],
  "recommendation": "Action if failed (optional)"
}
```

## Response Parsing

The service parses validation responses with fallback:

1. **JSON extraction**: Look for `{...}` in response
2. **Markdown code blocks**: Extract from ```json blocks
3. **Text inference**: Analyze for pass/fail indicators
4. **Default**: Return PARTIAL if unclear

## Validation Flow

1. **Scheduler completes execution** successfully (status=completed)
2. **Check `validation_enabled`** on schedule
3. **Call backend** `POST /api/internal/validate-execution`
4. **Backend creates** validation execution record (linked via `validates_execution_id`)
5. **Build auditor prompt** with original message + execution response
6. **Run validation** via TaskExecutionService (clean context)
7. **Parse response** into ValidationResult
8. **Update original execution** `business_status` and `validation_execution_id`
9. **On failure**: Add to operator queue for human review

## Operator Queue Integration

When validation fails:

```json
{
  "id": "val_{execution_id}_{timestamp}",
  "type": "alert",
  "priority": "high",
  "status": "pending",
  "title": "Validation Failed",
  "question": "Execution validation failed: {summary}",
  "context": {
    "execution_id": "...",
    "validation_status": "fail",
    "summary": "...",
    "items": [...],
    "recommendation": "..."
  }
}
```

## API Endpoints

### Internal (scheduler -> backend)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/internal/validate-execution` | Trigger validation |

Request:
```json
{
  "execution_id": "exec-123",
  "agent_name": "my-agent",
  "schedule_id": "sched-456",
  "original_message": "Create README file",
  "execution_response": "I created README.md...",
  "custom_prompt": null,
  "timeout_seconds": 120
}
```

### Schedule CRUD (include validation config)

| Method | Path | Fields |
|--------|------|--------|
| POST | `/api/agents/{name}/schedules` | `validation_enabled`, `validation_prompt`, `validation_timeout_seconds` |
| PUT | `/api/agents/{name}/schedules/{id}` | Same fields |
| GET | `/api/agents/{name}/schedules/{id}` | Returns validation config |

### Execution (include business status)

| Method | Path | Fields |
|--------|------|--------|
| GET | `/api/agents/{name}/executions` | `business_status`, `validation_execution_id` |
| GET | `/api/agents/{name}/executions/{id}` | Full validation fields |

## Database Schema

```sql
-- Schedule: validation config
ALTER TABLE agent_schedules ADD COLUMN validation_enabled INTEGER DEFAULT 0;
ALTER TABLE agent_schedules ADD COLUMN validation_prompt TEXT;
ALTER TABLE agent_schedules ADD COLUMN validation_timeout_seconds INTEGER DEFAULT 120;

-- Execution: validation tracking
ALTER TABLE schedule_executions ADD COLUMN business_status TEXT;
ALTER TABLE schedule_executions ADD COLUMN validated_at TEXT;
ALTER TABLE schedule_executions ADD COLUMN validation_execution_id TEXT;
ALTER TABLE schedule_executions ADD COLUMN validates_execution_id TEXT;

-- Indexes
CREATE INDEX idx_executions_business_status ON schedule_executions(business_status);
CREATE INDEX idx_executions_validates ON schedule_executions(validates_execution_id);
```

## Related Features

- [Scheduling](scheduling.md) - Cron-based task scheduling
- [Operator Queue](operator-queue.md) - Human review queue for failures
- [Activity Stream](activity-stream.md) - Activity tracking

## Issue Reference

- GitHub Issue: #294
- Feature ID: VALIDATE-001
