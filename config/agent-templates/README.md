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

These three are the **whole** visible catalog. Looking for more? The canonical way to build an agent is the [`abilityai/abilities`](https://github.com/abilityai/abilities) marketplace and its `create-agent` wizards — not a longer list of directories here.

## Not starting points

These directories are **excluded from the user-facing catalog** (`GET /api/templates` / `list_templates`) — each carries `hidden: true` in its `template.yaml`, so the exclusion is machine-enforced, not a naming convention. They stay deployable by id (e.g. `local:test-echo`) for the test/canary harness and demo scripts.

- **Test/canary fixtures** — `test-echo`, `test-counter`, `test-delegator`, `test-codex`, `test-gemini`, `test-leak-hook`, `sleep-echo`. Used by the test suite and the canary harness; `test-leak-hook` is a deliberately hazardous subprocess-leak repro — do **not** deploy it in production.
- **Demo fixtures** — `demo-researcher`, `demo-analyst`. A minimal producer→consumer shared-folder pair deployed together by [`config/manifests/research-network.yaml`](../manifests/research-network.yaml), not a starting point to build on.
- **Platform agent** — `trinity-system`. Auto-deployed and deletion-protected platform-operations agent; not something you create yourself.
- **Minimal reference** — `default`. The smallest template that still resolves: no tools, no schedules, no shared folders. It backs the `local:default` id used by the manifest/API documentation and the integration suites, so those examples deploy a real (if empty) agent instead of silently producing a blank container (#1759). To start a genuinely empty agent from the UI, use **Blank Agent** — not this.

### Demo fleet — VC due diligence

A startup due-diligence pipeline: `dd-intake` parses a pitch deck into structured data, nine specialists assess one dimension each, and `dd-lead` synthesizes a risk score and recommendation. It is a **demo we still run**, not a starting point to build on, so all eleven directories carry `hidden: true` (#1931).

Deploy the fleet as a set with [`config/manifests/vc-due-diligence.yaml`](../manifests/vc-due-diligence.yaml) (`POST /api/systems/deploy`, or the `deploy_system` MCP tool) — that manifest wires the nine `dd-lead` → specialist permissions the demo needs. Each template also stays individually creatable by id (`local:dd-lead`). Read the manifest's header before deploying: eleven containers, ~40 GB of declared memory limits, and the agent names are load-bearing.

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

## Declaring catalog intent

Every directory here declares `hidden:` **explicitly** in its `template.yaml` — `true` for a fixture, demo fleet, or platform agent; `false` for a starter we stand behind as someone's first agent. Omitting the key fails CI (`tests/unit/test_1931_catalog_intent.py`), so a new template can never *default* its way into the user-facing catalog. The same guard pins the visible set, and `tests/unit/test_local_templates_listing.py` separately refuses a visible `test-` / `demo-` / `dd-`-prefixed directory.

The **runtime** default is deliberately unchanged: an absent `hidden:` still lists. Flipping it would turn a forgotten key into a silent absence, which is a worse failure than the visible-demo-template one this rule exists to prevent — so the declaration is enforced at CI time, where it fails loudly.
