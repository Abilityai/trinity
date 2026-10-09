# Feature: Playbooks Tab (PLAYBOOK-001) — superseded

Superseded by [skills-tab.md](skills-tab.md) (trinity-enterprise#754): the Playbooks tab merged into the agent's Skills tab.

- The agent's own skills, with Run and Edit & Run, are the Skills tab's **Own skills** section. An old `?tab=playbooks` link opens the Skills tab (`TAB_ALIASES`).
- `PlaybooksPanel.vue` is deleted. `GET /api/agents/{name}/playbooks` keeps its name and status codes (the stopped-agent 503 now says "Start the agent to view its skills."); it reads through `services/agent_skills_listing.py` and takes an opt-in `?last_known=true`.
- The agent-server scanner (`GET /api/skills`, including the #2850 per-field frontmatter normalization) is documented in skills-tab.md → Agent Layer.

## Revision History

| Date | Changes |
|------|---------|
| 2026-10-08 | trinity-enterprise#754: retired to this pointer; the content lives in skills-tab.md |
| 2026-09-16 | #2850: per-field frontmatter normalization in the scanner |
| 2026-02-27 | Initial implementation (PLAYBOOK-001) |
