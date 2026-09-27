# Agent Quotas

Per-role limits on how many agents each user can create. Admins are always exempt.

## Default Quotas

| Role | Default Limit |
|------|---------------|
| admin | Unlimited |
| creator | 10 |
| operator | 3 |
| user | 1 |

## How It Works

When a user tries to create an agent (via UI or API), Trinity checks:
1. How many agents they currently own (excluding system agents).
2. Their role-based quota limit.
3. If at or above limit, the request is rejected with HTTP 429.

### Redeploy Exception

Redeploying an existing agent you own does not count against your quota — it's an update, not a new creation.

## Configuring Quotas

**Admin only**: Navigate to **Settings** and find the **Agent Quotas** section.

1. Set limits for each role (creator, operator, user).
2. Enter `0` for unlimited.
3. Click **Save**.

Admin quota is always unlimited and cannot be changed.

## For Agents

### Query Quota Settings

```bash
GET /api/settings/agent-quotas
```

**Response:**
```json
{
  "quotas": {
    "admin": {"value": 0, "description": "Unlimited (not editable)"},
    "creator": {"value": 10, "default": 10},
    "operator": {"value": 3, "default": 3},
    "user": {"value": 1, "default": 1}
  }
}
```

### Update Quotas

```bash
PUT /api/settings/agent-quotas
```

**Request:**
```json
{
  "max_agents_creator": "15",
  "max_agents_operator": "5",
  "max_agents_user": "2"
}
```

### Error Response

When quota is exceeded:

```json
{
  "error": "Agent quota exceeded. You have 10/10 agents. Delete an agent to create a new one.",
  "code": "QUOTA_EXCEEDED",
  "current": 10,
  "limit": 10
}
```

HTTP status: 429 Too Many Requests

## System Agents

System agents (those with `is_system=true`) are excluded from quota counts. Only user-created agents count toward the limit.

## Migration from Legacy Setting

If your installation used the old `max_agents_per_user` setting, it serves as a fallback until you configure per-role quotas. The Settings UI shows a warning banner if the legacy setting exists.

## See Also

- [Roles and Permissions](../getting-started/roles-and-permissions.md) — Role hierarchy
- [Creating Agents](../agents/creating-agents.md) — Agent creation flow
