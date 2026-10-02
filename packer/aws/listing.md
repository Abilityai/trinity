# Trinity — AWS Marketplace listing copy

Source text for the AWS Marketplace product page (single AMI, Free, Launch from
Website), versioned with the image. Logo: `../digitalocean/logo.png`.

---

## Product title

Trinity — sovereign infrastructure for autonomous AI agents

## Short description

Deploy, orchestrate and govern fleets of autonomous AI agents on an EC2 instance
you own. Each agent runs in its own container with managed credentials, tools and
MCP servers. Served over HTTPS on the instance's own IP, no domain required.

## Long description

Trinity deploys, orchestrates and governs fleets of autonomous AI agents on
hardware you control. Every agent runs in its own Docker container with a
standardized interface for credentials, tools and MCP servers, so an agent can
hold real secrets, reach real systems and keep running on a schedule without
your data leaving your AWS account.

This AMI brings up a complete Trinity instance with no configuration: first boot
obtains a Let's Encrypt certificate for the instance's public IP and starts
Trinity. You create the admin account in the browser after confirming the
instance's ID, so nobody else who finds the address can.

## Highlights

- **Agents as containers** — isolated workspaces, credentials and lifecycle;
  create, pause, clone and delete from the browser.
- **Unattended work** — schedules, webhooks and a work queue; failures retry and
  escalate.
- **Open source, Apache 2.0** — self-hosted and inspectable. No account with us
  is required.

## Pricing

Free software. You pay AWS for the EC2 instance, its EBS volume and its public
IPv4 address. Recommended: `t3a.large` (2 vCPU, 8 GiB).

## Recommended security group (version metadata)

| Protocol | Port | Source | Why |
|---|---|---|---|
| TCP | 443 | 0.0.0.0/0 | The web interface and API over HTTPS |
| TCP | 80 | 0.0.0.0/0 | Certificate validation (ACME http-01) and the redirect to HTTPS |

SSH (22) is not needed and is not opened. Add it from your own address only if
you want a shell.

## Usage instructions

1. Launch the instance with a public IPv4 address and the security group above.
   First boot takes a few minutes.
2. In the EC2 console, open **Instances** and copy this instance's **Instance ID**
   (it starts with `i-`) and its **Public IPv4 address**.
3. Open `https://<public IPv4 address>/`. Enter the instance ID when asked, then
   choose an admin password. An email address is optional; without one, sign in
   as `admin` with that password.
4. Add a model credential in the browser and create your first agent. The
   first-run guide walks through attaching a domain and a Cloudflare Tunnel when
   you are ready to move off the bare IP.

**Stopping and starting.** Without an Elastic IP, AWS gives the instance a new
public address after a stop/start. Trinity picks it up and obtains a new
certificate within about five minutes; open the new address after that. An
Elastic IP keeps the address fixed.

**Managing over SSH** (optional): `cd /opt/trinity`, then
`docker compose -f docker-compose.hosted.yml ps` / `logs -f backend`, and
`sudo ./scripts/deploy/start.sh --hosted` to restart. The database is a bundled
PostgreSQL container (`trinity-postgres`). Backups run nightly under
`/opt/trinity/trinity-data/backups/` on the same volume; take EBS snapshots as
well.

## Endpoint URL

`https://<public-ipv4>/`

## Operating system

Ubuntu 24.04 LTS (x86_64). Username for scanning: `ubuntu`.

## Included software

Trinity (the release shown at login), Docker Engine with Compose, Caddy (reverse
proxy with automatic Let's Encrypt certificates), Redis, Vector, OpenTelemetry
Collector, UFW plus `DOCKER-USER` rules closing every container port to the
internet. Every container image is baked into the AMI; first boot downloads
nothing but the certificate.

## Support

- **Issues and questions**: https://github.com/abilityai/trinity/issues — please
  use the `aws-marketplace` label
- **Documentation**: https://docs.ability.ai

**AWS does not build or support Trinity.** Support is provided by Ability AI
through the channels above.
