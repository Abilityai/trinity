# agent-dev Plugin

Development tools for extending existing agents — skills, memory systems, git-backed state, a GitHub Issues development cycle, long-running pipelines, multi-agent orchestration, a shared canonical-data layer, and fleet analysis and migration.

> 📺 **Watch:** [Why Every AI Agent Needs a GitHub Repo](https://youtu.be/R4nNHf6ywEs) *(Apr 2026)* · [3 AI Agents Run My Software Development Pipeline](https://youtu.be/zCDFDhewFkk) *(Apr 2026)* · [all videos](../videos.md)

## Installation

```bash
/plugin install agent-dev@abilityai
```

## Skills

| Skill | Description |
|-------|-------------|
| `/agent-dev:create-playbook` | Create a new skill/playbook for the agent |
| `/agent-dev:adjust-playbook` | Modify an existing skill/playbook |
| `/agent-dev:add-memory` | Add a memory system (file-index, brain, json-state, workspace) |
| `/agent-dev:add-git-sync` | Add git-as-state hooks — auto-commit on stop, rebase on session start |
| `/agent-dev:add-backlog` | Install the full GitHub Issues development cycle into the agent |
| `/agent-dev:backlog` | View the agent's current GitHub Issues backlog |
| `/agent-dev:claim` | Claim the next issue — picks the highest-priority todo, marks it in-progress |
| `/agent-dev:autoplan` | Analyze a claimed issue before implementing — affected files, changes, risks |
| `/agent-dev:commit` | Commit changed skill files and close the in-progress issue with traceability |
| `/agent-dev:close` | Close the current issue without a commit (use `/commit` when files changed) |
| `/agent-dev:groom` | Groom the backlog — label untagged issues, verify priorities, surface stale work |
| `/agent-dev:roadmap` | Strategic view — open issues grouped by skill area |
| `/agent-dev:sprint` | Human-supervised cycle: roadmap → claim → autoplan → implement → commit |
| `/agent-dev:work-loop` | Autonomous unit of work: pick one issue, execute, close, exit |
| `/agent-dev:plan` | Plan and execute a large multi-session project |
| `/agent-dev:add-pipeline` | Scaffold a long-running multi-stage pipeline inside the agent |
| `/agent-dev:add-pipeline-instance` | Add an instance (tenant / zone / case) to an existing pipeline |
| `/agent-dev:add-pipeline-stage` | Append a stage to an existing pipeline definition |
| `/agent-dev:validate-pipeline` | Lint a pipeline.yaml — schema, DAG acyclicity, referenced skills |
| `/agent-dev:add-orchestrator` | Make the agent a system-aware orchestrator — discover the fleet, compose systems, route and fan out work; `--check` reports installed-vs-bundled drift before you overwrite |
| `/agent-dev:add-canon` | Give the agent a shared canonical-data layer — a fleet-wide git repo with publish, consume, reconcile, and doctor skills |
| `/agent-dev:add-canon-lint` | Install deterministic (no-LLM) consistency linting into that canon repo, with a CI workflow |
| `/agent-dev:add-project-management` | Install cross-actor project management — GitHub Issues as the source of truth, an approval-ready completion lattice, and a project steward |
| `/agent-dev:agent-fleet-analysis` | Audit a directory of agents in any paradigm, score maturity and migration readiness, and emit a PDF report plus an agent-executable work order |
| `/agent-dev:agent-fleet-migrate` | Execute that work order non-destructively into a verified Claude Code fleet, with a before/after maturity report |

### Fleet-scale skills

The last five are about a *set* of agents rather than one.

**Canon** (`add-canon`, `add-canon-lint`) gives a fleet a shared, citable source of truth on plain git — no new platform primitive. Each agent owns a folder it publishes to and reads other agents' folders at a pinned ref; the lint pass keeps the claims structurally consistent and runs in CI. The layer also carries **relations** — per-counterpart collaboration memory: each agent keeps one doc per agent it actually works with (working agreements, a capped log of recent events, open threads), reads it before acting on that counterpart's request and appends the outcome before closing the interaction. Each side keeps its own view; a divergence between the pair is a dropped-thread signal, and open threads older than 30 days surface as needs-review rows in the reconcile pass.

**Project management** (`add-project-management`) installs a cross-actor task model on GitHub Issues, with an `open → pending-verification → done` lattice so work isn't marked complete until it's been verified, and a **loop-closure** discipline: no loop closes by silence in either direction. Every run ends by stating what is now true, what is waiting on the operator, and what happens next; work parked on someone the system cannot dispatch to (a client, a vendor, a colleague) is labeled `waiting-on:<actor>`, aged on a 3/7/14-day ladder, listed under **Your open loops** in the digest, and handed back with a drafted follow-up — the agent drafts, the human sends.

**Fleet analysis and migration** (`agent-fleet-analysis`, `agent-fleet-migrate`) are a pair. The first scans agents written in *any* paradigm — Claude Code, n8n workflow exports, LangChain/CrewAI/AutoGen applications, hand-rolled loops — and produces both a human-readable report and a work order an agent can execute. The second carries out that work order into a fresh `fleet-migrated/` tree; your original sources are never mutated, and each migrated agent passes a review gate before the run reports success.

## Memory Systems

Add persistent memory to agents via `/agent-dev:add-memory`:

| System | Purpose | Use When |
|--------|---------|----------|
| **file-index** | Workspace file awareness and search | Agent needs to know what files exist |
| **brain** | Zettelkasten-style knowledge graph | Agent builds connected notes over time |
| **json-state** | Structured JSON state with jq updates | Agent tracks counters, config, or structured data |
| **workspace** | Multi-session project tracking | Agent works on long-running projects |

Memory systems are copied directly into the agent — no plugin dependency at runtime.

## Git Sync (git-as-state)

```bash
/agent-dev:add-git-sync
```

Installs hooks that treat the agent's own repository as durable memory:

- **Auto-commit on Stop** — work-in-progress is committed when a session ends
- **Rebase on SessionStart** — each session begins from the latest remote state
- **Snapshot on PreCompact** — state is preserved before context compaction

This gives agents durable cross-session memory through their repo, complementing Trinity's [GitHub Sync](../integrations/github-sync.md) on the platform side.

## Playbook Development

### Create a New Playbook

```bash
/agent-dev:create-playbook
```

Guides through:

1. **Purpose** — What the playbook accomplishes
2. **Triggers** — When it should run
3. **Steps** — The workflow steps
4. **State** — What data it reads/writes

It picks a complexity tier and generates from a bundled template:

| Tier | Shape | Template |
|------|-------|----------|
| 1 — Simple skill | Stateless procedure, no persistent data | `simple-skill` |
| 2 — Stateful skill | Reads/writes agent state between runs | `stateful-skill` |
| 3 — Full playbook | Multi-step, scheduled or delegated work | `manual`, `gated`, or `autonomous` — by automation level |

Every generated skill carries a `metadata:` block with a newest-first changelog and a what's-new banner, and — for anything on a schedule — invokes by slash name so the scheduler message stays a bare call. Playbooks are written to be **called**: from a single `/name [args]` line, with inputs declared in `argument-hint`, running only themselves when invoked by another agent. See [Playbook calls](../automation/abilities-marketplace.md#playbook-calls-the-unit-of-inter-agent-work) for the convention, and the `--autonomous` run mode any gated playbook needs before it can go on a cron.

### Modify an Existing Playbook

```bash
/agent-dev:adjust-playbook daily-report
```

Options:

- Add/remove steps
- Change automation level
- Update schedule
- Fix issues
- **Make callable by other agents** — retrofit an existing skill to the playbook-call rule (declared arguments, one-line invocation, a headless mode if it has gates)

## GitHub Backlog Workflow

Add task management via GitHub Issues:

```bash
/agent-dev:add-backlog
```

This installs the full development cycle directly into the agent. After install, the agent has:

- `/backlog` — view current issues
- `/claim` — claim the next issue to work on
- `/autoplan` — analyze the claimed issue before implementing
- `/commit` — commit changes and close the issue with a traceable message
- `/close` — close without a commit
- `/groom` and `/roadmap` — keep the backlog labeled, prioritized, and surveyable
- `/sprint` — the human-supervised end-to-end cycle
- `/work-loop` — the autonomous variant (below)

### Autonomous Work Loop

```bash
/agent-dev:work-loop
```

One bounded unit of work: the agent picks the highest-priority issue, executes it, closes it, and **exits**. It is designed to be re-invoked — by a Trinity schedule for a steady cadence, or by a Trinity [agent loop](../automation/agent-loops.md) for a bounded burst ("drain up to 20 items, stop when the backlog is empty"). See the *Backlog draining* pattern in the Agent Loops guide.

## Pipelines (long-running multi-stage work)

```bash
/agent-dev:add-pipeline
```

Scaffolds an agent-owned pipeline for work that spans days or weeks (e.g. perception → synthesis → publish → measure): a `projects/<slug>/` directory with `pipeline.yaml` and per-instance state, tick/status/recover/pause/resume skills, a heartbeat schedule that advances stages, and a `~/.trinity/` read surface so Trinity can display pipeline state without owning it.

- `/agent-dev:add-pipeline-instance` — add a tenant/zone/case to an existing pipeline
- `/agent-dev:add-pipeline-stage` — extend the stage DAG
- `/agent-dev:validate-pipeline` — lint the definition (schema, acyclicity, skill references)

A stage can name a local skill or delegate to another fleet agent — `agent: <fleet-agent>` calls that agent's playbook as one line with `--run <pipeline>/<instance>`, dispatched asynchronously and polled for its result; such stages skip the local-skill check.

Pipelines are owned by the agent, not by Trinity — the platform only reads the published state. This matches Trinity's agent-defined-pipelines design: no central DAG engine.

## Orchestration (multi-agent)

```bash
/agent-dev:add-orchestrator
```

Makes any agent a system-aware orchestrator of other agents. Two modes, picked by whether the fleet already exists: **describe and route over an existing fleet** (read-only — discover, then orchestrate; no manifest, no deploy) or **provision a new system** (author the intent, compose a manifest, deploy, then orchestrate). It installs these skills into the agent:

| Skill | Purpose |
|-------|---------|
| `/discover-agents` | Discover the fleet (from the live Trinity instance and/or a repo list) into a descriptive `fleet/system-map.yaml` — including each agent's pipelines and canon declarations |
| `/compose-system` | Turn the map into a Trinity system manifest and deploy it; members are declared as `github:Org/repo` so the fleet is reproducible from source, and a spec-less repo gets a post-deploy `/trinity:onboard in-place` playbook call |
| `/orchestrate` | Route work to the right agent, fan the same task out across several, chain ordered steps, open a room, wire standing event reactions, or roll out an ephemeral agent for a one-off job — via Trinity MCP |
| `/sync-fleet-to-head` | Non-destructively bring in-scope agents to their GitHub HEAD |
| `/profile-fleet` | Interview and introspect agents, reconcile reality against the fleet narrative |
| `/fleet-reconcile` | Fold already-verified deltas into every doc surface behind one gate |
| `/project-init`, `/project-steward` *(opt-in)* | The project-management layer: create or adopt a managed project, and an autonomous steward that dispatches labeled work, escalates stalls, ages the operator's open loops, and writes a daily digest |

Three conventions run through the whole bundle:

- **Dispatch is a playbook call.** `/orchestrate` resolves each dispatch to a playbook from the target agent's *live* skill catalog and sends one line — `/<playbook> [args] --run <task_id>` — never a prose brief (a freeform brief is the recorded exception). Fire-and-park, never block-and-wait: it subscribes to the target's task-completion event and reports back when the work lands. See [Playbook calls](../automation/abilities-marketplace.md#playbook-calls-the-unit-of-inter-agent-work).
- **Standing wiring uses events, not pollers.** For "whenever X happens, have Y react", `/orchestrate` wires Trinity's pub/sub layer: the source agent's playbook is instructed to `emit_event` a named domain event, and the reacting agent subscribes *itself* (subscriptions are self-service, so the setup is dispatched to the subscriber, never wired on its behalf) with a `{{payload.field}}`-interpolated task. The design rules the platform does not enforce — exact-match event names, keep custom event graphs acyclic (only the built-in task-completion events carry a loop guard), a wake reaches only a running subscriber, interpolated payloads are agent-authored text — are written into the fleet narrative so the wiring stays reviewable. See [Event Subscriptions](../collaboration/event-subscriptions.md).
- **Loop closure.** A run is not done until the requester has been told the outcome, including failure; work parked on someone outside the fleet is labeled `waiting-on:<actor>`, aged, and handed back with a drafted follow-up. The agent drafts; the human sends.

**Re-running the installer.** `/agent-dev:add-orchestrator --check` is a read-only divergence report — per installed skill it says whether the bundle moved ahead (upgrade available), whether the installed copy was hand-edited (an overwrite would discard it — the diff is shown), or whether the installed copy is *ahead* of the bundle (a field-hardened copy the marketplace should pull from — an overwrite would be a downgrade). The same comparison runs inside every overwrite prompt when you re-run the installer, so the warning arrives at the moment of decision. It compares only this bundle's own skills and keeps no state on disk.

**Gated skills on crons.** `/sync-fleet-to-head` and `/profile-fleet` ask questions at their decision points; put on a schedule as-is, each run would block on a prompt nobody sees. Both declare a `--autonomous` run mode — the schedule message is the bare `/<skill> --autonomous`, and the skill takes the safe default at every gate, never a destructive path, recording anything non-trivial as a `needs-attention` line. This is the bundle-wide convention every gated-and-scheduled skill follows.

The orchestrator builds on Trinity's existing multi-agent primitives rather than inventing a parallel standard — see [System Manifest](../collaboration/system-manifest.md), [Fan-Out](../automation/fan-out.md), and [Rooms](../collaboration/rooms.md). Pipelines are the intra-agent sibling: `/orchestrate` routes pipeline-shaped work to the agent that owns the pipeline instead of re-sequencing its stages as a chain; canon is the data sibling: reads of published facts are served from the canon repo, writes are dispatched to the owning agent.

## Multi-Session Planning

For large projects that span multiple sessions:

```bash
/agent-dev:plan
```

Creates a persistent plan that tracks:

- Overall goals and milestones
- Current session focus
- Completed work
- Next steps

## See Also

- [create-agent Plugin](create-agent-plugin.md) — Create new agents
- [trinity Plugin](trinity-plugin.md) — Deploy, sync, and loop agents on Trinity
- [Agent Loops](../automation/agent-loops.md) — Drive `/work-loop` in bounded autonomous bursts
- [Skills and Playbooks](../automation/skills-and-playbooks.md) — How skills work in Trinity
- [Abilities Overview](overview.md) — Full toolkit overview
