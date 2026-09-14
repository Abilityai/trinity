# Hardening a Marketplace Install

A one-click Trinity droplet answers the public internet from the moment it boots. No provider in this channel can attach a private network at create time, so that is the starting posture by construction, not an oversight — and it is fine for evaluation, which is what the marketplace listing is for. This page is the path from there to an instance you would leave running.

Three stages, in order. Each one stands on its own, and each one is verifiable before you move to the next.

| Stage | What it changes | Who should do it |
|---|---|---|
| 1. Add a domain | A memorable name and an ordinary certificate instead of a bare IP | Anyone past the first evaluation |
| 2. Cloudflare Tunnel | The server stops listening on the public internet; inbound integrations keep working | Any instance that matters |
| 3. Tailscale (instead of stage 2) | Only you can reach it, over a private network; inbound integrations stop working | An instance nobody else needs to reach |

Stage 3 is an alternative to stage 2, not a step after it. Pick one, and the choice is decided by whether anything on the internet has to call your instance — see [Tailscale, and what it costs you](#tailscale-and-what-it-costs-you).

> **Before anything else:** a fresh droplet has no admin account until someone claims it in a browser, and whoever opens it first becomes the admin. If you have not claimed yours yet, do that now — the claim window and how to close it are covered in [DEPLOYMENT.md → Security Recommendations](https://github.com/abilityai/trinity/blob/main/docs/DEPLOYMENT.md#security-recommendations).

## What You Start With

A 1-Click droplet boots with:

- **Caddy on ports 80 and 443**, holding a browser-trusted Let's Encrypt certificate issued for the droplet's IP address. It works, and it needs no domain — but certificates on that profile last about six days, so an instance left switched off for longer comes back to a browser warning until renewal catches up.
- **A host firewall** allowing only 22, 80 and 443 inbound.
- **Container ports that are not reachable from outside**, enforced separately from the host firewall. Docker publishes past ordinary firewall rules, so Trinity installs its own rules that drop anything arriving at a container from off-box. The backend, the MCP server and the log collector are reachable only through Caddy, or from the droplet itself.

So the exposure is not "everything is open". It is that the web UI and the API answer anyone on the internet who finds the address, and they are protected by your login alone.

## Stage 1 — Add a Domain

**Prerequisite: point the domain at this server first.** Create an `A` record for the name you want, with the droplet's IPv4 address as its value. Wait until it resolves before moving on — `dig +short your-domain.com` from your own machine should print the droplet's address.

Then, in Trinity: **Settings → General → Public URL**, enter the full address including `https://`, and save. That is the entire stage — there is no terminal step and no certificate to install.

What saving it does:

- Trinity hands out that name instead of the IP everywhere it publishes an address: Telegram, WhatsApp and VoIP callbacks, Slack's OAuth return, public chat links, workspace links, file downloads.
- It authorises the web server in front to obtain a certificate **for that one name**. Caddy asks Trinity whether a name is allowed before requesting a certificate, and Trinity answers yes only for the name you saved. Nobody else can point a domain at your droplet and have certificates issued on your account.
- It re-registers existing Telegram webhooks and rewrites WhatsApp binding URLs to the new base, immediately. If the domain is not live yet, working bots move to an address that answers nothing — which is why the DNS record comes first.

**Clearing it later.** Emptying the Public URL field stops Trinity handing the name out, but if the address was also baked into the server's environment at install time, the web server keeps serving certificates for it. Settings will read "not configured" while that is still true — change the environment value and restart if you need it genuinely gone.

**How to verify it worked.** Open `https://your-domain.com` in a browser. You should get the Trinity login or dashboard with a valid padlock and no warning. The certificate is obtained on that first request, so this visit is what completes the stage — until someone makes it, nothing has been proven, and Trinity says so: the Public URL in Settings reads *saved, waiting for the first visit* until a request for the name actually arrives.

If the browser shows a certificate error instead, the request is not reaching this droplet. Check the `A` record, then that ports 80 and 443 are open to the internet (a cloud firewall you added during the claim window is the usual cause).

## Stage 2 — Cloudflare Tunnel

A tunnel is the step that actually takes the server off the public internet. `cloudflared` runs beside Trinity and dials **outward** to Cloudflare; traffic arrives back down that connection. Nothing needs to listen publicly, so you can close 80 and 443 entirely afterwards.

Inbound integrations keep working, because the world still reaches a public hostname — Cloudflare's — and Cloudflare forwards to you. That is the property that makes this the recommended posture rather than a VPN.

**Prerequisite:** stage 1, on a domain whose DNS is hosted by Cloudflare.

### The steps

The Cloudflare-side setup — creating the tunnel, the ingress rules, the DNS record — is the same on a marketplace droplet as anywhere else. Follow [Public Access](public-access.md#cloudflare-tunnel-setup) for those, then come back here for the two things that differ on this install.

**1. Put the token in `.env`.** On the droplet:

```bash
sudo nano /opt/trinity/.env
# add or edit:
# TUNNEL_TOKEN=eyJ...
```

**2. Restart through the installer, not `docker compose`.**

```bash
cd /opt/trinity
sudo ./scripts/deploy/start.sh --hosted
```

A non-empty `TUNNEL_TOKEN` is treated as intent: the installer starts the tunnel profile and records it in `.env`, so later `docker compose ... stop` and `... logs` act on the tunnel too. This is the same command used to update the instance, so it is safe to re-run.

### How to verify it worked

```bash
docker logs trinity-cloudflared | tail -20
# Look for: "Registered tunnel connection"
```

Then load your domain in a browser — you are now arriving through Cloudflare. To confirm the tunnel is carrying the traffic rather than the old direct path, close the door behind you: add a cloud firewall that blocks inbound 80 and 443, and load the site again. If it still works, you are on the tunnel. If it stops, the tunnel is not carrying traffic yet — re-check the ingress rules before leaving the ports closed.

Leave 22 reachable from your own address only, or use the provider's console for shell access.

**One cost to know about.** Closing 80 and 443 also stops Caddy renewing the certificates it holds for the droplet's IP and for your domain — renewal uses those ports. Behind a tunnel that does not matter day to day, because visitors arrive over Cloudflare's certificate rather than Caddy's, but the local ones will expire and the logs will say so. If you ever reopen the ports and reach the instance directly, expect a browser warning until renewal catches up.

## Tailscale, and What It Costs You

Tailscale (or any VPN) puts the droplet on a private network only your devices can reach. It is a legitimate finished posture — it is what Trinity's own managed fleet runs — but it is **not** a substitute for the tunnel, and picking it by mistake breaks things quietly.

**What stops working:** everything that calls *in*. Telegram, WhatsApp and VoIP webhooks, Slack events, public chat links, agent website links, schedule webhook triggers, inbound agent-to-agent calls. All of those are third parties making a request to your instance, and a private network is precisely what prevents that.

**Choose Tailscale if** you are the only person who uses this instance and you drive it from the UI, Claude Code or the CLI. **Choose the tunnel if** anything outside needs to reach it.

### Installing it on a marketplace droplet

Trinity has no Tailscale integration — this is a host-level install, and Trinity neither configures nor monitors it. Install it the standard way from Tailscale's own instructions, then two things specific to this image:

- **Install Tailscale *after* provisioning, never before.** The installer resets the host firewall when it provisions a machine, which discards rules you added by hand. If you re-run provisioning later, re-add any Tailscale firewall rules afterwards.
- **Reach Trinity through Caddy, not through container ports.** Over the tailnet, `https://your-domain.com` and the droplet's ports 80/443 work. Direct container ports — 8000 for the API, 8080 for MCP, 8686 for the log collector — do **not**, and cannot be opened by a firewall rule, because the container firewall rules are evaluated first. Point MCP clients at `https://your-domain.com/mcp`.

Once you are on the tailnet, close 80 and 443 to the internet with a cloud firewall. The instance is then reachable only from your devices.

## The Short Version

- The marketplace default is for **evaluation**. It is reachable by anyone who finds the address.
- Add a domain as soon as the instance is more than a test, and confirm it by loading the site.
- Keep a real instance off the open internet: a tunnel if anything needs to call in, a private network if not.
- Whichever you choose, close 80 and 443 with a cloud firewall afterwards — that is the step that makes it real.

## See Also

- [Public Access](public-access.md) — the Cloudflare Tunnel setup in full, and the ingress rules
- [Single-Server Deployment](single-server.md) — base server setup
- [Backup and Restore](backup-and-restore.md) — what to have in place before this instance matters
