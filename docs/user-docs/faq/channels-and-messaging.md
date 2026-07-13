# Trinity FAQ — Channels & Messaging

> Part of the [Trinity FAQ](README.md). Short, grounded answers with links to the full documentation.

## How do I connect my Trinity instance to Slack?

Connection happens in two steps. First, a platform admin goes to **Settings → Slack**, enters a Slack App Token (`xapp-...`), clicks **Connect** to start the Socket Mode transport, then clicks **Install to Workspace** to complete OAuth and obtain a bot token. Second, each agent is bound to Slack from its own detail page: open the **Sharing** tab, click **Configure** on the Slack row, and click **Create Channel** to create a dedicated channel bound to that agent. Note that only one Slack workspace can be connected per Trinity instance. See [Slack Integration](../integrations/slack-integration.md).

## What is the difference between Socket Mode and webhook mode for Slack?

Socket Mode is the default transport: Trinity opens an outbound WebSocket connection to Slack, so your instance needs no public URL — it works even on a laptop behind NAT. Webhook mode is the HTTP fallback for production environments where Slack posts events to your server, which requires a publicly accessible URL. If you can use Socket Mode, it is the simpler option. See [Slack Integration](../integrations/slack-integration.md).

## Can multiple agents share one Slack workspace?

Yes. Each agent gets its own dedicated Slack channel via **Create Channel** in the agent's Sharing tab, and @mentions in that channel route to the bound agent. Direct messages to the bot go to a single workspace-wide DM-default agent instead. Replies post with each agent's own name and avatar, so multiple agents in one workspace stay visually distinct. See [Slack Integration](../integrations/slack-integration.md).

## Which agent answers when someone DMs the Slack bot?

DMs are routed to the workspace's DM-default agent. The first agent bound to the workspace becomes the DM default automatically, but you can reassign it anytime: open the target agent's **Sharing** tab → Slack section and click **Set as DM Default** — the change takes effect immediately, no restart needed. Only one agent per workspace can be the DM default, and you cannot unbind the current DM-default agent while other agents are still bound; reassign the default first. See [Slack Integration](../integrations/slack-integration.md).

## Why does my agent keep replying in a Slack thread without being @mentioned?

That is thread tracking working as designed. Once an agent has been @mentioned in a channel (or a thread starts from one of the bot's own messages), replies in that thread route to the same agent automatically, so people can hold a conversation without re-mentioning the bot on every message. Thread tracking only applies to threads started by a bot message or an @mention — the bot does not join arbitrary threads. See [Slack Integration](../integrations/slack-integration.md).

## How does my agent show its own name and avatar in Slack?

Trinity posts each reply with the agent's name and its Trinity avatar as the per-message bot icon, using Slack's `chat:write.customize` scope. This applies to both replies and proactive messages, so in a multi-agent workspace you can always tell which agent is speaking. The agent also sees who is talking and where — channel messages arrive with a prefix like `[Channel: #engineering]` / `[From: John Smith (@johndoe)]`, while DMs stay clean. See [Slack Integration](../integrations/slack-integration.md).

## How do I connect an agent to Telegram?

Create a bot first: message @BotFather on Telegram, send `/newbot`, choose a name and a username ending in `bot`, and copy the bot token. Then open the agent's **Sharing** tab, click **Configure** on the Telegram row, paste the token, and click **Connect Bot** — Trinity validates the token and registers the webhook. Each agent needs its own dedicated bot; bots cannot be shared across agents. If the status shows "Connected (no webhook)", set your instance's public URL under **Settings → Public Chat URL** so Telegram can deliver messages. See [Telegram Integration](../integrations/telegram-integration.md).

## Why doesn't my Telegram bot see every message in a group?

Two settings control this. In Trinity, each group has a trigger mode: **Mention only** (the default — the bot responds to @mentions and replies) or **All messages**. For "all messages" to work, you must also disable Telegram's Privacy Mode via @BotFather (`/setprivacy` → Disable), because with Privacy Mode on, Telegram simply never delivers non-mention group messages to the bot. Critically, if the bot is already in the group, you must remove and re-add it after changing Privacy Mode — Telegram only applies the change to newly joined groups. See [Telegram Integration](../integrations/telegram-integration.md).

## Can users send voice messages to my Telegram bot?

Yes. Voice notes are automatically transcribed with Google Gemini and delivered to the agent as text prefixed with a 🎙️ emoji — users just send voice notes normally. Limits: 5 minutes duration, 10 MB file size, and the backend must have `GEMINI_API_KEY` configured. If transcription fails, the agent receives a placeholder message instead, so the conversation still progresses. See [Telegram Integration](../integrations/telegram-integration.md).

## How do I connect an agent to WhatsApp?

Trinity connects to WhatsApp through Twilio — you bring your own Twilio account. Open the agent's **Sharing** tab, click **Configure** on the WhatsApp row, and enter your Twilio Account SID, Auth Token (stored encrypted), and WhatsApp sender number in the form `whatsapp:+15551234567`. After connecting, copy the generated webhook URL into your Twilio console so inbound messages reach Trinity. You also need **Settings → Public Chat URL** set to your public domain, and if you route traffic through a tunnel, a rule sending `/api/whatsapp/webhook/*` to the backend service. See [WhatsApp Integration](../integrations/whatsapp-integration.md).

## Should I use the Twilio sandbox or a production WhatsApp sender?

Use the sandbox for development: it is free and uses Twilio's shared sender `whatsapp:+14155238886`, but every tester must first opt in by sending `join <your-sandbox-keyword>` to that number. For production, register a dedicated sender in the Twilio console — this requires linking a Meta Business Manager account, and display-name approval takes 24–48 hours. Trinity auto-detects the sandbox from the well-known sender number. See [WhatsApp Integration](../integrations/whatsapp-integration.md).

## Can my agent join WhatsApp group chats?

No. Twilio's WhatsApp API supports direct messages only, so WhatsApp groups are not available — this is a Twilio/WhatsApp platform limitation, not a Trinity setting. If you need group conversations, Telegram and Slack both support them. See [WhatsApp Integration](../integrations/whatsapp-integration.md).

## How do users verify their email from Telegram or WhatsApp?

Users send `/login user@example.com` to the bot, receive a 6-digit code at that address, and reply `/login 123456` to complete verification. `/whoami` shows the currently verified email and `/logout` clears it. On Slack no command is needed — the workspace OAuth already provides each user's email. Verification matters for agents with a restricted access policy: unverified users are prompted to log in before the agent responds. See [Access Control](../sharing-and-access/access-control.md).

## How do I let people chat with my agent without a Trinity account?

Create a public link: open the agent's **Sharing** tab, go to **Distribution → Public links**, and click **Create Public Link**. You can configure email verification on or off, rate limits, and a custom welcome message, then share the generated URL. Recipients open it and chat immediately — no login required. See [Public Links](../sharing-and-access/public-links.md).

## Does a public chat link remember returning users?

Yes, if email verification is enabled on the link. Verified sessions persist across page refreshes and return visits, and the agent maintains per-user memory scoped to that agent and email, updated by background summarization every 5 messages. Logged-in Trinity users additionally get a history dropdown to resume any past session. Anonymous sessions (verification off) have no cross-session continuity. See [Public Links](../sharing-and-access/public-links.md).

## Can I use a different model for public and channel conversations?

Yes. The **Public chat model** setting on the agent's Sharing tab overrides the model for public-facing surfaces only: public links, Slack/Telegram/WhatsApp channels, and paid chat. Your own authenticated chats and scheduled runs are unaffected, and the default option inherits the platform-wide model. This is useful for putting a cheaper or faster model in front of outside audiences. See [Agent Sharing & Access](../sharing-and-access/agent-sharing.md#public-chat-model).

## Can I give my agent extra instructions that only apply to strangers?

Yes. The **Additional instructions — public & channel chats only** field on the Sharing tab (up to 4,000 characters) is injected into the agent's system prompt for outside audiences only: public links, Slack/Telegram/WhatsApp, and paid chat. It never affects your own authenticated chats, scheduled runs, loops, or agent-to-agent calls, so you can set tone, disclosure rules, or scope limits for strangers without touching the agent's core instructions. Leave it empty to disable. See [Agent Sharing & Access](../sharing-and-access/agent-sharing.md).

## Can my agent speak its replies as voice notes?

Yes. With voice replies enabled, the agent's channel replies are synthesized with ElevenLabs and delivered in each channel's native voice format: an OGG/Opus voice note on Telegram, an inline MP3 clip in the Slack thread, and an OGG voice note via Twilio media on WhatsApp. It is one setting per agent that applies to all three channels: the platform admin sets `ELEVENLABS_API_KEY`, then you enable the **Voice replies** toggle inside any channel's dialog and paste an ElevenLabs voice ID. Voice replies do not cover the web chat UI or public links. See [Voice Replies](../advanced/voice-replies.md).

## Why did my agent's voice reply arrive as plain text?

Voice is strictly additive and fails soft — a voice problem never loses a message. Text is used instead when the reply exceeds the character cap (`TTS_MAX_CHARS`, default 1,500 characters), when the platform has no `ELEVENLABS_API_KEY`, or when synthesis, transcoding, hosting, or upload fails for any reason. If it happens consistently, check that the key is set and that your replies fit under the cap. See [Voice Replies](../advanced/voice-replies.md).

## Can my agent place real phone calls?

Yes — agents can dial a number through Twilio and hold a live, interruptible spoken conversation powered by Gemini Live. VoIP is off by default: the platform needs `VOIP_ENABLED=true` plus a `GEMINI_API_KEY`, a configured Public Chat URL, and a publicly reachable deployment (Twilio must open a WebSocket to your instance). The agent owner then configures a per-agent Twilio voice binding in the Sharing tab's **Voice calls** row, and calls are placed via the `call_user` MCP tool or the API. This release is outbound only — agents do not answer incoming calls. See [VoIP Telephony](../advanced/voip-telephony.md).

## What limits apply to agent phone calls, and what happens after a call?

Calls are rate-limited to 5 per owner and destination number per 60 seconds, capped by a per-agent daily call limit (default 50, overridable on the binding), and hard-capped at 10 minutes each. Calls bill to the agent owner's own Twilio account. After the call ends, the full transcript is saved to the agent's chat history with source `voice` and, by default, dispatched back to the agent as a task so it can follow up on what was discussed; unanswered calls skip this. See [VoIP Telephony](../advanced/voip-telephony.md).

## How does Trinity know who is messaging my agent from a channel?

The verified email is the identity across every channel: a user verified on Telegram, WhatsApp, or a public link, or identified via Slack workspace OAuth, is the same person to Trinity everywhere. That email is checked against the agent's access policy — open access lets anyone chat, while restricted agents admit only the owner, admins, and emails on the shared-access list, with everyone else generating a pending access request. Approving a user once admits them on all channels. The Sharing tab also shows a client roster of external channel users who have messaged the agent. For the approval flow and policy details, see [Access Control](../sharing-and-access/access-control.md).
