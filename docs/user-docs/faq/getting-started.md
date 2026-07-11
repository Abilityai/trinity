# Trinity FAQ — Getting Started

> Part of the [Trinity FAQ](README.md). Short, grounded answers with links to the full documentation.

## What is Trinity?

Trinity is an open-source autonomous agent orchestration platform — infrastructure for deploying, orchestrating, and governing fleets of AI agents on your own hardware. Each agent runs in an isolated Docker container with a pluggable runtime (Claude Code, OpenAI Codex, or Gemini CLI), persists memory across sessions, can delegate to other agents, and can run on schedules without human intervention. You interact with the platform through a web UI, a REST API, or MCP tools. See [Overview](../getting-started/overview.md).

## What do I need to run Trinity on my own machine?

You need Docker Desktop (or Docker with Docker Compose), Git, at least 8GB of RAM, and a modern web browser. Trinity cannot run without Docker — all platform services and every agent run as containers. See [Deploying Trinity](../guides/deploying-trinity.md).

## How do I install Trinity?

Clone the repository from GitHub, copy `.env.example` to `.env`, and run `./scripts/deploy/start.sh`. The script starts the backend, frontend, MCP server, Redis, scheduler, and log aggregator, and builds the base agent image automatically if it's missing (which takes 5–10 minutes on first run). Then open http://localhost in your browser. See [Setup](../getting-started/setup.md).

## Can I use Trinity without hosting it myself?

Yes. Ability.ai offers a cloud-hosted option where you sign up, copy an MCP connection URL from Settings, and connect from Claude Code — no infrastructure to manage. Self-hosting is free forever and keeps all data inside your own perimeter, which suits teams with compliance requirements. See [Deploying Trinity](../guides/deploying-trinity.md).

## What happens the first time I open Trinity in my browser?

If you didn't set `ADMIN_PASSWORD` in `.env` before first boot, you see a one-page "Create your admin account" form: enter your admin email (required — it becomes your sign-in identity), a password of 12+ characters with mixed case, a number, and a special character, and optionally your company name. The form works exactly once and disables itself permanently after the admin account is created. If you did set `ADMIN_PASSWORD`, the account is created automatically and no setup screen appears. Note that until setup completes, the form is reachable without authentication — keep an internet-facing server behind a tunnel, VPN, or firewall until you've finished. See [Setup](../getting-started/setup.md).

## How do I log in to Trinity?

There are two methods. Admin login: enter the username `admin` or the admin email you registered at setup, plus your password. Email login (passwordless): enter your email address, receive a 6-digit verification code, and submit it — this requires a configured email service and your address must be on the whitelist. See [Setup](../getting-started/setup.md).

## What is the email whitelist?

The email whitelist controls which email addresses can log in via the passwordless email-code flow. The admin manages it under Settings > Email Whitelist. Whitelisted users who sign up receive the creator role by default, which lets them immediately create and manage their own agents. See [Roles and Permissions](../getting-started/roles-and-permissions.md).

## Why can't my teammate log in with their email?

Two common reasons: their address isn't on the email whitelist, or no email service is configured so verification codes can't be delivered. Ask your admin to add the address under Settings > Email Whitelist, and check that `EMAIL_PROVIDER` in `.env` is set to a real provider. Without email service configuration, only admin password login is available. See [Setup](../getting-started/setup.md).

## Why didn't I receive my 6-digit login code?

The default email provider is `console`, which is meant for development — it prints the email (including the code) to the backend logs instead of sending it. For real delivery, set `EMAIL_PROVIDER` in `.env` to `smtp`, `sendgrid`, or `resend` and fill in the matching credentials. Also confirm your address is on the whitelist, since codes only go to allowed emails. See [Setup](../getting-started/setup.md).

## Why does nothing load when I open http://localhost?

First check that Docker is actually running — Trinity can't start without it. If Docker is up, another process may already hold port 80, which the frontend binds by default; add `FRONTEND_PORT=8090` (or any free port) to `.env`, restart with `./scripts/deploy/start.sh`, and open `http://localhost:8090` instead. See [Deploying Trinity](../guides/deploying-trinity.md).

## Where do I find the web UI, the API, and the interactive API docs?

The web UI is at http://localhost, the backend API with interactive Swagger docs is at http://localhost:8000/docs, and the MCP server is at http://localhost:8080/mcp. Local and production deployments use the same ports. See [Setup](../getting-started/setup.md).

## How do I create my first agent?

On a fresh install, a guided onboarding wizard opens automatically on your first Dashboard visit — it asks what you want your first agent to do, pre-selects a matching starter template, and walks you through creation. You can also do it manually: click **Create Agent** on the Dashboard, choose a template (a configured GitHub repo, a `github:Org/repo` URL, or from scratch), enter a lowercase name, and click **Create**. Trinity clones the template, builds the container, and starts the agent. See [Quick Start](../getting-started/quick-start.md).

## What is the Cornelius agent that appeared on my fresh install?

On a truly fresh install, Trinity auto-seeds a ready-to-use "Cornelius" second-brain agent so you land on a working agent without configuring a template first. It ships with the Brain Orb enabled — a self-rendering 3D knowledge graph on its Brain tab. This happens once only: it's skipped if the instance already has agents, and deleting Cornelius does not re-create it. See [Setup](../getting-started/setup.md).

## What is a template?

A template is a GitHub repository or local directory that defines an agent's initial configuration — its instructions (CLAUDE.md), metadata (template.yaml), MCP tool configuration, and credential declarations. When you create an agent, Trinity copies the template files into the agent's workspace and reports any declared credentials as "missing" until you configure them. See [Creating Agents](../agents/creating-agents.md).

## Is Trinity free to use?

Yes. Trinity is licensed under the Apache License 2.0 — free for any use, commercial included, with an explicit patent grant. You can run it on your own infrastructure, any cloud, or a managed instance. See the [LICENSE](https://github.com/abilityai/trinity/blob/main/LICENSE) file in the repository.

## Are some features only available in an enterprise edition?

Trinity is open-core: the open-source platform in the public repository is complete and fully functional on its own. Customers with an enterprise agreement additionally get access to a private companion repository that mounts as an optional module and unlocks extra capabilities under a separate commercial license. You can check what your instance is running via `GET /api/version`, which reports `edition` (`oss` or `enterprise`) and the list of registered enterprise features — an empty list is normal for open-source installs. See [Enterprise Modules](../../ENTERPRISE.md).

## Where can I get help if I'm stuck?

Ask the docs Q&A bot from the repo with `./scripts/ask-trinity.sh "your question"`, or use the floating Help widget inside the UI, which lets you ask questions, report bugs, request features, and send private feedback. You can also open GitHub Issues or Discussions, and watch workshops, demos, and deep-dives in the [video library](../videos.md). See [Getting Help](../getting-started/help.md).
