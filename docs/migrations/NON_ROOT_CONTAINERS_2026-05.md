# Non-Root Container Migration (Issue #874)

**Applies to**: existing Trinity deployments upgrading to the release that
contains issue #874 (non-root Trinity-built images).

**Fresh installs**: skip this document. `scripts/deploy/start.sh` now creates
the host data directory with the correct ownership automatically.

---

## What changed

Backend, scheduler, MCP server, and the production frontend now drop from
`root` to dedicated non-root users inside their containers:

| Service | Image user | UID |
|---------|-----------|-----|
| backend | `trinity` | 1000 |
| scheduler | `trinity` | 1000 |
| mcp-server | `node` (built-in) | 1000 |
| frontend (prod) | `nginx` (from `nginxinc/nginx-unprivileged`) | 101 |

On Linux hosts, backend additionally joins the host `docker` group via
`group_add: ${DOCKER_GID:-999}` so it can still read `/var/run/docker.sock`.

---

## Why an upgrade step is required

Trinity's persistent state lives on the host or in Docker named volumes that
predate this change:

- `/data` — bind mount to `${TRINITY_DATA_PATH:-./trinity-data}` in prod,
  Docker named volume `trinity-data` in dev. Contains `trinity.db` + WAL.
- `/agent-configs` — Docker named volume `agent-configs`. Contains uploaded
  agent templates.
- `/data/logs` — Docker named volume `trinity-logs`, shared with Vector. Owned
  by the `logs-init` one-shot (#1478).
- `/data/archives` — Docker named volume `trinity-archives` in dev; **inside the
  `/data` bind mount in prod**. Compressed log archives. Owned by the
  `archives-init` one-shot in dev (#2205).

**Nested mounts do not inherit the image's `chown`, and that is the trap.** The
Dockerfile creates and chowns `/data`, so a volume mounted *at* `/data` inherits
UID 1000 on first creation. `trinity-logs` and `trinity-archives` mount at paths
*below* it that the image never creates — there is nothing for Docker to copy
ownership from, so they are created root-owned and need an explicit one-shot.
`trinity-archives` shipped without one: `archive_storage` called
`mkdir(parents=True, exist_ok=True)`, which no-ops on an existing root-owned
directory, so nothing failed until the first write. Every archival run then died
with `Permission denied`, archived originals were never unlinked, and `/data/logs`
grew unbounded — 8.5 GB on the instance where it was found, one file at 4.6 GB,
with the error written into the very logs that were not being pruned (#2205).

`tests/unit/test_2205_volume_ownership.py` now fails if a named volume is mounted
into a UID-1000 service without one of these stories.

Docker only applies the image's `chown` to a named volume on first creation.
A volume that already exists from a previous root-running container keeps
its original ownership; the new UID 1000 backend cannot write to it.

For the bind mount, the Dockerfile's `chown` is masked entirely by the
mount — the host directory's ownership wins at runtime.

The symptoms are:

- Backend startup logs: `sqlite3.OperationalError: unable to open database file`.
- Agent template uploads silently fall back to the read-only template path
  and disappear on container restart (`services/agent_service/deploy.py`).
- Scheduler crash loop on the first cron fire (cannot write `trinity.db-wal`).

---

## Upgrade procedure (Linux)

Run the platform stack stopped first:

```bash
./scripts/deploy/stop.sh
```

### 1. Fix the data path ownership

**Production** (bind mount):

```bash
# Path is ${TRINITY_DATA_PATH:-./trinity-data} from the project root.
TRINITY_DATA_PATH="${TRINITY_DATA_PATH:-./trinity-data}"
sudo chown -R 1000:1000 "$TRINITY_DATA_PATH"
```

**Development** (named volume):

```bash
# Re-own the SQLite DB volume in place. The volume name depends on the
# compose project — check `docker volume ls` for `*_trinity-data`.
PROJECT="$(basename "$PWD")"
docker run --rm -v "${PROJECT}_trinity-data:/data" alpine \
    chown -R 1000:1000 /data
```

### 2. Re-own the `agent-configs` named volume

```bash
PROJECT="$(basename "$PWD")"
docker run --rm -v "${PROJECT}_agent-configs:/agent-configs" alpine \
    chown -R 1000:1000 /agent-configs
```

If you have no uploaded local templates to preserve, the simpler alternative
is to remove the volume and let it re-init clean on the next boot:

```bash
docker volume rm "${PROJECT}_agent-configs"
```

### 3. Confirm the host docker group GID matches

`.env.example` ships with `DOCKER_GID=999` (Debian / Ubuntu default).
RHEL / Fedora / Arch typically use a different GID. `start.sh` now
auto-detects on Linux when the .env value is blank, but check explicitly
on first run:

```bash
getent group docker | cut -d: -f3
# Update .env if the printed GID differs from the current DOCKER_GID value.
```

### 4. Bring the stack back up

```bash
./scripts/deploy/start.sh
```

If backend logs `PermissionError: [Errno 13] /var/run/docker.sock`, the
auto-detected `DOCKER_GID` is wrong — re-check step 3 and restart the
backend container only (`docker compose restart backend`).

---

## Upgrade procedure (macOS Docker Desktop)

Docker Desktop translates host UIDs through osxfs / virtiofs, so the chown
is mostly cosmetic. Volume re-init for `agent-configs` is still required
if you have an existing volume:

```bash
PROJECT="$(basename "$PWD")"
docker run --rm -v "${PROJECT}_agent-configs:/agent-configs" alpine \
    chown -R 1000:1000 /agent-configs
```

Docker Desktop does **not** ignore `group_add`: it presents the socket
root-group-owned (GID 0) inside the container, so the backend must join
GID 0, not the Debian/Ubuntu default 999. `./scripts/deploy/start.sh`
auto-detects this (it probes the in-container socket GID) and writes
`DOCKER_GID=0` to `.env`. If you skip start.sh, set `DOCKER_GID=0`
manually — leaving it at the 999 default denies socket access and the
Agents page silently shows "No agents" (#1131).

---

## Verification

After restart, confirm the new state:

```bash
docker exec trinity-backend id            # expect uid=1000(trinity)
docker exec trinity-scheduler id          # expect uid=1000(trinity)
docker exec trinity-mcp-server id         # expect uid=1000(node)

# Real Docker socket round-trip from inside the backend (group_add probe).
docker exec trinity-backend python3 -c \
    "import docker; print(docker.from_env().ping())"
# Expect: True
```

CI runs the same probes on every PR via the `verify-non-root` step in
`.github/workflows/container-security.yml`.

---

## Rollback

If the upgrade fails and you need to revert, downgrade the platform images
back to the previous tag and the existing root-owned volumes will work
again — the change is forward-only at the file-ownership layer but the
old image entrypoint runs as root and ignores ownership.
