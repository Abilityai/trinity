# Agent MCP Key — Detection, Self-Heal & Rotation (#1854)

The `scope='agent'` key an agent container presents to the Trinity MCP server is
what makes the `agent_permissions` matrix apply — every agent-to-agent gate in
the MCP server is `scope === "agent"`-conditional. A container carrying a
**user-scoped** key therefore operates with the *owner's* identity and bypasses
the matrix silently.

Before this slice there was no way to see which key an agent used, no way to mint
or rotate one, and nothing that reconciled a container's configuration back to
what the platform believed. This ships three reinforcing pieces in the order that
matters: **detect → self-heal → rotate**.

## Layers

| Layer | File | Notes |
|-------|------|-------|
| Router | `src/backend/routers/agent_mcp_key.py` | `/api/agents/{name}/mcp-key*`; thin, all logic in the service. Named `mcp-key` **not** `platform-key` — "platform key" already means the Anthropic key (`agent_ownership.use_platform_api_key`, `api-key-setting`) |
| Service | `src/backend/services/agent_mcp_key_service.py` | status/health, the in-container probe + verdict interpretation, the rotation orchestration, and `heal_agent_mcp_key_env` for the drift path |
| Drift predicate | `src/backend/services/agent_service/helpers.py::check_agent_mcp_key_matches` | ninth `check_*_matches` composed into `start_agent_internal`'s `needs_recreation` |
| Delivery seam | `src/backend/services/agent_service/lifecycle.py` | `env_overrides` kwarg on `recreate_container_with_updated_config`, applied **last** |
| DB | `src/backend/db/mcp_keys.py`, `db/agents.py`, `db/schedules/stats.py` | `list_active_agent_key_ids`, `delete_superseded_agent_keys`, `find_mcp_key_by_hash`, `hash_mcp_api_key`, `reconcile_spawn_key_id`, `get_agent_last_execution_at`; facade delegators on `database.py` |
| Auth | `src/backend/dependencies.py` | `User.mcp_scope` + `reject_non_interactive_principal` (allowlist) |
| Models | `src/backend/models.py` | `AgentMcpKeyStatus` / `…VerifyEntry` / `…VerifyResult` / `…RegenerateResult` (Invariant #14) |
| UI | `components/AgentMcpKeyPanel.vue` | additive section in `settings/SettingsPanel.vue`. **Zero lines of `McpKeysTab.vue`** (#1848 collision avoidance) |
| MCP server | `src/mcp-server/src/types.ts` | `scope` union widened with `portal_delegate` (Invariant #13) |

**No files under `docker/base-image/**`.** That is a design property, not an
accident: the delivery mechanism is the container env plus the agent server's
*existing* `inject_trinity_mcp_if_configured`, so this works on every already
deployed agent with no base-image rebuild and no old-image matrix.

## Endpoints

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| GET | `/api/agents/{name}/mcp-key` | `OwnedAgentByName` + `reject_non_interactive_principal` | Metadata + health. Never the secret |
| POST | `/api/agents/{name}/mcp-key/verify` | same, + rate limit | Container config-truth probe (one `docker exec`) |
| POST | `/api/agents/{name}/mcp-key/regenerate` | same, + per-agent and per-actor rate limits | Rotate + deliver; returns metadata only |

`OwnedAgentByName` gives the uniform 404 for both "no such agent" and "not
yours" (Invariant #8 / #186) — no 404-then-403 oracle.

## Why the incident persisted (three live causes)

The agent server re-injects the `trinity` entry on **every** start and overwrites
it unconditionally — but only *after* an early return that requires **both**
`TRINITY_MCP_URL` and `TRINITY_MCP_API_KEY`. Combined with a `try/except`-swallowed
mint at creation (which sets both, or neither), that yields:

- **(b) key absent from env** — injection never runs, so a hand-written entry
  survives forever.
- **(c) the container was simply never restarted** — chat traffic does not
  restart a container.
- **(d) a shadow entry** — a Trinity-pointing server under any name other than
  the literal `trinity`. Injection never touches it, and **rotating the key does
  not fix it**. Any credential-only remedy is blind to this one.

Rather than settle which by archaeology, the probe resolves it empirically and
catches all three by construction.

## 1. Detect — container config-truth probe

`verify_agent_mcp_key` runs ONE `docker exec` (`execute_command_in_container`,
the same primitive behind `git_service` / `ssh_service` /
`session_cleanup_service`) with a base64-injected Python script (the
`services/compatibility/collector.py` pattern).

**The script emits only digests.** It reads `~/.mcp.json`, pulls each entry's
`Authorization: Bearer` value, and returns `sha256(token)` — the token and the
file body never cross the container boundary. `mcp_api_keys.key_hash` is a plain
unsalted SHA-256, so the backend joins on the digest directly. Server names are
returned (a `shadow_entry` verdict is meaningless without one) but truncated, and
never logged or audited: they are agent-authored strings.

| Verdict | Meaning |
|---|---|
| `ok` | digest == this agent's active `scope='agent'` key |
| `foreign_user_key` | digest matches a user/system/portal row → *"this agent authenticates as user X — the permissions matrix is not in effect"* |
| `foreign_agent_key` | digest matches another agent's key |
| `unknown_key` | no matching row (or a matching but inactive one) |
| `not_configured` | no Trinity entry at all |
| `shadow_entry` | a second Trinity-pointing entry under a non-`trinity` name — **outranks the others**, because rotation cannot fix it |
| `unavailable` | container stopped / exec failed — degrade, never 500 |

A separate explicit route, not folded into the GET: a `docker exec` per panel
load would be slow and heavy. The panel probes once on mount and degrades
gracefully (the `CompatibilityPanel` idiom).

## 2. Self-heal — start-time drift predicate

`check_agent_mcp_key_matches(container, agent_name)` joins the eight existing
`check_*_matches` predicates. Drift when `TRINITY_MCP_API_KEY` **or**
`TRINITY_MCP_URL` is absent, or `sha256(env value)` matches no active
`scope='agent'` row for this agent. The env plaintext is hashed and discarded,
never logged.

On drift, `start_agent_internal` calls `heal_agent_mcp_key_env` (mint + reconcile
+ prune) and passes the result as `env_overrides` to the recreate.

- **Same lock as rotation, and inert without it.** The heal runs the identical
  capture→mint→DELETE sequence, so it takes the same
  `agent:mcp_key_regen:{name}` lock and does **nothing at all** when it cannot
  hold it. "The predicate only fires when no active row matches the container,
  so every active row is already unusable by it" is true at *predicate* time,
  but the DELETE runs after a mint — and there is no per-agent start lock, so
  two concurrent starts both see the drift. If the second captures after the
  first's mint but before its delete, it deletes the key the first is about to
  bake in, and whichever container survives the `containers.run` name-conflict
  adoption is left 401-ing the heartbeat, the result callback, the pull worker
  and the MCP client. A mint taken outside the lock can always be
  captured-and-deleted by the holder, so capture→mint→delete is atomic as a
  unit: no lock, no heal. Failure semantics differ from rotation on purpose —
  an owner asked for a rotation, so refusing it is loud (503/409); nobody asked
  for a heal, so refusing it is silent and inert (no mint, no delete, the
  recreate proceeds on the container's existing env, the next start retries).
- **Audited.** The heal mints and deletes credentials fleet-wide with no human
  in the loop, so it writes an `agent_key_self_heal` `mcp_operation` row
  (metadata only, actor `system`) — an INFO log is not an audit trail for a
  feature whose whole premise is that the platform could not say what its
  agents authenticate with.
- **Exemptions**: `trinity-system` (its key is `scope='system'`, so the predicate
  could never be satisfied and the recreate would mint an agent-scoped
  replacement — an irreversible privilege downgrade of the orchestrator, #1816)
  and ephemeral ghosts (volume-less by design, so a recreate destroys the
  workspace mid-budget, including `~/.trinity/pending-results/`).
- **Loop safety**: a successful mint satisfies the predicate on the next
  evaluation, so it converges in one pass — the same contract as the
  guardrails/PAT/auth-token matchers. A *failed* mint leaves it unsatisfied, but
  `start_agent_internal` runs on an explicit start, never on a timer, so the
  worst case is one extra recreate per manual start.
- **Fail-safe**: any exception reads as "no drift". A transient DB blip must not
  turn every start in the fleet into a container recreate.

This is the highest-leverage piece: a button repairs one agent when a human
notices; a predicate repairs the fleet whether or not anyone notices.

## 3. Rotate — ordering contract

```
1. refuse trinity-system / ephemeral ghost (409)   ← BEFORE any mutation
2. acquire agent:mcp_key_regen:{name}              ← FAIL-CLOSED (503)
3. capture the active scope='agent' id set         ← BEFORE the mint
4. mint (is_active=1 explicit)
5. reconcile spawned_by_key_id                     ← BEFORE delivery
6. deliver: running → clear_agent_breakers + recreate + post-condition
            stopped → DB-only
7. DELETE the captured superseded ids              ← not "everything else"
8. audit; return metadata (NO plaintext)
```

Each step earns its place:

- **Mint-first.** The heartbeat, the #1083 result callback, the #1081 pull worker
  and the runtime's MCP client all read `TRINITY_MCP_API_KEY` from **process
  env**, which a DB write cannot change. Revoking first 401s all four.
- **Fail-closed lock.** The fail-open idiom used for agent-data export guards an
  idempotent export; this guards a destructive credential swap. Two interleaved
  rotations under a failed-open lock end at *"the container holds K1 while the
  only active row is K2"* — permanently 401-ing four subsystems with the
  surviving plaintext unrecoverable (#1644: *"a guard that fails open
  manufactures confidence"*). Redis unreachable ⇒ 503; contention ⇒ 409.
- **Captured ids, not `id != new_id`.** There is no per-agent start lock, so a
  concurrent `recreate_missing_container` can mint K3 mid-flight; the negation
  form would delete the key actually baked into the live container.
- **DELETE, not deactivate.** `recover_agent_ownership` reactivates every
  inactive per-agent row, so a deactivated rotated-out key comes back alive after
  a soft-delete/recover cycle. Deletion also drops the `key_hash` of a credential
  just declared compromised.
- **`scope='agent'` only.** `deactivate_agent_mcp_keys` / `set_agent_keys_active`
  span `('agent','connector')` — reusing either would silently revoke the owner's
  MCP **connector** key.
- **Reconcile before delivery.** `enforce_agent_spawn_scope` compares
  newest-active-key vs the child's stored `spawned_by_key_id`, so the instant the
  mint commits every child is 403 until reconciled. Leaving it until after
  delivery opens that window across the whole recreate, and a crash mid-flight
  makes it permanent. The predicate is `!= :current_id`, never `= :old_id` — the
  latter is a no-op the moment the mint commits, and cannot repair a child
  stranded by an earlier crashed rotation (#1811 saw 50 accumulated rows).
- **`clear_agent_breakers` before the recreate.** Both breakers and the heartbeat
  markers are keyed by agent NAME, so the replacement inherits its predecessor's
  verdict and is fast-failed without ever being contacted (#1560) — read as *"the
  rotation broke my agent"*.
- **409-adoption post-condition.** On a name conflict the recreate ADOPTS a
  container someone else created, with someone else's env. The live container is
  reloaded and its `TRINITY_MCP_API_KEY` compared **in full, constant-time**
  against the plaintext just minted; otherwise nothing is deleted and the call
  fails. Not a `key_prefix` test — the prefix is `api_key[:20]`, of which 12
  characters are the literal `trinity_mcp_`, so that would be an
  eight-character assertion standing between a stranger's container and the
  DELETE of every superseded key. The plaintext is already in the frame.
- **DB-only for a stopped agent.** `recreate_container_with_updated_config` ends
  at `containers_run(..., detach=True)`, which CREATES AND STARTS. Stopping an
  agent is the standard containment response to a suspected compromise and
  rotating its key is the very next step — silently booting it would resume
  schedules, autonomy and spend. The drift predicate bakes the key on its next
  deliberate start.

### Delivery payload — all three vars, not just the key

```python
{"TRINITY_MCP_API_KEY": <new>, "TRINITY_MCP_URL": …, "TRINITY_BACKEND_URL": …}
```

Non-negotiable: the agent-server injection early-returns unless URL **and** key
are both set, and `crud._apply_mcp_and_auth_env` sets all three together inside
one `if agent_mcp_key:`, so a swallowed mint drops all three. Baking only the key
leaves injection still short-circuiting and `.mcp.json` untouched *while the
heartbeat starts working* — a partial success that reads as a fix, failing on
exactly the population the feature exists to repair.

`env_overrides` is applied **last**, immediately before the container handoff:
roughly twenty derived mutations sit between the `Config.Env` copy and that point
(subscription juggling, PAT, guardrails, stall limit,
`TRINITY_AGENT_AUTH_TOKEN`, the pull-mode pop+update).

### Honest failure semantics

The old container is stopped and **removed** before the replacement is created,
so a post-removal failure leaves the agent with no container. The 500 says
exactly that — *"the container was replaced and failed to start; the agent is
down — press Start to rebuild"* — and claims no continuity. Superseded keys are
deliberately **not** deleted on that path: the drift predicate and
`recreate_missing_container` heal on the next start. The router never echoes a
raw exception.

## Auth

- Path auth `OwnedAgentByName` (uniform 404).
- `reject_non_interactive_principal` is an **allowlist** on `User.mcp_scope is
  None`. The older two-guard pair is a denylist over a five-value free-text
  column: for `scope='system'` both `agent_name` and `connector_agent` are
  `None`, so `reject_agent_principal` and `_reject_connector_principal` are both
  no-ops — while the principal still resolves to an owner who, on a default
  admin-owned install, owns the whole fleet.
- Rate limits per agent and per actor (`services/rate_limiter.enforce`): for an
  admin, ownership is fleet-wide, so an unthrottled rotate loop is a scripted
  fleet-wide container-recreate storm.
- **Audit** (`AuditEventType.MCP_OPERATION`, success and failure) records
  `{new_key_id, new_key_prefix, superseded_key_ids, superseded_deleted, delivery,
  children_repointed}` plus the actor's `mcp_scope`. **Never** the plaintext, the
  key hash, `env_vars`, the probe output, or a raw exception string —
  `audit_log` is append-only with a 365-day no-delete trigger and `details` is
  `json.dumps`'d unsanitised, so anything written there is permanent.
- **Returns no plaintext at all.** The agent-key plaintext has never been exposed
  over HTTP; unlike the connector key (whose consumer is a human), nobody outside
  the container has any use for it. Returning it would add a
  credential-exfiltration primitive on an owner-reachable route and buy nothing.

## Health signal

| State | Predicate | Tone |
|---|---|---|
| `missing` | no active `scope='agent'` row | Warning |
| `env_absent` | the container env has no `TRINITY_MCP_API_KEY`/`TRINITY_MCP_URL` | Warning |
| `env_mismatch` | the env key matches no active `scope='agent'` row for this agent | Warning |
| `never_used` | `last_used_at IS NULL` | Neutral, escalates on a non-`ok` probe verdict |
| `stale` | `last_used_at` older than the agent's most recent execution by > 7d | Warning |
| `active` | recently used | Info |
| `exempt` | `trinity-system` | Info |

The two `env_*` states **outrank** every usage-derived state. A key row that
exists and was used last week says nothing about what the container is running
with *now*, and reporting `active` over an env carrying no key at all is exactly
the green-during-the-incident failure this feature exists to remove. They cost a
Docker **inspect**, not the `verify` docker **exec** — so the panel is honest on
load rather than only after a click — and the match verdict is delegated to
`check_agent_mcp_key_matches` so the panel and the start-time recreate decision
can never disagree about what "recognized" means. Fail-soft: any error (Docker
down, no container) falls back to the usage-derived state rather than claiming
drift it could not observe. Only `verify` can see a hand-edited `.mcp.json` whose
entry disagrees with the env, which is why both exist.

`stale` is load-bearing. The motivating incident's signature is verbatim *"the
agent-scoped key sat unused for months"* — non-NULL but old — which a binary
used/unused predicate renders green. `last_used_at` is a genuine signal precisely
because the high-frequency agent paths validate with `track_usage=False`
(heartbeat, result callback, internal), so it tracks real MCP tool use.

`trinity-system` reports `exempt`, not `missing`: `get_agent_mcp_api_key` filters
`scope='agent'` and the orchestrator's key is `scope='system'`, so any other
branch would show a permanent false warning on the platform orchestrator — the
fastest way to train operators to ignore the signal.

## Adjacent guards fixed here (FR-7)

`db.revoke_mcp_api_key` / `db.delete_mcp_api_key` skip the ownership check
entirely for admins, and an agent key resolves to its owner carrying the owner's
role — so on a default admin-owned install any agent key could delete **every**
MCP key in the instance. `POST /connector/key` likewise returned a sibling's
plaintext to an agent principal. All three now run `reject_agent_principal` +
`_reject_connector_principal`.

## Schema

**No change.** `agent_name`, `scope` and `spawned_by_key_id` all exist, health is
derived, and `User.mcp_scope` is a Pydantic field — no `db/migrations.py` entry,
no Alembic revision.

## Still exploitable / deferred

- A user-scoped key in an agent's `.mcp.json` still authenticates as the owner.
  This **detects and repairs**; it does not **prevent**.
- Rotation does not revoke the foreign key — the probe surfaces its prefix so the
  owner can revoke it in Settings → MCP Keys.
- **Part 2b (request-origin attribution)** is genuinely unavailable: the MCP
  server forwards no origin marker, and client IP cannot substitute (port 8080 is
  published on all interfaces, and the frontend also lives on
  `trinity-agent-network`). Best candidate is the #1159 derived
  `X-Trinity-Agent-Token`. Explicitly **not** the "unpublished second listener"
  trick — that is a self-declared origin, bypassed by editing the same
  `.mcp.json` one line above the header.
- **Part 3 (enforcement flag)** is downstream of 2b and must be an explicit
  **allowlist over all five scopes**; "reject non-agent" breaks the system agent,
  the connector and portal_delegate.
- `.gitignore` does not untrack an already-committed file, so an auto-sync `git
  pull` can restore a bad `.mcp.json` after startup. `git rm --cached` on the
  repair path is a follow-up.

## Tests

`tests/unit/test_1854_agent_mcp_key.py` (35 tests, written red-first).
