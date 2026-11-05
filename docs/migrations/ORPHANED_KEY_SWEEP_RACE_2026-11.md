# A just-minted agent key could be revoked by the cleanup sweep — operator note (#4259, 2026-11)

**Action required (existing instances only):** if any agent created before this fix answers every
MCP call with `401` (`chat_with_agent unavailable`, `Invalid API key`), its key was most likely
revoked by the race below. Find such keys with the query here and reactivate them. No database
migration runs, and nothing is reactivated automatically.

## What happened

`create_agent` mints the agent-scoped MCP key **before** the container exists and writes the
`agent_ownership` row only **after** the container is created. The cleanup sweep (`#1745`, every
5 minutes) revokes every agent-scoped key whose agent has no live ownership row. A sweep tick
landing inside that start-up window — a few seconds per agent — revoked the key of a healthy agent
one second after it was minted, and nothing repaired it afterwards. On an instance that creates
agents from a hook (onboarding) this was a coin flip per person.

## What changes

- The sweep skips keys younger than 10 minutes (`ORPHANED_KEY_GRACE_SECONDS`). A key whose create
  attempt failed and was not rolled back is still revoked on the first tick after the window.
- A key whose agent row exists and is not soft-deleted is never treated as orphaned, whatever the
  container state (creating, starting, stopped) — unchanged, now pinned by a test.
- The `[Cleanup] Deactivated MCP key(s) of N agent(s) that are no longer live (#1745): a, b` line
  names the agents, so a revocation is visible in the backend log.

## Why there is no automatic repair

`mcp_api_keys` records neither *when* nor *by whom* a key was deactivated, so a key revoked by the
race is indistinguishable in the database from one an operator revoked on purpose. A blind
reactivation would resurrect deliberate revocations (the same caveat already recorded in
`test_1745_agent_key_revocation.py::test_recover_does_not_resurrect_a_manually_revoked_key`).

## How to find them

Candidates are inactive agent-scoped keys whose agent row is live and has **no other active key**,
minted within about a minute of the agent row (`created_at` on both tables is ISO-Z text, so the
two timestamps compare directly):

```sql
SELECT k.id, k.agent_name, k.created_at AS key_created, o.created_at AS agent_created
FROM mcp_api_keys k
JOIN agent_ownership o ON o.agent_name = k.agent_name AND o.deleted_at IS NULL
WHERE k.scope IN ('agent', 'connector')
  AND k.is_active = 0
  AND NOT EXISTS (
        SELECT 1 FROM mcp_api_keys a
        WHERE a.agent_name = k.agent_name AND a.scope = k.scope AND a.is_active = 1)
ORDER BY k.agent_name;
```

Keep only the rows where `key_created` is at most a minute before `agent_created` (the create
window); a wider gap is some other revocation. Then, per key you have checked:

```sql
UPDATE mcp_api_keys SET is_active = 1 WHERE id = '<key id>';
```

The agent's container already holds the key as `TRINITY_MCP_API_KEY`, so no restart is needed.
Alternatively, recreate the agent, which mints a fresh key.

The backend log on the affected instance also names the moment: look for the
`[Cleanup] Deactivated ... MCP key(s)` line within seconds of the agent's `Created MCP API key for
agent <name>` line.
