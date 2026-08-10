# Trinity Docs Q&A

**ID**: DOCS-QA-001  
**Status**: Implemented  
**Added**: 2026-04-18

## Overview

Public conversational Q&A system for Trinity documentation, powered by Vertex AI Search with Gemini LLM. Users can ask questions about Trinity and receive grounded answers with citations from the onboarding documentation.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         Trinity Docs Q&A                                 │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  docs/onboarding/*.md                                                    │
│         │                                                                │
│         │ GitHub Action (on push)                                        │
│         ▼                                                                │
│  ┌─────────────────────┐    ┌─────────────────────┐                     │
│  │   GCS Bucket        │───▶│ Vertex AI Search    │                     │
│  │   (txt conversion)  │    │ Data Store          │                     │
│  └─────────────────────┘    └──────────┬──────────┘                     │
│                                        │                                 │
│                              ┌─────────┴──────────┐                     │
│                              │  Search Engine     │                     │
│                              │  (Gemini LLM)      │                     │
│                              └─────────┬──────────┘                     │
│                                        │                                 │
│                              ┌─────────┴──────────┐                     │
│                              │  Cloud Function    │                     │
│                              │  (public endpoint) │                     │
│                              └─────────┬──────────┘                     │
│                                        │                                 │
│                    ┌───────────────────┼───────────────────┐            │
│                    ▼                   ▼                   ▼            │
│              ask-trinity.sh       curl/REST           UI + MCP          │
│                                                                          │
└─────────────────────────────────────────────────────────────────────────┘
```

Consumers: `scripts/ask-trinity.sh`, raw REST, the docs-site assistant, the
in-app Help widget (#391), and the standalone `trinity-docs-mcp` MCP server
(#1459 — see [MCP Distribution](#mcp-distribution--trinity-docs-mcp-1459)).

## Components

### GCP Resources

| Resource | ID | Description |
|----------|-----|-------------|
| Project | `mcp-server-project-455215` | GCP project |
| GCS Bucket | `trinity-docs-rag-mcp-server-project-455215` | Document storage |
| Data Store | `trinity-docs` | Vertex AI Search data store |
| Search Engine | `trinity-search` | Search engine with LLM add-on |
| Cloud Function | `ask-trinity` | Public HTTP endpoint |
| Workload Identity Pool | `github-actions` | GitHub Actions auth |
| Service Account | `trinity-docs-sync` | GCS + Discovery Engine access |

### Files

| File | Purpose |
|------|---------|
| `.github/workflows/sync-docs-to-vertex.yml` | Auto-sync docs to GCS on push |
| `scripts/ask-trinity.sh` | CLI tool for querying |
| `docs/onboarding/*.md` | Source documentation (onboarding) |
| `docs/user-docs/**/*.md` | Source documentation (user docs incl. the 264-Q&A FAQ) |
| `docs/TRINITY_COMPATIBLE_AGENT_GUIDE.md` | Source documentation (agent guide, #1459) |
| `src/helper-mcp/` | Standalone MCP server exposing the endpoint (`trinity-docs-mcp`, #1459) |

## Data Flow

### Document Sync (GitHub Action)

1. Push to `docs/onboarding/*.md` triggers workflow
2. Workflow authenticates via Workload Identity Federation
3. Markdown files converted to `.txt` (Vertex AI requirement)
4. Files synced to `gs://trinity-docs-rag-*/txt/`
5. Document re-import triggered via Discovery Engine API
6. Vertex AI indexes and chunks documents

### Query Flow

1. User sends question via `ask-trinity.sh` or direct curl
2. Cloud Function receives request (no auth required)
3. Function calls Vertex AI Search Answer API
4. Gemini 2.0 Flash generates answer from indexed docs
5. Response includes answer text and citation references

## API

### Public Endpoint

```
POST https://us-central1-mcp-server-project-455215.cloudfunctions.net/ask-trinity
Content-Type: application/json

{
  "question": "How do I create an agent?",
  "session_id": "optional-for-multi-turn"
}
```

**Response:**
```json
{
  "answer": "To create an agent in Trinity...",
  "state": "SUCCEEDED",
  "session_id": "7547107641198884380"
}
```

**Contract notes (verified live, 2026-07-12 — #1459):**

- **No citations field.** The Cloud Function does not surface the Vertex AI
  Search citation references in its payload — consumers get `answer`/`state`/
  `session_id` only. (A CF enhancement would light up citation pass-through in
  the MCP adapters automatically.)
- **Session expiry is silent.** An expired or invalid `session_id` returns
  HTTP 200 with `state: SUCCEEDED`, a normal answer, and a **new**
  `session_id` — context is lost with no failure signal. Consumers doing
  multi-turn must compare the returned `session_id` to the one they sent.
- **`session_id` exceeds 2^53.** Treat it as an opaque string; numeric JSON
  handling silently corrupts it.
- Errors return `{"error": "..."}` JSON (e.g. HTTP 400 on a missing question);
  Google frontend failures may return HTML.

### Multi-Turn Chat

The endpoint supports conversational sessions with context memory:

```javascript
// First message - creates new session
const res1 = await fetch(ENDPOINT, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ question: "What are agents?" })
});
const { answer, session_id } = await res1.json();

// Follow-up - continues conversation
const res2 = await fetch(ENDPOINT, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ 
    question: "How do I create one?",  // "one" resolves to "agent"
    session_id: session_id
  })
});
```

Sessions persist ~30 minutes of inactivity. Context carries across turns.

### CLI Usage

```bash
./scripts/ask-trinity.sh "How do I add credentials to an agent?"
```

## Tone & Personality

The assistant has a baked-in personality via system prompt:

- **Markdown formatted** — headers, bullets, code blocks
- **Friendly & witty** — casual language, emojis, personality
- **Simple explanations** — plain language, no jargon overload
- **Concise** — get to the point without being robotic

## Configuration

### GitHub Secrets

| Secret | Value |
|--------|-------|
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | `projects/667627606781/locations/global/workloadIdentityPools/github-actions/providers/github` |
| `GCP_SERVICE_ACCOUNT` | `trinity-docs-sync@mcp-server-project-455215.iam.gserviceaccount.com` |

### Service Account Roles

- `roles/storage.objectAdmin` — GCS bucket access
- `roles/discoveryengine.editor` — Document import

## Indexed Documents

The docs-sync workflow converts each source `.md` to `.txt` (Vertex AI Search
requirement) and syncs to GCS with a flattened name (`/` → `-`):

| Source | Content |
|--------|---------|
| `docs/onboarding/*.md` | Welcome, getting started, use cases, workflows, troubleshooting |
| `docs/user-docs/**/*.md` | Full user documentation — guides, API reference, integrations, operations, and the 264-Q&A FAQ |
| `docs/TRINITY_COMPATIBLE_AGENT_GUIDE.md` | Agent authoring guide (`get_agent_requirements` source, #1459) |

## Limitations

- **Markdown not supported**: Vertex AI Search requires `text/plain`, so `.md` files are converted to `.txt`
- **No real-time indexing**: Document changes require ~30s for re-indexing
- **Context window**: Large questions may be truncated
- **Session timeout**: Sessions expire after ~30 min of inactivity

## In-App Help Widget (#391)

A floating help chat widget provides instant access to Trinity documentation from within the UI.

### Components

| File | Purpose |
|------|---------|
| `src/frontend/src/components/HelpChatWidget.vue` | Floating button + expandable chat panel |
| `src/frontend/src/App.vue` | Mounts widget for authenticated users |

### Features

- Floating button in bottom-right corner (collapsible)
- Chat panel with message history
- Multi-turn conversations via session persistence (localStorage)
- Markdown rendering with DOMPurify sanitization
- Loading indicator while waiting for response
- Error handling with retry button
- Keyboard navigation and focus trap
- ARIA labels for accessibility
- "New conversation" button to reset session

### User Flow

```
User clicks help button → Panel opens → Types question → Enter to send
    → Loading indicator → Response renders with markdown
    → Continue conversation or start new one
```

### Session Persistence

- Session ID stored in `localStorage` key `trinity_help_session_id`
- Sessions persist ~30 min server-side (Vertex AI Search limit)
- Conversation history shown in-panel during session
- "New conversation" clears local messages and session ID

## MCP Distribution — `trinity-docs-mcp` (#1459)

A standalone, dependency-light MCP server (`src/helper-mcp/`) exposes this
endpoint to any MCP client (Claude Code, Claude Desktop, Cursor) — **no Trinity
instance or API key required**. Pure protocol adapter: no new backend/QA logic,
no Cloud Function changes.

- **Tools**: `ask_trinity` (question + optional `session_id` multi-turn — always
  returns the effective `session_id` and warns when a silent session reset
  dropped context) and `get_agent_requirements` (fetches the agent guide from
  raw.githubusercontent.com live; quick-reference fallback on failure).
- **Guards**: 4,000-char question cap, 50s abort timeout, no auto-retry,
  `redirect: "error"`, non-JSON response guard, structured error text — a tool
  call never crashes the server. `console.error`-only logging (stdout is the
  JSON-RPC channel). Node ≥18 guarded at startup by the bin stub.
- **Config**: `ASK_TRINITY_ENDPOINT` env override (self-hosted mirrors, smoke
  test); default is the public Cloud Function.
- **Distribution**: npx stdio package, runtime deps `@modelcontextprotocol/sdk`
  + `zod` only. CI: `.github/workflows/helper-mcp-test.yml` (unit + pack-and-run
  stdio smoke). Publish: `.github/workflows/publish-helper-mcp.yml` (npm
  provenance; one-time manual first publish bootstraps trusted publishing —
  see workflow header comment).
- **Contract sharing**: tool name/schema kept identical to the planned
  `ask_trinity` inside the main Trinity MCP server (#1460) so the two surfaces
  never drift.
- **Deferred**: hosted remote Streamable-HTTP variant + vanity URL + MCP
  registry listing (the official SDK keeps the transport option open).

Requirements: `docs/memory/requirements/mcp.md` → "Trinity Helper MCP Server (#1459)".

## Future Enhancements

- [x] ~~Add more docs~~ (done: user-docs incl. API reference + FAQ; agent guide added by #1459)
- [x] ~~Integrate into Trinity UI as help widget~~ (done: #391)
- [x] ~~Add conversation memory for follow-up questions~~ (done: session support)
- [ ] Surface Vertex AI Search citations in the Cloud Function payload (consumers pass them through automatically)
- [ ] Hosted remote MCP endpoint (Streamable HTTP) + MCP registry listing (#1459 fast-follow)
- [ ] Support for code snippets with syntax highlighting
- [ ] Usage analytics/telemetry

## Related

- [Vertex AI Search Console](https://console.cloud.google.com/gen-app-builder/engines?project=mcp-server-project-455215)
- [Cloud Function](https://console.cloud.google.com/functions/details/us-central1/ask-trinity?project=mcp-server-project-455215)
- [GCS Bucket](https://console.cloud.google.com/storage/browser/trinity-docs-rag-mcp-server-project-455215)
