# Trinity Architecture — Channels, real-time, VoIP, Brain Orb

> Part of the Trinity architecture set. Core map, invariants and topology: [architecture.md](../architecture.md). This file is **not** auto-loaded.
>
> **Owns**: `src/backend/adapters/**`, `src/backend/services/event_bus.py`, `src/backend/services/ws_ticket_service.py`, `src/backend/services/voip_service.py`, `src/backend/services/slack_service.py`, `src/backend/services/tts_service.py`, `src/backend/services/voice_reply_service.py`, `src/backend/routers/agent_brain_orb.py`, `src/backend/services/agent_shared_files_service.py`
>
> **Read this before changing the paths above**: `/ws` is SCOPE_ALL and unfiltered, so a broadcast that carries a payload discloses it to every connected client. Broadcasts must be thin triggers carrying identifiers only, with listeners refetching through the access-controlled REST route (#918, pinned by `test_918_report_broadcast.py`).
>
> **Write path**: changes to this area land here, not in the core (core editorial rule 4). Keep the core's map row in step if the owned paths change.

---

### Real-time Delivery (RELIABILITY-003, #306)

**Transport** (`event_bus.py`, details in [websocket-event-bus.md](../feature-flows/websocket-event-bus.md)): Redis Streams. `ConnectionManager`/`FilteredWebSocketManager` are thin shims that `XADD` to the MAXLEN-trimmed `trinity:events` stream; one `StreamDispatcher` per backend process runs `XREAD BLOCK` and fans out, evicting a client after 3 consecutive delivery failures. New broadcast sites keep calling `manager.broadcast(...)` / `filtered_manager.broadcast_filtered(...)` — never publish to the stream directly (Invariant #10).

**Reconnect replay**: `/ws` and `/ws/events` accept `?last-event-id=<stream_id>`, regex-gated (`^\d+-\d+$`) by `validate_last_event_id()` before `XRANGE`. Catchup capped at `REPLAY_GAP_LIMIT=5000` — larger gaps return `{"type": "resync_required", "reason": "gap_too_large"}`. Authorization (`accessible_agents` for `/ws/events`) is re-applied on replay. The frontend tracks `_eid` per message, appends `&last-event-id=` on reconnect, and on `resync_required` clears the cursor and refetches via REST.

**`/ws` agent scope (ent#467).** `/ws` shipped `SCOPE_ALL` and **unfiltered**: every `agent_activity`, `agent_created`, `operator_queue_*`, `agent_report` and `agent_shared` event reached every authenticated client, carrying fleet-wide agent names, execution ids and activity types — and, on the two sharing events, another user's email. Any `role=user` with a single shared agent could mint a ticket (`POST /api/ws/ticket` is plain `get_current_user`) and read the whole instance. `/ws/events` had scoped by `accessible_agents` since #306; `/ws` never did, which also made every execution-id-keyed write route one forgotten check away from a cross-agent action (the trinity#2433 finding's enabling disclosure). A `/ws` client now sees a `scope=all` event only when **every** agent the payload names is one it may access. Five properties are load-bearing: (1) identity is resolved at connect by `services/ws_identity_service.resolve_ws_identity` and **fails closed** — unknown subject, suspended account (#995) or a raising lookup closes the socket with 4001, while a non-admin row with *no* email is a resolved identity with an **empty roster** rather than a refusal (ownership joins `users.email`, sharing is keyed on it, so the empty set is the exact answer — and the frontend never retries a 4001, so refusing would leave a legacy row permanently dark). (2) **Admins are never filtered** and deliberately resolve no roster: a connect-time snapshot would blind the operator to every agent created after the page loaded. (3) Agent identity is **derived from the payload** by `agent_names_in_payload()` over one named key vocabulary, because the 36 live `manager.broadcast` sites disagree about where the name lives (`agent_name` top-level, `data.name`, `data.agent_name`, two keys at once for `agent_collaboration`); `details` is read narrowly (the two collaboration keys) since a free-form `details["name"]` holding a *tool* name would hide the event from the agent's own owner — over-filtering is how this change breaks a working UI. (4) **Both** delivery paths filter — the `last-event-id` replay re-reads history straight from Redis, so a filter wired only into `_fanout` would hand the entire unfiltered backlog to any client that reconnects (pinned structurally). (5) A live roster is refreshed **by the stream itself**: `_maybe_invalidate_rosters` watches for `agent_created`/`agent_deleted`/`agent_shared`/`agent_unshared`/`agent_renamed` and re-resolves each non-admin slot off the event loop via an injected resolver (`set_accessible_resolver`, wired from `main.py` so the delivery layer never imports `database`, Invariant #1) — every worker reads the same stream, so one publish invalidates rosters fleet-wide with no call-site edits, and a *missed* invalidation degrades to the `/ws/events` contract (stale until reconnect), never to a leak, because the stale roster is the smaller one. No Redis envelope change, so entries written by a pre-ent#467 worker during a rolling deploy still filter correctly. An event naming **no** agent stays fleet-visible — fail-open by design, which is why `tests/unit/test_ent467_ws_agent_scope.py` AST-discovers every `/ws` broadcast payload in the OSS tree and fails unless each is agent-keyed or listed in `FLEET_LEVEL_ALLOWLIST` with its reason: the guard, not the extractor, is what stops a new event shape from leaking silently.

**WebSocket auth** (C-002, #550): `/ws` uses single-use opaque tickets, not a JWT in the URL: `POST /api/ws/ticket` mints a 32-byte urlsafe ticket (Redis, 30s TTL); client connects `/ws?ticket=...`; backend atomically `GETDEL`s then accepts. Closes the JWT-leak surface (nginx logs, history, proxies); CSWSH mitigated because minting needs the JWT in an `Authorization` header (CORS-blocked cross-origin). `/ws/events` still accepts `?token=trinity_mcp_*` for external scripts (scoped, revocable). `mint_ticket` optional `ttl_seconds` (default 30s, ceiling 600s); VoIP mints call-bound tickets (`scope="voip:{call_id}"`, 180s) since PSTN dial+ring exceeds 30s. Impl: `services/ws_ticket_service.py` + `routers/ws_tickets.py`.

### Outbound File Sharing (FILES-001)

Per-agent opt-in (`agent_ownership.file_sharing_enabled`). The agent writes to `/home/developer/public/` (Docker volume `agent-{name}-public`); on share, the backend extracts the named file via Docker SDK `get_archive` (never mounts the workspace — isolated blast radius) and stores bytes at `/data/agent-files/{file_id}`. `agent_shared_files_service.py` handles path validation, MIME blocklist, quota, extraction, URL building.

Download URL: `{public_chat_url}/api/files/{file_id}?sig={token}` — `?sig=` (NOT `?download_token=`) so the credential sanitizer's `.*TOKEN.*` pattern doesn't redact it in transcripts. **Delivery is shaped for the platforms the link is opened FROM (ent#461).** The route serves `Accept-Ranges: bytes` and real `206` partial content (single range; a multi-range request falls back to the full 200 per RFC 7233 §3.1, and an unsatisfiable one gets a `416` carrying the true length), `Cross-Origin-Resource-Policy: cross-origin`, `Cache-Control: private, max-age=min(remaining lifetime, 3600)`, and a normalized MIME (`audio/x-wav` → `audio/wav`). Each was individually fatal on mobile: without `206` iOS will not start audio at all, `no-store` forbade the in-app browser from buffering it, `same-origin` CORP stopped Telegram embedding it, and an unregistered type under `nosniff` made strict players decline it. **`Content-Disposition: inline` is an ALLOWLIST, not a relaxation** — `_INLINE_SAFE_TYPES` (audio/video/image/PDF) only; `text/html`, `application/xhtml+xml` and **`image/svg+xml`** stay `attachment`, because this route serves agent-authored bytes from the same origin as public chat and SVG is a script host wearing an image's name. The type is python-magic-detected from the file's own bytes at share time, never agent-supplied, and its unavailable-fallback (`application/octet-stream`) is outside the allowlist — so the failure direction is `attachment`. `nosniff` is kept and becomes more load-bearing once anything is inline. Byte length comes from `os.path.getsize`, never the stored `size_bytes`: drift between the two truncates or hangs every download and makes `Content-Range` contradict the body. A ranged fetch is one play, not many downloads, so the counter and the audit row fire only on the transfer-START (a plain GET, or a range beginning at byte 0) — and since #2582 the COUNTER additionally requires a FULL transfer: a ranged prefix read (the Workspace preview) is still audited, carrying `ranged_prefix: true`, but does not bump `download_count`, or every preview would inflate the owner's numbers and bury the audit log. **The one requester-supplied input is `?download=1`, and it is ONE-WAY (#2582)**: it may only force `attachment`, the strictly-safer direction, and there is no `?disposition=` and no way to force `inline` — a requester choosing to be more restricted about their own download grants nothing, while the reverse is the vulnerability the allowlist exists to prevent. It is applied **in the GET and HEAD handlers** (which must agree, or a player mis-plans), never in `_validate_download_request`, whose argument list is AST-pinned by `test_file_download_no_session_gate.py`. It is parsed tolerantly (`Optional[str]` + a truthy check) rather than as a `bool`, because a `bool` query param 422s on `?download=` or `?download=x` — a new failure mode on the public link this route exists to keep opening from Telegram, WhatsApp and iOS. `sig` is a stored bearer token compared with `compare_digest`, NOT an HMAC over the URL, so appending `&download=1` cannot invalidate it. Only the **Workspace Files tab's** URL carries the flag (built in `client_portal/service.py::portal_documents` off `get_portal_base_url()`); the agent's own chat link (`build_download_url` off `get_public_chat_url()`) does not, so ent#461's mobile inline path is untouched. Cascades manual per platform convention: agent delete removes rows + files + volume; `rename_agent()` re-keys `agent_name` across every `AGENT_REFS`-registered column (50 today, `db/agent_cleanup.py`). MCP tool `share_file`.

**A shared file is for the person the turn was for (trinity-enterprise#549).** The table was scoped by agent alone, so the Workspace Files tab listed every active share of an agent — download link included — to everyone on its roster: the third occurrence of one class (asks ent#428, reports ent#365), a table scoped by an owning entity that gains a per-person dimension. `agent_shared_files` now carries `addressed_to_email` (whose Files tab lists the row), `addressed_to_channel` (the channel identity — **display only**, no reader filters on it) and `audience_source` (`turn` / `override` / `channel` / `none` / `ambiguous`; NULL = a row older than the columns). NULL email AND NULL channel is **the owner only**, which is also what every pre-column row becomes — no backfill, and every share expires within seven days. The platform decides the addressee; the agent does not choose. `services/turn_audience.py` is the one place it is decided, and it keeps two questions apart. **(1) `audience_of(execution)` — given a turn, who is it for?** Pure and table-driven over columns every entry path already stamps: `source_channel ∈ {portal, room}` → `source_channel_client` (fails closed on a row from before that column); a messaging channel → the channel address, plus `source_channel_client` — which `message_router._run_agent_task` stamps **only for a verified speaker in a one-to-one chat**. **Never `source_user_email` on a channel turn:** that column is `verified_email OR the channel-native id` (an unverified user carries `telegram:<bot>:<id>` there), and in a GROUP the verified email is the UNLOCKER's, set once per group and not per speaker — the reason MEM-001 refuses group mode — so trusting it would list a file somebody else in the group asked for in the unlocker's Files tab. A group turn, an unverified user and a row from before the stamp all resolve to the channel address alone; the stamp is inert for completion reports, where only the portal leg compares that column; an inherited agent-to-agent context (`source_channel_agent` set AT ALL — it is written by the inheritance path and nothing else, and comparing it to the executing agent misses a self-task and A→B→A, where they are equal) and everything else → nobody. The channel set is an **allow-list** — an unrecognised `source_channel` makes no claim, the `canvas_service.canvas_visibility` rule. **(2) `resolve_turn_audience(...)` — which turn did this call come from?** The hard one: an MCP call carries the agent's key and nothing about the turn, so the only link is an `execution_id` the AGENT types, and a resumed session cites ids out of its own history (observed live: the id of a turn 22 minutes old). It therefore demands **positive evidence** and answers "could not tell" without it — not the agent's own key → no turn (an owner or a user-scoped key passes the share route's owner gate too); a cited id that is not an execution of THIS agent → could not tell; a cited agent-to-agent child → nobody; otherwise the cited id proves which **conversation** (`source_channel` + `source_channel_chat_id`) the call came from and **never which person, live or finished** — the person is whoever that conversation's RUNNING direct turns belong to (`db.get_running_in_conversation`, bound to the calling agent because a room holds several), and they must AGREE: one person is an answer, none or two different people is "could not tell". A room shares one Claude session across its humans, so a model citing another participant's id is the ordinary case there — a finished one must not send the file to them, and neither must a zombie row a crash left `running` beside somebody else's live turn; delegated children are not turns of the conversation and do not vote. A cited execution with no conversation at all (a schedule, an operator chat) is nobody while it runs and no evidence afterwards. **The shortcut that must not come back:** "the agent has exactly one running turn, so take it" over-shares — a web-terminal `claude` session holds the agent's key, has no execution row and cites no id, and that rule would list an operator's file for whichever client happened to be mid-conversation. Every "could not tell" is the owner only, so the failure direction is under-share, never over-share; it is resolved BEFORE the container read, the one slow await during which a turn's row can leave `running`. `platform_execution_id` is the unused slot for a platform-injected id (#2392 — every headless turn's process already carries `TRINITY_EXECUTION_ID`). The agent's ONE override is `audience_email`, checked with the **reader's** predicate `agent_on_roster(..., include_owned=True)` (a broader write gate stores rows nobody can read; the owner is nameable because the reader shows an owner the files addressed to them): 400 `AUDIENCE_NOT_ON_ROSTER`, 503 when the roster is unreadable, nothing stored either way. The addressee joins the #1084 effect key — hashed, and the replay snapshot is stored without the `addressed_to` echo, so `idempotency_keys` never holds an address — which makes re-addressing a file within one turn a second share rather than a silent replay of the first. The tool result carries `visible_to_requester` / `visibility_note` (the names `set_canvas` uses) and **never** an address the platform resolved; the guidance lives in the tool DESCRIPTION because the prompt's "Sharing Files with Users" section is dropped at the minimal tier. The two platform-side callers — WhatsApp outbound media and WhatsApp voice notes — already hold the recipient and pass `(verified email | None, "whatsapp:+…")` through `turn_audience.whatsapp_recipient`; Telegram and Slack upload natively and create no row, and channel turns allow only `WebSearch,WebFetch` by default, so an agent-called `share_file` there exists only where an owner widened the list. **Readers:** the Workspace Files tab (`client_portal.portal_documents`) narrows IN THE QUERY (`list_active_for_viewer`) to the viewer's normalised email, plus the owner-only rows when `portal_owns_agent` — a row addressed to a channel identity with no email is somebody's, not the owner's, and stays on the owner's panel; an unidentifiable viewer matches nothing rather than everything (`column == None` compiles to `IS NULL`, which is every unaddressed row). The owner's list route returns everything and withholds `addressed_to` / `addressed_to_channel` from non-interactive principals (`is_interactive_principal`): an agent-scoped key resolves to its owner and passes that route's owner gate. One normaliser (`normalize_addressee_email`) serves every writer and the reader — rooms stamp a raw `current_user.email` and `users.email` is never normalised. **Unchanged, and stated rather than implied away:** the `?sig=` link remains a bearer credential, so the audience governs the LISTING only (another human in a room transcript can open a link posted there); a session that carries ANOTHER conversation's history can cite a real id of it (an operator who `--resume`s a client's chat, or an operator chat whose `--continue` resumes the most recent session) — if that client is mid-turn and the model prefers the stale id, the file is addressed to the client, which a platform-injected id closes; and `visible_to_requester` is `true` only when the person actually has a tab (`agent_on_roster`) — a verified channel user the agent is not shared with owns the row and is told nothing.

### VoIP Telephony (VOIP-001, #1056)

Outbound phone calls from agents via Twilio Media Streams + Gemini Live (details in [voip-telephony.md](../feature-flows/voip-telephony.md)). Feature-flag gated: `voip_available = VOIP_ENABLED && bool(GEMINI_API_KEY)`, default OFF; also requires a per-agent `voip_bindings` row (Twilio-voice creds, validated via Twilio Account fetch, AuthToken AES-256-GCM encrypted). A voice transport, NOT a text `ChannelAdapter`.

**Call flow:** MCP tool `call_user` → `POST /api/agents/{name}/voip/call` → `voip_service.py`: gate checks (flag/binding) + abuse controls (rate limit per `(owner, destination)`, durable per-agent daily cap), stages a Gemini session intent in Redis keyed by `call_id` (distinct from the `vs_` VoiceSession id), mints a call-bound WSS ticket, calls Twilio `calls.create(<Connect><Stream>)`. Never calls `connect_and_stream` itself (cross-worker safety — the WS handler does). Optional `Idempotency-Key` honored (Invariant #18).

**Media bridge** (`transports/twilio_media_stream.py`, WS `/api/voip/voice/{call_id}`): `accept()`-then-authenticate — Twilio does NOT forward the `<Stream url>` query string, so the call-bound ticket arrives as `start.customParameters.ticket` in the first `start` frame, read after handshake (#1073); `?ticket=` fallback for non-Twilio clients. Then scope check (`voip:{call_id}`), `GETDEL` staged intent (consume-once), create the Gemini `VoiceSession` on the connecting worker, run the unmodified `connect_and_stream`. Per-connection `_CallBridge`: inbound μ-law→PCM resample, outbound paced 20ms 160-byte μ-law sender, `clear`-on-barge-in, `streamSid` capture; teardown ties Gemini-end→Twilio-close + SETNX-guarded single transcript save + post-call dispatch. Codec helpers in `transports/voip_audio.py` (stdlib `audioop`, per-direction `ratecv` state for anti-click; `audioop-lts` pinned for Python ≥ 3.13).

**Post-call:** transcript persisted to `chat_messages` (`source="voice"`) and dispatched to the main agent via `task_execution_service.execute_task(triggered_by="voip")` (default ON). Phase 2 column `inbound_number` reserved in `voip_bindings`.

### MCP Exposure — Dedicated Dynamic Tools (#846)

Per-agent owner-toggled flag (`agent_ownership.mcp_exposed`, default 0) that
publishes an agent as a first-class MCP tool. When enabled, the Trinity MCP
server **dynamically registers** a dedicated `chat_with_<slug>` tool —
functionally identical to `chat_with_agent` with the agent name pre-filled —
**at runtime, no MCP-server restart**. The flag publishes a *surface* only;
execution always runs the same `checkAgentAccess` gate, so ownership/sharing is
never bypassed.

- **Refresh = poll, not WS.** The MCP server polls `GET /api/internal/mcp-exposed-agents`
  (existing `X-Internal-Secret` path, ~20s, `tools/dynamic-agents.ts`
  reconciler), diffs an `agentName→toolName` map, and calls FastMCP
  `addTool`/`removeTool`. FastMCP fans `notifications/tools/list_changed` to live
  sessions, so a connected client sees/loses the tool within ~one poll. The
  reconciler is **fail-open** (mutates only on a valid 200; keeps last-known set
  on error/parse-failure/timeout) and holds an in-flight mutex so startup-sync
  and the interval can't race.
- **Slug = single backend source of truth.** `services/agent_service/mcp_tool_names.py::compute_tool_names`
  computes the deterministic, collision-free name over the **full set** (sorted;
  `_<sha1(name)[:4]>` suffix on agent-vs-agent base-slug collision). The MCP
  server consumes it verbatim and applies one final guard against its own
  built-in tool names. The per-agent GET uses the same helper so UI and MCP never
  diverge.
- **Description = name-only (metadata-free)** — the dedicated tool's description
  is advertised **globally** to every non-connector MCP key (FastMCP filters the
  advertised list by `canAccess`; dedicated tools use only the connector-tier
  gate), so it must carry no per-agent metadata. The `trinity.template` Docker
  label is deliberately **excluded**: embedding it leaked the template/repo
  identifier cross-tenant to callers who cannot access the agent and opened a
  prompt-injection surface into the advertised description (#846 CSO). The agent
  name is already intrinsic to the `chat_with_<slug>` tool name, so a name-only
  description adds no disclosure beyond the name.
- **Visibility** mirrors operator tools: dedicated tools register with the
  `connectorDenied` `canAccess` gate (hidden from connector-scoped keys, ent#46
  isolation preserved). The shared `chat_with_agent` body is extracted into
  `tools/chat.ts::runAgentChat`, reused by both `chat_with_agent` and every
  dedicated tool — no logic fork (preserves #946 pull routing, parallel/self-task
  paths, idempotency tokens, #914 gateway-timeout recovery). The audit row binds
  the target agent via `withAudit(..., boundTargetId)` since dedicated tools carry
  no `agent_name` param.
- `mcp_exposed` is surfaced on `GET /api/agents` / MCP `list_agents`. Dual-track
  migration (SQLite `agent_ownership_mcp_exposed` + Alembic
  `0009_agent_ownership_mcp_exposed`).
- **Connect surface (#1575):** the Expose-via-MCP panel (`components/McpExposedPanel.vue`)
  gains a one-click **Copy connection config** action when exposed — it reuses the
  existing [MCP connector](../feature-flows/mcp-connector.md) (scoped `scope='connector'`
  key + playbooks-as-tools + `build_snippets`) to hand an external client a
  ready-to-paste `.mcp.json` with a least-privilege, agent-scoped, revocable key
  already embedded. No new endpoint/key type — the connector endpoints are reused
  (owner-only, keys already list in Settings → MCP Keys, revoke severs the connection).

### Brain Orb — Self-Rendering Mind page (#58, trinity-enterprise)

Capability-gated per-agent 3D knowledge-graph page for Cornelius-class agents.
**Shipped: static render (Phase 1) + live scope control (Phase 2) + client-held
Gemini Live voice tile + read-only KB search (Phase 3, #60) + owner-gated KB
writes: capture/link + voice-transcript capture + the write→refresh loop (Phase
4a/4b, #61/#66/#67) + post-voice-session processing as a standard execution
(#102).** The orb renders the agent-produced graph, supports button-driven
**scope mount/unmount → agent re-export → live rebuild**, and a **client-held
voice tile** (browser connects directly to Gemini Live via a short-lived,
config-locked ephemeral token minted by Trinity — no audio proxying). Voice
transcripts save through the owner-gated `action` broker; the configured
post-session prompt (#73) is dispatched by `services/brain_orb_postprocess.py`
via `execute_task(triggered_by="voice")` — a real, observable execution row
(sweep-safe, cost-tracked, failures surface as FAILED), replacing the hook's
detached `claude -p` (#102). Still deferred: `run_skill` headless-skill
injection. Mirrors the workspace page (gated per-agent route) and the
agent-owned read-surface pattern (pipelines #919, reports #918): the agent owns
generation + scope state (Invariant #8), Trinity reads/renders + brokers
control. Default OFF — no impact on other agents. Full flow:
[brain-orb.md](../feature-flows/brain-orb.md).

**Default Cornelius (trinity-enterprise#107):** a **fresh install** auto-seeds a
default "Cornelius" second-brain agent from the public `github:Abilityai/cornelius`
template — cloned anonymously (source-mode, no PAT) on the trinity-enterprise#123
tokenless path (`services/cornelius_agent_service.py`) — and existence-guarded-enables the
`brain_orb_enabled` flag, so the orb renders out-of-the-box. First-run-only (durable
`cornelius_seeded` system-setting flag — deleting Cornelius does not re-provision)
and skipped when any non-system agent already exists (established fleets are never
surprised); Redis SETNX lock (`cornelius:provision`) guards the `--workers 2` race (ownership-checked via `SingleFlightLock` #1920 — a verbatim twin of system_seed's constant-"1" + unconditional-delete bug, fixed with it).
Full flow: [cornelius-default-agent.md](../feature-flows/cornelius-default-agent.md).

- **First-party assets** (`src/frontend/public/brain-orb/`): the orb's verbatim
  page is split into `index.html` + externalized `orb.js`, with `three`/`marked`/
  `DOMPurify`/JetBrains-Mono vendored locally — so it runs CSP-clean under prod
  `script-src 'self'` / `font-src 'self'` with **no nginx change** (the #979 trap
  was *agent-origin* + *inline* scripts; this is first-party + external). Only
  mechanical edits: externalize the module, vendor CDN deps, repoint the data +
  scope fetches at the per-agent proxy base (carrying the platform JWT), hide the
  still-deferred voice/action panels. Markdown note bodies are DOMPurify-sanitized
  (H-005).
- **Frontend host** (`views/AgentBrainOrb.vue`, route `/agents/:name/brain`,
  lazy + `beforeEnter` flag guard): a thin chrome + **same-origin iframe** of the
  static page. JWT reaches the iframe's data fetch via origin-pinned `postMessage`
  (orb posts `brain-orb:ready` → host replies `brain-orb:init {agentName, apiBase,
  authToken}`; never in a URL) — no new ticket primitive. A `brain-orb:error`
  message shows the "hasn't rendered its mind yet" empty state. Gating:
  `brainOrbAvailable` (platform flag, `stores/sessions.js`) **AND** the per-agent
  `brain-orb` token in `template.yaml capabilities` (read from `/info`). BOTH the
  route guard (`beforeEnter` fetches `/info`, #60) and the `visibleTabs` Brain tab
  enforce the capability, so the orb is never launchable on a non-Cornelius agent —
  even via a raw URL (redirect, not empty state). Selecting the tab route-pushes to
  the page. The voice tile also ships a **vendored p5** audio-reactive orb that
  pulses with the spoken audio (CDN load was removed then re-vendored, #60).
- **Backend proxy** (`routers/agent_brain_orb.py`, prefix `/api/agents/{name}/brain-orb/*`):
  one shared gate/proxy helper (flag → running → `agent_httpx_client` #1159, **byte
  pass-through**, 404/503/504/502 mapping). `GET /data` + `GET /scopes` + `POST /tool`
  (read-only KB search) are read (`AuthorizedAgentByName`); **`POST /scope` is the only
  mutating route and is `OwnedAgentByName`** (owner/admin) — body-capped 64 KB, 200s
  timeout above the agent hook's 180s. `POST /voice-token` (Phase 3, `AuthorizedAgentByName`,
  per-(user,agent) rate-limited) mints the ephemeral Gemini Live credential (does NOT
  contact the agent — a Google call, not an agent call).
- **Voice-token mint** (`services/brain_orb_voice_service.py`, Phase 3, #60): the client-held
  voice tile connects the browser DIRECTLY to Gemini Live. `mint_voice_token()` builds its
  **own v1alpha `genai.Client`** (NOT the cached `gemini_voice` singleton, which lacks
  v1alpha and would reject the ephemeral mint) and calls `auth_tokens.create` with
  `live_connect_constraints` locking the model + the whole `LiveConnectConfig` (system
  prompt + voice + the **read/visual/scope-only tool manifest** — no write tools), `uses=1`,
  a ~60s new-session window, and `expire_time = VOICE_MAX_DURATION`. The token's constraints
  ARE the security envelope (no Redis ticket needed — the browser talks to Google, not
  Trinity). Response field is `ephemeral_token` (never `token`, which would flip orb.js's
  deferred write surface on). The orb page (which holds the JWT) mints and relays only the
  Google token to the nested voice iframe over `postMessage`. Writes stay off by
  construction: locked manifest + no `/session` route.
- **Agent-server mirror** (`agent_server/routers/brain_orb.py`): `GET /api/brain-orb/data`
  streams the fixed-path `~/resources/agent-visualization/data.json` via `FileResponse`.
  Scope + search run **agent convention hooks** (mirrors `~/.trinity/pre-check`, #454):
  `GET /api/brain-orb/scopes` runs `~/.trinity/brain-orb/scopes`; `POST /api/brain-orb/scope`
  pipes the body to `~/.trinity/brain-orb/scope` (mutate active set, re-export → rewrite
  `data.json`, print new state); `POST /api/brain-orb/tool` pipes a query to the read-only
  `~/.trinity/brain-orb/search` hook (scope-aware, no writes). All via hardened async
  subprocess (timeout-kill, output cap, JSON-parse + non-zero-exit guards); **404 when the
  hook is absent**. Trinity never runs `export_data.py` itself — the agent owns generation +
  scope state (Invariant #8).
- Platform flags (**runtime-resolved, admin-configurable — trinity-enterprise#85**): the three
  flags resolve at request time via `settings_service.is_brain_orb_enabled()` /
  `..._voice_enabled()` / `..._write_enabled()` — `system_settings` row (wins in both
  directions) → `BRAIN_ORB_*` env var as opt-in fallback → default OFF (one shared
  `_resolve_bool_flag` helper; fail-open on a settings-read failure; deliberately uncached,
  #506 `--workers 2` rationale). **Precedence note:** once a stored row exists the env var is
  ignored until the override is cleared (`PUT /api/settings/brain-orb {clear: [...]}` or
  generic `DELETE /api/settings/{key}`). Compositions: `brain_orb_available = base`;
  `brain_orb_voice_available = base && voice && GEMINI_API_KEY` (key stays env-only — secret);
  `brain_orb_write_available = base && write`; the voice-token mint route gates on
  `base && voice` too. Admin surface: `GET/PUT /api/settings/brain-orb` (Settings → General
  panel with per-flag source display). Voice frontend assets are CSP-clean (hand-rolled Gemini
  client, externalized `voice/voice.js`, same-origin `voice/mic-worklet.js`; `connect-src`
  already allows `wss:`). No DB change, no migration, no new secret.

---

