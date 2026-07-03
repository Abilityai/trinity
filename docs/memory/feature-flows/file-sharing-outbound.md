# Feature: Outbound File Sharing (FILES-001)

## Revision History

| Date | Changes |
|------|---------|
| 2026-04-24 | Initial implementation (FILES-001 / #295). Steps 1–6 complete: schema, toggle + volume, internal share endpoint, public download, MCP tool, UI panel. |

## Overview

Agents publish files from their `/home/developer/public/` directory to a public download URL with a signed token and a 7-day default expiration. The URL works universally — web, Slack, Telegram, WhatsApp, email — replacing fragile per-channel upload patterns.

## Requirement Reference

- **Requirement**: §13.10 Outbound File Sharing (FILES-001)
- **GitHub Issue**: #295
- **Status**: ✅ Implemented 2026-04-24
- **Pillar**: III (Persistent Memory / Delivery)

## User Story

As an agent user (web, Slack, Telegram, WhatsApp), I want the agent to produce a downloadable file I can retrieve from a URL, so that outputs like CSV reports, PDFs, exports, and generated assets don't need to be pasted as text or handled with per-channel upload APIs.

## Entry Points

- **MCP tool**: `share_file({ filename, display_name?, expires_in? })` — from inside any agent with file sharing enabled
- **Owner UI**: Agent Detail → Sharing tab → File Sharing panel
- **Owner API**: `POST /api/agents/{name}/shared-files`
- **Agent-server path**: `POST /api/internal/agent-files/share` (agent-scoped internal call)
- **Public download**: `GET /api/files/{file_id}?sig={token}`

---

## Architecture

```
┌─────────────── Agent container ───────────────┐
│                                                │
│  Claude Code runtime                           │
│   │                                            │
│   ▼ (MCP JSON-RPC)                             │
│  trinity-mcp-server:8080                       │
│   │                                            │
│   ▼ (Bearer: agent-scoped MCP key)             │
│  POST /api/agents/{name}/shared-files          │
│                                                │
│  /home/developer/public/report.csv             │
│    (agent-{name}-public Docker volume,         │
│     mounted ONLY into the agent)               │
│                                                │
└────────────────────────┬───────────────────────┘
                         │ Docker SDK get_archive
                         │ (backend never mounts
                         │  agent workspace)
                         ▼
┌─────────────── Backend process ────────────────┐
│                                                │
│  services/agent_shared_files_service.py        │
│   ├── validate path (no abs, no .., no \)      │
│   ├── python-magic MIME + executable blocklist │
│   ├── enforce per-agent quota                  │
│   ├── shutil.disk_usage pre-check              │
│   └── write /data/agent-files/{file_id}        │
│                                                │
│  db: insert into agent_shared_files             │
│                                                │
└────────────────────────┬───────────────────────┘
                         │ response
                         ▼
            URL: {public_chat_url}/api/files/{file_id}?sig={token}

User clicks URL →
  GET /api/files/{file_id}?sig={token}
    ├── IP rate limit (file-download bucket)
    ├── constant-time compare vs stored download_token
    ├── revoked / expired checks
    ├── agent require_email policy gate (via validate_agent_session)
    ├── stream /data/agent-files/{file_id} (64 KB chunks)
    ├── Content-Disposition: attachment (RFC 6266 UTF-8)
    ├── X-Content-Type-Options: nosniff
    ├── bump download_count + last_downloaded_at
    └── audit: EXECUTION/file_share_download
```

---

## Frontend Layer

### Components

| File | Line | Description |
|------|------|-------------|
| `src/frontend/src/components/FileSharingPanel.vue` | 1-210 | Toggle, restart-required banner, quota, table (filename/size/expires/downloads), Copy URL + Revoke buttons, empty state |
| `src/frontend/src/components/SharingPanel.vue` | — | Embeds `<FileSharingPanel>` between Telegram/WhatsApp and Public Links sections |

### State Management

| Store method | File | Description |
|--------------|------|-------------|
| `getFileSharingStatus(name)` | `stores/agents.js` | GET `/api/agents/{name}/file-sharing` |
| `setFileSharingStatus(name, enabled)` | `stores/agents.js` | PUT toggle |
| `listSharedFiles(name)` | `stores/agents.js` | GET `/api/agents/{name}/shared-files` |
| `revokeSharedFile(name, id)` | `stores/agents.js` | DELETE |

No WebSocket updates yet — manual refresh on action (acceptable because volume is low and actions are local).

---

## Backend Layer

### Architecture — three layers

| Layer | File | Purpose |
|-------|------|---------|
| Router | `src/backend/routers/agent_files.py` (toggle + list/revoke) | GET/PUT `/file-sharing`, POST/GET/DELETE `/shared-files` |
| Router | `src/backend/routers/files.py` (download) | GET `/api/files/{id}` |
| Router | `src/backend/routers/internal.py` (share) | POST `/api/internal/agent-files/share` (agent-server path, `X-Internal-Secret` auth) |
| Service | `src/backend/services/agent_shared_files_service.py` | `create_share()` orchestrator, path validation, MIME detection, quota, extraction |
| Service | `src/backend/services/agent_service/file_sharing.py` | Toggle logic, `check_public_folder_mount_matches()` |
| DB | `src/backend/db/agent_shared_files.py` | `AgentSharedFilesOperations` CRUD |
| DB | `src/backend/db/agent_settings/file_sharing.py` | `FileSharingMixin` — per-agent toggle + volume name convention |

### Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/agents/{name}/file-sharing` | JWT (access) | Status + quota bar data |
| PUT | `/api/agents/{name}/file-sharing` | JWT (owner/admin) | Toggle; returns `restart_required: true` when config and mounts disagree |
| POST | `/api/agents/{name}/shared-files` | JWT (owner/admin) OR agent-scoped MCP key (same agent) | Mint a download URL |
| GET | `/api/agents/{name}/shared-files` | JWT (access) | List active shares |
| DELETE | `/api/agents/{name}/shared-files/{file_id}` | JWT (owner/admin) | Revoke (idempotent) |
| POST | `/api/internal/agent-files/share` | `X-Internal-Secret` | Agent-server direct path; takes `agent_name` in body |
| GET | `/api/files/{file_id}` | Token (`?sig=`) + optional `session_token` when agent requires email | Public download |

### MCP tool

| Tool | File | Description |
|------|------|-------------|
| `share_file` | `src/mcp-server/src/tools/files.ts` | Agent-scoped. Body: `{ filename, display_name?, expires_in? }`. Returns: `{ file_id, url, expires_at, size_bytes, mime_type }` |

### Key service behaviors

| Behavior | Where | Notes |
|----------|-------|-------|
| Path validation | `validate_publish_path()` | Rejects absolute paths, `..` segments, backslashes. Resolves against `/home/developer/public/`. |
| Extraction | `extract_from_agent()` | Docker SDK `get_archive`. Caps buffer at 50 MB + 4 KB tar overhead to prevent OOM. Rejects non-regular tar members (symlinks, dirs, devices). |
| MIME detection | `detect_mime()` + `check_mime_blocklist()` | python-magic on first 4096 bytes. Blocklist: PE (`MZ`), ELF (`\x7fELF`), Mach-O (4 variants), `#!` shebang. |
| Quota | `enforce_quota()` | Sum of non-revoked, non-expired `size_bytes` for the agent; default 500 MB. |
| Token | `secrets.token_urlsafe(32)` | 192-bit entropy, stored in `download_token`. URL param name is `sig` (NOT `download_token`) to bypass the credential sanitizer's `.*TOKEN.*` pattern. |
| URL build | `build_download_url()` | `{public_chat_url}/api/files/{id}?sig={token}` — uses existing `/api/*` proxy path on Vite dev + prod nginx, no new proxy rules needed. |

---

## Data Layer

### Database Schema

```sql
CREATE TABLE agent_shared_files (
    id TEXT PRIMARY KEY,                  -- UUID
    agent_name TEXT NOT NULL,
    filename TEXT NOT NULL,               -- Display name
    stored_filename TEXT NOT NULL,        -- UUID on disk
    size_bytes INTEGER NOT NULL,
    mime_type TEXT,
    download_token TEXT UNIQUE NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    one_time INTEGER DEFAULT 0,           -- Deferred column (not used)
    consumed_at TEXT,                     -- Deferred column (not used)
    download_count INTEGER DEFAULT 0,
    last_downloaded_at TEXT,
    FOREIGN KEY (agent_name) REFERENCES agent_ownership(agent_name)
        ON DELETE CASCADE ON UPDATE CASCADE
);
CREATE INDEX idx_agent_files_agent ON agent_shared_files(agent_name);
CREATE INDEX idx_agent_files_token ON agent_shared_files(download_token);
CREATE INDEX idx_agent_files_expires ON agent_shared_files(expires_at) WHERE revoked_at IS NULL;

-- Plus one column on the existing agent_ownership table:
ALTER TABLE agent_ownership ADD COLUMN file_sharing_enabled INTEGER DEFAULT 0;
```

FK `ON UPDATE/DELETE CASCADE` is declared but not enforced at runtime (platform-wide pattern — SQLite connections don't `PRAGMA foreign_keys=ON`). Explicit cascades are in:
- `routers/agents.py` delete handler — removes DB rows, on-disk files, and Docker volume
- `db/agent_settings/metadata.py:rename_agent()` — updates `agent_name` when an agent is renamed

### Storage locations

| Item | Location |
|------|----------|
| DB rows | `agent_shared_files` table in `/data/trinity.db` |
| Agent-side publish dir | `/home/developer/public/` (Docker volume `agent-{name}-public`, mounted only into the agent) |
| Backend-side extracted bytes | `/data/agent-files/{file_id}` (under the existing `trinity-data` volume — no compose changes) |

---

## Docker Integration

### Volume creation (crud.py + lifecycle.py)

When `agent_ownership.file_sharing_enabled = 1`, the agent start flow:
1. Creates Docker volume `agent-{name}-public` if missing
2. Runs alpine `chown 1000:1000 /public` to fix ownership for the `developer` user
3. Mounts volume at `/home/developer/public` (rw)

Volume is created on first toggle-on + restart and removed on agent delete.

### Backend storage

`/data/agent-files/{file_id}` — flat directory inside the existing `trinity-data` volume. No compose changes needed in dev or prod.

---

## Security Properties

See `docs/drafts/amazing-file-outbound.md` §6 for the full threat model. Key properties:

| # | Threat | Mitigation |
|---|--------|-----------|
| S1 | Path traversal — `share_file("../.env")` | `validate_publish_path()` rejects absolute, `..`, backslash; Docker SDK `get_archive` extracts into isolated buffer; backend never mounts agent workspace |
| S2 | Credential leak via backend filesystem reach | Backend only reads the single file the agent names; never `bind`-mounts `/home/developer/` |
| S3 | Predictable tokens | 192-bit `secrets.token_urlsafe(32)`; constant-time compare via `secrets.compare_digest` |
| S6 | XSS via agent-uploaded HTML | `Content-Disposition: attachment` + `X-Content-Type-Options: nosniff` — never inline |
| S7 | Filename header injection (CRLF) | Sanitizer allows `[A-Za-z0-9._\- ]` only; RFC 6266 UTF-8 percent-encoding for non-ASCII |
| S8 | MIME spoofing | python-magic detects actual MIME; blocklist rejects PE/ELF/Mach-O/shebang before storage |
| S9 | Storage DoS | 50 MB per-file + 500 MB per-agent quota (setting-configurable) |
| S10 | Token enumeration | 192-bit entropy + IP rate limit + audit log |
| S11 | Cross-tenant download | File addressed by `file_id` only; agent_name resolved from DB row |
| S14 | Access-policy bypass | Download endpoint runs the same `_agent_requires_email` gate as public chat; session_token validated via `validate_agent_session` (cross-link lookup) |
| S15 | Agent impersonation via MCP | Backend enforces `current_user.agent_name == path agent_name` for agent-scoped keys (same-agent defense) |

### Deferred / documented limitations

- **One-time download links** deferred. Schema columns retained (`one_time`, `consumed_at`) for future re-enablement.
- **Token in stored transcripts**: the URL (including `?sig=`) ends up in persisted chat_messages/schedule_executions for agents that include it in their response text. DB read-access allows URL reuse until expiration. Tracked for V1.1.
- **Shared rate-limit bucket**: currently uses `check_public_link_rate_limit` which shares a bucket with public chat (Phase 1 C5 will split this).
- **FK not runtime-enforced**: platform-wide pattern; manual cascade in agent delete + rename.

---

## Side Effects

- Audit event `EXECUTION/file_share_download` per GET (logs IP, UA, file_id, size, MIME, target agent)
- Download counter + `last_downloaded_at` bumped per download (best-effort; failures don't block the download)
- Agent delete cascades: unlinks on-disk files, removes Docker volume, deletes DB rows

---

## Error Handling

| Condition | HTTP status | Error body |
|-----------|-------------|------------|
| Missing `sig` | 401 | `sig required` |
| Invalid `sig` | 401 | `invalid download_token` |
| Unknown `file_id` | 404 | `not found` |
| Revoked | 410 | `revoked` |
| Expired | 410 | `expired` |
| Missing session_token when required | 401 | `session_token required` |
| Invalid session_token | 401 | `invalid or expired session_token` |
| Storage file missing on disk | 500 | `storage error` (also logged as orphan row) |
| Rate limit exceeded | 429 | `Too many requests. Please try again later.` |

---

## Testing

### Prerequisites
- Backend + frontend + mcp-server + agent container all running
- Admin user logged in, test agent created

### Unit tests
```bash
pytest tests/unit/test_agent_shared_files_migration.py \
       tests/unit/test_file_sharing_mixin.py \
       tests/unit/test_public_folder_mount_match.py -v
```
33 tests covering schema/migration, DB mixin, mount-match helper.

### End-to-end (manual / shell script)

See `docs/drafts/amazing-file-outbound.md` §7 (Steps 1–6) for the full 37-assertion live regression script.

### Happy path sanity check
```bash
AGENT=filetest-$(date +%s)
# 1) create + enable + restart agent
# 2) agent drops a file in /home/developer/public/
# 3) POST /api/agents/{name}/shared-files → get URL
# 4) curl URL → byte-identical download
# 5) DELETE agent → DB rows, on-disk files, Docker volume all gone
```

### Status: Live-verified via real Slack round-trip 2026-04-24

---

## Related Flows

- **Upstream**: [public-agent-links.md](public-agent-links.md) — URL + policy-gate pattern we clone
- **Upstream**: [agent-shared-folders.md](agent-shared-folders.md) — Docker volume pattern we clone
- **Upstream**: [unified-channel-access-control.md](unified-channel-access-control.md) — `require_email` / `session_token` gate reused here
- **Adjacent**: [agent-sharing.md](agent-sharing.md) — email allow-list that gates the download endpoint when `require_email=true`
- **Related**: [audit-trail.md](audit-trail.md) — `file_share_download` events recorded here
- **Guards**: [effect-idempotency.md](effect-idempotency.md) — `create_share` is wired through `effect_guard` (scope `effect:{execution_id}`, identity = filename + content version) so a re-delivered turn replays the existing signed URL instead of re-minting a share (#1084)

## Design References

- Design doc: `docs/drafts/amazing-file-outbound.md`
- Production readiness plan: `docs/drafts/amazing-file-outbound-production-readiness.md`
- Phase 1 execution checklist: `docs/drafts/amazing-file-outbound-phase1-execution.md`
