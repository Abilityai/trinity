# The `user` role is Workspace-only — operator note (trinity-enterprise#837, 2026-10)

**Action required:** before upgrading, raise every account that operates agents to
`operator` (or above). An account left at `user` keeps the Workspace and loses the
operator UI. No database migration runs, and no role is rewritten.

## What changes

- **The platform role `user` means a member who works with agents in the Workspace.**
  The operator UI (Dashboard, agent pages, Operations, Library, Settings) starts at
  `operator`. The role ladder itself is unchanged: `user` < `operator` < `creator` < `admin`.
- **The boundary is server-side.** Every operator route answers a `user` account with
  `403 {"code": "workspace_only", "message": "This account works in the Workspace. Open /workspace."}`,
  including the API and the MCP server reached with that account's keys. The Workspace,
  and the few operator-side routes it calls, keep working. The account's live-update stream
  carries only the events the Workspace shows.
- **Inside the Workspace, a `user` keeps the team view:** every canvas the agent draws, the
  agent's whole activity (loop runs included), voice and model choice. Someone you only shared
  an agent with, and who has no platform account, sees the narrower client view: the canvases
  the agent addressed to them and their own work. To give a person only that view, share the
  agent with them instead of whitelisting their email.
- **Signing in:** `/login` lands a `user` in `/workspace`. Opening an operator URL takes a
  `user` to the Workspace too (an agent page opens that agent's conversation).
- **Sharing stops creating platform logins.** Sharing an agent with an email, and
  approving an access request, no longer add the email to the login whitelist. The person
  signs in to the Workspace with the code emailed to them. (One exception: with MCP inline
  sign-in enabled, `MCP_INLINE_AUTH_ENABLED`, off by default, an MCP client that signs in with
  a shared address still gets a Workspace-only `user` account.) A platform account is an admin's
  decision: add the email to Settings → Access → Email Whitelist (which now has a role
  picker, default `user`), or change a role in User Management.

## Who is affected

Every account whose role is `user`. They usually got it from one of these:

| Source | Why it holds `user` |
|---|---|
| Settings → Access → Email Whitelist | the form added every email at `user` until this release |
| Sharing an agent, or approving an access request, before this release | the share wrote a whitelist row at `user` (#314) |
| Single sign-on, first sign-in | new accounts take the provider's configured default role, `user` unless changed |
| Public self-signup (`POST /api/access/request`, when enabled) | always `user` |

## Before you upgrade

1. **List the `user` accounts.**

   SQLite:

   ```bash
   sqlite3 ~/trinity-data/trinity.db \
     "SELECT username, email, last_login FROM users WHERE role = 'user' ORDER BY last_login DESC;"
   ```

   PostgreSQL:

   ```bash
   docker exec trinity-postgres psql -U trinity -d trinity -c \
     "SELECT username, email, last_login FROM users WHERE role = 'user' ORDER BY last_login DESC;"
   ```

   After the upgrade, Settings → Access → User Management shows how many accounts are
   Workspace-only, and **Show only these** filters the table to them.

2. **Raise anyone who operates agents** to `operator`: Settings → Access → User Management →
   Role, or `PUT /api/users/{username}/role` with `{"role": "operator"}` as an admin.

3. **If accounts are created on first single sign-on**, set the provider's default role to
   `operator` for a team of operators, or raise each account after it first signs in.

## Machine credentials and agents owned by a `user` account

- **MCP API keys** owned by a `user` account are refused on operator routes, on the few
  operator-side routes the Workspace calls (those admit only a browser session) and on the
  `/ws/events` stream. Such a key still reaches the Workspace's own API, as before. The account can no longer list or revoke them, because key
  management is an operator route. An admin can revoke any key by id
  (`POST /api/mcp/keys/{key_id}/revoke`). To list the keys still active:

  ```sql
  SELECT k.id, k.name, k.scope, u.username
  FROM mcp_api_keys k JOIN users u ON u.id = k.user_id
  WHERE u.role = 'user' AND k.is_active = 1;
  ```

- **Agents owned by a `user` account** (possible only after a demotion) keep their own
  runtime routes: heartbeat, result delivery, reports, notifications, reading their own
  record, their own asks, and the skill-gate check. Everything else they did through the
  platform — MCP fleet tools, schedules, canvases, chatting other agents (rooms included) — is refused until
  an admin raises the owner's role. What runs without the agent's key keeps running:
  its schedules, webhooks and channel bots, and its connector keys on their own routes (the
  agent's chat and playbook list). The owner can no longer pause those (they are operator
  routes), so an admin pauses them or raises the owner. There is no ownership transfer, and
  the agent's own key stays bound to the account that minted it. To find them:

  ```sql
  SELECT o.agent_name, u.username
  FROM agent_ownership o JOIN users u ON u.id = o.owner_id
  WHERE u.role = 'user';
  ```

- A `user` account no longer opens an agent's terminal.

## Lowering a role, and open sessions

A role change applies to a live connection when it reconnects. A lowered account's open
pages keep the scope they connected with: its `/ws` stream until it reconnects (the
Workspace reconnects it on the next refused call), and an open agent terminal until it is
closed. To end every open session at once, restart the backend: everyone signs in again.

## Whitelist rows written by sharing

Rows whose source is agent sharing or an access request stay where they are. Adding such an
email again on the Email Whitelist form replaces that row, so you can give the person
`operator` before their first sign-in; an account that already exists keeps its role until you
change it in User Management. Their emails
can still sign in at `/login`, and the account they get is a Workspace-only `user`. Remove a
row in Settings → Access → Email Whitelist if that person should use only the Workspace's
code sign-in.

## Rolling back

Redeploying the previous release restores the old behaviour. There is nothing to undo in the
data. Shares made while the new release ran wrote no whitelist row; if you roll back and those
people need a platform login, add their emails by hand.
