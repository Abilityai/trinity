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

### Step 3: Add your GitHub token (one-time)

Deployment is repository-first — Trinity clones the agent from its GitHub repo and tracks the branch. In the Trinity UI, go to **Settings → GitHub token** and add a fine-grained PAT with *Contents: Read* on the repos your agents live in. Public repos work without one.

### Step 4: Push, then deploy to Trinity

```bash
git push                # the wizard already initialized and committed the repo
/trinity:onboard
```

Checks compatibility, fills in anything missing in `template.yaml` (declared credentials, schedules, plugins), verifies the repo is pushed and readable, and deploys from it. Your agent is now running 24/7 — turn on its **autonomy** toggle in the UI when you want its schedules to start firing.

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

Checks your agent for Trinity compatibility, creates required files (`template.yaml`, `.env.example`, `.mcp.json.template`, `.gitignore`), and deploys — from the agent's GitHub repo by default, from local files as the fallback when there is no repo yet.

**Already deployed it from a bare repo?** If you created the agent in Trinity straight from a repository that had no `template.yaml` — someone else's agent, or one you didn't want to adapt locally — run `/trinity:onboard` *inside* that agent — send it `/trinity:onboard in-place` as a chat message, or run it from the agent's terminal — and it takes the **Onboard in place** path. It writes the Trinity files, installs and declares the plugins, pushes the result back to the repo, reconciles schedules, and verifies with the platform's compatibility report. See [Onboarding a deployed agent in place](../abilities/trinity-plugin.md#onboarding-a-deployed-agent-in-place).

### Step 3: Review and improve (optional)

```bash
/create-agent:review    # Read-only audit — prioritized findings, no changes
/create-agent:adjust    # Apply the improvements
```

`review` audits your agent against best practices without changing anything; `adjust` proposes and applies improvements to CLAUDE.md, skills, and Trinity files.

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
git push
/trinity:sync          # advances the deployed agent; also reconciles schedules + plugins
```

## What Gets Created

Wizard-created agents include everything needed for Trinity:

| File | Purpose |
|------|---------|
| `CLAUDE.md` | Agent identity and instructions |
| `.claude/skills/` | 2-4 starter skills |
| `template.yaml` | Trinity deployment config — resources, declared credentials, `schedules:`, `plugins:` |
| `.mcp.json.template` | MCP server configuration with `${VAR}` placeholders |
| `dashboard.yaml` | Custom metrics dashboard |
| `onboarding.json` | Setup progress tracker |

## Next Steps

- [create-agent Plugin](../abilities/create-agent-plugin.md) — All 14 creation wizards explained
- [agent-dev Plugin](../abilities/agent-dev-plugin.md) — Skills, memory systems, backlog, planning
- [trinity Plugin](../abilities/trinity-plugin.md) — Connect, onboard, deploy, sync workflows

## See Also

- [Abilities Marketplace](../automation/abilities-marketplace.md) — All 5 plugins, skill reference, four-step workflow
- [Creating Agents](../agents/creating-agents.md) — UI-based agent creation
- [Skills and Playbooks](../automation/skills-and-playbooks.md) — How skills work in Trinity
