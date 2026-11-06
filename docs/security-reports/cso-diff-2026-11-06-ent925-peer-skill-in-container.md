# CSO Diff Audit — 2026-11-06 — abilityai/trinity-enterprise#925 (peer skill grants, in-container)

**Mode**: diff (all phases, diff-constrained) · **Confidence gate**: 8/10 · **Branch**: `feature/925-peer-skill-in-container` → `dev` · **Skill**: cso v1.2.3 · **Audited**: `8b59fdbb1..fea4fb05d`. Not a full run; no Docker operation.

## Result

**0 findings** (critical 0 · high 0 · medium 0 · low 0). Phases 3, 4, 5 and 8 have nothing in the diff (no dependency, workflow, image, compose, vendored-policy or agent-shipped file changed).

## Architecture (Phase 0)

The ent#752 hook asks `POST /api/skill-gate/check` before every `Skill` load and subagent preload, with the run's id from the claude process's launch environment. `check_invocation` now runs a peer step before the gate logic: it reads the run's row (this agent's, any status), resolves who asked (the agent of an agent-scoped key first, then the stamped source agent), and refuses a library skill that peer holds no grant for. The gateway (`peer_skill_policy.enforce`) uses the same requester rule. A new manager-only read feeds one line in the Permissions panel when the agent has no intact hook.

Trust boundaries crossed: agent → backend (existing self-only route, new verdict), platform text → the callee's model (the refusal), person → backend (new read).

## Attack surface (Phase 1, diff-scoped)

| Surface | Change |
|---|---|
| `GET /api/agents/{agent_name}/peer-skills/enforcement` | New. `AuthorizedAgentByName` (uniform 404); answers only `is_person_principal` + `can_manage_agent_skills`, nulls otherwise; classified in the #2996 census and the ent#926 tier table |
| `POST /api/skill-gate/check` | New peer step; still self-only (`get_self_agent`) |
| Gateway `enforce` | Requester read key-first; fan-out-without-header and A2A agent-key requests now count as a peer's |
| `GET /api/skills/peer-grants/drift` | Admin; also reads in-container refusal audit rows |

## Checks run

- Phase 2: added lines scanned for AKIA / sk- / ghp_ / gho_ / github_pat_ / xox / re_ prefixes — none; no system_settings writer; no new subprocess/docker exec
- Phase 6: no raw X-Source-Agent reader added; the requester is now the agent of an agent-scoped key FIRST (key ids come from the authenticated principal or from rows the platform wrote from it, never a header), then the resolved source agent — stricter than before at the gateway
- Phase 6: /api/skill-gate/check stays self-only (get_self_agent); an execution id of another agent's run or an unknown id answers alike with one row read
- Phase 7 ASI03: new GET classified in the #2996 census and the ent#926 tier table; an agent key gets nulls and causes no /health probe
- Phase 7 ASI07: attribution tightened — fan-out without X-Source-Agent and A2A with an agent key were read as the callee itself / a person and skipped the ent#662 peer rule; now a peer at both layers (tests per producer row shape)
- Phase 7 ASI01: refusal text carries platform-sourced names only; no slash before a name
- Phase 9 A01: route behind the uniform-404 access dependency; the answer withheld unless is_person_principal AND can_manage_agent_skills
- Phase 9 A05: SQLAlchemy Core only, parameterized; audit details parsed with json.loads under try
- Phase 9 A10: failure direction stated per guard — in-container peer step fails OPEN on SQLAlchemyError only (owner ruling, docstring), other errors propagate; gateway: a known peer still refuses 503 on an unreadable read, a request never identified as a peer's passes as before; the gate's clearance keeps its own fail-closed reads and marker
- Invisible-Unicode check over all added lines (Python re, exact ranges) — none
- Frontend: text interpolation only, no v-html

## Considered and dismissed

- **Fail-open peer step lets a peer's run load a restricted skill during a database outage** — stated owner ruling (an outage must not block own/scheduled/person turns, which the hook cannot tell apart without the platform); the database error is not attacker-inducible through the check's inputs (bounded id, platform-written key ids)
- **could_reach reads fleet-wide edges and subscriptions** — returns one boolean to a person who manages the agent; inbound edges and subscriptions concern that agent; no names are returned
- **Copying a library skill under another name / acting on a SKILL.md it read / nested CLI / self-dispatch from a peer turn** — documented limits of the hook's non-adversarial threat model (adversarial-executor class), stated in requirements and security.md; follow-ups drafted
- **In-container audit rows name the peer as actor while the callee's key made the call** — deliberate: actor = whose request was refused (gateway parity); mcp_key_id = the calling key; the requester's key is in details

## Hypotheses

None.

## Coverage gaps

- No image-internal checks (the diff touches no image). gitleaks and zizmor were not run; the added lines were scanned by regex for credential prefixes and by exact ranges for invisible Unicode.

## Trend

Prior: `cso-diff-2026-10-05-ent752-gated-skill-hook`. Closes the ent#662 "Known gap" (prose requests) on runtimes with the hook, and the gateway attribution gap (fan-out without header, A2A agent key) found while planning this change.
