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

Set the ElevenLabs key under **Settings → General → Voice (ElevenLabs)** (stored setting, with `ELEVENLABS_API_KEY` in `.env` as the fallback for older deployments). Optionally set a **platform default voice** there too. Until a key resolves, the whole feature is unavailable — the `tts_available` flag is false and the agent config UI is disabled.

### 2. Owner — enable and configure the agent

1. Open the agent's **Settings** tab → **Voice** section.
2. Turn **voice replies on** and pick a **voice** — or rely on the platform default voice. Enabling with neither an agent voice nor a platform default is rejected.
3. Set the **per-channel allow flags** in each channel's own panel on the **Sharing** tab (Telegram, Slack, WhatsApp). Each channel is independent; leave a channel off to keep it text-only.

### 3. Agent — opt a reply into voice

During a channel turn, the agent calls `send_voice_reply` to speak that specific reply. The backend resolves the channel destination from the current execution, checks the agent-level enable plus the per-channel flag, synthesizes the audio, and delivers it. If any check fails, it reports "not delivered" and the agent sends text instead.

### 4. Agent — speak to someone proactively (scheduled briefs)

A scheduled run, loop, or reminder has no conversation to reply into, so a plain `send_voice_reply` call there is refused with guidance. To send a voice note from such a run, the agent also passes who to speak to — a role with `to` (`primary`, `approver` or `viewer`; the platform picks the person) or an email with `recipient_email` — and a `channel` (`telegram` or `whatsapp`). This works like `send_message`:

- The person must be **linked** on that channel (verified email) and must have **opted in to proactive messages** from the agent (or be its owner).
- The **per-person hourly limit** for proactive messages applies, and voice and text share it.
- Every send is recorded in the audit log.
- The agent-level voice setting and the channel's voice flag still apply.

Keep proactive voice notes short. Text longer than the cap (`TTS_MAX_CHARS`, 1500 characters by default) is refused with `text_too_long`, so a daily brief should be a short spoken summary followed by the detail in text.

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
| `/api/agents/{name}/voice-replies` | PUT | Owner-only partial update of `{enabled?, voice_id?, channels?}`; 400 unless `enabled` or `channels` is present, or when enabling with no agent voice and no platform default |
| `/api/agents/{name}/voice-reply` | POST | Per-message delivery backing the tool; fail-soft `{delivered, channel, reason}`; 409 on an in-flight duplicate for the same turn |
| `/api/settings/elevenlabs` | GET / PUT | Admin key + default voice: `{key_configured, key_source, default_voice_id}`, plus the key's speech-to-text status for Workspace dictation (`stt_capability`, `stt_detail`, `stt_checked_at`, `stt_last_failure`); key stored encrypted, never echoed |

MCP tool:

| Tool | Description |
|------|-------------|
| `send_voice_reply` | Speak the current channel reply as a voice note. Backend resolves the destination from the execution, gates on the agent + per-channel flags, and returns fail-soft so the agent can fall back to text. Pass `to` (a role) or `recipient_email`, plus `channel` (telegram or whatsapp), to speak to a person from a scheduled or other non-chat run; consent and rate limits apply as for `send_message` |

See [Backend API Docs](http://localhost:8000/docs) for full request/response schemas.

## Limitations

- Voice replies cover the messaging channels (Telegram, Slack, WhatsApp) — not the Agent Detail chat or public links. The [Workspace](../sharing-and-access/workspace.md) has its own speaker toggle (**Speak replies aloud**) that reads replies with the same agent voice; it is a viewer-side choice, not something the agent opts into.
- Voice is per-reply — the agent decides each time; there is no way to force every reply to be spoken.
- Proactive (addressed) voice notes reach Telegram and WhatsApp only; Slack is not supported yet. WhatsApp's 24-hour session window applies as it does to proactive text.
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
