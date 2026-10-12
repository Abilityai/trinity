# Self-change grants — an agent changes its own shape only with a grant (trinity-enterprise#164)

## Overview

An agent-scoped MCP key resolves to its **owner**, carrying the owner's role
(Invariant #8). The owner gates therefore let any agent rewrite any agent its
owner holds: its instructions, its schedules, its configuration, or the fleet
itself. ent#596 closed that for skills with a capability grant
([skill-manager-permission.md](skill-manager-permission.md)). This flow extends
the same seam to the rest of an agent's shape.

The 2026-10-02 re-scope ruled out per-action approval and pause-and-resume. An
agent either holds the grant and the call goes through, or it does not and the
call is **refused**. The refusal says which permission is missing and how to ask
for it (an ask of `type: question` whose title includes the permission id, e.g.
`schedules.manage`). Approving that ask does not grant anything: an instance
admin grants it from a signed-in session — in the agent's Settings →
**Permissions to change itself** (ent#756), or with
`PUT /api/agents/{agent}/capability-grants/{capability}`.

People (JWT, their own `user` key) and the system agent are never fenced.

## Capabilities

| Capability | Covers | Exempt |
|---|---|---|
| `skills.manage` | ent#596, unchanged | — |
| `schedules.manage` | create / update / delete / enable / disable a schedule; its webhook and webhook secret | the agent's **own** schedules (#2996); `trigger` |
| `instructions.manage` | `CLAUDE.md`, `AGENTS.md`, `.claude/**` except `.claude/skills/**` through the file routes; `git/reset-to-main-preserve-state` | — |
| `agents.manage` | create a durable agent, delete, `deploy-local`, `systems/deploy`, `PUT /model`, and `read-only` / `resources` / `timeout` / `public-channel-model` / `guardrails` | spawning or discarding an **ephemeral** agent (ent#69) |
| `projects.manage` (ent#588) | start a Workspace project for its owner (owner = creator and first member, agent = steward, members-only) and import one of its own folder projects — MCP `create_project` / `import_project`, checked by the projects module before any read | working on a project it is already active on; adding people or changing visibility is never granted |

Bounds on a holder:
- **Reach** (every capability): on every fenced route that names a target —
  the five reconfigure routes, the schedule writes, `PUT /model`, delete and
  `git/reset-to-main-preserve-state` — the target must be an agent its owner
  **owns** (`owner_username` equality, `dependencies._refuse_unless_owners_agent`).
  The handlers authorise through `can_user_access_agent` / `can_user_share_agent`,
  which admit any admin, and an agent key carries its owner's role — so without
  this bound one grant on an admin-owned install reached every agent on the
  instance. Not-owned answers 404, like nonexistent; a non-holder's 403 comes
  first, so the 404 is never an existence oracle.
- **Self**: a holder cannot change its **own** `read-only` mode or `guardrails`
  — those stay person-only when target == caller.
- **Delete** is still limited by `enforce_agent_spawn_scope` to agents the
  holder spawned; the grant is not "delete any agent".

None of these grant autonomy, api-key-setting, capabilities, capacity or rename,
which stay with a person.

## Flow

```
── the grant (a person at a screen) ───────────────────────────────────────────
admin → Agent Settings → Permissions to change itself (ent#756)
  │  components/settings/SelfChangePermissionsPanel.vue  over  stores/capabilityGrants.js
  │  copy + the permission-request title match: utils/capabilityGrants.js (pure)
  │  also reads GET /{agent}/autonomy (shown, never granted) and
  │  GET /api/operator-queue?agent_name=&status=pending (open permission requests)
  │  GET  /api/agents/{agent_name}/capability-grants          OwnedAgentByName
  │  PUT  /api/agents/{agent_name}/capability-grants/{cap} {granted}
  │        require_admin + reject_non_interactive_principal   ← a key never grants
  ▼
services/capability_grant_service.set_grant
  │  unknown cap            → 422 unknown_capability
  │  revoke                 → always allowed
  │  grant: nonexistent     → 404
  │         system          → 422 system_agent_not_grantable
  │         instructions.manage + readiness stamp 'calibrating'
  │                         → 422 calibrating_agent              (ent#663)
  │         ephemeral       → 422 ephemeral_agent_not_grantable
  ▼
db/capability_grants  agent_capability_grants(agent_name, capability, granted_by, granted_at)
                       audit: capability_grant | capability_revoke
                       (skills.manage: skill_manager_grant | skill_manager_revoke,
                        the same verb as PUT /api/skills/agents/{name}/skill-manager)

── the use ────────────────────────────────────────────────────────────────────
route-level   dependencies=[Depends(capability_fence(cap, ...))]   (dependencies.py)
              ├─ own_agent_exempt       (schedules: path agent == caller)
              └─ ephemeral_target_exempt (DELETE /api/agents/{name} of a ghost)
reconfigure   Depends(require_person_or_capability("agents.manage"[, self_person_only=True]))
              agent scope → the capability, then target owned by its owner
              (else 404); read-only / guardrails on itself → assert_person;
              anything else → assert_person
create        _require_create_capability (routers/agents.py), before the
              idempotency claim; ghosts (config.ephemeral) skip it
file routes   services/agent_service/files._require_skill_capability
              ├─ _touches_skills_dir     → skills.manage
              └─ _touches_instructions   → instructions.manage
  ▼
enforce_agent_capability → capability_refusal(user, cap)
  holder / human → pass
  else           → 403 {code: <class>_management_not_permitted, capability,
                   message: "... raise an ask (type 'question') whose title includes
                   '<capability>' ... Settings → Permissions to change itself"}
                                                  audit: capability_refused
```

## Upgrade effect

After deploy, no agent holds the three new capabilities. An orchestrating agent
that creates durable agents, writes another agent's schedules or instructions,
or reconfigures an agent is refused until an admin grants the permission.

## Known limits — out of scope for ent#164

The fence covers the platform routes listed above, and nothing else:

- An agent can still edit files inside its own container with its own tools.
- **Git**: `POST /git/pull` with `strategy=force_reset` and `POST /git/sync`
  with `pull_first` replace the workspace with the remote and are not fenced.
  Only `git/reset-to-main-preserve-state` takes `instructions.manage`.
- **Instruction writes outside the path list**: `CLAUDE.local.md`, the
  home-level `.claude.json`, a symlink inside the target workspace, and the
  DB-stored prompts (`PUT /{agent}/public-prompt`, `PUT /{name}/voice/prompt`).
- **Other owner-tier writes** stay agent-reachable without a grant:
  `circuit-breaker`, `mcp-exposed`, `folders`, `file-sharing`,
  `git/auto-sync`, `github-pat`, `access-policy`, and
  `git/freeze-schedules-if-failing`.
- **Calibration** is checked at grant time, for the holder only: a ready
  holder can still rewrite a calibrating sibling's `CLAUDE.md`, and a grant
  made before the calibrating stamp persists.

## Tests

- `tests/unit/test_ent164_self_change_grants.py`: the fences, read off FastAPI's dependant graph; behavior for each capability class; the ghost and own-schedule exemptions; the calibrating refusal.
- `tests/unit/test_2996_owner_config_person_only.py`: real keys against the real grant table, plus the grant route.
- `tests/unit/test_2996_human_only_routes.py`: the `person_or_grant` census class.
- `src/frontend/tests/unit/selfChangePermissionsPanel.spec.js` (mounted, ent#756): all-off fresh agent, an admin's grant reaching the PUT, the owner's disabled view, a refusal on its own toggle, LoadFailed, nothing for a non-owner, autonomy beside the four, the permission-request list and its title match.
