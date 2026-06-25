# Skills and Playbooks

Platform-managed skills that can be assigned to agents and invoked from the UI via the Playbooks tab or chat autocomplete.

> 📺 **Watch:** [Building Agents — Playbooks, Plugins, Deployment](https://youtu.be/MDxRZBikf70) *(Apr 2026)* · [Build an AI Recruiter Agent](https://youtu.be/K7hFWyFIf-Y) *(Jun 2026)* · [all videos](../videos.md)

## Concepts

- **Skill** -- A reusable capability defined as a markdown file (`SKILL.md`) stored in the platform's skills library. Skills contain instructions, tools, and procedures that agents can execute.
- **Skills Library** -- A GitHub repository synced to Trinity containing all available skills. Admins can trigger a sync from Settings.
- **Skill Assignment** -- An owner assigns skills to agents. Assigned skills are injected into the agent on startup.
- **Playbook** -- A skill invoked from the UI. The Playbooks tab shows assigned skills with a "Run" button.
- **Playbook Autocomplete** -- Type `/` in the Chat tab input to see a dropdown of available playbooks with ghost text showing command syntax and argument hints.

## How It Works

### Admin -- Managing Skills

1. Go to **Settings** or the **Skills** admin page.
2. Sync the skills library from GitHub.
3. Create, edit, or delete skills (full CRUD).
4. View skill details and usage.

### Owner -- Assigning Skills

1. Open the agent detail page.
2. Go to the skills/playbooks section.
3. Assign skills from the library to this agent.
4. Skills are injected on the next agent start.

### User -- Running Playbooks

1. Open agent detail, click the **Playbooks** tab.
2. See the list of assigned skills with descriptions.
3. Click **Run** on a playbook -- this sends the skill as a task to the agent.
4. Or: in the **Chat** tab, type `/` to autocomplete a playbook command.

### Skill Injection on Agent Start

When an agent starts, all assigned skills are written to the agent's `.claude/commands/` directory. The agent can then use them as slash commands during execution.

## For Agents

MCP tools available for skill and playbook management:

| Tool | Description |
|------|-------------|
| `list_skills()` | List all platform skills |
| `get_skill(id)` | Get skill details |
| `get_skills_library_status()` | Library sync status |
| `assign_skill_to_agent(skill_id, agent_name)` | Assign a skill to an agent |
| `set_agent_skills(agent_name, skill_ids)` | Set all skills for an agent |
| `sync_agent_skills(agent_name)` | Re-inject skills into a running agent |
| `get_agent_skills(agent_name)` | List skills assigned to an agent |

## See Also

- [Scheduling](scheduling.md) -- Automate skill execution on a schedule
