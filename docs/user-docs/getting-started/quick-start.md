# Quick Start: Create Your First Agent in 5 Minutes

Create and interact with a Trinity agent using the Web UI, API, or MCP tools.

> 📺 **Watch:** [Build an AI Recruiter Agent — zero to deployed](https://youtu.be/K7hFWyFIf-Y) *(Jun 2026)* · [From Zero to Deployed AI Agent](https://youtu.be/-TSZyekDS6o) *(Apr 2026)* · [all videos](../videos.md)

## Guided Onboarding (First Run)

The fastest path: let Trinity guide you. On a **fresh install with no agents yet**,
a short onboarding wizard opens automatically the first time you reach the Dashboard
after logging in. It asks one question — *what do you want your first agent to do?* —
then:

1. Opens the **Create Agent** form with a matching starter template pre-selected.
2. After the agent is created, walks you to the **Claude subscription** step so your
   agent can actually think (Settings → Integrations → Claude Subscriptions), or
   straight to chat if Claude auth is already configured.

You can dismiss it at any time ("Skip for now") — it won't nag you again.

**Relaunch the wizard any time** (e.g. to spin up another agent, or if you skipped it):
open the Dashboard with the `?onboarding=1` query parameter:

```
http://localhost/?onboarding=1
```

This works regardless of how many agents you already have. **Log in first**, then
open the link (opening it while signed out sends you through the login page, which
drops the `?onboarding=1` parameter). On a fresh, empty install the Dashboard also
shows a **Get started** button in the empty state that opens the same wizard.

> Prefer to do it manually? Skip the wizard and follow **How It Works** below.

## How It Works

1. Open http://localhost and log in as admin.
2. Click **Create Agent** on the Dashboard or Agents page.
3. Choose a template:
   - **GitHub template** -- Select from configured repos (e.g., pre-built agent templates).
   - **From scratch** -- Creates a minimal agent with a default CLAUDE.md.
   - **GitHub URL** -- Paste `github:Org/repo` or `github:Org/repo@branch` for a specific branch.
4. Enter an agent name (lowercase, hyphens allowed).
5. Click **Create** -- Trinity clones the template, builds the container, and starts it.
6. Navigate to the agent's detail page to interact via Terminal, Chat, or Tasks.

### What Happens After Creation

- A Docker container is built from the `trinity-agent-base` image.
- Template files are copied into the agent's workspace at `/home/developer/`.
- Credentials from `.mcp.json.template` are detected and shown as "missing" until configured.
- The agent starts automatically and appears on the Dashboard.

### Interacting With Your Agent

- **Terminal tab** -- Direct Claude Code TUI access (Claude/Gemini/Bash modes).
- **Chat tab** -- Simple chat bubble interface.
- **Tasks tab** -- Send one-off tasks and view execution history.
- **Files tab** -- Browse and edit agent workspace files.

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
  -d 'username=admin&password=YOUR_PASSWORD' | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

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

- [Setup](setup.md) -- Initial platform installation and configuration.
