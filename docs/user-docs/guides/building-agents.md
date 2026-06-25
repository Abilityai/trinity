# Building Agents with Claude Code

Use the **abilities** plugins to create, develop, and deploy agents to Trinity — all from your terminal.

> 📺 **Watch:** [Build an AI Recruiter Agent](https://youtu.be/K7hFWyFIf-Y) *(Jun 2026)* · [Build and Deploy Agents in Cursor](https://youtu.be/amqiysdlEWY) *(Apr 2026)* · [From Zero to Deployed](https://youtu.be/-TSZyekDS6o) *(Apr 2026)* · [all videos](../videos.md)

## Prerequisites

- **Claude Code** installed — `npm install -g @anthropic-ai/claude-code`
- **Trinity instance** running — either on [ability.ai](https://ability.ai) or [self-hosted](deploying-trinity.md)

## One-Time Setup

Add the abilities marketplace and install the core plugins:

```bash
# Add the abilities marketplace (one-time)
/plugin marketplace add abilityai/abilities

# Install the core plugins
/plugin install create-agent@abilityai
/plugin install agent-dev@abilityai
/plugin install trinity@abilityai
```

Or from the terminal: `claude plugin add abilityai/abilities`

## Path A: Creating a New Agent

Start from scratch with a guided wizard.

### Step 1: Choose a wizard and run it

```bash
/create-agent:create        # Shows all available wizards

# Or jump directly to a specific wizard:
/create-agent:prospector    # B2B sales research agent
/create-agent:chief-of-staff # Executive assistant
/create-agent:webmaster     # Website management
/create-agent:recon         # Competitive intelligence
/create-agent:ghostwriter   # Content writer
/create-agent:custom        # Blank canvas (you define everything)
```

Each wizard asks domain-specific questions and scaffolds a complete agent.

### Step 2: Connect to Trinity (one-time)

```bash
/trinity:connect
```

Authenticates and saves your MCP connection config. Only needed once per machine.

### Step 3: Deploy to Trinity

```bash
/trinity:onboard
```

Creates `template.yaml`, checks compatibility, and deploys. Your agent is now running 24/7.

## Path B: Onboarding an Existing Agent

Already have a Claude Code agent? Deploy it to Trinity.

### Step 1: Connect to Trinity (one-time)

```bash
/trinity:connect
```

### Step 2: Onboard the agent

```bash
/trinity:onboard
```

Checks your agent for Trinity compatibility, creates required files (`template.yaml`, `.mcp.json.template`), and deploys.

### Step 3: Review and improve (optional)

```bash
/create-agent:adjust
```

Audits your agent against best practices and proposes improvements to CLAUDE.md, skills, and Trinity files.

## Path C: Ongoing Development

Add capabilities and keep your agent in sync.

```bash
# Add a new skill/playbook
/agent-dev:create-playbook

# Add a memory system
/agent-dev:add-memory   # Choose: file-index, brain, json-state, workspace

# Add GitHub Issues task management
/agent-dev:add-backlog

# Push changes to Trinity
/trinity:sync          # Or just: git push
```

## What Gets Created

Wizard-created agents include everything needed for Trinity:

| File | Purpose |
|------|---------|
| `CLAUDE.md` | Agent identity and instructions |
| `.claude/skills/` | 2-4 starter skills |
| `template.yaml` | Trinity deployment config |
| `.mcp.json.template` | Credential declarations |
| `dashboard.yaml` | Custom metrics dashboard |
| `onboarding.json` | Setup progress tracker |

## Next Steps

- [create-agent Plugin](../abilities/create-agent-plugin.md) — All 13 creation wizards explained
- [agent-dev Plugin](../abilities/agent-dev-plugin.md) — Skills, memory systems, backlog, planning
- [trinity Plugin](../abilities/trinity-plugin.md) — Connect, onboard, deploy, sync workflows

## See Also

- [Abilities Marketplace](../automation/abilities-marketplace.md) — All 5 plugins, skill reference, four-step workflow
- [Creating Agents](../agents/creating-agents.md) — UI-based agent creation
- [Skills and Playbooks](../automation/skills-and-playbooks.md) — How skills work in Trinity
