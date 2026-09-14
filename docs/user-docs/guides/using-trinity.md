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
| **Workspace** | The chat app — one continuous conversation per agent, voice, files, loops, and the agent's canvas. Opens in its own browser tab so the console page you were on stays put |

One more entry appears only on entitled installations: **Enterprise** (the entitled-feature catalogue). Shared multi-agent rooms are not a nav entry — they open from the Workspace when you `@mention` a second agent in a chat.

See [Workspace](../sharing-and-access/workspace.md) for the full tour of that surface.

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

Each skill card carries an **Assigned to** list of the agents that hold it, an **Assign to…** picker to add another, and an × on each holder chip to unassign — so the page that tells you a skill is unused is the page that fixes it. A separate block lists assignments whose skill has since left the library; those packages stay on each agent until removed from that agent's Skills tab. See [Skills and Playbooks](../automation/skills-and-playbooks.md).

## Agent Management

Click any agent to open its detail page. Tabs appear based on what the agent has enabled — tabs that do not fit collapse into a **More ▾** menu:

| Tab | Purpose |
|-----|---------|
| **Overview** | Landing tab — trends, health, needs-attention count, footprint |
| **Tasks** / **Chat** | Send work to the agent. Chat here is stateless — each message starts fresh; **Continue in Workspace →** opens the continuous conversation |
| **Dashboard** | The agent's own YAML-defined dashboard (only when the agent ships one) |
| **Brain** | The Brain Orb mind page (only when enabled for the agent) |
| **Reports** | Structured reports the agent has published |
| **Canvas** | The surface the agent keeps current — its living sibling to Reports |
| **Schedules** | Cron jobs, trigger history, next run times |
| **Loops** | Bounded sequential task runs |
| **Playbooks** | Reusable prompts the agent exposes |
| **Credentials** | Per-agent credential setup and status |
| **Payments** | Nevermined payment configuration |
| **Access** / **Sharing** / **Permissions** | Who can reach the agent, and which agents it may call |
| **A2A** | Inbound A2A exposure (owner-only, entitled installations) |
| **Git** | Repository binding, sync status, and history |
| **Files** / **Folders** | Browse the agent workspace, download files, shared folders |
| **Skills** | Assign and sync skills from the library |
| **Settings** | Guardrails, autonomy, resources, timeouts, runtime options |
| **Info** | Template metadata and "what you can ask" |

Header actions:

- **Start/Stop** — Toggle agent container state.
- **Autonomy** — Enable/disable proactive (scheduled) operation. Turning it off holds schedules and reminders without erasing their individual on/off state, so turning it back on restores exactly what you had.
- **Workspace** — Open this agent in the Workspace.
- **Talk** — Start a voice call with the agent. The call opens in the Workspace and the conversation lives there, not in this page's chat.

There is no browser terminal on this page. Direct shell access is by SSH — see [Agent Terminal](../agents/agent-terminal.md).

## Creating Agents from the UI

Click **Create Agent** in the Dashboard header, or **Use Template** on the Library page:

1. **Choose a source** — a starter template, a GitHub template, an existing GitHub repository, or a blank agent.
2. **Enter a name** — Lowercase with hyphens (e.g., `my-research-agent`). You can also set a friendly display label.
3. **Create** — Trinity clones, builds, and starts the container.

Importing an existing GitHub repository runs a compatibility check inline and lets you choose how to take it on — fork, copy, or clone. See [Creating Agents](../agents/creating-agents.md).

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
| **General** | Admin | **Usage sharing** (opt-in telemetry), **Security & product updates** (operator contact), admin sign-in email, platform options and feature flags, proactive message limits, Brain Orb, voice, Trinity prompt, build info, default avatars |
| **Access** | Admin | Email whitelist, user management and roles, **SSH Access** toggle |
| **Integrations** | Admin | API keys, Slack, OAuth credentials, subscriptions (Claude subscription pool with live headroom), transport connection |
| **MCP Keys** | Everyone | Create and revoke your own MCP API keys; your personal GitHub token |
| **Agents** | Admin | GitHub templates, **Template registry**, **Skills Library** sources, agent quotas, automation defaults |
| **Retention** | Admin | How long executions, logs, health checks, and soft-deleted records are kept; **Workspace sessions** policy; **Room budgets** |

Additional tabs (**Agent Permissions**, **Security**, **SSO**, **Vault**, **Activation**) appear only when the corresponding capability is enabled on your installation.

Retention windows have exactly one validated write path — values are type- and range-checked, the change is audit-logged, and an unusually large deletion is held for explicit approval rather than run silently. See [Monitoring](../operations/monitoring.md).

## Next Steps

- [Building Agents](building-agents.md) — Create agents with Claude Code
- [Deploying Trinity](deploying-trinity.md) — Cloud and self-hosted setup

## See Also

- [Dashboard](../operations/dashboard.md) — Dashboard reference
- [Operations Page](../operations/operating-room.md) — Operator queue and notifications
- [Executions](../operations/executions.md) — Fleet execution list
- [Monitoring](../operations/monitoring.md) — Health tab and heartbeats
