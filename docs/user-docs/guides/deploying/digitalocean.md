# Deploy on DigitalOcean

Install Trinity on a DigitalOcean Droplet from your own terminal in about ten minutes, most of it waiting. You run one command on your computer. You end up with Trinity on its own server, behind HTTPS, with your admin password already set. You connect Claude in the browser when you first sign in.

## When to Use This

- You want Trinity on a server of its own, and you have a DigitalOcean account or can create one.
- You want to choose the admin password yourself, before the server exists. Nobody else can claim the instance, because the admin account exists from first boot.

The installer does not use the Marketplace image. It creates a stock Ubuntu Droplet and installs Trinity on first boot, so it works whether or not the Marketplace 1-Click is available to you. For the 1-Click itself, or for any other Linux server, see [Deploying Trinity](../deploying-trinity.md).

## Pre-flight

- [ ] **A [DigitalOcean](https://cloud.digitalocean.com/) account.**
- [ ] **A Claude subscription (Pro or Max) or an Anthropic API key.** Trinity's agents sign in to Claude with it. You add it in the browser after the install, not in the installer.
- [ ] **On Windows, [WSL](https://learn.microsoft.com/en-us/windows/wsl/install).** Run everything below inside it.
- [ ] **About $48/month** for the server (4 vCPU, 8 GB RAM), billed by DigitalOcean until you delete it.
- [ ] **Optional: SSH keys on your DigitalOcean account.** The installer attaches every key already there, so you can `ssh root@<droplet-ip>` later. Without one, use the Droplet's browser **Console** for shell access.

## Procedure

### Step 1: Install DigitalOcean's command-line tool

On macOS:

```bash
brew install doctl
```

On Linux or WSL, follow DigitalOcean's [install guide](https://docs.digitalocean.com/reference/doctl/how-to/install/).

### Step 2: Give it access to your account

Open [API Tokens](https://cloud.digitalocean.com/account/api/tokens) in DigitalOcean and click **Generate New Token**. Name it, tick **Write**, and copy the token. It is shown only once. Then run this and paste the token when asked:

```bash
doctl auth init
```

Confirm it worked:

```bash
doctl account get
# Expected: a row with your account's email and status "active"
```

### Step 3: Decide on a password

You sign in to Trinity with this password, and your username is `admin`. The installer requires at least 12 characters. It also rejects passwords that start with `password`, `admin`, `trinity`, `changeme` or `letmein`, in any letter case.

Trinity's own password rules also ask for upper- and lowercase letters, a digit and a symbol. The installer does not check those, so check them yourself: this password guards a server on the public internet.

### Step 4: Run the installer

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/abilityai/trinity/<release-tag>/scripts/deploy/trinity-do-create.sh)
```

Replace `<release-tag>` with the Trinity release you want, for example `v0.9.5`. Releases are listed on the [releases page](https://github.com/abilityai/trinity/releases). The script installs the release it was fetched from. The installer from `v0.9.5` and earlier also asks for a Claude subscription token; paste one from `claude setup-token`.

It asks three questions. The password is not shown as you type it.

1. **A password**, twice: the one from Step 3. It then prints `Your username will be: admin`.
2. **A region.** The prompt suggests `fra1` (Frankfurt, the default), `nyc3`, `sfo3`, `lon1` and `sgp1`.
3. **A name** for the Droplet in your DigitalOcean account. The default is `trinity`.

It then shows what it is about to create and asks before creating anything:

```
  About to create:  trinity  (s-4vcpu-8gb, fra1)
  Trinity release:  <release-tag>
  This costs about $48/month until you destroy it.

  Create it? [y/N]:
```

Type `y`. The installer creates the Droplet, prints `Server is up at <droplet-ip>. Installing Trinity — about five more minutes.`, and prints a dot every 15 seconds while it waits. The whole run takes about six minutes. Expected output at the end:

```
  Trinity is ready.

      Open:     https://<droplet-ip>
      Username: admin
      Password: the one you chose above

  After you sign in, Trinity asks you to connect Claude.
```

### Step 5: Open the address it prints

Sign in as `admin` with your password. The page is served over HTTPS with a real Let's Encrypt certificate for the IP address, so the browser shows no warning. The server's internal ports are closed to the internet.

Trinity then opens its first-run setup. The **Set up this instance** screen lists six steps: **Secure this instance** (marked **Recommended**), **Sign-in email**, **Connect Claude**, **Other keys**, **Your first agent** and **Usage sharing**.

**Connect Claude** is the one required step: until it is done, no agent can run. Paste either a subscription token or an API key. For a token, run `claude setup-token` on any computer where [Claude Code](https://code.claude.com/docs/en/setup) is signed in to your plan, and copy the `sk-ant-oat01-...` value it prints. For an API key, create one at [console.anthropic.com](https://console.anthropic.com/settings/keys). Trinity checks the credential with Anthropic before it saves it, then hands it to the starter agents. See [Platform Keys → Claude](../../credentials/platform-keys.md#claude).

**Secure this instance** is also worth doing now (Step 6). The rest is optional, and **Finish later** closes the sequence. See [First-Time Setup](../../getting-started/setup.md#your-first-dashboard).

### Step 6 (optional): Put a domain in front of it

The **Secure this instance** step suggests this. An IP address works, but it is awkward to share. Its certificate also lasts only about six days before it is renewed. Renewal happens automatically while the server runs. A Droplet switched off for longer comes back to a browser warning until renewal catches up.

1. **Point your domain's `A` record at the Droplet's IP address.** If the domain is on Cloudflare, set the record to **DNS only** (grey cloud). Confirm it resolves:

   ```bash
   dig +short your-domain.com
   # Expected: the Droplet's IP address
   ```

2. **Save the domain in Trinity.** In the **Secure this instance** step, enter `https://your-domain.com` in **Public URL** and click **Save domain**. Later, the same field is under **Settings → General → Public URL** (click **Save**). Include `https://`. Saving immediately re-points any Telegram and WhatsApp webhooks to the new address, which is why DNS comes first.
3. **Open `https://your-domain.com` in a new tab.** The first load takes a few seconds while the certificate is obtained. After that it loads normally.

Until that first visit, Trinity cannot tell whether the name works. The step's badge reads **Domain saved**, and Settings reads *saved, waiting for the first visit to confirm it resolves here*. After the visit, the badge reads **Domain reached**, and Settings shows the address with a tick.

The Droplet still answers anyone who finds its address. To take it off the public internet, serve the domain through a Cloudflare Tunnel, reach it over a private network, or both. A tunnel alone keeps the web UI answering anyone with the address. [Hardening a Marketplace Install](hardening.md) walks through all three choices, and [Public Access](public-access.md) covers the Cloudflare side.

## Verify

The first three checks run in your browser. The rest run on the Droplet: open a shell with `ssh root@<droplet-ip>`, or use the Droplet's **Console** in the DigitalOcean control panel.

| Check | How | Expected |
|---|---|---|
| The site serves Trinity | Open `https://<droplet-ip>` | The sign-in page, valid padlock, no warning |
| You can sign in | `admin` and your password | The Dashboard with the starter fleet, and first-run setup open over it |
| Claude is connected | **Settings → Integrations**, after **Connect Claude** | A subscription named `primary` under **Subscriptions**, or the API key field filled in |
| The install finished | `tail -20 /var/log/trinity-install.log` | The last line is `=== Trinity is ready at https://<droplet-ip> ===`. The first-boot script stops at the first failing command, so this line is written only after the install succeeded |
| The certificate was issued | `cat /etc/trinity/tls-status` | `ok` |
| The platform is healthy | `curl -s http://localhost:8000/health` | `{"status":"healthy",...}` |

## If the Installer Stops

The installer checks for `doctl` before it asks anything, and it checks each answer as you give it. Its messages mean:

| It says | What to do |
|---|---|
| `doctl is not installed` | Do [Step 1](#step-1-install-digitaloceans-command-line-tool), then run the installer again. |
| `doctl is installed but not signed in to DigitalOcean` | Do [Step 2](#step-2-give-it-access-to-your-account). The token needs **Write** ticked. |
| `doctl is installed as a snap but $HOME is not set` | Run the installer from a normal login shell, or install `doctl` from a package instead of snap. |
| `Too short` | The installer asks again. Use 12 characters or more. |
| `Too guessable` | The installer asks again. Pick a password that does not start with one of the words in [Step 3](#step-3-decide-on-a-password). |
| `Those did not match.` | The installer asks again. Type the same password twice. |
| `Nothing was created.` | You answered something other than `y` at the cost prompt. Nothing was billed. Run the installer again. |
| `The droplet was created but has no public IP yet` | The Droplet exists and is billing, and it still installs Trinity by itself. Find its IP at [cloud.digitalocean.com/droplets](https://cloud.digitalocean.com/droplets) and open `https://<droplet-ip>` after a few minutes. |
| `Trinity did not finish within 15 minutes` | It may still be working, so open the address in a browser first. If it does not answer, open the Droplet from [cloud.digitalocean.com/droplets](https://cloud.digitalocean.com/droplets), click **Console** (a terminal in your browser, no SSH key needed) and run `tail -50 /var/log/trinity-install.log`. |

If the install log shows `TLS: no valid certificate`, Caddy could not obtain the certificate for the IP address. Trinity still starts, but the installer keeps waiting for HTTPS. Read `journalctl -u caddy -n 100` on the Droplet, and check that no cloud firewall blocks ports 80 and 443. A Droplet that never came up has nothing to keep: delete it (below) and run the installer again.

## Removing It

```bash
doctl compute droplet delete <name>
```

Use the name you gave the server in Step 4 (`trinity` if you kept the default). `doctl` asks you to confirm. The server costs about $48/month until you delete it.

Deleting the Droplet also deletes everything on it, including your agents and Trinity's own backups, which live on the same disk. To keep anything, take a Droplet snapshot first. See [Backup and Restore](backup-and-restore.md).

## What the Installer Does

- **Runs on your computer.** It keeps your password off your screen and out of your shell history. The password travels only in the Droplet's own setup data (user-data), and Trinity's container firewall stops agents from reading that back. The temporary file that carries it is readable only by you and is deleted when the installer exits.
- **Creates an Ubuntu 24.04 Droplet** of size `s-4vcpu-8gb` in the region you chose. It attaches every SSH key already on your DigitalOcean account.
- **Hands the Droplet a first-boot script.** The script clones the chosen release to `/opt/trinity` and runs `./scripts/deploy/start.sh --provision --cloud digitalocean --hosted --unattended`. That installs Docker, Caddy with a Let's Encrypt certificate for the IP address, and the host firewall, then installs Trinity from prebuilt images. The Marketplace 1-Click uses the same installer.
- **Writes `/opt/trinity/.env`.** Your password goes in as `ADMIN_PASSWORD`, so the admin account exists at first boot. The file also gets `FRONTEND_PORT=8081` (Caddy owns ports 80 and 443 and forwards to the web UI), `FRONTEND_URL=https://<droplet-ip>` and `TRINITY_IMAGE_TAG=<release-tag>`. Finally, it records `TRINITY_INSTALL_SOURCE=do-script`, the marker that makes the **Secure this instance** step appear.
- **Leaves Claude to the first sign-in.** It asks for no Claude credential. The **Connect Claude** step checks yours with Anthropic before saving it, which the installer could not do before the server existed.
- **Waits for `https://<droplet-ip>/`** to answer with a valid certificate, for up to 15 minutes. The Droplet keeps its install log at `/var/log/trinity-install.log`.

To install a different release, fetch the script from that release's tag. Alternatively, set `TRINITY_IMAGE_TAG` when you run it, for example `TRINITY_IMAGE_TAG=<release-tag> bash <(curl -fsSL ...)`. The value must be a published release tag, because the Droplet uses it both to check out the code and to pull the images. [Read the script on GitHub](https://github.com/abilityai/trinity/blob/main/scripts/deploy/trinity-do-create.sh).

Unlike the 1-Click image, a Droplet created this way has no Trinity login banner.

## Managing the Droplet

Status, logs, restarts, password changes, upgrades and backups work as they do on the 1-Click. See [Single Server → Managing the Droplet](single-server.md#managing-the-droplet). The everyday commands run from `/opt/trinity`:

```bash
cd /opt/trinity
docker compose -f docker-compose.hosted.yml ps     # status
./scripts/deploy/stop.sh                           # stop (never `down`)
./scripts/deploy/start.sh --hosted                 # start, and apply .env changes
```

To upgrade, pin the new release in `.env`, check out the matching tag, and re-run `start.sh --hosted`. See [Upgrading](upgrading.md).

To operate the Droplet from Claude Code, point the [Ops Agent](ops-agent.md) at it with these values in the ops agent's `.env`:

```bash
SSH_HOST=<droplet-ip>
SSH_USER=root
TRINITY_PATH=/opt/trinity
COMPOSE_FILE=docker-compose.hosted.yml
FRONTEND_PORT=8081
```

## Next Steps

- [First-Time Setup](../../getting-started/setup.md) — the first-run steps and the Dashboard
- [Building Agents](../building-agents.md) — create new agents, or onboard existing ones with the abilities plugins for Claude Code
- [Hardening a Marketplace Install](hardening.md) — a domain, then a tunnel, a private network, or both
- [Upgrading](upgrading.md) — move the server to a newer release

## See Also

- [Deploying Trinity](../deploying-trinity.md) — every install path, side by side
- [Single-Server Deployment → DigitalOcean installer script](single-server.md#digitalocean-installer-script) — the reference summary, beside the 1-Click
- [Public Access](public-access.md) — Cloudflare Tunnel setup and ingress rules
- [Subscription Credentials](../../credentials/subscription-credentials.md) — how the Claude subscription is shared across agents
- [Ops Agent](ops-agent.md) — day-to-day operations from Claude Code
