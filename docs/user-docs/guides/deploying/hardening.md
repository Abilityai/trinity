# Hardening a Marketplace Install

Take a one-click Trinity droplet from a bare public IP to an instance you would leave running: a real domain, then a Cloudflare Tunnel and a private network — together, not instead of each other — with the public ports closed behind you.

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

Everything except the web interface is already closed. What stays open is Trinity itself: the web UI and the API answer anyone on the internet who finds the address, and your login is the only thing in the way.

## Pre-flight

- [ ] **The instance is claimed** — if nobody has created the admin account yet, do that first.
- [ ] **You own a domain** and can edit its DNS records.
- [ ] **For the tunnel path:** that domain's DNS is hosted by Cloudflare, and you can sign in to the Cloudflare Zero Trust dashboard.
- [ ] **For tailnet-only:** you have confirmed nothing outside needs to call your instance — see the comparison in [Step 2](#step-2-choose-how-it-is-reached). If anything does, you want both rather than this.
- [ ] **For both (2c):** you accept owning the list of paths the tunnel publishes, and re-checking it whenever you add a channel.
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

A tunnel and a private network are **not two options to pick between**. They solve different halves, they run side by side on the same server, and running both is the posture worth aiming at. All three columns end with no public ports open.

| | Tunnel only | Tailnet only | **Both** |
|---|---|---|---|
| Web UI, Workspace, MCP | Anyone with the address | Your devices only | **Your devices only** |
| Telegram, WhatsApp, VoIP | Work | Broken | **Work** |
| Public chat links, agent websites, webhook triggers, paid chat, inbound agent-to-agent | Work | Broken | **Work** |
| Slack | Works | Works — Trinity connects outward over a WebSocket | Works |
| Public ports open | None | None | None |
| Needs | A domain on Cloudflare | A Tailscale account and a device to connect from | Both |

**A tunnel is not privacy.** It closes the ports and hides the origin IP, and Cloudflare absorbs traffic before it reaches you — all real. But the hostname it publishes still answers anyone who has it, with your login as the only thing in the way: the same sentence as [What You Start With](#what-you-start-with). If that is what you wanted, tunnel-only is a fine place to stop.

**Everything Broken in the tailnet column is a third party calling *in*,** and a private network is precisely what prevents that. Which is why tailnet-only suits an instance nothing calls into, and why it is the wrong column to read as a verdict on private networks generally — the Both column is the same tailnet with the callbacks routed around it.

> **Telegram's Broken is ours, not Telegram's.** Trinity drives Telegram by webhook, so Telegram's servers have to reach your instance. The Bot API also offers outbound long polling, which would survive a tailnet exactly as Slack's Socket Mode does; Trinity does not implement it. Tracked as a gap, not a limitation: [#2849](https://github.com/abilityai/trinity/issues/2849).

**Where to go:** [Step 2a](#step-2a-cloudflare-tunnel) for the tunnel, [Step 2b](#step-2b-private-network-tailscale) for the tailnet, and [Step 2c](#step-2c-both-the-tunnel-carries-the-callbacks-the-tailnet-carries-you) to combine them — which is 2a and 2b plus one change to what the tunnel publishes.

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

Trinity has no Tailscale integration — this is a host-level install that Trinity neither configures nor monitors. There is no one-click or invite mechanism to lean on: neither DigitalOcean nor Vultr lists a Tailscale app, and both apply marketplace images at create time only, so nothing can be added to a droplet that is already running. The install is two commands.

**Reaching the web UI over a tailnet takes one setting** — see [Reaching the UI over the tailnet](#reaching-the-ui-over-the-tailnet) below, after the install.

**Before you start**, in the Tailscale admin console → **Keys** → *Generate auth key*: make it **reusable**, set an expiry (90 days is the maximum Tailscale allows), tick **pre-approved** if your tailnet has device approval on, and leave **ephemeral** off — ephemeral devices are removed 30–60 minutes after they go quiet, which would evict a server.

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --auth-key=tskey-auth-XXXX --ssh --hostname=trinity
tailscale ip -4
```

`--ssh` matters: it is what still gives you a shell after the next step closes port 22, and it must be opted into per device.

**Then turn off key expiry for this machine** — admin console → Machines → the device's menu → *Disable key expiry*. New tailnets expire node keys after 180 days by default, and a server that silently drops off the tailnet with port 22 already closed leaves the provider's recovery console as the only way back in. This is a required step, not a nicety.

Only once `tailscale ip -4` answers and you have confirmed you can SSH over the tailnet:

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow in on tailscale0
sudo ufw delete allow 22/tcp
sudo ufw enable
```

Order matters — the tailnet rule has to exist before the deny takes effect. (Tailscale's own guide enables the firewall first and writes the delete without `allow`, which does not match the rule as added.)

Two more things specific to this image:

- **Install Tailscale *after* provisioning, never before.** The installer resets the host firewall when it provisions a machine, discarding rules you added by hand. If you re-run provisioning later, re-add these.
- **Container ports stay unreachable over the tailnet.** 8000 for the API, 8080 for MCP, 8686 for the log collector — a firewall rule cannot open them, because Trinity's container rules are evaluated first.

Worth knowing: a new tailnet's default policy lets **every** device on your tailnet reach this one, not only the laptop you joined from. And Slack is the one integration that survives this path, because Trinity connects outward to Slack over a WebSocket — though installing the Slack app the first time still needs a public address for the OAuth callback.

### Reaching the UI over the tailnet

By default there is nothing to browse to once the public ports are closed, and one setting fixes it.

**Why the default leaves you stranded.** The web server picks which site to serve by the hostname in the request, not by the interface it arrived on, and it has exactly two: the droplet's public IP, which it holds a certificate for, and the domain you saved, which it obtains one for on demand. A tailnet address matches neither. Trinity is asked whether a certificate may be issued for it and answers no — that refusal is what stops your instance being an open certificate requester for anyone who points a name at it — and the connection fails. It could not succeed anyway: tailnet addresses live in carrier-grade NAT space, which no public certificate authority can validate. The saved domain does not help either, because public DNS resolves it to the address you just closed off.

**The fix: serve plain HTTP to the private network.** The VPN already encrypts the transport, so there is nothing for TLS to add. In `/opt/trinity/.env`:

```bash
PRIVATE_NETWORK_CIDRS="100.64.0.0/10 fd7a:115c:a1e0::/48"
```

Those two ranges are the ones Tailscale hands out, so they are correct whatever address your droplet ends up with. WireGuard, Nebula and ZeroTier use different space — use your own. Then re-render the web server's configuration:

```bash
cd /opt/trinity
sudo ./scripts/deploy/start.sh --provision --cloud digitalocean --caddy-only
```

`--caddy-only` rewrites the web-server config and nothing else. A plain restart does not pick the variable up, and re-running the full provisioning would also rewrite this instance's recorded address and install provenance, so it is the wrong tool for a one-variable change. The generated configuration is checked before the web server is reloaded — an invalid one leaves the running config alone rather than taking the site down.

Now `http://<tailnet-ip>` serves Trinity. Requests from anywhere else keep redirecting to HTTPS exactly as before.

The rule matches the **source address of the connection**, not a header, so a request from the public internet cannot claim to be local to get the login page in cleartext. Setting the value to `0.0.0.0/0` is refused for the same reason.

**If you would rather not serve any HTTP at all**, an SSH tunnel over the tailnet works with no configuration: `ssh -L 8443:127.0.0.1:8081 root@<tailnet-ip>`, then browse `http://localhost:8443`. The tunnel exits on the droplet as a local connection, so it needs no certificate.

**Why you cannot just browse the container port.** Trinity's container firewall drops anything reaching a container from off-box, tailnet traffic included, so `http://<tailnet-ip>:8081` is dropped. Ports 80 and 443 work because the web server in front is a host process, and host ports never pass through those rules.

### Step 2c: Both — the tunnel carries the callbacks, the tailnet carries you

The posture worth aiming at, and it needs no Trinity change. Do [Step 2a](#step-2a-cloudflare-tunnel) and [Step 2b](#step-2b-private-network-tailscale) as written, then make one change to what the tunnel publishes.

**The one change.** Step 2a's simplest setup is a single catch-all rule routing the whole hostname to the frontend, which publishes the web UI along with everything else. Instead, publish only the paths third parties actually call — the *"Narrower: path-split rules"* table in [Public Access](public-access.md#2-configure-public-hostnames) lists them — and **leave out the `/` catch-all row**. Without it the tunnel carries webhooks, public chat links, agent websites and MCP, and simply has no route for the admin UI.

Then reach the UI over the tailnet exactly as Step 2b describes, with `PRIVATE_NETWORK_CIDRS` set. Both paths terminate at the same Caddy on the same droplet; they differ only in how a request arrives.

The result is the third column of the table in [Step 2](#step-2-choose-how-it-is-reached): callbacks working, the UI answering your devices only, and nothing listening on the public interface.

**What this costs you.** An allowlist you own. Add a channel that introduces a new inbound path and you must publish that path, or it silently stops delivering — no error on this end, because the request never arrives. That is the safe direction to fail (a missing entry breaks one integration loudly rather than exposing the UI quietly), but it is still a list, and [Verify](#verify) is how you find out.

**If you would rather the UI stayed public but gated**, Cloudflare Access sits in front of the tunnel hostname and challenges a visitor for identity before the request is allowed down it. Trinity ships no integration for it; it is configured entirely on the Cloudflare side, and their documentation owns the steps. The same allowlist problem applies in mirror image — every machine caller needs a bypass, or it is challenged and fails.

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
| MCP client cannot connect over the tailnet | It is pointed at a container port, which the container firewall drops | Point it at `https://your-domain.com/mcp`, or tunnel to `127.0.0.1:8081` over SSH |
| Browser cannot reach the UI after joining a tailnet and closing the ports | A tailnet address has no certificate and cannot get one | Set `PRIVATE_NETWORK_CIDRS` and re-render — [Reaching the UI over the tailnet](#reaching-the-ui-over-the-tailnet) |

**Clearing the Public URL** stops Trinity handing the name out, but if the address was also baked into the server's environment at install time, the web server keeps serving certificates for it. Settings then reads *not configured* while that is still true — change the environment value and restart if you need it genuinely gone.

## The Short Version

- The marketplace default is for **evaluation**. It answers anyone who finds the address.
- Add a domain as soon as the instance is more than a test, and confirm it by loading the site.
- Keep a real instance off the open internet. A tunnel and a private network are not a choice — run both: the tunnel carries what has to call in, the tailnet carries you.
- A tunnel alone closes the ports but leaves the UI answering anyone with the address. A tailnet alone makes the UI yours but breaks every inbound integration. Only together do you get both.
- Closing 80 and 443 afterwards is the step that makes it real.

## See Also

- [Public Access](public-access.md) — the Cloudflare Tunnel setup in full, and the ingress rules
- [Single-Server Deployment](single-server.md) — base server setup
- [Backup and Restore](backup-and-restore.md) — have this in place before the instance matters
- [Monitoring](monitoring.md) — the six health probes and resource thresholds
