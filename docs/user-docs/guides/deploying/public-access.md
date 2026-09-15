# Public Access

By default, Trinity is only accessible on the local network or via VPN. Enabling public access exposes specific paths through a Cloudflare Tunnel so that external webhooks, public chat links and MCP clients work without opening firewall ports or requiring a static IP.

## TLS Is Terminated Outside Trinity

Trinity serves plain HTTP; no compose file carries an HTTPS listener or a certificate step. Before an instance answers on a public address, choose one posture:

| Posture | What it gives you | When to use it |
|---|---|---|
| **Cloudflare Tunnel** (this page) | HTTPS at a real hostname, no inbound ports open at all | The default for a public instance. Nothing to renew. |
| **Private network** (Tailscale / WireGuard / VPC) | Encrypted transport; the instance is not on the public internet | Teams and webhook sources that can all reach the VPN. Inbound integrations (Telegram, WhatsApp, VoIP, webhook triggers) do not work without a public hostname. |
| **Reverse proxy you run** (Caddy / nginx + Let's Encrypt) | HTTPS at your own domain | You already operate a proxy. |

Plain HTTP on a public IP with none of the above is the one combination to avoid. The DigitalOcean 1-Click ships its own Caddy with a short-lived certificate for the Droplet's IP, and its first-run **Secure this instance** card presents the tunnel below as the second hardening step — see [Single Server → DigitalOcean 1-Click](single-server.md#digitalocean-marketplace-1-click).

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
| MCP access for Claude Code clients off-VPN (`/mcp`) | Yes |

If all your users and webhook sources can reach the server directly (e.g., via Tailscale), you do not need the Cloudflare Tunnel.

## Cloudflare Tunnel Setup

### 1. Create a tunnel

1. Log in to the [Cloudflare Zero Trust dashboard](https://one.cloudflare.com/).
2. Navigate to **Networks → Tunnels → Create a tunnel**.
3. Select **Cloudflared** as the connector type.
4. Give the tunnel a name (e.g., `trinity`).
5. Copy the tunnel token — it starts with `eyJ...`.

### 2. Configure public hostnames

In the Cloudflare dashboard, add a public hostname for your tunnel under the **Public Hostnames** tab. The `cloudflared` container runs on both Trinity networks, so it reaches the platform containers by name.

**Simplest: one catch-all rule** — route the whole hostname to the frontend, `http://trinity-frontend:8080`. The production frontend's nginx already proxies `/api/`, `/a2a/`, `/mcp`, `/ws` and `/health` to the right upstream, so a single rule serves the web UI, every webhook, and MCP. (The frontend container listens on **8080** inside the network; `FRONTEND_PORT` only changes the host-side mapping.)

**Narrower: path-split rules** — if you want to publish only the webhook surface rather than the whole UI:

| Path prefix | Routes to | Purpose |
|---|---|---|
| `/api/public/*` | `http://trinity-backend:8000` | Public API, OAuth callbacks |
| `/api/telegram/webhook/*` | `http://trinity-backend:8000` | Telegram bot webhooks |
| `/api/whatsapp/*` | `http://trinity-backend:8000` | WhatsApp/Twilio webhooks |
| `/api/paid/*` | `http://trinity-backend:8000` | Nevermined paid chat |
| `/api/webhooks/*` | `http://trinity-backend:8000` | Schedule webhook triggers |
| `/api/voip/*` | `http://trinity-backend:8000` | VoIP media streams (WebSocket) |
| `/mcp` | `http://trinity-frontend:8080` | MCP server, proxied by the frontend |
| `/chat/*` | `http://trinity-frontend:8080` | Public chat UI |
| `/site/*` | `http://trinity-backend:8000` | Agent website proxy |
| `/assets/*` | `http://trinity-frontend:8080` | Static assets |
| `/` (catch-all) | `http://trinity-frontend:8080` | SPA root and web UI |

### 3. Add the DNS record

In your domain's Cloudflare DNS settings, add a CNAME:

```
public.your-domain.com  →  <tunnel-id>.cfargotunnel.com
```

Cloudflare creates this automatically when you use **Public Hostnames** in Zero Trust.

## Required Environment Variables

Both variables are forwarded by `docker-compose.prod.yml` and `docker-compose.hosted.yml` (the dev compose does not forward `TUNNEL_TOKEN` — a tunnel is a server-install feature):

| Variable | Example value | Notes |
|---|---|---|
| `TUNNEL_TOKEN` | `eyJhIjoiY...` | The token from the Cloudflare Zero Trust dashboard. Required for the `cloudflared` container to authenticate. |
| `PUBLIC_CHAT_URL` | `https://public.your-domain.com` | The externally reachable base URL. Used to construct webhook URLs, public chat links, and agent website proxy URLs. Must match the public hostname you configured in Cloudflare. Also settable after login as the **Public URL** under **Settings → General**. |

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

The `cloudflared` service is defined under the `tunnel` Compose profile. Setting `TUNNEL_TOKEN` alone starts nothing.

**Hosted installs:** `./scripts/deploy/start.sh --hosted` reads `TUNNEL_TOKEN` from `.env`, adds `--profile tunnel` itself, and appends `COMPOSE_PROFILES=tunnel` to `.env` — so every later bare `docker compose -f docker-compose.hosted.yml stop` / `logs` also acts on the tunnel container. Without that persisted profile, `stop` would leave the tunnel running and the instance publicly reachable after you had been told the stack was down.

**Source-built installs:** pass the profile explicitly:

```bash
# Start everything including the tunnel
docker compose -f docker-compose.prod.yml --profile tunnel up -d

# Start only the tunnel (if other services are already running)
docker compose -f docker-compose.prod.yml --profile tunnel up -d cloudflared

# Or make the profile stick for every later command
echo 'COMPOSE_PROFILES=tunnel' >> .env
```

Verify the tunnel is connected:

```bash
docker ps | grep cloudflared          # a missing container is silent — check it exists
docker logs trinity-cloudflared
# Look for: "Registered tunnel connection" or "Connection established"
```

## Stopping the Tunnel

```bash
docker compose -f docker-compose.prod.yml stop cloudflared     # or -f docker-compose.hosted.yml
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

MCP clients off-VPN connect to `{PUBLIC_CHAT_URL}/mcp` with an MCP API key (**Settings → MCP Keys**); **Settings → MCP Keys → MCP Server URL** sets the URL the UI shows to users.

## Security Notes

- The Cloudflare Tunnel does **not** require opening any inbound firewall ports — the `cloudflared` container initiates an outbound connection to Cloudflare's edge.
- `TUNNEL_TOKEN` is a long-lived credential. Store it only in `.env` (gitignored). Do not commit it.
- Path prefixes not listed in your ingress rules return 404 at Cloudflare's edge before reaching your server.
- The agent website proxy (`/site/<token>/`) strips `authorization`, `cookie`, and `x-internal-secret` headers before forwarding to agent containers.
- Rate limits apply to the public endpoints (120 req/min per IP for `/site/`, 10 req/60s per token for schedule webhooks).
- MCP over the tunnel is protected by the MCP API key on every request. Leave `MCP_INLINE_AUTH_ENABLED` at its default (`false`) unless you intend keyless email-code sign-in over MCP, and then only over TLS.

## See Also

- [Single-Server Deployment](single-server.md) — Base server setup before enabling public access, and the DigitalOcean 1-Click's own HTTPS
- [Slack Integration](../../integrations/slack-integration.md) — Slack OAuth and webhook configuration
- [Telegram Integration](../../integrations/telegram-integration.md) — Telegram bot webhook setup
