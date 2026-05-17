---
name: announce
description: Send an announcement message to Discord, Slack, Telegram, and/or Twitter/X channels
allowed-tools: [Bash, Read]
user-invocable: true
metadata:
  version: "1.6"
  created: 2026-03-28
  updated: 2026-04-25
  author: trinity
  changelog:
    - "1.6: Add Twitter/X support via Twitter API v2 + OAuth 1.0a (scripts/post_twitter.py)"
    - "1.5: Require sequential (not parallel) sends and no-blind-retry rule to prevent duplicate messages"
    - "1.4: Add Telegram support via Bot API + sendMessage with topic threading"
    - "1.3: Save each announcement to docs/user-docs/dev-announcements/ with timestamped filename"
    - "1.2: Add message style rule — dense, no-filler announcements"
    - "1.1: Add Slack support via Bot OAuth Token + chat.postMessage API"
    - "1.0: Initial version — Discord webhook support"
---

# Announce

## Purpose

Send an arbitrary message to a configured announcement channel. Supports Discord (webhooks), Slack (Bot OAuth Token + chat.postMessage API), Telegram (Bot API + sendMessage with topic threading), and Twitter/X (API v2 + OAuth 1.0a User Context).

## State Dependencies

| Source | Location | Read | Write | Description |
|--------|----------|------|-------|-------------|
| Env file | `.env` | Yes | | Webhook URLs and channel config |
| Announcements | `docs/user-docs/dev-announcements/` | | Yes | Timestamped announcement records |

## Channel Registry

Channels are configured in `.env` using the naming convention:

```bash
# Discord channels
ANNOUNCE_DISCORD_<NAME>_WEBHOOK=https://discord.com/api/webhooks/...

# Slack channels (Bot OAuth Token + channel IDs)
ANNOUNCE_SLACK_TOKEN=xoxb-...
ANNOUNCE_SLACK_<NAME>_CHANNEL=C0123456789

# Telegram channels (Bot API token + chat IDs)
ANNOUNCE_TELEGRAM_TOKEN=<bot-token-from-botfather>
ANNOUNCE_TELEGRAM_<NAME>_CHANNEL=<chat_id>            # channel or group
ANNOUNCE_TELEGRAM_<NAME>_CHANNEL=<chat_id>:<thread_id> # group with topic

# Twitter/X (OAuth 1.0a User Context — single account)
ANNOUNCE_TWITTER_API_KEY=<consumer-key>
ANNOUNCE_TWITTER_API_SECRET=<consumer-secret>
ANNOUNCE_TWITTER_ACCESS_TOKEN=<user-access-token>
ANNOUNCE_TWITTER_ACCESS_TOKEN_SECRET=<user-access-token-secret>

# Default channel (used when no channel is specified)
ANNOUNCE_DEFAULT_CHANNEL=discord:updates
```

### Currently configured channels

| Name | Platform | Purpose |
|------|----------|---------|
| `updates` | Discord | Trinity community updates channel |
| `updates` | Slack | Slack updates channel (C06MCLZ966Q) |
| `updates` | Telegram | Telegram group topic (-1001722567447, thread 7491) |
| `default` | Twitter/X | Authenticated account (OAuth 1.0a — one account per token set) |

## Prerequisites

- Webhook URL configured in `.env` (see Setup section)

## Process

### Step 1: Parse Arguments

The skill is invoked as:

```
/announce [message]
/announce [channel] [message]
```

- If no channel is specified, use the default from `ANNOUNCE_DEFAULT_CHANNEL`
- Channel format: `platform:name` (e.g., `discord:updates`)
- If just a name is given, assume `discord:` prefix

### Step 2: Load Configuration

```bash
source .env
```

Resolve the webhook URL from the channel name:

- Parse the channel argument (e.g., `discord:updates` -> platform=`discord`, name=`updates`)
- For Discord: look up `ANNOUNCE_DISCORD_UPDATES_WEBHOOK` (uppercased name)
- For Slack: look up `ANNOUNCE_SLACK_UPDATES_CHANNEL` (uppercased name) and `ANNOUNCE_SLACK_TOKEN`
- For Telegram: look up `ANNOUNCE_TELEGRAM_UPDATES_CHANNEL` (uppercased name) and `ANNOUNCE_TELEGRAM_TOKEN`. If the channel value contains `:`, split into `chat_id:thread_id` for topic threading.
- For Twitter: a single account is implied by the OAuth 1.0a token set, so any name (typically `default`) maps to the same account. Verify all four `ANNOUNCE_TWITTER_*` env vars are present.
- If not found, stop and show available channels

### Step 3: Send Message

#### Sending rules (critical — read before each run)

1. **Sequential only — never parallel.** When sending to multiple channels in a single invocation, run one curl at a time. Do NOT fire parallel tool calls across platforms. A parallel cancellation can abort sibling tools *after* their curl has already fired, leaving you blind about whether the message landed. Order: send Discord → wait for result → send Slack → wait → send Telegram → wait.

2. **Never blindly retry on ambiguous failure.** If a send's outcome is ambiguous — tool cancellation, network timeout, empty response body, missing `ok` field — STOP. These APIs have no idempotency key, so a retry will duplicate the message. Before resending: check the target channel directly (API read-back via `conversations.history` for Slack, `/getUpdates` or channel inspection for Telegram, webhook channels via the Discord UI), confirm the message is NOT already there, then retry. If in doubt, ask the user.

3. **Scripts must exit 0 on the success path.** Avoid trailing shell idioms like `[ "$HTTP" != "204" ] && cat /tmp/out` — on success the bracket test returns exit 1 and can cause the parallel-tool harness to cancel sibling calls (which may have already fired). Use an explicit if-block instead:

   ```bash
   if [ "$HTTP" = "204" ]; then
     echo "DISCORD: ok"
   else
     echo "DISCORD: failed HTTP $HTTP"
     cat /tmp/out
     exit 1
   fi
   ```

#### Message Style

Keep announcements dense and information-rich. No filler, no preamble, no "we're excited to announce". Lead with what changed, then why it matters. One sentence per fact. If the update fits in one line, use plain `content`. Only use embeds for multi-fact updates.

#### Discord

Post via webhook using curl:

```bash
curl -s -o /dev/null -w "%{http_code}" \
  -H "Content-Type: application/json" \
  -d '{"content": "<message>"}' \
  "$WEBHOOK_URL"
```

- HTTP 204 = success
- Any other status = error, show response body

For multi-line or formatted messages, Discord webhooks support markdown natively.

#### Embedding support

If the message warrants richer formatting, use Discord's embed structure:

```bash
curl -s -H "Content-Type: application/json" \
  -d '{
    "embeds": [{
      "title": "<title>",
      "description": "<body>",
      "color": 5814783
    }]
  }' \
  "$WEBHOOK_URL"
```

Use embeds when the operator provides a title + body, or when the message is long. Use plain `content` for short announcements.

#### Slack

Post via `chat.postMessage` API using Bot OAuth Token:

```bash
RESPONSE=$(curl -s -X POST https://slack.com/api/chat.postMessage \
  -H "Authorization: Bearer $ANNOUNCE_SLACK_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"channel\": \"$CHANNEL_ID\", \"text\": \"<message>\"}")
```

- Check `echo $RESPONSE | python3 -c "import sys,json; print(json.load(sys.stdin)['ok'])"` — `True` = success
- If `False`, extract error: `echo $RESPONSE | python3 -c "import sys,json; print(json.load(sys.stdin).get('error','unknown'))"`
- Common errors: `channel_not_found`, `not_in_channel` (bot must be invited to the channel first)

For rich formatting, Slack supports [mrkdwn](https://api.slack.com/reference/surfaces/formatting): `*bold*`, `_italic_`, `` `code` ``, `>` blockquote.

#### Telegram

Post via Bot API `sendMessage`:

```bash
# Parse chat_id and optional thread_id from channel value
CHANNEL_VALUE="$ANNOUNCE_TELEGRAM_UPDATES_CHANNEL"
if [[ "$CHANNEL_VALUE" == *":"* ]]; then
  CHAT_ID="${CHANNEL_VALUE%%:*}"
  THREAD_ID="${CHANNEL_VALUE##*:}"
  THREAD_PARAM="\"message_thread_id\": $THREAD_ID,"
else
  CHAT_ID="$CHANNEL_VALUE"
  THREAD_PARAM=""
fi

RESPONSE=$(curl -s -X POST "https://api.telegram.org/bot${ANNOUNCE_TELEGRAM_TOKEN}/sendMessage" \
  -H "Content-Type: application/json" \
  -d "{
    \"chat_id\": \"$CHAT_ID\",
    ${THREAD_PARAM}
    \"text\": \"<message>\",
    \"parse_mode\": \"HTML\"
  }")
```

- Check `echo $RESPONSE | python3 -c "import sys,json; r=json.load(sys.stdin); print('ok' if r['ok'] else r['description'])"` — `ok` = success
- Common errors: `Forbidden: bot is not a member of the channel chat` (bot must be admin), `Bad Request: message thread not found` (wrong thread ID)
- HTML formatting: `<b>bold</b>`, `<i>italic</i>`, `<code>code</code>`, `<pre>block</pre>`, `<a href="url">link</a>`
- Max message length: 4096 characters

#### Twitter/X

Twitter API v2 requires OAuth 1.0a HMAC-SHA1 signing, which is too gnarly to do safely in pure bash. Use the bundled helper at `.claude/skills/announce/scripts/post_twitter.py` which reads tweet text from stdin and prints `{"ok": true, "id": "<tweet_id>", "url": "..."}` on success.

```bash
RESPONSE=$(printf '%s' "$MESSAGE" | python3 .claude/skills/announce/scripts/post_twitter.py)
OK=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('ok', False))")
if [ "$OK" = "True" ]; then
  TWEET_URL=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('url',''))")
  echo "TWITTER: ok $TWEET_URL"
else
  echo "TWITTER: failed"
  echo "$RESPONSE"
  exit 1
fi
```

- The script exits 0 on success, 1 on failure — follow the same explicit if-block pattern as Discord (rule 3 of "Sending rules") so a non-zero exit doesn't cancel sibling tool calls in the harness.
- Max length: **280 chars** — strictly enforced by the API. The script rejects longer messages locally before the API call.
- Plain text only. Twitter does not render markdown; use raw URLs and line breaks.
- Common errors: `403 duplicate content` (Twitter blocks identical reposts within ~24h — vary the wording), `401 Unauthorized` (token revoked or wrong app permissions — needs Read+Write), `429` (rate limited).
- Idempotency: Twitter API has **no idempotency key**. Same rule as Slack/Telegram — on ambiguous failure, check `https://x.com/<your-handle>` before retrying.

### Step 4: Confirm

After each send completes, capture its outcome. For multi-channel announcements, accumulate per-channel results and present a single summary at the end (after all sequential sends have finished):

| Channel | Status |
|---|---|
| discord:updates | HTTP 204 ✓ |
| slack:updates | ok=True, ts=... ✓ |
| telegram:updates | ok=True, message_id=... ✓ |
| twitter:default | ok=True, id=..., url=https://x.com/i/status/... ✓ |

If any row is a ✗, do not silently retry — surface the error, and before resending to that channel, follow rule 2 of "Sending rules": verify the message is not already there (it may have landed before the reported failure).

### Step 5: Save Announcement Record

Save every announcement to `docs/user-docs/dev-announcements/` with a timestamped filename:

```bash
mkdir -p docs/user-docs/dev-announcements
TIMESTAMP=$(date +%Y-%m-%d-%H%M%S)
FILENAME="docs/user-docs/dev-announcements/${TIMESTAMP}.md"
```

Write the file with frontmatter and the message content:

```markdown
---
date: <ISO 8601 timestamp, e.g. 2026-03-28T14:30:00>
channel: <platform:name, e.g. discord:updates>
---

<message content as sent>
```

Report the saved path:

```
Saved to docs/user-docs/dev-announcements/2026-03-28-143000.md
```

## Outputs

- Message posted to the target channel
- Confirmation of delivery status
- Announcement record saved to `docs/user-docs/dev-announcements/<timestamp>.md`

---

## Setup

### Discord Webhook Setup

1. Open the Discord channel settings (gear icon)
2. Go to **Integrations** -> **Webhooks**
3. Click **New Webhook**, name it (e.g., "Trinity Announcements")
4. Copy the webhook URL

### Environment Configuration

Add to `.env` (gitignored — never committed):

```bash
# Announce skill - Discord webhooks
ANNOUNCE_DISCORD_UPDATES_WEBHOOK=https://discord.com/api/webhooks/...

# Default channel for /announce without a channel argument
ANNOUNCE_DEFAULT_CHANNEL=discord:updates
```

### Adding More Channels

To add another Discord channel:

```bash
ANNOUNCE_DISCORD_GENERAL_WEBHOOK=https://discord.com/api/webhooks/...
```

Then use: `/announce discord:general Your message here`

### Slack Setup

1. Go to your Slack app's **OAuth & Permissions** page
2. Ensure the bot has `chat:write` scope (and `chat:write.public` if posting to channels the bot isn't in)
3. Copy the **Bot User OAuth Token** (`xoxb-...`)
4. Invite the bot to the target channel: `/invite @YourBotName`
5. Get the channel ID (right-click channel → View channel details → ID at bottom)

Add to `.env`:

```bash
# Announce skill - Slack
ANNOUNCE_SLACK_TOKEN=xoxb-your-bot-token
ANNOUNCE_SLACK_UPDATES_CHANNEL=C06MCLZ966Q

# To add more Slack channels:
# ANNOUNCE_SLACK_GENERAL_CHANNEL=C0123456789
```

Usage: `/announce slack:updates Your message here`

### Telegram Setup

1. Message `@BotFather` on Telegram → `/newbot` → pick a name and username (must end in `bot`)
2. Copy the API token BotFather gives you
3. Add the bot as an **administrator** of your channel/group (must have "Post Messages" permission)
4. For groups with Topics: get the topic thread ID from the topic link (e.g., `https://t.me/c/1722567447/7491` → thread ID is `7491`, chat ID is `-1001722567447`)

Add to `.env`:

```bash
# Announce skill - Telegram
ANNOUNCE_TELEGRAM_TOKEN=<bot-token-from-botfather>
ANNOUNCE_TELEGRAM_UPDATES_CHANNEL=-100XXXXXXXXXX:7491  # chat_id:thread_id for topics
# Without topic: ANNOUNCE_TELEGRAM_GENERAL_CHANNEL=-100XXXXXXXXXX
```

Usage: `/announce telegram:updates Your message here`

### Twitter/X Setup

1. Create (or reuse) a developer app at https://developer.twitter.com — apply for **Read and Write** permissions in the app's User authentication settings (without it, posting returns 403).
2. Generate the four OAuth 1.0a credentials in the app's "Keys and tokens" tab:
   - **Consumer Keys**: API Key + API Secret
   - **Access Token and Secret**: must be regenerated *after* enabling Read+Write — old tokens stay read-only
3. Single account per token set. To post from another account, replace the four token values.

Add to `.env`:

```bash
# Announce skill - Twitter/X (OAuth 1.0a User Context)
ANNOUNCE_TWITTER_API_KEY=<consumer-key>
ANNOUNCE_TWITTER_API_SECRET=<consumer-secret>
ANNOUNCE_TWITTER_ACCESS_TOKEN=<user-access-token>
ANNOUNCE_TWITTER_ACCESS_TOKEN_SECRET=<user-access-token-secret>
```

> The same credentials are referenced in `~/Dropbox/Agents/ruby-internal/.mcp.json` under the `twitter-mcp` server (`@enescinar/twitter-mcp`). Copy them as-is — this skill calls the same Twitter API v2 endpoint.

Python dependency (one-time):

```bash
python3 -m pip install --user requests-oauthlib
```

Usage: `/announce twitter Your tweet here` or `/announce twitter:default Your tweet here` (the `default` channel name is implicit).
