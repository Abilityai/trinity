# Roles and Permissions

Trinity uses a 4-tier role system to control who can create agents, manage existing ones, or just interact with them.

## Role Hierarchy

| Role | Can Create Agents | Can Manage Agents | Entry Path |
|------|-------------------|-------------------|------------|
| **admin** | Yes | All agents | Password login (username `admin` or the registered admin email) |
| **creator** | Yes | Own agents only | Promoted by an admin, or a whitelist entry added with `default_role: creator` |
| **operator** | No | Assigned agents only | Set by an admin, or a whitelist entry added with `default_role: operator` |
| **user** | No | No | The default for whitelist entries, sharing grants, and approved access requests; public links (no account) |

Roles are hierarchical: admin > creator > operator > user. Higher roles inherit all permissions of lower roles.

## How It Works

### The admin account

The `admin` account is created once: from `ADMIN_PASSWORD` at first boot, or through the first-run **Create your admin account** form when no password was set. The form only ever provisions the **first** admin — on an install that already has a usable admin account it refuses, whether or not the setup flag says setup is complete. See [Setup](setup.md).

If two-factor authentication applies to the account (requires an entitlement), a correct password does not sign you in on its own: the login page moves to the second-factor step, and no session exists until it is completed. Two-factor can start applying the moment an administrator enables the role policy, before anyone has enrolled.

### Default Role Assignment

When you sign up via email (if whitelisted), you receive the **default role recorded on your whitelist entry** — `user` unless the admin set another `default_role` when adding the email through the API (the Settings whitelist form always adds at `user`). Being granted access to an agent (sharing, or an approved access request) whitelists you at `user`.

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
| Turn usage sharing on or off | Admin, **human only** |
| Mint a **Portal delegate** MCP key | Admin, **human only** |
| Mint an **Ops (read-only)** MCP key | Admin, **interactive browser session only** — no key of any scope can mint one |

### Role is not the same as "human"

An MCP API key resolves to the user who created it and **carries that user's role**. Because an agent's own key is created under its owner, an agent operating on a default admin-owned installation would otherwise satisfy any check that asks only "is this caller an admin?".

Three rules close that gap:

- **An agent-scoped key never satisfies an admin gate.** Every admin-only endpoint rejects agent principals itself, so a new admin endpoint is protected without anyone remembering to add a check. What an agent's key *can* do is act on its own agent (heartbeats, reports, result callbacks) and whatever its owner has shared with it.
- **Admin gates are an allowlist over key scopes.** Only a browser session, a standard (`user`) key, and the system agent's key can pass one. Any other scope — including one that does not exist yet — is refused.
- **Endpoints whose blast radius is operator-scale require a human caller** in addition to the role — an API key of any scope is rejected there. See [Authentication](../api-reference/authentication.md) for the list.

For monitoring integrations that must keep working under enforced two-factor, an admin can mint a bounded **Ops (read-only)** key instead of handing out an unbounded personal one. It reaches a fixed set of read endpoints (fleet health, telemetry, roster and capacity, execution history, subscription usage) and nothing else, gets no MCP tools, and every call it makes is attributed to that key in the audit log. It stays bound to the account that minted it: if that admin is demoted or suspended, the key stops working — so mint it under a service account that is not offboarded with a person. Scopes are described in [Authentication → MCP key scopes](../api-reference/authentication.md#mcp-key-scopes).

## Managing User Roles

**Admin only**: Navigate to **Settings → Access** and find the **User Management** section.

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
- [Authentication](../api-reference/authentication.md) — Tokens, MCP key scopes, and the human-only gates
- [Agent Sharing](../sharing-and-access/agent-sharing.md) — Sharing agents with users
- [Agent Quotas](../operations/agent-quotas.md) — Per-role agent creation limits
