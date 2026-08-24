# Installing Trinity — a runbook for AI agents

> **Audience: an AI agent (Claude Code, or any capable coding agent) installing Trinity on a user's computer at their request.**
> You are both the *operator* (you run the commands) and the *guide* (you explain each step to the user and hand them a friendly landing). Follow this top to bottom. It is deterministic on purpose: numbered steps, explicit success/failure checks, copy‑pasteable commands.
>
> **Stable URL** (fetch this mid‑install if you have web access):
> `https://raw.githubusercontent.com/abilityai/trinity/main/docs/AGENT_INSTALL_GUIDE.md`
>
> This guide is versioned with the installer (`scripts/deploy/start.sh`) — it describes what that script actually does, so it never drifts. Scope: the local **"install on my computer"** path (macOS Docker Desktop or Linux). Remote/cloud provisioning is a different flow (`trinity:deploy-new-instance`).

---

## How to communicate (bedside manner)

You are installing software on someone's machine. Be a good guide:

- **Confirm prerequisites before acting.** Tell the user what you're about to do and what it needs (Docker, a few GB of disk, ~5 min). Don't start a build silently.
- **Explain each step in one plain sentence** as you do it ("Pulling the platform images — this is the slow part, a few minutes").
- **Surface every secret you generate — once, clearly, and tell them to save it.** The installer auto‑generates an admin password and encryption keys. Show the admin password to the user explicitly; it's written to `.env` but they should store it in their password manager now.
- **Never invent a model API key.** If none is set, *ask the user* for an Anthropic API key (or a Claude subscription token / Google API key) — don't fabricate one or skip it silently. Agents can't run without it.
- **End with a friendly next‑steps card**, not a wall of logs. Give them the URL, their login, the plugin command, and one suggested first action.
- If a step fails, **stop and report the exact error + the remediation** from this guide rather than pushing on.

---

## Step 0 — Pre‑flight (verify, don't assume)

Run these checks first and report any failure to the user with the remediation. **Do not proceed if a hard requirement fails.**

```bash
docker info >/dev/null 2>&1 && echo "docker: ok"        || echo "docker: NOT RUNNING"
docker compose version >/dev/null 2>&1 && echo "compose: ok" || echo "compose: MISSING"
```

| Check | Requirement | If it fails — tell the user |
|-------|-------------|------------------------------|
| Docker daemon | reachable | "Docker isn't running. Start **Docker Desktop** (macOS/Windows) or `sudo systemctl start docker` (Linux), then I'll retry." Install: https://docs.docker.com/get-docker/ |
| Docker Compose v2 | `docker compose` (space, not `docker-compose`) | "Your Docker is too old — I need Compose v2. Update Docker Desktop / the compose plugin." |
| Disk | ~5 GB free | The base image build + platform images are a few GB. |
| Ports | 80, 8000, 8080, 6379 free | If something else owns them, ask the user to stop it, or set `FRONTEND_PORT=` in `.env` for the Web UI. On a **re‑install** these are Trinity's own — that's fine. |

`start.sh` re‑runs these same checks and fails fast with one consolidated message, so you don't have to be exhaustive here — but checking Docker up front gives the user a faster, clearer error than a mid‑build crash.

---

## Step 1 — Install (one shot, unattended)

**On a server — pull prebuilt images (`--hosted`). This is the default you want (#2280):**

```bash
git clone https://github.com/abilityai/trinity.git
cd trinity
export TRINITY_IMAGE_TAG=v0.9.0        # pin a release; `latest` moves every cut
./scripts/deploy/start.sh --hosted --unattended
```

Same script, same `.env` contract, same `ADMIN_PASSWORD` behaviour — it just
pulls the platform images and the agent base image from GHCR instead of
building them. That skips the 5-10 minute agent-base build, which is the slow
step you would otherwise have to narrate, and it is what makes a small VM
viable at all. Tell the user which tag you pinned.

**Fresh machine, building from source (a dev box, or a server with no registry access):**

```bash
git clone https://github.com/abilityai/trinity.git
cd trinity
./scripts/deploy/start.sh --unattended
```

**Already have the repo (or you cloned it):**

```bash
cd trinity   # the repo root
TRINITY_UNATTENDED=1 ./scripts/deploy/start.sh --unattended
```

What this does, in order (explain the slow ones to the user):
1. Creates `.env` from the template if missing.
2. **Auto‑generates** `CREDENTIAL_ENCRYPTION_KEY`, `SECRET_KEY`, `INTERNAL_API_SECRET`, `AGENT_AUTH_SECRET`, Redis passwords, and — in unattended mode — a strong **`ADMIN_PASSWORD`** (printed in the final summary).
3. Detects the Docker socket GID and the right Vector log source for the runtime.
4. Gets `trinity-agent-base:latest` — **pulled and retagged from GHCR in `--hosted` mode (fast)**, otherwise built locally (**slow — minutes**) — then `docker compose up -d`.
   The base image is not a compose service, so `--hosted` handles it separately; a bare `docker compose pull` would skip it and leave agents on the old runtime.
5. Polls the backend until it's actually serving (migrations + init done), then prints the next‑steps card.

> **Minimum size: 8 GB RAM.** Below that the agent containers and the platform services contend and turns start failing under load. Check before installing, and say so plainly if the machine is smaller.

> **Unattended is the key flag.** Without `--unattended` / `TRINITY_UNATTENDED=1`, `start.sh` *hard‑stops* asking the operator to choose an `ADMIN_PASSWORD` — which blocks an agent‑run install. With it, one is generated and surfaced.

---

## Step 2 — Confirm it's actually serving (not just "started")

`start.sh` already waits for health, but verify independently before you tell the user it's done:

```bash
curl -fsS -m 5 http://localhost:8000/health        # → {"status":"healthy",...}
curl -fsS -m 5 -o /dev/null -w '%{http_code}\n' http://localhost    # → 200 (or your FRONTEND_PORT)
```

- **Both succeed →** installation is complete; go to Step 3.
- **`/health` not responding after ~3 min →** containers may still be initializing (first‑run migrations / image build). Show the user: `docker compose logs -f backend`. Re‑running `./scripts/deploy/start.sh` is safe.

---

## Step 3 — Hand the user a friendly landing

Reproduce (don't just dump logs) a card like this, filling in the real values from the install output:

```
✅ Trinity is running.

  Open:   http://localhost           (or http://localhost:<FRONTEND_PORT>)
  Log in: admin / <ADMIN_PASSWORD>    ← save this now; it's in .env and I won't show it again

  Next:
    1. Finish the first‑run setup wizard in the UI (admin email + email whitelist).
    2. In Claude Code, install the agent toolkit:
         /plugin marketplace add abilityai/abilities
         /plugin install trinity@abilityai
       then run  /trinity:onboard  to build & connect your first agent.
    3. Or in the UI: Create Agent → pick a template.
```

- **The admin password:** if the installer generated one, it's the line the summary labels *auto‑generated*. Show it to the user verbatim and tell them to store it.
- **Model API key:** if the install output warned that no model key is set, **ask the user for one now** (Anthropic API key, Claude subscription token, or Google API key), add it to `.env` (`ANTHROPIC_API_KEY=...`), and re‑run `./scripts/deploy/start.sh`. Explain: their agents can't run until this is set.
- **First‑run setup wizard:** the first time they open the UI, Trinity asks for an admin email and whitelist. Walk them through it if they're unsure.

---

## Step 4 — Idempotency & re‑runs

Safe to re‑run `./scripts/deploy/start.sh` any time:
- existing secrets in `.env` are **kept**, not regenerated;
- a populated Redis is **never** re‑keyed (it refuses and points at `docs/migrations/REDIS_AUTH.md`);
- the "ports in use" warning on a re‑run is expected (Trinity owns them);
- to stop **without destroying agents**: `docker compose stop` (NOT `docker compose down` — `down` removes agent containers).

---

## Failure quick‑reference

| Symptom | Cause | Say / do |
|---------|-------|----------|
| `Docker daemon not reachable` | Docker not started | Start Docker Desktop / `systemctl start docker`, retry |
| `Docker Compose v2 missing` | old Docker | Update Docker; `docker compose version` must work |
| hard‑stop on `ADMIN_PASSWORD` | ran without `--unattended` | Re‑run with `--unattended` (generates + prints one) |
| `/health` never green | migrations / first build still running | `docker compose logs -f backend`; wait; re‑run start.sh |
| UI shows "Disconnected" / `ModuleNotFoundError` | stale platform images after a code pull | `docker compose build && docker compose up -d` |
| agents fail to run | no model API key | add `ANTHROPIC_API_KEY=...` to `.env`, re‑run start.sh |

---

*Keep this guide in sync with `scripts/deploy/start.sh`. If you change the installer's behavior or the ports/flags, update this file in the same PR — it is the single source of truth agents fetch.*
