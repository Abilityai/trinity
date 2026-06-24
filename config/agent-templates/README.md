# Agent template catalog

Ready-made starting points for deploying an agent to Trinity. Each subdirectory is one template — a `CLAUDE.md` (the agent's instructions) plus a `template.yaml` (metadata: `name`, `description`, `type`, `capabilities`, `commands`, `metrics`, `folders`). Pick one as your starting point instead of authoring an agent from scratch.

> New here? The repo's agent entry point is [`AGENTS.md`](../../AGENTS.md) → [Deploy an agent](../../AGENTS.md#deploy-an-agent-to-trinity). Full template schema: [`docs/TRINITY_COMPATIBLE_AGENT_GUIDE.md`](../../docs/TRINITY_COMPATIBLE_AGENT_GUIDE.md).

## How to use a template

- **From a checkout** — copy a template directory, edit its `CLAUDE.md` + `template.yaml`, then deploy it: `cd <copy>/ && trinity deploy .` (see [Deploy an agent](../../AGENTS.md#deploy-an-agent-to-trinity)).
- **From the web UI** — *Create Agent* → pick a template by `name`.
- **By reference** — these `name`s are the built-in templates the platform ships; they appear in `GET /api/templates` and `list_templates` (MCP).

## Starter templates

### Single-purpose

| `name` | What it does |
|--------|--------------|
| `scout` | Market research analyst — discovers trends, analyzes competitors, identifies opportunities |
| `sage` | Strategic advisor — synthesizes research into actionable recommendations |
| `scribe` | Content writer — reports, proposals, and client deliverables |
| `demo-researcher` | Minimal autonomous researcher (gathers + summarizes); good first deploy |
| `demo-analyst` | Minimal analysis agent — answers strategic questions over research findings |
| `trinity-system` | Platform operations manager — agent health, lifecycle, fleet management (system-scoped) |

`scout` → `sage` → `scribe` are designed to work as a **consulting team**: Scout researches into a shared folder, Sage strategizes over it, Scribe writes the deliverable. Deploy them together to see agent-to-agent collaboration.

### Due-diligence suite (coordinated multi-agent)

A startup due-diligence pipeline: `dd-intake` parses a pitch deck into structured data, nine specialists assess one dimension each, and `dd-lead` synthesizes a risk score and recommendation.

| `name` | Dimension |
|--------|-----------|
| `dd-intake` | Parse pitch decks → structured data for the specialists |
| `dd-lead` | Synthesize all findings → risk score + go/no-go |
| `dd-market` | TAM/SAM/SOM, growth, market headwinds |
| `dd-tech` | Technology, scalability, IP |
| `dd-founder` | Background checks, track record, controversy |
| `dd-traction` | Growth metrics, financial health, data accuracy |
| `dd-bizmodel` | Revenue model, unit economics, path to profitability |
| `dd-captable` | Equity structure, dilution, investor reputation |
| `dd-competitor` | Competitive landscape, market share, threats |
| `dd-compliance` | Regulatory landscape, compliance, market entry |
| `dd-legal` | Corporate structure, IP ownership, contracts |

## Not starting points

`test-echo`, `test-counter`, `test-delegator`, `test-codex`, `test-gemini`, `test-leak-hook`, and `sleep-echo` are internal **test/canary fixtures** used by the test suite and the canary harness — not templates to build on.
