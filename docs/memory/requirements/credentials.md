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

### 3.7 Platform Credential Settings Encrypted at Rest (ent#435)
- **Status**: ✅ Implemented
- **Description**: The six `system_settings` rows that hold live third-party
  credentials — `anthropic_api_key`, `github_pat`, `google_api_key`,
  `slack_app_token`, `slack_client_secret`, `slack_signing_secret` — are
  persisted as AES-256-GCM envelopes under `<key>_encrypted`, never in cleartext
  (CWE-312). Closes the gap where Architectural Invariant #12's own table read as
  though everything was covered while these six were readable by any DB dump,
  backup, replica or snapshot **without** `CREDENTIAL_ENCRYPTION_KEY`.
- **The key NAME moves, not only the value**: the cleartext row is DELETED. A
  same-named key that may hold either form leaves "is this install encrypted?"
  unanswerable by inspection, which is the reported defect rather than a
  cosmetic detail; with the rename, `SELECT key FROM system_settings WHERE key
  IN (…)` returning nothing is itself the verification.
- **Sink guard**: `db.set_setting` raises `SecretSettingWriteError` (mapped to
  422) for a registered secret key **or** any merely credential-*shaped* key
  (`*_api_key` / `*_token` / `*_secret` / `*_pat` / `*_password` /
  `*_credentials`). It lives at the sink because the generic
  `PUT /api/settings/{key}` catch-all can address any key — the door #506,
  #1609, ent#12, #1644, ent#14 and ent#346 each found open — and because
  `system_settings` has more than one writer.
- **Lazy migration on read**: resolution is encrypted → legacy-cleartext
  (encrypted-and-deleted on sight) → env → `''`. A one-shot migration converts
  what is on disk once; the read path is what makes cleartext *transient* rather
  than merely absent, since a restored pre-fix backup or a direct DB write can
  put it back. Steady state costs one read and zero writes.
- **Fail direction is asymmetric on purpose**: fail-OPEN on read (an unreadable
  envelope degrades to the env var, never a 500 on the agent-start path) but
  never down to a stale legacy row, which would resurrect a replaced credential;
  fail-CLOSED on write (no encryption key ⇒ refuse, never silently store
  cleartext).
- **Documented exemption**: `slack_client_id` stays a plain row — an OAuth
  client_id is a public identifier emitted verbatim in the browser-visible
  authorize URL (the `whatsapp_bindings.account_sid` "(public)" precedent). It is
  recorded with its reason in `PUBLIC_CREDENTIAL_SHAPED_KEYS` so a later reader
  can tell *reviewed* from *overlooked*.
- **Dual-track** (Invariant #9): `secret_settings_encryption` (SQLite) + Alembic
  `0041_secret_settings_encryption` (PostgreSQL — the backend the defect was
  reported on). Both call one `plan_migration`, because the two drivers cannot
  share SQL but must not disagree on policy. Hard-fails on a missing encryption
  key only when there is something to encrypt, so a fresh install still boots.
  `downgrade()` is a deliberate no-op: the honest inverse is "write these live
  credentials back in cleartext".
- **Rotation**: `scripts/deploy/rotate-credential-key.py` gains a row-keyed
  `system_settings` pass, which also closes the pre-existing gap that left
  `elevenlabs_api_key_encrypted` / `a2a_outbound_endpoints_encrypted` out of
  every key rotation (an envelope-in-a-row is invisible to a column sweep).
- **Operator follow-up**: encryption protects the DB going forward only —
  historical backups still hold the plaintext, so the affected tokens must be
  rotated. Runbook:
  [`docs/migrations/SECRET_SETTINGS_ENCRYPTION_2026-08.md`](../../migrations/SECRET_SETTINGS_ENCRYPTION_2026-08.md).
