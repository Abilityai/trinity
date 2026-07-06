# Voice Replies (Outbound TTS)

Agents can speak their channel replies as voice notes. When enabled, replies on Telegram, Slack, and WhatsApp are synthesized to audio with ElevenLabs and delivered in the channel's native voice format; long replies and any synthesis failure fall back to plain text automatically.

This is the *outbound* counterpart of inbound voice transcription (users sending voice notes *to* the agent, covered in each channel's doc).

## Concepts

- **Voice ID** — An ElevenLabs voice identifier (e.g. `21m00Tcm4TlvDq8ikWAM`) from your ElevenLabs account. It selects the voice the agent speaks with.
- **One setting, all channels** — Voice replies are configured once per agent and apply to Telegram, Slack, and WhatsApp simultaneously.
- **Fail-soft** — Voice is strictly additive. If the platform key is missing, the reply exceeds the character cap, or synthesis/transcoding/upload fails for any reason, the reply is delivered as text. A voice problem never loses a message.

## How It Works

1. The platform admin sets `ELEVENLABS_API_KEY` in `.env` (without it, the toggle is disabled with "Voice is unavailable" shown).
2. Open the agent's **Sharing** tab → **Channels** → open any channel's **Configure/Manage** dialog.
3. Find the **Voice replies** toggle ("Speak this agent's replies as a voice note (ElevenLabs). Long replies fall back to text.").
4. Paste an **ElevenLabs voice ID** and click **Save**, then enable the toggle. Enabling requires a voice ID.
5. The agent's replies on all connected channels now arrive as voice notes. Markdown is stripped before speaking.

### Per-Channel Delivery

| Channel | Delivery format |
|---------|-----------------|
| Telegram | OGG/Opus voice note (`sendVoice`); in groups it replies to the triggering message |
| Slack | Inline MP3 clip uploaded into the thread (Slack renders MP3 with a built-in player) |
| WhatsApp | OGG voice note delivered via Twilio media (hosted transiently by Trinity for ~1 hour) |

WhatsApp voice notes work even when the agent's file-sharing toggle is off — voice replies are gated only by their own toggle.

### When Text Is Used Instead

- The reply is longer than the platform character cap (`TTS_MAX_CHARS`, default 1,500 characters).
- The platform has no `ELEVENLABS_API_KEY`.
- Synthesis, transcoding, hosting, or upload fails for any reason.

## Configuration

| Variable | Description | Default |
|----------|-------------|---------|
| `ELEVENLABS_API_KEY` | Platform-wide ElevenLabs key; gates the whole feature | — (empty = unavailable) |
| `ELEVENLABS_MODEL_ID` | ElevenLabs model used for synthesis | `eleven_multilingual_v2` |
| `TTS_MAX_CHARS` | Character cap; longer replies fall back to text | `1500` |

ffmpeg must be present on the backend (bundled in the standard images) for the OGG voice-note transcode used by Telegram and WhatsApp.

## For Agents

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/agents/{name}/voice-replies` | GET | `{enabled, voice_id, available}` — `available` reflects the platform ElevenLabs key |
| `/api/agents/{name}/voice-replies` | PUT | Owner-only. `{enabled, voice_id}`; enabling requires a `voice_id` |

## Limitations

- Voice replies cover the messaging channels (Telegram, Slack, WhatsApp) — not the web chat UI or public links.
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
