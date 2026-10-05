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
for it. Approving that ask does not grant anything: an admin grants it in the
agent's Settings (the toggles are ent#756).

People (JWT, their own `user` key) and the system agent are never fenced.

## Capabilities

| Capability | Covers | Exempt |
|---|---|---|
| `skills.manage` | ent#596, unchanged | — |
| `schedules.manage` | create / update / delete / enable / disable a schedule; its webhook and webhook secret | the agent's **own** schedules (#2996); `trigger` |
| `instructions.manage` | `CLAUDE.md`, `AGENTS.md`, `.claude/**` except `.claude/skills/**` through the file routes; `git/reset-to-main-preserve-state` | — |
| `agents.manage` | create a durable agent, delete, `deploy-local`, `systems/deploy`, `PUT /model`, and `read-only` / `resources` / `timeout` / `public-channel-model` / `guardrails` | spawning or discarding an **ephemeral** agent (ent#69) |

None of these grant autonomy, api-key-setting, capabilities, capacity or rename,
which stay with a person.

## Flow

```
── the grant (a person at a screen) ───────────────────────────────────────────
admin → agent Settings (ent#756)
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

── the use ────────────────────────────────────────────────────────────────────
route-level   dependencies=[Depends(capability_fence(cap, ...))]   (dependencies.py)
              ├─ own_agent_exempt       (schedules: path agent == caller)
              └─ ephemeral_target_exempt (DELETE /api/agents/{name} of a ghost)
reconfigure   Depends(require_person_or_capability("agents.manage"))
              agent scope → the capability; anything else → assert_person
create        _require_create_capability (routers/agents.py), before the
              idempotency claim; ghosts (config.ephemeral) skip it
file routes   services/agent_service/files._require_skill_capability
              ├─ _touches_skills_dir     → skills.manage
              └─ _touches_instructions   → instructions.manage
  ▼
enforce_agent_capability → capability_refusal(user, cap)
  holder / human → pass
  else           → 403 {code: <class>_management_not_permitted, error: "... raise an ask
                   with ask_class 'permission-request' ... an admin grants it in the
                   agent's Settings"}            audit: capability_refused
```

## Upgrade effect

After deploy, no agent holds the three new capabilities. An orchestrating agent
that creates durable agents, writes another agent's schedules or instructions,
or reconfigures an agent is refused until an admin grants the permission.

## Known limit

The fence covers the platform's routes. An agent can still edit files inside its
own container with its own tools.

## Tests

- `tests/unit/test_ent164_self_change_grants.py`: the fences, read off FastAPI's dependant graph; behavior for each capability class; the ghost and own-schedule exemptions; the calibrating refusal.
- `tests/unit/test_2996_owner_config_person_only.py`: real keys against the real grant table, plus the grant route.
- `tests/unit/test_2996_human_only_routes.py`: the `person_or_grant` census class.
