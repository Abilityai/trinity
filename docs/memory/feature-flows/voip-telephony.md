# Feature: VoIP Telephony — Outbound Calls over Gemini Live (VOIP-001)

> **Status (2026-06-04)**: Phase 1 (outbound) implemented for #1056. Behind a
> feature flag that is **OFF by default** (`VOIP_ENABLED`). Inbound (Phase 2)
> and UI/observability (Phase 3) are not yet built. Real PSTN path is
> manual-verify (needs a live Twilio voice number).

## Overview

An agent places an outbound phone call to a user and holds a real-time,
interruptible spoken conversation. A phone call is just a **different audio
transport** feeding the **existing, unmodified** Gemini Live voice bridge
(`services/gemini_voice.py`). Twilio Programmable Voice + bidirectional Media
Streams carry G.711 μ-law 8kHz audio over a WebSocket; the adapter resamples it
into the PCM queues the Gemini bridge already exposes. After the call, the full
transcript is handed to the **main agent** to process.

## User Story

"My agent calls me on the phone, briefs me, and then — once we hang up — acts on
what we discussed (updates memory, creates tasks, sends follow-ups)."

## Entry Points

- **REST**: `POST /api/agents/{name}/voip/call` (JWT/MCP; `AuthorizedAgentByName`)
- **MCP**: `call_user` tool (`src/mcp-server/src/tools/voip.ts`)
- **Binding config (owner-only)**: `GET/PUT/DELETE /api/agents/{name}/voip`
- **Media Streams WS (Twilio, ticket-authed)**: `WS /api/voip/voice/{call_id}`
- **Feature flag**: `GET /api/settings/feature-flags` → `voip_available`

---

## Backend Layer

### Three-layer module (Invariant #1)

| Layer | File | Responsibility |
|-------|------|----------------|
| Router | `src/backend/routers/voip.py` | Binding CRUD, outbound trigger (idempotency + audit), Media Streams WS entrypoint |
| Service | `src/backend/services/voip_service.py` | Gate checks, abuse controls, intent staging, Twilio `calls.create`, post-call transcript dispatch |
| DB | `src/backend/db/voip.py` (`VoipOperations`) | `voip_bindings` CRUD + AuthToken encryption, `voip_call_logs` lifecycle, durable daily-cap count |

### Audio bridge (codec work lives outside `gemini_voice.py`)

- `src/backend/adapters/transports/twilio_media_stream.py` — the live WS bridge
  (`handle_media_stream`). Per-connection `_CallBridge`: inbound loop, outbound
  queue + paced 20ms 160-byte μ-law sender, `clear`-on-barge-in, teardown.
- `src/backend/adapters/transports/voip_audio.py` — pure, dependency-free codec
  helpers (`ulaw8k_to_pcm16k`, `pcm24k_to_ulaw8k`, `pop_frames`). Carries
  per-direction `audioop.ratecv` state across chunks (the anti-click guarantee).

**Bridge no longer strictly "unmodified"**: codec work still lives entirely in
the transport (above), but two non-codec session knobs are now threaded through
`gemini_voice.py` and benefit both transports: a per-session `max_duration`
(the watchdog honors `VOIP_MAX_CALL_DURATION` for phone calls — see Config) and
a shared `_TOOL_ETIQUETTE_INSTRUCTION` appended to `system_instruction` so the
agent says a brief filler ("let me check that") before a slow `run_task` call
instead of going silent. Both are described in [voice-chat.md](voice-chat.md)
(VOICE-007, VOICE-009).

### Outbound call flow

```
POST /api/agents/{name}/voip/call (Idempotency-Key?)
      │
      ▼  routers/voip.py
  idempotency_service.begin(agent_scope, key)   # dup trigger → 409 / replay snapshot
      │
      ▼  voip_service.place_outbound_call(...)
  is_available()?  ──no──► 404
  binding+enabled? ──no──► 400
  rate_limiter.enforce(owner+dest)              # 429
  db.count_voip_calls_since(agent) < cap?       # durable daily cap → 429
  db.get_or_create_chat_session(owner)          # transcript home (owner identity)
  call_id = token_urlsafe(24)                   # routing token (≠ vs_ session id)
  ticket = mint_ticket(scope=f"voip:{call_id}", ttl=180s)
  redis.SETEX voip_intent:{call_id} = {agent, chat_session, user, prompt, to, process_transcript}
  Twilio.calls.create(to, from_=binding.from_number,
        twiml="<Connect><Stream url='wss://…/api/voip/voice/{call_id}?ticket=…'/></Connect>")
      │  (on Twilio failure → delete intent, mark call failed, 502 → idempotency.fail)
      ▼
  202 { call_id, status: "ringing", twilio_call_sid }
```

### Media Streams WS flow (runs on whichever worker Twilio connects to)

```
WS /api/voip/voice/{call_id}?ticket=…
      │  adapters/transports/twilio_media_stream.handle_media_stream
  consume_ticket(ticket).scope == f"voip:{call_id}" ?  ──no──► close 4001
  redis.GETDEL voip_intent:{call_id}                  ──none─► close 4004  (consume-once)
  voice_service.create_session(...)  → vs_<id>        # Gemini session created HERE
      │
      ├─ Twilio "start"  → capture streamSid
      ├─ Twilio "media"  → ulaw2lin → ratecv(8k→16k) → voice_service.send_audio(vs)
      ├─ on_audio_out(PCM24k) → ratecv(24k→8k) → lin2ulaw → buffer
      ├─ paced sender (20ms) → 160-byte μ-law frames → Twilio "media"
      ├─ on_transcript(user) while agent speaking → Twilio "clear" + drop buffer  (barge-in)
      └─ Twilio "stop" / disconnect / gemini ends → teardown
                 │
                 ▼  _finalize (SETNX voip_saved:{call_id} → exactly once)
            _save_transcript(session, source="voice")   # chat_messages
            voip_service.process_call_transcript(...)    # task_execution_service.execute_task(triggered_by="voip")
```

### Two-id namespace (do not conflate)

- `call_id` — chosen at trigger time; in the WSS URL + ticket binding + Redis
  intent key. A high-entropy routing token.
- `vs_<id>` — the Gemini `VoiceSession.session_id`, minted at WS-connect inside
  the **unmodified** `create_session`. Everything (`send_audio`, `end_session`,
  transcript) keys on this.

---

## Database Layer

`voip_bindings` (one Twilio voice sender per agent — separate from
`whatsapp_bindings`): `account_sid`, AES-256-GCM-encrypted `auth_token`,
`from_number`, `inbound_number` (Phase 2, nullable), `webhook_secret`,
`daily_call_cap`, `enabled`. AuthToken wrapped via `CredentialEncryptionService`
(Invariant #12).

`voip_call_logs` (one row per outbound call): `call_id`, `agent_name`,
`chat_session_id`, `to_number`, `status` (initiated→ringing→connected→completed/
failed), `twilio_call_sid`, `initiated_by_*`, `started_at`. The
`(agent_name, started_at)` index backs the durable daily-cap count.

DDL in `db/schema.py`; migration `_migrate_voip_tables` in `db/migrations.py`
(registered as `voip_tables`) — Invariant #3.

---

## Security

| Surface | Control |
|---------|---------|
| Feature exposure | `voip_available` flag OFF by default (`VOIP_ENABLED`) + per-agent `voip_bindings` row required |
| Binding CRUD | Owner-only (`OwnedAgentByName`); AuthToken AES-256-GCM at rest |
| Outbound trigger | `AuthorizedAgentByName` (JWT/MCP); rate-limited per `(owner, destination)`; durable per-agent daily call cap; optional `Idempotency-Key` (Invariant #18) |
| PSTN spend | On the **agent owner's** Twilio account (each owner brings their own creds) |
| Media Streams WS | Twilio can't send a JWT → single-use, **call-bound** ticket (`scope="voip:{call_id}"`, 180s TTL); staged intent consumed once via Redis `GETDEL` |
| TwiML | URL attribute `quoteattr`-escaped (no injection) |
| Transcript save | SETNX sentinel → exactly-once across double-teardown |

**Deferred to Phase 2**: a formal opt-in destination allowlist (mirroring
`agent_sharing.allow_proactive`). Phase 1 bounds blast radius with
flag-off + own-Twilio + rate-limit + daily cap.

## Three surfaces in sync (Invariant #13)

Backend router (`routers/voip.py`) ↔ MCP tool (`src/mcp-server/src/tools/voip.ts`,
registered in `server.ts`, client method `placeVoipCall` in `client.ts`). No
agent-server mirror — the bridge is backend-only.

## Config

`VOIP_ENABLED` (default `false`), `VOIP_MAX_CALL_DURATION` (600s / 10 min),
`VOIP_DEFAULT_DAILY_CALL_CAP` (50), `VOIP_TICKET_TTL_SECONDS` (180),
`VOIP_INTENT_TTL_SECONDS` (180), `VOIP_CALL_RATE_LIMIT` / `VOIP_CALL_RATE_WINDOW`.
All read via `os.getenv` in `src/backend/config.py`. `audioop-lts` pinned for
`python_version >= "3.13"` (stdlib `audioop` removed in 3.13).

**Call duration**: `VOIP_MAX_CALL_DURATION` (default 600s / 10 min) is the phone
call's hard cap, enforced by the shared `gemini_voice._timeout_watchdog`. The
trigger path passes it into `voice_service.create_session(max_duration=…)` so the
per-session watchdog sleeps on it rather than the 5-min browser `VOICE_MAX_DURATION`.
(Until this wiring the constant was defined but unused — every session, phone
included, inherited the 300s voice cap, so calls were silently cut at 5 min.)

### Deployment / packaging (env vars must reach the container)

Because `VOIP_ENABLED` defaults **OFF**, the master switch only takes effect if
it is actually present in the backend container's environment — a code default of
`false` means setting `VOIP_ENABLED=true` in `.env` is a no-op unless compose
forwards it. The full `VOIP_*` set is wired into the `backend.environment:` block
of **both** `docker-compose.yml` (dev) and `docker-compose.prod.yml` (prod, which
launches standalone with no base-compose merge), plus the `VOICE_*` flags for
parity, and is documented in `.env.example`. Omitting these from the prod compose
was the original packaging gap (same class as #1039's `LOG_*` no-op) — the
supported `.env` lever did nothing because it never reached the container. The
enterprise prod overlay (`docker-compose.prod.enterprise.yml`) inherits this env
block, so no separate wiring is needed there.

### Operator enablement checklist (prod)

1. **Code/packaging** (in-repo, ships via `/update`): `VOIP_*` lines present in the
   prod compose `backend.environment:` — ✅ done.
2. **`.env`**: set `VOIP_ENABLED=true` (and `GEMINI_API_KEY`); recreate the backend
   container so the value is read.
3. **Cloudflare Tunnel ingress**: the Media Streams socket is
   `WS /api/voip/voice/{call_id}`. The frontend nginx `location /api/` block
   already proxies with `Upgrade`/`Connection` headers, so a catch-all
   `* → frontend:80` tunnel hostname needs nothing extra; a tunnel that routes
   specific paths straight to `backend:8000` must add an `api/voip/.*` route. This
   is Cloudflare-dashboard config (`cloudflared` runs `tunnel run` with a
   `TUNNEL_TOKEN`), not repo state.
4. **Per-agent binding**: configure Twilio voice creds via
   `PUT /api/agents/{name}/voip` (owner-only). Without a `voip_bindings` row the
   feature flag is on but calls 400.

## Testing

- `tests/unit/test_voip_audio.py` — codec round-trip, **stateful `ratecv`
  byte-for-byte continuity** vs stateless divergence (anti-click), 160-byte
  framing. Skips where `audioop`/`audioop-lts` absent; runs in the 3.11 container.
- `tests/unit/test_voip_db.py` — binding CRUD + encryption envelope, call-log
  lifecycle, durable daily-cap window count, migration idempotency.
- `tests/unit/test_voip_service.py` — E.164 validation, ws(s):// base + escaped
  TwiML, and `place_outbound_call` gates (flag/binding/cap/rate, mocked Twilio).
- **Manual-verify**: real outbound PSTN call (needs a live Twilio voice number).

## Accepted risk (instrument, don't block)

PSTN barge-in latency and telephone-speech (8kHz μ-law) STT quality are
unvalidated until real calls happen — Phase 1 logs the barge-in/transcript
signals so they can be measured on the first live calls.

## Related Flows

- **Upstream**: `voice-chat.md` (the Gemini Live bridge this reuses unchanged),
  `whatsapp-integration` (the Twilio binding/encryption pattern this mirrors).
- **Downstream**: post-call processing reuses `task_execution_service.execute_task`
  (`triggered_by="voip"`).
