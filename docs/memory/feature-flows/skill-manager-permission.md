# Skill managers — only designated agents may change an agent's skills (trinity-enterprise#596)

## Overview

An agent-scoped MCP key resolves to its **owner**, carrying the owner's role
(Invariant #8). Every route that changes an agent's skills was fenced on
ownership, so any agent could rewrite the skills of every sibling its owner
holds — and since #2703 an assignment also writes the skill's executable files
into the target in the same call. Tandem makes that real: companions face whole
teams, and a prompt-injected companion holds a key.

The ruling (operator 2026-09-17, restated 2026-09-18): changing an agent's
skills — **another agent's or its own** — is its own permission. An instance
admin grants it to named agents (the fleet orchestrators); every other agent key
is refused, on a sibling and on itself. Call permission and this permission
imply each other in neither direction. People and the system agent are
unchanged.

## Flow

```
── the grant (a person at a screen) ───────────────────────────────────────────
admin → Settings → Agents → Skill managers (SkillManagersPanel.vue, stores/skillManagers.js)
  │  GET  /api/skills/managers                         require_admin
  │  PUT  /api/agents/{agent_name}/skill-manager {granted}
  │        require_admin + reject_non_interactive_principal   ← a key never grants
  ▼
services/capability_grant_service.set_grant
  │  revoke: always allowed (no orphan rows)
  │  grant:  get_agent_owner → 404 (nonexistent OR soft-deleted, uniform)
  │          is_system       → 422 system_agent_not_grantable
  │          is_ephemeral    → 422 ephemeral_agent_not_grantable
  ▼
db/capability_grants  agent_capability_grants(agent_name, 'skills.manage', granted_by, granted_at)
  │  idempotent — a repeat keeps the original who/when       audit: skill_manager_grant | _revoke

── the use (every route that changes an agent's skills) ────────────────────────
PUT    /api/agents/{n}/skills             ┐
POST   /api/agents/{n}/skills/inject      │  Depends(get_skill_managed_agent_by_name)
POST   /api/agents/{n}/skills/{skill}     │
DELETE /api/agents/{n}/skills/{skill}     ┘
  ▼
dependencies.get_skill_managed_agent_by_name
  │  1. enforce_agent_capability(skills.manage)           ← FIRST
  │       capability_refusal: mcp_scope
  │         None | user | system       → pass (humans, trinity-system — unchanged)
  │         agent + live grant row     → pass (self or sibling alike)
  │         agent, no grant            → 403 skill_management_not_permitted
  │         connector | ops | portal_delegate | <new scope> → 403
  │         (no mcp_scope attribute)   → 403 (fails closed)
  │       refusal → audit `capability_refused` (agent, key id, target) → 403
  │  2. get_owned_agent_by_name                           ← unchanged owner fence (uniform 404)
  ▼
handler — writes agent_skills with assigned_by = owner username,
          assigned_by_agent = acting_agent_name(current_user)
          (agent name | 'trinity-system' | NULL for a person)

── the side door, closed ───────────────────────────────────────────────────────
PUT    /api/agents/{n}/files?path=…/.claude/skills/**      ┐ services/agent_service/files.py
POST   /api/agents/{n}/files/mkdir  …/.claude/skills/**     │ _require_skill_capability
DELETE /api/agents/{n}/files        …/.claude/skills/** OR  │ → the same enforce_agent_capability
                                    an ancestor (.claude, ~) ┘
```

## Why it is shaped this way

- **Backend, not MCP.** MCP proxies to these routes, so a backend fence covers
  every MCP tool *and* a direct call with the agent's own key. The three MCP rows
  in `access.ts` point at the fence (`SKILL_MANAGER_FENCE`), the same shape as
  `ADMIN_ONLY` / `REMINDER_SELF_GATE`.
- **An allowlist.** `mcp_scope` is free text; "agent is refused" would let the
  next scope someone invents inherit the owner (the #2323 lesson). Pinned by a
  ten-row scope matrix including a scope invented tomorrow.
- **A composed dependency, capability first.** A "call it first in the handler"
  convention has no guard — the next route forgets it — and checking ownership
  first would answer a non-holder 404 for some targets and 403 for others, an
  existence oracle. The route table test reads FastAPI's real dependant graph.
- **The file routes too.** Both independent reviewers found it: `PUT /files`
  checked only access and `.claude/skills/**` was not in the write deny-list, so
  the same `SKILL.md` + `scripts/` landed in a sibling through a different URL,
  and the fence above would have been decorative. A delete of an ancestor counts,
  since it removes every skill. The shared deny-list (mirrored in the agent image
  and the guardrails baseline) is untouched, so people can still edit skills in
  the Files tab.
- **A grants table, not a column.** Who granted it and when is the question an
  incident review asks; a row answers it, and later capabilities (ent#590,
  ent#341) share the seam.
- **Default-deny on upgrade.** Nobody holds it after deploy — that is the ruling.
  The refusal names where the grant lives, and the empty panel says no agent can
  change skills and what to do.
- **Attribution that survives a replace.** The bulk PUT is delete-all + reinsert;
  without carrying who/when for kept names, an orchestrator's next replace would
  make a skill a person assigned last month read as the orchestrator's.

## Rollout

Orchestrators that apply a skill map (`/reconcile-skill-map` in the `abilities`
marketplace) are refused with `skill_management_not_permitted` until an admin
grants them. Grant `trinity-pm` / `corbin` in Settings → Agents → Skill managers
at or before deploy. ent#589's installer must assign through the orchestrator
rather than self-assign (operator ruling 2026-09-23, option 1).

## Known limits

An agent editing its own `~/.claude/skills` from inside its container (no
platform gate reaches there); a git pull into a sibling; chat-driven
self-modification; the other owner-equivalent routes of ent#629.

## Files

| Layer | File | Role |
|---|---|---|
| DB | `db/capability_grants.py` · `db/schema.py` · `db/tables.py` · `db/migrations.py` (`agent_capability_grants`) · `migrations/versions/0072_agent_capability_grants.py` · `db/agent_cleanup.py` (CASCADE) · `db/skills.py` (`assigned_by_agent`, kept-name carry) · `database.py` facade | grants + attribution, both tracks |
| Gate | `dependencies.py` (`capability_refusal`, `enforce_agent_capability`, `acting_agent_name`, `get_skill_managed_agent_by_name`, `SkillManagedAgentByName`) | the fence |
| Service | `services/capability_grant_service.py` | who may receive a grant |
| Routes | `routers/skills.py` (four write routes fenced; `GET /skills/managers`; `PUT /agents/{n}/skill-manager`; `# mcp:` header) · `models.py` (`SkillManager*`) | use + grant |
| Side door | `services/agent_service/files.py` (`_touches_skills_dir`, `_require_skill_capability`) | `.claude/skills/**` via `/files` |
| MCP | `src/mcp-server/src/access.ts` (`SKILL_MANAGER_FENCE`) · `tools/skills.ts` (descriptions) | three-surface sync |
| UI | `components/SkillManagersPanel.vue` · `stores/skillManagers.js` · `views/Settings.vue` | the grant surface |
| Tests | `tests/unit/test_ent596_skill_manager.py` (65) · `src/frontend/tests/unit/skillManagersPanel.spec.js` (7, mounted) · `src/mcp-server/src/access.test.ts` (+1) | |
