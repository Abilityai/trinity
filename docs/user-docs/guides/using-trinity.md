# Using the Trinity Interface

A quick tour of the web UI — dashboard, agent management, chat, and day-to-day operations. Trinity is the operating system for the AI-native company: open source, self-hosted, and model-agnostic, so each agent you manage here runs on Claude Code, Gemini CLI or OpenAI Codex (see [Overview](../getting-started/overview.md)).

> 📺 **Watch:** [Trinity Platform Demo — full UI walkthrough](https://youtu.be/ivljtZqsxeo) *(May 2026)* · [all videos](../videos.md)

## Logging In

- **Admin login** — Enter username `admin` (or your admin email) and the password set via `ADMIN_PASSWORD` in `.env` before first boot or — on an install booted without `ADMIN_PASSWORD`, such as a marketplace one-click server or an AWS install — the one you chose on the first-visit "Create your admin account" screen.
- **Email login** — Enter your email to receive a 6-digit code (requires email service configuration).

## Top Navigation

| Entry | What it is |
|-------|-----------|
| **Dashboard** | The fleet — three view modes, plus Create Agent |
| **Library** | Everything installable: agent templates, systems, and skills |
| **Operations** | Operator queue, notifications, health, executions |
| **Settings** | Platform configuration (all users see MCP Keys; admins see every tab) |
| **Workspace** | The chat app — one continuous conversation per agent, voice, files, loops, and the agent's canvas. Opens in its own browser tab so the console page you were on stays put |

One more entry appears only on entitled installations: **Enterprise** (the entitled-feature catalogue). Shared multi-agent rooms are not a nav entry — they open from the Workspace when you `@mention` a second agent in a chat.

Opening an agent in the Workspace starts a new chat (or returns you to one holding an unsent draft), and keyboard shortcuts switch agents and chats and toggle the rail even while you type in the message field. See [Workspace](../sharing-and-access/workspace.md) for the full tour of that surface, including its [keyboard shortcuts](../sharing-and-access/workspace.md#keyboard-shortcuts).

There is no separate Agents page — it is now the Dashboard's List mode, and `/agents` redirects there.

## Dashboard

The Dashboard gives you a bird's-eye view of your agent fleet in three interchangeable views:

- **Timeline** (default) — Recent and live executions per agent, chronologically.
- **Grid** — A draggable tile canvas, optionally overlaid with department zones and reporting lines.
- **List** — A sortable, filterable row list with inline toggles and bulk tag actions.

Shared controls across all three: press `/` to type-filter the fleet by name, press `v` to cycle the view, plus tag filter, owner filter, time range, and **Create Agent**. A new agent shows up as soon as it is created — no waiting for the next refresh.

On a fresh install the Dashboard opens **first-run setup** — one guided sequence (connect Claude, optional keys, your first agent, usage sharing) that you can re-run from **Settings → General → First-run setup** or with `?onboarding=1`. After that, on instances that have it, a **Getting started** checklist sits in the Dashboard's left **Systems** sidebar, under the view list, until your first milestones are done. See [First-Time Setup → Your First Dashboard](../getting-started/setup.md#your-first-dashboard).

See [Dashboard](../operations/dashboard.md) for the full reference.

## Library

**Library** in the top nav (formerly Templates; `/templates` still redirects there) is one surface for everything you can install onto your fleet, split across three tabs:

- **Agent Templates** (`/library?tab=templates`) — Starter templates and GitHub templates; **Use Template** opens the create-agent flow.
- **Systems** (`/library?tab=systems`) — Install a whole multi-agent system from a manifest. Requires the creator role or above; below that the tab is not shown at all.
- **Skills** (`/library?tab=skills`) — Browse the shared skills library, see its sync state honestly, and see which agents already hold each skill — all without opening an individual agent.

The active tab lives in the URL, so a tab is linkable and survives a refresh. Switching tabs doesn't push browser history, so Back leaves the page rather than walking you through the tabs you visited. Each tab loads independently, so a failure in one never blanks the others.

Each skill card carries an **Assigned to** list of the agents that hold it, an **Assign to…** picker to add another, and an × on each holder chip to unassign — so the page that tells you a skill is unused is the page that fixes it. A separate block lists assignments whose skill has since left the library; those packages stay on each agent until removed from that agent's Skills tab. See [Skills and Playbooks](../automation/skills-and-playbooks.md).

## Agent Management

Click any agent to open its detail page. Tabs appear based on what the agent has enabled — tabs that do not fit collapse into a **More ▾** menu:

| Tab | Purpose |
|-----|---------|
| **Overview** | Landing tab — trends, health, needs-attention count, footprint |
| **Tasks** / **Chat** | Send work to the agent. Chat here is stateless — each message starts fresh; **Continue in Workspace →** opens the continuous conversation |
| **Dashboard** | The agent's own YAML-defined dashboard, or its declared metrics (only when the agent has one or the other) |
| **Brain** | The Brain Orb mind page (only when enabled for the agent) |
| **Reports** | Structured reports the agent has published |
| **Canvas** | The surface the agent keeps current — its living sibling to Reports |
| **Schedules** | Cron jobs, trigger history, next run times |
| **Loops** | Bounded sequential task runs |
| **Skills** | The agent's own skills and those shared from the library, each with **Run**; owners and admins also set approval and assign skills |
| **Credentials** | Per-agent credential setup and status |
| **Payments** | Nevermined payment configuration |
| **Access** / **Sharing** / **Permissions** | Who can reach the agent, and which agents it may call (owners and admins) |
| **A2A** | Inbound A2A exposure (owner-only, entitled installations) |
| **Git** | Repository binding, sync status, and history (only when the agent has a repository binding) |
| **Files** / **Folders** | Browse the agent workspace, download files, shared folders (Folders: owners and admins) |
| **Settings** | Guardrails, autonomy, resources, timeouts, runtime options (owners and admins) |
| **Info** | Template metadata and "what you can ask" |

Header actions:

- **Start/Stop** — Toggle agent container state.
- **Autonomy** — Enable/disable proactive (scheduled) operation. Turning it off holds schedules and reminders without erasing their individual on/off state, so turning it back on restores exactly what you had.
- **Workspace** — Open this agent in the Workspace.
- **Talk** — Start a voice call with the agent. The call opens in the Workspace and the conversation lives there, not in this page's chat.

There is no browser terminal on this page. Direct shell access is by SSH — see [Agent Terminal](../agents/agent-terminal.md).

## Creating Agents from the UI

Click **Create Agent** in the Dashboard header, or **Use Template** on the Library page:

1. **Choose a source**:
   - **Blank Agent (Claude Code)** — A minimal agent with a default CLAUDE.md that you shape yourself.
   - **Local Templates** — The templates bundled with your install (the scout / sage / scribe starters, and more).
   - **GitHub Templates** — Repositories your admin has registered under **Settings → Agents → GitHub Templates**.
   - **GitHub Repository** — Any repo, as `owner/repo` or a full GitHub URL, with a **Clone / Copy / Fork** choice for how the agent relates to that repo. A clone also asks **What is this repository?** — **An agent** or **A deployment of a codebase** — and Trinity sets up the git binding from the answer. (A specific branch, `github:Org/repo@branch`, is available through the API and MCP.)
   - Featured **fork-to-own** templates, when your install offers them, ask for a destination repo and a token so the agent gets a repository of its own.
2. **Enter a name** — A **Slug / Identifier**, lowercase with hyphens, no spaces (e.g., `my-research-agent`). It becomes the agent's permanent name in URLs, containers and keys. You can also set a friendly display label.
3. **Create Agent** — Trinity clones, builds, and starts the container. The form closes and the agent appears on the Dashboard (a GitHub-sourced create first shows an import check — **Close** is always available). Click the agent to open its detail page and start with **Chat**, **Tasks**, or the **Workspace**.

What happens after creation:

- A Docker container is built from the `trinity-agent-base` image.
- Template files are copied into the agent's workspace at `/home/developer/`.
- Credentials the template declares show as missing on the **Credentials** tab until you add them.
- The agent starts automatically and appears on the Dashboard.

Next, add credentials on the **Credentials** tab, send a task or chat, set up schedules (the **Schedules** tab's empty state has a **Create a schedule** button), and configure permissions if you are building a multi-agent system. Importing an existing GitHub repository runs a compatibility check inline. See [Creating Agents](../agents/creating-agents.md).

## Operations

**Operations** in the top nav is your control center for real-time oversight — one page at `/operations` with six tabs:

- **Needs Response** — Agent questions and approval requests waiting on you.
- **Notifications** — Agent alerts and status changes.
- **Health** (admin only) — Fleet health status; the monitoring loop is off by default and must be enabled explicitly, and the setting persists across restarts.
- **Executions** — All task runs across your fleet, with filters and live stats.
- **Reports** — Structured reports published by your agents, fleet-wide.
- **Resolved** — Previously handled items.

The nav entry carries a single badge counting pending queue items and notifications; it pulses when something critical is waiting. Each operator tab has a **Clear All** button for bulk cleanup.

## Settings

Settings is visible to every authenticated user, but most tabs are admin-only. Non-admins see **MCP Keys**.

| Tab | Who | Purpose |
|-----|-----|---------|
| **General** | Admin | **First-run setup** (re-run the guided sequence), **Usage sharing** (opt-in telemetry), **Security & product updates** (operator contact), admin sign-in email, platform options and feature flags, proactive message limits, Brain Orb, voice, Trinity prompt (instance-wide instructions for every agent; see the [recommended Trinity prompt](../agents/recommended-fleet-prompt.md)), build info, default avatars |
| **Access** | Admin | Email whitelist, user management and roles, **SSH Access** toggle |
| **Integrations** | Admin | Platform keys set in the browser (Anthropic, GitHub, email provider, Gemini), Slack, OAuth credentials, subscriptions (Claude subscription pool with live headroom), transport connection |
| **MCP Keys** | Everyone | Create and revoke your own MCP API keys; your personal GitHub token |
| **Agents** | Admin | GitHub templates, **Template registry**, **Skills Library** sources, agent quotas, automation defaults |
| **Retention** | Admin | How long executions, logs, health checks, metric points, and soft-deleted records are kept; **Workspace sessions** policy; **Room budgets** (when shared rooms are available) |

Further tabs appear only when the corresponding capability is enabled on your installation.

Retention windows have exactly one validated write path — values are type- and range-checked, the change is audit-logged, and an unusually large deletion is held for explicit approval rather than run silently. See [Monitoring](../operations/monitoring.md).

## Next Steps

- [Building Agents](building-agents.md) — Create agents with Claude Code
- [Deploying Trinity](deploying-trinity.md) — Cloud and self-hosted setup

## See Also

- [Dashboard](../operations/dashboard.md) — Dashboard reference
- [Operations Page](../operations/operating-room.md) — Operator queue and notifications
- [Executions](../operations/executions.md) — Fleet execution list
- [Monitoring](../operations/monitoring.md) — Health tab and heartbeats
