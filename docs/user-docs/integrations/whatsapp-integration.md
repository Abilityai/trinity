# WhatsApp Integration

Connect agents to WhatsApp via Twilio. Users can send direct messages to the agent from any WhatsApp account.

## Concepts

- **1:1 Binding** — Each agent has its own Twilio account and WhatsApp sender. Twilio accounts cannot be shared across agents.
- **Sandbox** — Twilio's shared test sender (`whatsapp:+14155238886`). Free to use; users must opt in with a keyword before messaging.
- **Production Sender** — A dedicated phone number registered with Meta via Twilio. Requires Meta Business Manager approval (24–48 hours).
- **Webhook** — Trinity receives inbound messages via a Twilio webhook URL. Requires `public_chat_url` to be configured in Settings and a Cloudflare Tunnel ingress rule.
- **Email Verification** — Users can verify their identity via `/login` to access agents with restricted access policies.

## Prerequisites

Before connecting a Twilio account, two platform-level prerequisites must be in place:

### 1. Public URL

Go to **Settings → Public Chat URL** and enter your Trinity instance's public domain (e.g., `https://your-domain.com`). Trinity uses this to generate the webhook URL shown in the UI.

### 2. Cloudflare Tunnel Ingress Rule

Add the following path rule in your Cloudflare Tunnel dashboard so Twilio webhooks reach the backend:

| Route | Backend |
|-------|---------|
| `/api/whatsapp/webhook/*` | `http://backend:8000` |

The webhook is a backend FastAPI route (Twilio-signature verified). Routing it to the frontend service silently drops inbound messages — point it at the backend.

Without this rule, Twilio webhooks return 404 at the Cloudflare edge and never reach Trinity. The UI shows a yellow notice as a reminder.

**Verify the route is working:**
```bash
curl -s -o /dev/null -w "%{http_code}\n" \
  -X POST https://your-domain.com/api/whatsapp/webhook/test
# Should return 200, not 404
```

## Getting Twilio Credentials

You need three values from Twilio: **Account SID**, **Auth Token**, and a **WhatsApp sender number**.

### Sandbox (Development)

1. Go to [Twilio Console](https://console.twilio.com) → **Messaging → Try WhatsApp**
2. Copy your **Account SID** and **Auth Token** from the top of the console
3. The sandbox sender is `whatsapp:+14155238886` (shared across all Twilio sandbox users)
4. Note the **sandbox join keyword** shown on the Try WhatsApp page — users must send `join <keyword>` to the sandbox number before they can message your agent

### Production

1. Twilio Console → **Messaging → Senders → WhatsApp Senders**
2. Register a sender — this requires linking a Meta Business Manager account
3. Display-name approval takes 24–48 hours
4. Once approved, copy the dedicated sender number in E.164 format (e.g., `+14155001234`)

## How It Works

### Connect WhatsApp to an Agent

1. Open the agent detail page
2. Select the **Sharing** tab and find the **WhatsApp** row under **Channels**
3. Click **Configure** — the WhatsApp (Twilio) configuration opens in a dialog
4. Enter:
   - **Twilio Account SID** — starts with `AC`, 34 characters long
   - **Auth Token** — from the Twilio Console (stored encrypted)
   - **WhatsApp From Number** — in format `whatsapp:+<E.164>`, e.g. `whatsapp:+14155238886`
5. Click **Connect**

Trinity validates the credentials against Twilio and generates a webhook URL. If the validation fails, the error from Twilio is shown inline.

### Wire the Webhook in Twilio

After connecting, a webhook URL appears in the UI. Copy it and paste it into Twilio:

**Sandbox:**
- Twilio Console → Messaging → Try WhatsApp → **Sandbox settings**
- Set **"When a message comes in"** to HTTP POST + your webhook URL

**Production:**
- Twilio Console → Messaging → Senders → your registered sender
- Set the webhook URL on the sender configuration

### Sandbox Opt-In

For the sandbox, users must opt in before they can chat. Share these instructions with testers:

1. Open WhatsApp on your phone
2. Send a message to `+1 415 523 8886`
3. Type: `join <your-sandbox-keyword>` (shown in Twilio Console → Try WhatsApp)
4. Once Twilio confirms opt-in, the user can message the agent normally

### Verify Credentials

On the connected state, click **Verify** to confirm the stored credentials are still valid (makes a live call to Twilio without sending a message).

### Disconnect

Click **Disconnect** to remove the binding. The Twilio sender is not affected — only the Trinity configuration is removed.

### Outbound Media & Files

Agents can deliver files and images in their WhatsApp replies, not just text. Each file is hosted transiently by Trinity (~1 hour) and sent as a Twilio media attachment — one message per file, since WhatsApp allows a single media item per message. The text reply always goes out first, so it survives even if a media send fails.

| File type | Delivered as | Size cap |
|-----------|--------------|----------|
| Images, audio, video | Native media | 5 MB |
| Text and structured documents (txt, JSON, XML, PDF, YAML, SQL, …) | Document | 16 MB |
| Unknown binary types | Not deliverable as media | — |

Rules:

- Outbound media requires the agent's **file-sharing** toggle to be on (Sharing tab → Distribution → File sharing) and a configured **Public Chat URL** (media links must be HTTPS).
- A file that is oversized, of an unsupported type, or otherwise undeliverable degrades to a `📎 name: url` download link appended to the text reply — it never blocks the reply or other files.

### Voice Replies (Outbound)

The agent can speak its replies as WhatsApp voice notes (OGG, delivered via Twilio media). Enable the shared **Voice replies** toggle inside the WhatsApp dialog — see [Voice Replies](../advanced/voice-replies.md). Voice notes work even when the file-sharing toggle is off; they are gated only by their own setting.

## User Commands

WhatsApp users can send these commands to the agent number:

| Command | Action |
|---------|--------|
| `/login user@example.com` | Sends a 6-digit verification code to the email address |
| `/login 123456` | Submits the code and verifies the email |
| `/logout` | Unlinks the verified email from this WhatsApp number |
| `/whoami` | Shows the currently verified email address |

After successful `/login`, if the agent has a restricted access policy (`require_email` enabled), Trinity checks whether the verified email is on the shared-access list. If not, an access request is automatically created and the user is notified that approval is pending.

## Access Control

WhatsApp respects the same cross-channel access policy as Telegram and Slack. Configure it in **Agent Detail → Sharing → Channel Access Policy**:

| Policy | Effect |
|--------|--------|
| **Open access** | Any WhatsApp user can chat with the agent without verification |
| **Require email** | Users must complete `/login` before chatting; unverified users are prompted |
| **Require sharing** | Users must be explicitly shared on the agent (owner/admin must approve) |

Approving an access request (from **Sharing → Access Requests**) automatically adds the email to the shared-access list.

## For Agents

### API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/agents/{name}/whatsapp` | GET | Binding status and webhook URL |
| `/api/agents/{name}/whatsapp` | PUT | Configure Twilio credentials |
| `/api/agents/{name}/whatsapp` | DELETE | Remove binding |
| `/api/agents/{name}/whatsapp/test` | POST | Verify credentials or send a test message |

See [Backend API Docs](http://localhost:8000/docs) for full request/response schemas.

### Configure Programmatically

```bash
# Connect a Twilio account to an agent
curl -X PUT http://localhost:8000/api/agents/my-agent/whatsapp \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "account_sid": "ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
    "auth_token": "your-auth-token",
    "from_number": "whatsapp:+14155238886"
  }'

# Check binding status
curl http://localhost:8000/api/agents/my-agent/whatsapp \
  -H "Authorization: Bearer $TOKEN"

# Verify credentials (no test message sent)
curl -X POST http://localhost:8000/api/agents/my-agent/whatsapp/test \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{}'

# Send a test message to a specific number
curl -X POST http://localhost:8000/api/agents/my-agent/whatsapp/test \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"to_number": "whatsapp:+15551234567", "message": "Test from Trinity"}'
```

## Limitations

- **Direct messages only** — Twilio's WhatsApp API does not support group chats. WhatsApp groups are not available.
- **24-hour message window** — Twilio can only send freeform messages to users who have messaged within the last 24 hours. Outside this window, Twilio returns error `63016`. Approved message templates (Phase 3, not yet available) will lift this restriction for outbound-first use cases.
- **One Twilio account per agent** — Each agent can only have one WhatsApp binding. Multiple agents cannot share the same Twilio account.
- **Sandbox requires opt-in** — Every tester must send the join keyword before messaging. The sandbox sender is shared across all Twilio users, so keywords help route to your account.
- **No SMS** — The binding is WhatsApp-only. SMS on the same Twilio number is not available from Trinity in this release.
- **Proactive messaging** — WhatsApp is not included in the automatic channel fallback (`auto`). Agents sending proactive messages must specify `channel="whatsapp"` explicitly and the recipient must have a verified email linked to a prior WhatsApp conversation.

## Troubleshooting

### "public_chat_url is not set" warning

Go to **Settings → Public Chat URL** and enter your Trinity instance's public domain. The webhook URL generates automatically once saved.

### Twilio webhook returns 404

The Cloudflare Tunnel ingress rule for `/api/whatsapp/webhook/*` is missing or misconfigured. Add it in the Cloudflare dashboard pointing to the **backend** service (`http://backend:8000`).

### HMAC signature validation fails

This means the URL Twilio signed does not match the URL Trinity sees. Check that:
- nginx has `X-Forwarded-Proto $scheme` configured
- uvicorn runs with `--proxy-headers`
- The `public_chat_url` in Settings exactly matches the public URL Twilio posts to

### Sandbox messages not arriving

1. Confirm the user sent the sandbox join keyword from their phone
2. Verify the webhook URL in Twilio Console → Try WhatsApp → Sandbox settings is set to HTTP POST
3. Check backend logs for signature or binding errors

### Credentials rejected

Twilio returns 401 if the AccountSid/AuthToken combination is invalid. Re-copy the Auth Token from the Twilio Console — it resets when you click "regenerate" in Twilio.

## See Also

- [Telegram Integration](telegram-integration.md)
- [Slack Integration](slack-integration.md)
- [Voice Replies](../advanced/voice-replies.md)
- [Access Control](../sharing-and-access/access-control.md)
- [Agent Sharing & Access](../sharing-and-access/agent-sharing.md)
