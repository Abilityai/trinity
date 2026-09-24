# /cso --diff — ent#530 skill sets (2026-09-24)

**Scope:** `feature/ent530-skill-sets` vs `dev`. Daily gate: 8/10.
**Result:** 0 critical, 0 high, 0 medium. 3 low findings, all fixed on the branch.

## Attack surface added
- `GET /api/skills/library/sets` (`get_current_user`). It returns metadata the skills listing already exposes: names, versions, problem codes and suggested schedules.
- `GET /api/agents/{agent_name}/skill-sets` (`get_authorized_agent_by_name`, uniform 404).
  - `?probe=true` runs one in-container credential-name check.
- `POST` / `DELETE /api/agents/{agent_name}/skill-sets/{set_name}` sit behind the ent#596 fence (`get_skill_managed_agent_by_name`). The capability is checked before the owner check, and a refusal gets one uniform 403.
- `PUT /api/agents/{agent_name}/skills` now accepts `sets` and `set:<name>` entries, behind the same fence.
- MCP gains `unassign_skill_set`, classified under the skill-manager fence. `assign_skill_to_agent` sends `set:` to the fenced route.
- Author-controlled `catalog.yaml` `sets:` goes through the existing hardened loader.
  - Set, member and env names are regex-restricted, so only those names reach CLAUDE.md, the injected meta and the probe (where they are `json.dumps`-embedded).
  - Parsing is bounded to 50 sets, 100 members, 10 schedules and 20 env keys.

## Phases
- **Secrets:** no key patterns in the diff.
- **Enterprise docs guard:** 0 hits.
- **Dependencies, CI and Docker:** unchanged.
- **Injection:** all SQL is parameterized SQLAlchemy, there is no `v-html`, and no catalog string reaches argv or a path.
- **Migrations:** `0073` exists on both tracks, is additive and idempotent, and leaves a single Alembic head. `agent_skill_sets` is registered as a CASCADE `AgentRef`.

## Fixed during the audit
- **L1:** the status read ran an in-container exec on every call. The probe is now opt-in (`?probe=true`); the Skills tab and the assign response use it, while MCP `get_agent_skills` and plain reads do not.
- **L2:** a `set:` entry in the bulk PUT replaced every held set, which could strip the skills of another set through automation. `set:` entries now only add sets (all-or-nothing), and the explicit `sets` field is the only full replace.
- **L3:** `tables.py` declared `agent_skill_sets.assigned_by` and `assigned_at` as nullable. They are now `NOT NULL`, matching both migration tracks.

## Data integrity (from the pre-landing review, fixed before this audit)
A crafted or broken catalog cannot strip skills. A held set drives no prune in any of these cases, and each one is mutation-tested:
- the set is invalid (a typo'd key, a bad or dropped member, or no members);
- the set is partial, which also covers an empty or half-checked-out skills root;
- the set is missing;
- the set's name now resolves to a different source.

## Appendix (below the gate)
- **Informational:** suggested-schedule `name` and `message` are catalog free text returned to callers. That is the same trust level as the SKILL.md content the library already injects.
- **Pre-existing, out of scope:** `tables.py` omits the `agent_skills` `UNIQUE(agent_name, skill_name)` that `schema.py` declares. It is recorded in `docs/memory/learnings/2026-09-24-tables-metadata-lacks-agent-skills-unique.md`.
