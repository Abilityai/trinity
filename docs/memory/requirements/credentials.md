# Requirements — Credential Management

> Part of Trinity's requirements set. Index & write-path rule: [requirements.md](../requirements.md).

---

## 3. Credential Management

### 3.1 Manual Credential Entry
- **Status**: ✅ Implemented
- **Description**: Add credentials via UI form with name, value, service

### 3.2 OAuth2 Flows
- **Status**: ✅ Implemented
- **Description**: OAuth2 authentication for Google, Slack, GitHub, Notion
- **Key Features**: MCP-compatible credential normalization

### 3.3 Credential Hot-Reload
- **Status**: ✅ Implemented
- **Description**: Update credentials on running agents without restart
- **Key Features**: Hot-reload via UI paste, writes `.env` and regenerates `.mcp.json`

### 3.4 Bulk Credential Import
- **Status**: ✅ Implemented
- **Description**: Paste `.env`-style KEY=VALUE pairs with template selector

### 3.5 Credential Requirements Declaration & Surfacing
- **Status**: ✅ Declaration schema shipped + surfaced on the catalog (ent#128); guided setup UI is ent#127
- **Description**: A template declares which credentials it needs in `credentials:`
  (names only, frozen) and optionally describes each one in the sibling
  `credential_setup:` (title / description / required / secret / format /
  setup_url / default). `template_service.normalize_credential_requirements()`
  joins the two BY NAME — base-set-plus-overlay, so `credential_setup:` can only
  decorate and the pair cannot drift — and every catalog entry carries
  `credential_requirements` plus a `required_credentials` count with
  platform-injected vars excluded.
- **Contract**: [`docs/schemas/trinity-agent-credentials.schema.json`](../../schemas/trinity-agent-credentials.schema.json)
  (authoritative), authoring guide in `docs/TRINITY_COMPATIBLE_AGENT_GUIDE.md` →
  "Declaring Credentials"
- **Correction (ent#128)**: this entry previously read "✅ Implemented — extract
  from `.mcp.json.template` and show configured vs missing status". Neither half
  was true: `extract_agent_credentials` (the extractor that reads
  `.mcp.json.template`) has **no production caller**, and no surface showed
  configured-vs-missing status — the catalog badge read a top-level
  `required_credentials:` key that no template defines, so it rendered 0 for
  everything. What ships now is the declaration schema and the catalog surface;
  the per-credential "configured vs missing" checklist is ent#127.

---
