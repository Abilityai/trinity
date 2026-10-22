# Sizing Your Server

Choose the memory, CPU and disk for a Trinity server, estimate it for a fleet you have not built yet, and know when to grow it. Every agent runs as its own container on one host, so the host is the whole budget.

## When to Use This

- You are about to create a server for Trinity and need to pick a size.
- You are recommending a size to someone else, such as a customer or a team, and need the questions to ask.
- An existing instance feels slow, a build failed for lack of disk, or an agent was killed for running out of memory, and you need to know whether the server is too small.

## The Short Answer

Start on **4 vCPU / 8 GB RAM / 80 GB disk**. It is the size the [DigitalOcean installer](digitalocean.md) creates (about $48/month) and runs a typical first fleet comfortably. Move to **4 vCPU / 16 GB / 150 GB** once several agents regularly run at the same moment, or once agents build code, index repositories or drive a browser.

The number of agents you *have* barely matters. An agent that is not running anything uses about 50–100 MB of memory and about 1% of one CPU core. What sizes the server is **how many agents run at the same time, and how heavy those runs are**.

## Size Tiers

| Tier | RAM | vCPU | Disk | Observed fit |
|---|---|---|---|---|
| Evaluation | 4 GB | 2 | 50 GB | The platform plus one or two agents, one run at a time. Fine for trying Trinity, not for production. |
| **Start here** | **8 GB** | **4** | **80 GB** | Up to about 8 agents, about 2 runs at a time. |
| Busy fleet | 16 GB | 4 | 150 GB | Up to about 20 agents, about 6 runs at a time, no more than 2 of them heavy. |
| Heavy workloads | 32 GB | 4 | 150–200 GB | Up to about 5 heavy runs at a time (research, scraping, large data processing). |
| Large fleet | 32 GB | 8 | 200 GB+ | 40 agents or more, up to about 16 runs at a time. |

The "observed fit" column is what we have measured on running Trinity instances. It is a guide, not a supported limit. Your fleet's mix of light and heavy runs matters more than the agent count.

Closest provider sizes (check current prices with the provider):

| Tier | DigitalOcean | AWS | Google Cloud | Hetzner Cloud |
|---|---|---|---|---|
| Evaluation | `s-2vcpu-4gb` | `t3a.medium` | `e2-medium` | CX22 |
| Start here | `s-4vcpu-8gb` | `t3a.large` (2 vCPU) | `e2-standard-2` (2 vCPU) | CPX31 |
| Busy fleet | `s-8vcpu-16gb` | `t3a.xlarge` | `e2-standard-4` | CPX41 |
| Heavy workloads | `m-4vcpu-32gb` | `r7i.xlarge` | `e2-highmem-4` | CCX33 |
| Large fleet | `g-8vcpu-32gb` | `m7i-flex.2xlarge` | `e2-standard-8` | CCX33 |

Where a provider's nearest size has fewer vCPUs than the tier, memory is what we matched on: for a light fleet the 2-vCPU size works, with less headroom for bursts. Trinity images are `linux/amd64` only. ARM instances (AWS Graviton, Hetzner CAX) do not run it.

## How Trinity Uses the Server

**Platform services.** The backend, frontend, scheduler, MCP server, database, Redis and log collector use about 0.8 GB together on a quiet instance and up to about 1.6 GB on one running 40+ agents. The operating system and Docker add about 0.5 GB. Plan for **1.5 GB before any agent runs**.

**Idle agents.** An agent that is deployed but not running anything holds about 50–100 MB of memory and almost no CPU. Twenty idle agents cost about 1–2 GB.

**Running agents.** This is where the memory and CPU go. While an agent runs a task, Claude Code and whatever tools it starts are active inside its container:

| Kind of run | Memory | CPU |
|---|---|---|
| Typical: chat, writing, API calls, light scripting | about 0.5 GB | about 0.5 core |
| Heavy: building code, indexing a repository, data processing, a headless browser | up to the agent's limit (4 GB by default) | up to the agent's limit (2 cores by default) |

**Agent limits are ceilings, not reservations.** Each agent has a memory and CPU limit (default 4 GB and 2 cores, set under **Settings › General › Execution**; see [Agent Configuration](../../agents/agent-configuration.md)). Docker does not set that memory aside. Twenty agents with 4 GB limits run fine on a 16 GB server, because they are almost never all busy at once. If one agent's run goes past its own limit, only that agent's container is stopped. Neighbouring agents and the platform keep running.

**One agent can run several tasks at once.** Each agent runs up to 3 tasks in parallel by default (an admin can raise the ceiling). Agent × parallel tasks is the theoretical upper bound on concurrent runs. In practice the busiest moment is usually a cluster of scheduled jobs that start at the same time.

**CPU above the host's count is capped.** If an agent (including the built-in system agent, which asks for 4) is set to more CPUs than the server has, it gets the server's CPU count instead and a warning is logged. A 2-vCPU server works, with the limits that implies.

**Disk is the resource that runs out first.** Across the instances we operate, a full disk has caused far more incidents than memory or CPU. Disk holds:

- Docker images: 10–25 GB, more while an upgrade builds new images next to the old ones.
- Logs collected by Vector: typically around 10 GB, much more on a busy instance with long retention.
- Agent workspaces: 100–200 MB for an ordinary agent, several GB for agents that keep scraped data or large git repositories, and occasionally tens of GB.
- The database: usually under 5 GB.

## Estimate It Yourself

Use this when the tiers do not match your fleet.

```
RAM (GB)  ≈ 1.5 + 0.1 × agents + 0.5 × typical runs at once + 4 × heavy runs at once
            then add 25% for headroom and file cache

vCPU      ≈ 0.5 + 0.5 × typical runs at once + 2 × heavy runs at once

Disk (GB) ≈ 25 (images) + 10 (upgrade headroom) + 10 (logs) + the sum of agent workspaces
            then add 50% for growth
```

Round up to the next size your provider sells.

**Example: a team assistant fleet.** 12 agents doing research and writing. At the busiest hour about 4 run at once, none heavy. RAM ≈ 1.5 + 1.2 + 2 = 4.7 GB, plus 25%, about 6 GB. vCPU ≈ 0.5 + 2 = 2.5. **Start here** (8 GB / 4 vCPU) fits.

**Example: an engineering fleet.** 15 agents. Up to 6 run at once, and 2 of those build and test code. RAM ≈ 1.5 + 1.5 + 2 + 8 = 13 GB, plus 25%, about 16 GB. vCPU ≈ 0.5 + 2 + 4 = 6.5. **Busy fleet** on memory, but the CPU estimate says the builds will be slower on 4 vCPU. Take 8 vCPU if build time matters.

## Recommending a Size

When someone asks "what server do I need?", ask these in order:

1. **How many agents will stay deployed?** This sets the floor for idle memory and for disk.
2. **How many will be running at the same moment, at the busiest time?** Count overlapping schedules, not only chats. Five agents scheduled at 09:00 means five runs at once.
3. **Will any agent run several tasks in parallel?** The default is up to 3 per agent.
4. **Do any runs build code, index repositories, process large files or drive a headless browser?** Each of those counts as a heavy run.
5. **Do agents fan out work to other agents or start temporary agents?** Count those as additional runs at the same moment.
6. **Will agents keep data on disk: scraped pages, datasets, large repositories?** Size the disk for it.

Then take the matching tier, or use the estimate above. If unsure, start on **Start here** and plan to grow: resizing is a stop, resize and start on every major provider, and Trinity keeps all its state on disk across the restart.

Avoid promising:

- A fixed number of agents per size. It depends on how many run at once.
- Response times. Most of a run's time is spent waiting on the model, not on the server.
- Automatic scaling. Trinity runs on one server, and you resize it yourself.

## When to Grow

Check these against the thresholds in [Monitoring → Check Resource Thresholds](monitoring.md#step-2-check-resource-thresholds):

| Signal | How to check | What it means |
|---|---|---|
| Disk use above 80% | `df -h /`, `docker system df` | Clean up first: old images (`docker image prune`), log retention, large agent workspaces. Then grow the disk. Disk can usually grow on a running server but cannot shrink. |
| Host memory above 85% while agents run | `free -h`, `docker stats --no-stream` | Too many runs at once for the RAM. Move up a tier, or spread schedules apart. |
| One agent keeps getting killed for memory | The agent's logs, `docker inspect <container>` (`OOMKilled`) | That agent needs a higher limit, not a bigger server. Raise it under **Settings › General › Execution** if the host has room. |
| Load average above the vCPU count for long stretches | `uptime` | CPU-bound. Runs take longer and health checks may time out. Add vCPUs. |
| Agents at their CPU limit during long runs | `docker stats --no-stream` | Raise that agent's CPU limit if the host has spare cores. |

A server that stays below 20% CPU and uses under a quarter of its memory for weeks is a candidate to move down a tier.

## See Also

- [Deploy on DigitalOcean](digitalocean.md) and [Deploy on AWS](aws.md): create a server of the size you picked
- [Single-Server Deployment](single-server.md): any other Linux server
- [Agent Configuration](../../agents/agent-configuration.md): per-agent memory, CPU and parallel task limits
- [Monitoring](monitoring.md): resource thresholds and disk cleanup
