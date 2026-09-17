# Trinity FAQ — Getting Started

> Part of the [Trinity FAQ](README.md). Short, grounded answers with links to the full documentation.

## What is Trinity?

Trinity is an open-source autonomous agent orchestration platform — infrastructure for deploying, orchestrating, and governing fleets of AI agents on your own hardware. Each agent runs in an isolated Docker container with a pluggable runtime (Claude Code, OpenAI Codex, or Gemini CLI), persists memory across sessions, can delegate to other agents, and can run on schedules without human intervention. You interact with the platform through a web UI, a REST API, or MCP tools. See [Overview](../getting-started/overview.md).

## What do I need to run Trinity on my own machine?

You need Docker Desktop (or Docker with Docker Compose), Git, at least 8GB of RAM, and a modern web browser. Trinity cannot run without Docker — all platform services and every agent run as containers. See [Deploying Trinity](../guides/deploying-trinity.md).

## How do I install Trinity?

Clone the repository from GitHub, copy `.env.example` to `.env`, set `ADMIN_PASSWORD` (the one required edit — the installer refuses to start while it is blank), and run `./scripts/deploy/start.sh` (`./quickstart.sh` is an alias for the same script). The script generates every other secret, starts the backend, frontend, MCP server, Redis, scheduler, and log aggregator, builds the base agent image automatically if it's missing (5–10 minutes on first run), and prints the access URLs once the backend reports healthy. Then open http://localhost and log in as `admin`. For a no-prompt install add `--unattended` (it generates and prints the admin password); on a server, `--hosted` pulls prebuilt images instead of building. See [Setup](../getting-started/setup.md).

## Can I use Trinity without hosting it myself?

Yes. Ability.ai offers a cloud-hosted option where you sign up, copy an MCP connection URL from Settings, and connect from Claude Code — no infrastructure to manage. Self-hosting is free forever and keeps all data inside your own perimeter, which suits teams with compliance requirements. See [Deploying Trinity](../guides/deploying-trinity.md).

## What happens the first time I open Trinity in my browser?

On a normal install there is no setup screen: `ADMIN_PASSWORD` in `.env` provisions the admin account at boot, so you go straight to the login page and sign in as `admin`. The one-page "Create your admin account" form at `/setup` (admin email, a 12+ character password with mixed case, a number, and a special character, an optional company name, and an opt-in to security and product-update emails) appears **only on an install with no admin account** — a blank `ADMIN_PASSWORD` brought up without the installer, a hand-rolled backend, or a DigitalOcean Marketplace 1-Click droplet, which ships this way on purpose so you can create its admin in the browser with no terminal (supply a password when you create the droplet and no form appears) — and the backend refuses it outright once a usable admin exists. If you are in that window, the form is reachable without authentication until you use it, so keep such an instance behind a tunnel, VPN, or firewall until the account exists, or claim a droplet as soon as it is up; the [Deployment FAQ](deployment-and-upgrades.md) covers the claim in detail. After login you land on the Dashboard with the starter fleet, and on a fresh install the first-run setup opens over it. See [Setup](../getting-started/setup.md).

## What is the panel that opens over my Dashboard after I first log in?

That is **first-run setup**: one guided sequence over a Dashboard that does not move underneath it, with a rail on the left listing only the steps your install actually needs, in a fixed order you can step back through. The steps are **Secure this instance** (admins, only on an install provisioned onto a cloud VM by the DigitalOcean 1-Click or installer script: set a real domain, then optionally a Cloudflare Tunnel), **Sign-in email** (only while the admin account has no email — an install claimed in the browser already collected it), **Connect Claude** (the one required step: paste a Claude subscription token or an Anthropic API key; it is checked with Anthropic before it is saved, and the first credential is handed to every agent that had none), **Other keys** (optional: a GitHub token, an email-provider key for sign-in codes, a Gemini key for voice and generated avatars), **Your first agent** (**Show me** opens a seeded agent's chat, **Make me one** creates one from a purpose you pick, **Bring it over** points at migrating an existing fleet, or skip) and **Usage sharing** (the anonymous usage-sharing consent). Every step except **Connect Claude** is skippable, and skipping tells you where to find it later; **Finish later** closes the whole sequence and nothing re-opens it on its own. See [Setup → Your First Dashboard](../getting-started/setup.md#your-first-dashboard).

## How do I re-run the first-run setup, or get back to a step I skipped?

Use **Settings → General → First-run setup → Re-run setup**, or add `?onboarding=1` to the Dashboard URL (`http://localhost/?onboarding=1`) — log in first, because the login page drops the parameter. This works however many agents you already have: completed steps show as done, a step you skipped earlier is offered again, and the sequence only ever opens by itself on a fresh install. Everything the steps touch also lives in Settings — the admin sign-in email under **General**, Claude and the other keys under **Integrations**, usage sharing under **General** — so you never need the sequence to finish configuration. See [Quick Start → Guided Onboarding](../getting-started/quick-start.md#guided-onboarding-first-run).

## Where did the Getting started checklist go?

It moved off the Dashboard body into the left **Systems** sidebar, under the list of views and above **New View**. If you do not see it, expand the sidebar: collapsing the sidebar hides the checklist along with the view labels. Its header shows your progress and collapses the list, and it refreshes whenever you return to the Dashboard. It also disappears once every milestone is done, or for good after **Don't show this again**, which cannot be undone. The checklist appears only on instances with the matching enterprise entitlement. See [Setup → Your First Dashboard](../getting-started/setup.md#your-first-dashboard).

## How do I log in to Trinity?

There are two methods. Admin login: enter the username `admin` (or `ADMIN_USERNAME`) or the admin's registered email, plus the password from `.env` — you bind an email in the **Sign-in email** step of first-run setup or under **Settings → General → Admin sign-in email** (an admin created in the browser at `/setup` already has one). Email login (passwordless): enter your email address, receive a 6-digit verification code, and submit it — this requires a configured email service and your address must be on the whitelist under **Settings → Access → Email Whitelist**. Password login is rate-limited: five failed attempts on one account within 15 minutes lock it until the window passes. See [Setup](../getting-started/setup.md).

## What is the email whitelist?

The email whitelist controls which email addresses can log in via the passwordless email-code flow. The admin manages it under **Settings → Access → Email Whitelist**. A whitelisted user who signs up receives the default role recorded on their whitelist entry — `user` unless the admin set another `default_role` when adding the address through the API (the Settings form always adds at `user`) — so promote them to `creator` from **Settings → Access → User Management** if they should create their own agents. See [Roles and Permissions](../getting-started/roles-and-permissions.md).

## Why can't my teammate log in with their email?

Two common reasons: their address isn't on the email whitelist, or no email service is configured so verification codes can't be delivered. Ask your admin to add the address under **Settings → Access → Email Whitelist**, and to add an email provider key under **Settings → Integrations** (or set `EMAIL_PROVIDER` in `.env` to a real provider). Without email service configuration, only admin password login is available. See [Platform Keys](../credentials/platform-keys.md#email-provider-resend).


## Why didn't I receive my 6-digit login code?

The default email provider is `console`, which is meant for development — it prints the email (including the code) to the backend logs instead of sending it. For real delivery, add a Resend key and a sender address under Settings > Integrations — no `.env` edit needed ([Platform Keys](../credentials/platform-keys.md#email-provider-resend)) — or set `EMAIL_PROVIDER` in `.env` to `smtp`, `sendgrid`, or `resend` and fill in the matching credentials. Also confirm your address is on the whitelist, since codes only go to allowed emails. See [Setup](../getting-started/setup.md).

## Why does nothing load when I open http://localhost?

First check that Docker is actually running — Trinity can't start without it. If Docker is up, another process may already hold port 80, which the frontend binds by default; add `FRONTEND_PORT=8090` (or any free port) to `.env`, restart with `./scripts/deploy/start.sh`, and open `http://localhost:8090` instead. See [Deploying Trinity](../guides/deploying-trinity.md).

## Where do I find the web UI, the API, and the interactive API docs?

The web UI is at http://localhost, the backend API with interactive Swagger docs is at http://localhost:8000/docs, and the MCP server is at http://localhost:8080/mcp. Local and production deployments use the same ports; on a server install the production frontend also proxies `/mcp`, so MCP is reachable at the web UI's own hostname (`https://trinity.your-domain.com/mcp`). See [Setup](../getting-started/setup.md).

## How is the web UI organized?

The top navigation has five entries. **Dashboard** is the fleet, in three interchangeable views — Timeline, Grid, and List — with `v` to cycle the view and `/` to type-filter by name; there is no separate Agents page (`/agents` redirects to the Dashboard's List mode). **Library** holds everything installable: agent templates, systems, and the shared skills library. **Operations** is the operator queue, notifications, health, and executions. **Settings** holds platform configuration. **Workspace** is the chat app — one continuous conversation per agent — and opens in its own browser tab so the console page you were on stays put. Clicking an agent opens its detail page, whose header has **Start/Stop**, **Autonomy**, **Workspace**, and **Talk** (a voice call that opens in the Workspace); there is no browser terminal tab — shell access is by SSH. See [Using Trinity](../guides/using-trinity.md).

## How do I create my first agent?

On a fresh install, use the **Your first agent** step of first-run setup, which comes right after **Connect Claude** so the agent can actually think. **Show me** (**Watch Cornelius work**) opens a seeded agent's chat before you build anything; **Make me one** asks one question — pick what the agent should do (**Research a market or topic**, **Advise on strategy**, **Write content & reports**, **Start from scratch**) — and opens the real Create Agent form with the matching starter template pre-selected; **Done** at the end of the sequence opens that agent's chat. The step counts as done once you own an agent Trinity did not seed, and you can come back to it any time with `?onboarding=1` or **Settings → General → First-run setup**. You can also do it manually: click **Create Agent** in the Dashboard header (present in every view — Timeline, Grid and List), choose a source (a starter template, a registered GitHub template, any GitHub repository, or a blank agent), enter a lowercase slug, and click **Create** — Trinity clones the template, builds the container, and starts the agent. On an install with no agents at all, the Dashboard's empty state shows a **Get started** button that opens the same form. See [Quick Start](../getting-started/quick-start.md).

## Which agents come with a fresh install, and can I delete them?

A truly fresh install seeds a starter fleet so you land on something working without configuring a template: **Cornelius**, a ready-to-use second-brain agent with the Brain Orb (a self-rendering 3D knowledge graph on its **Brain** tab) enabled, plus the **acme** starter team — `acme-scout` (research), `acme-sage` (strategy), and `acme-scribe` (content), three collaborating agents from the bundled default system manifest, with no schedules and no credentials needed at seed time. Seeding runs once only: it is skipped if the instance already has agents, and deleting a seeded agent does not re-create it. Skip or replace the team with `TRINITY_DEFAULT_SYSTEM_MANIFEST` in `.env`. The seeded agents cannot think until you add a model credential — the **Connect Claude** step of first-run setup, or an Anthropic API key or a Claude subscription under **Settings → Integrations**; the first credential you add is connected to every agent that has never run. The three `acme` agents ship with a bundled default avatar, while Cornelius shows initials until a Gemini key exists and **Generate Default Avatars** (Settings → General) has run. See [Setup → Your Starter Fleet](../getting-started/setup.md#your-starter-fleet).

## Where do I find things in Settings?

Settings is tabbed; every authenticated user sees **MCP Keys**, admins see all of them. **General** holds **First-run setup** (re-run the guided sequence), **Usage sharing** (opt-in telemetry), **Security & product updates**, the admin sign-in email, platform options and feature flags, proactive message limits, Brain Orb, voice, and build info. **Access** is the email whitelist, user management and roles, and the SSH Access toggle. **Integrations** is API keys, Slack, OAuth credentials, and Claude subscriptions. **Agents** holds GitHub templates, the template registry, skills-library sources, quotas, and automation defaults. **Retention** sets how long executions, logs, health checks, and soft-deleted records are kept, plus the **Workspace sessions** policy and **Room budgets**. Further tabs appear only when the corresponding capability is enabled on your installation. See [Using Trinity → Settings](../guides/using-trinity.md#settings).

## Trinity asked me to opt in to "Security & product updates" — what is that, and is it the same as usage sharing?

No, they are two separate, independent opt-ins, both off by default. **Security & product updates** (Settings → General) is an identified contact form — email required, company, name, role, and use case optional — that sends exactly the previewed submission, at most once per install, so the Trinity team can reach you about security notices and major releases; nothing leaves the box unless you click **Opt in & submit** (an install claimed in the browser asks the same question on the `/setup` form, and ticking it there is the same once-per-install submission). **Usage sharing** (also Settings → General, and the last step of first-run setup) is anonymous: coarse aggregates keyed by a random share id, with a preview of the exact payload before you consent, and reversible. Air-gapped or privacy-strict installs disable the contact form with `OPERATOR_INTAKE_ENABLED=false`, and `DO_NOT_TRACK=1` turns off both. See [Setup](../getting-started/setup.md) and [Telemetry](../operations/telemetry.md).

## What is a template?

A template is a GitHub repository or local directory that defines an agent's initial configuration — its instructions (CLAUDE.md), metadata (template.yaml), MCP tool configuration, and credential declarations. When you create an agent, Trinity copies the template files into the agent's workspace and reports any declared credentials as "missing" until you configure them. See [Creating Agents](../agents/creating-agents.md).

## Is Trinity free to use?

Yes. Trinity is licensed under the Apache License 2.0 — free for any use, commercial included, with an explicit patent grant. You can run it on your own infrastructure, any cloud, or a managed instance. See the [LICENSE](https://github.com/abilityai/trinity/blob/main/LICENSE) file in the repository.

## Are some features only available in an enterprise edition?

Trinity is open-core: the open-source platform in the public repository is complete and fully functional on its own. Customers with an enterprise agreement additionally get access to a private companion repository that mounts as an optional module and unlocks extra capabilities under a separate commercial license; in the UI, those surfaces simply say they require an entitlement. You can check what your instance is running via `GET /api/version`, which reports `edition` (`oss` or `enterprise`) and the list of registered enterprise features — an empty list is normal for open-source installs. See [Enterprise Modules](../../ENTERPRISE.md).

## Where can I get help if I'm stuck?

Ask the docs Q&A bot from the repo with `./scripts/ask-trinity.sh "your question"`, or use the floating Help widget inside the UI, which lets you ask questions, report bugs, request features, and send private feedback. You can also open GitHub Issues or Discussions, and watch workshops, demos, and deep-dives in the [video library](../videos.md). See [Getting Help](../getting-started/help.md).
