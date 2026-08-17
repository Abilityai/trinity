# create-agent Plugin

Create new Claude Code agents with domain-specific wizards. Each wizard is a domain expert that asks the right questions and builds a fully configured, Trinity-compatible agent.

> 📺 **Watch:** [Build an AI Recruiter Agent](https://youtu.be/K7hFWyFIf-Y) *(Jun 2026)* · [all videos](../videos.md)

## Installation

```bash
/plugin install create-agent@abilityai
```

## Available Wizards

| Wizard | Command | What it creates |
|--------|---------|-----------------|
| **create** | `/create-agent:create` | Discovery entry point — shows all wizards and lets you pick interactively |
| **prospector** | `/create-agent:prospector` | B2B SaaS sales research — company research, ICP scoring, CRM integration |
| **chief-of-staff** | `/create-agent:chief-of-staff` | Executive assistant — daily briefings, meeting prep, decision tracking |
| **webmaster** | `/create-agent:webmaster` | Website management — scaffolds and deploys Next.js 15 sites to Vercel |
| **recon** | `/create-agent:recon` | Competitive intelligence — competitor tracking, market research, battlecards |
| **receptionist** | `/create-agent:receptionist` | Email gateway — public-facing email communication and request routing |
| **ghostwriter** | `/create-agent:ghostwriter` | Content writer — brand voice profiles, platform-specific writing |
| **kb-agent** | `/create-agent:kb-agent` | Knowledge-base agent — Cornelius-shaped KB with local vector search |
| **doctor** | `/create-agent:doctor` | Personal medical-records agent — ingests health documents, tracks lab trends, preps doctor visits |
| **website** | `/create-agent:website` | Single website scaffold (no agent, just a site) |
| **custom** | `/create-agent:custom` | Custom agent from scratch — you define everything |
| **clone** | `/create-agent:clone` | Clone an existing agent repository as starting point |
| **review** | `/create-agent:review` | Read-only audit of an existing agent — prioritized findings report, no changes made |
| **adjust** | `/create-agent:adjust` | Apply best-practice improvements to an existing agent |

## How It Works

### Discovery Entry Point

Run `/create-agent:create` to see all 14 available wizards and select one interactively.

### Wizard Flow

Each wizard guides you through domain-specific questions:

1. **Identity** — Name, purpose, personality
2. **Tools** — Which integrations and capabilities
3. **Workflows** — Key playbooks and automations
4. **Configuration** — Environment variables, credentials needed

### Output

The wizard creates a complete agent directory:

```
my-agent/
├── CLAUDE.md              # Agent identity and instructions (carries the playbook-call rule)
├── template.yaml          # Trinity metadata: resources, credentials:, schedules:, plugins:
├── .env.example           # Required environment variables
├── .mcp.json.template     # MCP server configuration template (${VAR} in env blocks only)
├── dashboard.yaml         # Metrics dashboard definition
├── .claude/
│   └── skills/            # Initial playbooks
│       ├── onboarding/    # Setup progress tracker
│       └── update-dashboard/
└── .gitignore
```

Three conventions every wizard bakes in, so the agent is deployable as generated:

- **`template.yaml` declares everything Trinity materializes at creation** — the credentials the agent needs (so the guided credential checklist is populated), its recommended `schedules:`, and the `plugins:` it depends on (each wizard declares the plugins it already tells you to install, `trinity@abilityai` at minimum, so the selection survives a rebuild). See [Creating Agents](../agents/creating-agents.md).
- **Schedules are one-line playbook calls** — a generated schedule message is `/daily-briefing`, never a prose description of the work; the human-readable intent lives in the schedule's `purpose:` field. Cron times default to UTC (the container clock) with canonical IANA zone names.
- **Delegation is by playbook call** — the generated `CLAUDE.md` guidelines tell the agent to hand work to other agents only by invoking a named playbook, one line, on any transport. See [Playbook calls](../automation/abilities-marketplace.md#playbook-calls-the-unit-of-inter-agent-work).

## Usage Examples

### Create a Sales Research Agent

```bash
/create-agent:prospector
```

The wizard asks about:
- Target market and ICP criteria
- CRM system (HubSpot, Salesforce, etc.)
- Research sources (LinkedIn, Crunchbase, etc.)
- Output formats (reports, CRM updates)

### Create a Custom Agent

```bash
/create-agent:custom
```

Blank canvas — you define every aspect from scratch.

### Review and Improve an Existing Agent

```bash
/create-agent:review    # Read-only audit — prioritized findings, no changes
/create-agent:adjust    # Apply the improvements
```

`review` audits the agent against best practices — CLAUDE.md, skills, composition integrity, Trinity readiness — and produces a prioritized findings report without changing anything. Prose inter-agent delegation and prose schedule messages are reported as findings. `adjust` is the write-side companion: it proposes exact before/after changes and applies the ones you approve.

## See Also

- [agent-dev Plugin](agent-dev-plugin.md) — Extend agents with skills and memory
- [trinity Plugin](trinity-plugin.md) — Deploy to Trinity platform
- [Abilities Overview](overview.md) — Full toolkit overview
