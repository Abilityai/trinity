# agent-dev Plugin

Development tools for extending existing agents — skills, memory systems, git-backed state, a GitHub Issues development cycle, and long-running pipelines.

> 📺 **Watch:** [Why Every AI Agent Needs a GitHub Repo](https://youtu.be/R4nNHf6ywEs) *(Apr 2026)* · [3 AI Agents Run My Software Development Pipeline](https://youtu.be/zCDFDhewFkk) *(Apr 2026)* · [all videos](../videos.md)

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
| `/agent-dev:add-git-sync` | Add git-as-state hooks — auto-commit on stop, rebase on session start |
| `/agent-dev:add-backlog` | Install the full GitHub Issues development cycle into the agent |
| `/agent-dev:backlog` | View the agent's current GitHub Issues backlog |
| `/agent-dev:claim` | Claim the next issue — picks the highest-priority todo, marks it in-progress |
| `/agent-dev:autoplan` | Analyze a claimed issue before implementing — affected files, changes, risks |
| `/agent-dev:commit` | Commit changed skill files and close the in-progress issue with traceability |
| `/agent-dev:close` | Close the current issue without a commit (use `/commit` when files changed) |
| `/agent-dev:groom` | Groom the backlog — label untagged issues, verify priorities, surface stale work |
| `/agent-dev:roadmap` | Strategic view — open issues grouped by skill area |
| `/agent-dev:sprint` | Human-supervised cycle: roadmap → claim → autoplan → implement → commit |
| `/agent-dev:work-loop` | Autonomous unit of work: pick one issue, execute, close, exit |
| `/agent-dev:plan` | Plan and execute a large multi-session project |
| `/agent-dev:add-pipeline` | Scaffold a long-running multi-stage pipeline inside the agent |
| `/agent-dev:add-pipeline-instance` | Add an instance (tenant / zone / case) to an existing pipeline |
| `/agent-dev:add-pipeline-stage` | Append a stage to an existing pipeline definition |
| `/agent-dev:validate-pipeline` | Lint a pipeline.yaml — schema, DAG acyclicity, referenced skills |

## Memory Systems

Add persistent memory to agents via `/agent-dev:add-memory`:

| System | Purpose | Use When |
|--------|---------|----------|
| **file-index** | Workspace file awareness and search | Agent needs to know what files exist |
| **brain** | Zettelkasten-style knowledge graph | Agent builds connected notes over time |
| **json-state** | Structured JSON state with jq updates | Agent tracks counters, config, or structured data |
| **workspace** | Multi-session project tracking | Agent works on long-running projects |

Memory systems are copied directly into the agent — no plugin dependency at runtime.

## Git Sync (git-as-state)

```bash
/agent-dev:add-git-sync
```

Installs hooks that treat the agent's own repository as durable memory:

- **Auto-commit on Stop** — work-in-progress is committed when a session ends
- **Rebase on SessionStart** — each session begins from the latest remote state
- **Snapshot on PreCompact** — state is preserved before context compaction

This gives agents durable cross-session memory through their repo, complementing Trinity's [GitHub Sync](../integrations/github-sync.md) on the platform side.

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

This installs the full development cycle directly into the agent. After install, the agent has:

- `/backlog` — view current issues
- `/claim` — claim the next issue to work on
- `/autoplan` — analyze the claimed issue before implementing
- `/commit` — commit changes and close the issue with a traceable message
- `/close` — close without a commit
- `/groom` and `/roadmap` — keep the backlog labeled, prioritized, and surveyable
- `/sprint` — the human-supervised end-to-end cycle
- `/work-loop` — the autonomous variant (below)

### Autonomous Work Loop

```bash
/agent-dev:work-loop
```

One bounded unit of work: the agent picks the highest-priority issue, executes it, closes it, and **exits**. It is designed to be re-invoked — by a Trinity schedule for a steady cadence, or by a Trinity [agent loop](../automation/agent-loops.md) for a bounded burst ("drain up to 20 items, stop when the backlog is empty"). See the *Backlog draining* pattern in the Agent Loops guide.

## Pipelines (long-running multi-stage work)

```bash
/agent-dev:add-pipeline
```

Scaffolds an agent-owned pipeline for work that spans days or weeks (e.g. perception → synthesis → publish → measure): a `projects/<slug>/` directory with `pipeline.yaml` and per-instance state, tick/status/recover/pause/resume skills, a heartbeat schedule that advances stages, and a `~/.trinity/` read surface so Trinity can display pipeline state without owning it.

- `/agent-dev:add-pipeline-instance` — add a tenant/zone/case to an existing pipeline
- `/agent-dev:add-pipeline-stage` — extend the stage DAG
- `/agent-dev:validate-pipeline` — lint the definition (schema, acyclicity, skill references)

Pipelines are owned by the agent, not by Trinity — the platform only reads the published state. This matches Trinity's agent-defined-pipelines design: no central DAG engine.

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
- [trinity Plugin](trinity-plugin.md) — Deploy, sync, and loop agents on Trinity
- [Agent Loops](../automation/agent-loops.md) — Drive `/work-loop` in bounded autonomous bursts
- [Skills and Playbooks](../automation/skills-and-playbooks.md) — How skills work in Trinity
- [Abilities Overview](overview.md) — Full toolkit overview
