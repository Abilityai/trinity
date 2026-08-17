# Roles and Permissions

Trinity uses a 4-tier role system to control who can create agents, manage existing ones, or just interact with them.

## Role Hierarchy

| Role | Can Create Agents | Can Manage Agents | Entry Path |
|------|-------------------|-------------------|------------|
| **admin** | Yes | All agents | Password login |
| **creator** | Yes | Own agents only | Email whitelist signup |
| **operator** | No | Assigned agents only | Email whitelist, role manually set |
| **user** | No | No | Public links only |

Roles are hierarchical: admin > creator > operator > user. Higher roles inherit all permissions of lower roles.

## How It Works

### Default Role Assignment

When you sign up via email (if whitelisted), you receive the **creator** role by default. This allows you to immediately create and manage agents.

Admins can change a user's role at any time via Settings.

### Role-Based Restrictions

| Action | Required Role |
|--------|---------------|
| Create agents | creator or above |
| Delete agents | Owner or admin |
| Configure agent settings | Owner or admin |
| Run tasks and schedules | operator or above (with access) |
| Chat with shared agents | Any authenticated user |
| Use public links | Anyone (no auth required) |
| Install a system from a manifest | creator or above |
| Manage skill sources | Admin, **human only** |
| Approve an oversized retention deletion | Admin, **human only** |

### Role is not the same as "human"

An MCP API key resolves to the user who created it and **carries that user's role**. Because an agent's own key is created under its owner, an agent operating on a default admin-owned installation would otherwise satisfy any check that asks only "is this caller an admin?".

Endpoints whose blast radius is operator-scale therefore require a human caller *in addition to* the role — an API key of any scope is rejected there, no matter whose it is. See [Authentication](../api-reference/authentication.md) for the list.

## Managing User Roles

**Admin only**: Navigate to **Settings** and scroll to the **User Management** section.

1. Find the user in the table.
2. Select a new role from the dropdown.
3. The change takes effect immediately on their next request.

You cannot change your own role.

## For Agents

User roles are stored in the `users` table. The role is checked on each API request via the `require_role()` dependency.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/users` | GET | List all users (admin-only) |
| `/api/users/{username}/role` | PUT | Change a user's role (admin-only) |

**Request body:**
```json
{"role": "operator"}
```

**Valid roles:** `admin`, `creator`, `operator`, `user`

## Limitations

- Role changes apply immediately but don't invalidate existing JWT tokens.
- Public link users have no database entry — they operate at the `user` level.
- Admins cannot demote themselves.

## See Also

- [Setup](setup.md) — First-time admin configuration
- [Agent Sharing](../sharing-and-access/agent-sharing.md) — Sharing agents with users
- [Agent Quotas](../operations/agent-quotas.md) — Per-role agent creation limits
