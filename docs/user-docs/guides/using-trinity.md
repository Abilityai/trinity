# Using the Trinity Interface

A quick tour of the web UI — dashboard, agent management, chat, and day-to-day operations.

> 📺 **Watch:** [Trinity Platform Demo — full UI walkthrough](https://youtu.be/ivljtZqsxeo) *(May 2026)* · [all videos](../videos.md)

## Logging In

- **Admin login** — Enter username `admin` and the password set via `ADMIN_PASSWORD` in `.env` before first boot (self-hosted) or the one chosen at signup (cloud). There is no first-visit password wizard.
- **Email login** — Enter your email to receive a 6-digit code (requires email service configuration).

## Top Navigation

| Entry | What it is |
|-------|-----------|
| **Dashboard** | The fleet — three view modes, plus Create Agent |
| **Library** | Everything installable: agent templates, systems, and skills |
| **Operations** | Operator queue, notifications, health, executions |
| **Settings** | Platform configuration (all users see MCP Keys; admins see every tab) |

One more entry appears only on entitled installations: **Enterprise** (the entitled-feature catalogue). Shared multi-agent rooms are not a nav entry — they open from the Workspace when you `@mention` a second agent in a chat.

There is no separate Agents page — it is now the Dashboard's List mode, and `/agents` redirects there.

## Dashboard

The Dashboard gives you a bird's-eye view of your agent fleet in three interchangeable views:

- **Timeline** (default) — Recent and live executions per agent, chronologically.
- **Grid** — A draggable tile canvas, optionally overlaid with department zones and reporting lines.
- **List** — A sortable, filterable row list with inline toggles and bulk tag actions.

Shared controls across all three: press `/` to type-filter the fleet by name, press `v` to cycle the view, plus tag filter, owner filter, time range, and **Create Agent**.

See [Dashboard](../operations/dashboard.md) for the full reference.

## Library

**Library** in the top nav (formerly Templates; `/templates` still redirects there) is one surface for everything you can install onto your fleet, split across three tabs:

- **Agent Templates** (`/library?tab=templates`) — Starter templates and GitHub templates; **Use Template** opens the create-agent flow.
- **Systems** (`/library?tab=systems`) — Install a whole multi-agent system from a manifest. Requires the creator role or above; below that the tab is not shown at all.
- **Skills** (`/library?tab=skills`) — Browse the shared skills library, see its sync state honestly, and see which agents already hold each skill — all without opening an individual agent.

The active tab lives in the URL, so a tab is linkable and survives a refresh. Switching tabs doesn't push browser history, so Back leaves the page rather than walking you through the tabs you visited. Each tab loads independently, so a failure in one never blanks the others.

Skills are still *assigned* from an agent's own Skills tab. What the Library adds is the fleet-wide read: per skill, the agents that hold it, plus a list of assignments whose skill has since left the library.

## Agent Management

Click any agent to open its detail page. Tabs appear based on what the agent has enabled — tabs that do not fit collapse into a **More ▾** menu:

| Tab | Purpose |
|-----|---------|
| **Overview** | Landing tab — trends, health, needs-attention count, footprint |
| **Tasks** / **Chat** | Send work to the agent; conversation history |
| **Reports** | Structured reports the agent has published |
| **Schedules** | Cron jobs, trigger history, next run times |
| **Loops** | Bounded sequential task runs |
| **Playbooks** | Reusable prompts the agent exposes |
| **Credentials** | Per-agent credential setup and status |
| **Access** / **Sharing** / **Permissions** | Who can reach the agent, and which agents it may call |
| **Git** | Repository binding, sync status, and history |
| **Files** | Browse agent workspace, download files |
| **Skills** | Assign and sync skills from the library |
| **Settings** | Autonomy, resources, timeouts, runtime options |
| **Info** | Template metadata and "what you can ask" |

Key actions:

- **Start/Stop** — Toggle agent container state.
- **Autonomy** — Enable/disable proactive (scheduled) operation. Turning it off holds schedules and reminders without erasing their individual on/off state, so turning it back on restores exactly what you had.
- **Terminal** — SSH-style access to the agent container.

## Creating Agents from the UI

Click **Create Agent** in the Dashboard header, or **Use Template** on the Library page:

1. **Choose a source** — a starter template, a GitHub template, an existing GitHub repository, or a blank agent.
2. **Enter a name** — Lowercase with hyphens (e.g., `my-research-agent`). You can also set a friendly display label.
3. **Create** — Trinity clones, builds, and starts the container.

Importing an existing GitHub repository runs a compatibility check inline and lets you choose how to take it on — fork, copy, or clone. See [Creating Agents](../agents/creating-agents.md).

## Operations

**Operations** in the top nav is your control center for real-time oversight — one page at `/operations` with five tabs:

- **Needs Response** — Agent questions and approval requests waiting on you.
- **Notifications** — Agent alerts and status changes.
- **Health** (admin only) — Fleet health status; the monitoring loop is off by default and must be enabled explicitly, and the setting persists across restarts.
- **Executions** — All task runs across your fleet, with filters and live stats.
- **Resolved** — Previously handled items.

The nav entry carries a single badge counting pending queue items and notifications; it pulses when something critical is waiting. Each operator tab has a **Clear All** button for bulk cleanup.

## Settings

Settings is visible to every authenticated user, but most tabs are admin-only. Non-admins see **MCP Keys**.

| Tab | Who | Purpose |
|-----|-----|---------|
| **General** | Admin | Platform-wide options and feature flags |
| **Access** | Admin | Email whitelist, roles, who can log in |
| **Integrations** | Admin | Slack, Telegram, WhatsApp, and other channel connections |
| **MCP Keys** | Everyone | Create and revoke your own MCP API keys |
| **Agents** | Admin | GitHub template sources, skill sources, fleet defaults |
| **Retention** | Admin | How long executions, logs, health checks, and soft-deleted records are kept |

Additional tabs (**Agent Permissions**, **Security**, **SSO**, **Activation**) appear only when the corresponding capability is enabled on your installation.

Retention windows have exactly one validated write path — values are type- and range-checked, the change is audit-logged, and an unusually large deletion is held for explicit approval rather than run silently. See [Monitoring](../operations/monitoring.md).

## Next Steps

- [Building Agents](building-agents.md) — Create agents with Claude Code
- [Deploying Trinity](deploying-trinity.md) — Cloud and self-hosted setup

## See Also

- [Dashboard](../operations/dashboard.md) — Dashboard reference
- [Operations Page](../operations/operating-room.md) — Operator queue and notifications
- [Executions](../operations/executions.md) — Fleet execution list
- [Monitoring](../operations/monitoring.md) — Health tab and heartbeats
