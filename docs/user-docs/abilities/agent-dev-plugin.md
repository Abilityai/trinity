# agent-dev Plugin

Development tools for extending existing agents with skills, memory systems, and workflows.

## Installation

```bash
/plugin install agent-dev@abilityai
```

## Skills

| Skill | Description |
|-------|-------------|
| `/agent-dev:create-playbook` | Create a new skill/playbook for the agent |
| `/agent-dev:adjust-playbook` | Modify an existing skill/playbook |
| `/agent-dev:add-memory` | Add a memory system (file-index, brain, json-state, workspace) |
| `/agent-dev:add-backlog` | Add a GitHub Issues backlog workflow to the agent |
| `/agent-dev:backlog` | View the agent's current GitHub Issues backlog |
| `/agent-dev:pick-work` | Claim the next issue from the backlog |
| `/agent-dev:close-work` | Close the current in-progress issue |
| `/agent-dev:work-loop` | Run an autonomous loop: pick → work → close → repeat |
| `/agent-dev:plan` | Plan and execute a large multi-session project |

## Memory Systems

Add persistent memory to agents via `/agent-dev:add-memory`:

| System | Purpose | Use When |
|--------|---------|----------|
| **file-index** | Workspace file awareness and search | Agent needs to know what files exist |
| **brain** | Zettelkasten-style knowledge graph | Agent builds connected notes over time |
| **json-state** | Structured JSON state with jq updates | Agent tracks counters, config, or structured data |
| **workspace** | Multi-session project tracking | Agent works on long-running projects |

Memory systems are copied directly into the agent — no plugin dependency at runtime.

## Playbook Development

### Create a New Playbook

```bash
/agent-dev:create-playbook
```

Guides through:

1. **Purpose** — What the playbook accomplishes
2. **Triggers** — When it should run
3. **Steps** — The workflow steps
4. **State** — What data it reads/writes

### Modify an Existing Playbook

```bash
/agent-dev:adjust-playbook daily-report
```

Options:

- Add/remove steps
- Change automation level
- Update schedule
- Fix issues

## GitHub Backlog Workflow

Add task management via GitHub Issues:

```bash
/agent-dev:add-backlog
```

This installs the backlog skills (`backlog`, `pick-work`, `close-work`) directly into the agent. After install, the agent has:

- `/backlog` — view current issues
- `/pick-work` — claim the next task to work on
- `/close-work` — mark the current task done
- (combined with `/agent-dev:work-loop` for autonomous mode)

### Autonomous Work Loop

Run the agent autonomously through its backlog:

```bash
/agent-dev:work-loop
```

The agent:

1. Picks the highest priority issue
2. Works on it until complete
3. Closes the issue
4. Picks the next one
5. Repeats until the backlog is empty or the time limit is reached

## Multi-Session Planning

For large projects that span multiple sessions:

```bash
/agent-dev:plan
```

Creates a persistent plan that tracks:

- Overall goals and milestones
- Current session focus
- Completed work
- Next steps

## See Also

- [create-agent Plugin](create-agent-plugin.md) — Create new agents
- [Skills and Playbooks](../automation/skills-and-playbooks.md) — How skills work in Trinity
- [Abilities Overview](overview.md) — Full toolkit overview
