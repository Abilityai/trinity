# Template-declared library skills — `library_skills:` in template.yaml (#4387)

Requirements: [skills.md §21.3.4](../requirements/skills.md). Architecture:
[agent-lifecycle.md](../architecture/agent-lifecycle.md#template-declared-library-skills-4387-requirements-2134).

## Overview

A template names skills from the platform skills library that an agent created
from it should hold:

```yaml
library_skills:
  - user-research
  - research-planning
```

At creation each name the library has is assigned to the new agent, as an
ordinary assignment, and delivered once the agent's server answers. Before
this, `template.yaml` `skills:` was documentation only, so a template built
around a library source came up without its core skills and the owner assigned
them by hand. Bundling copies into the template instead freezes them and cuts
them off from library updates.

## Flow

```
── reading the declaration (catalog + create) ──────────────────────────────────
template.yaml library_skills:
  ▼
services/template_skills.normalize_declared_library_skills      total, never raises
  │  list of names only; skill_packaging.SKILL_NAME_RE charset; de-duplicated
  │  in order; capped at 50; `set:<name>` refused with a named error
  │  library_skill_errors(block) → the named errors
  ├─► template_service catalog builders (local + github)
  │     entry.library_skills, entry.library_skill_errors
  └─► crud._resolve_template → _TemplateResolution.declared_library_skills
        local:   tr.template_data["library_skills"]
        github:  source_metadata["library_skills"]   (template_data is {} here)
        copy:    _declared_library_skills_for_snapshot(staged tree)

── creation (crud.create_agent_internal, inside the rollback fence) ────────────
container started → _register_agent
  ▼
_permitted_library_skills(request, current_user, declared, config.name)
  │  dependencies.capability_refusal(current_user, "skills.manage")
  │    person / user key / system key ─► all names
  │        (an agent key the grant admits ─► all names + capability_used row)
  │    agent key without the grant, connector, unknown or absent scope
  │        ─► [] + log + capability_refused audit row
  │           (target = the new agent, details.library_skills = the names)
  │  never raises: a failure here costs the skills, never the creation
  ▼
_materialize_agent_files(..., declared_library_skills=<permitted>,
                         library_skills_assigned_by_agent=acting_agent_name(user))
  │  skipped for ephemeral (ghost) agents
  ▼
_assign_declared_library_skills
  │  skill_service.get_skill(name) ─ missing ─► skipped, logged
  │  db.assign_skill(agent, name, owner, assigned_by_agent=<creating agent>)
  ▼  (only when something was assigned)
_spawn_library_skill_delivery ─► asyncio task, strongly referenced
  ▼
_deliver_library_skills_when_ready
  │  lifecycle.wait_for_agent_ready(agent, timeout_s=900)
  │    not ready ─► log; the next start delivers (rows are stored)
  ▼
lifecycle.inject_assigned_skills(agent)   the start-path injection:
                                           set reconcile, gates, prune
```

## Why delivery is its own step

Creation starts the container directly, not through `start_agent_internal`, so
nothing else delivers assigned skills before the agent's next start. Delivering
at assignment time (as the assign route does) would only report
`not_delivered`, because the agent server is not up yet. The rows are written
at creation and the start-path injection runs once `/health` answers.

## Security

- **The fence.** Assigning skills is the `skills.manage` capability (ent#596).
  Without the fence at creation, an agent key could obtain library skills by
  creating an agent from a template that names them. All-or-none: a refused
  creator gets the agent with no library skills.
- **Audited like the route fence.** `capability_refused` on a refusal,
  `capability_used` for an agent key the grant admits, through
  `dependencies._audit_capability` (safe with `request=None`, since a system
  deploy creates without a request). The actor is the agent, not its owner
  (`principal_actor`, R29).
- **Attribution.** `assigned_by` is the owner; `assigned_by_agent` is the
  creating agent when an agent created it.
- **Names only.** The reader takes the library's own charset and refuses skill
  sets by name (sets have their own resolution rules, §21.3.2).

## After creation

The assignments are ordinary: shown on the Skills tab, unassignable there, and
subject to §21.3.3 peer grants. The declaration is read at creation only;
editing `template.yaml` later changes nothing on existing agents.

## Tests

- `tests/unit/test_4387_template_library_skills.py` — the reader, both catalog
  builders, all three resolver branches (copy snapshot included), the fence and
  its audit rows (including the real writer with `request=None`), assignment,
  ghost skip, non-fatal failures, readiness-gated delivery.
- `tests/unit/test_1484_create_agent_characterization.py` (`test_4387_*`) — a
  whole `create_agent_internal` run pins the call site: a person gets the
  names, an agent key without the grant gets none plus a refusal row, an agent
  key with the grant gets them attributed to itself.
