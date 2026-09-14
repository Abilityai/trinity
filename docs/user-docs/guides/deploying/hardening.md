# Hardening a Marketplace Install

Take a one-click Trinity droplet from a bare public IP to an instance you would leave running: a real domain, then either a Cloudflare Tunnel or a private network, with the public ports closed behind you.

## When to Run This

- You created a Trinity instance from a cloud marketplace listing, or with the DigitalOcean install script, and it answers at a bare IP address.
- The instance is about to hold real work, real credentials, or other people's data.
- You want a memorable address instead of an IP, and an ordinary long-lived certificate instead of the short-lived IP one.

Not for you if Trinity already runs on a private network — the managed fleet's own shape — or behind a reverse proxy you operate. Those are finished postures, not compromises.

## What You Start With

| Component | State on first boot |
|---|---|
| Caddy on ports 80 and 443 | A browser-trusted Let's Encrypt certificate for the droplet's **IP address**, so there is no warning and no domain required. Certificates on this profile last about six days and renew while the server runs — an instance switched off for longer comes back to a browser warning until renewal catches up |
| Host firewall | Inbound 22, 80 and 443 only |
| Container ports | Not reachable from off-box. Docker publishes past ordinary firewall rules, so Trinity installs its own rules that drop anything arriving at a container from outside. The backend, MCP server and log collector are reachable only through Caddy, or from the droplet itself |
| Admin account | None until someone claims it in a browser — **whoever opens it first becomes the admin** |

So the exposure is not "everything is open". It is that the web UI and the API answer anyone on the internet who finds the address, protected by your login alone.

## Pre-flight

- [ ] **The instance is claimed** — if nobody has created the admin account yet, do that first.
- [ ] **You own a domain** and can edit its DNS records.
- [ ] **For the tunnel path:** that domain's DNS is hosted by Cloudflare, and you can sign in to the Cloudflare Zero Trust dashboard.
- [ ] **For the private-network path:** you have confirmed nothing outside needs to call your instance — see the comparison in [Step 2](#step-2-choose-how-it-is-reached).
- [ ] **Shell access to the droplet** for Step 2 — over SSH, or the provider's web console.

## Procedure

### Step 1: Give it a real name

Create an `A` record for the name you want, pointing at the droplet's IPv4 address, and confirm it resolves:

```bash
dig +short your-domain.com
# Expected: the droplet's IP address
```

Then, in Trinity: **Settings → General → Public URL**, enter the full address including `https://`, and save. No terminal step, no certificate to install.

What saving it does:

- Trinity hands out that name instead of the IP everywhere it publishes an address — Telegram, WhatsApp and VoIP callbacks, Slack's OAuth return, public chat links, workspace links, file downloads.
- It authorises the web server in front to obtain a certificate **for that one name**. Caddy asks Trinity whether a name is allowed before requesting a certificate, and Trinity answers yes only for the saved name — so nobody else can point a domain at your droplet and have certificates issued on your account.
- It re-registers existing Telegram webhooks and rewrites WhatsApp binding URLs to the new base **immediately**. On a name that is not live yet, working bots move to an address that answers nothing, which is why the DNS record comes first.

The certificate is obtained on the first request that arrives for the name, so **visiting the site is what completes this step**. Until someone does, Trinity says so: Settings reads *saved, waiting for the first visit*, and the first-run setup step stays open.

### Step 2: Choose how it is reached

Both options reach the same outcome — nothing listening on the public interface — and differ in one way that decides it for you.

| | Cloudflare Tunnel | Private network (Tailscale) |
|---|---|---|
| Web UI, Workspace, MCP | Anyone you give the address to | Your devices only |
| Telegram, WhatsApp, VoIP | Work | **Broken** |
| Public chat links, agent websites, webhook triggers, paid chat, inbound agent-to-agent | Work | **Broken** |
| Slack | Works | Works — Trinity connects outward over a WebSocket |
| Needs | A domain on Cloudflare | A Tailscale account and a device to connect from |

Everything in the broken column is a third party making a request **to** your instance, and a private network is precisely what prevents that. Choose the tunnel unless nothing outside needs to call in.

### Step 2a: Cloudflare Tunnel

`cloudflared` runs beside Trinity and dials **outward** to Cloudflare; traffic arrives back down that connection.

The Cloudflare-side setup — creating the tunnel, the ingress rules, the DNS record — is identical on a marketplace droplet. Follow [Public Access → Cloudflare Tunnel Setup](public-access.md#cloudflare-tunnel-setup), then return here for the two things that differ on this install.

**1. Put the token in `.env`:**

```bash
sudo nano /opt/trinity/.env
# add or edit:
# TUNNEL_TOKEN=eyJ...
```

**2. Restart through the installer, not `docker compose`:**

```bash
cd /opt/trinity
sudo ./scripts/deploy/start.sh --hosted
```

A non-empty `TUNNEL_TOKEN` is treated as intent: the installer starts the tunnel profile and records it in `.env`, so later `docker compose ... stop` and `... logs` act on the tunnel too. This is the same command used to update the instance, so it is safe to re-run.

> **Use `docker compose restart`, not `down/up`.** `docker compose down` removes the `trinity-agent-network`, which orphans every running agent container — they keep running but lose their network and have to be removed and recreated. `restart` preserves both the agents and the network. The only times to use `down` are: (1) intentional full teardown, (2) recovering from a corrupted compose state.

### Step 2b: Private network (Tailscale)

Trinity has no Tailscale integration — this is a host-level install, and Trinity neither configures nor monitors it. Install it from Tailscale's own instructions, then mind two things specific to this image:

- **Install Tailscale *after* provisioning, never before.** The installer resets the host firewall when it provisions a machine, discarding rules you added by hand. If you re-run provisioning later, re-add your rules afterwards.
- **Reach Trinity through Caddy, not container ports.** Over the tailnet, `https://your-domain.com` and the droplet's ports 80/443 work. Direct container ports — 8000 for the API, 8080 for MCP, 8686 for the log collector — do **not**, and a firewall rule cannot open them, because Trinity's container rules are evaluated first. Point MCP clients at `https://your-domain.com/mcp`.

Slack is the one integration that survives this path: Trinity connects outward to Slack over a WebSocket, so channels keep working. Installing the Slack app the first time still needs a public address for the OAuth callback — do that before closing the instance off.

### Step 3: Close the public ports

Once the tunnel is connected, or you can reach the instance over the tailnet, add a cloud firewall blocking inbound 80 and 443. Leave 22 reachable from your own address only, or use the provider's console for shell access.

**One cost to know about.** Closing 80 and 443 also stops Caddy renewing the certificates it holds for the droplet's IP and for your domain — renewal needs those ports. Behind a tunnel that does not matter day to day, because visitors arrive over Cloudflare's certificate rather than Caddy's, but the local ones expire and the logs will say so. If you later reopen the ports and connect directly, expect a browser warning until renewal catches up.

## Verify

Run these as you go. If a check fails, do not close the ports — see **Recovery**.

| Check | Command | Expected |
|---|---|---|
| DNS points here | `dig +short your-domain.com` | The droplet's IP |
| The domain serves Trinity | Open `https://your-domain.com` in a browser | Trinity loads, valid padlock, no warning |
| Trinity agrees it is live | **Settings → General → Public URL** | The address with a tick, not *saved, waiting for the first visit* |
| Tunnel connected (2a) | `docker logs trinity-cloudflared \| tail -20` | `Registered tunnel connection` |
| Traffic is on the tunnel (2a) | Block inbound 80/443, reload the site | The site still loads |
| Platform healthy | `curl -s http://localhost:8000/health` | `{"status":"healthy",...}` |

The full six-probe check is in [Monitoring](monitoring.md) — run it if anything above looks wrong.

## Recovery

| Symptom | Cause | Fix |
|---|---|---|
| Certificate error on the domain | The request is not reaching this droplet, so no certificate was ever obtained | Re-check the `A` record, then that 80 and 443 are open to the internet (a cloud firewall added during the claim window is the usual cause) |
| Settings still says *saved, waiting for the first visit* | Nothing has arrived at that name yet | Load the site in a browser. If it still does not flip, treat as the row above |
| Telegram or WhatsApp stopped delivering after saving the domain | The webhooks were re-pointed at the new address before it was live | Confirm the domain loads, then re-save the Public URL to re-register them |
| Site unreachable after closing 80/443 | The tunnel is not carrying traffic | Reopen the ports, check `docker logs trinity-cloudflared`, re-check the ingress rules, then close them again |
| MCP client cannot connect over the tailnet | It is pointed at a container port | Point it at `https://your-domain.com/mcp` |

**Clearing the Public URL** stops Trinity handing the name out, but if the address was also baked into the server's environment at install time, the web server keeps serving certificates for it. Settings then reads *not configured* while that is still true — change the environment value and restart if you need it genuinely gone.

## The Short Version

- The marketplace default is for **evaluation**. It answers anyone who finds the address.
- Add a domain as soon as the instance is more than a test, and confirm it by loading the site.
- Keep a real instance off the open internet: a tunnel if anything needs to call in, a private network if not.
- Closing 80 and 443 afterwards is the step that makes it real.

## See Also

- [Public Access](public-access.md) — the Cloudflare Tunnel setup in full, and the ingress rules
- [Single-Server Deployment](single-server.md) — base server setup
- [Backup and Restore](backup-and-restore.md) — have this in place before the instance matters
- [Monitoring](monitoring.md) — the six health probes and resource thresholds
