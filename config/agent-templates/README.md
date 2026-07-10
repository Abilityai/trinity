# Agent template catalog

Ready-made starting points for deploying an agent to Trinity. Each subdirectory is one template — a `CLAUDE.md` (the agent's instructions) plus a `template.yaml` (metadata: `name`, `description`, `type`, `capabilities`, `commands`, `metrics`, `folders`). Pick one as your starting point instead of authoring an agent from scratch.

> New here? The repo's agent entry point is [`AGENTS.md`](../../AGENTS.md) → [Deploy an agent](../../AGENTS.md#deploy-an-agent-to-trinity). Full template schema: [`docs/TRINITY_COMPATIBLE_AGENT_GUIDE.md`](../../docs/TRINITY_COMPATIBLE_AGENT_GUIDE.md).

## How to use a template

- **From a checkout** — copy a template directory, edit its `CLAUDE.md` + `template.yaml`, then deploy it: `cd <copy>/ && trinity deploy .` (see [Deploy an agent](../../AGENTS.md#deploy-an-agent-to-trinity)).
- **From the web UI** — *Create Agent* → pick a template by `name`.
- **By reference** — these `name`s are the built-in templates the platform ships; they appear in `GET /api/templates` and `list_templates` (MCP). Directories marked `hidden: true` in their `template.yaml` (see [Not starting points](#not-starting-points)) are excluded from that catalog but stay deployable by id (e.g. `local:test-echo`).

## Starter templates

### Single-purpose

| `name` | What it does |
|--------|--------------|
| `scout` | Market research analyst — discovers trends, analyzes competitors, identifies opportunities |
| `sage` | Strategic advisor — synthesizes research into actionable recommendations |
| `scribe` | Content writer — reports, proposals, and client deliverables |

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

These directories are **excluded from the user-facing catalog** (`GET /api/templates` / `list_templates`) — each carries `hidden: true` in its `template.yaml`, so the exclusion is machine-enforced, not a naming convention. They stay deployable by id (e.g. `local:test-echo`) for the test/canary harness and demo scripts.

- **Test/canary fixtures** — `test-echo`, `test-counter`, `test-delegator`, `test-codex`, `test-gemini`, `test-leak-hook`, `sleep-echo`. Used by the test suite and the canary harness; `test-leak-hook` is a deliberately hazardous subprocess-leak repro — do **not** deploy it in production.
- **Demo fixtures** — `demo-researcher`, `demo-analyst`. A minimal producer→consumer shared-folder pair deployed together by the demo-fleet manifests, not a starting point to build on.
- **Platform agent** — `trinity-system`. Auto-deployed and deletion-protected platform-operations agent; not something you create yourself.

Adding a new fixture? Set `hidden: true` in its `template.yaml` so it never leaks into the catalog (a unit test guards this — see `tests/unit/test_local_templates_listing.py`).
