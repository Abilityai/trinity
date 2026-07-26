# Voice Replies (Outbound TTS)

Agents reply in **text by default**. Voice is a **per-message choice**: during a channel turn, the agent calls the `send_voice_reply` tool to speak that one reply as a voice note on Telegram, Slack, or WhatsApp. Nothing is spoken unless the agent asks for it — there is no always-on voice mode.

Delivery is fail-soft. If synthesis fails, the channel is disabled, or the platform has no voice key, the agent simply falls back to plain text. A voice problem never loses a message.

This is the *outbound* counterpart of inbound voice transcription (users sending voice notes *to* the agent, covered in each channel's doc).

## Concepts

- **Per-message voice** — Voice is opted into one reply at a time by the agent via the `send_voice_reply` tool. It is not a channel-wide "speak everything" setting.
- **Agent voice** — An ElevenLabs voice ID (e.g. `21m00Tcm4TlvDq8ikWAM`) picked by the owner. It selects the voice the agent speaks with.
- **Platform default voice** — A fallback voice ID an admin sets platform-wide. When an agent has no voice of its own, the default is used. Enabling voice replies requires **either** an agent voice **or** the platform default.
- **Per-channel allow flags** — Independent Telegram / Slack / WhatsApp switches. A channel with its flag off refuses voice for that agent even when the agent asks for it (the reply goes out as text).
- **Runtime-resolved key** — The ElevenLabs key is resolved live from a stored platform setting, then an environment-variable fallback. An admin can set or rotate it in Settings with no restart; the stored value is encrypted at rest and never echoed back.
- **Fail-soft** — Any failure (missing key, channel flag off, synthesis/transcode/upload error) delivers the reply as text.

## How It Works

### 1. Admin — provide a voice key (once)

Set the ElevenLabs key under **Settings** (stored setting, with an env-var fallback for older deployments). Optionally set a **platform default voice** there too. Until a key resolves, the whole feature is unavailable — the `tts_available` flag is false and the agent config UI is disabled.

### 2. Owner — enable and configure the agent

1. Open the agent's **Sharing** tab and its voice-replies configuration.
2. Turn **voice replies on** and pick a **voice** — or rely on the platform default voice. Enabling with neither an agent voice nor a platform default is rejected.
3. Set the **per-channel allow flags** for Telegram, Slack, and WhatsApp. Each channel is independent; leave a channel off to keep it text-only.

### 3. Agent — opt a reply into voice

During a channel turn, the agent calls `send_voice_reply` to speak that specific reply. The backend resolves the channel destination from the current execution, checks the agent-level enable plus the per-channel flag, synthesizes the audio, and delivers it. If any check fails, it reports "not delivered" and the agent sends text instead.

### Per-Channel Delivery

| Channel | Delivery format |
|---------|-----------------|
| Telegram | OGG/Opus voice note (`sendVoice`); in groups it replies to the triggering message |
| Slack | Inline MP3 clip uploaded into the thread (Slack renders MP3 with a built-in player) |
| WhatsApp | OGG voice note delivered via Twilio media (hosted transiently by Trinity) |

WhatsApp voice notes work even when the agent's file-sharing toggle is off — voice replies are gated only by their own flags.

## For Agents

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/agents/{name}/voice-replies` | GET | `{enabled, voice_id, channels:{telegram,slack,whatsapp}, effective_voice_id, default_voice_id, available}` — `available` reflects a resolvable platform key |
| `/api/agents/{name}/voice-replies` | PUT | Owner-only partial update of `{enabled?, voice_id?, channels?}`; 400 if none provided |
| `/api/agents/{name}/voice-reply` | POST | Per-message delivery backing the tool; fail-soft `{delivered, channel, reason}`; 409 on an in-flight duplicate for the same turn |
| `/api/settings/elevenlabs` | GET / PUT | Admin key + default voice: `{key_configured, key_source, default_voice_id}`; key stored encrypted, never echoed |

MCP tool:

| Tool | Description |
|------|-------------|
| `send_voice_reply` | Speak the current channel reply as a voice note. Backend resolves the destination from the execution, gates on the agent + per-channel flags, and returns fail-soft so the agent can fall back to text |

See [Backend API Docs](http://localhost:8000/docs) for full request/response schemas.

## Limitations

- Voice replies cover the messaging channels (Telegram, Slack, WhatsApp) — not the web chat UI or public links.
- Voice is per-reply — the agent decides each time; there is no way to force every reply to be spoken.
- The voice is an ElevenLabs voice; it is unrelated to the Gemini voice used for [Voice Chat](voice-chat.md) and [VoIP calls](voip-telephony.md).
- Synthesis cost accrues on your ElevenLabs account per character spoken.

## See Also

**Trinity docs:**

- [Telegram Integration](../integrations/telegram-integration.md) · [Slack Integration](../integrations/slack-integration.md) · [WhatsApp Integration](../integrations/whatsapp-integration.md)
- [Voice Chat](voice-chat.md) — live spoken conversation in the browser (Gemini)
- [VoIP Telephony](voip-telephony.md) — outbound phone calls (Gemini)

**External references:**

- [ElevenLabs: Voices](https://elevenlabs.io/docs/capabilities/voices) — finding and creating voice IDs
- [ElevenLabs: Text to Speech API](https://elevenlabs.io/docs/api-reference/text-to-speech/convert) — the synthesis primitive Trinity calls
