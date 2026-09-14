# First-Time Setup

Install Trinity, log in as the admin, and meet the starter fleet a fresh install comes with.

> 📺 **Watch:** [I Built a DevOps Agent That Deploys Other Agents](https://youtu.be/8RozanPd14Y) *(Apr 2026)* · [all videos](../videos.md)

## Concepts

- **Admin Account** -- The primary account with full platform access. It is created from `ADMIN_PASSWORD` in `.env` when the backend first starts; the installer (`start.sh`) requires that value, so on a normal install the account exists before you open the browser. `.env` stays the source of truth: every backend start re-applies `ADMIN_PASSWORD`, so to change the admin password edit that line and restart the backend (there is no change-password form in the UI). The admin signs in with the username `admin` (or `ADMIN_USERNAME`) **or** a registered email address, plus the password.
- **First-run form** -- A one-page "Create your admin account" screen that appears **only on an install with no admin account** — a blank `ADMIN_PASSWORD` brought up without the installer, or a hand-rolled backend. It refuses to run once a usable admin exists, whatever the setup flag says.
- **Email Login** -- A passwordless authentication method where users receive a one-time code via email. Requires an email service to be configured.
- **Starter fleet** -- The agents a fresh install seeds for you: the Cornelius second-brain agent plus a three-agent starter team. Nothing to configure; deleting them does not bring them back.

## How It Works

![Trinity deployment topology — one host, VPN-private access, tunnel-published public endpoints, agents isolated from the data plane](../../assets/trinity-deployment-topology.webp)

*The recommended production shape: everything on one host as Docker containers, private access over your VPN (Tailscale recommended), public endpoints published only through an outbound tunnel, and agents on a separate network from the data plane. A local install is the same containers with ports exposed directly.*

### Prerequisites

- Docker Desktop installed and running (or Docker Engine + the Compose v2 plugin on Linux)
- Git (required for GitHub-based agent templates)
- A modern web browser

### Installation

1. Clone the repository:

   ```bash
   git clone https://github.com/abilityai/trinity.git
   cd trinity
   ```

2. Set the admin password:

   ```bash
   cp .env.example .env
   # Set ADMIN_PASSWORD to a strong password (12+ characters)
   ```

   `start.sh` refuses to start while `ADMIN_PASSWORD` is blank. It generates every other secret (`SECRET_KEY`, `INTERNAL_API_SECRET`, `CREDENTIAL_ENCRYPTION_KEY`, `AGENT_AUTH_SECRET`, the Redis passwords) and detects `DOCKER_GID` on first run, writing them back to `.env`.

3. Start all services:

   ```bash
   ./scripts/deploy/start.sh
   ```

   On first run this builds the base agent image if it is missing (5–10 minutes), starts the backend, frontend, MCP server, Redis, scheduler, Vector and OTel collector, waits for the backend to report healthy, and prints the access URLs with a next-steps card. `./quickstart.sh` is an alias for the same script.

   > **No-prompt install:** run `./scripts/deploy/start.sh --unattended` (or set `TRINITY_UNATTENDED=1`) and the script never stops to ask for input — it generates the `admin` password and prints it in the final summary. Save it: it lands in `.env` and is shown only once. An AI coding agent (Claude Code) can drive the entire local install for you via the runbook at [`docs/AGENT_INSTALL_GUIDE.md`](../../AGENT_INSTALL_GUIDE.md). Server installs, prebuilt images and the DigitalOcean 1-Click: [Deploying Trinity](../guides/deploying-trinity.md).

4. Open http://localhost in your browser and log in.

### The first-run form (installs with no admin only)

If the backend started with a **blank** `ADMIN_PASSWORD` — which the installer does not allow, so this means a bare `docker compose up -d` or a hand-rolled backend — every page redirects to a one-page **"Create your admin account"** form until an admin exists: enter your **admin email** (required — it becomes your sign-in identity), a password (12+ characters with uppercase, lowercase, number, and special character; a live checklist guides you), confirm it, and optionally your company name and an opt-in to security and product update emails. The form disables itself permanently after the account is created, and the backend refuses it outright whenever a usable admin already exists — so an install that booted with `ADMIN_PASSWORD` set is never in this window.

> **Security note:** on an install with no admin, the form is reachable without authentication until you use it. Keep such an instance behind a tunnel, VPN, or firewall until the admin account exists. Setting `ADMIN_PASSWORD` before first boot closes the window entirely.

### Logging In

**Admin login:** Enter username `admin` **or the admin's registered email**, plus the password.

**Email login (passwordless):** Enter your email address, receive a 6-digit verification code, and submit it to log in. This requires email service configuration. The admin manages allowed email addresses under **Settings → Access → Email Whitelist**.

Password login is rate-limited: five failed attempts on one account within 15 minutes lock that account out until the window passes (a looser per-address limit — 30 failures in 5 minutes — guards shared networks without locking out everyone behind one address).

### Your First Dashboard

The Dashboard shows at most one first-run card at a time, highest priority first, and each is dismissible:

- **Secure this instance** -- Only on an install provisioned from a marketplace image (the DigitalOcean 1-Click), for admins, until a domain is configured. See [Single Server → DigitalOcean 1-Click](../guides/deploying/single-server.md#digitalocean-marketplace-1-click).
- **Start here** -- Shown while every agent you can see is one Trinity seeded, i.e. before you have made the install yours. Three doors: **Show me** opens the Cornelius chat so you can watch a seeded agent work; **Make me one** opens the one-question onboarding wizard (see [Quick Start](quick-start.md#guided-onboarding-first-run)); *Already run a fleet? Bring it over →* links to the docs for migrating an existing fleet. The card stands down for good once you create an agent of your own.
- **Getting started** -- A checklist that ticks off your first milestones and hides itself when the last one is done. It appears only on instances with the matching enterprise entitlement.
- **Finish setup** -- Admin-only post-login asks the first-run form no longer carries: **Add a sign-in email** (so you can log in with email + password; also available at **Settings → General → Admin sign-in email**) and the anonymous usage-sharing consent, whose **Not now** snoozes that ask for two weeks per browser (see [Telemetry](../operations/telemetry.md)).

### Your Starter Fleet

On a **fresh install**, Trinity seeds agents so you land on something working without cloning or configuring a template:

- **Cornelius** -- a ready-to-use second-brain agent, cloned from its public template at first boot, with the **Brain Orb** enabled — a self-rendering 3D knowledge graph on its **Brain** tab (see [Dynamic Dashboards → the Brain Orb](../advanced/dynamic-dashboards.md#related-the-brain-orb)).
- **The `acme` starter team** -- three collaborating agents from the bundled default system manifest: `acme-scout` (research), `acme-sage` (strategy) and `acme-scribe` (content), sharing folders and able to call each other. They ship with no schedules and need no credentials at seed time. Skip or replace this fleet with `TRINITY_DEFAULT_SYSTEM_MANIFEST` — see [System Manifests → Default System on First Run](../collaboration/system-manifest.md#default-system-on-first-run).

Seeding runs **once, only on a truly fresh install**: it is skipped when the instance already has agents, and deleting a seeded agent does **not** re-create it. Installs without Docker (demo mode) skip it entirely. The seeded agents cannot think until you add a model credential — an Anthropic API key or a Claude subscription under **Settings → Integrations**.

### Security & product updates (optional)

**Settings → General → Security & product updates** lets the admin opt in to occasional security and product-update emails from the Trinity team: **Email** (required), **Company**, **Name**, **Role** and **Primary use case** (optional), an **Exactly what would be sent** preview, and **Opt in & submit**. It sends exactly that, at most once per install, and nothing leaves the box unless you submit. Air-gapped or privacy-strict installs disable it entirely with `OPERATOR_INTAKE_ENABLED=false` or `DO_NOT_TRACK=1`. The separate, anonymous **Usage sharing** consent is described in [Telemetry](../operations/telemetry.md).

### Key URLs

| Service | URL |
|---------|-----|
| Web UI | http://localhost |
| Backend API docs | http://localhost:8000/docs |
| MCP Server | http://localhost:8080/mcp (on a server install also `https://trinity.your-domain.com/mcp`) |

### Stopping and Starting

```bash
# Stop all services (containers and the agent network stay in place)
./scripts/deploy/stop.sh

# Start all services
./scripts/deploy/start.sh

# Rebuild services after code changes
docker compose build --no-cache backend frontend mcp-server scheduler

# View backend logs
docker compose logs -f backend
```

Never `docker compose down` an instance you want to keep — see [Deploying Trinity → Managing Services](../guides/deploying-trinity.md#managing-services-self-hosted).

### Settings

Settings is tabbed — **General**, **Access**, **Integrations**, **MCP Keys**, **Agents**, **Retention**, plus tabs that appear only when the corresponding capability is enabled. The tab-by-tab guide is in [Using Trinity → Settings](../guides/using-trinity.md#settings). The GitHub personal access token that lets agents pull and push repositories is covered in [GitHub PAT Setup](../integrations/github-pat-setup.md).

## For Agents

### Authentication Endpoint

```
POST /api/token
Content-Type: application/x-www-form-urlencoded

username=admin&password=YOUR_PASSWORD
```

Returns:

```json
{
  "access_token": "eyJ...",
  "token_type": "bearer"
}
```

### Using the Token

Include the token in the `Authorization` header for all authenticated requests:

```bash
curl -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  http://localhost:8000/api/agents
```

### Token Details

- JWT tokens are valid for 7 days.
- Tokens are invalidated when the backend restarts. Re-login is required.
- MCP API keys (prefixed `trinity_mcp_`) also work as Bearer tokens.

### Unauthenticated Endpoints

The following endpoints do not require authentication:

- `GET /api/auth/mode` -- Returns the current authentication mode and whether setup is complete.
- `GET /api/setup/status` -- Returns whether initial setup is complete.
- `POST /api/setup/admin-password` -- The first-run form's endpoint; refuses (403) once a usable admin exists.
- `POST /api/token` -- The login endpoint itself.

### First-run state

- `GET /api/onboarding/first-run` -- Whether the caller still sees a seed-only install, the seeded agent names, and which agent the **Show me** door opens.
- `GET /api/settings/feature-flags` -- Includes `install_source`, `marketplace_install` and `install_tls_posture` (what the instance advertises: `unconfigured`, `http`, `https-ip`, `https-domain`).
- `GET /api/version` -- Also reports `install_source`.

## Limitations

- Backend restarts invalidate all active JWT tokens. All users and integrations must re-authenticate.
- Email login requires a configured email service. Without it, only admin password login is available.
- Trinity requires Docker. It cannot run without it.
- Seeding Cornelius needs outbound access to GitHub at first boot; a failed pass is retried on the next start.

## See Also

- [Quick Start](quick-start.md) -- Create your first agent, or let the onboarding wizard do it.
- [Deploying Trinity](../guides/deploying-trinity.md) -- Local, server, prebuilt-image and DigitalOcean 1-Click installs.
- [Overview](overview.md) -- Platform overview and core concepts.
- [Creating Agents](../agents/creating-agents.md) -- Deploy your first agent.
