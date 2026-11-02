# Public Access

By default, Trinity is only accessible on the local network or via VPN. Enabling public access exposes specific paths through a Cloudflare Tunnel so that external webhooks, public chat links and MCP clients work without opening firewall ports or requiring a static IP.

## TLS Is Terminated Outside Trinity

Trinity serves plain HTTP; no compose file carries an HTTPS listener or a certificate step. Before an instance answers on a public address, choose one posture:

| Posture | What it gives you | When to use it |
|---|---|---|
| **Cloudflare Tunnel** (this page) | HTTPS at a real hostname, no inbound ports open at all | The default for a public instance. Nothing to renew. |
| **Private network** (Tailscale / WireGuard / VPC) | Encrypted transport; the instance is not on the public internet | Teams and webhook sources that can all reach the VPN. Inbound integrations (Telegram, WhatsApp, VoIP, webhook triggers) do not work without a public hostname. |
| **Reverse proxy you run** (Caddy / nginx + Let's Encrypt) | HTTPS at your own domain | You already operate a proxy. Have it send `X-Forwarded-Proto: https` on every request — Trinity reads that header to show `https://` webhook and MCP URLs (see [Webhook URLs](#webhook-urls-after-enabling-public-access)). |

Plain HTTP on a public IP with none of the above is the one combination to avoid. An instance prepared by `start.sh --provision` (the DigitalOcean 1-Click or installer script, an AWS or Vultr install) ships its own Caddy with a short-lived certificate for the instance's IP, and on the DigitalOcean and AWS paths the **Secure this instance** step of the first-run setup presents the tunnel below as the second hardening stage, after a domain — see [Single Server → DigitalOcean 1-Click](single-server.md#digitalocean-marketplace-1-click).

## What Public Access Enables

| Feature | Requires public access |
|---|---|
| Slack: the one-time OAuth install callback, and event delivery in webhook mode (the default Socket Mode connects outward and needs none) | Yes |
| Telegram bot webhooks | Yes |
| WhatsApp (Twilio) webhooks | Yes |
| Public chat links (`/chat/*`) | Yes |
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

In the Cloudflare dashboard, add a public hostname for your tunnel under the **Public Hostnames** tab. The `cloudflared` container runs on the Trinity networks, so it reaches the platform containers by name. On production and hosted installs it also sits on the ingress network at a reserved address, and its `backend` and `trinity-backend` names are pinned there (compose `extra_hosts`), so both rule shapes below reach the backend from that address — which is how the backend knows a request came through the tunnel and reads the visitor's address from it (see [Visitor addresses](#visitor-addresses-behind-the-tunnel-and-other-proxies)).

**Simplest: one catch-all rule** — route the whole hostname to the frontend, `http://trinity-frontend:8080`. The production frontend's nginx already proxies `/api/`, `/a2a/`, `/mcp`, `/ws` and `/health` to the right upstream, so a single rule serves the web UI, every webhook, and MCP. (The frontend container listens on **8080** inside the network; `FRONTEND_PORT` only changes the host-side mapping.)

**Narrower: path-split rules** — if you want to publish only the webhook surface rather than the whole UI. **Anchor the A2A rules.** An unanchored `/a2a/*` also matches `/api/agents/{name}/a2a/call` — Trinity's *outbound* A2A caller, an authenticated internal endpoint that has no business on a public hostname — so use the two anchored rules below rather than a prefix.

| Path prefix | Routes to | Purpose |
|---|---|---|
| `/api/public/*` | `http://trinity-backend:8000` | Public API, OAuth callbacks |
| `/api/telegram/webhook/*` | `http://trinity-backend:8000` | Telegram bot webhooks |
| `/api/whatsapp/*` | `http://trinity-backend:8000` | WhatsApp/Twilio webhooks |
| `/api/paid/*` | `http://trinity-backend:8000` | Nevermined paid chat |
| `^/a2a/[^/]+$` | `http://trinity-backend:8000` | A2A inbound JSON-RPC door (paid or authenticated) |
| `^/a2a/[^/]+/\.well-known/agent-card\.json$` | `http://trinity-backend:8000` | A2A discovery card |
| `/api/webhooks/*` | `http://trinity-backend:8000` | Schedule webhook triggers |
| `/api/voip/*` | `http://trinity-backend:8000` | VoIP media streams (WebSocket) |
| `/mcp` | `http://trinity-frontend:8080` | MCP server, proxied by the frontend |
| `/chat/*` | `http://trinity-frontend:8080` | Public chat UI |
| `/assets/*` | `http://trinity-frontend:8080` | Static assets |

These rules publish the webhook surface and not the web interface. To publish the interface as well, add a `/` catch-all routing to `http://trinity-frontend:8080`. To keep it private, leave that out and reach it over a private network — [Hardening → Step 2c](hardening.md#step-2c-both--the-tunnel-carries-the-callbacks-the-tailnet-carries-you).

Add the matching rule whenever you connect a new channel. A path you have not published stops delivering, with no error on this end.

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
| `PUBLIC_CHAT_URL` | `https://public.your-domain.com` | The externally reachable base URL. Used to construct webhook URLs and public chat links. Must match the public hostname you configured in Cloudflare. Also settable after login as the **Public URL** under **Settings → General**. |

Set both in `.env`:

```
TUNNEL_TOKEN=your-tunnel-token-here
PUBLIC_CHAT_URL=https://public.your-domain.com
```

Also set `FRONTEND_URL` if it differs from `PUBLIC_CHAT_URL`:

```
FRONTEND_URL=https://trinity.your-domain.com
```

## Visitor addresses behind the tunnel and other proxies

Per-client sign-in limits and audit-log addresses use the **visitor's** address. The backend learns it from a proxy only when that proxy is trusted; nothing else (an agent, a direct caller to port 8000, the Docker bridge gateway) can choose the address it is recorded under.

- **Cloudflare Tunnel, either rule shape.** A direct-to-backend rule (`http://trinity-backend:8000`) reaches the backend from the tunnel's reserved address; a catch-all rule (`http://trinity-frontend:8080`) goes through nginx, which appends the tunnel's address to `X-Forwarded-For`. In both cases the backend takes the visitor from Cloudflare's single `CF-Connecting-IP` header. No configuration is needed.
- **When identity is missing.** A request that arrives through a trusted proxy without any client identity is still served, but it is attributed to the proxy itself: all such requests share one rate-limit bucket and the audit log shows the proxy's address. Identity that is present but malformed (an empty or duplicated `CF-Connecting-IP`, an `X-Forwarded-For` entry that is not an IP address) is refused with `400`, or a WebSocket close code `1008`. The backend logs a warning, at most once a minute, while requests arrive without identity.
- **Your own reverse proxy or load balancer** in front of port 80: list its address or range in `TRUSTED_PROXIES` in `.env` (comma-separated, e.g. `TRUSTED_PROXIES=192.0.2.10,198.51.100.0/28`), and make sure it overwrites or appends `X-Forwarded-For` rather than passing a client's value through. Only the right-hand entries your trusted proxies appended are read. Wildcards, `0.0.0.0/0`, loopback and Trinity's own Docker networks are refused when the backend starts, with the offending entry named. A proxy on the **same host** should not be listed; it uses the loopback PROXY-protocol listener (`CADDY_PROXY_PORT`, see [Single-Server Deployment](single-server.md)).
- **Development and bare containers.** The development compose (Vite on port 80) trusts no proxy: every request is attributed to its connecting address. A bare `docker run` of the backend image trusts none either until you configure it.

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
| Schedule webhooks | `https://<the host you opened Trinity on>/api/webhooks/{token}` — see below |

Copy the URL shown in the UI into the respective third-party dashboard (Telegram BotFather, Twilio console, etc.).

**Schedule webhook URLs and the auto-detected MCP URL follow the proxy, not `PUBLIC_CHAT_URL`.** Their host is the one you opened Trinity on, and their scheme comes from the `X-Forwarded-Proto` header the proxy in front of Trinity sends: a proxy that sends `https` (a Cloudflare Tunnel, or your own proxy configured as above) gets `https://<host>/api/webhooks/{token}` and `https://<host>/mcp`. A proxy that sends no such header, or Trinity opened directly on its own port, gets `http://` URLs. The scheme is only ever upgraded by that header, never downgraded. So open Trinity through its public address before you copy a schedule's webhook URL.

MCP clients off-VPN connect to `{PUBLIC_CHAT_URL}/mcp` with an MCP API key (**Settings → MCP Keys**). The URL the UI shows to users is auto-detected the same way — `https://<host>/mcp` behind an HTTPS proxy, `http://<host>:8080/mcp` otherwise; **Settings → MCP Keys → MCP Server URL** overrides it.

## Security Notes

- The Cloudflare Tunnel does **not** require opening any inbound firewall ports — the `cloudflared` container initiates an outbound connection to Cloudflare's edge.
- `TUNNEL_TOKEN` is a long-lived credential. Store it only in `.env` (gitignored). Do not commit it.
- Path prefixes not listed in your ingress rules return 404 at Cloudflare's edge before reaching your server.
- Rate limits apply to the public endpoints — schedule webhooks, for example, accept 10 triggers per token per 60 s (`WEBHOOK_RATE_LIMIT`) and 60 requests per IP per 60 s before authentication (`WEBHOOK_IP_RATE_LIMIT`).
- Forwarded client addresses are believed only from the bundled nginx, the bundled `cloudflared` and the proxies you list in `TRUSTED_PROXIES` — see [Visitor addresses](#visitor-addresses-behind-the-tunnel-and-other-proxies).
- MCP over the tunnel is protected by the MCP API key on every request. Leave `MCP_INLINE_AUTH_ENABLED` at its default (`false`) unless you intend keyless email-code sign-in over MCP, and then only over TLS.

## See Also

- [Single-Server Deployment](single-server.md) — Base server setup before enabling public access, and the DigitalOcean 1-Click's own HTTPS
- [Slack Integration](../../integrations/slack-integration.md) — Slack OAuth and webhook configuration
- [Telegram Integration](../../integrations/telegram-integration.md) — Telegram bot webhook setup
