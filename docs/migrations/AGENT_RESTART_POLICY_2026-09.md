# Agent Restart Policy Migration (Issue #2541)

**Applies to**: existing Trinity deployments upgrading to the release that
contains issue #2541. Every agent container created **before** that release is
still on Docker's default restart policy, `no`, and will not come back after a
host reboot.

**Fresh installs**: skip this document. Every container Trinity creates is now
born `unless-stopped`.

---

## What changed

Docker's default restart policy is `no`: a container created without an explicit
`restart_policy` stays `Exited` after a host reboot or a daemon restart until
somebody starts it by hand. Before #2541 only `trinity-system` was created with
a policy, so an unplanned power-off on 2026-09-04 brought back 11 of 19 agents on
the reference deployment and left the other 8 — `marshal`, the fleet conductor,
among them — dead for ~42 hours: 48 failed dispatches, ~168 lost `fleet-poll`
runs, 17 schedules dark.

Since #2541:

| Path | Before | After |
|---|---|---|
| Agent create (`crud.py`) | no policy → `no` | `unless-stopped` |
| Config recreate (`lifecycle.py`) | carried the old container's policy forward (#1816) | `unless-stopped`, unconditionally |
| #1559 recovery rebuild (`lifecycle.py`) | passed nothing → `no` | `unless-stopped` |
| System agent (`system_agent_service.py`) | `unless-stopped` (literal) | `unless-stopped` (shared constant) |
| `backend` / `frontend` / `redis` in `docker-compose.yml` | **no `restart:` key** | `restart: unless-stopped` |

`unless-stopped`, never `always`. Trinity stops agents through
`container.stop()`, which sets Docker's manual-stop flag; `unless-stopped`
honours it, so an agent an operator deliberately stopped or quarantined stays
down across a reboot. `always` would resurrect it.

> ⚠️ **The flag is set only when the stop succeeds.** `POST /api/ops/emergency-stop`
> stops agents in parallel and reports **per agent** — an agent that returns
> `{"result": "error"}` is still running, and after this change it now also
> survives the next reboot, where before it would have died there by accident.
> Read the emergency-stop response and re-issue for any agent not reported
> `stopped`; a quarantine is not complete until every agent reports it.

---

## Why an upgrade step is required

A restart policy is **creation-time**. Setting it on the create path does
nothing for containers that already exist — they keep `no` until they are
recreated. A recreate happens on a config change, a base-image adoption, a
`POST /api/ops/fleet/restart`, or a #1559 recovery rebuild — so an active fleet
heals itself eventually, and this runbook is "don't wait for one".

The one-shot sweep below is genuinely one-shot, not a treadmill: the create-path
fix **closes the population**. After this release the set of `restart=no` agent
containers is finite and can never grow.

**Platform services**: if you run the base `docker-compose.yml` (i.e.
`./scripts/deploy/start.sh` without `--hosted` — the README quickstart and the
source install), your `backend`, `frontend` and `redis` containers also carry
`no`. They adopt the new policy on the next `docker compose up`, which
`start.sh` performs; no manual step is required for them.

---

## Upgrade procedure

Run **after** deploying the release, not before. In that order the containers
you touch are already served by a fixed create/recreate path, so the update is
durable.

### 1. Census — what is still on `no`

```bash
for c in $(docker ps -a --format '{{.Names}}' | grep '^agent-'); do
  docker inspect -f '{{.Name}} {{.HostConfig.RestartPolicy.Name}}' "$c"
done
```

### 2. One-shot fix

```bash
docker ps -a --format '{{.Names}}' | grep '^agent-' | while read -r c; do
  [ "$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$c")" = "no" ] &&
    docker update --restart unless-stopped "$c"
done
```

`docker update --restart` on a **stopped** container changes the policy and does
**not** start it. Verified: `docker create --restart no` → `policy=no
state=created`; after `docker update --restart unless-stopped` → `policy=unless-stopped
state=created`. So a deliberately stopped agent stays stopped through this sweep.

Use `unless-stopped`, never `always` — see above.

### 3. Verify

```bash
# expect: every agent-* on unless-stopped
for c in $(docker ps -a --format '{{.Names}}' | grep '^agent-'); do
  docker inspect -f '{{.Name}} {{.HostConfig.RestartPolicy.Name}}' "$c"
done

# platform services
for c in trinity-backend trinity-frontend trinity-redis; do
  docker inspect -f '{{.Name}} {{.HostConfig.RestartPolicy.Name}}' "$c"
done
```

---

## Operational consequences — read before the next incident

### Never run `docker compose down` on a Trinity host

`scripts/deploy/stop.sh` uses `docker compose … stop`, **not** `down`, and says
why: `down` tears down `trinity-agent-network`, which every agent container is
attached to. A recreated network gets a **new id**, and pre-existing agent
containers then fail `docker start` with `network … not found`.

Before #2541 that bit exactly one container — `trinity-system`, the only one
carrying a policy — and it bit *once*, loudly: the start failed and the container
stayed down. After #2541 it bites **every** agent, and *automatically*: dockerd
retries per the restart policy in a backoff loop. Compounding it, a `restarting`
container now reports as `stopped` to the roster, so the fleet reads quiescent
while dockerd churns.

**Remedy**: `docker rm -f` the stale agent containers and let Trinity recreate
them (`POST /api/agents/{name}/start`, or `POST /api/ops/fleet/restart`). The
workspace volume holds the agent's data and is untouched by a container removal.

### "Agent X restarting frequently" alerts are new

Docker increments `RestartCount` only under a restart policy, so
`monitoring_service`'s `High restart count (N)` health issue and its
`alert_high_restart_count` operator alert have effectively never fired for a
regular agent. Both are now live fleet-wide. That is the detection that makes a
crash loop visible — but it is a notification no operator has seen before.

A restart storm is unlikely by construction: the base image's `CMD` is
`startup.sh`, which has no `set -e`, whose only `exit 1`s are `cd` guards, and
which ends at `tail -f /dev/null` — PID 1 effectively never exits. But if a
container *does* die early (OOM against its `mem_limit`, a bad env, an image
regression), `unless-stopped` re-runs the whole of `startup.sh` — git clone/pull,
credential injection, `.mcp.json` rendering, plugin installs — against GitHub and
the plugin marketplace on **every** retry. The alert above is what turns that
from silent amplification into a page.

### A daemon-driven restart skips the start ladder

`start_agent_internal` runs nine drift predicates, MCP-key healing, a readiness
wait, and credential / skill / read-only-hook injection. A restart the Docker
daemon performs runs **none** of it. That is safe for the common case — the
workspace volume persists `.env` and `.claude/skills/` across container starts,
and the 11 survivors of the 2026-09-04 reboot are the empirical proof — but a
**config-drifted** agent now returns on its stale config instead of staying down
until a start recreates it. If an agent comes back "on the old base image" after
a reboot, that is this, and `POST /api/ops/fleet/restart` (#1912) is the remedy.

### What this does NOT cover

Docker's restart policy is not a desired-state reconciler. After #2541 it is
Trinity's only convergence mechanism for "the process or the host died". It does
**not** cover a container that was **removed** (only the #1559 recovery rebuild
does, and it is reached solely from `start_agent_internal`, never autonomously),
nor a container that **runs but whose agent is wedged**. If the next incident is
one of those, it is a different gap — not a third occurrence of this one.

---

## Reference

- Requirements: `docs/memory/requirements/infrastructure.md` → **8.5d Container Restart Policy (#2541)**
- Flow: `docs/memory/feature-flows/container-capabilities.md` → Container Restart Policy
- Precedent: `docs/migrations/NON_ROOT_CONTAINERS_2026-05.md` (same shape — a
  creation-time property that existing containers must be swept for once)
