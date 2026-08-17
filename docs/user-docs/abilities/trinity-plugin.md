# trinity Plugin

Connect, deploy, operate, and sync agents on the Trinity platform. Seven skills covering the complete lifecycle — from a guided first journey through connection, deployment, remote loops, and instance provisioning.

> 🧭 **New to Trinity? Start with `/trinity:start-here`** — a guided, resumable walkthrough that takes you from "what is Trinity?" to your first agent running on your own instance. [Jump to the section](#the-guided-journey-trinitystart-here).

> 📺 **Watch:** [Build Autonomous Loops for Your AI Agents](https://youtu.be/q3YvFYtuhec) *(Jun 2026)* · [all videos](../videos.md)

## Installation

```bash
/plugin install trinity@abilityai
```

## Skills

| Skill | Description |
|-------|-------------|
| `/trinity:start-here` | **New here?** Guided, resumable journey: what Trinity is → get an instance → connect → first agent alive |
| `/trinity:connect` | One-time: authenticate and configure the MCP connection |
| `/trinity:onboard` | Per-agent: compatibility check, file creation, deploy from the repo — or, run inside an already-deployed agent, onboard it in place |
| `/trinity:sync` | Ongoing: git-based sync between your repo and the deployed agent, multi-remote, with schedule and plugin reconciliation |
| `/trinity:loop` | Run a remote agent task in a sequential, bounded loop — fire once, disconnect, check back |
| `/trinity:create-dashboard` | Generate an agent-specific `/update-dashboard` skill that keeps `dashboard.yaml` current |
| `/trinity:deploy-new-instance` | Deploy a Trinity instance on any server and scaffold an ops agent to manage it |

## The Guided Journey: `/trinity:start-here`

```bash
/plugin marketplace add abilityai/abilities
/plugin install trinity@abilityai
/trinity:start-here
```

One command that concierges a newcomer through the whole path — it sequences the skills below rather than replacing them, so you never have to know which one comes next:

| Stage | What happens |
|-------|--------------|
| **Orient** | What Trinity is, in plain terms, plus the current release |
| **Choose your door** | Just looking · build my first agent · I already have agents · set up my instance |
| **Get an instance** | Hands off to `/trinity:deploy-new-instance` (cloud, your server, or local Docker) — or takes the URL you already have |
| **Connect + verify** | Hands off to `/trinity:connect`, then runs a live smoke test: fleet visible, instance healthy, docs assistant online |
| **First agent alive** | Deploys via `/trinity:onboard` (or picks an existing agent) and exchanges a real message with it |

Three things worth knowing:

- **Nothing is deployed until you choose it.** The "just looking" door asks for no credentials and creates nothing.
- **It resumes.** Progress is saved in `~/.trinity/start-here.json`, so quitting — or the Claude Code restart needed to load the MCP server — picks up exactly where you stopped. Run `/trinity:start-here reset` to start over.
- **Answers come from live documentation, not a script.** Questions are answered from these docs at run time — through the public [Trinity Docs Q&A](../getting-started/help.md) before you have an instance, and through your instance's own `ask_trinity` tool afterward.

Already know what you want? The individual skills below work standalone, in any order.

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

### Step 2: Add your GitHub token (One-Time)

Deployment is **repository-first**: Trinity clones the agent straight from its GitHub repo and tracks the branch. In the Trinity UI, go to **Settings → GitHub token** and add a fine-grained PAT with *Contents: Read* on the repos your agents live in. Public repos work without a token; private repos don't. See [GitHub PAT Setup](../integrations/github-pat-setup.md).

### Step 3: Onboard (Per Agent)

```bash
/trinity:onboard            # asks what you want: deploy, adapt only, or onboard in place
/trinity:onboard analyze    # report only — no changes
/trinity:onboard in-place   # force the in-place path (see below)
```

Run it in the agent's directory. It analyzes the current state, then:

1. **Creates the Trinity files** it finds missing — `template.yaml` (with `plugins:`, `schedules:`, and credential declarations), `.env.example`, `.gitignore`, `.mcp.json.template`
2. **Checks GitHub readiness** — the token tier and a pushed remote — before any deploy runs
3. **Deploys** — from the repository by default (`create_agent` with `github:owner/repo@branch`); from a local archive as the fallback when the repo doesn't exist yet or the instance can't reach GitHub, with an offer to promote the agent onto the repo path afterwards
4. **Injects** gitignored credentials (`.env`) after deploy, since they are never in the clone or the archive
5. **Reconciles declared schedules** onto the instance, matching on schedule name
6. **Verifies** with the platform's own compatibility report — the report, not the skill's checklist, is the definition of "compatible"

Two things it will tell you and you cannot skip: declared schedules arm only on a literal `enabled: true`, and a newly created agent's **autonomy toggle is off** — until you turn it on in the UI, the scheduler skips every cron trigger for that agent.

There are three ways an agent gets onto Trinity:

| Path | When | What happens |
|------|------|--------------|
| **From the GitHub repo** (default) | The agent is adapted and pushed | Trinity clones the repo, tracks the branch, materializes declared schedules and plugins at creation |
| **From local files** (fallback) | No repo yet, air-gapped instance, or a throwaway | A snapshot of your directory is deployed; promote it onto the repo path with `initialize_github_sync` before it becomes long-lived |
| **Deploy as-is, then onboard in place** | A repo you can't or shouldn't adapt locally — someone else's agent, a bare repo with no `template.yaml` | Create the agent from the bare repo first (Trinity tolerates a missing `template.yaml`), then run `/trinity:onboard` *inside* that agent — see [below](#onboarding-a-deployed-agent-in-place) |

### Step 4: Sync (Ongoing)

```bash
/trinity:sync                       # status of every remote
/trinity:sync push [@remote] [branch]
/trinity:sync pull [@remote]
/trinity:sync deploy [@remote] <branch>
/trinity:sync remotes | add-remote <name> <agent> [branch] | set-default <name>
/trinity:sync schedules [@remote]   # reconcile template.yaml schedules: against the live agent
/trinity:sync plugins [@remote]     # reconcile template.yaml plugins: against what's installed
```

Sync is git-based and multi-remote — a `.trinity-remote.yaml` registry lets one repo serve several instances (production, staging), each tracking its own branch. `push` advances the deployed agent to your new commit; `pull` brings the agent's own commits back; `deploy` switches a remote to another branch.

`schedules` and `plugins` treat `template.yaml` as the design truth and the operator as owner of the live extras: they **create or install what is declared but missing, and report what is live but undeclared — never delete, never uninstall**. Both also run automatically after `push`, `pull`, and `deploy`, and read-only in `status`. A plugin installed by reconciliation loads on the agent's *next* execution.

## Onboarding a Deployed Agent In Place

When `/trinity:onboard` detects it is running *inside* a deployed Trinity agent — the agent's own workspace, with the platform's MCP tools already injected — it offers **Onboard in place** as the recommended goal (or take it directly with `/trinity:onboard in-place`, which is also the one-line form an orchestrator dispatches after deploying a bare repo). Nothing is created; the agent already exists. Instead it:

1. Writes the Trinity files — `template.yaml` (with `plugins:` declaring at least `trinity@abilityai`), `.env.example`, `.gitignore`, `.mcp.json.template` if the agent runs MCP servers of its own
2. **Installs the declared plugins now**, with the same CLI calls the container's boot hook uses, so they are present from the next execution rather than the next restart
3. **Commits and pushes the result back to the repo** — this step matters: a repo-deployed agent tracks its branch pull-only, so a file written in the container is lost on the next reset unless it reaches the repo. If the agent has no write credentials, the skill tries the platform's push path and, failing that, **stops and says so** — it prints the patch and states that the result is container-local, rather than pretending
4. Reconciles declared schedules live
5. Finishes with `get_agent_compatibility_report` — every HARD finding is yours to fix; SOFT and AI findings are advisory

**Bootstrap for agents that predate declared plugins.** The in-place path needs the `trinity` plugin present in the container. Agents created since Trinity re-installs declared plugins at boot get it from their `template.yaml`; an older agent has nothing declared yet, so run this once from its terminal, then start a fresh session:

```bash
claude plugin marketplace add abilityai/abilities && claude plugin install trinity@abilityai --yes
```

Onboarding in place is idempotent — if the push was refused, grant the agent write credentials and re-run.

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
| `template.yaml` | Trinity metadata (name, description, resources, declared schedules and plugins) |
| `.env.example` | Documents required environment variables |

Optional but recommended:

- `dashboard.yaml` — Custom metrics dashboard
- `.mcp.json.template` — MCP server configuration
- `template.yaml` `schedules:` — declared recurring work, materialized at creation
- `template.yaml` `plugins:` — declared Claude Code plugins, re-installed on every boot (declare `trinity@abilityai` at minimum)

The authoritative verdict is the platform's compatibility report (Agent Detail → Overview), which `/trinity:onboard` runs at the end of every path. See [Creating Agents](../agents/creating-agents.md).

## See Also

- [Agent Loops](../automation/agent-loops.md) — The server-side loops feature `/trinity:loop` drives
- [Trinity CLI](../cli/trinity-cli.md) — Command-line deployment
- [Creating Agents](../agents/creating-agents.md) — Agent creation in Trinity
- [Trinity Ops Agent](../guides/deploying/ops-agent.md) — Instance operations post-deploy
- [Abilities Overview](overview.md) — Full toolkit overview
