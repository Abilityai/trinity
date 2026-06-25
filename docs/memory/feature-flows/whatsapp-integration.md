# WhatsApp Integration via Twilio (WHATSAPP-001)

**Issues**: #299 (Phase 1), #467 (Phase 2), #1315 (Phase 3 — outbound media)
**Status**: Phase 3 partial — outbound media attachments shipped (#1315)
**Extends**: SLACK-002 `ChannelAdapter` abstraction

Adds WhatsApp as a per-agent channel via Twilio's Programmable Messaging API.
Each agent owner brings their own Twilio AccountSid + AuthToken + from-number —
no platform-level Twilio account required.

## Scope

### In scope (Phase 1)
- One Twilio sender per agent (AccountSid + AuthToken + WhatsApp from-number)
- Inbound DMs → `WhatsAppAdapter` → `ChannelMessageRouter` → `TaskExecutionService`
- Outbound responses via Twilio REST (`POST /Messages.json`, HTTP Basic auth)
- Media in (images, audio, PDFs via Twilio-hosted URLs — SSRF-gated)
- AuthToken encrypted at rest (AES-256-GCM via `CredentialEncryptionService`)
- Webhook HMAC-SHA1 verification via `twilio.request_validator.RequestValidator`
- Twilio Sandbox auto-detected from the well-known number `whatsapp:+14155238886`

### In scope (Phase 2 — #467, shipped 2026-04-23)
- `/login`, `/logout`, `/whoami` commands (dispatched by `twilio_webhook._process_update`)
- Redis-backed pending-login state with 10-minute TTL
  (keyed `whatsapp_pending_login:{binding_id}:{phone}`)
- Post-verification access gate inlined into `/login` so users learn their
  access status (shared / open_access / pending-approval) in the same message
- `access_requests.channel='whatsapp'` for restrictive-policy DMs
- `proactive_message_service._deliver_whatsapp()` — explicit-only channel
  (NOT part of `auto` fallback; see Phase 2 notes below)
- Markdown → WhatsApp-native syntax conversion in `send_response` so agent
  markdown (`**bold**`, `[text](url)`) renders correctly

### In scope (Phase 3 — #1315, outbound media, shipped)
- Outbound media attachments: `WhatsAppAdapter.send_response` delivers
  `ChannelResponse.files` as Twilio `MediaUrl` (one message per file), reusing
  FILES-001 storage to mint a public `?sig=` URL Twilio fetches server-side.
  Gated on `file_sharing_enabled`; text-link fallback when undeliverable as
  media. See *Flow: Outbound media attachments* below.

### Deferred
- **Phase 3 (remaining)** — SMS on the same Twilio binding, WhatsApp Business
  templates (for outbound-first messaging outside 24h window), interactive
  buttons, voice-note transcription, and promoting FILES-001 share URLs already
  present in `response.text` to `MediaUrl` (#1315 option b)
- **Out of scope** — WhatsApp group chats (Twilio's WhatsApp API does not
  support them)

## Entry Points

- **Backend router** (authenticated):
  - `GET /api/agents/{name}/whatsapp` — binding status
  - `PUT /api/agents/{name}/whatsapp` — configure credentials
  - `DELETE /api/agents/{name}/whatsapp` — remove binding
  - `POST /api/agents/{name}/whatsapp/test` — verify credentials / send test
- **Backend router** (public, HMAC-verified):
  - `POST /api/whatsapp/webhook/{webhook_secret}` — Twilio inbound webhook
- **Frontend**: Agent Detail → Sharing tab → `WhatsAppChannelPanel.vue`

## Components

| Layer | File |
|-------|------|
| Adapter | `src/backend/adapters/whatsapp_adapter.py` |
| Transport | `src/backend/adapters/transports/twilio_webhook.py` |
| DB ops | `src/backend/db/whatsapp_channels.py` |
| DB facade methods | `src/backend/database.py` (WHATSAPP-001 section) |
| Schema migration | `src/backend/db/migrations.py::_migrate_whatsapp_bindings` |
| Router | `src/backend/routers/whatsapp.py` |
| Main wiring | `src/backend/main.py` (router includes + lifespan start/stop) |
| Settings back-fill | `src/backend/routers/settings.py` (piggybacks on `public_chat_url` save) |
| UI panel | `src/frontend/src/components/WhatsAppChannelPanel.vue` |
| UI mount | `src/frontend/src/components/SharingPanel.vue` |
| Outbound media (#1315) | `src/backend/services/agent_shared_files_service.py::create_share_from_bytes` (FILES-001 reuse) |
| New dependency | `twilio==9.10.5` (in `docker/backend/Dockerfile`) |

## Flow: Inbound WhatsApp message

```
WhatsApp user's phone
        │ (Meta's WhatsApp network)
        ▼
Twilio WhatsApp API
        │ POST form-encoded body
        │ X-Twilio-Signature: HMAC-SHA1(AuthToken, URL + sorted params)
        ▼
Cloudflare Tunnel (path-filtered: /api/whatsapp/webhook/*)
        │
        ▼
nginx (sets X-Forwarded-Proto $scheme)
        │
        ▼
uvicorn --proxy-headers (so request.url is https://...)
        │
        ▼
routers/whatsapp.py :: handle_twilio_webhook()
        │
        ▼
TwilioWebhookTransport.handle_webhook()
   1. Resolve binding by webhook_secret
   2. Decrypt AuthToken
   3. RequestValidator.validate()
   4. Dedup by MessageSid
   5. Inject _binding_id + _agent_name
   6. asyncio.create_task(process) — returns 200 empty TwiML
        │
        ▼
WhatsAppAdapter.parse_message() → NormalizedMessage
        │
        ▼
ChannelMessageRouter.handle_message() (unchanged)
        │  — rate limit
        │  — access control (#311 — uses resolve_verified_email()
        │     wired in Phase 2; unverified + require_email → prompt_auth)
        │  — session lookup
        │  — file downloads (via adapter.download_file with SSRF allowlist)
        │  — TaskExecutionService.execute_task()
        │
        ▼
WhatsAppAdapter.send_response()
        │
        ▼
Twilio REST: POST /2010-04-01/Accounts/{sid}/Messages.json
        │  (HTTP Basic AccountSid:AuthToken)
        ▼
WhatsApp user's phone
```

## Flow: `/login` command (Phase 2, #467)

```
WhatsApp user sends "/login user@example.com"
        │
        ▼
twilio_webhook._process_update
   — detects Body.startswith("/")
   — parses via WhatsAppAdapter.parse_message
   — if adapter.is_command(normalized):
        reply = await adapter.handle_command(normalized)
        adapter.send_response(channel_id, reply)    ← short-circuits the router
        return
        │
        ▼
WhatsAppAdapter._handle_login_command("/login user@example.com")
   1. Validate email shape (contains '@', no spaces, ≤254 chars)
   2. db.create_login_code(email, expiry_minutes=10)     ← shared with web/Telegram
   3. EmailService.send_verification_code(email, code)
   4. _set_pending_login(binding_id, wa_user_phone, email) — Redis SETEX 10min
   5. Return "📧 Sent a 6-digit code to `user@example.com`..."
        │
        ▼
WhatsApp user sends "/login 123456"
        │
        ▼
_handle_login_command("/login 123456")
   1. pending_email = _get_pending_login(binding_id, phone)  — Redis GET
   2. db.verify_login_code(pending_email, "123456")          — crypto gate
   3. db.set_whatsapp_verified_email(binding_id, phone, email)
   4. _clear_pending_login(binding_id, phone)
   5. RUN ACCESS GATE INLINE:
       if email_has_agent_access OR policy.open_access:
           → "✅ Verified! You can chat normally."
       else:
           db.upsert_access_request(agent_name, email, "whatsapp")
           → "✅ Verified. 🔒 Access pending approval."
```

### Why inline the access gate?
Without it, a verified-but-not-shared user would see *"You can chat normally"*
at verification time, then hit *"Access pending approval"* on their first real
message. The inline check matches the Telegram pattern.

## Flow: Outbound markdown conversion

Agents emit standard markdown; WhatsApp uses its own syntax. `send_response`
runs `_markdown_to_whatsapp` before every Twilio POST so responses render
correctly. Conversions:

| Markdown | WhatsApp |
|----------|----------|
| `**bold**` / `__bold__` | `*bold*` |
| `*italic*` | unchanged (WA reads as bold — acceptable Phase 2 compromise) |
| `_italic_` | `_italic_` (passthrough) |
| `~~strike~~` | `~strike~` |
| ``` `code` ```  / ``` ```block``` ``` | passthrough |
| `# Header` / `## Sub` | `*Header*` |
| `[text](url)` | `text (url)` |

Implemented as a pure static method on `WhatsAppAdapter`; also exposed via
`format_response` for non-send callsites (e.g. proactive delivery).

## Flow: Outbound media attachments (#1315)

Agents can attach files to a reply. `ChannelMessageRouter._extract_outbound_files`
already pulls large fenced code blocks (csv/json/etc., ≤500 KB each, ≤2 MB total,
≤5 files) out of the response text into `ChannelResponse.files` (`OutboundFile`,
bytes in memory). WhatsApp delivers them as Twilio `MediaUrl` attachments.

Twilio fetches a `MediaUrl` **server-side from the public internet** — it cannot
upload bytes the way Slack does. So each file is persisted to FILES-001 storage
and its public `?sig=` download URL is handed to Twilio.

```
WhatsAppAdapter.send_response(channel_id, response)
        │
        ▼
_prepare_outbound_media(agent_name, response.files)   ← per-file isolated
   gate: db.get_file_sharing_enabled(agent_name)       (off → text-only + WARN)
   for each OutboundFile:
     create_share_from_bytes(agent_name, f.content,    ← FILES-001 reuse
         display_name=f.filename, expires_in=3600,      (1h TTL; reaper purges)
         created_by=agent_name)  → {url, mime_type, size_bytes}
       · HTTPException/any error → WARN + skip (never aborts text or siblings)
     classify:
       · url not https://         → text-link fallback (public_chat_url unset)
       · mime unsupported          → text-link fallback
       · size > WhatsApp cap       → text-link fallback
       · else                      → MediaUrl
   → (media_urls, fallback_links)
        │
        ▼
body = markdown(agent_text) + "\n\n" + fallback links (verbatim, NOT markdownized)
   1. send text body first (chunked) — survives total media failure
   2. one _send_message(..., media_url=...) per file  (WhatsApp = 1 media/msg)
```

Key decisions (#1315):
- **Why FILES-001 + URL, not direct upload**: Twilio's WhatsApp send only accepts
  a publicly-fetchable `MediaUrl`. The unauthenticated `/api/files/{id}?sig=`
  endpoint (192-bit token, constant-time compare, `attachment` + `nosniff`) is
  exactly that. `create_share_from_bytes` is the bytes-input twin of
  `create_share`; both share `_persist_and_register` (MIME-blocklist → quota →
  disk-check → persist → DB → URL).
- **Short 1h TTL** (`_OUTBOUND_MEDIA_EXPIRES_IN`): Twilio fetches the URL within
  seconds; the cleanup-service reaper (`_sweep_shared_files`) purges expired
  shares, so the public link doesn't linger. NOT revoke-after-send — Twilio's
  fetch is async *after* the POST 2xx, so revoking on the ack would race it.
- **Caps** (`_outbound_media_cap_for_mime`): image/audio/video ≈5 MB, documents
  (`text/*` + json/xml/pdf/yaml/sql/js) ≈16 MB; `application/octet-stream` /
  unknown → undeliverable → text link. Classified on the *detected* MIME (the
  download endpoint serves `Content-Type: mime_type`, which Twilio reads to
  render the media), not the code-fence `language` hint.
- **`file_sharing_enabled` gate**: outbound media reuses the same public storage
  + quota as the `share_file` MCP tool, so it honors the same per-agent toggle.
  Off → files are not delivered as media (the `(see attached: …)` placeholder
  text remains) + WARN.
- **Idempotency**: none added — `twilio_webhook` dedups inbound by `MessageSid`
  before dispatch, so `send_response` (and each mint) runs exactly once.

## Flow: Proactive messaging (`_deliver_whatsapp`)

```
proactive_message_service.send_message(agent, email, text, channel="whatsapp")
        │
        ▼
_deliver_whatsapp(agent_name, recipient_email, text)
   1. binding = db.get_whatsapp_binding(agent_name)
       → raise RecipientNotFoundError if no binding
   2. chat_link = db.get_whatsapp_chat_link_by_verified_email(binding.id, email)
       → raise RecipientNotFoundError if no prior verified user with this email
   3. auth_token = db.get_whatsapp_auth_token(agent_name)
   4. text = adapter.format_response(text)  ← markdown conversion
   5. adapter._send_message(account_sid, auth_token, from_number,
                            messaging_service_sid, to=phone, body=text)
   6. return DeliveryResult(success, channel="whatsapp", message_id=sid)
```

**Important**: WhatsApp is NOT in the default `auto` channel fallback
(`["telegram", "slack", "web"]`). Callers must pass `channel="whatsapp"`
explicitly. Rationale: Twilio enforces a 24-hour session window for
business-initiated freeform messages — outside the window Twilio returns
error `63016`. Including WhatsApp in `auto` would cause intermittent silent
failures for inactive recipients. Phase 3 (approved message templates) will
revisit this once template support lands.

## Security

### Webhook authentication
**Two-factor gate**:
1. `webhook_secret` in URL path — random 32-byte token, stored encrypted in DB,
   resolves to the right binding; unknown secrets return 200 (no leak)
2. `X-Twilio-Signature` — HMAC-SHA1 of `URL + sorted form params`, keyed on the
   agent's AuthToken, verified by `twilio.request_validator.RequestValidator`.
   Uses `hmac.compare_digest` internally (constant-time).

### Credential handling
- **AuthToken**: AES-256-GCM encrypted via shared `CredentialEncryptionService`
  (same as Slack/Telegram); never logged in full
- **AccountSid**: stored plaintext (public identifier — appears in URLs by design)
- Log masking: phone numbers rendered as `whatsapp:+141***5309` in error paths

### SSRF defense on media downloads
- Twilio media URLs are fetched with HTTP Basic auth
- URL host must match `*.twilio.com` (allowlist in `whatsapp_adapter._is_twilio_media_url`)
- `follow_redirects=False` on the initial request; a single 30x redirect is
  followed only if the target also passes the allowlist check
- Post-download size validation handled by `message_router`'s existing TOCTOU check

### Proxy-header correctness
- nginx already sets `X-Forwarded-Proto $scheme` (`src/frontend/nginx.conf:30`)
- uvicorn runs with `--proxy-headers --forwarded-allow-ips='*'` so `request.url`
  reconstructs to `https://public.example.com/...` — which is the URL Twilio
  signed. Without these flags, every signature check would fail.

## Deployment Prerequisite

Twilio webhooks arrive at `public.your-domain.com/api/whatsapp/webhook/*`,
which goes through Cloudflare Tunnel path filtering.

**Admins must add the path to the Cloudflare Tunnel ingress rules** (manual
dashboard step — see `docs/requirements/PUBLIC_EXTERNAL_ACCESS_SETUP.md`):

| Route | Service |
|-------|---------|
| `/api/whatsapp/webhook/*` | `http://backend:8000` (or `http://frontend:80` if frontend nginx proxies `/api/*`) |

Without this rule, Twilio webhooks return 404 at the Cloudflare edge and never
reach the backend — the UI surfaces a yellow banner reminding the admin.

### Verification
```bash
PUBLIC=https://public.your-domain.com
# Should NOT return 404 (returns 200 empty TwiML for unknown secret):
curl -s -o /dev/null -w "%{http_code}\n" -X POST "$PUBLIC/api/whatsapp/webhook/test"
```

## Database Schema

```sql
CREATE TABLE whatsapp_bindings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name TEXT NOT NULL UNIQUE,
    account_sid TEXT NOT NULL,
    auth_token_encrypted TEXT NOT NULL,          -- AES-256-GCM
    from_number TEXT NOT NULL,                   -- 'whatsapp:+E164'
    messaging_service_sid TEXT,                  -- optional, preferred over from_number
    display_name TEXT,                           -- friendly_name from Twilio
    is_sandbox INTEGER DEFAULT 0,                -- auto-detected
    webhook_secret TEXT NOT NULL UNIQUE,
    webhook_url TEXT,                            -- computed from public_chat_url
    enabled INTEGER DEFAULT 1,
    created_by TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT
);
CREATE INDEX idx_whatsapp_bindings_agent ON whatsapp_bindings(agent_name);
CREATE INDEX idx_whatsapp_bindings_webhook ON whatsapp_bindings(webhook_secret);

CREATE TABLE whatsapp_chat_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    binding_id INTEGER NOT NULL REFERENCES whatsapp_bindings(id),
    wa_user_phone TEXT NOT NULL,                 -- 'whatsapp:+E164'
    wa_user_name TEXT,                           -- Twilio ProfileName
    session_id TEXT,
    verified_email TEXT,                         -- #311 Phase 2
    verified_at TEXT,
    message_count INTEGER DEFAULT 0,
    last_active TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(binding_id, wa_user_phone)
);
CREATE INDEX idx_whatsapp_chat_links_binding ON whatsapp_chat_links(binding_id);
```

## Twilio Setup (user responsibility)

### Development — Sandbox
1. Twilio Console → Messaging → Try WhatsApp
2. Copy AccountSid + AuthToken
3. Sandbox sender is `whatsapp:+14155238886` (shared across Twilio users)
4. Users opt in by sending `join <sandbox-keyword>` from their phone
5. In Trinity: Agent Detail → Sharing → WhatsApp → paste credentials +
   `whatsapp:+14155238886` as from-number
6. Copy the webhook URL from Trinity → paste into Twilio Sandbox → "When a
   message comes in" (HTTP POST)

### Production
1. Twilio Console → Senders → register WhatsApp sender (requires Meta Business Manager linkage, 24–48h display-name approval)
2. Paste production AccountSid + AuthToken + sender from-number into Trinity
3. Configure the webhook URL on the registered sender in Twilio Console

## Twilio Error Codes (logged, not user-facing)

| Code | Meaning | Our handling |
|------|---------|--------------|
| 21211 | `To` number is not a valid WhatsApp number | Log, skip |
| 21408 | Region not enabled for WhatsApp | Log, skip |
| 63016 | Freeform message outside 24-hour customer-service window | Log — MVP limitation; templates come in Phase 3 |

## Related

- `docs/memory/feature-flows/telegram-integration.md` — closest precedent; diffs noted in this doc
- `docs/memory/feature-flows/slack-channel-routing.md` — SLACK-002 adapter abstraction
- `docs/requirements/PUBLIC_EXTERNAL_ACCESS_SETUP.md` — Cloudflare Tunnel ingress config
- `src/backend/adapters/message_router.py` — channel-agnostic pipeline (unchanged by this feature)
