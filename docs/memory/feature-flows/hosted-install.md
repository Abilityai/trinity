# Feature Flow: Prebuilt Images & Pull-Only Hosted Install (#2280)

> **Status**: 🟡 Partial (2026-08-24) — publish workflow, hosted compose, `start.sh --hosted`, TLS decision and docs landed; the cloud-init user-data example (AC4) lives in `trinity-ops-public` and is outstanding · **Issue**: #2280 (epic #2332) · **Requirements**: [infrastructure.md §8.9](../requirements/infrastructure.md)

## Overview

A fresh VM comes up serving Trinity in ~2 minutes with **no on-box builds**: five images are published to GHCR on every release tag, and `scripts/deploy/start.sh --hosted` pulls them instead of compiling.

**Why it exists.** `docker-compose.prod.yml` carried `build:` blocks and `start.sh` compiled `trinity-agent-base` on the host — Python + Node + Go + Claude Code, ~1.9 GB, 5–10 minutes. That fails the "one click" bar for every marketplace channel and is rejected outright by managed hosts and template catalogues (Elestio, Dokploy, Coolify, Hostinger Docker Manager), all of which accept pull-only compose files only. This is the gate the whole hosting arc sits behind: #2281 (DigitalOcean Marketplace), #2282 (Vultr), #2283 (Hostinger/Dokploy), #835 (Packer).

**Scope, stated honestly.** Pull-only means it does not **build** — it still needs the repository checked out beside it. The backend mounts four `./config/*` directories, Vector and the OTel collector mount their configs, and `/data` is a relative bind mount; handed to a catalogue without the repo, Docker creates every one of those as an empty directory and the install comes up with an empty template catalog and no first-run manifests, **with no error**. `docker-compose.hosted.yml`'s own header says so. Packaging the config into the images so the file can travel alone is a follow-up.

## User Story

As an operator provisioning a server (by hand, from a marketplace image, or from cloud-init), I run one command and get a serving Trinity in about two minutes, pinned to a release I chose — without a toolchain, a build cache, or a five-minute wait on the machine least able to absorb one.

## Entry Points

| Trigger | Where | Notes |
|---|---|---|
| `git push` of a `v*` tag | `.github/workflows/publish-images.yml` | Publishes all five images at `v0.9.0` / `0.9.0` / `0.9` / `latest` / `sha-<short>` for one digest |
| `workflow_dispatch` (optional `ref:`) | same workflow | **Smoke build only** — publishes `sha-<short>` and nothing else (see *Tag gating* below) |
| `./scripts/deploy/start.sh --hosted` | `scripts/deploy/start.sh` | Or `TRINITY_HOSTED=1`. Same script as the source install; only the image source differs |
| `./scripts/deploy/stop.sh` | `scripts/deploy/stop.sh` | Reads the running stack's own compose label to pick the right `-f`, then `stop` (never `down`) |

## Publish Layer — `.github/workflows/publish-images.yml`

Five matrix jobs, one per Trinity-built image: `trinity-backend`, `trinity-frontend`, `trinity-scheduler`, `trinity-mcp-server`, `trinity-agent-base`.

| Property | Contract |
|---|---|
| **OSS-only by construction** | `checkout` sets `submodules: false`, so `src/backend/enterprise` is empty in the build context. A structural property of the workflow, not a `.dockerignore` rule someone can edit; `build-without-submodule.yml` already proves the OSS tree builds standalone |
| **Provenance from the checked-out tree** | `VERSION`, `git rev-parse HEAD`, subject, timestamp and `inputs.ref \|\| github.ref_name` — never `github.sha`/`GITHUB_REF_NAME`, which describe the ref the workflow was *dispatched from*. A dispatch of `ref: v0.9.0` from `dev` would otherwise stamp `GIT_BRANCH=dev` into `GET /api/version` on the exact path meant for out-of-band builds |
| **Build args are per image** | Only `docker/backend/Dockerfile` declares the six provenance ARGs; `trinity-agent-base` takes `VERSION` alone (mirroring `build-base-image.sh`, its only other producer); scheduler and mcp-server declare none. The frontend takes **none** — every `VITE_*` is inlined into the world-readable bundle at build time, so a published image may carry only values correct for *every* install |
| **Injection-safe** | Every interpolated value arrives through `env:`, never inside a `run:` body. The commit subject is attacker-chosen text on a runner holding a `packages:write` token |
| **Actions SHA-pinned** | As everywhere else in this repo. A mutable tag on an action that holds a registry-write token is a supply-chain hole |
| **amd64 only (v1)** | DO Droplets and Vultr Cloud Compute both default to amd64. Cross-building the ~1.9 GB agent base under QEMU is slow and flaky; arm64 is gated on Hetzner CAX / Umbrel actually needing it |

### Tag gating (the `push`-only rule)

`flavor: latest=false` is load-bearing, not decoration: `docker/metadata-action`'s default is `latest=auto`, which applies `latest` on **any** semver tag ref on its own — so gating only the explicit `type=raw,value=latest` line would have been inert and the comment above it would have described a rule the workflow did not enforce.

Every mutable/version tag is then gated on `github.event_name == 'push'`. `github.ref` alone answers *"is this a tag ref"*, not *"is this a release"*: a `workflow_dispatch` started from a tag — the natural way to smoke-test this workflow — carries `refs/tags/v0.9.0` too. Dispatching an **old** tag rebuilds at a new digest (the agent base bakes `Claude Code (latest)`, so the content genuinely differs), republishes `0.9.0`/`v0.9.0`/`0.9` over a supposedly immutable version, and walks `latest` **backwards** — silently downgrading every unpinned hosted install on its next `start.sh --hosted`. A dispatch therefore publishes `sha-<short>` and nothing else, which is exactly what a smoke build needs; re-cutting a release is done by re-pushing the git tag, which fires `push` and takes the full set.

Both `0.9.0` **and** `v0.9.0` are published for one digest: `{{version}}` strips the leading `v`, so a release cut as git tag `v0.9.0` published only `0.9.0` while every doc, the compose header and `start.sh`'s own pin warning told operators to set `TRINITY_IMAGE_TAG=v0.9.0` — a pull that fails `manifest unknown`, which `start.sh` treats as fatal with a message blaming the operator's spelling.

`type=sha` is **not** used; the sha tag is a raw value from the actual HEAD, for the same dispatch reason as provenance.

### Verify anonymous pull

A container package first created by `GITHUB_TOKEN` is not reliably public. If any of the five lands private, `docker pull` on a fresh VM fails with `denied` — and `start.sh` has no way to tell that apart from a network fault, so it reports it to an operator who can do nothing about it. The last step therefore proves the operator's exact path where it is cheap: an **anonymous** `docker manifest inspect` with a scratch `DOCKER_CONFIG`, so the runner's own `ghcr.io` login cannot mask a private package. It runs after the push and fails the job on purpose — the image is already published; a red step is the one-time signal to flip visibility to Public.

**The reference is lowercased in the shell body.** This owner is literally `Abilityai` and a GHCR repository name must be lowercase: docker rejects the mixed-case reference *locally*, before any network call (`repository name must be lowercase`), so the retry loop would burn out and the step would fail on **every** publish — reporting a perfectly public package as private and destroying the one signal it exists to give. The push itself is unaffected (`docker/metadata-action` lowercases its `images:` input, and `start.sh` hardcodes `ghcr.io/abilityai/`); a hand-written reference has to do it itself.

## Compose Layer — `docker-compose.hosted.yml`

`docker-compose.prod.yml` with every `build:` block replaced by a GHCR `image:` and **nothing else changed** — same `.env` contract, ports, volumes, networks and security posture. Image selection is `${TRINITY_IMAGE_TAG:-latest}`, so an operator pins a release without editing the file.

**The two files are CI-guarded, never trusted.** `tests/unit/test_2280_hosted_compose_parity.py` fails the build when hosted and prod disagree on the service set, any third-party image pin, the top-level volumes/networks, or **any** per-service value — the service comparison is **wholesale** (`prod` minus `{build, image}` vs `hosted` minus `{image}`), not a key allowlist. It began as an 18-key list that omitted `profiles`, `env_file`, `logging`, `labels`, `deploy` and `stop_grace_period`; `profiles` is live today on `cloudflared`, so a prod change that profile-gated or un-gated a service would have drifted silently past the guard whose entire purpose is catching that. An allowlist watches only the keys someone remembered, and the drift that matters is the key nobody thought of.

Two compose files describing one platform is exactly the shape of a bug this repo has shipped five times and named: `LOG_*` (#1039), the VoIP master switch (#1056), `AGENT_AUTH_SECRET` (#1707), the container log caps (#1871), and `ADMIN_USERNAME` (#2381). The guard compares the **raw** YAML, not `docker compose config` output — the raw form still holds the unexpanded `${VAR:-default}` strings, so a changed default is a diff, where the resolved form would silently agree whenever the local `.env` happens to match.

## Install Layer — `scripts/deploy/start.sh --hosted`

One script, two image sources. Secret generation, the `ADMIN_PASSWORD` contract (#2381), `DOCKER_GID` detection, the serving health poll and the next-steps card are shared; a parallel installer would be the parity bug class one layer up.

```
--hosted / TRINITY_HOSTED=1
  ├─ COMPOSE_FILES=(-f docker-compose.hosted.yml)      # explicit -f ⇒ NO auto-merge
  ├─ pre-flight (docker daemon, compose v2, FRONTEND_PORT probe)
  ├─ .env bootstrap + secret generation + ensure_docker_gid
  ├─ resolve_image_tag()      shell/CI > .env > latest
  ├─ TUNNEL_TOKEN set → COMPOSE_FILES+=(--profile tunnel) + persist COMPOSE_PROFILES to .env
  ├─ data-switch guard (BOTH directions — refuse, never warn)
  ├─ docker pull ghcr.io/abilityai/trinity-agent-base:$TAG
  │     └─ docker tag … trinity-agent-base:latest       # fatal on failure, never falls back to building
  ├─ Docker-Desktop log-source override appended BY NAME (auto-merge is off)
  ├─ docker compose -f … pull      # tailored fatal naming the three likely causes
  ├─ docker compose -f … up -d
  └─ serving health poll → hosted-specific next-steps card (+ pin warning on `latest`)
```

### The parts that are not obvious

| Concern | Resolution |
|---|---|
| **The agent base image is not a compose service** | The backend creates agent containers from the literal local tag `trinity-agent-base:latest` (hardcoded in `services/agent_service/lifecycle.py`, allowlisted as `trinity-agent-base:*` by SEC-172), and compose cannot retag. So hosted mode pulls the GHCR copy and `docker tag`s it locally. Retagging rather than repointing keeps SEC-172 and the #1809/#1816 image-drift check untouched — both read the container's own `Config.Image`, which stays `trinity-agent-base:latest` either way. **A bare `docker compose -f docker-compose.hosted.yml up -d` starts a platform that cannot create a single agent**, and it surfaces later as a missing-image error at agent-create time. Hence: the upgrade instruction is "re-run `start.sh --hosted`", never "pull" |
| **The pin is read from `.env`** | Precedence is shell/CI → `.env` → `latest`. `.env` is the only place an `--unattended` or marketplace install can persist config, and exporting an unconditional default instead — as this first shipped — silently beat the operator's own line, because compose gives the shell environment precedence over `.env`. The summary then reported `Currently pinned to: latest` as though they had chosen it |
| **A failed pull is fatal** | Falling back to a local build would reintroduce the 5–10 minute first boot the mode exists to avoid, on the install least able to absorb it. Both pull failures carry a tailored message naming the likely causes (tag not published, GHCR package still private, no route to ghcr.io) |
| **Auto-merge is off, so side effects are named** | The Docker-Desktop Vector log-source fix (#1432) is appended **by name**. Forcing it off under `--hosted` (as this first shipped) meant a hosted install on any VM-backed runtime shipped the very `docker_logs` source #1432 exists to avoid, and Vector busy-loops and pegs the Docker VM. The `vector` service is byte-identical between the files, so the override merges cleanly onto either |
| **The tunnel actually starts** | `cloudflared` is profile-gated, so a non-empty `TUNNEL_TOKEN` alone starts nothing. `--hosted` reads the token and appends `--profile tunnel`, then **persists** `COMPOSE_PROFILES` to `.env` — compose acts only on services in the active profile set, so without it every command the summary prints (`stop`, `logs -f`) silently excludes the tunnel container and leaves the instance publicly reachable after the operator has been told the stack is down |
| **`FRONTEND_PORT` is honoured in prod too** | It was hardcoded `"80:8080"` in prod, while `start.sh` offers `FRONTEND_PORT` as the remedy for a port conflict and prints the resulting URL. Fixed in both files rather than only in hosted — diverging them would defeat the parity guard |

### Data-switch guard (both directions)

Dev and hosted share a compose project name but **not** a `/data` source: `docker-compose.yml` mounts the named volume `trinity-data`, hosted binds `${TRINITY_DATA_PATH:-./trinity-data}`. So `--hosted` in a checkout that has been running the dev stack would come up on an **empty** database and migrate from zero while the real one sat untouched in the volume — with Redis, a named volume both files share, **not** reset, i.e. a half-migrated install carrying live session and lock state pointing at rows that no longer exist.

Both crossings are refused with the copy command, not warned about: the failure is silent, and by the time it is noticed the fresh DB may already have been written to. `TRINITY_DATA_PATH` is the documented escape for a deliberate fresh start.

The project name is derived by **compose's own rule** (`compose_project_name()`: lowercase → keep `[a-z0-9_-]` → trim leading `_`/`-`, `COMPOSE_PROJECT_NAME` winning, shell over `.env`). The two disagreeing derivations it replaces stripped `_` and `-`, which compose keeps — so in a checkout named `project_trinity`, `trinity-dev`, or a worktree like `trinity-2280`, the derived name matched no real volume and this guard failed **open** on exactly the directory names most likely to be in use.

## Stop Layer — `scripts/deploy/stop.sh`

Hosted mode passes an explicit `-f`, which disables compose's default file merge — so a bare `docker compose` in the checkout loads the **dev** file instead. Same project name, so it acts on the same containers, but it knows nothing about `cloudflared`, which the dev file does not define: the tunnel keeps running and the instance stays publicly reachable after the script prints "All services stopped". That is the same hazard `persist_compose_profile` closes, re-entering through the one entry point that change did not touch.

So `stop.sh` reads the running stack's own `com.docker.compose.project.config_files` label — compose's record of the files that created the project, which cannot drift the way a marker file or a heuristic can — and adds `-f docker-compose.hosted.yml` when it names the hosted file. Missing container or missing label (nothing running, or a pre-#2280 stack) degrades to the dev default, which is what the script always did.

It also runs `stop`, not `down`. `down` removes the platform containers and tears down `trinity-agent-network`, which every agent container is attached to — and it is the command `start.sh`'s own closing summary tells operators **not** to run, in both branches. A script named `stop.sh` running the forbidden verb was a standing contradiction.

## TLS (decided, documented, not automated)

Trinity serves plain HTTP and terminates TLS **outside** the application; there is no HTTPS listener in any compose file and no auto-certificate step. Three supported postures, documented in `docs/DEPLOYMENT.md`:

1. **Tunnel** — Cloudflare Tunnel; `cloudflared` is already a service, set `TUNNEL_TOKEN`. The default for a public instance, nothing to renew.
2. **Private network** — Tailscale/WireGuard/VPC, what the managed fleet runs. HTTP over a WireGuard tunnel is encrypted transport and a finished posture, not a compromise.
3. **Operator-run reverse proxy** — Caddy/nginx + Let's Encrypt.

Plain HTTP on a public IPv4 with none of the three is the one combination called out as unsafe. A marketplace droplet is the deliberate exception — it comes up on a bare public IP with no domain, so that channel provisions a Caddy sidecar with Let's Encrypt short-lived IP certificates (#2281) and Trinity shows a provenance-gated first-run hardening guide (#2380).

## Upgrade Path

```bash
echo 'TRINITY_IMAGE_TAG=v0.9.1' >> .env      # .env, not the shell — survives a reboot
./scripts/deploy/start.sh --hosted            # re-pulls platform images AND the agent base
```

A plain `docker compose -f docker-compose.hosted.yml pull` skips the base image (not a compose service) and leaves agents on the old runtime. Riding `latest` makes every re-run an unscheduled upgrade, which the summary warns about explicitly.

## Testing

| File | Covers |
|---|---|
| `tests/unit/test_2280_hosted_compose_parity.py` | Wholesale prod↔hosted service parity, third-party pin equality, top-level volumes/networks, the 8 GB floor in the header, `FRONTEND_PORT` in both files |
| `tests/unit/test_2280_publish_workflow_and_stop.py` | The verify step never passes a mixed-case owner as an image reference and lowercases the one it builds; `flavor: latest=false`; every non-sha tag gated on `github.event_name == 'push'` and the sha tag on nothing; `stop.sh` never runs `compose down` and selects its file from the compose label |
| `tests/unit/test_2390_start_sh_env_and_project_name.py` | `env_value()` dotenv parity with compose (quotes, inline comments, last-wins) and `compose_project_name()` against compose's real derivation, including the `_`/`-`-bearing directory names that made the data guard fail open |

## Outstanding

- **AC4** — the cloud-init user-data example (`trinity-ops-public/provision/cloud-init.sh`) taking admin password / domain / optional Cloudflare Tunnel token. Cross-repo, so it does not land here; #2280 stays open until it does.

## Explicit non-goals (flagged, not filed)

arm64/multi-arch publishing; image signing/attestation; making the agent base image reference configurable (rejected — it would require widening the SEC-172 allowlist, and retagging achieves the same result with no security surface); packaging the mounted `./config/*` into the images so `docker-compose.hosted.yml` can travel without the repo.

## Related

- Requirements: [infrastructure.md §8.9](../requirements/infrastructure.md)
- Docs: `docs/DEPLOYMENT.md` (server install + TLS), `docs/AGENT_INSTALL_GUIDE.md` (Step 1)
- Precedent for the compose-parity bug class: [database-backup.md](database-backup.md) (#2216 env forwarding)
