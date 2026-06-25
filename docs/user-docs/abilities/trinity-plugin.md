# trinity Plugin

Connect, deploy, operate, and sync agents on the Trinity platform. Six skills covering the complete lifecycle — from first connection to running remote loops and provisioning new instances.

> 📺 **Watch:** [Build Autonomous Loops for Your AI Agents](https://youtu.be/q3YvFYtuhec) *(Jun 2026)* · [all videos](../videos.md)

## Installation

```bash
/plugin install trinity@abilityai
```

## Skills

| Skill | Description |
|-------|-------------|
| `/trinity:connect` | One-time: authenticate and configure the MCP connection |
| `/trinity:onboard` | Per-agent: compatibility check, file creation, deploy |
| `/trinity:sync` | Ongoing: sync changes between local and the running remote agent |
| `/trinity:loop` | Run a remote agent task in a sequential, bounded loop — fire once, disconnect, check back |
| `/trinity:create-dashboard` | Generate an agent-specific `/update-dashboard` skill that keeps `dashboard.yaml` current |
| `/trinity:deploy-new-instance` | Deploy a Trinity instance on any server and scaffold an ops agent to manage it |

## How It Works

### Step 1: Connect (One-Time)

```bash
/trinity:connect
```

This:

1. Prompts for your Trinity instance URL
2. Authenticates via email verification
3. Provisions an MCP API key
4. Configures `.mcp.json` for Trinity MCP tools

After connecting, Trinity MCP tools become available:

- `mcp__trinity__list_agents`
- `mcp__trinity__chat_with_agent`
- `mcp__trinity__deploy_local_agent`
- `mcp__trinity__run_agent_loop` (and the rest of the tool surface)

### Step 2: Onboard (Per Agent)

```bash
/trinity:onboard
```

For each agent you want to deploy:

1. **Compatibility check** — Verifies required files exist
2. **File creation** — Generates missing Trinity files
3. **Deploy** — Pushes the agent to the Trinity platform

Required files (created if missing):

- `template.yaml` — Agent metadata
- `.env.example` — Environment variable documentation
- `.mcp.json.template` — MCP server configuration

### Step 3: Sync (Ongoing)

```bash
/trinity:sync
```

After making local changes:

- Detects modified files
- Pushes updates to the remote agent
- Optionally restarts the agent

## Remote Loops: `/trinity:loop`

The remote counterpart to Claude Code's built-in `/loop`. Where `/loop` re-invokes your **local session** on a cadence, `/trinity:loop` hands one bounded, sequential loop to a **remote Trinity agent**: it fires `run_agent_loop` once, returns a `loop_id`, and you can disconnect — the Trinity backend runs every iteration in order and exits on a hard cap or a stop signal. Use it for iterative refinement, agentic retry, and bounded polling that must outlive your session.

```
/trinity:loop [@agent] <message>             start a loop
/trinity:loop status <loop_id>               show per-run progress
/trinity:loop stop <loop_id>                 request a graceful stop
```

No `@agent` means **this agent's remote copy** — the usual case is looping your own remote counterpart on Trinity (resolved from `.trinity-remote.yaml` or by name match).

Modifiers, anywhere in the message:

| Modifier | Effect |
|----------|--------|
| `5 times` / `x5` / `max 10` | Iteration cap (`max_runs`, 1–100; default 5) |
| `every 2m` / `every 30s` | Pause between iterations (`delay_seconds`, up to 1 hour — for slower cadences, use a schedule instead) |
| `until <condition>` / `stop when …` | Until mode — the skill rewrites the message so the agent emits a `[[DONE]]` sentinel when the condition is met, and the loop exits early |

Examples:

```
/trinity:loop @researcher draft section {{run}} of the report, 5 times
/trinity:loop @ci-agent run the test suite until it passes, max 10
/trinity:loop @monitor poll the deploy every 2m until it's healthy
/trinity:loop status loop_a1b2c3
/trinity:loop stop loop_a1b2c3
```

After firing, the skill starts a lightweight local watch by default — it polls the loop and reports run-by-run progress, stalls, and the final result. Say "fire and forget" to skip the watch; the remote loop runs either way and also appears on the agent's **Loops** tab in the Trinity web UI.

The loop mechanics — modes, template variables, stop signals, capacity, costs — are the platform's Sequential Agent Loops feature. See [Agent Loops](../automation/agent-loops.md) for the full guide.

### When to use what

| You want | Use |
|----------|-----|
| One remote turn | `chat_with_agent` |
| The same task across many agents at once | `fan_out` |
| One agent, N sequential iterations | `/trinity:loop` (or `run_agent_loop` directly) |
| A recurring task on a cron cadence | A Trinity [schedule](../automation/scheduling.md) |

## Dashboard Generation: `/trinity:create-dashboard`

```bash
/trinity:create-dashboard
```

Analyzes the agent's purpose and data sources, proposes a set of metrics, and — after your approval — scaffolds an agent-specific `/update-dashboard` skill that keeps `dashboard.yaml` current. Schedule that skill on Trinity to keep the agent's dashboard live. See [Dynamic Dashboards](../advanced/dynamic-dashboards.md).

## Instance Provisioning: `/trinity:deploy-new-instance`

```bash
/trinity:deploy-new-instance
```

Deploys a complete Trinity instance on any server you can reach — fresh installs and existing instances both — and scaffolds a dedicated ops agent to manage it (health checks, updates, rollbacks). See [Deploying Trinity](../guides/deploying-trinity.md) and the [Trinity Ops Agent](../guides/deploying/ops-agent.md).

## Alternative: Trinity CLI

You can also deploy via the command line:

```bash
# Install CLI
pip install trinity-cli

# Initialize (one-time)
trinity init

# Deploy agent
trinity deploy .
```

See [Trinity CLI](../cli/trinity-cli.md) for details.

## Compatibility Requirements

Agents must have:

| File | Purpose |
|------|---------|
| `CLAUDE.md` | Agent identity and instructions |
| `template.yaml` | Trinity metadata (name, description, type) |
| `.env.example` | Documents required environment variables |

Optional but recommended:

- `dashboard.yaml` — Custom metrics dashboard
- `.mcp.json.template` — MCP server configuration

## See Also

- [Agent Loops](../automation/agent-loops.md) — The server-side loops feature `/trinity:loop` drives
- [Trinity CLI](../cli/trinity-cli.md) — Command-line deployment
- [Creating Agents](../agents/creating-agents.md) — Agent creation in Trinity
- [Trinity Ops Agent](../guides/deploying/ops-agent.md) — Instance operations post-deploy
- [Abilities Overview](overview.md) — Full toolkit overview
