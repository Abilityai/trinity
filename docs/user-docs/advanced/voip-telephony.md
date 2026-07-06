# Outbound Phone Calls (VoIP)

Agents can place real outbound phone calls. The agent dials a number through Twilio and holds a live, interruptible spoken conversation powered by Gemini Live; after you hang up, the transcript flows back to the agent so it can act on what was discussed.

This is distinct from [Voice Chat](voice-chat.md): voice chat is *you* talking to your agent in the browser; VoIP is *the agent* calling a phone number over the public telephone network.

This release is **outbound only** — agents place calls; they do not answer incoming ones.

## Concepts

- **VoIP Binding** — A per-agent Twilio voice configuration: Account SID, Auth Token (stored encrypted), and a voice-capable from-number. Each agent owner brings their own Twilio account; calls bill to that account.
- **Feature Flag** — VoIP is off by default. It requires both `VOIP_ENABLED=true` and a `GEMINI_API_KEY` on the platform. When off, every VoIP endpoint returns 404.
- **Media Streams Bridge** — Twilio streams call audio to Trinity over a WebSocket; Trinity bridges it to the same Gemini Live engine that powers browser voice chat. The agent can interrupt and be interrupted mid-sentence (barge-in).
- **Post-Call Processing** — When the call ends, the full transcript is saved to the agent's chat history (source `voice`) and, by default, dispatched to the agent as a task so it can follow up — update memory, create tasks, send messages.
- **Abuse Controls** — A sliding-window rate limit per (owner, destination number) plus a durable per-agent daily call cap bound phone spend.

## Requirements

1. **Platform flags** — In `.env`: `VOIP_ENABLED=true` and a valid `GEMINI_API_KEY`. Restart the backend after changing them.
2. **Public URL** — **Settings → Public Chat URL** must be set to your instance's public domain (e.g. `https://your-domain.com`). Trinity builds the audio WebSocket URL from it.
3. **Public reachability** — Twilio must be able to open a WebSocket to `wss://your-domain.com/api/voip/voice/...`. This works on publicly reachable deployments, e.g. with a Cloudflare Tunnel set up per [Public Access](../guides/deploying/public-access.md). A catch-all tunnel route to the frontend needs nothing extra; a tunnel that routes specific paths directly to the backend must also route `api/voip/*`.
4. **Per-agent Twilio binding** — A Twilio account with a voice-capable phone number, configured on the agent (below). Without a binding, calls fail with 400 even when the flag is on.

## How It Works

### 1. Configure the Agent's Twilio Binding

The agent **owner** configures the binding — either in the UI or via the API.

**UI:** open the agent's **Sharing** tab → **Channels** → **Voice calls** row → **Configure** (the row appears only when VoIP is enabled platform-wide). The dialog ("Voice Calls (Twilio / VoIP)") takes the Twilio Account SID, Auth Token, From Number, and an optional daily call cap. Once connected, an **Enable/Disable** control flips calling on and off without re-entering credentials, and an **Agent voice** picker selects the Gemini voice used on calls (see below).

**API:**

```bash
curl -X PUT http://localhost:8000/api/agents/my-agent/voip \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "account_sid": "ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
    "auth_token": "your-twilio-auth-token",
    "from_number": "+15551234567",
    "daily_call_cap": 20
  }'
```

- **Account SID** — starts with `AC`, 34 characters. Trinity validates the credentials against Twilio before saving.
- **Auth Token** — stored encrypted (AES-256-GCM); never returned by the API.
- **From number** — a voice-capable Twilio number in E.164 format.
- **Daily call cap** — optional per-agent override of the platform default (50 calls/day).

`GET` the same endpoint to check binding status (returns the Twilio account's display name when configured); `DELETE` removes the binding. `PUT /api/agents/{name}/voip/enabled` with `{"enabled": false}` disables calling in place — the binding and credentials are kept, and outbound calls are refused until re-enabled.

### The Agent's Voice

Calls speak with the agent's persisted Gemini voice, shared with the in-app voice overlay. Pick it in the VoIP dialog's **Agent voice** selector or via `PUT /api/agents/{name}/voice/name`. Available voices: **Kore** (firm, the default), **Zephyr** (bright), **Puck** (upbeat), **Aoede** (breezy), **Charon** (informational), **Fenrir** (excitable), and **Gacrux** (mature). Clearing the setting reverts to Kore.

### 2. Place a Call

Anyone with access to the agent (owner, admin, or shared) can trigger a call — most commonly the agent itself, via the `call_user` MCP tool:

```typescript
mcp__trinity__call_user({
  to_number: "+15557654321",
  context: "Brief the user on today's pipeline status and ask about the Q3 budget decision."
})
// → { success: true, call_id: "voip_...", status: "ringing", twilio_call_sid: "CA..." }
```

The optional `context` (up to 2,000 characters) becomes the call's purpose in the agent's voice prompt — the agent greets the person, says who it is and why it's calling.

### 3. During the Call

- The conversation runs on Gemini Live with the agent's voice persona (same per-agent voice system prompt resolution as [Voice Chat](voice-chat.md)).
- The agent can delegate to its full Claude reasoning via the `run_task` tool mid-call — and announces it first ("let me check that") instead of going silent during slow lookups.
- You can interrupt the agent mid-sentence; it stops talking and listens.
- Calls are hard-capped at **10 minutes** by default (`VOIP_MAX_CALL_DURATION`).

### 4. After the Call

- The full transcript is saved into the agent's persistent chat history with source `voice`.
- By default the transcript is also dispatched to the agent as a task ("A phone call you placed just ended... take any appropriate follow-up"). Pass `process_transcript: false` to skip this.
- Calls that were never answered (empty transcript) skip post-call processing.

## For Agents

### MCP Tool

| Tool | Description |
|------|-------------|
| `call_user` | Place an outbound call. Params: `to_number` (E.164, required), `context` (≤2000 chars, optional), `process_transcript` (default `true`), `agent_name` (required for user-scoped keys; agent-scoped keys default to the bound agent) |

### API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/agents/{name}/voip` | GET | Binding status (owner-only) |
| `/api/agents/{name}/voip` | PUT | Configure Twilio voice binding — validates credentials with Twilio, encrypts the Auth Token (owner-only) |
| `/api/agents/{name}/voip` | DELETE | Remove the binding (owner-only) |
| `/api/agents/{name}/voip/enabled` | PUT | Enable/disable calling without re-entering credentials (owner-only; 404 if no binding) |
| `/api/agents/{name}/voice/name` | GET / PUT | The agent's persisted Gemini voice (PUT owner-only; applies to VoIP calls and the in-app voice overlay) |
| `/api/agents/{name}/voip/call` | POST | Place an outbound call; returns `{call_id, status: "ringing", to_number, twilio_call_sid, chat_session_id}` |
| `/api/voip/voice/{call_id}` | WebSocket | Twilio Media Streams audio bridge (ticket-authed, used by Twilio — not called directly) |

**API Endpoints**: See [Backend API Docs](http://localhost:8000/docs) for full schemas.

The call endpoint accepts an optional `Idempotency-Key` header, so a retried trigger never dials the same number twice.

```bash
curl -X POST http://localhost:8000/api/agents/my-agent/voip/call \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: briefing-2026-06-11" \
  -d '{"to_number": "+15557654321", "context": "Daily status briefing"}'
```

**Common errors:**

| Status | Meaning |
|--------|---------|
| 404 | VoIP is not enabled on the platform |
| 400 | No active binding, invalid phone number (must be E.164, e.g. `+15551234567`), or Public Chat URL not configured |
| 429 | Rate limit (default 5 calls per owner+destination per 60s) or daily call cap reached |
| 502 | Twilio rejected the call (e.g. unverified destination on a trial account) |

## Configuration

| Variable | Description | Default |
|----------|-------------|---------|
| `VOIP_ENABLED` | Master switch | `false` |
| `VOIP_MAX_CALL_DURATION` | Hard per-call cap in seconds | `600` (10 min) |
| `VOIP_DEFAULT_DAILY_CALL_CAP` | Per-agent calls/day (overridable per binding) | `50` |
| `VOIP_CALL_RATE_LIMIT` / `VOIP_CALL_RATE_WINDOW` | Calls per owner+destination per window (seconds) | `5` / `60` |

`GEMINI_API_KEY` (shared with voice chat) must also be set.

## Limitations

- **Outbound only** — Agents cannot receive calls in this release.
- **No call history UI** — Binding setup is in the Sharing tab, but call logs are API-only for now.
- **10-minute cap** — Calls end automatically at `VOIP_MAX_CALL_DURATION`.
- **Public deployment required** — Twilio must reach your instance over WSS; VoIP does not work on a localhost-only install.
- **Owner pays** — Calls bill to the agent owner's Twilio account at Twilio's voice rates.
- **Telephone audio quality** — Calls run over standard 8 kHz telephone audio, which is lower fidelity than browser voice chat.
- **Trial Twilio accounts** — Twilio trial accounts can only dial verified numbers; unverified destinations are rejected by Twilio.

## See Also

- [Voice Chat](voice-chat.md) — Talk to your agent in the browser (same Gemini Live engine)
- [Public Access](../guides/deploying/public-access.md) — Exposing your instance via Cloudflare Tunnel
- [WhatsApp Integration](../integrations/whatsapp-integration.md) — The same bring-your-own-Twilio pattern, for messaging
- [Agent Chat](../agents/agent-chat.md) — Where call transcripts appear

External references:

- [Twilio Programmable Voice](https://www.twilio.com/docs/voice) — Voice numbers, pricing, trial limits
- [Twilio Media Streams](https://www.twilio.com/docs/voice/media-streams) — The audio streaming protocol Trinity bridges
- [Google Gemini Live API](https://ai.google.dev/gemini-api/docs/live) — The real-time voice model behind the conversation
