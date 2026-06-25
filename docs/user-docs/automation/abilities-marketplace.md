# Abilities Marketplace

The abilities marketplace is a curated collection of Claude Code plugins covering the full agent lifecycle — from scaffolding and onboarding to deployment, scheduling, and ongoing operations.

> 📺 **Watch:** [Build and Deploy Agents in Cursor](https://youtu.be/amqiysdlEWY) *(Apr 2026)* · [Building Agents — Playbooks, Plugins, Deployment](https://youtu.be/MDxRZBikf70) *(Apr 2026)* · [all videos](../videos.md)

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

### create-agent — 13 skills

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
| `/create-agent:doctor` | Personal medical-records agent |
| `/create-agent:website` | Next.js website (no agent) |
| `/create-agent:custom` | Blank canvas — you define everything |
| `/create-agent:clone` | Clone an existing agent repo |
| `/create-agent:adjust` | Audit and improve an existing agent |

Every wizard-created agent includes `CLAUDE.md`, 2–4 starter skills, `template.yaml`, `dashboard.yaml`, and an onboarding tracker.

### agent-dev — 19 skills

Extend and develop existing agents: playbooks, memory, git-backed state, a full GitHub Issues dev cycle, and long-running pipelines.

```bash
/agent-dev:create-playbook    # Add a new skill/playbook
/agent-dev:adjust-playbook    # Modify an existing skill
/agent-dev:add-memory         # Add a memory system
/agent-dev:add-git-sync       # Git-as-state hooks (auto-commit, rebase, snapshot)
/agent-dev:add-backlog        # Install the GitHub Issues dev cycle
/agent-dev:claim              # Claim the next issue
/agent-dev:autoplan           # Analyze a claimed issue before implementing
/agent-dev:commit             # Commit and close the issue with traceability
/agent-dev:sprint             # Supervised cycle: claim → plan → implement → commit
/agent-dev:work-loop          # Autonomous unit: pick one issue, do it, close, exit
/agent-dev:add-pipeline       # Scaffold a long-running multi-stage pipeline
/agent-dev:plan               # Plan multi-session work
```

Plus `backlog`, `close`, `groom`, `roadmap`, `add-pipeline-instance`, `add-pipeline-stage`, and `validate-pipeline` — see the [agent-dev plugin page](../abilities/agent-dev-plugin.md) for the full table.

**Memory systems** (via `/agent-dev:add-memory`):

| System | Best for |
|--------|----------|
| `file-index` | Workspace file awareness and search |
| `brain` | Zettelkasten-style connected knowledge graph |
| `json-state` | Structured state, counters, config |
| `workspace` | Multi-session project tracking |

### trinity — 6 skills

Connect, deploy, operate, and sync agents on Trinity.

```bash
/trinity:connect              # One-time: authenticate and save MCP config
/trinity:onboard              # Per-agent: compatibility check + deploy
/trinity:sync                 # Sync local changes to the running remote agent
/trinity:loop                 # Run a remote agent in a sequential, bounded loop
/trinity:create-dashboard     # Generate an /update-dashboard skill for dashboard.yaml
/trinity:deploy-new-instance  # Deploy a Trinity instance + ops agent on any server
```

After connecting, Trinity MCP tools are available directly in your session:
`mcp__trinity__list_agents`, `mcp__trinity__chat_with_agent`, `mcp__trinity__deploy_local_agent`, `mcp__trinity__run_agent_loop`.

`/trinity:loop` is the conversational front-end to the platform's [Sequential Agent Loops](agent-loops.md): `/trinity:loop @ci-agent run the test suite until it passes, max 10` fires a server-side loop you can disconnect from.

### dev-methodology — 24 skills

Documentation-driven development methodology for any codebase.

```bash
/dev-methodology:init             # Scaffold methodology into your project
/dev-methodology:autoplan         # Reviewed implementation plan for an issue
/dev-methodology:implement        # End-to-end feature implementation
/dev-methodology:review           # Pre-landing structural code review
/dev-methodology:validate-pr      # Validate PR against methodology
/dev-methodology:sprint           # Full dev-cycle orchestrator (claim → PR)
/dev-methodology:cso              # Security audit (branch diff or full codebase)
/dev-methodology:release          # Cut a release with notes and tags
/dev-methodology:commit           # Well-formatted commits
/dev-methodology:generate-user-docs # Generate user-facing docs from source
```

Plus grooming, roadmap, testing, refactor-audit, feature-flow, and the three drift validators (architecture/config/schema) — see the [dev-methodology plugin page](../abilities/dev-methodology-plugin.md).

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
