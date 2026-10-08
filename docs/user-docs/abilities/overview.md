# Abilities Plugin Marketplace

The official agent development toolkit for Claude Code. Curated plugins covering the full agent lifecycle — from scaffolding and onboarding to deployment, scheduling, and ongoing operations.

Abilities is **Claude Code tooling**: the plugins run in your Claude Code session (or inside a deployed Claude Code agent), and the agents its wizards scaffold are Claude Code agents. Trinity itself is model-agnostic — an agent can also run on Gemini CLI or OpenAI Codex (see [Agent Runtimes](../agents/agent-runtimes.md)) — but you build those without these plugins.

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

# Browse the marketplace's plugins (interactive plugin manager)
/plugin

# Install core plugins
/plugin install create-agent@abilityai
/plugin install agent-dev@abilityai
/plugin install trinity@abilityai
```

Or from the terminal:

```bash
claude plugin marketplace add abilityai/abilities
claude plugin install create-agent@abilityai
```

## Available Plugins

| Plugin | Version | Skills | Purpose | Key Skills |
|--------|---------|--------|---------|------------|
| [create-agent](create-agent-plugin.md) | 2.2.0 | 6 | Agent creation — interview-driven, any domain | `/create-agent:custom`, `/create-agent:review` |
| [agent-dev](agent-dev-plugin.md) | 1.20.0 | 30 | Extend existing agents, and work fleet-wide | `/agent-dev:create-playbook`, `/agent-dev:add-memory`, `/agent-dev:add-project-management`, `/agent-dev:add-orchestrator`, `/agent-dev:add-canon`, `/agent-dev:agent-fleet-analysis` |
| [trinity](trinity-plugin.md) | 2.11.3 | 7 | Deploy to and operate on Trinity | `/trinity:start-here`, `/trinity:connect`, `/trinity:onboard`, `/trinity:sync`, `/trinity:loop` |
| [dev-methodology](dev-methodology-plugin.md) | 1.2.1 | 24 | Development workflow | `/dev-methodology:implement`, `/dev-methodology:validate-pr` |
| [utilities](utilities-plugin.md) | 1.2.2 | 7 | Ops and productivity | `/utilities:safe-deploy`, `/utilities:docker-ops` |

The standalone `add-project-management` plugin is deprecated — it is a pointer stub that installs nothing. Its skill lives in agent-dev: `/agent-dev:add-project-management`. Existing installs of the old name keep working for one release — switch, then uninstall it.

## The Agent Development Workflow

Abilities supports a four-step workflow:

```
1. Scaffold              2. Develop                    3. Deploy                    4. Iterate
/create-agent:*          /agent-dev:create-playbook    git push → /trinity:onboard  git push → /trinity:sync
                         /agent-dev:add-memory         (or onboard in place)        /create-agent:review
                         /agent-dev:add-backlog                                     /create-agent:adjust
```

**Scaffold** — Run `/create-agent:custom` to get a fully configured agent for any domain (`/create-agent:website` scaffolds a site with no agent).

**Develop** — Use `/agent-dev:create-playbook` to add capabilities, `/agent-dev:add-memory` for persistence, and `/agent-dev:add-backlog` (the agent's own dev backlog) or `/agent-dev:add-project-management` (cross-actor projects) for task management.

**Deploy** — Run `/trinity:connect` once to authenticate, push the agent's repo, then `/trinity:onboard` per agent — Trinity clones the repo. With your own GitHub token that can push to your own repo, the agent gets a working branch with auto-sync, so its work lands back in git; otherwise it is pull-only on the tracked branch. An agent that is already deployed from a bare repo can be onboarded *in place* by running `/trinity:onboard` inside it.

**Iterate** — Push changes and run `/trinity:sync`, which also reconciles declared schedules and plugins onto the instance. Use `/create-agent:review` and `/create-agent:adjust` to audit and improve.

## What Wizard-Created Agents Include

Every agent created with `/create-agent:custom` includes:

- **CLAUDE.md** — Identity and behavioral instructions
- **Initial skills** — 2-4 playbooks based on agent purpose
- **Onboarding system** — `onboarding.json` + `/onboarding` skill
- **Dashboard** — `dashboard.yaml` + `/update-dashboard` skill
- **Trinity files** — `template.yaml` (declaring credentials, `schedules:`, `plugins:`, and `metrics:`), `.env.example`, `.mcp.json.template`
- **Git repo** — Initialized and committed

## See Also

- [Trinity CLI](../cli/trinity-cli.md) — Command-line deployment
- [Skills and Playbooks](../automation/skills-and-playbooks.md) — How skills work in Trinity
- [GitHub: abilityai/abilities](https://github.com/abilityai/abilities) — Source repository
