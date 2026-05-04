# Abilities Marketplace

The abilities marketplace is a curated collection of Claude Code plugins covering the full agent lifecycle — from scaffolding and onboarding to deployment, scheduling, and ongoing operations.

## Concepts

- **Plugin marketplace** — A registry of versioned plugin packages hosted at `github.com/abilityai/abilities`. Claude Code's `/plugin marketplace add` command connects to it.
- **Plugin** — A named package of skills installed into your Claude Code session. Skills become available as `/plugin-name:skill-name` commands.
- **Skill** — A single SKILL.md file defining a workflow Claude executes when you invoke it.
- **abilities** — The specific marketplace hosted by Ability.ai, containing 5 plugins for the agent development lifecycle.

## How It Works

### Installation (one-time)

```bash
# Add the abilities marketplace to Claude Code
/plugin marketplace add abilityai/abilities
```

Or from the terminal:

```bash
claude plugin add abilityai/abilities
```

Then install the plugins you need:

```bash
/plugin install create-agent@abilityai    # Agent creation wizards
/plugin install agent-dev@abilityai       # Development tools
/plugin install trinity@abilityai         # Trinity platform integration
/plugin install dev-methodology@abilityai # Documentation-driven dev
/plugin install utilities@abilityai       # Ops and productivity
```

### Listing available plugins

```bash
/plugin list abilityai
```

## The 5 Plugins

### create-agent — 12 skills

Create new Claude Code agents with domain-specific wizards.

```bash
/create-agent:create    # Discovery — shows all available wizards
```

| Wizard | What it creates |
|--------|-----------------|
| `/create-agent:prospector` | B2B sales research agent |
| `/create-agent:chief-of-staff` | Executive assistant |
| `/create-agent:webmaster` | Website management agent |
| `/create-agent:recon` | Competitive intelligence agent |
| `/create-agent:receptionist` | Email gateway agent |
| `/create-agent:ghostwriter` | Content writer agent |
| `/create-agent:kb-agent` | Knowledge-base agent (Zettelkasten) |
| `/create-agent:website` | Next.js website (no agent) |
| `/create-agent:custom` | Blank canvas — you define everything |
| `/create-agent:clone` | Clone an existing agent repo |
| `/create-agent:adjust` | Audit and improve an existing agent |

Every wizard-created agent includes `CLAUDE.md`, 2–4 starter skills, `template.yaml`, `dashboard.yaml`, and an onboarding tracker.

### agent-dev — 15 skills

Extend and develop existing agents.

```bash
/agent-dev:create-playbook    # Add a new skill/playbook
/agent-dev:adjust-playbook    # Modify an existing skill
/agent-dev:add-memory         # Add a memory system
/agent-dev:add-backlog        # Add GitHub Issues task management
/agent-dev:work-loop          # Run an autonomous work loop
```

**Memory systems** (via `/agent-dev:add-memory`):

| System | Best for |
|--------|----------|
| `file-index` | Workspace file awareness and search |
| `brain` | Zettelkasten-style connected knowledge graph |
| `json-state` | Structured state, counters, config |
| `workspace` | Multi-session project tracking |

### trinity — 5 skills

Connect, deploy, and sync agents to Trinity.

```bash
/trinity:connect     # One-time: authenticate and save MCP config
/trinity:onboard     # Per-agent: compatibility check + deploy
/trinity:sync        # Sync local changes to the running remote agent
/trinity:deploy      # Provision a new Trinity instance + ops agent
```

After connecting, Trinity MCP tools are available directly in your session:
`mcp__trinity__list_agents`, `mcp__trinity__chat_with_agent`, `mcp__trinity__deploy_local_agent`.

### dev-methodology — 24 skills

Documentation-driven development methodology for any codebase.

```bash
/dev-methodology:init             # Scaffold methodology into your project
/dev-methodology:implement        # End-to-end feature implementation
/dev-methodology:validate-pr      # Validate PR against methodology
/dev-methodology:security-analysis # Deep security audit
/dev-methodology:commit           # Well-formatted commits
```

### utilities — 7 skills

General-purpose ops and productivity.

```bash
/utilities:investigate-incident   # Structured incident investigation
/utilities:safe-deploy            # Deployment with backup/rollback
/utilities:docker-ops             # Docker container management
/utilities:save-conversation      # Export conversation as markdown
/utilities:sync-ops-knowledge     # Update ops docs from commits
```

## The Four-Step Agent Workflow

```
Scaffold            Develop                     Deploy           Iterate
/create-agent:*     /agent-dev:create-playbook  /trinity:onboard /trinity:sync
                    /agent-dev:add-memory                        git push
                    /agent-dev:add-backlog                       /create-agent:adjust
```

1. **Scaffold** — Pick a wizard or use `/create-agent:custom`. Get a fully wired agent in one session.
2. **Develop** — Add skills, memory systems, and task management as the agent's role expands.
3. **Deploy** — Run `/trinity:onboard` to deploy to your Trinity instance. It runs 24/7 from there.
4. **Iterate** — Push changes with `git push` or `/trinity:sync`. Use `/create-agent:adjust` to audit and improve over time.

## See Also

**Trinity docs:**
- [Building Agents](../guides/building-agents.md) — End-to-end walkthrough using these plugins
- [create-agent Plugin](../abilities/create-agent-plugin.md) — All 12 creation wizards in detail
- [agent-dev Plugin](../abilities/agent-dev-plugin.md) — Skills, memory, backlog, planning
- [trinity Plugin](../abilities/trinity-plugin.md) — Connect, deploy, sync workflows
- [Skills and Playbooks](skills-and-playbooks.md) — How skills run inside Trinity agents
- [Trinity Ops Agent](../guides/deploying/ops-agent.md) — Managing a Trinity instance post-deploy

**External references:**
- [abilityai/abilities](https://github.com/abilityai/abilities) — Plugin source, changelog, contributing guide
