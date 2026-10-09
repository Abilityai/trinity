# Roles and Permissions

Trinity uses a 4-tier role system to control who can create agents, manage existing ones, or work with them in the [Workspace](../sharing-and-access/workspace.md) only.

## Role Hierarchy

| Role | Can Create Agents | Can Manage Agents | Entry Path |
|------|-------------------|-------------------|------------|
| **admin** | Yes | All agents | Password login (username `admin` or the registered admin email) |
| **creator** | Yes | Own agents only | Promoted by an admin, or a whitelist entry added with `default_role: creator` |
| **operator** | No | Assigned agents only | Set by an admin, or a whitelist entry added with role `operator` |
| **user** | No | No — **Workspace only** | The default for whitelist entries and single sign-on accounts; public self-signup |

**`user` means a member who works with agents in the Workspace.** A `user` account signs in at `/login` and lands in `/workspace`: the agents shared with them (plus any they own), their chats, the Inbox, Work, rooms, loops, voice, files and projects. Everything else — the Dashboard, agent pages, Operations, Library, Settings, and the API behind them — starts at **operator**. The refusal is server-side: an operator route answers a `user` with `403 workspace_only` ("This account works in the Workspace. Open /workspace."), whether the call comes from the browser, the API or an MCP key the account owns.

Roles are hierarchical: admin > creator > operator > user. Higher roles inherit all permissions of lower roles.

## How It Works

### The admin account

The `admin` account is created once: from `ADMIN_PASSWORD` at first boot, or through the first-run **Create your admin account** form when no password was set (the one-click marketplace and AWS paths — the first browser visitor claims the instance; on AWS they must also enter the EC2 instance ID). The form only ever provisions the **first** admin — on an install that already has a usable admin account it refuses, whether or not the setup flag says setup is complete. See [Setup](setup.md).

If two-factor authentication applies to the account (requires an entitlement), a correct password does not sign you in on its own: the login page moves to the second-factor step, and no session exists until it is completed. Two-factor can start applying the moment an administrator enables the role policy, before anyone has enrolled.

### Default Role Assignment

When you sign up via email (if whitelisted), you receive the **role recorded on your whitelist entry** — `user` unless the admin picked another role when adding the email (Settings → Access → Email Whitelist has a role picker, default `user`).

Being granted access to an agent — sharing, or an approved access request — does **not** create a platform account. The person signs in to the [Workspace](../sharing-and-access/workspace.md) with a code emailed to them. Whitelist rows that sharing wrote before this changed are still there, and the accounts they produce are Workspace-only `user` accounts.

Admins can change a user's role at any time via Settings. To give someone the operator UI, raise them to `operator`.

### Role-Based Restrictions

| Action | Required Role |
|--------|---------------|
| Create agents | creator or above |
| Delete agents | Owner or admin |
| Configure agent settings (autonomy, resources, capacity, timeout, guardrails, read-only mode and similar) | Owner or admin, **a person only** — a browser session or the person's own API key, never an agent's key |
| Open the operator UI (Dashboard, agent pages, Operations, Settings) | operator or above |
| Run tasks and schedules | operator or above (with access) |
| Chat with shared agents | Any authenticated user — in the Workspace for a `user` |
| Use public links | Anyone (no auth required) |
| Install a system from a manifest | creator or above |
| Manage skill sources | Admin, **human only** |
| Approve an oversized retention deletion | Admin, **human only** |
| Turn usage sharing on or off | Admin, **human only** |
| Mint a **Portal delegate** MCP key | Admin, **human only** |
| Mint an **Ops (read-only)** MCP key | Admin, **interactive browser session only** — no key of any scope can mint one |
| Create or list MCP keys | operator or above, **signed-in session only** — no key of any scope can create or list them |
| Change your own sign-in email or personal GitHub token | Any user, **signed-in session only**; a new sign-in email also needs the 6-digit code mailed to it |

### Role is not the same as "human"

An MCP API key resolves to the user who created it and **carries that user's role**. Because an agent's own key is created under its owner, an agent operating on a default admin-owned installation would otherwise satisfy any check that asks only "is this caller an admin?".

Four rules close that gap:

- **An agent-scoped key never satisfies an admin gate.** Every admin-only endpoint rejects agent principals itself, so a new admin endpoint is protected without anyone remembering to add a check. What an agent's key *can* do is act on its own agent (heartbeats, reports, result callbacks) and whatever its owner has shared with it.
- **Admin gates are an allowlist over key scopes.** Only a browser session, a standard (`user`) key, and the system agent's key can pass one. Any other scope — including one that does not exist yet — is refused.
- **Endpoints whose blast radius is operator-scale require a human caller** in addition to the role — an API key of any scope is rejected there. See [Authentication](../api-reference/authentication.md) for the list.
- **Owner-level grants require a person too.** An agent's configuration writes (autonomy, resources, guardrails and the rest) refuse agent-scoped and system keys even when the owner is an admin, so an agent cannot switch on its own unattended schedules or raise its own limits.

For monitoring integrations that must keep working under enforced two-factor, an admin can mint a bounded **Ops (read-only)** key instead of handing out an unbounded personal one. It reaches a fixed set of read endpoints (fleet health, telemetry, roster and capacity, execution history, subscription usage) and nothing else, gets no MCP tools, and every call it makes is attributed to that key in the audit log. It stays bound to the account that minted it: if that admin is demoted or suspended, the key stops working — so mint it under a service account that is not offboarded with a person. Scopes are described in [Authentication → MCP key scopes](../api-reference/authentication.md#mcp-key-scopes).

## Managing User Roles

**Admin only**: Navigate to **Settings → Access** and find the **User Management** section.

1. Find the user in the table. When some accounts are Workspace-only, the section says how many; **Show only these** filters the table to them.
2. Select a new role from the dropdown (`user` reads "user — Workspace only").
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
- Public link users have no database entry and need no role.
- An agent owned by a `user` account (possible only after a demotion) keeps its own heartbeat, reports, notifications and asks; everything else it did through the platform is refused until an admin raises the owner's role (agent ownership cannot be transferred).
- Admins cannot demote themselves.

## See Also

- [Setup](setup.md) — First-time admin configuration
- [Authentication](../api-reference/authentication.md) — Tokens, MCP key scopes, and the human-only gates
- [Agent Sharing](../sharing-and-access/agent-sharing.md) — Sharing agents with users
- [Agent Quotas](../operations/agent-quotas.md) — Per-role agent creation limits
