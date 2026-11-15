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

People (JWT, their own `user` key) and the system agent are never fenced by a
capability. A route that takes `require_person_or_capability` (the reconfigure
writes, the skill-gate writes of trinity-enterprise#753, and every
schedule-webhook route, trinity-enterprise#719) still refuses the system
agent's key, as `require_person` does.

## Capabilities

| Capability | Covers | Exempt |
|---|---|---|
| `skills.manage` | ent#596, unchanged | — |
| `schedules.manage` | create / update / delete / enable / disable a schedule; its webhook and webhook secret (mint, read, sign, unsign, revoke) | the agent's **own** schedules (#2996), but not their webhooks (trinity-enterprise#719: a webhook run is not gated by autonomy); `trigger` |
| `instructions.manage` | `CLAUDE.md`, `AGENTS.md`, `.claude/**` except `.claude/skills/**` through the file routes; `git/reset-to-main-preserve-state` | — |
| `agents.manage` | create a durable agent, delete, `deploy-local`, `systems/deploy`, `PUT /model`, and `read-only` / `resources` / `timeout` / `public-channel-model` / `guardrails` | spawning or discarding an **ephemeral** agent (ent#69) |
| `projects.manage` (ent#588) | start a Workspace project for its owner (owner = creator and first member, agent = steward, members-only) and import one of its own folder projects — MCP `create_project` / `import_project`; as any agent active on a project, REQUEST members and agents to add or remove and task owners, applied once when the agent's primary contact approves — its owner when none is assigned (ent#980, trinity-enterprise#1089, MCP `propose_project_changes`) — checked by the projects module before any read | working on a project it is already active on; adding or removing people or changing visibility directly is never granted — only requested |

Bounds on a holder:
- **Reach** (every capability): on every fenced route that names a target —
  the five reconfigure routes, the schedule writes, `PUT /model`, delete and
  `git/reset-to-main-preserve-state` — the target must be an agent its owner
  **owns** (`owner_username` equality, `enforce_agent_capability`'s `owner_bound`).
  The handlers authorise through `can_user_access_agent` / `can_user_share_agent`,
  which admit any admin, and an agent key carries its owner's role — so without
  this bound one grant on an admin-owned install reached every agent on the
  instance. Not-owned answers 404, like nonexistent; a non-holder's 403 comes
  first, so the 404 is never an existence oracle. The file routes apply the
  same bound, so an edge to another user's agent gives no write to its
  instructions or skills (trinity-enterprise#871): an edge means may call.
- **Never the system agent** (every capability, trinity-enterprise#871): it
  answers the same 404. An admin owns it, and what lands there runs under a key
  that bypasses every permission check, so one narrow grant would be all of them.
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
  │  GET /api/operator-queue?agent_name=&status=pending (open permission requests;
  │      every pending ask, walked past the first 500-row page — #3302)
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
                       audit: capability_grant | capability_revoke, details.capability
                       (every capability, every door; rows written before
                        ent#871 use skill_manager_grant | skill_manager_revoke
                        for skills.manage)

── the use ────────────────────────────────────────────────────────────────────
route-level   dependencies=[Depends(capability_fence(cap, ...))]   (dependencies.py)
              ├─ own_agent_exempt       (schedules: path agent == caller)
              └─ ephemeral_target_exempt (DELETE /api/agents/{name} of a ghost)
reconfigure   Depends(require_person_or_capability("agents.manage"[, self_person_only=True]))
              agent scope → the capability, then target owned by its owner
              (else 404); read-only / guardrails on itself → assert_person;
              anything else → assert_person
webhooks      Depends(require_person_or_capability("schedules.manage")) on all five
              schedule-webhook routes, the read included — no own-agent exemption;
              as reconfigure, with no self_person_only branch
create        _require_create_capability (routers/agents.py), before the
              idempotency claim; ghosts (config.ephemeral) skip it
file routes   services/agent_service/files._require_skill_capability
              ├─ _touches_skills_dir     → skills.manage
              └─ _touches_instructions   → instructions.manage
  ▼
enforce_agent_capability → capability_refusal(user, cap)
  person, user key, system key → pass, no row
  holder (agent key) → owner bound, where the route names a target
       not owned or nonexistent → the same 404
                                                  audit: capability_refused
                                                  (code capability_target_not_owned)
       owned or no bound        → pass
                                                  audit: capability_used
  else → 403 {code: <class>_management_not_permitted, capability,
                   message: "... raise an ask (type 'question') whose title includes
                   '<capability>' ... Settings → Permissions to change itself"}
                                                  audit: capability_refused
```

## The audit trail of a grant (trinity-enterprise#871)

Each row is `event_type=authorization`. The agent is the actor, the owner rides as
`actor_email`, and the row names `mcp_key_id`, `target_id`, `endpoint` and, in
`details`, the `capability` and the `method`.

| Call | Rows |
|---|---|
| A holder clears the fence | `capability_used` |
| A holder aims outside its owner's agents, or at the system agent | `capability_refused`, code `capability_target_not_owned`, and the same 404. On a file route with no edge, the route's access check answers first: a plain 403 and no row |
| A key without the grant | `capability_refused` |
| A key holding one of two capabilities a path needs | `capability_refused` only |
| A person, a `user` key, the system key, an event-loopback token | none: no grant decided the call |
| An agent on its own schedules, or discarding a ghost | none: no grant was used |
| One request passing the same capability twice (a metric declaration write) | one row |
| One file path needing two capabilities, both held | one row each |
| An agent create | `target_id` is the slug the agent would get, not the raw name in the body |
| A manifest deploy | `agents.manage` with no target, plus `skills.manage` per created member with gates, skills or sets |
| `GET .../skill-sets?probe=true`, when a holder's probe is honoured | `capability_used`: the probe runs inside the target |

**`capability_used` means the grant cleared the call, not that the change landed.**
A handler can still refuse afterwards: deleting a sibling the agent did not spawn
writes `capability_used` and answers 403, a body that fails validation answers
422, and a dry-run deploy changes nothing. Schedule writes and the timeout `PUT`
have no outcome row for any caller (item 3 of the issue, out of scope), so for
those the use row is the only trace and says nothing about the result. A files
write does: `file_write`, `file_delete` and `file_mkdir` (trinity-enterprise#820)
name the path and share `request_id` with the use row, as the agent `delete` row
now does. A join is only as good as that value: `X-Request-ID` is honoured from
the caller, and an agent can call the backend directly.

**Reads.** A fenced route writes a row whatever its method. A question answered by
a predicate writes none: `can_manage_agent_skills`, `person_or_capability_admits`,
`_may_reach_for_path`, and a holder reading a sibling's skill gate map through
`assert_skill_gates_readable`. A deploy that holds a gate or skill back, for want of
`skills.manage`, writes no refusal: the deploy succeeds and warns.

## Reading grants over MCP (trinity-enterprise#991)

`get_agent_capability_grants(agent_name)` (`src/mcp-server/src/tools/permissions.ts`)
reads `GET /api/agents/{name}/capability-grants` and returns it verbatim. The route's
scope is unchanged: an agent key reads itself or an agent it spawned, never an edge
peer; a person key reads only as owner or admin (ent#756). The access row is
`baselined` on that `OwnedAgentByName` fence, not `enforce`, because the MCP edge gate
would admit an edge peer the route refuses. A refusal (the uniform 404) comes back as an
error saying "refused, not 'no grants'". There is no write tool: granting stays an
admin's interactive action.

## What an agent may build from (trinity-enterprise#591)

`agents.manage` decides *whether* an agent may create a durable agent (and ghosts
need no grant); the instance's template-source allowlist decides *what from*, for
both. Requirement: `requirements/auth.md` §2.9a.

```
Settings → Agents → AgentCreationAllowlistPanel.vue
  GET  /api/settings/agent-creation-allowlist   (assert_admin)
  PUT  /api/settings/agent-creation-allowlist   (assert_interactive_admin; 422 invalid_template_sources)
        └─ routers/settings/templates.py → services/template_source_policy.py
              parse_entries → write_entries → system_settings['agent_creation_allowlist']
              describe(): entries, bounded, stored_invalid, not_bundled, hidden, in_use

POST /api/agents ─ _require_create_capability (agents.manage, durable only)
                 ─ template_source_policy.enforce(request, user, config)   ← before the idempotency claim
                 └─ create_agent_internal
                      ─ template_source_policy.enforce(...)                ← choke point, before the ghost pre-gates
                        (fork / copy / clone / system-deploy members all pass here)
POST /api/agents/deploy-local ─ capability_fence(agents.manage)
                              ─ enforce(..., upload=True)                  ← before the claim and the extraction
POST /api/systems/deploy ─ deploy_manifest: manifest_refusals() once
                              dry run → failed[] (403 per refused member, status invalid)
                              write   → 403 for the whole deploy, members[], before any create
GET /api/templates ─ annotate_templates(): allowed_for_agents per entry (bounded caller + list set)
```

- **Who is bounded**: every principal outside `{JWT, user, system}` (the
  `ADMIN_GATE_SCOPES` allowlist; an absent `mcp_scope` is bounded). People and
  `trinity-system` never read the row.
- **Matching** (`source_of` mirrors crud's `_parse_github_ref`): `local:<name>` only
  when the name is listed AND bundled (`_LOCAL_TEMPLATE_ROOTS[0]`); `github:o/*`;
  `github:o/r` (any ref); `github:o/r@ref` (only the ref the build will clone —
  `@ref`, else `source_branch`, else `main`; `fork_to_own`, a copy of `main` and a
  working-branch create, which all clone the default branch, match only unpinned
  entries). A create admitted only by a pin, with `source_mode` unset, is made
  source mode by `enforce` (`_pin_source_mode`), so ent#705's working-branch default
  cannot apply; an unpinned match wins when both match. Blank and unrecognised
  templates are refused.
- **Refusal**: 403 `template_source_not_allowed` (with `template` — canonical, never
  the raw string — and `allowed_sources`) or `template_upload_not_allowed`. One
  `template_source_refused` audit row per refused request: the agent as actor
  (`platform_audit_service.principal_actor`, shared with the capability audit), its
  key id, the sanitised slug as target (`None` for a ghost; its base name rides in
  `details.requested_name`).
- **Stored row**: no row or `[]` = unbounded; a non-list bounds agents to nothing;
  a list with bad entries keeps the valid ones; both set `stored_invalid`, and a
  refusal then says the list could not be read (`list_unreadable: true`). The
  generic `PUT`/`DELETE /api/settings/{key}` refuse the key. `hidden` names listed
  bundled templates marked `hidden: true` (creatable, but not in `GET /api/templates`).
- **Impact read**: `in_use` = distinct `trinity.template` labels of live agents with
  `spawned_by_agent` set (`db.list_agent_created_agent_names` + `list_all_agents_fast`),
  each allowed or refused under the saved list; `None` when unreadable.
- **Apply** (`POST /api/systems/apply`) is `require_person`, so agent keys never reach
  it; `test_apply_stays_person_only` names the blocker to add if that changes.
- **Limits**: create-time only (what a parent does to a child afterwards is governed
  by the grants above); a repo entry, pinned or not, is only as strong as who can
  push to the repo — a pin is a name, so push access can move a pinned branch or
  shadow a pinned tag with a same-named branch (resolved first). Protect the pinned
  name on GitHub.
- **Tests**: `tests/unit/test_ent591_template_source_bound.py`,
  `src/frontend/tests/unit/agentCreationAllowlistPanel.mount.spec.js`.

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
  DB-stored voice prompt (`PUT /{name}/voice/prompt`).
- **Other owner-tier writes** stay agent-reachable without a grant:
  those the route census lists as agent-callable or still holds in its frozen
  baseline (`tests/unit/_route_census.py`). A route that takes a person
  (trinity-enterprise#719) is lifted by no grant, `agents.manage` included.
- **Calibration** is checked at grant time, for the holder only: a ready
  holder can still rewrite a calibrating sibling's `CLAUDE.md`, and a grant
  made before the calibrating stamp persists.
- **An agent's own settings and git switches take a person, and no grant
  reaches them** (trinity-enterprise#719, reviewed, not overlooked):
  `circuit-breaker`, `label`, `mcp-exposed`, `operator-resume`,
  `voice-replies`, `git/auto-sync`, `git/freeze-schedules-if-failing` and
  `github-pat` are plain `require_person`, with no
  `require_person_or_capability` path. An orchestrator holding
  `agents.manage` therefore cannot reconfigure them on a sibling either: a
  guardrail an agent can lift is not a guardrail, and the sibling case was
  given up with it. Before ent#719 these routes took an agent key with no grant
  at all.

## Tests

- `tests/unit/test_ent871_capability_used_audit.py`: every audit-trail row above. The route rows go through the real routers with real keys and are read back from `audit_log`; `projects.manage` is a direct `enforce_agent_capability` call, and the manifest deploy records the audit sink.
- `tests/unit/test_ent164_self_change_grants.py`: the fences, read off FastAPI's dependant graph; behavior for each capability class; the ghost and own-schedule exemptions; the calibrating refusal.
- `tests/unit/test_2996_owner_config_person_only.py`: real keys against the real grant table, plus the grant route.
- `tests/unit/test_2996_human_only_routes.py`: the `person_or_grant` census class.
- `src/frontend/tests/unit/selfChangePermissionsPanel.spec.js` (mounted, ent#756): all-off fresh agent, an admin's grant reaching the PUT, the owner's disabled view, a refusal on its own toggle, LoadFailed, nothing for a non-owner, autonomy beside the four, the permission-request list and its title match.
