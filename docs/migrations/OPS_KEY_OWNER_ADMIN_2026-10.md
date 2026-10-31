# An `ops` key stops when its owner is no longer an admin — operator note (#3180, 2026-10)

**Action required:** before upgrading, find every active `ops` key whose owner is no longer an
admin, and re-mint it under a current admin. After the upgrade such a key is refused on every
route, including the reads it still answered before. No database migration runs, and no key is
revoked.

## What changes

- **An `ops` key works only while the account that minted it is an admin.** The `ops` scope
  (#2323) is the read-only machine credential for monitoring. Its docs always said that demoting
  the owner stops the key, but only the reads with an admin gate of their own enforced it. The
  reads held by the `ops` route fence alone kept answering a key whose owner had been demoted to
  `creator`, `operator` or `user`: the platform version, monitoring status, host and container
  telemetry, the agent roster, the execution reads and live log relay, and the subscription reads.
- **From this release the owner's role is checked on every route**, at the single auth entry
  point and ahead of the route fence. Such a key now gets:

  ```
  403 {"code": "ops_owner_not_admin", "message": "Ops keys work only while the account that minted them is an admin"}
  ```

- **This closes a gap between the docs and the behaviour; it is not an escalation fix.** A
  demoted owner could read the same endpoints with their own session.
- **Demotion suspends the key; it does not revoke it.** The key row stays active, and promoting
  the owner back to admin makes the key work again. Revoke the key to end it.
- A suspended owner's key was already refused (`401`), and that is unchanged.

## Who is affected

Anything that reads with an `ops` key minted by an account that is no longer an admin: a
monitoring dashboard, an ops agent, a script. To list those keys:

SQLite:

```bash
sqlite3 ~/trinity-data/trinity.db \
  "SELECT k.id, k.name, u.username, u.role FROM mcp_api_keys k JOIN users u ON u.id = k.user_id
   WHERE k.scope = 'ops' AND k.is_active = 1 AND u.role != 'admin';"
```

PostgreSQL:

```bash
docker exec trinity-postgres psql -U trinity -d trinity -c \
  "SELECT k.id, k.name, u.username, u.role FROM mcp_api_keys k JOIN users u ON u.id = k.user_id
   WHERE k.scope = 'ops' AND k.is_active = 1 AND u.role != 'admin';"
```

No rows means nothing changes for you.

## What to do

1. **Mint a replacement under a current admin.** Sign in as an admin and create a key in
   Settings → MCP Keys with **Ops (read-only)**. Minting an `ops` key needs an admin's signed-in
   session; an API key cannot mint one. Prefer a dedicated service admin account that is not
   offboarded with a person.
2. **Put the new key in the consumer** in place of the old one.
3. **Revoke the old key.** An admin can revoke any key by id, from a signed-in session:
   `POST /api/mcp/keys/{key_id}/revoke`, with the id from the query above.

If the owner should hold the admin role again, promoting them restores their key instead. Do that
only if they should be an admin; it is not a way to keep a departed person's key alive.

## Rolling back

Redeploying the previous release restores the old behaviour. Nothing in the data changes.
