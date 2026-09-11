# Trinity — Marketplace listing copy

Source text for the DigitalOcean Vendor Portal listing. Rule 11 of DigitalOcean's
1-Click build standard asks for this in-repo, so the catalog page and the image
are versioned together rather than the copy living only in the portal.

---

## Trinity — sovereign infrastructure for autonomous AI agents

Trinity deploys, orchestrates and governs fleets of autonomous AI agents on
hardware you own. Every agent runs in its own Docker container with a
standardized interface for credentials, tools and MCP servers — so an agent can
hold real secrets, reach real systems, and keep running on a schedule without
you handing your data to anyone else's cloud.

This 1-Click brings up a complete Trinity instance on your Droplet, served over
browser-trusted HTTPS, with no configuration and no domain required.

## Key features

- **Agents as containers** — each agent is isolated, with its own workspace,
  credentials and lifecycle. Create, pause, clone and delete from the browser.
- **Real work, unattended** — agents run on schedules, react to webhooks, and
  pick up work from a queue. Failures retry and escalate rather than vanishing.
- **Talk to your fleet** — a chat workspace per agent, with live execution
  visibility, shared files and a canvas the agent can draw results on.
- **Connected by default** — Slack, Telegram, WhatsApp and voice channels, plus
  an MCP server exposing your fleet to Claude Code and other MCP clients.
- **Credentials handled properly** — injected into containers at runtime,
  encrypted at rest, masked in every log.
- **Open source, Apache 2.0** — self-hosted, inspectable, and yours. No account
  with us is required to run it.

## System requirements

Trinity runs the platform plus one container per agent, so memory scales with
fleet size rather than with traffic.

| Use case | RAM | vCPU | Boot disk |
|---|---|---|---|
| Minimum — platform plus 1–2 agents | 4 GB | 2 | 50 GB |
| **Recommended** — a working fleet | **8 GB** | **4** | **80 GB** |
| Larger fleets | 16 GB+ | 8+ | 160 GB+ |

The images baked into this Droplet occupy a significant share of the disk before
any agent exists; agent workspaces grow from there. Disk can be increased on a
running Droplet, never decreased.

## Included system components

- **Ubuntu 24.04 LTS**
- **Trinity** — the version baked into this image, shown at login
- **Docker Engine** with the Compose plugin — the agent runtime
- **Caddy** — reverse proxy on 80/443, automatic Let's Encrypt certificate for
  the Droplet's own IP address
- **Redis** — transient secrets and the platform event bus
- **Vector** — log aggregation across every container
- **OpenTelemetry Collector** — agent metrics
- **UFW**, plus `DOCKER-USER` firewall rules so no container port is reachable
  from the internet

## Getting started

### 1. Create the Droplet

Choose a plan of at least 4 GB RAM (8 GB recommended) and create it. First boot
takes about ninety seconds: it obtains a certificate for the Droplet's IP and
starts Trinity.

### 2. Open it and create your admin account

Open `https://<your-droplet-ip>` in your browser. There is no password to look
up and no terminal to open: Trinity asks you to create the admin account — your
email, a password, and whether you want product updates — and you are in.

**Do this as soon as the Droplet is up.** Until an admin account exists, whoever
opens the address first creates it. The Droplet holds nothing at that point, so
if a Droplet you have never opened shows you a login page instead, destroy it and
create another. To close the window entirely, restrict port 443 to your own IP
with a cloud firewall until you have signed in (leave port 80 open — the
certificate is validated over it), or choose the password up front by pasting
this into **Additional Options → Startup scripts** when creating the Droplet:

```yaml
#cloud-config
write_files:
  - path: /etc/trinity/admin-password
    permissions: '0600'
    content: "your-password-here"
```

It must be `#cloud-config` with `write_files`, not a shell script — a shell
script runs too late in cloud-init to be seen. A Droplet created this way skips
the create-your-account screen: sign in as `admin` with that password.

### 3. Sign in

From then on, open `https://<your-droplet-ip>` and sign in with the account you
created. The certificate is a real Let's Encrypt certificate issued for the IP
address, so there is no browser warning and nothing to accept.

### 4. Add a model credential and create your first agent

Trinity ships no model credentials — you supply your own. Add one in the
browser, then create an agent from a template. The first-run guide walks through
attaching a domain and serving it through a Cloudflare Tunnel when you are ready
to move off the bare IP.

## Managing Trinity

Over SSH, or in the Droplet Console:

```bash
cd /opt/trinity

# status
docker compose -f docker-compose.hosted.yml ps

# stop / start / restart
./scripts/deploy/stop.sh
./scripts/deploy/start.sh --hosted
docker compose -f docker-compose.hosted.yml restart

# logs
docker compose -f docker-compose.hosted.yml logs -f backend
```

**Updating.** Pull the release you want and restart:

```bash
cd /opt/trinity
sudo git fetch --tags && sudo git checkout <tag>
sudo ./scripts/deploy/start.sh --hosted
```

Database backups run nightly and before every migration, under
`~/trinity-data/backups/`. They are on the same disk as the database, so they
protect against corruption and mistakes, not against losing the Droplet — take
Droplet snapshots as well.

## Support

- **Issues and questions**: https://github.com/abilityai/trinity/issues — please
  use the `do-marketplace` label
- **Documentation**: https://docs.ability.ai

**DigitalOcean does not build or support Trinity.** Support is provided by
Ability AI through the channels above.
