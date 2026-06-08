# Trinity Platform Demo Scenario

A structured walkthrough for recording a platform demo. Organized as alternating **slides** (concepts to explain) and **demo segments** (what to show on screen).

---

## Part 0 — The Analogy: Claude Code Local vs Trinity

### Slides: What an Agent Gets

Start with a comparison. When you run Claude Code locally, an agent has:

| What it has | Details |
|---|---|
| **Local filesystem** | Your entire computer as workspace |
| **Instructions** | `CLAUDE.md` — the system prompt |
| **Skills** | Slash commands and playbooks |
| **MCP servers** | Whatever you configure locally |
| **Invocation** | Manual — you type, it runs |
| **Credentials** | API keys from your local `.env` |
| **Lifespan** | Tied to your terminal session |

That's a powerful developer tool. But it's still *you* running it, on *your* machine, manually.

**Trinity takes that same agent and turns it into persistent infrastructure.** Here's what gets added:

| What Trinity adds | Why it matters |
|---|---|
| **Isolated container** | Dedicated Docker environment — not your laptop. Consistent base image (Python, Node.js, Go, Claude Code pre-installed). Runs 24/7. |
| **Persistent identity** | Filesystem, chat history, and session memory survive restarts. Accessible from anywhere, not just your terminal. |
| **GitHub sync** | Agent code is version-controlled. Auto-sync, per-agent PAT, rollback to any commit. |
| **Permissions layer** | Which agents can call which. Nothing is open by default — every agent-to-agent connection is an explicit grant. |
| **Access management** | Share agents with teammates by email. Role-based access. Multi-channel allow-lists. Access request approvals. |
| **Subscription management** | Payment-gated access via Nevermined x402 — charge per message, credit-based, sandbox or mainnet. |
| **Centralized MCP server** | 62 tools. Every agent on the fleet can discover and call every other agent through one hub. |
| **Agent shared folders** | Docker volumes mounted between agents — agents pass files to each other without going through external storage. |
| **Event pub/sub** | Agents subscribe to each other's events. Reactions happen without direct orchestration. |
| **Scheduling & autonomy** | Cron schedules, webhook triggers, pre-check hooks (skip runs with zero tokens consumed), per-agent autonomy master switch. |
| **Public exposure & authenticated access** | Generate a public link for any agent — share it with specific users who authenticate before they can talk to it. No need to give anyone a Trinity account. |
| **Messaging app presence** | Connect an agent to Slack, Telegram, or WhatsApp. Users interact in the apps they already use; the agent responds as a bot in their channel or DM. |
| **Inter-agent communication** | Agents call each other via the centralized MCP server (`chat_with_agent`), share files via mounted Docker volumes, and react to each other's events via pub/sub — three distinct communication primitives. |
| **Dashboard** | Visual fleet overview: agent cards, status, resource use. Collaboration graph showing the live agent network with animated edges during active calls. Timeline view of who called whom and when. |
| **Backup & recovery** | Credentials exported as AES-256-GCM encrypted files safe to commit to git. Agent code continuously synced to GitHub — any agent can be restored from its repo. Database backup scripts included. |
| **Operating Room** | Human-in-the-loop queue. Agents push approval requests or questions; you respond without going into the agent. |
| **Monitoring & audit** | Fleet health grid. Execution history with cost, duration, and model per run. Append-only audit log, 365-day retention. |
| **Resource governance** | Per-agent CPU/memory limits, execution timeouts, read-only mode, guardrails. |
| **Agent website proxy** | An agent can serve a full web app at a public URL — no infrastructure changes needed. |

The short version: **Claude Code locally is a developer tool. Trinity is a runtime.**

---

## Part 1 — What is Trinity

### Slides: Core Concept

- Trinity is **infrastructure for autonomous AI agents** — not a chat app, not a single agent, not a SaaS tool.
- An **agent** is an AI system running as an isolated Docker container. It has its own filesystem, credentials, tools, and memory. It persists across sessions.
- The **platform** is what you deploy on your own server (or Trinity Cloud). It manages the fleet: creation, lifecycle, scheduling, collaboration, monitoring.
- Key components to name:
  - **Backend** — the API and orchestration engine
  - **MCP Server** — how agents talk to each other and to the platform (62 tools)
  - **Agent containers** — each agent lives here; Claude Code is pre-installed
  - **Dashboard** — the web UI you operate everything from

> Reference: `docs/user-docs/getting-started/overview.md` — Concepts section and "How It Works" steps.

---

## Part 2 — Agent Creation

### Slides: Templates and the Creation Model

- Agents are created from **templates** — GitHub repositories that define an agent's instructions, tools, and credential requirements.
- A template contains:
  - `CLAUDE.md` — the agent's instructions / system prompt
  - `template.yaml` — metadata (name, description, required resources)
  - `.mcp.json.template` — which MCP tools and external services it uses, with `${VAR}` placeholders
  - `.env.example` — which credentials it needs
- When you create an agent, Trinity clones the template, builds a container from the base image, copies in the files, and starts the agent. The whole thing takes about 30 seconds.
- Names are unique, lowercase, hyphenated — they are also the agent's network identity inside the platform.

> Reference: `docs/user-docs/agents/creating-agents.md`

### Demo: Create an Agent

- [ ] Go to Dashboard → click **Create Agent**
- [ ] Show the template selection screen — highlight that templates come from GitHub repos
- [ ] Pick a template (something with a clear purpose — a research agent, a content agent)
- [ ] Enter a name, click Create
- [ ] Watch the container spin up — show the status going from creating → running
- [ ] Open the agent detail page — point out the tabs: Chat, Schedules, Files, Config

---

## Part 3 — Working with an Agent

### Slides: The Agent Interface

- The agent detail page is your interface to one agent. Four main areas:
  - **Chat** — send messages, get responses, full conversation history persisted in the database
  - **Session** — resumes the exact Claude Code session (memory, tool state, context) from where you left off
  - **Files** — browse everything in the agent's workspace; download any file
  - **Config** — credentials, permissions, autonomy, timeouts, guardrails

### Demo: Chat and Files

- [ ] Send a message to the agent — something that shows it actually doing work (using a tool, reading a file, producing output)
- [ ] Show the response with cost and execution time visible
- [ ] Switch to the **Session tab** — explain this resumes the same Claude Code session, memory intact
- [ ] Switch to **Files** — show the workspace tree, point out CLAUDE.md and any output files
- [ ] Open **Config** → Credentials — show the credential injection panel; explain credentials are injected as `.env` into the container, never stored in plaintext

> Reference: `docs/user-docs/agents/agent-chat.md`, `docs/user-docs/agents/agent-session.md`, `docs/user-docs/agents/agent-files.md`, `docs/user-docs/credentials/credential-management.md`

---

## Part 4 — Automation

### Slides: Schedules and Autonomy

- Agents can run **completely on their own** via schedules — cron-based, with timezone support.
- Each schedule has: a name, a cron expression, a message (the task), and optionally a model override.
- **Autonomy Mode** is a master switch per agent. When off, no schedules fire. When on, all enabled schedules run.
- Each firing creates an **execution record**: status, duration, response, cost, model used. Full history is kept.
- **Pre-check hook** — a script in the agent that runs before Claude is invoked. If there's nothing to do (no new emails, no new PRs), it returns empty and the execution is skipped — zero tokens consumed.
- Schedules can also be triggered via **webhook** — an external system can fire an agent without any Trinity login.

> Reference: `docs/user-docs/automation/scheduling.md`

### Demo: Create a Schedule

- [ ] Open the agent detail page → **Schedules tab**
- [ ] Click **Create Schedule** — show the form: name, cron expression, message, timezone
- [ ] Set a simple cron (e.g. every morning at 9am on weekdays: `0 9 * * 1-5`)
- [ ] Save it — show it appear in the list with next run time
- [ ] Click **Run Now** — trigger it manually
- [ ] Watch the execution appear in the history — open it to show status, duration, cost, full response
- [ ] Toggle **Autonomy Mode** on/off — explain what this controls

---

## Part 5 — Multi-Agent Collaboration

### Slides: The Agent Network

- Multiple agents can work together. One agent can **delegate tasks** to another via the `chat_with_agent` MCP tool.
- This enables **orchestrator / worker patterns**: one agent coordinates, others specialize.
- The **Collaboration Dashboard** shows this as a live network graph — agents as nodes, communication as animated edges.
- Two views: **Graph** (topology) and **Timeline** (execution flow with arrows showing which agent called which).
- Agents need explicit **permissions** to call each other — configured in the Permissions tab. Nothing is open by default.
- **Shared folders** let agents pass files to each other via Docker volumes.
- **Event subscriptions** let agents react to each other's events — pub/sub between agents.

> Reference: `docs/user-docs/collaboration/agent-network.md`, `docs/user-docs/collaboration/agent-permissions.md`, `docs/user-docs/collaboration/event-subscriptions.md`

### Demo: The Collaboration Dashboard

- [ ] Go to the Dashboard — show the network graph with existing agents as nodes
- [ ] Point out the node colors (green = running, gray = stopped)
- [ ] If a collaboration is live or can be triggered: show an animated edge appearing between two agents
- [ ] Switch to **Timeline view** — show executions as blocks, collaboration arrows between them
- [ ] Open one agent's **Permissions tab** — show how you grant another agent access to call it
- [ ] (Optional) Open **Folders tab** — show shared folder expose/mount controls

---

## Part 6 — Operations and Monitoring

### Slides: Running a Fleet

- When you have multiple agents running, you need visibility and control at the fleet level.
- **Monitoring** — per-agent health checks: container state, last activity, resource usage, success rate.
- **Operating Room** — the control center for human-in-the-loop moments. Agents can push approval requests or questions into the operator queue; you respond from here.
- **Audit Log** — every action on the platform is recorded: agent lifecycle events, authentication, credential operations, MCP tool calls. Append-only, 365-day retention.
- **Execution history** — searchable across all agents and schedules.

> Reference: `docs/user-docs/operations/monitoring.md`, `docs/user-docs/operations/operating-room.md`, `docs/user-docs/operations/audit-trail.md`, `docs/user-docs/operations/executions.md`

### Demo: Monitoring and Operating Room

- [ ] Go to **Monitoring** — show the fleet health grid, per-agent status
- [ ] Go to **Operating Room** — show the operator queue (if an agent has pending items, show one; otherwise explain the concept)
- [ ] Go to **Audit Log** — show a few entries, point out the event types, actor, timestamp
- [ ] Go to **Executions** — show the cross-agent execution history, open one to show full detail

---

## Closing Slide

- Everything shown runs on **your infrastructure** — your server, your data, your network.
- The same fleet can be managed via the web UI, the REST API, or via Claude Code using the Trinity MCP server.
- Templates live on GitHub — you build an agent once, deploy it anywhere.
