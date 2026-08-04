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

### 3.6 Guided Credential Setup (ent#127)
- **Status**: ✅ Implemented
- **Endpoint**: `GET /api/agents/{name}/credential-requirements` — **owner-only
  AND human-only** (`get_owned_agent_by_name` + `reject_agent_principal`). The
  credential inventory is an operator surface, so the read gate equals the write
  gate it drives: `get_authorized_agent_by_name` resolves an agent-scoped MCP key
  to the owner user carrying the owner's role, which would hand an agent the
  inventory of every sibling its owner can access — the whole fleet on a default
  admin-owned install. The coarse `/credentials/status` keeps the read gate
  because it returns a count and names nothing.
- **Description**: joins the §3.5 declaration (`normalize_credential_requirements`,
  `source_trust="deployed"`) against live in-container state. Status is collected
  by a fixed base64 `docker exec` probe (the #668 collector pattern, deliberately
  a SEPARATE probe — compat treats `.env` as existence-only by design and its
  payload feeds AI checks) returning **key names with a non-empty value, never
  values**, projected backend-side onto the declared set so an undeclared
  operator-added variable is never disclosed. No agent-server endpoint, so it
  works on the entire existing fleet the moment the backend updates — AC #3's
  audience is the ent#123 tokenless seeded fleet, and a checklist inert for
  every pre-release agent fails its own audience.
- **"Set" is defined as agreement with the agent's own exporter**
  (`agent_server/routers/credentials.py`), pinned by a parity test. A fresh
  `local:` agent's generated `.env` carries `KEY=` for every `env_file:`
  variable and a `github:` agent has no `.env` at all — absent ⇒ **missing**,
  never unknown; `unknown` means "we could not look".
- **States**: `ok` · `no_credentials_required` (a first-class ready state) ·
  `declaration_incomplete` (nothing declared but `${VAR}`s referenced in
  `.mcp.json.template` / `.env.example` — advisory rows, never required, never
  blocking; `.mcp.json.template` does not become a declaration authority) ·
  `degraded`, which **dominates the empty state unconditionally** because a
  degraded lookup and a genuinely credential-free agent produce an identical
  empty list and "Ready" is the one state nobody investigates.
- **Tri-states preserved**: per-variable `status` (set/missing/unknown) and
  `required` (true/false/`"unknown"`); platform-injected variables are excluded
  from the rows and counted separately.
- **`setup_url` rendering**: UTS-46 nontransitional canonical host with eTLD+1
  emphasis, failing **closed** to inert text (`services/setup_url_display.py`).
  A mitigation of the schema's IDN residual, not a closure — it does not catch
  subdomain deception, typosquats or a hostile destination behind a correct host.
- **Writes** reuse the existing owner-gated inject path — no new write surface.
- **Flow**: [`feature-flows/guided-credential-setup.md`](../feature-flows/guided-credential-setup.md)

---
