# Feature: Outbound File Sharing (FILES-001)

## Revision History

| Date | Changes |
|------|---------|
| 2026-04-24 | Initial implementation (FILES-001 / #295). Steps 1–6 complete: schema, toggle + volume, internal share endpoint, public download, MCP tool, UI panel. |
| 2026-09-07 | #2582 — the doc caught up with ent#461 (disposition is a server-decided allowlist, not a flat `attachment`) and with #568 (the `session_token` gate and the `require_email` policy check were deleted; both are AST-pinned dead). Added the one-way `?download=1` flag, the `HEAD` route, and the counter/audit split for a ranged prefix read. |
| 2026-09-21 | trinity-enterprise#549 — a shared file has an addressee. New section *Who a file is for*: the turn→addressee table, the two questions `services/turn_audience.py` keeps apart, the `audience_email` override, the honest-status result fields, the two platform-side WhatsApp callers, the two readers, known limits. Three nullable columns on `agent_shared_files` (both migration tracks), the "For" column on the owner's panel, three named share errors, and the addressee withheld from key-authenticated callers of the list route. |

## Overview

Agents publish files from their `/home/developer/public/` directory to a public download URL with a signed token and a 7-day default expiration. The URL works universally — web, Slack, Telegram, WhatsApp, email — replacing fragile per-channel upload patterns. Each share also has ONE addressee — the person the turn was for — which decides whose Workspace Files tab lists it (see *Who a file is for*).

## Requirement Reference

- **Requirement**: §13.10 Outbound File Sharing (FILES-001)
- **GitHub Issue**: #295
- **Status**: ✅ Implemented 2026-04-24
- **Pillar**: III (Persistent Memory / Delivery)

## User Story

As an agent user (web, Slack, Telegram, WhatsApp), I want the agent to produce a downloadable file I can retrieve from a URL, so that outputs like CSV reports, PDFs, exports, and generated assets don't need to be pasted as text or handled with per-channel upload APIs.

## Entry Points

- **MCP tool**: `share_file({ filename, display_name?, expires_in?, execution_id?, audience_email?, dedup_label? })` — from inside any agent with file sharing enabled
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
│   ├── resolve audience (who the file is for)   │
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
    ├── (no policy gate — #568 deleted require_email/session_token here;
    │    the 192-bit sig IS the credential, pinned dead by
    │    tests/unit/test_file_download_no_session_gate.py)
    ├── Range → 206 / 416, else stream in 64 KB chunks (ent#461)
    ├── Content-Disposition: inline for _INLINE_SAFE_TYPES, else attachment
    │    (RFC 6266 UTF-8) — server-decided; ?download=1 may force attachment
    │    ONLY, never inline (#2582)
    ├── X-Content-Type-Options: nosniff
    ├── bump download_count + last_downloaded_at — full transfers excluding `?preview=1` (#2582)
    └── audit: EXECUTION/file_share_download (details.ranged_prefix, details.preview)
```

---

## Who a file is for

A share has ONE addressee (trinity-enterprise#549). The platform decides it from the turn the share came from; the agent does not choose. The addressee governs **whose Workspace Files tab lists the row** and nothing else — the download link is untouched (see *Unchanged / known limits*).

| The turn came from | The file is for |
|--------------------|-----------------|
| a Workspace chat, or a room turn, with Ada | Ada (`source_channel_client`) |
| a WhatsApp / Telegram / Slack conversation | that channel identity; ALSO Ada's email when she is a **verified speaker in a one-to-one chat** — never in a group, where the verified email is the unlocker's, not the speaker's |
| a schedule, operator chat, MCP call, loop, voice post-session turn, agent-to-agent child | nobody — the owner only |

The decision is stored on the row: `addressed_to_email` (decides the listing), `addressed_to_channel` (display only) and `audience_source` (how it was decided). NULL email AND NULL channel = **the owner only**.

`src/backend/services/turn_audience.py` is the one place the decision is made — a leaf (no HTTP, no SQL; its DB reads go through the `database` facade) that keeps two questions apart.

### 1. Given a turn, who is it for? — `audience_of(execution)`

Pure and table-driven, over columns every entry path already stamps on the execution row. In order:

- **Inherited agent-to-agent context** — `source_channel_agent` is set at all → nobody. That column is written by the inheritance path and by nothing else, so non-NULL MEANS inherited, whoever it names: comparing it to the executing agent would miss an agent tasking ITSELF and A→B→A, where they are equal and the inherited client was picked through an agent-typed `parent_execution_id`. The child row carries its parent's channel context so completion reports find their way back; the turn itself came from an agent.
- `source_channel` is `portal` or `room` → `source_channel_client`. Fails closed (nobody) on a row from before that column existed: `source_user_email` alone is not a recipient check.
- `source_channel` is `telegram`, `whatsapp` or `slack` → the channel address (`channel_address()` → `<channel>:<chat id>`, prefixed exactly once — Twilio's `From` already arrives as `whatsapp:+…`), plus `source_channel_client`, which `adapters/message_router.py::_run_agent_task` stamps **only for a verified speaker in a one-to-one chat**. **Never `source_user_email` on a channel turn:** that column is `verified_email OR the channel-native id` (an unverified user carries `telegram:<bot>:<id>` there), and in a GROUP the verified email is the UNLOCKER's — set once per group, not per speaker, which is why MEM-001 refuses group mode — so trusting it would list a file somebody else in the group asked for in the unlocker's Files tab. A group turn, an unverified user and a row from before the stamp all resolve to the channel address alone. The stamp is inert for completion reports: only the portal leg compares that column.
- Anything else → nobody. The channel set is an **allow-list**: a `source_channel` the table has never heard of makes no claim, rather than defaulting to "a person".

`normalize_addressee_email()` is the ONE spelling of an addressee for every writer and for the reader (strip + lower-case; anything not email-shaped is `None`): rooms stamp a raw `current_user.email`, while portal identities are lower-cased at login.

### 2. Which turn did this call come from? — `resolve_turn_audience(agent_name, *, actor_is_agent, claimed_execution_id, platform_execution_id=None)`

An MCP tool call carries the agent's key and nothing about the turn, so the only link is the `execution_id` the AGENT types — and a resumed session cites ids out of its own history. The rule therefore needs **positive evidence**, and says "could not tell" without it:

1. **Not the agent's own key** (`actor_is_agent=False`) → no turn at all (`none`). An owner or a user-scoped key passes the share route's owner gate too; whatever id they cite, they are not an agent in a turn.
2. **The cited id is not an execution of THIS agent** — absent, unknown, or another agent's (`idempotency_service.resolve_and_validate_execution`) → could not tell.
3. **The cited execution is an agent-to-agent child** → nobody (question 1).
4. **Otherwise the cited id proves which CONVERSATION the call came from** (`source_channel` + `source_channel_chat_id`) — and never which person, whether that execution is live or finished. The person is whoever that conversation's RUNNING **direct** turns belong to — `db.get_running_in_conversation(agent_name, channel, chat_id)` (`db/schedules/executions.py`, facade in `database.py`; `RUNNING` rows only, all three predicates required, bound to the calling agent because a room holds several) — and they must **agree**: one person is an answer; none, or two different people, is could not tell. A room shares one Claude session across its humans, so a model citing another participant's id is the ordinary case there: a finished one must not send the file to them, and neither must a zombie row a crash left `running` beside somebody else's live turn. Delegated children in the same conversation are not turns of it and do not vote, so a turn that fanned out to itself still shares to its person.
5. **A cited execution with no conversation at all** (a schedule, an operator chat) is its own answer while it runs — nobody — and no evidence once it has finished.

Every "could not tell" (`ambiguous`) is **the owner only**: the rule can under-share, it cannot over-share. It never raises — a provenance lookup must not fail the share it describes — and every resolution of an agent's call logs `[turn-audience] agent=… rule=… execution=… source=… addressed=<bool> channel=<bool>`, naming the rule that fired (`platform-id`, `no-evidence`, `cited-delegated-child`, `cited-live-turn`, `stale-no-conversation`, `conversation`, `conversation-<n>-turns-<m>-people`) and never the addressee.

**The shortcut that must not return:** "the agent has exactly one running turn, so take it." A web-terminal `claude` session holds the agent's key, has no execution row and no Execution Context, and so cites nothing; that rule would hand an operator's file to whichever client happened to be mid-conversation.

`platform_execution_id` is the slot for a platform-injected id (#2392): every headless turn's process already carries `TRINITY_EXECUTION_ID`, and once that reaches the backend it answers this question with no cooperation from the model. Nothing passes it today.

`create_share()` resolves the audience **before** `extract_from_agent()` — that await is the slow step, and the turn's row can leave `running` while it is in flight.

### The override — `audience_email`

The agent's one override names a different person. `_audience_from_override()` (`agent_shared_files_service.py`) checks the address against the agent's OWN roster with the **reader's** predicate — `client_portal.service.agent_on_roster(agent_name, email, include_owned=True)`, imported function-locally and called through the module, because `client_portal.service` imports this service back — so a stored addressee is always someone who can open the tab it decides (a broader write gate stores rows nobody can read; a narrower one refuses people who could). `include_owned=True` keeps the owner nameable: the Files tab shows an owner the files addressed to them. Refusals are named and store nothing — 400 `INVALID_AUDIENCE`, 400 `AUDIENCE_NOT_ON_ROSTER`, 503 `AUDIENCE_UNVERIFIABLE` (see *Error Handling*). An override row carries `audience_source = override` and no channel.

### What the tool result tells the agent

`_visibility()` adds honest status to the share result, in the names `set_canvas` already uses:

| `visible_to_requester` | When | `visibility_note` |
|------------------------|------|-------------------|
| `true` | decided from the turn, the person has an email, AND the agent is shared with them (`agent_on_roster`) — so they really have a Files tab for it | `null` |
| `false` | could not tell which turn (`ambiguous`) — the file is listed for the owner only | how to fix it: call again with the current `execution_id`, or with `audience_email` |
| `null` | no claim — a turn with no person, a channel user with no verified email, a person the agent is not shared with (the row is theirs; the tab 404s for them today), an address the agent chose itself | `null` |

`addressed_to` echoes the address **only when the agent supplied it** (`override`). An address the platform resolved is never returned to the model: a channel user's verified email may be something the agent was never told.

### Platform-side callers (WhatsApp)

Two callers create share rows with no agent tool call, and both already HOLD the recipient — the number a reply is going to — so there is no turn to resolve. Each calls `turn_audience.whatsapp_recipient(binding_id, number)` → `(verified email | None, "whatsapp:+…")` and hands the pair to `create_share_from_bytes(..., addressed_to_email=, addressed_to_channel=)`, which stores `audience_source = channel`:

- **Outbound media** — `adapters/whatsapp_adapter.py::_prepare_outbound_media(agent_name, files, *, recipient=None, binding_id=None)`, called from `send_response` with `recipient=channel_id, binding_id=binding.get("id")`.
- **Voice notes** — `services/voice_reply_service.py::_deliver_whatsapp`, for the hosted `voice.ogg`.

The email is whatever the binding verified for that number (`db.get_whatsapp_verified_email`), or `None` — the file is then in nobody's Files tab, and the owner's panel shows the number. `whatsapp_recipient` never raises: a failed lookup is "no email", never lost media. Neither argument given = the owner only.

`POST /api/internal/agent-files/share` has no callers and passes no `actor_is_agent`, so its rows are owner-only (`none`).

### Readers

| Reader | Read | Lists |
|--------|------|-------|
| **Workspace Files tab** — `GET /api/enterprise/client-portal/agents/{agent_name}/documents` → `client_portal/service.py::portal_documents` (frontend: `stores/clientPortal.js::fetchDocuments`) | `db.list_active_shared_files_for_viewer(agent, normalize_addressee_email(email), include_owner_only=portal_owns_agent(email, agent, include_owned))` → `db/agent_shared_files.py::list_active_for_viewer` | rows whose `addressed_to_email` is the viewer; the agent's owner reads "mine ∪ owner-only" (NULL email AND NULL channel) |
| **Owner's Sharing panel** — `GET /api/agents/{name}/shared-files` | `db.list_active_shared_files_for_agent` → `list_active_for_agent` | everything the agent has out, with `addressed_to` / `addressed_to_channel` / `audience_source` per row |

- The Workspace read is narrowed **in the query**, never loaded whole and filtered in Python — that would put another person's download token in the process one edit away from the response.
- An empty viewer email matches nothing rather than everything (`column == None` compiles to `IS NULL`, which is every unaddressed row).
- A row addressed to a channel identity with no email is NOT owner-only: it is somebody's, just nobody with a Files tab. It stays off every Workspace tab, the owner's included, and on the owner's Sharing panel.
- "Owner" is `portal_owns_agent` — the membership the roster card renders — so a non-owner admin and an owner signed in with a magic-link portal token are viewers here, exactly as for the tab's delete affordance ([workspace-rail.md](workspace-rail.md)).
- The owner's list route withholds `addressed_to` / `addressed_to_channel` (not `audience_source`) from every non-interactive principal via `dependencies.is_interactive_principal` — only a JWT human (`mcp_scope is None`) passes. An agent-scoped key resolves to its owner and passes that route's owner gate, so without the strip an agent could read who each file was for: an email and, for a channel share, a phone number.

### Unchanged / known limits

- **The `?sig=` link remains a bearer credential.** The audience governs the LISTING only, not the download: anyone holding the URL can fetch the file until it expires or is revoked (another human in a room transcript can open a link posted there).
- **Channel scope.** On channels, share rows arise only from the two WhatsApp platform-side callers above — WhatsApp media and WhatsApp voice notes. Telegram and Slack upload natively and create no row, and channel turns allow only `WebSearch,WebFetch` by default (`_DEFAULT_CHANNEL_ALLOWED_TOOLS` in `adapters/message_router.py`, overridable through the `channel_allowed_tools` setting), so an agent-called `share_file` happens there only where that list was widened.
- **Pre-existing rows** are NULL in all three columns — the owner only. No backfill: their recipient is unknowable, and every share expires within seven days.
- **Residual:** a session that carries ANOTHER conversation's history can cite a real id of it — an operator who `--resume`s a client's chat in a terminal, and, before #2958, an operator chat, whose `--continue` resumed the most recent session in the agent's home (the chat now resumes only its own session id). If that client has a turn running at that moment and the model prefers the stale id to its own Execution Context, the file is addressed to the client. A platform-injected id (#2392) closes it.
- **By design, same reach as `report`:** a prompt-injected agent can address a file to any person on ITS OWN roster, and `AUDIENCE_NOT_ON_ROSTER` tells it whether an address is on that roster. Bounded by the roster, the MIME blocklist and the inline-disposition allowlist.

---

## Frontend Layer

### Components

| File | Line | Description |
|------|------|-------------|
| `src/frontend/src/components/FileSharingPanel.vue` | 1-258 | Toggle, restart-required banner, quota, table (filename/for/size/expires/downloads), Copy URL + Revoke buttons, empty state. The **For** column says who each file is for — a label plus a second line that says why — rendered through `describeSharedFileAudience(file)` |
| `src/frontend/src/utils/sharedFileAudience.js` | 38 | `describeSharedFileAudience(file)` → `{ label, detail, ownerOnly }`, pure. An email → the email, with the channel (`WhatsApp +15555550142`) or the reason (`turn` → "The person in the conversation", `override` → "Addressed by the agent") beneath; a channel identity alone → the channel, "No verified email — not in any Files tab"; neither → "Owner only", with `none` ("No person in this turn"), `ambiguous` ("Couldn't tell which conversation it came from") and a pre-column row ("Shared before files had an addressee") kept apart — `ambiguous` is recoverable, so the owner has to be able to see it happen |
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
| Service | `src/backend/services/agent_shared_files_service.py` | `create_share()` orchestrator, path validation, MIME detection, quota, extraction; `create_share_from_bytes()` for platform-side callers |
| Service | `src/backend/services/turn_audience.py` | Who a file is for — `audience_of()`, `resolve_turn_audience()`, `whatsapp_recipient()`; a leaf, no HTTP, no SQL |
| Service | `src/backend/client_portal/service.py` | `portal_documents()` — the Workspace Files tab's listing, narrowed to the viewer |
| Service | `src/backend/services/agent_service/file_sharing.py` | Toggle logic, `check_public_folder_mount_matches()` |
| DB | `src/backend/db/agent_shared_files.py` | `AgentSharedFilesOperations` CRUD; `list_active_for_agent()` (the owner's panel) vs `list_active_for_viewer()` (the Workspace) |
| DB | `src/backend/db/schedules/executions.py` | `get_running_in_conversation()` — this agent's running turns in one conversation (facade in `database.py`) |
| DB | `src/backend/db/agent_settings/file_sharing.py` | `FileSharingMixin` — per-agent toggle + volume name convention |

### Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/agents/{name}/file-sharing` | JWT (access) | Status + quota bar data |
| PUT | `/api/agents/{name}/file-sharing` | JWT (owner/admin) | Toggle; returns `restart_required: true` when config and mounts disagree |
| POST | `/api/agents/{name}/shared-files` | JWT (owner/admin) OR agent-scoped MCP key (same agent) | Mint a download URL. Optional `audience_email` names a rostered person instead of the person the turn was for. The route passes `audience_email=body.audience_email` and `actor_is_agent=bool(actor_agent)` — only the agent's own key is ever resolved to a turn. Response adds `visible_to_requester` / `visibility_note` / `addressed_to` |
| GET | `/api/agents/{name}/shared-files` | Owner/admin (`assert_agent_owner`) — a JWT, or an MCP key that resolves to the owner | List active shares and who each is for: `addressed_to`, `addressed_to_channel`, `audience_source`. The first two are withheld from every key-authenticated caller (`is_interactive_principal`) |
| DELETE | `/api/agents/{name}/shared-files/{file_id}` | JWT (owner/admin) | Revoke (idempotent) |
| POST | `/api/internal/agent-files/share` | `X-Internal-Secret` | Agent-server direct path; takes `agent_name` in body. No callers; its rows are owner-only |
| GET | `/api/files/{file_id}` | Token (`?sig=`) | Public download. Optional **one-way** `?download=1` forces `attachment` (#2582); tolerantly parsed, so a malformed value is ignored rather than 422'd. The `session_token` parameter is gone since #568 |
| HEAD | `/api/files/{file_id}` | Token (`?sig=`) | Same validation and headers as GET, no body, no counter, no audit row. Honours the same `?download=1`, because a disposition that disagrees with GET mis-plans the player that probed |

### MCP tool

| Tool | File | Description |
|------|------|-------------|
| `share_file` | `src/mcp-server/src/tools/files.ts` | Agent-scoped. Body: `{ filename, display_name?, expires_in?, execution_id?, audience_email?, dedup_label? }`. Returns: `{ file_id, url, expires_at, size_bytes, mime_type, visible_to_requester, visibility_note, addressed_to }` |

`execution_id` is how the platform knows which conversation a share belongs to, and so whose Files tab lists the file; without it the file is listed for the owner only. `audience_email` is forwarded as given (`undefined` when omitted, never a default), and the three status fields come back as `null` when the backend makes no claim. The who-sees-it guidance lives in the tool DESCRIPTION (and in the `execution_id` / `audience_email` parameter descriptions): the platform prompt's "Sharing Files with Users" section is in `_MINIMAL_DROP_SECTIONS` (`services/platform_prompt_service.py`), so it is dropped at the minimal prompt tier and the tool description is the durable home of tool guidance. That section carries the same rule as a "Who sees a shared file" paragraph for the tiers that render it. `src/mcp-server/src/client.ts::shareAgentFile` types the body field and the three result fields.

### Key service behaviors

| Behavior | Where | Notes |
|----------|-------|-------|
| Path validation | `validate_publish_path()` | Rejects absolute paths, `..` segments, backslashes. Resolves against `/home/developer/public/`. |
| Audience | `_audience_from_override()` / `turn_audience.resolve_turn_audience()` | Who the file is for — the agent's rostered `audience_email`, else the person the turn was for, else the owner only. Resolved before extraction. See *Who a file is for*. |
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
    addressed_to_email TEXT,              -- Whose Workspace Files tab lists the row
    addressed_to_channel TEXT,            -- Channel identity (`whatsapp:+…`) — DISPLAY ONLY, no reader filters on it
    audience_source TEXT,                 -- turn | override | channel | none | ambiguous; NULL = a row older than the columns
    FOREIGN KEY (agent_name) REFERENCES agent_ownership(agent_name)
        ON DELETE CASCADE ON UPDATE CASCADE
);
CREATE INDEX idx_agent_files_agent ON agent_shared_files(agent_name);
CREATE INDEX idx_agent_files_token ON agent_shared_files(download_token);
CREATE INDEX idx_agent_files_expires ON agent_shared_files(expires_at) WHERE revoked_at IS NULL;

-- Plus one column on the existing agent_ownership table:
ALTER TABLE agent_ownership ADD COLUMN file_sharing_enabled INTEGER DEFAULT 0;
```

The three addressee columns are nullable TEXT with no default and no backfill, on both migration tracks: SQLite `db/migrations.py::_migrate_agent_shared_files_audience` (registered as `agent_shared_files_audience`) and PostgreSQL `migrations/versions/0068_agent_shared_files_audience.py`; fresh-install DDL in `db/schema.py`, query metadata in `db/tables.py`. NULL email AND NULL channel = the owner only, which is also what every pre-existing row becomes.

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
| S6 | XSS via agent-uploaded HTML | **`inline` is an ALLOWLIST, not a relaxation** (ent#461): only `_INLINE_SAFE_TYPES` (audio/video/image/PDF) — `text/html`, `application/xhtml+xml` and `image/svg+xml` stay `attachment`, and the type is python-magic-detected from the bytes, never agent-supplied, with an unavailable-fallback outside the allowlist. `X-Content-Type-Options: nosniff` is kept and becomes MORE load-bearing once anything is inline. The requester's `?download=1` is one-way toward `attachment` (#2582), so no requester input can widen this |
| S7 | Filename header injection (CRLF) | Sanitizer allows `[A-Za-z0-9._\- ]` only; RFC 6266 UTF-8 percent-encoding for non-ASCII |
| S8 | MIME spoofing | python-magic detects actual MIME; blocklist rejects PE/ELF/Mach-O/shebang before storage |
| S9 | Storage DoS | 50 MB per-file + 500 MB per-agent quota (setting-configurable) |
| S10 | Token enumeration | 192-bit entropy + IP rate limit + audit log |
| S11 | Cross-tenant download | File addressed by `file_id` only; agent_name resolved from DB row |
| S14 | Access-policy bypass | **Historical — the gate is gone (#568).** `build_download_url` never appended a `session_token` and the agent had no way to learn the recipient's value, so the gate permanently broke sharing for `require_email` agents rather than protecting it. The 192-bit `sig` is the sole credential; `tests/unit/test_file_download_no_session_gate.py` AST-pins the removal, including `_validate_download_request`'s exact argument list |
| S15 | Agent impersonation via MCP | Backend enforces `current_user.agent_name == path agent_name` for agent-scoped keys (same-agent defense) |
| S16 | One client's file listed in another client's Workspace Files tab | Every share has one addressee, and `portal_documents` reads `list_active_for_viewer` — narrowed **in the query** to the viewer's normalised email (plus the owner-only rows for the agent's owner); an empty viewer matches nothing. The failure direction is **under-share**: deciding which turn a share came from needs positive evidence, and every "could not tell" is the owner only — never "the agent's one running turn" |
| S17 | Agent widens the audience | `audience_email` is checked against the agent's own roster with the reader's predicate (`agent_on_roster(..., include_owned=True)`): off-roster → 400 `AUDIENCE_NOT_ON_ROSTER`, unreadable roster → 503 `AUDIENCE_UNVERIFIABLE`, nothing stored either way. A caller that says nothing can never widen who sees a file — `actor_is_agent` defaults to False, and an absent audience is the owner only |
| S18 | Addressee disclosed to a machine caller | The owner's list route withholds `addressed_to` / `addressed_to_channel` from every key-authenticated principal (`is_interactive_principal` — only `mcp_scope is None` passes, and a principal with no `mcp_scope` fails closed): an agent-scoped key resolves to its owner and passes that route's owner gate. The share result never returns an address the platform resolved, the addressee is never logged, and it enters the #1084 effect key only as a hash |

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
| Share: `audience_email` is not email-shaped after normalisation | 400 | `INVALID_AUDIENCE: audience_email must be an email address` (a value with no `@`, or with a space, is a 422 from the `ShareFileMcpRequest` validator before the service sees it; the empty string is "absent") |
| Share: `audience_email` is not on the agent's roster | 400 | `AUDIENCE_NOT_ON_ROSTER: …` — nothing is stored; share the agent with that address first, or omit `audience_email` |
| Share: the roster cannot be read while checking `audience_email` | 503 | `AUDIENCE_UNVERIFIABLE: could not verify the file's audience — try again.` — nothing is stored |

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

Who a file is for (trinity-enterprise#549):
```bash
pytest tests/unit/test_ent549_file_audience.py -v
(cd src/mcp-server && node --import tsx --test src/files.test.ts)
(cd src/frontend && npm run test:unit -- tests/unit/sharedFileAudience.spec.js)
```
- `test_ent549_file_audience.py` — write→read **round trips**: the real `create_share` / `create_share_from_bytes` into a real SQLite file, read back through the real `portal_documents`; only the container extraction and the disk write are stubbed. Covers the failure direction (a terminal share never lands with the client who happens to be chatting; a human on the route has no turn), stale ids (a finished id proves the conversation, the running turn names the person; none or two running → ambiguous; another agent's id imports nobody), every row of the turn→addressee table through `audience_of`, the override (off-roster refused by name, unreadable roster refuses, the owner is nameable, re-addressing in one turn is a second share while a re-run still replays), the owner's list route with each key scope, the two WhatsApp callers through their call sites, and both migration tracks.
- `src/mcp-server/src/files.test.ts` — `audience_email` reaches the request body (`undefined` when omitted), the three status fields come back to the agent, and the tool description says whose Files tab a file lands in.
- `src/frontend/tests/unit/sharedFileAudience.spec.js` — `describeSharedFileAudience`: email, agent-chosen, channel beside an email, channel alone, unknown channel, `none` vs `ambiguous`, a pre-column row, a missing row.

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
- **Reader**: [workspace-rail.md](workspace-rail.md) — the Workspace Files tab, which lists what is addressed to the viewer
- **Caller**: [whatsapp-integration.md](whatsapp-integration.md) — outbound media and voice notes are hosted here, addressed to the number the reply is going to
- **Guards**: [effect-idempotency.md](effect-idempotency.md) — `create_share` is wired through `effect_guard` (scope `effect:{execution_id}`, identity = filename + content version + addressee) so a re-delivered turn replays the existing signed URL instead of re-minting a share (#1084), while re-addressing the same file to a second person in one turn is a second share

## Design References

- Design doc: `docs/drafts/amazing-file-outbound.md`
- Production readiness plan: `docs/drafts/amazing-file-outbound-production-readiness.md`
- Phase 1 execution checklist: `docs/drafts/amazing-file-outbound-phase1-execution.md`
