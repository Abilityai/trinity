# Quick Start: Create Your First Agent in 5 Minutes

Create and interact with a Trinity agent using the Web UI, API, or MCP tools.

> 📺 **Watch:** [Build an AI Recruiter Agent — zero to deployed](https://youtu.be/K7hFWyFIf-Y) *(Jun 2026)* · [From Zero to Deployed AI Agent](https://youtu.be/-TSZyekDS6o) *(Apr 2026)* · [all videos](../videos.md)

## Guided Onboarding (First Run)

The fastest path: let Trinity guide you. A fresh install already runs a seeded starter fleet
(see [First-Time Setup → Your Starter Fleet](setup.md#your-starter-fleet)), and the Dashboard
opens with **first-run setup** — one guided sequence over the Dashboard (the full step list is
in [First-Time Setup → Your First Dashboard](setup.md#your-first-dashboard)). Its
**Connect Claude** step is the one you cannot skip: paste a Claude subscription
token or an Anthropic API key, and Trinity checks it with Anthropic before saving it, so your
agent can actually think. Then the **Your first agent** step offers two doors:

- **Show me** — **Watch Cornelius work** opens a seeded agent's chat, before you build anything.
- **Make me one** — *pick what it should do*, with four purpose cards (**Research a market or
  topic**, **Advise on strategy**, **Write content & reports**, **Start from scratch**). Your pick
  opens the real **Create Agent** form with the matching starter template pre-selected; once the
  agent is created, the step is done.

Already running a fleet elsewhere? A small **Bring it over** link points at the migration docs.
Click **Continue**, and **Done** at the end of the sequence opens your new agent's chat
(**Show me** opens the seeded agent's chat instead).

The agent step is optional: **Skip — later in the dashboard** moves on, and **Finish later**
closes the whole sequence. Neither nags you again.

On an install with no agents at all (first-run seeding disabled), the Dashboard's empty state
shows a **Get started** button that opens the **Create Agent** form directly.

**Re-run first-run setup any time** (e.g. to spin up another agent, or if you skipped it):
open the Dashboard with the `?onboarding=1` query parameter, or use
**Settings → General → First-run setup → Re-run setup**:

```
http://localhost/?onboarding=1
```

This works regardless of how many agents you already have — completed steps show as done.
**Log in first**, then open the link (opening it while signed out sends you through the
login page, which drops the `?onboarding=1` parameter).

> Prefer to do it manually? Skip the step and follow **How It Works** below.

## How It Works

1. Open http://localhost and log in as admin.
2. Click **Create Agent** in the Dashboard header (it is there in every Dashboard view — Timeline, Grid and List; there is no separate Agents page).
3. Choose a template:
   - **Blank Agent (Claude Code)** -- A minimal agent with a default CLAUDE.md that you shape yourself.
   - **Local Templates** -- The templates bundled with your install (the scout / sage / scribe starters, and more).
   - **GitHub Templates** -- Repositories your admin has registered under **Settings → Agents → GitHub Templates**.
   - **GitHub Repository** -- Any repo, as `owner/repo` or a full GitHub URL, with a **Clone / Copy / Fork** choice for how the agent relates to that repo. (A specific branch, `github:Org/repo@branch`, is available through the API and MCP.)
   - Featured **fork-to-own** templates, when your install offers them, ask for a destination repo and a token so the agent gets a repository of its own.
4. Enter a **Slug / Identifier** (lowercase, no spaces — it becomes the agent's permanent name in URLs, containers and keys) and, optionally, a display name.
5. Click **Create** -- Trinity clones the template, builds the container, and starts it.
6. The form closes and the agent appears on the Dashboard (a GitHub-sourced create first shows an import check — **Close** is always available). Click the agent to open its detail page and start with **Chat**, **Tasks**, or the **Workspace**.

### What Happens After Creation

- A Docker container is built from the `trinity-agent-base` image.
- Template files are copied into the agent's workspace at `/home/developer/`.
- Credentials the template declares show as missing on the **Credentials** tab until you add them.
- The agent starts automatically and appears on the Dashboard.

Details: [Creating Agents](../agents/creating-agents.md).

### Interacting With Your Agent

The agent detail page has tabs for **Overview**, **Tasks**, **Chat**, **Reports**, **Canvas**, **Schedules**, **Loops**, **Playbooks**, **Credentials**, **Payments**, **Git**, **Files**, **Folders**, **Skills**, **Settings** and **Info**; **Dashboard**, **Brain**, **Access / Sharing / Permissions** and **A2A** appear when the agent or your install enables them.

- **Chat tab** -- Stateless chat: each message starts fresh. **Continue in Workspace →** (top of the tab) or the **Workspace** button in the agent header opens the continuous conversation, where memory, tool results and reasoning carry across turns; the header's **Talk** button starts a voice call there.
- **Tasks tab** -- Send one-off tasks and view execution history.
- **Files tab** -- Browse and edit agent workspace files.
- **Schedules tab** -- Make the agent autonomous. The empty state has a **Create a schedule** button.
- **Direct shell access** -- Over SSH, not the browser. See [Agent Terminal](../agents/agent-terminal.md).

### Next Steps

- Add credentials in the agent's **Credentials** tab.
- Send a task via the **Tasks** tab or chat via the **Chat** tab.
- Set up schedules for autonomous operation.
- Configure permissions if building multi-agent systems.

## For Agents

### Via the API

```bash
# Get auth token
TOKEN=$(curl -s -X POST http://localhost:8000/api/token \
  -d 'username=admin&password=YOUR_PASSWORD' | python3 -c "import sys,json; print(json.load(sys.stdin).get('access_token') or '')")

# Create agent from template
curl -X POST http://localhost:8000/api/agents \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "my-agent", "template": "github:Org/repo"}'
```

### Via MCP

```
create_agent(name="my-agent", template="github:Org/repo")
```

## See Also

- [Setup](setup.md) -- Initial platform installation, first login, the starter fleet.
- [Creating Agents](../agents/creating-agents.md) -- Every template type and what creation does.
