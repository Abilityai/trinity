# create-agent Plugin

Create new Claude Code agents from an interview. The `custom` wizard asks about your domain and builds a fully configured, Trinity-compatible agent; `website` scaffolds a site; `review`, `adjust`, and `clone` work on agents you already have.

> **create-agent 2.0.0 (2026-09-14):** the eight pre-built domain wizards (prospector, chief-of-staff, webmaster, recon, receptionist, ghostwriter, kb-agent, doctor) were retired. Describe the same domain to `/create-agent:custom` and the interview builds it. If you used `kb-agent`, run `/create-agent:custom` and then `/agent-dev:add-memory` for the knowledge graph.

> 📺 **Watch:** [Build an AI Recruiter Agent](https://youtu.be/K7hFWyFIf-Y) *(Jun 2026)* · [all videos](../videos.md)

## Installation

```bash
/plugin install create-agent@abilityai
```

## Available Wizards

| Wizard | Command | What it creates |
|--------|---------|-----------------|
| **create** | `/create-agent:create` | Discovery entry point — shows all wizards and lets you pick interactively |
| **custom** | `/create-agent:custom` | Any agent, from an interview — role, skills, schedules, credentials, Trinity wiring |
| **website** | `/create-agent:website` | Single Next.js website scaffold (no agent, just a site) |
| **clone** | `/create-agent:clone` | Clone an existing agent repository as starting point |
| **review** | `/create-agent:review` | Read-only audit of an existing agent — prioritized findings report, no changes made |
| **adjust** | `/create-agent:adjust` | Apply best-practice improvements to an existing agent |

## How It Works

### Discovery Entry Point

Run `/create-agent:create` to see the six available wizards and select one interactively.

### Wizard Flow

The `custom` wizard interviews you about:

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

Three conventions the wizard bakes in, so the agent is deployable as generated:

- **`template.yaml` declares everything Trinity materializes at creation** — the credentials the agent needs (so the guided credential checklist is populated), its recommended `schedules:`, and the `plugins:` it depends on (the scaffold declares the plugins it already tells you to install, `trinity@abilityai` at minimum, so the selection survives a rebuild). See [Creating Agents](../agents/creating-agents.md).
- **Schedules are one-line playbook calls** — a generated schedule message is `/daily-briefing`, never a prose description of the work; the human-readable intent lives in the schedule's `purpose:` field. Cron times default to UTC (the container clock) with canonical IANA zone names.
- **Delegation is by playbook call** — the generated `CLAUDE.md` guidelines tell the agent to hand work to other agents only by invoking a named playbook, one line, on any transport. See [Playbook calls](../automation/abilities-marketplace.md#playbook-calls--the-unit-of-inter-agent-work).

## Usage Examples

### Create an Agent for Any Domain

```bash
/create-agent:custom
```

The interview asks about:
- Role and purpose
- Skills and workflows
- Recommended schedules
- Credentials the agent needs
- Trinity wiring (`template.yaml`, `.mcp.json.template`, reporting)

Describe a sales researcher, a content writer, a knowledge base, or anything else — the same interview builds it.

### Review and Improve an Existing Agent

```bash
/create-agent:review    # Read-only audit — prioritized findings, no changes
/create-agent:adjust    # Apply the improvements
```

`review` audits the agent against best practices — CLAUDE.md, skills, composition integrity, Trinity readiness — and produces a prioritized findings report without changing anything. Prose inter-agent delegation and prose schedule messages are reported as findings, as are a `template.yaml` with no `plugins:` block and a report guard that does not handle the agent-scoped-key refusal. `adjust` is the write-side companion: it proposes exact before/after changes and applies the ones you approve.

## See Also

- [agent-dev Plugin](agent-dev-plugin.md) — Extend agents with skills and memory
- [trinity Plugin](trinity-plugin.md) — Deploy to Trinity platform
- [Abilities Overview](overview.md) — Full toolkit overview
