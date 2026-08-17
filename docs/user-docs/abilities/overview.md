# Abilities Plugin Marketplace

The official agent development toolkit for Claude Code. Curated plugins covering the full agent lifecycle — from scaffolding and onboarding to deployment, scheduling, and ongoing operations.

> 📺 **Watch:** [Build and Deploy Agents in Cursor](https://youtu.be/amqiysdlEWY) *(Apr 2026)* · [all videos](../videos.md)

## Quick Start

**New to Trinity?** Install the `trinity` plugin and run `/trinity:start-here` — a guided, resumable journey from "what is Trinity?" to your first agent running on your own instance. It sequences everything below, so you don't have to pick a starting point:

```bash
/plugin marketplace add abilityai/abilities
/plugin install trinity@abilityai
/trinity:start-here
```

Prefer to drive yourself?

```bash
# Add the abilities marketplace (one-time)
/plugin marketplace add abilityai/abilities

# List available plugins
/plugin list abilityai

# Install core plugins
/plugin install create-agent@abilityai
/plugin install agent-dev@abilityai
/plugin install trinity@abilityai
```

Or from the terminal:

```bash
claude plugin add abilityai/abilities
claude plugin install create-agent@abilityai
```

## Available Plugins

| Plugin | Skills | Purpose | Key Skills |
|--------|--------|---------|------------|
| [create-agent](create-agent-plugin.md) | 14 | Agent creation wizards | `/create-agent:prospector`, `/create-agent:custom` |
| [agent-dev](agent-dev-plugin.md) | 25 | Extend existing agents, and work fleet-wide | `/agent-dev:create-playbook`, `/agent-dev:add-memory`, `/agent-dev:add-orchestrator`, `/agent-dev:add-canon`, `/agent-dev:agent-fleet-analysis` |
| [trinity](trinity-plugin.md) | 7 | Deploy to and operate on Trinity | `/trinity:start-here`, `/trinity:connect`, `/trinity:onboard`, `/trinity:sync`, `/trinity:loop` |
| [dev-methodology](dev-methodology-plugin.md) | 24 | Development workflow | `/dev-methodology:implement`, `/dev-methodology:validate-pr` |
| [utilities](utilities-plugin.md) | 7 | Ops and productivity | `/utilities:safe-deploy`, `/utilities:docker-ops` |

## The Agent Development Workflow

Abilities supports a four-step workflow:

```
1. Scaffold              2. Develop                    3. Deploy                    4. Iterate
/create-agent:*          /agent-dev:create-playbook    git push → /trinity:onboard  git push → /trinity:sync
                         /agent-dev:add-memory         (or onboard in place)        /create-agent:review
                         /agent-dev:add-backlog                                     /create-agent:adjust
```

**Scaffold** — Use a wizard like `/create-agent:prospector` or `/create-agent:custom` to get a fully configured agent.

**Develop** — Use `/agent-dev:create-playbook` to add capabilities, `/agent-dev:add-memory` for persistence.

**Deploy** — Run `/trinity:connect` once to authenticate, push the agent's repo, then `/trinity:onboard` per agent — Trinity clones the repo and tracks the branch. An agent that is already deployed from a bare repo can be onboarded *in place* by running `/trinity:onboard` inside it.

**Iterate** — Push changes and run `/trinity:sync`, which also reconciles declared schedules and plugins onto the instance. Use `/create-agent:review` and `/create-agent:adjust` to audit and improve.

## What Wizard-Created Agents Include

Every agent created with the wizards includes:

- **CLAUDE.md** — Identity and behavioral instructions
- **Initial skills** — 2-4 playbooks based on agent purpose
- **Onboarding system** — `onboarding.json` + `/onboarding` skill
- **Dashboard** — `dashboard.yaml` + `/update-dashboard` skill
- **Trinity files** — `template.yaml` (declaring credentials, `schedules:`, and `plugins:`), `.env.example`, `.mcp.json.template`
- **Git repo** — Initialized and committed

## See Also

- [Trinity CLI](../cli/trinity-cli.md) — Command-line deployment
- [Skills and Playbooks](../automation/skills-and-playbooks.md) — How skills work in Trinity
- [GitHub: abilityai/abilities](https://github.com/abilityai/abilities) — Source repository
