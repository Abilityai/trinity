# CSO diff audit — trinity-enterprise#584 capability descriptions

- **Version / mode:** cso 1.2.3, `--diff`, daily gate (8/10)
- **Scope:** `feature/584-capability-descriptions` vs merge-base `11101139d`
- **Changed:** `services/skill_packaging.py` (the one blank rule `clean_description`; frontmatter framing anchored like the agent server's), `services/connector_service.py`, `services/agent_skills_listing.py`, `services/skill_service.py` (no description guessed from the body), `services/compatibility/{spec,static_checks}.py` (T-011 retired, X-010 added, X-003/X-010 messages name what they found), `client_portal/{models,service,agent_page}.py`, eight Vue components, MCP `get_agent_info` text, docs, tests.

## Attack surface

No new endpoint, MCP tool, WebSocket channel, credential, schema or dependency. Two payload fields are added, both closed Literals: `PortalPlaybook.kind` and `PortalAgentPage.capabilities_state`. Author-written SKILL.md descriptions already reached every surface listed in requirements/skills.md §23; the diff normalises blank values on the way and adds no new ingress.

## Checks

| Area | Result |
|---|---|
| P0 quicklist | Starlette: no changed backend file reads `request.url.path`. Claude Code pin 2.1.281 unchanged. Other rows: no dependency, image or compose change. |
| P2 secrets | Added lines: no known credential prefix, no concealed Unicode (tag-block, zero-width, bidi). Enterprise docs guard pattern and private-module names: no hit. |
| P3 supply chain | No dependency change. `skill_packaging.py` stays self-contained: the community skills library CI downloads exactly it and `utils/safe_yaml.py` at a pinned commit, and a new test pins its imports so a future edit cannot break that CI silently. |
| P4 CI/CD, P5 infra | No workflow, Dockerfile or compose change. |
| P6 integrations | `resolve_exposed_playbooks` cleans each description before building `ConnectorPlaybook`; a non-text value from an older agent image no longer raises and empties the list. The public link's field allowlist (`PUBLIC_SKILL_FIELDS`) is unchanged. |
| P7 ASI01/ASI07 | Descriptions reaching another LLM (connector `list_playbooks`, MCP skill tools) are the same text as before, minus blanks. X-010/X-003 messages now carry up to five agent-authored names, each through `_clip` (non-printables dropped, 40 chars), the precedent the D checks already set. |
| P7 LLM05 / P9 A05 | Every changed render path interpolates (`{{ }}`, `:title`). No `v-html` added; the Compatibility card renders `message` as text. |
| P8 agent-shipped | No skill, template, hook or MCP config change. |
| P9 A05 (YAML) | No new YAML load; the frontmatter block still goes through `load_hardened_yaml` with REJECT. The anchored regex is linear (`^` only at line starts). Values that reach a probe (`requires.binaries`, `requires.env`) are still regex-gated, so a block that now parses more of a file cannot inject. |
| P9 A10 | Failure directions stated: `clean_description` degrades to `None`; `recall()` cleans a kept copy; X-010 skips what the capped snapshot could not read (an `info` advisory, never a gate). |
| Guards | `test_1965_agent_server_safe_yaml.py`, `test_2996_human_only_routes.py`, `test_agent_auth_header_guard.py`, `test_186_enumeration_uniformity.py`, `test_models_centralized.py` green. `test_ent314_hardened_yaml.py` has one red, from the mounted enterprise submodule's own test file, not this diff. |

## Findings

None above the gate.

## Observation (below the gate, not a finding)

`clean_description` trims whitespace only, so a description can still carry zero-width, bidi or tag-block characters into the UIs and into other LLMs' context through `list_playbooks` and the MCP skill tools. That was true before this change and the diff does not widen it (hard exclusion 5). The new single seam makes stripping them a one-line follow-up if wanted.

## Coverage gaps

- Phase 3 scanner not run: no dependency changed.
- Phases 10–11 not run: diff mode, no new component or data class.
