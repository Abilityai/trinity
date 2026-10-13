# Deploy on AWS

Install Trinity on an EC2 instance of your own. One command, run as root on a fresh Ubuntu 24.04 instance, installs Docker, a web server with an HTTPS certificate for the instance's public IP, a host firewall, and Trinity itself from prebuilt images. You then prove the instance is yours by entering its EC2 instance ID when you create the admin account.

## When to Use This

- You want Trinity on a server of its own, and your infrastructure is on AWS.
- You can launch an EC2 instance and open a shell on it (SSH or EC2 Instance Connect).

Three ways onto EC2 run the same installer (`start.sh --provision --cloud aws`):

| Path | Status | What you do |
|---|---|---|
| **Script install** on your own instance | Available | The procedure on this page |
| **CloudFormation stack** (`packer/aws/trinity.cfn.yaml`) | Template in the repo; the public Trinity AMI it launches is not published yet | Create the stack with an AMI ID; it adds an Elastic IP and a security group for 80/443 |
| **AWS Marketplace AMI** | Listing pending | Subscribe, then launch |

All three end the same way: open `https://<public-ip>/`, enter the instance ID, set a password. For any other Linux server, see [Single-Server Deployment](single-server.md).

## Pre-flight

- [ ] **An x86_64 instance with 8 GiB of memory.** `t3a.large` is the size the CloudFormation template defaults to. The prebuilt images are published for `linux/amd64` only.
- [ ] **Ubuntu 24.04.** The installer adds Docker from Docker's Ubuntu repository.
- [ ] **A public IPv4 address** — auto-assigned by the subnet, or an Elastic IP. An Elastic IP also keeps the address across a stop/start.
- [ ] **A security group allowing TCP 80 and 443 from anywhere.** Port 80 carries the Let's Encrypt validation and a redirect. Keep 22 closed, or open it to your own address only. The instance needs no IAM role.
- [ ] **A Trinity release with AWS support.** v0.9.5 and earlier reject `--cloud aws`. Releases are listed on the [releases page](https://github.com/abilityai/trinity/releases).
- [ ] **A Claude subscription or an Anthropic API key**, to paste in the browser after the install.

## Procedure

### Step 1: Run the installer

On the fresh instance, as root:

```bash
git clone --depth 1 --branch <release-tag> https://github.com/abilityai/trinity.git /opt/trinity
cd /opt/trinity
./scripts/deploy/start.sh --provision --cloud aws --hosted --unattended
```

What it does, in order:

1. Reads the instance's public IP from the instance metadata service (IMDSv2).
2. Installs Docker, Caddy and the host firewall. The firewall allows 22, 80 and 443 on the host; the security group decides what actually reaches it.
3. Writes `/opt/trinity/.env` with `FRONTEND_PORT=8081` (Caddy owns 80 and 443), `FRONTEND_URL=https://<public-ip>` and `TRINITY_INSTALL_SOURCE=aws-script`.
4. With no `ADMIN_PASSWORD` in the environment or `.env`, writes this instance's ID to `trinity-data/setup-claim` and sets `ADMIN_PASSWORD_SOURCE=instance-id`. This is the **instance-ID claim** (Step 2).
5. Obtains a Let's Encrypt certificate for the IP address.
6. Installs `trinity-ip-refresh.timer`, which follows a changed public IP ([below](#when-the-public-ip-changes)).
7. Runs the normal hosted install: pulls the platform images and the agent base image, starts the stack on the bundled PostgreSQL, and waits for the backend to answer.

To skip the claim and set the admin password up front, pass it in the environment: `ADMIN_PASSWORD='your-password-here' ./scripts/deploy/start.sh --provision --cloud aws --hosted --unattended`. The admin is then `admin` with that password.

### Step 2: Claim the admin account

Open `https://<public-ip>/`. The certificate is a real Let's Encrypt certificate for the IP, so the browser shows no warning.

The **Create your admin account** form asks for one extra field: **EC2 instance ID**. Find it in the EC2 console under **Instances → Instance ID** (it starts with `i-`). Then:

- **Admin email** is optional here. Leave it blank to sign in as `admin`.
- **Password** needs 12+ characters with uppercase, lowercase, a digit and a special character.

A wrong instance ID is refused with *That instance ID does not match this server.* The claim file is consumed when the admin is created, so the form cannot be used twice. Someone who finds the IP before you cannot take the instance: they would also need to read the instance ID from your AWS account.

Creating the admin also deploys the built-in system agent (`trinity-system`) in the background. On a 2-vCPU instance it runs with 2 CPUs: Trinity caps every agent's CPU limit at the host's CPU count, because Docker refuses a larger one.

### Step 3: Connect Claude

The first-run setup opens on the Dashboard. **Connect Claude** is the one required step: paste a subscription token from `claude setup-token` or an Anthropic API key. Trinity checks it with Anthropic before saving it. Because the install recorded `aws-script`, the setup also offers **Secure this instance**: a domain, then a Cloudflare Tunnel. See [First-Time Setup](../../getting-started/setup.md) and [Hardening a Marketplace Install](hardening.md).

## Verify

| Check | How | Expected |
|---|---|---|
| The site serves Trinity | Open `https://<public-ip>/` | The setup form (or sign-in page), valid padlock |
| The certificate was issued | `cat /etc/trinity/tls-status` | `ok` |
| The recorded address | `cat /etc/trinity/public-ip` | The instance's current public IP |
| The refresh timer is active | `systemctl status trinity-ip-refresh.timer` | `active (waiting)` |
| The platform is healthy | `curl -s http://localhost:8000/health` | `{"status":"healthy",...}` |
| Provenance | **Settings → General → Build Info** | Install source **AWS (install script)** |

The full six-probe check is in [Monitoring](monitoring.md).

## When the Public IP Changes

EC2 gives an instance without an Elastic IP a new public IPv4 after a stop/start. `trinity-ip-refresh.timer` checks the metadata service a minute after boot and every five minutes after that. When the address changed, it:

- rewrites the Caddyfile and obtains a certificate for the new IP;
- rewrites `FRONTEND_URL`, but only if it still points at the old IP (a domain you set is kept);
- recreates the backend from the images already on the instance.

It never pulls or upgrades Trinity, and it logs only when something changed or failed. Allow a few minutes after a start before opening the new address. An Elastic IP avoids the change altogether.

**No public IPv4 at first boot.** The installer stops with *this EC2 instance has no public IPv4 address* after recording everything that needs no address and installing the refresh timer. Attach an Elastic IP or a public address. Within five minutes the timer finishes the setup and starts Trinity, with no shell needed.

## The CloudFormation Template

`packer/aws/trinity.cfn.yaml` creates:

- one instance from the Trinity AMI (`ImageId`), default `t3a.large` (choices: `t3a.large`, `t3.large`, `m7i-flex.large`, `t3a.xlarge`, `t3.xlarge`, `m7i-flex.xlarge`);
- a security group open on 80 and 443, plus SSH from `SshCidr` if you set one, with an optional key pair (`KeyName`);
- an Elastic IP;
- no IAM role.

The instance goes into a default subnet of the region's default VPC, or into `SubnetId` (set `VpcId` with it), and always gets a public IPv4 address. The stack outputs `TrinityUrl` and `InstanceId` — the value the setup form asks for. The Trinity AMI boots with no user data and always takes the instance-ID claim, ignoring any password in user data. The public AMI and the one-click Launch Stack link are not published yet; this page will carry them when they are.

## Security Notes

- **Agents cannot reach the metadata service.** The host firewall drops container traffic to `169.254.0.0/16`, so an agent cannot read user data or instance-role credentials.
- **Container ports are not reachable from off the instance.** The backend, MCP server and log collector answer only through Caddy on 80/443, or from the instance itself.
- **The refresh timer runs the checkout's `start.sh` as root** every five minutes. Keep `/opt/trinity` owned by root and writable by nobody else.

## Managing the Instance

Day-two commands are the same as on any hosted install, run from `/opt/trinity`:

```bash
cd /opt/trinity
docker compose -f docker-compose.hosted.yml ps     # status
./scripts/deploy/stop.sh                           # stop (never `down`)
./scripts/deploy/start.sh --hosted                 # start, and apply .env changes
```

To upgrade, pin the new release in `.env`, check out the matching tag, and re-run `start.sh --hosted` — see [Upgrading](upgrading.md). Automatic database backups land in `/opt/trinity/trinity-data/backups/`, on the instance's own disk; take EBS snapshots as well. See [Backup and Restore](backup-and-restore.md).

## See Also

- [Deploying Trinity](../deploying-trinity.md) — every install path, side by side
- [Single-Server Deployment](single-server.md) — the hosted install, `.env` reference and database backend
- [Hardening a Marketplace Install](hardening.md) — a domain, then a tunnel, a private network, or both
- [Monitoring](monitoring.md) — health probes and recovery patterns
- [Ops Agent](ops-agent.md) — day-to-day operations from Claude Code
