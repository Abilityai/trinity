# Trinity Docs MCP (`@abilityai/trinity-docs-mcp`)

Ask Trinity anything, from any MCP client. A standalone [Model Context
Protocol](https://modelcontextprotocol.io) server that exposes the
[Trinity](https://github.com/abilityai/trinity) documentation Q&A assistant
(Vertex AI Search + Gemini, grounded on Trinity's docs) as MCP tools.

**No Trinity instance, no API key, no credentials** — it talks to the same
public endpoint that powers Trinity's docs-site assistant and in-app Help
widget. Answers are AI-generated from Trinity's public documentation and may
be inaccurate; treat them as guidance.

## Install

### Claude Code

```bash
claude mcp add trinity-docs -- npx -y @abilityai/trinity-docs-mcp
```

### Claude Desktop / Cursor / any MCP client

```json
{
  "mcpServers": {
    "trinity-docs": {
      "command": "npx",
      "args": ["-y", "@abilityai/trinity-docs-mcp"]
    }
  }
}
```

Requires Node.js ≥ 18 (`npx` ships with it).

## Tools

| Tool | Description |
|------|-------------|
| `ask_trinity` | Ask a question about Trinity; returns a grounded answer. Pass the returned `session_id` back for multi-turn follow-ups (sessions expire after ~30 min of inactivity — an expired session silently starts fresh, and the response says so). |
| `get_agent_requirements` | The complete Trinity Compatible Agent Guide (required files, `template.yaml` schema, credential management), fetched live from the Trinity repository. |

## Configuration

| Env var | Default | Purpose |
|---------|---------|---------|
| `ASK_TRINITY_ENDPOINT` | the public Trinity docs Q&A endpoint | Point at a self-hosted mirror of the answer API. Logged to stderr when set. |

## Troubleshooting

- **"server disconnected" immediately** — check Node version: `node --version`
  must be ≥ 18. The launcher prints a clear message to the client's MCP logs.
- **Stale version after an update** — `npx` caches packages; clear with
  `npx clear-npx-cache` or pin a version: `npx -y @abilityai/trinity-docs-mcp@latest`.
- **Slow first call** — the answer API generates responses with an LLM; expect
  seconds. The server waits up to 50s before reporting a timeout.

## Development

This package lives in the Trinity monorepo at `src/helper-mcp/` (decision
recorded in issue [#1459](https://github.com/abilityai/trinity/issues/1459):
in-repo for CI/review co-location; npm distribution makes the repo location
invisible to users). It is deliberately **not** the
[Trinity MCP server](../mcp-server/) — that one orchestrates a Trinity
instance's agent fleet and requires a Trinity API key; this one answers
questions about Trinity for anyone.

```bash
npm ci          # install
npm run build   # tsc → dist/
npm test        # unit tests (mocked fetch, node --test)
npm run smoke   # pack-and-run stdio smoke test against a local mock endpoint
```

Runtime dependencies are intentionally minimal (`@modelcontextprotocol/sdk` +
`zod`). Keep it that way — every transitive dependency is supply-chain surface
for a public npx package. The package is permanently credential-free by design.

Released via `.github/workflows/publish-helper-mcp.yml` (npm provenance;
tag `helper-mcp-vX.Y.Z` or auto-patch on `main` pushes touching this
directory).
