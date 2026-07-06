# Telegram Integration

Connect agents to Telegram bots. Supports direct messages, group chats, @mentions, and reply threading.

> 📺 **Watch:** [Build an AI Recruiter Agent — Telegram bot setup](https://youtu.be/K7hFWyFIf-Y) *(Jun 2026)* · [all videos](../videos.md)

## Concepts

- **1:1 Mapping** -- Each agent has its own dedicated Telegram bot. Bots cannot be shared across agents.
- **Webhook** -- Trinity receives Telegram updates via a webhook URL. Requires `public_chat_url` to be configured in Settings.
- **Trigger Mode** -- In groups, controls whether the bot responds to all messages or only @mentions and direct replies.
- **Privacy Mode** -- A Telegram-level setting that determines which messages the bot receives in groups. Must be disabled for "all messages" mode to work.

## Bot Setup (BotFather)

Before connecting to Trinity, create and configure your bot via [@BotFather](https://t.me/BotFather) on Telegram.

### Step 1: Create the Bot

1. Open Telegram and message [@BotFather](https://t.me/BotFather)
2. Send `/newbot`
3. Enter a display name (e.g., "My Agent Bot")
4. Enter a username ending in `bot` (e.g., `my_agent_bot`)
5. Copy the **bot token** (format: `123456789:ABC-DEF...`) -- you'll need this for Trinity

### Step 2: Configure Privacy Mode (Required for Groups)

By default, Telegram bots have **Privacy Mode enabled**. This means:
- Direct messages work normally
- In groups, the bot only receives:
  - Messages that @mention the bot
  - Replies to the bot's own messages
  - Bot commands (messages starting with `/`)

**To enable "all messages" mode in groups, you must disable Privacy Mode:**

1. Message [@BotFather](https://t.me/BotFather)
2. Send `/setprivacy`
3. Select your bot from the list
4. Choose **Disable**
5. You'll see: "Privacy mode is disabled for YourBot"

**Critical step after changing privacy mode:**
- If the bot is already in a group, you must **remove and re-add the bot** for the change to take effect. Telegram only applies privacy mode changes to newly joined groups.

### Step 3: Optional Bot Settings

| Command | Purpose |
|---------|---------|
| `/setdescription` | What users see when they open your bot |
| `/setabouttext` | Shown in the bot's profile |
| `/setuserpic` | Bot avatar (or use Trinity's avatar generator) |

## How It Works

### Connect Bot to Agent (Agent Detail -> Sharing Tab)

1. Open the agent detail page
2. Select the **Sharing** tab and find the **Telegram** row under **Channels**
3. Click **Configure** — the Telegram configuration opens in a dialog
4. Paste your bot token and click **Connect Bot**
5. Trinity validates the token and registers the webhook

After connecting:
- The bot link (`t.me/YourBot`) appears in the UI
- Users can start chatting immediately via direct messages
- Group support is available once the bot is added to a group

### Webhook Status

| Status | Meaning |
|--------|---------|
| Green "Connected" | Bot configured, webhook active |
| Yellow "Connected (no webhook)" | Bot configured, but `public_chat_url` not set in Settings -- messages won't be received |

If you see the yellow warning, go to **Settings** and configure `public_chat_url` with your Trinity instance's public URL. The webhook registers automatically once saved.

### Message Flow

```
User sends message -> Telegram API -> Trinity webhook -> Agent -> Response -> Telegram
```

The agent receives the message text plus context for any media (photos, documents, voice notes). Responses are sent back as replies in the same chat.

### Voice Messages

Voice notes sent to the bot are automatically transcribed with Google Gemini 2.0 Flash and delivered to the agent as text prefixed with the 🎙️ emoji. Transcription is transparent — users just send voice notes normally.

| Constraint | Limit |
|------------|-------|
| Duration | 5 minutes |
| File size | 10 MB |
| Config required | `GEMINI_API_KEY` set on the backend |

If transcription fails or `GEMINI_API_KEY` is not configured, the agent receives a placeholder such as `[Voice message received — transcription failed]` so the conversation still progresses.

### Voice Replies (Outbound)

The agent can also *speak its replies* as Telegram voice notes (OGG/Opus via `sendVoice`; in groups the note replies to the triggering message). This is a per-agent setting shared across all messaging channels — enable it with the **Voice replies** toggle inside the Telegram dialog. See [Voice Replies](../advanced/voice-replies.md) for setup and fallback behavior.

### Bot Commands

Users in Telegram can use these commands:

| Command | Action |
|---------|--------|
| `/start` | Welcome message |
| `/help` | Shows capabilities |
| `/reset` | Clears conversation history |
| `/login <email>` | Verify email for access-controlled agents |
| `/logout` | Clear verified email |
| `/whoami` | Show current verified email |

## Group Chat Configuration

When a bot joins a Telegram group, Trinity automatically creates a group config.

### View and Manage Groups

1. Go to Agent Detail -> **Sharing** tab
2. Connected groups appear in the **Telegram Groups** section
3. Each group shows:
   - Group name and type (group/supergroup)
   - Current trigger mode
   - Welcome message settings

### Trigger Modes

| Mode | Bot Responds To | Privacy Mode Required |
|------|----------------|----------------------|
| **Mention only** (default) | @mentions and replies to bot | Either (works with Privacy enabled) |
| **All messages** | Every message in the group | Privacy must be **Disabled** |

**If "all messages" mode doesn't work:**
1. Verify Privacy Mode is disabled in BotFather (`/setprivacy` -> Disable)
2. Remove the bot from the group
3. Re-add the bot to the group
4. Wait a few seconds and send a test message

### Group Authentication

By default, anyone in a group can chat with the bot. To require at least one verified member before the bot responds:

1. Go to Agent Detail -> **Sharing** tab -> **Channel Access Policy**
2. Set **Group auth mode** to "Any verified member"
3. When someone @mentions the bot in an unverified group, the bot will prompt for verification
4. Once any group member completes `/login`, the group is unlocked for everyone

This is useful for agents that handle sensitive information or have usage costs. The verifying user's email is recorded, and all subsequent messages from any group member are allowed.

### Welcome Messages

Enable welcome messages to greet users who join the group:

1. Toggle **Welcome message** on
2. Enter the welcome text (up to 4096 characters)
3. Use `{name}` to include the user's first name

Note: Welcome messages require the bot to have **admin rights** in the group.

### Remove a Group

Click the remove button next to a group to deactivate it. The bot will stop responding in that group but remain a member. To fully remove the bot, use Telegram's group settings.

## For Agents

### API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/agents/{name}/telegram` | GET | Bot binding status |
| `/api/agents/{name}/telegram` | PUT | Configure bot token |
| `/api/agents/{name}/telegram` | DELETE | Remove bot binding |
| `/api/agents/{name}/telegram/test` | POST | Verify bot or send test message |
| `/api/agents/{name}/telegram/groups` | GET | List group configs |
| `/api/agents/{name}/telegram/groups/{id}` | PUT | Update trigger mode / welcome |
| `/api/agents/{name}/telegram/groups/{id}` | DELETE | Deactivate group config |

See [Backend API Docs](http://localhost:8000/docs) for full request/response schemas.

### Configure Bot Programmatically

```bash
# Connect bot to agent
curl -X PUT http://localhost:8000/api/agents/my-agent/telegram \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"bot_token": "123456789:ABC-DEF..."}'

# Check status
curl http://localhost:8000/api/agents/my-agent/telegram \
  -H "Authorization: Bearer $TOKEN"

# Set group to respond to all messages
curl -X PUT http://localhost:8000/api/agents/my-agent/telegram/groups/1 \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"trigger_mode": "all"}'
```

## Limitations

- **One bot per agent** -- Each agent can only have one Telegram bot. Each bot can only be bound to one agent.
- **Privacy mode is manual** -- Disabling Privacy Mode must be done through BotFather; it cannot be automated via the Bot API.
- **Group re-add required** -- After changing Privacy Mode, the bot must be removed and re-added to existing groups.
- **Welcome messages require admin** -- The bot needs admin rights in the group to see member join events.
- **20MB file limit** -- Media files larger than 20MB cannot be processed.
- **Voice transcription limits** -- Voice notes over 5 minutes or 10MB are rejected; transcription requires `GEMINI_API_KEY` on the backend.
- **No edited message handling** -- Edited messages are not re-processed.

## Troubleshooting

### Bot not responding in groups

1. **Check Privacy Mode**: Message @BotFather, send `/setprivacy`, verify it shows "Disabled"
2. **Re-add the bot**: Remove the bot from the group, then add it again
3. **Check trigger mode**: In Trinity, verify the group is set to the expected trigger mode
4. **Check agent status**: Ensure the agent container is running

### "Connected (no webhook)" warning

Configure `public_chat_url` in Settings with your Trinity instance's publicly accessible URL. The webhook registers automatically.

### Messages delayed or not arriving

- Check backend logs for webhook errors
- Verify your `public_chat_url` is accessible from the internet
- Telegram may retry failed webhooks; check for 429 (rate limit) errors

## See Also

- [Slack Integration](slack-integration.md)
- [Agent Sharing](../sharing-and-access/agent-sharing.md)
- [Public Links](../sharing-and-access/public-links.md)
