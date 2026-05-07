# Public Access

By default, Trinity is only accessible on the local network or via VPN. Enabling public access exposes specific paths through a Cloudflare Tunnel so that external webhooks and public chat links work without opening firewall ports or requiring a static IP.

## What Public Access Enables

| Feature | Requires public access |
|---|---|
| Slack channel adapter (OAuth callback + event webhooks) | Yes |
| Telegram bot webhooks | Yes |
| WhatsApp (Twilio) webhooks | Yes |
| Public chat links (`/chat/*`) | Yes |
| Agent website proxy (`/site/<token>/`) | Yes |
| Nevermined paid chat (`/api/paid/*/chat`) | Yes |
| Web UI access for team members off-VPN | Yes |
| MCP access for Claude Code clients off-VPN | Yes |

If all your users and webhook sources can reach the server directly (e.g., via Tailscale), you do not need the Cloudflare Tunnel.

## Cloudflare Tunnel Setup

### 1. Create a tunnel

1. Log in to the [Cloudflare Zero Trust dashboard](https://one.cloudflare.com/).
2. Navigate to **Networks → Tunnels → Create a tunnel**.
3. Select **Cloudflared** as the connector type.
4. Give the tunnel a name (e.g., `trinity`).
5. Copy the tunnel token — it starts with `eyJ...`.

### 2. Configure public hostnames

In the Cloudflare dashboard, add public hostnames for your tunnel under the **Public Hostnames** tab. Route traffic to the internal Docker services by container name.

Recommended ingress rules:

| Path prefix | Routes to | Purpose |
|---|---|---|
| `/api/public/*` | `http://backend:8000` | Public API, OAuth callbacks |
| `/api/telegram/webhook/*` | `http://backend:8000` | Telegram bot webhooks |
| `/api/whatsapp/*` | `http://backend:8000` | WhatsApp/Twilio webhooks |
| `/api/paid/*` | `http://backend:8000` | Nevermined paid chat |
| `/api/webhooks/*` | `http://backend:8000` | Schedule webhook triggers |
| `/chat/*` | `http://frontend:80` | Public chat UI |
| `/site/*` | `http://backend:8000` | Agent website proxy |
| `/assets/*` | `http://frontend:80` | Static assets |
| `/` (catch-all) | `http://frontend:80` | SPA root and web UI |

### 3. Add the DNS record

In your domain's Cloudflare DNS settings, add a CNAME:

```
public.your-domain.com  →  <tunnel-id>.cfargotunnel.com
```

Cloudflare creates this automatically when you use **Public Hostnames** in Zero Trust.

## Required Environment Variables

Both variables are forwarded by `docker-compose.prod.yml`:

| Variable | Example value | Notes |
|---|---|---|
| `TUNNEL_TOKEN` | `eyJhIjoiY...` | The token from the Cloudflare Zero Trust dashboard. Required for the `cloudflared` container to authenticate. |
| `PUBLIC_CHAT_URL` | `https://public.your-domain.com` | The externally reachable base URL. Used to construct webhook URLs, public chat links, and agent website proxy URLs. Must match the public hostname you configured in Cloudflare. |

Set both in `.env`:

```
TUNNEL_TOKEN=your-tunnel-token-here
PUBLIC_CHAT_URL=https://public.your-domain.com
```

Also set `FRONTEND_URL` if it differs from `PUBLIC_CHAT_URL`:

```
FRONTEND_URL=https://trinity.your-domain.com
```

## Starting with the Tunnel

The `cloudflared` service is defined under the `tunnel` Compose profile. It does **not** start with a plain `docker compose up -d`. Start it explicitly:

```bash
# Start everything including the tunnel
docker compose -f docker-compose.prod.yml --profile tunnel up -d

# Start only the tunnel (if other services are already running)
docker compose -f docker-compose.prod.yml --profile tunnel up -d cloudflared
```

Verify the tunnel is connected:

```bash
docker logs trinity-cloudflared
# Look for: "Registered tunnel connection" or "Connection established"
```

## Stopping the Tunnel

```bash
docker compose -f docker-compose.prod.yml stop cloudflared
```

This stops the tunnel container without affecting the rest of the platform.

## Webhook URLs After Enabling Public Access

Once `PUBLIC_CHAT_URL` is set and the tunnel is connected, Trinity constructs webhook URLs automatically. These appear in the UI when you configure each integration:

| Integration | URL pattern |
|---|---|
| Telegram | `{PUBLIC_CHAT_URL}/api/telegram/webhook/{secret}` |
| WhatsApp (Twilio) | `{PUBLIC_CHAT_URL}/api/whatsapp/webhook/{secret}` |
| Schedule webhooks | `{PUBLIC_CHAT_URL}/api/webhooks/{token}` |

Copy the URL shown in the UI into the respective third-party dashboard (Telegram BotFather, Twilio console, etc.).

## Security Notes

- The Cloudflare Tunnel does **not** require opening any inbound firewall ports — the `cloudflared` container initiates an outbound connection to Cloudflare's edge.
- `TUNNEL_TOKEN` is a long-lived credential. Store it only in `.env` (gitignored). Do not commit it.
- Path prefixes not listed in your ingress rules return 404 at Cloudflare's edge before reaching your server.
- The agent website proxy (`/site/<token>/`) strips `authorization`, `cookie`, and `x-internal-secret` headers before forwarding to agent containers.
- Rate limits apply to the public endpoints (120 req/min per IP for `/site/`, 10 req/60s per token for schedule webhooks).

## See Also

- [Single-Server Deployment](single-server.md) — Base server setup before enabling public access
- [Slack Integration](../../integrations/slack-integration.md) — Slack OAuth and webhook configuration
- [Telegram Integration](../../integrations/telegram-integration.md) — Telegram bot webhook setup
