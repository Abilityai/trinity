# Abilities Plugin Marketplace

The official agent development toolkit for Claude Code. Curated plugins covering the full agent lifecycle — from scaffolding and onboarding to deployment, scheduling, and ongoing operations.

## Quick Start

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
| [create-agent](create-agent-plugin.md) | 12 | Agent creation wizards | `/create-agent:prospector`, `/create-agent:custom` |
| [agent-dev](agent-dev-plugin.md) | 9 | Extend existing agents | `/agent-dev:create-playbook`, `/agent-dev:add-memory` |
| [trinity](trinity-plugin.md) | 3 | Deploy to Trinity | `/trinity:connect`, `/trinity:onboard` |
| [dev-methodology](dev-methodology-plugin.md) | 15 | Development workflow | `/dev-methodology:implement`, `/dev-methodology:validate-pr` |
| [utilities](utilities-plugin.md) | 7 | Ops and productivity | `/utilities:safe-deploy`, `/utilities:docker-ops` |

## The Agent Development Workflow

Abilities supports a four-step workflow:

```
1. Scaffold              2. Develop                    3. Deploy              4. Iterate
/create-agent:*          /agent-dev:create-playbook    /trinity:onboard       /trinity:sync
                         /agent-dev:add-memory         trinity deploy .       git push
                         /agent-dev:add-backlog                               /create-agent:adjust
```

**Scaffold** — Use a wizard like `/create-agent:prospector` or `/create-agent:custom` to get a fully configured agent.

**Develop** — Use `/agent-dev:create-playbook` to add capabilities, `/agent-dev:add-memory` for persistence.

**Deploy** — Run `/trinity:connect` once to authenticate, then `/trinity:onboard` per agent.

**Iterate** — Push changes with `git push` or `/trinity:sync`. Use `/create-agent:adjust` to audit.

## What Wizard-Created Agents Include

Every agent created with the wizards includes:

- **CLAUDE.md** — Identity and behavioral instructions
- **Initial skills** — 2-4 playbooks based on agent purpose
- **Onboarding system** — `onboarding.json` + `/onboarding` skill
- **Dashboard** — `dashboard.yaml` + `/update-dashboard` skill
- **Trinity files** — `template.yaml`, `.env.example`, `.mcp.json.template`
- **Git repo** — Initialized and committed

## See Also

- [Trinity CLI](../cli/trinity-cli.md) — Command-line deployment
- [Skills and Playbooks](../automation/skills-and-playbooks.md) — How skills work in Trinity
- [GitHub: abilityai/abilities](https://github.com/abilityai/abilities) — Source repository
